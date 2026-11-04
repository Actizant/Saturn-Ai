"""The Saturn Cyberpunk design contract: one token set, no anti-brand machinery.

The file is named for the Aurum era it grew out of, but the brand it now pins is
the cyberpunk rebrand: a magenta accent (#FF2BD6) with an acid-yellow secondary
on near-black purple surfaces, replacing the gold that came before it. The same
contract holds as when the shell went gold - ui.py must not monkey-patch
setStyleSheet to rewrite accents, must not hue-rotate the orb page, and must not
hardcode a palette any surface can outrank. The surfaces read class C tokens
directly, so a theme change is a re-polish rather than a find-and-replace.

These tests pin the cyberpunk values so the retired gold and the old cyan cannot
creep back in through a new call site. The layout and paint contracts (the orb
geometry, the workspace shape, the token-fed painters) are kept unchanged - only
the colour expectations moved with the brand.
"""
import os
import re
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

ROOT = Path(__file__).resolve().parent.parent
UI_SOURCE = (ROOT / "ui.py").read_text(encoding="utf-8")
PAGE_SOURCE = (ROOT / "assets" / "web_background" / "index.html").read_text(encoding="utf-8")
SMART_HOME_SOURCE = (ROOT / "smart_home_page_new.py").read_text(encoding="utf-8")

# The cyan the old machinery forced everywhere, and the icy blues the pre-Aurum
# reactor page was built from. Neither belongs in the gold design.
CYAN_TOKENS = ("#00e5ff", "#00E5FF", "0, 229, 255", "#80ffff")
ICY_PAGE_HEXES = ("#bdeaf7", "#c8edff", "#adc9db", "#081527", "#439cae", "#020306")
# The rest of the palette the smart-home page froze for itself, which is how it
# stayed blue on a gold brand for an entire redesign: plain white ink and the
# green/amber/red status lights that came with it.
FROZEN_PAGE_HEXES = ("#ffffff", "#FFFFFF", "rgba(255, 255, 255", "#35ff75",
                     "#ffcc00", "#ff6b6b", "#7bd3f7", "#00e5ff")


@pytest.fixture(scope="module")
def ui():
    import ui as module
    return module


@pytest.fixture()
def pristine_tokens(ui):
    """load_theme() writes onto class C, so every test that calls it restores."""
    C = ui.C
    saved = {k: v for k, v in vars(C).items() if not k.startswith("_")}
    yield C
    for key, val in saved.items():
        setattr(C, key, val)


# ── the palette itself ────────────────────────────────────────────────────────

def test_class_c_is_the_aurum_palette(pristine_tokens):
    """The brand tokens themselves, plus what the shipped default paints.

    Run through the fixture because ui.py calls load_theme() at import time with
    whatever accent the live install has saved - that is a user preference, not
    the spec, and it must no longer be overridden by a stylesheet patch.
    """
    C = pristine_tokens
    assert C.GOLD == "#FF2BD6"
    assert C.GOLD_DEEP == "#C2009E" and C.GOLD_LT == "#F3EDFF"
    assert C.GOLD_MID == "#9B5CFF" and C.ON_GOLD == "#0D0716"
    C.load_theme(C.GOLD)
    assert C.ACC == C.GOLD and C.PRI == C.GOLD, "the accent must be the brand, not a fallback"
    assert C.BG.upper() == "#05030A"
    assert C.TEXT == "#F3EDFF" and C.TEXT_DIM == "#9A8FB5"
    assert C.BORDER == "rgba(58, 33, 82, 0.85)", "glass borders are the purple hairline"


def test_shipped_default_accent_is_the_brand():
    """The template that a fresh install starts from must already be cyberpunk."""
    import json
    settings = json.loads((ROOT / "config" / "app_settings.json").read_text(encoding="utf-8"))
    assert settings["app_theme"] == "#FF2BD6"


def test_a_saved_non_gold_accent_is_honoured(pristine_tokens):
    """The other half of removing the monkey-patch: an explicit choice wins.

    Under the old filter a chosen accent was still fought over by a stylesheet
    rewrite, so nobody could tell what the setting actually did. Now the theme
    is the theme - gold is the default, not an enforced floor.
    """
    C = pristine_tokens
    C.load_theme("#ff6fa8")
    assert C.PRI == "#ff6fa8" and C.ACC == "#ff6fa8"
    assert "#ffc857" not in (C.PRI, C.ACC)


