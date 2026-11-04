"""
core/reasoning_filter.py - Decide what of a model's reply is worth saying aloud.

Two kinds of leak reach the voice path:

1. A reasoning trace in its own channel. When a model and server both support
   it, llama.cpp splits the reply so the thinking lands on
   ``message.reasoning_content`` and only the answer stays in ``message.content``.
   LFM3-Omni-2B Q4 does NOT do this: it declares no reasoning capability, so
   ``--reasoning on`` is inert and the model writes its working steps straight
   into the answer - measured at 141 tokens of arithmetic before the one line
   that mattered. That is what ``extract_final_answer`` handles.
2. A trace wrapped in tags, arriving as raw text inside the answer - from an
   older server, a ``--reasoning-format none`` fallback, or a model that emits
   the tags mid-answer. That is what ``strip_reasoning_trace`` handles.
3. A trace that is PREFILLED, so the reply begins inside the thinking block and
   only the CLOSING tag ever appears. This is what llama.cpp does with
   ``--reasoning on`` when the template opens the assistant's thinking channel for
   it, and it is the documented behaviour of the DeepSeek-R1-distill family. A
   block matcher keyed on an opening delimiter sees nothing and speaks the whole
   trace, so ``strip_reasoning_trace`` also handles an unmatched closing tag.

Tag shapes differ per model family, so all of them are matched. The literal
delimiters are assembled from character parts below rather than written inline,
so they survive whatever edits or renders this source file.

An unterminated opening tag is treated as "thinking still in progress", so
everything after it is dropped - that matters while streaming, before the
closing tag has arrived.
"""

from __future__ import annotations

import re
from typing import Any, Optional, Tuple

_LT = "<"
_GT = ">"

# Each entry: (open_tag, close_tag). Built from parts so the source stays readable.
_THINK = _LT + "think" + _GT
_THINK_SLASH = _LT + "/think" + _GT
_THINKING = _LT + "thinking" + _GT
_THINKING_SLASH = _LT + "/thinking" + _GT
# LFM3-Omni's template delimiters (assistant thinking channel).
_OMNI_OPEN = _LT + "think" + _GT
_OMNI_CLOSE = _LT + "/think" + _GT

# LFM3-Omni's template delimiters are the same pair as the generic think tags,
# so this tuple is deduplicated - a repeated pair would otherwise make the depth
# scanner below count one opener twice and mask a real orphan close.
_TAG_PAIRS: Tuple[Tuple[str, str], ...] = tuple(dict.fromkeys((
    (_OMNI_OPEN, _OMNI_CLOSE),
    (_THINK, _THINK_SLASH),
    (_THINKING, _THINKING_SLASH),
)))

# DeepSeek-R1 returns its trace tags wrapped in markdown code fences, so the
# fenced form has to be matched too or the block silently survives stripping.
_BACKTICK = "`"


def _fenced(tag: str) -> str:
    return _BACKTICK * 3 + tag


def _pattern_for(open_tag: str, close_tag: str) -> re.Pattern:
    """Match a complete fenced-or-plain block, or a dangling open tag."""
    alts = []
    for o, c in ((open_tag, close_tag), (_fenced(open_tag), _fenced(close_tag))):
        alts.append(re.escape(o) + r".*?" + re.escape(c))
        # Dangling open tag: swallow to end of text (streaming, unclosed trace).
        alts.append(re.escape(o) + r".*\Z")
    return re.compile("|".join(alts), re.DOTALL)


_COMPILED: Tuple[re.Pattern, ...] = tuple(
    _pattern_for(o, c) for o, c in _TAG_PAIRS
)


# Markers the local-mode prompt uses to separate worked steps from the line to
# say aloud. Matched case-insensitively. Ordered longest-first: the selection
# below keys on the last index found, and where two markers overlap inside one
# phrase ("final answer:") both cuts land on the same boundary, so the longer
# spelling can never leave a dangling word in front of the answer.
_ANSWER_MARKERS: Tuple[str, ...] = ("final answer:", "final:", "answer:")


