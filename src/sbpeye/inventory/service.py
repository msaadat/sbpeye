"""Orchestration. Owns search semantics; adapters own transport.

The CLI, a future HTTP route, and a future MCP tool all call :meth:`InventorySearchService.search`
and serialize what comes back. None of them may re-implement any part of the pipeline —
that is what keeps one set of numbers behind every surface.
"""

import logging
from datetime import datetime

import numpy as np
from sqlalchemy.orm import Session

from ..models import Circular, RegDocument
from ..llm_debug import emit_event, trace_operation
from . import adjudicate as adjudicate_module
from . import extract as extract_module
from .adjudicate import VERDICT_INCLUDED, VERDICT_UNDETERMINED, adjudicate
from .corpus import build_scope
from .extract import extract_spans, resolve_locator
from .fingerprint import CHUNKER_VERSION, embedding_fingerprint
from .index import SNAPSHOT_CACHE
from .ledger import indexed_source_ids as ledger_indexed_source_ids
from .ledger import lexical_indexed_documents
from .ledger import recorded_fingerprints
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
    EmbeddingFingerprintMismatch,
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
    def _allowed_documents(
        db: Session, request: InventorySearchRequest
    ) -> tuple[set[str] | None, set[str] | None]:
        """Resolve filters to the id sets that may be searched, before any retrieval.

        Filtering afterwards would leave `candidates_lexical` and `candidates_semantic`
        describing documents the caller excluded, and would shrink the semantic band
        below its requested size. Both arms therefore take these sets and never see the
        rest of the corpus. ``None`` means "no filter on this corpus".
        """
        filters = request.filters

        circular_ids: set[str] | None = None
        if request.include_circulars:
            query = db.query(Circular.id)
            constrained = False
            if filters.departments:
                query = query.filter(Circular.department.in_(filters.departments))
                constrained = True
            if filters.circular_statuses:
                query = query.filter(Circular.status.in_(filters.circular_statuses))
                constrained = True
            if filters.start_year:
                query = query.filter(Circular.date >= datetime(filters.start_year, 1, 1))
                constrained = True
            if filters.end_year:
                query = query.filter(
                    Circular.date <= datetime(filters.end_year, 12, 31, 23, 59, 59)
                )
                constrained = True
            if constrained:
                circular_ids = {row[0] for row in query.all()}

        law_ids: set[str] | None = None
        if request.include_laws:
            query = db.query(RegDocument.id)
            constrained = False
            if filters.law_types:
                query = query.filter(RegDocument.doc_type.in_(filters.law_types))
                constrained = True
            if not filters.include_delisted_laws:
                query = query.filter(RegDocument.delisted_at.is_(None))
                constrained = True
            if constrained:
                law_ids = {row[0] for row in query.all()}

        return circular_ids, law_ids

    # ------------------------------------------------------------------ main

    def search(
        self, request: InventorySearchRequest, db: Session
    ) -> InventorySearchResponse:
        if self._llm is None:
            return self._search(request, db)
        with trace_operation(
            "inventory.search", "implicit", target_kind="inventory",
            metadata={"query": request.query},
        ):
            response = self._search(request, db)
            payload = response.to_dict() if hasattr(response, "to_dict") else response
            emit_event("normalized_result", payload, stage="inventory.result")
            emit_event("persisted_result", {
                "persisted": False, "result": payload,
            }, stage="inventory.result")
            return response

    def _search(
        self, request: InventorySearchRequest, db: Session
    ) -> InventorySearchResponse:
        request.validate()
        warnings: list[str] = []

        allowed_circulars, allowed_laws = self._allowed_documents(db, request)
        allowed_keys = {"circular": allowed_circulars, "law": allowed_laws}
        scope = build_scope(
            db,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
            circular_ids=allowed_circulars,
            law_document_ids=allowed_laws,
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

        in_scope = scope.logical_documents()
        lexical_indexed = lexical_indexed_documents(
            db,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
        )
        coverage.lexical_documents_expected = len(in_scope)
        coverage.lexical_documents_indexed = len(in_scope & lexical_indexed)
        coverage.lexical_gaps = len(in_scope - lexical_indexed)
        if coverage.lexical_gaps:
            warnings.append(
                f"{coverage.lexical_gaps} document(s) in scope have no FTS row and are "
                "unreachable by the lexical arm"
            )

        coverage.is_complete = (
            stale_or_missing == 0
            and coverage.lexical_gaps == 0
            and not coverage.unsearchable
        )
        if request.require_complete_coverage and not coverage.is_complete:
            raise SemanticIndexIncomplete(
                f"{stale_or_missing} source(s) stale or missing, "
                f"{sum(coverage.unsearchable.values())} unsearchable"
            )

        self._assert_fingerprint_current(db)

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
            allowed_keys=allowed_keys,
        )

        hyde_passage = ""
        query_texts = [request.query, *request.alternate_queries]
        if request.use_hyde and self._llm is not None:
            hyde_passage, hyde_warnings = generate_hyde_passage(request.query, self._llm)
            warnings.extend(hyde_warnings)
            if hyde_passage:
                query_texts.append(hyde_passage)

        query_vectors = self._query_vectors(query_texts)
        snapshot = SNAPSHOT_CACHE.get(
            self._collection, snapshot_key, indexed_source_ids
        )
        if snapshot.excluded_orphans:
            warnings.append(
                f"{snapshot.excluded_orphans} stored chunk(s) had no current ledger "
                "entry and were excluded from scoring"
            )
        self._assert_embeddings_comparable(snapshot, query_vectors)
        dense = dense_band(
            snapshot,
            query_vectors,
            request.semantic_band,
            include_circulars=request.include_circulars,
            include_laws=request.include_laws,
            allowed_keys=allowed_keys,
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

        # Filters were applied before retrieval, so nothing is discarded here beyond
        # candidates whose database row has disappeared since the index was written.
        records = self._load_records(db, candidates)
        candidates = [c for c in candidates if c.key in records]
        self._locate_lexical_chunks(
            snapshot, candidates, max(1, request.evidence_per_result)
        )

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
        # Cap before extraction, not after: every dropped row would otherwise cost an
        # LLM call for text nobody sees.
        results_truncated = max(0, len(included) - request.max_results)
        if results_truncated:
            included = included[:request.max_results]
            warnings.append(
                f"{results_truncated} matching document(s) beyond max_results were not "
                "returned; raise max_results to see the complete inventory"
            )
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
                spans_extracted=bool(request.extract_spans and self._llm is not None),
                judge_prompt_version=(
                    f"{TERM_PROMPT_VERSION}/{adjudicate_module.JUDGE_PROMPT_VERSION}/"
                    f"{extract_module.EXTRACT_PROMPT_VERSION}"
                ),
            ),
            coverage=coverage,
            matched_documents=len(results),
            results_truncated=results_truncated,
            results=results,
            excluded=excluded,
            truncated=bool(truncated or results_truncated),
        )

    # ------------------------------------------------------------- internals

    def _assert_fingerprint_current(self, db: Session) -> None:
        """Refuse to score a query against vectors built by a different model.

        Cosine between two embedding spaces is a number with no meaning, and a dimension
        change would instead fail deep inside the matmul. Either way the caller must be
        told to re-index rather than handed a plausible-looking inventory.
        """
        current = embedding_fingerprint(self._embedding_config)
        recorded = recorded_fingerprints(db)
        if recorded and current not in recorded:
            raise EmbeddingFingerprintMismatch(
                f"index was built with {sorted(recorded)}, "
                f"but {self._embedding_config.model} fingerprints as {current}; "
                "re-index before searching"
            )

    @staticmethod
    def _assert_embeddings_comparable(snapshot, query_vectors) -> None:
        if snapshot.size and snapshot.dimension != int(query_vectors.shape[1]):
            raise EmbeddingFingerprintMismatch(
                f"stored vectors are {snapshot.dimension}-dimensional but the query "
                f"embeds to {int(query_vectors.shape[1])}; re-index before searching"
            )

    @staticmethod
    def _indexed_source_ids(db: Session) -> set[str]:
        """Source ids the ledger currently considers fully indexed."""
        return ledger_indexed_source_ids(db)

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
    def _locate_lexical_chunks(snapshot, candidates, limit: int) -> None:
        """Point lexical-only candidates at the chunks that actually contain their terms.

        A document found by the lexical arm alone has no `chunk_indices`, because only
        the dense arm assigns them. Two things then go wrong, and both were visible on
        the first live end-to-end run:

        * the judge was shown the head of the document instead of the matching passage,
          so a long framework with one call-centre paragraph on page 40 was judged on
          its cover page and correctly answered "no" to a question about text the model
          never saw;
        * the result carried no evidence at all, which acceptance criterion 5 forbids.

        The lexical arm is the recall backbone, so this is not a corner case — it is the
        common path for exactly the documents the inventory exists to find.
        """
        wanted = {c.key: c for c in candidates if not c.chunk_indices}
        if not wanted:
            return

        found: dict[tuple[str, str], list[int]] = {}
        for index, key in enumerate(snapshot.logical_keys()):
            candidate = wanted.get(key)
            if candidate is None or len(found.get(key, ())) >= limit:
                continue
            haystack = (snapshot.documents[index] or "").lower()
            if any(term.lower() in haystack for term in candidate.matched_terms):
                found.setdefault(key, []).append(index)

        for key, indices in found.items():
            wanted[key].chunk_indices = indices

    @staticmethod
    def _best_passage(snapshot, candidate, records) -> str:
        if candidate.chunk_indices:
            return snapshot.documents[candidate.chunk_indices[0]]
        # No chunk contains the term literally — it matched the FTS-tokenized title or
        # reference, or the chunk text differs from what was indexed for search. Fall
        # back to the head of the document rather than sending the judge nothing.
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
