"""The research steps behind an answer survive the turn that produced them.

A chat turn's tool payloads used to exist only while the request was running: the view
listed the steps a turn took, but a reader who wanted to know *what* a search returned
had nothing to open, and a page reload lost even the labels. These pin the record that
replaces that — a digest of each tool call, parsed from the payload the tool itself
built, stored with the answer, and fetched a step at a time when someone opens one.

Two properties matter beyond "it is saved". The digest is a **parse**, so the same
payload always yields the same record and nothing is regenerated on read. And it holds
the *real* citation tokens rather than the model-facing handles, because the reader
resolving them is a person clicking through to the document.
"""

import json

import pytest

from sbpeye.ai import AIClient, AIConfig
from sbpeye.chat_steps import STEP_SCHEMA_VERSION, build_step, failed_step
from sbpeye.citation_handles import CitationHandles
from sbpeye.models import ChatMessage, ChatSession

from conftest import TEST_ADMIN_ID, sign_in


def _step(name, payload, arguments=None, **kwargs):
    return build_step(name, arguments or {}, json.dumps(payload), label="Label", **kwargs)


# ------------------------------------------------------------------ what a step records


def test_a_search_lists_each_document_once_with_every_arm_that_found_it():
    """The arms of one response are three rankings of one corpus, not three result sets.

    A document that placed in both is the same document; listed twice it reads as two
    findings, which is the opposite of what agreement between the arms means.
    """
    step = _step("search_corpus", {
        "ranking": "dual_arm",
        "lexical_results": [{
            "title": "Capital adequacy",
            "reference": "BPRD Circular No. 3 of 2026",
            "date": "2026-01-04",
            "department": "BPRD",
            "citation": "[[circular:c-1|BPRD Circular No. 3 of 2026]]",
            "matching_passages": [{"passage": "The CAR shall not be less than 12.5%."}],
        }],
        "semantic_results": [{
            "title": "Capital adequacy",
            "citation": "[[circular:c-1|BPRD Circular No. 3 of 2026]]",
            "duplicate_of_earlier_entry": True,
        }],
        "count": 1,
    })

    assert len(step["hits"]) == 1
    hit = step["hits"][0]
    assert hit["citation"] == "[[circular:c-1|BPRD Circular No. 3 of 2026]]"
    assert hit["match"] == ["keyword", "meaning"]
    assert hit["snippet"] == "The CAR shall not be less than 12.5%."
    assert step["summary"] == "1 document"


def test_law_and_withdrawn_arms_are_kept_and_labelled():
    step = _step("search_corpus", {
        "ranking": "dual_arm",
        "lexical_results": [],
        "law_results": [{
            "title": "Banking Companies Ordinance, 1962",
            "law_type": "Ordinance",
            "citation": "[[law:l-1|Banking Companies Ordinance, 1962]]",
            "passages": [{"passage": "Section 41 applies."}],
        }],
        "withdrawn_matches": [{
            "title": "Old rule",
            "reference": "BPRD Circular No. 1 of 2019",
            "status": "superseded",
            "citation": "[[circular:c-9|BPRD Circular No. 1 of 2019]]",
            "superseded_by": "BPRD Circular No. 3 of 2026",
        }],
    })

    law, withdrawn = step["hits"]
    assert law["match"] == ["law"] and law["department"] == "Ordinance"
    assert withdrawn["match"] == ["withdrawn"]
    assert withdrawn["status"] == "superseded"
    assert withdrawn["note"] == "Superseded by BPRD Circular No. 3 of 2026"


def test_an_active_status_is_left_off_because_it_says_nothing():
    step = _step("get_latest_circulars", {"results": [
        {"title": "A", "status": "active", "citation": "[[circular:c-1|A]]"},
    ]})

    assert "status" not in step["hits"][0]


def test_a_located_passage_outranks_the_summary_as_the_snippet():
    """The summary is not evidence of a match; it is used only when nothing else exists."""
    with_passage = _step("search_corpus", {"results": [{
        "title": "A", "summary": "A general description.",
        "matching_passage_excerpt": "…the limit is USD 30,000…",
    }]})
    without = _step("search_corpus", {"results": [{
        "title": "A", "summary": "A general description.",
    }]})

    assert with_passage["hits"][0]["snippet"] == "…the limit is USD 30,000…"
    assert without["hits"][0]["snippet"] == "A general description."


