from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from urllib.parse import urljoin, urlparse, urlencode
from pathlib import Path
from contextlib import asynccontextmanager
import cloudscraper
from bs4 import BeautifulSoup
import re as _re
import os
import json
import uuid

from .database import PROJECT_ROOT, engine, Base, get_db, SessionLocal, has_vector_store_data
from .models import AIGenerationJob, Attachment, CachedDocument, SyncStatus, Circular, CircularEntity, CircularRelationship, EcoDataSeries, EcoDataEntry, Settings, ChatSession, ChatMessage, ResearchWorkspace, WorkspaceCircular
from .search import resolve_metric_terms, search_engine
from .ai import AIClient, AIConfig, classify_provider_state, friendly_chat_error, get_ai_client, get_provider_api_key, get_provider_definition, normalize_provider
from .circular_ai import GENERATION_ACTIONS, generation_job_payload, run_generation_job
from .checklist_export import build_checklist_workbook
from .embeddings import EmbeddingConfig, create_embedding_backend
from .env import managed_env_path, set_managed_env_value, unset_managed_env_value
from .link_routing import (
    DOCUMENT_EXTENSIONS,
    attachment_info as _attachment_info,
    is_allowed_sbp_url as _is_allowed_sbp_url,
    normalize_sbp_url as _normalize_sbp_url,
    rewrite_document_links as _rewrite_document_links,
)

from .scraper.circulars import (
    HEADERS,
    attachment_id,
    download_attachment,
    fetch_page_cached,
    process_attachment,
    process_circular,
)
from .scraper.ecodata import scrape_ecodata
from .scraper.clean_html import clean_sbp_html, extract_sbp_text
from .scraper.ecodata_index import scrape_ecodata_index
from .scraper.pdf_summarizer import summarize_pdf, is_summarizable
from datetime import datetime, timedelta


from .api.serializers import (
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_NAME,
    WORKSPACE_CHAT_SESSION_PREFIX,
    _chat_session_payload,
    _circular_summary,
    _document_payload,
    _ensure_default_workspace,
    _format_timestamp,
    _get_workspace_for_chat_session,
    _isoformat,
    _normalize_circular_ids,
    _parse_year,
    _safe_json_list,
    _safe_json_object,
    _save_ai_secret,
    _save_embedding_secret,
    _settings_payload,
    _sorted_workspace_pinned_links,
    _summary_preview,
    _workspace_chat_session_id,
    _workspace_chat_session_payload,
    _workspace_circular_ids,
    _workspace_circular_summaries,
    _workspace_id_from_chat_session,
    _workspace_payload,
    _workspace_search_state,
)
from .scraper.news import scrape_sbp_news


def _lazy_index_circular(circular_id: str) -> None:
    db = SessionLocal()
    try:
        circular = db.query(Circular).filter(Circular.id == circular_id).first()
        if not circular:
            return
        process_circular(
            db,
            title=circular.title,
            url=circular.url,
            department=circular.department or "Discovered from link",
            reference=circular.reference or "",
            include_attachments=True,
        )
    finally:
        db.close()



def fail_interrupted_ai_jobs() -> None:
    """Release jobs left active when the previous server process stopped."""
    db = SessionLocal()
    try:
        interrupted = db.query(AIGenerationJob).filter(
            AIGenerationJob.status.in_(("queued", "running"))
        ).all()
        for job in interrupted:
            job.status = "failed"
            job.error = "Generation was interrupted by a server restart."
            job.completed_at = datetime.utcnow()
        if interrupted:
            db.commit()
    finally:
        db.close()


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    fail_interrupted_ai_jobs()
    yield


# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="SBPEye",
    description="Independent SBP Circulars & EcoData Indexer",
    lifespan=app_lifespan,
)

# Setup SPA static files
os.makedirs("src/sbpeye/static", exist_ok=True)

SPA_DIR = Path("src/sbpeye/static/spa")
SPA_INDEX = SPA_DIR / "index.html"
SPA_ASSETS_DIR = SPA_DIR / "assets"

app.mount("/static", StaticFiles(directory="src/sbpeye/static"), name="static")
if SPA_ASSETS_DIR.exists():
    app.mount("/spa/assets", StaticFiles(directory=SPA_ASSETS_DIR), name="spa-assets")


def spa_index_response() -> FileResponse:
    return FileResponse(SPA_INDEX)


@app.get("/")
async def read_root():
    return spa_index_response()


@app.get("/circulars")
async def circulars_spa():
    return spa_index_response()


@app.get("/circulars/{path:path}")
async def circulars_spa_fallback(path: str):
    return spa_index_response()


@app.get("/documents/{path:path}")
async def documents_spa_fallback(path: str):
    return spa_index_response()



ECODATA_CACHE_TTL_HOURS = 1

@app.get("/ecodata")
async def ecodata_page():
    return spa_index_response()


@app.get("/api/app/status")
async def get_app_status(db: Session = Depends(get_db)):
    sync_status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    last_sync_dt = sync_status.last_sync_date if sync_status else None
    total_circulars = db.query(func.count(Circular.id)).scalar() or 0
    department_count = db.query(func.count(func.distinct(Circular.department))).filter(Circular.department.isnot(None)).scalar() or 0
    indexed_today = db.query(func.count(Circular.id)).filter(func.date(Circular.date) == datetime.now().date()).scalar() or 0
    vector_db_ready = has_vector_store_data()

    return {
        "sync_status": sync_status.status if sync_status and sync_status.status else "idle",
        "live_status": (sync_status.status if sync_status and sync_status.status else "idle").upper(),
        "total_circulars": total_circulars,
        "department_count": department_count,
        "indexed_today": indexed_today,
        "vector_db_state": "READY" if vector_db_ready else "NOT_INDEXED",
        "last_sync_display": _format_timestamp(last_sync_dt),
        "last_sync": _format_timestamp(last_sync_dt),
        "last_sync_dt": last_sync_dt.isoformat() if isinstance(last_sync_dt, datetime) else None,
        "last_sync_raw": last_sync_dt.isoformat() if isinstance(last_sync_dt, datetime) else None,
    }


@app.get("/api/llm/status")
async def get_llm_status(db: Session = Depends(get_db)):
    """Probe the configured LLM backend's availability.

    Checked on demand (e.g. on page refresh) rather than on a schedule, since a
    local or free-tier backend can go offline or get rate-limited at any time.
    """
    try:
        client = get_ai_client(db)
    except Exception as exc:
        return {
            "available": False,
            "state": "error",
            "detail": "AI backend is not configured",
            "provider": None,
            "model": None,
            "error": str(exc),
        }
    return client.check_availability()


