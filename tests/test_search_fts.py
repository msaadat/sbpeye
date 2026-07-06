"""Tests for the SQLite FTS5 lexical arm that replaced the in-memory rank-bm25 index.

These pin the properties that motivated the migration: the index is *incremental*
(a circular added after the initial backfill is searchable without a rebuild),
persistent, and preserves the old BM25 ranking behavior (synonym expansion,
reference digit padding). They exercise the FTS helpers directly and once through
`SearchEngine.search` with the vector arm neutralized so results are deterministic.
"""

import types

import pytest

import sbpeye.search as search_module
from sbpeye.models import Attachment
from sbpeye.search import (
    backfill_fts,
    delete_circular_fts,
    expand_query_tokens,
    index_circular_fts,
    search_engine,
    tokenize,
)

from conftest import make_circular


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


def _ranks(db, query: str) -> dict[str, int]:
    """Run the lexical arm exactly as `search()` does for a raw query string."""
    return search_engine._fts_ranks(db, expand_query_tokens(tokenize(query)))


def _add(db, **overrides):
    circular = make_circular(**overrides)
    db.add(circular)
    db.commit()
    return circular


# ---------------------------------------------------------------------------
# Incrementality — the core regression this migration fixes
# ---------------------------------------------------------------------------

def test_backfill_indexes_existing_circulars(db):
    _add(db, id="c-aml", title="AML CFT guidelines", content_text="anti money laundering")
    _add(db, id="c-car", title="Capital adequacy ratio", content_text="capital adequacy")
    backfill_fts(db)

    ranks = _ranks(db, "money laundering")
    assert "c-aml" in ranks
    assert "c-car" not in ranks


def test_circular_added_after_backfill_is_searchable_without_rebuild(db):
    _add(db, id="c-old", title="Old circular", content_text="existing corpus text")
    backfill_fts(db)

    # A brand-new circular arrives while the server runs. Only an incremental
    # upsert happens — no backfill/rebuild call.
    late = _add(
        db,
        id="c-new",
        title="Ransomware advisory",
        content_text="cyber attack malware mitigation",
    )
    index_circular_fts(db, late)

    ranks = _ranks(db, "ransomware")
    assert "c-new" in ranks


def test_backfill_force_rebuilds_and_drops_stale_rows(db):
    stale = _add(db, id="c-stale", title="Stale", content_text="obsolete text")
    backfill_fts(db)
    assert "c-stale" in _ranks(db, "obsolete")

    # Simulate the row's content changing out-of-band, then a forced reindex.
    stale.content_text = "fresh replacement text"
    db.commit()
    backfill_fts(db, force=True)
    assert "c-stale" in _ranks(db, "fresh")
    assert "c-stale" not in _ranks(db, "obsolete")


def test_backfill_is_noop_once_populated(db):
    _add(db, id="c-1", title="First", content_text="alpha")
    backfill_fts(db)
    # Add a row directly, then call backfill again — it must NOT wipe/rebuild.
    late = _add(db, id="c-2", title="Second", content_text="bravo")
    index_circular_fts(db, late)
    backfill_fts(db)  # populated → no-op
    assert "c-2" in _ranks(db, "bravo")
    assert "c-1" in _ranks(db, "alpha")


# ---------------------------------------------------------------------------
# Ranking parity with the old BM25 behavior
# ---------------------------------------------------------------------------

def test_synonym_expansion_matches_acronym_to_expansion(db):
    # Body stores the expansion; a query for the acronym must still hit via
    # expand_query_tokens (CRR -> cash reserve requirement).
    _add(
        db,
        id="c-crr",
        title="Reserve requirements",
        content_text="banks shall maintain the cash reserve requirement as prescribed",
    )
    backfill_fts(db)
    assert "c-crr" in _ranks(db, "CRR")


def test_reference_digit_padding_matches_both_forms(db):
    _add(
        db,
        id="c-08",
        reference="BPRD Circular No. 08 of 2024",
        title="Some policy",
        content_text="body",
    )
    backfill_fts(db)
    # Stored reference cell carries both "08" and "8"; a query for either matches.
    assert "c-08" in search_engine._fts_ranks(db, ["8"])
    assert "c-08" in search_engine._fts_ranks(db, ["08"])


def test_attachment_text_feeds_circular_body(db):
    circular = _add(db, id="c-att", title="Cover note", content_text="see attachment")
    db.add(Attachment(
        id="att-1",
        circular_id="c-att",
        filename="annex.pdf",
        original_url="https://example/annex.pdf",
        content_text="detailed provisioning requirements for classified loans",
    ))
    db.commit()
    db.refresh(circular)
    index_circular_fts(db, circular)

    assert "c-att" in _ranks(db, "provisioning")


# ---------------------------------------------------------------------------
# Deletion & empty-query safety
# ---------------------------------------------------------------------------

def test_delete_removes_row_from_results(db):
    _add(db, id="c-del", title="Doomed circular", content_text="ephemeral content")
    backfill_fts(db)
    assert "c-del" in _ranks(db, "ephemeral")

    delete_circular_fts(db, "c-del")
    assert "c-del" not in _ranks(db, "ephemeral")


def test_empty_expansion_yields_no_lexical_candidates(db):
    _add(db, id="c-x", title="Anything", content_text="anything")
    backfill_fts(db)
    assert search_engine._fts_ranks(db, []) == {}


# ---------------------------------------------------------------------------
# End-to-end through search() with the vector arm neutralized
# ---------------------------------------------------------------------------

def test_search_end_to_end_uses_fts_arm(db, monkeypatch):
    # Force the Chroma vector arm to fail fast so results come from FTS +
    # reference only, keeping the assertion deterministic.
    monkeypatch.setattr(
        search_module,
        "embedding_backend",
        types.SimpleNamespace(
            embed_queries=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no vec"))
        ),
    )
    _add(db, id="c-aml", title="AML CFT framework", content_text="anti money laundering rules")
    _add(db, id="c-fx", title="Foreign exchange manual", content_text="forex settlement")
    backfill_fts(db)

    results, total = search_engine.search("money laundering", db, limit=10)
    ids = [r["circular"].id for r in results]
    assert "c-aml" in ids
    assert total >= 1

    # Incremental add is immediately searchable end-to-end.
    late = _add(db, id="c-late", title="Digital banking", content_text="branchless agent banking")
    index_circular_fts(db, late)
    results2, _ = search_engine.search("branchless banking", db, limit=10)
    assert "c-late" in [r["circular"].id for r in results2]
