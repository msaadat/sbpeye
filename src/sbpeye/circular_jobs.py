"""Single-process circular job coordination, durable claims, and crash recovery."""

from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
import threading
import time
import uuid

from sqlalchemy import or_

from .circular_identity import circular_identity
from .mirror import now, dumps, reconcile, crawl_listing, publish_audit
from .mirror_models import MirrorAudit, MirrorGap, MirrorAttempt
from .models import Circular, SyncStatus
from .scraper.http import RequestPacer, http_job

CIRCULAR_JOB_LOCK = threading.Lock()
_cancellations = {}
_state_lock = threading.Lock()


def preflight(session):
    from .identity_migration import identity_preflight
    identity_preflight(session.connection().connection.driver_connection)


def select_candidates(session, request):
    query = session.query(MirrorGap).filter(MirrorGap.eligible.is_(True), MirrorGap.status.in_(request.statuses),
                                          MirrorGap.failures_since_requeue < request.max_attempts,
                                          MirrorGap.active_attempt_id.is_(None))
    if request.years:
        query = query.filter(MirrorGap.year.in_(request.years))
    if request.departments:
        query = query.filter(or_(*(MirrorGap.department.ilike(f"%{value}%") | MirrorGap.descriptor.ilike(f"%{value}%") for value in request.departments)))
    if request.order == "fewest_attempts":
        query = query.order_by(MirrorGap.attempts)
    query = query.order_by(MirrorGap.sort_date.is_(None), MirrorGap.sort_date.asc() if request.order == "oldest_first" else MirrorGap.sort_date.desc(), MirrorGap.id)
    if not request.repeat_until_done:
        query = query.limit(request.size)
    return [{"id": row.id, "descriptor": json.loads(row.descriptor), "variants": json.loads(row.variants)} for row in query.all()]


def create_backfill(session, request):
    preflight(session)
    selection = select_candidates(session, request)
    if not selection:
        return None, 0
    job_id = str(uuid.uuid4())
    session.add(SyncStatus(job_id=job_id, kind="circulars", status="queued", started_at=now(),
                           parameters=dumps({**request.model_dump(), "operation": "mirror_backfill", "identity_version": 2}),
                           selection=dumps(selection), progress=dumps({"selected_total": len(selection), "remaining": len(selection)})))
    session.commit()
    with _state_lock:
        _cancellations[job_id] = threading.Event()
    return job_id, len(selection)


def _presence(session, descriptor, variants):
    report = reconcile(variants or [descriptor], [dict(id=row.id, reference=row.reference, url=row.url, new_url=row.new_url, old_url=row.old_url) for row in session.query(Circular).all()])
    finding = next(item for item in report["items"] if item["bucket"] != "unlisted_local")
    return finding["matched_id"], finding["bucket"] == "ambiguous"


