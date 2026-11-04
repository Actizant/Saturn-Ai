from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.intent_dispatcher import IntentDispatcher
from core.study_guard import StudyGuardManager, StudyGuardStats
from actions import study_guard as study_guard_action
from features import study_guard as study_guard_feature


def test_study_guard_stats_model():
    stats = StudyGuardStats(
        session_id="study_test",
        subject="Mathematics",
        target_duration_min=30,
        started_at_str="2026-10-04 12:00:00",
        total_seconds=1200.0,
        active_focus_seconds=1140.0,
        distracted_seconds=60.0,
        sleep_alerts=1,
        phone_alerts=1,
        absence_alerts=0,
    )
    d = stats.to_dict()
    assert d["session_id"] == "study_test"
    assert d["subject"] == "Mathematics"
    assert d["total_seconds"] == 1200.0
    assert d["sleep_alerts"] == 1


def test_study_guard_lifecycle(tmp_path: Path):
    manager = StudyGuardManager()
    manager.is_active = False

    with patch.object(manager, "check_camera_available", return_value=(True, 0, "Mock cam")):
        with patch.object(manager, "_run_vision_loop"):
            # 1. Start session
            start_res = manager.start_session(duration_minutes=20, subject="Calculus")
            assert start_res["success"] is True
            assert manager.is_active is True
            assert manager.current_stats is not None
            assert manager.current_stats.subject == "Calculus"

            # 2. Check status
            st = manager.get_status()
            assert st["is_active"] is True
            assert st["stats"]["target_duration_min"] == 20

            # 3. Pause & Resume
            p_res = manager.pause_session()
            assert p_res["success"] is True
            assert manager.is_paused is True

            r_res = manager.resume_session()
            assert r_res["success"] is True
            assert manager.is_paused is False

            # Simulate ticks
            manager.current_stats.total_seconds = 600.0
            manager.current_stats.active_focus_seconds = 570.0
            manager.current_stats.sleep_alerts = 0
            manager.current_stats.phone_alerts = 1

            # 4. Stop session
            with patch.object(manager, "_save_history"):
                with patch.object(manager, "_generate_hud_card", return_value=str(tmp_path / "card.html")):
                    stop_res = manager.stop_session()
                    assert stop_res["success"] is True
                    assert manager.is_active is False
                    assert "stats" in stop_res
                    # Score: (570/600)*100 = 95.0, penalty: 1 phone alert (3.0) -> 92.0
                    assert stop_res["stats"]["focus_score"] == 92.0


def test_study_guard_camera_detection_and_timer_mode():
    manager = StudyGuardManager()
    manager.is_active = False

    # 1. Start vision session when NO camera is present -> must fail gracefully
    with patch.object(manager, "check_camera_available", return_value=(False, -1, "No camera")):
        res = manager.start_session(duration_minutes=25, subject="Physics", timer_only=False)
        assert res["success"] is False
        assert res["no_camera"] is True
        assert "No webcam detected" in res["message"]
        assert manager.is_active is False

    # 2. Start timer-only session (no camera needed) -> must succeed
    with patch.object(manager, "_run_timer_only_loop"):
        res_timer = manager.start_session(duration_minutes=15, subject="Reading", timer_only=True)
        assert res_timer["success"] is True
        assert res_timer["timer_only"] is True
        assert manager.is_active is True
        manager.stop_session()
        assert manager.is_active is False


def test_ear_calculation():
    class DummyLandmark:
        def __init__(self, x, y):
            self.x = x
            self.y = y

    # Simulate wide open eye: corners at x=10 and x=50, top/bottom separated by 20
    landmarks = [
        DummyLandmark(0, 0),  # dummy index 0
    ]
    # indices: [33, 160, 158, 133, 153, 144]
    mock_lms = {
        33: DummyLandmark(0.1, 0.5),   # corner 1
        160: DummyLandmark(0.2, 0.4),  # top 1
        158: DummyLandmark(0.4, 0.4),  # top 2
        133: DummyLandmark(0.5, 0.5),  # corner 2
        153: DummyLandmark(0.4, 0.6),  # bottom 1
        144: DummyLandmark(0.2, 0.6),  # bottom 2
    }
    indices = [33, 160, 158, 133, 153, 144]
    ear = StudyGuardManager._calculate_ear(mock_lms, indices, 100, 100)
    assert ear > 0.25


