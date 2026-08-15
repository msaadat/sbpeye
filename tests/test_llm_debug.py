import json
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import AIClient, AIConfig
from sbpeye.database import Base
from sbpeye import llm_debug
from sbpeye.models import LLMTrace, LLMTraceEvent, Settings
from sbpeye.api import debug as debug_api


def _debug_database(tmp_path: Path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'debug.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(llm_debug, "SessionLocal", factory)
    monkeypatch.setenv("LLM_DEBUG_ALLOWED", "true")
    db = factory()
    db.add(Settings(key="llm_debug_enabled", value="true"))
    db.commit()
    db.close()
    return factory


def test_trace_persists_ordered_events_and_active_snapshot(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)

    with llm_debug.trace_operation("circular.summary", "cli", target_id="c1"):
        llm_debug.emit_event("context", {"prompt": "complete evidence"})
        db = factory()
        db.query(Settings).filter(Settings.key == "llm_debug_enabled").one().value = "false"
        db.commit()
        db.close()
        llm_debug.emit_event("normalized_result", {"summary": "done"})

    with llm_debug.trace_operation("circular.tags", "cli") as disabled:
        assert disabled is None

    db = factory()
    traces = db.query(LLMTrace).all()
    assert len(traces) == 1
    assert traces[0].status == "succeeded"
    events = db.query(LLMTraceEvent).order_by(LLMTraceEvent.sequence).all()
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert [event.kind for event in events] == [
        "operation_started", "context", "normalized_result", "operation_completed",
    ]
    db.close()


def test_sanitizer_redacts_structural_secrets_without_losing_token_counts():
    canary = "CANARY-SECRET-9182"
    payload, _ = llm_debug.serialize_payload({
        "api_key": canary,
        "nested": {"Authorization": canary, "max_tokens": 100},
        "headers": {"safe-looking": canary},
        "request_url": f"https://example.test/v1?q=ok&access_token={canary}",
        "body": json.dumps({"client_secret": canary, "prompt_tokens": 14}),
    })
    assert canary not in payload
    parsed = json.loads(payload)
    assert parsed["nested"]["max_tokens"] == 100
    assert parsed["body"]["prompt_tokens"] == 14
    assert "headers" not in parsed
    try:
        raise RuntimeError(
            f"request failed at https://example.test/v1?api_key={canary} "
            f"with password={canary}"
        )
    except RuntimeError as exc:
        diagnostic = json.dumps(llm_debug.exception_payload(exc))
    assert canary not in diagnostic


def test_provider_gateway_records_exact_kwargs_and_returns_original(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)
    response = SimpleNamespace(
        id="req_123",
        model="fake-model",
        usage=SimpleNamespace(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content="answer", role="assistant", tool_calls=[]),
        )],
    )
    client = AIClient(AIConfig(provider="lmstudio", model="fake-model"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: response
    )))

    returned = client._create_traced_completion(
        stage="test", model="fake-model",
        messages=[{"role": "user", "content": "full prompt"}],
        temperature=0.2, max_tokens=25,
    )
    assert returned is response

    db = factory()
    trace = db.query(LLMTrace).one()
    assert trace.status == "succeeded"
    assert trace.attempt_count == 1
    assert trace.total_tokens == 10
    events = db.query(LLMTraceEvent).order_by(LLMTraceEvent.sequence).all()
    request = json.loads(next(event.payload_json for event in events if event.kind == "provider_request"))
    result = json.loads(next(event.payload_json for event in events if event.kind == "provider_response"))
    assert request["kwargs"]["messages"][0]["content"] == "full prompt"
    assert request["kwargs"]["max_tokens"] == 25
    assert result["id"] == "req_123"
    assert result["content"] == "answer"
    db.close()


