"""Photo and video capture, written straight to disk, never to a cloud.

The perimeter needs evidence and the assistant needs a camera tool; both come
through here so they cannot fight over the device and so both are subject to one
retention policy. Every capture takes a lease from :mod:`core.camera_hub`, which
makes the presence loop pause for the duration instead of failing.

Two honesty rules, both learned from the rest of this codebase:

* If the MP4 encoder is missing from this OpenCV build - it often is - the clip
  is written as AVI and the result says ``container: ".avi"``. Handing back a
  ``.mp4`` name for an MJPG stream produces a file that plays nowhere.
* A capture that recorded zero frames is deleted and reported as a failure. A
  zero-byte file named "evidence" is worse than no file.
"""

from __future__ import annotations

import ctypes
import os
import time
from pathlib import Path
from typing import Optional

from core.camera_hub import CameraBusy, get_camera_hub

DEFAULT_RETENTION_MB = 500
PHOTO_SIZE = (1280, 720)
VIDEO_SIZE = (640, 480)
VIDEO_FPS = 12.0
MAX_VIDEO_SECONDS = 300.0

# Tried in order. avc1 needs an H.264 encoder that many OpenCV wheels do not
# ship; MJPG always works but only in AVI.
CODECS = (("mp4v", ".mp4"), ("avc1", ".mp4"), ("MJPG", ".avi"))

FILE_ATTRIBUTE_HIDDEN = 0x2


def evidence_dir() -> Path:
    override = os.environ.get("SATURN_EVIDENCE_DIR")
    if override:
        target = Path(override).expanduser()
    else:
        from core.user_paths import get_user_data_dir

        target = get_user_data_dir() / "security" / "evidence"
    target.mkdir(parents=True, exist_ok=True)
    return target


def hide_folder(path: Path) -> None:
    """Mark the folder hidden on Windows so it does not sit in plain sight."""
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        current = kernel32.GetFileAttributesW(str(path))
        if current in (-1, 0xFFFFFFFF):
            return
        kernel32.SetFileAttributesW(str(path), int(current) | FILE_ATTRIBUTE_HIDDEN)
    except Exception:
        pass


def _slug(text: str) -> str:
    clean = "".join(c if c.isalnum() or c in "-_" else "-" for c in str(text or "").strip())
    clean = "-".join(part for part in clean.split("-") if part)
    return clean[:40]


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _unique(base: Path) -> Path:
    """Never overwrite evidence: two clip starts in one second both survive."""
    if not base.exists():
        return base
    for counter in range(2, 1000):
        candidate = base.with_name(f"{base.stem}-{counter}{base.suffix}")
        if not candidate.exists():
            return candidate
    return base.with_name(f"{base.stem}-{int(time.time() * 1000)}{base.suffix}")


def _writer_for(cv2, stem: Path, fps: float, size):
    """First codec that actually opens a writer, with the extension it implies."""
    width, height = size
    for fourcc, extension in CODECS:
        target = _unique(stem.with_suffix(extension))
        try:
            writer = cv2.VideoWriter(
                str(target), cv2.VideoWriter_fourcc(*fourcc), float(fps), (width, height)
            )
        except Exception:
            continue
        try:
            if writer is not None and writer.isOpened():
                return writer, target, fourcc
        except Exception:
            pass
        try:
            if writer is not None:
                writer.release()
        except Exception:
            pass
        # An unopened writer still creates an empty file; do not leave it behind.
        try:
            if target.exists() and target.stat().st_size == 0:
                target.unlink()
        except Exception:
            pass
    return None, None, None


def take_photo(camera: int = 0, note: str = "", size=PHOTO_SIZE) -> dict:
    """One still frame, written as JPEG."""
    try:
        import cv2
    except Exception as exc:
        return {"ok": False, "error": "OPENCV_UNAVAILABLE", "message": str(exc)}

    hub = get_camera_hub()
    hub.index = int(camera)
    folder = evidence_dir()
    hide_folder(folder)
    slug = _slug(note)
    target = _unique(folder / f"{_stamp()}_photo{('_' + slug) if slug else ''}.jpg")

    try:
        with hub.lease("photo", size=size) as lease:
            frame = None
            # Two reads: the first can still be the dark frame from opening.
            for _ in range(3):
                ok, candidate = lease.read()
                if ok and candidate is not None and getattr(candidate, "size", 0):
                    frame = candidate
            if frame is None:
                return {"ok": False, "error": "NO_FRAME",
                        "message": "The camera returned no picture, so nothing was saved."}
            written = cv2.imwrite(str(target), frame)
            if not written:
                return {"ok": False, "error": "WRITE_FAILED",
                        "message": f"Could not write {target}."}
            height, width = frame.shape[:2]
            return {"ok": True, "path": str(target), "bytes": target.stat().st_size,
                    "resolution": f"{width}x{height}", "hub": hub.health()}
    except CameraBusy as exc:
        return {"ok": False, "error": "CAMERA_BUSY", "message": str(exc)}
    except Exception as exc:
        return {"ok": False, "error": "CAPTURE_FAILED", "message": str(exc)}


