"""The scan overlay may only state what it actually measured.

The old closing line named a desktop ("Workspace Recognized / VS Code -
Browser - Terminal") on every machine. These tests hold the replacement to the
narrower claim: a count a worker reported, or an admission that there was none.
"""

import threading

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def overlay(app):
    import ui

    widget = ui.ScanningOverlay()
    yield widget
    widget._tmr.stop()
    widget.deleteLater()


def test_summary_states_both_measurements(overlay):
    overlay._controls_found = 40
    overlay._text_lines = 43

    assert overlay._measured_summary() == "SCAN COMPLETE\n40 controls · 43 lines of text"


def test_summary_uses_singular_for_a_single_item(overlay):
    overlay._controls_found = 1
    overlay._text_lines = 1

    assert overlay._measured_summary() == "SCAN COMPLETE\n1 control · 1 line of text"


def test_summary_can_report_controls_alone(overlay):
    overlay._controls_found = 12

    assert overlay._measured_summary() == "SCAN COMPLETE\n12 controls"


def test_summary_reports_nothing_found_when_a_read_returned_nothing(overlay):
    # A sense ran and came back empty, which is a finding worth stating.
    overlay._controls_attempted = True
    overlay._text_attempted = True

    assert overlay._measured_summary() == "SCAN COMPLETE\nnothing readable found"


def test_summary_admits_it_measured_nothing_when_no_sense_ran(overlay):
    # No window to ask and no frame to read: say only what did happen.
    assert overlay._measured_summary() == "SCAN COMPLETE\nscreen captured"


def test_caller_text_wins_over_the_measurement(overlay):
    overlay._controls_found = 12
    overlay.hide_overlay("Three windows open")

    assert overlay._result_text == "Three windows open"


def test_hide_overlay_without_text_leaves_the_line_to_the_measurement(overlay):
    overlay.hide_overlay()

    assert overlay._result_text == ""
    assert overlay._measured_summary().startswith("SCAN COMPLETE")


def test_adopted_text_reading_becomes_the_line_count(overlay):
    overlay._pending_text = 7

    overlay._adopt_text_reading()

    assert overlay._text_lines == 7
    assert overlay._pending_text is None


def test_a_reading_from_a_superseded_scan_is_dropped(overlay):
    overlay._text_generation = 2

    overlay._accept_text_reading(1, {"ok": True, "lines": [{"text": "a"}]})

    assert overlay._pending_text is None
    assert overlay._text_attempted is False


def test_a_failed_read_marks_the_attempt_without_a_count(overlay):
    overlay._text_generation = 3

    overlay._accept_text_reading(3, {"ok": False, "error": "OCR_UNAVAILABLE"})

    assert overlay._text_attempted is True
    assert overlay._pending_text is None


def test_request_reads_the_captured_frame_not_a_fresh_grab(overlay, monkeypatch):
    from core import vision

    seen = {}

    def fake_read_text(image=None, **kwargs):
        seen["size"] = (image.width, image.height)
        return {"ok": True, "lines": [{"text": "a"}, {"text": "b"}], "text": "a\nb"}

    monkeypatch.setattr(vision, "read_text", fake_read_text)
    overlay._scan_pixels = (bytes(4 * 3 * 3), 4, 3)

    overlay._request_text_reading()
    overlay._text_thread.join(5)

    assert seen["size"] == (4, 3)
    assert overlay._pending_text == 2
    # The buffer is released once the reader has it.
    assert overlay._scan_pixels is None


def test_without_a_captured_frame_no_reading_is_attempted(overlay):
    overlay._scan_pixels = None

    overlay._request_text_reading()

    assert overlay._text_thread is None
    assert overlay._text_attempted is False


def test_adopting_controls_counts_only_what_it_draws(overlay):
    overlay._pending_boxes = [
        {"box": (20, 20, 100, 30), "type": "Button", "name": "Save"},
        {"box": (200, 60, 100, 30), "type": "Button", "name": "Cancel"},
        # Below the 8px floor: too small to be a real control target.
        {"box": (0, 0, 4, 4), "type": "Button", "name": "Tiny"},
    ]

    overlay._adopt_control_boxes()

    assert overlay._controls_found == 2
    assert len(overlay._boxes) == 2


def test_measuring_reports_only_a_live_worker(overlay):
    release = threading.Event()
    worker = threading.Thread(target=lambda: release.wait(5), daemon=True)
    overlay._text_thread = worker
    worker.start()
    try:
        assert overlay._measuring() is True
    finally:
        release.set()
        worker.join(5)

    assert overlay._measuring() is False
