"""Warn-only checks on a finished answer — `docs/CHAT_REDESIGN.md` R6, checks 2 and 4.

Grounding: every cited document's text reached the model this turn — not merely its
name, in an `amended_by` entry, a `references_laws` line, an annexure listing or an
earlier answer. Supersession: nothing cited is withdrawn, or changed by a later
`amends` edge, without the answer saying so. Neither alters the answer.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sbpeye import ai as ai_module
from sbpeye.ai import AIClient, AIConfig
from sbpeye.answer_checks import check_answer
from sbpeye.models import Attachment, Base, Circular, CircularRelationship


def _circular(circular_id, reference, year, status="active"):
    return Circular(
        id=circular_id, reference=reference, title=f"Title {circular_id}",
        department="BPRD", date=datetime(year, 1, 1), status=status,
        url=f"https://www.sbp.org.pk/{circular_id}.htm", content_text="Body.",
    )


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    base = _circular("base", "BPRD Circular No. 07 of 2019", 2019, status="amended")
    base.attachments = [Attachment(
        id="annex", circular_id="base", filename="C7-Annex.pdf",
        original_url="https://www.sbp.org.pk/C7-Annex.pdf", content_text="Annex.",
        extraction_status="extracted",
    )]
    session.add_all([
        base,
        _circular("amender", "BPRD Circular No. 03 of 2021", 2021),
        _circular("old", "BPRD Circular No. 02 of 2010", 2010, status="superseded"),
        _circular("new", "BPRD Circular No. 11 of 2015", 2015),
        _circular("added", "BPRD Circular No. 01 of 2012", 2012, status="amended"),
        _circular("adder", "BPRD Circular No. 04 of 2013", 2013),
        CircularRelationship(source_id="amender", target_id="base", type="amends"),
        CircularRelationship(source_id="new", target_id="old", type="supersedes"),
        CircularRelationship(source_id="adder", target_id="added", type="adds_to"),
    ])
    session.commit()
    return session


def token(circular_id, label="x", kind="circular"):
    return f"[[{kind}:{circular_id}|{label}]]"


def check(db, answer, *, text=None, passages=None, listed=(), selected=()):
    # Every circular counts as read unless a test says otherwise, so each test isolates
    # the one check it is about.
    everything = {c: ["full_circular_text"] for c in
                  ("base", "amender", "old", "new", "added", "adder")}
    return check_answer(
        answer, db,
        sent_text_keys=everything if text is None else text,
        sent_passages=passages or {},
        listed_documents=set(listed),
        selected_circular_ids=selected,
    )


def kinds(warnings):
    return [(item["check"], item["severity"]) for item in warnings]


# ---------------------------------------------------------------------- supersession


def test_a_withdrawn_circular_cited_as_current_is_flagged(db):
    warnings = check(db, f"The limit is PKR 1m under {token('old')}.")

    assert kinds(warnings) == [("supersession", "high")]
    assert warnings[0]["related"] == ["[[circular:new|BPRD Circular No. 11 of 2015]]"]


@pytest.mark.parametrize("answer", [
    f"{token('old')} set PKR 1m, but {token('new')} replaced it.",
    f"{token('old')} set PKR 1m; see BPRD Circular No. 11 of 2015 for the current rule.",
    f"Under {token('old')}, since superseded, the limit was PKR 1m.",
])
def test_a_withdrawal_the_answer_states_is_not_flagged(db, answer):
    assert check(db, answer) == []


def test_withdrawal_words_count_only_near_the_citation(db):
    answer = f"Under {token('old')} the limit is PKR 1m.\n\nOther rules were superseded."

    assert kinds(check(db, answer)) == [("supersession", "high")]


def test_an_amended_circular_without_its_amender_is_flagged(db):
    warnings = check(db, f"The threshold is 10% under {token('base')}.")

    assert kinds(warnings) == [("supersession", "medium")]
    assert warnings[0]["related"] == ["[[circular:amender|BPRD Circular No. 03 of 2021]]"]


def test_naming_the_amender_satisfies_the_check(db):
    assert check(db, f"{token('base')}, as amended by {token('amender')}.") == []


def test_an_adds_to_edge_is_not_an_amendment_to_warn_about(db):
    """`adds_to` and `clarifies` leave the base text standing — most "amended"
    circulars had nothing changed at all (C11), so flagging them would be noise."""
    assert check(db, f"See {token('added')}.") == []


# ------------------------------------------------------------------------- grounding


def test_a_circular_whose_text_was_sent_is_grounded(db):
    assert check(db, f"See {token('new')}.", text={"new": ["matching_passages"]}) == []


def test_a_circular_named_only_as_a_pointer_is_flagged(db):
    """Excerpt-only rows are recorded with no text keys, so only a document that never
    had a row of its own — an amender, a withdrawn match — is ungrounded."""
    warnings = check(db, f"See {token('new')}.", text={})

    assert kinds(warnings) == [("grounding", "low")]


def test_passages_ledger_and_selection_both_ground_a_circular(db):
    assert check(db, f"See {token('new')}.", text={}, passages={"new": {"new__chunk_0"}}) == []
    assert check(db, f"See {token('new')}.", text={}, selected=["new"]) == []


def test_a_law_is_grounded_by_its_passages_not_by_a_reference_line(db):
    law = token("aml-regs", "AML/CFT/CPF Regulations", kind="law")

    assert check(db, law, text={}, passages={"aml-regs": {"v1__chunk_4"}}) == []
    assert kinds(check(db, law, text={})) == [("grounding", "low")]


def test_an_annexure_is_grounded_only_by_its_own_chunks(db):
    annex = token("annex", "C7-Annex.pdf", kind="attachment")

    read = check(db, annex, text={}, passages={"base": {"annex__chunk_3"}})
    listed_only = check(db, annex, text={}, passages={"base": {"base__chunk_0"}})

    assert read == []
    assert kinds(listed_only) == [("grounding", "low")]


def test_a_listing_tools_rows_are_grounded(db):
    assert check(db, f"See {token('new')}.", text={}, listed={("circular", "new")}) == []


def test_high_severity_comes_first(db):
    warnings = check(db, f"{token('adder')} and {token('old')}.", text={"old": ["x"]})

    assert kinds(warnings) == [("supersession", "high"), ("grounding", "low")]


# --------------------------------------------------------------------- the client


def test_the_client_checks_with_the_ledgers_the_turn_kept(db, monkeypatch):
    client = AIClient(AIConfig(provider="openai", api_key="t", model="t"))
    events = []
    monkeypatch.setattr(ai_module, "emit_event", lambda kind, payload, **k: events.append((kind, payload)))
    client._note_round_result(
        "get_latest_circulars", json.dumps({"results": [{"citation": token("adder")}]}),
    )

    warnings = client.verify_answer(f"See {token('adder')} and {token('old')}.", db)

    assert kinds(warnings) == [("supersession", "high"), ("grounding", "low")]
    # `adder` is grounded by the listing, so only `old` is ungrounded.
    assert warnings[1]["citation"] == token("old")
    assert events[-1][0] == "verification"
    assert (events[-1][1]["grounding"], events[-1][1]["supersession"]) == (1, 1)


def test_a_check_that_fails_reports_nothing_rather_than_breaking_the_answer(db, monkeypatch):
    client = AIClient(AIConfig(provider="openai", api_key="t", model="t"))
    monkeypatch.setattr(ai_module, "emit_event", lambda *a, **k: None)

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ai_module, "check_answer", broken)

    assert client.verify_answer(f"See {token('old')}.", db) == []


# ------------------------------------------------------------------------ the route


def test_warnings_reach_admins_only_while_they_are_being_measured():
    from types import SimpleNamespace

    from sbpeye.main import _message_verification
    from sbpeye.models import ChatMessage

    warning = {"check": "supersession", "severity": "high", "citation": token("old")}
    stored = ChatMessage(id="m", session_id="s", role="assistant", content="a",
                         verification_json=json.dumps([warning]))
    clean = ChatMessage(id="n", session_id="s", role="assistant", content="a")

    assert _message_verification(stored, SimpleNamespace(is_admin=True)) == {
        "verification": [warning]
    }
    assert _message_verification(stored, SimpleNamespace(is_admin=False)) == {}
    assert _message_verification(clean, SimpleNamespace(is_admin=True)) == {}


# ------------------------------------------------------------ refinements from replay


def test_an_amender_named_as_lineage_is_not_unread(db):
    """2026-08-26 P12: "the PRs have since been amended by X", X never read, and the
    answer said it could not see X's provisions. A graph fact, correctly disclosed."""
    answer = f"{token('base')}, since amended by {token('amender')}; I could not read it."

    assert check(db, answer, text={"base": ["full_circular_text"]}) == []


def test_an_amended_circular_cited_as_the_earlier_rule_is_not_flagged(db):
    """2026-09-26 P12: the old limits "were set by X and have been superseded by the 2025
    PRs" — the answer is not presenting X as current."""
    answer = f"The previous limits were set by {token('base')} and have since been superseded."

    assert check(db, answer) == []


def test_lineage_learned_from_a_circular_read_but_not_cited_counts(db):
    """In 2026-08-26 P12 the circular whose `amended_by` line named the amender was read,
    and the answer cited only the amender."""
    answer = f"Those limits have since been amended by {token('amender')}."

    assert check(db, answer, text={"base": ["full_circular_text"]}) == []


def test_a_check_that_raises_in_the_route_costs_the_reader_nothing(db):
    from sbpeye.main import _verify_turn

    class Broken:
        def verify_answer(self, *args, **kwargs):
            raise RuntimeError("boom")

    assert _verify_turn(Broken(), "An answer.", db, []) == []
    assert _verify_turn(object(), "An answer.", db, []) == []
