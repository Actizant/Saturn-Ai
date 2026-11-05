"""
tests/test_mcp_forge_hardening.py
=================================
The pre-ship hardening batch for the MCP-forge architecture:

1. Forge provenance  - goal text + version bump in manifest.json,
                       server.py.bak snapshots and rollback.
2. Secret-free spawn - the child environment is a minimal whitelist plus
                       the server's declared "env", never os.environ.
3. Lazy + kill switch- forged servers register without spawning, wake on
                       first call, and refuse everything when the global
                       "forged tools" setting is off (per-server disabled
                       is honoured too).
4. Audit trail       - every routed call lands in a persistent JSONL with
                       redacted arguments, outcome and duration.

Everything here is deterministic: no LLM, no network, no live subprocess.
"""

import json
import time
from pathlib import Path

import pytest

from core.audit_log import AuditLog, redact_args
from core.dynamic_registry import DynamicSkill, DynamicToolRegistry
from core.mcp_forge import MCPForge
from core.mcp_manager import (
    MCPServerInstance,
    UniversalMCPManager,
    _minimal_base_env,
)


# ── 1. Forge provenance ──────────────────────────────────────────────────────


def test_bump_version_reforges_patch_number():
    assert MCPForge._bump_version("1.0.2") == "1.0.3"
    assert MCPForge._bump_version("2.1.9") == "2.1.10"
    assert MCPForge._bump_version(None) == "1.0.1"
    assert MCPForge._bump_version("garbage") == "1.0.1"


def test_emit_files_stores_goal_and_first_forged(tmp_path, monkeypatch):
    monkeypatch.setattr("core.mcp_forge.MCP_SKILLS_DIR", tmp_path)
    manifest = {"name": "demo", "description": "d", "tools": []}
    first = MCPForge._emit_files("demo", "print(1)\n", manifest, [{}],
                                 goal="track my sprinklers")
    m = json.loads(first[1].read_text(encoding="utf-8"))
    assert m["goal"] == "track my sprinklers"
    assert m["version"] == "1.0.0"
    assert m["reforged"] is False
    first_ts = m["first_forged_at"]
    assert m["created_at"] >= first_ts

    # A reforge replays the goal, bumps the version, keeps the first date.
    manifest2 = {"name": "demo", "description": "d2", "tools": [],
                 "goal": m["goal"], "first_forged_at": m["first_forged_at"],
                 "created_at": m["created_at"]}
    MCPForge._emit_files("demo", "print(2)\n", manifest2, [{}],
                         goal="track my sprinklers", prev_version="1.0.0")
    m2 = json.loads(first[1].read_text(encoding="utf-8"))
    assert m2["version"] == "1.0.1"
    assert m2["previous_version"] == "1.0.0"
    assert m2["reforged"] is True
    assert m2["first_forged_at"] == first_ts
    assert m2["goal"] == "track my sprinklers"


def test_snapshot_and_restore_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.mcp_forge.MCP_SKILLS_DIR", tmp_path)
    server_dir = tmp_path / "demo"
    server_dir.mkdir()
    (server_dir / "server.py").write_text("OLD", encoding="utf-8")
    (server_dir / "manifest.json").write_text("{}", encoding="utf-8")

    pairs = MCPForge._snapshot_for_rollback("demo")
    assert len(pairs) == 2
    assert (server_dir / "server.py.bak").read_text(encoding="utf-8") == "OLD"

    # The reforge overwrites, then the failed checks roll it back.
    (server_dir / "server.py").write_text("BROKEN", encoding="utf-8")
    MCPForge._restore_snapshot(pairs)
    assert (server_dir / "server.py").read_text(encoding="utf-8") == "OLD"


def test_snapshot_on_brand_new_server_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr("core.mcp_forge.MCP_SKILLS_DIR", tmp_path)
    assert MCPForge._snapshot_for_rollback("never_forged") == []


# ── 2. Secret-free child environment ─────────────────────────────────────────


