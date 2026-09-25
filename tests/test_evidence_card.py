"""The evidence card — what one `search_corpus` row is (`docs/CHAT_REDESIGN.md` §5, R1).

A card is identity, then lineage, then what else exists, then the evidence. Two lines
are new, and each answers a question the old payload left the model to infer:

- **ANNEXURES** — *which* of a circular's attachments this row carries. The old
  `attachment_text_chars` was one total: it said a cover letter had text behind it, not
  which annexure, nor whether the passages shown came from it.
- **REFERENCES** — the Acts the circular names, from `reg_document_links`, as handles
  `get_law_details` can open. Called *references* because 791 of the 814 edges are name
  matches; the card does not assert a relationship nobody typed.

And four fields went, each for a measured reason — `summary` is null for 3,649 of 3,655
circulars, `url` and `tags` are never used in an answer, `match_source` is restated by
every passage.
"""

from datetime import datetime

from sbpeye.ai import (
    MAX_CARD_ANNEXURES,
    MAX_CARD_REFERENCED_LAWS,
    AIClient,
    _passage_ledger_key,
)
from sbpeye.models import Attachment, RegDocument, RegDocumentLink, RegDocumentVersion

from conftest import make_circular


def _attachment(circular, attachment_id, filename, text="annexure text " * 20):
    return Attachment(
        id=attachment_id, circular_id=circular.id, filename=filename,
        original_url=f"https://www.sbp.org.pk/{filename}", file_type="pdf",
        content_text=text, extraction_status="extracted" if text else "failed",
    )


def _passage(text, attachment_id=None, filename=None, chunk_index=0):
    return {
        "text": text,
        "match_source": "attachment" if attachment_id else "circular",
        "attachment_id": attachment_id,
        "attachment_filename": filename,
        "chunk_index": chunk_index,
        "source_page": 3,
    }


def _result(circular, **overrides):
    row = {"circular": circular, "snippet": "…a short window…", "match_source": "circular"}
    row.update(overrides)
    return row


def _card(result, *, sent=None, sent_passages=None):
    body_texts = AIClient._inline_body_texts([result], sent=sent)
    passage_sets = AIClient._passage_sets(
        [result], body_texts=body_texts, sent=sent, sent_passages=sent_passages,
    )
    return AIClient._search_result_payload(
        result, body_texts, passage_sets, sent, sent_passages
    )


def _law(document_id, title, *, delisted=False):
    return RegDocument(
        id=document_id, title=title, normalized_title=title.casefold(), doc_type="law",
        delisted_at=datetime(2026, 1, 1) if delisted else None,
    )


def _link(document, link_type="references", confidence=None):
    return RegDocumentLink(document=document, link_type=link_type, confidence=confidence)


# ------------------------------------------------------------------------- shape


def test_the_card_leads_with_identity_and_drops_the_fields_nothing_used():
    circular = make_circular(
        "c-1", content_text="Short letter.", summary="A summary.", tags='["AML"]',
    )

    card = _card(_result(circular, lexical_rank=2, semantic_rank=1))

    assert list(card)[:6] == ["citation", "title", "reference", "department", "date", "status"]
    for gone in ("summary", "tags", "url", "match_source", "attachment_citation",
                 "attachment_text_chars"):
        assert gone not in card
    assert (card["lexical_rank"], card["semantic_rank"]) == (2, 1)
    assert card["full_circular_text"] == "Short letter."


def test_lineage_comes_before_the_evidence():
    """An amender read after the letter is an amender read too late."""
    circular = make_circular("c-1", content_text="Limit is PKR 500,000.")
    amender = {"citation": "[[circular:c-2|BPRD Circular No. 03 of 2021]]",
               "date": "2021-02-11", "type": "amends"}

    card = _card(_result(circular, amended_by=[amender], note="Read the amender."))

    keys = list(card)
    assert keys.index("amended_by") < keys.index("full_circular_text")


# --------------------------------------------------------------------- annexures


def test_each_annexure_says_whether_this_row_carries_it():
    """A passage from the reporting format must not make the framework look read."""
    circular = make_circular("c-1", content_text="Details are at Annexures A and B.")
    circular.attachments = [
        _attachment(circular, "a-frame", "Annex-A-Framework.pdf", "framework " * 300),
        _attachment(circular, "a-format", "Annex-B-Format.pdf", "format " * 50),
    ]
    result = _result(circular, passages=[
        _passage("Reporting format row 1", "a-format", "Annex-B-Format.pdf"),
    ])

    card = _card(result)

    by_file = {entry["citation"]: entry for entry in card["annexures"]}
    assert by_file["[[attachment:a-format|Annex-B-Format.pdf]]"]["in_this_result"] == "passages"
    framework = by_file["[[attachment:a-frame|Annex-A-Framework.pdf]]"]
    assert framework["in_this_result"] == "no"
    assert framework["chars"] == len(("framework " * 300).strip())
    # What the row carries is listed first, so a cap can never hide it.
    assert card["annexures"][0]["in_this_result"] == "passages"


def test_an_attachment_hit_with_only_an_excerpt_says_so():
    circular = make_circular("c-1", content_text="x" * 5_000)  # too long to inline
    circular.attachments = [_attachment(circular, "a-1", "Annex.pdf")]

    card = _card(_result(circular, attachment_id="a-1", attachment_filename="Annex.pdf"))

    assert card["matching_passage_excerpt"]
    assert card["annexures"][0]["in_this_result"] == "excerpt"


