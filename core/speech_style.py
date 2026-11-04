"""
core/speech_style.py — Makes Saturn sound like a person, not a robot.

Two responsibilities:

1. ``clean_for_speech(text)``
   Any reply — markdown, emoji, bullet lists, code fences, bare URLs, symbols —
   is rewritten into flowing spoken sentences *before* it reaches a TTS engine.
   Reading "**bold**" or "- item" or "https://…" aloud is the single biggest
   robot tell, so this runs on every voice path (Gemini Live, Edge-TTS, SAPI).

2. ``resolve_voice_profile(style)``
   Maps a user-chosen persona ("jarvis", "eve", "edith", "classic") to a concrete
   Gemini Live voice + Edge-TTS voice + prosody, so timbre and delivery match the
   personality. The persona is a config knob (``voice_style`` in app_settings),
   changeable without touching code.

The target feel is JARVIS / EVE / EDITH: warm, quick, a little witty, always a
step ahead — never a monotone announcement bot.
"""

from __future__ import annotations

import re
from typing import Any, Dict

# ── Voice profiles ──────────────────────────────────────────────────────────
# Each profile is a persona. tts_speaker/tts_rate map a local engine id to that
# engine's speaker tag and prosody - the 'custom' (distilled/GPU) mouth reads
# these. Kokoro and MMS need neither: their speaker tag is the persona key
# itself (see tts_speaker_for). gemini_voice is the retired Gemini Live preset
# (legacy display only); edge_voice/hindi_edge_voice document the old Edge
# fallback and the Devanagari auto-switch that now lives at the ASR/TTS edges.
# rate/pitch are Edge-TTS prosody strings (e.g. "+3%").
VOICE_PROFILES: Dict[str, Dict[str, Any]] = {
    # The house persona: warm, quick, wise, a touch witty. Saturn is the product
    # identity — gender-neutral, easy wake word, one crisp name.
    "saturn": {
        "label": "Saturn",
        "tts_speaker": {"custom": ""},
        "tts_rate": {"custom": "+3%"},
        "gemini_voice": "Puck",
        "edge_voice": "en-IN-NeelNeural",
        "hindi_edge_voice": "hi-IN-MadhurNeural",
        "rate": "+3%",
        "pitch": "+0Hz",
        "gender": "male",
        "timbre": "warm, quick",
    },
    # Warm, dry, butler-sharp. British male, close to JARVIS.
    "jarvis": {
        "label": "Jarvis",
        "tts_speaker": {"custom": ""},
        "tts_rate": {"custom": "+4%"},
        "gemini_voice": "Puck",
        "edge_voice": "en-GB-RyanNeural",
        "hindi_edge_voice": "hi-IN-MadhurNeural",
        "rate": "+4%",
        "pitch": "+0Hz",
        "gender": "male",
        "timbre": "dry, British-style",
    },
    # Bright, playful female energy (Spider-Man's EVE).
    "eve": {
        "label": "Eve",
        "tts_speaker": {"custom": ""},
        "tts_rate": {"custom": "+5%"},
        "gemini_voice": "Aoede",
        "edge_voice": "en-US-AvaMultilingualNeural",
        "hindi_edge_voice": "hi-IN-SwaraNeural",
        "rate": "+5%",
        "pitch": "+2Hz",
        "gender": "female",
        "timbre": "bright, friendly",
    },
    # Calm, precise, protective (Far From Home's EDITH).
    "edith": {
        "label": "Edith",
        "tts_speaker": {"custom": ""},
        "tts_rate": {"custom": "+3%"},
        "gemini_voice": "Kore",
        "edge_voice": "en-US-AriaNeural",
        "hindi_edge_voice": "hi-IN-SwaraNeural",
        "rate": "+3%",
        "pitch": "+0Hz",
        "gender": "female",
        "timbre": "calm, precise",
    },
    # Legacy behaviour, kept as an opt-in.
    "classic": {
        "label": "Classic",
        "tts_speaker": {"custom": ""},
        "tts_rate": {"custom": "+0%"},
        "gemini_voice": "Charon",
        "edge_voice": "en-US-GuyNeural",
        "hindi_edge_voice": "hi-IN-MadhurNeural",
        "rate": "+0%",
        "pitch": "+0Hz",
        "gender": "male",
        "timbre": "deep",
    },
}