def test_minimal_base_env_never_leaks_secret_like_vars(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SECRET-XYZ")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRET-XYZ")
    monkeypatch.setenv("PATH", "C:\\keep\\me")
    base = _minimal_base_env()
    assert base.get("PATH") == "C:\\keep\\me"
    assert "GEMINI_API_KEY" not in base
    assert "AWS_SECRET_ACCESS_KEY" not in base
    assert not any("SECRET" in k.upper() for k in base)


def test_instance_defaults_lazy_only_for_forged_configs():
    forged = MCPServerInstance("f", {"command": "python", "args": ["s.py"],
                                     "source": "saturn_mcp_forge"})
    assert forged.is_forged and forged.lazy
    manual = MCPServerInstance("m", {"command": "npx", "args": ["-y", "x"]})
    assert not manual.is_forged and not manual.lazy
    override = MCPServerInstance("o", {"command": "python", "args": ["s.py"],
                                       "source": "saturn_mcp_forge", "lazy": False})
    assert override.is_forged and not override.lazy


# ── 3+4. Routing gates, lazy start and the audit trail ───────────────────────


def _bare_manager():
    UniversalMCPManager._instance = None
    return UniversalMCPManager.get_instance()


def _fake_server(name="alpha", **overrides):
    fake = type("S", (), {})()
    fake.is_connected = False
    fake.is_forged = True
    fake.lazy = True
    fake.error = None
    fake.proc = None
    fake.config = {"source": "saturn_mcp_forge"}
    fake._last_start_attempt = 0.0
    fake.tools = []
    fake.call_tool = lambda n, a: {"success": True, "output": "ok"}
    for k, v in overrides.items():
        setattr(fake, k, v)
    return fake


def test_kill_switch_blocks_forged_but_not_manual(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: False)
    mgr = _bare_manager()
    forged = _fake_server(is_connected=True)
    manual = _fake_server(is_forged=False, config={}, is_connected=True)
    mgr._servers["alpha"] = forged
    mgr._servers["man"] = manual

    out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {})
    assert out.startswith("Error: forged tools are currently disabled")
    out2 = mgr.execute_mcp_tool("mcp_man_tool_y", {})
    assert out2 == "ok"  # hand-configured servers are untouched


def test_per_server_disabled_is_honoured_at_call_time(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    mgr = _bare_manager()
    mgr._servers["alpha"] = _fake_server(
        config={"source": "saturn_mcp_forge", "disabled": True})
    out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {})
    assert "disabled" in out


def test_lazy_server_wakes_on_first_call(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    mgr = _bare_manager()
    wakes = []

    def _start(timeout=25):
        wakes.append(timeout)
        fake.is_connected = True
        return True

    fake = _fake_server(start=_start)
    mgr._servers["alpha"] = fake

    out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {})
    assert out == "ok"
    assert wakes == [25]


