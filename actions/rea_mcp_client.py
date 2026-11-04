"""
actions/rea_mcp_client.py
Reverse Engineer Anything (REA) MCP Client for Saturn.

Manages connection to the morluto/rea MCP server over stdio JSON-RPC 2.0.
Supports:
- Electron / JavaScript app inspection and AST de-bundling
- Native binary PE/ELF/Mach-O layout and function decompilation
- Web HAR / network API surface extraction
- WebAssembly (.wasm) inspection
- Security isolation & prompt-injection defense for target content
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("REAPClient")
logger.setLevel(logging.INFO)

# Default response budget: 10 MiB as defined by REA contracts
DEFAULT_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Supply-chain control. ``rea-agents@latest`` tracks upstream releases and is a
# convenience default; pin a known-good build by setting
# SATURN_REA_PACKAGE_SPEC="rea-agents@<version>" (or passing package_spec).
DEFAULT_REA_PACKAGE_SPEC = os.environ.get("SATURN_REA_PACKAGE_SPEC", "rea-agents@latest")


class REAMCPClient:
    """
    Python client for morluto/rea (Reverse Engineer Anything) MCP server.
    Spawns and orchestrates `npx -y rea-agents@latest` over stdio JSON-RPC 2.0.
    """

    def __init__(
        self,
        package_spec: Optional[str] = None,
        timeout: int = 60,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        custom_env: Optional[Dict[str, str]] = None,
    ):
        self.package_spec = package_spec or DEFAULT_REA_PACKAGE_SPEC
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.custom_env = custom_env or {}

        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.RLock()
        self._stdin_lock = threading.Lock()
        self._req_id = 0
        self._pending_requests: Dict[int, threading.Event] = {}
        self._responses: Dict[int, dict] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._is_ready = threading.Event()
        self._tools: List[dict] = []
        self._running = False
        self._session_id: Optional[str] = None
        # Filled from the initialize result so callers can audit exactly which
        # REA build answered and what it expects from the client.
        self.server_info: Dict[str, Any] = {}
        self.server_capabilities: Dict[str, Any] = {}
        # Roots REA may enumerate (roots/list). Defaults to the working dir; the
        # RE server uses them to scope what it is allowed to touch.
        self._roots: List[dict] = [{"name": "workspace", "uri": Path.cwd().as_uri()}]

    def set_roots(self, roots: List[dict]) -> None:
        """Override the ``file://`` roots advertised to REA via ``roots/list``."""
        self._roots = list(roots or [])

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        """Spawns the REA MCP server subprocess and completes the MCP handshake."""
        with self._lock:
            if self.is_alive():
                return True

            npx_cmd = "npx.cmd" if platform.system() == "Windows" else "npx"
            resolved_npx = shutil.which(npx_cmd) or shutil.which("npx") or npx_cmd

            cmd = [resolved_npx, "-y", self.package_spec]

            # Merge environment variables
            env = dict(os.environ)
            env["REA_MCP_MAX_RESPONSE_BYTES"] = str(self.max_response_bytes)
            env["NO_COLOR"] = "1"
            env.update(self.custom_env)

            logger.info(f"[REAPClient] Spawning REA MCP server: {' '.join(cmd)}")

            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    shell=(platform.system() == "Windows"),
                    bufsize=0,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
            except Exception as e:
                logger.error(f"[REAPClient] Failed to spawn {self.package_spec}: {e}")
                return False

            self._running = True
            self._is_ready.clear()
            self._reader_thread = threading.Thread(
                target=self._read_loop,
                daemon=True,
                name="REAPClient-Reader"
            )
            self._reader_thread.start()

            # Perform MCP Handshake
            try:
                init_resp = self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {
                            "roots": {"listChanged": True},
                            "sampling": {},
                        },
                        "clientInfo": {
                            "name": "saturn-ai",
                            "version": "2.0.0",
                        },
                    },
                    timeout=30,
                )
                if not init_resp:
                    logger.error("[REAPClient] Handshake timed out or failed to initialize")
                    return False

                # Record who we are talking to so the build can be audited.
                result = init_resp.get("result", {}) or {}
                self.server_capabilities = result.get("capabilities", {}) or {}
                self.server_info = result.get("serverInfo", {}) or {}
                logger.info(
                    "[REAPClient] Connected to REA '%s' v%s (capabilities: %s)",
                    self.server_info.get("name", "rea"),
                    self.server_info.get("version", "?"),
                    ",".join(sorted(self.server_capabilities.keys())) or "none",
                )
                if "@" not in self.package_spec or self.package_spec.endswith("@latest"):
                    logger.warning(
                        "[REAPClient] REA package '%s' is not version-pinned. Set "
                        "SATURN_REA_PACKAGE_SPEC=rea-agents@<version> for a "
                        "supply-chain stable install.",
                        self.package_spec,
                    )

                # Send initialized notification
                self._send_notification("notifications/initialized", {})

                # Discover available tools
                tools_resp = self._send_request("tools/list", {}, timeout=20)
                if tools_resp and "result" in tools_resp:
                    self._tools = tools_resp["result"].get("tools", [])
                    logger.info(f"[REAPClient] REA initialized with {len(self._tools)} tools")

                self._is_ready.set()
                return True
            except Exception as exc:
                logger.error(f"[REAPClient] Handshake error: {exc}")
                self.close()
                return False

    def _read_loop(self):
        """Reads JSON-RPC lines from stdout."""
        while self._running and self._proc and self._proc.stdout:
            try:
                line = self._proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if "method" in data and "id" in data:
                    # A server-initiated request (roots/list, sampling, ping...).
                    # Its id-space is independent from ours, so dispatch it on a
                    # worker thread: replying inline could block the reader, and
                    # letting it fall through would collide with a client reply
                    # that happens to share the same numeric id.
                    threading.Thread(
                        target=self._handle_server_request,
                        args=(data,),
                        daemon=True,
                        name="REAPClient-ServerReq",
                    ).start()
                elif "method" in data:
                    # Server notification (progress, message, log). Nothing to do.
                    continue
                else:
                    # A reply to one of our outstanding requests.
                    req_id = data.get("id")
                    if req_id is not None and req_id in self._pending_requests:
                        self._responses[req_id] = data
                        self._pending_requests[req_id].set()

            except Exception as e:
                if self._running:
                    logger.debug(f"[REAPClient] Read error: {e}")
                break

        self._running = False
        self._is_ready.clear()

    def _stdin_write(self, text: str) -> bool:
        """Serialized single-line write to the server's stdin (one JSON-RPC frame)."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return False
        try:
            with self._stdin_lock:
                proc.stdin.write(text + "\n")
                proc.stdin.flush()
            return True
        except Exception as e:
            logger.error(f"[REAPClient] stdin write error: {e}")
            return False

    def _send_notification(self, method: str, params: Optional[dict] = None) -> None:
        if not self.is_alive() or not self._proc or not self._proc.stdin:
            return
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
        }
        self._stdin_write(json.dumps(payload))

    def _send_request(
        self,
        method: str,
        params: Optional[dict] = None,
        timeout: Optional[int] = None,
    ) -> Optional[dict]:
        with self._lock:
            if not self.is_alive() or not self._proc or not self._proc.stdin:
                return None

            self._req_id += 1
            req_id = self._req_id
            event = threading.Event()
            self._pending_requests[req_id] = event

            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params or {},
            }

            try:
                if not self._stdin_write(json.dumps(payload)):
                    raise IOError("stdin unavailable")
            except Exception as e:
                logger.error(f"[REAPClient] Send error: {e}")
                self._pending_requests.pop(req_id, None)
                return None

        # Wait for reply
        wait_time = timeout or self.timeout
        if event.wait(timeout=wait_time):
            with self._lock:
                self._pending_requests.pop(req_id, None)
                return self._responses.pop(req_id, None)
        else:
            with self._lock:
                self._pending_requests.pop(req_id, None)
                logger.warning(f"[REAPClient] Request {method} (id={req_id}) timed out after {wait_time}s")
                return None

    def _send_response(self, req_id: Any, result: dict) -> None:
        self._stdin_write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}))

    def _send_error(self, req_id: Any, code: int, message: str) -> None:
        self._stdin_write(json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": code, "message": message},
        }))

    def _handle_server_request(self, data: dict) -> None:
        """Answer JSON-RPC requests REA sends to us, instead of ignoring them.

        Silently dropping a server request makes REA block until our client-side
        timeout fires; a well-formed reply (or a method-not-found error) lets it
        continue immediately. Runs on a worker thread off the reader loop.
        """
        method = data.get("method")
        req_id = data.get("id")
        params = data.get("params", {}) or {}
        try:
            if method == "ping":
                self._send_response(req_id, {})
            elif method == "roots/list":
                self._send_response(req_id, {"roots": list(self._roots)})
            elif method == "sampling/createMessage":
                self._fulfill_sampling(req_id, params)
            else:
                self._send_error(req_id, -32601, f"Saturn client does not implement '{method}'")
        except Exception as e:
            logger.warning(f"[REAPClient] Failed handling server request '{method}': {e}")
            self._send_error(req_id, -32603, str(e))

    def _fulfill_sampling(self, req_id: Any, params: dict) -> None:
        """Bridge REA's sampling request to Saturn's own model stack.

        Fail-soft: if no model is reachable we reply with an error so REA falls
        back to its non-sampling path rather than waiting on us forever.
        """
        messages = params.get("messages", []) or []
        transcript: List[str] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            content = m.get("content", {})
            text = content.get("text", "") if isinstance(content, dict) else str(content)
            transcript.append(f"{m.get('role', 'user')}: {text}")
        prompt = "\n\n".join(transcript)
        system_prompt = params.get("systemPrompt")
        try:
            max_tokens = int(params.get("maxTokens", 512) or 512)
        except (TypeError, ValueError):
            max_tokens = 512

        try:
            from llm_client import client as ai_client
            if not hasattr(ai_client, "chat"):
                raise AttributeError("llm client has no chat()")
            reply = ai_client.chat(prompt, system=system_prompt)
        except Exception as e:
            logger.info(f"[REAPClient] Sampling unavailable ({e}); REA will proceed without it")
            self._send_error(req_id, -32603, f"sampling unavailable: {e}")
            return

        if not reply:
            self._send_error(req_id, -32603, "sampling produced no completion")
            return

        self._send_response(req_id, {
            "role": "assistant",
            "model": "saturn-llm",
            "content": {"type": "text", "text": str(reply)[: max_tokens * 4]},
            "stopReason": "endTurn",
        })

    def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Calls an MCP tool on the REA server.
        Handles prompt-injection defense sanitization on results.
        """
        if not self.is_alive():
            if not self.start():
                return {"error": "REA server is not running and failed to start"}

        resp = self._send_request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=timeout or self.timeout,
        )

        if not resp:
            return {"error": f"Tool call '{tool_name}' timed out"}

        if "error" in resp:
            err = resp["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            return {"error": msg}

        res = resp.get("result", {})
        is_error = res.get("isError", False)
        content_items = res.get("content", [])
        structured = res.get("structuredContent")
        meta = res.get("_meta", {})

        # Extract text content
        text_parts = []
        for item in content_items:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))

        joined_text = "\n".join(text_parts).strip()

        # Prompt injection safety flag
        untrusted = False
        prov = meta.get("provenance", {})
        if prov.get("untrusted_content"):
            untrusted = True

        return {
            "success": not is_error,
            "text": joined_text,
            "structured": structured,
            "meta": meta,
            "is_untrusted_target_content": untrusted,
            "error": joined_text if is_error else None,
        }

    def list_tools(self) -> List[dict]:
        """Returns cached tool definitions advertised by REA."""
        if not self._tools and self.is_alive():
            resp = self._send_request("tools/list", {})
            if resp and "result" in resp:
                self._tools = resp["result"].get("tools", [])
        return list(self._tools)

    # ── Convenience Methods for REA Capabilities ──────────────────────────────

    def analyze_javascript_application(
        self,
        target_path: str,
        detail: str = "summary",
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Inspects an extracted JavaScript or Electron application directory or ASAR.
        Returns modules, imports, entrypoints, and Electron boundaries.
        """
        return self.call_tool(
            "analyze_javascript_application",
            {"target_path": str(Path(target_path).resolve()), "detail": detail},
            timeout=timeout,
        )

    def inspect_binary_layout(
        self,
        binary_path: str,
        detail: str = "summary",
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Inspects a native binary layout (PE, ELF, Mach-O).
        Extracts sections, symbols, linked libraries, imports, and security mitigations.
        """
        return self.call_tool(
            "inspect_binary_layout",
            {"target_path": str(Path(binary_path).resolve()), "detail": detail},
            timeout=timeout,
        )

    def analyze_function(
        self,
        binary_path: str,
        function_address_or_name: str,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """
        Decompiles a function into pseudocode and assembly.
        Extracts basic blocks, control flow graph, callers, and callees.
        """
        return self.call_tool(
            "analyze_function",
            {
                "target_path": str(Path(binary_path).resolve()),
                "procedure": function_address_or_name,
            },
            timeout=timeout,
        )

    def inspect_wasm(
        self,
        wasm_path: str,
        detail: str = "summary",
        timeout: int = 90,
    ) -> Dict[str, Any]:
        """Inspects and decodes WebAssembly (.wasm) into WAT and exports/imports."""
        return self.call_tool(
            "inspect_wasm_artifact",
            {"target_path": str(Path(wasm_path).resolve()), "detail": detail},
            timeout=timeout,
        )

    def trace_feature(
        self,
        evidence_id: str,
        seed_module_or_entry: str,
        timeout: int = 120,
    ) -> Dict[str, Any]:
        """Traces application logic from a known seed or entrypoint across the app graph."""
        return self.call_tool(
            "trace_application_feature",
            {
                "application": {"kind": "retained-evidence", "evidence_id": evidence_id},
                "seed": {"kind": "module", "value": seed_module_or_entry, "match": "substring"},
            },
            timeout=timeout,
        )

    def inspect_analysis_view(
        self,
        evidence_reference: str,
        view_type: str = "api-surface",
        offset: int = 0,
        limit: int = 64,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """Paginates or selects a specific view (e.g. api-surface, pseudocode, exports)."""
        return self.call_tool(
            "inspect_analysis_view",
            {
                "source": {"kind": "retained-evidence", "evidence_id": evidence_reference},
                "view": {"type": view_type, "offset": offset, "limit": limit},
            },
            timeout=timeout,
        )

    def close(self) -> str:
        """Terminates the REA MCP server subprocess cleanly."""
        with self._lock:
            self._running = False
            if self._proc:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=3)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None
            self._is_ready.clear()
            return "REA MCP server stopped."


# ── Global Singleton Accessor ─────────────────────────────────────────────────

_rea_client: Optional[REAMCPClient] = None
_rea_lock = threading.Lock()


def get_rea_client() -> REAMCPClient:
    """Returns the process-wide REA client singleton."""
    global _rea_client
    with _rea_lock:
        if _rea_client is None:
            _rea_client = REAMCPClient()
        return _rea_client
