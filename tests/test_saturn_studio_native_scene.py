"""The clean-room native engine, tested at the model layer.

Everything here is Qt-free on purpose: the scene model, the JSX reader, the
``.saturn`` codec, the runtime state and the verbs import no painter, so the
facts they produce are the same whatever draws them. The suite proves those
facts and, at the end, asserts the painter never crept in -- the boundary that
makes the renderer swappable.
"""

import json
import sys
import zipfile

import pytest

from saturn_studio.native import document as saturn_document
from saturn_studio.native import jsx, scene, tools
from saturn_studio.native.state import StudioState


# --------------------------------------------------------------------------- #
# Scene: geometry, hit-testing, the tree
# --------------------------------------------------------------------------- #

def _hero():
    root, warnings = jsx.build(
        '<Frame name="Hero" w={640} h={360} bg="#101418" rounded={12}>'
        '<Text name="Headline" size={34} weight={700}>Saturn drew this</Text>'
        '<Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />'
        "</Frame>"
    )
    return root, warnings


def test_root_holds_explicit_geometry_and_a_stack_warning():
    root, warnings = _hero()
    assert (root.width, root.height) == (640.0, 360.0)
    assert root.corner_radius == 12.0
    assert len(root.children) == 2
    assert any("stacked" in w for w in warnings)          # honest, not implied


def test_absolute_bounds_fold_the_parent_chain():
    root = scene.Node(type=scene.FRAME, x=100, y=50, width=200, height=100)
    child = scene.Node(type=scene.RECTANGLE, x=10, y=20, width=40, height=8)
    root.add_child(child)
    assert child.absolute_bounds() == (110.0, 70.0, 40.0, 8.0)


def test_hit_test_returns_the_topmost_child_not_the_frame():
    root = scene.Node(type=scene.FRAME, x=0, y=0, width=200, height=200)
    top = scene.Node(type=scene.RECTANGLE, x=50, y=50, width=20, height=20, name="top")
    low = scene.Node(type=scene.RECTANGLE, x=50, y=50, width=20, height=20, name="low")
    root.add_child(low)
    root.add_child(top)                                   # later sibling = higher z
    assert root.hit_test(55, 55) is top
    assert root.hit_test(5, 5) is root
    assert root.hit_test(500, 500) is None


def test_effective_corners_let_a_single_corner_win():
    node = scene.Node(type=scene.RECTANGLE, corner_radius=5.0, top_left_radius=20.0)
    assert node.effective_corners() == (20.0, 5.0, 5.0, 5.0)


# --------------------------------------------------------------------------- #
# Scene: serialization to the tool-report and the on-disk forms
# --------------------------------------------------------------------------- #

def test_to_json_reports_text_run_and_omits_defaults():
    root, _ = _hero()
    headline = root.children[0]
    js = headline.to_json()
    assert js["characters"] == "Saturn drew this"
    assert js["fontSize"] == 34.0 and js["fontWeight"] == 700
    assert js["textAutoResize"] == "WIDTH_AND_HEIGHT"
    assert "opacity" not in js                             # default 1 is not printed


def test_to_json_depth_collapses_children_to_a_count():
    root, _ = _hero()
    assert "childCount" not in root.to_json(2)
    shallow = root.to_json(0)
    assert shallow["childCount"] == 2 and "children" not in shallow


def test_dict_round_trip_preserves_ids_paint_and_corners():
    root, _ = _hero()
    restored = scene.Node.from_dict(json.loads(json.dumps(root.to_dict())))
    assert restored.id == root.id
    assert restored.top_left_radius is None
    assert len(restored.children) == 2
    assert restored.children[0].text.chars == "Saturn drew this"


# --------------------------------------------------------------------------- #
# The .saturn codec
# --------------------------------------------------------------------------- #

def test_document_round_trip_through_the_container(tmp_path):
    doc = scene.Document("poster")
    root, _ = _hero()
    doc.add(root)
    written = saturn_document.save_document(doc, tmp_path / "poster")
    assert written.suffix == ".saturn" and written.is_file()
    loaded, _media = saturn_document.load_document(written)
    assert loaded.name == "poster"
    assert loaded.layers[0].name == "Hero"


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    doc = scene.Document()
    doc.add(scene.Node(type=scene.RECTANGLE, name="Box"))
    saturn_document.save_document(doc, tmp_path / "clean")
    assert list(tmp_path.glob("*.tmp")) == []


def test_load_refuses_a_non_archive(tmp_path):
    bad = tmp_path / "junk.saturn"
    bad.write_bytes(b"this is not a zip")
    with pytest.raises(saturn_document.DocumentError):
        saturn_document.load_document(bad)


