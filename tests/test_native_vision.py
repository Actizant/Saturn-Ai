"""Native vision: local OCR, UI Automation structure, and the three tools.

The expensive parts - Tesseract and COM - are exercised against the real thing;
everything else is tested through its seam so a failing assertion names the bug
rather than the machine.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from actions import native_vision as nv
from core import vision

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="UI Automation is Windows only")
TESSERACT = vision.tesseract_binary()


# ── TSV parsing ──────────────────────────────────────────────────────────────

_TSV = (
    "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth"
    "\theight\tconf\ttext\n"
    "5\t1\t1\t1\t1\t1\t100\t40\t80\t20\t96.5\tSaturn\n"
    "5\t1\t1\t1\t1\t2\t190\t40\t60\t20\t91.0\tvision\n"
    "5\t1\t1\t1\t2\t1\t100\t80\t40\t20\t88.0\tlocal\n"
    "5\t1\t1\t1\t2\t2\t-1\t-1\t-1\t-1\t-1\tnoise\n"
)


def test_parse_tsv_reads_the_documented_columns():
    """left/top/width/height come from columns 6..9, not 5..8.

    Reading one column early turned every box into a sliver of nonsense: the
    word index became x, x became y, and the rectangles were unusable.
    """
    text, lines, words = vision._parse_tsv(_TSV, origin=(0, 0), scale=1.0)

    assert text == "Saturn vision\nlocal"
    assert [w["text"] for w in words] == ["Saturn", "vision", "local"]
    assert words[0]["box"] == (100, 40, 80, 20)
    assert words[1]["box"] == (190, 40, 60, 20)
    assert words[0]["conf"] == 96.5


def test_parse_tsv_maps_boxes_back_to_screen_pixels():
    """OCR runs on an upscaled image; the caller needs screen coordinates."""
    _text, lines, _words = vision._parse_tsv(_TSV, origin=(-8, 100), scale=2.0)

    assert lines[0]["box"] == (42, 120, 75, 10)  # round(100/2)-8, round(40/2)+100, ...
    assert lines[1]["box"] == (42, 140, 20, 10)


def test_parse_tsv_drops_low_confidence_rows():
    """conf -1 marks a row that is not a word."""
    _text, _lines, words = vision._parse_tsv(_TSV, origin=(0, 0), scale=1.0)
    assert "noise" not in [w["text"] for w in words]


def test_parse_tsv_tolerates_an_empty_result():
    header = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
    text, lines, words = vision._parse_tsv(header, origin=(0, 0), scale=1.0)
    assert (text, lines, words) == ("", [], [])


# ── Region parsing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "region,expected",
    [
        ((10, 20, 300, 400), (10, 20, 300, 400)),
        ([10, 20, 300, 400], (10, 20, 300, 400)),
        ({"x": 10, "y": 20, "width": 300, "height": 400}, (10, 20, 300, 400)),
        ({"x": 10, "y": 20, "w": 300, "h": 400}, (10, 20, 300, 400)),
        ((-8, -8, 1936, 1096), (-8, -8, 1936, 1096)),
    ],
)
def test_region_tuple_accepts_the_shapes_callers_use(region, expected):
    assert vision._region_tuple(region) == expected


@pytest.mark.parametrize(
    "region",
    [None, (1, 2, 3), ("a", "b", "c", "d"), (0, 0, 0, 500), (0, 0, 500, -1), "screen"],
)
def test_region_tuple_rejects_what_it_cannot_use(region):
    assert vision._region_tuple(region) is None


# ── Bounded execution ────────────────────────────────────────────────────────


def test_run_bounded_returns_the_result_when_it_finishes():
    finished, result, error = vision._run_bounded(lambda: 42, 2.0)
    assert (finished, result, error) == (True, 42, None)


def test_run_bounded_gives_up_on_a_stuck_call():
    """A hung target application must not hang the caller."""
    finished, result, error = vision._run_bounded(lambda: time.sleep(5), 0.15)
    assert (finished, result, error) == (False, None, None)


def test_run_bounded_surfaces_the_exception():
    def explode():
        raise RuntimeError("com died")

    finished, result, error = vision._run_bounded(explode, 2.0)
    assert finished is True
    assert result is None
    assert isinstance(error, RuntimeError)


# ── Screenshot OCR ──────────────────────────────────────────────────────────


def _text_image(tmp_path: Path, text: str = "SATURN VISION 42") -> Path:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (420, 120), "white")
    draw = ImageDraw.Draw(image)
    draw.text((20, 34), text, fill="black")
    path = tmp_path / "sample.png"
    image.save(path)
    return path


@pytest.mark.skipif(not TESSERACT, reason="Tesseract is not installed")
def test_read_text_reads_a_real_image(tmp_path):
    from PIL import Image

    result = vision.read_text(image=Image.open(_text_image(tmp_path)), region=(0, 0, 420, 120))

    assert result["ok"], result
    assert "SATURN" in result["text"].upper()
    assert result["lines"], "a line with a box is the whole point"
    x, y, w, h = result["lines"][0]["box"]
    assert 0 <= x <= 420 and 0 <= y <= 120
    assert w > 0 and h > 0 and h < 120


def test_read_text_reports_missing_ocr_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(vision, "tesseract_binary", lambda: None)
    result = vision.read_text(image=object(), region=(0, 0, 10, 10))

    assert result["ok"] is False
    assert result["error"] == "OCR_UNAVAILABLE"


def test_read_text_reports_a_capture_failure(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("no display")

    monkeypatch.setattr(vision, "capture", broken)
    result = vision.read_text(region=(0, 0, 10, 10))

    assert result["ok"] is False
    assert result["error"] == "CAPTURE_FAILED"
    assert "no display" in result["message"]


def test_read_text_surfaces_a_tesseract_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "tesseract_binary", lambda: "not-tesseract")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "Error opening data file"),
    )
    from PIL import Image

    result = vision.read_text(image=Image.new("RGB", (10, 10)), region=(0, 0, 10, 10))

    assert result["ok"] is False
    assert result["error"] == "OCR_FAILED"
    assert "Error opening data file" in result["message"]


@WINDOWS_ONLY
def test_capture_returns_a_region_of_the_asked_size():
    image, region, origin = vision.capture(region=(0, 0, 120, 60))

    assert image.size == (120, 60)
    assert region == (0, 0, 120, 60)
    assert origin == (0, 0)


# ── Control structure ───────────────────────────────────────────────────────


@WINDOWS_ONLY
def test_ui_elements_without_a_window_says_so():
    result = vision.ui_elements(hwnd=0, use_cache=False)

    assert result["ok"] is False
    assert result["error"] == "UIA_UNAVAILABLE"


def test_ui_elements_is_unavailable_off_windows(monkeypatch):
    monkeypatch.setattr(vision.sys, "platform", "linux")
    result = vision.ui_elements(hwnd=1234, use_cache=False)

    assert result["ok"] is False
    assert result["error"] == "UIA_UNAVAILABLE"


def test_walk_tree_stops_at_the_element_budget():
    """The budget is what keeps a browser's thousands of nodes from stalling."""

    class FakeRect:
        left, top, right, bottom = 0, 0, 50, 20

    class Node:
        def __init__(self, index):
            self.CurrentName = f"node {index}"
            self.CurrentControlType = 50000
            self.CurrentIsOffscreen = False
            self.CurrentBoundingRectangle = FakeRect()

        def children(self):
            return [Node(i) for i in range(10)]

    class Walker:
        def __init__(self):
            self.root = Node(0)
            self.count = 0

        def GetFirstChildElement(self, node):
            self.count += 1
            return Node(self.count) if self.count < 500 else None

        def GetNextSiblingElement(self, node):
            self.count += 1
            return Node(self.count) if self.count < 500 else None

    class Automation:
        ControlViewWalker = Walker()

        def ElementFromHandle(self, _hwnd):
            return Node(0)

    rows = vision._walk_tree(Automation(), 1, limit=25, depth_cap=12)
    assert len(rows) == 25


