"""
Saturn MCP Forge - the portable tier of self-extension
======================================================

Turns a natural-language goal into a real, spec-compliant MCP server that
Saturn itself - and any other MCP-speaking client (Claude Desktop, Cursor,
Qoder, anything) - can consume.

This lives *next to* SkillForge, not on top of it. Lightweight skills
(single ``execute()`` function, standard library, one-shot answers) still
compile to ``features/<name>.py`` and stay exactly as fast as before. When
a goal signals a richer, shareable capability - multiple verbs, external
dependencies, or an explicit request for a "server" or "portable tool" -
the SkillForge dispatches here instead.

The pipeline:

    goal -> classify_tier() -> "portable"
         -> LLM synthesises FastMCP source + manifest
         -> SkillCrucible.validate_mcp_server() spawns it over stdio,
            speaks JSON-RPC, calls every tool
         -> files land in %LOCALAPPDATA%\\SaturnAI\\mcp_skills\\<name>\\
         -> UniversalMCPManager.hot_register() adds it to
            mcp_servers.json and starts it live - no restart

Result shape is deliberately compatible with the lightweight SkillForge,
so ``_forge_skill`` in main.py and ``_run_skill_forge`` in the executor
work unchanged for either tier.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.gemini_models import ladder as _model_ladder
from core.user_paths import get_user_data_dir

logger = logging.getLogger("MCPForge")

MCP_SKILLS_DIR = get_user_data_dir() / "mcp_skills"

# Phrases in a user goal that flip the tier from lightweight to portable.
# Deliberately conservative - we do NOT want trivial skills becoming
# subprocess-spawned MCP servers. When in doubt, the lightweight tier wins.
PORTABLE_SIGNALS: Tuple[str, ...] = (
    "mcp",
    "mcp server",
    "portable tool",
    "portable skill",
    "shareable tool",
    "shareable skill",
    "publish as a tool",
    "publish as an mcp",
    "server that",
    "server which",
    "standalone server",
    "multiple tools",
    "many tools",
    "several tools",
    "tool with resources",
    "for claude desktop",
    "for cursor",
    "for any ai",
)


def classify_tier(goal: str, name_hint: str = "", context_hints: str = "") -> str:
    """Return 'lightweight' or 'portable' from a purely lexical check.

    No LLM call. Two reasons:
    1. The default must be 'lightweight' - in-process is faster and simpler,
       and we do not want the pipeline paying a subprocess spawn for a
       cricket-score lookup.
    2. The user's intent is usually explicit: 'make an MCP server',
       'build a portable tool', 'I want to use this in Cursor'.
    """
    text = f"{goal} {name_hint} {context_hints}".lower()
    if any(sig in text for sig in PORTABLE_SIGNALS):
        return "portable"
    # An underscore-suffixed name hint like 'foo_server' or 'foo_mcp' is
    # a strong author-side signal.
    if name_hint.lower().endswith(("_server", "_mcp")):
        return "portable"
    return "lightweight"


def _scan_needs(src: str) -> List[str]:
    needs: List[str] = []
    if re.search(r"\bsubprocess\b|\bos\.system\b|\bos\.popen\b|\bpty\b", src):
        needs.append("commands")
    if re.search(r"\brequests\b|\burllib\b|\bhttpx\b|\baiohttp\b|\bsocket\b|\burlopen\b", src):
        needs.append("network")
    if re.search(r"\bopen\s*\(|\bPath\b|\bpathlib\b|\bshutil\b|\bos\.remove\b|\bos\.mkdir\b|\bglob\b", src):
        needs.append("files")
    return needs


def infer_declared_needs(code: str, tool_name: Optional[str] = None) -> List[str]:
    """Scan synthesised server source for the capability buckets it touches.

    The Creators page shows a NETWORK / FILES / COMMANDS grid per tool, and
    the approval gate needs to know what a verb can do before it has ever
    run. The model *declares* needs in the manifest; this scan is both a
    safety net (a tool that shells out while declaring nothing still gets
    'commands') and the fallback for manifests written before the field
    existed.

    When ``tool_name`` is given we scope the scan to just that function's
    body (via AST), so a server that only fetches URLs in one verb does not
    smear 'network' across every other pure-calculation verb. If the named
    function cannot be found we return nothing rather than the whole-module
    floor, because at that point the model's own declaration is the better
    signal. With no ``tool_name`` (a single-purpose lightweight feature) we
    scan the whole source.
    """
    src = code or ""
    if tool_name:
        try:
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == tool_name:
                    body = ast.get_source_segment(src, node) or ""
                    return _scan_needs(body)
        except Exception:
            pass
        return []
    return _scan_needs(src)


def _get_gemini_api_key() -> str:
    if not hasattr(sys.modules[__name__], "_API_CONFIG_PATH"):
        globals()["_API_CONFIG_PATH"] = get_user_data_dir() / "config" / "api_keys.json"
    path = _API_CONFIG_PATH
    if path.exists():
        try:
            from core.secret_store import load_json
            data = load_json(path)
            key = data.get("gemini_api_key", "").strip()
            if key:
                return key
        except Exception:
            pass
    return (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()


class MCPForge:
    """Autonomous synthesiser of portable MCP servers."""

    @classmethod
    def forge_mcp_server(
        cls,
        goal: str,
        skill_name: Optional[str] = None,
        context_hints: str = "",
        max_repair_attempts: int = 2,
    ) -> Dict[str, Any]:
        """Full portable-tier pipeline: synthesise -> validate -> emit -> register."""
        logger.info(f"[MCPForge] Initiating portable synthesis for: '{goal}'")

        name_hint = re.sub(r"[^a-zA-Z0-9_]", "_", (skill_name or "").lower()).strip("_")

        synthesis = cls._call_llm_synthesizer(goal, name_hint, context_hints)
        if not synthesis.get("success"):
            return {
                "success": False,
                "message": f"MCP synthesis failed: {synthesis.get('error')}",
            }

        manifest = synthesis["manifest"]
        code = synthesis["code"]
        test_cases = synthesis.get("test_cases") or [{"tool": None, "args": {}}]
        actual_name = manifest.get("name") or name_hint or "custom_mcp_server"

        # Iterative verification / repair loop.
        from core.skill_crucible import SkillCrucible

        # Static sanity: no banned patterns, must import mcp.
        ast_ok, ast_err = SkillCrucible.validate_ast_server(code)
        if not ast_ok:
            if max_repair_attempts > 0:
                repair = cls._repair_code(code, ast_err, goal)
                if repair.get("success"):
                    code = repair["code"]
                    ast_ok, ast_err = SkillCrucible.validate_ast_server(code)
            if not ast_ok:
                return {"success": False, "message": f"AST rejected MCP server: {ast_err}"}

        # Dependency pass - the same pip resolver SkillForge uses.
        deps = SkillCrucible.extract_dependencies(code)
        deps_ok, deps_msg = SkillCrucible.resolve_dependencies(deps)
        if not deps_ok:
            return {"success": False, "message": f"Dependency resolution failed: {deps_msg}"}

        # Live subprocess handshake + tools/list + tools/call for every test case.
        for attempt in range(max_repair_attempts + 1):
            val_ok, val_msg, telemetry = SkillCrucible.validate_mcp_server(code, test_cases)
            if val_ok:
                break
            if attempt < max_repair_attempts:
                logger.warning(f"[MCPForge] Validation failed ({val_msg}). Asking LLM to repair...")
                repair = cls._repair_code(code, val_msg, goal)
                if repair.get("success"):
                    code = repair["code"]
                    continue
            return {
                "success": False,
                "message": f"MCP sandbox rejected server: {val_msg}",
                "telemetry": telemetry,
            }

        # A reforge replaces a server that is already live. Snapshot the old
        # files first so a new build that fails its live checks can be rolled
        # back instead of leaving Saturn with a broken capability on disk.
        rolled_back = cls._snapshot_for_rollback(actual_name)
        prev_version = None
        if rolled_back:
            try:
                old_manifest = json.loads(
                    (Path(rolled_back[0]).parent / "manifest.json").read_text(encoding="utf-8"))
                prev_version = old_manifest.get("version")
            except Exception:
                prev_version = None

        # Emit files.
        try:
            server_path, manifest_path = cls._emit_files(
                actual_name, code, manifest, test_cases, goal=goal, prev_version=prev_version)
        except Exception as exc:
            if rolled_back:
                cls._restore_snapshot(rolled_back)
            return {"success": False, "message": f"Failed writing MCP server files: {exc}"}

        # Register live. hot_register boots the freshly written server.py and
        # leaves any previous instance untouched when the boot fails, so a
        # failed reforge means the new files are bad while the old capability
        # is still serving from memory. Roll the folder back and keep that old
        # build registered so nothing downstream sees the broken rewrite.
        from core.mcp_manager import UniversalMCPManager
        cfg = {
            "command": _mcp_server_python(),
            "args": [str(server_path)],
            "disabled": False,
            "description": f"[Saturn-forged] {manifest.get('description', actual_name)}",
            "source": "saturn_mcp_forge",
        }
        reg_ok, reg_msg = UniversalMCPManager.get_instance().hot_register(actual_name, cfg)
        if not reg_ok:
            logger.warning(f"[MCPForge] Live registration failed: {reg_msg}. Files still on disk.")
            if rolled_back:
                cls._restore_snapshot(rolled_back)
                logger.info(f"[MCPForge] Rolled '{actual_name}' back to its previous build.")
                return {
                    "success": False,
                    "tier": "portable",
                    "name": actual_name,
                    "rolled_back": True,
                    "message": (
                        f"Re-forge of '{actual_name}' failed its live checks "
                        f"({reg_msg}); rolled back to the previous build, which "
                        f"stays registered and running."
                    ),
                }

        return {
            "success": True,
            "tier": "portable",
            "name": actual_name,
            "description": manifest.get("description", ""),
            "skill_path": str(server_path),
            "manifest_path": str(manifest_path),
            "tools": [t.get("name") for t in manifest.get("tools", []) if t.get("name")],
            "live_registered": reg_ok,
            "version": manifest.get("version", "1.0.0"),
            "prev_version": prev_version,
            "message": (
                f"Forged portable MCP server '{actual_name}'. "
                f"{len(manifest.get('tools', []))} tool(s) verified in the sandbox; "
                f"{'hot-loaded into Saturn live' if reg_ok else 'files on disk - live registration pending'}."
            ),
            "manifest": manifest,
        }

    # ── Emit / paths ──────────────────────────────────────────────────────────

    @staticmethod
    def _server_dir(name: str) -> Path:
        d = MCP_SKILLS_DIR / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _snapshot_for_rollback(name: str) -> list:
        """Copy the live server.py + manifest.json to ``*.bak`` before a
        (re)forge overwrites them. Returns the (original, backup) pairs so
        ``_restore_snapshot`` can put the previous build back when the new
        one fails its live checks. Empty list = brand-new server, nothing
        to roll back to."""
        target = MCP_SKILLS_DIR / name
        pairs = []
        for fname in ("server.py", "manifest.json"):
            src = target / fname
            if src.exists():
                dst = src.with_name(fname + ".bak")
                try:
                    shutil.copy2(src, dst)
                    pairs.append((src, dst))
                except Exception as exc:
                    logger.warning(f"[MCPForge] Could not back up {src.name}: {exc}")
        return pairs

    @staticmethod
    def _restore_snapshot(pairs: list) -> None:
        """Put the pre-reforge files back over the failed rewrite."""
        for src, bak in pairs:
            try:
                shutil.copy2(bak, src)
            except Exception as exc:
                logger.warning(f"[MCPForge] Rollback of {src.name} failed: {exc}")

    @staticmethod
    def _bump_version(prev: Optional[str]) -> str:
        """1.0.3 -> 1.0.4 on a reforge; anything unreadable restarts at
        1.0.1 so the manifest always carries a sane dotted version."""
        parts = str(prev or "").strip().split(".")
        if len(parts) == 3 and parts[-1].isdigit():
            parts[-1] = str(int(parts[-1]) + 1)
            return ".".join(parts)
        return "1.0.1"

    @classmethod
    def _emit_files(
        cls,
        name: str,
        code: str,
        manifest: Dict[str, Any],
        test_cases: List[Dict[str, Any]],
        goal: str = "",
        prev_version: Optional[str] = None,
    ) -> Tuple[Path, Path]:
        """Write server.py + manifest.json + test_cases.json under mcp_skills/<name>/."""
        target = cls._server_dir(name)

        manifest["name"] = name
        manifest["tier"] = "portable"
        # The original request text survives in the manifest so the Creators
        # page can show "Made from: ..." and so a reforge (manual or by the
        # health sweep) can replay the goal instead of a description guess.
        if goal:
            manifest["goal"] = str(goal)
        first_forged = manifest.get("first_forged_at") or manifest.get("created_at")
        manifest["first_forged_at"] = first_forged or time.time()
        manifest["created_at"] = time.time()
        manifest["reforged"] = bool(prev_version)
        if prev_version:
            manifest["previous_version"] = prev_version
            manifest["version"] = cls._bump_version(prev_version)
        else:
            manifest["version"] = manifest.get("version", "1.0.0")
        manifest["author"] = "Project Saturn Autonomous MCP Forge"
        manifest["active"] = True
        # Per-tool declared needs, the raw material for the approval gate and
        # the Creators page's NETWORK/FILES/COMMANDS grid. The model declares
        # them; a scan of the actual source is the safety net and the union
        # wins, so a verb that shells out cannot declare its way to silence.
        from core.permissions import DECLARED_NEEDS as _VALID_NEEDS
        from core.permissions import classify_defaults as _classify
        for t in manifest.get("tools", []) or []:
            if not isinstance(t, dict):
                continue
            declared = [str(r).lower() for r in (t.get("requires") or [])
                        if str(r).lower() in _VALID_NEEDS]
            t["requires"] = sorted(set(declared)
                                   | set(infer_declared_needs(code, t.get("name"))))
            t["permission_default"] = _classify(
                t["requires"], str(t.get("name", "")),
                str(t.get("description", "")))

        header = (
            f'"""\n'
            f"Saturn-forged MCP server: {name}\n"
            f"{manifest.get('description', '')}\n\n"
            f"Synthesised autonomously by Saturn. Speaks MCP over stdio.\n"
            f"Plug this into any MCP client (Saturn, Claude Desktop, Cursor, Qoder).\n"
            f'"""\n\n'
        )
        server_code = code if code.lstrip().startswith(("from mcp", "import mcp")) else header + code

        server_path = target / "server.py"
        server_path.write_text(server_code, encoding="utf-8")

        manifest_path = target / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=4), encoding="utf-8")

        tests_path = target / "test_cases.json"
        tests_path.write_text(json.dumps(test_cases, indent=4), encoding="utf-8")

        # A sibling README so anyone browsing the folder sees what it is and
        # how to add it to another MCP client. Cheap, high-signal.
        readme = target / "README.md"
        readme.write_text(
            f"# {name}\n\n"
            f"{manifest.get('description', '')}\n\n"
            f"A portable MCP server synthesised by Saturn.\n\n"
            f"## Tools exposed\n\n"
            + "\n".join(
                f"- `{t.get('name')}` — {t.get('description','')}"
                for t in manifest.get("tools", [])
            )
            + "\n\n## Run it manually\n\n"
            f"```bash\npython server.py\n```\n\n"
            f"## Add to another MCP client\n\n"
            f"```json\n"
            + json.dumps(
                {
                    name: {
                        "command": _mcp_server_python(),
                        "args": [str(server_path)],
                    }
                },
                indent=2,
            )
            + "\n```\n",
            encoding="utf-8",
        )

        return server_path, manifest_path

    @staticmethod
    def list_forged() -> List[Dict[str, Any]]:
        """Everything Saturn has ever forged as a portable MCP server."""
        out: List[Dict[str, Any]] = []
        if not MCP_SKILLS_DIR.exists():
            return out
        for item in sorted(MCP_SKILLS_DIR.iterdir()):
            if not item.is_dir():
                continue
            mp = item / "manifest.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text(encoding="utf-8"))
                m["_dir"] = str(item)
                out.append(m)
            except Exception:
                continue
        return out

    # ── LLM plumbing ─────────────────────────────────────────────────────────

    @classmethod
    def _call_llm_synthesizer(cls, goal: str, name_hint: str, context_hints: str) -> Dict[str, Any]:
        from core.skill_forge import SkillForge  # reuse the JSON extraction helper

        system = MCP_SYSTEM_INSTRUCTIONS

        prompt = (
            f"Synthesise a portable MCP server for this user request:\n"
            f"Goal: {goal}\n"
            f"Preferred name: {name_hint or 'auto_generate'}\n"
            f"Additional context: {context_hints}\n"
        )

        gemini_key = _get_gemini_api_key()
        if gemini_key:
            try:
                from google import genai
                client = genai.Client(api_key=gemini_key, http_options={"api_version": "v1beta"})
                for model in _model_ladder():
                    try:
                        resp = client.models.generate_content(
                            model=model,
                            contents=prompt,
                            config={
                                "temperature": 0.2,
                                "system_instruction": system,
                                "response_mime_type": "application/json",
                            },
                        )
                        raw = getattr(resp, "text", "") or ""
                        data = SkillForge._parse_json_response(raw)
                        if isinstance(data.get("manifest"), dict) and isinstance(data.get("code"), str):
                            data["success"] = True
                            return data
                    except Exception as exc:
                        logger.warning(f"[MCPForge] Model {model} failed: {exc}")
                        continue
            except Exception as exc:
                logger.error(f"[MCPForge] Gemini client error: {exc}")

        try:
            from llm_client import client as unified
            resp = unified.chat(prompt=prompt, system=system, max_tokens=8192, temperature=0.2)
            data = SkillForge._parse_json_response(resp)
            if isinstance(data.get("manifest"), dict) and isinstance(data.get("code"), str):
                data["success"] = True
                return data
            return {"success": False, "error": "LLM response missing manifest or code."}
        except Exception as exc:
            return {"success": False, "error": f"LLM synthesis failed: {exc}"}

    @classmethod
    def _repair_code(cls, broken: str, error_msg: str, goal: str) -> Dict[str, Any]:
        from core.skill_forge import SkillForge
        prompt = MCP_REPAIR_TEMPLATE.format(
            goal=goal, error=error_msg, code=broken,
        )
        gemini_key = _get_gemini_api_key()
        if not gemini_key:
            return {"success": False, "error": "No Gemini key for MCP repair."}
        try:
            from google import genai
            client = genai.Client(api_key=gemini_key, http_options={"api_version": "v1beta"})
            for model in _model_ladder():
                try:
                    resp = client.models.generate_content(
                        model=model,
                        contents=prompt,
                        config={"temperature": 0.1, "response_mime_type": "application/json"},
                    )
                    raw = getattr(resp, "text", "") or ""
                    data = SkillForge._parse_json_response(raw)
                    if "code" in data:
                        data["success"] = True
                        return data
                except Exception as exc:
                    logger.warning(f"[MCPForge] Repair model {model} failed: {exc}")
        except Exception as exc:
            logger.error(f"[MCPForge] Repair Gemini error: {exc}")
        return {"success": False, "error": "Repair attempt failed."}