def _get_ecodata_entries(db: Session, force_refresh: bool = False) -> list[dict]:
    sync_status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    ecodata_time = sync_status.ecodata_index_time if sync_status else None

    needs_refresh = force_refresh or ecodata_time is None
    if not needs_refresh and ecodata_time:
        age = datetime.now() - ecodata_time
        if age > timedelta(hours=ECODATA_CACHE_TTL_HOURS):
            needs_refresh = True

    if needs_refresh:
        scrape_ecodata_index(db)
        if sync_status:
            sync_status.ecodata_index_time = datetime.now()
            db.commit()
        else:
            new_status = SyncStatus(
                last_sync_date=datetime.now(),
                status="success",
                ecodata_index_time=datetime.now()
            )
            db.add(new_status)
            db.commit()

    entries = db.query(EcoDataEntry).order_by(EcoDataEntry.sort_order).all()
    return [
        {
            "id": e.id,
            "section": e.section,
            "subsection": e.subsection,
            "description": e.description,
            "url": e.url,
            "frequency": e.frequency,
            "format_url": e.format_url,
            "format_type": e.format_type,
            "last_update": e.last_update,
            "archive_url": e.archive_url,
            "archive_updated": e.archive_updated,
            "is_quick_link": e.is_quick_link,
            "can_summarize": is_summarizable(e.url) if e.url else False,
        }
        for e in entries
    ]


@app.get("/api/ecodata/entries")
async def get_ecodata_entries(db: Session = Depends(get_db)):
    return _get_ecodata_entries(db)


@app.get("/api/ecodata/pdf_summary")
async def get_pdf_summary(url: str, db: Session = Depends(get_db)):
    if not is_summarizable(url):
        return {"error": "This document is not configured for summarization."}
    try:
        summary = summarize_pdf(url, db)
        return {"summary": summary, "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}




@app.get("/api/circulars/tags")
async def get_tags(db: Session = Depends(get_db)):
    from sqlalchemy import distinct
    rows = db.query(Circular.tags).filter(Circular.tags != None, Circular.tags != "").all()
    tag_counts = {}
    for row in rows:
        for t in _safe_json_list(row[0]):
            tag_counts[t] = tag_counts.get(t, 0) + 1
    sorted_tags = sorted(tag_counts.items(), key=lambda x: -x[1])
    return [{"tag": t, "count": c} for t, c in sorted_tags]


@app.get("/api/ecodata")
async def get_ecodata(series: str = "KIBOR_6M", db: Session = Depends(get_db)):
    # Retrieve ecodata for charts
    data = db.query(EcoDataSeries).filter(EcoDataSeries.name == series).order_by(EcoDataSeries.date.asc()).all()
    return [{"date": d.date.strftime("%Y-%m-%d"), "value": d.value} for d in data]

@app.get("/api/circulars/search")
def search_circulars(
    q: str = "",
    start_year: str | None = None,
    end_year: str | None = None,
    department: str | None = None,
    sort_by: str = "relevance",
    tag: str | None = None,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db)
):
    page = max(page, 1)
    per_page = min(max(per_page, 1), 100)
    offset = (page - 1) * per_page
    results, total = search_engine.search(
        q, db,
        offset=offset,
        limit=per_page,
        start_year=_parse_year(start_year),
        end_year=_parse_year(end_year),
        department=department,
        sort_by=sort_by,
        tag=tag,
    )
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "items": [
            _circular_summary(
                r["circular"],
                r.get("snippet"),
                r.get("match_source", "circular"),
                r.get("attachment_id"),
                r.get("attachment_filename"),
                r.get("source_ref"),
                r.get("source_page"),
            )
            for r in results
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
    }


@app.get("/api/circulars/departments")
async def get_departments(db: Session = Depends(get_db)):
    from sqlalchemy import func
    results = db.query(
        Circular.department,
        func.count(Circular.id).label("count")
    ).group_by(Circular.department).order_by(Circular.department.asc()).all()
    return [{"department": r.department, "count": r.count} for r in results]


@app.get("/api/circulars/years")
async def get_years(department: str, db: Session = Depends(get_db)):
    from sqlalchemy import func, extract
    results = db.query(
        extract("year", Circular.date).label("year"),
        func.count(Circular.id).label("count")
    ).filter(
        Circular.department == department
    ).group_by(
        extract("year", Circular.date)
    ).order_by(
        extract("year", Circular.date).desc()
    ).all()
    return [{"year": int(r.year), "count": r.count} for r in results]


@app.get("/api/circulars/browse")
async def browse_circulars(department: str, year: int, db: Session = Depends(get_db)):
    from sqlalchemy import extract
    circulars = db.query(Circular).filter(
        Circular.department == department,
        extract("year", Circular.date) == year
    ).order_by(Circular.date.desc()).all()
    return circulars


@app.get("/api/circulars/browse_recent")
async def browse_recent_circulars(limit: int = 100, db: Session = Depends(get_db)):
    circulars = db.query(Circular).order_by(Circular.date.desc()).limit(limit).all()
    return [
        _circular_summary(c)
        for c in circulars
    ]


@app.get("/api/circulars/by_url")
async def get_circular_by_url(url: str, db: Session = Depends(get_db)):
    c = db.query(Circular).filter(Circular.url == url).first()
    if not c:
        normalized = url.rstrip("/")
        c = db.query(Circular).filter(Circular.url == normalized).first()
    if not c:
        c = db.query(Circular).filter(func.lower(Circular.url) == url.lower()).first()
    if not c:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    return _circular_summary(c)


