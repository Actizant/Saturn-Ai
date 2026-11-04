"""Saturn's View -- data providers for the globe engine.

Every function here answers in the shape one layer expects and nothing more.
The layer decides how to read it, which keeps this module free of rendering
assumptions:

  * `flight_states` returns OpenSky state-vector rows, not our own dicts
  * `tle_group` returns raw TLE text, because the orbit maths lives in JS
  * `earthquake_feed` returns compact rows, because the feed is large

Keyless upstreams only. Anything that would need a paid key stays unimplemented
and degrades in the UI instead of failing loudly.
"""

from __future__ import annotations

import io
import logging
import math
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import requests

logger = logging.getLogger("saturn.view.providers")

USER_AGENT = "Saturn/1.0 (+https://github.com/Actizant)"
_HTTP_TIMEOUT = 30
_session: Optional[requests.Session] = None
_session_lock = threading.Lock()

# CelesTrak groups the satellites layer can ask for. Anything else is refused so
# a crafted path can never reach an arbitrary upstream.
TLE_GROUPS = frozenset(
    {
        "stations",
        "visual",
        "gps-ops",
        "glo-ops",
        "galileo",
        "geo",
        "science",
        "weather",
        "iridium-NEXT",
        "oneweb",
    }
)

# CelesTrak refuses `active` and `starlink` to anonymous clients -- several
# thousand elements is treated as abuse. That is a fine line to be held to,
# because the honest answer is better than the big one: this composite is every
# object a person might actually want to look at, merged and de-duplicated by
# NORAD id. Crewed stations, the visible passes, navigation, weather, science,
# two broadband shells and the whole geostationary belt.
COMPOSITES = {
    "catalog": (
        "stations",
        "visual",
        "gps-ops",
        "glo-ops",
        "galileo",
        "science",
        "weather",
        "iridium-NEXT",
        "oneweb",
        "geo",
    ),
}

MPS_PER_KNOT = 0.514444
METRES_PER_FOOT = 0.3048


def _http() -> requests.Session:
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                session = requests.Session()
                session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
                _session = session
    return _session


class _TtlCache:
    """Small thread-safe TTL cache; the globe polls several feeds at once."""

    def __init__(self) -> None:
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Any:
        with self._lock:
            hit = self._items.get(key)
        if not hit:
            return None
        stored_at, value = hit
        return value if (time.time() - stored_at) < ttl else None

    def put(self, key: str, value: Any) -> Any:
        with self._lock:
            self._items[key] = (time.time(), value)
        return value


_cache = _TtlCache()


# --------------------------------------------------------------------------- #
# Aircraft
# --------------------------------------------------------------------------- #


def _adsb_row(aircraft: dict, snapshot_ms: int) -> list:
    """Flatten one adsb.lol observation into an OpenSky state-vector row.

    Slot order is fixed by the upstream normaliser: 0 icao24, 1 callsign,
    2 origin country, 3 position time, 4 contact time, 5 lon, 6 lat,
    7 baro altitude, 8 on ground, 9 ground speed, 10 course, 11 vertical rate,
    12 sensors, 13 geometric altitude, 14 squawk, 15 spi, 16 source, 17 category.
    """
    seen = aircraft.get("seen")
    seen_position = aircraft.get("seen_pos")
    on_ground = str(aircraft.get("alt_baro", "")).lower() == "ground"
    baro_ft = aircraft.get("alt_baro")
    geo_ft = aircraft.get("alt_geom")
    speed_kts = aircraft.get("gs")
    rate_ft_min = aircraft.get("baro_rate")

    return [
        str(aircraft.get("hex") or "").lower(),
        str(aircraft.get("flight") or "").strip() or None,
        None,  # adsb.lol publishes no origin country
        None if seen_position is None else round((snapshot_ms / 1000) - float(seen_position), 1),
        None if seen is None else round((snapshot_ms / 1000) - float(seen), 1),
        aircraft.get("lon"),
        aircraft.get("lat"),
        None if on_ground or not isinstance(baro_ft, (int, float)) else baro_ft * METRES_PER_FOOT,
        on_ground,
        None if not isinstance(speed_kts, (int, float)) else speed_kts * MPS_PER_KNOT,
        aircraft.get("track"),
        None if not isinstance(rate_ft_min, (int, float)) else rate_ft_min * METRES_PER_FOOT / 60.0,
        None,
        None if not isinstance(geo_ft, (int, float)) else geo_ft * METRES_PER_FOOT,
        str(aircraft.get("squawk") or "") or None,
        None,
        0,
        aircraft.get("category"),
    ]


