"""Retrieval knows which circulars are still the rule, and says what changed them.

Until C11 the word `status` appeared nowhere in `search.py`. `_apply_circular_filters`
filtered on year, department and tag, and that was the complete list — so a cancelled
2013 circular ranked exactly as well as the 2024 one that replaced it.

Measured over the traced turns, across 1,399 result entries handed to chat:

* **13.2%** were `superseded` or `cancelled` — text that has been withdrawn. 11.9% at
  the top three positions, so not a tail effect, and 11.7% of the *full covering letters
  inlined*, the most expensive item in the payload.
* **36.1%** were `amended`, which is a different thing entirely and must not be dropped:
  the circular is still the rule, something later modified part of it, and the base text
  is where the bulk of the requirements live. `_recompute_statuses` derives `amended`
  from `adds_to` (1,423 edges) and `clarifies` (435) as well as `amends` (838), so most
  of that third had nothing changed at all.
* Of the amended entries that resolve to the corpus, **64.8% arrived with no amender
  anywhere in the result set** — the model read a figure still printed in a circular
  still in force, with the document that changed it absent.

So: withdrawn text is **demoted, never hidden**. It leaves the ranked arms for a
`withdrawn_matches` pointer list, stays reachable by name, and says what replaced it.
Amended text stays at full rank and names what changed it.

Demotion rather than exclusion because a hard filter removes the #1 ranked hit in 12.6%
of arms, and answering "that is not in the corpus" about a document the corpus holds is a
worse failure than the one this item exists to fix. `BC & CPD Circular No. 08 of 2021` is
the case that proves it: it is superseded, and *neither ranked arm returns it at all* —
`reference_matches` is the only path by which a directly-named circular reaches the model.
A status clause applied uniformly across all three lists would answer a question about it
with five irrelevant active circulars and no mention of the one that was asked about.

See `docs/CHAT_CONTEXT_PLAN.md` C11.
"""

import types
from datetime import datetime

import pytest

import sbpeye.search as search_module
from sbpeye.ai import AIClient
from sbpeye.chat_retrieval import WITHDRAWN_QUERY_PATTERN
from sbpeye.models import CircularRelationship
from sbpeye.search import MAX_NAMED_AMENDERS, backfill_fts, search_engine

from conftest import make_circular


TOPIC = "enhanced due diligence for high risk customers"


@pytest.fixture
def db(db_factory):
    session = db_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def no_vectors(monkeypatch):
    """Neutralize the Chroma arm so the lexical arm is asserted deterministically."""
    monkeypatch.setattr(
        search_module,
        "embedding_backend",
        types.SimpleNamespace(
            embed_queries=lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no vec"))
        ),
    )


def _add(db, circular_id, *, status="active", year=2019, reference=None, title=None):
    circular = make_circular(
        circular_id,
        reference=reference or f"BPRD Circular No. 0{circular_id[-1]} of {year}",
        title=title or f"Enhanced Due Diligence requirements ({circular_id})",
        date=datetime(year, 5, 14),
        content_text=f"{TOPIC} — {circular_id} body text.",
        status=status,
    )
    db.add(circular)
    db.commit()
    backfill_fts(db, force=True)
    return circular


def _relate(db, source, target, edge_type):
    db.add(CircularRelationship(
        source_id=source.id, target_id=target.id,
        target_reference=target.reference, type=edge_type, confidence=1.0,
    ))
    db.commit()


def _arms(db, query=TOPIC, **kwargs):
    return search_engine.dual_arm_search(query, db, limit=10, include_laws=False, **kwargs)


def _ids(arm):
    return [row["circular"].id for row in arm]


# --------------------------------------------------- withdrawn is demoted, not deleted


@pytest.mark.parametrize("status", ["superseded", "cancelled"])
def test_withdrawn_circulars_leave_the_ranked_arms(db, no_vectors, status):
    _add(db, "gone-1", status=status)
    _add(db, "live-1", status="active")

    arms = _arms(db)

    assert _ids(arms["lexical_results"]) == ["live-1"]
    assert len(arms["withdrawn_matches"]) == 1


def test_a_demoted_hit_keeps_what_makes_it_findable_and_nothing_else(db, no_vectors):
    """No body, no passages, no excerpt — ~190 ch against 2,814 for a full entry.

    `title` earns its keep: it is the whole of what tells the model whether a withdrawn
    hit is worth opening.
    """
    gone = _add(db, "gone-1", status="superseded")

    pointer, = _arms(db)["withdrawn_matches"]

    assert set(pointer) == {"citation", "reference", "title", "date", "status"}
    assert pointer["status"] == "superseded"
    assert pointer["citation"] == f"[[circular:{gone.id}|{gone.display_name}]]"


