"""Stage Two copy-first migration: ``long_term.json`` -> native Saturn.

What this module owns
---------------------
* The mapping from the Saturn profile shape to native memor entries (delegated
  to :func:`memory.universal_bridge.profile_payload` / ``session_payload`` so
  there is only one translation).
* A *copy-first* importer. It reads ``long_term.json`` as a snapshot, takes a
  timestamped rollback copy, and only then writes to Saturn. The source file is
  never modified, truncated or rewritten: from Stage Two onwards it is a
  migration source, an export destination and a rollback point.
* Idempotency. Every migrated entry carries a stable identity - the
  ``saturn_category`` / ``saturn_key`` pair plus a deterministic entry id
  derived from it - and the importer skips anything already present with the
  same value and date. A second run performs zero writes.
* Session handling. ``sessions`` is a list in the JSON store but has no native
  equivalent, so each summary becomes a ``summary``-kind entry whose metadata
  keeps date, language and a content-derived key. Order and the "keep the last
  few" cap survive the move, and ``pop_last_session`` consumes a native entry
  instead of editing a JSON file.

What this module deliberately does not own
------------------------------------------
It does not decide which store is authoritative, it does not start the
sidecar, and it is not on the read path of hybrid or legacy mode. It is called
lazily the first time full mode needs the backend.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from memory import universal_bridge as ub
from memory.saturn_mode import resolve_saturn_mode

MIGRATION_VERSION = 1
MARKER_NAME = "saturn_migration.json"
STABLE_ID_PREFIX = "br_"
SESSION_CATEGORY = ub.SESSION_CATEGORY

_MIGRATE_ENV = "SATURN_MEMORY_MIGRATE"
_DISABLED_VALUES = ("off", "0", "false", "no", "disabled", "skip")


class MarkerWriteError(RuntimeError):
    """The migration marker could not be persisted.

    A marker is the only record that an import happened. Treating its failure
    as a warning is how a process reports success, caches it, and then imports
    the same snapshot again on the next boot (or, worse, claims a destination
    the rows are not in). The importer fails closed instead: no success, no
    cache, writes stay gated, and the retry is idempotent.
    """

_migrate_lock = Lock()
_migrated: dict | None = None
# The destination-bound identity of the cached report. A cache hit is only valid
# when version, source digest, destination store and namespace all still match,
# so pointing SATURN_MEMORY_FILE at a new engine (or changing namespace) migrates
# into that destination instead of reporting "already imported".
_migrated_key: dict | None = None


# ── Paths ─────────────────────────────────────────────────────────────────────

def memory_dir() -> Path:
    """Directory that holds the JSON store, the marker and the artifacts."""
    try:
        from core.user_paths import get_user_data_dir

        return get_user_data_dir() / "memory"
    except Exception:  # pragma: no cover - isolated environments
        return Path.home() / "SaturnAI" / "memory"


def default_source() -> Path:
    override = os.environ.get("SATURN_MEMORY_LONG_TERM")
    if override:
        return Path(override).expanduser()
    return memory_dir() / "long_term.json"


def _store_dir(base: Path | None = None) -> Path:
    """Directory of the JSON store that artifacts belong beside."""
    if base is None:
        return default_source().parent
    path = Path(base)
    if path.suffix:
        return path.parent
    return path


def marker_path(base: Path | None = None) -> Path:
    return _store_dir(base) / MARKER_NAME


def rollback_dir(base: Path | None = None) -> Path:
    return _store_dir(base) / "rollback"


def export_dir(base: Path | None = None) -> Path:
    return _store_dir(base) / "exports"


# ── Stable migration identity ─────────────────────────────────────────────────

def stable_identity(category: str, key: str) -> str:
    """Human-readable identity of a migrated fact."""
    return f"{category}/{key}"


def stable_entry_id(category: str, key: str, namespace: str | None = None) -> str:
    """Deterministic native id for a migrated fact.

    Stable across runs and machines: importing the same JSON twice reuses the
    same id, and the engine's ``preserveIds`` import then skips it. The
    namespace is always part of the identity - when none is passed the bridge's
    effective namespace is used - so the same fact migrated into two namespaces
    gets two ids instead of one row that only one of the stores can see.
    """
    scope = str(namespace or "").strip() or ub._namespace_id()
    material = f"{scope}\x1f{category}\x1f{key}"
    digest = hashlib.sha1(material.encode("utf-8")).hexdigest()[:24]
    return f"{STABLE_ID_PREFIX}{digest}"


def build_entries(memory: dict, namespace: str | None = None) -> list[dict]:
    """Build native entry payloads for a whole profile, stable order included."""
    entries: list[dict] = []
    if not isinstance(memory, dict):
        return entries

    for category, items in memory.items():
        if category == SESSION_CATEGORY or not isinstance(items, dict):
            continue
        for key, entry in items.items():
            value = ub.UniversalBridge._entry_value(entry)
            if not value:
                continue
            payload = ub.profile_payload(category, str(key), entry, namespace=namespace)
            payload["id"] = stable_entry_id(category, str(key), namespace)
            entries.append(payload)

    sessions = memory.get(SESSION_CATEGORY)
    if isinstance(sessions, list):
        for index, session in enumerate(sessions):
            payload = ub.session_payload(session, index=index, namespace=namespace)
            key = payload["meta"]["saturn_key"]
            if not payload["meta"]["saturn_value"]:
                continue
            payload["id"] = stable_entry_id(SESSION_CATEGORY, key, namespace)
            entries.append(payload)
    return entries


# ── Files ─────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def _prune(directory: Path, keep: int = 5) -> None:
    try:
        files = sorted(
            (item for item in directory.iterdir() if item.is_file() and item.suffix == ".json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return
    for stale in files[keep:]:
        try:
            stale.unlink()
        except Exception:
            pass


def load_profile(source: Path | None = None) -> tuple[dict | None, str]:
    """Read the JSON store as a snapshot. Returns ``(memory, sha256)``."""
    path = Path(source or default_source())
    if not path.exists():
        return None, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[SaturnMigrate] cannot read {path}: {exc}")
        return None, ""
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    try:
        data = json.loads(raw)
    except Exception as exc:
        print(f"[SaturnMigrate] invalid JSON in {path}: {exc}")
        return None, digest
    return (data if isinstance(data, dict) else None), digest


def write_rollback_copy(source: Path | None = None) -> Path | None:
    """Copy the JSON store aside before the first native write."""
    path = Path(source or default_source())
    if not path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    directory = rollback_dir(path)
    destination = directory / f"long_term-{stamp}.json"
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
    except Exception as exc:
        print(f"[SaturnMigrate] rollback copy failed: {exc}")
        return None
    _prune(directory)
    return destination


def read_marker(base: Path | None = None) -> dict:
    path = marker_path(base)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def migration_status(source: Path | None = None) -> dict:
    """Cheap, local-only view used by ``memory_diagnostics``."""
    source = Path(source or default_source())
    marker = read_marker(source)
    _, digest = load_profile(source)
    return {
        "version": marker.get("version", MIGRATION_VERSION),
        "source": str(source),
        "source_present": source.exists(),
        "source_sha256": digest,
        "marker": str(marker_path(source)),
        "marker_present": bool(marker),
        "imported_at": marker.get("imported_at"),
        "counts": marker.get("counts") or {},
        "rollback": marker.get("rollback"),
        "last_export": marker.get("last_export"),
        "engine_file": marker.get("engine_file"),
        "drifted": bool(marker.get("source_sha256")) and bool(digest) and marker.get("source_sha256") != digest,
    }


# ── Import ────────────────────────────────────────────────────────────────────

def import_from_json(
    source: Path | None = None,
    bridge: "ub.UniversalBridge | None" = None,
    dry_run: bool = False,
    destination: dict | None = None,
) -> dict:
    """Copy ``long_term.json`` into native Saturn, idempotently.

    Returns a report dict and never raises: migration is a background concern
    and a failure here must surface as a diagnostic, not as a lost write.
    """
    source_path = Path(source or default_source())
    report = {
        "ok": False,
        "source": str(source_path),
        "dry_run": bool(dry_run),
        "entries": 0,
        "sessions": 0,
        "imported": 0,
        "updated": 0,
        "skipped": 0,
        "rollback": None,
        "marker": str(marker_path(source_path)),
        "error": None,
        "reason": None,
        "destination": None,
    }

    memory, digest = load_profile(source_path)
    if memory is None:
        # A fresh install has nothing to migrate; that is a success, not an
        # error, and it must not leave a marker claiming data was copied.
        report["ok"] = not source_path.exists()
        report["reason"] = "source_missing" if report["ok"] else "source_unreadable"
        if not report["ok"]:
            report["error"] = "long_term.json could not be read"
        return report

    bridge = bridge or ub.get_bridge()
    destination = destination or _destination(bridge)
    report["destination"] = destination
    if destination.get("mismatch"):
        # The sidecar serves a different store than the one configured. A
        # marker written now would claim a destination this process is not
        # writing to, and the next run - against the store that really is
        # intended - would answer "already imported" about data that only ever
        # landed somewhere else. Fail closed: import nothing, claim nothing.
        report["ok"] = False
        report["reason"] = "destination_mismatch"
        report["error"] = _mismatch_error(destination)
        return report

    entries = build_entries(memory, namespace=destination["namespace"])
    report["entries"] = len([item for item in entries if item["meta"]["saturn_category"] != SESSION_CATEGORY])
    report["sessions"] = len([item for item in entries if item["meta"]["saturn_category"] == SESSION_CATEGORY])

    if not entries:
        report["ok"] = True
        report["reason"] = "source_empty"
        if not dry_run and not _record_marker(
            report, digest, destination, rollback=None, base=source_path
        ):
            return report
        return report

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

    if dry_run:
        try:
            index = bridge.native_index()
        except Exception as exc:
            report["error"] = f"Saturn export failed: {exc}"
            return report
        for entry in entries:
            meta = entry["meta"]
            rows = index.get((meta["saturn_category"], meta["saturn_key"]), [])
            if _matches(rows, meta):
                report["skipped"] += 1
            elif rows:
                report["updated"] += 1
            else:
                report["imported"] += 1
        report["ok"] = True
        return report

    rollback = write_rollback_copy(source_path)
    report["rollback"] = str(rollback) if rollback else None

    try:
        index = bridge.native_index()
        for entry in entries:
            meta = entry["meta"]
            identity = (meta["saturn_category"], meta["saturn_key"])
            rows = index.get(identity, [])
            if _matches(rows, meta):
                report["skipped"] += 1
                continue
            if rows:
                if _native_is_newer(rows, meta):
                    # A retry must never roll the native store back to the
                    # snapshot the JSON file still holds: the native value was
                    # written after the migration attempt and is newer.
                    report["skipped"] += 1
                    continue
                # Replace: write the new value first (without the stable id, so
                # the engine cannot skip an id that already exists), then drop
                # the stale rows. The JSON snapshot is still on disk, and a
                # rollback copy was taken above.
                created = bridge.import_entries([{**entry, "id": None}])
                if not int(created.get("imported") or 0):
                    report["error"] = f"Saturn rejected {identity[0]}/{identity[1]}"
                    return report
                report["updated"] += 1
                for row in rows:
                    if row.get("id"):
                        bridge.delete_entry(str(row["id"]))
                continue

            created = bridge.import_entries([entry])
            if int(created.get("imported") or 0):
                report["imported"] += 1
            elif int(created.get("skipped") or 0):
                report["skipped"] += 1
            else:
                report["error"] = f"Saturn skipped {identity[0]}/{identity[1]} without reporting a result"
                return report
    except ub.SaturnError as exc:
        report["error"] = str(exc)
        return report
    except Exception as exc:
        report["error"] = f"Saturn import failed: {exc}"
        return report

    report["ok"] = True
    if not _record_marker(report, digest, destination, rollback=rollback, base=source_path):
        return report
    return report


def _record_marker(
    report: dict,
    digest: str,
    destination: dict,
    rollback: Path | None,
    base: Path | None,
) -> bool:
    """Persist the marker and turn a failure into a failed report.

    The import itself is already done and is idempotent, so the report keeps
    its counts - but it is not a success: the caller must not cache it, the
    write gate must stay closed, and the next run must try again. Because the
    stable ids are already in the store, that retry skips them instead of
    overwriting whatever the native store has learned since.
    """
    try:
        _write_marker(report, digest, destination, rollback=rollback, base=base)
    except MarkerWriteError as exc:
        report["ok"] = False
        report["reason"] = "marker_write_failed"
        report["error"] = (
            f"{exc}. The import completed but is not recorded, so it will be "
            "retried; existing entries are skipped, not overwritten"
        )
        return False
    return True


def _matches(rows: list[dict], meta: dict) -> bool:
    wanted_value = meta["saturn_value"]
    wanted_date = meta.get("saturn_updated") or ""
    for row in rows or []:
        if row.get("value") != wanted_value:
            continue
        if wanted_date and row.get("updated") != wanted_date:
            continue
        return True
    return False


def _native_is_newer(rows: list[dict], meta: dict) -> bool:
    """True when a native row was updated after the JSON entry it came from.

    ``saturn_updated`` is a date (``YYYY-MM-DD``), so a plain string compare is
    enough and keeps the check independent of timezone parsing.
    """
    wanted = str(meta.get("saturn_updated") or "").strip()
    for row in rows or []:
        updated = str(row.get("updated") or "").strip()
        if updated and wanted and updated > wanted:
            return True
    return False


def _destination(bridge) -> dict:
    """Identity of the store a migration writes into.

    Markers and process caches are bound to this, not just to the source file:
    pointing ``SATURN_MEMORY_FILE`` or the namespace at a *different* store must
    migrate into it instead of reporting "already imported" from a marker that
    belongs to another destination.

    The *store* half comes from the serving side, not from configuration. When
    the sidecar answers ``/diagnostics``, the file it reports is the store
    ``import_entries`` will actually write to; a configured ``wanted.jsonl``
    that disagrees with a served ``served.jsonl`` is recorded in ``mismatch``
    and the importer refuses to run, so nothing is ever marked imported under a
    name the process is not writing to. The *namespace* half is the bridge's
    own, never the server's default.
    """
    configured = None
    served = None
    verified = False
    mismatch = ""
    try:
        configured = str(getattr(bridge, "file", "") or "") or None
    except Exception:
        configured = None
    namespace = ""
    try:
        namespace = str(getattr(bridge, "namespace", "") or "").strip()
    except Exception:
        namespace = ""
    probe = getattr(bridge, "destination_identity", None)
    if callable(probe):
        try:
            identity = probe(verify=True)
        except Exception:
            identity = None
        if isinstance(identity, dict):
            reported = identity.get("reported")
            reported = reported if isinstance(reported, dict) else {}
            served = str(
                identity.get("engine_file") or reported.get("engine_file") or ""
            ).strip() or None
            verified = bool(identity.get("verified"))
            mismatch = str(identity.get("mismatch") or "")
    if not namespace:
        namespace = str(
            os.environ.get("SATURN_MEMORY_NAMESPACE")
            or os.environ.get("SATURN_MEMORY_NAMESPACE")
            or ""
        ).strip() or "saturn"
    engine_file = served or configured
    identity = hashlib.sha1(
        f"{engine_file or ''}\x1f{namespace}".encode("utf-8")
    ).hexdigest()[:24]
    return {
        "engine_file": engine_file,
        "namespace": namespace,
        "id": identity,
        "configured_file": configured,
        "served_file": served,
        "verified": verified,
        "mismatch": mismatch,
    }


def _mismatch_error(destination: dict) -> str:
    """The diagnostic for a destination the process is not writing to."""
    return (
        f"refusing to migrate: {destination.get('mismatch')}; nothing was imported "
        f"and {destination.get('configured_file') or 'the configured store'} was not "
        "recorded as a migration destination"
    )


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
    namespace = str(marker.get("namespace") or "").strip()
    identity = hashlib.sha1(
        f"{engine_file or ''}\x1f{namespace}".encode("utf-8")
    ).hexdigest()[:24]
    return {"engine_file": engine_file, "namespace": namespace, "id": identity}


def _migration_key(
    bridge,
    digest: str,
    source: Path | None = None,
    destination: dict | None = None,
) -> dict:
    destination = destination or _destination(bridge)
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

    Version, source snapshot, namespace and destination store identity must all
    agree. Anything else means this run is a real migration into a new place.
    """
    if not marker or not marker.get("imported_at"):
        return False
    if int(marker.get("version") or 0) != int(key.get("version") or 0):
        return False
    if not key.get("source_sha256") or marker.get("source_sha256") != key["source_sha256"]:
        return False
    destination = _marker_destination(marker)
    if destination["id"] != key.get("destination_id"):
        return False
    return destination["namespace"] == str(key.get("namespace") or "")


