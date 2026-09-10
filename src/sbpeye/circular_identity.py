"""Storage-independent circular reference parsing and versioned identities."""

import re
import uuid
from typing import NamedTuple

IDENTITY_VERSION = 2

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
    r"(?:\s*/\s*(?P<slash_year>(?:19|20)\d{2})\b)?"
    r"(?:\s+of\s+(?P<year>(?:19|20)\d{2})\b)?",
    re.IGNORECASE,
)

# One parser implementation, with the precise pre-v2 grammar retained for migration
# eligibility. In particular the old optional year had no trailing word boundary.
LEGACY_REFERENCE_RE = re.compile(
    CIRCULAR_REFERENCE_RE.pattern.replace(
        r"(?:\s*/\s*(?P<slash_year>(?:19|20)\d{2})\b)?", "",
    ).replace(r"(?P<year>(?:19|20)\d{2})\b", r"(?P<year>(?:19|20)\d{2})"),
    re.IGNORECASE,
)


def _normalize_prefix(prefix: str) -> str:
    # "BC & CPD" and "BC&CPD" appear interchangeably across the site and annexures.
    collapsed = re.sub(r"\s*&\s*", "&", prefix)
    return _DEPT_FULL_NAME_TO_ABBR.get(collapsed.lower(), collapsed.upper())
REFERENCE_DATE_YEAR_RE = re.compile(
    r"\b(?:dated|of)\s+"
    r"(?:[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+|\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+,?\s+)"
    r"(?P<year>(?:19|20)\d{2})\b",
    re.IGNORECASE,
)


def _nearby_reference_year(text: str, start: int) -> int | None:
    match = REFERENCE_DATE_YEAR_RE.search(text[start:start + 90])
    return int(match.group("year")) if match else None


def _reference_parts(
    text: str | None, inferred_year: int | None = None, *, identity_version: int = IDENTITY_VERSION,
) -> dict | None:
    if identity_version not in (1, 2):
        raise ValueError("Unsupported circular identity version")
    if not text:
        return None
    pattern = CIRCULAR_REFERENCE_RE if identity_version == 2 else LEGACY_REFERENCE_RE
    match = pattern.search(text)
    if not match:
        return None
    year_text = match.group("year") or match.groupdict().get("slash_year")
    explicit_year = int(year_text) if year_text else None
    nearby_year = _nearby_reference_year(text, match.end()) if explicit_year is None else None
    return {
        "prefix": _normalize_prefix(match.group("prefix")),
        "is_letter": bool(match.group("letter")),
        "number": int(match.group("number")),
        "year": explicit_year or inferred_year or nearby_year,
    }


def normalize_reference(reference: str | None, *, identity_version: int = IDENTITY_VERSION) -> str | None:
    """Return a canonical, site-independent key for a circular reference.

    "DMMD Circular Letter No. 03 of 2023", " dmmd  circular letter no 3 of 2023 ",
    and the same reference as shown on either the new or the archived site all map to
    "DMMD CIRCULAR LETTER NO 3 OF 2023". Returns ``None`` when the text contains no
    parseable circular reference (e.g. unreferenced notices), so callers can fall back
    to a URL-derived identity.
    """
    parts = _reference_parts(reference, identity_version=identity_version)
    if not parts:
        return None
    kind = "CIRCULAR LETTER" if parts["is_letter"] else "CIRCULAR"
    key = f"{parts['prefix']} {kind} NO {parts['number']}"
    if parts["year"]:
        key += f" OF {parts['year']}"
    return key


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
        explicit_year = int(match.group("year") or match.group("slash_year")) if (match.group("year") or match.group("slash_year")) else None
        year = explicit_year or _nearby_reference_year(text, match.end())
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


def circular_identity(reference: str | None, url: str, *, identity_version: int = IDENTITY_VERSION) -> str:
    """Stable UUID from the canonical reference, falling back to the original URL."""
    basis = normalize_reference(reference, identity_version=identity_version) or url
    return str(uuid.uuid5(uuid.NAMESPACE_URL, basis))


def reference_conflicts(reference: str | None) -> list[str]:
    """Diagnose contradictory explicit years without changing display labels."""
    match = CIRCULAR_REFERENCE_RE.search(reference or "")
    if not match:
        return []
    years = {int(value) for value in (match.group("year"), match.group("slash_year")) if value}
    tail = (reference or "")[match.end():]
    repeated = re.match(r"(?:\s+of\s+(?:19|20)\d{2}\b)+", tail, re.I)
    if repeated:
        years.update(int(value) for value in re.findall(r"(?:19|20)\d{2}", repeated.group()))
    return ["conflicting_explicit_years"] if len(years) > 1 else []
