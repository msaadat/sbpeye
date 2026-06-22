import logging
from dataclasses import dataclass

from rank_bm25 import BM25Okapi
from sqlalchemy.orm import Session, joinedload

from .database import collection, embedding_backend
from .checklist import prepare_reference_chunks
from .documents import build_corpus
from .models import Circular
from .search import expand_query_tokens, tokenize


logger = logging.getLogger(__name__)

RRF_K = 60
DEFAULT_RESULT_LIMIT = 5


def estimate_tokens(text: str) -> int:
    """Estimate tokens without tying chat retrieval to one model tokenizer."""
    return max(1, (len(text) + 3) // 4)


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


@dataclass(frozen=True)
class ScopedChunk:
    chunk_id: str
    circular_id: str
    document_id: str
    document_type: str
    label: str
    text: str
    chunk_index: int

    @property
    def citation(self) -> str:
        kind = "attachment" if self.document_type == "attachment" else "circular"
        return f"[[{kind}:{self.document_id}|{self.label}]]"

    def payload(self) -> dict:
        return {
            "source_type": self.document_type,
            "source_label": self.label,
            "passage": self.text,
            "citation": self.citation,
            "chunk_index": self.chunk_index,
        }


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

    def _build_chunks(self) -> list[ScopedChunk]:
        chunks: list[ScopedChunk] = []
        for circular in self.circulars:
            for document in build_corpus(circular):
                prepared = prepare_reference_chunks(document)
                for index, item in enumerate(prepared):
                    chunks.append(
                        ScopedChunk(
                            chunk_id=f"{document['doc_id']}__chunk_{index}",
                            circular_id=circular.id,
                            document_id=document["doc_id"],
                            document_type=document["doc_type"],
                            label=document["doc_label"],
                            text=item["text"],
                            chunk_index=index,
                        )
                    )
        return chunks

    def attachment_manifest(self) -> str:
        sections: list[str] = []
        for circular in self.circulars:
            circular_label = circular.reference or circular.title
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
    ) -> list[dict]:
        excluded_document_ids = excluded_document_ids or set()
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

        ranked_chunks = sorted(
            (self._chunk_by_id[chunk_id] for chunk_id in scores),
            key=lambda chunk: scores[chunk.chunk_id],
            reverse=True,
        )
        results: list[dict] = []
        remaining = token_budget
        for chunk in ranked_chunks:
            if len(results) >= max(1, min(limit, 10)) or remaining <= 0:
                break
            text = chunk.text
            tokens = estimate_tokens(text)
            if tokens > remaining:
                if results:
                    continue
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
            remaining -= tokens
        return results


def build_chat_context(
    db: Session,
    circular_ids: list[str],
    query: str,
    max_context_tokens: int,
) -> tuple[str, ScopedChatRetriever]:
    retriever = ScopedChatRetriever(db, circular_ids)
    if not retriever.circulars:
        return "No circulars selected for context.", retriever

    grounding_budget = max(1, max_context_tokens // 4)
    direct, direct_ids = retriever.direct_documents(grounding_budget)
    retrieved = retriever.search(
        query,
        limit=DEFAULT_RESULT_LIMIT,
        token_budget=grounding_budget,
        excluded_document_ids=direct_ids,
    )

    sections = ["Selected circular and attachment manifest:", retriever.attachment_manifest()]
    if direct:
        sections.extend(["Small documents included in full:", "\n\n".join(direct)])
    if retrieved:
        passage_text = "\n\n".join(
            f"Source: {item['citation']}\nPassage:\n{item['passage']}"
            for item in retrieved
        )
        sections.extend(["Automatically retrieved passages for this question:", passage_text])
    return "\n\n".join(sections), retriever
