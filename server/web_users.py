"""Per-browser state for the hosted version.

Each browser gets a random client id (kept in its localStorage): its own voice settings,
conversation history and tool executor, so two people testing at once never share a conversation.

State lives in memory, except the conversation: it is saved to the database (server/conversations.py) a
couple of seconds after each change and restored when the browser comes back after a restart, so Єва
remembers it and the page shows it again. Voice settings come from the page with every call.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from config import WEB_SEARCH_MAX_CALLS_PER_TURN
from integrations.web_search import WebSearchRateLimiter
from server import checkins, conversations, guests, knowledge
from server.db import StoreError
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.results import ToolResult
from tools.web_search_tool import web_search_tool_result
from voice.background import BackgroundLog, make_openai_summarizer
from voice.conversation import ConversationLog
from voice.options import (
    LANGUAGE_OPTIONS,
    SPEED_OPTIONS,
    STYLE_OPTIONS,
    DEFAULT_PERSONA,
    VOICE_PERSONAS,
    _sanitize_name,
    asked_for_language,
    delivery_instruction,
)

logger = logging.getLogger(__name__)

_MAX_USERS = 500
_IDLE_EVICT_S = 24 * 3600
_background: set[asyncio.Task] = set()
VOICE_SWITCH_DEDUP_S = 15.0
# A conversation is saved this long after it changed: transcript deltas come many a second, one write covers them.
SAVE_DELAY_S = 2.0
# Camera (look_at_camera). A look is what is in front of the camera now: the page snaps a frame as soon
# as the request goes to the backend (and again when this tool is called). Taking the latest periodic
# frame (up to 5 s old) missed what was just held up — prod: «підніс до камери, а вона нічого не
# побачила». A frame that arrived after the work started, or this long before the call, is «now»: the
# one snapped at the start is there before the backend has even decided to look (measured on GPT-Live:
# the tool no longer waits ~0.4 s for the upload)…
FRAME_FOR_LOOK_S = 0.5
# …the tool waits this long for it…
FRAME_WAIT_S = 2.5
# …and without it (an old cached page, a lost upload) takes the page's latest periodic frame if it is
# no older than this.
FRAME_FALLBACK_S = 6.0


@dataclass
class WebUser:
    client_id: str
    # Whose conversations these are: the browser's own id, or a logged-in account's (Account.owner) — then
    # «Історія» follows the person to any device.
    owner: str = ""
    executor: ToolExecutor = field(init=False)
    assistant_name: str | None = None
    language: str = "uk"
    voice: str | None = None
    # Characters whose old (preset) voice the page picked instead of the own one (the picker's «новий / старий»).
    old_voices: set[str] = field(default_factory=set)
    speed: str = "normal"
    style: str = "normal"
    prompt_variant: str = "v5"  # delivery prompt (v5 default = v4 condensed, v4 = v3 + full answers), sent by the page with every call
    # The voice was changed during a call: the page reconnects with the new voice and the same
    # conversation once the current session closes.
    reconnect_pending: bool = False
    # Several paths can act on one «зміни голос …» (browser recogniser, transcript, the model):
    # within this window after a switch the others are the same request, not a new one.
    voice_switched_at: float = 0.0
    voice_switch_note: str = ""  # who switched it — shown on the page next to «Голос змінено»
    # Dialogue history independent of the Live session (and so of the voice), saved to the database.
    conversation: ConversationLog = field(default_factory=lambda: ConversationLog(merge_interleaved=True))
    conversation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    conversation_started_at: float = field(default_factory=time.time)
    # Whose conversation it is: each character has a memory of its own (see `character`); picking another one
    # puts the current conversation away and takes that character's own.
    conversation_character: str = ""
    _save_timer: threading.Timer | None = field(default=None, repr=False)
    _save_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _write_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # What people nearby said while Єва was paused — handed to her on the next wake.
    background: BackgroundLog = field(default_factory=lambda: BackgroundLog(_summarize_background))
    last_seen: float = field(default_factory=time.time)
    bridges: set[Any] = field(default_factory=set)  # open calls (SidebandToolBridge)
    # The latest camera frame (JPEG), in memory only; camera_on is false once the page turns it off.
    # When the server last closed one of its calls at the session cap (MAX_SESSION_MINUTES): the page
    # opens a new one with the whole conversation if the user was still talking.
    session_expired_at: float = 0.0
    # «Консьєрж»: who the guest was confirmed as (confirm_guest); cleared by «Наступний гість».
    guest: dict | None = None
    concierge: bool = False  # the current call was opened in «Консьєрж» mode
    check_question: bool = True  # «Перевірочне питання» on the page; off — the name on the list is enough
    camera_on: bool = False
    camera_frame: bytes | None = None
    camera_frame_at: float = 0.0

    @property
    def character(self) -> str:
        """The character the user is talking to: the picker's tab (the three Степани are one), by its group."""
        persona = VOICE_PERSONAS.get(self.voice or DEFAULT_PERSONA) or VOICE_PERSONAS[DEFAULT_PERSONA]
        return persona.group or persona.id

    @property
    def memory_set(self) -> str:
        """The knowledge set this character writes down into (knowledge.remember): its own, never another's."""
        if self.character == DEFAULT_PERSONA:
            return knowledge.MEMORY_SET
        persona = VOICE_PERSONAS.get(self.voice or DEFAULT_PERSONA) or VOICE_PERSONAS[DEFAULT_PERSONA]
        return knowledge.MEMORY_PREFIX + persona.label

    def set_camera_frame(self, jpeg: bytes) -> None:
        self.camera_frame, self.camera_frame_at, self.camera_on = jpeg, time.time(), True

    def camera_off(self) -> None:
        self.camera_on, self.camera_frame, self.camera_frame_at = False, None, 0.0

    def must_keep_session(self) -> bool:
        """A pause closes the Live session after 15 s (web/config.js) — but not while a tool still runs in it."""
        return any(getattr(bridge, "busy", False) for bridge in self.bridges)

    async def append_instruction(self, text: str) -> None:
        """Tell the running call something without a reply (e.g. the new language)."""
        for bridge in list(self.bridges):
            try:
                await bridge.append_instruction(text)
            except Exception:
                logger.debug("append_instruction failed", exc_info=True)

    async def apply_delivery(self) -> None:
        """Push the current speed/style into the running call."""
        text = delivery_instruction(self.speed, self.style, changed=True)
        for bridge in list(self.bridges):
            try:
                await bridge.append_instruction(text)
            except Exception:
                logger.debug("apply_delivery failed", exc_info=True)

    def __post_init__(self) -> None:
        self.owner = self.owner or self.client_id
        self.executor = _build_executor(self)
        self.conversation_character = self.conversation_character or self.character
        self.conversation.on_change = self._save_soon

    # ── the conversation in the database (server/conversations.py) ──
    def _save_soon(self) -> None:
        with self._save_lock:
            if self._save_timer is None:
                self._save_timer = threading.Timer(SAVE_DELAY_S, self.save_conversation)
                self._save_timer.daemon = True
                self._save_timer.start()

    def save_conversation(self) -> None:
        """Write it now (a pending save is folded in). A store that is down only loses this write."""
        with self._save_lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
                self._save_timer = None
        with self._write_lock:  # one write at a time: an older snapshot never lands after a newer one
            try:
                conversations.store.save(
                    self.conversation_id, self.owner, self.conversation_started_at, self.conversation.to_list(),
                    self.conversation_character,
                )
            except StoreError:
                logger.warning("web.conversation.save_failed client=%s", self.client_id[:8])

    def restore_conversation(self, in_use: frozenset[str] = frozenset()) -> None:
        """After a restart: the owner's latest conversation becomes the current one again — unless another
        device of the same account has it open now (two writers would overwrite each other's lines)."""
        try:
            saved = conversations.store.latest(self.owner, self.conversation_character)
        except StoreError:
            return
        if saved and saved["id"] not in in_use and not len(self.conversation):
            self.conversation_id, self.conversation_started_at = saved["id"], saved["started_at"]
            self.conversation.load(saved["turns"])
            logger.info("web.conversation.restored client=%s turns=%s", self.client_id[:8], len(saved["turns"]))

    def switch_owner(self, owner: str, in_use: frozenset[str] = frozenset()) -> None:
        """Logged in or out in this browser: the current conversation is saved under the old owner, and the
        new owner's latest one is picked up (none after a logout: the page starts clean)."""
        if owner == self.owner:
            return
        self.save_conversation()
        self.owner = owner
        self.conversation_id, self.conversation_started_at = uuid.uuid4().hex, time.time()
        self.conversation.clear()
        self.guest = None
        self.restore_conversation(in_use)

    def sync_character(self) -> bool:
        """The picked character changed: the conversation of the one before is saved, and this character's own
        latest one (or an empty one) becomes the current — so every character remembers only what was said to it.
        True when it switched."""
        now = self.character
        if now == self.conversation_character:
            return False
        if len(self.conversation):
            self.save_conversation()  # under the old character
        self.conversation_character = now
        self.conversation_id, self.conversation_started_at = uuid.uuid4().hex, time.time()
        self.conversation.clear()
        self.guest = None
        self.restore_conversation(frozenset())
        logger.info("web.conversation.character client=%s character=%s turns=%s", self.client_id[:8], now, len(self.conversation))
        return True

    def new_conversation(self) -> None:
        """«Нова розмова»: the current one stays saved (in «Історія»), a new one starts empty."""
        self.save_conversation()
        self.conversation_id, self.conversation_started_at = uuid.uuid4().hex, time.time()
        self.conversation.clear()
        self.guest = None

    def resume_conversation(self, saved: dict) -> None:
        """«Продовжити»: a saved conversation becomes the current one (the current one is saved first)."""
        self.save_conversation()
        self.conversation_id, self.conversation_started_at = saved["id"], saved["started_at"]
        self.conversation.load(saved["turns"])


