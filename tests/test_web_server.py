"""Hosted web backend: per-browser isolation, access code, voices, calls."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import server.app as web
from tools.executor import ToolExecutionContext

ALICE = "alice-browser-0123456789"
BOB = "bob-browser-0123456789ab"


@pytest.fixture()
def client(monkeypatch):
    return TestClient(web.app)


def test_health_and_config(client):
    health = client.get("/healthz").json()
    assert health["ok"] is True and isinstance(health["web_search"], bool)


def test_there_are_no_accounts_and_everything_is_open(client):
    """No login, no guests, no admins: any browser with a client id talks, reads «Знання» and the concierge's marks."""
    browser = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=browser).status_code == 200
    assert client.get("/api/knowledge").status_code == 200
    assert client.get("/api/concierge/checkins").status_code == 200
    assert client.delete("/api/concierge/checkins").status_code == 200
    assert client.get("/api/system").status_code == 200
    for path in ("/api/config", "/api/auth/me", "/api/admin/users"):
        assert client.get(path).status_code == 404


def test_client_id_is_validated(client):
    assert client.get("/api/me", headers={"X-Client-Id": "short"}).status_code == 400


def test_each_browser_has_its_own_conversation_and_voice(client):
    gina, hal = "gina-browser-0123456789ab", "hal-browser-0123456789abc"
    web.users.get(gina).conversation.add("user", "секрет Джини")
    client.post("/api/voice", json={"voice": "ripple"}, headers={"X-Client-Id": gina})
    assert len(web.users.get(hal).conversation) == 0 and web.users.get(hal).voice != "ripple"
    assert client.get("/api/me", headers={"X-Client-Id": gina}).json()["voice"] == "ripple"


def test_session_tools_run_per_browser():
    user = web.users.get("ivy-browser-0123456789abc")
    result = asyncio.run(
        user.executor.execute("set_assistant_name", {"name": "Ліна"}, ToolExecutionContext(session_id="s"))
    )
    assert result.ok and user.assistant_name == "Ліна"
    assert web.users.get("jay-browser-0123456789abc").assistant_name is None


class _FakeSideband:
    """Stands in for client.live.sideband.connect(): stays open until session.close()."""

    def __init__(self) -> None:
        self.closed = asyncio.Event()
        self.close_calls = 0
        outer = self

        class _Session:
            async def close(self) -> None:
                outer.close_calls += 1
                outer.closed.set()

        self.session = _Session()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def __aiter__(self):
        return self

    async def __anext__(self):
        await self.closed.wait()
        raise StopAsyncIteration


class _FakeLiveClient:
    def __init__(self) -> None:
        self.connections: list[_FakeSideband] = []
        outer = self

        class _Sideband:
            def connect(self, *, session_id: str) -> _FakeSideband:
                conn = _FakeSideband()
                outer.connections.append(conn)
                return conn

        class _Live:
            sideband = _Sideband()

        self.live = _Live()


def test_forgotten_call_is_closed_after_the_time_limit():
    from server.live_bridge import SidebandToolBridge

    fake = _FakeLiveClient()
    closed: list[object] = []
    bridge = SidebandToolBridge(
        client=fake,  # type: ignore[arg-type]
        session_id="sess-1",
        executor=None,  # type: ignore[arg-type]
        on_closed=closed.append,
        max_duration_s=0.05,
    )
    asyncio.run(asyncio.wait_for(bridge.run(), timeout=2))
    assert fake.connections[0].close_calls == 1
    assert closed == [bridge]
    assert not bridge.is_open


def test_new_call_closes_the_previous_one_of_the_same_browser(client, monkeypatch):
    class _Old:
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    class _Created:
        class session:  # noqa: N801
            id = "sess-new"

        class transport:  # noqa: N801
            sdp = "answer"

    async def fake_create(**_kwargs):
        return _Created

    started: list[object] = []
    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0, started.append(self)))
    user = web.users.get(ALICE)
    old = _Old()
    user.bridges.add(old)

    res = client.post("/api/session", json={"sdp": "offer"}, headers={"X-Client-Id": ALICE})
    assert res.status_code == 200
    assert res.json()["sdp"] == "answer"
    assert _Old.closed
    user.bridges.discard(old)
    assert [b.session_id for b in user.bridges] == ["sess-new"]
    user.bridges.clear()


