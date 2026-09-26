"""The tool loop ends when a round puts nothing new in front of the model (C6).

`CHAT_CONTEXT_PLAN.md` §5.6: `_MAX_TOOL_ITERATIONS` was spent unconditionally, and 15% of
tool calls returned only documents already seen; in the 2026-09-26 round P01 and P02 ran
to the ceiling on repeated reads of one regulation. The loop now goes to the final
synthesis as soon as a round adds nothing — and only then: not while nothing has been
found at all, not after a failed call the model may be about to correct.

The provider here always asks for another tool until it is asked to synthesise, which
is the behaviour the guard exists for; the tools write the ledgers the way real ones do.
"""

import json
from types import SimpleNamespace

import pytest

from sbpeye import ai as ai_module
from sbpeye.ai import _MAX_TOOL_ITERATIONS, AIClient, AIConfig


def _client():
    return AIClient(AIConfig(provider="openai", api_key="test", model="test"))


def _tool_call(index, name="search_corpus"):
    return SimpleNamespace(
        id=f"call-{index}", type="function", index=0,
        function=SimpleNamespace(name=name, arguments=json.dumps({"query": f"q{index}"})),
    )


def _provider(stages, tool_name):
    """Ask for a tool every iteration; answer only when synthesis is requested."""
    def create(*, stage, stream=False, **_kwargs):
        stages.append(stage)
        if stage == "chat.final_synthesis":
            if stream:
                return iter([SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="answer", tool_calls=None))])])
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="answer", tool_calls=None))])
        call = _tool_call(len(stages), tool_name(len(stages)))
        if stream:
            return iter([SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[call]))])])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[call]))])
    return create


def _run(client, loop, monkeypatch, rounds, tool_name=lambda i: "search_corpus"):
    """Drive one turn. `rounds[i]` is what round i's tool does: a callable that writes
    the ledgers and returns the result string."""
    stages: list[str] = []
    events: list[tuple[str, dict]] = []
    calls = iter(rounds)
    monkeypatch.setattr(client, "_create_traced_completion", _provider(stages, tool_name))
    monkeypatch.setattr(
        client, "_execute_tool",
        lambda name, args, *a, **k: next(calls, _nothing)(client),
    )
    monkeypatch.setattr(ai_module, "emit_event",
                        lambda kind, payload, **k: events.append((kind, payload)))
    result = getattr(client, loop)([{"role": "user", "content": "q"}], db=None)
    if loop == "_stream_chat_impl":
        list(result)
    return stages, [payload for kind, payload in events if kind == "early_stop"]


def _new_passage(key):
    def tool(client):
        client._sent_passages.setdefault("doc-1", set()).add(key)
        return json.dumps({"passages": [{"chunk_index": 0, "passage": "text"}]})
    return tool


def _nothing(client):
    return json.dumps({"passages": [], "provided_earlier": {"chunks": "0"}})


def _failed(client):
    return json.dumps({"error": "Ambiguous circular reference."})


def _empty_search(client):
    return json.dumps({"lexical_results": [], "semantic_results": [], "count": 0})


# ------------------------------------------------------------------------------ stops


@pytest.mark.parametrize("loop", ["_chat_impl", "_stream_chat_impl"])
def test_a_round_that_adds_nothing_ends_the_loop(loop, monkeypatch):
    stages, stops = _run(_client(), loop, monkeypatch, [_new_passage("a"), _nothing])

    assert stages == ["chat.iteration.1", "chat.iteration.2", "chat.final_synthesis"]
    assert stops == [{"reason": "no_new_evidence", "iteration": 2, "evidence_items": 1}]


def test_a_search_that_only_repeats_documents_ends_the_loop(monkeypatch):
    """The real shape of the waste: a rephrased search whose rows are all C1a pointers."""
    client = _client()

    def first(client):
        client._sent_text_keys["c-1"] = ["full_circular_text"]
        return json.dumps({"lexical_results": [{"citation": "[[circular:c-1|C 1]]"}]})

    def repeat(client):
        # A pointer row, plus a referenced law it has never opened: pointers are not
        # evidence, so neither is new.
        return json.dumps({"lexical_results": [{
            "citation": "[[circular:c-1|C 1]]", "duplicate_of_earlier_entry": True,
            "references_laws": [{"citation": "[[law:l-1|Act]]"}],
        }]})

    stages, stops = _run(client, "_chat_impl", monkeypatch, [first, repeat])

    assert stages[-1] == "chat.final_synthesis" and len(stages) == 3
    assert stops


# -------------------------------------------------------------------- does not stop


def test_every_productive_round_runs_to_the_ceiling(monkeypatch):
    rounds = [_new_passage(str(i)) for i in range(_MAX_TOOL_ITERATIONS)]

    stages, stops = _run(_client(), "_chat_impl", monkeypatch, rounds)

    assert len(stages) == _MAX_TOOL_ITERATIONS + 1
    # The ceiling is the ceiling, not an early stop, and is not recorded as one.
    assert stops == []


def test_a_turn_that_has_found_nothing_keeps_looking(monkeypatch):
    """Empty searches are the cue to rephrase — the one time another round is right."""
    rounds = [_empty_search, _empty_search, _new_passage("a"), _nothing]

    stages, stops = _run(_client(), "_chat_impl", monkeypatch, rounds)

    assert stages[:4] == [f"chat.iteration.{i}" for i in range(1, 5)]
    assert stops == [{"reason": "no_new_evidence", "iteration": 4, "evidence_items": 1}]


def test_a_failed_call_gets_its_correction(monkeypatch):
    """P02: a misread reference, then the retry that found the right circular."""
    rounds = [_new_passage("a"), _failed, _new_passage("b"), _nothing]

    stages, stops = _run(_client(), "_chat_impl", monkeypatch, rounds)

    assert stops and stops[0]["iteration"] == 4


def test_a_document_cited_by_a_ledgerless_tool_is_new_evidence(monkeypatch):
    """`get_latest_circulars` and friends write no ledger; what they cite is the signal."""
    client = _client()

    def latest(client):
        return json.dumps({"results": [{"citation": "[[circular:c-9|C 9]]"}]})

    names = {1: "search_corpus", 2: "get_latest_circulars", 3: "get_latest_circulars"}
    stages, stops = _run(
        client, "_chat_impl", monkeypatch, [_new_passage("a"), latest, latest],
        tool_name=lambda i: names.get(i, "search_corpus"),
    )

    # Round 2 cited a new circular; round 3 cited the same one again, and stopped.
    assert stops == [{"reason": "no_new_evidence", "iteration": 3, "evidence_items": 2}]
