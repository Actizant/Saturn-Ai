"""Focused tests for Stage Two: native Saturn ownership (``full`` mode).

The fake below is not a second implementation of the engine. It is a stand-in
for ``memory/sidecar.js``'s HTTP surface, installed through
``UniversalBridge._request``, so these tests exercise the real production code
paths: payload mapping, export parsing, reconcile/delete ordering, the import
driver and the migration importer. Only the Node process is replaced.

Covered here: migration round trip, idempotency, adapter CRUD, UI rows and
delete, native context merge, restart persistence, export/rollback artifacts,
native sessions, and an unavailable backend.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import memory.memory_manager as mm
import memory.saturn_migrate as migrate
import memory.universal_bridge as ub

SIDECAR_JS = Path(__file__).resolve().parent.parent / "memory" / "sidecar.js"
NODE_BINARY = shutil.which("node") or shutil.which("node.exe")

PROFILE = {
    "identity": {"name": {"value": "Ada", "updated": "2026-01-02"}},
    "preferences": {"favorite_color": {"value": "teal", "updated": "2026-02-03"}},
    "notes": {"coffee": {"value": "flat white", "updated": "2026-03-04"}},
    "sessions": [
        {"date": "2026-03-04", "summary": "Talked about coffee", "language": "english"},
        {"date": "2026-03-05", "summary": "Discussed colors", "language": "turkish"},
    ],
}

EMPTY_PROFILE = {
    "identity": {},
    "preferences": {},
    "projects": {},
    "relationships": {},
    "wishes": {},
    "notes": {},
}

ARTIFACT_KEYS = set(EMPTY_PROFILE) | {"sessions"}


def _not_found(path: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(path, 404, "Not Found", {}, None)


class FakeSaturn(ub.UniversalBridge):
    """In-process sidecar: engine routes + the three Saturn-only routes."""

    def __init__(self, store_path: Path | None = None) -> None:
        super().__init__()
        self.store_path = Path(store_path) if store_path else None
        self.entries: "OrderedDict[str, dict]" = OrderedDict()
        self.tombstones: set[str] = set()
        self.calls: list[tuple[str, str]] = []
        self.context_queries: list[str] = []
        self.fail = False
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

    # -- engine operations ---------------------------------------------
    def active_entries(self) -> list[dict]:
        return [item for item in self.entries.values() if item["id"] not in self.tombstones]

    def _next_id(self) -> str:
        self._seq += 1
        return f"m_000{self._seq:04x}_{self._seq:04x}"

    def _build(self, payload: dict, entry_id: str) -> dict:
        text = str(payload.get("text") or "")
        value = payload.get("value")
        return {
            "id": entry_id,
            "text": text,
            "value": value if value is not None else text[:200],
            "kind": str(payload.get("kind") or "note"),
            "tags": list(payload.get("tags") or []),
            "src": str(payload.get("src") or "origin"),
            "importance": float(payload.get("importance") or 0.5),
            "at": str(payload.get("at") or "2026-01-01T12:00:00"),
            "meta": dict(payload.get("meta") or {}),
        }

    def remember(self, payload: dict) -> dict:
        entry = self._build(payload, self._next_id())
        self.entries[entry["id"]] = entry
        self._persist()
        return entry

    def _import(self, payloads: list[dict]) -> dict:
        imported = skipped = 0
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            entry_id = payload.get("id")
            if not entry_id or entry_id in self.tombstones:
                self.remember({**payload, "id": None})
                imported += 1
                continue
            if entry_id in self.entries:
                skipped += 1
                continue
            self.entries[entry_id] = self._build(payload, entry_id)
            imported += 1
        self._persist()
        return {"imported": imported, "skipped": skipped}

    def forget_native(self, entry_id: str) -> dict:
        if entry_id not in self.entries or entry_id in self.tombstones:
            raise _not_found(f"/memory/{entry_id}")
        self.tombstones.add(entry_id)
        self._persist()
        return {"ok": True, "id": entry_id, "forgotten": True}

    def _context(self, query: str, limit: int) -> dict:
        words = [word for word in re.split(r"[^\w]+", query.lower()) if len(word) > 1]
        hits: list[tuple[int, dict]] = []
        for entry in self.active_entries():
            haystack = f"{entry.get('text', '')} {json.dumps(entry.get('meta') or {}, ensure_ascii=False)}".lower()
            score = sum(1 for word in words if word in haystack)
            if score:
                hits.append((score, entry))
        hits.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        selected = [entry for _score, entry in hits[:limit]]
        text = "\n".join(f"- {entry['text']}" for entry in selected)
        return {
            "text": text,
            "entries": selected,
            "tokens": len(text.split()),
            "mode": "lexical",
        }

    # -- HTTP surface ---------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None, timeout: float = 10.0):
        self.calls.append((method, path))
        if self.fail:
            raise urllib.error.URLError("connection refused")
        parts = urlsplit(path)
        route = parts.path
        query = parse_qs(parts.query)

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
            return self.forget_native(route.rsplit("/", 1)[-1])
        if route == "/remember" and method == "POST":
            return self.remember(payload or {})
        if route == "/import" and method == "POST":
            return self._import((payload or {}).get("entries") or [])
        if route == "/context" and method == "POST":
            body = payload or {}
            self.context_queries.append(str(body.get("query") or ""))
            return self._context(str(body.get("query") or ""), int(body.get("k") or 8))
        if route == "/recall" and method == "POST":
            hits = self._context(str((payload or {}).get("query") or ""), int((payload or {}).get("k") or 8))
            return {"hits": hits["entries"], "mode": "lexical"}
        raise AssertionError(f"unexpected route {method} {path}")


class OfflineSaturn(FakeSaturn):
    """Sidecar that is installed but not serving; must never be started."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True
        self.start_attempts = 0

    def _start(self) -> bool:  # pragma: no cover - asserted below
        self.start_attempts += 1
        return False


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    source = tmp_path / "memory" / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", source)
    monkeypatch.setenv("SATURN_MEMORY_MODE", "full")
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(tmp_path / "native" / "universal.jsonl"))
    monkeypatch.setenv("SATURN_MEMORY_LONG_TERM", str(source))
    monkeypatch.setenv("SATURN_MEMORY_EMBED", "off")
    monkeypatch.setenv("SATURN_MEMORY_AUTOSTART", "0")
    for name in ("SATURN_MEMORY_BACKEND", "SATURN_MEMORY_ENABLED", "SATURN_MEMORY_MIGRATE"):
        monkeypatch.delenv(name, raising=False)
    _reset_state()
    yield source
    _reset_state()