def test_keeps_itself_awake_only_when_deployed(monkeypatch):
    import time

    import httpx

    pinged: list[str] = []

    async def fake_get(self, url, **_kwargs):
        pinged.append(url)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(web, "KEEP_AWAKE_S", 0.05)

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "http://localhost:8000")
    with TestClient(web.app):
        time.sleep(0.2)
    assert pinged == []

    monkeypatch.setattr(web, "PUBLIC_BACKEND_URL", "https://backend.example")
    with TestClient(web.app):
        time.sleep(0.3)
    assert pinged and set(pinged) == {"https://backend.example/healthz"}


def test_voice_picker_lists_live_voices_and_saves_choice(client):
    data = client.get("/api/voices").json()
    ids = [v["id"] for v in data["voices"]]
    assert {"willow", "bossa", "marin", "lina", "meridian", "ripple", "blunt", "blunt_rough", "blunt_unsafe", "lesia", "kit"} == set(ids)
    assert data["default"] == "willow"  # Єва; OPENAI_LIVE_VOICE=gleam is no character any more
    assert any(v["feminine"] for v in data["voices"]) and not all(v["feminine"] for v in data["voices"])
    assert all(v["label"] and v["description"] for v in data["voices"])

    headers = {"X-Client-Id": ALICE}
    assert client.get("/api/me", headers=headers).json()["voice"] is None
    assert client.post("/api/voice", json={"voice": "willow"}, headers=headers).json()["voice"] == "willow"
    assert client.get("/api/me", headers=headers).json()["voice"] == "willow"
    assert client.post("/api/voice", json={"voice": "nope"}, headers=headers).status_code == 400

    config = web._session_config(web.users.get(ALICE))
    assert config["audio"]["output"]["voice"] == "willow"
    assert "Your character — Єва" in config["instructions"] and "Your name is Єва" in config["instructions"]
    assert "feminine grammatical gender" in config["instructions"]


_OWN_VOICES = {
    "meridian": ("OPENAI_VOICE_TARAS", "voice_taras_test", "meridian", "late 30s"),
    "blunt": ("OPENAI_VOICE_TOLIK", "voice_tolik_test", "cedar", "early 50s"),
    "kit": ("OPENAI_VOICE_KISHKA", "voice_kishka_test", "bossa", "late 20s"),
}


@pytest.mark.parametrize("persona", sorted(_OWN_VOICES))
def test_own_voice_goes_to_the_session_as_an_id_object(client, monkeypatch, persona):
    """Env set → audio.output.voice is {"id": …} and the voice description is a «Voice: …» line; the picker's
    sample stays the preset's (no speech-endpoint sample for own voices)."""
    env, voice_id, preset, age = _OWN_VOICES[persona]
    monkeypatch.setenv(env, voice_id)
    cid = f"own-{persona}-0123456789abcdef"
    assert client.post("/api/voice", json={"voice": persona}, headers={"X-Client-Id": cid}).status_code == 200
    user = web.users.get(cid)
    config = web._session_config(user)
    assert config["audio"]["output"]["voice"] == {"id": voice_id}
    assert "\nVoice: " in config["instructions"] and age in config["instructions"]
    conc = web._session_config(user, concierge=True)
    assert conc["audio"]["output"]["voice"] == {"id": voice_id} and "\nVoice: " in conc["instructions"]
    entry = next(v for v in client.get("/api/voices").json()["voices"] if v["id"] == persona)
    assert entry["sample"] == preset


@pytest.mark.parametrize("persona", sorted(_OWN_VOICES))
def test_empty_env_keeps_the_preset_voice(client, monkeypatch, persona):
    env, _, preset, _ = _OWN_VOICES[persona]
    monkeypatch.setenv(env, "  ")  # blank = unset
    cid = f"pre-{persona}-0123456789abcdef"
    assert client.post("/api/voice", json={"voice": persona}, headers={"X-Client-Id": cid}).status_code == 200
    config = web._session_config(web.users.get(cid))
    assert config["audio"]["output"]["voice"] == preset
    assert "\nVoice: " not in config["instructions"]


