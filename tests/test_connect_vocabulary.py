"""One address space: a phone is a device, and its words are capabilities.

The important test in this file is not any single translation - it is the pair
of guards at the top. One reads ``capability_registry.vocabulary()``, the other
reads the Android app's own ``when (action)`` block, and between them a line
can only be missing from the table if nobody noticed.
"""

import json
import os
import re
from pathlib import Path

import pytest

from actions import saturn_connect as bc
from saturn_connect import service as connect_service
from core import capability_registry as registry
from core import connect_bridge, connect_vocabulary as vocabulary, trust_gate
from core.capability_registry import Device

ROOT = Path(__file__).resolve().parent.parent
ANDROID = (ROOT / "saturn-connect-android" / "app" / "src" / "main" / "java"
           / "com" / "saturn" / "connect" / "commands" / "DeviceCommandHandler.kt")

PHONE = {
    "device_id": "android_001",
    "name": "Galaxy S24",
    "platform": "android",
    "online": True,
    "battery": 82,
    "capabilities": ["camera", "clipboard", "media", "launch_app"],
}


class FakeGateway:
    """The phone, as far as these tests are concerned."""

    def __init__(self, devices=None, answer=None):
        self.devices = [PHONE] if devices is None else devices
        self.answer = answer or {"success": True, "data": {"ok": True}}
        self.calls = []

    def list_devices(self):
        return [dict(device) for device in self.devices]

    def route_command(self, target, action, parameters=None):
        self.calls.append((target, action, dict(parameters or {})))
        if callable(self.answer):
            return self.answer(target, action, parameters or {})
        return dict(self.answer)


def _use(gateway):
    """Point every road at this gateway: tools, adapter and registry."""
    connect_service.set_service_provider(lambda: gateway)
    registry.clear_sources()
    connect_bridge.install()
    registry.reset_cache()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(registry.ENV_PATH, str(tmp_path / "devices.json"))
    # Nothing in this file may find a gateway this process happens to have.
    monkeypatch.setattr(connect_service, "_SERVICE", None)
    registry.reset_cache()
    connect_service.set_service_provider(None)
    yield
    connect_service.set_service_provider(None)
    registry.clear_sources()
    connect_bridge.install()
    registry.reset_cache()


def _allow(capability, **kwargs):
    return trust_gate.Decision(allowed=True, capability=capability,
                               trust=trust_gate.Trust.OWNER, verified=True)


def _deny(capability, **kwargs):
    return trust_gate.Decision(allowed=False, capability=capability,
                               trust=trust_gate.Trust.UNKNOWN, verified=True,
                               policy_allowed=False, spoken="That isn't yours to approve.")


# ── The guards ──────────────────────────────────────────────────────────────

def _android_actions():
    """The actions the phone's own handler answers, read from its source.

    Only the ``when (action)`` block counts: the file has other string arrows
    (a directory enum, for one), and reading those as commands would make this
    guard stricter than the truth.
    """
    source = ANDROID.read_text(encoding="utf-8")
    start = source.index("when (action.lowercase())")
    end = source.find("\n        }", start)
    return set(re.findall(r'"([a-z_]+)"\s*->', source[start:end if end > 0 else len(source)]))


def test_every_capability_is_either_mapped_or_explained():
    """A new capability must be answered for here, one way or the other."""
    for capability in registry.vocabulary():
        if capability == "sensor.read":
            continue
        assert (capability in vocabulary.CAPABILITY_TO_ACTION
                or capability in vocabulary.UNEXPRESSED_CAPABILITIES), (
            f"{capability} is in the vocabulary but not in connect_vocabulary: "
            "map it to a phone action, or say why a phone cannot do it.")


def test_every_mapped_capability_is_real():
    for capability in vocabulary.capabilities():
        assert registry.known(capability), f"'{capability}' is not a registry capability"


@pytest.mark.skipif(not ANDROID.exists(), reason="the Android app is not in this checkout")
def test_the_table_only_names_actions_the_phone_implements():
    implemented = _android_actions()
    for capability, action in vocabulary.CAPABILITY_TO_ACTION.items():
        assert action in implemented, f"{capability} -> '{action}', which the phone does not handle"
    for action in vocabulary.SENSOR_ACTIONS.values():
        assert action in implemented, f"sensor -> '{action}', which the phone does not handle"


