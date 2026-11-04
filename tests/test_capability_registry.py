"""The capability registry: the vocabulary, the devices, and who may act.

These tests pin the three claims the universal layer makes:

1. A device is a bundle of capabilities, not a class - so the same code drives
   a lamp, a TV and a printer.
2. The vocabulary is shared with the trust gate, so a capability cannot be
   allowed at one layer and forbidden at the other.
3. Nothing is sent anywhere before the request has been checked, and nothing
   is reported as done that an adapter did not confirm.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import capability_registry as registry
from core import trust_gate
from core.capability_registry import Capability, Device, Param  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(registry.ENV_PATH, str(tmp_path / "devices.json"))
    registry.reset_cache()
    # Nothing in this file should touch the house.
    monkeypatch.setattr("core.trust_gate.evaluate",
                        lambda *a, **k: trust_gate.Decision(
                            allowed=True, capability=str(a[0] if a else ""),
                            trust=trust_gate.Trust.OWNER, verified=True))
    yield
    registry.reset_cache()


def seed(*devices: Device) -> None:
    registry.save(list(devices))


def lamp(**overrides) -> Device:
    payload = dict(
        id="lamp", name="Desk lamp", adapter="dry_run", kind="light",
        capabilities={"switch.on": {}, "switch.off": {}, "level.set": {}},
    )
    payload.update(overrides)
    return Device(**payload)


def tv(**overrides) -> Device:
    payload = dict(
        id="tv", name="Smart TV", adapter="dry_run", kind="tv",
        capabilities={"switch.on": {}, "switch.off": {}, "media.play": {},
                      "display.show": {}},
    )
    payload.update(overrides)
    return Device(**payload)


def printer(**overrides) -> Device:
    payload = dict(
        id="printer", name="3D printer", adapter="dry_run", kind="printer",
        capabilities={"job.start": {}, "job.cancel": {}, "job.status": {},
                      "motion.home": {}},
    )
    payload.update(overrides)
    return Device(**payload)


# ── The vocabulary ─────────────────────────────────────────────────────────

def test_the_vocabulary_covers_every_kind_of_thing():
    for capability in ("switch.on", "level.set", "temperature.set", "media.play",
                       "display.show", "job.start", "motion.move", "lock.unlock",
                       "app.launch", "text.input", "screen.capture"):
        assert registry.known(capability), capability


def test_an_unlisted_member_of_a_known_family_is_legal():
    assert registry.known("switch.dp3") is True
    assert registry.known("nonsense.thing") is False


def test_capability_ids_are_normalised():
    assert registry.normalise("  Switch.ON ") == "switch.on"
    assert registry.known("SWITCH.ON") is True


def test_a_capability_carries_its_own_arguments():
    level = registry.capability("level.set")
    assert [param.name for param in level.params] == ["level"]
    assert level.params[0].minimum == 0 and level.params[0].maximum == 100


def test_the_signature_reads_like_a_call():
    assert registry.capability("level.set").signature() == "level.set(level:int)"
    assert registry.capability("switch.on").signature() == "switch.on"


def test_the_vocabulary_is_describable_in_one_block():
    text = registry.describe_vocabulary()
    assert "switch.on" in text and "job.start" in text


# ── Validation happens before anything is sent ─────────────────────────────

def test_a_missing_required_argument_is_refused():
    ok, message = registry.validate("job.start", {})
    assert ok is True  # job.start has no *required* parameter

    ok, message = registry.validate("level.set", {})
    assert ok is False
    assert "level" in message


def test_a_level_outside_its_range_is_refused():
    assert registry.validate("level.set", {"level": 101})[0] is False
    assert registry.validate("level.set", {"level": -1})[0] is False
    assert registry.validate("level.set", {"level": 40})[0] is True


def test_a_level_that_is_not_a_number_is_refused():
    ok, message = registry.validate("level.set", {"level": "quite bright"})
    assert ok is False and "number" in message


def test_an_unknown_capability_is_refused_with_the_known_families():
    ok, message = registry.validate("explode.now", {})
    assert ok is False
    assert "switch" in message


# ── Devices as bundles ─────────────────────────────────────────────────────

def test_a_device_is_a_bundle_of_capabilities():
    seed(lamp())
    device = registry.get("lamp")

    assert device.supports("switch.on") is True
    assert device.supports("job.start") is False
    assert device.capability_ids == ["level.set", "switch.off", "switch.on"]


def test_devices_are_found_by_id_or_by_name():
    seed(lamp(), tv())

    assert registry.resolve("tv").id == "tv"
    assert registry.resolve("Smart TV").id == "tv"
    assert registry.resolve("TV").id == "tv"
    assert registry.resolve("toaster") is None


def test_lookup_by_capability_finds_every_device_that_can_do_it():
    seed(lamp(), tv(), printer())

    plays = [device.id for device in registry.for_capability("media.play")]
    prints = [device.id for device in registry.for_capability("job.start")]

    assert plays == ["tv"]
    assert prints == ["printer"]


def test_a_disabled_device_is_not_offered():
    seed(lamp(enabled=False))
    assert registry.for_capability("switch.on") == []


def test_devices_survive_a_round_trip_through_the_file(tmp_path):
    seed(lamp(notes="on the desk"), printer())
    registry.reset_cache()

    devices = registry.load()
    assert [device.id for device in devices] == ["lamp", "printer"]
    assert registry.get("lamp").notes == "on the desk"
    assert registry.get("printer").capability_ids == [
        "job.cancel", "job.start", "job.status", "motion.home"]


def test_a_hand_written_file_with_a_list_of_capabilities_loads():
    path = registry._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"devices": [
        {"id": "plug", "name": "Espresso plug", "adapter": "mqtt",
         "capabilities": ["switch.on", "switch.off"]}
    ]}), encoding="utf-8")
    registry.reset_cache()

    device = registry.get("plug")
    assert device.capability_ids == ["switch.off", "switch.on"]


def test_a_broken_file_is_an_empty_house_not_a_crash():
    path = registry._path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    registry.reset_cache()

    assert registry.load() == []
    assert "No devices" in registry.describe()


def test_adding_a_duplicate_id_is_refused():
    seed(lamp())
    with pytest.raises(ValueError):
        registry.add(lamp())


def test_a_device_can_be_replaced():
    seed(lamp())
    registry.add(lamp(name="Reading lamp"), replace=True)
    assert registry.get("lamp").name == "Reading lamp"


def test_updating_merges_capabilities_and_state():
    seed(lamp())

    registry.update("lamp", {"capabilities": ["media.play"]})
    assert registry.get("lamp").capability_ids == ["media.play"]

    registry.update("lamp", {"state": {"is_on": True}})
    registry.update("lamp", {"state": {"level": 30}})
    assert registry.get("lamp").state == {"is_on": True, "level": 30}


def test_removing_a_device_that_is_not_there_reports_false():
    seed(lamp())
    assert registry.remove("ghost") is False
    assert registry.remove("lamp") is True
    assert registry.load() == []


def test_a_public_view_hides_secrets_but_keeps_the_shape():
    seed(Device(id="plug", name="Plug", adapter="mqtt",
                address={"broker": "1.2.3.4", "password": "hunter2"},
                capabilities=["switch.on"]))
    public = registry.get("plug").public()

    assert public["address"]["broker"] == "1.2.3.4"
    assert public["address"]["password"] == "***"


def test_the_description_lists_each_device_with_its_capabilities():
    seed(tv(), printer())
    text = registry.describe()

    assert "Smart TV" in text and "media.play" in text
    assert "3D printer" in text and "job.start" in text


# ── Execution ──────────────────────────────────────────────────────────────

def test_the_dry_run_adapter_reports_success_without_moving_anything():
    seed(lamp())
    result = registry.execute("lamp", "switch.on")

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert "[dry run]" in result["detail"]


def test_execution_updates_the_device_state():
    seed(lamp())
    registry.execute("lamp", "switch.on")

    assert registry.get("lamp").state == {"is_on": True}


def test_an_unknown_device_is_reported_not_raised():
    seed(lamp())
    result = registry.execute("toaster", "switch.on")

    assert result["ok"] is False
    assert result["error"] == "NO_SUCH_DEVICE"
    assert "toaster" in result["detail"]


def test_a_capability_the_device_lacks_is_reported_with_what_it_has():
    seed(lamp())
    result = registry.execute("lamp", "job.start")

    assert result["ok"] is False
    assert result["error"] == "UNSUPPORTED"
    assert "switch.on" in result["detail"]


def test_a_bad_argument_never_reaches_the_adapter():
    seed(lamp())
    result = registry.execute("lamp", "level.set", {"level": 400})

    assert result["ok"] is False
    assert result["error"] == "BAD_REQUEST"


def test_an_unknown_adapter_is_reported():
    seed(lamp(adapter="zigbee"))
    result = registry.execute("lamp", "switch.on")

    assert result["ok"] is False
    assert result["error"] == "NO_ADAPTER"


def test_the_gate_is_asked_before_the_adapter(monkeypatch):
    seed(lamp())
    asked = []

    def refuse(capability, **kwargs):
        asked.append(capability)
        return trust_gate.Decision(allowed=False, capability=capability,
                                   trust=trust_gate.Trust.UNKNOWN, verified=True,
                                   policy_allowed=False, reason="stranger",
                                   spoken="I don't recognise who's asking.")

    monkeypatch.setattr("core.trust_gate.evaluate", refuse)
    result = registry.execute("lamp", "switch.on")

    assert asked == ["switch.on"]
    assert result["ok"] is False
    assert result["error"] == "REFUSED"
    assert "recognise" in result["detail"]


def test_a_pending_confirmation_also_blocks_a_physical_action(monkeypatch):
    seed(printer())
    monkeypatch.setattr(
        "core.trust_gate.evaluate",
        lambda capability, **k: trust_gate.Decision(
            allowed=False, capability=capability, trust=trust_gate.Trust.OWNER,
            verified=True, needs_confirm=True, policy_allowed=True,
            spoken="I can start that job. Confirm and I'll go ahead."))

    result = registry.execute("printer", "job.start")

    assert result["ok"] is False
    assert result["error"] == "REFUSED"
    assert result["policy_allowed"] is True


def test_the_gate_can_be_skipped_only_on_purpose(monkeypatch):
    seed(lamp())

    def explode(*args, **kwargs):
        raise AssertionError("the gate must not be consulted")

    monkeypatch.setattr("core.trust_gate.evaluate", explode)
    assert registry.execute("lamp", "switch.on", gate=False)["ok"] is True


def test_a_broken_adapter_does_not_raise(monkeypatch):
    seed(lamp())

    class Angry:
        name = "dry_run"

        def send(self, device, capability, params):
            raise RuntimeError("driver crashed")

    monkeypatch.setattr("adapters.get_adapter", lambda name: Angry())
    result = registry.execute("lamp", "switch.on")

    assert result["ok"] is False
    assert result["error"] == "ADAPTER_FAILED"


def test_an_adapter_that_returns_nonsense_is_not_trusted(monkeypatch):
    seed(lamp())
    monkeypatch.setattr("adapters.get_adapter", lambda name: type(
        "Weird", (), {"send": lambda self, d, c, p: "all good I promise"})())

    result = registry.execute("lamp", "switch.on")

    assert result["ok"] is False
    assert result["error"] == "ADAPTER_CONTRACT"


# ── The two layers agree ──────────────────────────────────────────────────

def _tier_of(rule) -> str:
    if rule.secret:
        return registry.SECRET
    if rule.confirm:
        return registry.COSTLY
    return registry.READ_ONLY if rule.min_trust == trust_gate.Trust.UNKNOWN else registry.COMFORT


def test_every_capability_is_gated_at_the_tier_it_declares():
    """A capability must never be declared harmless here and guarded there."""
    drift = []
    for capability_id, spec in registry.CAPABILITIES.items():
        _, rule = trust_gate.rule_for(capability_id)
        if _tier_of(rule) != spec.trust:
            drift.append((capability_id, spec.trust, _tier_of(rule), rule.min_trust))

    assert drift == []


def test_unlocking_is_the_only_secret_capability():
    secrets = [cap.id for cap in registry.CAPABILITIES.values()
               if cap.trust == registry.SECRET]
    assert secrets == ["lock.unlock"]


# ── A device is a row, not a class ────────────────────────────────────────

def test_a_plain_row_can_be_added_without_building_a_dataclass():
    """A device is a declarative entry, so a dict is the natural input.

    This used to raise ``TypeError: asdict() should be called on dataclass
    instances`` from inside :mod:`dataclasses` - a message that named the
    helper instead of the device, and only appeared after the row had been
    accepted for writing.
    """
    device = registry.add({"id": "heater", "name": "Desk heater",
                           "adapter": "dry_run", "capabilities": ["switch.on"]})

    assert isinstance(device, Device)
    assert registry.get("heater") is not None
    assert registry.get("heater").capability_ids == ["switch.on"]


def test_a_row_without_an_id_is_refused_with_a_useful_message():
    with pytest.raises(ValueError, match="needs an id"):
        registry.add({"name": "nameless"})


def test_saving_rows_writes_rows():
    registry.save([{"id": "fan", "name": "Fan", "capabilities": ["switch.on"]}])

    written = json.loads(Path(os.environ[registry.ENV_PATH]).read_text(encoding="utf-8"))

    assert written["devices"][0]["id"] == "fan"
    assert written["devices"][0]["capabilities"] == {"switch.on": {}}
