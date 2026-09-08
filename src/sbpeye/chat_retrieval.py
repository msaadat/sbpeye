import hashlib
import logging
import re
from dataclasses import dataclass

from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session, joinedload

from .database import collection, embedding_backend
from .checklist import prepare_index_chunks
from .documents import build_corpus, document_from_attachment
from .models import Attachment, Circular, RegDocument
from .search import (
    _HEADING_SCAN,
    REFERENCE_PATTERN,
    SearchEngine,
    expand_query_tokens,
    is_listing_chunk,
    tokenize,
)


logger = logging.getLogger(__name__)

RRF_K = 60
DEFAULT_RESULT_LIMIT = 5
MAX_AUTO_CONTEXT_CIRCULARS = 3
# Chunks either side of an attachment hit that `ScopedAttachmentRetriever` returns with
# it. One, not `LAW_NEIGHBOUR_CHUNKS`' two: an annexure is numbered in paragraphs
# ("4.11. Stable retail deposits… 4.12. All retail deposits that do not…") and the
# paragraph a definition depends on is the one before it, which at index geometry is
# one chunk away. A statute's sub-sections run longer, hence the wider radius there.
ATTACHMENT_NEIGHBOUR_CHUNKS = 1
# Chunks either side of a hit that are returned with it. Sized from the question that
# exposed the need, not from a guess: in the SBP Act, s.9D(1) — who sits on the Monetary
# Policy Committee — is chunk 41, and s.9D(6) — the quorum — is chunk 43. A question
# about both matches the quorum, because that is the sub-section that names it, so a
# radius of 1 still returns an answer missing its own subject. Statutory provisions run
# longer than a paragraph and their sub-sections are what get split.
#
# At ~880 characters per chunk this costs about 4,400 characters per hit. The token
# budget spends that on depth before breadth, which is the right direction for "what
# does this Act say about X" and the wrong one for "which documents mention X" — the
# second question belongs to search_regulatory_inventory, which is why that tool keeps
# its own much smaller per-row excerpt.
LAW_NEIGHBOUR_CHUNKS = 2

FRESHNESS_QUERY_PATTERN = re.compile(
    r"\b(?:latest|current|currently|newest|most\s+recent|recently\s+revised)\b",
    re.IGNORECASE,
)
# A question that is *about* the old rule, rather than one that a withdrawn circular would
# answer wrongly. Withdrawn hits are demoted to pointers in the ranked arms by default
# (`docs/CHAT_CONTEXT_PLAN.md` C11 rule 2); these words are the asker saying they want the
# documents themselves. `FRESHNESS_QUERY_PATTERN` above is the precedent for the switch.
#
# A false positive costs a few full entries rather than a wrong answer — demotion, unlike
# a hard filter, fails safe — so this is broader than it would need to be if withdrawn
# circulars were being dropped.
#
# The year clause carries the one piece of care. A bare year is a historical marker ("what
# were the 2013 limits"), but every SBP circular reference ends in one — "BPRD Circular No.
# 07 of 2019" — so matching years indiscriminately would switch the demotion off for almost
# every question that names a circular. The lookbehind excludes the reference form, where
# the year is part of a name rather than a period being asked about.
WITHDRAWN_QUERY_PATTERN = re.compile(
    r"\b(?:superseded|supersession|supersede[sd]?|cancelled|canceled|withdrawn|withdrew|"
    r"repealed|rescinded|abrogated|replaced|previously|earlier|formerly|"
    r"used\s+to|at\s+the\s+time|no\s+longer\s+in\s+force|historical(?:ly)?|"
    r"(?:previous|earlier|old|former)\s+(?:version|rule|position|limit|requirement)s?)\b"
    r"|(?<!of )\b(?:19\d{2}|20[01]\d|202[0-4])\b",
    re.IGNORECASE,
)