def test_no_token_holds_a_cyan_value(ui):
    offenders = []
    for key, val in vars(ui.C).items():
        if key.startswith("_"):
            continue
        vals = val if isinstance(val, (tuple, list)) else (val,)
        for item in vals:
            if isinstance(item, str) and any(tok.lower() in item.lower() for tok in CYAN_TOKENS):
                offenders.append(f"{key}={item}")
    assert not offenders, f"cyan survived in the token set: {offenders}"


def test_orb_state_tokens_match_the_b2_spec(ui):
    """B2 names four states with three colours each; the tokens must be those."""
    C = ui.C
    assert C.S_IDLE == ("#FF2BD6", "#C2009E", "#FF9BEF")
    assert C.S_LISTEN == ("#B6FF3B", "#3F7D00", "#E4FFB0")
    assert C.S_THINK == ("#F5E94B", "#FF7A2B", "#FFF6A8")
    assert C.S_SPEAK == ("#FF2BD6", "#9B5CFF", "#D9A6FF")
    assert C.S_SPEAK_SPECTRUM == ("#FF2BD6", "#9B5CFF", "#F5E94B", "#B6FF3B")


def test_historical_aliases_still_resolve_to_gold(ui):
    """Call sites written against the old ICE_* names must not paint blue."""
    C = ui.C
    assert C.ICE == C.GOLD and C.ICE_DIM == C.GOLD_DEEP
    assert "255, 43, 214" in C.ICE_GHO


# ── the anti-gold machinery must stay deleted ─────────────────────────────────

def test_stylesheet_monkey_patch_is_gone():
    assert "_new_setStyleSheet" not in UI_SOURCE
    assert "QWidget.setStyleSheet = " not in UI_SOURCE, \
        "patching setStyleSheet silently rewrites every surface's colours"


def test_page_hue_rotator_is_gone():
    assert "_apply_theme_to_html" not in UI_SOURCE


def test_ui_source_holds_no_forced_cyan():
    found = [tok for tok in CYAN_TOKENS if tok in UI_SOURCE]
    assert not found, f"cyan hexes back in ui.py: {found}"


# ── the smart-home page must not own a palette ────────────────────────────────
# It was the last module in the app still carrying a frozen copy of one, and it
# stayed out of reach of every token change for a whole redesign because no test
# had ever opened the file.

def test_smart_home_page_defines_no_module_level_palette():
    frozen = re.findall(
        r"^[A-Z_][A-Z_0-9]* *= *[\"']#[0-9a-fA-F]{3,8}[\"']", SMART_HOME_SOURCE, re.M
    )
    assert not frozen, f"the page froze its own colours again: {frozen}"


def test_smart_home_page_holds_no_cyan_or_frozen_ink():
    found = [hex_ for hex_ in FROZEN_PAGE_HEXES if hex_ in SMART_HOME_SOURCE]
    assert not found, f"pre-Aurum colours back in smart_home_page_new.py: {found}"


def test_smart_home_page_resolves_the_tokens_per_call():
    """ui.py imports this module, so the tokens cannot be bound at import time.

    Reading them through _aurum() at style-build time is also what lets a theme
    change reach the page; a module-level binding would freeze them again.
    """
    assert "def _aurum()" in SMART_HOME_SOURCE
    assert not re.search(r"^(from ui import|import ui)$", SMART_HOME_SOURCE, re.M), \
        "a module-scope import of ui is circular - ui imports this file first"
    assert SMART_HOME_SOURCE.count("_aurum()") > 15


def test_smart_home_qfont_call_sites_read_the_tokens():
    allowed = {"Segoe UI Emoji", "Nirmala UI"}
    literals = set(re.findall(r'QFont\("([^"]+)"', SMART_HOME_SOURCE))
    assert literals <= allowed, \
        f"literal font families bypass the type tokens: {literals - allowed}"
    assert SMART_HOME_SOURCE.count("QFont(C.F_") > 10


def test_platform_badges_wear_the_accent_not_eight_brand_colours():
    """The picker used to paint each vendor in its own logo colour."""
    assert "def _badge_style" in SMART_HOME_SOURCE
    assert SMART_HOME_SOURCE.count("_badge_style(") >= 2
    assert not re.search(r"_BRAND_COLORS|brand_colors\s*=\s*\{", SMART_HOME_SOURCE)


