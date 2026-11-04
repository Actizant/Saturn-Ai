"""Reproducers for the six residual blockers found after the Task 10 pass.

Every test here failed against the previous code for a *specific* reason, and
each one is written against the real transport wherever the defect lived there:

1. A failed authoritative read returned a plain empty dict, so once the store
   recovered that dict could be saved and erase the profile. Provenance is now
   an unforgeable per-read token; a failed read is a distinct non-writable type.
2. The sidecar published its revision before the delegated write committed, so
   an export could name a revision whose contents did not exist yet. Reads and
   writes now share one queue and the bump happens after the awaited commit -
   reproduced here with a real slow-body request against the real sidecar.
3. ``/feedback`` was namespace-blind, so one namespace could reinforce another
   namespace's row.
4. The former loopback Ollama probe used ``urlopen`` (which follows 3xx) and
   the engine's embedding transport followed redirects with the memory payload
   attached. The Python probe is now gone entirely - embeddings are lexical-only
   and the bridge contacts no process but its own sidecar - while the engine's
   embedding transport still must request manual redirects (reproduced below).
5. A failed SQLite backup fell back to a byte copy, which is not a snapshot,
   and the importer read it while claiming success.
6. Migrated ids were derived from the process environment, and the cortex cache
   was keyed only on a process-wide generation, so two bridges shared rows.

Plus the two observations: ``CortexNativeStore._prepare`` must propagate a
failed migration instead of sleeping on it, and a destination identity must be
verified against what the sidecar reports it is serving.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

import memory.cortex_saturn as cu
import memory.memory_manager as mm
import memory.saturn_migrate as saturn_migrate
import memory.universal_bridge as ub
from core.memory_cortex import MemoryCortex

ROOT = Path(__file__).resolve().parent.parent
SIDECAR_JS = ROOT / "memory" / "sidecar.js"
ENGINE_JS = ROOT / "universal-memory" / "src" / "index.js"
OCR_JS = ROOT / "universal-memory" / "src" / "ocr.js"
NODE_BINARY = shutil.which("node") or shutil.which("node.exe")

API_KEY = "test-key"


# ── Shared fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A full-mode workspace with every in-process guard reset."""
    source = tmp_path / "memory" / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", source)
    monkeypatch.setenv("SATURN_MEMORY_MODE", "full")
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
        "SATURN_MEMORY_URL",
        "SATURN_MEMORY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    _reset_state()
    yield tmp_path
    _reset_state()


def _reset_state() -> None:
    MemoryCortex._instance = None
    cu.reset_migration_state()
    mm.reset_harvest_memo()
    with mm._lock:
        mm._native_state.update(
            {"read_ok": False, "degraded": "", "reported": "", "migration_error": ""}
        )


def _install(monkeypatch, bridge) -> None:
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def _profile_entry(value: str, updated: str = "2026-01-01") -> dict:
    return {"value": value, "updated": updated}


def _cortex_db(root: Path) -> Path:
    return Path(root) / "config" / "memory_cortex.db"