_summarizer = None


def _concierge_marks(text: str, conversation_id: str, check_question: bool = True) -> str:
    """«Консьєрж»: each record says what to ask about — a fact picked at random per guest and visit (one
    question for everyone, «ким ви працюєте?», could be learned) — or that the person was let in already, so
    the backend stops before the check question."""
    try:
        done = {checkins.name_key(c["full_name"]): c["time"] for c in checkins.store.all()}
    except StoreError:
        done = {}
    lines = []
    for line in text.split("\n"):
        if not line.startswith("[") or " — " not in line:
            lines.append(line)
            continue
        title, facts = line.split("] ", 1)[-1].split(" — ", 1)
        when = done.get(checkins.name_key(title))
        if when:
            lines.append(f"{line} [ВЖЕ ВІДМІЧЕНО о {when}]")
            continue
        keys = [f.split(":", 1)[0].strip() for f in facts.split("; ") if ":" in f]
        pick = guests.pick_fact(title, keys, conversation_id) if check_question else ""
        if pick:
            lines.append(f"{line} [перевірка: {pick}]")
        else:
            lines.append(line)
    return "\n".join(lines)


def _summarize_background(text: str) -> str:
    """Compress a long pause's overheard talk (lazy: one OpenAI client for the whole server)."""
    global _summarizer
    if _summarizer is None:
        from openai import OpenAI

        from config import OPENAI_API_KEY, OPENAI_LIVE_BACKEND_MODEL

        _summarizer = make_openai_summarizer(OpenAI(api_key=OPENAI_API_KEY), OPENAI_LIVE_BACKEND_MODEL)
    return _summarizer(text)