def estimate_tokens(text: str) -> int:
    """Estimate tokens without tying chat retrieval to one model tokenizer."""
    return max(1, (len(text) + 3) // 4)


def referenced_circular_ids(
    db: Session,
    query: str,
    *,
    limit: int = MAX_AUTO_CONTEXT_CIRCULARS,
) -> list[str]:
    """Resolve unambiguous circular references explicitly mentioned in a query."""
    resolved: list[str] = []
    for match in REFERENCE_PATTERN.finditer(query or ""):
        candidates = SearchEngine._search_by_reference(match.group(0), db, limit=2)
        if len(candidates) != 1:
            continue
        circular_id = candidates[0].id
        if circular_id not in resolved:
            resolved.append(circular_id)
        if len(resolved) >= max(1, limit):
            break
    return resolved


def query_context_circular_ids(
    db: Session,
    query: str,
    *,
    limit: int = MAX_AUTO_CONTEXT_CIRCULARS,
) -> list[str]:
    """Resolve explicit references or newest matches for a freshness question."""
    referenced_ids = referenced_circular_ids(db, query, limit=limit)
    if referenced_ids or not FRESHNESS_QUERY_PATTERN.search(query or ""):
        return referenced_ids

    results, _ = SearchEngine().search(
        query,
        db,
        limit=1,
        sort_by="date",
    )
    return [item["circular"].id for item in results]


def focused_retrieval_query(query: str) -> str:
    """Remove reference labels that add no value inside an already scoped corpus."""
    focused = REFERENCE_PATTERN.sub(
        lambda match: " " if match.group(2) else match.group(0),
        query or "",
    )
    focused = re.sub(r"\s+", " ", focused).strip(" ,:;-.")
    return focused or query


def _query_centered_excerpt(text: str, query_tokens: list[str], max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    lowered = text.lower()
    positions = [
        lowered.find(token.lower())
        for token in query_tokens
        if token and lowered.find(token.lower()) >= 0
    ]
    center = min(positions) if positions else 0
    start = max(0, center - max_chars // 2)
    start = min(start, len(text) - max_chars)
    return text[start : start + max_chars].strip()


def passage_key(document_id: str, chunk_index: int | None, text: str = "") -> str:
    """The identity of one passage, shared by every tool that hands passages out.

    Index chunks are written as ``{document_id}__chunk_{index}``, and this reproduces that
    id exactly, so a passage from `search_corpus`, one from `get_circular_details` and one
    from `read_attachment` compare equal when they are the same bytes. A passage without a
    chunk index — a lexical-only hit located by scanning — is keyed on its text instead,
    which is still the right notion of "the same passage".
    """
    if chunk_index is not None:
        return f"{document_id}__chunk_{chunk_index}"
    digest = hashlib.sha1(text.strip().encode("utf-8", "replace")).hexdigest()[:16]
    return f"{document_id}__text_{digest}"


@dataclass(frozen=True)
class ScopedChunk:
    chunk_id: str
    circular_id: str
    document_id: str
    document_type: str
    label: str
    text: str
    chunk_index: int
    page: int | None = None

    @property
    def citation(self) -> str:
        kind = "attachment" if self.document_type == "attachment" else "circular"
        return f"[[{kind}:{self.document_id}|{self.label}]]"

    def payload(self) -> dict:
        item = {
            "source_type": self.document_type,
            "source_label": self.label,
            "passage": self.text,
            "citation": self.citation,
            "chunk_index": self.chunk_index,
        }
        if self.page is not None:
            item["page"] = self.page
        return item

    def pointer(self) -> dict:
        """The same hit with its text withheld: this turn already sent the passage."""
        item = {
            "source_type": self.document_type,
            "source_label": self.label,
            "citation": self.citation,
            "chunk_index": self.chunk_index,
            "provided_earlier": True,
        }
        if self.page is not None:
            item["page"] = self.page
        return item


class ScopedChatRetriever:
    """Retrieve passages only from circulars explicitly selected for a chat."""

    def __init__(self, db: Session, circular_ids: list[str]):
        unique_ids = list(dict.fromkeys(value for value in circular_ids if value))
        rows = (
            db.query(Circular)
            .options(joinedload(Circular.attachments))
            .filter(Circular.id.in_(unique_ids))
            .all()
            if unique_ids
            else []
        )
        by_id = {row.id: row for row in rows}
        self.circulars = [by_id[value] for value in unique_ids if value in by_id]
        self.circular_ids = [row.id for row in self.circulars]
        self._chunks = self._build_chunks()
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in self._chunks}
        # Chunk ids the most recent `search` put on the wire, for the caller's ledger.
        self.last_sent_chunk_ids: list[str] = []

    def _stored_chunks(self) -> dict[str, list[tuple[str, str, dict]]]:
        """Every indexed chunk of the selected circulars, grouped by document.

        ``{document_id: [(chunk_id, text, metadata), …]}``, where a document is a
        circular body or one attachment. Read from Chroma for the reason
        `ScopedLawRetriever._load_chunks` gives: these are the units the vector arm
        scores, and the vector arm reports its hits by chunk id. The old code rebuilt
        the chunks locally at a *different* geometry — 350 words against the index's
        130 — under the same ``{doc}__chunk_{n}`` ids, so a vector hit on chunk 53 was
        looked up as local chunk 53 and boosted a passage thirty pages away. Every
        `get_circular_details` call ran with its semantic arm scrambled that way.

        An unavailable store, or a document the store has not indexed yet, degrades to
        `_build_chunks`' local fallback for that document only.
        """
        if not self.circular_ids:
            return {}
        where = (
            {"circular_id": self.circular_ids[0]}
            if len(self.circular_ids) == 1
            else {"circular_id": {"$in": self.circular_ids}}
        )
        try:
            stored = collection.get(where=where, include=["documents", "metadatas"])
        except Exception:
            logger.info(
                "Indexed chunk fetch unavailable for %s; chunking locally",
                self.circular_ids, exc_info=True,
            )
            return {}
        documents = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        ids = stored.get("ids") or []
        grouped: dict[str, list[tuple[str, str, dict]]] = {}
        for position, (text, meta) in enumerate(zip(documents, metadatas)):
            if not text:
                continue
            meta = meta or {}
            document_id = (
                meta.get("attachment_id")
                if meta.get("doc_type") == "attachment"
                else meta.get("circular_id")
            )
            if not document_id:
                continue
            chunk_id = (
                ids[position]
                if position < len(ids) and ids[position]
                else passage_key(document_id, int(meta.get("chunk_index", position)))
            )
            grouped.setdefault(document_id, []).append((chunk_id, text, meta))
        for items in grouped.values():
            items.sort(key=lambda item: int(item[2].get("chunk_index", 0)))
        return grouped

    def _build_chunks(self) -> list[ScopedChunk]:
        stored = self._stored_chunks()
        chunks: list[ScopedChunk] = []
        for circular in self.circulars:
            for document in build_corpus(circular):
                indexed = stored.get(document["doc_id"])
                if indexed:
                    for chunk_id, text, meta in indexed:
                        chunks.append(
                            ScopedChunk(
                                chunk_id=chunk_id,
                                circular_id=circular.id,
                                document_id=document["doc_id"],
                                document_type=document["doc_type"],
                                label=document["doc_label"],
                                text=text,
                                chunk_index=int(meta.get("chunk_index", 0)),
                                page=meta.get("page_start"),
                            )
                        )
                    continue
                # Not in the store: chunk at index geometry so the ids at least *mean*
                # the same thing if the document is indexed later this process.
                prepared = prepare_index_chunks(document)
                for index, item in enumerate(prepared):
                    chunks.append(
                        ScopedChunk(
                            chunk_id=passage_key(document["doc_id"], index),
                            circular_id=circular.id,
                            document_id=document["doc_id"],
                            document_type=document["doc_type"],
                            label=document["doc_label"],
                            text=item["text"],
                            chunk_index=index,
                            page=item.get("page_start"),
                        )
                    )
        return chunks

    def attachment_manifest(self) -> str:
        sections: list[str] = []
        for circular in self.circulars:
            circular_label = circular.display_name
            citation = f"[[circular:{circular.id}|{circular_label}]]"
            lines = [
                f"Circular: {citation}",
                f"Title: {circular.title}",
                f"Department: {circular.department or 'Unknown'}",
                f"Date: {circular.date.strftime('%Y-%m-%d') if circular.date else 'Unknown'}",
                f"Source URL: {circular.url or 'Unavailable'}",
                "Attachments:",
            ]
            if not circular.attachments:
                lines.append("- None")
            for attachment in sorted(
                circular.attachments, key=lambda item: item.filename.lower()
            ):
                attachment_citation = (
                    f"[[attachment:{attachment.id}|{attachment.filename}]]"
                )
                lines.append(
                    "- "
                    f"{attachment_citation}; type={attachment.file_type or 'unknown'}; "
                    f"source_url={attachment.original_url}; "
                    f"extraction_status={attachment.extraction_status or 'unknown'}; "
                    f"text_available={'yes' if attachment.content_text else 'no'}; "
                    f"indexed={'yes' if attachment.is_vectorized else 'no'}"
                )
            sections.append("\n".join(lines))
        return "\n\n".join(sections) or "No circulars selected for context."

    def direct_documents(self, token_budget: int) -> tuple[list[str], set[str]]:
        included: list[str] = []
        included_ids: set[str] = set()
        remaining = max(0, token_budget)
        for circular in self.circulars:
            for document in build_corpus(circular):
                token_count = estimate_tokens(document["text"])
                if token_count > remaining:
                    continue
                kind = document["doc_type"]
                citation = f"[[{kind}:{document['doc_id']}|{document['doc_label']}]]"
                included.append(
                    f"Source: {citation}\nFull extracted text:\n{document['text']}"
                )
                included_ids.add(document["doc_id"])
                remaining -= token_count
        return included, included_ids

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_RESULT_LIMIT,
        token_budget: int = 1000,
        excluded_document_ids: set[str] | None = None,
        sent_chunk_ids: set[str] | None = None,
    ) -> list[dict]:
        """Ranked passages from the selected circulars, under a token budget.

        `sent_chunk_ids` is the turn's passage ledger. A hit this turn has already handed
        the model comes back as a pointer — citation, index, `provided_earlier` — and is
        neither re-sent nor charged, so the budget and the `limit` both go to passages the
        model has not read. It is a pointer rather than an omission because a search that
        silently skipped the best-matching passage would read as a document that does not
        contain it; the same reasoning as `_dedupe_repeat_row` in `ai.py`.
        """
        excluded_document_ids = excluded_document_ids or set()
        sent_chunk_ids = sent_chunk_ids or set()
        self.last_sent_chunk_ids: list[str] = []
        candidates = [
            chunk
            for chunk in self._chunks
            if chunk.document_id not in excluded_document_ids
        ]
        if not query.strip() or not candidates or token_budget <= 0:
            return []

        lexical_tokens = [tokenize(chunk.text) for chunk in candidates]
        query_tokens = expand_query_tokens(tokenize(query))
        scores: dict[str, float] = {}

        if query_tokens and any(lexical_tokens):
            bm25 = BM25Okapi(lexical_tokens)
            lexical_scores = bm25.get_scores(query_tokens)
            ranked = sorted(
                range(len(lexical_scores)),
                key=lambda index: lexical_scores[index],
                reverse=True,
            )
            for rank, index in enumerate(ranked, start=1):
                if not set(query_tokens).intersection(lexical_tokens[index]):
                    continue
                chunk_id = candidates[index].chunk_id
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)

        try:
            where = (
                {"circular_id": self.circular_ids[0]}
                if len(self.circular_ids) == 1
                else {"circular_id": {"$in": self.circular_ids}}
            )
            vector_results = collection.query(
                query_embeddings=embedding_backend.embed_queries([query]),
                n_results=max(limit * 4, 20),
                where=where,
                include=["metadatas"],
            )
            vector_ids = vector_results.get("ids", [[]])[0]
            for rank, chunk_id in enumerate(vector_ids, start=1):
                chunk = self._chunk_by_id.get(chunk_id)
                if not chunk or chunk.document_id in excluded_document_ids:
                    continue
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank)
        except Exception:
            logger.info("Scoped vector retrieval unavailable; using lexical results", exc_info=True)

        # A contents page matches every query about the document and answers none;
        # it ranks behind every passage of provision text, as in `order_evidence`.
        ranked_chunks = sorted(
            (self._chunk_by_id[chunk_id] for chunk_id in scores),
            key=lambda chunk: (is_listing_chunk(chunk.text), -scores[chunk.chunk_id]),
        )
        results: list[dict] = []
        pointers: list[dict] = []
        remaining = token_budget
        for chunk in ranked_chunks:
            if len(results) >= max(1, min(limit, 10)) or remaining <= 0:
                break
            if chunk.chunk_id in sent_chunk_ids:
                # Ranked, already in the model's context: a pointer costs nothing and
                # takes no slot, so the `limit` fills with unread passages.
                if len(pointers) < max(1, min(limit, 10)):
                    pointers.append(chunk.pointer())
                continue
            text = chunk.text
            tokens = estimate_tokens(text)
            if tokens > remaining:
                text = _query_centered_excerpt(
                    text, query_tokens, remaining * 4
                )
                if not text:
                    break
                chunk = ScopedChunk(
                    **{**chunk.__dict__, "text": text}
                )
                tokens = estimate_tokens(text)
            results.append(chunk.payload())
            self.last_sent_chunk_ids.append(chunk.chunk_id)
            remaining -= tokens
        return results + pointers


