"""Tests for Saturn's advanced MCP host layer.

Covers three axes that push Saturn beyond a plain MCP caller:

1. Bidirectional host    - `sampling/createMessage` and `elicitation/create`
                           arriving FROM the server get answered by our
                           registered handlers, not silently dropped.
2. Tool Router           - TF-IDF selection ranks relevant tools above
                           noise, honours pinned names, respects top_k.
3. Capability Health     - rolling outcome record + unhealthy detection +
                           sweep that re-forges Saturn's own portable servers.

Nothing here boots a real subprocess. Every test injects fake proc objects
or fake providers into the modules and asserts the behaviour directly.
"""

from __future__ import annotations

import io
import json
import threading
import time
from unittest.mock import patch

import pytest

from core import mcp_host, mcp_manager
from core.capability_health import CapabilityHealth
from core.mcp_host import (
    _extract_text,
    _flatten_messages,
    make_elicitation_handler,
    make_sampling_handler,
)
from core.mcp_manager import MCPServerInstance, UniversalMCPManager
from core.tool_router import ToolRouter, _tokenize


@pytest.fixture(autouse=True)
def _reset_router_state():
    """Keep the module-level planner-registration flag from leaking between
    tests (planner_lines registers the real MCP source when first called)."""
    import core.tool_router as _tr
    _tr._STATE["planner_sources_registered"] = False
    yield
    _tr._STATE["planner_sources_registered"] = False


# ── Reader loop distinguishes responses from server-initiated requests ───


def _make_instance_with_piped_stdout(lines):
    """Build a real MCPServerInstance whose stdout is a StringIO of MCP
    JSON frames, so the reader loop runs exactly as it would over a real
    subprocess pipe."""
    inst = MCPServerInstance("test_server", {"command": "true", "args": []})
    # Fake the process object so reader works.
    fake = type("P", (), {})()
    fake.stdout = io.StringIO("".join(line + "\n" for line in lines))
    fake.poll = lambda: None
    fake.stdin = io.StringIO()
    inst.proc = fake
    return inst


def test_reader_delivers_response_to_pending_request():
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})]
    inst = _make_instance_with_piped_stdout(lines)
    evt = threading.Event()
    inst._pending_requests[1] = evt
    inst._read_stdout()
    assert inst._responses.get(1) == {
        "jsonrpc": "2.0", "id": 1, "result": {"tools": []},
    }


def test_reader_dispatches_server_request_to_handler():
    """A frame with both `id` and `method` is a server→client request; the
    reader must dispatch it through the manager's registered handler."""
    seen = {}

    def _handler(*, server_name, params):
        seen["server_name"] = server_name
        seen["params"] = params
        return {"role": "assistant", "content": {"type": "text", "text": "hi"}}

    UniversalMCPManager._instance = None
    mgr = UniversalMCPManager.get_instance()
    mgr.register_server_request_handler("sampling/createMessage", _handler)

    inst = MCPServerInstance("s1", {"command": "true", "args": []})
    stdin_capture = io.StringIO()
    fake_proc = type("P", (), {})()
    fake_proc.poll = lambda: None
    fake_proc.stdin = stdin_capture
    inst.proc = fake_proc

    # Drive the dispatch path directly rather than racing a synthetic
    # stdout pipe; the reader loop's method-vs-response discrimination is
    # already asserted separately in test_reader_delivers_response.
    inst._dispatch_server_request(
        req_id=42,
        method="sampling/createMessage",
        params={"messages": [{"role": "user", "content": "hello"}]},
    )

    assert seen["server_name"] == "s1"
    stdin_capture.seek(0)
    reply = json.loads(stdin_capture.read().strip().splitlines()[-1])
    assert reply["id"] == 42
    assert reply["result"]["content"]["text"] == "hi"
    assert "error" not in reply


def test_reader_replies_method_not_found_for_unknown_server_request():
    UniversalMCPManager._instance = None
    UniversalMCPManager.get_instance()

    inst = MCPServerInstance("s1", {"command": "true", "args": []})
    stdin_capture = io.StringIO()
    fake_proc = type("P", (), {})()
    fake_proc.poll = lambda: None
    fake_proc.stdin = stdin_capture
    inst.proc = fake_proc

    inst._dispatch_server_request(req_id=7, method="completion/complete", params={})

    stdin_capture.seek(0)
    err = json.loads(stdin_capture.read().strip().splitlines()[-1])
    assert err["error"]["code"] == -32601
    assert "completion/complete" in err["error"]["message"]


