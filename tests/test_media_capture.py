"""Capture: the two honesty rules, and one device shared without a fight.

Nothing here touches a real camera. The fake OpenCV build can open a specific
set of codecs, and it writes a file even when it refuses the codec - which is
exactly the behaviour that made the "returns an .mp4 that plays nowhere" bug
possible in the first place.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from pathlib import Path

import pytest

from core import media_capture
from core.camera_hub import CameraBusy


class _Frame:
    def __init__(self, shape=(480, 640, 3)):
        self.shape = shape
        self.size = shape[0] * shape[1] * shape[2]


class _Lease:
    def __init__(self, frame, ok=True, reads=None):
        self._frame = frame
        self._ok = ok
        self._reads = reads
        self._served = 0

    def read(self):
        self._served += 1
        if self._reads is not None:
            return self._reads[min(self._served, len(self._reads)) - 1]
        return self._ok, self._frame if self._ok else None


class FakeHub:
    """Stands in for core.camera_hub.CameraHub, including the busy refusal."""

    def __init__(self, frame=None, ok=True, busy=False, reads=None):
        self.index = 0
        self.frame = _Frame() if frame is None else frame
        self.ok = ok
        self.busy = busy
        self.reads = reads
        self.leases = 0
        self.finished = 0
        self.owners = []

    @contextmanager
    def lease(self, owner="photo", size=None, timeout=None):
        if self.busy:
            raise CameraBusy("The camera is in use by another part of Saturn.")
        self.leases += 1
        self.owners.append(owner)
        try:
            yield _Lease(self.frame, self.ok, reads=self.reads)
        finally:
            self.finished += 1

    def health(self):
        return {"ok": self.ok, "reason": "ok" if self.ok else "NO_FRAME"}


class _Writer:
    """An OpenCV writer that creates the file before discovering it cannot encode."""

    def __init__(self, path, opened, silent=False):
        self.path = Path(path)
        self.opened = opened
        self.silent = silent
        self.writes = 0
        self.released = False
        self.path.touch()

    def isOpened(self):
        return self.opened

    def write(self, frame):
        self.writes += 1
        if self.silent:
            return
        with self.path.open("ab") as handle:
            handle.write(b"frame")

    def release(self):
        self.released = True


def _fake_cv2(working=("mp4v", "avc1", "MJPG"), silent=False, imwrite_ok=True):
    allowed = set(working)
    module = types.SimpleNamespace(writers=[], photos=[], resized=0)

    # OpenCV takes the codec as four separate characters; this fake hands the
    # assembled name straight back so the writer can tell which codec it was.
    module.VideoWriter_fourcc = lambda *codes: "".join(codes)

    def _video_writer(path, fourcc, fps, size):
        writer = _Writer(path, opened=fourcc in allowed, silent=silent)
        module.writers.append(writer)
        return writer

    module.VideoWriter = _video_writer

    def _imwrite(path, image):
        if not imwrite_ok:
            return False
        Path(path).write_bytes(b"jpeg")
        module.photos.append(str(path))
        return True

    module.imwrite = _imwrite

    def _resize(image, size):
        module.resized += 1
        return image

    module.resize = _resize
    return module


class FakeTime:
    """An advancing clock, so a ten-second clip finishes in a millisecond."""

    def __init__(self, start=1_700_000_000.0, step=0.1):
        self.now = start
        self.step = step

    def time(self):
        self.now += self.step
        return self.now

    def sleep(self, seconds):
        self.now += self.step

    def strftime(self, fmt, *args):
        return "20240101-120000"

    def localtime(self, stamp=None):
        return None


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A private evidence folder, a fake cv2, and no real Windows attributes."""
    folder = tmp_path / "evidence"
    monkeypatch.setenv("SATURN_EVIDENCE_DIR", str(folder))
    monkeypatch.setattr(media_capture, "time", FakeTime())
    hidden = []
    monkeypatch.setattr(media_capture, "hide_folder", lambda path: hidden.append(path))
    module = _fake_cv2()
    monkeypatch.setitem(sys.modules, "cv2", module)
    return types.SimpleNamespace(folder=folder, hidden=hidden, cv2=module)


def _install(monkeypatch, hub, cv2=None):
    monkeypatch.setattr(media_capture, "get_camera_hub", lambda: hub)
    if cv2 is not None:
        monkeypatch.setitem(sys.modules, "cv2", cv2)
    return hub


# ── Photos ──────────────────────────────────────────────────────────────────

