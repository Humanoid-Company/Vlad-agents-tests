"""Server side of a browser Live session.

Audio flows browser ↔ OpenAI over WebRTC. This bridge attaches to the same session over a
sideband WebSocket and does what LiveVoiceSession does for the desktop app on its primary
connection: run the delegated function calls (the session tools: voice, name, language…)
and hand the results back so the model can continue. No audio, playback or local barge-in
here — the browser's echo cancellation and the Live model handle that.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from openai import AsyncOpenAI

from prompts.live_prompt import search_mumble_note, thinking_mumble_note
from server.call_log import CallLog
from tools.executor import ToolExecutionContext, ToolExecutor
from voice.conversation import ConversationLog, remember_tool_result
from voice.delegation import RESPONSE_FINISHED_TYPES, DelegatedResponseTracker, extract_completed_function_call

logger = logging.getLogger(__name__)

# All results are in but the response's finish event hasn't come: continue anyway after this.
_CONTINUE_FALLBACK_S = 2.0
# A request ends when the transcript has been quiet this long («зміни голос на …» + «чоловічий»).
_UTTERANCE_PAUSE_S = 0.9
# The page acts on the same request first (/api/voice-request) and swaps calls without a gap; this
# path only catches what it missed (an old cached page), so it waits that much longer.
_PAGE_FIRST_S = 1.5
# While delegated work runs she thinks aloud, the whole time («так, прикидаю…», «дивлюсь курс…», «ще
# гортаю…») — the team: a silent wait after her «Секунду» sounds like the line dropped, and a search with
# its summary was 6–10 s of silence. Any work still running _MUMBLE_THINK_FIRST_S after it started gets a
# line (a quick answer comes before that); a web search one _MUMBLE_SEARCH_FIRST_S after the search call,
# about what she is looking up. Then (a bare «угу…» / «хм…» and a few half-said words in turn, see
# prompts/live_prompt.py) one every _MUMBLE_EVERY_S until the answer is ready (the work is
# over), at most _MUMBLE_MAX; each only once she has been quiet _MUMBLE_QUIET_S (her estimated audio,
# not just her text), and none once the user spoke again (the work is stale). It replaces the single
# «still working» line after 8 s (fewer fillers was the old aim; now she keeps talking while she works).
_MUMBLE_THINK_FIRST_S = 3.0
_MUMBLE_SEARCH_FIRST_S = 1.5
_MUMBLE_EVERY_S = 2.5
_MUMBLE_MAX = 12
# «Консьєрж» while a check runs: one neutral line, nothing about who or what.
_NEUTRAL_WAIT_NOTE = (
    "Note, not a message from the user: the check is still running. Say one short, light line, not one you "
    "already said — «Ще секундочку…», «Майже-майже…», «Список у нас солідний — ще мить.» — no name, nothing "
    "about the person or what you found, not a question. Nothing else."
)
_MUMBLE_QUIET_S = 1.0
# The user's own request may still be transcribed this long after the work started.
_MUMBLE_INPUT_GRACE_S = 1.5
_MUMBLE_TOOLS = frozenset({"web_search"})
# Her transcript runs well ahead of her audio: a long answer (a full camera description) is all text
# within a second or two and then plays for many more, so «no new text for 4 s» came mid-sentence —
# prod: «…сидить у чорному кріслі. ДивитьсяДивлюсь.», the progress line cutting her off. The page
# hears her audio; here its end is estimated from the text at a speaking pace (a bit slow, so the
# guess errs on «still talking»).
_SPEECH_CHARS_PER_S = 14.0


def _search_query(arguments: Any) -> str:
    try:
        parsed = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
        return str(parsed.get("query") or "")[:120]
    except (ValueError, AttributeError):
        return ""


# Notes appended to her context: the page's (barge-in steers, greetings, wake notes) show up only as
# these events — their text is not echoed back — the server's own are logged where they are sent.
_APPENDED = {
    "session.instructions.appended": "instructions",
    "session.commentary.appended": "commentary",
    "session.thinking.appended": "thinking",
}
_OWN_EVENT_PREFIXES = ("comment_", "instr_", "tool_result_", "continue_")


def _parsed_args(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError:
            return arguments
    return arguments


def _brief_data(data: Any) -> Any:
    """A tool's structured data for /logs, without bulky parts (search hits keep title, url, snippet)."""
    if not isinstance(data, dict):
        return None
    out = {k: v for k, v in data.items() if k != "results" and not isinstance(v, (bytes, bytearray))}
    hits = data.get("results")
    if isinstance(hits, list):
        out["results"] = [
            {k: h.get(k) for k in ("title", "url", "snippet") if isinstance(h, dict)} for h in hits[:8]
        ]
    return out