DEFAULT_STYLE = "saturn"


# Engines whose `speaker_map` is keyed by the persona itself (voice family +
# pace, see core/tts/kokoro.py and core/tts/mms.py). They are handed the persona
# id verbatim; the remaining engines take an explicit tag from the profile table.
PERSONA_SPEAKER_ENGINES = frozenset({"kokoro", "mms-vits"})


def _style_key(style: str | None = None) -> str:
    """The persona key for ``style``, reading the configured one when None.

    Unknown or missing values fall back to the default persona - never crash."""
    if style is None:
        try:
            from memory import config_manager
            style = config_manager.get_voice_style()
        except Exception:
            style = None
    key = (style or "").strip().lower()
    return key if key in VOICE_PROFILES else DEFAULT_STYLE


def resolve_voice_profile(style: str | None = None) -> Dict[str, str]:
    """Return the voice profile dict for ``style``."""
    return VOICE_PROFILES[_style_key(style)]


def gemini_voice_name(style: str | None = None) -> str:
    return resolve_voice_profile(style)["gemini_voice"]


def tts_speaker_for(engine_id: str, style: str | None = None) -> str:
    """Persona -> the local TTS engine's speaker tag ('' = engine default).

    Kokoro and MMS have no per-persona soundstream id: their `speaker_map` is
    keyed by the persona itself (voice family + pace), so the persona name IS the
    speaker tag. Passing a legacy 'male'/'female' tag instead would silently
    drop both the voice and the pace. The other engines take an explicit tag
    from the profile table, falling back to the empty string for the engine
    default."""
    if engine_id in PERSONA_SPEAKER_ENGINES:
        return _style_key(style)
    mapping = resolve_voice_profile(style).get("tts_speaker") or {}
    return str(mapping.get(engine_id, mapping.get("custom", "")) or "")


def tts_rate_for(engine_id: str, style: str | None = None) -> str:
    mapping = resolve_voice_profile(style).get("tts_rate") or {}
    return str(mapping.get(engine_id, mapping.get("custom", "+0%")) or "+0%")


def current_tts_speaker(engine_id: str) -> str:
    """Speaker for the configured persona, resolved at speak time so a mid-
    conversation persona switch is reflected on the very next sentence."""
    return tts_speaker_for(engine_id)


def is_devanagari(text: str) -> bool:
    return any("\u0900" <= ch <= "\u097f" for ch in (text or ""))


# ── Natural-language persona switching ──────────────────────────────────────
# So the user can just say "speak in a female voice" / "be like Jarvis" and the
# assistant switches persona live, no settings screen.
_FEMALE_WORDS = ("female", "woman", "women", "she ", " her ", "girl", "madam", "madame",
                 "ma'am", "mam", "aunt", "mom", "mother", "sister", "lady", "feminine")
_MALE_WORDS = ("male", "man ", " a man", "boy", " he ", " him ", "sir", "buddy", "bro",
               "uncle", "dad", "father", "brother", "guy", "masculine", "butler")
_NAMED = {
    "saturn": "saturn", "kavi": "saturn", "sage": "saturn", "wise": "saturn", "default": "saturn", "your voice": "saturn", "own voice": "saturn", "normal": "saturn", "usual": "saturn",
    "jarvis": "jarvis", "jervis": "jarvis", "british": "jarvis",
    "eve": "eve", "playful": "eve", "cheerful": "eve", "bright": "eve", "peppy": "eve",
    "edith": "edith", "calm": "edith", "soft": "edith", "gentle": "edith", "precise": "edith", "soothing": "edith",
    "classic": "classic", "charon": "classic", "robot": "classic", "deep": "classic",
}
_STYLE_ORDER = ["saturn", "jarvis", "eve", "edith", "classic"]


