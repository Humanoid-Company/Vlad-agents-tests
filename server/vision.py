"""Fast eyes for Єва: a small vision model turns camera frames into text.

GPT-Live takes no images. Before, look_at_camera handed the JPEG to the backend model: it read the image
itself, so a look cost the backend a vision pass and the image stayed in its history. Now:

  * look_at_camera — a fast vision model (OPENAI_VISION_MODEL, no reasoning) answers the question about the
    frame and the tool returns that text; the backend only passes it on. The old image path is the
    fallback (and CAMERA_LOOK_MODE=image).
  * the scene line — while a call is open, a frame that changed is described in one or two sentences and
    sent to GPT-Live as quiet context (session.thinking.append): «що ти бачиш?» is answered at once, with
    no backend at all. Lines go only when the view changed, and short: the Live context also holds the
    audio (CAMERA_SCENE_* in config.py).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from config import (
    CAMERA_SCENE_CHANGE,
    CAMERA_SCENE_MAX_S,
    CAMERA_SCENE_MIN_S,
    OPENAI_VISION_EFFORT,
    OPENAI_VISION_MODEL,
    OPENAI_VISION_TIMEOUT_S,
)

logger = logging.getLogger(__name__)

# The model's answer when a frame shows nothing new against the last line (then nothing is sent).
UNCHANGED = "БЕЗ ЗМІН"

_LOOK_PROMPT = (
    "You are the eyes of a voice assistant. You get one camera frame taken just now and the user's question "
    "about it. Answer in the user's language ({language}), only from what the frame shows: two to four short plain sentences for "
    "speaking aloud, concrete — objects, colours, what the person holds or shows, expression; read any text "
    "word for word. If they ask for a full or detailed description («повністю», «детально»), everything "
    "visible, up to eight sentences. Say plainly what is blurry, too dark, cut off or out of frame. Never "
    "invent details and never guess who a person is from their face. No lists, no preamble."
)

_SCENE_PROMPT = (
    "You watch a camera for a voice assistant. Describe this frame in the user's language ({language}) in one or two short "
    "sentences (at most 35 words): who is there and what they do, what they hold, the main objects, the "
    "place. Plain facts only; never guess who a person is. The previous description was: «{previous}». If "
    "nothing worth mentioning changed since it, answer exactly «" + UNCHANGED + "»."
)


class VisionError(RuntimeError):
    """The vision model did not answer (timeout, API error, empty text)."""


@dataclass
class VisionAnswer:
    text: str
    ms: int


class Vision:
    """The vision model on the Responses API (sync client: callers run it in a worker thread)."""

    def __init__(self, client=None, *, model: str = OPENAI_VISION_MODEL, effort: str = OPENAI_VISION_EFFORT,
                 timeout_s: float = OPENAI_VISION_TIMEOUT_S) -> None:
        self._client = client
        self._lock = threading.Lock()
        self.model, self.effort, self.timeout_s = model, effort, timeout_s

    def _api(self):
        with self._lock:  # lazy: one client for the whole server, made on the first look
            if self._client is None:
                from openai import OpenAI

                from config import OPENAI_API_KEY

                self._client = OpenAI(api_key=OPENAI_API_KEY, timeout=self.timeout_s, max_retries=0)
            return self._client

    def _ask(self, instructions: str, text: str, jpeg: bytes, *, detail: str, max_tokens: int) -> VisionAnswer:
        url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
        kwargs: dict = {
            "model": self.model,
            "instructions": instructions,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "image_url": url, "detail": detail},
            ]}],
            "max_output_tokens": max_tokens,
        }
        if self.effort:
            kwargs["reasoning"] = {"effort": self.effort}
        started = time.monotonic()
        try:
            response = self._api().responses.create(**kwargs)
        except Exception as exc:
            raise VisionError(repr(exc)) from exc
        answer = " ".join((getattr(response, "output_text", "") or "").split())
        if not answer:
            raise VisionError("empty answer")
        return VisionAnswer(text=answer, ms=round((time.monotonic() - started) * 1000))

    def look(self, jpeg: bytes, question: str, *, language: str = "Ukrainian") -> VisionAnswer:
        """Answer a question about the frame (look_at_camera)."""
        return self._ask(_LOOK_PROMPT.format(language=language), "Question: " + (question or "що ти бачиш?"),
                         jpeg, detail="high", max_tokens=400)

    def scene(self, jpeg: bytes, previous: str = "", *, language: str = "Ukrainian") -> str | None:
        """The view in a line, or None when nothing changed against `previous`."""
        answer = self._ask(_SCENE_PROMPT.format(language=language, previous=previous or "—"), "The frame:",
                           jpeg, detail="low", max_tokens=120)
        text = answer.text.strip(" «»\"'.")
        if not text or text.upper().startswith(UNCHANGED):
            logger.info("web.camera.scene unchanged ms=%s", answer.ms)
            return None
        logger.info("web.camera.scene ms=%s chars=%s", answer.ms, len(answer.text))
        return answer.text


vision = Vision()


class SceneWatcher:
    """One user's scene line: describes a frame that changed, at most every CAMERA_SCENE_MIN_S, and also
    every CAMERA_SCENE_MAX_S when the page sees no change (light, a slow move). One description at a time."""

    def __init__(self, describe: Callable[[bytes, str], str | None], *, min_s: float = CAMERA_SCENE_MIN_S,
                 max_s: float = CAMERA_SCENE_MAX_S, change: float = CAMERA_SCENE_CHANGE) -> None:
        self._describe = describe
        self.min_s, self.max_s, self.change = min_s, max_s, change
        self.line = ""
        self.line_at = 0.0
        self._started_at = 0.0
        self._busy = False
        self._generation = 0  # reset() bumps it: a description started before is dropped

    def wants(self, change: float | None, now: float) -> bool:
        if self._busy or now - self._started_at < self.min_s:
            return False
        if change is None or not self.line:  # no hint from the page (an edge camera), or no line yet
            return True
        return change >= self.change or now - self._started_at >= self.max_s

    async def offer(self, jpeg: bytes, change: float | None, send: Callable[[str], Awaitable[None]]) -> bool:
        """A new frame: describe it if due and hand a new line to `send`. True when a line was sent."""
        now = time.time()
        if not self.wants(change, now):
            return False
        self._busy, self._started_at = True, now
        previous, generation = self.line, self._generation
        try:
            text = await asyncio.to_thread(self._describe, jpeg, previous)
        except Exception:
            logger.warning("web.camera.scene failed", exc_info=True)
            return False
        finally:
            self._busy = False
        if not text or generation != self._generation:  # nothing new, or reset (camera off, a new call) meanwhile
            return False
        self.line, self.line_at = text, time.time()
        await send(text)
        return True

    def reset(self) -> None:
        """Camera off or a new call: the next frame gets a full description again."""
        self.line, self.line_at, self._started_at = "", 0.0, 0.0
        self._generation += 1
