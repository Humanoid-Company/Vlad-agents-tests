"""Every web conversation, kept in the database (server/db.py), so neither a page refresh nor a deploy loses it.

A browser (its client id) has one current conversation per character: what that character remembers and what
the page shows after a refresh (each character has a memory of its own — «character»; rows from before have
none and count as Єва's). «Нова розмова» starts another; the old one stays here, listed on the page «Історія», where it can be
read, continued or deleted. A conversation is one row: its turns as JSON, rewritten as it grows (at most a
few hundred turns — voice/conversation.py keeps 400).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from server.db import SqlStore

logger = logging.getLogger(__name__)

TITLE_CHARS = 80
LIST_MAX = 100

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, started_at DOUBLE PRECISION NOT NULL,
            updated_at DOUBLE PRECISION NOT NULL, title TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0, turns TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS conversations_client ON conversations(client_id, updated_at)",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY, client_id TEXT NOT NULL, started_at REAL NOT NULL,
            updated_at REAL NOT NULL, title TEXT NOT NULL DEFAULT '',
            turn_count INTEGER NOT NULL DEFAULT 0, turns TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS conversations_client ON conversations(client_id, updated_at)",
    ],
}


# Sounds GPT-Live marks in its transcript («[hum]», «[breath]»): heard, not read.
_SOUND_TAG = re.compile(r"\[[a-z][a-z _-]*\]\s*", re.I)


def visible(turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """What was said, for the page: no overheard notes, no tool results kept for her memory, no sound tags."""
    shown = []
    for t in turns:
        if t["role"] in ("user", "assistant") and not t["text"].startswith("(Результат "):
            text = _SOUND_TAG.sub("", t["text"]).strip() if t["role"] == "assistant" else t["text"]
            if text:
                shown.append({"role": t["role"], "text": text})
    return shown


def _title(turns: list[dict[str, str]]) -> str:
    """The user's first line; "" while they have said nothing (such a conversation is not listed)."""
    first = next((t for t in visible(turns) if t["role"] == "user"), None)
    text = " ".join(first["text"].split()) if first else ""
    return text if len(text) <= TITLE_CHARS else text[: TITLE_CHARS - 1].rstrip() + "…"


DEFAULT_CHARACTER = "willow"


class ConversationStore(SqlStore):
    SCHEMA = _SCHEMA
    ADDED_COLUMNS = [("conversations", "character", {"postgres": "TEXT NOT NULL DEFAULT ''", "sqlite": "TEXT NOT NULL DEFAULT ''"})]
    UNAVAILABLE = "Історія розмов недоступна"
    FAILED = "Помилка історії розмов"

    def save(self, conv_id: str, client_id: str, started_at: float, turns: list[dict[str, str]],
             character: str = DEFAULT_CHARACTER) -> None:
        """Write the whole conversation (insert or replace). One where the user said nothing yet (a new
        one, a greeting) is kept too — it is the current one after a restart — but not listed."""
        with self._conn() as conn:
            conn.execute(
                self._q(
                    "INSERT INTO conversations (id, client_id, started_at, updated_at, title, turn_count, turns, "
                    "character) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET "
                    "updated_at = excluded.updated_at, title = excluded.title, "
                    "turn_count = excluded.turn_count, turns = excluded.turns, character = excluded.character"
                ),
                (conv_id, client_id, started_at, time.time(), _title(turns), len(visible(turns)),
                 json.dumps(turns, ensure_ascii=False), character),
            )

    @staticmethod
    def _of_character(character: str) -> tuple[str, tuple[str, ...]]:
        """The WHERE part and its arguments for one character's rows (rows from before count as Єва's)."""
        if character == DEFAULT_CHARACTER:
            return "(character = %s OR character = '')", (character,)
        return "character = %s", (character,)

    def latest(self, client_id: str, character: str = DEFAULT_CHARACTER) -> dict[str, Any] | None:
        """The character's most recent conversation in this browser, with its turns (restored after a restart
        and when the character is picked again)."""
        where, args = self._of_character(character)
        with self._conn() as conn:
            row = conn.execute(
                self._q("SELECT id, started_at, updated_at, title, turn_count, turns FROM conversations "
                        f"WHERE client_id = %s AND {where} ORDER BY updated_at DESC LIMIT 1"),
                (client_id, *args),
            ).fetchone()
        return self._row(row, with_turns=True) if row else None

    def list(self, client_id: str, limit: int = LIST_MAX, character: str | None = None) -> list[dict[str, Any]]:
        """Saved conversations, newest first; with `character` only that character's."""
        where, args = self._of_character(character) if character else ("1 = 1", ())
        with self._conn() as conn:
            rows = conn.execute(
                self._q("SELECT id, started_at, updated_at, title, turn_count FROM conversations "
                        f"WHERE client_id = %s AND title <> '' AND {where} ORDER BY updated_at DESC LIMIT %s"),
                (client_id, *args, limit),
            ).fetchall()
        return [self._row(r) for r in rows]

    def get(self, client_id: str, conv_id: str) -> dict[str, Any] | None:
        """One conversation — only the browser's own (another id is «not found»)."""
        with self._conn() as conn:
            row = conn.execute(
                self._q("SELECT id, started_at, updated_at, title, turn_count, turns, character FROM conversations "
                        "WHERE client_id = %s AND id = %s"),
                (client_id, conv_id),
            ).fetchone()
        return self._row(row, with_turns=True) if row else None

    def reassign(self, old_owner: str, new_owner: str) -> int:
        """A login: what this browser said before is the account's from now on (keyed by Account.owner)."""
        with self._conn() as conn:
            return conn.execute(
                self._q("UPDATE conversations SET client_id = %s WHERE client_id = %s"), (new_owner, old_owner)
            ).rowcount

    def delete(self, client_id: str, conv_id: str) -> bool:
        with self._conn() as conn:
            gone = conn.execute(
                self._q("DELETE FROM conversations WHERE client_id = %s AND id = %s"), (client_id, conv_id)
            ).rowcount
        return bool(gone)

    @staticmethod
    def _row(r: Any, *, with_turns: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {"id": r[0], "started_at": float(r[1]), "updated_at": float(r[2]),
                                "title": r[3], "count": int(r[4])}
        if with_turns:
            data["turns"] = json.loads(r[5])
        if len(r) > 6:
            data["character"] = r[6] or DEFAULT_CHARACTER
        return data


# The team's store. Tests swap it for a temporary one (monkeypatch server.conversations.store).
store = ConversationStore(os.getenv("DATABASE_URL", ""), os.getenv("CONVERSATIONS_SQLITE", "data/conversations.db"))
