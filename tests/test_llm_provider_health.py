"""What Saturn tells the user when the cloud refuses.

``classify()`` is the single vocabulary for a provider's HTTP answer: main.py
reads its state to decide whether to defer to another brain, and ui.py renders
the same state as the cloud chip. A state that is merely *near* the truth is a
misdiagnosis with a direction -- "rate limited" sends the user off to wait,
while the account was out of money and waiting is the one thing that cannot
help. The 402 spelling of an empty wallet reached this build in exactly that
disguise, because Google borrows the throttle's status word for it.

These tests hold the classifier to its contract without touching the network.
"""

import re
from pathlib import Path

import pytest

from llm_client import UnifiedAIClient

ROOT = Path(__file__).resolve().parent.parent


class _Http(Exception):
    """A provider error carrying the code the SDK sets on it."""

    def __init__(self, text, code=None, status=None):
        super().__init__(text)
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status


# Verbatim shape of what the owner's key answered on 2026-11-06.
WALLET_402 = _Http(
    "402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': 'Your prepayment "
    "credits are depleted. Please go to AI Studio at https://ai.studio/projects to "
    "manage your project and billing.', 'status': 'RESOURCE_EXHAUSTED'}}",
    code=402, status="RESOURCE_EXHAUSTED",
)


def state_of(exc):
    return UnifiedAIClient.classify(exc)


# ── The money answer ────────────────────────────────────────────────────────

def test_an_empty_wallet_is_called_a_money_problem():
    state, reason = state_of(WALLET_402)

    assert state == "blocked"
    assert "billing" in reason.lower()


def test_an_empty_wallet_is_never_advised_to_wait():
    """'Rate limited' is the failure this test exists to keep out."""
    _state, reason = state_of(WALLET_402)

    for wait_instead in ("rate limit", "quota", "retry", "try again", "later"):
        assert wait_instead not in reason.lower(), reason


def test_the_older_403_spelling_of_the_same_account_still_reads_blocked():
    """The fix must not have narrowed the case that was already right."""
    state, reason = state_of(_Http(
        "403 PERMISSION_DENIED. Billing not set up on this project.", code=403))

    assert state == "blocked"
    assert "billing" in reason.lower()


def test_a_403_that_is_not_about_money_stays_a_refusal():
    state, _reason = state_of(_Http(
        "403 PERMISSION_DENIED: the API is not enabled for this project", code=403))

    assert state == "forbidden"


# ── The neighbours it must not be confused with ─────────────────────────────

@pytest.mark.parametrize("exc, expected", [
    (_Http("429 RESOURCE_EXHAUSTED: quota per day exceeded for gemini-flash", code=429),
     "rate_limited"),
    (_Http("401 UNAUTHENTICATED: API key not valid", code=401), "bad_key"),
    (_Http("404 NOT_FOUND: model is no longer available to new users", code=404),
     "retired"),
    (_Http("503 the load balancer dropped the connection"), "error"),
])
def test_each_provider_answer_keeps_its_own_fix(exc, expected):
    """Four refusals, four different things for the user to do."""
    assert state_of(exc)[0] == expected, state_of(exc)


def test_a_load_balancer_is_not_reported_as_an_unpaid_account():
    """The tempting one-word rule ('balance' means money) misfiles this."""
    state, _reason = state_of(_Http("500 INTERNAL: the load balancer timed out"))

    assert state != "blocked"


# ── The two modules must not drift into two vocabularies ────────────────────

def test_the_client_asks_the_ladder_module_instead_of_re_deciding():
    """main.py's docstring already records what two classifiers did to this app."""
    source = (ROOT / "llm_client.py").read_text(encoding="utf-8")

    assert re.search(r"from core\.gemini_models import[^\n]*billing_blocked", source)
    assert source.count("prepayment") <= 1, "the billing tells were copied, not shared"


@pytest.mark.parametrize("exc", [WALLET_402, _Http("Payment Required", code=None)])
def test_whatever_the_ladder_calls_a_wallet_the_client_calls_blocked(exc):
    """One direction on purpose: the client may add its own 403 fallback."""
    from core.gemini_models import billing_blocked

    if billing_blocked(exc):
        assert state_of(exc)[0] == "blocked", state_of(exc)


def test_main_py_still_defers_to_another_brain_on_the_wallet_state():
    """A correct state that nobody acts on is a correct diagnosis of nothing.

    Read from source: importing main.py starts the assistant.
    """
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    block = source.split("_GEMINI_DEFER_STATES", 1)[-1]
    listed = re.search(r"=\s*\(([^)]*)\)", block).group(1)

    assert "blocked" in listed, "the wallet state would be retried forever"
    assert "rate_limited" in listed


# ── The second cloud has the same blind spot ───────────────────────────────


class _Resp:
    def __init__(self, status_code, body=""):
        self.status_code = status_code
        self.text = body

    def json(self):
        return {}


def _stubbed_orchestrator(monkeypatch, status_code, body=""):
    """An OpenRouter client whose transport is a counter of model names."""
    import or_client

    seen = []

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.append(json["model"])
        return _Resp(status_code, body)

    monkeypatch.setattr(or_client.requests, "post", fake_post)
    monkeypatch.setattr(or_client, "RETRY_DELAY", 0)

    client = or_client.OpenRouterClient()
    client.api_key = "sk-or-not-a-real-key"  # tripwire: allow (test stub, deliberately reviewed; not a live credential)
    client._headers = {"Authorization": "Bearer stub"}
    monkeypatch.setattr(client, "_ensure_api_key", lambda: client.api_key)
    return client, seen


def test_an_openrouter_wallet_is_asked_once_and_not_down_the_roster(monkeypatch):
    """One account, one answer: iterating models cannot find missing credit.

    Measured live on 2026-11-06: a 402 here retried the first model twice and
    then walked all ten configured names, each of which answered 404 for
    unrelated reasons, before reporting the whole thing as a key or network
    problem.
    """
    import or_client

    client, seen = _stubbed_orchestrator(monkeypatch, 402, "Insufficient Credits.")

    with pytest.raises(or_client.InsufficientCredits):
        client.chat("hi")

    assert len(seen) == 1, seen


def test_an_openrouter_wallet_is_not_blamed_on_the_key(monkeypatch):
    """The message used to send the user to their key and their router."""
    client, _seen = _stubbed_orchestrator(monkeypatch, 402, "Insufficient Credits.")

    with pytest.raises(Exception) as caught:
        client.chat("hi")

    text = str(caught.value).lower()
    assert "api key" not in text or "key is valid" in text, caught.value
    assert "network" not in text


def test_an_openrouter_wallet_reaches_the_same_state_as_gemini(monkeypatch):
    """Two providers, one vocabulary -- the classifier must agree with both."""
    client, _seen = _stubbed_orchestrator(monkeypatch, 402, "Insufficient Credits.")

    with pytest.raises(Exception) as caught:
        client.chat("hi")

    assert state_of(caught.value)[0] == "blocked"


def test_a_retired_openrouter_name_is_still_skipped_instead_of_raising(monkeypatch):
    """The 402 fix must not have swallowed the ordinary dead-catalogue case."""
    client, seen = _stubbed_orchestrator(monkeypatch, 404)

    with pytest.raises(RuntimeError) as caught:
        client.chat("hi")

    assert len(seen) > 1, "a 404 should keep walking the pool, not stop the app"
    assert "api key and network" not in str(caught.value).lower()
