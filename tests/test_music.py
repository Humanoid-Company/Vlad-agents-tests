"""«Музика»: recognising what plays nearby (AudD), writing songs (Lyria) and playing them by voice (server/music.py)."""
from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from server import app as web_app
from server import live_bridge, music, songs, web_users
from server.live_bridge import SidebandToolBridge
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import MUSIC_TOOLS
from tools.results import ToolResult

CLIENT = "music-test-client-0001"


class _Bridge:
    """An open call that only records what she was asked to say."""

    def __init__(self, told: list[str]) -> None:
        self.say_when_quiet = AsyncMock(side_effect=lambda text: told.append(text))
H = {"X-Client-Id": CLIENT}

AUDD_RESULT = {
    "artist": "Океан Ельзи", "title": "Обійми", "album": "Земля", "release_date": "2013-05-01", "label": "Comp",
    "song_link": "https://lis.tn/Obiymy",
    "apple_music": {"url": "https://music.apple.com/x", "previews": [{"url": "https://audio.apple/preview.m4a"}],
                    "artwork": {"url": "https://img.apple/{w}x{h}bb.jpg"}},
    "spotify": {"external_urls": {"spotify": "https://open.spotify.com/track/1"}},
    "deezer": {"link": "https://deezer.com/track/1", "preview": "https://cdn.deezer/p.mp3"},
}


@pytest.fixture
def client():
    return TestClient(web_app.app)


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    monkeypatch.setattr(music, "AUDD_API_TOKEN", "audd-test")
    monkeypatch.setattr(music, "GEMINI_API_KEY", "gemini-test")
    monkeypatch.setattr(music, "OPENROUTER_API_KEY", "or-test")
    monkeypatch.setattr(music, "MUSIC_MODELS", "openrouter:google/lyria-3-pro-preview:free,"
                        "openrouter:google/lyria-3-pro-preview,gemini:lyria-3-pro")
    monkeypatch.setattr(music, "_cooldown", {})
    monkeypatch.setattr(music, "jobs", music.JobBoard())
    monkeypatch.setattr(web_users, "MUSIC_RESULT_WAIT_S", 3.0)  # a stuck wait fails the test fast


@pytest.fixture(autouse=True)
def _fresh_user():
    user = web_app.users.get(CLIENT)
    user.music_result, user.music_result_at, user.music_listening_at, user.music_action = None, 0.0, 0.0, None
    yield


def _user():
    return web_app.users.get(CLIENT)


def _ctx(**extras) -> ToolExecutionContext:
    return ToolExecutionContext(session_id="s1", extras=extras)


# ── recognition ──
def test_audd_result_becomes_names_links_cover_and_preview():
    found = music.parse_audd(AUDD_RESULT)
    assert found["found"] and found["title"] == "Обійми" and found["artist"] == "Океан Ельзи"
    assert found["links"] == {"spotify": "https://open.spotify.com/track/1", "apple_music": "https://music.apple.com/x",
                              "deezer": "https://deezer.com/track/1", "youtube": "https://lis.tn/Obiymy?youtube"}
    assert found["preview_url"] == "https://audio.apple/preview.m4a"  # Apple first, Deezer as the fallback
    assert found["cover_url"] == "https://img.apple/300x300bb.jpg"
    assert music.describe(found) == "«Обійми» — Океан Ельзи (Земля, 2013)"
    assert music.parse_audd(None) == {"found": False}
    bare = music.parse_audd({"artist": "A", "title": "B", "deezer": {"preview": "https://cdn.deezer/p.mp3"}})
    assert bare["preview_url"] == "https://cdn.deezer/p.mp3" and bare["links"] == {}


def test_no_token_says_so_instead_of_calling(monkeypatch):
    monkeypatch.setattr(music, "AUDD_API_TOKEN", "")
    with pytest.raises(music.MusicError) as err:
        music.recognize(b"x", "audio/webm")
    assert err.value.code == "not_configured"
    assert _user().executor.execute_sync("recognize_song", {}, _ctx()).status == "not_configured"


