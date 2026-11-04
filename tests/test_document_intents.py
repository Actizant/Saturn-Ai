"""A deck, a workbook, a website, a program: one road each, and one only.

These four used to be detected *below* the router in ``main.py``, which meant a
request was only ever recognised when the Gemini classifier happened to be
switched on. Turn it off - the phone, Instagram, a live duplex session all do -
and the words did nothing. They are Tier-1 intents now, so the same sentence is
answered the same way from every door.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.action_router import has_handler
from core.intent_dispatcher import IntentDispatcher

ROOT = Path(__file__).resolve().parent.parent
MAIN = ROOT / "main.py"
OFFICE = ROOT / "agent" / "actions" / "office.py"


@pytest.fixture
def dispatcher(tmp_path: Path):
    return IntentDispatcher(cache_file=tmp_path / "intent_cache.json")


def _intent(dispatcher: IntentDispatcher, text: str):
    """The Tier-1 answer, or None when nothing local claims the sentence."""
    result = dispatcher.match_instant_reflex(text)
    return None if result is None else result.intent


# ── Each category is recognised, and knows which category it is ──────────────

@pytest.mark.parametrize(
    "text",
    [
        "make me a presentation about the Q3 numbers",
        "create a power point deck on climate policy",
        "build a pitch deck for investors",
    ],
)
def test_a_deck_is_recognised(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "presentation"
    assert result.tier == 1
    assert result.parameters["topic"] == text


@pytest.mark.parametrize(
    "text",
    [
        "make an excel budget sheet",
        "create a spreadsheet of our expenses",
        "build an expense tracker",
    ],
)
def test_a_workbook_is_recognised(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "spreadsheet"
    assert result.parameters["topic"] == text


@pytest.mark.parametrize(
    "text",
    [
        "build a portfolio website",
        "create a landing page for my store",
        "design a website for my business",       # a verb the code detector lacks
        "write a flask app that logs visits",
        "create a python script to parse logs",
        "develop a snake game",
    ],
)
def test_software_is_recognised(dispatcher, text):
    result = dispatcher.match_instant_reflex(text)

    assert result.intent == "developer_agent"
    assert result.parameters["description"] == text


# ── The order of the four is the legacy order, and it matters ────────────────

def test_a_deck_beats_the_code_detector(dispatcher):
    # "react" and "app" are both code words; the deck is what was asked for.
    assert _intent(dispatcher, "create a slide deck about our react app") == "presentation"


def test_a_workbook_beats_the_code_detector(dispatcher):
    assert _intent(dispatcher, "build an excel sheet for the app") == "spreadsheet"


# ── But a sentence may be *about* software without asking for any ────────────

@pytest.mark.parametrize(
    "text",
    [
        "make an api call to the weather service",
        "open website google.com",
        "go to website youtube.com",
        "what is a website",
        "build a website on my phone",
        "build a react app on my android tablet",
    ],
)
def test_talking_about_software_is_not_building_it(dispatcher, text):
    assert _intent(dispatcher, text) != "developer_agent"


def test_a_phone_build_does_not_become_a_desktop_project(dispatcher):
    # Regression: the phone markers used to be spelled only in the bare form, so
    # "build a react app on my android tablet" scaffolded on the PC instead.
    for text in (
        "build a react app on my phone",
        "build a react app on my android tablet",
        "create a website on my iphone",
    ):
        assert _intent(dispatcher, text) != "developer_agent", text


# ── The road has to exist at the other end ──────────────────────────────────

@pytest.mark.parametrize("intent", ["presentation", "spreadsheet", "developer_agent"])
def test_every_new_intent_has_a_handler(intent):
    assert has_handler(intent), f"Tier 1 can return '{intent}' with nothing to run it"


# ── The regression itself: no model required ────────────────────────────────

def test_the_phone_can_ask_for_a_deck(monkeypatch, dispatcher):
    """A phone turn has Tier 3 off. It used to get nothing at all."""
    monkeypatch.setattr("core.omni_planner.OmniPlanner.is_composite_goal", lambda text: False)
    monkeypatch.setattr(
        "core.memory_cortex.MemoryCortex.get_instance", classmethod(lambda cls: MagicMock())
    )

    result = dispatcher.resolve("make me a presentation about bees", enable_ai_tier=False)

    assert result.tier == 1
    assert result.intent == "presentation"
    assert result.parameters["topic"] == "make me a presentation about bees"


# ── And main.py keeps no second copy of any of it ───────────────────────────

def test_main_still_carries_no_detector_and_no_second_ladder():
    source = MAIN.read_text(encoding="utf-8")

    for name in (
        "_looks_like_code_request",
        "_looks_like_website_request",
        "_looks_like_presentation_request",
        "_looks_like_spreadsheet_request",
    ):
        assert name not in source, f"{name} has a second home again"

    # The sentences now live in one place: the handler that speaks them.
    office = OFFICE.read_text(encoding="utf-8")
    for line in (
        "Designing your presentation slides...",
        "Building your spreadsheet workbook...",
        "Working on your project with Saturn Dev...",
    ):
        assert line not in source, line
        assert line in office, line

    # The tool branch may still call the generator directly; the turn may not.
    assert source.count("run_dev_agent") == 0

    # Nor does the turn still own the step lists a second ladder would need.
    for step in ("Researching topic", "Outlining slides", "Scaffolding files", "Verifying build"):
        assert step not in source, step
        assert step in office, step
