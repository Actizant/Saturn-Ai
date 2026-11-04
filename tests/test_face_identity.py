"""Face identity: the maths, the store, and the refusals.

No test here needs a camera, a face, or an ONNX model. The parts that can be
proven without one -- normalisation, matching, thresholds, the descriptor store,
and every "I can't do that" path -- are proven below.
"""

import json

import numpy as np
import pytest

from core import face_id


def _unit(size=4, seed=1):
    rng = np.random.default_rng(seed)
    values = rng.normal(size=size)
    return face_id.normalise(values.tolist())


def _axis(index, size=None):
    """A one-hot unit descriptor, for building exactly-known similarities."""
    size = face_id.EMBEDDING_SIZE if size is None else size
    values = [0.0] * size
    values[index] = 1.0
    return values


def _store_with(tmp_path, name, embedding, **extra):
    store = {"version": 1, "people": {name: {"embedding": embedding,
                                             "samples": extra.get("samples", 1),
                                             "created": "2026-01-01T00:00:00"}}}
    path = tmp_path / "faces.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    return path


# ── Maths ────────────────────────────────────────────────────────────────────

def test_normalise_returns_a_unit_vector():
    vector = face_id.normalise([3.0, 4.0])

    assert vector == pytest.approx([0.6, 0.8])
    assert sum(v * v for v in vector) == pytest.approx(1.0)


def test_normalise_keeps_the_shape_of_a_zero_vector():
    # A dead descriptor still has to line up with the others rather than crash.
    assert face_id.normalise([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


def test_cosine_of_identical_descriptors_is_one():
    vector = _unit()
    assert face_id.cosine(vector, vector) == pytest.approx(1.0)


def test_cosine_of_opposite_descriptors_is_minus_one():
    vector = _unit()
    flipped = [-v for v in vector]
    assert face_id.cosine(vector, flipped) == pytest.approx(-1.0)


def test_cosine_of_orthogonal_descriptors_is_zero():
    assert face_id.cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_cosine_of_a_dead_descriptor_is_zero_not_an_error():
    assert face_id.cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_rejects_descriptors_of_different_sizes():
    with pytest.raises(ValueError):
        face_id.cosine([1.0, 0.0], [1.0, 0.0, 0.0])


# ── Matching ─────────────────────────────────────────────────────────────────

def test_best_match_names_the_closest_person():
    target = _unit(seed=1)
    people = {"Ada": {"embedding": target},
              "Sam": {"embedding": _unit(seed=2)}}

    match = face_id.best_match(target, people, threshold=0.36)

    assert match["name"] == "Ada"
    assert match["score"] == pytest.approx(1.0)


def test_best_match_refuses_below_the_threshold():
    target = _unit(seed=1)
    other = face_id.normalise([-v for v in target])
    people = {"Ada": {"embedding": other}}

    match = face_id.best_match(target, people, threshold=0.36)

    assert match["name"] is None
    assert match["reason"] == "BELOW_THRESHOLD"


def test_best_match_reports_the_margin_over_the_runner_up():
    target = _unit(seed=3)
    close = face_id.normalise([v + 0.05 for v in target])
    people = {"Ada": {"embedding": target}, "Sam": {"embedding": close}}

    match = face_id.best_match(target, people, threshold=0.36)

    assert match["name"] == "Ada"
    assert 0.0 < match["margin"] < 0.05


def test_best_match_says_when_nobody_is_enrolled():
    match = face_id.best_match(_unit(), {}, threshold=0.36)

    assert match["name"] is None
    assert match["reason"] == "NOBODY_ENROLLED"


def test_best_match_ignores_records_of_the_wrong_size():
    match = face_id.best_match(_unit(4), {"Ada": {"embedding": [0.1, 0.2]}})

    assert match["name"] is None


# ── Discovery ────────────────────────────────────────────────────────────────

def test_available_reports_the_haar_fallback_when_no_models_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])

    info = face_id.available()

    assert info["ok"] is True
    assert info["detector"] == "haar"
    assert info["identity"] is False
    assert "yunet" in (info["hint"] or "").lower()


