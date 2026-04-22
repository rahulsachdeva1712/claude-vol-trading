"""FastAPI dashboard app.

Endpoints:
    GET  /                         — static SPA (index.html)
    GET  /api/status               — snapshot of both modes
    GET  /api/closed_trades?mode=  — closed cycles for one mode
    POST /api/config               — runtime params (per-mode lots_per_trade)
    POST /api/live/arm             — arm live entries (requires 'confirm': true)
    POST /api/live/disarm          — disarm live entries
    POST /api/live/kill            — kill switch: disarm + flatten live
    POST /api/live/kill/clear      — clear kill flag after manual review
    WS   /ws                       — push stream of engine events
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from ..config import Mode
from ..logging_setup import get_logger

log = get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def create_app(state) -> FastAPI:
    """`state` is a RuntimeState object (see main.py)."""
    app = FastAPI(title="volscalp dashboard")

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        index_file = STATIC_DIR / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>volscalp</h1><p>Static UI missing.</p>")
        return HTMLResponse(index_file.read_text(encoding="utf-8"))

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        return state.snapshot()

    @app.get("/api/closed_trades")
    async def closed_trades(mode: str = "paper") -> dict[str, Any]:
        mode_l = mode.lower()
        if mode_l not in ("paper", "live"):
            raise HTTPException(400, "mode must be 'paper' or 'live'")
        m = Mode(mode_l)
        if m not in state.modes:
            return {"mode": mode_l, "trades": []}
        rows = await state.db.fetch_closed_trades(state.modes[m].session_id)
        return {"mode": mode_l, "trades": rows}

    @app.post("/api/config")
    async def update_config(req: Request) -> dict[str, Any]:
        body = await req.json()
        changed = await state.update_runtime_config(body)
        return {"updated": changed}

    @app.post("/api/live/arm")
    async def live_arm(req: Request) -> dict[str, Any]:
        body = await req.json() if req.headers.get("content-length") else {}
        if state.cfg.dashboard.live_mode_require_confirm and not body.get("confirm"):
            raise HTTPException(400, "live arm requires 'confirm': true")
        try:
            await state.arm_live()
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        return {"armed": True}

    @app.post("/api/live/disarm")
    async def live_disarm() -> dict[str, Any]:
        await state.disarm_live()
        return {"armed": False}

    @app.post("/api/live/kill")
    async def live_kill(req: Request) -> dict[str, Any]:
        body = await req.json() if req.headers.get("content-length") else {}
        if state.cfg.dashboard.kill_switch_require_confirm and not body.get("confirm"):
            raise HTTPException(400, "kill switch requires 'confirm': true")
        try:
            await state.trigger_kill_switch()
        except RuntimeError as e:
            raise HTTPException(400, str(e)) from e
        return {"killed": True}

    @app.post("/api/live/kill/clear")
    async def live_kill_clear() -> dict[str, Any]:
        await state.clear_kill_switch()
        return {"killed": False}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        q: asyncio.Queue = state.bus.subscribe()
        try:
            await ws.send_text(json.dumps({"kind": "snapshot", "payload": state.snapshot()}))
            while True:
                msg = await q.get()
                await ws.send_text(json.dumps(msg, default=str))
        except WebSocketDisconnect:
            pass
        except Exception as e:  # noqa: BLE001
            log.warning("ws_error", error=str(e))
        finally:
            state.bus.unsubscribe(q)

    return app