@app.post("/api/circulars/open")
async def open_circular_by_url(
    url: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    try:
        url = _normalize_sbp_url(url)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    circular = db.query(Circular).filter(func.lower(Circular.url) == url.lower()).first()
    if circular:
        return _circular_summary(circular)

    if Path(urlparse(url).path).suffix.lower() in DOCUMENT_EXTENSIONS:
        return JSONResponse({"error": "Document URLs must be opened through the document route."}, status_code=400)

    try:
        raw_html = fetch_page_cached(url)
        soup = BeautifulSoup(raw_html, "html.parser")
        content_text = extract_sbp_text(raw_html)
        if not content_text:
            raise ValueError("The SBP page did not contain readable content.")
        heading = soup.find(["h1", "h2"])
        title_tag = soup.find("title")
        title = (
            heading.get_text(" ", strip=True) if heading else ""
        ) or (
            title_tag.get_text(" ", strip=True) if title_tag else ""
        ) or Path(urlparse(url).path).name or "SBP circular"
        circular = Circular(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
            title=_re.sub(r"\s+", " ", title)[:500],
            department="Discovered from link",
            date=datetime.now(),
            url=url,
            content_text=content_text,
            status="active",
        )
        db.add(circular)
        db.commit()
        db.refresh(circular)
    except Exception as exc:
        db.rollback()
        return JSONResponse({"error": str(exc), "original_url": url}, status_code=502)

    background_tasks.add_task(_lazy_index_circular, circular.id)
    return _circular_summary(circular)


@app.post("/api/circulars/{circular_id}/refresh")
async def refresh_circular(
    circular_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    try:
        raw_html = fetch_page_cached(circular.url, force=True)
        if circular.url.lower().split("?", 1)[0].endswith(".pdf"):
            if not raw_html.startswith(b"%PDF"):
                raise ValueError("The refreshed SBP source is not a PDF.")
            return _circular_summary(circular)
        soup = BeautifulSoup(raw_html, "html.parser")
        content_text = extract_sbp_text(raw_html)
        if not content_text:
            raise ValueError("The refreshed SBP page did not contain readable content.")
        if content_text != circular.content_text:
            circular.content_text = content_text
            circular.compliance_checklist = None
            circular.checklist_generated_at = None
        db.commit()
    except Exception as exc:
        db.rollback()
        return JSONResponse(
            {"error": str(exc), "original_url": circular.url}, status_code=502
        )
    background_tasks.add_task(_lazy_index_circular, circular.id)
    return _circular_summary(circular)


@app.get("/api/circulars/{circular_id}/source")
async def get_circular_source(circular_id: str, db: Session = Depends(get_db)):
    c = db.query(Circular).filter(Circular.id == circular_id).first()
    if not c:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    if not _is_allowed_sbp_url(c.url):
        return JSONResponse({"error": "Only SBP (sbp.org.pk) circulars are supported."}, status_code=400)

    if c.url.lower().split("?", 1)[0].endswith(".pdf"):
        try:
            fetch_page_cached(c.url)
        except Exception as exc:
            return JSONResponse({"error": str(exc), "type": "pdf", "url": c.url}, status_code=502)
        return {
            "type": "pdf",
            "url": f"/api/circulars/{c.id}/document",
            "original_url": c.url,
            "content": None,
        }

    try:
        raw_html = fetch_page_cached(c.url)
        return {
            "type": "html",
            "url": c.url,
            "content": _rewrite_document_links(
                clean_sbp_html(raw_html, base_url=c.url), c, db
            ),
        }
    except Exception as e:
        return JSONResponse({"error": str(e), "type": "html", "url": c.url, "content": ""}, status_code=502)


@app.get("/api/circulars/{circular_id}/document")
async def circular_document(circular_id: str, db: Session = Depends(get_db)):
    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular or not circular.url.lower().split("?", 1)[0].endswith(".pdf"):
        return JSONResponse({"error": "Circular PDF not found."}, status_code=404)
    try:
        content = fetch_page_cached(circular.url)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    if not content.startswith(b"%PDF"):
        return JSONResponse({"error": "The cached source is not a PDF."}, status_code=502)
    return StreamingResponse(
        iter([content]),
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="circular.pdf"', "Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/circulars/{circular_id}")
async def get_circular_detail(circular_id: str, db: Session = Depends(get_db)):
    c = db.query(Circular).filter(Circular.id == circular_id).first()
    if not c:
        return JSONResponse({"error": "Circular not found"}, status_code=404)

    outgoing = db.query(CircularRelationship).filter(
        CircularRelationship.source_id == circular_id
    ).all()
    incoming = db.query(CircularRelationship).filter(
        CircularRelationship.target_id == circular_id
    ).all()

    def rel_dict(r):
        source = None
        target = None
        if r.source_id:
            sc = db.query(Circular).filter(Circular.id == r.source_id).first()
            if sc:
                source = {"id": sc.id, "title": sc.title, "reference": sc.reference, "url": sc.url, "status": sc.status or "active"}
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active"}
        return {
            "type": r.type,
            "source_id": r.source_id,
            "target_id": r.target_id,
            "target_reference": r.target_reference,
            "confidence": r.confidence,
            "source": source,
            "target": target,
        }

    return {
        "id": c.id,
        "title": c.title,
        "department": c.department,
        "reference": c.reference,
        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
        "url": c.url,
        "new_url": c.new_url or c.url,
        "old_url": c.old_url,
        "summary": c.summary,
        "tags": _safe_json_list(c.tags),
        "compliance_checklist": _safe_json_object(c.compliance_checklist),
        "entities": [_entity_dict(e) for e in c.entities],
        "status": c.status or "active",
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "original_url": attachment.original_url,
                "file_type": attachment.file_type,
                "extraction_status": attachment.extraction_status,
                "is_scanned": attachment.extraction_status == "scanned",
                "is_vectorized": bool(attachment.is_vectorized),
                "has_text": bool(attachment.content_text),
                "local_url": f"/documents/open?{urlencode({'id': attachment.id})}",
            }
            for attachment in sorted(c.attachments, key=lambda item: item.filename)
        ],
        "attachment_count": len(c.attachments),
        "relationships": {
            "outgoing": [rel_dict(r) for r in outgoing],
            "incoming": [rel_dict(r) for r in incoming],
        },
        "generation": {
            "summary": _isoformat(c.summary_generated_at),
            "tags": _isoformat(c.tags_generated_at),
            "checklist": _isoformat(c.checklist_generated_at),
            "relationships": _isoformat(c.relationships_generated_at),
            "entities": _isoformat(c.entities_generated_at),
        },
    }


@app.get("/api/circulars/{circular_id}/checklist.xlsx")
async def export_circular_checklist(circular_id: str, db: Session = Depends(get_db)):
    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular:
        return JSONResponse({"error": "Circular not found"}, status_code=404)

    checklist = _safe_json_object(circular.compliance_checklist)
    if not checklist:
        return JSONResponse({"error": "This circular does not have a generated checklist"}, status_code=404)

    safe_reference = _re.sub(r"[^A-Za-z0-9._-]+", "_", circular.reference or circular.id).strip("._")
    filename = f"{safe_reference or 'circular'}_checklist.xlsx"
    return StreamingResponse(
        build_checklist_workbook(circular, checklist),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _entity_dict(e: CircularEntity, *, include_circular: bool = False) -> dict:
    payload = {
        "id": e.id,
        "circular_id": e.circular_id,
        "entity_type": e.entity_type,
        "metric": e.metric,
        "comparator": e.comparator,
        "value_numeric": e.value_numeric,
        "value_high": e.value_high,
        "unit": e.unit,
        "value_text": e.value_text,
        "subject": e.subject,
        "effective_date": e.effective_date.strftime("%Y-%m-%d") if e.effective_date else None,
        "context_snippet": e.context_snippet,
        "page_start": e.page_start,
        "confidence": e.confidence,
    }
    if include_circular and e.circular is not None:
        c = e.circular
        payload["circular"] = {
            "id": c.id,
            "reference": c.reference,
            "title": c.title,
            "department": c.department,
            "date": c.date.strftime("%Y-%m-%d") if c.date else None,
            "status": c.status or "active",
        }
    return payload


@app.get("/api/circulars/entities/query")
async def query_circular_entities(
    metric: str | None = None,
    entity_type: str | None = None,
    unit: str | None = None,
    comparator: str | None = None,
    subject: str | None = None,
    department: str | None = None,
    min_value: float | None = None,
    max_value: float | None = None,
    current_only: bool = False,
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
):
    """Structured query over extracted regulatory values. Examples:
    ?unit=%&comparator=min&min_value=10 -> thresholds above 10%;
    ?metric=Paid-up Capital&subject=MFB&current_only=true -> the current MFB minimum capital."""
    query = db.query(CircularEntity).join(Circular, CircularEntity.circular_id == Circular.id)
    if metric:
        distinct_metrics = [m[0] for m in db.query(CircularEntity.metric).distinct() if m[0]]
        matched = resolve_metric_terms(metric, distinct_metrics)
        if matched:
            query = query.filter(CircularEntity.metric.in_(matched))
        else:
            query = query.filter(CircularEntity.metric.ilike(f"%{metric}%"))
    if entity_type:
        query = query.filter(CircularEntity.entity_type == entity_type)
    if unit:
        query = query.filter(CircularEntity.unit == unit)
    if comparator:
        query = query.filter(CircularEntity.comparator == comparator)
    if subject:
        query = query.filter(CircularEntity.subject.ilike(f"%{subject}%"))
    if department:
        query = query.filter(Circular.department.ilike(f"%{department}%"))
    if min_value is not None:
        query = query.filter(CircularEntity.value_numeric >= min_value)
    if max_value is not None:
        query = query.filter(CircularEntity.value_numeric <= max_value)
    if current_only:
        query = query.filter(~Circular.status.in_(("superseded", "cancelled")))

    # Most recent first, by the value's effective date then the circular's date.
    rows = query.order_by(
        CircularEntity.effective_date.desc().nullslast(),
        Circular.date.desc().nullslast(),
    ).all()

    if current_only:
        # Keep only the latest value per (metric, subject) group.
        seen: set[tuple] = set()
        deduped = []
        for entity in rows:
            key = ((entity.metric or "").lower(), (entity.subject or "").lower())
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entity)
        rows = deduped

    total = len(rows)
    per_page = max(1, min(per_page, 200))
    page = max(1, page)
    start = (page - 1) * per_page
    window = rows[start:start + per_page]
    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "results": [_entity_dict(e, include_circular=True) for e in window],
    }


