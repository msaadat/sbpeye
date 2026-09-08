"""Whether a chat question can reach the inside of a circular's annexure.

The motivating failure, session `15fd3ff1` (2026-09-08): asked what Basel III counts as
a "stable" deposit for the liquidity ratios, the model answered that the defining
section of BPRD Circular No. 08 of 2016's annexure was "not available" to it. The
annexure — 134,205 characters, 222 indexed chunks — had been in the corpus for two
months, and the vector arm had *retrieved* the defining paragraph, at ranks 8 and 9 of
the store. Everything after retrieval lost it:

* `_collect_evidence` kept three chunks per circular, in distance order, so the
  annexure's 4th and 5th nearest chunks — page 15, the definition — were discarded;
* every later `search_corpus` call ranked the same circular first and came back as a
  pointer to the first call's three chunks, because the turn ledger knew *that* the
  document had been sent and not *which parts*;
* `get_circular_details` re-chunked the annexure locally at 350 words under the index's
  ``{doc}__chunk_{n}`` ids, which the store had written at 130 words — so a vector hit
  on chunk 53 (page 17) was looked up as local chunk 53 (page 34) and boosted the wrong
  passage; it returned the contents page;
* no tool could be pointed at a page or a paragraph of an attachment, though the model
  had read "Part 1, section 4" off that contents page and said so.

These pin the properties that fix each.
"""

import json
from datetime import datetime

import pytest

from sbpeye.ai import (
    SEARCH_PASSAGES_PER_RESULT_CHARS,
    AIClient,
    AIConfig,
)
from sbpeye.chat_retrieval import (
    ATTACHMENT_NEIGHBOUR_CHUNKS,
    ScopedAttachmentRetriever,
    ScopedChatRetriever,
    build_chat_context,
    passage_key,
)
from sbpeye.chat_steps import build_step
from sbpeye.models import Attachment
from sbpeye.search import (
    MatchEvidence,
    SearchEngine,
    _collect_evidence,
    is_listing_chunk,
    order_evidence,
    search_engine,
    tokenize,
)

from conftest import make_circular


CIRCULAR_ID = "bprd-08-2016"
ANNEX_ID = "c8-annex"

# The real text, abridged, with the real chunk split: SBP's PDF puts the run-off
# framing at the foot of page 14 and the definition on page 15, and the chunker cuts
# the definition from the conditions that complete it.
CONTENTS = (
    "Contents Introduction – Basel III Liquidity Standards ...................... 1 "
    "Part 1: The Liquidity Coverage Ratio (LCR) ................................ 2 "
    "4. Total net cash outflows – the denominator of LCR ...................... 8 "
    "Part 2: Net Stable Funding Ratio ........................................ 25 "
    "B. Definition of Available Stable Funding ............................... 27"
)
SCOPE = (
    "Part 1: The Liquidity Coverage Ratio (LCR) 1. Scope of Application 1.1. The "
    "Liquidity Coverage Ratio (LCR) is a quantitative requirement which aims to ensure "
    "that a bank maintains an adequate level of unencumbered high quality liquid assets."
)
RUN_OFF_FRAMING = (
    "4.10. The retail deposits are divided into stable and less stable portions of "
    "funds as described below, with minimum run-off rates listed for each category."
)
DEFINITION = (
    "Stable retail deposits (run-off rate = 5%) 4.11. Stable retail deposits are the "
    "amount of the retail deposits that are fully insured by an effective deposit "
    "insurance scheme, and the depositors have other established relationships with "
    "the bank that make deposit withdrawal highly unlikely."
)
LESS_STABLE = (
    "Less stable deposits (run-off rate = 10%) 4.12. All retail deposits that do not "
    "fulfill the conditions of stable deposits are to be considered as less stable "
    "deposits."
)
TEMPLATE = (
    "5. Reporting Template of LCR (LR-1) LR-1 Liquidity Coverage Ratio (LCR) Bank "
    "Name: Month: I. High Quality Liquid Assets (HQLA) Level 1 Assets 100%."
)

# (chunk_index, page, text) — the index's own geometry, ids `{ANNEX_ID}__chunk_{n}`.
ANNEX_CHUNKS = [
    (4, 5, CONTENTS),
    (10, 8, SCOPE),
    (42, 14, RUN_OFF_FRAMING),
    (43, 15, DEFINITION),
    (44, 15, LESS_STABLE),
    (92, 25, TEMPLATE),
]


