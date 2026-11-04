"""Focused tests for the Stage One Saturn hybrid memory foundation.

These deliberately mock the Node sidecar. They verify Saturn's contract:
JSON authority, bounded mirroring, merged/deterministic recall, prompt
primacy, legacy isolation, and lexical-only embeddings with no probing.
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import memory.memory_manager as mm
import memory.universal_bridge as ub
from memory.saturn_mode import resolve_saturn_mode


class FakeBridge:
    def __init__(self, ready: bool = True, hits: list[dict] | None = None):
        self.ready = ready
        self.hits = list(hits or [])
        self.saved: list[dict] = []
        self.recall_queries: list[tuple[str, int]] = []
        self.save_error: Exception | None = None
        self.recall_error: Exception | None = None
        self.on_save = None

    def is_ready(self) -> bool:
        return self.ready

    def save_memory(self, memory: dict, timeout: float | None = None, autostart: bool = False) -> bool:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(json.loads(json.dumps(memory)))
        if self.on_save is not None:
            self.on_save(memory)
        return True

    def recall_entries(self, query: str, limit: int = 8, timeout: float | None = None) -> list[dict]:
        self.recall_queries.append((query, limit))
        if self.recall_error is not None:
            raise self.recall_error
        return list(self.hits)

    def diagnostics(self) -> dict:
        return {
            "enabled": True,
            "ready": self.ready,
            "shadow_write": "synced" if self.saved else "idle",
            "ollama": {"available": False, "reason": "unreachable"},
        }


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "memory" / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", path)
    monkeypatch.setenv("SATURN_MEMORY_MODE", "hybrid")
    return path


def _install_bridge(monkeypatch, bridge: FakeBridge) -> None:
    monkeypatch.setattr(ub, "get_bridge", lambda: bridge)


def _entry(value: str, updated: str = "2026-01-02") -> dict:
    return {"value": value, "updated": updated}


# ── Mode mapping ─────────────────────────────────────────────────────────────

def test_mode_mapping_legacy_hybrid_and_reserved(monkeypatch):
    monkeypatch.delenv("SATURN_MEMORY_MODE", raising=False)
    default = resolve_saturn_mode()
    assert default.mode == "hybrid"
    assert (default.dual_write, default.merged_recall, default.prompt_augment) == (True, True, True)
    assert default.reserved is False

    monkeypatch.setenv("SATURN_MEMORY_MODE", "legacy")
    legacy = resolve_saturn_mode()
    assert legacy.mode == "legacy"
    assert legacy.stage == 0
    assert (legacy.dual_write, legacy.merged_recall, legacy.prompt_augment) == (False, False, False)

    monkeypatch.setenv("SATURN_MEMORY_MODE", "FULL")
    full = resolve_saturn_mode()
    assert full.requested == "FULL"
    # Stage Two promoted ``full`` from reserved to implemented. It is the one
    # mode where the sidecar, not long_term.json, owns the durable data.
    assert full.mode == "full"
    assert full.reserved is False
    assert full.target_stage == 2
    assert full.authoritative_store == "saturn"
    assert full.saturn_authoritative is True
    assert full.warning == ""
    assert (full.dual_write, full.merged_recall, full.prompt_augment) == (False, False, False)
    assert (full.native_reads, full.native_writes, full.json_writes) == (True, True, False)
    assert full.migration is True

    # Stage Three promotes ``unified`` from reserved to implemented: the profile
    # helpers AND the cortex facade share one native store, and nothing in the
    # unified path writes long_term.json or runs a dual-write mirror.
    monkeypatch.setenv("SATURN_MEMORY_MODE", "unified")
    unified = resolve_saturn_mode()
    assert unified.mode == "unified"
    assert unified.reserved is False
    assert unified.stage == 3
    assert unified.target_stage == 3
    assert unified.authoritative_store == "saturn"
    assert unified.saturn_authoritative is True
    assert unified.cortex_native is True
    assert unified.warning == ""
    assert (unified.dual_write, unified.merged_recall, unified.prompt_augment) == (False, False, False)
    assert (unified.native_reads, unified.native_writes, unified.json_writes) == (True, True, False)
    assert unified.migration is True

    # An unknown value still degrades to the JSON-owned default rather than
    # promoting the sidecar by accident.
    monkeypatch.setenv("SATURN_MEMORY_MODE", "banana")
    unknown = resolve_saturn_mode()
    assert unknown.mode == "hybrid"
    assert unknown.dual_write is True
    assert unknown.cortex_native is False
    assert "Unknown" in unknown.warning


def test_payload_mapping_uses_stable_origin_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(tmp_path / "universal.jsonl"))
    bridge = ub.UniversalBridge()

    payload = bridge._payload_for(
        "preferences", "favorite_color", _entry("teal", "2026-01-02")
    )
    assert payload["text"] == "Preferences favorite color: teal"
    assert payload["kind"] == "preference"
    assert payload["src"] == "saturn"
    assert payload["at"] == "2026-01-02T12:00:00"
    assert payload["tags"] == ["saturn", "cat:preferences", "key:favorite_color"]
    assert payload["meta"] == {
        "saturn_category": "preferences",
        "saturn_key": "favorite_color",
        "saturn_value": "teal",
        "saturn_updated": "2026-01-02",
    }

    identity = bridge._payload_for("identity", "name", {"value": "Ada"})
    assert identity["kind"] == "fact"
    assert identity["subject"] == "user"
    assert identity["predicate"] == "name"
    assert len(identity["meta"]["saturn_updated"]) == 10  # YYYY-MM-DD fallback


def test_today_is_never_published_as_a_future_window(tmp_path, monkeypatch):
    """A date-only value resolves to noon, and noon today can still be ahead.

    The engine copies ``at`` into ``validFrom`` and hides any row whose window
    has not opened, so a payload dated today must not carry a future time or
    the day's memories would stay invisible until noon.
    """
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(tmp_path / "universal.jsonl"))
    bridge = ub.UniversalBridge()

    payload = bridge._payload_for("identity", "name", {"value": "Ada"})

    assert datetime.fromisoformat(payload["at"]) <= datetime.now()


# ── Dual write ───────────────────────────────────────────────────────────────

def test_hybrid_writes_local_first_then_mirrors(store, monkeypatch):
    bridge = FakeBridge()
    observed_at_mirror: list[dict] = []

    def check_local_first(_memory):
        observed_at_mirror.append(json.loads(store.read_text(encoding="utf-8")))

    bridge.on_save = check_local_first
    _install_bridge(monkeypatch, bridge)

    result = mm.update_memory({"preferences": {"favorite_color": _entry("teal")}})

    assert result["preferences"]["favorite_color"]["value"] == "teal"
    on_disk = json.loads(store.read_text(encoding="utf-8"))
    assert on_disk["preferences"]["favorite_color"]["value"] == "teal"
    assert observed_at_mirror, "mirror must run after the JSON write"
    assert observed_at_mirror[0]["preferences"]["favorite_color"]["value"] == "teal"
    assert bridge.saved, "hybrid must mirror durable updates"
    assert bridge.saved[-1]["preferences"]["favorite_color"]["value"] == "teal"

    rows = mm.all_entries_for_ui()
    assert rows and set(rows[0]) == {"category", "key", "value", "updated"}


def test_load_memory_is_local_and_never_touches_the_bridge(store, monkeypatch):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"identity": {"name": _entry("Ada")}, "sessions": []}),
        encoding="utf-8",
    )

    def explode():
        raise AssertionError("load_memory must not consult Saturn")

    monkeypatch.setattr(ub, "get_bridge", explode)
    loaded = mm.load_memory()

    assert loaded["identity"]["name"]["value"] == "Ada"
    assert "sessions" in loaded


def test_dual_write_failure_never_loses_json(store, monkeypatch):
    bridge = FakeBridge()
    bridge.save_error = RuntimeError("node timed out")
    _install_bridge(monkeypatch, bridge)

    result = mm.update_memory({"notes": {"coffee": _entry("flat white")}})

    assert result["notes"]["coffee"]["value"] == "flat white"
    assert json.loads(store.read_text(encoding="utf-8"))["notes"]["coffee"]["value"] == "flat white"


def test_legacy_mode_skips_all_saturn_paths(store, monkeypatch):
    monkeypatch.setenv("SATURN_MEMORY_MODE", "legacy")
    bridge = FakeBridge(hits=[{"category": "", "key": "", "value": "native only"}])
    _install_bridge(monkeypatch, bridge)

    mm.update_memory({"preferences": {"favorite_color": _entry("teal")}})
    assert bridge.saved == []

    output = mm.search_memory("favorite color")
    assert output.startswith("Stored facts matching 'favorite color':")
    assert "preferences/favorite color: teal" in output
    assert "native only" not in output
    assert bridge.recall_queries == []

    prompt = mm.format_memory_for_prompt(mm.load_memory())
    assert "SATURN NATIVE MEMORY" not in prompt
    assert bridge.recall_queries == []


# ── Merged recall and dedupe ─────────────────────────────────────────────────

def test_merged_recall_dedupes_deterministically_and_keeps_format(store, monkeypatch):
    bridge = FakeBridge(hits=[
        # Anchored duplicate of a local row with a stale mirrored value.
        {
            "category": "preferences",
            "key": "favorite_color",
            "value": "stale blue",
            "has_saturn_meta": True,
        },
        # Two identical unanchored native hits: one should survive.
        {"category": "", "key": "", "value": "Loves jazz on Sundays", "has_saturn_meta": False},
        {"category": "", "key": "", "value": "Loves jazz on Sundays", "has_saturn_meta": False},
        # Unanchored mirror of a local value that lexical recall did not match.
        {"category": "", "key": "", "value": "flat white", "has_saturn_meta": False},
    ])
    _install_bridge(monkeypatch, bridge)

    mm.update_memory({
        "preferences": {"favorite_color": _entry("teal")},
        "notes": {"coffee": _entry("flat white")},
    })

    first = mm.search_memory("favorite color")
    second = mm.search_memory("favorite color")

    assert first == second, "merged recall must be deterministic"
    assert first.startswith("Stored facts matching 'favorite color':")
    assert first.count("preferences/favorite color:") == 1
    assert "teal" in first
    assert "stale blue" not in first
    assert first.count("Loves jazz on Sundays") == 1
    # Mapped back to the local identity/value, not a duplicate native line.
    assert first.count("flat white") == 1
    assert "notes/coffee: flat white" in first
    assert "saturn/memory: flat white" not in first


def test_recall_bridge_failure_falls_back_to_lexical(store, monkeypatch):
    bridge = FakeBridge()
    bridge.recall_error = TimeoutError("sidecar timeout")
    _install_bridge(monkeypatch, bridge)
    mm.update_memory({"preferences": {"favorite_color": _entry("teal")}})

    output = mm.search_memory("favorite color")

    assert "preferences/favorite color: teal" in output
    assert output.startswith("Stored facts matching 'favorite color':")


# ── Prompt primacy ───────────────────────────────────────────────────────────

def test_prompt_is_primary_and_native_suffix_is_bounded(store, monkeypatch):
    bridge = FakeBridge(hits=[
        {"category": "preferences", "key": "favorite_color", "value": "teal", "has_saturn_meta": True},
        {"category": "", "key": "", "value": "teal", "has_saturn_meta": False},
        {"category": "notes", "key": "jazz", "value": "Loves jazz on Sundays", "has_saturn_meta": False},
    ] + [
        {"category": "notes", "key": f"extra_{i}", "value": f"Native extra fact number {i}"}
        for i in range(8)
    ])
    _install_bridge(monkeypatch, bridge)
    mm.update_memory({
        "identity": {"name": _entry("Ada")},
        "preferences": {"favorite_color": _entry("teal")},
    })

    prompt = mm.format_memory_for_prompt(mm.load_memory())

    assert prompt.startswith("[WHAT YOU KNOW ABOUT THIS PERSON")
    assert "Name: Ada" in prompt
    assert "SATURN NATIVE MEMORY" in prompt
    assert prompt.count("teal") == 1
    assert "Loves jazz on Sundays" in prompt
    native_block = prompt.split("SATURN NATIVE MEMORY", 1)[1]
    native_lines = [line for line in native_block.splitlines() if line.startswith("  - ")]
    assert 1 <= len(native_lines) <= mm.SATURN_PROMPT_MAX_LINES
    assert len(native_block) <= mm.SATURN_PROMPT_MAX_CHARS + 160


def test_prompt_native_suffix_is_absent_when_bridge_fails_or_is_not_ready(store, monkeypatch):
    mm.update_memory({"identity": {"name": _entry("Ada")}})
    baseline = mm.format_memory_for_prompt(mm.load_memory())

    failing = FakeBridge()
    failing.recall_error = RuntimeError("sidecar died")
    _install_bridge(monkeypatch, failing)
    assert mm.format_memory_for_prompt(mm.load_memory()) == baseline

    cold = FakeBridge(ready=False, hits=[{"category": "", "key": "", "value": "should not appear"}])
    _install_bridge(monkeypatch, cold)
    assert mm.format_memory_for_prompt(mm.load_memory()) == baseline
    assert cold.recall_queries == []


# ── Embeddings and diagnostics ───────────────────────────────────────────────

def test_embeddings_are_lexical_only_and_never_probed(tmp_path, monkeypatch):
    """The Ollama probe is gone: no embed/Ollama env can make the bridge call
    any process other than its own sidecar, and the sidecar is pinned lexical."""
    monkeypatch.setenv("SATURN_MEMORY_FILE", str(tmp_path / "universal.jsonl"))
    monkeypatch.setenv("SATURN_MEMORY_EMBED", "ollama")
    monkeypatch.setenv("SATURN_MEMORY_OLLAMA", "http://127.0.0.1:11434")
    monkeypatch.setenv("SATURN_MEMORY_MODEL", "nomic-embed-text")

    opened: list = []

    class _NeverConnection:
        def __init__(self, *args, **kwargs):
            opened.append(args)

    monkeypatch.setattr(ub.http.client, "HTTPConnection", _NeverConnection)
    bridge = ub.UniversalBridge()

    status = bridge.ollama_status(force=True)
    assert status["available"] is False
    assert status["reason"] == "disabled"
    assert opened == []

    env = bridge._sidecar_env()
    assert env["SATURN_MEMORY_EMBED"] == "lexical"
    assert int(env["SATURN_MEMORY_EMBED_TIMEOUT_MS"]) > 0
    assert bridge.is_ready() is False

    diagnostics = bridge.diagnostics()
    assert diagnostics["ready"] is False
    assert diagnostics["ollama"]["available"] is False
    assert "api_key" not in diagnostics


def test_memory_diagnostics_reports_mode_and_local_authority(store, monkeypatch):
    bridge = FakeBridge()
    _install_bridge(monkeypatch, bridge)
    mm.update_memory({"identity": {"name": _entry("Ada")}})

    diagnostics = mm.memory_diagnostics()

    assert diagnostics["saturn"]["mode"] == "hybrid"
    assert diagnostics["authoritative_store"] == "long_term.json"
    assert diagnostics["entries"] == 1
    assert diagnostics["bridge"]["enabled"] is True
