"""A chat turn hands the model each document's text once, not once per mention.

The motivating measurement, from `sbpeye_debug.db` over 2026-08-15..25 — 5,096,863
characters of search tool output across 34 turns:

* **458,376 characters** were the same circular's body and passages serialized twice
  inside a *single* search response. Both retrieval arms look the text up from the same
  `_inline_body_texts` / `_passage_sets` result, so a circular that both arms returned
  produced two byte-identical copies.
* **552,709 characters** were the same circular coming back in a *later* call of the
  same turn. Nothing carried the knowledge that it had already been sent.
* Law passages repeat at **40.2%** — 73,051 of 181,923 characters.

Together, **19.8% of all search output**. None of it is information; it is the identical
bytes. On the worked example (session `3fd797e0`) the second `search_corpus` call was 72%
documents the first had already returned.

Two properties are pinned here. The text goes out once per turn — and, just as important,
a withheld copy is not *charged* to the budget it no longer occupies, because a ceiling
spent on bytes that never leave would starve the documents behind it. That would trade a
context saving for a worse answer, which is the one outcome this change must not have.
"""

from types import SimpleNamespace

import pytest

from sbpeye.ai import (
    LAW_SEARCH_PASSAGE_BUDGET_CHARS,
    SEARCH_INLINE_BODY_BUDGET_CHARS,
    SEARCH_PASSAGE_BUDGET_CHARS,
    AIClient,
    AIConfig,
)
from sbpeye.models import RegDocument, RegDocumentVersion

from conftest import make_circular


LETTER = (
    "The Presidents/ Chief Executives of\n\nAll Authorized Dealers in Foreign Exchange\n\n"
    "Dear Sir/ Madam,\n\nIt has been decided to place an annual limit of USD 30,000 per "
    "individual on card based cross-border transactions."
)


def _result(circular, **overrides):
    row = {
        "result_kind": "circular",
        "circular": circular,
        "snippet": "…All Authorized Dealers in Foreign Exchange…",
        "match_source": "circular",
    }
    row.update(overrides)
    return row


def _passage(text, **overrides):
    row = {
        "text": text,
        "match_source": "attachment",
        "attachment_id": "att-1",
        "attachment_filename": "Annexure-A.pdf",
        "source_page": 3,
        "source_ref": None,
    }
    row.update(overrides)
    return row


def _serialize(results, sent, *, budget=SEARCH_INLINE_BODY_BUDGET_CHARS,
               passage_budget=SEARCH_PASSAGE_BUDGET_CHARS):
    """One search response, serialized the way `_execute_tool` serializes one."""
    body_texts = AIClient._inline_body_texts(*results, budget=budget, sent=sent)
    passage_sets = AIClient._passage_sets(
        *results, body_texts=body_texts, budget=passage_budget, sent=sent
    )
    return [
        [AIClient._search_result_payload(r, body_texts, passage_sets, sent) for r in arm]
        for arm in results
    ]


def _law(document_id="sbp-act", title="State Bank of Pakistan Act, 1956", text="x" * 5_000):
    document = RegDocument(
        id=document_id, title=title, normalized_title=title.casefold(), doc_type="law",
    )
    version = RegDocumentVersion(
        id=f"{document_id}-v1", document_id=document_id, content_text=text, is_current=1,
    )
    return document, version


def _law_result(document, version, passages):
    return {"law": document, "version": version, "passages": passages, "snippet": ""}


# --------------------------------------------------------------- within one response


def test_a_circular_in_both_arms_sends_its_letter_once():
    """Both arms read the same lookup, so the second copy was byte-identical."""
    circular = make_circular("fe-07-2022", content_text=LETTER)
    lexical, semantic = _serialize(
        [[_result(circular, lexical_rank=1)], [_result(circular, semantic_rank=2)]],
        sent={},
    )

    assert lexical[0]["full_circular_text"] == LETTER
    assert semantic[0]["duplicate_of_earlier_entry"] is True
    assert semantic[0]["text_provided_earlier"] == ["full_circular_text"]


def test_the_repeat_row_still_reads_as_a_ranked_hit():
    """Only the evidence goes. A row the model cannot place is worse than a long one."""
    circular = make_circular("fe-07-2022", content_text=LETTER)
    _lexical, semantic = _serialize(
        [[_result(circular, lexical_rank=4)], [_result(circular, semantic_rank=1)]],
        sent={},
    )

    entry = semantic[0]
    assert entry["citation"] == f"[[circular:{circular.id}|{circular.display_name}]]"
    assert entry["title"] == circular.title
    assert entry["reference"] == circular.reference
    assert entry["status"] == "active"
    assert entry["semantic_rank"] == 1


