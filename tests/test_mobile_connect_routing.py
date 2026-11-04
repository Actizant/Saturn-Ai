import json
from unittest.mock import MagicMock, patch

import pytest
from core.intent_dispatcher import _looks_like_code_request, _looks_like_website_request


def test_looks_like_code_request_excludes_mobile_and_open_verbs():
    assert not _looks_like_code_request("open whatsapp on my phone")
    assert not _looks_like_code_request("open whatsapp")
    assert not _looks_like_code_request("launch telegram on android")
    assert not _looks_like_code_request("start calculator")
    assert not _looks_like_code_request("open settings")
    assert not _looks_like_code_request("turn on flashlight on phone")
    assert not _looks_like_code_request("ring my phone")


def test_looks_like_code_request_identifies_actual_development():
    assert _looks_like_code_request("build a react app")
    assert _looks_like_code_request("create a python script to parse logs")
    assert _looks_like_code_request("code a calculator in python")
    assert _looks_like_code_request("develop a snake game")
    assert _looks_like_code_request("write a fast api backend")


def test_looks_like_website_request_excludes_mobile_and_browser_open():
    assert not _looks_like_website_request("open whatsapp on my phone")
    assert not _looks_like_website_request("open website google.com")
    assert not _looks_like_website_request("go to website youtube.com")
    assert not _looks_like_website_request("visit site github.com")


def test_looks_like_website_request_identifies_website_creation():
    assert _looks_like_website_request("build a portfolio website")
    assert _looks_like_website_request("create a landing page for my store")
    assert _looks_like_website_request("design a website for my business")


def test_saturn_connect_command_routing_no_device():
    from main import SaturnLive

    class DummyController:
        _handle_saturn_connect_command = SaturnLive._handle_saturn_connect_command
        _extract_launch_app_name = SaturnLive._extract_launch_app_name
        def __init__(self):
            self.ui = MagicMock()
            self.ui.muted = False
            self.spoken_messages = []
        def speak(self, text):
            self.spoken_messages.append(text)

    ctrl = DummyController()

    with patch("main.connect_list_devices", return_value=json.dumps({"success": True, "devices": []})):
        handled = ctrl._handle_saturn_connect_command("open whatsapp on my phone")
        assert handled is True
        assert any("no phone is currently connected" in msg.lower() for msg in ctrl.spoken_messages)


def test_saturn_connect_command_routing_with_connected_phone():
    from main import SaturnLive

    class DummyController:
        _handle_saturn_connect_command = SaturnLive._handle_saturn_connect_command
        _extract_launch_app_name = SaturnLive._extract_launch_app_name
        def __init__(self):
            self.ui = MagicMock()
            self.ui.muted = False
            self.spoken_messages = []
        def speak(self, text):
            self.spoken_messages.append(text)

    ctrl = DummyController()
    fake_devices = [
        {"device_id": "phone_123", "name": "Pixel 8", "platform": "android", "online": True}
    ]

    with patch("main.connect_list_devices", return_value=json.dumps({"success": True, "devices": fake_devices})), \
         patch("main.connect_execute", return_value=json.dumps({"success": True, "detail": "Launched WhatsApp."})) as mock_exec:
        handled = ctrl._handle_saturn_connect_command("open whatsapp on my phone")
        assert handled is True
        mock_exec.assert_called_once()
        call_params = mock_exec.call_args[1]["parameters"]
        assert call_params["device"] == "phone_123"
        assert call_params["action"] == "launch_app"
        assert call_params["parameters"]["app_name"] == "whatsapp"


def test_saturn_connect_ignores_informational_phone_questions():
    from main import SaturnLive

    class DummyController:
        _handle_saturn_connect_command = SaturnLive._handle_saturn_connect_command
        _extract_launch_app_name = SaturnLive._extract_launch_app_name
        def __init__(self):
            self.ui = MagicMock()
            self.spoken_messages = []
        def speak(self, text):
            self.spoken_messages.append(text)

    ctrl = DummyController()
    handled = ctrl._handle_saturn_connect_command("what is a smartphone?")
    assert handled is False
