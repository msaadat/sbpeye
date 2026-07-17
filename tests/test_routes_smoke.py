"""Characterization tests for the FastAPI routes that the refactor moves/dedups.

They assert response shape and status codes (not AI content) so the Phase 2 router
split and the Phase 1c chat-session dedup can be proven behavior-preserving.
"""

import json
from bs4 import BeautifulSoup
import sbpeye.database as database_module
import sbpeye.main as main_module
from sbpeye.models import Attachment, CachedDocument, ChatMessage, ChatSession, CircularRelationship, SyncStatus
from sbpeye.scraper.circulars import circular_identity

from conftest import make_circular


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

def test_document_content_redownloads_missing_attachment_file(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database_module, "PROJECT_ROOT", tmp_path)
    repaired_path = tmp_path / "attachments" / "c1" / "att-1.pdf"

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
                local_path="attachments/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_process_attachment(db, circular, info, force_download=False, verbose=False):
        assert circular.id == "c1"
        assert info["id"] == "att-1"
        assert force_download is True
        repaired_path.parent.mkdir(parents=True, exist_ok=True)
        repaired_path.write_bytes(b"%PDF repaired")
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
        attachment.local_path = str(repaired_path.relative_to(tmp_path))
        attachment.extraction_status = "extracted"
        attachment.extraction_error = None
        db.commit()
        return attachment

    monkeypatch.setattr(main_module, "process_attachment", fake_process_attachment)

    resp = test_client.get("/api/documents/att-1/content")

    assert resp.status_code == 200
    assert resp.content == b"%PDF repaired"
    db = db_factory()
    try:
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
        assert attachment.local_path == "attachments/c1/att-1.pdf"
    finally:
        db.close()


def test_document_content_redownloads_missing_standalone_file(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database_module, "PROJECT_ROOT", tmp_path)
    repaired_path = tmp_path / "attachments" / "standalone" / "doc-1.pdf"

    db = db_factory()
    try:
        db.add(
            CachedDocument(
                id="doc-1",
                filename="rules.pdf",
                original_url="https://www.sbp.org.pk/files/rules.pdf",
                local_path="attachments/standalone/missing.pdf",
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
        assert document.local_path == "attachments/standalone/doc-1.pdf"
        assert document.error is None
    finally:
        db.close()


def test_document_content_reports_failed_redownload(client, monkeypatch, tmp_path):
    test_client, db_factory = client
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database_module, "PROJECT_ROOT", tmp_path)

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
                local_path="attachments/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
            )
        )
        db.commit()
    finally:
        db.close()

    def fake_process_attachment(db, circular, info, force_download=False, verbose=False):
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
        attachment.extraction_status = "error"
        attachment.extraction_error = "download failed"
        db.commit()
        return attachment

    monkeypatch.setattr(main_module, "process_attachment", fake_process_attachment)

    resp = test_client.get("/api/documents/att-1/content")

    assert resp.status_code == 502
    assert resp.json() == {"error": "download failed"}


def test_ensure_document_cached_redownloads_missing_attachment(db_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database_module, "PROJECT_ROOT", tmp_path)
    repaired_path = tmp_path / "attachments" / "c1" / "att-1.pdf"

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
                local_path="attachments/c1/missing.pdf",
                file_type="pdf",
                extraction_status="extracted",
            )
        )
        db.commit()
        attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()

        def fake_process_attachment(db, circular, info, force_download=False, verbose=False):
            assert info["id"] == "att-1"
            assert force_download is True
            repaired_path.parent.mkdir(parents=True, exist_ok=True)
            repaired_path.write_bytes(b"%PDF repaired")
            attachment = db.query(Attachment).filter(Attachment.id == "att-1").one()
            attachment.local_path = str(repaired_path.relative_to(tmp_path))
            attachment.extraction_status = "extracted"
            attachment.extraction_error = None
            db.commit()
            return attachment

        monkeypatch.setattr(main_module, "process_attachment", fake_process_attachment)

        repaired, path = main_module._ensure_document_cached(db, attachment)

        assert repaired.id == "att-1"
        assert path == repaired_path
        assert repaired.local_path == "attachments/c1/att-1.pdf"
    finally:
        db.close()


def test_ensure_document_cached_redownloads_missing_standalone(db_factory, monkeypatch, tmp_path):
    monkeypatch.setattr(main_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(database_module, "PROJECT_ROOT", tmp_path)
    repaired_path = tmp_path / "attachments" / "standalone" / "doc-1.pdf"

    db = db_factory()
    try:
        document = CachedDocument(
            id="doc-1",
            filename="rules.pdf",
            original_url="https://www.sbp.org.pk/files/rules.pdf",
            local_path="attachments/standalone/missing.pdf",
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
        assert repaired.local_path == "attachments/standalone/doc-1.pdf"
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