def test_the_ghost_brace_stylesheet_bug_stays_fixed():
    """A stray ``}`` made one sheet unparseable, so its buttons painted as
    system grey and Qt blamed only an object address."""
    assert "}}QPushButton" not in SMART_HOME_SOURCE
    assert "}}Q" not in SMART_HOME_SOURCE


# ── theming must be a re-polish, not a snap to cyan ───────────────────────────

def test_unknown_theme_word_wears_the_brand(pristine_tokens):
    C = pristine_tokens
    C.load_theme("nonsense")
    assert C.ACC == C.GOLD
    C.load_theme("#ffffff")
    assert C.ACC == C.GOLD, "a white theme means 'no accent chosen'"
    C.load_theme("blue")
    assert C.ACC == "#007aff", "an explicit choice is still honoured"


def test_aurum_words_alias_to_gold(pristine_tokens):
    C = pristine_tokens
    for word in ("gold", "aurum", "amber"):
        C.load_theme(word)
        assert C.ACC == "#FF2BD6", f"{word} must resolve to the brand"
    C.load_theme("violet")
    assert C.ACC == "#9B5CFF", "the thinking-state violet is a reachable theme"


def test_theme_change_recomputes_borders_from_the_choice(pristine_tokens):
    pristine_tokens.load_theme("#ff6fa8")
    assert pristine_tokens.BORDER == "rgba(255, 111, 168, 0.17)"
    assert pristine_tokens.BORDER_B == "rgba(255, 111, 168, 0.35)"


# ── the reactor page carries the same design ─────────────────────────────────

def test_reactor_page_has_no_icy_leftovers():
    found = [hex_ for hex_ in ICY_PAGE_HEXES if hex_ in PAGE_SOURCE]
    assert not found, f"pre-Aurum icy hexes still in the page: {found}"


def test_page_state_tints_match_b2():
    match = re.search(r"let Ft=\{([^}]*)\}", PAGE_SOURCE)
    assert match, "the page keeps its own state->tint map for the 3D reactor"
    tints = dict(re.findall(r'(\w+):"(#[0-9a-fA-F]{6})"', match.group(1)))
    assert tints["IDLE"] == "#ffc857"
    assert tints["LISTENING"] == "#ffe08a"
    assert tints["THINKING"] == "#b28cff"
    assert tints["SPEAKING"] == "#ff6fa8"


def test_page_orb_motion_follows_the_b2_timing():
    """B2 gives each state its own spin period and breath, as data."""
    assert re.search(r"let OrbSpec=\{IDLE:\{spin:40,breathe:4\.2", PAGE_SOURCE)
    blocks = re.findall(r"(\w+):\{spin:([\d.]+),breathe:([\d.]+),amp:([\d.]+)", PAGE_SOURCE)
    got = {name: (float(spin), float(breathe)) for name, spin, breathe, _amp in blocks}
    assert got["IDLE"] == (40.0, 4.2)
    assert got["LISTENING"] == (20.0, 2.2)
    assert got["THINKING"] == (5.0, 1.6)
    assert got["SPEAKING"] == (9.0, 1.1)
    assert got["MUTED"] == (80.0, 6.0), "muted is deliberately near-still"


def test_state_change_re_targets_spin_and_breathe():
    body = re.search(r"window\.setSaturnState=U=>\{(.*?)\}\}", PAGE_SOURCE)
    assert body, "setSaturnState must exist"
    src = body.group(1)
    assert "sp=OrbSpec[se]||OrbSpec.IDLE" in src
    assert "_brPeriod=sp.breathe" in src
    assert "_brAmp=sp.amp" in src
    assert 'toUpperCase()' in src, "the host sends mixed-case states"
    assert 'Ft[nt]?nt:"IDLE"' in src, "an unknown state must fall back to idle, not vanish"


def test_breathe_oscillator_is_wired_into_the_scale():
    assert re.search(r"_brPhase\+=nt\*6\.283185/_brPeriod", PAGE_SOURCE)
    assert "_brAmp*Math.sin(_brPhase)" in PAGE_SOURCE
    assert "_audioLevel*_brVoice" in PAGE_SOURCE, "audio must still push the orb while speaking"