def _reset_state() -> None:
    migrate.reset_migration_state()
    mm._native_state.update({"read_ok": False, "degraded": "", "reported": ""})


def _write_store(path: Path, profile: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(profile, indent=2, ensure_ascii=False)
    path.write_text(text, encoding="utf-8")
    return text


def _install(monkeypatch, bridge) -> None:
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def _identity_rows(bridge: FakeSaturn, category: str, key: str) -> list[dict]:
    return bridge.native_index().get((category, key), [])


# ── Migration round trip ─────────────────────────────────────────────────────

def test_full_mode_migrates_json_preserving_identity_dates_and_sessions(workspace, monkeypatch):
    original = _write_store(workspace, PROFILE)
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)

    profile = mm.load_memory()

    # Values, dates and categories survive the move.
    assert profile["identity"]["name"] == {"value": "Ada", "updated": "2026-01-02"}
    assert profile["preferences"]["favorite_color"] == {"value": "teal", "updated": "2026-02-03"}
    assert profile["notes"]["coffee"] == {"value": "flat white", "updated": "2026-03-04"}
    assert [session["summary"] for session in profile["sessions"]] == [
        "Talked about coffee",
        "Discussed colors",
    ]
    assert [session["date"] for session in profile["sessions"]] == ["2026-03-04", "2026-03-05"]
    assert profile["sessions"][1]["language"] == "turkish"

    # Stable migration identity: the deterministic id is what landed natively.
    stable = migrate.stable_entry_id("preferences", "favorite_color")
    assert stable in bridge.entries
    assert bridge.entries[stable]["meta"]["saturn_value"] == "teal"
    session_ids = [
        item["id"] for item in bridge.active_entries()
        if item["meta"].get("saturn_category") == "sessions"
    ]
    assert len(session_ids) == 2
    assert all(entry_id.startswith("br_") for entry_id in session_ids)

    # Copy-first: the source is byte-identical and a rollback copy exists.
    assert workspace.read_text(encoding="utf-8") == original
    marker = migrate.read_marker(workspace)
    assert marker["counts"] == {"entries": 3, "sessions": 2, "imported": 5, "updated": 0, "skipped": 0}
    rollback = Path(marker["rollback"])
    assert rollback.exists()
    assert rollback.read_text(encoding="utf-8") == original

    # Migration used the engine's own import, not a second writer.
    assert any(method == "POST" and path == "/import" for method, path in bridge.calls)


