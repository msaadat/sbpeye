"""Tests for the payload `search_circulars` hands back to the model.

The motivating failure: a chat asked for the per-individual limits on foreign
currency transactions. FE Circular No. 07 of 2022 states the answer outright — an
annual USD 30,000 cap on card-based cross-border transactions — and the semantic arm
did surface it, but only on the turn's last permitted tool round, so no
get_circular_details call could follow. All the model received was `matching_passage`,
whose term-density window had picked the addressee block over the operative sentence,
and it answered that the circular's text "isn't extracted in the provided context".

The circular's whole body is 2,792 chars. These pin the property that fixes it: a
letter that short travels whole in the search result, so the answer survives even
when the lookup budget is spent.
"""

import json

import pytest

from sbpeye.ai import (
    SEARCH_INLINE_BODY_BUDGET_CHARS,
    SEARCH_INLINE_BODY_MAX_CHARS,
    AIClient,
)

from conftest import make_circular


FE_07_2022_BODY = (
    "The Presidents/ Chief Executives of\n\nAll Authorized Dealers in Foreign "
    "Exchange\n\nDear Sir/ Madam,\n\nCross-Border Transactions through Debit/ Credit "
    "Cards\n\nAttention of Authorized Dealers (ADs) is invited towards card-based "
    "transactions conducted by their customers, involving cross-border payments.\n\n"
    "In order to ensure judicious use of cards, including virtual cards, it has been "
    "decided to place an annual limit of USD 30,000 per individual on card based "
    "cross-border transactions. This limit would be applicable for an individual "
    "across the banking industry."
)


def _result(circular, **overrides):
    row = {
        "result_kind": "circular",
        "circular": circular,
        "snippet": "…All Authorized Dealers in Foreign Exchange Dear Sir/ Madam…",
        "match_source": "circular",
    }
    row.update(overrides)
    return row


def test_short_circular_travels_whole_not_as_a_preview():
    circular = make_circular(
        "fe-07-2022",
        reference="FE Circular No. 07 of 2022",
        title="Cross-Border Transactions through Debit/ Credit Cards",
        content_text=FE_07_2022_BODY,
    )
    results = [_result(circular, semantic_rank=5)]

    body_texts = AIClient._inline_body_texts(results)
    payload = AIClient._search_result_payload(results[0], body_texts)

    # The preview alone is what produced the wrong answer; it stays, but no longer
    # carries the burden of being the only text.
    assert "USD 30,000" not in payload["matching_passage"]
    assert payload["full_circular_text"] == FE_07_2022_BODY
    assert "USD 30,000" in payload["full_circular_text"]


def test_long_circular_is_left_as_a_preview():
    circular = make_circular("long-1", content_text="word " * SEARCH_INLINE_BODY_MAX_CHARS)
    results = [_result(circular)]

    payload = AIClient._search_result_payload(results[0], AIClient._inline_body_texts(results))

    assert "full_circular_text" not in payload
    assert payload["matching_passage"]


def test_arms_share_the_budget_round_robin():
    """The semantic arm must not starve because lexical was serialized first.

    A circular whose title looks unrelated to the query is exactly what the semantic
    arm is for, and exactly what a budget spent front-to-back would drop.
    """
    body = "x" * (SEARCH_INLINE_BODY_MAX_CHARS - 1)
    lexical = [
        _result(make_circular(f"lex-{i}", content_text=body))
        for i in range(SEARCH_INLINE_BODY_BUDGET_CHARS // len(body) + 5)
    ]
    semantic = [_result(make_circular("sem-1", content_text=body))]

    body_texts = AIClient._inline_body_texts(lexical, semantic)

    assert "sem-1" in body_texts
    assert len(lexical) > len(body_texts)  # the budget did bind


def test_budget_is_charged_once_for_a_circular_in_both_arms():
    circular = make_circular("both-1", content_text=FE_07_2022_BODY)
    lexical = [_result(circular, lexical_rank=1, semantic_rank=1)]
    semantic = [_result(circular, lexical_rank=1, semantic_rank=1)]

    body_texts = AIClient._inline_body_texts(lexical, semantic)

    assert list(body_texts) == ["both-1"]
    for arm in (lexical, semantic):
        assert AIClient._search_result_payload(arm[0], body_texts)["full_circular_text"]


def test_empty_body_gets_no_key():
    circular = make_circular("blank-1", content_text="   ")
    results = [_result(circular)]

    payload = AIClient._search_result_payload(results[0], AIClient._inline_body_texts(results))

    assert "full_circular_text" not in payload


def test_payload_without_body_texts_is_unchanged():
    """Callers that pass no budget map still get the old shape."""
    circular = make_circular("plain-1", content_text=FE_07_2022_BODY)

    payload = AIClient._search_result_payload(_result(circular))

    assert "full_circular_text" not in payload
    assert json.loads(json.dumps(payload))["citation"].startswith("[[circular:plain-1|")