def test_structured_fallback_stays_in_one_logical_trace(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)
    response = SimpleNamespace(
        id="req_ok", model="fake-model", usage=None,
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(content='{"items": []}', role="assistant", tool_calls=[]),
        )],
    )
    calls = 0

    def create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider does not support response_format json_schema")
        return response

    client = AIClient(AIConfig(provider="openai", model="fake-model", api_key="test"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    raw = client._complete_json(
        "system", "prompt",
        json_schema={"type": "object", "properties": {"items": {"type": "array"}}},
    )
    assert raw == '{"items": []}'

    db = factory()
    trace = db.query(LLMTrace).one()
    assert trace.status == "succeeded"
    assert trace.attempt_count == 2
    kinds = [row.kind for row in db.query(LLMTraceEvent).order_by(LLMTraceEvent.sequence)]
    assert kinds.count("provider_request") == 2
    assert "provider_error" in kinds
    assert "structured_mode_changed" in kinds
    assert "retry" in kinds
    assert kinds[-1] == "operation_completed"
    db.close()


def test_stream_is_reconstructed_into_one_response_event(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)
    chunks = [
        SimpleNamespace(
            id="stream_1", model="fake-model", usage=None,
            choices=[SimpleNamespace(
                finish_reason=None,
                delta=SimpleNamespace(role="assistant", content="hel", tool_calls=[]),
            )],
        ),
        SimpleNamespace(
            id="stream_1", model="fake-model",
            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            choices=[SimpleNamespace(
                finish_reason="stop",
                delta=SimpleNamespace(role=None, content="lo", tool_calls=[]),
            )],
        ),
    ]
    client = AIClient(AIConfig(provider="lmstudio", model="fake-model"))
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: iter(chunks)
    )))
    observed = list(client._create_traced_completion(
        stage="chat.iteration.1", model="fake-model", stream=True,
        messages=[{"role": "user", "content": "say hello"}],
    ))
    assert observed == chunks

    db = factory()
    trace = db.query(LLMTrace).one()
    assert trace.status == "succeeded"
    responses = db.query(LLMTraceEvent).filter(
        LLMTraceEvent.kind == "provider_response"
    ).all()
    assert len(responses) == 1
    payload = json.loads(responses[0].payload_json)
    assert payload["content"] == "hello"
    assert payload["finish_reason"] == "stop"
    assert payload["usage"]["total_tokens"] == 3
    assert responses[0].attempt_number == 1
    db.close()


def test_debug_api_payloads_filter_increment_and_prune(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)
    with llm_debug.trace_operation(
        "circular.summary", "cli", target_id="circular-1", job_id="job-1"
    ):
        llm_debug.emit_event("context", {"prompt": "evidence"})

    db = factory()
    gate = debug_api.status(db)
    assert gate["effective"] is True
    assert gate["trace_count"] == 1
    listing = debug_api.list_traces(
        status="succeeded", operation="circular.summary", origin="cli",
        provider=None, model=None, correlation="job-1", page=1, per_page=50, db=db,
    )
    assert listing["total"] == 1
    assert "events" not in listing["items"][0]
    trace_id = listing["items"][0]["id"]
    first = debug_api.trace_detail(trace_id, after_sequence=0, db=db)
    assert first["last_sequence"] == len(first["events"])
    assert debug_api.trace_detail(
        trace_id, after_sequence=first["last_sequence"], db=db
    )["events"] == []

    db.add(LLMTrace(
        id="still-running", operation="chat.turn", origin="web_chat",
        status="running", metadata_json="{}", started_at=llm_debug.datetime.utcnow(),
        updated_at=llm_debug.datetime.utcnow(),
    ))
    db.commit()
    result = debug_api.delete_all_traces(db)
    assert result == {"deleted": 1, "skipped_running": 1}
    assert db.query(LLMTrace).one().id == "still-running"
    assert db.query(LLMTraceEvent).count() == 0
    db.close()


def test_only_gateway_calls_chat_completions_create():
    root = Path(__file__).resolve().parents[1] / "src" / "sbpeye"
    occurrences = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "chat.completions.create" in line:
                occurrences.append((path.name, number))
    assert len(occurrences) == 1
    assert occurrences[0][0] == "ai.py"