def _attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class SidebandToolBridge:
    def __init__(
        self,
        *,
        client: AsyncOpenAI,
        session_id: str,
        executor: ToolExecutor,
        on_closed: Callable[[SidebandToolBridge], None] | None = None,
        max_duration_s: float | None = None,
        conversation: ConversationLog | None = None,
        call_log: CallLog | None = None,
        think_aloud: str = "full",
    ) -> None:
        self._client = client
        self.expired = False  # closed by the cost cap (max_duration_s), not by the page
        # Short lines while the backend works: "full" (thinking aloud about the request), "neutral" — one bare
        # «Ще секундочку…» («Консьєрж»: «Так, Тарас у нас є, зараз звірю…» told a stranger the name was on the
        # list before any check, yet with nothing at all a slow check sounded like she had hung), or "off".
        self._think_aloud = think_aloud
        # /logs: what was said and done in this call (server/call_log.py).
        self.call_log = call_log
        self.session_id = session_id
        self._executor = executor
        self._on_closed = on_closed
        self._max_duration_s = max_duration_s
        self._connection: Any = None
        self._closed = asyncio.Event()
        self._tool_tasks: set[asyncio.Task] = set()
        self._responses = DelegatedResponseTracker()
        self._user_turns: list[str] = []
        self._input_buf = ""
        # Her last lines, for a time she offered and the user took («Так»).
        self._assistant_turns: list[str] = []
        self._output_buf = ""
        self.end_requested = False
        self._conversation = conversation
        # Set when the voice changes: close the session right away (no spoken confirmation) so the page
        # reconnects in the new voice — Live voices are fixed per session — and carries on.
        self.restart_for_voice = False
        # instructions.append lands mid-answer as an interruption (it is how a barge-in stops her),
        # so setting changes wait until she has finished speaking.
        self._last_output_at = 0.0
        self._speech_ends_at = 0.0  # loop time her audio is expected to end (_SPEECH_CHARS_PER_S)
        self._last_input_at = 0.0
        self._pending_instruction: asyncio.Task | None = None
        # Delegated work in progress: key → the tools it has called so far, and the task that says
        # «шукаю…» while it runs.
        self._work: dict[str, list[str]] = {}
        # Wall time each piece of work started: a camera frame the page snapped since then is «now».
        self._work_started_at: dict[str, float] = {}
        self._mumbles: dict[str, asyncio.Task] = {}  # work in progress: she thinks aloud (_mumble)
        self._searching: dict[str, tuple[float, str]] = {}  # work that searches: when, and the query
        # Work whose results are in: the backend is writing the answer, she is about to speak it.
        self._answering: set[str] = set()

    async def run(self) -> None:
        expiry: asyncio.Task | None = None
        try:
            async with self._client.live.sideband.connect(session_id=self.session_id) as connection:
                self._connection = connection
                logger.info("web.sideband.connected session_id=%s", self.session_id)
                if self._max_duration_s:
                    expiry = asyncio.create_task(self._expire_after(self._max_duration_s))
                async for event in connection:
                    await self._handle_event(event)
                    if _attr(event, "type") == "session.closed":
                        break
        except Exception:
            logger.exception("web.sideband.failed session_id=%s", self.session_id)
        finally:
            self._closed.set()
            self._connection = None
            if expiry is not None:
                expiry.cancel()
            for task in [*self._tool_tasks, *self._mumbles.values()]:
                task.cancel()
            logger.info("web.sideband.closed session_id=%s", self.session_id)
            self._log("closed")
            if self.call_log is not None:
                self.call_log.end()
            if self._on_closed:
                self._on_closed(self)

    def _log(self, kind: str, **data: Any) -> None:
        if self.call_log is not None:
            self.call_log.add(kind, **data)

    @property
    def busy(self) -> bool:
        """A tool is still running for this call (the page must not close it yet)."""
        return any(not task.done() for task in self._tool_tasks)

    @property
    def is_open(self) -> bool:
        return self._connection is not None and not self._closed.is_set()

    async def say(self, text: str) -> None:
        """Have the assistant tell the user something (a note she says in her own words)."""
        if not self.is_open:
            return
        self._log("note", via="commentary", text=text)
        await self._connection.session.commentary.append(
            content=text, delegation_id=None, event_id=f"comment_{uuid.uuid4().hex[:8]}"
        )

    async def append_instruction(self, text: str, *, quiet_s: float = 3.0, max_wait_s: float = 120.0) -> None:
        """New speed/style asked by voice (set_voice_style), sent once she is quiet — an instruction
        that arrives mid-answer derails it. A newer setting replaces one still waiting. (The page
        does this itself for the settings panel: it hears her actual audio.)"""
        if not self.is_open or not text:
            return
        if self._pending_instruction is not None:
            self._pending_instruction.cancel()
        self._pending_instruction = asyncio.create_task(self._append_when_quiet(text, quiet_s, max_wait_s))

    async def _append_when_quiet(self, text: str, quiet_s: float, max_wait_s: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max_wait_s
        while self.is_open and self._quiet_for(loop.time()) < quiet_s and loop.time() < deadline:
            await asyncio.sleep(0.2)
        if not self.is_open:
            return
        self._pending_instruction = None
        logger.info("web.voice.delivery_applied session_id=%s", self.session_id)
        self._log("note", via="instructions", text=text)
        await self._connection.session.instructions.append(
            content=text, delegation_id=None, event_id=f"instr_{uuid.uuid4().hex[:8]}"
        )

    def _quiet_for(self, now: float) -> float:
        """How long she has been silent: since her last text, or since its audio is expected to end."""
        return now - max(self._last_output_at, self._speech_ends_at)

    async def _expire_after(self, seconds: float) -> None:
        """Cost cap: a call nobody ended (tab left open, browser crashed) is closed server-side."""
        await asyncio.sleep(seconds)
        logger.info("web.session.expired session_id=%s after_s=%s", self.session_id, int(seconds))
        self.expired = True  # the page carries on in a new session if the user is still talking (/api/me)
        await self.close()

    async def close(self) -> None:
        if self.is_open:
            try:
                await self._connection.session.close()
            except Exception:
                logger.debug("web.sideband close failed", exc_info=True)

    async def _handle_event(self, event: Any) -> None:
        etype = _attr(event, "type")
        if etype == "session.input_transcript.delta":
            delta = _attr(event, "delta") or ""
            self._log("user", text=delta)
            self._input_buf += delta
            self._last_input_at = asyncio.get_running_loop().time()
            if self._output_buf.strip():  # the user speaks: her line is over
                self._assistant_turns = (self._assistant_turns + [self._output_buf.strip()])[-5:]
                self._output_buf = ""
            if self._conversation is not None:
                self._conversation.add("user", delta)
            return
        if etype == "session.output_transcript.delta":
            delta = _attr(event, "delta") or ""
            self._log("eva", text=delta)
            self._output_buf += delta
            now = asyncio.get_running_loop().time()
            self._last_output_at = now
            self._speech_ends_at = max(self._speech_ends_at, now) + len(delta) / _SPEECH_CHARS_PER_S
            if self._conversation is not None:
                self._conversation.add("assistant", delta)
            if self._input_buf.strip():
                # The model started answering → the user's turn is complete.
                self._user_turns = (self._user_turns + [self._input_buf.strip()])[-20:]
                self._input_buf = ""
            return
        if etype in _APPENDED:
            by = str(_attr(event, "client_event_id") or "")
            if etype == "session.instructions.appended":
                logger.info("web.instructions.appended session_id=%s by=%s", self.session_id, by)
            if not by.startswith(_OWN_EVENT_PREFIXES):  # the page's: barge-in steers, greetings, wake notes
                self._log("page_note", via=_APPENDED[etype], id=by)
            return
        if etype == "session.delegation.created":
            delegation = _attr(event, "delegation")
            self._log("delegation", target=_attr(delegation, "target"), id=_attr(delegation, "id"))
            return
        if etype == "error":
            logger.error("web.live.error session_id=%s detail=%s", self.session_id, _attr(event, "error") or event)
            self._log("error", source="live", detail=str(_attr(event, "error") or event))
            return
        if etype != "response.event":
            return
        inner = _attr(event, "event")
        itype = _attr(inner, "type")
        response = _attr(inner, "response")
        key = str(_attr(event, "delegation_id") or _attr(response, "id") or _attr(inner, "response_id") or "")
        if itype == "response.created":
            self._log("backend_start", key=key[-8:])
            self._responses.response_started(key)
            self._work_started(key)
            return
        if itype in RESPONSE_FINISHED_TYPES:
            ok = itype == "response.completed"
            usage = _attr(response, "usage")
            self._log(
                "backend_done", key=key[-8:], status=itype.split(".")[-1],
                tokens_in=_attr(usage, "input_tokens"), tokens_out=_attr(usage, "output_tokens"),
                error=str(_attr(response, "error") or "") or None,
            )
            if not ok or not self._responses.had_calls(key):
                self._work.pop(key, None)  # the final answer (or a failure): she speaks now
                self._work_started_at.pop(key, None)
                self._answering.discard(key)
            if self._responses.response_finished(key, ok=ok):
                await self._continue(key, reason="response_finished")
            return
        if itype != "response.output_item.done":
            return
        item = _attr(inner, "item")
        if _attr(item, "type") == "message":  # the backend's written answer, which she then says
            text = "".join(str(_attr(c, "text") or "") for c in (_attr(item, "content") or []))
            self._log("backend", key=key[-8:], text=text)
            return
        completed = extract_completed_function_call(event)
        if completed is None:
            return
        self._responses.call_started(key, completed.call_id)
        self._log("tool_call", key=key[-8:], name=completed.name, call_id=completed.call_id,
                  args=_parsed_args(completed.arguments))
        if key in self._work:
            self._work[key].append(completed.name)
            self._answering.discard(key)  # one more step
            if completed.name in _MUMBLE_TOOLS and key not in self._searching:
                self._searching[key] = (asyncio.get_running_loop().time(), _search_query(completed.arguments))
        logger.info(
            "web.tool.call session_id=%s call_id=%s tool_name=%s",
            self.session_id,
            completed.call_id,
            completed.name,
        )
        # Never block the event loop on a tool: slow handlers run in worker threads.
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
        task.add_done_callback(self._tool_tasks.discard)

    def _work_started(self, key: str) -> None:
        """A delegated response started; a continuation after tool results is the same work."""
        if key in self._work:
            return
        self._work[key] = []
        self._work_started_at[key] = time.time()
        if self._think_aloud != "off":
            self._mumbles[key] = asyncio.create_task(self._mumble(key))

    async def _mumble(self, key: str) -> None:
        """Delegated work runs: short lines thinking aloud until its answer is ready."""
        loop = asyncio.get_running_loop()
        started = loop.time()
        next_at = started + _MUMBLE_THINK_FIRST_S
        said = 0
        try:
            while said < _MUMBLE_MAX:
                await asyncio.sleep(0.25)
                now = loop.time()
                search = self._searching.get(key)
                # Their speech after the work began (or after a search started) makes it stale.
                stale_after = started + _MUMBLE_INPUT_GRACE_S
                if search is not None:
                    stale_after = min(stale_after, search[0])
                if not self.is_open or key not in self._work or self._last_input_at > stale_after:
                    return
                if said == 0 and search is not None:
                    next_at = min(next_at, search[0] + _MUMBLE_SEARCH_FIRST_S)
                if now >= next_at and self._quiet_for(now) >= _MUMBLE_QUIET_S:
                    if self._think_aloud == "neutral":
                        if said:
                            return  # one line is enough
                        note = _NEUTRAL_WAIT_NOTE
                    else:
                        note = search_mumble_note(search[1], said) if search is not None else thinking_mumble_note(said)
                    logger.info(
                        "web.work.mumble session_id=%s n=%s after_s=%.1f search=%s",
                        self.session_id, said + 1, now - started, search is not None,
                    )
                    await self.say(note)
                    said += 1
                    next_at = now + _MUMBLE_EVERY_S
        finally:
            self._mumbles.pop(key, None)
            self._searching.pop(key, None)

    async def _execute_and_continue(
        self,
        *,
        name: str,
        arguments: Any,
        call_id: str,
        delegation_id: str | None,
        key: str,
    ) -> None:
        ctx = ToolExecutionContext(
            session_id=self.session_id,
            delegation_id=delegation_id,
            extras={"work_started_at": self._work_started_at[key]} if key in self._work_started_at else {},
            user_utterances=list(self._user_turns) + ([self._input_buf.strip()] if self._input_buf.strip() else [])
            or None,
            assistant_utterances=(self._assistant_turns + [self._output_buf.strip()] if self._output_buf.strip()
                                  else list(self._assistant_turns))[-3:] or None,
        )
        started = time.monotonic()
        try:
            result = await self._executor.execute(name, arguments, ctx)
        except Exception as exc:
            logger.exception("web.tool.failed tool_name=%s", name)
            self._log("tool_result", name=name, call_id=call_id, ok=False, status="exception",
                      message=repr(exc), ms=round((time.monotonic() - started) * 1000))
            return
        self._log(
            "tool_result", name=name, call_id=call_id, ok=result.ok, status=result.status, message=result.message,
            ms=round((time.monotonic() - started) * 1000), image=bool(getattr(result, "image_jpeg", None)),
            data=_brief_data(result.data),
        )
        if not self.is_open:
            return
        if name == "end_conversation" and result.ok:
            self.end_requested = True
        remember_tool_result(self._conversation, name, result.message)
        if self.restart_for_voice:
            # Don't let this session say anything more: the next one, in the new voice, carries on.
            self.restart_for_voice = False
            logger.info("web.voice.restart session_id=%s", self.session_id)
            await self.close()
            return
        logger.info(
            "web.tool.result session_id=%s call_id=%s tool_name=%s status=%s",
            self.session_id,
            call_id,
            name,
            result.status,
        )
        await self._connection.response.item.create(
            event_id=f"tool_result_{call_id}",
            item={"type": "function_call_output", "call_id": call_id, "output": result.to_output()},
        )
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
        if self.is_open:
            logger.info("web.response.continue session_id=%s reason=%s", self.session_id, reason)
            await self._connection.response.create(event_id=f"continue_{uuid.uuid4().hex[:8]}")
