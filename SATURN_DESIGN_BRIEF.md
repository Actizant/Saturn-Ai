# Saturn — New UI Design Brief (build from scratch)

**Audience:** a designer (or design studio), no prior context assumed.
**Mandate:** design a **completely new visual identity and UI** for Saturn. We are rebuilding the entire front-end from the ground up on a lightweight GPU stack. The product's *behaviour* is fixed; the *look* is yours to invent.

---

## 1. What Saturn is

Saturn is a **local-first AI desktop assistant** for Windows. It lives on the desktop as a floating, animated **orb**, listens through the mic, can see the screen and camera, remembers context, and takes action: chat, voice, a live vision HUD, smart-home control, service connections (Gmail / Instagram / Spotify / Discord / phone pairing), proactive reminders, self-repair, and a daily briefing.

It is being **rebranded from "Brahma AI Evo" to "Saturn."** The engine and every feature already work — we are replacing *only* the interface.

### Fixed brand rules (non-negotiable)
- ✅ Product name: **Saturn**. Maker tagline: **by Actizant**.
- ❌ Never show the old brand: "Brahma", "Evo", "AI-EVO", the Devanagari "ब्रह्मा", or the legacy cyan accent.
- The **orb is the hero** and must remain the centrepiece (its *style* is open — see §4).

### Open to you
- **Everything about the look**: palette, mood, typography, iconography, layout language, motion.
- **Personality/tone** — decide with us: a calm helpful companion, a bold "villain"/powerful-operator character, or something between. This can drive the whole aesthetic.

### The ambition: **futuristic — and genuinely the best**
We want Saturn to look **futuristic**: a calm, cinematic, next-generation **operator instrument** — the refinement of a sci-fi HUD / spacecraft console, but everyday-usable, not gimmicky. It should look like *nothing else on the Windows desktop*. Signals to chase: precise thin line-work, elegant data visualisation, tasteful depth and light, purposeful motion, and a distinctive display typeface.

- **Get "futuristic" from form, not from cost.** Glow, depth and atmosphere must come from **vector shapes, gradients, hairline strokes, layering and well-tuned motion** — **not** from full-screen blur, glassmorphism or particle sims, which we can't afford on the GPU (§3). Restraint and craft are exactly what will read as *premium*; cheap neon haze reads as *dated*.
- This is a **north star, not a template** — the specific vision is still the designer's to propose (§2 concepts).

> The existing gold-on-black "Aurum" design (see §8) is **one reference among many**, not the target. Feel free to propose something entirely different.

---

## 2. Process we'd like

1. **Moodboards / 2–3 concept directions** (palette, type, feel) — pick one before any screens.
2. **Key screens** in the chosen direction (orb + Home + Chat first).
3. **Full surface set** (§5), then **states & variants** (§6).
4. **A token spec** — exact colour / type / spacing / radius values in a table, so engineering wires them into a single source of truth.

---

## 3. Hard technical constraints (so the design is actually buildable)

