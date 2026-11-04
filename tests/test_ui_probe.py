"""What is on screen, and how sure am I that the thing was found.

The scoring rules are the ones that decide whether Saturn clicks "Save" or
"Save as…", so they are tested directly rather than through a whole loop.
"""

from __future__ import annotations

import pytest

from core import ui_probe
from core.ui_probe import Observation

WINDOW = {"title": "untitled - Blender", "process_name": "blender.exe",
          "class_name": "GHOST_WindowClass", "hwnd": 4242,
          "rect": (0, 0, 1200, 800), "method": "foreground", "self_window": False}


def element(name, box=(10, 20, 100, 30), type_="Button", **extra):
    return {"name": name, "type": type_, "box": list(box), **extra}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    ui_probe.clear_cache()
    monkeypatch.setattr(ui_probe, "get_active_user_window", lambda: dict(WINDOW))
    yield
    ui_probe.clear_cache()


# ── Matching ───────────────────────────────────────────────────────────────

def test_an_exact_name_is_the_strongest_match():
    assert ui_probe.score("Save", "save") == 100


def test_a_containing_name_still_matches():
    assert ui_probe.score("Save as PNG", "save") >= 85


def test_a_whole_word_inside_a_longer_name_is_a_strong_match():
    assert ui_probe.score("Open project settings", "open") == 85


def test_word_overlap_is_only_a_hint():
    value = ui_probe.score("Project settings dialog", "settings render")
    assert 0 < value < 85


def test_unrelated_words_do_not_match():
    assert ui_probe.score("Cancel", "render") == 0
    assert ui_probe.score("", "render") == 0
    assert ui_probe.score("Cancel", "") == 0


def test_punctuation_and_case_are_ignored():
    assert ui_probe.score("Save  As...", "save as") == 100


def test_matching_is_not_confused_by_a_short_word_inside_another():
    """'ok' must not match 'Look' or 'Token'."""
    assert ui_probe.score("Look dev", "ok") == 0
    assert ui_probe.score("Token", "ok") == 0


# ── The snapshot ───────────────────────────────────────────────────────────

def test_a_snapshot_says_which_sense_worked():
    assert Observation(elements=[element("Save")]).rung == "control"
    assert Observation(text="hello").rung == "text"
    assert Observation().rung == "none"


def test_a_snapshot_names_its_controls():
    observation = Observation(elements=[element("Save"), element("Cancel")])
    assert observation.names() == ["Save", "Cancel"]


def test_the_digest_only_changes_when_the_screen_does():
    first = Observation(window_title="a", elements=[element("Save")])
    same = Observation(window_title="a", elements=[element("Save")])
    other = Observation(window_title="a", elements=[element("Save"), element("Render")])

    assert first.digest() == same.digest()
    assert first.digest() != other.digest()


def test_an_empty_snapshot_is_not_readable():
    assert Observation().readable is False
    assert Observation(window_title="x").readable is True


def test_a_title_is_not_the_same_as_readable_content():
    assert Observation(window_title="Game").content_readable is False
    assert Observation(window_title="Game", text="score 10").content_readable is True


# ── Finding ────────────────────────────────────────────────────────────────

def test_a_control_is_found_with_its_centre():
    observation = Observation(elements=[element("Render", box=(100, 200, 60, 20))])

    match = ui_probe.find("render", observation, include_text=False)

    assert match["source"] == "control"
    assert match["center"] == (130, 210)


def test_a_control_beats_text_of_the_same_strength():
    observation = Observation(elements=[element("Render")],
                              words=[{"text": "Render", "box": [5, 5, 40, 12]}])

    match = ui_probe.find("render", observation, include_text=True)

    assert match["source"] == "control"


def test_text_is_used_when_there_is_no_control():
    observation = Observation(words=[{"text": "Render", "left": 10, "top": 20,
                                      "width": 40, "height": 12}])

    match = ui_probe.find("render", observation)

    assert match["source"] == "text"
    assert match["box"] == (10, 20, 40, 12)


