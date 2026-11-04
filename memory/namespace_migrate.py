"""Copy one namespace's entries into another namespace.

This is an explicit, one-shot maintenance tool. Nothing imports it, so the
running agent never executes it, and it writes nothing unless ``--apply`` is
passed: the default is a read-only dry run that reports what would move.

Why a copy and not a rename
---------------------------
A namespace is a field on every stored row, not a directory. No engine
operation rewrites it in place, and changing the default only hides the old
rows: a ``saturn`` reader would no longer see rows labelled ``origin``. So the
migration reads the source, imports fresh rows into the destination and leaves
the source untouched as a rollback artifact.

What is copied
--------------
* rows labelled exactly ``source``;
* the fields the import path understands: text, kind, tags, timestamps,
  importance/confidence, the subject/predicate/value triple, meta, and the
  document fields.

What is not
-----------
* rows with no namespace: the engine already shows those to every namespace,
  so copying them would create duplicates;
* rows whose validity window has closed unless ``--include-history`` is passed:
  recall never shows them, and the engine may revive an expired row on import,
  so the default is to leave them where they are and report the count;
* ids: the destination mints new ones. The engine's id index is global to the
  file, so preserving an id that already exists in the source makes the import
  skip the row instead of copying it;
* ``supersedes``/``supersededBy`` and ``seen``/``accessCount``/
  ``lastAccessedAt``: derived state the destination recomputes;
* vectors: the engine re-embeds when semantic mode is on, and none is needed
  when it is off.

Safety
------
* ``--apply`` refuses while a queued write is still pending, and while another
  process owns the sidecar unless ``--allow-shared`` says otherwise;
* the store and its outbox are copied next to themselves before the first
  write;
* rows already present in the destination (same kind/text/time/identity) are
  skipped, so an interrupted run can be retried without duplicating anything;
* a marker is written only after the destination is verified, and a verified
  marker short-circuits a rerun.

Usage
-----
    python -m memory.namespace_migrate --from origin --to saturn
    python -m memory.namespace_migrate --from origin --to saturn --apply

Exit codes: 0 report produced, 1 read/import failure, 2 refused to write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from memory.universal_bridge import UniversalBridge

REPORT_VERSION = 1
DEFAULT_PAGE_SIZE = 500
MAX_PAGES = 10_000

# Fields the import path accepts. Anything else is derived state the
# destination recomputes, or an id/namespace this tool must not carry over.
_IMPORT_FIELDS = (
    "text",
    "kind",
    "tags",
    "at",
    "validFrom",
    "validTo",
    "importance",
    "confidence",
    "meta",
    "session",
    "subject",
    "predicate",
    "value",
    "src",
    "docId",
    "title",
    "chunk",
    "ttlDays",
)


def _label(entry: dict) -> str:
    return str(entry.get("namespace") or "").strip()


def _fingerprint(entry: dict) -> str:
    """Identity of a row, independent of its id and namespace.

    Used for two things: refusing to copy a row that already exists in the
    destination, and proving afterwards that every source row arrived. The text
    is whitespace-normalised because that is what the engine stores, so the
    same row fingerprints the same on both sides.
    """
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    parts = [
        str(entry.get("kind") or ""),
        " ".join(str(entry.get("text") or "").split()),
        str(entry.get("at") or ""),
        str(meta.get("saturn_category") or ""),
        str(meta.get("saturn_key") or ""),
        str(meta.get("saturn_value") or ""),
    ]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()


def _clean_entry(entry: dict) -> dict | None:
    """Reduce an exported row to what the import path may legally receive."""
    clean = {field: entry[field] for field in _IMPORT_FIELDS if field in entry}
    text = str(clean.get("text") or "").strip()
    if not text:
        return None
    clean["text"] = text
    meta = clean.get("meta")
    if meta is not None and not isinstance(meta, dict):
        clean.pop("meta", None)
    return clean


def _is_active(entry: dict) -> bool:
    """Whether the engine still treats a row as live.

    The engine archives a row by setting ``validTo``; a row without one never
    expires. A timestamp this tool cannot parse counts as live: skipping a row
    over a format it does not recognise would silently lose memory.
    """
    raw = str(entry.get("validTo") or "").strip()
    if not raw:
        return True
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment > datetime.now(timezone.utc)


def _collect(
    bridge,
    namespace: str,
    page_size: int,
    timeout: float | None,
    include_history: bool = True,
) -> list[dict]:
    """Read every row in a namespace, page by page.

    ``include_history`` is on so the store's archived rows are counted rather
    than silently dropped from the totals; the caller decides which of them to
    copy.
    """
    rows: list[dict] = []
    offset = 0
    for _ in range(MAX_PAGES):
        page = bridge.list_entries(
            limit=page_size,
            offset=offset,
            namespace=namespace,
            timeout=timeout,
            include_history=include_history,
        )
        batch = [row for row in (page.get("entries") or []) if isinstance(row, dict)]
        rows.extend(batch)
        total = int(page.get("total") or 0)
        offset += len(batch)
        if not batch or (total and offset >= total):
            break
    else:
        raise RuntimeError(f"listing namespace {namespace!r} did not terminate")
    return rows


def _store_file(bridge) -> Path:
    data = bridge.status(timeout=1.5)
    raw = str((data or {}).get("file") or getattr(bridge, "file", "") or "")
    return Path(raw) if raw else Path("memory.jsonl")


def _marker_path(store_file: Path) -> Path:
    return Path(str(store_file) + ".namespace-migration.json")


def _read_marker(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_marker(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _backup(store_file: Path) -> list[str]:
    """Copy the store and its outbox beside themselves before the first write."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    made: list[str] = []
    for suffix in ("", ".outbox.json"):
        original = Path(str(store_file) + suffix)
        if not original.exists():
            continue
        target = Path(str(original) + f".bak-ns-{stamp}")
        shutil.copy2(original, target)
        made.append(str(target))
    return made