def _meta(index, page):
    return {
        "circular_id": CIRCULAR_ID,
        "attachment_id": ANNEX_ID,
        "doc_type": "attachment",
        "filename": "C8-Annex.pdf",
        "chunk_index": index,
        "ref": f"Page {page}",
        "page_start": page,
        "page_end": page,
    }


class AnnexStore:
    """The annexure's chunks, addressable the way Chroma addresses them.

    `query` ranks by the order given in `ranked`, reproducing the real failure: the
    scope paragraph and the reporting template repeat "LCR", so they sit nearer the
    query than the definition does.
    """

    def __init__(self, chunks=ANNEX_CHUNKS, ranked=(10, 92, 4, 43, 44)):
        self.chunks = {index: (page, text) for index, page, text in chunks}
        self.ranked = ranked
        self.get_calls: list[dict] = []
        self.query_calls: list[dict] = []

    def _id(self, index):
        return f"{ANNEX_ID}__chunk_{index}"

    def get(self, **kwargs):
        self.get_calls.append(kwargs)
        if "ids" in kwargs:
            wanted = [
                (int(cid.rsplit("_", 1)[1]), cid) for cid in kwargs["ids"]
                if cid.startswith(f"{ANNEX_ID}__chunk_")
            ]
            found = [(index, cid) for index, cid in wanted if index in self.chunks]
            return {
                "ids": [cid for _, cid in found],
                "documents": [self.chunks[index][1] for index, _ in found],
                "metadatas": [_meta(index, self.chunks[index][0]) for index, _ in found],
            }
        ordered = sorted(self.chunks)
        return {
            "ids": [self._id(index) for index in ordered],
            "documents": [self.chunks[index][1] for index in ordered],
            "metadatas": [_meta(index, self.chunks[index][0]) for index in ordered],
        }

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        ranked = [index for index in self.ranked if index in self.chunks]
        return {
            "ids": [[self._id(index) for index in ranked]],
            "documents": [[self.chunks[index][1] for index in ranked]],
            "distances": [[0.4 + 0.02 * rank for rank in range(len(ranked))]],
            "metadatas": [[_meta(index, self.chunks[index][0]) for index in ranked]],
        }


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def store(monkeypatch):
    stub = AnnexStore()
    monkeypatch.setattr("sbpeye.search.collection", stub)
    monkeypatch.setattr("sbpeye.chat_retrieval.collection", stub)
    monkeypatch.setattr("sbpeye.search.embedding_backend.embed_queries", lambda q: [[0.1]])
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda q: [[0.1]]
    )
    return stub


def add_circular(db, *, attachment_text=None):
    circular = make_circular(
        CIRCULAR_ID,
        reference="BPRD Circular No. 08 of 2016",
        title="Implementation of Basel III – Liquidity Standards",
        date=datetime(2016, 6, 23),
        content_text=(
            "The State Bank of Pakistan intends to adopt the Liquidity Standards as "
            "proposed by the Basel Committee. Enclosed: SBP Basel III Liquidity Instructions."
        ),
    )
    if attachment_text is None:
        # The indexed pages, plus enough unindexed filler that the annexure is too
        # long to travel whole as a "small document" — the real one is 134k chars.
        attachment_text = "\n".join(
            f"[[SBPEYE_PAGE:{page}]]\n{text}" for _, page, text in ANNEX_CHUNKS
        ) + "\n[[SBPEYE_PAGE:60]]\n" + ("background " * 3_000)
    circular.attachments = [
        Attachment(
            id=ANNEX_ID,
            circular_id=CIRCULAR_ID,
            filename="C8-Annex.pdf",
            original_url="https://www.sbp.org.pk/assets/documents/circulars/C8-Annex.pdf",
            file_type="pdf",
            content_text=attachment_text,
            extraction_status="extracted",
            is_vectorized=True,
        )
    ]
    db.add(circular)
    db.commit()
    return circular


def _client():
    client = AIClient(AIConfig(max_context_tokens=8_000))
    client._search_payload_budgets = lambda: (40_000, 24_000, 8_000)
    return client


