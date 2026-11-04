"""
tests/test_space_cleaner.py
===========================
Comprehensive unit tests for Saturn - Storage Sentinel & Space Cleaner Subsystem.
Tests:
- SpaceCleanerEngine singleton & lifecycle
- Multi-drive telemetry and partition enumeration
- Windows Storage Sense registry inspection & quick launch
- Safe directory scanning and culprit discovery
- Multi-category storage scanning and ScanReport serialization
- Multi-drive purge execution & byte reclamation
- IntentDispatcher reflex & semantic routing
- OmniPlanner subsystem integration
- HUD Bridge slots (switchDrive, openStorageSense, purgeCategoriesOnDrive)
"""

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.space_cleaner import (
    SpaceCleanerEngine,
    CleanupCategory,
    ScanReport,
    get_space_cleaner,
)
from core.intent_dispatcher import IntentDispatcher
from core.omni_planner import OmniPlanner, PlanStep


@pytest.fixture
def temp_junk_dir():
    """Creates a temporary sandbox directory with dummy files for safe scanning & purge testing."""
    tmp = tempfile.mkdtemp(prefix="saturn_cleaner_test_")
    test_files = []
    for i in range(5):
        fpath = Path(tmp) / f"temp_artifact_{i}.tmp"
        fpath.write_bytes(b"A" * 1024 * (i + 1))  # 1KB, 2KB, 3KB, 4KB, 5KB = 15KB total
        test_files.append(fpath)

    sub = Path(tmp) / "sub_cache"
    sub.mkdir()
    sub_file = sub / "shader.cache"
    sub_file.write_bytes(b"B" * 2048)  # 2KB

    yield Path(tmp)
    shutil.rmtree(tmp, ignore_errors=True)


def test_space_cleaner_singleton():
    """Verifies SpaceCleanerEngine maintains singleton identity."""
    s1 = get_space_cleaner()
    s2 = SpaceCleanerEngine.get_instance()
    assert s1 is s2
    assert isinstance(s1, SpaceCleanerEngine)


def test_disk_telemetry():
    """Verifies disk telemetry fetches root partition stats."""
    engine = get_space_cleaner()
    tele = engine.get_disk_telemetry()

    assert "drive" in tele
    assert tele["total_bytes"] > 0
    assert tele["free_bytes"] >= 0
    assert tele["total_gb"] > 0
    assert 0 <= tele["percent_used"] <= 100


def test_available_drives_enumeration():
    """Verifies multi-drive enumeration returns mounted partitions."""
    engine = get_space_cleaner()
    drives = engine.get_available_drives()

    assert isinstance(drives, list)
    assert len(drives) >= 1
    primary = drives[0]
    assert "mountpoint" in primary
    assert "total_gb" in primary
    assert "free_gb" in primary
    assert "percent_used" in primary


def test_storage_sense_info():
    """Verifies Windows Storage Sense policy detection."""
    engine = get_space_cleaner()
    info = engine.get_storage_sense_info()

    assert "supported" in info
    assert "os_enabled" in info
    assert "message" in info


def test_safe_scan_dir(temp_junk_dir):
    """Verifies safe recursive directory tree traversal and byte counting."""
    engine = get_space_cleaner()
    size_bytes, file_count, culprits = engine._safe_scan_dir(temp_junk_dir, max_depth=3)

    assert file_count == 6  # 5 in root + 1 in sub_cache
    assert size_bytes == (15 * 1024) + 2048


def test_scan_storage_report():
    """Verifies scan_storage executes across categories and produces ScanReport."""
    engine = get_space_cleaner()

    progress_events = []
    def _prog(msg, pct):
        progress_events.append((msg, pct))

    report = engine.scan_storage(progress_cb=_prog)

    assert isinstance(report, ScanReport)
    assert len(report.categories) >= 5
    assert report.total_bytes > 0
    assert report.free_bytes >= 0
    assert len(progress_events) >= 2
    assert progress_events[-1][1] == 100

    # Verify JSON serialization
    d = report.to_dict()
    assert "total_gb" in d
    assert "free_gb" in d
    assert "reclaimable_mb" in d
    assert "available_drives" in d
    assert "storage_sense_info" in d
    assert len(d["categories"]) >= 5
    for cat in d["categories"]:
        assert "id" in cat
        assert "name" in cat
        assert "size_mb" in cat