def test_the_arm_does_not_refill_the_vacated_slot(db, no_vectors):
    """The slice comes before the split, and that is the whole of the saving.

    Filter-then-slice pulls the next-ranked circular up into the gap, and because the
    replacement carries a body and passages of its own the response ends up the same
    size — a hit lost for nothing. Measured on the live corpus, filter-then-slice moved
    the payload only -2.5%.
    """
    _add(db, "gone-1", status="superseded")
    for index in range(3):
        _add(db, f"live-{index}", status="active")

    arms = search_engine.dual_arm_search(TOPIC, db, limit=2, include_laws=False)

    assert len(arms["lexical_results"]) == 1     # not topped back up to 2
    assert len(arms["withdrawn_matches"]) == 1


def test_a_circular_named_outright_is_never_filtered(db, no_vectors):
    """Rule 1, and the reason it is load-bearing rather than a courtesy.

    `reference_matches` is the only path by which a directly-named circular reaches the
    model — measured on `BC & CPD Circular No. 08 of 2021`, neither ranked arm returns it
    at all. A status clause applied to all three lists would answer "what did circular X
    say?" with irrelevant active circulars and no mention of X.
    """
    gone = _add(db, "gone-1", status="cancelled",
                reference="BPRD Circular No. 07 of 2013")
    _add(db, "live-1", status="active")

    arms = _arms(db, query="BPRD Circular No. 07 of 2013")

    assert _ids(arms["reference_matches"]) == [gone.id]
    assert gone.id not in [p["citation"] for p in arms["withdrawn_matches"]]


def test_asking_about_the_old_rule_turns_the_demotion_off(db, no_vectors):
    _add(db, "gone-1", status="superseded")
    _add(db, "live-1", status="active")

    arms = _arms(db, include_withdrawn=True)

    assert set(_ids(arms["lexical_results"])) == {"gone-1", "live-1"}
    assert arms["withdrawn_matches"] == []


@pytest.mark.parametrize("query", [
    "which circulars were superseded in 2019",
    "has BPRD Circular 07 been cancelled",
    "list withdrawn AML circulars",
    "what was the previous version of this rule",
    "what did the 2013 rule say about EDD",
    "what were the limits previously",
    "what did banks used to be required to do",
])
def test_a_question_about_the_old_rule_keeps_withdrawn_circulars(query):
    assert WITHDRAWN_QUERY_PATTERN.search(query)


@pytest.mark.parametrize("query", [
    "enhanced due diligence requirements",
    "what is the current minimum capital requirement",
    "BPRD Circular No. 07 of 2019",
    "Asaan account credit balance limit",
])
def test_an_ordinary_question_does_not(query):
    """The year clause must not fire on a circular reference.

    Every SBP reference ends in a year — "BPRD Circular No. 07 of 2019" — so matching
    years indiscriminately would switch the demotion off for almost every question that
    names a circular.
    """
    assert not WITHDRAWN_QUERY_PATTERN.search(query)


# --------------------------------------------------------- amended stays, at full rank


def test_an_amended_circular_is_still_the_rule_and_stays(db, no_vectors):
    """The bucket includes every `adds_to` and `clarifies` target — 1,858 of 3,172
    relationships — where nothing was changed. Dropping it discards the base text."""
    _add(db, "amend-1", status="amended")
    _add(db, "live-1", status="active")

    arms = _arms(db)

    assert set(_ids(arms["lexical_results"])) == {"amend-1", "live-1"}
    assert arms["withdrawn_matches"] == []


def test_an_amended_circular_is_not_demoted_either(db, no_vectors):
    """Ranking a circular lower because someone once clarified it is noise, not signal."""
    strong = _add(db, "amend-1", status="amended",
                  title="Enhanced Due Diligence for high risk customers")
    _add(db, "live-1", status="active", title="Unrelated matters")

    arms = _arms(db)

    assert _ids(arms["lexical_results"])[0] == strong.id


# --------------------------------------------------------------- naming what changed


def test_an_amended_circular_names_the_circular_that_amended_it(db, no_vectors):
    """The 64.8% finding: a status flag says *that* something happened, never what."""
    base = _add(db, "amend-1", status="amended", year=2019)
    amender = _add(db, "later-1", status="active", year=2021)
    _relate(db, amender, base, "amends")

    row = _arms(db)["lexical_results"][0]

    assert row["circular"].id == base.id
    assert row["amended_by"] == [{
        "citation": f"[[circular:{amender.id}|{amender.display_name}]]",
        "date": "2021-05-14",
        "type": "amends",
    }]
    assert "quoting a figure" in row["note"]


