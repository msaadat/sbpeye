"""Overnight repo rates from SBP's economic-data corpus.

The July-2026 redesign moved every economic-data document out of `/ecodata/*.pdf`
into the shared flat asset store under `/assets/document/`. The old
`/ecodata/overnightsreporates2.pdf` still answers HTTP 200, but with the generic
site page as `text/html` — no `%PDF` marker anywhere in the body — so pdfplumber
fails with "No /Root object!". The URLs below are the ones `/economic-data`
itself links to for the "Overnight Repo Rates" row (see `ecodata_index.py`).
"""

import logging
import re
from datetime import datetime
from io import BytesIO

import pdfplumber
import requests
from sqlalchemy.orm import Session

from ..models import EcoDataSeries
from .circulars import HEADERS

logger = logging.getLogger(__name__)

ASSET_BASE_URL = "https://www.sbp.org.pk/assets/document"
# The daily report: a single row, the most recent business day.
REPO_RATE_URL = f"{ASSET_BASE_URL}/overnightsreporates2.pdf"
# The archive: the same series back to May 2015, three date/rate column pairs per page.
REPO_RATE_ARCHIVE_URL = f"{ASSET_BASE_URL}/OvernightsRepoRates_Arch2.pdf"

REPO_RATE_SERIES = "Overnight_Repo_Rate"

# SBP writes the date column half a dozen ways across the archive's eleven years:
# "25-May, 2015", "4-April, 2016", "11 April, 2016", "2 March 2026", "20-AUG-26".
_DATE_RE = re.compile(r"^(\d{1,2})[\s,./-]+([A-Za-z]{3,9})[\s,./-]+(\d{2}|\d{4})$")
_RATE_RE = re.compile(r"^\d{1,3}(?:\.\d+)?$")
_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


class EcoDataUnavailable(RuntimeError):
    """SBP is no longer serving a parseable document for the requested series."""


def _parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    match = _DATE_RE.match(" ".join(str(text).split()))
    if not match:
        return None
    day, month_name, year = match.groups()
    month = _MONTHS.get(month_name[:3].lower())
    if month is None:
        return None
    year_num = int(year)
    if year_num < 100:
        year_num += 2000
    try:
        return datetime(year_num, month, int(day))
    except ValueError:
        return None


def _parse_rate(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = " ".join(str(text).split()).replace(",", "").rstrip("%").strip()
    if not _RATE_RE.match(cleaned):
        return None
    return float(cleaned)


def extract_repo_rates(tables: list[list[list]]) -> list[tuple[datetime, float]]:
    """Pull (date, rate) pairs out of pdfplumber tables.

    Walks each row left to right and pairs any cell that reads as a date with the
    next cell that reads as a rate, so the daily report's single two-column table
    and the archive's three side-by-side date/rate pairs both fall out of the same
    pass. Header rows ("As on (Date)", "Rate (%)") parse as neither and drop out.
    """
    by_date: dict[datetime, float] = {}
    for table in tables:
        for row in table or []:
            pending_date: datetime | None = None
            for cell in row:
                date = _parse_date(cell)
                if date is not None:
                    pending_date = date
                    continue
                rate = _parse_rate(cell)
                if rate is not None and pending_date is not None:
                    by_date[pending_date] = rate
                    pending_date = None
    return sorted(by_date.items())


def parse_repo_rate_pdf(content: bytes) -> list[tuple[datetime, float]]:
    with pdfplumber.open(BytesIO(content)) as pdf:
        tables = [table for page in pdf.pages for table in page.extract_tables()]
    return extract_repo_rates(tables)


def _download_pdf(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    if not resp.content.startswith(b"%PDF"):
        raise EcoDataUnavailable(
            f"{url} answered {resp.status_code} with "
            f"{resp.headers.get('content-type')!r} ({len(resp.content)} bytes) and no "
            "%PDF marker — SBP has moved or withdrawn the document. Check the "
            "/economic-data index for its current location."
        )
    return resp.content


def fetch_repo_rates(include_archive: bool = True) -> list[tuple[datetime, float]]:
    """Fetch the overnight repo rate series, newest day last.

    The archive is fetched first so the daily report — which is republished before
    the archive catches up — wins on any date the two disagree.
    """
    by_date: dict[datetime, float] = {}
    if include_archive:
        by_date.update(parse_repo_rate_pdf(_download_pdf(REPO_RATE_ARCHIVE_URL)))
    by_date.update(parse_repo_rate_pdf(_download_pdf(REPO_RATE_URL)))
    if not by_date:
        raise EcoDataUnavailable(
            f"{REPO_RATE_URL} parsed as a PDF but yielded no date/rate rows; "
            "SBP has probably changed the report's layout."
        )
    return sorted(by_date.items())


def scrape_ecodata(db: Session, include_archive: bool = True) -> int:
    """Refresh the overnight repo rate series. Returns the number of rows written.

    Raises rather than swallowing: a scraper that logs a stack trace and returns
    leaves the chart silently frozen on whatever it last held, which is how the
    dead `/ecodata/` URL went unnoticed. Callers decide what a failed refresh means.
    """
    rates = fetch_repo_rates(include_archive=include_archive)

    existing = {
        row.date: row
        for row in db.query(EcoDataSeries).filter(EcoDataSeries.name == REPO_RATE_SERIES)
    }
    written = 0
    for date, value in rates:
        row = existing.get(date)
        if row is None:
            db.add(EcoDataSeries(name=REPO_RATE_SERIES, date=date, value=value))
            written += 1
        elif row.value != value:
            row.value = value
            written += 1
    db.commit()

    logger.info(
        "%s: %d rows parsed (%s to %s), %d written",
        REPO_RATE_SERIES, len(rates), rates[0][0].date(), rates[-1][0].date(), written,
    )
    return written
