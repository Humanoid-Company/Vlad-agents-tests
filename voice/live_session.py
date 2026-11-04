"""GPT-Live voice session with Responses delegation (the session tools: voice, name, language…)."""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI, OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_LIVE_AUDIO_RATE,
    OPENAI_LIVE_BACKEND_EFFORT,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_PARALLEL_TOOLS,
    OPENAI_LIVE_VOICE,
    TTS_MODEL,
    TTS_VOICE,
    VOICE_BARGE_IN_CONFIRM_MS,
    VOICE_BARGE_IN_COOLDOWN_MS,
    VOICE_BARGE_IN_DUCK_VOLUME,
    VOICE_BARGE_IN_ECHO_MARGIN,
    VOICE_BARGE_IN_ENERGY_MARGIN,
    VOICE_BARGE_IN_ENERGY_MARGIN_PLAYING,
    VOICE_BARGE_IN_MIN_SPEECH_MS,
    VOICE_BARGE_IN_ONSET_FRAMES,
    VOICE_BARGE_IN_REJECT_SILENCE_MS,
    VOICE_BARGE_IN_USE_ENERGY_GATE,
    VOICE_BUSY_CUE_DELAY_MS,
    VOICE_BUSY_CUE_MAX_PER_TURN,
    VOICE_BUSY_CUE_SECOND_DELAY_MS,
    VOICE_BUSY_CUES_ENABLED,
    VOICE_LOCAL_BARGE_IN,
)
from prompts.live_prompt import woken_request_note, work_progress_note
from tools.executor import ToolExecutionContext, ToolExecutor
from voice.barge_in_gate import BargeInAction, BargeInGate, BargeInState
from voice.busy_cues import BusyCueController
from voice.conversation import ConversationLog, remember_tool_result
from voice.delegation import (
    RESPONSE_FINISHED_TYPES,
    DelegatedResponseTracker,
    extract_completed_function_call,
    responses_delegation,
)
from voice.interrupt_intent import classify_interjection, is_backchannel_utterance
from voice.local_vad import LocalSpeechDetector
from voice.options import api_voice
from voice.playback import PlaybackTracker
from voice.wake_phrases import is_stop

logger = logging.getLogger(__name__)

# How much of the assistant's recent speech to compare mic transcripts against (echo check).
_ECHO_CONTEXT_CHARS = 400
# All results are in but the response's finish event hasn't come: continue anyway after this.
_CONTINUE_FALLBACK_S = 2.0
# Assistant text this far apart is a new utterance (her «угу» vs the answer that follows).
_BURST_GAP_S = 0.8
# Words heard with no open barge-in candidate are judged together within this window.
_IDLE_INTERJECTION_WINDOW_S = 2.0
# After a barge-in, drop the old answer's audio at most this long (once the user is quiet).
_STALE_DROP_MAX_S = 1.5

_SHUTDOWN_WAIT_S = 3.0

# Delegated work (a slow tool) keeps her silent: this long after it started she says once that she
# is on it, when she has been quiet for _PROGRESS_QUIET_S; past _PROGRESS_GIVE_UP_S she says nothing
# (same timings as server/live_bridge.py). The user's request may still be transcribed meanwhile.
_PROGRESS_AFTER_S = 5.0
_PROGRESS_QUIET_S = 2.5
_PROGRESS_GIVE_UP_S = 20.0
_PROGRESS_INPUT_GRACE_S = 1.5
# A setting change (speed/style, name, language) is an instruction, and one that lands mid-answer
# derails it (instructions.append is how a barge-in stops her): it waits for this much quiet.
_INSTRUCTION_QUIET_S = 2.5
_INSTRUCTION_MAX_WAIT_S = 120.0

# Same wording as steer("pause") in web/app.js.
_PAUSE_STEER = (
    "The user said «Дякую, Єва»: stop speaking immediately and stay completely silent. Do not reply "
    "to it. You will be woken again later — then continue with full memory of this conversation."
)


def _event_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


_cue_client: OpenAI | None = None


def _synthesize_cue_pcm(text: str) -> bytes | None:
    """Local OpenAI TTS → raw PCM16 @ 24 kHz (no Live reasoning cycle)."""
    global _cue_client
    if not text.strip():
        return None
    try:
        if _cue_client is None:
            _cue_client = OpenAI(api_key=OPENAI_API_KEY)  # one client: reuses its HTTPS connection
        response = _cue_client.audio.speech.create(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            input=text,
            response_format="pcm",
        )
        return response.content
    except Exception:
        logger.exception("BUSY_CUE TTS failed")
        return None


