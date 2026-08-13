"""Tests for exhaustive inventory search (docs/INVENTORY_SEARCH_PLAN.md).

The properties pinned here are the ones the feature's completeness claim rests on:
recall is mechanical and uncapped, generated terms can only widen the search, quoted
text is verified against stored source text, and nothing is silently dropped.

Chroma, the embedding backend, and the LLM are all stubbed, so runs are offline.
"""

from datetime import datetime

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from sbpeye.database import Base
from sbpeye.models import (
    Attachment,
    Circular,
    RegDocument,
    RegDocumentVersion,
    SemanticIndexSource,
)
from sbpeye.inventory import corpus, ledger, retrieval, terms
from sbpeye.inventory.adjudicate import VERDICT_INCLUDED, VERDICT_UNDETERMINED, adjudicate
from sbpeye.inventory.extract import resolve_locator, verify_span
from sbpeye.inventory.index import CorpusEmbeddingSnapshot, load_snapshot
from sbpeye.inventory.schemas import InvalidQuery, InventorySearchRequest
from sbpeye.inventory.service import InventorySearchService
from sbpeye.search import backfill_fts, backfill_laws_fts


# --------------------------------------------------------------------- doubles


class FakeCollection:
    def __init__(self):
        self.records: dict[str, dict] = {}

    def add(self, documents, embeddings, ids, metadatas):
        for doc, emb, id_, meta in zip(documents, embeddings, ids, metadatas):
            self.records[id_] = {"document": doc, "embedding": emb, "metadata": meta}

    def get(self, ids=None, where=None, limit=None, offset=None, include=None):
        items = list(self.records.items())
        if where:
            (key, value), = where.items()
            items = [(i, r) for i, r in items if r["metadata"].get(key) == value]
        if offset:
            items = items[offset:]
        if limit is not None:
            items = items[:limit]
        include = include or []
        page = {"ids": [i for i, _ in items]}
        if "metadatas" in include:
            page["metadatas"] = [r["metadata"] for _, r in items]
        if "documents" in include:
            page["documents"] = [r["document"] for _, r in items]
        if "embeddings" in include:
            page["embeddings"] = [r["embedding"] for _, r in items]
        return page

    def delete(self, ids):
        for id_ in ids:
            self.records.pop(id_, None)

    def count(self):
        return len(self.records)


class FakeBackend:
    """Deterministic 3-d embeddings: axis 0 = 'aml', axis 1 = 'audit', axis 2 = noise."""

    @staticmethod
    def _vector(text: str):
        lowered = text.lower()
        return [
            float(lowered.count("aml") + lowered.count("money laundering")),
            float(lowered.count("audit")),
            1.0,
        ]

    def embed_documents(self, documents):
        return [self._vector(d) for d in documents]

    def embed_queries(self, queries):
        return [self._vector(q) for q in queries]


class FakeConfig:
    provider = "fastembed"
    model = "test-model"


class FakeLLM:
    """Scriptable LLM. Records prompts so tests can assert what it was asked."""

    def __init__(self, terms=None, verdicts=None, span=None, fail=False):
        self._terms = terms or []
        self._verdicts = verdicts
        self._span = span
        self._fail = fail
        self.calls: list[str] = []

    model_name = "fake-judge"

    def complete_json(self, system_prompt, user_prompt, *, json_schema):
        self.calls.append(user_prompt)
        if self._fail:
            raise RuntimeError("llm down")
        properties = json_schema.get("properties", {})
        if "terms" in properties:
            return {"terms": list(self._terms)}
        if "passage" in properties:
            return {"passage": "The bank shall maintain AML controls."}
        if "verdicts" in properties:
            ids = [
                int(line.split("]")[0][1:])
                for line in user_prompt.splitlines()
                if line.startswith("[")
            ]
            if self._verdicts is None:
                return {"verdicts": [
                    {"id": i, "discusses": True, "reason": "on topic"} for i in ids
                ]}
            return {"verdicts": [
                {"id": i, "discusses": self._verdicts, "reason": "scripted"} for i in ids
            ]}
        return {"span": self._span if self._span is not None else ""}


# ----------------------------------------------------------------- fixtures


def make_session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False)()


def add_circular(db, cid, reference, title, body, department="BPRD", year=2021):
    db.add(Circular(
        id=cid, reference=reference, title=title, department=department,
        url=f"https://example.test/{cid}", content_text=body,
        date=datetime(year, 1, 1), status="active",
    ))


def add_law(db, lid, title, body, doc_type="regulation"):
    db.add(RegDocument(id=lid, title=title, doc_type=doc_type,
                       source_url=f"https://example.test/{lid}"))
    db.add(RegDocumentVersion(
        id=f"{lid}-v1", document_id=lid, content_hash=f"h-{lid}", file_type="pdf",
        content_text=body, is_current=1, is_vectorized=1,
    ))