def _build_executor(user: WebUser) -> ToolExecutor:
    """The session tools (tools/live_schemas.py). A new tool: describe it there, register it here."""
    executor = ToolExecutor()

    def set_name(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = _sanitize_name(str(args.get("name") or ""))
        if not name:
            return ToolResult(ok=False, status="needs_more_info", message="Не зрозумів нового імені.")
        user.assistant_name = name
        return ToolResult(ok=True, status="ok", message=f"Тепер мене звати {name}.")

    def change_language(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        language = str(args.get("language") or "").strip().lower()
        if language not in LANGUAGE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Доступні мови: uk, ru, en.")
        if not asked_for_language(language, ctx.user_utterances):
            # They spoke Russian (or English) — not a request: it stuck for every next call.
            logger.info("web.language.tool_refused language=%s (user didn't ask for it)", language)
            return ToolResult(
                ok=False, status="error",
                message=f"Користувач не просив змінити мову — не змінюй її: говори далі "
                f"{LANGUAGE_OPTIONS[user.language]}, навіть якщо він сказав щось іншою мовою.",
            )
        user.language = language
        lang_name = LANGUAGE_OPTIONS[language]
        # The running call was started with «Speak exclusively in …» the old language: without this
        # she went back to it a few replies later (the desktop did this already).
        task = asyncio.get_running_loop().create_task(
            user.append_instruction(f"From now on speak exclusively in {lang_name}.")
        )
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ToolResult(
            ok=True,
            status="ok",
            message=f"Мову змінено на {lang_name}. Нічого про це не кажи: просто говори далі цією мовою, а "
            "якщо вже почала відповідати — продовжуй, не починай заново.",
        )

    def set_voice_style(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        speed = str(args.get("speed") or user.speed).strip().lower()
        style = str(args.get("style") or user.style).strip().lower()
        if speed not in SPEED_OPTIONS or style not in STYLE_OPTIONS:
            return ToolResult(ok=False, status="needs_more_info", message="Темп: slow/normal/fast, стиль: calm/normal/expressive.")
        user.speed, user.style = speed, style
        task = asyncio.get_running_loop().create_task(user.apply_delivery())
        _background.add(task)
        task.add_done_callback(_background.discard)
        return ToolResult(ok=True, status="ok", message="Готово, говорю так з наступного речення.")

    def end_conversation(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return ToolResult(ok=True, status="ok", message="Попрощайся коротко.")

    # At most a few searches per request (per browser), so one question can't loop the paid API.
    limiter = WebSearchRateLimiter(max_per_turn=WEB_SEARCH_MAX_CALLS_PER_TURN)

    def web_search(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        return web_search_tool_result(
            args, session_id=ctx.session_id, delegation_id=ctx.delegation_id, limiter=limiter
        )

    def look_at_camera(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        called_at = time.time()
        fresh_since = called_at - FRAME_FOR_LOOK_S
        if ctx.extras.get("work_started_at"):
            fresh_since = min(fresh_since, ctx.extras["work_started_at"])
        deadline = time.monotonic() + FRAME_WAIT_S
        while True:  # runs in a worker thread: waiting here never stalls the call
            if not user.camera_on:
                return ToolResult(
                    ok=False,
                    status="camera_off",
                    message="Камера вимкнена. Скажи коротко, що не бачиш, і попроси увімкнути «Камера» на сторінці.",
                )
            frame, frame_at = user.camera_frame, user.camera_frame_at
            if frame and frame_at >= fresh_since:
                break  # snapped for this look
            if time.monotonic() >= deadline:
                if frame and time.time() - frame_at <= FRAME_FALLBACK_S:
                    break
                return ToolResult(
                    ok=False,
                    status="no_frame",
                    message="Камера увімкнена, але кадр не надходить. Попроси перевірити камеру на сторінці.",
                )
            time.sleep(0.1)
        age = time.time() - frame_at
        question = str(args.get("question") or "").strip()[:300]
        logger.info("web.camera.look bytes=%s age_s=%.1f", len(frame), age)
        return ToolResult(
            ok=True,
            status="ok",
            message="Кадр з камери користувача — у зображенні нижче. Опиши, що на ньому"
            + (f", відповідаючи на питання: «{question}»." if question else "."),
            image_jpeg=frame,
        )

    def knowledge_lookup(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        query = str(args.get("query") or "").strip()[:300]
        try:
            found = knowledge.store.lookup(query, str(args.get("set") or ""), with_memory=not user.concierge,
                                         memory=user.memory_set, character=user.character)
        except knowledge.KnowledgeError as exc:
            return ToolResult(ok=False, status="error", message=f"{exc} Скажи, що зараз не можеш заглянути в записи команди.")
        logger.info("web.knowledge.lookup total=%s shown=%s", found["total"], found["shown"])
        if not found["total"]:
            return ToolResult(
                ok=True, status="empty",
                message="Записів ще немає (сторінка «Знання»; ти й сама записуєш, що тобі розкажуть). Скажи, що нікого такого не пригадуєш.",
            )
        text = found["text"]
        if user.concierge:
            text = _concierge_marks(text, user.conversation_id, user.check_question)
        head = (
            f"Записи команди ({found['shown']} з {found['total']}"
            + (", найближчі до запиту" if found["shown"] < found["total"] else ", усі")
            + "). Відповідай лише з них; чого тут немає — того ти не знаєш.\n"
        )
        return ToolResult(ok=True, status="ok", message=head + text, data={"total": found["total"], "shown": found["shown"]})

    def _guest_records() -> list:
        """The guest list is what the team uploaded: what Єва wrote down herself never lets anyone in."""
        return [r for r in knowledge.store.enabled_records(user.character)[1]
                if not knowledge.is_memory(r.set_name)]

    def remember_person(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = " ".join(str(args.get("name") or "").split())
        facts = args.get("facts")
        if isinstance(facts, dict):
            facts = {str(k): str(v) for k, v in facts.items() if v is not None}
        else:
            facts = {}
        try:
            saved = knowledge.store.remember(name, facts, user.memory_set)
        except knowledge.KnowledgeError as exc:
            return ToolResult(ok=False, status="error", message=f"{exc} Скажи, що цього не вдалося записати.")
        logger.info("web.knowledge.remember new=%s", saved["new"])
        return ToolResult(
            ok=True, status="ok", data={"new": saved["new"]},
            message=("Записала: " if saved["new"] else "Дописала до того, що знаєш про людину: ") + saved["person"]
            + ". Підтверди одним коротким реченням у своєму характері, без «додано до бази».",
        )

    def find_guest(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        heard = " ".join(str(args.get("name") or "").split())[:100]
        surname = " ".join(str(args.get("surname") or "").split())[:100]
        try:
            records = _guest_records()
        except knowledge.KnowledgeError as exc:
            return ToolResult(ok=False, status="error", message=f"{exc} Скажи, що зараз не можеш перевірити список, і порадь звернутися до команди самостійно (покликати когось ти не можеш).")
        found = guests.find_guest(heard, surname, records)
        logger.info("web.concierge.find status=%s", found.status)
        said = (heard + " " + surname).strip()
        if found.status == "not_found":
            return ToolResult(
                ok=True, status="not_found", data=guests.as_dict(found),
                message=f"У списку гостей нікого з іменем «{said}» немає (перевірено й короткі форми та схожі написання). "
                "Ввічливо скажи, що не знаходиш людину з таким ім'ям серед гостей; якщо ще не перепитували — попроси "
                "повторити ім'я або назвати прізвище. Якщо й тоді немає — порадь звернутися до того, хто запрошував, або "
                "до когось із команди. Не пускай і нікого зі списку не називай.",
            )
        if found.status == "ask_surname":
            if surname:  # «Саша Козак»: Олександр and Олександра Козак — the surname can't decide it
                return ToolResult(
                    ok=True, status="ask_full_name", data=guests.as_dict(found),
                    message=f"З таким ім'ям і прізвищем у списку {found.count} людей. Спитай повне ім'я (не "
                    "скорочене) — нікого не називай.",
                )
            return ToolResult(
                ok=True, status="ask_surname", data=guests.as_dict(found),
                message=f"З цим ім'ям у списку {found.count} людей. Спитай прізвище (можна й повне ім'я) — нікого не називай.",
            )
        record = found.record
        assert record is not None
        try:
            before = checkins.store.find(record.title)
        except StoreError:
            before = None
        if before is not None:
            return ToolResult(
                ok=True, status="already_checked_in", data=guests.as_dict(found),
                message=f"{record.title} вже відмічений сьогодні о {before['time']} — другий раз пройти не можна. Без "
                "перевірочного питання: ввічливо скажи, що цю людину вже зареєстровано, і порадь самостійно звернутися до команди (покликати когось ти не можеш).",
            )
        if not user.check_question:  # the name on the list is enough: in and marked, no question
            return _let_in(record.title)
        pick = guests.pick_fact(record.title, list(record.fields), user.conversation_id)
        return ToolResult(
            ok=True, status="found", data=guests.as_dict(found),
            message=f"У списку є: {record.title}" + (f" ({found.short_form})" if found.short_form else "") + ". "
            f"Запис (нікому не зачитуй): {record.line()}. Відповідай гостю лише перевірочним питанням — про «{pick}»: "
            "одне відкрите питання, яке не містить відповіді й не підказує її. Не кажи, що людину знайдено, і не "
            "називай прізвища чи чогось іншого із запису до підтвердження. Відповідь гостя звір за змістом і тоді "
            "виклич confirm_guest.",
        )

    def confirm_guest(args: dict, ctx: ToolExecutionContext) -> ToolResult:
        name = " ".join(str(args.get("full_name") or "").split())[:200]
        verified = bool(args.get("verified"))
        if not verified:
            user.guest = {"name": name, "verified": False, "at": time.time()}
            logger.info("web.concierge.guest verified=False")
            return ToolResult(
                ok=True, status="not_verified",
                message="Не підтверджено. Ввічливо скажи, що не вдалося підтвердити, і порадь самостійно звернутися до того, хто запрошував, або до команди (покликати когось ти не можеш).",
            )
        # Only for a person really on the list…
        try:
            on_list = guests.is_on_list(name, _guest_records())
        except knowledge.KnowledgeError:
            on_list = True  # the list can't be read: the check question still guards it
        if not on_list:
            logger.info("web.concierge.confirm_refused why=not_on_list")
            return ToolResult(
                ok=False, status="not_on_list",
                message="Такої людини у списку гостей немає — підтвердити не можна. Порадь звернутися до того, хто "
                "запрошував, або до когось із команди.",
            )
        # …and only with the guest's own answer to the check question, heard in this call — not their name
        # (unless the page turned the check question off).
        problem = (checkins.answer_problem(name, str(args.get("guest_answer") or ""), ctx.user_utterances or [])
                   if user.check_question else None)
        if problem:
            logger.info("web.concierge.confirm_refused why=%s", problem)
            return ToolResult(
                ok=False, status="needs_check_question",
                message="Ще рано підтверджувати: спершу постав одне перевірочне питання про факт із запису цієї "
                "людини і дочекайся її відповіді; guest_answer — її власні слова у відповідь (не ім'я).",
            )
        return _let_in(name)

    def _let_in(name: str) -> ToolResult:
        """Mark the guest as in (once a day) and tell her to welcome them — or that they came already."""
        try:
            before = checkins.store.find(name)
            if before is None:
                checkins.store.add(name, user.client_id[:8])
        except StoreError:
            before = None  # the mark can't be read: let them in rather than stop everyone
        if before is not None:
            user.guest = {"name": name, "verified": False, "duplicate": True, "time": before["time"], "at": time.time()}
            logger.info("web.concierge.guest duplicate")
            return ToolResult(
                ok=True, status="already_checked_in",
                message=f"{name} вже відмічений сьогодні о {before['time']} — другий раз пройти не можна. Ввічливо "
                "скажи, що цю людину вже зареєстровано, і порадь самостійно звернутися до команди (покликати когось ти не можеш). "
                "Не вітай як гостя.",
            )
        user.guest = {"name": name, "verified": True, "at": time.time()}
        logger.info("web.concierge.guest verified=True")
        return ToolResult(
            ok=True, status="ok",
            message=f"Гостя підтверджено й відмічено: {name}. Коротко привітай на ім'я і скажи «Проходьте, будь ласка» — без питань і розпитувань.",
        )

    executor.register("set_assistant_name", set_name)
    executor.register("change_language", change_language)
    executor.register("set_voice_style", set_voice_style)
    executor.register("end_conversation", end_conversation)
    executor.register("web_search", web_search, run_in_thread=True)  # network: off the event loop
    executor.register("look_at_camera", look_at_camera, run_in_thread=True)  # may wait for a frame
    executor.register("knowledge_lookup", knowledge_lookup, run_in_thread=True)  # database
    executor.register("remember_person", remember_person, run_in_thread=True)  # database
    executor.register("find_guest", find_guest, run_in_thread=True)  # database
    executor.register("confirm_guest", confirm_guest, run_in_thread=True)
    return executor


class WebUserRegistry:
    def __init__(self) -> None:
        self._users: dict[str, WebUser] = {}
        self._lock = threading.Lock()

    def get(self, client_id: str, owner: str | None = None) -> WebUser:
        """The browser's state; `owner` = the logged-in account's Account.owner (None: the browser's own)."""
        owner = owner or client_id
        with self._lock:
            user = self._users.get(client_id)
            created = user is None
            if user is None:
                self._evict_idle()
                user = WebUser(client_id=client_id, owner=owner)
                self._users[client_id] = user
                logger.info("web.user.created users=%s", len(self._users))
            user.last_seen = time.time()
            switched = not created and user.owner != owner
            in_use = self._open_elsewhere(owner, client_id) if created or switched else frozenset()
        # Outside the lock: the database may take a moment.
        if created:
            user.restore_conversation(in_use)
        elif switched:
            user.switch_owner(owner, in_use)
        return user

    def peek(self, client_id: str) -> WebUser | None:
        """The browser's state if the server has it now — nothing is created or restored."""
        with self._lock:
            return self._users.get(client_id)

    def _open_elsewhere(self, owner: str, client_id: str) -> frozenset[str]:
        """Conversations of this owner current in its other browsers (seen within the hour)."""
        now = time.time()
        return frozenset(u.conversation_id for cid, u in self._users.items()
                         if cid != client_id and u.owner == owner and now - u.last_seen < 3600)

    def save_all(self) -> None:
        """Server shutting down (a deploy): write the conversations still waiting to be saved."""
        with self._lock:
            pending = [u for u in self._users.values() if u._save_timer is not None]
        for user in pending:
            user.save_conversation()

    def _evict_idle(self) -> None:
        now = time.time()
        stale = [cid for cid, u in self._users.items() if now - u.last_seen > _IDLE_EVICT_S and not u.bridges]
        for cid in stale:
            self._forget(cid)
        while len(self._users) >= _MAX_USERS:
            self._forget(min(self._users.values(), key=lambda u: u.last_seen).client_id)

    def _forget(self, client_id: str) -> None:
        user = self._users.pop(client_id)
        if user._save_timer is not None:  # not written yet: now, before it is gone
            user.save_conversation()
