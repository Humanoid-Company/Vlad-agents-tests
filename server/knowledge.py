"""Team knowledge: sets of records (people with their age, hobbies, favourite films…; an event's facts)
uploaded on the page «Знання» (web/knowledge.html) and looked up by Єва with knowledge_lookup.

One store for the whole team (everyone with the access code), not per browser: «Корпоратив 20.10» is
uploaded once and every call knows it.

Postgres (Neon) when DATABASE_URL is set (server/db.py). Without it, a SQLite file (KNOWLEDGE_SQLITE,
data/knowledge.db): fine locally, but on Render every deploy wipes it — /api/knowledge says so and the page
shows it.
"""
from __future__ import annotations

import difflib
import functools
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from server.db import SqlStore, StoreError

logger = logging.getLogger(__name__)

MAX_ITEMS_PER_SET = 2000
MAX_FIELDS_PER_ITEM = 40
MAX_VALUE_CHARS = 1000
MAX_TITLE_CHARS = 200
MAX_SET_NAME_CHARS = 120
MAX_NOTE_CHARS = 1000
# A set is either records (people, from a table or a document) or one text under its own heading («Правила»,
# «Про подію», «Історії»…), written on the page.
KINDS = ("records", "text")
MAX_BODY_CHARS = 20_000
# «Завжди пам'ятати»: the pinned sets go into the instructions of every call, not through knowledge_lookup —
# rules she must keep in every reply, the event's basics. All of them together up to this many characters
# (GPT-Live reads its instructions each turn: a long block slows her and blurs the rest of the prompt).
PINNED_MAX_CHARS = 12_000
# A text set looked up (not pinned) is split at its markdown headings: one record per section, so the
# closest sections come back when everything doesn't fit.
_HEADING = re.compile(r"^#{1,4}\s+(.+?)\s*#*\s*$", re.M)
# Everything enabled goes to the backend model at once while it fits (it matches names and answers
# «хто з них любить Тарантіно» better than any search); above this, only the closest records.
LOOKUP_ALL_CHARS = 16_000
LOOKUP_TOP = 40
# What Єва writes down herself in conversations (remember()): one set of her own. The concierge's guest list
# never reads it (server/web_users.py) — whoever she heard about must not become a guest.
MEMORY_SET = "Єва запам'ятала"
# Every other character writes down into a set of its own («Пам'ять: Степан»): what one robot learned, another
# does not know. Єва keeps the old name, so what she has written so far stays hers.
MEMORY_PREFIX = "Пам'ять: "


def is_memory(name: str) -> bool:
    """A set a character wrote itself (Єва's or another's), not one the team uploaded."""
    return name == MEMORY_SET or name.startswith(MEMORY_PREFIX)
# The enabled records, kept between lookups: Neon wakes in ~0.5 s after a pause, and a call asks often.
SNAPSHOT_TTL_S = 60.0

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS kb_sets (
            id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', enabled BOOLEAN NOT NULL DEFAULT TRUE,
            item_count INTEGER NOT NULL DEFAULT 0, created_at DOUBLE PRECISION NOT NULL,
            owner_id BIGINT, owner_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'records',
            body TEXT NOT NULL DEFAULT '', pinned BOOLEAN NOT NULL DEFAULT FALSE)""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id BIGSERIAL PRIMARY KEY, set_id BIGINT NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS kb_sets (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
            item_count INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
            owner_id INTEGER, owner_name TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'records',
            body TEXT NOT NULL DEFAULT '', pinned INTEGER NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS kb_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, set_id INTEGER NOT NULL REFERENCES kb_sets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL, title TEXT NOT NULL, fields TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS kb_items_set ON kb_items(set_id, position)",
    ],
}


# Kept under its old name: the tool and the endpoints catch it.
KnowledgeError = StoreError


def sees(character: str | None, set_character: str) -> bool:
    """Does `character` know a set made for `set_character`? A set for nobody in particular ("") is for all;
    character=None means no character is asked for (the team's own tools, tests): every set."""
    return character is None or not set_character or set_character == character


