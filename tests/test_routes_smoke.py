"""Characterization tests for the FastAPI routes that the refactor moves/dedups.

They assert response shape and status codes (not AI content) so the Phase 2 router
split and the Phase 1c chat-session dedup can be proven behavior-preserving.
"""

import json

from sbpeye.models import ChatMessage, ChatSession, CircularRelationship

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
    assert set(body["generation"]) == {"summary", "tags", "checklist", "relationships"}


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
