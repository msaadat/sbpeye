from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from urllib.parse import urljoin, urlparse, urlencode
from pathlib import Path
import requests as http_requests
from bs4 import BeautifulSoup
import re as _re
import os
import json
import uuid

from .database import engine, Base, get_db, SessionLocal
from .models import SyncStatus, Circular, CircularRelationship, EcoDataSeries, EcoDataEntry, Settings, ChatSession, ChatMessage
from .search import search_engine
from .ai import AIConfig, get_ai_client

from .scraper.circulars import HEADERS
from .scraper.ecodata import scrape_ecodata
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


def _circular_summary(circular: Circular, snippet: str | None = None) -> dict:
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
    }


def _settings_payload(config: AIConfig) -> dict:
    return {
        "provider": config.provider,
        "base_url": config.base_url,
        "api_key": config.api_key,
        "model": config.model,
        "chat_model": config.chat_model,
        "max_context_tokens": config.max_context_tokens,
        "ai_provider": config.provider,
        "ai_base_url": config.base_url,
        "ai_api_key": config.api_key,
        "ai_model": config.model,
        "ai_chat_model": config.chat_model,
        "ai_max_context_tokens": config.max_context_tokens,
    }


def _is_allowed_sbp_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and parsed.netloc.lower() in ALLOWED_DOMAINS




ALLOWED_DOMAINS = {"www.sbp.org.pk", "sbp.org.pk"}

SBP_BASE = "https://www.sbp.org.pk"

NAV_PATTERNS = [
    "home", "what's new", "site map", "contact us", "faqs", "home page",
    "feedback", "careers", "tenders", "rti", "sitemap",
    "disclaimer", "copyright  ", "all rights reserved",
    "back to", "previous page", "next page", "last updated",
    "state bank of pakistan", "sbp logo",
    "i. i. chundrigar road", "phone:", "fax:",
]

# Create all tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="SBPEye", description="Independent SBP Circulars & EcoData Indexer")

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


