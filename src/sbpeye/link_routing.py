from pathlib import Path
import re
from typing import NamedTuple
from urllib.parse import urldefrag, urlencode, urlparse

from bs4 import BeautifulSoup, NavigableString
from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from .models import Attachment, Circular


DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx"}

_DEPT_FULL_NAME_TO_ABBR: dict[str, str] = {
    "banking policy regulations department": "BPRD",
    "financial institutions resolution department": "FIRD",
    "digital innovation settlements department": "DISD",
    "agriculture credit financial inclusion": "ACFID",
    "banking conduct policy department": "BCPD",
    "islamic finance development department": "IFDD",
    "banking supervision department": "BSD",
    "islamic finance policy department": "IFPD",
    "consumer protection department": "CPD",
    "cyber risk management department": "CRMD",
    "currency management department": "CMD",
    "currency accounts department": "CAD",
    "agriculture credit department": "ACD",
    "domestic markets monetary management": "DMMD",
    "financial stability department": "FSD",
    "banking surveillance department": "BSRVD",
    "payment systems department": "PSD",
    "payment systems oversight": "PSD",
    "treasury operations department": "TOD",
    "sme finance department": "SMEFD",
    "microfinance department": "MFD",
}

_full_name_alts = "|".join(
    re.escape(k) for k in sorted(_DEPT_FULL_NAME_TO_ABBR, key=len, reverse=True)
)
CIRCULAR_REFERENCE_RE = re.compile(
    rf"\b(?P<prefix>{_full_name_alts}|[A-Z][A-Z&]{{1,12}}(?:\s?&\s?[A-Z]{{2,12}})?)\s+Circular"
    r"(?P<letter>\s+Letter)?\s+(?:No\.?\s*)?"
    r"(?P<number>\d{1,3})"
    r"(?P<more>(?:\s*,\s*\d{1,3}|\s+and\s+\d{1,3})*)"
    r"(?:\s+of\s+(?P<year>(?:19|20)\d{2}))?",
    re.IGNORECASE,
)


def _normalize_prefix(prefix: str) -> str:
    # "BC & CPD" and "BC&CPD" appear interchangeably across the site and annexures.
    collapsed = re.sub(r"\s*&\s*", "&", prefix)
    return _DEPT_FULL_NAME_TO_ABBR.get(collapsed.lower(), collapsed.upper())
DATED_YEAR_RE = re.compile(
    r"\bdated\s+"
    r"(?:[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+|\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s+)"
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
        "prefix": _normalize_prefix(match.group("prefix")),
        "is_letter": bool(match.group("letter")),
        "number": int(match.group("number")),
        "year": explicit_year or inferred_year,
    }


def normalize_reference(reference: str | None) -> str | None:
    """Return a canonical, site-independent key for a circular reference.

    "DMMD Circular Letter No. 03 of 2023", " dmmd  circular letter no 3 of 2023 ",
    and the same reference as shown on either the new or the archived site all map to
    "DMMD CIRCULAR LETTER NO 3 OF 2023". Returns ``None`` when the text contains no
    parseable circular reference (e.g. unreferenced notices), so callers can fall back
    to a URL-derived identity.
    """
    parts = _reference_parts(reference)
    if not parts:
        return None
    kind = "CIRCULAR LETTER" if parts["is_letter"] else "CIRCULAR"
    key = f"{parts['prefix']} {kind} NO {parts['number']}"
    if parts["year"]:
        key += f" OF {parts['year']}"
    return key


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


