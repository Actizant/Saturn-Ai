"""The fail-safe credential. Every test here is about a secret staying secret.

The passphrase is the only way back in when the camera is blind, which makes it
the weakest link in the subsystem. So the tests are about what must NOT happen:
the plaintext must not be recoverable from disk, two users must not share a
salt, a corrupted encoding must fail closed rather than raise, and the bypass
window must expire on the boundary rather than a second late.
"""

from __future__ import annotations

import types

import pytest

from core import passphrase_vault


@pytest.fixture
def store(monkeypatch):
    """An in-memory settings file, so no test ever touches the real one."""
    import memory.config_manager as config_manager

    data: dict = {}
    monkeypatch.setattr(
        config_manager, "get_setting", lambda key, default=None: data.get(key, default)
    )
    monkeypatch.setattr(
        config_manager, "set_setting", lambda key, value: data.__setitem__(key, value)
    )
    return data


@pytest.fixture
def clock(monkeypatch):
    """A movable wall clock, because the bypass window is measured in real time."""
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(
        passphrase_vault, "time", types.SimpleNamespace(time=lambda: now["t"])
    )
    return now


# ── Hashing and verification ────────────────────────────────────────────────

def test_a_passphrase_verifies_against_its_own_hash():
    encoded = passphrase_vault.make_hash("correct horse battery")

    assert passphrase_vault.verify("correct horse battery", encoded) is True


def test_a_wrong_passphrase_does_not_verify():
    encoded = passphrase_vault.make_hash("correct horse battery")

    assert passphrase_vault.verify("correct horse batterY", encoded) is False
    assert passphrase_vault.verify("", encoded) is False
    assert passphrase_vault.verify("correct horse batter", encoded) is False


def test_two_hashes_of_the_same_passphrase_differ_and_both_verify():
    first = passphrase_vault.make_hash("correct horse battery")
    second = passphrase_vault.make_hash("correct horse battery")

    # Different salts, so identical passphrases are not identifiable on disk.
    assert first != second
    assert passphrase_vault.verify("correct horse battery", first) is True
    assert passphrase_vault.verify("correct horse battery", second) is True


def test_the_encoding_carries_its_own_work_factor():
    encoded = passphrase_vault.make_hash("correct horse battery", iterations=1_000)
    algorithm, iterations, salt, digest = encoded.split("$")

    assert algorithm == passphrase_vault.ALGORITHM
    assert iterations == "1000"
    assert len(bytes.fromhex(salt)) == passphrase_vault.SALT_BYTES
    # A hash made at a lower cost still verifies after the default is raised.
    assert passphrase_vault.verify("correct horse battery", encoded) is True


def test_an_empty_passphrase_is_refused_at_the_hashing_layer():
    with pytest.raises(ValueError):
        passphrase_vault.make_hash("")
    with pytest.raises(ValueError):
        passphrase_vault.make_hash(None)


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        None,
        "not-a-hash",
        "pbkdf2_sha512$200000$00ff$00ff",          # wrong algorithm
        "pbkdf2_sha256$200000$00ff",               # too few fields
        "pbkdf2_sha256$200000$00ff$00ff$extra",    # too many fields
        "pbkdf2_sha256$notanint$00ff$00ff",        # unparsable cost
        "pbkdf2_sha256$0$00ff$00ff",               # zero cost
        "pbkdf2_sha256$200000$zzzz$00ff",          # unparsable salt
        "pbkdf2_sha256$200000$$00ff",              # empty salt
        "pbkdf2_sha256$200000$00ff$",              # empty digest
    ],
)
def test_a_corrupt_encoding_fails_closed_instead_of_raising(encoded):
    assert passphrase_vault.verify("correct horse battery", encoded) is False


# ── Storage ─────────────────────────────────────────────────────────────────

def test_nothing_is_configured_before_a_passphrase_is_set(store):
    assert passphrase_vault.configured() is False
    assert passphrase_vault.check_passphrase("correct horse battery") is False


def test_setting_a_passphrase_stores_a_hash_and_never_the_plaintext(store):
    result = passphrase_vault.set_passphrase("correct horse battery")

    assert result["ok"] is True
    assert result == {"ok": True, "message": "Passphrase set."}
    stored = store[passphrase_vault.HASH_KEY]
    assert "correct horse battery" not in stored
    assert stored.startswith(passphrase_vault.ALGORITHM + "$")


def test_the_result_of_setting_a_passphrase_does_not_echo_the_secret(store):
    result = passphrase_vault.set_passphrase("correct horse battery")

    assert "correct horse battery" not in repr(result)


def test_a_short_passphrase_is_refused_and_nothing_is_written(store):
    result = passphrase_vault.set_passphrase("short")

    assert result["ok"] is False
    assert result["error"] == "TOO_SHORT"
    assert passphrase_vault.HASH_KEY not in store


def test_the_stored_passphrase_is_the_only_one_that_checks_out(store):
    passphrase_vault.set_passphrase("correct horse battery")

    assert passphrase_vault.check_passphrase("correct horse battery") is True
    assert passphrase_vault.check_passphrase("correct horse batter") is False
    assert passphrase_vault.configured() is True


def test_setting_a_new_passphrase_retires_the_old_one(store):
    passphrase_vault.set_passphrase("correct horse battery")
    passphrase_vault.set_passphrase("a different secret")

    assert passphrase_vault.check_passphrase("correct horse battery") is False
    assert passphrase_vault.check_passphrase("a different secret") is True


