from conftest import TEST_ADMIN_ID
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from sbpeye.ai import AIClient, AIConfig
from sbpeye.database import AppBase, Base, DebugBase, _relocate_app_tables, _relocate_trace_tables
from sbpeye import llm_debug
from sbpeye.models import (
    ChatMessage,
    ChatSession,
    LLMTrace,
    LLMTraceEvent,
    ResearchWorkspace,
    Settings,
    WorkspaceCircular,
)
from sbpeye.api import debug as debug_api


def _debug_database(tmp_path: Path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'debug.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    AppBase.metadata.create_all(engine)
    DebugBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    # Production splits these across three files; one factory here so a test can read
    # the `llm_debug_enabled` setting and the traces it produced from the same place.
    monkeypatch.setattr(llm_debug, "AppSessionLocal", factory)
    monkeypatch.setattr(llm_debug, "DebugSessionLocal", factory)
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


def _drive_across_threads(iterator):
    """Resume ``iterator`` the way Starlette's threadpool drives a sync generator.

    Each chunk is produced on a different worker, so nothing a step writes to a
    ContextVar survives into the next step unless the iterator pins a context of
    its own. This is what silently emptied every streamed chat trace.
    """
    chunks = []
    while True:
        box = {}

        def step():
            try:
                box["value"] = next(iterator)
            except StopIteration:
                box["stop"] = True
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                box["error"] = exc

        worker = threading.Thread(target=step)
        worker.start()
        worker.join()
        if "error" in box:
            raise box["error"]
        if box.get("stop"):
            return chunks
        chunks.append(box["value"])


def test_streamed_trace_keeps_every_event_across_worker_threads(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)

    def turn():
        with llm_debug.trace_operation(
            "chat.turn", "web_chat", chat_session_id="session-1",
        ):
            llm_debug.emit_event("context", {"step": "open"}, stage="chat.context")
            yield "first"
            # A nested trace_operation must still find the parent here, or it
            # opens an orphan trace carrying no chat session.
            with llm_debug.trace_operation("chat.turn", "implicit") as nested:
                assert nested is not None and nested.origin == "web_chat"
                yield "second"
            llm_debug.emit_event("normalized_result", {"step": "close"}, stage="chat.result")

    assert _drive_across_threads(llm_debug.bind_context(turn())) == ["first", "second"]

    db = factory()
    traces = db.query(LLMTrace).all()
    assert len(traces) == 1
    assert traces[0].chat_session_id == "session-1"
    assert traces[0].status == "succeeded"
    events = db.query(LLMTraceEvent).order_by(LLMTraceEvent.sequence).all()
    assert [event.kind for event in events] == [
        "operation_started", "context", "normalized_result", "operation_completed",
    ]
    db.close()


def test_abandoned_stream_closes_its_trace_in_the_pinned_context(tmp_path, monkeypatch):
    factory = _debug_database(tmp_path, monkeypatch)
    cleaned = []

    def turn():
        with llm_debug.trace_operation("chat.turn", "web_chat"):
            try:
                yield "first"
                yield "second"
            finally:
                cleaned.append(llm_debug.current_trace() is not None)

    stream = llm_debug.bind_context(turn())
    assert next(stream) == "first"
    stream.close()

    assert cleaned == [True]
    db = factory()
    trace = db.query(LLMTrace).one()
    assert trace.status == "cancelled"
    db.close()


def test_unpinned_generator_never_raises_tracing_errors_at_the_caller(tmp_path, monkeypatch):
    """Tracing is fail-open: a stray context must cost events, never the response."""
    _debug_database(tmp_path, monkeypatch)

    def turn():
        with llm_debug.trace_operation("chat.turn", "web_chat"):
            yield "first"
            yield "second"

    assert _drive_across_threads(turn()) == ["first", "second"]


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
    # `status` is the one route spanning both databases: the enabled flag is a
    # Settings row in the application database, the counts come from the debug one.
    # The fixture collapses them onto a single session.
    gate = debug_api.status(db, db)
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


def test_trace_tables_stay_out_of_the_application_metadata():
    """`Base.metadata.create_all(engine)` must never put traces back in sbpeye.db."""
    assert "llm_traces" not in Base.metadata.tables
    assert "llm_trace_events" not in Base.metadata.tables
    assert "llm_traces" in DebugBase.metadata.tables
    assert "llm_trace_events" in DebugBase.metadata.tables


def test_relocate_moves_legacy_traces_and_drops_them(tmp_path):
    """An existing checkout keeps its history and stops paying for it in sbpeye.db."""
    app_path, debug_path = tmp_path / "app.db", tmp_path / "debug.db"
    source = create_engine(f"sqlite:///{app_path}")
    target = create_engine(f"sqlite:///{debug_path}")

    # Seed the legacy layout: trace tables living in the application database.
    DebugBase.metadata.create_all(source)
    seed = sessionmaker(bind=source)()
    seed.add(LLMTrace(
        id="t-1", operation="chat.turn", origin="web_chat", status="succeeded",
        metadata_json="{}", started_at=llm_debug.datetime.utcnow(),
        updated_at=llm_debug.datetime.utcnow(),
    ))
    seed.add(LLMTraceEvent(
        trace_id="t-1", sequence=1, kind="context", payload_json="{}",
        payload_bytes=2, created_at=llm_debug.datetime.utcnow(),
    ))
    seed.commit()
    seed.close()

    assert _relocate_trace_tables(source, target, debug_path) == 2

    moved = sessionmaker(bind=create_engine(f"sqlite:///{debug_path}"))()
    assert moved.query(LLMTrace).one().id == "t-1"
    assert moved.query(LLMTraceEvent).one().kind == "context"
    moved.close()

    with create_engine(f"sqlite:///{app_path}").connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert not names & {"llm_traces", "llm_trace_events"}

    # Idempotent: a second start finds nothing left to move.
    assert _relocate_trace_tables(source, target, debug_path) == 0


def test_runtime_tables_stay_out_of_the_corpus_metadata():
    """`Base.metadata.create_all(engine)` must never recreate these in sbpeye.db."""
    for table in (
        "research_workspaces", "workspace_circulars",
        "chat_sessions", "chat_messages", "settings",
    ):
        assert table not in Base.metadata.tables
        assert table in AppBase.metadata.tables


def test_corpus_session_cannot_see_runtime_tables(tmp_path):
    """A corpus session must not resolve app tables — the failure mode of the split.

    `Base.metadata.create_all` is what a corpus database is built from, so if an app
    table ever drifts back onto `Base` this query starts succeeding. It surfaces in
    production as a 500 from whichever route was handed the wrong session, which is
    how `/api/debug/status` broke: it passed `Depends(get_db)` into a settings read.
    """
    corpus = create_engine(f"sqlite:///{tmp_path / 'corpus.db'}")
    Base.metadata.create_all(corpus)
    session = sessionmaker(bind=corpus)()
    try:
        for model in (ChatSession, ChatMessage, ResearchWorkspace, WorkspaceCircular, Settings):
            with pytest.raises(OperationalError, match="no such table"):
                session.query(model).first()
            session.rollback()
    finally:
        session.close()


def test_relocate_moves_legacy_runtime_state_and_drops_it(tmp_path):
    """Workspaces, chat and settings move out of sbpeye.db with their rows intact."""
    corpus_path, app_path = tmp_path / "corpus.db", tmp_path / "app.db"
    source = create_engine(f"sqlite:///{corpus_path}")
    target = create_engine(f"sqlite:///{app_path}")

    # Seed the legacy layout: runtime state living in the corpus database.
    AppBase.metadata.create_all(source)
    seed = sessionmaker(bind=source)()
    seed.add(ResearchWorkspace(
        id="ws-1", name="Capital", is_default=1, search_state="{}",
        created_at=llm_debug.datetime.utcnow(),
        updated_at=llm_debug.datetime.utcnow(),
    ))
    seed.add(WorkspaceCircular(
        workspace_id="ws-1", circular_id="circular-1", role="pinned",
        added_at=llm_debug.datetime.utcnow(),
    ))
    seed.add(ChatSession(user_id=TEST_ADMIN_ID, id="cs-1", title="MCR question"))
    seed.add(ChatMessage(
        id="cm-1", session_id="cs-1", role="user", content="What is the MCR?",
    ))
    seed.add(Settings(key="ai_provider", value="groq"))
    seed.commit()
    seed.close()

    assert _relocate_app_tables(source, target, app_path) == 5

    moved = sessionmaker(bind=create_engine(f"sqlite:///{app_path}"))()
    assert moved.query(ResearchWorkspace).one().name == "Capital"
    assert moved.query(WorkspaceCircular).one().circular_id == "circular-1"
    assert moved.query(ChatMessage).one().content == "What is the MCR?"
    assert moved.query(Settings).one().value == "groq"
    moved.close()

    with create_engine(f"sqlite:///{corpus_path}").connect() as conn:
        names = set(inspect(conn).get_table_names())
    assert not names & {
        "research_workspaces", "workspace_circulars",
        "chat_sessions", "chat_messages", "settings",
    }

    # Idempotent: a second start finds nothing left to move.
    assert _relocate_app_tables(source, target, app_path) == 0


def test_only_gateway_calls_chat_completions_create():
    root = Path(__file__).resolve().parents[1] / "src" / "sbpeye"
    occurrences = []
    for path in root.rglob("*.py"):
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if "chat.completions.create" in line:
                occurrences.append((path.name, number))
    assert len(occurrences) == 1
    assert occurrences[0][0] == "ai.py"
