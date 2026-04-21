"""Strategy engine — Repeated OTM Strangle.

One engine instance per underlying (NIFTY, BANKNIFTY). All engines run
on the same asyncio loop and share the MarketDataService / OrderManager.

Hot path:
    tick → update leg last_price → recompute cycle MTM → MtmController
    bar close → entry evaluation (momentum) → place orders
    leg SL hit (per-bar) → exit that leg → maybe schedule lazy leg
    session close → force-close everything

Every decision is timestamped and (best-effort) logged to the DB via the
Database.log_decision helper.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..broker.instruments import InstrumentMaster
from ..config import AppConfig, IndexName, MtmProfile
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
    """One engine per underlying."""

    def __init__(
        self,
        underlying: IndexName,
        cfg: AppConfig,
        market_data: MarketDataService,
        instruments: InstrumentMaster,
        order_mgr: OrderManager,
        db: Database,
        kill_switch: asyncio.Event,
        event_bus,   # EventBus - kept duck-typed to avoid import cycle
    ):
        self.underlying = underlying
        self.cfg = cfg
        self.md = market_data
        self.instruments = instruments
        self.orders = order_mgr
        self.db = db
        self.kill = kill_switch
        self.bus = event_bus

        self.profile: MtmProfile = cfg.mtm_profiles[underlying]
        self.inst_cfg = cfg.instruments[underlying]
        self.tz = ZoneInfo(cfg.session.timezone)

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

        md.on_bar_close(self._on_bar_close)
        md.on_tick(self._on_tick)

    # ---- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        """Engine driver — pumps the cycle state machine on a 1-second tick."""
        self.expiry = self.instruments.pick_expiry(self.underlying, self.cfg.expiry)
        log.info("engine_started", underlying=self.underlying.value, expiry=self.expiry,
                 profile=self.profile.profile)

        while not self._stop.is_set():
            try:
                await self._tick_engine()
            except Exception as e:  # noqa: BLE001
                log.exception("engine_tick_error", error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

        log.info("engine_stopped", underlying=self.underlying.value)

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

        if self.current_cycle is None:
            await self._start_new_cycle()
            return

        # If the current cycle is fully closed, open the next (cooldown = 0).
        if self.current_cycle.state == CycleState.CLOSED:
            self.current_cycle = None
            if not after_close:
                await self._start_new_cycle()

    async def _start_new_cycle(self) -> None:
        spot = self.md.spot(self.underlying.value)
        if spot <= 0:
            # Wait until we have a spot quote.
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
        self.current_cycle_row_id = await self.db.insert_cycle(cycle)
        log.info(
            "cycle_start",
            cycle=cycle.cycle_id,
            underlying=cycle.underlying,
            spot=spot,
            atm=atm,
            ce_strike=ce_strike,
            pe_strike=pe_strike,
        )

        await self.bus.broadcast(
            "cycle_update",
            {"underlying": self.underlying.value, "cycle": cycle.cycle_id,
             "state": cycle.state.value, "atm": atm, "spot": spot,
             "legs": self._legs_snapshot()},
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
            lots=self.cfg.engine.lots_per_trade,
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
            if bar.return_pct >= self.cfg.engine.momentum_threshold_pct:
                await self._enter_leg(leg, bar)
            else:
                await self.db.log_decision(
                    self.current_cycle_row_id,
                    "entry_eval",
                    {"slot": leg.slot, "ret_pct": round(bar.return_pct, 3)},
                    "SKIP",
                )

        # SL check for ACTIVE legs (close-based per FRD default).
        for leg in list(cycle.legs.values()):
            if leg.security_id != bar.security_id or leg.status != LegStatus.ACTIVE:
                continue
            ref_price = bar.close if self.cfg.engine.sl_price_source == "close" else bar.low
            if ref_price <= leg.sl_price:
                await self._exit_leg(leg, ref_price, ExitReason.LEG_SL)
                await self._maybe_schedule_lazy_leg(leg)

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
            await self.db.record_order_request(req, self.current_cycle_row_id, leg.row_id)

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
            {"underlying": self.underlying.value, "cycle": self.current_cycle.cycle_id,
             "slot": leg.slot, "status": leg.status.value,
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
            underlying=self.underlying.value,
            cycle=self.current_cycle.cycle_id if self.current_cycle else None,
            slot=leg.slot,
            reason=reason.value,
            price=fill_price,
            pnl=leg.realized_pnl,
        )
        await self.bus.broadcast(
            "leg_update",
            {"underlying": self.underlying.value,
             "cycle": self.current_cycle.cycle_id if self.current_cycle else None,
             "slot": leg.slot, "status": leg.status.value,
             "exit_price": leg.exit_price, "reason": reason.value, "pnl": leg.realized_pnl},
        )

    async def _maybe_schedule_lazy_leg(self, stopped: Leg) -> None:
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
        if inst:
            leg.security_id = inst.security_id
            leg.trading_symbol = inst.trading_symbol
            if not leg.lot_size:
                leg.lot_size = inst.lot_size
        else:
            log.warning("lazy_leg_strike_missing", strike=strike, option_type=opposite.value)
            leg.status = LegStatus.EMPTY
        cycle.legs[slot] = leg
        log.info(
            "lazy_leg_scheduled",
            underlying=self.underlying.value,
            cycle=cycle.cycle_id,
            slot=slot,
            strike=strike,
            option_type=opposite.value,
        )

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
            {"underlying": self.underlying.value, "cycle": cycle.cycle_id,
             "state": cycle.state.value, "mtm": round(mtm, 2),
             "peak": round(cycle.peak_mtm, 2), "trough": round(cycle.trough_mtm, 2),
             "locked": cycle.lock_activated, "floor": round(cycle.lock_floor, 2),
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
                cycle.lock_activated,
                cycle.lock_floor,
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
            underlying=self.underlying.value,
            cycle=cycle.cycle_id,
            reason=reason.value,
            pnl=cycle.cycle_pnl,
            peak=cycle.peak_mtm,
            trough=cycle.trough_mtm,
        )
        await self.bus.broadcast(
            "cycle_update",
            {"underlying": self.underlying.value, "cycle": cycle.cycle_id,
             "state": cycle.state.value, "mtm": cycle.cycle_pnl,
             "exit_reason": reason.value, "legs": self._legs_snapshot(), "closed": True},
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
        winners = [1 for _ in range(self.win_count)]
        total_trades = self.win_count + self.loss_count
        win_rate = (self.win_count / total_trades * 100.0) if total_trades else 0.0
        return {
            "underlying": self.underlying.value,
            "mode": self.cfg.mode.value,
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