def test_purge_selected(temp_junk_dir):
    """Verifies purge_selected safely deletes files in targeted categories and reports metrics."""
    engine = get_space_cleaner()

    mock_cat = CleanupCategory(
        id="mock_purge_target",
        name="Mock Target",
        description="Testing sandbox purge",
        safety_tier="safe",
        preselected=True,
        size_bytes=(15 * 1024) + 2048,
        item_count=6,
        paths=[str(temp_junk_dir)],
        top_culprits=[],
    )

    mock_report = ScanReport(
        drive="C:\\",
        total_bytes=100 * (1024 ** 3),
        used_bytes=50 * (1024 ** 3),
        free_bytes=50 * (1024 ** 3),
        percent_used=50.0,
        total_reclaimable_bytes=(15 * 1024) + 2048,
        safe_reclaimable_bytes=(15 * 1024) + 2048,
        categories=[mock_cat],
        scan_duration_sec=0.1,
        timestamp="2026-10-10 12:00:00",
    )

    engine.last_scan_report = mock_report

    result = engine.purge_selected(["mock_purge_target"])

    assert result["success"] is True
    assert result["deleted_files"] == 6
    assert result["freed_bytes"] == (15 * 1024) + 2048
    assert result["freed_mb"] >= 0.0

    # Verify sandbox files were deleted
    remaining = [f for f in temp_junk_dir.glob("**/*") if f.is_file()]
    assert len(remaining) == 0


def test_intent_dispatcher_space_cleaner_reflex():
    """Verifies IntentDispatcher reflex rules route disk cleaning utterances to space_cleaner."""
    dispatcher = IntentDispatcher()

    utterances = [
        "clean disk",
        "clean up disk space",
        "free up space on my computer",
        "disk cleanup",
        "storage sentinel",
        "clean up temporary junk files",
        "free some storage",
        "open space cleaner",
    ]

    for utt in utterances:
        res = dispatcher.resolve(utt)
        assert res.intent == "space_cleaner", f"Failed to route '{utt}', got '{res.intent}'"
        assert res.confidence >= 0.70
        assert "Storage Sentinel" in res.voice_ack or "storage" in res.voice_ack.lower() or "disk" in res.voice_ack.lower()


def test_omni_planner_space_cleaner_step():
    """Verifies OmniPlanner successfully routes and executes a space_cleaner plan step."""
    planner = OmniPlanner()
    step = PlanStep(
        step_id=1,
        subsystem="space_cleaner",
        action="purge_safe",
        description="Audit storage pressure and volatile clutter",
        parameters={"categories": ["system_temp"]},
    )

    with patch.object(SpaceCleanerEngine, "purge_selected", return_value={
        "success": True,
        "freed_mb": 150.5,
        "freed_gb": 0.15,
        "deleted_files": 42,
        "message": "Successfully freed 150.5 MB across 42 files.",
    }):
        step_res = planner._execute_step(step)
        assert step_res.success is True
        assert step_res.subsystem == "space_cleaner"


def test_hud_assets_and_bridge():
    """Verifies HUD template index.html exists and SpaceCleanerBridge initializes cleanly."""
    from core.space_cleaner_hud import HUD_HTML_FILE, SpaceCleanerBridge

    assert HUD_HTML_FILE.exists()
    content = HUD_HTML_FILE.read_text(encoding="utf-8")
    assert "Saturn - Storage Sentinel" in content
    assert "drive-dropdown-menu" in content or "drive-select" in content
    assert "storage-sense" in content.lower()
    assert "spaceCleanerBridge" in content

    # Bridge slots check
    bridge = SpaceCleanerBridge()
    assert hasattr(bridge, "triggerScan")
    assert hasattr(bridge, "switchDrive")
    assert hasattr(bridge, "openStorageSense")
    assert hasattr(bridge, "purgeCategories")
    assert hasattr(bridge, "purgeCategoriesOnDrive")
    assert hasattr(bridge, "closeWindow")
