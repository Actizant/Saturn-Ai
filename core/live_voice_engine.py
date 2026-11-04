"""
core/live_voice_engine.py
Ultra-low-latency real-time live voice engine for Saturn.
Designed to feel as responsive as ChatGPT's Advanced Voice Mode.

Key capabilities:
1. Continuous Bidirectional Streaming (WebSockets / Multimodal Live API).
2. Hands-Free Voice Activation (Zero push-to-talk, dynamic noise floor VAD, ~280ms speech offset detection).
3. Instant Interruption Handling (Barge-in sub-30ms cut-off, audio buffer flush, state reset).
4. Direct Audio-to-Audio streaming (16kHz in / 24kHz out, zero-budget thinking, lean latency profile).
5. Multi-client support (PC microphone/speakers, Browser Web Audio, Mobile Connect).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import numpy as np

# Audio Constants
SEND_SAMPLE_RATE = 16000     # 16 kHz Mono input for Gemini Live / Whisper
RECEIVE_SAMPLE_RATE = 24000  # 24 kHz Mono output from Gemini Live
CHUNK_SIZE = 512             # 32ms per frame @ 16kHz for ultra-low latency VAD & transport
CHANNELS = 1

logger = logging.getLogger("SaturnLiveVoice")


class VoiceActivityDetector:
    """
    High-responsiveness, low-latency streaming Voice Activity Detector (VAD).
    Combines adaptive noise floor tracking, Short-Term Energy (STE), and Zero-Crossing Rate (ZCR).
    """

    def __init__(
        self,
        sample_rate: int = SEND_SAMPLE_RATE,
        chunk_size: int = CHUNK_SIZE,
        energy_threshold_multiplier: float = 2.4,
        min_speech_frames: int = 2,           # ~64ms of speech to trigger onset
        silence_timeout_ms: int = 300,        # ~300ms silence to trigger speech complete
    ):
        self.sample_rate = sample_rate
        self.chunk_size = chunk_size
        self.energy_multiplier = energy_threshold_multiplier
        self.min_speech_frames = min_speech_frames
        self.silence_frames_threshold = max(2, int((silence_timeout_ms / 1000.0) * (sample_rate / chunk_size)))

        # Adaptive noise floor
        self.noise_floor = 18.0
        self.noise_history = collections.deque(maxlen=40)
        self.is_speech_active = False
        self.speech_frames_count = 0
        self.silence_frames_count = 0

    def process_frame(self, pcm_data: np.ndarray) -> tuple[bool, float, Optional[str]]:
        """
        Process a PCM int16 audio frame.
        Returns: (is_voice_now, normalized_energy, event)
        event can be: 'speech_start', 'speech_stop', or None.
        """
        if len(pcm_data) == 0:
            return False, 0.0, None

        float_data = pcm_data.astype(np.float32)
        rms = float(np.sqrt(np.mean(np.square(float_data))))
        normalized_energy = min(1.0, rms / 1500.0)

        # Dynamic noise floor update during non-speech periods
        if not self.is_speech_active:
            self.noise_history.append(rms)
            if len(self.noise_history) >= 10:
                self.noise_floor = max(8.0, float(np.percentile(self.noise_history, 25)))

        threshold = max(22.0, self.noise_floor * self.energy_multiplier)
        is_above_thresh = rms > threshold

        event = None

        if is_above_thresh:
            self.silence_frames_count = 0
            self.speech_frames_count += 1
            if not self.is_speech_active and self.speech_frames_count >= self.min_speech_frames:
                self.is_speech_active = True
                event = "speech_start"
        else:
            self.speech_frames_count = 0
            if self.is_speech_active:
                self.silence_frames_count += 1
                if self.silence_frames_count >= self.silence_frames_threshold:
                    self.is_speech_active = False
                    self.silence_frames_count = 0
                    event = "speech_stop"

        return self.is_speech_active, normalized_energy, event

    def reset(self):
        self.is_speech_active = False
        self.speech_frames_count = 0
        self.silence_frames_count = 0


class LiveVoiceEngine:
    """
    Central real-time bidirectional live voice streaming engine.
    Connects to Google GenAI Multimodal Live API with low-latency duplex audio streaming.
    Handles barge-in, hands-free VAD, state dispatch, and multi-sink audio playback.
    """

    _instance: Optional[LiveVoiceEngine] = None

    @classmethod
    def get_instance(cls) -> LiveVoiceEngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.vad = VoiceActivityDetector()
        self.state: str = "IDLE"  # IDLE, LISTENING, THINKING, SPEAKING, INTERRUPTED
        self._state_lock = threading.Lock()

        # Session and Queues
        self.session = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.in_audio_queue: asyncio.Queue = asyncio.Queue()     # Audio from mic/clients to model
        self.out_audio_queue: asyncio.Queue = asyncio.Queue()    # Audio from model to local playback/sinks
        self._running = False
        self._active_task_group = None

        # Sinks (WebSockets or local speakers)
        self._ws_listeners: Set[Any] = set()
        self._ws_lock = threading.Lock()

        # Callbacks
        self.on_state_change: Optional[Callable[[str], None]] = None
        self.on_user_transcript: Optional[Callable[[str, bool], None]] = None
        self.on_ai_transcript: Optional[Callable[[str, bool], None]] = None
        self.on_audio_level: Optional[Callable[[float, str], None]] = None  # (level, 'user'|'ai')
        self.on_interrupted: Optional[Callable[[], None]] = None
        self.on_tool_call: Optional[Callable[[str, dict], Coroutine[Any, Any, Any]]] = None

        # Hardware stream handles
        self._mic_stream = None
        self._spk_stream = None
        self._spk_active = False

        # Metrics
        self.last_speech_start_time = 0.0
        self.last_speech_stop_time = 0.0
        self.last_first_audio_time = 0.0
        self.turn_latency_ms = 0

    def set_state(self, new_state: str):
        with self._state_lock:
            if self.state == new_state:
                return
            old_state = self.state
            self.state = new_state

        if self.on_state_change:
            try:
                self.on_state_change(new_state)
            except Exception as e:
                logger.error(f"Error in on_state_change: {e}")

        # Broadcast state to all connected live voice WebSockets
        self.broadcast_control_json({
            "type": "state",
            "state": new_state.lower(),
            "prev_state": old_state.lower(),
            "timestamp": time.time(),
        })

    def register_ws_client(self, websocket: Any):
        with self._ws_lock:
            self._ws_listeners.add(websocket)
        logger.info(f"[LiveVoice] Registered WebSocket voice client. Total: {len(self._ws_listeners)}")

    def unregister_ws_client(self, websocket: Any):
        with self._ws_lock:
            self._ws_listeners.discard(websocket)
        logger.info(f"[LiveVoice] Unregistered WebSocket voice client. Total: {len(self._ws_listeners)}")

    def _get_active_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        if self._loop and self._loop.is_running():
            return self._loop
        try:
            loop = asyncio.get_running_loop()
            if loop and loop.is_running():
                self._loop = loop
                return loop
        except RuntimeError:
            pass
        return None

    def broadcast_control_json(self, data: dict):
        """Thread-safe and async-safe dispatch to connected WebSocket clients."""
        msg_str = json.dumps(data)
        loop = self._get_active_loop()
        with self._ws_lock:
            dead = set()
            for ws in list(self._ws_listeners):
                try:
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(ws.send_text(msg_str), loop)
                except Exception:
                    dead.add(ws)
            self._ws_listeners.difference_update(dead)

    def broadcast_audio_chunk(self, chunk: bytes):
        """Broadcast 24kHz PCM audio chunk to all connected live voice WebSockets."""
        loop = self._get_active_loop()
        with self._ws_lock:
            dead = set()
            for ws in list(self._ws_listeners):
                try:
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(ws.send_bytes(chunk), loop)
                except Exception:
                    dead.add(ws)
            self._ws_listeners.difference_update(dead)

    # ── Immediate Interruption / Barge-In ─────────────────────────────────────
    def trigger_interruption(self, source: str = "local"):
        """
        Sub-30ms Instant Interruption:
        - Drops all queued output audio immediately.
        - Notifies all audio output sinks (hardware speaker & WebSockets) to stop playing.
        - Switches state to LISTENING immediately.
        """
        with self._state_lock:
            if self.state not in ("SPEAKING", "THINKING"):
                return
            logger.info(f"[LiveVoice] ⚡ BARGE-IN TRIGGERED from {source}!")
            self.state = "INTERRUPTED"

        # 1. Drain pending audio output queue
        drain_count = 0
        while not self.out_audio_queue.empty():
            try:
                self.out_audio_queue.get_nowait()
                drain_count += 1
            except Exception:
                break

        # 2. Notify all web clients to abort current audio playback buffer immediately
        self.broadcast_control_json({
            "type": "interrupt",
            "source": source,
            "timestamp": time.time(),
        })

        # 3. Fire local interruption callback
        if self.on_interrupted:
            try:
                self.on_interrupted()
            except Exception as e:
                logger.error(f"Error in on_interrupted callback: {e}")

        # 4. Immediate state switch back to LISTENING
        self.set_state("LISTENING")

    # ── Audio Ingestion & VAD ────────────────────────────────────────────────
    def feed_pcm_audio(self, pcm_bytes: bytes, source: str = "mic"):
        """
        Receives raw 16kHz 16-bit Mono PCM audio chunks (from local mic or WebSocket).
        Processes VAD, checks for barge-in, and pushes to model inbound queue.
        """
        if not pcm_bytes:
            return

        try:
            pcm_array = np.frombuffer(pcm_bytes, dtype=np.int16)
        except Exception:
            return

        is_voice, energy, event = self.vad.process_frame(pcm_array)

        # Dispatch audio energy level for real-time visualizer
        if self.on_audio_level:
            try:
                self.on_audio_level(energy, "user")
            except Exception:
                pass

        # BARGE-IN: If AI is speaking or thinking and user starts speaking, interrupt instantly!
        # Do not auto-interrupt on raw PC mic because desktop speaker audio bleeds into the mic without AEC
        if is_voice and self.state in ("SPEAKING", "THINKING"):
            if source != "pc_mic":
                self.trigger_interruption(source=source)

        if event == "speech_start":
            self.last_speech_start_time = time.time()
            if self.state != "SPEAKING":
                self.set_state("LISTENING")
            self.broadcast_control_json({
                "type": "vad",
                "speaking": True,
                "energy": round(energy, 3),
            })

        elif event == "speech_stop":
            self.last_speech_stop_time = time.time()
            # If user stopped speaking and we are in LISTENING, transition to THINKING
            if self.state == "LISTENING":
                self.set_state("THINKING")
            self.broadcast_control_json({
                "type": "vad",
                "speaking": False,
                "energy": round(energy, 3),
            })

        # Forward audio chunk to the active Gemini Live session
        if self._loop and self._loop.is_running() and self.session:
            self._loop.call_soon_threadsafe(
                self.in_audio_queue.put_nowait,
                {"data": pcm_bytes, "mime_type": "audio/pcm"}
            )

    # ── Gemini Live Session Management ───────────────────────────────────────
    def _get_api_key(self) -> str:
        try:
            from core.user_paths import get_user_data_dir
            from core.secret_store import load_json
            api_file = get_user_data_dir() / "config" / "api_keys.json"
            if api_file.exists():
                return load_json(api_file).get("gemini_api_key", "")
        except Exception:
            pass
        return os.environ.get("GEMINI_API_KEY", "")

    def _build_live_config(self) -> Any:
        from google.genai import types

        sys_prompt = (
            "You are Saturn, a real-time conversational AI operating system running on this Windows PC. "
            "You are communicating over ultra-low-latency bidirectional live voice. "
            "STYLE GUIDELINES:\n"
            "- Speak naturally, conversationally, concisely, and directly.\n"
            "- Deliver snappy responses in 1-2 short sentences whenever possible unless asked for detail.\n"
            "- Do not recite markdown formatting, bullet symbols, asterisks, or source citations.\n"
            "- If the user cuts you off or changes topic, acknowledge smoothly and pivot immediately.\n"
            "- You have full control over this PC and can trigger desktop tools when needed.\n"
            "- Stay silent on initial connection until the user speaks to you."
        )

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction=sys_prompt,
            thinking_config=types.ThinkingConfig(thinking_budget=0),  # Zero latency thinking budget
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Charon")
                )
            ),
        )

    async def send_text_turn(self, text: str):
        """Sends a text utterance to Gemini Live session."""
        if not self.session:
            return
        from google.genai import types
        self.set_state("THINKING")
        content = types.Content(role="user", parts=[types.Part.from_text(text=text)])
        await self.session.send_client_content(turns=[content], turn_complete=True)

    async def speak_live_text(self, text: str):
        """Asks Gemini Live to speak an explicit statement or confirmation in its Charon voice."""
        self.broadcast_control_json({
            "type": "transcript",
            "role": "model",
            "text": text,
            "interim": False,
        })
        if not self.session:
            return
        from google.genai import types
        self.set_state("THINKING")
        prompt = (
            f"Deliver this exact update aloud to the user right now in your Charon voice. "
            f"Speak directly to the user as Saturn without saying thank you or acknowledging this prompt:\n{text}"
        )
        content = types.Content(role="user", parts=[types.Part.from_text(text=prompt)])
        await self.session.send_client_content(turns=[content], turn_complete=True)

    async def _send_audio_loop(self):
        """Continuously streams real-time microphone/client audio to the Gemini session."""
        while self._running:
            try:
                frame = await self.in_audio_queue.get()
                if self.session:
                    await self.session.send_realtime_input(media=frame)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LiveVoice] Error in _send_audio_loop: {e}")
                await asyncio.sleep(0.05)

    async def _receive_audio_loop(self):
        """Receives real-time streaming audio & transcripts from Gemini Live."""
        out_transcript_buf: List[str] = []
        in_transcript_buf: List[str] = []

        while self._running:
            try:
                if not self.session:
                    await asyncio.sleep(0.1)
                    continue

                async for response in self.session.receive():
                    if not self._running:
                        break

                    # 1. Incoming Audio Stream
                    if response.data:
                        # If we were thinking or listening, switch to SPEAKING the instant the first chunk arrives
                        if self.state != "SPEAKING":
                            self.set_state("SPEAKING")
                            now = time.time()
                            if self.last_speech_stop_time > 0:
                                self.turn_latency_ms = int((now - self.last_speech_stop_time) * 1000)
                                self.broadcast_control_json({
                                    "type": "metrics",
                                    "latency_ms": self.turn_latency_ms,
                                })

                        # Dispatch audio chunk to local speaker queue and WebSockets
                        self.out_audio_queue.put_nowait(response.data)
                        self.broadcast_audio_chunk(response.data)

                        # Audio energy calculation for AI visualizer
                        try:
                            pcm_out = np.frombuffer(response.data, dtype=np.int16)
                            lvl = float(np.sqrt(np.mean(np.square(pcm_out.astype(np.float32)))))
                            ai_energy = min(1.0, lvl / 2200.0)
                            if self.on_audio_level:
                                self.on_audio_level(ai_energy, "ai")
                            self.broadcast_control_json({
                                "type": "energy",
                                "role": "ai",
                                "level": round(ai_energy, 3),
                            })
                        except Exception:
                            pass

                    # 2. Server Content & Live Transcription
                    if response.server_content:
                        sc = response.server_content

                        # Model speech transcription
                        if sc.output_transcription and sc.output_transcription.text:
                            txt = sc.output_transcription.text
                            out_transcript_buf.append(txt)
                            if self.on_ai_transcript:
                                self.on_ai_transcript(txt, False)
                            self.broadcast_control_json({
                                "type": "transcript",
                                "role": "model",
                                "text": txt,
                                "accumulated": "".join(out_transcript_buf),
                                "interim": True,
                            })

                        # User speech transcription
                        if sc.input_transcription and sc.input_transcription.text:
                            in_txt = sc.input_transcription.text
                            in_transcript_buf.append(in_txt)
                            if self.on_user_transcript:
                                self.on_user_transcript(in_txt, False)
                            self.broadcast_control_json({
                                "type": "transcript",
                                "role": "user",
                                "text": in_txt,
                                "accumulated": "".join(in_transcript_buf),
                                "interim": True,
                            })

                        # Turn Complete
                        if sc.turn_complete:
                            full_user = "".join(in_transcript_buf).strip()
                            full_ai = "".join(out_transcript_buf).strip()

                            if full_user:
                                self.broadcast_control_json({
                                    "type": "transcript",
                                    "role": "user",
                                    "text": full_user,
                                    "interim": False,
                                })
                            if full_ai:
                                self.broadcast_control_json({
                                    "type": "transcript",
                                    "role": "model",
                                    "text": full_ai,
                                    "interim": False,
                                })

                            in_transcript_buf.clear()
                            out_transcript_buf.clear()

                            # If no more audio chunks queued, transition back to LISTENING
                            if self.out_audio_queue.empty() and self.state == "SPEAKING":
                                self.set_state("LISTENING")

                    # 3. Tool Calls
                    if response.tool_call and self.on_tool_call:
                        self.set_state("THINKING")
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            res = await self.on_tool_call(fc.name, dict(fc.args or {}))
                            fn_responses.append(res)
                        await self.session.send_tool_response(function_responses=fn_responses)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[LiveVoice] Error in _receive_audio_loop: {e}")
                await asyncio.sleep(0.5)

    async def _play_local_audio_loop(self):
        """Plays 24kHz audio chunks to the PC local speakers with low latency."""
        import sounddevice as sd

        try:
            self._spk_stream = sd.RawOutputStream(
                samplerate=RECEIVE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=0,
            )
            self._spk_stream.start()
            logger.info("[LiveVoice] 🔊 Local PC speaker stream active.")

            while self._running:
                try:
                    # Wait up to 350ms for the next chunk
                    chunk = await asyncio.wait_for(self.out_audio_queue.get(), timeout=0.35)
                    await asyncio.to_thread(self._spk_stream.write, chunk)
                except asyncio.TimeoutError:
                    if self.state == "SPEAKING" and self.out_audio_queue.empty():
                        self.set_state("LISTENING")
                    continue
                except asyncio.CancelledError:
                    break
        except Exception as e:
            logger.warning(f"[LiveVoice] Local speaker stream disabled: {e}")
        finally:
            if self._spk_stream:
                try:
                    self._spk_stream.stop()
                    self._spk_stream.close()
                except Exception:
                    pass

    async def _capture_local_mic_loop(self):
        """Captures 16kHz audio from the local PC microphone and feeds to VAD + Model."""
        import sounddevice as sd

        def callback(indata, frames, time_info, status):
            if not self._running:
                return
            pcm_bytes = indata.tobytes()
            self.feed_pcm_audio(pcm_bytes, source="local_mic")

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=0,
                callback=callback,
            ):
                logger.info("[LiveVoice] 🎤 Local PC microphone stream active.")
                while self._running:
                    await asyncio.sleep(0.1)
        except Exception as e:
            logger.warning(f"[LiveVoice] Local mic stream disabled: {e}")

    # ── Main Run Loop ────────────────────────────────────────────────────────
    async def run(self, enable_local_hardware: bool = True):
        """Starts the persistent live streaming duplex connection."""
        from google import genai

        self._running = True
        self._loop = asyncio.get_running_loop()
        api_key = self._get_api_key()

        if not api_key:
            logger.error("[LiveVoice] Missing Gemini API key! Live voice disabled.")
            self.set_state("ERROR")
            return

        client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})
        # saturn:live-api-model - the Live API keeps its own model namespace
        # (v1beta, duplex audio). These are not text models, so the retirement
        # table in core/gemini_models.py deliberately does not touch them; the
        # repo-wide guard in tests/test_gemini_models.py skips this block.
        model_candidates = [
            "models/gemini-2.5-flash-native-audio-latest",
            "models/gemini-2.5-flash-native-audio-preview-12-2025",
            "models/gemini-2.0-flash-exp",
        ]

        while self._running:
            connected = False
            for target_model in model_candidates:
                try:
                    logger.info(f"[LiveVoice] Connecting to {target_model}...")
                    self.set_state("CONNECTING")
                    config = self._build_live_config()

                    connect_cm = client.aio.live.connect(model=target_model, config=config)
                    session = await asyncio.wait_for(connect_cm.__aenter__(), timeout=8.0)
                    self.session = session
                    connected = True
                    logger.info(f"[LiveVoice] ✅ Connected to {target_model}!")
                    self.set_state("LISTENING")

                    try:
                        async with asyncio.TaskGroup() as tg:
                            self._active_task_group = tg
                            tg.create_task(self._send_audio_loop())
                            tg.create_task(self._receive_audio_loop())
                            if enable_local_hardware:
                                tg.create_task(self._capture_local_mic_loop())
                                tg.create_task(self._play_local_audio_loop())
                    finally:
                        try:
                            await connect_cm.__aexit__(None, None, None)
                        except Exception:
                            pass
                        self.session = None
                    break  # If session finished cleanly, exit model loop
                except Exception as e:
                    logger.warning(f"[LiveVoice] Connection attempt failed on {target_model}: {e}")
                    await asyncio.sleep(1.0)

            if not self._running:
                break

            logger.info("[LiveVoice] 🔄 Reconnecting session in 3s...")
            self.set_state("RECONNECTING")
            await asyncio.sleep(3.0)

    def stop(self):
        """Stops the live voice engine gracefully."""
        self._running = False
        self.set_state("IDLE")
