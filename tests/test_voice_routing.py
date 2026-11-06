"""
tests/test_voice_routing.py
===========================
Which voice engine gets a sentence.

`speak()` decided by asking whether a Gemini Live socket was open, and
`run()` opens that socket unconditionally. So a machine explicitly set to
``default_ai_provider: "Local"`` with ``offline_mode_enabled: true`` still had
every word handed to the cloud voice: the answer came from LFM3-Omni, Gemini's
Charon read it out, and the native omni TTS branch was unreachable no matter
what the user chose. The turn router consulted the provider; the voice funnel
did not, and the two disagreed.

These tests pin the single rule: the preference picks the voice, not the
connection.
"""

import asyncio
import inspect
import time
import winsound

import pytest

try:  # importing ``main`` pulls the vision stack, which needs cv2
    import cv2  # noqa: F401
except Exception:  # cv2's binding fails to load on some interpreters (e.g. py3.14)
    pytest.skip("cv2 unavailable in this interpreter; main imports the vision stack",
                allow_module_level=True)

import main
from main import SaturnLive


class StubUI:
    muted = False

    def __init__(self):
        self.subtitles = []

    def set_live_subtitles(self, who, text, interim=False):
        self.subtitles.append((who, text, interim))

    def set_state(self, state):
        pass

    def write_log(self, line):
        pass


class StubSession:
    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns=None, turn_complete=False):
        self.sent.append(turns)


