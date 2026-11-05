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
from core.audit_log import AuditLog

logger = logging.getLogger("MCPManager")
logger.setLevel(logging.INFO)

CONFIG_DIR = get_user_data_dir() / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
MCP_CONFIG_FILE = CONFIG_DIR / "mcp_servers.json"

# The minimal, secret-free environment every MCP child process gets.
# A blanket os.environ.copy() used to hand every server - including code
# Saturn forged - Saturn's whole environment: API keys, tokens, everything.
# Children now receive only these ordinary system/toolchain variables (with
# real values taken from the local machine so PATH keeps working) plus
# whatever the server's config explicitly declares in its own "env" block.
# On Windows os.environ lookup is case-insensitive, so the canonical upper
# spellings resolve "Path", "SystemRoot", etc.; on POSIX the Windows-only
# names simply never match and drop out.
_SAFE_INHERIT_ENV: Tuple[str, ...] = (
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME",
    "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PYTHONHOME", "PYTHONPATH", "NODE_PATH", "LANG", "LC_ALL", "TZ",
)

# A server that will not boot must not be re-spawned on every routed call.
# One launch attempt per this window.
_START_RETRY_SECONDS = 30.0


def _minimal_base_env() -> Dict[str, str]:
    """Whitelisted slice of the process environment safe for child servers."""
    base: Dict[str, str] = {}
    for key in _SAFE_INHERIT_ENV:
        try:
            if key in os.environ:
                base[key] = os.environ[key]
        except Exception:
            continue
    if not any(k.lower() == "path" for k in base):
        # Worst case at least let the interpreter's own directory work.
        base["PATH"] = str(Path(sys.executable or "python").parent)
    return base


def forged_tools_enabled() -> bool:
    """The global kill switch for everything Saturn forged.

    Backed by ``forged_tools_enabled`` in the app settings and on by default.
    When off, forged MCP servers stay visible but cannot be called, and
    forged features refuse at dispatch. Hand-configured servers are untouched.
    """
    try:
        from memory.config_manager import get_setting
        return bool(get_setting("forged_tools_enabled", True))
    except Exception:
        return True


def forged_max_running() -> int:
    """How many forged servers Saturn will keep spawned at once.

    Forged servers start lazily, on the call that needs one, and each is a
    live Python process. This cap is the honest version of the design's "Max
    running" select: past it, waking another lazy server is refused with a
    clear reason rather than quietly ballooning memory. Backed by
    ``forged_max_running`` in the app settings; 4 by default."""
    try:
        from memory.config_manager import get_setting
        return max(1, int(get_setting("forged_max_running", 4)))
    except Exception:
        return 4


