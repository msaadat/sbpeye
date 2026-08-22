"""Tests for snippet selection from retrieved chunks.

The bug these pin: the vector arm knows which chunk matched, but the old code
collapsed that to a document rank, threw the chunk away, and then re-found a
passage by scanning the whole document for query-term density. On a long
attachment the two disagreed — the result cited the matched chunk's page while
displaying text the density scan had pulled from somewhere else entirely.

These cover the split that fixed it: locating a passage is retrieval's job
(`MatchEvidence`), cutting a preview out of one is `best_window`'s, and marking
it up is `highlight_terms`'.
"""

from datetime import datetime

import pytest

from sbpeye.models import Attachment, RegDocument, RegDocumentVersion
from sbpeye.search import (
    PREVIEW_REGION_CHARS,
    MatchEvidence,
    _preview_region,
    backfill_fts,
    best_window,
    choose_evidence,
    highlight_terms,
    index_law_fts,
    make_preview,
    search_engine,
)

from conftest import make_circular


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


def _evidence(text, page, distance, **overrides):
    fields = dict(
        text=text,
        doc_type="attachment",
        source_id="attachment-1",
        source_label="guidelines.pdf",
        page=page,
        source_ref=f"Page {page}",
        distance=distance,
    )
    fields.update(overrides)
    return MatchEvidence(**fields)


# --- primitives keep to one job each -------------------------------------


def test_best_window_locates_without_marking_up():
    text = "alpha beta " * 30 + "the quuxreport threshold is fifty " + "gamma delta " * 30
    window = best_window(text, {"quuxreport"}, window=5)
    assert "quuxreport" in window
    assert "<mark>" not in window


def test_highlight_terms_marks_up_without_locating():
    marked = highlight_terms("the quuxreport threshold", {"quuxreport"})
    assert marked == "the <mark>quuxreport</mark> threshold"


def test_short_passage_is_returned_whole():
    assert best_window("a quuxreport here", {"quuxreport"}, window=25) == "a quuxreport here"


def test_preview_is_empty_without_query_tokens():
    assert make_preview("some text", set()) == ""


# --- previewing a whole document is bounded ------------------------------
#
# `make_preview` is the fallback for results with no retrieved chunk to quote, so unlike
# `best_window` it is handed entire documents. It used to scan all of one — 87% of a law
# search — and now locates a region first (P13/P14 in docs/PERFORMANCE_PLAN.md). These pin
# what that region selection must not break.


def test_preview_finds_a_term_buried_deep_in_a_long_document():
    """The property the exhaustive scan gave for free, and the one bounding could lose."""
    filler = "alpha beta gamma delta " * 6000  # ≈ 140 KB before the term appears
    text = filler + "the quuxreport threshold is fifty " + filler

    preview = make_preview(text, {"quuxreport"})

    assert "quuxreport" in preview
    assert "<mark>quuxreport</mark>" in preview


def test_preview_region_is_capped_regardless_of_document_size():
    """Bounded means bounded: the word-level scorer never sees more than one region."""
    text = "alpha beta " * 50_000 + "quuxreport" + " gamma delta " * 50_000

    region, cut_before, cut_after = _preview_region(text, {"quuxreport"})

    assert len(region) <= PREVIEW_REGION_CHARS
    assert "quuxreport" in region
    assert cut_before and cut_after


def test_preview_marks_both_edges_when_the_region_was_cut():
    """The region is a slice of a document, so its edges are elisions, not the document's."""
    filler = "alpha beta gamma delta " * 2000
    preview = make_preview(filler + "quuxreport here" + filler, {"quuxreport"})

    assert preview.startswith("…")
    assert preview.endswith("…")


def test_preview_without_any_match_falls_back_to_the_opening():
    """No match means every window scores zero, so the opening is as good an answer."""
    text = "alpha beta gamma delta " * 2000

    preview = make_preview(text, {"quuxreport"})

    assert preview.startswith("alpha beta")
    assert not preview.startswith("…")


