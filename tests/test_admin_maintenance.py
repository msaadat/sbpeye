import json

from sbpeye import admin_maintenance as service
from sbpeye.models import Circular, Attachment, SyncStatus


def add_circular(db, identity, **kwargs):
    row = Circular(id=identity, title=identity, reference=identity, content_text="Some regulatory content", **kwargs)
    db.add(row)
    db.commit()
    return row


def test_unscanned_is_not_the_same_as_no_attachments(db_factory):
    from datetime import datetime
    with db_factory() as db:
        add_circular(db, "unscanned")
        add_circular(db, "scanned", attachments_scanned_at=datetime.now())
        rows = {r["id"]: r for r in service.assess(db)}
        assert rows["unscanned"]["attachments"] == "unscanned"
        assert rows["scanned"]["attachments"] == "ready"
        assert service.eligible(rows["unscanned"], "attachments")
        assert not service.eligible(rows["scanned"], "attachments")


def test_mixed_batch_scopes_by_saved_ids(db_factory):
    with db_factory() as db:
        add_circular(db, "one", department="BPRD")
        add_circular(db, "two", department="EPD")
        add_circular(db, "unrelated", department="EPD")
        db.add(SyncStatus(job_id="source", status="success", selection=json.dumps([{"id": "one"}, {"id": "two"}])))
        db.commit()
        assert {r["id"] for r in service.assess(db, source_job_id="source")} == {"one", "two"}


def test_legacy_ai_values_count_as_done(db_factory):
    with db_factory() as db:
        add_circular(db, "one", summary="Existing summary", tags="[]")
        row = service.assess(db)[0]
        assert row["ai"]["summary"] == row["ai"]["tags"] == "completed"
        assert not service.eligible(row, "ai", ["tags", "summary"])
        assert service.eligible(row, "ai", ["checklist"])


def test_audit_detects_missing_file_even_when_recorded_extracted(db_factory):
    from datetime import datetime
    with db_factory() as db:
        c = add_circular(db, "one", attachments_scanned_at=datetime.now())
        db.add(Attachment(id="att", circular_id=c.id, filename="gone.pdf", original_url="https://www.sbp.org.pk/gone.pdf", local_path="missing/gone.pdf", extraction_status="extracted", content_text="text"))
        db.commit()
        row = service.assess(db)[0]
        assert row["attachments"] == "needs_files"
        assert row["files"][0]["state"] == "missing_file"


def test_fts_verification_detects_changed_content_without_writes(db_factory):
    from sbpeye.search import index_circular_fts
    from sqlalchemy import text
    with db_factory() as db:
        c = add_circular(db, "one")
        index_circular_fts(db, c)
        before = db.execute(text("SELECT body FROM circulars_fts")).scalar()
        c.content_text = "Entirely different evidence"
        db.commit()
        assert service.assess(db, verify=True)[0]["keyword"] == "stale"
        assert db.execute(text("SELECT body FROM circulars_fts")).scalar() == before


def test_frozen_job_selection_and_failure_isolation(db_factory, monkeypatch):
    monkeypatch.setattr("sbpeye.circular_jobs.preflight", lambda db: None)
    with db_factory() as db:
        for identity in ["one", "two", "unrelated"]:
            add_circular(db, identity)
        job = service.create_job(db, service.BatchRequest(action="attachments", ids=["one", "two"]))
        job_id = job.job_id
    calls = []
    def perform(db, identity, action, features):
        calls.append(identity)
        if identity == "one":
            raise ValueError("SBP unavailable")
    monkeypatch.setattr(service, "perform", perform)
    service.run_job(job_id, db_factory)
    with db_factory() as db:
        job = db.query(SyncStatus).filter_by(job_id=job_id).one()
        assert calls == ["one", "two"]
        assert job.status == "failed" and job.processed_count == 1 and job.error_count == 1
        assert json.loads(job.progress)["items"][0]["error"] == "SBP unavailable"


def test_cancel_before_start_preserves_pending_selection(db_factory, monkeypatch):
    monkeypatch.setattr("sbpeye.circular_jobs.preflight", lambda db: None)
    with db_factory() as db:
        add_circular(db, "one")
        job_id = service.create_job(db, service.BatchRequest(action="attachments", ids=["one"])).job_id
    monkeypatch.setattr(service, "perform", lambda *args: (_ for _ in ()).throw(AssertionError("must not run")))
    service.cancel_job(job_id)
    service.run_job(job_id, db_factory)
    with db_factory() as db:
        job = db.query(SyncStatus).filter_by(job_id=job_id).one()
        assert json.loads(job.progress)["items"] == [{"id": "one", "status": "pending"}]
        assert json.loads(job.progress)["cancel_requested"]


