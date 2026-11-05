"""Tests for the CREATOR TOOLS centre page and its add-server dialog.

These cover the saturn_final_17 design surface that is pure logic + widget
construction (no live server boots, no LLM): ``CreatorToolsPage._collect`` which
turns the mcp_servers config + forge manifests + live manager into grid rows,
the page's grid/workspace rendering, and ``AddMcpServerDialog`` validation - the
trust gate before a hand-configured server is booted.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def ui(qapp):
    import ui as ui_module
    return ui_module


def _seed(monkeypatch, tmp_path, forged=("cricket",), live=None):
    """Point the page's three read seams at scratch data so it never touches
    the real config, the real forge folder, or the live manager."""
    from core.creators_catalog import CreatorsCatalog
    from core.mcp_forge import MCPForge

    cfg = {"mcpServers": {
        "filesystem": {"command": "npx", "args": ["-y", "fs-server"],
                       "description": "files"},
        "cricket": {"command": "python", "args": ["server.py"]},
        "broken": {"command": "python", "args": ["x.py"], "disabled": True},
    }}
    p = tmp_path / "mcp_servers.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(CreatorsCatalog, "_config_paths", staticmethod(lambda: [p]))
    monkeypatch.setattr(CreatorsCatalog, "_live_servers", staticmethod(lambda: live or {}))
    monkeypatch.setattr(MCPForge, "list_forged",
                        staticmethod(lambda: [{"name": n} for n in forged]))
    return p


# ── _collect (the grid's data source) ───────────────────────────────────────
def test_collect_marks_forged_and_state(ui, tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    rows = ui.CreatorToolsPage._collect()
    by = {r["name"]: r for r in rows}
    assert by["cricket"]["forged"] is True
    assert by["filesystem"]["forged"] is False
    assert by["broken"]["state"] == "disabled"
    # No live process for filesystem → offline, and it is stdio (has command).
    assert by["filesystem"]["state"] == "offline"
    assert by["filesystem"]["transport"] == "stdio"
    assert by["filesystem"]["tool_count"] == 0     # never a placeholder count


def test_collect_reads_live_state(ui, tmp_path, monkeypatch):
    class _Server:
        is_connected = True
        lazy = True
        tools = [{"name": "get_score"}, {"name": "list_matches"}]

    _seed(monkeypatch, tmp_path, live={"cricket": _Server()})
    rows = ui.CreatorToolsPage._collect()
    cricket = next(r for r in rows if r["name"] == "cricket")
    assert cricket["state"] == "online"
    assert cricket["tool_count"] == 2
    assert cricket["connected"] is True


# ── page construction + rendering ────────────────────────────────────────────
def test_page_constructs_and_renders(ui, tmp_path, monkeypatch):
    _seed(monkeypatch, tmp_path)
    page = ui.CreatorToolsPage()
    assert len(page._rows) == 3
    # Re-render every branch without throwing (cards, workspace, header).
    page._render_grid()
    page._render_header()
    page._render_workspace()
    # A forged row's action offers the jump-to-Creators hand-off, not Connect.
    forged = next(r for r in page._rows if r["forged"])
    actions = page._card_actions(forged)
    labels = [b.text() for i in range(actions.count())
              if (b := actions.itemAt(i).widget()) is not None]
    assert "OPEN IN CREATORS" in labels
    manual = next(r for r in page._rows if not r["forged"] and r["state"] != "disabled")
    la = page._card_actions(manual)
    mlabels = [b.text() for i in range(la.count())
               if (b := la.itemAt(i).widget()) is not None]
    assert "CONNECT" in mlabels and "REMOVE" in mlabels


def test_empty_state_when_no_servers(ui, tmp_path, monkeypatch):
    from core.creators_catalog import CreatorsCatalog
    from core.mcp_forge import MCPForge
    empty = tmp_path / "none.json"
    empty.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    monkeypatch.setattr(CreatorsCatalog, "_config_paths", staticmethod(lambda: [empty]))
    monkeypatch.setattr(CreatorsCatalog, "_live_servers", staticmethod(lambda: {}))
    monkeypatch.setattr(MCPForge, "list_forged", staticmethod(lambda: []))
    page = ui.CreatorToolsPage()
    assert page._rows == []
    page._render_grid()   # draws the empty-state box, no crash


# ── add-server trust dialog ──────────────────────────────────────────────────
def test_add_dialog_rejects_bad_input(ui):
    dlg = ui.AddMcpServerDialog(existing=["filesystem"])
    dlg._name.setText(""); dlg._cmd.setText("python")
    dlg._submit()
    assert dlg.entry is None and "name" in dlg._err.text().lower()

    dlg._name.setText("filesystem"); dlg._submit()
    assert dlg.entry is None and "already" in dlg._err.text()

    dlg._name.setText("weather"); dlg._cmd.setText(""); dlg._submit()
    assert dlg.entry is None and "command" in dlg._err.text().lower()


def test_add_dialog_builds_config(ui):
    dlg = ui.AddMcpServerDialog()
    dlg._name.setText("weather")
    dlg._cmd.setText("python")
    dlg._args.setText("server.py --port 8080")
    dlg._env.setPlainText('{"API_KEY": "secret"}')
    dlg._submit()
    assert dlg.entry is not None
    name, cfg = dlg.entry
    assert name == "weather"
    assert cfg["command"] == "python"
    assert cfg["args"] == ["server.py", "--port", "8080"]
    assert cfg["env"] == {"API_KEY": "secret"}
    assert cfg["disabled"] is False


def test_add_dialog_rejects_bad_env_json(ui):
    dlg = ui.AddMcpServerDialog()
    dlg._name.setText("weather")
    dlg._cmd.setText("python")
    dlg._env.setPlainText("this is not json")
    dlg._submit()
    assert dlg.entry is None and "JSON" in dlg._err.text()


# ── rail wiring (the p7 seam) ────────────────────────────────────────────────
def test_rail_has_both_creators_and_tools_rows(ui):
    rail = ui.CommandNavSidebar()
    # Creators (self-forged shelf) and Creator Tools (MCP bench) are distinct.
    assert "cre" in rail.rows and "tools" in rail.rows
    assert rail.rows["cre"].text() == "Creators"
    assert rail.rows["tools"].text() == "Creator Tools"
    assert ui._PAGE_TOOLS == ui._PAGE_CREATORS + 1

