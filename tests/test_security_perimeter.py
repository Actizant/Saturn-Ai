"""The perimeter's policy. Every test here is about something it must NOT do.

A lockout is the only irreversible action in this subsystem, so the tests are
weighted towards the conditions that have to stop it: one reading, a walk-past,
a blind camera, a missing model, an active bypass, a cooldown, a dry run.
"""

from __future__ import annotations

import pytest

from core import passphrase_vault, security_alerts, security_perimeter
from core.presence_watch import (
    ABSENT, FAULT, LOCKED, PRESENT_KNOWN, PRESENT_OWNER, PRESENT_UNVERIFIED,
)
from core.security_perimeter import SecurityPerimeter

BASE = {
    "enabled": True,
    "armed": True,
    "dry_run": False,
    "away_lock": "off",
    "away_seconds": 300.0,
    "grace_minutes": 10.0,
    "lock_cooldown_seconds": 600.0,
    "evidence_seconds": 0.0,
    "owner_name": "Ada",
    "sample_seconds": 3.0,
}


class FakeHub:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy

    def health(self) -> dict:
        return {"ok": self.healthy, "reason": "ok" if self.healthy else "NO_FRAME"}

    def probe(self, reset: bool = False) -> dict:
        return {"healthy": self.healthy, "reason": "ok" if self.healthy else "NO_FRAME"}


class Spies:
    def __init__(self):
        self.locks = 0
        self.evidence = 0
        self.announcements = []
        self.answers = []

    def lock(self) -> bool:
        self.locks += 1
        return True

    def capture(self, sample, seconds):
        self.evidence += 1
        return {"photo": "/tmp/evidence.jpg", "video": None}

    def announce(self, kind, payload=None):
        self.announcements.append(kind)

    def ask(self, reason) -> bool:
        return bool(self.answers.pop(0)) if self.answers else False

    def kinds(self) -> list:
        return list(self.announcements)


def make_perimeter(clock, spies=None, enrolled=("Ada",), **overrides):
    spies = spies or Spies()
    config = dict(BASE)
    config.update(overrides)
    hub = FakeHub(healthy=config.pop("hub_healthy", True))
    perimeter = SecurityPerimeter(
        watcher=None,
        hub=hub,
        config=config,
        lock=spies.lock,
        evidence=spies.capture,
        announce=spies.announce,
        ask_passphrase=spies.ask,
        clock=lambda: clock["t"],
    )
    perimeter._enrolled = lambda: list(enrolled)
    return perimeter, spies


def sample(state, arrival=False, healthy=True, identity_available=True,
           skipped=False, identity=None):
    return {
        "state": state,
        "arrival": arrival,
        "skipped": skipped,
        "identity": identity or {},
        "identity_available": identity_available,
        "hub": {"ok": healthy, "reason": "ok" if healthy else "NO_FRAME"},
        "faulted": state == FAULT,
    }


def drive(perimeter, clock, times, **kwargs):
    """Feed an unverified arrival (first tick) and then further readings."""
    verdicts = []
    for index, moment in enumerate(times):
        clock["t"] = moment
        verdicts.append(
            perimeter.on_sample(
                sample(PRESENT_UNVERIFIED, arrival=(index == 0), **kwargs)
            )
        )
    return verdicts


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("SATURN_SECURITY_DIR", str(tmp_path / "security"))
    monkeypatch.setattr(passphrase_vault, "bypass_active", lambda: False)
    monkeypatch.setattr(passphrase_vault, "bypass_remaining", lambda: 0.0)
    monkeypatch.setattr(passphrase_vault, "configured", lambda: True)
    # A test that answers the passphrase challenge must not write a real 24-hour
    # bypass into the owner's settings file.
    monkeypatch.setattr(passphrase_vault, "grant_bypass", lambda *a, **k: 0.0)
    monkeypatch.setattr(passphrase_vault, "clear_bypass", lambda *a, **k: None)
    # Alert files are redirected by SATURN_SECURITY_DIR, but the permanent memory
    # they feed is not, so a test-triggered alert must not reach the owner's memory.
    monkeypatch.setattr(security_alerts, "_remember", lambda payload: None)


