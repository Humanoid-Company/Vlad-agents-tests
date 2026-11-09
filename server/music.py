"""«Музика» beyond the uploaded songs: naming what plays nearby (AudD) and writing a new song (Lyria, Gemini API).

Recognition: the page records about ten seconds of raw sound (no echo cancelling or noise suppression — they wipe
music out) and posts it; AudD names the track and adds Apple Music / Spotify / Deezer links and a 30 s preview. The
same upload serves the panel's «Слухати» button and Єва's recognize_song tool, which waits for the page's clip the way
look_at_camera waits for a frame.

Generation: a song takes a minute or two, so it never holds up the call — generate_song starts a job and returns at
once; the finished song goes into the shared songs (server/songs.py) and Єва is told it is ready.
"""
from __future__ import annotations

import asyncio
import base64
import difflib
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from server import songs
from server.db import StoreError

logger = logging.getLogger(__name__)

# ── Recognition (AudD, https://docs.audd.io) ──
AUDD_API_TOKEN = os.getenv("AUDD_API_TOKEN", "").strip()
AUDD_URL = os.getenv("AUDD_URL", "https://api.audd.io/")
AUDD_TIMEOUT_S = float(os.getenv("AUDD_TIMEOUT_S", "15"))
# Each provider block adds a little latency; these three give the links, the cover and a preview.
AUDD_RETURN = "apple_music,spotify,deezer"
# Ten seconds of Opus is ~40 KB, of WAV ~1 MB: anything far bigger is not a clip from our page.
MAX_CLIP_BYTES = 3 * 1024 * 1024

# ── Generation (Lyria via the Gemini API «interactions» endpoint) ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# lyria-3.5: full songs of a couple of minutes, vocals and lyrics; lyria-3-clip-preview: always 30 s, faster.
LYRIA_MODEL = os.getenv("LYRIA_MODEL", "lyria-3.5").strip()
LYRIA_URL = os.getenv("LYRIA_URL", "https://generativelanguage.googleapis.com/v1beta/interactions")
LYRIA_TIMEOUT_S = float(os.getenv("LYRIA_TIMEOUT_S", "300"))
MAX_PROMPT_CHARS = 1500
MAX_LYRICS_CHARS = 4000
MAX_TITLE_CHARS = 80
# A song costs money and minutes: one at a time per browser, a few at once on the server.
MAX_JOBS_PER_CLIENT = 1
MAX_JOBS_RUNNING = 3
JOBS_KEPT = 40

_EXT = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
        "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac"}


class MusicError(Exception):
    """A failure to tell the user (message is Ukrainian, ready to show or say)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def status() -> dict[str, Any]:
    """What the panel can offer: without a key its button says why instead of failing later."""
    return {"recognize": bool(AUDD_API_TOKEN), "generate": bool(GEMINI_API_KEY), "model": LYRIA_MODEL}


# ── recognition ──
def recognize(audio: bytes, mime: str) -> dict[str, Any]:
    """Name the music in a clip. {"found": False} when AudD hears nothing it knows."""
    if not AUDD_API_TOKEN:
        raise MusicError("not_configured", "Розпізнавання музики не налаштоване (немає AUDD_API_TOKEN на сервері).")
    mime = (mime or "audio/webm").split(";")[0].strip().lower()
    started = time.monotonic()
    try:
        response = httpx.post(
            AUDD_URL,
            data={"api_token": AUDD_API_TOKEN, "return": AUDD_RETURN},
            files={"file": (f"clip.{_EXT.get(mime, 'webm')}", audio, mime)},
            timeout=AUDD_TIMEOUT_S,
        )
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("music.recognize.unavailable detail=%s", exc)
        raise MusicError("unavailable", "Сервіс розпізнавання зараз не відповідає.") from exc
    if body.get("status") != "success":
        error = body.get("error") or {}
        logger.warning("music.recognize.error code=%s message=%s", error.get("error_code"), error.get("error_message"))
        raise MusicError("error", "Сервіс розпізнавання повернув помилку: " + str(error.get("error_message") or "невідома"))
    result = parse_audd(body.get("result"))
    logger.info("music.recognize found=%s ms=%s bytes=%s", result["found"], int((time.monotonic() - started) * 1000), len(audio))
    return result


def parse_audd(result: Any) -> dict[str, Any]:
    """AudD's result → what the panel shows and Єва says: names, links, a cover and a preview (any of them may be missing)."""
    if not isinstance(result, dict) or not (result.get("title") or result.get("artist")):
        return {"found": False}
    apple = result.get("apple_music") if isinstance(result.get("apple_music"), dict) else {}
    spotify = result.get("spotify") if isinstance(result.get("spotify"), dict) else {}
    deezer = result.get("deezer") if isinstance(result.get("deezer"), dict) else {}
    song_link = str(result.get("song_link") or "")
    links: dict[str, str] = {}
    spotify_url = (spotify.get("external_urls") or {}).get("spotify") if isinstance(spotify.get("external_urls"), dict) else None
    if spotify_url:
        links["spotify"] = str(spotify_url)
    if apple.get("url"):
        links["apple_music"] = str(apple["url"])
    if deezer.get("link"):
        links["deezer"] = str(deezer["link"])
    # lis.tn redirects «?youtube» to the track on YouTube (AudD has no YouTube block of its own).
    if urlparse(song_link).hostname == "lis.tn":
        links["youtube"] = song_link + ("&" if "?" in song_link else "?") + "youtube"
    preview = ""
    previews = apple.get("previews")
    if isinstance(previews, list) and previews and isinstance(previews[0], dict):
        preview = str(previews[0].get("url") or "")
    preview = preview or str(deezer.get("preview") or "")
    cover = ""
    artwork = apple.get("artwork")
    if isinstance(artwork, dict) and artwork.get("url"):
        cover = str(artwork["url"]).replace("{w}", "300").replace("{h}", "300")
    if not cover:
        images = (spotify.get("album") or {}).get("images") if isinstance(spotify.get("album"), dict) else None
        if isinstance(images, list) and images and isinstance(images[0], dict):
            cover = str(images[0].get("url") or "")
    if not cover and isinstance(deezer.get("album"), dict):
        cover = str(deezer["album"].get("cover_medium") or "")
    return {
        "found": True,
        "artist": str(result.get("artist") or ""),
        "title": str(result.get("title") or ""),
        "album": str(result.get("album") or ""),
        "release_date": str(result.get("release_date") or ""),
        "label": str(result.get("label") or ""),
        "song_link": song_link,
        "links": links,
        "preview_url": preview,
        "cover_url": cover,
    }