@app.post("/api/circulars/{circular_id}/generate")
async def generate_circular_intelligence(
    circular_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "A JSON request body is required."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)

    feature = str(data.get("feature", "")).lower().strip()
    if feature not in GENERATION_ACTIONS:
        return JSONResponse(
            {"error": f"Feature must be one of: {', '.join(GENERATION_ACTIONS)}."},
            status_code=400,
        )

    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    has_pdf_text = any(
        (attachment.file_type or "").lower() == "pdf"
        and attachment.extraction_status == "extracted"
        and bool(attachment.content_text)
        for attachment in circular.attachments
    )
    if not circular.content_text and not has_pdf_text:
        return JSONResponse(
            {"error": "This circular has no extracted content to analyze."},
            status_code=422,
        )

    active_job = db.query(AIGenerationJob).filter(
        AIGenerationJob.circular_id == circular_id,
        AIGenerationJob.status.in_(("queued", "running")),
    ).order_by(AIGenerationJob.created_at.desc()).first()
    if active_job:
        return JSONResponse(
            {"error": "Generation is already in progress for this circular.", "job": generation_job_payload(active_job)},
            status_code=409,
        )

    job = AIGenerationJob(
        id=str(uuid.uuid4()),
        circular_id=circular_id,
        feature=feature,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_generation_job, job.id)
    return JSONResponse(generation_job_payload(job), status_code=202)


@app.get("/api/ai/jobs/{job_id}")
async def get_ai_generation_job(job_id: str, db: Session = Depends(get_db)):
    job = db.query(AIGenerationJob).filter(AIGenerationJob.id == job_id).first()
    if not job:
        return JSONResponse({"error": "Generation job not found"}, status_code=404)
    return generation_job_payload(job)


@app.get("/api/circulars/{circular_id}/relationships")
async def get_circular_relationships(circular_id: str, db: Session = Depends(get_db)):
    outgoing = db.query(CircularRelationship).filter(
        CircularRelationship.source_id == circular_id
    ).all()
    incoming = db.query(CircularRelationship).filter(
        CircularRelationship.target_id == circular_id
    ).all()

    def rel_dict(r):
        source = None
        target = None
        if r.source_id:
            sc = db.query(Circular).filter(Circular.id == r.source_id).first()
            if sc:
                source = {"id": sc.id, "title": sc.title, "reference": sc.reference, "url": sc.url, "status": sc.status or "active"}
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active"}
        return {
            "type": r.type,
            "source_id": r.source_id,
            "target_id": r.target_id,
            "target_reference": r.target_reference,
            "confidence": r.confidence,
            "source": source,
            "target": target,
        }

    return {
        "outgoing": [rel_dict(r) for r in outgoing],
        "incoming": [rel_dict(r) for r in incoming],
    }


@app.get("/api/sbp_news")
async def get_sbp_news(db: Session = Depends(get_db)):
    try:
        return scrape_sbp_news(db)
    except Exception as e:
        return {"press_releases": [], "whats_new": [], "error": str(e)}