def _annex_rows(payload, arm=None):
    """The circular's rows, lexical arm first — the arm that carries the text.

    The test database has no FTS backfill, so the circular usually ranks in the
    semantic arm only; a test that cares which arm carried it names one.
    """
    arms = (arm,) if arm else ("lexical_results", "semantic_results")
    return [
        row for name in arms for row in payload.get(name, [])
        if row.get("reference") == "BPRD Circular No. 08 of 2016"
    ]


def _pages(row):
    return [item["page"] for item in row.get("matching_passages", [])]


# ------------------------------------------------- scoped retrieval reads the index

def test_scoped_chunks_are_the_indexed_chunks(db, store):
    """The id a vector hit carries must name the same bytes locally.

    The old retriever chunked `content_text` itself at 350 words under the index's
    130-word ids. On the real annexure Chroma's chunk 53 is page 17 and the local
    chunk 53 was page 34.
    """
    circular = add_circular(db)

    retriever = ScopedChatRetriever(db, [circular.id])
    by_id = {chunk.chunk_id: chunk for chunk in retriever._chunks}

    for index, page, text in ANNEX_CHUNKS:
        chunk = by_id[f"{ANNEX_ID}__chunk_{index}"]
        assert chunk.text == text
        assert chunk.chunk_index == index
        assert chunk.page == page


