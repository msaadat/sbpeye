"""Tests for `SearchEngine.dual_arm_search` — the unfused retrieval chat uses.

The motivating failure: a chat asked for the current Asaan Account credit balance
limit and answered from a 2022 circular, because the 2026 circular that revised the
limit states it only inside an attached framework PDF. That circular was the *best*
semantic match in the corpus, but `search()` fuses the arms with RRF and then adds a
title-overlap bonus an order of magnitude larger than the whole RRF range, so a
document whose title does not repeat the query's words cannot win. These pin the
properties that fix it: the arms come back separately, in their own retrieval order,
carrying both ranks, with no bonus applied to either.
"""

import types
from datetime import datetime

import pytest

import sbpeye.search as search_module
from sbpeye.models import Attachment
from sbpeye.search import backfill_fts, search_engine

from conftest import make_circular


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


def _add(db, attachment_text: str | None = None, **overrides):
    circular = make_circular(**overrides)
    db.add(circular)
    db.flush()
    if attachment_text:
        db.add(
            Attachment(
                id=f"att-{circular.id}",
                circular_id=circular.id,
                filename=f"{circular.id}-annex.pdf",
                original_url=f"https://www.sbp.org.pk/{circular.id}-annex.pdf",
                file_type="pdf",
                content_text=attachment_text,
                extraction_status="extracted",
            )
        )
    db.commit()
    return circular


@pytest.fixture
def no_vectors(monkeypatch):
    """Neutralize the Chroma arm so the lexical arm is asserted deterministically."""
    monkeypatch.setattr(
        search_module,
        "embedding_backend",
        types.SimpleNamespace(
            embed_queries=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no vec"))
        ),
    )


@pytest.fixture
def fake_vectors(monkeypatch):
    """Drive the semantic arm from an explicit chunk order.

    Returns a setter taking `[(circular_id, attachment_id_or_None), ...]` in the
    rank order Chroma should report.
    """
    def install(chunks: list[tuple[str, str | None]]):
        metadatas = [
            {
                "circular_id": circular_id,
                "doc_type": "attachment" if attachment_id else "circular",
                **({"attachment_id": attachment_id} if attachment_id else {}),
            }
            for circular_id, attachment_id in chunks
        ]
        monkeypatch.setattr(
            search_module,
            "embedding_backend",
            types.SimpleNamespace(embed_queries=lambda queries: [[0.0]] * len(queries)),
        )
        monkeypatch.setattr(
            search_module,
            "collection",
            types.SimpleNamespace(
                query=lambda **_kw: {
                    "ids": [[f"chunk-{i}" for i in range(len(metadatas))]],
                    "metadatas": [metadatas],
                }
            ),
        )
    return install


# ---------------------------------------------------------------------------
# The regression: a body/annexure-only match must reach the model
# ---------------------------------------------------------------------------

def test_annexure_only_match_leads_the_semantic_arm(db, fake_vectors):
    """A circular whose title says nothing about the topic still leads the
    semantic list — the case fusion + title bonus used to bury."""
    _add(
        db,
        id="c-titled",
        title="Guidelines on Asaan Account",
        reference="BPRD Circular Letter No. 10 of 2022",
        content_text="Total credit balance limit for the Asaan Account is one million",
    )
    _add(
        db,
        id="c-framework",
        title="Consolidated Customer Onboarding Framework",
        reference="BPRD Circular Letter No. 09 of 2026",
        content_text="The subject Framework has been amended. Details at Annexure.",
        attachment_text="Asaan Account maximum credit balance limit PKR 3,000,000",
    )
    backfill_fts(db)
    fake_vectors([("c-framework", "att-c-framework"), ("c-titled", None)])

    arms = search_engine.dual_arm_search("asaan account credit balance limit", db)

    assert [r["circular"].id for r in arms["semantic_results"]][0] == "c-framework"
    # Present in the lexical arm too, just lower — both opinions survive.
    assert "c-framework" in [r["circular"].id for r in arms["lexical_results"]]


def test_fused_search_still_ranks_the_titled_circular_first(db, fake_vectors):
    """The contrast that justifies the unfused arm.

    Same corpus and same retrieval as the test above, but through `search()`: the
    title-overlap bonus outweighs the whole RRF range, so the circular that merely
    *names* the topic outranks the one that answers it. That is acceptable for the
    search UI, where a human reads the list; it is what makes a single fused list
    the wrong input for a chat model. If this ever stops holding, revisit whether
    `dual_arm_search` is still earning its place.
    """
    _add(
        db,
        id="c-titled",
        title="Guidelines on Asaan Account",
        content_text="Total credit balance limit for the Asaan Account is one million",
    )
    _add(
        db,
        id="c-framework",
        title="Consolidated Customer Onboarding Framework",
        content_text="The subject Framework has been amended. Details at Annexure.",
        attachment_text="Asaan Account maximum credit balance limit PKR 3,000,000",
    )
    backfill_fts(db)
    fake_vectors([("c-framework", "att-c-framework"), ("c-titled", None)])

    results, _ = search_engine.search("asaan account credit balance limit", db, limit=10)

    assert [r["circular"].id for r in results][0] == "c-titled"


