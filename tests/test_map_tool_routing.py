"""The map tool's vocabulary, and what it promises when the engine stays quiet.

None of this needs a window. The router is tested against a fake view, and the
legacy branch against a stubbed module, because the two failure modes worth
catching are both in the mapping: a verb the assistant was told about that the
tool cannot answer, and a sentence that reports a number the engine never gave.
"""

import re
import sys
import types
from pathlib import Path

import pytest

from saturn_view import tool

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# A view that answers, and one that does not
# --------------------------------------------------------------------------- #


class FakeView:
    """The surface ``tool.py`` uses, with every reply scripted."""

    def __init__(self, layers=None, state=None, publishes=True, follows=True):
        self._layers = layers if layers is not None else []
        self._state = state or {"ready": True, "lens": "orbit", "layers": self._layers}
        self._publishes = publishes
        # Whether the view actually does what it is told. A real page updates
        # the lens it reports; one that is busy, or dead, keeps saying whatever
        # it last said. `tool._lens` reads the difference out loud, so the
        # double has to be able to express both.
        self._follows = follows
        self.calls = []

    def open(self, location=None, height=None):
        self.calls.append(("open", location, height))
        return {"location": location or "the Pacific"}

    def show_window(self):
        self.calls.append(("show_window",))
        return {"visible": True}

    def close_window(self):
        self.calls.append(("close_window",))

    def fly_to(self, place, height=None):
        self.calls.append(("fly_to", place, height))
        return {"location": place, "height": height or 900_000.0}

    def home(self):
        self.calls.append(("home",))

    def set_lens(self, name):
        self.calls.append(("set_lens", name))
        if self._follows:
            self._state["lens"] = name
        return name

    def set_layer(self, layer_id, enable):
        self.calls.append(("set_layer", layer_id, enable))
        for layer in self._layers:
            if layer.get("id") == layer_id:
                layer["enabled"] = enable
        return enable

    def layers(self):
        self.calls.append(("layers",))
        if not self._publishes:
            # On, running, and giving nothing back -- the case that matters.
            return [{**layer, "readout": ""} for layer in self._layers]
        return list(self._layers)

    def refresh_layer(self, layer_id):
        self.calls.append(("refresh_layer", layer_id))
        return any(layer.get("id") == layer_id for layer in self._layers)

    def state(self):
        self.calls.append(("state",))
        return self._state

    def describe(self):
        return "The camera is over the Pacific at 900 km."

    def capture(self, path):
        self.calls.append(("capture", path))
        return path


@pytest.fixture
def fast_clock(monkeypatch):
    """A clock that advances on sleep, so a six-second budget costs nothing.

    Patching time rather than shortening the budget keeps the boundary under
    test the one production uses.
    """

    class Synthetic:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Synthetic()
    monkeypatch.setattr(tool, "time", clock)
    return clock


# --------------------------------------------------------------------------- #
# Aliases: what people say, mapped to what the engine is called
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "spoken, layer_id",
    [
        ("earthquakes", "quakes"),
        ("Earthquake", "quakes"),
        ("seismic", "quakes"),
        ("iss", "satellites"),
        ("ISS", "satellites"),
        ("satellite", "satellites"),
        ("buildings", "city"),
        ("the city", "city"),
        ("The Buildings", "city"),
        ("city_register", "city"),
        ("aircraft", "flights"),
        ("flight", "flights"),
        ("tides", "tides"),
        ("", None),
        (None, None),
        ("   ", None),
    ],
)
def test_spoken_names_reach_the_engine(spoken, layer_id):
    assert tool._layer_id(spoken) == layer_id


def test_no_name_and_a_wrong_name_are_different_answers():
    """One lists what exists, the other corrects the caller. Collapsing them
    would make "which layers are there?" answer "there is no layer"."""
    view = FakeView(layers=[{"id": "city", "enabled": False}],
                    state={"ready": True, "layers": [{"id": "city", "enabled": False}]})
    assert "Layers:" in tool._layer(view, {})
    assert "no tides layer" in tool._layer(view, {"layer": "tides"})


def test_the_two_verb_sets_do_not_overlap():
    """A verb cannot be owned by both globes; the split has to stay honest."""
    assert not set(tool.HANDLERS) & tool.LEGACY_VERBS


def test_height_parses_and_refuses_nonsense():
    assert tool._height({"height": 400}) == 400.0
    assert tool._height({"altitude": "1200"}) == 1200.0
    assert tool._height({}) is None
    assert tool._height({"height": "the sky"}) is None
    assert tool._height({"height": -5}) is None, "a camera underground is not an altitude"


# --------------------------------------------------------------------------- #
# Honesty: no figure the engine did not say
# --------------------------------------------------------------------------- #


def test_a_layer_that_answers_is_reported_in_its_own_words():
    view = FakeView(
        layers=[
            {
                "id": "flights",
                "title": "Aircraft",
                "enabled": True,
                "readout": "38 in view",
                "freshness": {"age": "4s"},
            }
        ]
    )
    assert tool._flights(view, {}) == (
        "Live aircraft. Aircraft: 38 in view, 4s old."
    )