@pytest.fixture
def corpus_db():
    db = make_session()
    add_circular(db, "c-aml", "BPRD Circular No. 07 of 2019",
                 "Anti-Money Laundering Regulations",
                 "Banks shall implement anti-money laundering and AML controls.")
    add_circular(db, "c-audit", "BPRD Circular No. 05 of 2021",
                 "Corporate Governance",
                 "The internal audit function shall report to the audit committee.")
    add_circular(db, "c-mention", "BSD Circular No. 37 of 2001",
                 "Non Performing Loans Database",
                 "Reporting of NPLs. Branches shall also observe AML instructions "
                 "when onboarding. Remaining paragraphs concern loan classification.")
    add_circular(db, "c-none", "DMMD Circular No. 09 of 2019",
                 "Statutory Liquidity Requirement",
                 "Banks shall maintain the prescribed liquidity ratio.")
    add_law(db, "l-aml", "AML/CFT Regulations",
            "Customer due diligence and anti-money laundering obligations apply.")
    db.commit()
    backfill_fts(db, force=True)
    backfill_laws_fts(db, force=True)
    return db


@pytest.fixture
def indexed(corpus_db):
    """Index the corpus into a fake collection and reconcile the ledger."""
    from sbpeye.checklist import prepare_index_chunks

    collection = FakeCollection()
    backend = FakeBackend()
    for circular in corpus_db.query(Circular).all():
        chunks = prepare_index_chunks({
            "doc_id": circular.id, "doc_type": "circular",
            "doc_label": circular.reference, "text": circular.content_text,
            "file_type": "html",
        })
        collection.add(
            documents=[c["text"] for c in chunks],
            embeddings=backend.embed_documents([c["embed_text"] for c in chunks]),
            ids=[f"{circular.id}__chunk_{i}" for i in range(len(chunks))],
            metadatas=[{
                "circular_id": circular.id, "doc_type": "circular",
                "title": circular.title, "ref": c["ref"],
                "source_start": c["source_start"], "source_end": c["source_end"],
            } for c in chunks],
        )
    for document in corpus_db.query(RegDocument).all():
        version = document.current_version
        chunks = prepare_index_chunks({
            "doc_id": version.id, "doc_type": "law", "doc_label": document.title,
            "text": version.content_text, "file_type": "pdf",
        })
        collection.add(
            documents=[c["text"] for c in chunks],
            embeddings=backend.embed_documents([c["embed_text"] for c in chunks]),
            ids=[f"{version.id}__chunk_{i}" for i in range(len(chunks))],
            metadatas=[{
                "kind": "law", "doc_type": "law", "document_id": document.id,
                "version_id": version.id, "title": document.title, "ref": c["ref"],
                "source_start": c["source_start"], "source_end": c["source_end"],
            } for c in chunks],
        )
    ledger.reconcile(corpus_db, collection, FakeConfig(), write=True)
    return corpus_db, collection


def make_service(collection, llm=None):
    return InventorySearchService(collection, FakeBackend(), FakeConfig(), llm=llm)


# ------------------------------------------------------------------ corpus


def test_scope_excludes_circular_backed_and_manifest_laws(corpus_db):
    db = corpus_db
    db.add(RegDocument(id="l-dupe", title="Backed by circular", doc_type="regulation",
                       circular_id="c-aml"))
    db.add(RegDocument(id="l-manifest", title="Container", doc_type="law"))
    db.add(RegDocumentVersion(id="l-manifest-v1", document_id="l-manifest",
                              content_hash="h-m", file_type="manifest",
                              content_text="", is_current=1))
    db.commit()

    scope = corpus.build_scope(db)

    assert scope.excluded_by_design["law_backed_by_circular"] == 1
    assert scope.excluded_by_design["law_manifest"] == 1
    assert ("law", "l-dupe") not in scope.logical_documents()


def test_attachment_extraction_error_is_not_a_semantic_non_match(corpus_db):
    corpus_db.add(Attachment(
        id="a-bad", circular_id="c-aml", filename="scan.pdf",
        original_url="https://example.test/a", file_type="pdf",
        content_text=None, extraction_status="error", extraction_error="ocr failed",
    ))
    corpus_db.commit()

    scope = corpus.build_scope(corpus_db)
    bad = [s for s in scope.unsearchable if s.source_id == "a-bad"]

    assert bad and bad[0].unsearchable_status == corpus.STATUS_EXTRACTION_ERROR
    assert bad[0].unsearchable_detail == "ocr failed"


# ------------------------------------------------------------------ ledger


def test_ledger_marks_sources_indexed_when_chunk_counts_match(indexed):
    db, _ = indexed
    rows = {r.source_id: r for r in db.query(SemanticIndexSource).all()}

    assert rows["c-aml"].status == corpus.STATUS_INDEXED
    assert rows["c-aml"].expected_chunks == rows["c-aml"].indexed_chunks > 0


