"""Online migration drains work, preserves provenance gates, and resumes after crashes."""

import asyncio
import json
import sqlite3
import threading
import uuid

import pytest

from sbpeye.circular_identity import circular_identity
from sbpeye.maintenance import MaintenanceGate, MaintenanceMiddleware, maintenance, web_process_lease
from sbpeye.migration_console import MigrationConsole


@pytest.fixture
def console(tmp_path, monkeypatch):
    from sbpeye import migration_console
    corpus, app = tmp_path / "sbpeye.db", tmp_path / "app.db"
    ref, url = "FD Circular No. 1/2016", "https://www.sbp.org.pk/2016/FD/C1.htm"
    old = circular_identity(ref, url, identity_version=1)
    with sqlite3.connect(corpus) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE circulars (id TEXT PRIMARY KEY, reference TEXT, url TEXT, content_text TEXT, summary TEXT)")
        db.execute("INSERT INTO circulars VALUES (?,?,?,?,?)", (old, ref, url, "2016 circular body", "2016 analysis"))
    with sqlite3.connect(app) as db:
        db.execute("CREATE TABLE chat_messages (id TEXT PRIMARY KEY, circular_ids TEXT, content TEXT)")
        db.execute("INSERT INTO chat_messages VALUES (?,?,?)", ("chat", json.dumps([old]), "Historical prose"))
    manager = MigrationConsole(tmp_path / "maintenance", corpus, app, tmp_path / "html", repair=lambda report: None)
    monkeypatch.setattr(migration_console, "console", manager)
    yield manager
    if manager.worker:
        manager.worker.join(timeout=5)
    maintenance.pause(False)


def finish(manager):
    manager.worker.join(timeout=10)
    assert not manager.worker.is_alive()
    return manager.status()


def ready(manager):
    manager.prepare()
    state = finish(manager)
    old = state["mappings"][0]["old_id"]
    manager.review(old, "Source identifies FD Circular No. 1/2016 and supports the analysis.",
                   "Compared stored summary with the 2016 source.", ["analysis"], [], "admin")
    state = finish(manager)
    assert state["can_apply"], state
    return state


def test_console_routes_drain_and_gate_traffic(client, console):
    http, _ = client
    assert maintenance.enter()  # Simulate an in-flight request/background task.
    try:
        response = http.post("/api/circulars/mirror/identity/prepare")
        assert response.status_code == 202
        assert console.status()["status"] == "preparing"
        assert not (console.root / "manifest.json").exists()
        assert http.get("/api/circulars/search").status_code == 503
        assert http.post("/api/circulars/sync", json={}).status_code == 503
        assert http.get("/api/auth/me").status_code == 200
        assert http.get("/healthz").status_code == 200
        assert http.get("/api/circulars/mirror/identity").status_code == 200
        assert http.post("/api/circulars/mirror/identity/cancel").status_code == 409
    finally:
        maintenance.leave()
    state = finish(console)
    assert state["status"] == "review"
    assert not state["can_apply"]
    assert http.post("/api/circulars/mirror/identity/apply", json={"manifest_hash": state["manifest_hash"]}).status_code == 409
    assert http.post("/api/circulars/mirror/identity/cancel").status_code == 200
    assert http.get("/api/circulars/search").status_code == 200


