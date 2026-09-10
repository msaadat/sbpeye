"""Retirement is bounded, backed up, restartable, and followed by fresh mirror fetches."""

from contextlib import closing
from datetime import datetime
import json
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye.circular_identity import circular_identity
from sbpeye.identity_migration import apply_migration, identity_preflight, open_readonly
from sbpeye.identity_removal import apply_removal, plan_removal, remove_legacy_vectors
from sbpeye.maintenance import maintenance
from sbpeye.migration_console import MigrationConsole
from sbpeye.models import (
    Base, Circular, Attachment, CircularRelationship, CircularEntity, CircularConsolidation,
    RegDocument, RegDocumentVersion, RegDocumentLink, AIGenerationJob, SemanticIndexSource,
)
from sbpeye.mirror_models import IdentityAlias, MirrorAudit, MirrorGap, MirrorAttempt


@pytest.fixture
def removal_data(tmp_path, monkeypatch, isolated_vector_store):
    from sbpeye import migration_console
    corpus, app = tmp_path / "sbpeye.db", tmp_path / "app.db"
    engine = create_engine(f"sqlite:///{corpus}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    ref, url = "FD Circular No. 1/2016", "https://www.sbp.org.pk/2016/FD/C1.htm"
    old, new = circular_identity(ref, url, identity_version=1), circular_identity(ref, url)
    archive = tmp_path / "old-annexure.pdf"
    archive.write_bytes(b"preserve these archived bytes")
    with factory() as db:
        db.add_all([
            Circular(id=old, reference=ref, title="Legacy", url=url, content_text="Old source", summary="Unverified analysis", tags='["Legacy"]'),
            Circular(id="kept", title="Unrelated", status="amended", relationships_generated_at=datetime.now()),
            RegDocument(id="law", title="Retained law", doc_type="law"),
        ])
        db.commit()
        db.add_all([
            Attachment(id="old-attachment", circular_id=old, filename="Annex.pdf", original_url=url + "/Annex.pdf", local_path=str(archive), content_text="Old annexure"),
            CircularEntity(circular_id=old, entity_type="numeric_limit"),
            CircularEntity(subject_kind="law", document_id="law", entity_type="numeric_limit"),
            CircularRelationship(source_id=old, target_id="kept", type="amends"),
            CircularRelationship(source_id="kept", target_id=old, type="references", target_reference=ref),
            CircularConsolidation(chain_id="kept", as_of_circular_id="kept", member_ids=json.dumps(["kept", old]), requirements=json.dumps([{"introduced_by": old}])),
            RegDocument(id="listing", title="Circular listing stub", circular_id=old),
            RegDocumentVersion(id="version", document_id="law", content_hash="law-hash", file_type="pdf", is_current=True),
            RegDocumentLink(circular_id=old, document_id="law", link_type="references"),
            AIGenerationJob(id="job", circular_id=old, feature="summary", status="success"),
            SemanticIndexSource(id="old-ledger", source_kind="circular", source_id=old, logical_kind="circular", logical_document_id=old),
            SemanticIndexSource(id="law-ledger", source_kind="law_version", source_id="version", logical_kind="law", logical_document_id="law"),
            MirrorGap(id=new, descriptor="{}", variants="[]", status="resolved", resolution_id=old, eligible=True),
            MirrorAttempt(id="history", job_id="history-job", circular_identity=old, origin="sync", descriptor=json.dumps({"stored_id": old}), outcome="resolved"),
        ])
        db.commit()
    with sqlite3.connect(corpus) as db:
        db.execute("CREATE VIRTUAL TABLE circulars_fts USING fts5(circular_id UNINDEXED, title, body, reference, department)")
        db.execute("INSERT INTO circulars_fts VALUES (?,?,?,?,?)", (old, "Legacy", "Old source", ref, "FD"))
    with sqlite3.connect(app) as db:
        db.execute("CREATE TABLE workspace_circulars (workspace_id TEXT, circular_id TEXT, note TEXT, PRIMARY KEY(workspace_id,circular_id))")
        db.execute("INSERT INTO workspace_circulars VALUES ('work',?, 'Keep my note')", (old,))
        db.execute("CREATE TABLE chat_messages (id TEXT PRIMARY KEY, circular_ids TEXT, content TEXT)")
        db.execute("INSERT INTO chat_messages VALUES ('message',?,?)", (json.dumps([old]), f"Historical [[c:{old}]]"))
    for identity, metadata in [
        ("old-body", {"circular_id": old, "doc_type": "circular"}),
        ("old-annex", {"attachment_id": "old-attachment", "doc_type": "attachment"}),
        ("law-chunk", {"circular_id": old, "kind": "law", "doc_type": "law"}),
        ("kept-chunk", {"circular_id": "kept", "doc_type": "circular"}),
    ]:
        isolated_vector_store.add(ids=[identity], metadatas=[metadata])
    manager = MigrationConsole(tmp_path / "maintenance", corpus, app, tmp_path / "html")
    monkeypatch.setattr(migration_console, "console", manager)
    yield manager, factory, old, new, archive, isolated_vector_store
    if manager.worker and manager.worker.is_alive():
        manager.worker.join(10)
    maintenance.pause(False)
    engine.dispose()


def finish(manager):
    manager.worker.join(15)
    assert not manager.worker.is_alive()
    return manager.status()


def prepare(manager):
    manager.prepare()
    state = finish(manager)
    assert state["can_remove"], state
    assert not state["can_apply"]  # Unverified data blocks re-keying, not retirement.
    return state


def test_remove_api_backs_up_only_selected_records_and_preserves_research(client, removal_data):
    http, _ = client
    manager, factory, old, new, archive, vectors = removal_data
    state = prepare(manager)
    assert state["removal_count"] == 1 and state["removal_attachment_count"] == 1
    before_app = manager.app.read_bytes()
    response = http.post("/api/circulars/mirror/identity/remove", json={"removal_hash": state["removal_hash"]})
    assert response.status_code == 202, response.text
    result = finish(manager)
    assert result["status"] == "complete", result
    assert result["operation"] == "remove_legacy" and result["removed_count"] == 1
    assert not maintenance.paused
    assert manager.app.read_bytes() == before_app
    assert archive.read_bytes() == b"preserve these archived bytes"
    assert set(vectors.records) == {"law-chunk", "kept-chunk"}
    with factory() as db:
        assert db.get(Circular, old) is None and db.get(Circular, new) is None
        assert db.get(Circular, "kept").status == "active"
        assert db.get(Circular, "kept").relationships_generated_at is None
        inbound = db.query(CircularRelationship).one()
        assert inbound.target_id is None and inbound.target_reference == "FD Circular No. 1/2016"
        assert db.query(CircularConsolidation).count() == 0
        assert db.query(Attachment).count() == 0
        assert db.query(CircularEntity).one().document_id == "law"
        assert db.get(RegDocumentVersion, "version").content_hash == "law-hash"
        assert db.get(RegDocument, "listing").circular_id is None
        assert db.query(RegDocumentLink).count() == 0
        assert db.get(AIGenerationJob, "job").result_status == "source_removed"
        assert db.get(SemanticIndexSource, "old-ledger") is None
        assert db.get(SemanticIndexSource, "law-ledger") is not None
        assert db.get(MirrorAttempt, "history").circular_identity == old
        assert db.get(IdentityAlias, ("circular", old)).new_id == new
        assert not db.get(MirrorGap, new).eligible
    with closing(open_readonly(manager.corpus)) as db:
        identity_preflight(db)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT count(*) FROM circulars_fts WHERE circular_id=?", (old,)).fetchone()[0] == 0
    backup = next((manager.root / "backups").glob("*-corpus.db"))
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT summary FROM circulars WHERE id=?", (old,)).fetchone()[0] == "Unverified analysis"
        assert db.execute("SELECT count(*) FROM attachments").fetchone()[0] == 1


@pytest.mark.parametrize("phase", ["corpus_committed", "indexes_verified"])
def test_removal_resumes_from_journal_without_second_backup(removal_data, phase):
    manager, _, old, _, _, _ = removal_data
    state = prepare(manager)
    manifest = json.loads((manager.root / "removal.json").read_text())
    def crash(current):
        if current == phase:
            raise RuntimeError("Container stopped")
    with pytest.raises(RuntimeError):
        apply_removal(manifest, manager.root / "backups", remove_vectors=remove_legacy_vectors, phase_hook=crash)
    restarted = MigrationConsole(manager.root, manager.corpus, manager.app, manager.html_cache)
    restarted.recover()
    assert restarted.status()["operation"] == "remove_legacy"
    assert maintenance.paused
    with pytest.raises(ValueError):
        restarted.cancel()
    with pytest.raises(ValueError, match="in progress"):
        restarted.apply(state["manifest_hash"])
    restarted.remove(state["removal_hash"])
    assert finish(restarted)["status"] == "complete"
    assert len(list((manager.root / "backups").glob("*-corpus.db"))) == 1


def test_failed_vector_cleanup_keeps_gate_and_resumes(removal_data):
    manager, _, _, _, _, _ = removal_data
    state = prepare(manager)
    def fail(_):
        raise RuntimeError("Vector store unavailable")
    manager.remove_vectors = fail
    manager.remove(state["removal_hash"])
    failed = finish(manager)
    assert failed["status"] == "failed" and failed["apply_started"]
    assert maintenance.paused
    manager.remove_vectors = None
    manager.remove(state["removal_hash"])
    assert finish(manager)["status"] == "complete"


def test_stale_or_client_selected_removal_is_refused(client, removal_data):
    http, _ = client
    manager, _, old, _, _, _ = removal_data
    state = prepare(manager)
    assert http.post("/api/circulars/mirror/identity/remove", json={"removal_hash": "0" * 64}).status_code == 409
    assert http.post("/api/circulars/mirror/identity/remove", json={"removal_hash": state["removal_hash"], "ids": ["kept"]}).status_code == 422
    with sqlite3.connect(manager.corpus) as db:
        db.execute("UPDATE circulars SET summary='changed' WHERE id=?", (old,))
    manager.remove(state["removal_hash"])
    failed = finish(manager)
    assert failed["status"] == "failed" and failed["can_cancel"]
    with sqlite3.connect(manager.corpus) as db:
        assert db.execute("SELECT 1 FROM circulars WHERE id=?", (old,)).fetchone()
    assert not list((manager.root / "backups").glob("*-corpus.db"))


def test_unknown_dependency_blocks_removal(removal_data):
    manager, _, old, _, _, _ = removal_data
    with sqlite3.connect(manager.corpus) as db:
        db.execute("CREATE TABLE unknown_links (id INTEGER PRIMARY KEY, circular_id TEXT REFERENCES circulars(id))")
        db.execute("INSERT INTO unknown_links VALUES (1,?)", (old,))
    report = plan_removal(manager.corpus, manager.app)
    assert any("unknown_links" in reason for item in report["conflicts"] for reason in item["reasons"])
    with pytest.raises(ValueError, match="structural"):
        apply_removal(report, manager.root / "backups", remove_vectors=lambda _: None)


def test_remove_requires_admin(client, removal_data):
    from conftest import sign_in
    http, factory = client
    sign_in(http, factory, user_id="reader-removal", is_admin=False)
    response = http.post("/api/circulars/mirror/identity/remove", json={"removal_hash": "0" * 64})
    assert response.status_code == 403


def test_mirror_rediscovers_removed_circular_and_fetches_fresh_html(removal_data, monkeypatch):
    from sbpeye.circular_jobs import create_backfill, run_backfill
    from sbpeye.identity_aliases import resolve_id
    from sbpeye.mirror import reconcile, publish_audit
    from sbpeye.mirror_types import BackfillRequest
    manager, factory, old, new, _, _ = removal_data
    state = prepare(manager)
    manager.remove(state["removal_hash"])
    assert finish(manager)["status"] == "complete"
    descriptor = dict(reference="FD Circular No. 1/2016", title="Fresh circular", url="https://www.sbp.org.pk/2016/FD/C1.htm", department="FD", year="2016", date="January 1, 2016")
    with factory() as db:
        db.add(MirrorAudit(id="fresh-audit", status="queued"))
        db.commit()
        report = reconcile([descriptor], [dict(id="kept", reference=None, url=None)])
        publish_audit(db, "fresh-audit", report, {"status": "success", "pages_total": 1, "diagnostics": []})
        assert db.get(MirrorGap, new).eligible
        job, count = create_backfill(db, BackfillRequest())
        assert count == 1
    calls = []
    def fresh(db, **options):
        calls.append(options)
        db.add(Circular(id=new, reference=descriptor["reference"], title="Fresh circular", url=descriptor["url"], content_text="Freshly fetched"))
        db.commit()
        options["outcome"].update(body="ready", fts="ready", vector="ready", attachments="deferred")
    monkeypatch.setattr("sbpeye.scraper.circulars.process_circular", fresh)
    run_backfill(job, BackfillRequest(), factory)
    assert calls[0]["force_fetch"] is True
    with factory() as db:
        assert db.get(MirrorGap, new).status == "resolved"
        replacement = db.get(Circular, resolve_id(db, old))
        assert replacement.content_text == "Freshly fetched" and replacement.summary is None


def test_removal_manifest_cannot_be_used_for_rekeying(removal_data):
    manager, _, _, _, _, _ = removal_data
    report = plan_removal(manager.corpus, manager.app)
    with pytest.raises(ValueError, match="removal workflow"):
        apply_migration(report, manager.root / "backups", repair_indexes=lambda _: None)


def test_removal_allows_existing_orphans_and_resumes_planned_journal(removal_data, monkeypatch):
    from sbpeye import identity_removal
    manager, factory, old, _, _, _ = removal_data
    with factory() as db:
        db.add(AIGenerationJob(id="orphan", circular_id="already-missing", feature="summary", status="success"))
        db.commit()
    state = prepare(manager)
    original = identity_removal._delete_source
    def fail(db, mapping, migration_id):
        original(db, mapping, migration_id)
        raise ValueError("Removal would leave invalid foreign keys")
    monkeypatch.setattr(identity_removal, "_delete_source", fail)
    manager.remove(state["removal_hash"])
    assert finish(manager)["status"] == "failed"
    monkeypatch.setattr(identity_removal, "_delete_source", original)
    manager.remove(state["removal_hash"])
    result = finish(manager)
    assert result["status"] == "complete", result
    with sqlite3.connect(manager.corpus) as db:
        assert db.execute("SELECT 1 FROM circulars WHERE id=?", (old,)).fetchone() is None
        assert db.execute("SELECT circular_id FROM ai_generation_jobs WHERE id='orphan'").fetchone()[0] == "already-missing"
        assert len(db.execute("PRAGMA foreign_key_check").fetchall()) == 1
    backups = list((manager.root / "backups").glob("*-corpus.db"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT 1 FROM circulars WHERE id=?", (old,)).fetchone()


@pytest.mark.parametrize("changed_existing", [False, True])
def test_removal_rolls_back_new_or_changed_foreign_key_violations(removal_data, monkeypatch, changed_existing):
    from sbpeye import identity_removal
    manager, factory, old, _, _, vectors = removal_data
    with factory() as db:
        db.add(AIGenerationJob(id="orphan", circular_id="already-missing", feature="summary", status="success"))
        db.commit()
    manifest = plan_removal(manager.corpus, manager.app)
    original = identity_removal._delete_source
    def break_link(db, mapping, migration_id):
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        original(db, mapping, migration_id)
        db.execute("UPDATE ai_generation_jobs SET circular_id=? WHERE id=?",
                   ("different-missing" if changed_existing else old, "orphan" if changed_existing else "job"))
    monkeypatch.setattr(identity_removal, "_delete_source", break_link)
    with pytest.raises(ValueError, match="new or changed invalid foreign keys"):
        apply_removal(manifest, manager.root / "backups", remove_vectors=remove_legacy_vectors)
    with sqlite3.connect(manager.corpus) as db:
        assert db.execute("SELECT 1 FROM circulars WHERE id=?", (old,)).fetchone()
        assert db.execute("SELECT circular_id FROM ai_generation_jobs WHERE id='orphan'").fetchone()[0] == "already-missing"
        assert not db.execute("SELECT 1 FROM identity_alias").fetchone()
        assert len(db.execute("PRAGMA foreign_key_check").fetchall()) == 1
    assert "old-body" in vectors.records


@pytest.mark.parametrize("storage", ["", "WITHOUT ROWID"])
def test_violation_snapshot_detects_changed_composite_keys(storage):
    from sbpeye.identity_removal import _foreign_key_violations
    with closing(sqlite3.connect(":memory:")) as db:
        db.execute("CREATE TABLE parent (a TEXT, b TEXT, PRIMARY KEY(a,b))")
        db.execute(f"CREATE TABLE child (id INTEGER PRIMARY KEY, a TEXT, b TEXT, FOREIGN KEY(a,b) REFERENCES parent(a,b)) {storage}")
        db.execute("INSERT INTO child VALUES (1,'missing','first')")
        before = _foreign_key_violations(db)
        assert before and not _foreign_key_violations(db) - before
        db.execute("UPDATE child SET b='second'")
        assert _foreign_key_violations(db) - before
        db.execute("DELETE FROM child")
        assert not _foreign_key_violations(db) - before
