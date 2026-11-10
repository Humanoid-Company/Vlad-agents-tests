"""HTTP backend for the hosted web version (deploy on Render).

    uvicorn server.app:app --host 0.0.0.0 --port 8000

The browser (web/, deployed on Vercel) talks to OpenAI GPT-Live directly over WebRTC for
audio; this server only:
  - creates the Live session from the browser's SDP offer (the OpenAI key never leaves it),
  - attaches a sideband to that session and runs the tools (voice, name, language…).

Env: OPENAI_API_KEY, PUBLIC_BACKEND_URL, FRONTEND_ORIGINS. No accounts: everything is open. See DEPLOY.md.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
import urllib.parse
from collections import deque
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from openai import AsyncOpenAI
from pydantic import BaseModel

from config import (
    CAMERA_SCENE_CONTEXT,
    OPENAI_API_KEY,
    OPENAI_LIVE_BACKEND_EFFORT,
    OPENAI_LIVE_BACKEND_MODEL,
    OPENAI_LIVE_MODEL,
    OPENAI_LIVE_PARALLEL_TOOLS,
    OPENAI_LIVE_VOICE,
    WEB_SEARCH_API_KEY,
)
from prompts.backend_prompt import build_backend_prompt, local_today
from prompts.live_prompt import build_concierge_prompt, build_live_prompt
from server import checkins, conversations, knowledge, music, songs, usage
from server.call_log import CallLogStore
from server.db import StoreError
from server.knowledge_import import KnowledgeImportError, make_openai_extract, make_openai_plan, parse_file
from server.live_bridge import SidebandToolBridge
from server.web_users import WebUser, WebUserRegistry
from tools.live_schemas import (
    CONFIRM_GUEST_TOOL,
    FIND_GUEST_TOOL,
    KNOWLEDGE_LOOKUP_TOOL,
    REMEMBER_PERSON_TOOL,
    LIVE_BACKEND_TOOLS,
    LOOK_AT_CAMERA_TOOL,
    MUSIC_TOOLS,
)
from voice.background import wake_commentary
from voice.delegation import responses_delegation
from voice.options import (
    DEFAULT_PERSONA,
    LANGUAGE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    VOICE_PERSONAS,
    api_voice,
    delivery_instruction,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s")
for noisy in ("httpx", "httpcore", "openai"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("server")

PUBLIC_BACKEND_URL = os.getenv("PUBLIC_BACKEND_URL", "http://localhost:8000").rstrip("/")
FRONTEND_ORIGINS = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o.strip()]
SERVER_STARTED_AT = time.time()
# The hidden /logs page reads whole conversations: only with this token (unset = logs can't be read).
LOGS_TOKEN = os.getenv("LOGS_TOKEN", "").strip()
# Cap on one Live session (cost guard: a tab left open in a noisy room kept a call going). 10 min, not 30: a
# real talk goes on — the page opens a new session with the whole conversation (/api/me «expired»).
MAX_SESSION_S = float(os.getenv("MAX_SESSION_MINUTES", "10")) * 60
# The page asks within this long after the cap closed a call whether to carry on.
EXPIRED_FRESH_S = 60.0

_WEB_NOTE = "\nWeb demo: you run in a browser tab for the team to try."
# GPT-Live takes no images: the backend looks through the camera (look_at_camera) and tells her. This
# overrides «answer everything else yourself» in the delegation rules.
_CAMERA_NOTE = (
    "\nCamera: the page can show the backend the user's camera. You cannot see by yourself, so anything "
    "about what is visible — «подивись на мене», «що в мене в руці», «що тут написано», «як я "
    "виглядаю», «що ти бачиш» — always goes to the backend at once (it looks and tells you), even if "
    "you think the camera is off. People show things without naming them: a short question that "
    "points at something — «а це шо?», «це що таке?», «що це?», «а так?», «а тепер?», «шо тєпєрь "
    "показує», «глянь» — means they hold it up to the camera. Send it to the backend at once; never "
    "answer «не знаю, про що ти» or ask what they mean. Every new look is a new frame: what they show "
    "changes. Say what it saw as if you looked yourself («Бачу…»), in your own words; never guess or "
    "describe anything it did not report. You have never seen anything by yourself: what is visible "
    "NOW always needs a new look, even right after one («а зараз?», «що ти бачиш зараз»), never «the "
    "same picture» from memory, and never a room, person or object you imagine. What they showed "
    "before you may recall from earlier looks. One look per request: do not look again on your own. "
    "If you said something a look did not confirm, it was your mistake — say so, never blame the user."
)
# «Музика» (server/music.py): she can't hear music or sing herself — the backend can (recognize_song, generate_song,
# play_song). Short: it is in every call's instructions.
_MUSIC_NOTE = (
    "\nMusic: you cannot hear music or sing by yourself — the backend can. «що це грає?», «що за пісня?», «впізнай "
    "пісню» go to the backend at once: it listens through the mic for about ten seconds, so say only a short "
    "«Слухаю…» and then stay silent until it reports. If they only say they are going to put music on, tell them to "
    "put it on and to say when it plays — listen then, not before. A new song («напиши / створи пісню про …») goes "
    "to the backend once you know what it is about (a theme, a person, an occasion) or its style; a bare «можеш "
    "створити пісню?» — ask what about first. It is ready in a minute or two in «Музика» — the talk goes on "
    "meanwhile; never sing or read lyrics yourself. «увімкни пісню …», «увімкни її» go to the backend too. While a song plays you are paused."
)
_MUSIC_BACKEND_NOTE = (
    "\nMusic: recognize_song listens to what plays near the user — call it once per request, never guess a song. "
    "generate_song starts a new song — only once the user said what it is about or its style; if they did not, "
    "do not call it: write a short question asking what the song should be about. You write the prompt and, for a "
    "song with words, short original lyrics with [Verse] / [Chorus] tags in the user's language. It returns at once — "
    "write that it is being written, never the lyrics. play_song plays a song from «Музика» by name, or recognized=true for the song just recognised.\n"
)
# v5's camera rules, condensed the same way.
# The characters (all but Кішка) are people: what the backend writes for them is never «як ШІ» either.
_PERSON_BACKEND_NOTE = (
    "\nSTRICT RULE: the voice is a character, never an AI assistant: write what is said as that character would, "
    "never «як ШІ», «я програма», «я асистент» or «у мене немає досвіду». (If the user seriously asks whether they talk to an AI, the truth "
    "is told — the voice handles that.)\n"
)
# GPT-Live itself knows nothing of the records: people go to the backend, which looks them up and writes them down.
_PEOPLE_NOTE = (
    "\nPeople: «ти знаєш Олега?», «що пам'ятаєш про Марину?», «запам'ятай, що Іра любить йогу» always go to the "
    "backend (it holds what you know about people and writes down new things). So does anyone telling you who "
    "they are («я Влад», «мене звати Владислав») and every «пам'ятаєш мене?», «хто я?», «що ти про мене знаєш?» — "
    "send it at once with the name they gave, never answer «не знаю» yourself; no name given yet — ask for it. "
    "Once you know someone, you are old friends: warm, familiar, remembering their things as you talk. Say what it reports as your own "
    "memory, in your own manner («О, так, пам'ятаю…») — never «в базі», never a list; nothing found — you "
    "don't recall them. Never invent facts about a person."
)
_CAMERA_NOTE_V5 = (
    "\nCamera: you cannot see by yourself. Anything about what is visible — «що ти бачиш», «подивись на "
    "мене», «що в мене в руці», «що тут написано», or a short question pointing at something («а це "
    "шо?», «а тепер?», «глянь») — goes to the backend at once, every time, even if you think the camera "
    "is off; never ask what they mean. Each look is a new frame: never a scene from memory or "
    "imagination. Tell what it saw as if you looked («Бачу…»), only what it reported; one look per "
    "request. A description no look confirmed is your mistake, never the user's."
)
# The scene line (server/vision.py, CAMERA_SCENE_CONTEXT): quiet «[камера …]» lines from a fast vision model. With
# them a general «що ти бачиш?» needs no backend; anything shown, read or looked at closely still gets a look.
_SCENE_NOTE = (
    "\nCamera lines: while the camera is on you also get quiet lines «[камера ЧЧ:ММ:СС] …» — what a quick "
    "glance saw when the view last changed; a new line comes a second or two after a change, so the newest "
    "one is what is in front of the camera now. This is seeing, and it overrides «always a new look» for "
    "general questions about the whole view — «що ти бачиш?», «хто тут?», «що відбувається?», «де я?»: answer "
    "from the newest line at once, in your own words («Бачу…»), without the backend and without adding "
    "anything it does not say. Everything else still goes to the backend for a new look, as above: "
    "something held up or pointed at («а це шо?», «що в мене в руці?»), text to read, counting, colours or "
    "small details, «а зараз?» right after your answer, or when there is no line yet. «[камера вимкнена]» "
    "means you see nothing now. Never mention these lines; never comment on the view unless asked."
)
# Delivery prompts the page may pick; v5 is the default. v1 and v2 are desktop-only (a page asking for them gets v5).
WEB_PROMPT_VARIANTS = ("v3", "v4", "v5")
# A camera frame (JPEG from the page, 768 px): far below this; anything bigger is not ours.
MAX_FRAME_BYTES = 600_000
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{16,64}$")
_background_tasks: set[asyncio.Task] = set()

# Free Render sleeps after ~15 min without inbound requests, and a restart forgets everyone's
# conversation. While running, ping our own public URL (through Render's proxy, so it counts as
# inbound traffic). It can't wake a sleeping server — the first visitor does that. 0 = off.
# Note: one always-on free service uses ~744 of the 750 free instance hours a month.
KEEP_AWAKE_S = float(os.getenv("KEEP_AWAKE_MINUTES", "10")) * 60


async def _keep_awake() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            await asyncio.sleep(KEEP_AWAKE_S)
            try:
                await http.get(f"{PUBLIC_BACKEND_URL}/healthz")
            except httpx.HTTPError:
                logger.warning("keep-awake ping failed")


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    task = None
    if KEEP_AWAKE_S > 0 and PUBLIC_BACKEND_URL.startswith("https://"):
        task = asyncio.create_task(_keep_awake())
        logger.info("keep-awake every %ss → %s/healthz", int(KEEP_AWAKE_S), PUBLIC_BACKEND_URL)
    if not WEB_SEARCH_API_KEY:
        logger.warning("WEB_SEARCH_API_KEY is not set: web search answers «unavailable»")
    if not knowledge.store.persistent:
        logger.warning("DATABASE_URL is not set: «Знання» lives in a SQLite file that the next deploy wipes")
    yield
    if task is not None:
        task.cancel()
    users.save_all()  # a deploy stops the server: conversations not yet written are written now


app = FastAPI(title="Voice agent web backend", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],  # DELETE: «Нова розмова», background listening off, a knowledge set
    allow_headers=["*"],
)

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
users = WebUserRegistry()
calls = CallLogStore()


# ── who is asking: no accounts, everything is open — a browser is told apart by its X-Client-Id ──
def _user(client_id: str | None) -> WebUser:
    if not client_id or not _CLIENT_ID_RE.match(client_id):
        raise HTTPException(status_code=400, detail="bad_client_id")
    return users.get(client_id)


def _default_voice() -> str:
    return OPENAI_LIVE_VOICE if OPENAI_LIVE_VOICE in VOICE_PERSONAS else DEFAULT_PERSONA


def _talker(client_id: str | None) -> WebUser:
    """Whatever costs tokens (a call, the pause's digest, what is overheard or seen)."""
    return _user(client_id)


def _set_voice_variant(user: WebUser, variant: str | None) -> None:
    """The picker's «новий / старий голос» for the character now picked."""
    voice = _user_voice(user)
    if variant == "preset":
        user.old_voices.add(voice)
    elif variant == "own":
        user.old_voices.discard(voice)


def _user_voice(user: WebUser) -> str:
    """The character this browser picked; one that is gone (an old plain voice) → the default one."""
    return user.voice if user.voice in VOICE_PERSONAS else _default_voice()


def _session_config(user: WebUser, *, concierge: bool = False) -> dict:
    """The assistant knows people the way an acquaintance does (knowledge_lookup, remember_person, no check-in);
    the concierge has its own protocol (_concierge_config) and never sees what Єва wrote down herself."""
    user.sync_character()  # a call always opens with the picked character's own memory
    language = LANGUAGE_OPTIONS.get(user.language, LANGUAGE_OPTIONS["uk"])
    today = local_today()
    voice = _user_voice(user)
    if concierge:
        return _concierge_config(user, language, today, voice)
    # «Завжди пам'ятати» last: the team's rules for this event come after (and over) the general ones.
    standing = knowledge.pinned_note(character=user.character)
    config = {
        "model": OPENAI_LIVE_MODEL,
        "instructions": build_live_prompt(
            language_name=language,
            assistant_name=user.assistant_name,
            today=today,
            voice=voice,
            delivery=delivery_instruction(user.speed, user.style),
            variant=user.prompt_variant,
            old_voice=voice in user.old_voices,
        )
        + _WEB_NOTE
        + (_CAMERA_NOTE_V5 if user.prompt_variant == "v5" else _CAMERA_NOTE)
        + (_SCENE_NOTE if CAMERA_SCENE_CONTEXT else "")
        + _PEOPLE_NOTE
        + _MUSIC_NOTE
        + standing,
        "audio": {"output": {"voice": api_voice(voice, voice in user.old_voices)}},
        "delegation": responses_delegation(
            model=OPENAI_LIVE_BACKEND_MODEL,
            instructions=build_backend_prompt(today=today, language_name=language, camera=True)
            + knowledge.PEOPLE_NOTE
            + (VOICE_PERSONAS[voice].backend_note if voice in VOICE_PERSONAS else "")
            + (_PERSON_BACKEND_NOTE if voice in VOICE_PERSONAS else "")
            + _MUSIC_BACKEND_NOTE
            + knowledge.pinned_note(backend=True, character=user.character),
            tools=[*LIVE_BACKEND_TOOLS, LOOK_AT_CAMERA_TOOL, KNOWLEDGE_LOOKUP_TOOL, REMEMBER_PERSON_TOOL, *MUSIC_TOOLS],
            parallel_tools=OPENAI_LIVE_PARALLEL_TOOLS,
            effort=OPENAI_LIVE_BACKEND_EFFORT,
        ),
    }
    # A new voice or a wake after a long pause is a new Live session: it starts from the
    # conversation so far, so nothing said before is forgotten.
    history = user.conversation.live_input()
    if history:
        config["input"] = history
    return config


# «Консьєрж»: the backend gets only what the job needs — the list, the check-in, ending, the language. No web
# search or camera: nothing to make up a bar «five minutes away» from.
_CONCIERGE_TOOLS = ("find_guest", "confirm_guest", "knowledge_lookup", "end_conversation", "change_language")


def _concierge_config(user: WebUser, language: str, today: str, voice: str) -> dict:
    """Its own prompt (prompts/live_prompt.py build_concierge_prompt), not the friend-for-a-chat v5."""
    tools = [t for t in [*LIVE_BACKEND_TOOLS, FIND_GUEST_TOOL, CONFIRM_GUEST_TOOL, KNOWLEDGE_LOOKUP_TOOL]
             if t["name"] in _CONCIERGE_TOOLS]
    config = {
        "model": OPENAI_LIVE_MODEL,
        "instructions": build_concierge_prompt(language_name=language, today=today, voice=voice,
                                               check_question=user.check_question,
                                               old_voice=voice in user.old_voices)
        + knowledge.pinned_note(character=user.character),
        "audio": {"output": {"voice": api_voice(voice, voice in user.old_voices)}},  # the voice only: a concierge never roasts guests
        "delegation": responses_delegation(
            model=OPENAI_LIVE_BACKEND_MODEL,
            instructions=build_backend_prompt(today=today, language_name=language)
            + knowledge.BACKEND_NOTE + knowledge.concierge_backend_note(user.check_question)
            + knowledge.pinned_note(backend=True, character=user.character),
            tools=tools,
            parallel_tools=OPENAI_LIVE_PARALLEL_TOOLS,
            effort=OPENAI_LIVE_BACKEND_EFFORT,
        ),
    }
    history = user.conversation.live_input()
    if history:
        config["input"] = history
    return config


class SessionRequest(BaseModel):
    sdp: str
    # The page's picker choices: server state is in memory and is lost on a Render restart.
    voice: str | None = None
    voice_variant: str | None = None  # "own" | "preset": which voice of `voice` (when it has an own one)
    speed: str | None = None
    style: str | None = None
    # What the user asked right after the wake phrase: goes into the new call's history as their
    # own message (a quoted commentary made GPT-Live read it back).
    user_text: str | None = None
    # A voice switch opens the new call beside the current one: the page closes the old call
    # itself once the new one has taken over (the old one goes on talking meanwhile).
    keep_old: bool = False
    # Delivery prompt: v4 (default) or v3.
    prompt: str | None = None
    # The page's top switch: "concierge" — she welcomes people and checks them against «Знання».
    mode: str | None = None
    # «Консьєрж» → «Перевірочне питання»: false — the name on the list is enough.
    check_question: bool | None = None


# A kept old call still open this long after its successor opened is closed anyway (page gone).
KEEP_OLD_MAX_S = 120.0


async def _close_if_replaced(old: Any, new: Any, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    if getattr(new, "is_open", False) and getattr(old, "is_open", False):
        logger.info("web.session.replaced_close session_id=%s", getattr(old, "session_id", "?"))
        await old.close()


class VoiceRequest(BaseModel):
    voice: str | None = None
    voice_variant: str | None = None
    speed: str | None = None
    style: str | None = None


@app.get("/healthz")
def healthz() -> dict:
    # Render sets these: shows which commit/branch is actually deployed.
    return {
        "ok": True,
        "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7] or None,
        "branch": os.getenv("RENDER_GIT_BRANCH") or None,
        # Without the Tavily key she says «пошук зараз не працює» — check it here, not by asking her.
        "web_search": bool(WEB_SEARCH_API_KEY),
        # «Знання» and the conversations: postgres (Neon) keeps them; sqlite on Render is wiped by every deploy.
        "knowledge": knowledge.store.kind,
        "conversations": conversations.store.kind,
    }


# ── «Налаштування» → «Про систему»: what runs where, so nobody has to ask in chat ──
def _repo_url() -> str:
    slug = os.getenv("RENDER_GIT_REPO_SLUG", "").strip()
    return f"https://github.com/{slug}" if slug else ""


@app.get("/api/system")
def system_info() -> dict:
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()
    repo = _repo_url()

    def safe(call, default):
        try:
            return call()
        except (StoreError, knowledge.KnowledgeError):
            return default

    sets = safe(knowledge.store.list_sets, [])
    info: dict[str, Any] = {
        "deploy": {
            "commit": commit[:7] or None,
            "commit_url": f"{repo}/commit/{commit}" if repo and commit else "",
            "branch": os.getenv("RENDER_GIT_BRANCH") or None,
            "repo_url": repo,
            "started_at": SERVER_STARTED_AT,
            "host": "Render" if os.getenv("RENDER") else "локально",
        },
        "models": {
            "live": OPENAI_LIVE_MODEL, "backend": OPENAI_LIVE_BACKEND_MODEL,
            "effort": OPENAI_LIVE_BACKEND_EFFORT or "за замовчуванням", "voice": OPENAI_LIVE_VOICE,
        },
        "services": {
            "web_search": bool(WEB_SEARCH_API_KEY),
            "database": knowledge.store.kind, "persistent": knowledge.store.persistent,
            "logs": bool(LOGS_TOKEN),
        },
        "limits": {
            "session_minutes": round(MAX_SESSION_S / 60, 1),
            "live_price_per_minute": usage.LIVE_PRICE_PER_MINUTE,
        },
        "knowledge": {"sets": len(sets), "enabled": sum(1 for s in sets if s.get("enabled"))},
    }
    month = safe(lambda: usage.store.summary(30), None)
    info["usage"] = {"days": 30, **month["total"], "today": month["days"][0] if month and month["days"] else None} if month else None
    return info


@app.post("/api/session")
async def create_session(
    body: SessionRequest,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Browser SDP offer in → SDP answer out; tools run here over a sideband."""
    user = await asyncio.to_thread(_talker, x_client_id)  # may restore the conversation from the database
    if body.voice and body.voice.strip().lower() in VOICE_PERSONAS:
        user.voice = body.voice.strip().lower()
    user.sync_character()  # before anything is added: the first words go into this character's own conversation
    _set_voice_variant(user, body.voice_variant)
    if body.speed in SPEED_OPTIONS:
        user.speed = body.speed
    if body.style in STYLE_OPTIONS:
        user.style = body.style
    user.prompt_variant = body.prompt if body.prompt in WEB_PROMPT_VARIANTS else "v5"
    user.concierge = body.mode == "concierge"
    user.check_question = body.check_question is not False
    user.reconnect_pending = False
    if body.user_text and body.user_text.strip():
        user.conversation.add("user", body.user_text.strip()[:500])
        user.conversation.end_turn()
    try:
        result = await openai_client.live.create(
            session=_session_config(user, concierge=body.mode == "concierge"),
            transport={"type": "webrtc", "sdp": body.sdp},
        )
    except Exception as exc:
        logger.exception("web.session.create_failed")
        raise HTTPException(status_code=502, detail=f"live_create_failed: {type(exc).__name__}") from exc
    session_id = result.session.id
    # One call per browser: an older session (another tab, a page reload) would keep billing
    # and run tools in parallel. A voice switch keeps it for a moment.
    old_bridges = list(user.bridges)
    if not body.keep_old:
        for old in old_bridges:
            try:
                await old.close()
            except Exception:
                logger.debug("closing old session failed", exc_info=True)
    call_log = calls.start(
        session_id, user.client_id[:8], voice=_user_voice(user), prompt=user.prompt_variant,
        model=OPENAI_LIVE_MODEL, backend=OPENAI_LIVE_BACKEND_MODEL, effort=OPENAI_LIVE_BACKEND_EFFORT or "default",
        history=len(user.conversation.live_input()), speed=user.speed, style=user.style,
        **({"mode": "concierge"} if body.mode == "concierge" else {}),
        **({"check_question": False} if body.mode == "concierge" and not user.check_question else {}),
    )
    if body.user_text and body.user_text.strip():
        call_log.add("user", text=body.user_text.strip()[:500], via="wake")
    bridge = SidebandToolBridge(
        client=openai_client,
        session_id=session_id,
        executor=user.executor,
        on_closed=lambda b: _call_closed(user, b),
        max_duration_s=MAX_SESSION_S,
        conversation=user.conversation,
        call_log=call_log,
        think_aloud="neutral" if user.concierge else "full",
    )
    user.bridges.add(bridge)
    user.scene.reset()  # the new call gets the scene from the next frame, in full
    task = asyncio.create_task(bridge.run())
    _background_tasks.add(task)  # keep a reference so the task isn't garbage-collected
    task.add_done_callback(_background_tasks.discard)
    if body.keep_old:
        for old in old_bridges:
            closer = asyncio.create_task(_close_if_replaced(old, bridge, KEEP_OLD_MAX_S))
            _background_tasks.add(closer)
            closer.add_done_callback(_background_tasks.discard)
    logger.info("web.session.created session_id=%s keep_old=%s", session_id, body.keep_old)
    return {"sdp": result.transport.sdp, "session_id": session_id}


def _call_closed(user: WebUser, bridge: SidebandToolBridge) -> None:
    """A call ended: off the user's open calls; a cap close noted for the page; its minutes and tokens saved."""
    user.bridges.discard(bridge)
    if bridge.expired:
        user.session_expired_at = time.time()
    if bridge.call_log is not None:
        row = usage.call_row(bridge.call_log)
        task = asyncio.get_running_loop().run_in_executor(None, _record_usage, row)
        _background_tasks.add(task)  # type: ignore[arg-type]
        task.add_done_callback(_background_tasks.discard)  # type: ignore[arg-type]


def _record_usage(row: dict) -> None:
    try:
        usage.store.record(row)
    except StoreError:
        logger.warning("usage.record_failed session=%s", row["session_id"][-8:])


@app.get("/api/me")
def me(
    x_client_id: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id)
    return {
        "assistant_name": user.assistant_name,
        "voice": user.voice,  # None until chosen; the page keeps its own choice then
        "speed": user.speed,
        "style": user.style,
        "reconnect": user.reconnect_pending,  # voice changed by voice command: page reconnects
        "voice_note": user.voice_switch_note,  # why it changed (diagnostics on the page)
        "keep_session": user.must_keep_session(),  # paused: don't close the call yet
        "history_turns": len(user.conversation),
        "guest": user.guest,  # «Консьєрж»: who the guest was confirmed as
        # The server's cap closed the call a moment ago and no other is open: carry on in a new session.
        "expired": not user.bridges and time.time() - user.session_expired_at < EXPIRED_FRESH_S,
    }


@app.get("/api/voices")
def voices() -> dict:
    """Voices for the page's picker; the chosen one applies from the next call."""
    return {
        "default": _default_voice(),
        "voices": [
            {"id": p.id, "label": p.label, "group": p.group or p.id, "variant_label": p.variant_label, "description": p.description, "feminine": p.feminine,
             "special": p.special, "sample": p.voice, "own_voice": bool(p.custom_voice_id), "wake": list(p.wake_names)}
            for p in VOICE_PERSONAS.values()
        ],
    }


@app.post("/api/voice")
def set_voice(
    body: VoiceRequest,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Voice (applies via reconnect) and speed/style. Both are applied by the page: it reconnects
    for a voice, and sends the returned speed/style instruction once Єва is quiet — the page hears
    her audio, while an instruction that lands mid-answer derails it."""
    user = _user(x_client_id)
    if body.voice is not None:
        voice = body.voice.strip().lower()
        if voice not in VOICE_PERSONAS:
            raise HTTPException(status_code=400, detail="unknown_voice")
        user.voice = voice
        user.sync_character()  # each character has a memory of its own: its conversation comes with it
    _set_voice_variant(user, body.voice_variant)
    delivery_changed = False
    for name, options in (("speed", SPEED_OPTIONS), ("style", STYLE_OPTIONS)):
        value = getattr(body, name)
        if value is None:
            continue
        if value not in options:
            raise HTTPException(status_code=400, detail=f"unknown_{name}")
        delivery_changed |= value != getattr(user, name)
        setattr(user, name, value)
    instruction = delivery_instruction(user.speed, user.style, changed=True) if delivery_changed else ""
    return {"voice": user.voice, "speed": user.speed, "style": user.style, "instruction": instruction}


# ── «Музика»: songs shared by everyone, on every device (server/songs.py, the panel on web/index.html) ──
def _songs(call, *args):
    try:
        return call(*args)
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/songs")
def list_songs() -> dict:
    return {"songs": _songs(songs.store.list)}


@app.post("/api/songs")
async def add_song(request: Request, name: str = "") -> dict:
    """The audio file is the raw request body (no multipart); its name is the `name` query."""
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Це не аудіофайл.")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Порожній файл.")
    if len(data) > songs.MAX_SONG_BYTES:
        raise HTTPException(status_code=413, detail=f"Файл завеликий: найбільше {songs.MAX_SONG_BYTES // 1024 // 1024} МБ.")
    if await asyncio.to_thread(_songs, songs.store.count) >= songs.MAX_SONGS:
        raise HTTPException(status_code=409, detail=f"Пісень уже {songs.MAX_SONGS} — видаліть зайві.")
    return await asyncio.to_thread(_songs, songs.store.add, name, mime, data)


@app.get("/api/songs/{song_id}/audio")
def song_audio(song_id: str, range: str | None = Header(default=None)) -> Response:
    """The file, with Range: Safari won't play audio from a server that ignores it."""
    found = _songs(songs.store.get, song_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Пісню не знайдено.")
    mime, data = found
    size = len(data)
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "public, max-age=86400"}
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", (range or "").strip())
    if match and (match.group(1) or match.group(2)):
        if match.group(1):
            start = int(match.group(1))
            end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
        else:  # «bytes=-500»: the last 500
            start, end = max(0, size - int(match.group(2))), size - 1
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return Response(data[start:end + 1], status_code=206, media_type=mime, headers=headers)
    return Response(data, media_type=mime, headers=headers)


@app.delete("/api/songs/{song_id}")
def delete_song(song_id: str) -> dict:
    return {"ok": _songs(songs.store.delete, song_id)}


# ── «Музика»: recognising what plays nearby and writing new songs (server/music.py) ──
@app.get("/api/music/status")
def music_status() -> dict:
    return music.status()


@app.post("/api/music/listening")
def music_listening(x_client_id: str | None = Header(default=None)) -> dict:
    """The page started recording a clip: recognize_song waits for it instead of giving up."""
    _user(x_client_id).music_listening_at = time.time()
    return {"ok": True}


@app.post("/api/music/recognize")
async def music_recognize(request: Request, via: str = "button", x_client_id: str | None = Header(default=None),
                          x_clip_level: str | None = Header(default=None)) -> dict:
    """A clip of what plays nearby (raw audio body). The result goes to the page and to a waiting recognize_song."""
    user = await asyncio.to_thread(_talker, x_client_id)
    mime = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not mime.startswith("audio/"):
        raise HTTPException(status_code=415, detail="Це не аудіо.")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Порожній запис.")
    if len(data) > music.MAX_CLIP_BYTES:
        raise HTTPException(status_code=413, detail="Запис завеликий.")
    try:
        level = float(x_clip_level) if x_clip_level else None
    except ValueError:
        level = None
    if level is not None and level < music.QUIET_LEVEL:
        logger.info("music.recognize.quiet level=%.5f bytes=%s", level, len(data))
        result = {"found": False, "quiet": True}
        user.set_music_result(result)
        return result
    try:
        result = await asyncio.to_thread(music.recognize, data, mime)
    except music.MusicError as exc:
        user.set_music_result({"found": False, "error": exc.message})
        raise HTTPException(status_code=503 if exc.code in ("not_configured", "unavailable") else 502,
                            detail=exc.message) from exc
    user.set_music_result(result)
    if via != "voice" and result.get("found") and user.bridges and not user.concierge:
        # Recognised from the panel during a call: she knows it if they ask «а що це було?».
        task = asyncio.create_task(user.think_note(f"[музика] Користувач щойно впізнав у панелі «Музика»: "
                                                   f"{music.describe(result)}."))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return result


@app.get("/api/music/action")
def music_action(x_client_id: str | None = Header(default=None)) -> dict:
    """What play_song asked this page to play (taken once)."""
    return {"action": _user(x_client_id).take_music_action()}


class GenerateBody(BaseModel):
    prompt: str = ""
    lyrics: str = ""
    instrumental: bool = False
    title: str = ""


@app.post("/api/music/generate")
async def music_generate(body: GenerateBody, x_client_id: str | None = Header(default=None)) -> dict:
    """«Створити» in the panel: a song job like generate_song's (she is not told — the panel shows it)."""
    user = await asyncio.to_thread(_talker, x_client_id)
    if not music.providers():
        raise HTTPException(status_code=503, detail="Генерація музики не налаштована (немає OPENROUTER_API_KEY чи GEMINI_API_KEY на сервері).")
    prompt, lyrics = body.prompt.strip(), body.lyrics.strip()
    if not prompt and not lyrics:
        raise HTTPException(status_code=400, detail="Опишіть пісню: про що, у якому стилі.")
    if not prompt:
        prompt = "A song with these lyrics."
    title = music.song_title(body.title, prompt)
    try:
        job = music.jobs.start(user.client_id, title=title, prompt=prompt, via="button")
    except music.MusicError as exc:
        raise HTTPException(status_code=409, detail=exc.message) from exc
    task = asyncio.create_task(music.run_job(job, prompt, lyrics, body.instrumental, notify=user.song_ready))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return job.public()


@app.get("/api/music/jobs")
def music_jobs(x_client_id: str | None = Header(default=None)) -> dict:
    return {"jobs": music.jobs.for_client(_user(x_client_id).client_id)}


class OverheardBody(BaseModel):
    text: str


@app.post("/api/overheard")
def overheard(
    body: OverheardBody,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """A phrase the page's recogniser heard while Єва was paused (not addressed to her)."""
    _talker(x_client_id).background.add(body.text[:1000])
    return {"ok": True}


# Numbers only from the page (mic gate levels, barge-in timings, unanswered turns — no words): how the
# call sounds on the testers' own devices, which the live tests can't show (her echo on laptop speakers,
# a quiet voice over her). The last few hundred, in memory; GET /api/diag reads them.
_DIAG_MAX_BYTES = 4000
_diag: deque[dict] = deque(maxlen=200)


def _numbers_only(value: Any, depth: int = 0) -> Any:
    """Keeps numbers, booleans and short keyword strings (states like «muted»); drops any other text."""
    if depth > 3:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= 24 and re.fullmatch(r"[\w.:-]*", value) else None
    if isinstance(value, list):
        return [_numbers_only(v, depth + 1) for v in value[:30]]
    if isinstance(value, dict):
        return {str(k)[:24]: _numbers_only(v, depth + 1) for k, v in list(value.items())[:40]}
    return None


@app.post("/api/diag")
async def diag_post(
    request: Request,
    x_client_id: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id)
    raw = await request.body()
    if len(raw) > _DIAG_MAX_BYTES:
        raise HTTPException(status_code=413, detail="too_big")
    try:
        data = _numbers_only(json.loads(raw))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="bad_json") from exc
    _diag.append({"at": round(time.time()), "client": user.client_id[:6], "commit": os.getenv("RENDER_GIT_COMMIT", "")[:7], "data": data})
    return {"ok": True}


@app.get("/api/diag")
def diag_get(n: int = 50) -> list[dict]:
    return list(_diag)[-max(1, min(n, 200)):]


def _logs_auth(token: str | None) -> None:
    """Open, unless LOGS_TOKEN is set on the server (then only with it — the logs hold whole conversations)."""
    if not LOGS_TOKEN:
        return
    if not hmac.compare_digest((token or "").encode(), LOGS_TOKEN.encode()):
        raise HTTPException(status_code=401, detail="bad_logs_token")


@app.get("/api/logs")
def logs_list(x_logs_token: str | None = Header(default=None), n: int = 100) -> list[dict]:
    """The latest calls, newest first (web/logs.html). In memory: a deploy clears them."""
    _logs_auth(x_logs_token)
    return calls.latest(max(1, min(n, 200)))


@app.get("/api/usage")
def usage_summary(x_logs_token: str | None = Header(default=None), days: int = 14) -> dict:
    """/logs → «Витрати»: minutes, tokens and the estimated $ per day and per browser (server/usage.py)."""
    _logs_auth(x_logs_token)
    try:
        return usage.store.summary(max(1, min(days, 90)))
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/logs/{session_id}")
def logs_call(session_id: str, x_logs_token: str | None = Header(default=None)) -> dict:
    """One call: every line, note, backend step and tool call with its arguments, result and time."""
    _logs_auth(x_logs_token)
    log = calls.get(session_id)
    if log is None:
        raise HTTPException(status_code=404, detail="unknown_call")
    return log.to_dict()


@app.delete("/api/overheard")
def overheard_clear(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Background listening turned off on the page: forget what was heard so far."""
    _user(x_client_id).background.clear()
    return {"ok": True}


@app.post("/api/overheard/digest")
def overheard_digest(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """On wake: the gist of the pause as a commentary for the page to send; kept in the history."""
    user = _talker(x_client_id)
    note = user.background.digest()
    if not note:
        return {"commentary": ""}
    user.conversation.add_note(note)
    logger.info("web.background.digest chars=%s", len(note))
    return {"commentary": wake_commentary(note)}


@app.post("/api/frame")
async def camera_frame(
    request: Request,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """The page's latest camera frame (raw JPEG body), kept in memory for look_at_camera."""
    user = await asyncio.to_thread(_talker, x_client_id)
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame_too_large")  # before reading it in
    body = await request.body()
    if len(body) > MAX_FRAME_BYTES:
        raise HTTPException(status_code=413, detail="frame_too_large")
    if not body.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=415, detail="jpeg_expected")
    user.set_camera_frame(body)
    # The scene line: the page scores how much the view changed since its last frame (0–100).
    change = _frame_change(request.headers.get("x-frame-change", ""))
    if user.watches_scene:
        task = asyncio.create_task(user.offer_scene_frame(body, change))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    return {"ok": True}


def _frame_change(value: str) -> float | None:
    try:
        return max(0.0, min(100.0, float(value)))
    except ValueError:
        return None  # an old page or an edge camera: no hint


@app.delete("/api/frame")
async def camera_off(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Camera turned off on the page: forget the last frame (and tell an open call its scene is gone)."""
    user = await asyncio.to_thread(_user, x_client_id)
    was_on = user.camera_on
    user.camera_off()
    if was_on and user.bridges:
        await user.camera_off_note()
    return {"ok": True}


@app.delete("/api/conversation")
def clear_conversation(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """«Нова розмова»: Єва starts from a clean page; the old conversation stays saved («Історія»)."""
    user = _user(x_client_id)
    user.new_conversation()
    user.background.clear()
    return {"ok": True}


# ── the conversations: the current one (shown again after a refresh) and «Історія» (server/conversations.py) ──
def _history(call, *args):
    try:
        return call(*args)
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/conversation")
def current_conversation(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """What the page shows on load: the conversation Єва remembers, as it was said."""
    user = _user(x_client_id)
    user.sync_character()
    return {"id": user.conversation_id, "started_at": user.conversation_started_at,
            "turns": conversations.visible(user.conversation.to_list())}


# ── «Консьєрж»: who has been let in (server/checkins.py) — a second visit under the same name is caught ──
@app.get("/api/concierge/checkins")
def concierge_checkins() -> dict:
    try:
        return {"checkins": checkins.store.all()}
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.delete("/api/concierge/checkins")
def concierge_reset() -> dict:
    """A new event: everyone may come in again."""
    try:
        return {"ok": True, "removed": checkins.store.reset()}
    except StoreError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/conversations")
def conversation_list(
    x_client_id: str | None = Header(default=None),
) -> dict:
    """This browser's saved conversations, newest first (the current one is marked)."""
    user = _user(x_client_id)
    user.sync_character()
    user.save_conversation()  # the current one as it is now
    # Only this character's: each one has its own memory, and «Продовжити» must not mix them.
    return {"current": user.conversation_id, "persistent": conversations.store.persistent,
            "conversations": _history(conversations.store.list, user.owner, conversations.LIST_MAX, user.character)}


@app.get("/api/conversations/{conv_id}")
def conversation_get(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
) -> dict:
    user = _user(x_client_id)
    if conv_id == user.conversation_id:
        user.save_conversation()
    found = _history(conversations.store.get, user.owner, conv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    found["turns"] = conversations.visible(found["turns"])
    return found


@app.post("/api/conversations/{conv_id}/resume")
def conversation_resume(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """«Продовжити»: a saved conversation becomes the current one — Єва remembers it from the next call."""
    user = _user(x_client_id)
    found = _history(conversations.store.get, user.owner, conv_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    user.resume_conversation(found)
    user.background.clear()
    return {"ok": True, "id": conv_id}


@app.delete("/api/conversations/{conv_id}")
def conversation_delete(
    conv_id: str,
    x_client_id: str | None = Header(default=None),
) -> dict:
    """Delete a saved conversation; the current one is also forgotten by Єва (a new one starts)."""
    user = _user(x_client_id)
    if conv_id == user.conversation_id:
        user.new_conversation()
    if not _history(conversations.store.delete, user.owner, conv_id):
        raise HTTPException(status_code=404, detail="Такої розмови немає.")
    return {"ok": True}


# ── «Знання»: the team's knowledge sets (server/knowledge.py, web/knowledge.html) ──
# One store for everyone with the access code. Upload is two steps: /parse reads the file and returns the
# records (nothing saved), the page shows them, and POST /api/knowledge saves what the user kept.
_extract = None
_plan = None


def _kb(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except knowledge.KnowledgeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _text_extract():
    global _extract
    if _extract is None:
        from openai import OpenAI

        _extract = make_openai_extract(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _extract


def _column_plan():
    """The model that names a table's person column and shortens question headers («Який твій улюблений…?»)."""
    global _plan
    if _plan is None:
        from openai import OpenAI

        _plan = make_openai_plan(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _plan


class KnowledgeItem(BaseModel):
    title: str = ""
    fields: dict[str, Any] = {}


class KnowledgeSetIn(BaseModel):
    name: str
    note: str = ""
    source: str = ""
    items: list[KnowledgeItem] = []
    # "text": one text under the name (body), written on the page — «Правила», «Про подію», «Історії»…
    kind: str = "records"
    body: str = ""
    pinned: bool = False  # «Завжди пам'ятати»: in the instructions of every call
    character: str = ""  # a character's id: the set is known to it alone; "" = everyone


class KnowledgeSetPatch(BaseModel):
    enabled: bool | None = None
    pinned: bool | None = None
    name: str | None = None
    note: str | None = None
    body: str | None = None  # a text set's new text
    character: str | None = None  # whose set it is ("" = everyone)


def _set_character(value: str) -> str:
    """The character a set is for: one of the picker's ("" = everyone)."""
    value = (value or "").strip().lower()
    if value and value not in {p.group or p.id for p in VOICE_PERSONAS.values()}:
        raise HTTPException(status_code=400, detail="Невідомий персонаж.")
    return value


@app.post("/api/knowledge/parse")
async def knowledge_parse(request: Request) -> dict:
    """The file as the raw body, its name in X-File-Name (URL-encoded) → the records it holds."""
    filename = urllib.parse.unquote(request.headers.get("x-file-name", ""))[:200]
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > 5_000_000:
        raise HTTPException(status_code=413, detail="Файл завеликий: до 5 МБ.")
    data = await request.body()
    try:
        parsed = await asyncio.to_thread(parse_file, filename, data, lambda text: _text_extract()(text),
                                        lambda header, sample: _column_plan()(header, sample))
    except KnowledgeImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info("knowledge.parsed ext=%s items=%s kind=%s", filename.rsplit(".", 1)[-1], len(parsed.items), parsed.kind)
    return parsed.to_dict()


@app.get("/api/knowledge")
def knowledge_sets() -> dict:
    return {"storage": knowledge.store.kind, "persistent": knowledge.store.persistent,
            "sets": [s for s in _kb(knowledge.store.list_sets)],
            "pinned_chars": _kb(knowledge.store.pinned_chars), "pinned_max": knowledge.PINNED_MAX_CHARS}


@app.post("/api/knowledge")
def knowledge_create(body: KnowledgeSetIn) -> dict:
    items = [item.model_dump() for item in body.items]
    created = _kb(knowledge.store.create_set, body.name, items, note=body.note, source=body.source,
                  kind=body.kind, body=body.body, pinned=body.pinned, character=_set_character(body.character))
    return created


@app.get("/api/knowledge/{set_id}")
def knowledge_get(set_id: int) -> dict:
    found = _kb(knowledge.store.get_set, set_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    return found


@app.post("/api/knowledge/{set_id}")
def knowledge_update(set_id: int, body: KnowledgeSetPatch) -> dict:
    """Turn a set on or off for Єва («Єва знає»), pin it («Завжди пам'ятати»), rename it, change its note or text."""
    found = _kb(knowledge.store.update_set, set_id, enabled=body.enabled, name=body.name, note=body.note,
                pinned=body.pinned, body=body.body,
                character=None if body.character is None else _set_character(body.character))
    if found is None:
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    return found


@app.delete("/api/knowledge/{set_id}")
def knowledge_delete(set_id: int) -> dict:
    found = _kb(knowledge.store.get_set, set_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    if not _kb(knowledge.store.delete_set, set_id):
        raise HTTPException(status_code=404, detail="Такого набору немає.")
    logger.info("knowledge.set_deleted id=%s", set_id)
    return {"ok": True}