def test_migration_is_idempotent_and_never_rewrites_the_source(workspace, monkeypatch):
    original = _write_store(workspace, PROFILE)
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)

    first = migrate.import_from_json(source=workspace, bridge=bridge)
    assert first["ok"] is True
    assert (first["imported"], first["updated"], first["skipped"]) == (5, 0, 0)
    count_after_first = len(bridge.active_entries())

    second = migrate.import_from_json(source=workspace, bridge=bridge)
    assert second["ok"] is True
    assert (second["imported"], second["updated"], second["skipped"]) == (0, 0, 5)
    assert len(bridge.active_entries()) == count_after_first
    assert sorted(bridge.entries) == sorted(set(bridge.entries))
    assert workspace.read_text(encoding="utf-8") == original

    # The process-level guard is not the only protection: a second process
    # skips a snapshot the marker already recorded, so native edits made after
    # the migration are not reverted by a stale JSON file.
    migrate.reset_migration_state()
    bridge.remember({"text": "Lives in Lviv", "kind": "note"})
    again = migrate.ensure_migrated(bridge=bridge, source=workspace)
    assert again["reason"] == "already_imported"
    assert len(bridge.active_entries()) == count_after_first + 1


# ── Adapter CRUD ─────────────────────────────────────────────────────────────

def test_full_mode_crud_goes_through_adapters_and_never_writes_json(workspace, monkeypatch):
    original = _write_store(workspace, PROFILE)
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)
    mm.load_memory()

    assert mm.remember("city", "Kyiv", "identity") == "Remembered: identity/city = Kyiv"
    assert mm.load_memory()["identity"]["city"]["value"] == "Kyiv"
    assert len(_identity_rows(bridge, "identity", "city")) == 1

    # Updating the same identity replaces it instead of accumulating rows.
    assert mm.remember("city", "Lviv", "identity") == "Remembered: identity/city = Lviv"
    rows = _identity_rows(bridge, "identity", "city")
    assert len(rows) == 1
    assert rows[0]["value"] == "Lviv"

    updated = mm.update_memory({"preferences": {"favorite_color": {"value": "navy"}}})
    assert updated["preferences"]["favorite_color"]["value"] == "navy"
    assert _identity_rows(bridge, "preferences", "favorite_color")[0]["value"] == "navy"

    assert mm.forget("city", "identity") == "Forgotten: identity/city"
    assert _identity_rows(bridge, "identity", "city") == []
    assert "city" not in mm.load_memory()["identity"]
    assert mm.forget("ghost", "notes") == "Not found: notes/ghost"

    # Nothing above wrote JSON: in full mode it is migration/export/rollback only.
    assert workspace.read_text(encoding="utf-8") == original


def test_full_mode_ui_rows_and_delete(workspace, monkeypatch):
    _write_store(workspace, PROFILE)
    _install(monkeypatch, FakeSaturn())
    mm.load_memory()

    rows = mm.all_entries_for_ui()
    assert rows
    assert all(set(row) == {"category", "key", "value", "updated"} for row in rows)
    assert {row["category"] for row in rows} == {"identity", "preferences", "notes"}
    by_identity = {(row["category"], row["key"]): row["value"] for row in rows}
    assert by_identity[("identity", "name")] == "Ada"
    dates = [row["updated"] for row in rows]
    assert dates == sorted(dates, reverse=True)

    assert mm.forget("name", "identity") == "Forgotten: identity/name"
    remaining = {(row["category"], row["key"]) for row in mm.all_entries_for_ui()}
    assert ("identity", "name") not in remaining
    assert ("preferences", "favorite_color") in remaining


# ── Context merge ───────────────────────────────────────────────────────────