def test_available_finds_models_by_their_published_names(tmp_path, monkeypatch):
    (tmp_path / "face_detection_yunet_2023mar.onnx").write_bytes(b"stub")
    (tmp_path / "face_recognition_sface_2021dec.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    # This test is about filenames. Whether the bytes open is a separate check,
    # covered by the tests below.
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))

    info = face_id.available()

    assert info["detector"] == "yunet"
    assert info["identity"] is True
    assert info["hint"] is None


def test_a_truncated_model_is_not_reported_as_available(tmp_path, monkeypatch):
    """A half-finished download must not be able to arm the perimeter.

    Identity is claimed only when the bytes actually open. Files that merely
    exist would make every arrival look like a stranger, and the policy would
    then act on that guess.
    """
    (tmp_path / "face_detection_yunet_2023mar.onnx").write_bytes(b"stub")
    (tmp_path / "face_recognition_sface_2021dec.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    face_id._LOAD_CACHE.clear()

    info = face_id.available()

    assert info["identity"] is False
    assert info["load_errors"]
    assert "do not work" in info["hint"]
    # The detector could not load either, so the fallback is stated honestly.
    assert info["detector"] == "haar"


def test_an_empty_model_file_fails_without_being_opened(tmp_path, monkeypatch):
    (tmp_path / "yunet.onnx").write_bytes(b"")
    (tmp_path / "sface.onnx").write_bytes(b"")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    face_id._LOAD_CACHE.clear()
    opened = []
    monkeypatch.setattr(face_id, "_yunet", lambda *a, **k: opened.append("detector"))
    monkeypatch.setattr(face_id, "_sface", lambda *a, **k: opened.append("recognizer"))

    info = face_id.available()

    assert info["identity"] is False
    assert opened == []
    assert "is empty" in info["hint"]


def test_a_loadable_model_is_checked_once_not_every_tick(tmp_path, monkeypatch):
    # The sensor asks what is possible on every sample; opening two ONNX files
    # each time would be absurd.
    (tmp_path / "yunet.onnx").write_bytes(b"stub")
    (tmp_path / "sface.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    face_id._LOAD_CACHE.clear()
    opened = []
    monkeypatch.setattr(face_id, "_yunet",
                        lambda *a, **k: opened.append("detector") or object())
    monkeypatch.setattr(face_id, "_sface",
                        lambda *a, **k: opened.append("recognizer") or object())

    first = face_id.available()
    second = face_id.available()

    assert first["identity"] is True
    assert second["identity"] is True
    assert opened == ["detector", "recognizer"]


def test_replacing_a_model_makes_it_be_checked_again(tmp_path, monkeypatch):
    model = tmp_path / "sface.onnx"
    model.write_bytes(b"stub")
    (tmp_path / "yunet.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    face_id._LOAD_CACHE.clear()
    opens = []
    monkeypatch.setattr(face_id, "_yunet", lambda *a, **k: object())
    monkeypatch.setattr(face_id, "_sface", lambda *a, **k: opens.append(1) or object())

    face_id.available()
    model.write_bytes(b"a fresh full download")   # the re-download
    face_id.available()

    assert len(opens) == 2


def test_identify_says_the_model_is_broken_not_the_face(tmp_path, monkeypatch):
    (tmp_path / "sface.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    face_id._LOAD_CACHE.clear()

    result = face_id.identify([None])

    assert result["ok"] is False
    assert result["error"] == "FACE_MODEL_BROKEN"


def test_the_first_model_root_that_has_a_file_wins(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    full = tmp_path / "full"
    full.mkdir()
    (full / "yunet.onnx").write_bytes(b"stub")
    monkeypatch.setattr(face_id, "model_roots", lambda: [empty, full])

    assert face_id.models()["detector"] == str(full / "yunet.onnx")


# ── Detection ────────────────────────────────────────────────────────────────

def test_detect_finds_nothing_in_a_blank_frame():
    assert face_id.detect(np.zeros((240, 320, 3), dtype=np.uint8)) == []


def test_detect_treats_a_missing_image_as_no_faces():
    assert face_id.detect(None) == []


def test_a_detector_model_that_cannot_load_falls_back_to_haar(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": str(tmp_path / "absent.onnx"),
                                 "recognizer": None})

    # A blank frame plus a detector that raises: the cascade still answers, and
    # the answer is still no faces. The point is that nothing propagates.
    assert face_id.detect(np.zeros((120, 160, 3), dtype=np.uint8)) == []


# ── Embedding refusals ───────────────────────────────────────────────────────

def test_embed_without_a_recogniser_explains_which_models_to_add(monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": None, "recognizer": None})

    result = face_id.embed(None, {"row": [0.0] * 15})

    assert result["ok"] is False
    assert result["error"] == "FACE_MODEL_MISSING"
    assert "sface" in result["message"].lower()


def test_embed_without_landmarks_refuses_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": None,
                                 "recognizer": str(tmp_path / "sface.onnx")})

    result = face_id.embed(None, {"row": None})

    assert result["ok"] is False
    assert result["error"] == "FACE_LANDMARKS_MISSING"


def test_embed_hands_aligncrop_a_numpy_array(tmp_path, monkeypatch):
    """detect() keeps the row as a JSON-safe list, but the binding refuses one.

    Passing that list straight through made every embedding fail with "face_box
    is not a numpy array", which killed enrolment and identity silently - the
    models were fine, the call was not.
    """
    import numpy as np

    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": None,
                                 "recognizer": str(tmp_path / "sface.onnx")})
    seen = {}

    class FakeRecognizer:
        def alignCrop(self, image, box):
            seen["type"] = type(box).__name__
            seen["shape"] = getattr(box, "shape", None)
            return "aligned"

        def feature(self, aligned):
            return np.ones((1, face_id.EMBEDDING_SIZE), dtype=np.float32)

    monkeypatch.setattr(face_id, "_sface", lambda path: FakeRecognizer())

    result = face_id.embed(object(), {"row": [0.0] * 15})

    assert result["ok"] is True
    assert seen["type"] == "ndarray"
    assert seen["shape"] == (15,)
    assert len(result["embedding"]) == face_id.EMBEDDING_SIZE


# ── Capture ──────────────────────────────────────────────────────────────────

class _Frame:
    def __init__(self, tag):
        self.tag = tag
        self.size = 640 * 480


class _FakeCapture:
    """Minimal stand-in for cv2.VideoCapture, including the exposure settle."""

    def __init__(self, frames, settle=6):
        self._frames = list(frames)
        self._settle = settle
        self._read = 0
        self.released = False

    def isOpened(self):
        return True

    def read(self):
        if self._read < self._settle:
            self._read += 1
            return True, _Frame("settle")
        position = self._read - self._settle
        if position < len(self._frames):
            self._read += 1
            return True, self._frames[position]
        return False, None

    def release(self):
        self.released = True


def _install_capture(monkeypatch, capture):
    import sys
    import types

    module = types.ModuleType("cv2")
    module.VideoCapture = lambda *a, **k: capture
    module.CAP_DSHOW = 700
    monkeypatch.setitem(sys.modules, "cv2", module)


def test_capture_frames_waits_for_a_usable_face(monkeypatch):
    # Blindly taking the next N frames reported "no face" on a face plainly in
    # view, so enrolment now waits for frames that actually hold one.
    capture = _FakeCapture([_Frame("empty"), _Frame("empty"), _Frame("face"),
                            _Frame("face"), _Frame("face")])
    _install_capture(monkeypatch, capture)
    monkeypatch.setattr(face_id, "detect",
                        lambda frame: [{}] if frame.tag == "face" else [])

    frames = face_id.capture_frames(2, require_face=True, deadline=2.0)

    assert [frame.tag for frame in frames] == ["face", "face"]
    assert capture.released is True


def test_capture_frames_explains_when_no_face_shows_up(monkeypatch):
    capture = _FakeCapture([_Frame("empty")] * 4)
    _install_capture(monkeypatch, capture)
    monkeypatch.setattr(face_id, "detect", lambda frame: [])

    with pytest.raises(RuntimeError) as caught:
        face_id.capture_frames(3, require_face=True, deadline=0.1)

    assert "face" in str(caught.value).lower()
    assert capture.released is True


def test_capture_frames_still_takes_blind_frames_by_default(monkeypatch):
    # "who is this" asks for two frames and must not wait for a pose it likes.
    capture = _FakeCapture([_Frame("empty"), _Frame("face")])
    _install_capture(monkeypatch, capture)
    monkeypatch.setattr(face_id, "detect", lambda frame: [])

    frames = face_id.capture_frames(2)

    assert len(frames) == 2


# ── Store ────────────────────────────────────────────────────────────────────

def test_loading_a_missing_store_gives_no_people(tmp_path):
    assert face_id.load_store(tmp_path / "nothing.json")["people"] == {}


def test_a_corrupt_store_reads_as_empty_instead_of_raising(tmp_path):
    path = tmp_path / "faces.json"
    path.write_text("{not json", encoding="utf-8")

    assert face_id.load_store(path)["people"] == {}


def test_a_record_with_the_wrong_size_is_dropped(tmp_path):
    path = tmp_path / "faces.json"
    path.write_text(json.dumps({"people": {
        "Ada": {"embedding": [0.1, 0.2]},
        "Sam": {"embedding": face_id.normalise([1.0] * face_id.EMBEDDING_SIZE)},
    }}), encoding="utf-8")

    assert face_id.people(path) == ["Sam"]


def test_a_saved_store_round_trips(tmp_path):
    path = tmp_path / "faces.json"
    vector = face_id.normalise([1.0] * face_id.EMBEDDING_SIZE)

    face_id.save_store({"people": {"Ada": {"embedding": vector, "samples": 3,
                                           "created": "2026-01-01T00:00:00"}}}, path)
    loaded = face_id.load_store(path)

    assert loaded["people"]["Ada"]["embedding"] == pytest.approx(vector)
    assert loaded["people"]["Ada"]["samples"] == 3
    # Atomic write: the temporary file is gone once the replace landed.
    assert not (tmp_path / "faces.json.tmp").exists()


def test_people_are_listed_in_a_stable_order(tmp_path):
    path = tmp_path / "faces.json"
    vector = face_id.normalise([1.0] * face_id.EMBEDDING_SIZE)
    face_id.save_store({"people": {
        "sam": {"embedding": vector},
        "Ada": {"embedding": vector},
    }}, path)

    assert face_id.people(path) == ["Ada", "sam"]


def test_forget_ignores_case_and_reports_a_miss(tmp_path):
    path = _store_with(tmp_path, "Ada", face_id.normalise([1.0] * face_id.EMBEDDING_SIZE))

    assert face_id.forget("ada", path) is True
    assert face_id.people(path) == []
    assert face_id.forget("nobody", path) is False


def test_the_store_path_can_be_moved_by_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SATURN_FACE_STORE", str(tmp_path / "custom.json"))
    assert face_id.store_path() == tmp_path / "custom.json"


# ── Enrolment ────────────────────────────────────────────────────────────────

def test_enrolment_needs_a_name(tmp_path):
    result = face_id.enroll("   ", [object()], path=tmp_path / "faces.json")

    assert result["ok"] is False
    assert result["error"] == "NAME_REQUIRED"


def test_enrolment_without_a_recogniser_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": None, "recognizer": None})

    result = face_id.enroll("Ada", [object()], path=tmp_path / "faces.json")

    assert result["ok"] is False
    assert result["error"] == "FACE_MODEL_MISSING"


def test_enrolment_averages_clear_frames(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [{"box": (0, 0, 40, 40),
                                                               "row": [0.0] * 15}])
    vectors = iter([_axis(0), _axis(1)])
    monkeypatch.setattr(face_id, "embed",
                        lambda frame, face: {"ok": True, "embedding": next(vectors)})
    path = tmp_path / "faces.json"

    result = face_id.enroll("Ada", [object(), object()], path=path)

    assert result["ok"] is True
    assert result["samples"] == 2
    stored = face_id.load_store(path)["people"]["Ada"]
    # The average of two one-hot descriptors, re-normalised, sits between them.
    assert stored["embedding"][0] == pytest.approx(2 ** -0.5)
    assert stored["embedding"][1] == pytest.approx(2 ** -0.5)
    assert sum(v * v for v in stored["embedding"]) == pytest.approx(1.0)


def test_enrolment_says_why_frames_were_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [])

    result = face_id.enroll("Ada", [object()], path=tmp_path / "faces.json")

    assert result["ok"] is False
    assert result["error"] == "NO_FACE"
    assert result["rejected"] == ["no face"]


