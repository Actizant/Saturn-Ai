"""Which Gemini model names this build is willing to call.

Google retires model names without warning and the 404 reads like our bug
("models/gemini-2.5-flash is no longer available to new users"). On 2026-10-30
every 2.5 name answered 404 for the owner's key while 3.5-3.8 answered, and
three separate parts of the app had a 2.5 name hardcoded - so the repair shop,
the unified client and the Tier-3 classifier were all dead at once, and the log
said "Expecting value: line 1 column 1" instead.
"""

from pathlib import Path

import pytest

from core import gemini_models


# ── The ladder ─────────────────────────────────────────────────────────────

def test_no_name_in_the_ladder_is_one_we_know_is_retired():
    for name in gemini_models.FLASH + gemini_models.LITE:
        assert not gemini_models.is_retired(name), name


def test_a_retired_name_is_replaced_instead_of_being_called():
    """A stale literal must not become a 404; it becomes its successor."""
    for retired, successor in gemini_models.RETIRED.items():
        assert gemini_models.resolve(retired) == successor
        assert successor not in gemini_models.RETIRED, "a replacement is not also dead"


def test_the_ladder_keeps_its_own_order_and_has_no_duplicates():
    chain = gemini_models.ladder()

    assert chain == gemini_models.FLASH
    assert len(set(chain)) == len(chain)


def test_a_living_lite_request_stays_lite():
    chain = gemini_models.ladder("lite")

    assert chain[0] in gemini_models.LITE
    assert "lite" in chain[0], chain


def test_a_retired_lite_name_becomes_the_lite_successor():
    chain = gemini_models.ladder("gemini-2.5-flash-lite")

    assert chain[0] == gemini_models.LITE[0]
    assert chain[0] not in gemini_models.RETIRED


def test_an_unknown_living_name_is_still_tried_first():
    """We do not own the list; a name we have never seen is the caller's call."""
    chain = gemini_models.ladder("gemini-9.9-flash")

    assert chain[0] == "gemini-9.9-flash"
    assert gemini_models.FLASH[0] in chain, "a missing name must have a fallback"


def test_the_floating_aliases_are_not_relied_on():
    for alias in gemini_models.UNSTABLE:
        assert gemini_models.resolve(alias) not in gemini_models.UNSTABLE


def test_the_live_audio_model_is_left_alone():
    """The Live API has its own names; changing them would break voice."""
    assert "native-audio" in _text("main.py")


# ── The callers use it ─────────────────────────────────────────────────────

def _text(relative: str) -> str:
    return (Path(__file__).resolve().parent.parent / relative).read_text(encoding="utf-8")


#: Directories that are not our source: a vendored interpreter, byte caches,
#: build output, and any other runtime environment sitting in the tree.
_SKIP_DIRS = {".venv312", "__pycache__", ".pytest_cache", "build", "dist", "node_modules"}

#: Files allowed to name a dead model, with the reason spelled out. A name
#: in this list is not a call to Google's API, so remapping it would be the bug.
_ALLOWLIST = {
    # The retirement table has to name the dead ones to be able to map them.
    "core/gemini_models.py",
    # This test, which hands the shim a dead name on purpose to watch it climb.
    "tests/test_gemini_models.py",
    # OpenRouter catalogue ids ("google/gemini-2.0-flash-exp:free") - a routing
    # key on another provider's roster, not a model Google will look up.
    "or_client.py",
}

#: The Live API keeps its own model namespace, so a 2.5 name on one of these
#: lines is a live-audio id, not a text-model call. Voice depends on them.
_LIVE_NAMESPACE = ("native-audio", "LIVE_MODEL")

#: ... and when the name has neither of those tells, the site marks its own
#: block: everything from this comment until the literal closes is exempt.
_LIVE_MARKER = "saturn:live-api-model"


def _dead_names():
    """Every name we will not write down: retired (404) and floating (503).

    Both kinds were found in live code on 2026-10-30 - the 2.5 generation for
    404s and ``-latest`` for 503s - so guarding only the first would leave the
    dev agent and the API brain betting on one alias.
    """
    return sorted(gemini_models.RETIRED) + sorted(gemini_models.UNSTABLE)


