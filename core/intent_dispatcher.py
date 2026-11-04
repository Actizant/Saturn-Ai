"""
core/intent_dispatcher.py
=========================
Intelligent 3-Tier Semantic Intent Dispatcher for Saturn.

Architecture:
- Tier 1: Instant Local Reflex (< 2ms)
  Zero-latency regex & alias resolution for common hardware controls (volume, media,
  direct local app launches, explicit phone toggles).
- Tier 2: Learned Phrase Cache (< 1ms)
  In-memory + persisted JSON cache of previously interpreted conversational phrases.
- Tier 3: Semantic AI Classifier (~200-400ms)
  Single-turn fast LLM classification using Gemini Flash to interpret indirect,
  conversational, or complex commands into structured executable actions.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from core.user_paths import get_user_data_dir

logger = logging.getLogger("intent_dispatcher")
logger.setLevel(logging.INFO)


@dataclass
class IntentResult:
    tier: int  # 1 = Local Reflex, 2 = Phrase Cache, 3 = Semantic AI
    intent: str  # e.g., "mobile_connect", "developer_agent", "presentation", "spreadsheet", "open_app", "media_control", "general_conversation"
    action: str  # e.g., "launch_app", "flashlight_on", "create_presentation", "volume_up", etc.
    target: str = "pc"  # "pc" | "phone"
    parameters: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    voice_ack: str = ""
    latency_ms: float = 0.0


# =============================================================================
# WHAT A SENTENCE IS ASKING FOR: THE DOCUMENT AND SOFTWARE DETECTORS
# =============================================================================
#
# These predicates used to live in main.py, *below* the router: a presentation
# request was only ever recognised when the semantic tier happened to be on, so
# the same words built a deck at the keyboard and did nothing at all from the
# phone, from Instagram, or during a live duplex session (all three turn Tier 3
# off). The patterns are unchanged; what moved is where they are asked, and the
# answer is now a Tier-1 intent with no cloud round trip.

# "Build it on my phone" is a phone command, never a PC build. The two original
# copies of this list disagreed, and both only knew the bare forms, so "build a
# react app on my android tablet" slipped through as a desktop project. The
# possessive forms are spelled out for that reason.
_PHONE_MARKERS = (
    "on phone", "in phone", "on the phone",
    "on my phone", "in my phone",
    "on mobile", "on my mobile",
    "on android", "on my android",
    "on iphone", "on my iphone",
    "on tablet", "on my tablet", "on the tablet",
    "on my cell", "on my device",
)


def _looks_like_code_request(text: str) -> bool:
    low = (text or "").lower().strip()
    if not low:
        return False
    # If explicitly targeting a mobile device, never treat as a local code
    # creation request.
    if any(m in low for m in _PHONE_MARKERS):
        return False
    # Action/execution commands targeting local apps or utilities are not code
    # creation.
    if any(low.startswith(p) for p in ("open ", "launch ", "start ", "run ", "close ", "kill ", "quit ", "turn on ", "turn off ", "switch on ", "switch off ", "ring ", "find ")):
        if not any(re.search(rf"\b{a}\b", low) for a in ("write", "create", "build", "code", "develop", "implement", "scaffold", "generate")):
            return False

    code_intent_verbs = (
        "build", "create", "make", "code", "develop", "program",
        "implement", "write", "scaffold", "generate", "refactor", "debug"
    )
    code_target_nouns = (
        "app", "application", "website", "web page", "webpage", "web app",
        "webapp", "script", "program", "game", "calculator", "bot",
        "api", "server", "service", "module", "component", "frontend",
        "backend", "ui", "landing page", "portfolio"
    )
    tech_markers = (
        "in python", "using python", "python script", "python code",
        "html", "css", "javascript", "react", "typescript", "flask",
        "fastapi", "django", "node.js", "nodejs", "express", "sql"
    )

    has_intent = any(re.search(rf"\b{re.escape(v)}\b", low) for v in code_intent_verbs)
    has_target = any(re.search(rf"\b{re.escape(n)}\b", low) for n in code_target_nouns)
    has_tech = any(re.search(rf"\b{re.escape(t)}\b", low) for t in tech_markers)

    return (has_intent and (has_target or has_tech)) or (has_target and has_tech)


def _looks_like_website_request(text: str) -> bool:
    low = (text or "").lower().strip()
    if not low:
        return False
    if any(m in low for m in _PHONE_MARKERS):
        return False
    if any(low.startswith(p) for p in ("open ", "go to ", "visit ", "browse ", "launch ")):
        if not any(re.search(rf"\b{a}\b", low) for a in ("make", "create", "build", "design", "develop", "code", "generate")):
            return False

    website_words = (
        "website",
        "web site",
        "webpage",
        "web page",
        "landing page",
        "homepage",
        "home page",
        "portfolio",
        "product site",
        "business site",
        "marketing site",
        "web app",
        "frontend",
        "site",
        "html",
        "react",
        "web",
    )
    action_words = ("make", "create", "build", "design", "develop", "generate", "code", "edit", "update", "fix")
    has_web = any(re.search(rf"\b{re.escape(w)}\b", low) for w in website_words)
    has_action = any(re.search(rf"\b{re.escape(a)}\b", low) for a in action_words)
    return has_web and has_action


def _looks_like_presentation_request(text: str) -> bool:
    low = (text or "").lower()
    ppt_keywords = (
        "presentation", "powerpoint", "slideshow", "slides", "slide deck",
        "pitch deck", "deck", "ppt", "pptx"
    )
    action_words = ("make", "create", "build", "design", "develop", "generate", "draft", "prepare")
    has_keyword = any(re.search(rf"\b{re.escape(k)}\b", low) for k in ppt_keywords)
    has_action = any(re.search(rf"\b{re.escape(a)}\b", low) for a in action_words)
    return has_keyword and (has_action or any(k in low for k in ("slide deck", "pitch deck", "powerpoint", "pptx", "ppt")))


def _looks_like_spreadsheet_request(text: str) -> bool:
    low = (text or "").lower()
    sheet_keywords = (
        "spreadsheet", "excel", "sheet", "sheets", "workbook", "xlsx",
        "tracker", "expense tracker", "budget sheet"
    )
    action_words = ("make", "create", "build", "design", "develop", "generate", "draft", "prepare")
    has_keyword = any(re.search(rf"\b{re.escape(k)}\b", low) for k in sheet_keywords)
    has_action = any(re.search(rf"\b{re.escape(a)}\b", low) for a in action_words)
    return has_keyword and (has_action or any(k in low for k in ("spreadsheet", "excel sheet", "expense tracker", "budget sheet", "xlsx")))


# ── Gmail and Calendar, which are only ever *read* here ────────────────────
#
# The handler can read an inbox and list a schedule; it cannot send, reply,
# create or cancel. So a sentence that asks for a write must not be claimed -
# a misheard "check my emails" is an inconvenience, a misheard "send" is not.
_EMAIL_READ_PHRASES = (
    "check email", "check my email", "unread email", "read email",
    "my inbox", "check inbox", "show emails", "check gmail", "my gmail",
    "unread emails", "new emails", "any emails",
)

_EMAIL_WRITE_WORDS = (
    "send", "reply", "forward", "compose", "draft", "write an email",
    "email to", "mail to",
)

_CALENDAR_READ_PHRASES = (
    "my schedule", "what's on my calendar", "what is on my calendar",
    "check calendar", "my meetings", "schedule today", "events today",
    "my calendar", "today's events", "upcoming meetings", "any meetings",
)

_CALENDAR_WRITE_WORDS = (
    "add to", "create a", "create an", "new event", "new meeting",
    "cancel", "delete", "remove", "reschedule", "invite", "book a",
    "set up a", "set a meeting", "move my",
)


def _looks_like_email_check(text: str) -> bool:
    low = (text or "").lower()
    if not low or any(w in low for w in _EMAIL_WRITE_WORDS):
        return False
    return any(p in low for p in _EMAIL_READ_PHRASES)


def _looks_like_calendar_check(text: str) -> bool:
    low = (text or "").lower()
    if not low or any(w in low for w in _CALENDAR_WRITE_WORDS):
        return False
    return any(p in low for p in _CALENDAR_READ_PHRASES)


# The kinds of thing "build me one of these" keeps pointing at.
_PROJECT_WORDS = (
    "app", "website", "web", "program", "script", "project", "game", "calc",
    "html", "react",
)


def _looks_like_software_request(text: str) -> bool:
    """Is this a request to *build software*, rather than to talk about it?

    The website and code detectors overlap without either containing the other
    ("design a website" carries a verb the code detector does not know; "write
    a flask app" carries a technology the website detector does not), so both
    are asked. A verb plus a technical noun is still too generous on its own -
    "make an api call" is about an API, not about writing code - which is why
    the project itself also has to be named.
    """
    if _looks_like_website_request(text):
        return True
    if not _looks_like_code_request(text):
        return False
    low = (text or "").lower()
    return any(re.search(rf"\b{re.escape(w)}\b", low) for w in _PROJECT_WORDS)


# =============================================================================
# WHAT THE MACHINE IS DOING: RAM, POWER, BRIGHTNESS, AND ENDING A PROGRAM
# =============================================================================
#
# These five branches used to live in main.py below the router and end in a bare
# `return`, so they were reached from the keyboard and nowhere else: a phone turn
# or an Instagram turn asking "how much RAM is Chrome using" fell through to the
# model, or to nothing at all. They are three intents rather than one because the
# trust gate sees an intent *name* and never an action. Reading RAM is harmless,
# dimming the screen is one keypress away from where you started, and ending a
# program takes whatever was unsaved inside it with it.

_RAM_PHRASES = (
    "eating my ram", "ram hogs", "memory hogs", "check ram", "ram usage",
    "memory usage", "who is using ram", "how much ram",
)

_CPU_PHRASES = (
    "eating my cpu", "cpu hogs", "check cpu", "cpu usage", "who is using cpu",
    "how much cpu", "processor usage",
)

_BATTERY_PHRASES = (
    "battery health", "check battery", "battery status", "battery percentage",
    "is laptop charging", "how much battery", "battery level", "battery left",
)

_STATUS_PHRASES = (
    "system status", "system diagnostics", "hardware status", "computer vitals",
    "pc diagnostics", "full diagnostics", "system report", "hardware report",
)

# "brightness" alone is enough: the module reads the current level back when no
# number is given, so "what's my brightness" is the same request as "set it to 40"
# minus the number.
_BRIGHTNESS_WORDS = (
    "brightness", "dim screen", "dim the screen", "dim monitor",
    "dim the monitor", "brighten screen", "brighten the screen", "screen dimmer",
    "screen brighter", "darker screen", "brighter screen",
)

_KILL_VERBS = ("kill", "terminate", "end process", "close app")


# =============================================================================
# SATURN'S OWN REPAIR SHOP: THE PATCH LOG, A ROLLBACK, A FIX
# =============================================================================
#
# Four keyword branches that also used to sit below the router in main.py, with
# no intents at all - so "heal yourself" did nothing from the phone. They are two
# intents: reading the patch log, and anything that writes a source file. The
# third write is "trigger test bug", which sounds harmless and is not: the demo
# bug exists to be caught and immediately patched.
#
# Order matters here in a way it did not before. "auto heal status" contains
# "auto heal", and the old ladder tested the repair branch first - so asking for
# a status report patched the code. The report is now asked first.

_SELF_HEAL_REPORT_PHRASES = (
    "patch history", "patch log", "show patches", "auto heal status",
    "self heal status", "self heal report",
)

_SELF_HEAL_ROLLBACK_PHRASES = (
    "undo last patch", "rollback patch", "roll back the patch", "roll back the last patch",
    "revert patch", "undo patch",
)

_SELF_HEAL_TRIGGER_PHRASES = (
    "trigger test bug", "simulate bug", "test bug", "create bug", "simulate error",
    "trigger error", "break test",
)

_SELF_HEAL_REPAIR_PHRASES = (
    "fix that bug", "fix the bug", "heal yourself", "auto heal", "patch yourself",
    "self heal", "fix error", "fix this error",
)


def _looks_like_self_heal(low: str) -> Optional[tuple[str, str]]:
    """Return ``(intent, action)`` for a repair-shop sentence, or None."""
    if any(p in low for p in _SELF_HEAL_REPORT_PHRASES):
        return ("self_heal_report", "status" if "status" in low else "history")
    if any(p in low for p in _SELF_HEAL_ROLLBACK_PHRASES):
        return ("self_heal_repair", "rollback")
    if any(p in low for p in _SELF_HEAL_TRIGGER_PHRASES):
        return ("self_heal_repair", "trigger_bug")
    if any(p in low for p in _SELF_HEAL_REPAIR_PHRASES):
        return ("self_heal_repair", "heal")
    return None


# =============================================================================
# THE RULE BOOK: TEACHING A RULE, READING IT, FORGETTING ONE
# =============================================================================
#
# This was the last thing in the turn pipeline that wrote without asking: a hook
# in main.py watched every sentence, and the first time it heard "always save to
# the desktop" it stored a rule - no intent, no gate, no confirmation. A guest in
# the room could quietly change how Saturn behaves, and nobody said so.
#
# The patterns themselves stay in core/learned_rules.py (`detect_rule`), which
# now only *reports* a lesson. Recognising and performing are separate steps so
# the trust gate can come between them.

_RULE_LIST_PHRASES = (
    "what rules do you know", "what rules have you learned", "show me your rules",
    "show your rules", "list your rules", "list rules", "what have you learned",
    "rule book", "your rules", "my rules",
)

_RULE_FORGET_PREFIXES = (
    "forget the rule", "forget that rule", "forget rule", "forget your rule",
    "delete the rule", "delete rule", "remove the rule", "remove rule",
    "drop the rule", "drop rule",
)


def _looks_like_rule_book(low: str) -> bool:
    return any(p in low for p in _RULE_LIST_PHRASES)


def _looks_like_forget_rule(low: str) -> Optional[dict[str, Any]]:
    """Turn "forget the rule about chrome" into ``{"target": "chrome"}``."""
    for prefix in _RULE_FORGET_PREFIXES:
        if low.startswith(prefix):
            rest = low[len(prefix):].strip()
            rest = re.sub(r"^(about|on|of|that)\s+", "", rest).strip()
            return {"target": rest}
    return None


def _looks_like_lesson(text: str) -> Optional[str]:
    """The rule this sentence is teaching, or None. Writes nothing."""
    try:
        from core.learned_rules import LearnedRulesEngine

        return LearnedRulesEngine.detect_rule(text)
    except Exception as exc:  # a rule book that cannot read must not break the turn
        logger.debug("Lesson detection unavailable: %s", exc)
        return None


def _looks_like_system_status(low: str) -> Optional[dict[str, Any]]:
    """Return the diagnostics parameters for a vitals question, or None."""
    if any(p in low for p in _RAM_PHRASES):
        return {"action": "ram_hogs"}
    if any(p in low for p in _CPU_PHRASES):
        return {"action": "cpu_hogs"}
    if any(p in low for p in _BATTERY_PHRASES):
        return {"action": "battery"}
    if any(p in low for p in _STATUS_PHRASES):
        return {"action": "status"}
    return None


def _parse_brightness_request(low: str) -> Optional[dict[str, Any]]:
    """Read the number out of a brightness request, or return None.

    A spoken number is absolute ("brightness to 40"); a bare verb is relative
    ("dim it" -> -20). The old ladder sent both at once, so "increase brightness
    to 90" asked for ninety points *more* than the current level.
    """
    if not any(w in low for w in _BRIGHTNESS_WORDS):
        return None

    number = re.search(r"\b(\d{1,3})\b", low)
    if number:
        return {"level": int(number.group(1)), "relative": False}

    words = set(low.split())
    if words & {"dim", "dimmer", "darker", "lower", "decrease", "down", "min"}:
        return {"level": -20, "relative": True}
    if words & {"brighten", "brighter", "increase", "raise", "boost", "up", "max"}:
        return {"level": 20, "relative": True}
    # A question about the current level, not a change.
    return {}


def _parse_kill_request(low: str) -> Optional[dict[str, Any]]:
    """Turn "force kill chrome" into ``{"target": "chrome", "force": True}``.

    The old ladder split on the first space, so "force kill chrome" asked to end
    a process called *kill chrome*. Stripping the modifier first is the whole
    point of parsing this here rather than there.
    """
    text = (low or "").strip()
    words = text.split()
    if not words or len(words) > 4:
        return None

    force = False
    if text.startswith("force "):
        force = True
        text = text[len("force "):].strip()

    for verb in _KILL_VERBS:
        if text == verb:
            return {"target": "", "force": force}
        if text.startswith(verb + " "):
            target = text[len(verb) + 1:].strip()
            target = re.sub(r"^(the|a|an|my)\s+", "", target).strip()
            return {"target": target, "force": force}
    return None


class IntentDispatcher:
    def __init__(self, cache_file: Optional[Path] = None):
        self.cache_dir = get_user_data_dir() / "config"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = cache_file or (self.cache_dir / "intent_cache.json")
        self._cache: dict[str, dict[str, Any]] = {}
        self._load_cache()
        self._gemini_client = None
        self._client_init_attempted = False

    # =========================================================================
    # TIER 2: LEARNED PHRASE CACHE
    # =========================================================================

    def _normalize(self, text: str) -> str:
        t = (text or "").lower().strip()
        t = re.sub(r"[^\w\s]", " ", t)
        t = re.sub(r"\s+", " ", t).strip()
        return t

    def _load_cache(self) -> None:
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception as exc:
                logger.warning(f"Failed to load intent cache: {exc}")
                self._cache = {}

    def _save_cache(self) -> None:
        try:
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            logger.warning(f"Failed to save intent cache: {exc}")

    def get_cached_intent(self, text: str) -> Optional[IntentResult]:
        key = self._normalize(text)
        if not key:
            return None
        cached = self._cache.get(key)
        if cached:
            return IntentResult(
                tier=2,
                intent=cached.get("intent", "general_conversation"),
                action=cached.get("action", ""),
                target=cached.get("target", "pc"),
                parameters=cached.get("parameters", {}),
                confidence=cached.get("confidence", 0.99),
                voice_ack=cached.get("voice_ack", ""),
                latency_ms=0.5,
            )
        return None

    def remember_intent(self, text: str, intent_res: IntentResult) -> None:
        key = self._normalize(text)
        if not key or intent_res.tier < 3:
            return
        self._cache[key] = {
            "intent": intent_res.intent,
            "action": intent_res.action,
            "target": intent_res.target,
            "parameters": intent_res.parameters,
            "confidence": intent_res.confidence,
            "voice_ack": intent_res.voice_ack,
            "cached_at": time.time(),
        }
        self._save_cache()

    # =========================================================================
    # TIER 1: INSTANT LOCAL REFLEX (< 2ms)
    # =========================================================================

    def match_instant_reflex(self, text: str, connected_devices: list[dict[str, Any]] | None = None) -> Optional[IntentResult]:
        t0 = time.perf_counter()
        low = self._normalize(text)
        if not low:
            return None

        # Strip wake-word prefixes
        for pfx in ("hey saturn ", "ok saturn ", "saturn "):
            if low.startswith(pfx):
                low = low[len(pfx):].strip()
                break

        # --- 1. Audio & Media Controls ---
        if low in {"volume up", "increase volume", "louder", "turn it up"}:
            return IntentResult(tier=1, intent="media_control", action="volume_up", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"volume down", "decrease volume", "lower volume", "softer", "turn it down"}:
            return IntentResult(tier=1, intent="media_control", action="volume_down", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"mute", "unmute", "toggle mute", "mute volume", "mute audio"}:
            return IntentResult(tier=1, intent="media_control", action="volume_mute", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"pause", "pause music", "stop music", "pause playback"}:
            return IntentResult(tier=1, intent="media_control", action="media_pause", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"play", "resume", "resume music", "play music", "unpause"}:
            return IntentResult(tier=1, intent="media_control", action="media_play", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"next song", "next track", "skip song", "skip"}:
            return IntentResult(tier=1, intent="media_control", action="media_next", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"previous song", "previous track", "prev song"}:
            return IntentResult(tier=1, intent="media_control", action="media_previous", latency_ms=(time.perf_counter() - t0) * 1000)

        # Volume percentage match e.g. "volume 50" or "volume to 70 percent"
        vol_match = re.match(r"^volume\s+(?:to\s+)?(\d{1,3})(?:\s*percent|%)?$", low)
        if vol_match:
            val = int(vol_match.group(1))
            return IntentResult(tier=1, intent="media_control", action="volume_set", parameters={"value": min(100, max(0, val))}, latency_ms=(time.perf_counter() - t0) * 1000)

        # --- 2. Meeting Mode ---
        if low in {"start meeting mode", "begin meeting mode", "meeting mode"}:
            return IntentResult(tier=1, intent="meeting_mode", action="start", latency_ms=(time.perf_counter() - t0) * 1000)
        if low in {"stop meeting mode", "end meeting mode", "close meeting mode"}:
            return IntentResult(tier=1, intent="meeting_mode", action="stop", latency_ms=(time.perf_counter() - t0) * 1000)

        # --- 3. Direct Mobile Commands (Explicit) ---
        has_mobile_target = any(token in low for token in ("on phone", "on my phone", "on mobile", "on my mobile", "on android", "on tablet"))
        is_flashlight_simple = low in {"flashlight on", "torch on", "turn on flashlight", "turn on torch"}
        is_flashlight_off = low in {"flashlight off", "torch off", "turn off flashlight", "turn off torch"}
        is_ring_phone = low in {"ring phone", "ring my phone", "find my phone", "locate phone"}

        if is_flashlight_simple or ("flashlight" in low and "on" in low and has_mobile_target):
            return IntentResult(tier=1, intent="mobile_connect", action="flashlight_on", target="phone", voice_ack="Flashlight turned on.", latency_ms=(time.perf_counter() - t0) * 1000)
        if is_flashlight_off or ("flashlight" in low and "off" in low and has_mobile_target):
            return IntentResult(tier=1, intent="mobile_connect", action="flashlight_off", target="phone", voice_ack="Flashlight turned off.", latency_ms=(time.perf_counter() - t0) * 1000)
        if is_ring_phone:
            return IntentResult(tier=1, intent="mobile_connect", action="ring_phone", target="phone", voice_ack="Ringing your phone now, sir.", latency_ms=(time.perf_counter() - t0) * 1000)

        if has_mobile_target and any(low.startswith(p) for p in ("open ", "launch ", "start ")):
            app_cand = low
            for p in ("open ", "launch ", "start "):
                if app_cand.startswith(p):
                    app_cand = app_cand[len(p):].strip()
                    break
            app_cand = re.sub(r"\s+(?:on|in|to)\s+(?:my\s+)?(?:phone|mobile|tablet|android|device)\b.*$", "", app_cand).strip()
            app_cand = re.sub(r"\b(?:app|application|the)\b", " ", app_cand).strip()
            if app_cand:
                return IntentResult(tier=1, intent="mobile_connect", action="launch_app", target="phone", parameters={"app_name": app_cand}, voice_ack=f"Opening {app_cand} on your phone, sir.", latency_ms=(time.perf_counter() - t0) * 1000)

        # --- 4. Camera & Screen Vision Perception ---
        cam_words = ("camera", "cam", "webcam", "see me", "look at me", "in front of the camera", "pc cam", "laptop cam")
        has_cam = any(re.search(rf"\b{re.escape(w)}\b", low) for w in cam_words)
        screen_words = ("screen", "display", "monitor")
        has_screen = any(re.search(rf"\b{re.escape(w)}\b", low) for w in screen_words)
        vision_verbs = ("check", "look", "see", "what", "analy", "read", "tell", "inspect", "view", "watch")
        has_vision_verb = any(re.search(rf"\b{re.escape(w)}\b", low) for w in vision_verbs)

        if has_cam and has_vision_verb:
            return IntentResult(
                tier=1,
                intent="vision",
                action="inspect_camera",
                target="pc",
                parameters={"angle": "camera"},
                voice_ack="Checking your camera now, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        if has_screen and has_vision_verb and not any(w in low for w in ("brightness", "dim", "off")):
            return IntentResult(
                tier=1,
                intent="vision",
                action="inspect_screen",
                target="pc",
                parameters={"angle": "screen"},
                voice_ack="Inspecting your screen, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 5. E-Commerce & Shopping Commands ---
        is_open_shopping_link = (
            any(phrase in low for phrase in (
                "open the item link", "open item link", "open the link", "open link in browser",
                "open in browser", "open it in browser", "open product link", "open the product",
                "open top pick", "open the top pick", "open best pick", "open the amazon link",
                "open the flipkart link", "open on amazon", "open on flipkart"
            ))
            or (low in {"open it", "open link", "buy it", "open deal", "open this"})
        )
        if is_open_shopping_link:
            target_platform = None
            if "amazon" in low:
                target_platform = "amazon"
            elif "flipkart" in low:
                target_platform = "flipkart"
            target_device = "phone" if any(w in low for w in ("on phone", "on my phone", "to phone")) else "pc"
            return IntentResult(
                tier=1,
                intent="shopping",
                action="open_item",
                target=target_device,
                parameters={"platform": target_platform, "mode": target_device},
                voice_ack="Opening product link in browser, sir." if target_device == "pc" else "Opening product link on your phone, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        has_store_trigger = any(token in low for token in ("amazon", "flipkart"))
        is_shopping_query = (
            any(w in low for w in ("buy ", "order ", "purchase ", "shopping ", "find price", "compare price", "best deal on"))
            or (has_store_trigger and any(w in low for w in ("search ", "find ", "check price", "look for ", "compare ")))
        )
        if is_shopping_query:
            target_platform = "both"
            if "amazon" in low and "flipkart" not in low:
                target_platform = "amazon"
            elif "flipkart" in low and "amazon" not in low:
                target_platform = "flipkart"

            clean_q = low
            for prefix in ("buy ", "order ", "find me ", "find ", "search for ", "search ", "look for ", "check price of ", "compare prices of ", "compare "):
                if clean_q.startswith(prefix):
                    clean_q = clean_q[len(prefix):].strip()
                    break
            clean_q = re.sub(r"\s+(?:on|from|in)\s+(?:amazon(?:\.in)?|flipkart)(?:\s+(?:and|or)\s+(?:amazon(?:\.in)?|flipkart))?\b.*$", "", clean_q).strip()

            if clean_q:
                return IntentResult(
                    tier=1,
                    intent="shopping",
                    action="search_and_compare",
                    target="pc",
                    parameters={"query": clean_q, "platform": target_platform},
                    voice_ack=f"Scanning {'Amazon and Flipkart' if target_platform == 'both' else target_platform.capitalize()} for {clean_q}, sir.",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )

        # --- 6. FlowState Time Machine (Workspace Snapshots & Restoration) ---
        is_flowstate_list = low in {
            "list workspaces", "list workspace", "list restoration points", "list restoration point",
            "show workspaces", "show my workspaces", "show restoration points", "saved workspaces"
        }
        if is_flowstate_list:
            return IntentResult(
                tier=1,
                intent="flowstate",
                action="list",
                target="pc",
                voice_ack="Checking your saved workspaces, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        snap_match = re.match(
            r"^(?:snapshot|save|create)\s+(?:(?:a|the|this|my)\s+)?(?:current\s+)?(?:workspace|restoration\s+point|current\s+state)(?:\s+(?:as|named|for))?(?:\s+(.*))?$",
            low
        )
        if snap_match:
            raw_name = (snap_match.group(1) or "").strip()
            raw_name = re.sub(r"^['\"]|['\"]$", "", raw_name).strip()
            ws_name = raw_name if raw_name and raw_name.lower() not in {"as", "named", "for"} else "Current Workspace"
            return IntentResult(
                tier=1,
                intent="flowstate",
                action="snapshot",
                target="pc",
                parameters={"name": ws_name},
                voice_ack=f"Snapshotting workspace '{ws_name}' now, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        restore_match = re.match(
            r"^(?:restore|switch\s+to|load|switch)\s+(?:(?:the|my|this)\s+)?(?:workspace|restoration\s+point)(?:\s+(?:to|named))?(?:\s+(.*))?$",
            low
        )
        if restore_match:
            raw_name = (restore_match.group(1) or "").strip()
            raw_name = re.sub(r"^['\"]|['\"]$", "", raw_name).strip()
            ws_name = raw_name if raw_name and raw_name.lower() not in {"to", "named"} else "Main Workspace"
            return IntentResult(
                tier=1,
                intent="flowstate",
                action="restore",
                target="pc",
                parameters={"name": ws_name},
                voice_ack=f"Restoring workspace '{ws_name}', sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 7. StudyGuard & Focus Sentinel (Webcam AI Study Supervisor) ---
        is_sg_active = False
        try:
            from core.study_guard import get_study_guard_manager
            is_sg_active = get_study_guard_manager().is_active
        except Exception:
            pass

        # If study session is active, any general stop/pause/resume directly controls it!
        if is_sg_active:
            if low in {"stop", "stop it", "cancel", "end", "halt", "quit", "abort", "finish", "stop now", "please stop", "shut up"}:
                return IntentResult(
                    tier=1,
                    intent="study_guard",
                    action="stop",
                    target="pc",
                    voice_ack="Stopping study session, sir. Generating performance report...",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )
            if low in {"pause", "pause it", "pause session", "pause now", "hold"}:
                return IntentResult(
                    tier=1,
                    intent="study_guard",
                    action="pause",
                    target="pc",
                    voice_ack="Pausing study session, sir.",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )
            if low in {"resume", "resume it", "continue", "continue session", "unpause"}:
                return IntentResult(
                    tier=1,
                    intent="study_guard",
                    action="resume",
                    target="pc",
                    voice_ack="Resuming study monitor, sir. Stay focused.",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )

        study_stop_phrases = (
            "stop study", "stop studying", "cancel study", "end study", "quit study", "finish study", "halt study",
            "stop study session", "end study session", "finish study session", "cancel study session",
            "stop study mode", "end study mode", "stop study guard", "exit study mode",
            "stop focus session", "end focus session", "stop focus", "end focus",
            "stop the study session", "stop the study", "stop session", "end session", "cancel session",
            "stop pomodoro", "end pomodoro", "cancel pomodoro"
        )
        is_study_stop = any(low == p or low.startswith(p + " ") for p in study_stop_phrases)
        if is_study_stop:
            return IntentResult(
                tier=1,
                intent="study_guard",
                action="stop",
                target="pc",
                voice_ack="Concluding your study session, sir. Generating performance report...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_study_pause = low in {"pause study session", "pause study mode", "pause study", "pause focus session", "pause focus"}
        if is_study_pause:
            return IntentResult(
                tier=1,
                intent="study_guard",
                action="pause",
                target="pc",
                voice_ack="Pausing study monitor, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_study_resume = low in {"resume study session", "resume study mode", "resume study", "continue study session", "continue study", "resume focus"}
        if is_study_resume:
            return IntentResult(
                tier=1,
                intent="study_guard",
                action="resume",
                target="pc",
                voice_ack="Resuming study monitor, sir. Stay focused.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_study_stats = low in {"study stats", "study session stats", "study status", "focus stats", "study report", "study progress"}
        if is_study_stats:
            return IntentResult(
                tier=1,
                intent="study_guard",
                action="status",
                target="pc",
                voice_ack="Checking study session status, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_timer_only = any(p in low for p in ("study timer", "focus timer", "pomodoro timer", "pomodoro", "timer only", "without camera", "no camera", "camera off"))
        is_study_start = (
            any(phrase in low for phrase in (
                "start study session", "start a study session", "start study mode",
                "study mode", "study monitor", "study guard", "start focus session",
                "start studying", "study session", "start study timer", "start focus timer",
                "start pomodoro", "pomodoro session"
            ))
            or bool(re.search(r"\bstudy\s+for\s+\d+\s+min", low))
        )
        if is_study_start:
            duration = 25
            m_dur = re.search(r"(\d+)\s*(?:minutes?|mins?|m)\b", low)
            if m_dur:
                duration = int(m_dur.group(1))

            lofi = any(w in low for w in ("lofi", "lo-fi", "music", "beats"))
            ack_msg = f"Starting Pomodoro focus timer for {duration} minutes, sir." if is_timer_only else f"Activating StudyGuard for {duration} minutes, sir. I'll monitor your focus and keep you awake."
            return IntentResult(
                tier=1,
                intent="study_guard",
                action="start",
                target="pc",
                parameters={"duration": duration, "lofi": lofi, "subject": "Focus Sprint", "timer_only": is_timer_only},
                voice_ack=ack_msg,
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 8. Game Sentinel & Hardware Turbo Boost (Mem Reduct & Power Plan) ---
        is_clean_ram = low in {
            "clean ram", "purge ram", "clear ram", "free ram", "free memory",
            "clean memory", "purge memory", "clear memory", "mem reduct",
            "reduce ram", "optimize ram", "boost ram", "flush ram", "flush memory"
        }
        if is_clean_ram:
            return IntentResult(
                tier=1,
                intent="game_sentinel",
                action="clean_ram",
                target="pc",
                voice_ack="Executing Mem Reduct deep memory purge...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_game_start = low in {
            "game mode", "start game mode", "activate game mode", "enable game mode",
            "turn on game mode", "game mode on", "turbo mode", "turbo boost", "hardware boost",
            "engage game mode", "launch game mode"
        }
        if is_game_start:
            return IntentResult(
                tier=1,
                intent="game_sentinel",
                action="start",
                target="pc",
                voice_ack="Activating Game Mode: Turbo boost engaged, RAM purged, and performance profile active.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_game_stop = low in {
            "stop game mode", "disable game mode", "turn off game mode", "exit game mode",
            "deactivate game mode", "game mode off", "disengage game mode", "end game mode"
        }
        if is_game_stop:
            return IntentResult(
                tier=1,
                intent="game_sentinel",
                action="stop",
                target="pc",
                voice_ack="Disengaging Game Mode. Restoring normal power and notification profile, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_game_status = low in {"game status", "game mode status", "gaming status"}
        if is_game_status:
            return IntentResult(
                tier=1,
                intent="game_sentinel",
                action="status",
                target="pc",
                voice_ack="Checking Game Sentinel telemetry, sir.",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 9. Saturn NetSentinel & Network Bridge ---
        is_connect_devices = (
            low in {
                "connect all devices", "connect to all devices", "connect to all my devices",
                "connect devices", "connect to devices", "link all devices", "link network devices",
                "connect devices via network", "broadcast to all devices", "notify devices",
                "connect network devices", "mesh connect", "connect to network devices",
                "broadcast online", "broadcast online status"
            } or
            bool(re.search(r"\b(?:connect|link|mesh|broadcast|notify)\b.*\b(?:all\s+devices|network\s+devices|my\s+devices|devices\s+on\s+(?:the\s+|my\s+)?network)\b", low))
        )
        if is_connect_devices:
            return IntentResult(
                tier=1,
                intent="net_sentinel",
                action="connect_all_devices",
                target="pc",
                voice_ack="Connecting with all devices across your network and transmitting broadcast beacon...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_net_scan = low in {
            "scan network", "scan wifi", "scan lan", "scan my network", "scan the network",
            "show connected devices", "list connected devices", "who is on my wifi",
            "who is on my network", "network devices", "devices on my network",
            "show devices on wifi", "network map", "scan devices",
            "radar", "open radar", "show radar", "network radar", "scan radar",
            "wifi radar", "lan radar", "open network radar"
        }
        if is_net_scan:
            return IntentResult(
                tier=1,
                intent="net_sentinel",
                action="scan_network",
                target="pc",
                voice_ack="Scanning your local Wi-Fi network and default gateway. Resolving connected devices...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_router_req = low in {
            "inspect router", "search router", "check router", "check my router",
            "router status", "router info", "router audit", "audit router", "router details"
        }
        if is_router_req:
            return IntentResult(
                tier=1,
                intent="net_sentinel",
                action="inspect_router",
                target="pc",
                voice_ack="Inspecting your gateway router, management ports, and network status...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        is_internet_req = low in {
            "internet status", "check internet", "check my internet", "my public ip",
            "what is my public ip", "isp info", "internet health", "test internet connection",
            "network status"
        }
        if is_internet_req:
            return IntentResult(
                tier=1,
                intent="net_sentinel",
                action="internet_status",
                target="pc",
                voice_ack="Checking public internet uplink and ISP health...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # Wake on LAN
        if low.startswith("wake on lan") or low.startswith("wake device") or low.startswith("wake pc"):
            raw_mac = re.sub(r"^(?:wake on lan|wake device|wake pc)\s*", "", low).strip()
            hex_parts = re.findall(r"[0-9a-fA-F]{2}", raw_mac)
            cand_mac = ":".join(p.upper() for p in hex_parts) if len(hex_parts) == 6 else raw_mac
            return IntentResult(
                tier=1,
                intent="net_sentinel",
                action="wake_device",
                target="pc",
                parameters={"target": cand_mac},
                voice_ack="Broadcasting Wake-on-LAN magic packet...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # Ping
        if low.startswith("ping "):
            m_ping = re.search(r"\bping\s+([a-zA-Z0-9\.\_\:\-]+)", text.strip(), re.I)
            if m_ping:
                cand_tgt = m_ping.group(1).strip()
                return IntentResult(
                    tier=1,
                    intent="net_sentinel",
                    action="ping",
                    target="pc",
                    parameters={"target": cand_tgt},
                    voice_ack=f"Pinging {cand_tgt}...",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )

        # --- 10. Space Cleaner / Storage Sentinel ---
        is_space_req = (
            low in {
                "clean disk", "clean space", "free up space", "free space", "storage sentinel",
                "disk cleanup", "space cleaner", "disk cleaner", "purge temp files", "clean junk files",
                "clean junk", "purge disk", "open space cleaner", "open storage sentinel"
            } or (
                bool(re.search(r"\b(?:clean|cleanup|purge|free\s+up|free)\b.*\b(?:disk|storage|disk\s*space|space|junk|temp)\b", low))
                and not any(w in low for w in ("ram", "memory", "cache memory", "standby list"))
            )
        )
        if is_space_req:
            return IntentResult(
                tier=1,
                intent="space_cleaner",
                action="open_hud",
                target="pc",
                voice_ack="Opening Storage Sentinel and analyzing disk sectors, sir...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 11. Direct PC App Launcher (Exact Known App) ---
        from actions.open_app import _APP_ALIASES
        if not has_mobile_target and any(low.startswith(p) for p in ("open ", "launch ", "start ")):
            app_cand = low
            for p in ("open ", "launch ", "start "):
                if app_cand.startswith(p):
                    app_cand = app_cand[len(p):].strip()
                    break
            app_cand = re.sub(r"\b(?:app|the)\b", " ", app_cand).strip()
            if app_cand in _APP_ALIASES:
                return IntentResult(tier=1, intent="open_app", action="open_local_app", target="pc", parameters={"app_name": app_cand}, voice_ack=f"Opening {app_cand}, sir.", latency_ms=(time.perf_counter() - t0) * 1000)

        # --- 12. Reverse Engineering, Decompilation & App Remaking ---
        is_re_verb = any(v in low for v in ("reverse engineer", "decompile", "remake", "replicate", "extract api", "clone"))
        url_match = re.search(r"https?://[^\s\"'>]+", text)
        file_cand_match = re.search(r"([a-zA-Z]:[\\/][^\s\"'>]+|\b[\w\-.]+\.(?:exe|dll|asar|wasm)\b)", text, re.IGNORECASE)

        if is_re_verb and (url_match or file_cand_match or "api surface" in low or "app" in low or "binary" in low or "website" in low):
            target_str = url_match.group(0) if url_match else (file_cand_match.group(0) if file_cand_match else "")
            action_type = "remake" if ("remake" in low or "clone" in low or "replicate" in low) else "analyze"
            if "api" in low and ("surface" in low or "extract" in low or "endpoints" in low):
                action_type = "api_surface"
            elif "decompile" in low or "pseudocode" in low:
                action_type = "decompile"

            return IntentResult(
                tier=1,
                intent="reverse_engineer",
                action=action_type,
                target="pc",
                parameters={"target": target_str or text, "action": action_type},
                voice_ack=f"Initiating reverse engineering on {target_str or 'target'}, sir...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 13. AI Image Generation ---
        is_img_gen = (
            any(low.startswith(pfx) for pfx in (
                "generate an image", "generate image", "create an image", "create image",
                "draw an image", "draw a ", "draw me a ", "draw ",
                "generate a picture", "generate picture", "create a picture", "create picture",
                "generate art", "create art", "make an image", "make a picture", "paint a ", "paint an "
            ))
            or (any(tok in low for tok in ("image of", "picture of", "photo of", "painting of", "wallpaper of", "art of"))
                and any(v in low for v in ("generate", "create", "draw", "make", "paint", "render")))
        )
        if is_img_gen and not any(w in low for w in ("analyze", "what is", "explain this image", "screen", "webcam")):
            clean_prompt = low
            for pfx in (
                "generate an image of ", "generate an image ", "generate image of ", "generate image ",
                "create an image of ", "create an image ", "create image of ", "create image ",
                "generate a picture of ", "generate a picture ", "create a picture of ", "create a picture ",
                "draw me an image of ", "draw me a picture of ", "draw me a ", "draw an image of ",
                "draw a picture of ", "draw a ", "draw ", "make an image of ", "make a picture of ",
                "paint a picture of ", "paint an image of ", "paint a ", "generate art of "
            ):
                if clean_prompt.startswith(pfx):
                    clean_prompt = clean_prompt[len(pfx):].strip()
                    break

            style_cand = "photorealistic"
            for st in ("cyberpunk", "anime", "cinematic_3d", "digital_art", "concept_art", "fantasy", "minimalist", "photorealistic"):
                if st.replace("_", " ") in clean_prompt:
                    style_cand = st
                    break

            return IntentResult(
                tier=1,
                intent="generate_image",
                action="generate",
                target="pc",
                parameters={"prompt": clean_prompt or text, "style": style_cand},
                voice_ack=f"Generating your {style_cand.replace('_', ' ')} image now, sir...",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 14. The machine's own vitals ---
        # No voice_ack on any of these: the diagnostics module speaks its own
        # answer ("Your RAM is at 71 percent..."), and two sentences for one
        # question is a bug we have fixed in four other places already.
        status_params = _looks_like_system_status(low)
        if status_params is not None:
            return IntentResult(
                tier=1,
                intent="system_status",
                action=status_params["action"],
                target="pc",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        brightness_params = _parse_brightness_request(low)
        if brightness_params is not None:
            return IntentResult(
                tier=1,
                intent="brightness_control",
                action="brightness",
                target="pc",
                parameters=brightness_params,
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        kill_params = _parse_kill_request(low)
        if kill_params is not None:
            return IntentResult(
                tier=1,
                intent="process_kill",
                action="kill",
                target="pc",
                parameters=kill_params,
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 15. Saturn's own repair shop ---
        # No voice_ack: the engine narrates the repair itself.
        heal_request = _looks_like_self_heal(low)
        if heal_request is not None:
            return IntentResult(
                tier=1,
                intent=heal_request[0],
                action=heal_request[1],
                target="pc",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 16. The rule book ---
        # Forgetting is asked before teaching: "never forget the rule about X"
        # is a lesson, "forget the rule about X" is not, and the difference is
        # the first word.
        forget_params = _looks_like_forget_rule(low)
        if forget_params is not None:
            return IntentResult(
                tier=1,
                intent="forget_rule",
                action="forget",
                target="pc",
                parameters=forget_params,
                latency_ms=(time.perf_counter() - t0) * 1000
            )
        if _looks_like_rule_book(low):
            return IntentResult(
                tier=1,
                intent="rule_book",
                action="list",
                target="pc",
                latency_ms=(time.perf_counter() - t0) * 1000
            )
        lesson = _looks_like_lesson(text)
        if lesson:
            # No voice_ack: the handler reads the rule back, which is the
            # acknowledgement and the receipt in one sentence.
            return IntentResult(
                tier=1,
                intent="learn_rule",
                action="learn",
                target="pc",
                parameters={"rule": lesson},
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 17. Gmail and Calendar reads ---
        if _looks_like_email_check(text):
            return IntentResult(
                tier=1,
                intent="google_workspace",
                action="check_email",
                target="pc",
                latency_ms=(time.perf_counter() - t0) * 1000
            )
        if _looks_like_calendar_check(text):
            return IntentResult(
                tier=1,
                intent="google_workspace",
                action="check_calendar",
                target="pc",
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        # --- 18. Document factories and software projects ---
        # Nothing more specific below this line, so the broad rules come last.
        # The ack is left to the handler: one sentence, one place.
        if _looks_like_presentation_request(low):
            return IntentResult(
                tier=1,
                intent="presentation",
                action="create_presentation",
                target="pc",
                parameters={"topic": text},
                latency_ms=(time.perf_counter() - t0) * 1000
            )
        if _looks_like_spreadsheet_request(low):
            return IntentResult(
                tier=1,
                intent="spreadsheet",
                action="create_spreadsheet",
                target="pc",
                parameters={"topic": text},
                latency_ms=(time.perf_counter() - t0) * 1000
            )
        if _looks_like_software_request(low):
            return IntentResult(
                tier=1,
                intent="developer_agent",
                action="build_project",
                target="pc",
                parameters={"description": text},
                latency_ms=(time.perf_counter() - t0) * 1000
            )

        return None

    # =========================================================================
    # TIER 1.5: LOCAL SEMANTIC FAST-PATH (< 3ms, ZERO CLOUD LATENCY)
    # =========================================================================

    @staticmethod
    def _text_similarity(s1: str, s2: str) -> float:
        """Calculates combined lexical Jaccard & character 3-gram similarity with containment boost."""
        w1 = set(re.findall(r"\w+", s1.lower()))
        w2 = set(re.findall(r"\w+", s2.lower()))
        if not w1 or not w2:
            return 0.0
        word_sim = len(w1 & w2) / len(w1 | w2)

        def ngrams(s: str, n: int = 3) -> set[str]:
            return {s[i:i+n] for i in range(len(s) - n + 1)} if len(s) >= n else {s}
        g1 = ngrams(s1.lower())
        g2 = ngrams(s2.lower())
        gram_sim = len(g1 & g2) / max(1, len(g1 | g2))

        base_sim = 0.5 * word_sim + 0.5 * gram_sim
        # Containment boost: if an exemplar's words are a subset of the query words
        if w2.issubset(w1) and len(w2) >= 2:
            base_sim = max(base_sim, 0.72)
        elif len(w1 & w2) >= 2 and len(w1 & w2) / len(w2) >= 0.75:
            base_sim = max(base_sim, 0.62)

        return base_sim

    def match_semantic_fastpath(self, text: str) -> Optional[IntentResult]:
        """Matches conversational phrasing against a semantic exemplar bank in < 2ms."""
        t0 = time.perf_counter()
        low = self._normalize(text)
        if len(low) < 3:
            return None

        exemplars: list[tuple[str, str, str, str, dict[str, Any], list[str]]] = [
            (
                "game_sentinel", "activate_game_mode", "pc",
                "Engaging Game Mode and Hardware Turbo Boost, sir.",
                {},
                [
                    "boost my pc", "turbo boost rig", "gaming mode on", "boost gaming performance",
                    "maximum hardware speed", "overclock cpu priority", "engage turbo boost", "turbo mode"
                ]
            ),
            (
                "game_sentinel", "clean_ram", "pc",
                "Purging standby memory and flushing process working sets, sir.",
                {},
                [
                    "clean ram", "purge standby memory", "flush ram cache", "clear system memory",
                    "free up ram", "reduce memory usage", "flush working sets", "clean my ram",
                    "free standby memory", "purge cache memory", "mem reduct", "flush ram"
                ]
            ),
            (
                "net_sentinel", "scan_network", "pc",
                "Scanning local Wi-Fi subnet and identifying connected hardware nodes...",
                {},
                [
                    "scan wifi", "who is on my wifi", "show connected devices", "scan local network",
                    "radar scan devices", "check lan devices", "detect who is on network", "scan subnet",
                    "audit wifi devices", "find devices on my network", "wifi radar"
                ]
            ),
            (
                "net_sentinel", "inspect_router", "pc",
                "Inspecting gateway router, open ports, and admin console...",
                {},
                [
                    "inspect router", "gateway router status", "check router console", "audit home router",
                    "router admin info", "examine my router", "probe default gateway"
                ]
            ),
            (
                "net_sentinel", "internet_status", "pc",
                "Testing public internet uplink, ISP routing, and download speeds...",
                {},
                [
                    "check internet speed", "internet connection health", "test download speed",
                    "check my isp", "public ip address", "internet telemetry", "ping test internet"
                ]
            ),
            (
                "flowstate", "snapshot_workspace", "pc",
                "Capturing active workspace snapshot, open browser tabs, and IDE folders...",
                {"name": "Quick Snapshot"},
                [
                    "save workspace", "snapshot my tabs", "save current desktop session",
                    "backup active apps and tabs", "save my setup", "snapshot workspace", "freeze workspace"
                ]
            ),
            (
                "study_guard", "start_session", "pc",
                "Engaging StudyGuard focus session and silencing distractions...",
                {"duration_minutes": 25},
                [
                    "start study guard", "deep focus mode", "study session start",
                    "pomodoro focus session", "lock in for studying", "start studying now"
                ]
            ),
            (
                "space_cleaner", "open_hud", "pc",
                "Opening Storage Sentinel and analyzing disk sectors, sir...",
                {},
                [
                    "clean disk space", "free up hard drive space", "clean junk files", "disk cleanup utility",
                    "storage sentinel", "purge temporary files", "clean up my storage", "free disk space"
                ]
            ),
        ]

        best_score = 0.0
        best_match = None

        for intent, action, target, voice_ack, params, phrases in exemplars:
            for phrase in phrases:
                sim = self._text_similarity(low, phrase)
                if sim > best_score:
                    best_score = sim
                    best_match = (intent, action, target, voice_ack, params)

        if best_match and best_score >= 0.52:
            intent, action, target, voice_ack, params = best_match
            elapsed = (time.perf_counter() - t0) * 1000
            logger.info(f"Tier 1.5 Semantic Fast-Path matched '{text}' -> {intent}:{action} (score: {best_score:.2f}, {elapsed:.1f}ms)")
            return IntentResult(
                tier=1,
                intent=intent,
                action=action,
                target=target,
                parameters=params,
                confidence=round(best_score, 2),
                voice_ack=voice_ack,
                latency_ms=elapsed
            )

        return None

    # =========================================================================
    # TIER 3: FAST SEMANTIC AI ROUTER (~200ms)
    # =========================================================================

    def _get_llm_client(self):
        if self._client_init_attempted:
            return self._gemini_client
        self._client_init_attempted = True
        try:
            from core.user_paths import get_user_data_dir
            key_file = get_user_data_dir() / "config" / "api_keys.json"
            if not key_file.exists():
                return None
            from core.secret_store import load_json
            data = load_json(key_file)
            api_key = data.get("gemini_api_key", "").strip()
            if not api_key:
                return None
            from google import genai
            self._gemini_client = genai.Client(api_key=api_key)
        except Exception as exc:
            logger.warning(f"Could not initialize Gemini Client for Intent Dispatcher: {exc}")
            self._gemini_client = None
        return self._gemini_client

    def classify_semantic_intent(
        self,
        text: str,
        connected_devices: Optional[list[dict[str, Any]]] = None,
        active_window: str = ""
    ) -> Optional[IntentResult]:
        t0 = time.perf_counter()
        client = self._get_llm_client()
        if not client:
            return None

        device_names = []
        if connected_devices:
            for d in connected_devices:
                name = d.get("name") or d.get("device_id") or "Phone"
                device_names.append(str(name))
        device_context = ", ".join(device_names) if device_names else "None connected"

        system_instruction = (
            "You are the high-speed Semantic Intent Dispatcher for Saturn AI OS.\n"
            "Your job is to analyze the user's voice command and classify it into an executable action.\n\n"
            "Available Intent Categories and Actions:\n"
            "1. 'vision' (Target: 'pc')\n"
            "   Actions: 'inspect_camera' (params: {'angle': 'camera'}), 'inspect_screen' (params: {'angle': 'screen'})\n"
            "   Use this WHENEVER user asks to check, look at, or see through their camera/cam/webcam or inspect what is on their screen.\n"
            "2. 'mobile_connect' (Target: 'phone')\n"
            "   Actions: 'flashlight_on', 'flashlight_off', 'ring_phone', 'launch_app' (params: app_name),\n"
            "   'get_battery', 'open_url' (params: url), 'mobile_autopilot' (params: instruction)\n"
            "3. 'developer_agent' (Target: 'pc')\n"
            "   Actions: 'build_project', 'scaffold_app', 'write_code', 'debug'\n"
            "   Use this ONLY when the user asks to build, code, program, or create software/web apps/scripts.\n"
            "4. 'presentation' (Target: 'pc')\n"
            "   Action: 'create_presentation' (params: topic, slides_count)\n"
            "   Use when user wants slides, PowerPoint, pitch deck, or presentation.\n"
            "5. 'spreadsheet' (Target: 'pc')\n"
            "   Action: 'create_spreadsheet' (params: topic, data_description)\n"
            "   Use when user asks for Excel, spreadsheet, financial model, or tabular workbook.\n"
            "6. 'open_app' (Target: 'pc')\n"
            "   Action: 'open_local_app' (params: app_name)\n"
            "7. 'media_control' (Target: 'pc')\n"
            "   Actions: 'volume_up', 'volume_down', 'volume_set', 'media_play', 'media_pause', 'media_next'\n"
            "8. 'google_workspace' (Target: 'pc')\n"
            "   Actions: 'check_email', 'check_calendar'\n"
            "9. 'shopping' (Target: 'pc' or 'phone')\n"
            "   Actions: 'search_and_compare' (params: {'query': product_name, 'platform': 'both'|'amazon'|'flipkart'}), 'add_to_cart', 'buy_now'\n"
            "   Use when user wants to search, compare prices, find deals, or buy items on Amazon or Flipkart.\n"
            "10. 'flowstate' (Target: 'pc')\n"
            "   Actions: 'snapshot' (params: {'name': workspace_name}), 'restore' (params: {'name': workspace_name}), 'list'\n"
            "   Use when user wants to snapshot, save, restore, or list persistent workspace states/restoration points.\n"
            "11. 'general_conversation' (Target: 'pc')\n"
            "   Use for general questions, greetings, trivia, stock market queries, or requests not fitting above.\n\n"
            "Output Format: Strict raw JSON only with NO markdown fences, matching this structure:\n"
            "{\n"
            "  \"intent\": \"<intent_category>\",\n"
            "  \"action\": \"<action_name>\",\n"
            "  \"target\": \"pc\" | \"phone\",\n"
            "  \"parameters\": {},\n"
            "  \"confidence\": 0.95,\n"
            "  \"voice_ack\": \"Short, polite Jarvis-style verbal acknowledgment (max 1 sentence)\"\n"
            "}"
        )

        user_prompt = (
            f"User Command: {text}\n"
            f"Connected Mobile Devices: {device_context}\n"
            f"Active Foreground Window: {active_window or 'Desktop'}"
        )

        # Resolved rather than hardcoded: the 2.5 names went 404 for this key
        # and all three candidates failed together, so Tier 3 never answered.
        from core.gemini_models import ladder

        for model in ladder("lite"):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config={
                        "system_instruction": system_instruction,
                        "temperature": 0.1,
                        "response_mime_type": "application/json",
                    }
                )
                raw_text = resp.text.strip() if resp and resp.text else ""
                if not raw_text:
                    continue

                raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
                raw_text = re.sub(r"\s*```$", "", raw_text).strip()
                data = json.loads(raw_text)

                result = IntentResult(
                    tier=3,
                    intent=data.get("intent", "general_conversation"),
                    action=data.get("action", ""),
                    target=data.get("target", "pc"),
                    parameters=data.get("parameters", {}),
                    confidence=float(data.get("confidence", 0.9)),
                    voice_ack=data.get("voice_ack", ""),
                    latency_ms=(time.perf_counter() - t0) * 1000,
                )
                # Save into Tier 2 cache for future 0ms instant execution
                if result.intent != "general_conversation" and result.confidence >= 0.8:
                    self.remember_intent(text, result)
                return result
            except Exception as exc:
                logger.debug(f"Model {model} classification attempt failed: {exc}")
                continue

        return None

    # =========================================================================
    # MASTER DISPATCH PIPELINE
    # =========================================================================

    def resolve(
        self,
        text: str,
        connected_devices: Optional[list[dict[str, Any]]] = None,
        active_window: str = "",
        enable_ai_tier: bool = True
    ) -> IntentResult:
        """
        Resolves the user command using the Multi-Tier Pipeline:
        0. Memory Cortex Autonomous Fact Ingestion
        1. OmniPlanner Multi-Step Composite Goal Check
        2. Fast Local Reflex (< 2ms)
        3. Learned Phrase Cache (< 1ms)
        4. Tier 1.5 Local Semantic Fast-Path (< 3ms)
        5. Tier 3 Semantic AI Router (~200ms)
        Falls back to 'general_conversation' if unresolved.
        """
        t0 = time.perf_counter()

        # 0. Autonomous Fact Harvesting into Long-Term Memory
        try:
            from core.memory_cortex import MemoryCortex
            MemoryCortex.get_instance().auto_harvest_facts(text)
        except Exception:
            pass

        # 1. Multi-Step Composite Goal Check
        try:
            from core.omni_planner import OmniPlanner
            if OmniPlanner.is_composite_goal(text):
                return IntentResult(
                    tier=1,
                    intent="omni_planner",
                    action="execute_composite_goal",
                    target="pc",
                    parameters={"goal": text},
                    voice_ack="Synthesizing multi-step execution plan across system domains, sir...",
                    latency_ms=(time.perf_counter() - t0) * 1000
                )
        except Exception:
            pass

        # --- Tier 1: Instant Local Reflex ---
        tier1_res = self.match_instant_reflex(text, connected_devices=connected_devices)
        if tier1_res:
            return tier1_res

        # --- Tier 2: Learned Phrase Cache ---
        tier2_res = self.get_cached_intent(text)
        if tier2_res:
            return tier2_res

        # --- Tier 1.5: Local Semantic Fast-Path (< 3ms, zero cloud latency) ---
        tier15_res = self.match_semantic_fastpath(text)
        if tier15_res:
            return tier15_res

        # --- Tier 3: Semantic AI Classifier ---
        if enable_ai_tier:
            try:
                tier3_res = self.classify_semantic_intent(text, connected_devices=connected_devices, active_window=active_window)
                if tier3_res:
                    return tier3_res
            except Exception as exc:
                logger.warning(f"Tier 3 classification error: {exc}")

        # Fallback: Conversational query
        return IntentResult(
            tier=1,
            intent="general_conversation",
            action="chat",
            target="pc",
            confidence=0.5,
            latency_ms=(time.perf_counter() - t0) * 1000
        )


# Global singleton instance
_GLOBAL_DISPATCHER: Optional[IntentDispatcher] = None

def get_intent_dispatcher() -> IntentDispatcher:
    global _GLOBAL_DISPATCHER
    if _GLOBAL_DISPATCHER is None:
        _GLOBAL_DISPATCHER = IntentDispatcher()
    return _GLOBAL_DISPATCHER
