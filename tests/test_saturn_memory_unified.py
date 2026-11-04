"""Focused tests for Stage Three: one memory store (``unified`` mode).

In ``unified`` mode ``core/memory_cortex.py`` stops owning a private SQLite
database and becomes a compatibility facade over the *same* Saturn sidecar,
namespace and physical store that ``memory/memory_manager.py`` already uses.

The fake below is not a second memory engine. It stands in for the HTTP surface
of ``memory/sidecar.js`` (installed through ``UniversalBridge._request``) and
implements only the semantics these tests depend on: exact/structured dedupe
with ``seen`` merging, contradiction versioning by (kind, subject, predicate),
id-preserving import, active-only export and lexical ranking. Everything else
runs the production code: payload mapping, the coverage index, harvest
dedupe, habit identity, migration, the facade and the mode resolver.

Covered here: mode authority and the absence of runtime SQLite access, cortex
CRUD with stable int ids, engine-ranked recall/context, deterministic + native
harvesting with cross-profile dedupe and once-per-turn behaviour, habit logical
identity with frequency/action updates, copy-first idempotent SQLite migration
(memories *and* habits, untouched source, rollback copy, marker), restart
persistence, the shared store/namespace without destructive cross-pruning,
unavailable/disabled backend degradation, regressions for legacy/hybrid/full,
and one round trip against the real Node sidecar.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import core.memory_cortex as cortex_mod
import memory.cortex_saturn as cu
import memory.memory_manager as mm
import memory.saturn_migrate as saturn_migrate
import memory.universal_bridge as ub
from core.memory_cortex import MemoryCortex
from memory.saturn_mode import resolve_saturn_mode

SIDECAR_JS = Path(__file__).resolve().parent.parent / "memory" / "sidecar.js"
NODE_BINARY = shutil.which("node") or shutil.which("node.exe")

EMPTY_PROFILE = {
    "identity": {},
    "preferences": {},
    "projects": {},
    "relationships": {},
    "wishes": {},
    "notes": {},
}

DEDUPE_KINDS = {"note", "fact", "preference"}
ENGINE_KINDS = {"note", "turn", "episode", "fact", "preference", "summary", "document"}


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(token) > 2}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _not_found(path: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(path, 404, "Not Found", {}, None)


class NativeSidecar(ub.UniversalBridge):
    """In-process stand-in for ``memory/sidecar.js`` (engine semantics only)."""

    def __init__(
        self,
        store_path: Path | None = None,
        namespace: str = "origin",
        fail: bool = False,
    ) -> None:
        super().__init__()
        self.store_path = Path(store_path) if store_path else None
        self.namespace = namespace
        self.entries: "OrderedDict[str, dict]" = OrderedDict()
        self.tombstones: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.feedback_ids: list[str] = []
        self.deleted_ids: list[str] = []
        self.imported_ids: list[str] = []
        self.candidates: list[dict] = []
        self.fail = fail
        self.start_attempts = 0
        self._seq = 0
        if self.store_path and self.store_path.exists():
            self._reload()

    # -- persistence ---------------------------------------------------
    def _reload(self) -> None:
        data = json.loads(self.store_path.read_text(encoding="utf-8"))
        self.entries = OrderedDict((item["id"], item) for item in data.get("entries", []))
        self.tombstones = set(data.get("tombstones", []))
        self._seq = int(data.get("seq") or len(self.entries))

    def _persist(self) -> None:
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(
                {
                    "entries": list(self.entries.values()),
                    "tombstones": sorted(self.tombstones),
                    "seq": self._seq,
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # -- engine helpers -------------------------------------------------
    def active_entries(self) -> list[dict]:
        # Mirrors the engine's _active(): superseded rows keep their history
        # (validTo/supersededBy) but stop being live, so dedupe, recall and
        # export only see the current form of a fact.
        return [
            item for item in self.entries.values()
            if item["id"] not in self.tombstones and not item.get("validTo")
        ]

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
            for existing in self.active_entries():
                if self._identity(existing) == wanted:
                    # Engine merge: same fact restated -> seen + 1, importance
                    # nudged up, timestamp refreshed, no new row.
                    existing["seen"] = int(existing.get("seen") or 1) + 1
                    existing["importance"] = min(
                        1.0, max(existing["importance"], entry["importance"]) + 0.03
                    )
                    existing["at"] = entry["at"]
                    self._persist()
                    return {"id": existing["id"], "merged": True}

        superseded: list[str] = []
        if self._versioned(entry):
            for old in self.active_entries():
                if old["id"] == entry["id"] or not self._versioned(old):
                    continue
                if (
                    old["kind"] == entry["kind"]
                    and old.get("subject") == entry.get("subject")
                    and old.get("predicate") == entry.get("predicate")
                    and old.get("value") != entry.get("value")
                ):
                    old["validTo"] = _now_iso()
                    old["supersededBy"] = entry["id"]
                    superseded.append(old["id"])

        self.entries[entry["id"]] = entry
        self._persist()
        return {"id": entry["id"], "merged": False, "superseded": superseded}

    def _import(self, payloads: list[dict]) -> dict:
        imported = skipped = 0
        for payload in payloads:
            if not isinstance(payload, dict):
                skipped += 1
                continue
            entry_id = payload.get("id")
            if not entry_id or entry_id in self.tombstones:
                self.remember({**payload, "id": None})
                imported += 1
                continue
            if entry_id in self.entries:
                skipped += 1
                continue
            entry = self._entry(payload, str(entry_id))
            entry["seen"] = max(1, int(payload.get("seen") or 1))
            entry["accessCount"] = max(0, int(payload.get("accessCount") or 0))
            self.entries[entry["id"]] = entry
            self.imported_ids.append(entry["id"])
            imported += 1
        self._persist()
        return {"imported": imported, "skipped": skipped}

    def _recall(self, query: str, k: int, min_score: float | None) -> list[dict]:
        words = _tokens(query)
        if not words:
            return []
        rows: list[dict] = []
        for entry in self.active_entries():
            haystack = _tokens(
                f"{entry.get('text', '')} {json.dumps(entry.get('meta') or {}, ensure_ascii=False)}"
            )
            overlap = words & haystack
            if not overlap:
                continue
            relevance = round(len(overlap) / len(words), 4)
            score = round(0.5 * relevance + 0.3 * 0.9 + 0.2 * entry["importance"], 4)
            if min_score is not None and score < float(min_score):
                continue
            rows.append({**entry, "score": score, "relevance": relevance})
        rows.sort(key=lambda row: (-row["score"], row["id"]))
        return rows[: max(0, int(k))]

    # -- HTTP surface ---------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
        self.calls.append((method, path))
        if self.fail:
            raise urllib.error.URLError("connection refused")
        parts = urlsplit(path)
        route = parts.path
        query = parse_qs(parts.query)
        body = payload or {}

        if route == "/health":
            return {"ok": True, "mode": "lexical"}
        if route == "/export":
            return {"ok": True, "mode": "lexical", "entries": self.active_entries()}
        if route == "/entries":
            entries = self.active_entries()
            offset = int((query.get("offset") or ["0"])[0])
            limit = int((query.get("limit") or [str(len(entries))])[0])
            return {
                "ok": True,
                "total": len(entries),
                "mode": "lexical",
                "entries": entries[offset:offset + limit],
            }
        if route == "/diagnostics":
            return {
                "ok": True,
                "namespace": self.namespace,
                "mode": "lexical",
                "stats": {"entries": len(self.active_entries())},
                "metrics": {},
            }
        if route.startswith("/memory/") and method == "GET":
            entry_id = route.rsplit("/", 1)[-1]
            entry = self.entries.get(entry_id)
            if not entry or entry_id in self.tombstones:
                raise _not_found(path)
            return {"ok": True, "entry": entry}
        if route.startswith("/memory/") and method == "DELETE":
            entry_id = route.rsplit("/", 1)[-1]
            if entry_id not in self.entries or entry_id in self.tombstones:
                raise _not_found(path)
            self.tombstones.add(entry_id)
            self.deleted_ids.append(entry_id)
            self._persist()
            return {"ok": True, "id": entry_id, "forgotten": True}
        if route == "/remember" and method == "POST":
            return self.remember(body)
        if route == "/import" and method == "POST":
            return self._import(list(body.get("entries") or []))
        if route == "/recall" and method == "POST":
            min_score = body.get("minScore")
            hits = self._recall(
                str(body.get("query") or ""),
                int(body.get("k") or 5),
                None if min_score is None else float(min_score),
            )
            return {"hits": hits, "mode": "lexical"}
        if route == "/context" and method == "POST":
            hits = self._recall(str(body.get("query") or ""), int(body.get("k") or 8), None)
            text = "\n".join(f"- {hit['text']}" for hit in hits)
            return {"text": text, "entries": hits, "tokens": len(text.split()), "mode": "lexical"}
        if route == "/feedback" and method == "POST":
            entry_id = str(body.get("id") or "")
            entry = self.entries.get(entry_id)
            if not entry or entry_id in self.tombstones:
                return {"ok": False, "error": "NOT_FOUND"}
            delta = float(body.get("delta") or 0.03)
            entry["importance"] = min(1.0, float(entry["importance"]) + delta)
            self.feedback_ids.append(entry_id)
            self._persist()
            return {"ok": True, "id": entry_id}
        if route == "/extract" and method == "POST":
            return {"ok": True, "mode": "lexical", "candidates": list(self.candidates)}
        raise AssertionError(f"unexpected route {method} {path}")


class OfflineSidecar(NativeSidecar):
    """Installed but not serving; reads must never spawn Node."""

    def __init__(self) -> None:
        super().__init__(fail=True)

    def _start(self) -> bool:  # pragma: no cover - asserted, never succeeds
        self.start_attempts += 1
        return False


# ── Fixtures ─────────────────────────────────────────────────────────────────

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
    mm._native_state.update({"read_ok": False, "degraded": "", "reported": ""})


def _install(monkeypatch, bridge) -> None:
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def _cortex_db(workspace: Path) -> Path:
    return Path(workspace) / "config" / "memory_cortex.db"


def _write_sentinel(path: Path, payload: dict | None = None) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload or {"identity": {"name": {"value": "Ada", "updated": "2026-01-02"}}}, indent=2)
    path.write_text(text, encoding="utf-8")
    return text


def _cortex_records(sidecar: NativeSidecar) -> list[dict]:
    return [entry for entry in sidecar.active_entries() if (entry.get("meta") or {}).get("cortex")]


def _db_rows(path: Path) -> tuple:
    """Committed content of a cortex database, independent of page layout.

    A migration snapshot is produced by SQLite's online backup, so it is
    logically identical to the source but not necessarily byte-identical (the
    database header carries its own change counter). This is how the tests
    compare a snapshot to the file it came from.
    """
    conn = sqlite3.connect(str(path))
    try:
        memories = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, category, content, keywords, importance, created_at, "
                "last_accessed_at, access_count FROM memories ORDER BY id"
            )
        ]
        habits = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, trigger_event, suggested_action, frequency, last_triggered "
                "FROM habits ORDER BY id"
            )
        ]
    finally:
        conn.close()
    return memories, habits


def _make_legacy_db(path: Path, memories: list[tuple], habits: list[tuple]) -> None:
    """A byte-for-byte copy of the schema ``MemoryCortex._init_db`` creates."""
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
        for category, content, importance, created_at, accessed_at, access_count in memories:
            keywords = " ".join(sorted(_tokens(content)))
            conn.execute(
                "INSERT INTO memories (category, content, keywords, importance, created_at, "
                "last_accessed_at, access_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (category, content, keywords, importance, created_at, accessed_at, access_count),
            )
        for trigger, action, frequency, last_triggered in habits:
            conn.execute(
                "INSERT INTO habits (trigger_event, suggested_action, frequency, last_triggered) "
                "VALUES (?, ?, ?, ?)",
                (trigger, action, frequency, last_triggered),
            )
        conn.commit()
    finally:
        conn.close()


# ── Mode authority and the absence of runtime SQLite ────────────────────────

def test_unified_mode_grants_native_cortex_authority(workspace, monkeypatch):
    state = resolve_saturn_mode()
    assert state.mode == "unified"
    assert state.stage == 3
    assert state.cortex_native is True
    assert state.authoritative_store == "saturn"
    assert state.saturn_authoritative is True
    assert (state.native_reads, state.native_writes, state.json_writes) == (True, True, False)
    assert (state.dual_write, state.merged_recall, state.prompt_augment) == (False, False, False)

    db_path = _cortex_db(workspace)
    cortex = MemoryCortex(db_path=db_path)
    assert cortex.backend() == "saturn"
    assert not db_path.exists(), "unified mode must not create memory_cortex.db"

    with pytest.raises(RuntimeError, match="not used in unified mode"):
        cortex._get_connection()

    # No production path may open SQLite while serving unified reads/writes.
    def forbidden(*_args, **_kwargs):  # pragma: no cover - asserted below
        raise AssertionError("unified mode opened SQLite at runtime")

    monkeypatch.setattr(cortex_mod.sqlite3, "connect", forbidden)
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)

    assert isinstance(cortex.remember("User preferred editor: Neovim", "preference"), int)
    assert cortex.recall("Neovim")
    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    assert cortex.get_habit_suggestion("user says good morning", min_frequency=1)
    assert cortex.build_context_prompt("Neovim")
    assert cortex.get_stats()["total_memories"] == 1
    assert not db_path.exists()


# ── Cortex CRUD and stable int ids ──────────────────────────────────────────

def test_cortex_crud_returns_stable_int_ids(workspace, monkeypatch):
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    first = cortex.remember("User preferred editor: Neovim", "preference")
    assert isinstance(first, int) and first > 0

    # The same statement is one entry, so the id is stable, not a new row.
    assert cortex.remember("User preferred editor: Neovim", "preference") == first
    records = _cortex_records(bridge)
    assert len(records) == 1
    record = records[0]
    assert cu.cortex_int(record["id"]) == first
    assert record["src"] == "saturn-cortex"
    assert record["kind"] == "preference"
    assert record["meta"]["cortex_record"] == "memory"
    assert record["meta"]["cortex_category"] == "preference"
    assert record["tags"] == ["saturn-cortex", "cortex:preference"]
    assert "cortex:preference" in record["tags"]
    # Cortex identity must never look like a profile entry.
    assert "saturn_category" not in record["meta"]
    assert "saturn_key" not in record["meta"]

    other = cortex.remember("User drinks oolong tea", "preference")
    assert other != first
    native_ids = [record["id"] for record in _cortex_records(bridge)]
    assert {cu.cortex_int(native_id) for native_id in native_ids} == {first, other}

    assert cortex.forget(other) is True
    assert [row["id"] for row in cortex.recall("oolong tea")] == []
    assert cortex.forget(other) is False
    assert cortex.forget("not-an-int") is False
    assert [row["id"] for row in cortex.recall("Neovim")] == [first]

    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    assert cortex.get_stats()["total_habits"] == 1
    cortex.clear_all_memories()
    assert _cortex_records(bridge) == []
    assert cortex.get_stats()["total_memories"] == 0
    assert cortex.get_stats()["total_habits"] == 0


# ── Native recall and context ───────────────────────────────────────────────

def test_native_recall_and_context_are_engine_ranked(workspace, monkeypatch):
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    neovim = cortex.remember("User preferred editor: Neovim", "preference")
    tea = cortex.remember("User drinks oolong tea every morning", "preference")
    cortex.remember("User name (identity): Ada", "identity")

    hits = cortex.recall("Neovim editor")
    assert hits, "engine recall must find the stored fact"
    assert hits[0]["id"] == neovim
    assert hits[0]["category"] == "preference"
    assert hits[0]["content"] == "User preferred editor: Neovim"
    assert set(hits[0]) == {
        "id", "category", "content", "score", "importance", "created_at", "access_count"
    }
    assert isinstance(hits[0]["score"], float) and hits[0]["score"] > 0
    assert isinstance(hits[0]["created_at"], float)
    assert all("oolong" not in row["content"] for row in hits)

    assert cortex.recall("") == []
    assert cortex.recall("oolong", top_k=1)[0]["id"] == tea
    assert len(cortex.recall("Neovim oolong", top_k=1)) == 1
    # min_score is honoured, so a caller can request only strong matches.
    assert all(row["score"] >= 0.4 for row in cortex.recall("Neovim oolong", min_score=0.4))
    assert cortex.recall("Neovim", min_score=0.999) == []

    prompt = cortex.build_context_prompt("Neovim editor", max_items=4)
    assert prompt.startswith("[RECALLED LONG-TERM MEMORY & USER CONTEXT]:")
    assert "- (Preference) User preferred editor: Neovim" in prompt
    assert prompt.endswith("Use this background knowledge naturally when responding.")
    assert cortex.build_context_prompt("") == ""

    # The same store now backs both surfaces: a profile entry that already
    # says the fact suppresses the cortex line instead of duplicating it.
    assert mm.remember("editor", "Neovim", "preferences").startswith("Remembered")
    fresh = MemoryCortex(db_path=_cortex_db(workspace))
    assert fresh.build_context_prompt("Neovim editor", max_items=4) == ""


# ── Harvesting: deterministic + native, cross-profile dedupe ────────────────

def test_harvest_runs_deterministic_then_native_with_cross_profile_dedupe(workspace, monkeypatch):
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    facts = cortex.harvest_turn("my name is Ada")
    assert facts == ["User name (identity): Ada"]

    # The profile harvester owns this statement; the cortex pass reinforced
    # that entry instead of storing a second copy of the same sentence.
    ada_entries = [entry for entry in bridge.active_entries() if "Ada" in entry["text"]]
    assert len(ada_entries) == 1
    assert ada_entries[0]["meta"].get("saturn_category") == "identity"
    assert "cortex" not in ada_entries[0]["meta"]
    assert bridge.feedback_ids == [ada_entries[0]["id"]]

    # A statement only the cortex patterns understand is stored natively.
    directive = cortex.harvest_turn("always remember the garage pin is 4417")
    assert directive == ["User directive: the garage pin is 4417"]
    cortex_records = _cortex_records(bridge)
    assert len(cortex_records) == 1
    assert cortex_records[0]["meta"]["cortex_category"] == "directive"
    assert cortex_records[0]["kind"] == "note"

    # Engine extraction is an addition, not a second source of duplicates.
    bridge.candidates = [
        {"text": "User prefers tabs over spaces", "kind": "preference"},
        {"text": "User name (identity): Ada", "kind": "fact"},
    ]
    cortex.harvest_turn("I like vim keybindings")
    extracted = [
        entry for entry in _cortex_records(bridge)
        if (entry.get("meta") or {}).get("cortex_extracted")
    ]
    assert [entry["text"] for entry in extracted] == ["User prefers tabs over spaces"]
    assert extracted[0]["meta"]["cortex_category"] == "preference"
    assert len([entry for entry in bridge.active_entries() if "Ada" in entry["text"]]) == 1

    assert cortex.harvest_turn("hi") == []


def test_harvest_turn_is_once_per_turn_across_call_sites(workspace, monkeypatch):
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))
    text = "always remember the garage pin is 4417"

    first = cortex.harvest_turn(text)
    count = len(bridge.active_entries())
    feedback_before = list(bridge.feedback_ids)

    second = cortex.harvest_turn(text)          # dispatcher-style repeat
    profile = mm.harvest_turn(text)             # main.py-style repeat

    assert first == second == ["User directive: the garage pin is 4417"]
    assert profile == {}
    assert len(bridge.active_entries()) == count, "one turn must write one entry"
    assert len(bridge.feedback_ids) > len(feedback_before), "repeat turns reinforce, not rewrite"
    assert len(_cortex_records(bridge)) == 1


# ── Habits: stable logical identity, frequency and action updates ──────────

def test_habit_identity_is_stable_and_frequency_updates(workspace, monkeypatch):
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    trigger = "user says good morning"
    action = "reads the daily briefing"
    key, entry_id = cu.habit_identity(trigger)
    # Lossless identity: a readable prefix plus a digest of the whole trigger.
    # The slug alone would merge two triggers that share their opening words.
    assert key.startswith("user-says-good-morning-")
    assert len(key) == len("user-says-good-morning-") + 16
    assert key != cu.habit_identity(trigger + " at the office")[0]
    assert entry_id.startswith("brc_")
    assert cu.habit_identity("  User SayS   Good Morning ") == (key, entry_id)

    cortex.record_habit_event(trigger, action)
    cortex.record_habit_event("User says good morning", "Reads the daily briefing")

    habits = [entry for entry in _cortex_records(bridge) if entry["meta"]["cortex_record"] == "habit"]
    assert len(habits) == 1, "occurrences merge into one logical habit"
    assert habits[0]["meta"]["cortex_key"] == key
    assert habits[0]["predicate"] == f"cortex.habit.{key}"
    assert habits[0]["seen"] == 2
    assert habits[0]["meta"]["cortex_action"] == action
    assert habits[0]["meta"]["cortex_category"] == "habit"

    assert cortex.get_habit_suggestion(trigger, min_frequency=2) == action
    assert cortex.get_habit_suggestion(trigger, min_frequency=3) is None
    assert cortex.get_habit_suggestion("unknown trigger", min_frequency=1) is None

    # A new action supersedes the old form: still one active habit per trigger.
    new_action = "opens the calendar"
    cortex.record_habit_event(trigger, new_action)
    habits = [entry for entry in _cortex_records(bridge) if entry["meta"]["cortex_record"] == "habit"]
    assert len(habits) == 1
    assert habits[0]["meta"]["cortex_action"] == new_action
    assert habits[0]["seen"] == 1
    assert cortex.get_habit_suggestion(trigger, min_frequency=2) is None
    assert cortex.get_habit_suggestion(trigger, min_frequency=1) == new_action

    # Repeating the new action resumes counting on the same identity.
    cortex.record_habit_event(trigger, new_action)
    assert cortex.get_habit_suggestion(trigger, min_frequency=2) == new_action
    habits = [entry for entry in _cortex_records(bridge) if entry["meta"]["cortex_record"] == "habit"]
    assert len(habits) == 1 and habits[0]["seen"] == 2

    cortex.record_habit_event("", "nothing")
    assert len(_cortex_records(bridge)) == 1


# ── Copy-first, idempotent SQLite migration ────────────────────────────────

def test_cortex_migration_is_copy_first_and_idempotent(workspace, monkeypatch):
    db_path = _cortex_db(workspace)
    now = time.time()
    created_identity = now - 172800.0
    created_preference = now - 86400.0
    _make_legacy_db(
        db_path,
        memories=[
            ("identity", "User name (identity): Ada", 0.9, created_identity, now - 3600.0, 3),
            ("preference", "User preferred editor: Neovim", 0.8, created_preference, now - 60.0, 1),
        ],
        habits=[("user says good morning", "reads the daily briefing", 4, now - 120.0)],
    )
    before_bytes = db_path.read_bytes()

    bridge = NativeSidecar()
    _install(monkeypatch, bridge)

    # Snapshot-first: the source is opened exactly once, read-only, and only to
    # take a transactionally consistent backup. Every row read targets the
    # snapshot.
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(cortex_mod.sqlite3, "connect", spy)
    monkeypatch.setattr(cu.sqlite3, "connect", spy)

    cortex = MemoryCortex(db_path=db_path)
    stats = cortex.get_stats()  # first read triggers the migration

    assert opened, "migration must take a snapshot"
    source_opens = [target for target in opened if db_path.name in target and "rollback" not in target]
    assert len(source_opens) == 1, opened
    assert source_opens[0].endswith("?mode=ro"), source_opens
    row_reads = [target for target in opened if "rollback" in target and target.endswith("?mode=ro")]
    assert row_reads, opened

    # Source untouched; the snapshot holds the same committed rows and is
    # fingerprinted as the state the importer actually read.
    assert db_path.read_bytes() == before_bytes
    marker = cu.read_marker(db_path)
    rollback = Path(marker["rollback"])
    assert rollback.exists()
    assert _db_rows(rollback) == _db_rows(db_path)
    assert marker["version"] == cu.MIGRATION_VERSION
    assert marker["source_sha256"] == hashlib.sha256(rollback.read_bytes()).hexdigest()
    assert marker["source_files_sha256"] == cu.source_files_digest(db_path)
    assert marker["imported_at"]
    assert marker["namespace"] == "origin"
    assert marker["destination"]["namespace"] == "origin"
    assert marker["counts"] == {"memories": 2, "habits": 1, "imported": 3, "skipped": 0}
    assert cu.migration_status(db_path)["source_present"] is True

    # Memories and habits both landed, with stable ids and preserved state.
    identity_id = cu.migrated_memory_id("identity", "User name (identity): Ada", created_identity)
    preference_id = cu.migrated_memory_id(
        "preference", "User preferred editor: Neovim", created_preference
    )
    _habit_key, habit_id = cu.habit_identity("user says good morning")
    assert set(bridge.imported_ids) == {identity_id, preference_id, habit_id}
    assert all(entry_id.startswith(cu.STABLE_ID_PREFIX) for entry_id in bridge.imported_ids)

    by_id = {entry["id"]: entry for entry in bridge.active_entries()}
    assert by_id[identity_id]["meta"]["cortex_migrated"] is True
    assert by_id[identity_id]["meta"]["cortex_source_id"] == 1
    assert by_id[identity_id]["meta"]["cortex_category"] == "identity"
    assert by_id[identity_id]["accessCount"] == 3
    assert by_id[habit_id]["meta"]["cortex_record"] == "habit"
    assert by_id[habit_id]["seen"] == 4
    assert by_id[habit_id]["meta"]["cortex_action"] == "reads the daily briefing"

    assert stats["available"] is True and stats["store"] == "saturn"
    assert stats["total_memories"] == 2
    assert stats["total_habits"] == 1
    assert stats["categories"] == {"identity": 1, "preference": 1}

    hits = cortex.recall("Neovim editor")
    assert hits and hits[0]["id"] == cu.cortex_int(preference_id)
    assert cortex.get_habit_suggestion("user says good morning", min_frequency=4) == (
        "reads the daily briefing"
    )

    # Idempotent: the process guard, the marker and the engine import all agree.
    cu.reset_migration_state()
    again = cu.ensure_cortex_migrated(bridge=bridge, source=db_path)
    assert again["reason"] == "already_imported"
    assert again["imported"] == 0
    assert len(bridge.active_entries()) == 3

    second = cu.migrate_cortex_db(source=db_path, bridge=bridge)
    assert second["ok"] is True
    assert (second["imported"], second["skipped"]) == (0, 3)
    assert len(bridge.active_entries()) == 3
    assert db_path.read_bytes() == before_bytes

    # A deleted native fact is not resurrected by an unchanged snapshot.
    assert cortex.forget(cu.cortex_int(identity_id)) is True
    cu.reset_migration_state()
    assert cu.ensure_cortex_migrated(bridge=bridge, source=db_path)["reason"] == "already_imported"
    assert identity_id not in {entry["id"] for entry in bridge.active_entries()}
    assert len(bridge.active_entries()) == 2


def test_cortex_migration_is_triggered_by_the_first_native_write(workspace, monkeypatch):
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("fact", "User owns a blue bicycle", 0.6, now - 600.0, now - 600.0, 0)],
        habits=[],
    )
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)

    cortex = MemoryCortex(db_path=db_path)
    new_id = cortex.remember("User drinks oolong tea", "preference")

    assert isinstance(new_id, int)
    texts = {entry["text"] for entry in bridge.active_entries()}
    assert "User owns a blue bicycle" in texts
    assert "User drinks oolong tea" in texts
    assert cu.read_marker(db_path)["counts"]["imported"] == 1
    assert cortex.get_stats()["total_memories"] == 2


# ── Restart persistence ────────────────────────────────────────────────────

def test_unified_cortex_survives_restart(workspace, monkeypatch):
    store_path = Path(workspace) / "native" / "restart.jsonl"
    db_path = _cortex_db(workspace)

    first_run = NativeSidecar(store_path=store_path)
    _install(monkeypatch, first_run)
    cortex = MemoryCortex(db_path=db_path)
    fact_id = cortex.remember("User preferred editor: Neovim", "preference")
    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    count = len(first_run.active_entries())
    assert count == 2

    # New process: fresh guards, fresh bridge over the same native file.
    _reset_state()
    second_run = NativeSidecar(store_path=store_path)
    _install(monkeypatch, second_run)
    restarted = MemoryCortex(db_path=db_path)

    hits = restarted.recall("Neovim editor")
    assert hits and hits[0]["id"] == fact_id, "int ids must be stable across restarts"
    assert restarted.get_habit_suggestion("user says good morning", min_frequency=2) == (
        "reads the daily briefing"
    )
    assert restarted.get_stats()["total_memories"] == 1
    assert restarted.get_stats()["total_habits"] == 1
    assert len(second_run.active_entries()) == count
    assert not db_path.exists()


# ── One store and namespace, no destructive cross-pruning ──────────────────

def test_cortex_and_profile_share_one_store_without_cross_pruning(workspace, monkeypatch):
    store_path = Path(workspace) / "native" / "shared.jsonl"
    bridge = NativeSidecar(store_path=store_path)
    _install(monkeypatch, bridge)

    assert mm.remember("name", "Ada", "identity").startswith("Remembered")
    cortex = MemoryCortex(db_path=_cortex_db(workspace))
    cortex_id = cortex.remember("User drinks oolong tea", "preference")
    cortex.record_habit_event("user says good morning", "reads the daily briefing")

    entries = bridge.active_entries()
    assert len(entries) == 3
    assert {entry["namespace"] for entry in entries} == {"origin"}
    assert len([entry for entry in entries if entry["meta"].get("saturn_category")]) == 1
    assert len(_cortex_records(bridge)) == 2
    # One physical store, not two: the fake persists to a single file.
    assert store_path.exists()
    assert sorted(path.name for path in store_path.parent.iterdir()) == ["shared.jsonl"]

    # A profile reconcile prunes only profile identities it owns.
    mm.update_memory({"notes": {"coffee": {"value": "flat white"}}})
    remaining = bridge.active_entries()
    assert len(remaining) == 4
    assert any(entry["meta"].get("cortex") and entry["text"] == "User drinks oolong tea" for entry in remaining)
    assert any(entry["meta"].get("cortex_record") == "habit" for entry in remaining)

    # Clearing the cortex surface never touches profile memory.
    cortex.clear_all_memories()
    profile_entries = [entry for entry in bridge.active_entries() if entry["meta"].get("saturn_category")]
    assert {entry["meta"]["saturn_key"] for entry in profile_entries} == {"name", "coffee"}
    assert _cortex_records(bridge) == []
    assert cortex.get_stats()["total_memories"] == 0
    assert mm.load_memory()["identity"]["name"]["value"] == "Ada"
    assert isinstance(cortex_id, int)
    assert cortex.forget(cortex_id) is False


# ── Unavailable and disabled backends ─────────────────────────────────────

def test_unavailable_backend_degrades_reads_and_fails_writes_safely(workspace, monkeypatch):
    source = Path(workspace) / "memory" / "long_term.json"
    original = _write_sentinel(source)
    offline = OfflineSidecar()
    _install(monkeypatch, offline)
    monkeypatch.setenv("SATURN_MEMORY_AUTOSTART", "1")

    db_path = _cortex_db(workspace)
    cortex = MemoryCortex(db_path=db_path)
    assert cortex.backend() == "saturn"

    assert cortex.recall("anything") == []
    assert cortex.build_context_prompt("anything") == ""
    stats = cortex.get_stats()
    assert stats["available"] is False
    assert stats["store"] == "saturn"
    assert stats["total_memories"] == 0 and stats["total_habits"] == 0
    assert offline.start_attempts == 0, "a read must never spawn Node"

    assert cortex.remember("User name (identity): Ada") == -1
    assert offline.start_attempts >= 1, "a write may try to start the sidecar"
    assert cortex.forget(123456) is False
    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    assert cortex.get_habit_suggestion("user says good morning", min_frequency=1) is None
    assert cortex.harvest_turn("always remember the garage pin is 4417") == []
    cortex.clear_all_memories()
    assert offline.active_entries() == []

    # Reads degrade the profile too, and nothing is written as a fallback.
    assert mm.load_memory() == EMPTY_PROFILE
    assert mm.degraded_status()["active"] is True
    assert source.read_text(encoding="utf-8") == original
    assert not db_path.exists()


def test_disabled_backend_is_reported_not_silently_used(workspace, monkeypatch):
    _install(monkeypatch, None)
    cortex = MemoryCortex(db_path=_cortex_db(workspace))

    assert cortex.remember("User name (identity): Ada") == -1
    assert cortex.recall("Ada") == []
    assert cortex.get_habit_suggestion("user says good morning", 1) is None
    stats = cortex.get_stats()
    assert stats["available"] is False and stats["total_memories"] == 0
    assert mm.remember("name", "Ada", "identity").startswith("Remember failed")


# ── Regressions: legacy, hybrid and full keep the SQLite cortex ────────────

@pytest.mark.parametrize("mode", ["legacy", "hybrid"])
def test_legacy_and_hybrid_keep_the_sqlite_cortex(mode, workspace, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_MODE", mode)
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    db_path = _cortex_db(workspace)

    state = resolve_saturn_mode()
    assert state.mode == mode
    assert state.cortex_native is False

    cortex = MemoryCortex(db_path=db_path)
    assert cortex.backend() == "sqlite"
    assert db_path.exists(), "non-unified modes keep the private SQLite database"

    memory_id = cortex.remember("User preferred editor: Neovim", "preference")
    assert isinstance(memory_id, int)
    assert cortex.remember("User preferred editor: Neovim", "preference") == memory_id
    assert [row["content"] for row in cortex.recall("Neovim editor")] == [
        "User preferred editor: Neovim"
    ]
    assert cortex.build_context_prompt("Neovim").startswith("[RECALLED LONG-TERM MEMORY")

    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    cortex.record_habit_event("user says good morning", "reads the daily briefing")
    assert cortex.get_habit_suggestion("user says good morning", 2) == "reads the daily briefing"

    stats = cortex.get_stats()
    assert stats["total_memories"] == 1
    assert stats["total_habits"] == 1
    assert stats["db_path"] == str(db_path)

    assert cortex.forget(memory_id) is True
    assert bridge.calls == [], f"{mode} cortex must not talk to Saturn"
    assert bridge.entries == {}


def test_full_mode_cortex_stays_sqlite_while_profile_is_native(workspace, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_MODE", "full")
    bridge = NativeSidecar()
    _install(monkeypatch, bridge)
    db_path = _cortex_db(workspace)

    state = resolve_saturn_mode()
    assert state.mode == "full"
    assert state.cortex_native is False
    assert state.saturn_authoritative is True

    cortex = MemoryCortex(db_path=db_path)
    assert cortex.backend() == "sqlite"
    cortex.remember("User drinks oolong tea", "preference")
    assert cortex.get_stats()["total_memories"] == 1
    assert _cortex_records(bridge) == [], "Stage Two cortex is still local"

    assert mm.remember("name", "Ada", "identity").startswith("Remembered")
    assert mm.load_memory()["identity"]["name"]["value"] == "Ada"
    assert [entry["meta"].get("saturn_key") for entry in bridge.active_entries()] == ["name"]
    assert cortex.get_stats()["total_memories"] == 1


# ── Real sidecar round trip (skipped when node is unavailable) ─────────────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(NODE_BINARY is None, reason="node is required to run memory/sidecar.js")
def test_real_sidecar_round_trip_for_cortex_records(workspace, monkeypatch):
    port = _free_port()
    data_file = Path(workspace) / "native" / "sidecar.jsonl"
    monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SATURN_MEMORY_PORT", str(port))
    monkeypatch.setenv("SATURN_MEMORY_HOST", "127.0.0.1")
    monkeypatch.setenv("SATURN_MEMORY_KEY", "test-key")
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
    monkeypatch.setenv("SATURN_MEMORY_EMBED", "off")

    env = {
        **os.environ,
        "SATURN_MEMORY_PORT": str(port),
        "SATURN_MEMORY_HOST": "127.0.0.1",
        "SATURN_MEMORY_FILE": str(data_file),
        "SATURN_MEMORY_KEY": "test-key",
        "SATURN_MEMORY_EMBED": "off",
        "SATURN_MEMORY_NAMESPACE": "origin",
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
    try:
        bridge = ub.UniversalBridge()
        deadline = time.time() + 20
        while time.time() < deadline and not bridge.health(timeout=0.5):
            time.sleep(0.15)
        assert bridge.health(), "sidecar failed to start"
        bridge.require()
        _install(monkeypatch, bridge)

        cortex = MemoryCortex(db_path=_cortex_db(workspace))
        first = cortex.remember("User preferred editor: Neovim", "preference")
        again = cortex.remember("User preferred editor: Neovim", "preference")
        assert first == again and isinstance(first, int), "engine merges the restated fact"

        hits = cortex.recall("Neovim editor")
        assert hits and hits[0]["id"] == first
        assert hits[0]["content"] == "User preferred editor: Neovim"

        cortex.record_habit_event("user says good morning", "reads the daily briefing")
        cortex.record_habit_event("user says good morning", "reads the daily briefing")
        assert cortex.get_habit_suggestion("user says good morning", 2) == "reads the daily briefing"

        exported = bridge.export_entries()["entries"]
        cortex_rows = [entry for entry in exported if (entry.get("meta") or {}).get("cortex")]
        assert len(cortex_rows) == 2
        assert {entry["namespace"] for entry in cortex_rows} == {"origin"}
        assert all(entry["src"] == "saturn-cortex" for entry in cortex_rows)
        # The profile reconciler sees no origin_* keys here, so it cannot prune them.
        assert bridge.native_index() == {}

        # Migration against the real engine: ids survive, recall works.
        legacy_db = Path(workspace) / "legacy" / "memory_cortex.db"
        created = time.time() - 7200.0
        _make_legacy_db(
            legacy_db,
            memories=[("identity", "User name (identity): Ada", 0.9, created, created, 2)],
            habits=[("user says good night", "turns off the lights", 3, created)],
        )
        report = cu.migrate_cortex_db(source=legacy_db, bridge=bridge)
        assert report["ok"] is True
        assert (report["memories"], report["habits"], report["imported"]) == (1, 1, 2)
        assert cu.cortex_int(cu.migrated_memory_id("identity", "User name (identity): Ada", created)) in {
            row["id"] for row in cortex.recall("Ada name")
        }
        assert cortex.get_habit_suggestion("user says good night", 3) == "turns off the lights"
        # The archive is a consistent snapshot of the same committed rows.
        assert _db_rows(Path(report["rollback"])) == _db_rows(legacy_db)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung node
            process.kill()
