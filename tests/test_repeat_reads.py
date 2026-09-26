"""A drill-in call does not hand the model chunks it already holds (`CHAT_CONTEXT_PLAN.md` C5).

Keying a repeat guard on the call's arguments does not work. In the 2026-08-19 worked
example four `get_law_details` calls with four different queries returned chunks 45-49
twice, byte for byte; in the 2026-09-26 round P14 read the Payment Systems and EFT Act
six times. What repeats is what the call *resolved to*, so the guard is keyed on the
chunks: the turn's passage ledger, shared by `search_corpus`, `get_law_details` and
`read_attachment` in the index's own ids.

Pinned here:

- a chunk the model holds is withheld, not re-sent, and the payload names it;
- a call that matched only held chunks says so and says what to do instead;
- a section the model holds is reported as held — it does not fall through to a query
  that hands over something else;
- a chunk `search_corpus`'s law arm sent is held for `get_law_details`, and the reverse;
- `read_attachment` obeys the same ledger.
"""

import json
from datetime import datetime

import pytest

from sbpeye.ai import AIClient, AIConfig, _chunk_ranges
from sbpeye.models import Attachment, RegDocument, RegDocumentVersion
from sbpeye.search import backfill_fts, index_law_fts

from conftest import make_circular


SECTIONS = {
    index: f"{index}. Heading {index}. Provision text about topic{index} and nothing else."
    for index in range(1, 9)
}


class StubStore:
    """One document's chunks, ranked by whether a chunk names the query's topic."""

    def __init__(self, key: str, value: str, chunks: dict[int, str]):
        self.key, self.value, self.chunks = key, value, chunks

    def get(self, where=None, include=None):
        return {
            "documents": list(self.chunks.values()),
            "metadatas": [{"chunk_index": i, self.key: self.value} for i in self.chunks],
        }

    def query(self, query_embeddings=None, **kwargs):
        topic = self.topic
        ordered = sorted(self.chunks, key=lambda i: 0 if topic in self.chunks[i] else 1)
        return {"metadatas": [[{"chunk_index": i} for i in ordered[:1]]]}


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def act(db, monkeypatch):
    document = RegDocument(
        id="eft-act", title="Payment Systems and Electronic Fund Transfers Act, 2007",
        normalized_title="payment systems act", doc_type="law",
        first_seen_at=datetime(2026, 8, 1), last_seen_at=datetime(2026, 8, 1),
    )
    db.add(document)
    db.add(RegDocumentVersion(
        id="eft-act-v1", document_id="eft-act", content_hash="h", file_type="pdf",
        content_text="\n\n".join(SECTIONS.values()), is_current=1,
        first_seen_at=datetime(2026, 8, 1), last_seen_at=datetime(2026, 8, 1),
    ))
    db.commit()
    index_law_fts(db, document)
    backfill_fts(db)
    store = StubStore("document_id", "eft-act", SECTIONS)
    monkeypatch.setattr("sbpeye.chat_retrieval.collection", store)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )
    return store


def _client():
    return AIClient(AIConfig(provider="openai", api_key="test", model="test"))


def _read(client, db, store, **arguments):
    store.topic = arguments.get("query", "")
    arguments.setdefault("law_title", "Payment Systems and Electronic Fund Transfers Act, 2007")
    return json.loads(client._law_details_tool(arguments, db))


def _indexes(payload):
    return [item["chunk_index"] for item in payload["passages"]]


# ------------------------------------------------------------------------- the law tool


def test_the_same_question_twice_returns_a_pointer_the_second_time(db, act):
    client = _client()

    first = _read(client, db, act, query="topic5", limit=1)
    second = _read(client, db, act, query="topic5", limit=1)

    assert _indexes(first) == [3, 4, 5, 6, 7]  # the hit with LAW_NEIGHBOUR_CHUNKS each side
    assert second["passages"] == []
    assert second["provided_earlier"]["chunks"] == "3-7"  # the whole window it holds
    assert "will not return anything new" in second["provided_earlier"]["note"]
    # A held match is not a miss: "nothing matched" would send the model looking elsewhere.
    assert "note" not in second


