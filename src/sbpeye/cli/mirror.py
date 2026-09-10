"""Offline mirror maintenance. Bootstrap validates exclusivity before Chroma opens."""

from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import uuid

import click


@click.group()
def mirror():
    """Audit circular presence and backfill bounded gaps. Stop other writers first."""


def _bootstrap():
    from ..storage_preflight import require_exclusive_store
    root = Path(os.getenv("SBPEYE_DATA_DIR") or Path(__file__).resolve().parents[3])
    try:
        require_exclusive_store(root / "chroma_db")
        require_exclusive_store(root / "sbpeye.db")
        require_exclusive_store(Path(os.getenv("SBPEYE_APP_DB") or root / "sbpeye_app.db"))
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc
    from ..database import SessionLocal, Base, engine
    from .. import models
    from ..circular_jobs import recover_jobs
    Base.metadata.create_all(engine)
    with SessionLocal() as session:
        recover_jobs(session)
    return SessionLocal


def _emit(value):
    click.echo(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _readonly():
    from ..identity_migration import open_readonly
    root = Path(os.getenv("SBPEYE_DATA_DIR") or Path(__file__).resolve().parents[3])
    return open_readonly(root / "sbpeye.db")


def _record(row):
    from ..mirror_reads import JSON_COLUMNS
    if row is None:
        return None
    result = dict(row)
    for key in result.keys() & JSON_COLUMNS:
        result[key] = json.loads(result[key]) if result[key] else None
    return result


@mirror.command()
@click.option("--workers", type=click.IntRange(1, 8), default=4)
@click.option("--delay", type=click.FloatRange(0, 10), default=0.5)
@click.option("--json", "json_output", is_flag=True)
def audit(workers, delay, json_output):
    factory = _bootstrap()
    from ..circular_jobs import preflight, run_audit
    from ..mirror import now, dumps
    from ..mirror_models import MirrorAudit
    from ..mirror_reads import payload
    from ..mirror_types import AuditRequest
    request = AuditRequest(workers=workers, delay=delay)
    audit_id = str(uuid.uuid4())
    with factory() as db:
        try:
            preflight(db)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        db.add(MirrorAudit(id=audit_id, status="queued", started_at=now(), parameters=dumps(request.model_dump())))
        db.commit()
    with redirect_stdout(sys.stderr):
        run_audit(audit_id, request, factory)
    with factory() as db:
        result = payload(db.get(MirrorAudit, audit_id))
    _emit(result)
    if result["status"] != "success":
        raise click.exceptions.Exit(1)


@mirror.command()
@click.option("--json", "json_output", is_flag=True)
def status(json_output):
    from contextlib import closing
    from ..identity_migration import _tables
    with closing(_readonly()) as db:
        tables = _tables(db)
        if "mirror_audit" not in tables:
            _emit({"audit": None, "latest_complete_audit": None, "queue_counts": {}})
            return
        _emit({"audit": _record(db.execute("SELECT * FROM mirror_audit ORDER BY started_at DESC,id LIMIT 1").fetchone()),
               "latest_complete_audit": _record(db.execute("SELECT * FROM mirror_audit WHERE status='success' ORDER BY started_at DESC,id LIMIT 1").fetchone()),
               "queue_counts": dict(db.execute("SELECT status,count(*) FROM mirror_gap GROUP BY status").fetchall())})


@mirror.command()
@click.option("--status", type=click.Choice(["pending", "running", "resolved", "failed", "skipped"]))
@click.option("--year", type=int)
@click.option("--dept")
@click.option("--csv", "csv_path", type=click.Path(path_type=Path))
def gaps(status, year, dept, csv_path):
    from contextlib import closing
    from types import SimpleNamespace
    from ..identity_migration import _tables
    from ..mirror_reads import gaps_csv
    with closing(_readonly()) as db:
        clauses, values = [], []
        for field, value in (("status", status), ("year", year), ("department", dept)):
            if value is not None:
                clauses.append(f"{field} LIKE ?" if field == "department" else f"{field}=?")
                values.append(f"%{value}%" if field == "department" else value)
        rows = []
        if "mirror_gap" in _tables(db):
            sql = "SELECT * FROM mirror_gap" + (" WHERE " + " AND ".join(clauses) if clauses else "")
            rows = db.execute(sql + " ORDER BY sort_date IS NULL,sort_date DESC,id", values).fetchall()
        if csv_path:
            class Rows:
                def yield_per(self, count):
                    return (SimpleNamespace(**dict(row)) for row in rows)
            with csv_path.open("x", encoding="utf-8", newline="") as target:
                target.write(gaps_csv(Rows()))
        else:
            _emit([_record(row) for row in rows])


@mirror.command()
@click.option("--size", type=click.IntRange(1, 500), default=50)
@click.option("--year", "years", type=click.IntRange(1000, 9999), multiple=True)
@click.option("--dept", "departments", multiple=True)
@click.option("--status", "statuses", type=click.Choice(["pending", "failed"]), multiple=True)
@click.option("--order", type=click.Choice(["newest_first", "oldest_first", "fewest_attempts"]), default="newest_first")
@click.option("--attachments", "include_attachments", is_flag=True)
@click.option("--workers", type=click.IntRange(1, 8), default=2)
@click.option("--delay", type=click.FloatRange(0, 10), default=0.5)
@click.option("--max-attempts", type=click.IntRange(1, 20), default=3)
@click.option("--repeat-until-done", is_flag=True)
@click.option("--llm-feature", "llm_features", type=click.Choice(["summary", "tags", "checklist", "relationships", "entities", "consolidation"]), multiple=True)
def backfill(**options):
    import signal
    factory = _bootstrap()
    from ..mirror_types import BackfillRequest
    from ..circular_jobs import create_backfill, run_backfill, cancel_job
    from ..models import SyncStatus
    from ..mirror_reads import payload
    for field in ("years", "departments", "statuses", "llm_features"):
        options[field] = list(options[field])
    options["statuses"] = options["statuses"] or ["pending"]
    request = BackfillRequest(**options)
    with factory() as db:
        try:
            job_id, count = create_backfill(db, request)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
    if not job_id:
        _emit({"status": "empty", "selected_total": 0})
        return
    interrupted = False
    def cancel(signum, frame):
        nonlocal interrupted
        interrupted = True
        cancel_job(job_id)
    previous = signal.signal(signal.SIGINT, cancel)
    try:
        with redirect_stdout(sys.stderr):
            run_backfill(job_id, request, factory, lambda state: click.echo(json.dumps(state), err=True))
    finally:
        signal.signal(signal.SIGINT, previous)
    with factory() as db:
        result = payload(db.query(SyncStatus).filter_by(job_id=job_id).one())
    _emit(result)
    raise click.exceptions.Exit(130 if interrupted else (1 if result["status"] != "success" or result["error_count"] else 0))


def _transition(identity, action, reason=None):
    factory = _bootstrap()
    from ..mirror_models import MirrorGap
    from ..mirror_reads import payload
    from ..circular_jobs import preflight
    with factory() as db:
        try:
            preflight(db)
        except ValueError as exc:
            raise click.UsageError(str(exc)) from exc
        row = db.get(MirrorGap, identity)
        allowed = {"pending", "failed"} if action == "skip" else {"failed", "skipped"}
        if row is None or row.active_attempt_id or row.status not in allowed:
            raise click.UsageError("Gap is missing, claimed or in an incompatible state")
        if action == "skip":
            if not reason.strip():
                raise click.UsageError("A skip reason is required")
            row.status, row.skip_reason = "skipped", reason.strip()
        else:
            row.status, row.failures_since_requeue = "pending", 0
        db.commit()
        _emit(payload(row))


@mirror.command()
@click.option("--id", "identity", required=True)
def requeue(identity):
    _transition(identity, "requeue")


@mirror.command()
@click.option("--id", "identity", required=True)
@click.option("--reason", required=True)
def skip(identity, reason):
    _transition(identity, "skip", reason)


@mirror.command("repair-index")
@click.option("--job-id", required=True)
def repair_index(job_id):
    factory = _bootstrap()
    from ..mirror_repairs import repair_job_indexes
    result = repair_job_indexes(job_id, factory)
    _emit(result)
    if result["errors"]:
        raise click.exceptions.Exit(1)