def test_a_long_list_is_capped_and_says_how_many_it_dropped():
    step = _step("search_corpus", {"results": [
        {"title": f"Circular {index}", "citation": f"[[circular:c-{index}|C{index}]]"}
        for index in range(30)
    ]})

    assert len(step["hits"]) == 20
    assert step["omitted"] == 10
    assert step["summary"] == "30 documents (10 not shown)"


def test_a_long_passage_is_clipped_rather_than_stored_whole():
    step = _step("search_selected_documents", {"results": [
        {"source_label": "Annexure", "passage": "x" * 5000},
    ]})

    snippet = step["hits"][0]["snippet"]
    assert len(snippet) < 300 and snippet.endswith("…")
    assert step["summary"] == "1 passage"


def test_a_tool_error_becomes_the_step_summary():
    step = _step("get_circular_details", {"error": "Circular not found: BPRD 99"})

    assert step["error"] == "Circular not found: BPRD 99"
    assert step["summary"] == "Circular not found: BPRD 99"
    assert "hits" not in step


def test_an_ambiguous_reference_shows_its_candidates_rather_than_reading_as_a_failure():
    step = _step("get_circular_details", {
        "error": "Ambiguous circular reference. Include the year.",
        "candidates": [
            {"title": "A", "reference": "BPRD 3 of 2024", "citation": "[[circular:c-1|A]]"},
            {"title": "B", "reference": "BPRD 3 of 2026", "citation": "[[circular:c-2|B]]"},
        ],
    })

    assert [hit["reference"] for hit in step["hits"]] == ["BPRD 3 of 2024", "BPRD 3 of 2026"]
    assert step["error"].startswith("Ambiguous")
    assert step["summary"] == "2 documents"


def test_a_regulatory_value_is_recorded_the_way_the_row_states_it():
    step = _step("query_regulatory_values", {"results": [{
        "metric": "Capital adequacy ratio",
        "comparator": ">=", "value": 12.5, "unit": "%",
        "subject": "All banks",
        "effective_date": "2026-01-01",
        "context": "The CAR shall not be less than 12.5%.",
        "citation": "[[circular:c-1|BPRD Circular No. 3 of 2026]]",
    }]})

    hit = step["hits"][0]
    assert hit["title"] == "Capital adequacy ratio"
    assert hit["reference"] == "All banks"
    assert hit["note"] == ">= 12.5 %"
    assert step["summary"] == "1 value"


def test_a_law_lookup_lists_its_passages_under_the_instrument():
    step = _step("get_law_details", {
        "resolved_title": "Banking Companies Ordinance, 1962",
        "law_type": "Ordinance",
        "citation": "[[law:l-1|Banking Companies Ordinance, 1962]]",
        "passages": [
            {"passage": "Section 41.", "locator": "s. 41", "page": 12},
            {"passage": "Section 42.", "locator": "s. 42"},
        ],
        "passage_count": 2,
    })

    assert step["summary"] == "2 passages from Banking Companies Ordinance, 1962"
    assert [hit["reference"] for hit in step["hits"]] == ["s. 41", "s. 42"]
    assert step["hits"][0]["note"] == "Page 12"
    assert all(hit["citation"] == "[[law:l-1|Banking Companies Ordinance, 1962]]"
               for hit in step["hits"])


def test_an_inventory_answer_that_was_cut_short_is_marked_incomplete():
    step = _step("search_regulatory_inventory", {
        "documents_matched": 40,
        "documents_returned": 2,
        "complete": False,
        "omitted": 38,
        "results": [
            {"title": "A", "matched_terms": ["outsourcing"], "passage": "…",
             "citation": "[[circular:c-1|A]]"},
            {"title": "B", "matched_terms": ["outsourcing"], "passage": "…",
             "citation": "[[circular:c-2|B]]"},
        ],
    })

    assert step["incomplete"] is True
    assert step["summary"] == "2 of 40 matching documents"
    assert step["hits"][0]["note"] == "Matched: outsourcing"


def test_a_payload_that_cannot_be_read_still_records_what_ran():
    step = build_step("search_corpus", {"query": "capital"}, "not json at all", label="Search")

    assert step["tool"] == "search_corpus"
    assert step["arguments"] == {"query": "capital"}
    assert step["summary"] == "Result could not be read"


