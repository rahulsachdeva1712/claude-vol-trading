"""FastAPI dashboard app.

Endpoints:
    GET  /                — static SPA (index.html)
    GET  /api/status      — snapshot of KPIs + current cycles
    POST /api/mode        — switch paper/live
    POST /api/kill        — kill switch (force close + halt entries)
    POST /api/config      — update runtime params (lots_per_trade, etc.)
    WS   /ws              — push stream of engine events
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
from .event_bus import EventBus

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

    @app.post("/api/mode")
    async def set_mode(req: Request) -> dict[str, Any]:
        body = await req.json()
        new = str(body.get("mode", "")).lower()
        if new not in ("paper", "live"):
            raise HTTPException(400, "mode must be 'paper' or 'live'")
        if new == "live" and not body.get("confirm"):
            raise HTTPException(400, "live mode requires 'confirm': true")
        await state.set_mode(Mode(new))
        return {"mode": new}

    @app.post("/api/kill")
    async def kill(req: Request) -> dict[str, Any]:
        body = await req.json() if req.headers.get("content-length") else {}
        if state.cfg.dashboard.kill_switch_require_confirm and not body.get("confirm"):
            raise HTTPException(400, "kill switch requires 'confirm': true")
        await state.trigger_kill_switch()
        return {"killed": True}

    @app.post("/api/config")
    async def update_config(req: Request) -> dict[str, Any]:
        body = await req.json()
        changed = await state.update_runtime_config(body)
        return {"updated": changed}

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        q: asyncio.Queue = state.bus.subscribe()
        # Send initial snapshot.
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