def _make_legacy_db(path: Path, memories: list[tuple], habits: list[tuple] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
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
            """
        )
        conn.execute(
            """
            CREATE TABLE habits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_event TEXT NOT NULL UNIQUE,
                suggested_action TEXT NOT NULL,
                frequency INTEGER DEFAULT 1,
                last_triggered REAL NOT NULL
            );
            """
        )
        for category, content, importance, created, accessed, count in memories:
            conn.execute(
                "INSERT INTO memories (category, content, keywords, importance, created_at, "
                "last_accessed_at, access_count) VALUES (?,?,?,?,?,?,?)",
                (category, content, "kw", importance, created, accessed, count),
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


class _CortexBridge:
    """A native store double that records exactly what the adapter writes."""

    def __init__(self, namespace: str = "origin", file: Path | None = None) -> None:
        self.namespace = namespace
        self.file = Path(file) if file is not None else Path(f"/nonexistent/{namespace}.jsonl")
        self.entries: list[dict] = []
        self.imported_payloads: list[dict] = []
        self.remembered: list[dict] = []
        self.deleted: list[str] = []
        self.feedback: list[str] = []
        self.exports = 0
        self._seq = 0

    # -- bridge surface ---------------------------------------------------
    def require(self) -> bool:
        return True

    def is_ready(self) -> bool:
        return True

    def available(self, autostart: bool = True) -> bool:
        return True

    def export_entries(self, timeout=None) -> dict:
        self.exports += 1
        return {"ok": True, "entries": [dict(row) for row in self.entries]}

    def import_entries(self, payloads, timeout=None) -> dict:
        self.imported_payloads.extend(payloads)
        added = 0
        for payload in payloads:
            if any(row["id"] == payload.get("id") for row in self.entries):
                continue
            self.entries.append(self._row(payload))
            added += 1
        return {"ok": True, "imported": added, "skipped": 0}

    def remember_entry(self, payload, timeout=None) -> dict:
        self.remembered.append(payload)
        row = self._row(payload)
        self.entries.append(row)
        return {"id": row["id"]}

    def delete_entry(self, entry_id, timeout=None) -> bool:
        self.deleted.append(str(entry_id))
        self.entries = [row for row in self.entries if row["id"] != entry_id]
        return True

    def feedback_entry(self, entry_id, delta: float = 0.03, timeout=None) -> bool:
        self.feedback.append(str(entry_id))
        return True

    def recall_entries(self, query, limit=None, timeout=None) -> list:
        return []

    def extract_candidates(self, text, limit=None, timeout=None) -> list:
        return []

    def load_memory(self, **kwargs) -> dict:
        return {}

    def save_memory(self, memory, **kwargs) -> bool:
        return True

    def _row(self, payload: dict) -> dict:
        self._seq += 1
        return {
            "id": str(payload.get("id") or f"m_{self._seq:04d}"),
            "text": str(payload.get("text") or ""),
            "namespace": str(payload.get("namespace") or self.namespace),
            "meta": dict(payload.get("meta") or {}),
            "seen": payload.get("seen") or 1,
            "importance": payload.get("importance") or 0.5,
        }


class _ProfileBridge(ub.UniversalBridge):
    """In-process profile surface: returns plain dicts, records writes."""

    def __init__(self, profile: dict | None = None, namespace: str = "origin", file=None):
        super().__init__()
        self.namespace = namespace
        self.file = Path(file) if file is not None else self.file
        self.profile = dict(profile or {})
        self.writes: list[dict] = []
        self._available = True

    def require(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def available(self, autostart: bool = True) -> bool:
        return True

    def load_memory(self, include_sessions: bool = True, timeout=None) -> dict:
        return json.loads(json.dumps(self.profile))

    def write_profile(
        self,
        memory,
        timeout=None,
        autostart: bool = True,
        include_sessions: bool = True,
        expected_revision=None,
    ) -> dict:
        self.writes.append(json.loads(json.dumps(dict(memory))))
        self.profile = dict(memory)
        return {"ok": True, "written": 1}


# ── Real sidecar plumbing ────────────────────────────────────────────────────

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
        "SATURN_MEMORY_KEY": API_KEY,
        "SATURN_MEMORY_EMBED": "off",
        "SATURN_MEMORY_NAMESPACE": namespace,
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [NODE_BINARY, str(SIDECAR_JS)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    return port, process


def _connect(monkeypatch, port: int, namespace: str) -> ub.UniversalBridge:
    monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SATURN_MEMORY_PORT", str(port))
    monkeypatch.setenv("SATURN_MEMORY_HOST", "127.0.0.1")
    monkeypatch.setenv("SATURN_MEMORY_KEY", API_KEY)
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", namespace)
    bridge = ub.UniversalBridge()
    deadline = time.time() + 25
    while time.time() < deadline and not bridge.health(timeout=0.5):
        time.sleep(0.15)
    assert bridge.health(), "sidecar failed to start"
    bridge.require()
    return bridge


def _stop(process) -> None:
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:  # pragma: no cover - hostile
        process.kill()


def _http(port: int, method: str, path: str, body=None, timeout: float = 5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"accept": "application/json", "authorization": f"Bearer {API_KEY}"}
        if payload is not None:
            headers["content-type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


requires_node = pytest.mark.skipif(NODE_BINARY is None, reason="node is required")


# ── Fix 1: outage snapshots cannot be written back ──────────────────────────

@requires_node
def test_real_sidecar_outage_snapshot_cannot_erase_a_recovered_profile(workspace, monkeypatch):
    """Replay: read OK -> outage -> recovery -> the outage object must be refused."""
    data_file = Path(workspace) / "native" / "outage.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    second = None
    try:
        # The bridge must be configured for the store this sidecar actually
        # serves. Left on the fixture default, the destination-identity check
        # would refuse the *fresh* write too, and the test would stop being
        # about snapshot provenance.
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        _install(monkeypatch, bridge)
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )

        good = mm.load_memory()
        assert isinstance(good, ub.ProfileSnapshot)
        assert ub.snapshot_state(good)["ok"] is True
        assert good["identity"]["name"]["value"] == "Ada"

        _stop(process)
        outage = mm.load_memory()
        assert isinstance(outage, ub.ProfileOutage)
        assert outage.writable is False
        assert ub.snapshot_state(outage)["ok"] is False
        assert ub.snapshot_state(outage)["issued"] is True
        assert "failed read" in ub.snapshot_state(outage)["reason"]
        assert outage == mm._empty_memory(), "an outage still reads like an empty profile"

        # Recovery: the store is healthy again and a later read succeeds.
        port2, second = _start_sidecar(data_file, "origin")
        bridge = _connect(monkeypatch, port2, "origin")
        _install(monkeypatch, bridge)
        recovered = mm.load_memory()
        assert ub.snapshot_state(recovered)["ok"] is True
        snapshot_state = mm.degraded_status()
        assert snapshot_state["read_ok"] is True

        # The stale outage is still refused *after* global health recovered.
        outage["identity"] = {"name": _profile_entry("nobody", "2026-02-02")}
        with pytest.raises(ub.SaturnWriteError) as excinfo:
            mm.save_memory(outage)
        assert "refusing to overwrite" in str(excinfo.value)
        assert bridge.load_memory()["identity"]["name"]["value"] == "Ada"

        # The gate is precise: a fresh snapshot of the same store still writes.
        mm.save_memory(mm.load_memory())
        assert bridge.load_memory()["identity"]["name"]["value"] == "Ada"
    finally:
        if second is not None:
            _stop(second)


def test_only_a_live_read_token_can_be_written_back(workspace, monkeypatch):
    bridge = _ProfileBridge({"identity": {"name": _profile_entry("Ada")}})
    _install(monkeypatch, bridge)

    snapshot = mm.load_memory()
    assert isinstance(snapshot, ub.ProfileSnapshot)
    mm.save_memory(snapshot)
    assert len(bridge.writes) == 1

    plain = dict(snapshot)
    assert ub.snapshot_state(plain)["ok"] is False
    with pytest.raises(ub.SaturnWriteError):
        mm.save_memory(plain)

    forged = dict(snapshot)
    forged["provenance"] = {"read_ok": True, "revision": "forged:1"}
    forged["_token"] = object()
    assert ub.snapshot_state(forged)["ok"] is False
    with pytest.raises(ub.SaturnWriteError):
        mm.save_memory(forged)

    round_tripped = json.loads(json.dumps(snapshot))
    assert ub.snapshot_state(round_tripped)["ok"] is False
    with pytest.raises(ub.SaturnWriteError):
        mm.save_memory(round_tripped)

    outage = ub.ProfileOutage("transport down")
    assert outage == mm._empty_memory()
    with pytest.raises(ub.SaturnWriteError) as excinfo:
        mm.save_memory(outage)
    assert "failed read" in str(excinfo.value)
    assert len(bridge.writes) == 1

    # A shallow copy is still the same evidence, and stays writable.
    assert ub.snapshot_state(snapshot.copy())["ok"] is True


# ── Fix 2: an export can never name an uncommitted revision ─────────────────

@requires_node
def test_real_sidecar_export_blocks_on_an_in_flight_write(workspace, monkeypatch):
    """The slow-body repro: an unread request body must not publish a revision."""
    data_file = Path(workspace) / "native" / "inflight.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    slow = None
    try:
        _connect(monkeypatch, port, "origin")
        status, before = _http(port, "GET", "/export?namespace=origin")
        assert status == 200
        revision_before = str(before["revision"])
        assert before["entries"] == []

        payload = json.dumps(
            {"text": "User preferred editor: Neovim", "kind": "note", "namespace": "origin"}
        ).encode("utf-8")
        head = (
            "POST /remember HTTP/1.1\r\n"
            "Host: 127.0.0.1\r\n"
            "content-type: application/json\r\n"
            f"content-length: {len(payload)}\r\n"
            f"authorization: Bearer {API_KEY}\r\n"
            "connection: close\r\n\r\n"
        ).encode("utf-8")
        slow = socket.create_connection(("127.0.0.1", port), timeout=15)
        slow.sendall(head)  # headers only: the body is deliberately withheld
        time.sleep(0.5)

        # The write owns the queue, so an export cannot observe a bumped
        # revision over the pre-write contents. It blocks instead.
        with pytest.raises((socket.timeout, TimeoutError)):
            _http(port, "GET", "/export?namespace=origin", timeout=1.0)

        slow.sendall(payload)
        slow.settimeout(20)
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = slow.recv(65536)
            if not chunk:
                break
            raw += chunk
        assert b"200" in raw.split(b"\r\n", 1)[0]

        deadline = time.time() + 20
        after = {}
        while time.time() < deadline:
            status, after = _http(port, "GET", "/export?namespace=origin")
            if status == 200 and after.get("entries"):
                break
            time.sleep(0.1)
        assert status == 200
        assert [entry["text"] for entry in after["entries"]] == ["User preferred editor: Neovim"]
        revision_after = str(after["revision"])
        epoch, counter = revision_before.split(":")
        assert revision_after.split(":")[0] == epoch
        assert int(revision_after.split(":")[1]) == int(counter) + 1
    finally:
        if slow is not None:
            slow.close()
        _stop(process)


# ── Fix 3: feedback is namespace-scoped ────────────────────────────────────

@requires_node
def test_real_sidecar_feedback_rejects_a_foreign_namespace_id(workspace, monkeypatch):
    data_file = Path(workspace) / "native" / "feedback.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        origin = _connect(monkeypatch, port, "origin")
        other = _connect(monkeypatch, port, "other")

        created = other.remember_entry(
            cu.memory_payload("User preferred editor: Helix", "preference", store_namespace="other")
        )
        entry_id = str(created["id"])
        assert entry_id

        assert origin.feedback_entry(entry_id) is False
        assert origin.get_entry(entry_id) is None
        assert other.feedback_entry(entry_id) is True

        status, body = _http(
            port, "POST", "/feedback", {"id": entry_id, "namespace": "origin"}
        )
        assert status == 404
        assert body["error"] == "NOT_FOUND"
        status, body = _http(port, "POST", "/feedback", {"id": entry_id, "namespace": "other"})
        assert status == 200
        assert body["ok"] is True
    finally:
        _stop(process)


# ── Fix 4: the engine transport cannot be redirected ───────────────────────

@requires_node
def test_engine_embedding_transport_requests_manual_redirects(workspace):
    script = Path(workspace) / "embed_redirect_check.js"
    script.write_text(_EMBED_REDIRECT_SCRIPT, encoding="utf-8")
    out_file = Path(workspace) / "native" / "embed.jsonl"
    result = subprocess.run(
        [NODE_BINARY, str(script), str(ENGINE_JS), str(out_file)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["result"] is None
    assert payload["redirectHits"] == 1
    assert payload["targetHits"] == 0, "the embedding request followed the redirect"
    assert payload["seen"], "the engine never issued a request"
    assert payload["seen"][0]["redirect"] == "manual"


_EMBED_REDIRECT_SCRIPT = r"""
'use strict';
const http = require('http');

const indexPath = process.argv[2];
const storeFile = process.argv[3];
const { UniversalMemory } = require(indexPath);

let targetHits = 0;
let redirectHits = 0;
const seen = [];

const target = http.createServer((req, res) => {
  targetHits += 1;
  res.writeHead(200, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ embeddings: [[1, 0, 0]] }));
});
const redirector = http.createServer((req, res) => {
  redirectHits += 1;
  res.writeHead(302, {
    location: `http://127.0.0.1:${target.address().port}/api/embed`,
    'content-length': 0,
  });
  res.end('');
});

const listen = (server) =>
  new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));