@app.post("/api/documents/resolve")
async def resolve_document(
    id: str | None = None,
    url: str | None = None,
    circular_id: str | None = None,
    refresh: bool = False,
    db: Session = Depends(get_db),
):
    attachment = db.query(Attachment).filter(Attachment.id == id).first() if id else None
    standalone = db.query(CachedDocument).filter(CachedDocument.id == id).first() if id and not attachment else None
    if standalone:
        info = {
            "url": standalone.original_url,
            "filename": standalone.filename,
            "file_type": standalone.file_type,
        }
        local_path = PROJECT_ROOT / standalone.local_path if standalone.local_path else None
        if refresh or not local_path or not local_path.is_file():
            path, _, error, _ = download_attachment("standalone", info, force=refresh)
            standalone.local_path = str(path.relative_to(PROJECT_ROOT)) if path else None
            standalone.error = error
            db.commit()
        payload = _document_payload(standalone)
        if not payload["cached"]:
            return JSONResponse(payload, status_code=502)
        return payload

    normalized_url = None
    if not attachment and url:
        try:
            normalized_url = _normalize_sbp_url(url)
            info = _attachment_info(normalized_url)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        query = db.query(Attachment).filter(func.lower(Attachment.original_url) == normalized_url.lower())
        if circular_id:
            query = query.filter(Attachment.circular_id == circular_id)
        attachment = query.first()
    else:
        info = None

    if attachment:
        circular = attachment.circular
        info = {
            "url": attachment.original_url,
            "filename": attachment.filename,
            "file_type": attachment.file_type,
        }
    else:
        circular = db.query(Circular).filter(Circular.id == circular_id).first() if circular_id else None

    if not attachment and not circular:
        cached_document = db.query(CachedDocument).filter(
            func.lower(CachedDocument.original_url) == normalized_url.lower()
        ).first()
        if not cached_document:
            cached_document = CachedDocument(
                id=attachment_id("standalone", normalized_url),
                filename=info["filename"],
                original_url=normalized_url,
                file_type=info["file_type"],
            )
            db.add(cached_document)
            db.commit()
        local_path = PROJECT_ROOT / cached_document.local_path if cached_document.local_path else None
        if refresh or not local_path or not local_path.is_file():
            path, _, error, _ = download_attachment("standalone", info, force=refresh)
            cached_document.local_path = str(path.relative_to(PROJECT_ROOT)) if path else None
            cached_document.error = error
            db.commit()
        payload = _document_payload(cached_document)
        if not payload["cached"]:
            return JSONResponse(payload, status_code=502)
        return payload

    local_path = PROJECT_ROOT / attachment.local_path if attachment and attachment.local_path else None
    if refresh or not attachment or not local_path or not local_path.is_file():
        attachment = process_attachment(db, circular, info, force_download=refresh)

    payload = _document_payload(attachment)
    if not payload["cached"]:
        return JSONResponse(payload, status_code=502)
    return payload


@app.get("/api/documents/{attachment_id}/content")
async def document_content(attachment_id: str, db: Session = Depends(get_db)):
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        attachment = db.query(CachedDocument).filter(CachedDocument.id == attachment_id).first()
    if not attachment or not attachment.local_path:
        return JSONResponse({"error": "Cached document not found."}, status_code=404)
    path = (PROJECT_ROOT / attachment.local_path).resolve()
    attachments_root = (PROJECT_ROOT / "attachments").resolve()
    if attachments_root not in path.parents or not path.is_file():
        return JSONResponse({"error": "Cached document not found."}, status_code=404)
    disposition = "inline" if attachment.file_type == "pdf" else "attachment"
    media_type = "application/pdf" if attachment.file_type == "pdf" else "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=Path(attachment.filename).name,
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/pdf_preview")
async def pdf_preview(url: str):
    import io
    import base64
    import pdfplumber

    if not _is_allowed_sbp_url(url):
        return {"error": "Only SBP PDFs are supported."}

    try:
        resp = cloudscraper.create_scraper().get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        pdf = pdfplumber.open(io.BytesIO(resp.content))
        page_count = len(pdf.pages)

        if page_count == 1:
            text = pdf.pages[0].extract_text() or ""
            pdf.close()
            return {"type": "text", "content": text.strip(), "pages": page_count}

        img = pdf.pages[0].to_image(resolution=150)
        pil_img = img.original
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        pdf.close()
        return {"type": "image", "content": b64, "pages": page_count}

    except Exception as e:
        return {"error": str(e)}


@app.get("/api/pdf_proxy")
async def pdf_proxy(url: str):
    if not _is_allowed_sbp_url(url):
        return JSONResponse({"error": "Only SBP PDFs are supported."}, status_code=400)

    if not urlparse(url).path.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files are supported."}, status_code=400)

    try:
        resp = cloudscraper.create_scraper().get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)

    content_type = resp.headers.get("content-type", "")
    if "pdf" not in content_type.lower() and not resp.content.startswith(b"%PDF"):
        return JSONResponse({"error": "The source did not return a PDF document."}, status_code=502)

    return StreamingResponse(
        iter([resp.content]),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'inline; filename="sbp-document.pdf"',
            "Cache-Control": "private, max-age=300",
        },
    )




import csv
import io
import zipfile
import time

# --- Settings Page ---

@app.get("/settings")
async def settings_page():
    return spa_index_response()


@app.get("/api/settings")
async def get_settings(db: Session = Depends(get_db)):
    config = AIConfig.from_db(db) or AIConfig.from_env()
    embedding = EmbeddingConfig.from_db(db)
    return _settings_payload(config, embedding)