@dataclass
class Record:
    set_name: str
    title: str
    fields: dict[str, str]
    character: str = ""  # whose set it is: "" = everyone's

    def line(self) -> str:
        facts = "; ".join(f"{k}: {v}" for k, v in self.fields.items())
        return f"{self.title} — {facts}" if facts else self.title

    # Its words, split once and kept (records stay in the snapshot between lookups).
    @functools.cached_property
    def title_words(self) -> frozenset[str]:
        return frozenset(_words(self.title))

    @functools.cached_property
    def fact_words(self) -> frozenset[str]:
        return frozenset(_words(" ".join(f"{k} {v}" for k, v in self.fields.items())))


def clean_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Records as the page sent them → titles and string fields within the limits; empty ones dropped."""
    out: list[dict[str, Any]] = []
    for item in items[:MAX_ITEMS_PER_SET]:
        title = " ".join(str(item.get("title") or "").split())[:MAX_TITLE_CHARS]
        fields: dict[str, str] = {}
        for key, value in list((item.get("fields") or {}).items())[:MAX_FIELDS_PER_ITEM]:
            key = " ".join(str(key).split())[:80]
            value = " ".join(str(value if value is not None else "").split())[:MAX_VALUE_CHARS]
            if key and value:
                fields[key] = value
        if title or fields:
            out.append({"title": title or next(iter(fields.values())), "fields": fields})
    return out


def text_sections(name: str, body: str) -> list[Record]:
    """A text set → one record per markdown section («## Правила» … ), the set's name for the text before
    the first heading. No headings: the whole text is one record."""
    records: list[Record] = []
    marks = list(_HEADING.finditer(body))
    starts = [(name, 0)] + [(m.group(1).strip(), m.end()) for m in marks]
    ends = [m.start() for m in marks] + [len(body)]
    for (title, start), end in zip(starts, ends, strict=True):
        text = body[start:end].strip()
        if text:
            records.append(Record(name, title, {"Текст": " ".join(text.split())}))
    return records


class KnowledgeStore(SqlStore):
    SCHEMA = _SCHEMA
    # Who uploaded a set (an account's id and name): they and admins may delete it. Sets from before have none.
    # kind / body / pinned: text sets and «Завжди пам'ятати» (a database from before gets them as records).
    ADDED_COLUMNS = [
        ("kb_sets", "owner_id", {"postgres": "BIGINT", "sqlite": "INTEGER"}),
        ("kb_sets", "owner_name", {"postgres": "TEXT NOT NULL DEFAULT ''", "sqlite": "TEXT NOT NULL DEFAULT ''"}),
        ("kb_sets", "kind", {"postgres": "TEXT NOT NULL DEFAULT 'records'", "sqlite": "TEXT NOT NULL DEFAULT 'records'"}),
        ("kb_sets", "body", {"postgres": "TEXT NOT NULL DEFAULT ''", "sqlite": "TEXT NOT NULL DEFAULT ''"}),
        ("kb_sets", "pinned", {"postgres": "BOOLEAN NOT NULL DEFAULT FALSE", "sqlite": "INTEGER NOT NULL DEFAULT 0"}),
        # Who the set is for: a character's id (the picker's tab); "" = everyone. Sets from before are everyone's.
        ("kb_sets", "character", {"postgres": "TEXT NOT NULL DEFAULT ''", "sqlite": "TEXT NOT NULL DEFAULT ''"}),
    ]
    _SET_COLS = ("id, name, note, source, enabled, item_count, created_at, owner_id, owner_name, kind, body, pinned, "
                 "character")
    UNAVAILABLE = "База знань недоступна"
    FAILED = "Помилка бази знань"

    def __init__(self, database_url: str = "", sqlite_path: str = "data/knowledge.db") -> None:
        super().__init__(database_url, sqlite_path)
        self._snapshot: list[Record] | None = None
        self._sets_summary: list[dict[str, Any]] = []
        self._texts: list[Record] = []
        self._looked_up: list[Record] = []
        self._pinned_parts: list[tuple[str, str]] = []
        self._snapshot_at = 0.0

    def _changed(self) -> None:
        self._snapshot = None

    # ── sets ──
    def list_sets(self) -> list[dict[str, Any]]:
        """Every set without its text (the page opens one to read it): «chars» says how long it is."""
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT {self._SET_COLS} FROM kb_sets ORDER BY created_at DESC, id DESC"
            ).fetchall()
        sets = [self._set_row(r) for r in rows]
        for s in sets:
            del s["body"]
        return sets

    @staticmethod
    def _set_row(r: Any) -> dict[str, Any]:
        kind = r[9] if r[9] in KINDS else "records"
        body = r[10] or ""
        return {"id": int(r[0]), "name": r[1], "note": r[2], "source": r[3], "enabled": bool(r[4]),
                "count": int(r[5]), "created_at": float(r[6]),
                "owner_id": int(r[7]) if r[7] is not None else None, "owner_name": r[8] or "",
                "kind": kind, "body": body, "chars": len(body), "pinned": bool(r[11]), "character": r[12] or ""}

    def pinned_chars(self) -> int:
        """How much of «Завжди пам'ятати» goes into every call now (the page shows it against the limit)."""
        return len(self.pinned_block())

    def get_set(self, set_id: int) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                self._q(f"SELECT {self._SET_COLS} FROM kb_sets WHERE id = %s"),
                (set_id,),
            ).fetchone()
            if row is None:
                return None
            items = conn.execute(
                self._q("SELECT title, fields FROM kb_items WHERE set_id = %s ORDER BY position"), (set_id,)
            ).fetchall()
        data = self._set_row(row)
        data["items"] = [{"title": t, "fields": json.loads(f)} for t, f in items]
        return data

    def create_set(self, name: str, items: list[dict[str, Any]], *, note: str = "", source: str = "",
                   owner_id: int | None = None, owner_name: str = "", kind: str = "records", body: str = "",
                   pinned: bool = False, character: str = "") -> dict[str, Any]:
        """A set of records (items), or with kind="text" one text under the name (body; items are ignored).
        `character`: the one it is for ("" = every character knows it)."""
        name = " ".join(name.split())[:MAX_SET_NAME_CHARS]
        if kind not in KINDS:
            raise KnowledgeError("Невідомий тип розділу.")
        if is_memory(name):
            raise KnowledgeError("Ця назва зайнята: так персонажі називають те, що записують самі.")
        body = _clean_body(body) if kind == "text" else ""
        items = clean_items(items) if kind == "records" else []
        if not name:
            raise KnowledgeError("Дайте набору назву.")
        if kind == "records" and not items:
            raise KnowledgeError("У наборі немає жодного запису.")
        if kind == "text" and not body:
            raise KnowledgeError("Розділ порожній: напишіть текст.")
        with self._conn() as conn:
            row = conn.execute(
                self._q("INSERT INTO kb_sets (name, note, source, enabled, item_count, created_at, owner_id, owner_name, "
                        "kind, body, pinned, character) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"),
                (name, note.strip()[:MAX_NOTE_CHARS], source.strip()[:200], True, len(items), time.time(),
                 owner_id, " ".join(owner_name.split())[:120], kind, body, bool(pinned), character.strip().lower()[:40]),
            ).fetchone()
            set_id = int(row[0])
            rows = [(set_id, i, it["title"], json.dumps(it["fields"], ensure_ascii=False)) for i, it in enumerate(items)]
            sql = self._q("INSERT INTO kb_items (set_id, position, title, fields) VALUES (%s, %s, %s, %s)")
            if rows and self.kind == "postgres":
                with conn.cursor() as cur:
                    cur.executemany(sql, rows)
            elif rows:
                conn.executemany(sql, rows)
        self._changed()
        logger.info("knowledge.set_created id=%s set_kind=%s items=%s chars=%s kind=%s",
                    set_id, kind, len(items), len(body), self.kind)
        created = self.get_set(set_id)
        assert created is not None
        return created

    def update_set(self, set_id: int, *, enabled: bool | None = None, name: str | None = None,
                   note: str | None = None, pinned: bool | None = None,
                   body: str | None = None, character: str | None = None) -> dict[str, Any] | None:
        """Switch a set on or off, pin it («Завжди пам'ятати»), rename it, change its note — or a text set's text."""
        current = self.get_set(set_id)
        if current is None:
            return None
        changes: list[tuple[str, Any]] = []
        if enabled is not None:
            changes.append(("enabled", bool(enabled)))
        if pinned is not None:
            if is_memory(current["name"]):
                raise KnowledgeError("Записи персонажа не закріплюються: він знаходить їх сам, коли треба.")
            changes.append(("pinned", bool(pinned)))
        if name is not None and " ".join(name.split()):
            new_name = " ".join(name.split())[:MAX_SET_NAME_CHARS]
            if (is_memory(new_name) or is_memory(current["name"])) and new_name != current["name"]:
                raise KnowledgeError("Цей набір не перейменовується: так персонажі називають те, що записують самі.")
            changes.append(("name", new_name))
        if note is not None:
            changes.append(("note", note.strip()[:MAX_NOTE_CHARS]))
        if character is not None:
            changes.append(("character", character.strip().lower()[:40]))
        if body is not None:
            if current["kind"] != "text":
                raise KnowledgeError("Текст змінюється лише в текстовому розділі.")
            body = _clean_body(body)
            if not body:
                raise KnowledgeError("Розділ порожній: напишіть текст або видаліть розділ.")
            changes.append(("body", body))
        if changes:
            with self._conn() as conn:
                conn.execute(
                    self._q("UPDATE kb_sets SET " + ", ".join(f"{col} = %s" for col, _ in changes) + " WHERE id = %s"),
                    (*[v for _, v in changes], set_id),
                )
            self._changed()
        return self.get_set(set_id)

    def delete_set(self, set_id: int) -> bool:
        with self._conn() as conn:
            conn.execute(self._q("DELETE FROM kb_items WHERE set_id = %s"), (set_id,))
            gone = conn.execute(self._q("DELETE FROM kb_sets WHERE id = %s"), (set_id,)).rowcount
        self._changed()
        return bool(gone)

    # ── what Єва writes down herself ──
    def remember(self, name: str, fields: dict[str, str], memory: str = MEMORY_SET) -> dict[str, Any]:
        """Add facts about a person to Єва's own set: a known person (same name, or the name is part of the
        stored one — «Олег» / «Олег Петренко») gets them written after what is there, a new one gets a record."""
        cleaned = clean_items([{"title": name, "fields": fields}])
        if not cleaned or not cleaned[0]["fields"] or not " ".join(name.split()):
            raise KnowledgeError("Нічого запам'ятовувати: потрібні ім'я людини і хоч один факт.")
        title, new = cleaned[0]["title"], cleaned[0]["fields"]
        with self._conn() as conn:
            row = conn.execute(self._q("SELECT id FROM kb_sets WHERE name = %s"), (memory,)).fetchone()
            if row:
                set_id = int(row[0])
            else:
                set_id = int(conn.execute(
                    self._q("INSERT INTO kb_sets (name, note, source, enabled, item_count, created_at, owner_id, "
                            "owner_name) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id"),
                    (memory, "Те, що персонаж записав зі слів співрозмовників.", "", True, 0, time.time(), None, ""),
                ).fetchone()[0])
            items = conn.execute(
                self._q("SELECT id, title, fields, position FROM kb_items WHERE set_id = %s ORDER BY position"),
                (set_id,),
            ).fetchall()
            known = _same_person(title, [(int(i[0]), i[1]) for i in items])
            if known is not None:
                old = next(i for i in items if int(i[0]) == known)
                merged = _merge_fields(json.loads(old[2]), new)
                conn.execute(self._q("UPDATE kb_items SET fields = %s WHERE id = %s"),
                             (json.dumps(merged, ensure_ascii=False), known))
                person, added = old[1], False
            else:
                merged, person, added = new, title, True
                position = max((int(i[3]) for i in items), default=-1) + 1
                conn.execute(self._q("INSERT INTO kb_items (set_id, position, title, fields) VALUES (%s, %s, %s, %s)"),
                             (set_id, position, title, json.dumps(merged, ensure_ascii=False)))
                conn.execute(self._q("UPDATE kb_sets SET item_count = item_count + 1 WHERE id = %s"), (set_id,))
        self._changed()
        logger.info("knowledge.remembered new=%s fields=%s", added, len(new))
        return {"person": person, "new": added, "fields": merged}

    # ── what Єва knows ──
    def _load(self) -> None:
        """The enabled sets, cached for SNAPSHOT_TTL_S: people's records, the text sections to look up, and
        the pinned block for the instructions."""
        if self._snapshot is not None and time.monotonic() - self._snapshot_at < SNAPSHOT_TTL_S:
            return
        with self._conn() as conn:
            sets = conn.execute(
                self._q("SELECT id, name, note, item_count, kind, body, pinned, character FROM kb_sets WHERE enabled = %s "
                        "ORDER BY created_at, id"),
                (True,),
            ).fetchall()
            rows = conn.execute(
                self._q("SELECT s.id, i.title, i.fields FROM kb_items i JOIN kb_sets s ON s.id = i.set_id "
                        "WHERE s.enabled = %s ORDER BY s.created_at, s.id, i.position"),
                (True,),
            ).fetchall()
        names = {int(s[0]): (s[1], s[7] or "") for s in sets}
        records = [Record(names[int(sid)][0], title, json.loads(fields), names[int(sid)][1])
                   for sid, title, fields in rows if int(sid) in names]
        texts: list[Record] = []
        pinned: list[tuple[str, str]] = []  # (whose, its text)
        summary: list[dict[str, Any]] = []
        for _set_id, name, note, count, kind, body, is_pinned, character in sets:
            character = character or ""
            own = [r for r in records if r.set_name == name]
            if kind == "text":
                sections = text_sections(name, body or "")
                for section in sections:
                    section.character = character
                if is_pinned:
                    # its own headings one level under the set's name
                    pinned.append((character, f"## {name}\n"
                                   + re.sub(r"^(#{1,4})\s", r"#\1 ", (body or "").strip(), flags=re.M)))
                else:
                    texts.extend(sections)
                    summary.append({"name": name, "note": note, "count": len(sections), "kind": "text",
                                    "titles": [r.title for r in sections][:25], "character": character})
                continue
            if is_pinned and not is_memory(name):
                pinned.append((character, f"## {name}\n" + "\n".join(r.line() for r in own)))
                continue
            summary.append({"name": name, "note": note, "count": int(count), "kind": "records",
                            "titles": [r.title for r in own][:25], "character": character})
        pinned_names = {s[1] for s in sets if s[6] and s[4] != "text" and not is_memory(s[1])}
        self._sets_summary, self._snapshot, self._texts = summary, records, texts
        self._looked_up = [r for r in records if r.set_name not in pinned_names]
        self._pinned_parts, self._snapshot_at = pinned, time.monotonic()

    def enabled_records(self, character: str | None = None) -> tuple[list[dict[str, Any]], list[Record]]:
        """The enabled record sets (name, note, count, a few titles) and all their people, pinned or not — the
        concierge's guest list (of the character at the door: a set made for another one is not its list).
        Text sets are not people: never in here."""
        self._load()
        return ([s for s in self._sets_summary if s["kind"] == "records" and sees(character, s["character"])],
                [r for r in self._snapshot or [] if sees(character, r.character)])

    def pinned_block(self, character: str | None = None) -> str:
        """«Завжди пам'ятати»: the pinned sets as one text («## назва» + its text or records), for the prompts —
        the ones for everyone and for this character."""
        self._load()
        block = "\n\n".join(text for whose, text in self._pinned_parts if sees(character, whose))
        if len(block) > PINNED_MAX_CHARS:
            logger.warning("knowledge.pinned_too_long chars=%s max=%s", len(block), PINNED_MAX_CHARS)
            block = block[:PINNED_MAX_CHARS].rsplit("\n", 1)[0] + "\n[…далі обрізано: забагато в «Завжди пам'ятати»]"
        return block

    def lookup(self, query: str, set_name: str = "", *, with_memory: bool = True,
               memory: str = MEMORY_SET, character: str | None = None) -> dict[str, Any]:
        """Records for the backend model: everything enabled while it fits, else the closest ones. Text sets
        come as their sections; pinned sets are left out (they are in the instructions already).
        A character sees only its own memory set (`memory`): what another wrote down is not its knowledge.
        with_memory=False leaves out every written-down set (the concierge never shows them).
        A set uploaded for one character is known to that character only (`character`)."""
        self._load()
        sets, records = self._sets_summary, [*self._looked_up, *self._texts]
        own = memory if with_memory else None
        sets = [s for s in sets if (not is_memory(s["name"]) or s["name"] == own) and sees(character, s["character"])]
        records = [r for r in records if (not is_memory(r.set_name) or r.set_name == own) and sees(character, r.character)]
        if set_name.strip():
            wanted = _best_set(set_name, [s["name"] for s in sets])
            if wanted:
                records = [r for r in records if r.set_name == wanted]
        total = len(records)
        text = "\n".join(f"[{r.set_name}] {r.line()}" for r in records)
        if len(text) <= LOOKUP_ALL_CHARS:
            return {"sets": sets, "total": total, "shown": total, "text": text}
        scored = _closest(query, records, LOOKUP_TOP)
        text = "\n".join(f"[{r.set_name}] {r.line()}" for r in scored)
        return {"sets": sets, "total": total, "shown": len(scored), "text": text[:LOOKUP_ALL_CHARS]}


