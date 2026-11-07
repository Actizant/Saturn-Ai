"""Manual check of the Studio bridge against a real native canvas.

Run it with Saturn closed, so only one process owns the canvas::

    python -m saturn_studio.live_check

The studio comes up the way it does in the app -- a Qt ``QWidget`` canvas on the
GUI thread -- while the verbs are called from a second thread, which is how the
voice loop calls them. The engine runs in this process: there is no loopback
server, no JavaScript bridge and no browser to wait on. The only thing stubbed
here is Saturn's brain, because this checks the bridge and not the model: the
JSX it hands back has to reach the engine and come back reported as what the
engine actually said.

The automatic suite in ``tests/`` covers the same contract against doubles and
offscreen canvases. This script is what catches a bridge that is wired wrong in
a way a double cannot notice -- a call that never reaches the GUI thread, a
scene the canvas never repaints, a reply that names a layer the engine never
made, or auto-layout that did not actually run against real widget metrics.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

# The canvas has to be paintable without a display, so default to offscreen
# unless the operator asked to watch it: set SATURN_STUDIO_LIVE=1 to see the
# window and drop a real screenshot.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

# The brain has to be stubbed before the tool imports it, which happens inside
# the design verb, so this goes in place first.
JSX = (
    '<Frame name="Live check" w={640} h={360} bg="#101418" flex="col" '
    'gap={16} p={24} rounded={12}>'
    '<Text name="Headline" size={34} color="#F0B429" weight={700}>Saturn drew this</Text>'
    '<Frame name="Bar" w="fill" h={8} bg="#2BD6FF" rounded={4} />'
    '<Text name="Caption" size={16} color="#C8D3E0">one frame, two children, real pixels</Text>'
    "</Frame>"
)


class _FakeBrain:
    """Answers like a model that writes JSX and wraps it in a fence."""

    def chat(self, prompt, **kwargs):
        return f"Here is your design:\n```jsx\n{JSX}\n```"


brain = types.ModuleType("llm_client")
brain.client = _FakeBrain()
sys.modules["llm_client"] = brain

import saturn_studio.tool as studio_api  # noqa: E402  (needs the stub above)
import saturn_studio.window as studio_host  # noqa: E402

CALLS = (
    {"action": "open"},
    {"action": "design", "brief": "a banner that says Saturn drew this"},
    {"action": "read"},
    {"action": "describe"},
    {"action": "status"},
    {"action": "tools"},
    {"action": "engrave"},  # a verb Saturn never claimed, to see it refused
)


def _clean_of(value) -> str:
    """Engine names come back as text or None; compare them plainly."""
    return "" if value is None else str(value).strip().strip('"')


def main() -> int:
    app = QApplication(sys.argv)
    window = studio_host.StudioWindow()
    window.resize(1280, 800)
    window.show()

    spoken: list[tuple[str, str]] = []
    done = threading.Event()
    started = time.monotonic()

    def worker() -> None:
        for call in CALLS:
            spoken.append((str(call.get("action")), studio_api.saturn_studio(call)))
        done.set()

    def probe() -> None:
        """Ask the canvas what it actually holds, before trusting the replies.

        The native surface is a ``QGraphicsScene`` of projected items -- there is
        no DOM and no web canvas to query here, only the model the canvas renders.
        """
        print("--- canvas probes ---")
        scene = window.canvas.scene()
        print("graphics items:", len(scene.items()))
        print("scene rect:", scene.sceneRect())
        print("is visible:", window.isVisible())

    def report() -> None:
        probe()
        if os.environ.get("SATURN_STUDIO_LIVE") == "1":
            shot = Path(tempfile.gettempdir()) / "saturn_studio_live.png"
            try:
                window.grab().save(str(shot))
                print("screenshot:", shot)
            except Exception as exc:  # noqa: BLE001 -- a screenshot is not the point
                print("screenshot failed:", exc)
        print("\n--- what Saturn would have said ---")
        for action, sentence in spoken:
            print(f"[{action}] {sentence}")
        print("--- engine state, read directly ---")
        state = window.state()
        print(state)

        # The layout engine is Saturn's own, so prove it is the one measuring,
        # not a stack. A child declared w="fill" inside 24 of padding only comes
        # out at 592 wide if the flex pass actually ran on the real canvas.
        print("--- layout proof ---")
        top = (state.get("layers") or [{}])[0]
        frame = window.run_tool("get_node", {"id": top.get("id")}).get("result") or {}
        kids = frame.get("children") or []
        geometry = []
        for kid in kids[:4]:
            kid_id = kid if isinstance(kid, str) else (
                kid.get("id") if isinstance(kid, dict) else None
            )
            got = window.run_tool("get_node", {"id": kid_id}).get("result") or {}
            geometry.append(got)
            print("  child:", {k: got.get(k) for k in ("name", "x", "y", "width", "height")})
        bar = next((g for g in geometry if _clean_of(g.get("name")) == "Bar"), {})

        answers = dict(spoken)
        failures = []
        if "Live check" not in answers.get("design", ""):
            failures.append("design did not report the layer the engine named")
        if "Live check" not in answers.get("read", ""):
            failures.append("read did not see the drawn layer")
        if not state.get("layers"):
            failures.append("the engine reports an empty canvas")
        if "engrave" not in answers.get("engrave", "").lower():
            failures.append("an unknown verb was not refused by name")
        if not geometry:
            failures.append(f"could not read child geometry from the engine: {frame!r}")
        elif abs(float(bar.get("width") or 0) - 592.0) > 1.0:
            failures.append(
                "the layout engine did not measure the fill child: "
                f"Bar came out {bar.get('width')}, expected 592"
            )
        print("\n--- verdict ---")
        if failures:
            for line in failures:
                print("FAIL:", line)
        else:
            print("PASS: the bridge carried a design onto a real native canvas and back")
        app.quit()

    def poll() -> None:
        if done.is_set():
            report()
        elif time.monotonic() - started > 120:
            print("TIMEOUT with these answers so far:")
            for action, sentence in spoken:
                print(f"[{action}] {sentence}")
            app.quit()
        else:
            QTimer.singleShot(200, poll)

    QTimer.singleShot(1500, lambda: threading.Thread(target=worker, daemon=True).start())
    QTimer.singleShot(2000, poll)
    app.exec()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
