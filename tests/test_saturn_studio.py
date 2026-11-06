"""Saturn Studio smoke tests -- the seams Saturn depends on, no rendering.

The canvas itself is measured in the native suites (scene, layout, host parity
and Milestone 3). This file guards what can silently regress afterwards:

* Saturn's own writing never names a foreign engine vendor;
* the command rail routes Design to a real centre page;
* the voice verbs reach the canvas through ``tool.py``, and say only what the
  canvas answered;
* the design tool is registered in every seam the model is told about, and the
  vocabulary the model sees is the vocabulary the router actually answers.

The engine is native now, so the router is driven with a scripted page
(``FakeStudio``) rather than a real one -- importing Qt WebEngine in the
offscreen suite is a known way to break unrelated tests, and the native canvas
is exercised for real in ``test_saturn_studio_host_parity.py``.
"""
import re
import sys
import types
from pathlib import Path

import pytest

from saturn_studio import tool as studio_tool

ROOT = Path(__file__).resolve().parent.parent


# ── the shipped surface never names its engine ──────────────────────────────

def test_saturn_docs_never_name_the_engine_vendor():
    """Saturn's own writing describes the studio as Saturn's.

    Attribution the law requires lives in NOTICE; the engine is Saturn's own
    now, so no documentation names any design-engine vendor. This catches an
    edit that reintroduces a foreign package scope into a Saturn-owned doc.
    """
    docs = list(ROOT.glob("*.md"))
    docs += [p for p in (ROOT / "saturn_studio").rglob("*.md")
             if "engine" not in p.parts]
    assert docs, "documentation audit found nothing to read"
    for doc in docs:
        text = doc.read_text(encoding="utf-8", errors="replace").lower()
        assert "pencil" not in text, f"foreign branding in {doc}"


# ── the command rail wiring (static, ui.py is too heavy to import here) ─────

def test_rail_routes_design_to_its_page():
    src = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert "_PAGE_DESIGN = 11" in src
    assert '("design", "Design")' in src
    assert "_PAGE_DESIGN: \"design\"" in src          # rail lights from stack
    assert 'elif key == "design":' in src             # row routes, never dead
    assert "from saturn_studio.window import StudioWindow" in src
    # the design glyph exists for the row, so the rail never paints a dot
    assert '"design": "M4 4h10v10H4z' in src


# ── Phase 2: the bridge the voice comes through ─────────────────────────────

class FakeStudio:
    """The page surface ``tool.py`` uses, with every reply scripted."""

    def __init__(self, *, answers=None, state=None, ready=True, catalog=None):
        self.answers = answers if answers is not None else []
        self._state = state or {"ready": True, "doc": "untitled", "page": "Page 1",
                               "tool": "SELECT", "nodes": 1, "layers": []}
        self._ready = ready
        self._catalog = catalog or []
        self.calls = []
        self.saved = "/tmp/Saturn Studio/untitled.saturn"

    def open_page(self, *, timeout: float = 30.0):
        self.calls.append(("open_page",))
        return {"shown": True, "hosted": True, "booted": True}

    def put_away(self):
        self.calls.append(("put_away",))
        return {"hidden": True}

    def is_on_screen(self):
        return True

    def wait_ready(self, *, timeout: float = 25.0):
        return self._ready

    def state(self):
        return self._state

    def catalog(self):
        return self._catalog

    def run_tool(self, name, args=None, *, timeout: float = 25.0):
        self.calls.append(("run_tool", name, args))
        return self.answers.pop(0) if self.answers else {"ok": True, "result": {}}

    def set_tool(self, tool):
        self.calls.append(("set_tool", tool))
        return {"ok": True}

    def new_document(self):
        self.calls.append(("new_document",))
        return {"ok": True}

    def view(self, kind):
        self.calls.append(("view", kind))
        return {"ok": True}

    def open_fig(self, path):
        self.calls.append(("open_fig", path))
        # Mirrors the native host's honest limit: .fig is refused by name.
        return {"ok": False, "error": "the native Saturn engine reads .saturn documents, not .fig files"}

    def open_document(self, path):
        self.calls.append(("open_document", path))
        return {"ok": True, "doc": path.stem, "layers": 0}

    def save_document(self, *, timeout: float = 15.0):
        self.calls.append(("save_document",))
        return {"ok": True, "path": self.saved}