def _write_marker(
    report: dict,
    digest: str,
    destination: dict,
    rollback: Path | None,
    base: Path | None = None,
) -> None:
    previous = read_marker(base)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # ``imported_at`` is the "first import into *this* destination" stamp. When
    # the destination identity changed, the old stamp describes a copy that
    # lives somewhere else, so it must not be inherited.
    same_destination = bool(previous) and _marker_destination(previous)["id"] == destination.get("id")
    first_import = (previous.get("imported_at") or previous.get("last_import_at")) if same_destination else None
    marker = {
        "version": MIGRATION_VERSION,
        "source": report.get("source"),
        "source_sha256": digest,
        "engine_file": destination.get("engine_file"),
        "namespace": destination.get("namespace"),
        "destination": dict(destination),
        "stable_identity": "namespace+saturn_category+saturn_key -> br_<sha1[:24]>",
        "imported_at": first_import or stamp,
        "last_import_at": stamp,
        "counts": {
            "entries": report.get("entries", 0),
            "sessions": report.get("sessions", 0),
            "imported": report.get("imported", 0),
            "updated": report.get("updated", 0),
            "skipped": report.get("skipped", 0),
        },
        "rollback": str(rollback) if rollback else previous.get("rollback"),
        "last_export": previous.get("last_export"),
    }
    try:
        _atomic_write(marker_path(base), marker)
    except Exception as exc:
        raise MarkerWriteError(
            f"cannot persist the migration marker at {marker_path(base)}: {exc}"
        ) from exc