def test_page_clip_is_recognised_and_kept_for_the_tool(client, monkeypatch):
    monkeypatch.setattr(music, "recognize", lambda data, mime: music.parse_audd(AUDD_RESULT))
    res = client.post("/api/music/recognize", content=b"clip", headers={**H, "Content-Type": "audio/webm;codecs=opus"})
    assert res.status_code == 200 and res.json()["title"] == "Обійми"
    assert _user().music_result["title"] == "Обійми"
    assert client.post("/api/music/recognize", content=b"x", headers={**H, "Content-Type": "text/plain"}).status_code == 415
    assert client.post("/api/music/recognize", content=b"", headers={**H, "Content-Type": "audio/webm"}).status_code == 400


def test_a_failed_lookup_still_answers_the_waiting_tool(client, monkeypatch):
    def down(data, mime):
        raise music.MusicError("unavailable", "Сервіс розпізнавання зараз не відповідає.")
    monkeypatch.setattr(music, "recognize", down)
    res = client.post("/api/music/recognize", params={"via": "voice"}, content=b"clip",
                      headers={**H, "Content-Type": "audio/webm"})
    assert res.status_code == 503
    assert _user().music_result["error"].startswith("Сервіс розпізнавання")


def test_tool_waits_for_the_pages_clip():
    user = _user()

    def page():  # the page sees the call, starts listening, posts the clip a moment later
        time.sleep(0.2)
        user.music_listening_at = time.time()
        time.sleep(0.3)
        user.set_music_result(music.parse_audd(AUDD_RESULT))
    threading.Thread(target=page).start()
    result = user.executor.execute_sync("recognize_song", {}, _ctx(work_started_at=time.time()))
    assert result.ok and result.status == "ok" and "Обійми" in result.message and "уривок" in result.message
    assert result.data["artist"] == "Океан Ельзи"


def test_an_old_result_is_not_this_request(monkeypatch):
    """A song recognised minutes ago must not be the answer to a new «що це грає?»."""
    user = _user()
    user.set_music_result(music.parse_audd(AUDD_RESULT))
    user.music_result_at = time.time() - 300
    monkeypatch.setattr(web_users, "MUSIC_LISTEN_START_S", 0.3)
    result = user.executor.execute_sync("recognize_song", {}, _ctx())
    assert not result.ok and result.status == "no_listen"


def test_nothing_recognised():
    user = _user()
    user.music_listening_at = time.time()
    user.set_music_result({"found": False})
    result = user.executor.execute_sync("recognize_song", {}, _ctx())
    assert result.ok and result.status == "not_found"


def test_she_stays_quiet_while_the_page_listens(monkeypatch):
    """No thinking aloud over the music being recorded (the raw mic would hear her too)."""
    monkeypatch.setattr(live_bridge, "_MUMBLE_THINK_FIRST_S", 0.05)
    monkeypatch.setattr(live_bridge, "_MUMBLE_EVERY_S", 0.1)
    monkeypatch.setattr(live_bridge, "_MUMBLE_QUIET_S", 0.0)
    monkeypatch.setattr(live_bridge, "_MUMBLE_INPUT_GRACE_S", 0.0)
    executor = ToolExecutor()
    executor.register("recognize_song", lambda args, ctx: (time.sleep(0.8), ToolResult(ok=True, status="ok", message="ok"))[1],
                      run_in_thread=True)
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()),
                           session=SimpleNamespace(commentary=SimpleNamespace(append=AsyncMock())))
    bridge._connection = conn

    def ev(inner):
        return {"type": "response.event", "delegation_id": "dlg-1", "event": inner}

    async def run():
        await bridge._handle_event(ev({"type": "response.created", "response": {"id": "r1"}}))
        await bridge._handle_event(ev({"type": "response.output_item.done", "item": {
            "type": "function_call", "call_id": "c1", "name": "recognize_song", "arguments": "{}"}}))
        await asyncio.sleep(0.6)
        assert conn.session.commentary.append.await_count == 0
        await asyncio.sleep(0.4)

    asyncio.run(run())


