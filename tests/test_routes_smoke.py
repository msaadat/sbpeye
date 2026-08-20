"""Characterization tests for the FastAPI routes that the refactor moves/dedups.

They assert response shape and status codes (not AI content) so the Phase 2 router
split and the Phase 1c chat-session dedup can be proven behavior-preserving.
"""

import json
from datetime import datetime
from bs4 import BeautifulSoup
import sbpeye.database as database_module
import sbpeye.main as main_module
from sbpeye import llm_debug
from sbpeye.models import (
    Attachment,
    CachedDocument,
    ChatMessage,
    ChatSession,
    CircularRelationship,
    LLMTrace,
    LLMTraceEvent,
    RegDocument,
    RegDocumentVersion,
    Settings,
    SyncStatus,
)
from sbpeye.scraper.circulars import circular_identity

from conftest import FakeAIClient, make_circular, use_tmp_data_root


def _seed_circular(db_factory, **overrides):
    db = db_factory()
    try:
        circular = make_circular(**overrides)
        db.add(circular)
        db.commit()
        return circular.id
    finally:
        db.close()


def test_circular_detail_shape(client):
    test_client, db_factory = client
    _seed_circular(db_factory, circular_id="c1", summary="A summary", tags=json.dumps(["AML"]))

    resp = test_client.get("/api/circulars/c1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "c1"
    assert body["title"] == "Test circular"
    assert body["tags"] == ["AML"]
    assert body["status"] == "active"
    assert body["attachments"] == []
    assert body["attachment_count"] == 0
    assert body["relationships"] == {"outgoing": [], "incoming": []}
    assert set(body["generation"]) == {"summary", "tags", "checklist", "relationships", "entities"}
    assert body["entities"] == []


def test_circular_detail_missing_returns_404(client):
    test_client, _ = client
    resp = test_client.get("/api/circulars/does-not-exist")
    assert resp.status_code == 404
    assert resp.json() == {"error": "Circular not found"}


def test_circular_relationships_shape(client):
    test_client, db_factory = client
    _seed_circular(db_factory, circular_id="src")
    _seed_circular(db_factory, circular_id="tgt")
    db = db_factory()
    try:
        db.add(
            CircularRelationship(
                source_id="src", target_id="tgt", type="supersedes", target_reference="ref", confidence=0.9
            )
        )
        db.commit()
    finally:
        db.close()

    resp = test_client.get("/api/circulars/src/relationships")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["outgoing"]) == 1
    rel = body["outgoing"][0]
    assert rel["type"] == "supersedes"
    assert rel["source"]["id"] == "src"
    assert rel["target"]["id"] == "tgt"
    assert rel["confidence"] == 0.9
    assert body["incoming"] == []


def test_circular_sync_worker_updates_status(db_factory, monkeypatch):
    captured = {}

    def fake_scrape_circulars(db, **kwargs):
        captured.update(kwargs)
        return {"processed": 2, "skipped": 1, "errors": 0}

    monkeypatch.setattr(main_module, "SessionLocal", db_factory)
    monkeypatch.setattr(main_module, "scrape_circulars", fake_scrape_circulars)
    main_module._REMOTE_CIRCULAR_CHECK_CACHE = {
        "remote_check_status": "new_available",
        "_expires_at": main_module.datetime.utcnow() + main_module.REMOTE_CIRCULAR_CHECK_TTL,
    }

    db = db_factory()
    try:
        db.add(SyncStatus(job_id="sync-1", status="queued"))
        db.commit()
    finally:
        db.close()

    options = main_module._sync_options_from_payload(
        {
            "departments": "bprd, epd",
            "years": "2025",
            "limit": 2,
            "include_attachments": False,
        }
    )
    assert main_module._CIRCULAR_SYNC_LOCK.acquire(blocking=False)
    main_module._run_circular_sync("sync-1", options)

    db = db_factory()
    try:
        status = db.query(SyncStatus).filter(SyncStatus.job_id == "sync-1").one()
        assert status.status == "success"
        assert status.processed_count == 2
        assert status.skipped_count == 1
        assert status.error_count == 0
    finally:
        db.close()

    assert captured["departments"] == ["bprd", "epd"]
    assert captured["years"] == ["2025"]
    assert captured["limit"] == 2
    assert captured["include_attachments"] is False
    assert captured["skip_llm"] is True
    assert main_module._REMOTE_CIRCULAR_CHECK_CACHE is None


