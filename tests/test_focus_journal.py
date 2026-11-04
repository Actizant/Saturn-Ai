"""The focus journal: what it records, what it refuses to record, and how it reads.

Every test feeds the journal snapshots by hand, so none of them needs a desktop,
a sensorium thread, or the wall clock to behave a particular way.
"""

import os
import time

import pytest

from core import focus_journal
from core.focus_journal import FocusJournal

BASE = time.time()


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """Pin the wall clock to ``BASE`` for every test in this module.

    ``BASE`` is captured at import, but ``FocusJournal.segments`` filters against
    a live ``time.time()``. A full-suite run reaches these tests several minutes
    later, by which point hand-fed samples anchored to ``BASE`` have genuinely
    aged out of short windows such as ``segments(5)`` - the engine is right and
    only the clock is wrong. Pinning makes each test depend solely on the
    offsets it declares, which is what this module's docstring promises.
    """
    monkeypatch.setattr(time, "time", lambda: BASE)


def snap(offset, title, proc="Code.exe", dwell=0.0, idle=0.0, pid=None):
    """A sensorium snapshot ``offset`` seconds from now."""
    return {
        "timestamp": BASE + offset,
        "window_title": title,
        "process_name": proc,
        "dwell_seconds": dwell,
        "user_idle_seconds": idle,
        "pid": pid,
    }


def _split(journal):
    """Finished segments and the segment still open, kept apart.

    ``segments()`` deliberately includes the open one - a summary that stopped
    at the last window change would understate the current hour - so a test
    that cares about boundaries has to separate them.
    """
    return [dict(item) for item in journal._segments], journal.current()


@pytest.fixture
def journal():
    return FocusJournal(sensorium=object())


# ── Recording ────────────────────────────────────────────────────────────────

def test_the_first_sample_opens_a_segment_without_recording_one(journal):
    assert journal.sample(snap(0, "a.py - VS Code")) is True

    current = journal.current()
    assert current["title"] == "a.py - VS Code"
    assert current["seconds"] == pytest.approx(0.0)
    assert journal.segments(60) == [{"app": "Code.exe", "title": "a.py - VS Code",
                                     "start": BASE, "end": BASE, "seconds": 0.0}]


def test_a_same_window_sample_extends_the_open_segment(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(10, "a.py - VS Code", dwell=10))

    assert journal.current()["seconds"] == pytest.approx(10.0)


def test_a_window_change_closes_the_previous_segment_at_the_switch(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(10, "a.py - VS Code", dwell=10))
    journal.sample(snap(20, "Inbox - Chrome", proc="chrome.exe", dwell=10))

    finished, open_segment = _split(journal)
    assert len(finished) == 1
    assert finished[0]["title"] == "a.py - VS Code"
    # The new window's dwell says it started at +10, so that is where the old
    # segment ends: no gap, no double count.
    assert finished[0]["end"] == pytest.approx(BASE + 10)
    assert finished[0]["end"] - finished[0]["start"] == pytest.approx(10.0)
    assert open_segment["title"] == "Inbox - Chrome"


def test_a_flicker_shorter_than_the_minimum_is_not_recorded(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(3, "Settings", proc="SystemSettings.exe", dwell=3))
    journal.sample(snap(20, "a.py - VS Code", dwell=17))

    titles = [item["title"] for item in journal.segments(60)]
    assert "Settings" not in titles


def test_an_idle_stretch_closes_the_segment_at_the_last_input(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(10, "a.py - VS Code", dwell=10))

    # 200 s without a keystroke: the sensorium's dwell keeps growing, but the
    # attention stopped at +10.
    assert journal.sample(snap(210, "a.py - VS Code", dwell=210, idle=200)) is False

    finished = journal.segments(60)
    assert finished[0]["end"] == pytest.approx(BASE + 10)
    assert journal.current() is None


def test_a_dropped_flicker_does_not_split_a_visit(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(30, "a.py - VS Code", dwell=30))
    # Two seconds on a settings popup, then straight back: that is one visit to
    # the editor, interrupted, not two visits.
    journal.sample(snap(32, "Settings", proc="SystemSettings.exe", dwell=2))
    journal.sample(snap(60, "a.py - VS Code", dwell=28))
    journal.sample(snap(120, "Inbox - Chrome", proc="chrome.exe", dwell=60))

    finished, _open_segment = _split(journal)
    assert len(finished) == 1
    assert finished[0]["start"] == pytest.approx(BASE)
    assert finished[0]["end"] == pytest.approx(BASE + 60)


def test_the_journal_never_records_its_own_window(journal):
    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(10, "a.py - VS Code", dwell=10))

    assert journal.sample(snap(20, "Saturn", proc="python.exe",
                               dwell=10, pid=os.getpid())) is False
    assert journal.current() is None
    assert journal.segments(60)[0]["seconds"] == pytest.approx(10.0)


def test_an_empty_title_still_identifies_the_app(journal):
    journal.sample(snap(0, "", proc="explorer.exe"))
    journal.sample(snap(10, "", proc="explorer.exe", dwell=10))
    journal.sample(snap(30, "Downloads", proc="explorer.exe", dwell=20))

    finished, _open_segment = _split(journal)
    assert finished[0]["app"] == "explorer.exe"
    assert finished[0]["title"] == ""


def test_the_ring_buffer_keeps_only_the_newest_segments():
    journal = FocusJournal(sensorium=object(), max_segments=3)
    for index in range(6):
        start = index * 100
        journal.sample(snap(start, f"doc{index}"))
        journal.sample(snap(start + 10, f"doc{index}", dwell=10))

    assert len(journal._segments) == 3
    assert [item["title"] for item in journal._segments] == ["doc2", "doc3", "doc4"]