def ensure_migrated(
    bridge: "ub.UniversalBridge | None" = None,
    source: Path | None = None,
    force: bool = False,
) -> dict:
    """Run the importer at most once per process *and* per destination.

    A marker recording the same source snapshot, migration version, namespace
    and destination store is honoured across restarts. That matters more than
    it looks: full mode keeps editing the native store while
    ``long_term.json`` stays frozen, so re-importing an unchanged snapshot on
    every boot would quietly revert newer native values back to whatever the
    file said at migration time. A changed file, a changed namespace or a
    different ``SATURN_MEMORY_FILE`` is still imported, because that is a
    deliberate change of destination.

    Failures are not cached, so a later call retries once the sidecar is up.
    """
    global _migrated, _migrated_key
    if not resolve_saturn_mode().migration:
        return {"ok": True, "skipped": "migration_not_enabled"}
    if str(os.environ.get(_MIGRATE_ENV, "auto")).strip().lower() in _DISABLED_VALUES:
        return {"ok": True, "skipped": "disabled_by_env"}

    source_path = Path(source or default_source())
    bridge = bridge or ub.get_bridge()
    _, digest = load_profile(source_path)
    # Verification happens once per run: the key, the refusal and the import
    # must all describe the same serving store, or a mismatch could be checked
    # against one endpoint and the write sent to another. A missing source is
    # not a migration, so it is reported as such instead of as a mismatch.
    destination = _destination(bridge)
    if destination.get("mismatch") and source_path.exists():
        report = {
            "ok": False,
            "reason": "destination_mismatch",
            "source": str(source_path),
            "marker": str(marker_path(source_path)),
            "imported": 0,
            "updated": 0,
            "skipped": 0,
            "rollback": None,
            "destination": destination,
            "error": _mismatch_error(destination),
        }
        print(f"[SaturnMigrate] import deferred: {report['error']}")
        return report
    key = _migration_key(bridge, digest, source_path, destination)

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
                    "updated": 0,
                    "skipped": int((marker.get("counts") or {}).get("skipped") or 0),
                    "rollback": marker.get("rollback"),
                    "destination": _marker_destination(marker),
                    "error": None,
                }
                _migrated_key = key
                return _migrated

        result = import_from_json(source=source_path, bridge=bridge, destination=destination)
        if result.get("ok"):
            _migrated = result
            _migrated_key = key
        elif result.get("error"):
            print(f"[SaturnMigrate] import deferred: {result['error']}")
        return result