def test_surface_shader_keeps_warm_shadow_and_its_semicolon():
    """The one line that tints the planet body; a dropped semicolon kills the
    shader and the page shows nothing but an error message."""
    line = re.search(r"vec3 color=mix\(tint\*vec3\(([^)]*)\),mix\(tint,vec3\(([^)]*)\),[^;]*;", PAGE_SOURCE)
    assert line, "the surface colour term must stay a complete GLSL statement"
    assert [float(x) for x in line.group(1).split(",")] == [.42, .24, .08]
    assert [float(x) for x in line.group(2).split(",")] == [1.0, .95, .82]


def test_state_bridge_exposes_the_new_motion_fields():
    for field in ("spinSpeed", "breathePeriod", "breatheAmp", "frameGapMs"):
        assert field in PAGE_SOURCE, f"__ORB__.getState() must report {field} for measurement"


# ── typography arrives with the fonts, not before ─────────────────────────────

def test_font_tokens_start_on_system_faces(ui):
    C = ui.C
    assert C.F_BODY in ("Segoe UI", "Manrope")
    assert C.F_MONO in ("Consolas", "JetBrains Mono")
    assert C.F_DISPLAY


def test_qfont_call_sites_read_the_tokens_not_a_literal_family():
    """A hardcoded family would silently outrank the bundled Aurum faces."""
    allowed = {"Segoe UI Emoji", "Nirmala UI"}  # script/glyph fallbacks, by design
    literals = set(re.findall(r'QFont\("([^"]+)"', UI_SOURCE))
    assert literals <= allowed, f"literal font families bypass the type tokens: {literals - allowed}"
    assert UI_SOURCE.count("QFont(C.F_") > 200


def test_install_is_a_silent_no_op_without_font_files(tmp_path):
    from core import theme_fonts

    class _Tokens:
        F_BODY = "Segoe UI"
        F_DISPLAY = "Segoe UI"
        F_MONO = "Consolas"

    empty = tmp_path / "fonts"
    empty.mkdir()
    before = (_Tokens.F_BODY, _Tokens.F_MONO)
    assert theme_fonts.install(_Tokens, fonts_dir=empty) == {}
    assert (_Tokens.F_BODY, _Tokens.F_MONO) == before


def test_font_spec_names_the_three_aurum_families():
    from core import theme_fonts
    # The cyberpunk design keeps the two Windows system faces it already uses
    # (Segoe UI body, Consolas mono) and bundles only the Unbounded display face,
    # so the spec is a single entry rather than the old three-family Aurum set.
    assert set(theme_fonts.SPEC.values()) == {"Unbounded"}
    assert set(theme_fonts.SPEC) == {"F_DISPLAY"}
    assert ".woff2" not in theme_fonts.SUPPORTED_SUFFIXES, \
        "Qt cannot read woff2; bundling it would register nothing"


# ── geometry the token layer cannot express ───────────────────────────────────
# Each of these was found by photographing the running shell, not by reading it.
# They are layout and paint contracts, so no stylesheet test would catch their
# return, and every one of them made a surface look broken while the colours
# were correct.

def test_untitled_settings_card_adds_no_phantom_label():
    """_card("") used to add an empty QLabel as its first layout item.

    The summary card sits beside a sixteen-card column and was stretched to its
    full height, so with no stretch item of its own the empty label claimed an
    equal share of the surplus and pushed every real row below the viewport.
    The panel rendered as a tall, blank rectangle.
    """
    page = UI_SOURCE[UI_SOURCE.index("class SystemConnectivityPage"):]
    card = re.search(r"    def _card\(self, title: str.*?\n(?=    def )", page, re.S)
    assert card, "SystemConnectivityPage._card must still exist"
    body = card.group(0)
    # Anchored to the line: a 12-space indented call contains eight spaces plus
    # the text as a substring, so a plain `in` check would never fail.
    assert not re.search(r"^ {8}lay\.addWidget\(head\)$", body, re.M), \
        "the title label must be added only under `if title:`, never unconditionally"
    assert "if title:" in body

    summary = re.search(r"    def _build_summary_card\(self\):.*?\n(?=    def )", page, re.S)
    assert summary and "addStretch(1)" in summary.group(0), \
        "the summary card needs a tail stretch to collect surplus height"
    assert "self._build_summary_card(), 3, Qt.AlignmentFlag.AlignTop" in page, \
        "top-aligned so the card keeps its own height instead of the column's"


