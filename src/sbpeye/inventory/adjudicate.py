"""Layer 2 — decide whether each candidate actually discusses the query subject.

This is where precision comes from, and it is what makes recall affordable: a net wide
enough to catch documents at rank 3,000 is only usable if something reduces it to a
reviewable list. A cosine threshold cannot do that — measured, it puts irrelevant
Primary Dealer circulars above genuinely relevant ones.

The model only ever *labels* candidates that layer 1 enumerated. It cannot widen the
corpus, and its exclusions are returned with reasons rather than dropped, so a caller can
audit what was rejected and why.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

logger = logging.getLogger(__name__)

JUDGE_PROMPT_VERSION = "judge-v1"

BATCH_SIZE = 12
MAX_WORKERS = 4
PASSAGE_CHARS = 1200

VERDICT_INCLUDED = "included"
VERDICT_EXCLUDED = "excluded"
VERDICT_UNDETERMINED = "undetermined"

_SYSTEM_PROMPT = """You decide whether regulatory documents discuss a given subject.

For each numbered passage, answer whether the document it came from discusses the \
subject. Answer yes if the passage imposes, describes, amends, or refers to \
requirements on the subject — even in passing, and even if the document is mainly about \
something else. Answer no if the subject merely resembles the passage's vocabulary, or \
if the passage is boilerplate, a covering note, or about a different matter.

Give a reason of at most 15 words for every item."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "discusses": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "discusses", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    verdict: str
    reason: str = ""


def _build_prompt(query: str, items: list[tuple[int, str, str]]) -> str:
    lines = [f"Subject: {query}", "", "Passages:"]
    for number, label, passage in items:
        text = passage[:PASSAGE_CHARS].replace("\n", " ").strip()
        lines.append(f"[{number}] ({label}) {text}")
    lines.append("")
    lines.append("Return a verdict for every numbered passage.")
    return "\n".join(lines)


def _judge_batch(
    llm, query: str, items: list[tuple[int, str, str]]
) -> dict[int, Verdict]:
    try:
        response = llm.complete_json(
            _SYSTEM_PROMPT, _build_prompt(query, items), json_schema=_SCHEMA
        )
        verdicts = response.get("verdicts") or []
        if not isinstance(verdicts, list):
            raise ValueError("`verdicts` was not a list")
    except Exception as exc:  # noqa: BLE001
        logger.warning("adjudication batch failed: %s", exc)
        # Undetermined, not excluded. Silently dropping a candidate the model could not
        # read would be indistinguishable from the model having rejected it.
        return {
            number: Verdict(VERDICT_UNDETERMINED, f"judge unavailable: {exc}")
            for number, _, _ in items
        }

    resolved: dict[int, Verdict] = {}
    for entry in verdicts:
        if not isinstance(entry, dict):
            continue
        try:
            number = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        verdict = VERDICT_INCLUDED if entry.get("discusses") else VERDICT_EXCLUDED
        resolved[number] = Verdict(verdict, str(entry.get("reason") or "").strip())

    for number, _, _ in items:
        if number not in resolved:
            resolved[number] = Verdict(
                VERDICT_UNDETERMINED, "judge returned no verdict for this passage"
            )
    return resolved


def adjudicate(
    llm,
    query: str,
    entries: list[tuple[str, str]],
    max_workers: int = MAX_WORKERS,
) -> list[Verdict]:
    """Judge every entry. ``entries`` is ``[(label, passage), ...]``, order preserved.

    With no LLM configured every candidate is ``undetermined`` — the caller then decides
    whether an unreviewed union is useful, rather than the service pretending it judged.
    """
    if not entries:
        return []
    if llm is None:
        return [
            Verdict(VERDICT_UNDETERMINED, "adjudication disabled") for _ in entries
        ]

    batches: list[list[tuple[int, str, str]]] = []
    for start in range(0, len(entries), BATCH_SIZE):
        batches.append([
            (start + offset, label, passage)
            for offset, (label, passage) in enumerate(entries[start:start + BATCH_SIZE])
        ])

    results: dict[int, Verdict] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        for batch_result in pool.map(lambda b: _judge_batch(llm, query, b), batches):
            results.update(batch_result)

    return [
        results.get(index, Verdict(VERDICT_UNDETERMINED, "missing verdict"))
        for index in range(len(entries))
    ]
