"""Camera on the web: the page sends frames, look_at_camera hands the latest one to the tool model."""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import server.app as web
import server.vision as vision_mod
import server.web_users as web_users
from server.live_bridge import SidebandToolBridge
from tools.executor import ToolExecutionContext, ToolExecutor
from tools.live_schemas import LIVE_BACKEND_TOOLS
from tools.results import ToolResult

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 100


@pytest.fixture()
def client(monkeypatch):
    return TestClient(web.app)


def _look(user, question="що ти бачиш?"):
    return asyncio.run(
        user.executor.execute("look_at_camera", {"question": question}, ToolExecutionContext(session_id="s"))
    )


def test_result_without_image_stays_plain_json():
    result = ToolResult(ok=True, status="ok", message="Готово.")
    assert result.to_output() == result.to_json()


def test_result_with_image_carries_it_for_the_model():
    output = ToolResult(ok=True, status="ok", message="Кадр.", image_jpeg=JPEG).to_output()
    text, image = output
    assert text["type"] == "input_text" and "Кадр." in text["text"]
    assert image["type"] == "input_image" and image["image_url"].startswith("data:image/jpeg;base64,/9j/")
    assert "image_jpeg" not in ToolResult(ok=True, status="ok", message="x", image_jpeg=JPEG).to_dict()


def test_frame_endpoint_keeps_the_latest_frame_and_forgets_it_when_off(client):
    headers = {"X-Client-Id": "cam-browser-0123456789ab"}
    user = web.users.get(headers["X-Client-Id"])
    assert client.post("/api/frame", headers={**headers, "Content-Type": "image/jpeg"}, content=JPEG).status_code == 200
    assert user.camera_on and user.camera_frame == JPEG
    assert client.delete("/api/frame", headers=headers).status_code == 200
    assert not user.camera_on and user.camera_frame is None


def test_frame_endpoint_takes_only_jpeg_of_sane_size(client):
    headers = {"X-Client-Id": "cam2-browser-0123456789a", "Content-Type": "image/jpeg"}
    assert client.post("/api/frame", headers=headers, content=b"<html>").status_code == 415
    big = b"\xff\xd8\xff" + b"0" * web.MAX_FRAME_BYTES
    assert client.post("/api/frame", headers=headers, content=big).status_code == 413
    assert web.users.get(headers["X-Client-Id"]).camera_frame is None


def test_look_with_the_camera_off_says_so():
    result = _look(web.users.get("cam3-browser-0123456789a"))
    assert not result.ok and result.status == "camera_off" and result.image_jpeg is None
    assert "Камера" in result.message


def test_look_returns_the_fresh_frame_with_the_question():
    user = web.users.get("cam4-browser-0123456789a")
    user.set_camera_frame(JPEG)
    result = _look(user, "що в мене в руці?")
    assert result.ok and result.image_jpeg == JPEG and "що в мене в руці?" in result.message


def test_look_waits_for_the_next_frame_after_a_pause(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam5-browser-0123456789a")
    user.set_camera_frame(b"\xff\xd8\xffold")
    user.camera_frame_at = time.time() - 60  # sent before the pause
    threading.Timer(0.3, user.set_camera_frame, args=(JPEG,)).start()
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_takes_the_frame_snapped_for_it_not_an_older_one(monkeypatch):
    """Prod: the object was held up as they asked, and she described the frame from before."""
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam8-browser-0123456789a")
    user.set_camera_frame(b"\xff\xd8\xffbefore")
    user.camera_frame_at = time.time() - 2  # the last periodic frame: recent, but before the look
    threading.Timer(0.4, user.set_camera_frame, args=(JPEG,)).start()  # the page snaps one for the look
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_takes_the_frame_snapped_when_the_request_went_to_the_backend(monkeypatch):
    """The page snaps as the delegation starts: by the time the backend decides to look, that frame —
    taken after the question — is on the server, and the tool returns it without waiting."""
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 2.0)
    user = web.users.get("cam10-browser-0123456789")
    work_started = time.time() - 1.5
    user.set_camera_frame(JPEG)
    user.camera_frame_at = work_started + 0.4  # uploaded 1.1 s before the call, after the work began
    started = time.monotonic()
    result = asyncio.run(user.executor.execute(
        "look_at_camera", {"question": "що ти бачиш?"},
        ToolExecutionContext(session_id="s", extras={"work_started_at": work_started}),
    ))
    assert result.ok and result.image_jpeg == JPEG and time.monotonic() - started < 0.3