def test_gesture_trigger_lives_in_the_taskbar():
    """The camera toggle used to be a chip inside a card floating over the page
    and permanently visible, so it landed on top of whatever each page kept in
    that corner: first the bar's own chips, then the chat rail, then a Quick
    Actions button. Live-input controls belong on the bar."""
    assert 'QPushButton("Gestures")' in UI_SOURCE
    assert "header_lay.addWidget(self._gesture_chip)" in UI_SOURCE

    card = re.search(r"class FloatingGestureCard.*?\n\nclass ", UI_SOURCE, re.S)
    assert card
    body = card.group(0)
    assert "self.btn" not in body, "the tile must not carry its own trigger"
    assert "self.hide()" in body, "the tile must start closed"
    assert "def _place" in body, "the tile must be positioned against the bar"

    # A hidden widget cannot overlap anything, so the resize handler only has
    # to follow the bar while the camera is actually open. Scoped to MainWindow,
    # whose resizeEvent is the second one in the file.
    mw = UI_SOURCE[UI_SOURCE.index("class MainWindow(QMainWindow):"):]
    resize = re.search(r"    def resizeEvent\(self, event\):.*?\n(?=    def )", mw, re.S)
    assert resize
    assert "_floating_gesture_card.isVisible()" in resize.group(0), \
        "repositioning must be gated on the tile being open"
    assert "_floating_gesture_card.show()" not in UI_SOURCE, \
        "nothing may make the camera tile visible except its own chip"


def test_idle_waveform_reads_as_a_meter_not_an_ellipsis():
    """At rest the bars were 3px wide by 3-5px tall with a 1.5 radius, i.e. a
    row of dots, which made the listening pill look like a rendering glitch."""
    cls = re.search(r"class VoiceWaveformVisualizer.*?\n\nclass ", UI_SOURCE, re.S)
    assert cls
    idle = re.search(r"idle_h = ([\d.]+) \+ harmonic \* ([\d.]+)", cls.group(0))
    assert idle, "the resting bar height must stay a formula of the shimmer"
    floor, swing = float(idle.group(1)), float(idle.group(2))
    assert floor >= 4.0 and swing >= 5.0, \
        f"a {floor}-{floor + swing}px tall bar on a 3px footprint reads as a dot"


def test_waveform_paints_from_the_tokens():
    """A painted widget cannot wear a stylesheet, so its colours have to come
    from C or a theme change leaves a gold shell with a fixed-gold meter."""
    cls = re.search(r"class VoiceWaveformVisualizer.*?\n\nclass ", UI_SOURCE, re.S)
    paint = cls.group(0)[cls.group(0).index("def paintEvent"):]
    assert not re.search(r"QColor\(\"#", paint), \
        "hardcoded hexes in paintEvent outrank every theme"
    assert "QColor(C.ACC" in paint and "QColor(C.RED)" in paint


def test_command_palette_chip_labels_the_chord_it_binds():
    """The artboard says "K" in the Mac chord. This app binds Ctrl+K on
    Windows keyboards, and Consolas has no U+2318, so Qt fell through the font
    cascade onto Segoe UI Emoji and painted the K in colour.

    In the Command Center header the palette is reached from the search icon
    button, so the chord it binds is named in that button's tooltip rather than
    on a text chip."""
    assert "Open the command palette (Ctrl+K)" in UI_SOURCE
    assert "_btn_search = self._hdr_icon_button(" in UI_SOURCE
    assert r'QPushButton("\u2318' not in UI_SOURCE, \
        "the looped-square glyph has no key on this app's keyboards and no glyph in the mono token"


# ── C1 / C2: the floating orb and the Saturn Workspace ────────────────────────

@pytest.fixture(scope="module")
def launcher_src():
    src = re.search(r"class FloatingLauncher.*?\n\nclass ", UI_SOURCE, re.S)
    assert src, "FloatingLauncher must still exist"
    return src.group(0)


def without_comments(src: str) -> str:
    """Strip comment lines before sweeping a block of code.

    Several of the fixes below are documented by naming the hex or glyph they
    removed, and a comment is not a call site - asserting against the whole
    source made those tests fail on their own explanations.
    """
    return "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))