def test_two_faces_in_one_frame_are_not_enrolled(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [{"row": [0.0] * 15},
                                                               {"row": [0.0] * 15}])

    result = face_id.enroll("Ada", [object()], path=tmp_path / "faces.json")

    assert result["rejected"] == ["2 faces"]


# ── Identification ───────────────────────────────────────────────────────────

def test_identification_without_a_recogniser_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": None, "recognizer": None})

    result = face_id.identify([object()], path=tmp_path / "faces.json")

    assert result["ok"] is False
    assert result["error"] == "FACE_MODEL_MISSING"


def test_identification_with_nobody_enrolled_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))

    result = face_id.identify([object()], path=tmp_path / "faces.json")

    assert result["ok"] is False
    assert result["error"] == "NOBODY_ENROLLED"


def test_identification_names_the_person_it_matches(tmp_path, monkeypatch):
    vector = face_id.normalise([1.0] * face_id.EMBEDDING_SIZE)
    path = _store_with(tmp_path, "Ada", vector)
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [{"row": [0.0] * 15}])
    monkeypatch.setattr(face_id, "embed",
                        lambda frame, face: {"ok": True, "embedding": vector})

    result = face_id.identify([object()], path=path)

    assert result["name"] == "Ada"
    assert result["score"] == pytest.approx(1.0)
    assert result["reason"] is None


