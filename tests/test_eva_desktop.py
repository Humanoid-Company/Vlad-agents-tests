"""Desktop Єва: «Дякую, Єва» pauses without losing the conversation; wakes carry the request."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from tools.executor import ToolExecutor
from voice.conversation import ConversationLog
from voice.live_session import LiveVoiceSession


def _session(log: ConversationLog, voice: str = "gleam") -> LiveVoiceSession:
    ex = ToolExecutor()
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-eva", voice=voice, conversation=log)
    session.player._stream = MagicMock()  # no PortAudio in tests
    session.player._thread = MagicMock()
    return session


def _feed(session: LiveVoiceSession, *events: tuple[str, str]) -> None:
    async def run():
        for etype, delta in events:
            await session._handle_event({"type": etype, "delta": delta})

    asyncio.run(run())


def test_stop_phrase_pauses_even_while_speaking_and_keeps_history():
    log = ConversationLog()
    session = _session(log)
    _feed(
        session,
        ("session.input_transcript.delta", "Мене звати Остап."),
        ("session.output_transcript.delta", "Приємно, Остапе! Ну що, розкажу про погоду: завтра"),
    )
    session.player.enqueue(b"\x00\x01" * 400)  # Єва is talking
    assert session.player.is_playing
    _feed(session, ("session.input_transcript.delta", "Дякую, "), ("session.input_transcript.delta", "Єво"))
    # A pause keeps the session open (muted) for a quick wake — it is not the end of it.
    assert session.pause_requested and session.paused and not session.sleep_requested
    assert session.player.queued_bytes == 0  # speech cut immediately
    assert [t.text for t in log.turns()][0] == "Мене звати Остап."


def test_next_session_starts_from_the_shared_history():
    log = ConversationLog()
    _feed(
        _session(log),
        ("session.input_transcript.delta", "Я п'ю каву без цукру."),
        ("session.output_transcript.delta", "Запам'ятала!"),
    )
    config = _session(log, voice="meridian")._session_config()  # e.g. after a voice change
    assert config["audio"]["output"]["voice"] == "meridian"
    assert [i["content"][0]["text"] for i in config["input"]] == ["Я п'ю каву без цукру.", "Запам'ятала!"]


def test_wake_phrase_wakes_and_carries_the_request():
    from assistant import Assistant
    from voice.base import State

    bot = Assistant.__new__(Assistant)  # no microphone / Google in tests
    bot.state = State.SLEEPING
    bot._wake_request = ""
    bot.stt = MagicMock()
    bot.stt.listen.return_value = ("Єво, скажи, котра година", None, "")
    bot._handle_sleeping()
    assert bot.state == State.AWAKE and bot._wake_request == "котра година"

    bot.state = State.SLEEPING
    bot.stt.listen.return_value = ("привіт", None, "")
    bot._handle_sleeping()
    assert bot.state == State.SLEEPING


def test_desktop_session_sends_an_own_voice_as_an_id_object(monkeypatch):
    monkeypatch.setenv("OPENAI_VOICE_TARAS", "voice_taras_test")
    assert _session(ConversationLog(), voice="meridian")._session_config()["audio"]["output"]["voice"] == {"id": "voice_taras_test"}
    monkeypatch.delenv("OPENAI_VOICE_TARAS")
    assert _session(ConversationLog(), voice="meridian")._session_config()["audio"]["output"]["voice"] == "meridian"