def _marker_payload(report: dict, store_file: Path) -> dict:
    return {
        "version": REPORT_VERSION,
        "applied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "store": str(store_file),
        "source": report["source"],
        "destination": report["destination"],
        "source_labeled": report["source_labeled"],
        "imported": report["imported"],
        "already_present": report["already_present"],
        "destination_after": report["destination_after"],
        "verified": report["verified"],
    }


def migrate_namespace(
    source: str,
    destination: str,
    *,
    bridge=None,
    apply: bool = False,
    force: bool = False,
    backup: bool = True,
    allow_shared: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float | None = 30.0,
    close_bridge: bool | None = None,
    include_history: bool = False,
) -> dict:
    """Copy labelled rows from ``source`` into ``destination``.

    Read-only unless ``apply`` is true. Returns a report dict; a false ``ok``
    always carries a ``reason`` and never a half-written marker.
    """
    source = str(source or "").strip()
    destination = str(destination or "").strip()
    report: dict = {
        "version": REPORT_VERSION,
        "ok": False,
        "dry_run": not apply,
        "source": source,
        "destination": destination,
        "reason": "",
        "blockers": [],
        "store": "",
        "pending_writes": 0,
        "source_total": 0,
        "source_labeled": 0,
        "source_expired": 0,
        "include_history": bool(include_history),
        "unlabeled_skipped": 0,
        "destination_before": 0,
        "destination_unlabeled": 0,
        "already_present": 0,
        "to_import": 0,
        "imported": 0,
        "skipped_by_engine": 0,
        "verified": False,
        "missing_after": 0,
        "destination_after": 0,
        "backup": [],
        "marker": "",
    }
    if not source or not destination or source == destination:
        report["reason"] = "source and destination must be two different, non-empty namespaces"
        return report

    created_bridge = bridge is None
    if bridge is None:
        bridge = UniversalBridge()
    if close_bridge is None:
        close_bridge = created_bridge

    try:
        if not bridge.available(autostart=True):
            report["reason"] = (
                "memory store unavailable: "
                + (str(getattr(bridge, "_last_error", "") or "not reachable"))
            )
            return report

        store_file = _store_file(bridge)
        marker_path = _marker_path(store_file)
        report["store"] = str(store_file)
        report["marker"] = str(marker_path)

        marker = _read_marker(marker_path)
        if (
            not force
            and marker.get("verified") is True
            and marker.get("source") == source
            and marker.get("destination") == destination
        ):
            report.update(
                ok=True,
                verified=True,
                reason="already_migrated",
                source_labeled=int(marker.get("source_labeled") or 0),
                # This run copied nothing: the marker says a previous run
                # verified every labelled row is in the destination.
                imported=0,
                to_import=0,
                already_present=int(marker.get("source_labeled") or 0),
                destination_after=int(marker.get("destination_after") or 0),
            )
            return report

        owns = bool(getattr(bridge, "_owns_process", False))
        pending = int(bridge.pending_writes() or 0)
        if apply and not owns and not allow_shared:
            report["blockers"].append("shared_store")
        if apply and not report["blockers"] and pending:
            # A queued write carries the source namespace and would be dropped
            # by replay after the switch, so it has to land first.
            bridge.flush_outbox(timeout=timeout)
            pending = int(bridge.pending_writes() or 0)
        report["pending_writes"] = pending
        if pending:
            report["blockers"].append("pending_writes")
        if apply and report["blockers"]:
            report["reason"] = (
                "another process owns the sidecar; stop it or pass --allow-shared"
                if "shared_store" in report["blockers"]
                else "queued writes are still pending; start the app once to replay them"
            )
            return report

        source_rows = _collect(bridge, source, page_size, timeout)
        dest_rows = _collect(bridge, destination, page_size, timeout)
        report["source_total"] = len(source_rows)
        # Expired rows stay put unless explicitly asked for: recall never shows
        # them and an import may revive them, which would resurrect memory the
        # engine had already retired.
        report["source_expired"] = sum(
            1 for row in source_rows if _label(row) == source and not _is_active(row)
        )
        source_labeled = [
            row
            for row in source_rows
            if _label(row) == source and (include_history or _is_active(row))
        ]
        dest_labeled = [row for row in dest_rows if _label(row) == destination]
        report["source_labeled"] = len(source_labeled)
        report["unlabeled_skipped"] = sum(1 for row in source_rows if not _label(row))
        report["destination_before"] = len(dest_labeled)
        report["destination_unlabeled"] = sum(1 for row in dest_rows if not _label(row))

        present = {_fingerprint(row) for row in dest_rows}
        pending_rows: list[dict] = []
        already = 0
        for row in source_labeled:
            fingerprint = _fingerprint(row)
            if fingerprint in present:
                already += 1
                continue
            clean = _clean_entry(row)
            if clean is None:
                continue
            pending_rows.append(clean)
            present.add(fingerprint)
        report["already_present"] = already
        report["to_import"] = len(pending_rows)

        if not apply:
            report["ok"] = True
            return report

        if not pending_rows:
            report["ok"] = True
            report["verified"] = True
            report["reason"] = "nothing_to_do"
            report["destination_after"] = len(dest_labeled)
            _write_marker(marker_path, _marker_payload(report, store_file))
            return report

        if backup:
            report["backup"] = _backup(store_file)

        previous = bridge.namespace
        bridge.namespace = destination
        import_error = ""
        result: dict = {}
        try:
            result = bridge.import_entries(pending_rows, timeout=timeout, preserve_ids=False)
        except Exception as exc:
            # A throw can come after part of the batch committed. Verify below
            # instead of assuming either outcome.
            import_error = f"{type(exc).__name__}: {exc}"
        finally:
            bridge.namespace = previous
        report["imported"] = int(result.get("imported") or 0)
        report["skipped_by_engine"] = int(result.get("skipped") or 0)

        after_rows = _collect(bridge, destination, page_size, timeout)
        after_labeled = [row for row in after_rows if _label(row) == destination]
        after = {_fingerprint(row) for row in after_rows}
        missing = [row for row in source_labeled if _fingerprint(row) not in after]
        report["destination_after"] = len(after_labeled)
        report["missing_after"] = len(missing)
        report["verified"] = not missing
        report["ok"] = not missing
        if missing:
            report["reason"] = (
                (f"import failed: {import_error}; " if import_error else "")
                + "destination is missing source rows; marker not written"
            )
            return report

        _write_marker(marker_path, _marker_payload(report, store_file))
        report["reason"] = "migrated" if not import_error else f"migrated ({import_error})"
        return report
    except Exception as exc:
        report["reason"] = f"{type(exc).__name__}: {exc}"
        return report
    finally:
        if close_bridge:
            try:
                bridge.stop()
            except Exception:
                pass


