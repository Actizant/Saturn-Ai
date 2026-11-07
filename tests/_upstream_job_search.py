"""
tests/_upstream_job_search.py
=============================
Load the *original* job-search tools from the archive the port was made from,
so the tests can diff Saturn's behaviour against the real thing.

Why this exists: P0's gate was proven by running the upstream implementation
and Saturn's side by side over an adversarial corpus. That proof is the only
reason to believe a rewrite kept the security semantics, and it does not work
if the test trusts a transcription of the original. So this module extracts the
genuine ``tools/*.py`` from the owner's downloaded archive into a temp
directory and imports them as their own module objects.

The archive is the developer's local copy, not a repository dependency. When it
is absent every test that uses it skips rather than fails: a missing provenance
file is not a behavioural regression, and a suite that goes red on a machine
that simply does not have the zip teaches nothing.

Naming note -- upstream ``tools/robots_check.py`` and ``tools/fetch_page.py``
are Saturn ``core/robots_consent.py`` and ``core/page_fetch.py``. The names
deliberately differ: upstream's fetcher does ``sys.path.insert`` and then a
plain ``import robots_check``, so if Saturn's module shared the name a test
could end up comparing Saturn's consent gate against upstream's fetcher, an
accidentally mixed pair that would agree with itself and prove nothing.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import zipfile
from pathlib import Path

ARCHIVE_CANDIDATES = (
    Path.home() / "Downloads" / "ai-job-search-master.zip",
    Path(os.environ.get("SATURN_JOB_SEARCH_ZIP", "")),
)

# Upstream's own filename -> the name it is imported under in these tests.
UPSTREAM_FILES = {
    "page_decode.py": "upstream_page_decode",
    "robots_check.py": "upstream_robots_check",
    "fetch_page.py": "upstream_fetch_page",
    "job_key.py": "upstream_job_key",
    "posting_guard.py": "upstream_posting_guard",
}

# Filled by archive_root() before any load() of a module with cross-imports.
UPSTREAM_ROOT: Path | None = None

_loaded: dict[str, object] = {}


def archive_path() -> Path | None:
    """The downloaded upstream archive, or None when it is not on this machine."""
    for candidate in ARCHIVE_CANDIDATES:
        if candidate and candidate.exists():
            return candidate
    return None


def archive_root() -> Path | None:
    """Extract upstream ``tools/`` into temp, or return the directory if present.

    Extraction is idempotent and rewrites every file, so a stale copy from an
    earlier run can never be diffed against by accident.
    """
    global UPSTREAM_ROOT
    archive = archive_path()
    if archive is None:
        return None
    tools = (Path(os.environ.get("TEMP", os.environ.get("TMP", ".")))
             / "saturn_upstream_job_search" / "tools")
    with zipfile.ZipFile(archive) as zf:
        prefix = "ai-job-search-master/tools/"
        members = [n for n in zf.namelist()
                   if n.startswith(prefix) and n.endswith(".py")]
        tools.mkdir(parents=True, exist_ok=True)
        for member in members:
            (tools / Path(member).name).write_bytes(zf.read(member))
    UPSTREAM_ROOT = tools
    return tools


def load(upstream_name: str):
    """Import ``tools/<upstream_name>`` as a private module, once per process.

    The extracted directory is put on ``sys.path`` for the duration of the
    import because upstream resolves its own siblings with
    ``sys.path.insert`` plus a plain ``import``; leaving it on the path
    afterwards would let it shadow repository modules.
    """
    alias = UPSTREAM_FILES[upstream_name]
    if alias in _loaded:
        return _loaded[alias]
    if UPSTREAM_ROOT is None:
        raise RuntimeError("call archive_root() before load()")
    inserted = str(UPSTREAM_ROOT) not in sys.path
    if inserted:
        sys.path.insert(0, str(UPSTREAM_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(alias, UPSTREAM_ROOT / upstream_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(str(UPSTREAM_ROOT))
        sys.modules.pop(alias, None)
    _loaded[alias] = module
    return module


def load_all():
    """The four modules a P2 test needs, in dependency order.

    Returns ``(modules, None)`` on success or ``(None, reason)`` when the
    archive is missing, so the caller can ``pytest.skip`` with a useful message
    instead of importing a half-set.
    """
    root = archive_root()
    if root is None:
        return None, ("upstream archive not found: expected the downloaded "
                      f"ai-job-search-master.zip at {[str(c) for c in ARCHIVE_CANDIDATES]}")
    # page_decode and robots_check first: fetch_page imports both at module
    # level, so they must already be resolvable when its body executes.
    return {
        "page_decode": load("page_decode.py"),
        "robots_check": load("robots_check.py"),
        "posting_guard": load("posting_guard.py"),
        "fetch_page": load("fetch_page.py"),
        "job_key": load("job_key.py"),
    }, None
