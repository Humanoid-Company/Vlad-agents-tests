"""Desktop works like the web page: a pause keeps the session open for a quick wake, silence pauses
her, a dropped line reconnects, settings never freeze the session or derail her mid-answer, slow
tools get a «ще роблю», and the tools see who said what."""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from voice import live_driver, live_session
from voice.conversation import ConversationLog
from voice.live_session import LiveVoiceSession


def _conn() -> SimpleNamespace:
    return SimpleNamespace(
        response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
        session=SimpleNamespace(
            commentary=SimpleNamespace(append=AsyncMock()),
            thinking=SimpleNamespace(append=AsyncMock()),
            instructions=SimpleNamespace(append=AsyncMock()),
            input_audio=SimpleNamespace(mute=AsyncMock(), unmute=AsyncMock(), append=AsyncMock()),
            close=AsyncMock(),
        ),
    )


def _session(executor: ToolExecutor | None = None, log: ConversationLog | None = None) -> LiveVoiceSession:
    session = LiveVoiceSession(tool_executor=executor or ToolExecutor(), session_id="sess-d", conversation=log)
    session.player._stream = MagicMock()  # no PortAudio in tests
    session.player._thread = MagicMock()
    session._connection = _conn()
    return session


def _instructions(session: LiveVoiceSession) -> list[str]:
    return [c.kwargs["content"] for c in session._connection.session.instructions.append.await_args_list]


# ── settings: never block the session loop, wait until she is quiet ──────────


def test_instruction_from_a_tool_handler_does_not_freeze_the_session(monkeypatch):
    """Desktop handlers run on the session loop: waiting there for the loop blocked it 15 s and the
    tool failed («говори повільніше», «тебе звати …», «говори англійською»)."""
    monkeypatch.setattr(live_session, "_INSTRUCTION_QUIET_S", 0.0)
    session = _session()

    def set_style(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        session.append_instruction("Speak slower.", when_quiet="delivery")
        return ToolResult(ok=True, status="ok", message="ok")

    session._executor.register("set_voice_style", set_style)

    async def run():
        session._loop = asyncio.get_running_loop()
        started = time.monotonic()
        result = await session._executor.execute("set_voice_style", {}, ToolExecutionContext())
        assert result.ok and time.monotonic() - started < 1.0
        await asyncio.sleep(0.3)
        assert _instructions(session) == ["Speak slower."]

    asyncio.run(run())


def test_setting_waits_while_she_talks_and_the_newest_wins(monkeypatch):
    monkeypatch.setattr(live_session, "_INSTRUCTION_QUIET_S", 0.0)
    session = _session()

    async def run():
        session._loop = asyncio.get_running_loop()
        session.player.enqueue(b"\x00\x01" * 400)  # she is talking
        session.append_instruction("slow", when_quiet="delivery")
        session.append_instruction("fast", when_quiet="delivery")
        session.append_instruction("English", when_quiet="language")
        await asyncio.sleep(0.5)
        assert _instructions(session) == []  # not mid-answer
        session.player.interrupt()
        await asyncio.sleep(0.5)
        assert _instructions(session) == ["fast\n\nEnglish"]

    asyncio.run(run())


# ── pause keeps the session open ─────────────────────────────────────────────


def _feed(session: LiveVoiceSession, *events: tuple[str, str]) -> None:
    async def run():
        for etype, delta in events:
            await session._handle_event({"type": etype, "delta": delta})

    asyncio.run(run())


def test_pause_mutes_and_drops_what_she_says_until_resume():
    log = ConversationLog()
    session = _session(log=log)
    _feed(session, ("session.input_transcript.delta", "Дякую, Єва"))
    assert session.paused and not session.sleep_requested
    _feed(
        session,
        ("session.output_transcript.delta", "Будь ласка!"),  # her reply to the pause phrase
        ("session.input_transcript.delta", "люди поруч говорять"),
    )
    session.player.enqueue(b"\x00\x01" * 4)
    asyncio.run(session._handle_event({"type": "session.output_audio.delta", "delta": "AAAA"}))
    assert "Будь ласка!" not in [t.text for t in log.turns()]
    assert session._input_muted

    session.resume()  # no loop running in the test: unmute is skipped, flags are what matter
    assert not session.paused and not session.pause_requested and not session._input_muted
    # The rolling window that held «Дякую, Єва» is cleared: the next words don't pause her again.
    _feed(session, ("session.input_transcript.delta", "котра година?"))
    assert not session.paused


def test_pause_tells_the_model_to_stay_silent_and_mutes_its_input():
    session = _session()

    async def run():
        await session._handle_event({"type": "session.input_transcript.delta", "delta": "Дякую, Єво"})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    assert "stay completely silent" in _instructions(session)[0]
    session._connection.session.input_audio.mute.assert_awaited()


# ── the tools see the dialogue ───────────────────────────────────────────────


def test_tools_get_the_users_and_her_recent_lines():
    session = _session()
    _feed(
        session,
        ("session.input_transcript.delta", "Розкажи "),
        ("session.input_transcript.delta", "щось"),
        ("session.output_transcript.delta", "Хочеш, розкажу про космос?"),
        ("session.input_transcript.delta", "Так"),
    )
    assert session.recent_user_utterances() == ["Розкажи щось", "Так"]
    assert session.recent_assistant_utterances() == ["Хочеш, розкажу про космос?"]


def _bot():
    from assistant import Assistant

    bot = Assistant.__new__(Assistant)  # no microphone / Google in tests
    bot._memory = {}
    bot._save_memory = lambda: None
    bot._live = None
    bot._voice_change_pending = False
    return bot


# ── «ще роблю» during slow work ──────────────────────────────────────────────


def _event(inner: dict) -> dict:
    return {"type": "response.event", "delegation_id": "dlg-1", "event": inner}


def test_slow_tool_gets_one_progress_line(monkeypatch):
    monkeypatch.setattr(live_session, "_PROGRESS_AFTER_S", 0.05)
    monkeypatch.setattr(live_session, "_PROGRESS_QUIET_S", 0.0)
    monkeypatch.setattr(live_session, "_PROGRESS_INPUT_GRACE_S", 0.0)
    executor = ToolExecutor({"slow_lookup": lambda args, ctx: ToolResult(ok=True, status="ok", message="ok")})
    session = _session(executor)

    async def run():
        await session._handle_event(_event({"type": "response.created", "response": {"id": "r1"}}))
        await session._handle_event(_event({
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "slow_lookup", "arguments": "{}"},
        }))
        await asyncio.sleep(0.6)

    asyncio.run(run())
    said = [c.kwargs["content"] for c in session._connection.session.commentary.append.await_args_list]
    assert len(said) == 1 and "still running" in said[0]


