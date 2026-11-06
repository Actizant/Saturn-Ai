"""Milestone 3 -- layer rail, batch edits, layout verbs and real export.

The first half is Qt-free on purpose: ``batch_update``, ``reorder_node``,
``set_layout`` and ``set_layout_child`` are model logic in ``tools.py``, and the
export verbs refuse *by name* when no painter is attached -- so those refusals
and those reflows are proven without a canvas, exactly as the live engine runs
them. The second half attaches the real Qt painter through the host window and
checks that a PNG and an SVG come out of the same numbers ``describe`` quotes.

Split by what they touch: no painter needed -> the top; a real export -> the
``live`` fixture, which builds an offscreen native window whose ``export.attach``
is installed in its constructor.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct
from pathlib import Path

import pytest

from saturn_studio.native import scene, tools
from saturn_studio.native.state import StudioState


def _ok(answer):
    assert answer.get("ok") is True, answer
    return answer["result"]


def _render(state, markup):
    return _ok(tools.run(state, "render", {"jsx": markup}))["id"]


# --------------------------------------------------------------------------- #
# Qt-free: batch_update is one undo step across many nodes
# --------------------------------------------------------------------------- #

def test_batch_update_moves_several_nodes_in_a_single_undo():
    state = StudioState()
    a = _render(state, '<Rectangle name="A" w={20} h={20} bg="#fff"/>')
    b = _render(state, '<Rectangle name="B" w={20} h={20} bg="#fff"/>')
    result = _ok(tools.run(state, "batch_update", {"updates": [
        {"id": a, "x": 100, "y": 5}, {"id": b, "x": 200, "opacity": 0.5},
    ]}))
    assert result["count"] == 2
    assert state.document.find_by_id(a).x == 100.0
    assert state.document.find_by_id(b).opacity == 0.5
    # one undo rewinds every node at once -- it was a single step
    assert state.view("undo")["ok"] is True
    assert state.document.find_by_id(a).x == 0.0
    assert state.document.find_by_id(b).opacity == 1.0


def test_batch_update_reports_a_missing_node_without_losing_the_rest():
    state = StudioState()
    a = _render(state, '<Rectangle name="A" w={20} h={20} bg="#fff"/>')
    result = _ok(tools.run(state, "batch_update", {"updates": [
        {"id": a, "x": 42}, {"id": "ghost", "x": 1},
    ]}))
    assert result["results"][0]["updated"] == ["x"]
    assert "error" in result["results"][1]
    assert state.document.find_by_id(a).x == 42.0            # the valid edit applied


def test_batch_update_refuses_an_empty_list():
    state = StudioState()
    bad = tools.run(state, "batch_update", {"updates": []})
    assert bad["ok"] is False and "non-empty list" in bad["error"]


# --------------------------------------------------------------------------- #
# Qt-free: reorder_node -- the layer rail
# --------------------------------------------------------------------------- #

def _layer_names(state):
    return [c.name for c in state.document.current_page.children]


def test_reorder_to_front_and_back_moves_within_the_same_parent():
    state = StudioState()
    for name in ("A", "B", "C"):
        _render(state, f'<Rectangle name="{name}" w={20} h={20} bg="#fff"/>')
    assert _layer_names(state) == ["A", "B", "C"]            # C is front-most
    a_id = state.document.current_page.children[0].id
    _ok(tools.run(state, "reorder_node", {"id": a_id, "to": "front"}))
    assert _layer_names(state) == ["B", "C", "A"]
    _ok(tools.run(state, "reorder_node", {"id": a_id, "to": "back"}))
    assert _layer_names(state) == ["A", "B", "C"]


def test_reorder_forward_backward_and_explicit_index():
    state = StudioState()
    for name in ("A", "B", "C"):
        _render(state, f'<Rectangle name="{name}" w={20} h={20} bg="#fff"/>')
    c_id = state.document.current_page.children[2].id
    _ok(tools.run(state, "reorder_node", {"id": c_id, "to": "backward"}))
    assert _layer_names(state) == ["A", "C", "B"]
    _ok(tools.run(state, "reorder_node", {"id": c_id, "index": 0}))
    assert _layer_names(state) == ["C", "A", "B"]


def test_reorder_is_undoable():
    state = StudioState()
    for name in ("A", "B", "C"):
        _render(state, f'<Rectangle name="{name}" w={20} h={20} bg="#fff"/>')
    a_id = state.document.current_page.children[0].id
    _ok(tools.run(state, "reorder_node", {"id": a_id, "to": "front"}))
    state.view("undo")
    assert _layer_names(state) == ["A", "B", "C"]


# --------------------------------------------------------------------------- #
# Qt-free: set_layout / set_layout_child re-run the flex pass on live nodes
# --------------------------------------------------------------------------- #

def test_set_layout_turns_a_stacked_frame_into_a_real_column():
    state = StudioState()
    frame = _render(state,
        '<Frame name="C" w={300} h={300} bg="#000">'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle w={50} h={50} bg="#fff"/>'
        "</Frame>")
    _ok(tools.run(state, "set_layout", {"id": frame, "mode": "col", "gap": 10}))
    node = state.document.find_by_id(frame)
    assert node.layout.mode == scene.FLEX_COL
    top, bottom = node.children
    assert top.y == 0 and bottom.y == 60                     # 50 + 10 gap, the flex ran


def test_set_layout_padding_and_justify_landscape():
    state = StudioState()
    frame = _render(state,
        '<Frame name="R" w={300} h={100} bg="#000">'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle w={50} h={50} bg="#fff"/>'
        "</Frame>")
    _ok(tools.run(state, "set_layout",
                  {"id": frame, "mode": "row", "gap": 0, "padding": 20, "justify": "between"}))
    node = state.document.find_by_id(frame)
    left, right = node.children
    assert left.x == 20 and abs(right.x - 230) < 0.01        # 300 - 20 - 50
    assert node.layout.primary_align == scene.ALIGN_SPACE


def test_set_layout_refuses_a_leaf():
    state = StudioState()
    rect = _render(state, '<Rectangle name="Leaf" w={20} h={20} bg="#fff"/>')
    bad = tools.run(state, "set_layout", {"id": rect, "mode": "col"})
    assert bad["ok"] is False and "cannot hold auto-layout" in bad["error"]


def test_set_layout_child_grow_expands_into_leftover_space():
    state = StudioState()
    frame = _render(state,
        '<Frame name="C" w={300} h={200} bg="#000" flex="col" gap={0}>'
        '<Rectangle w={100} h={50} bg="#fff"/><Rectangle w={100} h={50} bg="#fff"/>'
        "</Frame>")
    node = state.document.find_by_id(frame)
    grow_id = node.children[1].id
    _ok(tools.run(state, "set_layout_child", {"id": grow_id, "grow": 1}))
    assert state.document.find_by_id(grow_id).height == 150  # 200 - 50, the flex re-ran


def test_set_layout_child_absolute_leaves_the_flow():
    state = StudioState()
    frame = _render(state,
        '<Frame name="C" w={300} h={300} bg="#000" flex="col" gap={10}>'
        '<Rectangle w={50} h={50} bg="#fff"/><Rectangle name="Pin" w={30} h={30} bg="#fff"/>'
        "</Frame>")
    node = state.document.find_by_id(frame)
    pin = node.children[1].id
    _ok(tools.run(state, "set_layout_child", {"id": pin, "absolute": True}))
    assert state.document.find_by_id(pin).absolute is True


# --------------------------------------------------------------------------- #
# Qt-free: export verbs refuse by name when no painter is attached
# --------------------------------------------------------------------------- #

def test_export_refuses_by_name_without_a_painter():
    state = StudioState()
    _render(state, '<Frame name="Card" w={100} h={100} bg="#111"/>')
    png = tools.run(state, "export_image", {})
    svg = tools.run(state, "export_svg", {})
    assert png["ok"] is False and "painter" in png["error"]
    assert svg["ok"] is False and "painter" in svg["error"]


def test_export_falls_back_to_base64_when_no_folder_is_attached():
    state = StudioState()
    card = _render(state, '<Frame name="Card" w={100} h={100} bg="#111"/>')
    blob = b"\x89PNG\r\n\x1a\nFAKE-BYTES"                     # a stub, not real pixels
    state.export_png = lambda node, *, scale=1.0, background=None: blob
    state.export_dir = None                                   # no folder -> base64
    result = _ok(tools.run(state, "export_image", {"id": card}))
    assert result["saved"] is False and result["bytes"] == len(blob)
    import base64

    assert base64.b64decode(result["data"]) == blob


# --------------------------------------------------------------------------- #
# The new verbs are advertised to the router
# --------------------------------------------------------------------------- #

def test_catalog_names_the_milestone_3_verbs():
    names = {entry["name"] for entry in tools.catalog()}
    assert {"batch_update", "reorder_node", "set_layout",
            "set_layout_child", "export_image", "export_svg"} <= names


# --------------------------------------------------------------------------- #
# Real Qt export -- the painter that draws the canvas writes the file
# --------------------------------------------------------------------------- #

@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A native window with the real Qt exporter attached (in its constructor)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])         # noqa: F841
    import core.user_paths as user_paths

    monkeypatch.setattr(user_paths, "get_desktop_output_dir", lambda: tmp_path)
    import saturn_studio.native.window as native_window

    window = native_window.NativeStudioWindow()
    try:
        yield window
    finally:
        if native_window.NativeStudioWindow._instance is window:
            native_window.NativeStudioWindow._instance = None
        window.deleteLater()


def _png_size(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"                  # PNG signature
    width = struct.unpack(">I", data[16:20])[0]
    height = struct.unpack(">I", data[20:24])[0]
    return width, height


def test_export_png_matches_the_models_own_box(live, tmp_path):
    rid = _ok(live.run_tool("render", {"jsx": '<Frame name="Card" w={640} h={360} bg="#101418"/>'}))["id"]
    result = _ok(live.run_tool("export_image", {"id": rid}))
    assert result["saved"] is True and result["format"] == "png"
    assert Path(result["path"]).is_file()
    assert _png_size(Path(result["path"]).read_bytes()) == (640, 360)


def test_export_png_2x_doubles_the_canvas(live):
    rid = _ok(live.run_tool("render", {"jsx": '<Frame name="Card" w={300} h={200} bg="#101418"/>'}))["id"]
    result = _ok(live.run_tool("export_image", {"id": rid, "scale": 2}))
    assert result["scale"] == 2
    assert _png_size(Path(result["path"]).read_bytes()) == (600, 400)


def test_export_svg_is_vector_with_shapes(live):
    rid = _ok(live.run_tool("render", {"jsx":
        '<Frame name="Card" w={300} h={200} bg="#101418">'
        '<Text name="Hi" size={24} color="#FFFFFF">hello</Text></Frame>'}))["id"]
    result = _ok(live.run_tool("export_svg", {"id": rid}))
    text = Path(result["path"]).read_text(encoding="utf-8")
    assert "<svg" in text and 'xmlns="http://www.w3.org/2000/svg"' in text
    assert ("<rect" in text or "<path" in text)              # the frame drew a shape


def test_export_defaults_into_the_studio_folder(live, tmp_path):
    # No id, no filename: export the whole page into <Studio dir>/<doc>.png.
    _ok(live.run_tool("render", {"jsx": '<Frame name="Layer" w={120} h={80} bg="#123456"/>'}))
    result = _ok(live.run_tool("export_image", {}))
    path = Path(result["path"])
    assert path.parent == tmp_path / "Studio" and path.suffix == ".png"
    assert _png_size(path.read_bytes())[0] >= 1


def test_export_of_an_empty_page_reports_there_is_nothing(live):
    # A fresh native window's page has no layers, so there is nothing to draw.
    empty = live.run_tool("export_image", {})
    assert empty["ok"] is False and "nothing visible" in empty["error"]
