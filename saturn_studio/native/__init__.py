"""Saturn Studio -- the clean-room, native design engine.

Everything in this package replaces the vendored TypeScript engine with
Saturn's own Python. The one structural rule that makes the whole thing safe
to build in parallel -- and the renderer genuinely swappable -- is the layering:

* ``scene`` is the authority. It holds what is on the canvas and imports no
  painter at all: no Qt, no Skia, nothing that puts a pixel down.
* ``tools`` are the Saturn verbs, run synchronously against that scene.
* ``document`` reads and writes Saturn's own ``.saturn`` format.
* ``items`` (and, later, any other backend) *projects* the scene into pixels.
  Qt is the first backend; a native Skia-GPU one could sit beside it and the
  scene would not know or care which is drawing.

Written from the verb contract and this model's own rules, not ported from any
existing engine's source.
"""
