from fastapi import FastAPI, Depends, Request, BackgroundTasks, Form
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from urllib.parse import urljoin, urlparse, urlencode
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

# Setup templates and static files
os.makedirs("src/sbpeye/static", exist_ok=True)
os.makedirs("src/sbpeye/templates", exist_ok=True)

app.mount("/static", StaticFiles(directory="src/sbpeye/static"), name="static")
templates = Jinja2Templates(directory="src/sbpeye/templates")

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request, db: Session = Depends(get_db)):
    # Get last sync date
    sync_status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    last_sync = sync_status.last_sync_date if sync_status else "Never"
    
    # Get departments for filters
    departments_query = db.query(Circular.department).distinct().order_by(Circular.department).all()
    departments = [d[0] for d in departments_query if d[0]]

    # Get year range for filter dropdowns
    year_range = db.query(
        func.min(extract('year', Circular.date)),
        func.max(extract('year', Circular.date)),
    ).filter(Circular.date.isnot(None)).first()
    min_year = int(year_range[0]) if year_range[0] else datetime.now().year
    max_year = int(year_range[1]) if year_range[1] else datetime.now().year

    return templates.TemplateResponse(
        request=request, name="index.html",
        context={
            "last_sync": last_sync,
            "departments": departments,
            "min_year": min_year,
            "max_year": max_year,
        }
    )


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


@app.get("/view_circular", response_class=HTMLResponse)
async def view_circular(request: Request, cir: str):
    parsed = urlparse(cir)
    domain = parsed.netloc.lower()
    if domain not in ALLOWED_DOMAINS:
        return templates.TemplateResponse(
            request=request, name="circular.html",
            context={"content": "", "url": cir,
                     "error": "Only SBP (sbp.org.pk) circulars are supported."}
        )

    try:
        resp = http_requests.get(cir, headers=HEADERS, timeout=50)
        resp.raise_for_status()

        if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "windows-1252"):
            if resp.apparent_encoding:
                resp.encoding = resp.apparent_encoding

        cleaned_html = clean_sbp_html(resp.content, base_url=cir)

        return templates.TemplateResponse(
            request=request, name="circular.html",
            context={"content": cleaned_html, "url": cir, "error": None}
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="circular.html",
            context={"content": "", "url": cir, "error": str(e)}
        )

ECODATA_CACHE_TTL_HOURS = 1

@app.get("/ecodata", response_class=HTMLResponse)
async def ecodata_page(request: Request, db: Session = Depends(get_db)):
    sync_status = db.query(SyncStatus).order_by(SyncStatus.id.desc()).first()
    last_sync = sync_status.last_sync_date if sync_status else None
    ecodata_time = sync_status.ecodata_index_time if sync_status else None

    return templates.TemplateResponse(
        request=request, name="ecodata.html",
        context={
            "last_sync": last_sync,
            "ecodata_time": ecodata_time,
        }
    )


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


@app.get("/partials/ecodata_table", response_class=HTMLResponse)
async def partial_ecodata_table(request: Request, db: Session = Depends(get_db)):
    entries = _get_ecodata_entries(db)

    quick_links = [e for e in entries if e["is_quick_link"]]
    sections = {}
    for e in entries:
        if e["is_quick_link"]:
            continue
        section = e["section"]
        subsection = e["subsection"]
        if section not in sections:
            sections[section] = {"subsections": {}, "entries": []}
        if subsection:
            if subsection not in sections[section]["subsections"]:
                sections[section]["subsections"][subsection] = []
            sections[section]["subsections"][subsection].append(e)
        else:
            sections[section]["entries"].append(e)

    return templates.TemplateResponse(
        request=request, name="partials/ecodata_table.html",
        context={
            "quick_links": quick_links,
            "sections": sections,
        }
    )


@app.get("/partials/ecodata_summary", response_class=HTMLResponse)
async def partial_ecodata_summary(request: Request, url: str, db: Session = Depends(get_db)):
    if not is_summarizable(url):
        return templates.TemplateResponse(
            request=request, name="partials/ecodata_summary.html",
            context={"error": "This document is not configured for summarization.", "summary": None}
        )
    try:
        summary = summarize_pdf(url, db)
        return templates.TemplateResponse(
            request=request, name="partials/ecodata_summary.html",
            context={"summary": summary, "error": None, "url": url}
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request, name="partials/ecodata_summary.html",
            context={"error": str(e), "summary": None}
        )


@app.get("/api/circulars/tags")
async def get_tags(db: Session = Depends(get_db)):
    from sqlalchemy import distinct
    rows = db.query(Circular.tags).filter(Circular.tags != None, Circular.tags != "").all()
    tag_counts = {}
    for row in rows:
        try:
            tags = json.loads(row[0]) if row[0] else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        for t in tags:
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
    db: Session = Depends(get_db)
):
    results, total = search_engine.search(
        q, db,
        start_year=_parse_year(start_year),
        end_year=_parse_year(end_year),
        department=department,
        sort_by=sort_by,
        tag=tag,
    )
    return [
        {
            "id": r["circular"].id,
            "title": r["circular"].title,
            "department": r["circular"].department,
            "reference": r["circular"].reference,
            "date": r["circular"].date.strftime("%Y-%m-%d") if r["circular"].date else None,
            "url": r["circular"].url,
            "summary": r["circular"].summary[:200] if r["circular"].summary else None,
            "tags": json.loads(r["circular"].tags) if r["circular"].tags else [],
            "status": r["circular"].status or "active",
            "snippet": r["snippet"],
        }
        for r in results
    ]


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
        {
            "id": c.id,
            "title": c.title,
            "department": c.department,
            "reference": c.reference,
            "date": c.date.strftime("%Y-%m-%d") if c.date else None,
            "url": c.url,
            "summary": c.summary[:200] if c.summary else None,
            "tags": json.loads(c.tags) if c.tags else [],
            "status": c.status or "active",
        }
        for c in circulars
    ]


