"""Whether a chat question can reach the laws corpus at all.

The motivating failure, from the 2026-08-23 benchmark round: two questions answerable
only from an Act of Parliament, and neither answer came from one. The catalogue recorded
this as retrieval never routing to the laws corpus. The traces say otherwise — the SBP
Act came back as the *top* result of a laws sweep in both runs of P15. What was missing
was everything after retrieval:

* no discovery tool asked for laws, so reaching one depended on the model choosing an
  inventory sweep described as being for "list all" questions;
* `get_circular_details("State Bank of Pakistan Act, 1956")` answered with BPRD Circular
  No. 27 of 1999 — a cash-reserve circular whose title merely mentions the Act — and
  presented it as the requested document;
* the one passage a law hit did carry was 240 characters, which is s.9D(6) alone. The
  composition of the committee is s.9D(1), one chunk earlier, across a page boundary.

These pin the three properties that fix it.
"""

import json
from datetime import datetime

import pytest

from sbpeye.chat_retrieval import LAW_NEIGHBOUR_CHUNKS, ScopedLawRetriever
from sbpeye.models import RegDocument, RegDocumentVersion
from sbpeye.search import backfill_fts, index_law_fts, search_engine

from conftest import make_circular


# The real text, abridged, with the real chunk split: SBP's PDF puts s.9D(1) on page 19
# and s.9D(6) on page 20, so retrieval that matches "quorum" lands one chunk past the
# membership list it is asked about.
MPC_COMPOSITION = (
    "9D. Establishment of Monetary Policy Committee. (1) The Monetary Policy Committee "
    "shall consist of the Governor as Chairperson, one Deputy Governor, three members of "
    "the Board to be nominated by the Board, and three external members."
)
MPC_QUORUM = (
    "(6) The quorum for the Monetary Policy Committee meeting shall be four members "
    "including at least one of whom shall be the Governor as Chairperson, or in his "
    "absence, the relevant Deputy Governor as nominated by the Governor."
)
SBP_ACT_TEXT = f"{MPC_COMPOSITION}\n\n{MPC_QUORUM}"


def add_law(db, document_id="sbp-act", title="State Bank of Pakistan Act, 1956",
            text_body=SBP_ACT_TEXT, doc_type="law"):
    document = RegDocument(
        id=document_id,
        title=title,
        normalized_title=title.casefold(),
        doc_type=doc_type,
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
    )
    db.add(document)
    db.add(RegDocumentVersion(
        id=f"{document_id}-v1",
        document_id=document_id,
        content_hash=f"hash-{document_id}",
        file_type="pdf",
        content_text=text_body,
        is_current=1,
        first_seen_at=datetime(2026, 8, 1),
        last_seen_at=datetime(2026, 8, 1),
    ))
    db.commit()
    index_law_fts(db, document)
    return document


class StubCollection:
    """The chunks of one law, addressable the way Chroma addresses them."""

    def __init__(self, chunks: list[tuple[int, str, str, int]], document_id: str):
        self.document_id = document_id
        self.chunks = chunks

    def get(self, where=None, include=None):
        return {
            "documents": [text for _, text, _, _ in self.chunks],
            "metadatas": [
                {"chunk_index": index, "ref": ref, "page_start": page,
                 "document_id": self.document_id}
                for index, _, ref, page in self.chunks
            ],
        }

    def query(self, **kwargs):
        # Ranked so the quorum chunk wins, reproducing the real failure: the retriever
        # matches the sub-section the question names and not the one it depends on.
        ordered = sorted(self.chunks, key=lambda item: 0 if "quorum" in item[1] else 1)
        return {
            "ids": [[f"chunk-{index}" for index, _, _, _ in ordered]],
            "documents": [[text for _, text, _, _ in ordered]],
            "distances": [[0.1 * rank for rank in range(len(ordered))]],
            "metadatas": [[
                {"chunk_index": index, "ref": ref, "page_start": page,
                 "doc_type": "law", "document_id": self.document_id}
                for index, _, ref, page in ordered
            ]],
        }


@pytest.fixture
def act_chunks():
    return [
        (41, MPC_COMPOSITION, "Page 19", 19),
        (42, MPC_QUORUM, "Page 20", 20),
    ]


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


class EmptyCollection:
    def query(self, **kwargs):
        return {"ids": [[]], "metadatas": [[]]}


