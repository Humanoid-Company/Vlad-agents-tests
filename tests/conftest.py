from __future__ import annotations

import os
import tempfile

# config.py raises EnvironmentError at import time if OPENAI_API_KEY isn't set — set a dummy
# value before any test imports config, so tests never depend on a real .env.
os.environ.setdefault("OPENAI_API_KEY", "sk-test-dummy-key")
# The stores' default files (a save timer may fire after a test's own store is gone) go to a temp dir,
# never the repo's data/.
_TMP = tempfile.mkdtemp(prefix="eva-tests-")
os.environ.setdefault("KNOWLEDGE_SQLITE", os.path.join(_TMP, "knowledge.db"))
os.environ.setdefault("CONVERSATIONS_SQLITE", os.path.join(_TMP, "conversations.db"))
os.environ.setdefault("USAGE_SQLITE", os.path.join(_TMP, "usage.db"))
os.environ.setdefault("CHECKINS_SQLITE", os.path.join(_TMP, "checkins.db"))
os.environ.setdefault("SONGS_SQLITE", os.path.join(_TMP, "songs.db"))


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _temporary_stores(tmp_path, monkeypatch):
    """Knowledge and saved conversations go to throwaway SQLite files, never the repo's data/ or a real database."""
    from server import checkins, conversations, knowledge, songs, usage

    monkeypatch.setattr(knowledge, "store", knowledge.KnowledgeStore("", str(tmp_path / "knowledge.db")))
    monkeypatch.setattr(conversations, "store", conversations.ConversationStore("", str(tmp_path / "conversations.db")))
    monkeypatch.setattr(usage, "store", usage.UsageStore("", str(tmp_path / "usage.db")))
    monkeypatch.setattr(checkins, "store", checkins.CheckinStore("", str(tmp_path / "checkins.db")))
    monkeypatch.setattr(songs, "store", songs.SongStore("", str(tmp_path / "songs.db")))


@pytest.fixture(autouse=True)
def _no_own_voices(monkeypatch):
    """The preset voices by default, whatever the real .env holds; a test sets OPENAI_VOICE_* to try an own voice."""
    for name in ("OPENAI_VOICE_TARAS", "OPENAI_VOICE_TOLIK", "OPENAI_VOICE_TOLIK_RAW", "OPENAI_VOICE_KISHKA"):
        monkeypatch.delenv(name, raising=False)


class _VisionOff:
    """The vision model in tests: always fails, so look_at_camera falls back to the image and nothing is billed."""

    class responses:  # noqa: N801 — mirrors client.responses
        @staticmethod
        def create(**kwargs):
            raise RuntimeError("vision API is off in tests")


@pytest.fixture(autouse=True)
def _no_vision_api(monkeypatch):
    """Tests never reach the real vision model (server/vision.py); a test stubs it to try the text path."""
    from server import vision

    monkeypatch.setattr(vision.vision, "_client", _VisionOff())