class LiveVoiceSession:
    """Full-duplex GPT-Live session. Realtime VAD/response.create turn hacks are not used."""

    def __init__(
        self,
        *,
        tool_executor: ToolExecutor,
        voice: str | None = None,
        live_model: str | None = None,
        backend_model: str | None = None,
        audio_rate: int | None = None,
        on_user_transcript: Callable[[str], None] | None = None,
        session_id: str | None = None,
        cue_synthesize: Callable[[str], bytes | None] | None = None,
        conversation: ConversationLog | None = None,
    ) -> None:
        self._executor = tool_executor
        self._voice = voice or OPENAI_LIVE_VOICE
        self._live_model = live_model or OPENAI_LIVE_MODEL
        self._backend_model = backend_model or OPENAI_LIVE_BACKEND_MODEL
        self._audio_rate = audio_rate or OPENAI_LIVE_AUDIO_RATE
        self._on_user_transcript = on_user_transcript
        self.session_id = session_id or str(uuid.uuid4())

        self.player = PlaybackTracker(sample_rate=self._audio_rate)
        self._turns: list[dict] = []
        self._input_buf = ""
        self._output_buf = ""
        # History shared across sessions (voice changes, sleep/wake): read at startup, fed here.
        self._conversation = conversation
        self._recent_user_text = ""  # rolling window for «Дякую, Єва» / «зміни голос …»
        self._pause_requested = False
        # Paused («Дякую, Єва» or silence): the session stays open for a quick wake, but the model
        # hears nothing (input muted, no mic sent) and nothing it says is played or remembered.
        self._paused = False
        self._input_muted = False
        self._mic_drain: Callable[[], None] | None = None
        # Results of tools that finished during a pause: she said them into the muted session and
        # nobody heard them — the driver passes them on at the wake (take_unheard).
        self._unheard: list[str] = []
        # Last time anyone spoke (idle pause) and the transcripts' timing (quiet checks).
        self._last_activity = time.monotonic()
        self._last_output_at = 0.0
        self._last_input_at = 0.0
        # Recent lines of both sides for the tools (a voice change must have been asked for; a
        # «так» acts on what she offered). A user's turn ends when she answers, hers when they speak.
        self._user_turns: list[str] = []
        self._turn_input = ""
        self._assistant_turns: list[str] = []
        self._turn_output = ""
        # Instructions waiting for her to be quiet: kind → text (a newer one of a kind replaces it).
        self._quiet_instructions: dict[str, str] = {}
        self._quiet_sender: asyncio.Task | None = None
        # Delegated work in progress: key → tools called so far; the task that says «шукаю…».
        self._work: dict[str, list[str]] = {}
        self._progress: dict[str, asyncio.Task] = {}
        self._answering: set[str] = set()

        self._sleep_requested = False
        self._voice_restart_requested = False
        self._closed = threading.Event()
        self._started = threading.Event()
        self._error: BaseException | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._connection: Any = None
        self._client: AsyncOpenAI | None = None
        self._mic_read: Callable[[], bytes] | None = None
        self._live_instructions = ""
        self._backend_instructions = ""
        self._tasks: set[asyncio.Task] = set()
        self._tool_tasks: set[asyncio.Task] = set()
        self._responses = DelegatedResponseTracker()  # when to continue after tool results
        self._delegation_ids: dict[str, str] = {}  # response_id -> delegation_id
        self._last_barge_in_at = 0.0
        self._close_sent = False
        self._session_closing = False

        # Local fast barge-in: two-stage gate (duck → confirm). Not VAD-alone cancel.
        self._local_barge_in = VOICE_LOCAL_BARGE_IN
        self._barge_cooldown_s = max(0.05, VOICE_BARGE_IN_COOLDOWN_MS / 1000.0)
        self._vad = LocalSpeechDetector(
            sample_rate=self._audio_rate,
            onset_frames=VOICE_BARGE_IN_ONSET_FRAMES,
        )
        self._barge_gate = BargeInGate(
            vad=self._vad,
            confirm_ms=VOICE_BARGE_IN_CONFIRM_MS,
            min_speech_ms=VOICE_BARGE_IN_MIN_SPEECH_MS,
            duck_volume=VOICE_BARGE_IN_DUCK_VOLUME,
            use_energy_gate=VOICE_BARGE_IN_USE_ENERGY_GATE,
            energy_margin=VOICE_BARGE_IN_ENERGY_MARGIN,
            energy_margin_playing=VOICE_BARGE_IN_ENERGY_MARGIN_PLAYING,
            reject_silence_ms=VOICE_BARGE_IN_REJECT_SILENCE_MS,
            echo_margin=VOICE_BARGE_IN_ECHO_MARGIN,
        )
        self._speech_onset_mono: float | None = None
        self._duck_volume = VOICE_BARGE_IN_DUCK_VOLUME
        # Response-aware output gate (not just SUPPRESS_MS time window).
        self._assistant_generation = 0
        self._play_assistant_audio = True
        self._awaiting_output_gap = False
        self._last_output_delta_at = 0.0
        # What she has said since her last pause: «Угу.» while the user talks is not an answer
        # to barge into (see _speaking_backchannel).
        self._assistant_burst = ""
        self._assistant_burst_at = 0.0
        self._output_gap_ms = 220.0
        self._barge_in_mono = 0.0
        self._stale_dropped_chunks = 0
        self._stale_response = False
        # Echo detection context + words heard after a short candidate already ended.
        self._recent_assistant_text = ""
        self._idle_interjection = ""
        self._idle_interjection_at = 0.0

        synth = cue_synthesize or _synthesize_cue_pcm
        self._busy_cues = BusyCueController(
            play_cue=self._play_busy_cue,
            synthesize=synth,
            enabled=VOICE_BUSY_CUES_ENABLED,
            first_delay_ms=VOICE_BUSY_CUE_DELAY_MS,
            second_delay_ms=VOICE_BUSY_CUE_SECOND_DELAY_MS,
            max_per_turn=VOICE_BUSY_CUE_MAX_PER_TURN,
        )

    @property
    def sleep_requested(self) -> bool:
        return self._sleep_requested

    @property
    def voice_restart_requested(self) -> bool:
        return self._voice_restart_requested

    def request_sleep(self) -> None:
        self._sleep_requested = True

    @property
    def pause_requested(self) -> bool:
        """«Дякую, Єва»: back to waiting for the wake phrase, conversation kept, no goodbye."""
        return self._pause_requested

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def closed(self) -> bool:
        """The connection is gone — asked for, or dropped (network, server, error)."""
        return self._closed.is_set()

    @property
    def busy(self) -> bool:
        """A tool is still running (a pause must not close the session yet)."""
        return any(not task.done() for task in self._tool_tasks)

    def idle_for(self) -> float:
        """Seconds since anyone last said anything."""
        return time.monotonic() - self._last_activity

    def pause(self, *, reason: str) -> None:
        """Mute her without closing the session (silence, or the driver's own decision)."""
        if self._paused:
            return
        self._enter_pause(reason)
        self._submit(self._silence_for_pause())

    def resume(self) -> None:
        """Woken within the pause: she hears the mic again (the driver then tells her what to say)."""
        if not self._paused:
            return
        if self._mic_drain is not None:
            self._mic_drain()  # what the mic buffered during the pause is not for her
        self._paused = False
        self._pause_requested = False
        self._recent_user_text = ""
        self._last_activity = time.monotonic()
        self._input_muted = False
        self._run_coro(self._unmute())
        logger.info("live.resume session_id=%s", self.session_id)

    def request_voice_restart(self) -> None:
        self._voice_restart_requested = True
        self._sleep_requested = True

    def get_turns(self) -> list[dict]:
        return list(self._turns)

    def connect(
        self,
        instructions: str,
        *,
        mic_read_chunk: Callable[[], bytes],
        backend_instructions: str | None = None,
        mic_drain: Callable[[], None] | None = None,
    ) -> None:
        if self._thread is not None:
            raise RuntimeError("LiveVoiceSession already connected")
        self._live_instructions = instructions
        self._backend_instructions = backend_instructions or ""
        self._mic_read = mic_read_chunk
        self._mic_drain = mic_drain
        self._closed.clear()
        self._started.clear()
        self._error = None
        self.player.start()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="gpt-live-session")
        self._thread.start()
        if not self._started.wait(timeout=30):
            self.close()
            raise TimeoutError("GPT-Live session.started timed out")
        if self._error is not None:
            raise RuntimeError(f"GPT-Live failed to start: {self._error}") from self._error
        # Preload busy-cue PCM off the voice path (best-effort).
        if self._busy_cues.enabled:
            threading.Thread(
                target=self._busy_cues.preload,
                daemon=True,
                name="busy-cue-preload",
            ).start()
        logger.info(
            "live.session.started session_id=%s model=%s backend=%s local_barge_in=%s busy_cues=%s",
            self.session_id,
            self._live_model,
            self._backend_model,
            self._local_barge_in,
            self._busy_cues.enabled,
        )

    def run_until_idle(self) -> None:
        """Block until sleep/voice restart or fatal error."""
        while not self._closed.wait(timeout=0.1):
            if self._error is not None and not self._sleep_requested:
                raise RuntimeError(f"GPT-Live session error: {self._error}") from self._error
            if self._sleep_requested and self._closed.is_set():
                break
        if self._thread is not None:
            self._thread.join(timeout=20)

    def append_instruction(self, text: str, *, when_quiet: str | None = None) -> None:
        """when_quiet: a kind («delivery», «name», «language») — sent once she is quiet; a newer
        instruction of the same kind replaces one still waiting."""
        if when_quiet:
            self._submit(self._queue_quiet_instruction(when_quiet, text))
        else:
            self._submit(self._append_instruction(text))

    def speak_context(self, text: str) -> None:
        self._run_coro(self._speak_context(text))

    def stop_playback(self) -> None:
        self.player.interrupt()
        self._call_on_loop(lambda: self._busy_cues.cancel(reason="stop_playback"))

    def take_unheard(self) -> list[str]:
        """Tool results nobody heard (they came during a pause); cleared once taken."""
        results, self._unheard = self._unheard, []
        return results

    def close(self) -> None:
        """Graceful close — do not wait forever for tools still running in worker threads."""
        self._sleep_requested = True
        self._session_closing = True
        self._call_on_loop(lambda: self._busy_cues.cancel(reason="session_close"))
        loop = self._loop
        if loop is not None and loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
            try:
                fut.result(timeout=_SHUTDOWN_WAIT_S)
            except TimeoutError:
                logger.warning(
                    "live.session.closed shutdown timeout session_id=%s — forcing local cleanup",
                    self.session_id,
                )
                try:
                    loop.call_soon_threadsafe(self._cancel_owned_tasks_soon)
                except Exception:
                    pass
            except Exception:
                logger.exception("live.session.closed shutdown error session_id=%s", self.session_id)
        self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.player.close()
        logger.info("live.session.closed session_id=%s", self.session_id)

    def _cancel_owned_tasks_soon(self) -> None:
        for task in list(self._tool_tasks) + list(self._tasks):
            if not task.done():
                task.cancel()

    # ── thread / asyncio ──────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            self._error = exc
            logger.exception("live.error session_id=%s", self.session_id)
        finally:
            self._closed.set()
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._loop = None

    def _run_coro(self, coro) -> Any:
        """Run on the session loop and wait — from another thread. On the loop itself (a tool
        handler) waiting would block the loop that has to run it: schedule it instead."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return None
        if self._on_loop(loop):
            self._spawn(coro)
            return None
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=15)

    def _submit(self, coro) -> None:
        """Run on the session loop without waiting for it (any thread)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            coro.close()
            return
        if self._on_loop(loop):
            self._spawn(coro)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    def _call_on_loop(self, fn: Callable[[], Any]) -> None:
        """asyncio tasks may only be cancelled from their own loop: from another thread (the driver's
        idle pause, stop_playback), hand the call over to it."""
        loop = self._loop
        if loop is not None and loop.is_running() and not self._on_loop(loop):
            loop.call_soon_threadsafe(fn)
        else:
            fn()

    @staticmethod
    def _on_loop(loop: asyncio.AbstractEventLoop) -> bool:
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _async_main(self) -> None:
        assert self._mic_read is not None
        self._client = AsyncOpenAI(api_key=OPENAI_API_KEY)
        session_config = self._session_config()
        async with self._client, self._client.live.connect() as connection:
            self._connection = connection
            await connection.session.start(session=session_config, event_id="event_start")
            sender = asyncio.create_task(self._send_audio_loop())
            self._tasks.add(sender)
            try:
                async for event in connection:
                    await self._handle_event(event)
                    if self._sleep_requested and _event_attr(event, "type") == "session.closed":
                        break
                    if self._sleep_requested and not self._closed.is_set():
                        # Request graceful close once; keep reading until session.closed.
                        if not getattr(self, "_close_sent", False):
                            self._close_sent = True
                            try:
                                await connection.session.close()
                            except Exception:
                                logger.exception("live.error session.close failed")
                                break
            finally:
                sender.cancel()
                await asyncio.gather(sender, return_exceptions=True)
                self._tasks.discard(sender)
                self._connection = None

    def _session_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {
            "model": self._live_model,
            "instructions": self._live_instructions,
            "audio": {
                "format": {"type": "audio/pcm", "rate": self._audio_rate},
                "output": {"voice": api_voice(self._voice)},
            },
            "delegation": responses_delegation(
                model=self._backend_model,
                instructions=self._backend_instructions,
                tools=self._backend_tools(),
                parallel_tools=OPENAI_LIVE_PARALLEL_TOOLS,
                effort=OPENAI_LIVE_BACKEND_EFFORT,
            ),
        }
        if self._conversation is not None and (history := self._conversation.live_input()):
            config["input"] = history
        return config

    def _backend_tools(self) -> list[dict]:
        from tools.live_schemas import LIVE_BACKEND_TOOLS

        return LIVE_BACKEND_TOOLS

    async def _send_audio_loop(self) -> None:
        pending = b""
        assert self._mic_read is not None
        while not self._sleep_requested:
            if self._input_muted:
                # Paused: the wake-phrase recogniser has the mic (it may share this stream).
                await asyncio.sleep(0.02)
                continue
            try:
                chunk = await asyncio.to_thread(self._mic_read)
            except Exception as exc:
                logger.error("live.error microphone failure: %s", type(exc).__name__)
                self._error = exc
                self._sleep_requested = True
                break
            if not chunk or self._connection is None:
                await asyncio.sleep(0.01)
                continue

            # Two-stage local barge-in (duck → confirm). Do not wait for full STT.
            if self._local_barge_in:
                self._maybe_local_barge_in(chunk)

            data = pending + chunk
            complete = len(data) - (len(data) % 2)
            pending = data[complete:]
            if complete:
                audio_b64 = base64.b64encode(data[:complete]).decode("ascii")
                try:
                    await self._connection.session.input_audio.append(audio=audio_b64)
                except Exception as exc:
                    if self._sleep_requested:
                        break
                    logger.warning("live.error input_audio.append: %s", type(exc).__name__)
                    await asyncio.sleep(0.05)

    def _speaking_backchannel(self) -> bool:
        return is_backchannel_utterance(self._assistant_burst)

    def _maybe_local_barge_in(self, chunk: bytes) -> None:
        # Her «угу» while the user keeps talking is meant to overlap them: neither duck nor stop it.
        playing = (self.player.is_playing or self._busy_cues.cue_playing) and not self._speaking_backchannel()
        now = time.monotonic()
        self._maybe_release_output_after_gap(now)

        decision = self._barge_gate.feed_mic(
            chunk,
            assistant_or_cue_playing=playing,
            output_rms=self.player.recent_output_rms(),
        )

        if decision.action == BargeInAction.DUCK:
            self._speech_onset_mono = now
            self.player.set_volume(self._duck_volume)
            return

        if decision.action == BargeInAction.REJECT:
            self.player.set_volume(1.0)
            self._speech_onset_mono = None
            return

        if decision.action == BargeInAction.CONFIRM:
            if now - self._last_barge_in_at < self._barge_cooldown_s:
                self.player.set_volume(1.0)
                return
            self._trigger_barge_in(
                source="vad_confirmed",
                speech_ms=decision.speech_ms,
                reason=decision.reason,
            )

    def _on_interjection(self, frag: str) -> None:
        """User speech transcribed while the assistant talks: interrupt only on real intent
        (stop word / taking the turn), never on backchannels, room chatter or echo."""
        gate = self._barge_gate
        recent = self._recent_assistant_text
        if self._speaking_backchannel() and classify_interjection(frag, assistant_recent=recent) != "stop":
            return  # the user talking over her «угу» is the point of it
        if gate.state == BargeInState.POSSIBLE:
            decision = gate.note_partial_transcript(frag, assistant_recent=recent)
            if decision.action == BargeInAction.CONFIRM:
                self._speech_onset_mono = self._speech_onset_mono or time.monotonic()
                self._trigger_barge_in(
                    source="transcript",
                    speech_ms=decision.speech_ms,
                    reason=decision.intent or decision.reason,
                    short_ack=decision.intent == "stop",
                )
            elif decision.action == BargeInAction.REJECT:
                self.player.set_volume(1.0)
                self._speech_onset_mono = None
            return
        if not (self.player.is_playing or self._stale_response):
            return
        # No open candidate (it already ended, e.g. a short «стоп»): judge the words that
        # arrived within the last couple of seconds.
        now = time.monotonic()
        if now - self._idle_interjection_at > _IDLE_INTERJECTION_WINDOW_S:
            self._idle_interjection = ""
        self._idle_interjection_at = now
        self._idle_interjection += frag
        intent = classify_interjection(self._idle_interjection, assistant_recent=recent)
        # A takeover also needs local evidence of near-field speech; room chatter has none.
        if intent == "stop" or (intent == "takeover" and gate.had_recent_candidate()):
            self._idle_interjection = ""
            self._speech_onset_mono = self._speech_onset_mono or now
            forced = gate.force_confirm(source="transcript_keyword")
            if forced.action == BargeInAction.CONFIRM:
                self._trigger_barge_in(
                    source="transcript_keyword",
                    speech_ms=forced.speech_ms,
                    reason=intent,
                    short_ack=intent == "stop",
                )

    def _invalidate_assistant_response(self, *, reason: str) -> None:
        """Stop the current answer now; _maybe_release_output_after_gap decides when audio resumes."""
        self._assistant_generation += 1
        self._play_assistant_audio = False
        self._awaiting_output_gap = True
        self._barge_in_mono = time.monotonic()
        self._stale_dropped_chunks = 0
        self._stale_response = True
        self._last_output_delta_at = time.monotonic()
        self.player.interrupt()
        self.player.set_volume(1.0)
        self._busy_cues.on_user_speech()
        logger.info(
            "RESPONSE invalidated generation=%s reason=%s",
            self._assistant_generation,
            reason,
        )

    def _maybe_release_output_after_gap(self, now: float | None = None) -> None:
        """After a barge-in, let assistant audio play again.

        Live output audio carries no response id, so old and new speech can't be told apart
        directly. Release when the old stream pauses (gap), or — because the model often
        flows straight from the old answer into the reply to the interruption without a
        pause — once the user has stopped talking and a short window has passed. Waiting
        only for a gap could mute the assistant for its whole next answer.
        """
        if not self._awaiting_output_gap or self._play_assistant_audio:
            return
        now = now if now is not None else time.monotonic()
        if self._last_output_delta_at <= 0:
            self._last_output_delta_at = now
            return
        gap_ms = (now - self._last_output_delta_at) * 1000.0
        waited_s = now - self._barge_in_mono if self._barge_in_mono else 0.0
        if gap_ms >= self._output_gap_ms:
            reason = "gap"
        elif waited_s >= _STALE_DROP_MAX_S and not self._vad.speaking:
            reason = "max_wait"
        else:
            return
        self._play_assistant_audio = True
        self._awaiting_output_gap = False
        self._stale_response = False
        logger.info(
            "RESPONSE new_generation generation=%s reason=%s gap_ms=%.0f waited_s=%.1f dropped_chunks=%s",
            self._assistant_generation,
            reason,
            gap_ms,
            waited_s,
            self._stale_dropped_chunks,
        )
        self._stale_dropped_chunks = 0

    def _trigger_barge_in(
        self,
        *,
        source: str,
        speech_ms: float = 0.0,
        reason: str = "",
        short_ack: bool = False,
    ) -> None:
        now = time.monotonic()
        if now - self._last_barge_in_at < self._barge_cooldown_s:
            return
        onset = self._speech_onset_mono or now
        latency_ms = max(0, int((now - onset) * 1000))
        logger.info("BARGE_IN detected source=%s reason=%s", source, reason or source)
        self._invalidate_assistant_response(reason=source)
        self._last_barge_in_at = now
        self._barge_gate.reset()
        # Stop word → one short ack («Добре.»); words → stop and listen; sound only (no words
        # yet) → pause, and resume if it turns out nobody was talking to the assistant.
        self._schedule_steer_stop(short_ack=short_ack, uncertain=source == "vad_confirmed")
        logger.info(
            "BARGE_IN playback_stopped latency_ms=%s barge_in_latency_ms=%s "
            "speech_ms=%.0f source=%s generation=%s candidates=%s confirmed=%s rejected=%s",
            latency_ms,
            latency_ms,
            speech_ms,
            source,
            self._assistant_generation,
            self._barge_gate.candidate_count,
            self._barge_gate.confirmed_count,
            self._barge_gate.rejected_count,
        )
        self._speech_onset_mono = None

    def _schedule_steer_stop(self, *, short_ack: bool = False, uncertain: bool = False) -> None:
        if self._connection is None or self._session_closing:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._steer_stop_speaking(short_ack=short_ack, uncertain=uncertain))
            return
        except RuntimeError:
            pass
        loop = self._loop
        if loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self._steer_stop_speaking(short_ack=short_ack, uncertain=uncertain), loop
            )

    async def _steer_stop_speaking(self, *, short_ack: bool = False, uncertain: bool = False) -> None:
        """Ask Live to stop; local epoch already dropped old audio."""
        if self._connection is None or self._session_closing:
            return
        if short_ack:
            content = (
                "Stop your previous answer immediately. Do not continue or resume it. "
                "Reply with at most one short acknowledgement such as «Добре.» or "
                "«Так, чекаю.» Then wait silently for the user."
            )
        elif uncertain:
            # Only a sound was detected, no words yet: a false alarm must not leave the
            # assistant silent mid-answer.
            content = (
                "Pause — the user may be starting to talk. Listen. If they say something to "
                "you, answer that. If nobody actually spoke to you (noise, a cough, your own "
                "voice echoing), continue your previous answer from where you stopped, without "
                "repeating it from the beginning."
            )
        else:
            content = (
                "Stop speaking immediately. The user is talking. "
                "Do not finish or resume your previous sentence, and do not say that you stop or "
                "listen — just go silent. Listen."
            )
        try:
            await self._connection.session.instructions.append(
                content=content,
                delegation_id=None,
                event_id=f"barge_{uuid.uuid4().hex[:8]}",
            )
            logger.info(
                "BARGE_IN response_cancelled method=instructions.append short_ack=%s "
                "generation=%s",
                short_ack,
                self._assistant_generation,
            )
        except Exception:
            logger.debug("BARGE_IN steer_stop failed", exc_info=True)

    def _output_accepted(self) -> bool:
        """Response-aware gate: stale generation never plays, even after time passes."""
        self._maybe_release_output_after_gap()
        return self._play_assistant_audio

    async def _play_busy_cue(self, phrase: str, pcm: bytes) -> None:
        if not self._output_accepted() or self._vad.speaking or self._stale_response:
            logger.info("BUSY_CUE cancelled reason=user_speech phrase=%r", phrase)
            return
        if self.player.is_playing and self.player.playing_kind == "assistant":
            logger.info("BUSY_CUE skipped reason=assistant_playing")
            return
        self.player.enqueue(pcm, kind="cue")

    async def _handle_event(self, event: Any) -> None:
        etype = _event_attr(event, "type")
        if etype == "session.started":
            sess = _event_attr(event, "session")
            remote_id = _event_attr(sess, "id")
            if remote_id:
                self.session_id = str(remote_id)
            self._started.set()
            return
        if self._paused and etype in (
            "session.output_audio.delta",
            "session.output_transcript.delta",
            "session.input_transcript.delta",
        ):
            return  # whatever she says to «Дякую, Єва» is not played, and the muted mic hears nothing
        if etype == "session.output_audio.delta":
            now = time.monotonic()
            self._last_activity = now
            if not self._output_accepted():
                self._last_output_delta_at = now
                if self._stale_dropped_chunks == 0:
                    logger.info("RESPONSE stale_audio_dropping generation=%s", self._assistant_generation)
                self._stale_dropped_chunks += 1
                return
            # Final/assistant audio must not overlap a thinking cue.
            if self.player.playing_kind == "cue" or self._busy_cues.cue_playing:
                self._busy_cues.on_final_response_starting()
                self.player.interrupt()
            delta = _event_attr(event, "delta") or ""
            try:
                pcm = base64.b64decode(delta)
            except Exception:
                return
            self._last_output_delta_at = now
            self.player.enqueue(pcm, kind="assistant")
            return
        if etype == "session.input_transcript.delta":
            frag = _event_attr(event, "delta") or ""
            if isinstance(frag, str) and frag.strip():
                playing = self.player.is_playing or self._busy_cues.cue_playing or self._stale_response
                if playing or self._barge_gate.state == BargeInState.POSSIBLE:
                    self._on_interjection(frag)
            self._input_buf += frag
            self._note_user_text(frag)
            if self._conversation is not None:
                self._conversation.add("user", frag)
            self._recent_user_text = (self._recent_user_text + frag)[-80:]
            if not self._pause_requested and is_stop(self._recent_user_text):
                # A pause, not the end: the session stays open (muted) for a quick «Єва, скажи».
                self._enter_pause("stop_phrase")
                self._spawn(self._silence_for_pause())
                return
            if self._on_user_transcript:
                try:
                    self._on_user_transcript(frag)
                except Exception:
                    logger.exception("user transcript callback failed")
            return
        if etype == "session.output_transcript.delta":
            # Stale response text must not accumulate into the next turn context.
            if self._stale_response or not self._play_assistant_audio:
                self._last_output_delta_at = time.monotonic()
                return
            frag = _event_attr(event, "delta") or ""
            now = time.monotonic()
            if now - self._assistant_burst_at > _BURST_GAP_S:
                self._assistant_burst = ""
            self._assistant_burst += frag
            self._assistant_burst_at = now
            self._output_buf += frag
            self._note_assistant_text(frag)
            if self._conversation is not None:
                self._conversation.add("assistant", frag)
            self._recent_assistant_text = (self._recent_assistant_text + frag)[-_ECHO_CONTEXT_CHARS:]
            return
        if etype == "session.delegation.created":
            delegation = _event_attr(event, "delegation")
            did = _event_attr(delegation, "id")
            logger.info(
                "live.delegation.created session_id=%s delegation_id=%s target=%s",
                self.session_id,
                did,
                _event_attr(delegation, "target"),
            )
            return
        if etype == "response.event":
            await self._handle_response_envelope(event)
            return
        if etype == "error":
            logger.error(
                "live.error session_id=%s detail=%s",
                self.session_id,
                _event_attr(event, "error") or event,
            )
            return
        if etype == "session.closed":
            self._flush_transcript_buffers()
            self._closed.set()
            return

        # Commit turn boundaries opportunistically when buffers look settled —
        # GPT-Live does not guarantee a single "turn complete" transcript event.
        if etype in ("session.updated",):
            self._flush_transcript_buffers()

    async def _handle_response_envelope(self, outer: Any) -> None:
        delegation_id = _event_attr(outer, "delegation_id")
        inner = _event_attr(outer, "event")
        if inner is None:
            return
        itype = _event_attr(inner, "type")

        response = _event_attr(inner, "response")
        rid = _event_attr(response, "id") or _event_attr(inner, "response_id")
        key = str(delegation_id or rid or "")
        if itype == "response.created":
            if rid and delegation_id:
                self._delegation_ids[str(rid)] = str(delegation_id)
            self._responses.response_started(key)
            self._work_started(key)
            return

        if itype in RESPONSE_FINISHED_TYPES:
            ok = itype == "response.completed"
            if not ok or not self._responses.had_calls(key):
                self._work.pop(key, None)  # the final answer (or a failure): she speaks now
                self._answering.discard(key)
            if self._responses.response_finished(key, ok=ok):
                await self._continue(key, reason="response_finished")
            return

        if itype == "response.output_item.done":
            # Re-wrap so extract_completed_function_call sees the outer envelope.
            completed = extract_completed_function_call(outer)
            if completed is None:
                return
            self._responses.call_started(key, completed.call_id)
            if key in self._work:
                self._work[key].append(completed.name)
                self._answering.discard(key)  # one more step
            logger.info(
                "live.backend.function_call session_id=%s delegation_id=%s call_id=%s tool_name=%s",
                self.session_id,
                completed.delegation_id,
                completed.call_id,
                completed.name,
            )
            self._busy_cues.on_tool_started()
            # Do NOT await on the recv loop — blocking tools must not freeze mic/events.
            task = asyncio.create_task(
                self._execute_and_continue(
                    name=completed.name,
                    arguments=completed.arguments,
                    call_id=completed.call_id,
                    delegation_id=completed.delegation_id,
                    key=key,
                )
            )
            self._tool_tasks.add(task)

            def _done(done_task: asyncio.Task, *, call_id: str = completed.call_id) -> None:
                self._tool_tasks.discard(done_task)
                if done_task.cancelled():
                    return
                exc = done_task.exception()
                if exc is not None:
                    logger.error(
                        "live.tool.completed unexpected error call_id=%s error=%s",
                        call_id,
                        type(exc).__name__,
                        exc_info=exc,
                    )

            task.add_done_callback(_done)
            return

    async def _execute_and_continue(
        self,
        *,
        name: str,
        arguments: Any,
        call_id: str,
        delegation_id: str | None,
        key: str,
    ) -> None:
        if self._connection is None:
            self._busy_cues.on_tool_finished()
            return
        ctx = ToolExecutionContext(
            session_id=self.session_id,
            delegation_id=delegation_id,
            user_utterances=self.recent_user_utterances(),
            assistant_utterances=self.recent_assistant_utterances(),
        )
        result = await self._executor.execute(name, arguments, ctx)
        self._busy_cues.on_tool_finished()
        # Session may have closed while a blocking tool ran in a worker thread.
        if self._session_closing or self._closed.is_set() or self._connection is None:
            logger.info(
                "live.tool.completed discarded session_id=%s call_id=%s tool_name=%s — session closed",
                self.session_id,
                call_id,
                name,
            )
            return
        result_json = result.to_json()
        if name == "end_conversation" and result.ok:
            self._sleep_requested = True
        remember_tool_result(self._conversation, name, result.message)
        if self._paused and result.message:
            # Her answer goes into the muted session: nobody hears it. Told at the wake instead.
            self._unheard.append(result.message)
        logger.info(
            "live.backend.function_result session_id=%s delegation_id=%s call_id=%s "
            "tool_name=%s status=%s",
            self.session_id,
            delegation_id,
            call_id,
            name,
            result.status,
        )

        try:
            await self._connection.response.item.create(
                event_id=f"tool_result_{call_id}",
                item={
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": result_json,
                },
            )
        except Exception:
            if self._session_closing or self._closed.is_set():
                return
            raise
        if self._responses.call_finished(key, call_id):
            await self._continue(key, reason="results_in")
        elif self._responses.results_complete(key):
            task = asyncio.create_task(self._continue_if_stuck(key))
            self._tool_tasks.add(task)
            task.add_done_callback(self._tool_tasks.discard)

    async def _continue_if_stuck(self, key: str) -> None:
        await asyncio.sleep(_CONTINUE_FALLBACK_S)
        if self._responses.results_complete(key):
            await self._continue(key, reason="fallback")

    async def _continue(self, key: str, *, reason: str) -> None:
        """Every call of the delegated response has its result: let the model go on."""
        self._responses.mark_continued(key)
        if key in self._work:
            self._answering.add(key)
        if self._session_closing or self._closed.is_set() or self._connection is None:
            return
        # Cancel any lingering cue before the spoken continuation.
        self._busy_cues.on_final_response_starting()
        if self.player.playing_kind == "cue":
            self.player.interrupt()
        await self._connection.response.create(event_id=f"continue_{uuid.uuid4().hex[:8]}")
        logger.info("live.backend.response_continued session_id=%s key=%s reason=%s", self.session_id, key, reason)

    # ── pause / resume ─────────────────────────────────────────────────────────

    def _enter_pause(self, reason: str) -> None:
        """Local part of a pause: cut her off now and drop whatever she says until resume()."""
        logger.info("live.pause reason=%s session_id=%s", reason, self.session_id)
        self._paused = True
        self._pause_requested = True
        self._input_muted = True
        self._recent_user_text = ""
        self.stop_playback()
        self.player.set_volume(1.0)
        self._barge_gate.reset()
        for task in list(self._progress.values()):
            self._call_on_loop(task.cancel)

    async def _silence_for_pause(self) -> None:
        if self._connection is None or self._session_closing:
            return
        try:
            await self._connection.session.instructions.append(
                content=_PAUSE_STEER, delegation_id=None, event_id=f"pause_{uuid.uuid4().hex[:8]}"
            )
            await self._connection.session.input_audio.mute(event_id=f"mute_{uuid.uuid4().hex[:8]}")
        except Exception:
            logger.debug("live.pause mute failed", exc_info=True)

    async def _unmute(self) -> None:
        if self._connection is None or self._session_closing:
            return
        try:
            await self._connection.session.input_audio.unmute(event_id=f"unmute_{uuid.uuid4().hex[:8]}")
        except Exception:
            logger.debug("live.resume unmute failed", exc_info=True)

    # ── who said what (for the tools) ──────────────────────────────────────────

    def _note_user_text(self, frag: str) -> None:
        now = time.monotonic()
        self._last_input_at = now
        self._last_activity = now
        if self._turn_output.strip():  # the user speaks: her line is over
            self._assistant_turns = (self._assistant_turns + [self._turn_output.strip()])[-5:]
            self._turn_output = ""
        self._turn_input += frag

    def _note_assistant_text(self, frag: str) -> None:
        now = time.monotonic()
        self._last_output_at = now
        self._last_activity = now
        if self._turn_input.strip():  # she started answering: the user's turn is complete
            self._user_turns = (self._user_turns + [self._turn_input.strip()])[-20:]
            self._turn_input = ""
        self._turn_output += frag

    def recent_user_utterances(self) -> list[str] | None:
        current = [self._turn_input.strip()] if self._turn_input.strip() else []
        return (self._user_turns + current) or None

    def recent_assistant_utterances(self) -> list[str] | None:
        current = [self._turn_output.strip()] if self._turn_output.strip() else []
        return (self._assistant_turns + current)[-3:] or None

    # ── settings applied once she is quiet ─────────────────────────────────────

    def _is_quiet(self, quiet_s: float) -> bool:
        return (
            not self._paused
            and not self.player.is_playing
            and time.monotonic() - self._last_output_at >= quiet_s
        )

    async def _queue_quiet_instruction(self, kind: str, text: str) -> None:
        if not text:
            return
        self._quiet_instructions[kind] = text
        if self._quiet_sender is None or self._quiet_sender.done():
            self._quiet_sender = self._spawn(self._send_when_quiet())

    async def _send_when_quiet(self) -> None:
        deadline = time.monotonic() + _INSTRUCTION_MAX_WAIT_S
        # The transcript runs ahead of the audio; the player tells when she has really finished.
        while not self._is_quiet(_INSTRUCTION_QUIET_S) and time.monotonic() < deadline:
            if self._connection is None or self._session_closing:
                return
            await asyncio.sleep(0.2)
        text = "\n\n".join(self._quiet_instructions.values())
        kinds = ",".join(self._quiet_instructions)
        self._quiet_instructions.clear()
        if text:
            logger.info("live.instruction.applied kinds=%s session_id=%s", kinds, self.session_id)
            await self._append_instruction(text)

    # ── «ще роблю» during slow delegated work ──────────────────────────────────

    def _work_started(self, key: str) -> None:
        """A delegated response started; a continuation after tool results is the same work."""
        if key in self._work:
            return
        self._work[key] = []
        try:
            task = self._spawn(self._say_progress(key))
        except RuntimeError:
            return  # no running loop (unit tests feeding events synchronously)
        self._progress[key] = task
        task.add_done_callback(lambda _: self._progress.pop(key, None))

    async def _say_progress(self, key: str) -> None:
        """The work keeps her silent: once, she says she is on it, like a person looking something
        up — silence sounds like the line dropped. Not once the results are in, not after the user
        spoke again (a correction makes the work stale), not while paused."""
        started = time.monotonic()
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if (
                self._connection is None
                or self._session_closing
                or self._paused
                or key not in self._work
                or key in self._answering
                or self._last_input_at > started + _PROGRESS_INPUT_GRACE_S
                or now - started > _PROGRESS_GIVE_UP_S
            ):
                return
            if now - started >= _PROGRESS_AFTER_S and self._is_quiet(_PROGRESS_QUIET_S):
                break
        logger.info(
            "live.work.progress session_id=%s after_s=%.1f tools=%s",
            self.session_id,
            time.monotonic() - started,
            self._work[key],
        )
        await self._connection.session.commentary.append(
            content=work_progress_note(), delegation_id=None, event_id=f"progress_{uuid.uuid4().hex[:8]}"
        )

    async def _append_instruction(self, text: str) -> None:
        if self._connection is None:
            return
        await self._connection.session.instructions.append(
            content=text, delegation_id=None, event_id=f"instr_{uuid.uuid4().hex[:8]}"
        )

    async def _speak_context(self, text: str) -> None:
        if self._connection is None:
            return
        # Explicit local speak — allow a fresh generation after any barge-in.
        self._assistant_generation += 1
        self._play_assistant_audio = True
        self._awaiting_output_gap = False
        self._stale_response = False
        logger.info("RESPONSE new_generation generation=%s reason=speak_context", self._assistant_generation)
        await self._connection.session.commentary.append(
            content=text, delegation_id=None, event_id=f"comment_{uuid.uuid4().hex[:8]}"
        )

    def ask_backend(self, request: str, note: str = "") -> None:
        """A request said with the wake phrase: to the backend as the user's message (see
        woken_request_note); note — what she overheard / missed during the pause, as silent context."""
        self._run_coro(self._ask_backend(request, note))

    async def _ask_backend(self, request: str, note: str) -> None:
        if self._connection is None:
            return
        self._assistant_generation += 1  # her answer is a fresh generation, as after _speak_context
        self._play_assistant_audio = True
        self._awaiting_output_gap = False
        self._stale_response = False
        logger.info("RESPONSE new_generation generation=%s reason=ask_backend", self._assistant_generation)
        thinking = self._connection.session.thinking
        if note.strip():
            await thinking.append(content=note.strip(), delegation_id=None, event_id=f"note_{uuid.uuid4().hex[:8]}")
        await thinking.append(
            content=woken_request_note(request), delegation_id=None, event_id=f"asked_{uuid.uuid4().hex[:8]}"
        )
        await self._connection.response.item.create(
            item={"type": "message", "role": "user", "content": [{"type": "input_text", "text": request}]},
            event_id=f"ask_{uuid.uuid4().hex[:8]}",
        )
        await self._connection.response.create(event_id=f"go_{uuid.uuid4().hex[:8]}")

    async def _shutdown(self) -> None:
        self._session_closing = True
        self._sleep_requested = True
        self._busy_cues.cancel(reason="shutdown")
        for task in list(self._tool_tasks):
            if not task.done():
                task.cancel()
        if self._tool_tasks:
            await asyncio.gather(*list(self._tool_tasks), return_exceptions=True)
            self._tool_tasks.clear()
        if self._connection is not None and not self._close_sent:
            self._close_sent = True
            try:
                await asyncio.wait_for(self._connection.session.close(), timeout=2.0)
            except Exception:
                try:
                    await self._connection.close()
                except Exception:
                    pass

    def _flush_transcript_buffers(self) -> None:
        if self._input_buf.strip():
            self._turns.append({"role": "user", "content": self._input_buf.strip()})
            self._input_buf = ""
        if self._output_buf.strip():
            self._turns.append({"role": "assistant", "content": self._output_buf.strip()})
            self._output_buf = ""
