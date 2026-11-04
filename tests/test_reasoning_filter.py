"""core/reasoning_filter.py: the model's chain-of-thought must never be spoken.

LFM3-Omni-2B Q4 has no native reasoning channel - verified against a live server
with ``--reasoning on`` and ``enable_thinking``/``preserve_thinking`` set, where
``reasoning_content`` stayed empty and replies carried only ``['role',
'content']``. The model therefore writes its worked steps into the answer itself,
which makes the ``FINAL:`` marker seam the primary defence and the
``reasoning_content`` split a forward compatibility path for models that do have
the channel. Both are covered below, plus the last layer: a trace that arrives as
raw tag-wrapped text inside the answer.

Tag literals are assembled from ``chr()`` rather than written inline, so the
delimiters survive whatever does with this source file.
"""

from core.reasoning_filter import (
    extract_final_answer,
    speech_text,
    split_reply,
    strip_reasoning_trace,
)

_LT = chr(60)
_GT = chr(62)
_FENCE = chr(96) * 3


def tag(name: str) -> str:
    return f"{_LT}{name}{_GT}"


def block(name: str, inner: str, fenced: bool = False) -> str:
    """A complete reasoning block, optionally wrapped in a code fence."""
    open_fence = _FENCE if fenced else ""
    close_fence = _FENCE if fenced else ""
    return f"{open_fence}{tag(name)}{inner}{close_fence}{tag('/' + name)}"


# ---------------------------------------------------------------------------
# plain answers must survive untouched
# ---------------------------------------------------------------------------

def test_plain_answer_is_unchanged():
    text = "The capital of France is Paris."
    assert strip_reasoning_trace(text) == text


def test_empty_and_none_are_safe():
    assert strip_reasoning_trace(None) == ""
    assert strip_reasoning_trace("") == ""


# ---------------------------------------------------------------------------
# inline traces are removed, the answer is kept
# ---------------------------------------------------------------------------

def test_think_block_is_removed():
    text = block("think", "user asked arithmetic, 2+2") + "The answer is 4."
    assert strip_reasoning_trace(text) == "The answer is 4."


def test_thinking_block_is_removed():
    text = block("thinking", "step by step") + "Done."
    assert strip_reasoning_trace(text) == "Done."


def test_multiline_trace_is_removed():
    text = block("think", "line one\nline two\nline three") + "\n\nThe real reply."
    assert strip_reasoning_trace(text) == "The real reply."


def test_fenced_deepseek_tags_are_removed():
    """DeepSeek returns its trace tags inside code fences; matching only the bare
    form would let the whole block through silently."""
    text = block("think", "hidden reasoning", fenced=True) + "Visible answer."
    assert strip_reasoning_trace(text) == "Visible answer."


def test_multiple_blocks_are_all_removed():
    text = (
        block("think", "first thought")
        + "Between thoughts. "
        + block("think", "second thought")
        + "Final answer."
    )
    assert strip_reasoning_trace(text) == "Between thoughts. Final answer."


# ---------------------------------------------------------------------------
# streaming: an unclosed trace must not be read aloud
# ---------------------------------------------------------------------------

def test_dangling_open_tag_swallows_the_rest():
    text = tag("think") + "still reasoning and never closes"
    assert strip_reasoning_trace(text) == ""


def test_answer_before_a_dangling_open_tag_survives():
    text = "The answer is 4. " + tag("think") + "begins thinking without closing"
    assert strip_reasoning_trace(text) == "The answer is 4."


# ---------------------------------------------------------------------------
# split_reply: the two-channel read
# ---------------------------------------------------------------------------

def test_split_reply_reads_the_native_reasoning_channel():
    msg = {
        "role": "assistant",
        "content": "Final answer.",
        "reasoning_content": "deep thoughts",
    }
    answer, reasoning = split_reply(msg)
    assert answer == "Final answer."
    assert reasoning == "deep thoughts"


def test_split_reply_filters_a_leaked_trace_from_content():
    msg = {"content": block("think", "leaked trace") + "Clean part."}
    answer, reasoning = split_reply(msg)
    assert answer == "Clean part."


def test_split_reply_handles_a_tool_call_message_with_no_content():
    """A tool-call turn has null content; the answer must be empty, not 'None'."""
    msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]}
    answer, reasoning = split_reply(msg)
    assert answer == ""
    assert reasoning == ""


def test_split_reply_is_safe_on_non_dicts():
    assert split_reply(None) == ("", "")
    assert split_reply("not a message") == ("", "")


# ---------------------------------------------------------------------------
# extract_final_answer: the seam that actually carries this model
#
# These shapes are taken from live replies measured over HTTP, not invented.
# ---------------------------------------------------------------------------

def test_marker_returns_only_the_conclusion():
    text = (
        "The first train covers 60 km in the two-hour gap, so the second train "
        "needs to close that 60 km gap at 90 km/h, which takes 60/90 = 2/3 hour."
        "\n\nFINAL: 2/3 hour after the second train leaves."
    )
    assert extract_final_answer(text) == "2/3 hour after the second train leaves."


def test_text_without_a_marker_is_returned_whole():
    """Guessing where a spoken answer starts can cut the conclusion off, so an
    unmarked reply must pass through untouched rather than be truncated."""
    text = "12 pencils cost 8 dollars."
    assert extract_final_answer(text) == text