def test_a_photo_is_written_and_reported(isolated, monkeypatch):
    hub = _install(monkeypatch, FakeHub())

    result = media_capture.take_photo(note="visitor")

    assert result["ok"] is True
    assert result["resolution"] == "640x480"
    assert result["bytes"] > 0
    assert Path(result["path"]).exists()
    assert Path(result["path"]).suffix == ".jpg"
    assert "visitor" in Path(result["path"]).name
    assert hub.leases == 1 and hub.finished == 1


def test_the_evidence_folder_is_marked_hidden(isolated, monkeypatch):
    _install(monkeypatch, FakeHub())

    media_capture.take_photo()

    assert isolated.hidden, "the folder was never handed to hide_folder()"
    assert Path(isolated.hidden[0]) == isolated.folder


def test_a_camera_that_returns_nothing_saves_nothing(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(ok=False))

    result = media_capture.take_photo()

    assert result["ok"] is False
    assert result["error"] == "NO_FRAME"
    assert isolated.cv2.photos == []


def test_a_busy_camera_is_reported_as_busy_not_as_a_fault(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(busy=True))

    result = media_capture.take_photo()

    assert result["ok"] is False
    assert result["error"] == "CAMERA_BUSY"


def test_a_failed_write_is_reported(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(), cv2=_fake_cv2(imwrite_ok=False))

    result = media_capture.take_photo()

    assert result["ok"] is False
    assert result["error"] == "WRITE_FAILED"


def test_the_camera_index_the_caller_asked_for_is_the_one_used(isolated, monkeypatch):
    hub = _install(monkeypatch, FakeHub())

    media_capture.take_photo(camera=1)

    assert hub.index == 1


def test_a_second_photo_in_the_same_second_does_not_overwrite_the_first(
    isolated, monkeypatch
):
    _install(monkeypatch, FakeHub())

    first = media_capture.take_photo()
    second = media_capture.take_photo()

    assert first["path"] != second["path"]
    assert Path(first["path"]).exists() and Path(second["path"]).exists()


def test_missing_opencv_is_stated_rather_than_raised(isolated, monkeypatch):
    _install(monkeypatch, FakeHub())
    monkeypatch.setitem(sys.modules, "cv2", None)

    photo = media_capture.take_photo()
    video = media_capture.record_video(seconds=1)

    assert photo["error"] == "OPENCV_UNAVAILABLE"
    assert video["error"] == "OPENCV_UNAVAILABLE"


# ── Video: the container that is written is the container that is returned ──

def test_a_working_mp4_encoder_is_reported_as_mp4(isolated, monkeypatch):
    hub = _install(monkeypatch, FakeHub())

    result = media_capture.record_video(seconds=1.0, note="arrival")

    assert result["ok"] is True
    assert result["container"] == ".mp4"
    assert result["codec"] == "mp4v"
    assert Path(result["path"]).suffix == ".mp4"
    assert result["frames"] > 1
    assert hub.leases == 1 and hub.finished == 1
    assert hub.owners == ["video"]


def test_a_build_without_mp4_falls_back_to_avi_and_says_so(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(), cv2=_fake_cv2(working=("MJPG",)))

    result = media_capture.record_video(seconds=1.0)

    assert result["ok"] is True
    # The honest part: the name matches the streams that were written.
    assert result["codec"] == "MJPG"
    assert result["container"] == ".avi"
    assert Path(result["path"]).suffix == ".avi"


def test_the_codec_order_is_tried_in_turn(isolated, monkeypatch):
    cv2 = _fake_cv2(working=("MJPG",))
    _install(monkeypatch, FakeHub(), cv2=cv2)

    media_capture.record_video(seconds=1.0)

    attempted = [writer.path.suffix for writer in cv2.writers]
    assert attempted[:3] == [".mp4", ".mp4", ".avi"]