@dataclass(frozen=True)
class LawChunk:
    chunk_index: int
    text: str
    ref: str
    page: int | None

    def payload(self) -> dict:
        item = {"chunk_index": self.chunk_index, "passage": self.text}
        if self.ref:
            item["locator"] = self.ref
        if self.page is not None:
            item["page"] = self.page
        return item


class IndexedDocumentRetriever:
    """Read inside one long indexed document: by provision number, by page, by meaning.

    The shape both drill-in tools share — `get_law_details` over a statute and
    `read_attachment` over an annexure — kept in one place so that a retrieval property
    fixed for one (neighbour expansion, the contents-page tie-break in `section`) is
    fixed for the other. BM25 and vector arms fused with RRF over the chunks of one
    document, under a caller's token budget. Two things distinguish it from
    :class:`ScopedChatRetriever`, both because a long document is not a two-page letter.

    * **Nothing is ever inlined whole.** `ScopedChatRetriever.direct_documents` hands over
      a short circular in full; the State Bank of Pakistan Act is 96,422 characters across
      175 chunks and there is no budget under which that is the answer.
    * **Hits are returned with their neighbours.** A circular's chunk boundaries fall
      inside one argument; a statute's fall between sub-sections of one provision, so the
      chunk that matches "quorum" routinely sits one index away from the chunk that lists
      who is subject to it.

    Subclasses say which chunks are theirs (`_where`), how far a hit reaches
    (`neighbour_chunks`), and what to chunk when the store has nothing
    (`_fallback_document`).
    """

    neighbour_chunks: int = LAW_NEIGHBOUR_CHUNKS

    def __init__(self) -> None:
        self._chunks = self._load_chunks()
        self._by_index = {chunk.chunk_index: chunk for chunk in self._chunks}

    def _where(self) -> dict:
        raise NotImplementedError

    def _fallback_document(self) -> dict | None:
        """The document to chunk locally when the store holds none of it, or None."""
        return None

    def _log_label(self) -> str:
        return str(self._where())

    def _load_chunks(self) -> list[LawChunk]:
        """Every indexed chunk of the document, in document order.

        Read from Chroma rather than re-chunked from `content_text`: these are the exact
        units the retrievers scored, so a chunk index in a hit and a chunk index here mean
        the same thing. A store that is unavailable degrades to `_fallback_document`,
        chunked at index geometry so its indexes still mean what the store's would.
        """
        try:
            stored = collection.get(where=self._where(), include=["documents", "metadatas"])
        except Exception:
            logger.info(
                "Indexed chunk fetch unavailable for %s", self._log_label(), exc_info=True
            )
            stored = {}

        documents = stored.get("documents") or []
        metadatas = stored.get("metadatas") or []
        chunks = [
            LawChunk(
                chunk_index=int(meta.get("chunk_index", index)),
                text=text,
                ref=str(meta.get("ref") or ""),
                page=meta.get("page_start"),
            )
            for index, (text, meta) in enumerate(zip(documents, metadatas))
            if text
        ]
        if not chunks:
            fallback = self._fallback_document()
            if fallback:
                chunks = [
                    LawChunk(
                        chunk_index=index,
                        text=item["text"],
                        ref=str(item.get("ref") or ""),
                        page=item.get("page_start"),
                    )
                    for index, item in enumerate(prepare_index_chunks(fallback))
                ]
        return sorted(chunks, key=lambda chunk: chunk.chunk_index)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def pages(self) -> list[int]:
        """The page numbers the chunks carry, ascending; empty for an HTML source."""
        return sorted({chunk.page for chunk in self._chunks if chunk.page is not None})

    def page(self, number: int, *, token_budget: int) -> list[dict]:
        """Every chunk of one page, in order, with no neighbour expansion.

        The chunker restarts at page boundaries, so a page's chunks *are* the page. A
        reader that names a page wants that page — a model that has seen "Part 1,
        section 4 is on page 15" in a contents listing needs no ranking to get there —
        so nothing is widened and the budget is the only bound.
        """
        hits = [chunk for chunk in self._chunks if chunk.page == number]
        return self._expand(hits, token_budget=token_budget, neighbours=0)

    def section(self, label: str, *, token_budget: int) -> list[dict]:
        """Chunks where a numbered provision matching `label` is set out.

        Retrieval by meaning cannot be asked for "section 9D" — the phrase carries no
        semantics, and the FTS arm scores the digits against every other number in the
        corpus. A model that already knows the provision it wants should be able to name
        it, so this scans for the heading directly.

        Where the provision *is set out* and where it is *mentioned* are different
        things, and the mention is far more common: the SBP Act says "the Monetary Policy
        Committee established under section 9D" in its definitions, eight pages before
        section 9D itself. So the heading form is searched first and a cross-reference is
        a fallback, used only when nothing declares the provision at all.

        The heading pattern tolerates a run of digits immediately in front of the number.
        PDF extraction glues footnote markers onto the text that follows them, and this
        Act's section 9D is extracted as ``419D. Establishment of Monetary Policy
        Committee`` — footnote 41, then the heading, with nothing between them.
        """
        wanted = re.sub(
            r"^(?:sections?|sec\.?|s\.?|regulations?|reg\.?)\s*", "", label.strip(),
            flags=re.IGNORECASE,
        ).strip(" .()")
        if not wanted:
            return []
        escaped = re.escape(wanted)
        # "<number>. <Capitalised heading>" — a provision being declared.
        heading = re.compile(
            rf"(?:^|[^0-9A-Za-z])\d{{0,3}}{escaped}\s*[.:)]\s*[-–—]*\s*[A-Z]"
        )
        # "under section 9D" — a provision being referred to.
        mention = re.compile(
            rf"\b(?:sections?|regulations?|reg\.?|s\.?)\s*{escaped}\b", re.IGNORECASE
        )
        hits = [chunk for chunk in self._chunks if heading.search(chunk.text)]
        # A table of contents is heading-shaped by construction, so it matches the
        # heading pattern as well as the provision does. Asked for section 9D of the SBP
        # Act, the unranked scan returned the Act's contents page and nothing else — it
        # sits 39 chunks earlier, and under a budget "first" means "instead". What tells
        # the two apart is density: the contents chunk carries 20 headings, the provision
        # carries 4. A section number declares one provision, so only the least dense
        # matches are kept — ties included, since a document in parts may genuinely
        # declare the same number twice, but a contents page never ties with a provision.
        if len(hits) > 1:
            densities = {
                chunk.chunk_index: len(_HEADING_SCAN.findall(chunk.text))
                for chunk in hits
            }
            floor = min(densities.values())
            hits = [chunk for chunk in hits if densities[chunk.chunk_index] == floor]
        if not hits:
            hits = [chunk for chunk in self._chunks if mention.search(chunk.text)]
        return self._expand(hits, token_budget=token_budget)

    def search(self, query: str, *, limit: int, token_budget: int) -> list[dict]:
        if not query.strip() or not self._chunks or token_budget <= 0:
            return []

        query_tokens = expand_query_tokens(tokenize(query))
        scores: dict[int, float] = {}

        lexical_tokens = [tokenize(chunk.text) for chunk in self._chunks]
        if query_tokens and any(lexical_tokens):
            bm25 = BM25Okapi(lexical_tokens)
            lexical_scores = bm25.get_scores(query_tokens)
            ranked = sorted(
                range(len(lexical_scores)),
                key=lambda index: lexical_scores[index],
                reverse=True,
            )
            for rank, index in enumerate(ranked, start=1):
                if not set(query_tokens).intersection(lexical_tokens[index]):
                    continue
                key = self._chunks[index].chunk_index
                scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)

        try:
            vector_results = collection.query(
                query_embeddings=embedding_backend.embed_queries([query]),
                n_results=max(limit * 4, 20),
                where=self._where(),
                include=["metadatas"],
            )
            for rank, meta in enumerate(
                (vector_results.get("metadatas") or [[]])[0], start=1
            ):
                key = int(meta.get("chunk_index", -1))
                if key in self._by_index:
                    scores[key] = scores.get(key, 0.0) + 1.0 / (RRF_K + rank)
        except Exception:
            logger.info(
                "Scoped vector retrieval unavailable for %s; using lexical results",
                self._log_label(), exc_info=True,
            )

        # Contents pages last, as in `ScopedChatRetriever.search` and `order_evidence`.
        ranked_indexes = sorted(
            scores,
            key=lambda key: (is_listing_chunk(self._by_index[key].text), -scores[key]),
        )
        hits = [self._by_index[key] for key in ranked_indexes[: max(1, min(limit, 10))]]
        return self._expand(hits, token_budget=token_budget)

    def _payload(self, chunk: LawChunk) -> dict:
        return chunk.payload()

    def _expand(
        self,
        hits: list[LawChunk],
        *,
        token_budget: int,
        neighbours: int | None = None,
    ) -> list[dict]:
        """Widen each hit by its neighbours, merge overlaps, return in document order.

        Order is the document's, never relevance: consecutive chunks of one provision
        read as the provision only in the order it was written, and a caller that quotes
        them out of order quotes a rule that does not exist.
        """
        radius = self.neighbour_chunks if neighbours is None else max(0, neighbours)
        keep: set[int] = set()
        remaining = max(0, token_budget)
        for hit in hits:
            window = [
                index
                for index in range(hit.chunk_index - radius, hit.chunk_index + radius + 1)
                if index in self._by_index and index not in keep
            ]
            cost = sum(estimate_tokens(self._by_index[index].text) for index in window)
            # The hit itself is worth exceeding the budget for; its neighbours are not.
            if cost > remaining and keep:
                break
            keep.update(window)
            remaining -= cost
        return [self._payload(self._by_index[index]) for index in sorted(keep)]


