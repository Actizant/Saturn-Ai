"""The passphrase prompt: the one input a model must never be able to supply.

This is now on the lock path, so a bug here is the difference between asking a
human and locking them out. There is no event loop in these tests: the dialog is
driven the way a person would drive it, button press by button press.
"""

from __future__ import annotations

import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="module")
def qt():
    from PyQt6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def ui(qt):
    import ui as module

    return module


@pytest.fixture
def window(qt, ui):
    """Stands in for MainWindow: borrow the real methods, no 17k-line window."""
    from PyQt6.QtWidgets import QWidget

    holder = QWidget()
    holder._passphrase_dialog = None
    for name in ("_passphrase_finished", "_apply_passphrase_request"):
        setattr(holder, name, types.MethodType(getattr(ui.MainWindow, name), holder))
    yield holder
    holder.deleteLater()


def _answered(ui, text: str, mode: str = "ask"):
    dialog = ui.PassphraseDialog(mode=mode)
    dialog._input.setText(text)
    if mode == "new":
        dialog._confirm.setText(text)
    dialog._submit()
    return dialog


# ── The dialog itself ───────────────────────────────────────────────────────

def test_the_passphrase_is_masked_while_it_is_typed(ui):
    from PyQt6.QtWidgets import QLineEdit

    dialog = ui.PassphraseDialog(mode="new")

    assert dialog._input.echoMode() == QLineEdit.EchoMode.Password
    assert dialog._confirm.echoMode() == QLineEdit.EchoMode.Password


def test_a_password_field_never_offers_browser_style_completion(ui):
    dialog = ui.PassphraseDialog(mode="ask")

    # Whatever is typed must not be echoed back into the widget's own text.
    assert dialog._input.placeholderText() == "Passphrase"
    assert dialog.windowTitle() == "Saturn - Security"


def test_a_correct_entry_is_handed_back(ui):
    dialog = _answered(ui, "correct horse battery")

    assert dialog.passphrase() == "correct horse battery"


def test_an_empty_entry_is_refused_not_accepted(ui):
    dialog = _answered(ui, "")

    assert dialog.passphrase() is None
    assert dialog._hint.text() == "Nothing entered."


def test_a_new_passphrase_must_be_long_enough(ui):
    dialog = _answered(ui, "short", mode="new")

    assert dialog.passphrase() is None
    assert "eight" in dialog._hint.text()


def test_a_new_passphrase_must_be_typed_the_same_twice(ui):
    dialog = ui.PassphraseDialog(mode="new")
    dialog._input.setText("correct horse battery")
    dialog._confirm.setText("correct horse batterX")
    dialog._submit()

    assert dialog.passphrase() is None
    assert "do not match" in dialog._hint.text()


def test_cancelling_is_a_refusal_with_no_text(ui):
    dialog = ui.PassphraseDialog(mode="ask")
    dialog._input.setText("correct horse battery")
    dialog.reject()

    assert dialog.passphrase() is None


def test_the_countdown_running_out_is_a_refusal_not_a_success(ui):
    dialog = ui.PassphraseDialog(mode="ask")
    dialog._input.setText("correct horse battery")
    dialog._timer.stop()
    dialog._remaining = 1.0

    dialog._tick()

    assert dialog.passphrase() is None
    from PyQt6.QtWidgets import QDialog

    assert dialog.result() != QDialog.DialogCode.Accepted


def test_a_stale_timer_cannot_accept_a_dialog_after_the_fact(ui):
    dialog = _answered(ui, "correct horse battery")

    dialog._tick()  # the timer fired once more after acceptance

    assert dialog.passphrase() == "correct horse battery"


# ── Tokens: every request gets its own answer ───────────────────────────────

def test_every_request_gets_its_own_token(ui):
    tokens = [ui._next_passphrase_token() for _ in range(5)]

    assert len(set(tokens)) == 5


# ── The hand-off to the waiting caller ──────────────────────────────────────

def test_the_waiting_caller_receives_the_typed_text(ui, window):
    received = []
    token = ui._next_passphrase_token()
    ui._passphrase_waiters[token] = received.append

    dialog = _answered(ui, "correct horse battery")
    ui.MainWindow._passphrase_finished(window, token, "ask", dialog)

    assert received == ["correct horse battery"]


def test_the_waiting_caller_is_released_exactly_once(ui, window):
    received = []
    token = ui._next_passphrase_token()
    ui._passphrase_waiters[token] = received.append

    dialog = _answered(ui, "correct horse battery")
    ui.MainWindow._passphrase_finished(window, token, "ask", dialog)
    ui.MainWindow._passphrase_finished(window, token, "ask", dialog)

    # A duplicate finish must not answer the same question twice.
    assert received == ["correct horse battery"]
    assert token not in ui._passphrase_waiters


def test_a_cancelled_dialog_tells_the_caller_no(ui, window):
    received = []
    token = ui._next_passphrase_token()
    ui._passphrase_waiters[token] = received.append

    dialog = ui.PassphraseDialog(mode="ask")
    dialog.reject()
    ui.MainWindow._passphrase_finished(window, token, "ask", dialog)

    assert received == [None]


