"""The transports. Nothing here should ever move something by accident.

Each adapter is tested for the two things that matter: the request it builds,
and what it says when it cannot do the thing. An adapter that reports success
it did not achieve is worse than one that refuses.
"""

from __future__ import annotations

import pytest

import adapters
from adapters import adb as adb_adapter
from adapters import dry_run
from adapters import http as http_adapter
from adapters import mqtt as mqtt_adapter
from adapters import render
from core.capability_registry import Device


# ── Template rendering ─────────────────────────────────────────────────────

def test_a_placeholder_is_replaced():
    assert render("/device/{device}/on", {"device": "lamp"}) == "/device/lamp/on"


def test_a_lone_placeholder_keeps_the_type():
    assert render("{level}", {"level": 40}) == 40
    assert render("{level}", {"level": True}) is True


def test_nested_templates_are_rendered():
    payload = {"level": "{level}", "tags": ["{device}"]}
    assert render(payload, {"level": 40, "device": "lamp"}) == {
        "level": 40, "tags": ["lamp"]}


def test_an_unknown_placeholder_is_left_visible():
    """A typo should show up in the request, not silently become empty."""
    assert render("/{devcie}/on", {"device": "lamp"}) == "/{devcie}/on"


def test_a_non_string_template_is_returned_untouched():
    assert render(7, {"x": 1}) == 7
    assert render(None, {}) is None


# ── The registry of transports ─────────────────────────────────────────────

def test_the_four_transports_are_registered():
    names = {adapter.name for adapter in adapters.adapters()}
    assert {"http", "mqtt", "adb", "dry_run"} <= names


def test_an_adapter_is_found_case_insensitively():
    assert adapters.get_adapter("HTTP").name == "http"
    assert adapters.get_adapter("dry-run").name == "dry_run"


def test_an_unknown_adapter_is_none():
    assert adapters.get_adapter("zigbee") is None


def test_the_transport_list_is_describable():
    text = adapters.describe()
    assert "http:" in text and "mqtt:" in text


def test_a_missing_optional_library_is_reported_not_hidden(monkeypatch):
    monkeypatch.setattr(mqtt_adapter, "_publish", None)
    adapter = mqtt_adapter.MqttAdapter()

    assert adapter.available is False
    result = adapter.send(Device(id="x", name="X", adapter="mqtt"), "switch.on", {})
    assert result["ok"] is False
    assert result["error"] == "ADAPTER_UNAVAILABLE"


# ── HTTP ───────────────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text or "{}"

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeRequests:
    def __init__(self, response=None):
        self.calls = []
        self.response = response or FakeResponse()

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def http_device(**mapping):
    # ``switch_on=`` reads better in a test than a dict literal; the real key is dotted.
    caps = {key.replace("_", "."): value for key, value in mapping.items()}
    return Device(
        id="tv", name="Smart TV", adapter="http",
        address={"base_url": "http://192.168.1.20:8060"},
        capabilities=caps or {"switch.on": {"method": "POST", "path": "/keypress/Power"}},
    )