def test_ledger_detects_a_source_whose_chunks_are_missing(indexed):
    db, collection = indexed
    for chunk_id in [i for i in collection.records if i.startswith("c-audit__")]:
        collection.delete([chunk_id])

    report = ledger.reconcile(db, collection, FakeConfig(), write=True)
    row = db.query(SemanticIndexSource).filter_by(source_id="c-audit").one()

    assert row.status == corpus.STATUS_STALE
    assert not report.is_complete


def test_ledger_reports_orphan_chunks(indexed):
    db, collection = indexed
    collection.add(documents=["ghost"], embeddings=[[0.0, 0.0, 1.0]],
                   ids=["c-deleted__chunk_0"],
                   metadatas=[{"circular_id": "c-deleted", "doc_type": "circular"}])

    report = ledger.reconcile(db, collection, FakeConfig(), write=False)

    assert report.orphan_chunks == 1
    assert not report.is_complete


def test_snapshot_id_changes_when_content_changes(indexed):
    db, collection = indexed
    before = ledger.snapshot_id(db)

    db.query(Circular).filter_by(id="c-none").one().content_text = "different text"
    db.commit()
    ledger.reconcile(db, collection, FakeConfig(), write=True)

    assert ledger.snapshot_id(db) != before


# ------------------------------------------------------------------- terms


def test_generated_terms_can_only_widen_the_set():
    llm = FakeLLM(terms=["anti-money laundering", "CDD"])

    term_set, warnings = terms.build_term_set("AML", llm=llm)

    assert "AML" in term_set.all_terms
    assert "anti-money laundering" in term_set.all_terms
    assert not warnings


def test_llm_failure_leaves_the_verbatim_query_searchable():
    term_set, warnings = terms.build_term_set("AML", llm=FakeLLM(fail=True))

    assert term_set.all_terms == ["AML"]
    assert warnings and "term generation failed" in warnings[0]


def test_degenerate_llm_response_cannot_shrink_the_term_set():
    term_set, warnings = terms.build_term_set(
        "AML", alternate_queries=["money laundering"], llm=FakeLLM(terms=["", "  ", "a"])
    )

    assert term_set.all_terms == ["AML", "money laundering"]
    assert warnings == ["term generation returned no usable terms"]


def test_multi_word_terms_match_as_phrases():
    assert terms.fts_match_query(["call centre"]) == '"call centre"'


# --------------------------------------------------------------- retrieval


def test_lexical_arm_has_no_candidate_cap(corpus_db):
    for index in range(60):
        add_circular(corpus_db, f"bulk-{index}", f"REF {index}", "Bulk",
                     "This circular concerns anti-money laundering duties.")
    corpus_db.commit()
    backfill_fts(corpus_db, force=True)

    matches = lexical = retrieval.lexical_candidates(
        corpus_db, ["anti-money laundering"], include_laws=False
    )

    assert len(matches) >= 60, "lexical arm truncated its result set"
    assert all(kind == "circular" for kind, _ in lexical)


def test_lexical_arm_finds_a_passing_mention_semantics_would_rank_last(indexed):
    db, _ = indexed

    matches = retrieval.lexical_candidates(db, ["AML"], include_laws=False)

    # c-mention is mostly about NPL reporting; its AML sentence is one line in four.
    assert ("circular", "c-mention") in matches


def test_dense_band_scores_every_chunk_and_respects_its_cap(indexed):
    _, collection = indexed
    snapshot = load_snapshot(collection, "snap")
    vectors = np.asarray([[1.0, 0.0, 0.0]], dtype=np.float32)

    band = retrieval.dense_band(snapshot, vectors, band=2)

    assert len(band) == 2
    assert ("circular", "c-aml") in band


def test_union_never_truncates_a_lexical_match_before_a_semantic_one():
    lexical = {("circular", "lex"): ["aml"]}
    dense = {("circular", "sem"): (0.99, [0])}

    candidates, truncated = retrieval.union_candidates(lexical, dense, max_candidates=1)

    assert truncated == 1
    assert candidates[0].document_id == "lex"


def test_union_records_both_arms_for_a_document_found_twice():
    lexical = {("circular", "both"): ["aml"]}
    dense = {("circular", "both"): (0.9, [3, 1])}

    candidates, _ = retrieval.union_candidates(lexical, dense, max_candidates=10)

    assert candidates[0].matched_via == {"lexical", "semantic"}
    assert candidates[0].chunk_indices == [3, 1]


# --------------------------------------------------------------- extraction


def test_verified_span_must_occur_in_the_passage():
    passage = "Banks shall implement anti-money laundering controls at onboarding."

    assert verify_span("anti-money laundering controls", passage).verified
    assert verify_span("Banks must implement AML controls", passage).verified is False


