import requests
import uuid
from bs4 import BeautifulSoup, Tag
from urllib.parse import urljoin
from sqlalchemy.orm import Session
from ..models import EcoDataEntry

BASE_URL = "https://www.sbp.org.pk/ecodata"
INDEX_URL = f"{BASE_URL}/index2.asp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

SECTION_IMAGES = {
    "heading-rs.jpg": "Real Sector",
    "heading-msf.jpg": "Monetary and Financial Statistics",
    "heading-ext.jpg": "External Sector",
}

SUBSECTION_BGCOLORS = {"#698CA8", "#7E2055"}

def _resolve_url(href: str) -> str | None:
    if not href or href.strip() == "":
        return None
    href = href.strip()
    if href.startswith(("http://", "https://")):
        return href
    if href.startswith("/"):
        return urljoin("https://www.sbp.org.pk", href)
    return urljoin(BASE_URL + "/", href)

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

def _is_leaf_data_table(table) -> bool:
    rows = table.find_all("tr")
    six_cell_rows = [tr for tr in rows if len(tr.find_all(["td", "th"])) == 6]
    if not six_cell_rows:
        return False
    child_tables = table.find_all("table")
    for child in child_tables:
        child_rows = child.find_all("tr")
        child_six_cell = [tr for tr in child_rows if len(tr.find_all(["td", "th"])) == 6]
        if child_six_cell:
            return False
    return True

def scrape_ecodata_index(db: Session) -> list[dict]:
    resp = requests.get(INDEX_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    if resp.encoding and resp.encoding.lower() in ("iso-8859-1", "windows-1252"):
        if resp.apparent_encoding:
            resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.content, "html.parser")

    entries = []
    current_section = None
    current_subsection = None
    sort_order = 0
    seen_ids = set()

    for element in soup.find_all(["table", "td"]):
        if element.name == "td":
            bgcolor = element.get("bgcolor", "").upper()
            if bgcolor in {c.upper() for c in SUBSECTION_BGCOLORS}:
                text = _clean_text(element.get_text())
                if text and len(text) < 100:
                    skip_words = ["description", "frequency", "format", "data tables", "economic data"]
                    if not any(kw in text.lower() for kw in skip_words):
                        current_subsection = text
            continue

        if element.name != "table":
            continue

        table = element

        img = table.find("img")
        if img:
            src = img.get("src", "").lower()
            for img_name, section_name in SECTION_IMAGES.items():
                if img_name in src:
                    current_section = section_name
                    current_subsection = None
                    break

        if not _is_leaf_data_table(table):
            continue

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])

            if len(cells) != 6:
                continue

            first_cell = cells[0]
            first_text = _clean_text(first_cell.get_text())

            if not first_text or len(first_text) < 5:
                continue

            skip_words = ["description", "frequency", "other formats", "last update", "data archive", "archive updated"]
            if any(sw in first_text.lower() for sw in skip_words):
                continue

            if "tenor" in first_text.lower() and ("cut-off" in first_text.lower() or "bids" in first_text.lower()):
                continue

            link = first_cell.find("a")
            url = _resolve_url(link.get("href", "")) if link else None

            frequency = _clean_text(cells[1].get_text())
            if frequency in ("", None):
                frequency = None

            format_url = None
            format_type = None
            format_link = cells[2].find("a")
            if format_link:
                format_url = _resolve_url(format_link.get("href", ""))
                format_type = _extract_format_type(format_url)

            last_update = _clean_text(cells[3].get_text())
            if last_update in ("", None, "---"):
                last_update = None

            archive_url = None
            archive_updated = None
            archive_link = cells[4].find("a")
            if archive_link:
                archive_url = _resolve_url(archive_link.get("href", ""))

            archive_updated = _clean_text(cells[5].get_text())
            if archive_updated in ("", "---"):
                archive_updated = None

            entry_id_base = url or f"{current_section}-{current_subsection}-{first_text}"
            entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, entry_id_base))

            if entry_id in seen_ids:
                entry_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{entry_id_base}-{sort_order}"))
            seen_ids.add(entry_id)

            is_quick_link = 1 if current_section is None else 0

            entry = {
                "id": entry_id,
                "section": current_section or "General",
                "subsection": current_subsection,
                "description": first_text,
                "url": url,
                "frequency": frequency,
                "format_url": format_url,
                "format_type": format_type,
                "last_update": last_update,
                "archive_url": archive_url,
                "archive_updated": archive_updated,
                "sort_order": sort_order,
                "is_quick_link": is_quick_link,
            }
            entries.append(entry)
            sort_order += 1

    db.query(EcoDataEntry).delete()
    db.commit()

    for entry_data in entries:
        db_entry = EcoDataEntry(**entry_data)
        db.add(db_entry)

    db.commit()
    return entries