@pytest.fixture
def no_vectors(monkeypatch):
    monkeypatch.setattr("sbpeye.search.collection", EmptyCollection())
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )


# --------------------------------------------------------------- discovery reaches laws

def test_discovery_returns_laws_alongside_circulars(db, no_vectors):
    add_law(db)
    db.add(make_circular(
        id="c-1", title="Cash reserve requirement",
        content_text="Scheduled banks shall maintain a cash reserve with the State Bank.",
    ))
    db.commit()
    backfill_fts(db)

    arms = search_engine.dual_arm_search("Monetary Policy Committee quorum", db)

    assert [r["law"].id for r in arms["law_results"]] == ["sbp-act"]


def test_a_law_only_question_is_not_lost_to_the_circular_short_circuit(db, no_vectors):
    """The case that used to return nothing.

    A statutory question typically matches no circular at all, and `dual_arm_search`
    returned its empty dict the moment the circular candidate set came back empty —
    discarding a law arm that had matched.
    """
    add_law(db)
    backfill_fts(db)

    arms = search_engine.dual_arm_search("Monetary Policy Committee quorum", db)

    assert arms["lexical_results"] == []
    assert [r["law"].id for r in arms["law_results"]] == ["sbp-act"]


def test_circular_only_filters_drop_the_law_arm(db, no_vectors):
    """A law never faced the department filter, so it must not sit beside ones that did."""
    add_law(db)
    backfill_fts(db)

    arms = search_engine.dual_arm_search(
        "Monetary Policy Committee quorum", db, department="BPRD", include_laws=False
    )

    assert arms["law_results"] == []


def test_law_results_carry_whole_passages(db, monkeypatch, act_chunks):
    """A window across a provision boundary quotes half a rule, so laws get chunks whole."""
    add_law(db)
    backfill_fts(db)
    stub = StubCollection(act_chunks, "sbp-act")
    monkeypatch.setattr("sbpeye.search.collection", stub)
    monkeypatch.setattr(
        "sbpeye.search.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    arms = search_engine.dual_arm_search("Monetary Policy Committee quorum", db)

    passages = arms["law_results"][0]["passages"]
    assert passages, "a law hit with vector evidence must carry its chunks whole"
    assert any(item["text"] == MPC_QUORUM for item in passages)


# ------------------------------------------------------------------ drill-in reads laws

def test_neighbour_expansion_returns_the_provision_whole(db, monkeypatch, act_chunks):
    """The P15 failure, directly.

    Retrieval matches the quorum chunk. The question also asks who sits on the committee,
    which is the chunk before it, across a page boundary. Without expansion the answer is
    "the retrieved passage does not provide a full list of the committee's membership" —
    which is what the system actually said.
    """
    document = add_law(db)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.collection", StubCollection(act_chunks, "sbp-act")
    )
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    passages = ScopedLawRetriever(db, document).search(
        "quorum for the Monetary Policy Committee", limit=1, token_budget=4000
    )

    texts = [item["passage"] for item in passages]
    assert MPC_QUORUM in texts, "the matched chunk"
    assert MPC_COMPOSITION in texts, "and the chunk it depends on, one index away"
    assert LAW_NEIGHBOUR_CHUNKS >= 1


def test_expanded_passages_come_back_in_document_order(db, monkeypatch, act_chunks):
    """Consecutive sub-sections read as the provision only in the order they were written."""
    document = add_law(db)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.collection", StubCollection(act_chunks, "sbp-act")
    )
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    passages = ScopedLawRetriever(db, document).search(
        "quorum", limit=1, token_budget=4000
    )

    assert [item["chunk_index"] for item in passages] == [41, 42]


def test_a_provision_can_be_fetched_by_its_number(db, monkeypatch, act_chunks):
    """Semantic search cannot find "section 9D" by meaning — the phrase carries none."""
    document = add_law(db)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.collection", StubCollection(act_chunks, "sbp-act")
    )

    passages = ScopedLawRetriever(db, document).section("9D", token_budget=4000)

    assert any(item["passage"] == MPC_COMPOSITION for item in passages)


