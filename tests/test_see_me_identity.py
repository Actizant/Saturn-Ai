"""The 'see me' identity fuse: recognise every face, then phrase the glance.

Covers the two new pieces without a camera or ONNX model:
* face_id.recognise_all - name each face, dedup, count strangers, flag thin matches.
* screen_processor._identity_context - how that roster becomes an instruction.
"""

import pytest

from core import face_id

# screen_processor imports cv2 at module load; on some interpreters that import
# fails for reasons unrelated to this logic, so the pure phrasing tests skip
# there instead of breaking collection. face_id imports cv2 lazily and always
# loads.
try:
    from actions.screen_processor import _identity_context
except Exception:  # pragma: no cover - environment dependent
    _identity_context = None

requires_ctx = pytest.mark.skipif(
    _identity_context is None, reason="cv2 unavailable in this interpreter")

OWNER = "Shreshtha"


def _axis(index, size=None):
    size = face_id.EMBEDDING_SIZE if size is None else size
    values = [0.0] * size
    values[index] = 1.0
    return values


def _enrolled_store(tmp_path, people):
    import json
    store = {"version": 1,
             "people": {name: {"embedding": emb, "samples": 1,
                               "created": "2026-01-01T00:00:00"}
                        for name, emb in people.items()}}
    path = tmp_path / "faces.json"
    path.write_text(json.dumps(store), encoding="utf-8")
    return path


# ── recognise_all ────────────────────────────────────────────────────────────
def _wire(monkeypatch, vecs):
    """Fake one frame holding the given face descriptors, model present."""
    monkeypatch.setattr(face_id, "models",
                        lambda: {"recognizer": "x", "detector": "x"})
    faces = [{"box": (i, i, 40, 40), "row": [0.0] * 15} for i, _ in enumerate(vecs)]
    monkeypatch.setattr(face_id, "detect", lambda frame, **k: list(faces))
    monkeypatch.setattr(face_id, "embed",
                        lambda frame, one: {"ok": True,
                                            "embedding": vecs[faces.index(one)]})


def test_recognise_all_names_each_person_and_counts_the_stranger(tmp_path, monkeypatch):
    path = _enrolled_store(tmp_path, {"Shreshtha": _axis(0), "Priya": _axis(1)})
    # three faces: owner, friend, someone not enrolled at all
    _wire(monkeypatch, [_axis(0), _axis(1), _axis(2)])

    out = face_id.recognise_all([object()], path=path)

    assert out["ok"] is True
    assert out["faces"] == 3
    assert {r["name"] for r in out["recognised"]} == {"Shreshtha", "Priya"}
    assert out["unknown"] == 1
    assert all(r["confident"] for r in out["recognised"])


def test_recognise_all_dedupes_the_same_person_across_faces(tmp_path, monkeypatch):
    path = _enrolled_store(tmp_path, {"Shreshtha": _axis(0)})
    _wire(monkeypatch, [_axis(0), _axis(0)])  # two sightings of one person

    out = face_id.recognise_all([object()], path=path)

    names = [r["name"] for r in out["recognised"]]
    assert names == ["Shreshtha"]
    assert out["unknown"] == 1  # faces(2) minus the one distinct person


def test_recognise_all_refuses_without_the_recognizer_model(monkeypatch):
    monkeypatch.setattr(face_id, "models", lambda: {"recognizer": "", "detector": ""})

    out = face_id.recognise_all([object()])

    assert out["ok"] is False
    assert out["error"] == "FACE_MODEL_MISSING"


# ── _identity_context ─────────────────────────────────────────────────────────
def _roster(faces, recognised, unknown=0):
    confident = [r for r in recognised if r["confident"]]
    return {"ok": True, "faces": faces, "recognised": recognised,
            "confident": confident, "unknown": unknown}


def _person(name, margin, confident):
    return {"name": name, "score": 0.6, "margin": margin, "confident": confident}


@requires_ctx
def test_context_greets_the_owner_and_names_a_known_friend():
    roster = _roster(2, [_person("Shreshtha", 0.3, True), _person("Priya", 0.2, True)])

    ctx = _identity_context(roster, OWNER)

    assert "Shreshtha (the OWNER)" in ctx
    assert "Priya (a known person)" in ctx
    assert "Greet them by name" in ctx


@requires_ctx
def test_context_counts_a_stranger_without_inventing_a_name():
    roster = _roster(2, [_person("Shreshtha", 0.3, True)], unknown=1)

    ctx = _identity_context(roster, OWNER)

    assert "Unrecognised faces: 1." in ctx
    assert "someone with you" in ctx


@requires_ctx
def test_context_holds_back_on_a_thin_match():
    roster = _roster(1, [_person("Shreshtha", 0.01, False)])

    ctx = _identity_context(roster, OWNER)

    assert "do NOT assert these names" in ctx
    assert "Speak to" not in ctx  # owner not confidently present, so no greeting


@requires_ctx
def test_context_is_empty_when_no_face_is_in_view():
    assert _identity_context(_roster(0, []), OWNER) == ""