def test_scoped_chunks_fall_back_to_index_geometry_when_the_store_is_empty(db, monkeypatch):
    class Empty:
        def get(self, **kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

    monkeypatch.setattr("sbpeye.chat_retrieval.collection", Empty())
    circular = add_circular(db)

    retriever = ScopedChatRetriever(db, [circular.id])
    annex = [chunk for chunk in retriever._chunks if chunk.document_id == ANNEX_ID]

    assert annex, "an unindexed attachment is still searchable lexically"
    assert annex[0].chunk_id == passage_key(ANNEX_ID, 0)
    assert annex[0].page == 5


def test_circular_details_returns_the_definition_not_the_contents_page(db, store):
    """The P15 failure, on the circular side.

    With a scrambled vector arm the contents page won on term density. With the
    arms scoring the same units, and a query for the definition, the definition wins.
    """
    circular = add_circular(db)
    store.ranked = (43, 44, 42, 10, 4)

    payload = json.loads(_client()._execute_tool(
        "get_circular_details",
        {"circular_reference": circular.reference,
         "query": "definition of stable retail deposits"},
        db,
        user_query="what are stable deposits?",
    ))
    context = payload["document_context"]

    assert DEFINITION in context
    if CONTENTS in context:
        assert context.index(DEFINITION) < context.index(CONTENTS)


# ------------------------------------------------- evidence: wider, ordered, with neighbours

def test_attachment_evidence_is_not_capped_at_three(store):
    results = store.query()

    _, evidence = _collect_evidence(
        results, "circular_id", SearchEngine.EVIDENCE_K, SearchEngine.ATTACHMENT_EVIDENCE_K
    )

    texts = [item.text for item in evidence[CIRCULAR_ID]]
    assert DEFINITION in texts, "the annexure's 4th nearest chunk survives"
    assert SearchEngine.ATTACHMENT_EVIDENCE_K > SearchEngine.EVIDENCE_K


def test_body_chunks_keep_their_own_cap(store):
    body = {
        "ids": [[f"c__chunk_{i}" for i in range(5)]],
        "documents": [[f"body {i}" for i in range(5)]],
        "distances": [[0.1 * i for i in range(5)]],
        "metadatas": [[{"circular_id": "c", "doc_type": "circular", "chunk_index": i}
                       for i in range(5)]],
    }

    _, evidence = _collect_evidence(body, "circular_id", 3, 8)

    assert len(evidence["c"]) == 3


def test_a_contents_page_is_a_listing_and_ranks_last():
    assert is_listing_chunk(CONTENTS)
    assert not is_listing_chunk(DEFINITION)

    tokens = set(tokenize("stable deposits liquidity coverage ratio LCR run-off rates"))
    evidence = [
        MatchEvidence(text=CONTENTS, doc_type="attachment", source_id=ANNEX_ID, chunk_index=4),
        MatchEvidence(text=DEFINITION, doc_type="attachment", source_id=ANNEX_ID, chunk_index=43),
    ]

    ordered = order_evidence(evidence, tokens)

    assert [item.chunk_index for item in ordered] == [43, 4]


def test_a_hit_travels_with_its_neighbours_in_document_order():
    """Overlapping windows are one passage; merely adjacent ones are not."""
    tokens = set(tokenize("stable deposits"))
    evidence = [
        MatchEvidence(text=SCOPE, doc_type="attachment", source_id=ANNEX_ID, chunk_index=10),
        MatchEvidence(text=DEFINITION, doc_type="attachment", source_id=ANNEX_ID, chunk_index=43),
        MatchEvidence(text=LESS_STABLE, doc_type="attachment", source_id=ANNEX_ID, chunk_index=44),
        MatchEvidence(text=RUN_OFF_FRAMING, doc_type="attachment", source_id=ANNEX_ID,
                      chunk_index=42, is_neighbour=True),
        MatchEvidence(text="scope continued", doc_type="attachment", source_id=ANNEX_ID,
                      chunk_index=11, is_neighbour=True),
    ]

    ordered = [item.chunk_index for item in order_evidence(evidence, tokens, radius=1)]

    assert ordered[:3] == [42, 43, 44], "the definition, framed and completed"
    assert ordered[3:] == [10, 11]


def test_search_fetches_neighbours_in_one_store_round_trip(db, store):
    add_circular(db)
    store.ranked = (43,)

    arms = search_engine.dual_arm_search("stable deposits", db, limit=5)

    fetches = [call for call in store.get_calls if "ids" in call]
    assert len(fetches) == 1
    assert set(fetches[0]["ids"]) == {f"{ANNEX_ID}__chunk_42", f"{ANNEX_ID}__chunk_44"}
    row = next(r for r in arms["semantic_results"] if r["circular"].id == CIRCULAR_ID)
    assert [p["chunk_index"] for p in row["passages"]] == [42, 43, 44]
    assert [p["is_neighbour"] for p in row["passages"]] == [True, False, True]
    assert row["source_page"] == 15, "the hit cites its own page, not a neighbour's"
    assert ATTACHMENT_NEIGHBOUR_CHUNKS >= 1


def test_neighbours_of_a_contents_page_are_not_fetched(db, store):
    add_circular(db)
    store.ranked = (4,)

    search_engine.dual_arm_search("liquidity", db, limit=5)

    assert not [call for call in store.get_calls if "ids" in call]


# ------------------------------------------------- the passage ledger

def test_the_first_search_carries_the_definition(db, store):
    add_circular(db)

    payload = json.loads(_client()._execute_tool(
        "search_corpus", {"query": "stable deposits run-off"}, db,
    ))

    row = _annex_rows(payload)[0]
    assert DEFINITION in [p["passage"] for p in row["matching_passages"]]
    assert 15 in _pages(row)


def test_a_repeat_search_hands_over_the_passages_the_first_did_not(db, store):
    """Six searches, all pointers to the first three chunks — the failure, directly."""
    add_circular(db)
    client = _client()
    store.ranked = (10, 92)
    first = json.loads(client._execute_tool("search_corpus", {"query": "LCR"}, db))
    first_row = _annex_rows(first)[0]
    assert sorted(_pages(first_row)) == [8, 25]

    store.ranked = (43, 44)
    second = json.loads(client._execute_tool(
        "search_corpus", {"query": "stable deposits definition"}, db,
    ))
    second_row = _annex_rows(second)[0]

    assert second_row["duplicate_of_earlier_entry"] is True
    assert second_row["passages_not_provided_earlier"] is True
    assert DEFINITION in [p["passage"] for p in second_row["matching_passages"]]
    assert SCOPE not in [p["passage"] for p in second_row["matching_passages"]]
    assert "full_circular_text" not in second_row, "the letter is still a pointer"


def test_a_repeat_search_with_nothing_new_is_still_a_pointer(db, store):
    add_circular(db)
    client = _client()
    store.ranked = (43,)
    client._execute_tool("search_corpus", {"query": "stable deposits"}, db)

    again = json.loads(client._execute_tool("search_corpus", {"query": "stable deposits"}, db))
    row = _annex_rows(again)[0]

    assert row["duplicate_of_earlier_entry"] is True
    assert "matching_passages" not in row
    assert "matching_passages" in row["text_provided_earlier"]


def test_a_circular_in_both_arms_sends_new_passages_once(db, store):
    add_circular(db)
    store.ranked = (43,)

    payload = json.loads(_client()._execute_tool("search_corpus", {"query": "stable"}, db))

    carried = [
        arm for arm in ("lexical_results", "semantic_results")
        if any("matching_passages" in row for row in _annex_rows(payload, arm))
    ]
    assert len(carried) == 1


def test_sent_passages_are_not_charged_to_the_budget():
    circular = make_circular("c-1", content_text="letter")
    passage = {"text": "x" * 1_000, "match_source": "attachment",
               "attachment_id": "a-1", "attachment_filename": "a.pdf", "chunk_index": 7}
    results = [{"result_kind": "circular", "circular": circular, "snippet": "",
                "match_source": "attachment", "passages": [passage]}]
    sent_passages = {"c-1": {passage_key("a-1", 7)}}

    chosen = AIClient._passage_sets(
        results, body_texts={}, budget=1_000, sent={"c-1": ["matching_passages"]},
        sent_passages=sent_passages,
    )

    assert chosen == {}


def test_one_result_cannot_spend_the_whole_passage_budget():
    """With attachment evidence uncapped, one annexure could starve the results after it."""
    circular = make_circular("c-1", content_text="letter")
    chunk = "y" * 2_000
    passages = [
        {"text": chunk, "match_source": "attachment", "attachment_id": "a-1",
         "attachment_filename": "a.pdf", "chunk_index": i}
        for i in range(20)
    ]
    results = [{"result_kind": "circular", "circular": circular, "snippet": "",
                "match_source": "attachment", "passages": passages}]

    chosen = AIClient._passage_sets(results, body_texts={}, budget=100_000)

    spent = sum(len(item["text"]) for item in chosen["c-1"])
    assert spent <= SEARCH_PASSAGES_PER_RESULT_CHARS + len(chunk)
    assert len(chosen["c-1"]) >= 1


def test_circular_details_points_at_passages_a_search_already_sent(db, store):
    add_circular(db)
    client = _client()
    store.ranked = (43, 44)
    client._execute_tool("search_corpus", {"query": "stable deposits"}, db)

    payload = json.loads(client._execute_tool(
        "get_circular_details", {"circular_reference": "BPRD Circular No. 08 of 2016"},
        db, user_query="stable deposits",
    ))
    context = payload["document_context"]

    assert "already provided earlier" in context
    assert "chunk 43 (page 15)" in context, "pointed at by index and page"
    assert DEFINITION not in context, "sent by the search; pointed at here"
    # The search sent 43 and 44 with 42 as their neighbour. Of the chunks that match
    # "stable deposits" that leaves the contents page, and the details call spends its
    # budget on that rather than on a fourth copy of the definition.
    assert CONTENTS in context, "the one matching chunk the search did not send"


# ------------------------------------------------- read_attachment

def test_a_page_can_be_read_whole(db, store):
    add_circular(db)

    payload = json.loads(_client()._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016", "page": 15},
        db,
    ))

    assert [p["passage"] for p in payload["passages"]] == [DEFINITION, LESS_STABLE]
    assert payload["attachment_citation"] == f"[[attachment:{ANNEX_ID}|C8-Annex.pdf]]"
    assert payload["pages"] == {"first": 5, "last": 25, "count": 5}
    assert "note" not in payload