def test_the_orb_reads_its_state_colours_from_the_b2_tokens(launcher_src):
    """The orb kept a private table of RGB triples whose comments disagreed with
    their own values - (255, 200, 87) was labelled "Electric Ice Cyan" - and it
    had no way to follow app_theme. C1/C2 give every state the same ring/deep/
    core triple the B2 artboard already feeds C.S_* with, so the orb reads those."""
    assert "_STATE_THEMES" not in launcher_src, "no private frozen palette"
    for state, token in (("idle", "C.S_IDLE"), ("listening", "C.S_LISTEN"),
                         ("thinking", "C.S_THINK"), ("speaking", "C.S_SPEAK")):
        assert f'"{state}": {token}' in launcher_src, f"{state} must wear the B2 triple"
    assert "def _theme(self)" in launcher_src, "the triple is resolved per frame"


def test_the_orb_carries_the_saturn_mark_not_the_old_name(launcher_src):
    """The core painted Devanagari "ब्रह्मा" - Brahma, the product's previous
    name - in Nirmala UI, a font no artboard mentions. C1 and C2 both put the
    ringed planet at the centre of the orb."""
    name = "\u092C\u094D\u0930\u0939\u094D\u092E\u093E"
    # The class docstring is allowed to name the old emblem it replaced; the
    # code that paints is not. Same reason the hex sweep below strips comments.
    code = "\n".join(l for l in launcher_src.splitlines() if not l.lstrip().startswith("#"))
    assert name not in code, "the old name must not be painted"
    assert "Nirmala UI" not in code
    assert "saturn_glyph" in launcher_src, "the core is the shared planet painter"
    assert "def _planet_pixmap" in launcher_src, "static art is cached, not redrawn per tick"


def test_the_orb_is_scaled_from_one_artboard_unit(launcher_src):
    """C2 sizes the orb 64 tray / 96 floating / 160 expanded off a 200-unit
    stage. Hardcoded pixel radii meant the layers fell out of proportion the
    moment the widget changed size, and the widget was 88px - nobody's number."""
    assert "FLOATING_D = 96" in launcher_src
    assert "ORB_UNITS = 217.0" in launcher_src
    assert "setFixedSize(88, 88)" not in launcher_src
    paint = without_comments(launcher_src[launcher_src.index("def paintEvent"):])
    assert "unit = self.FLOATING_D * (200.0 / self.ORB_UNITS)" in paint, \
        "every layer is derived from the artboard unit"
    # The window is wider than the orb (quick-action arc) and taller than the
    # orb when the pill shows, so either edge taken as the scale would resize
    # the orb as the pointer or the engine state changed.
    assert "min(w, h)" not in paint, "the window's shape must not drive the orb's size"
    assert not re.search(r"^ {8}(glow_r|sph_r|shock_r|R1|R2) = \d", paint, re.M), \
        "no layer may be a bare pixel radius any more"


def test_the_orb_offers_c2_hover_quick_actions(launcher_src):
    """C2 arcs three 40px wells over the orb on hover and hides them at rest,
    so a resting orb is still one object rather than a control panel."""
    assert "QA_D = 40" in launcher_src
    assert "def _place_quick_actions" in launcher_src
    assert "def _set_quick_actions_visible" in launcher_src
    assert "self._set_quick_actions_visible(bool(self._hovered))" in launcher_src, \
        "the arc opens and closes with the pointer and nothing else"
    for label in ("Talk", "Vision", "Chat"):
        assert f'"{label}"' in launcher_src
    assert '"toggle_mute"' in without_comments(launcher_src), "Talk has somewhere to land"


def test_the_orb_stage_never_resizes_under_the_pointer(launcher_src):
    """The stage is bigger than the orb on every side, so it must be one fixed
    square: growing and shrinking it for the pill slid the orb's centre 21px,
    and a transparent margin that grabs every click needs a hit test."""
    assert "STAGE_W = FLOATING_D + QA_D + MARGIN" in launcher_src
    assert "STAGE_H = FLOATING_D + MARGIN + PILL_GAP + PILL_H" in launcher_src
    assert "self.setFixedSize(self.STAGE_W, self.STAGE_H)" in launcher_src
    assert "self.setFixedSize(width, wanted)" not in launcher_src, \
        "set_state must not resize the stage"
    assert "def _hits_the_orb" in launcher_src
    init = launcher_src[launcher_src.index("def __init__"):launcher_src.index("def set_audio_level")]
    other_sizes = [line for line in init.splitlines()
                   if "self.setFixedSize" in line and "self.STAGE_W" not in line]
    assert not other_sizes, \
        f"the stage is sized once: {other_sizes}"


