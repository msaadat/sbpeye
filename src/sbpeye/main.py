from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from urllib.parse import urljoin, urlparse, urlencode
from pathlib import Path
from contextlib import asynccontextmanager
import requests as http_requests
from bs4 import BeautifulSoup
import re as _re
import os
import json
import uuid

from .database import PROJECT_ROOT, engine, Base, get_db, SessionLocal, has_vector_store_data
from .models import AIGenerationJob, Attachment, CachedDocument, SyncStatus, Circular, CircularRelationship, EcoDataSeries, EcoDataEntry, Settings, ChatSession, ChatMessage
from .search import search_engine
from .ai import AIClient, AIConfig, get_ai_client, get_provider_api_key, get_provider_definition, normalize_provider
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


def _parse_year(val: str | None) -> int | None:
    return int(val) if val else None


def _format_timestamp(value: datetime | str | None) -> str:
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y %H:%M")
    return value or "Never"


def _safe_json_list(value: str | None) -> list:
    if not value:
        return []
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _safe_json_object(value: str | None) -> dict | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _normalize_circular_ids(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(
        item.strip() for item in value if isinstance(item, str) and item.strip()
    ))


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _circular_summary(
    circular: Circular,
    snippet: str | None = None,
    match_source: str = "circular",
    attachment_id: str | None = None,
    attachment_filename: str | None = None,
    source_ref: str | None = None,
    source_page: int | None = None,
) -> dict:
    return {
        "id": circular.id,
        "title": circular.title,
        "department": circular.department,
        "reference": circular.reference,
        "date": circular.date.strftime("%Y-%m-%d") if circular.date else None,
        "url": circular.url,
        "summary": circular.summary[:200] if circular.summary else None,
        "tags": _safe_json_list(circular.tags),
        "status": circular.status or "active",
        "snippet": snippet or "",
        "match_source": match_source,
        "attachment_id": attachment_id,
        "attachment_filename": attachment_filename,
        "source_ref": source_ref,
        "source_page": source_page,
    }


def _settings_payload(config: AIConfig, embedding: EmbeddingConfig) -> dict:
    ai_secret = AIConfig.secret_state(config.provider)
    embedding_secret = EmbeddingConfig.secret_state(embedding.provider)
    return {
        "provider": config.provider,
        "base_url": config.base_url,
        "api_key": "",
        "api_key_configured": ai_secret["api_key_configured"],
        "api_key_env_var": ai_secret["api_key_env_var"],
        "model": config.model,
        "chat_model": config.chat_model,
        "max_context_tokens": config.max_context_tokens,
        "ai_provider": config.provider,
        "ai_base_url": config.base_url,
        "ai_api_key": "",
        "ai_model": config.model,
        "ai_chat_model": config.chat_model,
        "ai_max_context_tokens": config.max_context_tokens,
        "embedding_provider": embedding.provider,
        "embedding_model": embedding.model,
        "embedding_base_url": embedding.base_url,
        "embedding_api_key": "",
        "embedding_api_key_configured": embedding_secret["api_key_configured"],
        "embedding_api_key_env_var": embedding_secret["api_key_env_var"],
        "managed_env_file": str(managed_env_path().name),
    }


def _delete_setting_key(db: Session, key: str) -> bool:
    row = db.query(Settings).filter(Settings.key == key).first()
    if not row:
        return False
    db.delete(row)
    return True


def _purge_legacy_secret_settings(db: Session) -> bool:
    changed = False
    for key in ("ai_api_key", "embedding_api_key"):
        changed = _delete_setting_key(db, key) or changed
    if changed:
        db.commit()
    return changed


def _save_ai_secret(provider: str, api_key: str | None, clear_secret: bool) -> None:
    definition = get_provider_definition(provider)
    secret_state = AIConfig.secret_state(provider)
    target_env_var = definition.api_key_env_vars[0]
    if clear_secret:
        unset_managed_env_value(str(secret_state["api_key_env_var"]))
        return
    if api_key is None or not api_key.strip():
        return
    set_managed_env_value(target_env_var, api_key.strip())


def _save_embedding_secret(api_key: str | None, clear_secret: bool) -> None:
    target_env_var = "EMBEDDING_API_KEY"
    secret_state = EmbeddingConfig.secret_state()
    if clear_secret:
        unset_managed_env_value(str(secret_state["api_key_env_var"]))
        return
    if api_key is None or not api_key.strip():
        return
    set_managed_env_value(target_env_var, api_key.strip())


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


SBP_BASE = "https://www.sbp.org.pk"


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
    db = SessionLocal()
    try:
        _purge_legacy_secret_settings(db)
    finally:
        db.close()
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
        "summary": c.summary,
        "tags": _safe_json_list(c.tags),
        "compliance_checklist": _safe_json_object(c.compliance_checklist),
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


def _normalize_title(text: str) -> str:
    return _re.sub(r'\s+', ' ', text).strip()


