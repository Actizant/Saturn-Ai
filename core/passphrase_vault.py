"""The fail-safe credential for the security perimeter.

WHY THIS EXISTS
    If the camera dies, or the frame comes back black because the lens is
    covered, the perimeter must never lock the owner out on the strength of a
    fault it cannot distinguish from a stranger. So there is a way to say "it is
    me" without a camera: a local passphrase.

    That makes the passphrase the weakest link in the whole subsystem, so it is
    treated as a secret from the first line: salted PBKDF2, compared in constant
    time, never logged, never a tool parameter, never in a printed dict.

WHERE IT LIVES
    Two flat keys in the existing settings file - ``security_passphrase_hash``
    and ``security_bypass_until`` - rather than inside the perimeter's own dict.
    ``memory.config_manager.save_settings`` merges at the top level, so two
    writers touching different keys cannot erase each other's work. Nesting the
    hash inside the perimeter dict would have created exactly that bug the first
    time either side saved.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

# OWASP's current floor for PBKDF2-HMAC-SHA256 is 600k; 200k is what a 12th-gen
# i5 turns around in roughly a tenth of a second, and this prompt is shown to a
# person at a keyboard, not to a server under load. Raising it is a one-line
# change if the machine is ever fast enough for the larger number.
ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 200_000
SALT_BYTES = 16

# Short enough to be typeable, long enough that guessing is pointless. The lock
# is a deterrent against an opportunist, not against a targeted attack.
MIN_LENGTH = 8

HASH_KEY = "security_passphrase_hash"
BYPASS_KEY = "security_bypass_until"

DEFAULT_BYPASS_HOURS = 24.0
MAX_BYPASS_HOURS = 24.0 * 7.0

_lock = threading.Lock()


# ── Settings seam (kept narrow so tests can drive it without the app) ────────

def _get(key, default=None):
    from memory.config_manager import get_setting

    return get_setting(key, default)


def _set(key, value) -> None:
    from memory.config_manager import set_setting

    set_setting(key, value)


# ── Hashing ─────────────────────────────────────────────────────────────────

def make_hash(passphrase: str, iterations: int = ITERATIONS, salt: bytes | None = None) -> str:
    """Encode a passphrase as ``pbkdf2_sha256$iterations$salt$hash``.

    The iteration count travels with the hash, so raising the work factor later
    does not invalidate a passphrase that was set before.
    """
    text = str(passphrase or "")
    if not text:
        raise ValueError("the passphrase is empty")
    salt_bytes = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt_bytes, int(iterations))
    return f"{ALGORITHM}${int(iterations)}${salt_bytes.hex()}${digest.hex()}"


def verify(passphrase: str, encoded: str) -> bool:
    """Constant-time check of a passphrase against an encoded hash."""
    if not encoded or not isinstance(encoded, str):
        return False
    parts = encoded.split("$")
    if len(parts) != 4 or parts[0] != ALGORITHM:
        return False
    try:
        iterations = int(parts[1])
        salt = bytes.fromhex(parts[2])
        expected = bytes.fromhex(parts[3])
    except (TypeError, ValueError):
        return False
    if iterations <= 0 or not salt or not expected:
        return False
    try:
        actual = hashlib.pbkdf2_hmac("sha256", str(passphrase or "").encode("utf-8"),
                                     salt, iterations)
    except Exception:
        return False
    # compare_digest, not ==: the comparison must not leak how much matched.
    return hmac.compare_digest(actual, expected)


# ── Stored passphrase ───────────────────────────────────────────────────────

def stored_hash() -> str:
    try:
        return str(_get(HASH_KEY, "") or "")
    except Exception:
        return ""


def configured() -> bool:
    """True once a passphrase has been set. Says nothing about its strength."""
    return bool(stored_hash())


def set_passphrase(passphrase: str) -> dict:
    """Store a new passphrase. Returns a plain dict, never echoing the secret."""
    text = str(passphrase or "")
    if len(text) < MIN_LENGTH:
        return {"ok": False, "error": "TOO_SHORT",
                "message": f"The passphrase needs at least {MIN_LENGTH} characters."}
    try:
        encoded = make_hash(text)
    except Exception as exc:
        return {"ok": False, "error": "HASH_FAILED", "message": str(exc)}
    with _lock:
        try:
            _set(HASH_KEY, encoded)
        except Exception as exc:
            return {"ok": False, "error": "SAVE_FAILED", "message": str(exc)}
    return {"ok": True, "message": "Passphrase set."}


def check_passphrase(passphrase: str) -> bool:
    """True only for the passphrase that is currently stored."""
    with _lock:
        encoded = stored_hash()
    if not encoded:
        return False
    return verify(passphrase, encoded)


def clear_passphrase() -> dict:
    """Forget the passphrase. The perimeter loses its fail-safe and must disarm."""
    with _lock:
        try:
            _set(HASH_KEY, "")
            _set(BYPASS_KEY, 0)
        except Exception as exc:
            return {"ok": False, "error": "SAVE_FAILED", "message": str(exc)}
    return {"ok": True, "message": "Passphrase removed and bypass cleared."}


# ── Bypass window ───────────────────────────────────────────────────────────

def grant_bypass(hours: float = DEFAULT_BYPASS_HOURS, reason: str = "") -> float:
    """Stop asking until the wall clock passes the given number of hours.

    Wall-clock time, not a monotonic counter: the window has to survive a
    restart, and the threat model does not include an attacker who can reset the
    system clock without also being able to log in.
    """
    span = max(0.0, min(float(hours), MAX_BYPASS_HOURS))
    until = time.time() + span * 3600.0
    with _lock:
        try:
            _set(BYPASS_KEY, until)
        except Exception:
            return 0.0
    return until


def bypass_until() -> float:
    try:
        return float(_get(BYPASS_KEY, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def bypass_remaining() -> float:
    """Seconds left in the bypass window, 0 when none is active."""
    return max(0.0, bypass_until() - time.time())


def bypass_active() -> bool:
    """True only while a window is open that a passphrase actually earned.

    A leftover timestamp with no stored passphrase must not silence the gate:
    there is nothing to bypass *from*, and a stale value here would quietly
    disable the perimeter while ``status`` reported it as protected.
    """
    if not configured():
        return False
    return bypass_remaining() > 0.0


def clear_bypass() -> None:
    with _lock:
        try:
            _set(BYPASS_KEY, 0)
        except Exception:
            pass


def status() -> dict:
    """What can be said out loud without leaking anything about the secret."""
    active = bypass_active()
    remaining = bypass_remaining() if active else 0.0
    return {
        "configured": configured(),
        "bypass_active": active,
        "bypass_minutes": int(remaining // 60),
        "min_length": MIN_LENGTH,
        "algorithm": ALGORITHM,
        "iterations": ITERATIONS,
    }
