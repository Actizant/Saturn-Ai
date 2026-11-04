from core.user_paths import get_user_data_dir
from core.gemini_models import generate as gemini_generate
import json
import logging
import re
import requests
import time
from pathlib import Path
from typing import Optional
from or_client import client as openrouter_client

logger = logging.getLogger("llm_client")

API_KEYS_PATH_NAME = "api_keys.json"


def _key_phrase(text: str) -> str:
    """The fragment of a provider error a human can act on.

    Google and OpenRouter state the code, the RPC name and the python exception
    class first and put the one useful sentence last, so a head clip cut the
    advice off ("...PermissionDenied: Your bi"). Split on the punctuation and
    keep the longest trailing clause.
    """
    parts = [p.strip() for p in re.split(r"[:;.\n]", str(text or "")) if p.strip()]
    if not parts:
        return ""
    for frag in reversed(parts):
        if len(frag) >= 20 and not frag.startswith("http"):
            return frag
    return parts[-1]


def _clip(text: str, limit: int = 180) -> str:
    """Collapse a provider error to one readable line without losing its point."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    # Never stop mid-word: "an outstandi" tells the user nothing.
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:.\u2013") + "\u2026"

def _get_base_dir() -> Path:
    import sys
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR = _get_base_dir()
SETTINGS_PATH = get_user_data_dir() / "config" / "app_settings.json"

class UnifiedAIClient:
    def __init__(self):
        self._provider = "OpenRouter"
        self._local_url = "http://localhost:11434/v1"
        self._local_model = "llama3.2"
        # Honest record of what the cloud did the last time it was asked. Every
        # leg of one request is kept, so the UI can say "Gemini refused because
        # billing is unpaid, this answer came from the local model" instead of
        # showing a green chip that only proves a key is typed into a box. Before
        # this existed a 403 fell through two silent excepts into llama-server and
        # nothing in the app or the logs admitted the brain had been downgraded.
        self._legs: list[dict] = []
        self._status: dict = {"provider": None, "state": "untested",
                              "reason": "", "served_by": None, "checked_at": 0.0,
                              "chain": []}
        self._on_status = None
        self.reload_settings()

    # ── provider health ─────────────────────────────────────────────────
    def set_status_callback(self, callback) -> None:
        """Register a callable that receives the status dict on every change."""
        self._on_status = callback

    def provider_status(self) -> dict:
        """A copy of the last real cloud outcome."""
        return dict(self._status)

    def _begin(self) -> None:
        self._legs = []

    def begin_request(self) -> None:
        """Public entry for callers that run their own cascade (main.py)."""
        self._begin()

    def note_leg(self, provider: str, state: str, reason: str = "") -> None:
        """Record what one brain did during the request in progress."""
        self._note(provider, state, reason)

    def end_request(self, provider: str, served_by: Optional[str] = None) -> None:
        """Freeze the turn; ``served_by`` is inferred from the legs when omitted."""
        if served_by is None:
            for leg in reversed(self._legs):
                if leg["state"] == "ok":
                    served_by = leg["provider"]
                    break
        self._publish(provider, served_by)

    def _note(self, provider: str, state: str, reason: str) -> None:
        self._legs.append({"provider": provider, "state": state, "reason": reason})
        if state != "ok":
            logger.warning(f"[LLM Client] {provider} -> {state}: {reason}")

    def _publish(self, provider: str, served_by: Optional[str]) -> None:
        """Freeze one request's outcome and tell anyone who registered."""
        fails = [leg for leg in self._legs if leg["state"] != "ok"]
        if served_by and not fails:
            state, reason = "ok", ""
        elif fails:
            state, reason = fails[0]["state"], fails[0]["reason"]
        else:
            state, reason = "error", "The request produced no text."
        status = {"provider": provider, "state": state, "reason": reason,
                  "served_by": served_by, "checked_at": time.time(),
                  "chain": list(self._legs)}
        changed = (state != self._status.get("state")
                   or served_by != self._status.get("served_by"))
        self._status = status
        if changed and callable(self._on_status):
            try:
                self._on_status(dict(status))
            except Exception as exc:                      # a UI hook must never
                logger.error(f"[LLM Client] status callback failed: {exc}")

    @staticmethod
    def classify(exc: BaseException) -> tuple[str, str]:
        """Turn a provider exception into (state, short human reason).

        The distinction matters to the user: an unpaid Google account, a key that
        was never entered, a revoked key and a dead network are four different
        fixes, and all four used to look identical from here - a swallowed
        exception and a slower answer.
        """
        raw = str(exc)
        text = _clip(raw)
        phrase = _clip(_key_phrase(raw), 110)
        if phrase[:1].isupper() and not phrase[:4].isupper():
            phrase = phrase[0].lower() + phrase[1:]
        low = text.lower()
        code = getattr(exc, "code", None)
        try:
            code = int(code)
        except (TypeError, ValueError):
            code = None
        if code == 403 or "permission_denied" in low:
            if "balance" in low or "billing" in low:
                # Short on purpose: every surface that shows this has a different
                # width budget, and the full provider text is in the log line that
                # _note() writes alongside it.
                return "blocked", ("Unpaid billing balance on the provider "
                                   "account, so every model is refused.")
            return "forbidden", f"The provider refused this request: {phrase or text}"
        if code == 401 or "unauthenticated" in low or "invalid api key" in low:
            return "bad_key", f"The provider rejected the stored key: {phrase or text}"
        if isinstance(exc, PermissionError) or "api key is missing" in low:
            return "no_key", "No key is stored for this provider."
        if (code == 429 or "resource_exhausted" in low or "rate limit" in low
                or "quota" in low or "too many requests" in low):
            return "rate_limited", f"Quota or rate limit reached: {phrase or text}"
        if code == 404 or "no longer available" in low or "not found" in low:
            return "retired", f"That model name is no longer offered: {phrase or text}"
        if isinstance(exc, requests.exceptions.Timeout) or "timeout" in low:
            return "unreachable", "The provider did not answer in time."
        if (isinstance(exc, requests.exceptions.ConnectionError)
                or "failed to establish" in low or "max retries" in low
                or "name or service not known" in low):
            return "unreachable", "No route to the provider; check the internet connection."
        return "error", f"{type(exc).__name__}: {phrase or text}"

    def _api_keys(self) -> dict:
        """The DPAPI-sealed key file, or {} when it cannot be read."""
        try:
            from core.secret_store import load_json
            return load_json(get_user_data_dir() / "config" / API_KEYS_PATH_NAME) or {}
        except Exception:
            return {}

    def reload_settings(self):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._provider = data.get("default_ai_provider", "OpenRouter")
            self._local_url = data.get("local_ai_url", "http://localhost:11434/v1").rstrip("/")
            self._local_model = data.get("local_ai_model", "llama3.2")
        except Exception as e:
            logger.error(f"[LLM Client] Failed to load settings: {e}")

    def _local_chat_completion(self, messages: list[dict], temperature: float = 0.7, response_format: Optional[dict] = None) -> Optional[str]:
        payload = {
            "model": self._local_model,
            "messages": messages,
            "temperature": temperature
        }
        if response_format:
            payload["response_format"] = response_format

        endpoint = f"{self._local_url}/chat/completions"
        try:
            resp = requests.post(
                endpoint,
                headers={"Content-Type": "application/json"},
                json=payload,
                timeout=120
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content.strip() if content else None
            else:
                logger.error(f"[LLM Client] Local AI Error {resp.status_code}: {resp.text}")
                self._note("Local", "error",
                           f"The local brain answered HTTP {resp.status_code}.")
                return None
        except Exception as e:
            logger.error(f"[LLM Client] Local AI Request Failed: {e}")
            state, reason = self.classify(e)
            self._note("Local", "unreachable" if state == "unreachable" else "error",
                       f"The local brain at {self._local_url} is not answering "
                       f"({reason})." if state != "unreachable" else reason)
            return None

    def _get_gemini_client(self):
        """Build the Gemini client, recording why it could not be built.

        "No key", "SDK missing" and "client failed to start" are different
        problems; lumping them into a silent None made an account-level block and
        an empty settings field look the same from the outside.
        """
        try:
            from google import genai
        except Exception as exc:
            self._note("Gemini", "error",
                       f"The Gemini SDK is not installed in this environment: {_clip(exc)}")
            return None
        key = (self._api_keys().get("gemini_api_key") or "").strip()
        if not key:
            self._note("Gemini", "no_key", "No Gemini key is stored in Settings.")
            return None
        try:
            return genai.Client(api_key=key, http_options={"api_version": "v1beta"})
        except Exception as exc:
            logger.error(f"[LLM Client] Failed to init Gemini client: {exc}")
            self._note("Gemini", *self.classify(exc))
            return None

    def _gemini_chat_completion(self, messages: list[dict], temperature: float = 0.7, json_mode: bool = False) -> Optional[str]:
        client = self._get_gemini_client()
        if not client:
            return None
        try:
            from google.genai import types
            sys_parts = [m["content"] for m in messages if m["role"] == "system"]
            system_instruction = "\n".join(sys_parts) if sys_parts else None

            contents = []
            for m in messages:
                if m["role"] == "system":
                    continue
                role = "user" if m["role"] == "user" else "model"
                contents.append(types.Content(role=role, parts=[types.Part.from_text(text=str(m["content"]))]))

            config = types.GenerateContentConfig(
                temperature=temperature,
                system_instruction=system_instruction,
                response_mime_type="application/json" if json_mode else None
            )
            # Climb the ladder instead of betting the whole brain on one name:
            # the top rung can be retired or out of daily quota while the next
            # one is idle.
            resp = gemini_generate(client, contents, config=config)
            text = resp.text.strip() if resp and resp.text else ""
            if text:
                self._note("Gemini", "ok", "")
                return text
            self._note("Gemini", "error", "Gemini answered with no text.")
            return None
        except Exception as e:
            logger.error(f"[LLM Client] Gemini request failed: {e}")
            self._note("Gemini", *self.classify(e))
            return None

    def chat(self, prompt: str, system: str = "You are a helpful assistant.", history: Optional[list[dict]] = None, model: Optional[str] = None, max_tokens: int = 4096, temperature: float = 0.7) -> str:
        self.reload_settings()
        self._begin()
        if self._provider in ("Gemini", "Google Gemini"):
            messages = [{"role": "system", "content": system}]
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": prompt})
            result = self._gemini_chat_completion(messages, temperature)
            if result:
                self._publish("Gemini", "Gemini")
                return result
            try:
                out = openrouter_client.chat(prompt, system, history, model, max_tokens, temperature)
                if out:
                    self._publish("Gemini", "OpenRouter")
                    return out
                self._note("OpenRouter", "error", "OpenRouter answered with no text.")
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
            local = self._local_chat_completion(messages, temperature) or ""
            self._publish("Gemini", "Local" if local else None)
            return local
        elif self._provider == "Local":
            messages = [{"role": "system", "content": system}]
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": prompt})
            result = self._local_chat_completion(messages, temperature)
            if result:
                self._publish("Local", "Local")
                return result
            self._publish("Local", None)
            raise RuntimeError("Local AI request failed. Please check if Ollama or LM Studio is running.")
        else:
            try:
                out = openrouter_client.chat(prompt, system, history, model, max_tokens, temperature)
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
                self._publish("OpenRouter", None)
                raise
            self._publish("OpenRouter", "OpenRouter" if out else None)
            return out

    def chat_json(self, prompt: str, system: str = "Return ONLY valid JSON.", model: Optional[str] = None, max_tokens: int = 4096) -> dict:
        self.reload_settings()
        self._begin()
        if self._provider in ("Gemini", "Google Gemini"):
            messages = [
                {"role": "system", "content": system + " Output valid JSON only, without any markdown formatting."},
                {"role": "user", "content": prompt}
            ]
            raw = self._gemini_chat_completion(messages, temperature=0.2, json_mode=True)
            if not raw:
                try:
                    data = openrouter_client.chat_json(prompt, system, model, max_tokens)
                    self._publish("Gemini", "OpenRouter")
                    return data
                except Exception as exc:
                    self._note("OpenRouter", *self.classify(exc))
                raw = self._local_chat_completion(messages, temperature=0.2, response_format={"type": "json_object"})
                if raw:
                    self._publish("Gemini", "Local")
                else:
                    self._publish("Gemini", None)
                    raise RuntimeError("AI request failed for JSON generation.")
            else:
                self._publish("Gemini", "Gemini")
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip().rstrip("`").strip()
            try:
                return json.loads(clean)
            except json.JSONDecodeError as e:
                raise ValueError(f"Gemini model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")
        elif self._provider == "Local":
            messages = [
                {"role": "system", "content": system + " Output valid JSON only, without any markdown formatting."},
                {"role": "user", "content": prompt}
            ]
            raw = self._local_chat_completion(messages, temperature=0.2, response_format={"type": "json_object"})
            if not raw:
                self._publish("Local", None)
                raise RuntimeError("Local AI request failed.")
            self._publish("Local", "Local")
            
            clean = raw.strip()
            if clean.startswith("```"):
                parts = clean.split("```")
                clean = parts[1] if len(parts) > 1 else clean
                if clean.startswith("json"):
                    clean = clean[4:]
            clean = clean.strip().rstrip("`").strip()
            
            try:
                return json.loads(clean)
            except json.JSONDecodeError as e:
                raise ValueError(f"Local model returned unparseable JSON: {e}\nRaw output: {raw[:200]}")
        else:
            try:
                data = openrouter_client.chat_json(prompt, system, model, max_tokens)
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
                self._publish("OpenRouter", None)
                raise
            self._publish("OpenRouter", "OpenRouter")
            return data

    def vision(self, prompt: str, image_b64: str, mime: str = "image/png", system: str = "Analyze the image.", model: Optional[str] = None, max_tokens: int = 1024) -> str:
        self.reload_settings()
        self._begin()
        if self._provider in ("Gemini", "Google Gemini"):
            client = self._get_gemini_client()
            if client:
                try:
                    import base64
                    from google.genai import types
                    image_bytes = base64.b64decode(image_b64)
                    resp = gemini_generate(
                        client,
                        [
                            types.Part.from_bytes(data=image_bytes, mime_type=mime),
                            prompt
                        ],
                        config=types.GenerateContentConfig(
                            system_instruction=system,
                            max_output_tokens=max_tokens
                        )
                    )
                    if resp and resp.text:
                        self._note("Gemini", "ok", "")
                        self._publish("Gemini", "Gemini")
                        return resp.text.strip()
                    self._note("Gemini", "error", "Gemini answered the image with no text.")
                except Exception as e:
                    logger.error(f"[LLM Client] Gemini vision request failed: {e}")
                    self._note("Gemini", *self.classify(e))
            try:
                out = openrouter_client.vision(prompt, image_b64, mime, system, model, max_tokens)
                if out:
                    self._publish("Gemini", "OpenRouter")
                    return out
                self._note("OpenRouter", "error", "OpenRouter answered with no text.")
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
            self._publish("Gemini", None)
            raise RuntimeError("Vision request failed with Gemini provider.")
        elif self._provider == "Local":
            messages = [
                {"role": "system", "content": system},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
            result = self._local_chat_completion(messages, temperature=0.2)
            if result:
                self._publish("Local", "Local")
                return result
            self._publish("Local", None)
            raise RuntimeError("Local AI vision request failed.")
        else:
            try:
                out = openrouter_client.vision(prompt, image_b64, mime, system, model, max_tokens)
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
                self._publish("OpenRouter", None)
                raise
            self._publish("OpenRouter", "OpenRouter" if out else None)
            return out

    def vision_from_file(self, prompt: str, image_path: str, system: str = "Analyze the image.", model: Optional[str] = None, max_tokens: int = 1024) -> str:
        self.reload_settings()
        if self._provider in ("Local", "Gemini", "Google Gemini"):
            import base64
            path = Path(image_path)
            mime_map = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
            mime = mime_map.get(path.suffix.lower(), "image/png")
            with open(path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode("utf-8")
            return self.vision(prompt, image_b64, mime, system, model, max_tokens)
        else:
            return openrouter_client.vision_from_file(prompt, image_path, system, model, max_tokens)

    def multi_turn(self, messages: list[dict], model: Optional[str] = None, max_tokens: int = 4096, temperature: float = 0.7) -> str:
        self.reload_settings()
        self._begin()
        if self._provider in ("Gemini", "Google Gemini"):
            result = self._gemini_chat_completion(messages, temperature)
            if result:
                self._publish("Gemini", "Gemini")
                return result
            try:
                out = openrouter_client.multi_turn(messages, model, max_tokens, temperature)
                if out:
                    self._publish("Gemini", "OpenRouter")
                    return out
                self._note("OpenRouter", "error", "OpenRouter answered with no text.")
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
            local = self._local_chat_completion(messages, temperature) or ""
            self._publish("Gemini", "Local" if local else None)
            return local
        elif self._provider == "Local":
            result = self._local_chat_completion(messages, temperature)
            if result:
                self._publish("Local", "Local")
                return result
            self._publish("Local", None)
            raise RuntimeError("Local AI request failed.")
        else:
            try:
                out = openrouter_client.multi_turn(messages, model, max_tokens, temperature)
            except Exception as exc:
                self._note("OpenRouter", *self.classify(exc))
                self._publish("OpenRouter", None)
                raise
            self._publish("OpenRouter", "OpenRouter" if out else None)
            return out

client = UnifiedAIClient()
LLMClient = UnifiedAIClient