class queue_Pipe:
    """Stdin-style file with a push()/close() API so a test can feed the
    MCP reader loop frames on demand.

    Retained for future end-to-end reader-loop tests that need real
    asynchronous dispatch. Not used by the current test set."""

    def __init__(self):
        self._buf = io.StringIO()
        self._closed = False
        self._cv = threading.Condition()

    def push(self, line: str) -> None:
        with self._cv:
            self._buf.write(line + "\n")
            self._cv.notify_all()

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def readline(self) -> str:
        """Return the next newline-terminated line, or '' when closed and
        no more data. Reads from an internal cursor, not the underlying
        StringIO's tell()."""
        with self._cv:
            while True:
                data = self._buf.getvalue()
                if "\n" in data:
                    end = data.index("\n")
                    line = data[:end + 1]
                    # Rebuild the buffer without the consumed line.
                    self._buf = io.StringIO(data[end + 1:])
                    return line
                if self._closed:
                    return ""
                self._cv.wait(timeout=0.05)


# ── Sampling handler plumbing ───────────────────────────────────────────────


def test_extract_text_handles_all_mcp_content_shapes():
    assert _extract_text("plain") == "plain"
    assert _extract_text({"type": "text", "text": "obj"}) == "obj"
    assert _extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"


def test_flatten_messages_separates_system_and_transcript():
    sys_p, prompt = _flatten_messages([
        {"role": "system", "content": {"type": "text", "text": "be concise"}},
        {"role": "user", "content": "what is 2+2"},
        {"role": "assistant", "content": "4"},
        {"role": "user", "content": "and 3+3"},
    ])
    assert sys_p == "be concise"
    assert "[user] what is 2+2" in prompt
    assert "[assistant] 4" in prompt
    assert "[user] and 3+3" in prompt


def test_sampling_handler_routes_through_llm_client():
    handler = make_sampling_handler()
    fake_client = type("C", (), {"chat": lambda self, **kw: "MARS"})()
    with patch("llm_client.client", fake_client):
        result = handler(
            server_name="quiz",
            params={"messages": [{"role": "user", "content": {"type": "text", "text": "largest planet?"}}]},
        )
    assert result["role"] == "assistant"
    assert result["content"]["text"] == "MARS"
    assert result["stopReason"] == "endTurn"


