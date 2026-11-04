from __future__ import annotations
from core.user_paths import get_user_data_dir

import json
import os
import re
from datetime import datetime
from threading import Lock, RLock
from pathlib import Path
import sys
import time

from memory.saturn_mode import resolve_saturn_mode

for _s_name in ("stdout", "stderr"):
    try:
        _s = getattr(sys, _s_name, None)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = get_user_data_dir() / "memory" / "long_term.json"
_lock            = Lock()
# Serializes read-modify-write sequences (update/remember/forget/session) so two
# threads cannot both read a profile and then write back their own version. The
# sidecar revision protects across processes; this protects inside one.
_rmw_lock        = RLock()
MAX_VALUE_LENGTH = 380

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

# ── Saturn (Stage One hybrid) budgets ────────────────────────────────────────
#
# The JSON block stays primary. Native Saturn context is an optional, bounded
# suffix: if the sidecar is slow, missing, or returns nothing new, the prompt
# is byte-for-byte what it was before. These are deliberately small - the
# point is to surface an old fact the lexical index cannot rank, not to dump
# a second memory into every request.
SATURN_PROMPT_MAX_LINES = 4
SATURN_PROMPT_MAX_CHARS = 320
SATURN_PROMPT_TIMEOUT = 1.5
SATURN_RECALL_TIMEOUT = 2.5
# Mirror writes are bounded and happen after the JSON write. A slow sidecar
# costs a few seconds at most on the extracting thread, never the local save.
SATURN_WRITE_TIMEOUT = 4.0

def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, default))))
    except (TypeError, ValueError):
        return default

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }


def _local_memory() -> dict:
    """Read the authoritative JSON store. Sessions live here too."""
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
            return _empty_memory()


# ── Stage Two: full Saturn ownership ─────────────────────────────────────────
#
# In ``full`` mode the sidecar is the durable store. Reads and writes go to
# Saturn through the adapters below, and ``long_term.json`` is only a migration
# source, an export destination and a rollback point.
#
# The invariant that keeps authority from splitting is *reads degrade, writes
# raise*:
#
#   * a read that cannot reach Saturn returns a ``ProfileOutage`` - an
#     empty-shaped map that carries failed-read provenance - and records the
#     reason in ``degraded_status()``. It never invents data and never edits the
#     JSON file.
#   * a write after a failed read is refused. Otherwise a transient outage
#     would look like "the user deleted everything", and the next save would
#     erase the native store.
#   * a write that fails raises ``SaturnError``. There is no JSON fallback,
#     because a second copy written behind a failed authority is exactly how
#     two stores start disagreeing about what someone said.
_native_state = {"read_ok": False, "degraded": "", "reported": "", "migration_error": ""}

_MIGRATE_ENV = "SATURN_MEMORY_MIGRATE"
_MIGRATE_DISABLED_VALUES = ("off", "0", "false", "no", "disabled", "skip")


def _migration_enabled() -> bool:
    """True when copy-first migration is part of this mode and not switched off."""
    try:
        if not resolve_saturn_mode().migration:
            return False
    except Exception:
        return False
    return str(os.environ.get(_MIGRATE_ENV, "auto")).strip().lower() not in _MIGRATE_DISABLED_VALUES


def _migration_block_reason() -> str:
    """Why authoritative writes must stay blocked, or ``""`` when they may run.

    A failed initial import means the native store may be missing facts that
    ``long_term.json`` already holds. Writing over it anyway is how a migration
    failure turns into data loss, so the gate lasts until migration succeeds -
    unless the operator explicitly disables migration.
    """
    if not _migration_enabled():
        return ""
    with _lock:
        return str(_native_state.get("migration_error") or "")


def _require_migration_ready() -> None:
    problem = _migration_block_reason()
    if not problem:
        return
    from memory import universal_bridge as ub

    raise ub.SaturnWriteError(
        "refusing to write: the initial JSON memory migration has not completed "
        f"({problem}). Nothing was written; retry once migration succeeds, or set "
        f"{_MIGRATE_ENV}=off to accept the current native store as-is."
    )


def _native_bridge():
    """Return the live bridge for an authoritative operation.

    Imported lazily so a legacy deployment never loads the transport, and so
    tests can substitute ``get_bridge``.
    """
    from memory import universal_bridge as ub

    bridge = ub.get_bridge()
    if bridge is None:
        raise ub.SaturnUnavailable(
            "Saturn memory backend is disabled (SATURN_MEMORY_BACKEND); "
            "full mode cannot read or write memory"
        )
    return bridge


def _set_native_health(ok: bool, reason: str = "") -> None:
    """Record authoritative-backend health, logging only on change."""
    with _lock:
        was_ok = bool(_native_state["read_ok"])
        _native_state["read_ok"] = bool(ok)
        _native_state["degraded"] = "" if ok else (reason or "unknown Saturn failure")
        report = (not ok) and reason != _native_state["reported"]
        if not ok:
            _native_state["reported"] = reason
        else:
            _native_state["reported"] = ""
    if report:
        print(f"[Memory] ⚠️ Saturn authoritative store unavailable: {reason or 'unknown failure'}")
    elif ok and not was_ok:
        print("[Memory] Saturn authoritative store recovered")


def degraded_status() -> dict:
    """Public, read-only view of the authoritative backend's health.

    ``active`` means the last native read failed. While it is set, writes are
    refused rather than applied to a profile that may have been invented by the
    outage.
    """
    with _lock:
        return {
            "active": not _native_state["read_ok"],
            "reason": _native_state["degraded"],
            "read_ok": bool(_native_state["read_ok"]),
        }


