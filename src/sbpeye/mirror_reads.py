"""Read-only queries shared by admin routes and the CLI."""

import csv
import io
import json
from sqlalchemy import func

from .mirror_models import MirrorAudit, MirrorAuditItem, MirrorGap, MirrorAttempt

JSON_COLUMNS = {"parameters", "selection", "progress", "counts", "coverage", "diagnostics", "descriptor", "variants", "evidence", "stages"}


def payload(row):
    if row is None:
        return None
    data = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    for key in data.keys() & JSON_COLUMNS:
        data[key] = json.loads(data[key]) if data[key] else None
    return data


def overview(session):
    from .models import SyncStatus
    from .mirror import now
    latest = session.query(MirrorAudit).order_by(MirrorAudit.started_at.desc(), MirrorAudit.id).first()
    complete = session.query(MirrorAudit).filter_by(status="success").order_by(MirrorAudit.started_at.desc(), MirrorAudit.id).first()
    active = session.query(SyncStatus).filter(SyncStatus.kind == "circulars", SyncStatus.status.in_(["queued", "running"])).order_by(SyncStatus.id.desc()).first()
    return {"audit": payload(latest), "latest_complete_audit": payload(complete), "active_job": payload(active),
            "queue_counts": dict(session.query(MirrorGap.status, func.count()).group_by(MirrorGap.status).all()), "generated_at": now()}


def gaps_query(session, status=None, year=None, department=None, q=None, eligible=None):
    query = session.query(MirrorGap)
    if status:
        query = query.filter(MirrorGap.status == status)
    if year:
        query = query.filter(MirrorGap.year == year)
    if department:
        query = query.filter(MirrorGap.department.ilike(f"%{department}%"))
    if q:
        query = query.filter(MirrorGap.descriptor.ilike(f"%{q}%"))
    if eligible is not None:
        query = query.filter(MirrorGap.eligible == eligible)
    return query.order_by(MirrorGap.sort_date.is_(None), MirrorGap.sort_date.desc(), MirrorGap.id)


def paginate(query, page=1, per_page=50):
    if page < 1 or not 1 <= per_page <= 100:
        raise ValueError("Invalid pagination")
    return {"items": [payload(row) for row in query.offset((page - 1) * per_page).limit(per_page).all()],
            "total": query.count(), "page": page, "per_page": per_page}


def gaps_csv(query):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["id", "identity_version", "audit_id", "status", "reference", "title", "url", "year", "attempts", "last_error"])
    def safe(value):
        value = str(value or "")
        return "'" + value if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) else value
    for row in query.yield_per(100):
        descriptor = json.loads(row.descriptor)
        writer.writerow([safe(value) for value in (row.id, row.identity_version, row.last_audit_id, row.status,
                         descriptor.get("reference"), descriptor.get("title"), descriptor.get("url"), row.year, row.attempts, row.last_error)])
    return output.getvalue()