def test_failed_start_is_not_retried_inside_backoff_window(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    mgr = _bare_manager()
    tries = []

    def _start(timeout=25):
        tries.append(timeout)
        fake.error = "boot failed"
        return False

    fake = _fake_server(start=_start)
    mgr._servers["alpha"] = fake
    mgr.execute_mcp_tool("mcp_alpha_tool_x", {})
    assert tries == [25]
    # A second call lands inside _START_RETRY_SECONDS: no re-spawn hammering.
    fake._last_start_attempt = time.time()
    out = mgr.execute_mcp_tool("mcp_alpha_tool_x", {})
    assert tries == [25]
    assert "offline" in out


def test_lazy_declarations_come_from_the_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    server_dir = tmp_path / "demo_srv"
    server_dir.mkdir()
    (server_dir / "server.py").write_text("x", encoding="utf-8")
    (server_dir / "manifest.json").write_text(json.dumps({
        "name": "demo_srv", "tier": "portable",
        "tools": [{"name": "look_up", "description": "looks things up",
                   "inputSchema": {"type": "object", "properties": {
                       "q": {"type": "string"}}, "required": ["q"]}}],
    }), encoding="utf-8")

    UniversalMCPManager._instance = None
    mgr = UniversalMCPManager.get_instance()
    inst = MCPServerInstance("demo_srv", {
        "command": "python", "args": [str(server_dir / "server.py")],
        "source": "saturn_mcp_forge"})
    mgr._servers["demo_srv"] = inst

    decls = mgr.get_all_tool_declarations()
    row = next(d for d in decls if d["name"] == "mcp_demo_srv_look_up")
    assert row["parameters"]["properties"]["q"]["type"] == "STRING"
    assert row["parameters"]["required"] == ["q"]


def test_execute_records_redacted_audit_entries(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    mgr = _bare_manager()
    mgr._servers["alpha"] = _fake_server(is_connected=True)
    mgr.execute_mcp_tool(
        "mcp_alpha_tool_x",
        {"city": "Pune", "api_key": "HACKME", "page": 2})

    entries = AuditLog.recent(limit=5)
    assert len(entries) == 1
    e = entries[0]
    assert e["tool"] == "mcp_alpha_tool_x"
    assert e["ok"] is True
    assert e["duration_ms"] >= 0
    assert e["args"]["city"] == "Pune"
    assert e["args"]["api_key"] == "***redacted***"
    assert e["args"]["page"] == 2
    assert "HACKME" not in json.dumps(e)


# ── 4b. Audit module itself ──────────────────────────────────────────────────


def test_redact_args_scrubs_masks_and_caps():
    red = redact_args({
        "token": "abc",
        "nested": {"client_secret": "s", "note": "hello"},
        "long": "x" * 500,
        "items": list(range(50)),
    })
    assert red["token"] == "***redacted***"
    assert red["nested"]["client_secret"] == "***redacted***"
    assert red["nested"]["note"] == "hello"
    assert len(red["long"]) < 300
    assert len(red["items"]) == 21  # 20 kept + one "+more" marker
    assert redact_args(None) == {}


def test_audit_recent_orders_oldest_first_and_limits():
    for i in range(5):
        AuditLog.record(f"tool_{i}", True, 1.0, {"i": i})
    entries = AuditLog.recent(limit=3)
    assert [e["tool"] for e in entries] == ["tool_2", "tool_3", "tool_4"]


# ── 3b/4c. Forged-feature side (dynamic registry) ────────────────────────────


def _skill(name, author, active=True):
    manifest = {"name": name, "description": "d", "author": author,
                "active": active,
                "parameters": {"type": "OBJECT", "properties": {}}}
    return DynamicSkill(Path("nowhere.py"), manifest)


def test_dynamic_skill_forged_flag_follows_author():
    assert _skill("f", "Project Saturn Autonomous Self-Evolution Engine").forged
    assert not _skill("n", "someone@handwritten").forged


def test_kill_switch_hides_and_blocks_forged_features(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: False)
    forged = _skill("forged_feat", "Project Saturn Autonomous")
    native = _skill("native_feat", "handwritten")
    monkeypatch.setattr(DynamicToolRegistry, "_skills",
                        {"forged_feat": forged, "native_feat": native})
    monkeypatch.setattr(DynamicToolRegistry, "_initialized", True)

    names = {d["name"] for d in DynamicToolRegistry.get_tool_declarations()}
    assert "native_feat" in names and "forged_feat" not in names

    out = DynamicToolRegistry.execute_sync("forged_feat", {})
    assert out.startswith("Error: forged tools are currently disabled")


def test_forged_feature_call_lands_in_audit(monkeypatch):
    monkeypatch.setattr("core.mcp_manager.forged_tools_enabled", lambda: True)
    skill = _skill("audit_feat", "Project Saturn Autonomous")
    calls = []

    def _sync(**kw):
        calls.append(kw)
        return "done"

    monkeypatch.setattr(skill, "execute_sync", _sync)
    monkeypatch.setattr(DynamicToolRegistry, "_skills", {"audit_feat": skill})
    monkeypatch.setattr(DynamicToolRegistry, "_initialized", True)

    assert DynamicToolRegistry.execute_sync("audit_feat", {"a": 1}) == "done"
    e = AuditLog.recent(limit=1)[-1]
    assert e["tool"] == "audit_feat"
    assert e["tier"] == "feature"
    assert e["ok"] is True and e["args"] == {"a": 1}
