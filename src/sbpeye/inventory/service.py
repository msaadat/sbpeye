"""Orchestration. Owns search semantics; adapters own transport.

The CLI, a future HTTP route, and a future MCP tool all call :meth:`InventorySearchService.search`
and serialize what comes back. None of them may re-implement any part of the pipeline —
that is what keeps one set of numbers behind every surface.
"""

import logging

import numpy as np
from sqlalchemy.orm import Session

from ..models import Circular, RegDocument
from . import adjudicate as adjudicate_module
from . import extract as extract_module
from .adjudicate import VERDICT_INCLUDED, VERDICT_UNDETERMINED, adjudicate
from .corpus import build_scope
from .extract import extract_spans, resolve_locator
from .fingerprint import CHUNKER_VERSION, embedding_fingerprint
from .index import SNAPSHOT_CACHE
from .ledger import snapshot_id as compute_snapshot_id
from .retrieval import (
    dense_band,
    generate_hyde_passage,
    lexical_candidates,
    union_candidates,
)
from .schemas import (
    Coverage,
    Evidence,
    ExcludedResult,
    InventoryResult,
    InventorySearchRequest,
    InventorySearchResponse,
    RetrievalPolicy,
    SemanticIndexIncomplete,
)
from .terms import TERM_PROMPT_VERSION, build_term_set

logger = logging.getLogger(__name__)


