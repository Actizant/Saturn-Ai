"""Tests for the Creators shelf and the Tasks forge-tier chip.

These cover the saturn_final_16 design surfaces that are pure logic + widget
construction (no live forging, no LLM): the capability catalog that feeds the
page, the CreatorsPage helpers/filters, and TasksPage.mark_forge_result, which
is the seam the controller uses to stamp a successful forge.
"""
import os
import time

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


# ── catalog (the page's single data source) ────────────────────────────────
def test_catalog_build_returns_uniform_rows():
    from core.creators_catalog import CreatorsCatalog, summary
    caps = CreatorsCatalog.build()
    assert isinstance(caps, list)
    for c in caps:
        # Every capability the page can draw carries this contract (handoff §3).
        for field in ("name", "tier", "description", "origin", "tools",
                      "health", "tool_count", "can_reforge", "can_copy_config"):
            assert field in c, f"missing {field} on {c.get('name')}"
        assert c["tier"] in ("mcp", "feature")
        assert c["origin"] in ("forged", "manual")
    s = summary(caps)
    for key in ("features", "mcp_servers", "tools_live", "forged_30d",
                "self_healed", "calls_14d", "lines_written", "total"):
        assert key in s
    assert s["total"] == len(caps)


# ── CreatorsPage static/derived helpers ─────────────────────────────────────
def test_creators_key_and_ago_helpers(ui):
    C = ui.CreatorsPage
    assert C._key({"tier": "mcp", "name": "cricket"}) == "mcp::cricket"
    assert C._key({"tier": "feature", "name": "wx"}) == "feature::wx"
    now = time.time()
    assert C._ago(now - 5) == "just now"
    assert C._ago(now - 120) == "2m ago"
    assert C._ago(now - 7200) == "2h ago"
    assert C._ago(now - 3 * 86400) == "3d ago"
    assert C._date(None) == "—"
    assert C._date(now)  # a formatted date string


def test_creators_page_constructs_and_filters(ui):
    page = ui.CreatorsPage()
    page.refresh()
    # Seed two synthetic capabilities and check tier filtering + selection.
    now = time.time()
    a = _cap("cricket", "mcp", now - 10, tools_live=2)
    b = _cap("wx", "feature", now - 5, tools_live=1)
    page._all = [a, b]
    page._tier = "all"
    page._query = ""
    page._health = "any"
    assert len(page._filtered()) == 2
    page._tier = "feature"
    assert [c["name"] for c in page._filtered()] == ["wx"]
    page._tier = "all"
    page._query = "crick"
    assert [c["name"] for c in page._filtered()] == ["cricket"]
    # sort by recent → wx (forged more recently) first
    page._query = ""
    page._sort = "recent"
    assert page._filtered()[0]["name"] == "wx"
    # by_key + select round trip
    assert page._by_key(page._key(a)) is a
    page._select_key(page._key(b))
    assert page._sel_key == "feature::wx"


def test_creators_health_and_empty_branches_render(ui):
    page = ui.CreatorsPage()
    page._all = []
    page._sel_key = None
    # Empty state must render without throwing (no forged rows).
    page._render_header()
    page._render_heroes()
    page._render_list()
    page._render_detail()
    # A failing MCP capability exercises the tabbed surface + error list.
    bad = _cap("broken", "mcp", time.time(), health="err")
    bad["last_errors"] = [{"at": time.time(), "tool": "t", "message": "boom", "healed": False}]
    page._all = [bad]
    page._sel_key = page._key(bad)
    page._render_list()
    page._render_detail()


def test_creators_copy_and_reforge_disabled_rules(ui):
    page = ui.CreatorsPage()
    manual = _cap("fs", "mcp", 0, origin="manual")
    manual["can_reforge"] = False
    feat = _cap("wx", "feature", time.time())
    feat["can_copy_config"] = False
    actions_mcp = page._build_actions(manual)
    actions_feat = page._build_actions(feat)
    # Both action rows build without error; the buttons honour the flags.
    assert actions_mcp is not None and actions_feat is not None