def _resolve_circular_reference_from_parts(
    parts: dict,
    current: Circular,
    db: Session,
) -> Circular | None:
    # The DB may store "BC & CPD" where the normalized prefix is "BC&CPD"; let the
    # SQL prefilter match either spacing — the exact parts comparison below keeps precision.
    prefix_pattern = parts["prefix"].replace("&", "%&%")
    query = db.query(Circular).filter(
        or_(
            Circular.reference.ilike(f"{prefix_pattern}%Circular%"),
            Circular.title.ilike(f"{prefix_pattern}%Circular%"),
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


def resolve_reference_parts(parts: dict, current: Circular, db: Session) -> Circular | None:
    """Resolve an already-parsed reference (e.g. a CircularReference from
    ``iter_circular_references``) to a stored circular. Public entry point for callers
    that harvest references in bulk, such as annexure withdrawal lists."""
    return _resolve_circular_reference_from_parts(parts, current, db)


def _resolve_circular_reference(
    reference_text: str,
    current: Circular,
    db: Session,
    inferred_year: int | None = None,
) -> Circular | None:
    parts = _reference_parts(reference_text, inferred_year)
    if not parts:
        return None
    return _resolve_circular_reference_from_parts(parts, current, db)


def _nearby_dated_year(text: str, start: int) -> int | None:
    match = DATED_YEAR_RE.search(text[start:start + 90])
    return int(match.group("year")) if match else None


class CircularReference(NamedTuple):
    """A single circular number found in text, with its display span and inferred year.

    Grouped references such as "DMMD Circular No. 20, 21 and 22 dated November 03, 2011"
    yield one entry per number (20, 21, 22), all sharing the prefix and the trailing date.
    For the first number the display span covers the whole "<prefix> Circular No. <n>" label;
    for the grouped numbers it covers just the bare number.
    """

    prefix: str
    is_letter: bool
    number: int
    year: int | None
    label_start: int
    label_end: int


def _grouped_numbers(match: re.Match) -> list[tuple[int, int, int]]:
    """Return (number, start, end) spans for the primary and grouped numbers in a match."""
    numbers = [(int(match.group("number")), match.start("number"), match.end("number"))]
    more = match.group("more")
    if more:
        base = match.start("more")
        for item in re.finditer(r"\d{1,3}", more):
            numbers.append((int(item.group()), base + item.start(), base + item.end()))
    return numbers


def iter_circular_references(text: str):
    """Yield a CircularReference for every individual circular number mentioned in `text`.

    Used by both inline-link rendering and relationship resolution so the two paths share
    identical reference parsing and year inference.
    """
    for match in CIRCULAR_REFERENCE_RE.finditer(text):
        prefix = _normalize_prefix(match.group("prefix"))
        is_letter = bool(match.group("letter"))
        explicit_year = int(match.group("year")) if match.group("year") else None
        year = explicit_year or _nearby_dated_year(text, match.end())
        for index, (number, num_start, num_end) in enumerate(_grouped_numbers(match)):
            label_start = match.start() if index == 0 else num_start
            yield CircularReference(prefix, is_letter, number, year, label_start, num_end)


def infer_reference_year(
    content_text: str | None,
    prefix: str,
    is_letter: bool,
    number: int,
) -> int | None:
    """Infer the year of a circular reference from where it is mentioned in `content_text`."""
    for reference in iter_circular_references(content_text or ""):
        if (
            reference.prefix == prefix
            and reference.is_letter == is_letter
            and reference.number == number
        ):
            return reference.year
    return None


def resolve_reference_in_context(
    db: Session,
    current: Circular,
    reference_text: str,
) -> Circular | None:
    """Resolve a free-text circular reference, inferring the year from `current`'s content.

    Mirrors inline-link resolution: a bare reference like "DMMD Circular no. 20" is
    disambiguated by the year of the nearby "dated ..." text in the source content, so
    relationship targets resolve to the same circular the inline link points to.
    """
    parts = _reference_parts(reference_text)
    if not parts:
        return None
    if parts["year"] is None:
        # Prefer a year written into the reference itself ("... dated May 08, 2003",
        # "... of October 8, 2008"), then fall back to inferring it from where the
        # reference is mentioned in the source content.
        embedded = re.search(r"\b(?:19|20)\d{2}\b", reference_text)
        year = int(embedded.group()) if embedded else infer_reference_year(
            current.content_text, parts["prefix"], parts["is_letter"], parts["number"]
        )
        parts = {**parts, "year": year}
    return _resolve_circular_reference_from_parts(parts, current, db)


def _link_plain_circular_references(soup: BeautifulSoup, circular: Circular, db: Session) -> None:
    resolved: dict[tuple, Circular | None] = {}
    for text_node in list(soup.find_all(string=CIRCULAR_REFERENCE_RE)):
        parent = text_node.parent
        if parent and parent.name in {"a", "script", "style", "textarea"}:
            continue

        text = str(text_node)
        replacements = []
        last_end = 0
        for reference in iter_circular_references(text):
            key = (reference.prefix, reference.is_letter, reference.number, reference.year)
            if key not in resolved:
                resolved[key] = _resolve_circular_reference_from_parts(
                    {
                        "prefix": reference.prefix,
                        "is_letter": reference.is_letter,
                        "number": reference.number,
                        "year": reference.year,
                    },
                    circular,
                    db,
                )
            target = resolved[key]
            if not target:
                continue

            if reference.label_start > last_end:
                replacements.append(NavigableString(text[last_end:reference.label_start]))
            anchor = soup.new_tag("a", href=f"/circulars/{target.id}")
            anchor["class"] = "document-pill circular-reference-pill"
            anchor["data-document-link"] = "true"
            anchor["data-document-kind"] = "circular"
            anchor["title"] = target.title
            anchor.string = text[reference.label_start:reference.label_end]
            replacements.append(anchor)
            last_end = reference.label_end

        if not replacements:
            continue
        if last_end < len(text):
            replacements.append(NavigableString(text[last_end:]))
        text_node.replace_with(*replacements)


def harvest_reference_links(html: str | bytes, db: Session, current: Circular) -> list[Circular]:
    """Circulars that ``current``'s detail page hyperlinks to.

    The redesigned site pre-renders in-text references as real ``<a>`` links to other
    circular slugs. Resolving those anchors against stored circulars gives deterministic
    relationship targets (no LLM needed), which callers merge with the model's output.
    """
    soup = BeautifulSoup(html, "html.parser")
    targets: dict[str, Circular] = {}
    for anchor in soup.find_all("a", href=True):
        try:
            url = normalize_sbp_url(anchor.get("href", ""))
        except ValueError:
            continue
        path = urlparse(url).path
        # Only individual circular detail slugs, not the paginated listing (…/circulars/P30).
        if "/circulars/" not in path or re.search(r"/circulars/P\d+$", path):
            continue
        match = (
            db.query(Circular)
            .filter(
                or_(
                    func.lower(Circular.new_url) == url.lower(),
                    func.lower(Circular.url) == url.lower(),
                )
            )
            .first()
        )
        if match is not None and match.id != current.id:
            targets[match.id] = match
    return list(targets.values())


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
