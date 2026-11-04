"""Assistant mixin: drives an awake session on GPT-Live + the session tools' handlers."""
from __future__ import annotations

import logging
import threading
import time
import uuid

from config import (
    BACKGROUND_LISTENING,
    IDLE_PAUSE_SECONDS,
    LIVE_PROMPT_VARIANT,
    OPENAI_LIVE_VOICE,
    PAUSE_CLOSE_SECONDS,
)
from prompts.backend_prompt import build_backend_prompt, local_today
from prompts.live_prompt import PROMPT_VARIANTS, build_live_prompt
from tools.executor import ToolExecutionContext
from tools.results import ToolResult
from voice.background import wake_commentary
from voice.base import State
from voice.conversation import ConversationLog
from voice.live_session import LiveVoiceSession
from voice.mic import LiveMicCapture
from voice.options import (
    LANGUAGE_OPTIONS,
    LIVE_VOICE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    VOICE_PERSONAS,
    _sanitize_name,
    delivery_instruction,
)

logger = logging.getLogger(__name__)


# Said right after a wake phrase so the user hears Єва is on (same wording in web/app.js).
WAKE_GREETING = (
    "The user just called you: «{phrase}». You are back and listening — let them hear it. "
    "Reply right away in two to four words that match it: to «привіт», «вітаю», «гей» greet back "
    "warmly («Привіт! Що робимо?», «О, привіт! Слухаю»), otherwise a short «Так, слухаю» or "
    "«Слухаю тебе». Vary it, then wait."
)

# The connection dropped and a new session took over with the same history.
RECONNECTED = (
    "The connection dropped for a moment and you are back with the whole conversation. Say in a few "
    "words that the line dropped and you are listening again («Ой, зв'язок перервався. Я тут.»), then wait."
)

# Tool results that came during a pause: she said them into the muted session and nobody heard.
UNHEARD = (
    "While you were paused, work you had started finished, and the user has not heard the result: "
    "{results}. Tell them briefly first."
)

# A paused session is kept open no longer than this while a tool still runs in it.
_PAUSE_HOLD_MAX_S = 5 * 60
# A dropped session is reopened at most this many times in a row; one that lived this long resets it.
_MAX_RECONNECTS = 2
_STABLE_SESSION_S = 30.0


def _close_session(live: LiveVoiceSession, mic: LiveMicCapture) -> None:
    try:
        live.close()
    finally:
        mic.close()


