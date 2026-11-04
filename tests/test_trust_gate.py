"""The gate's policy. Every test here is about who is allowed to do what.

The failure modes that matter are opposite: a stranger must not be able to
start a print or unlock anything, and the owner must not be locked out of his
own assistant because a camera is unplugged. Both directions are tested.
"""

from __future__ import annotations

import pytest

from core import trust_gate
from core.presence_watch import (
    ABSENT, FAULT, LOCKED, PRESENT_KNOWN, PRESENT_OWNER, PRESENT_UNVERIFIED,
    STARTING,
)
from core.trust_gate import Decision, Evidence, Trust


def snapshot(state: str, name: str = "", reason: str = "") -> dict:
    return {"state": state, "identity": {"name": name} if name else {},
            "reason": reason or state.lower()}


class Watcher:
    def __init__(self, snap: dict):
        self._snap = snap

    def snapshot(self) -> dict:
        return dict(self._snap)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Default to an armed perimeter the tests then steer explicitly."""
    monkeypatch.setattr("core.security_perimeter.load_config",
                        lambda: {"enabled": True, "armed": True})
    monkeypatch.setattr("core.presence_watch.get_presence_watcher",
                        lambda **kw: Watcher(snapshot(STARTING)))
    monkeypatch.setattr(trust_gate, "_owner_name", lambda: "Ada")
    monkeypatch.setattr(trust_gate, "mode", lambda: trust_gate.MODE_ENFORCE)
    trust_gate.clear_override()
    yield
    trust_gate.clear_override()


def see(monkeypatch, state: str, name: str = "", reason: str = ""):
    monkeypatch.setattr("core.presence_watch.get_presence_watcher",
                        lambda **kw: Watcher(snapshot(state, name, reason)))


# ── Reading the evidence ────────────────────────────────────────────────────

def test_an_owner_face_is_a_verified_owner(monkeypatch):
    see(monkeypatch, PRESENT_OWNER, "Ada")
    evidence = trust_gate.presence_evidence()

    assert evidence.trust == Trust.OWNER
    assert evidence.verified is True
    assert evidence.source == "face"
    assert evidence.name == "Ada"


def test_another_enrolled_person_is_a_guest(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    evidence = trust_gate.presence_evidence()

    assert evidence.trust == Trust.GUEST
    assert evidence.verified is True


def test_an_unrecognised_face_is_not_a_guest(monkeypatch):
    """The whole point of enrolling the owner is that this is a different claim."""
    see(monkeypatch, PRESENT_UNVERIFIED, "")
    assert trust_gate.presence_evidence().trust == Trust.UNKNOWN


def test_an_empty_frame_while_armed_is_not_the_owner(monkeypatch):
    see(monkeypatch, ABSENT)
    assert trust_gate.presence_evidence().trust == Trust.UNKNOWN


def test_a_dead_camera_assumes_the_owner_but_says_so(monkeypatch):
    """No lockout: a single desktop with a broken sensor is still the owner's."""
    see(monkeypatch, FAULT, reason="LENS_COVERED")
    evidence = trust_gate.presence_evidence()

    assert evidence.trust == Trust.OWNER
    assert evidence.verified is False
    assert evidence.source == "fault"


def test_a_locked_session_is_not_evidence_of_absence(monkeypatch):
    see(monkeypatch, LOCKED)
    assert trust_gate.presence_evidence().trust == Trust.OWNER


def test_an_unarmed_perimeter_does_not_claim_verification(monkeypatch):
    monkeypatch.setattr("core.security_perimeter.load_config",
                        lambda: {"enabled": True, "armed": False})
    evidence = trust_gate.presence_evidence()

    assert evidence.trust == Trust.OWNER
    assert evidence.verified is False
    assert evidence.source == "perimeter_off"