def test_bridge_tells_the_tool_when_the_work_started():
    seen = {}

    def look(args, ctx):
        seen.update(ctx.extras)
        return ToolResult(ok=True, status="ok", message="Кадр.")

    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=ToolExecutor({"look_at_camera": look}))
    bridge._connection = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()))

    async def run():
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {"type": "response.created", "response": {"id": "r"}}})
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "look_at_camera", "arguments": "{}"},
        }})
        await asyncio.gather(*bridge._tool_tasks)

    before = time.time()
    asyncio.run(run())
    assert before - 1 < seen["work_started_at"] <= time.time()


def test_look_falls_back_to_a_recent_frame_when_none_is_snapped(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 0.3)
    user = web.users.get("cam9-browser-0123456789a")
    user.set_camera_frame(JPEG)
    user.camera_frame_at = time.time() - 3  # an old cached page sends only periodic frames
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_look_gives_up_when_frames_stopped(monkeypatch):
    monkeypatch.setattr(web_users, "FRAME_WAIT_S", 0.3)
    user = web.users.get("cam6-browser-0123456789a")
    user.set_camera_frame(JPEG)
    user.camera_frame_at = time.time() - 60  # the tab was closed without turning it off
    result = _look(user)
    assert not result.ok and result.status == "no_frame" and result.image_jpeg is None


def test_web_session_gets_the_camera_tool_and_notes(monkeypatch):
    sent: list[dict] = []

    async def fake_create(*, session, transport):
        sent.append(session)
        return SimpleNamespace(session=SimpleNamespace(id="sess-cam"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "cam7-browser-0123456789a"}
    client = TestClient(web.app)
    for body in ({"sdp": "o"}, {"sdp": "o", "prompt": "v4"}):  # v5 (default) and v4
        assert client.post("/api/session", headers=headers, json=body).status_code == 200
    v5, v4 = sent
    responses = v5["delegation"]["responses"]
    assert "look_at_camera" in [t["name"] for t in responses["tools"]]
    assert "CAMERA:" in responses["instructions"]
    assert "One look per request" in responses["instructions"]
    assert "You cannot see by yourself" in v4["instructions"]
    # Prod: «А це шо?», «Це шо таке?» while holding a speaker up got «не знаю, про що ти питаєш».
    assert "а це шо?" in v4["instructions"] and "never answer «не знаю" in v4["instructions"]
    # Prod: «що ти бачиш» answered without a look — «стіл, ноутбук, чашка» — then «ту саму картину».
    assert "always needs a new look" in v4["instructions"] and "never blame the user" in v4["instructions"]
    # v5 says the same, condensed.
    v5i = v5["instructions"]
    assert "you cannot see by yourself" in v5i and "а це шо?" in v5i and "never ask what they mean" in v5i
    assert "Each look is a new frame" in v5i and "never the user's" in v5i and "one look per request" in v5i
    web.users.get(headers["X-Client-Id"]).bridges.clear()


def test_desktop_tools_have_no_camera():
    assert "look_at_camera" not in [t["name"] for t in LIVE_BACKEND_TOOLS]


def test_bridge_sends_the_frame_as_an_image_output():
    executor = ToolExecutor({
        "look_at_camera": lambda args, ctx: ToolResult(ok=True, status="ok", message="Кадр.", image_jpeg=JPEG),
    })
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=executor)
    conn = SimpleNamespace(response=SimpleNamespace(item=SimpleNamespace(create=AsyncMock()), create=AsyncMock()))
    bridge._connection = conn

    async def run():
        await bridge._handle_event({"type": "response.event", "delegation_id": "d", "event": {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "name": "look_at_camera", "arguments": "{}"},
        }})
        await asyncio.gather(*bridge._tool_tasks)

    asyncio.run(run())
    item = conn.response.item.create.await_args.kwargs["item"]
    assert item["type"] == "function_call_output" and item["output"][1]["type"] == "input_image"