(async () => {
  await listen(target);
  const redirectPort = await listen(redirector);
  const realFetch = globalThis.fetch;
  const recordingFetch = (url, options) => {
    seen.push({ url: String(url), redirect: options && options.redirect });
    return realFetch(url, options);
  };
  const memory = new UniversalMemory({
    file: storeFile,
    namespace: 'origin',
    ollamaUrl: `http://127.0.0.1:${redirectPort}`,
    model: 'nomic-embed-text',
    fetch: recordingFetch,
  });
  const result = await memory._requestEmbed('/api/embed', {
    model: 'nomic-embed-text',
    input: ['hello'],
  });
  console.log(JSON.stringify({ result, targetHits, redirectHits, seen }));
  target.close();
  redirector.close();
})().catch((error) => {
  console.error(String(error && error.stack ? error.stack : error));
  process.exit(1);
});
"""


# ── Fix 5: a failed backup blocks the migration ────────────────────────────

def test_backup_failure_fails_migration_closed_with_a_live_wal_writer(workspace, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 200.0, now - 100.0, 2)],
    )

    # A real WAL writer with an uncommitted row: the state a byte copy would
    # capture as a torn page.
    writer = sqlite3.connect(str(db_path), timeout=5.0)
    writer.isolation_level = None
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "INSERT INTO memories (category, content, keywords, importance, created_at, "
        "last_accessed_at, access_count) VALUES ('fact','User prefers dark mode','kw',0.5,?,?,0)",
        (now, now),
    )
    try:
        def _fail_backup(source, destination):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(cu, "_snapshot_with_backup", _fail_backup)

        assert cu.write_rollback_copy(db_path) is None
        assert list(cu.rollback_dir(db_path).glob("*.db")) == []

        bridge = _CortexBridge("origin", Path(workspace) / "native" / "a.jsonl")
        cu.reset_migration_state()
        report = cu.ensure_cortex_migrated(bridge=bridge, source=db_path)
        assert report["ok"] is False
        assert "SQLite backup API" in report["error"]
        assert report["rollback"] is None
        assert bridge.imported_payloads == []
        assert bridge.entries == []
        assert cu.marker_path(db_path).exists() is False

        # The store's write paths are closed until the migration succeeds.
        monkeypatch.setattr(ub, "get_bridge", lambda: bridge)
        cu.reset_migration_state()
        store = cu.CortexNativeStore(db_path=db_path)
        assert store.remember("User name (identity): Ada", "identity") == -1
        assert store.forget(1234) is False
        store.clear_all_memories()
        assert store.harvest("hello", [("User preferred editor: Neovim", "preference", 0.9)]) == []
        store.record_habit("build starts", "run tests")
        assert bridge.remembered == []
        assert bridge.deleted == []
    finally:
        writer.rollback()
        writer.close()


# ── Fix 6: ids and caches follow the bridge, never the environment ──────────

def test_cortex_migration_ids_are_derived_from_the_bridge_namespace(workspace, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 200.0, now - 100.0, 2)],
        habits=[("build fails", "run the tests", 3, now - 50.0)],
    )
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", "gamma")  # must be ignored

    alpha = _CortexBridge("alpha", Path(workspace) / "native" / "a.jsonl")
    beta = _CortexBridge("beta", Path(workspace) / "native" / "b.jsonl")

    cu.reset_migration_state()
    first = cu.ensure_cortex_migrated(bridge=alpha, source=db_path)
    cu.reset_migration_state()
    second = cu.ensure_cortex_migrated(bridge=beta, source=db_path)

    assert first["imported"] == 2 and second["imported"] == 2
    ids_alpha = {payload["id"] for payload in alpha.imported_payloads}
    ids_beta = {payload["id"] for payload in beta.imported_payloads}
    assert len(ids_alpha) == 2 and len(ids_beta) == 2
    assert ids_alpha.isdisjoint(ids_beta), "one namespace's rows collided with the other's"
    assert {payload["namespace"] for payload in alpha.imported_payloads} == {"alpha"}
    assert {payload["namespace"] for payload in beta.imported_payloads} == {"beta"}
    assert len(alpha.entries) == 2 and len(beta.entries) == 2

    baseline = cu.migrated_memory_id("identity", "Ada", 1.0, 0, "alpha")
    assert baseline != cu.migrated_memory_id("identity", "Ada", 1.0, 0, "beta")
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", "delta")
    assert cu.migrated_memory_id("identity", "Ada", 1.0, 0, "alpha") == baseline


def test_cortex_cache_is_bound_to_the_bridge_destination(workspace, monkeypatch):
    alpha = _CortexBridge("alpha", Path(workspace) / "native" / "a.jsonl")
    beta = _CortexBridge("beta", Path(workspace) / "native" / "b.jsonl")
    alpha.entries = [alpha._row(cu.memory_payload("Alpha fact", "fact", store_namespace="alpha"))]
    beta.entries = [beta._row(cu.memory_payload("Beta fact", "fact", store_namespace="beta"))]

    store = cu.CortexNativeStore(cache_ttl=60.0)
    assert [entry["text"] for entry in store._entries(alpha)] == ["Alpha fact"]
    assert alpha.exports == 1

    # A different bridge (destination *and* namespace) must not read the cache
    # that was filled from alpha, even inside the TTL.
    assert [entry["text"] for entry in store._entries(beta)] == ["Beta fact"]
    assert beta.exports == 1

    assert [entry["text"] for entry in store._entries(alpha)] == ["Alpha fact"]
    assert alpha.exports == 2

    # A namespace change on the same store invalidates it as well.
    alpha.namespace = "moved"
    assert [entry["text"] for entry in store._entries(alpha)] == ["Alpha fact"]
    assert alpha.exports == 3


# ── Observations: _prepare propagation and destination verification ────────

def test_failed_migration_status_propagates_and_blocks_every_write(workspace, monkeypatch):
    bridge = _CortexBridge("origin", Path(workspace) / "native" / "a.jsonl")
    _install(monkeypatch, bridge)
    store = cu.CortexNativeStore(db_path=_cortex_db(workspace))

    monkeypatch.setattr(
        cu, "ensure_cortex_migrated", lambda **kwargs: {"ok": False, "error": "import failed"}
    )

    assert store._prepare(bridge)["error"] == "import failed"
    assert store.remember("User name (identity): Ada", "identity") == -1
    assert store.forget(4242) is False
    store.clear_all_memories()
    assert store.harvest("hello", [("User preferred editor: Neovim", "preference", 0.9)]) == []
    store.record_habit("build starts", "run tests")
    assert bridge.remembered == []
    assert bridge.deleted == []
    assert bridge.feedback == []

    # An explicitly disabled migration is not a blocker.
    monkeypatch.setattr(
        cu, "ensure_cortex_migrated", lambda **kwargs: {"ok": True, "skipped": "disabled_by_env"}
    )
    assert store.remember("User name (identity): Ada", "identity") != -1
    assert len(bridge.remembered) == 1

    # A crashed importer is a failed status, not an exception on the write path.
    def _boom(**kwargs):
        raise RuntimeError("importer exploded")

    monkeypatch.setattr(cu, "ensure_cortex_migrated", _boom)
    status = store._prepare(bridge)
    assert status["ok"] is False
    assert "importer exploded" in status["error"]
    assert store.remember("User name (identity): Ada", "identity") == -1


def test_destination_identity_is_verified_against_the_serving_diagnostics(workspace):
    configured = Path(workspace) / "native" / "configured.jsonl"
    served = Path(workspace) / "native" / "served.jsonl"

    class _Verified(_CortexBridge):
        def destination_identity(self, verify: bool = False, timeout=None) -> dict:
            return {
                "engine_file": str(served),
                "namespace": "served",
                "id": "ignored",
                "verified": True,
                "mismatch": f"the serving sidecar reports store {served}",
                "expected": {"engine_file": str(configured), "namespace": "origin"},
                "reported": {"engine_file": str(served), "namespace": "served"},
            }

    verified = cu._destination(_Verified("origin", configured))
    # The *store* follows the sidecar, so the configured file can never be
    # recorded as the destination it is not actually writing to.
    assert verified["engine_file"] == str(served)
    # The *namespace* is the bridge's: diagnostics reports the server's default,
    # which must never replace the namespace this bridge asked for.
    assert verified["namespace"] == "origin"
    assert verified["served_namespace"] == "served"
    assert verified["verified"] is True
    assert verified["mismatch"]
    assert verified["id"] == hashlib.sha1(f"{served}\x1forigin".encode("utf-8")).hexdigest()[:24]

    class _Unreachable(_CortexBridge):
        def destination_identity(self, verify: bool = False, timeout=None) -> dict:
            raise ub.SaturnUnavailable("sidecar down")

    fallback = cu._destination(_Unreachable("origin", configured))
    assert fallback["engine_file"] == str(configured)
    assert fallback["namespace"] == "origin"
    assert fallback["verified"] is False
    assert fallback["mismatch"] == ""

    # A double without the probe still produces a stable, namespace-bound id.
    plain = cu._destination(_CortexBridge("origin", configured))
    assert plain == {
        "engine_file": str(configured),
        "namespace": "origin",
        "id": hashlib.sha1(f"{configured}\x1forigin".encode("utf-8")).hexdigest()[:24],
        "verified": False,
        "mismatch": "",
        "served_namespace": None,
    }
    other = cu._destination(_CortexBridge("other", configured))
    assert other["id"] != plain["id"]


# ── Task 14: the remaining four blockers, against the real transports ───────

@requires_node
def test_real_engine_imports_each_bridge_namespace_independently(workspace, monkeypatch):
    """One engine, two bridges, and a process environment pinned to ``origin``.

    ``/diagnostics`` reports the sidecar's own default namespace. Adopting it
    would give the ``other`` bridge a ``origin`` destination, so both imports
    would land in one namespace under identical deterministic ids and only one
    row would survive.
    """
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    data_file = Path(workspace) / "native" / "namespaces.jsonl"
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 200.0, now - 100.0, 2)],
    )

    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        origin = _connect(monkeypatch, port, "origin")
        # The second bridge is the same connection asked for another namespace;
        # the process environment is deliberately left on ``origin``.
        other = ub.UniversalBridge()
        other.namespace = "other"
        assert os.environ["SATURN_MEMORY_NAMESPACE"] == "origin"
        assert other.namespace == "other"

        # The serving sidecar really does advertise ``origin``: this is exactly
        # the answer that must not be allowed to retarget the ``other`` import.
        served = other.destination_identity(verify=True)
        assert served["verified"] is True
        assert served["served_namespace"] == "origin"
        assert served["namespace"] == "other"

        cu.reset_migration_state()
        first = cu.ensure_cortex_migrated(bridge=origin, source=db_path)
        cu.reset_migration_state()
        second = cu.ensure_cortex_migrated(bridge=other, source=db_path)

        assert first["ok"] is True and first["imported"] == 1
        assert second["ok"] is True and second["imported"] == 1
        assert first["destination"]["namespace"] == "origin"
        assert second["destination"]["namespace"] == "other"

        rows_origin = origin.export_entries()["entries"]
        rows_other = other.export_entries()["entries"]
        assert len(rows_origin) == 1, rows_origin
        assert len(rows_other) == 1, rows_other
        assert {row["namespace"] for row in rows_origin} == {"origin"}
        assert {row["namespace"] for row in rows_other} == {"other"}
        assert rows_origin[0]["id"] != rows_other[0]["id"], "the two namespaces share an id"
        assert os.environ["SATURN_MEMORY_NAMESPACE"] == "origin"
    finally:
        _stop(process)


@requires_node
def test_profile_migration_never_marks_a_store_the_sidecar_does_not_serve(
    workspace, monkeypatch
):
    """Configured ``wanted.jsonl``, served ``served.jsonl``.

    The run must fail closed - no import, no marker claiming ``wanted`` - and
    once the configuration matches the store that really is intended, the
    (still empty) store must be imported into instead of answering
    ``already_imported``.
    """
    monkeypatch.setenv("SATURN_MEMORY_MODE", "full")
    served_file = Path(workspace) / "native" / "served.jsonl"
    wanted_file = Path(workspace) / "native" / "wanted.jsonl"
    source = mm.MEMORY_PATH
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps({"identity": {"name": _profile_entry("Ada")}}), encoding="utf-8"
    )

    port, process = _start_sidecar(served_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(wanted_file))
        bridge = _connect(monkeypatch, port, "origin")
        assert Path(bridge.file).resolve() == wanted_file.resolve()

        saturn_migrate.reset_migration_state()
        blocked = saturn_migrate.ensure_migrated(bridge=bridge, source=source)

        assert blocked["ok"] is False
        assert blocked["reason"] == "destination_mismatch"
        assert "serving sidecar reports store" in blocked["error"]
        assert Path(blocked["destination"]["engine_file"]).resolve() == served_file.resolve()
        assert Path(blocked["destination"]["configured_file"]).resolve() == wanted_file.resolve()
        # The namespace is still the bridge's, not the server's default.
        assert blocked["destination"]["namespace"] == "origin"
        # Nothing imported, nothing claimed, and the wanted store never touched.
        assert bridge.export_entries()["entries"] == []
        assert saturn_migrate.read_marker(source) == {}
        assert wanted_file.exists() is False

        # Point the process at the store the sidecar really serves: it is
        # empty, so this must import rather than report already_imported.
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(served_file))
        aligned = _connect(monkeypatch, port, "origin")
        saturn_migrate.reset_migration_state()
        imported = saturn_migrate.ensure_migrated(bridge=aligned, source=source)

        assert imported["ok"] is True, imported
        assert imported["reason"] != "already_imported"
        assert imported["imported"] == 1
        marker = saturn_migrate.read_marker(source)
        assert Path(marker["destination"]["engine_file"]).resolve() == served_file.resolve()
        rows = aligned.export_entries()["entries"]
        assert [row["meta"]["saturn_key"] for row in rows] == ["name"]
        assert wanted_file.exists() is False

        # A second run against the same aligned destination is a marker hit.
        saturn_migrate.reset_migration_state()
        again = saturn_migrate.ensure_migrated(bridge=aligned, source=source)
        assert again["reason"] == "already_imported"
    finally:
        _stop(process)


@requires_node
def test_two_sidecars_sharing_a_configured_file_never_share_cortex_rows(workspace, monkeypatch):
    """Same configured ``SATURN_MEMORY_FILE``, two endpoints, two stores.

    The cache scope must come from the store each sidecar reports it serves,
    so one endpoint can never answer with the other endpoint's rows.
    """
    shared_config = Path(workspace) / "native" / "shared-config.jsonl"
    alpha_file = Path(workspace) / "native" / "alpha.jsonl"
    beta_file = Path(workspace) / "native" / "beta.jsonl"

    port_a, process_a = _start_sidecar(alpha_file, "origin")
    port_b, process_b = _start_sidecar(beta_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(shared_config))
        alpha = _connect(monkeypatch, port_a, "origin")
        beta = _connect(monkeypatch, port_b, "origin")
        assert Path(alpha.file).resolve() == Path(beta.file).resolve()
        assert alpha.base_url != beta.base_url

        alpha.remember_entry(
            cu.memory_payload("Alpha fact", "fact", store_namespace="origin")
        )
        beta.remember_entry(
            cu.memory_payload("Beta fact", "fact", store_namespace="origin")
        )

        dest_alpha = cu._destination(alpha)
        dest_beta = cu._destination(beta)
        assert Path(dest_alpha["engine_file"]).resolve() == alpha_file.resolve()
        assert Path(dest_beta["engine_file"]).resolve() == beta_file.resolve()
        assert dest_alpha["id"] != dest_beta["id"]
        assert cu.CortexNativeStore._scope(alpha) != cu.CortexNativeStore._scope(beta)

        # Count exports so the cache behaviour is observable, not assumed.
        counts = {"alpha": 0, "beta": 0}
        for name, endpoint in (("alpha", alpha), ("beta", beta)):
            real = endpoint.export_entries

            def counted(*args, _name=name, _real=real, **kwargs):
                counts[_name] += 1
                return _real(*args, **kwargs)

            endpoint.export_entries = counted

        store = cu.CortexNativeStore(cache_ttl=60.0)
        assert [row["text"] for row in store._entries(alpha)] == ["Alpha fact"]
        assert [row["text"] for row in store._entries(beta)] == ["Beta fact"]
        assert [row["text"] for row in store._entries(alpha)] == ["Alpha fact"]
        assert counts == {"alpha": 2, "beta": 1}
    finally:
        _stop(process_a)
        _stop(process_b)


@requires_node
def test_public_snapshot_construction_cannot_mint_write_evidence(workspace, monkeypatch):
    """A forged snapshot must be unwritable and must not erase the store."""
    data_file = Path(workspace) / "native" / "forged.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        bridge = _connect(monkeypatch, port, "origin")
        _install(monkeypatch, bridge)
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Ada")}}, include_sessions=True
        )
        good = mm.load_memory()
        assert ub.snapshot_state(good)["ok"] is True

        shape = {"identity": {"name": _profile_entry("nobody")}}

        # The old shortcut: a perfectly shaped provenance dict on the public
        # constructor. It mints no evidence, and says so.
        forged = ub.ProfileSnapshot(
            shape,
            provenance={"read_ok": True, "revision": "9:9", "namespace": "origin"},
        )
        assert forged.writable is False
        assert forged.provenance["read_ok"] is False
        assert forged.provenance["issued"] is False
        state = ub.snapshot_state(forged)
        assert state["ok"] is False and state["issued"] is False
        with pytest.raises(ub.SaturnWriteError):
            mm.save_memory(forged)
        assert ub.snapshot_state(forged.copy())["ok"] is False

        # Nor does a bare construction, nor a hand-made token.
        plain = ub.ProfileSnapshot(shape)
        assert ub.snapshot_state(plain)["ok"] is False
        with pytest.raises(ub.SaturnWriteError):
            mm.save_memory(plain)
        with pytest.raises(TypeError):
            ub.ProfileSnapshot(shape, token=object())

        # The private factory is powerless without the module's own proof, and
        # the public promotion shortcut no longer exists.
        assert ub.snapshot_state(ub._verified_snapshot(shape))["ok"] is False
        assert not hasattr(ub, "issue_snapshot")

        # Nothing the forgeries attempted reached the store.
        assert bridge.load_memory()["identity"]["name"]["value"] == "Ada"
        assert ub.snapshot_state(good)["ok"] is True
    finally:
        _stop(process)


# ════════════════════════════════════════════════════════════════════════════
# Task 16: the five defects the final review found
# ════════════════════════════════════════════════════════════════════════════


# ── 1. A snapshot may only be written back to the store it came from ────────

@requires_node
def test_manager_refuses_a_snapshot_from_another_namespace(workspace, monkeypatch):
    """Namespace A's snapshot cannot alter namespace B, even at equal revisions.

    Both reads come from one sidecar, so both snapshots carry the *same*
    revision. A compare-and-swap on the revision alone accepts them
    interchangeably - which is exactly how one profile overwrote another.
    """
    data_file = Path(workspace) / "native" / "scope.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        origin = _connect(monkeypatch, port, "origin")
        other = _connect(monkeypatch, port, "other")

        origin.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )
        other.write_profile(
            {"identity": {"name": _profile_entry("Grace", "2026-01-01")}}, include_sessions=True
        )

        _install(monkeypatch, origin)
        mm.load_memory()  # establishes the read health the write path requires
        token_a = origin.load_memory()
        token_b = other.load_memory()
        state_a = ub.snapshot_state(token_a)
        state_b = ub.snapshot_state(token_b)
        assert state_a["ok"] is True and state_b["ok"] is True
        assert state_a["revision"] == state_b["revision"], "one store, one revision"
        assert state_a["namespace"] == "origin"
        assert state_b["namespace"] == "other"

        token_b["identity"] = {"name": _profile_entry("Mallory", "2026-02-02")}
        with pytest.raises(ub.SaturnSnapshotScopeError) as excinfo:
            mm.save_memory(token_b)
        message = str(excinfo.value)
        assert "refusing to overwrite" in message
        assert "namespace" in message

        # Neither namespace moved, and health stayed usable for the valid write.
        assert origin.load_memory()["identity"]["name"]["value"] == "Ada"
        assert other.load_memory()["identity"]["name"]["value"] == "Grace"
        mm.save_memory(token_a)
        assert origin.load_memory()["identity"]["name"]["value"] == "Ada"
    finally:
        _stop(process)


@requires_node
def test_bridge_refuses_a_snapshot_from_another_store_or_namespace(workspace, monkeypatch):
    """The boundary itself refuses a foreign snapshot before a byte is sent.

    No ``expectedRevision`` is passed: this is the direct caller that a
    revision-only check would serve without noticing the snapshot is foreign.
    """
    file_a = Path(workspace) / "native" / "store-a.jsonl"
    file_b = Path(workspace) / "native" / "store-b.jsonl"
    port_a, process_a = _start_sidecar(file_a, "origin")
    port_b, process_b = _start_sidecar(file_b, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(file_a))
        bridge_a = _connect(monkeypatch, port_a, "origin")
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(file_b))
        bridge_b = _connect(monkeypatch, port_b, "origin")
        bridge_a.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )
        bridge_b.write_profile(
            {"identity": {"name": _profile_entry("Grace", "2026-01-01")}}, include_sessions=True
        )

        foreign = bridge_b.load_memory()
        assert ub.snapshot_state(foreign)["ok"] is True
        with pytest.raises(ub.SaturnSnapshotScopeError) as excinfo:
            bridge_a.write_profile(foreign, include_sessions=True)
        assert "store" in str(excinfo.value)
        assert bridge_a.load_memory()["identity"]["name"]["value"] == "Ada"
        assert bridge_b.load_memory()["identity"]["name"]["value"] == "Grace"

        # Same store, different namespace: refused as well.
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(file_a))
        foreign_scope = _connect(monkeypatch, port_a, "other")
        foreign_ns = foreign_scope.load_memory()
        assert ub.snapshot_state(foreign_ns)["namespace"] == "other"
        with pytest.raises(ub.SaturnSnapshotScopeError) as excinfo:
            bridge_a.write_profile(foreign_ns, include_sessions=True)
        assert "namespace" in str(excinfo.value)
        assert bridge_a.load_memory()["identity"]["name"]["value"] == "Ada"
    finally:
        _stop(process_a)
        _stop(process_b)


# ── 2. Marker persistence is part of success ────────────────────────────────

@requires_node
def test_cortex_marker_failure_gates_writes_and_retries_without_duplicating(
    workspace, monkeypatch
):
    """A marker that did not reach disk is a failed import, not a silent one."""
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    db_path = _cortex_db(workspace)
    now = time.time()
    _make_legacy_db(
        db_path,
        memories=[("identity", "User name (identity): Ada", 0.9, now - 600.0, now - 60.0, 2)],
        habits=[("user says good morning", "reads the daily briefing", 3, now - 120.0)],
    )
    data_file = Path(workspace) / "native" / "cortex-marker.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        _install(monkeypatch, bridge)
        cu.reset_migration_state()

        original = cu._atomic_write

        def broken(path, payload):
            raise OSError("disk full")

        monkeypatch.setattr(cu, "_atomic_write", broken)
        failed = cu.migrate_cortex_db(source=db_path, bridge=bridge)
        assert failed["ok"] is False
        assert failed["reason"] == "marker_write_failed"
        assert "not recorded" in failed["error"]
        assert not cu.marker_path(db_path).exists()
        # No success is cached, and the write gate stays closed.
        assert cu.migration_allows_writes(failed) is False
        assert cu._migrated is None
        assert cu.ensure_cortex_migrated(bridge=bridge, source=db_path)["ok"] is False

        before = {entry["id"]: entry for entry in bridge.export_entries()["entries"]}
        assert len(before) == 2, "the rows themselves did land; only the marker is missing"

        # Retry once persistence works: nothing is duplicated and nothing the
        # store holds now is rolled back.
        monkeypatch.setattr(cu, "_atomic_write", original)
        retry = cu.migrate_cortex_db(source=db_path, bridge=bridge)
        assert retry["ok"] is True
        assert retry["imported"] == 0
        assert retry["skipped"] == 2
        assert cu.marker_path(db_path).exists()
        assert cu.migration_allows_writes(retry) is True
        after = {entry["id"]: entry for entry in bridge.export_entries()["entries"]}
        assert set(after) == set(before)
        for entry_id, entry in before.items():
            assert after[entry_id]["text"] == entry["text"]
            assert after[entry_id]["meta"] == entry["meta"]

        # The recorded migration is honoured across a restart.
        cu.reset_migration_state()
        assert cu.ensure_cortex_migrated(bridge=bridge, source=db_path)["reason"] == "already_imported"
    finally:
        _stop(process)


@requires_node
def test_json_marker_failure_is_not_cached_and_never_reverts_newer_values(
    workspace, monkeypatch
):
    """The JSON importer reports a failed marker and its retry keeps new writes."""
    data_file = Path(workspace) / "native" / "json-marker.jsonl"
    source = Path(workspace) / "memory" / "long_term.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        json.dumps({"identity": {"name": {"value": "Ada", "updated": "2026-01-01T00:00:00"}}}),
        encoding="utf-8",
    )
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        original = saturn_migrate._atomic_write

        def broken(path, payload):
            raise OSError("disk full")

        monkeypatch.setattr(saturn_migrate, "_atomic_write", broken)
        failed = saturn_migrate.import_from_json(source=source, bridge=bridge)
        assert failed["ok"] is False
        assert failed["reason"] == "marker_write_failed"
        assert "not recorded" in failed["error"]
        assert failed["imported"] == 1
        assert not saturn_migrate.marker_path(source).exists()

        # The store learns something newer while the marker is still missing.
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Ada Lovelace", "2026-03-03")}},
            include_sessions=True,
        )

        monkeypatch.setattr(saturn_migrate, "_atomic_write", original)
        retry = saturn_migrate.import_from_json(source=source, bridge=bridge)
        assert retry["ok"] is True, retry
        assert retry["imported"] == 0
        assert retry["updated"] == 0, "a retry must not roll the native store back"
        assert retry["skipped"] == 1
        assert saturn_migrate.marker_path(source).exists()
        assert bridge.load_memory()["identity"]["name"]["value"] == "Ada Lovelace"

        # And the recorded migration stays recorded.
        assert saturn_migrate.migration_status(source=source)["imported_at"]
    finally:
        _stop(process)


# ── 3. Live identities are lossless, labels stay readable ───────────────────

@requires_node
def test_real_sidecar_keeps_shared_prefix_facts_and_habits_distinct(workspace, monkeypatch):
    prefix = (
        "The user attended the international memory engineering conference in Kyoto and "
        "stayed for the evening sessions discussing retrieval"
    )
    assert len(prefix) > 60
    data_file = Path(workspace) / "native" / "lossless.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")

        bridge.remember_entry(cu.memory_payload(prefix + " on Monday", "fact"))
        bridge.remember_entry(cu.memory_payload(prefix + " on Tuesday", "fact"))
        facts = [e for e in bridge.export_entries()["entries"] if e["kind"] == "fact"]
        assert len(facts) == 2, "a shared 60-character prefix must not merge two facts"
        assert len({entry["meta"]["cortex_key"] for entry in facts}) == 2
        for entry in facts:
            key = entry["meta"]["cortex_key"]
            # Readable label + digest of the whole statement; the label alone is
            # never the identity, and the key stays bounded.
            assert key.startswith(cu._slug(entry["text"], cu.KEY_LABEL_CHARS))
            assert len(key) < 60
            assert entry["meta"]["cortex_label"] == cu._slug(entry["text"])
            assert entry["predicate"] == f"cortex.fact.{key}"
            assert len(entry["predicate"]) <= 80

        # Restating one fact still merges: lossless is not "unique per write".
        bridge.remember_entry(cu.memory_payload(prefix + " on Monday", "fact"))
        monday = [
            e for e in bridge.export_entries()["entries"] if e["text"].endswith("on Monday")
        ]
        assert len(monday) == 1 and monday[0]["seen"] == 2

        # Habits: same trigger counts up, a shared-prefix trigger stays separate.
        habit_a = prefix + " before coffee"
        habit_b = prefix + " after coffee"
        bridge.remember_entry(cu.habit_payload(habit_a, "reads the news"))
        bridge.remember_entry(cu.habit_payload(habit_a, "reads the news"))
        bridge.remember_entry(cu.habit_payload(habit_b, "opens the standup notes"))
        habits = [
            e for e in bridge.export_entries()["entries"]
            if (e.get("meta") or {}).get("cortex_record") == "habit"
        ]
        assert len(habits) == 2, "two triggers sharing a prefix are two habits"
        key_a, _ = cu.habit_identity(habit_a)
        key_b, _ = cu.habit_identity(habit_b)
        by_key = {entry["meta"]["cortex_key"]: entry for entry in habits}
        assert set(by_key) == {key_a, key_b}
        assert by_key[key_a]["seen"] == 2
        assert by_key[key_b]["seen"] == 1
        assert by_key[key_a]["meta"]["cortex_action"] == "reads the news"
        assert by_key[key_a]["meta"]["cortex_label"].startswith("the-user-attended-the-international")

        # A genuine mutable slot still supersedes: the predicate names the slot,
        # not the text, so a changed value replaces the old one.
        slot = "cortex.preference.favourite-colour"
        bridge.remember_entry(
            cu.memory_payload(
                "User favourite colour is teal", "preference", subject="user",
                predicate=slot, value="teal",
            )
        )
        bridge.remember_entry(
            cu.memory_payload(
                "User favourite colour is blue", "preference", subject="user",
                predicate=slot, value="blue",
            )
        )
        active = [e for e in bridge.export_entries()["entries"] if e.get("predicate") == slot]
        assert [entry["value"] for entry in active] == ["blue"]
    finally:
        _stop(process)


# ── 4. A large cortex import is batched, verified and only then recorded ────

@requires_node
def test_real_sidecar_batches_a_large_cortex_import_and_marks_it_only_when_complete(
    workspace, monkeypatch
):
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    db_path = _cortex_db(workspace)
    now = time.time()
    padding = "retrieval context " * 90
    rows = [
        (
            "fact",
            f"Fact number {index}: the user keeps a note about {padding} case {index}",
            0.6,
            now - 1000.0 + index,
            now - 500.0,
            index % 3,
        )
        for index in range(500)
    ]
    payload_bytes = sum(len(content.encode("utf-8")) for _c, content, *_rest in rows)
    assert payload_bytes > 600 * 1024, payload_bytes
    _make_legacy_db(db_path, memories=rows, habits=[])

    data_file = Path(workspace) / "native" / "batched.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")

        # Interrupt the third batch: a transport failure, not a fabric.
        real_request = bridge._request
        calls = {"import": 0}

        def flaky(method, path, payload=None, timeout=10.0):
            if method == "POST" and path == "/import":
                calls["import"] += 1
                if calls["import"] == 3:
                    raise OSError("connection reset by peer")
            return real_request(method, path, payload, timeout)

        monkeypatch.setattr(bridge, "_request", flaky)
        interrupted = cu.migrate_cortex_db(source=db_path, bridge=bridge)
        assert interrupted["ok"] is False
        assert "Saturn import failed" in interrupted["error"]
        assert not cu.marker_path(db_path).exists()
        partial = len(bridge.export_entries()["entries"])
        assert 0 < partial < 500, f"expected a partial import, got {partial}"

        # Retry: the batches are resent, the rows already there are skipped and
        # the marker is written only after the last batch accounts for its rows.
        monkeypatch.setattr(bridge, "_request", real_request)
        report = cu.migrate_cortex_db(source=db_path, bridge=bridge)
        assert report["ok"] is True, report
        assert report["batches"] >= 2
        assert report["imported"] + report["skipped"] == 500
        assert len(bridge.export_entries()["entries"]) == 500
        assert cu.marker_path(db_path).exists()

        cu.reset_migration_state()
        assert cu.ensure_cortex_migrated(bridge=bridge, source=db_path)["reason"] == "already_imported"
        assert len(bridge.export_entries()["entries"]) == 500
    finally:
        _stop(process)


# ── 5. A queued patch validates first and never takes the sidecar down ──────

@requires_node
def test_real_sidecar_patch_rejects_invalid_ttl_before_mutating(workspace):
    data_file = Path(workspace) / "native" / "patch-ttl.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        status, export = _http(port, "GET", "/export?namespace=origin")
        assert status == 200
        revision = export["revision"]

        for bad in (
            {"ttlDays": -1},
            {"ttlDays": 0},
            {"ttlDays": "soon"},
            {"ttlDays": 99999},
            {"ttl": -3},
        ):
            status, body = _http(
                port,
                "POST",
                "/patch",
                {
                    "expectedRevision": revision,
                    "namespace": "origin",
                    "remember": [{"text": "Should never be stored", **bad}],
                    "forget": [],
                },
            )
            assert status == 400, (bad, status, body)
            assert body["error"] == "INVALID_PATCH"
            assert body["revision"] == revision, "a refused patch must not move the revision"
            assert body["details"], body
            assert body["details"][0]["field"].startswith("remember[0].")

        # Every other input is validated the same way, still before mutation.
        for extra in (
            {"remember": [{"text": "   "}]},
            {"remember": [{"text": "x", "importance": 4}]},
            {"remember": [{"text": "x", "tags": "not-a-list"}]},
            {"remember": [{"text": "x"}], "forget": [""]},
            {"remember": "not-a-list"},
        ):
            status, body = _http(
                port,
                "POST",
                "/patch",
                {"expectedRevision": revision, "namespace": "origin", **extra},
            )
            assert status == 400, (extra, status, body)
            assert body["error"] == "INVALID_PATCH"

        status, after = _http(port, "GET", "/export?namespace=origin")
        assert after["entries"] == []
        assert after["revision"] == revision

        # An oversized body is answered on a live connection: the reader drains
        # the request instead of destroying the socket mid-upload.
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "x" * (300 * 1024)}],
                "forget": [],
            },
        )
        assert status == 413, body
        assert body["error"] == "BODY_TOO_LARGE"
        assert _http(port, "GET", "/diagnostics")[0] == 200
        assert _http(port, "GET", "/export?namespace=origin")[1]["entries"] == []

        # A valid TTL is accepted and really sets an expiry.
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Expiring note", "ttlDays": 1}],
                "forget": [],
            },
        )
        assert status == 200 and body["ok"] is True
        assert len(body["applied"]) == 1
        assert body["revision"] != revision
        entries = _http(port, "GET", "/export?namespace=origin")[1]["entries"]
        assert len(entries) == 1
        assert entries[0]["ttlDays"] == 1 and entries[0]["validTo"]
    finally:
        _stop(process)


@requires_node
def test_real_sidecar_queued_patch_failure_keeps_the_store_serving(workspace):
    """A rejecting queued task answers 500 and leaves the process serving."""
    data_file = Path(workspace) / "native" / "patch-queue.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    broken = None
    try:
        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Kept fact"}],
                "forget": [],
            },
        )
        assert status == 200 and body["ok"] is True

        # Make the store unwritable without touching the process: the queued
        # task now rejects in the middle of its own write.
        data_file.unlink()
        data_file.mkdir()
        broken = data_file

        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Cannot be written"}],
                "forget": [],
            },
        )
        assert status == 500, body
        assert body["error"] == "PARTIAL_WRITE"
        assert body["revision"] == revision, "nothing committed, so the revision cannot move"
        assert body["applied"] == [] and body["forgotten"] == []
        assert isinstance(body["message"], str) and body["message"]

        # The process survived and the socket was not reset: this same sidecar
        # answers a fresh request normally.
        status, diagnostics = _http(port, "GET", "/diagnostics")
        assert status == 200 and diagnostics["ok"] is True

        # Restore the store and prove the queue is still usable.
        broken.rmdir()
        broken = None
        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Written after recovery"}],
                "forget": [],
            },
        )
        assert status == 200 and body["ok"] is True
        texts = [entry["text"] for entry in _http(port, "GET", "/export?namespace=origin")[1]["entries"]]
        assert "Written after recovery" in texts
    finally:
        if broken is not None and broken.exists():
            try:
                broken.rmdir()
            except OSError:
                pass
        _stop(process)


# ════════════════════════════════════════════════════════════════════════════
# Task 19: the four defects the last review found
# ════════════════════════════════════════════════════════════════════════════


def _http_without_auth(port: int, method: str, path: str, body=None, timeout: float = 5.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"accept": "application/json"}
        if payload is not None:
            headers["content-type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, (json.loads(raw) if raw else {})
    finally:
        conn.close()


# ── 1. The write boundary is self-sufficient for direct callers ─────────────

@requires_node
def test_real_sidecar_bridge_refuses_an_outage_snapshot_after_recovery(workspace, monkeypatch):
    """An outage snapshot is unwritable even when the store is healthy again.

    ``memory_manager`` has always refused it; this calls ``write_profile``
    directly, which is the path an adapter or a script takes without the
    manager's checks.
    """
    data_file = Path(workspace) / "native" / "outage-direct.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    second_port = second_process = None
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )
        _stop(process)
        process = None

        outage = bridge.load_memory()
        assert isinstance(outage, ub.ProfileOutage)
        state = ub.snapshot_state(outage)
        assert state["ok"] is False and state["issued"] is True
        assert state["revision"] is None

        # The store comes back holding Ada. The outage must not turn into
        # "the user deleted everything" just because it is a dict.
        second_port, second_process = _start_sidecar(data_file, "origin")
        revived = _connect(monkeypatch, second_port, "origin")
        with pytest.raises(ub.SaturnSnapshotScopeError) as excinfo:
            revived.write_profile(outage, include_sessions=True)
        assert "failed read" in str(excinfo.value)
        assert revived.load_memory()["identity"]["name"]["value"] == "Ada"

        # A snapshot that is current for this store does write.
        current = revived.load_memory()
        current["notes"] = {"drink": _profile_entry("oolong", "2026-01-02")}
        revived.write_profile(current, include_sessions=True)
        profile = revived.load_memory()
        assert profile["identity"]["name"]["value"] == "Ada"
        assert profile["notes"]["drink"]["value"] == "oolong"
    finally:
        if process is not None:
            _stop(process)
        if second_process is not None:
            _stop(second_process)


@requires_node
def test_real_sidecar_bridge_derives_the_cas_revision_from_the_snapshot_token(
    workspace, monkeypatch
):
    """A direct write with no ``expectedRevision`` still compare-and-swaps.

    The token is the snapshot's own revision, so omitting the keyword cannot
    downgrade an authoritative write into a blind overwrite.
    """
    data_file = Path(workspace) / "native" / "cas-direct.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        monkeypatch.setenv("SATURN_MEMORY_FILE", str(data_file))
        bridge = _connect(monkeypatch, port, "origin")
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Ada", "2026-01-01")}}, include_sessions=True
        )
        stale = bridge.load_memory()
        assert ub.snapshot_state(stale)["ok"] is True

        # Somebody else commits while this snapshot is being edited.
        bridge.write_profile(
            {"identity": {"name": _profile_entry("Grace", "2026-02-01")}}, include_sessions=True
        )
        assert bridge.load_memory()["identity"]["name"]["value"] == "Grace"

        # Deleting everything through the stale snapshot would erase Grace.
        stale["identity"] = {}
        stale["notes"] = {"drink": _profile_entry("oolong")}
        with pytest.raises(ub.SaturnStaleSnapshot):
            bridge.write_profile(stale, include_sessions=True)
        profile = bridge.load_memory()
        assert profile["identity"]["name"]["value"] == "Grace"
        assert "drink" not in profile["notes"]

        # A hand-built snapshot has no evidence and is refused here, not only
        # inside the manager.
        forged = ub.ProfileSnapshot({"identity": {"name": _profile_entry("Mallory")}})
        with pytest.raises(ub.SaturnSnapshotScopeError):
            bridge.write_profile(forged, include_sessions=True)
        assert bridge.load_memory()["identity"]["name"]["value"] == "Grace"

        # The current snapshot writes, and only its own change lands.
        current = bridge.load_memory()
        current["notes"] = {"drink": _profile_entry("oolong", "2026-03-01")}
        bridge.write_profile(current, include_sessions=True)
        profile = bridge.load_memory()
        assert profile["identity"]["name"]["value"] == "Grace"
        assert profile["notes"]["drink"]["value"] == "oolong"
    finally:
        _stop(process)


# ── 2. An import is validated whole and never half-applies ──────────────────

@requires_node
def test_real_sidecar_import_prevalidates_the_whole_batch(workspace):
    """One invalid entry refuses the batch; a storage failure moves no revision."""
    data_file = Path(workspace) / "native" / "import-batch.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    broken = None
    try:
        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]

        # Entry 0 is perfectly valid. Entry 1 asks for a TTL the sidecar must
        # refuse, and the whole batch has to be refused with it - otherwise the
        # caller gets half an import and an error it cannot retry safely.
        status, body = _http(
            port,
            "POST",
            "/import",
            {
                "namespace": "origin",
                "entries": [
                    {"id": "row-ok", "text": "A perfectly valid row"},
                    {"id": "row-bad", "text": "Expires in a geological age", "ttlDays": 1e20},
                ],
            },
        )
        assert status == 400, body
        assert body["error"] == "INVALID_IMPORT"
        assert body["details"][0]["field"] == "entries[1].ttlDays"
        assert body["revision"] == revision, "a refused import must not move the revision"
        assert _http(port, "GET", "/export?namespace=origin")[1]["entries"] == []

        status, body = _http(
            port,
            "POST",
            "/import",
            {"namespace": "origin", "entries": [{"text": "fine"}, {"text": "   "}]},
        )
        assert status == 400 and body["error"] == "INVALID_IMPORT"
        assert body["details"][0]["field"] == "entries[1].text"
        assert _http(port, "GET", "/export?namespace=origin")[1]["entries"] == []

        # The valid contract still imports, and moves the revision because it
        # really wrote.
        status, body = _http(
            port,
            "POST",
            "/import",
            {
                "namespace": "origin",
                "entries": [
                    {"id": "keep-1", "text": "First imported row", "kind": "fact"},
                    {"id": "keep-2", "text": "Second imported row", "kind": "fact"},
                ],
            },
        )
        assert status == 200 and body["ok"] is True, body
        assert body["imported"] == 2
        assert body["revision"] != revision
        assert len(_http(port, "GET", "/export?namespace=origin")[1]["entries"]) == 2

        # A failure that committed nothing is reported, keeps the revision, and
        # leaves the sidecar serving.
        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]
        data_file.unlink()
        data_file.mkdir()
        broken = data_file
        status, body = _http(
            port,
            "POST",
            "/import",
            {"namespace": "origin", "entries": [{"text": "Cannot land"}]},
        )
        assert status == 500, body
        assert body["error"] == "IMPORT_FAILED"
        assert body["revision"] == revision, "nothing committed, so the revision cannot move"
        assert body["applied"] == 0
        assert isinstance(body["message"], str) and body["message"]
        assert _http(port, "GET", "/diagnostics")[0] == 200

        broken.rmdir()
        broken = None
        status, body = _http(
            port,
            "POST",
            "/import",
            {"namespace": "origin", "entries": [{"id": "keep-3", "text": "Lands after recovery"}]},
        )
        assert status == 200 and body["ok"] is True, body
        texts = [
            entry["text"] for entry in _http(port, "GET", "/export?namespace=origin")[1]["entries"]
        ]
        assert any("Lands after recovery" in text for text in texts)
    finally:
        if broken is not None and broken.exists():
            try:
                broken.rmdir()
            except OSError:
                pass
        _stop(process)


# The real engine only fails on I/O, and an I/O failure fails the first write
# as readily as the last, so a post-commit failure cannot be scheduled against
# it. This stub engine commits exactly one entry and then throws - the shape
# the route has to handle - and the *real* sidecar source runs against it.
_STUB_ENGINE = r"""
'use strict';
class UniversalMemory {
  constructor(options = {}) {
    this.file = options.file || 'stub.jsonl';
    this.namespace = options.namespace || 'origin';
    this.entries = [];
    this.lastMode = 'lexical';
    this.semantic = false;
    this.model = null;
    this.schemaVersion = 1;
  }
  stats() { return { total: this.entries.length }; }
  metrics() { return {}; }
  get(id) { return this.entries.find((entry) => entry.id === id) || null; }
  forget(id) {
    const before = this.entries.length;
    this.entries = this.entries.filter((entry) => entry.id !== id);
    return { ok: before !== this.entries.length, deleted: before - this.entries.length };
  }
  feedback() { return { ok: true }; }
  documents() { return []; }
  removeDocument() { return { deleted: 0 }; }
  export(options = {}) {
    const namespace = options.namespace || null;
    return {
      schemaVersion: this.schemaVersion,
      exportedAt: new Date().toISOString(),
      entries: this.entries.filter((entry) => !namespace || entry.namespace === namespace),
    };
  }
  async import(entries = []) {
    const first = entries[0];
    if (first && first.text !== 'abort-before-commit') {
      this.entries.push({ ...first, id: 'stub-' + (this.entries.length + 1) });
      throw new Error('storage died after committing one row');
    }
    throw new Error('storage died before committing');
  }
  close() {}
}
module.exports = { UniversalMemory };
"""

_STUB_SERVER = r"""
'use strict';
function createMemoryServer() {
  const handler = (request, response) => {
    response.writeHead(404, { 'content-type': 'application/json' });
    response.end('{"error":"NOT_FOUND"}');
  };
  return { listeners: () => [handler] };
}
module.exports = { createMemoryServer };
"""


@requires_node
def test_real_sidecar_import_partial_failure_advances_the_revision(workspace):
    """A throw after a commit must publish a revision that names the new state."""
    root = Path(workspace) / "fault-inject"
    (root / "memory").mkdir(parents=True)
    (root / "universal-memory" / "src").mkdir(parents=True)
    shutil.copyfile(SIDECAR_JS, root / "memory" / "sidecar.js")
    (root / "universal-memory" / "src" / "index.js").write_text(_STUB_ENGINE, encoding="utf-8")
    (root / "universal-memory" / "src" / "server.js").write_text(_STUB_SERVER, encoding="utf-8")
    # The sidecar wires the OCR hooks from a sibling module, so the stub tree
    # needs it to load at all. Copy the real one: the OCR pipeline is idle here
    # (nothing is ingested), and a stub would hide a renamed export.
    shutil.copyfile(OCR_JS, root / "universal-memory" / "src" / "ocr.js")
    port = _free_port()
    env = {
        **os.environ,
        "SATURN_MEMORY_PORT": str(port),
        "SATURN_MEMORY_HOST": "127.0.0.1",
        "SATURN_MEMORY_FILE": str(root / "stub.jsonl"),
        "SATURN_MEMORY_KEY": API_KEY,
        "SATURN_MEMORY_EMBED": "off",
        "SATURN_MEMORY_NAMESPACE": "origin",
    }
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    process = subprocess.Popen(
        [NODE_BINARY, str(root / "memory" / "sidecar.js")],
        cwd=str(root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )
    try:
        deadline = time.time() + 25
        ready = False
        while time.time() < deadline and not ready:
            try:
                ready = _http(port, "GET", "/diagnostics", timeout=0.5)[0] == 200
            except OSError:
                ready = False
            if not ready:
                time.sleep(0.15)
        assert ready, "the fault-injection sidecar did not start"

        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]
        status, body = _http(
            port,
            "POST",
            "/import",
            {"namespace": "origin", "entries": [{"text": "committed"}, {"text": "never written"}]},
        )
        assert status == 500, body
        assert body["error"] == "PARTIAL_IMPORT"
        assert body["applied"] == 1
        assert body["counts"] == {"applied": 1, "total": 2}
        assert body["revision"] != revision, "a partial commit moved the store"
        assert isinstance(body["message"], str) and body["message"]
        # The revision it published names what is really there.
        assert len(_http(port, "GET", "/export?namespace=origin")[1]["entries"]) == 1

        # A failure that committed nothing keeps the revision: no caller is
        # forced to re-read for a write that never happened.
        revision = body["revision"]
        status, body = _http(
            port,
            "POST",
            "/import",
            {"namespace": "origin", "entries": [{"text": "abort-before-commit"}]},
        )
        assert status == 500, body
        assert body["error"] == "IMPORT_FAILED"
        assert body["revision"] == revision
        assert body["applied"] == 0
        assert _http(port, "GET", "/diagnostics")[0] == 200
    finally:
        _stop(process)


# ── 3. A document can only be deleted from the namespace that owns it ───────

@requires_node
def test_real_sidecar_cross_namespace_document_delete_is_refused(workspace):
    """The engine's own DELETE /documents/:id ignores namespaces; the override must not."""
    data_file = Path(workspace) / "native" / "documents.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        status, other_doc = _http(
            port,
            "POST",
            "/documents",
            {
                "text": "Other store document that must survive this test.",
                "title": "Other",
                "namespace": "other",
            },
        )
        assert status == 200 and other_doc["chunks"] >= 1, other_doc
        other_id = other_doc["docId"]

        status, origin_doc = _http(
            port,
            "POST",
            "/documents",
            {"text": "Saturn document.", "title": "Saturn", "namespace": "origin"},
        )
        assert status == 200 and origin_doc["chunks"] >= 1, origin_doc
        origin_id = origin_doc["docId"]
        assert other_id != origin_id

        assert [
            doc["docId"] for doc in _http(port, "GET", "/documents?namespace=other")[1]["documents"]
        ] == [other_id]
        assert [
            doc["docId"] for doc in _http(port, "GET", "/documents?namespace=origin")[1]["documents"]
        ] == [origin_id]

        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]

        # Unauthenticated deletion never reaches the store.
        status, body = _http_without_auth(port, "DELETE", f"/documents/{other_id}?namespace=other")
        assert status == 401 and body["error"] == "UNAUTHORIZED"
        assert _http(port, "GET", "/documents?namespace=other")[1]["documents"]

        # Saturn cannot delete the neighbour's document, by query or by body.
        status, body = _http(port, "DELETE", f"/documents/{other_id}?namespace=origin")
        assert status == 404 and body["error"] == "NOT_FOUND"
        status, body = _http(port, "DELETE", f"/documents/{other_id}", {"namespace": "origin"})
        assert status == 404 and body["error"] == "NOT_FOUND"
        assert _http(port, "GET", "/export?namespace=other")[1]["entries"], "the foreign document survived"
        assert [
            doc["docId"] for doc in _http(port, "GET", "/documents?namespace=other")[1]["documents"]
        ] == [other_id]
        assert _http(port, "GET", "/export?namespace=origin")[1]["revision"] == revision

        # Saturn deletes its own document; the neighbour is untouched.
        status, body = _http(port, "DELETE", f"/documents/{origin_id}?namespace=origin")
        assert status == 200 and body["ok"] is True and body["deleted"] >= 1, body
        assert body["revision"] != revision
        assert _http(port, "GET", "/documents?namespace=origin")[1]["documents"] == []
        assert [
            doc["docId"] for doc in _http(port, "GET", "/documents?namespace=other")[1]["documents"]
        ] == [other_id]

        # And the owning namespace can delete its own through the body.
        status, body = _http(port, "DELETE", f"/documents/{other_id}", {"namespace": "other"})
        assert status == 200 and body["deleted"] >= 1, body
        assert _http(port, "GET", "/export?namespace=other")[1]["entries"] == []
    finally:
        _stop(process)


