"""«Роботи»: керування роботами джойстиком із сайту, поруч із голосовим агентом.

    bridge біля робота --wss--> /ws/robot[?token=ROBOT_TOKEN]
    сторінка (web/robots.js) --wss--> /ws/operator?name=vlad

Релей лише з'єднує одних з іншими: хто онлайн, хто чим керує (один оператор на робота), і пересилає
команди. Ліміти швидкості й deadman — у самому bridge (robot_bridge/bridge.py): зв'язок може зникнути,
а робот мусить зупинитися й без сервера. Стан у пам'яті: деплой скидає замки, bridge'і перепідключаються самі.

Оператори без паролів, як і весь сайт: лише ім'я, щоб усі бачили, хто яким роботом керує.
Env: ROBOT_TOKEN — необов'язковий пароль для bridge'ів (порожній = будь-який bridge може підключитись).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("server.robots")

HEARTBEAT_TIMEOUT_S = 3.0  # без heartbeat стільки — робот офлайн
_ROBOT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_ACTION_RE = re.compile(r"^[a-z0-9_]{1,32}$")

router = APIRouter()


def _token_ok(given: str | None, expected: str) -> bool:
    return hmac.compare_digest((given or "").encode(), expected.encode())


def _origin_ok(origin: str | None) -> bool:
    """CORS не діє на WebSocket: сторінку оператора пускаємо лише з FRONTEND_ORIGINS (як і REST)."""
    allowed = [o.strip().rstrip("/") for o in os.getenv("FRONTEND_ORIGINS", "*").split(",") if o.strip()]
    if not origin or "*" in allowed:
        return True
    return origin.rstrip("/") in allowed


def _num(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return f if f == f and abs(f) < 1e3 else 0.0  # NaN і сміття → 0; ліміти обрізає bridge


class RobotRelay:
    def __init__(self) -> None:
        self.robots: dict[str, dict] = {}  # robot_id -> {ws, info, state, last_seen, operator}
        self.operators: dict[WebSocket, str] = {}  # ws -> ім'я
        self._reaper: asyncio.Task | None = None

    # ── що бачать оператори ──
    def listing(self) -> list[dict]:
        return [
            {
                "robot_id": rid,
                "name": r["info"].get("name") or rid,
                "kind": r["info"].get("kind", "generic"),
                "actions": r["info"].get("actions", []),
                "max_speed": r["info"].get("max_speed"),
                "operator": r["operator"],
                "state": r["state"],
            }
            for rid, r in self.robots.items()
        ]

    async def _send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg, ensure_ascii=False))
        except Exception:  # вкладка вже закрита — її прибере власний обробник
            pass

    async def broadcast(self, msg: dict | None = None) -> None:
        msg = msg or {"type": "robots", "robots": self.listing()}
        await asyncio.gather(*(self._send(ws, msg) for ws in list(self.operators)))

    async def stop_robot(self, rid: str) -> None:
        r = self.robots.get(rid)
        if r:
            await self._send(r["ws"], {"type": "stop"})

    async def release_all_of(self, name: str) -> None:
        changed = False
        for rid, r in self.robots.items():
            if r["operator"] == name:
                r["operator"] = None
                await self.stop_robot(rid)
                changed = True
        if changed:
            await self.broadcast()

    # ── роботи, що замовкли ──
    async def reap(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        dead = [rid for rid, r in self.robots.items() if now - r["last_seen"] > HEARTBEAT_TIMEOUT_S]
        for rid in dead:
            r = self.robots.pop(rid)
            logger.info("robots.offline robot=%s", rid)
            try:
                await r["ws"].close()
            except Exception:
                pass
        if dead:
            await self.broadcast()

    def start(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.create_task(self._reap_forever())

    def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            await self.reap()

    # ── bridge ──
    async def robot_session(self, ws: WebSocket) -> None:
        robot_token = os.getenv("ROBOT_TOKEN", "").strip()
        if robot_token and not _token_ok(ws.query_params.get("token"), robot_token):
            await ws.close(code=4401)
            return
        await ws.accept()
        rid: str | None = None
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                t = msg.get("type")
                if t == "register":
                    new_id = str(msg.get("robot_id", ""))
                    if not _ROBOT_ID_RE.match(new_id):
                        await self._send(ws, {"type": "error", "text": "bad robot_id"})
                        continue
                    rid = new_id
                    old = self.robots.get(rid)
                    actions = [a for a in msg.get("actions", []) if isinstance(a, str) and _ACTION_RE.match(a)]
                    self.robots[rid] = {
                        "ws": ws,
                        "info": {
                            "name": str(msg.get("name") or rid)[:40],
                            "kind": str(msg.get("kind") or "generic")[:20],
                            "actions": actions[:16],
                            "max_speed": msg.get("max_speed"),
                        },
                        "state": {},
                        "last_seen": time.time(),
                        "operator": old["operator"] if old else None,  # перепідключився — замок лишається
                    }
                    logger.info("robots.online robot=%s kind=%s", rid, self.robots[rid]["info"]["kind"])
                    await self._send(ws, {"type": "registered", "robot_id": rid})
                    await self.broadcast()
                elif rid in self.robots and t in ("heartbeat", "state"):
                    r = self.robots[rid]
                    r["last_seen"] = time.time()
                    state = msg.get("state") if isinstance(msg.get("state"), dict) else {}
                    if state != r["state"]:
                        r["state"] = state
                        await self.broadcast()
                elif rid in self.robots and t == "event":
                    await self.broadcast({"type": "event", "robot_id": rid, "text": str(msg.get("text", ""))[:200]})
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            if rid and self.robots.get(rid, {}).get("ws") is ws:
                self.robots.pop(rid, None)
                logger.info("robots.offline robot=%s", rid)
                await self.broadcast()

    # ── оператор ──
    async def operator_session(self, ws: WebSocket) -> None:
        if not _origin_ok(ws.headers.get("origin")):
            await ws.close(code=4403)
            return
        name = re.sub(r"\s+", " ", ws.query_params.get("name") or "").strip()[:32]
        if not name:
            await ws.close(code=4400)
            return
        await ws.accept()
        self.operators[ws] = name
        await self._send(ws, {"type": "hello", "name": name})
        await self._send(ws, {"type": "robots", "robots": self.listing()})
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                await self._operator_message(ws, name, msg)
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            self.operators.pop(ws, None)
            if name not in self.operators.values():  # остання вкладка цього оператора
                await self.release_all_of(name)

    async def _operator_message(self, ws: WebSocket, name: str, msg: dict) -> None:
        t = msg.get("type")
        rid = msg.get("robot_id")
        r = self.robots.get(rid) if isinstance(rid, str) else None

        if t == "claim":
            if r is None:
                await self._send(ws, {"type": "claim_result", "robot_id": rid, "ok": False, "reason": "offline"})
            elif r["operator"] in (None, name):
                r["operator"] = name
                logger.info("robots.claim robot=%s by=%s", rid, name)
                await self._send(ws, {"type": "claim_result", "robot_id": rid, "ok": True})
                await self.broadcast()
            else:
                await self._send(ws, {"type": "claim_result", "robot_id": rid, "ok": False,
                                      "reason": "busy", "operator": r["operator"]})
        elif t == "takeover" and r is not None:
            prev = r["operator"]
            r["operator"] = name
            await self.stop_robot(rid)
            logger.info("robots.takeover robot=%s from=%s by=%s", rid, prev, name)
            await self._send(ws, {"type": "claim_result", "robot_id": rid, "ok": True})
            await self.broadcast()
            if prev and prev != name:
                await self.broadcast({"type": "event", "robot_id": rid, "text": f"{name} перехопив керування у {prev}"})
        elif t == "release" and r is not None and r["operator"] == name:
            r["operator"] = None
            await self.stop_robot(rid)
            await self.broadcast()
        elif t == "stop" and r is not None:
            await self.stop_robot(rid)  # аварійний стоп — будь-хто, не лише власник замка
        elif t in ("move", "action") and r is not None:
            if r["operator"] != name:
                await self._send(ws, {"type": "error", "robot_id": rid, "text": "Цим роботом зараз керуєте не ви"})
                return
            if t == "move":
                cmd = {"type": "move", "vx": _num(msg.get("vx")), "vy": _num(msg.get("vy")), "yaw": _num(msg.get("yaw"))}
            else:
                action = msg.get("name")
                if action not in r["info"]["actions"]:
                    return
                cmd = {"type": "action", "name": action}
            await self._send(r["ws"], cmd)


relay = RobotRelay()


@router.websocket("/ws/robot")
async def ws_robot(ws: WebSocket) -> None:
    await relay.robot_session(ws)


@router.websocket("/ws/operator")
async def ws_operator(ws: WebSocket) -> None:
    await relay.operator_session(ws)


@router.get("/api/robots")
def robots_list() -> dict:
    """Хто онлайн — для панелі до відкриття WebSocket (і для перевірки з curl)."""
    return {"robots": relay.listing()}
