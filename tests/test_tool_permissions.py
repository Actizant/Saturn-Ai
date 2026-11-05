"""
Tests for the declared-needs + approval-gated permission model (design
package 17, CREATORS detail panel) and its two enforcement seams.

Covers, in order:
- core/permissions.py: classification ladder, risk badge, override store
- core/mcp_forge.py: declared requires recorded + code-scan union at emit
- core/mcp_manager.execute_mcp_tool: Block refuses, Ask parks behind the
  confirmation banner, headless passthrough, manual servers untouched
- core/dynamic_registry: the same three behaviours on the feature side
- core/creators_catalog: permission rows, risk, version history, audit
  rows and the safety-state bar numbers
"""

import json
import time
from pathlib import Path

import pytest

from core import confirm, permissions
from core.permissions import ASK, ALLOW, BLOCK


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(permissions, "STORE_FILE", tmp_path / "tool_permissions.json")
    return permissions


@pytest.fixture(autouse=True)
def _clean_confirm_state():
    """core/confirm keeps a module-global pending banner; without clearing it
    a test that parks an ASK banner and never resolves it would leak that
    pending into the next test's gate check. Reset it around every test."""
    confirm._pending = None
    confirm.bind(None, None, None)
    yield
    confirm._pending = None
    confirm.bind(None, None, None)


# ── core/permissions.py ──────────────────────────────────────────────────────

def test_classification_ladder():
    # Commands or mutating verbs must ask; a shelled-out tool never auto-allows.
    assert permissions.classify_defaults(["commands"], "run_backup", "") == ASK
    assert permissions.classify_defaults([], "delete_old_files", "") == ASK
    # Declared network/files but an obviously read-only name is free.
    assert permissions.classify_defaults(["network"], "get_weather", "") == ALLOW
    assert permissions.classify_defaults(["files"], "list_downloads", "") == ALLOW
    # Declared needs + an ambiguous name stay conservative.
    assert permissions.classify_defaults(["network"], "process_invoice", "") == ASK
    # Pure in-process calculation with nothing scary in the words.
    assert permissions.classify_defaults([], "fibonacci", "") == ALLOW
    # ...unless the description promises to touch things anyway.
    assert permissions.classify_defaults([], "helper", "uploads your files") == ASK


def test_risk_badge_follows_blast_radius():
    assert permissions.risk_level(["commands"], ASK, "run_x") == "HIGH"
    assert permissions.risk_level([], ASK, "delete_rows") == "HIGH"
    assert permissions.risk_level(["network"], ALLOW, "get_feed") == "MEDIUM"
    assert permissions.risk_level([], ALLOW, "add_numbers") == "LOW"
    # Blocked runs nothing, so its live risk is LOW whatever it could do.
    assert permissions.risk_level(["commands"], BLOCK, "run_x") == "LOW"


def test_store_roundtrip_and_override_priority(store):
    assert store.resolve("alpha", "tool_x", ["commands"], "tool_x") == ASK
    assert store.PermissionsStore.set_level("alpha", "tool_x", BLOCK) is True
    assert store.resolve("alpha", "tool_x", ["commands"], "tool_x") == BLOCK
    eff = store.effective("alpha", "tool_x", ["commands"], "tool_x")
    assert eff["overridden"] is True and eff["level"] == BLOCK
    # Clearing the override falls back to the classified default.
    assert store.PermissionsStore.set_level("alpha", "tool_x", None) is True
    assert store.resolve("alpha", "tool_x", ["commands"], "tool_x") == ASK
    assert store.effective("alpha", "tool_x", ["commands"], "tool_x")["overridden"] is False


def test_capability_wildcard_and_garbage_store(store):
    store.PermissionsStore.set_level("beta", "*", BLOCK)
    assert store.resolve("beta", "anything", [], "count") == BLOCK
    assert "beta" in store.blocked_capabilities()
    # A corrupt file degrades to defaults instead of raising.
    store.STORE_FILE.write_text("{not json", encoding="utf-8")
    assert store.resolve("beta", "anything", [], "count") == ALLOW


# ── forge records declared needs ─────────────────────────────────────────────