def clean_sbp_html(html_content: bytes, base_url: str = "") -> str:
    soup = BeautifulSoup(html_content, "html.parser")

    for tag in soup.find_all(["head", "script", "style", "noscript", "link", "meta"]):
        tag.decompose()

    if base_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if src and not src.startswith(("http://", "https://", "data:")):
                img["src"] = urljoin(base_url, src)
        for a in soup.find_all("a"):
            href = a.get("href", "")
            if href and not href.startswith(("http://", "https://", "#", "javascript:", "mailto:")):
                a["href"] = urljoin(base_url, href)
            a["target"] = "_blank"
            a["rel"] = "noopener noreferrer"

    body = soup.find("body")
    if not body:
        return "<p>No content found</p>"

    tables = body.find_all("table")
    for table in tables:
        text = table.get_text().strip().lower()
        if not text:
            table.decompose()
            continue

        links = table.find_all("a")
        link_count = len(links)

        if link_count >= 3 and len(text) < 500:
            if any(p in text for p in NAV_PATTERNS):
                table.decompose()
                continue

        if len(text) < 200 and any(p in text for p in NAV_PATTERNS):
            table.decompose()

    for table in body.find_all("table"):
        if not table.get_text(strip=True):
            table.decompose()
            continue
        for tr in table.find_all("tr"):
            row_text = tr.get_text(strip=True).lower()
            if not row_text:
                tr.decompose()
                continue
            # if len(row_text) < 100 and any(p in row_text for p in NAV_PATTERNS):
            #     tr.decompose()
            #     continue
            imgs = tr.find_all("img")
            cells = tr.find_all(["td", "th"])
            if imgs and all(not td.get_text(strip=True) for td in cells):
                tr.decompose()

    for font in soup.find_all("font"):
        font.unwrap()

    for tag in soup.find_all(style=True):
        del tag["style"]

    for tag in soup.find_all(bgcolor=True):
        del tag["bgcolor"]

    for tag in soup.find_all(background=True):
        del tag["background"]

    for tag in soup.find_all(color=True):
        del tag["color"]

    body_tag = soup.find("body")
    if body_tag:
        for attr in ("text", "link", "vlink", "alink"):
            body_tag.attrs.pop(attr, None)

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if any(kw in src.lower() for kw in ["logo", "banner", "header", "footer", "nav", "bg", "background", "back"]):
            img.decompose()

    for br in soup.find_all("br"):
        if br.parent and len(br.parent.get_text(strip=True)) == 0:
            br.decompose()

    result = str(body)
    return result



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
    vector_db_ready = os.path.exists("chroma_db/chroma.sqlite3")

    return {
        "sync_status": sync_status.status if sync_status and sync_status.status else "idle",
        "live_status": (sync_status.status if sync_status and sync_status.status else "idle").upper(),
        "total_circulars": total_circulars,
        "department_count": department_count,
        "indexed_today": indexed_today,
        "vector_db_state": "READY" if vector_db_ready else "OFFLINE",
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
async def search_circulars(
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
        "items": [_circular_summary(r["circular"], r.get("snippet")) for r in results],
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


@app.get("/api/circulars/{circular_id}/source")
async def get_circular_source(circular_id: str, db: Session = Depends(get_db)):
    c = db.query(Circular).filter(Circular.id == circular_id).first()
    if not c:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    if not _is_allowed_sbp_url(c.url):
        return JSONResponse({"error": "Only SBP (sbp.org.pk) circulars are supported."}, status_code=400)

    if c.url.lower().split("?", 1)[0].endswith(".pdf"):
        return {
            "type": "pdf",
            "url": c.url,
            "content": None,
            "preview_url": f"/api/pdf_preview?url={urlencode({'': c.url})[1:]}",
        }

    try:
        resp = http_requests.get(c.url, headers=HEADERS, timeout=50)
        resp.raise_for_status()

        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "windows-1252"):
            if resp.apparent_encoding:
                resp.encoding = resp.apparent_encoding

        return {
            "type": "html",
            "url": c.url,
            "content": clean_sbp_html(resp.content, base_url=c.url),
        }
    except Exception as e:
        return JSONResponse({"error": str(e), "type": "html", "url": c.url, "content": ""}, status_code=502)


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
        "compliance_checklist": _safe_json_list(c.compliance_checklist),
        "status": c.status or "active",
        "relationships": {
            "outgoing": [rel_dict(r) for r in outgoing],
            "incoming": [rel_dict(r) for r in incoming],
        },
    }


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


@app.get("/api/pdf_preview")
async def pdf_preview(url: str):
    import io
    import base64
    import pdfplumber

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain not in ALLOWED_DOMAINS:
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
    config = AIConfig.from_db(db) or AIConfig.from_env()
    return _settings_payload(config)