def _load_native() -> dict:
    """Read the authoritative profile from Saturn, migrating once if needed.

    Every failure returns a :class:`memory.universal_bridge.ProfileOutage`
    rather than a plain empty dict. It still reads like a profile, but it
    carries failed-read provenance and a distinct type, so no write path -
    including one that runs after the store recovered - can save it back over
    the real data.
    """
    from memory import universal_bridge as ub

    try:
        bridge = _native_bridge()
        bridge.require()
    except Exception as exc:
        _set_native_health(False, str(exc))
        return ub.ProfileOutage(str(exc) or type(exc).__name__)

    # Copy-first import of long_term.json, at most once per process. It is
    # idempotent and never modifies the source, so a failure here is a
    # diagnostic, not a reason to refuse the read - but it *is* a reason to
    # refuse the next write, which is what ``migration_error`` records.
    try:
        from memory.saturn_migrate import ensure_migrated

        report = ensure_migrated(bridge=bridge, source=MEMORY_PATH)
        error = str(report.get("error") or "")
        if error:
            print(f"[Memory] Saturn migration deferred: {error}")
        with _lock:
            _native_state["migration_error"] = error
    except Exception as exc:
        print(f"[Memory] Saturn migration skipped: {exc}")
        with _lock:
            _native_state["migration_error"] = str(exc)

    try:
        memory = bridge.load_memory(include_sessions=True)
    except Exception as exc:
        _set_native_health(False, str(exc))
        return ub.ProfileOutage(str(exc) or type(exc).__name__)
    if isinstance(memory, ub.ProfileOutage):
        _set_native_health(False, memory.reason)
        return memory
    if not isinstance(memory, dict):
        reason = "native store returned no profile"
        _set_native_health(False, reason)
        return ub.ProfileOutage(reason)
    _set_native_health(True)
    # A transport that answers with a plain dict (an in-process double, or a
    # future adapter) is attested here - and only here - because this is the
    # one place that both performed the read and holds its result. The public
    # ``ProfileSnapshot`` constructor mints nothing, so a caller cannot do the
    # same thing with a profile it invented.
    if not ub.snapshot_state(memory)["ok"]:
        return ub._attest_read(
            memory,
            revision=getattr(memory, "revision", None) or None,
            namespace=getattr(bridge, "namespace", None) or None,
            file=getattr(bridge, "file", None) and str(bridge.file) or None,
        )
    return memory


def load_memory() -> dict:
    """Return the authoritative memory profile.

    ``legacy``/``hybrid``: the local ``long_term.json`` store, unchanged.
    ``full``: the profile rendered from native Saturn. A read failure returns a
    :class:`memory.universal_bridge.ProfileOutage` plus a degraded diagnostic
    instead of raising, so prompt construction and the UI can never crash a
    session - and because that object is a distinct, non-writable type, it can
    never be persisted over the real profile either.
    """
    if resolve_saturn_mode().saturn_authoritative:
        return _load_native()
    return _local_memory()


def _saturn_ready_bridge():
    """Return the bridge only when Saturn is on and already serving.

    ``is_ready`` never probes or starts Node, so recall and prompt construction
    cannot block a session or add startup latency. When the sidecar is not up,
    callers degrade to the JSON store's lexical behaviour.
    """
    try:
        from memory.universal_bridge import get_bridge
        bridge = get_bridge()
        if bridge is not None and bridge.is_ready():
            return bridge
    except Exception as exc:
        print(f"[Memory] Saturn bridge unavailable: {exc}")
    return None