def test_infer_declared_needs_scans_source():
    from core.mcp_forge import infer_declared_needs
    src = ("import subprocess, requests\n"
           "from pathlib import Path\n"
           "def t():\n"
           "    subprocess.run(['dir'])\n"
           "    requests.get('http://x', timeout=8)\n"
           "    Path('a').read_text()\n")
    assert sorted(infer_declared_needs(src)) == ["commands", "files", "network"]
    assert infer_declared_needs("def t(x: int) -> int:\n    return x * 2\n") == []


def test_emit_files_unions_declared_and_scanned(tmp_path, monkeypatch):
    from core import mcp_forge
    def _srv_dir(name, _t=tmp_path):
        p = _t / name
        p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(mcp_forge.MCPForge, "_server_dir", staticmethod(_srv_dir))
    manifest = {"description": "d", "tools": [
        {"name": "get_feed", "description": "reads a url",
         "requires": ["bogus", "network"], "inputSchema": {}},
        {"name": "count_things", "description": "adds up", "inputSchema": {}},
    ]}
    code = "import urllib.request\ndef get_feed():\n    urllib.request.urlopen('x')\n"
    _, mpath = mcp_forge.MCPForge._emit_files(
        "alpha", code, manifest, [], goal="watch the feed", prev_version=None)
    saved = json.loads(Path(mpath).read_text(encoding="utf-8"))
    tools = {t["name"]: t for t in saved["tools"]}
    # Invalid declared value dropped; the scan backs the declaration up.
    assert tools["get_feed"]["requires"] == ["network"]
    assert tools["get_feed"]["permission_default"] in (ALLOW, ASK)
    # No needs, no scary words -> classified allow.
    assert tools["count_things"]["requires"] == []
    assert tools["count_things"]["permission_default"] == ALLOW


# ── MCP execute gate ─────────────────────────────────────────────────────────

def _fake_server(tools=None, forged=True, **kw):
    s = type("S", (), {})()
    s.is_connected = kw.get("is_connected", True)
    s.error = None
    s.proc = object()
    s.resources = []
    s.prompts = []
    s.tools = tools if tools is not None else [{"name": "tool_x", "description": "reads", "requires": []}]
    s.config = kw.get("config", {"source": "saturn_mcp_forge"} if forged else {})
    s.is_forged = forged
    s.lazy = False
    s._last_start_attempt = time.time()
    s.call_tool = lambda name, args: {"success": True, "output": "ok"}
    return s


def _bare_manager():
    from core.mcp_manager import UniversalMCPManager
    mgr = UniversalMCPManager.__new__(UniversalMCPManager)
    mgr._servers = {}
    return mgr


@pytest.fixture
def no_kill_switch(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)


def _reset_confirm():
    confirm._pending = None
    confirm.bind(None, None, None)


def test_block_refuses_the_call(store, monkeypatch, no_kill_switch):
    _reset_confirm()
    monkeypatch.setattr(confirm, "_show_cb", None)
    from core.audit_log import AuditLog
    mgr = _bare_manager()
    mgr._servers["alpha"] = _fake_server()
    store.PermissionsStore.set_level("alpha", "tool_x", BLOCK)
    out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {"q": "1"})
    assert out.startswith("Blocked:")
    entries = [e for e in AuditLog.recent(limit=5) if e["tool"] == "mcp_alpha_tool_x"]
    assert entries and entries[-1]["ok"] is False
    assert "Blocked" in (entries[-1].get("error") or "")


def test_ask_parks_behind_banner_then_runs(store, monkeypatch, no_kill_switch):
    _reset_confirm()
    shown, hidden = [], []
    confirm.bind(lambda t, d: shown.append((t, d)),
                 lambda: hidden.append(True))
    try:
        store.PermissionsStore.set_level("alpha", "tool_x", ASK)
        mgr = _bare_manager()
        mgr._servers["alpha"] = _fake_server()

        out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {"q": "1"})
        assert out.startswith("[CONFIRMATION_PENDING]")
        assert shown and shown[0][0] == "Run tool_x?"
        assert "Declared needs" in shown[0][1]

        # The model cannot get the payload by re-calling: a second attempt
        # while the banner is up refuses to stack or run.
        out2 = mgr.execute_mcp_tool("mcp_alpha_tool_x", {"q": "1"})
        assert out2.startswith("[CONFIRMATION_PENDING]")

        # The human presses CONFIRM: the stored run executes with the
        # ORIGINAL arguments and the audit trail records the real call.
        confirm.resolve(True)
        for _ in range(50):
            time.sleep(0.02)
            if hidden:
                break
        assert hidden
        from core.audit_log import AuditLog
        ran = [e for e in AuditLog.recent(limit=10)
               if e["tool"] == "mcp_alpha_tool_x" and e["ok"]]
        assert ran and ran[-1]["args"] == {"q": "1"}
    finally:
        _reset_confirm()