def _mcp_server_python() -> str:
    """Absolute path to the interpreter that hosts Saturn's packages.

    We use python.exe (not pythonw.exe) so the child server can print to
    stdout without a console. This is what Saturn spawns when running the
    forged server; the same interpreter already has `mcp` installed via
    requirements.txt, so no venv gymnastics are needed.
    """
    exe = sys.executable or "python"
    if exe.lower().endswith("pythonw.exe"):
        # Strip the trailing 'w.exe' (5 chars) and put '.exe' back on.
        cand = exe[:-5] + ".exe"
        if Path(cand).exists():
            return cand
    return exe


# ── Prompt templates ─────────────────────────────────────────────────────────

MCP_SYSTEM_INSTRUCTIONS = """You are the Saturn Portable MCP Architect.
Your job is to synthesise a real, spec-compliant MCP server for the user's goal.

The generated server MUST:
1. Import `from mcp.server.fastmcp import FastMCP` and instantiate
   `mcp = FastMCP("<snake_case_name>")` at module scope.
2. Define every verb as a top-level function decorated with `@mcp.tool()`.
   Every function must have:
     - Python type annotations on all parameters (str, int, float, bool,
       Optional[...], etc.).
     - A clear one-line docstring. This becomes the tool description.
     - Return type of `str` (a human-readable summary) OR `dict` (structured).
       NEVER return None from a tool.
3. End with:
       if __name__ == "__main__":
           mcp.run(transport="stdio")
4. Handle real-world failures:
     - Wrap any HTTP in try/except; if the request fails, return a graceful
       text summary rather than raising. The MCP protocol turns exceptions
       into `isError` responses, and Saturn will reject the server.
     - Always pass `timeout=8` and leave TLS verification ON by default.
       Only if the first call raises `ssl.SSLCertVerificationError` (common
       on Windows where the system cert store drifts from certifi's) may
       the tool retry *once* with `verify=False` and log a warning. Never
       disable verification on the first try; never disable it for
       sensitive endpoints (auth, payments, personal data).
     - Never require an environment API key. Use public endpoints.
5. Zero subprocess spawning, no filesystem writes outside the deliverables
   dir, no destructive commands, no reading of Saturn's own secrets.

Output MUST be a single JSON object with exactly this shape:

{
  "manifest": {
    "name": "snake_case_server_name",
    "description": "One line. When should an assistant call this server?",
    "tools": [
      {
        "name": "tool_fn_name",
        "description": "what this specific verb does",
        "requires": ["network"],
        "inputSchema": {
          "type": "object",
          "properties": {
            "arg": {"type": "string", "description": "what arg means"}
          },
          "required": ["arg"]
        }
      }
    ]
  },
  "code": "the FULL content of server.py, imports + FastMCP(...) + @mcp.tool() defs + __main__ block",
  "test_cases": [
    {"tool": "tool_fn_name", "args": {"arg": "sample"}},
    {"tool": null, "args": {}}
  ]
}

The `test_cases` MUST contain at least one entry per declared tool plus one
discovery-only entry with `tool=null` (which just checks `tools/list`
returns successfully).

Each tool's `requires` is a list drawn from "network", "files", "commands"
(use [] for a pure in-process calculation) naming what that verb touches.
Saturn shows this declaration to the user and gates risky calls on it, so it
must be honest - an omitted need is caught by Saturn's own scan of the code
and only makes the gate stricter, never looser.

Return ONLY this JSON object with no markdown fences around it.
"""

MCP_REPAIR_TEMPLATE = """You are repairing a Python MCP server that Saturn generated for:
Goal: {goal}

The server failed verification. Reason:
{error}

Broken code:
```python
{code}
```

Repair rules:
1. Keep the FastMCP structure intact:
     from mcp.server.fastmcp import FastMCP
     mcp = FastMCP("...")
     @mcp.tool()
     def some_tool(x: str) -> str: ...
     if __name__ == "__main__":
         mcp.run(transport="stdio")
2. Do NOT change the tool names, only their bodies / imports / error handling.
3. Every @mcp.tool() function must return a str or dict, never raise.
4. Wrap network calls in try/except and provide a graceful string fallback.
5. Keep TLS verification on. `requests` calls always pass `timeout=8`.
   Only if the first attempt raises `ssl.SSLCertVerificationError` may a
   single retry use `verify=False` inside a `try/except` with a warning.

Return ONLY a JSON object:
{{
  "code": "the fully corrected server.py source"
}}
"""
