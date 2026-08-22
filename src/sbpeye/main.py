from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form, Body
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, extract, and_, or_, text
from urllib.parse import quote, urljoin, urlparse, urlencode
from pathlib import Path
from contextlib import asynccontextmanager
import requests
from bs4 import BeautifulSoup
import re as _re
import logging
import os
import json
import uuid
import threading

from .database import PROJECT_ROOT, AppSessionLocal, engine, Base, checkpoint_sqlite, get_app_db, get_db, SessionLocal, has_vector_store_data
from .models import AIGenerationJob, Attachment, CachedDocument, SyncStatus, circular_sync_only, Circular, CircularEntity, CircularRelationship, EcoDataSeries, EcoDataEntry, RegDocument, RegDocumentVersion, Settings, ChatSession, ChatMessage, ResearchWorkspace, User, WorkspaceCircular, upsert_settings
from .api.admin import router as admin_router
from .api.debug import router as debug_router
from .llm_debug import (
    bind_context,
    emit_event,
    fail_interrupted_traces,
    trace_operation,
)
from .search import backfill_fts, backfill_laws_fts, index_circular_fts, resolve_metric_terms, search_engine
from .ai import AIClient, AIConfig, MissingUserAIConfig, classify_provider_state, friendly_chat_error, get_ai_client, get_ai_client_for_user, get_provider_api_key, get_provider_definition, normalize_provider
from .circular_ai import GENERATION_ACTIONS, generation_job_payload, run_generation_job
from .laws_ai import (
    CONTAINER_FEATURES,
    GAP_MANIFEST,
    LAW_GENERATION_ACTIONS,
    STRUCTURAL_GAPS,
    gap_message,
    is_container,
    law_corpus,
    rollup_sources,
    run_law_generation_job,
)
from .checklist_export import build_checklist_workbook, circular_subject, law_subject
from .chat_export import render_session_markdown, session_filename
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
    CIRCULARS_LISTING_URL_FIRST,
    HEADERS,
    attachment_id,
    circular_identity,
    download_attachment,
    fetch_page,
    fetch_page_cached,
    parse_circular_listing,
    process_attachment,
    process_circular,
    scrape_circulars,
)
from .auth_routes import (
    bootstrap_admin,
    current_user,
    is_public_path,
    admin_only,
    require_admin,
    resolve_request_user,
    router as auth_router,
    verify_auth_configuration,
)
from .env import CIRCULAR_FILES_DIR, LAWS_ARCHIVE_DIR
from .scraper.laws import download_law_file
from .scraper.clean_html import clean_sbp_html, extract_sbp_text
from .scraper.ecodata_index import scrape_ecodata_index
from .scraper.pdf_summarizer import summarize_pdf, is_summarizable
from datetime import datetime, timedelta


from .api.serializers import (
    DEFAULT_WORKSPACE_ID,
    DEFAULT_WORKSPACE_NAME,
    WORKSPACE_CHAT_SESSION_PREFIX,
    _chat_session_payload,
    _circular_regulations,
    _circular_summary,
    _document_payload,
    _ensure_default_workspace,
    _format_timestamp,
    _get_workspace_for_chat_session,
    _isoformat,
    _law_detail,
    _law_summary,
    _law_version_payload,
    _load_workspace_circulars,
    split_law_title,
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
        # process_circular already upserts the FTS row; keep this explicit call
        # in case the circular row existed but its content changed here.
        index_circular_fts(db, circular)
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


def fail_interrupted_sync_jobs() -> None:
    """Mark a previous process' in-flight sync as failed, whichever corpus it scraped.

    Deliberately *not* filtered through `circular_sync_only()`. Laws runs share this
    table, and excluding them meant a killed `laws sync` left its row reading "running"
    with no completion for ever — two such rows accumulated in the local corpus before
    the admin console started displaying run history and made them visible.

    The one case this could mislabel is a CLI laws sync in flight on the same corpus
    while the server boots. It self-corrects: `_run_laws_sync` writes the terminal status
    from its own session when it finishes, and that UPDATE lands after this one.
    """
    db = SessionLocal()
    try:
        interrupted = db.query(SyncStatus).filter(
            SyncStatus.status.in_(("queued", "running")),
        ).all()
        for job in interrupted:
            job.status = "failed"
            job.error = "Sync was interrupted by a server restart."
            # `completed_at` stays NULL: the run has no known end. Stamping it with the
            # restart time made run history report a sync killed two days ago as having
            # taken 38 hours, which is a duration nothing measured.
        if interrupted:
            db.commit()
    finally:
        db.close()


def _warm_up_search_index() -> None:
    db = SessionLocal()
    try:
        # Build the persistent FTS5 indexes once if empty; a no-op thereafter.
        backfill_fts(db)
        backfill_laws_fts(db)
    finally:
        db.close()


def _document_download_info(document: Attachment | CachedDocument) -> dict:
    return {
        "url": document.original_url,
        "filename": document.filename,
        "file_type": document.file_type,
    }


def _cached_document_path(document: Attachment | CachedDocument) -> Path | None:
    if not document.local_path:
        return None
    path = (PROJECT_ROOT / document.local_path).resolve()
    if CIRCULAR_FILES_DIR.resolve() not in path.parents:
        return None
    return path if path.is_file() else None


def _ensure_document_cached(
    db: Session,
    document: Attachment | CachedDocument,
    *,
    refresh: bool = False,
) -> tuple[Attachment | CachedDocument, Path | None]:
    cached_path = _cached_document_path(document)
    if cached_path is not None and not refresh:
        return document, cached_path

    info = _document_download_info(document)
    if isinstance(document, Attachment):
        circular = document.circular
        if circular is None:
            document.extraction_status = "error"
            document.extraction_error = "Attachment is not linked to a circular."
            db.commit()
            return document, None
        info["id"] = document.id
        if refresh:
            # An explicit refresh is a re-ingest and is meant to rewrite the row:
            # re-download, re-extract, and mark the attachment for re-embedding.
            document = process_attachment(
                db,
                circular,
                info,
                force_download=True,
            )
            return document, _cached_document_path(document)

        # A cache miss on an ordinary view is not a re-ingest. Fetch the bytes, record
        # where they landed, and touch nothing else.
        #
        # `process_attachment` used to run here too, which committed seven columns for
        # what the caller asked to be a read: it re-extracted `content_text` and reset
        # `is_vectorized` to 0. That flag is a ledger — `index_pending_attachments`
        # skips on it, the CLI selects work by it, the API reports it, and
        # `chat_retrieval` states it to the model as `indexed=yes/no`. Resetting it does
        # not touch the vector store, whose chunks stay in place and stay correct; it
        # just makes the ledger claim "unindexed" for a row that is indexed, and buys a
        # redundant re-embed on the next index run. On a deployment `attachments/` starts
        # empty, so every one of the 1368 rows with a path is a guaranteed miss and any
        # tester opening any PDF would flip it.
        local_path, _, download_error, _ = download_attachment(
            circular.id, info, force=True
        )
        if local_path is None:
            # Left uncommitted deliberately: a failed fetch is not corpus state. The
            # attribute carries the reason back to the route, which reads it to build
            # the 502, and is discarded when the request's session closes.
            document.extraction_error = (
                download_error or "Attachment could not be downloaded."
            )
            return document, None
        document.local_path = str(local_path.relative_to(PROJECT_ROOT))
        db.commit()
        return document, _cached_document_path(document)

    path, _, error, _ = download_attachment("standalone", info, force=True)
    document.local_path = str(path.relative_to(PROJECT_ROOT)) if path else None
    document.error = error
    db.commit()
    return document, _cached_document_path(document)


def _law_archive_path(version: RegDocumentVersion) -> Path | None:
    """The archived file for a law version, if it is on disk and inside the archive."""
    if not version.local_path:
        return None
    candidate = (PROJECT_ROOT / version.local_path).resolve()
    # Guarded against the archive specifically. Before the trees were split these two
    # guards shared one root, so an attachment path satisfied the law guard and vice
    # versa; they now each admit only their own tree.
    if LAWS_ARCHIVE_DIR.resolve() not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _ensure_law_version_cached(
    db: Session, version: RegDocumentVersion
) -> tuple[Path | None, str | None]:
    """The archived file for a law version, fetching it once if it is not on disk.

    Refetching a law file is not the same operation as refetching a circular attachment.
    An attachment's bytes are reproducible: `file_url` serves the same document tomorrow.
    SBP replaces law PDFs in place and keeps no history, so a law's URL always serves
    whichever edition is current — for a superseded version that is different bytes by
    definition, and serving them would answer a request for the historical record with
    today's text.

    The content hash is what makes fetching safe. Bytes are accepted for this version
    only if they hash to the hash this version is identified by. Returns (path, error).
    """
    path = _law_archive_path(version)
    if path is not None:
        return path, None
    if not version.file_url:
        return None, "This version has no source file to download."

    local_path, content_hash, error = download_law_file(
        version.document_id, version.file_url
    )
    if local_path is None:
        return None, error or "The archived file could not be downloaded."

    # `download_law_file` names the destination from the hash of what it actually
    # fetched (`_archive_name`), so a replaced edition lands under a different filename
    # than the archived one and cannot overwrite it. Whatever arrived is safely on disk;
    # the only open question is which version those bytes belong to.
    owner = (
        db.query(RegDocumentVersion)
        .filter(
            RegDocumentVersion.document_id == version.document_id,
            RegDocumentVersion.content_hash == content_hash,
        )
        .first()
    )
    if owner is not None:
        relative = str(local_path.relative_to(PROJECT_ROOT))
        if owner.local_path != relative:
            owner.local_path = relative
            db.commit()
        if owner.id == version.id:
            return _law_archive_path(version), None

    # The bytes are a different edition than the one asked for. Leave every version row
    # otherwise untouched: creating a version and deciding `is_current` across its
    # siblings is sync's job (`capture_document_version`), it is admin-gated under the
    # corpus write policy, and an unsynced edition sitting in the archive is exactly what
    # `download_law_file` expects to find on the next run.
    return None, (
        "SBP no longer serves this edition at its source URL - the file there is now a "
        "different version. The archived copy is not available on this deployment."
    )


@asynccontextmanager
async def app_lifespan(_app: FastAPI):
    # Before anything else: a deployment that cannot sign cookies cannot authenticate
    # anyone, and finding that out at boot beats finding it out when a tester tries to
    # sign in. Raising here fails the container rather than serving an open door.
    verify_auth_configuration()
    bootstrap_admin()
    fail_interrupted_traces()
    fail_interrupted_ai_jobs()
    fail_interrupted_sync_jobs()
    threading.Thread(target=_warm_up_search_index, daemon=True).start()
    # Started here rather than run here: the first scrape is a live HTTP round-trip to
    # sbp.org.pk, and doing it inside the lifespan would hold the container short of
    # ready for as long as SBP takes to answer.
    _ecodata_stop.clear()
    ecodata_thread = threading.Thread(
        target=_ecodata_refresh_loop, name="ecodata-refresh", daemon=True
    )
    ecodata_thread.start()
    try:
        yield
    finally:
        # Signalled rather than left to the daemon flag, so a reload in development does
        # not leave a scraper running against a database the next process owns.
        _ecodata_stop.set()
        ecodata_thread.join(timeout=5)
        # Last, and after the scraper has stopped: under WAL the recent commits live in a
        # `-wal` sidecar until something folds them back, and the corpus is moved between
        # machines by copying `sbpeye.db`. Without this a clean stop can still leave the
        # bulk of a sync outside the file the upload picks up.
        checkpoint_sqlite()


# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="SBPEye",
    description="Independent SBP Circulars & EcoData Indexer",
    lifespan=app_lifespan,
)
# Keep routes flat in the application router. The FastAPI version used by this
# project defers ``app.include_router`` behind an internal placeholder, which would
# make literal-route shadow checks see the placeholder instead of the API routes.
app.router.routes.extend(admin_router.routes)
app.router.routes.extend(debug_router.routes)
app.router.routes.extend(auth_router.routes)
app.router._mark_routes_changed()