class InventorySearchService:
    """Exhaustive inventory search over circulars and laws."""

    def __init__(self, collection, embedding_backend, embedding_config, llm=None):
        self._collection = collection
        self._embedding_backend = embedding_backend
        self._embedding_config = embedding_config
        self._llm = llm

    # ---------------------------------------------------------------- helpers

    def _query_vectors(self, texts: list[str]) -> np.ndarray:
        vectors = np.asarray(
            self._embedding_backend.embed_queries(texts), dtype=np.float32
        )
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return vectors / norms

    @staticmethod
    def _passes_filters(record, request: InventorySearchRequest, kind: str) -> bool:
        filters = request.filters
        if kind == "circular":
            if filters.departments and (record.department or "") not in filters.departments:
                return False
            if filters.circular_statuses and (record.status or "") not in filters.circular_statuses:
                return False
            year = record.date.year if record.date else None
            if filters.start_year and (year is None or year < filters.start_year):
                return False
            if filters.end_year and (year is None or year > filters.end_year):
                return False
            return True
        if filters.law_types and (record.doc_type or "") not in filters.law_types:
            return False
        if record.delisted_at is not None and not filters.include_delisted_laws:
            return False
        return True

    # ------------------------------------------------------------------ main

    def search(
        self, request: InventorySearchRequest, db: Session
    ) -> InventorySearchResponse:
        request.validate()
        warnings: list[str] = []

        scope = build_scope(
            db,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
            include_delisted_laws=request.filters.include_delisted_laws,
        )
        snapshot_key = compute_snapshot_id(db)
        # Read per call, never cached on the instance: a long-lived process would
        # otherwise keep reporting coverage from whenever it first ran a search.
        indexed_source_ids = self._indexed_source_ids(db)
        stale_or_missing = sum(
            1 for source in scope.searchable
            if source.source_id not in indexed_source_ids
        )

        coverage = Coverage(
            logical_documents_in_scope=len(scope.logical_documents()),
            source_units_expected=len(scope.searchable),
            source_units_indexed=len(scope.searchable) - stale_or_missing,
            excluded_by_design=dict(scope.excluded_by_design),
            stale_or_missing_index=stale_or_missing,
        )
        for source in scope.unsearchable:
            status = source.unsearchable_status or "unknown"
            coverage.unsearchable[status] = coverage.unsearchable.get(status, 0) + 1

        coverage.is_complete = stale_or_missing == 0 and not coverage.unsearchable
        if request.require_complete_coverage and not coverage.is_complete:
            raise SemanticIndexIncomplete(
                f"{stale_or_missing} source(s) stale or missing, "
                f"{sum(coverage.unsearchable.values())} unsearchable"
            )

        # ---- layer 0: term set -------------------------------------------
        term_set, term_warnings = build_term_set(
            request.query,
            request.alternate_queries,
            llm=self._llm,
            generate=request.generate_terms,
        )
        warnings.extend(term_warnings)

        # ---- layer 1: retrieval ------------------------------------------
        lexical = lexical_candidates(
            db,
            term_set.all_terms,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
        )

        hyde_passage = ""
        query_texts = [request.query, *request.alternate_queries]
        if request.use_hyde and self._llm is not None:
            hyde_passage, hyde_warnings = generate_hyde_passage(request.query, self._llm)
            warnings.extend(hyde_warnings)
            if hyde_passage:
                query_texts.append(hyde_passage)

        snapshot = SNAPSHOT_CACHE.get(self._collection, snapshot_key)
        dense = dense_band(
            snapshot,
            self._query_vectors(query_texts),
            request.semantic_band,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
        )

        candidates, truncated = union_candidates(lexical, dense, request.max_candidates)
        coverage.candidates_lexical = len(lexical)
        coverage.candidates_semantic = len(dense)
        coverage.candidates_union = len(candidates) + truncated
        coverage.candidates_truncated = truncated
        if truncated:
            warnings.append(
                f"{truncated} candidate(s) beyond max_candidates were not adjudicated; "
                "lexical matches were retained ahead of semantic-only candidates"
            )

        records = self._load_records(db, candidates)
        candidates = [
            candidate for candidate in candidates
            if candidate.key in records
            and self._passes_filters(
                records[candidate.key], request, candidate.logical_kind
            )
        ]

        # ---- layer 2: adjudication ---------------------------------------
        passages = [self._best_passage(snapshot, c, records) for c in candidates]
        if request.skip_adjudication:
            verdicts = [
                adjudicate_module.Verdict(VERDICT_INCLUDED, "adjudication skipped")
                for _ in candidates
            ]
        else:
            verdicts = adjudicate(
                self._llm,
                request.query,
                [(self._label(records[c.key], c.logical_kind), p)
                 for c, p in zip(candidates, passages)],
            )

        included, excluded = [], []
        for candidate, verdict in zip(candidates, verdicts):
            record = records[candidate.key]
            if verdict.verdict == VERDICT_INCLUDED:
                included.append((candidate, verdict))
            else:
                if verdict.verdict == VERDICT_UNDETERMINED:
                    coverage.adjudicated_undetermined += 1
                else:
                    coverage.adjudicated_excluded += 1
                excluded.append(ExcludedResult(
                    document_id=candidate.document_id,
                    result_kind=candidate.logical_kind,
                    reference=self._reference(record, candidate.logical_kind),
                    title=record.title or "",
                    matched_via=sorted(candidate.matched_via),
                    judge_reason=verdict.reason,
                ))
        coverage.adjudicated_included = len(included)

        # ---- layer 3: extraction -----------------------------------------
        results = self._build_results(
            request, snapshot, included, records, warnings
        )

        coverage.warnings = warnings
        return InventorySearchResponse(
            query=request.query,
            snapshot_id=snapshot_key,
            retrieval_policy=RetrievalPolicy(
                term_set=term_set.all_terms,
                term_set_source=term_set.as_source_map(),
                hyde_passage=hyde_passage,
                embedding_model=self._embedding_config.model,
                embedding_fingerprint=embedding_fingerprint(self._embedding_config),
                chunker_version=CHUNKER_VERSION,
                semantic_band=request.semantic_band,
                judge_model=getattr(self._llm, "model_name", "") if self._llm else "",
                judge_prompt_version=(
                    f"{TERM_PROMPT_VERSION}/{adjudicate_module.JUDGE_PROMPT_VERSION}/"
                    f"{extract_module.EXTRACT_PROMPT_VERSION}"
                ),
            ),
            coverage=coverage,
            matched_documents=len(results),
            results=results,
            excluded=excluded,
            truncated=bool(truncated),
        )

    # ------------------------------------------------------------- internals

    @staticmethod
    def _indexed_source_ids(db: Session) -> set[str]:
        """Source ids the ledger currently considers fully indexed."""
        from ..models import SemanticIndexSource

        return {
            row[0]
            for row in db.query(SemanticIndexSource.source_id)
            .filter(SemanticIndexSource.status == "indexed")
            .all()
        }

    @staticmethod
    def _label(record, kind: str) -> str:
        if kind == "circular":
            return f"{record.reference or ''} {record.title or ''}".strip()
        return record.title or ""

    @staticmethod
    def _reference(record, kind: str) -> str:
        return (record.reference or "") if kind == "circular" else (record.doc_type or "")

    def _load_records(self, db: Session, candidates) -> dict[tuple[str, str], object]:
        circular_ids = [c.document_id for c in candidates if c.logical_kind == "circular"]
        law_ids = [c.document_id for c in candidates if c.logical_kind == "law"]
        records: dict[tuple[str, str], object] = {}
        for start in range(0, len(circular_ids), 500):
            batch = circular_ids[start:start + 500]
            for row in db.query(Circular).filter(Circular.id.in_(batch)).all():
                records[("circular", row.id)] = row
        for start in range(0, len(law_ids), 500):
            batch = law_ids[start:start + 500]
            for row in db.query(RegDocument).filter(RegDocument.id.in_(batch)).all():
                records[("law", row.id)] = row
        return records

    @staticmethod
    def _best_passage(snapshot, candidate, records) -> str:
        if candidate.chunk_indices:
            return snapshot.documents[candidate.chunk_indices[0]]
        # Lexical-only hit: the dense arm never scored it, so fall back to the head of
        # the document's own text rather than sending the judge nothing.
        record = records.get(candidate.key)
        text = getattr(record, "content_text", "") or ""
        if not text and getattr(record, "current_version", None) is not None:
            text = record.current_version.content_text or ""
        return text[:1500]

    def _build_results(
        self, request, snapshot, included, records, warnings
    ) -> list[InventoryResult]:
        passages: list[str] = []
        plan: list[tuple[int, int]] = []  # (result index, chunk index)
        for result_index, (candidate, _) in enumerate(included):
            for chunk_index in candidate.chunk_indices[:request.evidence_per_result]:
                plan.append((result_index, chunk_index))
                passages.append(snapshot.documents[chunk_index])

        extractions = (
            extract_spans(self._llm, request.query, passages)
            if request.extract_spans and self._llm is not None
            else [extract_module.Extraction("", False) for _ in passages]
        )
        unverified = sum(1 for e in extractions if passages and not e.verified)
        if unverified and request.extract_spans and self._llm is not None:
            warnings.append(
                f"{unverified} extracted span(s) could not be verified against stored "
                "text; the full passage is returned for those"
            )

        evidence_by_result: dict[int, list[Evidence]] = {}
        for (result_index, chunk_index), extraction in zip(plan, extractions):
            metadata = snapshot.metadatas[chunk_index]
            passage = snapshot.documents[chunk_index]
            locator = resolve_locator(metadata)
            source_kind = metadata.get("doc_type") or "circular"
            evidence_by_result.setdefault(result_index, []).append(Evidence(
                source_kind=source_kind,
                source_id=(
                    metadata.get("attachment_id")
                    or metadata.get("version_id")
                    or metadata.get("circular_id")
                    or metadata.get("document_id")
                    or ""
                ),
                source_label=metadata.get("filename") or metadata.get("title") or "",
                score=0.0,
                passage=passage,
                extracted_text=extraction.text or passage,
                extraction_verified=extraction.verified,
                **locator,
            ))

        results: list[InventoryResult] = []
        for result_index, (candidate, verdict) in enumerate(included):
            record = records[candidate.key]
            evidence = evidence_by_result.get(result_index, [])
            if candidate.logical_kind == "circular":
                results.append(InventoryResult(
                    result_kind="circular",
                    document_id=candidate.document_id,
                    title=record.title or "",
                    reference=record.reference or "",
                    department=record.department or "",
                    date=record.date.strftime("%Y-%m-%d") if record.date else None,
                    status=record.status or "",
                    matched_via=sorted(candidate.matched_via),
                    matched_terms=candidate.matched_terms,
                    semantic_score=candidate.semantic_score,
                    judge_reason=verdict.reason,
                    evidence=evidence,
                ))
            else:
                version = record.current_version
                parent = record.parent
                results.append(InventoryResult(
                    result_kind="law",
                    document_id=candidate.document_id,
                    title=record.title or "",
                    matched_via=sorted(candidate.matched_via),
                    matched_terms=candidate.matched_terms,
                    semantic_score=candidate.semantic_score,
                    judge_reason=verdict.reason,
                    evidence=evidence,
                    law_type=record.doc_type or "",
                    version_id=version.id if version else "",
                    version_label=(version.version_label or "") if version else "",
                    parent_id=parent.id if parent else "",
                    parent_title=(parent.title or "") if parent else "",
                ))

        results.sort(key=lambda r: (
            0 if "lexical" in r.matched_via else 1,
            -r.semantic_score,
            r.result_kind,
            r.document_id,
        ))
        return results