# ── Things that must never lock ─────────────────────────────────────────────

def test_one_reading_never_locks():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0])

    assert spies.locks == 0
    assert "one reading" in verdicts[-1]["reason"]


def test_two_readings_three_seconds_apart_are_not_enough():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0, 703.0])

    assert spies.locks == 0
    assert "too soon" in verdicts[-1]["reason"]


def test_a_walk_past_while_the_owner_is_still_there_never_locks():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [100.0, 106.0, 110.0])

    assert spies.locks == 0
    assert "owner seen recently" in verdicts[-1]["reason"]


def test_a_known_person_at_the_desk_never_locks():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    clock["t"] = 900.0

    for moment in (900.0, 906.0, 910.0):
        clock["t"] = moment
        perimeter.on_sample(sample(PRESENT_KNOWN, arrival=False))

    assert spies.locks == 0


def test_a_faulty_camera_hard_disables_the_lock():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    clock["t"] = 900.0
    first = perimeter.on_sample(sample(FAULT, healthy=False))
    for moment in (906.0, 910.0):
        clock["t"] = moment
        perimeter.on_sample(sample(FAULT, healthy=False))

    assert spies.locks == 0
    assert first["fault"] is True
    assert "camera_fault" in spies.kinds()


def test_a_fault_is_announced_once_not_every_three_seconds():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    for index, moment in enumerate((900.0, 903.0, 906.0, 909.0)):
        clock["t"] = moment
        perimeter.on_sample(sample(FAULT, healthy=False))

    assert spies.kinds().count("camera_fault") == 1


def test_a_fault_reports_the_capture_error_not_the_hub_state():
    """The hub says "idle" whenever it is not holding the device.

    Leading with that produced live alerts reading "the camera stopped working:
    idle", which names a state that is not a fault at all and hides the reason the
    owner would need in order to fix it.
    """
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    reading = sample(FAULT, healthy=False)
    reading["hub"] = {"ok": False, "reason": "idle"}
    reading["reason"] = "CAPTURE_FAILED: device 0 could not be opened"

    clock["t"] = 900.0
    verdict = perimeter.on_sample(reading)

    assert "CAPTURE_FAILED" in verdict["reason"]
    assert "idle" not in verdict["reason"]
    assert spies.kinds().count("camera_fault") == 1


def test_a_fault_alert_describes_the_fault_not_the_previous_reading():
    clock = {"t": 0.0}
    perimeter, _spies = make_perimeter(clock)

    clock["t"] = 100.0
    perimeter.on_sample(sample(PRESENT_OWNER, arrival=True))

    clock["t"] = 200.0
    perimeter.on_sample(sample(FAULT, healthy=False))

    rows = security_alerts._read_lines(security_alerts.alerts_path())
    faults = [row for row in rows if row.get("kind") == "camera_fault"]

    assert len(faults) == 1
    # This used to carry the verdict from the previous sample, so a camera fault
    # was filed with the state "PRESENT_OWNER" and reason "owner seen".
    assert faults[0]["state"] == FAULT
    assert "NO_FRAME" in (faults[0]["reason"] or "")


def test_a_dry_run_cannot_lock_even_when_called_directly():
    """The lock is refused at the point of no return, not only in the callers."""
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, dry_run=True, armed=True)
    verdict = {"action": "lock", "reason": "", "state": PRESENT_UNVERIFIED}

    perimeter._perform_lock("unverified_arrival", sample(PRESENT_UNVERIFIED), verdict)

    assert spies.locks == 0
    assert verdict["reason"] == "dry run"


def test_missing_identity_models_never_lock():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0], identity_available=False)

    assert spies.locks == 0
    assert "identity unavailable" in verdicts[-1]["blocked_by"]


def test_nobody_enrolled_never_locks():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, enrolled=())

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert "nobody enrolled" in verdicts[-1]["blocked_by"]