def record_video(seconds: float = 10.0, camera: int = 0, note: str = "",
                 size=VIDEO_SIZE, fps: float = VIDEO_FPS) -> dict:
    """A clip of at most ``seconds``. Returns the container it really wrote."""
    try:
        import cv2
    except Exception as exc:
        return {"ok": False, "error": "OPENCV_UNAVAILABLE", "message": str(exc)}

    span = max(0.5, min(float(seconds or 0), MAX_VIDEO_SECONDS))
    rate = max(1.0, min(float(fps or VIDEO_FPS), 60.0))
    hub = get_camera_hub()
    hub.index = int(camera)
    folder = evidence_dir()
    hide_folder(folder)
    slug = _slug(note)
    stem = folder / f"{_stamp()}_clip{('_' + slug) if slug else ''}"

    writer = None
    target: Optional[Path] = None
    fourcc = None
    frames = 0
    started = time.time()
    recorded_from = None
    try:
        with hub.lease("video", size=size) as lease:
            ok, frame = lease.read()
            if not ok or frame is None or not getattr(frame, "size", 0):
                return {"ok": False, "error": "NO_FRAME",
                        "message": "The camera returned no picture, so no clip was recorded."}
            height, width = frame.shape[:2]
            writer, target, fourcc = _writer_for(cv2, stem, rate, (width, height))
            if writer is None:
                return {"ok": False, "error": "NO_ENCODER",
                        "message": "No video encoder is available in this OpenCV build."}
            # The clock starts at the first frame, not at the call: waking a
            # webcam can take seconds, and a two-second clip must not come out
            # as a single still because the device was still opening.
            recorded_from = time.time()
            writer.write(frame)
            frames = 1
            deadline = recorded_from + span
            interval = 1.0 / rate
            while time.time() < deadline:
                time.sleep(interval)
                ok, frame = lease.read()
                if ok and frame is not None and getattr(frame, "size", 0):
                    if frame.shape[1] != width or frame.shape[0] != height:
                        frame = cv2.resize(frame, (width, height))
                    writer.write(frame)
                    frames += 1
    except CameraBusy as exc:
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass
        return {"ok": False, "error": "CAMERA_BUSY", "message": str(exc)}
    except Exception as exc:
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass
        return {"ok": False, "error": "CAPTURE_FAILED", "message": str(exc)}
    finally:
        if writer is not None:
            try:
                writer.release()
            except Exception:
                pass

    elapsed = round(time.time() - started, 2)
    recorded = round(time.time() - (recorded_from or started), 2)
    if frames == 0 or target is None or not target.exists() or target.stat().st_size == 0:
        try:
            if target is not None and target.exists():
                target.unlink()
        except Exception:
            pass
        return {"ok": False, "error": "EMPTY_CLIP",
                "message": "The clip came out empty, so it was deleted rather than kept."}

    prune()
    # ``seconds`` is the length of the clip; ``elapsed`` is how long the call
    # took, which is longer whenever the camera had to wake up first.
    return {"ok": True, "path": str(target), "container": target.suffix.lower(),
            "codec": fourcc, "frames": frames, "seconds": recorded,
            "elapsed": elapsed, "bytes": target.stat().st_size, "fps": rate}


def media_files() -> list[Path]:
    try:
        return sorted(
            (p for p in evidence_dir().iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return []


def prune(retention_mb: Optional[float] = None) -> int:
    """Delete the oldest captures until the folder is under the limit."""
    limit_mb = DEFAULT_RETENTION_MB if retention_mb is None else float(retention_mb)
    if limit_mb <= 0:
        return 0
    limit = limit_mb * 1024 * 1024
    files = media_files()
    try:
        total = sum(p.stat().st_size for p in files)
    except OSError:
        return 0
    removed = 0
    # Oldest first, and never the newest file: that is the one just written.
    for path in files[:-1]:
        if total <= limit:
            break
        try:
            size = path.stat().st_size
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            continue
    return removed


def listing(limit: int = 20) -> list[dict]:
    """Newest first, for the assistant to report what evidence exists."""
    out = []
    for path in reversed(media_files()):
        try:
            stat = path.stat()
        except OSError:
            continue
        out.append({
            "name": path.name,
            "kind": "video" if path.suffix.lower() in (".mp4", ".avi", ".mjpg", ".mov") else "photo",
            "bytes": stat.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
        if len(out) >= max(1, int(limit)):
            break
    return out
