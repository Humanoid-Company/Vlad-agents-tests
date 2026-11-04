"""
Main assistant state machine (desktop app).

States
------
SLEEPING – Idle; only listens for the wake phrase (cheap Google STT).
AWAKE    – One GPT-Live voice session handles the conversation.

Conversation memory
--------------------
Turns are carried over in-memory between sleep/wake cycles within the same
process run — say «Дякую, Єва» and «Єва, скажи» again and the assistant still
remembers. Restarting the script clears it (nothing is persisted to disk).

Voice commands (handled as tool calls, not regex)
----------------------------------------------------------------
"тебе звати …"             – change the assistant's own name
"зміни голос …"            – another voice; "говори повільніше" – speed/manner
"до побачення" / "бувай" / … — model calls end_conversation to go back to sleep
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from config import BACKGROUND_LISTENING, WEB_SEARCH_MAX_CALLS_PER_TURN
from integrations.web_search import WebSearchRateLimiter
from speech_to_text import SpeechToText
from text_to_speech import TextToSpeech
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from tools.web_search_tool import web_search_tool_result
from voice.background import BackgroundLog, make_openai_summarizer
from voice.base import State
from voice.conversation import ConversationLog
from voice.live_driver import LiveDriverMixin
from voice.live_session import LiveVoiceSession
from voice.wake_phrases import match_wake

_MEMORY_FILE = Path(__file__).parent / "assistant_memory.json"

logger = logging.getLogger(__name__)


class Assistant(LiveDriverMixin):
    """Voice assistant state machine — orchestration/lifecycle only."""

    def __init__(self) -> None:
        self.stt = SpeechToText()
        self.tts = TextToSpeech()
        self._live: LiveVoiceSession | None = None
        self.state = State.SLEEPING
        self._running = False
        self._sleep_requested = False
        self._voice_change_pending = False
        self._voice_restarted = False
        # The connection dropped: the next session continues the conversation (voice/live_driver.py).
        self._reconnecting = False
        self._reconnect_attempts = 0
        # Tool results that came during a pause and nobody heard: told at the next wake.
        self._unheard: list[str] = []

        # Conversation history for the current run only — carries over between
        # sleep/wake cycles (in-memory), but resets when the process restarts.
        self._history: list[dict] = []
        # The same history, independent of the voice session — a voice change or a wake
        # after «Дякую, Єва» starts a new Live session that is seeded with it.
        self._conversation = ConversationLog()
        self._wake_request: str = ""
        self._wake_phrase: str = ""
        # Paused by «Дякую, Єва» (not ended): what is said nearby is kept and handed to her on wake.
        self._paused = False
        self._background = BackgroundLog(self._summarize_background)

        # Long-term settings (name, voice, language, speed/style) — persist between runs.
        self._memory: dict = self._load_memory()
        self._web_search_limiter = WebSearchRateLimiter(max_per_turn=WEB_SEARCH_MAX_CALLS_PER_TURN)
        self._tool_executor = self._build_tool_executor()

    def _build_tool_executor(self) -> ToolExecutor:
        """The session tools (tools/live_schemas.py). A new tool: describe it there, register it here
        (run_in_thread=True for blocking network / file work)."""
        executor = ToolExecutor()
        executor.register("set_assistant_name", self._live_set_name)
        executor.register("set_voice_style", self._live_set_voice_style)
        executor.register("change_language", self._live_change_language)
        executor.register("end_conversation", self._live_end_conversation)
        executor.register("web_search", self._web_search, run_in_thread=True)
        return executor

    def _web_search(self, args: dict, context: ToolExecutionContext) -> ToolResult:
        return web_search_tool_result(
            args, session_id=context.session_id, delegation_id=context.delegation_id, limiter=self._web_search_limiter
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Assistant started.")
        self.tts.speak("Єва готова. Покличте її: «Єва, скажи» або «Привіт, Єва».")

        while self._running:
            try:
                if self.state == State.SLEEPING:
                    self._handle_sleeping()
                elif self.state == State.AWAKE:
                    self._run_awake_session_live()
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                logger.error("Unhandled error in main loop: %s", exc, exc_info=True)
                self.state = State.SLEEPING

        self._cleanup()

    def stop(self) -> None:
        self._running = False
        if self._live is not None:
            self._live.close()

    # ── State handlers ────────────────────────────────────────────────────────

    def _handle_sleeping(self) -> None:
        text, _, _ = self.stt.listen()
        if self._take_wake(text):
            logger.info("Wake phrase heard → AWAKE")
            self.state = State.AWAKE

    def _take_wake(self, text: str | None) -> bool:
        """A wake phrase in the recognised text → True (its request kept for the model). Anything
        else said during a pause is kept as background talk."""
        rest = match_wake(text) if text else None
        if rest is None:
            if text and BACKGROUND_LISTENING and getattr(self, "_paused", False):
                self._background.add(text)  # the same STT that waits for «Єва, скажи» — no extra cost
            return False
        self._wake_request = rest  # «Єва, скажи, котра година» → the question goes to the model
        self._wake_phrase = text or ""
        return True

    def _summarize_background(self, text: str) -> str:
        from openai import OpenAI

        from config import OPENAI_API_KEY, OPENAI_LIVE_BACKEND_MODEL

        return make_openai_summarizer(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)(text)

    # ── Long-term memory ──────────────────────────────────────────────────────

    def _load_memory(self) -> dict:
        if _MEMORY_FILE.exists():
            try:
                return json.loads(_MEMORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_memory(self) -> None:
        _MEMORY_FILE.write_text(json.dumps(self._memory, ensure_ascii=False, indent=2), encoding="utf-8")

    def _set_assistant_name(self, name: str) -> str:
        self._memory["assistant_name"] = name
        self._save_memory()
        if self._live is not None:
            self._live.append_instruction(
                f"Your name is now {name}. Introduce yourself with that name.", when_quiet="name"
            )
        return f"Ім'я асистента змінено на {name}."

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _cleanup(self) -> None:
        if self._live is not None:
            self._live.close()
        self.tts.cleanup()
        self.stt.close()
        logger.info("Assistant shut down cleanly.")

