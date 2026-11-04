"""Mode control for the Saturn memory unification (Stage Three: unified).

The Saturn engine lives in ``universal-memory`` (Node). Saturn adapts to it;
it does not reimplement it. This module is the single place that decides how
far that adaptation may go.

Implemented stages
------------------
``legacy``
    Saturn behaves exactly as it did before Saturn: ``long_term.json`` only,
    lexical recall, no sidecar traffic.

``hybrid`` (default)
    ``long_term.json`` stays authoritative. Durable updates are mirrored to
    the local Saturn sidecar, native recall is merged with Saturn's lexical
    recall, and the JSON prompt block stays primary with a small, bounded,
    deduplicated native suffix.

``full`` (Stage Two)
    Native Saturn is the authoritative durable store. ``load_memory``,
    ``save_memory`` and every public memory helper read and write the sidecar
    through compatibility adapters, so Python and UI contracts are unchanged
    while ownership moves. ``long_term.json`` is demoted to a migration,
    export and rollback source and is never written as part of a normal save.
    Writes are strict: a sidecar failure raises instead of falling back to
    JSON, which is what keeps a failed write from splitting authority.

``unified`` (Stage Three)
    Native Saturn is the single store for *both* memory surfaces. The profile
    helpers keep writing native entries as in ``full``, and
    ``core/memory_cortex.py`` becomes a compatibility facade over the same
    sidecar, namespace and physical store instead of opening its own SQLite
    database. Facts, preferences, directives, hardware, network, projects,
    episodes and habits all become native entries; recall, prompt context and
    fact harvesting are served by the engine. The legacy ``memory_cortex.db``
    is copied, imported and then retained untouched as a rollback archive.

Environment
-----------
``SATURN_MEMORY_MODE`` = ``legacy`` | ``hybrid`` | ``full`` | ``unified``
Default: ``hybrid``. Aliases (``off``/``json``/``stage1``/``stage2``/...) are
accepted for operator convenience.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import threading

ENV_VAR = "SATURN_MEMORY_MODE"
DEFAULT_MODE = "hybrid"

LEGACY = "legacy"
HYBRID = "hybrid"
FULL = "full"
UNIFIED = "unified"

# Requested value -> canonical requested stage. The mapping is intentionally
# permissive: an operator typo should degrade to the safest implemented stage,
# never to "no memory" and never to an unimplemented authority change.
_ALIASES = {
    "legacy": LEGACY,
    "off": LEGACY,
    "disabled": LEGACY,
    "disable": LEGACY,
    "json": LEGACY,
    "local": LEGACY,
    "0": LEGACY,
    "false": LEGACY,
    "no": LEGACY,
    "hybrid": HYBRID,
    "stage1": HYBRID,
    "stage 1": HYBRID,
    "stage one": HYBRID,
    "on": HYBRID,
    "auto": HYBRID,
    "1": HYBRID,
    "true": HYBRID,
    "yes": HYBRID,
    "full": FULL,
    "stage2": FULL,
    "stage 2": FULL,
    "stage two": FULL,
    "unified": UNIFIED,
    "saturn": UNIFIED,
    "stage3": UNIFIED,
    "stage 3": UNIFIED,
    "stage three": UNIFIED,
}

_TARGET_STAGE = {LEGACY: 0, HYBRID: 1, FULL: 2, UNIFIED: 3}
_IMPLEMENTED_STAGE = 3
# A reserved request falls back to JSON authority, not to the newest
# implemented stage, so an unimplemented value can never move the durable
# store out of the file a user can inspect and back up.
_RESERVED_FALLBACK = HYBRID

JSON_STORE = "long_term.json"
SATURN_STORE = "saturn"

_warn_lock = threading.Lock()
_warned: set[str] = set()


@dataclass(frozen=True)
class SaturnMode:
    """Resolved mode plus the capabilities this stage actually provides."""

    requested: str
    mode: str
    stage: int
    target_stage: int
    reserved: bool
    dual_write: bool
    merged_recall: bool
    prompt_augment: bool
    authoritative_store: str
    native_reads: bool
    native_writes: bool
    json_writes: bool
    migration: bool
    # Stage Three: ``core/memory_cortex.py`` is a facade over this same
    # sidecar/namespace/store instead of owning a private SQLite database.
    cortex_native: bool = False
    warning: str = ""

    @property
    def saturn_authoritative(self) -> bool:
        """True when the sidecar, not ``long_term.json``, is the durable store."""
        return self.authoritative_store == SATURN_STORE

    def as_dict(self) -> dict:
        return {
            "requested": self.requested,
            "mode": self.mode,
            "stage": self.stage,
            "target_stage": self.target_stage,
            "reserved": self.reserved,
            "dual_write": self.dual_write,
            "merged_recall": self.merged_recall,
            "prompt_augment": self.prompt_augment,
            "authoritative_store": self.authoritative_store,
            "saturn_authoritative": self.saturn_authoritative,
            "native_reads": self.native_reads,
            "native_writes": self.native_writes,
            "json_writes": self.json_writes,
            "migration": self.migration,
            "cortex_native": self.cortex_native,
            "warning": self.warning,
        }


def _normalise(raw: str) -> str:
    return " ".join(str(raw or "").strip().lower().replace("-", " ").replace("_", " ").split())


def _warn_once(key: str, message: str) -> None:
    with _warn_lock:
        if key in _warned:
            return
        _warned.add(key)
    print(f"[SaturnMode] {message}")


# Implemented behaviour per stage. Keeping this declarative makes the
# authority change one table instead of a scatter of booleans.
_BEHAVIOUR = {
    LEGACY: dict(
        stage=0,
        dual_write=False,
        merged_recall=False,
        prompt_augment=False,
        authoritative_store=JSON_STORE,
        native_reads=False,
        native_writes=False,
        json_writes=True,
        migration=False,
    ),
    HYBRID: dict(
        stage=1,
        dual_write=True,
        merged_recall=True,
        prompt_augment=True,
        authoritative_store=JSON_STORE,
        native_reads=False,
        native_writes=True,
        json_writes=True,
        migration=False,
    ),
    FULL: dict(
        stage=2,
        dual_write=False,
        merged_recall=False,
        prompt_augment=False,
        authoritative_store=SATURN_STORE,
        native_reads=True,
        native_writes=True,
        json_writes=False,
        migration=True,
    ),
    # Stage Three: the profile store and the cortex store are one native
    # store. No JSON writes, no dual writes, no merged prompt blocks - there
    # is only one authority to read from and one place to write to.
    UNIFIED: dict(
        stage=3,
        dual_write=False,
        merged_recall=False,
        prompt_augment=False,
        authoritative_store=SATURN_STORE,
        native_reads=True,
        native_writes=True,
        json_writes=False,
        migration=True,
        cortex_native=True,
    ),
}


def _build(requested: str, canonical: str, warning: str = "") -> SaturnMode:
    target_stage = _TARGET_STAGE[canonical]
    reserved = target_stage > _IMPLEMENTED_STAGE
    if reserved:
        # Safe scaffold: report the requested stage, execute the JSON-owned
        # fallback. Never promote the sidecar for an unimplemented mode.
        warning = warning or (
            f"Saturn mode '{requested}' (stage {target_stage}) is not implemented; "
            f"running stage {_TARGET_STAGE[_RESERVED_FALLBACK]} ({_RESERVED_FALLBACK}). "
            f"long_term.json remains authoritative."
        )
        _warn_once(f"reserved:{canonical}", warning)
    elif warning:
        _warn_once(f"warning:{requested}", warning)

    implemented = _RESERVED_FALLBACK if reserved else canonical
    behaviour = _BEHAVIOUR[implemented]
    return SaturnMode(
        requested=requested,
        mode=implemented,
        target_stage=target_stage,
        reserved=reserved,
        warning=warning,
        **behaviour,
    )


def resolve_saturn_mode(raw: str | None = None) -> SaturnMode:
    """Resolve the configured mode. Reads the environment on every call so a
    long-running process (and the test suite) can change it without reloads."""
    if raw is None:
        raw = os.environ.get(ENV_VAR, "")
    text = str(raw or "").strip()
    if not text:
        return _build(DEFAULT_MODE, HYBRID)

    canonical = _ALIASES.get(_normalise(text))
    if canonical is None:
        warning = (
            f"Unknown {ENV_VAR}='{text}'; defaulting to '{DEFAULT_MODE}' "
            f"(long_term.json stays authoritative)."
        )
        return _build(DEFAULT_MODE, HYBRID, warning)
    return _build(text, canonical)


def reset_mode_warnings() -> None:
    """Test helper: allow warnings to be observed again in a fresh scenario."""
    with _warn_lock:
        _warned.clear()
