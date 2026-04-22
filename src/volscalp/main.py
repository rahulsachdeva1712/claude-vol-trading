"""Application entrypoint — wires every module into a single asyncio process.

Dual-mode topology:
    Paper and Live engines run SIMULTANEOUSLY in one process. They share:
        - Dhan market feed (WebSocket)
        - MarketDataService + candle builder
        - Instrument master

    They do NOT share:
        - OrderManager (paper uses LTP fills; live uses Dhan REST)
        - Strategy engines (one per (mode, underlying) = 4 total)
        - DB sessions (one row per mode)
        - Lots-per-trade (per-mode UI input)

    Controls:
        - Live engines start DISARMED; entries are suppressed until the user
          clicks "Arm" on the live tab.
        - Kill switch targets live only: disarms + force-closes live cycles.
        - Paper has no kill switch (it's always safe) and no arm toggle
          (it's always armed).

Boot responsibilities:
    1. Process hygiene (PID file, stale cleanup, signal handlers).
    2. Config + secrets load, logging setup.
    3. Dhan instrument master download (or cached).
    4. Dhan WebSocket connection and ATM ± N strike subscription per index.
    5. Market data service + candle builder + spot tracker.
    6. Two OrderManagers: paper + live (live only if Dhan creds present).
    7. Four StrategyEngines (paper×2, live×2).
    8. FastAPI dashboard (uvicorn running in the same loop).
    9. Optional Dhan position reconciler (live only).
   10. Graceful shutdown on SIGINT/SIGTERM.
"""
from __future__ import annotations

import argparse
import asyncio
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


class ModeTree:
    """Per-mode container: order manager, engines, session_id, lot size,
    arm flag. Paper has arm permanently set; live starts disarmed."""

    def __init__(
        self,
        mode: Mode,
        order_mgr: OrderManager,
        session_id: int,
        lots: int,
        armed: asyncio.Event,
    ):
        self.mode = mode
        self.order_mgr = order_mgr
        self.session_id = session_id
        self.lots = lots
        self.armed = armed
        self.engines: dict[IndexName, StrategyEngine] = {}
        self.kill_switch: asyncio.Event = asyncio.Event()


