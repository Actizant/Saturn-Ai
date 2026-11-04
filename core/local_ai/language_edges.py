"""
core/local_ai/language_edges.py - Multilingual handling at the edges, not the brain.

The brain is a single model (Qwen2.5-1.5B-Instruct, CPU GGUF - see
core/local_ai/gguf_brain.py). We do NOT hot-swap it per language: that would cost
a reload of the GGUF every time the user flips languages. Instead language is
handled at two edges:

  1. Ear  - the ASR engine returns {transcript, language} every turn
            (faster-whisper large-v3 has native language ID for ~99 languages).
  2. Brain - `apply_reply_language()` appends one directive line to the system
            block so the brain answers in the detected language.

There used to be a third edge - a transient NLLB-200 translation "gloss" hop for
long-tail languages outside the brain's dependable set. It was removed: on a 16 GB
laptop the ~2.7 GB gloss model was the single biggest source of the load-time RAM
squeeze (and the paging stutter that follows), for a feature that only mattered
when speaking non-English. English (and whatever the brain handles natively) is
unaffected; a non-English turn now simply goes straight to the brain with the
reply-in-language directive below.

`reply_language=None` means "change nothing" - every function here is a no-op
on the byte path, which is what the regression gate in the plan requires.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("local_ai.language_edges")

# Spoken-name -> ISO code, for ASR engines that return names instead of codes.
_LANGUAGE_NAMES = {
    "english": "en", "hindi": "hi", "hinglish": "hi", "french": "fr",
    "spanish": "es", "german": "de", "italian": "it", "portuguese": "pt",
    "dutch": "nl", "polish": "pl", "russian": "ru", "turkish": "tr",
    "swedish": "sv", "indonesian": "id", "vietnamese": "vi",
    "chinese": "zh", "mandarin": "zh", "japanese": "ja", "korean": "ko",
    "tamil": "ta", "telugu": "te", "bengali": "bn", "arabic": "ar",
    "thai": "th", "malayalam": "ml", "kannada": "kn", "marathi": "mr",
    "gujarati": "gu", "punjabi": "pa", "urdu": "ur", "farsi": "fa",
    "persian": "fa", "hebrew": "he", "ukrainian": "uk", "romanian": "ro",
    "greek": "el", "czech": "cs", "danish": "da", "finnish": "fi",
    "norwegian": "no", "hungarian": "hu", "burmese": "my", "khmer": "km",
    "lao": "lo", "tagalog": "tl", "filipino": "tl", "swahili": "sw",
    "amharic": "am", "hausa": "ha", "yoruba": "yo", "zulu": "zu",
    "nepali": "ne", "sinhala": "si", "azeerbaijani": "az", "azerbaijani": "az",
}

REPLY_LANGUAGE_DIRECTIVE = (
    "Reply ONLY in {language}; keep code, tool names and paths in English."
)


def normalize_language(tag: str) -> str:
    """Map any ASR/language hint ('Hindi', 'hin', 'hi-IN', 'hi_Deva') to a short
    ISO code. Unknown input is lowercased and returned as-is so callers can
    still log it honestly."""
    if not tag:
        return ""
    t = str(tag).strip()
    low = t.lower()
    if low in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[low]
    # "hi-IN", "hi_Deva", "hin" -> leading short code when it is recognisable
    head = low.replace("_", "-").split("-")[0]
    if head in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[head]
    if len(head) == 2 and head.isalpha():
        return head
    # three-letter ISO (hin -> hi) when it maps to a known name
    for name, code in _LANGUAGE_NAMES.items():
        if name.startswith(head):
            return code
    return head or low


def language_display_name(tag: str) -> str:
    """Human-readable name for the directive line ('Hindi' from 'hi')."""
    code = normalize_language(tag)
    for name, c in _LANGUAGE_NAMES.items():
        if c == code:
            return name.capitalize()
    return code


def apply_reply_language(system: str, reply_language: Optional[str]) -> str:
    """Append the reply-language directive to a system block.

    reply_language None/empty -> byte-identical return (regression gate)."""
    if not reply_language:
        return system
    directive = REPLY_LANGUAGE_DIRECTIVE.format(language=language_display_name(reply_language))
    base = (system or "").rstrip()
    return f"{base}\n{directive}" if base else directive
