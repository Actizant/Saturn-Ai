"""Saturn's View -- the seam between Saturn's map tool and the globe.

``geospatial_globe`` has been Saturn's map verb since before the engine existed,
and it still points at ``core/globe_window.py``, the prototype that came first.
This module is where the two meet: the verbs the engine answers itself go to
Saturn's View, and the handful it cannot yet answer keep going to the old
window, so switching the tool over does not quietly delete a capability.

The split is deliberate and it is stated as data rather than as a comment,
because the honest answer to "should the assistant open the new globe or the
old one" changes as layers are written, and it should change in one place.

Two rules hold everywhere in here. Nothing is claimed that the engine did not
say: every figure in a returned sentence comes out of ``state()`` or a layer's
own readout, because a voice assistant that reports "38 aircraft" has to have
heard 38 from somewhere. And a call that did not answer is reported as not
having answered, not as a failure -- the difference matters on a page that is
sometimes busy encoding a coastline.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("saturn.view.tool")

# What the engine can draw, and what it cannot yet. A verb in here is answered
# by the old globe, which is why the tool can be switched over today without
# losing routes, driving directions, weather, places or radar.
LEGACY_VERBS = frozenset({"route", "drive", "weather", "nearby", "radar"})

#: The lenses, in the order the rail shows them. Only a fallback: the engine
#: is asked first, so a lens added to a theme is answerable the same day.
LENSES = ("orbit", "relief", "signal")

#: The live layers, same caveat. ``quakes`` is what the rail calls earthquakes.
LAYERS = ("flights", "satellites", "quakes", "city")

_ALIASES = {
    # Both numbers of every name, because "show me the earthquake" and "show
    # me earthquakes" are the same request said by two different people, and an
    # alias table that only covers one of them is a table with a hole in it.
    "earthquake": "quakes",
    "earthquakes": "quakes",
    "quake": "quakes",
    "seismic": "quakes",
    "aircraft": "flights",
    "flight": "flights",
    "flights": "flights",
    "satellite": "satellites",
    "satellites": "satellites",
    "iss": "satellites",
    "station": "satellites",
    "building": "city",
    "buildings": "city",
    "register": "city",
    "cityregister": "city",
    "city": "city",
}

#: An article is not part of a layer's name, but it is very much part of a
#: sentence a person says, and the argument arrives here having been lifted out
#: of one: "the city" has to reach the city layer as "city".
_ARTICLE = re.compile(r"^(?:the|a|an)[\s_-]+")


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _height(args: dict) -> Optional[float]:
    """An altitude in metres, or None for the host's own default.

    Named places arrive with an altitude attached, which is what puts a request
    for "the buildings at Mumbai" into the register's window at all: the city
    layer draws below about a kilometre and says so above it.
    """
    raw = args.get("height") or args.get("altitude")
    if raw in (None, ""):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _layer_id(raw: Any) -> Optional[str]:
    """A spoken layer name, resolved to an engine id -- or None if nothing came.

    Returns None rather than an empty string because "no name given" and "a name
    the engine does not have" are different answers: the first lists the layers,
    the second corrects the caller with the truth.
    """
    # The article goes first: once the spaces are gone "the city" is "thecity",
    # and the word "the" no longer has a boundary to be recognised at.
    name = _ARTICLE.sub("", _clean(raw).lower())
    name = name.replace(" ", "").replace("_", "").replace("-", "")
    if not name:
        return None
    return _ALIASES.get(name, name)


def _known_layers(view) -> list:
    """What the engine says it has, falling back to the last known list.

    Asked rather than assumed so that a layer written tomorrow is answerable
    tomorrow, and so a wrong name can be corrected with the truth instead of
    with a tuple that went stale the day it was written.
    """
    try:
        ids = [str(layer.get("id")) for layer in view.layers() if layer.get("id")]
    except Exception:  # noqa: BLE001 -- an unanswered call is not an error here
        ids = []
    return ids or list(LAYERS)


def _known_lenses(view) -> list:
    try:
        names = [str(name) for name in (view.state().get("lenses") or []) if name]
    except Exception:  # noqa: BLE001
        names = []
    return names or list(LENSES)


#: What the rail prints in an age cell before that layer has been read at all.
#: An em-dash is not a duration, and "4s old" and "-- old" are the two shapes of
#: the same field, so the placeholder has to be dropped rather than spoken.
_PLACEHOLDER_AGE = frozenset({"", "-", "--", "\u2014", "\u2013", "unknown", "n/a", "na", "never"})


def _age(layer: dict) -> str:
    fresh = layer.get("freshness") or {}
    raw = _clean(fresh.get("age"))
    return "" if raw.lower() in _PLACEHOLDER_AGE else raw


def _figure(layer: dict) -> str:
    """One layer's own line, from the state already in hand.

    Takes the row rather than the id because the whole report is built out of
    one ``state()`` call; asking the page again per layer would be a round trip
    for a fact already held, and on a page that is busy it might not answer.
    """
    layer_id = str(layer.get("id") or "layer")
    title = _clean(layer.get("title")) or layer_id
    if not layer.get("enabled"):
        return f"{title} is switched off."

    readout = _clean(layer.get("readout"))
    age = _age(layer)
    if readout:
        return f"{title}: {readout}" + (f", {age} old." if age else ".")
    if age:
        return f"{title}: last read {age} ago, no figure given."
    return f"{title} is on and has not answered yet."


# -------------------------------------------------------------------------- #
# The verbs Saturn's View answers itself
# -------------------------------------------------------------------------- #


def _await_figure(view, layer_id: str, seconds: float = 6.0) -> dict:
    """Wait a little for a layer to say something about itself.

    The old verbs promised a number -- "showing 12 aircraft" -- and a tool that
    now answers "layer on" would be a downgrade dressed as a rewrite. So the
    layer is asked, and asked again, until it publishes a readout or the budget
    runs out. It is a poll of the cheapest kind, one ``layers()`` call, and a
    layer that has not answered in six seconds is reported as not having
    answered rather than waited on forever: the feed's own backoff will get it.
    """
    deadline = time.monotonic() + seconds
    layer: dict = {}
    while True:
        layer = next(
            (item for item in view.layers() if item.get("id") == layer_id), {}
        )
        if layer.get("readout") or time.monotonic() >= deadline:
            return layer
        time.sleep(0.4)


def _show_layer(view, args: dict, layer_id: str, *, wait: float = 6.0) -> str:
    """Switch a layer on and report the figure it publishes, in its own words."""
    enabled = view.set_layer(layer_id, True)
    if not enabled:
        return f"Saturn's View would not switch the {layer_id} layer on."

    layer = _await_figure(view, layer_id, wait)
    figure = _figure(layer or {"id": layer_id, "enabled": True})
    return figure


def _flights(view, args: dict) -> str:
    """The old verb, which counted the aircraft rather than merely drawing them."""
    place = _clean(args.get("location"))
    if place and place.lower() not in ("current", "here", "me"):
        view.fly_to(place, height=_height(args) or 1_200_000.0)
    return "Live aircraft. " + _show_layer(view, args, "flights")


def _quakes(view, args: dict) -> str:
    return "Earthquakes. " + _show_layer(view, args, "quakes")


def _iss(view, args: dict) -> str:
    """The station, from the satellite layer's own pass prediction."""
    return "Satellites. " + _show_layer(view, args, "satellites")