# ── generation ──
def _lyria_body(audio=b"ID3song", text="[Verse] ...") -> dict:
    return {"steps": [{"type": "user_input"}, {"type": "model_output", "content": [
        {"type": "text", "text": text}, {"type": "audio", "data": base64.b64encode(audio).decode(), "mime_type": "audio/mpeg"}]}]}


def test_lyria_response_and_prompt():
    mime, data, text = music.parse_lyria(_lyria_body())
    assert (mime, data, text) == ("audio/mpeg", b"ID3song", "[Verse] ...")
    with pytest.raises(music.MusicError):
        music.parse_lyria({"steps": [{"type": "model_output", "content": [{"type": "text", "text": "no audio"}]}]})
    assert music.lyria_input("lo-fi", "[Verse]\nла-ла", False) == "lo-fi\n\nLyrics:\n[Verse]\nла-ла"
    assert music.lyria_input("lo-fi", "[Verse]", True) == "lo-fi\n\nInstrumental only, no vocals."
    assert music.song_title("", "весела пісня про котика й осінь у Львові") == "Нова пісня: весела пісня про котика й осінь"


def test_voice_request_starts_a_job_and_the_song_lands_in_music(monkeypatch):
    async def fake_generate(prompt, lyrics, instrumental):
        await asyncio.sleep(0.05)
        return "audio/mpeg", b"ID3song", "lyrics", "lyria-3-pro-preview:free (OpenRouter)"
    monkeypatch.setattr(music, "generate", fake_generate)
    user = web_users.WebUser(client_id="music-voice-client-01")
    told: list[str] = []
    user.bridges.add(_Bridge(told))

    async def run():
        result = await user.executor.execute("generate_song", {"prompt": "весела поп-пісня", "title": "Котик"}, _ctx())
        assert result.ok and result.status == "started" and "хвилину" in result.message
        again = await user.executor.execute("generate_song", {"prompt": "ще одна"}, _ctx())
        assert not again.ok and again.status == "busy"  # one at a time per browser
        for _ in range(40):
            await asyncio.sleep(0.05)
            if told:
                break

    asyncio.run(run())
    assert [s["name"] for s in songs.store.list()] == ["Котик"]
    assert told and "Котик" in told[0] and "ready" in told[0]
    job = music.jobs.for_client("music-voice-client-01")[0]
    assert job["status"] == "done" and job["song_name"] == "Котик" and job["model"].endswith("(OpenRouter)")


def test_failed_song_is_reported(monkeypatch):
    async def refuse(prompt, lyrics, instrumental):
        raise music.MusicError("refused", "Модель не взялася за цю пісню: artist voice")
    monkeypatch.setattr(music, "generate", refuse)
    user = web_users.WebUser(client_id="music-voice-client-02")
    told: list[str] = []
    user.bridges.add(_Bridge(told))

    async def run():
        job = music.jobs.start(user.client_id, title="X", prompt="p", via="voice")
        await music.run_job(job, "p", "", False, notify=user.song_ready)

    asyncio.run(run())
    assert music.jobs.for_client(user.client_id)[0]["status"] == "failed"
    assert told and "could not be written" in told[0] and songs.store.list() == []


def test_full_music_refuses_the_new_song(monkeypatch):
    monkeypatch.setattr(songs, "MAX_SONGS", 0)
    with pytest.raises(music.MusicError) as err:
        music.save_song("x", "audio/mpeg", b"1")
    assert err.value.code == "full"


def test_status(client, monkeypatch):
    assert client.get("/api/music/status").json() == {"recognize": True, "generate": True, "models": [
        "lyria-3-pro-preview:free (OpenRouter)", "lyria-3-pro-preview (OpenRouter)", "lyria-3-pro (Gemini)"]}
    monkeypatch.setattr(music, "GEMINI_API_KEY", "")
    assert client.get("/api/music/status").json()["models"] == [
        "lyria-3-pro-preview:free (OpenRouter)", "lyria-3-pro-preview (OpenRouter)"]
    monkeypatch.setattr(music, "OPENROUTER_API_KEY", "")
    assert client.get("/api/music/status").json()["generate"] is False
    assert client.post("/api/music/generate", json={"prompt": "x"}, headers=H).status_code == 503