# ── Reading ──────────────────────────────────────────────────────────────────

def _recording_journal():
    journal = FocusJournal(sensorium=object())
    # 30 minutes on the editor, 10 in the browser.
    journal.sample(snap(-2400, "ui.py - VS Code"))
    journal.sample(snap(-600, "ui.py - VS Code", dwell=1800))
    journal.sample(snap(0, "Inbox - Chrome", proc="chrome.exe", dwell=600))
    return journal


def test_the_summary_ranks_windows_by_time():
    text = _recording_journal().summary(60)

    lines = text.splitlines()
    assert lines[0].startswith("The last hour:")
    assert "1 window" not in lines[0]
    assert "VS Code" in lines[1]
    assert "Chrome" in lines[2]
    # 30 minutes on the editor, 10 in the browser.
    assert "30 min" in lines[1]
    assert "10 min" in lines[2]


def test_the_summary_admits_an_empty_journal(journal):
    journal._running = True
    assert "anything recorded" in journal.summary(60)


def test_the_summary_says_when_tracking_is_off(journal):
    assert "not tracking focus" in journal.summary(60)


def test_recent_returns_the_newest_first(journal):
    record = _recording_journal()

    items = record.recent(60)

    assert items[0]["title"] == "Inbox - Chrome"


def test_segments_outside_the_window_are_left_out(journal):
    record = _recording_journal()

    assert [item["title"] for item in record.segments(5)] == ["Inbox - Chrome"]


# ── Lifecycle ────────────────────────────────────────────────────────────────

class _Sensorium:
    def __init__(self, running=True):
        self._running = running
        self.calls = 0

    def get_snapshot(self):
        self.calls += 1
        return snap(0, "a.py - VS Code")


def test_a_stopped_sensorium_is_not_sampled():
    # Its fields would be frozen at the last poll, which would record the same
    # minute twice.
    sensorium = _Sensorium(running=False)
    journal = FocusJournal(sensorium=sensorium)

    assert journal.sample() is False
    assert sensorium.calls == 0


def test_a_running_sensorium_is_sampled():
    sensorium = _Sensorium(running=True)
    journal = FocusJournal(sensorium=sensorium)

    assert journal.sample() is True
    assert sensorium.calls == 1


def test_start_refuses_when_there_is_no_sensorium(monkeypatch):
    journal = FocusJournal()
    monkeypatch.setattr(journal, "_resolve_sensorium", lambda: None)

    assert journal.start() is False
    assert journal.running is False


def test_start_refuses_while_the_sensorium_is_stopped():
    journal = FocusJournal(sensorium=_Sensorium(running=False))

    assert journal.start() is False
    assert journal.running is False


def test_start_and_stop_are_safe_to_call_twice():
    journal = FocusJournal(sensorium=_Sensorium(), sample_seconds=0.05)

    assert journal.start() is True
    thread = journal._thread
    assert journal.start() is True
    assert journal._thread is thread

    journal.stop()
    assert journal.running is False
    journal.stop()


def test_a_callback_reports_a_change_of_focus(journal):
    seen = []
    journal.on_change(lambda current: seen.append(current["title"]))

    journal.sample(snap(0, "a.py - VS Code"))
    journal.sample(snap(10, "Inbox - Chrome", proc="chrome.exe", dwell=10))

    assert seen == ["a.py - VS Code", "Inbox - Chrome"]


# ── Formatting ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,expected", [
    (4, "4 sec"),
    (60, "1 min"),
    (300, "5 min"),
    (3900, "1 h 5 min"),
    (7200, "2 h"),
])
def test_durations_read_naturally(seconds, expected):
    assert focus_journal._human(seconds) == expected


def test_a_long_title_is_cut_at_a_word_not_mid_word():
    title = "Greatest Club Prime OAT shorts viral football - YouTube - Google Chrome"

    cleaned = focus_journal._clean_title(title)

    assert len(cleaned) <= focus_journal._TITLE_LIMIT + 4
    assert cleaned.endswith(" ...")
    body = cleaned[:-4]
    assert body in title
    # No half-words: whatever comes next in the original is a space.
    assert title[len(body):].startswith(" ")


def test_whitespace_in_a_title_is_collapsed():
    assert focus_journal._clean_title("  a.py\n\t-   VS Code ") == "a.py - VS Code"


def test_a_hour_is_called_an_hour():
    assert focus_journal._window_label(60) == "the last hour"
    assert focus_journal._window_label(90) == "the last 90 minutes"


# ── The tool ─────────────────────────────────────────────────────────────────

def test_the_tool_says_when_tracking_never_ran(monkeypatch):
    from actions import focus_report

    monkeypatch.setattr(focus_report, "get_focus_journal", lambda: FocusJournal())

    answer = focus_report.what_have_i_been_doing({})

    assert "isn't running" in answer


def test_the_tool_returns_the_summary(monkeypatch):
    from actions import focus_report

    record = _recording_journal()
    monkeypatch.setattr(focus_report, "get_focus_journal", lambda: record)

    answer = focus_report.what_have_i_been_doing({"minutes": 60})

    assert "The last hour:" in answer
    assert "VS Code" in answer


@pytest.mark.parametrize("raw,expected", [
    (None, 60.0),
    (0, 1.0),
    ("abc", 60.0),
    (100000, 720.0),
    ("15", 15.0),
])
def test_the_tool_clamps_the_window(raw, expected):
    from actions import focus_report

    assert focus_report._minutes({"minutes": raw}) == expected