def _bind_dependency_overrides(routes) -> None:
    """Let `app.dependency_overrides` reach routes that were extended in, not included.

    `include_router` passes its `dependency_overrides_provider` down to each route, and
    `APIRoute` captures it in the request handler it builds during `__init__`. Routes
    copied off a bare `APIRouter` never got one, so overriding `get_db` or `get_app_db`
    silently missed them and they went on using the process-wide session factories.

    That is not only a test-harness inconvenience: it is why the suite was writing live
    rows into the developer's real `sbpeye_app.db`. The handler has to be rebuilt after
    the attribute is set, because it closes over the value it had at construction.
    """
    from fastapi.routing import APIRoute, request_response

    for route in routes:
        if isinstance(route, APIRoute):
            route.dependency_overrides_provider = app
            route.app = request_response(route.get_route_handler())


_bind_dependency_overrides(admin_router.routes)
_bind_dependency_overrides(debug_router.routes)
_bind_dependency_overrides(auth_router.routes)


@app.exception_handler(StarletteHTTPException)
async def http_exception_as_error(request: Request, exc: StarletteHTTPException):
    """Report raised exceptions in the shape the rest of the application uses.

    Everything that answers with a failure here writes `{"error": ...}` — the auth
    middleware, and every route that returns a `JSONResponse` itself — and the browser
    client reads that key. FastAPI's own handler writes `{"detail": ...}` instead, so an
    `HTTPException` arrived at the SPA as an object it could not read, and a carefully
    worded refusal ("Indexing a new circular from a link is limited to administrators…
    Ask an admin to add it") was displayed as "Request failed with 403". One shape, so
    the message a route bothers to write is the message the user gets.
    """
    detail = exc.detail
    payload = (
        {"error": detail} if isinstance(detail, str)
        # A non-string detail is structured data — FastAPI's validation errors are the
        # usual case. It is kept intact under its own key rather than stringified, with
        # a readable sentence alongside for the client that only knows how to show one.
        else {"error": "The request could not be completed.", "detail": detail}
    )
    return JSONResponse(payload, status_code=exc.status_code, headers=exc.headers)


@app.middleware("http")
async def require_authentication(request: Request, call_next):
    """The authentication boundary for the whole application.

    Everything is closed unless `auth_routes.is_public_path` opens it. Doing this as
    middleware rather than a `Depends` on each route means a route added later is private
    by default: the cost of forgetting is a login prompt somebody notices immediately,
    not a public endpoint nobody does.
    """
    if is_public_path(request.url.path):
        return await call_next(request)

    user = resolve_request_user(request)
    if user is None:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "Sign in to continue."}, status_code=401)
        # A browser navigation, so send them somewhere useful and bring them back after.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(f"/login?next={quote(target, safe='')}", status_code=303)

    request.state.user = user
    return await call_next(request)

# Setup SPA static files
STATIC_DIR = Path(__file__).resolve().parent / "static"
os.makedirs(STATIC_DIR, exist_ok=True)

SPA_DIR = STATIC_DIR / "spa"
SPA_INDEX = SPA_DIR / "index.html"
SPA_ASSETS_DIR = SPA_DIR / "assets"

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
if SPA_ASSETS_DIR.exists():
    app.mount("/spa/assets", StaticFiles(directory=SPA_ASSETS_DIR), name="spa-assets")


_CIRCULAR_SYNC_LOCK = threading.Lock()
ACTIVE_SYNC_STATUSES = {"queued", "running"}
REMOTE_CIRCULAR_CHECK_TTL = timedelta(minutes=30)
_REMOTE_CIRCULAR_CHECK_LOCK = threading.Lock()
_REMOTE_CIRCULAR_CHECK_CACHE: dict | None = None
_REMOTE_CIRCULAR_CHECK_RUNNING = False


def _latest_sync_status(db: Session) -> SyncStatus | None:
    return (
        db.query(SyncStatus)
        .filter(circular_sync_only())
        .order_by(SyncStatus.id.desc())
        .first()
    )


def _remote_circular_availability_payload(db: Session) -> dict:
    """Fetch the first SBP circular listing page and compare it with local rows."""
    checked_at = datetime.utcnow()
    items = parse_circular_listing(fetch_page(CIRCULARS_LISTING_URL_FIRST))
    listing_rows = [
        {
            **item,
            "id": circular_identity(item.get("reference"), item["url"]),
        }
        for item in items
        if item.get("url")
    ]
    listing_ids = [item["id"] for item in listing_rows]
    existing_ids: set[str] = set()
    if listing_ids:
        existing_ids = {
            row[0]
            for row in db.query(Circular.id).filter(Circular.id.in_(listing_ids)).all()
        }
    missing = [item for item in listing_rows if item["id"] not in existing_ids]
    newest = missing[0] if missing else None

    return {
        "remote_check_status": "new_available" if missing else "fresh",
        "remote_checked_at": checked_at.isoformat(),
        "remote_new_count": len(missing),
        "remote_newest": (
            {
                "id": newest["id"],
                "title": newest.get("title") or "",
                "reference": newest.get("reference") or None,
                "department": newest.get("department") or None,
                "date": newest.get("date") or None,
                "url": newest.get("url") or None,
            }
            if newest
            else None
        ),
        "remote_error": None,
    }


def _set_remote_circular_check_cache(payload: dict) -> None:
    global _REMOTE_CIRCULAR_CHECK_CACHE
    _REMOTE_CIRCULAR_CHECK_CACHE = {
        **payload,
        "_expires_at": datetime.utcnow() + REMOTE_CIRCULAR_CHECK_TTL,
    }


def _clear_remote_circular_check_cache() -> None:
    global _REMOTE_CIRCULAR_CHECK_CACHE
    with _REMOTE_CIRCULAR_CHECK_LOCK:
        _REMOTE_CIRCULAR_CHECK_CACHE = None


def _run_remote_circular_check() -> None:
    global _REMOTE_CIRCULAR_CHECK_RUNNING
    db = SessionLocal()
    try:
        payload = _remote_circular_availability_payload(db)
    except Exception as exc:
        payload = {
            "remote_check_status": "error",
            "remote_checked_at": datetime.utcnow().isoformat(),
            "remote_new_count": 0,
            "remote_newest": None,
            "remote_error": str(exc),
        }
    finally:
        db.close()

    with _REMOTE_CIRCULAR_CHECK_LOCK:
        _set_remote_circular_check_cache(payload)
        _REMOTE_CIRCULAR_CHECK_RUNNING = False


def _remote_circular_check_status() -> dict:
    """Return cached remote availability and refresh it asynchronously when stale."""
    global _REMOTE_CIRCULAR_CHECK_RUNNING
    now = datetime.utcnow()
    with _REMOTE_CIRCULAR_CHECK_LOCK:
        cache = _REMOTE_CIRCULAR_CHECK_CACHE
        if cache and cache.get("_expires_at") and cache["_expires_at"] > now:
            return {key: value for key, value in cache.items() if not key.startswith("_")}

        if not _REMOTE_CIRCULAR_CHECK_RUNNING:
            _REMOTE_CIRCULAR_CHECK_RUNNING = True
            threading.Thread(target=_run_remote_circular_check, daemon=True).start()

    return {
        "remote_check_status": "checking",
        "remote_checked_at": None,
        "remote_new_count": None,
        "remote_newest": None,
        "remote_error": None,
    }