def test_clearing_the_passphrase_also_clears_the_bypass(store, clock):
    passphrase_vault.set_passphrase("correct horse battery")
    passphrase_vault.grant_bypass(hours=24)
    assert passphrase_vault.bypass_active() is True

    result = passphrase_vault.clear_passphrase()

    assert result["ok"] is True
    assert passphrase_vault.configured() is False
    assert passphrase_vault.bypass_active() is False


def test_a_settings_failure_is_reported_rather_than_raised(store, monkeypatch):
    import memory.config_manager as config_manager

    def _explode(*_args, **_kwargs):
        raise OSError("the disk is gone")

    monkeypatch.setattr(config_manager, "set_setting", _explode)

    result = passphrase_vault.set_passphrase("correct horse battery")

    assert result["ok"] is False
    assert result["error"] == "SAVE_FAILED"


# ── Bypass window ───────────────────────────────────────────────────────────

def test_no_bypass_is_active_before_one_is_granted(store, clock):
    assert passphrase_vault.bypass_active() is False
    assert passphrase_vault.bypass_remaining() == 0.0


def test_a_granted_bypass_is_active_for_its_whole_window(store, clock):
    passphrase_vault.set_passphrase("correct horse battery")
    passphrase_vault.grant_bypass(hours=1.0)

    assert passphrase_vault.bypass_active() is True
    assert passphrase_vault.bypass_remaining() == pytest.approx(3600.0)

    clock["t"] += 3599.0
    assert passphrase_vault.bypass_active() is True

    # The boundary: one second past the window it is over, not "still active".
    clock["t"] += 1.0
    assert passphrase_vault.bypass_active() is False
    assert passphrase_vault.bypass_remaining() == 0.0


def test_a_bypass_without_a_passphrase_is_inert(store, clock):
    # No passphrase can earn this window, so it must not silence the gate and
    # status must not claim protection that is not there.
    passphrase_vault.grant_bypass(hours=24)

    assert passphrase_vault.bypass_active() is False
    assert passphrase_vault.status()["bypass_active"] is False
    assert passphrase_vault.status()["bypass_minutes"] == 0


def test_a_bypass_window_cannot_be_stretched_past_a_week(store, clock):
    until = passphrase_vault.grant_bypass(hours=10_000)

    assert until == pytest.approx(clock["t"] + passphrase_vault.MAX_BYPASS_HOURS * 3600.0)


def test_a_negative_bypass_grants_nothing(store, clock):
    passphrase_vault.grant_bypass(hours=-5)

    assert passphrase_vault.bypass_active() is False


def test_a_bypass_can_be_cleared_early(store, clock):
    passphrase_vault.set_passphrase("correct horse battery")
    passphrase_vault.grant_bypass(hours=24)
    assert passphrase_vault.bypass_active() is True

    passphrase_vault.clear_bypass()

    assert passphrase_vault.bypass_active() is False


def test_a_corrupt_bypass_timestamp_reads_as_no_bypass(store, clock):
    store[passphrase_vault.BYPASS_KEY] = "tomorrow"

    assert passphrase_vault.bypass_until() == 0.0
    assert passphrase_vault.bypass_active() is False


# ── Status ──────────────────────────────────────────────────────────────────

def test_status_says_what_is_true_without_leaking_the_secret(store, clock):
    passphrase_vault.set_passphrase("correct horse battery")
    passphrase_vault.grant_bypass(hours=2)

    status = passphrase_vault.status()

    assert status["configured"] is True
    assert status["bypass_active"] is True
    assert status["bypass_minutes"] == 120
    assert status["min_length"] == passphrase_vault.MIN_LENGTH
    assert status["algorithm"] == passphrase_vault.ALGORITHM
    assert "correct horse battery" not in repr(status)


def test_status_before_anything_is_set(store, clock):
    status = passphrase_vault.status()

    assert status["configured"] is False
    assert status["bypass_active"] is False
    assert status["bypass_minutes"] == 0


# ── The on-screen setup flow (actions/security_tools.py) ───────────────────
# The whole point of the prompt is that a human types the secret and it gets
# stored. A version of set_perimeter_passphrase showed the dialog and logged
# "set" without ever calling set_passphrase, so arming kept reporting no
# passphrase. These tests drive that flow with a fake UI.

class _FakeUI:
    def __init__(self, answer):
        self._answer = answer
        self.logs = []

    def ask_passphrase(self, reason, on_result=None, mode="ask"):
        if on_result is not None:
            on_result(self._answer)

    def write_log(self, text):
        self.logs.append(text)


def _run_setup(store, monkeypatch, answer):
    from actions import security_tools

    ui = _FakeUI(answer)
    monkeypatch.setattr(security_tools, "_ui", ui)
    security_tools.set_perimeter_passphrase({}, player=None)
    return ui


def test_the_setup_prompt_actually_stores_the_passphrase(store, monkeypatch):
    ui = _run_setup(store, monkeypatch, "correct horse battery")

    assert passphrase_vault.configured() is True
    assert passphrase_vault.check_passphrase("correct horse battery") is True
    assert any("set" in line.lower() for line in ui.logs)


def test_cancelling_the_setup_prompt_stores_nothing(store, monkeypatch):
    ui = _run_setup(store, monkeypatch, None)

    assert passphrase_vault.configured() is False
    assert any("not set" in line.lower() for line in ui.logs)


def test_a_too_short_passphrase_is_reported_and_not_stored(store, monkeypatch):
    # The dialog guards length, but the callback must still fail honestly rather
    # than claim success if a short value ever reaches it.
    ui = _run_setup(store, monkeypatch, "short")

    assert passphrase_vault.configured() is False
    assert any("not saved" in line.lower() for line in ui.logs)
