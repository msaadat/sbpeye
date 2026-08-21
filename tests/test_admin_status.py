"""The read-only admin status API: shape, the admin gate, and that it stays read-only.

The last of those is the point of the module. Every route under `/api/admin` exists so an
operator can look at the corpus and the index without a shell — and looking must never
change what is being looked at. `test_audit_does_not_write_the_ledger` is the guard: the
reconciler it calls has a `write=True` mode, and reaching for it here would turn a page
load into a corpus write with no admin action behind it.
"""

from datetime import datetime

from sbpeye.models import (
    AIGenerationJob,
    Attachment,
    CircularRelationship,
    RegDocument,
    RegDocumentVersion,
    SemanticIndexSource,
    SyncStatus,
)

from conftest import make_circular, sign_in, sign_out, use_tmp_data_root

ADMIN_ROUTES = (
    "/api/admin/corpus",
    "/api/admin/index",
    "/api/admin/index/audit",
    "/api/admin/runs",
    "/api/admin/environment",
)


def _seed_corpus(db_factory):
    db = db_factory()
    try:
        analysed = make_circular(
            circular_id="c1",
            summary="A summary",
            summary_generated_at=datetime(2026, 1, 1),
            tags_generated_at=datetime(2026, 1, 1),
            department="BPRD",
        )
        bare = make_circular(circular_id="c2", department="EPD", date=datetime(2024, 5, 1))
        db.add_all([analysed, bare])
        db.add(
            Attachment(
                id="a1",
                circular_id="c1",
                filename="annexure.pdf",
                original_url="https://www.sbp.org.pk/a1.pdf",
                file_type="pdf",
                content_text="body",
                extraction_status="extracted",
                is_vectorized=1,
            )
        )
        db.add(
            CircularRelationship(
                source_id="c1", target_id="c2", type="supersedes", confidence=0.9
            )
        )
        document = RegDocument(id="d1", title="Prudential Regulations", doc_type="regulation")
        db.add(document)
        db.add(
            RegDocumentVersion(
                id="v1",
                document_id="d1",
                content_hash="sha256:abc",
                file_type="pdf",
                content_text="law body",
                extraction_status="extracted",
                is_current=1,
                summary_generated_at=datetime(2026, 2, 1),
            )
        )
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------------ the gate


def test_every_admin_route_refuses_a_tester(client, db_factory):
    test_client, _ = client
    sign_out(test_client)
    sign_in(test_client, db_factory, is_admin=False)

    for path in ADMIN_ROUTES:
        assert test_client.get(path).status_code == 403, path


def test_every_admin_route_refuses_an_anonymous_caller(client):
    test_client, _ = client
    sign_out(test_client)

    for path in ADMIN_ROUTES:
        # The middleware answers before the route's admin dependency does, so this is a
        # 401 rather than a 403 — a new route is private by default (invariant 3.4).
        assert test_client.get(path).status_code == 401, path


# ---------------------------------------------------------------------------- shape


def test_corpus_overview_counts_and_coverage(client, db_factory):
    test_client, db_factory_ = client
    _seed_corpus(db_factory_)

    body = test_client.get("/api/admin/corpus").json()

    assert body["circulars"]["total"] == 2
    assert body["circulars"]["latest"]["id"] == "c1"
    assert {row["label"] for row in body["circulars"]["by_department"]} == {"BPRD", "EPD"}

    # "Touched by anything" is a different question from "complete", and the console
    # shows both: c1 has two features, c2 none.
    assert body["circulars"]["analysed"] == 1
    assert body["laws"]["analysed"] == 1

    coverage = {row["feature"]: row for row in body["circulars"]["coverage"]}
    # Coverage reads the generated-at timestamps, not the text columns: c1 was analysed
    # for summary and tags, neither circular for the rest.
    assert coverage["summary"] == {"feature": "summary", "generated": 1, "total": 2}
    assert coverage["checklist"]["generated"] == 0
    assert set(coverage) == {"summary", "tags", "checklist", "relationships", "entities"}

    assert body["attachments"]["total"] == 1
    assert body["attachments"]["vectorized"] == 1
    assert body["attachments"]["by_extraction_status"] == {"extracted": 1}

    assert body["laws"]["documents"] == 1
    assert body["laws"]["versions"] == 1
    assert body["laws"]["current_versions"] == 1

    assert body["relationships"] == {
        "total": 1,
        "resolved": 1,
        "by_type": {"supersedes": 1},
    }


