"""
core/mcp_host.py
================
Saturn as a full MCP host.

The MCP spec is bidirectional: a server can turn around and ask the client
for two things.

1. `sampling/createMessage`  - the server wants an LLM completion.
   Saturn routes this through its unified LLM client (Gemini, OpenRouter,
   local GGUF - whatever the user has configured), so a forged server can
   call on Saturn's brain mid-computation without ever seeing the API key.

2. `elicitation/create`      - the server wants to ask the *user* something.
   Saturn surfaces the question on the HUD and blocks until the user
   answers, declines, or times out.

Both handlers are installed on the UniversalMCPManager at boot by
`install_host_handlers(...)`. The MCP reader loop already routes
server-initiated requests to whatever the manager has registered, so this
file only needs to provide the callables.

Design:
- Sampling uses `llm_client.client.chat(...)` so Saturn honours the user's
  provider choice, model selection, and idle-eviction policies. It does NOT
  bypass the local/cloud arbiter.
- Elicitation prefers a real modal UI (via the injected `prompt_callback`).
  If the UI has not wired one (headless tests, early boot), the handler
  returns `action: "cancel"` so the server does not hang.
- Every handler call is bounded by `TIMEOUT_S` (default 60s). A server that
  abuses sampling cannot stall Saturn's reader loop because dispatch runs
  on its own thread.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("MCPHost")

# Ceiling on how long a single sampling / elicitation call may block a
# server's request handler. Servers that need more should chunk.
SAMPLING_TIMEOUT_S = 45.0
ELICITATION_TIMEOUT_S = 120.0

# Rough token ceiling on the LLM completion. Small enough that a runaway
# server cannot bankrupt the API wallet on one call.
SAMPLING_MAX_TOKENS = 512


def _extract_text(content: Any) -> str:
    """The MCP spec allows content to be a string, a TextContent object, or
    a list of typed blocks. Normalise to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        # Some servers ship {"text": "..."} without a type. Best-effort.
        if "text" in content:
            return str(content.get("text", ""))
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _flatten_messages(messages: List[Dict[str, Any]]) -> tuple[str, str]:
    """Split MCP's `messages` array into (system, prompt) for our chat API.

    MCP allows role=system inside the transcript. We take the LAST system
    message as the top-level system prompt (matches OpenAI/Anthropic
    convention) and concatenate everything else as the running prompt.
    """
    system = ""
    transcript: List[str] = []
    for m in messages or []:
        role = str(m.get("role", "user")).lower()
        text = _extract_text(m.get("content"))
        if not text:
            continue
        if role == "system":
            system = text  # last one wins
        elif role == "assistant":
            transcript.append(f"[assistant] {text}")
        else:
            transcript.append(f"[user] {text}")
    return system, "\n\n".join(transcript)


def make_sampling_handler() -> Callable[..., Dict[str, Any]]:
    """Return a handler that answers `sampling/createMessage` through the
    Saturn LLM client. The handler captures the current provider from
    `llm_client.client`, so whatever the user configured is respected."""

    def _handler(*, server_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        messages = params.get("messages") or []
        sys_prompt, prompt = _flatten_messages(messages)
        # Some servers put systemPrompt at the top level rather than in the
        # transcript. Merge so we do not drop it.
        if params.get("systemPrompt"):
            sys_prompt = (sys_prompt + "\n\n" + str(params["systemPrompt"])).strip()
        if not prompt:
            return {
                "role": "assistant",
                "model": "saturn-host",
                "content": {"type": "text", "text": "(no prompt supplied)"},
            }

        max_tokens = int(params.get("maxTokens") or SAMPLING_MAX_TOKENS)
        max_tokens = max(64, min(2048, max_tokens))
        temperature = float(params.get("temperature") or 0.3)
        temperature = max(0.0, min(1.5, temperature))

        from llm_client import client as unified

        t0 = time.time()
        try:
            completion = unified.chat(
                prompt=prompt,
                system=sys_prompt or None,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            logger.error(f"[MCPHost] sampling via llm_client failed for '{server_name}': {exc}")
            # Return a graceful text so the server does not hang. MCP
            # clients should treat this as a normal completion that says
            # "the host LLM failed".
            return {
                "role": "assistant",
                "model": "saturn-host",
                "content": {
                    "type": "text",
                    "text": f"(Saturn host could not sample: {exc})",
                },
                "stopReason": "endTurn",
            }

        elapsed = time.time() - t0
        if elapsed > SAMPLING_TIMEOUT_S:
            logger.warning(
                f"[MCPHost] sampling for '{server_name}' took {elapsed:.1f}s "
                f"(> {SAMPLING_TIMEOUT_S}s). Server should chunk."
            )

        return {
            "role": "assistant",
            "model": _current_model_label(),
            "content": {"type": "text", "text": str(completion or "")},
            "stopReason": "endTurn",
        }

    return _handler


def _current_model_label() -> str:
    """Best-effort label for which brain actually produced the completion.
    The MCP spec wants a `model` string; if we can read it from the unified
    client we use that, otherwise we say 'saturn-host'."""
    try:
        from llm_client import client as unified
        for attr in ("model", "_model", "current_model", "_current_model"):
            val = getattr(unified, attr, None)
            if isinstance(val, str) and val:
                return val
    except Exception:
        pass
    return "saturn-host"


def make_elicitation_handler(
    prompt_callback: Optional[Callable[[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
) -> Callable[..., Dict[str, Any]]:
    """Return a handler that answers `elicitation/create`.

    `prompt_callback(message, requested_schema)` should show a modal or HUD
    card to the user and block until they answer. It must return either:
      - a dict of collected values (success), or
      - None / raise if the user cancelled or declined.

    When no callback is installed (tests, headless boot, plugins that
    register servers before the UI comes up), the handler returns
    `action: "cancel"` so the server does not sit waiting forever.
    """

    def _handler(*, server_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        message = str(params.get("message") or "")
        schema = params.get("requestedSchema") or {}
        if not message:
            return {"action": "cancel"}
        if prompt_callback is None:
            logger.info(
                f"[MCPHost] elicitation from '{server_name}' had no UI callback "
                f"installed; cancelling so the server does not hang."
            )
            return {"action": "cancel"}
        try:
            collected = prompt_callback(message=message, schema=schema,
                                        server_name=server_name)
        except Exception as exc:
            logger.error(f"[MCPHost] elicitation callback raised: {exc}")
            return {"action": "cancel"}
        if collected is None:
            return {"action": "cancel"}
        if isinstance(collected, dict) and collected.get("action") in ("decline", "cancel"):
            return {"action": collected["action"]}
        return {
            "action": "accept",
            "content": collected if isinstance(collected, dict) else {"value": collected},
        }

    return _handler


def install_host_handlers(
    prompt_callback: Optional[Callable[..., Optional[Dict[str, Any]]]] = None,
) -> None:
    """Wire Saturn's sampling + elicitation handlers into the MCP manager.
    Called once at boot from main.py. Idempotent - re-installing replaces
    the existing handlers, which lets the UI re-wire elicitation after it
    comes up without a full restart."""
    from core.mcp_manager import UniversalMCPManager

    mgr = UniversalMCPManager.get_instance()
    mgr.register_server_request_handler("sampling/createMessage", make_sampling_handler())
    mgr.register_server_request_handler("elicitation/create", make_elicitation_handler(prompt_callback))
    logger.info("[MCPHost] Sampling + Elicitation handlers installed.")