def _clean_body(body: str) -> str:
    """A text set's text: line breaks kept (headings, lists), trailing spaces and runs of empty lines gone."""
    lines = [line.rstrip() for line in str(body or "").replace("\r\n", "\n").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()[:MAX_BODY_CHARS]


# ── loose matching: names come from speech recognition, declined («Олегові») or misspelt («Алєг») ──
_WORD = re.compile(r"[\w'’ʼ-]+", re.UNICODE)


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е").replace("’", "'").replace("ʼ", "'")


def _words(text: str) -> list[str]:
    return [w for w in _WORD.findall(_norm(text)) if len(w) >= 3]


def _close(a: str, b: str) -> bool:
    if a[:4] == b[:4] and min(len(a), len(b)) >= 4:  # same stem: Олег / Олегові / Олега
        return True
    m = difflib.SequenceMatcher(None, a, b)
    # quick_ratio is an upper bound of ratio: most pairs are out before the slow exact one.
    return m.real_quick_ratio() >= 0.75 and m.quick_ratio() >= 0.75 and m.ratio() >= 0.75


def _matches(query_words: list[str], vocabulary: set[str]) -> list[frozenset[str]]:
    """For each query word, the words of the records it matches — each pair compared once, not once per
    record («хобі», «посада» are in every record): 2000 people took 2 s a lookup, compared record by record."""
    return [frozenset(t for t in vocabulary if _close(w, t)) for w in query_words]


def _score_with(matched: list[frozenset[str]], record: Record) -> float:
    score = 0.0
    for m in matched:
        if m & record.title_words:
            score += 3
        elif m & record.fact_words:
            score += 1
    return score


def _score(query: str, record: Record) -> float:
    q = _words(query)
    return _score_with(_matches(q, set(record.title_words | record.fact_words)), record) if q else 0.0


def _closest(query: str, records: list[Record], top: int) -> list[Record]:
    q = _words(query)
    if not q:
        return records[:top]
    vocabulary: set[str] = set()
    for r in records:
        vocabulary |= r.title_words
        vocabulary |= r.fact_words
    matched = _matches(q, vocabulary)
    return sorted(records, key=lambda r: -_score_with(matched, r))[:top]


def _same_person(title: str, existing: list[tuple[int, str]]) -> int | None:
    """The stored record this name means: the same words, or — when only one record fits — a name that is part
    of the stored one («Олег» → «Олег Петренко»). Two such records: none (a new one, never a wrong merge)."""
    wanted = set(_words(title))
    if not wanted:
        return None
    for item_id, stored in existing:
        if set(_words(stored)) == wanted:
            return item_id
    partial = [item_id for item_id, stored in existing
               if wanted <= set(_words(stored)) or set(_words(stored)) <= wanted]
    return partial[0] if len(partial) == 1 else None


def _merge_fields(old: dict[str, str], new: dict[str, str]) -> dict[str, str]:
    """New facts go after the old ones under the same name («Хобі: шахи; риболовля»), nothing is lost."""
    merged = dict(old)
    for key, value in new.items():
        if key in merged and _norm(value) not in _norm(merged[key]):
            merged[key] = f"{merged[key]}; {value}"[:MAX_VALUE_CHARS]
        elif key not in merged and len(merged) < MAX_FIELDS_PER_ITEM:
            merged[key] = value
    return merged


def _best_set(name: str, names: list[str]) -> str | None:
    n = _norm(name)
    for candidate in names:
        if n in _norm(candidate) or _norm(candidate) in n:
            return candidate
    best = difflib.get_close_matches(n, [_norm(c) for c in names], n=1, cutoff=0.5)
    return next((c for c in names if _norm(c) == best[0]), None) if best else None


# ── prompts ──
BACKEND_NOTE = (
    "\nTEAM KNOWLEDGE: knowledge_lookup returns the records the team uploaded (people with their age, "
    "hobbies, favourite films and so on; an event's facts). Call it for any question about a person or "
    "event from them, and for «хто з них…», «кому…», «що подарувати…» — the query is the name or words as "
    "heard (names come from speech recognition: declined or misspelt; the tool matches loosely, and you "
    "match the right person yourself). Then answer only from the records: the concrete values, one to "
    "three spoken sentences. For «хто з них…» go through every record and name everyone who fits, by "
    "meaning, not only by the words: a film counts for its director («Кримінальне чтиво» is Tarantino's), "
    "a diet for «не їсть м'ясо», a hobby for «спортивний». If the records lack it, say plainly "
    "that it isn't in what the team gave — never invent. Two people with a close name: ask which one.\n"
)

# The assistant's (not the concierge's) way with people: she recalls them like an acquaintance, in her own
# character (each voice's backend_note carries the manner), and writes down what she is told.
PEOPLE_NOTE = (
    "\nPEOPLE YOU KNOW: knowledge_lookup holds what you know about people and events (age, hobbies, films, "
    "what they do — uploaded by the team and written down by you earlier). «Ти знаєш Олега?», «що ти "
    "пам'ятаєш про Марину?», «хто це?» — call it with the name as heard (names come from speech "
    "recognition: declined or misspelt; you match the right person yourself) and answer as if you "
    "remember the person yourself, in your own character and manner — «о, так, пам'ятаю…», «ага, це той, "
    "що…» — with the concrete facts from the records, one to three spoken sentences, never «у базі "
    "написано», never a list read out. No record — say honestly that you don't recall them; never "
    "invent. Two people with a close name: ask which one. This is a talk between acquaintances: no "
    "check question, no check-in, no letting anyone in.\n"
    "THE PERSON YOU TALK TO: someone says who they are («я Влад», «мене звати Владислав»), or asks «пам'ятаєш "
    "мене?», «хто я?», «що ти про мене знаєш?» — look up the name they gave in this conversation (no name yet: "
    "reply that you need it, and ask). A short or pet form matches the full name («Влад» — Владислав, «Катя» — "
    "Катерина): one fitting record — greet them as an old acquaintance in your manner («О, Владе! Пам'ятаю тебе…») "
    "and say one or two facts from it; several fit — ask the surname; none — say you don't recall them yet and "
    "offer to remember what they tell about themselves. Never say «не впізнаю» or «не знаю» without a lookup.\n"
    "AN OLD FRIEND: when you know the person (found in the records, or told to you earlier in this talk), talk to "
    "them as to a friend you have known for years and not seen for a while: warm, on «ти», familiar, in your own "
    "manner. Bring their facts up as part of the talk, the way a friend remembers («ти ж, здається, шахи любиш — "
    "ще граєш?», «ну що, подивився той фільм, про який ти розказував?»): one or two facts per reply, never the "
    "whole record at once and never as a list. Ask about their life, remember what they say next, use their name. "
    "Never invent a shared past — no meetings, places or events the records don't hold; you remember what you "
    "were told, and the rest you are curious about.\n"
    "WRITING DOWN: when the user tells you something about a person to keep («запам'ятай, що Олег "
    "любить Тарантіно», «це моя сестра Іра, їй 30») or asks you to add it, call remember_person(name, "
    "facts) — name as the user said it (add the surname if they gave it), facts a short object like "
    "{\"Хобі\": \"шахи\", \"Вік\": \"30\"}; several facts in one call. Then confirm in a few words in your "
    "character, and bring it up later when it fits. Write down only what the user said about a person "
    "and wanted kept: never guess, never save passwords, bank data or health details, never save "
    "unasked what you merely overheard.\n"
)


# The team's store. Tests swap it for a temporary one (monkeypatch server.knowledge.store).
store = KnowledgeStore(os.getenv("DATABASE_URL", ""), os.getenv("KNOWLEDGE_SQLITE", "data/knowledge.db"))


def pinned_note(*, backend: bool = False, character: str | None = None) -> str:
    """«Завжди пам'ятати» for the instructions of a call: the voice (GPT-Live) keeps its rules in every reply
    and knows the basics without a lookup; the backend gets the same so what it writes keeps them too. Empty
    when nothing is pinned or the store is down (a call never fails for it)."""
    try:
        block = store.pinned_block(character)
    except StoreError:
        logger.warning("knowledge.pinned_unavailable")
        return ""
    if not block:
        return ""
    who = ("what you write for the voice must keep them too" if backend
           else "keep them in every reply")
    return (
        "\n\nTHE TEAM'S STANDING KNOWLEDGE (the page «Знання» → «Завжди пам'ятати»): the setting of these calls — "
        f"facts, roles and rules the team wrote. They are in force for the whole call: {who}. Where they differ "
        "from the general rules above (how to address people, what to talk about, how long to speak), they win. "
        "Use the facts naturally, never read them out as a list, never say the headings, «у базі» or that it was "
        "written for you; anything not here — look it up or say you haven't been told.\n"
        + block + "\n"
    )


def concierge_backend_note(check_question: bool = True) -> str:
    """The backend's protocol at the door; without the check question (the page's switch) find_guest lets the
    one person in itself."""
    return CONCIERGE_BACKEND_NOTE if check_question else CONCIERGE_NO_CHECK_BACKEND_NOTE


# «Консьєрж» (web, the page's top switch): she has her own prompt (prompts/live_prompt.py
# build_concierge_prompt); the backend checks people against these records with this protocol.
CONCIERGE_BACKEND_NOTE = (
    "\nCONCIERGE: you check people against the team's guest list for a voice concierge; she says your reply. "
    "A name someone gives — first name, surname, patronymic or the full name, in any order: call "
    "find_guest(name, surname) with everything they said (the full name may go whole into name) — never decide "
    "from knowledge_lookup or from memory; it matches first name, surname and full name, short forms («Катя» — "
    "Катерина), case endings and near spellings itself. Reply as its result says: not on the list — say you can't find this name among the "
    "guests and ask them to repeat it or add the surname; still not there — suggest they contact the person "
    "who invited them or someone from the team, and do not let them in; several with the name — ask the "
    "surname without naming anyone; already registered — say so, no check question; one person — ONE open "
    "check question about the fact it names, which never contains or hints at the answer («Нагадайте, ким "
    "ви у нас працюєте?», «Як звати вашого улюбленця?», «Яке у вас хобі?»). A name with the surname is never "
    "enough: the check question always comes first. Their answer: compare it by meaning with that fact in "
    "the record (other words for the same thing, a part of a list is enough). It matches → confirm_guest("
    "full_name exactly as in the record, verified=true, guest_answer=their own words) and reply as its result "
    "says: a short welcome by first name and «Проходьте, будь ласка» — no question, no small talk. It does not "
    "→ one more check question about another fact from the record; a second miss → confirm_guest(verified="
    "false) and reply that you could not confirm it, politely. Never reveal anything from a record before "
    "confirmation. Questions about the place or the event: knowledge_lookup, answer only from the records "
    "(an event's notes); not there — say you don't know and advise them to ask the team on the spot; never "
    "invent places, directions or times. The concierge cannot call, notify or connect anyone and you have no "
    "tool for it: never write that someone will come, will be called or will be connected — if the guest "
    "needs a person, tell them to turn to the one who invited them or to the team themselves.\n"
)

CONCIERGE_NO_CHECK_BACKEND_NOTE = (
    "\nCONCIERGE: you check people against the team's guest list for a voice concierge; she says your reply. "
    "There is NO check question at this door: the name on the list is enough. A name someone gives — first "
    "name, surname, patronymic or the full name, in any order: call find_guest(name, surname) with everything "
    "they said (the full name may go whole into name) — never decide from knowledge_lookup or from memory; "
    "it matches first name, surname and full name, short forms («Катя» — Катерина), case endings and near "
    "spellings itself, and lets the one "
    "person in and marks them itself. Reply as its result says: not on the list — say you can't find this name "
    "among the guests and ask them to repeat it or add the surname; still not there — suggest they contact the "
    "person who invited them or someone from the team, and do not let them in; several with the name — ask the "
    "surname without naming anyone; already registered — say so; let in — a short welcome by first name and "
    "«Проходьте, будь ласка», no question, no small talk. Never ask a check question and never call "
    "confirm_guest. Never reveal anything from a record. Questions about the place or the event: "
    "knowledge_lookup, answer only from the records (an event's notes); not there — say you don't know and "
    "advise them to ask the team on the spot; never invent places, directions or times. The concierge cannot "
    "call, notify or connect anyone and you have no tool for it: never write that someone will come, will be "
    "called or will be connected — if the guest needs a person, tell them to turn to the one who invited them "
    "or to the team themselves.\n"
)