- **Stack:** desktop app rendered on a **GPU scene graph (Qt Quick / QML)** — *no web views, no Electron* (RAM is the whole reason we're rebuilding).
- **Cheap motion only:** no heavy blur / glassmorphism / full-screen shadows. Every animation must run at 60fps+ on a mid laptop (RTX 3050 / integrated). Provide a **"reduce effects"** low-power variant.
- **Local-first:** the core must look and work with **no internet** — design assumes offline, not cloud.
- **Multilingual type:** must not break with Latin, **Bengali / Devanagari**, and long CJK strings.
- **Two themes:** dark **and** light.
- **Windows 10/11**, `Segoe UI` + `Consolas` are always present; any display font must be a bundled file.
- **Layout safety:** define a min window size (~880×560) and respect the **taskbar / multi-monitor** edges (we've hit a real collision bug).
- **Accessibility:** visible keyboard focus, AA contrast, no meaning carried by colour alone.

### Target tech stack (what the new UI will actually be built on)
- **Runtime:** Python 3.12 (existing `.venv312` toolchain).
- **UI framework:** **Qt Quick / QML (Qt 6)** driven from **PyQt6** (`QtQml`, `QtQuick`, `QtQuickControls2`).
  - The reference build is written in **PySide6**; because the engine is **PyQt6** and a single process cannot load both Qt bindings, the new front-end is hosted by **PyQt6's QML modules**. The `.qml` files themselves are binding-agnostic — only the Python host changes (`Signal/Slot/Property` → `pyqtSignal/pyqtSlot/pyqtProperty`).
- **Rendering:** native **GPU scene graph** (QML shapes, `ShaderEffect`, `Canvas` only where needed). **No QWebEngineView, no Electron, no Chromium** — cutting those is the main reason for the rebuild (RAM).
- **Motion:** vsync-locked, frame-driven (one animation advanced per rendered frame; no repaint timers). `QtQuick.Effects` used sparingly; **no full-screen Gaussian blur**.
- **Engine (unchanged):** the existing `SaturnLive` brain, voice routing, memory, smart-home, service connections and self-repair stay in Python and are **not** rewritten.
- **The one boundary:** a single `SaturnBackend`-style `QObject` between engine and UI — methods out (`set_state`, `set_level`, `add_message`, `add_event`, `add_error`, `set_frame`, `set_detections`, …) and signals in (`userMessage`, `talkRequested`, `serviceRequested`, `settingChanged`, …). QML binds to it via a context property. This is the seam the new UI plugs into.
- **Theming:** one **token source** — a QML singleton `Theme` that every component reads, with **dark + light** variants. A palette change re-skins the whole app from that single place (this is why the designer's §7 token table is the key handoff artifact).
- **Fonts:** system `Segoe UI` / `Consolas`; any display font (e.g. *Unbounded*) is **bundled as a file** and registered via `QFontDatabase` — never assumed present.
- **Icons:** a monochrome **line-icon set** (custom SVG, or `Segoe MDL2 Assets` glyphs that inherit the theme colour). **No colour emoji as UI icons.**
- **Packaging / OS:** PyInstaller (existing installer specs), targeting **Windows 10/11**.

---

## 4. The orb (hero element)

A living, state-aware sphere. Keep the concept; invent the style. It must clearly read these **states** (visually distinct, and blending smoothly between them):

| State | Meaning |
|---|---|
| `idle` | present, listening for a wake word |
| `listening` | hearing the user (react to mic level) |
| `thinking` | processing / working |
| `speaking` | talking (react to voice) |
| `muted` | mic off |
| `offline` | no connection / local-only |
| `error` | something failed / self-repair |

Optional signature ideas to explore (not required): an audio-reactive ring, a planetary/rings motif playing on the name "Saturn", a subtle "eye". The current build uses a 72-tick audio ring + ringed-planet core — a fine example, but **your treatment can be entirely new**.

---

## 5. Surfaces to design (the full inventory)

**Core (exist today, need a new look)**
1. **Home** — orb + at-a-glance cards (time/date, active window, engine status, today, telemetry).
2. **Chat** — message feed + composer.
3. **Vision HUD** — live camera/screen view with detection overlays, scan line, brackets, radar, timeline, privacy & performance.
4. **Settings** — AI providers, Identity, Connections, Vision, Orb, Audio & Hardware, Startup, Auto-Heal.
5. **Floating orb window** (always-on-top mini Saturn) + **Workspace mini-chat**.
6. **System tray** presence.

**Additional screens (exist in the product, need designing)**
7. **Boot / startup sequence** — the "waking up" panel with progress.
8. **First-run onboarding / setup wizard** — welcome, identity, voice, permissions, connect a provider.
9. **Smart Home / device management** — rooms, device tiles, add/edit, scenes.
10. **System & Connectivity detail** — per-service connect flows, **mobile QR-pairing**, OAuth states, disconnect/re-auth.
11. **Task Workspace / Task Dock** — multi-step tasks: live steps, progress, sub-results, artifacts, stop/pause.
12. **Memory Inspector** — what Saturn remembers: search, edit, forget, per-session.
13. **Meeting mode** — live transcript, participants, action items, summarize.
14. **Command palette** — Ctrl+K omnibox.
15. **Developer mode** — logs, tool traces, prompt/telemetry.
16. **Daily Briefing** — weather + calendar + **latest local & international news**.
17. **Self-repair / malfunction HUD** — what broke, the patch, rollback, history.
18. **Gesture / floating vision window** — hand-tracking preview + pin-on-top vision card.

**Chat message components (beyond plain text)**
- Attachment / file card, image result card, code block (with copy), artifact card (doc/ppt/sheet), tool-run / progress card ("Executing X…"), streaming/typing indicator, message actions (copy/speak/retry), user avatar/initials, folded "N system events" line, error card with an action button, and a **live-subtitles strip** while Saturn speaks.

**Global surfaces & dialogs**
- Confirmation dialog (dangerous actions), passphrase / secret entry, proactive incoming-alert, scanning overlay, floating HUD readout cards, live telemetry (CPU/RAM/network/GPU), voice waveform / input-level visualizer, file drop zone, undo / action-history.

---

## 6. Every screen must also specify

- **Empty / loading / error** states.
- **Dark and light** themes.
- **Reduce-effects** (low-power) variant for anything animated.
- Behaviour at **min size** and on **multi-monitor**.
- **Focus / keyboard** states.

---

## 7. Deliverables

1. Concept directions → chosen identity.
2. All §5 surfaces, with §6 states.
3. The §4 orb in all states.
4. A **token table** (exact hex / type / spacing / radius / motion timings) — this becomes engineering's single source of truth.
5. Icon set (line style preferred; no colour emoji as UI icons).

---

## 8. Reference material (examples, NOT the target look)

- A complete working build in the current gold "Aurum" style: run `cd experimental/saturn_ui_qml && .venv_qml\Scripts\python main.py`, or see `shots/qml_live.png`. Use it to understand **what each screen does and how it behaves** — the visual style is open for reinvention.
- The shipping app (PyQt6) demonstrates the same features with real data if you want to see behaviour in context.

**Bottom line:** build us a Saturn that looks and feels like nothing we have today — same brain, new face.