@pytest.mark.parametrize("persona", sorted(_OWN_VOICES))
def test_old_voice_variant_switches_back_to_the_preset(client, monkeypatch, persona):
    """The picker's «новий / старий голос»: voice_variant "preset" → the preset and no «Voice:» line, "own" → back."""
    env, voice_id, preset, _ = _OWN_VOICES[persona]
    monkeypatch.setenv(env, voice_id)
    cid = f"var-{persona}-0123456789abcdef"
    headers = {"X-Client-Id": cid}
    entry = next(v for v in client.get("/api/voices").json()["voices"] if v["id"] == persona)
    assert entry["own_voice"] is True
    user = web.users.get(cid)
    assert client.post("/api/voice", json={"voice": persona, "voice_variant": "preset"}, headers=headers).status_code == 200
    for concierge in (False, True):
        config = web._session_config(user, concierge=concierge)
        assert config["audio"]["output"]["voice"] == preset and "\nVoice: " not in config["instructions"]
    assert client.post("/api/voice", json={"voice": persona, "voice_variant": "own"}, headers=headers).status_code == 200
    assert web._session_config(user)["audio"]["output"]["voice"] == {"id": voice_id}


def test_voices_say_whether_a_character_has_an_own_voice(client, monkeypatch):
    monkeypatch.setenv("OPENAI_VOICE_KISHKA", "voice_k")
    flags = {v["id"]: v["own_voice"] for v in client.get("/api/voices").json()["voices"]}
    assert flags["kit"] is True and flags["blunt"] is False and flags["lesia"] is False


def test_delivery_notes_name_pace_and_manner_for_every_answer():
    from voice.options import delivery_instruction

    slow = delivery_instruction("slow", "calm")
    assert "PACE" in slow and "slow" in slow and "MANNER" in slow and "calm" in slow and "on top of your character" in slow
    changed = delivery_instruction("fast", "expressive", changed=True)
    assert "replace any earlier" in changed and "faster" in changed and "expressive" in changed
    assert delivery_instruction("normal", "normal") == ""


def test_own_voice_is_accepted_by_the_desktop_change_voice():
    from voice.options import LIVE_VOICE_OPTIONS, VOICE_PERSONAS, api_voice

    assert {"blunt", "kit"} <= set(VOICE_PERSONAS) and "blunt" not in LIVE_VOICE_OPTIONS
    assert api_voice("willow") == "willow" and api_voice("cedar") == "cedar"  # plain presets pass through


def test_tolik_is_a_character_on_cedar(client):
    """Степан, the courtyard hooligan: Кедр's voice — apart in the picker, only on request, and never at the
    concierge's door."""
    tolik = next(v for v in client.get("/api/voices").json()["voices"] if v["id"] == "blunt")
    assert tolik["special"] and tolik["sample"] == "cedar" and tolik["label"] == "Степан"
    user = web.users.get("blunt-0123456789abcdef")
    assert client.post("/api/voice", json={"voice": "blunt"}, headers={"X-Client-Id": "blunt-0123456789abcdef"}).status_code == 200
    config = web._session_config(user)
    assert config["audio"]["output"]["voice"] == "cedar"  # the API gets the preset
    assert "You are Степан" in config["instructions"] and "Your name is Степан" in config["instructions"]
    assert "masculine grammatical gender" in config["instructions"]
    # The softest version: a human (not a cat), boorish but not aggressive, no swearing at all.
    assert "never a cat, a robot or any animal" in config["instructions"]
    assert "No swearing at all" in config["instructions"] and "not aggressive" in config["instructions"]
    assert "With children" in config["instructions"] and "noticeably softer" in config["instructions"]
    # Prod: a rant, then a calm helpful answer; «Вибач, я справді занадто складно пояснив»; «Да, я здесь».
    assert "never a normal assistant's tone" in config["instructions"] and "Never apologise" in config["instructions"]
    assert "that is the game, not distress" in config["instructions"] and "never a word of Russian" in config["instructions"]
    assert "never death, graves" in config["instructions"]
    # v5's friendly lines are gone for him (they won: «Та нормально, з тобою от балакаю. А в тебе як?»).
    assert "з тобою от балакаю" not in config["instructions"] and "warm and friendly" not in config["instructions"]
    assert "end with one short, specific question" not in config["instructions"]
    assert "VOICE CHARACTER: the voice is Степан" in config["delegation"]["responses"]["instructions"]
    conc = web._session_config(user, concierge=True)
    assert conc["audio"]["output"]["voice"] == "cedar" and "courtyard hooligan" not in conc["instructions"]
    assert "You are Степан, the concierge" in conc["instructions"]  # his name at the door, not his roast
    assert "VOICE CHARACTER" not in conc["delegation"]["responses"]["instructions"]
    # Others keep their own character and none of his.
    plain = web._session_config(web.users.get("blunt-plain-0123456789a"))
    assert "з тобою от балакаю" in plain["instructions"]  # Єва, the default: a warm one keeps v5 as it is
    assert "You are Степан" not in plain["instructions"] and "Степан" not in plain["delegation"]["responses"]["instructions"]