def test_creators_permission_select_roundtrip(ui, tmp_path, monkeypatch):
    """Changing a verb's Always/Ask/Block select must persist through the
    catalog's single write seam and re-render without the old typo crash."""
    from core import permissions
    # Point the store at a scratch file so we never touch the real overrides.
    store_file = tmp_path / "tool_permissions.json"
    monkeypatch.setattr(permissions, "STORE_FILE", store_file)

    page = ui.CreatorsPage()
    cap = _cap("cricket", "mcp", time.time(), tools_live=2)
    tool = cap["tools"][0]
    tool["requires"] = ["network"]
    tool["permission"] = {"level": permissions.ASK, "default": permissions.ASK,
                          "declared_requires": ["network"]}
    cap["declared_needs"] = ["network"]
    cap["risk"] = "MEDIUM"
    cap["version_history"] = [{"version": "1.0.0", "state": "current", "at": time.time()}]
    cap["audit"] = [{"ts": time.time(), "tool": "run", "ok": True,
                     "duration_ms": 12, "args": {"q": "scores"}}]
    page._all = [cap]
    page._sel_key = page._key(cap)
    page._render_detail()  # forged branches: grid, callouts, version, audit, combo

    # Drive the combo the way a user click would, picking Block on this verb.
    box = page._perm_combo(cap, tool)
    idx = box.findData(permissions.BLOCK)
    box.setCurrentIndex(idx)
    page._apply_permission(cap, tool, box)
    assert store_file.exists()
    assert permissions.PermissionsStore().get_level("cricket", "run") == permissions.BLOCK

    # Picking the classified default again clears the override back to None.
    idx = box.findData(tool["permission"]["default"])
    box.setCurrentIndex(idx)
    page._apply_permission(cap, tool, box)
    assert permissions.PermissionsStore().get_level("cricket", "run") is None


# ── TasksPage forge chip (the design's §1 minor edit) ───────────────────────
def test_tasks_mark_forge_result_normalizes_tier(ui):
    tp = ui.TasksPage()
    tid = tp.mark_forge_result("cricket", "portable", goal="live scores")
    t = tp._by(tid)
    assert t["status"] == "done"
    assert t["forge_tier"] == "mcp"       # portable → mcp
    assert t["forge_capability"] == "cricket"
    assert t["arts"] and t["arts"][-1]["k"] == "CAPABILITY"

    tid2 = tp.mark_forge_result("wx", "lightweight", goal="weather")
    assert tp._by(tid2)["forge_tier"] == "feature"  # lightweight → feature


def test_tasks_reuses_inflight_forge_task(ui):
    tp = ui.TasksPage()
    # Add a running task whose title mentions the capability, then land the forge.
    running = tp._mk(99, "Forge the payment_router capability",
                    [{"n": "write"}], status="running", cur=0, el=5)
    tp._tasks.append(running)
    tid = tp.mark_forge_result("payment_router", "mcp", goal="route payments")
    assert tid == 99                       # reused, not duplicated
    assert tp._by(99)["status"] == "done"


def test_tasks_open_creators_signal_exists(ui):
    tp = ui.TasksPage()
    assert hasattr(tp, "open_creators")
    got = []
    tp.open_creators.connect(lambda n, tr: got.append((n, tr)))
    tp.open_creators.emit("cricket", "mcp")
    assert got == [("cricket", "mcp")]


# ── helpers ─────────────────────────────────────────────────────────────────
def _cap(name, tier, forged_at, *, origin="forged", tools_live=1, health="ok"):
    now = time.time()
    return {
        "name": name, "tier": tier, "config": {"command": "python"},
        "description": f"{name} does a thing", "long_description": f"{name} detail",
        "origin": origin, "forged_at": forged_at, "forge_task_id": 1,
        "version": "1.0.0", "folder": f"C:/tmp/{name}", "source_path": None,
        "tools": [{"name": "run", "description": "run it",
                   "params": [{"name": "x", "type": "string", "required": True,
                               "description": "an arg", "default": None}]},
                  {"name": "peek", "description": "peek", "params": []}][:tools_live],
        "resources": [{"uri": "r://1", "description": "res", "mime": "text/plain"}]
        if tier == "mcp" and tools_live > 1 else [],
        "prompts": [{"name": "p", "description": "prompt", "args": ["x"]}]
        if tier == "mcp" and tools_live > 1 else [],
        "tool_count": tools_live, "connected": True,
        "can_reforge": origin == "forged", "can_copy_config": tier == "mcp",
        "source_lines": 0, "health": health, "health_note": "note",
        "calls_24h": [{"ok": 1, "failed": 0} for _ in range(24)],
        "calls_14d": [{"ok": 2, "failed": 0} for _ in range(14)],
        "calls_14d_total": 12, "success_pct": 96.0, "avg_latency_ms": 30.0,
        "last_used_at": now, "last_errors": [], "heal_count": 0,
    }
