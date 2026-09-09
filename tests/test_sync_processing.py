"""Sync indexes annexures and runs only explicitly selected AI features, offline."""

from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from sbpeye import circular_ai, main
from sbpeye.models import (
    AIGenerationJob, Attachment, Circular, CircularConsolidation,
    CircularRelationship, SyncStatus,
)
from sbpeye.scraper import circulars as scraper


def _stored_circular(db, circular_id="synced"):
    circular = Circular(
        id=circular_id, title="Reporting rules", reference="BPRD Circular No. 1 of 2026",
        department="BPRD", date=datetime(2026, 1, 1),
        url="https://www.sbp.org.pk/circulars/reporting", content_text="Banks must report.",
    )
    db.add(circular)
    db.commit()
    return circular


def _stub_scrape(monkeypatch):
    monkeypatch.setattr(scraper, "fetch_page_cached", lambda *a, **kw: b"<p>Banks must report.</p>")
    monkeypatch.setattr(scraper, "extract_sbp_text", lambda raw: "Banks must report.")
    monkeypatch.setattr(scraper, "detect_attachments", lambda *a: [{"url": "https://www.sbp.org.pk/rules.pdf"}])
    monkeypatch.setattr(scraper, "_link_circular_to_laws", lambda *a, **kw: None)
    monkeypatch.setattr(scraper, "embedding_backend", SimpleNamespace(
        embed_documents=lambda chunks: [[1.0, 0.0] for _ in chunks],
    ))

    def attachment(db, circular, info, **kwargs):
        row = db.get(Attachment, "annexure")
        if row is None:
            row = Attachment(
                id="annexure", circular_id=circular.id, filename="rules.pdf",
                original_url=info["url"], file_type="pdf",
                content_text="Annexure liquidity reporting obligations.",
                extraction_status="extracted", is_vectorized=0,
            )
            db.add(row)
            db.commit()
        return row

    monkeypatch.setattr(scraper, "process_attachment", attachment)


def test_sync_indexes_attachment_vectors_and_keyword_text_by_default(
    db_factory, monkeypatch, isolated_vector_store,
):
    _stub_scrape(monkeypatch)
    with db_factory() as db:
        circular = scraper.process_circular(
            db, "Reporting rules", "https://www.sbp.org.pk/circulars/reporting",
            reference="BPRD Circular No. 1 of 2026", listing_date="January 01 2026",
        )
        assert db.get(Attachment, "annexure").is_vectorized == 1
        assert any(key.startswith("annexure__chunk_") for key in isolated_vector_store.records)
        hits = db.execute(text(
            "SELECT circular_id FROM circulars_fts WHERE circulars_fts MATCH 'liquidity'"
        )).scalars().all()
        assert hits == [circular.id]
        # Already indexed annexures aren't re-embedded on the next processing pass.
        monkeypatch.setattr(scraper, "vectorize_attachment", lambda *a, **kw: pytest.fail("re-embedded"))
        scraper.process_circular(
            db, "Reporting rules", circular.url, reference=circular.reference,
            listing_date="January 01 2026",
        )


def test_explicit_full_pipeline_index_opt_out_remains_supported(db_factory, monkeypatch, isolated_vector_store):
    _stub_scrape(monkeypatch)
    with db_factory() as db:
        scraper.process_circular(
            db, "Reporting rules", "https://www.sbp.org.pk/circulars/reporting",
            listing_date="January 01 2026", index_attachments=False,
        )
        assert db.get(Attachment, "annexure").is_vectorized == 0
        assert not any(key.startswith("annexure__chunk_") for key in isolated_vector_store.records)


def test_attachment_index_failure_is_reported_without_losing_body(db_factory, monkeypatch):
    _stub_scrape(monkeypatch)
    monkeypatch.setattr(scraper, "vectorize_attachment", lambda *a, **kw: False)
    with db_factory() as db:
        with pytest.raises(RuntimeError, match="Circular saved"):
            scraper.process_circular(
                db, "Reporting rules", "https://www.sbp.org.pk/circulars/reporting",
                listing_date="January 01 2026",
            )
        assert db.query(Circular).one().content_text == "Banks must report."
        assert db.execute(text("SELECT count(*) FROM circulars_fts")).scalar() == 1


@pytest.mark.parametrize("features", ["summary", ["all"], ["unknown"], [False], None])
def test_sync_rejects_invalid_analysis_selections(features):
    with pytest.raises(ValueError, match="LLM features"):
        main._sync_options_from_payload({"llm_features": features})


def test_sync_selection_is_explicit_and_canonical():
    assert main._sync_options_from_payload({})["llm_features"] == []
    assert main._sync_options_from_payload({"llm_features": ["tags"]})["skip_llm"] is False
    assert main._sync_options_from_payload({
        "llm_features": ["consolidation", "tags", "summary", "summary"],
    })["llm_features"] == ["summary", "tags", "consolidation"]


