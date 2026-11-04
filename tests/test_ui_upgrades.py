"""
Unit Tests for Saturn UI Upgrades:
1. Holographic Omni-Command Palette (fuzzy filtering, commands list, actions)
2. Live Voice-Reactive Audio Waveform Visualizer (level response, muting, ticking)
3. Rich Code Block Parser & Markdown Formatter (_split_markdown_code_blocks, _markdown_to_html)
"""

import pytest
import os
import sys

# Ensure offscreen Qt platform for testing
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["--platform", "offscreen"])
    return app


def test_markdown_code_block_splitting():
    from ui import _split_markdown_code_blocks

    text = (
        "Here is a Python function:\n"
        "```python\n"
        "def hello_saturn():\n"
        "    return 'Saturn'\n"
        "```\n"
        "And here is PowerShell:\n"
        "```powershell\n"
        "Get-Process saturn\n"
        "```\n"
        "That concludes the code."
    )
    chunks = _split_markdown_code_blocks(text)
    assert len(chunks) == 5

    assert chunks[0]["type"] == "text"
    assert "Here is a Python function:" in chunks[0]["content"]

    assert chunks[1]["type"] == "code"
    assert chunks[1]["lang"] == "PYTHON"
    assert "def hello_saturn():" in chunks[1]["content"]

    assert chunks[2]["type"] == "text"
    assert "And here is PowerShell:" in chunks[2]["content"]

    assert chunks[3]["type"] == "code"
    assert chunks[3]["lang"] == "POWERSHELL"
    assert "Get-Process saturn" in chunks[3]["content"]

    assert chunks[4]["type"] == "text"
    assert "That concludes the code." in chunks[4]["content"]


def test_markdown_to_html_formatting():
    from ui import _markdown_to_html

    sample = "**Important Notice** with *emphasis* and `inline_code()`.\n- Step 1\n- Step 2"
    html = _markdown_to_html(sample, role="assistant")

    assert "<b>Important Notice</b>" in html
    assert "<i>emphasis</i>" in html
    assert "<code style=" in html
    assert "inline_code()" in html
    assert "• Step 1" in html
    assert "• Step 2" in html


def test_code_block_widget_creation(qapp):
    from ui import CodeBlockWidget

    code_sample = "def compute_energy():\n    return 42 * 100"
    widget = CodeBlockWidget(code=code_sample, lang="PYTHON")

    assert widget._lang == "PYTHON"
    assert widget._raw_code == code_sample
    assert widget._code_edit.toPlainText() == code_sample
    assert widget._copy_btn.text() == "📋 Copy"

    # Test copy execution
    widget._copy_code()
    assert widget._copy_btn.text() == "✓ Copied!"


def test_voice_waveform_visualizer(qapp):
    from ui import VoiceWaveformVisualizer

    visualizer = VoiceWaveformVisualizer(bars=12)
    assert visualizer._bar_count == 12
    assert visualizer._audio_level == 0.0

    # Set audio level
    visualizer.set_audio_level(0.85)
    assert visualizer._target_level == 0.85

    # Run tick
    visualizer._tick()
    assert visualizer._audio_level > 0.0

    # Test muting
    visualizer.set_muted(True)
    assert visualizer._muted is True


def test_omni_command_palette_filtering(qapp):
    from ui import OmniCommandPalette, DEFAULT_PALETTE_COMMANDS

    palette = OmniCommandPalette(commands=DEFAULT_PALETTE_COMMANDS)
    assert len(palette._all_commands) >= 10

    # Test filtering for "snapshot"
    palette._on_search_text_changed("snapshot")
    assert any("snapshot" in item.get("title", "").lower() for item in palette._filtered_commands)

    # Test filtering for "saturn"
    palette._on_search_text_changed("saturn")
    assert any("saturn" in item.get("title", "").lower() for item in palette._filtered_commands)

    # Test custom query addition
    palette._on_search_text_changed("how do black holes work?")
    custom_items = [i for i in palette._filtered_commands if i.get("id") == "custom_query"]
    assert len(custom_items) == 1
    assert "how do black holes work?" in custom_items[0]["cmd"]