def test_marker_is_matched_case_insensitively():
    assert extract_final_answer("steps\nfinal: 15") == "15"
    assert extract_final_answer("steps\nFinal: 15") == "15"


def test_last_marker_wins():
    """A model that names the marker while reasoning must still end at its real
    conclusion, not at the mention."""
    text = "I should write FINAL: somewhere. FINAL: the answer is 15."
    assert extract_final_answer(text) == "the answer is 15."


def test_empty_after_the_marker_does_not_become_silence():
    """A marker with no conclusion means the model ran out of budget; speaking what
    came before beats saying nothing - and beats reading the word FINAL aloud."""
    assert extract_final_answer("The answer is 4.\nFINAL:") == "The answer is 4."


def test_overlapping_markers_cut_at_the_same_boundary():
    """'final answer:' also contains 'answer:', and both spellings end at the same
    offset, so neither can leave a dangling fragment in front of the answer."""
    assert extract_final_answer("steps\nFINAL ANSWER: 15 sheep") == "15 sheep"
    assert extract_final_answer("steps\nANSWER: 15 sheep") == "15 sheep"


def test_none_and_empty_are_safe():
    assert extract_final_answer(None) == ""
    assert extract_final_answer("") == ""


# ---------------------------------------------------------------------------
# prefilled traces: only a closing tag ever arrives
#
# llama.cpp with --reasoning on opens the assistant's thinking channel inside the
# template, so the reply reaches us already mid-trace with no opening delimiter
# to match. Without this rule the whole chain of thought gets read aloud.
# ---------------------------------------------------------------------------

def test_prefilled_trace_is_swallowed_up_to_the_close():
    text = (
        "Hmm, wait - the farmer had 12, sells 5 so 7, then buys 8 more, "
        "that is 15. Let me double check: 12-5=7, 7+8=15."
        + tag("/think")
        + " The farmer has 15 sheep."
    )
    assert strip_reasoning_trace(text) == "The farmer has 15 sheep."


def test_prefilled_fenced_trace_leaves_no_backticks():
    """The DeepSeek-style fence wraps the trace, and only the closing fence and
    tag arrive. Swallowing the tag must not strand its trailing backticks in the
    spoken line."""
    text = "long private trace" + _FENCE + tag("/think") + " Real answer."
    assert strip_reasoning_trace(text) == "Real answer."


def test_matched_open_tag_protects_the_prefix():
    """The balancing rule, not the position of the close, decides this: a real
    opener before the close means the text ahead of it is genuine answer, not a
    prefilled trace, so only the block itself may be dropped.

    Spacing is compared after normalisation on purpose. Removing a block that sat
    mid-sentence leaves the two flanking spaces adjacent, and collapsing runs of
    spaces in the implementation would just as happily flatten the indentation of
    a code sample in an answer - so the contract is the surviving content, not
    byte-exact whitespace.
    """
    text = "The answer is 4. " + block("think", "a thought") + " Done."
    cleaned = strip_reasoning_trace(text)
    assert "a thought" not in cleaned
    assert " ".join(cleaned.split()) == "The answer is 4. Done."


def test_repeated_orphan_closes_each_drop_a_prefix():
    """A second unmatched close means the trace resumed after a stray answer
    fragment; each one swallows from the start again."""
    text = ("first stretch of reasoning" + tag("/think")
            + "second stretch" + tag("/think") + " the real reply")
    assert strip_reasoning_trace(text) == "the real reply"


def test_plain_text_without_any_tags_is_untouched():
    text = "Opening Notepad now, sir."
    assert strip_reasoning_trace(text) == text


def test_prefill_seam_and_marker_seam_compose_in_the_right_order():
    """A prefilled trace that itself contains a FINAL: must not have that marker
    treated as the conclusion - the trace is stripped first, so the reply's own
    marker wins."""
    text = (
        "I think the answer is nine." + tag("/think")
        + "Working: 12 minus 5 is 7, plus 8 is 15.\nFINAL: 15 sheep"
    )
    assert speech_text(text) == "15 sheep"


def test_split_reply_carries_a_prefilled_trace():
    msg = {
        "role": "assistant",
        "content": "reasoning reasoning" + tag("/think") + " 15 sheep",
    }
    answer, _reasoning = split_reply(msg)
    assert answer == "15 sheep"


# ---------------------------------------------------------------------------
# speech_text: ordering matters - tags stripped before the marker is sought
# ---------------------------------------------------------------------------

def test_marker_inside_a_thinking_block_cannot_win():
    """If the trace were searched first, a 'FINAL:' the model wrote inside its own
    reasoning would be taken as the answer and the real reply dropped."""
    text = (
        block("think", "I will say FINAL: nine at the end")
        + "The result is 15.\nFINAL: 15"
    )
    assert speech_text(text) == "15"


def test_speech_text_removes_tags_and_keeps_the_conclusion():
    text = block("think", "working it out") + "Steps.\nFINAL: 15 sheep"
    assert speech_text(text) == "15 sheep"


def test_split_reply_uses_the_marker_seam_on_this_model():
    """The shape a live LFM3-Omni reply actually has: no reasoning_content."""
    msg = {
        "role": "assistant",
        "content": "12 minus 5 is 7, plus 8 is 15.\n\nFINAL: 15",
    }
    answer, reasoning = split_reply(msg)
    assert answer == "15"
    assert reasoning == ""
