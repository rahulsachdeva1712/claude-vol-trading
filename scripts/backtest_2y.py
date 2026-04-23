"""2-year strategy replay off the local rollingoption cache.

Reads the JSON files produced by ``backtest_history_fetch.py`` under
``data/backtest_cache/rollingoption/{INDEX}/{YYYY-MM}/...`` and replays the
Repeated OTM Strangle strategy (see FRD.md) cycle-by-cycle. Emits a
markdown report + CSV of per-cycle results.

Strategy mirror:
  - Cycle opens on every minute that has usable data and no active cycle
    (cooldown = 0), starting 09:30 IST.
  - Base legs: ATM+6 CE (long), ATM-6 PE (long) — ATM computed from
    cycle-start spot, rounded to the index strike interval.
  - Entry: 1-min option bar return >= 1.0 %. Legs watch independently.
  - Base leg SL = entry * 0.85. Lazy leg SL = entry * 0.88.
  - Leg-SL trigger is **intrabar** (``bar.low <= sl_price``). The fill
    price is ``sl_price * (1 - slip_bps / 10000)`` — a sensitivity knob for
    stop-order slippage (``--leg-sl-slip-bps``; 0 matches the Codex
    "perfect-limit-fill" assumption, higher values model real-world slip).
  - Lazy leg: on base SL, open the opposite-side leg at the **fresh ATM**
    taken from spot at the stop minute, **immediately** at that bar's
    close (no momentum gate — mirrors Codex spread_lab.py:1237-1249).
    Strike offset is still ±6 relative to that fresh ATM. Served by the
    appropriate cached offset file.
  - Cycle exit priority: session_close @ 15:15 → MTM_MAX_LOSS → MTM_TARGET
    → LEG_SL (leg-only, cycle continues). MTM is evaluated on bar close.
  - 15:15 force-closes everything and ends the session.
  - Pricing fidelity — the per-strike "fixed frame" captured at entry only
    contains minutes where that strike was within ±``--data-fan`` (default
    6) of ATM. When a strike drifts beyond ±fan its bar is absent for that
    minute, and ``last_price`` (set on the most recent valid bar) acts as
    the implicit ``_get_row_at_or_before`` fallback Codex uses. Default
    mirrors both Codex's MAX_STRIKE_SHIFT=6 fetch and the live engine's
    strike subscription window, so backtest and live see identical tape.

Usage::

    # single run at 100 bps slippage
    python scripts/backtest_2y.py --from 2024-04-23 --to 2026-04-22 --leg-sl-slip-bps 100

    # sensitivity sweep: compare 0 / 50 / 100 / 200 / 500 bps in one pass
    python scripts/backtest_2y.py --from 2024-04-23 --to 2026-04-22 \\
        --sweep-slip 0,50,100,200,500 --csv out/cycles.csv

Reads from the same cache root the fetcher writes to. Failing lookups
(missing month, strike offset out of fan) are skipped with a counter.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Strategy parameters — mirror configs/default.yaml + FRD §5/§9.
MOMENTUM_THRESHOLD_PCT = 1.0
STRIKE_OFFSET_CE = 6
STRIKE_OFFSET_PE = -6
BASE_LEG_SL_PCT = 15.0
LAZY_LEG_SL_PCT = 12.0
LAZY_ENABLED = True
SESSION_START = time(9, 30)
SESSION_END = time(15, 15)

# Strategy risk config — mirrors Codex's "Tight MTM" (the profile that
# produced the headline +Rs 2.3M BANKNIFTY / +Rs 1.7M NIFTY 2y backtest).
# Thresholds:
#   - max_loss = 3500 (cycle force-close if aggregate MTM breaches -3500)
#   - target   = 300  (cycle force-close once aggregate MTM crosses +300)
#   - lock&trail activates at peak>=500, initial floor 350, ratchets 1:1
#     per +100 of additional peak profit.
# Override any of these at the CLI to A/B.
# Lot sizes match the current NSE exchange values (NIFTY=65, BANKNIFTY=30).
# Update here AND in configs/default.yaml if the exchange bumps lot sizes.
INDEX_SPECS = {
    "NIFTY":     {"strike_interval": 50,  "lot_size": 65, "max_loss": 3500, "target": 300},
    "BANKNIFTY": {"strike_interval": 100, "lot_size": 30, "max_loss": 3500, "target": 300},
}

# Lock-and-trail: protects peak profit via a ratcheting floor.
# Activation: peak_mtm >= LOCK_START (else no floor).
# Floor formula (see Codex spread_lab.py:1264-1276):
#   extra_steps = (peak_mtm - LOCK_START) // TRAIL_STEP_PROFIT
#   lock_floor  = LOCK_PROFIT + extra_steps * TRAIL_LOCK_STEP
# With the Tight defaults that works out to lock_floor = peak_mtm - 150
# once peak >= 500: every Rs 100 of additional peak profit ratchets the
# floor up Rs 100, so profit is protected with a 150-rupee buffer.
LOCK_START_RUPEES = 500.0
LOCK_PROFIT_RUPEES = 350.0
TRAIL_STEP_PROFIT_RUPEES = 100.0
TRAIL_LOCK_STEP_RUPEES = 100.0


# ---------------------------------------------------------------------------
# Bar + cache
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Bar:
    minute_epoch: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int
    spot: float
    # Per-bar strike number (in rupees). Dhan's rollingoption re-resolves
    # the ATM+N label bar-by-bar as spot drifts, so two consecutive bars in
    # the same offset file can belong to DIFFERENT contracts. The strike
    # field is the ground truth for "which contract is this bar priced on".
    # 0.0 means unknown (old caches fetched before the fetcher requested
    # the field) — caller should fall back to label-frame continuity.
    strike: float = 0.0

    @property
    def return_pct(self) -> float:
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open * 100.0


class MonthCache:
    """All 22 offset series for one (index, YYYY-MM). Lazy: loads on demand.

    Exposes two views on the same underlying bars:

    * ``bars_for_day(opt_type, offset, session)`` — the **label** view. Keyed
      on ATM offset, this is what "ATM+6 CE right now" looks like bar-by-bar.
      Useful for WATCHING (momentum detection): the strategy watches whatever
      is ATM±6 at each moment, not a fixed strike.
    * ``fixed_bars_for_day(opt_type, session, strike)`` — the **contract**
      view. Keyed on actual strike number, this is one specific option's
      continuous tape. Useful for ACTIVE legs: once we've entered a long at
      strike 49700, we need to keep following the 49700 contract even as
      spot drifts and that strike ceases to be ATM+6.

    The fixed view is built by aggregating bars across ALL offset files in
    the fan. A given (timestamp, strike) appears in at most one offset file
    at a time, so there's no ambiguity.
    """

    def __init__(self, index: str, year_month: str, cache_root: Path,
                 *, data_fan: int | None = None):
        self.index = index
        self.year_month = year_month
        self.cache_root = cache_root
        # Restrict the offset files loaded to |offset| <= data_fan. None
        # means "load whatever is on disk". Lets us A/B whether Codex's
        # narrower fetch fan (MAX_STRIKE_SHIFT=6 in volume-trading) is the
        # source of their backtest's inflated P&L — a strike that drifts
        # outside the fan vanishes from the per-strike frame, letting
        # violent price moves happen invisibly (and leg-SL triggers never
        # fire) during the gap window.
        self.data_fan = data_fan
        # Label view: (option_type, offset) -> sorted list[Bar]
        self._bars: dict[tuple[str, int], list[Bar]] = {}
        # Label view, per-day cache: (option_type, offset, session_iso) -> sublist
        self._by_day: dict[tuple[str, int, str], list[Bar]] = {}
        # Contract view: (option_type, strike_rupees) -> sorted list[Bar]
        self._fixed: dict[tuple[str, float], list[Bar]] = {}
        # Contract view, per-day cache: (option_type, strike_rupees, session_iso) -> sublist
        self._fixed_by_day: dict[tuple[str, float, str], list[Bar]] = {}
        self.loaded = False

    def load(self) -> bool:
        if self.loaded:
            return True
        month_dir = self.cache_root / self.index / self.year_month
        if not month_dir.is_dir():
            self.loaded = True
            return False
        for f in sorted(month_dir.glob("*.json")):
            stem = f.stem  # "CE_ATM+6" or "PE_ATM-3"
            try:
                opt_type, label = stem.split("_", 1)
                if not label.startswith("ATM"):
                    continue
                offset = int(label[3:])   # handles "+6", "-3", "+0", "-0"
            except ValueError:
                continue
            if self.data_fan is not None and abs(offset) > self.data_fan:
                continue
            try:
                body = json.loads(f.read_text())
            except Exception:  # noqa: BLE001
                continue
            leg = (body.get("data") or {}).get("ce" if opt_type == "CE" else "pe")
            if not isinstance(leg, dict):
                continue
            ts = leg.get("timestamp") or []
            op = leg.get("open") or []
            hi = leg.get("high") or []
            lo = leg.get("low") or []
            cl = leg.get("close") or []
            vl = leg.get("volume") or []
            oi = leg.get("oi") or []
            sp = leg.get("spot") or []
            st = leg.get("strike") or []
            n = len(ts)
            bars = [
                Bar(
                    minute_epoch=int(ts[i]),
                    open=float(op[i] or 0), high=float(hi[i] or 0),
                    low=float(lo[i] or 0), close=float(cl[i] or 0),
                    volume=int(vl[i] or 0) if i < len(vl) else 0,
                    oi=int(oi[i] or 0) if i < len(oi) else 0,
                    spot=float(sp[i] or 0) if i < len(sp) else 0.0,
                    strike=float(st[i] or 0) if i < len(st) else 0.0,
                )
                for i in range(n)
            ]
            self._bars[(opt_type, offset)] = bars
        self._build_fixed_index()
        self.loaded = True
        return bool(self._bars)

    def _build_fixed_index(self) -> None:
        """Aggregate bars across offset files, keyed on actual strike.

        The same (timestamp, strike) shouldn't appear twice across different
        offset files (a given strike is ATM+N for exactly one N at any given
        moment), but if it does — e.g. a boundary bar where Dhan's internal
        ATM call flips — we keep the first occurrence. The per-strike frames
        are sorted and deduped by minute_epoch downstream.
        """
        grouped: dict[tuple[str, float], dict[int, Bar]] = {}
        for (opt_type, _offset), bars in self._bars.items():
            for b in bars:
                if b.strike <= 0:
                    continue
                key = (opt_type, float(b.strike))
                by_min = grouped.setdefault(key, {})
                # Prefer the first-seen bar at a given minute; offset files
                # are walked in label order so this is deterministic.
                if b.minute_epoch not in by_min:
                    by_min[b.minute_epoch] = b
        for key, by_min in grouped.items():
            sorted_bars = [by_min[m] for m in sorted(by_min)]
            self._fixed[key] = sorted_bars

    def bars_for_day(self, option_type: str, offset: int, session: date) -> list[Bar]:
        key = (option_type, offset, session.isoformat())
        cached = self._by_day.get(key)
        if cached is not None:
            return cached
        bars = self._bars.get((option_type, offset))
        if not bars:
            self._by_day[key] = []
            return []
        day_bars = [
            b for b in bars
            if datetime.fromtimestamp(b.minute_epoch, tz=IST).date() == session
        ]
        day_bars.sort(key=lambda b: b.minute_epoch)
        self._by_day[key] = day_bars
        return day_bars

    def fixed_bars_for_day(
        self, option_type: str, session: date, strike: float,
    ) -> list[Bar]:
        """Per-day bars for a specific strike, continuous across ATM drift.

        Returns an empty list if the strike never appears in this month's
        fan on that session (e.g. spot drifted so far the contract left the
        ±fan window). Caller should then fall back to the label frame.
        """
        if strike <= 0:
            return []
        key = (option_type, float(strike), session.isoformat())
        cached = self._fixed_by_day.get(key)
        if cached is not None:
            return cached
        bars = self._fixed.get((option_type, float(strike)))
        if not bars:
            self._fixed_by_day[key] = []
            return []
        day_bars = [
            b for b in bars
            if datetime.fromtimestamp(b.minute_epoch, tz=IST).date() == session
        ]
        self._fixed_by_day[key] = day_bars
        return day_bars


class CacheStore:
    """Holds month caches across indices. LRU by (index, year_month)."""

    def __init__(self, cache_root: Path, *, data_fan: int | None = None):
        self.cache_root = cache_root
        self.data_fan = data_fan
        self._cache: dict[tuple[str, str], MonthCache] = {}

    def get(self, index: str, session: date) -> MonthCache:
        ym = f"{session.year:04d}-{session.month:02d}"
        key = (index, ym)
        mc = self._cache.get(key)
        if mc is None:
            mc = MonthCache(index, ym, self.cache_root, data_fan=self.data_fan)
            mc.load()
            self._cache[key] = mc
        return mc


# ---------------------------------------------------------------------------
# Strategy state
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Leg:
    slot: int
    kind: Literal["BASE", "LAZY"]
    option_type: Literal["CE", "PE"]
    offset: int                       # signed, label-frame lookup key
    # Label-frame bars, keyed by minute. Used for WATCHING (momentum
    # detection operates on "whatever is ATM+N right now"). Once the leg
    # becomes ACTIVE it switches to `fixed_bars_by_minute` below — SL /
    # target tracking must follow ONE specific strike, not the rolling
    # label.
    bars_by_minute: dict[int, Bar]
    lot_size: int
    lots: int = 1
    status: Literal["WATCHING", "ACTIVE", "STOPPED", "EMPTY"] = "WATCHING"
    entry_price: float = 0.0
    sl_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    last_price: float = 0.0
    # Populated at the WATCHING -> ACTIVE transition: the actual strike
    # (rupees) captured from the entry bar, and the per-minute dict of that
    # contract's bars for the rest of the session. Reads from here during
    # ACTIVE; falls back to bars_by_minute if the fixed frame is missing
    # (e.g. strike is 0 because the cache predates the strike-field fetch).
    entry_strike: float = 0.0
    fixed_bars_by_minute: dict[int, Bar] | None = None
    # Trade-trace timestamps, for leg-level reporting comparable to
    # Codex's spread_leg_results.csv.
    entry_epoch: int = 0
    exit_epoch: int = 0

    @property
    def quantity(self) -> int:
        return self.lots * self.lot_size

    @property
    def realized_pnl(self) -> float:
        if self.status != "STOPPED" or self.exit_price <= 0:
            return 0.0
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def unrealized_pnl(self) -> float:
        if self.status != "ACTIVE" or self.entry_price <= 0:
            return 0.0
        return (self.last_price - self.entry_price) * self.quantity

    def active_bar(self, minute_epoch: int) -> Bar | None:
        """Bar to evaluate against while ACTIVE — prefer fixed-strike frame.

        Returns the EXACT minute match or None. A None return means "no
        fresh bar for this strike this minute" (e.g. the strike has drifted
        outside the loaded cache fan). The caller is expected to hold
        ``last_price`` over from the previous valid bar and use it for
        MTM / force-close — that's the ``_get_row_at_or_before`` semantic
        Codex uses in ``spread_lab._get_row_at_or_before`` (spread_lab.py
        L928-935), and it's how backtest + live agree on stale-bar
        handling when a contract goes dark during a fast move.

        Why not return the label frame instead? Because the label frame
        always points at "whatever strike is ATM±N right now", which is a
        DIFFERENT contract once the original strike has drifted. Reading
        from it would splice two contracts' tapes together.
        """
        if self.fixed_bars_by_minute is not None:
            return self.fixed_bars_by_minute.get(minute_epoch)
        return self.bars_by_minute.get(minute_epoch)


@dataclass(slots=True)
class CycleResult:
    underlying: str
    session_date: date
    cycle_no: int
    started_at: datetime
    ended_at: datetime
    atm_at_start: int
    ce_strike: int
    pe_strike: int
    exit_reason: str
    cycle_pnl: float
    peak_mtm: float
    trough_mtm: float
    legs_entered: int


@dataclass(slots=True)
class LegTrade:
    """Mirror of one row in Codex's spread_leg_results.csv for side-by-side
    trade-level comparison."""
    underlying: str
    session_date: date
    cycle_no: int
    slot: str              # "Base Call" / "Base Put" / "Lazy Call" / "Lazy Put"
    role: str              # "base" / "lazy"
    option_type: str       # "CE" / "PE"
    series_shift: int      # signed offset used to find the label frame
    entry_strike: float    # actual strike captured at entry (0 if label fallback)
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    exit_reason: str
    profit_amount: float


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _iter_sessions(start: date, end: date) -> list[date]:
    """All weekdays in [start, end]. NSE holidays are skipped by absent bars."""
    days: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _round_to_atm(spot: float, interval: int) -> int:
    # Standard-half-up, with tie going lower to match engine behaviour.
    if spot <= 0:
        return 0
    lower = int(spot // interval * interval)
    upper = lower + interval
    if abs(spot - lower) <= abs(upper - spot):
        return lower
    return upper


def _session_minutes(session: date) -> list[int]:
    out: list[int] = []
    t = datetime.combine(session, SESSION_START, tzinfo=IST)
    end = datetime.combine(session, SESSION_END, tzinfo=IST)
    while t <= end:
        out.append(int(t.timestamp()))
        t += timedelta(minutes=1)
    return out


def _bars_by_minute(bars: list[Bar]) -> dict[int, Bar]:
    return {b.minute_epoch: b for b in bars}


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ReplayStats:
    sessions_simulated: int = 0
    sessions_skipped_no_data: int = 0
    cycles: int = 0
    lazy_ce_out_of_fan: int = 0
    lazy_pe_out_of_fan: int = 0


def simulate_session(
    underlying: str, session: date, store: CacheStore, fan: int, stats: ReplayStats,
    *, leg_sl_slip_bps: int = 0, disable_fixed_strike: bool = False,
    leg_trades_out: list[LegTrade] | None = None,
) -> list[CycleResult]:
    spec = INDEX_SPECS[underlying]
    interval = spec["strike_interval"]

    mc = store.get(underlying, session)
    if not mc.loaded or not mc._bars:  # noqa: SLF001
        stats.sessions_skipped_no_data += 1
        return []

    # Base bars for today (spot lives inside every bar).
    base_ce_bars = mc.bars_for_day("CE", STRIKE_OFFSET_CE, session)
    base_pe_bars = mc.bars_for_day("PE", STRIKE_OFFSET_PE, session)
    if not base_ce_bars and not base_pe_bars:
        stats.sessions_skipped_no_data += 1
        return []

    # Spot series — prefer CE+6 series, fall back to PE-6.
    spot_src = base_ce_bars if base_ce_bars else base_pe_bars
    spot_by_minute = {b.minute_epoch: b.spot for b in spot_src if b.spot > 0}

    # Day-open ATM — from the first session-window bar that has a positive spot.
    session_minutes = _session_minutes(session)
    day_atm = 0
    for m in session_minutes:
        if spot_by_minute.get(m, 0.0) > 0:
            day_atm = _round_to_atm(spot_by_minute[m], interval)
            break
    if day_atm == 0:
        stats.sessions_skipped_no_data += 1
        return []

    results: list[CycleResult] = []
    cycle_no = 0
    i = 0

    # Cycles loop: open a new one whenever no cycle is active.
    while i < len(session_minutes):
        m = session_minutes[i]
        spot = spot_by_minute.get(m, 0.0)
        if spot <= 0:
            i += 1
            continue

        atm = _round_to_atm(spot, interval)
        cycle_no += 1

        # Base leg data pulls from the canonical offsets.
        ce_bars = mc.bars_for_day("CE", STRIKE_OFFSET_CE, session)
        pe_bars = mc.bars_for_day("PE", STRIKE_OFFSET_PE, session)

        legs: list[Leg] = []
        if ce_bars:
            legs.append(Leg(
                slot=1, kind="BASE", option_type="CE",
                offset=STRIKE_OFFSET_CE, bars_by_minute=_bars_by_minute(ce_bars),
                lot_size=spec["lot_size"],
            ))
        if pe_bars:
            legs.append(Leg(
                slot=2, kind="BASE", option_type="PE",
                offset=STRIKE_OFFSET_PE, bars_by_minute=_bars_by_minute(pe_bars),
                lot_size=spec["lot_size"],
            ))
        if not legs:
            i += 1
            cycle_no -= 1
            continue

        ce_strike = atm + STRIKE_OFFSET_CE * interval
        pe_strike = atm + STRIKE_OFFSET_PE * interval

        peak_mtm = 0.0
        trough_mtm = 0.0
        cycle_exit_reason = ""
        cycle_started_m = m
        lazy_ce_done = False
        lazy_pe_done = False
        legs_entered = 0

        while i < len(session_minutes):
            m_now = session_minutes[i]

            # 1) Evaluate each leg's own bar at this minute.
            for leg in legs:
                # WATCHING reads from the label frame (we care about
                # whatever is ATM+N right now); ACTIVE reads from the
                # entry-captured fixed strike (we must follow the specific
                # contract we're long, not the rolling label).
                if leg.status == "WATCHING":
                    bar = leg.bars_by_minute.get(m_now)
                    if bar is None or bar.close <= 0:
                        continue
                    leg.last_price = bar.close
                    if bar.return_pct >= MOMENTUM_THRESHOLD_PCT:
                        leg.entry_price = bar.close
                        sl_pct = BASE_LEG_SL_PCT if leg.kind == "BASE" else LAZY_LEG_SL_PCT
                        leg.sl_price = round(bar.close * (1 - sl_pct / 100.0), 2)
                        # Capture the strike of the entry bar and switch
                        # the leg to follow that specific contract. If
                        # strike is 0 (old cache without the field), the
                        # leg keeps using the label frame — a known
                        # approximation, flagged via fixed_bars_by_minute
                        # staying None. The disable_fixed_strike escape
                        # hatch is purely for A/B debugging to measure the
                        # fix's impact against a fresh cache.
                        if (not disable_fixed_strike) and bar.strike > 0:
                            fixed = mc.fixed_bars_for_day(
                                leg.option_type, session, bar.strike,
                            )
                            if fixed:
                                leg.entry_strike = float(bar.strike)
                                leg.fixed_bars_by_minute = _bars_by_minute(fixed)
                        leg.status = "ACTIVE"
                        leg.entry_epoch = m_now
                        legs_entered += 1
                elif leg.status == "ACTIVE":
                    bar = leg.active_bar(m_now)
                    if bar is None or bar.close <= 0:
                        continue
                    leg.last_price = bar.close
                    # Intrabar stop-out: triggers the moment the bar's low
                    # touches the stop price. Fill is modelled as
                    #   fill = sl_price * (1 - slip_bps / 10000)
                    # (slip_bps=0 reproduces Codex's "perfect stop-limit fill".)
                    if bar.low <= leg.sl_price:
                        fill_price = leg.sl_price * (1.0 - leg_sl_slip_bps / 10000.0)
                        # Never fill below the bar's own low — that's
                        # physically impossible in a 1-min OHLC world.
                        if fill_price < bar.low:
                            fill_price = bar.low
                        leg.exit_price = round(fill_price, 2)
                        leg.status = "STOPPED"
                        leg.exit_reason = "LEG_SL"
                        leg.exit_epoch = m_now
                        if LAZY_ENABLED and leg.kind == "BASE":
                            entered = _schedule_lazy(
                                stopped=leg, legs=legs, mc=mc, session=session,
                                spot_by_minute=spot_by_minute, m_now=m_now,
                                interval=interval, lot_size=spec["lot_size"],
                                day_atm=day_atm, fan=fan,
                                lazy_ce_done=lazy_ce_done, lazy_pe_done=lazy_pe_done,
                                stats=stats,
                                disable_fixed_strike=disable_fixed_strike,
                            )
                            # Lazy now enters IMMEDIATELY at the stop-minute
                            # bar close (no momentum gate — matches Codex).
                            # Track legs_entered against the base-leg accumulator
                            # so it shows up in per-cycle reporting.
                            legs_entered += entered
                            # "Done" flag flips whether or not the lazy
                            # actually entered (a skipped lazy still burns
                            # the one-per-cycle allowance for that side).
                            if leg.option_type == "CE":
                                lazy_pe_done = True
                            else:
                                lazy_ce_done = True

            # 2) Cycle MTM (realized + unrealized).
            mtm = sum(l.realized_pnl for l in legs) + sum(l.unrealized_pnl for l in legs)
            if mtm > peak_mtm:
                peak_mtm = mtm
            if mtm < trough_mtm:
                trough_mtm = mtm

            # 3) Session close is absolute — always wins priority.
            t_ist = datetime.fromtimestamp(m_now, tz=IST).time()
            if t_ist >= SESSION_END:
                cycle_exit_reason = "SESSION_CLOSE"
                break

            # 4) Aggregate-MTM (cycle-portfolio) exits fire while at least
            #    one leg is ACTIVE. Mirrors Codex spread_lab.py:1251 — if
            #    all open positions have closed and we're just waiting on
            #    a WATCHING lazy/base to enter, the portfolio P&L is
            #    purely realized and the cycle stays "alive" until either
            #    a new leg enters or the cycle-done check below breaks.
            any_active = any(leg.status == "ACTIVE" for leg in legs)
            if any_active:
                # 5) Lock & trail — compute the protective floor based on
                #    peak_mtm so far. Mirror of Codex spread_lab.py:1264-1276.
                #    Once peak has cleared LOCK_START, the floor ratchets up
                #    with peak so winners can't round-trip back to zero.
                lock_floor: float | None = None
                if peak_mtm >= LOCK_START_RUPEES:
                    extra_steps = int(
                        max(0.0, peak_mtm - LOCK_START_RUPEES)
                        // TRAIL_STEP_PROFIT_RUPEES
                    )
                    lock_floor = (
                        LOCK_PROFIT_RUPEES + extra_steps * TRAIL_LOCK_STEP_RUPEES
                    )

                # 6) Exit checks — priority order mirrors Codex exactly:
                #    SESSION_CLOSE (handled above) > MTM_MAX_LOSS >
                #    MTM_TARGET > LOCK_TRAIL.
                if mtm <= -abs(spec["max_loss"]):
                    cycle_exit_reason = "MTM_MAX_LOSS"
                    break
                target = spec.get("target")
                if target is not None and mtm >= target:
                    cycle_exit_reason = "MTM_TARGET"
                    break
                if lock_floor is not None and mtm <= lock_floor:
                    cycle_exit_reason = "LOCK_TRAIL"
                    break

            # 7) Cycle-done check — a cycle is "done" when there are no
            #    legs in ACTIVE or WATCHING state. This means:
            #      (a) both bases SL'd and no lazy was eligible, OR
            #      (b) everyone exited via the aggregate-MTM trigger
            #          (handled via the `break` above).
            #    Without this gate, a cycle that had both legs stopped
            #    out would idle to session close and block any new cycle
            #    from starting — exactly the "mine: 1 cycle vs codex: 11"
            #    divergence we chased down on 2025-04-23 NIFTY. Once the
            #    cycle is done, we set exit_reason from the last leg's
            #    exit reason (mirroring Codex spread_lab.py:1360) and
            #    break so the outer restart loop can open the next cycle
            #    one bar later (cooldown=0).
            has_watching = any(leg.status == "WATCHING" for leg in legs)
            if not any_active and not has_watching and legs_entered > 0:
                # Pick the last-exiting leg's reason as the cycle's exit
                # reason (matches Codex's "if all legs SL-out, cycle
                # exit_reason = last leg's LEG_SL" fallback).
                latest_leg = max(
                    (l for l in legs if l.status == "STOPPED" and l.exit_epoch),
                    key=lambda l: l.exit_epoch,
                    default=None,
                )
                cycle_exit_reason = latest_leg.exit_reason if latest_leg else "LEG_SL"
                # Advance the loop index so `session_minutes[end_idx]`
                # reflects the actual last-leg exit minute (not one past).
                if latest_leg is not None:
                    try:
                        i = session_minutes.index(latest_leg.exit_epoch)
                    except ValueError:
                        pass
                break

            i += 1

        # Force-close still-ACTIVE legs at last_price.
        end_idx = min(i, len(session_minutes) - 1)
        force_close_m = session_minutes[end_idx]
        for leg in legs:
            if leg.status == "ACTIVE":
                leg.exit_price = leg.last_price or leg.entry_price
                leg.status = "STOPPED"
                leg.exit_reason = cycle_exit_reason or "SESSION_CLOSE"
                leg.exit_epoch = force_close_m

        if not cycle_exit_reason:
            cycle_exit_reason = "SESSION_CLOSE"

        # Emit per-leg trade records (for Codex-style comparison) for every
        # leg that actually entered this cycle. WATCHING-only legs are
        # skipped — they never opened a position.
        if leg_trades_out is not None:
            for leg in legs:
                if leg.entry_epoch == 0:
                    continue
                if leg.kind == "BASE":
                    slot_name = "Base Call" if leg.option_type == "CE" else "Base Put"
                    role = "base"
                else:
                    slot_name = "Lazy Call" if leg.option_type == "CE" else "Lazy Put"
                    role = "lazy"
                leg_trades_out.append(LegTrade(
                    underlying=underlying,
                    session_date=session,
                    cycle_no=cycle_no,
                    slot=slot_name,
                    role=role,
                    option_type=leg.option_type,
                    series_shift=leg.offset,
                    entry_strike=leg.entry_strike,
                    entry_time=datetime.fromtimestamp(leg.entry_epoch, tz=IST),
                    exit_time=datetime.fromtimestamp(leg.exit_epoch or force_close_m, tz=IST),
                    entry_price=leg.entry_price,
                    exit_price=leg.exit_price,
                    exit_reason=leg.exit_reason or cycle_exit_reason,
                    profit_amount=round(
                        (leg.exit_price - leg.entry_price) * leg.quantity, 2
                    ),
                ))

        final_mtm = sum(l.realized_pnl for l in legs)
        started_dt = datetime.fromtimestamp(cycle_started_m, tz=IST)
        ended_dt = datetime.fromtimestamp(session_minutes[end_idx], tz=IST)
        results.append(CycleResult(
            underlying=underlying, session_date=session, cycle_no=cycle_no,
            started_at=started_dt, ended_at=ended_dt, atm_at_start=atm,
            ce_strike=ce_strike, pe_strike=pe_strike,
            exit_reason=cycle_exit_reason, cycle_pnl=round(final_mtm, 2),
            peak_mtm=round(peak_mtm, 2), trough_mtm=round(trough_mtm, 2),
            legs_entered=legs_entered,
        ))
        stats.cycles += 1
        i += 1   # next minute starts the next cycle

    stats.sessions_simulated += 1
    return results


def _schedule_lazy(
    *,
    stopped: Leg, legs: list[Leg], mc: MonthCache, session: date,
    spot_by_minute: dict[int, float], m_now: int, interval: int, lot_size: int,
    day_atm: int, fan: int,
    lazy_ce_done: bool, lazy_pe_done: bool,
    stats: ReplayStats,
    disable_fixed_strike: bool = False,
) -> int:
    """Open a lazy leg on the opposite side at fresh ATM±6, **immediately**
    at the stop minute's bar close (no momentum gate — mirrors Codex
    spread_lab.py:1047-1088 + 1237-1249, where a lazy entry scheduled at
    the stop minute is built into a position and added to open_positions
    on that same iteration).

    Returns 1 if the lazy leg entered ACTIVE, 0 otherwise (out-of-fan,
    no bar at stop minute, or missing data). The caller uses this to
    advance `legs_entered`."""
    is_ce_stopped = stopped.option_type == "CE"
    if is_ce_stopped and lazy_pe_done:
        return 0
    if (not is_ce_stopped) and lazy_ce_done:
        return 0

    fresh_spot = spot_by_minute.get(m_now, 0.0)
    if fresh_spot <= 0:
        return 0
    fresh_atm = _round_to_atm(fresh_spot, interval)
    drift_strikes = (fresh_atm - day_atm) // interval

    if is_ce_stopped:
        # Lazy PE — strike = fresh_atm - 6*interval → offset vs day_atm = drift - 6
        desired_offset = drift_strikes + STRIKE_OFFSET_PE
        opt_type = "PE"
    else:
        # Lazy CE — strike = fresh_atm + 6*interval → offset = drift + 6
        desired_offset = drift_strikes + STRIKE_OFFSET_CE
        opt_type = "CE"

    if abs(desired_offset) > fan:
        if opt_type == "CE":
            stats.lazy_ce_out_of_fan += 1
        else:
            stats.lazy_pe_out_of_fan += 1
        return 0

    lazy_bars = mc.bars_for_day(opt_type, desired_offset, session)
    if not lazy_bars:
        return 0

    # Locate the bar at the current (stop) minute. If that exact bar is
    # missing, bail — Codex similarly bails via `_get_row_at_or_after`
    # returning None, and we don't want to enter a lazy with stale data.
    bars_map = _bars_by_minute(lazy_bars)
    entry_bar = bars_map.get(m_now)
    if entry_bar is None or entry_bar.close <= 0:
        return 0

    new_leg = Leg(
        slot=3 if opt_type == "PE" else 4,
        kind="LAZY", option_type=opt_type,
        offset=desired_offset,
        bars_by_minute=bars_map,
        lot_size=lot_size,
    )
    # Immediate entry — no momentum check. Lazy SL is 12% (vs base 15%).
    new_leg.entry_price = entry_bar.close
    new_leg.last_price = entry_bar.close
    new_leg.sl_price = round(
        entry_bar.close * (1 - LAZY_LEG_SL_PCT / 100.0), 2
    )
    new_leg.status = "ACTIVE"
    new_leg.entry_epoch = m_now

    # Capture the specific contract's bars (fixed-strike frame), same
    # logic as the base-leg entry path at simulate_session line 569-575.
    if (not disable_fixed_strike) and entry_bar.strike > 0:
        fixed = mc.fixed_bars_for_day(
            opt_type, session, entry_bar.strike,
        )
        if fixed:
            new_leg.entry_strike = float(entry_bar.strike)
            new_leg.fixed_bars_by_minute = _bars_by_minute(fixed)

    legs.append(new_leg)
    return 1


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _fmt_inr(v: float) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.2f}"


def _summarise(cycles: list[CycleResult]) -> dict:
    if not cycles:
        return {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg": 0.0,
                "best": 0.0, "worst": 0.0}
    pnl = sum(c.cycle_pnl for c in cycles)
    wins = sum(1 for c in cycles if c.cycle_pnl > 0)
    losses = sum(1 for c in cycles if c.cycle_pnl <= 0)
    total = wins + losses
    return {
        "n": len(cycles),
        "pnl": pnl,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total * 100.0) if total else 0.0,
        "avg": pnl / len(cycles),
        "best": max(c.cycle_pnl for c in cycles),
        "worst": min(c.cycle_pnl for c in cycles),
    }


def _group(cycles: list[CycleResult], key):
    out: dict = {}
    for c in cycles:
        out.setdefault(key(c), []).append(c)
    return out


def _print_report(cycles: list[CycleResult], stats: ReplayStats,
                  start: date, end: date, indices: list[str]) -> None:
    print("# Backtest replay report\n")
    print(f"- Range: **{start}** .. **{end}**")
    print(f"- Indices: {', '.join(indices)}")
    print(f"- Sessions simulated: **{stats.sessions_simulated}**")
    print(f"- Sessions skipped (no cache data): **{stats.sessions_skipped_no_data}**")
    print(f"- Cycles simulated: **{len(cycles)}**")
    print(f"- Lazy legs skipped (out of fan): "
          f"CE={stats.lazy_ce_out_of_fan} PE={stats.lazy_pe_out_of_fan}")
    print()

    print("## Totals\n")
    print("| Scope | Cycles | P&L (Rs) | Wins | Losses | Win % | Avg P&L | Best | Worst |")
    print("|:--|--:|--:|--:|--:|--:|--:|--:|--:|")
    for scope in ["ALL"] + indices:
        subset = cycles if scope == "ALL" else [c for c in cycles if c.underlying == scope]
        s = _summarise(subset)
        print(f"| {scope} | {s['n']} | {_fmt_inr(s['pnl'])} | {s['wins']} | {s['losses']} "
              f"| {s['win_rate']:.1f}% | {_fmt_inr(s['avg'])} | {_fmt_inr(s['best'])} "
              f"| {_fmt_inr(s['worst'])} |")

    print("\n## By year\n")
    print("| Year | Idx | Cycles | P&L (Rs) | Win % | Avg P&L |")
    print("|:--|:--|--:|--:|--:|--:|")
    for y in sorted(_group(cycles, lambda c: c.session_date.year)):
        for idx in indices:
            sub = [c for c in cycles
                   if c.session_date.year == y and c.underlying == idx]
            if not sub:
                continue
            s = _summarise(sub)
            print(f"| {y} | {idx} | {s['n']} | {_fmt_inr(s['pnl'])} "
                  f"| {s['win_rate']:.1f}% | {_fmt_inr(s['avg'])} |")

    print("\n## By month (all indices combined)\n")
    print("| Month | Cycles | P&L (Rs) | Win % | Avg P&L |")
    print("|:--|--:|--:|--:|--:|")
    for k in sorted(_group(cycles, lambda c: c.session_date.strftime("%Y-%m"))):
        sub = [c for c in cycles if c.session_date.strftime("%Y-%m") == k]
        s = _summarise(sub)
        print(f"| {k} | {s['n']} | {_fmt_inr(s['pnl'])} "
              f"| {s['win_rate']:.1f}% | {_fmt_inr(s['avg'])} |")

    print("\n## Exit-reason mix\n")
    print("| Reason | Count | % of cycles | Total P&L | Avg P&L |")
    print("|:--|--:|--:|--:|--:|")
    by_reason = _group(cycles, lambda c: c.exit_reason)
    for reason in sorted(by_reason):
        sub = by_reason[reason]
        s = _summarise(sub)
        pct = (s["n"] / len(cycles) * 100.0) if cycles else 0.0
        print(f"| {reason} | {s['n']} | {pct:.1f}% | {_fmt_inr(s['pnl'])} "
              f"| {_fmt_inr(s['avg'])} |")


def _write_csv(cycles: list[CycleResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "session_date", "underlying", "cycle_no", "started_at", "ended_at",
            "atm_at_start", "ce_strike", "pe_strike",
            "exit_reason", "legs_entered", "cycle_pnl", "peak_mtm", "trough_mtm",
        ])
        for c in cycles:
            w.writerow([
                c.session_date.isoformat(), c.underlying, c.cycle_no,
                c.started_at.isoformat(), c.ended_at.isoformat(),
                c.atm_at_start, c.ce_strike, c.pe_strike,
                c.exit_reason, c.legs_entered,
                f"{c.cycle_pnl:.2f}", f"{c.peak_mtm:.2f}", f"{c.trough_mtm:.2f}",
            ])


# ---------------------------------------------------------------------------
# Trace (per-leg, one session)
# ---------------------------------------------------------------------------

def _print_trace(legs: list[LegTrade], trace_day: date, indices: list[str]) -> None:
    """Emit a Codex-style per-leg table for one session.

    Columns mirror volume-trading's spread_leg_results.csv so the two can
    be diffed side-by-side by cycle_no. Totals printed per (index, cycle)
    and overall.
    """
    print(f"\n# === Per-leg trade trace for {trace_day} ===")
    print("# (columns mirror Codex's spread_leg_results.csv)\n")

    # Sort by (index, cycle_no, entry_time) so cycles are contiguous.
    legs = sorted(legs, key=lambda x: (x.underlying, x.cycle_no, x.entry_time))

    header = (
        "| Idx | Cyc | Slot       | Type | Shift | Strike  | Entry  | Exit   "
        "| EntryPx  | ExitPx   | Exit reason     | Profit     |"
    )
    sep = (
        "|:----|----:|:-----------|:-----|------:|--------:|:-------|:-------"
        "|---------:|---------:|:----------------|-----------:|"
    )
    print(header)
    print(sep)

    for lt in legs:
        strike_str = f"{int(lt.entry_strike):>7d}" if lt.entry_strike > 0 else "     -- "
        print(
            f"| {lt.underlying:<4} "
            f"| {lt.cycle_no:>3d} "
            f"| {lt.slot:<10} "
            f"| {lt.option_type:<4} "
            f"| {lt.series_shift:>+5d} "
            f"| {strike_str} "
            f"| {lt.entry_time.strftime('%H:%M'):<6} "
            f"| {lt.exit_time.strftime('%H:%M'):<6} "
            f"| {lt.entry_price:>8.2f} "
            f"| {lt.exit_price:>8.2f} "
            f"| {lt.exit_reason:<15} "
            f"| {lt.profit_amount:>+10.2f} |"
        )

    # Per-cycle totals.
    print("\n## Per-cycle totals\n")
    print("| Idx | Cyc | Legs | Cycle P&L (Rs) |")
    print("|:----|----:|-----:|---------------:|")
    per_cycle: dict[tuple[str, int], list[LegTrade]] = {}
    for lt in legs:
        per_cycle.setdefault((lt.underlying, lt.cycle_no), []).append(lt)
    for (idx, cyc) in sorted(per_cycle):
        sub = per_cycle[(idx, cyc)]
        pnl = sum(x.profit_amount for x in sub)
        print(f"| {idx:<4} | {cyc:>3d} | {len(sub):>4d} | {_fmt_inr(pnl):>14} |")

    # Index totals.
    print("\n## Session totals\n")
    print("| Idx | Legs | Cycles | P&L (Rs) |")
    print("|:----|-----:|-------:|---------:|")
    for idx in indices:
        sub = [x for x in legs if x.underlying == idx]
        if not sub:
            continue
        cycles = {x.cycle_no for x in sub}
        pnl = sum(x.profit_amount for x in sub)
        print(f"| {idx:<4} | {len(sub):>4d} | {len(cycles):>6d} | {_fmt_inr(pnl):>8} |")
    total = sum(x.profit_amount for x in legs)
    print(f"\n**ALL:** {_fmt_inr(total)}")


def _write_trace_csv(legs: list[LegTrade], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "session_date", "underlying", "cycle_no",
            "slot", "role", "option_type", "series_shift", "entry_strike",
            "entry_time", "exit_time", "entry_price", "exit_price",
            "exit_reason", "profit_amount",
        ])
        for lt in sorted(legs, key=lambda x: (x.underlying, x.cycle_no, x.entry_time)):
            w.writerow([
                lt.session_date.isoformat(), lt.underlying, lt.cycle_no,
                lt.slot, lt.role, lt.option_type, lt.series_shift,
                f"{lt.entry_strike:.2f}",
                lt.entry_time.isoformat(), lt.exit_time.isoformat(),
                f"{lt.entry_price:.2f}", f"{lt.exit_price:.2f}",
                lt.exit_reason, f"{lt.profit_amount:.2f}",
            ])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    ap.add_argument("--to",   dest="to_date",   type=_parse_date, required=True)
    ap.add_argument("--index", default="both",
                    choices=["NIFTY", "BANKNIFTY", "both"])
    ap.add_argument("--fan", type=int, default=6,
                    help="Max |offset| the sim will request from the cache; "
                         "lazy legs beyond this are skipped. Default 6 — matches "
                         "Codex's MAX_STRIKE_SHIFT=6 and the live engine's ±6 "
                         "strike-offset config.")
    ap.add_argument("--csv", default=None,
                    help="Optional path to write per-cycle CSV. "
                         "In --sweep-slip mode each run gets a '.slip{N}bps' suffix.")
    ap.add_argument("--cache-root", default=None,
                    help="Override cache root")
    ap.add_argument("--leg-sl-slip-bps", type=int, default=100,
                    help="Slippage applied to the leg-SL fill, in basis points "
                         "(1 bp = 0.01%%). fill = sl_price * (1 - bps/10000). "
                         "Default 100 bps (~1%%). Use 0 for Codex-style perfect fills.")
    ap.add_argument("--sweep-slip", default=None,
                    help="Comma-separated list of slippage bps to sweep, "
                         "e.g. '0,50,100,200,500'. Runs the full replay once per "
                         "value and prints a comparison table. Overrides "
                         "--leg-sl-slip-bps if present.")
    ap.add_argument("--disable-fixed-strike", action="store_true",
                    help="A/B debug: skip the entry-bar strike capture and keep "
                         "ACTIVE legs reading the label frame (the old, buggy "
                         "behavior where 'ATM+6' is treated as one continuous "
                         "contract). Use only to measure the fix's impact.")
    ap.add_argument("--max-loss", type=float, default=None,
                    help="Override INDEX_SPECS[*]['max_loss'] for both indices. "
                         "Codex Tight=3500.")
    ap.add_argument("--target", type=float, default=None,
                    help="Override INDEX_SPECS[*]['target']. Pass 0 to disable "
                         "(letting winners run to lock&trail / EOD). Codex "
                         "Tight has no target.")
    ap.add_argument("--no-lock-trail", action="store_true",
                    help="Disable the lock-and-trail floor entirely, so cycles "
                         "can only exit via SL / target / session-close. Use to "
                         "measure lock&trail's contribution.")
    ap.add_argument("--trace-day", default=None,
                    help="YYYY-MM-DD — after the replay, print a per-leg trade "
                         "table for that session (columns mirror Codex's "
                         "spread_leg_results.csv) to stdout. Combine with a "
                         "tight --from/--to window for quick A/B inspection.")
    ap.add_argument("--trace-csv", default=None,
                    help="Optional path to write the per-leg trade table "
                         "(captured whenever --trace-day is set) as CSV.")
    ap.add_argument("--data-fan", type=int, default=6,
                    help="Cap the loaded cache to offset files with |offset| "
                         "<= N, so the per-strike fixed frame only contains "
                         "minutes where that strike was within ±N of ATM. "
                         "Default 6 — matches (a) the live engine's strike "
                         "subscription window (strike_offset_ce=+6, "
                         "strike_offset_pe=-6) and (b) Codex's "
                         "MAX_STRIKE_SHIFT=6 fetch. This is the 'Codex "
                         "parity' pricing path: when a strike drifts beyond "
                         "±N its fixed frame gets no new rows, so subsequent "
                         "minutes fall back to the last-known close (the "
                         "_get_row_at_or_before semantics Codex uses). "
                         "Set to a wider value (e.g. 10) only for A/B "
                         "sensitivity studies against a richer cache.")
    args = ap.parse_args()

    if args.from_date > args.to_date:
        print("ERROR: --from must be <= --to")
        return 2

    # CLI overrides for risk config. Mutates the module-level dict; fine
    # because each invocation is one-shot.
    global LOCK_START_RUPEES  # noqa: PLW0603
    if args.max_loss is not None:
        for k in INDEX_SPECS:
            INDEX_SPECS[k]["max_loss"] = float(args.max_loss)
    if args.target is not None:
        for k in INDEX_SPECS:
            INDEX_SPECS[k]["target"] = None if args.target <= 0 else float(args.target)
    if args.no_lock_trail:
        # Set lock_start absurdly high so the activation check never fires.
        LOCK_START_RUPEES = float("inf")

    repo_root = Path(__file__).resolve().parent.parent
    if args.cache_root:
        cache_root = Path(args.cache_root).expanduser().resolve()
    else:
        data_dir = Path(os.getenv("VOLSCALP_DATA_DIR", "") or (repo_root / "data"))
        if not data_dir.is_absolute():
            data_dir = repo_root / data_dir
        cache_root = data_dir / "backtest_cache" / "rollingoption"

    if not cache_root.is_dir():
        print(f"ERROR: cache root not found: {cache_root}")
        print("Run scripts/backtest_history_fetch.py first.")
        return 2

    indices = ["NIFTY", "BANKNIFTY"] if args.index == "both" else [args.index]
    store = CacheStore(cache_root, data_fan=args.data_fan)

    sessions = _iter_sessions(args.from_date, args.to_date)
    print(f"# cache root: {cache_root}")
    print(f"# indices: {indices}")
    print(f"# sessions in range (weekdays): {len(sessions)} "
          f"(first={sessions[0]} last={sessions[-1]})", flush=True)

    # Resolve the slippage schedule: single value or sweep.
    if args.sweep_slip:
        try:
            slip_values = [int(x.strip()) for x in args.sweep_slip.split(",") if x.strip()]
        except ValueError:
            print(f"ERROR: --sweep-slip expected comma-separated ints, got {args.sweep_slip!r}")
            return 2
        if not slip_values:
            print("ERROR: --sweep-slip is empty")
            return 2
    else:
        slip_values = [int(args.leg_sl_slip_bps)]

    sweep_rows: list[tuple[int, list[CycleResult], ReplayStats]] = []

    trace_day: date | None = None
    if args.trace_day:
        try:
            trace_day = _parse_date(args.trace_day)
        except ValueError:
            print(f"ERROR: --trace-day expected YYYY-MM-DD, got {args.trace_day!r}")
            return 2

    # Only populated when trace_day is inside the replay range and slip
    # sweep is a single value (trace across a sweep is meaningless).
    trace_legs: list[LegTrade] = []

    for slip_bps in slip_values:
        stats = ReplayStats()
        run_cycles: list[CycleResult] = []
        print(f"\n# === run: leg_sl_slip_bps = {slip_bps} ===", flush=True)
        for i, session in enumerate(sessions, start=1):
            legs_sink = (
                trace_legs if (trace_day is not None and session == trace_day
                               and len(slip_values) == 1) else None
            )
            for underlying in indices:
                run_cycles.extend(
                    simulate_session(
                        underlying, session, store, args.fan, stats,
                        leg_sl_slip_bps=slip_bps,
                        disable_fixed_strike=args.disable_fixed_strike,
                        leg_trades_out=legs_sink,
                    )
                )
            if i % 50 == 0 or i == len(sessions):
                print(f"# progress [{slip_bps}bps]: {i}/{len(sessions)} sessions — "
                      f"cycles so far: {len(run_cycles)}", flush=True)

        if args.csv:
            csv_path = Path(args.csv).expanduser().resolve()
            if len(slip_values) > 1:
                csv_path = csv_path.with_name(
                    f"{csv_path.stem}.slip{slip_bps}bps{csv_path.suffix}"
                )
            _write_csv(run_cycles, csv_path)
            print(f"# wrote CSV: {csv_path} ({len(run_cycles)} rows)", flush=True)

        sweep_rows.append((slip_bps, run_cycles, stats))

    if len(slip_values) == 1:
        slip_bps, run_cycles, stats = sweep_rows[0]
        print(f"\n_(leg-SL slippage: {slip_bps} bps)_\n")
        _print_report(run_cycles, stats, args.from_date, args.to_date, indices)
    else:
        _print_sweep_report(sweep_rows, args.from_date, args.to_date, indices)

    if trace_day is not None and trace_legs:
        _print_trace(trace_legs, trace_day, indices)
        if args.trace_csv:
            trace_csv_path = Path(args.trace_csv).expanduser().resolve()
            _write_trace_csv(trace_legs, trace_csv_path)
            print(f"# wrote trace CSV: {trace_csv_path} ({len(trace_legs)} rows)")
    elif trace_day is not None:
        print(f"\n# --trace-day {trace_day}: no legs entered (session "
              f"skipped or out of range).")

    return 0


def _print_sweep_report(
    rows: list[tuple[int, list[CycleResult], ReplayStats]],
    start: date, end: date, indices: list[str],
) -> None:
    print("\n# Slippage sensitivity sweep\n")
    print(f"- Range: **{start}** .. **{end}**")
    print(f"- Indices: {', '.join(indices)}")
    print()

    print("## P&L vs leg-SL slippage (all indices combined)\n")
    print("| Slip (bps) | Cycles | Win % | Total P&L | Avg P&L | Best cycle | Worst cycle "
          "| TARGET % | LOCK_TRAIL % | MAX_LOSS % | CLOSE % |")
    print("|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for slip_bps, cycles, _stats in rows:
        s = _summarise(cycles)
        by_reason = _group(cycles, lambda c: c.exit_reason)
        n = max(1, len(cycles))
        pct_target = len(by_reason.get("MTM_TARGET", [])) / n * 100.0
        pct_lock = len(by_reason.get("LOCK_TRAIL", [])) / n * 100.0
        pct_max_loss = len(by_reason.get("MTM_MAX_LOSS", [])) / n * 100.0
        pct_close = len(by_reason.get("SESSION_CLOSE", [])) / n * 100.0
        print(f"| {slip_bps} | {s['n']} | {s['win_rate']:.1f}% "
              f"| {_fmt_inr(s['pnl'])} | {_fmt_inr(s['avg'])} "
              f"| {_fmt_inr(s['best'])} | {_fmt_inr(s['worst'])} "
              f"| {pct_target:.1f}% | {pct_lock:.1f}% | {pct_max_loss:.1f}% | {pct_close:.1f}% |")

    # Per-index breakdown
    print("\n## P&L vs slippage by index\n")
    print("| Slip (bps) | Index | Cycles | Win % | Total P&L | Avg P&L | Worst cycle |")
    print("|--:|:--|--:|--:|--:|--:|--:|")
    for slip_bps, cycles, _stats in rows:
        for idx in indices:
            sub = [c for c in cycles if c.underlying == idx]
            s = _summarise(sub)
            print(f"| {slip_bps} | {idx} | {s['n']} | {s['win_rate']:.1f}% "
                  f"| {_fmt_inr(s['pnl'])} | {_fmt_inr(s['avg'])} "
                  f"| {_fmt_inr(s['worst'])} |")


if __name__ == "__main__":
    import sys
    sys.exit(main())