def test_a_section_lookup_prefers_the_provision_over_the_contents_page(db, monkeypatch):
    """Found live against the real Act, not reasoned about.

    A table of contents is heading-shaped by construction — "9D. Establishment of
    Monetary Policy Committee 14" matches any pattern the provision itself matches, and
    it sits 39 chunks earlier, so under a budget it arrived instead of the provision.
    Density separates them: the contents chunk holds 20 headings, the provision holds 4.
    """
    document = add_law(db)
    contents = (
        "CONTENTS 9. Board of Directors 12 9C. Prohibition on Government Borrowing. 13 "
        "9D. Establishment of Monetary Policy Committee 14 9E. Powers and Functions of "
        "Monetary Policy Committee 15 10. Deputy Governors 16 11. Meetings 17"
    )
    # Footnote markers are glued onto the text that follows them by PDF extraction, so
    # the real heading extracts as "419D." — footnote 41, then the section number.
    provision = f"41{MPC_COMPOSITION}"
    monkeypatch.setattr("sbpeye.chat_retrieval.collection", StubCollection(
        [(2, contents, "Page 3", 3), (41, provision, "Page 19", 19)], "sbp-act"
    ))

    passages = ScopedLawRetriever(db, document).section("9D", token_budget=4000)

    assert passages[0]["chunk_index"] == 41, "the provision, not the contents listing"


def test_a_section_lookup_ignores_a_cross_reference_when_the_provision_exists(
    db, monkeypatch
):
    """"…established under section 9D" is a mention, and mentions outnumber declarations."""
    document = add_law(db)
    definitions = (
        '(p) "Monetary Policy Committee" means the Monetary Policy Committee '
        "established under section 9D."
    )
    monkeypatch.setattr("sbpeye.chat_retrieval.collection", StubCollection(
        [(16, definitions, "Page 9", 9), (41, f"41{MPC_COMPOSITION}", "Page 19", 19)],
        "sbp-act",
    ))

    passages = ScopedLawRetriever(db, document).section("9D", token_budget=4000)

    assert 41 in [item["chunk_index"] for item in passages]


# ------------------------------------------------------------------- resolution honesty

def _client():
    from sbpeye.ai import AIClient, AIConfig

    return AIClient(AIConfig(provider="openai", api_key="test", model="test"))


def test_asking_for_an_act_never_returns_a_circular(db, no_vectors):
    """RC-2, for the law path.

    `get_circular_details` resolves an unfound reference through a top-1 search with no
    threshold, so asking it for the SBP Act returned a 1999 circular about cash reserves.
    A tool that reads laws must not have that failure mode: the corpus either holds the
    instrument or it does not.
    """
    db.add(make_circular(
        id="c-27-1999",
        title="Maintenance of Statutory Cash Reserve under Section 35 (1) of State Bank "
              "of Pakistan Act, 1956",
        reference="BPRD Circular No. 27 of 1999",
        content_text="Every scheduled bank shall maintain an average weekly balance.",
    ))
    db.commit()
    backfill_fts(db)

    payload = json.loads(_client()._law_details_tool(
        {"law_title": "State Bank of Pakistan Act, 1956", "query": "quorum"}, db
    ))

    assert "error" in payload
    assert "citation" not in payload


def test_the_resolved_document_is_named_back(db, no_vectors, monkeypatch, act_chunks):
    """A near-match is reported as one, rather than passed off as what was asked for."""
    add_law(db)
    backfill_fts(db)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.collection", StubCollection(act_chunks, "sbp-act")
    )
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    payload = json.loads(_client()._law_details_tool(
        {"law_title": "State Bank of Pakistan Act", "query": "quorum"}, db
    ))

    assert payload["requested"] == "State Bank of Pakistan Act"
    assert payload["resolved_title"] == "State Bank of Pakistan Act, 1956"
    assert payload["citation"] == "[[law:sbp-act|State Bank of Pakistan Act, 1956]]"
    assert payload["full_text_chars"] == len(SBP_ACT_TEXT)


def test_a_law_that_matches_nothing_says_so(db, no_vectors, monkeypatch):
    """Protect the abstention the round got right: disclosed misses, never papered over."""
    document = add_law(db)
    backfill_fts(db)

    class Empty:
        def get(self, **kwargs):
            return {"documents": [], "metadatas": []}

        def query(self, **kwargs):
            return {"ids": [[]], "metadatas": [[]]}

    monkeypatch.setattr("sbpeye.chat_retrieval.collection", Empty())
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )

    payload = json.loads(_client()._law_details_tool(
        {"law_title": document.title, "query": "reserve requirement"}, db
    ))

    assert payload["passages"] == []
    assert "note" in payload