def test_a_layer_that_stays_quiet_says_so_instead_of_inventing_a_count(fast_clock):
    view = FakeView(
        layers=[{"id": "flights", "title": "Aircraft", "enabled": True}],
        publishes=False,
    )
    said = tool._flights(view, {})
    assert "38" not in said
    assert "has not answered yet" in said
    assert fast_clock.now >= 6.0, "the whole budget should be spent before giving up"


def test_an_age_placeholder_is_not_read_out_as_a_duration():
    """The rail puts an em-dash in an age cell it has not filled. A voice that
    says "no contacts, -- old" is a voice making the placeholder into a fact.
    """
    for placeholder in ("--", "-", "\u2014", "unknown", "never", ""):
        layer = {
            "id": "flights", "title": "Aircraft", "enabled": True,
            "readout": "no contacts", "freshness": {"age": placeholder},
        }
        assert tool._figure(layer) == "Aircraft: no contacts.", placeholder

    # A real age is still reported, because it is the useful half of the line.
    layer["freshness"] = {"age": "4s"}
    assert tool._figure(layer) == "Aircraft: no contacts, 4s old."

    # And "just now" is an age, not a placeholder -- the table stays small.
    layer["freshness"] = {"age": "just now"}
    assert tool._figure(layer) == "Aircraft: no contacts, just now old."


def test_a_layer_that_is_off_is_not_described_as_empty():
    view = FakeView(layers=[{"id": "quakes", "title": "Quakes", "enabled": False}])
    assert tool._figure(view._layers[0]) == "Quakes is switched off."


def test_read_can_walk_every_layer_from_one_state_call():
    view = FakeView(
        state={
            "ready": True,
            "layers": [
                {"id": "flights", "title": "Aircraft", "enabled": True,
                 "readout": "38 in view", "freshness": {"age": "4s"}},
                {"id": "city", "title": "Register", "enabled": False},
            ],
            "metrics": {"lastFrame": 7.7, "fps": 130},
        }
    )
    said = tool._read(view, {"detail": "layers"})
    assert "Aircraft: 38 in view, 4s old." in said
    assert "Register is switched off." in said
    assert "Frame cost 7.7 ms at 130 fps." in said
    # One state() call for the whole report: no per-layer round trip.
    assert [call for call in view.calls if call == ("layers",)] == []


def test_lens_names_are_asked_not_assumed():
    view = FakeView(state={"ready": True, "lens": "orbit", "lenses": ["orbit", "relief"]})
    assert tool._lens(view, {"lens": "deep"}) == (
        "There is no deep lens. The lenses are orbit, relief."
    )
    assert tool._lens(view, {"lens": "relief"}) == "Switched to the relief lens."


def test_a_lens_that_was_not_taken_is_reported_as_taken_not_switched():
    # The page accepted the call and then said nothing about it. Claiming the
    # switch happened would be the tool describing a thing it did not observe.
    view = FakeView(
        state={"ready": True, "lens": "orbit", "lenses": ["orbit", "relief"]},
        follows=False,
    )
    assert tool._lens(view, {"lens": "relief"}) == (
        "Asked for the relief lens; the view says it is on orbit."
    )


def test_a_view_that_stops_reporting_which_lens_says_so():
    # A page that is drawing but has never published a lens. Following=False
    # keeps it from inventing one by accepting the call, which is exactly the
    # hole this branch exists for: no answer is not the same as "failed".
    view = FakeView(
        state={"ready": True, "lenses": ["orbit", "relief"]},
        follows=False,
    )
    assert tool._lens(view, {"lens": "relief"}) == (
        "Asked for the relief lens, and the view has not said which one is on."
    )


def test_an_unknown_layer_is_corrected_with_the_truth():
    view = FakeView(layers=[{"id": "flights", "enabled": True}],
                    state={"ready": True, "layers": [{"id": "flights", "enabled": True}]})
    before = view.layers()
    assert "no tides layer" in tool._layer(view, {"layer": "tides"})
    assert [layer["id"] for layer in before] == ["flights"], "unchanged"


# --------------------------------------------------------------------------- #
# Routing: which globe gets which verb
# --------------------------------------------------------------------------- #