def _install_host(monkeypatch, studio):
    """Give the router a canvas to talk to, without importing a real engine."""
    host = types.ModuleType("saturn_studio.window")
    host.get_window = lambda parent=None, *, timeout=30.0: studio
    monkeypatch.setitem(sys.modules, "saturn_studio.window", host)
    monkeypatch.setattr(studio_tool, "window", host, raising=False)
    return host


def _install_brain(monkeypatch, reply):
    """Script Saturn's answer to a design brief, or make it not answer."""
    brain = types.ModuleType("llm_client")

    class _Client:
        def chat(self, prompt, **kwargs):
            if isinstance(reply, Exception):
                raise reply
            return reply

    brain.client = _Client()
    monkeypatch.setitem(sys.modules, "llm_client", brain)


def test_an_unknown_verb_is_answered_without_touching_the_canvas(monkeypatch):
    """A mistyped verb is a vocabulary problem, not a reason to boot a renderer."""
    host = types.ModuleType("saturn_studio.window")

    def no_import(*_a, **_k):
        raise AssertionError("the canvas host was imported for a bad verb")

    host.get_window = no_import
    monkeypatch.setitem(sys.modules, "saturn_studio.window", host)

    said = studio_tool.saturn_studio({"action": "engrave"})
    assert "no engrave action" in said
    assert "design" in said                      # the point is to teach the vocabulary


def test_a_missing_page_is_reported_as_missing(monkeypatch):
    _install_host(monkeypatch, None)
    assert studio_tool.saturn_studio({"action": "status"}) == (
        "Saturn Studio is not available on this machine right now, so nothing was drawn."
    )


def test_open_answers_with_what_the_page_actually_reports(monkeypatch):
    studio = FakeStudio(state={"ready": True, "doc": "hero", "page": "Page 1",
                               "tool": "SELECT", "nodes": 4,
                               "layers": [{"id": "1", "name": "Hero", "type": "FRAME",
                                           "children": 2}]})
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "open"})
    assert "hero has 1 layer on it" in said
    assert ("open_page",) in studio.calls


def test_design_draws_through_the_engine_and_says_only_what_came_back(monkeypatch):
    studio = FakeStudio(answers=[
        {"ok": True, "result": {"id": "42", "name": "Login screen",
                              "type": "FRAME", "children": ["1", "2", "3"]}},
    ])
    _install_host(monkeypatch, studio)
    _install_brain(monkeypatch, '```jsx\n<Frame w={400} h={300} bg="#101010" />\n```')

    said = studio_tool.saturn_studio({"action": "design", "brief": "a login screen"})

    render = next(call for call in studio.calls if call[0] == "run_tool")
    assert render[1] == "render"
    assert render[2]["jsx"] == '<Frame w={400} h={300} bg="#101010" />'   # fences stripped
    assert "Login screen" in said and "FRAME" in said and "3 child layers" in said
    assert ("view", "zoom") in studio.calls


def test_design_says_so_when_no_brain_answers(monkeypatch):
    studio = FakeStudio()
    _install_host(monkeypatch, studio)
    _install_brain(monkeypatch, RuntimeError("no provider answered"))

    said = studio_tool.saturn_studio({"action": "design", "brief": "a poster"})
    assert "nothing was drawn" in said
    assert not any(call[0] == "run_tool" for call in studio.calls)


def test_design_says_so_when_the_brief_is_empty(monkeypatch):
    _install_host(monkeypatch, FakeStudio())
    assert "what to design" in studio_tool.saturn_studio({"action": "design"})


def test_a_refusal_from_the_engine_is_quoted_and_not_painted_over(monkeypatch):
    studio = FakeStudio(answers=[{"ok": False, "error": "Invalid arguments for render"}])
    _install_host(monkeypatch, studio)
    _install_brain(monkeypatch, "<Frame />")

    said = studio_tool.saturn_studio({"action": "design", "brief": "a frame"})
    assert "Invalid arguments for render" in said
    assert "Designed" not in said


def test_read_uses_the_pages_own_layers(monkeypatch):
    studio = FakeStudio(state={"ready": True, "doc": "poster", "page": "Page 1",
                               "tool": "SELECT", "nodes": 9, "layers": [
                                   {"id": "1", "name": "Band", "type": "RECTANGLE", "children": 0},
                                   {"id": "2", "name": "Title", "type": "TEXT", "children": 0}]})
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "read"})
    assert "2 top-level layers" in said and "Band (RECTANGLE)" in said and "Title (TEXT)" in said


