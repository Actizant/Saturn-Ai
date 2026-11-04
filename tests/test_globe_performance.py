import os
from pathlib import Path
import pytest
from core.globe_window import GlobeBridge

def test_globe_assets_exist():
    base_dir = Path(__file__).resolve().parent.parent
    assets_dir = base_dir / "assets" / "globe"
    html_file = assets_dir / "index.html"
    tex_file = assets_dir / "earth-night-2k.jpg"

    assert html_file.exists(), "assets/globe/index.html must exist"
    assert tex_file.exists(), "assets/globe/earth-night-2k.jpg must exist"
    assert tex_file.stat().st_size > 50000, "Optimized texture should be valid size"

def test_globe_html_performance_optimizations():
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "globe" / "index.html"
    content = html_file.read_text(encoding="utf-8")

    # Verify texture loading
    assert "earth-night-2k.jpg" in content
    # Verify depthWrite false on atmosphere
    assert "depthWrite: false" in content
    # Verify dragging detection
    assert "isUserDragging" in content
    # Verify damping factor optimization
    assert "dampingFactor = 0.14" in content
    # Verify mediump precision for smooth rendering
    assert 'precision: "mediump"' in content
    # Verify GPU transform promotion in CSS
    assert "translateZ(0)" in content

def test_chromium_gpu_flags():
    """The backdrop must present in step with the display.

    This test used to assert the opposite: --disable-gpu-vsync and
    --disable-frame-rate-limit counted as "performance" flags. Measured on a
    laptop they cost the app its smoothness - the WebGL backdrop rendered as
    fast as it could, unsynchronised from the 60Hz panel, DWM threw most of
    those frames away and the screen showed ~22-28 distinct frames a second
    with stalls over 100ms. Keeping them out is the regression test, so the
    string is read from main.py rather than from the environment (which
    depends on which module happened to be imported first).
    """
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    lines = [ln for ln in src.splitlines()
             if "--use-angle=d3d11" in ln and "--enable-gpu-rasterization" in ln]
    assert lines, "main.py must set the Chromium GPU flags on one line"
    flags = lines[0]
    for needed in ("--enable-gpu-rasterization", "--enable-zero-copy",
                   "--ignore-gpu-blocklist", "--use-angle=d3d11"):
        assert needed in flags
    for harmful in ("--disable-gpu-vsync", "--disable-frame-rate-limit",
                    "--disable-gpu-driver-bug-workarounds"):
        assert harmful not in flags
    assert "fmt.setSwapInterval(1)" in src, "Qt surfaces must swap on vsync"

def test_globe_bridge_signals():
    bridge = GlobeBridge()
    received = []

    bridge.close_requested.connect(lambda: received.append("closed"))
    bridge.view_updated.connect(lambda data: received.append(data))

    bridge.closeGlobe()
    assert "closed" in received

    bridge.reportView('{"centerLat": 20.5, "centerLon": 78.9}')
    assert any(isinstance(x, dict) and x.get("centerLat") == 20.5 for x in received)

def test_globe_low_end_device_optimizations():
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "globe" / "index.html"
    content = html_file.read_text(encoding="utf-8")

    # Verify WebGL hardware fallback support for low-end GPUs
    assert "failIfMajorPerformanceCaveat: false" in content
    # Verify mipmap generation is disabled for fast upload without GPU stalls
    assert "tex.generateMipmaps = false" in content
    # Verify zero-allocation objects for GC smoothness
    assert "_camDirVec" in content
    assert "_pulseVec" in content
    # Verify lifecycle pause/resume
    assert "pauseGlobe" in content
    assert "resumeGlobe" in content
    assert "pause: () => pauseGlobe()" in content
    assert "resume: () => resumeGlobe()" in content
    # Verify heavy backdrop-filter is completely eliminated to protect Intel HD graphics
    assert "backdrop-filter" not in content
    # Verify pixel ratio is capped at 1.0
    assert "Math.min(window.devicePixelRatio || 1, 1.0)" in content
    # Verify responsive rotation speed & debounce
    assert "rotateSpeed = 1.25" in content
    assert "autoRotateSpeed = 2.0" in content
    assert "dragEndTimeout" in content
    assert "_flyStepQ" in content
    assert "status-fps-val" in content
    assert "lastFrameTime" in content

def test_globe_window_lifecycle_events():
    from core.globe_window import GlobeWindow
    assert hasattr(GlobeWindow, "showEvent")
    assert hasattr(GlobeWindow, "hideEvent")
