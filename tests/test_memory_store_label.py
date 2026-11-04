"""The memory inspector must name the store that actually owns memory."""

from pathlib import Path

import pytest

from memory import memory_manager as mm
from memory import saturn_mode as um

UI_PATH = Path(__file__).resolve().parent.parent / "ui.py"


@pytest.fixture(autouse=True)
def restore_health():
    """Leave the module-level native health flag as we found it."""
    before = mm.degraded_status()
    yield
    mm._set_native_health(before["read_ok"], before["reason"])


@pytest.mark.parametrize("mode", ["legacy", "hybrid", "", "not-a-mode"])
def test_json_authority_modes_point_at_long_term(monkeypatch, mode):
    monkeypatch.setenv(um.ENV_VAR, mode)
    assert mm.memory_store_label() == "memory/long_term.json"


@pytest.mark.parametrize("mode", ["full", "unified"])
def test_saturn_authority_modes_point_at_the_native_engine(monkeypatch, mode):
    monkeypatch.setenv(um.ENV_VAR, mode)
    mm._set_native_health(True)
    assert mm.memory_store_label() == "the Saturn native memory engine"


def test_full_mode_points_at_the_native_engine(monkeypatch):
    monkeypatch.setenv(um.ENV_VAR, "full")
    mm._set_native_health(True)
    label = mm.memory_store_label()
    assert label == "the Saturn native memory engine"
    assert "long_term.json" not in label


def test_full_mode_reports_an_unreachable_engine(monkeypatch):
    monkeypatch.setenv(um.ENV_VAR, "full")
    mm._set_native_health(False, "sidecar unreachable")
    label = mm.memory_store_label()
    assert label == "the Saturn native memory engine (unreachable right now)"
    assert "long_term.json" not in label


def test_inspector_caption_is_mode_aware():
    source = UI_PATH.read_text(encoding="utf-8")
    assert "memory_store_label" in source
    # The old hard-coded claim must not survive anywhere in the panel source.
    assert "Stored locally in memory/long_term.json" not in source
