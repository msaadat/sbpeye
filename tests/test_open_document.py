"""R5a: four tools where there were nine, and nothing the nine could reach lost.

`CHAT_REDESIGN.md` §6. The circular, attachment and law readers became `open_document`;
the recency, tag and inventory listings became `list_documents`; the selection search
became `search_corpus`'s `scope`; `query_regulatory_values` is offered only when the
table behind it has coverage. The readers themselves did not change, and are pinned by
their own suites (`test_attachment_reach`, `test_law_chat_reach`, `test_repeat_reads`,
`test_document_ledger`). What is pinned here is the part that is new: which reader a
request reaches, what a listing row carries, and when the values tool is offered.

The routing property that matters most is the one the split existed to paper over: a
request that names an Act reaches the Act, even when a circular's title contains the
Act's name — `get_circular_details("State Bank of Pakistan Act, 1956")` once answered
with a 1999 cash-reserve circular.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye import search as search_module
from sbpeye.ai import (
    TOOLS,
    VALUES_TOOL_MIN_COVERAGE,
    AIClient,
    AIConfig,
    tools_for_turn,
    values_coverage,
)
from sbpeye.answer_checks import LISTING_TOOLS
from sbpeye.chat_steps import build_step
from sbpeye.citation_handles import CitationHandles
from sbpeye.models import (
    Attachment,
    Base,
    Circular,
    CircularEntity,
    RegDocument,
    RegDocumentVersion,
)


def _circular(circular_id, reference, title, date, department="BPRD", tags=None):
    return Circular(
        id=circular_id, reference=reference, title=title, department=department,
        date=date, url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text="Body.", summary="A summary nobody reads.",
        tags=json.dumps(tags or []),
    )


def _law(document_id, title, circular_id=None):
    document = RegDocument(
        id=document_id, title=title, normalized_title=title.casefold(), doc_type="law",
        circular_id=circular_id,
        first_seen_at=datetime(2026, 8, 1), last_seen_at=datetime(2026, 8, 1),
    )
    version = RegDocumentVersion(
        id=f"{document_id}-v1", document_id=document_id, content_hash=f"hash-{document_id}",
        file_type="pdf", content_text=f"The text of {title}.", is_current=1,
        first_seen_at=datetime(2026, 8, 1), last_seen_at=datetime(2026, 8, 1),
    )
    return document, version


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    lcr = _circular("bprd-08-2016", "BPRD Circular No. 08 of 2016",
                    "Basel III Liquidity Standards", datetime(2016, 6, 23), tags=["Liquidity"])
    lcr.attachments = [Attachment(
        id="att-8", circular_id="bprd-08-2016", filename="C8-Annex.pdf",
        original_url="https://www.sbp.org.pk/C8-Annex.pdf", content_text="Annex text.",
        extraction_status="extracted",
    )]
    session.add_all([
        lcr,
        # The trap: a circular whose title contains the Act's name.
        _circular("bprd-03-1999", "BPRD Circular No. 03 of 1999",
                  "Cash Reserve Requirement under the State Bank of Pakistan Act, 1956",
                  datetime(1999, 3, 1), tags=["Cash Reserve"]),
        _circular("epd-01-2025", "EPD Circular No. 01 of 2025", "Export proceeds",
                  datetime(2025, 1, 15), department="EPD", tags=["Forex"]),
        _circular("bprd-02-2025", "BPRD Circular No. 02 of 2025", "AML amendments",
                  datetime(2025, 2, 10), tags=["AML"]),
        _circular("outsourcing-c", "BPRD Circular No. 06 of 2017",
                  "Framework for Risk Management in Outsourcing Arrangements",
                  datetime(2017, 7, 20)),
    ])
    for pair in (
        _law("sbp-act", "State Bank of Pakistan Act, 1956"),
        _law("aml-regs", "AML/CFT/CPF Regulations"),
        # A listing row that is really a circular: no text of its own.
        _law("outsourcing-l", "Framework for Risk Management in Outsourcing Arrangements",
             circular_id="outsourcing-c"),
    ):
        session.add_all(pair)
    session.commit()
    return session


@pytest.fixture
def client():
    """A client whose three readers report what they were handed instead of reading.

    Retrieval is not what is under test, and needs a vector store; which reader a request
    reaches, with which document and which note, is.
    """
    client = AIClient(AIConfig(provider="openai", api_key="test", model="test"))
    client._turn_handles = CitationHandles()
    client._read_circular = lambda circular, note, arguments, db, user_query: json.dumps(
        {"reader": "circular", "id": circular.id, "note": note}
    )
    client._read_attachment = lambda circular, note, arguments: json.dumps({
        "reader": "attachment", "id": circular.id, "note": note,
        "attachment": arguments.get("attachment"), "page": arguments.get("page"),
        "section": arguments.get("section"),
    })
    client._read_law = lambda document, requested, note, arguments, db: json.dumps(
        {"reader": "law", "id": document.id, "note": note}
    )
    return client


def _open(client, db, **arguments):
    return json.loads(client._execute_tool("open_document", arguments, db))


# ------------------------------------------------------------------ by handle


def test_a_law_handle_opens_the_law(client, db):
    handle = client._turn_handles.mint("law", "sbp-act", "State Bank of Pakistan Act, 1956")

    assert _open(client, db, document=handle) == {"reader": "law", "id": "sbp-act", "note": None}


def test_an_attachment_handle_opens_that_attachment(client, db):
    handle = client._turn_handles.mint("attachment", "att-8", "C8-Annex.pdf")

    result = _open(client, db, document=handle, page=15)

    assert (result["reader"], result["id"], result["attachment"], result["page"]) == (
        "attachment", "bprd-08-2016", "att-8", 15,
    )


def test_a_circular_handle_opens_the_circular(client, db):
    handle = client._turn_handles.mint("circular", "bprd-08-2016", "BPRD Circular No. 08 of 2016")

    assert _open(client, db, document=handle, query="run-off")["reader"] == "circular"


@pytest.mark.parametrize("inside", [{"page": 15}, {"section": "4.11"}, {"attachment": "C8-Annex.pdf"}])
def test_a_circular_with_a_page_section_or_attachment_reads_inside_it(client, db, inside):
    """`read_attachment` took a circular plus a locator; `open_document` must too."""
    handle = client._turn_handles.mint("circular", "bprd-08-2016", "BPRD Circular No. 08 of 2016")

    result = _open(client, db, document=handle, **inside)

    assert (result["reader"], result["id"]) == ("attachment", "bprd-08-2016")


def test_a_law_slug_the_turn_did_not_mint_still_resolves_exactly(client, db):
    """A handle from replayed history: matched by applying `slugify`, as circular slugs are."""
    result = _open(client, db, document="[[l:State-Bank-of-Pakistan-Act-1956]]")

    assert result == {"reader": "law", "id": "sbp-act", "note": None}


def test_an_unknown_attachment_handle_is_refused_with_what_to_do_instead(client, db):
    result = _open(client, db, document="[[a:Some-Annex]]")

    assert "attachment" in result["error"] and "`document`" in result["error"]


# ---------------------------------------------------------------- by free text


def test_a_circular_reference_opens_the_circular(client, db):
    result = _open(client, db, document="BPRD Circular No. 08 of 2016")

    assert (result["reader"], result["id"], result["note"]) == ("circular", "bprd-08-2016", None)


def test_an_act_by_name_opens_the_act_not_a_circular_named_after_it(client, db):
    """The failure the old routing prose existed to prevent, now prevented by order."""
    result = _open(client, db, document="State Bank of Pakistan Act, 1956")

    assert (result["reader"], result["id"], result["note"]) == ("law", "sbp-act", None)


def test_a_law_listing_row_that_is_a_circular_opens_the_circular(client, db):
    """`RegDocument.circular_id` rows store no content; the law reader would find none."""
    handle = client._turn_handles.mint(
        "law", "outsourcing-l", "Framework for Risk Management in Outsourcing Arrangements"
    )

    for document in (handle, "Framework for Risk Management in Outsourcing Arrangements"):
        result = _open(client, db, document=document)
        assert (result["reader"], result["id"]) == ("circular", "outsourcing-c")


def test_an_act_the_corpus_lacks_says_so_rather_than_passing_off_a_neighbour(
    client, db, monkeypatch,
):
    """P01/P02 on 2026-09-26: the AML Act, 2010 is not held; the Regulations are."""
    regulations = db.get(RegDocument, "aml-regs")
    monkeypatch.setattr(
        search_module.search_engine, "search",
        lambda query, db, limit=10, source="circulars", **_: (
            ([{"law": regulations}], None) if source == "laws" else ([], None)
        ),
    )

    result = _open(client, db, document="Anti-Money Laundering Act, 2010")

    assert (result["reader"], result["id"]) == ("law", "aml-regs")
    assert "does not hold" in result["note"]


def test_the_old_argument_names_are_still_understood(client, db):
    assert _open(client, db, circular_reference="BPRD Circular No. 08 of 2016")["reader"] == "circular"
    assert _open(client, db, law_title="State Bank of Pakistan Act, 1956")["reader"] == "law"


def test_naming_nothing_is_an_error(client, db):
    assert "error" in _open(client, db, document="  ")


# ---------------------------------------------------------------- list_documents


def _list(client, db, **arguments):
    return json.loads(client._execute_tool("list_documents", arguments, db))


def test_the_date_listing_is_newest_first_and_carries_pointers_only(client, db):
    result = _list(client, db, limit=3)

    assert [row["reference"] for row in result["results"]] == [
        "BPRD Circular No. 02 of 2025", "EPD Circular No. 01 of 2025",
        "BPRD Circular No. 06 of 2017",
    ]
    # R1's card discipline: `summary`, `url` and `tags` are not on a row.
    assert set(result["results"][0]) == {
        "citation", "title", "reference", "department", "date", "status",
    }


@pytest.mark.parametrize("filters,expected", [
    ({"department": "EPD"}, ["epd-01-2025"]),
    ({"tag": "AML"}, ["bprd-02-2025"]),
    ({"start_year": 2016, "end_year": 2017}, ["outsourcing-c", "bprd-08-2016"]),
    ({"department": "BPRD", "start_year": 2025}, ["bprd-02-2025"]),
])
def test_the_date_listing_takes_department_tag_and_years_together(client, db, filters, expected):
    result = _list(client, db, **filters)

    assert [row["citation"].split("|")[0].removeprefix("[[circular:") for row in result["results"]] == expected


def test_a_query_is_an_inventory_sweep(client, db):
    seen = {}
    client._inventory_tool = lambda arguments, db: seen.update(arguments) or json.dumps({"results": []})

    _list(client, db, query="call centres", sources="laws")

    assert seen == {"query": "call centres", "sources": "laws"}


@pytest.mark.parametrize("arguments", [
    {"query": "AML", "tag": "AML"},
    {"sources": "laws"},
    {"start_year": "last year"},
])
def test_a_combination_it_cannot_serve_is_an_error_not_a_silent_drop(client, db, arguments):
    assert "error" in _list(client, db, **arguments)


@pytest.mark.parametrize("legacy,arguments,expected", [
    ("get_latest_circulars", {"department": "EPD"}, ["epd-01-2025"]),
    ("get_circulars_by_tag", {"tag": "AML"}, ["bprd-02-2025"]),
])
def test_the_old_listing_names_are_served_by_the_new_listing(client, db, legacy, arguments, expected):
    result = json.loads(client._execute_tool(legacy, arguments, db))

    assert [row["citation"].split("|")[0].removeprefix("[[circular:") for row in result["results"]] == expected


def test_a_listing_row_counts_as_shown_for_the_grounding_check(client, db):
    assert "list_documents" in LISTING_TOOLS
    result = client._execute_tool("list_documents", {"tag": "AML"}, db)
    client._note_round_result("list_documents", result)

    assert ("circular", "bprd-02-2025") in client._listed_documents


# ------------------------------------------------------------------ the schema


def _names(tools):
    return [tool["function"]["name"] for tool in tools]


def test_four_tools_in_the_full_schema():
    assert _names(TOOLS) == [
        "search_corpus", "open_document", "list_documents", "query_regulatory_values",
    ]


def test_the_values_tool_is_withheld_below_its_coverage_floor(db):
    """57 values from 7 of 3,655 circulars answers most quantitative questions with nothing."""
    assert values_coverage(db) == 0.0
    assert "query_regulatory_values" not in _names(tools_for_turn(None, db))
    # No database, nothing to measure: withheld rather than offered on trust.
    assert "query_regulatory_values" not in _names(tools_for_turn(None))


def test_the_values_tool_is_offered_once_extraction_covers_the_corpus(db):
    circulars = db.query(Circular).all()
    covered = circulars[: max(1, round(len(circulars) * VALUES_TOOL_MIN_COVERAGE))]
    db.add_all([
        CircularEntity(circular_id=c.id, entity_type="ratio", metric="CAR", value_numeric=11.5)
        for c in covered
    ])
    db.commit()

    assert values_coverage(db) >= VALUES_TOOL_MIN_COVERAGE
    assert "query_regulatory_values" in _names(tools_for_turn(None, db))


# ------------------------------------------------------------ research steps


def test_an_open_document_step_is_digested_by_what_it_read():
    law = build_step("open_document", {"document": "[[l:SBP-Act]]"}, json.dumps({
        "requested": "[[l:SBP-Act]]", "resolved_title": "State Bank of Pakistan Act, 1956",
        "citation": "[[law:sbp-act|State Bank of Pakistan Act, 1956]]", "passage_count": 2,
        "passages": [{"passage": "(1) ..."}, {"passage": "(2) ..."}],
    }), label="Reading the document")
    annex = build_step("open_document", {"document": "[[a:C8-Annex]]"}, json.dumps({
        "circular": "BPRD Circular No. 08 of 2016", "filename": "C8-Annex.pdf",
        "attachment_citation": "[[attachment:att-8|C8-Annex.pdf]]", "passage_count": 1,
        "passages": [{"page": 15, "passage": "Stable deposits are ..."}],
    }), label="Reading the document")
    circular = build_step("open_document", {"document": "[[c:BPRD-C-08-2016]]"}, json.dumps({
        "citation": "[[circular:bprd-08-2016|BPRD Circular No. 08 of 2016]]",
        "title": "Basel III Liquidity Standards", "document_context": "...",
        "attachment_citations": ["[[attachment:att-8|C8-Annex.pdf]]"],
    }), label="Reading the document")

    assert law["summary"] == "2 passages from State Bank of Pakistan Act, 1956"
    assert annex["summary"] == "1 passage from C8-Annex.pdf"
    assert annex["hits"][0]["note"] == "Page 15"
    assert circular["summary"] == "Basel III Liquidity Standards"


def test_a_selected_scope_search_step_counts_passages():
    step = build_step("search_corpus", {"query": "x", "scope": "selected"}, json.dumps({
        "results": [{"citation": "[[circular:a|A]]", "title": "A", "passage": "..."}],
        "count": 1,
    }), label="Searching circulars and laws")

    assert step["summary"] == "1 passage"


def test_an_inventory_listing_step_says_when_it_was_cut_short():
    step = build_step("list_documents", {"query": "AML"}, json.dumps({
        "search_terms": ["AML"], "documents_matched": 12, "documents_returned": 10,
        "complete": False, "results": [
            {"citation": f"[[circular:{n}|C{n}]]", "title": f"C{n}", "passage": "..."}
            for n in range(10)
        ],
    }), label="Listing documents")

    assert step["summary"] == "10 of 12 matching documents"
    assert step["incomplete"] is True


def test_a_part_named_by_its_chapter_is_a_law_not_a_circular_reference(client, db):
    """Found on the real corpus: the reference parser reads any word and a number as a
    reference, so "Foreign Exchange Manual Chapter 12" parsed as "Chapter 12" and came
    back an ambiguous circular. `get_law_details` had always resolved it to the chapter."""
    manual, manual_version = _law("fe-manual", "Foreign Exchange Manual")
    chapter, chapter_version = _law("fe-manual-12", "EXPORTS")
    chapter.parent_id, chapter.part_label, chapter.part_order = "fe-manual", "Chapter 12", 12
    db.add_all([
        manual, manual_version, chapter, chapter_version,
        # Something for the parser to find, as the real corpus had.
        _circular("chapter-12", "FE Circular No. 12 of 2019", "Amendments in Chapter 12",
                  datetime(2019, 5, 1)),
    ])
    db.commit()

    result = _open(client, db, document="Foreign Exchange Manual Chapter 12")

    assert (result["reader"], result["id"]) == ("law", "fe-manual-12")


# ------------------------------------------------ fixes from the 2026-09-27 R5a round


def _capture_search(monkeypatch):
    """Record what `search_corpus` hands the engine, and return nothing."""
    calls = []

    def dual_arm_search(query, db, **kwargs):
        calls.append(("dual_arm", kwargs))
        return {"reference_matches": [], "lexical_results": [], "semantic_results": [],
                "law_results": [], "withdrawn_matches": []}

    def search(query, db, **kwargs):
        calls.append(("search", kwargs))
        return [], 0

    monkeypatch.setattr(search_module.search_engine, "dual_arm_search", dual_arm_search)
    monkeypatch.setattr(search_module.search_engine, "search", search)
    return calls


@pytest.mark.parametrize("query,path", [
    ("auto financing tenure", "dual_arm"),
    ("latest auto financing circulars", "search"),
])
def test_search_passes_the_year_bounds_it_was_given(client, db, monkeypatch, query, path):
    """P13 and P19 passed `start_year` to `search_corpus`, which dropped it without a word."""
    calls = _capture_search(monkeypatch)

    result = json.loads(client._execute_tool(
        "search_corpus", {"query": query, "start_year": 2022, "end_year": "2024"}, db,
    ))

    kind, kwargs = calls[0]
    assert kind == path
    assert (kwargs["start_year"], kwargs["end_year"]) == (2022, 2024)
    if path == "dual_arm":
        # Years are circular-only, like department and tag: laws are left out, and said so.
        assert kwargs["include_laws"] is False
        assert "year" in result["laws_excluded_by_filter"]


def test_search_without_years_still_reaches_the_laws(client, db, monkeypatch):
    calls = _capture_search(monkeypatch)

    client._execute_tool("search_corpus", {"query": "auto financing tenure"}, db)

    assert calls[0][1]["include_laws"] is True
    assert "start_year" not in calls[0][1]


def test_a_year_that_is_not_a_year_is_an_error(client, db, monkeypatch):
    _capture_search(monkeypatch)

    result = json.loads(client._execute_tool(
        "search_corpus", {"query": "x", "start_year": "last year"}, db,
    ))

    assert "start_year" in result["error"]


def test_the_schema_offers_the_years_it_now_honours():
    search = next(t for t in tools_for_turn(None) if t["function"]["name"] == "search_corpus")

    assert {"start_year", "end_year"} <= set(search["function"]["parameters"]["properties"])


def test_a_named_instrument_is_opened_by_name_not_searched_for():
    """P17: the baseline opened the AML Act by name at call 2; under R5a's first wording —
    "open a document by the citation a result gave you" — it searched three times and swept
    the inventory first, then kept searching after the not-in-corpus note, to the round limit.
    """
    client = AIClient(AIConfig(provider="openai", api_key="test", model="test"))
    prompt = client._chat_system_prompt()
    reader = next(t for t in TOOLS if t["function"]["name"] == "open_document")

    assert "open it by that name first" in prompt
    assert "searching again will not find it" in prompt
    assert "no search is needed first" in reader["function"]["description"]
