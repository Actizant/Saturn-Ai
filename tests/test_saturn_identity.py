"""Guard: the retired project codenames must never reappear in tracked source.

Two names were dropped when the assistant became **Saturn**. This test is the
backstop: it greps every tracked text file and fails if either spelling is
found. Both forbidden strings are assembled at runtime -- the split falls inside
the word, so this module is not itself a match -- because the only way a retired
spelling may appear is as a real, contiguous token somewhere else, which is what
we forbid.

It also pins the load-bearing identity constants so a future change cannot
silently move the on-disk data directory or resurrect the compat migration layer.
"""

import importlib
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Each split falls INSIDE the retired word, so no contiguous spelling occurs here.
RETIRED = ["Bra" + "hma", "Ul" + "tron"]


def _git_grep(pattern: str) -> str:
    return subprocess.run(
        ["git", "grep", "-n", "-I", "-i", "-E", pattern, "--", ":(exclude)*.jsonl"],
        capture_output=True, text=True,
    ).stdout


def test_no_retired_codename_anywhere_in_tracked_source():
    offenders = []
    for retired in RETIRED:
        matches = _git_grep(retired)
        if matches.strip():
            offenders.append((retired, matches.strip().splitlines()))
    assert not offenders, (
        "Retired project name(s) found in tracked files; this must be a clean "
        f"Saturn tree. Offences: {offenders}"
    )


def test_data_directory_identity_is_saturn():
    from core import user_paths

    assert user_paths._DATA_DIRNAME == "SaturnAI"
    # The legacy spelling is gone entirely -- there is no compat migration layer.
    assert not hasattr(user_paths, "get_legacy_user_data_dir")
    assert not hasattr(user_paths, "migrate_user_data_dir")


def test_renamed_and_removed_modules_no_longer_import():
    # Module names are built by splitting inside the retired word so this test
    # file never spells them contiguously.
    gone = [
        "core.state_" + "migration",
        "memory.cortex_" + "ul" + "tron",
        "memory.ul" + "tron" + "_mode",
        "memory.ul" + "tron" + "_migrate",
    ]
    for mod in gone:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"retired module {mod!r} is still importable")