def test_arms_keep_their_own_order_and_carry_both_ranks(db, fake_vectors):
    _add(db, id="c-a", title="Alpha topic", content_text="alpha alpha alpha topic")
    _add(db, id="c-b", title="Beta", content_text="alpha topic mentioned once")
    backfill_fts(db)
    fake_vectors([("c-b", None), ("c-a", None)])

    arms = search_engine.dual_arm_search("alpha topic", db)

    assert [r["circular"].id for r in arms["lexical_results"]] == ["c-a", "c-b"]
    assert [r["circular"].id for r in arms["semantic_results"]] == ["c-b", "c-a"]
    by_id = {r["circular"].id: r for r in arms["lexical_results"]}
    assert (by_id["c-a"]["lexical_rank"], by_id["c-a"]["semantic_rank"]) == (1, 2)
    assert (by_id["c-b"]["lexical_rank"], by_id["c-b"]["semantic_rank"]) == (2, 1)


def test_rank_is_none_when_an_arm_did_not_return_the_circular(db, fake_vectors):
    _add(db, id="c-lex", title="Lexical only", content_text="unmistakable keyword")
    _add(db, id="c-vec", title="Vector only", content_text="unrelated prose")
    backfill_fts(db)
    fake_vectors([("c-vec", None)])

    arms = search_engine.dual_arm_search("unmistakable keyword", db)

    lexical = {r["circular"].id: r for r in arms["lexical_results"]}
    semantic = {r["circular"].id: r for r in arms["semantic_results"]}
    assert lexical["c-lex"]["semantic_rank"] is None
    assert semantic["c-vec"]["lexical_rank"] is None


# ---------------------------------------------------------------------------
# Reference matches, filters, degradation
# ---------------------------------------------------------------------------

def test_exact_reference_returns_its_own_list(db, no_vectors):
    _add(db, id="c-ref", reference="BPRD Circular No. 07 of 2019",
         title="Target", date=datetime(2019, 3, 1))
    _add(db, id="c-other", reference="BPRD Circular No. 08 of 2019",
         title="Other", date=datetime(2019, 4, 1))
    backfill_fts(db)

    arms = search_engine.dual_arm_search("BPRD Circular No. 07 of 2019", db)

    assert [r["circular"].id for r in arms["reference_matches"]] == ["c-ref"]


def test_exact_reference_survives_a_date_that_disagrees_with_it(db, no_vectors):
    """The reference arm keys off the year in the reference, not the date year.

    The two are not the same field: 45 circulars in the corpus disagree, most of
    them EDMD's, which carry a backfilled 2001-03-31 date under references
    numbered "of 2002" through "of 2004". Narrowing the arm on the date year made
    every one of them unreachable by the reference a user would actually cite.
    """
    _add(db, id="c-backfilled", reference="EDMD Circular No. 12 of 2004",
         title="Target", date=datetime(2001, 3, 31))
    _add(db, id="c-same-date", reference="EDMD Circular No. 11 of 2004",
         title="Other", date=datetime(2001, 3, 31))
    backfill_fts(db)

    arms = search_engine.dual_arm_search("EDMD Circular No. 12 of 2004", db)

    assert [r["circular"].id for r in arms["reference_matches"]] == ["c-backfilled"]


def test_filters_apply_to_both_arms(db, fake_vectors):
    _add(db, id="c-bprd", department="BPRD", title="Onboarding", content_text="topic text")
    _add(db, id="c-epd", department="EPD", title="Onboarding", content_text="topic text")
    backfill_fts(db)
    fake_vectors([("c-epd", None), ("c-bprd", None)])

    arms = search_engine.dual_arm_search("topic text", db, department="BPRD")

    assert [r["circular"].id for r in arms["lexical_results"]] == ["c-bprd"]
    assert [r["circular"].id for r in arms["semantic_results"]] == ["c-bprd"]


def test_limit_applies_per_arm(db, fake_vectors):
    for index in range(5):
        _add(db, id=f"c-{index}", title=f"Doc {index}", content_text="shared topic text")
    backfill_fts(db)
    fake_vectors([(f"c-{index}", None) for index in range(5)])

    arms = search_engine.dual_arm_search("shared topic", db, limit=2)

    assert len(arms["lexical_results"]) == 2
    assert len(arms["semantic_results"]) == 2


def test_vector_failure_degrades_to_the_lexical_arm(db, no_vectors):
    _add(db, id="c-only", title="Solitary", content_text="distinctive phrase here")
    backfill_fts(db)

    arms = search_engine.dual_arm_search("distinctive phrase", db)

    assert [r["circular"].id for r in arms["lexical_results"]] == ["c-only"]
    assert arms["semantic_results"] == []


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_returns_empty_arms(db, no_vectors, query):
    _add(db, id="c-any", title="Anything", content_text="any text")
    backfill_fts(db)

    assert search_engine.dual_arm_search(query, db) == {
        "reference_matches": [], "lexical_results": [], "semantic_results": []
    }
