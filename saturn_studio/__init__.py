"""Saturn Studio -- the design engine Saturn draws with.

Saturn's own clean-room engine, written in Python against a Qt painter. There
is no vendored JavaScript engine, no loopback server and no browser process:
the whole canvas lives in this process.

Two parts, one boundary:

* ``native`` -- the engine. A Qt-free scene model (``native.scene``) is the
  authority; a Python verb registry (``native.tools``) answers the same named
  tools the studio has always spoken; a Qt painter (``native.items`` and
  ``native.export``) projects the model to the screen and to PNG/SVG. The model
  imports no painter, which is what keeps the renderer swappable.
* ``window`` -- the door. It re-exports the canvas host so ``tool.py``, the
  voice layer and the agents call one API and never learn which engine answers.

Everything a user sees and says -- the shell, the verbs, the chrome -- is
Saturn's own. CesiumJS, vendored separately for Saturn's View globe, is the only
third-party runtime in the product, and its notice lives in the repository
NOTICE like every bundled piece.
"""