def test_an_annexure_read_earlier_in_the_turn_is_not_announced_as_unread():
    """`read_attachment` wrote the ledger; the first search row after it must say so."""
    circular = make_circular("c-1", content_text="See Annex.")
    circular.attachments = [_attachment(circular, "a-1", "Annex.pdf")]
    read = _passage("Paragraph 4.11 defines stable deposits.", "a-1", "Annex.pdf", 42)
    sent_passages = {circular.id: {_passage_ledger_key(circular.id, read)}}

    card = _card(_result(circular, passages=[read]), sent={}, sent_passages=sent_passages)

    assert "matching_passages" not in card
    assert card["annexures"][0]["in_this_result"] == "provided_earlier"


def test_an_attachment_with_no_text_is_counted_not_offered():
    """No tool can open a scan, so a handle to one leads nowhere — but its existence
    is what lets the model say the terms are not available rather than absent."""
    circular = make_circular("c-1", content_text="Annexure attached.")
    circular.attachments = [
        _attachment(circular, "a-1", "Annex.pdf"),
        _attachment(circular, "a-2", "Scan.pdf", text=None),
    ]

    card = _card(_result(circular))

    assert [entry["citation"] for entry in card["annexures"]] == [
        "[[attachment:a-1|Annex.pdf]]"
    ]
    assert card["annexures_without_text"] == 1


def test_a_long_schedule_of_annexures_is_capped_and_counted():
    circular = make_circular("c-1", content_text="Forms attached.")
    circular.attachments = [
        _attachment(circular, f"a-{i:02}", f"Form-{i:02}.pdf")
        for i in range(MAX_CARD_ANNEXURES + 4)
    ]

    card = _card(_result(circular))

    assert len(card["annexures"]) == MAX_CARD_ANNEXURES
    assert card["annexures_not_listed"] == 4


def test_a_repeat_row_does_not_repeat_the_annexures():
    circular = make_circular("c-1", content_text="See Annex.")
    circular.attachments = [_attachment(circular, "a-1", "Annex.pdf")]
    sent: dict = {}

    first = _card(_result(circular, lexical_rank=1), sent=sent)
    second = _card(_result(circular, semantic_rank=1), sent=sent)

    assert first["annexures"]
    assert second["duplicate_of_earlier_entry"] is True
    assert "annexures" not in second


# -------------------------------------------------------------------- references


def test_the_card_names_the_acts_the_circular_references():
    circular = make_circular("c-1")
    aml_act = _law("aml-act", "Anti-Money Laundering Act, 2010")
    circular.reg_links = [_link(aml_act)]

    card = _card(_result(circular))

    assert card["references_laws"] == [{
        "citation": "[[law:aml-act|Anti-Money Laundering Act, 2010]]",
        "title": "Anti-Money Laundering Act, 2010",
    }]


def test_a_listing_edge_and_a_delisted_document_are_not_references():
    """`listing` means the circular *is* the laws page's row; a delisted document is
    one `get_law_details` would not open."""
    circular = make_circular("c-1")
    circular.reg_links = [
        _link(_law("self", "Circular as listed"), link_type="listing"),
        _link(_law("gone", "Repealed Rules, 1990", delisted=True)),
    ]

    assert "references_laws" not in _card(_result(circular))


def test_a_typed_edge_outranks_a_name_match_and_carries_its_type():
    circular = make_circular("c-1")
    named = _law("ordinance", "Banking Companies Ordinance, 1962")
    amended = _law("regs", "AML/CFT/CPF Regulations")
    circular.reg_links = [
        _link(named),
        _link(amended, link_type="amends", confidence=0.9),
        _link(amended),  # the same document by name as well: listed once, typed
    ]

    card = _card(_result(circular))

    assert card["references_laws"] == [
        {"citation": "[[law:regs|AML/CFT/CPF Regulations]]",
         "title": "AML/CFT/CPF Regulations", "link": "amends"},
        {"citation": "[[law:ordinance|Banking Companies Ordinance, 1962]]",
         "title": "Banking Companies Ordinance, 1962"},
    ]


def test_references_are_capped_and_counted():
    circular = make_circular("c-1")
    circular.reg_links = [
        _link(_law(f"law-{i}", f"Act {i}")) for i in range(MAX_CARD_REFERENCED_LAWS + 2)
    ]

    card = _card(_result(circular))

    assert len(card["references_laws"]) == MAX_CARD_REFERENCED_LAWS
    assert card["references_laws_not_listed"] == 2


# ---------------------------------------------------------------------------- law


def test_a_law_card_leads_with_its_citation_and_drops_the_url():
    document = _law("sbp-act", "State Bank of Pakistan Act, 1956")
    document.source_url = "https://www.sbp.org.pk/sbp-act.pdf"
    version = RegDocumentVersion(
        id="v1", document_id=document.id, content_text="x" * 9_000, is_current=1,
        file_url="https://www.sbp.org.pk/sbp-act.pdf",
    )

    card, = AIClient._law_search_payloads([{
        "law": document, "version": version, "snippet": "",
        "passages": [{"text": "Section 9D. The Monetary Policy Committee…"}],
    }])

    assert next(iter(card)) == "citation"
    assert "source_url" not in card
    assert card["full_text_chars"] == 9_000
