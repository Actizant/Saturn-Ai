"""
core/theme_fonts.py
====================
The Saturn display typeface, bundled and offline.

Why this module exists
----------------------
The cyberpunk design (saturn_package) sets its headings, wordmark, clock and
orb state names in Unbounded, a Google Font that the shell does not ship with -
and Saturn runs offline, so a ``<link>`` to fonts.googleapis.com is not an
option. Unbounded therefore has to live in ``assets/fonts`` and be registered
with Qt at startup, exactly like every other asset.

Body and mono text use Segoe UI and Consolas, both of which ship with Windows
and already match the design, so they need no bundling and no download. This
module is only about upgrading the display family.

Registration is deliberately tolerant. If the folder is empty (a fresh clone,
or the user has not run ``assets/fonts/fetch_saturn_fonts.ps1`` yet), or a file
is corrupt, or there is no ``QApplication`` to attach fonts to, this returns
nothing and the display token keeps the system face it started with. A missing
font must never be a crash, and it must never be a dialog either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List

# Token attribute on ui.C -> the family the design wants under it. Only the
# display face is bundled; body (Segoe UI) and mono (Consolas) are system faces.
SPEC: Dict[str, str] = {
    "F_DISPLAY": "Unbounded",
}

# QFont families are matched by name, and the variable-font builds report a
# slightly different family than the static ones. Accept either.
_ALIASES: Dict[str, tuple] = {
    "Unbounded": ("Unbounded", "Unbounded Variable", "Unbounded[wght]"),
}

# Qt's font database reads these; .woff2 is a web format and is silently
# ignored on purpose so a mistaken download does not look like a success.
SUPPORTED_SUFFIXES = {".ttf", ".otf", ".ttc"}

FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def candidate_files(directory: Path) -> List[Path]:
    """Every font file we would try to register, in a stable order."""
    if not directory.is_dir():
        return []
    return sorted(
        p for p in directory.glob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def _added_families(paths: Iterable[Path]) -> List[str]:
    """Register each file with Qt and collect the family names it reported."""
    families: List[str] = []
    try:
        from PyQt6.QtGui import QFontDatabase
    except Exception:
        return families
    for path in paths:
        try:
            ids = QFontDatabase.addApplicationFont(str(path))
            if ids is None:
                continue
            if isinstance(ids, int):  # -1 on failure, older bindings
                ids = [] if ids < 0 else [ids]
            for font_id in ids:
                families.extend(QFontDatabase.applicationFontFamilies(font_id))
        except Exception:
            # One unreadable file must not cost us the other five.
            continue
    return families


def install(tokens: Any, fonts_dir: Path | str | None = None) -> Dict[str, str]:
    """Point ``tokens`` at the bundled Aurum faces where they are available.

    ``tokens`` is ``ui.C`` in the app (any object with F_BODY/F_DISPLAY/F_MONO
    attributes works in a test). Only attributes the object already has are
    touched, so a typo in SPEC cannot invent state on the palette class.

    Returns the ``{attribute: family}`` pairs that actually took effect.
    """
    directory = Path(fonts_dir) if fonts_dir else FONTS_DIR
    available = {f.strip() for f in _added_families(candidate_files(directory))}
    applied: Dict[str, str] = {}
    for attr, wanted in SPEC.items():
        if not hasattr(tokens, attr):
            continue
        for name in _ALIASES.get(wanted, (wanted,)):
            if name in available:
                setattr(tokens, attr, name)
                applied[attr] = name
                break
    return applied


def missing_families(fonts_dir: Path | str | None = None) -> List[str]:
    """Which design families are not in the folder yet, for the setup screen."""
    directory = Path(fonts_dir) if fonts_dir else FONTS_DIR
    present = {p.stem.lower() for p in candidate_files(directory)}
    wanted = {"Unbounded": "unbounded"}
    return [
        family for family, key in wanted.items()
        if not any(key in name for name in present)
    ]
