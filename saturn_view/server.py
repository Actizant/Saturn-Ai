"""Saturn's View -- HTTP host for the native globe engine.

The engine is plain static files (vendored CesiumJS plus our own modules) served
from `assets/saturn_view`. This module supplies the one thing a static tree
cannot: the live data endpoints. No Node, no build step, no API keys required
for the first-run experience.

Run standalone:
    .venv312\\Scripts\\python.exe -m uvicorn saturn_view.server:app --port 8794

Layer routes are deliberately thin. Each one answers in the shape its layer
expects and nothing more; the layer decides how to read it.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from saturn_view import providers

logger = logging.getLogger("saturn.view")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DIST = BASE_DIR / "assets" / "saturn_view"

# Windows resolves .js from the registry, which some installs map to text/plain.
# That silently breaks ES modules, so the types the engine needs are pinned here.
for _suffix, _type in (
    (".js", "text/javascript"),
    (".mjs", "text/javascript"),
    (".css", "text/css"),
    (".json", "application/json"),
    (".wasm", "application/wasm"),
    (".glb", "model/gltf-binary"),
    (".gltf", "model/gltf+json"),
    (".svg", "image/svg+xml"),
):
    mimetypes.add_type(_type, _suffix)


def resolve_dist(explicit: Optional[str] = None) -> Optional[Path]:
    """Locate the engine tree, newest configuration first."""
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ["SATURN_VIEW_DIST"]) if os.environ.get("SATURN_VIEW_DIST") else None,
        DEFAULT_DIST,
    ]
    for candidate in candidates:
        if candidate and (candidate / "index.html").is_file():
            return candidate
    return None


def _num(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def create_app(dist_dir: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="Saturn's View", docs_url=None, redoc_url=None)
    dist = resolve_dist(dist_dir)

    @app.get("/api/engine")
    def engine_status():
        """What the engine can ask this host for. Used by the Python bridge."""
        return JSONResponse(
            {
                "name": "saturn-view",
                "routes": ["flights", "military", "celestrak", "quakes", "land", "buildings"],
                "dist": str(dist) if dist else None,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/flights")
    def flights(request: Request):
        query = request.query_params
        lat, lon = _num(query.get("lat")), _num(query.get("lon"))
        span = _num(query.get("span"))
        headers = {"Cache-Control": "no-store"}
        try:
            if lat is not None and lon is not None:
                # The globe asks for "here" as a centre plus a span, in degrees.
                pad = span if span and span > 0 else 4.0
                payload, meta = providers.flight_states(
                    lat - pad, lon - pad, lat + pad, lon + pad
                )
            else:
                payload, meta = providers.flight_states()
        except Exception as exc:
            logger.warning("flights feed failed: %s", exc)
            return JSONResponse(
                {"error": "flights unavailable"}, status_code=502, headers=headers
            )
        headers.update(meta)
        return JSONResponse(payload, headers=headers)

    @app.get("/api/military")
    def military():
        # readsb-shaped rows: the military layer normalises this itself.
        headers = {"Cache-Control": "no-store", "X-Feed-Source": "adsb.lol"}
        try:
            return JSONResponse(providers.military_states(), headers=headers)
        except Exception as exc:
            logger.warning("military feed failed: %s", exc)
            return JSONResponse(
                {"error": "military feed unavailable"}, status_code=502, headers=headers
            )

    @app.get("/api/celestrak/{group}")
    def celestrak(group: str):
        try:
            text = providers.tle_group(group)
        except KeyError:
            return JSONResponse({"error": "Unknown satellite group"}, status_code=404)
        except Exception as exc:
            logger.warning("TLE fetch failed for %s: %s", group, exc)
            return JSONResponse({"error": "TLE unavailable"}, status_code=502)
        return PlainTextResponse(text, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/api/quakes")
    def quakes():
        """Global seismicity for the last 24 hours, compacted for the globe."""
        headers = {"Cache-Control": "no-store", "X-Feed-Source": "USGS"}
        try:
            payload = providers.earthquake_feed()
        except Exception as exc:
            logger.warning("earthquake feed failed: %s", exc)
            return JSONResponse(
                {"quakes": [], "error": "quakes unavailable"}, status_code=502, headers=headers
            )
        return JSONResponse(payload, headers=headers)

    @app.get("/api/land")
    def land():
        """World land polygons -- the Orbit lens' geography.

        Geometry instead of imagery: land needs no tile server, no licence and
        no per-pan download, and the edge stays crisp at every zoom because it
        is drawn rather than sampled.
        """
        headers = {"Cache-Control": "public, max-age=86400", "X-Feed-Source": "Natural Earth"}
        try:
            payload = providers.land_feed()
        except Exception as exc:
            logger.warning("land feed failed: %s", exc)
            return JSONResponse(
                {"polygons": [], "error": "land unavailable"}, status_code=502, headers=headers
            )
        return JSONResponse(payload, headers=headers)

    @app.get("/api/buildings")
    def buildings(request: Request):
        """Real building footprints around a point, for the city register.

        A square window rather than a box: the layer computes it from what the
        camera can actually see and the provider caches on the rounded result,
        so a camera that drifts is answered from the tile it already has.
        """
        query = request.query_params
        lat, lon = _num(query.get("lat")), _num(query.get("lon"))
        span = _num(query.get("span"))
        headers = {"Cache-Control": "no-store", "X-Feed-Source": "OpenStreetMap"}

        if lat is None or lon is None:
            return JSONResponse(
                {"buildings": [], "error": "buildings need lat and lon"},
                status_code=400,
                headers=headers,
            )

        # A window is clamped rather than refused: the layer asks for what it
        # can see, and a view that is too wide is a view that is too high to be
        # drawing footprints in at all.
        size = min(0.4, max(0.004, span if span and span > 0 else 0.05))
        north = max(-85.0, min(85.0, lat + size / 2))
        south = max(-85.0, min(85.0, lat - size / 2))
        west = max(-180.0, lon - size / 2)
        east = min(180.0, lon + size / 2)

        try:
            payload = providers.building_feed(south, west, north, east)
        except Exception as exc:
            logger.warning("building feed failed: %s", exc)
            return JSONResponse(
                {"buildings": [], "error": "buildings unavailable"},
                status_code=502,
                headers=headers,
            )
        return JSONResponse(payload, headers=headers)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST"])
    def unimplemented(rest: str):
        # An honest 404 beats a stack trace; layers degrade by design.
        return JSONResponse({"error": "Unknown API route"}, status_code=404)

    if dist:
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="view")
        logger.info("Saturn's View served from %s", dist)
    else:
        logger.warning("engine tree not found at %s", DEFAULT_DIST)

        @app.get("/")
        def missing():
            return PlainTextResponse("Saturn's View engine tree is missing.", status_code=503)

    return app


app = create_app()