def persona_from_request(text: str, current: str | None = None) -> tuple[str, bool]:
    """Map a user's spoken request to a persona key.

    Returns ``(persona_key, matched)``. When nothing clearly matches, cycles to
    the next persona so a bare "change your voice" still does something useful -
    staying within the same gender, because asking for a different delivery is
    not asking to become a man or a woman.
    """
    low = f" {(text or '').strip().lower()} "
    for name, key in _NAMED.items():
        if name in low:
            return key, True
    if any(w in low for w in _FEMALE_WORDS):
        return "eve", True
    if any(w in low for w in _MALE_WORDS):
        return "jarvis", True
    # Ambiguous - advance to a different persona than the current one.
    cur = (current or DEFAULT_STYLE).lower()
    if cur not in _STYLE_ORDER:
        cur = DEFAULT_STYLE
    start = _STYLE_ORDER.index(cur)
    gender = VOICE_PROFILES[cur].get("gender") or ""
    for offset in range(1, len(_STYLE_ORDER)):
        candidate = _STYLE_ORDER[(start + offset) % len(_STYLE_ORDER)]
        if VOICE_PROFILES[candidate].get("gender") == gender:
            return candidate, False
    return _STYLE_ORDER[(start + 1) % len(_STYLE_ORDER)], False


def assistant_name() -> str:
    """The one name the assistant uses, from the identity settings."""
    try:
        from core.identity import identity

        name = str(identity.get_assistant_name() or "").strip()
        if name:
            return name
    except Exception:
        pass
    return "Saturn"


def voice_description(style: str | None = None) -> str:
    """Say what the voice now sounds like, in the assistant's own name.

    The persona keys are jarvis / eve / edith for historical reasons, and those
    used to leak into the confirmation - "this is Jarvis, a male voice" - which
    made the assistant introduce itself under a character name the owner never
    chose. Only the sound changes; the name does not.
    """
    profile = resolve_voice_profile(style)
    gender = str(profile.get("gender") or "new")
    timbre = str(profile.get("timbre") or "").strip()
    lead = f"{timbre} {gender}" if timbre else gender
    return f"I'm {assistant_name()}, in a {lead} voice"


# ── Live-session reconnect hook ───────────────────────────────────────────────
# The Gemini Live duplex voice is fixed at connect time, so switching persona
# mid-session needs a reconnect. main.py registers a callback here at startup;
# the set_voice action fires it. Decoupled so this module imports nothing heavy.
_reconnect_callback = None


def set_reconnect_callback(cb) -> None:
    global _reconnect_callback
    _reconnect_callback = cb


def notify_voice_changed() -> None:
    if _reconnect_callback is not None:
        try:
            _reconnect_callback()
        except Exception:
            pass


# ── Text cleanup ──────────────────────────────────────────────────────────────

# Emoji / pictograph ranges. Deliberately excludes Devanagari so Hindi survives.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, transport, supplemental
    "\U00002600-\U000027BF"   # misc symbols + dingbats
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flags)
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U00002190-\U000021FF"   # arrows
    "\U0000200D"              # zero-width joiner
    "\U0000200B-\U0000200F"
    "️"                      # stray variation selector-16
    "]+",
    flags=re.UNICODE,
)

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_BARE_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MD_HEADER_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MD_EMPH_RE = re.compile(r"(\*{1,3}|_{2,3}|~{2})")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_NUMBERED_RE = re.compile(r"^\s*\d{1,2}[.)]\s+", re.MULTILINE)
_TABLE_PIPE_RE = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_TABLE_SEP_RE = re.compile(r"^\s*:?-{2,}:?.*$", re.MULTILINE)


