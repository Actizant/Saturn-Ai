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

import json
import logging
import os
import re
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

        # Emit files.
        try:
            server_path, manifest_path = cls._emit_files(actual_name, code, manifest, test_cases)
        except Exception as exc:
            return {"success": False, "message": f"Failed writing MCP server files: {exc}"}

        # Register live. If this fails we still keep the files so the user can
        # debug or add them by hand.
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

        return {
            "success": True,
            "tier": "portable",
            "name": actual_name,
            "description": manifest.get("description", ""),
            "skill_path": str(server_path),
            "manifest_path": str(manifest_path),
            "tools": [t.get("name") for t in manifest.get("tools", []) if t.get("name")],
            "live_registered": reg_ok,
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

    @classmethod
    def _emit_files(
        cls,
        name: str,
        code: str,
        manifest: Dict[str, Any],
        test_cases: List[Dict[str, Any]],
    ) -> Tuple[Path, Path]:
        """Write server.py + manifest.json + test_cases.json under mcp_skills/<name>/."""
        target = cls._server_dir(name)

        manifest["name"] = name
        manifest["tier"] = "portable"
        manifest["created_at"] = time.time()
        manifest["version"] = manifest.get("version", "1.0.0")
        manifest["author"] = "Project Saturn Autonomous MCP Forge"
        manifest["active"] = True

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
