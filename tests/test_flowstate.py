from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.flowstate import AppSnapshot, FlowStateManager, FlowStateSnapshot
from core.intent_dispatcher import IntentDispatcher
from features import flowstate as flowstate_feature


def test_flowstate_models():
    app = AppSnapshot(
        name="code.exe",
        exe_path="C:\\Users\\Test\\Code.exe",
        title="VS Code",
        cwd="D:\\projects\\saturn"
    )
    d = app.to_dict()
    assert d["name"] == "code.exe"
    assert d["exe_path"] == "C:\\Users\\Test\\Code.exe"

    snap = FlowStateSnapshot(
        name="Test Snap",
        created_at=1700000000.0,
        created_str="2026-10-04 12:00:00",
        apps=[app],
        chrome_tabs=["https://github.com", "https://amazon.in"],
        vscode_workspaces=["D:\\projects\\saturn"]
    )
    snap_dict = snap.to_dict()
    assert snap_dict["name"] == "Test Snap"
    assert len(snap_dict["apps"]) == 1
    assert len(snap_dict["chrome_tabs"]) == 2


def test_flowstate_manager_lifecycle(tmp_path: Path):
    manager = FlowStateManager()
    manager.storage_file = tmp_path / "flowstate_test.json"
    manager._ensure_storage()

    # 1. Snapshot creation
    with patch.object(manager, "extract_chrome_tabs", return_value=["https://github.com/project"]):
        with patch.object(manager, "extract_edge_tabs", return_value=[]):
            with patch.object(manager, "extract_vscode_workspaces", return_value=["D:\\code\\demo"]):
                res = manager.snapshot_workspace("Alpha Workspace")
                assert res["success"] is True
                assert res["name"] == "Alpha Workspace"
                assert res["chrome_tabs_count"] == 1
                assert "D:\\code\\demo" in res["vscode_workspaces"]

    # 2. List workspaces
    ws_list = manager.list_workspaces()
    assert len(ws_list) == 1
    assert ws_list[0]["name"] == "Alpha Workspace"

    # 3. Restore workspace (mocking subprocess.Popen)
    with patch("subprocess.Popen") as mock_popen:
        restored = manager.restore_workspace("Alpha Workspace")
        assert restored["success"] is True
        assert restored["name"] == "Alpha Workspace"
        assert len(restored["restored_items"]) >= 1

    # 4. Restore non-existent workspace
    missing = manager.restore_workspace("Non Existent One")
    assert missing["success"] is False
    assert "not found" in missing["error"]

    # 5. Delete workspace
    ok = manager.delete_workspace("Alpha Workspace")
    assert ok is True
    assert len(manager.list_workspaces()) == 0


def test_intent_dispatcher_flowstate_reflexes():
    dispatcher = IntentDispatcher()

    # Snapshot variations
    r1 = dispatcher.match_instant_reflex("snapshot workspace E-commerce Engine")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "flowstate"
    assert r1.action == "snapshot"
    assert "commerce engine" in r1.parameters.get("name", "")

    r2 = dispatcher.match_instant_reflex("save restoration point as Dev Sprint")
    assert r2 is not None
    assert r2.tier == 1
    assert r2.intent == "flowstate"
    assert r2.action == "snapshot"
    assert r2.parameters.get("name") == "dev sprint"

    # Restore variations
    r3 = dispatcher.match_instant_reflex("restore workspace E-commerce Engine")
    assert r3 is not None
    assert r3.tier == 1
    assert r3.intent == "flowstate"
    assert r3.action == "restore"
    assert "commerce engine" in r3.parameters.get("name", "")

    r4 = dispatcher.match_instant_reflex("switch to workspace Dev Sprint")
    assert r4 is not None
    assert r4.tier == 1
    assert r4.intent == "flowstate"
    assert r4.action == "restore"
    assert r4.parameters.get("name") == "dev sprint"

    # Natural snapshot phrasing with filler words and wake word
    r6 = dispatcher.match_instant_reflex("saturn snapshot this current workspace as")
    assert r6 is not None
    assert r6.tier == 1
    assert r6.intent == "flowstate"
    assert r6.action == "snapshot"
    assert r6.parameters.get("name") == "Current Workspace"

    # Verify Dynamic Registry does NOT falsely match generic words
    from core.dynamic_registry import DynamicToolRegistry
    DynamicToolRegistry.initialize()
    assert DynamicToolRegistry.find_matching_skill("saturn snapshot this current workspace as") is None
    assert DynamicToolRegistry.find_matching_skill("check current stock prices") is None



def test_flowstate_dynamic_feature(tmp_path: Path):
    with patch("core.flowstate.get_flowstate_manager") as mock_mgr_getter:
        mock_mgr = MagicMock()
        mock_mgr.snapshot_workspace.return_value = {
            "success": True,
            "apps_count": 5,
            "chrome_tabs_count": 3,
            "edge_tabs_count": 0,
        }
        mock_mgr.list_workspaces.return_value = [
            {"name": "Workspace 1", "apps_count": 4, "tabs_count": 2, "workspaces_count": 1, "created_str": "Today"}
        ]
        mock_mgr_getter.return_value = mock_mgr

        # Test snapshot
        res_snap = flowstate_feature.execute(action="snapshot", name="Research Hub")
        assert res_snap["success"] is True
        assert res_snap["action"] == "snapshot"

        # Test list
        res_list = flowstate_feature.execute(action="list")
        assert res_list["success"] is True
        assert len(res_list["workspaces"]) == 1
