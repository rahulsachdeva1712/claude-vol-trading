"""Domain models: ticks, bars, legs, cycles, orders, events.

All models are immutable frozen dataclasses on the hot path (Tick, Bar)
to keep allocations cheap and GC pressure low. Pydantic is only used
for boundary types (config, UI payloads).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class LegKind(str, Enum):
    BASE = "BASE"
    LAZY = "LAZY"


class LegStatus(str, Enum):
    EMPTY = "EMPTY"
    WATCHING = "WATCHING"
    PENDING = "PENDING"       # order sent, awaiting fill
    ACTIVE = "ACTIVE"         # filled, in the market
    EXITING = "EXITING"       # exit order sent
    STOPPED = "STOPPED"       # closed (SL or other exit)


class CycleState(str, Enum):
    IDLE = "IDLE"
    WATCHING = "WATCHING"
    ACTIVE = "ACTIVE"
    EXITING = "EXITING"
    CLOSED = "CLOSED"


class ExitReason(str, Enum):
    LEG_SL = "LEG_SL"
    MTM_TARGET = "MTM_TARGET"
    MTM_MAX_LOSS = "MTM_MAX_LOSS"
    LOCK_TRAIL_FLOOR = "LOCK_TRAIL_FLOOR"
    SESSION_CLOSE = "SESSION_CLOSE"
    KILL_SWITCH = "KILL_SWITCH"
    MANUAL = "MANUAL"


# ---------------------------------------------------------------------------
# Hot-path tick/bar — keep these small.
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class Tick:
    """One market tick.

    `security_id` matches Dhan's instrument token.
    `ts_ns` is monotonic-ish wall time in nanoseconds (time.time_ns()).
    """
    security_id: int
    exchange_segment: str
    ltp: float
    ltq: int
    ltt_epoch: int          # last traded time from exchange (seconds)
    bid: float = 0.0
    ask: float = 0.0
    bid_qty: int = 0
    ask_qty: int = 0
    volume: int = 0
    oi: int = 0
    ts_ns: int = 0


@dataclass(slots=True)
class Bar:
    """1-minute OHLCV bar. Mutable because it aggregates ticks in-place."""
    security_id: int
    minute_epoch: int       # UTC minute-start epoch seconds
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    oi: int = 0
    tick_count: int = 0

    @property
    def return_pct(self) -> float:
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open * 100.0


@dataclass(slots=True, frozen=True)
class OptionInstrument:
    security_id: int
    exchange_segment: str
    underlying: str          # "NIFTY" | "BANKNIFTY"
    strike: int
    option_type: OptionType
    expiry: str              # ISO date
    lot_size: int
    trading_symbol: str


# ---------------------------------------------------------------------------
# Orders and fills.
# ---------------------------------------------------------------------------

OrderSide = Literal["BUY", "SELL"]
OrderStatus = Literal["PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED", "FAILED"]


@dataclass(slots=True)
class OrderRequest:
    client_order_id: str          # our tag; used to reconcile with Dhan
    security_id: int
    exchange_segment: str
    trading_symbol: str
    side: OrderSide
    quantity: int
    product_type: str = "INTRADAY"
    order_type: str = "MARKET"
    price: float = 0.0
    tag: str = "volscalp"


@dataclass(slots=True)
class OrderReport:
    client_order_id: str
    broker_order_id: str
    security_id: int
    side: OrderSide
    quantity: int
    filled_quantity: int
    avg_fill_price: float
    status: OrderStatus
    message: str = ""
    ts_ns: int = 0


# ---------------------------------------------------------------------------
# Strategy state objects (mutable — engine owns them).
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Leg:
    slot: int                     # 1..4 per cycle
    kind: LegKind
    option_type: OptionType
    underlying: str
    strike: int
    security_id: int = 0
    trading_symbol: str = ""
    expiry: str = ""
    lot_size: int = 0
    lots: int = 1
    status: LegStatus = LegStatus.EMPTY

    entry_bar_ts: int = 0
    entry_price: float = 0.0
    sl_price: float = 0.0
    exit_bar_ts: int = 0
    exit_price: float = 0.0
    exit_reason: ExitReason | None = None

    last_price: float = 0.0       # last seen LTP for MTM

    entry_order_id: str = ""
    exit_order_id: str = ""

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def realized_pnl(self) -> float:
        if self.status != LegStatus.STOPPED or self.exit_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def unrealized_pnl(self) -> float:
        if self.status != LegStatus.ACTIVE or self.entry_price <= 0:
            return 0.0
        return (self.last_price - self.entry_price) * self.quantity

    @property
    def pnl(self) -> float:
        return self.realized_pnl if self.status == LegStatus.STOPPED else self.unrealized_pnl


@dataclass(slots=True)
class Cycle:
    cycle_id: int
    underlying: str              # NIFTY | BANKNIFTY
    start_ts: int
    atm_at_start: int = 0
    state: CycleState = CycleState.WATCHING
    legs: dict[int, Leg] = field(default_factory=dict)   # slot -> Leg

    mtm: float = 0.0
    peak_mtm: float = 0.0
    trough_mtm: float = 0.0

    lock_activated: bool = False
    lock_floor: float = 0.0

    lazy_ce_scheduled: bool = False
    lazy_pe_scheduled: bool = False

    exit_ts: int = 0
    exit_reason: ExitReason | None = None
    cycle_pnl: float = 0.0

    def active_legs(self) -> list[Leg]:
        return [leg for leg in self.legs.values() if leg.status == LegStatus.ACTIVE]

    def any_leg_open(self) -> bool:
        return any(
            leg.status in (LegStatus.ACTIVE, LegStatus.PENDING, LegStatus.EXITING)
            for leg in self.legs.values()
        )


# ---------------------------------------------------------------------------
# UI / event payloads.
# ---------------------------------------------------------------------------

@dataclass(slots=True, frozen=True)
class EngineEvent:
    """Generic event pushed to the dashboard WebSocket."""
    kind: str                    # "tick" | "bar" | "cycle_update" | "leg_update" | ...
    ts_ns: int
    payload: dict


def now_ns() -> int:
    import time
    return time.time_ns()


def minute_of(ts_epoch: float) -> int:
    return int(ts_epoch) // 60 * 60


def fmt_ts(ts_ns: int) -> str:
    return datetime.utcfromtimestamp(ts_ns / 1e9).isoformat(timespec="milliseconds") + "Z"
