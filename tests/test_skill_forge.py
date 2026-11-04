from unittest.mock import patch

from core.dynamic_registry import DynamicToolRegistry
from core.skill_crucible import SkillCrucible
from core.skill_forge import SkillForge


def test_forged_feature_registers_and_executes(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized
    payload = {
        "success": True,
        "manifest": {
            "name": "forge_smoke_test",
            "description": "Forge smoke test",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return "forge smoke test passed"\n',
        "test_cases": [{"input": {}}],
    }

    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", tmp_path / "vault"),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=payload),
            patch.object(SkillCrucible, "resolve_dependencies", return_value=(True, "No dependencies.")),
        ):
            result = SkillForge.forge_skill("smoke test feature", "forge_smoke_test")

            assert result["success"] is True, result
            assert DynamicToolRegistry.execute_sync("forge_smoke_test", {}) == "forge smoke test passed"
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized


def test_crucible_detects_error_dictionary_in_sandbox():
    broken_code = 'def execute(**kwargs):\n    return {"error": "SSL handshake failed"}\n'
    ok, msg, telemetry = SkillCrucible.run_sandbox_test(broken_code, [{"input": {}}])
    assert ok is False
    assert "SSL handshake failed" in msg

    working_code = 'def execute(**kwargs):\n    return {"title": "Success", "summary": "Rendered"}\n'
    ok2, msg2, telemetry2 = SkillCrucible.run_sandbox_test(working_code, [{"input": {}}])
    assert ok2 is True


def test_forge_repairs_skill_when_sandbox_returns_error_dict(tmp_path):
    original_skills = DynamicToolRegistry._skills.copy()
    original_initialized = DynamicToolRegistry._initialized

    initial_payload = {
        "success": True,
        "manifest": {
            "name": "repair_smoke_test",
            "description": "Repair test",
            "parameters": {"type": "OBJECT", "properties": {}},
            "active": True,
        },
        "code": 'def execute(**kwargs):\n    return {"error": "Initial SSL failure"}\n',
        "test_cases": [{"input": {}}],
    }

    repaired_code = 'def execute(**kwargs):\n    return {"title": "Repaired Deliverable", "summary": "Working"}\n'

    try:
        with (
            patch("core.dynamic_registry.FEATURES_DIR", tmp_path / "features"),
            patch("core.dynamic_registry.APPDATA_SKILLS_DIR", tmp_path / "vault"),
            patch.object(SkillForge, "_call_llm_synthesizer", return_value=initial_payload),
            patch.object(SkillCrucible, "resolve_dependencies", return_value=(True, "No dependencies.")),
            patch.object(SkillForge, "_repair_code", return_value={"success": True, "code": repaired_code}) as mock_repair,
        ):
            result = SkillForge.forge_skill("repair smoke test", "repair_smoke_test")

            assert mock_repair.called
            assert result["success"] is True, result
            res = DynamicToolRegistry.execute_sync("repair_smoke_test", {})
            assert isinstance(res, dict) and res.get("title") == "Repaired Deliverable"
    finally:
        DynamicToolRegistry._skills = original_skills
        DynamicToolRegistry._initialized = original_initialized