def test_an_active_bypass_never_locks(monkeypatch):
    monkeypatch.setattr(passphrase_vault, "bypass_active", lambda: True)
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert "bypass active" in verdicts[-1]["blocked_by"]


def test_a_dry_run_announces_instead_of_locking():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, dry_run=True)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert "dry run" in verdicts[-1]["blocked_by"]
    assert "unverified_person" in spies.kinds()


def test_an_unarmed_perimeter_announces_instead_of_locking():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, armed=False)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert "not armed" in verdicts[-1]["blocked_by"]


def test_a_disabled_perimeter_judges_nothing():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, enabled=False)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert verdicts[-1]["reason"] == "disabled"


def test_a_skipped_tick_is_not_evidence():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    clock["t"] = 700.0
    perimeter.on_sample(sample(PRESENT_UNVERIFIED, arrival=True, skipped=True))
    clock["t"] = 706.0
    perimeter.on_sample(sample(PRESENT_UNVERIFIED, arrival=False, skipped=True))

    assert spies.locks == 0
    assert spies.evidence == 0


# ── The one path that does lock ─────────────────────────────────────────────
def test_the_full_sequence_locks_exactly_once():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0, 703.0, 707.0])

    assert spies.locks == 1
    assert verdicts[-1]["action"] == "lock"
    assert verdicts[-1]["reason"] == "unverified_arrival"


# ── The safety valve: a face that cannot be matched is not a verdict ────────

def test_an_owner_the_camera_cannot_match_is_asked_not_locked():
    """The realistic false positive: you come back and the match fails.

    "Not the owner" and "the owner in worse light" give this comparison the
    same answer, so the machine asks instead of guessing.
    """
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    spies.answers = [True]

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert verdicts[-1]["action"] == "bypass"
    assert "bypass_granted" in spies.kinds()


def test_answering_the_prompt_wrongly_still_locks():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    spies.answers = [False]

    drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 1


def test_the_prompt_comes_after_the_evidence_not_before_it():
    clock = {"t": 0.0}
    order = []
    perimeter, spies = make_perimeter(clock)
    spies.capture = lambda sample, seconds: order.append("evidence") or {"photo": "x"}
    perimeter._evidence_fn = spies.capture
    perimeter._ask_passphrase = lambda reason: order.append("prompt") and False

    drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert order[:2] == ["evidence", "prompt"]


def test_strict_mode_locks_without_asking():
    # The deployment that wants no prompt at all keeps that behaviour.
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, confirm_before_lock=False)
    asked = []
    perimeter._ask_passphrase = lambda reason: asked.append(reason) or True

    drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert asked == []
    assert spies.locks == 1


def test_a_missing_prompt_fails_closed_instead_of_locking():
    """No prompt seam must not quietly mean "lock immediately".

    "Nobody could be asked" and "the answer was wrong" are both False to the
    challenge, and only one of them is a reason to act.
    """
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    perimeter._ask_passphrase = None

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert verdicts[-1]["reason"] == "confirmation_unavailable"
    assert "confirmation_unavailable" in spies.kinds()


def test_a_cleared_passphrase_fails_closed_too(monkeypatch):
    # An armed perimeter whose passphrase was removed has no way back in either.
    monkeypatch.setattr(passphrase_vault, "configured", lambda: False)
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert spies.locks == 0
    assert verdicts[-1]["reason"] == "confirmation_unavailable"


def test_status_says_whether_it_could_even_ask():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    assert perimeter.status()["can_prompt"] is True

    perimeter._ask_passphrase = None
    assert perimeter.status()["can_prompt"] is False


def test_a_stale_owner_name_falls_back_to_the_only_enrolled_person():
    """A leftover placeholder used to block arming on a machine with one face."""
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, owner_name="owner", enrolled=("Ada",))

    assert perimeter._owner_name() == "Ada"
    assert perimeter.status()["owner"] == "Ada"


def test_a_stale_owner_name_with_two_people_enrolled_asks_instead_of_guessing():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, owner_name="owner", enrolled=("Ada", "Bob"))

    assert perimeter._owner_name() == ""