def test_the_repeat_row_carries_nothing_but_identity_and_placement():
    """Retired C4: the duplicated *rows* were never the cost, the duplicated evidence was.

    Dropping the second copy's `url`, `tags`, `summary` and excerpt alongside its text
    takes a repeat from ~2,714 characters to ~260 — about 90% of what merging the two
    lists into one would have removed, with both lists still readable top to bottom.
    """
    circular = make_circular("fe-07-2022", content_text=LETTER, tags='["AML", "KYC"]')
    _lexical, semantic = _serialize(
        [[_result(circular, lexical_rank=1)], [_result(circular, semantic_rank=1)]],
        sent={},
    )

    assert set(semantic[0]) == {
        "title", "reference", "department", "date", "status", "citation",
        "semantic_rank", "duplicate_of_earlier_entry", "text_provided_earlier",
    }


def test_matched_passages_are_withheld_on_a_repeat_too():
    circular = make_circular("cl-09-2026", content_text="Details are at Annexure.")
    chunk = "Max Credit Balance: PKR 1,000,000 for Asaan Accounts."
    lexical, semantic = _serialize(
        [
            [_result(circular, lexical_rank=1, passages=[_passage(chunk)])],
            [_result(circular, semantic_rank=1, passages=[_passage(chunk)])],
        ],
        sent={},
    )

    assert [p["passage"] for p in lexical[0]["matching_passages"]] == [chunk]
    assert "matching_passages" not in semantic[0]
    assert semantic[0]["text_provided_earlier"] == [
        "full_circular_text", "matching_passages"
    ]


# ------------------------------------------------------------------- across the turn


def test_a_circular_found_again_later_in_the_turn_is_not_re_sent():
    """The 552,709-character half of the measurement: two search calls, one turn."""
    circular = make_circular("fe-07-2022", content_text=LETTER)
    sent: dict[str, set[str]] = {}

    first, = _serialize([[_result(circular, lexical_rank=1)]], sent)
    second, = _serialize([[_result(circular, lexical_rank=3)]], sent)

    assert first[0]["full_circular_text"] == LETTER
    assert "full_circular_text" not in second[0]
    assert second[0]["duplicate_of_earlier_entry"] is True
    assert second[0]["text_provided_earlier"] == ["full_circular_text"]


def test_the_first_occurrence_is_untouched():
    circular = make_circular("fe-07-2022", content_text=LETTER)

    first, = _serialize([[_result(circular, lexical_rank=1)]], sent={})

    assert first[0]["full_circular_text"] == LETTER
    assert "duplicate_of_earlier_entry" not in first[0]
    assert first[0]["url"] == circular.url


def test_without_a_ledger_nothing_is_withheld():
    """`sent=None` is every caller outside a chat turn — the old behaviour, unchanged.

    "Already sent" only means something inside one conversation. A serializer reached
    from anywhere else must not withhold text on the strength of a turn that is not
    happening.
    """
    circular = make_circular("fe-07-2022", content_text=LETTER)
    lexical, semantic = _serialize(
        [[_result(circular, lexical_rank=1)], [_result(circular, semantic_rank=1)]],
        sent=None,
    )

    assert lexical[0]["full_circular_text"] == LETTER
    assert semantic[0]["full_circular_text"] == LETTER
    assert "duplicate_of_earlier_entry" not in semantic[0]


# ------------------------------------------------------- the budget must not be spent


def test_a_withheld_letter_does_not_consume_the_inline_budget():
    """The failure this change could have introduced, pinned so it cannot come back.

    Charging for a letter that is stripped downstream would spend the ceiling on bytes
    that never leave, and the circulars behind it would lose their letters — a context
    saving bought with a worse answer.
    """
    repeat = make_circular("seen-1", content_text=LETTER)
    fresh = make_circular("new-1", content_text=LETTER)
    sent: dict[str, set[str]] = {}

    _serialize([[_result(repeat)]], sent)                       # turn already sent it
    budget = len(LETTER) + 10                                   # room for exactly one
    second, = _serialize([[_result(repeat), _result(fresh)]], sent, budget=budget)

    assert "full_circular_text" not in second[0]
    assert second[1]["full_circular_text"] == LETTER