def test_each_character_answers_to_its_own_name(client):
    """«хай кожен персонаж відкликається на своє імя»: the page wakes and pauses each by its name."""
    voices = {v["id"]: v for v in client.get("/api/voices").json()["voices"]}
    assert voices["willow"]["label"] == "Єва" and "єва" in voices["willow"]["wake"]
    assert {"степан", "степане", "толік"} <= set(voices["blunt"]["wake"]) and "мілано" in voices["bossa"]["wake"]
    assert all(v["wake"] for v in voices.values())
    tolik = web.users.get("wake-tolik-0123456789a")
    tolik.voice = "blunt"
    assert "«Степан, скажи»" in web._session_config(tolik)["instructions"]


def test_characters_have_their_tone_and_limits():
    """Мілана flirts lightly, Ліна is gloomy only in manner; the gone plain voices
    fall back to Єва."""
    def cfg(voice):
        user = web.users.get(f"char-{voice}-0123456789abc"[:24])
        user.voice = voice
        return web._session_config(user)

    milana, lina = cfg("bossa"), cfg("lina")
    assert "nothing explicit or intimate" in milana["instructions"] and "з тобою от балакаю" in milana["instructions"]
    assert lina["audio"]["output"]["voice"] == "willow" and "never death" in lina["instructions"]
    assert "warm and friendly" not in lina["instructions"]  # her own tone
    assert "VOICE CHARACTER: the voice is Ліна" in lina["delegation"]["responses"]["instructions"]
    gone = cfg("tempo")  # picked before the characters
    assert gone["audio"]["output"]["voice"] == "willow" and "Your name is Єва" in gone["instructions"]
    # Леся, a robot cat on Глім's voice: a pessimist who whines; literary curses only, and sorry for them.
    lesia = cfg("lesia")
    assert lesia["audio"]["output"]["voice"] == "gleam" and "a robot cat" not in lesia["instructions"] and "a living person" in lesia["instructions"]
    assert "щоб тебе качка копнула" in lesia["instructions"] and "never mat" in lesia["instructions"]
    assert "You are a person, not a robot and not a cat" in lesia["instructions"] and "курва" not in lesia["instructions"]
    assert "You tease the person" in lesia["instructions"] and "Never mean" in lesia["instructions"]
    # Prod: «А ти як? Живий-цілий?», «Ти що зараз думаєш робити?» every reply — v5's friendly lines are gone for her.
    assert "warm and friendly" not in lesia["instructions"] and "no friendly follow-up questions" in lesia["instructions"]
    assert "feminine grammatical gender" in lesia["instructions"]
    # The owner's rules: no mat at all; a light «до дупи» now and then, with «вибачте за мій французький» each time.
    assert "вибачте за мій французький" in lesia["instructions"] and "Never «бля»" in lesia["instructions"]
    # Кішка — a talking cat for birthdays and parties: a different greeting every time, no mat, apologises.
    kit = cfg("kit")
    assert kit["audio"]["output"]["voice"] == "bossa" and "a talking she-cat" in kit["instructions"]  # a female voice
    assert "never the same script" in kit["instructions"] and "No mat at all" in kit["instructions"]
    assert "гони касу" in kit["instructions"] and "feminine grammatical gender" in kit["instructions"]
    assert "warm and friendly" not in kit["instructions"] and "«Кішка, скажи»" in kit["instructions"]
    assert "«Леся, скажи»" in lesia["instructions"] and "Леся" in lesia["delegation"]["responses"]["instructions"]


def test_blunt_prompt_takes_the_users_additions(monkeypatch):
    """voice/blunt_prompt.py: MY_ADDITIONS go to the Live prompt and to the backend; empty — nothing extra."""
    from voice import blunt_prompt

    assert "More from the user" not in blunt_prompt.character()
    monkeypatch.setattr(blunt_prompt, "MY_ADDITIONS", "\nКажи «братан» замість «друже».\n")
    assert blunt_prompt.character().endswith("(these win over the above):\nКажи «братан» замість «друже».")
    assert "Кажи «братан»" in blunt_prompt.backend_note() and blunt_prompt.backend_note().endswith("\n")