def test_the_watcher_is_told_the_owner_that_was_actually_resolved(monkeypatch):
    # The sensor decides PRESENT_OWNER by comparing a name to watcher.owner_name;
    # handing it a placeholder made the real owner look merely "known".
    monkeypatch.setattr(security_perimeter, "save_config",
                        lambda patch: {**dict(BASE), **patch})

    class Watcher:
        owner_name = ""
        hub = None

    watcher = Watcher()
    perimeter = SecurityPerimeter(watcher=watcher, hub=FakeHub(), config=dict(BASE))
    perimeter._enrolled = lambda: ["Ada"]

    perimeter.update({"away_seconds": 300.0})

    assert watcher.owner_name == "Ada"


def test_a_prompt_is_never_shown_when_the_lock_is_not_permitted():
    # A dry run must not put a passphrase dialog in front of the owner.
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, dry_run=True)
    asked = []
    perimeter._ask_passphrase = lambda reason: asked.append(reason) or True

    drive(perimeter, clock, [700.0, 706.0, 710.0])

    assert asked == []
    assert spies.locks == 0


def test_evidence_is_captured_before_the_lock_is_asked_for():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, armed=False)

    drive(perimeter, clock, [700.0, 706.0, 710.0])

    # Captured even though nothing locked: the evidence is the audit trail.
    assert spies.evidence == 1
    assert spies.locks == 0


def test_the_cooldown_stops_a_second_lock():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    drive(perimeter, clock, [700.0, 706.0, 710.0])
    assert spies.locks == 1

    # Someone leaves and comes back a minute later.
    clock["t"] = 720.0
    perimeter.on_sample(sample(ABSENT))
    drive(perimeter, clock, [800.0, 806.0, 810.0])

    assert spies.locks == 1


def test_a_new_episode_after_the_cooldown_can_lock_again():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    drive(perimeter, clock, [700.0, 706.0, 710.0])
    assert spies.locks == 1

    clock["t"] = 1400.0
    perimeter.on_sample(sample(ABSENT))
    drive(perimeter, clock, [1450.0, 1456.0, 1460.0])

    assert spies.locks == 2


def test_an_owner_sighting_refreshes_the_grace_window():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    clock["t"] = 700.0
    perimeter.on_sample(sample(PRESENT_OWNER))

    verdicts = drive(perimeter, clock, [703.0, 709.0, 713.0])

    assert spies.locks == 0
    assert "owner seen recently" in verdicts[-1]["reason"]


def test_the_lock_is_aborted_if_the_camera_dies_at_the_last_moment():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)

    verdict = {"action": "none", "reason": "", "state": PRESENT_UNVERIFIED}
    perimeter._perform_lock("unverified_arrival",
                            sample(PRESENT_UNVERIFIED, healthy=False), verdict)

    assert spies.locks == 0
    assert verdict["action"] == "none"
    assert "camera failed" in verdict["reason"]


# ── The inference-timeout fallback ──────────────────────────────────────────

def test_a_stuck_inference_offers_the_passphrase_instead_of_locking():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    spies.answers = [True]

    verdicts = drive(perimeter, clock, [700.0, 706.0, 710.0],
                     identity={"name": None, "reason": "INFERENCE_TIMEOUT"})

    assert spies.locks == 0
    assert verdicts[-1]["action"] == "bypass"


def test_a_refused_passphrase_falls_back_to_locking():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock)
    spies.answers = [False]

    drive(perimeter, clock, [700.0, 706.0, 710.0],
          identity={"name": None, "reason": "INFERENCE_TIMEOUT"})

    assert spies.locks == 1


# ── Empty desk ──────────────────────────────────────────────────────────────

def test_the_away_announcement_fires_once():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, away_lock="announce", away_seconds=60.0)

    clock["t"] = 700.0
    perimeter.on_sample(sample(ABSENT))
    clock["t"] = 770.0
    perimeter.on_sample(sample(ABSENT))
    clock["t"] = 780.0
    perimeter.on_sample(sample(ABSENT))

    assert spies.kinds().count("away_unlocked") == 1
    assert spies.locks == 0