def process_gap(session_factory, selected, job_id, request, pacer):
    from .scraper.circulars import process_circular
    from .identity_removal import requires_fresh_fetch
    attempt_id = str(uuid.uuid4())
    descriptor = selected["descriptor"]
    stages, error = {}, None
    with session_factory() as session:
        gap = session.get(MirrorGap, selected["id"])
        if not gap or gap.active_attempt_id or gap.status not in {"pending", "failed"} or not gap.eligible:
            return {"held": 1}
        present, ambiguous = _presence(session, descriptor, selected["variants"])
        if ambiguous:
            gap.eligible, gap.eligibility_reason = False, "ambiguous"
            session.commit()
            return {"held": 1}
        if present:
            gap.status, gap.resolution_id, gap.resolved_at = "resolved", present, now()
            session.add(MirrorAttempt(id=attempt_id, gap_id=gap.id, job_id=job_id, circular_identity=gap.id,
                                     origin="mirror_backfill", descriptor=dumps(selected), started_at=now(), completed_at=now(), outcome="already_present"))
            session.commit()
            return {"already_present": 1}
        changed = session.query(MirrorGap).filter(MirrorGap.id == gap.id, MirrorGap.active_attempt_id.is_(None), MirrorGap.status.in_(["pending", "failed"])).update(
            {MirrorGap.status: "running", MirrorGap.active_attempt_id: attempt_id,
             MirrorGap.attempts: MirrorGap.attempts + 1, MirrorGap.last_attempt_at: now()}, synchronize_session=False)
        if not changed:
            session.rollback()
            return {"held": 1}
        session.add(MirrorAttempt(id=attempt_id, gap_id=gap.id, job_id=job_id, circular_identity=gap.id, origin="mirror_backfill",
                                 descriptor=dumps(selected), started_at=now(), outcome="running"))
        session.commit()
    try:
        with session_factory() as session, http_job(pacer):
            force_fetch = requires_fresh_fetch(session, selected["id"])
            variants = [descriptor, *[item for item in selected["variants"] if item["url"] != descriptor["url"]]]
            for index, item in enumerate(variants):
                try:
                    process_circular(session, title=item["title"], url=item["url"], reference=item.get("reference", ""),
                                     department=item.get("department", "Unknown"), listing_date=item.get("date", ""), year=str(item.get("year", "")),
                                     include_attachments=request.include_attachments, outcome=stages, force_fetch=force_fetch)
                    stages["successful_url"] = item["url"]
                    break
                except Exception as exc:
                    session.rollback()
                    if getattr(getattr(exc, "response", None), "status_code", None) != 404 or index == len(variants) - 1:
                        raise
    except Exception as exc:
        error = exc
    with session_factory() as session:
        gap, attempt = session.get(MirrorGap, selected["id"]), session.get(MirrorAttempt, attempt_id)
        present, _ = _presence(session, descriptor, selected["variants"])
        gap.status, gap.resolution_id = ("resolved", present) if present else ("failed", None)
        gap.resolved_at, gap.active_attempt_id = now() if present else None, None
        if not present:
            gap.failures_since_requeue += 1
        failed_stage = next((key for key in ("body", "fts", "vector") if stages.get(key) != "ready"), None)
        gap.last_error_stage = failed_stage if error or failed_stage else None
        gap.last_error = str(error) if error else (f"{failed_stage} did not complete" if failed_stage else None)
        attempt.outcome, attempt.completed_at = gap.status, now()
        attempt.stages, attempt.error = dumps(stages), gap.last_error
        attempt.error_stage, attempt.error_type = gap.last_error_stage, type(error).__name__ if error else None
        session.commit()
        return {"attempted": 1, "resolved_new": int(bool(present)), "failed_missing": int(not present),
                "required_errors": int(bool(failed_stage)), "warning_items": int(bool(error) or any(value == "warning" for value in stages.values())),
                "circular_id": present}


