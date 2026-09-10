import json
from types import SimpleNamespace

from bs4 import BeautifulSoup
import pytest

from sbpeye.circular_identity import circular_identity
from sbpeye.mirror import reconcile, publish_audit, crawl_listing
from sbpeye.mirror_models import MirrorAudit, MirrorGap
from sbpeye.mirror_types import BackfillRequest
from sbpeye import circular_jobs


def descriptor(number=1, year=2017, **kw):
    return {"reference": f"BPRD Circular No. {number:02} of {year}", "title": f"Circular {number}",
            "url": f"https://www.sbp.org.pk/circulars/bprd-circular-no-{number}-of-{year}",
            "department": "BPRD", "year": str(year), "date": "May 08", **kw}


def test_reconcile_dedup_variants_drift_and_local_denominator():
    a, b, c = descriptor(), descriptor(2), descriptor(3)
    variant = {**a, "url": "https://www.sbp.org.pk/circulars/alternate"}
    local = [{"id": "drift", "reference": a["reference"], "url": variant["url"]},
             {"id": circular_identity(b["reference"], b["url"]), **b},
             {"id": "unlisted", "reference": "", "url": "https://www.sbp.org.pk/unlisted"}]
    report = reconcile([variant, a, b, c], local)
    assert report["raw_total"] == 4
    assert report["distinct_total"] == 3
    assert report["counts"] == dict(matched=1, drifted=1, missing=1, ambiguous=0, unlisted_local=1,
                                    identity_collision_groups=0, listing_duplicate_groups=1, duplicate_entries=1)
    drift = next(item for item in report["items"] if item["bucket"] == "drifted")
    assert drift["descriptor"]["url"] == a["url"]
    assert drift["matched_id"] == "drift"


def test_conflicting_url_identity_is_ambiguous():
    a = descriptor()
    local = [{"id": circular_identity(a["reference"], a["url"]), **a},
             {"id": "other", **a}]
    result = reconcile([a], local)
    assert result["counts"]["ambiguous"] == 1
    assert result["counts"]["unlisted_local"] == 0


@pytest.mark.parametrize("status", ["partial", "failed"])
def test_partial_publication_never_changes_queue(db_factory, status):
    with db_factory() as db:
        gap = MirrorGap(id="old", descriptor="{}", status="skipped", eligible=True, attempts=3, last_error="keep")
        db.add_all([gap, MirrorAudit(id="audit", status="running")])
        db.commit()
        publish_audit(db, "audit", reconcile([descriptor()], []), {"status": status, "pages_total": 1, "diagnostics": []})
        assert db.query(MirrorGap).count() == 1
        assert gap.status == "skipped" and gap.eligible and gap.attempts == 3 and gap.last_error == "keep"


def test_complete_audit_preserves_skip_and_attempts(db_factory):
    item = descriptor()
    identity = circular_identity(item["reference"], item["url"])
    with db_factory() as db:
        gap = MirrorGap(id=identity, descriptor="{}", status="skipped", eligible=True, attempts=3, last_error="keep")
        db.add_all([gap, MirrorAudit(id="audit", status="running")])
        db.commit()
        publish_audit(db, "audit", reconcile([item], []), {"status": "success", "pages_total": 1, "diagnostics": []})
        assert gap.status == "skipped" and gap.attempts == 3 and gap.last_error == "keep"
        assert gap.eligible


def test_missing_pagination_is_failed():
    capture = crawl_listing(SimpleNamespace(workers=1), lambda url: BeautifulSoup("<html>Challenge</html>", "html.parser"))
    assert capture["status"] == "failed"
    assert capture["error_code"] == "invalid_listing"


def test_request_strict_booleans_years():
    with pytest.raises(ValueError):
        BackfillRequest(include_attachments="false")
    with pytest.raises(ValueError):
        BackfillRequest(years=[99])


def test_admin_reads_and_empty_backfill(client):
    http, factory = client
    assert http.get("/api/admin/mirror").json()["audit"] is None
    assert http.get("/api/admin/mirror/gaps").json()["total"] == 0
    response = http.post("/api/circulars/mirror/backfill", json={})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "empty"
    assert not circular_jobs.CIRCULAR_JOB_LOCK.locked()
    assert http.get("/api/admin/mirror/jobs/unknown").status_code == 404
    assert http.post("/api/circulars/mirror/backfill", json={"include_attachments": "false"}).status_code == 422