def test_a_withheld_passage_set_does_not_consume_the_passage_budget():
    chunk = "y" * 400
    repeat = make_circular("seen-1", content_text="letter")
    fresh = make_circular("new-1", content_text="letter")
    sent: dict[str, set[str]] = {}

    _serialize([[_result(repeat, passages=[_passage(chunk)])]], sent)
    second, = _serialize(
        [[_result(repeat, passages=[_passage(chunk)]),
          _result(fresh, passages=[_passage(chunk)])]],
        sent,
        passage_budget=len(chunk) + 10,
    )

    assert "matching_passages" not in second[0]
    assert [p["passage"] for p in second[1]["matching_passages"]] == [chunk]


def test_one_copy_is_charged_when_the_ledger_is_active():
    """`_passage_sets` charges per copy that goes on the wire, and now that is one.

    Without a ledger a circular in both arms is serialized in both and costs twice —
    `test_passage_budget_charges_a_circular_served_in_both_arms_twice` pins that. With
    the ledger the second copy is withheld, so charging twice would reserve half the
    budget for bytes nobody receives.
    """
    text = "y" * (SEARCH_PASSAGE_BUDGET_CHARS // 2 + 100)
    circular = make_circular("both-1", content_text="short letter")
    lexical = [_result(circular, passages=[_passage(text)])]
    semantic = [_result(circular, passages=[_passage(text)])]

    with_ledger = AIClient._passage_sets(lexical, semantic, body_texts={}, sent={})
    without = AIClient._passage_sets(lexical, semantic, body_texts={})

    assert "both-1" in with_ledger   # one copy fits
    assert without == {}             # two do not


# -------------------------------------------------------------------------- the laws


def test_a_law_passage_set_travels_once_per_turn():
    """Measured at 40.2% of law passage text — the highest repeat rate of any tool."""
    document, version = _law()
    chunk = "The quorum for the Monetary Policy Committee meeting shall be four members."
    sent: dict[str, set[str]] = {}
    result = _law_result(document, version, [{"text": chunk, "source_ref": "9D", "source_page": 4}])

    first = AIClient._law_search_payloads([result], sent=sent)
    second = AIClient._law_search_payloads([result], sent=sent)

    assert [p["passage"] for p in first[0]["passages"]] == [chunk]
    assert "passages" not in second[0]
    assert second[0]["text_provided_earlier"] == ["passages"]
    assert second[0]["citation"] == f"[[law:{document.id}|{document.title}]]"
    assert second[0]["title"] == document.title


def test_a_withheld_law_does_not_consume_the_law_budget():
    repeat, repeat_version = _law("seen-act", "Seen Act, 1956")
    fresh, fresh_version = _law("new-act", "New Act, 1962")
    chunk = "z" * (LAW_SEARCH_PASSAGE_BUDGET_CHARS // 2 + 100)
    sent: dict[str, set[str]] = {}

    seen = _law_result(repeat, repeat_version, [{"text": chunk}])
    AIClient._law_search_payloads([seen], sent=sent)
    second = AIClient._law_search_payloads(
        [seen, _law_result(fresh, fresh_version, [{"text": chunk}])], sent=sent
    )

    assert "passages" not in second[0]
    assert [p["passage"] for p in second[1]["passages"]] == [chunk]


# --------------------------------------------------------------------- the turn scope


def _ledger_client() -> AIClient:
    """A client with no SDK behind it — the chat loop below never reaches a provider."""
    client = AIClient.__new__(AIClient)
    client.config = AIConfig(provider="openrouter", api_key="test", model="test")
    client._client = None
    client._structured_mode = "json_object"
    client._context_budget = None
    client._sent_text_keys = {}
    return client


def test_a_new_client_starts_with_an_empty_ledger():
    assert _ledger_client()._sent_text_keys == {}


@pytest.mark.parametrize("loop", ["_chat_impl", "_stream_chat_impl"])
def test_the_ledger_does_not_survive_into_the_next_turn(loop, monkeypatch):
    """Turn scope is the whole guarantee.

    A ledger that outlived its turn would withhold text from a conversation that never
    received it, and the model would be told to look further up for a letter that is not
    there. `get_ai_client_for_user` builds a client per request today, so this can only
    break by someone reusing one — which is exactly why it is pinned rather than assumed.
    """
    client = _ledger_client()
    client._sent_text_keys = {"stale-1": {"full_circular_text"}}

    def _answer(*_args, stream=False, **_kwargs):
        if stream:
            return iter([SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="answer", tool_calls=None)
            )])])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="answer", tool_calls=None)
        )])

    monkeypatch.setattr(client, "_create_traced_completion", _answer)

    result = getattr(client, loop)([{"role": "user", "content": "hi"}], db=None)
    if loop == "_stream_chat_impl":
        list(result)

    assert client._sent_text_keys == {}