def test_load_refuses_a_foreign_or_too_new_format(tmp_path):
    target = tmp_path / "future.saturn"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("manifest.json", json.dumps({"format": "figma", "version": 9}))
        archive.writestr("document.json", "{}")
    with pytest.raises(saturn_document.DocumentError):
        saturn_document.load_document(target)


# --------------------------------------------------------------------------- #
# Runtime state: viewport and honest snapshot undo
# --------------------------------------------------------------------------- #

def test_viewport_maps_scene_to_screen_and_back():
    state = StudioState()
    state.viewport.zoom, state.viewport.pan_x = 2.0, 10.0
    sx, sy = state.viewport.to_screen(30.0, 0.0)
    assert (sx, sy) == (40.0, 0.0)
    assert state.viewport.to_scene(40.0, 0.0) == pytest.approx((30.0, 0.0))


def test_zoom_to_fit_frames_content_and_zoom_on_empty_resets():
    state = StudioState()
    assert state.view("zoom") == {"ok": True}              # empty doc, no crash
    root, _ = _hero()
    state.document.add(root)
    assert state.view("zoom") == {"ok": True}
    assert state.viewport.zoom > 0


def test_undo_and_redo_walk_a_snapshot_history():
    state = StudioState()
    tools.run(state, "render", {"jsx": '<Rectangle name="A" w={10} h={10} bg="#fff"/>'})
    assert len(state.document.layers) == 1
    assert state.view("undo")["ok"] is True
    assert state.document.layers == []
    assert state.view("undo")["ok"] is False               # nothing before the blank
    assert state.view("redo")["ok"] is True
    assert len(state.document.layers) == 1


def test_set_tool_accepts_the_eight_the_bar_ships_and_refuses_others():
    state = StudioState()
    assert state.set_tool("text")["ok"] is True and state.tool == "TEXT"
    assert state.set_tool("spray")["ok"] is False


# --------------------------------------------------------------------------- #
# Verbs: reply shapes and honest limits
# --------------------------------------------------------------------------- #

def test_render_reply_is_the_shape_the_router_reads():
    state = StudioState()
    answer = tools.run(state, "render", {"jsx": '<Frame name="Card" w={100} h={100} bg="#111"><Text>Hi</Text></Frame>'})
    assert answer["ok"] is True
    assert {"id", "name", "type", "children"} <= set(answer["result"])
    assert answer["result"]["type"] == "FRAME"


def test_describe_size_and_visual_come_off_the_model():
    state = StudioState()
    rid = tools.run(state, "render", {"jsx": '<Frame name="Hero" w={640} h={360} bg="#101418" rounded={12}/>'})["result"]["id"]
    described = tools.run(state, "describe", {"id": rid})["result"]
    assert described["size"] == "640×360"
    assert "rounded" in described["visual"]


def test_missing_node_is_a_soft_failure_not_a_crash():
    state = StudioState()
    assert tools.run(state, "get_node", {"id": "nope"}) == {"ok": False, "error": 'Node "nope" not found'}


def test_calc_answers_arithmetic_and_flags_bad_input():
    state = StudioState()
    assert tools.run(state, "calc", {"expr": "844 - 56 - 96 - 82"})["result"]["result"] == 610
    batch = tools.run(state, "calc", {"expr": '["2+2", "floor(3.9)", "1/0"]'})["result"]["results"]
    assert batch[0]["result"] == 4 and "error" in batch[2]


def test_undelivered_capabilities_are_refused_by_name():
    state = StudioState()
    ev = tools.run(state, "eval", {"code": "1+1"})
    assert ev["ok"] is False and "sandbox" in ev["error"]
    assert tools.run(state, "engrave")["error"] == "Saturn Studio has no engrave tool"


def test_the_model_layer_imports_no_painter():
    """The swappable-painter invariant, checked in a fresh interpreter so test
    order cannot fake a pass or a fail: importing the scene, the verbs, the
    runtime state, the JSX reader and the ``.saturn`` codec must never drag in
    Qt, Skia or a canvas -- those belong only to ``items`` and the host."""
    probe = (
        "import sys;"
        "from saturn_studio.native import scene, jsx, tools, document, state;"
        "print([m for m in sys.modules"
        " if m.split('.')[0] in ('PyQt5','PyQt6','PySide2','PySide6','skia','canvaskit')])"
    )
    import subprocess

    out = subprocess.run([sys.executable, "-c", probe],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err = out.stderr.decode("utf-8", "replace")
    listed = out.stdout.decode("utf-8", "replace").strip()
    assert out.returncode == 0, err
    assert listed.endswith("[]"), f"model layer imported a painter: {listed}"