def describe(found: dict[str, Any]) -> str:
    """«Назва» — Виконавець (альбом, рік) for a sentence."""
    text = f"«{found.get('title') or 'без назви'}» — {found.get('artist') or 'невідомий виконавець'}"
    extra = [x for x in (found.get("album"), (found.get("release_date") or "")[:4]) if x]
    return text + (f" ({', '.join(extra)})" if extra else "")


# ── generation ──
def lyria_input(prompt: str, lyrics: str = "", instrumental: bool = False) -> str:
    """One text for Lyria: the musical description, then the lyrics apart from it (section tags kept)."""
    parts = [prompt.strip()[:MAX_PROMPT_CHARS]]
    if instrumental:
        parts.append("Instrumental only, no vocals.")
    elif lyrics.strip():
        parts.append("Lyrics:\n" + lyrics.strip()[:MAX_LYRICS_CHARS])
    return "\n\n".join(p for p in parts if p)


def _find_blocks(node: Any, kind: str) -> list[dict]:
    """Every content block of a type anywhere in the response (steps → model_output → content today)."""
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == kind and ("data" in node or "text" in node):
            found.append(node)
        for value in node.values():
            found.extend(_find_blocks(value, kind))
    elif isinstance(node, list):
        for value in node:
            found.extend(_find_blocks(value, kind))
    return found


def parse_lyria(body: Any) -> tuple[str, bytes, str]:
    """(mime, audio bytes, lyrics/structure text) from an interactions response."""
    audio = next((b for b in _find_blocks(body, "audio") if b.get("data")), None)
    if audio is None:
        raise MusicError("no_audio", "Модель не повернула аудіо — спробуйте інший опис.")
    try:
        data = base64.b64decode(audio["data"])
    except (ValueError, TypeError) as exc:
        raise MusicError("no_audio", "Модель повернула пошкоджене аудіо.") from exc
    mime = str(audio.get("mime_type") or audio.get("mimeType") or "audio/mpeg").split(";")[0]
    text = "\n".join(str(b.get("text") or "") for b in _find_blocks(body, "text")).strip()
    return mime, data, text


async def generate(prompt: str, lyrics: str = "", instrumental: bool = False) -> tuple[str, bytes, str]:
    if not GEMINI_API_KEY:
        raise MusicError("not_configured", "Генерація музики не налаштована (немає GEMINI_API_KEY на сервері).")
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=LYRIA_TIMEOUT_S) as http:
            response = await http.post(
                LYRIA_URL,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={"model": LYRIA_MODEL, "input": lyria_input(prompt, lyrics, instrumental)},
            )
    except httpx.TimeoutException as exc:
        raise MusicError("timeout", "Пісня писалася задовго — спробуйте ще раз або коротший опис.") from exc
    except httpx.HTTPError as exc:
        logger.warning("music.generate.unavailable detail=%s", exc)
        raise MusicError("unavailable", "Сервіс генерації зараз не відповідає.") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400:
        detail = str((body.get("error") or {}).get("message") or response.status_code) if isinstance(body, dict) else str(response.status_code)
        logger.warning("music.generate.error status=%s detail=%s", response.status_code, detail)
        # Safety filters refuse real artists' voices and existing lyrics: say it plainly.
        raise MusicError("refused" if response.status_code == 400 else "error", "Модель не взялася за цю пісню: " + detail[:200])
    mime, data, text = parse_lyria(body)
    logger.info("music.generate ok model=%s ms=%s bytes=%s", LYRIA_MODEL, int((time.monotonic() - started) * 1000), len(data))
    return mime, data, text