@app.post("/api/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    provider = normalize_provider(data.get("provider", data.get("ai_provider", "lmstudio")))
    provider_definition = get_provider_definition(provider)
    config = AIConfig(
        provider=provider,
        base_url=(
            data.get("base_url")
            or data.get("ai_base_url")
            or provider_definition.default_base_url
        ),
        api_key="",
        model=data.get("model", data.get("ai_model", provider_definition.default_model)),
        chat_model=data.get("chat_model", data.get("ai_chat_model", "")),
        max_context_tokens=int(data.get("max_context_tokens", data.get("ai_max_context_tokens", 4000))),
    )
    _save_ai_secret(
        provider=provider,
        api_key=data.get("api_key", data.get("ai_api_key")),
        clear_secret=bool(data.get("clear_api_key")),
    )
    config.api_key, _ = get_provider_api_key(provider)
    detected_context_window = AIClient(config).detect_context_window()
    if detected_context_window is not None:
        config.max_context_tokens = detected_context_window
    config.save_to_db(db)
    embedding = EmbeddingConfig(
        provider=data.get("embedding_provider", "fastembed"),
        model=data.get("embedding_model", "BAAI/bge-base-en-v1.5"),
        base_url=data.get("embedding_base_url", "http://localhost:1234/v1"),
        api_key="",
    )
    embedding.save_to_db(db)
    _save_embedding_secret(
        api_key=data.get("embedding_api_key"),
        clear_secret=bool(data.get("clear_embedding_api_key")),
    )
    config = AIConfig.from_db(db) or AIConfig.from_env()
    embedding = EmbeddingConfig.from_db(db)
    context_message = (
        f" Provider context window detected: {detected_context_window:,} tokens."
        if detected_context_window is not None
        else " Provider context metadata was unavailable; the configured token limit was retained."
    )
    return {
        "message": (
            "Settings saved. LLM provider changes apply immediately."
            f"{context_message} Run sbpeye reindex after changing the embedding provider or model."
        ),
        "settings": _settings_payload(config, embedding),
        "context_window_detected": detected_context_window is not None,
    }


@app.post("/api/settings/models")
async def list_settings_models(request: Request):
    data = await request.json()
    provider = normalize_provider(data.get("provider", data.get("ai_provider", "lmstudio")))
    provider_definition = get_provider_definition(provider)
    api_key = (data.get("api_key") or data.get("ai_api_key") or "").strip()
    if not api_key and not data.get("clear_api_key"):
        api_key, _ = get_provider_api_key(provider)
    config = AIConfig(
        provider=provider,
        base_url=(
            data.get("base_url")
            or data.get("ai_base_url")
            or provider_definition.default_base_url
        ),
        api_key=api_key,
        model=data.get("model", data.get("ai_model", provider_definition.default_model)),
        chat_model=data.get("chat_model", data.get("ai_chat_model", "")),
    )
    try:
        return {
            "success": True,
            "provider": provider,
            "models": AIClient(config).list_models(),
        }
    except Exception as exc:
        state, detail = classify_provider_state(exc)
        return {
            "success": False,
            "provider": provider,
            "state": state,
            "error": detail,
            "models": [],
        }


@app.post("/api/settings/embeddings/test")
def test_embedding_connection(db: Session = Depends(get_db)):
    try:
        config = EmbeddingConfig.from_db(db)
        backend = create_embedding_backend(config)
        embedding = backend.embed_queries(["SBP monetary policy"])
        return {"success": True, "dimensions": len(embedding[0]), "provider": config.provider}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/settings/test")
async def test_ai_connection(db: Session = Depends(get_db)):
    try:
        client = get_ai_client(db)
        result = client.test_connection()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Research Workspaces ---

@app.get("/api/workspaces")
async def list_research_workspaces(db: Session = Depends(get_db)):
    _ensure_default_workspace(db)
    workspaces = db.query(ResearchWorkspace).order_by(
        ResearchWorkspace.is_default.desc(),
        ResearchWorkspace.created_at.asc(),
        ResearchWorkspace.id.asc(),
    ).all()
    return [_workspace_payload(workspace, include_circulars=False) for workspace in workspaces]


@app.get("/api/workspaces/default")
async def get_default_research_workspace(db: Session = Depends(get_db)):
    workspace = _ensure_default_workspace(db)
    return _workspace_payload(workspace)


@app.post("/api/workspaces")
async def create_research_workspace(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "Workspace name cannot be empty"}, status_code=400)

    search_state = data.get("search_state", {})
    if search_state is None:
        search_state = {}
    if not isinstance(search_state, dict):
        return JSONResponse({"error": "search_state must be an object"}, status_code=400)

    last_circular_id = data.get("last_circular_id")
    if last_circular_id is not None and not isinstance(last_circular_id, str):
        return JSONResponse({"error": "last_circular_id must be a string"}, status_code=400)

    workspace = ResearchWorkspace(
        id=str(uuid.uuid4()),
        name=name.strip()[:120],
        is_default=0,
        search_state=json.dumps(search_state),
        last_circular_id=last_circular_id or None,
    )
    db.add(workspace)
    db.commit()
    db.refresh(workspace)
    return _workspace_payload(workspace)


@app.get("/api/workspaces/{workspace_id}")
async def get_research_workspace(workspace_id: str, db: Session = Depends(get_db)):
    if workspace_id == DEFAULT_WORKSPACE_ID:
        return _workspace_payload(_ensure_default_workspace(db))

    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    return _workspace_payload(workspace)


@app.patch("/api/workspaces/{workspace_id}")
async def update_research_workspace(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    if workspace_id == DEFAULT_WORKSPACE_ID:
        _ensure_default_workspace(db)

    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    data = await request.json()
    if "name" in data:
        if workspace.is_default:
            return JSONResponse({"error": "Default workspace cannot be renamed"}, status_code=400)
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "Workspace name cannot be empty"}, status_code=400)
        workspace.name = name.strip()[:120]

    if "search_state" in data:
        search_state = data.get("search_state") or {}
        if not isinstance(search_state, dict):
            return JSONResponse({"error": "search_state must be an object"}, status_code=400)
        workspace.search_state = json.dumps(search_state)

    if "last_circular_id" in data:
        last_circular_id = data.get("last_circular_id")
        if last_circular_id is not None and not isinstance(last_circular_id, str):
            return JSONResponse({"error": "last_circular_id must be a string"}, status_code=400)
        workspace.last_circular_id = last_circular_id or None

    workspace.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(workspace)
    return _workspace_payload(workspace)


@app.delete("/api/workspaces/{workspace_id}")
async def delete_research_workspace(workspace_id: str, db: Session = Depends(get_db)):
    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    if workspace.is_default:
        return JSONResponse({"error": "Default workspace cannot be deleted"}, status_code=400)
    workspace_session_id = _workspace_chat_session_id(workspace.id)
    db.query(ChatMessage).filter(ChatMessage.session_id == workspace_session_id).delete()
    db.query(ChatSession).filter(ChatSession.id == workspace_session_id).delete()
    db.delete(workspace)
    db.commit()
    return {"success": True}