def test_a_broken_presence_module_fails_open_for_the_owner(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("cv2 is on fire")

    monkeypatch.setattr("core.presence_watch.get_presence_watcher", boom)
    evidence = trust_gate.presence_evidence()

    assert evidence.trust == Trust.OWNER
    assert evidence.verified is False


# ── Ordinary control stays ordinary ────────────────────────────────────────

def test_a_guest_may_turn_on_a_light(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    decision = trust_gate.evaluate("switch.on")

    assert decision.allowed is True
    assert decision.spoken == ""


def test_the_owner_does_not_get_a_question_for_a_light(monkeypatch):
    see(monkeypatch, PRESENT_OWNER, "Ada")
    assert trust_gate.evaluate("switch.on").allowed is True


def test_a_stranger_may_not_touch_a_switch(monkeypatch):
    see(monkeypatch, PRESENT_UNVERIFIED)
    decision = trust_gate.evaluate("switch.on")

    assert decision.allowed is False
    assert decision.trust == Trust.UNKNOWN
    assert "don't recognise" in decision.spoken


def test_reading_is_open_to_anyone_in_the_room(monkeypatch):
    see(monkeypatch, PRESENT_UNVERIFIED)
    for capability in ("sensor.read", "status.printer", "list.devices", "get.temperature"):
        assert trust_gate.evaluate(capability).allowed is True, capability


# ── Work that costs something ──────────────────────────────────────────────

def test_starting_a_job_asks_once(monkeypatch):
    see(monkeypatch, PRESENT_OWNER, "Ada")

    first = trust_gate.evaluate("job.start")
    assert first.allowed is False
    assert first.needs_confirm is True
    assert "Confirm" in first.spoken

    second = trust_gate.evaluate("job.start", confirm=True)
    assert second.allowed is True


def test_a_guest_cannot_start_a_print(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    decision = trust_gate.evaluate("job.start", confirm=True)

    assert decision.allowed is False
    assert decision.policy_allowed is False


def test_a_guest_cannot_delete_files(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    assert trust_gate.evaluate("file.delete.repo", confirm=True).allowed is False


def test_cancelling_is_always_allowed(monkeypatch):
    """Stopping is the safe direction, so it must never be the hard one."""
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    assert trust_gate.evaluate("job.cancel").allowed is True


# ── Secrets never ride on an assumption ────────────────────────────────────

def test_a_verified_owner_face_is_enough_for_unlocking(monkeypatch):
    """A face the camera recognised is a stronger claim than a typed secret."""
    see(monkeypatch, PRESENT_OWNER, "Ada")
    decision = trust_gate.evaluate("lock.unlock")

    assert decision.needs_passphrase is False
    assert decision.needs_confirm is True
    assert trust_gate.evaluate("lock.unlock", confirm=True).allowed is True


def test_unlocking_needs_the_passphrase_when_the_camera_is_dead(monkeypatch):
    see(monkeypatch, FAULT)
    decision = trust_gate.evaluate("lock.unlock")

    assert decision.allowed is False
    assert decision.needs_passphrase is True


def test_unlocking_passes_with_the_passphrase_when_nobody_was_recognised(monkeypatch):
    see(monkeypatch, FAULT)
    assert trust_gate.evaluate("lock.unlock", passphrase=True).allowed is True


def test_a_guest_cannot_unlock_with_a_passphrase(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    decision = trust_gate.evaluate("lock.unlock", passphrase=True)

    assert decision.allowed is False
    assert decision.policy_allowed is False


def test_locking_is_guest_level_but_unlocking_is_not(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")

    assert trust_gate.evaluate("lock.lock").allowed is True
    assert trust_gate.evaluate("lock.unlock", passphrase=True).allowed is False


def test_disarming_security_without_a_face_needs_the_passphrase(monkeypatch):
    see(monkeypatch, FAULT)
    assert trust_gate.evaluate("security.disarm").needs_passphrase is True


def test_the_passphrase_also_answers_the_confirmation(monkeypatch):
    see(monkeypatch, FAULT)
    decision = trust_gate.evaluate("lock.unlock", passphrase=True)

    assert decision.needs_confirm is False
    assert decision.allowed is True


# ── The intent vocabulary ──────────────────────────────────────────────────

def test_an_intent_is_translated_into_a_capability():
    assert trust_gate.canonical("shopping") == "purchase.retail"
    assert trust_gate.canonical("open_app") == "app.launch"
    assert trust_gate.canonical("sensor.read") == "sensor.read"


def test_shopping_is_owner_work(monkeypatch):
    see(monkeypatch, PRESENT_OWNER, "Ada")
    assert trust_gate.evaluate("shopping").needs_confirm is True


def test_opening_an_app_is_guest_work(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    assert trust_gate.evaluate("open_app").allowed is True


def test_every_registered_intent_has_a_policy():
    """A new intent must not silently inherit the loosest default."""
    from agent.action_router import registered_intents

    for intent in registered_intents():
        _, rule = trust_gate.rule_for(intent)
        assert rule is not trust_gate.DEFAULT_RULE, intent


def test_an_unlisted_capability_defaults_to_guest(monkeypatch):
    see(monkeypatch, PRESENT_KNOWN, "Bob")
    assert trust_gate.evaluate("something.brand_new").allowed is True


# ── Modes, overrides, and the log ──────────────────────────────────────────

def test_observe_mode_records_but_does_not_block(monkeypatch):
    monkeypatch.setattr(trust_gate, "mode", lambda: trust_gate.MODE_OBSERVE)
    see(monkeypatch, PRESENT_UNVERIFIED)
    decision = trust_gate.evaluate("file.delete.repo")

    assert decision.allowed is True
    assert decision.policy_allowed is False
    assert decision.enforced is False


def test_an_override_wins_over_the_camera(monkeypatch):
    see(monkeypatch, PRESENT_UNVERIFIED)
    trust_gate.set_override(Trust.OWNER)

    assert trust_gate.presence_evidence().source == "manual"
    assert trust_gate.evaluate("switch.on").allowed is True


def test_clearing_the_override_restores_the_camera(monkeypatch):
    see(monkeypatch, PRESENT_UNVERIFIED)
    trust_gate.set_override(Trust.OWNER)
    trust_gate.clear_override()

    assert trust_gate.evaluate("switch.on").allowed is False


def test_the_log_keeps_the_most_recent_decisions(monkeypatch):
    see(monkeypatch, PRESENT_UNVERIFIED)
    for _ in range(trust_gate._log.maxlen + 10):
        trust_gate.evaluate("switch.on")

    recent = trust_gate.recent_decisions(3)
    assert len(recent) == 3
    assert all(item["capability"] == "switch.on" for item in recent)
    assert recent[-1]["allowed"] is False


def test_check_answers_with_a_bool(monkeypatch):
    see(monkeypatch, PRESENT_OWNER, "Ada")
    assert trust_gate.check("switch.on") is True
    assert trust_gate.check("job.start") is False


def test_status_exposes_no_secret():
    payload = trust_gate.status()
    text = repr(payload).lower()

    assert payload["owner"] == "Ada"
    assert "passphrase" not in text
    assert "hash" not in text


def test_a_decision_serialises_for_the_log():
    decision = Decision(allowed=False, capability="job.start", trust=Trust.GUEST,
                        verified=True, needs_confirm=True, reason="needs a spoken yes")
    payload = decision.as_dict()

    assert payload["trust"] == "GUEST"
    assert payload["needs_confirm"] is True
    assert decision.blocked is True


def test_evidence_serialises_without_the_camera():
    payload = Evidence(trust=Trust.GUEST, verified=True, source="face").as_dict()
    assert payload["trust"] == "GUEST"
    assert payload["source"] == "face"