def test_a_paragraph_can_be_fetched_by_its_number(db, store):
    """The model read "4.11" off the contents page and had no verb for it."""
    add_circular(db)

    payload = json.loads(_client()._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016", "section": "4.11"},
        db,
    ))

    passages = [p["passage"] for p in payload["passages"]]
    assert DEFINITION in passages
    assert RUN_OFF_FRAMING in passages, "the paragraph before it, one chunk away"
    assert CONTENTS not in passages, "the contents page names 4.11 too"


def test_a_query_returns_hits_with_their_neighbours(db, store):
    add_circular(db)
    store.ranked = (43,)

    payload = json.loads(_client()._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016",
         "query": "what counts as a stable deposit", "limit": 1},
        db,
    ))

    assert [p["chunk_index"] for p in payload["passages"]] == [42, 43, 44]
    assert payload["passage_count"] == 3


def test_an_attachment_can_be_named_by_its_citation_handle(db, store):
    circular = add_circular(db)
    circular.attachments.append(Attachment(
        id="c8-form", circular_id=CIRCULAR_ID, filename="LR-1-Form.pdf",
        original_url="https://www.sbp.org.pk/x.pdf", file_type="pdf",
        content_text="[[SBPEYE_PAGE:1]]\nReturn form.", extraction_status="extracted",
    ))
    db.commit()
    client = _client()

    ambiguous = json.loads(client._execute_tool(
        "read_attachment", {"circular_reference": "BPRD Circular No. 08 of 2016", "page": 15}, db,
    ))
    by_handle = json.loads(client._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016",
         "attachment": "[[a:C8-Annex]]", "page": 15},
        db,
    ))

    assert "error" in ambiguous
    assert {a["attachment_citation"] for a in ambiguous["attachments"]} == {
        f"[[attachment:{ANNEX_ID}|C8-Annex.pdf]]", "[[attachment:c8-form|LR-1-Form.pdf]]",
    }
    assert by_handle["filename"] == "C8-Annex.pdf"
    assert DEFINITION in [p["passage"] for p in by_handle["passages"]]


