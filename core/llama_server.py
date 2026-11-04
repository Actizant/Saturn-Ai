"""
core/llama_server.py - Manages the llama-server subprocess for LFM3-Omni-2B.

Launches llama-server.exe as a background process, monitors health, provides
TTS via the /tts endpoint, and ensures clean shutdown with Saturn.

This is the local-first omni-modal brain: one process handles text, vision,
audio input, and speech output. No cloud, no internet required.
"""

import json
import logging
import os
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

logger = logging.getLogger("llama_server")

# --- Paths (relative to project root) ---
BASE_DIR = Path(__file__).resolve().parent.parent
LLAMA_CPP_DIR = BASE_DIR / "llama-cpp"
MODELS_DIR = BASE_DIR / "models" / "gguf"

# --- Server defaults ---
LLAMA_SERVER_PORT = 8080
LLAMA_SERVER_HOST = "127.0.0.1"
LLAMA_SERVER_URL = f"http://{LLAMA_SERVER_HOST}:{LLAMA_SERVER_PORT}"
HEALTH_TIMEOUT = 3.0        # seconds to wait for /health
BOOT_TIMEOUT = 30.0         # seconds to wait for model load
MODEL_FILE = MODELS_DIR / "LFM3-Omni-2B-Q4_K_M.gguf"
MMPROJ_FILE = MODELS_DIR / "mmproj-LFM3-Omni-2B-Q4_K_M.gguf"
SERVER_BIN = LLAMA_CPP_DIR / "llama-server.exe"

# --- Inference knobs -------------------------------------------------------
# Context window. Measured on this machine: the Q4 model + mmproj + an 8192
# token KV cache occupies 2394 MiB of the 4096 MiB card, which leaves roughly
# 1.7 GB of headroom before anything spills into WDDM system memory. Spill does
# not error out - it shows up as multi-second stalls - so 8192 is set to the
# largest value verified to stay resident, not simply the largest available.
# llama.cpp reserves KV per slot (n_slots = 4), so widening this scales the KV
# allocation directly.
CTX_SIZE = 8192
# Reasoning / chain-of-thought flags. IMPORTANT: these are inert on the current
# model and are kept only so the flags are already right if a reasoning-capable
# GGUF is dropped into models/gguf. LFM3-Omni-2B declares no reasoning channel
# (verified: with --reasoning on plus chat_template_kwargs enable_thinking and
# preserve_thinking, message.reasoning_content is always empty and the reply
# object carries only ['role', 'content']). The model writes its working steps
# into `content` as ordinary text instead, so the thing that actually keeps the
# trace out of the voice path is the FINAL: marker seam in
# core/reasoning_filter.speech_text, driven by core/prompt_local.txt.
# If a reasoning-capable model is swapped in, REASONING_FORMAT=deepseek moves
# the trace onto message.reasoning_content and split_reply() already routes it
# to the HUD instead of the speaker.
REASONING_ENABLED = True
REASONING_PRESERVE = True
REASONING_FORMAT = "deepseek"
# -1 lets the model think as long as it needs. Lower it to cap per-turn latency.
REASONING_BUDGET = -1

# How long to let the driver reclaim VRAM after a reap before launching. Real
# number for a real 4 GB card; named so tests can zero it without monkeypatching
# the time module out from under everything else in the process.
REAP_SETTLE_SECONDS = 1.0


def _matches_our_binary(proc) -> bool:
    """True only if `proc` was launched from the exact binary we would start.

    Matching on the resolved executable path, rather than the process name, is
    what makes reaping safe: an unrelated llama-server the user built themselves
    or runs for another project has a different path and is left alone. Paths we
    cannot read (access denied, exited between iterations) are treated as not
    ours, so uncertainty never turns into a kill.
    """
    try:
        return Path(proc.exe()).resolve() == SERVER_BIN.resolve()
    except Exception:
        return False


def reap_orphans(wait_timeout: float = 5.0) -> list:
    """Terminate llama-server processes left behind by a previous crashed run.

    Saturn starts the server as a child process and stops it on a clean exit, so
    anything already holding our binary at boot belongs to a run that died
    without reaching stop(). One 4 GB card cannot host two loaded models: the
    leftovers keep VRAM and ~1.2 GB of RAM each, and the next launch silently
    degrades or fails to bind the port.

    Never uses os.kill(pid, 0) as a liveness probe - on Windows that raises and
    can terminate the target. psutil owns the process handle, and terminate()
    gets a grace window before kill() is used.
    """
    try:
        import psutil
    except ImportError:
        logger.warning("[llama-server] psutil unavailable - skipping orphan reap")
        return []

    reaped = []
    me = psutil.Process(os.getpid())
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.pid == me.pid or not _matches_our_binary(proc):
                continue
            logger.warning("[llama-server] reaping orphan pid=%d (%s)",
                           proc.pid, proc.info.get("name"))
            proc.terminate()
            try:
                proc.wait(timeout=wait_timeout)
            except psutil.TimeoutExpired:
                logger.warning("[llama-server] orphan pid=%d ignored SIGTERM - killing", proc.pid)
                proc.kill()
                proc.wait(timeout=wait_timeout)
            reaped.append(proc.pid)
        except psutil.NoSuchProcess:
            continue
        except Exception as exc:
            # An orphan we cannot reap is worth a warning, not a crashed boot.
            logger.warning("[llama-server] could not reap pid=%s: %s",
                           proc.info.get("pid"), exc)
    if reaped:
        # Give the driver a moment to actually reclaim the allocations.
        time.sleep(REAP_SETTLE_SECONDS)
        logger.info("[llama-server] reaped %d orphan process(es): %s", len(reaped), reaped)
    return reaped