def _retired_hits(rel: str, path: Path):
    """Every (lineno, name) in one file that calls a model we should not name."""
    hits = []
    in_live_block = False
    depth = 0
    opened = False

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for lineno, line in enumerate(lines, 1):
        balance = sum(line.count(ch) for ch in "([{") - sum(line.count(ch) for ch in ")]}")

        if in_live_block:
            depth += balance
            opened = opened or depth > 0
            if opened and depth <= 0:
                in_live_block = False
                depth = 0
                opened = False
            continue

        if _LIVE_MARKER in line:
            in_live_block = True
            depth = 0
            opened = False
            continue

        if any(marker in line for marker in _LIVE_NAMESPACE):
            continue

        for retired in _dead_names():
            if retired in line:
                hits.append(f"{rel}:{lineno} calls {retired}")

    return hits


def _source_files():
    root = Path(__file__).resolve().parent.parent
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield rel, path


def test_the_repo_is_actually_being_scanned():
    """A guard that finds no files proves nothing; check it has teeth."""
    found = [rel for rel, _ in _source_files()]

    assert len(found) > 100, f"only {len(found)} files scanned"
    assert "main.py" in found
    assert "actions/auto_heal_engine.py" in found


def test_no_source_file_hardcodes_a_retired_model_name():
    """Whole-repo, not a hand-kept list of files someone remembered to check."""
    offenders = []

    for rel, path in _source_files():
        if rel in _ALLOWLIST:
            continue
        offenders.extend(_retired_hits(rel, path))

    assert not offenders, "a retired name is a 404 and a floating alias a 503:\n" + "\n".join(offenders)


def test_the_marker_exempts_its_block_and_nothing_else(tmp_path):
    """The exemption is scoped to the literal, not to the whole file.

    Without this, one marker at the top of a module would blind the guard to
    every dead name below it - which is exactly the bug it exists to catch.
    """
    guilty = tmp_path / "guilty.py"
    guilty.write_text(
        "# saturn:live-api-model\n"
        "LIVE = [\n"
        "    'models/gemini-2.5-flash-native-audio-latest',\n"
        "    'models/gemini-2.0-flash-exp',\n"
        "]\n"
        "\n"
        "def answer(client):\n"
        "    return client.models.generate_content(model='gemini-1.5-pro')\n",
        encoding="utf-8",
    )

    hits = _retired_hits("guilty.py", guilty)

    assert hits == ["guilty.py:8 calls gemini-1.5-pro"], hits


def test_the_live_api_block_marker_cannot_go_stale_quietly():
    """A marker that marks nothing would turn an exemption into a blind spot."""
    marked = [
        rel for rel, path in _source_files()
        if rel not in _ALLOWLIST and _LIVE_MARKER in path.read_text(encoding="utf-8", errors="replace")
    ]

    assert marked, "every live-api-model block is gone; drop the marker handling"
    for rel in marked:
        path = Path(__file__).resolve().parent.parent / rel
        assert _retired_hits(rel, path) == []


def test_the_allowlist_is_not_holding_a_file_that_no_longer_needs_it():
    """An allowlist that only grows is how a guard stops meaning anything."""
    for rel in _ALLOWLIST:
        assert (Path(__file__).resolve().parent.parent / rel).exists(), f"{rel} is gone"


# ── The shim retries instead of dying on one name ──────────────────────────

class _Resp:
    text = "ok"


class _Models:
    def __init__(self, failures, error=None):
        self.calls = []
        self.failures = failures
        self.error = error or (lambda m: RuntimeError(
            f"404 NOT_FOUND. This model models/{m} is no longer available to new users."
        ))

    def generate_content(self, model, contents, config=None):
        self.calls.append(model)
        if model in self.failures:
            raise self.error(model)
        return _Resp()


class _Client:
    def __init__(self, failures=()):
        self.models = _Models(set(failures))


def test_the_shim_walks_the_ladder_when_a_name_is_gone():
    from core.gemini_shim import _Model

    client = _Client(failures={gemini_models.FLASH[0]})
    reply = _Model(client, "gemini-2.5-flash").generate_content("hello")

    assert reply.text == "ok"
    assert client.models.calls[0] == gemini_models.FLASH[0]
    assert len(client.models.calls) >= 2


def test_the_shim_does_not_retry_an_auth_error():
    """A bad key fails the same way on every model; do not burn three calls."""
    from core.gemini_shim import _Model

    class _Broken(_Models):
        def generate_content(self, model, contents, config=None):
            self.calls.append(model)
            raise RuntimeError("401 UNAUTHENTICATED: API key not valid")

    client = _Client()
    client.models = _Broken(set())

    with pytest.raises(RuntimeError, match="UNAUTHENTICATED"):
        _Model(client, None).generate_content("hello")

    assert len(client.models.calls) == 1