def test_identification_withholds_a_name_below_the_threshold(tmp_path, monkeypatch):
    path = _store_with(tmp_path, "Ada", _axis(0))
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [{"row": [0.0] * 15}])
    # A descriptor orthogonal to Ada's: cosine 0, far under any real threshold.
    monkeypatch.setattr(face_id, "embed",
                        lambda frame, face: {"ok": True, "embedding": _axis(1)})

    result = face_id.identify([object()], path=path)

    assert result["name"] is None
    assert result["reason"] == "BELOW_THRESHOLD"
    assert result["score"] == pytest.approx(0.0)
    assert result["threshold"] == pytest.approx(face_id.MATCH_THRESHOLD)


def test_identification_reports_an_empty_view(tmp_path, monkeypatch):
    path = _store_with(tmp_path, "Ada", face_id.normalise([1.0] * face_id.EMBEDDING_SIZE))
    monkeypatch.setattr(face_id, "models",
                        lambda: {"detector": "yunet", "recognizer": "sface"})
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "detect", lambda frame, **kw: [])

    result = face_id.identify([object()], path=path)

    assert result["ok"] is True
    assert result["reason"] == "NO_FACE"
    assert result["faces"] == 0


def test_status_reports_whether_naming_is_possible(tmp_path, monkeypatch):
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])

    report = face_id.status(path=tmp_path / "faces.json")

    assert report["local_only"] is True
    assert report["available"]["identity"] is False
    assert report["enrolled"] == []