class ScopedLawRetriever(IndexedDocumentRetriever):
    """Retrieve passages from inside one law or regulation — see the base class."""

    neighbour_chunks = LAW_NEIGHBOUR_CHUNKS

    def __init__(self, db: Session, document: RegDocument):
        self.document = document
        self.version = document.current_version
        super().__init__()

    def _where(self) -> dict:
        return {"document_id": self.document.id}

    def _log_label(self) -> str:
        return f"law {self.document.id}"


class ScopedAttachmentRetriever(IndexedDocumentRetriever):
    """Retrieve passages from inside one circular attachment — the annexure reader.

    What `read_attachment` runs on. The motivating failure: asked what Basel III counts
    as a "stable" deposit, the model correctly located the answer — "Part 1, section 4
    of C8-Annex.pdf", read off the annexure's own contents page — and then had no tool
    that could be pointed at a page or a paragraph of an attachment. `get_law_details`
    has had `section` for statutes since the P15 failure; this is the same verb for the
    134,000-character annexure behind a two-page covering letter.

    An attachment the store has not indexed yet is chunked from its extracted text at
    index geometry, so `page` and `section` work on it and the chunk indexes match what
    the store will hold once it is.
    """

    neighbour_chunks = ATTACHMENT_NEIGHBOUR_CHUNKS

    def __init__(self, attachment: Attachment):
        self.attachment = attachment
        super().__init__()

    def _where(self) -> dict:
        return {"attachment_id": self.attachment.id}

    def _log_label(self) -> str:
        return f"attachment {self.attachment.id}"

    def _fallback_document(self) -> dict | None:
        if not (self.attachment.content_text or "").strip():
            return None
        return document_from_attachment(self.attachment)

    def _payload(self, chunk: LawChunk) -> dict:
        # `ref` on an attachment chunk is "Page N", which `page` already says.
        item = {"chunk_index": chunk.chunk_index, "passage": chunk.text}
        if chunk.page is not None:
            item["page"] = chunk.page
        return item

    def chunk_ids(self, passages: list[dict]) -> list[str]:
        """The ledger keys of returned passages, in the index's own id form."""
        return [
            passage_key(self.attachment.id, item["chunk_index"])
            for item in passages
            if item.get("chunk_index") is not None
        ]


