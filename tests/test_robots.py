"""«Роботи»: релей між bridge'ами роботів і сторінкою з джойстиком (server/robots.py)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import server.app as web
from server import robots

ROBOT = "/ws/robot"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.delenv("ROBOT_TOKEN", raising=False)
    monkeypatch.setenv("FRONTEND_ORIGINS", "*")
    monkeypatch.setattr(robots, "relay", robots.RobotRelay())  # кожен тест — з чистим списком
    return TestClient(web.app)


def op(name: str) -> str:
    return f"/ws/operator?name={name}"


def register(ws, robot_id: str, kind: str = "dog", actions=("stand", "sit")) -> None:
    ws.send_json({"type": "register", "robot_id": robot_id, "name": robot_id.upper(), "kind": kind,
                  "actions": list(actions), "max_speed": {"vx": 0.5, "vy": 0.3, "yaw": 0.8}})
    assert ws.receive_json() == {"type": "registered", "robot_id": robot_id}


def until(ws, kind: str) -> dict:
    """Наступне повідомлення цього типу (списки роботів приходять між іншими)."""
    for _ in range(20):
        msg = ws.receive_json()
        if msg["type"] == kind:
            return msg
    raise AssertionError(f"no {kind}")


def test_operator_needs_only_a_name(client):
    """Паролів немає — лише ім'я, щоб усі бачили, хто керує. Без імені не пускає."""
    with client.websocket_connect(op("vlad")) as o:
        assert o.receive_json() == {"type": "hello", "name": "vlad"}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/operator") as ws:
        ws.receive_json()


def test_robot_password_is_optional(client, monkeypatch):
    with client.websocket_connect(ROBOT) as go2:  # без ROBOT_TOKEN — будь-який bridge
        register(go2, "go2-01")
    monkeypatch.setenv("ROBOT_TOKEN", "robot-pass")  # задали на Render — тепер лише з ним
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws/robot?token=nope") as ws:
        ws.receive_json()
    with client.websocket_connect("/ws/robot?token=robot-pass") as go2:
        register(go2, "go2-01")


def test_operator_from_a_foreign_site_is_refused(client, monkeypatch):
    monkeypatch.setenv("FRONTEND_ORIGINS", "https://eva.example")
    foreign = {"origin": "https://evil.example"}
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(op("vlad"), headers=foreign) as ws:
        ws.receive_json()
    with client.websocket_connect(op("vlad"), headers={"origin": "https://eva.example"}) as ws:
        assert ws.receive_json()["type"] == "hello"


def test_two_operators_two_robots_each_controls_their_own(client):
    with client.websocket_connect(ROBOT) as go2, client.websocket_connect(ROBOT) as r1:
        register(go2, "go2-01")
        register(r1, "r1-01", kind="humanoid", actions=("wave",))
        assert {r["robot_id"] for r in client.get("/api/robots").json()["robots"]} == {"go2-01", "r1-01"}

        with client.websocket_connect(op("vlad")) as vlad, client.websocket_connect(op("colleague")) as col:
            vlad.send_json({"type": "claim", "robot_id": "go2-01"})
            assert until(vlad, "claim_result")["ok"] is True
            col.send_json({"type": "claim", "robot_id": "r1-01"})
            assert until(col, "claim_result")["ok"] is True

            # чужий робот: зайнятий, і команди руху до нього не доходять
            col.send_json({"type": "claim", "robot_id": "go2-01"})
            busy = until(col, "claim_result")
            assert busy["ok"] is False and busy["reason"] == "busy" and busy["operator"] == "vlad"
            col.send_json({"type": "move", "robot_id": "go2-01", "vx": 1})
            assert until(col, "error")["robot_id"] == "go2-01"

            # свій — доходить, лише відомі дії, сміття у швидкостях → 0
            vlad.send_json({"type": "move", "robot_id": "go2-01", "vx": 0.4, "vy": "x", "yaw": float("nan")})
            assert go2.receive_json() == {"type": "move", "vx": 0.4, "vy": 0.0, "yaw": 0.0}
            vlad.send_json({"type": "action", "robot_id": "go2-01", "name": "rm -rf"})
            vlad.send_json({"type": "action", "robot_id": "go2-01", "name": "sit"})
            assert go2.receive_json() == {"type": "action", "name": "sit"}
            col.send_json({"type": "move", "robot_id": "r1-01", "vx": 0.2, "vy": 0, "yaw": 0})
            assert r1.receive_json()["vx"] == 0.2


def test_stop_works_for_anyone_and_takeover_stops_first(client):
    with client.websocket_connect(ROBOT) as go2:
        register(go2, "go2-01")
        with client.websocket_connect(op("vlad")) as vlad, client.websocket_connect(op("colleague")) as col:
            vlad.send_json({"type": "claim", "robot_id": "go2-01"})
            until(vlad, "claim_result")
            col.send_json({"type": "stop", "robot_id": "go2-01"})
            assert go2.receive_json() == {"type": "stop"}

            col.send_json({"type": "takeover", "robot_id": "go2-01"})
            assert until(col, "claim_result")["ok"] is True
            assert go2.receive_json() == {"type": "stop"}
            assert "перехопив" in until(vlad, "event")["text"]
            vlad.send_json({"type": "move", "robot_id": "go2-01", "vx": 0.3})
            assert until(vlad, "error")


def test_closing_the_tab_stops_the_robot_and_frees_it(client):
    with client.websocket_connect(ROBOT) as go2:
        register(go2, "go2-01")
        with client.websocket_connect(op("vlad")) as vlad:
            vlad.send_json({"type": "claim", "robot_id": "go2-01"})
            until(vlad, "claim_result")
        assert go2.receive_json() == {"type": "stop"}
        assert client.get("/api/robots").json()["robots"][0]["operator"] is None


def test_robot_gone_leaves_the_list(client):
    with client.websocket_connect(ROBOT) as go2:
        register(go2, "go2-01")
    assert client.get("/api/robots").json()["robots"] == []


def test_silent_robot_is_reaped():
    """Bridge завис без розриву з'єднання: без heartbeat він зникає зі списку і з'єднання закривається."""
    import asyncio

    class FakeWs:
        closed = False

        async def close(self):
            self.closed = True

    relay = robots.RobotRelay()
    ws = FakeWs()
    relay.robots["go2-01"] = {"ws": ws, "info": {}, "state": {}, "last_seen": 100.0, "operator": "vlad"}
    asyncio.run(relay.reap(now=100.0 + robots.HEARTBEAT_TIMEOUT_S - 0.5))
    assert "go2-01" in relay.robots
    asyncio.run(relay.reap(now=100.0 + robots.HEARTBEAT_TIMEOUT_S + 0.5))
    assert relay.robots == {} and ws.closed