def test_panel_generate_validates_and_starts(client, monkeypatch):
    started: list = []

    async def fake_run(job, prompt, lyrics, instrumental, notify=None):
        started.append((job.title, prompt, instrumental))
    monkeypatch.setattr(music, "run_job", fake_run)
    assert client.post("/api/music/generate", json={}, headers=H).status_code == 400
    job = client.post("/api/music/generate", json={"prompt": "джаз", "title": "Ніч", "instrumental": True}, headers=H).json()
    assert job["title"] == "Ніч" and job["status"] == "running" and job["via"] == "button"
    assert client.get("/api/music/jobs", headers=H).json()["jobs"][0]["id"] == job["id"]


# ── «увімкни …» ──
def test_song_names_match_loosely():
    items = [{"id": "1", "name": "Червона рута"}, {"id": "2", "name": "Обійми (live)"}, {"id": "3", "name": "Котик"}]
    assert music.match_song("червону руту", items)["id"] == "1"
    assert music.match_song("обійми", items)["id"] == "2"
    assert music.match_song("котика", items)["id"] == "3"
    assert music.match_song("щось зовсім інше", items) is None
    assert music.match_song("", items) is None


def test_play_by_voice_reaches_the_page_once(client):
    song = songs.store.add("Червона рута", "audio/mpeg", b"123")
    result = _user().executor.execute_sync("play_song", {"name": "червону руту"}, _ctx())
    assert result.ok and "Червона рута" in result.message
    action = client.get("/api/music/action", headers=H).json()["action"]
    assert action["type"] == "play" and action["song_id"] == song["id"]
    assert client.get("/api/music/action", headers=H).json()["action"] is None  # taken once


def test_unknown_name_lists_the_songs_and_never_plays_another():
    songs.store.add("Червона рута", "audio/mpeg", b"1")
    user = _user()
    user.set_music_result(music.parse_audd(AUDD_RESULT))
    result = user.executor.execute_sync("play_song", {"name": "Smells like teen spirit"}, _ctx())
    assert not result.ok and result.status == "needs_more_info" and "Червона рута" in result.message
    assert user.music_action is None


def test_play_the_recognised_song_preview():
    user = _user()
    user.set_music_result(music.parse_audd(AUDD_RESULT))
    result = user.executor.execute_sync("play_song", {"recognized": True}, _ctx())
    assert result.ok and user.music_action["type"] == "preview"
    assert user.music_action["url"] == "https://audio.apple/preview.m4a"
    by_name = user.executor.execute_sync("play_song", {"name": "обійми"}, _ctx())
    assert by_name.ok and user.music_action["type"] == "preview"


def test_music_tools_are_in_her_call_not_the_concierges():
    user = _user()
    config = web_app._session_config(user)
    assert "Music:" in config["instructions"]
    assert {t["name"] for t in MUSIC_TOOLS} <= set(str(config["delegation"]).split("'")) or \
        all(t["name"] in str(config["delegation"]) for t in MUSIC_TOOLS)
    concierge = web_app._session_config(user, concierge=True)
    assert "Music:" not in concierge["instructions"]
    assert "recognize_song" not in str(concierge["delegation"])


# ── what went wrong in the first real try ──
def test_silent_clip_is_answered_without_audd(client, monkeypatch):
    calls = []
    monkeypatch.setattr(music, "recognize", lambda data, mime: calls.append(1) or {"found": False})
    res = client.post("/api/music/recognize", params={"via": "voice"}, content=b"RIFF....",
                      headers={**H, "Content-Type": "audio/wav", "X-Clip-Level": "0.00010"})
    assert res.json() == {"found": False, "quiet": True} and calls == []
    user = _user()
    user.music_listening_at = time.time()
    result = user.executor.execute_sync("recognize_song", {}, _ctx())
    assert result.status == "quiet" and "голосніше" in result.message
    loud = client.post("/api/music/recognize", content=b"RIFF....",
                       headers={**H, "Content-Type": "audio/wav", "X-Clip-Level": "0.05"})
    assert loud.status_code == 200 and calls == [1]


