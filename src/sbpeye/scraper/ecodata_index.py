import cloudscraper
import uuid
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from sqlalchemy.orm import Session
from ..models import EcoDataEntry

SITE_URL = "https://www.sbp.org.pk"
INDEX_URL = f"{SITE_URL}/economic-data"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}


def _resolve_url(href: str) -> str | None:
    if not href or href.strip() == "":
        return None
    return urljoin(SITE_URL + "/", href.strip())

def _clean_text(text: str) -> str:
    return " ".join(text.split()).strip()

def _extract_format_type(url: str | None) -> str | None:
    if not url:
        return None
    url_lower = url.lower()
    if url_lower.endswith(".xlsx"):
        return "xlsx"
    if url_lower.endswith(".xls"):
        return "xls"
    if url_lower.endswith(".pdf"):
        return "pdf"
    if url_lower.endswith(".csv"):
        return "csv"
    return None

def parse_ecodata_index(soup: BeautifulSoup) -> list[dict]:
    """Parse the redesigned /economic-data page into EcoDataEntry descriptors.

    Layout: an ``h2.sector-heading`` names the sector; within it an accordion
    ``h5.primary-color`` names the category; a ``table.economic-data-table`` lists rows,
    each a ``div.data-row-wrapper`` with a title (``h6``), frequency/date pill badges,
    and a document download link.
    """
    entries: list[dict] = []
    seen_ids: set[str] = set()
    sort_order = 0

    for table in soup.select("table.economic-data-table"):
        section_el = table.find_previous("h2", class_="sector-heading")
        section = _clean_text(section_el.get_text()) if section_el else "General"
        category_el = table.find_previous("h5", class_="primary-color")
        subsection = _clean_text(category_el.get_text()) if category_el else None

        for wrapper in table.select("div.data-row-wrapper"):
            title_el = wrapper.select_one("h6")
            description = _clean_text(title_el.get_text()) if title_el else ""
            if len(description) < 3:
                continue

            badges = [_clean_text(b.get_text()) for b in wrapper.select(".pill-badge")]
            frequency = badges[0] if badges else None
            last_update = badges[1] if len(badges) > 1 else None

            series_url = document_url = None
            for anchor in wrapper.select("a[href]"):
                resolved = _resolve_url(anchor.get("href", ""))
                if not resolved:
                    continue
                if "/economic-data/" in resolved and series_url is None:
                    series_url = resolved
                elif _extract_format_type(resolved) and document_url is None:
                    document_url = resolved

            url = series_url or document_url
            format_url = document_url
            format_type = _extract_format_type(document_url)

            entry_id_base = url or f"{section}-{subsection}-{description}"
            entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, entry_id_base))
            if entry_id in seen_ids:
                entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{entry_id_base}-{sort_order}"))
            seen_ids.add(entry_id)

            entries.append({
                "id": entry_id,
                "section": section or "General",
                "subsection": subsection,
                "description": description,
                "url": url,
                "frequency": frequency,
                "format_url": format_url,
                "format_type": format_type,
                "last_update": last_update,
                "archive_url": None,
                "archive_updated": None,
                "sort_order": sort_order,
                "is_quick_link": 0,
            })
            sort_order += 1

    return entries


def scrape_ecodata_index(db: Session) -> list[dict]:
    resp = cloudscraper.create_scraper().get(INDEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")
    entries = parse_ecodata_index(soup)

    db.query(EcoDataEntry).delete()
    db.commit()
    for entry_data in entries:
        db.add(EcoDataEntry(**entry_data))
    db.commit()
    return entries
