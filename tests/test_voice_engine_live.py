"""Live architecture unit tests (no audio hardware): the tool executor, delegation events, playback."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from voice.delegation import accumulate_transcript, extract_completed_function_call
from voice.playback import PlaybackTracker


def test_tool_executor_routing_and_errors():
    calls = []

    def handler(args, ctx):
        calls.append((args, ctx.session_id))
        return ToolResult(ok=True, status="ok", message="Готово.")

    ex = ToolExecutor()
    ex.register("set_assistant_name", handler)
    ctx = ToolExecutionContext(session_id="sess")
    result = ex.execute_sync("set_assistant_name", {"name": "Ліна"}, ctx)
    assert result.status == "ok" and result.message == "Готово."
    assert calls == [({"name": "Ліна"}, "sess")]

    assert ex.execute_sync("set_assistant_name", '{"name": "Ліна"}', ctx).ok  # JSON string arguments
    bad = ex.execute_sync("set_assistant_name", "{not-json", ctx)
    assert bad.status == "error"

    unknown = ex.execute_sync("totally_unknown_tool", {}, ctx)
    assert unknown.status == "error"
    assert "Невідома" in unknown.message


def test_plain_string_result_is_ok():
    ex = ToolExecutor({"ping": lambda args, ctx: "pong"})
    result = ex.execute_sync("ping", {}, ToolExecutionContext())
    assert result.ok and result.message == "pong"


def test_tool_executor_exception_not_fake_success():
    def boom(args, ctx):
        raise RuntimeError("down")

    ex = ToolExecutor({"boom": boom})
    result = ex.execute_sync("boom", {}, ToolExecutionContext())
    assert result.ok is False
    assert result.status == "error"


def test_extract_function_call_only_on_output_item_done():
    delta = {
        "type": "response.event",
        "delegation_id": "dlg-1",
        "event": {"type": "response.function_call_arguments.delta", "delta": '{"voice":'},
    }
    assert extract_completed_function_call(delta) is None

    done = {
        "type": "response.event",
        "delegation_id": "dlg-1",
        "event": {
            "type": "response.output_item.done",
            "response_id": "resp-1",
            "item": {
                "type": "function_call",
                "call_id": "call-9",
                "name": "change_voice",
                "arguments": '{"voice":"cedar"}',
            },
        },
    }
    completed = extract_completed_function_call(done)
    assert completed is not None
    assert completed.call_id == "call-9"
    assert completed.name == "change_voice"
    assert completed.delegation_id == "dlg-1"
    assert completed.response_id == "resp-1"


def test_transcript_fragments_accumulate():
    buf = ""
    for frag in ("I", " want", " Friday"):
        buf = accumulate_transcript(buf, frag)
    assert buf == "I want Friday"


def test_playback_tracker_enqueue_interrupt_without_hardware():
    tracker = PlaybackTracker(sample_rate=24000)
    # Avoid opening PortAudio in CI — patch start/write loop.
    tracker._stream = MagicMock()
    tracker._thread = threading_dummy()
    tracker.enqueue(b"\x00\x01" * 10)
    assert tracker.queued_bytes > 0
    tracker.interrupt()
    assert tracker.queued_bytes == 0
    tracker.close()


def threading_dummy():
    class T:
        def join(self, timeout=None):
            return None

    return T()


def test_function_result_continues_backend_exactly_once():
    """function_call → ToolExecutor → function_call_output → response.create once."""
    from voice.live_session import LiveVoiceSession

    ex = ToolExecutor({"set_assistant_name": lambda args, ctx: ToolResult(ok=True, status="ok", message="ok")})
    session = LiveVoiceSession(tool_executor=ex, session_id="sess-test")
    conn = SimpleNamespace(
        response=SimpleNamespace(
            item=SimpleNamespace(create=AsyncMock()),
            create=AsyncMock(),
        )
    )
    session._connection = conn

    async def _run():
        await session._execute_and_continue(
            name="set_assistant_name",
            arguments={"name": "Ліна"},
            call_id="call-1",
            delegation_id="dlg-1",
            key="dlg-1",
        )

    asyncio.run(_run())
    conn.response.item.create.assert_awaited_once()
    item = conn.response.item.create.await_args.kwargs["item"]
    assert item["type"] == "function_call_output"
    assert item["call_id"] == "call-1"
    conn.response.create.assert_awaited_once()


def test_live_backend_tools_are_the_session_tools():
    from tools.live_schemas import LIVE_BACKEND_TOOLS

    names = {t["name"] for t in LIVE_BACKEND_TOOLS}
    assert names == {
        "set_assistant_name", "set_voice_style", "change_language", "end_conversation", "web_search",
    }