def test_a_failed_tool_is_a_step_too():
    step = failed_step("search_corpus", {"query": "capital"},
                       label="Search", error="Connection refused")

    assert step["error"] == "Connection refused"
    assert step["v"] == STEP_SCHEMA_VERSION


# ------------------------------------------------------------------- inside a chat turn


def _tool_client() -> AIClient:
    """A client with no SDK behind it; only the tool loop is exercised."""
    client = AIClient.__new__(AIClient)
    client.config = AIConfig(provider="openrouter", api_key="test", model="test")
    client._client = None
    client._structured_mode = "json_object"
    client._context_budget = None
    client._sent_text_keys = {}
    client._turn_steps = []
    return client


def _call(name="search_corpus", arguments='{"query": "capital"}'):
    return {
        "id": "call-1", "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_the_step_holds_real_citations_not_the_model_facing_handles(monkeypatch):
    """The model is shown `[[c:BPRD-CL-03-2026]]`; the reader must be shown the id.

    Handles exist so the model cannot mangle a uuid. A step is read by a person
    clicking through to the document, so it is digested before the substitution.
    """
    client = _tool_client()
    payload = json.dumps({"results": [{
        "title": "Capital adequacy",
        "reference": "BPRD Circular No. 3 of 2026",
        "citation": "[[circular:c-1|BPRD Circular No. 3 of 2026]]",
    }]})
    monkeypatch.setattr(client, "_execute_tool", lambda *a, **k: payload)
    handles = CitationHandles()
    messages = [{"role": "user", "content": "What is the CAR?"}]

    client._apply_tool_calls(messages, "Let me search.", [_call()], None, None, handles)

    step = client.turn_steps[0]
    assert step["hits"][0]["citation"] == "[[circular:c-1|BPRD Circular No. 3 of 2026]]"
    # The model's copy went through the substitution, which is what proves the two
    # views are built from different strings rather than the same one.
    assert "[[circular:c-1|" not in messages[-1]["content"]


def test_a_tool_that_raises_still_leaves_its_step_behind(monkeypatch):
    client = _tool_client()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("index unavailable")

    monkeypatch.setattr(client, "_execute_tool", _boom)

    with pytest.raises(RuntimeError):
        client._apply_tool_calls([], "", [_call()], None, None, CitationHandles())

    assert client.turn_steps[0]["error"] == "index unavailable"


def test_steps_do_not_survive_into_the_next_turn(monkeypatch):
    """Turn scope, for the reason the text ledger has it: a client serves one request."""
    from types import SimpleNamespace

    client = _tool_client()
    client._turn_steps = [{"tool": "stale"}]
    monkeypatch.setattr(client, "_create_traced_completion", lambda *a, **k: SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="answer", tool_calls=None))]
    ))

    client._chat_impl([{"role": "user", "content": "hi"}], db=None)

    assert client.turn_steps == []


# --------------------------------------------------------------- fetched, not broadcast


def _session_with_steps(db_factory, steps, *, user_id=TEST_ADMIN_ID):
    db = db_factory()
    db.add(ChatSession(id="s-1", user_id=user_id, title="Capital"))
    db.add(ChatMessage(id="m-1", session_id="s-1", role="user", content="What is the CAR?"))
    db.add(ChatMessage(
        id="m-2", session_id="s-1", role="assistant", content="12.5%.",
        steps_json=json.dumps(steps),
    ))
    db.commit()
    db.close()


STORED_STEPS = [
    {
        "v": STEP_SCHEMA_VERSION, "tool": "search_corpus",
        "label": "Searching circulars and laws", "summary": "1 document",
        "arguments": {"query": "capital adequacy"},
        "note": "Let me search the corpus.",
        "hits": [{
            "citation": "[[circular:c-1|BPRD Circular No. 3 of 2026]]",
            "title": "Capital adequacy",
            "snippet": "The CAR shall not be less than 12.5%.",
        }],
    },
    {
        "v": STEP_SCHEMA_VERSION, "tool": "get_circular_details",
        "label": "Reading circular details", "summary": "Capital adequacy",
        "arguments": {"circular_reference": "BPRD Circular No. 3 of 2026"},
    },
]