@app.post("/api/workspaces/{workspace_id}/circulars")
async def pin_workspace_circular(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    data = await request.json()
    circular_id = data.get("circular_id")
    if not isinstance(circular_id, str) or not circular_id.strip():
        return JSONResponse({"error": "circular_id is required"}, status_code=400)

    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular:
        return JSONResponse({"error": "Circular not found"}, status_code=404)

    link = db.query(WorkspaceCircular).filter(
        WorkspaceCircular.workspace_id == workspace_id,
        WorkspaceCircular.circular_id == circular_id,
    ).first()
    if not link:
        link = WorkspaceCircular(
            workspace_id=workspace_id,
            circular_id=circular_id,
            role="pinned",
            added_at=datetime.utcnow(),
        )
        db.add(link)
    link.last_viewed_at = datetime.utcnow()
    workspace.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(workspace)
    return _workspace_payload(workspace)


@app.delete("/api/workspaces/{workspace_id}/circulars/{circular_id}")
async def unpin_workspace_circular(workspace_id: str, circular_id: str, db: Session = Depends(get_db)):
    workspace = db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    link = db.query(WorkspaceCircular).filter(
        WorkspaceCircular.workspace_id == workspace_id,
        WorkspaceCircular.circular_id == circular_id,
    ).first()
    if link:
        db.delete(link)
        if workspace.last_circular_id == circular_id:
            workspace.last_circular_id = None
        workspace.updated_at = datetime.utcnow()
        db.commit()
    db.refresh(workspace)
    return _workspace_payload(workspace)


# --- Chat Feature ---

@app.get("/chat")
async def chat_page():
    return spa_index_response()


@app.get("/api/chat/sessions")
async def list_chat_sessions(db: Session = Depends(get_db)):
    _ensure_default_workspace(db)
    workspaces = db.query(ResearchWorkspace).order_by(
        ResearchWorkspace.is_default.desc(),
        func.coalesce(ResearchWorkspace.updated_at, ResearchWorkspace.created_at).desc()
    ).all()
    workspace_session_ids = [
        _workspace_chat_session_id(workspace.id) for workspace in workspaces
    ]
    workspace_sessions = db.query(ChatSession).filter(
        ChatSession.id.in_(workspace_session_ids)
    ).all() if workspace_session_ids else []
    workspace_session_by_id = {
        session.id: session for session in workspace_sessions
    }
    sessions = db.query(ChatSession).order_by(
        func.coalesce(ChatSession.updated_at, ChatSession.created_at).desc()
    ).filter(~ChatSession.id.in_(workspace_session_ids)).limit(50).all()
    return [
        *[
            _workspace_chat_session_payload(
                workspace,
                workspace_session_by_id.get(_workspace_chat_session_id(workspace.id)),
            )
            for workspace in workspaces
        ],
        *[_chat_session_payload(session) for session in sessions],
    ]


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, db: Session = Depends(get_db)):
    workspace = _get_workspace_for_chat_session(db, session_id)
    if workspace:
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        messages = _ordered_chat_messages(db, session_id) if session else []
        return {
            **_workspace_chat_session_payload(workspace, session),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "content": m.content,
                    "circular_ids": _normalize_circular_ids(_safe_json_list(m.circular_ids)),
                    "created_at": _isoformat(m.created_at),
                }
                for m in messages
            ],
            "circulars": _workspace_circular_summaries(workspace),
        }
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    messages = db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.created_at, ChatMessage.id).all()
    circular_ids = _normalize_circular_ids(_safe_json_list(session.circular_ids))
    circulars = db.query(Circular).filter(Circular.id.in_(circular_ids)).all() if circular_ids else []
    circular_by_id = {circular.id: circular for circular in circulars}
    return {
        "id": session.id,
        "title": session.title,
        "session_type": "chat",
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "circular_ids": _normalize_circular_ids(_safe_json_list(m.circular_ids)),
                "created_at": _isoformat(m.created_at),
            }
            for m in messages
        ],
        "circulars": [
            _circular_summary(circular_by_id[circular_id])
            for circular_id in circular_ids
            if circular_id in circular_by_id
        ],
    }


@app.patch("/api/chat/sessions/{session_id}")
async def rename_chat_session(session_id: str, request: Request, db: Session = Depends(get_db)):
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace chat sessions use the workspace name"}, status_code=400)

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    data = await request.json()
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return JSONResponse({"error": "Title cannot be empty"}, status_code=400)

    session.title = title.strip()[:120]
    session.updated_at = datetime.utcnow()
    db.commit()
    return {"id": session.id, "title": session.title, "updated_at": _isoformat(session.updated_at)}


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, db: Session = Depends(get_db)):
    workspace = _get_workspace_for_chat_session(db, session_id)
    if workspace:
        db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
        db.query(ChatSession).filter(ChatSession.id == session_id).delete()
        if workspace.is_default:
            db.query(WorkspaceCircular).filter(
                WorkspaceCircular.workspace_id == workspace.id
            ).delete()
            workspace.last_circular_id = None
            workspace.updated_at = datetime.utcnow()
        else:
            db.delete(workspace)
        db.commit()
        return {"success": True}
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.delete(session)
    db.commit()
    return {"success": True}


def _ordered_chat_messages(db: Session, session_id: str) -> list[ChatMessage]:
    return db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.created_at, ChatMessage.id).all()


def _truncate_chat_messages(
    db: Session,
    session_id: str,
    message_id: str,
    *,
    include_message: bool,
) -> ChatMessage | None:
    messages = _ordered_chat_messages(db, session_id)
    target_index = next(
        (index for index, message in enumerate(messages) if message.id == message_id),
        None,
    )
    if target_index is None:
        return None

    target = messages[target_index]
    delete_from = target_index if include_message else target_index + 1
    delete_ids = [message.id for message in messages[delete_from:]]
    if delete_ids:
        db.query(ChatMessage).filter(ChatMessage.id.in_(delete_ids)).delete(
            synchronize_session=False
        )
    return target


@app.delete("/api/chat/sessions/{session_id}/messages/{message_id}")
async def truncate_chat_session(
    session_id: str,
    message_id: str,
    db: Session = Depends(get_db),
):
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _truncate_chat_messages(
        db, session_id, message_id, include_message=True
    ):
        return JSONResponse({"error": "Message not found"}, status_code=404)

    session.updated_at = datetime.utcnow()
    db.commit()
    return {"success": True}


def _build_chat_circulars_context(
    db: Session,
    circular_ids: list[str],
    query: str = "",
    max_context_tokens: int = 4000,
) -> str:
    from .chat_retrieval import build_chat_context

    context, _ = build_chat_context(
        db, circular_ids, query, max_context_tokens
    )
    return context


def get_or_create_chat_session(db, session_id, message, circular_ids, workspace):
    """Resolve (and persist) the ChatSession for a chat turn.

    Returns ``(session, session_id, circular_ids)``. A new id is minted when none was
    supplied or the referenced session is missing; workspace sessions always adopt the
    workspace name and its pinned circulars as the authoritative selection.
    """
    if workspace:
        circular_ids = _workspace_circular_ids(workspace)
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            session = ChatSession(
                id=session_id,
                title=workspace.name,
                circular_ids=json.dumps(circular_ids),
            )
            db.add(session)
        else:
            session.title = workspace.name
    elif not session_id:
        session_id = str(uuid.uuid4())
        session = ChatSession(
            id=session_id,
            title=message[:80],
            circular_ids=json.dumps(circular_ids),
        )
        db.add(session)
    else:
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            session_id = str(uuid.uuid4())
            session = ChatSession(
                id=session_id,
                title=message[:80],
                circular_ids=json.dumps(circular_ids),
            )
            db.add(session)
    session.circular_ids = json.dumps(circular_ids)
    session.updated_at = datetime.utcnow()
    return session, session_id, circular_ids


