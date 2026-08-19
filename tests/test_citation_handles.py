"""The citation gate: what the model is shown, and what is allowed back out.

The 2026-08-19 benchmark round is the reference case throughout — the malformed ids below
are the ones that round actually produced.
"""

import json

from sbpeye.ai import AIClient, AIConfig, _messages_with_handles
from sbpeye.citation_handles import CitationHandles, StreamExpander, slugify


REAL_ID = "da13ed0e-96c5-5945-adb2-e5d9687301b5"
REAL_LABEL = "BPRD Circular No. 05 of 2020"
REAL_TOKEN = f"[[circular:{REAL_ID}|{REAL_LABEL}]]"


def handles_for(*tokens: str) -> tuple[CitationHandles, str]:
    handles = CitationHandles()
    return handles, handles.to_handles(" ".join(tokens))


def test_slug_keeps_the_parts_that_identify_a_document():
    assert slugify("BPRD Circular Letter No. 01 of 2021") == "BPRD-CL-01-2021"
    assert slugify("FE Circular No. 02 of 2026") == "FE-C-02-2026"
    assert slugify("SH&SFD Circular Letter No. 04 of 2026") == "SHSFD-CL-04-2026"
    assert slugify("CL8-Annex_A-_NCB_Form.xlsx", kind="attachment") == "CL8-Annex-A-NCB-Form"


def test_long_law_titles_keep_the_words_that_tell_them_apart():
    """Chapters of one manual differ only at the end, so the tail must survive."""
    twelve = slugify("Foreign Exchange Manual - Chapter 12: EXPORTS OF GOODS", kind="law")
    thirteen = slugify("Foreign Exchange Manual - Chapter 13: IMPORTS OF GOODS", kind="law")

    assert twelve != thirteen
    assert twelve.endswith("EXPORTS-OF-GOODS")


def test_handle_round_trip_restores_the_original_token():
    handles, context = handles_for(REAL_TOKEN)

    assert REAL_ID not in context
    assert handles.expand(f"See {context} for detail.") == f"See {REAL_TOKEN} for detail."


def test_minting_is_stable_so_one_document_has_one_handle():
    handles = CitationHandles()

    first = handles.to_handles(REAL_TOKEN)
    second = handles.to_handles(f"again {REAL_TOKEN}")

    assert first in second
    assert handles.to_handles(first) == first  # already converted, nothing to do


def test_two_documents_with_the_same_label_get_different_handles():
    handles = CitationHandles()

    one = handles.to_handles("[[circular:id-one|BPRD Circular No. 05 of 2020]]")
    two = handles.to_handles("[[circular:id-two|BPRD Circular No. 05 of 2020]]")

    assert one != two
    assert handles.expand(one + two).count("id-one") == 1
    assert handles.expand(one + two).count("id-two") == 1


def test_the_labels_a_reader_sees_come_from_the_server_not_the_model():
    handles, context = handles_for(REAL_TOKEN)
    slug = context.strip()

    # The model appended its own label to the handle. The pill must not show it.
    expanded = handles.expand(slug.replace("]]", "|BPRD Circular No. 99 of 1999]]"))

    assert expanded == REAL_TOKEN
    assert "1999" not in expanded


def test_an_unknown_handle_degrades_to_prose_rather_than_a_dead_link():
    handles, _ = handles_for(REAL_TOKEN)

    expanded = handles.expand("Required under [[c:BPRD-C-99-2099]] as amended.")

    assert expanded == "Required under BPRD-C-99-2099 as amended."
    assert handles.dropped == [{"reason": "unknown_handle", "handle": "c:BPRD-C-99-2099"}]


def test_an_id_never_offered_this_turn_is_dropped_to_its_label():
    """P03's `…645299` was well-formed and absent from the database."""
    handles, _ = handles_for(REAL_TOKEN)
    invented = "[[circular:f5738d4b-c49e-5199-ac3b-9a8148645299|BPRD Circular No. 3 of 2025]]"

    expanded = handles.expand(f"Per {invented} the limit applies.")

    assert expanded == "Per BPRD Circular No. 3 of 2025 the limit applies."
    assert handles.dropped[0]["reason"] == "unknown_id"


def test_a_bare_uuid_in_the_answer_is_removed():
    """No ids go in, so an id coming out was invented, whatever it looks like."""
    handles, _ = handles_for(REAL_TOKEN)

    assert handles.expand(f"Circular {REAL_ID} applies.") == "Circular applies."


def test_a_real_token_survives_the_uuid_strip():
    handles, context = handles_for(REAL_TOKEN)

    assert handles.expand(f"See {context}.") == f"See {REAL_TOKEN}."


def test_the_malformed_ids_from_the_benchmark_round_all_stop_at_the_gate():
    handles, _ = handles_for(REAL_TOKEN)
    observed = [
        "[[circular:da13ed0e-96c5-5945-ut]]",
        "[[circular:1dc2c et al]]",
        "[[circular:1dcf1c84]]",
        "[[circular:ce964153-c290-541d-ade4-4c725a5f12d|BPRD Circular No. 07 of 2019]]",
        "[[circular:da13ed0e-96c5-5945-advb2-*]]",
    ]

    expanded = handles.expand(" ".join(observed))

    assert "[[circular:" not in expanded
    assert "-96c5-" not in expanded