def test_an_unknown_token_is_ignored_rather_than_raising(ui, window):
    dialog = _answered(ui, "correct horse battery")

    # A stray finish must not reach into the GUI thread and explode.
    ui.MainWindow._passphrase_finished(window, "pp-not-a-real-token", "ask", dialog)

    assert window._passphrase_dialog is None


def test_a_caller_that_raises_does_not_break_the_ui(ui, window):
    def explode(_text):
        raise RuntimeError("the waiting caller went away")

    token = ui._next_passphrase_token()
    ui._passphrase_waiters[token] = explode

    dialog = _answered(ui, "correct horse battery")
    ui.MainWindow._passphrase_finished(window, token, "ask", dialog)

    assert token not in ui._passphrase_waiters


def test_only_one_prompt_is_ever_on_screen(ui, window):
    ui.MainWindow._apply_passphrase_request(window, "ask", "first", "pp-101")
    first = window._passphrase_dialog
    assert first is not None

    ui.MainWindow._apply_passphrase_request(window, "ask", "second", "pp-102")
    second = window._passphrase_dialog

    assert second is not first
    assert not first.isVisible()


def test_a_raise_in_the_finish_handler_does_not_kill_the_app(ui, window, monkeypatch):
    """An exception inside a Qt signal handler aborts the process on Windows.

    This handler sits on the lock path, so it must swallow its own failures and
    release the waiter rather than taking Saturn down with it.
    """
    received = []
    token = ui._next_passphrase_token()
    ui._passphrase_waiters[token] = received.append

    def boom(*_args, **_kwargs):
        raise RuntimeError("the dialog went away mid-hand-off")

    ui.MainWindow._apply_passphrase_request(window, "ask", "reason", token)
    dialog = window._passphrase_dialog
    monkeypatch.setattr(window, "_passphrase_finished", boom)

    dialog.accept()  # fires finished -> the broken handler

    assert token not in ui._passphrase_waiters
    assert window._passphrase_dialog is None


def test_the_prompt_carries_the_reason_it_was_raised_for(ui, window):
    ui.MainWindow._apply_passphrase_request(
        window, "ask", "I do not recognise the person at this desk.", "pp-201"
    )

    dialog = window._passphrase_dialog
    shown = " ".join(
        label.text() for label in dialog.findChildren(type(dialog._hint)) if label.text()
    )

    assert "recognise" in shown


# ── The public seam the security perimeter holds ─────────────────────────────
#
# ``ask_passphrase`` is what core/security_perimeter calls when it needs a
# human. It used to not exist at all, so every perimeter challenge silently
# degraded to "hold" - safe, but a lockout the owner could never answer.

class _DirectSignal:
    """Runs the slot inline, which is what the queued signal does on the GUI thread."""

    def __init__(self, slot):
        self._slot = slot
        self.sent = []

    def emit(self, *args):
        self.sent.append(args)
        self._slot(*args)


@pytest.fixture
def asking(ui, qt):
    """A stand-in window with the real methods and an inline signal."""
    from PyQt6.QtWidgets import QWidget

    holder = QWidget()
    holder._passphrase_dialog = None
    for name in ("_passphrase_finished", "_apply_passphrase_request", "ask_passphrase"):
        setattr(holder, name, types.MethodType(getattr(ui.MainWindow, name), holder))
    holder._passphrase_req_sig = _DirectSignal(
        lambda mode, reason, token: ui.MainWindow._apply_passphrase_request(
            holder, mode, reason, token))
    yield holder
    holder.deleteLater()


def test_the_seam_hands_over_the_typed_answer(asking):
    received = []
    asking.ask_passphrase("Disarm the perimeter.", received.append)

    dialog = asking._passphrase_dialog
    dialog._input.setText("correct horse battery")
    dialog._submit()

    assert received == ["correct horse battery"]


def test_a_cancelled_prompt_tells_the_caller_no(asking):
    received = []
    asking.ask_passphrase("Disarm the perimeter.", received.append)

    asking._passphrase_dialog.reject()

    assert received == [None]


def test_a_new_secret_asks_for_it_twice(asking, ui):
    asking.ask_passphrase("Choose one.", lambda text: None, mode="new")

    dialog = asking._passphrase_dialog

    assert dialog._mode == "new"
    assert not dialog._confirm.isHidden()

    dialog.reject()  # release the waiter, so the next test starts clean


def test_a_second_question_releases_the_first_caller(asking):
    """Two prompts cannot both be answered, so the older one gets a no."""
    first, second = [], []
    asking.ask_passphrase("first", first.append)
    asking.ask_passphrase("second", second.append)

    assert first == [None]
    assert second == []

    asking._passphrase_dialog.reject()


def test_a_prompt_with_no_waiter_still_closes_cleanly(asking, ui):
    """Showing a question nobody is waiting for must not leak the dialog."""
    asking.ask_passphrase("Would you like to set one?", None)

    asking._passphrase_dialog.reject()

    assert asking._passphrase_dialog is None
    assert not ui._passphrase_waiters

