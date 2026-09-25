"""A handle passed back as a tool argument resolves to the document it names.

The model is shown ``[[c:BPRD-CL-01-2021]]`` and cites with it, so it also hands it back.
In the 2026-09-26 benchmark round (P02) it called
``get_circular_details("BPRD-CL-01-2021")``: the reference parser could not read the slug,
full-text search took the nearest title, and the tool returned BPRD Circular Letter No. 24
of 2006 as though it had been asked for it. The model noticed and spent a round re-asking.

Three properties fix it, and are pinned here:

- a handle minted this turn is an exact address, resolved before anything is parsed;
- a circular slug the turn's map does not hold is matched by *applying* `slugify` to the
  candidates, so it is exact too — never a neighbour;
- when resolution does fall through to full-text search, the result says it is the
  closest match rather than passing itself off as the one named.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye import search as search_module
from sbpeye.ai import AIClient, AIConfig
from sbpeye.citation_handles import CitationHandles
from sbpeye.models import Attachment, Base, Circular


def _circular(circular_id, reference, title):
    return Circular(
        id=circular_id, reference=reference, title=title, department="BPRD",
        date=datetime(2021, 1, 1), url=f"https://www.sbp.org.pk/{circular_id}.htm",
        content_text="Body.",
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    target = _circular("cl-01-2021", "BPRD Circular Letter No. 01 of 2021",
                       "Amendments in AML/CFT/CPF Regulations")
    target.attachments = [Attachment(
        id="att-1", circular_id="cl-01-2021", filename="CL1-Annex.pdf",
        original_url="https://www.sbp.org.pk/CL1-Annex.pdf", content_text="Annex text.",
        extraction_status="extracted",
    )]
    session.add_all([
        target,
        # The circular P02 was actually handed: a title that full-text search liked.
        _circular("cl-24-2006", "BPRD Circular Letter No. 24 of 2006",
                  "Consolidation of Licensing, Corporate Governance & Regulations at BPRD"),
        _circular("shsfd-04-2026", "SH&SFD Circular No.04 of 2026 of 2026",
                  "Housing finance amendments"),
    ])
    session.commit()
    return session


def _turn(*tokens):
    handles = CitationHandles()
    handles.to_handles(" ".join(tokens))
    return handles


# ------------------------------------------------------------------ the handle map


@pytest.mark.parametrize("argument", [
    "[[c:BPRD-CL-01-2021]]", "c:BPRD-CL-01-2021", "BPRD-CL-01-2021", "bprd-cl-01-2021",
])
def test_every_form_of_a_handle_the_model_writes_is_found(argument):
    handles = _turn("[[circular:cl-01-2021|BPRD Circular Letter No. 01 of 2021]]")

    assert handles.lookup(argument) == (
        "circular", "cl-01-2021", "BPRD Circular Letter No. 01 of 2021"
    )


def test_a_written_reference_is_not_a_handle_and_is_not_a_drop():
    handles = _turn("[[circular:cl-01-2021|BPRD Circular Letter No. 01 of 2021]]")

    assert handles.lookup("BPRD Circular Letter No. 01 of 2021") is None
    assert handles.lookup("BPRD-CL-07-2019") is None
    # `dropped` counts citations a reader lost; a tool argument is not one.
    assert handles.dropped == []


def test_a_handle_from_this_turn_resolves_exactly(db):
    handles = _turn("[[circular:cl-01-2021|BPRD Circular Letter No. 01 of 2021]]")

    circular, error, note = AIClient._resolve_circular("[[c:BPRD-CL-01-2021]]", db, handles)

    assert (circular.id, error, note) == ("cl-01-2021", None, None)


def test_an_attachment_handle_resolves_to_its_circular(db):
    handles = _turn("[[attachment:att-1|CL1-Annex.pdf]]")

    circular, error, _ = AIClient._resolve_circular("[[a:CL1-Annex]]", db, handles)

    assert error is None and circular.id == "cl-01-2021"


def test_a_law_handle_is_refused_with_the_tool_that_reads_it(db):
    handles = _turn("[[law:aml-act|Anti-Money Laundering Act, 2010]]")

    circular, error, _ = AIClient._resolve_circular(
        "[[l:Anti-Money-Laundering-Act-2010]]", db, handles
    )

    assert circular is None
    assert "get_law_details" in error["error"]


# ---------------------------------------------------------- slugs outside the map


def test_the_p02_slug_resolves_without_a_turn_map(db):
    """History replay, or a handle typed from memory: no map, and still exact."""
    circular, error, note = AIClient._resolve_circular("BPRD-CL-01-2021", db)

    assert (circular.id, error, note) == ("cl-01-2021", None, None)


def test_a_slug_is_matched_by_slugify_so_ampersands_and_doubled_years_survive(db):
    """`slugify` drops the '&' in SH&SFD and keeps SBP's duplicated year; parsing the
    slug back could not recover either, applying `slugify` to the candidates does."""
    circular, error, _ = AIClient._resolve_circular("SHSFD-C-04-2026-2026", db)

    assert error is None and circular.id == "shsfd-04-2026"


# ------------------------------------------------------------ the fuzzy fallback


def test_a_closest_match_by_search_says_that_is_what_it_is(db, monkeypatch):
    nearest = db.get(Circular, "cl-24-2006")
    monkeypatch.setattr(
        search_module.search_engine, "search",
        lambda *args, **kwargs: ([{"circular": nearest}], 1),
    )

    circular, error, note = AIClient._resolve_circular("BPRD-CL-99-2021", db)

    assert error is None and circular.id == "cl-24-2006"
    assert "closest match" in note
    assert "BPRD Circular Letter No. 24 of 2006" in note


def test_an_exact_reference_carries_no_note(db):
    circular, error, note = AIClient._resolve_circular(
        "BPRD Circular Letter No. 01 of 2021", db
    )

    assert (circular.id, error, note) == ("cl-01-2021", None, None)


# ------------------------------------------------------------------------ wiring


@pytest.mark.parametrize("tool", ["get_circular_details", "read_attachment"])
def test_both_circular_tools_resolve_through_the_turns_map(db, tool):
    """A law handle is refused before any retrieval runs, so the refusal proves the
    tool consulted the map the chat loop set — without needing a vector store."""
    client = AIClient(AIConfig(provider="openai", api_key="test", model="test"))
    client._turn_handles = _turn("[[law:aml-act|Anti-Money Laundering Act, 2010]]")

    result = json.loads(client._execute_tool(
        tool, {"circular_reference": "[[l:Anti-Money-Laundering-Act-2010]]"}, db,
    ))

    assert "get_law_details" in result["error"]