# ── The fast vision model (server/vision.py): a look as text, and the scene line ──

class _FakeBridge:
    """An open call that only takes quiet context."""

    busy = False

    def __init__(self):
        self.think = AsyncMock()


class _FakeVisionClient:
    """client.responses.create that answers like the vision model and records what it was sent."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.responses = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.answers.pop(0) if self.answers else "")


def test_look_returns_what_the_vision_model_saw_as_text(monkeypatch):
    """No image for the backend: it only passes on what the fast model saw (one vision pass less)."""
    fake = _FakeVisionClient("На столі синя чашка з написом «Kyiv».")
    monkeypatch.setattr(vision_mod.vision, "_client", fake)
    user = web.users.get("cam11-browser-0123456789")
    user.set_camera_frame(JPEG)
    result = _look(user, "що на столі?")
    assert result.ok and result.image_jpeg is None
    assert "синя чашка" in result.message and "нічого не додаючи" in result.message
    assert "vision_ms" in result.data
    sent = fake.calls[0]
    assert sent["model"] == vision_mod.vision.model and sent["reasoning"] == {"effort": "none"}
    text, image = sent["input"][0]["content"]
    assert "що на столі?" in text["text"] and image["image_url"].startswith("data:image/jpeg;base64,/9j/")
    assert result.to_output() == result.to_json()  # plain text to the backend


def test_look_falls_back_to_the_image_when_the_vision_model_fails():
    """conftest's vision client always fails: the backend model gets the frame, as before."""
    user = web.users.get("cam12-browser-0123456789")
    user.set_camera_frame(JPEG)
    result = _look(user)
    assert result.ok and result.image_jpeg == JPEG


def test_image_mode_sends_the_frame_to_the_backend(monkeypatch):
    fake = _FakeVisionClient("не має бути викликано")
    monkeypatch.setattr(vision_mod.vision, "_client", fake)
    monkeypatch.setattr(web_users, "CAMERA_LOOK_MODE", "image")
    user = web.users.get("cam13-browser-0123456789")
    user.set_camera_frame(JPEG)
    result = _look(user)
    assert result.image_jpeg == JPEG and not fake.calls


def _watcher(answers, **kw):
    seen: list[str] = []

    def describe(jpeg, previous):
        seen.append(previous)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    return vision_mod.SceneWatcher(describe, **{"min_s": 0.0, "max_s": 100.0, "change": 6.0, **kw}), seen


def test_scene_line_is_sent_only_when_the_view_changed():
    watcher, previous = _watcher(["Людина сидить за столом.", "Людина тримає чашку."])
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    async def run():
        assert await watcher.offer(JPEG, None, send)           # the first frame: always described
        assert not await watcher.offer(JPEG, 1.5, send)        # hardly changed: not even asked
        assert await watcher.offer(JPEG, 20.0, send)           # changed: a new line
    asyncio.run(run())
    assert sent == ["Людина сидить за столом.", "Людина тримає чашку."]
    assert previous == ["", "Людина сидить за столом."]  # the model compares with the last line


def test_scene_unchanged_answer_sends_nothing_and_failures_are_quiet():
    watcher, _ = _watcher([None, RuntimeError("timeout")])
    sent: list[str] = []

    async def send(text):
        sent.append(text)

    async def run():
        return [await watcher.offer(JPEG, None, send), await watcher.offer(JPEG, None, send)]
    assert asyncio.run(run()) == [False, False] and sent == []