def test_a_missing_page_is_reported_not_invented(db, store):
    add_circular(db)

    payload = json.loads(_client()._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016", "page": 99},
        db,
    ))

    assert payload["passages"] == []
    assert "No page 99" in payload["note"]
    assert "5-25" in payload["note"]


def test_read_attachment_records_what_it_sent(db, store):
    """A search after a read points at the read's passages rather than re-sending them."""
    add_circular(db)
    client = _client()
    client._execute_tool(
        "read_attachment",
        {"circular_reference": "BPRD Circular No. 08 of 2016", "page": 15},
        db,
    )
    store.ranked = (43, 44)

    payload = json.loads(client._execute_tool("search_corpus", {"query": "stable"}, db))
    row = _annex_rows(payload)[0]

    assert DEFINITION not in [p["passage"] for p in row.get("matching_passages", [])]


def test_an_unindexed_attachment_is_still_readable(db, monkeypatch):
    class Empty:
        def get(self, **kwargs):
            return {"ids": [], "documents": [], "metadatas": []}

        def query(self, **kwargs):
            return {"ids": [[]], "metadatas": [[]]}

    monkeypatch.setattr("sbpeye.chat_retrieval.collection", Empty())
    circular = add_circular(db)

    retriever = ScopedAttachmentRetriever(circular.attachments[0])

    assert retriever.chunk_count > 0
    page = retriever.page(15, token_budget=4_000)
    # The fixture writes page 15 as two page sections, so index geometry gives two chunks.
    assert [p["passage"] for p in page] == [
        f"C8-Annex.pdf. Page 15. {DEFINITION}", f"C8-Annex.pdf. Page 15. {LESS_STABLE}",
    ]


def test_the_tool_is_declared_and_labelled():
    from sbpeye.ai import TOOLS, tool_activity_label

    names = [tool["function"]["name"] for tool in TOOLS]
    assert "read_attachment" in names
    schema = next(t for t in TOOLS if t["function"]["name"] == "read_attachment")
    assert {"page", "section", "query", "attachment"} <= set(
        schema["function"]["parameters"]["properties"]
    )
    assert tool_activity_label("read_attachment") == "Reading the annexure"
    details = next(t for t in TOOLS if t["function"]["name"] == "get_circular_details")
    assert "query" in details["function"]["parameters"]["properties"]


def test_a_read_step_lists_its_passages_under_the_attachment():
    step = build_step("read_attachment", {"page": 15}, json.dumps({
        "circular": "BPRD Circular No. 08 of 2016",
        "citation": "[[circular:c-1|BPRD Circular No. 08 of 2016]]",
        "attachment_citation": "[[attachment:a-1|C8-Annex.pdf]]",
        "filename": "C8-Annex.pdf",
        "passages": [
            {"chunk_index": 43, "page": 15, "passage": DEFINITION},
            {"chunk_index": 44, "page": 15, "passage": LESS_STABLE},
        ],
        "passage_count": 2,
    }), label="Reading the annexure")

    assert step["summary"] == "2 passages from C8-Annex.pdf"
    assert [hit["note"] for hit in step["hits"]] == ["Page 15", "Page 15"]
    assert all(hit["citation"] == "[[attachment:a-1|C8-Annex.pdf]]" for hit in step["hits"])
    assert step["hits"][0]["reference"] == "BPRD Circular No. 08 of 2016"
