"""The rule book: a lesson is recognised, then gated, then read back.

Learning a rule used to be a hook in ``main.py`` that ran on every sentence and
wrote to disk the moment it recognised one. No intent, no gate, no confirmation:
a guest could rewrite how Saturn behaves and nobody would be told.

The fix has three parts, and this file pins all three: detection writes nothing
(``detect_rule``), the intent is gated like any other (``rule.write`` at guest),
and the handler **says the rule out loud** so a stored rule is never silent.
"""

import time
from pathlib import Path

import pytest

import core.trust_gate as gate
from agent.action_router import dispatch_actions, has_handler
from core.intent_dispatcher import IntentDispatcher
from core.learned_rules import LearnedRulesEngine
from core.trust_gate import Evidence, Trust, evaluate

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"


@pytest.fixture
def dispatcher(tmp_path: Path):
    return IntentDispatcher(cache_file=tmp_path / "intent_cache.json")


@pytest.fixture
def rule_store(monkeypatch):
    """An in-memory rule book: records writes, holds whatever a test sets."""
    state = {"rules": [], "writes": [], "toggles": [], "message": ""}

    def _add(rule_text, category="general", origin="user_correction"):
        state["writes"].append((rule_text, category, origin))
        stored = {"id": f"id{len(state['writes'])}", "rule": rule_text, "origin": origin,
                  "active": True}
        state["rules"].append(stored)
        return {"success": True, "rule": stored,
                "message": state["message"] or f"Learned new rule: '{rule_text}'"}

    def _list(active_only=False):
        return list(state["rules"])

    def _toggle(rule_id):
        state["toggles"].append(rule_id)
        return {"success": True, "message": f"Rule {rule_id} deactivated."}

    monkeypatch.setattr(LearnedRulesEngine, "add_rule", classmethod(lambda cls, t, **k: _add(t, **k)))
    monkeypatch.setattr(LearnedRulesEngine, "list_rules", classmethod(lambda cls, **k: _list()))
    monkeypatch.setattr(LearnedRulesEngine, "toggle_rule", classmethod(lambda cls, i: _toggle(i)))
    return state


# ── Detection writes nothing ───────────────────────────────────────────────

def test_detection_does_not_learn(monkeypatch):
    def _must_not_write(*args, **kwargs):
        raise AssertionError("detect_rule must not store a rule")

    monkeypatch.setattr(LearnedRulesEngine, "add_rule", classmethod(_must_not_write))

    assert LearnedRulesEngine.detect_rule("always use chrome") == "Always use chrome"


# ── A lesson is claimed ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text, rule",
    [
        ("always save documents to the desktop", "Always save documents to the desktop"),
        ("remember that the kettle is on the left", "The kettle is on the left"),
        ("remember to check my email", "Check my email"),
        ("new rule: always use chrome", "Always use chrome"),
        ("from now on keep the volume at 50", "Keep the volume at 50"),
        ("no, don't open edge, use chrome", "Prefer chrome instead of open edge"),
    ],
)
def test_a_lesson_is_claimed(dispatcher, text, rule):
    result = dispatcher.match_instant_reflex(text)

    assert result is not None, text
    assert result.intent == "learn_rule"
    assert result.action == "learn"
    assert result.parameters == {"rule": rule}
    assert result.voice_ack == ""


@pytest.mark.parametrize(
    "text, target",
    [
        ("forget the rule about chrome", "chrome"),
        ("delete rule id3", "id3"),
        ("remove the rule about the kettle", "the kettle"),
        ("forget the rule", ""),
    ],
)
def test_forgetting_names_its_target(dispatcher, text, target):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "forget_rule"
    assert result.parameters == {"target": target}


