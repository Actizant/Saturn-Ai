"""The native host against the router's own contract.

``saturn_studio/tool.py`` is the stable surface the agent speaks to. This suite
drives a real offscreen Qt canvas through that unchanged router and asserts it
answers with the exact facts the router quotes back: the same reply shapes, the
same counts, the same sentences for the same brief -- and refuses, by name, the
things the native engine does not ship rather than faking them with a number.

Geometry is compared to within a tolerance, because Qt's text shaper is not
pixel-exact run to run; this is a contract test, not a pixel test.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
import types

import pytest

QAPP = None


def _app():
    global QAPP
    from PyQt6.QtWidgets import QApplication

    if QAPP is None:
        QAPP = QApplication.instance() or QApplication([])
    return QAPP


@pytest.fixture()
def studio(monkeypatch):
    """A fresh native canvas, registered so the router's get_window finds it."""
    _app()
    import saturn_studio.native.window as native_window

    window = native_window.NativeStudioWindow()
    try:
        yield window
    finally:
        if native_window.NativeStudioWindow._instance is window:
            native_window.NativeStudioWindow._instance = None
        window.deleteLater()


HERO_JSX = (
    '<Frame name="Hero" w={640} h={360} bg="#101418" rounded={12}>'
    '<Text name="Headline" size={34} weight={700}>Saturn drew this</Text>'
    '<Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />'
    "</Frame>"
)


def _ok(answer):
    assert answer.get("ok") is True, answer
    return answer["result"]


# --------------------------------------------------------------------------- #
# The host surface answers the shapes the router reads
# --------------------------------------------------------------------------- #

def test_render_returns_the_shape_the_router_quotes(studio):
    drawn = _ok(studio.run_tool("render", {"jsx": HERO_JSX}))
    assert drawn["type"] == "FRAME" and drawn["name"] == "Hero"
    assert isinstance(drawn["children"], list) and len(drawn["children"]) == 2
    assert any("stacked" in w for w in drawn.get("warnings", []))


def test_state_matches_the_layer_and_count_contract(studio):
    studio.run_tool("render", {"jsx": HERO_JSX})
    state = studio.state()
    assert {"ready", "doc", "page", "pages", "tool", "nodes", "layers", "status"} <= set(state)
    assert state["ready"] is True and state["tool"] == "SELECT"
    assert len(state["layers"]) == 1
    top = state["layers"][0]
    assert set(top) == {"id", "name", "type", "children"}
    assert top["name"] == "Hero" and top["children"] == 2
    assert state["nodes"] == 3                                    # Hero + 2 children


def test_describe_size_is_exact_within_one_pixel(studio):
    rid = _ok(studio.run_tool("render", {"jsx": '<Frame name="Card" w={320} h={180} bg="#111"/>'}))["id"]
    described = _ok(studio.run_tool("describe", {"id": rid}))
    w, h = described["size"].split("×")
    assert abs(float(w) - 320) <= 1 and abs(float(h) - 180) <= 1


def test_catalog_is_the_named_tool_list(studio):
    catalog = studio.catalog()
    assert all(set(entry) == {"name", "description", "mutates"} for entry in catalog)
    names = {entry["name"] for entry in catalog}
    assert {"render", "describe", "get_node", "find_nodes", "calc", "eval"} <= names


def test_view_history_round_trips(studio):
    studio.run_tool("render", {"jsx": HERO_JSX})
    assert studio.view("zoom")["ok"] is True
    assert studio.view("undo")["ok"] is True and studio.state()["nodes"] == 0
    assert studio.view("undo")["ok"] is False                    # honest bottom
    assert studio.view("redo")["ok"] is True and studio.state()["nodes"] == 3


# --------------------------------------------------------------------------- #
# Honest limits: refused by name, never faked with a number
# --------------------------------------------------------------------------- #

def test_undelivered_verbs_refuse_by_name(studio):
    unknown = studio.run_tool("spray_paint", {})
    assert unknown["ok"] is False and "no spray_paint tool" in unknown["error"]
    ev = studio.run_tool("eval", {"code": "1"})
    assert ev["ok"] is False and "sandbox" in ev["error"]


def test_fig_is_refused_because_the_engine_reads_saturn(studio, tmp_path):
    fake = tmp_path / "deck.fig"
    fake.write_bytes(b"kiwi-bytes")
    refused = studio.open_fig(fake)
    assert refused["ok"] is False and "saturn" in refused["error"].lower()


def test_save_lands_a_real_saturn_file_in_the_studio_folder(studio, tmp_path, monkeypatch):
    import core.user_paths as user_paths

    monkeypatch.setattr(user_paths, "get_desktop_output_dir", lambda: tmp_path)
    studio.run_tool("render", {"jsx": HERO_JSX})
    saved = studio.save_document()
    assert saved["ok"] is True
    path = saved["path"]
    assert path.endswith(".saturn")
    from saturn_studio.native import document as saturn_document

    loaded, _ = saturn_document.load_document(path)
    assert loaded.layers and loaded.layers[0].name == "Hero"


