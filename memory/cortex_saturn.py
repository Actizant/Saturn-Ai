"""Stage Three: the memory cortex as a client of the shared Saturn store.

``core/memory_cortex.py`` used to own a private SQLite database
(``config/memory_cortex.db``). In ``unified`` mode that database is retired and
this module supplies the same capabilities through the *same* sidecar,
namespace and physical store the profile memory helpers already use.

What lives here
---------------
* The mapping from cortex concepts (facts, preferences, directives, hardware,
  network, projects, episodes, importance, habits) to native entries.
* :class:`CortexNativeStore` - the read/write port the facade calls. It only
  translates; dedupe, merging, contradiction handling, ranking and extraction
  stay in the Node engine.
* A copy-first, idempotent importer for the legacy SQLite database. The source
  file is copied, the *copy* is read, entries are imported with stable ids, and
  the source is left untouched as a rollback archive.

Invariants
----------
1. Cortex entries are recognisable by ``meta.cortex`` and never carry
   ``meta.saturn_category`` / ``meta.saturn_key``. The profile reconciler only
   manages entries with those keys, so a profile save can never delete a
   cortex fact and a cortex write can never look like a profile entry.
2. Cortex predicates are namespaced (``cortex.<category>.<slug>``,
   ``cortex.habit.<slug>``). The engine's contradiction policy versions entries
   by (kind, subject, predicate), so a cortex fact can never supersede a
   profile fact that happens to share a key.
3. Reads degrade, writes report. A recall against a stopped sidecar returns an
   empty list; ``remember`` returns ``-1``. No read path starts Node.
4. Habit state lives in one active entry per trigger: the engine's
   ``seen``/``at`` fields become frequency and last-triggered, so repeated
   occurrences merge instead of accumulating duplicate rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from memory import universal_bridge as ub
from memory.saturn_mode import resolve_saturn_mode

CORTEX_SRC = "saturn-cortex"
CORTEX_FLAG = "cortex"
# Every cortex predicate starts with this. The engine's contradiction policy
# compares predicates verbatim, so the prefix is what keeps a cortex fact from
# ever superseding a profile fact that happens to name the same slot.
CORTEX_PREDICATE_PREFIX = "cortex"

# The sidecar names its store from this variable, so every cortex payload names
# it too. Without it the engine's importer falls back to its own ``default``
# namespace, which is a different store than the one recall and the profile
# adapters read - migrated facts would be present but invisible.
NAMESPACE_ENV = "SATURN_MEMORY_NAMESPACE"
SATURN_NAMESPACE_ENV = "SATURN_MEMORY_NAMESPACE"
DEFAULT_NAMESPACE = "saturn"

MIGRATION_VERSION = 1
MARKER_NAME = "cortex_migration.json"
STABLE_ID_PREFIX = "brc_"

_MIGRATE_ENV = "SATURN_MEMORY_CORTEX_MIGRATE"
_DISABLED_VALUES = ("off", "0", "false", "no", "disabled", "skip")


class MarkerWriteError(RuntimeError):
    """The cortex migration marker could not be persisted.

    The marker is the only evidence that the legacy rows were copied. A
    swallowed write failure reports success, caches it for the process, and
    then re-imports the same database after a restart - or claims a destination
    the rows are not in. The importer fails closed instead: no success, no
    cache, writes stay gated, and the retry skips what is already there.
    """

# Cortex category -> engine kind. ``episode`` keeps its own kind so episodic
# recall decays differently; everything structural becomes a note unless the
# engine has a better-fitting kind.
CATEGORY_KIND = {
    "identity": "fact",
    "preference": "preference",
    "directive": "note",
    "hardware": "note",
    "network": "note",
    "project": "note",
    "episode": "episode",
    "fact": "fact",
}

CATEGORY_IMPORTANCE = {
    "identity": 0.9,
    "preference": 0.8,
    "directive": 0.85,
    "hardware": 0.7,
    "network": 0.7,
    "project": 0.7,
    "episode": 0.5,
    "fact": 0.6,
}

HABIT_IMPORTANCE = 0.55

# Small local stopword list. Deliberately separate from ``core.memory_cortex``
# so this module never imports the facade (which imports this one).
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "are",
    "was", "were", "you", "your", "our", "his", "her", "its", "their", "into",
    "about", "user", "users", "prefer", "prefers", "preferred", "like", "likes",
    "love", "loves", "want", "wants", "using", "use", "uses", "will", "would",
    "can", "could", "should", "when", "then", "than", "there", "here", "what",
    "which", "who", "whom", "how", "why", "all", "any", "some", "more", "most",
    "not", "but", "also", "very", "just", "now", "still", "been", "being",
}

_migrate_lock = threading.Lock()
_migrated: dict | None = None
# Destination-bound identity of the cached report (version + source digest +
# destination store id + namespace). A cache hit is only valid while all four
# still match, so a new SATURN_MEMORY_FILE or namespace migrates into the new
# store instead of reporting "already imported".
_migrated_key: dict | None = None
# Bumped by every successful import. The importer writes straight to the
# engine, so a snapshot a store cached before it ran would hide the migrated
# rows until the cache TTL expired (or forever, if the cache keeps refreshing).
_generation = 0


# ── Small helpers ────────────────────────────────────────────────────────────

def _clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return low


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _slug(text: Any, limit: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").casefold()).strip("-")
    return slug[:limit].strip("-") or "entry"


# How much readable text a content-derived key keeps before its digest. The
# digest covers the whole statement, so the prefix is decoration - and it is
# short enough that ``cortex.<category>.<key>`` still fits the engine's
# 80-character predicate column with room to spare.
KEY_LABEL_CHARS = 32


def content_key(text: Any, label_chars: int = KEY_LABEL_CHARS) -> str:
    """Bounded, collision-resistant identity for a whole statement.

    ``_slug`` truncates at sixty characters, so two long facts that share an
    opening sentence produce the *same* slug - and a key is used as the
    engine's versioning predicate, its dedupe slot and its reconciliation
    label. Truncation therefore silently merges two different memories, or
    lets a restatement supersede an unrelated one.

    The key is a readable prefix plus a digest of the complete normalized
    text: bounded in length, lossless in identity, and stable across restarts.
    Nothing that needs a stable handle (``cortex_key``, predicates, habit
    reconciliation) may use a truncated slug alone.
    """
    normalized = " ".join(str(text or "").split()).strip()
    digest = hashlib.sha1(normalized.casefold().encode("utf-8")).hexdigest()[:16]
    return f"{_slug(normalized, label_chars)}-{digest}"


def namespace() -> str:
    """The namespace the sidecar itself is configured with."""
    value = str(
        os.environ.get(SATURN_NAMESPACE_ENV)
        or os.environ.get(NAMESPACE_ENV)
        or DEFAULT_NAMESPACE
        or ""
    ).strip()
    return value or DEFAULT_NAMESPACE


def resolve_namespace(explicit: Any = None) -> str:
    """Namespace of the store a caller is actually writing into.

    The process environment is only the last-resort default for helpers invoked
    without a bridge. Every migration identity and every payload the native
    store writes passes the namespace of the *bridge* it is talking to, so two
    bridges served from one process can never share ids.
    """
    value = str(explicit or "").strip()
    return value or namespace()


def tokens(text: Any) -> set:
    """Significant tokens, used only for equivalence checks (never ranking)."""
    return {
        token
        for token in re.findall(r"[a-z0-9]+", str(text or "").casefold())
        if len(token) > 2 and token not in _STOPWORDS
    }


def _iso_from_epoch(value: Any) -> Optional[str]:
    epoch = _as_float(value, 0.0)
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_from_iso(value: Any, default: Optional[float] = None) -> float:
    text = str(value or "").strip()
    if not text:
        return time.time() if default is None else default
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return time.time() if default is None else default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def cortex_int(native_id: Any) -> int:
    """Stable integer id for a native entry.

    ``MemoryCortex`` has always returned ``int`` ids. Hashing the native id
    keeps that contract without a local id table, and it is stable across
    restarts and machines.
    """
    digest = hashlib.sha1(str(native_id or "").encode("utf-8")).hexdigest()[:15]
    try:
        return int(digest, 16)
    except ValueError:  # pragma: no cover - defensive
        return 0


def semantic_slot(category: str, subject: str, predicate: str) -> str:
    """Cortex-prefixed predicate for an extracted (subject, predicate) pair.

    The engine versions an entry by ``(kind, namespace, subject, predicate)``
    and supersedes the active row only when the *value* changes. Keying a
    statement on the extracted slot rather than a content hash is therefore
    what makes a restated preference - "favourite colour is teal" then "... is
    blue" - supersede the earlier value instead of leaving both active.
    """
    cat = str(category or "fact").strip().lower() or "fact"
    subj = _slug(subject, 40) or "user"
    pred = _slug(predicate, 60) or "entry"
    return f"{CORTEX_PREDICATE_PREFIX}.{cat}.{subj}.{pred}"


def migrated_memory_id(
    category: str,
    content: str,
    created_at: Any,
    ordinal: int = 0,
    store_namespace: Any = None,
) -> str:
    """Deterministic, lossless id for a migrated SQLite memory row.

    The digest covers the whole statement, the *destination* namespace and an
    occurrence ordinal - never a 60-character slug, and never the process
    environment. Two rows that share a long prefix, or two identical rows
    recorded at the same instant, therefore keep separate ids instead of
    colliding into one native entry. The namespace is part of the id because
    the engine keys entries by id across namespaces: a shared id would make the
    second namespace's import look like a duplicate and silently drop the row.
    """
    stamp = f"{_as_float(created_at, 0.0):.3f}"
    material = (
        f"memory\x1f{resolve_namespace(store_namespace)}"
        f"\x1f{category}\x1f{content}\x1f{stamp}"
    )
    if ordinal:
        material += f"\x1f#{int(ordinal)}"
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]
    return f"{STABLE_ID_PREFIX}{digest}"


def habit_identity(trigger: str, store_namespace: Any = None) -> Tuple[str, str]:
    """Stable logical identity of a habit, independent of occurrence count.

    ``key`` is the lossless identity used for reconciliation - a bounded
    readable label plus a digest of the *whole* trigger - so two triggers that
    share their first sixty characters stay two habits, and the same trigger in
    two namespaces stays two habits. The id is derived the same way.
    """
    key = content_key(trigger)
    trigger_text = " ".join(str(trigger or "").split()).strip().lower()
    digest = hashlib.sha1(
        f"habit\x1f{resolve_namespace(store_namespace)}\x1f{trigger_text}".encode("utf-8")
    ).hexdigest()[:24]
    return key, f"{STABLE_ID_PREFIX}{digest}"


# ── Native entry payloads ────────────────────────────────────────────────────

def memory_payload(
    content: str,
    category: str = "fact",
    importance: float = 1.0,
    *,
    at: Optional[str] = None,
    created_at: Optional[float] = None,
    keywords: Optional[Iterable[str]] = None,
    extra_meta: Optional[dict] = None,
    subject: Optional[str] = None,
    predicate: Optional[str] = None,
    value: Optional[str] = None,
    store_namespace: Any = None,
) -> dict:
    """Map one cortex memory onto a native ``remember`` body.

    ``predicate`` is what the engine versions on. Left unset it is derived from
    the whole statement, so re-stating the same fact dedupes into a merge
    (``seen`` +1, ``at`` refreshed). Callers that know the *slot* a statement
    is about - a preference, an extracted subject/predicate pair - pass it in
    so a changed value supersedes the old row instead of living beside it.
    """
    body = " ".join(str(content or "").split()).strip()
    category = str(category or "fact").strip().lower()
    if category not in CATEGORY_KIND:
        category = "fact"
    impression = _clamp(
        importance if importance is not None else CATEGORY_IMPORTANCE[category],
        0.05,
        1.0,
    )
    created = float(created_at) if created_at else time.time()
    created_iso = at or _iso_from_epoch(created) or _now_iso()
    label = _slug(body)
    key = content_key(body)
    subject_text = _slug(subject, 40) or "user"
    value_text = " ".join(str(value if value is not None else body).split()).strip()
    meta = {
        CORTEX_FLAG: True,
        "cortex_record": "memory",
        "cortex_category": category,
        # Identity for dedupe/reconciliation: always derived from the whole
        # statement. The readable slug is kept beside it, never used as the key.
        "cortex_key": key,
        "cortex_label": label,
        "cortex_content": body,
        "cortex_importance": round(impression, 3),
        "cortex_keywords": list(keywords or sorted(tokens(body))),
        "cortex_created_at": created_iso,
        "cortex_created_ts": round(created, 3),
    }
    if predicate:
        meta["cortex_semantic_key"] = str(predicate)
    if subject:
        meta["cortex_subject"] = subject_text
    if extra_meta:
        meta.update({str(k): v for k, v in extra_meta.items()})
    kind = CATEGORY_KIND[category]
    return {
        "text": body,
        "kind": kind,
        "tags": ["saturn-cortex", f"cortex:{category}"],
        "src": CORTEX_SRC,
        "namespace": resolve_namespace(store_namespace),
        "importance": impression,
        "at": created_iso,
        "subject": subject_text,
        "predicate": str(predicate or f"{CORTEX_PREDICATE_PREFIX}.{category}.{key}"),
        "value": value_text[:200],
        # Episodes are historical events: identical text is still one event per
        # occurrence, so the engine must not merge them.
        "dedupe": kind != "episode",
        "meta": meta,
    }


def _candidate_field(candidate: dict, name: str) -> str:
    """Read one candidate field, wherever the extractor happened to put it.

    Engine versions have returned these flat, under ``meta`` and under
    ``fields``; accepting all three keeps the adapter schema-agnostic instead
    of silently dropping the semantic slot when the shape changes.
    """
    if not isinstance(candidate, dict):
        return ""
    found = candidate.get(name)
    if found is None or not str(found).strip():
        for container_name in ("meta", "fields", "attributes"):
            container = candidate.get(container_name)
            if isinstance(container, dict):
                nested = container.get(name)
                if nested is not None and str(nested).strip():
                    found = nested
                    break
    return str(found if found is not None else "").strip()


def deterministic_slot(content: str, category: str) -> str:
    """Stable semantic slot for the deterministic (regex) harvester.

    The pattern extractor writes ``User preferred <thing>: <value>`` for
    preferences and ``User name (identity): <value>`` for a name. Those are the
    mutable statements - only their tail changes - so they get a slot the
    engine can version on. Everything else (directives, projects, episodes)
    stays content-identified, because a second directive is a second rule, not
    a correction of the first.
    """
    text = " ".join(str(content or "").split()).strip()
    lowered = text.casefold()
    cat = str(category or "").strip().lower()
    if cat == "preference" and lowered.startswith("user preferred "):
        thing = text[len("User preferred "):].split(":", 1)[0].strip()
        if thing:
            return f"preferred.{_slug(thing, 40)}"
    if cat == "identity" and lowered.startswith("user name"):
        return "name"
    return ""


def candidate_payload(candidate: dict, store_namespace: Any = None) -> Optional[dict]:
    """Map an engine extraction candidate onto the cortex mapping.

    The extractor already knows the ``subject`` / ``predicate`` / ``value`` of
    what it found; dropping them and hashing the sentence instead is what made
    changed preferences coexist. They are kept here - cortex-prefixed - so the
    engine supersedes the older statement, and copied into meta so a reader can
    still see what the extractor meant.
    """
    if not isinstance(candidate, dict):
        return None
    text = " ".join(str(candidate.get("text") or "").split()).strip()
    if len(text) < 4:
        return None
    kind = str(candidate.get("kind") or "").strip().lower()
    category = {
        "preference": "preference",
        "fact": "fact",
        "episode": "episode",
        "note": "fact",
    }.get(kind, "fact")
    semantic_subject = _candidate_field(candidate, "subject")
    semantic_predicate = _candidate_field(candidate, "predicate")
    semantic_value = _candidate_field(candidate, "value")
    slot = ""
    if category != "episode" and semantic_predicate:
        slot = semantic_slot(category, semantic_subject or "user", semantic_predicate)
    payload = memory_payload(
        text,
        category,
        candidate.get("importance"),
        subject=semantic_subject or None,
        predicate=slot or None,
        value=semantic_value or None,
        extra_meta={
            "cortex_extracted": True,
            "cortex_predicate": semantic_predicate or None,
            "cortex_subject": semantic_subject or None,
            "cortex_value": semantic_value or None,
        },
        store_namespace=store_namespace,
    )
    return payload


def habit_payload(
    trigger: str,
    action: str,
    importance: float = HABIT_IMPORTANCE,
    store_namespace: Any = None,
) -> dict:
    """One structured habit entry: identity from the trigger, state in the row.

    ``kind='fact'`` with subject/predicate/value means the engine versions the
    entry: the same trigger + action merges (frequency increments through
    ``seen``), and a different action supersedes the previous form instead of
    leaving two active habits for one trigger.
    """
    trig = " ".join(str(trigger or "").split()).strip().lower()
    act = " ".join(str(action or "").split()).strip().lower()
    key, _entry_id = habit_identity(trig, store_namespace)
    text = f"Habit: when {trig} -> {act}"
    return {
        "text": text,
        "kind": "fact",
        "tags": ["saturn-cortex", "cortex:habit"],
        "src": CORTEX_SRC,
        "namespace": resolve_namespace(store_namespace),
        "importance": _clamp(importance, 0.05, 1.0),
        "subject": "user",
        "predicate": f"cortex.habit.{key}",
        "value": act[:200],
        "dedupe": True,
        "meta": {
            CORTEX_FLAG: True,
            "cortex_record": "habit",
            "cortex_category": "habit",
            # Whole-trigger identity; the readable label is metadata only.
            "cortex_key": key,
            "cortex_label": _slug(trig),
            "cortex_content": text,
            "cortex_trigger": trig,
            "cortex_action": act,
        },
    }


# ── The store the facade talks to ────────────────────────────────────────────

def migration_allows_writes(status: Any) -> bool:
    """True when a cortex migration status does not forbid native writes.

    Only an explicit success opens the write path - and that includes the
    "migration not enabled" / "disabled by the operator" outcomes, so a mode
    without cortex migration keeps working. A failed import, a missing status
    or a crashed importer keeps every write closed: writing on top of a
    half-migrated store is what leaves the two memory surfaces divergent.
    """
    if not isinstance(status, dict):
        return False
    return bool(status.get("ok"))


class CortexNativeStore:
    """Read/write port for cortex-shaped data on the shared Saturn store."""

    def __init__(self, db_path: Optional[Path] = None, cache_ttl: float = 1.0):
        self.db_path = Path(db_path) if db_path else None
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._lock = threading.RLock()
        self._cache: Optional[List[dict]] = None
        self._cache_at = 0.0
        self._cache_gen = -1
        self._cache_scope: Optional[Tuple[str, str, str]] = None

    # ------------------------------------------------------------- bridges
    def _read_bridge(self):
        """A bridge that is already serving. Never probes or starts Node."""
        try:
            bridge = ub.get_bridge()
            if bridge is None:
                return None
            if bridge.is_ready():
                return bridge
            return bridge if bridge.available(autostart=False) else None
        except Exception:
            return None

    def _write_bridge(self):
        """A bridge that may start the sidecar; raises when it cannot serve."""
        bridge = ub.get_bridge()
        if bridge is None:
            raise ub.SaturnUnavailable(
                "Saturn memory backend is disabled (SATURN_MEMORY_BACKEND)"
            )
        bridge.require()
        return bridge

    # --------------------------------------------------------------- cache
    def _invalidate(self) -> None:
        with self._lock:
            self._cache = None
            self._cache_at = 0.0
            self._cache_scope = None

    @staticmethod
    def _store_namespace(bridge) -> str:
        """Namespace of the store ``bridge`` writes into - never the process env."""
        try:
            value = str(getattr(bridge, "namespace", "") or "").strip()
        except Exception:
            value = ""
        return value or DEFAULT_NAMESPACE

    @classmethod
    def _scope(cls, bridge) -> Tuple[str, str, str]:
        """Cache identity: the store that actually *serves* the bridge.

        Bound to the verified destination identity - the file the sidecar
        reports it is serving, plus the namespace this bridge asked for - and
        to the endpoint, never to the configured ``bridge.file`` alone. Two
        endpoints pointed at the same configured file but serving different
        stores therefore produce different scopes and can never answer from one
        another's rows, and repointing either half invalidates the cache even
        though the process-wide import generation never changed.
        """
        try:
            destination = _destination(bridge)
            identity = str(destination.get("id") or "")
            namespace = str(destination.get("namespace") or "")
        except Exception:
            identity = ""
            namespace = cls._store_namespace(bridge)
        try:
            endpoint = str(getattr(bridge, "base_url", "") or "")
        except Exception:
            endpoint = ""
        return (identity, namespace or cls._store_namespace(bridge), endpoint)

    def _entries(self, bridge, refresh: bool = False) -> List[dict]:
        scope = self._scope(bridge)
        if not refresh:
            with self._lock:
                if (self._cache is not None
                        and self._cache_gen == _generation
                        and self._cache_scope == scope
                        and time.monotonic() - self._cache_at < self.cache_ttl):
                    return self._cache
        data = bridge.export_entries()
        entries = [item for item in (data.get("entries") or []) if isinstance(item, dict)]
        with self._lock:
            self._cache = entries
            self._cache_at = time.monotonic()
            self._cache_gen = _generation
            self._cache_scope = scope
        return entries

    @staticmethod
    def _meta(entry: dict) -> dict:
        meta = entry.get("meta")
        return meta if isinstance(meta, dict) else {}

    def _comparable_text(self, entry: dict) -> str:
        """The text that represents what an entry *says*, whichever writer made it.

        Cortex entries carry the sentence in ``meta.cortex_content``. Profile
        entries only carry the bare value in ``meta.saturn_value``, while the
        rendered text adds the category label and the key
        (``Preferences editor: Neovim``). Comparing against the bare value alone
        misses restatements such as ``User preferred editor: Neovim``, so both
        parts are kept for profile rows.
        """
        meta = self._meta(entry)
        if meta.get(CORTEX_FLAG):
            return str(meta.get("cortex_content") or entry.get("text") or "")
        return " ".join(
            part
            for part in (
                str(meta.get("saturn_value") or ""),
                str(entry.get("text") or ""),
            )
            if part
        )

    def _cortex_entries(
        self,
        entries: Iterable[dict],
        record: Optional[str] = None,
    ) -> List[dict]:
        rows = []
        for entry in entries:
            meta = self._meta(entry)
            if not meta.get(CORTEX_FLAG):
                continue
            if record is not None and meta.get("cortex_record") != record:
                continue
            rows.append(entry)
        return rows

    def _coverage_index(self, entries: Iterable[dict], exclude_id: str = "") -> List[Tuple[str, set]]:
        """Token sets of everything already stored, cortex or profile.

        Used to answer "is this fact already known?" across *both* writers, so
        the profile harvester, the cortex harvester and the engine extractor
        cannot create two rows for one statement.
        """
        index: List[Tuple[str, set]] = []
        for entry in entries:
            entry_id = str(entry.get("id") or "")
            if entry_id and entry_id == exclude_id:
                continue
            meta = self._meta(entry)
            if meta.get("cortex_record") == "habit":
                continue
            text = self._comparable_text(entry)
            value_set = tokens(text)
            if value_set:
                index.append((entry_id, value_set))
        return index

    @staticmethod
    def _covered(token_set: set, index: List[Tuple[str, set]]) -> str:
        if not token_set:
            return ""
        for entry_id, other in index:
            if token_set <= other:
                return entry_id
        return ""

    def _prepare(self, bridge) -> dict:
        """Run the legacy migration and report whether writes may proceed.

        The status is returned instead of being swallowed. A failed import
        means the legacy rows are not in the native store yet, so every write
        refuses until the migration is either fixed or explicitly disabled -
        callers see a distinct outcome per write instead of a silent success.
        """
        if bridge is None:
            return {"ok": True, "skipped": "no_bridge"}
        try:
            status = ensure_cortex_migrated(bridge=bridge, source=self.db_path)
        except Exception as exc:  # a crashed importer blocks writes
            print(f"[MemoryCortex] cortex migration failed: {exc}")
            return {"ok": False, "error": f"cortex migration failed: {exc}"}
        if not isinstance(status, dict):
            return {"ok": False, "error": "cortex migration returned no status"}
        return status

    # ----------------------------------------------------------- memory CRUD
    def remember(self, content: str, category: str = "fact", importance: float = 1.0) -> int:
        body = " ".join(str(content or "").split()).strip()
        if not body:
            return -1
        try:
            bridge = self._write_bridge()
        except Exception as exc:
            print(f"[MemoryCortex] native write unavailable: {exc}")
            return -1
        status = self._prepare(bridge)
        if not migration_allows_writes(status):
            print(
                "[MemoryCortex] native remember blocked until the cortex migration "
                f"succeeds: {status.get('error') or status}"
            )
            return -1
        slot = deterministic_slot(body, category)
        payload = memory_payload(
            body,
            category,
            importance,
            predicate=semantic_slot(category, "user", slot) if slot else None,
            store_namespace=self._store_namespace(bridge),
        )
        try:
            result = bridge.remember_entry(payload)
        except Exception as exc:
            print(f"[MemoryCortex] native remember failed: {exc}")
            return -1
        entry_id = str(result.get("id") or "")
        if not entry_id:
            print(f"[MemoryCortex] native remember rejected: {result.get('error') or 'no id'}")
            return -1
        self._invalidate()
        return cortex_int(entry_id)

    def forget(self, memory_id: int) -> bool:
        try:
            target = int(memory_id)
        except (TypeError, ValueError):
            return False
        try:
            bridge = self._write_bridge()
        except Exception as exc:
            print(f"[MemoryCortex] native forget unavailable: {exc}")
            return False
        status = self._prepare(bridge)
        if not migration_allows_writes(status):
            print(
                "[MemoryCortex] native forget blocked until the cortex migration "
                f"succeeds: {status.get('error') or status}"
            )
            return False
        try:
            for entry in self._cortex_entries(self._entries(bridge, refresh=True), record="memory"):
                if cortex_int(entry.get("id")) != target:
                    continue
                ok = bridge.delete_entry(str(entry.get("id")))
                self._invalidate()
                return bool(ok)
        except Exception as exc:
            print(f"[MemoryCortex] native forget failed: {exc}")
        return False

    def clear_all_memories(self) -> None:
        try:
            bridge = self._write_bridge()
        except Exception as exc:
            print(f"[MemoryCortex] native clear unavailable: {exc}")
            return
        status = self._prepare(bridge)
        if not migration_allows_writes(status):
            print(
                "[MemoryCortex] native clear blocked until the cortex migration "
                f"succeeds: {status.get('error') or status}"
            )
            return
        try:
            entries = self._cortex_entries(self._entries(bridge, refresh=True))
        except Exception as exc:
            print(f"[MemoryCortex] native clear failed: {exc}")
            return
        for entry in entries:
            entry_id = str(entry.get("id") or "")
            if not entry_id:
                continue
            try:
                bridge.delete_entry(entry_id)
            except Exception as exc:
                print(f"[MemoryCortex] native delete failed for {entry_id}: {exc}")
        self._invalidate()

    # --------------------------------------------------------------- recall
    def recall(self, query: str, top_k: int = 5, min_score: float = 0.12) -> List[Dict[str, Any]]:
        text = str(query or "").strip()
        if not text:
            return []
        bridge = self._read_bridge()
        if bridge is None:
            return []
        self._prepare(bridge)
        try:
            hits = bridge.recall_entries(
                text,
                limit=max(8, min(int(top_k or 5) * 4, 50)),
            )
        except Exception as exc:
            print(f"[MemoryCortex] native recall skipped: {exc}")
            return []

        results: List[Dict[str, Any]] = []
        for hit in hits or []:
            if not hit.get("cortex"):
                continue
            meta = hit.get("meta") or {}
            if meta.get("cortex_record") != "memory":
                continue
            # The engine always ranks; a hit with zero lexical relevance is
            # filler, not a recalled memory.
            if _as_float(hit.get("relevance")) <= 0:
                continue
            score = _as_float(hit.get("score"))
            if score < float(min_score):
                continue
            content = str(meta.get("cortex_content") or hit.get("text") or "").strip()
            if not content:
                continue
            results.append({
                "id": cortex_int(hit.get("id")),
                "category": str(meta.get("cortex_category") or "fact"),
                "content": content,
                "score": round(score, 3),
                "importance": _as_float(hit.get("importance"), CATEGORY_IMPORTANCE["fact"]),
                "created_at": _epoch_from_iso(
                    meta.get("cortex_created_at") or hit.get("created_at"),
                ),
                "access_count": int(_as_float(hit.get("access_count"), 0)),
            })
            if len(results) >= max(1, int(top_k or 5)):
                break
        return results

    def build_context_prompt(self, user_query: str, max_items: int = 4) -> str:
        recalled = self.recall(user_query, top_k=max_items, min_score=0.15)
        if not recalled:
            return ""
        # Drop anything the profile block already says. Without this the same
        # fact would ride in the prompt twice, once per memory surface.
        profile_sets: List[Tuple[str, set]] = []
        bridge = self._read_bridge()
        if bridge is not None:
            try:
                entries = self._entries(bridge)
                profile_sets = [
                    (str(entry.get("id") or ""), tokens(self._comparable_text(entry)))
                    for entry in entries
                    if not self._meta(entry).get(CORTEX_FLAG)
                ]
                profile_sets = [(eid, tset) for eid, tset in profile_sets if tset]
            except Exception:
                profile_sets = []

        lines = ["[RECALLED LONG-TERM MEMORY & USER CONTEXT]:"]
        used = 0
        for row in recalled:
            token_set = tokens(row["content"])
            if token_set and self._covered(token_set, profile_sets):
                continue
            lines.append(f"- ({str(row['category']).capitalize()}) {row['content']}")
            used += 1
        if not used:
            return ""
        lines.append("Use this background knowledge naturally when responding.")
        return "\n".join(lines)

    # ------------------------------------------------------------ harvesting
    def harvest(
        self,
        text: str,
        facts: List[Tuple[str, str, float]],
    ) -> List[str]:
        """Persist deterministic facts, then engine-extracted candidates.

        ``facts`` is the deterministic pattern output from the facade, so an
        optional model is never required and the guaranteed path stays in
        Python. Anything already present in either surface is reinforced with
        the engine's feedback call instead of being stored again.
        """
        harvested: List[str] = [
            content for content, _category, _importance in facts if content
        ]
        if not harvested and not str(text or "").strip():
            return []

        try:
            bridge = self._write_bridge()
        except Exception as exc:
            print(f"[MemoryCortex] native harvest unavailable: {exc}")
            return []

        status = self._prepare(bridge)
        if not migration_allows_writes(status):
            print(
                "[MemoryCortex] native harvest blocked until the cortex migration "
                f"succeeds: {status.get('error') or status}"
            )
            return []
        store_namespace = self._store_namespace(bridge)
        try:
            entries = self._entries(bridge, refresh=True)
        except Exception as exc:
            print(f"[MemoryCortex] native harvest index failed: {exc}")
            entries = []
        index = self._coverage_index(entries)

        for content, category, importance in facts:
            body = " ".join(str(content or "").split()).strip()
            if not body:
                continue
            token_set = tokens(body)
            existing = self._covered(token_set, index) if token_set else ""
            if existing:
                bridge.feedback_entry(existing)
                continue
            slot = deterministic_slot(body, category)
            payload = memory_payload(
                body,
                category,
                importance,
                predicate=semantic_slot(category, "user", slot) if slot else None,
                store_namespace=store_namespace,
            )
            try:
                result = bridge.remember_entry(payload)
            except Exception as exc:
                print(f"[MemoryCortex] native harvest write failed: {exc}")
                continue
            if result.get("id"):
                index = self._coverage_index(self._entries(bridge, refresh=True))
        self._invalidate()

        # Native extraction on top of the deterministic pass. Failures return an
        # empty list inside the bridge, so a missing extractor is invisible.
        try:
            candidates = bridge.extract_candidates(text, limit=5) if str(text or "").strip() else []
        except Exception:
            candidates = []
        if candidates:
            try:
                entries = self._entries(bridge, refresh=True)
            except Exception:
                entries = []
            index = self._coverage_index(entries)
            for candidate in candidates:
                payload = candidate_payload(candidate, store_namespace)
                if not payload:
                    continue
                token_set = tokens(payload["meta"]["cortex_content"])
                existing = self._covered(token_set, index) if token_set else ""
                if existing:
                    bridge.feedback_entry(existing)
                    continue
                try:
                    result = bridge.remember_entry(payload)
                except Exception:
                    continue
                if result.get("id"):
                    index = self._coverage_index(self._entries(bridge, refresh=True))
            self._invalidate()

        return harvested

    # ---------------------------------------------------------------- habits
    def record_habit(self, trigger_event: str, action_taken: str) -> None:
        trig = " ".join(str(trigger_event or "").split()).strip().lower()
        act = " ".join(str(action_taken or "").split()).strip().lower()
        if not trig or not act:
            return
        try:
            bridge = self._write_bridge()
        except Exception as exc:
            print(f"[MemoryCortex] native habit unavailable: {exc}")
            return
        status = self._prepare(bridge)
        if not migration_allows_writes(status):
            print(
                "[MemoryCortex] native habit blocked until the cortex migration "
                f"succeeds: {status.get('error') or status}"
            )
            return
        try:
            result = bridge.remember_entry(
                habit_payload(trig, act, store_namespace=self._store_namespace(bridge))
            )
        except Exception as exc:
            print(f"[MemoryCortex] native habit write failed: {exc}")
            return
        if not result.get("id"):
            print(f"[MemoryCortex] native habit rejected: {result.get('error') or 'no id'}")
        self._invalidate()

    def habit_suggestion(self, trigger_event: str, min_frequency: int = 2) -> Optional[str]:
        trig = " ".join(str(trigger_event or "").split()).strip().lower()
        if not trig:
            return None
        bridge = self._read_bridge()
        if bridge is None:
            return None
        key, _entry_id = habit_identity(trig, self._store_namespace(bridge))
        self._prepare(bridge)
        try:
            entries = self._cortex_entries(self._entries(bridge), record="habit")
        except Exception as exc:
            print(f"[MemoryCortex] native habit read failed: {exc}")
            return None
        best: Optional[dict] = None
        for entry in entries:
            meta = self._meta(entry)
            if str(meta.get("cortex_key") or "") != key:
                continue
            if best is None or _as_float(entry.get("seen"), 1) > _as_float(best.get("seen"), 1):
                best = entry
        if best is None:
            return None
        frequency = int(_as_float(best.get("seen"), 1))
        if frequency < max(1, int(min_frequency or 1)):
            return None
        meta = self._meta(best)
        return str(meta.get("cortex_action") or "").strip() or None

    # ----------------------------------------------------------------- stats
    def get_stats(self) -> Dict[str, Any]:
        bridge = self._read_bridge()
        if bridge is None:
            return {
                "total_memories": 0,
                "total_habits": 0,
                "categories": {},
                "db_path": "saturn (unavailable)",
                "available": False,
                "store": "saturn",
            }
        self._prepare(bridge)
        try:
            entries = self._entries(bridge)
        except Exception as exc:
            print(f"[MemoryCortex] native stats failed: {exc}")
            return {
                "total_memories": 0,
                "total_habits": 0,
                "categories": {},
                "db_path": "saturn (unavailable)",
                "available": False,
                "store": "saturn",
            }
        memories = self._cortex_entries(entries, record="memory")
        habits = self._cortex_entries(entries, record="habit")
        categories: Dict[str, int] = {}
        for entry in memories:
            category = str(self._meta(entry).get("cortex_category") or "fact")
            categories[category] = categories.get(category, 0) + 1
        return {
            "total_memories": len(memories),
            "total_habits": len(habits),
            "categories": categories,
            "db_path": f"saturn:{self._store_namespace(bridge)}",
            "available": True,
            "store": "saturn",
        }


# ── Paths ────────────────────────────────────────────────────────────────────

def default_source() -> Path:
    """The legacy SQLite database. Never written to in unified mode."""
    override = os.environ.get("SATURN_MEMORY_CORTEX_DB")
    if override:
        return Path(override).expanduser()
    try:
        from core.user_paths import get_user_data_dir

        return get_user_data_dir() / "config" / "memory_cortex.db"
    except Exception:  # pragma: no cover - isolated environments
        return Path.home() / "SaturnAI" / "config" / "memory_cortex.db"


def _store_dir(base: Optional[Path] = None) -> Path:
    path = Path(base or default_source())
    return path.parent if path.suffix else path


def marker_path(base: Optional[Path] = None) -> Path:
    return _store_dir(base) / MARKER_NAME


def rollback_dir(base: Optional[Path] = None) -> Path:
    return _store_dir(base) / "rollback"


# ── Migration ────────────────────────────────────────────────────────────────

def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except Exception:
        return ""


def source_files_digest(source: Optional[Path] = None) -> str:
    """Cheap digest of the database files, ``-wal`` included.

    Used only as the fast "did the source change at all" gate before a snapshot
    is taken. It reads bytes and never opens SQLite, so it is safe on the read
    path, and hashing the ``-wal`` sibling means committed-but-uncheckpointed
    rows take part in the comparison.
    """
    path = Path(source or default_source())
    digest = hashlib.sha256(b"memory_cortex\x1f")
    for suffix in ("", "-wal"):
        candidate = Path(str(path) + suffix)
        label = suffix or "main"
        try:
            if not candidate.exists():
                digest.update(f"{label}\x1f0\x1f".encode("utf-8"))
                continue
            data = candidate.read_bytes()
        except Exception as exc:
            digest.update(f"{label}\x1funreadable\x1f{exc}".encode("utf-8"))
            continue
        digest.update(f"{label}\x1f{len(data)}\x1f".encode("utf-8"))
        digest.update(data)
    return digest.hexdigest()


def _snapshot_with_backup(source: Path, destination: Path) -> None:
    """Copy ``source`` into ``destination`` through SQLite's backup API.

    The backup runs inside a read transaction, so the destination is one
    committed state - including rows that are committed but still sitting in
    the write-ahead log. Read-only access is tried first so the source is
    provably untouched; a read-write handle is the fallback for a WAL database
    whose shared-memory file is not readable by a read-only connection.
    """
    attempts = (Path(source).resolve().as_uri() + "?mode=ro", str(source))
    last_error: Optional[Exception] = None
    for target in attempts:
        try:
            src = sqlite3.connect(
                target, uri=target.startswith("file:"), timeout=5.0
            )
        except Exception as exc:
            last_error = exc
            continue
        try:
            dest = sqlite3.connect(str(destination))
            try:
                src.backup(dest)
                dest.commit()
            finally:
                dest.close()
            return
        except Exception as exc:
            last_error = exc
        finally:
            src.close()
    raise RuntimeError(f"cannot snapshot {source}: {last_error}")


def _discard_snapshot(destination: Path) -> None:
    """Remove a partially written snapshot so it can never be read as one."""
    for candidate in (
        destination,
        Path(str(destination) + "-wal"),
        Path(str(destination) + "-shm"),
    ):
        try:
            if candidate.exists():
                candidate.unlink()
        except Exception:
            pass


def write_rollback_copy(source: Optional[Path] = None) -> Optional[Path]:
    """Take a transactionally consistent snapshot of the database.

    A byte copy is not a snapshot: in WAL mode the committed rows may still be
    sitting in ``-wal``, and a rollback-journal writer may be mid-transaction,
    so a plain copy can miss committed data or capture a torn page. The
    snapshot is produced by SQLite's online backup instead and is both the
    rollback archive and the only file the importer reads.

    There is no byte-copy fallback. When the backup API cannot produce a
    consistent snapshot the caller fails closed - no import, no marker, no
    claim that a snapshot exists - because importing a torn copy silently
    duplicates or loses rows, while importing nothing is merely deferred.
    """
    path = Path(source or default_source())
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = rollback_dir(path) / f"memory_cortex-{stamp}.db"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _snapshot_with_backup(path, destination)
    except Exception as exc:
        _discard_snapshot(destination)
        print(f"[MemoryCortex] rollback snapshot failed, migration blocked: {exc}")
        return None
    return destination


def _read_cortex_rows(snapshot: Path) -> Tuple[List[dict], List[dict]]:
    """Read memories/habits from the *copy*, read-only."""
    uri = Path(snapshot).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        memories = [
            dict(row)
            for row in conn.execute(
                "SELECT id, category, content, keywords, importance, created_at, "
                "last_accessed_at, access_count FROM memories"
            )
        ]
        try:
            habits = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, trigger_event, suggested_action, frequency, last_triggered "
                    "FROM habits"
                )
            ]
        except sqlite3.Error:
            habits = []
    finally:
        conn.close()
    return memories, habits


def build_cortex_entries(
    memories: List[dict],
    habits: List[dict],
    store_namespace: Any = None,
) -> List[dict]:
    """Build native entry payloads with stable ids, in a deterministic order.

    Memory identity is the full deterministic id: the statement, its timestamp,
    the *destination* namespace and an occurrence ordinal all go into it.
    Nothing here is a sixty-character slug, so two long statements sharing a
    prefix - and two identical episodes recorded in the same second - both
    survive the move. The namespace is supplied by the caller from the bridge
    being imported into, never read from the process environment: importing the
    same database into two namespaces must produce two independent row sets.
    """
    entries: List[dict] = []
    occurrences: Dict[Tuple[str, str, str], int] = {}
    for row in memories:
        body = " ".join(str(row.get("content") or "").split()).strip()
        if not body:
            continue
        category = str(row.get("category") or "fact").strip().lower() or "fact"
        created = _as_float(row.get("created_at"), 0.0) or time.time()
        occurrence = (category, body, f"{created:.3f}")
        ordinal = occurrences.get(occurrence, 0)
        occurrences[occurrence] = ordinal + 1
        entry_id = migrated_memory_id(category, body, created, ordinal, store_namespace)
        digest = entry_id[len(STABLE_ID_PREFIX):]
        payload = memory_payload(
            body,
            category,
            row.get("importance"),
            at=_iso_from_epoch(created),
            created_at=created,
            keywords=str(row.get("keywords") or "").split() or None,
            predicate=f"{CORTEX_PREDICATE_PREFIX}.{category}.{digest}",
            extra_meta={
                "cortex_migrated": True,
                "cortex_source_id": int(row.get("id") or 0),
                "cortex_last_accessed_at": _iso_from_epoch(row.get("last_accessed_at")),
            },
            store_namespace=store_namespace,
        )
        payload["id"] = entry_id
        # Key the row by the same digest the id uses, so key-based reconciliation
        # elsewhere is as lossless as the id itself.
        payload["meta"]["cortex_key"] = digest
        payload["seen"] = 1
        payload["accessCount"] = max(0, int(row.get("access_count") or 0))
        last_access = _iso_from_epoch(row.get("last_accessed_at"))
        if last_access:
            payload["lastAccessedAt"] = last_access
        entries.append(payload)

    for row in habits:
        trigger = " ".join(str(row.get("trigger_event") or "").split()).strip().lower()
        action = " ".join(str(row.get("suggested_action") or "").split()).strip().lower()
        if not trigger or not action:
            continue
        _key, entry_id = habit_identity(trigger, store_namespace)
        last = _as_float(row.get("last_triggered"), 0.0)
        payload = habit_payload(trigger, action, store_namespace=store_namespace)
        payload["id"] = entry_id
        payload["seen"] = max(1, int(row.get("frequency") or 1))
        at_iso = _iso_from_epoch(last)
        if at_iso:
            payload["at"] = at_iso
        payload["meta"]["cortex_migrated"] = True
        payload["meta"]["cortex_source_id"] = int(row.get("id") or 0)
        entries.append(payload)
    return entries


def read_marker(base: Optional[Path] = None) -> dict:
    path = marker_path(base)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def migration_status(source: Optional[Path] = None) -> dict:
    source_path = Path(source or default_source())
    marker = read_marker(source_path)
    return {
        "version": marker.get("version", MIGRATION_VERSION),
        "source": str(source_path),
        "source_present": source_path.exists(),
        "source_sha256": marker.get("source_sha256") or _file_sha256(source_path),
        "source_files_sha256": marker.get("source_files_sha256") or source_files_digest(source_path),
        "marker": str(marker_path(source_path)),
        "marker_present": bool(marker),
        "imported_at": marker.get("imported_at"),
        "counts": marker.get("counts") or {},
        "rollback": marker.get("rollback"),
        "destination": _marker_destination(marker) if marker else None,
    }


def _destination(bridge) -> dict:
    """Identity of the store a cortex migration writes into.

    Markers and process caches are bound to this, not just to the SQLite file:
    pointing ``SATURN_MEMORY_FILE`` or the namespace at a *different* store
    must migrate into it, instead of reporting "already imported" from a marker
    that describes some other destination.

    Two rules, deliberately asymmetric:

    * the *store* follows the sidecar. When ``/diagnostics`` answers, the file
      it reports is the destination - that is what ``import_entries`` writes
      to - and a disagreement with the configured file is recorded in
      ``mismatch``.
    * the *namespace* follows the bridge, never the diagnostics. The engine
      scopes every call by the namespace carried in the request, so the
      server's own default namespace is not the destination of this import.
      Adopting it would fold two bridges (``origin`` and ``other``) into one
      namespace, where their deterministic ids collide and only one row
      survives.
    """
    engine_file = None
    try:
        engine_file = str(getattr(bridge, "file", "") or "") or None
    except Exception:
        engine_file = None
    ns = ""
    try:
        ns = str(getattr(bridge, "namespace", "") or "").strip()
    except Exception:
        ns = ""
    verified = False
    mismatch = ""
    served_namespace = None
    probe = getattr(bridge, "destination_identity", None)
    if callable(probe):
        try:
            identity = probe(verify=True)
        except Exception:
            identity = None
        if isinstance(identity, dict):
            reported = identity.get("reported")
            reported = reported if isinstance(reported, dict) else {}
            served_file = str(
                identity.get("engine_file") or reported.get("engine_file") or ""
            ).strip()
            served_namespace = str(reported.get("namespace") or "").strip() or None
            if served_file:
                engine_file = served_file
            verified = bool(identity.get("verified"))
            mismatch = str(identity.get("mismatch") or "")
    ns = ns or DEFAULT_NAMESPACE
    identity_hash = hashlib.sha1(
        f"{engine_file or ''}\x1f{ns}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "engine_file": engine_file,
        "namespace": ns,
        "id": identity_hash,
        "verified": verified,
        "mismatch": mismatch,
        "served_namespace": served_namespace,
    }


def _marker_destination(marker: dict) -> dict:
    """Destination recorded by a marker, including legacy single-field markers."""
    recorded = marker.get("destination")
    if isinstance(recorded, dict) and recorded.get("id"):
        return {
            "engine_file": recorded.get("engine_file"),
            "namespace": str(recorded.get("namespace") or "").strip(),
            "id": str(recorded.get("id")),
        }
    engine_file = marker.get("engine_file")
    ns = str(marker.get("namespace") or "").strip()
    identity = hashlib.sha1(
        f"{engine_file or ''}\x1f{ns}".encode("utf-8")
    ).hexdigest()[:24]
    return {"engine_file": engine_file, "namespace": ns, "id": identity}


def _migration_key(bridge, digest: str, source: Optional[Path] = None) -> dict:
    destination = _destination(bridge)
    return {
        "version": MIGRATION_VERSION,
        "source": str(Path(source or default_source())),
        "source_sha256": digest,
        "destination_id": destination["id"],
        "namespace": destination["namespace"],
        "engine_file": destination["engine_file"],
    }


def _marker_matches(marker: dict, key: dict) -> bool:
    """A marker only satisfies a run when it describes the *same* migration.

    Version, source digest (WAL included), namespace and destination store
    identity must all agree. Anything else - a changed database, a different
    ``SATURN_MEMORY_FILE``, another namespace - is a real migration into a new
    place, not a repeat of this one.
    """
    if not marker or not marker.get("imported_at"):
        return False
    if int(marker.get("version") or 0) != int(key.get("version") or 0):
        return False
    if not key.get("source_sha256"):
        return False
    recorded = str(
        marker.get("source_files_sha256") or marker.get("source_sha256") or ""
    )
    if recorded != key["source_sha256"]:
        return False
    destination = _marker_destination(marker)
    if destination["id"] != key.get("destination_id"):
        return False
    return destination["namespace"] == str(key.get("namespace") or "")


def _atomic_write(path: Path, payload) -> None:
    """Write ``payload`` as JSON through a temp file, or raise.

    Kept separately named so a test can inject a persistence failure without
    having to make a directory read-only, and so ``_write_marker`` has exactly
    one place that can fail.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _write_marker(
    report: dict,
    digest: str,
    destination: dict,
    rollback: Optional[Path],
    base: Optional[Path],
) -> None:
    previous = read_marker(base)
    stamp = _now_iso()
    # Only inherit the "first import" stamp when this marker describes the same
    # destination store; otherwise the old stamp belongs to a different copy.
    same_destination = bool(previous) and _marker_destination(previous)["id"] == destination.get("id")
    marker = {
        "version": MIGRATION_VERSION,
        "source": report.get("source"),
        "source_sha256": digest,
        "source_files_sha256": report.get("source_files_sha256"),
        "namespace": destination.get("namespace"),
        "engine_file": destination.get("engine_file"),
        "destination": dict(destination),
        "stable_identity": "memory(full statement+created_at+ordinal) / habit(trigger) -> brc_<sha1[:24]>",
        "imported_at": (previous.get("imported_at") or stamp) if same_destination else stamp,
        "last_import_at": stamp,
        "counts": dict(report.get("counts") or {}),
        "rollback": str(rollback) if rollback else previous.get("rollback"),
    }
    path = marker_path(base)
    try:
        _atomic_write(path, marker)
    except Exception as exc:
        # A marker that did not reach disk did not record the migration. Say so
        # loudly instead of printing and reporting success: the caller turns
        # this into a failed report so nothing caches the run and the next boot
        # retries against an idempotent importer.
        raise MarkerWriteError(
            f"cannot persist the cortex migration marker at {path}: {exc}"
        ) from exc