def run_backfill(job_id, request, session_factory, progress=lambda value: None):
    with _state_lock:
        cancel = _cancellations.setdefault(job_id, threading.Event())
    pacer = RequestPacer(request.delay, cancel)
    started = time.monotonic()
    last_progress_write = 0.0
    counts = dict(attempted=0, resolved_new=0, already_present=0, failed_missing=0, required_errors=0, warning_items=0, held=0)
    terminal, failure = "exhausted", None
    selection = []
    try:
        with session_factory() as session:
            job = session.query(SyncStatus).filter_by(job_id=job_id).one()
            selection = json.loads(job.selection)
            job.status = "running"
            session.commit()
        for offset in range(0, len(selection), request.size):
            if cancel.is_set():
                terminal = "cancelled"
                break
            batch = selection[offset:offset + request.size]
            completed_ids, batch_attempted, batch_errors = [], 0, 0
            with ThreadPoolExecutor(max_workers=request.workers) as pool:
                todo, active = iter(batch), set()
                while True:
                    while not cancel.is_set() and len(active) < request.workers:
                        selected = next(todo, None)
                        if selected is None:
                            break
                        active.add(pool.submit(process_gap, session_factory, selected, job_id, request, pacer))
                    if not active:
                        break
                    done, active = wait(active, return_when=FIRST_COMPLETED)
                    for future in done:
                        result = future.result()
                        for key in counts:
                            counts[key] += result.get(key, 0)
                        batch_attempted += result.get("attempted", 0)
                        batch_errors += result.get("required_errors", 0)
                        if result.get("circular_id"):
                            completed_ids.append(result["circular_id"])
                    payload = {**counts, "selected_total": len(selection), "remaining": len(selection) - counts["attempted"] - counts["already_present"] - counts["held"],
                               "elapsed": time.monotonic() - started, "batch_number": offset // request.size + 1, "cancel_requested": cancel.is_set()}
                    if time.monotonic() - last_progress_write >= 1 or not active:
                        with session_factory() as session:
                            job = session.query(SyncStatus).filter_by(job_id=job_id).one()
                            job.progress = dumps(payload)
                            session.commit()
                        last_progress_write = time.monotonic()
                        progress(payload)
            if request.llm_features and completed_ids:
                from .circular_ai import run_sync_generation
                generated = run_sync_generation(completed_ids, request.llm_features)
                counts["warning_items"] += generated["errors"]
            if batch_attempted and batch_errors / batch_attempted > 0.5:
                terminal = "failure_threshold"
                break
        if cancel.is_set():
            terminal = "cancelled"
    except Exception as exc:
        terminal, failure = "fatal_failure", str(exc)
    finally:
        with session_factory() as session:
            job = session.query(SyncStatus).filter_by(job_id=job_id).one()
            payload = {**json.loads(job.progress or "{}"), **counts, "terminal_reason": terminal, "cancel_requested": cancel.is_set(),
                       "selected_total": len(selection), "remaining": len(selection) - counts["attempted"] - counts["already_present"] - counts["held"], "elapsed": time.monotonic() - started}
            job.progress, job.status = dumps(payload), "success" if terminal == "exhausted" else "failed"
            job.error, job.completed_at = failure, now()
            job.processed_count, job.skipped_count, job.error_count = counts["resolved_new"], counts["already_present"], counts["required_errors"]
            session.commit()
        with _state_lock:
            _cancellations.pop(job_id, None)


def cancel_job(job_id):
    with _state_lock:
        cancel = _cancellations.get(job_id)
        if cancel:
            cancel.set()
        return cancel is not None


def run_audit(audit_id, request, session_factory):
    from .scraper.circulars import fetch_page
    pacer = RequestPacer(request.delay)
    last_write = 0.0
    def progress(value):
        nonlocal last_write
        if time.monotonic() - last_write < 1:
            return
        with session_factory() as session:
            row = session.get(MirrorAudit, audit_id)
            row.pages_total, row.pages_completed = value["pages_total"], value["pages_completed"]
            session.commit()
        last_write = time.monotonic()
    def fetch(url):
        with http_job(pacer):
            return fetch_page(url)
    with session_factory() as session:
        audit = session.get(MirrorAudit, audit_id)
        audit.status = "running"
        baseline = session.query(MirrorAudit).filter_by(status="success").order_by(MirrorAudit.started_at.desc()).first()
        baseline = {"raw_total": baseline.raw_total} if baseline else None
        session.commit()
    try:
        capture = crawl_listing(request, fetch, progress=progress, baseline=baseline)
        with session_factory() as session:
            rows = [dict(id=row.id, reference=row.reference, title=row.title, url=row.url, new_url=row.new_url, old_url=row.old_url, department=row.department, date=row.date) for row in session.query(Circular).all()]
        report = reconcile(capture["entries"], rows)
        report["local_observed_at"] = now()
        with session_factory() as session:
            publish_audit(session, audit_id, report, capture)
    except Exception as exc:
        with session_factory() as session:
            audit = session.get(MirrorAudit, audit_id)
            audit.status, audit.error, audit.completed_at = "failed", str(exc), now()
            session.commit()


def recover_jobs(session):
    """Startup/explicit CLI only. Presence wins even when the outcome commit was lost."""
    for attempt in session.query(MirrorAttempt).filter_by(outcome="running").all():
        gap = session.get(MirrorGap, attempt.gap_id) if attempt.gap_id else None
        stored = json.loads(attempt.descriptor)
        descriptor = stored.get("descriptor", stored)
        present, ambiguous = _presence(session, descriptor, stored.get("variants", []))
        if gap is None and not present and not ambiguous and attempt.origin == "ordinary_sync":
            gap = session.get(MirrorGap, attempt.circular_identity)
            if gap is None:
                gap = MirrorGap(id=attempt.circular_identity, descriptor=dumps(descriptor), variants=dumps([descriptor]),
                                status="failed", eligible=True, eligibility_source="ordinary_sync", eligibility_reason="interrupted_missing",
                                first_seen_at=now(), attempts=1, failures_since_requeue=0)
                session.add(gap)
            attempt.gap_id = gap.id
        attempt.outcome, attempt.error = "interrupted", "Process interrupted; unfinished stages require inspection"
        if gap:
            gap.status, gap.resolution_id = ("resolved", present) if present else ("failed", None)
            gap.active_attempt_id = None
            if not present:
                gap.failures_since_requeue += 1
    for audit in session.query(MirrorAudit).filter(MirrorAudit.status.in_(["queued", "running"])):
        audit.status, audit.error_code = "failed", "interrupted"
    for job in session.query(SyncStatus).filter(SyncStatus.status.in_(["queued", "running"])):
        if json.loads(job.parameters or "{}").get("operation") == "mirror_backfill":
            job.status, job.error = "failed", "Interrupted; start a new job to handle remaining items"
            job.progress = dumps({**json.loads(job.progress or "{}"), "terminal_reason": "interrupted"})
    session.commit()


def ordinary_attempt_start(session, descriptor, run_id):
    identity = circular_identity(descriptor.get("reference"), descriptor["url"])
    attempt = MirrorAttempt(id=str(uuid.uuid4()), job_id=run_id, circular_identity=identity,
                            origin="ordinary_sync", descriptor=dumps(descriptor), started_at=now(), outcome="running")
    gap = session.get(MirrorGap, identity)
    if gap and not gap.active_attempt_id:
        attempt.gap_id = gap.id
        gap.active_attempt_id = attempt.id
        gap.attempts += 1
        gap.last_attempt_at = now()
    session.add(attempt)
    session.commit()
    return attempt.id


def ordinary_attempt_finish(session, attempt_id, stages, error):
    attempt = session.get(MirrorAttempt, attempt_id)
    descriptor = json.loads(attempt.descriptor)
    present, ambiguous = _presence(session, descriptor, [])
    gap = session.get(MirrorGap, attempt.circular_identity)
    if not present and not ambiguous and gap is None:
        gap = MirrorGap(id=attempt.circular_identity, descriptor=dumps(descriptor), variants=dumps([descriptor]),
                        first_seen_at=now(), status="failed", eligible=True, eligibility_source="ordinary_sync",
                        eligibility_reason="missing", eligibility_checked_at=now(), attempts=1, failures_since_requeue=0)
        session.add(gap)
        attempt.gap_id = gap.id
    if gap:
        skipped = gap.status == "skipped"
        if present:
            gap.status, gap.resolution_id, gap.resolved_at = "resolved", present, now()
        elif not skipped:
            gap.status = "failed"
        if not present:
            gap.failures_since_requeue += 1
        gap.last_seen_at, gap.active_attempt_id = now(), None
        gap.last_error = str(error) if error else None
        gap.last_error_stage = next((key for key in ("body", "fts", "vector") if stages.get(key) != "ready"), None)
    attempt.outcome, attempt.completed_at = "resolved" if present else "failed", now()
    attempt.stages, attempt.error = dumps(stages), str(error) if error else None
    attempt.error_type = type(error).__name__ if error else None
    session.commit()
