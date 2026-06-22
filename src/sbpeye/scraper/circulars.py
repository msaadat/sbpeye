import requests
from bs4 import BeautifulSoup
import re
import logging
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from ..models import Attachment, Circular, CircularRelationship
from ..database import PROJECT_ROOT, collection, embedding_backend
from ..checklist import PAGE_MARKER_RE, prepare_reference_chunks
from .clean_html import extract_sbp_text
from ..link_routing import normalize_sbp_url
import uuid
from urllib.parse import unquote, urljoin, urlparse

BASE_URL = "https://www.sbp.org.pk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
HTML_CACHE_DIR = PROJECT_ROOT / "cache" / "html"
ATTACHMENTS_DIR = PROJECT_ROOT / "attachments"
ATTACHMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
_CHROMA_WRITE_LOCK = threading.Lock()

# Map from department index URL path to a human-readable department name.
# Scraped from the cir.asp page links [31]-[56].
DEPARTMENT_INDEX_PAGES = {
    "../acd/index.htm":        "Agriculture Credit & Financial Inclusion (ACFID)",
    "../bsd-1/index.htm":      "Banking Supervision Department-1",
    "../bsd-2/index.htm":      "Banking Supervision Department-2",
    "../bsd-3/index.htm":      "Banking Supervision Department-3",
    "../bpd/index.htm":        "Banking Policy & Regulations (BPRD)",
    "../bsrvd/index.htm":      "Banking Surveillance Department",
    "../CRMD/index.htm":       "Cyber Risk Management Department",
    "../cpd/index.htm":        "Consumer Protection Department",
    "../BCPD/index.htm":       "Banking Conduct Policy Department",
    "../stats/index.htm":      "Statistics & Data Services Department",
    "../dmmd/index.htm":       "Domestic Markets & Monetary Management (DMMD)",
    "../DFIs/index.htm":       "DFIs & Exchange Companies Inspection",
    "../disd/index.htm":       "Digital Innovation & Settlements Department",
    "../acc/index.htm":        "Finance Department",
    "../FIRD/index.htm":       "Financial Institutions Resolution Department",
    "../fsd/index.htm":        "Financial Stability Department",
    "../smefd/circulars/index.htm": "SME, Housing & Sustainable Finance",
    "../ifpd/index.htm":       "Islamic Finance Policy Department",
    "../ifdd/index.htm":       "Islamic Finance Development Department",
    "../MFD/index.htm":        "Microfinance Department",
    "../psd/index.htm":        "Payment Systems Policy & Oversight",
    "../rtgs/circulars/index.htm": "RTGS System",
    "../tod/index.htm":        "Treasury Operations Department",
    "../CMD/index.htm":        "Currency & Accounts Department (CAD)",
}

# Exchange Policy Department uses a different base domain
EPD_INDEX = "https://www.sbp.org.pk/epd/index.htm"


def _get_sbp(url: str, **kwargs):
    """Fetch an SBP URL while validating every redirect target."""
    current_url = normalize_sbp_url(url)
    for _ in range(6):
        response = requests.get(current_url, allow_redirects=False, **kwargs)
        status_code = getattr(response, "status_code", 200)
        if status_code not in {301, 302, 303, 307, 308}:
            return response
        location = getattr(response, "headers", {}).get("location")
        response.close()
        if not location:
            raise ValueError("SBP returned a redirect without a destination.")
        current_url = normalize_sbp_url(urljoin(current_url, location))
    raise ValueError("SBP document fetch exceeded the redirect limit.")


def fetch_page(url: str) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    resp = requests.get(url, headers=HEADERS, timeout=50)
    resp.raise_for_status()
    return BeautifulSoup(resp.content, "html.parser")