def test_intent_dispatcher_study_guard_reflexes():
    dispatcher = IntentDispatcher()

    # Start session variations
    r1 = dispatcher.match_instant_reflex("start study session for 45 minutes")
    assert r1 is not None
    assert r1.tier == 1
    assert r1.intent == "study_guard"
    assert r1.action == "start"
    assert r1.parameters.get("duration") == 45

    r2 = dispatcher.match_instant_reflex("study for 30 minutes")
    assert r2 is not None
    assert r2.intent == "study_guard"
    assert r2.action == "start"
    assert r2.parameters.get("duration") == 30

    r3 = dispatcher.match_instant_reflex("start study mode")
    assert r3 is not None
    assert r3.intent == "study_guard"
    assert r3.action == "start"

    # Stop session variations
    r4 = dispatcher.match_instant_reflex("stop study session")
    assert r4 is not None
    assert r4.intent == "study_guard"
    assert r4.action == "stop"

    r5 = dispatcher.match_instant_reflex("end study mode")
    assert r5 is not None
    assert r5.intent == "study_guard"
    assert r5.action == "stop"

    r5b = dispatcher.match_instant_reflex("stop study")
    assert r5b is not None
    assert r5b.intent == "study_guard"
    assert r5b.action == "stop"

    r5c = dispatcher.match_instant_reflex("stop studying")
    assert r5c is not None
    assert r5c.intent == "study_guard"
    assert r5c.action == "stop"

    r5d = dispatcher.match_instant_reflex("cancel study")
    assert r5d is not None
    assert r5d.intent == "study_guard"
    assert r5d.action == "stop"

    # Timer only variation
    r_timer = dispatcher.match_instant_reflex("start study timer for 25 minutes")
    assert r_timer is not None
    assert r_timer.intent == "study_guard"
    assert r_timer.action == "start"
    assert r_timer.parameters.get("timer_only") is True

    # Active session override: "stop" when session is active must route to study_guard stop
    mock_active_mgr = MagicMock()
    mock_active_mgr.is_active = True
    with patch("core.study_guard.get_study_guard_manager", return_value=mock_active_mgr):
        r_active_stop = dispatcher.match_instant_reflex("stop")
        assert r_active_stop is not None
        assert r_active_stop.intent == "study_guard"
        assert r_active_stop.action == "stop"

        r_active_cancel = dispatcher.match_instant_reflex("cancel")
        assert r_active_cancel is not None
        assert r_active_cancel.intent == "study_guard"
        assert r_active_cancel.action == "stop"

    # Pause & Stats
    r6 = dispatcher.match_instant_reflex("pause study session")
    assert r6 is not None
    assert r6.intent == "study_guard"
    assert r6.action == "pause"

    r7 = dispatcher.match_instant_reflex("study stats")
    assert r7 is not None
    assert r7.intent == "study_guard"
    assert r7.action == "status"


def test_study_guard_action_and_feature():
    # Test action plugin wrapper
    with patch("actions.study_guard.get_study_guard_manager") as mock_mgr_getter:
        mock_mgr = MagicMock()
        mock_mgr.start_session.return_value = {"success": True}
        mock_mgr.stop_session.return_value = {
            "success": True,
            "stats": {"total_seconds": 1800, "focus_score": 98.0, "sleep_alerts": 0, "phone_alerts": 0}
        }
        mock_mgr.pause_session.return_value = {"success": True, "message": "Paused."}
        mock_mgr_getter.return_value = mock_mgr

        res_act = study_guard_action.run({"action": "start", "duration": 30, "subject": "Biology"})
        assert "activated" in res_act

        res_stop = study_guard_action.run({"action": "stop"})
        assert "finished" in res_stop

    # Test dynamic feature wrapper
    with patch("features.study_guard.get_study_guard_manager") as mock_feat_getter:
        mock_feat = MagicMock()
        mock_feat.start_session.return_value = {"success": True, "message": "Started."}
        mock_feat_getter.return_value = mock_feat

        res_f = study_guard_feature.execute(action="start", duration=25)
        assert res_f["success"] is True
        assert res_f["action"] == "start"