def test_a_handle_split_across_stream_chunks_still_resolves():
    handles, context = handles_for(REAL_TOKEN)
    answer = f"As set out in {context.strip()} the ratio applies."

    for size in (1, 2, 3, 7, 13):
        expander = StreamExpander(handles)
        chunks = [answer[index : index + size] for index in range(0, len(answer), size)]
        streamed = "".join(expander.feed(chunk) for chunk in chunks) + expander.close()

        assert streamed == f"As set out in {REAL_TOKEN} the ratio applies.", f"at chunk size {size}"


def test_a_uuid_split_across_stream_chunks_is_not_half_released():
    handles, _ = handles_for(REAL_TOKEN)
    expander = StreamExpander(handles)
    answer = f"Circular {REAL_ID} applies."

    streamed = "".join(expander.feed(char) for char in answer) + expander.close()

    assert streamed == "Circular applies."


def test_prose_in_brackets_is_not_held_back_forever():
    handles, _ = handles_for(REAL_TOKEN)
    expander = StreamExpander(handles)
    answer = "The schedule [[ see annexure ]] lists the rates. " + "x" * 300

    streamed = "".join(expander.feed(char) for char in answer) + expander.close()

    assert streamed == answer


def test_a_dropped_citation_takes_its_own_punctuation_with_it():
    handles, _ = handles_for(REAL_TOKEN)

    assert handles.expand("Banks must report [[circular:1dcf1c84]] monthly.") == (
        "Banks must report monthly."
    )
    assert handles.expand(f"Banks must report ({REAL_ID}) monthly.") == (
        "Banks must report monthly."
    )
    assert handles.expand("Banks must report ([[circular:1dc2c et al]]) monthly.") == (
        "Banks must report monthly."
    )


def test_prose_that_merely_looks_like_markup_is_left_alone():
    """A drop elsewhere in the answer must not reflow unrelated text."""
    handles, _ = handles_for(REAL_TOKEN)
    answer = f"The schedule [[ see annexure ]] lists  the rates. Id {REAL_ID} invented."

    assert handles.expand(answer) == "The schedule [[ see annexure ]] lists  the rates. Id invented."


def test_a_streamed_answer_reads_the_same_as_a_whole_one():
    """The reader sees the stream; the database stores it. They must not diverge."""
    handles, context = handles_for(REAL_TOKEN)
    answer = (
        f"Under {context.strip()} banks must report. "
        "A second source [[c:BPRD-C-99-2099]] does not exist, "
        f"and the id {REAL_ID} was invented. "
        "The schedule [[ see annexure ]] lists the rates."
    )
    whole = CitationHandles()
    whole.to_handles(REAL_TOKEN)

    for size in (1, 4, 11, 40):
        streamed_handles = CitationHandles()
        streamed_handles.to_handles(REAL_TOKEN)
        expander = StreamExpander(streamed_handles)
        chunks = [answer[index : index + size] for index in range(0, len(answer), size)]
        streamed = "".join(expander.feed(chunk) for chunk in chunks) + expander.close()

        assert streamed == whole.expand(answer), f"at chunk size {size}"


def test_replayed_history_carries_handles_not_ids():
    """Answers persist with real tokens; the routes replay those rows verbatim."""
    handles = CitationHandles()
    stored = [
        {"role": "user", "content": "What does it require?"},
        {"role": "assistant", "content": f"Under {REAL_TOKEN} banks must report."},
        {"role": "user", "content": "And the deadline?"},
    ]

    replayed = _messages_with_handles(stored, handles)

    assert REAL_ID not in json.dumps(replayed)
    assert handles.expand(replayed[1]["content"]) == f"Under {REAL_TOKEN} banks must report."
    assert replayed[0] == stored[0]  # the user's own words are untouched


def test_the_prompt_the_model_receives_contains_no_document_ids():
    client = AIClient(AIConfig())
    handles = CitationHandles()
    context = f"Circular: {REAL_TOKEN}\nAttachments:\n- [[attachment:8da4b856-3ab0-5f06-b546-2c4ab629ca8d|C1-Onboarding.pdf]]"

    full = client._chat_full_messages(
        [{"role": "assistant", "content": f"Earlier I cited {REAL_TOKEN}."},
         {"role": "user", "content": "Say more"}],
        context,
        ["some-circular"],
        handles,
    )

    serialized = json.dumps(full)
    assert REAL_ID not in serialized
    assert "8da4b856" not in serialized
    assert "[[c:BPRD-C-05-2020]]" in serialized


def test_tool_results_reach_the_model_as_handles(monkeypatch):
    client = AIClient(AIConfig())
    handles = CitationHandles()
    monkeypatch.setattr(
        AIClient,
        "_execute_tool",
        lambda *args, **kwargs: json.dumps({"results": [{"citation": REAL_TOKEN}]}),
    )
    full_messages: list[dict] = [{"role": "user", "content": "find it"}]

    client._apply_tool_calls(
        full_messages,
        "",
        [{"id": "call-1", "type": "function",
          "function": {"name": "search_circulars", "arguments": "{}"}}],
        None,
        None,
        handles,
    )

    tool_message = full_messages[-1]["content"]
    assert REAL_ID not in tool_message
    # A slug is [A-Za-z0-9-] precisely so it can be swapped into a JSON payload untouched.
    assert json.loads(tool_message) == {"results": [{"citation": "[[c:BPRD-C-05-2020]]"}]}
    assert handles.expand(tool_message).count(REAL_ID) == 1
