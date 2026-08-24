"""The rules every chat prompt must carry, and the drift these tests exist to stop.

Before `_CITATION_RULES` and `_ANSWER_CONTRACT` existed, the citation rules lived in three
hand-maintained copies — the two branches of `_chat_system_prompt` and the system message
built by `_tool_result_synthesis_messages` — in three different wordings. The synthesis copy
had already lost a rule: it compressed all three citation rules into one sentence and dropped
the clause saying a source with no handle is named in prose and cited with nothing.

That is the copy that matters most. Synthesis is reached only when the tool loop hits its
iteration ceiling, which is when retrieval is struggling, so the weakest statement of the
contract governed the answers least able to afford it (benchmark round 2026-08-23, item P15).

These tests do not grade the wording. They pin the invariant: whatever the rules say, all
three prompts say the same thing.

The same file also pins the tool *schema* against the same contract. The prompt rule and the
schema are two statements of one fact — what this turn can actually do — and they were not
held together: the prompt withheld `search_selected_documents` from an unselected turn while
the schema offered it anyway (chat session `48655b06`, benchmark P14).
"""

import pytest

from sbpeye.ai import (
    _ANSWER_CONTRACT,
    _CITATION_RULES,
    _SYNTHESIS_EVIDENCE_HEADER,
    _SYNTHESIS_FRAMING,
    TOOLS,
    AIClient,
    AIConfig,
    tools_for_turn,
)


@pytest.fixture
def client() -> AIClient:
    return AIClient(AIConfig(provider="openai", api_key="test", model="test"))


def _synthesis_system(client: AIClient) -> str:
    """The system message the tool-free synthesis step would send."""
    turn = [
        {"role": "user", "content": "who sits on the MPC?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "1",
                "type": "function",
                "function": {"name": "search_corpus", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "1", "content": '{"results": []}'},
    ]
    messages = client._tool_result_synthesis_messages(turn[:1], turn, None)
    return messages[0]["content"]


def _all_three(client: AIClient) -> dict[str, str]:
    return {
        "selected": client._chat_system_prompt("Circular A: ..."),
        "general": client._chat_system_prompt(),
        "synthesis": _synthesis_system(client),
    }


@pytest.mark.parametrize("block", [_CITATION_RULES, _ANSWER_CONTRACT])
def test_every_chat_prompt_carries_the_shared_blocks(client, block):
    """A rule added to one prompt applies on every path, or it applies on none."""
    for name, prompt in _all_three(client).items():
        assert block in prompt, f"{name} prompt is missing a shared block"


def test_the_selected_branch_still_embeds_its_context(client):
    """Composition must not displace what the selected-circulars branch exists to carry."""
    prompt = client._chat_system_prompt("Circular A: the body text")
    assert "Circular A: the body text" in prompt
    assert "Pre-selected circulars:" in prompt


def test_no_prompt_advertises_a_tool_its_path_cannot_call(client):
    """The shared blocks are shared; the tool rules are not, and must not be merged.

    Not a check that the wording has not changed — a check that each prompt offers only what
    that path can actually do. `search_selected_documents` is scoped to selected circulars
    server-side and has nothing to search without them; the synthesis step runs with no tools
    at all. A prompt naming either outside its path invites a call that cannot be served.
    """
    prompts = _all_three(client)
    assert "search_selected_documents" in prompts["selected"]
    assert "search_selected_documents" not in prompts["general"]
    assert "get_law_details" in prompts["general"]
    # Synthesis has no tools at all, so it must not advertise any.
    assert "get_law_details" not in prompts["synthesis"]
    assert "No tools are available in this step." in prompts["synthesis"]


def _offered(selected: list[str] | None) -> set[str]:
    return {tool["function"]["name"] for tool in tools_for_turn(selected)}


def test_the_schema_withholds_what_the_prompt_withholds():
    """The schema owes the same answer as the prompt to "what can this turn do?".

    `_chat_system_prompt` has always kept `search_selected_documents` out of the general
    branch. The schema did not: both loop paths passed the `TOOLS` constant, so an
    unselected turn was handed a tool whose only possible reply is the scope error. On
    session `48655b06` the model duly called it and lost an iteration to the answer.
    """
    assert "search_selected_documents" in _offered(["circular-id-1"])
    assert "search_selected_documents" not in _offered(None)
    assert "search_selected_documents" not in _offered([])


def test_withdrawing_one_tool_withdraws_only_that_tool():
    """A filter is one line away from being a filter that drops too much.

    The unselected turn must lose the selection-scoped tool and keep every other tool
    whole — same objects, so a description edited in `TOOLS` cannot go stale here.
    """
    everything = _offered(["circular-id-1"])
    general = _offered(None)

    assert everything == {tool["function"]["name"] for tool in TOOLS}
    assert everything - general == {"search_selected_documents"}
    assert tools_for_turn(["circular-id-1"]) == TOOLS
    for tool in tools_for_turn(None):
        assert tool in TOOLS


@pytest.mark.parametrize(
    "selected,context,label",
    [(["circular-id-1"], "Circular A: ...", "selected"), (None, None, "general")],
)
def test_every_tool_a_prompt_names_is_offered_on_that_path(
    client, selected, context, label
):
    """Drift guard in the direction the other tests cannot see.

    `test_no_prompt_advertises_a_tool_its_path_cannot_call` reads the prompt and
    `test_the_schema_withholds_what_the_prompt_withholds` reads the schema; neither
    notices if the two are edited apart. A prompt may stay silent about a tool it is
    given — the general branch never names `query_regulatory_values` — but naming one the
    turn does not carry is an instruction to make a call that cannot be served.
    """
    offered = _offered(selected)
    prompt = client._chat_system_prompt(context)
    for tool in TOOLS:
        name = tool["function"]["name"]
        if name in prompt:
            assert name in offered, (
                f"the {label} prompt names {name} but that turn's schema withholds it"
            )


def test_the_scope_guard_survives_the_schema_change(client):
    """Withdrawing the schema is the saving; the guard is what makes the scope a fact.

    A model can call a tool it was never given, so `_execute_tool` must still refuse an
    unselected call rather than reaching for a retriever with nothing to retrieve from.
    """
    result = client._execute_tool(
        "search_selected_documents", {"query": "anything"}, None, None, ""
    )
    assert "No circulars are selected for this chat" in result


def test_the_evidence_header_is_charged_to_the_budget_it_appears_under():
    """One literal, two uses: the framing charge and the header the model actually sees.

    `_SYNTHESIS_FRAMING`'s length is subtracted from the synthesis evidence budget. When the
    header was spelled out separately in both places, renaming one silently stopped the
    ceiling from being the ceiling.
    """
    assert _SYNTHESIS_EVIDENCE_HEADER in _SYNTHESIS_FRAMING


def test_the_synthesis_header_is_what_the_prompt_actually_prints(client):
    turn = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "1",
                "type": "function",
                "function": {"name": "search_corpus", "arguments": "{}"},
            }],
        },
        {"role": "tool", "tool_call_id": "1", "content": '{"results": []}'},
    ]
    messages = client._tool_result_synthesis_messages(turn[:1], turn, None)
    assert _SYNTHESIS_EVIDENCE_HEADER in messages[-1]["content"]
