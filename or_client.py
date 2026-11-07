import os
import json
import sys
import time
import base64
import logging
from pathlib import Path
from typing import Optional

import requests

from core.user_paths import get_user_data_dir

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("openrouter_client")

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR     = _get_base_dir()
USER_CONFIG  = get_user_data_dir() / "config" / "api_keys.json"
LOCAL_CONFIG = BASE_DIR / "config" / "api_keys.json"

def _load_api_key() -> str:
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        return env_key

    for p in (USER_CONFIG, LOCAL_CONFIG):
        try:
            if p.exists():
                from core.secret_store import load_json
                data = load_json(p)
                key = (data.get("openrouter_api_key") or "").strip()
                if key:
                    return key
        except Exception as e:
            logger.warning(f"[OpenRouter] Failed reading {p}: {e}")
    return ""

TEXT_MODELS: list[str] = [
    "openrouter/auto",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "mistralai/mistral-small-3.1:free",
    "liquid/lfm-2.5-1.2b-instruct:free",
    "meta-llama/llama-3.2-3b-instruct:free",
    "google/gemini-2.0-flash-exp:free",
    "google/gemma-2-9b-it:free",
    "deepseek/deepseek-chat:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

VISION_MODELS: list[str] = [
    "openrouter/auto",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "google/gemini-2.0-flash-exp:free",
]

API_URL               = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MAX_TOKENS    = 4096
DEFAULT_TEMPERATURE   = 0.7
REQUEST_TIMEOUT       = 20   # seconds per request
MAX_RETRIES_PER_MODEL = 2    # attempts before moving to next model
RETRY_DELAY           = 1    # seconds between retries
RATE_LIMIT_COOLDOWN   = 60   # seconds before retrying a rate-limited model

_rate_limited: dict[str, float] = {}


class InsufficientCredits(RuntimeError):
    """The account cannot pay, which is not a fault in the key or the connection.

    OpenRouter answers HTTP 402 for this. Until now nothing in ``_call`` looked
    at 402, so an empty wallet was retried twice per model, walked the whole
    pool, and ended in "check your API key and network connection" - three
    wrong directions for the one fix that works, which is adding credit.
    Billing is counted per account, so this leaves the loop immediately.
    """


class OpenRouterClient:

    def __init__(self) -> None:
        self.api_key = ""
        self._headers: dict = {}
        self._ensure_api_key()

    def _ensure_api_key(self) -> str:
        key = _load_api_key()
        if key != self.api_key or not self._headers:
            self.api_key = key
            self._headers = {
                "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
                "Content-Type":  "application/json",
                "HTTP-Referer":  "https://github.com/Actizant",
                "X-Title":       "Saturn",
            }
        return self.api_key

    def _is_rate_limited(self, model: str) -> bool:
        ts = _rate_limited.get(model)
        if ts is None:
            return False
        if time.time() - ts > RATE_LIMIT_COOLDOWN:
            del _rate_limited[model]
            return False
        return True

    def _mark_rate_limited(self, model: str) -> None:
        _rate_limited[model] = time.time()
        logger.warning(
            f"[OpenRouter] Rate limited: {model} — "
            f"cooling down for {RATE_LIMIT_COOLDOWN}s"
        )

    def _call(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> Optional[str]:
        self._ensure_api_key()
        if not self.api_key:
            raise PermissionError(
                "[OpenRouter] API key is missing. Add a valid sk-or- key in Settings."
            )

        payload: dict = {
            "model":       model,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        for attempt in range(1, MAX_RETRIES_PER_MODEL + 1):
            try:
                resp = requests.post(
                    API_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )

                if resp.status_code == 401:
                    raise PermissionError(
                        f"[OpenRouter] Authentication failed: invalid API key (HTTP 401)."
                    )

                if resp.status_code == 403:
                    raise PermissionError(
                        f"[OpenRouter] Access denied for model {model} (HTTP 403)."
                    )

                if resp.status_code == 402:
                    # Say "billing" and quote the code: llm_client classifies an
                    # exception by its text as well as its code, so this reaches
                    # the user as CLOUD BLOCKED instead of a retry suggestion.
                    detail = " ".join(str(getattr(resp, "text", "") or "").split())[:160]
                    raise InsufficientCredits(
                        "[OpenRouter] No billing credit left on this account "
                        "(HTTP 402). The stored key is valid; top up at "
                        f"https://openrouter.ai/settings/credits - {detail}"
                    )

                if resp.status_code == 429:
                    self._mark_rate_limited(model)
                    return None

                if resp.status_code in (400, 404):
                    logger.warning(
                        f"[OpenRouter] {model} unavailable (HTTP {resp.status_code}). Skipping."
                    )
                    return None

                if resp.status_code == 200:
                    data    = resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                    )
                    return content.strip() if content else None

                logger.warning(
                    f"[OpenRouter] {model} → HTTP {resp.status_code} "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )

            except PermissionError:
                raise
            except InsufficientCredits:
                raise
            except requests.exceptions.Timeout:
                logger.warning(
                    f"[OpenRouter] {model} → Timeout "
                    f"(attempt {attempt}/{MAX_RETRIES_PER_MODEL})"
                )
            except Exception as e:
                logger.error(f"[OpenRouter] {model} → Unexpected error: {e}")

            if attempt < MAX_RETRIES_PER_MODEL:
                time.sleep(RETRY_DELAY)

        return None

    def _call_with_fallback(
        self,
        pool: list[str],
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        response_format: Optional[dict] = None,
    ) -> str:
        self._ensure_api_key()
        if not self.api_key:
            raise PermissionError(
                "[OpenRouter] API key is missing. Add a valid sk-or- key in Settings."
            )

        if model and not self._is_rate_limited(model):
            try:
                result = self._call(model, messages, max_tokens, temperature, response_format)
                if result:
                    return result
                logger.info(
                    f"[OpenRouter] Requested model failed, "
                    f"falling back to pool: {model}"
                )
            except PermissionError:
                raise
            except InsufficientCredits:
                raise

        for m in pool:
            if self._is_rate_limited(m):
                continue
            logger.info(f"[OpenRouter] Trying: {m}")
            try:
                result = self._call(m, messages, max_tokens, temperature, response_format)
                if result:
                    logger.info(f"[OpenRouter] ✓ Success: {m}")
                    return result
            except PermissionError:
                raise
            except InsufficientCredits:
                raise
            except Exception as e:
                logger.warning(f"[OpenRouter] Failed on {m}: {e}")

        raise RuntimeError(
            f"[OpenRouter] None of the {len(pool)} configured models answered. "
            "The reasons are in the lines above, and they are usually retired "
            "':free' model names (HTTP 404/400) rather than a bad key: only an "
            "HTTP 401 or 402 means the key or the account itself."
        )

    def chat(
        self,
        prompt: str,
        system: str = (
            "You are a component of Saturn, an autonomous self-evolving personal assistant. "
            "Be concise, helpful, and precise."
        ),
        history: Optional[list[dict]] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        messages = [{"role": "system", "content": system}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": prompt})

        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def chat_json(
        self,
        prompt: str,
        system: str = (
            "Return ONLY valid JSON. "
            "No markdown fences, no extra text, no explanation."
        ),
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict:
        messages = [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ]
        raw = self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature=0.2
        )

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
            logger.error(
                f"[OpenRouter] JSON parse failed: {e}\n"
                f"Raw response (first 300 chars): {raw[:300]}"
            )
            raise ValueError(
                f"Model returned unparseable JSON: {e}\n"
                f"Raw output: {raw[:200]}"
            )

    def vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/png",
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            },
        ]
        return self._call_with_fallback(
            VISION_MODELS, messages, model, max_tokens, temperature=0.2
        )

    def vision_from_file(
        self,
        prompt: str,
        image_path: str,
        system: str = "Analyze the image and describe what you see clearly and concisely.",
        model: Optional[str] = None,
        max_tokens: int = 1024,
    ) -> str:
        path = Path(image_path)
        mime_map = {
            ".png":  "image/png",
            ".jpg":  "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif":  "image/gif",
        }
        mime = mime_map.get(path.suffix.lower(), "image/png")

        with open(path, "rb") as f:
            image_b64 = base64.b64encode(f.read()).decode("utf-8")

        return self.vision(prompt, image_b64, mime, system, model, max_tokens)

    def multi_turn(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
    
        return self._call_with_fallback(
            TEXT_MODELS, messages, model, max_tokens, temperature
        )

    def available_models(self) -> dict:
        return {
            "text_models":   TEXT_MODELS,
            "vision_models": VISION_MODELS,
            "rate_limited":  list(_rate_limited.keys()),
            "total_text":    len(TEXT_MODELS),
            "total_vision":  len(VISION_MODELS),
        }

client = OpenRouterClient()

if __name__ == "__main__":
    print("=" * 55)
    print("  Saturn — OpenRouter Client Self-Test")
    print("=" * 55)

    print("\n[TEST 1] Basic chat...")
    try:
        reply = client.chat("Introduce yourself in one sentence.")
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 2] JSON mode...")
    try:
        data = client.chat_json(
            'List 3 programming languages. Format: {"languages": ["a", "b", "c"]}',
            system="Return only valid JSON. No extra text."
        )
        print(f"  Response : {data}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 3] Multi-turn conversation...")
    try:
        history = [
            {"role": "system",    "content": "You are a helpful assistant. Be brief."},
            {"role": "user",      "content": "My name is User."},
            {"role": "assistant", "content": "Hello User, how can I help you?"},
            {"role": "user",      "content": "What is my name?"},
        ]
        reply = client.multi_turn(history)
        print(f"  Response : {reply}")
        print(f"  Status   : PASS ✓")
    except Exception as e:
        print(f"  Status   : FAIL ✗ — {e}")

    print("\n[TEST 4] Model pool info...")
    info = client.available_models()
    print(f"  Text models   : {info['total_text']}")
    print(f"  Vision models : {info['total_vision']}")
    print(f"  Rate limited  : {info['rate_limited'] or 'none'}")
    print(f"  Status        : PASS ✓")

    print("\n" + "=" * 55)
    print("  All tests complete.")
    print("=" * 55)