class _InlineThread:
    """Runs the speak worker on the calling thread so assertions need no sleep."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        self._target, self._args, self._kwargs = target, tuple(args), dict(kwargs or {})

    def start(self):
        self._target(*self._args, **self._kwargs)


class _ThreadingShim:
    Thread = _InlineThread


@pytest.fixture
def controller(monkeypatch):
    """A SaturnLive with real routing code and every outside world stubbed."""

    class Dummy:
        speak = SaturnLive.speak
        _live_gemini_voice_active = SaturnLive._live_gemini_voice_active

        def __init__(self):
            self.ui = StubUI()
            self.session = StubSession()
            self._loop = object()
            self._in_live_tool = False
            self.speaking = []
            self.native_tts = []
            self.played = []
            self.handed_to_loop = []

        def set_speaking(self, value):
            self.speaking.append(value)

    dummy = Dummy()

    def fake_synth(text):
        dummy.native_tts.append(text)
        return b"RIFFfakewav"

    def fake_dispatch(coro, loop):
        dummy.handed_to_loop.append(loop)
        # Drive the coroutine by hand: the stubs never actually suspend, so no
        # event loop is needed to reach the session and observe what was sent.
        try:
            coro.send(None)
        except StopIteration:
            pass
        except Exception:
            pass
        return None

    monkeypatch.setattr(main.llama_server, "synthesize_speech", fake_synth)
    monkeypatch.setattr(main, "threading", _ThreadingShim)
    monkeypatch.setattr(winsound, "PlaySound",
                        lambda data, flags: dummy.played.append(bytes(data)))
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", fake_dispatch)

    def set_provider(provider, offline):
        monkeypatch.setattr(main.config_manager, "load_settings",
                            lambda: {"default_ai_provider": provider,
                                     "offline_mode_enabled": offline})

    set_provider("Local", True)
    dummy.set_provider = set_provider
    return dummy


def test_local_provider_uses_the_native_omni_voice_even_with_live_socket_open(controller):
    """The reported bug: Local selected, yet the cloud voice answered."""
    assert controller.session and controller._loop

    controller.speak("The farm has fifteen sheep.")

    assert controller.native_tts == ["The farm has fifteen sheep."]
    assert controller.played, "the omni WAV must actually reach the sound card"
    assert controller.handed_to_loop == [], "Gemini must not be asked to read it"
    assert controller.session.sent == []


def test_gemini_provider_keeps_the_duplex_voice(controller):
    """An explicit cloud choice still speaks through the Live session."""
    controller.set_provider("Google Gemini", False)

    controller.speak("Hello sir.")

    assert len(controller.session.sent) == 1
    assert controller.native_tts == []
    assert controller.played == []


@pytest.mark.parametrize("provider,offline", [
    ("Local", False),
    ("Gemini", True),      # offline mode outranks the stored cloud default
    ("OpenRouter", False),
])
def test_only_an_explicit_cloud_choice_hands_the_voice_to_gemini(controller, provider, offline):
    controller.set_provider(provider, offline)

    controller.speak("Status report.")

    assert controller.native_tts == ["Status report."]
    assert controller.session.sent == []


def test_unreadable_settings_do_not_silently_switch_the_voice(controller, monkeypatch):
    """Failing to read the preference must not mute or swap the engine."""
    def boom():
        raise OSError("settings file locked")

    monkeypatch.setattr(main.config_manager, "load_settings", boom)

    controller.speak("Still here.")

    assert len(controller.session.sent) == 1


def test_live_tool_flag_only_mutes_when_the_cloud_owns_the_voice(controller):
    """Duplex tool runs announce their own result, so speak() stood down.

    Standing down while the local provider owns the voice meant a finished
    background task was never announced at all.
    """
    controller._in_live_tool = True

    controller.speak("Download finished.")
    assert controller.native_tts == ["Download finished."], "nobody else will announce this"

    controller.native_tts.clear()
    controller.set_provider("Gemini", False)
    controller.speak("Download finished.")
    assert controller.session.sent == [] and controller.native_tts == []


def test_disconnected_session_speaks_locally(controller):
    controller.session = None

    controller.speak("Offline and alone.")

    assert controller.native_tts == ["Offline and alone."]


def test_reasoning_trace_never_reaches_either_voice(controller):
    """Subtitles and audio come from the same filtered string."""
    controller.speak("Let me think. 2+2 is 4.\n\nFINAL: The answer is 4.")

    spoken = controller.native_tts[0]
    assert spoken == "The answer is 4."
    assert controller.ui.subtitles[0][1] == spoken


def test_no_voice_route_keys_off_the_socket_alone():
    """The socket check caused this bug; it must not come back in any funnel."""
    source = inspect.getsource(main)

    assert "if self.session and self._loop:" not in source
    assert "is_cloud_gemini and self.session and self._loop" not in source

    # Speech and turn routing call the one predicate, so they cannot diverge.
    speak_body = source[source.index("def speak(self, text: str"):
                        source.index("def speak_error(")]
    assert "live_voice = self._live_gemini_voice_active()" in speak_body
    assert "if live_voice:" in speak_body

    # Every place that decides who speaks -- the funnel, the proactive nudge, and
    # the always-on Gemini duplex -- must consult the SAME predicate. A second,
    # socket-only check anywhere is how "I chose local but heard three voices"
    # happens, so none may exist beyond calls of this one predicate.
    assert "if self.session and self._loop" not in source
    assert "self._live_gemini_voice_active()" in source
    assert source.count("self._live_gemini_voice_active()") >= 3

    # The duplex drops Gemini's own audio whenever Gemini does not own the voice,
    # so Charon can never read a line the local voice is about to speak.
    recv_body = source[source.index("async def _receive_audio"):]
    assert "if response.data and self._live_gemini_voice_active():" in recv_body
    assert "not gemini_owns_voice" in recv_body
    assert "self._on_text_command" in recv_body


def test_proactive_nudge_uses_the_speech_funnel():
    """It used to be injected into the Live socket, so Local mode never heard it."""
    source = inspect.getsource(SaturnLive)
    start = source.index("engine.build_prompt(memory={})")
    window = source[start:start + 700]

    assert "self.speak(prompt" in window
    assert "send_client_content" not in window


def test_booting_local_voice_waits_for_itself_instead_of_leaking_generic(controller, monkeypatch):
    """The reported 'Microsoft voice first' bug.

    With the local brain selected, the omni model is still warming up when the
    first line is spoken, so the very first TTS call comes back empty. speak()
    used to fall straight through to the edge/Windows voice -- a stranger voice
    the user never picked. It must instead wake the local server and try again,
    and only that voice should reach the speakers.
    """
    state = {"ready": False}

    def flaky_synth(text):
        controller.native_tts.append(text)
        return b"RIFFlocal" if state["ready"] else None

    def fake_ensure_running():
        state["ready"] = True
        return True

    def leak_detector(text, *a, **k):
        controller.played.append("GENERIC-VOICE-LEAKED")

    monkeypatch.setattr(main.llama_server, "synthesize_speech", flaky_synth)
    monkeypatch.setattr(main.llama_server, "ensure_running", fake_ensure_running)
    import actions.attention_monitor as am
    monkeypatch.setattr(am, "_speak_edge_native", leak_detector)

    controller.speak("Good morning.")

    assert state["ready"], "the local server must be woken before giving up"
    assert controller.played == [b"RIFFlocal"], "only the local voice may be heard"
    assert "GENERIC-VOICE-LEAKED" not in controller.played
    # Called twice: once while still booting, once after the retry.
    assert controller.native_tts == ["Good morning.", "Good morning."]


class _OfferBot:
    """Binds only the greeting-offer methods so their decision is testable."""
    _handle_briefing_offer = SaturnLive._handle_briefing_offer
    _attention_matches = SaturnLive._attention_matches

    def __init__(self):
        self.spoken = []
        self._briefing_offer_pending = True
        self._briefing_offer_time = time.monotonic()
        self._briefing_narrative = "Two world stories and one local one."

    def speak(self, text, proactive=True):
        self.spoken.append(text)


def test_yes_after_greeting_reads_the_real_briefing():
    """A bare 'yes' must deliver the actual news, not a generic reply."""
    bot = _OfferBot()
    assert bot._handle_briefing_offer("yes") is True
    assert bot.spoken == ["Two world stories and one local one."]
    assert bot._briefing_offer_pending is False


def test_no_after_greeting_declines_without_reading_news():
    bot = _OfferBot()
    assert bot._handle_briefing_offer("no thanks") is True
    assert bot.spoken and "Two world stories" not in bot.spoken[0]
    assert bot._briefing_offer_pending is False


def test_stale_greeting_offer_does_not_ambush_the_user():
    """An unrelated 'yes' long after the greeting must fall through to the brain."""
    bot = _OfferBot()
    bot._briefing_offer_time = time.monotonic() - 300
    assert bot._handle_briefing_offer("yes") is False
    assert bot.spoken == []


def test_unrelated_reply_leaves_the_offer_open():
    """A non-answer is not an answer: keep the offer and speak nothing."""
    bot = _OfferBot()
    assert bot._handle_briefing_offer("what's the weather") is False
    assert bot.spoken == []
    assert bot._briefing_offer_pending is True
