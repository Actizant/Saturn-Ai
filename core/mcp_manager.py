"""
core/mcp_manager.py
===================
Saturn - Universal Model Context Protocol (MCP) Manager.

Standard: JSON-RPC 2.0 over stdio (compatible with Claude Desktop & Claude Code).
Connects Saturn to any MCP server published on the internet:
- GitHub, GitLab, Jira, Linear
- PostgreSQL, MySQL, SQLite, Redis, MongoDB
- Playwright, Puppeteer (dynamic web browsing)
- Docker, Kubernetes, AWS
- Filesystem, Memory, Brave Search, Fetch, Slack, Notion

Reads config from `config/mcp_servers.json` and hot-exposes all discovered tools
directly to Gemini Live, OpenRouter, and the Saturn Omni-Planner.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from core.user_paths import get_user_data_dir

logger = logging.getLogger("MCPManager")
logger.setLevel(logging.INFO)

CONFIG_DIR = get_user_data_dir() / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
MCP_CONFIG_FILE = CONFIG_DIR / "mcp_servers.json"


class MCPServerInstance:
    """Manages an active stdio connection to a single MCP server."""

    def __init__(self, name: str, config: Dict[str, Any]):
        self.name = name
        self.config = config
        self.command = config.get("command", "")
        self.args = config.get("args", [])
        self.env_vars = config.get("env", {})
        
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._req_id = 0
        self._pending_requests: Dict[int, threading.Event] = {}
        self._responses: Dict[int, dict] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self.tools: List[Dict[str, Any]] = []
        self.is_connected = False
        self.error: Optional[str] = None

    def start(self, timeout: int = 20) -> bool:
        """Launches the server subprocess, completes JSON-RPC handshake, and fetches tools."""
        with self._lock:
            if self.is_connected:
                return True

            cmd_resolved = shutil.which(self.command) or self.command
            full_cmd = [cmd_resolved] + [str(a) for a in self.args]

            # Merge environment variables
            merged_env = os.environ.copy()
            merged_env.update({str(k): str(v) for k, v in self.env_vars.items()})

            try:
                logger.info(f"[MCP:{self.name}] Spawning process: {' '.join(full_cmd)}")
                self.proc = subprocess.Popen(
                    full_cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=merged_env,
                    text=True,
                    bufsize=1,
                    shell=sys.platform.startswith("win") and self.command in ("npx", "uvx", "npm")
                )
                self._reader_thread = threading.Thread(
                    target=self._read_stdout,
                    name=f"MCPReader-{self.name}",
                    daemon=True
                )
                self._reader_thread.start()

                # 1. MCP Initialize Handshake
                init_res = self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "Saturn", "version": "2.0.0"}
                    },
                    timeout=timeout
                )
                if not init_res:
                    self.error = "Handshake timeout"
                    return False

                # 2. Initialized Notification
                self._send_notification("notifications/initialized", {})

                # 3. Discover Tools
                tools_res = self._send_request("tools/list", {}, timeout=timeout)
                if tools_res and "tools" in tools_res.get("result", {}):
                    self.tools = tools_res["result"]["tools"]
                    self.is_connected = True
                    logger.info(f"[MCP:{self.name}] Online! Discovered {len(self.tools)} tools.")
                    return True
                else:
                    self.tools = []
                    self.is_connected = True
                    return True

            except Exception as e:
                self.error = str(e)
                logger.error(f"[MCP:{self.name}] Failed to start: {e}")
                self.stop()
                return False

    def _send_request(self, method: str, params: dict, timeout: int = 30) -> Optional[dict]:
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                return None
            self._req_id += 1
            req_id = self._req_id
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params
            }
            evt = threading.Event()
            self._pending_requests[req_id] = evt

            try:
                line = json.dumps(payload) + "\n"
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except Exception as e:
                logger.error(f"[MCP:{self.name}] Write error: {e}")
                self._pending_requests.pop(req_id, None)
                return None

        if evt.wait(timeout=timeout):
            with self._lock:
                self._pending_requests.pop(req_id, None)
                return self._responses.pop(req_id, None)
        else:
            with self._lock:
                self._pending_requests.pop(req_id, None)
            return None

    def _send_notification(self, method: str, params: dict) -> None:
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                return
            payload = {"jsonrpc": "2.0", "method": method, "params": params}
            try:
                line = json.dumps(payload) + "\n"
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
            except Exception:
                pass

    def call_tool(self, tool_name: str, arguments: dict, timeout: int = 60) -> Dict[str, Any]:
        """Invokes a specific tool on this MCP server."""
        res = self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments},
            timeout=timeout
        )
        if not res:
            return {"success": False, "error": f"Tool '{tool_name}' timed out on MCP server '{self.name}'."}

        if "error" in res:
            err = res["error"]
            return {"success": False, "error": err.get("message", str(err))}

        result_data = res.get("result", {})
        content_items = result_data.get("content", [])
        text_outputs = []
        for item in content_items:
            if item.get("type") == "text":
                text_outputs.append(item.get("text", ""))
            elif item.get("type") == "image":
                text_outputs.append(f"[Image: mime_type={item.get('mimeType')}]")

        output_str = "\n".join(text_outputs) if text_outputs else str(result_data)
        is_error = result_data.get("isError", False)
        return {
            "success": not is_error,
            "output": output_str,
            "raw": result_data
        }

    def _read_stdout(self):
        try:
            for line in iter(self.proc.stdout.readline, ""):
                if not line:
                    break
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                    req_id = data.get("id")
                    if req_id in self._pending_requests:
                        self._responses[req_id] = data
                        self._pending_requests[req_id].set()
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self.is_connected = False

    def stop(self):
        with self._lock:
            if self.proc:
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2)
                except Exception:
                    try:
                        self.proc.kill()
                    except Exception:
                        pass
                self.proc = None
            self.is_connected = False


class UniversalMCPManager:
    """
    Central hub managing all configured MCP servers.
    Provides automatic discovery, tool declaration synthesis, and request routing.
    """

    _instance: Optional[UniversalMCPManager] = None
    _lock = threading.Lock()

    def __init__(self):
        self._servers: Dict[str, MCPServerInstance] = {}
        self._ensure_default_config()

    @classmethod
    def get_instance(cls) -> UniversalMCPManager:
        with cls._lock:
            if cls._instance is None:
                cls._instance = UniversalMCPManager()
            return cls._instance

    def _ensure_default_config(self):
        """Creates sample mcp_servers.json if it doesn't already exist."""
        if not MCP_CONFIG_FILE.exists():
            default_config = {
                "mcpServers": {
                    "filesystem": {
                        "command": "npx",
                        "args": ["-y", "@modelcontextprotocol/server-filesystem", str(Path.home() / "Desktop")]
                    }
                }
            }
            try:
                MCP_CONFIG_FILE.write_text(json.dumps(default_config, indent=2), encoding="utf-8")
            except Exception:
                pass

    def load_servers(self) -> None:
        """Loads and initializes all servers defined in mcp_servers.json (both user dir and workspace)."""
        candidate_paths = [
            MCP_CONFIG_FILE,
            Path("config/mcp_servers.json").resolve(),
            Path.cwd() / "mcp_servers.json"
        ]

        merged_configs: Dict[str, Any] = {}
        for p in candidate_paths:
            if p.exists():
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    srvs = data.get("mcpServers", {})
                    merged_configs.update(srvs)
                    logger.info(f"[MCPManager] Loaded {len(srvs)} server definitions from {p}")
                except Exception as e:
                    logger.error(f"[MCPManager] Error reading config from {p}: {e}")

        for name, cfg in merged_configs.items():
            if cfg.get("disabled", False):
                continue
            if name not in self._servers:
                instance = MCPServerInstance(name, cfg)
                # Start in background thread to avoid blocking startup
                threading.Thread(
                    target=instance.start,
                    name=f"MCPInit-{name}",
                    daemon=True
                ).start()
                self._servers[name] = instance

    def get_all_tool_declarations(self) -> List[Dict[str, Any]]:
        """
        Converts all discovered MCP tools from all running servers into
        standard Gemini / OpenRouter function declarations.
        """
        declarations = []
        for server_name, server in self._servers.items():
            if not server.is_connected:
                continue
            for tool in server.tools:
                t_name = f"mcp_{server_name}_{tool.get('name')}"
                t_desc = f"[MCP:{server_name}] {tool.get('description', '')}"
                raw_schema = tool.get("inputSchema", {})
                
                # Normalize schema to Gemini format
                props = {}
                required = raw_schema.get("required", [])
                for prop_k, prop_v in raw_schema.get("properties", {}).items():
                    p_type = str(prop_v.get("type", "STRING")).upper()
                    p_def = {
                        "type": p_type,
                        "description": prop_v.get("description", "")
                    }
                    if p_type == "ARRAY":
                        raw_items = prop_v.get("items")
                        if isinstance(raw_items, dict):
                            p_def["items"] = {
                                "type": str(raw_items.get("type", "STRING")).upper()
                            }
                        else:
                            p_def["items"] = {"type": "STRING"}
                    props[prop_k] = p_def

                declarations.append({
                    "name": t_name,
                    "description": t_desc,
                    "parameters": {
                        "type": "OBJECT",
                        "properties": props,
                        "required": required
                    }
                })
        return declarations

    def execute_mcp_tool(self, full_tool_name: str, arguments: dict) -> str:
        """
        Routes a tool call 'mcp_<server>_<tool>' to the appropriate server.
        """
        if not full_tool_name.startswith("mcp_"):
            return f"Error: '{full_tool_name}' is not an MCP tool."

        remainder = full_tool_name[4:]
        parts = remainder.split("_", 1)
        if len(parts) < 2:
            return f"Error: Invalid MCP tool name format '{full_tool_name}'."

        server_name, tool_name = parts[0], parts[1]
        server = self._servers.get(server_name)
        if not server:
            return f"Error: MCP server '{server_name}' is not registered or running."

        if not server.is_connected:
            return f"Error: MCP server '{server_name}' is currently offline ({server.error or 'initializing'})."

        res = server.call_tool(tool_name, arguments)
        if res.get("success"):
            return res.get("output", "Success")
        else:
            return f"MCP Tool Error: {res.get('error', 'Unknown failure')}"

    def get_status_summary(self) -> str:
        """Returns diagnostic status of all connected MCP servers."""
        if not self._servers:
            return "No MCP servers configured in config/mcp_servers.json."

        lines = ["[MCP SERVERS STATUS]"]
        for name, s in self._servers.items():
            status = "ONLINE" if s.is_connected else ("ERROR" if s.error else "CONNECTING")
            lines.append(f"• {name}: {status} ({len(s.tools)} tools discovered)")
        return "\n".join(lines)
