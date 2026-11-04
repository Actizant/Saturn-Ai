"""core/secret_store.py -- at-rest encryption for the assistant's secret config.

The Gemini/OpenRouter/Anthropic keys and the Instagram credentials live in
``config/api_keys.json``. On Windows that file is sealed with the Data
Protection API (DPAPI, ``crypt32``), keyed to the **current user account**: the
bytes on disk are opaque ciphertext to anyone who is not logged in as you, and
no passphrase prompt ever interrupts launch.

Every reader and writer of that file goes through :func:`load_json` /
:func:`save_json` here instead of opening it directly, so the protection cannot
be skipped and the format stays consistent across the main app and the
Instagram MCP subprocess (which run as the same user and therefore share the
same DPAPI key).

Design notes:
  * DPAPI user-scope is used deliberately (not ``CRYPTPROTECT_LOCAL_MACHINE``):
    a machine-scope blob could be read by *any* local account, which is weaker.
  * A fixed ``_ENTROPY`` label is folded in as DPAPI "optional entropy". It adds
    no secrecy (it ships in this file) but means another DPAPI caller cannot
    decrypt our blob unless it replays the same label.
  * Graceful degradation is a product rule: where DPAPI is unavailable (a
    non-Windows host, or a hard ``crypt32`` failure) the payload is written as
    plain JSON rather than losing the keys or crashing. The env-var kill switch
    ``SATURN_SECRETS_PLAINTEXT=1`` forces plain JSON on every platform.
  * Legacy plaintext is migrated transparently: the first time a plain JSON
    file is loaded on Windows it is re-sealed, so existing installs are
    protected with no user action.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict

# Magic tag so the loader tells our envelope apart from a legacy plaintext file
# without any parsing guesswork.
MAGIC = "SATURN-SECRET-v1"

# Non-secret app label used as DPAPI optional entropy (see module docstring).
_ENTROPY = b"Saturn.secret_store.v1"

# CRYPTPROTECT_PROMPTSTRUCT is all-zero -> no UI; this flag forbids prompts
# outright so background threads never raise a security dialog.
_UI_FORBIDDEN = 0x00000001

_PLAINTEXT_ENV = "SATURN_SECRETS_PLAINTEXT"


def dpapi_available() -> bool:
    """True when we can seal secrets with Windows DPAPI for the current user."""
    if os.environ.get(_PLAINTEXT_ENV) == "1":
        return False
    return sys.platform == "win32" and hasattr(ctypes.windll, "crypt32")


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_byte)),
    ]


def _make_blob(data: bytes) -> "_DataBlob":
    # from_buffer_copy yields an exact-length array (no trailing NUL semantics),
    # and the local keeps the backing memory alive for the call.
    buf = (ctypes.c_byte * max(len(data), 1)).from_buffer_copy(data or b"\x00")
    blob = _DataBlob()
    blob.cbData = len(data)
    blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))
    # Attach the buffer so it is not garbage-collected while the API reads it.
    blob._keepalive = buf  # type: ignore[attr-defined]
    return blob


def _protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    blob_in = _make_blob(data)
    entropy = _make_blob(_ENTROPY)
    blob_out = _DataBlob()
    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, ctypes.byref(entropy),
        None, None, _UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _unprotect(blob: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    blob_in = _make_blob(blob)
    entropy = _make_blob(_ENTROPY)
    blob_out = _DataBlob()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, ctypes.byref(entropy),
        None, None, _UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise OSError("CryptUnprotectData failed (wrong user or corrupted blob)")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _wrap_envelope(blob: bytes) -> bytes:
    body = base64.b64encode(blob).decode("ascii")
    return (MAGIC + "\n" + body + "\n").encode("utf-8")


def _unwrap_envelope(raw: bytes) -> bytes:
    text = raw.decode("utf-8").lstrip("\ufeff")
    body = text[len(MAGIC):].strip()
    return base64.b64decode(body)


def is_sealed(raw: bytes) -> bool:
    return raw.lstrip(b"\xef\xbb\xbf").startswith(MAGIC.encode("ascii"))


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def save_json(path: os.PathLike[str] | str, data: Dict[str, Any]) -> None:
    """Persist ``data`` to ``path``, sealed with DPAPI whenever possible."""
    p = Path(path)
    payload = json.dumps(data, indent=4).encode("utf-8")
    if dpapi_available():
        try:
            _atomic_write(p, _wrap_envelope(_protect(payload)))
            return
        except Exception:
            pass  # fall through: never lose the keys for a crypto hiccup
    _atomic_write(p, payload)


def load_json(path: os.PathLike[str] | str) -> Dict[str, Any]:
    """Return the dict stored at ``path``.

    Missing file -> ``{}``. A sealed file is decrypted; a legacy plain-JSON file
    is read and, on Windows, transparently re-sealed so the next call is already
    protected. Raises on genuinely corrupt content so callers keep their own
    existing try/except behaviour rather than silently wiping secrets.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = p.read_bytes()
    if is_sealed(raw):
        plain = _unprotect(_unwrap_envelope(raw))
        data = json.loads(plain.decode("utf-8"))
    else:
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            data = {}
        if dpapi_available():
            try:
                save_json(p, data)  # migrate plaintext -> sealed, silently
            except Exception:
                pass
    return data if isinstance(data, dict) else {}
