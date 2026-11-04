"""
core/api_brain.py - The cloud-API "brain" facade.

This replaces the in-process GGUF brain (core/local_ai/*). Every text call now
goes to a generic OpenAI-compatible ``/chat/completions`` endpoint. By default it
targets OpenRouter, but any provider that speaks the OpenAI schema works: point
``api_base_url`` + ``api_model`` at it in Settings and put the key in
``config/api_keys.json`` (``api_key``, or a provider-specific key). No weights,
no GPU, no local runtime.

The public surface is intentionally identical to the old ``NativeBrain`` so
``llm_client.py``, ``main.py`` and the tool loop did not have to change their
call shape - only where the tokens come from changed. ``chat_complete`` returns
the OpenAI-compatible envelope the tool loop already understands::

    {"choices": [{"message": {"role": "assistant", "content": ..., "tool_calls": [...]}}]}

so the existing tool-execution code in main.py works untouched. The endpoint does
the function-calling natively; we just relay it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from core.gemini_models import resolve as gemini_model

logger = logging.getLogger("api_brain")

# Default endpoint: OpenRouter, an OpenAI-compatible aggregator, so a single
# configured key reaches many models. Overridable via Settings (api_base_url).
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_ENDPOINT = "openai-compatible://api"

# --- Provider registry -----------------------------------------------------------
# Each entry: display name → (base_url, default_model, key_field_in_api_keys_json).
# The UI presents these as a dropdown; selecting one writes api_base_url +
# api_model into settings so _resolve_endpoint() picks them up.
PROVIDERS: Dict[str, tuple] = {
    "OpenRouter":  ("https://openrouter.ai/api/v1", "openai/gpt-4o-mini", "openrouter_api_key"),
    "Groq":        ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile", "groq_api_key"),
    # The Gemini default is resolved, not written down: the floating "-latest"
    # alias this used to carry answered 503 when probed on 2026-10-30.
    "Gemini":      ("https://generativelanguage.googleapis.com/v1beta/openai", gemini_model(), "gemini_api_key"),
    "OpenAI":      ("https://api.openai.com/v1", "gpt-4o-mini", "openai_api_key"),
    "Together":    ("https://api.together.xyz/v1", "meta-llama/Llama-3.3-70B-Instruct-Turbo-Free", "together_api_key"),
    "DeepInfra":   ("https://api.deepinfra.com/v1/openai", "meta-llama/Meta-Llama-3-70B-Instruct", "deepinfra_api_key"),
}

# Retained for the old ``from core.local_brain import CORE_LOCAL_TOOL_NAMES``
# callers. A capable cloud model is not drowned by the full tool list the way a
# 1.5B local brain was, so this set no longer filters anything - it only documents
# the desktop tools the assistant is expected to reach for first.
CORE_LOCAL_TOOL_NAMES = {
    "open_app", "computer_settings", "system_diagnostics", "spotify_controller",
    "youtube_video", "web_search", "weather_report", "file_controller",
    "smart_organizer", "desktop_control", "execute_protocol", "reminder",
    "word_document", "pdf_document", "dev_agent", "recall_memory",
    "save_memory", "shutdown_saturn", "undo",
}

# Key lookup order in api_keys.json. A generic ``api_key`` always wins; otherwise
# the first populated provider key is used, so whatever provider you add "just
# plugs in" without a code change.
_KEY_PRIORITY = (
    "api_key",
    "openrouter_api_key",
    "groq_api_key",
    "gemini_api_key",
    "openai_api_key",
    "together_api_key",
    "deepinfra_api_key",
    "anthropic_api_key",
)


def convert_schema_to_lowercase(schema: Any) -> Any:
    """Recursively lowercase Gemini-style schema type names (STRING -> string)."""
    if not isinstance(schema, dict):
        return schema
    res: Dict[str, Any] = {}
    for k, v in schema.items():
        if k == "type" and isinstance(v, str):
            res[k] = v.lower()
        elif isinstance(v, dict):
            res[k] = convert_schema_to_lowercase(v)
        elif isinstance(v, list):
            res[k] = [convert_schema_to_lowercase(i) if isinstance(i, dict) else i for i in v]
        else:
            res[k] = v
    return res


def format_tools_for_local(tool_declarations: List[Dict[str, Any]], focus_core: bool = True) -> List[Dict[str, Any]]:
    """Normalise Gemini- or OpenAI-style tool declarations to OpenAI function
    tools with lowercased JSON schema types.

    ``focus_core`` is kept as a signature argument so existing callers do not
    break, but it no longer restricts the set: the small-model cap existed only
    because a 1.5B local brain could not pick among dozens of tools, which does
    not apply to a cloud model. The full declared tool list is relayed."""
    formatted: List[Dict[str, Any]] = []
    for t in tool_declarations or []:
        name = t.get("name") or (t.get("function", {}).get("name") if isinstance(t.get("function"), dict) else None)
        if not name:
            continue
        if t.get("type") == "function" and isinstance(t.get("function"), dict):
            fn = dict(t["function"])
            if "parameters" in fn:
                fn["parameters"] = convert_schema_to_lowercase(fn["parameters"])
            formatted.append({"type": "function", "function": fn})
        else:
            formatted.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": t.get("description", ""),
                    "parameters": convert_schema_to_lowercase(
                        t.get("parameters", {"type": "object", "properties": {}})),
                },
            })
    return formatted


def _api_keys() -> Dict[str, Any]:
    """Load the secrets file the same way the rest of the app does: the user-data
    config first, then the in-repo config as a fallback."""
    candidates: List[Path] = []
    try:
        from core.user_paths import get_user_data_dir
        candidates.append(get_user_data_dir() / "config" / "api_keys.json")
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parent.parent / "config" / "api_keys.json")
    for path in candidates:
        try:
            if path.exists():
                from core.secret_store import load_json
                data = load_json(path)
                if isinstance(data, dict):
                    return data
        except Exception:
            continue
    return {}


def _resolve_endpoint() -> tuple:
    """(base_url, api_key, model) for the live API brain.

    Settings win over defaults. The key comes from api_keys.json; when a named
    provider is selected via ``api_provider`` in settings, that provider's key
    field is tried first before falling through the generic priority list."""
    from memory import config_manager

    base_url = str(config_manager.get_setting("api_base_url", "") or "").strip().rstrip("/")
    if not base_url:
        base_url = DEFAULT_BASE_URL
    model = str(config_manager.get_setting("api_model", "") or "").strip()
    if not model:
        model = DEFAULT_MODEL

    keys = _api_keys()
    api_key = ""

    # Try the selected provider's specific key first.
    provider_name = str(config_manager.get_setting("api_provider", "") or "").strip()
    if provider_name and provider_name in PROVIDERS:
        _url, _mdl, key_field = PROVIDERS[provider_name]
        val = str(keys.get(key_field, "") or "").strip()
        if val:
            api_key = val

    # Fall through to generic priority list.
    if not api_key:
        for name in _KEY_PRIORITY:
            val = str(keys.get(name, "") or "").strip()
            if val:
                api_key = val
                break

    # An explicit key stored directly in settings overrides the file.
    settings_key = str(config_manager.get_setting("api_key", "") or "").strip()
    if settings_key:
        api_key = settings_key
    return base_url, api_key, model


class ApiBrain:
    """Cloud-API replacement for the old in-process ``NativeBrain``."""

    def __init__(self, default_model: Optional[str] = None) -> None:
        self._model_override = default_model
        self.endpoint = DEFAULT_ENDPOINT  # inert label kept for older callers
        self.enabled = True
        self.default_model = default_model or DEFAULT_MODEL

    # ----------------------------------------------------------- availability ---

    def is_available(self) -> bool:
        """True when a base_url, a model and a key are all resolvable. No network
        probe: this must be cheap and offline-safe (the app calls it at boot)."""
        try:
            base_url, api_key, model = _resolve_endpoint()
        except Exception:
            return False
        self.default_model = self._model_override or model
        return bool(base_url and api_key and model)

    def list_installed_models(self) -> List[str]:
        """The API brain has no local weights; report the configured model so the
        status card is still truthful about which brain is answering."""
        try:
            _, _, model = _resolve_endpoint()
            return [model] if model else []
        except Exception:
            return []

    def pull_model_async(self, model_name: Optional[str] = None, progress_callback=None) -> None:
        """No-op for an API brain - there is nothing to download. Kept so the old
        ``manage_local_model`` tool path and any UI wiring still resolve."""
        target = model_name or self.default_model
        if progress_callback:
            try:
                progress_callback({"status": "api brain: no download required"})
            except Exception:
                pass
        return None

    # -------------------------------------------------------------- inference ---

    def chat_complete(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.1,
        focus_core: bool = True,
    ) -> Dict[str, Any]:
        """One turn. Returns the OpenAI-compatible completion the tool loop reads."""
        import requests

        base_url, api_key, default_model = _resolve_endpoint()
        active_model = model or self._model_override or default_model
        if not (base_url and api_key):
            raise RuntimeError(
                "The API brain is not configured. Set the endpoint and key in "
                "Settings (API provider), then add your key to config/api_keys.json."
            )
        self.default_model = active_model

        payload: Dict[str, Any] = {
            "model": active_model,
            "messages": messages,
            "temperature": temperature,
        }
        formatted_tools = format_tools_for_local(tools, focus_core=focus_core) if tools else None
        if formatted_tools:
            payload["tools"] = formatted_tools
            payload["tool_choice"] = "auto"

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API brain error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        choices = data.get("choices") or [{}]
        message = choices[0].get("message", {}) or {}
        return {
            "model": data.get("model", active_model),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": choices[0].get("finish_reason", "stop")}],
        }

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
    ) -> Generator[str, None, None]:
        """Token-level text deltas (plain strings) from an SSE stream.

        Raises immediately (not at first ``next()``) when the brain is not
        configured, so callers fail loudly at request time."""
        import requests

        base_url, api_key, default_model = _resolve_endpoint()
        active_model = model or self._model_override or default_model
        if not (base_url and api_key):
            raise RuntimeError(
                "The API brain is not configured. Set the endpoint and key in "
                "Settings (API provider), then add your key to config/api_keys.json."
            )
        self.default_model = active_model

        payload: Dict[str, Any] = {
            "model": active_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        formatted_tools = format_tools_for_local(tools) if tools else None
        if formatted_tools:
            payload["tools"] = formatted_tools
            payload["tool_choice"] = "auto"

        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=120,
            stream=True,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"API brain error {resp.status_code}: {resp.text[:300]}")
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                obj = json.loads(chunk)
            except json.JSONDecodeError:
                continue
            for choice in obj.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content")
                if delta:
                    yield delta

    def generate_chat_stream(
        self,
        messages: List[Dict[str, Any]],
        model: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
    ) -> Generator[Dict[str, Any], None, None]:
        """Compatibility generator yielding OpenAI-style chunk dicts. Relays the
        streamed text deltas so existing OpenAI-chunk consumers still work."""
        try:
            # Stream text deltas, emitting one OpenAI-shaped delta chunk at a time.
            for delta in self.chat_stream(messages, model=model, tools=tools, temperature=temperature):
                yield {"choices": [{"delta": {"content": delta}, "index": 0}]}
            yield {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}
        except Exception as exc:
            logger.error("[API BRAIN] stream generation failed: %s", exc)
            yield {"choices": [{"delta": {"role": "assistant", "content": ""}, "index": 0}]}


api_brain = ApiBrain()