def reset_migration_state() -> None:
    """Test helper: forget the once-per-process guard."""
    global _migrated, _migrated_key
    with _migrate_lock:
        _migrated = None
        _migrated_key = None


# ── Export / rollback artifact ────────────────────────────────────────────────

def export_native_profile(
    destination: Path | None = None,
    bridge: "ub.UniversalBridge | None" = None,
    base: Path | None = None,
) -> dict:
    """Write the native store as a ``long_term.json``-shaped artifact.

    This is the rollback path: copy the artifact over ``long_term.json`` and set
    ``SATURN_MEMORY_MODE=legacy`` to return to a file-owned store. The
    artifact contains only the profile shape so it can be used verbatim.
    """
    bridge = bridge or ub.get_bridge()
    if bridge is None:
        return {"ok": False, "error": "Saturn backend is disabled", "path": None}
    try:
        bridge.require()
        memory = bridge.load_memory(include_sessions=True)
    except ub.SaturnError as exc:
        return {"ok": False, "error": str(exc), "path": None}
    except Exception as exc:
        return {"ok": False, "error": f"Saturn export failed: {exc}", "path": None}
    if isinstance(memory, ub.ProfileOutage):
        # A rollback artifact built from an outage would overwrite the *good*
        # artifact with an empty profile, so the export fails closed.
        return {"ok": False, "error": f"Saturn export failed: {memory.reason}", "path": None}
    if memory is None:
        return {"ok": False, "error": "Saturn export returned nothing", "path": None}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = Path(destination) if destination else export_dir(base) / f"long_term-{stamp}.json"
    try:
        _atomic_write(target, memory)
    except Exception as exc:
        return {"ok": False, "error": f"cannot write {target}: {exc}", "path": str(target)}
    _prune(target.parent)

    previous = read_marker(base)
    if previous:
        previous["last_export"] = str(target)
        try:
            _atomic_write(marker_path(base), previous)
        except Exception:
            pass

    return {
        "ok": True,
        "path": str(target),
        "entries": sum(len(items) for items in memory.values() if isinstance(items, dict)),
        "sessions": len(memory.get(SESSION_CATEGORY) or []),
        "mode": resolve_saturn_mode().mode,
    }
