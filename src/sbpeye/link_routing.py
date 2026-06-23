from pathlib import Path
import re
from urllib.parse import urldefrag, urlencode, urlparse

from bs4 import BeautifulSoup, NavigableString
from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from .models import Attachment, Circular


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}
CIRCULAR_REFERENCE_RE = re.compile(
    r"\b(?P<prefix>[A-Z][A-Z&]{1,12})\s+Circular"
    r"(?P<letter>\s+Letter)?\s+No\.?\s*"
    r"(?P<number>\d{1,3})"
    r"(?:\s+of\s+(?P<year>(?:19|20)\d{2}))?",
    re.IGNORECASE,
)
DATED_YEAR_RE = re.compile(
    r"\bdated\s+"
    r"(?:[A-Z][a-z]+\s+\d{1,2},?\s+|\d{1,2}\s+[A-Z][a-z]+,?\s+)"
    r"(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def is_allowed_sbp_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and (hostname == "sbp.org.pk" or hostname.endswith(".sbp.org.pk"))
        and parsed.username is None
        and parsed.password is None
    )


def normalize_sbp_url(url: str) -> str:
    normalized = urldefrag(url.strip())[0]
    if normalized.startswith("http://"):
        normalized = "https://" + normalized[7:]
    if not is_allowed_sbp_url(normalized):
        raise ValueError("Only HTTPS links on sbp.org.pk are supported.")
    return normalized


def attachment_info(url: str) -> dict:
    path = Path(urlparse(url).path)
    extension = path.suffix.lower()
    if extension not in DOCUMENT_EXTENSIONS:
        raise ValueError("This SBP link is not a supported document.")
    return {"url": url, "filename": path.name or f"document{extension}", "file_type": extension.lstrip(".")}


def _reference_parts(text: str | None, inferred_year: int | None = None) -> dict | None:
    if not text:
        return None
    match = CIRCULAR_REFERENCE_RE.search(text)
    if not match:
        return None
    explicit_year = int(match.group("year")) if match.group("year") else None
    return {
        "prefix": match.group("prefix").upper(),
        "is_letter": bool(match.group("letter")),
        "number": int(match.group("number")),
        "year": explicit_year or inferred_year,
    }


def _candidate_year(circular: Circular) -> int | None:
    if circular.date:
        return circular.date.year
    for value in (circular.reference, circular.url):
        if not value:
            continue
        match = re.search(r"\b((?:19|20)\d{2})\b", value)
        if match:
            return int(match.group(1))
    return None


def _resolve_circular_reference(
    reference_text: str,
    current: Circular,
    db: Session,
    inferred_year: int | None = None,
) -> Circular | None:
    parts = _reference_parts(reference_text, inferred_year)
    if not parts:
        return None

    query = db.query(Circular).filter(
        or_(
            Circular.reference.ilike(f"{parts['prefix']}%Circular%"),
            Circular.title.ilike(f"{parts['prefix']}%Circular%"),
        )
    )
    if parts["year"]:
        query = query.filter(
            or_(
                extract("year", Circular.date) == parts["year"],
                Circular.reference.ilike(f"%{parts['year']}%"),
                Circular.url.ilike(f"%/{parts['year']}/%"),
            )
        )

    matches: list[Circular] = []
    for candidate in query.all():
        if candidate.id == current.id:
            continue
        candidate_parts = _reference_parts(candidate.reference) or _reference_parts(candidate.title)
        if not candidate_parts:
            continue
        if (
            candidate_parts["prefix"] != parts["prefix"]
            or candidate_parts["is_letter"] != parts["is_letter"]
            or candidate_parts["number"] != parts["number"]
        ):
            continue
        candidate_year = candidate_parts["year"] or _candidate_year(candidate)
        if parts["year"] and candidate_year != parts["year"]:
            continue
        matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def _nearby_dated_year(text: str, start: int) -> int | None:
    match = DATED_YEAR_RE.search(text[start:start + 90])
    return int(match.group("year")) if match else None


def _link_plain_circular_references(soup: BeautifulSoup, circular: Circular, db: Session) -> None:
    resolved: dict[str, Circular | None] = {}
    for text_node in list(soup.find_all(string=CIRCULAR_REFERENCE_RE)):
        parent = text_node.parent
        if parent and parent.name in {"a", "script", "style", "textarea"}:
            continue

        text = str(text_node)
        replacements = []
        last_end = 0
        for match in CIRCULAR_REFERENCE_RE.finditer(text):
            reference_text = match.group(0)
            inferred_year = _nearby_dated_year(text, match.end())
            normalized_reference = re.sub(r"\s+", " ", reference_text).strip().lower()
            key = f"{normalized_reference}|{inferred_year or ''}"
            if key not in resolved:
                resolved[key] = _resolve_circular_reference(reference_text, circular, db, inferred_year)
            target = resolved[key]
            if not target:
                continue

            if match.start() > last_end:
                replacements.append(NavigableString(text[last_end:match.start()]))
            anchor = soup.new_tag("a", href=f"/circulars/{target.id}")
            anchor["class"] = "document-pill circular-reference-pill"
            anchor["data-document-link"] = "true"
            anchor["data-document-kind"] = "circular"
            anchor["title"] = target.title
            anchor.string = reference_text
            replacements.append(anchor)
            last_end = match.end()

        if not replacements:
            continue
        if last_end < len(text):
            replacements.append(NavigableString(text[last_end:]))
        text_node.replace_with(*replacements)


def rewrite_document_links(html: str, circular: Circular, db: Session) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        try:
            url = normalize_sbp_url(anchor.get("href", ""))
        except ValueError:
            continue
        known = db.query(Circular.id).filter(func.lower(Circular.url) == url.lower()).first()
        attachment = db.query(Attachment.id).filter(
            func.lower(Attachment.original_url) == url.lower()
        ).first()
        if known:
            target, kind = f"/circulars/{known[0]}", "circular"
        elif attachment:
            target = f"/documents/open?{urlencode({'id': attachment[0]})}"
            kind = Path(urlparse(url).path).suffix.lstrip(".").upper() or "document"
        else:
            continue
        anchor["href"] = target
        anchor["class"] = list(dict.fromkeys([*(anchor.get("class") or []), "document-pill"]))
        anchor["data-document-link"] = "true"
        anchor["data-document-kind"] = kind
        anchor.attrs.pop("target", None)
        anchor.attrs.pop("rel", None)
    _link_plain_circular_references(soup, circular, db)
    return str(soup)
