"""Application entrypoint — wires every module into a single asyncio process.

Responsibilities:
    1. Process hygiene (PID file, stale cleanup, signal handlers).
    2. Config + secrets load, logging setup.
    3. Dhan instrument master download (or cached).
    4. Dhan WebSocket connection and ATM ± N strike subscription per index.
    5. Market data service + candle builder + spot tracker.
    6. Order manager (paper or live backend).
    7. Strategy engines (one per index).
    8. FastAPI dashboard (uvicorn running in the same loop).
    9. Optional Dhan position reconciler (live mode only).
   10. Graceful shutdown on SIGINT/SIGTERM.

The RuntimeState class is the single object the dashboard talks to — all
mode switches, kill-switch triggers, and lots-per-trade updates go
through it.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import uvicorn

from .broker.dhan_client import DhanClient
from .broker.dhan_ws import DhanMarketFeed
from .broker.instruments import InstrumentMaster
from .config import AppConfig, EnvSecrets, IndexName, Mode, load_config, load_secrets
from .dashboard.app import create_app
from .dashboard.event_bus import EventBus
from .logging_setup import configure_logging, get_logger
from .market_data.feed import MarketDataService
from .models import OptionType, Tick
from .orders.manager import LiveBackend, OrderManager, PaperBackend
from .orders.reconciler import PositionReconciler
from .process_guard import (
    install_signal_handlers,
    register_shutdown,
    request_shutdown,
    run_shutdown_callbacks,
    setup_process_hygiene,
    shutdown_event,
)
from .storage.db import Database
from .strategy.engine import StrategyEngine

log = get_logger(__name__)

# Dhan index security IDs (segment IDX_I). Stable across releases.
INDEX_FEED = {
    IndexName.NIFTY: ("IDX_I", 13),
    IndexName.BANKNIFTY: ("IDX_I", 25),
}

# Number of strikes on each side of ATM we pre-subscribe per index.
# 6 is the strategy offset; 15 gives slack for lazy legs chasing fresh ATMs.
PRE_SUBSCRIBE_RANGE = 15


class RuntimeState:
    """Mutable runtime state shared between the dashboard and the engines.

    Anything the UI can change at runtime (mode, lots, kill switch) lives
    here. Config objects remain frozen — we mirror the dynamic fields in
    this object and engines read from it on every decision.
    """

    def __init__(
        self,
        cfg: AppConfig,
        secrets: EnvSecrets,
        db: Database,
        bus: EventBus,
        instruments: InstrumentMaster,
        feed: DhanMarketFeed | None,
    ):
        self.cfg = cfg
        self.secrets = secrets
        self.db = db
        self.bus = bus
        self.instruments = instruments
        self.feed = feed
        self.mode: Mode = cfg.mode
        self.lots_per_trade: int = cfg.engine.lots_per_trade
        self.engines: dict[IndexName, StrategyEngine] = {}
        self.market_data: MarketDataService | None = None
        self.reconciler: PositionReconciler | None = None
        self.dhan_client: DhanClient | None = None
        self.kill_switch: asyncio.Event = asyncio.Event()
        self._mode_lock = asyncio.Lock()

    def register_engine(self, idx: IndexName, engine: StrategyEngine) -> None:
        self.engines[idx] = engine

    # ---- snapshot consumed by /api/status + WS initial payload ----------------

    def snapshot(self) -> dict[str, Any]:
        engine_kpis: dict[str, dict[str, Any]] = {}
        realized_total = 0.0
        unrealized_total = 0.0
        closed_total = 0
        wins_total = 0
        losses_total = 0
        peak_total = 0.0
        trough_total = 0.0

        for idx, eng in self.engines.items():
            kpi = eng.kpi_snapshot()
            kpi["legs"] = eng._legs_snapshot()  # noqa: SLF001 — intentional read
            if eng.current_cycle:
                kpi["cycle_no"] = eng.current_cycle.cycle_id
                kpi["state"] = eng.current_cycle.state.value
                kpi["mtm"] = round(eng.current_cycle.mtm, 2)
                kpi["atm"] = eng.current_cycle.atm_at_start
                kpi["locked"] = eng.current_cycle.lock_activated
                kpi["floor"] = eng.current_cycle.lock_floor
                if self.market_data:
                    kpi["spot"] = self.market_data.spot(idx.value)
            engine_kpis[idx.value] = kpi
            realized_total += kpi.get("realized_pnl", 0.0)
            unrealized_total += kpi.get("unrealized_pnl", 0.0)
            closed_total += kpi.get("closed_cycles", 0)
            wins_total += kpi.get("wins", 0)
            losses_total += kpi.get("losses", 0)
            peak_total += kpi.get("peak_session_pnl", 0.0)
            trough_total += kpi.get("trough_session_pnl", 0.0)

        total_trades = wins_total + losses_total
        win_rate = (wins_total / total_trades * 100.0) if total_trades else 0.0

        # Profit factor = gross_profit / gross_loss (fallback: from realized).
        gross_profit = sum(max(0.0, eng.realized_pnl) for eng in self.engines.values() if eng.win_count)
        gross_loss = abs(sum(min(0.0, eng.realized_pnl) for eng in self.engines.values() if eng.loss_count))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0

        open_positions = sum(
            1 for eng in self.engines.values()
            for leg in (eng.current_cycle.legs.values() if eng.current_cycle else [])
            if leg.status.value == "ACTIVE"
        )

        return {
            "mode": self.mode.value,
            "lots_per_trade": self.lots_per_trade,
            "kill_switch": self.kill_switch.is_set(),
            "engines": engine_kpis,
            "aggregate": {
                "realized_pnl": round(realized_total, 2),
                "unrealized_pnl": round(unrealized_total, 2),
                "total_mtm": round(realized_total + unrealized_total, 2),
                "cumulative_pnl": round(realized_total, 2),
                "open_positions": open_positions,
                "closed_cycles": closed_total,
                "win_rate_pct": round(win_rate, 1),
                "avg_pnl_per_trade": round(realized_total / closed_total, 2) if closed_total else 0.0,
                "profit_factor": round(profit_factor, 2),
                "max_drawdown": round(trough_total, 2),
            },
        }

    # ---- mutations from dashboard --------------------------------------------

    async def set_mode(self, new_mode: Mode) -> None:
        async with self._mode_lock:
            if new_mode == self.mode:
                return
            log.warning("runtime_mode_switch_requested", old=self.mode.value, new=new_mode.value)

            # Switching into live requires a functioning Dhan client.
            if new_mode == Mode.LIVE:
                if not self.secrets.has_dhan_credentials():
                    raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing in .env")
                if self.dhan_client is None:
                    self.dhan_client = DhanClient(
                        self.secrets.DHAN_CLIENT_ID,
                        self.secrets.DHAN_ACCESS_TOKEN,
                        timeout_s=self.cfg.broker.order_timeout_s,
                    )

            new_backend = (
                LiveBackend(self.dhan_client)
                if new_mode == Mode.LIVE and self.dhan_client is not None
                else PaperBackend(
                    price_lookup=(self.market_data.last_ltp if self.market_data else lambda _sid: 0.0),
                    slippage_bps=self.cfg.paper.slippage_bps,
                )
            )
            for eng in self.engines.values():
                eng.orders.mode = new_mode
                eng.orders.backend = new_backend
            self.mode = new_mode
            await self.bus.broadcast("mode_change", {"mode": new_mode.value})

    async def trigger_kill_switch(self) -> None:
        log.warning("kill_switch_triggered")
        self.kill_switch.set()
        await self.bus.broadcast("kill_switch", {"active": True})

    async def update_runtime_config(self, body: dict[str, Any]) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        if "lots_per_trade" in body:
            try:
                lots = max(1, int(body["lots_per_trade"]))
            except (TypeError, ValueError):
                raise RuntimeError("lots_per_trade must be an integer >= 1") from None
            self.lots_per_trade = lots
            self.cfg.engine.lots_per_trade = lots
            changed["lots_per_trade"] = lots
            log.info("runtime_lots_updated", lots=lots)
        if changed:
            await self.bus.broadcast("runtime_config", changed)
        return changed


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

async def _run(cfg: AppConfig) -> int:
    secrets = load_secrets()
    configure_logging(level=cfg.logging.level, json_output=cfg.logging.json_output, path=cfg.logging.path)

    pid_file = Path.home() / ".claude-vol-trading" / "run" / "app.pid"
    setup_process_hygiene(pid_file)

    loop = asyncio.get_running_loop()
    install_signal_handlers(loop)

    # Persistence.
    db = Database(cfg.persistence.sqlite_path)
    await db.open()
    session_id = await db.start_session(
        index_name="+".join(i.value for i in cfg.indices),
        mode=cfg.mode.value,
    )
    log.info("session_started", session_id=session_id, mode=cfg.mode.value)

    # Instrument master.
    instruments = InstrumentMaster(Path(secrets.VOLSCALP_DATA_DIR) / "instruments")
    try:
        await instruments.ensure_loaded()
    except Exception as e:  # noqa: BLE001
        log.error("instrument_master_load_failed", error=str(e))
        await db.close()
        return 2

    # Tick queue + market data service.
    tick_queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=8192)
    market_data = MarketDataService(tick_queue)

    # Register spot security IDs.
    for idx, (seg, sid) in INDEX_FEED.items():
        market_data.register_spot(sid, idx.value)

    # Dhan market feed (paper mode can still use the live feed — strategy
    # logic is identical; only order placement differs).
    feed: DhanMarketFeed | None = None
    if secrets.has_dhan_credentials():
        feed = DhanMarketFeed(
            client_id=secrets.DHAN_CLIENT_ID,
            access_token=secrets.DHAN_ACCESS_TOKEN,
            out_queue=tick_queue,
            backoffs=cfg.broker.ws_reconnect_backoff_s,
            feed_mode=cfg.broker.feed_mode,
        )
    else:
        log.warning("dhan_credentials_missing — running without live feed (paper w/o data)")

    # Order backend.
    dhan_client: DhanClient | None = None
    if cfg.mode == Mode.LIVE:
        if not secrets.has_dhan_credentials():
            log.error("cannot_start_live_without_credentials")
            await db.close()
            return 3
        dhan_client = DhanClient(
            secrets.DHAN_CLIENT_ID,
            secrets.DHAN_ACCESS_TOKEN,
            timeout_s=cfg.broker.order_timeout_s,
        )
        backend = LiveBackend(dhan_client)
    else:
        backend = PaperBackend(price_lookup=market_data.last_ltp, slippage_bps=cfg.paper.slippage_bps)

    async def _order_request_cb(req):
        # Best-effort persistence of the order request (cycle/leg IDs set by engine).
        await db.record_order_request(req, None, None)

    async def _order_report_cb(report):
        await db.record_order_report(report)

    order_mgr = OrderManager(
        mode=cfg.mode, backend=backend, on_request=_order_request_cb, on_report=_order_report_cb
    )

    # Event bus + runtime state.
    bus = EventBus()
    state = RuntimeState(cfg, secrets, db, bus, instruments, feed)
    state.market_data = market_data
    state.dhan_client = dhan_client

    # Strategy engines (one per index).
    engines: list[StrategyEngine] = []
    for idx in cfg.indices:
        engine = StrategyEngine(
            underlying=idx,
            cfg=cfg,
            market_data=market_data,
            instruments=instruments,
            order_mgr=order_mgr,
            db=db,
            kill_switch=state.kill_switch,
            event_bus=bus,
        )
        engines.append(engine)
        state.register_engine(idx, engine)

    # Pre-subscribe option strikes around ATM for each index so ticks are
    # flowing by the time cycles start.
    if feed is not None:
        initial_subs: list[tuple[str, int]] = []
        for idx in cfg.indices:
            inst_cfg = cfg.instruments[idx]
            expiry = instruments.pick_expiry(idx, cfg.expiry)
            # We don't have spot yet — subscribe to index feed first, then
            # ATM ± N once spot arrives (via `dynamic_subscribe` below).
            seg, sid = INDEX_FEED[idx]
            initial_subs.append((seg, sid))
            # Also seed with a broad range of option strikes using any known
            # strikes for the expiry (reduces first-cycle subscription delay).
            strikes = instruments.strikes_for(idx.value, expiry)
            if strikes:
                # Pick a central slice (30 strikes) to keep initial sub size small.
                mid = len(strikes) // 2
                slice_ = strikes[max(0, mid - 30): mid + 30]
                for k in slice_:
                    for ot in (OptionType.CE, OptionType.PE):
                        opt = instruments.get(idx.value, expiry, k, ot)
                        if opt:
                            initial_subs.append((opt.exchange_segment, opt.security_id))
        feed_task = asyncio.create_task(feed.run(initial_subs=initial_subs), name="dhan_ws")
    else:
        feed_task = None

    async def dynamic_subscriber() -> None:
        """Once we see spot for an index, subscribe ATM ± N strikes."""
        seen: set[IndexName] = set()
        while not shutdown_event().is_set():
            for idx in cfg.indices:
                if idx in seen:
                    continue
                spot = market_data.spot(idx.value)
                if spot <= 0:
                    continue
                inst_cfg = cfg.instruments[idx]
                expiry = instruments.pick_expiry(idx, cfg.expiry)
                atm = instruments.nearest_strike(idx.value, expiry, spot, inst_cfg.strike_interval)
                lo = atm - PRE_SUBSCRIBE_RANGE * inst_cfg.strike_interval
                hi = atm + PRE_SUBSCRIBE_RANGE * inst_cfg.strike_interval
                subs: list[tuple[str, int]] = []
                for k in range(lo, hi + inst_cfg.strike_interval, inst_cfg.strike_interval):
                    for ot in (OptionType.CE, OptionType.PE):
                        opt = instruments.get(idx.value, expiry, k, ot)
                        if opt:
                            subs.append((opt.exchange_segment, opt.security_id))
                if subs and feed:
                    await feed.subscribe(subs)
                    log.info("atm_range_subscribed", underlying=idx.value,
                             atm=atm, count=len(subs))
                seen.add(idx)
            try:
                await asyncio.wait_for(shutdown_event().wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    # Reconciler (live only).
    reconciler_task: asyncio.Task | None = None
    if cfg.mode == Mode.LIVE and dhan_client is not None:
        state.reconciler = PositionReconciler(dhan_client, interval_s=cfg.broker.reconcile_interval_s)
        reconciler_task = asyncio.create_task(state.reconciler.run(), name="reconciler")

    # Engines.
    engine_tasks = [asyncio.create_task(e.run(), name=f"engine-{e.underlying.value}") for e in engines]
    md_task = asyncio.create_task(market_data.run(), name="market_data")
    sub_task = asyncio.create_task(dynamic_subscriber(), name="dynamic_subscriber")

    # Dashboard server.
    app = create_app(state)
    server_cfg = uvicorn.Config(
        app, host=cfg.dashboard.host, port=cfg.dashboard.port,
        log_level=cfg.logging.level.lower(), loop="asyncio", lifespan="on",
    )
    server = uvicorn.Server(server_cfg)
    server_task = asyncio.create_task(server.serve(), name="uvicorn")

    # Shutdown callbacks (LIFO).
    async def _shutdown() -> None:
        log.info("shutdown_sequence_starting")
        for e in engines:
            e.stop()
        market_data.stop()
        if feed:
            feed.stop()
        if state.reconciler:
            state.reconciler.stop()
        server.should_exit = True
        try:
            await db.end_session(
                realized_pnl=sum(e.realized_pnl for e in engines),
                total_cycles=sum(e.closed_cycles_count for e in engines),
                peak=max((e.peak_session_pnl for e in engines), default=0.0),
                trough=min((e.trough_session_pnl for e in engines), default=0.0),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("end_session_error", error=str(exc))
        await db.close()
        if dhan_client:
            await dhan_client.close()

    register_shutdown(_shutdown)

    log.info(
        "volscalp_ready",
        mode=cfg.mode.value,
        dashboard=f"http://{cfg.dashboard.host}:{cfg.dashboard.port}",
        indices=[i.value for i in cfg.indices],
    )

    # Wait for shutdown signal.
    try:
        await shutdown_event().wait()
    except asyncio.CancelledError:
        pass

    log.info("shutdown_requested_running_callbacks")
    await run_shutdown_callbacks()

    # Cancel remaining tasks.
    all_tasks = [t for t in (
        md_task, sub_task, server_task, feed_task, reconciler_task, *engine_tasks,
    ) if t is not None]
    for t in all_tasks:
        t.cancel()
    await asyncio.gather(*all_tasks, return_exceptions=True)

    log.info("volscalp_shutdown_complete")
    return 0


def cli() -> None:
    """Console-script entrypoint (installed as `volscalp`)."""
    parser = argparse.ArgumentParser(prog="volscalp", description="Repeated OTM Strangle trading app")
    parser.add_argument("--config", "-c", default="configs/default.yaml")
    parser.add_argument("--mode", choices=["paper", "live"], help="override configured mode")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.mode:
        cfg = cfg.model_copy(update={"mode": Mode(args.mode)})

    # uvloop for lower overhead when available.
    try:
        import uvloop  # type: ignore[import-not-found]
        uvloop.install()
    except ImportError:
        pass

    try:
        rc = asyncio.run(_run(cfg))
    except KeyboardInterrupt:
        request_shutdown()
        rc = 0
    sys.exit(rc)


if __name__ == "__main__":  # pragma: no cover
    cli()