def test_open_document_reloads_a_saved_saturn_file_onto_the_canvas(studio, tmp_path, monkeypatch):
    import core.user_paths as user_paths

    monkeypatch.setattr(user_paths, "get_desktop_output_dir", lambda: tmp_path)
    studio.run_tool("render", {"jsx": HERO_JSX})
    path = studio.save_document()["path"]
    studio.new_document()
    assert studio.state()["nodes"] == 0                        # canvas cleared

    opened = studio.open_document(path)
    assert opened["ok"] is True and opened["layers"] == 1
    assert studio.state()["layers"][0]["name"] == "Hero"        # the real scene came back


# --------------------------------------------------------------------------- #
# The unchanged router, driven by a real native canvas
# --------------------------------------------------------------------------- #

@pytest.fixture()
def router(monkeypatch):
    from saturn_studio import tool as studio_tool

    monkeypatch.setattr(studio_tool, "window", None, raising=False)
    return studio_tool


def _script_brain(monkeypatch, reply):
    brain = types.ModuleType("llm_client")

    class _Client:
        def chat(self, prompt, **kwargs):
            return reply

    brain.client = _Client()
    monkeypatch.setitem(sys.modules, "llm_client", brain)


def test_router_design_verb_speaks_only_what_the_native_engine_returned(studio, router, monkeypatch):
    _script_brain(monkeypatch, HERO_JSX)
    said = router.saturn_studio({"action": "design", "brief": "a hero"})
    assert "Designed Hero" in said and "FRAME" in said and "2 child layers" in said
    assert "engine noted" in said                               # the stack warning surfaced


def test_router_read_and_status_use_the_pages_own_layers(studio, router):
    studio.run_tool("render", {"jsx": HERO_JSX})
    assert "1 top-level layers: Hero (FRAME, 2 children)" in router.saturn_studio({"action": "read"})
    status = router.saturn_studio({"action": "status"})
    assert "3 nodes in all" in status and "SELECT tool" in status


def test_router_describe_falls_back_to_the_last_layer(studio, router):
    studio.run_tool("render", {"jsx": '<Frame name="Poster" w={640} h={360} bg="#101418"/>'})
    said = router.saturn_studio({"action": "describe"})
    assert "Poster is a FRAME" in said and "640×360" in said


def test_router_tools_counts_the_native_registry(studio, router):
    said = router.saturn_studio({"action": "tools"})
    assert "answers" in said and "render" in said and "describe" in said


# --------------------------------------------------------------------------- #
# Milestone 2 -- a real Qt canvas runs the flex pass, not just a stack
# --------------------------------------------------------------------------- #

FLEX_JSX = (
    '<Frame name="Live check" w={640} h={360} bg="#101418" flex="col" '
    'gap={16} p={24} rounded={12}>'
    '<Text name="Headline" size={34} color="#F0B429" weight={700}>Saturn drew this</Text>'
    '<Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />'
    '<Text name="Caption" size={16} color="#C8D3E0">one frame, real pixels</Text>'
    "</Frame>"
)


def test_flex_fill_resolves_against_real_padding_on_the_canvas(studio):
    # The plan's acceptance numbers: inside 640 with 24 of padding the fill
    # child lands at x=24 and 592 wide -- only if the flex pass actually ran.
    _ok(studio.run_tool("render", {"jsx": FLEX_JSX}))
    page = studio.rt.document.current_page
    bar = page.children[0].children[1]
    assert bar.x == 24.0 and abs(bar.width - 592.0) < 1.0


def test_flex_render_reports_no_stack_warning(studio):
    drawn = _ok(studio.run_tool("render", {"jsx": FLEX_JSX}))
    assert not any("stacked" in w for w in drawn.get("warnings", []))


def test_get_node_reports_the_layout_vocabulary(studio):
    rid = _ok(studio.run_tool("render", {"jsx": FLEX_JSX}))["id"]
    node = _ok(studio.run_tool("get_node", {"id": rid}))
    assert node["layoutMode"] == "VERTICAL"
    assert node["itemSpacing"] == 16 and node["paddingLeft"] == 24


def test_describe_quotes_the_layout_the_engine_ran(studio):
    studio.run_tool("render", {"jsx": FLEX_JSX})
    described = _ok(studio.run_tool("describe", {"id": studio.rt.document.layers[0].id}))
    assert "auto-layout column" in described["layout"]
    assert "16 gap" in described["layout"] and "padding" in described["layout"]