class RuntimeState:
    """Shared state between the dashboard and the engines.

    Holds one ModeTree per mode (always paper; live only if Dhan creds
    present). The dashboard reads `snapshot()` and mutates lots / arm /
    kill via the provided async methods.
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
        self.market_data: MarketDataService | None = None
        self.reconciler: PositionReconciler | None = None
        self.dhan_client: DhanClient | None = None
        self.modes: dict[Mode, ModeTree] = {}
        self._arm_lock = asyncio.Lock()

    # ---- registry --------------------------------------------------------

    def register_mode(self, tree: ModeTree) -> None:
        self.modes[tree.mode] = tree

    def has_live(self) -> bool:
        return Mode.LIVE in self.modes

    # ---- snapshot --------------------------------------------------------

    def _tree_snapshot(self, tree: ModeTree) -> dict[str, Any]:
        engine_kpis: dict[str, dict[str, Any]] = {}
        realized_total = 0.0
        unrealized_total = 0.0
        closed_total = 0
        wins_total = 0
        losses_total = 0
        peak_total = 0.0
        trough_total = 0.0

        for idx, eng in tree.engines.items():
            kpi = eng.kpi_snapshot()
            kpi["legs"] = eng._legs_snapshot()  # noqa: SLF001
            if eng.current_cycle:
                kpi["cycle_no"] = eng.current_cycle.cycle_id
                kpi["state"] = eng.current_cycle.state.value
                kpi["mtm"] = round(eng.current_cycle.mtm, 2)
                kpi["atm"] = eng.current_cycle.atm_at_start
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

        gross_profit = sum(max(0.0, e.realized_pnl) for e in tree.engines.values() if e.win_count)
        gross_loss = abs(sum(min(0.0, e.realized_pnl) for e in tree.engines.values() if e.loss_count))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0.0

        open_positions = sum(
            1 for eng in tree.engines.values()
            for leg in (eng.current_cycle.legs.values() if eng.current_cycle else [])
            if leg.status.value == "ACTIVE"
        )

        return {
            "mode": tree.mode.value,
            "armed": tree.armed.is_set(),
            "kill_switch": tree.kill_switch.is_set(),
            "lots_per_trade": tree.lots,
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

    def snapshot(self) -> dict[str, Any]:
        return {
            "modes": {m.value: self._tree_snapshot(t) for m, t in self.modes.items()},
            "live_available": self.has_live(),
        }

    # ---- mutations from dashboard ---------------------------------------

    async def arm_live(self) -> None:
        if Mode.LIVE not in self.modes:
            raise RuntimeError("live mode is not available (missing Dhan credentials)")
        async with self._arm_lock:
            tree = self.modes[Mode.LIVE]
            if tree.kill_switch.is_set():
                raise RuntimeError("kill switch is active; clear it before arming")
            tree.armed.set()
            log.warning("live_armed")
            await self.bus.broadcast("live_arm", {"armed": True})

    async def disarm_live(self) -> None:
        if Mode.LIVE not in self.modes:
            return
        async with self._arm_lock:
            tree = self.modes[Mode.LIVE]
            tree.armed.clear()
            log.warning("live_disarmed")
            await self.bus.broadcast("live_arm", {"armed": False})

    async def trigger_kill_switch(self) -> None:
        """Kill switch is LIVE-ONLY. Disarms + flattens all live positions."""
        if Mode.LIVE not in self.modes:
            raise RuntimeError("kill switch only applies to live; live is not available")
        log.warning("kill_switch_triggered")
        tree = self.modes[Mode.LIVE]
        tree.kill_switch.set()
        tree.armed.clear()
        await self.bus.broadcast("kill_switch", {"active": True})

    async def clear_kill_switch(self) -> None:
        if Mode.LIVE not in self.modes:
            return
        tree = self.modes[Mode.LIVE]
        tree.kill_switch = asyncio.Event()
        for eng in tree.engines.values():
            eng.kill = tree.kill_switch
        log.warning("kill_switch_cleared")
        await self.bus.broadcast("kill_switch", {"active": False})

    async def update_runtime_config(self, body: dict[str, Any]) -> dict[str, Any]:
        changed: dict[str, Any] = {}
        mode_str = str(body.get("mode", "")).lower()
        if mode_str not in ("paper", "live"):
            raise RuntimeError("config update requires 'mode': 'paper' | 'live'")
        mode = Mode(mode_str)
        if mode not in self.modes:
            raise RuntimeError(f"{mode_str} mode is not available")
        tree = self.modes[mode]
        if "lots_per_trade" in body:
            try:
                lots = max(1, int(body["lots_per_trade"]))
            except (TypeError, ValueError):
                raise RuntimeError("lots_per_trade must be an integer >= 1") from None
            tree.lots = lots
            changed["lots_per_trade"] = lots
            log.info("runtime_lots_updated", mode=mode.value, lots=lots)
        if changed:
            await self.bus.broadcast("runtime_config", {"mode": mode.value, **changed})
        return changed


# ---------------------------------------------------------------------------
# Boot
# ---------------------------------------------------------------------------

async def _build_mode_tree(
    mode: Mode,
    cfg: AppConfig,
    db: Database,
    secrets: EnvSecrets,
    market_data: MarketDataService,
    instruments: InstrumentMaster,
    bus: EventBus,
    dhan_client: DhanClient | None,
) -> ModeTree:
    """Create DB session, OrderManager, lots counter, and engines for one mode."""
    session_id = await db.start_session(
        index_name="+".join(i.value for i in cfg.indices),
        mode=mode.value,
    )
    log.info("session_started", session_id=session_id, mode=mode.value)

    if mode == Mode.LIVE:
        assert dhan_client is not None, "live backend requires Dhan client"
        backend = LiveBackend(dhan_client)
        lots = cfg.engine.lots_per_trade_live
        armed = asyncio.Event()   # live starts disarmed
    else:
        backend = PaperBackend(
            price_lookup=market_data.last_ltp, slippage_bps=cfg.paper.slippage_bps
        )
        lots = cfg.engine.lots_per_trade_paper
        armed = asyncio.Event()
        armed.set()  # paper is always armed

    async def _order_request_cb(req):
        await db.record_order_request(session_id, req, None, None)

    async def _order_report_cb(report):
        await db.record_order_report(report)

    order_mgr = OrderManager(
        mode=mode, backend=backend, on_request=_order_request_cb, on_report=_order_report_cb
    )

    tree = ModeTree(mode=mode, order_mgr=order_mgr, session_id=session_id, lots=lots, armed=armed)

    for idx in cfg.indices:
        engine = StrategyEngine(
            mode=mode,
            underlying=idx,
            cfg=cfg,
            market_data=market_data,
            instruments=instruments,
            order_mgr=order_mgr,
            db=db,
            session_id=session_id,
            kill_switch=tree.kill_switch,
            armed=armed,
            event_bus=bus,
            lots_provider=lambda t=tree: t.lots,
        )
        tree.engines[idx] = engine

    return tree


async def _run(cfg: AppConfig) -> int:
    secrets = load_secrets()
    configure_logging(level=cfg.logging.level, json_output=cfg.logging.json_output, path=cfg.logging.path)

    pid_file = Path.home() / ".claude-vol-trading" / "run" / "app.pid"
    setup_process_hygiene(pid_file)

    loop = asyncio.get_running_loop()
    install_signal_handlers(loop)

    db = Database(cfg.persistence.sqlite_path)
    await db.open()

    # Instrument master (shared).
    instruments = InstrumentMaster(Path(secrets.VOLSCALP_DATA_DIR) / "instruments")
    try:
        await instruments.ensure_loaded()
    except Exception as e:  # noqa: BLE001
        log.error("instrument_master_load_failed", error=str(e))
        await db.close()
        return 2

    # Shared tick queue + market data service.
    tick_queue: asyncio.Queue[Tick] = asyncio.Queue(maxsize=8192)
    market_data = MarketDataService(tick_queue)
    for idx, (_seg, sid) in INDEX_FEED.items():
        market_data.register_spot(sid, idx.value)

    # Single Dhan market feed (shared by paper + live).
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

    # Dhan REST client (for live order placement + position reconciliation).
    dhan_client: DhanClient | None = None
    if secrets.has_dhan_credentials():
        dhan_client = DhanClient(
            secrets.DHAN_CLIENT_ID,
            secrets.DHAN_ACCESS_TOKEN,
            timeout_s=cfg.broker.order_timeout_s,
        )

    # Event bus + runtime state.
    bus = EventBus()
    state = RuntimeState(cfg, secrets, db, bus, instruments, feed)
    state.market_data = market_data
    state.dhan_client = dhan_client

    # Always build paper. Build live only if we have Dhan credentials.
    paper_tree = await _build_mode_tree(
        Mode.PAPER, cfg, db, secrets, market_data, instruments, bus, dhan_client=None,
    )
    state.register_mode(paper_tree)

    if dhan_client is not None:
        live_tree = await _build_mode_tree(
            Mode.LIVE, cfg, db, secrets, market_data, instruments, bus, dhan_client=dhan_client,
        )
        state.register_mode(live_tree)
    else:
        log.warning("live_mode_disabled_no_credentials")

    # Collect every engine for task spawning + shutdown fan-out.
    all_engines: list[StrategyEngine] = []
    for tree in state.modes.values():
        all_engines.extend(tree.engines.values())

    # Pre-subscribe option strikes around ATM so ticks are flowing by the
    # time cycles start. Also seed with a broad slice of strikes.
    if feed is not None:
        initial_subs: list[tuple[str, int]] = []
        for idx in cfg.indices:
            inst_cfg = cfg.instruments[idx]
            expiry = instruments.pick_expiry(idx, cfg.expiry)
            seg, sid = INDEX_FEED[idx]
            initial_subs.append((seg, sid))
            strikes = instruments.strikes_for(idx.value, expiry)
            if strikes:
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
                    log.info("atm_range_subscribed", underlying=idx.value, atm=atm, count=len(subs))
                seen.add(idx)
            try:
                await asyncio.wait_for(shutdown_event().wait(), timeout=5.0)
            except asyncio.TimeoutError:
                continue

    # Position reconciler — only meaningful for live.
    reconciler_task: asyncio.Task | None = None
    if dhan_client is not None:
        state.reconciler = PositionReconciler(dhan_client, interval_s=cfg.broker.reconcile_interval_s)
        reconciler_task = asyncio.create_task(state.reconciler.run(), name="reconciler")

    # Engines.
    engine_tasks = [
        asyncio.create_task(e.run(), name=f"engine-{e.mode.value}-{e.underlying.value}")
        for e in all_engines
    ]
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

    async def _shutdown() -> None:
        log.info("shutdown_sequence_starting")
        for e in all_engines:
            e.stop()
        market_data.stop()
        if feed:
            feed.stop()
        if state.reconciler:
            state.reconciler.stop()
        server.should_exit = True

        # End one session row per mode with its rolled-up P&L.
        for tree in state.modes.values():
            engines = list(tree.engines.values())
            try:
                await db.end_session(
                    session_id=tree.session_id,
                    realized_pnl=sum(e.realized_pnl for e in engines),
                    total_cycles=sum(e.closed_cycles_count for e in engines),
                    peak=max((e.peak_session_pnl for e in engines), default=0.0),
                    trough=min((e.trough_session_pnl for e in engines), default=0.0),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("end_session_error", mode=tree.mode.value, error=str(exc))
        await db.close()
        if dhan_client:
            await dhan_client.close()

    register_shutdown(_shutdown)

    log.info(
        "volscalp_ready",
        modes=[m.value for m in state.modes],
        live_available=state.has_live(),
        dashboard=f"http://{cfg.dashboard.host}:{cfg.dashboard.port}",
        indices=[i.value for i in cfg.indices],
    )

    try:
        await shutdown_event().wait()
    except asyncio.CancelledError:
        pass

    log.info("shutdown_requested_running_callbacks")
    await run_shutdown_callbacks()

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
    args = parser.parse_args()

    cfg = load_config(args.config)

    # uvloop for lower overhead when available (Linux/macOS only).
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
