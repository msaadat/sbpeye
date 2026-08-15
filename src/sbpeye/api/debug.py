"""HTTP API for the gated, read-only LLM trace inspector."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..llm_debug import debug_allowed, debug_setting_enabled
from ..models import LLMTrace, LLMTraceEvent

router = APIRouter(prefix="/api/debug", tags=["llm-debug"])


def _parsed(value: str, fallback: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def _iso(value):
    return value.isoformat() if value else None


def _summary(row: LLMTrace) -> dict[str, Any]:
    return {
        "id": row.id, "schema_version": row.schema_version,
        "operation": row.operation, "origin": row.origin, "status": row.status,
        "provider": row.provider, "model": row.model, "job_id": row.job_id,
        "chat_session_id": row.chat_session_id, "target_kind": row.target_kind,
        "target_id": row.target_id, "command_name": row.command_name,
        "metadata": _parsed(row.metadata_json, {}), "attempt_count": row.attempt_count,
        "prompt_tokens": row.prompt_tokens, "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens, "payload_bytes": row.payload_bytes,
        "error_type": row.error_type, "error_message": row.error_message,
        "started_at": _iso(row.started_at), "updated_at": _iso(row.updated_at),
        "completed_at": _iso(row.completed_at), "duration_ms": row.duration_ms,
    }


def require_debug_enabled(db: Session = Depends(get_db)) -> None:
    if not debug_allowed() or not debug_setting_enabled(db):
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/status")
def status(db: Session = Depends(get_db)):
    allowed = debug_allowed()
    enabled = debug_setting_enabled(db)
    effective = allowed and enabled
    payload: dict[str, Any] = {
        "allowed": allowed, "enabled": enabled, "effective": effective,
    }
    if effective:
        count, size = db.query(
            func.count(LLMTrace.id), func.coalesce(func.sum(LLMTrace.payload_bytes), 0)
        ).one()
        payload.update(trace_count=int(count), payload_bytes=int(size))
    return payload


@router.get("/traces", dependencies=[Depends(require_debug_enabled)])
def list_traces(
    status: str | None = None,
    operation: str | None = None,
    origin: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    correlation: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(LLMTrace)
    for column, value in (
        (LLMTrace.status, status), (LLMTrace.operation, operation),
        (LLMTrace.origin, origin), (LLMTrace.provider, provider),
        (LLMTrace.model, model),
    ):
        if value:
            query = query.filter(column == value)
    if correlation:
        query = query.filter(or_(
            LLMTrace.id == correlation, LLMTrace.job_id == correlation,
            LLMTrace.chat_session_id == correlation, LLMTrace.target_id == correlation,
        ))
    total = query.count()
    rows = query.order_by(LLMTrace.started_at.desc(), LLMTrace.id.desc()).offset(
        (page - 1) * per_page
    ).limit(per_page).all()

    def facet(column) -> list[str]:
        return [str(value) for (value,) in db.query(column).filter(
            column.isnot(None), column != ""
        ).distinct().order_by(column).all()]

    latest_event_id = db.query(func.max(LLMTraceEvent.id)).scalar() or 0
    return {
        "items": [_summary(row) for row in rows], "total": total,
        "page": page, "per_page": per_page,
        "total_pages": max(1, (total + per_page - 1) // per_page),
        "latest_event_id": int(latest_event_id),
        "facets": {
            "statuses": facet(LLMTrace.status), "operations": facet(LLMTrace.operation),
            "origins": facet(LLMTrace.origin), "providers": facet(LLMTrace.provider),
            "models": facet(LLMTrace.model),
        },
    }


@router.get("/traces/{trace_id}", dependencies=[Depends(require_debug_enabled)])
def trace_detail(
    trace_id: str,
    after_sequence: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    trace = db.query(LLMTrace).filter(LLMTrace.id == trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    events = db.query(LLMTraceEvent).filter(
        LLMTraceEvent.trace_id == trace_id,
        LLMTraceEvent.sequence > after_sequence,
    ).order_by(LLMTraceEvent.sequence.asc()).all()
    last_sequence = db.query(func.max(LLMTraceEvent.sequence)).filter(
        LLMTraceEvent.trace_id == trace_id
    ).scalar() or 0
    return {
        "trace": _summary(trace),
        "events": [{
            "id": event.id, "trace_id": event.trace_id, "sequence": event.sequence,
            "kind": event.kind, "stage": event.stage, "attempt_id": event.attempt_id,
            "attempt_number": event.attempt_number,
            "payload": _parsed(event.payload_json, {"parse_error": True}),
            "payload_bytes": event.payload_bytes, "created_at": _iso(event.created_at),
            "elapsed_ms": event.elapsed_ms,
        } for event in events],
        "last_sequence": int(last_sequence), "status": trace.status,
    }


@router.delete("/traces/{trace_id}", dependencies=[Depends(require_debug_enabled)])
def delete_trace(trace_id: str, db: Session = Depends(get_db)):
    trace = db.query(LLMTrace).filter(LLMTrace.id == trace_id).first()
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found")
    if trace.status == "running":
        raise HTTPException(status_code=409, detail="A running trace cannot be deleted")
    db.query(LLMTraceEvent).filter(LLMTraceEvent.trace_id == trace_id).delete(
        synchronize_session=False
    )
    db.delete(trace)
    db.commit()
    return {"deleted": 1}


@router.delete("/traces", dependencies=[Depends(require_debug_enabled)])
def delete_all_traces(db: Session = Depends(get_db)):
    running_ids = [row[0] for row in db.query(LLMTrace.id).filter(
        LLMTrace.status == "running"
    ).all()]
    completed_ids = [row[0] for row in db.query(LLMTrace.id).filter(
        LLMTrace.status != "running"
    ).all()]
    if completed_ids:
        db.query(LLMTraceEvent).filter(LLMTraceEvent.trace_id.in_(completed_ids)).delete(
            synchronize_session=False
        )
        db.query(LLMTrace).filter(LLMTrace.id.in_(completed_ids)).delete(
            synchronize_session=False
        )
    db.commit()
    return {"deleted": len(completed_ids), "skipped_running": len(running_ids)}