def test_audd_fingerprint_failure_is_said_plainly(monkeypatch):
    def fake_post(*args, **kwargs):
        return httpx.Response(200, json={"status": "error", "error": {"error_code": 300, "error_message":
                              "Recognition failed: there's been a problem with creating an audio fingerprint."}},
                              request=httpx.Request("POST", music.AUDD_URL))
    monkeypatch.setattr(music.httpx, "post", fake_post)
    with pytest.raises(music.MusicError) as err:
        music.recognize(b"clip", "audio/webm")
    assert err.value.code == "bad_clip" and "голосніше" in err.value.message


def _route(handlers):
    """A fake network: each URL answers with its handler's response; the requests are kept."""
    seen = []

    def handle(request):
        seen.append((str(request.url), json.loads(request.content or b"{}")))
        return handlers[request.url.host](request)
    return httpx.MockTransport(handle), seen


def _use(monkeypatch, transport):
    real = httpx.AsyncClient
    monkeypatch.setattr(music.httpx, "AsyncClient", lambda **kw: real(transport=transport, **kw))


def _sse(*events) -> bytes:
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


def test_openrouter_stream_is_assembled(monkeypatch):
    mp3 = b"ID3" + bytes(range(60))
    half = len(mp3) // 2
    sse = _sse({"choices": [{"delta": {"content": "[Verse] la"}}]},
               {"choices": [{"delta": {"audio": {"data": base64.b64encode(mp3[:half]).decode()}}}]},
               {"choices": [{"delta": {"audio": {"data": base64.b64encode(mp3[half:]).decode()}}}]})
    transport, seen = _route({"openrouter.ai": lambda r: httpx.Response(200, headers={"content-type": "text/event-stream"},
                                                                         content=sse)})
    _use(monkeypatch, transport)
    mime, data, text, model = asyncio.run(music.generate("pop", "[Verse] la", False))
    assert (mime, data, text, model) == ("audio/mpeg", mp3, "[Verse] la", "lyria-3-pro-preview:free (OpenRouter)")
    body = seen[0][1]
    assert body["model"] == "google/lyria-3-pro-preview:free" and body["stream"] is True
    assert body["modalities"] == ["text", "audio"] and "Lyrics:" in body["messages"][0]["content"]


def test_free_limit_runs_out_and_the_next_model_writes_the_song(monkeypatch):
    """The free model's daily limit → the paid one has no credits → Gemini writes it; next time the free one waits."""
    def openrouter(request):
        model = json.loads(request.content)["model"]
        if model.endswith(":free"):
            return httpx.Response(429, json={"error": {"code": 429, "message": "Rate limit exceeded: free-models-per-day"}})
        return httpx.Response(402, json={"error": {"code": 402, "message": "Insufficient credits"}})
    gemini = lambda request: httpx.Response(200, json=_lyria_body(b"ID3gem"))  # noqa: E731
    transport, seen = _route({"openrouter.ai": openrouter, "generativelanguage.googleapis.com": gemini})
    _use(monkeypatch, transport)
    mime, data, text, model = asyncio.run(music.generate("pop", "", False))
    assert data == b"ID3gem" and model == "lyria-3-pro (Gemini)"
    assert [u.split("/")[2] for u, _ in seen] == ["openrouter.ai", "openrouter.ai", "generativelanguage.googleapis.com"]
    seen.clear()
    asyncio.run(music.generate("pop", "", False))  # both OpenRouter models are resting now: Gemini at once
    assert [u.split("/")[2] for u, _ in seen] == ["generativelanguage.googleapis.com"]


