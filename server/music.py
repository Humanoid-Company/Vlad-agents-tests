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
import json
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
# Ten seconds of Opus is ~40 KB, of the page's 16 kHz WAV ~320 KB: anything far bigger is not a clip from our page.
MAX_CLIP_BYTES = 3 * 1024 * 1024
# The page measures its clip (RMS of the raw samples, 0–1): below this it is silence (a mic giving zeros, nothing
# playing) — said so at once instead of AudD's «problem with creating an audio fingerprint».
QUIET_LEVEL = 0.0015

# ── Generation (Lyria via the Gemini API «interactions» endpoint) ──
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# lyria-3.5: full songs of a couple of minutes, vocals and lyrics; lyria-3-clip-preview: always 30 s, faster.
LYRIA_MODEL = os.getenv("LYRIA_MODEL", "lyria-3.5").strip()
LYRIA_URL = os.getenv("LYRIA_URL", "https://generativelanguage.googleapis.com/v1beta/interactions")
LYRIA_TIMEOUT_S = float(os.getenv("LYRIA_TIMEOUT_S", "300"))
# Lyria through OpenRouter (one key for every model, a free variant too).
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_URL = os.getenv("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")
OPENROUTER_AUDIO_FORMAT = os.getenv("OPENROUTER_AUDIO_FORMAT", "mp3").strip().lower()
_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac", "opus": "audio/ogg", "pcm16": "audio/wav"}
# The order songs are tried in: «openrouter:<model>» or «gemini:<model>», comma-separated. When one runs out (the free
# daily limit, no credits) the next one writes the song. Only those whose key is set count. The paid OpenRouter Pro is
# $0.08 a song and works only with credits on the account; the Clip (google/lyria-3-clip-preview) is $0.04, 30 s.
MUSIC_MODELS = os.getenv(
    "MUSIC_MODELS",
    f"openrouter:google/lyria-3-pro-preview:free,openrouter:google/lyria-3-pro-preview,gemini:{LYRIA_MODEL}",
)
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

    def __init__(self, code: str, message: str, *, daily: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.daily = daily  # a «per day» limit: the model is skipped until midnight UTC


def status() -> dict[str, Any]:
    """What the panel can offer: without a key its button says why instead of failing later."""
    chain = [short_label(kind, model) for kind, model in providers()]
    return {"recognize": bool(AUDD_API_TOKEN), "generate": bool(chain), "models": chain}


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
        text = str(error.get("error_message") or "")
        logger.warning("music.recognize.error code=%s mime=%s bytes=%s message=%s", error.get("error_code"), mime,
                       len(audio), text[:200])
        if "fingerprint" in text.lower():  # it could not read the clip: empty, silent or a container it doesn't take
            raise MusicError("bad_clip", "Не вдалося обробити запис — у ньому майже нічого не чути. Увімкніть музику "
                             "голосніше або піднесіть телефон ближче й спробуйте ще раз.")
        raise MusicError("error", "Сервіс розпізнавання повернув помилку: " + (text[:200] or "невідома"))
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


# ── which model writes the song: a chain, the next one when one runs out (MUSIC_MODELS) ──
def providers() -> list[tuple[str, str]]:
    """(kind, model) in the order they are tried; only those whose key is set."""
    chain: list[tuple[str, str]] = []
    for item in MUSIC_MODELS.split(","):
        kind, _, model = item.strip().partition(":")
        kind, model = kind.strip().lower(), model.strip()
        if kind == "openrouter" and OPENROUTER_API_KEY and model:
            chain.append(("openrouter", model))
        elif kind == "gemini" and GEMINI_API_KEY:
            chain.append(("gemini", model or LYRIA_MODEL))
    return chain


def short_label(kind: str, model: str) -> str:
    return model.split("/")[-1] + (" (OpenRouter)" if kind == "openrouter" else " (Gemini)")


# A model that has run out is skipped for a while, so a song never waits for its refusal first: a daily limit until
# the next midnight UTC, a per-minute one for a minute, no credits for a while. Tried again last if all else fails.
_cooldown: dict[str, float] = {}
_cooldown_lock = threading.Lock()
_COOLDOWN_S = {"quota": 60.0, "no_credits": 600.0, "unavailable": 30.0, "timeout": 30.0, "auth": 600.0, "no_model": 3600.0}


def _cool(label: str, exc: MusicError) -> None:
    now = time.time()
    if exc.code == "quota" and exc.daily:
        until = (int(now // 86400) + 1) * 86400  # the next midnight UTC: free daily limits reset then
    elif exc.code in _COOLDOWN_S:
        until = now + _COOLDOWN_S[exc.code]
    else:
        return
    with _cooldown_lock:
        _cooldown[label] = until


def _cooling(label: str) -> bool:
    with _cooldown_lock:
        return _cooldown.get(label, 0.0) > time.time()


def _http_error(status: int, raw: bytes | str, who: str) -> MusicError:
    """An HTTP error of a provider → a MusicError the chain understands (quota / no credits / refused …)."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    detail = text
    try:
        body = json.loads(text)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            detail = str(error.get("message") or text)
            raw_meta = (error.get("metadata") or {}).get("raw") if isinstance(error.get("metadata"), dict) else None
            if raw_meta:
                detail += " " + str(raw_meta)
    except ValueError:
        pass
    detail = re.sub(r"\s+", " ", detail).strip()[:300]
    low = detail.lower()
    if status == 429 or "rate limit" in low or "quota" in low:
        return MusicError("quota", f"{who}: ліміт вичерпано.", daily="per day" in low or "per-day" in low or "daily" in low)
    if status == 402:
        return MusicError("no_credits", f"{who}: немає кредитів для платної моделі.")
    if status in (401, 403):
        return MusicError("auth", f"{who}: ключ не підходить.")
    if status == 404:
        return MusicError("no_model", f"{who}: модель недоступна.")
    if status == 400:
        # Safety filters refuse real artists' voices and existing lyrics: say it plainly.
        return MusicError("refused", "Модель не взялася за цю пісню: " + detail[:200])
    return MusicError("unavailable", f"{who}: сервіс не відповідає ({status}).")


def sniff_mime(data: bytes, fallback: str = "audio/mpeg") -> str:
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "audio/mpeg"
    if data[:4] == b"RIFF":
        return "audio/wav"
    if data[:4] == b"fLaC":
        return "audio/flac"
    if data[:4] == b"OggS":
        return "audio/ogg"
    if data[4:8] == b"ftyp":
        return "audio/mp4"
    return fallback


def _b64_join(chunks: list[str]) -> bytes:
    """Streamed base64 pieces: each padded on its own, or one string cut anywhere."""
    try:
        if all(len(c) % 4 == 0 for c in chunks):
            return b"".join(base64.b64decode(c) for c in chunks)
        return base64.b64decode("".join(chunks))
    except (ValueError, TypeError) as exc:
        raise MusicError("no_audio", "Модель повернула пошкоджене аудіо.") from exc


def parse_openrouter_json(body: Any) -> tuple[str, bytes, str]:
    """A non-streamed chat completion: message.audio.data, an audio content part, or a data:audio URL."""
    message = {}
    if isinstance(body, dict) and body.get("choices"):
        message = (body["choices"][0] or {}).get("message") or {}
    audio = message.get("audio") if isinstance(message.get("audio"), dict) else {}
    b64 = audio.get("data")
    if not b64:
        block = next((b for b in _find_blocks(body, "audio") if b.get("data")), None)
        b64 = block.get("data") if block else None
    if not b64:
        found = re.search(r"data:audio/[\w.+-]+;base64,([A-Za-z0-9+/=]+)", json.dumps(body))
        b64 = found.group(1) if found else None
    if not b64:
        raise MusicError("no_audio", "Модель не повернула аудіо.")
    data = _b64_join([b64])
    content = message.get("content")
    text = content if isinstance(content, str) else str(audio.get("transcript") or "")
    return sniff_mime(data), data, text.strip()


async def _openrouter(model: str, text_in: str) -> tuple[str, bytes, str]:
    """Lyria through OpenRouter: chat completions with audio output, which comes streamed (delta.audio.data)."""
    who = short_label("openrouter", model)
    body = {"model": model, "messages": [{"role": "user", "content": text_in}], "modalities": ["text", "audio"],
            "audio": {"format": OPENROUTER_AUDIO_FORMAT}, "stream": True}
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json",
               "X-Title": "Eva voice agent"}
    chunks: list[str] = []
    texts: list[str] = []
    try:
        async with (
            httpx.AsyncClient(timeout=httpx.Timeout(LYRIA_TIMEOUT_S, connect=15.0)) as http,
            http.stream("POST", OPENROUTER_URL, headers=headers, json=body) as response,
        ):
            if response.status_code >= 400:
                raise _http_error(response.status_code, await response.aread(), who)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raw = await response.aread()
                try:
                    return parse_openrouter_json(json.loads(raw))
                except ValueError as exc:
                    raise MusicError("no_audio", f"{who}: незрозуміла відповідь.") from exc
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if isinstance(event.get("error"), dict):  # a failure mid-stream (a limit hit by the provider)
                    code = event["error"].get("code")
                    raise _http_error(code if isinstance(code, int) else 500, json.dumps(event), who)
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    audio = delta.get("audio") if isinstance(delta.get("audio"), dict) else {}
                    if audio.get("data"):
                        chunks.append(str(audio["data"]))
                    if audio.get("transcript"):
                        texts.append(str(audio["transcript"]))
                    if isinstance(delta.get("content"), str):
                        texts.append(delta["content"])
    except httpx.TimeoutException as exc:
        raise MusicError("timeout", f"{who}: пісня писалася задовго.") from exc
    except httpx.HTTPError as exc:
        logger.warning("music.generate.unavailable who=%s detail=%s", who, exc)
        raise MusicError("unavailable", f"{who}: сервіс не відповідає.") from exc
    if not chunks:
        raise MusicError("no_audio", f"{who}: модель не повернула аудіо.")
    data = _b64_join(chunks)
    return sniff_mime(data, _MIME.get(OPENROUTER_AUDIO_FORMAT, "audio/mpeg")), data, "".join(texts).strip()


async def _gemini(model: str, text_in: str) -> tuple[str, bytes, str]:
    """Lyria straight from the Gemini API («interactions»)."""
    who = short_label("gemini", model)
    try:
        async with httpx.AsyncClient(timeout=LYRIA_TIMEOUT_S) as http:
            response = await http.post(
                LYRIA_URL,
                headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                json={"model": model, "input": text_in},
            )
    except httpx.TimeoutException as exc:
        raise MusicError("timeout", f"{who}: пісня писалася задовго.") from exc
    except httpx.HTTPError as exc:
        logger.warning("music.generate.unavailable who=%s detail=%s", who, exc)
        raise MusicError("unavailable", f"{who}: сервіс не відповідає.") from exc
    if response.status_code >= 400:
        raise _http_error(response.status_code, response.content, who)
    try:
        body = response.json()
    except ValueError as exc:
        raise MusicError("no_audio", f"{who}: незрозуміла відповідь.") from exc
    return parse_lyria(body)


_REASONS = {"quota": "ліміт вичерпано", "no_credits": "немає кредитів", "auth": "ключ не підходить",
            "no_model": "модель недоступна", "unavailable": "не відповідає", "timeout": "задовго",
            "no_audio": "не повернула аудіо"}


def _all_failed(failures: list[tuple[str, MusicError]]) -> MusicError:
    refused = next((exc for _, exc in failures if exc.code == "refused"), None)
    if refused is not None:  # the song itself was refused: another model would refuse it too
        return refused
    parts = [f"{who} — {_REASONS.get(exc.code, exc.message)}" for who, exc in failures]
    code = "quota" if any(exc.code in ("quota", "no_credits") for _, exc in failures) else failures[-1][1].code
    tail = (" Безкоштовний ліміт закінчився — він оновлюється щодня; або поповніть кредити OpenRouter."
            if code == "quota" else "")
    return MusicError(code, "Не вдалося написати пісню жодною моделлю: " + "; ".join(parts) + "." + tail)


async def generate(prompt: str, lyrics: str = "", instrumental: bool = False) -> tuple[str, bytes, str, str]:
    """(mime, audio, lyrics text, which model wrote it): the chain in order, the next model when one runs out."""
    chain = providers()
    if not chain:
        raise MusicError("not_configured", "Генерація музики не налаштована (немає OPENROUTER_API_KEY чи "
                         "GEMINI_API_KEY на сервері).")
    text_in = lyria_input(prompt, lyrics, instrumental)
    # Models that ran out a moment ago go last: tried only if every other one fails too.
    ordered = [p for p in chain if not _cooling(f"{p[0]}:{p[1]}")] + [p for p in chain if _cooling(f"{p[0]}:{p[1]}")]
    failures: list[tuple[str, MusicError]] = []
    for kind, model in ordered:
        label, who = f"{kind}:{model}", short_label(kind, model)
        started = time.monotonic()
        try:
            mime, data, text = await (_openrouter(model, text_in) if kind == "openrouter" else _gemini(model, text_in))
        except MusicError as exc:
            logger.warning("music.generate.next from=%s code=%s detail=%s", label, exc.code, exc.message[:200])
            _cool(label, exc)
            failures.append((who, exc))
            if exc.code == "refused":
                break
            continue
        logger.info("music.generate ok via=%s ms=%s bytes=%s", label, int((time.monotonic() - started) * 1000), len(data))
        return mime, data, text, who
    raise _all_failed(failures)


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
    model: str = ""  # which model of the chain wrote it
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "status": self.status, "song_id": self.song_id,
                "song_name": self.song_name, "error": self.error, "via": self.via, "model": self.model,
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

    def finish(self, job: MusicJob, song: dict[str, Any], lyrics: str = "", model: str = "") -> None:
        with self._lock:
            job.status, job.song_id, job.song_name, job.lyrics, job.model = "done", song["id"], song["name"], lyrics, model
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
        mime, data, text, model = await generate(prompt, lyrics, instrumental)
        song = await asyncio.to_thread(save_song, job.title, mime, data)
    except MusicError as exc:
        jobs.fail(job, exc.message)
    except Exception:  # never let a background job die silently
        logger.exception("music.job.failed id=%s", job.id)
        jobs.fail(job, "Не вдалося написати пісню.")
    else:
        jobs.finish(job, song, text, model)
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