def fetch_page_cached(url: str, force: bool = False) -> bytes:
    """Return circular HTML from its deterministic disk cache or the network."""
    HTML_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    circular_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    cache_file = HTML_CACHE_DIR / f"{circular_id}.html"

    if cache_file.exists() and not force:
        return cache_file.read_bytes()

    response = _get_sbp(url, headers=HEADERS, timeout=50)
    response.raise_for_status()
    temp_file = cache_file.with_suffix(".html.part")
    temp_file.write_bytes(response.content)
    temp_file.replace(cache_file)
    return response.content


def detect_attachments(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Return unique attachment links found in circular HTML."""
    found: list[dict] = []
    seen_urls: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if not href:
            continue

        try:
            absolute_url = normalize_sbp_url(urljoin(base_url, href))
        except ValueError:
            continue
        parsed = urlparse(absolute_url)
        extension = Path(parsed.path).suffix.lower()
        if extension not in ATTACHMENT_EXTENSIONS:
            continue
        if parsed.path.lower().endswith("-u.pdf"):
            continue
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)

        filename = unquote(Path(parsed.path).name) or f"attachment{extension}"
        found.append({
            "url": absolute_url,
            "filename": filename,
            "file_type": extension.lstrip("."),
        })

    return found


def attachment_id(circular_id: str, original_url: str) -> str:
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"{circular_id}:{original_url}")
    )


def download_attachment(
    circular_id: str,
    att_info: dict,
    force: bool = False,
) -> tuple[Path | None, bool, str | None]:
    """Stream an attachment into the local cache and atomically publish it."""
    att_id = attachment_id(circular_id, att_info["url"])
    extension = f".{att_info['file_type']}" if att_info.get("file_type") else ""
    destination_dir = ATTACHMENTS_DIR / circular_id
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{att_id}{extension}"

    if destination.exists() and not force:
        return destination, False, None

    temp_path = destination.with_name(f"{destination.name}.part")
    response = None
    try:
        response = _get_sbp(
            att_info["url"], headers=HEADERS, timeout=60, stream=True
        )
        response.raise_for_status()
        with temp_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
        temp_path.replace(destination)
        return destination, True, None
    except Exception as exc:
        temp_path.unlink(missing_ok=True)
        logging.warning("Failed to download attachment %s: %s", att_info["url"], exc)
        return None, False, str(exc)
    finally:
        if response is not None:
            response.close()


def _clean_pdf_pages(raw_pages: list[str]) -> list[str]:
    """Remove repeated page furniture while retaining page-local structure."""
    page_lines = [
        [re.sub(r"\s+", " ", line).strip() for line in page.splitlines() if line.strip()]
        for page in raw_pages
    ]
    edge_counts: Counter[str] = Counter()
    for lines in page_lines:
        for line in lines[:2] + lines[-2:]:
            if len(line) <= 160:
                edge_counts[line.casefold()] += 1
    repeat_threshold = max(3, (len(page_lines) + 1) // 2)
    repeated = {
        value for value, count in edge_counts.items() if count >= repeat_threshold
    }

    cleaned_pages: list[str] = []
    page_number_re = re.compile(r"^(?:page\s+)?\d+(?:\s+of\s+\d+)?$", re.IGNORECASE)
    for lines in page_lines:
        retained: list[str] = []
        last_index = len(lines) - 1
        for index, line in enumerate(lines):
            is_edge = index <= 1 or index >= last_index - 1
            if is_edge and (line.casefold() in repeated or page_number_re.fullmatch(line)):
                continue
            if retained and retained[-1].endswith("-") and line[:1].islower():
                retained[-1] = retained[-1][:-1] + line
            else:
                retained.append(line)
        cleaned_pages.append("\n".join(retained))
    return cleaned_pages


def extract_pdf_text(pdf_path: Path) -> tuple[str, str, str | None]:
    """Extract page-aware PDF text and classify image-only documents."""
    try:
        import pdfplumber

        raw_pages: list[str] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                raw_pages.append(page.extract_text() or "")

        cleaned_pages = _clean_pdf_pages(raw_pages)
        pages_text = [
            f"[[SBPEYE_PAGE:{page_number}]]\n{text.strip()}"
            for page_number, text in enumerate(cleaned_pages, start=1)
        ]

        full_text = "\n\n".join(pages_text)
        extracted_chars = sum(
            len(PAGE_MARKER_RE.sub("", value).strip()) for value in pages_text
        )
        average_chars = extracted_chars / max(len(pages_text), 1)
        status = "scanned" if average_chars < 50 else "extracted"
        return full_text, status, None
    except Exception as exc:
        logging.warning("pdfplumber failed on %s: %s", pdf_path, exc)
        return "", "error", str(exc)


def extract_xlsx_text(xlsx_path: Path) -> tuple[str, str | None]:
    """Extract sheet names and header rows from an XLSX workbook."""
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(
            str(xlsx_path), read_only=True, data_only=True
        )
        parts: list[str] = []
        try:
            for sheet_name in workbook.sheetnames:
                worksheet = workbook[sheet_name]
                parts.append(f"Sheet: {sheet_name}")
                row = next(
                    worksheet.iter_rows(min_row=1, max_row=1, values_only=True),
                    None,
                )
                if row:
                    headers = [str(cell) for cell in row if cell is not None]
                    if headers:
                        parts.append("Headers: " + ", ".join(headers))
        finally:
            workbook.close()
        return "\n".join(parts), None
    except Exception as exc:
        logging.warning("openpyxl failed on %s: %s", xlsx_path, exc)
        return "", str(exc)


def process_attachment(
    db: Session,
    circular: Circular,
    att_info: dict,
    force_download: bool = False,
    verbose: bool = False,
) -> Attachment:
    """Download, extract, and persist one attachment idempotently."""
    att_id = attachment_id(circular.id, att_info["url"])
    attachment = db.query(Attachment).filter(Attachment.id == att_id).first()
    if attachment is None:
        attachment = Attachment(
            id=att_id,
            circular_id=circular.id,
            filename=att_info["filename"],
            original_url=att_info["url"],
            file_type=att_info["file_type"],
            extraction_status="pending",
            is_vectorized=0,
        )
        db.add(attachment)
        db.commit()

    cached_path = (
        PROJECT_ROOT / attachment.local_path if attachment.local_path else None
    )
    complete_statuses = {"extracted", "scanned", "unsupported"}
    if (
        not force_download
        and attachment.extraction_status in complete_statuses
        and cached_path is not None
        and cached_path.exists()
    ):
        if verbose:
            print(f"    [ATT] Already processed: {attachment.filename}")
        return attachment

    local_path, downloaded, download_error = download_attachment(
        circular.id, att_info, force=force_download
    )
    if local_path is None:
        attachment.extraction_status = "error"
        attachment.extraction_error = download_error
        db.commit()
        return attachment

    attachment.local_path = str(local_path.relative_to(PROJECT_ROOT))
    attachment.filename = att_info["filename"]
    attachment.original_url = att_info["url"]
    attachment.file_type = att_info["file_type"]
    attachment.extraction_error = None

    if verbose:
        state = "Downloaded" if downloaded else "Cached"
        print(f"    [ATT] {state}: {attachment.filename}")

    if attachment.file_type == "pdf":
        text, status, error = extract_pdf_text(local_path)
        attachment.content_text = text or None
        attachment.extraction_status = status
        attachment.extraction_error = error
    elif attachment.file_type == "xlsx":
        text, error = extract_xlsx_text(local_path)
        attachment.content_text = text or None
        attachment.extraction_status = "error" if error else "extracted"
        attachment.extraction_error = error
    else:
        attachment.content_text = None
        attachment.extraction_status = "unsupported"

    attachment.is_vectorized = 0
    db.commit()
    return attachment


def fetch_attachments_for_circular(
    db: Session,
    circular: Circular,
    force_fetch: bool = False,
    force_download: bool = False,
    verbose: bool = False,
) -> list[Attachment]:
    raw_html = fetch_page_cached(circular.url, force=force_fetch)
    soup = BeautifulSoup(raw_html, "html.parser")
    detected = detect_attachments(soup, circular.url)
    if verbose:
        print(f"  [ATT] Detected {len(detected)} attachment(s)")

    processed = [
        process_attachment(
            db,
            circular,
            info,
            force_download=force_download,
            verbose=verbose,
        )
        for info in detected
    ]
    circular.attachments_scanned_at = datetime.utcnow()
    db.commit()
    return processed


def discover_departments(verbose: bool = False) -> list[dict]:
    """
    Scrape cir.asp to discover department index pages.
    Returns list of {"name": ..., "url": ...} dicts.
    """
    soup = fetch_page(f"{BASE_URL}/circulars/cir.asp")
    departments = []

    for link in soup.find_all("a", href=True):
        href = link["href"]
        text = link.get_text(strip=True)
        if not text:
            continue

        # Match known department links
        if href in DEPARTMENT_INDEX_PAGES:
            dept_url = urljoin(f"{BASE_URL}/circulars/cir.asp", href)
            dept_name = DEPARTMENT_INDEX_PAGES[href]
            departments.append({"name": dept_name, "url": dept_url})
        elif href == EPD_INDEX or "epd/index" in href:
            departments.append({"name": "Exchange Policy Department", "url": EPD_INDEX})

    if verbose:
        print(f"Discovered {len(departments)} departments")
        for d in departments:
            print(f"  - {d['name']}: {d['url']}")

    return departments


def discover_year_pages(dept_url: str, verbose: bool = False) -> list[dict]:
    """
    Given a department index page URL, discover year-level circular pages.
    Returns list of {"year": str, "url": str} dicts.
    """
    soup = fetch_page(dept_url)
    year_pages = []

    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True)
        href = link["href"]

        # Year links are typically just "2025", "2024", etc.
        if re.match(r"^\d{4}$", text):
            try:
                year_url = normalize_sbp_url(urljoin(dept_url, href))
            except ValueError:
                continue
            year_pages.append({"year": text, "url": year_url})
        # Some departments also have range links like "1981-1990" pointing to PDFs
        elif re.match(r"^\d{4}-\d{4}$", text) and not href.endswith(".pdf"):
            try:
                year_url = normalize_sbp_url(urljoin(dept_url, href))
            except ValueError:
                continue
            year_pages.append({"year": text, "url": year_url})

    if verbose:
        print(f"  Year pages found: {len(year_pages)}")
        for yp in year_pages[:5]:
            print(f"    {yp['year']}: {yp['url']}")
        if len(year_pages) > 5:
            print(f"    ... and {len(year_pages) - 5} more")

    return year_pages


def discover_circulars_on_year_page(
    year_url: str, department: str, year: str, verbose: bool = False
) -> list[dict]:
    """
    Given a year-level page URL, discover individual circulars.
    Returns list of {"reference": ..., "date": ..., "title": ..., "url": ..., "department": ..., "year": ...} dicts.

    Parses table rows with structure: [Circular Reference | Date | Title (link)]
    Falls back to link-only extraction if no suitable table is found.
    """
    try:
        soup = fetch_page(year_url)
    except Exception as e:
        if verbose:
            print(f"    [WARN] Could not fetch {year_url}: {e}")
        return []

    circulars = []
    seen_urls = set()

    # The main circular listing table typically uses width="1000".
    tables = soup.find_all("table", attrs={"width": "1000"})
    if not tables:
        tables = soup.find_all("table")

    for table in tables:
        for tr in table.find_all("tr"):
            if tr.find("table") and tr.find("table").find("img", src="../../images/back.jpg"):
                continue

            cells = tr.find_all(["td", "th"])
            if len(cells) < 3 or len(cells) > 10:
                continue

            title_cell = cells[2]
            link = title_cell.find("a", href=True)
            if not link:
                continue

            href = link["href"]
            title = link.get_text(strip=True)

            if not title or len(title) < 3:
                continue

            href_lower = href.lower()
            if not href_lower.endswith((".htm", ".html")):
                continue
            if href_lower.endswith("-u.pdf"):
                continue
            if href.startswith("#") or "javascript:" in href_lower:
                continue
            try:
                full_url = normalize_sbp_url(urljoin(year_url, href))
            except ValueError:
                continue
            if full_url in seen_urls:
                continue
            seen_urls.add(full_url)

            reference = cells[0].get_text(strip=True)
            date_text = re.sub(r"\s+", " ", cells[1].get_text()).strip()

            circulars.append({
                "reference": reference,
                "date": date_text,
                "title": title,
                "url": full_url,
                "department": department,
                "year": year,
            })

    if not circulars:
        for link in soup.find_all("a", href=True):
            href = link["href"]
            text = link.get_text(strip=True)

            if not text or len(text) < 3:
                continue

            if href.startswith("..") and "index" in href:
                continue
            if href.startswith("#") or "javascript:" in href.lower():
                continue
            if href.lower().endswith("-u.pdf"):
                continue

            href_lower = href.lower()
            if href_lower.endswith((".htm", ".html")):
                try:
                    full_url = normalize_sbp_url(urljoin(year_url, href))
                except ValueError:
                    continue
                if full_url not in seen_urls:
                    seen_urls.add(full_url)
                    circulars.append({
                        "reference": "",
                        "date": "",
                        "title": text,
                        "url": full_url,
                        "department": department,
                        "year": year,
                    })

    if verbose:
        print(f"    Circulars on {year} page: {len(circulars)}")

    return circulars


def scrape_circulars(
    db: Session,
    departments: list[str] | None = None,
    years: list[str] | None = None,
    limit: int = 0,
    skip_llm: bool = True,
    verbose: bool = False,
    force_fetch: bool = False,
    force_download: bool = False,
    include_attachments: bool = True,
    workers: int = 4,
):
    """
    Main entry point: discovers and processes circulars one by one.

    Args:
        db: SQLAlchemy session.
        departments: Optional list of department name substrings to filter.
        years: Optional list of year strings to filter (e.g., ["2025", "2024"]).
        limit: Max circulars to process (0 = unlimited).
        skip_llm: If True, skip LLM relationship extraction.
        verbose: If True, print progress details.
    """
    all_depts = discover_departments(verbose=verbose)

    if departments:
        dept_lower = [d.lower() for d in departments]
        all_depts = [
            d for d in all_depts
            if any(f in d["name"].lower() for f in dept_lower)
        ]
        if verbose:
            print(f"Filtered to {len(all_depts)} departments")

    seen_urls = set()
    pending: list[dict] = []
    skipped = 0

    for dept in all_depts:
        if verbose:
            print(f"\n[DEPT] {dept['name']}")

        year_pages = discover_year_pages(dept["url"], verbose=verbose)

        if years:
            year_pages = [yp for yp in year_pages if yp["year"] in years]

        for yp in year_pages:
            circs = discover_circulars_on_year_page(
                yp["url"], dept["name"], yp["year"], verbose=verbose
            )

            for circ_info in circs:
                if circ_info["url"] in seen_urls:
                    continue
                seen_urls.add(circ_info["url"])


                existing = db.query(Circular).filter(
                    Circular.url == circ_info["url"]
                ).first()
                if existing and not force_fetch and not force_download:
                    skipped += 1
                    print(f"Circular {circ_info['url']} already exists. Skipping")
                    continue

                pending.append(circ_info)
                if limit > 0 and len(pending) >= limit:
                    break
            if limit > 0 and len(pending) >= limit:
                break
        if limit > 0 and len(pending) >= limit:
            break

    worker_count = max(1, workers)
    print(f"Processing {len(pending)} circular(s) with {worker_count} worker(s)")

    def process_one(circ_info: dict) -> None:
        from ..database import SessionLocal

        worker_db = SessionLocal()
        try:
            process_circular(
                worker_db,
                title=circ_info["title"],
                url=circ_info["url"],
                department=circ_info["department"],
                reference=circ_info.get("reference", ""),
                listing_date=circ_info.get("date", ""),
                year=circ_info.get("year", ""),
                skip_llm=skip_llm,
                verbose=verbose,
                force_fetch=force_fetch,
                force_download=force_download,
                include_attachments=include_attachments,
            )
        finally:
            worker_db.close()

    errors = 0
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(process_one, item): (index, item)
            for index, item in enumerate(pending, start=1)
        }
        for future in as_completed(futures):
            index, item = futures[future]
            try:
                future.result()
                print(f"[{index}/{len(pending)}] {item['title']}")
            except Exception as exc:
                errors += 1
                print(f"  [ERROR] {item['url']}: {exc}")

    print(
        f"\nScrape complete. Processed: {len(pending) - errors}, "
        f"errors: {errors}, skipped (existing): {skipped}"
    )


def process_circular(
    db: Session,
    title: str,
    url: str,
    department: str = "Unknown",
    reference: str = "",
    listing_date: str = "",
    year: str = "",
    skip_llm: bool = True,
    verbose: bool = False,
    force_fetch: bool = False,
    force_download: bool = False,
    include_attachments: bool = True,
):
    """Download and idempotently store a circular and its attachments."""
    if verbose:
        print(f"  Fetching: {url}")

    circular_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))
    raw_html = fetch_page_cached(url, force=force_fetch)
    soup = BeautifulSoup(raw_html, "html.parser")
    content_text = extract_sbp_text(raw_html)

    if not content_text:
        if verbose:
            print(f"  [SKIP] No content")
        return

    existing = db.query(Circular).filter(Circular.id == circular_id).first()
    circular_date = None
    if listing_date:
        circular_date = _parse_listing_date(listing_date, year)

    if circular_date is None:
        print(f"  [WARN] Could not parse listing date: '{listing_date}' with year '{year}' for circular {url}")
        circular_date = _extract_date(content_text)
        print(f"  [WARN] Alternate listing date: '{circular_date}'  for circular {url}")

    if circular_date is None and existing is None:
        print(f"  [WARN] Could not extract date from content for circular {url}")
        circular_date = datetime.now()

    reference = re.sub(r"\s+", " ", reference)
    title = re.sub(r"\s+", " ", title)

    if existing is None:
        circular = Circular(id=circular_id)
        db.add(circular)
    else:
        circular = existing
    circular.reference = reference or circular.reference
    circular.title = title
    circular.department = department
    circular.date = circular_date or circular.date or datetime.now()
    circular.url = url
    circular.content_text = content_text
    db.commit()

    if verbose:
        print(f"  [DB] Saved ({len(content_text)} chars, dept={department})")

    if include_attachments:
        detected = detect_attachments(soup, url)
        if verbose:
            print(f"  [ATT] Detected {len(detected)} attachment(s)")
        for info in detected:
            process_attachment(
                db,
                circular,
                info,
                force_download=force_download,
                verbose=verbose,
            )
        circular.attachments_scanned_at = datetime.utcnow()
        db.commit()

    _index_circular(circular, verbose=verbose)
    return circular


def _delete_document_chunks(
    *, circular_id: str | None = None, attachment_id_value: str | None = None
) -> None:
    if attachment_id_value:
        result = collection.get(
            where={"attachment_id": attachment_id_value}, include=["metadatas"]
        )
        ids = result.get("ids", [])
    elif circular_id:
        result = collection.get(
            where={"circular_id": circular_id}, include=["metadatas"]
        )
        ids = [
            item_id
            for item_id, metadata in zip(
                result.get("ids", []), result.get("metadatas", [])
            )
            if not (metadata or {}).get("attachment_id")
        ]
    else:
        ids = []
    if ids:
        collection.delete(ids=ids)


def _index_circular(circular: Circular, verbose: bool = False) -> None:
    """Replace one circular's Chroma chunks without touching attachments."""
    try:
        document = {
            "doc_id": circular.id,
            "doc_type": "circular",
            "doc_label": f"{circular.department} - {circular.reference or circular.title}",
            "text": circular.content_text or "",
            "file_type": "html",
        }
        reference_chunks = prepare_reference_chunks(document)
        chunks = [item["text"] for item in reference_chunks]
        chunk_ids = [f"{circular.id}__chunk_{i}" for i in range(len(chunks))]
        chunk_metas = [
            {
                "circular_id": circular.id,
                "doc_type": "circular",
                "title": circular.title or "",
                "url": circular.url or "",
                "department": circular.department or "",
                "chunk_index": i,
                "ref": item["ref"],
                "unit_id": item["unit_id"],
                "source_start": item["source_start"],
                "source_end": item["source_end"],
                **({"page_start": item["page_start"]} if item["page_start"] else {}),
                **({"page_end": item["page_end"]} if item["page_end"] else {}),
            }
            for i, item in enumerate(reference_chunks)
        ]
        embeddings = embedding_backend.embed_documents(chunks)
        with _CHROMA_WRITE_LOCK:
            _delete_document_chunks(circular_id=circular.id)
            collection.add(
                documents=chunks,
                embeddings=embeddings,
                metadatas=chunk_metas,
                ids=chunk_ids,
            )
        if verbose:
            print(f"  [CHROMA] Indexed ({len(chunks)} chunk(s))")
    except Exception as e:
        logging.exception("ChromaDB indexing failed for %s", circular.url)
        if verbose:
            print(f"  [CHROMA] Error: {e}")


def vectorize_attachment(
    db: Session, attachment: Attachment, verbose: bool = False
) -> bool:
    """Replace the vector chunks for one extracted attachment."""
    if not attachment.content_text or not attachment.content_text.strip():
        return False

    try:
        document = {
            "doc_id": attachment.id,
            "doc_type": "attachment",
            "doc_label": attachment.filename,
            "text": attachment.content_text,
            "file_type": attachment.file_type or "",
        }
        reference_chunks = prepare_reference_chunks(document)
        chunks = [item["text"] for item in reference_chunks]
        chunk_ids = [f"{attachment.id}__chunk_{i}" for i in range(len(chunks))]
        metadata = [
            {
                "circular_id": attachment.circular_id,
                "attachment_id": attachment.id,
                "doc_type": "attachment",
                "title": attachment.filename,
                "filename": attachment.filename,
                "url": attachment.original_url,
                "department": attachment.circular.department or "",
                "chunk_index": index,
                "ref": item["ref"],
                "unit_id": item["unit_id"],
                "source_start": item["source_start"],
                "source_end": item["source_end"],
                **({"page_start": item["page_start"]} if item["page_start"] else {}),
                **({"page_end": item["page_end"]} if item["page_end"] else {}),
            }
            for index, item in enumerate(reference_chunks)
        ]
        embeddings = embedding_backend.embed_documents(chunks)
        with _CHROMA_WRITE_LOCK:
            _delete_document_chunks(attachment_id_value=attachment.id)
            collection.add(
                documents=chunks,
                embeddings=embeddings,
                metadatas=metadata,
                ids=chunk_ids,
            )
        attachment.is_vectorized = 1
        db.commit()
        if verbose:
            print(
                f"  [CHROMA] Indexed attachment: {attachment.filename} "
                f"({len(chunks)} chunks)"
            )
        return True
    except Exception:
        attachment.is_vectorized = 0
        db.commit()
        logging.exception("ChromaDB indexing failed for attachment %s", attachment.id)
        return False


def vectorize_attachments(
    db: Session, circular: Circular, verbose: bool = False
) -> int:
    indexed = 0
    for attachment in circular.attachments:
        if attachment.is_vectorized:
            continue
        if vectorize_attachment(db, attachment, verbose=verbose):
            indexed += 1
    return indexed


def reextract_circular_from_cache(
    db: Session,
    circular: Circular,
    *,
    reindex: bool = False,
    verbose: bool = False,
) -> dict[str, int]:
    """Re-extract one circular and its PDFs using local cached files only."""
    changed = 0
    errors = 0
    indexed = 0
    cache_id = str(uuid.uuid5(uuid.NAMESPACE_URL, circular.url))
    html_path = HTML_CACHE_DIR / f"{cache_id}.html"

    if html_path.is_file():
        extracted = extract_sbp_text(html_path.read_bytes())
        if extracted and extracted != (circular.content_text or ""):
            circular.content_text = extracted
            changed += 1
            _delete_document_chunks(circular_id=circular.id)
    else:
        errors += 1
        if verbose:
            print(f"  [WARN] Missing HTML cache: {html_path}")

    for attachment in circular.attachments:
        if (attachment.file_type or "").lower() != "pdf":
            continue
        local_path = PROJECT_ROOT / attachment.local_path if attachment.local_path else None
        if not local_path or not local_path.is_file():
            errors += 1
            missing_error = "Local PDF cache is missing."
            if (
                attachment.extraction_status != "error"
                or attachment.extraction_error != missing_error
                or attachment.content_text
            ):
                attachment.content_text = None
                attachment.extraction_status = "error"
                attachment.extraction_error = missing_error
                attachment.is_vectorized = 0
                changed += 1
                _delete_document_chunks(attachment_id_value=attachment.id)
            continue
        text, status, error = extract_pdf_text(local_path)
        if text != (attachment.content_text or "") or status != attachment.extraction_status:
            attachment.content_text = text or None
            attachment.extraction_status = status
            attachment.extraction_error = error
            attachment.is_vectorized = 0
            changed += 1
            _delete_document_chunks(attachment_id_value=attachment.id)

    if changed:
        circular.compliance_checklist = None
        circular.checklist_generated_at = None
    db.commit()

    if reindex:
        _index_circular(circular, verbose=verbose)
        for attachment in circular.attachments:
            if (
                (attachment.file_type or "").lower() == "pdf"
                and attachment.extraction_status == "extracted"
                and attachment.content_text
            ):
                indexed += int(vectorize_attachment(db, attachment, verbose=verbose))

    return {"changed": changed, "errors": errors, "indexed": indexed}


def _extract_date(text: str) -> datetime | None:
    """Try to extract a date from circular text."""
    # Common SBP date patterns
    patterns = [
        # "January 15, 2025" or "March 3, 2024"
        r"(\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4})",
        # "15th January, 2025"
        r"(\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|September|October|November|December),?\s+\d{4})",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            date_str = match.group(1)
            # Clean up ordinals
            date_str = re.sub(r"(\d+)(?:st|nd|rd|th)", r"\1", date_str)
            date_str = date_str.replace(",", "")
            for fmt in ["%B %d %Y", "%d %B %Y"]:
                try:
                    return datetime.strptime(date_str, fmt)
                except ValueError:
                    continue

    return None


def _parse_listing_date(date_str: str, year: str = "") -> datetime | None:
    """Parse a date string from the circular listing table."""
    date_str = date_str.strip()
    if not date_str:
        return None

    # Append the year only if no year already appears in the date string.
    if year and not re.search(r"\b(?:19|20)\d{2}\b", date_str):
        date_str = f"{date_str}, {year}"

    clean_date_str = re.sub(r'(?<=\d)(st|nd|rd|th)', '', date_str) #14th, 2nd etc
    clean_date_str = re.sub(r"\s+,\s*", ", ", clean_date_str)   # "January 15 , 2025" -> "January 15, 2025"
    clean_date_str = re.sub(r"\s+", " ", clean_date_str)        # "January  15, 2025" -> "January 15, 2025"

    formats = [
        "%b %d, %Y",
        "%d %B %Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%d-%b-%Y",
        "%d-%b-%y",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
        "%d %B, %Y",
        "%d %B %Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(clean_date_str, fmt)
        except ValueError:
            continue

    return None