def _open(view, args: dict) -> str:
    location = _clean(args.get("location"))
    result = view.open(location or None, height=_height(args))

    error = _clean(result.get("error"))
    if error:
        return f"Saturn's View could not start: {error}"

    where = _clean(result.get("location"))
    return (
        f"Opened Saturn's View over {where}."
        if where
        else "Opened Saturn's View."
    )


def _show(view, args: dict) -> str:
    state = view.show_window()
    return "Saturn's View is on screen." if state.get("visible") else "Saturn's View stayed hidden."


def _hide(view, args: dict) -> str:
    view.close_window()
    return (
        "Put Saturn's View away. It keeps its layers and their ages, "
        "so it comes back exactly as it was."
    )


def _fly(view, args: dict) -> str:
    place = _clean(args.get("location")) or _clean(args.get("place"))
    if not place or place.lower() in ("current", "here", "me"):
        state = view.state()
        view_now = state.get("view") or {}
        if view_now:
            return (
                f"The camera is already over {view_now.get('lat', 0):.1f}, "
                f"{view_now.get('lon', 0):.1f} at {view_now.get('height', 0) / 1000:.0f} km."
            )
        return "Nothing to fly to; the view has not reported where it is."

    height = _height(args)
    arrived = view.fly_to(place, height=height)
    label = _clean(arrived.get("location")) or place
    metres = arrived.get("height")
    if not isinstance(metres, (int, float)):
        return f"Saturn's View is looking at {label}."
    if metres < 20_000:
        return f"Saturn's View is looking at {label}, {metres / 1000:.1f} km up."
    return f"Saturn's View is looking at {label}, {metres / 1000:.0f} km up."