def test_admin_review_apply_preserves_links_and_backups(client, console):
    http, _ = client
    http.post("/api/circulars/mirror/identity/prepare")
    state = finish(console)
    row = state["mappings"][0]
    before = (console.root / "state.json").read_bytes()
    detail = http.get(f"/api/circulars/mirror/identity/records/{row['old_id']}")
    assert detail.status_code == 200
    assert detail.json()["circular"]["summary"] == "2016 analysis"
    http.get("/api/circulars/mirror/identity")
    assert (console.root / "state.json").read_bytes() == before
    response = http.post("/api/circulars/mirror/identity/review", json={
        "old_id": row["old_id"], "source_text": "2016 source", "note": "Verified the 2016 analysis against this source.", "scopes": ["analysis"],
    })
    assert response.status_code == 202, response.text
    state = finish(console)
    assert state["can_apply"]
    assert http.post("/api/circulars/mirror/identity/apply", json={"manifest_hash": "0" * 64}).status_code == 409
    assert http.post("/api/circulars/mirror/identity/apply", json={"manifest_hash": state["manifest_hash"]}).status_code == 202
    assert finish(console)["status"] == "complete"
    assert not maintenance.paused
    with sqlite3.connect(console.corpus) as db:
        assert db.execute("SELECT id FROM circulars").fetchone()[0] == row["new_id"]
    with sqlite3.connect(console.app) as db:
        assert json.loads(db.execute("SELECT circular_ids FROM chat_messages").fetchone()[0]) == [row["new_id"]]
    backup = next((console.root / "backups").glob("*-corpus.db"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT id FROM circulars").fetchone()[0] == row["old_id"]


def test_index_failure_keeps_maintenance_until_resumed(console):
    state = ready(console)
    def fail(_):
        raise RuntimeError("Embedding service unavailable")
    console.repair = fail
    console.apply(state["manifest_hash"])
    failed = finish(console)
    assert failed["status"] == "failed" and failed["apply_started"]
    assert maintenance.paused
    with pytest.raises(ValueError, match="Resume"):
        console.cancel()
    restarted = MigrationConsole(console.root, console.corpus, console.app, console.html_cache, repair=lambda _: None)
    restarted.recover()
    assert restarted.status()["status"] == "interrupted"
    assert restarted.status()["phase"] == "app_committed"
    restarted.apply(state["manifest_hash"])
    assert finish(restarted)["status"] == "complete"
    assert not maintenance.paused
    assert len(list((console.root / "backups").glob("*-corpus.db"))) == 1


def test_stale_review_can_be_regenerated_without_bypassing_gate(console):
    state = ready(console)
    with sqlite3.connect(console.corpus) as db:
        db.execute("UPDATE circulars SET content_text='changed'")
    console.apply(state["manifest_hash"])
    state = finish(console)
    assert state["status"] == "failed" and state["can_cancel"]
    assert "changed" in state["error"]
    console.prepare()
    assert finish(console)["status"] == "review"
    console.cancel()


def test_attachment_review_requires_original_link_in_source(console):
    with sqlite3.connect(console.corpus) as db:
        old = db.execute("SELECT id FROM circulars").fetchone()[0]
        url = "https://www.sbp.org.pk/2016/FD/Annex.pdf"
        attachment_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{old}:{url}"))
        db.execute("CREATE TABLE attachments (id TEXT PRIMARY KEY, circular_id TEXT, original_url TEXT, filename TEXT, is_vectorized INTEGER)")
        db.execute("INSERT INTO attachments VALUES (?,?,?,?,0)", (attachment_id, old, url, "Annex.pdf"))
    console.prepare()
    finish(console)
    proof = [{"id": attachment_id, "detection_url": url}]
    console.review(old, '<a href="https://www.sbp.org.pk/wrong.pdf">Wrong</a>', "Reviewed attachment and analysis.", ["analysis"], proof, "admin")
    state = finish(console)
    assert not state["can_apply"]
    assert "attachment_detection_provenance_unverified" in state["mappings"][0]["conflicts"]
    console.review(old, f'<a href="{url}">Annexure to FD 1/2016</a>', "Reviewed the original annexure link and source.", [], proof, "admin")
    assert finish(console)["can_apply"]


def test_all_migration_routes_require_admin(client, console):
    from conftest import sign_in
    http, factory = client
    sign_in(http, factory, email="reader@example.test", is_admin=False)
    for method, path, body in [
        ("GET", "", None), ("GET", "/records/unknown", None),
        ("POST", "/prepare", {}), ("POST", "/cancel", {}),
        ("POST", "/apply", {"manifest_hash": "0" * 64}),
        ("POST", "/review", {"old_id": "x", "source_text": "x", "note": "A reviewed note", "scopes": ["analysis"]}),
    ]:
        response = http.request(method, "/api/circulars/mirror/identity" + path, json=body)
        assert response.status_code == 403, (path, response.text)


def test_api_rejects_client_filesystem_paths(client, console):
    http, _ = client
    response = http.post("/api/circulars/mirror/identity/review", json={
        "old_id": "x", "source_text": "x", "note": "A reviewed note", "scopes": ["analysis"], "source_path": "C:/secret",
    })
    assert response.status_code == 422


def test_pure_asgi_gate_tracks_work_after_response_body():
    gate = MaintenanceGate()
    async def scenario():
        body_sent, finish_background = asyncio.Event(), asyncio.Event()
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"done"})
            body_sent.set()
            await finish_background.wait()
        async def send(message):
            pass
        wrapped = MaintenanceMiddleware(app, gate)
        task = asyncio.create_task(wrapped({"type": "http", "path": "/api/chat"}, None, send))
        await body_sent.wait()
        gate.pause()
        assert gate.active == 1
        with pytest.raises(ValueError, match="Active requests"):
            gate.drain(timeout=0)
        finish_background.set()
        await task
        gate.drain(timeout=0)
    asyncio.run(scenario())