def test_the_conversation_carries_the_step_names_and_not_what_they_found(client):
    """The names are what a reader chooses between; the evidence is what they open.

    Sending the evidence too would put several times the answer's own size into every
    conversation load for something almost nobody opens.
    """
    test_client, db_factory = client
    _session_with_steps(db_factory, STORED_STEPS)

    body = test_client.get("/api/chat/sessions/s-1").json()

    assert [len(m["steps"]) for m in body["messages"]] == [0, 2]
    assert [step["label"] for step in body["messages"][1]["steps"]] == [
        "Searching circulars and laws", "Reading circular details",
    ]
    assert "The CAR shall not be less than" not in json.dumps(body)
    assert "capital adequacy" not in json.dumps(body)


def test_opening_a_step_returns_what_the_tool_found(client):
    test_client, db_factory = client
    _session_with_steps(db_factory, STORED_STEPS)

    body = test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/0").json()

    assert body["index"] == 0 and body["step_count"] == 2
    assert body["step"]["label"] == "Searching circulars and laws"
    assert body["step"]["note"] == "Let me search the corpus."
    assert body["step"]["hits"][0]["citation"] == "[[circular:c-1|BPRD Circular No. 3 of 2026]]"


def test_the_same_step_reads_the_same_way_every_time(client):
    """Nothing is regenerated on read, so two fetches cannot disagree."""
    test_client, db_factory = client
    _session_with_steps(db_factory, STORED_STEPS)

    first = test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/1").json()
    second = test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/1").json()

    assert first == second


@pytest.mark.parametrize("path", [
    "/api/chat/sessions/s-1/messages/m-2/steps/2",
    "/api/chat/sessions/s-1/messages/m-2/steps/-1",
    "/api/chat/sessions/s-1/messages/nope/steps/0",
    "/api/chat/sessions/nope/messages/m-2/steps/0",
])
def test_a_step_that_does_not_exist_is_a_404(client, path):
    test_client, db_factory = client
    _session_with_steps(db_factory, STORED_STEPS)

    assert test_client.get(path).status_code == 404


def test_a_step_belonging_to_someone_else_is_not_readable(client):
    """Ownership is checked on the session, as it is on every other message route."""
    test_client, db_factory = client
    _session_with_steps(db_factory, STORED_STEPS, user_id="someone-else")
    sign_in(test_client, db_factory, user_id="intruder", email="intruder@example.com")

    response = test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/0")

    assert response.status_code == 404


def test_a_turn_recorded_before_this_existed_simply_has_no_steps(client):
    """Older rows are NULL: the payloads they came from are gone and cannot be rebuilt."""
    test_client, db_factory = client
    db = db_factory()
    db.add(ChatSession(id="s-1", user_id=TEST_ADMIN_ID, title="Capital"))
    db.add(ChatMessage(id="m-2", session_id="s-1", role="assistant", content="12.5%."))
    db.commit()
    db.close()

    body = test_client.get("/api/chat/sessions/s-1").json()

    assert body["messages"][0]["steps"] == []
    assert test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/0").status_code == 404


# ------------------------------------------------------------------------- the gate


def test_steps_are_not_offered_to_a_non_admin_account(client):
    """Admin-only while the feature settles. The account still owns the session — the
    gate is about who is shown the machinery, not about whose conversation it is."""
    test_client, db_factory = client
    sign_in(test_client, db_factory, user_id="tester-1", email="tester1@example.com")
    _session_with_steps(db_factory, STORED_STEPS, user_id="tester-1")

    body = test_client.get("/api/chat/sessions/s-1").json()

    assert body["messages"][1]["steps"] == []
    assert test_client.get("/api/chat/sessions/s-1/messages/m-2/steps/0").status_code == 404


def test_the_turn_records_its_steps_whoever_asked(client):
    """The gate is on reading, not on writing: lifting it later has to expose the
    history that accumulated behind it, not start the record from that day."""
    test_client, db_factory = client
    sign_in(test_client, db_factory, user_id="tester-2", email="tester2@example.com")
    _session_with_steps(db_factory, STORED_STEPS, user_id="tester-2")

    db = db_factory()
    stored = db.query(ChatMessage).filter(ChatMessage.id == "m-2").first().steps_json
    db.close()

    assert len(json.loads(stored)) == 2