# ── The tools ────────────────────────────────────────────────────────────────

def _no_camera(*args, **kwargs):
    raise AssertionError("the camera must not open without the identity models")


def test_who_is_this_refuses_without_opening_the_camera(tmp_path, monkeypatch):
    from actions import face_identity as tools

    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    monkeypatch.setattr(face_id, "capture_frames", _no_camera)

    answer = tools.who_is_this({})

    assert "YuNet" in answer
    assert "SFace" in answer
    assert "Ada" not in answer


def test_remember_face_refuses_without_opening_the_camera(tmp_path, monkeypatch):
    from actions import face_identity as tools

    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    monkeypatch.setattr(face_id, "capture_frames", _no_camera)

    answer = tools.remember_face({"name": "Ada"})

    assert "ONNX" in answer


def test_remember_face_asks_for_a_name_first():
    from actions import face_identity as tools

    assert "name" in tools.remember_face({}).lower()


def test_status_says_when_will_not_guess_a_name(tmp_path, monkeypatch):
    from actions import face_identity as tools

    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    monkeypatch.setattr(face_id, "store_path", lambda: tmp_path / "faces.json")

    answer = tools.face_status({})

    assert "haar" in answer
    assert "no name will be guessed" in answer


def test_forget_reports_a_missing_name(tmp_path, monkeypatch):
    from actions import face_identity as tools

    monkeypatch.setattr(face_id, "store_path", lambda: tmp_path / "faces.json")

    assert "nobody called Ada" in tools.forget_face({"name": "Ada"})