def test_ask_passes_through_when_no_ui_bound(store, monkeypatch, no_kill_switch):
    _reset_confirm()
    store.PermissionsStore.set_level("alpha", "tool_x", ASK)
    mgr = _bare_manager()
    mgr._servers["alpha"] = _fake_server()
    assert mgr.execute_mcp_tool("mcp_alpha_tool_x", {}) == "ok"


def test_allow_and_manual_servers_run_free(store, monkeypatch, no_kill_switch):
    _reset_confirm()
    monkeypatch.setattr(permissions, "STORE_FILE", Path(__file__).parent / "_no_such_store.json")
    # Pure-read forged verb: classified allow, no banner, no refusal.
    shown = []
    confirm.bind(lambda t, d: shown.append(t), lambda: None)
    try:
        mgr = _bare_manager()
        mgr._servers["alpha"] = _fake_server()
        assert mgr.execute_mcp_tool("mcp_alpha_tool_x", {}) == "ok"
        assert shown == []
        # Manual server with a mutating name: never gated at all.
        mgr._servers["man"] = _fake_server(
            tools=[{"name": "delete_rows", "description": "", "requires": ["commands"]}],
            forged=False, config={})
        assert mgr.execute_mcp_tool("mcp_man_delete_rows", {}) == "ok"
        assert shown == []
    finally:
        _reset_confirm()


# ── feature-side parity ──────────────────────────────────────────────────────

