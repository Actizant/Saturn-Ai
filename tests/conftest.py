import sys
from pathlib import Path

import pytest

# Ensure tests can import the application package from the workspace root.
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    """Keep the persistent tool-audit trail out of the real user vault.

    Every routed tool call appends to %LOCALAPPDATA%-side logs; tests that
    exercise those seams must write per-test files instead, exactly like the
    voice/local-AI isolation rules already demand for config-backed code.
    """
    import core.audit_log as audit
    monkeypatch.setattr(audit, "AUDIT_FILE", tmp_path / "tool_audit.jsonl")
    monkeypatch.setattr(audit, "LOGS_DIR", tmp_path / "logs")


@pytest.fixture(autouse=True)
def _isolate_permissions_store(tmp_path, monkeypatch):
    """Same rule for the tool-permission overrides: a test that sets Block on
    a capability must not switch it off for the real user's Saturn."""
    import core.permissions as perms
    monkeypatch.setattr(perms, "STORE_FILE", tmp_path / "tool_permissions.json")


@pytest.fixture(autouse=True)
def _isolate_mcp_singleton():
    """Suites that exercise the MCP manager register fake servers directly on
    the live ``UniversalMCPManager`` singleton (`get_instance()._servers[...]
    = fake`). Without a restore, those fakes leak into later tests that read
    the same dict through the creators catalog (safety-state counts, live
    server maps) and fail only in full-suite ordering.

    We only snapshot when the singleton already exists so no test gains the
    side effect of instantiating it (the constructor writes the default
    mcp_servers.json). Fake instances injected by tests are never stopped -
    the fixture just puts the mapping back the way it found it.
    """
    from core.mcp_manager import UniversalMCPManager

    instance = UniversalMCPManager._instance
    before = dict(instance._servers) if instance is not None else None
    yield
    live = UniversalMCPManager._instance
    if live is None:
        return
    if before is None:
        # The singleton was created during this test; drop server entries it
        # added so suites that expect a pristine manager still get one.
        live._servers.clear()
        return
    if live._servers is not None:
        live._servers.clear()
        live._servers.update(before)
