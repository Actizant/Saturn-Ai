import json

from actions import spotify_controller as spotify_module
from actions.circuit_assembler import PRESET_ULTRASONIC_ARDUINO_UNO
from actions.geospatial_globe import calculate_great_circle_route, geocode_location
from core.circuit_hud import generate_circuit_html


def test_generated_skill_module_is_not_imported_during_registry_startup(tmp_path, monkeypatch):
    import core.dynamic_registry as registry

    features = tmp_path / "features"
    skills = tmp_path / "skills"
    features.mkdir()
    skills.mkdir()
    marker = tmp_path / "imported.txt"
    metadata = {
        "name": "deferred_probe",
        "description": "Deferred-load test feature",
        "author": "Project Saturn Autonomous Self-Evolution Engine",
        "parameters": {"type": "OBJECT", "properties": {}},
    }
    source = (
        f"FEATURE_METADATA = {metadata!r}\n"
        f"open({str(marker)!r}, 'w').write('imported')\n"
        "def execute(**kwargs): return kwargs.get('value', 'ran')\n"
    )
    (features / "deferred_probe.py").write_text(source, encoding="utf-8")

    monkeypatch.setattr(registry, "FEATURES_DIR", features)
    monkeypatch.setattr(registry, "APPDATA_SKILLS_DIR", skills)
    monkeypatch.setattr(registry.DynamicToolRegistry, "_skills", {})
    monkeypatch.setattr(registry.DynamicToolRegistry, "_initialized", False)

    registry.DynamicToolRegistry.initialize()
    assert not marker.exists()
    assert registry.DynamicToolRegistry.execute_sync("deferred_probe", {"value": "ran"}) == "ran"
    assert marker.exists()


def test_manifest_marks_paired_feature_as_generated(tmp_path, monkeypatch):
    import core.dynamic_registry as registry

    features = tmp_path / "features"
    skills = tmp_path / "skills"
    features.mkdir()
    skills.mkdir()
    module = features / "paired_probe.py"
    module.write_text(
        "FEATURE_METADATA = {'name': 'paired_probe', 'author': 'native'}\n"
        "def execute(**kwargs): return 'ran'\n",
        encoding="utf-8",
    )
    package = features / "paired_probe"
    package.mkdir()
    manifest = {
        "name": "paired_probe",
        "description": "Generated feature",
        "author": "Project Saturn Autonomous Self-Evolution Engine",
        "parameters": {"type": "OBJECT", "properties": {}},
    }
    (package / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (package / "skill.py").write_text("def execute(**kwargs): return 'ran'\n", encoding="utf-8")

    monkeypatch.setattr(registry, "FEATURES_DIR", features)
    monkeypatch.setattr(registry, "APPDATA_SKILLS_DIR", skills)
    monkeypatch.setattr(registry.DynamicToolRegistry, "_skills", {})
    monkeypatch.setattr(registry.DynamicToolRegistry, "_initialized", False)

    registry.DynamicToolRegistry.initialize()
    skill = registry.DynamicToolRegistry.get_skill("paired_probe")
    assert skill.manifest["author"] == manifest["author"]


def test_geospatial_route_has_expected_waypoints():
    origin = geocode_location("Mumbai")
    destination = geocode_location("London")
    route = calculate_great_circle_route(origin[0], origin[1], destination[0], destination[1])

    assert origin[2].startswith("Mumbai")
    assert destination[2].startswith("London")
    assert 7000 < route["distance_km"] < 7500
    assert len(route["waypoints"]) == 51


def test_circuit_hud_renders_upstream_preset():
    html = generate_circuit_html(PRESET_ULTRASONIC_ARDUINO_UNO)

    assert "HC-SR04" in html
    assert "Trigger (Pin 9)" in html
    assert PRESET_ULTRASONIC_ARDUINO_UNO["warnings"][0] in html


def test_circuit_hud_escapes_model_provided_markup():
    html = generate_circuit_html({
        "title": "</script><img src=x onerror=alert(1)>",
        "components": [{
            "id": "x",
            "name": "<img src=x onerror=alert(2)>",
            "subtitle": "test",
            "left_pins": [{"name": "<svg onload=alert(3)>", "color": "url(javascript:alert(4))"}],
            "right_pins": [],
        }],
        "wires": [],
    })

    assert "<img src=x onerror" not in html
    assert "&lt;img src=x onerror" in html
    assert "\\u003c/script\\u003e" in html
    assert 'style="background: url(javascript' not in html


def test_spotify_mcp_actions_and_legacy_fallback(monkeypatch):
    real_mcp_action = spotify_module._spotify_mcp_action
    monkeypatch.setattr(spotify_module, "is_spotify_configured", lambda: True)
    monkeypatch.setattr(
        spotify_module,
        "_spotify_mcp_action",
        lambda params: f"mcp:{params['action']}",
    )
    assert spotify_module.spotify_controller({"action": "get_playlists"}) == "mcp:get_playlists"

    monkeypatch.setattr(spotify_module, "is_spotify_configured", lambda: False)
    monkeypatch.setattr(spotify_module, "_spotify_mcp_action", real_mcp_action)
    message = spotify_module.spotify_controller({"action": "get_now_playing"})
    assert "spotify-config.json" in message