def _forged_skill(tmp_path, name="nightly_cleanup", source="import os\ndef execute(**kw):\n    return 'done'\n"):
    from core.dynamic_registry import DynamicSkill
    d = tmp_path / name
    d.mkdir()
    (d / "skill.py").write_text(source, encoding="utf-8")
    manifest = {"name": name, "description": "cleans the downloads folder",
                "author": "Project Saturn Autonomous Self-Evolution Engine",
                "requires": ["files"]}
    (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return DynamicSkill(d, manifest)


def test_feature_block_and_ask(store, tmp_path):
    _reset_confirm()
    skill = _forged_skill(tmp_path)
    assert skill.forged
    store.PermissionsStore.set_level(skill.name, skill.name, BLOCK)
    out = skill.execute_sync()
    assert out.startswith("Blocked:")

    store.PermissionsStore.set_level(skill.name, skill.name, ASK)
    shown = []
    confirm.bind(lambda t, d: shown.append((t, d)), lambda: None)
    try:
        out = skill.execute_sync(path="C:/dl")
        assert out.startswith("[CONFIRMATION_PENDING]")
        assert shown[0][0] == f"Run {skill.name}?"
    finally:
        _reset_confirm()


def test_feature_confirmed_run_keeps_arguments(store, tmp_path):
    _reset_confirm()
    skill = _forged_skill(tmp_path, name="echo_tool",
                          source="def execute(**kw):\n    return kw\n")
    # No declared needs and a harmless name -> classified allow, kwargs flow.
    (skill.skill_dir / "manifest.json").write_text(json.dumps({
        "name": "echo_tool", "description": "echoes", "author": "saturn"}),
        encoding="utf-8")
    skill.manifest = {"name": "echo_tool", "description": "echoes",
                      "author": "Project Saturn Autonomous Self-Evolution Engine"}
    assert skill.execute_sync(note="hi") == {"note": "hi"}

    store.PermissionsStore.set_level("echo_tool", "echo_tool", ASK)
    results = []
    confirm.bind(lambda t, d: None, lambda: None,
                 log=None)
    try:
        out = skill.execute_sync(note="confirmed-hi")
        assert out.startswith("[CONFIRMATION_PENDING]")
        confirm.resolve(True)
        for _ in range(50):
            time.sleep(0.02)
            from core.audit_log import AuditLog
            ran = [e for e in AuditLog.recent(limit=10)
                   if e["tool"] == "echo_tool" and e["ok"]]
            if ran:
                results.append(ran[-1])
                break
        assert results and results[-1]["args"] == {"note": "confirmed-hi"}
    finally:
        _reset_confirm()


def test_native_feature_never_gated(store, tmp_path):
    from core.dynamic_registry import DynamicSkill
    _reset_confirm()
    d = tmp_path / "native_thing"
    d.mkdir()
    (d / "skill.py").write_text("def execute(**kw):\n    return 'native'\n", encoding="utf-8")
    skill = DynamicSkill(d, {"name": "native_thing", "description": "delete stuff",
                             "author": "someone"})
    assert not skill.forged
    store.PermissionsStore.set_level("native_thing", "native_thing", BLOCK)
    assert skill.execute_sync() == "native"  # user's own code: store not consulted


# ── catalog exposure (p4) ────────────────────────────────────────────────────

def test_catalog_permission_rows_risk_and_safety(tmp_path, monkeypatch, store):
    from core.creators_catalog import CreatorsCatalog

    folder = tmp_path / "mcp_skills" / "alpha"
    folder.mkdir(parents=True)
    (folder / "server.py").write_text("import subprocess\n", encoding="utf-8")
    (folder / "server.py.bak").write_text("old\n", encoding="utf-8")
    manifest = {
        "name": "alpha", "description": "does things", "goal": "watch my downloads",
        "version": "1.0.2", "previous_version": "1.0.1",
        "first_forged_at": 1000.0, "created_at": 2000.0, "reforged": True,
        "author": "Project Saturn Autonomous MCP Forge",
        "tools": [
            {"name": "run_cleanup", "description": "runs a sweep",
             "requires": ["commands"], "inputSchema": {}},
            {"name": "get_status", "description": "reads state",
             "requires": [], "inputSchema": {}},
        ],
    }
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr("core.mcp_forge.MCP_SKILLS_DIR", tmp_path / "mcp_skills")
    monkeypatch.setattr("core.mcp_forge.MCPForge.list_forged",
                        classmethod(lambda cls: [dict(manifest, _dir=str(folder))]))

    caps = CreatorsCatalog.build()
    alpha = next(c for c in caps if c["name"] == "alpha")
    assert alpha["goal"] == "watch my downloads"
    assert alpha["permissions_enforced"] is True
    assert alpha["risk"] == "HIGH"          # a commands tool is in the table
    assert alpha["declared_needs"] == ["commands"]
    by_name = {t["name"]: t for t in alpha["tools"]}
    assert by_name["run_cleanup"]["permission"]["level"] == ASK
    assert by_name["run_cleanup"]["permission"]["risk"] == "HIGH"
    assert by_name["get_status"]["permission"]["level"] == ALLOW
    # The user changes one row; the catalog reflects the override next build.
    assert CreatorsCatalog.set_permission("alpha", "get_status", BLOCK) is True
    caps2 = CreatorsCatalog.build()
    alpha2 = next(c for c in caps2 if c["name"] == "alpha")
    st = {t["name"]: t for t in alpha2["tools"]}["get_status"]
    assert st["permission"]["level"] == BLOCK and st["permission"]["overridden"]
    # Version history: current + the recorded predecessor + the .bak proof.
    versions = [h["version"] for h in alpha2["version_history"]]
    assert "1.0.2" in versions and "1.0.1" in versions
    assert any(h["state"] == "rollback snapshot" for h in alpha2["version_history"])
    # Audit rows surface per-capability once calls have been recorded.
    from core.audit_log import AuditLog
    AuditLog.record("mcp_alpha_get_status", True, 12.0, {"why": "x"})
    alpha3 = next(c for c in CreatorsCatalog.build() if c["name"] == "alpha")
    assert alpha3["audit"] and alpha3["audit"][0]["tool"] == "get_status"

    state = CreatorsCatalog.safety_state(caps2)
    assert state["forged_tools_enabled"] is True
    assert state["forged_servers"] == 1     # the one forged cap in the list