def test_span_verification_tolerates_collapsed_whitespace():
    result = verify_span("shall implement AML", "Banks\n  shall implement AML now.")

    assert result.verified and result.text == "shall implement AML"


def test_locator_is_offset_for_html_bodies_and_page_for_pdfs():
    assert resolve_locator({"source_start": 10, "ref": "Chunk 1"})["locator_kind"] == "offset"
    assert resolve_locator({"page_start": 4, "ref": "Page 4"})["locator_kind"] == "page"
    assert resolve_locator({"ref": "Chunk 1"})["locator_kind"] == "chunk"


# ------------------------------------------------------------- adjudication


def test_judge_failure_marks_candidates_undetermined_not_excluded():
    verdicts = adjudicate(FakeLLM(fail=True), "AML", [("a", "text"), ("b", "text")])

    assert [v.verdict for v in verdicts] == [VERDICT_UNDETERMINED] * 2


def test_no_llm_yields_undetermined_rather_than_a_silent_pass():
    verdicts = adjudicate(None, "AML", [("a", "text")])

    assert verdicts[0].verdict == VERDICT_UNDETERMINED


def test_adjudication_preserves_entry_order_across_batches():
    entries = [(f"doc-{i}", f"passage {i}") for i in range(30)]

    verdicts = adjudicate(FakeLLM(), "AML", entries)

    assert len(verdicts) == 30
    assert all(v.verdict == VERDICT_INCLUDED for v in verdicts)


# ------------------------------------------------------------------ service


def test_search_runs_without_any_llm(indexed):
    db, collection = indexed
    request = InventorySearchRequest(
        query="anti-money laundering", generate_terms=False, use_hyde=False,
        skip_adjudication=True, extract_spans=False,
    )

    response = make_service(collection).search(request, db)

    ids = {r.document_id for r in response.results}
    assert "c-aml" in ids and "c-mention" in ids
    assert response.retrieval_policy.term_set == ["anti-money laundering"]


def test_excluded_documents_are_returned_with_reasons(indexed):
    db, collection = indexed
    request = InventorySearchRequest(
        query="anti-money laundering", generate_terms=False, use_hyde=False,
        extract_spans=False,
    )

    response = make_service(collection, llm=FakeLLM(verdicts=False)).search(request, db)

    assert response.matched_documents == 0
    assert response.excluded, "rejected candidates must stay visible"
    assert all(e.judge_reason for e in response.excluded)


def test_extraction_falls_back_to_the_passage_when_unverifiable(indexed):
    db, collection = indexed
    llm = FakeLLM(span="a paraphrase that is nowhere in the source")
    request = InventorySearchRequest(
        query="anti-money laundering", generate_terms=False, use_hyde=False,
    )

    response = make_service(collection, llm=llm).search(request, db)

    evidence = response.results[0].evidence[0]
    assert evidence.extraction_verified is False
    assert evidence.extracted_text == evidence.passage
    assert any("could not be verified" in w for w in response.coverage.warnings)


def test_strict_coverage_refuses_an_incomplete_index(indexed):
    db, collection = indexed
    for chunk_id in [i for i in collection.records if i.startswith("c-audit__")]:
        collection.delete([chunk_id])
    ledger.reconcile(db, collection, FakeConfig(), write=True)

    request = InventorySearchRequest(
        query="AML", generate_terms=False, use_hyde=False, skip_adjudication=True,
        require_complete_coverage=True,
    )

    with pytest.raises(Exception) as excinfo:
        make_service(collection).search(request, db)
    assert "semantic_index_incomplete" in getattr(excinfo.value, "code", "")


def test_law_results_carry_version_and_hierarchy(indexed):
    db, collection = indexed
    request = InventorySearchRequest(
        query="anti-money laundering", sources=["laws"], generate_terms=False,
        use_hyde=False, skip_adjudication=True, extract_spans=False,
    )

    response = make_service(collection).search(request, db)

    law = next(r for r in response.results if r.document_id == "l-aml")
    assert law.result_kind == "law" and law.version_id == "l-aml-v1"


def test_blank_query_is_rejected(indexed):
    db, collection = indexed

    with pytest.raises(InvalidQuery):
        make_service(collection).search(InventorySearchRequest(query="   "), db)


def test_response_serializes_without_law_keys_on_circulars(indexed):
    db, collection = indexed
    request = InventorySearchRequest(
        query="AML", generate_terms=False, use_hyde=False, skip_adjudication=True,
        extract_spans=False,
    )

    payload = make_service(collection).search(request, db).to_dict()

    circular_rows = [r for r in payload["results"] if r["result_kind"] == "circular"]
    assert circular_rows and "version_id" not in circular_rows[0]