# ── Search ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,query,expected",
    [
        ("Save", "save", 100),           # the control is the label
        ("Save as", "save", 80),         # the label starts with it
        ("Save all files", "save", 80),  # ... and so does this one
        ("Save as", "as", 70),           # a whole word inside the label
        ("Baseline", "as", 60),          # buried mid-word
        ("Cancel", "save", 0),
        ("", "save", 0),
    ],
)
def test_score_ranks_an_exact_control_above_a_loose_one(name, query, expected):
    assert vision._score(name, query) == expected


def test_find_on_screen_skips_pixels_when_a_control_matches_exactly(monkeypatch):
    """An exact named control is the answer; a full OCR pass would add nothing."""
    calls = []

    monkeypatch.setattr(
        vision, "ui_elements",
        lambda *a, **k: {"ok": True, "elements": [
            {"type": "Button", "name": "Send", "box": (10, 20, 80, 30), "depth": 3},
        ]},
    )

    def forbidden(*_a, **_k):
        calls.append(1)
        raise AssertionError("OCR should not run after an exact control match")

    monkeypatch.setattr(vision, "read_text", forbidden)
    result = vision.find_on_screen("Send")

    assert calls == []
    assert result["found"] is True
    assert result["matches"][0]["source"] == "control"
    assert result["matches"][0]["center"] == (50, 35)