def test_full_mode_context_merge_adds_only_native_only_material(workspace, monkeypatch):
    _write_store(workspace, PROFILE)
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)
    mm.load_memory()
    bridge.remember({"text": "Loves jazz on Sundays", "kind": "note", "src": "api"})

    first = mm.search_memory("jazz")
    second = mm.search_memory("jazz")

    assert first == second, "recall must stay deterministic"
    assert first.startswith("Stored facts matching 'jazz':")
    assert "saturn/memory: Loves jazz on Sundays" in first
    assert bridge.context_queries

    # A fact the profile already owns is never duplicated by the native merge.
    owned = mm.search_memory("teal")
    assert owned.count("preferences/favorite color: teal") == 1
    assert "saturn/memory" not in owned


# ── Restart persistence ─────────────────────────────────────────────────────

def test_full_mode_survives_restart_without_reimporting(workspace, monkeypatch):
    _write_store(workspace, PROFILE)
    native_path = workspace.parent.parent / "native" / "restart.jsonl"

    first_run = FakeSaturn(store_path=native_path)
    _install(monkeypatch, first_run)
    mm.load_memory()
    mm.remember("city", "Kyiv", "identity")
    count = len(first_run.active_entries())

    # Simulate a new process: fresh bridge over the same native file, fresh
    # in-process guards.
    migrate.reset_migration_state()
    mm._native_state.update({"read_ok": False, "degraded": "", "reported": ""})
    second_run = FakeSaturn(store_path=native_path)
    _install(monkeypatch, second_run)

    profile = mm.load_memory()

    assert profile["identity"]["city"]["value"] == "Kyiv"
    assert profile["identity"]["name"]["value"] == "Ada"
    assert len(second_run.active_entries()) == count, "restart must not duplicate or revert entries"


# ── Sessions ────────────────────────────────────────────────────────────────

def test_full_mode_session_summary_round_trip_stays_native(workspace, monkeypatch):
    original = _write_store(
        workspace,
        {"identity": {"name": {"value": "Ada", "updated": "2026-01-02"}}},
    )
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)
    mm.load_memory()

    mm.save_session_summary("First session about memory.", "english")
    mm.save_session_summary("Second session about the store.", "turkish")

    profile = mm.load_memory()
    assert [item["summary"] for item in profile["sessions"]] == [
        "First session about memory.",
        "Second session about the store.",
    ]
    assert profile["sessions"][1]["language"] == "turkish"

    popped = mm.pop_last_session()
    assert popped is not None and popped["summary"] == "Second session about the store."
    assert [item["summary"] for item in mm.load_memory()["sessions"]] == [
        "First session about memory."
    ]
    assert workspace.read_text(encoding="utf-8") == original


# ── Export / rollback artifacts ─────────────────────────────────────────────

def test_export_artifact_is_a_usable_rollback_store(workspace, monkeypatch):
    _write_store(workspace, PROFILE)
    bridge = FakeSaturn()
    _install(monkeypatch, bridge)
    mm.load_memory()
    mm.remember("city", "Kyiv", "identity")

    result = migrate.export_native_profile(bridge=bridge, base=workspace)
    assert result["ok"] is True
    artifact = Path(result["path"])
    assert artifact.exists()

    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert set(payload) == ARTIFACT_KEYS
    assert payload["identity"]["city"]["value"] == "Kyiv"
    assert [item["summary"] for item in payload["sessions"]] == [
        "Talked about coffee",
        "Discussed colors",
    ]
    assert migrate.read_marker(workspace)["last_export"] == str(artifact)
    artifact_bytes = artifact.read_bytes()

    # Artifacts are pruned so a long-running install cannot fill the disk.
    for _ in range(7):
        migrate.export_native_profile(bridge=bridge, base=workspace)
    assert len(list(artifact.parent.glob("*.json"))) <= 5

    # Rollback path: copy the artifact over long_term.json and switch back to a
    # file-owned mode. The artifact is a verbatim long_term.json.
    workspace.write_bytes(artifact_bytes)
    monkeypatch.setenv("SATURN_MEMORY_MODE", "legacy")
    rolled_back = mm.load_memory()
    assert rolled_back["identity"]["city"]["value"] == "Kyiv"
    assert rolled_back["identity"]["name"]["value"] == "Ada"


# ── Unavailable backend ─────────────────────────────────────────────────────

