from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.intent_dispatcher import IntentDispatcher, IntentResult


@pytest.fixture
def dispatcher(tmp_path: Path):
    cache_file = tmp_path / "test_intent_cache.json"
    return IntentDispatcher(cache_file=cache_file)


def test_instant_reflex_volume(dispatcher: IntentDispatcher):
    r1 = dispatcher.match_instant_reflex("volume up")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "media_control"
    assert r1.action == "volume_up"
    assert r1.latency_ms < 50.0

    r2 = dispatcher.match_instant_reflex("volume to 65%")
    assert r2 is not None
    assert r2.action == "volume_set"
    assert r2.parameters["value"] == 65


def test_instant_reflex_media(dispatcher: IntentDispatcher):
    r1 = dispatcher.match_instant_reflex("pause music")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.action == "media_pause"

    r2 = dispatcher.match_instant_reflex("next track")
    assert r2 is not None
    assert r2.action == "media_next"


def test_instant_reflex_direct_phone_commands(dispatcher: IntentDispatcher):
    r1 = dispatcher.match_instant_reflex("flashlight on")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "mobile_connect"
    assert r1.action == "flashlight_on"
    assert r1.target == "phone"

    r2 = dispatcher.match_instant_reflex("ring my phone")
    assert r2 is not None
    assert r2.action == "ring_phone"
    assert r2.target == "phone"

    r3 = dispatcher.match_instant_reflex("open whatsapp on my phone")
    assert r3 is not None
    assert r3.action == "launch_app"
    assert r3.target == "phone"
    assert r3.parameters["app_name"] == "whatsapp"


def test_instant_reflex_direct_pc_apps(dispatcher: IntentDispatcher):
    r = dispatcher.match_instant_reflex("open chrome")
    assert r is not None
    assert r.tier == 1
    assert r.intent == "open_app"
    assert r.action == "open_local_app"
    assert r.target == "pc"
    assert r.parameters["app_name"] == "chrome"


def test_instant_reflex_camera_and_screen_vision(dispatcher: IntentDispatcher):
    # Camera checks
    r1 = dispatcher.match_instant_reflex("check through cam")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "vision"
    assert r1.action == "inspect_camera"
    assert r1.parameters.get("angle") == "camera"

    r2 = dispatcher.match_instant_reflex("check my current pc cam and tell what u can see")
    assert r2 is not None
    assert r2.tier == 1
    assert r2.intent == "vision"
    assert r2.action == "inspect_camera"
    assert r2.parameters.get("angle") == "camera"

    # Screen checks
    r3 = dispatcher.match_instant_reflex("check my screen")
    assert r3 is not None
    assert r3.tier == 1
    assert r3.intent == "vision"
    assert r3.action == "inspect_screen"
    assert r3.parameters.get("angle") == "screen"



def test_tier2_learned_phrase_cache(dispatcher: IntentDispatcher):
    phrase = "flick the torch on my cell"
    assert dispatcher.get_cached_intent(phrase) is None

    learned = IntentResult(
        tier=3,
        intent="mobile_connect",
        action="flashlight_on",
        target="phone",
        parameters={},
        confidence=0.95,
        voice_ack="Flicking the torch on."
    )
    dispatcher.remember_intent(phrase, learned)

    cached = dispatcher.get_cached_intent(phrase)
    assert cached is not None
    assert cached.tier == 2
    assert cached.intent == "mobile_connect"
    assert cached.action == "flashlight_on"
    assert cached.target == "phone"
    assert cached.latency_ms < 2.0


def test_semantic_ai_classification_mocked(dispatcher: IntentDispatcher):
    mock_resp = MagicMock()
    mock_resp.text = '{"intent": "presentation", "action": "create_presentation", "target": "pc", "parameters": {"topic": "AI in healthcare", "slides_count": 6}, "confidence": 0.96, "voice_ack": "Designing presentation."}'

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = mock_resp

    with patch.object(dispatcher, "_get_llm_client", return_value=mock_client):
        res = dispatcher.classify_semantic_intent("I need a 6 slide deck on AI in healthcare")
        assert res is not None
        assert res.tier == 3
        assert res.intent == "presentation"
        assert res.action == "create_presentation"
        assert res.parameters["topic"] == "AI in healthcare"
        assert res.parameters["slides_count"] == 6

        # Check it auto-remembered in Tier 2
        cached = dispatcher.get_cached_intent("I need a 6 slide deck on AI in healthcare")
        assert cached is not None
        assert cached.tier == 2