def test_selected_generation_uses_durable_jobs_and_preserves_existing(db_factory, monkeypatch):
    monkeypatch.setattr(circular_ai, "SessionLocal", db_factory)
    calls = []
    fake_client = SimpleNamespace(
        summarize=lambda *a: calls.append("summary") or "New summary",
        generate_tags=lambda *a: calls.append("tags") or ["reporting"],
    )
    monkeypatch.setattr(circular_ai, "get_ai_client", lambda db: fake_client)
    with db_factory() as db:
        circular = _stored_circular(db)
        circular.summary = "Existing summary"
        db.commit()
    result = circular_ai.run_sync_generation(["synced", "synced"], ["summary", "tags"])
    assert (result["completed"], result["skipped"], result["errors"]) == (1, 1, 0)
    assert calls == ["tags"]
    with db_factory() as db:
        assert db.get(Circular, "synced").summary == "Existing summary"
        assert db.get(Circular, "synced").tags == '["reporting"]'
        job = db.query(AIGenerationJob).one()
        assert (job.feature, job.status) == ("tags", "succeeded")


def test_generation_failure_keeps_circular_and_continues_selected_features(db_factory, monkeypatch):
    monkeypatch.setattr(circular_ai, "SessionLocal", db_factory)
    monkeypatch.setattr(circular_ai, "get_ai_client", lambda db: SimpleNamespace(
        summarize=lambda *a: "", generate_tags=lambda *a: ["reporting"],
    ))
    with db_factory() as db:
        _stored_circular(db)
    result = circular_ai.run_sync_generation(["synced"], ["summary", "tags"])
    assert result["completed"] == result["errors"] == 1
    assert "empty summary" in result["error_details"][0]
    with db_factory() as db:
        assert db.get(Circular, "synced").content_text == "Banks must report."
        assert {job.feature: job.status for job in db.query(AIGenerationJob)} == {
            "summary": "failed", "tags": "succeeded",
        }


def test_relationships_finish_before_consolidation_and_shared_chain_runs_once(db_factory, monkeypatch):
    monkeypatch.setattr(circular_ai, "SessionLocal", db_factory)
    with db_factory() as db:
        _stored_circular(db, "a")
        _stored_circular(db, "b")
    calls = []

    def generate(job_id):
        with db_factory() as db:
            job = db.get(AIGenerationJob, job_id)
            calls.append((job.circular_id, job.feature))
            if job.feature == "relationships":
                db.get(Circular, job.circular_id).relationships_generated_at = datetime(2026, 1, 1)
                if job.circular_id == "b":
                    db.add(CircularRelationship(source_id="b", target_id="a", type="amends"))
            else:
                assert db.query(CircularRelationship).count() == 1
                db.add(CircularConsolidation(
                    chain_id="a", as_of_circular_id="b", member_ids=json.dumps(["a", "b"]),
                    requirements="[]", stale=0,
                ))
            job.status = "succeeded"
            db.commit()

    monkeypatch.setattr(circular_ai, "run_generation_job", generate)
    result = circular_ai.run_sync_generation(["a", "b"], ["consolidation", "relationships"])
    assert calls == [("a", "relationships"), ("b", "relationships"), ("a", "consolidation")]
    assert (result["completed"], result["skipped"], result["errors"]) == (3, 1, 0)


def test_scraper_returns_only_successfully_processed_ids(db_factory, monkeypatch):
    from sbpeye import database

    monkeypatch.setattr(database, "SessionLocal", db_factory)
    descriptors = [
        {"title": value, "url": f"https://www.sbp.org.pk/circulars/{value}", "department": "BPRD"}
        for value in ("held", "new", "failed")
    ]
    monkeypatch.setattr(scraper, "discover_circulars", lambda **kw: descriptors)
    with db_factory() as db:
        existing = _stored_circular(db, scraper.circular_identity(None, descriptors[0]["url"]))
        existing.url = descriptors[0]["url"]
        db.commit()

        def process(worker_db, **options):
            assert options["index_attachments"] is True
            if options["title"] == "failed":
                raise RuntimeError("Fetch failed")
            assert options["title"] == "new"
            return SimpleNamespace(id="new-id")

        monkeypatch.setattr(scraper, "process_circular", process)
        result = scraper.scrape_circulars(db, workers=1)
    assert result["circular_ids"] == ["new-id"]
    assert (result["processed"], result["skipped"], result["errors"]) == (1, 1, 1)


@pytest.mark.parametrize("features", [[], ["summary"]])
def test_sync_worker_generates_only_for_this_runs_processed_ids(db_factory, monkeypatch, features):
    monkeypatch.setattr(main, "SessionLocal", db_factory)
    calls = []

    def scrape(db, **options):
        assert "llm_features" not in options
        return {"processed": 1, "skipped": 4, "errors": 0, "circular_ids": ["new"]}

    def generate(ids, selected):
        calls.append((ids, selected))
        return {"completed": 0, "skipped": 0, "errors": 1, "error_details": ["Model unavailable"]}

    monkeypatch.setattr(main, "scrape_circulars", scrape)
    monkeypatch.setattr(main, "run_sync_generation", generate)
    with db_factory() as db:
        db.add(SyncStatus(job_id="sync-selected", status="queued"))
        db.commit()
    assert main._CIRCULAR_SYNC_LOCK.acquire(blocking=False)
    main._run_circular_sync("sync-selected", main._sync_options_from_payload({"llm_features": features}))
    assert calls == ([(["new"], ["summary"])] if features else [])
    with db_factory() as db:
        job = db.query(SyncStatus).one()
        payload = main._sync_status_payload(job)
        assert payload["running"] is False
        assert job.processed_count == 1
        assert job.error_count == (1 if features else 0)
        assert (payload["generation"] is not None) == bool(features)
    assert not main._CIRCULAR_SYNC_LOCK.locked()