def _from_adsb(aircraft: list[dict]) -> dict:
    snapshot_ms = int(time.time() * 1000)
    rows = [_adsb_row(item, snapshot_ms) for item in aircraft if item.get("lat") is not None]
    return {"time": snapshot_ms // 1000, "states": [r for r in rows if r[0]]}


def _radius_nm(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> int:
    """Bounding box -> adsb.lol point radius, capped at their 250 nm limit."""
    lat_span = abs(max_lat - min_lat)
    lon_span = abs(max_lon - min_lon)
    return int(max(40, min(250, max(lat_span, lon_span) * 60 * 0.75)))


def flight_states(
    min_lat: Optional[float] = None,
    min_lon: Optional[float] = None,
    max_lat: Optional[float] = None,
    max_lon: Optional[float] = None,
) -> tuple[dict, dict]:
    """Live aircraft snapshot. Returns (payload, response headers).

    A viewport box is answered from adsb.lol, which is keyless, fast and local.
    A worldwide request falls back to OpenSky, which is rate limited, so it is
    cached briefly and allowed to go stale rather than blank the globe.
    """
    if None not in (min_lat, min_lon, max_lat, max_lon):
        centre_lat = (min_lat + max_lat) / 2
        centre_lon = (min_lon + max_lon) / 2
        radius = _radius_nm(min_lat, min_lon, max_lat, max_lon)
        key = f"adsb:{round(centre_lat, 2)}:{round(centre_lon, 2)}:{radius}"
        cached = _cache.get(key, ttl=12)
        if cached is None:
            url = f"https://api.adsb.lol/v2/point/{centre_lat:.4f}/{centre_lon:.4f}/{radius}"
            response = _http().get(url, timeout=_HTTP_TIMEOUT)
            response.raise_for_status()
            cached = _cache.put(key, _from_adsb(response.json().get("ac") or []))
        return cached, {
            "X-Flight-Source": "adsb.lol",
            "X-Flight-Coverage": f"{radius} nm around viewport centre",
            "X-Flight-Radius": str(radius),
        }

    cached = _cache.get("opensky:world", ttl=20)
    if cached is None:
        response = _http().get(
            "https://opensky-network.org/api/states/all", timeout=_HTTP_TIMEOUT
        )
        if response.status_code == 429:
            stale = _cache.get("opensky:world", ttl=600)
            if stale is not None:
                return stale, {
                    "X-Flight-Source": "OpenSky (stale)",
                    "X-Flight-Coverage": "cached",
                    "X-Flight-Radius": "0",
                }
            raise RuntimeError("OpenSky rate limited")
        response.raise_for_status()
        payload = response.json() or {}
        cached = _cache.put(
            "opensky:world", {"time": payload.get("time"), "states": payload.get("states") or []}
        )
    return cached, {
        "X-Flight-Source": "OpenSky Network",
        "X-Flight-Coverage": "worldwide snapshot",
        "X-Flight-Radius": "0",
    }


def military_states() -> dict:
    """Military aircraft from adsb.lol's curated feed.

    Returned raw: the military layer normalises readsb rows (`ac`), unlike the
    flights layer which wants OpenSky state vectors.
    """
    cached = _cache.get("adsb:mil", ttl=30)
    if cached is None:
        response = _http().get("https://api.adsb.lol/v2/mil", timeout=_HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json() or {}
        cached = _cache.put("adsb:mil", {"ac": payload.get("ac") or [], "now": payload.get("now")})
    return cached


# --------------------------------------------------------------------------- #
# Satellites
# --------------------------------------------------------------------------- #


def _tle_blocks(text: str) -> list[tuple[Optional[str], str, str]]:
    """Split raw TLE text into (name, line1, line2) triples, trusting nothing."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    blocks: list[tuple[Optional[str], str, str]] = []

    for index in range(len(lines) - 1):
        first = lines[index]
        second = lines[index + 1]
        if not (first.startswith("1 ") and second.startswith("2 ")):
            continue
        previous = lines[index - 1] if index > 0 else ""
        name = previous if previous and not previous.startswith(("1 ", "2 ")) else None
        blocks.append((name, first, second))

    return blocks


def _celestrak(group: str) -> str:
    url = f"https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
    response = _http().get(url, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    text = response.text
    if not text.strip():
        raise RuntimeError(f"empty TLE payload for {group}")
    return text


def _celestrak_safe(group: str) -> Optional[str]:
    """Fetch one group without letting it take the whole sky down with it.

    One retry, because the common failure is a transient 502 from CelesTrak's
    edge and a second attempt a moment later almost always answers. A refused
    group is not retried: 403 means the catalogue is closed to us, and asking
    twice does not change that.
    """
    for attempt in (1, 2):
        try:
            return _celestrak(group)
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", 0)
            if status in (403, 404) or attempt == 2:
                logger.warning("celestrak group %s refused (%s): %s", group, status, exc)
                return None
            time.sleep(0.4)
        except Exception as exc:  # noqa: BLE001 -- any failure is a missing group
            if attempt == 2:
                logger.warning("celestrak group %s unavailable: %s", group, exc)
                return None
            time.sleep(0.4)
    return None


def tle_group(group: str) -> str:
    """Raw TLE text for a CelesTrak group or composite.

    Single groups are passed through. Composites are fetched in parallel,
    merged, and de-duplicated by NORAD id, because one slow or refused member
    must never empty the sky.
    """
    members = COMPOSITES.get(group)
    if members is None and group not in TLE_GROUPS:
        raise KeyError(group)

    cache_key = f"tle:{group}"
    cached = _cache.get(cache_key, ttl=6 * 3600)
    if cached is not None:
        return cached

    if members is None:
        return _cache.put(cache_key, _celestrak(group))

    merged: dict[str, tuple[Optional[str], str, str]] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for text in pool.map(_celestrak_safe, members):
            if not text:
                continue
            for name, first, second in _tle_blocks(text):
                merged.setdefault(first[2:7], (name, first, second))

    if not merged:
        raise RuntimeError(f"no elements available for composite group {group}")

    lines: list[str] = []
    for key, (name, first, second) in merged.items():
        lines.append(name or f"NORAD {key}")
        lines.append(first)
        lines.append(second)

    return _cache.put(cache_key, "\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Earthquakes
# --------------------------------------------------------------------------- #


def earthquake_feed() -> dict:
    """The last 24 hours of global seismicity, compacted for the globe.

    USGS publishes GeoJSON with far more per-event detail than a dot and a
    ring need. Flattening here keeps the payload an order of magnitude
    smaller and lets the layer stay ignorant of GeoJSON.
    """
    cached = _cache.get("usgs:day", ttl=60)
    if cached is not None:
        return cached

    response = _http().get(
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
        timeout=_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json() or {}

    rows: list[dict[str, Any]] = []
    for feature in payload.get("features") or []:
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        if len(coords) < 2 or coords[0] is None or coords[1] is None:
            continue

        properties = feature.get("properties") or {}
        magnitude = properties.get("mag")
        when = properties.get("time")
        if not isinstance(when, (int, float)):
            continue

        rows.append(
            {
                "id": feature.get("id") or f"{coords[0]:.3f},{coords[1]:.3f},{when}",
                "lon": round(float(coords[0]), 4),
                "lat": round(float(coords[1]), 4),
                "depth": round(float(coords[2]), 1) if len(coords) > 2 and coords[2] is not None else None,
                "mag": magnitude if isinstance(magnitude, (int, float)) else None,
                "place": properties.get("place"),
                "time": int(when),
            }
        )

    return _cache.put(
        "usgs:day",
        {
            "quakes": rows,
            "count": len(rows),
            "source": "USGS",
            "generated": (payload.get("metadata") or {}).get("generated"),
        },
    )


# --------------------------------------------------------------------------- #
# Land
# --------------------------------------------------------------------------- #

# Natural Earth 1:50m land polygons. Public domain, no attribution obligation,
# and the only geography the Orbit lens needs -- because one dataset serves both
# jobs: the polygon fill tints land against sea, and the same rings stroked give
# the coastline. Geometry instead of imagery means no tile server, no licence,
# no per-pan download, and an edge that stays crisp at any zoom.
#
# 1:50m rather than the lighter 1:110m on purpose. Measured: 1:110m is 5,143
# points for the whole planet, which turns the Konkan coast into a handful of
# straight chords the moment anyone leans in. 1:50m is 60,669 points and about
# a megabyte on the wire -- once a month, from a local server. 1:10m (446,175
# points) is the point at which the download stops being free.
LAND_URLS = (
    "https://cdn.jsdelivr.net/gh/nvkelso/natural-earth-vector@v5.1.2/geojson/ne_50m_land.geojson",
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_50m_land.geojson",
)
LAND_TTL = 30 * 24 * 3600
LAND_FILE = "ne_50m_land.json"


def _cache_dir():
    import os
    from pathlib import Path

    explicit = os.environ.get("SATURN_CACHE_DIR")
    if explicit:
        return Path(explicit)
    root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(root) / "Saturn" / "saturn-view"


def _land_from_disk() -> Optional[dict]:
    import json

    path = _cache_dir() / LAND_FILE
    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > LAND_TTL:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if payload.get("polygons") else None
    except Exception as exc:  # noqa: BLE001 -- a corrupt cache is a missing cache
        logger.warning("land cache unreadable: %s", exc)
        return None


def _ring(coords: Any) -> list[list[float]]:
    """One ring, rounded to about 110 m -- finer than the source itself."""
    if not isinstance(coords, list):
        return []
    points = [
        [round(float(point[0]), 3), round(float(point[1]), 3)]
        for point in coords
        if isinstance(point, (list, tuple)) and len(point) >= 2
    ]
    return points if len(points) >= 3 else []


def _land_compact(geojson: dict) -> dict:
    """Flatten GeoJSON into polygons of rings: [[outer, hole, ...], ...]."""
    polygons: list[list[list[list[float]]]] = []

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        kind = node.get("type")
        coords = node.get("coordinates")

        if kind == "Polygon" and isinstance(coords, list):
            rings = [ring for ring in (_ring(part) for part in coords) if ring]
            if rings:
                polygons.append(rings)
        elif kind == "MultiPolygon" and isinstance(coords, list):
            for part in coords:
                walk({"type": "Polygon", "coordinates": part})
        elif kind == "GeometryCollection":
            for child in node.get("geometries") or []:
                walk(child)
        elif kind == "Feature":
            walk(node.get("geometry"))
        elif kind == "FeatureCollection":
            for feature in node.get("features") or []:
                walk(feature)

    walk(geojson)
    return {"source": "Natural Earth 1:110m land (public domain)", "polygons": polygons}


def land_feed() -> dict:
    """World land polygons, cached for a month."""
    cached = _cache.get("naturalearth:land", ttl=LAND_TTL)
    if cached is not None:
        return cached

    stored = _land_from_disk()
    if stored is not None:
        return _cache.put("naturalearth:land", stored)

    last_error: Optional[Exception] = None
    for url in LAND_URLS:
        try:
            response = _http().get(url, timeout=_HTTP_TIMEOUT)
            response.raise_for_status()
            payload = _land_compact(response.json() or {})
            if not payload["polygons"]:
                raise RuntimeError("land payload had no polygons")

            try:
                path = _cache_dir() / LAND_FILE
                path.parent.mkdir(parents=True, exist_ok=True)
                import json

                with path.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, separators=(",", ":"))
            except Exception as exc:  # noqa: BLE001 -- caching is an optimisation
                logger.warning("land cache unwritable: %s", exc)

            return _cache.put("naturalearth:land", payload)
        except Exception as exc:  # noqa: BLE001 -- try the next mirror
            last_error = exc
            logger.warning("land source failed (%s): %s", url.split("/")[2], exc)

    raise RuntimeError(f"no land source answered: {last_error}")


# --------------------------------------------------------------------------- #
# Buildings
# --------------------------------------------------------------------------- #

# Overpass is the public query service over the OpenStreetMap planet. It is
# keyless, and it is the only honest source of real footprints: a generated
# street grid proves a renderer works and proves nothing about a city. Three
# mirrors because the main instance queues under load, and a busy mirror is a
# slow city rather than a missing one.
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)

# Footprints do not move. A month on disk is a cache policy, not a freshness
# claim -- the number that can actually be wrong is the height, which is why
# every response counts how much of the skyline was stated and how much was
# assumed from the building type.
BUILDING_TTL = 30 * 24 * 3600
BUILDING_QUERY_TIMEOUT = 45
BUILDING_MAX = 6000

# One storey, in metres. 3.2 rather than 3.0 because OSM counts the ground
# floor, whose ceiling is higher than the ones stacked above it.
LEVEL_M = 3.2

# Height implied by the building type alone. Medians of what is actually built,
# not measurements -- the payload keeps a separate count for every row that had
# to come from this table.
TYPICAL_HEIGHT_M = {
    "house": 8.0,
    "detached": 8.0,
    "semidetached_house": 8.0,
    "terrace": 9.0,
    "bungalow": 5.0,
    "garage": 3.0,
    "garages": 3.0,
    "garage_boxes": 3.0,
    "shed": 3.0,
    "hut": 3.0,
    "carport": 3.0,
    "roof": 4.0,
    "greenhouse": 4.0,
    "kiosk": 3.0,
    "residential": 13.0,
    "apartments": 20.0,
    "dormitory": 16.0,
    "retail": 8.0,
    "commercial": 15.0,
    "office": 26.0,
    "industrial": 11.0,
    "warehouse": 9.0,
    "factory": 12.0,
    "hangar": 14.0,
    "school": 10.0,
    "university": 15.0,
    "college": 13.0,
    "hospital": 21.0,
    "hotel": 24.0,
    "church": 16.0,
    "cathedral": 32.0,
    "chapel": 11.0,
    "mosque": 15.0,
    "temple": 13.0,
    "shrine": 9.0,
    "stadium": 22.0,
    "sports_hall": 12.0,
    "train_station": 14.0,
    "transportation": 10.0,
    "parking": 12.0,
    "service": 8.0,
    "yes": 9.0,
}


def _length_m(raw: Any) -> Optional[float]:
    """A tag value as metres: '45', '45 m', '148 ft', '45;50'.

    OSM is written by people, so a height is a string with whatever units and
    punctuation the surveyor felt like using. Anything that does not parse is
    not a height, and is left for the next source to answer.
    """
    if raw is None:
        return None

    text = str(raw).strip().lower().replace(",", "")
    if not text:
        return None

    factor = 1.0
    if text.endswith("ft"):
        factor = 0.3048
        text = text[:-2]
    elif text.endswith("feet"):
        factor = 0.3048
        text = text[:-4]
    else:
        for unit in ("metres", "meters", "metre", "meter", "m"):
            if text.endswith(unit):
                text = text[: -len(unit)]
                break

    text = text.strip().strip("'\"").split(";")[0].split()
    if not text:
        return None
    try:
        value = float(text[0]) * factor
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _height_m(tags: dict) -> tuple[float, str]:
    """A footprint's height and where it came from: 'height', 'levels', 'type'."""
    for key in ("height", "building:height"):
        stated = _length_m(tags.get(key))
        if stated is not None and 2.0 <= stated <= 900.0:
            return round(stated, 1), "height"

    levels = _length_m(tags.get("building:levels"))
    if levels is not None and 1.0 <= levels <= 250.0:
        return round(levels * LEVEL_M + 1.0, 1), "levels"

    kind = str(tags.get("building") or "").strip().lower()
    return TYPICAL_HEIGHT_M.get(kind, TYPICAL_HEIGHT_M["yes"]), "type"


def _footprint(geometry: Any) -> list[tuple[float, float]]:
    """Overpass `out geom` node list -> [(lat, lon), ...], open ring."""
    if not isinstance(geometry, list):
        return []

    points: list[tuple[float, float]] = []
    for node in geometry:
        if not isinstance(node, dict):
            continue
        lat, lon = node.get("lat"), node.get("lon")
        if lat is None or lon is None:
            continue
        points.append((float(lat), float(lon)))

    # A closed way repeats its first node; the renderer closes its own ring.
    if len(points) > 1 and points[0] == points[-1]:
        points.pop()

    return points if len(points) >= 3 else []


def _metres_per_degree(lat: float) -> tuple[float, float]:
    """(northing, easting) metres in one degree, at this latitude."""
    return 110_574.0, max(1.0, 111_320.0 * math.cos(math.radians(lat)))


def _area_m2(points: list[tuple[float, float]]) -> float:
    lat0 = sum(point[0] for point in points) / len(points)
    northing, easting = _metres_per_degree(lat0)

    total = 0.0
    for index, (lat1, lon1) in enumerate(points):
        lat2, lon2 = points[(index + 1) % len(points)]
        total += (lon1 * easting) * (lat2 * northing) - (lon2 * easting) * (lat1 * northing)
    return abs(total) / 2.0


def _thin(points: list[tuple[float, float]], tolerance_m: float = 1.4):
    """Drop vertices the eye cannot resolve, keeping every corner that matters.

    A curved facade can carry fifty hand-drawn nodes. At the zoom a city is read
    at, a vertex every 1.4 metres is invisible weight on the wire -- but a plain
    rectangle is already minimal and must come through untouched, which is why
    short rings are returned as they arrived.
    """
    if len(points) < 6:
        return points

    lat0 = sum(point[0] for point in points) / len(points)
    northing, easting = _metres_per_degree(lat0)
    tol_lat = tolerance_m / northing
    tol_lon = tolerance_m / easting

    kept = [points[0]]
    for point in points[1:]:
        previous = kept[-1]
        if abs(point[0] - previous[0]) >= tol_lat or abs(point[1] - previous[1]) >= tol_lon:
            kept.append(point)

    return kept if len(kept) >= 3 else points


def _building_rows(elements: list, precision: int):
    """[(height, origin, [height, lon, lat, lon, lat, ...]), ...]."""
    rows: list[tuple[float, str, list[float]]] = []

    for element in elements:
        if not isinstance(element, dict):
            continue

        tags = element.get("tags")
        if not isinstance(tags, dict) or not tags:
            continue

        points = _footprint(element.get("geometry"))
        if not points:
            continue

        # A bus shelter is not a volume. Below six square metres the extrusion
        # is a pixel of noise on every frame and never a place.
        if _area_m2(points) < 6.0:
            continue

        height, origin = _height_m(tags)
        row = [height]
        for lat, lon in _thin(points):
            row.append(round(lon, precision))
            row.append(round(lat, precision))
        rows.append((height, origin, row))

    return rows


def _buildings_key(south: float, west: float, north: float, east: float) -> str:
    # The `b2_` prefix is the row format, not a version number for its own
    # sake: a cache written before the per-building evidence codes existed has
    # no `origins` in it, and silently reading one would turn every stated
    # height into a guess on the drawing. A new prefix retires the old files.
    return f"b2_{south:.3f}_{west:.3f}_{north:.3f}_{east:.3f}"


def _buildings_from_disk(key: str) -> Optional[dict]:
    import json

    path = _cache_dir() / f"{key}.json"
    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > BUILDING_TTL:
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload.get("buildings"), list) else None
    except Exception as exc:  # noqa: BLE001 -- a corrupt cache is a missing cache
        logger.warning("building cache unreadable: %s", exc)
        return None


def _overpass(query: str) -> dict:
    """Ask every mirror in turn until one answers with a document."""
    last_error: Optional[Exception] = None

    for url in OVERPASS_ENDPOINTS:
        try:
            response = _http().post(
                url, data={"data": query}, timeout=BUILDING_QUERY_TIMEOUT
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("no document in reply")
            return payload
        except Exception as exc:  # noqa: BLE001 -- a busy mirror is not a dead one
            last_error = exc
            logger.warning("overpass %s refused: %s", url.split("/")[2], exc)

    raise RuntimeError(f"no Overpass mirror answered: {last_error}")


# The planet's own map endpoint. Measured against Overpass on the same window:
# 4.3 seconds for 3.3 km of central Mumbai and 2,014 buildings, where Overpass
# took 22 seconds for eight buildings in a 300 metre box -- its cost is the
# queue, not the query, and no smaller question gets a faster answer. This one
# has no queue and a hard ceiling: fifty thousand nodes per response.
#
# 0.025 degrees rather than the 0.030 that also sometimes fits. Central Mumbai
# at 0.030 answered once and was refused the next time on a box a few hundred
# metres away; a limit that is usually survivable is not a limit, and the cost
# of crossing it is a 39 second fallback. 0.025 is 2.8 km and held on every try.
OSM_MAP_URL = "https://api.openstreetmap.org/api/0.6/map"
OSM_MAP_MAX_SPAN = 0.025
OSM_MAP_TIMEOUT = 30


def _map_api_elements(text: str) -> list[dict]:
    """OSM XML -> rows in Overpass' `out geom` shape.

    One parser downstream instead of two: the caller cannot tell which source
    answered, and the layer never learns that there is more than one.

    Streamed rather than loaded, because the document carries every road, fence
    and post box in the window as well as the buildings, and at ten megabytes
    the intermediate tree is the most expensive thing in the request.
    """
    nodes: dict[str, tuple[str, str]] = {}
    ways: list[dict] = []

    for event, element in ET.iterparse(io.StringIO(text), events=("end",)):
        if element.tag == "node":
            node_id = element.get("id")
            if node_id is not None:
                nodes[node_id] = (element.get("lat"), element.get("lon"))
            element.clear()
        elif element.tag == "way":
            tags: dict[str, str] = {}
            refs: list[str] = []
            for child in element:
                if child.tag == "nd":
                    ref = child.get("ref")
                    if ref is not None:
                        refs.append(ref)
                elif child.tag == "tag":
                    key = child.get("k")
                    if key is not None:
                        tags[key] = child.get("v") or ""

            if tags.get("building"):
                geometry = []
                for ref in refs:
                    node = nodes.get(ref)
                    if node is None or node[0] is None or node[1] is None:
                        continue
                    geometry.append({"lat": float(node[0]), "lon": float(node[1])})
                if geometry:
                    ways.append({"geometry": geometry, "tags": tags})
            element.clear()

    return ways


def _elements_for(south: float, west: float, north: float, east: float, span: float) -> list:
    """Footprints for a window, from whichever source can answer it best."""
    if span <= OSM_MAP_MAX_SPAN:
        mid_lat = (south + north) / 2
        mid_lon = (west + east) / 2

        # The node ceiling is a property of the district, not of the request:
        # a quiet suburb is fine at 2.8 km and the middle of Mumbai is not.
        # Halving the box costs one extra fast attempt and still beats falling
        # through to a mirror that will make the operator wait half a minute.
        for shrink in (1.0, 0.5):
            half_lat = (north - south) * shrink / 2
            half_lon = (east - west) * shrink / 2
            box = (
                f"{mid_lon - half_lon:.5f},{mid_lat - half_lat:.5f},"
                f"{mid_lon + half_lon:.5f},{mid_lat + half_lat:.5f}"
            )
            try:
                response = _http().get(f"{OSM_MAP_URL}?bbox={box}", timeout=OSM_MAP_TIMEOUT)
                response.raise_for_status()
                if shrink < 1.0:
                    logger.info("footprints answered for a window half the size asked")
                return _map_api_elements(response.text)
            except Exception as exc:  # noqa: BLE001 -- try smaller, then elsewhere
                logger.warning("osm map refused %s: %s", box, exc)

    query = (
        "[out:json][timeout:25]"
        f"[bbox:{south:.4f},{west:.4f},{north:.4f},{east:.4f}];"
        'way["building"];'
        "out geom;"
    )
    return _overpass(query).get("elements") or []


def building_feed(south: float, west: float, north: float, east: float) -> dict:
    """Real OpenStreetMap footprints for a window, with honest heights.

    The window is quantised to three decimal places -- about a hundred metres --
    before anything else happens, so a camera that drifts by a street is
    answered from the tile already on disk instead of asking a public server the
    same question twice.

    Rows are `[height, lon, lat, lon, lat, ...]`: the ring is flat because the
    layer hands it straight to the renderer, and a nested array of two-number
    pairs costs half the payload again in brackets.
    """
    south = max(-85.0, min(85.0, south))
    north = max(-85.0, min(85.0, north))
    if north < south:
        south, north = north, south
    if east < west:
        west, east = east, west
    east = min(180.0, west + min(3.0, east - west))

    south, west = round(south, 3), round(west, 3)
    north, east = round(north, 3), round(east, 3)

    span = max(north - south, east - west)
    if span <= 0:
        raise ValueError("empty window")

    key = _buildings_key(south, west, north, east)

    cached = _cache.get(f"osm2:{key}", ttl=3600)
    if cached is not None:
        return cached

    stored = _buildings_from_disk(key)
    if stored is not None:
        return _cache.put(f"osm2:{key}", stored)

    elements = _elements_for(south, west, north, east, span)

    # Precision scales with the window: a five-kilometre street view is read at
    # under a metre per pixel and wants six decimals; a thirty-kilometre
    # overview is read at ten and a 1.1 m grid is payload for nothing.
    precision = 6 if span <= 0.05 else 5

    parsed = _building_rows(elements, precision)
    truncated = len(parsed) > BUILDING_MAX
    if truncated:
        # Keep the tallest. Losing a shed is losing nothing; losing a tower
        # changes the skyline.
        parsed.sort(key=lambda item: item[0], reverse=True)
        parsed = parsed[:BUILDING_MAX]

    stated = {"height": 0, "levels": 0, "type": 0}
    rows: list[list[float]] = []
    # One code per row, in the same order, so the drawing can say which
    # skyline it is looking at: 2 and 1 are stated, 0 is inferred. Kept out of
    # the rows themselves because the row is a flat ring of coordinates and a
    # code in the middle of it would be off by one forever.
    evidence = {"height": 2, "levels": 1, "type": 0}
    origins: list[int] = []
    for _height, origin, row in parsed:
        stated[origin] = stated.get(origin, 0) + 1
        origins.append(evidence.get(origin, 0))
        rows.append(row)

    result = {
        "buildings": rows,
        "origins": origins,
        "count": len(rows),
        "truncated": truncated,
        "stated": stated,
        "bounds": [south, west, north, east],
        "precision": precision,
        "source": "OpenStreetMap",
    }

    try:
        import json

        path = _cache_dir() / f"{key}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(result, handle, separators=(",", ":"))
    except Exception as exc:  # noqa: BLE001 -- caching is an optimisation
        logger.warning("building cache unwritable: %s", exc)

    return _cache.put(f"osm2:{key}", result)