def test_the_shockwave_wears_the_active_state(launcher_src):
    """The audio ring was the one place that hardcoded the brand gold straight
    into a QColor, so a violet theme still pulsed gold behind the orb."""
    paint = launcher_src[launcher_src.index("def paintEvent"):]
    assert "QColor(255, 200, 87" not in paint
    assert "with_alpha(a_ring, self._audio_lvl * 240)" in paint


def test_the_orb_reads_its_state_pill_under_the_design_geometry(launcher_src):
    """C1 hangs a 130x32 r16 glass pill beneath the orb. It is a child widget
    rather than painted text so a theme change reaches it too."""
    assert "PILL_W = 130" in launcher_src
    assert "PILL_H = 32" in launcher_src
    assert "self._pill = QLabel(self)" in launcher_src
    assert "WA_TransparentForMouseEvents" in launcher_src, \
        "the pill must not eat the orb's own clicks"
    assert "self._pill.setText(" in launcher_src[launcher_src.index("def set_state"):]


def test_the_workspace_is_the_shape_and_kit_c1_drew():
    """500x820, cyberpunk-edited glass at radius 0, an h30 tab row, 34px icon
    wells and a primary-gradient send square - all of it from the shared
    builders, none of it a fourth hand-written copy of the recipe."""
    ws = re.search(r"class WorkspaceSidebar.*?\n\nclass ", UI_SOURCE, re.S)
    assert ws
    body = ws.group(0)
    assert "self._expanded_w = 500" in body
    assert "self._target_h = 820" in body
    assert "border-radius: {S.CARD_R}px" in body
    assert "S.nav(active, height=30, padding=12, font_size=12)" in body
    assert "self._close_btn.setFixedSize(S.ICON_D, S.ICON_D)" in body
    assert "saturn_avatar(36)" in body, "the header signs the panel with the planet"
    assert "def set_engine_state" in body, "the header mirrors the orb's state line"
    send = body[body.index("self._send_btn"):]
    assert f"stop:0 {{C.ACC}}, stop:1 {{C.PRI_GRAD}}" in send, \
        "the send square is the same fill as every primary action"
    assert "border-radius: {S.PILL_R}px" in send, \
        "the send square reads the shared radius constant, not a frozen 19px"


def test_the_feed_stops_bringing_its_own_brand_colours():
    """Event cards each arrived with a frozen hex - a Discord blue, three
    different ambers - and the history rows wore a literal "B" for the old
    product name in a hard-coded Segoe UI. The artboards are monochrome gold
    with the planet as the only mark."""
    # Sweep the code, not the commentary: several fixes are documented by naming
    # the hex they removed, and a comment is not a call site.
    code = "\n".join(l for l in UI_SOURCE.splitlines() if not l.lstrip().startswith("#"))
    for hex_ in ("#5865F2", "#ff7a45", "#ffbf00"):
        assert hex_ not in code, f"{hex_} is a brand colour the design removed"
    # #ffd98a still belongs to the reactor page's spark palette, so it is only
    # banned from the feed, where it was one of the per-kind accents.
    assert "#ffd98a" not in code[code.index("class EventCard"):code.index("class ArtifactCard")]
    event = re.search(r"class EventCard.*?\n\nclass ", UI_SOURCE, re.S)
    assert event and not re.search(r"accent = \"#", event.group(0))
    builder = re.search(r"    def _build_event_card.*?\n(?=    def )", UI_SOURCE, re.S)
    assert builder and "accent = None" in builder.group(0)
    history = re.search(r"class HistoryConversationItem.*?\n\nclass ", UI_SOURCE, re.S)
    assert history and "saturn_avatar(30)" in history.group(0)
    assert 'icon = QLabel("B")' not in UI_SOURCE