def test_scene_waits_between_descriptions_and_heartbeats_without_a_change():
    watcher, _ = _watcher(["a", "b"], min_s=3.0, max_s=20.0)
    watcher.line, watcher._started_at = "a", time.time() - 1
    assert not watcher.wants(50.0, time.time())          # too soon after the last one
    watcher._started_at = time.time() - 5
    assert watcher.wants(50.0, time.time()) and not watcher.wants(2.0, time.time())
    watcher._started_at = time.time() - 25
    assert watcher.wants(2.0, time.time())               # no visible change for long: look again anyway


def test_scene_reset_drops_a_description_already_running():
    watcher, _ = _watcher([])
    sent: list[str] = []

    def slow(jpeg, previous):
        watcher.reset()  # the camera went off while the model looked
        return "Кімната."

    watcher._describe = slow

    async def send(text):
        sent.append(text)

    assert not asyncio.run(watcher.offer(JPEG, None, send)) and sent == [] and watcher.line == ""


def test_scene_vision_unchanged_answer_maps_to_none(monkeypatch):
    v = vision_mod.Vision(_FakeVisionClient("БЕЗ ЗМІН.", "Двоє людей біля вікна."))
    assert v.scene(JPEG, "Двоє людей біля вікна.") is None
    assert v.scene(JPEG, "") == "Двоє людей біля вікна."


def test_frame_from_the_page_feeds_the_scene_line_into_the_open_call(client, monkeypatch):
    monkeypatch.setattr(vision_mod.vision, "_client", _FakeVisionClient("Людина махає рукою."))
    headers = {"X-Client-Id": "cam14-browser-0123456789", "Content-Type": "image/jpeg"}
    user = web.users.get(headers["X-Client-Id"])
    bridge = _FakeBridge()
    user.bridges.add(bridge)
    try:
        assert client.post("/api/frame", headers={**headers, "X-Frame-Change": "12.5"}, content=JPEG).status_code == 200
        for _ in range(50):
            if bridge.think.await_count:
                break
            time.sleep(0.05)
        line = bridge.think.await_args.args[0]
        assert line.startswith("[камера ") and line.endswith("Людина махає рукою.")
        assert client.delete("/api/frame", headers=headers).status_code == 200
        assert "[камера вимкнена]" in bridge.think.await_args.args[0] and user.scene.line == ""
    finally:
        user.bridges.clear()


def test_no_scene_lines_for_the_concierge_or_without_a_call():
    user = web.users.get("cam15-browser-0123456789")
    user.set_camera_frame(JPEG)
    assert not user.watches_scene  # no open call
    user.bridges.add(_FakeBridge())
    try:
        assert user.watches_scene
        user.concierge = True
        assert not user.watches_scene
    finally:
        user.bridges.clear()
        user.concierge = False


def test_bridge_think_sends_quiet_context():
    bridge = SidebandToolBridge(client=SimpleNamespace(), session_id="s1", executor=ToolExecutor({}))
    append = AsyncMock()
    bridge._connection = SimpleNamespace(session=SimpleNamespace(thinking=SimpleNamespace(append=append)))
    asyncio.run(bridge.think("[камера 10:00:00] Кімната."))
    kwargs = append.await_args.kwargs
    assert kwargs["content"] == "[камера 10:00:00] Кімната." and kwargs["event_id"].startswith("think_")


def test_web_session_tells_her_about_the_scene_lines(monkeypatch):
    sent: list[dict] = []

    async def fake_create(*, session, transport):
        sent.append(session)
        return SimpleNamespace(session=SimpleNamespace(id="sess-scene"), transport=SimpleNamespace(sdp="answer"))

    monkeypatch.setattr(web.openai_client.live, "create", fake_create)
    monkeypatch.setattr(web.SidebandToolBridge, "run", lambda self: asyncio.sleep(0))
    headers = {"X-Client-Id": "cam16-browser-0123456789"}
    assert TestClient(web.app).post("/api/session", headers=headers, json={"sdp": "o"}).status_code == 200
    instructions = sent[0]["instructions"]
    assert "Camera lines:" in instructions and "«що ти бачиш?»" in instructions
    assert "something held up or pointed at" in instructions
    web.users.get(headers["X-Client-Id"]).bridges.clear()