# --- choosing among matched passages -------------------------------------


def test_denser_passage_wins_when_distances_are_comparable():
    """The case from BPRD Circular No. 11 of 2015: the nearest chunk was a heading
    and the chunk 0.003 behind it held the limits table."""
    heading = _evidence("Asaan Account Guidelines on Low Risk Accounts. Eligibility.", 2, 0.5706)
    table = _evidence(
        "Transaction Total Debit per Month: Rs. 500,000 Limits Total Credit "
        "Balance Limit: Rs. 500,000 Geographic Coverage",
        4,
        0.5734,
    )

    chosen, snippet = choose_evidence([heading, table], {"credit", "balance", "limit"})

    assert chosen is table
    assert chosen.page == 4          # the page cited is the page shown
    assert "<mark>Credit</mark>" in snippet


def test_nearest_passage_wins_when_density_ties():
    first = _evidence("a credit limit applies", 1, 0.10)
    second = _evidence("a credit limit applies elsewhere", 9, 0.90)

    chosen, _ = choose_evidence([first, second], {"credit", "limit"})

    assert chosen is first


def test_no_usable_evidence_returns_none():
    assert choose_evidence([], {"credit"}) is None
    assert choose_evidence([_evidence("", 1, 0.1)], {"credit"}) is None


# --- end to end through search() -----------------------------------------


def _circular_with_long_attachment():
    """An attachment whose densest prose is on a different page from its table."""
    circular = make_circular(title="Asaan Account guidelines", content_text="Cover letter.")
    filler_prose = (
        "The credit balance limit and the debit limit and the credit limit are "
        "discussed at length in this paragraph about limits. " * 12
    )
    table = "Total Credit Balance Limit: Rs. 500,000 quuxthreshold applies."
    circular.attachments = [
        Attachment(
            id="attachment-1",
            circular_id=circular.id,
            filename="guidelines.pdf",
            original_url="https://example/guidelines.pdf",
            content_text=f"{filler_prose}\n{table}",
            file_type="pdf",
            extraction_status="extracted",
        )
    ]
    return circular


def test_result_cites_the_page_of_the_passage_it_shows(db, monkeypatch):
    circular = _circular_with_long_attachment()
    db.add(circular)
    db.commit()
    backfill_fts(db)

    # The vector arm matched the table, which lives on page 7 — while the densest
    # prose in the document sits in the page-3 filler.
    class TableChunkCollection:
        def query(self, **kwargs):
            return {
                "ids": [["chunk-table"]],
                "documents": [["Total Credit Balance Limit: Rs. 500,000 quuxthreshold applies."]],
                "distances": [[0.21]],
                "metadatas": [[{
                    "circular_id": circular.id,
                    "attachment_id": "attachment-1",
                    "doc_type": "attachment",
                    "filename": "guidelines.pdf",
                    "page_start": 7,
                    "ref": "Page 7",
                }]],
            }

    monkeypatch.setattr("sbpeye.search.collection", TableChunkCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.1]]
    )

    results, _ = search_engine.search("credit balance limit", db)

    result = results[0]
    assert result["source_page"] == 7
    assert result["match_source"] == "attachment"
    assert result["attachment_id"] == "attachment-1"
    assert result["attachment_filename"] == "guidelines.pdf"
    # The snippet is cut from the matched chunk, not from the denser filler prose.
    assert "quuxthreshold" in result["snippet"]


def test_lexical_only_hit_still_previews_but_cites_no_page(db, monkeypatch):
    """No vector evidence — Chroma empty or down. The scan fallback still produces a
    snippet, and declines to cite a page because nothing located the passage."""
    circular = _circular_with_long_attachment()
    db.add(circular)
    db.commit()
    backfill_fts(db)

    class EmptyCollection:
        def query(self, **kwargs):
            return {"ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]}

    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.1]]
    )

    results, _ = search_engine.search("quuxthreshold", db)

    result = results[0]
    assert "<mark>quuxthreshold</mark>" in result["snippet"]
    assert result["match_source"] == "attachment"
    assert result.get("source_page") is None