# ── the driver: idle pause, wake within the pause, dropped line ──────────────


class _FakeLive:
    def __init__(self, **flags) -> None:
        self.sleep_requested = flags.get("sleep", False)
        self.pause_requested = flags.get("pause", False)
        self.closed = flags.get("closed", False)
        self.busy = flags.get("busy", False)
        self.idle = flags.get("idle", 0.0)
        self.player = SimpleNamespace(is_playing=False)
        self.paused_by: str | None = None
        self.resumed = False
        self.said: list[str] = []
        self.asked: list[tuple[str, str]] = []
        self.unheard: list[str] = flags.get("unheard", [])

    def idle_for(self) -> float:
        return self.idle

    def pause(self, *, reason: str) -> None:
        self.paused_by = reason

    def resume(self) -> None:
        self.resumed = True

    def speak_context(self, text: str) -> None:
        self.said.append(text)

    def ask_backend(self, request: str, note: str = "") -> None:
        self.asked.append((request, note))

    def take_unheard(self) -> list[str]:
        results, self.unheard = self.unheard, []
        return results


def _driver_bot():
    from voice.background import BackgroundLog

    bot = _bot()
    bot._running = True
    bot._sleep_requested = False
    bot._paused = False
    bot._wake_request = ""
    bot._wake_phrase = ""
    bot._conversation = ConversationLog()
    bot._background = BackgroundLog()
    bot.stt = MagicMock()
    return bot


