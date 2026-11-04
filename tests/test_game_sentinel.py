"""
tests/test_game_sentinel.py
Unit tests for Game Sentinel, Mem Reduct RAM Cleaner, and Hardware Turbo Boost.
"""

from unittest.mock import MagicMock, patch
import pytest

from core.mem_reduct import MemReductEngine
from core.game_sentinel import GameSentinelManager, get_game_sentinel_manager
from core.intent_dispatcher import IntentDispatcher
from actions import game_sentinel as game_sentinel_action
from features import game_sentinel as game_sentinel_feature


def test_mem_reduct_engine_execution():
    """Verifies that MemReductEngine runs and returns valid statistics."""
    res = MemReductEngine.clean_ram(purge_standby=True, empty_working_sets=True)
    assert res["success"] is True
    assert "freed_mb" in res
    assert "before_percent" in res
    assert "after_percent" in res
    assert "trimmed_processes" in res
    assert isinstance(res["freed_mb"], float)
    assert res["total_ram_gb"] > 0


def test_game_sentinel_manager_lifecycle():
    """Verifies game mode activation and deactivation state machine."""
    manager = GameSentinelManager()
    manager.is_game_mode_active = False

    # 1. Activate Game Mode
    with patch.object(MemReductEngine, "clean_ram", return_value={"freed_mb": 512.0, "before_percent": 30.0, "after_percent": 24.0}):
        with patch.object(manager, "activate_turbo_power_plan", return_value="mock-guid"):
            res = manager.activate_game_mode(game_name="Valorant", pid=1234, manual=False, speak=False)
            assert res["success"] is True
            assert manager.is_game_mode_active is True
            assert manager.active_game_name == "Valorant"
            assert "Activating Game Mode" in res["message"]
            assert "Valorant" in res["message"]

            # 2. Check status
            st = manager.get_status()
            assert st["is_game_mode_active"] is True
            assert st["active_game_name"] == "Valorant"
            assert "ram_percent" in st

            # 3. Deactivate Game Mode
            with patch.object(manager, "set_power_scheme", return_value=True):
                deact_res = manager.deactivate_game_mode(manual=False, speak=False)
                assert deact_res["success"] is True
                assert manager.is_game_mode_active is False
                assert "Valorant session concluded" in deact_res["message"]


def test_game_sentinel_clean_ram_now():
    """Verifies manual Mem Reduct purge via GameSentinelManager."""
    manager = GameSentinelManager()
    with patch.object(MemReductEngine, "clean_ram", return_value={"freed_mb": 420.0, "after_percent": 18.0}):
        res = manager.clean_ram_now(speak=False)
        assert res["success"] is True
        assert "RAM purged successfully" in res["message"]
        assert "420" in res["message"]


def test_intent_dispatcher_game_sentinel_reflexes():
    """Verifies Tier 1 reflex routing for game mode and Mem Reduct commands."""
    dispatcher = IntentDispatcher()

    # 1. Start Game Mode & Turbo Boost
    r1 = dispatcher.match_instant_reflex("game mode")
    assert r1 is not None
    assert r1.intent == "game_sentinel"
    assert r1.action == "start"

    r2 = dispatcher.match_instant_reflex("turbo boost")
    assert r2 is not None
    assert r2.intent == "game_sentinel"
    assert r2.action == "start"

    r3 = dispatcher.match_instant_reflex("activate game mode")
    assert r3 is not None
    assert r3.intent == "game_sentinel"
    assert r3.action == "start"

    # 2. Stop Game Mode
    r4 = dispatcher.match_instant_reflex("stop game mode")
    assert r4 is not None
    assert r4.intent == "game_sentinel"
    assert r4.action == "stop"

    r5 = dispatcher.match_instant_reflex("game mode off")
    assert r5 is not None
    assert r5.intent == "game_sentinel"
    assert r5.action == "stop"

    # 3. Mem Reduct RAM Cleaner
    r6 = dispatcher.match_instant_reflex("clean ram")
    assert r6 is not None
    assert r6.intent == "game_sentinel"
    assert r6.action == "clean_ram"

    r7 = dispatcher.match_instant_reflex("purge ram")
    assert r7 is not None
    assert r7.intent == "game_sentinel"
    assert r7.action == "clean_ram"

    r8 = dispatcher.match_instant_reflex("mem reduct")
    assert r8 is not None
    assert r8.intent == "game_sentinel"
    assert r8.action == "clean_ram"

    r9 = dispatcher.match_instant_reflex("free memory")
    assert r9 is not None
    assert r9.intent == "game_sentinel"
    assert r9.action == "clean_ram"

    # 4. Telemetry / Status
    r10 = dispatcher.match_instant_reflex("game status")
    assert r10 is not None
    assert r10.intent == "game_sentinel"
    assert r10.action == "status"


def test_game_sentinel_action_and_feature():
    """Verifies action plugin and dynamic feature execution."""
    with patch("actions.game_sentinel.get_game_sentinel_manager") as mock_mgr_getter:
        mock_mgr = MagicMock()
        mock_mgr.activate_game_mode.return_value = {
            "success": True,
            "power_plan": "Ultimate Performance",
            "ram_cleaned": {"freed_mb": 600.0},
            "message": "Game Mode Activated"
        }
        mock_mgr.deactivate_game_mode.return_value = {"success": True, "message": "Disengaged"}
        mock_mgr.clean_ram_now.return_value = {
            "success": True,
            "stats": {"freed_mb": 350.0, "trimmed_processes": 45, "before_percent": 24.0, "after_percent": 19.0}
        }
        mock_mgr.get_status.return_value = {"is_game_mode_active": True, "active_game_name": "Cyberpunk 2077", "ram_percent": 20.0}
        mock_mgr_getter.return_value = mock_mgr

        # Action tests
        act_res = game_sentinel_action.run({"action": "start", "game_name": "Elden Ring"})
        assert "Activated" in act_res

        clean_res = game_sentinel_action.run({"action": "clean_ram"})
        assert "Mem Reduct RAM purge completed" in clean_res

        stop_res = game_sentinel_action.run({"action": "stop"})
        assert "Deactivated" in stop_res

        status_res = game_sentinel_action.run({"action": "status"})
        assert "Game Sentinel: ACTIVE" in status_res

    # Dynamic feature tests
    with patch("features.game_sentinel.get_game_sentinel_manager") as mock_feat_mgr:
        mock_feat = MagicMock()
        mock_feat.activate_game_mode.return_value = {"success": True, "message": "Engaged"}
        mock_feat.deactivate_game_mode.return_value = {"success": True, "message": "Disengaged"}
        mock_feat.clean_ram_now.return_value = {"success": True, "stats": {}, "message": "Purged"}
        mock_feat_mgr.return_value = mock_feat

        f_start = game_sentinel_feature.execute(action="start")
        assert f_start["success"] is True

        f_clean = game_sentinel_feature.execute(action="clean_ram")
        assert f_clean["success"] is True
