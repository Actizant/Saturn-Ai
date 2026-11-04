import os
from pathlib import Path
import pytest

def test_web_background_assets_all_local():
    base_dir = Path(__file__).resolve().parent.parent
    bg_dir = base_dir / "assets" / "web_background"
    html_file = bg_dir / "index.html"

    assert html_file.exists(), "assets/web_background/index.html must exist"

    required_scripts = [
        "three.min.js",
        "OrbitControls.js",
        "EffectComposer.js",
        "RenderPass.js",
        "ShaderPass.js",
        "CopyShader.js",
        "LuminosityHighPassShader.js",
        "UnrealBloomPass.js",
    ]

    for script in required_scripts:
        script_path = bg_dir / script
        assert script_path.exists(), f"{script} must exist locally in assets/web_background"
        assert script_path.stat().st_size > 100, f"{script} must not be empty"

def test_web_background_instant_boot_no_document_write():
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "web_background" / "index.html"
    content = html_file.read_text(encoding="utf-8")

    # Verify no blocking document.write calls
    assert "document.write" not in content, "index.html should not contain blocking document.write"
    # Verify no external CDN fallbacks that cause stalls when offline
    assert "cdnjs.cloudflare.com" not in content
    assert "cdn.jsdelivr.net" not in content

    # Verify robust startup boot function
    assert "function boot()" in content
    assert "document.readyState" in content

    # Verify safe canvas sizing
    assert "Math.max" in content

def test_opengl_context_sharing_flag():
    import ui
    from PyQt6.QtCore import QCoreApplication, Qt
    assert QCoreApplication.testAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)


def test_loaded_orb_page_is_self_contained_and_bridged():
    """saturn_orb.html is what BackgroundWidget now loads in place of the old
    three.js reactor. It must boot offline with no external assets and expose
    every entry point the Qt side drives it through, or the shell goes blank."""
    base_dir = Path(__file__).resolve().parent.parent
    html_file = base_dir / "assets" / "web_background" / "saturn_orb.html"
    assert html_file.exists(), "assets/web_background/saturn_orb.html must exist"
    content = html_file.read_text(encoding="utf-8")

    # Self-contained: no CDN, no external <script src> / <link href> to stall on.
    for offline in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com",
                    "fonts.googleapis.com"):
        assert offline not in content, f"{offline} would stall the boot offline"
    assert "<script src=" not in content, "the orb page may not load remote scripts"
    assert "document.write" not in content

    # The bridge the app calls (ui.py runJavaScript sites) must all be defined.
    for entry in ("setSaturnState", "setAudioLevel", "setPointerNorm",
                  "pauseSaturnBackground", "resumeSaturnBackground",
                  "setSaturnRenderBudget"):
        assert f"window.{entry}" in content, f"the page must expose window.{entry}"

    # The seven shape-encoded states mirror class C.S_* so web and Qt cannot drift.
    for state in ("idle", "listening", "thinking", "speaking", "muted", "offline", "error"):
        assert f"{state}:" in content, f"the orb must carry a {state} state"