@pytest.mark.parametrize(
    "text", ["show me your rules", "what have you learned", "list rules", "rule book"]
)
def test_the_rule_book_is_readable(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "rule_book"
    assert result.action == "list"


@pytest.mark.parametrize("text", ["open chrome", "always open chrome", "play some music"])
def test_unrelated_sentences_stay_unclaimed(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result is None or result.intent not in (
        "learn_rule", "rule_book", "forget_rule"
    ), text


# ── The handlers ───────────────────────────────────────────────────────────

class UI:
    def __init__(self):
        self.finished = []
        self.hud = []
        self.log = []

    def write_log(self, message=""):
        self.log.append(str(message))

    def show_hud_deliverable(self, **kwargs):
        self.hud.append(kwargs)

    def finish_task_workspace(self, result, status, percent):
        self.finished.append((result, status, percent))


class App:
    def __init__(self):
        self.ui = UI()
        self.said = []

    def speak(self, message):
        self.said.append(message)


class Intent:
    def __init__(self, name, action, parameters=None):
        self.intent = name
        self.action = action
        self.parameters = parameters or {}
        self.voice_ack = ""


def _run(intent: Intent, text: str = "do it"):
    app = App()
    handled = dispatch_actions(text, intent, app, source="dashboard")

    for _ in range(80):
        if app.ui.finished or app.said:
            break
        time.sleep(0.02)

    return app, handled


def test_a_learned_rule_is_read_back(rule_store):
    app, handled = _run(
        Intent("learn_rule", "learn", {"rule": "Always save documents to the desktop"})
    )

    assert handled is True
    assert rule_store["writes"] == [
        ("Always save documents to the desktop", "conversational_preference",
         "conversational_dialogue")
    ]
    assert app.said == ["Noted, sir: Always save documents to the desktop."]
    assert app.ui.hud and app.ui.hud[0]["kind"] == "rule"
    assert app.ui.finished[-1][1] == "Rule learned."
    assert app.ui.finished[-1][2] == 100


def test_teaching_it_twice_says_it_already_knew(rule_store):
    rule_store["message"] = "Rule already exists and is active."

    app, _ = _run(Intent("learn_rule", "learn", {"rule": "Always use chrome"}))

    assert app.said == ["I already had that one, sir: Always use chrome."]


def test_a_rule_that_cannot_be_written_says_so(monkeypatch):
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(LearnedRulesEngine, "add_rule", classmethod(_boom))
    app, handled = _run(Intent("learn_rule", "learn", {"rule": "Always use chrome"}))

    assert handled is True
    assert app.ui.finished[-1][2] == 0
    assert app.said[-1] == "I could not write that rule down, sir."


def test_an_empty_rule_is_declined():
    app, handled = _run(Intent("learn_rule", "learn", {}))

    assert handled is False
    assert app.said == []


def test_an_empty_rule_book_says_so(rule_store):
    app, _ = _run(Intent("rule_book", "list"), "show me your rules")

    assert app.said == ["I have not learned any rules yet, sir."]
    assert app.ui.finished[-1][1] == "Rule book"


def test_the_rule_book_lists_what_it_knows(rule_store):
    rule_store["rules"] = [
        {"id": "a1", "rule": "Always use chrome", "active": True},
        {"id": "b2", "rule": "Never open edge", "active": True},
    ]

    app, _ = _run(Intent("rule_book", "list"), "show me your rules")

    assert app.said == [
        "I know 2 rules, sir. Say 'forget the rule about' and a word from it to remove one."
    ]
    body = app.ui.finished[-1][0]
    assert "[a1] Always use chrome" in body and "[b2] Never open edge" in body


def test_forgetting_switches_one_off(rule_store):
    rule_store["rules"] = [{"id": "a1", "rule": "Always use chrome", "active": True}]

    app, _ = _run(Intent("forget_rule", "forget", {"target": "chrome"}), "forget the rule about chrome")

    assert rule_store["toggles"] == ["a1"]
    assert app.said == ["Forgotten, sir: Always use chrome."]
    assert app.ui.finished[-1][1] == "Rule forgotten."


def test_forgetting_something_it_never_learned_says_so(rule_store):
    rule_store["rules"] = [{"id": "a1", "rule": "Always use chrome", "active": True}]

    app, _ = _run(Intent("forget_rule", "forget", {"target": "edge"}))

    assert rule_store["toggles"] == []
    assert app.said == ["I have no rule about edge, sir."]


def test_an_ambiguous_forget_asks_rather_than_guesses(rule_store):
    rule_store["rules"] = [
        {"id": "a1", "rule": "Always use chrome", "active": True},
        {"id": "b2", "rule": "Never open chrome incognito", "active": True},
    ]

    app, _ = _run(Intent("forget_rule", "forget", {"target": "chrome"}))

    assert rule_store["toggles"] == [], "guessing would remove the wrong rule"
    assert "2 rules mentioning chrome" in app.said[-1]
    assert app.ui.finished[-1][1] == "Several rules match"


def test_forgetting_with_no_target_lists_the_book(rule_store):
    rule_store["rules"] = [
        {"id": "a1", "rule": "Always use chrome", "active": True},
        {"id": "b2", "rule": "Never open edge", "active": True},
    ]

    app, _ = _run(Intent("forget_rule", "forget", {"target": ""}))

    assert rule_store["toggles"] == []
    assert "Say it with more words" in app.said[-1]


def test_the_three_intents_are_dispatchable():
    for intent in ("learn_rule", "rule_book", "forget_rule"):
        assert has_handler(intent), intent


# ── Who may teach ─────────────────────────────────────────────────────────

def test_a_guests_rule_is_learned():
    guest = Evidence(trust=Trust.GUEST, verified=True, source="face")

    decision = evaluate("learn_rule", evidence=guest)

    assert decision.denied is False
    assert decision.capability == "rule.write"


def test_an_unrecognised_voice_may_not_teach():
    unknown = Evidence(trust=Trust.UNKNOWN, verified=True, source="face")

    decision = evaluate("learn_rule", evidence=unknown)

    assert decision.denied is True
    assert decision.spoken


def test_anybody_may_read_the_rule_book():
    unknown = Evidence(trust=Trust.UNKNOWN, verified=True, source="face")

    assert evaluate("rule_book", evidence=unknown).denied is False


def test_a_stranger_is_not_told_only_the_owner_may_teach():
    """A guest may teach, so the refusal must not overstate the rule."""
    unknown = Evidence(trust=Trust.UNKNOWN, verified=True, source="face")

    spoken = evaluate("learn_rule", evidence=unknown).spoken

    assert "someone I know can approve that" in spoken


def test_the_owner_only_bar_still_says_so():
    """Naming known guests must not weaken the owner-only cases."""
    unknown = Evidence(trust=Trust.UNKNOWN, verified=True, source="face")

    spoken = evaluate("process_kill", evidence=unknown).spoken

    assert "can approve that" in spoken
    assert "someone I know" not in spoken


def test_a_refused_lesson_is_never_stored(monkeypatch, rule_store):
    monkeypatch.setattr(
        gate,
        "presence_evidence",
        lambda: Evidence(trust=Trust.UNKNOWN, verified=True, source="face"),
    )
    app, handled = _run(Intent("learn_rule", "learn", {"rule": "Always open edge"}))

    assert handled is True
    assert rule_store["writes"] == []
    assert app.said, "the refusal is spoken out loud"
    assert any(line.startswith("GATE:") for line in app.ui.log)


# ── The regression: a door with no model behind it ─────────────────────────

def test_the_phone_can_teach_a_rule(monkeypatch, dispatcher):
    from unittest.mock import MagicMock

    monkeypatch.setattr("core.omni_planner.OmniPlanner.is_composite_goal", lambda text: False)
    monkeypatch.setattr(
        "core.memory_cortex.MemoryCortex.get_instance", classmethod(lambda cls: MagicMock())
    )

    result = dispatcher.resolve("always save documents to the desktop", enable_ai_tier=False)

    assert result.tier == 1
    assert result.intent == "learn_rule"
    assert result.parameters == {"rule": "Always save documents to the desktop"}


# ── And main.py no longer watches every sentence ───────────────────────────

def test_main_has_no_learner_hook_left():
    source = MAIN.read_text(encoding="utf-8")

    for forgotten in (
        "detect_and_learn_conversational_rule",
        "Continuous Learning",
        "Behavioral Directive Learned",
        "Rule learned.",
    ):
        assert forgotten not in source, forgotten
