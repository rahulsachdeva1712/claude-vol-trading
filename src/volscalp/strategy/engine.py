"""Strategy engine — Repeated OTM Strangle.

One engine instance per (mode, underlying) pair. With paper and live both
running simultaneously, that's four engines per process (paper-NIFTY,
paper-BANKNIFTY, live-NIFTY, live-BANKNIFTY). They share the market-data
service and instrument master; everything else — cycles, MTM, P&L,
session id — is per-engine.

Hot path:
    tick → update leg last_price → recompute cycle MTM → MtmController
    bar close → entry evaluation (momentum) for BASE legs → place orders
    leg SL hit (per-bar) → exit that leg → immediately enter opposite lazy
        leg (no momentum gate — Codex aggregate-MTM semantics)
    cycle aggregate MTM (realized + unrealized) crosses target/max_loss
        → force-close remaining ACTIVE legs
    last surviving leg exits and no lazy is pending → cycle is "done"
        → next cycle spins up on the next bar
    session close → force-close everything

Every decision is timestamped and (best-effort) logged to the DB via the
Database.log_decision helper.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..broker.instruments import InstrumentMaster
from ..config import AppConfig, IndexName, Mode, MtmProfile
from ..logging_setup import get_logger
from ..market_data.feed import MarketDataService
from ..models import (
    Bar,
    Cycle,
    CycleState,
    ExitReason,
    Leg,
    LegKind,
    LegStatus,
    OptionInstrument,
    OptionType,
    OrderRequest,
    now_ns,
)
from ..orders.manager import OrderManager, new_client_order_id
from ..storage.db import Database
from .mtm import MtmController

log = get_logger(__name__)


class StrategyEngine:
    """One engine per (mode, underlying)."""

    def __init__(
        self,
        mode: Mode,
        underlying: IndexName,
        cfg: AppConfig,
        market_data: MarketDataService,
        instruments: InstrumentMaster,
        order_mgr: OrderManager,
        db: Database,
        session_id: int,
        kill_switch: asyncio.Event,
        armed: asyncio.Event,
        event_bus,   # EventBus - kept duck-typed to avoid import cycle
        lots_provider,  # callable() -> int, read at cycle start
    ):
        self.mode = mode
        self.underlying = underlying
        self.cfg = cfg
        self.md = market_data
        self.instruments = instruments
        self.orders = order_mgr
        self.db = db
        self.session_id = session_id
        self.kill = kill_switch
        self.armed = armed
        self.bus = event_bus
        self._lots_provider = lots_provider

        self.profile: MtmProfile = cfg.mtm_profiles[underlying]
        self.inst_cfg = cfg.instruments[underlying]
        self.tz = ZoneInfo(cfg.session.timezone)
        self.tag = f"{mode.value}:{underlying.value}"

        self.current_cycle: Cycle | None = None
        self.current_cycle_row_id: int | None = None
        self.expiry: str = ""
        self.cycle_counter: int = 0
        self.realized_pnl: float = 0.0
        self.peak_session_pnl: float = 0.0
        self.trough_session_pnl: float = 0.0
        self.closed_cycles_count: int = 0
        self.win_count: int = 0
        self.loss_count: int = 0

        self._mtm_ctrl: MtmController | None = None
        self._stop = asyncio.Event()

        self.md.on_bar_close(self._on_bar_close)
        self.md.on_tick(self._on_tick)

    # ---- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def _backfill_counters_from_db(self) -> None:
        """Rehydrate in-memory KPI counters from today's closed cycles.

        A new DB session row is created on every process start, so without
        this the KPI strip would show zeros after any mid-day restart even
        though today's cycles are safely on disk. The reconstruction is
        across all sessions for this (mode, underlying) on today's date,
        so multiple restarts in a single trading day still roll up into
        one view.

        Peak/trough here are the running max/min of cumulative realized
        P&L across cycle-close boundaries; we can't recover intra-cycle
        MTM from the DB, but cycle-close boundaries are a faithful
        reconstruction of what the engine held at the moment of its last
        close, and the live tick loop will immediately start updating
        peak/trough again against unrealized MTM once a new cycle opens.
        """
        today_iso = datetime.now(self.tz).date().isoformat()
        try:
            kpis = await self.db.fetch_today_engine_kpis(
                self.mode.value, self.underlying.value, today_iso,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("kpi_backfill_failed", tag=self.tag, error=str(exc))
            return

        if not kpis or kpis.get("closed_cycles", 0) == 0:
            return

        self.realized_pnl = float(kpis["realized_pnl"])
        self.closed_cycles_count = int(kpis["closed_cycles"])
        self.win_count = int(kpis["wins"])
        self.loss_count = int(kpis["losses"])
        self.peak_session_pnl = float(kpis["peak_session_pnl"])
        self.trough_session_pnl = float(kpis["trough_session_pnl"])
        # New cycles opened in this process should continue numbering from
        # today's max — the UI labels cycles 1..N per trading day, not per
        # process boot.
        self.cycle_counter = int(kpis["max_cycle_no"])

    async def run(self) -> None:
        """Engine driver — pumps the cycle state machine on a 1-second tick."""
        self.expiry = self.instruments.pick_expiry(self.underlying, self.cfg.expiry)
        await self._backfill_counters_from_db()
        log.info(
            "engine_started",
            tag=self.tag,
            underlying=self.underlying.value,
            mode=self.mode.value,
            expiry=self.expiry,
            backfilled_realized_pnl=round(self.realized_pnl, 2),
            backfilled_cycles=self.closed_cycles_count,
            backfilled_cycle_counter=self.cycle_counter,
        )

        while not self._stop.is_set():
            try:
                await self._tick_engine()
            except Exception as e:  # noqa: BLE001
                log.exception("engine_tick_error", tag=self.tag, error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

        log.info("engine_stopped", tag=self.tag, underlying=self.underlying.value)

    # ---- state machine -----------------------------------------------------

    async def _tick_engine(self) -> None:
        if self.kill.is_set():
            await self._force_close_all(ExitReason.KILL_SWITCH)
            return

        now_local = datetime.now(self.tz).time()
        after_close = now_local >= self.cfg.session.end
        before_start = now_local < self.cfg.session.start

        if after_close and self.current_cycle and self.current_cycle.any_leg_open():
            await self._force_close_all(ExitReason.SESSION_CLOSE)
            return

        if after_close:
            return  # session done

        if before_start:
            return  # warmup

        # Live engines don't open new cycles until the user arms them from
        # the dashboard. Already-open cycles continue to be managed (so
        # disarming during a cycle does NOT halt MTM / SL evaluation — the
        # kill switch is the escape hatch for that).
        if not self.armed.is_set() and self.current_cycle is None:
            return

        if self.current_cycle is None:
            await self._start_new_cycle()
            return

        # Cycle-done detector (aggregate-MTM semantics — FRD §5.2 / §6.1):
        # If every leg in the current cycle has already stopped (or was
        # never filled) and nothing is still WATCHING for a momentum
        # entry, the cycle is done — close it and let the next tick spin
        # up a new one (cooldown=0). Without this, a cycle where both
        # bases SL out and no lazy is eligible would idle all the way to
        # session close, blocking further cycles for the day. This
        # matches the behaviour of scripts/backtest_2y.py's cycle-done
        # break (see §5-7 of simulate_session).
        if (
            self.current_cycle.state != CycleState.CLOSED
            and self._cycle_is_done(self.current_cycle)
        ):
            reason = self._last_leg_exit_reason(self.current_cycle) or ExitReason.LEG_SL
            await self._force_close_all(reason)

        # If the current cycle is fully closed, open the next (cooldown = 0).
        if self.current_cycle.state == CycleState.CLOSED:
            self.current_cycle = None
            if not after_close and self.armed.is_set():
                await self._start_new_cycle()

    @staticmethod
    def _cycle_is_done(cycle) -> bool:
        """True when no leg is still alive — i.e. everyone is STOPPED or
        EMPTY, with at least one leg actually in a terminal state (so a
        cycle that just started with only EMPTY slots from a missing
        strike chain also returns True and gets re-opened quickly)."""
        has_alive = any(
            leg.status in (LegStatus.WATCHING, LegStatus.PENDING,
                           LegStatus.ACTIVE, LegStatus.EXITING)
            for leg in cycle.legs.values()
        )
        if has_alive:
            return False
        has_any_leg = bool(cycle.legs)
        return has_any_leg

    @staticmethod
    def _last_leg_exit_reason(cycle) -> ExitReason | None:
        """Return the exit reason of the most recently stopped leg.
        Used to label a cycle that ran out of alive legs with the same
        reason the last leg reported (e.g. LEG_SL if the final leg SL'd
        out). Mirrors Codex spread_lab.py:1360."""
        stopped = [leg for leg in cycle.legs.values()
                   if leg.status == LegStatus.STOPPED and leg.exit_bar_ts]
        if not stopped:
            return None
        stopped.sort(key=lambda leg: leg.exit_bar_ts, reverse=True)
        return stopped[0].exit_reason

    async def _start_new_cycle(self) -> None:
        spot = self.md.spot(self.underlying.value)
        if spot <= 0:
            # Wait until we have a spot quote. Log at DEBUG-ish cadence
            # (once every ~30s) to prove the engine is polling, not stuck.
            now_mono = int(datetime.now(timezone.utc).timestamp())
            last = getattr(self, "_last_waiting_spot_log", 0)
            if now_mono - last >= 30:
                log.info("waiting_for_spot", tag=self.tag, underlying=self.underlying.value)
                self._last_waiting_spot_log = now_mono
            return

        atm = self.instruments.nearest_strike(
            self.underlying.value, self.expiry, spot, self.inst_cfg.strike_interval
        )
        self.cycle_counter += 1
        cycle = Cycle(
            cycle_id=self.cycle_counter,
            underlying=self.underlying.value,
            start_ts=now_ns(),
            atm_at_start=atm,
            state=CycleState.WATCHING,
        )

        # Configure CE and PE base legs.
        ce_strike = atm + self.cfg.engine.strike_offset_ce * self.inst_cfg.strike_interval
        pe_strike = atm + self.cfg.engine.strike_offset_pe * self.inst_cfg.strike_interval
        cycle.legs[1] = self._new_leg_slot(1, LegKind.BASE, OptionType.CE, ce_strike)
        cycle.legs[2] = self._new_leg_slot(2, LegKind.BASE, OptionType.PE, pe_strike)

        self.current_cycle = cycle
        self._mtm_ctrl = MtmController(self.profile)
        self.current_cycle_row_id = await self.db.insert_cycle(self.session_id, cycle)
        log.info(
            "cycle_start",
            tag=self.tag,
            cycle=cycle.cycle_id,
            underlying=cycle.underlying,
            spot=spot,
            atm=atm,
            ce_strike=ce_strike,
            pe_strike=pe_strike,
        )

        await self.bus.broadcast(
            "cycle_update",
            {"mode": self.mode.value, "underlying": self.underlying.value,
             "cycle": cycle.cycle_id, "state": cycle.state.value,
             "atm": atm, "spot": spot, "legs": self._legs_snapshot()},
        )

        # Subscribe the MarketDataService's WS to the option instruments for this cycle.
        await self._subscribe_cycle_instruments(cycle)

    def _new_leg_slot(self, slot: int, kind: LegKind, opt_type: OptionType, strike: int) -> Leg:
        return Leg(
            slot=slot,
            kind=kind,
            option_type=opt_type,
            underlying=self.underlying.value,
            strike=strike,
            status=LegStatus.WATCHING,
            lots=max(1, int(self._lots_provider())),
            lot_size=self.inst_cfg.lot_size,
            expiry=self.expiry,
        )

    async def _subscribe_cycle_instruments(self, cycle: Cycle) -> None:
        """Resolve security IDs for each planned leg and add them to the feed.

        Subscription itself is done by the engine's controller — here we
        only resolve the instrument metadata onto the Leg.
        """
        for leg in cycle.legs.values():
            inst = self.instruments.get(self.underlying.value, self.expiry, leg.strike, leg.option_type)
            if inst is None:
                log.warning(
                    "strike_missing_in_chain",
                    underlying=self.underlying.value,
                    expiry=self.expiry,
                    strike=leg.strike,
                    option_type=leg.option_type.value,
                )
                leg.status = LegStatus.EMPTY
                continue
            leg.security_id = inst.security_id
            leg.trading_symbol = inst.trading_symbol
            if not leg.lot_size:
                leg.lot_size = inst.lot_size

    # ---- listeners ---------------------------------------------------------

    def _on_tick(self, tick) -> None:
        cycle = self.current_cycle
        if not cycle:
            return
        for leg in cycle.legs.values():
            if leg.security_id == tick.security_id and leg.status == LegStatus.ACTIVE:
                leg.last_price = tick.ltp

    async def _on_bar_close(self, bar: Bar) -> None:
        cycle = self.current_cycle
        if not cycle:
            return

        # Entry evaluation for any WATCHING base/lazy leg on this instrument.
        for leg in list(cycle.legs.values()):
            if leg.security_id != bar.security_id:
                continue
            if leg.status != LegStatus.WATCHING:
                continue
            ret_pct = round(bar.return_pct, 3)
            threshold = self.cfg.engine.momentum_threshold_pct
            if bar.return_pct >= threshold:
                log.info(
                    "entry_signal",
                    tag=self.tag, slot=leg.slot,
                    option_type=leg.option_type.value, strike=leg.strike,
                    ret_pct=ret_pct, threshold=threshold, bar_close=bar.close,
                )
                await self._enter_leg(leg, bar)
            else:
                log.info(
                    "entry_skip",
                    tag=self.tag, slot=leg.slot,
                    option_type=leg.option_type.value, strike=leg.strike,
                    ret_pct=ret_pct, threshold=threshold,
                )
                await self.db.log_decision(
                    self.session_id,
                    self.current_cycle_row_id,
                    "entry_eval",
                    {"slot": leg.slot, "ret_pct": ret_pct},
                    "SKIP",
                )

        # SL check for ACTIVE legs. Default ref price is bar.low (intrabar
        # trigger; matches Codex's simulate_strategy_day + backtest_2y). Set
        # engine.sl_price_source=close in YAML for end-of-bar simulation.
        for leg in list(cycle.legs.values()):
            if leg.security_id != bar.security_id or leg.status != LegStatus.ACTIVE:
                continue
            ref_price = bar.close if self.cfg.engine.sl_price_source == "close" else bar.low
            if ref_price <= leg.sl_price:
                await self._exit_leg(leg, ref_price, ExitReason.LEG_SL)
                await self._maybe_schedule_lazy_leg(leg, bar)

        # Cycle-level MTM re-evaluation.
        await self._evaluate_mtm()

    async def _enter_leg(self, leg: Leg, bar: Bar) -> None:
        if not leg.security_id:
            log.warning("cannot_enter_leg_missing_security_id", slot=leg.slot)
            return
        entry_price = bar.close if self.cfg.engine.entry_price_source == "close" else bar.open
        sl_pct = self.cfg.engine.base_leg_sl_pct if leg.kind == LegKind.BASE else self.cfg.engine.lazy_leg_sl_pct
        leg.entry_price = entry_price
        leg.sl_price = round(entry_price * (1 - sl_pct / 100.0), 2)
        leg.entry_bar_ts = bar.minute_epoch
        leg.status = LegStatus.PENDING
        leg.last_price = entry_price

        inst = self.instruments.get_by_id(leg.security_id) or OptionInstrument(
            security_id=leg.security_id, exchange_segment="NSE_FNO", underlying=leg.underlying,
            strike=leg.strike, option_type=leg.option_type, expiry=leg.expiry,
            lot_size=leg.lot_size, trading_symbol=leg.trading_symbol,
        )

        coid = new_client_order_id("buy")
        req = OrderRequest(
            client_order_id=coid,
            security_id=leg.security_id,
            exchange_segment=inst.exchange_segment,
            trading_symbol=leg.trading_symbol,
            side="BUY",
            quantity=leg.quantity,
        )
        leg.entry_order_id = coid

        if self.current_cycle_row_id is not None:
            leg.row_id = await self.db.insert_leg(self.current_cycle_row_id, leg)
            await self.db.record_order_request(self.session_id, req, self.current_cycle_row_id, leg.row_id)

        report = await self.orders.place(req)
        if report.status == "FILLED":
            if report.avg_fill_price > 0:
                leg.entry_price = report.avg_fill_price
                leg.sl_price = round(report.avg_fill_price * (1 - sl_pct / 100.0), 2)
            leg.status = LegStatus.ACTIVE
            if self.current_cycle and self.current_cycle.state == CycleState.WATCHING:
                self.current_cycle.state = CycleState.ACTIVE
        elif report.status in ("REJECTED", "FAILED", "CANCELLED"):
            leg.status = LegStatus.EMPTY
            leg.exit_reason = ExitReason.MANUAL
            log.warning("leg_order_rejected", slot=leg.slot, reason=report.message)

        await self.bus.broadcast(
            "leg_update",
            {"mode": self.mode.value, "underlying": self.underlying.value,
             "cycle": self.current_cycle.cycle_id, "slot": leg.slot,
             "status": leg.status.value,
             "entry_price": leg.entry_price, "sl": leg.sl_price},
        )

    async def _exit_leg(self, leg: Leg, price: float, reason: ExitReason) -> None:
        inst = self.instruments.get_by_id(leg.security_id)
        if not inst:
            log.warning("exit_leg_missing_instrument", slot=leg.slot)
            return

        coid = new_client_order_id("sell")
        req = OrderRequest(
            client_order_id=coid,
            security_id=leg.security_id,
            exchange_segment=inst.exchange_segment,
            trading_symbol=leg.trading_symbol,
            side="SELL",
            quantity=leg.quantity,
        )
        leg.exit_order_id = coid
        leg.status = LegStatus.EXITING

        report = await self.orders.place(req)
        fill_price = report.avg_fill_price if report.avg_fill_price > 0 else price
        leg.exit_price = fill_price
        leg.status = LegStatus.STOPPED
        leg.exit_reason = reason
        leg.exit_bar_ts = int(datetime.now(timezone.utc).timestamp())
        self.realized_pnl += leg.realized_pnl

        if leg.row_id:
            try:
                await self.db.update_leg_exit(leg.row_id, leg)
            except Exception as exc:  # noqa: BLE001
                log.warning("leg_exit_db_update_failed", slot=leg.slot, error=str(exc))

        log.info(
            "leg_exit",
            tag=self.tag,
            underlying=self.underlying.value,
            cycle=self.current_cycle.cycle_id if self.current_cycle else None,
            slot=leg.slot,
            reason=reason.value,
            price=fill_price,
            pnl=leg.realized_pnl,
        )
        await self.bus.broadcast(
            "leg_update",
            {"mode": self.mode.value, "underlying": self.underlying.value,
             "cycle": self.current_cycle.cycle_id if self.current_cycle else None,
             "slot": leg.slot, "status": leg.status.value,
             "exit_price": leg.exit_price, "reason": reason.value, "pnl": leg.realized_pnl},
        )

    async def _maybe_schedule_lazy_leg(self, stopped: Leg, bar: Bar) -> None:
        """Enter the lazy (opposite-side OTM) leg at the stop-minute bar close.

        Codex semantics: lazy leg goes ACTIVE immediately when the base leg
        stops — there is no subsequent momentum-gate wait. The entry price is
        the current (stop-minute) close of the lazy instrument; if we have no
        fresh bar for it yet we fall back to the last known LTP.
        """
        cycle = self.current_cycle
        if not cycle or not self.cfg.engine.lazy_enabled or stopped.kind != LegKind.BASE:
            return

        opposite = OptionType.PE if stopped.option_type == OptionType.CE else OptionType.CE

        # Already scheduled? Block duplicates.
        if opposite == OptionType.PE:
            if cycle.lazy_pe_scheduled:
                return
            cycle.lazy_pe_scheduled = True
            slot = 3
        else:
            if cycle.lazy_ce_scheduled:
                return
            cycle.lazy_ce_scheduled = True
            slot = 4

        spot = self.md.spot(self.underlying.value)
        if spot <= 0:
            return
        atm = self.instruments.nearest_strike(
            self.underlying.value, self.expiry, spot, self.inst_cfg.strike_interval
        )
        offset = self.cfg.engine.strike_offset_ce if opposite == OptionType.CE else self.cfg.engine.strike_offset_pe
        strike = atm + offset * self.inst_cfg.strike_interval

        leg = self._new_leg_slot(slot, LegKind.LAZY, opposite, strike)
        inst = self.instruments.get(self.underlying.value, self.expiry, strike, opposite)
        if not inst:
            log.warning("lazy_leg_strike_missing", strike=strike, option_type=opposite.value)
            leg.status = LegStatus.EMPTY
            cycle.legs[slot] = leg
            return

        leg.security_id = inst.security_id
        leg.trading_symbol = inst.trading_symbol
        if not leg.lot_size:
            leg.lot_size = inst.lot_size
        cycle.legs[slot] = leg

        # Build a bar for the lazy instrument at the same stop-minute. Prefer
        # the live candle builder's in-progress bar; fall back to last LTP
        # wrapped in a minimal OHLC shell so _enter_leg has a close to use.
        lazy_bar = self.md.current_bar(leg.security_id)
        if lazy_bar is None or lazy_bar.close <= 0:
            px = self.md.last_ltp(leg.security_id)
            if px <= 0:
                log.warning(
                    "lazy_leg_no_price",
                    slot=slot, strike=strike, option_type=opposite.value,
                )
                leg.status = LegStatus.EMPTY
                return
            lazy_bar = Bar(
                security_id=leg.security_id,
                minute_epoch=bar.minute_epoch,
                open=px, high=px, low=px, close=px,
            )

        log.info(
            "lazy_leg_scheduled",
            tag=self.tag,
            underlying=self.underlying.value,
            cycle=cycle.cycle_id,
            slot=slot,
            strike=strike,
            option_type=opposite.value,
            entry_price=lazy_bar.close,
        )

        await self._enter_leg(leg, lazy_bar)

    # ---- MTM ---------------------------------------------------------------

    async def _evaluate_mtm(self) -> None:
        cycle = self.current_cycle
        if not cycle or not self._mtm_ctrl:
            return

        unrealized = sum(leg.unrealized_pnl for leg in cycle.legs.values())
        realized = sum(leg.realized_pnl for leg in cycle.legs.values())
        mtm = unrealized + realized

        decision = self._mtm_ctrl.update(cycle, mtm)
        if decision.exit:
            await self._force_close_all(decision.reason or ExitReason.MTM_TARGET)

        await self.bus.broadcast(
            "cycle_update",
            {"mode": self.mode.value, "underlying": self.underlying.value,
             "cycle": cycle.cycle_id, "state": cycle.state.value,
             "mtm": round(mtm, 2),
             "peak": round(cycle.peak_mtm, 2), "trough": round(cycle.trough_mtm, 2),
             "legs": self._legs_snapshot()},
        )

    async def _force_close_all(self, reason: ExitReason) -> None:
        cycle = self.current_cycle
        if not cycle:
            return
        cycle.state = CycleState.EXITING
        for leg in list(cycle.legs.values()):
            if leg.status == LegStatus.ACTIVE:
                await self._exit_leg(leg, leg.last_price or leg.entry_price, reason)

        cycle.cycle_pnl = sum(leg.realized_pnl for leg in cycle.legs.values())
        cycle.state = CycleState.CLOSED
        cycle.exit_reason = reason
        cycle.exit_ts = now_ns()
        if self.current_cycle_row_id is not None:
            await self.db.update_cycle_close(
                self.current_cycle_row_id,
                reason,
                cycle.cycle_pnl,
                cycle.peak_mtm,
                cycle.trough_mtm,
            )
        self.closed_cycles_count += 1
        if cycle.cycle_pnl > 0:
            self.win_count += 1
        else:
            self.loss_count += 1
        if self.realized_pnl > self.peak_session_pnl:
            self.peak_session_pnl = self.realized_pnl
        if self.realized_pnl < self.trough_session_pnl:
            self.trough_session_pnl = self.realized_pnl

        log.info(
            "cycle_closed",
            tag=self.tag,
            underlying=self.underlying.value,
            cycle=cycle.cycle_id,
            reason=reason.value,
            pnl=cycle.cycle_pnl,
            peak=cycle.peak_mtm,
            trough=cycle.trough_mtm,
        )
        await self.bus.broadcast(
            "cycle_update",
            {"mode": self.mode.value, "underlying": self.underlying.value,
             "cycle": cycle.cycle_id, "state": cycle.state.value,
             "mtm": cycle.cycle_pnl, "exit_reason": reason.value,
             "legs": self._legs_snapshot(), "closed": True},
        )

    # ---- UI helpers --------------------------------------------------------

    def _legs_snapshot(self) -> list[dict]:
        if not self.current_cycle:
            return []
        out = []
        for leg in self.current_cycle.legs.values():
            out.append(
                {
                    "slot": leg.slot,
                    "kind": leg.kind.value,
                    "option_type": leg.option_type.value,
                    "strike": leg.strike,
                    "status": leg.status.value,
                    "entry_price": leg.entry_price,
                    "sl": leg.sl_price,
                    "ltp": leg.last_price,
                    "pnl": round(leg.pnl, 2),
                    "quantity": leg.quantity,
                    "symbol": leg.trading_symbol,
                    "security_id": leg.security_id,
                }
            )
        return out

    def kpi_snapshot(self) -> dict:
        unrealized = 0.0
        if self.current_cycle:
            unrealized = sum(leg.unrealized_pnl for leg in self.current_cycle.legs.values())
        total = self.realized_pnl + unrealized
        avg_closed = (self.realized_pnl / self.closed_cycles_count) if self.closed_cycles_count else 0.0
        total_trades = self.win_count + self.loss_count
        win_rate = (self.win_count / total_trades * 100.0) if total_trades else 0.0
        return {
            "underlying": self.underlying.value,
            "mode": self.mode.value,
            "armed": self.armed.is_set(),
            "cycle_no": self.cycle_counter,
            "closed_cycles": self.closed_cycles_count,
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(unrealized, 2),
            "mtm": round(total, 2),
            "peak_session_pnl": round(self.peak_session_pnl, 2),
            "trough_session_pnl": round(self.trough_session_pnl, 2),
            "avg_pnl_per_trade": round(avg_closed, 2),
            "win_rate_pct": round(win_rate, 1),
            "wins": self.win_count,
            "losses": self.loss_count,
        }