def test_a_build_with_no_encoder_says_no_encoder_and_leaves_nothing_behind(
    isolated, monkeypatch
):
    _install(monkeypatch, FakeHub(), cv2=_fake_cv2(working=()))

    result = media_capture.record_video(seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "NO_ENCODER"
    # An unopened writer creates an empty file; it must not survive as "evidence".
    assert media_capture.media_files() == []


def test_an_empty_clip_is_deleted_rather_than_kept(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(), cv2=_fake_cv2(silent=True))

    result = media_capture.record_video(seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "EMPTY_CLIP"
    assert media_capture.media_files() == []


def test_a_camera_that_returns_nothing_records_nothing(isolated, monkeypatch):
    _install(monkeypatch, FakeHub(ok=False))

    result = media_capture.record_video(seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "NO_FRAME"
    assert isolated.cv2.writers == []


def test_a_busy_camera_is_refused_without_an_orphan_file(isolated, monkeypatch):
    hub = _install(monkeypatch, FakeHub(busy=True))

    result = media_capture.record_video(seconds=1.0)

    assert result["ok"] is False
    assert result["error"] == "CAMERA_BUSY"
    assert hub.leases == 0
    assert media_capture.media_files() == []


def test_the_writer_is_always_released(isolated, monkeypatch):
    cv2 = _fake_cv2()
    _install(monkeypatch, FakeHub(), cv2=cv2)

    media_capture.record_video(seconds=1.0)

    assert all(writer.released for writer in cv2.writers)


def test_a_clip_is_clamped_to_a_sane_length(isolated, monkeypatch):
    hub = _install(monkeypatch, FakeHub())

    result = media_capture.record_video(seconds=0)

    # Zero was clamped up to half a second, so this is a clip, not a still.
    assert result["ok"] is True
    assert hub.owners == ["video"]
    assert result["seconds"] >= 0.4


def test_a_slow_camera_open_does_not_truncate_the_clip(isolated, monkeypatch):
    """Waking a webcam can take seconds; the clip is measured from frame one.

    Measuring from the start of the call made a two-second recording come back
    as a single still, which is the worst possible outcome for evidence.
    """

    class SlowHub(FakeHub):
        @contextmanager
        def lease(self, owner="photo", size=None, timeout=None):
            media_capture.time.now += 6.0  # the device was still opening
            with super().lease(owner, size=size, timeout=timeout) as handle:
                yield handle

    _install(monkeypatch, SlowHub())

    result = media_capture.record_video(seconds=2.0)

    assert result["ok"] is True
    assert result["frames"] > 1
    assert result["seconds"] >= 1.5
    # Elapsed is the whole call, so it carries the six seconds spent opening.
    assert result["elapsed"] > result["seconds"]


# ── Retention ───────────────────────────────────────────────────────────────

def _seed(folder, count, size=1024, start_mtime=1_000_000):
    folder.mkdir(parents=True, exist_ok=True)
    made = []
    for index in range(count):
        path = folder / f"clip-{index}.mp4"
        path.write_bytes(b"x" * size)
        stamp = start_mtime + index
        import os

        os.utime(path, (stamp, stamp))
        made.append(path)
    return made


def test_retention_deletes_oldest_first_until_under_the_limit(isolated):
    made = _seed(isolated.folder, 4, size=1024 * 1024)

    # 4 MB against a 2.5 MB limit: the two oldest have to go, and no more.
    removed = media_capture.prune(retention_mb=2.5)

    assert removed == 2
    assert not made[0].exists() and not made[1].exists()
    assert made[2].exists() and made[3].exists()


def test_retention_never_deletes_the_newest_capture(isolated):
    made = _seed(isolated.folder, 2, size=1024 * 1024)

    media_capture.prune(retention_mb=0.001)

    assert not made[0].exists()
    # Whatever else happens, the file just written survives.
    assert made[1].exists()


def test_a_zero_limit_disables_retention_entirely(isolated):
    made = _seed(isolated.folder, 2, size=1024)

    assert media_capture.prune(retention_mb=0) == 0
    assert all(path.exists() for path in made)


def test_retention_on_a_small_folder_deletes_nothing(isolated):
    made = _seed(isolated.folder, 3, size=1024)

    assert media_capture.prune(retention_mb=500) == 0
    assert all(path.exists() for path in made)


def test_recording_prunes_as_a_side_effect(isolated, monkeypatch):
    _seed(isolated.folder, 2, size=1024)
    _install(monkeypatch, FakeHub())
    calls = []
    monkeypatch.setattr(media_capture, "prune", lambda *a, **k: calls.append(1) or 0)

    media_capture.record_video(seconds=1.0)

    assert calls == [1]


# ── Listing and naming ──────────────────────────────────────────────────────

def test_listing_is_newest_first_and_labels_the_kind(isolated):
    _seed(isolated.folder, 2, size=16)
    (isolated.folder / "still.jpg").write_bytes(b"jpeg")

    rows = media_capture.listing()

    assert rows[0]["name"] == "still.jpg"
    assert rows[0]["kind"] == "photo"
    assert rows[-1]["kind"] == "video"


def test_listing_respects_its_limit(isolated):
    _seed(isolated.folder, 5, size=16)

    assert len(media_capture.listing(limit=2)) == 2


def test_odd_notes_do_not_become_odd_filenames():
    assert media_capture._slug("../../etc/passwd") == "etc-passwd"
    assert media_capture._slug("") == ""
    assert len(media_capture._slug("x" * 200)) <= 40


def test_an_existing_name_is_never_reused(isolated):
    base = isolated.folder / "clip.mp4"
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b"first")

    second = media_capture._unique(base)

    assert second != base
    assert second.name == "clip-2.mp4"
