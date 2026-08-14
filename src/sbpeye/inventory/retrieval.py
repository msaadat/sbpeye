"""Layer 1 — build the candidate set. No LLM decides membership here.

Two arms, unioned:

* **lexical** — FTS5 over the resolved term set, with no cutoff. This is the recall
  backbone: measured against the live corpus it finds every document that contains a
  term, which is what "list all circulars that talk about X" actually asks for.
* **dense** — an exact, full-corpus cosine scan, capped to a band. Its job is only to
  catch documents that discuss the subject without using any term in the set. It is a
  supplement because a threshold on this embedding model cannot separate: relevant
  documents were measured as deep as rank 3,153 of 3,727.

See docs/INVENTORY_SEARCH_PLAN.md sections 4 and 6.
"""

import logging
from dataclasses import dataclass, field

import numpy as np
from sqlalchemy import text
from sqlalchemy.orm import Session

from .index import CorpusEmbeddingSnapshot
from .terms import fts_match_query

logger = logging.getLogger(__name__)

HYDE_PROMPT_VERSION = "hyde-v1"

_HYDE_SYSTEM = """You write a short passage in the register of Pakistani banking \
regulation. Given a topic, write one paragraph of about 80 words that reads like an \
excerpt from a State Bank of Pakistan circular or regulation discussing that topic: \
obligations, defined terms, and the language such an instrument would use. Write only \
the passage."""

_HYDE_SCHEMA = {
    "type": "object",
    "properties": {"passage": {"type": "string"}},
    "required": ["passage"],
    "additionalProperties": False,
}


@dataclass
class Candidate:
    """One logical document admitted to adjudication, with why it got here."""

    logical_kind: str
    document_id: str
    matched_via: set[str] = field(default_factory=set)
    matched_terms: list[str] = field(default_factory=list)
    semantic_score: float = 0.0
    # Chunk indices into the snapshot, best-scoring first.
    chunk_indices: list[int] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return (self.logical_kind, self.document_id)


def generate_hyde_passage(query: str, llm) -> tuple[str, list[str]]:
    """A synthetic passage to embed instead of the bare query.

    A three-word query and a regulatory paragraph sit in different regions of the
    embedding space — the measured gap between "AML" and "anti-money laundering" was
    large enough to change the result count fourfold. Embedding passage-shaped text
    compares like with like.
    """
    if llm is None:
        return "", []
    try:
        response = llm.complete_json(
            _HYDE_SYSTEM, f"Topic: {query}", json_schema=_HYDE_SCHEMA
        )
        passage = (response.get("passage") or "").strip()
        if not passage:
            return "", ["HyDE returned an empty passage; using the query alone"]
        return passage, []
    except Exception as exc:  # noqa: BLE001 - degrade explicitly
        return "", [f"HyDE generation failed, using the query alone: {exc}"]


def lexical_candidates(
    db: Session,
    terms: list[str],
    include_circulars: bool = True,
    include_laws: bool = True,
    allowed_keys: dict[str, set[str] | None] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Every document matching any term. Returns ``{key: matched terms}``.

    Deliberately unbounded — a ``LIMIT`` here would reintroduce the truncation the
    inventory exists to avoid. Terms are matched one at a time rather than as one OR
    expression so each hit records which term found it.
    """
    matches: dict[tuple[str, str], list[str]] = {}
    if not terms:
        return matches

    targets = []
    if include_circulars:
        targets.append(("circular", "circulars_fts", "circular_id"))
    if include_laws:
        targets.append(("law", "laws_fts", "document_id"))

    for term in terms:
        match_query = fts_match_query([term])
        if not match_query:
            continue
        for logical_kind, table, id_column in targets:
            try:
                rows = db.execute(
                    text(
                        f"SELECT {id_column} FROM {table} WHERE {table} MATCH :mq"
                    ),
                    {"mq": match_query},
                ).fetchall()
            except Exception:
                logger.exception("%s lexical arm failed for term %r", table, term)
                continue
            allowed = (allowed_keys or {}).get(logical_kind)
            for row in rows:
                if allowed is not None and row[0] not in allowed:
                    continue
                matches.setdefault((logical_kind, row[0]), []).append(term)
    return matches


def dense_band(
    snapshot: CorpusEmbeddingSnapshot,
    query_vectors: np.ndarray,
    band: int,
    include_circulars: bool = True,
    include_laws: bool = True,
    allowed_keys: dict[str, set[str] | None] | None = None,
) -> dict[tuple[str, str], tuple[float, list[int]]]:
    """Top ``band`` documents by max chunk cosine, scored exactly over every chunk.

    Returns ``{key: (score, chunk indices best-first)}``. Max rather than mean: one
    clearly relevant provision is enough to make a long circular relevant, and averaging
    would penalise exactly the long frameworks most worth finding.
    """
    if band <= 0 or snapshot.size == 0 or len(query_vectors) == 0:
        return {}

    scores = snapshot.scores(query_vectors)
    # Best query variant per chunk — the winning variant is a reporting detail, the
    # maximum is what decides membership.
    chunk_scores = scores.max(axis=0)

    keys = snapshot.logical_keys()
    allowed = set()
    if include_circulars:
        allowed.add("circular")
    if include_laws:
        allowed.add("law")

    best: dict[tuple[str, str], float] = {}
    chunks: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for index, key in enumerate(keys):
        if key[0] not in allowed or not key[1]:
            continue
        # Filter before ranking, not after: taking the top `band` and then discarding
        # filtered documents would silently return a band smaller than requested.
        permitted = (allowed_keys or {}).get(key[0])
        if permitted is not None and key[1] not in permitted:
            continue
        score = float(chunk_scores[index])
        if score > best.get(key, -1.0):
            best[key] = score
        chunks.setdefault(key, []).append((score, index))

    ranked = sorted(best.items(), key=lambda item: (-item[1], item[0]))[:band]
    result: dict[tuple[str, str], tuple[float, list[int]]] = {}
    for key, score in ranked:
        ordered = sorted(chunks[key], key=lambda item: -item[0])
        result[key] = (score, [index for _, index in ordered])
    return result


def union_candidates(
    lexical: dict[tuple[str, str], list[str]],
    dense: dict[tuple[str, str], tuple[float, list[int]]],
    max_candidates: int,
) -> tuple[list[Candidate], int]:
    """Merge both arms. Returns ``(candidates, truncated count)``.

    Truncation order is a correctness property, not a preference: a document containing
    a search term is never dropped in favour of one that merely scored well, and within
    the dense-only group the lowest scores go first.
    """
    candidates: dict[tuple[str, str], Candidate] = {}

    for key, terms in lexical.items():
        candidate = Candidate(logical_kind=key[0], document_id=key[1])
        candidate.matched_via.add("lexical")
        candidate.matched_terms = sorted(set(terms))
        candidates[key] = candidate

    for key, (score, chunk_indices) in dense.items():
        candidate = candidates.get(key)
        if candidate is None:
            candidate = Candidate(logical_kind=key[0], document_id=key[1])
            candidates[key] = candidate
        candidate.matched_via.add("semantic")
        candidate.semantic_score = score
        candidate.chunk_indices = chunk_indices

    ordered = sorted(
        candidates.values(),
        key=lambda c: (
            0 if "lexical" in c.matched_via else 1,
            -c.semantic_score,
            c.document_id,
        ),
    )
    if len(ordered) <= max_candidates:
        return ordered, 0
    return ordered[:max_candidates], len(ordered) - max_candidates