def test_silence_pauses_her_unless_a_tool_runs(monkeypatch):
    monkeypatch.setattr(live_driver, "IDLE_PAUSE_SECONDS", 90)
    bot = _driver_bot()
    live = _FakeLive(idle=91)
    assert bot._converse(live) == "pause" and live.paused_by == "idle"
    assert bot._converse(_FakeLive(closed=True)) == "dropped"
    assert bot._converse(_FakeLive(sleep=True, closed=True)) == "end"


def test_wake_within_the_pause_answers_in_the_open_session(monkeypatch):
    monkeypatch.setattr(live_driver, "PAUSE_CLOSE_SECONDS", 30)
    monkeypatch.setattr(live_driver, "BACKGROUND_LISTENING", True)
    bot = _driver_bot()
    bot.stt.listen.side_effect = [
        ("а завтра о сьомій ми їдемо на вокзал", None, ""),  # people nearby
        ("Єво, скажи, котра година", None, ""),
    ]
    live = _FakeLive(pause=True)
    assert bot._wait_in_pause(live)
    bot._wake_in_session(live)
    assert live.resumed and not bot._paused
    request, note = live.asked[-1]  # to the backend, with what she overheard as silent context
    assert request == "котра година" and "вокзал" in note and not live.said
    assert [t.text for t in bot._conversation.turns() if t.role == "user"] == ["Єва, скажи, котра година"]


def test_pause_closes_the_session_after_the_window(monkeypatch):
    monkeypatch.setattr(live_driver, "PAUSE_CLOSE_SECONDS", 0.05)
    bot = _driver_bot()
    bot.stt.listen.side_effect = lambda timeout=None: (time.sleep(0.03), (None, None, ""))[1]
    assert not bot._wait_in_pause(_FakeLive(pause=True))
    assert bot._paused  # background listening goes on while sleeping


def test_dropped_line_reconnects_a_limited_number_of_times():
    bot = _driver_bot()
    assert bot._may_reconnect(2.0) and bot._may_reconnect(2.0)
    assert not bot._may_reconnect(2.0)  # it keeps dropping right away: stop and wait for «Єва, скажи»
    assert bot._may_reconnect(120.0)  # a session that lived a while starts the count over


# ── a pause never loses a tool's answer, and never cancels tasks from the wrong thread ──


def test_tool_result_during_a_pause_is_kept_for_the_wake():
    """Her answer goes into the muted session: nobody hears it, so it is handed over at the wake."""
    session = _session(ToolExecutor({"slow_lookup": lambda a, c: ToolResult(ok=True, status="ok", message="Знайшла три кафе.")}))

    async def run(name: str) -> None:
        await session._execute_and_continue(name=name, arguments={}, call_id=name, delegation_id=None, key=name)

    asyncio.run(run("slow_lookup"))
    assert session.take_unheard() == []  # heard: not paused
    session._enter_pause("stop_phrase")
    asyncio.run(run("slow_lookup"))
    assert session.take_unheard() == ["Знайшла три кафе."] and session.take_unheard() == []


def test_wake_tells_what_came_during_the_pause():
    bot = _driver_bot()
    bot._wake_request = ""
    bot._wake_phrase = "Єва, скажи"
    live = _FakeLive(pause=True, unheard=["Знайшла три кафе."])
    bot._wake_in_session(live)
    assert "Знайшла три кафе." in live.said[-1] and live.said[-1].index("кафе") < live.said[-1].index("Єва, скажи")
    # The pause outlasted the window: the next session's wake says it.
    bot._unheard = ["Таймер спрацював."]
    assert "Таймер спрацював." in bot._unheard_note() and bot._unheard_note() == ""


def test_idle_pause_from_the_driver_thread_cancels_on_the_session_loop():
    import threading

    session = _session()
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        session._loop = loop
        task = asyncio.run_coroutine_threadsafe(_current_task(), loop).result(timeout=2)  # a task on that loop
        session._progress = {"k": task}
        session._enter_pause("idle")  # called from this (the driver's) thread
        deadline = time.monotonic() + 2
        while not task.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert task.cancelled()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)


async def _current_task() -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(asyncio.sleep(30))
    await asyncio.sleep(0)
    return task