def test_find_on_screen_falls_back_to_rendered_text(monkeypatch):
    monkeypatch.setattr(vision, "ui_elements", lambda *a, **k: {"ok": True, "elements": []})
    monkeypatch.setattr(
        vision, "read_text",
        lambda *a, **k: {"ok": True, "text": "Total due 40", "lines": [
            {"text": "Total due 40", "box": (100, 300, 160, 24)},
        ]},
    )

    result = vision.find_on_screen("Total due")

    assert result["found"] is True
    best = result["matches"][0]
    assert best["source"] == "text"
    assert best["center"] == (180, 312)
    # Rendered text is weaker evidence than a named control, so it scores lower
    # than the same match would as a control.
    assert best["score"] < 100


def test_find_on_screen_returns_nothing_rather_than_a_guess(monkeypatch):
    monkeypatch.setattr(vision, "ui_elements", lambda *a, **k: {"ok": True, "elements": []})
    monkeypatch.setattr(vision, "read_text", lambda *a, **k: {"ok": True, "text": "", "lines": []})

    result = vision.find_on_screen("Delete everything")

    assert result["ok"] is True
    assert result["found"] is False
    assert result["matches"] == []


def test_find_on_screen_needs_a_query():
    result = vision.find_on_screen("   ")
    assert result["ok"] is False
    assert result["error"] == "QUERY_REQUIRED"


def test_find_on_screen_keeps_working_when_ocr_is_missing(monkeypatch):
    monkeypatch.setattr(
        vision, "ui_elements",
        lambda *a, **k: {"ok": True, "elements": [
            {"type": "Button", "name": "Send", "box": (0, 0, 10, 10), "depth": 1},
        ]},
    )
    monkeypatch.setattr(
        vision, "read_text",
        lambda *a, **k: {"ok": False, "error": "OCR_UNAVAILABLE", "message": "no tesseract"},
    )

    result = vision.find_on_screen("Nothing here")

    assert result["ok"] is True
    assert result["found"] is False
    assert any("OCR_UNAVAILABLE" in note for note in result["notes"])


# ── Snapshot ────────────────────────────────────────────────────────────────


def test_see_reports_each_sense_separately(monkeypatch):
    """A dead sense must not discard the one that already answered."""
    monkeypatch.setattr(vision, "active_window", lambda: {"title": "Notepad", "hwnd": 7, "rect": (0, 0, 100, 100)})
    monkeypatch.setattr(
        vision, "ui_elements",
        lambda *a, **k: {"ok": True, "elements": [{"type": "Edit", "name": "Text", "box": (1, 2, 3, 4)}], "ms": 5},
    )
    monkeypatch.setattr(vision, "read_text", lambda *a, **k: {"ok": True, "text": "hello", "lines": [], "region": (0, 0, 100, 100)})

    snapshot = vision.see()

    assert snapshot["window"]["title"] == "Notepad"
    assert snapshot["elements"][0]["name"] == "Text"
    assert snapshot["text"] == "hello"
    assert snapshot["notes"] == []


def test_see_survives_a_dead_ui_automation(monkeypatch):
    monkeypatch.setattr(vision, "active_window", lambda: {"title": "Game", "hwnd": 9, "rect": (0, 0, 10, 10)})
    monkeypatch.setattr(vision, "ui_elements", lambda *a, **k: {"ok": False, "error": "UIA_TIMEOUT", "message": "no answer"})
    monkeypatch.setattr(vision, "read_text", lambda *a, **k: {"ok": True, "text": "score 12", "lines": [], "region": (0, 0, 10, 10)})

    snapshot = vision.see()

    assert snapshot["text"] == "score 12"
    assert any("UIA_TIMEOUT" in note for note in snapshot["notes"])


