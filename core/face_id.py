"""On-device face identity: detect, describe, match, remember.

Nothing here touches a network. What is stored is a list of 128 numbers per
person - a face *descriptor*, not a photograph - and the photographs are never
written to disk. No image, frame or descriptor is uploaded anywhere.

The pipeline has two halves and they fail independently:

* **Detection** asks "is there a face in this frame, and where". The Haar
  cascade shipped with OpenCV needs no model file and is therefore always
  available.
* **Identity** asks "whose face is it". That needs two ONNX files, YuNet to
  place the five landmarks and SFace to turn the aligned crop into a descriptor.
  Neither ships with OpenCV, so when they are absent :func:`available` reports
  the gap and :func:`embed` refuses rather than inventing a name.

Drop the two files into ``models/face`` (or point ``SATURN_FACE_MODELS`` at a
folder) and identity starts working with no code change.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

# Where a person's descriptors live. Only numbers are ever written.
STORE_VERSION = 1

DETECTOR_NAMES = (
    "yunet.onnx",
    "face_detection_yunet_2023mar.onnx",
    "face_detection_yunet.onnx",
)
RECOGNIZER_NAMES = (
    "sface.onnx",
    "face_recognition_sface_2021dec.onnx",
    "sface_2021dec.onnx",
)

# OpenCV's documented cosine threshold for the SFace model. Below it two faces
# are not the same person; raising it makes identity stricter, lowering it
# looser. Calibrated for their model, so it is not a guess.
MATCH_THRESHOLD = float(os.environ.get("SATURN_FACE_MATCH_THRESHOLD", "0.363"))
DETECT_SCORE = float(os.environ.get("SATURN_FACE_DETECT_SCORE", "0.8"))
EMBEDDING_SIZE = 128

# Three captures is enough to average out a blink and too few to be a chore.
ENROLL_FRAMES = 3

_HAAR_XML = Path(__file__).resolve().parent / "haarcascade_frontalface_default.xml"

_needed_hint = (
    "Identity needs two OpenCV model files: "
    "face_detection_yunet_2023mar.onnx (YuNet) and "
    "face_recognition_sface_2021dec.onnx (SFace). "
    "Put them in models/face inside the app folder, or set SATURN_FACE_MODELS "
    "to the folder that holds them."
)


# ── Models and availability ──────────────────────────────────────────────────

def model_roots() -> list[Path]:
    """Folders searched for the ONNX models, first match wins."""
    roots: list[Path] = []
    override = os.environ.get("SATURN_FACE_MODELS")
    if override:
        roots.append(Path(override).expanduser())
    roots.append(Path(__file__).resolve().parent.parent / "models" / "face")
    try:
        from core.user_paths import get_user_data_dir

        roots.append(get_user_data_dir() / "models" / "face")
    except Exception:
        pass
    return roots


def models() -> dict:
    """Where the detector and recogniser were found, if at all."""
    found = {"detector": None, "recognizer": None}
    for root in model_roots():
        for key, names in (("detector", DETECTOR_NAMES), ("recognizer", RECOGNIZER_NAMES)):
            if found[key]:
                continue
            for name in names:
                candidate = root / name
                try:
                    if candidate.is_file():
                        found[key] = str(candidate)
                        break
                except OSError:
                    continue
    return found


# A model file that exists but will not open is worse than a missing one: the
# capability checks would promise identity and then every identification would
# fail, which the policy reads as "an unrecognised person" rather than as a
# broken install. Results are cached against (size, mtime) so a re-download is
# noticed without restarting the app.
_LOAD_CACHE: dict = {}


def _opens(path: str, kind: str) -> tuple[bool, str]:
    """Whether a model file actually loads, not merely exists."""
    try:
        stat = os.stat(path)
    except OSError as exc:
        return False, f"cannot be read ({exc})"
    if stat.st_size <= 0:
        return False, "is empty"
    key = (stat.st_size, int(stat.st_mtime))
    cached = _LOAD_CACHE.get(path)
    if cached is not None and cached[0] == key:
        return cached[1], cached[2]
    try:
        if kind == "detector":
            _yunet(path, 320, 240, DETECT_SCORE)
        else:
            _sface(path)
    except Exception as exc:
        ok, why = False, f"will not load ({str(exc)[:100]})"
    else:
        ok, why = True, ""
    _LOAD_CACHE[path] = (key, ok, why)
    return ok, why


def available() -> dict:
    """What this machine can actually do, stated without optimism."""
    try:
        import cv2
    except Exception as exc:
        return {"ok": False, "error": "OPENCV_UNAVAILABLE", "message": str(exc)}

    found = models()
    haar = _HAAR_XML.is_file()
    loaded = {"detector": True, "recognizer": True}
    unloadable: list[str] = []
    for kind in ("detector", "recognizer"):
        if not found[kind]:
            continue
        ok, why = _opens(found[kind], kind)
        loaded[kind] = ok
        if not ok:
            unloadable.append(f"{Path(found[kind]).name} {why}")
    identity = bool(found["detector"] and found["recognizer"]
                    and loaded["detector"] and loaded["recognizer"])
    if identity:
        hint = None
    elif unloadable:
        hint = ("The face models are present but do not work: " + "; ".join(unloadable)
                + ". Download them again - an unfinished download looks exactly like "
                "this, and it would make every arrival look unrecognised.")
    else:
        hint = _needed_hint
    return {
        "ok": True,
        "opencv": getattr(cv2, "__version__", "?"),
        "haar": haar,
        "detector": "yunet" if (found["detector"] and loaded["detector"])
                    else ("haar" if haar else None),
        "recognizer": found["recognizer"],
        "identity": identity,
        "load_errors": unloadable,
        "search": [str(root) for root in model_roots()],
        "hint": hint,
    }


# ── Vector maths ─────────────────────────────────────────────────────────────

def normalise(vector) -> list:
    """Unit-length copy of a descriptor, so cosine is a dot product."""
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values))
    if norm <= 0.0:
        # A zero descriptor cannot be compared to anything; keep the shape and
        # let the caller's threshold reject it rather than raising mid-scan.
        return [0.0] * len(values)
    return [v / norm for v in values]


def cosine(a, b) -> float:
    """Cosine similarity of two descriptors, in [-1, 1]."""
    if len(a) != len(b):
        raise ValueError(f"descriptor sizes differ: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def best_match(vector, people: dict, threshold: float | None = None) -> dict:
    """The closest enrolled person to ``vector``, and how close the runner-up is.

    The margin matters when reading the answer aloud: 0.62 against a runner-up
    of 0.60 is a plausible stranger, not a confident sighting.
    """
    limit = MATCH_THRESHOLD if threshold is None else float(threshold)
    scored = []
    for name, record in (people or {}).items():
        embedding = (record or {}).get("embedding") or []
        if len(embedding) != len(vector):
            continue
        scored.append((name, cosine(vector, embedding)))
    if not scored:
        return {"name": None, "score": 0.0, "margin": 0.0, "reason": "NOBODY_ENROLLED"}
    scored.sort(key=lambda item: item[1], reverse=True)
    name, score = scored[0]
    runner_up = scored[1][1] if len(scored) > 1 else 0.0
    if score < limit:
        return {"name": None, "score": score, "margin": score - runner_up,
                "reason": "BELOW_THRESHOLD"}
    return {"name": name, "score": score, "margin": score - runner_up, "reason": None}


# ── Detection ────────────────────────────────────────────────────────────────

_cascade = None
_detector = None
_detector_key = None


def _haar():
    global _cascade
    if _cascade is None:
        import cv2

        if not _HAAR_XML.is_file():
            raise RuntimeError("haarcascade_frontalface_default.xml is missing from core/")
        cascade = cv2.CascadeClassifier(str(_HAAR_XML))
        if cascade.empty():
            raise RuntimeError("the Haar cascade failed to load")
        _cascade = cascade
    return _cascade


def _yunet(path: str, width: int, height: int, score: float):
    """A YuNet detector for this exact frame size, reused while the size holds."""
    global _detector, _detector_key
    import cv2

    key = (path, width, height, score)
    if _detector is not None and _detector_key == key:
        return _detector
    detector = cv2.FaceDetectorYN.create(path, "", (width, height), score, 0.3, 5000)
    _detector, _detector_key = detector, key
    return detector


def detect(image, score_threshold: float | None = None, limit: int = 8) -> list[dict]:
    """Faces in a BGR frame, largest first.

    Returns ``{"box": (x, y, w, h), "score": float | None, "row": list | None}``.
    ``row`` is YuNet's 15-value detection, which the recogniser needs to align
    the crop; it is None when only the Haar cascade could see the face.
    """
    import cv2

    if image is None:
        return []
    height, width = image.shape[:2]
    if width < 1 or height < 1:
        return []

    found = models()
    results: list[dict] = []
    if found["detector"]:
        threshold = DETECT_SCORE if score_threshold is None else float(score_threshold)
        try:
            detector = _yunet(found["detector"], width, height, threshold)
            _retval, faces = detector.detect(image)
        except Exception:
            faces = None
        for row in (faces if faces is not None else []):
            x, y, w, h = (int(round(float(v))) for v in row[:4])
            if w < 8 or h < 8:
                continue
            results.append({
                "box": (max(0, x), max(0, y), w, h),
                "score": float(row[14]) if len(row) > 14 else None,
                "row": [float(v) for v in row],
            })
    if not results and _HAAR_XML.is_file():
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        boxes = _haar().detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=5, minSize=(48, 48)
        )
        for x, y, w, h in (boxes if boxes is not None else []):
            results.append({"box": (int(x), int(y), int(w), int(h)),
                            "score": None, "row": None})

    results.sort(key=lambda item: item["box"][2] * item["box"][3], reverse=True)
    return results[:max(1, int(limit))]


# ── Descriptors ──────────────────────────────────────────────────────────────

_recognizer = None
_recognizer_path = None


def _sface(path: str):
    global _recognizer, _recognizer_path
    import cv2

    if _recognizer is not None and _recognizer_path == path:
        return _recognizer
    recognizer = cv2.FaceRecognizerSF.create(path, "")
    _recognizer, _recognizer_path = recognizer, path
    return recognizer


def embed(image, face: dict) -> dict:
    """A unit descriptor for one detected face, or a stated reason it has none."""
    found = models()
    if not found["recognizer"]:
        return {"ok": False, "error": "FACE_MODEL_MISSING", "message": _needed_hint}
    row = (face or {}).get("row")
    if not row:
        # Haar found a face but cannot place the landmarks SFace aligns on.
        # Cropping on the bounding box alone would produce a descriptor that
        # does not line up with anything enrolled, so this is a hard stop.
        return {
            "ok": False,
            "error": "FACE_LANDMARKS_MISSING",
            "message": (
                "The face was detected but not aligned, so it cannot be compared "
                "with anyone. " + _needed_hint
            ),
        }
    try:
        import cv2
        import numpy as np

        recognizer = _sface(found["recognizer"])
        # ``detect`` keeps the detection row as a plain list so snapshots stay
        # JSON-safe, but alignCrop's Python binding rejects anything that is not
        # a real ndarray - it fails with "face_box is not a numpy array" and the
        # whole identity tier silently produces nothing.
        box = row if isinstance(row, np.ndarray) else np.array(row, dtype=np.float32)
        aligned = recognizer.alignCrop(image, box.reshape(-1))
        feature = recognizer.feature(aligned)
        values = feature.flatten().tolist()
    except Exception as exc:
        return {"ok": False, "error": "FACE_EMBED_FAILED", "message": str(exc)}
    if len(values) != EMBEDDING_SIZE:
        return {"ok": False, "error": "FACE_EMBED_FAILED",
                "message": f"expected {EMBEDDING_SIZE} values, got {len(values)}"}
    return {"ok": True, "embedding": normalise(values)}


# ── Store ────────────────────────────────────────────────────────────────────

def store_path() -> Path:
    override = os.environ.get("SATURN_FACE_STORE")
    if override:
        return Path(override).expanduser()
    from core.user_paths import get_user_data_dir

    return get_user_data_dir() / "face_identity.json"


def load_store(path=None) -> dict:
    """Enrolled people, dropping any record that is not shaped as expected."""
    target = Path(path) if path else store_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": STORE_VERSION, "people": {}}
    except (OSError, ValueError):
        # A corrupt file is reported by its absence of people, not by raising
        # into the middle of a camera capture.
        return {"version": STORE_VERSION, "people": {}}

    people = {}
    for name, record in (raw.get("people") or {}).items():
        if not isinstance(name, str) or not name.strip():
            continue
        embedding = (record or {}).get("embedding")
        if not isinstance(embedding, list) or len(embedding) != EMBEDDING_SIZE:
            continue
        try:
            people[name] = {
                "embedding": [float(v) for v in embedding],
                "samples": int(record.get("samples") or 0),
                "created": str(record.get("created") or ""),
            }
        except (TypeError, ValueError):
            continue
    return {"version": STORE_VERSION, "people": people}


def save_store(store: dict, path=None) -> Path:
    """Write the store atomically, so a crash cannot truncate it."""
    target = Path(path) if path else store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": STORE_VERSION,
               "people": (store or {}).get("people") or {}}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(temporary, target)
    return target


def people(path=None) -> list[str]:
    return sorted(load_store(path)["people"].keys())


def forget(name: str, path=None) -> bool:
    store = load_store(path)
    wanted = str(name or "").strip()
    removed = False
    for key in list(store["people"].keys()):
        if key.lower() == wanted.lower():
            del store["people"][key]
            removed = True
    if removed:
        save_store(store, path)
    return removed


# ── Capture, enroll, identify ────────────────────────────────────────────────

def open_camera(index: int = 0, warmup: int = 6):
    """One BGR frame from the default camera, or a RuntimeError that says why."""
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError(f"OpenCV is not available: {exc}") from exc

    if os.name == "nt":
        capture = cv2.VideoCapture(int(index), cv2.CAP_DSHOW)
    else:
        capture = cv2.VideoCapture(int(index))
    if not capture or not capture.isOpened():
        try:
            capture.release()
        except Exception:
            pass
        raise RuntimeError(
            f"Camera {index} would not open. Close any app that is holding it "
            "and try again."
        )
    try:
        frame = None
        for _ in range(max(1, int(warmup))):
            # The first frames after opening are dark or empty, so the last of
            # the warmup run is the one worth returning.
            ok, candidate = capture.read()
            if ok and candidate is not None and candidate.size:
                frame = candidate
        if frame is None:
            raise RuntimeError(f"Camera {index} opened but returned no picture.")
        return frame
    finally:
        try:
            capture.release()
        except Exception:
            pass


def capture_frames(count: int = ENROLL_FRAMES, index: int = 0,
                   require_face: bool = False, deadline: float = 6.0) -> list:
    """Several frames from one open camera, so enrollment is a single capture.

    ``require_face`` keeps reading until ``count`` frames holding exactly one
    face arrive, or ``deadline`` seconds pass. Taking the next N frames blindly
    fails whenever the person blinks or is not squarely in view yet, and the
    enrollment then reports "no face" for a reason that has nothing to do with
    the face. Exactly one, because enrollment averages one descriptor per frame
    and a second person would be averaged into the record.
    """
    frames = []
    try:
        import cv2
    except Exception as exc:
        raise RuntimeError(f"OpenCV is not available: {exc}") from exc

    capture = cv2.VideoCapture(int(index), cv2.CAP_DSHOW) if os.name == "nt" \
        else cv2.VideoCapture(int(index))
    if not capture or not capture.isOpened():
        try:
            capture.release()
        except Exception:
            pass
        raise RuntimeError(
            f"Camera {index} would not open. Close any app that is holding it "
            "and try again."
        )
    try:
        for _ in range(6):
            capture.read()  # settle exposure
        limit = max(1, int(count))
        if require_face:
            give_up = time.time() + max(0.1, float(deadline))
            while len(frames) < limit and time.time() < give_up:
                ok, frame = capture.read()
                if not ok or frame is None or not getattr(frame, "size", 0):
                    continue
                if len(detect(frame)) == 1:
                    frames.append(frame)
        else:
            for _ in range(limit):
                ok, frame = capture.read()
                if ok and frame is not None and frame.size:
                    frames.append(frame)
        if not frames:
            if require_face:
                raise RuntimeError(
                    f"Camera {index} did not see one clear face in "
                    f"{float(deadline):.0f} seconds. Look straight at it in a lit "
                    "room and try again."
                )
            raise RuntimeError(f"Camera {index} opened but returned no picture.")
        return frames
    finally:
        try:
            capture.release()
        except Exception:
            pass


def enroll(name: str, frames: list, path=None) -> dict:
    """Average one descriptor per usable frame into a person's record."""
    wanted = str(name or "").strip()
    if not wanted:
        return {"ok": False, "error": "NAME_REQUIRED",
                "message": "Tell me whose face this is - a name to remember it by."}
    if not models()["recognizer"]:
        return {"ok": False, "error": "FACE_MODEL_MISSING", "message": _needed_hint}

    vectors = []
    rejected = []
    for frame in frames or []:
        faces = detect(frame)
        if len(faces) != 1:
            rejected.append("no face" if not faces else f"{len(faces)} faces")
            continue
        result = embed(frame, faces[0])
        if result.get("ok"):
            vectors.append(result["embedding"])
        else:
            rejected.append(result.get("message") or result.get("error"))
    if not vectors:
        return {"ok": False, "error": "NO_FACE",
                "message": "I could not get a clear single face. Look straight at "
                           "the camera in a lit room.",
                "rejected": rejected}

    size = len(vectors[0])
    averaged = [sum(vector[i] for vector in vectors) / len(vectors)
                for i in range(size)]
    store = load_store(path)
    store["people"][wanted] = {
        "embedding": normalise(averaged),
        "samples": len(vectors),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    saved = save_store(store, path)
    return {"ok": True, "name": wanted, "samples": len(vectors),
            "rejected": rejected, "store": str(saved)}


def identify(frames: list, path=None, threshold: float | None = None) -> dict:
    """Who is in these frames. Never returns a name below the threshold."""
    found_models = models()
    if not found_models["recognizer"]:
        return {"ok": False, "error": "FACE_MODEL_MISSING", "message": _needed_hint}
    loaded, why = _opens(found_models["recognizer"], "recognizer")
    if not loaded:
        return {"ok": False, "error": "FACE_MODEL_BROKEN",
                "message": f"The face recogniser {why}; download it again."}

    store = load_store(path)
    if not store["people"]:
        return {"ok": False, "error": "NOBODY_ENROLLED",
                "message": "Nobody is enrolled yet, so there is no name to match."}

    best = None
    faces_seen = 0
    started = time.perf_counter()
    for frame in frames or []:
        faces = detect(frame)
        faces_seen = max(faces_seen, len(faces))
        if not faces:
            continue
        result = embed(frame, faces[0])
        if not result.get("ok"):
            continue
        match = best_match(result["embedding"], store["people"], threshold)
        if best is None or match["score"] > best["score"]:
            best = match
    elapsed = int((time.perf_counter() - started) * 1000)

    if best is None:
        return {"ok": True, "name": None, "faces": faces_seen,
                "reason": "NO_FACE" if not faces_seen else "FACE_UNREADABLE",
                "ms": elapsed}
    return {"ok": True, "name": best["name"], "score": round(best["score"], 4),
            "margin": round(best["margin"], 4), "reason": best["reason"],
            "faces": faces_seen, "ms": elapsed,
            "threshold": MATCH_THRESHOLD if threshold is None else float(threshold)}


def recognise_all(frames, path=None, threshold: float | None = None) -> dict:
    """Name every face in the frames, not just the largest one.

    :func:`identify` answers "whose face is this" for a single dominant person.
    A glance at the camera can hold the owner *and* a friend, or a stranger, so
    this walks each detected face and returns a roster: the distinct people
    recognised (deduped to their best sighting), how many faces were in frame,
    and how many matched nobody. A name is attached only when the match clears
    the threshold, and a thin margin is kept but flagged ``confident=False`` so
    the caller can choose to describe a person without asserting their name.

    Local only - every descriptor is compared on this machine, as always.
    """
    found_models = models()
    if not found_models["recognizer"]:
        return {"ok": False, "error": "FACE_MODEL_MISSING", "message": _needed_hint}
    store = load_store(path)
    if not store["people"]:
        return {"ok": True, "faces": 0, "recognised": [], "confident": [],
                "unknown": 0, "reason": "NOBODY_ENROLLED"}

    limit = MATCH_THRESHOLD if threshold is None else float(threshold)
    best_by_name: dict = {}
    face_total = 0
    started = time.perf_counter()
    for frame in frames or []:
        faces = detect(frame)
        face_total = max(face_total, len(faces))
        for one in faces:
            result = embed(frame, one)
            if not result.get("ok"):
                continue
            match = best_match(result["embedding"], store["people"], limit)
            name = match.get("name")
            if not name:
                continue
            entry = {"name": name,
                     "score": round(float(match.get("score") or 0.0), 4),
                     "margin": round(float(match.get("margin") or 0.0), 4),
                     "confident": float(match.get("margin") or 0.0) >= 0.05}
            key = name.lower()
            prev = best_by_name.get(key)
            if prev is None or entry["score"] > prev["score"]:
                best_by_name[key] = entry

    recognised = sorted(best_by_name.values(), key=lambda r: r["score"], reverse=True)
    confident = [r for r in recognised if r["confident"]]
    unknown = max(0, face_total - len(recognised))
    elapsed = int((time.perf_counter() - started) * 1000)
    return {"ok": True, "faces": face_total, "recognised": recognised,
            "confident": confident, "unknown": unknown, "ms": elapsed,
            "threshold": MATCH_THRESHOLD if threshold is None else float(threshold)}


def status(path=None) -> dict:
    """Everything the assistant needs to explain what face identity can do here."""
    info = available()
    enrolled = people(path)
    return {
        "available": info,
        "enrolled": enrolled,
        "threshold": MATCH_THRESHOLD,
        "store": str(Path(path) if path else store_path()),
        "local_only": True,
    }
