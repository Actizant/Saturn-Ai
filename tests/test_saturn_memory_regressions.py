"""Regressions for the ten blockers fixed in the Saturn memory hardening pass.

Each test here exists because a specific failure mode was possible:

1. ``/export`` was read unscoped, so a profile reconcile could index - and then
   delete - another namespace's rows, and a cortex clear could wipe a whole
   neighbouring store.
2. Two in-process read-modify-write cycles could interleave, and an
   authoritative write computed from an old snapshot could erase a newer
   successful one.
3. A failed initial JSON migration left authoritative writes running against a
   store that might be missing facts.
4. Migration markers and process caches described the *source* but not the
   destination, so a new ``SATURN_MEMORY_FILE`` or namespace reported
   "already imported".
5. Migrated profile and session payloads carried no namespace, so recall could
   not find them and re-import could land them in the wrong store.
6. Extracted subject/predicate/value were dropped, so a changed preference
   coexisted with its own replacement instead of superseding it.
7. A failing ``save_memory`` tool branch could escape into the live receive
   loop, and reported nothing truthful about the failure.
8. Endpoint validation used prefix checks, so ``127.0.0.1.evil.example``
   received memory traffic, and redirects were followed.
9. The cortex rollback "copy" was a byte copy, which misses committed rows
   still sitting in the write-ahead log.
10. Migrated memories were identified by a 60-character slug, so rows sharing a
    long prefix - or repeated episodes - collapsed into one entry.

The double below is not a memory engine. It implements only the HTTP surface
semantics these tests depend on (namespace scoping, a mutating revision,
compare-and-swap ``/patch``, id-preserving import and structured versioning),
and everything else runs production code.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import memory.cortex_saturn as cu
import memory.memory_manager as mm
import memory.saturn_migrate as saturn_migrate
import memory.universal_bridge as ub
from core.memory_cortex import MemoryCortex

SIDECAR_JS = Path(__file__).resolve().parent.parent / "memory" / "sidecar.js"
NODE_BINARY = shutil.which("node") or shutil.which("node.exe")

DEDUPE_KINDS = {"note", "fact", "preference"}
ENGINE_KINDS = {"note", "turn", "episode", "fact", "preference", "summary", "document"}


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(token) > 2}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _not_found(path: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(path, 404, "Not Found", {}, None)


class RevisionedStore(ub.UniversalBridge):
    """Sidecar stand-in with the semantics the fixes are about.

    * ``/export`` and ``/entries`` are namespace-scoped and ``/export`` reports
      a revision.
    * every mutation moves that revision, and ``POST /patch`` is a real
      compare-and-swap: a stale ``expectedRevision`` answers 409.
    * ``/import`` preserves ids and skips ids that already exist anywhere in
      the store (the engine's global ``_docs`` lookup).
    * ``/remember`` applies the engine's structured versioning: a different
      value on the same ``(kind, namespace, subject, predicate)`` supersedes.
    """

    def __init__(self, namespace: str = "origin", file: Path | None = None) -> None:
        super().__init__()
        self.namespace = namespace
        self.file = Path(file) if file is not None else self.file
        self.rows: "OrderedDict[str, dict]" = OrderedDict()
        self.tombstones: set[str] = set()
        self.revision = 0
        self.calls: list[tuple[str, str]] = []
        self.patches: list[dict] = []
        self._seq = 0
        # A double is serving by definition; never let is_ready() probe or
        # start Node.
        self._available = True

    # -- bookkeeping -----------------------------------------------------
    def _bump(self) -> str:
        self.revision += 1
        return str(self.revision)

    def active(self) -> list[dict]:
        return [
            row for row in self.rows.values()
            if row["id"] not in self.tombstones and not row.get("validTo")
        ]

    def in_namespace(self, namespace: str) -> list[dict]:
        return [
            row for row in self.active()
            if not row.get("namespace") or row["namespace"] == namespace
        ]

    def history(self, entry_id: str) -> dict:
        return self.rows[entry_id]

    # -- engine ----------------------------------------------------------
    def _next_id(self) -> str:
        self._seq += 1
        return f"m_{self._seq:04x}{self._seq:04x}"

    def _entry(self, payload: dict, entry_id: str) -> dict:
        kind = str(payload.get("kind") or "note")
        if kind not in ENGINE_KINDS:
            kind = "note"
        text = " ".join(str(payload.get("text") or "").split()).strip()
        value = payload.get("value")
        return {
            "id": entry_id,
            "text": text,
            "value": value if value is not None else text[:200],
            "kind": kind,
            "tags": list(payload.get("tags") or []),
            "src": str(payload.get("src") or "origin"),
            "namespace": str(payload.get("namespace") or self.namespace),
            "subject": payload.get("subject"),
            "predicate": payload.get("predicate"),
            "importance": float(payload.get("importance") or 0.5),
            "at": str(payload.get("at") or "2026-01-01T12:00:00"),
            "seen": 1,
            "accessCount": 0,
            "meta": dict(payload.get("meta") or {}),
        }

    @staticmethod
    def _identity(entry: dict) -> tuple:
        return (
            entry.get("namespace"),
            entry.get("kind"),
            entry.get("subject"),
            entry.get("predicate"),
            entry.get("value"),
            entry.get("text"),
        )

    @staticmethod
    def _versioned(entry: dict) -> bool:
        return (
            entry.get("kind") in ("fact", "preference")
            and bool(entry.get("subject"))
            and bool(entry.get("predicate"))
            and entry.get("value") not in (None, "")
        )

    def remember(self, payload: dict) -> dict:
        entry = self._entry(payload, self._next_id())
        if not entry["text"]:
            return {"error": "EMPTY"}

        if bool(payload.get("dedupe", True)) and entry["kind"] in DEDUPE_KINDS:
            wanted = self._identity(entry)
            for existing in self.active():
                if self._identity(existing) == wanted:
                    existing["seen"] = int(existing.get("seen") or 1) + 1
                    existing["at"] = entry["at"]
                    self._bump()
                    return {"id": existing["id"], "merged": True}

        superseded: list[str] = []
        if self._versioned(entry):
            for old in self.active():
                if old["id"] == entry["id"] or not self._versioned(old):
                    continue
                if (
                    old["kind"] == entry["kind"]
                    and old.get("namespace") == entry.get("namespace")
                    and old.get("subject") == entry.get("subject")
                    and old.get("predicate") == entry.get("predicate")
                    and old.get("value") != entry.get("value")
                ):
                    old["validTo"] = _now_iso()
                    old["supersededBy"] = entry["id"]
                    superseded.append(old["id"])

        self.rows[entry["id"]] = entry
        self._bump()
        return {"id": entry["id"], "merged": False, "superseded": superseded}

    def _import(self, payloads: list[dict]) -> dict:
        imported = skipped = 0
        for payload in payloads:
            if not isinstance(payload, dict):
                skipped += 1
                continue
            entry_id = payload.get("id")
            if entry_id and entry_id in self.rows:
                # The engine looks ids up across every namespace, which is why
                # a shared id would silently drop a second namespace's row.
                skipped += 1
                continue
            if not entry_id:
                self.remember({**payload, "id": None})
                imported += 1
                continue
            entry = self._entry(payload, str(entry_id))
            entry["seen"] = max(1, int(payload.get("seen") or 1))
            entry["accessCount"] = max(0, int(payload.get("accessCount") or 0))
            self.rows[entry["id"]] = entry
            imported += 1
        self._bump()
        return {"imported": imported, "skipped": skipped}

    def _recall(self, query: str, k: int, namespace: str) -> list[dict]:
        words = _tokens(query)
        if not words:
            return []
        rows: list[dict] = []
        for entry in self.in_namespace(namespace):
            haystack = _tokens(
                f"{entry.get('text', '')} {json.dumps(entry.get('meta') or {}, ensure_ascii=False)}"
            )
            overlap = words & haystack
            if not overlap:
                continue
            relevance = round(len(overlap) / len(words), 4)
            rows.append({
                **entry,
                "relevance": relevance,
                "score": round(0.5 * relevance + 0.3 * 0.9 + 0.2 * entry["importance"], 4),
            })
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[: max(0, int(k))]

    # -- HTTP surface ----------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
        self.calls.append((method, path))
        parts = urlsplit(path)
        route = parts.path
        query = parse_qs(parts.query)
        body = payload or {}
        wanted = str((query.get("namespace") or [body.get("namespace") or self.namespace])[0])
        wanted = wanted.strip() or self.namespace

        if route == "/health":
            return {"ok": True, "mode": "lexical"}
        if route == "/export":
            return {
                "ok": True,
                "mode": "lexical",
                "namespace": wanted,
                "revision": str(self.revision),
                "entries": self.in_namespace(wanted),
            }
        if route == "/entries":
            entries = self.in_namespace(wanted)
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or [str(len(entries))])[0])
            return {
                "ok": True,
                "total": len(entries),
                "mode": "lexical",
                "entries": entries[offset:offset + limit],
            }
        if route == "/diagnostics":
            return {"ok": True, "namespace": self.namespace, "mode": "lexical", "stats": {}}
        if route.startswith("/memory/") and method == "GET":
            entry = self.rows.get(route.rsplit("/", 1)[-1])
            if not entry or entry["id"] in self.tombstones:
                raise _not_found(path)
            return {"ok": True, "entry": entry}
        if route.startswith("/memory/") and method == "DELETE":
            entry_id = route.rsplit("/", 1)[-1]
            entry = self.rows.get(entry_id)
            if not entry or entry_id in self.tombstones:
                raise _not_found(path)
            actual = str(entry.get("namespace") or "").strip()
            if actual and actual != wanted:
                raise _not_found(path)
            self.tombstones.add(entry_id)
            self._bump()
            return {"ok": True, "id": entry_id, "forgotten": True}
        if route == "/remember" and method == "POST":
            return self.remember(body)
        if route == "/import" and method == "POST":
            return self._import(list(body.get("entries") or []))
        if route == "/patch" and method == "POST":
            expected = str(body.get("expectedRevision") or "")
            self.patches.append({"expected": expected, "revision": str(self.revision)})
            if expected != str(self.revision):
                raise urllib.error.HTTPError(path, 409, "Conflict", {}, None)
            applied: list[str] = []
            forgotten: list[str] = []
            rejected: list[dict] = []
            for entry in body.get("remember") or []:
                result = self.remember({**entry, "namespace": wanted})
                if result.get("id"):
                    applied.append(result["id"])
                else:
                    rejected.append({"reason": result.get("error") or "NOT_STORED"})
            for entry_id in body.get("forget") or []:
                entry = self.rows.get(str(entry_id))
                actual = str((entry or {}).get("namespace") or "").strip()
                if not entry or (actual and actual != wanted):
                    rejected.append({"id": entry_id, "reason": "NOT_FOUND"})
                    continue
                self.tombstones.add(str(entry_id))
                forgotten.append(str(entry_id))
            self._bump()
            return {
                "ok": True,
                "namespace": wanted,
                "revision": str(self.revision),
                "applied": applied,
                "forgotten": forgotten,
                "rejected": rejected,
            }
        if route == "/recall" and method == "POST":
            return {"hits": self._recall(str(body.get("query") or ""), int(body.get("k") or 5), wanted)}
        if route == "/context" and method == "POST":
            hits = self._recall(str(body.get("query") or ""), int(body.get("k") or 8), wanted)
            return {"entries": hits, "text": "\n".join(hit["text"] for hit in hits), "tokens": 0}
        if route == "/feedback" and method == "POST":
            entry = self.rows.get(str(body.get("id") or ""))
            if not entry or entry["id"] in self.tombstones:
                return {"ok": False, "error": "NOT_FOUND"}
            entry["importance"] = min(1.0, float(entry["importance"]) + 0.03)
            self._bump()
            return {"ok": True, "id": entry["id"]}
        if route == "/extract" and method == "POST":
            return {"ok": True, "candidates": list(getattr(self, "candidates", []))}
        raise AssertionError(f"unexpected route {method} {path}")


# ── Fixtures and helpers ─────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    source = tmp_path / "memory" / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", source)
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(tmp_path / "native" / "universal.jsonl"))
    monkeypatch.setenv("SATURN_MEMORY_LONG_TERM", str(source))
    monkeypatch.setenv("SATURN_MEMORY_EMBED", "off")
    monkeypatch.setenv("SATURN_MEMORY_AUTOSTART", "0")
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", "origin")
    for name in (
        "SATURN_MEMORY_BACKEND",
        "SATURN_MEMORY_ENABLED",
        "SATURN_MEMORY_MIGRATE",
        "SATURN_MEMORY_CORTEX_MIGRATE",
        "SATURN_MEMORY_CORTEX_DB",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_state()
    yield tmp_path
    _reset_state()


def _reset_state() -> None:
    """Forget every in-process guard, exactly like a fresh interpreter."""
    MemoryCortex._instance = None
    cu.reset_migration_state()
    saturn_migrate.reset_migration_state()
    mm.reset_harvest_memo()
    with mm._lock:
        mm._native_state.update(
            {"read_ok": False, "degraded": "", "reported": "", "migration_error": ""}
        )


def _install(monkeypatch, bridge) -> None:
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setenv("SATURN_MEMORY_MODE", mode)


def _profile_entry(value: str, updated: str = "2026-01-05") -> dict:
    return {"value": value, "updated": updated}


def _cortex_db(root: Path) -> Path:
    return Path(root) / "config" / "memory_cortex.db"


def _make_legacy_db(path: Path, memories: list[tuple], habits: list[tuple] | None = None) -> None:
    """The schema ``MemoryCortex._init_db`` creates, with the given rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("""
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL,
                importance REAL DEFAULT 1.0,
                created_at REAL NOT NULL,
                last_accessed_at REAL NOT NULL,
                access_count INTEGER DEFAULT 0
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_cat ON memories(category);")
        conn.execute("""
            CREATE TABLE habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_event TEXT NOT NULL UNIQUE,
                suggested_action TEXT NOT NULL,
                frequency INTEGER DEFAULT 1,
                last_triggered REAL NOT NULL
            );
        """)
        for category, content, importance, created, accessed, count in memories:
            conn.execute(
                "INSERT INTO memories (category, content, keywords, importance, created_at, "
                "last_accessed_at, access_count) VALUES (?,?,?,?,?,?,?)",
                (category, content, " ".join(sorted(_tokens(content))), importance, created, accessed, count),
            )
        for trigger, action, frequency, last in habits or []:
            conn.execute(
                "INSERT INTO habits (trigger_event, suggested_action, frequency, last_triggered) "
                "VALUES (?,?,?,?)",
                (trigger, action, frequency, last),
            )
        conn.commit()
    finally:
        conn.close()


def _active_texts(store) -> list[str]:
    return [row["text"] for row in store.active()]


def _cortex_rows(store, namespace: str = "origin") -> list[dict]:
    return [
        row for row in store.in_namespace(namespace)
        if (row.get("meta") or {}).get("cortex")
    ]


# ── Fix 1: namespace isolation ──────────────────────────────────────────────

def test_payloads_carry_the_verified_namespace():
    profile = ub.profile_payload(
        "identity", "name", _profile_entry("Ada"), namespace="origin"
    )
    session = ub.session_payload(
        {"summary": "worked on memory", "date": "2026-01-02", "language": "en"},
        namespace="origin",
    )
    # The namespace is a top-level field, not something a reconciler has to
    # infer from meta.
    assert profile["namespace"] == "origin"
    assert session["namespace"] == "origin"
    assert "namespace" not in profile["meta"]


def test_namespace_scoping_is_enforced_client_side_too():
    bridge = RevisionedStore(namespace="origin")
    data = {
        "entries": [
            {"id": "a", "namespace": "origin", "meta": {"saturn_category": "identity", "saturn_key": "name"}},
            {"id": "b", "namespace": "other", "meta": {"saturn_category": "identity", "saturn_key": "name"}},
            {"id": "c", "meta": {"saturn_category": "notes", "saturn_key": "drink"}},
        ]
    }
    scoped = bridge._scoped_entries(data)
    # A row that names another namespace is never visible, even if a transport
    # ignored the scope parameter. A row from before namespaces existed is ours.
    assert [entry["id"] for entry in scoped] == ["a", "c"]
    assert list(bridge._group_identities(data)) == [("identity", "name"), ("notes", "drink")]


def test_reconcile_and_cortex_clear_stay_inside_the_namespace(workspace, monkeypatch):
    store = RevisionedStore(namespace="origin")
    _install(monkeypatch, store)
    other = RevisionedStore(namespace="other")
    other.file = store.file

    # A second namespace writes a profile fact and a cortex memory into the
    # same physical store.
    other.write_profile(
        {"identity": {"name": _profile_entry("Grace", "2026-03-01")}},
        include_sessions=True,
    )
    other.remember_entry(cu.memory_payload("User preferred editor: Helix", "preference"))
    other_before = [row["id"] for row in other.active()]
    assert len(other_before) == 2

    # Saturn's profile reconciles itself: nothing of the neighbour's is indexed
    # or pruned.
    store.write_profile(
        {"identity": {"name": _profile_entry("Ada", "2026-01-01")}},
        include_sessions=True,
    )
    assert [row["id"] for row in other.active()] == other_before
    assert set(store.native_index()) == {("identity", "name")}

    cortex = MemoryCortex(db_path=_cortex_db(workspace))
    cortex.remember("User preferred editor: Neovim", "preference")
    mine = [row["id"] for row in _cortex_rows(store, "origin")]
    assert len(mine) == 1

    cortex.clear_all_memories()

    assert _cortex_rows(store, "origin") == []
    assert [row["id"] for row in other.active()] == other_before
    assert other.get_entry(other_before[1]) is not None


# ── Fix 2: concurrency and stale snapshots ──────────────────────────────────

def test_stale_snapshot_cannot_erase_a_newer_write(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)
    _set_mode(monkeypatch, "full")

    mm.load_memory()  # establishes successful-read health
    stale = mm.load_memory()
    assert stale.provenance["revision"] == str(store.revision)

    # Somebody else commits while this snapshot is being edited.
    store.write_profile(
        {"identity": {"name": _profile_entry("Grace", "2026-02-01")}},
        include_sessions=True,
    )
    assert "Identity name: Grace" in _active_texts(store)

    stale["notes"] = {"drink": _profile_entry("oolong")}
    with pytest.raises(ub.SaturnStaleSnapshot):
        mm.save_memory(stale)

    # The competitor's write survived and nothing of the stale diff landed.
    texts = _active_texts(store)
    assert "Identity name: Grace" in texts
    assert not any("oolong" in text for text in texts)
    # The 409 is about *this* snapshot, not about the backend being unhealthy.
    assert mm.degraded_status()["active"] is False
    last = store.patches[-1]
    assert last["expected"] != last["revision"]


def test_snapshot_provenance_belongs_to_each_snapshot(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)

    first = store.load_memory()
    first_revision = first.revision
    assert first.provenance["read_ok"] is True
    assert first.provenance["namespace"] == "origin"

    store.remember(cu.memory_payload("User preferred editor: Neovim", "preference"))

    second = store.load_memory()
    # A later read does not rewrite the older snapshot's evidence: a global
    # read_ok flag could not tell these two apart.
    assert first.revision == first_revision
    assert second.revision != first_revision
    assert first.provenance["revision"] == first_revision


def test_concurrent_profile_updates_are_serialized_and_lossless(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)
    _set_mode(monkeypatch, "full")

    real_load = mm.load_memory
    state = {"now": 0, "max": 0}
    guard = threading.Lock()

    def slow_load():
        # Two loads inside the read-modify-write would be observable here if
        # the cycle were not serialized.
        with guard:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        try:
            time.sleep(0.02)
            return real_load()
        finally:
            with guard:
                state["now"] -= 1

    monkeypatch.setattr(mm, "load_memory", slow_load)
    failures: list[Exception] = []

    def worker(index: int) -> None:
        try:
            mm.update_memory({"notes": {f"k{index}": {"value": f"v{index}"}}})
        except Exception as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not failures, failures
    assert state["max"] == 1, "read-modify-write cycles overlapped"
    assert set(mm.load_memory()["notes"]) == {"k0", "k1", "k2", "k3"}


# ── Fix 3: the migration gate ───────────────────────────────────────────────

def test_failed_initial_migration_blocks_every_authoritative_write(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)
    _set_mode(monkeypatch, "full")
    monkeypatch.setattr(
        saturn_migrate,
        "ensure_migrated",
        lambda **kwargs: {"ok": False, "error": "the sidecar refused the snapshot"},
    )

    mm.load_memory()  # the read succeeds; only the migration failed
    assert mm._migration_block_reason()

    with pytest.raises(ub.SaturnWriteError):
        mm.update_memory({"notes": {"drink": {"value": "oolong"}}})
    assert store.active() == []

    assert "Remember failed" in mm.remember("drink", "oolong", "notes")
    assert "Forget failed" in mm.forget("name", "identity")
    assert mm.pop_last_session() is None
    mm.save_session_summary("a session happened", "en")

    assert store.active() == [], "nothing may be written while the gate is closed"

    # The documented escape hatch: an operator who accepts the current native
    # store as-is can explicitly disable migration.
    monkeypatch.setenv("SATURN_MEMORY_MIGRATE", "off")
    saved = mm.update_memory({"notes": {"drink": {"value": "oolong"}}})
    assert saved["notes"]["drink"]["value"] == "oolong"
    assert any("oolong" in text for text in _active_texts(store))


def test_migration_retry_does_not_roll_the_native_store_back(workspace, monkeypatch):
    source = Path(workspace) / "memory" / "long_term.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps({"identity": {"name": _profile_entry("Ada", "2026-01-01")}}),
        encoding="utf-8",
    )
    store = RevisionedStore()
    _install(monkeypatch, store)
    # The native store already holds a value written after the snapshot was
    # frozen - exactly the state a retry must not undo.
    store.remember(
        ub.profile_payload("identity", "name", _profile_entry("Grace", "2026-02-01"), namespace="origin")
    )

    report = saturn_migrate.import_from_json(source=source, bridge=store)

    assert report["ok"] is True
    assert (report["imported"], report["updated"], report["skipped"]) == (0, 0, 1)
    texts = _active_texts(store)
    assert "Identity name: Grace" in texts
    assert "Identity name: Ada" not in texts


# ── Helpers shared by the migration tests ───────────────────────────────────

def _write_profile(path: Path, include_session: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "identity": {"name": _profile_entry("Ada", "2026-01-02")},
        "preferences": {"editor": _profile_entry("Neovim", "2026-01-03")},
        "projects": {},
        "relationships": {},
        "wishes": {},
        "notes": {},
    }
    if include_session:
        payload["sessions"] = [
            {"date": "2026-01-04", "summary": "worked on the memory subsystem", "language": "en"}
        ]
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def _snapshot_contents(path: Path) -> list[str]:
    conn = sqlite3.connect(str(path))
    try:
        return [row[0] for row in conn.execute("SELECT content FROM memories ORDER BY id")]
    finally:
        conn.close()


# ── Fix 4: destination-bound markers and caches ─────────────────────────────

def test_json_migration_marker_is_bound_to_its_destination(workspace, monkeypatch):
    source = _write_profile(Path(workspace) / "memory" / "long_term.json")
    native = Path(workspace) / "native"

    first = RevisionedStore(namespace="origin", file=native / "a.jsonl")
    report = saturn_migrate.ensure_migrated(bridge=first, source=source)
    assert report["ok"] is True and report["imported"] == 3

    marker = saturn_migrate.read_marker(source)
    _memory, source_digest = saturn_migrate.load_profile(source)
    assert marker["version"] == saturn_migrate.MIGRATION_VERSION
    assert marker["source_sha256"] == source_digest
    assert marker["destination"]["id"] == saturn_migrate._destination(first)["id"]
    assert marker["destination"]["namespace"] == "origin"

    saturn_migrate.reset_migration_state()
    assert saturn_migrate.ensure_migrated(bridge=first, source=source)["reason"] == "already_imported"

    # Age the marker so "did this rewrite the stamp" is observable.
    aged = dict(marker, imported_at="2020-01-01T00:00:00+00:00")
    source_marker_path = saturn_migrate.marker_path(source)
    source_marker_path.write_text(json.dumps(aged, indent=2), encoding="utf-8")

    # A different engine file is a different destination: migrate into it.
    second = RevisionedStore(namespace="origin", file=native / "b.jsonl")
    saturn_migrate.reset_migration_state()
    moved = saturn_migrate.ensure_migrated(bridge=second, source=source)
    assert moved.get("reason") != "already_imported"
    assert moved["imported"] == 3
    assert sorted(_active_texts(second)) == [
        "Identity name: Ada",
        "Preferences editor: Neovim",
        "Saturn session 2026-01-04: worked on the memory subsystem",
    ]
    rewritten = saturn_migrate.read_marker(source)
    assert rewritten["destination"]["id"] != marker["destination"]["id"]
    assert rewritten["destination"]["engine_file"] == str(native / "b.jsonl")
    assert rewritten["imported_at"] != "2020-01-01T00:00:00+00:00"

    # A different namespace is a different destination too.
    other = RevisionedStore(namespace="other", file=native / "b.jsonl")
    saturn_migrate.reset_migration_state()
    renamed = saturn_migrate.ensure_migrated(bridge=other, source=source)
    assert renamed["imported"] == 3
    assert {row["namespace"] for row in other.active()} == {"other"}
    assert saturn_migrate.read_marker(source)["destination"]["namespace"] == "other"


def test_cortex_migration_marker_and_cache_are_bound_to_the_destination(workspace, monkeypatch):
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 200.0, now - 100.0, 2)],
    )
    native = Path(workspace) / "native"

    first = RevisionedStore(namespace="origin", file=native / "a.jsonl")
    report = cu.ensure_cortex_migrated(bridge=first, source=db_path)
    assert report["ok"] is True and report["imported"] == 1
    revision_after_import = first.revision

    marker = cu.read_marker(db_path)
    rollback = Path(report["rollback"])
    assert marker["destination"]["namespace"] == "origin"
    assert marker["destination"]["id"] == cu._destination(first)["id"]
    # The fingerprint is of the committed snapshot (WAL included), and the
    # cheap gate covers the same bytes.
    assert marker["source_sha256"] == hashlib.sha256(rollback.read_bytes()).hexdigest()
    assert marker["source_files_sha256"] == cu.source_files_digest(db_path)

    # Same destination: the process cache answers without touching the store.
    assert cu.ensure_cortex_migrated(bridge=first, source=db_path)["ok"] is True
    assert first.revision == revision_after_import
    cu.reset_migration_state()
    assert cu.ensure_cortex_migrated(bridge=first, source=db_path)["reason"] == "already_imported"
    assert first.revision == revision_after_import

    # Another engine file is another destination.
    second = RevisionedStore(namespace="origin", file=native / "b.jsonl")
    cu.reset_migration_state()
    moved = cu.ensure_cortex_migrated(bridge=second, source=db_path)
    assert moved.get("reason") != "already_imported"
    assert moved["imported"] == 1
    assert [row["text"] for row in _cortex_rows(second, "origin")] == ["User name (identity): Ada"]

    # Another namespace is another destination.
    other = RevisionedStore(namespace="other", file=native / "b.jsonl")
    cu.reset_migration_state()
    renamed = cu.ensure_cortex_migrated(bridge=other, source=db_path)
    assert renamed["imported"] == 1
    assert len(_cortex_rows(other, "other")) == 1


# ── Fix 5: namespace-aware JSON migration with real recall ──────────────────

def test_migrated_facts_and_sessions_are_namespaced_and_recallable(workspace, monkeypatch):
    source = _write_profile(Path(workspace) / "memory" / "long_term.json")
    store = RevisionedStore()

    report = saturn_migrate.import_from_json(source=source, bridge=store)
    assert report["ok"] is True
    assert (report["entries"], report["sessions"], report["imported"]) == (2, 1, 3)

    entries = store.active()
    assert {row["namespace"] for row in entries} == {"origin"}
    by_identity = {
        (row["meta"]["saturn_category"], row["meta"]["saturn_key"]): row for row in entries
    }
    assert by_identity[("identity", "name")]["id"] == saturn_migrate.stable_entry_id(
        "identity", "name", "origin"
    )
    session_keys = [key for category, key in by_identity if category == "sessions"]
    assert len(session_keys) == 1
    assert by_identity[("sessions", session_keys[0])]["id"] == saturn_migrate.stable_entry_id(
        "sessions", session_keys[0], "origin"
    )

    # Read back through the engine: the migrated preference and the migrated
    # session are both recallable.
    fact_hits = store.recall_entries("editor Neovim")
    assert fact_hits and fact_hits[0]["value"] == "Neovim"
    assert fact_hits[0]["category"] == "preferences"

    session_hits = store.recall_entries("memory subsystem session")
    assert session_hits, "the migrated session must be recallable"
    assert session_hits[0]["category"] == "sessions"
    assert session_hits[0]["value"] == "worked on the memory subsystem"

    profile = store.load_memory()
    assert profile["identity"]["name"]["value"] == "Ada"
    assert profile["sessions"] == [
        {"date": "2026-01-04", "summary": "worked on the memory subsystem", "language": "en"}
    ]


def test_session_import_is_idempotent_and_stays_in_one_namespace(workspace, monkeypatch):
    source = _write_profile(Path(workspace) / "memory" / "long_term.json")
    store = RevisionedStore()

    first = saturn_migrate.import_from_json(source=source, bridge=store)
    second = saturn_migrate.import_from_json(source=source, bridge=store)

    assert first["imported"] == 3
    assert (second["imported"], second["updated"]) == (0, 0)
    assert second["skipped"] == 3
    assert len(store.active()) == 3


# ── Fix 6: semantic contradiction identity ─────────────────────────────────

def test_candidate_payload_preserves_extracted_semantics():
    flat = {
        "text": "The user loves oolong tea",
        "kind": "preference",
        "subject": "user",
        "predicate": "favourite_tea",
        "value": "oolong",
    }
    payload = cu.candidate_payload(flat)
    assert payload["kind"] == "preference"
    assert payload["subject"] == "user"
    assert payload["predicate"] == "cortex.preference.user.favourite-tea"
    assert payload["value"] == "oolong"
    assert payload["dedupe"] is True
    assert payload["meta"]["cortex_predicate"] == "favourite_tea"
    assert payload["meta"]["cortex_value"] == "oolong"

    # Schema-agnostic: nested field containers produce the same slot.
    for container in ("meta", "fields", "attributes"):
        nested = {
            "text": "The user loves oolong tea",
            "kind": "preference",
            container: {"subject": "user", "predicate": "favourite_tea", "value": "oolong"},
        }
        assert cu.candidate_payload(nested)["predicate"] == payload["predicate"]

    # Episodes are historical events: no semantic slot, occurrence identity.
    episode = cu.candidate_payload({
        "text": "The user attended a wedding in Kyoto",
        "kind": "episode",
        "subject": "user",
        "predicate": "event",
        "value": "wedding",
    })
    assert episode["predicate"] != cu.semantic_slot("episode", "user", "event")
    assert episode["dedupe"] is False
    other_episode = cu.candidate_payload({
        "text": "The user attended a conference in Osaka",
        "kind": "episode",
        "subject": "user",
        "predicate": "event",
        "value": "conference",
    })
    assert other_episode["predicate"] != episode["predicate"]


def test_changed_preferences_and_identity_supersede_while_rules_coexist(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    assert cortex.remember("User preferred editor: Neovim", "preference") != -1
    cortex.remember("User preferred editor: Helix", "preference")

    rows = _cortex_rows(store)
    assert [row["meta"]["cortex_content"] for row in rows] == ["User preferred editor: Helix"]
    assert len(store.rows) == 2, "the superseded row is kept as history, not as a rival"
    superseded = [row for row in store.rows.values() if row.get("validTo")]
    assert len(superseded) == 1 and superseded[0]["meta"]["cortex_content"].endswith("Neovim")

    cortex.remember("User name (identity): Ada", "identity")
    cortex.remember("User name (identity): Grace", "identity")
    identity_rows = [
        row for row in _cortex_rows(store) if row["meta"]["cortex_category"] == "identity"
    ]
    assert [row["text"] for row in identity_rows] == ["User name (identity): Grace"]

    # A directive is a rule, not a value: a second one is a second rule.
    cortex.remember("Always answer in English", "directive")
    cortex.remember("Always answer briefly", "directive")
    directives = [
        row for row in _cortex_rows(store) if row["meta"]["cortex_category"] == "directive"
    ]
    assert len(directives) == 2

    # Episodes stay occurrence-identified even when the text repeats.
    cortex.remember("The user attended a wedding in Kyoto", "episode")
    cortex.remember("The user attended a wedding in Kyoto", "episode")
    episodes = [
        row for row in _cortex_rows(store) if row["meta"]["cortex_category"] == "episode"
    ]
    assert len(episodes) == 2


def test_extracted_semantic_slot_supersedes_on_restatement(workspace, monkeypatch):
    store = RevisionedStore()
    _install(monkeypatch, store)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    store.candidates = [{
        "text": "The user's tea of choice is oolong",
        "kind": "preference",
        "subject": "user",
        "predicate": "tea_of_choice",
        "value": "oolong",
    }]
    cortex.harvest_turn("The user's tea of choice is oolong")

    store.candidates = [{
        "text": "The user's tea of choice is green",
        "kind": "preference",
        "subject": "user",
        "predicate": "tea_of_choice",
        "value": "green",
    }]
    cortex.harvest_turn("The user's tea of choice is green")

    contents = [row["meta"]["cortex_content"] for row in _cortex_rows(store)]
    assert contents == ["The user's tea of choice is green"]
    assert len(store.rows) == 2


# ── Fix 9: WAL-safe cortex snapshot ────────────────────────────────────────

def test_cortex_snapshot_includes_committed_uncheckpointed_wal_rows(workspace, monkeypatch):
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 900.0, now - 800.0, 1)],
    )

    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(
            "INSERT INTO memories (category, content, keywords, importance, created_at, "
            "last_accessed_at, access_count) VALUES (?,?,?,?,?,?,?)",
            ("preference", "User preferred editor: Neovim", "editor neovim", 0.8, now - 60.0, now - 40.0, 0),
        )
        writer.commit()

        wal = Path(str(db_path) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0
        # The committed row is not in the main database file yet: a byte copy
        # of ``memory_cortex.db`` alone would lose it.
        assert b"Neovim" not in db_path.read_bytes()
        digest_before = cu.source_files_digest(db_path)

        snapshot = cu.write_rollback_copy(db_path)
        assert snapshot is not None
        assert "User preferred editor: Neovim" in _snapshot_contents(snapshot)
        assert "User name (identity): Ada" in _snapshot_contents(snapshot)

        # The cheap gate sees later WAL commits too.
        writer.execute(
            "INSERT INTO memories (category, content, keywords, importance, created_at, "
            "last_accessed_at, access_count) VALUES (?,?,?,?,?,?,?)",
            ("preference", "User preferred shell: fish", "shell fish", 0.7, now - 30.0, now - 20.0, 0),
        )
        writer.commit()
        assert cu.source_files_digest(db_path) != digest_before

        # And the importer reads the snapshot, so both WAL rows migrate.
        store = RevisionedStore()
        _install(monkeypatch, store)
        report = cu.migrate_cortex_db(source=db_path, bridge=store)
        assert report["ok"] is True
        assert report["memories"] == 3
        assert report["imported"] == 3
        texts = {row["meta"]["cortex_content"] for row in _cortex_rows(store)}
        assert texts == {
            "User name (identity): Ada",
            "User preferred editor: Neovim",
            "User preferred shell: fish",
        }
    finally:
        writer.close()


# ── Fix 10: lossless cortex migration identity ─────────────────────────────

def test_repeated_episodes_and_long_shared_prefixes_keep_distinct_identities():
    prefix = (
        "The user attended the international memory engineering conference in Kyoto "
        "and stayed for the evening sessions discussing retrieval"
    )
    assert len(prefix) > 60
    created = 1_767_000_000.0
    rows = [
        {
            "id": index,
            "category": "episode",
            "content": prefix + suffix,
            "keywords": "",
            "importance": 0.6,
            "created_at": created,
            "last_accessed_at": created,
            "access_count": 0,
        }
        for index, suffix in enumerate((" on Monday", " on Tuesday", " on Monday"), start=1)
    ]

    entries = cu.build_cortex_entries(rows, [])
    ids = [entry["id"] for entry in entries]
    assert len(ids) == 3
    assert len(set(ids)) == 3, "a shared 60-character prefix must not collapse rows"
    assert len({entry["meta"]["cortex_key"] for entry in entries}) == 3

    # The same source rows migrate as three entries, not one.
    store = RevisionedStore()
    assert store.import_entries(entries)["imported"] == 3
    assert len(store.active()) == 3
    # …and a second import is a no-op rather than a duplicate.
    assert store.import_entries(entries)["skipped"] == 3
    assert len(store.active()) == 3


def test_migration_identity_is_namespace_aware():
    rows = [{
        "id": 1,
        "category": "identity",
        "content": "User name (identity): Ada",
        "keywords": "",
        "importance": 0.9,
        "created_at": 1_767_000_000.0,
        "last_accessed_at": 1_767_000_000.0,
        "access_count": 0,
    }]
    origin = cu.build_cortex_entries(rows, [])[0]["id"]

    os.environ["SATURN_MEMORY_NAMESPACE"] = "other"
    try:
        other = cu.build_cortex_entries(rows, [])[0]["id"]
    finally:
        os.environ["SATURN_MEMORY_NAMESPACE"] = "origin"

    # The engine keys ids across namespaces, so equal ids would make the
    # second namespace's import look like a duplicate.
    assert origin != other


def test_habit_identity_keeps_the_whole_trigger_not_a_sixty_char_slug():
    shared = (
        "every morning when I open my laptop and connect to the office network "
        "and say hello to the team"
    )
    assert len(shared) > 60
    key_a, id_a = cu.habit_identity(shared + " before coffee")
    key_b, id_b = cu.habit_identity(shared + " after coffee")

    # Reconciliation, habit lookup and the versioning predicate all use the
    # key. A 60-character slug made these two triggers *identical* - one key,
    # one habit, and the second action silently absorbed the first.
    assert key_a != key_b
    assert id_a != id_b
    # Still readable: the label prefix is the trigger, the digest is the whole
    # trigger, so an operator can debug a row without decoding a raw hash.
    assert key_a.startswith("every-morning-when-i-open-my-lap-")
    assert len(key_a) < 60

    # Two habits sharing a 60-character prefix migrate as two habits.
    rows = [
        {
            "id": 1,
            "trigger_event": shared + " before coffee",
            "suggested_action": "reads the news",
            "frequency": 3,
            "last_triggered": 1_767_000_000.0,
        },
        {
            "id": 2,
            "trigger_event": shared + " after coffee",
            "suggested_action": "opens the standup notes",
            "frequency": 5,
            "last_triggered": 1_767_000_100.0,
        },
    ]
    entries = cu.build_cortex_entries([], rows)
    assert len(entries) == 2
    assert len({entry["meta"]["cortex_key"] for entry in entries}) == 2
    assert len({entry["id"] for entry in entries}) == 2
    assert [entry["seen"] for entry in entries] == [3, 5]


# ── Fix 7: the save_memory tool must contain Saturn failures ───────────────

class _FakeUi:
    """The slice of the UI the tool handler touches, with every call recorded."""

    def __init__(self) -> None:
        self.muted = False
        self.states: list[str] = []
        self.logs: list[str] = []
        self.finished: list[tuple] = []

    def set_state(self, state: str) -> None:
        self.states.append(state)

    def show_hud_operation(self, **kwargs) -> None:
        pass

    def update_task_workspace(self, **kwargs) -> None:
        pass

    def finish_task_workspace(self, *args) -> None:
        self.finished.append(args)

    def write_log(self, message: str) -> None:
        self.logs.append(message)


def _tool_controller():
    from main import SaturnLive

    class Controller:
        _execute_tool = SaturnLive._execute_tool

        def __init__(self) -> None:
            self.ui = _FakeUi()
            self._in_live_tool = False

    return Controller()


def _function_call(args: dict, call_id: str = "call-1"):
    # The local tool dispatcher reads only .name / .args / .id, so this in-process
    # stand-in replaces the retired google.genai.types.FunctionCall. No cloud SDK
    # is imported anywhere.
    class _FunctionCall:
        __slots__ = ("name", "args", "id")

        def __init__(self, name, args, id):
            self.name = name
            self.args = args
            self.id = id

    return _FunctionCall(name="save_memory", args=args, id=call_id)


def test_save_memory_tool_contains_saturn_failures(monkeypatch):
    import main as main_module

    controller = _tool_controller()
    call = _function_call({"category": "preferences", "key": "editor", "value": "Neovim"})

    def refuse(_update):
        raise ub.SaturnWriteError("the native store is unreachable")

    monkeypatch.setattr(main_module, "update_memory", refuse)

    # The handler returns; it does not raise into the live receive loop.
    response = asyncio.run(controller._execute_tool(call))

    assert response.name == "save_memory"
    assert response.response["success"] is False
    assert response.response["stored"] is False
    assert "unreachable" in response.response["error"]
    assert response.response["result"].startswith("Memory not saved")
    # State restored in ``finally``, and the user was told what happened.
    assert controller.ui.states[-1] == "LISTENING"
    assert any("Memory not saved" in line for line in controller.ui.logs)
    assert controller.ui.finished == []

    # The success path still reports truthfully.
    monkeypatch.setattr(main_module, "update_memory", lambda _update: {"ok": True})
    ok = asyncio.run(controller._execute_tool(_function_call(
        {"category": "preferences", "key": "editor", "value": "Helix"}, "call-2"
    )))
    assert ok.response["stored"] is True
    assert ok.response["silent"] is True
    assert controller.ui.finished
    assert controller.ui.states[-1] == "LISTENING"

    # One branch, not two copies that can drift apart.
    source = Path(main_module.__file__).read_text(encoding="utf-8")
    assert source.count('if name == "save_memory"') == 1


# ── Fix 8: local endpoint validation ───────────────────────────────────────

@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1.evil.example",
        "localhost.example.com",
        "127.0.0.1.nip.io",
        "127.0.0.1x",
        "10.0.0.1",
        "192.168.1.10",
        "::2",
        "0.0.0.0",
        "example.com",
        "",
    ],
)
def test_deceptive_and_remote_hosts_are_not_local(host):
    assert ub._is_local_host(host) is False
    assert ub._local_url(f"http://{host}/v1") is False


def test_a_host_string_carrying_a_port_is_not_a_loopback_literal():
    # ``SATURN_MEMORY_HOST`` is a bare host. ``ip_address`` rejects a value
    # that still carries the port, which is the point: only a real loopback
    # parse counts, not a string that merely starts with one.
    assert ub._is_local_host("127.0.0.1:8799") is False
    # In a full URL the port belongs to the authority, so the same endpoint
    # written as a URL is legitimately local.
    assert ub._local_url("http://127.0.0.1:8799/v1") is True


@pytest.mark.parametrize(
    ("host", "url"),
    [
        ("localhost", "http://localhost:11434/api/embeddings"),
        ("LOCALHOST", "http://LOCALHOST:11434/api/embeddings"),
        ("127.0.0.1", "http://127.0.0.1:11434/api/embeddings"),
        ("127.0.0.2", "http://127.0.0.2:11434/api/embeddings"),
        # An IPv6 literal must be bracketed inside a URL; both spellings of
        # the host itself are loopback.
        ("::1", "http://[::1]:11434/api/embeddings"),
        ("[::1]", "http://[::1]:11434/api/embeddings"),
    ],
)
def test_loopback_names_and_literals_are_local(host, url):
    assert ub._is_local_host(host) is True
    assert ub._local_url(url) is True


def test_bridge_refuses_a_non_loopback_endpoint_before_any_traffic(workspace, monkeypatch):
    import http.client as http_client

    monkeypatch.setenv("SATURN_MEMORY_URL", "http://127.0.0.1.evil.example:8799")
    opened: list[tuple] = []

    class _Forbidden:
        def __init__(self, *args, **kwargs):
            opened.append((args, kwargs))
            raise AssertionError("a connection was attempted")

    monkeypatch.setattr(http_client, "HTTPConnection", _Forbidden)

    bridge = ub.UniversalBridge()
    with pytest.raises(ub.SaturnUnavailable):
        bridge._endpoint()

    assert bridge.health() is False
    with pytest.raises(ub.SaturnUnavailable):
        bridge.remember_entry({"text": "a secret the user told me"})
    assert opened == []

    # Nor may autostart spawn a sidecar aimed at that host: "no traffic" has
    # to include "no process listening on the wrong interface".
    monkeypatch.setenv("SATURN_MEMORY_AUTOSTART", "1")
    assert bridge.available(autostart=True) is False
    assert bridge._process is None


def test_redirects_are_never_followed(workspace, monkeypatch):
    leaked: list[str] = []

    class _Remote(BaseHTTPRequestHandler):
        def do_GET(self):  # pragma: no cover - asserted never to run
            leaked.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        do_POST = do_GET

        def log_message(self, *args):
            pass

    class _Redirector(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def _redirect(self):
            # Drain the request body before answering and declare an empty
            # response. Closing a socket that still holds unread request bytes
            # resets the connection on Windows, which would hide the 302 the
            # client is supposed to see.
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{remote.server_address[1]}/leak")
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _redirect
        do_POST = _redirect

        def log_message(self, *args):
            pass

    remote = HTTPServer(("127.0.0.1", 0), _Remote)
    redirector = HTTPServer(("127.0.0.1", 0), _Redirector)
    for server in (remote, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{redirector.server_address[1]}")
        bridge = ub.UniversalBridge()

        assert bridge.health() is False
        with pytest.raises(urllib.error.HTTPError) as failure:
            bridge._request("POST", "/remember", {"text": "a secret the user told me"})
        assert failure.value.code == 302
        assert leaked == [], "the redirect target received memory content"
    finally:
        for server in (remote, redirector):
            server.shutdown()
            server.server_close()


# ── Real Node sidecar: namespace isolation, recall, lossless identity ──────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_sidecar(data_file: Path, namespace: str = "origin"):
    port = _free_port()
    env = {
        **os.environ,
        "SATURN_MEMORY_PORT": str(port),
        "SATURN_MEMORY_HOST": "127.0.0.1",
        "SATURN_MEMORY_FILE": str(data_file),
        "SATURN_MEMORY_KEY": "test-key",
        "SATURN_MEMORY_EMBED": "off",
        "SATURN_MEMORY_NAMESPACE": namespace,
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [NODE_BINARY, str(SIDECAR_JS)],
        cwd=str(SIDECAR_JS.parent.parent),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return port, process


def _connect(monkeypatch, port: int, namespace: str) -> "ub.UniversalBridge":
    monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SATURN_MEMORY_PORT", str(port))
    monkeypatch.setenv("SATURN_MEMORY_HOST", "127.0.0.1")
    monkeypatch.setenv("SATURN_MEMORY_KEY", "test-key")
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", namespace)
    bridge = ub.UniversalBridge()
    deadline = time.time() + 25
    while time.time() < deadline and not bridge.health(timeout=0.5):
        time.sleep(0.15)
    assert bridge.health(), "sidecar failed to start"
    bridge.require()
    return bridge


def _cortex_rows_live(bridge) -> list[dict]:
    return [
        entry for entry in bridge.export_entries()["entries"]
        if (entry.get("meta") or {}).get("cortex")
    ]


@pytest.mark.skipif(NODE_BINARY is None, reason="node is required to run memory/sidecar.js")
def test_real_sidecar_keeps_another_namespace_through_reconcile_and_clear(workspace, monkeypatch):
    data_file = Path(workspace) / "native" / "shared.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        origin = _connect(monkeypatch, port, "origin")
        other = _connect(monkeypatch, port, "other")
        _install(monkeypatch, origin)

        neighbour_profile = other.remember_entry(
            ub.profile_payload("identity", "name", _profile_entry("Grace", "2026-03-01"), namespace="other")
        )
        neighbour_cortex = other.remember_entry(
            cu.memory_payload("User preferred editor: Helix", "preference")
        )
        assert neighbour_profile.get("id") and neighbour_cortex.get("id")
        assert {row["namespace"] for row in other.export_entries()["entries"]} == {"other"}

        # Saturn reconciles its profile (which prunes identities it does not
        # own) and then clears its cortex store.
        origin.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )
        cortex = MemoryCortex(db_path=_cortex_db(workspace))
        assert cortex.remember("User preferred editor: Neovim", "preference") != -1
        assert len(_cortex_rows_live(origin)) == 1
        cortex.clear_all_memories()

        assert _cortex_rows_live(origin) == []
        assert other.get_entry(neighbour_profile["id"]) is not None
        assert other.get_entry(neighbour_cortex["id"]) is not None
        assert other.load_memory()["identity"]["name"]["value"] == "Grace"
    finally:
        process.terminate()
        process.wait(timeout=20)


@pytest.mark.skipif(NODE_BINARY is None, reason="node is required to run memory/sidecar.js")
def test_real_sidecar_recalls_migrated_facts_sessions_and_keeps_lossless_ids(workspace, monkeypatch):
    data_file = Path(workspace) / "native" / "migrated.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        # The importer refuses to migrate into a store the sidecar does not
        # actually serve, so the configured file must be exactly the one this
        # engine was started on (the fixture default points elsewhere).
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        _install(monkeypatch, bridge)

        source = _write_profile(Path(workspace) / "memory" / "long_term.json")
        report = saturn_migrate.import_from_json(source=source, bridge=bridge)
        assert report["ok"] is True
        assert (report["entries"], report["sessions"], report["imported"]) == (2, 1, 3)

        fact_hits = bridge.recall_entries("Neovim editor")
        assert fact_hits, "the migrated preference must be recallable"
        assert fact_hits[0]["value"] == "Neovim"
        session_hits = bridge.recall_entries("memory subsystem session")
        assert session_hits, "the migrated session must be recallable"
        assert session_hits[0]["category"] == "sessions"
        assert {row["namespace"] for row in bridge.export_entries()["entries"]} == {"origin"}

        # A restated preference with the same semantic slot supersedes.
        slot = cu.semantic_slot("preference", "user", "preferred.editor")
        first = bridge.remember_entry(
            cu.memory_payload("User preferred editor: Neovim", "preference", predicate=slot)
        )
        second = bridge.remember_entry(
            cu.memory_payload("User preferred editor: Helix", "preference", predicate=slot)
        )
        assert first["id"] != second["id"]
        live = {
            row["meta"]["cortex_content"] for row in _cortex_rows_live(bridge)
        }
        assert "User preferred editor: Helix" in live
        assert "User preferred editor: Neovim" not in live

        # Lossless identity: repeated episodes and long shared prefixes all
        # survive the import as separate entries.
        prefix = (
            "The user attended the international memory engineering conference in Kyoto "
            "and stayed for the evening sessions discussing retrieval"
        )
        created = time.time() - 3600.0
        rows = [
            {
                "id": index,
                "category": "episode",
                "content": prefix + suffix,
                "keywords": "",
                "importance": 0.6,
                "created_at": created,
                "last_accessed_at": created,
                "access_count": 0,
            }
            for index, suffix in enumerate((" on Monday", " on Tuesday", " on Monday"), start=1)
        ]
        entries = cu.build_cortex_entries(rows, [])
        assert len({entry["id"] for entry in entries}) == 3
        assert bridge.import_entries(entries)["imported"] == 3
        exported = {row["id"] for row in bridge.export_entries()["entries"]}
        assert {entry["id"] for entry in entries} <= exported
    finally:
        process.terminate()
        process.wait(timeout=20)