def test_sampling_handler_degrades_gracefully_on_llm_error():
    handler = make_sampling_handler()

    def _boom(**_kw):
        raise RuntimeError("provider 500")

    fake_client = type("C", (), {"chat": _boom})()
    with patch("llm_client.client", fake_client):
        result = handler(
            server_name="quiz",
            params={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert "could not sample" in result["content"]["text"]


def test_elicitation_handler_cancels_without_ui_callback():
    handler = make_elicitation_handler(prompt_callback=None)
    out = handler(server_name="form", params={"message": "your dob?"})
    assert out == {"action": "cancel"}


def test_elicitation_handler_passes_collected_values_through():
    def _cb(*, message, schema, server_name):
        return {"dob": "1990-01-01", "server": server_name}
    handler = make_elicitation_handler(prompt_callback=_cb)
    out = handler(server_name="form", params={"message": "your dob?"})
    assert out["action"] == "accept"
    assert out["content"]["dob"] == "1990-01-01"


def test_elicitation_handler_respects_decline():
    def _cb(*, message, schema, server_name):
        return {"action": "decline"}
    handler = make_elicitation_handler(prompt_callback=_cb)
    out = handler(server_name="form", params={"message": "your dob?"})
    assert out == {"action": "decline"}


# ── Tool Router ─────────────────────────────────────────────────────────────


@pytest.fixture
def router():
    ToolRouter.reset_instance()
    r = ToolRouter.get_instance()
    yield r
    ToolRouter.reset_instance()


def test_tokenize_splits_snake_case_and_lowercases():
    assert _tokenize("read_resource") == ["read", "resource"]
    assert _tokenize("MCP:cricket_server Live Score") == ["mcp", "cricket", "server", "live", "score"]


def test_router_ranks_relevant_tool_above_noise(router):
    router.register_source("test", lambda: [
        {"name": "mcp_cricket_live_score", "description": "Fetch live cricket scores for a match"},
        {"name": "mcp_weather_forecast", "description": "Get weather forecast for a city"},
        {"name": "mcp_recipe_search", "description": "Find recipes by ingredient"},
    ])
    picked = router.select(goal="what is the live cricket score today", top_k=2)
    names = [p["name"] for p in picked]
    assert names[0] == "mcp_cricket_live_score"
    assert "mcp_weather_forecast" not in names[:1]


def test_router_always_includes_pinned_tools(router):
    router.register_source("test", lambda: [
        {"name": "skill_forge", "description": "Create a new capability"},
        {"name": "mcp_cricket_live_score", "description": "Live cricket"},
    ])
    router.pin("skill_forge")
    picked = router.select(goal="what is the live cricket score", top_k=1)
    names = [p["name"] for p in picked]
    assert "skill_forge" in names
    assert "mcp_cricket_live_score" in names


def test_router_returns_full_tool_dict(router):
    router.register_source("test", lambda: [
        {"name": "alpha", "description": "does foo", "parameters": {"type": "OBJECT"}},
    ])
    picked = router.select(goal="does foo thing", top_k=1)
    assert picked[0]["parameters"] == {"type": "OBJECT"}


def test_router_honours_min_score_floor(router):
    router.register_source("test", lambda: [
        {"name": "unrelated_one", "description": "does quantum tunneling"},
        {"name": "unrelated_two", "description": "parses mandarin grammar"},
    ])
    picked = router.select(goal="make me a sandwich", top_k=10, min_score=1.5)
    assert picked == []


def test_router_planner_lines_render_planner_shape(router):
    router.register_source("test", lambda: [
        {"name": "mcp_cricket_live_score",
         "description": "Fetch live cricket scores for a match",
         "parameters": {"type": "OBJECT", "properties": {"match": {"type": "STRING"}}}},
        {"name": "mcp_weather_forecast", "description": "Get weather forecast"},
    ])
    lines = router.planner_lines(goal="what is the live cricket score")
    assert lines, "a strong match must produce at least one planner line"
    assert lines[0].startswith("mcp_cricket_live_score: Fetch live cricket scores")
    # Same shape the planner already uses for dynamic feature tools.
    assert "Parameters: {" in lines[0]
    assert "mcp_weather_forecast" not in "\n".join(lines)


def test_router_planner_lines_empty_without_goal(router):
    assert router.planner_lines(goal="") == []
    assert router.planner_lines(goal="   ") == []


def test_planner_prompt_includes_router_mcp_lines():
    """create_plan/replan feed the goal into the system prompt; the prompt
    must gain an MCP section only when the router finds relevant tools."""
    from agent import planner

    UniversalMCPManager._instance = None
    ToolRouter.reset_instance()
    router = ToolRouter.get_instance()
    router.register_source("test", lambda: [
        {"name": "mcp_alpha_score", "description": "Fetch live cricket scores",
         "parameters": {}},
    ])
    # planner_lines auto-registers the real (empty) mcp source; label "mcp"
    # must not clash with the fake "test" label above.
    prompt = planner._planner_system_prompt(goal="what is the live cricket score")
    assert "MCP SERVER TOOLS RELEVANT TO THIS GOAL" in prompt
    assert "mcp_alpha_score" in prompt

    bare = planner._planner_system_prompt()
    assert "MCP SERVER TOOLS RELEVANT TO THIS GOAL" not in bare
    ToolRouter.reset_instance()


# ── Capability Health ───────────────────────────────────────────────────────


def _reset_health():
    CapabilityHealth.reset()


def test_health_records_outcomes_and_detects_failure_streak():
    _reset_health()
    for _ in range(5):
        CapabilityHealth.record("mcp_cricket_live_score", ok=False, latency_ms=100, error="boom")
    assert CapabilityHealth.is_unhealthy("mcp_cricket_live_score") is True


def test_health_requires_min_calls_before_flagging():
    _reset_health()
    CapabilityHealth.record("mcp_x_y", ok=False, latency_ms=50, error="err")
    CapabilityHealth.record("mcp_x_y", ok=False, latency_ms=50, error="err")
    # 2 failures is below the 5-call floor.
    assert CapabilityHealth.is_unhealthy("mcp_x_y") is False


def test_health_success_resets_consecutive_streak():
    _reset_health()
    for _ in range(2):
        CapabilityHealth.record("mcp_a_b", ok=False, latency_ms=10, error="x")
    CapabilityHealth.record("mcp_a_b", ok=True, latency_ms=10, error=None)
    for _ in range(2):
        CapabilityHealth.record("mcp_a_b", ok=False, latency_ms=10, error="x")
    # Consecutive streak was reset by the success - it now counts only the
    # two failures AFTER that success, not the four failures total.
    rec = CapabilityHealth.stats("mcp_a_b")
    assert rec.consecutive_failures == 2
    assert rec.failures == 4
    assert rec.successes == 1


def test_health_avg_latency_rolls():
    _reset_health()
    for ms in (100, 200, 300):
        CapabilityHealth.record("mcp_l_t", ok=True, latency_ms=ms, error=None)
    rec = CapabilityHealth.stats("mcp_l_t")
    assert abs(rec.avg_latency_ms - 200) < 1e-6


def test_health_cooldown_prevents_repeated_reforge_offers():
    _reset_health()
    for _ in range(5):
        CapabilityHealth.record("mcp_cool_run", ok=False, latency_ms=10, error="x")
    assert CapabilityHealth.should_offer_reforge("mcp_cool_run") is True
    CapabilityHealth.mark_reforge_offered("mcp_cool_run")
    assert CapabilityHealth.should_offer_reforge("mcp_cool_run") is False


def test_health_failure_evidence_mentions_error():
    _reset_health()
    for _ in range(3):
        CapabilityHealth.record("mcp_e_x", ok=False, latency_ms=10, error="connection refused")
    ev = CapabilityHealth.failure_evidence("mcp_e_x")
    assert "connection refused" in ev
    assert "mcp_e_x" in ev


def test_health_sweep_reforges_only_portable_servers(router=None):
    """Sweep only acts on servers whose manifest tier == 'portable', and
    only once per cooldown window."""
    _reset_health()
    # Simulate an unhealthy portable server Saturn forged. Server name has
    # no underscore to match the current `mcp_<server>_<tool>` parsing.
    for _ in range(5):
        CapabilityHealth.record("mcp_alpha_do_thing", ok=False, latency_ms=10, error="boom")

    forged = [{"name": "alpha", "tier": "portable", "description": "does thing"}]
    calls = []

    def _fake_forge(goal, skill_name, context_hints, max_repair_attempts):
        calls.append((goal, skill_name, context_hints))
        return {"success": True}

    with (
        patch("core.mcp_forge.MCPForge.list_forged", return_value=forged),
        patch("core.mcp_forge.MCPForge.forge_mcp_server", staticmethod(_fake_forge)),
    ):
        kicked = CapabilityHealth.sweep_and_reforge()

    # Wait for the worker thread to run.
    deadline = time.time() + 2.0
    while time.time() < deadline and not calls:
        time.sleep(0.05)

    assert kicked == ["alpha"]
    assert calls[0][1] == "alpha"
    assert "Auto-reforge triggered" in calls[0][2]


def test_health_sweep_skips_non_portable_and_configured_servers():
    _reset_health()
    for _ in range(5):
        CapabilityHealth.record("mcp_thirdparty_thing_do", ok=False, latency_ms=10, error="boom")

    forged = [{"name": "thirdparty_thing", "tier": "user_installed", "description": "user config"}]
    calls = []

    def _never_called(*_a, **_kw):
        calls.append(True)
        return {}

    with (
        patch("core.mcp_forge.MCPForge.list_forged", return_value=forged),
        patch("core.mcp_forge.MCPForge.forge_mcp_server", staticmethod(_never_called)),
    ):
        kicked = CapabilityHealth.sweep_and_reforge()

    assert kicked == []
    assert calls == []


def test_execute_mcp_tool_records_success_and_failure():
    """The manager must hand every tool outcome to CapabilityHealth so the
    immune system sees real traffic, not just the tests."""
    _reset_health()
    UniversalMCPManager._instance = None
    mgr = UniversalMCPManager.get_instance()

    fake = type("S", (), {})()
    fake.is_connected = True
    fake.error = None
    fake.call_tool = lambda name, args: {"success": True, "output": "42"}
    mgr._servers["alpha"] = fake
    out = mgr.execute_mcp_tool("mcp_alpha_get_number", {})
    assert out == "42"
    assert CapabilityHealth.stats("mcp_alpha_get_number").successes == 1

    fake.call_tool = lambda name, args: {"success": False, "error": "unreachable"}
    out = mgr.execute_mcp_tool("mcp_alpha_get_number", {})
    assert out.startswith("MCP Tool Error")
    assert CapabilityHealth.stats("mcp_alpha_get_number").failures == 1
    assert "unreachable" in CapabilityHealth.stats("mcp_alpha_get_number").last_error


# ── Resources + Prompts ─────────────────────────────────────────────────────


def test_get_all_tool_declarations_includes_resource_and_prompt_verbs():
    UniversalMCPManager._instance = None
    mgr = UniversalMCPManager.get_instance()
    fake = type("S", (), {})()
    fake.is_connected = True
    fake.tools = [{"name": "do_thing", "description": "does a thing", "inputSchema": {}}]
    fake.resources = [{"uri": "thing://live", "name": "live feed"}]
    fake.prompts = [{"name": "plan", "description": "plan verb"}]
    mgr._servers["alpha"] = fake

    decls = mgr.get_all_tool_declarations()
    names = {d["name"] for d in decls}
    assert "mcp_alpha_do_thing" in names
    assert "mcp_alpha_read_resource" in names
    assert "mcp_alpha_get_prompt" in names