def test_an_overlapping_question_returns_only_what_is_new(db, act):
    client = _client()

    _read(client, db, act, query="topic5", limit=1)
    second = _read(client, db, act, query="topic8", limit=1)

    # 8's window is 6-8; 6 and 7 went out with the first call.
    assert _indexes(second) == [8]
    assert second["provided_earlier"]["chunks"] == "6-7"
    assert "not repeated" in second["provided_earlier"]["note"]


def test_a_section_already_held_does_not_fall_through_to_the_query(db, act):
    client = _client()

    _read(client, db, act, section="5")
    second = _read(client, db, act, section="5", query="topic1", limit=1)

    assert second["passages"] == []
    assert second["provided_earlier"]["chunks"] == "3-7"
    assert "No provision numbered" not in json.dumps(second)


def test_a_fresh_turn_starts_with_an_empty_ledger(db, act):
    """The ledger is turn state. A new question may need the same provision again."""
    first = _read(_client(), db, act, query="topic5", limit=1)
    again = _read(_client(), db, act, query="topic5", limit=1)

    assert _indexes(again) == _indexes(first)
    assert "provided_earlier" not in again


# ------------------------------------------------------------- shared with the law arm


def _law_arm_row(db, chunk_index):
    document = db.get(RegDocument, "eft-act")
    return {
        "law": document, "version": document.current_version, "snippet": "",
        "passages": [{"text": SECTIONS[chunk_index], "chunk_index": chunk_index,
                      "source_ref": None, "source_page": None}],
    }


def test_a_chunk_search_sent_is_held_for_get_law_details(db, act):
    client = _client()
    AIClient._law_search_payloads(
        [_law_arm_row(db, 5)], sent={}, sent_passages=client._sent_passages,
    )

    payload = _read(client, db, act, query="topic5", limit=1)

    # Search sent chunk 5 alone. Its neighbours are what the model came for — the rest of
    # a provision split across a chunk boundary — so they still go out.
    assert _indexes(payload) == [3, 4, 6, 7]
    assert payload["provided_earlier"]["chunks"] == "5"


def test_a_chunk_get_law_details_sent_is_not_re_sent_by_search(db, act):
    client = _client()
    _read(client, db, act, query="topic5", limit=1)

    row, = AIClient._law_search_payloads(
        [_law_arm_row(db, 5)], sent=client._sent_text_keys,
        sent_passages=client._sent_passages,
    )

    assert "passages" not in row


# ------------------------------------------------------------------- read_attachment


def test_read_attachment_obeys_the_same_ledger(db, monkeypatch):
    circular = make_circular(
        "c-8-2016", reference="BPRD Circular No. 08 of 2016", content_text="See Annex.",
    )
    circular.attachments = [Attachment(
        id="annex-1", circular_id=circular.id, filename="C8-Annex.pdf",
        original_url="https://www.sbp.org.pk/C8-Annex.pdf", file_type="pdf",
        content_text="\n\n".join(SECTIONS.values()), extraction_status="extracted",
    )]
    db.add(circular)
    db.commit()
    store = StubStore("attachment_id", "annex-1", SECTIONS)
    store.topic = "topic5"
    monkeypatch.setattr("sbpeye.chat_retrieval.collection", store)
    monkeypatch.setattr(
        "sbpeye.chat_retrieval.embedding_backend.embed_queries", lambda queries: [[0.0]]
    )
    client = _client()
    arguments = {"circular_reference": "BPRD Circular No. 08 of 2016",
                 "query": "topic5", "limit": 1}

    first = json.loads(client._read_attachment_tool(dict(arguments), db))
    second = json.loads(client._read_attachment_tool(dict(arguments), db))

    assert _indexes(first) == [4, 5, 6]  # ATTACHMENT_NEIGHBOUR_CHUNKS = 1
    assert second["passages"] == []
    assert second["provided_earlier"]["chunks"] == "4-6"


# ---------------------------------------------------------------------------- format


def test_chunk_ranges_read_the_way_a_person_writes_them():
    assert _chunk_ranges([45, 46, 47, 49]) == "45-47, 49"
    assert _chunk_ranges([7]) == "7"
    assert _chunk_ranges([3, 1, 2, 2]) == "1-3"