def test_chunk_without_stored_text_ranks_but_is_not_quoted(db, monkeypatch):
    """A chunk whose text did not come back still counts for ranking; it just falls
    through to the scan rather than producing an empty snippet."""
    circular = _circular_with_long_attachment()
    db.add(circular)
    db.commit()
    backfill_fts(db)

    class TextlessCollection:
        def query(self, **kwargs):
            return {
                "ids": [["chunk-1"]],
                "documents": [[""]],
                "distances": [[0.2]],
                "metadatas": [[{
                    "circular_id": circular.id,
                    "doc_type": "attachment",
                    "attachment_id": "attachment-1",
                    "page_start": 7,
                }]],
            }

    monkeypatch.setattr("sbpeye.search.collection", TextlessCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.1]]
    )

    results, total = search_engine.search("quuxthreshold", db)

    assert total == 1
    assert "<mark>quuxthreshold</mark>" in results[0]["snippet"]
    assert results[0].get("source_page") is None


# --- laws get the same treatment -----------------------------------------
#
# These carry the law path on their own: no law chunks are indexed in the local
# Chroma store (`kind="law"` matches nothing), so the law vector arm returns empty
# against real data and only a stub exercises this.


def _add_law(db, document_id="law-1"):
    document = RegDocument(
        id=document_id,
        title="Prudential Regulations for Microfinance Banks",
        normalized_title="prudential regulations for microfinance banks",
        doc_type="regulation",
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
    )
    db.add(document)
    db.add(
        RegDocumentVersion(
            id=f"{document_id}-v1",
            document_id=document_id,
            content_hash=f"hash-{document_id}",
            file_type="pdf",
            content_text=(
                "Preamble discussing capital and capital and capital at length. " * 8
                + "Regulation R-1: Minimum Capital Requirement is Rs. 500 million quuxcapital."
            ),
            is_current=1,
            first_seen_at=datetime(2026, 8, 1),
            last_seen_at=datetime(2026, 8, 1),
        )
    )
    db.commit()
    index_law_fts(db, document)
    return document


def test_law_result_quotes_the_matched_chunk_and_cites_its_page(db, monkeypatch):
    document = _add_law(db)

    class LawChunkCollection:
        def query(self, **kwargs):
            return {
                "ids": [["law-chunk-9"]],
                "documents": [[
                    "Regulation R-1: Minimum Capital Requirement is Rs. 500 million quuxcapital."
                ]],
                "distances": [[0.18]],
                "metadatas": [[{
                    "kind": "law",
                    "doc_type": "law",
                    "document_id": document.id,
                    "version_id": f"{document.id}-v1",
                    "page_start": 12,
                    "ref": "Regulation R-1",
                }]],
            }

    monkeypatch.setattr("sbpeye.search.collection", LawChunkCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.1]]
    )

    results, _ = search_engine.search("minimum capital requirement", db, source="laws")

    result = results[0]
    assert result["result_kind"] == "law"
    assert result["source_page"] == 12
    assert result["source_ref"] == "Regulation R-1"
    # Cut from the matched chunk, not from the capital-repeating preamble.
    assert "quuxcapital" in result["snippet"]


def test_law_result_falls_back_to_scanning_without_evidence(db, monkeypatch):
    _add_law(db)

    class EmptyCollection:
        def query(self, **kwargs):
            return {"ids": [[]], "metadatas": [[]], "documents": [[]], "distances": [[]]}

    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.1]]
    )

    results, _ = search_engine.search("quuxcapital", db, source="laws")

    # Marked-up, though the highlighter's dotted-acronym branch pulls in a trailing
    # period when the term ends a sentence — hence the open-ended match.
    assert "<mark>quuxcapital" in results[0]["snippet"]
    assert results[0].get("source_page") is None