def test_every_model_out_says_why(monkeypatch):
    monkeypatch.setattr(music, "GEMINI_API_KEY", "")
    transport, _ = _route({"openrouter.ai": lambda r: httpx.Response(429, json={"error": {
        "message": "Rate limit exceeded for model (limit: 0 requests per day on Free Tier)"}})})
    _use(monkeypatch, transport)
    with pytest.raises(music.MusicError) as err:
        asyncio.run(music.generate("pop", "", False))
    assert err.value.code == "quota" and "ліміт вичерпано" in err.value.message and "оновлюється щодня" in err.value.message


def test_a_refused_song_is_not_tried_on_other_models(monkeypatch):
    transport, seen = _route({"openrouter.ai": lambda r: httpx.Response(400, json={"error": {
        "message": "Prompt mentions a real artist's voice"}})})
    _use(monkeypatch, transport)
    with pytest.raises(music.MusicError) as err:
        asyncio.run(music.generate("як у Бейонсе", "", False))
    assert err.value.code == "refused" and len(seen) == 1


def test_gemini_free_tier_quota(monkeypatch):
    monkeypatch.setattr(music, "OPENROUTER_API_KEY", "")
    transport, _ = _route({"generativelanguage.googleapis.com": lambda r: httpx.Response(429, json={"error": {
        "message": "Rate limit exceeded for model lyria-3-pro (limit: 0 requests per day on Free Tier)"}})})
    _use(monkeypatch, transport)
    with pytest.raises(music.MusicError) as err:
        asyncio.run(music.generate("pop", "", False))
    assert err.value.code == "quota"
    midnight = (int(time.time() // 86400) + 1) * 86400
    assert music._cooldown["gemini:lyria-3-pro"] == midnight  # a daily limit: skipped until midnight UTC


def test_she_asks_what_the_song_is_about_first():
    from tools.live_schemas import GENERATE_SONG_TOOL, RECOGNIZE_SONG_TOOL
    assert "можеш створити пісню?" in GENERATE_SONG_TOOL["description"]
    assert "going to put it on" in RECOGNIZE_SONG_TOOL["description"]
    assert "ask what about first" in web_app._MUSIC_NOTE and "do not call it" in web_app._MUSIC_BACKEND_NOTE


def test_elevenlabs_writes_the_song_when_the_others_run_out(monkeypatch):
    monkeypatch.setattr(music, "ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setattr(music, "ELEVENLABS_LENGTH_S", 45)
    monkeypatch.setattr(music, "MUSIC_MODELS", "openrouter:google/lyria-3-pro-preview:free,elevenlabs:music_v1")
    audio = b"ID3" + b"\x00" * 64
    transport, seen = _route({
        "openrouter.ai": lambda r: httpx.Response(429, json={"error": {"message": "limit: 0 requests per day"}}),
        "api.elevenlabs.io": lambda r: httpx.Response(200, content=audio, headers={"content-type": "audio/mpeg"}),
    })
    _use(monkeypatch, transport)
    mime, data, _, who = asyncio.run(music.generate("весела поп-пісня", "[Verse] Собаки", False))
    assert data == audio and mime == "audio/mpeg" and who == "music_v1 (ElevenLabs)"
    url, sent = next(item for item in seen if "elevenlabs" in item[0])
    assert sent["model_id"] == "music_v1" and sent["music_length_ms"] == 45000 and "Собаки" in sent["prompt"]
    assert "output_format=mp3_44100_128" in url


def test_elevenlabs_free_plan_without_music_is_a_clear_message(monkeypatch):
    monkeypatch.setattr(music, "ELEVENLABS_API_KEY", "el-test")
    monkeypatch.setattr(music, "MUSIC_MODELS", "elevenlabs:music_v1")
    transport, _ = _route({"api.elevenlabs.io": lambda r: httpx.Response(402, json={"detail": {
        "status": "payment_required", "message": "Music requires a paid subscription"}})})
    _use(monkeypatch, transport)
    with pytest.raises(music.MusicError) as err:
        asyncio.run(music.generate("pop", "", False))
    assert "ElevenLabs" in err.value.message and "кредит" in err.value.message