def _join_list_items(text: str) -> str:
    """Turn a run of bullet/numbered lines into one flowing spoken sentence.

    "- milk\n- eggs\n- bread" -> "milk, eggs, and bread".
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    items = [ln for ln in lines if re.match(r"^(?:[-*•]|\d{1,2}[.)])\s+", ln)]
    if len(items) < 2 or len(items) != len(lines):
        return text  # not a clean list — leave it alone
    cleaned = [re.sub(r"^(?:[-*•]|\d{1,2}[.)])\s+", "", ln).strip(" .;") for ln in items]
    cleaned = [c for c in cleaned if c]
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _expand_symbols(text: str) -> str:
    """Say symbols as words so TTS doesn't skip or garble them."""
    subs = [
        (r"°C", " degrees Celsius"),
        (r"°F", " degrees Fahrenheit"),
        (r"(\d)\s?°", r"\1 degrees"),
        (r"(\d)%", r"\1 percent"),
        (r"\band\b", " and "),  # normalize spacing around &
        (r"&", " and "),
        (r"(\d)\s?/\s?(?:hr|hour|hour)", r"\1 per hour"),
        (r"\bkm/h\b", " kilometers per hour"),
        (r"\bmph\b", " miles per hour"),
        (r"\bGB\b", " gigabytes"),
        (r"\bMB\b", " megabytes"),
        (r"\bRAM\b", " ram"),
        (r"\bCPU\b", " cpu"),
        (r"\bWi-?Fi\b", " wifi"),
        (r"\bvs\.?\b", " versus"),
        (r"\betc\.?", " and so on"),
        (r"(\d)(am|pm)", r"\1 \2"),
        (r"\$([\d.]+)", r"\1 dollars"),
    ]
    out = text
    for pat, rep in subs:
        out = re.sub(pat, rep, out, flags=re.IGNORECASE)
    return out


def clean_for_speech(text: str) -> str:
    """Rewrite any reply into natural spoken prose. Safe on plain sentences."""
    if not text:
        return ""
    out = text

    # Drop code blocks entirely (they were never meant to be spoken).
    out = _CODE_FENCE_RE.sub(" the code", out)
    out = _INLINE_CODE_RE.sub(r"\1", out)
    # Links and URLs become plain words.
    out = _MD_LINK_RE.sub(r"\1", out)
    out = _BARE_URL_RE.sub("a web link", out)
    # Tables: strip pipes and separator rows.
    out = _TABLE_SEP_RE.sub("", out)
    out = _TABLE_PIPE_RE.sub(lambda m: m.group(0).replace("|", ", ").strip(", "), out)
    # Headers and emphasis markers.
    out = _MD_HEADER_RE.sub("", out)
    out = _MD_EMPH_RE.sub("", out)

    # Lists: if the entire reply is a list, fold it into one spoken sentence;
    # otherwise just drop the bullet/number markers and let the lines join below.
    body_lines = [ln for ln in out.splitlines() if ln.strip()]
    is_pure_list = bool(body_lines) and all(
        re.match(r"^(?:[-*\u2022]|\d{1,2}[.)])\s+", ln) for ln in body_lines
    )
    if is_pure_list:
        out = _join_list_items(out)
    else:
        out = _BULLET_RE.sub("", out)
        out = _NUMBERED_RE.sub("", out)

    # Emojis out, symbols spoken as words.
    out = _EMOJI_RE.sub("", out)
    out = _expand_symbols(out)

    # Squash whitespace and stray punctuation from all the stripping.
    out = re.sub(r"[ \t]+", " ", out)
    # Join the remaining lines into flowing speech: a line that already ends a
    # sentence runs straight into the next; a fragment (list item, label) is
    # separated with a comma. A trailing colon just introduces the next line.
    out = re.sub(r":\s*\n", "\n", out)
    pieces = [ln.strip() for ln in out.splitlines() if ln.strip()]
    joined = ""
    for piece in pieces:
        if not joined:
            joined = piece
        elif joined[-1] in ".!?":
            joined += " " + piece
        else:
            joined += ", " + piece
    out = joined
    out = re.sub(r",\s*,", ", ", out)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = out.strip(" ,;:")

    # Guarantee terminal punctuation so intonation lands naturally.
    if out and out[-1] not in ".!?":
        out += "."
    return out
