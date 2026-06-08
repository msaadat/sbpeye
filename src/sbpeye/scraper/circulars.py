import requests
from bs4 import BeautifulSoup
import re
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from ..models import Circular, CircularRelationship
from ..database import collection
from ..search import prepare_chunks
import uuid
from urllib.parse import urljoin

BASE_URL = "https://www.sbp.org.pk"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

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


def _ensure_https(url: str) -> str:
    if url.startswith("http://"):
        return "https://" + url[7:]
    return url


def fetch_page(url: str) -> BeautifulSoup:
    """Fetch a URL and return a BeautifulSoup object."""
    resp = requests.get(url, headers=HEADERS, timeout=50)
    resp.raise_for_status()
    return BeautifulSoup(resp.content, "html.parser")


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
            year_url = _ensure_https(urljoin(dept_url, href))
            year_pages.append({"year": text, "url": year_url})
        # Some departments also have range links like "1981-1990" pointing to PDFs
        elif re.match(r"^\d{4}-\d{4}$", text) and not href.endswith(".pdf"):
            year_url = _ensure_https(urljoin(dept_url, href))
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
            full_url = _ensure_https(urljoin(year_url, href))
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
                full_url = _ensure_https(urljoin(year_url, href))
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
    processed = 0
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


                if db.query(Circular).filter(Circular.url == circ_info["url"]).first():
                    skipped += 1
                    print(f"Circular {circ_info['url']} already exists. Skipping")
                    continue

                processed += 1
                if limit > 0 and processed > limit:
                    print(f"\nLimit of {limit} reached. Stopping.")
                    print(f"Processed: {processed}, Skipped (existing): {skipped}")
                    return

                print(f"[{processed}] {circ_info['department']} / {circ_info['date']} - {circ_info['title']}")
                try:
                    process_circular(
                        db,
                        title=circ_info["title"],
                        url=circ_info["url"],
                        department=circ_info["department"],
                        reference=circ_info.get("reference", ""),
                        listing_date=circ_info.get("date", ""),
                        year=circ_info.get("year", ""),
                        skip_llm=skip_llm,
                        verbose=verbose,
                    )
                except Exception as e:
                    print(f"  [ERROR] {e}")

    print(f"\nScrape complete. Processed: {processed}, Skipped (existing): {skipped}")


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
):
    """Download and store a single circular."""
    if verbose:
        print(f"  Fetching: {url}")

    resp = requests.get(url, headers=HEADERS, timeout=50)
    soup = BeautifulSoup(resp.content, "html.parser")
    content_text = soup.get_text(separator=" ", strip=True)

    if not content_text:
        if verbose:
            print(f"  [SKIP] No content")
        return

    circular_id = str(uuid.uuid5(uuid.NAMESPACE_URL, url))

    circular_date = None
    if listing_date:
        circular_date = _parse_listing_date(listing_date, year)

    if circular_date is None:
        print(f"  [WARN] Could not parse listing date: '{listing_date}' with year '{year}' for circular {url}")
        circular_date = _extract_date(content_text)
        print(f"  [WARN] Alternate listing date: '{circular_date}'  for circular {url}")
        
    if circular_date is None: 
        print(f"  [WARN] Could not extract date from content for circular {url}")
        exit(1)
    
    reference = re.sub(r"\s+", " ", reference)
    title = re.sub(r"\s+", " ", title)

    circular = Circular(
        id=circular_id,
        reference=reference or None,
        title=title,
        department=department,
        date=circular_date or datetime.now(),
        url=url,
        content_text=content_text,
    )
    db.add(circular)
    db.commit()

    if verbose:
        print(f"  [DB] Saved ({len(content_text)} chars, dept={department})")

    # Create Vector Embeddings in ChromaDB (chunked)
    try:
        chunks = prepare_chunks(title, content_text)
        chunk_ids = [f"{circular_id}__chunk_{i}" for i in range(len(chunks))]
        chunk_metas = [
            {
                "circular_id": circular_id,
                "title": title,
                "url": url,
                "department": department,
                "chunk_index": i,
            }
            for i in range(len(chunks))
        ]
        collection.add(
            documents=chunks,
            metadatas=chunk_metas,
            ids=chunk_ids,
        )
        if verbose:
            print(f"  [CHROMA] Indexed ({len(chunks)} chunk(s))")
    except Exception as e:
        logging.exception("ChromaDB indexing failed for %s", url)
        if verbose:
            print(f"  [CHROMA] Error: {e}")


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