def test_finite_selection_and_existing_no_fetch(db_factory, monkeypatch):
    from sbpeye.models import Circular, SyncStatus
    item = descriptor()
    identity = circular_identity(item["reference"], item["url"])
    request = BackfillRequest(repeat_until_done=True)
    with db_factory() as db:
        db.add(MirrorGap(id=identity, descriptor=json.dumps(item), variants=json.dumps([item]), status="pending", eligible=True))
        db.commit()
        job_id, total = circular_jobs.create_backfill(db, request)
        assert total == 1
        db.add(Circular(id=identity, **{key: value for key, value in item.items() if key in {"reference", "title", "url", "department"}}))
        db.commit()
    monkeypatch.setattr("sbpeye.scraper.circulars.process_circular", lambda *a, **kw: pytest.fail("must not fetch present row"))
    circular_jobs.run_backfill(job_id, request, db_factory)
    with db_factory() as db:
        gap = db.get(MirrorGap, identity)
        assert gap.status == "resolved" and gap.attempts == 0
        job = db.query(SyncStatus).filter_by(job_id=job_id).one()
        assert job.status == "success"
        assert json.loads(job.progress)["already_present"] == 1


def test_body_commit_then_failure_resolves_presence_and_records_stage(db_factory, monkeypatch):
    from sbpeye.models import Circular
    from sbpeye.mirror_models import MirrorAttempt
    item = descriptor()
    identity = circular_identity(item["reference"], item["url"])
    request = BackfillRequest()
    with db_factory() as db:
        db.add(MirrorGap(id=identity, descriptor=json.dumps(item), variants=json.dumps([item]), status="pending", eligible=True))
        db.commit()
        job_id, _ = circular_jobs.create_backfill(db, request)
    def process(db, **options):
        db.add(Circular(id=identity, title=item["title"], reference=item["reference"], url=item["url"], content_text="stored"))
        db.commit()
        options["outcome"].update(body="ready", fts="ready", vector="failed")
        raise RuntimeError("embedding unavailable")
    monkeypatch.setattr("sbpeye.scraper.circulars.process_circular", process)
    circular_jobs.run_backfill(job_id, request, db_factory)
    with db_factory() as db:
        gap = db.get(MirrorGap, identity)
        assert gap.status == "resolved" and gap.attempts == 1 and gap.failures_since_requeue == 0
        attempt = db.query(MirrorAttempt).one()
        assert attempt.error_stage == "vector"
        assert attempt.outcome == "resolved"


def test_cancellation_before_dispatch_leaves_unstarted_gaps_unchanged(db_factory, monkeypatch):
    from sbpeye.models import SyncStatus
    item = descriptor()
    identity = circular_identity(item["reference"], item["url"])
    request = BackfillRequest()
    with db_factory() as db:
        db.add(MirrorGap(id=identity, descriptor=json.dumps(item), variants=json.dumps([item]), status="pending", eligible=True))
        db.commit()
        job_id, _ = circular_jobs.create_backfill(db, request)
    circular_jobs.cancel_job(job_id)
    monkeypatch.setattr("sbpeye.scraper.circulars.process_circular", lambda *a, **kw: pytest.fail("cancelled"))
    circular_jobs.run_backfill(job_id, request, db_factory)
    with db_factory() as db:
        assert db.get(MirrorGap, identity).attempts == 0
        job = db.query(SyncStatus).filter_by(job_id=job_id).one()
        assert json.loads(job.progress)["terminal_reason"] == "cancelled"
        assert json.loads(job.progress)["remaining"] == 1


def test_mirror_reads_are_admin_only_and_do_not_change_rows(client):
    from conftest import sign_in, sign_out
    http, factory = client
    with factory() as db:
        db.add(MirrorGap(id="kept", descriptor="{}", status="resolved", resolution_id="absent", eligible=True))
        db.commit()
    http.get("/api/admin/mirror")
    http.get("/api/admin/mirror/gaps")
    http.get("/api/admin/mirror/gaps.csv")
    with factory() as db:
        row = db.get(MirrorGap, "kept")
        assert row.status == "resolved" and row.resolution_id == "absent" and row.eligible
    sign_out(http)
    sign_in(http, factory, user_id="tester-mirror", is_admin=False)
    assert http.get("/api/admin/mirror").status_code == 403
    assert http.post("/api/circulars/mirror/audit", json={}).status_code == 403
    assert http.post("/api/circulars/mirror/backfill", json={}).status_code == 403


def test_csv_escapes_formulas_and_filters(client):
    import csv
    import io
    http, factory = client
    with factory() as db:
        db.add_all([MirrorGap(id="one", descriptor=json.dumps({"title": '=SUM(1,2)\n"quoted"'}), status="pending"),
                    MirrorGap(id="two", descriptor=json.dumps({"title": "held"}), status="skipped")])
        db.commit()
    response = http.get("/api/admin/mirror/gaps.csv?status=pending")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert len(rows) == 1 and rows[0]["title"] == '\'=SUM(1,2)\n"quoted"'