def test_corpus_overview_on_an_empty_corpus(client):
    test_client, _ = client
    body = test_client.get("/api/admin/corpus").json()

    assert body["circulars"]["total"] == 0
    assert body["circulars"]["latest"] is None
    # A zero denominator has to survive rather than divide: the frontend renders these
    # as bars, and an empty corpus is a state the console must show, not a 500.
    assert all(row["total"] == 0 for row in body["circulars"]["coverage"])
    assert body["consolidations"] == {"total": 0, "stale": 0}


def test_index_overview_reports_recorded_state_and_drift(client, db_factory):
    test_client, db_factory_ = client
    db = db_factory_()
    try:
        db.add(
            SemanticIndexSource(
                id="s1",
                source_kind="circular",
                source_id="c1",
                logical_kind="circular",
                logical_document_id="c1",
                expected_chunks=3,
                indexed_chunks=3,
                status="indexed",
                chunker_version="v0-ancient",
                embedding_fingerprint="sha256:something-else",
                indexed_at=datetime(2026, 3, 1),
            )
        )
        db.commit()
    finally:
        db.close()

    body = test_client.get("/api/admin/index").json()

    assert body["ledger"]["rows"] == 1
    assert body["ledger"]["by_status"] == {"indexed": 1}
    assert body["ledger"]["expected_chunks"] == 3
    assert body["ledger"]["searchable"] == 1
    assert body["embedding"]["provider"]

    # The whole reason this route reports drift: an index built with a different model
    # returns nonsense rather than an error (deployment plan 2.2).
    assert body["drift"]["fingerprint_matches"] is False
    assert body["drift"]["chunker_matches"] is False


def test_index_overview_without_a_ledger(client):
    test_client, _ = client
    body = test_client.get("/api/admin/index").json()

    assert body["ledger"]["rows"] == 0
    # Nothing recorded is not the same claim as "recorded and wrong", so drift is
    # unknown rather than False.
    assert body["drift"]["fingerprint_matches"] is None
    assert body["vector_store"]["state"] in {"ok", "empty"}


def test_runs_lists_both_corpora_and_labels_ai_targets(client, db_factory):
    test_client, db_factory_ = client
    _seed_corpus(db_factory_)
    db = db_factory_()
    try:
        db.add(
            SyncStatus(
                job_id="j-circ",
                kind=None,  # predates the discriminator; reads as a circular run
                status="success",
                started_at=datetime(2026, 1, 1, 10, 0),
                completed_at=datetime(2026, 1, 1, 10, 5),
                processed_count=12,
            )
        )
        db.add(
            SyncStatus(
                job_id="j-laws",
                kind="laws",
                status="failed",
                started_at=datetime(2026, 1, 2, 10, 0),
                completed_at=datetime(2026, 1, 2, 10, 1),
                error="boom",
            )
        )
        db.add(
            AIGenerationJob(
                id="job-1",
                target_kind="circular",
                circular_id="c1",
                feature="summary",
                status="succeeded",
                created_at=datetime(2026, 1, 3),
            )
        )
        db.commit()
    finally:
        db.close()

    body = test_client.get("/api/admin/runs").json()

    kinds = {run["job_id"]: run["kind"] for run in body["sync_runs"]}
    assert kinds == {"j-circ": "circulars", "j-laws": "laws"}

    circular_run = next(r for r in body["sync_runs"] if r["job_id"] == "j-circ")
    assert circular_run["duration_seconds"] == 300.0
    assert circular_run["processed_count"] == 12

    assert len(body["ai_jobs"]) == 1
    assert body["ai_jobs"][0]["target_label"] == "BPRD Circular No. 01 of 2025"