def song_title(title: str, prompt: str) -> str:
    title = re.sub(r"\s+", " ", title or "").strip()[:MAX_TITLE_CHARS]
    if title:
        return title
    words = re.sub(r"\s+", " ", prompt or "").strip().split(" ")[:6]
    return ("Нова пісня: " + " ".join(words)).strip(": ")[:MAX_TITLE_CHARS] or "Нова пісня"


def save_song(name: str, mime: str, data: bytes) -> dict[str, Any]:
    """Into the shared songs, with the same limits as an upload."""
    if len(data) > songs.MAX_SONG_BYTES:
        raise MusicError("too_big", "Пісня вийшла завеликою для «Музики».")
    try:
        if songs.store.count() >= songs.MAX_SONGS:
            raise MusicError("full", f"У «Музиці» вже {songs.MAX_SONGS} пісень — видаліть зайві, і я збережу нову.")
        return songs.store.add(name, mime, data)
    except StoreError as exc:
        raise MusicError("store", "Не вдалося зберегти пісню.") from exc


@dataclass
class MusicJob:
    id: str
    client_id: str
    title: str
    prompt: str
    via: str  # "voice" (generate_song) | "button" (the panel)
    status: str = "running"  # running | done | failed
    song_id: str = ""
    song_name: str = ""
    error: str = ""
    lyrics: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "song_id": self.song_id,
                "song_name": self.song_name, "error": self.error, "via": self.via,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "elapsed_s": round((self.finished_at or time.time()) - self.started_at)}


class JobBoard:
    """Songs being written, in memory (a restart forgets them; finished songs are in the database)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, MusicJob] = {}

    def start(self, client_id: str, *, title: str, prompt: str, via: str) -> MusicJob:
        with self._lock:
            running = [j for j in self._jobs.values() if j.status == "running"]
            if sum(1 for j in running if j.client_id == client_id) >= MAX_JOBS_PER_CLIENT:
                raise MusicError("busy", "Одна пісня вже пишеться — дочекайтеся її, тоді наступна.")
            if len(running) >= MAX_JOBS_RUNNING:
                raise MusicError("busy", "Зараз пишеться забагато пісень одночасно — спробуйте за хвилину.")
            job = MusicJob(id=uuid.uuid4().hex, client_id=client_id, title=title, prompt=prompt, via=via)
            self._jobs[job.id] = job
            for old in list(self._jobs)[:-JOBS_KEPT]:
                if self._jobs[old].status != "running":
                    del self._jobs[old]
            return job

    def finish(self, job: MusicJob, song: dict[str, Any], lyrics: str = "") -> None:
        with self._lock:
            job.status, job.song_id, job.song_name, job.lyrics = "done", song["id"], song["name"], lyrics
            job.finished_at = time.time()

    def fail(self, job: MusicJob, error: str) -> None:
        with self._lock:
            job.status, job.error, job.finished_at = "failed", error, time.time()

    def for_client(self, client_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            mine = [j for j in self._jobs.values() if j.client_id == client_id]
        return [j.public() for j in sorted(mine, key=lambda j: j.started_at, reverse=True)[:limit]]


jobs = JobBoard()


async def run_job(job: MusicJob, prompt: str, lyrics: str, instrumental: bool,
                  notify: Callable[[MusicJob], Awaitable[None]] | None = None) -> None:
    """Write the song, keep it in «Музика», then tell whoever asked (notify gets the finished or failed job)."""
    try:
        mime, data, text = await generate(prompt, lyrics, instrumental)
        song = await asyncio.to_thread(save_song, job.title, mime, data)
    except MusicError as exc:
        jobs.fail(job, exc.message)
    except Exception:  # never let a background job die silently
        logger.exception("music.job.failed id=%s", job.id)
        jobs.fail(job, "Не вдалося написати пісню.")
    else:
        jobs.finish(job, song, text)
    if notify is not None:
        try:
            await notify(job)
        except Exception:
            logger.debug("music.job.notify failed", exc_info=True)


# ── «увімкни …»: which shared song was meant ──
def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'")
    return re.sub(r"[^\w' ]+", " ", text).strip()


def match_song(query: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The song whose name fits what was said (a part of it, other word order, a near spelling)."""
    q = _norm(query)
    if not q or not items:
        return None
    q_words = set(q.split())
    best, best_score = None, 0.0
    for item in items:
        name = _norm(str(item.get("name") or ""))
        if not name:
            continue
        if name == q:
            return item
        words = set(name.split())
        overlap = len(q_words & words) / max(1, len(q_words))
        score = max(overlap, difflib.SequenceMatcher(None, q, name).ratio())
        if q in name or name in q:
            score = max(score, 0.9)
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 0.6 else None