def _scrape_sbp_news(db: Session | None = None) -> dict:
    resp = http_requests.get(f"{SBP_BASE}/index.html", headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    press_releases = []
    whats_new = []

    pr_div = soup.find("div", id="PressRelease3")
    if pr_div:
        for li in pr_div.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            title = _normalize_title(a.get_text(strip=True))
            href = a.get("href", "")
            if not title or not href:
                continue
            if title.lower() in ("more", "clarifications/rebuttals"):
                continue
            if href.endswith("-U.pdf") or "urdu" in title.lower():
                continue
            url = urljoin(SBP_BASE + "/", href)
            if db and db.query(Circular).filter(Circular.url == url).first():
                url = f"/view_circular?cir={url}"
            press_releases.append({"title": title, "url": url})
            if len(press_releases) >= 5:
                break

    for table in soup.find_all("table"):
        table_text = table.get_text()[:200].lower()
        if "what" in table_text and "new" in table_text:
            box = table.find("div", class_="box")
            if not box:
                continue
            for li in box.find_all("li"):
                a = li.find("a")
                if not a:
                    continue
                title = _normalize_title(a.get_text(strip=True))
                href = a.get("href", "")
                if not title or not href:
                    continue
                if title.lower() == "more":
                    continue
                url = urljoin(SBP_BASE + "/", href)
                if db and db.query(Circular).filter(Circular.url == url).first():
                    url = f"/view_circular?cir={url}"
                whats_new.append({"title": title, "url": url})
                if len(whats_new) >= 5:
                    break
            break

    return {"press_releases": press_releases, "whats_new": whats_new}


@app.get("/api/sbp_news")
async def get_sbp_news(db: Session = Depends(get_db)):
    try:
        return _scrape_sbp_news(db)
    except Exception as e:
        return {"press_releases": [], "whats_new": [], "error": str(e)}


def _document_payload(attachment: Attachment | CachedDocument) -> dict:
    local_path = PROJECT_ROOT / attachment.local_path if attachment.local_path else None
    return {
        "id": attachment.id,
        "circular_id": getattr(attachment, "circular_id", None),
        "filename": attachment.filename,
        "file_type": attachment.file_type,
        "original_url": attachment.original_url,
        "cached": bool(local_path and local_path.is_file()),
        "content_url": f"/api/documents/{attachment.id}/content",
        "extraction_status": getattr(attachment, "extraction_status", None),
        "error": getattr(attachment, "extraction_error", None) or getattr(attachment, "error", None),
    }


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
            path, _, error = download_attachment("standalone", info, force=refresh)
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
            path, _, error = download_attachment("standalone", info, force=refresh)
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
        resp = http_requests.get(url, headers=HEADERS, timeout=20)
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
        resp = http_requests.get(url, headers=HEADERS, timeout=30)
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
    _purge_legacy_secret_settings(db)
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
        model=data.get("model", data.get("ai_model", "local-model")),
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
    _purge_legacy_secret_settings(db)
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


# --- Chat Feature ---

@app.get("/chat")
async def chat_page():
    return spa_index_response()


@app.get("/api/chat/sessions")
async def list_chat_sessions(db: Session = Depends(get_db)):
    sessions = db.query(ChatSession).order_by(ChatSession.created_at.desc()).limit(50).all()
    return [
        {"id": s.id, "title": s.title, "created_at": s.created_at.isoformat() if s.created_at else None}
        for s in sessions
    ]


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    if session.circular_ids is not None:
        circular_ids = _normalize_circular_ids(_safe_json_list(session.circular_ids))
    else:
        # Legacy sessions predate authoritative session context. Preserve their
        # historical selection until the next message stores current UI state.
        circular_ids = list(dict.fromkeys(
            cid
            for message in messages
            for cid in _normalize_circular_ids(_safe_json_list(message.circular_ids))
        ))
    circulars = db.query(Circular).filter(Circular.id.in_(circular_ids)).all() if circular_ids else []
    circular_by_id = {circular.id: circular for circular in circulars}
    return {
        "id": session.id,
        "title": session.title,
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ],
        "circulars": [
            _circular_summary(circular_by_id[circular_id])
            for circular_id in circular_ids
            if circular_id in circular_by_id
        ],
    }


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(session_id: str, db: Session = Depends(get_db)):
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    db.delete(session)
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


@app.post("/api/chat")
async def chat_message(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    if not session_id:
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

    user_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=message,
        circular_ids=json.dumps(circular_ids) if circular_ids else None,
    )
    db.add(user_msg)
    db.commit()

    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
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
        response_text = f"Error generating response: {str(e)}"

    assistant_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="assistant",
        content=response_text,
    )
    db.add(assistant_msg)
    db.commit()

    return {"response": response_text, "session_id": session_id}


@app.post("/api/chat/stream")
async def chat_message_stream(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    if not session_id:
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

            rows = stream_db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
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
            stream_db.commit()
            yield sse("done", {"session_id": session_id})
        except Exception as e:
            stream_db.rollback()
            error_text = f"Error generating response: {str(e)}"
            if not response_parts:
                response_parts.append(error_text)
                yield sse("token", {"content": error_text})
            assistant_msg = ChatMessage(
                id=str(uuid.uuid4()),
                session_id=session_id,
                role="assistant",
                content="".join(response_parts),
            )
            stream_db.add(assistant_msg)
            stream_db.commit()
            yield sse("error", {"error": str(e), "session_id": session_id})
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
                    resp = http_requests.get(c.url, headers=HEADERS, timeout=20)
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
                        local_path, _, error = download_attachment(
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
