"""How the final, tool-free synthesis step spends its window on tool results.

The motivating failure, from the 2026-08-23 benchmark round — chat session
`9f5724b0`, item P15, "Who sits on the Monetary Policy Committee, and what is the
quorum for its meetings?". The model made **ten** tool calls across five iterations,
hit the iteration ceiling, and fell through to synthesis. The synthesis prompt
contained **one** of the ten: a broad opening `search_circulars` that had returned four
unrelated BPRD governance circulars, clipped mid-JSON at exactly 16,000 characters. The
budget loop spent the window in call order with no per-result bound and then `break`.

The fifth call had returned the State Bank of Pakistan Act, 1956 with the quorum
provision in it. It never reached the model, and the answer went out saying the Act was
"not among the provided sources".

Run 1 of the same question differed in one respect only: the model happened to call the
laws sweep *first*, so that result got the window and the answer partly landed. Nothing
about the retrieval differed. These pin the properties that remove the coin-flip.
"""

import pytest

from sbpeye.ai import AIClient, AIConfig


def _client(budget_chars: int = 16_000) -> AIClient:
    """A client whose synthesis evidence budget is pinned, so sizes here are exact.

    `_synthesis_evidence_budget` resolves the provider's context window, which is the
    behaviour under test in `test_the_budget_follows_the_model_not_the_document_clip`;
    everywhere else it is a fixture detail and is fixed.
    """
    client = AIClient(AIConfig(provider="openai", api_key="test", model="test"))
    client._context_budget = max(1, budget_chars // 4)
    return client


def _turn(*results: tuple[str, str, str]) -> list[dict]:
    """A `full_messages` transcript for the given (call_id, tool_name, content) results."""
    messages: list[dict] = [{"role": "user", "content": "who sits on the MPC?"}]
    for call_id, name, content in results:
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": '{"query": "monetary policy"}'},
            }],
        })
        messages.append({
            "role": "tool", "tool_call_id": call_id, "content": content,
        })
    return messages


def _synthesis_text(client: AIClient, full_messages: list[dict]) -> str:
    built = client._tool_result_synthesis_messages(
        [{"role": "user", "content": "who sits on the MPC?"}], full_messages, None,
    )
    return built[-1]["content"]


def _system_text(client: AIClient, full_messages: list[dict]) -> str:
    built = client._tool_result_synthesis_messages(
        [{"role": "user", "content": "who sits on the MPC?"}], full_messages, None,
    )
    return built[0]["content"]


# ------------------------------------------------------------------- the P15 regression

def test_a_late_tool_result_survives_an_oversized_first_one():
    """P15 run 2, reduced to its mechanism.

    A huge opening search followed by the one lookup that actually found the Act. Under
    the old first-come loop the opening search took the whole window and the Act was
    dropped; the answer then denied the Act was available.
    """
    full_messages = _turn(
        ("call-1", "search_corpus", "IRRELEVANT " * 6000),
        ("call-2", "get_law_details", 'THE-ACT {"quorum": "four members"}'),
    )

    text = _synthesis_text(_client(), full_messages)

    assert "THE-ACT" in text, "the result that answers the question must reach the model"


def test_every_tool_result_of_the_turn_is_represented():
    """Ten calls in, ten sections out — P15 run 2 put one of ten in front of the model."""
    full_messages = _turn(*[
        (f"call-{index}", "search_corpus", f"MARKER-{index} " + "x" * 4000)
        for index in range(10)
    ])

    text = _synthesis_text(_client(), full_messages)

    missing = [index for index in range(10) if f"MARKER-{index}" not in text]
    assert not missing, f"tool results dropped entirely: {missing}"


def test_the_order_results_arrive_in_does_not_decide_who_gets_the_window():
    """The coin-flip between run 1 and run 2 was call order and nothing else."""
    big = "IRRELEVANT " * 6000
    act = 'THE-ACT {"quorum": "four members"}'
    first = _synthesis_text(_client(), _turn(
        ("call-1", "search_corpus", big), ("call-2", "get_law_details", act),
    ))
    reversed_ = _synthesis_text(_client(), _turn(
        ("call-1", "get_law_details", act), ("call-2", "search_corpus", big),
    ))

    assert "THE-ACT" in first and "THE-ACT" in reversed_


# ------------------------------------------------------------------------- fair sharing