def test_speed_and_style_are_saved_and_returned_for_the_page(client):
    """The page sends the returned instruction itself once Єва is quiet; the server only stores."""
    user = web.users.get("dave-browser-0123456789a")
    sent = []

    class FakeBridge:
        async def append_instruction(self, text):
            sent.append(text)

    user.bridges.add(FakeBridge())
    headers = {"X-Client-Id": "dave-browser-0123456789a"}
    body = client.post("/api/voice", json={"speed": "slow", "style": "calm"}, headers=headers).json()
    assert body["speed"] == "slow" and body["style"] == "calm"
    assert "slower" in body["instruction"] and "calm" in body["instruction"] and "not a message" in body["instruction"]
    assert sent == []  # not pushed mid-answer by the server
    assert "slower" in web._session_config(user)["instructions"]  # a new session starts with them
    assert client.post("/api/voice", json={"speed": "slow"}, headers=headers).json()["instruction"] == ""
    assert client.post("/api/voice", json={"speed": "warp"}, headers=headers).status_code == 400
    user.bridges.clear()


def test_new_conversation_clears_history(client):
    headers = {"X-Client-Id": "erin-browser-0123456789a"}
    user = web.users.get("erin-browser-0123456789a")
    user.conversation.add("user", "секрет")
    assert client.delete("/api/conversation", headers=headers).json()["ok"]
    assert len(user.conversation) == 0 and "input" not in web._session_config(user)


def test_speed_change_waits_until_she_finishes_speaking(monkeypatch):
    """An instruction appended mid-answer derails it: it is sent once she is quiet, latest only."""
    from server import live_bridge
    from server.live_bridge import SidebandToolBridge

    monkeypatch.setattr(live_bridge, "_SPEECH_CHARS_PER_S", 1e6)  # the text's quiet only (audio: test_work_progress)

    sent: list[str] = []

    class Conn:
        class session:
            class instructions:
                @staticmethod
                async def append(content, **kw):
                    sent.append(content)

    async def scenario():
        bridge = SidebandToolBridge(client=None, session_id="s4", executor=None)
        bridge._connection = Conn()
        for word in ["Жила-була ", "маленька "]:
            await bridge._handle_event({"type": "session.output_transcript.delta", "delta": word})
        await bridge.append_instruction("slow", quiet_s=0.3)
        await asyncio.sleep(0.1)
        await bridge.append_instruction("calm", quiet_s=0.3)  # replaces «slow»
        await bridge._handle_event({"type": "session.output_transcript.delta", "delta": "дівчинка."})
        await asyncio.sleep(0.15)
        assert sent == []  # still talking
        await asyncio.sleep(0.5)
        assert sent == ["calm"]

    asyncio.run(scenario())


def test_the_language_changes_only_when_asked():
    """Prod: «Ну так, расскажи, как ИИ работает» → change_language ru by the model; «Українською говори» never
    switched it back, and every next call opened with «speak exclusively in Russian»."""
    user = web.users.get("lang-guard-0123456789a")
    said = lambda *u: ToolExecutionContext(session_id="s", user_utterances=list(u))  # noqa: E731
    refused = asyncio.run(user.executor.execute("change_language", {"language": "ru"}, said("Алло", "расскажи, как ИИ работает")))
    assert not refused.ok and user.language == "uk" and "не змінюй" in refused.message
    assert asyncio.run(user.executor.execute("change_language", {"language": "en"}, said("What's up"))).ok is False
    asked = asyncio.run(user.executor.execute("change_language", {"language": "en"}, said("Давай англійською")))
    assert asked.ok and user.language == "en"
    back = asyncio.run(user.executor.execute("change_language", {"language": "uk"}, said("ok")))  # back home: always
    assert back.ok and user.language == "uk"
    assert asyncio.run(user.executor.execute("change_language", {"language": "ru"}, said("говори по-русски"))).ok


