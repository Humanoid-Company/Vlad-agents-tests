"""Every character has a memory of its own: its conversation, and the people it wrote down."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import conversations, knowledge


@pytest.fixture
def client():
    return TestClient(web.app)


def _say(user, text):
    user.conversation.add("user", text)
    user.conversation.end_turn()


def test_each_character_has_its_own_conversation():
    user = web.users.get("mem-conv-0123456789abc")
    user.voice = "meridian"
    assert user.sync_character()
    _say(user, "Привіт, Тарасе")
    taras_id = user.conversation_id

    user.voice = "lesia"
    assert user.sync_character() and len(user.conversation) == 0  # Леся knows nothing of what Тарас heard
    _say(user, "Привіт, Лесю")
    lesia_id = user.conversation_id
    assert lesia_id != taras_id

    user.voice = "meridian"
    assert user.sync_character()
    assert user.conversation_id == taras_id  # Тарас has his own conversation back
    assert [t["text"] for t in user.conversation.to_list() if t["role"] == "user"] == ["Привіт, Тарасе"]


def test_the_three_stepans_share_one_memory():
    user = web.users.get("mem-stepan-0123456789ab")
    user.voice = "blunt"
    user.sync_character()
    _say(user, "Привіт")
    first = user.conversation_id
    for variant in ("blunt_rough", "blunt_unsafe"):
        user.voice = variant
        assert not user.sync_character() and user.conversation_id == first


def test_history_lists_only_the_picked_character(client):
    headers = {"X-Client-Id": "mem-hist-0123456789abcd"}
    user = web.users.get("mem-hist-0123456789abcd")
    user.voice = "meridian"
    user.sync_character()
    _say(user, "Це для Тараса")
    user.voice = "kit"
    user.sync_character()
    _say(user, "Це для Кішки")
    titles = [c["title"] for c in client.get("/api/conversations", headers=headers).json()["conversations"]]
    assert titles == ["Це для Кішки"]
    client.post("/api/voice", json={"voice": "meridian"}, headers=headers)
    titles = [c["title"] for c in client.get("/api/conversations", headers=headers).json()["conversations"]]
    assert titles == ["Це для Тараса"]
    assert [t["text"] for t in client.get("/api/conversation", headers=headers).json()["turns"]] == ["Це для Тараса"]


def test_old_conversations_without_a_character_are_Evas():
    conversations.store.save("old-1", "mem-old-0123456789abcde", 1.0, [{"role": "user", "text": "давнє"}], "")
    assert conversations.store.latest("mem-old-0123456789abcde", "willow")["id"] == "old-1"
    assert conversations.store.latest("mem-old-0123456789abcde", "meridian") is None


def test_what_a_character_wrote_down_stays_with_it():
    store = knowledge.store
    store.remember("Олег", {"хобі": "йога"})  # Єва's
    store.remember("Марія", {"хобі": "шахи"}, knowledge.MEMORY_PREFIX + "Тарас")
    assert "Олег" in store.lookup("Олег")["text"] and "Марія" not in store.lookup("Марія")["text"]
    own = store.lookup("Марія", memory=knowledge.MEMORY_PREFIX + "Тарас")
    assert "Марія" in own["text"] and "Олег" not in own["text"]
    assert "Марія" not in store.lookup("Марія", with_memory=False)["text"]  # the concierge sees neither


def test_memory_sets_are_reserved():
    with pytest.raises(knowledge.KnowledgeError):
        knowledge.store.create_set(knowledge.MEMORY_PREFIX + "Тарас", [{"title": "Олег"}])
    assert knowledge.is_memory(knowledge.MEMORY_SET) and knowledge.is_memory("Пам'ять: Леся")
    assert not knowledge.is_memory("Корпоратив")


def test_remember_tool_writes_into_the_characters_own_set():
    user = web.users.get("mem-tool-0123456789abcd")
    user.voice = "meridian"
    assert user.memory_set == knowledge.MEMORY_PREFIX + "Тарас"
    user.voice = "willow"
    assert user.memory_set == knowledge.MEMORY_SET
