"""Guard test: ``config/api_keys.json`` is a sealed secret store.

Every read/write of the assistant's secret config (Gemini / OpenRouter keys +
Instagram credentials) must go through :func:`core.secret_store.load_json` /
:func:`core.secret_store.save_json`. That module owns the DPAPI envelope; any
code that opens the file directly would either read ciphertext as if it were
plain JSON or write a plaintext secret that never gets sealed.

Because each of the ~30 call sites wraps its read in a try/except, a missed
site does **not** crash -- it silently drops the API key. This test converts
"did I miss one?" into an automated check: it fails if any module other than
``core/secret_store.py`` performs raw file I/O on the api-keys path.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The one module that is *allowed* to touch the bytes of the secret file.
SECRET_STORE = (REPO_ROOT / "core" / "secret_store.py").resolve()

# Any identifier assigned a path that ends in api_keys.json, e.g.
#   API_CONFIG_PATH = get_user_data_dir() / "config" / "api_keys.json"
VAR_ASSIGN = re.compile(
    r"^\s*([A-Za-z_]\w*)\s*(?::\s*[\w\[\], .]+)?=\s.*api_keys\.json"
)

# Raw file-IO that must never target the secret path variable.
def _raw_var_patterns(var: str) -> list[re.Pattern]:
    v = re.escape(var)
    return [
        re.compile(r"(?<![\w.])open\(\s*" + v + r"\b"),
        re.compile(r"\b" + v + r"\s*\.\s*(?:read_text|read_bytes|write_text|write_bytes)\s*\("),
    ]

# A line that both names the file and does raw I/O inline, e.g. the old
#   with open(get_user_data_dir() / "config" / "api_keys.json") as f:
IO_INLINE = re.compile(r"(?<![\w.])open\(|\.\s*(?:read_text|read_bytes|write_text|write_bytes)\s*\(|json\.(?:load|dump)s?\s*\(")


def _iter_python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rp = path.resolve()
        if rp == SECRET_STORE:
            continue
        parts = set(rp.parts)
        if {"tests", ".venv", ".venv312", "__pycache__", "node_modules"} & parts:
            continue
        # Skip the vendored Android/gradle and any dependency folders.
        yield path


def _collect_violations():
    violations = []
    for path in _iter_python_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue

        secret_vars = set()
        for line in lines:
            m = VAR_ASSIGN.match(line)
            if m:
                secret_vars.add(m.group(1))

        raw_checks = []
        for var in secret_vars:
            raw_checks.extend(_raw_var_patterns(var))

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            # Ignore pure comment lines for the inline heuristic.
            if stripped.startswith("#"):
                continue
            for chk in raw_checks:
                if chk.search(line):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: raw I/O on secret var -> {stripped}")
            # Inline: the literal filename AND a raw IO token on the same line.
            if "api_keys.json" in line and IO_INLINE.search(line):
                # load_json/save_json are the sanctioned accessors, not raw IO.
                IO_INLINE_NO_SAFE = re.compile(r"(?<![\w.])open\(|\.\s*(?:read_text|read_bytes|write_text|write_bytes)\s*\(|(?<!_)\bjson\.(?:load|dump)s?\s*\(")
                if IO_INLINE_NO_SAFE.search(line.replace("load_json", "SAFE").replace("save_json", "SAFE")):
                    violations.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: raw I/O inline with api_keys.json -> {stripped}")
    return violations


def test_api_keys_file_is_never_read_raw():
    violations = _collect_violations()
    assert not violations, (
        "config/api_keys.json must be accessed only through "
        "core.secret_store.load_json / save_json. Offenders:\n" + "\n".join(violations)
    )


def test_secret_store_roundtrip(tmp_path, monkeypatch):
    """save_json/load_json round-trip, and on Windows the bytes on disk are not
    plaintext (no secret leaks to disk)."""
    from core import secret_store

    payload = {"gemini_api_key": "SECRET-XYZ-123", "instagram_password": "hunter2"}
    target = tmp_path / "api_keys.json"

    secret_store.save_json(target, payload)
    assert secret_store.load_json(target) == payload

    raw = target.read_bytes()
    if secret_store.dpapi_available():
        assert secret_store.is_sealed(raw)
        assert b"SECRET-XYZ-123" not in raw
        assert b"hunter2" not in raw
    else:
        # Graceful degradation still returns the exact data.
        assert not secret_store.is_sealed(raw)


def test_secret_store_migrates_legacy_plaintext(tmp_path, monkeypatch):
    """A pre-existing plaintext file is re-sealed transparently on first load."""
    from core import secret_store

    if not secret_store.dpapi_available():
        pytest.skip("DPAPI unavailable on this host")

    target = tmp_path / "api_keys.json"
    target.write_text('{"gemini_api_key": "LEGACY-PLAIN"}', encoding="utf-8")

    data = secret_store.load_json(target)
    assert data["gemini_api_key"] == "LEGACY-PLAIN"
    assert secret_store.is_sealed(target.read_bytes())
    # And still readable after sealing.
    assert secret_store.load_json(target)["gemini_api_key"] == "LEGACY-PLAIN"


def test_secret_store_missing_file_returns_empty(tmp_path):
    from core import secret_store
    assert secret_store.load_json(tmp_path / "does_not_exist.json") == {}
