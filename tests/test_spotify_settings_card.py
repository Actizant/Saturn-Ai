import json
import os
from pathlib import Path
from actions import spotify_controller as spotify_module


def test_spotify_credentials_helpers(tmp_path, monkeypatch):
    cfg_file = tmp_path / "spotify-config.json"
    monkeypatch.setattr(spotify_module, "SPOTIFY_CONFIG_PATH", cfg_file)

    assert not spotify_module.is_spotify_configured()
    assert spotify_module.get_spotify_config() == {}

    # Save credentials
    ok = spotify_module.save_spotify_credentials("client_123", "secret_456")
    assert ok
    assert spotify_module.is_spotify_configured()

    data = spotify_module.get_spotify_config()
    assert data["clientId"] == "client_123"
    assert data["clientSecret"] == "secret_456"
    assert data["redirectUri"] == "http://127.0.0.1:8888/callback"

    # Add token
    data["refreshToken"] = "rt_xyz"
    cfg_file.write_text(json.dumps(data), encoding="utf-8")
    assert spotify_module.get_spotify_config()["refreshToken"] == "rt_xyz"

    # Change client id, verify old token gets invalidated
    spotify_module.save_spotify_credentials("client_new", "secret_new")
    updated = spotify_module.get_spotify_config()
    assert updated["clientId"] == "client_new"
    assert "refreshToken" not in updated

    # Clear credentials
    assert spotify_module.clear_spotify_credentials()
    assert not cfg_file.exists()
    assert not spotify_module.is_spotify_configured()


def test_system_connectivity_page_spotify_card(tmp_path, monkeypatch):
    cfg_file = tmp_path / "spotify-config.json"
    monkeypatch.setattr(spotify_module, "SPOTIFY_CONFIG_PATH", cfg_file)

    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PyQt6.QtWidgets import QApplication, QLineEdit
    app = QApplication.instance() or QApplication([])

    import ui

    page = ui.SystemConnectivityPage()

    # Verify widgets exist
    assert hasattr(page, "_spotify_client_id")
    assert hasattr(page, "_spotify_client_secret")
    assert hasattr(page, "_spotify_status_lbl")
    assert hasattr(page, "_spotify_save_btn")
    assert hasattr(page, "_spotify_auth_btn")
    assert hasattr(page, "_spotify_test_btn")
    assert hasattr(page, "_spotify_disconnect_btn")

    # Verify initial state: Not configured
    page._update_spotify_status()
    assert "Not Configured" in page._spotify_status_lbl.text()

    # Secret reveal toggle
    page._spotify_client_secret.setText("super_secret")
    assert page._spotify_client_secret.echoMode() == QLineEdit.EchoMode.Password
    page._spotify_reveal_btn.setChecked(True)
    page._toggle_spotify_secret_reveal()
    assert page._spotify_client_secret.echoMode() == QLineEdit.EchoMode.Normal
    page._spotify_reveal_btn.setChecked(False)
    page._toggle_spotify_secret_reveal()
    assert page._spotify_client_secret.echoMode() == QLineEdit.EchoMode.Password

    # Save credentials through helper
    spotify_module.save_spotify_credentials("test_id", "test_sec")
    page._update_spotify_status()
    assert "Credentials Saved" in page._spotify_status_lbl.text()

    # Pre-fill verification
    page._prefill_spotify_credentials()
    assert page._spotify_client_id.text() == "test_id"
    assert page._spotify_client_secret.text() == "test_sec"

    # Authorized state
    cfg = spotify_module.get_spotify_config()
    cfg["refreshToken"] = "mock_refresh"
    cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
    page._update_spotify_status()
    assert "Connected & Authorized" in page._spotify_status_lbl.text()