@app.post("/api/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    config = AIConfig(
        provider=data.get("provider", data.get("ai_provider", "lmstudio")),
        base_url=data.get("base_url", data.get("ai_base_url", "http://localhost:1234/v1")),
        api_key=data.get("api_key", data.get("ai_api_key", "lm-studio")),
        model=data.get("model", data.get("ai_model", "local-model")),
        chat_model=data.get("chat_model", data.get("ai_chat_model", "")),
        max_context_tokens=int(data.get("max_context_tokens", data.get("ai_max_context_tokens", 4000))),
    )
    config.save_to_db(db)
    return {"message": "Settings saved successfully", "settings": _settings_payload(config)}


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
    circular_ids = set()
    for m in messages:
        if m.circular_ids:
            for cid in json.loads(m.circular_ids):
                circular_ids.add(cid)
    circulars = db.query(Circular).filter(Circular.id.in_(circular_ids)).all() if circular_ids else []
    return {
        "id": session.id,
        "title": session.title,
        "messages": [
            {"id": m.id, "role": m.role, "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else None}
            for m in messages
        ],
        "circulars": [_circular_summary(c) for c in circulars],
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


def _build_chat_circulars_context(db: Session, circular_ids: list[str]) -> str:
    circulars_context = ""
    if circular_ids:
        circulars = db.query(Circular).filter(Circular.id.in_(circular_ids)).all()
        for c in circulars:
            title = c.title or "Untitled"
            ref = c.reference or "No reference"
            content = c.content_text or ""
            if len(content) > 2000:
                content = content[:2000] + "..."
            circulars_context += f"\n---\n[{ref}] {title}\n{content}\n---\n"

    return circulars_context or "No circulars selected for context."


@app.post("/api/chat")
async def chat_message(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    message = data.get("message", "")
    circular_ids = data.get("circular_ids", [])
    session_id = data.get("session_id")

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    if not session_id:
        session_id = str(uuid.uuid4())
        session = ChatSession(id=session_id, title=message[:80])
        db.add(session)
    else:
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            session_id = str(uuid.uuid4())
            session = ChatSession(id=session_id, title=message[:80])
            db.add(session)

    user_msg = ChatMessage(
        id=str(uuid.uuid4()),
        session_id=session_id,
        role="user",
        content=message,
        circular_ids=json.dumps(circular_ids) if circular_ids else None,
    )
    db.add(user_msg)
    db.commit()

    circulars_context = _build_chat_circulars_context(db, circular_ids)
    messages = db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
    chat_messages = [{"role": m.role, "content": m.content} for m in messages]

    try:
        client = get_ai_client(db)
        response_text = client.chat(chat_messages, db, circulars_context=circulars_context)
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
    circular_ids = data.get("circular_ids", [])
    session_id = data.get("session_id")

    if not message.strip():
        return JSONResponse({"error": "Message cannot be empty"}, status_code=400)

    if not session_id:
        session_id = str(uuid.uuid4())
        session = ChatSession(id=session_id, title=message[:80])
        db.add(session)
    else:
        session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
        if not session:
            session_id = str(uuid.uuid4())
            session = ChatSession(id=session_id, title=message[:80])
            db.add(session)

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

            circulars_context = _build_chat_circulars_context(stream_db, circular_ids)
            rows = stream_db.query(ChatMessage).filter(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at).all()
            chat_messages = [{"role": m.role, "content": m.content} for m in rows]
            client = get_ai_client(stream_db)

            for chunk in client.stream_chat(chat_messages, stream_db, circulars_context=circulars_context):
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
                
            try:
                resp = http_requests.get(c.url, headers=HEADERS, timeout=20)
                resp.raise_for_status()
            except Exception as e:
                print(f"Failed to fetch {c.url}: {e}")
                continue
                
            # Safely create a file name
            safe_ref = (c.reference or c.id).replace("/", "_").replace("\\", "_")
            if c.url.lower().endswith(".pdf"):
                zip_file.writestr(f"{safe_ref}.pdf", resp.content)
            else:
                # It's an HTML circular. Save the HTML.
                zip_file.writestr(f"{safe_ref}.html", resp.content)
                
                # Fetch attachments
                soup = BeautifulSoup(resp.content, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a.get("href", "").strip()
                    if href.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx")):
                        abs_url = urljoin(c.url, href)
                        try:
                            att_resp = http_requests.get(abs_url, headers=HEADERS, timeout=20)
                            att_resp.raise_for_status()
                            att_filename = abs_url.split("/")[-1].split("?")[0]
                            if not att_filename:
                                att_filename = "attachment.file"
                            # Add to a subfolder for this circular
                            zip_file.writestr(f"{safe_ref}_attachments/{att_filename}", att_resp.content)
                        except Exception as e:
                            print(f"Failed to fetch attachment {abs_url}: {e}")
            time.sleep(0.5) # Be gentle to SBP servers
            
    zip_buffer.seek(0)
    return StreamingResponse(
        iter([zip_buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=circulars_batch.zip"}
    )
