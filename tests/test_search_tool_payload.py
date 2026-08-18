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
    SEARCH_PASSAGE_BUDGET_CHARS,
    AIClient,
)
from sbpeye.models import Attachment

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
    assert "USD 30,000" not in payload["matching_passage_excerpt"]
    assert payload["full_circular_text"] == FE_07_2022_BODY
    assert "USD 30,000" in payload["full_circular_text"]


def test_long_circular_is_left_as_a_preview():
    circular = make_circular("long-1", content_text="word " * SEARCH_INLINE_BODY_MAX_CHARS)
    results = [_result(circular)]

    payload = AIClient._search_result_payload(results[0], AIClient._inline_body_texts(results))

    assert "full_circular_text" not in payload
    assert payload["matching_passage_excerpt"]


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


# ---------------------------------------------------------------------------
# Matched passages, and cover letters
# ---------------------------------------------------------------------------
#
# The second motivating failure: asked for the current Asaan Account credit balance
# limit, a chat answered Rs 1,000,000 from a 2022 circular. The 2026 circular that
# raised it to PKR 3,000,000 was retrieved — semantic rank 1 — but its covering letter
# says only "Details of amendments are provided at Annexure", and the single preview
# shown came from the annexure's *notes* chunk, which discusses the limit without
# stating it. Meanwhile the 2022 circular's whole letter, containing "Rs. 1,000,000"
# outright, travelled inline. The model had a complete-looking answer and no reason to
# look further. These pin the two properties that fix it: matched chunks travel whole
# (a windowed table lies), and a covering letter announces the annexure text it omits.

ASAAN_TABLE_CHUNK = (
    "Sr. Account/ Eligible Customer Features/ Requirements Transaction limits14 No. "
    "Wallet type Authorized Financial Institutions as per SBP's Branchless Banking "
    "Regulations 1 Branchless Resident Pakistanis Self-Declaration required "
    "Credit/Debit Banking for source of income Account separately)15: PKR 100,000/D "
    "PKR only PKR 300,000/M Max Credit Balance16: PKR 1,000,000 Banks/ Microfinance "
    "Banks (MFBs) 2 Asaan Account Resident Pakistanis Self-declaration regarding "
    "(Digital/ In- residency status, source Max Credit Balance: person) of income/ "
    "funds and PKR 3,000,000 beneficial ownership, PKR only Restriction for cross "
    "border (outward) transactions Debit: PKR 1,000,000/M"
)
ASAAN_NOTES_CHUNK = (
    "Notes: 1. SBP REs shall inform/ convey limits/ restrictions (if any) on the "
    "accounts to the customers during account opening 2. Following exceptions for the "
    "limits mentioned for Asaan Accounts are available: a. Credit transactions beyond "
    "total credit balance limits in case of inward remittances in Asaan Account "
    "subject to proper analysis of transaction and evaluation of risk"
)


def _passage(text, **overrides):
    row = {
        "text": text,
        "match_source": "attachment",
        "attachment_id": "att-1",
        "attachment_filename": "CL9-Framework.pdf",
        "source_page": 18,
        "source_ref": None,
    }
    row.update(overrides)
    return row


def test_matched_chunks_travel_whole_so_a_table_cannot_be_misquoted():
    """The notes chunk ranks first and states no figure; the table chunk states it.

    A term-density window over the table pairs the Branchless row's label with text
    from the Asaan row, so the figure has to arrive as a whole chunk or not at all.
    """
    circular = make_circular("cl-09-2026", content_text="Details of amendments are at Annexure.")
    results = [_result(
        circular,
        semantic_rank=1,
        match_source="attachment",
        passages=[_passage(ASAAN_NOTES_CHUNK, source_page=21), _passage(ASAAN_TABLE_CHUNK)],
    )]

    body_texts = AIClient._inline_body_texts(results)
    payload = AIClient._search_result_payload(
        results[0], body_texts, AIClient._passage_sets(results, body_texts=body_texts)
    )

    passages = payload["matching_passages"]
    assert [item["passage"] for item in passages] == [ASAAN_NOTES_CHUNK, ASAAN_TABLE_CHUNK]
    assert "PKR 3,000,000" in passages[1]["passage"]
    assert passages[1]["attachment_citation"] == "[[attachment:att-1|CL9-Framework.pdf]]"
    assert passages[0]["page"] == 21


def test_cover_letter_declares_the_annexure_text_it_omits():
    """`full_circular_text` on a cover letter is complete and answers nothing.

    `attachment_text_chars` is what separates it from a circular that really does
    state its own terms, so it has to be present on one and absent on the other.
    """
    cover = make_circular("cl-09-2026", content_text="Details of amendments are at Annexure.")
    cover.attachments = [
        Attachment(
            id="att-1", circular_id=cover.id, filename="CL9-Framework.pdf",
            original_url="https://www.sbp.org.pk/CL9-Framework.pdf", file_type="pdf",
            content_text=ASAAN_TABLE_CHUNK, extraction_status="extracted",
        )
    ]
    standalone = make_circular("cl-10-2022", content_text="Total Credit Balance Limit: Rs. 1,000,000")

    for circular in (cover, standalone):
        results = [_result(circular)]
        payload = AIClient._search_result_payload(
            results[0], AIClient._inline_body_texts(results)
        )
        if circular is cover:
            assert payload["attachment_text_chars"] == len(ASAAN_TABLE_CHUNK)
        else:
            assert "attachment_text_chars" not in payload


def test_body_chunks_are_dropped_when_the_letter_already_travels_whole():
    circular = make_circular("dup-1", content_text=FE_07_2022_BODY)
    results = [_result(circular, passages=[_passage(FE_07_2022_BODY, match_source="circular",
                                                   attachment_id=None, attachment_filename=None)])]

    body_texts = AIClient._inline_body_texts(results)
    payload = AIClient._search_result_payload(
        results[0], body_texts, AIClient._passage_sets(results, body_texts=body_texts)
    )

    assert payload["full_circular_text"] == FE_07_2022_BODY
    assert "matching_passages" not in payload


def test_passage_budget_charges_a_circular_served_in_both_arms_twice():
    """Charging once would understate the wire cost by half."""
    text = "y" * (SEARCH_PASSAGE_BUDGET_CHARS // 2 + 100)
    circular = make_circular("both-1", content_text="short letter")
    lexical = [_result(circular, passages=[_passage(text)])]
    semantic = [_result(circular, passages=[_passage(text)])]

    both = AIClient._passage_sets(lexical, semantic, body_texts={})
    one_arm = AIClient._passage_sets(lexical, body_texts={})

    assert both == {}          # 2 x over half the budget does not fit
    assert "both-1" in one_arm  # 1 x does


def test_passage_budget_favours_the_head_of_each_arm():
    text = "z" * (SEARCH_PASSAGE_BUDGET_CHARS // 3)
    lexical = [
        _result(make_circular(f"lex-{i}", content_text="letter"), passages=[_passage(text)])
        for i in range(6)
    ]
    semantic = [_result(make_circular("sem-1", content_text="letter"), passages=[_passage(text)])]

    chosen = AIClient._passage_sets(lexical, semantic, body_texts={})

    assert "sem-1" in chosen           # round-robin reached the semantic arm
    assert len(chosen) < len(lexical)  # the budget did bind


def test_excerpt_is_used_when_no_chunk_was_located():
    circular = make_circular("lex-only", content_text=FE_07_2022_BODY)
    results = [_result(circular, passages=[])]

    payload = AIClient._search_result_payload(results[0], {}, {})

    assert "matching_passages" not in payload
    assert payload["matching_passage_excerpt"]
