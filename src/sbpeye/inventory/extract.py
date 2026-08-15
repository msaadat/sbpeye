"""Layer 3 — reduce a matched chunk to the span worth citing, and locate it.

A 130-word chunk is not a citation. The output row is
``reference | locator | relevant text``, so the passage has to be narrowed to the part
that actually discusses the subject.

The hard rule: **a span presented as a quotation must be a literal substring of stored
source text.** A model that paraphrases produces something that reads like a regulatory
quotation and is not one, which in an audit context is the one failure that cannot ship.
Every extraction is verified, and an unverifiable one falls back to the whole chunk.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass

from .llm import field_of_type

logger = logging.getLogger(__name__)

EXTRACT_PROMPT_VERSION = "extract-v1"

MAX_WORKERS = 4
MAX_SPAN_CHARS = 600

_SYSTEM_PROMPT = """You select the sentences in a passage that discuss a given subject.

Copy the relevant sentences from the passage EXACTLY, character for character. Do not \
paraphrase, summarize, correct spelling, fix spacing, or add words. Do not add quotation \
marks. If several separate sentences are relevant, return the single most relevant \
contiguous run. If nothing in the passage discusses the subject, return an empty string."""

_SCHEMA = {
    "type": "object",
    "properties": {"span": {"type": "string"}},
    "required": ["span"],
    "additionalProperties": False,
}


@dataclass
class Extraction:
    text: str
    verified: bool


def _normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def verify_span(span: str, passage: str) -> Extraction:
    """Accept a span only if it really occurs in the passage.

    Whitespace is normalized on both sides before comparison — a model that collapses a
    line break has not altered the words, and rejecting that would push almost every
    honest extraction into the fallback. Anything beyond that is a rewrite.
    """
    cleaned = _normalize_whitespace(span)
    if not cleaned:
        return Extraction("", False)

    haystack = _normalize_whitespace(passage)
    if cleaned in haystack:
        return Extraction(cleaned[:MAX_SPAN_CHARS], True)

    # Strip decoration a model may have added around an otherwise exact quote.
    stripped = cleaned.strip('"“”\'` .')
    if stripped and stripped in haystack:
        return Extraction(stripped[:MAX_SPAN_CHARS], True)

    return Extraction("", False)


def _extract_one(llm, query: str, passage: str) -> Extraction:
    if llm is None or not passage.strip():
        return Extraction("", False)
    try:
        response = llm.complete_json(
            _SYSTEM_PROMPT,
            f"Subject: {query}\n\nPassage:\n{passage}\n\n"
            'Return JSON of the form {"span": "..."}.',
            json_schema=_SCHEMA,
        )
        span = field_of_type(response, "span", str) or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("span extraction failed: %s", exc)
        return Extraction("", False)
    return verify_span(span, passage)


def extract_spans(
    llm, query: str, passages: list[str], max_workers: int = MAX_WORKERS
) -> list[Extraction]:
    """Extract one span per passage, order preserved.

    An unverified result is returned as ``Extraction("", False)``; the caller falls back
    to the full passage and reports ``extraction_verified: false`` rather than dropping
    the evidence.
    """
    if not passages:
        return []
    if llm is None:
        return [Extraction("", False) for _ in passages]

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        tasks = [(copy_context(), passage) for passage in passages]
        return list(pool.map(
            lambda item: item[0].run(_extract_one, llm, query, item[1]), tasks
        ))


def resolve_locator(metadata: dict) -> dict:
    """Decide how to point at a passage.

    Page numbers only exist where the chunker found page markers, which is the PDF
    attachment path. Circular HTML bodies have none, so the whole circular-body arm
    needs a different anchor — character offsets, which the chunker already stores.
    """
    page_start = metadata.get("page_start")
    page_end = metadata.get("page_end")
    source_start = metadata.get("source_start")
    source_end = metadata.get("source_end")

    if page_start is not None:
        kind = "page"
    elif source_start is not None:
        kind = "offset"
    else:
        kind = "chunk"

    return {
        "locator_kind": kind,
        "page_start": page_start,
        "page_end": page_end,
        "source_start": source_start,
        "source_end": source_end,
        "source_ref": metadata.get("ref") or "",
    }