def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    entries = _all_entries(memory)
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def _write_local(memory: dict) -> None:
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        MEMORY_PATH.write_text(
            json.dumps(memory, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def save_memory(memory: dict) -> None:
    """Persist ``memory`` to whichever store currently owns it.

    ``full``: the native profile write is the only durable step. It raises
    :class:`SaturnError` on failure and never touches ``long_term.json``, so a
    failed write cannot leave two stores holding different answers.

    ``legacy``/``hybrid``: the JSON write is synchronous and unconditional; the
    sidecar mirror is a best-effort second step with its own deadline. A Node,
    Ollama, sidecar, or timeout failure can therefore annotate a diagnostic,
    but it can never lose the durable update.
    """
    if not isinstance(memory, dict):
        return

    if resolve_saturn_mode().saturn_authoritative:
        _write_native(memory)
        return

    _write_local(memory)

    if not resolve_saturn_mode().dual_write:
        return

    try:
        from memory.universal_bridge import get_bridge
        bridge = get_bridge()
        if bridge is None:
            return
        timeout = _env_float("SATURN_MEMORY_WRITE_TIMEOUT", SATURN_WRITE_TIMEOUT, 0.5, 30.0)
        was_ready = bridge.is_ready()
        if not bridge.save_memory(memory, timeout=timeout) and was_ready:
            print("[Memory] Saturn mirror failed; long_term.json is up to date")
    except Exception as exc:
        print(f"[Memory] Saturn mirror skipped: {exc}")


def _write_native(memory: dict) -> dict:
    """Authoritative profile write (Stage Two). Raises on failure.

    Four checks stand between a snapshot and the store:

    * the object must carry a live, unforgeable snapshot token for a
      *successful* read. That refuses a failed-read :class:`ProfileOutage`, a
      plain dict, a hand-built ``provenance`` attribute, and a snapshot whose
      provenance was stripped by ``dict(snapshot)`` - and it keeps refusing the
      outage after the store recovers and a later read succeeds,
    * the backend must be healthy,
    * the snapshot must have been read from the namespace and store this
      bridge is verified to be serving, so a namespace-A profile can never be
      replayed over namespace B (the bridge re-checks this itself),
    * the initial migration must have completed, and
    * the sidecar revision the snapshot was read at must still be current, so a
      slow writer cannot erase a newer successful one.
    """
    from memory import universal_bridge as ub

    state = ub.snapshot_state(memory)
    if not state["ok"]:
        raise ub.SaturnWriteError(
            "refusing to overwrite the native memory store: "
            f"{state['reason']}. Nothing was written."
        )

    with _lock:
        read_ok = bool(_native_state["read_ok"])
        reason = _native_state["degraded"]

    if not read_ok:
        raise ub.SaturnWriteError(
            "refusing to overwrite the native memory store: it has not been read "
            f"successfully ({reason or 'unknown reason'}). Nothing was written; "
            "retry once Saturn is reachable."
        )
    _require_migration_ready()

    expected_revision = state["revision"] or None

    timeout = _env_float("SATURN_MEMORY_WRITE_TIMEOUT", SATURN_WRITE_TIMEOUT, 0.5, 30.0)
    try:
        bridge = _native_bridge()
        # One bridge instance for the check *and* the write: validating the
        # scope against one connection and then writing through another is
        # exactly the race this is meant to close.
        scope_of = getattr(bridge, "snapshot_scope", None)
        if callable(scope_of):
            scope = scope_of(
                memory, expected_revision=expected_revision, timeout=timeout
            )
            if not scope["ok"]:
                raise ub.SaturnSnapshotScopeError(
                    "refusing to overwrite the native memory store: "
                    f"{scope['reason']}. Nothing was written."
                )
        summary = bridge.write_profile(
            memory,
            timeout=timeout,
            autostart=True,
            include_sessions=True,
            expected_revision=expected_revision,
        )
    except ub.SaturnStaleSnapshot:
        # The store is healthy; *this* snapshot is out of date. Degrading the
        # backend here would refuse every later write for the wrong reason.
        raise
    except ub.SaturnSnapshotScopeError:
        # Same reasoning: the backend is fine, this snapshot belongs elsewhere.
        raise
    except ub.SaturnError as exc:
        _set_native_health(False, str(exc))
        raise
    except Exception as exc:
        _set_native_health(False, str(exc))
        raise ub.SaturnWriteError(f"Saturn write failed: {exc}") from exc

    with _lock:
        _native_state["degraded"] = ""
        _native_state["reported"] = ""
    return summary



def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            entry    = {"value": new_val, "updated": datetime.now().strftime("%Y-%m-%d")}
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    """Merge ``memory_update`` into the authoritative profile and persist it.

    The return shape is unchanged: the full profile, so callers can keep
    treating this as "write then read". In full mode a failed authoritative
    write propagates instead of falling back to JSON; callers report it and
    nothing is written on the wrong side of the authority boundary.

    The read-modify-write runs under ``_rmw_lock``: two extractor threads used
    to interleave here, and the slower one wrote back the profile it had read
    before the faster one's fact existed. The sidecar revision covers the
    cross-process case; this covers the in-process one.
    """
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    with _rmw_lock:
        memory = load_memory()
        if _recursive_update(memory, memory_update):
            save_memory(memory)
            print(f"[Memory] 💾 Saved: {list(memory_update.keys())}")
    return memory


def should_extract_memory(user_text: str, saturn_text: str, api_key: str = "") -> bool:
    try:
        from llm_client import client

        combined = f"User: {user_text[:300]}\nSaturn: {saturn_text[:1000]}"

        result = client.chat(
            f"Does this conversation contain ANY of the following?\n"
            f"- Personal facts (name, age, city, job, birthday, nationality)\n"
            f"- Preferences or favorites (food, color, music, sport, game, film, book, etc.)\n"
            f"- Active projects or goals the user is working on\n"
            f"- People in the user's life (friends, family, partner, colleagues)\n"
            f"- Things the user wants to do or buy in the future\n"
            f"- Any other fact worth remembering long-term\n\n"
            f"Reply only YES or NO.\n\nConversation:\n{combined}",
            system="You are a memory relevance checker. Reply only YES or NO.",
            max_tokens=5,
            temperature=0.0,
        )
        return "YES" in result.upper()

    except Exception as e:
        print(f"[Memory] ⚠️ Stage1 check failed: {e}")
        return False


def extract_memory(user_text: str, saturn_text: str, api_key: str = "") -> dict:
    try:
        from llm_client import client

        combined = f"User: {user_text[:600]}\nSaturn: {saturn_text[:300]}"

        raw = client.chat(
            f"Extract ALL memorable personal facts from this conversation. Any language.\n"
            f"Return ONLY valid JSON. Use {{}} if truly nothing is worth saving.\n\n"
            f"Category guide:\n"
            f"  identity      → name, age, birthday, city, country, job, school, nationality, language\n"
            f"  preferences   → ANY favorite or preferred thing:\n"
            f"                  favorite_food, favorite_color, favorite_music, favorite_film,\n"
            f"                  favorite_game, favorite_sport, favorite_book, favorite_artist,\n"
            f"                  favorite_country, hobbies, interests, dislikes, etc.\n"
            f"  projects      → projects being built, ongoing work, goals, ideas in progress\n"
            f"                  (e.g. saturn_ai: 'Building the Saturn assistant')\n"
            f"  relationships → people mentioned: friends, family, partner, colleagues\n"
            f"                  (e.g. best_friend_alex: 'Best friend, met in university')\n"
            f"  wishes        → future plans, things to buy, travel plans, dreams\n"
            f"  notes         → anything else worth remembering (habits, schedule, etc.)\n\n"
            f"IMPORTANT:\n"
            f"- Be LIBERAL: if something MIGHT be worth remembering, include it.\n"
            f"- Extract from BOTH user and Saturn turns.\n"
            f"- Skip: weather, reminders, search results, one-time commands.\n"
            f"- Use concise English values regardless of conversation language.\n\n"
            f"Format:\n"
            f'{{"identity":{{"name":{{"value":"User"}}}},\n'
            f' "preferences":{{"favorite_color":{{"value":"blue"}}}},\n'
            f' "projects":{{"saturn_ai":{{"value":"Saturn assistant"}}}},\n'
            f' "relationships":{{"friend_alex":{{"value":"close friend"}}}},\n'
            f' "wishes":{{"buy_guitar":{{"value":"wants an acoustic guitar"}}}},\n'
            f' "notes":{{"works_at_night":{{"value":"usually active late at night"}}}}}}\n\n'
            f"Conversation:\n{combined}\n\nJSON:",
            system="Return ONLY valid JSON. No markdown, no explanation, no extra text.",
            max_tokens=1024,
            temperature=0.2,
        )

        clean = raw.strip()
        clean = re.sub(r"```(?:json)?", "", clean).strip().rstrip("`").strip()

        if not clean or clean == "{}":
            return {}

        return json.loads(clean)

    except json.JSONDecodeError:
        return {}
    except Exception as e:
        if "429" not in str(e):
            print(f"[Memory] ⚠️ Extract failed: {e}")
        return {}


def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def _fingerprint(value: str) -> str:
    """Whitespace/case/punctuation-insensitive form used for dedupe only."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _saturn_prompt_query(memory: dict) -> str:
    """A short, deterministic query for supplementary native recall.

    The prompt has no user question attached, so the query is built from the
    identity name and the most recently updated keys. Keys are already what the
    JSON block is organised around, which keeps the native search comparable
    instead of speculative.
    """
    terms: list[str] = []
    name = _entry_value((memory.get("identity", {}) or {}).get("name"))
    if name:
        terms.append(name)

    recent: list[tuple[str, str]] = []
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            if not _entry_value(entry):
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            recent.append((updated, key))
    recent.sort(key=lambda item: item[0], reverse=True)
    for _updated, key in recent[:6]:
        pretty = _pretty(key)
        if pretty:
            terms.append(pretty)

    return " ".join(dict.fromkeys(terms))


def _saturn_prompt_suffix(memory: dict, base_text: str) -> str:
    """Optional bounded native context appended after the primary JSON block.

    Skipped entirely in legacy mode, when the sidecar is not already serving,
    or when every native hit is already represented. That makes the common
    case free of both network calls and prompt drift.
    """
    if not resolve_saturn_mode().prompt_augment:
        return ""

    bridge = _saturn_ready_bridge()
    if bridge is None:
        return ""

    query = _saturn_prompt_query(memory)
    if not query:
        return ""

    try:
        timeout = _env_float("SATURN_MEMORY_PROMPT_TIMEOUT", SATURN_PROMPT_TIMEOUT, 0.25, 5.0)
        hits = bridge.recall_entries(query, limit=6, timeout=timeout)
    except Exception as exc:
        print(f"[Memory] Saturn prompt recall skipped: {exc}")
        return ""

    local_ids: set[tuple[str, str]] = set()
    local_values: set[str] = set()
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            local_ids.add((_fingerprint(cat), _fingerprint(_pretty(key))))
            val = _fingerprint(_entry_value(entry))
            if val:
                local_values.add(val)

    known = _fingerprint(base_text)
    known_words = set(known.split())
    lines: list[str] = []
    seen: set[str] = set()
    used = 0

    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        cat = str(hit.get("category") or "").strip()
        key = str(hit.get("key") or "").strip()
        val = str(hit.get("value") or hit.get("text") or "").strip()
        if not val:
            continue
        if cat and key and (_fingerprint(cat), _fingerprint(_pretty(key))) in local_ids:
            continue
        fp = _fingerprint(val)
        if not fp or fp in seen or fp in local_values:
            continue
        # Token containment avoids hiding a short value ("art") inside an
        # unrelated longer word ("party") while still catching paraphrases of
        # a line already in the primary block.
        if fp in known or set(fp.split()).issubset(known_words):
            continue

        line = f"  - {_pretty(key).title()}: {val}" if key else f"  - {val}"
        if len(line) > 120:
            line = line[:117].rstrip() + "…"
        if (len(lines) >= SATURN_PROMPT_MAX_LINES
                or used + len(line) + 1 > SATURN_PROMPT_MAX_CHARS):
            break
        lines.append(line)
        seen.add(fp)
        used += len(line) + 1

    if not lines:
        return ""

    return (
        "\n[SATURN NATIVE MEMORY — supplementary and pre-deduplicated; "
        "the facts above are authoritative]\n"
        + "\n".join(lines)
        + "\n"
    )


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step.

    In full mode the profile passed here is native-owned, so the block is the
    prompt contract over Saturn rather than over the JSON file; a caller that
    passes ``None`` gets the authoritative profile read for it."""
    if memory is None and resolve_saturn_mode().saturn_authoritative:
        memory = load_memory()
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    base = "\n".join(out) + "\n"
    # Native Saturn context is strictly additive: bounded, deduplicated, and
    # only when the sidecar is already serving. The block above never changes.
    return base + _saturn_prompt_suffix(memory, base)


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def _native_recall_rows(bridge, query: str, limit: int) -> list[dict]:
    """Structured native hits, normalised for the merge below.

    The bridge owns Saturn's API shape; this function only guarantees the
    category/key/value fields the merged output needs and remembers whether a
    hit came from a Saturn-mirrored entry (``anchored``). Anchored hits can be
    deduplicated by identity, which is exact; unanchored hits fall back to a
    value fingerprint.
    """
    try:
        timeout = _env_float("SATURN_MEMORY_RECALL_TIMEOUT", SATURN_RECALL_TIMEOUT, 0.5, 15.0)
        hits = bridge.recall_entries(query, limit=max(4, min(24, limit * 2)), timeout=timeout)
    except Exception as exc:
        print(f"[Memory] Saturn recall skipped: {exc}")
        return []

    rows: list[dict] = []
    for hit in hits or []:
        if not isinstance(hit, dict):
            continue
        cat = str(hit.get("category") or "").strip()
        key = str(hit.get("key") or "").strip()
        val = str(hit.get("value") or hit.get("text") or "").strip()
        if not val:
            continue
        anchored = bool(hit.get("has_saturn_meta")) if "has_saturn_meta" in hit else bool(cat and key)
        rows.append({
            "category": cat or "saturn",
            "key": key or "memory",
            "value": val,
            "anchored": anchored,
        })
    return rows


def _native_context_rows(bridge, query: str, limit: int) -> list[dict]:
    """Structured rows from the engine's context assembly (full mode).

    ``/context`` recalls without reinforcing and disables per-subject
    collapsing, so calling it from recall cannot mutate ranking state and
    cannot hide a second predicate on the same subject. Everything already
    owned by the profile is deduplicated by :func:`_merge_recall_rows`; what
    survives is genuinely native-only material the profile does not contain.
    """
    try:
        timeout = _env_float("SATURN_MEMORY_RECALL_TIMEOUT", SATURN_RECALL_TIMEOUT, 0.5, 15.0)
        data = bridge.native_context(query, limit=max(4, min(24, limit * 2)), timeout=timeout)
    except Exception as exc:
        print(f"[Memory] Saturn context recall skipped: {exc}")
        return []

    rows: list[dict] = []
    for hit in (data.get("rows") if isinstance(data, dict) else None) or []:
        if not isinstance(hit, dict):
            continue
        cat = str(hit.get("category") or "").strip()
        key = str(hit.get("key") or "").strip()
        val = str(hit.get("value") or hit.get("text") or "").strip()
        if not val:
            continue
        anchored = bool(hit.get("has_saturn_meta")) if "has_saturn_meta" in hit else bool(cat and key)
        rows.append({
            "category": cat or "saturn",
            "key": key or "memory",
            "value": val,
            "anchored": anchored,
        })
    return rows


def _merge_recall_rows(
    lexical_rows: list[tuple],
    native_rows: list[dict],
    local_entries: list[tuple] | None = None,
) -> list[tuple]:
    """Union lexical and native recall deterministically.

    Rules, in order:
      1. Lexical rows are authoritative and keep their values and scores.
      2. A native hit anchored to a Saturn identity that lexical already
         returned is dropped.
      3. An anchored hit lexical missed is added with the *local* value, never
         the possibly stale mirror value.
      4. An unanchored native hit whose value matches a local entry is mapped
         back to that entry's identity, so mirrors written before metadata was
         attached cannot create a second copy.
      5. Remaining native-only hits get descending integer scores by native
         rank, below strong lexical matches, so merged order is stable.
    """
    merged = list(lexical_rows)
    seen_ids = {
        (_fingerprint(cat), _fingerprint(_pretty(key)))
        for _s, cat, key, _v in lexical_rows
    }

    local_index: dict[tuple[str, str], tuple[str, str, str]] = {}
    local_by_value: dict[str, list[tuple[str, str, str]]] = {}
    for cat, key, val in local_entries or []:
        identity = (_fingerprint(cat), _fingerprint(_pretty(key)))
        local_index[identity] = (cat, key, val)
        fp = _fingerprint(val)
        if fp:
            local_by_value.setdefault(fp, []).append((cat, key, val))
    for candidates in local_by_value.values():
        candidates.sort(key=lambda item: (item[0], item[1]))

    def add(score: int, cat: str, key: str, val: str) -> bool:
        identity = (_fingerprint(cat), _fingerprint(_pretty(key)))
        if identity in seen_ids:
            return False
        seen_ids.add(identity)
        merged.append((score, cat, key, val))
        return True

    native_values: set[str] = set()
    for rank, row in enumerate(native_rows):
        cat = str(row.get("category") or "saturn").strip() or "saturn"
        key = str(row.get("key") or "memory").strip() or "memory"
        val = str(row.get("value") or "").strip()
        if not val:
            continue
        score = max(1, 5 - rank)
        fp = _fingerprint(val)

        if row.get("anchored"):
            identity = (_fingerprint(cat), _fingerprint(_pretty(key)))
            if identity in seen_ids:
                continue
            local = local_index.get(identity)
            if local:
                cat, key, val = local
            add(score, cat, key, val)
            continue

        if fp and fp in local_by_value:
            for lcat, lkey, lval in local_by_value[fp]:
                if add(score, lcat, lkey, lval):
                    break
            continue
        if fp and fp in native_values:
            continue
        if fp:
            native_values.add(fp)
        add(score, cat, key, val)

    return merged


def search_memory(query: str, limit: int = 8) -> str:
    """Find stored facts matching `query`. Backs the recall_memory tool.

    An empty query is treated as "show me everything you know", capped - the
    model asks that when the user says "what do you remember about me?".

    In hybrid mode native Saturn recall is merged in when the sidecar is
    already serving. In full mode the profile is already native, so the merge
    uses the engine's context assembly instead, which adds only material the
    profile does not own (ingested documents, entries written by another
    client). The output format is unchanged either way: same header, same
    ``category/key: value`` lines, same overflow hint.
    """
    memory = load_memory()
    state = resolve_saturn_mode()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]
    limit  = max(1, int(limit or 1))

    local_entries: list[tuple[str, str, str]] = []
    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            local_entries.append((cat, key, val))
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    if query and state.merged_recall:
        bridge = _saturn_ready_bridge()
        if bridge is not None:
            rows = _merge_recall_rows(
                rows,
                _native_recall_rows(bridge, query, limit),
                local_entries,
            )
    elif query and state.saturn_authoritative:
        # Reads never start Node here: a proactive start belongs to the write
        # path. If the sidecar is not already serving the lexical pass above
        # still answers from the profile loaded a moment ago.
        bridge = _saturn_ready_bridge()
        if bridge is not None:
            rows = _merge_recall_rows(
                rows,
                _native_context_rows(bridge, query, limit),
                local_entries,
            )

    if not rows:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}" for _s, cat, key, val in rows[:limit]]
    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{len(rows) - len(lines)} more — search with a narrower keyword)"
             if len(rows) > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what Saturn knows, and when it learned it.

    Sorted newest first so the panel opens on what changed most recently. The
    row shape is the panel's contract; ``load_memory()`` is what makes this an
    adapter, because in full mode the rows come from the native store.
    """
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows


def saturn_mode() -> dict:
    """Public, read-only view of the resolved Saturn mode."""
    return resolve_saturn_mode().as_dict()


def memory_store_label() -> str:
    """Name the store that currently owns memory, for user-facing captions.

    The memory inspector renders this verbatim, so it stays short and factual:
    ``long_term.json`` is only mentioned while the JSON file is authoritative.
    Full mode names the native engine instead, and says so when the engine is
    unreachable, because the rows the panel shows would then be empty rather
    than reflecting a user who never saved anything.
    """
    if not resolve_saturn_mode().saturn_authoritative:
        return "memory/long_term.json"
    if degraded_status()["active"]:
        return "the Saturn native memory engine (unreachable right now)"
    return "the Saturn native memory engine"


def memory_diagnostics() -> dict:
    """Local-only diagnostics for the memory stack.

    Safe to call from a health check or a log dump: it never starts Node and
    never leaves the machine. ``bridge.diagnostics()`` may probe a loopback
    Ollama endpoint, which is the only network-shaped action here.

    ``authoritative_store`` answers "which store owns the data right now" and
    ``degraded`` explains it when that owner cannot be reached. Migration state
    (source hash, counts, rollback copy) is reported in full mode so an
    operator can tell a migrated store from an empty one.
    """
    state = resolve_saturn_mode()
    memory = load_memory()
    sessions = memory.get("sessions")
    payload = {
        "saturn": state.as_dict(),
        "authoritative_store": state.authoritative_store,
        "store_path": str(MEMORY_PATH),
        "entries": len(_all_entries(memory)),
        "sessions": len(sessions) if isinstance(sessions, list) else 0,
        "degraded": degraded_status(),
        "bridge": None,
    }
    if state.migration:
        try:
            from memory.saturn_migrate import migration_status
            payload["migration"] = migration_status(source=MEMORY_PATH)
        except Exception as exc:
            payload["migration"] = {"error": str(exc)}
    try:
        from memory.universal_bridge import get_bridge
        bridge = get_bridge()
        payload["bridge"] = (
            bridge.diagnostics() if bridge is not None
            else {"enabled": False, "reason": "backend disabled or unavailable"}
        )
    except Exception as exc:
        payload["bridge"] = {"enabled": False, "error": str(exc)}
    return payload


def remember(key: str, value: str, category: str = "notes") -> str:
    """Store one fact and report the truth about whether it landed.

    In full mode the failure message is the only thing standing between a user
    and a silently forgotten fact, so it says plainly that nothing was stored.
    """
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    state = resolve_saturn_mode()
    try:
        update_memory({category: {key: {"value": value}}})
    except Exception as exc:
        return f"Remember failed — nothing was stored for {category}/{key}: {exc}"
    if state.saturn_authoritative and degraded_status()["active"]:
        return (
            f"Remember failed — nothing was stored for {category}/{key}: "
            "the Saturn memory store is unavailable"
        )
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    """Delete one fact from whichever store currently owns it."""
    if resolve_saturn_mode().saturn_authoritative:
        return _forget_native(key, category)

    with _rmw_lock:
        memory = load_memory()
        cat    = memory.get(category, {})
        if key in cat:
            del cat[key]
            memory[category] = cat
            save_memory(memory)
            return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


def _forget_native(key: str, category: str) -> str:
    """Delete every native row for one identity, strictly.

    Deletes are authoritative too: if the sidecar cannot confirm the removal
    the caller is told nothing was deleted, rather than being told "forgotten"
    while the fact survives in the store the UI now reads from.
    """
    problem = _migration_block_reason()
    if problem:
        return (
            f"Forget failed — nothing was deleted for {category}/{key}: the initial "
            f"JSON memory migration has not completed ({problem})"
        )
    try:
        bridge = _native_bridge()
        deleted = bridge.forget_identity(category, key, strict=True)
    except Exception as exc:
        _set_native_health(False, str(exc))
        return f"Forget failed — nothing was deleted for {category}/{key}: {exc}"
    _set_native_health(True)
    return f"Forgotten: {category}/{key}" if deleted else f"Not found: {category}/{key}"


forget_memory = forget


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to the authoritative store.

    Full mode writes a native ``summary`` entry and trims to ``_SESSION_MAX``;
    the JSON list is not touched, because it is no longer read by anything.
    """
    summary = (summary or "").strip()
    if not summary:
        return

    if resolve_saturn_mode().saturn_authoritative:
        problem = _migration_block_reason()
        if problem:
            print(f"[Memory] ⚠️ Session not saved — migration incomplete: {problem}")
            return
        try:
            bridge = _native_bridge()
            bridge.save_session(
                summary[:280],
                language,
                max_sessions=_SESSION_MAX,
                timeout=_env_float("SATURN_MEMORY_WRITE_TIMEOUT", SATURN_WRITE_TIMEOUT, 0.5, 30.0),
            )
        except Exception as exc:
            _set_native_health(False, str(exc))
            print(f"[Memory] ⚠️ Session not saved — Saturn unavailable: {exc}")
            return
        _set_native_health(True)
        print(f"[Memory] 📝 Session saved ({datetime.now().strftime('%Y-%m-%d')}): {summary[:60]}…")
        return

    with _rmw_lock:
        memory   = load_memory()
        sessions = memory.get("sessions", [])
        if not isinstance(sessions, list):
            sessions = []
        entry: dict = {
            "date":    datetime.now().strftime("%Y-%m-%d"),
            "summary": summary[:280],
        }
        if language:
            entry["language"] = language
        sessions.append(entry)
        memory["sessions"] = sessions[-_SESSION_MAX:]
        with _lock:
            MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    if resolve_saturn_mode().saturn_authoritative:
        problem = _migration_block_reason()
        if problem:
            print(f"[Memory] ⚠️ pop_last_session skipped — migration incomplete: {problem}")
            return None
        try:
            return _native_bridge().pop_session()
        except Exception as exc:
            _set_native_health(False, str(exc))
            print(f"[Memory] ⚠️ pop_last_session skipped — Saturn unavailable: {exc}")
            return None

    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            MEMORY_PATH.write_text(
                json.dumps(memory, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None

# ── Chat History ──────────────────────────────────────────────────────────────
CHAT_HISTORY_PATH = get_user_data_dir() / "memory" / "chat_history.json"
MAX_HISTORY_LENGTH = 40

def load_chat_history() -> list[dict]:
    if not CHAT_HISTORY_PATH.exists():
        return []
    with _lock:
        try:
            data = json.loads(CHAT_HISTORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            print(f"[Memory] ⚠️ Chat history load error: {e}")
        return []

def save_chat_history(history: list[dict]) -> None:
    CHAT_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        CHAT_HISTORY_PATH.write_text(
            json.dumps(history[-MAX_HISTORY_LENGTH:], indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

def append_to_chat_history(user_msg: str, ai_reply: str) -> None:
    history = load_chat_history()
    if user_msg:
        history.append({"role": "user", "content": user_msg})
    if ai_reply:
        history.append({"role": "assistant", "content": ai_reply})
    save_chat_history(history)


# ── Living Knowledge Graph & Auto-Learning (Pillar 5) ─────────────────────────

_COMMON_TECH = {
    "python": "Python",
    "typescript": "TypeScript",
    "javascript": "JavaScript",
    "react": "React",
    "vue": "Vue.js",
    "angular": "Angular",
    "node.js": "Node.js",
    "nodejs": "Node.js",
    "fastapi": "FastAPI",
    "django": "Django",
    "flask": "Flask",
    "flutter": "Flutter",
    "dart": "Dart",
    "rust": "Rust",
    "golang": "Go",
    "docker": "Docker",
    "kubernetes": "Kubernetes",
    "tailwind": "Tailwind CSS",
    "postgres": "PostgreSQL",
    "sqlite": "SQLite",
    "mongodb": "MongoDB",
    "redis": "Redis",
    "pytorch": "PyTorch",
    "tensorflow": "TensorFlow",
    "next.js": "Next.js",
    "nextjs": "Next.js",
}


def auto_learn_interaction(user_text: str, saturn_text: str = "") -> dict:
    """
    Adaptive Living Knowledge Graph extractor (Pillar 5).
    Runs deterministic, zero-latency heuristic extraction across user interaction turns:
    - User name / identity facts ('my name is ...', 'call me ...')
    - City / location ('live in ...', 'based in ...')
    - Email addresses
    - Project workspaces and absolute directory paths
    - Projects being developed ('working on ... project/app', 'building ...')
    - Tech stack, tools, and frameworks (Python, React, FastAPI, Docker, etc.)
    - Explicit user preferences ('i prefer ...', 'my favorite ...')

    Persists newly discovered facts through ``update_memory()``, so the write
    follows the active Saturn mode: native Saturn in ``full`` mode,
    ``memory/long_term.json`` in ``legacy``/``hybrid``. Returns the dictionary
    of updates made.
    """
    if not user_text or len(user_text.strip()) < 4:
        return {}

    text = user_text.strip()
    low_u = text.lower()
    updates: dict[str, dict] = {}

    # 1. Identity: Name
    m_name = re.search(r"\b(?:my name is|call me|i am called)\s+([A-Z][a-zA-Z]{1,20})\b", text)
    if m_name:
        name_val = m_name.group(1).capitalize()
        if name_val.lower() not in ("saturn", "sir", "user", "admin", "echo", "assistant", "here"):
            updates.setdefault("identity", {})["name"] = {"value": name_val}

    # 2. Identity: City / Location
    m_city = re.search(r"\b(?:i live in|i'm living in|i'm based in|based in|located in)\s+([A-Za-z\s]{2,25})\b", text, re.IGNORECASE)
    if m_city:
        city_cand = m_city.group(1).strip()
        if city_cand.lower() not in ("india", "scratch", "now", "home", "here", "today", "yesterday"):
            updates.setdefault("identity", {})["city"] = {"value": city_cand.title()}

    # 3. Identity: Email
    m_email = re.search(r"\b([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)\b", text)
    if m_email:
        updates.setdefault("identity", {})["email"] = {"value": m_email.group(1).lower()}

    # 4. Projects: Absolute workspace directories
    paths = re.findall(r"\b([A-Za-z]:\\[^<>\":|?*\n\r]+?)(?=[,\s;'\"]|$)", text)
    for p in paths:
        clean_p = p.strip().rstrip("\\.,;")
        if len(clean_p) > 3 and ("\\" in clean_p):
            folder_name = Path(clean_p).name or "workspace"
            slug = re.sub(r"[^a-z0-9_]+", "_", folder_name.lower()).strip("_")
            if slug:
                updates.setdefault("projects", {})[f"path_{slug}"] = {
                    "value": f"Workspace directory: {clean_p}"
                }

    # 5. Projects: Active development
    m_proj = re.search(
        r"\b(?:working on|building|creating|developing)\s+(?:a|an|the|my)?\s*([a-zA-Z0-9_\-\s]{2,30}?)\s*(?:app|application|project|tool|bot|service|website|backend|frontend)\b",
        text,
        re.IGNORECASE
    )
    if m_proj:
        proj_name = m_proj.group(1).strip()
        slug = re.sub(r"[^a-z0-9_]+", "_", proj_name.lower()).strip("_")
        if slug and len(slug) > 2 and slug not in ("new", "this", "that", "some", "a"):
            updates.setdefault("projects", {})[slug] = {
                "value": f"Developing {proj_name} project"
            }

    # 6. Tech Stacks & Frameworks
    if any(kw in low_u for kw in ("stack", "use ", "using", "built with", "coded in", "prefer ", "framework", "language")):
        detected_tech = []
        for tech_kw, tech_name in _COMMON_TECH.items():
            if re.search(rf"\b{re.escape(tech_kw)}\b", low_u):
                detected_tech.append(tech_name)
        if detected_tech:
            updates.setdefault("preferences", {})["tech_stack"] = {
                "value": f"Tech stack: {', '.join(detected_tech)}"
            }

    # 7. Explicit Preferences
    m_pref = re.search(r"\b(?:i prefer|i like using|my preferred)\s+([a-zA-Z0-9_\s]{2,30})\b", text, re.IGNORECASE)
    if m_pref:
        pref_val = m_pref.group(1).strip()
        slug = re.sub(r"[^a-z0-9_]+", "_", pref_val.lower()).strip("_")
        if slug and len(slug) > 2 and slug not in ("to", "a", "the", "that"):
            updates.setdefault("preferences", {})[slug] = {"value": f"Prefers {pref_val}"}

    if updates:
        update_memory(updates)

    return updates


# ── One harvest per conversational turn ───────────────────────────────────────
#
# Two call sites harvest the same sentence: main.py's background memory task and
# ``core/intent_dispatcher.py`` before it routes a request. Each one ran its own
# deterministic extractor and write, so a single statement could be stored
# twice - and in unified mode both writers reach the *same* store, which made
# the duplicate visible as two rows for one fact.
#
# Both call sites now use ``harvest_turn``. The memo below collapses a second
# call for the same text inside the same process window, which covers the
# dispatcher-then-background-task ordering and repeated identical turns without
# changing what either caller sees: the first caller's updates are returned
# again instead of being recomputed and re-written.

HARVEST_MEMO_TTL = 600.0
HARVEST_MEMO_MAX = 256
_harvest_memo: dict[str, tuple[float, dict]] = {}
_harvest_lock = Lock()


def _harvest_key(text: str) -> str:
    return _fingerprint(text)[:240] or str(text or "").strip()[:240]


def harvest_turn(
    user_text: str,
    saturn_text: str = "",
    ttl: float = HARVEST_MEMO_TTL,
) -> dict:
    """Harvest one conversational turn exactly once.

    Deterministic and model-free: this is the guaranteed capture path, and the
    optional model extraction in ``main.py`` still runs afterwards. Returns the
    same ``{category: {key: {"value": ...}}}`` shape ``auto_learn_interaction``
    has always returned. A failed write is reported, never raised, because a
    turn must not fail just because memory is momentarily unavailable.
    """
    text = str(user_text or "").strip()
    if len(text) < 4:
        return {}

    key = _harvest_key(text)
    now = time.monotonic()
    with _harvest_lock:
        memo = _harvest_memo.get(key)
        if memo and now - memo[0] < max(0.0, float(ttl)):
            return dict(memo[1])

    try:
        updates = auto_learn_interaction(text, saturn_text)
    except Exception as exc:
        print(f"[Memory] ⚠️ Harvest skipped: {exc}")
        return {}

    with _harvest_lock:
        if len(_harvest_memo) >= HARVEST_MEMO_MAX:
            stale = sorted(_harvest_memo.items(), key=lambda item: item[1][0])
            for stale_key, _ in stale[: max(1, HARVEST_MEMO_MAX // 4)]:
                _harvest_memo.pop(stale_key, None)
        _harvest_memo[key] = (now, dict(updates or {}))
    return dict(updates or {})


def reset_harvest_memo() -> None:
    """Test helper: forget which turns were already harvested."""
    with _harvest_lock:
        _harvest_memo.clear()

