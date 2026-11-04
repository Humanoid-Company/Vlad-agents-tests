"""The shared songs of the page's «Музика» panel (web/index.html): uploaded once, on every device and for everyone.

The audio itself lives in the database (server/db.py), like «Знання»: Postgres (Neon) when DATABASE_URL is set,
else a SQLite file (SONGS_SQLITE, data/songs.db) that a Render deploy wipes. The list shows no bytes; a song is
streamed by its id (with Range, which Safari needs to play audio).
"""
from __future__ import annotations

import os
import time
import uuid
from typing import Any

from server.db import SqlStore

MAX_SONG_BYTES = 25 * 1024 * 1024
MAX_SONGS = 50
MAX_NAME_CHARS = 120

_SCHEMA = {
    "postgres": [
        """CREATE TABLE IF NOT EXISTS songs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
            created_at DOUBLE PRECISION NOT NULL, data BYTEA NOT NULL)""",
    ],
    "sqlite": [
        """CREATE TABLE IF NOT EXISTS songs (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
            created_at REAL NOT NULL, data BLOB NOT NULL)""",
    ],
}


class SongStore(SqlStore):
    SCHEMA = _SCHEMA
    UNAVAILABLE = "Музика недоступна"
    FAILED = "Помилка збереження музики"

    def list(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT id, name, mime, size FROM songs ORDER BY created_at, name").fetchall()
        return [{"id": r[0], "name": r[1], "mime": r[2], "size": r[3]} for r in rows]

    def count(self) -> int:
        with self._conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM songs").fetchone()[0])

    def add(self, name: str, mime: str, data: bytes) -> dict[str, Any]:
        song_id = uuid.uuid4().hex
        name = name.strip()[:MAX_NAME_CHARS] or "Пісня"
        with self._conn() as conn:
            conn.execute(
                self._q("INSERT INTO songs (id, name, mime, size, created_at, data) VALUES (%s, %s, %s, %s, %s, %s)"),
                (song_id, name, mime, len(data), time.time(), data),
            )
        return {"id": song_id, "name": name, "mime": mime, "size": len(data)}

    def get(self, song_id: str) -> tuple[str, bytes] | None:
        """(mime, bytes) or None."""
        with self._conn() as conn:
            row = conn.execute(self._q("SELECT mime, data FROM songs WHERE id = %s"), (song_id,)).fetchone()
        return (row[0], bytes(row[1])) if row else None

    def delete(self, song_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(self._q("DELETE FROM songs WHERE id = %s"), (song_id,))
            return bool(cur.rowcount)


# The team's store. Tests swap it for a temporary one (monkeypatch server.songs.store).
store = SongStore(os.getenv("DATABASE_URL", ""), os.getenv("SONGS_SQLITE", "data/songs.db"))