def _human(report: dict) -> str:
    lines = [
        f"source       : {report['source']} - {report['source_labeled']} labelled rows, "
        f"{report['unlabeled_skipped']} unlabelled skipped",
    ]
    if report["source_expired"]:
        lines.append(
            f"expired      : {report['source_expired']} archived rows left behind "
            "(pass --include-history to copy them)"
        )
    lines += [
        f"destination  : {report['destination']} - {report['destination_before']} rows before",
        f"already there: {report['already_present']}",
        f"to import    : {report['to_import']}",
    ]
    if report["dry_run"]:
        lines.append("mode         : DRY RUN - nothing was written")
    else:
        lines.append(
            f"imported     : {report['imported']} "
            f"(engine skipped {report['skipped_by_engine']})"
        )
        lines.append(
            f"after        : {report['destination_after']} rows, "
            f"verified={report['verified']}, missing={report['missing_after']}"
        )
        if report["backup"]:
            lines.append("backup       : " + ", ".join(report["backup"]))
        if report["marker"]:
            lines.append(f"marker       : {report['marker']}")
    if report["blockers"]:
        lines.append("blockers     : " + ", ".join(report["blockers"]))
    if report["reason"]:
        lines.append(f"result       : {report['reason']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory.namespace_migrate",
        description="Copy one memory namespace into another. Read-only unless --apply is passed.",
    )
    parser.add_argument("--from", dest="source", required=True, help="namespace to read")
    parser.add_argument("--to", dest="destination", required=True, help="namespace to fill")
    parser.add_argument("--apply", action="store_true", help="write the destination")
    parser.add_argument("--force", action="store_true", help="ignore a verified marker and re-check")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the pre-write copy of the store (not recommended)",
    )
    parser.add_argument(
        "--allow-shared",
        action="store_true",
        help="allow writing while another process owns the sidecar",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--include-history",
        action="store_true",
        help="also copy rows whose validity window has closed",
    )
    parser.add_argument("--json", action="store_true", help="print the whole report as JSON")
    args = parser.parse_args(argv)

    report = migrate_namespace(
        args.source,
        args.destination,
        apply=args.apply,
        force=args.force,
        backup=not args.no_backup,
        allow_shared=args.allow_shared,
        page_size=max(1, int(args.page_size)),
        timeout=args.timeout,
        include_history=args.include_history,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False) if args.json else _human(report))
    if report.get("ok"):
        return 0
    return 2 if report.get("blockers") else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
