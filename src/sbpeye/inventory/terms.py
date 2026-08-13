"""Layer 0 — turn a query into the term set the lexical arm searches for.

The measured problem this solves: "AML" and "anti-money laundering" name the same
subject but retrieve very differently, so a caller who happens to type the acronym gets
a different inventory from one who types the phrase. Expanding the term set is the single
highest-leverage recall lever available (section 5.3 of the plan).

The generator runs unsupervised as part of a tool call, so the safety property lives in
the code rather than in a human review step:

    the verbatim query terms are always members of the set, and generated terms may only
    union into it

which means a degenerate, empty, or hostile model response can add noise but can never
retrieve less than the plain query would have. Layer 2 removes the noise.
"""

from dataclasses import dataclass, field

from ..search import tokenize
from .llm import InventoryLLM

TERM_PROMPT_VERSION = "terms-v1"

MAX_GENERATED_TERMS = 24
MAX_TERM_WORDS = 6

_SYSTEM_PROMPT = """You expand a search topic into the vocabulary that Pakistani banking \
regulation actually uses, so that a keyword search over State Bank of Pakistan circulars \
and regulations finds every document discussing the topic.

Return terms that a document would literally contain. Include:
- acronym expansions and contractions (AML <-> anti-money laundering, CDD, KYC)
- British and American spellings (centre / center)
- SBP and Pakistan specific terminology and institution names
- closely associated instruments and obligations that imply the topic

Do not include: generic banking words that appear in nearly every circular, single \
stopwords, explanations, or terms unrelated to the topic. Prefer specific noun phrases \
of one to four words."""

_SCHEMA = {
    "type": "object",
    "properties": {
        "terms": {
            "type": "array",
            "items": {"type": "string"},
        }
    },
    "required": ["terms"],
    "additionalProperties": False,
}


@dataclass
class TermSet:
    """The resolved vocabulary, with each term's provenance kept separate."""

    verbatim: list[str] = field(default_factory=list)
    caller: list[str] = field(default_factory=list)
    generated: list[str] = field(default_factory=list)

    @property
    def all_terms(self) -> list[str]:
        """Every term, de-duplicated, verbatim first so provenance is legible."""
        seen: set[str] = set()
        ordered: list[str] = []
        for term in [*self.verbatim, *self.caller, *self.generated]:
            key = term.lower()
            if key not in seen:
                seen.add(key)
                ordered.append(term)
        return ordered

    def as_source_map(self) -> dict[str, list[str]]:
        return {
            "verbatim": list(self.verbatim),
            "caller": list(self.caller),
            "generated": list(self.generated),
        }


def _acceptable(term: str) -> bool:
    """Reject terms that would match everything or nothing."""
    cleaned = (term or "").strip()
    if not cleaned or len(cleaned) > 120:
        return False
    if len(cleaned.split()) > MAX_TERM_WORDS:
        return False
    # A term that tokenizes to nothing is a stopword or punctuation; FTS cannot use it.
    return bool(tokenize(cleaned))


def build_term_set(
    query: str,
    alternate_queries: list[str] | None = None,
    llm: InventoryLLM | None = None,
    generate: bool = True,
) -> tuple[TermSet, list[str]]:
    """Resolve the term set. Returns ``(term_set, warnings)``.

    Generation failure is a warning, never an error: the verbatim terms still retrieve.
    """
    warnings: list[str] = []
    verbatim = [query.strip()] if query.strip() else []
    caller = [q.strip() for q in (alternate_queries or []) if _acceptable(q)]
    term_set = TermSet(verbatim=verbatim, caller=caller)

    if not generate or llm is None:
        return term_set, warnings

    try:
        response = llm.complete_json(
            _SYSTEM_PROMPT,
            f"Topic: {query}\n\nReturn the search terms as JSON.",
            json_schema=_SCHEMA,
        )
        raw_terms = response.get("terms") or []
        if not isinstance(raw_terms, list):
            raise ValueError("`terms` was not a list")
    except Exception as exc:  # noqa: BLE001 - degrade explicitly, never silently
        warnings.append(f"term generation failed, using verbatim query only: {exc}")
        return term_set, warnings

    existing = {t.lower() for t in term_set.all_terms}
    generated: list[str] = []
    for candidate in raw_terms:
        if not isinstance(candidate, str) or not _acceptable(candidate):
            continue
        key = candidate.strip().lower()
        if key in existing:
            continue
        existing.add(key)
        generated.append(candidate.strip())
        if len(generated) >= MAX_GENERATED_TERMS:
            break

    term_set.generated = generated
    if not generated:
        warnings.append("term generation returned no usable terms")
    return term_set, warnings


def fts_match_query(terms: list[str]) -> str:
    """Build an FTS5 MATCH expression that ORs every term.

    Multi-word terms become quoted phrases against the tokenized body, so "call centre"
    does not also match a document that says "centre" three paragraphs after "call".
    """
    clauses: list[str] = []
    seen: set[str] = set()
    for term in terms:
        tokens = tokenize(term)
        if not tokens:
            continue
        phrase = " ".join(tokens)
        if phrase in seen:
            continue
        seen.add(phrase)
        clauses.append('"%s"' % phrase.replace('"', '""'))
    return " OR ".join(clauses)
