"""
tests/test_image_generator.py
Unit tests for the Neural Image Generator and Intent Dispatcher integration.
"""

import io
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from actions.image_generator import (
    generate_image,
    _build_enhanced_prompt,
    _get_output_directory,
    STYLE_PROMPT_ENHANCERS,
    PLUGIN
)
from core.intent_dispatcher import IntentDispatcher


# ── Prompt Builder & Style Tests ──────────────────────────────────────────────

def test_prompt_enhancer_styles():
    assert "photorealistic" in STYLE_PROMPT_ENHANCERS
    assert "cyberpunk" in STYLE_PROMPT_ENHANCERS
    assert "anime" in STYLE_PROMPT_ENHANCERS
    assert "cinematic_3d" in STYLE_PROMPT_ENHANCERS

    prompt = "a majestic golden eagle"
    enhanced = _build_enhanced_prompt(prompt, "cyberpunk")
    assert "cyberpunk style" in enhanced
    assert "a majestic golden eagle" in enhanced

    # Avoid duplicating existing style keywords
    already_cyberpunk = "a cyberpunk street market at night"
    enhanced_dup = _build_enhanced_prompt(already_cyberpunk, "cyberpunk")
    assert enhanced_dup == already_cyberpunk


def test_custom_output_directory():
    with tempfile.TemporaryDirectory() as tmp_dir:
        target_dir = os.path.join(tmp_dir, "custom_images")
        out = _get_output_directory(target_dir)
        assert out.exists()
        assert out.is_dir()
        assert str(out) == target_dir


# ── Execution Tests ───────────────────────────────────────────────────────────

def test_generate_image_missing_prompt():
    res = generate_image({})
    assert "Error" in res
    assert "Please provide a description" in res


def test_generate_image_success():
    fake_jpeg_bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 1024

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.read.return_value = fake_jpeg_bytes
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    mock_player = MagicMock()
    mock_speak = MagicMock()

    with tempfile.TemporaryDirectory() as tmp_dir:
        with patch("urllib.request.urlopen", return_value=mock_resp), \
             patch("os.startfile", return_value=None):

            res = generate_image(
                parameters={"prompt": "futuristic neon supercar", "style": "cyberpunk", "destination": tmp_dir},
                player=mock_player,
                speak=mock_speak
            )

            assert "Image Generated Successfully" in res
            assert "futuristic neon supercar" in res
            assert "Cyberpunk" in res

            # Verify saved file exists in tmp_dir
            saved_files = list(Path(tmp_dir).glob("*.jpg"))
            assert len(saved_files) == 1
            assert saved_files[0].read_bytes() == fake_jpeg_bytes

            # Verify player HUD callbacks
            mock_player.show_hud_deliverable.assert_called_once()
            call_kwargs = mock_player.show_hud_deliverable.call_args[1]
            assert call_kwargs["kind"] == "image"
            assert "futuristic" in call_kwargs["title"]


def test_generate_image_network_error():
    mock_player = MagicMock()
    mock_speak = MagicMock()

    with patch("urllib.request.urlopen", side_effect=Exception("Connection timed out")):
        res = generate_image(
            parameters={"prompt": "beautiful mountain landscape"},
            player=mock_player,
            speak=mock_speak
        )

        assert "Error" in res
        assert "Connection timed out" in res
        mock_player.write_log.assert_called()


# ── Intent Dispatcher Reflex Tests ───────────────────────────────────────────

def test_intent_dispatcher_image_generation():
    dispatcher = IntentDispatcher()

    # Prompt with cyberpunk style
    r1 = dispatcher.match_instant_reflex("generate an image of a neon city skyline at night in cyberpunk style")
    assert r1 is not None
    assert r1.intent == "generate_image"
    assert "neon city skyline" in r1.parameters.get("prompt", "")
    assert r1.parameters.get("style") == "cyberpunk"

    # Prompt with anime style
    r2 = dispatcher.match_instant_reflex("draw an anime picture of a brave samurai")
    assert r2 is not None
    assert r2.intent == "generate_image"
    assert "brave samurai" in r2.parameters.get("prompt", "")
    assert r2.parameters.get("style") == "anime"

    # Generic drawing prompt
    r3 = dispatcher.match_instant_reflex("create an image of a cozy cottage in the snow")
    assert r3 is not None
    assert r3.intent == "generate_image"
    assert "cozy cottage in the snow" in r3.parameters.get("prompt", "")