def test_describe_falls_back_to_the_last_layer_drawn(monkeypatch):
    studio = FakeStudio(
        state={"ready": True, "doc": "poster", "page": "Page 1", "tool": "SELECT",
               "nodes": 9, "layers": [{"id": "7", "name": "Title", "type": "TEXT",
                                       "children": 0}]},
        answers=[{"ok": True, "result": {"id": "7", "name": "Title", "type": "TEXT",
                                        "role": "heading", "size": "720×96"}}],
    )
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "describe"})
    assert studio.calls[-1][1] == "describe"
    assert studio.calls[-1][2] == {"id": "7"}
    assert "Title is a TEXT" in said and "720×96" in said


def test_tools_answers_from_the_catalog_the_page_prints(monkeypatch):
    studio = FakeStudio(catalog=[{"name": "render", "description": "d", "mutates": True},
                                 {"name": "describe", "description": "d", "mutates": False}])
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "tools"})
    assert "2 tools" in said and "render" in said


def test_save_reports_the_path_the_document_landed_at(monkeypatch):
    studio = FakeStudio()
    _install_host(monkeypatch, studio)
    assert studio.saved in studio_tool.saturn_studio({"action": "save"})


def test_open_file_loads_a_saturn_document(monkeypatch, tmp_path):
    doc = tmp_path / "deck.saturn"
    doc.write_bytes(b"saturn-zip-bytes")            # FakeStudio never reads it
    studio = FakeStudio(state={"ready": True, "doc": "deck", "page": "Page 1",
                               "tool": "SELECT", "nodes": 3,
                               "layers": [{"id": "1", "name": "Hero", "type": "FRAME",
                                           "children": 2}]})
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "open_file", "path": str(doc)})
    assert ("open_document", doc) in studio.calls
    assert "Opened deck in Saturn Studio" in said and "1 top-level layer" in said


def test_open_file_refuses_a_fig_because_the_engine_reads_saturn(monkeypatch, tmp_path):
    fake = tmp_path / "deck.fig"
    fake.write_bytes(b"kiwi-bytes")
    studio = FakeStudio()
    _install_host(monkeypatch, studio)
    said = studio_tool.saturn_studio({"action": "open_file", "path": str(fake)})
    assert "not a .saturn document" in said
    assert not any(call[0] == "open_document" for call in studio.calls)


# ── the seams a voice verb has to be wired into ─────────────────────────────

def _schema_verbs():
    """The actions main.py tells the model it may name."""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    block = source.split('"name": "saturn_studio"', 1)[-1]
    match = re.search(r'"action":\s*\{[^{}]*"description":\s*"([^"]+)"', block)
    assert match, "no action description found in the saturn_studio schema"
    return {part.strip() for part in match.group(1).split("|")}


def test_seams_register_the_design_tool():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert '"name": "saturn_studio"' in source
    assert 'elif name == "saturn_studio":' in source
    assert "from saturn_studio.tool import saturn_studio as studio_tool" in source

    plan = (ROOT / "agent" / "planner.py").read_text(encoding="utf-8")
    assert "\nsaturn_studio\n" in plan

    loop = (ROOT / "agent" / "executor.py").read_text(encoding="utf-8")
    assert 'elif tool == "saturn_studio":' in loop
    assert "from saturn_studio.tool import saturn_studio as studio_tool" in loop


def test_every_verb_the_model_is_told_exists():
    """The contract shown to the assistant and the router must be one vocabulary."""
    handlers = studio_tool.HANDLERS
    for verb in _schema_verbs():
        assert verb in handlers, f"schema promises {verb}, the router has no such verb"

    source = (ROOT / "agent" / "planner.py").read_text(encoding="utf-8")
    block = source.split("\nsaturn_studio\n", 1)[-1]
    line = re.search(r'^\s*action:.*$', block, re.MULTILINE).group(0)
    for verb in re.findall(r'"([a-z_]+)"', line):
        assert verb in handlers, f"the planner promises {verb}, the router has no such verb"


def test_the_authoring_reference_ships_with_the_native_engine():
    """Saturn writes JSX out of its own manual, so the manual must be there."""
    assert studio_tool.AUTHORING_REFERENCE.is_file()
    text = studio_tool.AUTHORING_REFERENCE.read_text(encoding="utf-8")
    assert "flex=" in text and "auto-layout" in text
    assert len(studio_tool._authoring_reference()) == len(text)
