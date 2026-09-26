"""A turn hands each document's text over once, whichever tool reaches it (C1).

`CHAT_CONTEXT_PLAN.md` §5.1. C1a made a repeat *search row* a pointer; C12 and C5 did the
same for passages and for the drill-in readers. What was left is `get_circular_details`,
which neither read nor wrote the ledger. Measured over its eleven calls in the 2026-08-26
and 2026-09-26 rounds:

- five re-sent a covering letter the same turn's search had already inlined whole;
- all eleven sent the letter's first 2,000 characters twice in one payload — as
  `content_preview` and again inside `document_context`;
- none recorded what it sent, so a later search would inline the letter a third time.

And the ledger's other half, gap 1: a later search row could never *upgrade* an earlier
one, so a circular first seen as an excerpt kept the excerpt for the rest of the turn.
"""

import json

from sbpeye.ai import AIClient, AIConfig

from test_chat_retrieval import add_circular, disable_vectors, make_session

LETTER = "The circular introduces an updated framework for Asaan accounts."
ANNEX = "Maximum credit balance is PKR 3,000,000 for Asaan accounts."


def _client(tokens=4000):
    return AIClient(AIConfig(provider="openai", api_key="t", model="t",
                             max_context_tokens=tokens))


def _details(client, db, circular):
    return json.loads(client._execute_tool(
        "get_circular_details", {"circular_reference": circular.reference}, db,
        user_query="What is the maximum credit balance?",
    ))


def _search_row(client, circular, *, inline_budget=40_000, **ranks):
    result = {"circular": circular, "snippet": "…Asaan…", **ranks}
    body_texts = AIClient._inline_body_texts(
        [result], budget=inline_budget, sent=client._sent_text_keys
    )
    passage_sets = AIClient._passage_sets(
        [result], body_texts=body_texts, sent=client._sent_text_keys,
        sent_passages=client._sent_passages,
    )
    return AIClient._search_result_payload(
        result, body_texts, passage_sets, client._sent_text_keys, client._sent_passages
    )


# ------------------------------------------------------------- get_circular_details


def test_a_letter_search_already_sent_is_pointed_at_not_repeated(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER, ANNEX)
    client = _client()

    row = _search_row(client, circular, lexical_rank=1)
    payload = _details(client, db, circular)

    assert row["full_circular_text"] == LETTER
    context = payload["document_context"]
    assert "already provided in full earlier" in context
    assert f"[[circular:one|{circular.display_name}]]" in context
    assert LETTER not in context
    assert "content_preview" not in payload
    # What it did not have yet still arrives: the annexure.
    assert ANNEX in context


def test_the_letter_is_not_sent_twice_in_one_payload(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER, ANNEX)

    payload = _details(_client(), db, circular)

    assert LETTER in payload["document_context"]
    assert "content_preview" not in payload


def test_a_letter_too_long_to_include_keeps_its_preview(monkeypatch):
    """The preview is the only sight of the letter when `document_context` cannot hold it."""
    disable_vectors(monkeypatch)
    db = make_session()
    long_letter = "Dear Sir, " + ("the framework is revised " * 400)
    circular = add_circular(db, "one", long_letter, ANNEX)

    payload = _details(_client(tokens=400), db, circular)

    assert payload["content_preview"] == long_letter[:2000]


def test_the_card_fields_search_dropped_are_dropped_here_too(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER, ANNEX)

    payload = _details(_client(), db, circular)

    for gone in ("url", "summary", "tags"):
        assert gone not in payload
    assert next(iter(payload)) == "citation"


def test_what_details_sent_makes_a_later_search_row_a_pointer(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER, ANNEX)
    client = _client()

    _details(client, db, circular)
    row = _search_row(client, circular, semantic_rank=2)

    assert row["duplicate_of_earlier_entry"] is True
    assert "full_circular_text" in row["text_provided_earlier"]
    assert "full_circular_text" not in row


def test_an_annexure_every_chunk_of_which_was_sent_is_held(monkeypatch):
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER, ANNEX)
    client = _client()

    first = _details(client, db, circular)
    second = _details(client, db, circular)

    assert ANNEX in first["document_context"]
    assert ANNEX not in second["document_context"]
    assert "[[attachment:attachment-one|rules-one.pdf]]" in (
        second["document_context"].split("already provided in full earlier")[1]
    )


# ----------------------------------------------------------- gap 1: the upgrade


def test_a_circular_first_seen_as_an_excerpt_gets_its_letter_later(monkeypatch):
    """C1a could only reduce a repeat. A repeat that carries more is an upgrade."""
    disable_vectors(monkeypatch)
    db = make_session()
    circular = add_circular(db, "one", LETTER)
    client = _client()

    first = _search_row(client, circular, inline_budget=0, lexical_rank=9)
    second = _search_row(client, circular, lexical_rank=1)
    third = _search_row(client, circular, lexical_rank=1)

    assert "full_circular_text" not in first
    assert second["duplicate_of_earlier_entry"] is True
    assert second["full_circular_text"] == LETTER
    assert second["letter_not_provided_earlier"] is True
    # Once is once: the upgrade is recorded, and the next repeat is a pointer again.
    assert "full_circular_text" not in third
