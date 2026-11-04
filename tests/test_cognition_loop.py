"""
tests/test_cognition_loop.py
============================
Hermetic tests for the closed cognition loop (perceive -> recall -> reflect).

Every engine is patched at its module boundary, so these tests never touch the
real memory database, the profile store, or a live model.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.cognition_loop import CognitionLoop, _merge_blocks
from core.memory_cortex import MemoryCortex


@pytest.fixture()
def loop():
    """A fresh loop per test so the idempotency guard never leaks between them."""
    return CognitionLoop()


def _profile_store(text: str):
    """Patch the real ``workspace_store.store`` accessor with a stub returning ``text``."""
    store = SimpleNamespace(memory_context=lambda query, limit=5: text)
    module = SimpleNamespace(store=lambda: store)
    return patch.dict("sys.modules", {"workspace_store": module})


# ── Recall ────────────────────────────────────────────────────────────────────

def test_recall_context_merges_cortex_and_profile(loop):
    fake = MagicMock()
    fake.build_context_prompt.return_value = (
        "[RECALLED LONG-TERM MEMORY & USER CONTEXT]:\n"
        "- (Identity) User name (identity): Alex"
    )
    fake.get_habit_suggestion.return_value = None
    with patch.object(MemoryCortex, "get_instance", return_value=fake), \
         _profile_store("Relevant Memories:\n- User name (identity): Alex\n- User preferred editor: Neovim"):
        ctx = loop.recall_context("what editor do I use")

    assert ctx.count("Alex") == 1, "the same profile fact must not appear twice"
    assert "Neovim" in ctx
    assert "[RECALLED LONG-TERM MEMORY & USER CONTEXT]:" in ctx
    assert "Relevant Memories:" in ctx
    assert loop.stats["recall"] == 1


def test_recall_context_surfaces_learned_habit(loop):
    fake = MagicMock()
    fake.build_context_prompt.return_value = ""
    fake.get_habit_suggestion.return_value = "weather_report"
    with patch.object(MemoryCortex, "get_instance", return_value=fake), _profile_store(""):
        ctx = loop.recall_context("What is the weather today")

    assert "[LEARNED HABIT]:" in ctx
    assert "weather_report" in ctx
    fake.get_habit_suggestion.assert_called_once_with("weather today", min_frequency=3)


def test_recall_context_has_no_hint_before_a_habit_forms(loop):
    fake = MagicMock()
    fake.build_context_prompt.return_value = ""
    fake.get_habit_suggestion.return_value = None
    with patch.object(MemoryCortex, "get_instance", return_value=fake), _profile_store(""):
        assert loop.recall_context("What is the weather today") == ""


def test_recall_context_degrades_when_engines_raise(loop):
    with patch.object(MemoryCortex, "get_instance", side_effect=RuntimeError("db locked")), \
         _profile_store(""):
        assert loop.recall_context("anything at all") == ""


def test_recall_context_ignores_empty_query(loop):
    fake = MagicMock()
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        assert loop.recall_context("   ") == ""
    fake.build_context_prompt.assert_not_called()


def test_merge_blocks_keeps_one_header_and_dedupes_facts():
    merged = _merge_blocks([
        "[RECALLED LONG-TERM MEMORY & USER CONTEXT]:\n"
        "- (Preference) User preferred editor: Neovim\n"
        "Use this background knowledge naturally when responding.",
        "Relevant Memories:\n- User preferred editor: Neovim\n- User timezone: IST",
    ])
    lines = merged.splitlines()
    assert merged.count("Neovim") == 1
    assert "User timezone: IST" in merged
    assert lines[0] == "[RECALLED LONG-TERM MEMORY & USER CONTEXT]:"
    assert lines[-1] == "Use this background knowledge naturally when responding.", \
        "the prose instruction must close the block, not split the facts"


# ── Perception ────────────────────────────────────────────────────────────────

def test_perceive_harvests_once_per_utterance(loop):
    fake = MagicMock()
    fake.harvest_turn.return_value = ["User name (identity): Alex"]
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        first = loop.perceive("My name is Alex, by the way")
        second = loop.perceive("My name is Alex, by the way")

    assert first == ["User name (identity): Alex"]
    assert second == []
    assert fake.harvest_turn.call_count == 1
    assert loop.stats["facts"] == 1


def test_perceive_ignores_short_text(loop):
    fake = MagicMock()
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        assert loop.perceive("ok") == []
    fake.harvest_turn.assert_not_called()


def test_perceive_never_raises_when_cortex_fails(loop):
    with patch.object(MemoryCortex, "get_instance", side_effect=RuntimeError("unavailable")):
        assert loop.perceive("remember that I drive a blue car") == []


# ── Reflection ────────────────────────────────────────────────────────────────

def test_observe_action_records_habit_for_repeated_trigger(loop, monkeypatch):
    from core.world_model import world_model

    # Exercise the utterance fallback: no intent was dispatched for this turn.
    monkeypatch.setattr(world_model, "last_intent", lambda: "")
    fake = MagicMock()
    fake.harvest_turn.return_value = []
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        loop.perceive("What is the weather today please")
        result = loop.observe_action("weather_report", {"city": "Kolkata"}, {"result": "28C and clear"})

    assert result is not None and result.verified_success is True
    fake.record_habit_event.assert_called_once()
    trigger, action = fake.record_habit_event.call_args[0]
    assert action == "weather_report"
    assert trigger == "weather today", "filler words must not pollute the habit trigger"
    assert loop.stats["habits"] == 1


def test_habit_trigger_prefers_the_dispatched_intent(loop, monkeypatch):
    """Raw speech-to-text cannot key a habit - it never repeats identically."""
    from core.world_model import world_model

    monkeypatch.setattr(world_model, "last_intent", lambda: "screen_analysis")
    fake = MagicMock()
    fake.harvest_turn.return_value = []
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        loop.perceive("see whast going ide")
        loop.observe_action("see_screen", {}, {"result": "captured"})

    trigger, action = fake.record_habit_event.call_args[0]
    assert trigger == "screen_analysis"
    assert action == "see_screen"


def test_observe_action_returns_diagnosis_without_learning(loop):
    fake = MagicMock()
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        result = loop.observe_action(
            "computer_settings", {}, {"result": "Tool 'computer_settings' failed: Access is denied"}
        )

    assert result is not None
    assert result.verified_success is False
    assert "privilege" in result.diagnosis.lower()
    assert "administrator" in result.suggested_fix.lower()
    fake.record_habit_event.assert_not_called()
    assert loop.stats["failed"] == 1


def test_observe_action_keeps_explicit_failure_flag(loop):
    fake = MagicMock()
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        result = loop.observe_action("open_app", {}, {"success": False, "result": "window not found"})

    assert result is not None and result.verified_success is False
    fake.record_habit_event.assert_not_called()


def test_observe_action_degrades_when_reflection_unavailable(loop):
    with patch("core.execution_reflection.get_reflection_engine", side_effect=RuntimeError("offline")):
        assert loop.observe_action("open_app", {}, {"result": "Opened"}) is None


# ── Startup warm-up ───────────────────────────────────────────────────────────

def test_warm_context_returns_cortex_block(loop):
    fake = MagicMock()
    fake.build_context_prompt.return_value = "[RECALLED LONG-TERM MEMORY & USER CONTEXT]:\n- (Identity) User name: Alex"
    with patch.object(MemoryCortex, "get_instance", return_value=fake):
        assert "Alex" in loop.warm_context()


def test_warm_context_degrades_to_empty(loop):
    with patch.object(MemoryCortex, "get_instance", side_effect=RuntimeError("cold start")):
        assert loop.warm_context() == ""