def test_circular_sync_payload_validation():
    status = SyncStatus(job_id="sync-1", status="running", parameters='{"limit": 5}')
    payload = main_module._sync_status_payload(status)
    assert payload["running"] is True
    assert payload["parameters"] == {"limit": 5}

    try:
        main_module._sync_options_from_payload({"years": "25"})
    except ValueError as exc:
        assert "four-digit" in str(exc)
    else:
        raise AssertionError("Expected invalid sync year to be rejected")


def _listing_html(reference: str, slug: str = "new-circular") -> str:
    return f"""
    <div class="publication-box-new">
      <h4 class="mb-2"><a href="/circulars/{slug}">New Prudential Rules</a></h4>
      <p class="mb-3 date">{reference}</p>
      <p class="date">July 17 2026 | <span class="dept">BPRD</span> | <span class="cat">Banking</span> | <span class="type">Circulars</span></p>
    </div>
    """


def test_remote_circular_availability_detects_missing_listing_item(db_factory, monkeypatch):
    monkeypatch.setattr(
        main_module,
        "fetch_page",
        lambda url: BeautifulSoup(_listing_html("BPRD Circular No. 17 of 2026"), "html.parser"),
    )

    db = db_factory()
    try:
        payload = main_module._remote_circular_availability_payload(db)
    finally:
        db.close()

    assert payload["remote_check_status"] == "new_available"
    assert payload["remote_new_count"] == 1
    assert payload["remote_newest"]["reference"] == "BPRD Circular No. 17 of 2026"


def test_remote_circular_availability_is_fresh_when_listing_item_exists(db_factory, monkeypatch):
    reference = "BPRD Circular No. 17 of 2026"
    url = "https://www.sbp.org.pk/circulars/new-circular"
    circular_id = circular_identity(reference, url)
    monkeypatch.setattr(
        main_module,
        "fetch_page",
        lambda url: BeautifulSoup(_listing_html(reference), "html.parser"),
    )

    db = db_factory()
    try:
        db.add(make_circular(circular_id=circular_id, reference=reference, url=url))
        db.commit()
        payload = main_module._remote_circular_availability_payload(db)
    finally:
        db.close()

    assert payload["remote_check_status"] == "fresh"
    assert payload["remote_new_count"] == 0
    assert payload["remote_newest"] is None