def test_http_builds_the_request_from_the_mapping(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(http_adapter, "requests", fake)

    result = http_adapter.HttpAdapter().send(
        http_device(), "switch.on", {})

    assert result["ok"] is True
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://192.168.1.20:8060/keypress/Power"
    assert call["verify"] is True


def test_http_reads_by_default_for_read_verbs(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(http_adapter, "requests", fake)
    device = http_device(job_status={"path": "/api/job"})

    http_adapter.HttpAdapter().send(device, "job.status", {})

    assert fake.calls[0]["method"] == "GET"


def test_http_substitutes_the_arguments_into_the_body(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(http_adapter, "requests", fake)
    device = http_device(level_set={"path": "/level", "body": {"brightness": "{level}"}})

    http_adapter.HttpAdapter().send(device, "level.set", {"level": 40})

    assert fake.calls[0]["json"] == {"brightness": 40}


def test_http_reports_a_non_2xx_as_a_failure(monkeypatch):
    monkeypatch.setattr(http_adapter, "requests",
                        FakeRequests(FakeResponse(status=500, text="boom")))
    result = http_adapter.HttpAdapter().send(http_device(), "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "HTTP_STATUS"
    assert result["status"] == 500


def test_http_turns_a_connection_error_into_a_readable_refusal(monkeypatch):
    class Dead:
        def request(self, *args, **kwargs):
            raise OSError("connection refused")

    monkeypatch.setattr(http_adapter, "requests", Dead())
    result = http_adapter.HttpAdapter().send(http_device(), "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "UNREACHABLE"
    assert "did not answer" in result["detail"]


def test_http_refuses_a_device_without_an_address(monkeypatch):
    monkeypatch.setattr(http_adapter, "requests", FakeRequests())
    device = Device(id="x", name="Nowhere", adapter="http", address={})

    result = http_adapter.HttpAdapter().send(device, "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "NO_ADDRESS"


def test_http_tracks_state_only_when_asked(monkeypatch):
    payload = {"on": True, "level": 40, "name": "lamp"}
    monkeypatch.setattr(http_adapter, "requests",
                        FakeRequests(FakeResponse(payload=payload)))

    tracked = http_device(switch_on={"path": "/on", "track_state": True})
    result = http_adapter.HttpAdapter().send(tracked, "switch.on", {})
    assert result["state"] == payload

    plain = http_device(switch_on={"path": "/on"})
    result = http_adapter.HttpAdapter().send(plain, "switch.on", {})
    assert "state" not in result


# ── MQTT ───────────────────────────────────────────────────────────────────

class FakePublish:
    def __init__(self):
        self.sent = []

    def single(self, topic, **kwargs):
        self.sent.append({"topic": topic, **kwargs})


def mqtt_device(**mapping):
    caps = {key.replace("_", "."): value for key, value in mapping.items()}
    return Device(
        id="lamp", name="Desk lamp", adapter="mqtt",
        address={"broker": "192.168.1.9", "port": 1883, "username": "saturn",
                 "password": "x"},
        capabilities=caps or {"switch.on": {"topic": "home/lamp/set", "payload": "ON"}},
    )


def test_mqtt_publishes_the_mapped_payload(monkeypatch):
    fake = FakePublish()
    monkeypatch.setattr(mqtt_adapter, "_publish", fake)

    result = mqtt_adapter.MqttAdapter().send(mqtt_device(), "switch.on", {})

    assert result["ok"] is True
    sent = fake.sent[0]
    assert sent["topic"] == "home/lamp/set"
    assert sent["payload"] == "ON"
    assert sent["hostname"] == "192.168.1.9"
    assert sent["auth"] == {"username": "saturn", "password": "x"}


def test_mqtt_renders_json_payloads_with_types(monkeypatch):
    fake = FakePublish()
    monkeypatch.setattr(mqtt_adapter, "_publish", fake)
    device = mqtt_device(level_set={"topic": "home/lamp/set",
                                    "payload": {"brightness": "{level}"}})

    mqtt_adapter.MqttAdapter().send(device, "level.set", {"level": 40})

    assert '"brightness": 40' in fake.sent[0]["payload"]


def test_mqtt_sends_the_argument_when_no_payload_is_mapped(monkeypatch):
    fake = FakePublish()
    monkeypatch.setattr(mqtt_adapter, "_publish", fake)
    device = mqtt_device(level_set={"topic": "home/lamp/set"})

    mqtt_adapter.MqttAdapter().send(device, "level.set", {"level": 40})

    assert fake.sent[0]["payload"] == "40"


def test_mqtt_refuses_a_device_without_a_topic(monkeypatch):
    monkeypatch.setattr(mqtt_adapter, "_publish", FakePublish())
    device = mqtt_device(level_set={})

    result = mqtt_adapter.MqttAdapter().send(device, "level.set", {})

    assert result["ok"] is False
    assert result["error"] == "NO_TOPIC"


def test_mqtt_refuses_a_device_without_a_broker(monkeypatch):
    monkeypatch.setattr(mqtt_adapter, "_publish", FakePublish())
    device = Device(id="lamp", name="Lamp", adapter="mqtt",
                    capabilities={"switch.on": {"topic": "t"}})

    result = mqtt_adapter.MqttAdapter().send(device, "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "NO_ADDRESS"


def test_mqtt_turns_a_broker_error_into_a_readable_refusal(monkeypatch):
    class Dead:
        def single(self, *args, **kwargs):
            raise OSError("no route to host")

    monkeypatch.setattr(mqtt_adapter, "_publish", Dead())
    result = mqtt_adapter.MqttAdapter().send(mqtt_device(), "switch.on", {})

    assert result["ok"] is False
    assert result["error"] == "UNREACHABLE"


# ── ADB ────────────────────────────────────────────────────────────────────

def adb_device():
    return Device(id="phone", name="Phone", adapter="adb",
                  address={"serial": "192.168.1.31:5555"},
                  capabilities={"app.launch": {}, "key.press": {}, "text.input": {},
                                "lock.unlock": {}})


def test_adb_escapes_spaces_and_shell_characters():
    assert adb_adapter.escape_text("hello world") == "hello%sworld"
    assert adb_adapter.escape_text("a;rm -rf") == "a\\;rm%s-rf"
    assert adb_adapter.escape_text('say "hi"') == 'say%s\\"hi\\"'


def test_adb_launches_an_app_by_package(monkeypatch):
    calls = []

    def fake_run(self, device, args, timeout=0):
        calls.append(args)
        return 0, "Events injected", ""

    monkeypatch.setattr(adb_adapter.AdbAdapter, "_run", fake_run)
    result = adb_adapter.AdbAdapter().send(
        adb_device(), "app.launch", {"package": "com.google.android.youtube"})

    assert result["ok"] is True
    assert calls[0][:4] == ["shell", "monkey", "-p", "com.google.android.youtube"]


def test_adb_maps_friendly_key_names(monkeypatch):
    calls = []
    monkeypatch.setattr(adb_adapter.AdbAdapter, "_run",
                        lambda self, d, args, timeout=0: (calls.append(args), (0, "", ""))[1])

    adb_adapter.AdbAdapter().send(adb_device(), "key.press", {"key": "home"})
    adb_adapter.AdbAdapter().send(adb_device(), "key.press", {"key": "KEYCODE_BACK"})

    assert calls[0] == ["shell", "input", "keyevent", "KEYCODE_HOME"]
    assert calls[1] == ["shell", "input", "keyevent", "KEYCODE_BACK"]


def test_adb_refuses_to_unlock_a_phone_and_says_why():
    result = adb_adapter.AdbAdapter().send(adb_device(), "lock.unlock", {})

    assert result["ok"] is False
    assert result["error"] == "UNSUPPORTED"
    assert "PIN" in result["detail"]


def test_adb_refuses_a_capability_it_has_no_mapping_for():
    result = adb_adapter.AdbAdapter().send(adb_device(), "job.start", {})

    assert result["ok"] is False
    assert "no ADB mapping" in result["detail"]


def test_adb_reports_a_failed_command_with_its_stderr(monkeypatch):
    monkeypatch.setattr(adb_adapter.AdbAdapter, "_run",
                        lambda self, d, args, timeout=0: (1, "", "error: no devices"))

    result = adb_adapter.AdbAdapter().send(adb_device(), "key.press", {"key": "home"})

    assert result["ok"] is False
    assert "no devices" in result["detail"]


def test_adb_says_so_when_the_binary_is_missing(monkeypatch):
    monkeypatch.setattr(adb_adapter, "_find_adb", lambda: "")
    result = adb_adapter.AdbAdapter().send(adb_device(), "key.press", {"key": "home"})

    assert result["ok"] is False
    assert result["error"] == "ADAPTER_UNAVAILABLE"


def test_adb_missing_argument_is_a_bad_request(monkeypatch):
    monkeypatch.setattr(adb_adapter, "_find_adb", lambda: "adb")
    result = adb_adapter.AdbAdapter().send(adb_device(), "app.launch", {})

    assert result["ok"] is False
    assert result["error"] == "BAD_REQUEST"


def test_adb_screenshot_writes_a_file(monkeypatch, tmp_path):
    class Done:
        returncode = 0
        stdout = b"\x89PNG\r\n"
        stderr = b""

    monkeypatch.setattr(adb_adapter, "_find_adb", lambda: "adb")
    monkeypatch.setattr(adb_adapter.subprocess, "run", lambda *a, **k: Done())
    monkeypatch.setattr("core.user_paths.get_user_data_dir", lambda: tmp_path)

    result = adb_adapter.AdbAdapter().send(adb_device(), "screen.capture", {})

    assert result["ok"] is True
    assert result["path"].endswith(".png")
    assert (tmp_path / "screenshots").exists()


# ── Dry run ────────────────────────────────────────────────────────────────

def test_dry_run_records_what_it_would_have_sent():
    device = Device(id="lamp", name="Desk lamp", adapter="dry_run",
                    capabilities={"switch.on": {}})

    result = dry_run.DryRunAdapter().send(device, "switch.on", {})

    assert result["ok"] is True and result["dry_run"] is True
    assert dry_run.recent(1)[0]["device"] == "lamp"


def test_dry_run_never_claims_a_real_effect():
    """It reports the shape of the request, and the detail says so."""
    device = Device(id="printer", name="Printer", adapter="dry_run",
                    capabilities={"job.start": {}})

    result = dry_run.DryRunAdapter().send(device, "job.start", {"source": "cube.stl"})

    assert result["detail"].startswith("[dry run]")
    assert "is_on" not in result["state"]


def test_dry_run_tracks_only_the_state_it_can_know():
    device = Device(id="lamp", name="Lamp", adapter="dry_run",
                    capabilities={"switch.on": {}, "level.set": {}})

    assert dry_run.DryRunAdapter().send(device, "switch.on", {})["state"] == {"is_on": True}
    assert dry_run.DryRunAdapter().send(device, "level.set", {"level": 30})["state"] == {"level": 30}