def is_installed() -> bool:
    """Check that the server binary and model files are present on disk."""
    return (
        SERVER_BIN.exists()
        and MODEL_FILE.exists()
        and MMPROJ_FILE.exists()
    )


class LlamaServerProcess:
    """Manages the llama-server.exe subprocess lifecycle."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._url = LLAMA_SERVER_URL

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> bool:
        """Launch llama-server if not already running. Returns True on success."""
        with self._lock:
            if self.running:
                logger.info("[llama-server] already running (pid=%d)", self._process.pid)
                return True
            if not is_installed():
                logger.warning("[llama-server] not installed – missing binary or model files")
                return False

            # Anything from our binary still running here is a corpse from a
            # previous boot; it would hold the port and the VRAM we need.
            reap_orphans()

            cmd = [
                str(SERVER_BIN),
                "-m", str(MODEL_FILE),
                "--mmproj", str(MMPROJ_FILE),
                "-ngl", "99",
                "--temp", "0",
                "-c", str(CTX_SIZE),
                "--reasoning", "on" if REASONING_ENABLED else "off",
                "--reasoning-format", REASONING_FORMAT,
                "--reasoning-budget", str(REASONING_BUDGET),
                "--reasoning-preserve" if REASONING_PRESERVE else "--no-reasoning-preserve",
                "--host", LLAMA_SERVER_HOST,
                "--port", str(LLAMA_SERVER_PORT),
            ]
            logger.info("[llama-server] launching: %s", " ".join(cmd))
            try:
                self._process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as e:
                logger.error("[llama-server] failed to launch: %s", e)
                return False

            # Wait for health
            return self._wait_healthy()

    def _wait_healthy(self) -> bool:
        """Poll /health until the model is loaded (or timeout)."""
        deadline = time.time() + BOOT_TIMEOUT
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                logger.error("[llama-server] process exited early (rc=%d)", self._process.returncode)
                self._process = None
                return False
            try:
                req = urllib.request.Request(f"{self._url}/health", method="GET")
                with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
                    if resp.status == 200:
                        logger.info("[llama-server] healthy (pid=%d)", self._process.pid)
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        logger.error("[llama-server] timed out waiting for health (%.0fs)", BOOT_TIMEOUT)
        return False

    def stop(self) -> None:
        """Terminate the server process gracefully."""
        with self._lock:
            if self._process and self._process.poll() is None:
                logger.info("[llama-server] stopping pid=%d", self._process.pid)
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

    def health(self) -> dict:
        """Query /health and /props for a status snapshot."""
        result = {"running": self.running, "url": self._url}
        if not self.running:
            return result
        try:
            req = urllib.request.Request(f"{self._url}/health", method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                result["healthy"] = resp.status == 200
        except Exception as e:
            result["healthy"] = False
            result["error"] = str(e)
        return result


# --- Module-level singleton ---
_server = LlamaServerProcess()


def ensure_running() -> bool:
    """Start llama-server if needed. Called at Saturn boot."""
    return _server.start()


def stop() -> None:
    """Stop llama-server. Called at Saturn shutdown."""
    _server.stop()


def status() -> dict:
    """Return current server status for UI/diagnostics."""
    return _server.health()


def synthesize_speech(text: str, voice: str = "female", lang: str = "en") -> Optional[bytes]:
    """Generate TTS audio via the /tts endpoint. Returns WAV bytes or None."""
    # Probe health rather than relying on internal process tracking:
    # the server may have been adopted or started externally.
    try:
        req = urllib.request.Request(f"{_server._url}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.status != 200:
                return None
    except Exception:
        return None
    payload = json.dumps({
        "prompt": text,
        "tts_lang": lang,
        "tts_voice": voice,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{_server._url}/tts",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            if resp.status == 200:
                return resp.read()
    except Exception as e:
        logger.error("[llama-server] TTS failed: %s", e)
    return None