def _record_marker(
    report: dict,
    digest: str,
    destination: dict,
    rollback: Optional[Path],
    base: Optional[Path],
) -> bool:
    """Persist the marker as part of the result, or fail the whole report.

    Marker persistence *is* the success condition: an import whose marker was
    not written is indistinguishable from one that never ran, so the report is
    failed (and never cached) rather than left looking complete. The import
    itself stays on disk, which is safe because the importer skips ids that
    already exist and never overwrites newer native values - a retry adds
    nothing but writes the marker.
    """
    try:
        _write_marker(report, digest, destination, rollback, base)
    except MarkerWriteError as exc:
        report["ok"] = False
        report["reason"] = "marker_write_failed"
        report["error"] = (
            f"{exc}. The import was attempted but is not recorded, so it will be "
            "retried; entries already present are skipped, never overwritten"
        )
        return False
    return True


def migrate_cortex_db(
    source: Optional[Path] = None,
    bridge: "ub.UniversalBridge | None" = None,
) -> dict:
    """Import the legacy SQLite database into Saturn, snapshot-first and idempotent.

    The source database is never modified and never removed. A transactionally
    consistent snapshot is taken (SQLite backup, so committed WAL content is
    included), the snapshot's fingerprint and the destination store identity
    are recorded in the marker, and only the snapshot is read.
    """
    source_path = Path(source or default_source())
    report: dict = {
        "ok": False,
        "source": str(source_path),
        "entries": 0,
        "memories": 0,
        "habits": 0,
        "imported": 0,
        "skipped": 0,
        "batches": 0,
        "rollback": None,
        "marker": str(marker_path(source_path)),
        "engine_file": None,
        "source_files_sha256": None,
        "destination": None,
        "reason": None,
        "error": None,
    }

    if not source_path.exists():
        report["ok"] = True
        report["reason"] = "source_missing"
        return report

    bridge = bridge or ub.get_bridge()
    if bridge is None:
        report["error"] = "Saturn backend is disabled (SATURN_MEMORY_BACKEND)"
        return report
    try:
        bridge.require()
    except ub.SaturnError as exc:
        report["error"] = str(exc)
        return report
    except Exception as exc:  # pragma: no cover - hostile environment
        report["error"] = f"Saturn backend unavailable: {exc}"
        return report

    destination = _destination(bridge)
    report["destination"] = destination
    report["engine_file"] = destination["engine_file"]
    report["source_files_sha256"] = source_files_digest(source_path)
    rollback = write_rollback_copy(source_path)
    report["rollback"] = str(rollback) if rollback else None
    if rollback is None:
        # The SQLite backup API could not produce a consistent snapshot and
        # there is no byte-copy fallback: importing a torn copy would silently
        # duplicate or drop rows. Nothing is imported and no snapshot or marker
        # is claimed, so a later call retries once the writer settles.
        report["error"] = (
            "cannot snapshot memory_cortex.db with the SQLite backup API; "
            "migration blocked and nothing was imported"
        )
        return report
    # Fingerprint the committed snapshot, not the raw files: this is the state
    # the importer actually read, WAL rows included.
    digest = _file_sha256(rollback)

    try:
        memories, habits = _read_cortex_rows(rollback)
    except Exception as exc:
        report["error"] = f"cannot read the database copy: {exc}"
        return report

    entries = build_cortex_entries(
        memories, habits, store_namespace=destination["namespace"]
    )
    report["entries"] = len(entries)
    report["memories"] = len(memories)
    report["habits"] = len(habits)

    if not entries:
        report["ok"] = True
        report["reason"] = "source_empty"
        report["counts"] = {"memories": 0, "habits": 0, "imported": 0, "skipped": 0}
        _record_marker(report, digest, destination, rollback, source_path)
        return report

    try:
        existing = bridge.export_entries()
    except Exception as exc:
        report["error"] = f"Saturn export failed: {exc}"
        return report

    known_ids: set = set()
    habit_slots: set = set()
    for entry in existing.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        known_ids.add(str(entry.get("id") or ""))
        meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
        # Only habits reconcile by trigger. A live habit row was written by
        # ``record_habit`` with an engine-assigned id, so the migrated frequency
        # has to be absorbed by that row instead of becoming a second habit.
        if meta.get(CORTEX_FLAG) and meta.get("cortex_record") == "habit":
            habit_slots.add((str(meta.get("cortex_record") or ""), str(meta.get("cortex_key") or "")))

    pending = []
    for entry in entries:
        meta = entry["meta"]
        if entry["id"] in known_ids:
            report["skipped"] += 1
            continue
        if str(meta.get("cortex_record") or "") == "habit":
            identity = (str(meta.get("cortex_record") or ""), str(meta.get("cortex_key") or ""))
            if identity in habit_slots:
                report["skipped"] += 1
                continue
        # Memories are identified by their own id and nothing else: the id is
        # the full statement plus timestamp, namespace and ordinal, so no row
        # can be lost to a truncated keyword colliding with another row.
        pending.append(entry)

    if pending:
        try:
            result = bridge.import_entries(pending)
        except Exception as exc:
            report["error"] = f"Saturn import failed: {exc}"
            return report
        report["imported"] = int(result.get("imported") or 0)
        report["skipped"] += int(result.get("skipped") or 0)
        report["batches"] = int(result.get("batches") or 0)
        # The transport already refuses a batch it cannot account for, so this
        # is belt and braces: a partial import that looked complete must not
        # reach the marker, because the marker is what stops the retry.
        accounted = report["imported"] + int(result.get("skipped") or 0)
        if accounted != len(pending):
            report["error"] = (
                f"Saturn accounted for {accounted} of {len(pending)} cortex entries"
            )
            return report
        if report["imported"] == 0 and report["skipped"] < len(entries):
            report["error"] = "Saturn did not import the cortex snapshot"
            return report
        if report["imported"]:
            global _generation
            _generation += 1

    report["ok"] = True
    report["counts"] = {
        "memories": report["memories"],
        "habits": report["habits"],
        "imported": report["imported"],
        "skipped": report["skipped"],
    }
    # Only now - every batch verified and accounted for - is the migration
    # recorded. A marker written before the last batch would convert an
    # interruption into permanent data loss.
    _record_marker(report, digest, destination, rollback, source_path)
    return report