def test_unavailable_backend_degrades_reads_and_refuses_writes(workspace, monkeypatch):
    original = _write_store(workspace, PROFILE)
    offline = OfflineSaturn()
    _install(monkeypatch, offline)

    profile = mm.load_memory()
    assert profile == EMPTY_PROFILE
    assert mm.degraded_status()["active"] is True
    assert offline.start_attempts == 0, "a read must not spawn Node"

    message = mm.remember("city", "Kyiv", "identity")
    assert message.startswith("Remember failed — nothing was stored for identity/city")
    assert offline.active_entries() == []

    # A write is refused rather than applied over an unreadable store, so an
    # outage can never be mistaken for "the user deleted everything".
    with pytest.raises(ub.SaturnError):
        mm.update_memory({"notes": {"coffee": {"value": "espresso"}}})
    assert offline.active_entries() == []

    assert mm.search_memory("anything") == "Nothing stored about 'anything'."
    assert mm.all_entries_for_ui() == []
    assert mm.format_memory_for_prompt(None) == ""
    assert mm.pop_last_session() is None
    mm.save_session_summary("A session that cannot be stored.")
    assert offline.active_entries() == []

    diagnostics = mm.memory_diagnostics()
    assert diagnostics["authoritative_store"] == "saturn"
    assert diagnostics["degraded"]["active"] is True
    assert diagnostics["entries"] == 0
    assert diagnostics["migration"]["source"] == str(workspace)
    assert diagnostics["bridge"]["ready"] is False

    # JSON was never written as a fallback.
    assert workspace.read_text(encoding="utf-8") == original


def test_disabled_backend_is_reported_not_silently_used(workspace, monkeypatch):
    _write_store(workspace, PROFILE)
    _install(monkeypatch, None)
    _reset_state()

    assert mm.load_memory() == EMPTY_PROFILE
    message = mm.remember("city", "Kyiv", "identity")
    assert message.startswith("Remember failed — nothing was stored")
    assert "disabled" in message

    diagnostics = mm.memory_diagnostics()
    assert diagnostics["degraded"]["active"] is True
    assert diagnostics["saturn"]["mode"] == "full"


# ── Real sidecar routes ─────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(NODE_BINARY is None, reason="node is required to run memory/sidecar.js")
def test_sidecar_exposes_the_native_routes(tmp_path, monkeypatch):
    """The three Saturn-only routes exist and everything else still delegates."""
    port = _free_port()
    data_file = tmp_path / "native" / "sidecar.jsonl"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SATURN_MEMORY_KEY", "test-key")
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))

    env = {
        **os.environ,
        "SATURN_MEMORY_PORT": str(port),
        "SATURN_MEMORY_HOST": "127.0.0.1",
        "SATURN_MEMORY_FILE": str(data_file),
        "SATURN_MEMORY_KEY": "test-key",
        "SATURN_MEMORY_EMBED": "off",
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

        payload = ub.profile_payload(
            "preferences", "favorite_color", {"value": "teal", "updated": "2026-02-03"}
        )
        entry_id = str(bridge._request("POST", "/remember", payload)["id"])

        listing = bridge.list_entries()
        assert listing["total"] == 1
        assert listing["entries"][0]["id"] == entry_id

        entry = bridge.get_entry(entry_id)
        assert entry is not None
        assert entry["meta"]["saturn_value"] == "teal"

        diagnostics = bridge.native_diagnostics()
        assert diagnostics["ok"] is True
        assert diagnostics["stats"]["active"] >= 1

        stable = migrate.stable_entry_id("preferences", "favorite_color")
        assert bridge.import_entries([{**payload, "id": stable}])["imported"] == 1
        assert bridge.get_entry(stable)["id"] == stable
        assert bridge.import_entries([{**payload, "id": stable}])["skipped"] == 1

        context = bridge.native_context("teal")
        assert context["rows"]
        assert any("teal" in row["value"].lower() for row in context["rows"])

        assert bridge.delete_entry(stable) is True
        assert bridge.get_entry(stable) is None

        unauthorized = ub.UniversalBridge()
        unauthorized.api_key = "wrong-key"
        with pytest.raises(urllib.error.HTTPError):
            unauthorized.list_entries()
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - hung node
            process.kill()
