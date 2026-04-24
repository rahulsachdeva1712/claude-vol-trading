"""FastAPI dashboard app.

Endpoints:
    GET  /                         — static SPA (index.html)
    GET  /api/status               — snapshot of both modes
    GET  /api/closed_trades?mode=  — closed cycles for one mode (today only)
    GET  /api/equity_curve?mode=   — per-day cumulative P&L across all sessions
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
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

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

    # Dev tool on 127.0.0.1 only (see FRD §11.1/§12.3). Tell the browser
    # never to cache our static bundle or index page — otherwise a code
    # deploy leaves the old app.js in place and the new endpoints look
    # broken until the user hard-refreshes. The perf hit is trivial on
    # localhost.
    @app.middleware("http")
    async def _no_cache_static(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

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

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """Diagnostic counters — useful to confirm the feed is live and
        signals are being evaluated even when no cycles have fired yet."""
        md = state.market_data
        feed = state.feed
        out: dict[str, Any] = {
            "feed_connected": bool(feed and getattr(feed, "_ws", None)),
            "feed_subscriptions": len(getattr(feed, "_subscriptions", set())) if feed else 0,
            "feed_first_tick_seen": bool(getattr(feed, "_first_tick_logged", False)) if feed else False,
            "ticks_total": getattr(md, "ticks_total", 0) if md else 0,
            "bars_closed_total": getattr(md, "bars_closed_total", 0) if md else 0,
            "unique_securities_seen": len(getattr(md, "_unique_securities", set())) if md else 0,
            "spots": {k: round(v, 2) for k, v in (getattr(md, "_spot_by_name", {}) or {}).items() if v > 0},
            "engines": {},
        }
        for m, tree in state.modes.items():
            for idx, eng in tree.engines.items():
                out["engines"][f"{m.value}-{idx.value}"] = {
                    "armed": eng.armed.is_set(),
                    "cycle_no": eng.cycle_counter,
                    "has_open_cycle": eng.current_cycle is not None,
                    "cycle_state": eng.current_cycle.state.value if eng.current_cycle else None,
                    "expiry": eng.expiry,
                }
        return out

    @app.get("/api/closed_trades")
    async def closed_trades(mode: str = "paper") -> dict[str, Any]:
        mode_l = mode.lower()
        if mode_l not in ("paper", "live"):
            raise HTTPException(400, "mode must be 'paper' or 'live'")
        m = Mode(mode_l)
        if m not in state.modes:
            return {"mode": mode_l, "trades": []}
        # Scope by (mode, session_date) rather than session_id so a
        # mid-session process restart (which opens a new session row)
        # still shows today's full cycle history. Matches the engine's
        # startup backfill of KPI counters.
        tz = ZoneInfo(state.cfg.session.timezone)
        today_iso = datetime.now(tz).date().isoformat()
        rows = await state.db.fetch_closed_trades_today(mode_l, today_iso)
        return {"mode": mode_l, "trades": rows}

    @app.get("/api/equity_curve")
    async def equity_curve(mode: str = "paper") -> dict[str, Any]:
        """One point per trading day with running cumulative P&L across
        every session ever recorded for the mode (FRD §10.1). The
        rightmost `cum_pnl` value equals the KPI strip's "Cumulative
        P&L" figure — the KPI is just this series' tail."""
        mode_l = mode.lower()
        if mode_l not in ("paper", "live"):
            raise HTTPException(400, "mode must be 'paper' or 'live'")
        days = await state.db.fetch_daily_pnl(mode_l)
        return {"mode": mode_l, "days": days}

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

    @app.post("/api/orphans/kill")
    async def orphan_kill(req: Request) -> dict[str, Any]:
        """Square off a single orphan Dhan position by securityId.
        Body: ``{"security_id": int}``. Only touches positions the
        reconciler has classified as orphans (FRD §8.2).
        """
        body = await req.json() if req.headers.get("content-length") else {}
        try:
            sid = int(body.get("security_id", 0))
        except (TypeError, ValueError):
            raise HTTPException(400, "security_id must be an int") from None
        if not sid:
            raise HTTPException(400, "security_id required")
        if state.reconciler is None:
            raise HTTPException(400, "reconciler not available (live mode disabled)")
        result = await state.reconciler.kill_orphan(sid)
        if not result.get("ok"):
            # Return 200 with ok:false so the dashboard can show the
            # reason (e.g. 'not_an_orphan') without a network error.
            pass
        return result

    @app.post("/api/orphans/auto_kill")
    async def orphan_auto_kill(req: Request) -> dict[str, Any]:
        """Toggle the reconciler's auto-kill mode.
        Body: ``{"enabled": bool}``. When on, confirmed orphans that
        persist past the grace window (~13s) are auto-SELL'd at MARKET.
        """
        body = await req.json() if req.headers.get("content-length") else {}
        enabled = bool(body.get("enabled", False))
        if state.reconciler is None:
            raise HTTPException(400, "reconciler not available (live mode disabled)")
        state.reconciler.set_auto_kill(enabled)
        return {"enabled": state.reconciler.auto_kill_enabled}

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
