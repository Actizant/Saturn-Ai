"""
Saturn - StudyGuard & Focus Sentinel (Smart Study Monitor).
Real-time AI study session supervisor using MediaPipe Face Mesh and OpenCV.
Tracks:
1. Drowsiness / Sleep (Prolonged Eye Closure via Eye Aspect Ratio - EAR)
2. Phone / Anti-Doomscroll Distraction (Hand & device near face/camera)
3. Desk Absence (Empty seat detection)
4. Focus Efficiency Score & Pomodoro Flow State analytics
5. Live HUD Deliverable Cards & Voice Interventions
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cv2
import numpy as np

try:
    import mediapipe as mp
    _MEDIAPIPE_AVAILABLE = True
except ImportError:
    mp = None
    _MEDIAPIPE_AVAILABLE = False

from core.user_paths import get_user_data_dir

logger = logging.getLogger("StudyGuard")

STUDY_DIR = get_user_data_dir() / "study_sessions"
STUDY_DIR.mkdir(parents=True, exist_ok=True)
HISTORY_FILE = STUDY_DIR / "study_history.json"


# MediaPipe FaceMesh Eye Landmark Indices
LEFT_EYE_LANDMARKS = [33, 160, 158, 133, 153, 144]
RIGHT_EYE_LANDMARKS = [362, 385, 387, 263, 373, 380]


@dataclass
class StudyGuardStats:
    session_id: str
    subject: str
    target_duration_min: int
    started_at_str: str
    ended_at_str: str = ""
    total_seconds: float = 0.0
    active_focus_seconds: float = 0.0
    distracted_seconds: float = 0.0
    absent_seconds: float = 0.0
    sleep_alerts: int = 0
    phone_alerts: int = 0
    absence_alerts: int = 0
    focus_score: float = 100.0
    is_completed: bool = False
    events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class StudyGuardManager:
    """Singleton supervisor for real-time study sessions."""

    _instance: Optional[StudyGuardManager] = None

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.is_active = False
        self.is_paused = False
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self.current_stats: Optional[StudyGuardStats] = None
        self._speak_cb: Optional[Callable[[str], None]] = None
        self._hud_update_cb: Optional[Callable[[str, int], None]] = None

        # Thresholds
        self.EAR_SLEEP_THRESHOLD = 0.19  # EAR below this = eye closed
        self.SLEEP_TRIGGER_SECONDS = 3.0  # Eyes closed for >= 3s triggers alarm
        self.PHONE_TRIGGER_SECONDS = 3.5  # Phone/hand raised for >= 3.5s triggers warning
        self.ABSENCE_TRIGGER_SECONDS = 12.0  # Empty desk for >= 12s triggers absence notice
        self.ALERT_COOLDOWN_SECONDS = 7.0  # Default min time between interventions
        self.SLEEP_COOLDOWN_SECONDS = 25.0
        self.PHONE_COOLDOWN_SECONDS = 20.0
        self.ABSENCE_COOLDOWN_SECONDS = 15.0

        self.consecutive_absence_alerts = 0
        self.auto_paused_due_to_absence = False
        self._last_hud_sec = -1
        self._last_alert_time = 0.0
        self._last_alert_times: Dict[str, float] = {}
        self._cap: Optional[cv2.VideoCapture] = None

    @classmethod
    def get_instance(cls) -> StudyGuardManager:
        if cls._instance is None:
            cls._instance = StudyGuardManager()
        return cls._instance

    @staticmethod
    def check_camera_available() -> Tuple[bool, int, str]:
        """Probes for a functional video capture device across indices 0 and 1 without hanging."""
        try:
            if hasattr(cv2, "utils") and hasattr(cv2.utils, "logging"):
                cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
        except Exception:
            pass

        for idx in (0, 1):
            try:
                backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
                cap = cv2.VideoCapture(idx, backend)
                if not cap.isOpened():
                    cap.release()
                    cap = cv2.VideoCapture(idx)

                if cap.isOpened():
                    ret, frame = cap.read()
                    cap.release()
                    if ret and frame is not None and frame.size > 0 and frame.std() >= 0.5:
                        return True, idx, f"Camera verified on device {idx}"
                else:
                    cap.release()
            except Exception:
                pass

        return False, -1, "No functional webcam detected"

    # =========================================================================
    # MATH & EYE ASPECT RATIO (EAR)
    # =========================================================================

    @staticmethod
    def _calculate_ear(landmarks: Any, eye_indices: List[int], w: int, h: int) -> float:
        """Computes Eye Aspect Ratio (EAR) for given landmarks."""
        try:
            pts = []
            for idx in eye_indices:
                lm = landmarks[idx]
                pts.append(np.array([lm.x * w, lm.y * h]))

            # Vertical distances: |p2 - p6|, |p3 - p5|
            v1 = np.linalg.norm(pts[1] - pts[5])
            v2 = np.linalg.norm(pts[2] - pts[4])

            # Horizontal distance: |p1 - p4|
            h_dist = np.linalg.norm(pts[0] - pts[3])

            if h_dist < 1e-6:
                return 0.3
            return float((v1 + v2) / (2.0 * h_dist))
        except Exception:
            return 0.3

    # =========================================================================
    # SESSION LIFECYCLE
    # =========================================================================

    def start_session(
        self,
        duration_minutes: int = 25,
        subject: str = "Focus Sprint",
        play_lofi: bool = False,
        timer_only: bool = False,
        speak_cb: Optional[Callable[[str], None]] = None,
        hud_update_cb: Optional[Callable[[str, int], None]] = None,
    ) -> Dict[str, Any]:
        """Launches the study session monitor (Webcam Vision or Pure Pomodoro Timer)."""
        with self._lock:
            if self.is_active:
                return {
                    "success": False,
                    "message": "A study session is already active.",
                    "stats": self.current_stats.to_dict() if self.current_stats else {},
                }

        # Camera pre-flight check if not explicitly running timer-only
        cam_available, cam_idx, cam_msg = False, -1, ""
        if not timer_only:
            cam_available, cam_idx, cam_msg = self.check_camera_available()
            if not cam_available:
                return {
                    "success": False,
                    "no_camera": True,
                    "message": (
                        "No webcam detected on your system. StudyGuard requires an active camera "
                        "for vision-based focus, drowsiness, and phone tracking. "
                        "If you want a timer without camera tracking, say 'start study timer'."
                    ),
                }

        with self._lock:
            self.is_active = True
            self.is_paused = False
            self.auto_paused_due_to_absence = False
            self.consecutive_absence_alerts = 0
            self._last_hud_sec = -1
            self._stop_event.clear()
            self._speak_cb = speak_cb
            self._hud_update_cb = hud_update_cb
            self._last_alert_time = 0.0
            self._last_alert_times = {}

            session_id = f"study_{int(time.time())}"
            self.current_stats = StudyGuardStats(
                session_id=session_id,
                subject=subject or "Focus Sprint",
                target_duration_min=max(1, duration_minutes),
                started_at_str=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

            if timer_only:
                target_loop = self._run_timer_only_loop
                loop_args = (play_lofi,)
                mode_desc = "Pomodoro focus timer active (camera-free)"
            else:
                target_loop = self._run_vision_loop
                loop_args = (play_lofi, cam_idx)
                mode_desc = "Webcam sentinel is monitoring"

            self._worker_thread = threading.Thread(
                target=target_loop,
                args=loop_args,
                daemon=True,
                name=f"StudyGuard-{session_id}"
            )
            self._worker_thread.start()

            logger.info(f"StudyGuard session started: {subject} ({duration_minutes}m) [{mode_desc}]")
            return {
                "success": True,
                "session_id": session_id,
                "subject": subject,
                "target_duration_min": duration_minutes,
                "timer_only": timer_only,
                "message": f"StudyGuard activated for {duration_minutes} minutes ({subject}). {mode_desc}.",
            }

    def pause_session(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_active:
                return {"success": False, "message": "No active study session to pause."}
            self.is_paused = True
            if self._speak_cb:
                self._speak_cb("Study session paused, sir.")
            return {"success": True, "message": "Study session paused."}

    def resume_session(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_active:
                return {"success": False, "message": "No active study session to resume."}
            self.is_paused = False
            if self._speak_cb:
                self._speak_cb("Resuming study session, sir. Let's stay focused.")
            return {"success": True, "message": "Study session resumed."}

    def stop_session(self) -> Dict[str, Any]:
        """Terminates session, computes final score, saves history and returns report."""
        with self._lock:
            if not self.is_active or not self.current_stats:
                return {"success": False, "message": "No active study session running."}

            self._stop_event.set()
            self.is_active = False
            self.is_paused = False
            self.auto_paused_due_to_absence = False
            self.consecutive_absence_alerts = 0

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

        with self._lock:
            stats = self.current_stats
            stats.ended_at_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            stats.is_completed = True

            total = max(1.0, stats.total_seconds)
            focus = stats.active_focus_seconds
            # Score formula: Focus percentage penalized slightly by distraction events
            raw_score = (focus / total) * 100.0
            penalty = (stats.sleep_alerts * 4.0) + (stats.phone_alerts * 3.0) + (stats.absence_alerts * 2.0)
            stats.focus_score = max(0.0, min(100.0, round(raw_score - penalty, 1)))

            self._save_history(stats)
            card_path = self._generate_hud_card(stats)

            summary_text = (
                f"Study session finished! Total time: {int(stats.total_seconds // 60)}m {int(stats.total_seconds % 60)}s. "
                f"Focus Efficiency: {stats.focus_score}%. "
                f"Alerts: {stats.sleep_alerts} sleep, {stats.phone_alerts} phone distractions."
            )
            return {
                "success": True,
                "stats": stats.to_dict(),
                "summary": summary_text,
                "card_path": card_path,
            }

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            if not self.is_active or not self.current_stats:
                return {"is_active": False, "message": "No study session in progress."}
            return {
                "is_active": True,
                "is_paused": self.is_paused,
                "stats": self.current_stats.to_dict(),
            }

    # =========================================================================
    # WORKER LOOPS (Pure Pomodoro Timer & Vision Sentinel)
    # =========================================================================

    def _run_timer_only_loop(self, play_lofi: bool) -> None:
        """Lightweight timer-only loop when no camera is present or requested."""
        logger.info("Initializing StudyGuard Pomodoro focus timer (camera-free mode)...")
        if play_lofi:
            try:
                import webbrowser
                webbrowser.open("https://www.youtube.com/watch?v=jfKfPfyJRdk")  # Lofi Girl
            except Exception:
                pass

        target_total_sec = self.current_stats.target_duration_min * 60.0
        last_tick = time.time()

        try:
            while not self._stop_event.is_set():
                now = time.time()
                dt = now - last_tick
                last_tick = now

                if self.is_paused:
                    time.sleep(0.5)
                    continue

                if self.current_stats and self.current_stats.total_seconds >= target_total_sec:
                    self._dispatch_voice_alert("Great job, sir! Your scheduled study sprint is complete. Excellent work.")
                    break

                self._update_stats_tick(dt=dt, is_focused=True, is_absent=False)
                time.sleep(0.5)
        except Exception as exc:
            logger.error(f"Error in StudyGuard timer loop: {exc}")
        finally:
            logger.info("StudyGuard timer loop finished.")

    def _run_vision_loop(self, play_lofi: bool, cam_index: int = 0) -> None:
        """Webcam processing loop running on dedicated worker thread."""
        logger.info(f"Initializing StudyGuard webcam vision on camera index {cam_index}...")

        if play_lofi:
            try:
                import webbrowser
                webbrowser.open("https://www.youtube.com/watch?v=jfKfPfyJRdk")  # Lofi Girl live
            except Exception:
                pass

        face_mesh = None
        hands = None
        if _MEDIAPIPE_AVAILABLE and mp is not None:
            try:
                face_mesh = mp.solutions.face_mesh.FaceMesh(
                    max_num_faces=1,
                    refine_landmarks=True,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5
                )
                hands = mp.solutions.hands.Hands(
                    max_num_hands=2,
                    min_detection_confidence=0.5,
                    min_tracking_confidence=0.5
                )
            except Exception as e:
                logger.warning(f"MediaPipe initialization error: {e}")

        # Camera capture
        backend = cv2.CAP_DSHOW if os.name == "nt" else cv2.CAP_ANY
        cap = cv2.VideoCapture(cam_index, backend)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(cam_index)

        if not cap.isOpened():
            logger.warning("Webcam failed to open inside vision loop; operating in fallback timer mode.")
            cap.release()
            cap = None

        eyes_closed_start = 0.0
        phone_raised_start = 0.0
        absence_start = 0.0
        last_tick = time.time()

        target_total_sec = self.current_stats.target_duration_min * 60.0

        try:
            while not self._stop_event.is_set():
                now = time.time()
                dt = now - last_tick
                last_tick = now

                if self.is_paused:
                    time.sleep(0.5)
                    # If auto-paused due to absence, periodically check if user has returned
                    if self.auto_paused_due_to_absence and cap and cap.isOpened():
                        ret_p, frame_p = cap.read()
                        if ret_p and frame_p is not None:
                            rgb_p = cv2.cvtColor(frame_p, cv2.COLOR_BGR2RGB)
                            if face_mesh:
                                res_p = face_mesh.process(rgb_p)
                                if res_p.multi_face_landmarks:
                                    self.is_paused = False
                                    self.auto_paused_due_to_absence = False
                                    self.consecutive_absence_alerts = 0
                                    self._dispatch_voice_alert("Welcome back, sir. Resuming study session.")
                    continue

                # Check if target duration reached
                if self.current_stats and self.current_stats.total_seconds >= target_total_sec:
                    self._dispatch_voice_alert("Great job, sir! Your scheduled study sprint is complete. Excellent work.")
                    break

                ret, frame = False, None
                if cap and cap.isOpened():
                    ret, frame = cap.read()

                if not ret or frame is None:
                    # Synthetic tick if camera is temporarily unavailable
                    self._update_stats_tick(dt=dt, is_focused=True, is_absent=False)
                    time.sleep(0.2)
                    continue

                h, w = frame.shape[:2]
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

                face_detected = False
                eyes_closed = False
                phone_or_hand_raised = False

                # 1. Face & Drowsiness Analysis
                if face_mesh:
                    results_mesh = face_mesh.process(rgb)
                    if results_mesh.multi_face_landmarks:
                        face_detected = True
                        lms = results_mesh.multi_face_landmarks[0].landmark

                        left_ear = self._calculate_ear(lms, LEFT_EYE_LANDMARKS, w, h)
                        right_ear = self._calculate_ear(lms, RIGHT_EYE_LANDMARKS, w, h)
                        avg_ear = (left_ear + right_ear) / 2.0

                        if avg_ear < self.EAR_SLEEP_THRESHOLD:
                            eyes_closed = True

                # 2. Hand / Phone Proximity Analysis
                if hands:
                    results_hands = hands.process(rgb)
                    if results_hands.multi_hand_landmarks:
                        # Check if hand is raised in upper 60% of frame (near face/screen)
                        for hand_lms in results_hands.multi_hand_landmarks:
                            wrist = hand_lms.landmark[0]
                            index_tip = hand_lms.landmark[8]
                            if wrist.y < 0.70 or index_tip.y < 0.65:
                                phone_or_hand_raised = True
                                break

                # Fallback simple face detector if MediaPipe failed
                if not face_mesh and not face_detected:
                    face_detected = True  # Graceful fallback assuming presence

                # --- 3. Drowsiness / Sleep Trigger ---
                if eyes_closed and face_detected:
                    if eyes_closed_start == 0.0:
                        eyes_closed_start = now
                    elif (now - eyes_closed_start) >= self.SLEEP_TRIGGER_SECONDS:
                        self._trigger_intervention("sleep", "Wake up, sir! Take a deep breath or grab some water.")
                        eyes_closed_start = now  # Reset for cooldown
                else:
                    eyes_closed_start = 0.0

                # --- 4. Phone Distraction Trigger ---
                if phone_or_hand_raised and face_detected:
                    if phone_raised_start == 0.0:
                        phone_raised_start = now
                    elif (now - phone_raised_start) >= self.PHONE_TRIGGER_SECONDS:
                        self._trigger_intervention("phone", "Sir, please put your phone down and stay focused on your work.")
                        phone_raised_start = now
                else:
                    phone_raised_start = 0.0

                # --- 5. Absence Trigger (Anti-Spam with Auto-Pause) ---
                if not face_detected:
                    if absence_start == 0.0:
                        absence_start = now
                    elif (now - absence_start) >= self.ABSENCE_TRIGGER_SECONDS:
                        if self.consecutive_absence_alerts == 0:
                            self._trigger_intervention("absence", "Sir, you've stepped away from your desk. Study session is still running.")
                            self.consecutive_absence_alerts += 1
                            absence_start = now
                        elif self.consecutive_absence_alerts == 1 and (now - absence_start) >= 20.0:
                            self._trigger_intervention("absence", "Sir, you are still away. Pausing study session until you return.")
                            self.consecutive_absence_alerts += 1
                            self.is_paused = True
                            self.auto_paused_due_to_absence = True
                            absence_start = now
                else:
                    absence_start = 0.0
                    if self.auto_paused_due_to_absence:
                        self.is_paused = False
                        self.auto_paused_due_to_absence = False
                        self.consecutive_absence_alerts = 0
                        self._dispatch_voice_alert("Welcome back, sir. Resuming study session.")
                    elif self.consecutive_absence_alerts > 0:
                        self.consecutive_absence_alerts = 0

                # Update running counters
                is_focused = face_detected and not eyes_closed and not phone_or_hand_raised
                self._update_stats_tick(dt=dt, is_focused=is_focused, is_absent=(not face_detected))

                # Sleep small fraction to keep CPU usage low
                time.sleep(0.08)

        except Exception as exc:
            logger.error(f"Error in StudyGuard vision loop: {exc}")
        finally:
            if cap:
                cap.release()
            if face_mesh:
                face_mesh.close()
            if hands:
                hands.close()
            logger.info("StudyGuard vision loop terminated.")

    def _update_stats_tick(self, dt: float, is_focused: bool, is_absent: bool) -> None:
        with self._lock:
            if not self.current_stats:
                return
            self.current_stats.total_seconds += dt
            if is_absent:
                self.current_stats.absent_seconds += dt
            elif is_focused:
                self.current_stats.active_focus_seconds += dt
            else:
                self.current_stats.distracted_seconds += dt

            # Update live score
            total = max(1.0, self.current_stats.total_seconds)
            focus = self.current_stats.active_focus_seconds
            raw_score = (focus / total) * 100.0
            penalty = (self.current_stats.sleep_alerts * 4.0) + (self.current_stats.phone_alerts * 3.0)
            self.current_stats.focus_score = max(0.0, min(100.0, round(raw_score - penalty, 1)))

            # HUD tick callback throttled to once per second
            cur_sec = int(self.current_stats.total_seconds)
            if self._hud_update_cb and cur_sec != self._last_hud_sec:
                self._last_hud_sec = cur_sec
                pct = int((self.current_stats.total_seconds / (self.current_stats.target_duration_min * 60.0)) * 100)
                rem_sec = max(0, int((self.current_stats.target_duration_min * 60.0) - self.current_stats.total_seconds))
                mins, secs = divmod(rem_sec, 60)
                status_str = f"Focus Score: {self.current_stats.focus_score}% | Remaining: {mins:02d}:{secs:02d}"
                self._hud_update_cb(status_str, min(100, pct))

    def _trigger_intervention(self, alert_type: str, voice_msg: str) -> None:
        # Suppress non-critical voice interventions while gaming
        try:
            from core.game_sentinel import get_game_sentinel_manager
            if get_game_sentinel_manager().is_game_mode_active:
                logger.debug("Suppressing StudyGuard alert because Game Mode is active.")
                return
        except Exception:
            pass

        now = time.time()
        cooldown = self.ALERT_COOLDOWN_SECONDS
        if alert_type == "sleep":
            cooldown = self.SLEEP_COOLDOWN_SECONDS
        elif alert_type == "phone":
            cooldown = self.PHONE_COOLDOWN_SECONDS
        elif alert_type == "absence":
            cooldown = self.ABSENCE_COOLDOWN_SECONDS

        last_type_time = self._last_alert_times.get(alert_type, 0.0)
        if (now - last_type_time) < cooldown:
            return

        self._last_alert_time = now
        self._last_alert_times[alert_type] = now
        with self._lock:
            if not self.current_stats:
                return
            if alert_type == "sleep":
                self.current_stats.sleep_alerts += 1
            elif alert_type == "phone":
                self.current_stats.phone_alerts += 1
            elif alert_type == "absence":
                self.current_stats.absence_alerts += 1

            self.current_stats.events.append({
                "time": datetime.now().strftime("%H:%M:%S"),
                "type": alert_type,
                "message": voice_msg,
            })

        self._dispatch_voice_alert(voice_msg)

    def _dispatch_voice_alert(self, msg: str) -> None:
        if self._speak_cb:
            try:
                self._speak_cb(msg)
            except Exception as e:
                logger.warning(f"Voice callback failed: {e}")

    # =========================================================================
    # PERSISTENCE & HUD DELIVERABLE CARDS
    # =========================================================================

    def _save_history(self, stats: StudyGuardStats) -> None:
        try:
            history = []
            if HISTORY_FILE.exists():
                with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
            history.append(stats.to_dict())
            with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save study history: {e}")

    def _generate_hud_card(self, stats: StudyGuardStats) -> str:
        """Renders an interactive cyberpunk study report card HTML."""
        total_m = int(stats.total_seconds // 60)
        total_s = int(stats.total_seconds % 60)
        focus_m = int(stats.active_focus_seconds // 60)
        focus_s = int(stats.active_focus_seconds % 60)

        card_path = STUDY_DIR / f"{stats.session_id}_report.html"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>StudyGuard Session Report</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ background: #0a0e17; color: #e2e8f0; padding: 24px; }}
  .container {{ max-width: 650px; margin: 0 auto; background: rgba(15, 23, 42, 0.85); border: 1px solid #1e293b; border-radius: 16px; padding: 28px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); backdrop-filter: blur(12px); }}
  .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #334155; padding-bottom: 16px; margin-bottom: 24px; }}
  .badge {{ background: #0ea5e9; color: #fff; font-size: 11px; font-weight: 700; text-transform: uppercase; padding: 4px 10px; border-radius: 20px; letter-spacing: 0.05em; }}
  .title {{ font-size: 24px; font-weight: 700; color: #f8fafc; }}
  .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 24px; }}
  .stat-card {{ background: rgba(30, 41, 59, 0.6); border: 1px solid #334155; border-radius: 12px; padding: 16px; text-align: center; }}
  .stat-val {{ font-size: 26px; font-weight: 800; color: #38bdf8; margin-top: 4px; }}
  .stat-label {{ font-size: 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.05em; }}
  .score-card {{ background: linear-gradient(135deg, rgba(14, 165, 233, 0.15), rgba(99, 102, 241, 0.15)); border: 1px solid #0ea5e9; border-radius: 12px; padding: 20px; text-align: center; margin-bottom: 24px; }}
  .score-val {{ font-size: 48px; font-weight: 900; color: #38bdf8; text-shadow: 0 0 20px rgba(56, 189, 248, 0.4); }}
  .events-box {{ background: rgba(30, 41, 59, 0.4); border-radius: 12px; padding: 16px; border: 1px solid #1e293b; }}
  .event-item {{ font-size: 13px; color: #cbd5e1; padding: 8px 0; border-bottom: 1px solid rgba(255,255,255,0.05); display: flex; justify-content: space-between; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <div class="title">🧠 StudyGuard Focus Report</div>
      <div style="font-size: 13px; color: #94a3b8; margin-top: 4px;">Subject: {stats.subject} • {stats.started_at_str}</div>
    </div>
    <div class="badge">Session Complete</div>
  </div>

  <div class="score-card">
    <div class="stat-label">Focus Efficiency Score</div>
    <div class="score-val">{stats.focus_score}%</div>
    <div style="font-size: 13px; color: #cbd5e1; margin-top: 6px;">Deep Flow State Maintained</div>
  </div>

  <div class="grid">
    <div class="stat-card">
      <div class="stat-label">Total Time</div>
      <div class="stat-val">{total_m}m {total_s}s</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Active Focus</div>
      <div class="stat-val">{focus_m}m {focus_s}s</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Alerts</div>
      <div class="stat-val" style="color: {'#ef4444' if (stats.sleep_alerts + stats.phone_alerts) > 0 else '#22c55e'};">{stats.sleep_alerts + stats.phone_alerts}</div>
    </div>
  </div>

  <div class="events-box">
    <div style="font-size: 13px; font-weight: 700; color: #94a3b8; margin-bottom: 8px; text-transform: uppercase;">Sentinel Interventions</div>
    <div class="event-item"><span>😴 Drowsiness / Sleep Warnings</span><span style="font-weight: 700;">{stats.sleep_alerts}</span></div>
    <div class="event-item"><span>📱 Phone / Doomscroll Alerts</span><span style="font-weight: 700;">{stats.phone_alerts}</span></div>
    <div class="event-item"><span>🙈 Desk Absence Events</span><span style="font-weight: 700;">{stats.absence_alerts}</span></div>
  </div>
</div>
</body>
</html>
"""
        try:
            with open(card_path, "w", encoding="utf-8") as f:
                f.write(html_content)
        except Exception as e:
            logger.error(f"Failed to write study HUD card: {e}")

        return str(card_path.resolve())


def get_study_guard_manager() -> StudyGuardManager:
    return StudyGuardManager.get_instance()