def _identity_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(face_id, "model_roots", lambda: [tmp_path])
    (tmp_path / "yunet.onnx").write_bytes(b"stub")
    (tmp_path / "sface.onnx").write_bytes(b"stub")
    # Stub bytes would not really open, and available() now says so. These tests
    # are about what the tool says once identity works, so the load check is
    # stubbed to the answer that simulates a good install.
    monkeypatch.setattr(face_id, "_opens", lambda path, kind: (True, ""))
    monkeypatch.setattr(face_id, "capture_frames", lambda count=2: [object()])
    monkeypatch.setattr(face_id, "store_path", lambda: tmp_path / "faces.json")


def test_who_is_this_states_the_match_score(tmp_path, monkeypatch):
    from actions import face_identity as tools

    _identity_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(face_id, "identify",
                        lambda frames, **kw: {"ok": True, "name": "Ada",
                                              "score": 0.71, "margin": 0.3,
                                              "reason": None})

    assert tools.who_is_this({}) == "That looks like Ada. Match 0.71."


def test_who_is_this_hedges_a_thin_margin(tmp_path, monkeypatch):
    from actions import face_identity as tools

    _identity_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(face_id, "identify",
                        lambda frames, **kw: {"ok": True, "name": "Ada",
                                              "score": 0.62, "margin": 0.01,
                                              "reason": None})

    answer = tools.who_is_this({})

    assert "could be Ada" in answer
    assert "not a clear match" in answer


def test_who_is_this_states_a_refusal_with_the_numbers(tmp_path, monkeypatch):
    from actions import face_identity as tools

    _identity_ready(monkeypatch, tmp_path)
    monkeypatch.setattr(face_id, "identify",
                        lambda frames, **kw: {"ok": True, "name": None,
                                              "score": 0.21, "margin": 0.0,
                                              "reason": "BELOW_THRESHOLD",
                                              "faces": 1, "threshold": 0.363})

    answer = tools.who_is_this({})

    assert "don't recognise" in answer
    assert "0.21" in answer
    assert "0.36" in answer