def test_a_throttled_rung_moves_to_the_sibling_with_budget():
    """Free-tier quota is counted per model, so a 429 is not the end.

    Measured live on 2026-10-30: the top rung answered six calls and then said
    'quota ... gemini-3.8-flash, 20 per day'. The whole app used to stop there.
    """
    from core.gemini_shim import _Model

    client = _Client()
    client.models.failures = {gemini_models.FLASH[0]}
    client.models.error = lambda m: RuntimeError(f"429 RESOURCE_EXHAUSTED: quota for {m}")

    reply = _Model(client, None).generate_content("hello")

    assert reply.text == "ok"
    assert client.models.calls == [gemini_models.FLASH[0], gemini_models.FLASH[1]]


# ── An empty wallet is not a throttle ─────────────────────────────────────

#: Verbatim shape of what this key actually answered on 2026-11-06.
WALLET = ("402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': 'Your "
          "prepayment credits are depleted. Please go to AI Studio at https://"
          "ai.studio/projects to manage your project and billing.', 'status': "
          "'RESOURCE_EXHAUSTED'}}")


class _NoCredit(Exception):
    code = 402
    status = "RESOURCE_EXHAUSTED"


def test_an_empty_wallet_is_asked_once_and_not_down_the_ladder():
    """Billing is counted per account, so a sibling model is not a workaround.

    Google spells an empty wallet with the status word of a throttle
    (RESOURCE_EXHAUSTED), and the climb rule read that word as "try the next
    name" -- so on 2026-11-06 one request burned three 402s, and the day's
    quota logic looked fine while the account could not answer at all.
    """
    from core.gemini_models import generate

    class _Dead(_Models):
        def generate_content(self, model, contents, config=None):
            self.calls.append(model)
            raise _NoCredit(WALLET)

    client = _Client()
    client.models = _Dead(set())

    with pytest.raises(_NoCredit):
        generate(client, "hi")

    assert len(client.models.calls) == 1, client.models.calls


def test_the_wallet_is_recognised_from_its_code_or_its_words():
    """Either tell is enough; the SDK has been seen to send only one of them."""
    assert gemini_models.billing_blocked(_NoCredit(WALLET))
    assert gemini_models.billing_blocked(RuntimeError("Payment Required: add funds"))
    assert not gemini_models.billing_blocked(RuntimeError("401 UNAUTHENTICATED: key not valid"))


def test_a_load_balancer_in_a_network_error_is_not_reported_as_money():
    """The word 'balance' is not a billing tell, and matching it was tempting.

    A connection error routed through a load balancer would otherwise be shown
    to the user as an account problem, sending them to the billing page when
    the internet is what is broken.
    """
    exc = RuntimeError("500 INTERNAL: the load balancer dropped the connection")

    assert not gemini_models.billing_blocked(exc)


def test_a_plain_throttle_still_climbs_after_the_wallet_fix():
    """The correction must not have swallowed the behaviour it shares a word with.

    RESOURCE_EXHAUSTED with no money tell in it is the per-model quota, and the
    app must still move to the rung that has budget.
    """
    throttled = RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for model")

    assert not gemini_models.billing_blocked(throttled)
    assert gemini_models.should_climb(throttled)
    assert not gemini_models.should_climb(_NoCredit(WALLET))


def test_generate_is_the_same_climb_for_callers_that_loop_nothing():
    from core.gemini_models import generate

    client = _Client(failures={gemini_models.FLASH[0], gemini_models.FLASH[1]})

    assert generate(client, "hi").text == "ok"
    assert client.models.calls == list(gemini_models.FLASH)


def test_generate_raises_what_climbing_cannot_fix():
    from core.gemini_models import generate

    class _Broken(_Models):
        def generate_content(self, model, contents, config=None):
            self.calls.append(model)
            raise RuntimeError("400 BAD_REQUEST: unsupported argument")

    client = _Client()
    client.models = _Broken(set(gemini_models.FLASH))

    with pytest.raises(RuntimeError, match="BAD_REQUEST"):
        generate(client, "hi")

    assert len(client.models.calls) == 1, "a request Google rejected is not retried three times"