@pytest.mark.skipif(not ANDROID.exists(), reason="the Android app is not in this checkout")
def test_every_phone_action_is_mapped_or_explained():
    """The other half of the guard: an action cannot be quietly unreachable."""
    answered = (set(vocabulary.ACTION_TO_CAPABILITY)
                | set(vocabulary.SENSOR_ACTIONS.values())
                | set(vocabulary.UNEXPRESSED_ACTIONS))
    for action in _android_actions():
        assert action in answered, (
            f"the phone implements '{action}' but connect_vocabulary ignores it: "
            "map it, or add a line saying why it has no capability.")


# ── One direction ───────────────────────────────────────────────────────────

def test_a_capability_becomes_the_phones_word():
    assert vocabulary.to_action("switch.on").target == "flashlight_on"
    assert vocabulary.to_action("app.launch", {"package": "com.spotify.music"}).target == "launch_app"


def test_the_argument_is_renamed_not_just_carried():
    moved = vocabulary.to_action("level.set", {"level": 40})
    assert moved.target == "volume_set"
    assert moved.params == {"value": 40}


def test_a_level_that_arrived_as_text_is_still_a_number():
    assert vocabulary.to_action("level.set", {"level": "40"}).params == {"value": 40}


def test_a_show_becomes_a_url():
    shown = vocabulary.to_action("display.show", {"content": "https://example.com"})
    assert (shown.target, shown.params) == ("open_url", {"url": "https://example.com"})


def test_an_argument_the_table_does_not_know_is_carried_through():
    sent = vocabulary.to_action("text.input", {"text": "hello", "clear_first": True})
    assert sent.params == {"text": "hello", "clear_first": True}


def test_a_sensor_needs_a_name():
    unnamed = vocabulary.to_action("sensor.read")
    assert unnamed.ok is False
    assert "battery" in unnamed.problem


def test_a_sensor_name_picks_the_action():
    assert vocabulary.to_action("sensor.read", {"name": "battery"}).target == "get_battery"
    assert vocabulary.to_action("sensor.read", {"name": "info"}).target == "get_device_info"


def test_an_unknown_sensor_says_what_it_can_read():
    missing = vocabulary.to_action("sensor.read", {"name": "pressure"})
    assert missing.ok is False
    assert "pressure" in missing.problem and "battery" in missing.problem


def test_a_phone_cannot_print_and_says_so():
    refused = vocabulary.to_action("job.start")
    assert refused.ok is False
    assert "cannot" in refused.problem


# ── The other direction ─────────────────────────────────────────────────────

def test_the_round_trip_comes_back_to_the_same_capability():
    for capability in vocabulary.CAPABILITY_TO_ACTION:
        action = vocabulary.to_action(capability).target
        assert vocabulary.to_capability(action).target == capability


def test_the_phone_word_comes_back_as_a_capability():
    assert vocabulary.to_capability("volume_set", {"value": 12}).target == "level.set"
    assert vocabulary.to_capability("volume_set", {"value": 12}).params == {"level": 12}
    assert vocabulary.to_capability("get_battery").target == "sensor.read"


def test_unlocking_is_refused_in_words_rather_than_guessed():
    """The PIN is the reason. A secret that travels through a model is not one."""
    both_ways = [vocabulary.to_action("lock.unlock"),
                 vocabulary.to_capability("unlock_phone")]
    for translation in both_ways:
        assert translation.ok is False
        assert "PIN" in translation.problem


def test_an_action_nobody_knows_is_reported_as_such():
    assert vocabulary.to_capability("explode").ok is False


# ── The bridge: a phone in the same list as a lamp ──────────────────────────

def test_a_paired_phone_appears_as_a_device():
    _use(FakeGateway())

    device = registry.resolve("Galaxy S24")

    assert device is not None
    assert device.id == "connect:android_001"
    assert device.adapter == "connect"
    assert device.address == {"target": "android_001"}
    assert "app.launch" in device.capability_ids
    assert device.state["battery"] == 82


def test_a_revoked_phone_is_not_in_the_house():
    _use(FakeGateway(devices=[dict(PHONE, revoked=True)]))

    assert registry.resolve("Galaxy S24") is None


def test_no_gateway_means_no_phones_rather_than_an_error():
    connect_service.set_service_provider(None)
    registry.clear_sources()
    connect_bridge.install()
    registry.reset_cache()

    assert connect_bridge.devices() == []
    assert registry.load() == []


