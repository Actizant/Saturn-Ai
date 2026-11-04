"""
tests/test_api_brain.py - Phase 1 rewire gate: the cloud API brain.

The in-process GGUF brain was replaced by core/api_brain.ApiBrain (generic
OpenAI-compatible /chat/completions). These lock in that it satisfies the exact
surface llm_client.py / main.py already call, using a stub HTTP layer so no real
key or credits are involved.
"""

import pytest

from core.api_brain import (
    ApiBrain,
    _resolve_endpoint,
    format_tools_for_local,
)


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="", lines=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text
        self._lines = lines or []

    def json(self):
        return self._payload

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)


@pytest.fixture
def brain(monkeypatch):
    b = ApiBrain()
    monkeypatch.setattr(
        "core.api_brain._resolve_endpoint",
        lambda: ("https://api.test/v1", "sk-test", "test/model"),
    )
    return b


# ------------------------------------------------------------- availability ---

def test_is_available_true_when_configured(brain):
    assert brain.is_available() is True


def test_is_available_false_without_key(monkeypatch):
    monkeypatch.setattr(
        "core.api_brain._resolve_endpoint",
        lambda: ("https://api.test/v1", "", "test/model"),
    )
    assert ApiBrain().is_available() is False


def test_list_installed_models_reports_configured_model(brain):
    assert brain.list_installed_models() == ["test/model"]


def test_pull_model_async_is_a_noop(brain):
    seen = []
    brain.pull_model_async("x", progress_callback=lambda s: seen.append(s))
    assert seen and "no download" in seen[0]["status"]


# -------------------------------------------------------------- resolve -------

def test_resolve_endpoint_prefers_generic_key(monkeypatch):
    monkeypatch.setattr("core.api_brain._api_keys", lambda: {
        "api_key": "KEY-GENERIC", "openrouter_api_key": "KEY-OR"})
    monkeypatch.setattr("memory.config_manager.get_setting", lambda *a, **k: "")
    base, key, model = _resolve_endpoint()
    assert key == "KEY-GENERIC"
    assert base.endswith("/v1")  # falls back to the OpenRouter default
    assert model  # a default model is always resolved


# ------------------------------------------------------------ chat_complete --

def test_chat_complete_returns_openai_shape(brain, monkeypatch):
    payload = {"model": "test/model",
               "choices": [{"message": {"role": "assistant", "content": "hi there"},
                            "finish_reason": "stop"}]}

    def fake_post(url, headers=None, json=None, timeout=None, stream=None):
        assert url == "https://api.test/v1/chat/completions"
        assert headers["Authorization"] == "Bearer sk-test"
        assert json["messages"][0]["content"] == "hello"
        return _FakeResp(200, payload)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    res = brain.chat_complete([{"role": "user", "content": "hello"}])
    assert res["choices"][0]["message"]["content"] == "hi there"


def test_chat_complete_relays_tools_and_parses_tool_calls(brain, monkeypatch):
    captured = {}
    payload = {"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "function": {"name": "open_app", "arguments": '{"a":1}'}}]},
        "finish_reason": "tool_calls"}]}

    def fake_post(url, headers=None, json=None, timeout=None, stream=None):
        captured.update(json)
        return _FakeResp(200, payload)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    tools = [{"name": "open_app", "description": "open", "parameters": {"type": "object"}}]
    res = brain.chat_complete([{"role": "user", "content": "launch"}], tools=tools)
    assert captured["tools"][0]["function"]["name"] == "open_app"
    assert captured["tool_choice"] == "auto"
    assert res["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "open_app"


def test_chat_complete_raises_on_http_error(brain, monkeypatch):
    import requests
    monkeypatch.setattr(requests, "post",
                        lambda *a, **k: _FakeResp(402, text="Insufficient credits"))
    with pytest.raises(RuntimeError):
        brain.chat_complete([{"role": "user", "content": "x"}])


# -------------------------------------------------------------- streaming -----

def test_chat_stream_yields_text_deltas(brain, monkeypatch):
    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        "data: [DONE]",
    ]

    def fake_post(url, headers=None, json=None, timeout=None, stream=None):
        assert json["stream"] is True
        return _FakeResp(200, lines=lines)

    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    assert list(brain.chat_stream([{"role": "user", "content": "hi"}])) == ["Hel", "lo"]


def test_generate_chat_stream_emits_openai_chunks(brain, monkeypatch):
    lines = ['data: {"choices":[{"delta":{"content":"tok"}}]}', "data: [DONE]"]
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(200, lines=lines))
    chunks = list(brain.generate_chat_stream([{"role": "user", "content": "hi"}]))
    assert chunks[0]["choices"][0]["delta"]["content"] == "tok"


def test_chat_stream_raises_when_unconfigured(monkeypatch):
    monkeypatch.setattr("core.api_brain._resolve_endpoint",
                        lambda: ("https://api.test/v1", "", "test/model"))
    with pytest.raises(RuntimeError):
        list(ApiBrain().chat_stream([{"role": "user", "content": "hi"}]))


# -------------------------------------------------------- tool normalisation --

def test_format_tools_lowercases_gemini_schema_types():
    gemini_tools = [{"name": "x", "description": "d",
                     "parameters": {"type": "OBJECT",
                                    "properties": {"n": {"type": "STRING"}}}}]
    out = format_tools_for_local(gemini_tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["parameters"]["type"] == "object"
    assert out[0]["function"]["parameters"]["properties"]["n"]["type"] == "string"