def test_legacy_verbs_go_to_the_old_globe(monkeypatch):
    """``route`` must still work, and must not drag Qt into the test."""
    seen = {}

    class StubGlobe:
        def show_route(self, origin, destination):
            seen["route"] = (origin, destination)
            return {"origin": origin, "destination": destination,
                    "distance_km": 1400, "flight_time": "2h 20m"}

        def show_driving_route(self, origin, destination):
            return {"origin": origin, "destination": destination,
                    "distance_km": 1500, "duration_str": "1 day"}

        def show_weather(self, location):
            return {"location": location, "weather": "clear, 24C"}

        def show_nearby(self, query, location):
            return [1, 2, 3]

        def toggle_weather_radar(self, enable):
            seen["radar"] = enable

    class GlobeWindow:
        @classmethod
        def get_instance(cls, parent=None):
            return StubGlobe()

    stub = types.ModuleType("core.globe_window")
    stub.GlobeWindow = GlobeWindow
    monkeypatch.setitem(sys.modules, "core.globe_window", stub)

    assert tool.geospatial_globe(
        {"action": "route", "origin": "Delhi", "destination": "Mumbai"}
    ) == "Flight route: Delhi to Mumbai, 1400 km, about 2h 20m."
    assert seen["route"] == ("Delhi", "Mumbai")
    assert tool.geospatial_globe({"action": "drive", "origin": "A", "destination": "B"}).startswith(
        "Driving route: A to B, 1500 km"
    )
    assert tool.geospatial_globe({"action": "weather", "location": "Pune"}) == (
        "Weather for Pune: clear, 24C."
    )
    assert tool.geospatial_globe({"action": "nearby", "query": "fuel"}) == (
        "Found 3 nearby places."
    )
    assert tool.geospatial_globe({"action": "radar", "enable": False}) == (
        "Weather radar disabled."
    )
    assert seen["radar"] is False


def test_an_unknown_verb_is_answered_without_opening_a_window():
    """The refusal comes back before the host is imported, and never raises."""
    said = tool.geospatial_globe({"action": "tidal_bore"})
    assert "no tidal_bore action" in said
    assert "Saturn's View is not running" not in said
    # The point of the message is to teach the caller the real vocabulary.
    for verb in ("fly_to", "capture", "route"):
        assert verb in said


def test_no_action_at_all_means_open_the_view(monkeypatch):
    """"Show me the globe" carries no verb, and must land on ``open`` rather
    than on the unknown-verb message. The host is stubbed, so no window is
    built -- which is also the only way to know the router reached the factory
    through the module rather than through a copy of it.
    """
    import saturn_view

    view = FakeView()
    stub = types.ModuleType("saturn_view.window")
    stub.get_view = lambda parent=None, **kwargs: view
    monkeypatch.setitem(sys.modules, "saturn_view.window", stub)
    # ``from saturn_view import window`` prefers a package attribute over the
    # sys.modules entry, so both have to be pointed at the stub or the real
    # Qt host would answer whenever another test imported it first.
    monkeypatch.setattr(saturn_view, "window", stub, raising=False)

    assert tool.geospatial_globe({"location": "Goa"}) == "Opened Saturn's View over Goa."
    assert view.calls[0] == ("open", "Goa", None)


def test_a_view_that_cannot_be_built_says_nothing_was_drawn(monkeypatch):
    import saturn_view

    stub = types.ModuleType("saturn_view.window")
    stub.get_view = lambda parent=None, **kwargs: None
    monkeypatch.setitem(sys.modules, "saturn_view.window", stub)
    monkeypatch.setattr(saturn_view, "window", stub, raising=False)

    assert tool.geospatial_globe({"action": "read"}) == (
        "Saturn's View is not available on this machine right now, "
        "so nothing was drawn."
    )


# --------------------------------------------------------------------------- #
# The three vocabularies must not drift apart
# --------------------------------------------------------------------------- #


def _source(path):
    """Read a source file with its line endings settled.

    These two files are checked in with platform line endings, and an index
    lookup on "\nname\n" is a brittle way to notice that.
    """
    return (ROOT / path).read_text(encoding="utf-8").replace("\r\n", "\n")


def _schema_verbs():
    """The verbs main.py's tool declaration advertises, read off its own line.

    Parsed from source rather than imported, because importing main.py starts
    the assistant. This is the contract the model is shown, so it is the thing
    worth checking, and checking it costs no more than the router's own test.
    """
    block = _source("main.py").split('"name": "geospatial_globe"', 1)[-1]
    match = re.search(
        r'"action":\s*\{[^{}]*"description":\s*"([^"]+)"', block
    )
    assert match, "no action description found in the geospatial_globe schema"
    return {part.strip() for part in match.group(1).split("|")}


def _planner_verbs():
    """The verbs agent/planner.py tells the agent loop to emit."""
    block = _source("agent/planner.py").split("\ngeospatial_globe\n", 1)[-1]
    match = re.search(r"^\s*action:.*$", block, re.MULTILINE)
    assert match, "the planner's geospatial_globe block lost its action line"
    return set(re.findall(r'"([a-z_]+)"', match.group(0)))


def test_the_schema_and_the_planner_advertise_only_verbs_that_answer():
    answerable = set(tool.HANDLERS) | tool.LEGACY_VERBS

    schema = _schema_verbs()
    assert schema, "the schema must list the verbs it accepts"
    assert schema - answerable == set(), (
        f"main.py advertises verbs the router cannot answer: {schema - answerable}"
    )

    planner = _planner_verbs()
    assert planner - answerable == set(), (
        f"agent/planner.py advertises verbs the router cannot answer: "
        f"{planner - answerable}"
    )


def test_the_two_advertised_lists_agree_with_each_other():
    """The model reads one of these and the caller reads the other; a verb in
    only half the house is a verb that fails depending who asked."""
    assert _schema_verbs() == _planner_verbs()