def test_a_named_withdrawn_circular_says_what_replaced_it(db, no_vectors):
    base = _add(db, "gone-1", status="cancelled", year=2013)
    successor = _add(db, "live-1", status="active", year=2024)
    _relate(db, successor, base, "cancels")

    row = _arms(db, query=base.reference)["reference_matches"][0]

    assert row["replaced_by"][0]["citation"].endswith(f"|{successor.display_name}]]")
    assert row["replaced_by"][0]["type"] == "cancels"
    assert "no longer in force" in row["note"]


def test_a_demoted_pointer_says_what_replaced_it(db, no_vectors):
    """The semantic arm often surfaces the successor already, with nothing to say the two
    are related. The `supersedes` edge is in the database; this is what states it."""
    base = _add(db, "gone-1", status="superseded", year=2021)
    successor = _add(db, "live-1", status="active", year=2025)
    _relate(db, successor, base, "supersedes")

    pointer, = _arms(db)["withdrawn_matches"]

    assert pointer["superseded_by"]["citation"].endswith(f"|{successor.display_name}]]")
    assert pointer["superseded_by"]["date"] == "2025-05-14"


def test_replacement_wins_when_a_circular_was_both_amended_and_replaced(db, no_vectors):
    """Reporting the amendment would bury the fact that the circular is gone."""
    base = _add(db, "gone-1", status="cancelled", year=2013)
    amender = _add(db, "amend-1", status="active", year=2016)
    killer = _add(db, "live-1", status="active", year=2024)
    _relate(db, amender, base, "amends")
    _relate(db, killer, base, "cancels")

    row = _arms(db, query=base.reference)["reference_matches"][0]

    assert "amended_by" not in row
    assert [item["citation"] for item in row["replaced_by"]] == [
        f"[[circular:{killer.id}|{killer.display_name}]]"
    ]


def test_the_amender_list_is_capped_and_counts_the_rest(db, no_vectors):
    """`BSD Circular No.18 of 2001` has 266 amenders. Uncapped, annotating that one row
    costs ~16,000 ch — a fifth of a whole search response spent on a list nobody reads
    past the top of."""
    base = _add(db, "amend-1", status="amended", year=2001)
    for index in range(6):
        _relate(db, _add(db, f"later-{index}", year=2010 + index), base, "adds_to")

    row = _arms(db)["lexical_results"][0]

    assert len(row["amended_by"]) == MAX_NAMED_AMENDERS
    assert row["older_changes_not_shown"] == 3


def test_the_named_amenders_are_the_most_recent_ones(db, no_vectors):
    base = _add(db, "amend-1", status="amended", year=2001)
    for index in range(6):
        _relate(db, _add(db, f"later-{index}", year=2010 + index), base, "adds_to")

    row = _arms(db)["lexical_results"][0]

    assert [item["date"] for item in row["amended_by"]] == [
        "2015-05-14", "2014-05-14", "2013-05-14"
    ]


def test_an_unchanged_circular_carries_no_annotation(db, no_vectors):
    _add(db, "live-1", status="active")

    row = _arms(db)["lexical_results"][0]

    assert "amended_by" not in row
    assert "replaced_by" not in row
    assert "note" not in row


# ------------------------------------------------------------ it reaches the model


def test_the_annotation_survives_into_the_tool_payload(db, no_vectors):
    """`_search_result_payload` is the last place it could be dropped."""
    base = _add(db, "amend-1", status="amended", year=2019)
    amender = _add(db, "later-1", status="active", year=2021)
    _relate(db, amender, base, "amends")

    row = _arms(db)["lexical_results"][0]
    payload = AIClient._search_result_payload(row)

    assert payload["status"] == "amended"
    assert payload["amended_by"][0]["citation"].endswith(f"|{amender.display_name}]]")
    assert "note" in payload


def test_the_withdrawn_instruction_is_said_once_not_once_per_pointer():
    """There can be a dozen demoted hits and the instruction is the same for all of them.

    Repeating it per row is the pattern `_dedupe_repeat_row` exists to remove.
    """
    from sbpeye.ai import _withdrawn_section

    section = _withdrawn_section([{"citation": "a"}, {"citation": "b"}])

    assert len(section["withdrawn_matches"]) == 2
    assert isinstance(section["withdrawn_matches_note"], str)
    assert _withdrawn_section([]) == {}


def test_the_browse_ui_can_still_find_a_withdrawn_circular(db, no_vectors):
    """`search()` is shared with browse, where a human *should* reach a cancelled
    circular. The currency rule belongs to the chat arms, not to the engine."""
    _add(db, "gone-1", status="cancelled")

    results, _total = search_engine.search(TOPIC, db, limit=10)

    assert "gone-1" in [item["circular"].id for item in results]