class MCPServerInstance:
    """Manages an active stdio connection to a single MCP server.

    Saturn is a full MCP *host*, not just a caller. This class handles both
    directions of the JSON-RPC conversation:

    - Client -> server: `initialize`, `tools/list`, `tools/call`, notifications.
    - Server -> client: `sampling/createMessage` (server asks Saturn's LLM for
      a completion) and `elicitation/create` (server asks Saturn to prompt the
      user through the HUD). Both are answered by the registered handlers on
      the `UniversalMCPManager` singleton, wired at boot by main.py.
    """

    # Server-initiated JSON-RPC methods Saturn answers as a host.
    _SERVER_REQUEST_METHODS = {
        "sampling/createMessage",
        "elicitation/create",
    }

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
        # Saturn-forged servers are lazy by default: registering one must not
        # cost RAM until it is actually called. The forge marks its configs
        # with source "saturn_mcp_forge"; "lazy" overrides per server.
        self.is_forged = str(config.get("source", "")) == "saturn_mcp_forge"
        self.lazy = bool(config.get("lazy", self.is_forged))
        self._last_start_attempt = 0.0
        self._manifest_tools: Optional[List[Dict[str, Any]]] = None
        # Every resource / prompt the server exposes beyond plain tools.
        # Populated after the initialize handshake so the ToolRouter and the
        # planner can reason over the full server surface, not just tools/list.
        self.resources: List[Dict[str, Any]] = []
        self.prompts: List[Dict[str, Any]] = []
        # Server-declared capabilities come back in the initialize result.
        # We remember them so sampling/elicitation calls only go to servers
        # that actually implement them.
        self.server_capabilities: Dict[str, Any] = {}

    def load_manifest_tools(self) -> List[Dict[str, Any]]:
        """Tool list read from the forge manifest on disk.

        A lazy server is not running until it is called, but the planner
        still has to see its verbs. The forge already wrote each tool's
        name/description/inputSchema into manifest.json next to
        server.py, and that block has the same shape as a live
        ``tools/list`` entry, so declarations can be built without
        spawning the process. Cached; empty list means nothing to show.
        """
        if self._manifest_tools is not None:
            return self._manifest_tools
        self._manifest_tools = []
        try:
            if self.args:
                mp = Path(str(self.args[0])).parent / "manifest.json"
                if mp.exists():
                    data = json.loads(mp.read_text(encoding="utf-8"))
                    tools = data.get("tools")
                    if isinstance(tools, list):
                        self._manifest_tools = [
                            t for t in tools
                            if isinstance(t, dict) and t.get("name")
                        ]
        except Exception as exc:
            logger.debug(f"[MCP:{self.name}] manifest tools read failed: {exc}")
        return self._manifest_tools

    def start(self, timeout: int = 20) -> bool:
        """Launches the server subprocess, completes JSON-RPC handshake, and fetches tools."""
        with self._lock:
            if self.is_connected:
                return True

            cmd_resolved = shutil.which(self.command) or self.command
            full_cmd = [cmd_resolved] + [str(a) for a in self.args]
            self._last_start_attempt = time.time()

            # Secret-free base environment + this server's declared "env".
            # Never os.environ.copy(): children must not inherit Saturn's
            # API keys just because they exist.
            merged_env = _minimal_base_env()
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

                # 1. MCP Initialize Handshake. Saturn declares itself as a
                # full host: sampling (server can ask our LLM) and
                # elicitation (server can ask the user through the HUD).
                init_res = self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "sampling": {},
                            "elicitation": {},
                            "roots": {"listChanged": True},
                        },
                        "clientInfo": {"name": "Saturn", "version": "2.0.0"}
                    },
                    timeout=timeout
                )
                if not init_res:
                    self.error = "Handshake timeout"
                    return False

                # Remember what the server itself supports so we do not send
                # requests it will reject.
                try:
                    self.server_capabilities = (
                        init_res.get("result", {}).get("capabilities", {}) or {}
                    )
                except Exception:
                    self.server_capabilities = {}

                # 2. Initialized Notification
                self._send_notification("notifications/initialized", {})

                # 3. Discover Tools
                tools_res = self._send_request("tools/list", {}, timeout=timeout)
                if tools_res and "tools" in tools_res.get("result", {}):
                    self.tools = tools_res["result"]["tools"]
                else:
                    self.tools = []

                # 4. Discover Resources (optional - some servers do not implement it)
                if "resources" in self.server_capabilities:
                    try:
                        res_res = self._send_request("resources/list", {}, timeout=timeout)
                        if res_res and "resources" in res_res.get("result", {}):
                            self.resources = res_res["result"]["resources"]
                    except Exception:
                        self.resources = []

                # 5. Discover Prompts (optional)
                if "prompts" in self.server_capabilities:
                    try:
                        pr_res = self._send_request("prompts/list", {}, timeout=timeout)
                        if pr_res and "prompts" in pr_res.get("result", {}):
                            self.prompts = pr_res["result"]["prompts"]
                    except Exception:
                        self.prompts = []

                self.is_connected = True
                logger.info(
                    f"[MCP:{self.name}] Online! Discovered {len(self.tools)} tools, "
                    f"{len(self.resources)} resources, {len(self.prompts)} prompts."
                )
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

    def read_resource(self, uri: str, timeout: int = 30) -> Dict[str, Any]:
        """Fetch one resource payload. Servers that do not implement
        resources/list will simply return an error and Saturn routes around
        it."""
        res = self._send_request("resources/read", {"uri": uri}, timeout=timeout)
        if not res:
            return {"success": False, "error": f"resources/read timed out on '{self.name}'."}
        if "error" in res:
            err = res["error"]
            return {"success": False, "error": err.get("message", str(err))}
        contents = res.get("result", {}).get("contents", []) or []
        text_chunks: List[str] = []
        for c in contents:
            if c.get("text"):
                text_chunks.append(c["text"])
            elif c.get("blob"):
                text_chunks.append(f"[Binary {c.get('mimeType','?')}: {len(c['blob'])}B]")
        return {"success": True, "output": "\n".join(text_chunks) or str(contents)}

    def get_prompt(self, name: str, arguments: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
        """Fetch one pre-authored prompt template from the server."""
        res = self._send_request(
            "prompts/get",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )
        if not res:
            return {"success": False, "error": f"prompts/get timed out on '{self.name}'."}
        if "error" in res:
            err = res["error"]
            return {"success": False, "error": err.get("message", str(err))}
        msgs = res.get("result", {}).get("messages", []) or []
        rendered = []
        for m in msgs:
            role = m.get("role", "assistant")
            content = m.get("content") or {}
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            rendered.append(f"[{role}] {text}")
        return {"success": True, "output": "\n\n".join(rendered) or str(msgs)}

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
                except Exception:
                    continue
                # Distinguish response-vs-request-vs-notification.
                # A response has an `id` that matches one of our pending
                # requests. A server-initiated request has an `id` that is
                # NOT in our pending set AND carries a `method` field. A
                # notification has a `method` but no `id`.
                try:
                    req_id = data.get("id")
                    method = data.get("method")
                    if req_id is not None and method is None:
                        # This is a response to one of our requests.
                        with self._lock:
                            if req_id in self._pending_requests:
                                self._responses[req_id] = data
                                self._pending_requests[req_id].set()
                    elif req_id is not None and method in self._SERVER_REQUEST_METHODS:
                        # Server -> client request. Handle in a worker thread
                        # so a slow sampling / elicitation never blocks the
                        # reader loop (which would freeze response delivery
                        # for other in-flight tool calls on this server).
                        threading.Thread(
                            target=self._dispatch_server_request,
                            args=(req_id, method, data.get("params") or {}),
                            name=f"MCPServerReq-{self.name}-{method}",
                            daemon=True,
                        ).start()
                    elif req_id is not None and method is not None:
                        # Unknown server-initiated request. Reply with a
                        # JSON-RPC method-not-found error so the server does
                        # not hang waiting on us.
                        self._send_error(req_id, -32601, f"Saturn does not handle '{method}'.")
                    # else: bare notification - nothing to reply to.
                except Exception as exc:
                    logger.debug(f"[MCP:{self.name}] reader parse error: {exc}")
        except Exception:
            pass
        finally:
            self.is_connected = False

    # ── Server -> client request handling (Saturn as MCP host) ────────────

    def _dispatch_server_request(self, req_id, method: str, params: dict) -> None:
        """Answer a server-initiated JSON-RPC request through the manager's
        registered handlers. Runs on its own thread, launched from the
        stdout reader loop."""
        try:
            mgr = UniversalMCPManager.get_instance()
            handler = mgr.get_server_request_handler(method)
            if handler is None:
                self._send_error(req_id, -32601,
                                 f"Saturn host has no handler for '{method}'.")
                return
            result = handler(server_name=self.name, params=params)
            self._send_result(req_id, result if isinstance(result, dict) else {"value": result})
        except Exception as exc:
            logger.error(f"[MCP:{self.name}] {method} handler crashed: {exc}")
            try:
                self._send_error(req_id, -32603, f"Saturn handler error: {exc}")
            except Exception:
                pass

    def _send_result(self, req_id, result: dict) -> None:
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                return
            payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except Exception as exc:
                logger.error(f"[MCP:{self.name}] Failed to send result for {req_id}: {exc}")

    def _send_error(self, req_id: int, code: int, message: str) -> None:
        with self._lock:
            if not self.proc or self.proc.poll() is not None:
                return
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
            try:
                self.proc.stdin.write(json.dumps(payload) + "\n")
                self.proc.stdin.flush()
            except Exception:
                pass

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

    As a *host*, Saturn also answers server-initiated requests (sampling and
    elicitation). Concrete handlers are registered by main.py at boot via
    `register_server_request_handler()`; the manager itself knows nothing
    about Gemini or the HUD.
    """

    _instance: Optional[UniversalMCPManager] = None
    _lock = threading.Lock()

    def __init__(self):
        self._servers: Dict[str, MCPServerInstance] = {}
        # method -> callable(server_name: str, params: dict) -> dict
        self._server_request_handlers: Dict[str, Callable[..., Dict[str, Any]]] = {}
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

        allow_forged = forged_tools_enabled()
        for name, cfg in merged_configs.items():
            if cfg.get("disabled", False):
                continue
            if name not in self._servers:
                instance = MCPServerInstance(name, cfg)
                self._servers[name] = instance
                if instance.is_forged and not allow_forged:
                    # Kill switch off the ground: stay registered so the
                    # user can still see the capability, but never spawn
                    # and refuse every call at the routing seam.
                    logger.info(f"[MCPManager] '{name}' forged but forged tools are disabled")
                    continue
                if instance.lazy:
                    # No RAM until first call; the manifest keeps the tools
                    # visible to the planner in the meantime.
                    logger.info(f"[MCPManager] '{name}' registered lazily (not spawned yet)")
                    continue
                # Start in background thread to avoid blocking startup
                threading.Thread(
                    target=instance.start,
                    name=f"MCPInit-{name}",
                    daemon=True
                ).start()

    def get_all_tool_declarations(self) -> List[Dict[str, Any]]:
        """
        Converts all discovered MCP tools from all running servers into
        standard Gemini / OpenRouter function declarations.

        Beyond tools, each server's resources and prompts also become callable
        verbs (`mcp_<server>_read_resource`, `mcp_<server>_get_prompt`), so a
        forged server that ships them is actually *used* by Saturn instead of
        silently ignored.
        """
        declarations = []
        for server_name, server in self._servers.items():
            if server.is_connected:
                tools = server.tools
            elif getattr(server, "lazy", False) and forged_tools_enabled():
                # Lazy forged server that has not been woken yet: declare
                # straight from its manifest so the planner still sees the
                # capability; the first real call spawns the process.
                tools = server.load_manifest_tools()
            else:
                continue
            for tool in tools:
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

            # Resource-read verb - one per server, takes a uri.
            if server.resources:
                uri_values = ", ".join(r.get("uri", "") for r in server.resources[:5])
                declarations.append({
                    "name": f"mcp_{server_name}_read_resource",
                    "description": (
                        f"[MCP:{server_name}] Read one of this server's live "
                        f"data resources. Available URIs: {uri_values}"
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "uri": {"type": "STRING", "description": "The resource uri"},
                        },
                        "required": ["uri"],
                    },
                })

            # Prompt-get verb - one per server, takes a name + args.
            if server.prompts:
                names = ", ".join(p.get("name", "") for p in server.prompts[:5])
                declarations.append({
                    "name": f"mcp_{server_name}_get_prompt",
                    "description": (
                        f"[MCP:{server_name}] Fetch one of this server's "
                        f"pre-authored prompt templates. Names: {names}"
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "name": {"type": "STRING", "description": "Prompt name"},
                            "arguments": {"type": "OBJECT", "description": "Template args"},
                        },
                        "required": ["name"],
                    },
                })
        return declarations

    def execute_mcp_tool(self, full_tool_name: str, arguments: dict) -> str:
        """
        Routes a tool call 'mcp_<server>_<tool>' to the appropriate server.

        Every call is recorded by the CapabilityHealth monitor so Saturn can
        notice a forged server drifting and offer to reforge it, and lands
        in the persistent audit log with its (secret-redacted) arguments,
        outcome and duration. Both records are best-effort: a broken
        monitor never breaks a real tool call.

        Gates checked before routing, in order: the global forged-tools
        kill switch, the per-server ``disabled`` flag, the per-tool
        permission level (Block refuses, Ask each time parks the call
        behind the on-screen confirmation banner), then connectivity -
        a lazy server that has never run is spawned right here, on the
        call that first needs it.
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
            self._record_health(full_tool_name, False, 0.0,
                                f"server '{server_name}' not registered")
            AuditLog.record(full_tool_name, False, 0.0, arguments,
                            f"server '{server_name}' not registered")
            return f"Error: MCP server '{server_name}' is not registered or running."

        if getattr(server, "is_forged", False) and not forged_tools_enabled():
            self._record_health(full_tool_name, False, 0.0, "forged tools disabled")
            AuditLog.record(full_tool_name, False, 0.0, arguments,
                            "forged tools disabled by user")
            return (f"Error: forged tools are currently disabled in Saturn's "
                    f"settings, so '{server_name}' cannot run.")

        if (getattr(server, "config", {}) or {}).get("disabled", False):
            self._record_health(full_tool_name, False, 0.0, "server disabled")
            AuditLog.record(full_tool_name, False, 0.0, arguments,
                            f"server '{server_name}' is disabled")
            return f"Error: MCP server '{server_name}' is disabled."

        if not server.is_connected and getattr(server, "proc", None) is None \
                and time.time() - getattr(server, "_last_start_attempt", 0.0) >= _START_RETRY_SECONDS:
            # Lazy first use (or a retry after the backoff window): this
            # call is what wakes the server. Past the concurrency cap we
            # refuse rather than spawn another process the user did not ask
            # to keep resident.
            if getattr(server, "is_forged", False) and self._running_forged_count() >= forged_max_running():
                why = f"max running forged servers ({forged_max_running()}) reached"
                self._record_health(full_tool_name, False, 0.0, why)
                AuditLog.record(full_tool_name, False, 0.0, arguments, why)
                return (f"Error: '{server_name}' is asleep to save memory and "
                        f"{self._running_forged_count()} forged servers are already "
                        f"running (the limit). Close one on the Creators page or "
                        f"raise the Max-running limit, then try again.")
            logger.info(f"[MCP:{server_name}] Lazy start on first call")
            server.start(timeout=25)

        if not server.is_connected:
            self._record_health(full_tool_name, False, 0.0,
                                server.error or "server offline")
            AuditLog.record(full_tool_name, False, 0.0, arguments,
                            server.error or "server offline")
            return f"Error: MCP server '{server_name}' is currently offline ({server.error or 'initializing'})."

        gate = self._permission_gate(server, server_name, tool_name,
                                     full_tool_name, arguments)
        if gate is not None:
            return gate

        t0 = time.perf_counter()
        res = self._dispatch(server, tool_name, arguments)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        ok = bool(res.get("success"))
        err_msg = None if ok else str(res.get("error", "Unknown failure"))
        self._record_health(full_tool_name, ok, latency_ms, err_msg)
        AuditLog.record(full_tool_name, ok, latency_ms, arguments, err_msg)
        if ok:
            return res.get("output", "Success")
        return f"MCP Tool Error: {err_msg}"

    @staticmethod
    def _dispatch(server: "MCPServerInstance", tool_name: str, arguments: dict) -> Dict[str, Any]:
        """The real call, shared by the direct path and the confirmed path.

        Meta verbs for resources and prompts. These are NOT real tools on
        the server - they are Saturn synthesising a callable surface around
        the spec's resources/read and prompts/get methods so the LLM can
        actually use them. Safe to run on any thread: the JSON-RPC client
        serialises writes itself, and the confirmation worker is a plain
        thread outside the Qt and executor loops.
        """
        if tool_name == "read_resource" and server.resources:
            return server.read_resource(arguments.get("uri", ""))
        if tool_name == "get_prompt" and server.prompts:
            return server.get_prompt(arguments.get("name", ""),
                                     arguments.get("arguments") or {})
        return server.call_tool(tool_name, arguments)

    def _tool_declaration(self, server: "MCPServerInstance", tool_name: str) -> Dict[str, Any]:
        """The manifest/live entry for one verb, so its declared ``requires``
        reaches the permission classifier. Falls back to the bare name for
        duck-typed test fakes and servers that never listed tools."""
        entries = (getattr(server, "tools", None)
                   or (server.load_manifest_tools()
                       if hasattr(server, "load_manifest_tools") else [])
                   or [])
        for t in entries:
            if isinstance(t, dict) and t.get("name") == tool_name:
                return t
        return {"name": tool_name}

    def _permission_gate(self, server, server_name: str, tool_name: str,
                         full_tool_name: str, arguments: dict) -> Optional[str]:
        """Apply the tool's permission level; None means the call may proceed.

        Only forged servers are gated: a hand-configured server was chosen by
        the user with full knowledge of what it is, and retro-nagging those
        would train the user to click through real warnings. Block refuses
        outright. Ask parks the call behind core/confirm - the model cannot
        forge that banner any more than it can forge a shutdown confirmation.
        """
        if not getattr(server, "is_forged", False):
            return None
        try:
            from core import permissions
            decl = self._tool_declaration(server, tool_name)
            level = permissions.resolve(
                server_name, tool_name,
                requires=decl.get("requires") or [],
                tool_name=tool_name,
                description=str(decl.get("description", "")))
        except Exception as exc:  # a broken gate must never brick the toolbelt
            logger.debug(f"[MCP] permission gate skipped: {exc}")
            return None

        if level == permissions.BLOCK:
            why = f"Blocked: tool '{tool_name}' is set to Block"
            self._record_health(full_tool_name, False, 0.0, why)
            AuditLog.record(full_tool_name, False, 0.0, arguments, why)
            return (f"Blocked: the user set '{tool_name}' to Block on Saturn's "
                    f"Creators page, so this call was refused. Do not retry it; "
                    f"tell the user it is blocked and that they can change the "
                    f"permission on the Creators page.")

        if level != permissions.ASK:
            return None

        from core import confirm
        if not confirm.is_bound():
            # Headless (tests, CLI, early boot): nobody can see a banner, so
            # asking would only refuse. Allow the call and keep the audit
            # trail; the gate reappears the moment the HUD is wired.
            logger.debug(f"[MCP:{server_name}] ask-gate skipped (no UI): {tool_name}")
            return None
        if confirm.pending_title():
            return ("[CONFIRMATION_PENDING] Another confirmation is already on "
                    f"screen, so '{tool_name}' has not been called yet. Ask the "
                    "user to answer that confirmation first.")

        def _run_confirmed() -> str:
            t0 = time.perf_counter()
            res = self._dispatch(server, tool_name, arguments)
            latency_ms = (time.perf_counter() - t0) * 1000.0
            ok = bool(res.get("success"))
            err_msg = None if ok else str(res.get("error", "Unknown failure"))
            self._record_health(full_tool_name, ok, latency_ms, err_msg)
            AuditLog.record(full_tool_name, ok, latency_ms, arguments, err_msg)
            if ok:
                return str(res.get("output", "Success"))[:600]
            return f"MCP Tool Error: {err_msg}"

        key = f"mcp_{server_name}_{tool_name}"
        needs = ", ".join(decl.get("requires") or []) or "see the Creators page"
        detail = (f"Saturn built this tool itself and you set it to ask first. "
                  f"Declared needs: {needs}.")
        return confirm.request(key, f"Run {tool_name}?",
                               f"Server '{server_name}' wants to call "
                               f"'{tool_name}'. {detail}", _run_confirmed)

    @staticmethod
    def _record_health(tool_name: str, ok: bool, latency_ms: float, error: Optional[str]) -> None:
        """Best-effort hand-off to the capability-health monitor."""
        try:
            from core.capability_health import CapabilityHealth
            CapabilityHealth.record(tool_name, ok, latency_ms, error)
        except Exception:
            pass

    def _running_forged_count(self) -> int:
        """How many forged servers are connected (resident) right now."""
        n = 0
        for srv in self._servers.values():
            try:
                if getattr(srv, "is_forged", False) and getattr(srv, "is_connected", False):
                    n += 1
            except Exception:
                continue
        return n

    # ── Server -> client request handlers (Saturn acting as MCP host) ────

    def register_server_request_handler(
        self,
        method: str,
        handler: Callable[..., Dict[str, Any]],
    ) -> None:
        """Bind a callable to a server-initiated JSON-RPC method.

        Contract of `handler`:
            handler(server_name: str, params: dict) -> dict

        The returned dict is written back as the JSON-RPC `result`. Raising
        any exception is caught in the reader dispatch and returned to the
        server as a JSON-RPC error, so handlers do not need to guard
        themselves against every failure mode - but they should return
        quickly. Long-running sampling goes through the LLM client, which
        is expected to have its own timeouts.
        """
        self._server_request_handlers[method] = handler

    def get_server_request_handler(self, method: str):
        return self._server_request_handlers.get(method)

    def get_status_summary(self) -> str:
        """Returns diagnostic status of all connected MCP servers."""
        if not self._servers:
            return "No MCP servers configured in config/mcp_servers.json."
    
        lines = ["[MCP SERVERS STATUS]"]
        for name, s in self._servers.items():
            status = "ONLINE" if s.is_connected else ("ERROR" if s.error else "CONNECTING")
            lines.append(f"\u2022 {name}: {status} ({len(s.tools)} tools discovered)")
        return "\n".join(lines)
    
    # ── Hot registration (used by Saturn's portable SkillForge tier) ──────
    
    def hot_register(
        self,
        name: str,
        config: Dict[str, Any],
        persist: bool = True,
        timeout: int = 25,
    ) -> Tuple[bool, str]:
        """Add and boot a new MCP server without restarting Saturn.
    
        Persists the definition to the user's `mcp_servers.json` first, then
        spawns the subprocess and completes the handshake. If the boot fails
        we roll the file change back so a broken skill cannot poison the
        next start. Saturn's live loop and the planner pick the new tools up
        automatically - they read from `get_all_tool_declarations()` per turn.
        """
        if not name or not isinstance(config, dict):
            return False, "hot_register needs (name, config dict)."
    
        with self._lock:
            existing = self._servers.get(name)
            if existing and existing.is_connected:
                return False, f"MCP server '{name}' is already registered and online."
    
        persisted: List[Path] = []
        if persist:
            persisted = self._append_server_to_user_config(name, config)
    
        instance = MCPServerInstance(name, config)
        ok = False
        try:
            ok = instance.start(timeout=timeout)
        except Exception as exc:
            instance.error = str(exc)
    
        if not ok:
            # Roll back the file change so next boot is clean.
            for p in persisted:
                self._remove_server_from_config(p, name)
            try:
                instance.stop()
            except Exception:
                pass
            return False, instance.error or "Server failed to boot."
    
        with self._lock:
            old = self._servers.get(name)
            if old is not None and old is not instance:
                try:
                    old.stop()
                except Exception:
                    pass
            self._servers[name] = instance
    
        return True, f"Registered '{name}' with {len(instance.tools)} tool(s)."
    
    def hot_unregister(self, name: str, remove_from_config: bool = True) -> Tuple[bool, str]:
        """Tear down a live server and (optionally) drop it from disk."""
        with self._lock:
            instance = self._servers.pop(name, None)
        stopped = False
        if instance is not None:
            try:
                instance.stop()
                stopped = True
            except Exception as exc:
                return False, f"Stop raised: {exc}"
        if remove_from_config:
            self._remove_server_from_config(MCP_CONFIG_FILE, name)
        if instance is None and not stopped:
            return True, f"'{name}' was not live; ensured absent from config."
        return True, f"Unregistered '{name}'."
    
    @staticmethod
    def _append_server_to_user_config(name: str, cfg: Dict[str, Any]) -> List[Path]:
        """Write the server entry into the user's mcp_servers.json.
    
        We deliberately do NOT touch the workspace `config/mcp_servers.json`:
        that file is the shipped template and dirtying it would show up in
        every contributor's git status. Forged tools belong to the person
        who forged them.
        """
        try:
            MCP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if MCP_CONFIG_FILE.exists():
                try:
                    data = json.loads(MCP_CONFIG_FILE.read_text(encoding="utf-8"))
                except Exception:
                    data = {"mcpServers": {}}
            else:
                data = {"mcpServers": {}}
            servers = data.setdefault("mcpServers", {})
            servers[name] = cfg
            MCP_CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return [MCP_CONFIG_FILE]
        except Exception as exc:
            logger.warning(f"[MCPManager] Could not persist '{name}': {exc}")
            return []
    
    @staticmethod
    def _remove_server_from_config(path: Path, name: str) -> None:
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            servers = data.get("mcpServers") or {}
            if name in servers:
                del servers[name]
                path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            logger.warning(f"[MCPManager] Could not remove '{name}' from {path}: {exc}")