def test_the_self_repair_hud_wears_the_shell_palette():
    """The malfunction popup was the last surface with a palette of its own - a
    blue-black panel no artboard uses, white-on-grey buttons, hand-mixed diff
    greens and reds, and a Segoe UI Emoji font for its icons. Its alert red and
    success green are semantic and stay, but they come from the tokens now."""
    popup = re.search(r"class SaturnMalfunctionDialog.*?\n\nclass ", UI_SOURCE, re.S)
    assert popup, "the self-repair HUD must still exist"
    body = without_comments(popup.group(0))
    for stray in ("#151821", "#06070a", "#ff8580", "#ff7070", "#40ff70",
                  "rgba(10, 12, 16", "rgba(255, 255, 255, 0.08)", "#ffffff"):
        assert stray not in body, f"{stray} is a colour the token layer owns"
    assert "Segoe UI Emoji" not in body
    for glyph in ("\U0001f4c1", "\U0001f4a5", "\U0001f50d", "\U0001f6a8", "\U0001f6e1"):
        assert glyph not in body, "the design uses monochrome glyphs, not colour emoji"
    assert "background: {C.PANEL}" in body
    assert "S.ghost(size=12)" in body
    assert "S.glass()" in body
    assert "color: {C.RED}" in body


def test_the_conversation_feed_reads_the_tokens_too():
    """The feed's empty-state card and scrollbar were the last white-on-dark
    literals inside C1's own panel, so a theme change reached the panel and
    stopped at its contents."""
    feed = re.search(r"class ConversationFeed.*?\n\nclass ", UI_SOURCE, re.S)
    assert feed
    body = without_comments(feed.group(0))
    for stray in ("#ffffff", "rgba(255,255,255,0.03)", "rgba(255, 200, 87,0.45)"):
        assert stray not in body, f"{stray} bypasses the token layer"
    assert "S.glass()" in body, \
        "the empty-state card takes the shared radius-0 glass"
    assert "background: {C.PRI_GHO};" in body, "the scroll handle is a token wash"


def test_the_composer_wears_line_icons_not_color_emoji():
    """The composer's mic, attach and send were color emoji (🎙📎➤).

    An emoji ignores the palette, renders as an inconsistent bitmap and is the
    single most obvious tell of a stock bot. They are now Segoe MDL2 Assets line
    glyphs drawn in the icon face, so the gold ink of the theme paints them. This
    also covers the quick-action tray, whose chips no longer prefix every word
    with a stray emoji.
    """
    code = without_comments(UI_SOURCE)

    # A dedicated icon face exists as a token and is the one used for the glyphs.
    assert 'F_ICON    = "Segoe MDL2 Assets"' in UI_SOURCE
    assert "QFont(C.F_ICON" in code
    assert 'font-family: \'{C.F_ICON}\'' in code, "the send disc paints its glyph too"

    # Neither composer may build a button from a color emoji any more.
    for emoji in ("🎙", "📎", "➤"):
        assert f'QPushButton("{emoji}")' not in code
    # ...but every one of them has a line-icon replacement.
    for glyph in ("\\uE720", "\\uE723", "\\uE724"):
        assert f'QPushButton("{glyph}")' in code

    # The quick-action tray dropped its leading emoji and reads as clean type.
    assert '("🛡️ Saturn Status"' not in code
    assert '("Saturn Status", "auto heal status")' in code


def test_a_truncated_system_card_is_recognised_as_an_echo(ui):
    """The reply used to appear twice: a bubble, then a 'System Event' card that
    re-printed the same words cut off at the first line. Exact equality missed the
    truncated copy, so the guard compares a collapsed prefix."""
    bubble = ("That's correct, as we just discussed. My memory context is dynamic, "
              "resizing based on the immediate demands of the active task.")
    truncated_card = ("That's correct, as we just discussed. My memory context is "
                      "dynamic, resizing based on the immediate demands of")
    assert ui._chat_echo(bubble, truncated_card) is True
    # A genuinely different status line is never swallowed.
    assert ui._chat_echo(bubble, "Study session stopped, sir.") is False
    assert ui._chat_echo("", "anything") is False


def test_the_feed_dedups_at_its_single_choke_point():
    """Every renderer -- live path and history replay -- passes through
    ConversationFeed.add_message, so that is where the echo must be dropped, not
    only in record_chat_event (which the replay bypasses)."""
    feed = re.search(r"class ConversationFeed.*?\n    def _build_event_card", UI_SOURCE, re.S)
    assert feed
    body = without_comments(feed.group(0))
    assert "_chat_echo(prev, text)" in body
    assert "self._recent_bubbles" in body