def test_background_reservations_and_second_process_lease(tmp_path):
    gate = MaintenanceGate()
    release = threading.Event()
    assert gate.start_thread(lambda: release.wait(5))
    try:
        gate.pause()
        assert gate.active == 1
        assert not gate.start_thread(lambda: None)
    finally:
        release.set()
    gate.drain(timeout=5)
    with web_process_lease(tmp_path):
        with pytest.raises(RuntimeError, match="one web process"):
            with web_process_lease(tmp_path):
                pass


def test_real_response_background_task_is_drained():
    from fastapi import BackgroundTasks, FastAPI
    from fastapi.testclient import TestClient
    from starlette.middleware.base import BaseHTTPMiddleware

    gate = MaintenanceGate()
    started, release = threading.Event(), threading.Event()
    app = FastAPI()
    def generation():
        started.set()
        release.wait(5)
    @app.post("/generate")
    def generate(tasks: BackgroundTasks):
        tasks.add_task(generation)
        return {"queued": True}
    # Reproduce main.py's BaseHTTPMiddleware boundary inside the drain middleware.
    async def passthrough(request, call_next):
        return await call_next(request)
    app.add_middleware(BaseHTTPMiddleware, dispatch=passthrough)
    app.add_middleware(MaintenanceMiddleware, gate=gate)
    with TestClient(app) as http:
        responses = []
        worker = threading.Thread(target=lambda: responses.append(http.post("/generate")))
        worker.start()
        try:
            assert started.wait(5)
            gate.pause()
            assert gate.active == 1
            assert http.post("/generate").status_code == 503
        finally:
            release.set()
            worker.join(5)
        gate.drain(timeout=1)
        assert responses[0].status_code == 200


def test_restart_during_review_keeps_evidence_and_allows_cancel(console):
    state = ready(console)
    restarted = MigrationConsole(console.root, console.corpus, console.app, console.html_cache)
    restarted.recover()
    restored = restarted.status()
    assert restored["maintenance"] and restored["can_apply"]
    assert restored["manifest_hash"] == state["manifest_hash"]
    restarted.cancel()
    assert not maintenance.paused


def test_worker_start_failure_does_not_wedge_operation(console, monkeypatch):
    def fail(self):
        raise RuntimeError("No worker available")
    monkeypatch.setattr(threading.Thread, "start", fail)
    with pytest.raises(RuntimeError, match="No worker"):
        console.prepare()
    console.worker = None
    assert not console.operation.locked()
    assert console.status()["can_cancel"]
    console.cancel()


def test_final_commit_before_console_update_is_resumable(console):
    from sbpeye.identity_migration import apply_migration
    state = ready(console)
    manifest = json.loads((console.root / "manifest.json").read_text(encoding="utf-8"))
    console._update(status="applying", apply_started=True)
    apply_migration(manifest, console.root / "backups", repair_indexes=lambda _: None)
    restarted = MigrationConsole(console.root, console.corpus, console.app, console.html_cache, repair=lambda _: None)
    restarted.recover()
    assert restarted.status()["status"] == "interrupted"
    assert maintenance.paused
    restarted.apply(state["manifest_hash"])
    assert finish(restarted)["status"] == "complete"