def test_away_lock_mode_locks_an_unattended_session():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, away_lock="lock", away_seconds=60.0)

    clock["t"] = 700.0
    perimeter.on_sample(sample(ABSENT))
    clock["t"] = 770.0
    verdict = perimeter.on_sample(sample(ABSENT))

    assert spies.locks == 1
    assert verdict["action"] == "lock"


def test_away_off_does_nothing_at_all():
    clock = {"t": 0.0}
    perimeter, spies = make_perimeter(clock, away_lock="off", away_seconds=10.0)

    clock["t"] = 700.0
    perimeter.on_sample(sample(ABSENT))
    clock["t"] = 900.0
    perimeter.on_sample(sample(ABSENT))

    assert spies.locks == 0
    assert "away_unlocked" not in spies.kinds()


# ── Arming ──────────────────────────────────────────────────────────────────

def test_arming_refuses_with_a_cold_camera(monkeypatch, tmp_path):
    from core import face_id

    monkeypatch.setattr(face_id, "people", lambda path=None: ["Ada"])
    monkeypatch.setattr(face_id, "available", lambda: {"identity": True})
    monkeypatch.setattr(security_perimeter, "save_config", lambda patch: dict(BASE))

    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock, hub_healthy=False)
    result = perimeter.arm(owner="Ada")

    assert result["ok"] is False
    assert any("self-test" in problem for problem in result["problems"])


def test_arming_refuses_without_an_enrolled_owner(monkeypatch):
    from core import face_id

    monkeypatch.setattr(face_id, "people", lambda path=None: [])
    monkeypatch.setattr(face_id, "available", lambda: {"identity": False, "hint": "need models"})
    monkeypatch.setattr(security_perimeter, "save_config", lambda patch: dict(BASE))

    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock, enrolled=())
    result = perimeter.arm()

    assert result["ok"] is False
    assert any("Nobody is enrolled" in problem for problem in result["problems"])


def test_arming_refuses_without_a_passphrase(monkeypatch):
    from core import face_id

    monkeypatch.setattr(face_id, "people", lambda path=None: ["Ada"])
    monkeypatch.setattr(face_id, "available", lambda: {"identity": True})
    monkeypatch.setattr(passphrase_vault, "configured", lambda: False)
    monkeypatch.setattr(security_perimeter, "save_config", lambda patch: dict(BASE))

    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock)
    result = perimeter.arm(owner="Ada")

    assert result["ok"] is False
    assert any("passphrase" in problem for problem in result["problems"])


def test_arming_succeeds_when_everything_is_present(monkeypatch):
    from core import face_id

    monkeypatch.setattr(face_id, "people", lambda path=None: ["Ada"])
    monkeypatch.setattr(face_id, "available", lambda: {"identity": True})
    saved = {}

    def _save(patch):
        saved.update(patch)
        return {**BASE, **saved}

    monkeypatch.setattr(security_perimeter, "save_config", _save)

    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock)
    result = perimeter.arm(owner="Ada")

    assert result["ok"] is True
    assert saved["armed"] is True
    assert saved["dry_run"] is False
    assert saved["owner_name"] == "Ada"


def test_disarming_always_works():
    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock)

    result = perimeter.disarm()

    assert result["ok"] is True
    assert result["armed"] is False


def test_status_states_the_three_tiers_honestly(monkeypatch):
    from core import face_id

    monkeypatch.setattr(face_id, "available",
                        lambda: {"identity": False, "detector": "haar", "hint": "need models"})

    clock = {"t": 0.0}
    perimeter, _ = make_perimeter(clock, armed=False, dry_run=True)
    status = perimeter.status()

    assert status["armed"] is False
    assert status["dry_run"] is True
    assert status["identity_available"] is False
    assert status["detector"] == "haar"
    assert status["hint"] == "need models"
    assert status["enrolled"] == ["Ada"]