@app.post("/api/chat")
async def chat_message(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")
    workspace = _get_workspace_for_chat_session(db, session_id)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)
    if _workspace_id_from_chat_session(session_id) and not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session, session_id, circular_ids = get_or_create_chat_session(
        db, session_id, message, circular_ids, workspace
    )

    user_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=message,
        circular_ids=json.dumps(circular_ids) if circular_ids else None,
    )
    db.add(user_msg)
    db.commit()

    messages = _ordered_chat_messages(db, session_id)
    chat_messages = [{"role": m.role, "content": m.content} for m in messages]

    try:
        client = get_ai_client(db)
        circulars_context = _build_chat_circulars_context(
            db, circular_ids, message, client.config.max_context_tokens
        )
        response_text = client.chat(
            chat_messages,
            db,
            circulars_context=circulars_context,
            selected_circular_ids=circular_ids,
        )
    except Exception as e:
        response_text = friendly_chat_error(e)

    assistant_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=response_text,
    )
    db.add(assistant_msg)
    session.updated_at = datetime.utcnow()
    db.commit()

    return {"response": response_text, "session_id": session_id}


@app.post("/api/chat/stream")
async def chat_message_stream(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")
    replace_message_id = data.get("replace_message_id")
    workspace = _get_workspace_for_chat_session(db, session_id)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)
    if _workspace_id_from_chat_session(session_id) and not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    if replace_message_id and not session_id:
        return JSONResponse(
            {"error": "A session is required to replace a message"}, status_code=400
        )

    session, session_id, circular_ids = get_or_create_chat_session(
        db, session_id, message, circular_ids, workspace
    )

    if replace_message_id:
        user_msg = _truncate_chat_messages(
            db, session_id, replace_message_id, include_message=False
        )
        if not user_msg or user_msg.role != "user":
            db.rollback()
            return JSONResponse({"error": "User message not found"}, status_code=404)
        user_msg.content = message
        user_msg.circular_ids = json.dumps(circular_ids) if circular_ids else None
    else:
        user_msg = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role="user",
            content=message,
            circular_ids=json.dumps(circular_ids) if circular_ids else None,
        )
        db.add(user_msg)
    db.commit()

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    def stream_response():
        stream_db = SessionLocal()
        response_parts: list[str] = []
        try:
            yield sse("meta", {"session_id": session_id})

            rows = _ordered_chat_messages(stream_db, session_id)
            chat_messages = [{"role": m.role, "content": m.content} for m in rows]
            client = get_ai_client(stream_db)
            circulars_context = _build_chat_circulars_context(
                stream_db,
                circular_ids,
                message,
                client.config.max_context_tokens,
            )

            for chunk in client.stream_chat(
                chat_messages,
                stream_db,
                circulars_context=circulars_context,
                selected_circular_ids=circular_ids,
            ):
                if isinstance(chunk, dict):
                    yield sse("status", chunk)
                    continue
                response_parts.append(chunk)
                yield sse("token", {"content": chunk})

            response_text = "".join(response_parts)
            assistant_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content=response_text,
            )
            stream_db.add(assistant_msg)
            stream_session = stream_db.query(ChatSession).filter(
                ChatSession.id == session_id
            ).first()
            if stream_session:
                stream_session.updated_at = datetime.utcnow()
            stream_db.commit()
            yield sse("done", {"session_id": session_id, "message_id": assistant_msg.id})
        except Exception as e:
            stream_db.rollback()
            yield sse(
                "error",
                {"error": friendly_chat_error(e), "session_id": session_id},
            )
        finally:
            stream_db.close()

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/circulars/export_csv")
async def export_search_csv(
    q: str = "",
    start_year: str | None = None,
    end_year: str | None = None,
    department: str | None = None,
    sort_by: str = "relevance",
    tag: str | None = None,
    db: Session = Depends(get_db)
):
    results, _ = search_engine.search(
        q, db, limit=500,
        start_year=_parse_year(start_year),
        end_year=_parse_year(end_year),
        department=department,
        sort_by=sort_by,
        tag=tag,
    )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Circular Ref", "Title", "Department", "Date", "Url"])

    for r in results:
        c = r["circular"]
        date_str = c.date.strftime('%Y-%m-%d') if c.date else 'N/A'
        writer.writerow([c.reference, c.title, c.department, date_str, c.url])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=sbpeye_search_results.csv"}
    )

@app.post("/api/circulars/batch_download")
async def batch_download(
    circular_ids: list[str] = Form(...),
    db: Session = Depends(get_db)
):
    circulars = db.query(Circular).filter(Circular.id.in_(circular_ids)).all()
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "a", zipfile.ZIP_DEFLATED, False) as zip_file:
        for c in circulars:
            if not c.url:
                continue

            # Safely create a file name
            safe_ref = (c.reference or c.id).replace("/", "_").replace("\\", "_")
            if c.url.lower().endswith(".pdf"):
                try:
                    resp = cloudscraper.create_scraper().get(c.url, headers=HEADERS, timeout=20)
                    resp.raise_for_status()
                    zip_file.writestr(f"{safe_ref}.pdf", resp.content)
                except Exception as e:
                    print(f"Failed to fetch {c.url}: {e}")
            else:
                try:
                    html = fetch_page_cached(c.url)
                    zip_file.writestr(f"{safe_ref}.html", html)
                except Exception as e:
                    print(f"Failed to fetch {c.url}: {e}")
                    continue

                used_names: set[str] = set()
                for attachment in c.attachments:
                    local_path = (
                        PROJECT_ROOT / attachment.local_path
                        if attachment.local_path
                        else None
                    )
                    if local_path is None or not local_path.exists():
                        local_path, _, error, _ = download_attachment(
                            c.id,
                            {
                                "url": attachment.original_url,
                                "filename": attachment.filename,
                                "file_type": attachment.file_type,
                            },
                        )
                        if local_path is None:
                            print(
                                f"Failed to fetch attachment "
                                f"{attachment.original_url}: {error}"
                            )
                            continue
                        attachment.local_path = str(local_path.relative_to(PROJECT_ROOT))
                        db.commit()

                    safe_name = Path(attachment.filename).name or attachment.id
                    if safe_name in used_names:
                        path = Path(safe_name)
                        safe_name = f"{path.stem}_{attachment.id[:8]}{path.suffix}"
                    used_names.add(safe_name)
                    zip_file.writestr(
                        f"{safe_ref}_attachments/{safe_name}",
                        local_path.read_bytes(),
                    )
            time.sleep(0.5) # Be gentle to SBP servers

    zip_buffer.seek(0)
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=circulars_batch.zip"}
    )