def _home(view, args: dict) -> str:
    view.home()
    return "Returned the camera to its home position."


def _lens(view, args: dict) -> str:
    names = _known_lenses(view)
    name = _clean(args.get("lens") or args.get("name")).lower()
    if not name:
        state = view.state()
        return f"The lens is {state.get('lens', 'unknown')}. The lenses are {', '.join(names)}."
    if name not in names:
        return f"There is no {name} lens. The lenses are {', '.join(names)}."

    view.set_lens(name)
    # setLens() answers, and the answer is not the fact that matters. The lens
    # that is on is whatever the engine now says it is on, so the state is read
    # back rather than the request being repeated -- a lens switch behind a busy
    # coastline is exactly the call that comes back looking like it worked.
    now = _clean((view.state() or {}).get("lens")).lower()
    if now == name:
        return f"Switched to the {name} lens."
    if not now:
        return f"Asked for the {name} lens, and the view has not said which one is on."
    return f"Asked for the {name} lens; the view says it is on {now}."


def _layer(view, args: dict) -> str:
    known = _known_layers(view)
    layer_id = _layer_id(args.get("layer") or args.get("name") or args.get("action_target"))
    if not layer_id or layer_id not in known:
        state = view.state()
        on = [layer.get("id") for layer in state.get("layers") or [] if layer.get("enabled")]
        if layer_id:
            return f"Saturn's View has no {layer_id} layer. The layers are {', '.join(known)}."
        return (
            f"Layers: {', '.join(known)}. Currently on: "
            + (", ".join(str(item) for item in on) if on else "none.")
        )

    raw = args.get("enable")
    if raw is None:
        raw = not _clean(args.get("set") or args.get("state")).lower() in ("off", "false", "0")
    on = bool(raw)

    enabled = view.set_layer(layer_id, on)
    if enabled == on:
        return f"{layer_id.capitalize()} layer {'on' if on else 'off'}."
    return (
        f"Asked for the {layer_id} layer "
        f"{'on' if on else 'off'} and the view says it is "
        f"{'on' if enabled else 'off'}."
    )


def _refresh(view, args: dict) -> str:
    known = _known_layers(view)
    layer_id = _layer_id(args.get("layer")) or known[0]
    if not view.refresh_layer(layer_id):
        return f"The {layer_id} layer is not running, so there is nothing to refresh."
    return f"Asked the {layer_id} layer to poll now."


def _read(view, args: dict) -> str:
    """Say what the instrument is actually showing, in its own numbers."""
    state = view.state()
    if not state or not state.get("ready"):
        return "Saturn's View is not running."

    lines = [view.describe()]

    if _clean(args.get("detail")).lower() in ("all", "layers", "true", "1"):
        for layer in state.get("layers") or []:
            lines.append(_figure(layer))

    metrics = state.get("metrics") or {}
    frame = metrics.get("lastFrame")
    if frame is not None:
        lines.append(f"Frame cost {frame:.1f} ms at {metrics.get('fps', 0):.0f} fps.")

    return " ".join(line for line in lines if line)


def _capture(view, args: dict) -> str:
    path = _clean(args.get("path"))
    if not path:
        path = str(Path(tempfile.gettempdir()) / "saturn_view.png")

    if view.capture(path):
        return f"Wrote what Saturn's View shows to {path}."
    return "Saturn's View could not be photographed."