def test_available_always_describes_itself():
    report = vision.available()
    assert set(report) >= {"ocr", "uia", "platform", "dpi_scale"}
    assert report["platform"] == sys.platform


def test_layout_boxes_without_a_window_is_empty():
    assert nv.layout_boxes(None) == []


# ── Tool wording ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Save \U0001F600", "Save "),
        ("Tab\there\nNext", "Tab\there\nNext"),
        ("bell\x07 gone", "bell gone"),
    ],
)
def test_clean_keeps_what_can_be_spoken(raw, expected):
    assert nv._clean(raw) == expected


def test_read_region_says_which_sense_is_missing(monkeypatch):
    monkeypatch.setattr(
        vision, "read_text",
        lambda *a, **k: {"ok": False, "error": "OCR_UNAVAILABLE", "message": "Tesseract is not installed"},
    )

    text = nv.read_region({})

    assert "Tesseract" in text
    assert "OCR_UNAVAILABLE" not in text


def test_read_region_reports_an_empty_area_honestly(monkeypatch):
    monkeypatch.setattr(
        vision, "read_text",
        lambda *a, **k: {"ok": True, "text": "  ", "lines": [], "region": (4, 5, 60, 70)},
    )

    text = nv.read_region({})

    assert text == "No text in that area (60x70 at 4,5)."


def test_see_screen_names_the_window_and_its_controls(monkeypatch):
    monkeypatch.setattr(vision, "active_window", lambda: {"title": "Untitled - Notepad", "hwnd": 3, "rect": (0, 0, 9, 9)})
    monkeypatch.setattr(
        vision, "see",
        lambda **k: {
            "window": {"title": "Untitled - Notepad"},
            "elements": [
                {"type": "Button", "name": "Save", "box": (0, 0, 10, 10)},
                {"type": "Button", "name": "Close", "box": (0, 0, 10, 10)},
            ],
            "text": "draft text",
            "notes": [],
        },
    )

    text = nv.see_screen({})

    assert "Untitled - Notepad" in text
    assert 'Button "Save"' in text
    assert "draft text" in text


def test_see_screen_admits_when_a_window_exposes_nothing(monkeypatch):
    monkeypatch.setattr(vision, "active_window", lambda: {"title": "Game", "hwnd": 3, "rect": (0, 0, 9, 9)})
    monkeypatch.setattr(
        vision, "see",
        lambda **k: {"window": {"title": "Game"}, "elements": [], "text": "", "notes": []},
    )

    text = nv.see_screen({})

    assert "exposes nothing readable" in text


def test_find_on_screen_describes_a_control_hit(monkeypatch):
    monkeypatch.setattr(
        vision, "find_on_screen",
        lambda *a, **k: {"ok": True, "found": True, "matches": [
            {"source": "control", "label": "Send", "type": "Button",
             "box": (10, 20, 80, 30), "center": (50, 35), "score": 100},
        ], "notes": []},
    )

    text = nv.find_on_screen({"text": "Send"})

    assert text.startswith('"Send" is a Button at 10,20 (80x30)')
    assert "centre 50,35" in text


def test_find_on_screen_denies_finding_what_is_not_there(monkeypatch):
    monkeypatch.setattr(
        vision, "find_on_screen",
        lambda *a, **k: {"ok": True, "found": False, "matches": [], "notes": ["text: OCR_UNAVAILABLE"]},
    )

    text = nv.find_on_screen({"text": "Self destruct"})

    assert text.startswith("I don't see \"Self destruct\" on screen")
    assert "OCR_UNAVAILABLE" in text


def test_find_on_screen_asks_for_a_label():
    assert nv.find_on_screen({}) == "Tell me what to look for."


# ── Binary discovery ────────────────────────────────────────────────────────


def test_tesseract_binary_honours_an_override(monkeypatch, tmp_path):
    fake = tmp_path / "tesseract.exe"
    fake.write_bytes(b"stub")
    monkeypatch.setenv("SATURN_TESSERACT", str(fake))

    assert vision.tesseract_binary() == str(fake)


@pytest.mark.skipif(not TESSERACT, reason="Tesseract is not installed")
def test_tesseract_binary_finds_the_windows_install(monkeypatch):
    """The installer adds nothing to PATH, so the install directory is searched."""
    monkeypatch.delenv("SATURN_TESSERACT", raising=False)
    monkeypatch.setenv("PATH", "")  # nothing on PATH at all

    found = vision.tesseract_binary()

    assert found and Path(found).is_file()