class LiveDriverMixin:
    # Set up by Assistant.__init__ / the awake-session loop.
    _live: LiveVoiceSession | None
    _running: bool
    _history: list[dict]
    _history_cutoff: int | None
    _pending_cutoff: int
    _conversation: ConversationLog
    _wake_request: str
    _wake_phrase: str

    def _run_awake_session_live(self) -> None:
        """GPT-Live path: full duplex + Responses delegation. No manual turn create."""
        self._sleep_requested = False
        self._voice_change_pending = False
        self._history_cutoff = None
        self._pending_cutoff = len(self._history)
        self._session_id = str(uuid.uuid4())
        voice = self._memory.get("realtime_voice") or self._memory.get("live_voice") or OPENAI_LIVE_VOICE
        mic = LiveMicCapture(self.stt.read_chunk)
        mic.open()
        live = LiveVoiceSession(
            tool_executor=self._tool_executor,
            voice=voice,
            session_id=self._session_id,
            conversation=self._conversation,
        )
        # A new voice or a reconnect after a dropped line: no greeting, she carries on with the same memory.
        voice_restart = getattr(self, "_voice_restarted", False)
        reconnect = getattr(self, "_reconnecting", False)
        self._voice_restarted = False
        self._reconnecting = False
        silent_start = voice_restart or reconnect
        wake_request, self._wake_request = self._wake_request, ""
        if wake_request and not silent_start:
            # Their request starts the new session's history as their own message: quoted in a
            # commentary, GPT-Live sometimes read it back instead of answering.
            self._conversation.add("user", f"Єва, скажи, {wake_request}")
            self._conversation.end_turn()
        language_name = LANGUAGE_OPTIONS.get(self._memory.get("language", "uk"), LANGUAGE_OPTIONS["uk"])
        self._live = live
        outcome = "end"
        started_at = time.monotonic()
        try:
            live.connect(
                build_live_prompt(
                    language_name=language_name,
                    assistant_name=self._memory.get("assistant_name"),
                    today=local_today(),
                    voice=voice,
                    delivery=delivery_instruction(
                        self._memory.get("speed", "normal"), self._memory.get("style", "normal")
                    ),
                    variant=LIVE_PROMPT_VARIANT if LIVE_PROMPT_VARIANT in PROMPT_VARIANTS else "v4",
                ),
                mic_read_chunk=mic.read_chunk,
                mic_drain=mic.drain,
                backend_instructions=build_backend_prompt(today=local_today(), language_name=language_name),
            )
            # What she overheard during the pause goes in the same commentary as the wake reply (silent
            # context next to a request: see LiveVoiceSession.ask_backend).
            heard = "" if silent_start else self._take_background_note() + self._unheard_note()
            if voice_restart:
                pass  # a new voice: no announcement, she just listens on with the same memory
            elif reconnect:
                live.speak_context(RECONNECTED)
            elif wake_request:
                live.ask_backend(wake_request, heard)
            elif self._wake_phrase:
                live.speak_context(heard + WAKE_GREETING.format(phrase=self._wake_phrase))
            else:
                live.speak_context(heard + "Слухаю!")
            while True:
                outcome = self._converse(live)
                if outcome != "pause":
                    break
                # «Дякую, Єва» or silence: the session stays open (muted) for a while, so a wake
                # within it is answered at once — like the web page.
                if not self._wait_in_pause(live):
                    break
                self._wake_in_session(live)
            logger.info("[latency] Live awake session duration: %.1fs", time.monotonic() - started_at)
            live.stop_playback()
            if outcome == "pause":
                self._paused = True
                logger.info("Live pause outlasted %ss — session closed; «Єва, скажи» reopens it.", int(PAUSE_CLOSE_SECONDS))
            elif outcome == "dropped":
                logger.warning("Live session dropped after %.1fs.", time.monotonic() - started_at)
            elif not live.voice_restart_requested and not self._voice_change_pending:
                live.speak_context("До побачення!")
                time.sleep(1.0)
        except Exception:
            logger.error("Live awake session crashed; closing before sleep.", exc_info=True)
            raise
        finally:
            turns = live.get_turns()
            self._history = turns[: self._history_cutoff] if self._history_cutoff is not None else turns
            self._live = None
            # Results nobody heard (the pause outlasted the window): told at the next wake.
            self._unheard = getattr(self, "_unheard", []) + live.take_unheard()
            voice_change = live.voice_restart_requested or self._voice_change_pending
            if voice_change:
                # The old session closes in the background: the new voice starts seconds sooner.
                threading.Thread(target=_close_session, args=(live, mic), daemon=True, name="live-close").start()
            else:
                _close_session(live, mic)
            if voice_change:
                # Live: restart a new awake session with the new voice — do NOT kill the process.
                logger.info("Live voice change — restarting voice session without process exit.")
                self._voice_change_pending = False
                self._voice_restarted = True
                self.state = State.AWAKE
            elif outcome == "dropped" and self._may_reconnect(time.monotonic() - started_at):
                logger.info("Reconnecting to GPT-Live with the same conversation.")
                self._reconnecting = True
                self.state = State.AWAKE
            else:
                if outcome == "dropped":
                    self._paused = True  # nothing lost: «Єва, скажи» opens a new session with the history
                self.state = State.SLEEPING
                time.sleep(1.5)

    # ── awake session lifecycle ──────────────────────────────────────────────

    def _converse(self, live: LiveVoiceSession) -> str:
        """Until the conversation stops: "end" (goodbye, voice change, app exit), "pause" («Дякую,
        Єва» or nobody spoke for IDLE_PAUSE_SECONDS) or "dropped" (the connection is gone)."""
        while self._running:
            if live.sleep_requested or self._sleep_requested:
                return "end"
            if live.pause_requested:
                return "pause"
            if live.closed:
                return "dropped"
            if (
                IDLE_PAUSE_SECONDS > 0
                and live.idle_for() > IDLE_PAUSE_SECONDS
                and not live.busy
                and not live.player.is_playing
            ):
                logger.info("Тиша %ss — пауза, щоб не витрачати хвилини GPT-Live.", int(IDLE_PAUSE_SECONDS))
                live.pause(reason="idle")
                return "pause"
            # Live owns turn-taking; we only poll lifecycle flags.
            time.sleep(0.05)
        return "end"

    def _wait_in_pause(self, live: LiveVoiceSession) -> bool:
        """Paused with the session open: True once the wake phrase is heard, False after
        PAUSE_CLOSE_SECONDS (longer while a tool still runs) or when the session is gone."""
        self._paused = True
        paused_at = time.monotonic()
        deadline = paused_at + PAUSE_CLOSE_SECONDS
        while self._running and not live.closed and not live.sleep_requested:
            now = time.monotonic()
            if now >= deadline:
                if not (live.busy and now - paused_at < _PAUSE_HOLD_MAX_S):
                    return False
                deadline = now + 5.0  # a tool still runs in this session: keep it a little longer
            text, _, _ = self.stt.listen(timeout=max(0.5, deadline - now))
            if self._take_wake(text):
                return True
        return False

    def _wake_in_session(self, live: LiveVoiceSession) -> None:
        """«Єва, скажи» heard while the paused session is still open: unmute and answer at once."""
        wake_request, self._wake_request = self._wake_request, ""
        heard = self._take_background_note() + self._unheard_note(live)
        live.resume()
        if wake_request:
            self._conversation.end_turn()
            self._conversation.add("user", f"Єва, скажи, {wake_request}")
            self._conversation.end_turn()
            live.ask_backend(wake_request, heard)
        else:
            live.speak_context(heard + WAKE_GREETING.format(phrase=self._wake_phrase or "Єва, скажи"))

    def _take_background_note(self) -> str:
        """What she overheard during the pause, as the start of the wake commentary ("" if nothing).
        Ends the pause."""
        note = self._background.digest() if getattr(self, "_paused", False) and BACKGROUND_LISTENING else ""
        self._paused = False
        if not note:
            return ""
        self._conversation.add_note(note)
        logger.info("background.digest chars=%s", len(note))
        return wake_commentary(note) + "\n\n"

    def _unheard_note(self, live: LiveVoiceSession | None = None) -> str:
        """Tool results nobody heard (they came during a pause), for the wake commentary ("" if none)."""
        results = list(getattr(self, "_unheard", []))
        if live is not None:
            results += live.take_unheard()
        self._unheard = []
        if not results:
            return ""
        return UNHEARD.format(results=" ".join(f"«{r}»" for r in results)) + "\n\n"

    def _may_reconnect(self, lived_s: float) -> bool:
        attempts = 0 if lived_s >= _STABLE_SESSION_S else getattr(self, "_reconnect_attempts", 0)
        if attempts >= _MAX_RECONNECTS or not self._running:
            self._reconnect_attempts = 0
            return False
        self._reconnect_attempts = attempts + 1
        return True

    def _live_set_name(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        new_name = _sanitize_name(args.get("name", ""))
        if not new_name:
            return ToolResult(ok=False, status="error", message="Не зрозумів нового імені.")
        msg = self._set_assistant_name(new_name)
        return ToolResult(ok=True, status="ok", message=msg)

    def _live_set_voice_style(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        """Speed/style of the current voice — instructions, applied from the next sentence."""
        del context
        speed = str(args.get("speed") or self._memory.get("speed", "normal")).strip().lower()
        style = str(args.get("style") or self._memory.get("style", "normal")).strip().lower()
        if speed not in SPEED_OPTIONS or style not in STYLE_OPTIONS:
            return ToolResult(ok=False, status="error", message="Темп: slow/normal/fast, стиль: calm/normal/expressive.")
        self._memory["speed"], self._memory["style"] = speed, style
        self._save_memory()
        if self._live is not None:
            self._live.append_instruction(delivery_instruction(speed, style, changed=True), when_quiet="delivery")
        return ToolResult(ok=True, status="ok", message="Готово, говорю так з наступного речення.")

    def _live_change_language(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del context
        language = str(args.get("language", "")).strip().lower()
        if language not in LANGUAGE_OPTIONS:
            return ToolResult(
                ok=False,
                status="error",
                message=f"Мова {language!r} не підтримується — скажи користувачу спробувати ще раз.",
            )
        self._memory["language"] = language
        self._save_memory()
        lang_name = LANGUAGE_OPTIONS[language]
        if self._live is not None:
            self._live.append_instruction(f"From now on speak exclusively in {lang_name}.", when_quiet="language")
        return ToolResult(
            ok=True,
            status="ok",
            message=f"Мову змінено на {lang_name}. Нічого про це не кажи: просто говори далі цією мовою, а "
            "якщо вже почала відповідати — продовжуй, не починай заново.",
        )

    def _live_end_conversation(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        del args, context
        self._sleep_requested = True
        self._history_cutoff = self._pending_cutoff
        if self._live is not None:
            self._live.request_sleep()
        return ToolResult(ok=True, status="ok", message="Розмову завершено.")