@app.get("/api/circulars/by_url")
async def get_circular_by_url(url: str, db: Session = Depends(get_db)):
    c = db.query(Circular).filter(Circular.url == url).first()
    if not c:
        return JSONResponse({"error": "Circular not found"}, status_code=404)
    return {
        "id": c.id,
        "title": c.title,
        "reference": c.reference,
        "department": c.department,
        "date": c.date.strftime("%Y-%m-%d") if c.date else None,
        "url": c.url,
    }


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
        target = None
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active"}
        return {
            "type": r.type,
            "target_id": r.target_id,
            "target_reference": r.target_reference,
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
        "tags": json.loads(c.tags) if c.tags else [],
        "compliance_checklist": json.loads(c.compliance_checklist) if c.compliance_checklist else [],
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
        target = None
        if r.target_id:
            tc = db.query(Circular).filter(Circular.id == r.target_id).first()
            if tc:
                target = {"id": tc.id, "title": tc.title, "reference": tc.reference, "url": tc.url, "status": tc.status or "active"}
        return {
            "type": r.type,
            "target_id": r.target_id,
            "target_reference": r.target_reference,
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


# --- HTMX Partial Routes ---

@app.get("/partials/news", response_class=HTMLResponse)
async def partial_news(request: Request, db: Session = Depends(get_db)):
    try:
        data = _scrape_sbp_news(db)
    except Exception:
        data = {"press_releases": [], "whats_new": [], "error": "Failed to load news"}
    return templates.TemplateResponse(
        request=request, name="partials/news.html",
        context={"press_releases": data.get("press_releases", []), "whats_new": data.get("whats_new", [])}
    )


@app.get("/partials/departments", response_class=HTMLResponse)
async def partial_departments(request: Request, db: Session = Depends(get_db)):
    results = db.query(
        Circular.department, func.count(Circular.id).label("count")
    ).group_by(Circular.department).order_by(Circular.department.asc()).all()
    departments = [{"department": r.department, "count": r.count} for r in results]
    return templates.TemplateResponse(
        request=request, name="partials/departments.html",
        context={"departments": departments}
    )


@app.get("/partials/years", response_class=HTMLResponse)
async def partial_years(request: Request, department: str, db: Session = Depends(get_db)):
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
    years = [{"year": int(r.year), "count": r.count} for r in results]
    return templates.TemplateResponse(
        request=request, name="partials/years.html",
        context={"years": years, "department": department}
    )


@app.get("/partials/circulars", response_class=HTMLResponse)
async def partial_circulars(request: Request, department: str, year: int, db: Session = Depends(get_db)):
    circulars = db.query(Circular).filter(
        Circular.department == department,
        extract("year", Circular.date) == year
    ).order_by(Circular.date.desc()).all()
    return templates.TemplateResponse(
        request=request, name="partials/circulars.html",
        context={"circulars": circulars, "department": department, "year": year}
    )


@app.get("/partials/search", response_class=HTMLResponse)
async def partial_search(
    request: Request,
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

    params = {}
    if q: params['q'] = q
    if start_year: params['start_year'] = start_year
    if end_year: params['end_year'] = end_year
    if department: params['department'] = department
    if tag: params['tag'] = tag
    params['sort_by'] = sort_by
    params['per_page'] = per_page
    base_qs = urlencode(params)

    return templates.TemplateResponse(
        request=request, name="partials/search_results.html",
        context={
            "results": results, "query": q,
            "page": page, "total_pages": total_pages,
            "total": total, "per_page": per_page,
            "base_qs": base_qs,
            "start_year": start_year, "end_year": end_year,
            "department": department, "sort_by": sort_by,
            "tag": tag,
        }
    )

import csv
import io
import zipfile
import time

# --- Settings Page ---

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: Session = Depends(get_db)):
    config = AIConfig.from_db(db) or AIConfig.from_env()
    return templates.TemplateResponse(
        request=request, name="settings.html",
        context={"config": config}
    )


@app.post("/api/settings")
async def save_settings(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    config = AIConfig(
        provider=data.get("ai_provider", "lmstudio"),
        base_url=data.get("ai_base_url", "http://localhost:1234/v1"),
        api_key=data.get("ai_api_key", "lm-studio"),
        model=data.get("ai_model", "local-model"),
        chat_model=data.get("ai_chat_model", ""),
        max_context_tokens=int(data.get("ai_max_context_tokens", 4000)),
    )
    config.save_to_db(db)
    return {"message": "Settings saved successfully"}


@app.post("/api/settings/test")
async def test_ai_connection(db: Session = Depends(get_db)):
    try:
        client = get_ai_client(db)
        result = client.test_connection()
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}


# --- Chat Feature ---

@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse(request=request, name="chat.html", context={})


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
        "circulars": [
            {"id": c.id, "title": c.title, "department": c.department, "reference": c.reference, "date": c.date.strftime("%Y-%m-%d") if c.date else None}
            for c in circulars
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

    if not circulars_context:
        circulars_context = "No circulars selected for context."

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
