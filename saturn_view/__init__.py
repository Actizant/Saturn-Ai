"""Saturn's View -- the globe engine that Saturn looks with.

Three parts, and the boundary between them is the whole design:

* ``assets/saturn_view`` -- the engine. Static files, vendored CesiumJS plus our
  own modules for the viewer, the layer contract, the camera and freshness.
  It has no idea Saturn exists.
* ``providers`` -- the live data. Python, keyless where possible, cached, and
  the only thing that talks to the outside world.
* ``server`` -- the HTTP host that joins the two, so the engine needs no Node,
  no build step and no bundler.

Nothing in here is derived from another product's application code. CesiumJS is
an independent Apache-2.0 renderer; the engine, the shell, the layer model and
the freshness contract are written for Saturn.
"""