# ── 4. ``ttl`` is refused instead of silently becoming a permanent row ──────

@requires_node
def test_real_sidecar_rejects_the_ttl_alias_everywhere(workspace):
    """Only ``ttlDays`` is a TTL; the alias is an error, never a permanent row."""
    data_file = Path(workspace) / "native" / "ttl-alias.jsonl"
    port, process = _start_sidecar(data_file, "origin")
    try:
        revision = _http(port, "GET", "/export?namespace=origin")[1]["revision"]

        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Believed to expire", "ttl": 1}],
                "forget": [],
            },
        )
        assert status == 400, body
        assert body["error"] == "INVALID_PATCH"
        assert body["details"][0]["field"] == "remember[0].ttl"
        assert body["details"][0]["reason"] == "UNSUPPORTED_TTL_ALIAS"
        assert body["revision"] == revision
        assert _http(port, "GET", "/export?namespace=origin")[1]["entries"] == []

        status, body = _http(
            port,
            "POST",
            "/import",
            {
                "namespace": "origin",
                "entries": [{"id": "alias-row", "text": "Believed to expire on import", "ttl": 7}],
            },
        )
        assert status == 400, body
        assert body["error"] == "INVALID_IMPORT"
        assert body["details"][0]["reason"] == "UNSUPPORTED_TTL_ALIAS"
        export = _http(port, "GET", "/export?namespace=origin")[1]
        assert export["entries"] == [], "the alias must not leave a permanent row"
        assert export["revision"] == revision

        # ``ttlDays`` is the one accepted spelling and really sets an expiry.
        status, body = _http(
            port,
            "POST",
            "/patch",
            {
                "expectedRevision": revision,
                "namespace": "origin",
                "remember": [{"text": "Actually expiring", "ttlDays": 1}],
                "forget": [],
            },
        )
        assert status == 200 and body["ok"] is True, body
        entries = _http(port, "GET", "/export?namespace=origin")[1]["entries"]
        assert len(entries) == 1
        assert entries[0]["ttlDays"] == 1 and entries[0]["validTo"]
    finally:
        _stop(process)