def test_a_small_result_is_never_clipped_to_feed_a_large_one():
    """Max-min fairness: a result wanting less than its share takes only what it needs."""
    small = '{"error": "No circulars are selected for this chat"}'
    full_messages = _turn(
        ("call-1", "search_corpus", "x" * 60_000),
        ("call-2", "search_selected_documents", small),
    )

    text = _synthesis_text(_client(), full_messages)

    assert small in text, "a 51-character result fits any budget and must arrive whole"


def test_the_surplus_from_small_results_is_redistributed():
    """An equal split alone would waste the share of every result that did not need it."""
    tiny = [(f"call-{i}", "get_circular_details", "ok") for i in range(8)]
    full_messages = _turn(*tiny, ("call-big", "search_corpus", "y" * 60_000))

    text = _synthesis_text(_client(), full_messages)

    # An unredistributed ninth share of 16,000 would be ~1,777 characters.
    assert text.count("y") > 5_000


# --------------------------------------------------------------------------- disclosure

def test_a_clipped_result_says_it_was_clipped():
    """A truncated JSON payload that does not announce itself reads as a complete one."""
    full_messages = _turn(("call-1", "search_corpus", "z" * 60_000))

    text = _synthesis_text(_client(), full_messages)

    assert "[clipped:" in text
    assert "characters of this result are not shown]" in text


def test_the_prompt_tells_the_model_the_record_is_partial():
    """Protect the round's one reliable virtue: retrieval failures disclosed, not papered over."""
    full_messages = _turn(("call-1", "search_corpus", "z" * 60_000))

    system = _system_text(_client(), full_messages)

    assert "clipped" in system
    assert "Do not guess at clipped content." in system


def test_nothing_is_said_about_clipping_when_nothing_was_clipped():
    full_messages = _turn(("call-1", "get_law_details", "short result"))

    system = _system_text(_client(), full_messages)

    assert "clipped" not in system


def test_each_section_names_the_tool_that_produced_it():
    """Unattributed JSON blobs cannot be weighed against each other."""
    full_messages = _turn(
        ("call-1", "search_corpus", "first"),
        ("call-2", "get_law_details", "second"),
    )

    text = _synthesis_text(_client(), full_messages)

    assert "Result of search_corpus(" in text
    assert "Result of get_law_details(" in text


# ------------------------------------------------------------------------------ bounds

@pytest.mark.parametrize("budget_chars", [16_000, 400_000])
@pytest.mark.parametrize("result_count", [1, 6, 40])
def test_the_budget_is_respected(budget_chars, result_count):
    """Labels and clip markers are charged to the budget, not added on top of it."""
    full_messages = _turn(*[
        (f"call-{index}", "search_corpus", "x" * 200_000)
        for index in range(result_count)
    ])

    text = _synthesis_text(_client(budget_chars), full_messages)

    assert len(text) <= budget_chars


def test_the_budget_follows_the_model_not_the_document_clip():
    """`max_context_tokens` is a per-document character clip, not a context window.

    Reading it as a window gave every provider the same 16,000 characters of evidence
    for a whole turn, whatever model was behind it — the 2026-08-23 round ran on that.
    The budget now comes from `resolve_context_budget`, so it tracks the provider.
    """
    def budget_for(provider: str) -> int:
        client = AIClient(AIConfig(
            provider=provider, api_key="test", model="test", max_context_tokens=4000,
        ))
        return client._synthesis_evidence_budget()

    # The document clip is identical across all three; the evidence budget is not.
    assert budget_for("lmstudio") < budget_for("openrouter") < budget_for("openai")
    # Every one of them beats the flat 16,000 the old expression produced.
    assert budget_for("lmstudio") > 16_000


def test_a_turn_with_no_tool_results_still_builds():
    built = _client()._tool_result_synthesis_messages(
        [{"role": "user", "content": "hello"}],
        [{"role": "user", "content": "hello"}],
        None,
    )

    assert built[0]["role"] == "system"
    assert "No selected circular context was provided." in built[-1]["content"]


def test_fair_shares_never_exceeds_the_budget():
    shares = AIClient._fair_shares([10, 200, 3000, 5], budget=100)

    assert sum(shares) <= 100
    assert all(share >= 0 for share in shares)


def test_fair_shares_gives_everything_away_when_the_budget_allows():
    lengths = [10, 200, 300, 5]
    shares = AIClient._fair_shares(lengths, budget=10_000)

    assert shares == lengths
