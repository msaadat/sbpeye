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
"""

import pytest

from sbpeye.ai import (
    _ANSWER_CONTRACT,
    _CITATION_RULES,
    _SYNTHESIS_EVIDENCE_HEADER,
    _SYNTHESIS_FRAMING,
    AIClient,
    AIConfig,
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