def test_nothing_on_screen_is_none_not_a_guess():
    observation = Observation(elements=[element("Cancel"), element("Help")])
    assert ui_probe.find("render", observation, include_text=False) is None


def test_an_empty_target_finds_nothing():
    observation = Observation(elements=[element("Save")])
    assert ui_probe.find("   ", observation) is None


def test_disabled_and_offscreen_flags_travel_with_the_match():
    observation = Observation(elements=[element("Save", enabled=False, offscreen=True)])
    match = ui_probe.find("save", observation, include_text=False)

    assert match["enabled"] is False
    assert match["offscreen"] is True


# ── Observe ────────────────────────────────────────────────────────────────

def test_observing_reads_the_users_window_not_the_focused_one():
    """The orb being focused must never turn the probe onto Saturn's own UI."""
    seen = {}

    def fake_elements(window=None, hwnd=None, **kwargs):
        seen["hwnd"] = hwnd
        return {"ok": True, "elements": [element("Save")]}

    import core.vision as vision

    original = vision.ui_elements
    vision.ui_elements = fake_elements
    try:
        observation = ui_probe.observe(include_text=False)
    finally:
        vision.ui_elements = original

    assert seen["hwnd"] == 4242
    assert observation.window_title == "untitled - Blender"
    assert observation.app.startswith("Blender")


def test_observing_reports_a_window_that_cannot_be_read():
    import core.vision as vision

    original_elements, original_text = vision.ui_elements, vision.read_text
    vision.ui_elements = lambda **kwargs: {"ok": False, "error": "UIA_UNAVAILABLE"}
    vision.read_text = lambda **kwargs: {"ok": False, "error": "OCR_UNAVAILABLE"}
    try:
        observation = ui_probe.observe()
    finally:
        vision.ui_elements, vision.read_text = original_elements, original_text

    assert observation.readable is True
    assert observation.content_readable is False
    assert any("nothing readable" in note for note in observation.notes)


def test_a_second_look_within_the_cache_window_is_free():
    import core.vision as vision

    calls = {"n": 0}
    original = vision.ui_elements

    def counting(**kwargs):
        calls["n"] += 1
        return {"ok": True, "elements": [element("Save")]}

    vision.ui_elements = counting
    try:
        ui_probe.observe(include_text=False)
        ui_probe.observe(include_text=False)
    finally:
        vision.ui_elements = original

    assert calls["n"] == 1


def test_the_cache_can_be_bypassed():
    import core.vision as vision

    calls = {"n": 0}
    original = vision.ui_elements

    def counting(**kwargs):
        calls["n"] += 1
        return {"ok": True, "elements": [element("Save")]}

    vision.ui_elements = counting
    try:
        ui_probe.observe(include_text=False)
        ui_probe.observe(include_text=False, use_cache=False)
    finally:
        vision.ui_elements = original

    assert calls["n"] == 2


def test_a_dying_vision_layer_is_a_note_not_a_crash():
    import core.vision as vision

    def boom(**kwargs):
        raise RuntimeError("uia died")

    original = vision.ui_elements
    vision.ui_elements = boom
    try:
        observation = ui_probe.observe(include_text=False)
    finally:
        vision.ui_elements = original

    assert observation.readable is True   # the title is still known
    assert any("UIA_FAILED" in note for note in observation.notes)


# ── Describing ─────────────────────────────────────────────────────────────

def test_a_summary_lists_the_controls():
    observation = Observation(window_title="untitled - Blender",
                              process="blender.exe",
                              elements=[element("Render"), element("Save")])

    text = ui_probe.summarise(observation)

    assert "Blender" in text
    assert "Render" in text


def test_a_summary_admits_a_blind_screen():
    text = ui_probe.summarise(Observation(window_title="Game"))

    assert "could not read" in text