def build_chat_context(
    db: Session,
    circular_ids: list[str],
    query: str,
    max_context_tokens: int,
    *,
    sent_chunk_ids: set[str] | None = None,
) -> tuple[str, ScopedChatRetriever]:
    """The selected circulars as the model reads them: manifest, short texts, passages.

    `sent_chunk_ids` is the turn's passage ledger (see `ScopedChatRetriever.search`).
    What went out is left on `retriever.last_sent_chunk_ids` for the caller to record.
    """
    retriever = ScopedChatRetriever(db, circular_ids)
    if not retriever.circulars:
        return "No circulars selected for context.", retriever

    grounding_budget = max(1, max_context_tokens // 4)
    direct, direct_ids = retriever.direct_documents(grounding_budget)
    retrieved = retriever.search(
        focused_retrieval_query(query),
        limit=DEFAULT_RESULT_LIMIT,
        token_budget=grounding_budget,
        excluded_document_ids=direct_ids,
        sent_chunk_ids=sent_chunk_ids,
    )

    sections = ["Selected circular and attachment manifest:", retriever.attachment_manifest()]
    if direct:
        sections.extend(["Small documents included in full:", "\n\n".join(direct)])
    passages = [item for item in retrieved if not item.get("provided_earlier")]
    pointers = [item for item in retrieved if item.get("provided_earlier")]
    if passages:
        passage_text = "\n\n".join(
            f"Source: {item['citation']}\nPassage:\n{item['passage']}"
            for item in passages
        )
        sections.extend(["Automatically retrieved passages for this question:", passage_text])
    if pointers:
        pointer_text = "\n".join(
            f"- {item['citation']} chunk {item['chunk_index']}"
            + (f" (page {item['page']})" if item.get("page") is not None else "")
            for item in pointers
        )
        sections.extend([
            "Matching passages already provided earlier in this conversation (not repeated):",
            pointer_text,
        ])
    return "\n\n".join(sections), retriever