def test_recovery_keeps_completed_items(db_factory):
    with db_factory() as db:
        db.add(SyncStatus(job_id="interrupted", status="running", parameters='{"operation":"maintenance"}', progress=json.dumps({"items": [{"id":"one", "status":"completed"}, {"id":"two", "status":"running"}]})))
        db.commit()
        service.recover(db)
        job = db.query(SyncStatus).filter_by(job_id="interrupted").one()
        assert job.status == "failed"
        assert [i["status"] for i in json.loads(job.progress)["items"]] == ["completed", "failed"]


def test_vector_audit_detects_wrong_passage_with_correct_count(db_factory):
    from sbpeye.database import collection
    from sbpeye.inventory.ledger import record_source
    with db_factory() as db:
        c = add_circular(db, "one")
        record_source(db, source_kind="circular", source_id=c.id, logical_kind="circular",
                      logical_document_id=c.id, text=c.content_text, indexed_chunks=1)
        collection.add(ids=["one__chunk_0"], documents=["wrong passage"], metadatas=[{"circular_id": "one"}])
        assert service.assess(db)[0]["vectors"][0]["state"] == "recorded"
        assert service.assess(db, verify=True)[0]["vectors"][0]["state"] == "stale"


def test_ai_batch_orders_features_across_all_documents(db_factory, monkeypatch):
    monkeypatch.setattr("sbpeye.circular_jobs.preflight", lambda db: None)
    with db_factory() as db:
        add_circular(db, "one")
        add_circular(db, "two")
        job_id = service.create_job(db, service.BatchRequest(action="ai", ids=["one", "two"], features=["tags", "summary"])).job_id
    calls = []
    monkeypatch.setattr(service, "perform", lambda db, identity, action, features: calls.append((identity, features)))
    service.run_job(job_id, db_factory)
    assert calls == [("one", ["summary"]), ("two", ["summary"]), ("one", ["tags"]), ("two", ["tags"])]


def test_console_routes_scope_and_enforce_admin(client, monkeypatch):
    from conftest import sign_out
    from sbpeye import main
    http, factory = client
    monkeypatch.setattr("sbpeye.circular_jobs.preflight", lambda db: None)
    with factory() as db:
        add_circular(db, "one")
    assert http.get("/api/admin/maintenance/documents").json()["items"][0]["id"] == "one"
    callbacks = []
    monkeypatch.setattr(main.maintenance, "start_thread", lambda worker: callbacks.append(worker) or True)
    try:
        response = http.post("/api/circulars/maintenance/jobs", json={"action":"attachments", "ids":["one"]})
        assert response.status_code == 202, response.text
        identity = response.json()["job_id"]
        assert http.get(f"/api/admin/maintenance/jobs/{identity}").json()["selection"] == ["one"]
        assert http.post("/api/circulars/maintenance/jobs", json={"action":"attachments", "ids":["one"]}).status_code == 409
        assert http.post(f"/api/circulars/maintenance/jobs/{identity}/cancel").status_code == 200
    finally:
        main._CIRCULAR_SYNC_LOCK.release()
    sign_out(http)
    assert http.get("/api/admin/maintenance/documents").status_code in {401, 403}
    assert http.post("/api/circulars/maintenance/jobs", json={"action":"attachments", "ids":["one"]}).status_code in {401, 403}


def test_index_repair_populates_both_indexes_and_verifies(db_factory, monkeypatch):
    from types import SimpleNamespace
    from sbpeye.scraper import circulars
    monkeypatch.setattr(circulars, "embedding_backend", SimpleNamespace(embed_documents=lambda texts: [[1.0, 0.0] for _ in texts]))
    with db_factory() as db:
        add_circular(db, "one")
        service.perform(db, "one", "index", [])
        row = service.assess(db, verify=True)[0]
        assert row["keyword"] == "verified"
        assert row["vectors"][0]["state"] == "verified"
        assert not service.eligible(row, "index")


def test_attachment_followup_adds_text_to_fts_and_vectors(db_factory, monkeypatch):
    from datetime import datetime
    from types import SimpleNamespace
    from sqlalchemy import text
    from sbpeye.scraper import circulars
    monkeypatch.setattr(circulars, "embedding_backend", SimpleNamespace(embed_documents=lambda texts: [[1.0, 0.0] for _ in texts]))
    monkeypatch.setattr(service, "file_present", lambda path: bool(path))
    def fetch(db, c):
        db.add(Attachment(id="annexure", circular_id=c.id, filename="annexure.pdf", original_url="https://www.sbp.org.pk/a.pdf", local_path="a.pdf", file_type="pdf", content_text="Important annexure obligations", extraction_status="extracted"))
        c.attachments_scanned_at = datetime.now()
        db.commit()
    monkeypatch.setattr(circulars, "fetch_attachments_for_circular", fetch)
    with db_factory() as db:
        add_circular(db, "one")
        service.perform(db, "one", "attachments", [])
        assert "annexure" in db.execute(text("SELECT body FROM circulars_fts WHERE circular_id='one'")).scalar()
        assert db.get(Attachment, "annexure").is_vectorized
        assert service.assess(db)[0]["attachments"] == "ready"