def test_app_status_includes_remote_circular_fields(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr(
        main_module,
        "_remote_circular_check_status",
        lambda: {
            "remote_check_status": "new_available",
            "remote_checked_at": "2026-07-17T12:00:00",
            "remote_new_count": 2,
            "remote_newest": {"title": "New Prudential Rules"},
            "remote_error": None,
        },
    )

    resp = test_client.get("/api/app/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["remote_check_status"] == "new_available"
    assert body["remote_new_count"] == 2
    assert body["sync"]["remote_check_status"] == "new_available"

def _unexpected_reingest(*args, **kwargs):
    """`process_attachment` re-extracts and resets `is_vectorized`. A plain cache miss on
    a read must never reach it; only an explicit refresh may."""
    raise AssertionError("process_attachment must not run on a plain cache miss")


def test_document_content_redownloads_missing_attachment_file(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    use_tmp_data_root(monkeypatch, tmp_path)
    repaired_path = tmp_path / "files" / "circulars" / "c1" / "att-1.pdf"

    db = db_factory()
    try:
        circular = make_circular(circular_id="c1")
        db.add(circular)
        db.add(
            Attachment(
                id="att-1",
                circular_id="c1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="files/circulars/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
                content_text="original extracted text",
                is_vectorized=1,
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_download_attachment(circular_id, att_info, force=False):
        assert circular_id == "c1"
        assert att_info["id"] == "att-1"
        assert force is True
        repaired_path.parent.mkdir(parents=True, exist_ok=True)
        repaired_path.write_bytes(b"%PDF repaired")
        return repaired_path, True, None, att_info["url"]

    monkeypatch.setattr(main_module, "download_attachment", fake_download_attachment)
    monkeypatch.setattr(main_module, "process_attachment", _unexpected_reingest)

    resp = test_client.get("/api/documents/att-1/content")

    assert resp.status_code == 200
    assert resp.content == b"%PDF repaired"
    db = db_factory()
    try:
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
        assert attachment.local_path == "files/circulars/c1/att-1.pdf"
        # The pointer is the only thing the read is allowed to change. The text the
        # vector store was built from, and the ledger saying it is in there, both stand.
        assert attachment.content_text == "original extracted text"
        assert attachment.is_vectorized == 1
    finally:
        db.close()


def test_document_content_redownloads_missing_standalone_file(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    use_tmp_data_root(monkeypatch, tmp_path)
    repaired_path = tmp_path / "files" / "circulars" / "standalone" / "doc-1.pdf"

    db = db_factory()
    try:
        db.add(
            CachedDocument(
                id="doc-1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="files/circulars/standalone/missing.pdf",
                file_type="pdf",
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_download_attachment(circular_id, info, force=False):
        assert circular_id == "standalone"
        assert force is True
        repaired_path.parent.mkdir(parents=True, exist_ok=True)
        repaired_path.write_bytes(b"%PDF standalone")
        return repaired_path, True, None, info["url"]

    monkeypatch.setattr(main_module, "download_attachment", fake_download_attachment)

    resp = test_client.get("/api/documents/doc-1/content")

    assert resp.status_code == 200
    assert resp.content == b"%PDF standalone"
    db = db_factory()
    try:
        document = db.query(CachedDocument).filter(CachedDocument.id == "doc-1").one()
        assert document.local_path == "files/circulars/standalone/doc-1.pdf"
        assert document.error is None
    finally:
        db.close()


def test_document_content_reports_failed_redownload(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    use_tmp_data_root(monkeypatch, tmp_path)

    db = db_factory()
    try:
        circular = make_circular(circular_id="c1")
        db.add(circular)
        db.add(
            Attachment(
                id="att-1",
                circular_id="c1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="files/circulars/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_download_attachment(circular_id, att_info, force=False):
        return None, False, "download failed", None

    monkeypatch.setattr(main_module, "download_attachment", fake_download_attachment)
    monkeypatch.setattr(main_module, "process_attachment", _unexpected_reingest)

    resp = test_client.get("/api/documents/att-1/content")

    assert resp.status_code == 502
    assert resp.json() == {"error": "download failed"}

    # A failed fetch is not corpus state: the row is left exactly as it shipped.
    db = db_factory()
    try:
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
        assert attachment.extraction_status == "extracted"
        assert attachment.extraction_error is None
    finally:
        db.close()


def test_ensure_document_cached_redownloads_missing_attachment(db_factory, monkeypatch, tmp_path):
    use_tmp_data_root(monkeypatch, tmp_path)
    repaired_path = tmp_path / "files" / "circulars" / "c1" / "att-1.pdf"

    db = db_factory()
    try:
        circular = make_circular(circular_id="c1")
        db.add(circular)
        db.add(
            Attachment(
                id="att-1",
                circular_id="c1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="files/circulars/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
                is_vectorized=1,
            )
        )
        db.commit()
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()

        def fake_download_attachment(circular_id, att_info, force=False):
            assert circular_id == "c1"
            assert att_info["id"] == "att-1"
            assert force is True
            repaired_path.parent.mkdir(parents=True, exist_ok=True)
            repaired_path.write_bytes(b"%PDF repaired")
            return repaired_path, True, None, att_info["url"]

        monkeypatch.setattr(main_module, "download_attachment", fake_download_attachment)
        monkeypatch.setattr(main_module, "process_attachment", _unexpected_reingest)

        repaired, path = main_module._ensure_document_cached(db, attachment)

        assert repaired.id == "att-1"
        assert path == repaired_path
        assert repaired.local_path == "files/circulars/c1/att-1.pdf"
        assert repaired.is_vectorized == 1
    finally:
        db.close()


def test_ensure_document_cached_redownloads_missing_standalone(db_factory, monkeypatch, tmp_path):
    use_tmp_data_root(monkeypatch, tmp_path)
    repaired_path = tmp_path / "files" / "circulars" / "standalone" / "doc-1.pdf"

    db = db_factory()
    try:
        document = CachedDocument(
            id="doc-1",
            filename="rules.pdf",
            original_url="https://www.sbp.org.pk/files/rules.pdf",
            local_path="files/circulars/standalone/missing.pdf",
            file_type="pdf",
        )
        db.add(document)
        db.commit()

        def fake_download_attachment(circular_id, info, force=False):
            assert circular_id == "standalone"
            assert force is True
            repaired_path.parent.mkdir(parents=True, exist_ok=True)
            repaired_path.write_bytes(b"%PDF repaired")
            return repaired_path, True, None, info["url"]

        monkeypatch.setattr(main_module, "download_attachment", fake_download_attachment)

        repaired, path = main_module._ensure_document_cached(db, document)

        assert repaired.id == "doc-1"
        assert path == repaired_path
        assert repaired.local_path == "files/circulars/standalone/doc-1.pdf"
        assert repaired.error is None
    finally:
        db.close()


def test_workspace_crud_flow(client):
    test_client, db_factory = client
    _seed_circular(db_factory, circular_id="c1")

    # Default workspace is created on demand.
    resp = test_client.get("/api/workspaces")
    assert resp.status_code == 200
    assert any(ws["is_default"] for ws in resp.json())

    # Create a workspace.
    resp = test_client.post("/api/workspaces", json={"name": "Research A"})
    assert resp.status_code == 200
    workspace = resp.json()
    ws_id = workspace["id"]
    assert workspace["name"] == "Research A"
    assert workspace["is_default"] is False
    assert workspace["pinned_count"] == 0

    # Pin a circular.
    resp = test_client.post(f"/api/workspaces/{ws_id}/circulars", json={"circular_id": "c1"})
    assert resp.status_code == 200
    assert resp.json()["pinned_circular_ids"] == ["c1"]

    # Unpin it.
    resp = test_client.delete(f"/api/workspaces/{ws_id}/circulars/c1")
    assert resp.status_code == 200
    assert resp.json()["pinned_circular_ids"] == []

    # Delete the workspace.
    resp = test_client.delete(f"/api/workspaces/{ws_id}")
    assert resp.status_code == 200
    assert resp.json() == {"success": True}


def test_workspace_create_rejects_empty_name(client):
    test_client, _ = client
    resp = test_client.post("/api/workspaces", json={"name": "  "})
    assert resp.status_code == 400


def test_chat_message_creates_session_and_persists(client):
    test_client, db_factory = client
    resp = test_client.post("/api/chat", json={"message": "Hello there", "circular_ids": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["response"] == "fake assistant reply"
    session_id = body["session_id"]
    assert session_id

    db = db_factory()
    try:
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        assert session is not None
        assert session.title == "Hello there"
        roles = [m.role for m in db.query(ChatMessage).filter(ChatMessage.session_id == session_id).all()]
        assert roles == ["user", "assistant"]
    finally:
        db.close()


def test_default_chat_auto_scopes_unpinned_circular_for_one_turn(client, monkeypatch):
    test_client, db_factory = client
    _seed_circular(
        db_factory,
        circular_id="unselected",
        reference="BPRD Circular Letter No. 09 of 2026",
        title="Customer Onboarding Framework",
        date=datetime(2026, 3, 24),
    )
    captured = {}

    class CapturingAIClient:
        class Config:
            max_context_tokens = 4000

        config = Config()

        def chat(self, messages, db, **kwargs):
            captured.update(kwargs)
            return "scoped reply"

    # Chat resolves the requesting user's own credentials (7.5), so the capture has
    # to be installed on that resolver rather than the deployment-level one.
    monkeypatch.setattr(
        main_module, "get_ai_client_for_user", lambda user: CapturingAIClient()
    )

    response = test_client.post("/api/chat", json={
        "message": "Check BPRD Circular Letter No. 09 of 2026 for the limit",
        "session_id": "workspace:default",
    })

    assert response.status_code == 200
    assert captured["selected_circular_ids"] == ["unselected"]
    assert test_client.get("/api/workspaces/default").json()["pinned_count"] == 0

    db = db_factory()
    try:
        session = db.query(ChatSession).filter(
            ChatSession.id == "workspace:default"
        ).one()
        assert json.loads(session.circular_ids) == []
    finally:
        db.close()


def test_chat_message_rejects_empty(client):
    test_client, _ = client
    resp = test_client.post("/api/chat", json={"message": "   "})
    assert resp.status_code == 400


def test_chat_message_reuses_existing_session(client):
    test_client, _ = client
    first = test_client.post("/api/chat", json={"message": "First"}).json()
    session_id = first["session_id"]
    second = test_client.post(
        "/api/chat", json={"message": "Second", "session_id": session_id}
    ).json()
    assert second["session_id"] == session_id


def test_chat_stream_creates_session_and_streams(client):
    test_client, db_factory = client
    with test_client.stream("POST", "/api/chat/stream", json={"message": "Stream me"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: meta" in body
    assert "event: token" in body
    assert "event: done" in body
    assert "fake " in body and "stream reply" in body

    db = db_factory()
    try:
        sessions = db.query(ChatSession).all()
        assert len(sessions) == 1
        messages = db.query(ChatMessage).filter(
            ChatMessage.session_id == sessions[0].id
        ).all()
        assert [m.role for m in messages] == ["user", "assistant"]
        assert messages[1].content == "fake stream reply"
    finally:
        db.close()


def test_chat_stream_records_one_complete_trace(client, monkeypatch):
    """The streamed turn must leave a debug record good enough to answer "why that answer?".

    Starlette produces each SSE chunk on a different threadpool worker. Tracing state
    lives in a ContextVar, so unless the route pins a context, the trace opened while
    producing the first chunk is gone by the second: the provider payloads land on an
    orphan trace with no chat session, the later events are dropped, and the closing
    reset raises into the route — which is what made streamed chats undebuggable.
    """
    test_client, db_factory = client
    monkeypatch.setenv("LLM_DEBUG_ALLOWED", "true")
    db = db_factory()
    db.add(Settings(key="llm_debug_enabled", value="true"))
    db.commit()
    db.close()

    class TracingAIClient(FakeAIClient):
        """Mirrors AIClient.stream_chat: a nested trace around a multi-chunk stream."""

        def stream_chat(self, messages, db, **kwargs):
            yield from llm_debug.bind_context(self._traced(messages, db))

        def _traced(self, messages, db):
            with llm_debug.trace_operation("chat.turn", "implicit", provider="stub"):
                llm_debug.emit_event(
                    "provider_request", {"provider": "stub", "kwargs": {"model": "m"}},
                    stage="chat.iteration.1",
                )
                yield {"phase": "thinking"}
                yield "fake "
                yield {"phase": "tools", "tools": ["Searching circulars"]}
                llm_debug.emit_event(
                    "tool_result", {"name": "search_circulars"}, stage="chat.tools",
                )
                yield "stream reply"
                llm_debug.emit_event(
                    "provider_response", {"content": "fake stream reply", "stream": True},
                    stage="chat.iteration.1",
                )

    monkeypatch.setattr(
        main_module, "get_ai_client_for_user", lambda user: TracingAIClient()
    )

    with test_client.stream("POST", "/api/chat/stream", json={"message": "Trace me"}) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    assert "event: done" in body
    assert "event: error" not in body

    db = db_factory()
    try:
        session_id = db.query(ChatSession).one().id
        traces = db.query(LLMTrace).all()
        # One trace, not a web_chat stub plus a detached implicit one.
        assert len(traces) == 1
        trace = traces[0]
        assert (trace.origin, trace.status) == ("web_chat", "succeeded")
        assert trace.chat_session_id == session_id

        kinds = [
            event.kind
            for event in db.query(LLMTraceEvent)
            .filter(LLMTraceEvent.trace_id == trace.id)
            .order_by(LLMTraceEvent.sequence)
            .all()
        ]
        # Everything after the first chunk used to be missing entirely.
        assert kinds == [
            "operation_started", "context", "provider_request", "tool_result",
            "provider_response", "normalized_result", "persisted_result",
            "operation_completed",
        ]
    finally:
        db.close()


def test_chat_sessions_list_includes_default_workspace(client):
    test_client, _ = client
    resp = test_client.get("/api/chat/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert any(s.get("session_type") == "workspace" and s.get("is_default_workspace") for s in sessions)


def test_chat_session_get_missing_returns_404(client):
    test_client, _ = client
    resp = test_client.get("/api/chat/sessions/nope")
    assert resp.status_code == 404


def test_ensure_document_cached_refresh_reingests(db_factory, monkeypatch, tmp_path):
    """The counterpart to the tests above: an explicit refresh *is* a re-ingest."""
    use_tmp_data_root(monkeypatch, tmp_path)

    db = db_factory()
    try:
        db.add(make_circular(circular_id="c1"))
        db.add(
            Attachment(
                id="att-1",
                circular_id="c1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="files/circulars/c1/att-1.pdf",
                file_type="pdf",
                extraction_status="extracted",
                is_vectorized=1,
            )
        )
        db.commit()
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()

        calls = []

        def fake_process_attachment(db, circular, info, force_download=False, verbose=False):
            calls.append(force_download)
            return db.query(Attachment).filter(Attachment.id == "att-1").one()

        monkeypatch.setattr(main_module, "process_attachment", fake_process_attachment)

        main_module._ensure_document_cached(db, attachment, refresh=True)

        assert calls == [True]
    finally:
        db.close()


def _add_law_version(db, *, content_hash, local_path=None):
    db.add(
        RegDocument(
            id="d1",
            title="SBP Act",
            normalized_title="sbp act",
            doc_type="act",
            first_seen_at=datetime(2026, 8, 1),
            last_seen_at=datetime(2026, 8, 1),
        )
    )
    db.add(
        RegDocumentVersion(
            id="d1-v1",
            document_id="d1",
            content_hash=content_hash,
            file_url="https://www.sbp.org.pk/l/act.pdf",
            local_path=local_path,
            file_type="pdf",
            is_current=1,
            first_seen_at=datetime(2026, 8, 1),
            last_seen_at=datetime(2026, 8, 1),
        )
    )
    db.commit()


def test_law_file_redownloads_when_the_hash_matches(client, monkeypatch, tmp_path):
    """A missing archive file is refetched, and accepted because it is the same bytes."""
    test_client, db_factory = client
    use_tmp_data_root(monkeypatch, tmp_path)
    archived = tmp_path / "files" / "laws" / "d1" / "hash-a-act.pdf"

    db = db_factory()
    try:
        _add_law_version(db, content_hash="hash-a")
    finally:
        db.close()

    def fake_download_law_file(document_id, url, force=False):
        assert document_id == "d1"
        archived.parent.mkdir(parents=True, exist_ok=True)
        archived.write_bytes(b"%PDF act")
        return archived, "hash-a", None

    monkeypatch.setattr(main_module, "download_law_file", fake_download_law_file)

    resp = test_client.get("/api/laws/d1/file")

    assert resp.status_code == 200
    assert resp.content == b"%PDF act"
    db = db_factory()
    try:
        version = db.query(RegDocumentVersion).filter(RegDocumentVersion.id == "d1-v1").one()
        assert version.local_path == "files/laws/d1/hash-a-act.pdf"
    finally:
        db.close()


def test_law_file_refuses_when_the_live_file_is_a_different_edition(client, monkeypatch, tmp_path):
    """SBP replaces law PDFs in place. Bytes that do not hash to this version's hash are
    a different edition, and must not be served as though they were the archived one."""
    test_client, db_factory = client
    use_tmp_data_root(monkeypatch, tmp_path)
    fetched = tmp_path / "files" / "laws" / "d1" / "hash-b-act.pdf"

    db = db_factory()
    try:
        _add_law_version(db, content_hash="hash-a")
    finally:
        db.close()

    def fake_download_law_file(document_id, url, force=False):
        fetched.parent.mkdir(parents=True, exist_ok=True)
        fetched.write_bytes(b"%PDF a newer act")
        return fetched, "hash-b", None

    monkeypatch.setattr(main_module, "download_law_file", fake_download_law_file)

    resp = test_client.get("/api/laws/d1/file")

    assert resp.status_code == 404
    assert "different version" in resp.json()["error"]
    db = db_factory()
    try:
        version = db.query(RegDocumentVersion).filter(RegDocumentVersion.id == "d1-v1").one()
        # No row is re-pointed at the wrong bytes; capturing a new edition is sync's job.
        assert version.local_path is None
    finally:
        db.close()


def test_healthz_reports_every_backing_store(client):
    test_client, _ = client

    resp = test_client.get("/healthz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert set(body["checks"]) == {"corpus_db", "app_db", "vector_store"}
    assert all(v.startswith("ok") for v in body["checks"].values())


def test_healthz_survives_an_llm_provider_outage(client, monkeypatch):
    """The platform health check must not be coupled to the model provider.

    If it were, someone else's outage would fail the check and roll the container,
    taking down search and browsing along with chat.
    """
    test_client, _ = client

    def unreachable(*args, **kwargs):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(main_module, "get_ai_client", unreachable)

    assert test_client.get("/healthz").status_code == 200


def test_healthz_is_unhealthy_when_the_vector_store_fails(client, monkeypatch):
    test_client, _ = client

    def broken():
        raise RuntimeError("chroma segment is unreadable")

    monkeypatch.setattr(main_module, "has_vector_store_data", broken)

    resp = test_client.get("/healthz")

    assert resp.status_code == 503
    assert resp.json()["status"] == "unhealthy"
    # The reason is the exception class only: this endpoint is unauthenticated and a
    # store or database error carries filesystem paths in its message.
    assert resp.json()["checks"]["vector_store"] == "error: RuntimeError"
    assert "chroma segment" not in resp.text


def test_ecodata_entries_never_scrapes(client, monkeypatch):
    """The read path is pure.

    It used to refresh on a TTL from whichever request found the data stale, so an
    arbitrary user's page load blocked on a live scrape of sbp.org.pk and wrote the
    corpus with nobody to attribute the write to.
    """
    test_client, _ = client

    def unexpected_scrape(db):
        raise AssertionError("GET /api/ecodata/entries must not scrape")

    monkeypatch.setattr(main_module, "scrape_ecodata_index", unexpected_scrape)

    assert test_client.get("/api/ecodata/entries").status_code == 200


def test_ecodata_refresh_is_admin_only(client, db_factory):
    from conftest import sign_in, sign_out

    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    assert test_client.post("/api/ecodata/refresh").status_code == 403


def test_ecodata_refresh_scrapes_and_stamps_the_time(client, monkeypatch):
    test_client, db = client
    calls = []
    monkeypatch.setattr(main_module, "scrape_ecodata_index", lambda db: calls.append(1))

    response = test_client.post("/api/ecodata/refresh")

    assert response.status_code == 200
    assert calls == [1]
    session = db()
    try:
        stamped = session.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
        assert stamped is not None and stamped.ecodata_index_time is not None
    finally:
        session.close()


def test_a_refresh_already_running_is_reported_not_queued(client, monkeypatch):
    """Two concurrent scrapes would race on the same rows; the second is told to wait."""
    test_client, _ = client
    main_module._ECODATA_REFRESH_LOCK.acquire()
    try:
        response = test_client.post("/api/ecodata/refresh")
    finally:
        main_module._ECODATA_REFRESH_LOCK.release()

    assert response.status_code == 409
    assert "already running" in response.json()["error"]


def test_the_refresh_interval_is_configurable_and_disablable(monkeypatch):
    monkeypatch.delenv("SBPEYE_ECODATA_REFRESH_SECONDS", raising=False)
    assert main_module._ecodata_refresh_interval_seconds() == 3600

    monkeypatch.setenv("SBPEYE_ECODATA_REFRESH_SECONDS", "120")
    assert main_module._ecodata_refresh_interval_seconds() == 120

    # Zero disables the scheduler outright, which the loop checks before its first wait.
    monkeypatch.setenv("SBPEYE_ECODATA_REFRESH_SECONDS", "0")
    assert main_module._ecodata_refresh_interval_seconds() == 0

    # A typo falls back to the default rather than disabling the refresh silently.
    monkeypatch.setenv("SBPEYE_ECODATA_REFRESH_SECONDS", "hourly")
    assert main_module._ecodata_refresh_interval_seconds() == 3600