def _latest_successful_sync(db: Session) -> SyncStatus | None:
    return (
        db.query(SyncStatus)
        .filter(
            SyncStatus.status == "success",
            SyncStatus.last_sync_date.isnot(None),
            circular_sync_only(),
        )
        .order_by(SyncStatus.last_sync_date.desc())
        .first()
    )


def _parse_sync_parameters(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sync_status_payload(sync_status: SyncStatus | None, last_success: SyncStatus | None = None) -> dict:
    last_sync_dt = None
    if last_success and isinstance(last_success.last_sync_date, datetime):
        last_sync_dt = last_success.last_sync_date
    elif sync_status and isinstance(sync_status.last_sync_date, datetime):
        last_sync_dt = sync_status.last_sync_date

    status = sync_status.status if sync_status and sync_status.status else "idle"
    return {
        "job_id": sync_status.job_id if sync_status else None,
        "status": status,
        "live_status": status.upper(),
        "running": status in ACTIVE_SYNC_STATUSES,
        "started_at": _isoformat(sync_status.started_at) if sync_status else None,
        "completed_at": _isoformat(sync_status.completed_at) if sync_status else None,
        "last_sync_display": _format_timestamp(last_sync_dt),
        "last_sync": _format_timestamp(last_sync_dt),
        "last_sync_dt": last_sync_dt.isoformat() if isinstance(last_sync_dt, datetime) else None,
        "last_sync_raw": last_sync_dt.isoformat() if isinstance(last_sync_dt, datetime) else None,
        "error": sync_status.error if sync_status else None,
        "parameters": _parse_sync_parameters(sync_status.parameters if sync_status else None),
        "processed_count": sync_status.processed_count if sync_status else None,
        "skipped_count": sync_status.skipped_count if sync_status else None,
        "error_count": sync_status.error_count if sync_status else None,
    }


def _as_string_list(value) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _sync_options_from_payload(data: dict) -> dict:
    departments = _as_string_list(data.get("departments", data.get("department")))
    years = _as_string_list(data.get("years", data.get("year")))
    invalid_years = [year for year in years if not (year.isdigit() and len(year) == 4)]
    if invalid_years:
        raise ValueError("Years must be four-digit values.")

    try:
        limit = int(data.get("limit") or 0)
        workers = int(data.get("workers") or 1)
    except (TypeError, ValueError):
        raise ValueError("Limit and workers must be integers.") from None

    if limit < 0:
        raise ValueError("Limit cannot be negative.")
    if workers < 1 or workers > 8:
        raise ValueError("Workers must be between 1 and 8.")

    include_attachments = bool(data.get("include_attachments", not data.get("no_attachments", False)))
    return {
        "departments": departments or None,
        "years": years or None,
        "limit": limit,
        "skip_llm": bool(data.get("skip_llm", True)),
        "verbose": bool(data.get("verbose", False)),
        "force_fetch": bool(data.get("force_fetch", False)),
        "force_download": bool(data.get("force_download", False)),
        "include_attachments": include_attachments,
        "workers": workers,
        "full_listing": bool(data.get("full_listing", False)),
    }


def _run_circular_sync(job_id: str, options: dict) -> None:
    db = SessionLocal()
    try:
        job = db.query(SyncStatus).filter(SyncStatus.job_id == job_id).first()
        if not job:
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        db.commit()

        result = scrape_circulars(db, **options) or {}

        job = db.query(SyncStatus).filter(SyncStatus.job_id == job_id).first()
        if not job:
            return
        job.status = "success"
        job.completed_at = datetime.utcnow()
        job.last_sync_date = job.completed_at
        job.error = None
        job.processed_count = int(result.get("processed") or 0)
        job.skipped_count = int(result.get("skipped") or 0)
        job.error_count = int(result.get("errors") or 0)
        db.commit()
        _clear_remote_circular_check_cache()
    except Exception as exc:
        db.rollback()
        job = db.query(SyncStatus).filter(SyncStatus.job_id == job_id).first()
        if job:
            job.status = "failed"
            job.completed_at = datetime.utcnow()
            job.error = str(exc)
            db.commit()
        _clear_remote_circular_check_cache()
    finally:
        db.close()
        _CIRCULAR_SYNC_LOCK.release()


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


@app.get("/laws")
async def laws_spa():
    return spa_index_response()


@app.get("/laws/{path:path}")
async def laws_spa_fallback(path: str):
    return spa_index_response()


@app.get("/values")
async def values_spa():
    return spa_index_response()


@app.get("/about.html")
async def about_page():
    return FileResponse(SPA_DIR / "about.html")



# How often the scheduler re-scrapes SBP's EcoData index. `0` disables the refresh
# entirely, which is what a deployment wants if it would rather the index stay put.
ECODATA_REFRESH_DEFAULT_SECONDS = 3600
# The first scrape waits this long after boot. Not zero: the container has to answer its
# health check before spending a live HTTP round-trip to sbp.org.pk, or a slow scrape
# looks like a slow start and the platform rolls a deploy that was fine.
ECODATA_FIRST_REFRESH_DELAY_SECONDS = 30

# One refresh at a time. The scheduler is the only caller in normal operation, but the
# admin route can trigger one alongside it.
_ECODATA_REFRESH_LOCK = threading.Lock()
_ecodata_stop = threading.Event()

@app.get("/ecodata")
async def ecodata_page():
    return spa_index_response()


@app.get("/healthz")
def healthz():
    """Readiness probe for the platform health check.

    Green when both databases open and the vector store answers. Deliberately does
    **not** probe the LLM provider: chat would degrade during a provider outage, but
    search, browsing and the rest of the corpus keep working, and wiring the provider
    into this endpoint would let someone else's outage roll the container.

    Failures report the exception class, not its message. This endpoint is unauthenticated
    and a SQLAlchemy error carries the database path in its text.
    """
    checks: dict[str, str] = {}
    healthy = True

    for name, factory in (("corpus_db", SessionLocal), ("app_db", AppSessionLocal)):
        session = None
        try:
            session = factory()
            session.execute(text("SELECT 1"))
            checks[name] = "ok"
        except Exception as exc:
            logging.exception("Health check failed for %s", name)
            checks[name] = f"error: {type(exc).__name__}"
            healthy = False
        finally:
            if session is not None:
                session.close()

    # Responding is the check, not holding data: a store that opens but is empty is a
    # deployment that has not been seeded yet, which is a legible state rather than a
    # broken one. It is reported so the distinction is visible.
    try:
        checks["vector_store"] = "ok" if has_vector_store_data() else "ok (empty)"
    except Exception as exc:
        logging.exception("Health check failed for the vector store")
        checks["vector_store"] = f"error: {type(exc).__name__}"
        healthy = False

    return JSONResponse(
        {"status": "ok" if healthy else "unhealthy", "checks": checks},
        status_code=200 if healthy else 503,
    )


@app.get("/api/app/status")
async def get_app_status(db: Session = Depends(get_db)):
    sync_status = _latest_sync_status(db)
    sync_payload = _sync_status_payload(sync_status, _latest_successful_sync(db))
    remote_payload = _remote_circular_check_status()
    sync_payload = {**sync_payload, **remote_payload}
    total_circulars = db.query(func.count(Circular.id)).scalar() or 0
    department_count = db.query(func.count(func.distinct(Circular.department))).filter(Circular.department.isnot(None)).scalar() or 0
    indexed_today = db.query(func.count(Circular.id)).filter(func.date(Circular.indexed_at) == datetime.utcnow().date()).scalar() or 0
    vector_db_ready = has_vector_store_data()

    return {
        "sync_status": sync_payload["status"],
        "live_status": sync_payload["live_status"],
        "sync": sync_payload,
        "total_circulars": total_circulars,
        "department_count": department_count,
        "indexed_today": indexed_today,
        "vector_db_state": "READY" if vector_db_ready else "NOT_INDEXED",
        "last_sync_display": sync_payload["last_sync_display"],
        "last_sync": sync_payload["last_sync"],
        "last_sync_dt": sync_payload["last_sync_dt"],
        "last_sync_raw": sync_payload["last_sync_raw"],
        **remote_payload,
    }


@app.get("/api/llm/status")
async def get_llm_status(user: User = Depends(current_user)):
    """Probe the availability of *this user's* LLM backend.

    Scoped to the caller, not to the deployment. Chat resolves credentials per user and
    refuses to fall back (`get_ai_client_for_user`), so probing the deployment config
    here showed a tester a green light for a backend they will never reach: the badge
    said online, the next chat turn said "add your own API key". Reading the same config
    chat reads is what makes the indicator worth looking at — and it keeps the admin's
    provider and model out of a response every signed-in account can fetch.

    Checked on demand (e.g. on page refresh) rather than on a schedule, since a
    local or free-tier backend can go offline or get rate-limited at any time.
    """
    try:
        client = get_ai_client_for_user(user)
    except MissingUserAIConfig as exc:
        # A state of its own, not an error: nothing is broken and retrying will not help
        # until this user sets a key, so the sidebar sends them to Settings instead of
        # to a network problem they do not have.
        return {
            "available": False,
            "state": "not_configured",
            "detail": str(exc),
            "provider": None,
            "model": None,
        }
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


def _ecodata_refresh_interval_seconds() -> int:
    raw = os.getenv("SBPEYE_ECODATA_REFRESH_SECONDS")
    if raw is None or not raw.strip():
        return ECODATA_REFRESH_DEFAULT_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logging.warning(
            "SBPEYE_ECODATA_REFRESH_SECONDS=%r is not a number; using the %ds default.",
            raw, ECODATA_REFRESH_DEFAULT_SECONDS,
        )
        return ECODATA_REFRESH_DEFAULT_SECONDS


def refresh_ecodata_index(db: Session) -> bool:
    """Re-scrape SBP's EcoData index and record when it happened.

    This writes the corpus. It used to run from whichever `GET /api/ecodata/entries`
    request happened to find the data stale, which meant an arbitrary user's page load
    blocked on a live scrape of sbp.org.pk and wrote the corpus with no identity to
    attribute the write to — the one corpus writer the admin gate in 1.3 could not reach.
    It is now the application refreshing its own scraped index on a schedule, which is a
    thing the application does rather than a thing a user did.

    Returns whether the scrape ran; `False` means another one was already in progress.
    """
    if not _ECODATA_REFRESH_LOCK.acquire(blocking=False):
        return False
    try:
        scrape_ecodata_index(db)
        sync_status = (
            db.query(SyncStatus)
            .filter(circular_sync_only())
            .order_by(SyncStatus.id.desc())
            .first()
        )
        if sync_status:
            sync_status.ecodata_index_time = datetime.now()
        else:
            db.add(SyncStatus(
                last_sync_date=datetime.now(),
                status="success",
                ecodata_index_time=datetime.now(),
            ))
        db.commit()
        return True
    finally:
        _ECODATA_REFRESH_LOCK.release()


def _ecodata_refresh_loop() -> None:
    """The scheduler. Owns every routine EcoData refresh."""
    interval = _ecodata_refresh_interval_seconds()
    if interval <= 0:
        logging.info("EcoData scheduled refresh is disabled.")
        return

    if _ecodata_stop.wait(ECODATA_FIRST_REFRESH_DELAY_SECONDS):
        return
    while True:
        session = SessionLocal()
        try:
            refresh_ecodata_index(session)
        except Exception:
            # A scrape failure must not kill the scheduler: SBP is intermittently
            # unreachable, and the next tick is a perfectly good retry.
            logging.exception("Scheduled EcoData refresh failed")
        finally:
            session.close()
        if _ecodata_stop.wait(interval):
            return


def _get_ecodata_entries(db: Session) -> list[dict]:
    """A pure read. Never scrapes — see `refresh_ecodata_index`."""
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
def get_ecodata_entries(db: Session = Depends(get_db)):
    return _get_ecodata_entries(db)


@app.post("/api/ecodata/refresh", dependencies=[Depends(require_admin)])
def force_ecodata_refresh(db: Session = Depends(get_db)):
    """Re-scrape now instead of waiting for the next scheduled tick.

    Admin-only, because it writes the corpus and calls out to SBP. The scheduler covers
    the routine case; this exists so an admin does not have to wait an hour to see a
    change SBP published five minutes ago.
    """
    ran = refresh_ecodata_index(db)
    if not ran:
        return JSONResponse(
            {"error": "A refresh is already running; try again shortly."},
            status_code=409,
        )
    return {"ok": True, "entries": db.query(EcoDataEntry).count()}


@app.get("/api/ecodata/pdf_summary")
def get_pdf_summary(url: str, db: Session = Depends(get_db)):
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


@app.get("/api/circulars/sync/status")
def get_circular_sync_status(db: Session = Depends(get_db)):
    return _sync_status_payload(_latest_sync_status(db), _latest_successful_sync(db))


@app.post("/api/circulars/sync", dependencies=[Depends(require_admin)])
def start_circular_sync(data: dict | None = Body(default=None), db: Session = Depends(get_db)):
    try:
        options = _sync_options_from_payload(data or {})
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if not _CIRCULAR_SYNC_LOCK.acquire(blocking=False):
        active = _latest_sync_status(db)
        return JSONResponse(
            {
                "error": "A circular sync is already running.",
                "sync": _sync_status_payload(active, _latest_successful_sync(db)),
            },
            status_code=409,
        )

    job = SyncStatus(
        job_id=str(uuid.uuid4()),
        status="queued",
        started_at=datetime.utcnow(),
        parameters=json.dumps(options),
    )
    try:
        db.add(job)
        db.commit()
        db.refresh(job)
        job_id = job.job_id
        response_payload = _sync_status_payload(job, _latest_successful_sync(db))
        db.close()
    except Exception as exc:
        db.rollback()
        _CIRCULAR_SYNC_LOCK.release()
        return JSONResponse({"error": str(exc)}, status_code=500)

    threading.Thread(
        target=_run_circular_sync,
        args=(job_id, options),
        daemon=True,
    ).start()
    return JSONResponse(
        response_payload,
        status_code=202,
    )


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
    source: str = "circulars",
    doc_type: str | None = None,
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db)
):
    """Hybrid search. `source` is circulars (default) | laws | all.

    The default keeps this endpoint circular-only, because law results carry a different
    shape and the SPA has no way to render them yet; a caller that opts in gets mixed
    items discriminated by `result_kind`.
    """
    page = max(page, 1)
    per_page = min(max(per_page, 1), 100)
    offset = (page - 1) * per_page
    try:
        results, total = search_engine.search(
            q, db,
            offset=offset,
            limit=per_page,
            start_year=_parse_year(start_year),
            end_year=_parse_year(end_year),
            department=department,
            sort_by=sort_by,
            tag=tag,
            source=source,
            doc_type=doc_type,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    total_pages = max(1, (total + per_page - 1) // per_page)
    return {
        "items": [
            _law_summary(r["law"], r.get("snippet"))
            if r.get("result_kind") == "law"
            else _circular_summary(
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


@app.post(
    "/api/circulars/open",
    dependencies=[Depends(admin_only(
        "Indexing a new circular from a link is limited to administrators on this "
        "deployment, because it writes to the shared corpus. Ask an admin to add it."
    ))],
)
def open_circular_by_url(
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
            indexed_at=datetime.utcnow(),
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


@app.post("/api/circulars/{circular_id}/refresh", dependencies=[Depends(require_admin)])
def refresh_circular(
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
def get_circular_source(circular_id: str, db: Session = Depends(get_db)):
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
def circular_document(circular_id: str, db: Session = Depends(get_db)):
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
                source = {"id": sc.id, "title": sc.title, "reference": sc.reference, "url": sc.url, "status": sc.status or "active", "date": sc.date.strftime("%Y-%m-%d") if sc.date else None}
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active", "date": tc.date.strftime("%Y-%m-%d") if tc.date else None}
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
        # The laws & regulations this circular cites. `relationships` above is
        # circular↔circular; this is the other half of the graph.
        "regulations": _circular_regulations(c),
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

    subject = circular_subject(circular)
    return StreamingResponse(
        build_checklist_workbook(subject, checklist),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{subject.safe_filename()}"'
        },
    )


def _entity_dict(e: CircularEntity, *, include_circular: bool = False) -> dict:
    payload = {
        "id": e.id,
        "subject_kind": e.subject_kind or "circular",
        "circular_id": e.circular_id,
        "document_id": e.document_id,
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
    # The law arm's equivalent. Named `document` rather than folded into `circular` so a
    # consumer cannot mistake a regulation for a circular by reading one field.
    if include_circular and e.document is not None:
        d = e.document
        payload["document"] = {
            "id": d.id,
            "title": d.title,
            "display_title": split_law_title(d.title)[0],
            "doc_type": d.doc_type,
            "part_label": d.part_label,
            "parent_title": (
                split_law_title(d.parent.title)[0] if d.parent is not None else None
            ),
            # Whether the edition this value came from is still the one in force.
            "in_force": bool(e.version is not None and e.version.is_current),
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
    source: str = "all",
    page: int = 1,
    per_page: int = 50,
    db: Session = Depends(get_db),
):
    """Structured query over extracted regulatory values. Examples:
    ?unit=%&comparator=min&min_value=10 -> thresholds above 10%;
    ?metric=Paid-up Capital&subject=MFB&current_only=true -> the current MFB minimum capital.

    `source` selects the corpus: circulars | laws | all (default). It defaults to `all`
    because the values people look for — CAR, MCR, LTV ceilings — are stated in the
    Prudential Regulations and only moved by circulars.
    """
    # Outer joins, not inner: a law-sourced value has no circular, and the inner join this
    # replaced would have silently dropped every one of them from the browser.
    query = (
        db.query(CircularEntity)
        .outerjoin(Circular, CircularEntity.circular_id == Circular.id)
        .outerjoin(RegDocument, CircularEntity.document_id == RegDocument.id)
        .outerjoin(
            RegDocumentVersion, CircularEntity.version_id == RegDocumentVersion.id
        )
    )
    if source == "circulars":
        query = query.filter(CircularEntity.subject_kind == "circular")
    elif source == "laws":
        query = query.filter(CircularEntity.subject_kind == "law")
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
        # A department is a circular's attribute, so this narrows to circulars by nature.
        query = query.filter(Circular.department.ilike(f"%{department}%"))
    if min_value is not None:
        query = query.filter(CircularEntity.value_numeric >= min_value)
    if max_value is not None:
        query = query.filter(CircularEntity.value_numeric <= max_value)
    if current_only:
        # "Current" means something different per corpus: a circular is superseded by
        # another circular, while a law's value ages out when SBP replaces the edition
        # that stated it. Both arms have to be expressed or one corpus filters to nothing.
        query = query.filter(
            or_(
                and_(
                    CircularEntity.subject_kind == "circular",
                    ~Circular.status.in_(("superseded", "cancelled")),
                ),
                and_(
                    CircularEntity.subject_kind == "law",
                    RegDocumentVersion.is_current == 1,
                ),
            )
        )

    # Most recent first, by the value's effective date, then by the date of whatever
    # stated it — a circular's publication date, or when we first captured the edition.
    rows = query.order_by(
        CircularEntity.effective_date.desc().nullslast(),
        func.coalesce(Circular.date, RegDocumentVersion.first_seen_at).desc().nullslast(),
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


@app.post("/api/circulars/{circular_id}/generate", dependencies=[Depends(require_admin)])
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
        target_kind="circular",
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
                source = {"id": sc.id, "title": sc.title, "reference": sc.reference, "url": sc.url, "status": sc.status or "active", "date": sc.date.strftime("%Y-%m-%d") if sc.date else None}
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active", "date": tc.date.strftime("%Y-%m-%d") if tc.date else None}
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


@app.get("/api/circulars/{circular_id}/consolidation")
async def get_circular_consolidation(circular_id: str, db: Session = Depends(get_db)):
    """The consolidated requirement view of the circular's amendment chain.

    Shared by every chain member: any circular connected through resolved
    `amends` relationships returns the same chain and stored consolidation."""
    from .consolidation import consolidation_payload

    circular = db.query(Circular).filter(Circular.id == circular_id).first()
    if not circular:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    return consolidation_payload(db, circular)


@app.get("/api/sbp_news")
def get_sbp_news(db: Session = Depends(get_db)):
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
    request: Request = None,
    db: Session = Depends(get_db),
):
    # Reading a document is open to everyone; re-ingesting one is not. `refresh=True`
    # runs the full pipeline, rewriting `content_text` and resetting `is_vectorized`
    # (3.5.2), which is a corpus write and so belongs to the admin under 1.3.
    if refresh:
        actor = getattr(getattr(request, "state", None), "user", None)
        if actor is None or not actor.is_admin:
            return JSONResponse(
                {"error": "Re-fetching a document rewrites the shared corpus and is "
                          "limited to administrators on this deployment."},
                status_code=403,
            )

    attachment = db.query(Attachment).filter(Attachment.id == id).first() if id else None
    standalone = db.query(CachedDocument).filter(CachedDocument.id == id).first() if id and not attachment else None
    if standalone:
        standalone, _ = _ensure_document_cached(db, standalone, refresh=refresh)
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
        cached_document, _ = _ensure_document_cached(db, cached_document, refresh=refresh)
        payload = _document_payload(cached_document)
        if not payload["cached"]:
            return JSONResponse(payload, status_code=502)
        return payload

    if not attachment:
        attachment = process_attachment(db, circular, info, force_download=refresh)
    else:
        attachment, _ = _ensure_document_cached(db, attachment, refresh=refresh)

    payload = _document_payload(attachment)
    if not payload["cached"]:
        return JSONResponse(payload, status_code=502)
    return payload


@app.get("/api/documents/{attachment_id}/content")
async def document_content(attachment_id: str, db: Session = Depends(get_db)):
    attachment = db.query(Attachment).filter(Attachment.id == attachment_id).first()
    if not attachment:
        attachment = db.query(CachedDocument).filter(CachedDocument.id == attachment_id).first()
    if not attachment:
        return JSONResponse({"error": "Cached document not found."}, status_code=404)
    attachment, path = _ensure_document_cached(db, attachment)
    if path is None:
        error = (
            getattr(attachment, "extraction_error", None)
            or getattr(attachment, "error", None)
            or "Cached document not found."
        )
        return JSONResponse({"error": error}, status_code=502)
    file_type = (attachment.file_type or "").lower()
    disposition = "inline" if file_type == "pdf" else "attachment"
    media_type = "application/pdf" if file_type == "pdf" else "application/octet-stream"
    return FileResponse(
        path,
        media_type=media_type,
        filename=Path(attachment.filename).name,
        content_disposition_type=disposition,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/laws")
def list_laws(
    q: str = "",
    doc_type: str | None = None,
    parent_id: str | None = None,
    top_level: bool = False,
    include_delisted: bool = False,
    sort_by: str = "title",
    page: int = 1,
    per_page: int = 20,
    db: Session = Depends(get_db),
):
    """List laws & regulations, or search them.

    With `q` this runs the hybrid engine against the law corpus, ordered by relevance;
    `sort_by` applies to the plain listing only. `top_level` hides the parts of container
    documents (FE Manual chapters and the like), which are documents in their own right
    but noise in a flat list.

    `sort_by=captured` orders by when we first saw the edition now in force, newest first
    — the "what moved recently" view, and the one thing SBP's own site cannot answer,
    since it replaces files in place and keeps no history.
    """
    page = max(page, 1)
    per_page = min(max(per_page, 1), 100)
    offset = (page - 1) * per_page

    if q.strip():
        results, total = search_engine.search(
            q, db, offset=offset, limit=per_page, source="laws", doc_type=doc_type,
        )
        items = [_law_summary(item["law"], item.get("snippet")) for item in results]
        return {"items": items, "total": total, "page": page, "per_page": per_page}

    query = db.query(RegDocument)
    if doc_type and doc_type.strip():
        query = query.filter(RegDocument.doc_type == doc_type.strip())
    if parent_id:
        query = query.filter(RegDocument.parent_id == parent_id)
    elif top_level:
        query = query.filter(RegDocument.parent_id.is_(None))
    if not include_delisted:
        query = query.filter(RegDocument.delisted_at.is_(None))

    total = query.count()

    if sort_by == "captured":
        # Outer join, not inner: the 21 documents with no version at all (stubs, external
        # laws, dead links) must still appear, and they sort to the end. `is_current` is
        # unique per document, so this cannot multiply rows.
        current = aliased(RegDocumentVersion)
        query = query.outerjoin(
            current,
            and_(
                current.document_id == RegDocument.id,
                current.is_current == 1,
            ),
        ).order_by(
            # Explicit nulls-last, rather than NULLS LAST, so the ordering does not
            # depend on the SQLite version underneath.
            current.first_seen_at.is_(None),
            current.first_seen_at.desc(),
            RegDocument.title,
        )
    else:
        query = query.order_by(RegDocument.doc_type, RegDocument.title)

    documents = query.offset(offset).limit(per_page).all()
    return {
        "items": [_law_summary(document) for document in documents],
        "total": total,
        "page": page,
        "per_page": per_page,
    }


@app.get("/api/laws/types")
def list_law_types(db: Session = Depends(get_db)):
    """Document-type facets with counts, for filter UI."""
    rows = (
        db.query(RegDocument.doc_type, func.count(RegDocument.id))
        .filter(RegDocument.delisted_at.is_(None))
        .group_by(RegDocument.doc_type)
        .all()
    )
    return [
        {"doc_type": doc_type, "count": count}
        for doc_type, count in sorted(rows, key=lambda row: -row[1])
        if doc_type
    ]


@app.get("/api/laws/{document_id}")
def get_law(document_id: str, db: Session = Depends(get_db)):
    """One document: what is in force, its version timeline, parts, linked circulars."""
    document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
    if document is None:
        return JSONResponse({"error": "Document not found"}, status_code=404)
    return _law_detail(document)


@app.post("/api/laws/{document_id}/generate", dependencies=[Depends(require_admin)])
async def generate_law_intelligence(
    document_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Queue AI analysis for one law/regulation. Mirrors the circular endpoint."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"error": "A JSON request body is required."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"error": "The request body must be a JSON object."}, status_code=400)

    feature = str(data.get("feature", "")).lower().strip()
    if feature not in LAW_GENERATION_ACTIONS:
        return JSONResponse(
            {"error": f"Feature must be one of: {', '.join(LAW_GENERATION_ACTIONS)}."},
            status_code=400,
        )

    document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
    if not document:
        return JSONResponse({"error": "Document not found"}, status_code=404)

    # A container holds no wording, but it is not un-analysable: its summary is a rollup
    # over its parts. Only the features that can be built that way are offered.
    if is_container(document):
        if feature != "all" and feature not in CONTAINER_FEATURES:
            return JSONResponse(
                {
                    "error": (
                        f"This is a collection with no text of its own, so it cannot "
                        f"produce a {feature}. Analyse its parts individually."
                    ),
                    "reason": GAP_MANIFEST,
                    "structural": True,
                },
                status_code=422,
            )
        if feature in ("summary", "all") and not rollup_sources(document):
            return JSONResponse(
                {
                    "error": (
                        "None of this collection's parts have been summarised yet. "
                        "Analyse the parts first, then roll them up."
                    ),
                    "reason": GAP_MANIFEST,
                    # Recoverable: summarise the parts and this becomes possible.
                    "structural": False,
                },
                status_code=422,
            )
    else:
        # 33 of the 133 documents in the corpus have nothing analysable, for six different
        # reasons. `law_corpus` knows which, and `gap_message` turns that into a sentence —
        # "this is a collection, analyse its parts" is actionable where "no content" is not.
        documents, gaps = law_corpus(document)
        if not documents:
            return JSONResponse(
                {
                    "error": gap_message(gaps),
                    "reason": gaps[0]["reason"] if gaps else None,
                    "structural": bool(gaps and gaps[0]["reason"] in STRUCTURAL_GAPS),
                },
                status_code=422,
            )

    active_job = db.query(AIGenerationJob).filter(
        AIGenerationJob.document_id == document_id,
        AIGenerationJob.status.in_(("queued", "running")),
    ).order_by(AIGenerationJob.created_at.desc()).first()
    if active_job:
        return JSONResponse(
            {
                "error": "Generation is already in progress for this document.",
                "job": generation_job_payload(active_job),
            },
            status_code=409,
        )

    job = AIGenerationJob(
        id=str(uuid.uuid4()),
        target_kind="law",
        document_id=document_id,
        feature=feature,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    background_tasks.add_task(run_law_generation_job, job.id)
    return JSONResponse(generation_job_payload(job), status_code=202)


@app.get("/api/laws/{document_id}/versions/{version_id}")
def get_law_version(document_id: str, version_id: str, db: Session = Depends(get_db)):
    """One captured version, including its extracted text and archived-file reference.

    The archive copy is exposed as a `/api/documents/open`-style local path rather than
    the SBP URL: the point of the archive is that SBP's copy may already be gone.
    """
    version = (
        db.query(RegDocumentVersion)
        .filter(
            RegDocumentVersion.id == version_id,
            RegDocumentVersion.document_id == document_id,
        )
        .first()
    )
    if version is None:
        return JSONResponse({"error": "Version not found"}, status_code=404)

    payload = _law_version_payload(version, include_text=True)
    payload["document"] = {
        "id": version.document.id,
        "title": version.document.title,
        "doc_type": version.document.doc_type,
        "part_label": version.document.part_label,
    }
    # A pure read: reports what is on disk and never fetches. Downloading belongs to
    # `/file`, where the caller has actually asked for the bytes.
    payload["archive_path"] = None
    candidate = _law_archive_path(version)
    if candidate is not None:
        payload["archive_path"] = version.local_path
        payload["archive_size"] = candidate.stat().st_size
    return payload


@app.get("/api/laws/{document_id}/checklist.xlsx")
def export_law_checklist(document_id: str, db: Session = Depends(get_db)):
    """The obligations checklist for a regulation, as a workbook.

    Exports the edition in force. A checklist stored against a superseded edition is
    still on disk but is not what this document requires today, so it is never served —
    the same rule the detail payload follows.
    """
    document = db.query(RegDocument).filter(RegDocument.id == document_id).first()
    if document is None:
        return JSONResponse({"error": "Document not found"}, status_code=404)

    version = document.current_version
    checklist = _safe_json_object(version.compliance_checklist) if version else None
    if not checklist:
        return JSONResponse(
            {"error": "This document does not have a generated checklist"},
            status_code=404,
        )

    subject = law_subject(document, version)
    return StreamingResponse(
        build_checklist_workbook(subject, checklist),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{subject.safe_filename()}"'
        },
    )


@app.get("/api/laws/{document_id}/file")
def get_law_file(document_id: str, version_id: str | None = None, db: Session = Depends(get_db)):
    """Serve a version's archived file from disk (defaults to the version in force)."""
    query = db.query(RegDocumentVersion).filter(
        RegDocumentVersion.document_id == document_id
    )
    if version_id:
        version = query.filter(RegDocumentVersion.id == version_id).first()
    else:
        version = query.filter(RegDocumentVersion.is_current == 1).first()
    if version is None:
        return JSONResponse({"error": "No archived file for this version"}, status_code=404)

    candidate, cache_error = _ensure_law_version_cached(db, version)
    if candidate is None:
        return JSONResponse(
            {"error": cache_error or "Archived file is missing"}, status_code=404
        )
    # `inline`, not the default `attachment`: the reader renders this in an iframe, and an
    # attachment disposition makes the browser download it instead of showing it. The
    # filename is kept so saving it from the viewer still gets a meaningful name.
    return FileResponse(
        candidate, filename=candidate.name, content_disposition_type="inline"
    )


@app.get("/api/pdf_preview")
def pdf_preview(url: str):
    import io
    import base64
    import pdfplumber

    if not _is_allowed_sbp_url(url):
        return {"error": "Only SBP PDFs are supported."}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
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
def pdf_proxy(url: str):
    if not _is_allowed_sbp_url(url):
        return JSONResponse({"error": "Only SBP PDFs are supported."}, status_code=400)

    if not urlparse(url).path.lower().endswith(".pdf"):
        return JSONResponse({"error": "Only PDF files are supported."}, status_code=400)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
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


# Served to any signed-in user, unlike `/debug`, which is gated at the page. The console
# itself renders an explanation for a non-admin and every API it calls is admin-gated, so
# the worst a tester who types the URL sees is that explanation — which beats a raw 403
# body from a page they navigated to.
@app.get("/admin")
async def admin_page():
    return spa_index_response()


# The console's tabs are child routes (`/admin/corpus`, `/admin/index`, …), so they have
# to survive a reload and a pasted link, not only in-app navigation. Registered after the
# literal `/admin` above, and it cannot shadow the admin API: that lives under
# `/api/admin/…`, a different prefix entirely.
@app.get("/admin/{path:path}")
async def admin_spa_fallback(path: str):
    return spa_index_response()


@app.get("/debug", dependencies=[Depends(require_admin)])
async def debug_page():
    return spa_index_response()


@app.get("/api/settings", dependencies=[Depends(require_admin)])
async def get_settings(db: Session = Depends(get_app_db)):
    config = AIConfig.from_db(db) or AIConfig.from_env()
    embedding = EmbeddingConfig.from_db(db)
    return _settings_payload(config, embedding, db)


@app.post("/api/settings/adopt-my-provider", dependencies=[Depends(require_admin)])
def adopt_my_provider(request: Request, db: Session = Depends(get_app_db)):
    """Copy the calling admin's own provider settings into the deployment configuration.

    An admin otherwise types the same credentials twice: once under Settings for their
    chat, once here for corpus generation. The two genuinely are separate — generation
    runs in background threads with no user attached, and tying it to one account would
    break the moment that account was deleted — but "separate" should not mean "entered
    twice".

    Copied server-side rather than in the browser because the personal key is write-only
    through the API: `GET /api/settings/ai` reports whether one is stored, never its
    value. A client-side copy would have to send the key back out to do it.

    It is a copy, not a link. Changing your personal key afterwards leaves the deployment
    on the old one, which is the safe direction: corpus generation keeps working when an
    admin rotates their own credentials or leaves.
    """
    from .auth import decrypt_secret

    user = request.state.user
    chosen = (getattr(user, "ai_provider", "") or "").strip()
    if not chosen:
        return JSONResponse(
            {"error": "Set your own AI provider under Settings first, then copy it here."},
            status_code=400,
        )

    provider = normalize_provider(chosen)
    definition = get_provider_definition(provider)
    api_key = decrypt_secret(getattr(user, "ai_api_key_encrypted", None))
    if not api_key and not definition.default_api_key:
        return JSONResponse(
            {"error": "Your provider settings have no API key stored to copy."},
            status_code=400,
        )

    _save_ai_secret(provider=provider, api_key=api_key or None, clear_secret=False)

    existing = AIConfig.from_db(db)
    config = AIConfig(
        provider=provider,
        base_url=(user.ai_base_url or definition.default_base_url),
        api_key="",
        model=(user.ai_model or definition.default_model),
        chat_model=(user.ai_chat_model or ""),
        # Carried over rather than re-detected: detection calls the provider, and this
        # route should not fail because the vendor is briefly unreachable.
        max_context_tokens=(existing.max_context_tokens if existing else 4000),
    )
    config.api_key, _ = get_provider_api_key(provider)
    config.save_to_db(db)
    logging.warning("%s copied their provider settings to the deployment", user.email)
    return {"ok": True, "provider": provider}


@app.post("/api/settings", dependencies=[Depends(require_admin)])
def save_settings(data: dict = Body(...), db: Session = Depends(get_app_db)):
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
    if "llm_debug_enabled" in data:
        upsert_settings(db, {
            "llm_debug_enabled": (
                "true" if bool(data.get("llm_debug_enabled")) else "false"
            )
        })
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
        "settings": _settings_payload(config, embedding, db),
        "context_window_detected": detected_context_window is not None,
    }


@app.post("/api/settings/models", dependencies=[Depends(require_admin)])
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


@app.post("/api/settings/embeddings/test", dependencies=[Depends(require_admin)])
def test_embedding_connection(db: Session = Depends(get_app_db)):
    try:
        config = EmbeddingConfig.from_db(db)
        backend = create_embedding_backend(config)
        embedding = backend.embed_queries(["SBP monetary policy"])
        return {"success": True, "dimensions": len(embedding[0]), "provider": config.provider}
    except Exception as e:
        return {"success": False, "error": str(e)}


@app.post("/api/settings/test", dependencies=[Depends(require_admin)])
def test_ai_connection(db: Session = Depends(get_db)):
    try:
        client = get_ai_client(db)
        with trace_operation(
            "settings.connection_test", "connection_test",
            provider=client.config.provider, model=client.config.model,
        ):
            result = client.test_connection()
            return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Research Workspaces ---

@app.get("/api/workspaces")
async def list_research_workspaces(
    db: Session = Depends(get_db), app_db: Session = Depends(get_app_db)
):
    _ensure_default_workspace(app_db)
    workspaces = app_db.query(ResearchWorkspace).order_by(
        ResearchWorkspace.is_default.desc(),
        ResearchWorkspace.created_at.asc(),
        ResearchWorkspace.id.asc(),
    ).all()
    # Loaded even though the circulars themselves are not serialized here: the pin ids
    # and count are filtered against the corpus, so an empty map would report every
    # workspace as holding nothing.
    circulars = _load_workspace_circulars(db, *workspaces)
    return [
        _workspace_payload(workspace, circulars, include_circulars=False)
        for workspace in workspaces
    ]


@app.get("/api/workspaces/default")
async def get_default_research_workspace(
    db: Session = Depends(get_db), app_db: Session = Depends(get_app_db)
):
    workspace = _ensure_default_workspace(app_db)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


@app.post("/api/workspaces")
async def create_research_workspace(
    request: Request,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
):
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
    app_db.add(workspace)
    app_db.commit()
    app_db.refresh(workspace)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


@app.get("/api/workspaces/{workspace_id}")
async def get_research_workspace(
    workspace_id: str,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
):
    if workspace_id == DEFAULT_WORKSPACE_ID:
        workspace = _ensure_default_workspace(app_db)
        return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))

    workspace = app_db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


@app.patch("/api/workspaces/{workspace_id}")
async def update_research_workspace(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
):
    if workspace_id == DEFAULT_WORKSPACE_ID:
        _ensure_default_workspace(app_db)

    workspace = app_db.query(ResearchWorkspace).filter(
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
    app_db.commit()
    app_db.refresh(workspace)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


@app.delete("/api/workspaces/{workspace_id}")
async def delete_research_workspace(workspace_id: str, app_db: Session = Depends(get_app_db)):
    workspace = app_db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    if workspace.is_default:
        return JSONResponse({"error": "Default workspace cannot be deleted"}, status_code=400)
    doomed = [
        session.id
        for session in app_db.query(ChatSession).filter(
            ChatSession.id.startswith(WORKSPACE_CHAT_SESSION_PREFIX)
        ).all()
        if _workspace_id_from_chat_session(session.id) == workspace.id
    ]
    if doomed:
        app_db.query(ChatMessage).filter(
            ChatMessage.session_id.in_(doomed)
        ).delete(synchronize_session=False)
        app_db.query(ChatSession).filter(
            ChatSession.id.in_(doomed)
        ).delete(synchronize_session=False)
    app_db.delete(workspace)
    app_db.commit()
    return {"success": True}


@app.post("/api/workspaces/{workspace_id}/circulars")
async def pin_workspace_circular(
    workspace_id: str,
    request: Request,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
):
    workspace = app_db.query(ResearchWorkspace).filter(
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

    link = app_db.query(WorkspaceCircular).filter(
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
        app_db.add(link)
    link.last_viewed_at = datetime.utcnow()
    workspace.updated_at = datetime.utcnow()
    app_db.commit()
    app_db.refresh(workspace)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


@app.delete("/api/workspaces/{workspace_id}/circulars/{circular_id}")
async def unpin_workspace_circular(
    workspace_id: str,
    circular_id: str,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
):
    workspace = app_db.query(ResearchWorkspace).filter(
        ResearchWorkspace.id == workspace_id
    ).first()
    if not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    link = app_db.query(WorkspaceCircular).filter(
        WorkspaceCircular.workspace_id == workspace_id,
        WorkspaceCircular.circular_id == circular_id,
    ).first()
    if link:
        app_db.delete(link)
        if workspace.last_circular_id == circular_id:
            workspace.last_circular_id = None
        workspace.updated_at = datetime.utcnow()
        app_db.commit()
    app_db.refresh(workspace)
    return _workspace_payload(workspace, _load_workspace_circulars(db, workspace))


# --- Chat Feature ---

@app.get("/chat")
async def chat_page():
    return spa_index_response()


@app.get("/chat/{path:path}")
async def chat_spa_fallback(path: str):
    """Conversations are addressable (/chat/<session id>), so a reload or a
    pasted link has to land on the SPA rather than a 404."""
    return spa_index_response()


@app.get("/api/chat/sessions")
async def list_chat_sessions(
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    _ensure_default_workspace(app_db)
    workspaces = app_db.query(ResearchWorkspace).order_by(
        ResearchWorkspace.is_default.desc(),
        func.coalesce(ResearchWorkspace.updated_at, ResearchWorkspace.created_at).desc()
    ).all()
    circulars = _load_workspace_circulars(db, *workspaces)
    # Workspaces are shared, so every user sees the same list of them; the conversation
    # inside each one is their own, which is why the id carries the owner.
    workspace_session_ids = [
        _workspace_chat_session_id(workspace.id, user.id) for workspace in workspaces
    ]
    workspace_sessions = app_db.query(ChatSession).filter(
        ChatSession.id.in_(workspace_session_ids),
        ChatSession.user_id == user.id,
    ).all() if workspace_session_ids else []
    workspace_session_by_id = {
        session.id: session for session in workspace_sessions
    }
    sessions = app_db.query(ChatSession).order_by(
        func.coalesce(ChatSession.updated_at, ChatSession.created_at).desc()
    ).filter(
        ChatSession.user_id == user.id,
        ~ChatSession.id.in_(workspace_session_ids),
    ).limit(50).all()
    return [
        *[
            _workspace_chat_session_payload(
                workspace,
                circulars,
                workspace_session_by_id.get(
                    _workspace_chat_session_id(workspace.id, user.id)
                ),
                user_id=user.id,
            )
            for workspace in workspaces
        ],
        *[_chat_session_payload(session) for session in sessions],
    ]


@app.get("/api/chat/sessions/{session_id}")
async def get_chat_session(
    session_id: str,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    workspace = _get_workspace_for_chat_session(app_db, session_id)
    if workspace:
        circulars = _load_workspace_circulars(db, workspace)
        session = _owned_chat_session(app_db, session_id, user)
        messages = _ordered_chat_messages(app_db, session_id) if session else []
        return {
            **_workspace_chat_session_payload(
                workspace, circulars, session, user_id=user.id
            ),
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
            "circulars": _workspace_circular_summaries(workspace, circulars),
        }
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session = _owned_chat_session(app_db, session_id, user)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    messages = app_db.query(ChatMessage).filter(
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


@app.get("/api/chat/sessions/{session_id}/export.md")
async def export_chat_session(
    session_id: str,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    """The whole conversation as one markdown file, both sides, citations linked to SBP.

    Server-side because only the database knows where a cited document lives on
    sbp.org.pk; the chat view has the labels but not the URLs behind them.
    """
    workspace = _get_workspace_for_chat_session(app_db, session_id)
    if workspace:
        title = workspace.name
    elif _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    else:
        session = _owned_chat_session(app_db, session_id, user)
        if not session:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        title = session.title

    # The corpus session: the citations being resolved are circulars and law versions.
    markdown = render_session_markdown(
        db, title, _ordered_chat_messages(app_db, session_id)
    )
    return StreamingResponse(
        iter([markdown]),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{session_filename(title)}"'
        },
    )


@app.patch("/api/chat/sessions/{session_id}")
async def rename_chat_session(
    session_id: str,
    request: Request,
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace chat sessions use the workspace name"}, status_code=400)

    session = _owned_chat_session(app_db, session_id, user)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    data = await request.json()
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return JSONResponse({"error": "Title cannot be empty"}, status_code=400)

    session.title = title.strip()[:120]
    session.updated_at = datetime.utcnow()
    app_db.commit()
    return {"id": session.id, "title": session.title, "updated_at": _isoformat(session.updated_at)}


@app.delete("/api/chat/sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    workspace = _get_workspace_for_chat_session(app_db, session_id)
    if workspace:
        app_db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
        app_db.query(ChatSession).filter(
            ChatSession.id == session_id, ChatSession.user_id == user.id
        ).delete()
        if workspace.is_default:
            app_db.query(WorkspaceCircular).filter(
                WorkspaceCircular.workspace_id == workspace.id
            ).delete()
            workspace.last_circular_id = None
            workspace.updated_at = datetime.utcnow()
        else:
            app_db.delete(workspace)
        app_db.commit()
        return {"success": True}
    if _workspace_id_from_chat_session(session_id):
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session = _owned_chat_session(app_db, session_id, user)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    app_db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
    app_db.delete(session)
    app_db.commit()
    return {"success": True}


def _owned_chat_session(
    app_db: Session, session_id: str, user: User
) -> ChatSession | None:
    """A chat session, but only if it belongs to this user.

    Every route that loads a session by id goes through here. Filtering by owner at each
    call site instead would work right up until someone adds the eleventh route and
    forgets, and the failure mode of that omission is one tester reading another's
    conversation.

    Returns None for both "no such session" and "not yours", which the routes turn into
    the same 404. A 403 would confirm the id exists.
    """
    return (
        app_db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )


def _ordered_chat_messages(app_db: Session, session_id: str) -> list[ChatMessage]:
    """Chat messages live in the application database — `db` here would be the corpus."""
    return app_db.query(ChatMessage).filter(
        ChatMessage.session_id == session_id
    ).order_by(ChatMessage.created_at, ChatMessage.id).all()


def _truncate_chat_messages(
    app_db: Session,
    session_id: str,
    message_id: str,
    *,
    include_message: bool,
) -> ChatMessage | None:
    messages = _ordered_chat_messages(app_db, session_id)
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
        app_db.query(ChatMessage).filter(ChatMessage.id.in_(delete_ids)).delete(
            synchronize_session=False
        )
    return target


@app.delete("/api/chat/sessions/{session_id}/messages/{message_id}")
async def truncate_chat_session(
    session_id: str,
    message_id: str,
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    session = _owned_chat_session(app_db, session_id, user)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    if not _truncate_chat_messages(
        app_db, session_id, message_id, include_message=True
    ):
        return JSONResponse({"error": "Message not found"}, status_code=404)

    session.updated_at = datetime.utcnow()
    app_db.commit()
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


def _chat_turn_circular_ids(
    db: Session,
    circular_ids: list[str],
    message: str,
) -> list[str]:
    """Add referenced or freshness-matched circulars without pinning them."""
    from .chat_retrieval import query_context_circular_ids

    inferred_ids = query_context_circular_ids(db, message)
    return list(dict.fromkeys([
        *inferred_ids,
        *circular_ids,
    ]))


def get_or_create_chat_session(
    app_db, session_id, message, circular_ids, workspace, user, workspace_circulars=None
):
    """Resolve (and persist) the ChatSession for a chat turn.

    Returns ``(session, session_id, circular_ids)``. A new id is minted when none was
    supplied or the referenced session is missing; workspace sessions always adopt the
    workspace name and its pinned circulars as the authoritative selection.

    ``workspace_circulars`` is the corpus lookup from ``_load_workspace_circulars``,
    needed only on the workspace path; sessions live in the application database, so
    this function never holds a corpus session of its own.
    """
    if workspace:
        circular_ids = _workspace_circular_ids(workspace, workspace_circulars or {})
        session = _owned_chat_session(app_db, session_id, user)
        if not session:
            session = ChatSession(
                id=session_id,
                user_id=user.id,
                title=workspace.name,
                circular_ids=json.dumps(circular_ids),
            )
            app_db.add(session)
        else:
            session.title = workspace.name
    elif not session_id:
        session_id = str(uuid.uuid4())
        session = ChatSession(
            id=session_id,
            user_id=user.id,
            title=message[:80],
            circular_ids=json.dumps(circular_ids),
        )
        app_db.add(session)
    else:
        session = _owned_chat_session(app_db, session_id, user)
        if not session:
            session_id = str(uuid.uuid4())
            session = ChatSession(
                id=session_id,
                user_id=user.id,
                title=message[:80],
                circular_ids=json.dumps(circular_ids),
            )
            app_db.add(session)
    session.circular_ids = json.dumps(circular_ids)
    session.updated_at = datetime.utcnow()
    return session, session_id, circular_ids


@app.post("/api/chat")
async def chat_message(
    request: Request,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")
    workspace = _get_workspace_for_chat_session(app_db, session_id)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)
    if _workspace_id_from_chat_session(session_id) and not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    session, session_id, circular_ids = get_or_create_chat_session(
        app_db, session_id, message, circular_ids, workspace, user,
        _load_workspace_circulars(db, workspace) if workspace else None,
    )
    turn_circular_ids = _chat_turn_circular_ids(db, circular_ids, message)

    user_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=message,
        circular_ids=json.dumps(circular_ids) if circular_ids else None,
    )
    app_db.add(user_msg)
    app_db.commit()

    trace_kwargs = {
        "chat_session_id": session_id,
        "target_kind": "chat_session",
        "target_id": session_id,
        "metadata": {
            "user_message_id": user_msg.id,
            "selected_circular_ids": turn_circular_ids,
            "workspace_id": workspace.id if workspace else None,
        },
    }
    try:
        with trace_operation("chat.turn", "web_chat", **trace_kwargs):
            emit_event("context", {
                "user_message": message,
                "selected_circular_ids": turn_circular_ids,
            }, stage="chat.context")
            messages = _ordered_chat_messages(app_db, session_id)
            chat_messages = [{"role": m.role, "content": m.content} for m in messages]
            client = get_ai_client_for_user(user)
            circulars_context = _build_chat_circulars_context(
                db, turn_circular_ids, message, client.config.max_context_tokens
            )
            response_text = client.chat(
                chat_messages, db, circulars_context=circulars_context,
                selected_circular_ids=turn_circular_ids,
            )
            emit_event("normalized_result", {"response": response_text}, stage="chat.result")
            assistant_msg = ChatMessage(
                id=str(uuid.uuid4()), session_id=session_id,
                role="assistant", content=response_text,
            )
            app_db.add(assistant_msg)
            session.updated_at = datetime.utcnow()
            app_db.commit()
            emit_event("persisted_result", {
                "persisted": True, "message_id": assistant_msg.id,
                "chat_session_id": session_id,
            }, stage="chat.persist")
    except Exception as exc:
        response_text = friendly_chat_error(exc)
        assistant_msg = ChatMessage(
            id=str(uuid.uuid4()), session_id=session_id,
            role="assistant", content=response_text,
        )
        app_db.add(assistant_msg)
        session.updated_at = datetime.utcnow()
        app_db.commit()

    return {"response": response_text, "session_id": session_id}


@app.post("/api/chat/stream")
async def chat_message_stream(
    request: Request,
    db: Session = Depends(get_db),
    app_db: Session = Depends(get_app_db),
    user: User = Depends(current_user),
):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = _normalize_circular_ids(data.get("circular_ids", []))
    session_id = data.get("session_id")
    replace_message_id = data.get("replace_message_id")
    workspace = _get_workspace_for_chat_session(app_db, session_id)

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)
    if _workspace_id_from_chat_session(session_id) and not workspace:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    if replace_message_id and not session_id:
        return JSONResponse(
            {"error": "A session is required to replace a message"}, status_code=400
        )

    session, session_id, circular_ids = get_or_create_chat_session(
        app_db, session_id, message, circular_ids, workspace, user,
        _load_workspace_circulars(db, workspace) if workspace else None,
    )
    turn_circular_ids = _chat_turn_circular_ids(db, circular_ids, message)

    if replace_message_id:
        user_msg = _truncate_chat_messages(
            app_db, session_id, replace_message_id, include_message=False
        )
        if not user_msg or user_msg.role != "user":
            app_db.rollback()
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
        app_db.add(user_msg)
    app_db.commit()

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    def stream_response():
        # The request's own sessions are closed when this handler returns, before the
        # generator runs, so the stream opens its own pair: the corpus for retrieval,
        # the application database for the messages it writes.
        stream_db = SessionLocal()
        stream_app_db = AppSessionLocal()
        # Text streamed since the last tool call. A model that is about to call a
        # tool narrates first ("Let me search the corpus…"); that narration is
        # progress reporting, not part of the answer. It is flushed into the tool
        # status event when the call arrives, so only the final segment — the text
        # written after the last tool returned — survives as the saved answer.
        answer_parts: list[str] = []
        persisted = False

        def persist(text: str, partial: bool) -> str | None:
            """Save the assistant turn. Returns the new message id, or None."""
            nonlocal persisted
            if persisted or not text.strip():
                return None
            assistant_msg = ChatMessage(
                id=str(uuid.uuid4()), session_id=session_id,
                role="assistant", content=text,
            )
            stream_app_db.add(assistant_msg)
            stream_session = stream_app_db.query(ChatSession).filter(
                ChatSession.id == session_id, ChatSession.user_id == user.id
            ).first()
            if stream_session:
                stream_session.updated_at = datetime.utcnow()
            stream_app_db.commit()
            persisted = True
            emit_event("persisted_result", {
                "persisted": True, "message_id": assistant_msg.id,
                "chat_session_id": session_id, "partial": partial,
            }, stage="chat.persist")
            return assistant_msg.id

        try:
            with trace_operation(
                "chat.turn", "web_chat", chat_session_id=session_id,
                target_kind="chat_session", target_id=session_id,
                metadata={
                    "user_message_id": user_msg.id,
                    "selected_circular_ids": turn_circular_ids,
                    "workspace_id": workspace.id if workspace else None,
                    "stream": True,
                },
            ):
                emit_event("context", {
                    "user_message": message,
                    "selected_circular_ids": turn_circular_ids,
                }, stage="chat.context")
                yield sse("meta", {"session_id": session_id})

                rows = _ordered_chat_messages(stream_app_db, session_id)
                chat_messages = [{"role": m.role, "content": m.content} for m in rows]
                client = get_ai_client_for_user(user)
                circulars_context = _build_chat_circulars_context(
                    stream_db, turn_circular_ids, message,
                    client.config.max_context_tokens,
                )

                for chunk in client.stream_chat(
                    chat_messages, stream_db,
                    circulars_context=circulars_context,
                    selected_circular_ids=turn_circular_ids,
                ):
                    if isinstance(chunk, dict):
                        if chunk.get("phase") == "tools":
                            chunk = {**chunk, "note": "".join(answer_parts).strip()}
                            answer_parts.clear()
                        yield sse("status", chunk)
                        continue
                    answer_parts.append(chunk)
                    yield sse("token", {"content": chunk})

                response_text = "".join(answer_parts)
                emit_event("normalized_result", {"response": response_text}, stage="chat.result")
                message_id = persist(response_text, partial=False)
                yield sse("done", {
                    "session_id": session_id, "message_id": message_id,
                })
        except Exception as e:
            stream_db.rollback()
            stream_app_db.rollback()
            # A stream that dies mid-answer used to discard everything the model had
            # already written — the user watched text appear, then lose it on the
            # next reload. Keep the partial so the turn survives in history and the
            # UI can offer to continue it.
            partial_text = "".join(answer_parts)
            persist(partial_text, partial=True)
            yield sse("error", {
                "error": friendly_chat_error(e),
                "session_id": session_id,
                "partial": bool(partial_text.strip()),
            })
        finally:
            # A closed generator means the client hit Stop, and this is the only
            # hook that runs on disconnect. Unlike the error path — where the
            # fragment is the whole of what the model managed to say, however
            # short — a stop usually lands mid tool-loop, where the buffer holds
            # the model's narration ("Let me pull the circular…") rather than an
            # answer. Keeping those turned history into a list of stubs, so only a
            # substantive fragment is worth a row.
            leftover = "".join(answer_parts)
            if not persisted and (len(leftover.strip()) >= 160 or "\n" in leftover):
                try:
                    persist(leftover, partial=True)
                except Exception:
                    stream_app_db.rollback()
            stream_db.close()
            stream_app_db.close()

    return StreamingResponse(
        # Starlette drives a sync generator through the threadpool, handing each
        # chunk to whichever worker is free. Without a pinned context the trace
        # opened while producing the first chunk is invisible while producing the
        # rest, which cost this endpoint every event after `chat.context`.
        bind_context(stream_response()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/circulars/batch_download")
def batch_download(
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
                    resp = requests.get(c.url, headers=HEADERS, timeout=20)
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