def _first_orphan_close(text: str, open_tag: str, close_tag: str) -> Optional[Tuple[int, int]]:
    """Locate the first closing tag that has no opener in front of it.

    Returns ``(index, length)`` of that closing tag, or None if every close is
    balanced by an open before it. A prefill-style reply - the trace was opened
    by the template, so only the close ever reaches us - lands on the first
    branch, and the whole prefix up to and including that tag is the hidden
    reasoning.

    Only the plain (unfolded) delimiters are scanned, deliberately: the fenced
    form contains the plain tag as a substring, so counting both would double
    the depth and let a genuinely orphan close look balanced. Fenced blocks are
    already handled by the pattern pass in ``strip_reasoning_trace``.
    """
    tokens: list = []
    for match in re.finditer(re.escape(open_tag), text):
        tokens.append((match.start(), 1))
    for match in re.finditer(re.escape(close_tag), text):
        tokens.append((match.start(), 0))
    depth = 0
    for _pos, is_open in sorted(tokens):
        if is_open:
            depth += 1
        else:
            if depth == 0:
                return _pos, len(close_tag)
            depth -= 1
    return None


def strip_reasoning_trace(text: Optional[str]) -> str:
    """Return ``text`` with every internal reasoning block removed.

    Safe on plain answers: a string with no thinking tags is returned
    unchanged (aside from collapsed blank lines), so callers can apply it
    unconditionally to any provider's output.

    Two shapes are removed: a complete block (or an unterminated opener, which
    means thinking is still running), and - for prefilled reasoners - everything
    from the start of the reply through a closing tag that no opener backs.
    """
    if not text:
        return ""
    cleaned = text
    for pattern in _COMPILED:
        cleaned = pattern.sub("", cleaned)
    # Prefilled traces: repeat, because a reply can carry more than one unmatched
    # close once the prefix before the first one is dropped.
    while True:
        hit = None
        for open_tag, close_tag in _TAG_PAIRS:
            found = _first_orphan_close(cleaned, open_tag, close_tag)
            if found and (hit is None or found[0] < hit[0]):
                hit = found
        if hit is None:
            break
        cleaned = cleaned[hit[0] + hit[1]:]
        # A fenced close leaves its trailing backticks behind once swallowed.
        cleaned = re.sub(r"\A\s*`+", "", cleaned)
    # Collapse the blank runs a removed block leaves behind.
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def extract_final_answer(text: Optional[str]) -> str:
    """Return the part of ``text`` that should actually be spoken.

    LFM3-Omni has no native thinking channel (see notes in core/llama_server.py),
    so when it works a problem out it writes the steps straight into the answer -
    141 measured tokens of arithmetic before the one line that mattered. The
    local-mode prompt therefore asks it to put the sentence to say after a
    ``FINAL:`` marker. When that marker is present, only what follows it is
    returned.

    When it is absent the text is returned untouched. Guessing where a spoken
    answer begins would risk cutting off the answer itself, and a reply that
    loses its conclusion is worse than one that repeats the working. The same
    rule covers a marker with nothing after it, which happens when the model runs
    out of generation budget mid-sentence: whatever preceded the marker is
    spoken rather than silence, and the marker word itself is never spoken.
    """
    if not text:
        return ""
    lowered = text.lower()
    best = -1
    best_len = 0
    for marker in _ANSWER_MARKERS:
        idx = lowered.rfind(marker)
        # The LAST occurrence wins: a model that mentions the marker while
        # reasoning still ends with the real one.
        if idx != -1 and (best == -1 or idx > best):
            best = idx
            best_len = len(marker)
    if best == -1:
        return text.strip()
    tail = text[best + best_len:].strip()
    if not tail:
        return text[:best].strip()
    return tail


def speech_text(text: Optional[str]) -> str:
    """Full spoken-output pipeline: drop reasoning blocks, keep the final answer.

    This is what the voice path should call, in this order - reasoning tags go
    first so a marker buried inside a thinking block cannot win.
    """
    return extract_final_answer(strip_reasoning_trace(text))


def split_reply(message: Any) -> Tuple[str, str]:
    """Extract ``(answer, reasoning)`` from a chat-completion ``message`` dict.

    A server with reasoning parsing enabled publishes the trace on
    ``reasoning_content`` and leaves ``content`` clean. LFM3-Omni-2B never takes
    that route, because the model declares no reasoning channel - the field is
    absent and the working steps arrive inside ``content`` - so both channels are
    read and ``content`` always goes through ``speech_text``.

    Reading both, and filtering regardless, is what keeps this correct across
    models: whichever channel a given GGUF actually uses, the trace is separated
    from the answer and neither is ever lost.
    """
    if not isinstance(message, dict):
        return "", ""
    raw_answer = message.get("content") or ""
    reasoning = (
        message.get("reasoning_content")
        or message.get("reasoning")
        or message.get("thinking")
        or ""
    )
    # The answer must never depend on what the trace looked like.
    answer = speech_text(raw_answer if isinstance(raw_answer, str) else "")
    if not isinstance(reasoning, str):
        reasoning = str(reasoning) if reasoning else ""
    return answer, reasoning