def test_a_source_that_raises_does_not_hide_the_lamp():
    registry.save([Device(id="lamp", name="Desk lamp", adapter="dry_run",
                          capabilities={"switch.on": {}})])
    registry.register_source(lambda: (_ for _ in ()).throw(RuntimeError("gateway gone")))
    registry.reset_cache()

    assert [device.id for device in registry.load()] == ["lamp"]


def test_a_phone_never_lands_in_the_file():
    _use(FakeGateway())

    registry.save([Device(id="lamp", name="Desk lamp", adapter="dry_run",
                          capabilities={"switch.on": {}})])

    written = json.loads(Path(os.environ[registry.ENV_PATH]).read_text(encoding="utf-8"))
    assert [row["id"] for row in written["devices"]] == ["lamp"]


def test_an_owner_entry_wins_over_a_live_device_with_the_same_id():
    registry.save([Device(id="connect:android_001", name="My phone, written down",
                          adapter="dry_run", capabilities={"switch.on": {}})])
    _use(FakeGateway())
    registry.save([Device(id="connect:android_001", name="My phone, written down",
                          adapter="dry_run", capabilities={"switch.on": {}})])

    device = registry.get("connect:android_001")
    assert device.adapter == "dry_run"


# ── The adapter ─────────────────────────────────────────────────────────────

def _adapter():
    from adapters import get_adapter

    return get_adapter("connect")


def _phone_row():
    """The live row for whichever gateway is installed right now."""
    device = registry.get("connect:android_001")
    assert device is not None, "no phone in the house - call _use(FakeGateway()) first"
    return device


def test_the_connect_adapter_is_registered():
    _use(FakeGateway())

    assert _adapter() is not None
    assert _adapter().name == "connect"
    assert _adapter().available is True


def test_the_adapter_says_the_level_in_the_phones_word():
    gateway = FakeGateway()
    _use(gateway)

    result = _adapter().send(_phone_row(), "level.set", {"level": 40})

    assert result["ok"] is True
    assert gateway.calls == [("android_001", "volume_set", {"value": 40})]
    assert result["state"] == {"level": 40}


def test_the_adapter_reports_a_refusal_in_the_phones_own_words():
    gateway = FakeGateway(answer={"success": False, "error_code": "DEVICE_OFFLINE",
                                 "error": "Your Galaxy S24 is currently offline."})
    _use(gateway)

    result = _adapter().send(_phone_row(), "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "DEVICE_OFFLINE"
    assert "offline" in result["detail"]


def test_the_adapter_will_not_pretend_a_phone_can_take_a_screenshot():
    gateway = FakeGateway()
    _use(gateway)

    result = _adapter().send(_phone_row(), "screen.capture", {})

    assert result["ok"] is False
    assert result["error"] == "NOT_EXPRESSIBLE"
    assert gateway.calls == []


def test_no_gateway_is_a_named_failure_not_an_exception():
    connect_service.set_service_provider(None)
    device = Device(id="connect:x", name="A phone", adapter="connect",
                    address={"target": "x"}, capabilities={"switch.on": {}})

    result = _adapter().send(device, "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "GATEWAY_UNAVAILABLE"


# ── The whole road, and the gate on both of its branches ────────────────────

def test_one_address_space_a_phone_answers_to_switch_on(monkeypatch):
    gateway = FakeGateway(answer={"success": True, "data": {"is_on": True}})
    _use(gateway)
    monkeypatch.setattr("core.trust_gate.evaluate", _allow)

    result = registry.execute("Galaxy S24", "switch.on")

    assert result["ok"] is True
    assert result["capability"] == "switch.on"
    assert gateway.calls == [("android_001", "flashlight_on", {})]


def test_the_gate_answers_before_the_phone_is_asked(monkeypatch):
    gateway = FakeGateway()
    _use(gateway)
    monkeypatch.setattr("core.trust_gate.evaluate", _deny)

    result = registry.execute("Galaxy S24", "switch.on")

    assert result["ok"] is False
    assert result["error"] == "REFUSED"
    assert gateway.calls == []


def test_the_old_phone_road_is_not_a_way_around_the_gate(monkeypatch):
    gateway = FakeGateway()
    bc.set_service_provider(lambda: gateway)
    monkeypatch.setattr("core.trust_gate.evaluate", _deny)

    payload = json.loads(bc.connect_execute({
        "device": "Galaxy S24", "action": "launch_app",
        "parameters": {"app_name": "Spotify"},
    }))

    assert payload["success"] is False
    assert payload["error_code"] == "REFUSED"
    assert payload["capability"] == "app.launch"
    assert gateway.calls == []
