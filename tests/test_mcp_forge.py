"""Portable-tier tests for the Saturn MCP Forge.

Nothing here needs a Gemini key, a real network, or the `mcp` package
installed: we prove the classifier, the file emitter, the AST validator,
and the hot-register persistence contract in isolation.
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from core import mcp_forge
from core.mcp_forge import MCPForge, classify_tier
from core.skill_crucible import SkillCrucible


# ── classify_tier ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("goal", [
    "learn a skill that shows cricket scores",
    "give me a tool that checks the price of bitcoin",
    "make a feature that shows ascii banners",
])
def test_classifier_defaults_to_lightweight(goal):
    assert classify_tier(goal) == "lightweight"


@pytest.mark.parametrize("goal", [
    "build a portable mcp server that reports cricket scores",
    "make a shareable tool I can plug into Claude Desktop",
    "create a standalone server that shows flight ET plans",
    "give me an MCP server for my Cursor editor",
])
def test_classifier_picks_portable_on_signal(goal):
    assert classify_tier(goal) == "portable"


def test_classifier_reads_name_hint_suffix():
    assert classify_tier("check something", "weather_server") == "portable"
    assert classify_tier("check something", "weather_mcp") == "portable"
    assert classify_tier("check something", "weather") == "lightweight"


# ── validate_ast_server ──────────────────────────────────────────────────────

GOOD_SERVER = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")

@mcp.tool()
def echo(text: str) -> str:
    """Echo back the input."""
    return text

if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def test_ast_validator_accepts_a_real_fastmcp_shape():
    ok, err = SkillCrucible.validate_ast_server(GOOD_SERVER)
    assert ok is True
    assert err is None


def test_ast_validator_rejects_missing_fastmcp_import():
    code = GOOD_SERVER.replace("from mcp.server.fastmcp import FastMCP", "")
    ok, err = SkillCrucible.validate_ast_server(code)
    assert ok is False
    assert "FastMCP" in (err or "")


def test_ast_validator_rejects_server_with_no_tools():
    code = GOOD_SERVER.replace("@mcp.tool()\n", "")
    ok, err = SkillCrucible.validate_ast_server(code)
    assert ok is False
    assert "@mcp.tool()" in (err or "")


def test_ast_validator_rejects_missing_stdio_boot():
    code = GOOD_SERVER.replace('mcp.run(transport="stdio")', "mcp.run()")
    ok, err = SkillCrucible.validate_ast_server(code)
    assert ok is False
    assert "stdio" in (err or "")


def test_ast_validator_blocks_destructive_patterns():
    code = GOOD_SERVER.replace('"""Echo back the input."""', '"""wipe system32 please"""')
    ok, err = SkillCrucible.validate_ast_server(code)
    assert ok is False
    assert "Security Violation" in (err or "")


# ── _emit_files ──────────────────────────────────────────────────────────────

def test_emit_files_writes_server_manifest_tests_and_readme(tmp_path):
    with patch.object(mcp_forge, "MCP_SKILLS_DIR", tmp_path):
        manifest = {
            "name": "demo_server",
            "description": "Demo",
            "tools": [
                {"name": "echo", "description": "Echoes", "inputSchema": {"type": "object", "properties": {}}},
            ],
        }
        tests = [{"tool": "echo", "args": {"text": "hi"}}]

        server_path, manifest_path = MCPForge._emit_files("demo_server", GOOD_SERVER, manifest, tests)

        assert server_path.exists()
        assert manifest_path.exists()
        assert (tmp_path / "demo_server" / "test_cases.json").exists()
        assert (tmp_path / "demo_server" / "README.md").exists()

        # README tells a stranger how to plug this into their own MCP client.
        readme = (tmp_path / "demo_server" / "README.md").read_text(encoding="utf-8")
        assert "demo_server" in readme
        assert "Add to another MCP client" in readme
        assert "echo" in readme

        # Manifest got stamped with the Saturn-forge provenance.
        written = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert written["tier"] == "portable"
        assert written["author"] == "Project Saturn Autonomous MCP Forge"
        assert written["active"] is True


def test_list_forged_enumerates_persisted_servers(tmp_path):
    with patch.object(mcp_forge, "MCP_SKILLS_DIR", tmp_path):
        assert MCPForge.list_forged() == []

        MCPForge._emit_files(
            "alpha_server",
            GOOD_SERVER,
            {"name": "alpha_server", "description": "A", "tools": []},
            [{"tool": None, "args": {}}],
        )
        MCPForge._emit_files(
            "beta_server",
            GOOD_SERVER,
            {"name": "beta_server", "description": "B", "tools": []},
            [{"tool": None, "args": {}}],
        )

        forged = {m["name"]: m for m in MCPForge.list_forged()}

    assert set(forged) == {"alpha_server", "beta_server"}
    assert forged["alpha_server"]["tier"] == "portable"


# ── UniversalMCPManager.hot_register / hot_unregister ────────────────────────

def _manager():
    from core.mcp_manager import UniversalMCPManager
    # Reset the singleton each test so state never leaks across them.
    UniversalMCPManager._instance = None
    return UniversalMCPManager.get_instance()


def test_hot_register_persists_then_starts(tmp_path):
    from core import mcp_manager as mm

    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    fake_instance = mm.MCPServerInstance("demo", {})
    fake_instance.tools = [{"name": "echo"}]
    fake_instance.is_connected = True

    def fake_ctor(name, cfg):
        return fake_instance

    with (
        patch.object(mm, "MCP_CONFIG_FILE", config_path),
        patch.object(mm, "MCPServerInstance", fake_ctor),
    ):
        mgr = _manager()
        ok, msg = mgr.hot_register("demo", {"command": "python", "args": ["x.py"]})

    assert ok is True
    assert "1 tool" in msg
    # Config file now contains the entry so a restart picks it up too.
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "demo" in data["mcpServers"]
    assert data["mcpServers"]["demo"]["command"] == "python"


def test_hot_register_rolls_back_the_file_when_boot_fails(tmp_path):
    from core import mcp_manager as mm

    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    fake_instance = mm.MCPServerInstance("bad", {})
    fake_instance.error = "server crashed at hello"
    fake_instance.is_connected = False

    def fake_start(timeout=20):
        return False

    fake_instance.start = fake_start  # type: ignore[assignment]

    with (
        patch.object(mm, "MCP_CONFIG_FILE", config_path),
        patch.object(mm, "MCPServerInstance", lambda name, cfg: fake_instance),
    ):
        mgr = _manager()
        ok, msg = mgr.hot_register("bad", {"command": "python", "args": ["broken.py"]})

    assert ok is False
    assert "crashed" in msg
    # Rolled back: file is back to the empty mcpServers map.
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "bad" not in data["mcpServers"]


def test_hot_unregister_stops_and_removes_from_disk(tmp_path):
    from core import mcp_manager as mm

    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(
        json.dumps({"mcpServers": {"temp": {"command": "python", "args": ["x"]}}}),
        encoding="utf-8",
    )

    fake_instance = mm.MCPServerInstance("temp", {})
    fake_instance.is_connected = True
    stopped = []
    fake_instance.stop = lambda: stopped.append(True)  # type: ignore[assignment]

    mgr = _manager()
    mgr._servers["temp"] = fake_instance

    with patch.object(mm, "MCP_CONFIG_FILE", config_path):
        ok, _ = mgr.hot_unregister("temp")

    assert ok is True
    assert stopped == [True]
    data = json.loads(config_path.read_text(encoding="utf-8"))
    assert "temp" not in data["mcpServers"]
    assert "temp" not in mgr._servers


def test_hot_register_rejects_when_already_live(tmp_path):
    from core import mcp_manager as mm

    config_path = tmp_path / "mcp_servers.json"
    config_path.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

    live = mm.MCPServerInstance("dup", {})
    live.is_connected = True

    mgr = _manager()
    mgr._servers["dup"] = live

    with patch.object(mm, "MCP_CONFIG_FILE", config_path):
        ok, msg = mgr.hot_register("dup", {"command": "python", "args": ["x"]})

    assert ok is False
    assert "already registered" in msg


# ── SkillForge dispatch ──────────────────────────────────────────────────────

def test_skillforge_forwards_portable_goals_to_mcp_forge():
    from core import skill_forge

    with patch.object(
        mcp_forge.MCPForge,
        "forge_mcp_server",
        return_value={"success": True, "tier": "portable", "name": "x"},
    ) as mock:
        result = skill_forge.SkillForge.forge_skill(
            "build a portable mcp server that shows time in Tokyo",
            skill_name="tokyo_time",
        )

    assert result["tier"] == "portable"
    mock.assert_called_once()


def test_skillforge_leaves_lightweight_goals_untouched():
    """A plain goal should NOT call MCPForge - it stays on the in-process path."""
    from core import skill_forge

    with (
        patch.object(mcp_forge.MCPForge, "forge_mcp_server") as mcp_mock,
        patch.object(
            skill_forge.SkillForge, "_call_llm_synthesizer",
            return_value={"success": False, "error": "no key"},
        ),
    ):
        result = skill_forge.SkillForge.forge_skill("check Bitcoin price")

    mcp_mock.assert_not_called()
    assert result["success"] is False