def test_alias_lookup_preserves_historical_circular_urls(client):
    from sbpeye.models import Circular
    from sbpeye.mirror_models import IdentityAlias
    http, factory = client
    with factory() as db:
        db.add(Circular(id="new", title="Moved", content_text="Stored body"))
        db.add(IdentityAlias(kind="circular", old_id="old", new_id="new", migration_id="fixture", created_at="2026-09-10"))
        db.commit()
    response = http.get("/api/circulars/old")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == "new"


def listing_html(start, count, total=31):
    rows = ''.join(f'<div class="publication-box-new"><h4><a href="/circulars/bprd-{number}">Circular {number}</a></h4><p class="mb-3 date">BPRD Circular No. {number} of 2017</p></div>' for number in range(start, start + count))
    return BeautifulSoup(f'<div class="pagination-custom" data-total-pages="{(total + 29) // 30}"></div><input id="total_all_records" value="{total}">{rows}', 'html.parser')


def test_complete_crawl_retains_every_occurrence_and_rechecks_page_zero():
    calls = []
    def fetch(url):
        calls.append(url)
        return listing_html(31, 1) if url.endswith('P30') else listing_html(1, 30)
    capture = crawl_listing(SimpleNamespace(workers=2), fetch)
    assert capture["status"] == "success"
    assert len(capture["entries"]) == 31
    assert calls.count("https://www.sbp.org.pk/circulars/") == 2


def test_failed_page_is_partial_and_collapse_is_not_published():
    def failed(url):
        if url.endswith('P30'):
            raise OSError("TLS unavailable")
        return listing_html(1, 30)
    capture = crawl_listing(SimpleNamespace(workers=1), failed)
    assert capture["status"] == "partial" and len(capture["entries"]) == 30
    capture = crawl_listing(SimpleNamespace(workers=1), lambda url: listing_html(31, 1) if url.endswith('P30') else listing_html(1, 30), baseline={"raw_total": 100})
    assert capture["status"] == "failed" and capture["error_code"] == "listing_collapse"


def test_repeated_page_and_continuously_shifting_listing_are_rejected():
    capture = crawl_listing(SimpleNamespace(workers=1), lambda url: listing_html(1, 30, total=60))
    assert capture["status"] == "failed" and capture["error_code"] == "invalid_listing"
    calls = 0
    def shifting(url):
        nonlocal calls
        if url.endswith('P30'):
            return listing_html(31, 1)
        calls += 1
        return listing_html(calls, 30)
    capture = crawl_listing(SimpleNamespace(workers=1), shifting)
    assert capture["status"] == "partial" and capture["error_code"] == "listing_changed"
    assert calls == 4


def test_ordinary_discovery_failure_has_a_terminal_run(db_factory, monkeypatch):
    from sbpeye.models import SyncStatus
    from sbpeye.scraper import circulars
    def fail(**kwargs):
        raise OSError("listing transport failed")
    monkeypatch.setattr(circulars, "discover_circulars", fail)
    with db_factory() as db:
        with pytest.raises(OSError):
            circulars.scrape_circulars(db)
        job = db.query(SyncStatus).one()
        assert job.status == "failed" and "transport" in job.error


@pytest.mark.parametrize("operation", ["audit", "backfill"])
def test_legacy_identity_returns_conflict_without_starting_job(client, operation):
    from sbpeye.models import Circular, SyncStatus

    http, factory = client
    reference = "FD Circular No. 1/2016"
    url = "https://www.sbp.org.pk/2016/FD/C1.htm"
    legacy_id = circular_identity(reference, url, identity_version=1)
    assert legacy_id != circular_identity(reference, url)
    with factory() as db:
        db.add(Circular(id=legacy_id, reference=reference, title="Legacy circular", url=url))
        db.commit()

    response = http.post(f"/api/circulars/mirror/{operation}", json={})

    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "identity_preflight",
        "message": "Legacy slash-year identities remain; review and apply an identity migration first.",
    }
    assert not circular_jobs.CIRCULAR_JOB_LOCK.locked()
    with factory() as db:
        assert db.query(MirrorAudit).count() == 0
        assert db.query(SyncStatus).count() == 0
        assert db.get(Circular, legacy_id) is not None


def test_thread_start_failure_releases_lock_and_finalizes_audit(client, monkeypatch):
    from sbpeye import main
    http, factory = client
    def fail(self):
        raise RuntimeError("no worker thread")
    monkeypatch.setattr(main.threading.Thread, "start", fail)
    # Call the handler directly: TestClient itself needs worker threads.
    with factory() as db:
        with pytest.raises(RuntimeError, match="no worker"):
            main.start_mirror_audit(main.AuditRequest(), db)
        row = db.query(MirrorAudit).one()
        assert row.status == "failed"
    assert not circular_jobs.CIRCULAR_JOB_LOCK.locked()