def test_wake_request_goes_into_the_new_call_as_the_users_message(monkeypatch):
    """Prod: «Єва, скажи, яка в мене завтра подія» → she said «Яка в мене завтра подія?». Quoted in a
    commentary GPT-Live sometimes reads it back (1 of 3); as the user's message in the history, 0 of 3."""
    captured = {}

    async def fake_create(*, session, transport):
        captured["input"] = session.get("input", [])
        return SimpleNamespace(session=SimpleNamespace(id="sess-wake"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    user = web.users.get("wake-req-0123456789ab")
    user.conversation.clear()
    res = TestClient(web.app).post(
        "/api/session",
        headers={"X-Client-Id": "wake-req-0123456789ab"},
        json={"sdp": "offer", "user_text": "Єва, скажи, яка в мене завтра подія"},
    )
    assert res.status_code == 200
    last = captured["input"][-1]
    assert last["role"] == "user" and "яка в мене завтра подія" in last["content"][0]["text"]


def test_voice_switch_keeps_the_old_call_until_the_page_swaps(monkeypatch):
    """keep_old: the new call in another voice opens beside the talking one; the page closes the old
    call when it swaps — and the server does it anyway if both are still open much later."""
    monkeypatch.setattr(web, "KEEP_OLD_MAX_S", 0.05)

    class _Old:
        session_id = "sess-old"
        is_open = True
        closed = False

        async def close(self) -> None:
            _Old.closed = True

    async def fake_create(**_kwargs):
        return SimpleNamespace(session=SimpleNamespace(id="sess-standby"), transport=SimpleNamespace(sdp="answer"))

    async def fake_run(self):
        self._connection = object()  # stays open
        await asyncio.sleep(1)

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", fake_run)
    client_id = "keep-old-0123456789ab"
    user = web.users.get(client_id)
    user.bridges.clear()
    old = _Old()
    user.bridges.add(old)
    with TestClient(web.app) as client:
        res = client.post("/api/session", json={"sdp": "offer", "keep_old": True}, headers={"X-Client-Id": client_id})
        assert res.status_code == 200
        assert not _Old.closed  # still talking while the new call comes up
        import time

        time.sleep(0.3)
    assert _Old.closed  # the page never swapped: closed by the server
    user.bridges.clear()


def test_prompt_v5_is_the_default_and_v4_v3_only_when_the_page_asks(monkeypatch):
    """v5 (v4 condensed) is the default; v1 and v2 are gone from the web (desktop only): a page that
    still asks for them gets v5."""
    sent: list[str] = []

    async def fake_create(*, session, transport):
        sent.append(session["instructions"])
        return SimpleNamespace(session=SimpleNamespace(id=f"sess-p{len(sent)}"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "prompt-var-0123456789ab"}
    client = TestClient(web.app)
    bodies = ({"sdp": "o", "prompt": "v1"}, {"sdp": "o", "prompt": "v2"}, {"sdp": "o", "prompt": "v3"},
              {"sdp": "o", "prompt": "v4"}, {"sdp": "o"}, {"sdp": "o", "prompt": "nonsense"})
    for body in bodies:
        assert client.post("/api/session", headers=headers, json=body).status_code == 200
    v1, v2, v3, v4, v5, fallback = sent
    assert "Being a person" in v3 and "you are talking, not reading" in v3 and "Answering" not in v3
    assert "Answering" in v4 and "Being a person" in v4
    assert "How a turn goes" in v5 and "Answering" not in v5 and v5 == fallback == v1 == v2
    for text in (v3, v4):  # the shared parts stay in both
        assert "Voice changes:" in text and "Interruption policy" in text
        assert "You are a voice assistant first" in text and "зараз прикину" in text
    web.users.get(headers["X-Client-Id"]).bridges.clear()


def test_language_change_reaches_the_running_call():
    """The call was started with «Speak exclusively in українською»: without a note in the call itself
    she went back to Ukrainian a few replies after switching to English."""

    class _Bridge:
        def __init__(self) -> None:
            self.notes: list[str] = []

        async def append_instruction(self, text: str, **_) -> None:
            self.notes.append(text)

    async def scenario():
        user = web.users.get("language-switch-0123456789")
        bridge = _Bridge()
        user.bridges.add(bridge)
        result = await user.executor.execute("change_language", {"language": "en"}, ToolExecutionContext())
        await asyncio.sleep(0)  # the note goes out in the background
        await asyncio.sleep(0)
        user.bridges.clear()
        return user, bridge, result

    user, bridge, result = asyncio.run(scenario())
    assert result.ok and user.language == "en"
    assert bridge.notes == ["From now on speak exclusively in англійською."]
    assert "не починай заново" in result.message  # she goes on, no «Sure… Okay… Sure…»


def test_diag_keeps_numbers_only():
    """/api/diag: the page's call numbers (gate levels, barge-in timings, unanswered turns) — any text
    is dropped, an oversized report refused; GET reads the latest."""
    headers = {"X-Client-Id": "diag-0123456789abcdef"}
    client = TestClient(web.app)
    body = {"gate": {"echo": 0.12, "mic": "speakers"}, "said": "Привіт, Єва, ось мій секрет", "barge": {"muteMs": [800, 1500]}}
    assert client.post("/api/diag", headers=headers, json=body).json() == {"ok": True}
    assert client.post("/api/diag", headers=headers, content=b"x" * 5000).status_code == 413
    last = client.get("/api/diag", params={"n": 1}).json()[-1]
    assert last["data"] == {"gate": {"echo": 0.12, "mic": "speakers"}, "said": None, "barge": {"muteMs": [800, 1500]}}


def test_concierge_mode_and_confirm_guest(client):
    """«Консьєрж»: her rules and the backend's check protocol only in that mode; confirm_guest marks the guest,
    «Наступний гість» clears it, and the page sees it in /api/me."""
    from server import knowledge
    from server.knowledge_import import parse_file

    knowledge.store.create_set("Гості", parse_file("g.csv", "ПІБ;Посада\nОлег Петренко;бекенд-розробник\n".encode()).items)
    user = web.users.get("conc-browser-0123456789ab")
    plain, conc = web._session_config(user), web._session_config(user, concierge=True)
    assert "the concierge at the entrance" in conc["instructions"] and "concierge" not in plain["instructions"]
    names = lambda c: [t["name"] for t in c["delegation"]["responses"]["tools"]]  # noqa: E731
    assert "confirm_guest" in names(conc) and "confirm_guest" not in names(plain)
    assert "CONCIERGE" in conc["delegation"]["responses"]["instructions"]
    ok = asyncio.run(user.executor.execute(
        "confirm_guest", {"full_name": "Олег Петренко", "verified": True, "guest_answer": "бекенд-розробник"},
        ToolExecutionContext(session_id="s", user_utterances=["Олег", "Петренко", "Я бекенд-розробник"])))
    assert ok.ok and "Олег Петренко" in ok.message
    me = client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()
    assert me["guest"]["name"] == "Олег Петренко" and me["guest"]["verified"] is True
    client.delete("/api/conversation", headers={"X-Client-Id": "conc-browser-0123456789ab"})  # «Наступний гість»
    assert client.get("/api/me", headers={"X-Client-Id": "conc-browser-0123456789ab"}).json()["guest"] is None


def test_three_toliks_safe_rough_and_unsafe(client):
    """Безпечний Степан (blunt, cedar): no swearing, softer with children. Степан (blunt_rough, ash): light swearing.
    Небезпечний (blunt_unsafe, ash): mat. All three speak the own voice when it is set."""
    voices = {v["id"]: v for v in client.get("/api/voices").json()["voices"]}
    assert voices["blunt"]["label"] == voices["blunt_rough"]["label"] == voices["blunt_unsafe"]["label"] == "Степан"  # one tab
    assert voices["blunt"]["group"] == voices["blunt_rough"]["group"] == voices["blunt_unsafe"]["group"] == "blunt"
    assert [voices[k]["variant_label"] for k in ("blunt", "blunt_rough", "blunt_unsafe")] == ["Безпечний Степан", "Степан", "Небезпечний Степан"]
    assert voices["blunt"]["sample"] == "cedar" and voices["blunt_rough"]["sample"] == "ash"
    safe = web.users.get("tolik-safe-0123456789abc")
    safe.voice = "blunt"
    rough = web.users.get("tolik-rough-0123456789ab")
    rough.voice = "blunt_rough"
    safe_cfg, rough_cfg = web._session_config(safe), web._session_config(rough)
    assert safe_cfg["audio"]["output"]["voice"] == "cedar" and rough_cfg["audio"]["output"]["voice"] == "ash"
    assert "No swearing at all" in safe_cfg["instructions"] and "With children" in safe_cfg["instructions"]
    assert "How you swear" in rough_cfg["instructions"] and "No swearing at all" not in rough_cfg["instructions"]
    assert "You are Степан" in rough_cfg["instructions"] and "Your name is Степан" in rough_cfg["instructions"]
    assert "no swearing at all" in safe_cfg["delegation"]["responses"]["instructions"]
    assert "no swearing at all" not in rough_cfg["delegation"]["responses"]["instructions"]