def ensure_cortex_migrated(
    bridge: "ub.UniversalBridge | None" = None,
    source: Optional[Path] = None,
    force: bool = False,
) -> dict:
    """Run the cortex importer at most once per process *and* per destination.

    A marker for the same source snapshot, migration version, namespace and
    destination store is respected across restarts: unified mode keeps editing
    the native store while the SQLite file stays frozen, so re-importing an
    unchanged snapshot on every boot would resurrect deleted cortex facts. A
    different ``SATURN_MEMORY_FILE`` or namespace is a different destination
    and is migrated into, rather than mistaken for a repeat.

    Failures are not cached, so a later call retries once the sidecar is up.
    """
    global _migrated, _migrated_key
    state = resolve_saturn_mode()
    if not state.cortex_native or not state.migration:
        return {"ok": True, "skipped": "migration_not_enabled"}
    if str(os.environ.get(_MIGRATE_ENV, "auto")).strip().lower() in _DISABLED_VALUES:
        return {"ok": True, "skipped": "disabled_by_env"}

    source_path = Path(source or default_source())
    bridge = bridge or ub.get_bridge()
    # The gate never opens SQLite: it hashes the main file and its -wal sibling,
    # so committed-but-uncheckpointed rows are part of the comparison.
    key = _migration_key(bridge, source_files_digest(source_path), source_path)

    with _migrate_lock:
        if _migrated is not None and not force and _migrated_key == key:
            return _migrated

        if not force:
            marker = read_marker(source_path)
            if _marker_matches(marker, key):
                _migrated = {
                    "ok": True,
                    "reason": "already_imported",
                    "source": str(source_path),
                    "marker": str(marker_path(source_path)),
                    "imported": 0,
                    "skipped": int((marker.get("counts") or {}).get("skipped") or 0),
                    "rollback": marker.get("rollback"),
                    "destination": _marker_destination(marker),
                    "error": None,
                }
                _migrated_key = key
                return _migrated

        result = migrate_cortex_db(source=source_path, bridge=bridge)
        if result.get("ok") and result.get("reason") != "source_missing":
            _migrated = result
            _migrated_key = key
        elif result.get("error"):
            print(f"[MemoryCortex] cortex import deferred: {result['error']}")
        return result


def reset_migration_state() -> None:
    """Test helper: forget the once-per-process guard."""
    global _migrated, _migrated_key
    with _migrate_lock:
        _migrated = None
        _migrated_key = None
