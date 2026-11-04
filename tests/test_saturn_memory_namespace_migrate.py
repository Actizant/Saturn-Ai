"""Namespace migration: opt-in, reversible, idempotent.

The tool is deliberately not imported by the application, so these tests are
the only thing that exercises it:

* a dry run reads both sides and writes nothing;
* an apply copies labelled rows, backs the store up, verifies and marks;
* a retry does not duplicate (marker short-circuit, then fingerprint skip);
* unlabelled and foreign rows are not copied;
* a shared owner or a pending outbox blocks the write;
* the bridge can request fresh ids, which is what makes the copy possible.
"""

from __future__ import annotations

import json
import shutil
import socket
from pathlib import Path

import pytest

import memory.namespace_migrate as nm
import memory.universal_bridge as ub

NODE_BINARY = shutil.which("node") or shutil.which("node.exe")
requires_node = pytest.mark.skipif(NODE_BINARY is None, reason="node is required")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _configure(monkeypatch, tmp_path: Path, namespace: str = "saturn-test"):
    port = _free_port()
    store = Path(tmp_path) / "store.jsonl"
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(store))
    monkeypatch.setenv("SATURN_MEMORY_PORT", str(port))
    monkeypatch.setenv("SATURN_MEMORY_HOST", "127.0.0.1")
    monkeypatch.setenv("SATURN_MEMORY_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("SATURN_MEMORY_NAMESPACE", namespace)
    monkeypatch.setenv("SATURN_MEMORY_EMBED", "off")
    monkeypatch.setenv("SATURN_MEMORY_AUTOSTART", "1")
    monkeypatch.setenv("SATURN_MEMORY_ENABLED", "1")
    monkeypatch.setenv("SATURN_MEMORY_OUTBOX", "1")
    monkeypatch.setenv("SATURN_MEMORY_TOKEN_FILE", str(Path(tmp_path) / "sidecar.token"))
    monkeypatch.setenv("SATURN_MEMORY_WRITE_TIMEOUT", "6")
    monkeypatch.setenv("SATURN_MEMORY_RECALL_TIMEOUT", "3")
    monkeypatch.delenv("SATURN_MEMORY_KEY", raising=False)
    monkeypatch.delenv("SATURN_MEMORY_KEY", raising=False)
    return port, store


def _bridge_for(monkeypatch, tmp_path: Path):
    _port, store = _configure(monkeypatch, tmp_path)
    bridge = ub.UniversalBridge()
    assert bridge.available(autostart=True), bridge._last_error
    return bridge, store


class _FakeBridge:
    """Enough of the bridge surface for the refusal and classification paths."""

    def __init__(self, rows, *, owns=True, pending=0, file=None):
        self.rows = {name: list(value) for name, value in rows.items()}
        self._owns_process = owns
        self._pending = pending
        self.file = str(file or Path("memory.jsonl"))
        self.namespace = "source"
        self.calls = []

    def available(self, autostart=True):
        return True

    def status(self, timeout=1.5, namespace=None):
        return {"ok": True, "file": self.file}

    def pending_writes(self):
        return self._pending

    def flush_outbox(self, timeout=None):
        return {"pending": self._pending}

    def list_entries(
        self,
        limit=200,
        offset=0,
        namespace=None,
        include_sessions=True,
        timeout=None,
        include_history=False,
    ):
        rows = self.rows.get(namespace, [])
        # The sidecar hides archived rows unless asked: mirror that, so a test
        # that forgets the flag fails here instead of in production.
        if not include_history:
            rows = [row for row in rows if not row.get("validTo")]
        return {"entries": rows[offset:offset + limit], "total": len(rows), "namespace": namespace}

    def import_entries(self, entries, timeout=None, preserve_ids=None):
        self.calls.append({"entries": list(entries), "preserve_ids": preserve_ids})
        dest = self.rows.setdefault("dest", [])
        for entry in entries:
            dest.append({**entry, "id": f"new_{len(dest)}", "namespace": "dest"})
        return {"ok": True, "imported": len(entries), "skipped": 0}

    def stop(self):
        pass


def _row(text: str, namespace: str = "source", **extra) -> dict:
    return {
        "id": f"m_{abs(hash(text))}",
        "text": text,
        "kind": "fact",
        "at": "2026-01-01T00:00:00.000Z",
        "namespace": namespace,
        "meta": {"saturn_category": "identity", "saturn_key": text, "saturn_value": text},
        **extra,
    }


# ── Pure helpers ─────────────────────────────────────────────────────────────

def test_clean_entry_keeps_only_importable_fields():
    entry = {
        "id": "m_1",
        "namespace": "source",
        "text": "Ada lives in Delhi",
        "kind": "fact",
        "tags": ["profile"],
        "at": "2026-01-01T00:00:00Z",
        "validFrom": "2026-01-01T00:00:00Z",
        "validTo": None,
        "importance": 0.7,
        "confidence": 0.9,
        "meta": {"saturn_category": "identity", "saturn_key": "city", "saturn_value": "Delhi"},
        "src": "origin",
        "seen": 3,
        "accessCount": 7,
        "lastAccessedAt": "2026-02-01T00:00:00Z",
        "supersedes": "m_0",
        "vec": "AAAA",
    }
    clean = nm._clean_entry(entry)
    assert clean is not None
    for stripped in ("id", "namespace", "seen", "accessCount", "lastAccessedAt", "supersedes", "vec"):
        assert stripped not in clean
    assert clean["meta"]["saturn_key"] == "city"
    assert clean["at"] == "2026-01-01T00:00:00Z"
    assert clean["text"] == "Ada lives in Delhi"


def test_clean_entry_rejects_empty_text():
    assert nm._clean_entry({"text": "   ", "namespace": "source"}) is None


def test_fingerprint_ignores_id_and_namespace_but_not_content():
    first = _row("Delhi", "source")
    same = {**first, "id": "other", "namespace": "dest"}
    assert nm._fingerprint(first) == nm._fingerprint(same)

    assert nm._fingerprint(first) != nm._fingerprint({**first, "text": "Mumbai"})
    assert nm._fingerprint(first) != nm._fingerprint({**first, "at": "2026-01-02T00:00:00.000Z"})
    changed_meta = {**first, "meta": {**first["meta"], "saturn_value": "Mumbai"}}
    assert nm._fingerprint(first) != nm._fingerprint(changed_meta)


# ── Classification and refusals (no node needed) ─────────────────────────────

def test_unlabelled_and_foreign_rows_are_not_copied(tmp_path):
    fake = _FakeBridge(
        {
            "source": [
                _row("labelled", "source"),
                _row("unlabelled", ""),
                _row("foreign", "other"),
            ],
            "dest": [],
        },
        file=Path(tmp_path) / "store.jsonl",
    )
    report = nm.migrate_namespace("source", "dest", bridge=fake, apply=False, close_bridge=False)
    assert report["ok"] is True, report
    assert report["dry_run"] is True
    assert report["source_labeled"] == 1
    assert report["unlabeled_skipped"] == 1
    assert report["to_import"] == 1
    assert fake.calls == []


def test_shared_owner_blocks_apply_unless_allowed(tmp_path):
    fake = _FakeBridge(
        {"source": [_row("labelled")], "dest": []},
        owns=False,
        file=Path(tmp_path) / "store.jsonl",
    )
    blocked = nm.migrate_namespace("source", "dest", bridge=fake, apply=True, close_bridge=False)
    assert blocked["ok"] is False
    assert "shared_store" in blocked["blockers"]
    assert fake.calls == []

    allowed = nm.migrate_namespace(
        "source", "dest", bridge=fake, apply=True, allow_shared=True, close_bridge=False
    )
    assert allowed["ok"] is True, allowed
    assert fake.calls and fake.calls[0]["preserve_ids"] is False
    assert allowed["verified"] is True


def test_pending_outbox_blocks_apply(tmp_path):
    fake = _FakeBridge(
        {"source": [_row("labelled")], "dest": []},
        pending=1,
        file=Path(tmp_path) / "store.jsonl",
    )
    report = nm.migrate_namespace("source", "dest", bridge=fake, apply=True, close_bridge=False)
    assert report["ok"] is False
    assert "pending_writes" in report["blockers"]
    assert fake.calls == []

    # A dry run reports the same blocker but still produces a report.
    dry = nm.migrate_namespace("source", "dest", bridge=fake, apply=False, close_bridge=False)
    assert dry["ok"] is True
    assert "pending_writes" in dry["blockers"]


def test_import_entries_can_request_fresh_ids(monkeypatch):
    bridge = ub.UniversalBridge()
    seen: dict = {}

    def fake_request(method, path, payload=None, timeout=10.0):
        seen["method"] = method
        seen["path"] = path
        seen["payload"] = dict(payload or {})
        return {"ok": True, "imported": 1, "skipped": 0}

    monkeypatch.setattr(bridge, "_request", fake_request)
    bridge.import_entries([{"text": "x"}], preserve_ids=False)
    assert seen["payload"]["preserveIds"] is False
    assert seen["payload"]["namespace"] == bridge.namespace

    seen.clear()
    bridge.import_entries([{"text": "x"}])
    assert "preserveIds" not in seen["payload"], "default must keep the engine's own behaviour"


def test_list_entries_exposes_history_only_on_request(monkeypatch):
    bridge = ub.UniversalBridge()
    seen: dict = {}

    def fake_request(method, path, payload=None, timeout=10.0):
        seen["path"] = path
        return {"entries": [], "total": 0}

    monkeypatch.setattr(bridge, "_request", fake_request)
    bridge.list_entries(namespace="origin")
    assert "includeHistory" not in seen["path"], "the default listing must stay active-only"

    bridge.list_entries(namespace="origin", include_history=True)
    assert "includeHistory=1" in seen["path"]


def test_expired_rows_are_counted_and_left_behind(tmp_path):
    expired = _row("old fact", validTo="2020-01-01T00:00:00.000Z")
    fake = _FakeBridge({"source": [_row("live fact"), expired]}, file=tmp_path / "memory.jsonl")

    report = nm.migrate_namespace("source", "dest", bridge=fake, apply=False, close_bridge=False)

    assert report["ok"] is True, report
    assert report["source_total"] == 2
    assert report["source_expired"] == 1
    assert report["source_labeled"] == 1
    assert report["to_import"] == 1
    assert fake.calls == [], "a dry run must not import anything"


def test_include_history_copies_expired_rows(tmp_path):
    expired = _row("old fact", validTo="2020-01-01T00:00:00.000Z")
    fake = _FakeBridge({"source": [_row("live fact"), expired]}, file=tmp_path / "memory.jsonl")

    applied = nm.migrate_namespace(
        "source", "dest", bridge=fake, apply=True, close_bridge=False, include_history=True
    )

    assert applied["ok"] is True, applied
    assert applied["verified"] is True
    assert applied["source_labeled"] == 2
    assert applied["imported"] == 2


# ── End to end against a real sidecar ────────────────────────────────────────

@requires_node
def test_dry_run_apply_and_retry(monkeypatch, tmp_path):
    bridge, store = _bridge_for(monkeypatch, tmp_path)
    try:
        profile = {
            "identity": {
                "name": {"value": "Ada", "updated": "2026-01-01"},
                "city": {"value": "Delhi", "updated": "2026-01-01"},
            }
        }
        bridge.write_profile(profile, autostart=True)
        source_rows = bridge.list_entries()["entries"]
        assert len(source_rows) == 2, source_rows
        source_ids = {row["id"] for row in source_rows}

        dry = nm.migrate_namespace("saturn-test", "saturn-dest", bridge=bridge, apply=False)
        assert dry["ok"] is True, dry
        assert dry["dry_run"] is True
        assert dry["source_labeled"] == 2
        assert dry["to_import"] == 2
        assert bridge.list_entries(namespace="saturn-dest")["entries"] == []
        assert not Path(dry["marker"]).exists()

        applied = nm.migrate_namespace("saturn-test", "saturn-dest", bridge=bridge, apply=True)
        assert applied["ok"] is True, applied
        assert applied["verified"] is True
        assert applied["imported"] == 2
        assert applied["destination_after"] == 2
        assert applied["missing_after"] == 0
        assert applied["backup"] and all(Path(path).exists() for path in applied["backup"])
        assert Path(applied["marker"]).exists()
        marker = json.loads(Path(applied["marker"]).read_text(encoding="utf-8"))
        assert marker["verified"] is True
        assert marker["source"] == "saturn-test" and marker["destination"] == "saturn-dest"

        dest_rows = bridge.list_entries(namespace="saturn-dest")["entries"]
        assert len(dest_rows) == 2
        assert all(row["namespace"] == "saturn-dest" for row in dest_rows)
        assert {row["id"] for row in dest_rows}.isdisjoint(source_ids)
        assert {row["text"] for row in dest_rows} == {row["text"] for row in source_rows}
        assert all(row["meta"]["saturn_category"] == "identity" for row in dest_rows)

        # The source stays intact: it is the rollback artifact.
        assert len(bridge.list_entries()["entries"]) == 2

        # A verified marker short-circuits a rerun.
        again = nm.migrate_namespace("saturn-test", "saturn-dest", bridge=bridge, apply=True)
        assert again["ok"] is True
        assert again["reason"] == "already_migrated"
        assert again["imported"] == 0
        assert again["already_present"] == 2
        assert len(bridge.list_entries(namespace="saturn-dest")["entries"]) == 2

        # Forcing a re-check falls back to the fingerprint skip, not duplicates.
        forced = nm.migrate_namespace(
            "saturn-test", "saturn-dest", bridge=bridge, apply=True, force=True
        )
        assert forced["ok"] is True, forced
        assert forced["already_present"] == 2
        assert forced["to_import"] == 0
        assert forced["reason"] == "nothing_to_do"
        assert len(bridge.list_entries(namespace="saturn-dest")["entries"]) == 2
        assert Path(store).exists()
    finally:
        bridge.stop()