HANDLERS: dict[str, Callable[..., str]] = {
    "open": _open,
    "show": _show,
    "close": _hide,
    "hide": _hide,
    "fly_to": _fly,
    "location": _fly,
    "focus": _fly,
    "look": _fly,
    "home": _home,
    "lens": _lens,
    "layer": _layer,
    "refresh": _refresh,
    "read": _read,
    "describe": _read,
    "status": _read,
    "inspect": _read,
    "capture": _capture,
    "screenshot": _capture,
    # The verbs the old tool already had, which asked for a count as well as a
    # drawing. Kept by name so a sentence learned last year still works.
    "flights": _flights,
    "earthquakes": _quakes,
    "iss": _iss,
}


# -------------------------------------------------------------------------- #
# The verbs the old globe still owns
# -------------------------------------------------------------------------- #


def _legacy(action: str, args: dict, parent: Any) -> str:
    """Answer from ``core/globe_window.py``, in the words the tool used before.

    Kept this way on purpose: routes, driving directions, weather, nearby
    places and the radar overlay are real features that people ask for, and an
    engine that cannot draw a great circle is not a reason to stop answering.
    When the last of these verbs gets a layer, its branch goes away.
    """
    from core.globe_window import GlobeWindow

    try:
        globe = GlobeWindow.get_instance(parent=parent)
    except Exception as exc:  # noqa: BLE001
        return f"The map engine that handles {action} is not available: {exc}"

    location = _clean(args.get("location")) or "current"
    try:
        if action == "route":
            result = globe.show_route(args.get("origin", ""), args.get("destination", ""))
            return (
                f"Flight route: {result.get('origin')} to {result.get('destination')}, "
                f"{result.get('distance_km')} km, about {result.get('flight_time')}."
            )
        if action == "drive":
            result = globe.show_driving_route(args.get("origin", ""), args.get("destination", ""))
            return (
                f"Driving route: {result.get('origin')} to {result.get('destination')}, "
                f"{result.get('distance_km')} km, {result.get('duration_str')}."
            )
        if action == "weather":
            result = globe.show_weather(location)
            return f"Weather for {result.get('location')}: {result.get('weather')}."
        if action == "nearby":
            found = globe.show_nearby(args.get("query") or "hospitals", args.get("location"))
            return f"Found {len(found)} nearby places."
        if action == "radar":
            enabled = bool(args.get("enable", True))
            globe.toggle_weather_radar(enabled)
            return f"Weather radar {'enabled' if enabled else 'disabled'}."
    except Exception as exc:  # noqa: BLE001
        logger.exception("legacy map verb %s failed", action)
        return f"The {action} request failed: {exc}"

    return f"Unsupported map action: {action}."


# -------------------------------------------------------------------------- #


def geospatial_globe(parameters: Optional[dict] = None, *, parent: Any = None) -> str:
    """Answer Saturn's map tool, from any thread.

    The window is built on the GUI thread through the host's factory, because
    tool calls arrive on the asyncio loop and a widget constructed on the wrong
    thread is a crash that looks like a mystery.
    """
    args = dict(parameters or {})
    action = _clean(args.get("action")).lower() or "open"

    if action in LEGACY_VERBS:
        return _legacy(action, args, parent)

    handler = HANDLERS.get(action)
    if handler is None:
        # Answered before the host is imported: a mistyped verb is a vocabulary
        # problem, and spinning up Qt WebEngine to report one would be an
        # expensive way to say "no".
        return (
            f"Saturn's View has no {action} action. It answers "
            f"{', '.join(sorted(set(HANDLERS)))}, and the map engine still "
            f"answers {', '.join(sorted(LEGACY_VERBS))}."
        )

    from saturn_view import window as view_host

    view = view_host.get_view(parent=parent)
    if view is None:
        return (
            "Saturn's View is not available on this machine right now, "
            "so nothing was drawn."
        )

    try:
        return handler(view, args)
    except Exception as exc:  # noqa: BLE001 -- a tool call must answer, not raise
        logger.exception("map action %s failed", action)
        return f"Saturn's View did not answer the {action} request: {exc}"