def test_interrupted_runs_are_released_for_both_corpora(client, db_factory, monkeypatch):
    """A killed sync must not read as "running" for ever, whichever corpus it scraped.

    Laws runs were excluded from this recovery, so a killed `laws sync` left a row in
    flight permanently — three had accumulated in the local corpus. Nothing displayed
    them until run history existed, which is how it went unnoticed.
    """
    import sbpeye.main as main_module

    test_client, db_factory_ = client
    db = db_factory_()
    try:
        for kind in (None, "circulars", "laws"):
            db.add(
                SyncStatus(
                    job_id=f"j-{kind}",
                    kind=kind,
                    status="running",
                    started_at=datetime(2026, 1, 1, 10, 0),
                )
            )
        db.commit()
    finally:
        db.close()

    monkeypatch.setattr(main_module, "SessionLocal", db_factory_)
    main_module.fail_interrupted_sync_jobs()

    body = test_client.get("/api/admin/runs").json()
    assert {run["status"] for run in body["sync_runs"]} == {"failed"}
    # No end time was observed, so none is invented — otherwise a run killed two days ago
    # reports a 38-hour duration that nothing measured.
    assert all(run["completed_at"] is None for run in body["sync_runs"])
    assert all(run["duration_seconds"] is None for run in body["sync_runs"])


def test_runs_limit_is_bounded(client):
    test_client, _ = client
    assert test_client.get("/api/admin/runs?limit=0").status_code == 422
    assert test_client.get("/api/admin/runs?limit=500").status_code == 422
    assert test_client.get("/api/admin/runs?limit=5").status_code == 200


def test_environment_reports_paths_and_capabilities(client, monkeypatch, tmp_path):
    test_client, _ = client
    root = use_tmp_data_root(monkeypatch, tmp_path)
    (root / "files" / "cache").mkdir(parents=True)
    (root / "files" / "cache" / "page.html").write_text("cached")

    body = test_client.get("/api/admin/environment").json()

    trees = {tree["name"]: tree for tree in body["file_trees"]}
    assert trees["cache"]["deletable"] is True
    # The archive is the tree nothing may ever delete (invariant 3.7). If this flag ever
    # flips, a future "clean up disk" control would be pointed at irreplaceable files.
    assert trees["laws archive"]["deletable"] is False

    assert isinstance(body["capabilities"]["checklist_generation"], bool)
    assert body["embedding"]["fingerprint"].startswith("sha256:")


# ----------------------------------------------------------------------- read-only


def test_audit_does_not_write_the_ledger(client, db_factory):
    """The audit must report drift, never silently repair it.

    `reconcile` writes ledger rows when asked to. Calling it with `write=True` from a GET
    would mean opening the Index tab mutated the corpus — with no operator action behind
    it, and on a route any admin can refresh.
    """
    test_client, db_factory_ = client
    _seed_corpus(db_factory_)

    db = db_factory_()
    try:
        before = db.query(SemanticIndexSource).count()
    finally:
        db.close()

    assert test_client.get("/api/admin/index/audit").status_code == 200

    db = db_factory_()
    try:
        assert db.query(SemanticIndexSource).count() == before
    finally:
        db.close()


def test_audit_reports_coverage(client, db_factory):
    test_client, db_factory_ = client
    _seed_corpus(db_factory_)

    body = test_client.get("/api/admin/index/audit").json()

    # Nothing was ever embedded in this fixture, so every text-bearing source is stale
    # and the corpus is emphatically not completely searchable. That is the honest
    # answer, and the one the tab has to be able to show.
    assert body["is_complete"] is False
    assert body["status_counts"].get("stale", 0) > 0
    assert body["indexed_chunks"] == 0
    assert isinstance(body["duration_ms"], int)
