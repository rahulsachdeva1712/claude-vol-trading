"""Scoped backtest — last N trading days on real Dhan 1-minute data.

Mirrors the live strategy exactly:
  - Cycle opens at 09:30 IST (ATM computed from spot at that minute)
  - Base legs: ATM+6 CE (long), ATM-6 PE (long) -- both start WATCHING
  - Entry on 1-min option bar close if bar.return_pct >= 1.0 %
  - Leg SL: entry * 0.85 for base, entry * 0.88 for lazy (close-based)
  - Lazy leg on opposite side after a base leg stops, fresh ATM, slot 3/4
  - Cycle exits on: kill (N/A here) -> session close 15:15 -> max_loss -> target -> leg SL
  - Cooldown = 0; next cycle opens on the first eligible minute after close

Data source: Dhan /v2/charts/intraday (5-day windows).

Since the Dhan scrip master holds only currently-listed contracts, the
achievable window is roughly the lifetime of the current monthly expiry
(~30-60 sessions). Sessions where the required option security_id is
not resolvable are skipped with a note.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\backtest.py --days 30

Output is markdown: overall, by-month, by-week, by-day tables.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

DHAN_BASE = "https://api.dhan.co/v2"
IST = ZoneInfo("Asia/Kolkata")

# Strategy params (mirror configs/default.yaml).
MOMENTUM_THRESHOLD_PCT = 1.0
STRIKE_OFFSET_CE = 6
STRIKE_OFFSET_PE = -6
BASE_LEG_SL_PCT = 15.0
LAZY_LEG_SL_PCT = 12.0
LAZY_ENABLED = True
SESSION_START = time(9, 30)
SESSION_END = time(15, 15)

# Per-index config.
INDEX_SPECS = {
    "NIFTY":     {"security_id": 13, "strike_interval": 50,  "lot_size": 75, "max_loss": 2500, "target": 300},
    "BANKNIFTY": {"security_id": 25, "strike_interval": 100, "lot_size": 35, "max_loss": 2500, "target": 300},
}


# --------------------------------------------------------------------------
# Data types
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Bar:
    minute_epoch: int    # unix seconds, aligned to minute
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def return_pct(self) -> float:
        if self.open <= 0:
            return 0.0
        return (self.close - self.open) / self.open * 100.0


@dataclass(slots=True)
class Leg:
    slot: int
    kind: Literal["BASE", "LAZY"]
    option_type: Literal["CE", "PE"]
    strike: int
    security_id: int = 0
    lots: int = 1
    lot_size: int = 75
    status: Literal["WATCHING", "ACTIVE", "STOPPED", "EMPTY"] = "WATCHING"
    entry_price: float = 0.0
    sl_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    last_price: float = 0.0

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


@dataclass(slots=True)
class CycleResult:
    underlying: str
    session_date: date
    cycle_no: int
    started_at: datetime
    ended_at: datetime
    atm_at_start: int
    exit_reason: str
    cycle_pnl: float
    peak_mtm: float
    trough_mtm: float
    legs_entered: int


# --------------------------------------------------------------------------
# Dhan historical data
# --------------------------------------------------------------------------

async def fetch_intraday_bars(
    client: httpx.AsyncClient,
    security_id: int,
    segment: str,
    instrument: str,
    from_dt: date,
    to_dt: date,
) -> list[Bar]:
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": "1",
        "oi": False,
        "fromDate": datetime.combine(from_dt, time(9, 15)).strftime("%Y-%m-%d %H:%M:%S"),
        "toDate":   datetime.combine(to_dt,   time(15, 30)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    r = await client.post("/charts/intraday", json=payload, timeout=30.0)
    if r.status_code != 200:
        return []
    body = r.json() or {}
    ts = body.get("timestamp") or []
    op = body.get("open") or []
    hi = body.get("high") or []
    lo = body.get("low") or []
    cl = body.get("close") or []
    vl = body.get("volume") or [0] * len(ts)
    bars: list[Bar] = []
    for i, t in enumerate(ts):
        bars.append(Bar(
            minute_epoch=int(t),
            open=float(op[i]), high=float(hi[i]),
            low=float(lo[i]), close=float(cl[i]),
            volume=int(vl[i]) if i < len(vl) else 0,
        ))
    return bars


# --------------------------------------------------------------------------
# Scrip master loader
# --------------------------------------------------------------------------

def load_option_map(csv_path: Path) -> dict[tuple[str, str, int, str], int]:
    """(underlying, expiry_iso, strike, option_type) -> security_id"""
    out: dict[tuple[str, str, int, str], int] = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("INSTRUMENT", "").strip().upper() != "OPTIDX":
                continue
            u = row.get("UNDERLYING_SYMBOL", "").strip().upper()
            if u not in ("NIFTY", "BANKNIFTY"):
                continue
            try:
                strike = int(float(row.get("STRIKE_PRICE", 0) or 0))
                sec = int(row.get("SECURITY_ID", 0) or 0)
            except ValueError:
                continue
            expiry = (row.get("SM_EXPIRY_DATE", "") or "")[:10]
            opt_type = (row.get("OPTION_TYPE", "") or "").strip().upper()
            if not expiry or opt_type not in ("CE", "PE"):
                continue
            out[(u, expiry, strike, opt_type)] = sec
    return out


def nearest_monthly_expiry(
    option_map: dict[tuple[str, str, int, str], int], underlying: str, ref: date,
) -> str | None:
    """Pick the nearest future monthly expiry for `underlying` from map (vs ref date).

    Monthly = last expiry day in each month.
    """
    expiries = sorted({e for (u, e, _s, _t) in option_map if u == underlying})
    future = [e for e in expiries if e >= ref.isoformat()]
    if not future:
        return None
    # Group by YYYY-MM, pick last per month, then earliest of those.
    buckets: dict[str, list[str]] = {}
    for e in future:
        buckets.setdefault(e[:7], []).append(e)
    monthlies = sorted(max(v) for v in buckets.values())
    return monthlies[0] if monthlies else future[0]


def nearest_strike(available: list[int], spot: float, interval: int) -> int:
    if not available:
        return int((spot // interval) * interval)
    lower = max((s for s in available if s <= spot), default=available[0])
    higher = min((s for s in available if s > spot), default=lower)
    if abs(spot - lower) <= abs(higher - spot):
        return lower
    return higher


# --------------------------------------------------------------------------
# Strategy replay
# --------------------------------------------------------------------------

def minute_bar_at(bars: list[Bar], minute_epoch: int) -> Bar | None:
    # Bars assumed sorted by minute_epoch; linear scan is fine here.
    for b in bars:
        if b.minute_epoch == minute_epoch:
            return b
        if b.minute_epoch > minute_epoch:
            return None
    return None


def ist_minute_epochs(session: date) -> list[int]:
    """Every minute tick 09:30 -> 15:15 IST for the given date."""
    start = datetime.combine(session, SESSION_START, tzinfo=IST)
    end = datetime.combine(session, SESSION_END, tzinfo=IST)
    out = []
    t = start
    while t <= end:
        out.append(int(t.timestamp()))
        t += timedelta(minutes=1)
    return out


async def resolve_and_fetch_option(
    client: httpx.AsyncClient,
    option_map: dict[tuple[str, str, int, str], int],
    bar_cache: dict[tuple[int, date], list[Bar]],
    underlying: str, expiry: str, strike: int, opt_type: str,
    session: date,
) -> tuple[int | None, list[Bar]]:
    """Resolve (underlying, expiry, strike, opt_type) -> security_id and fetch 1-min bars for the session.
    Returns (security_id, bars). bars is [] if not resolvable or no data.
    """
    sec = option_map.get((underlying, expiry, strike, opt_type))
    if sec is None:
        return None, []
    cache_key = (sec, session)
    if cache_key in bar_cache:
        return sec, bar_cache[cache_key]
    bars = await fetch_intraday_bars(client, sec, "NSE_FNO", "OPTIDX", session, session)
    bar_cache[cache_key] = bars
    return sec, bars


async def simulate_day(
    client: httpx.AsyncClient,
    option_map: dict[tuple[str, str, int, str], int],
    bar_cache: dict[tuple[int, date], list[Bar]],
    underlying: str, session: date, index_bars: list[Bar],
) -> list[CycleResult]:
    """Replay the strategy for one (underlying, session)."""
    spec = INDEX_SPECS[underlying]
    interval = spec["strike_interval"]
    expiry = nearest_monthly_expiry(option_map, underlying, session)
    if expiry is None:
        return []

    # Strikes available for this expiry.
    strikes = sorted({s for (u, e, s, _t) in option_map if u == underlying and e == expiry})
    if not strikes:
        return []

    # Index LTP at every minute.
    spot_by_minute: dict[int, float] = {b.minute_epoch: b.close for b in index_bars}

    session_minutes = ist_minute_epochs(session)

    results: list[CycleResult] = []
    cycle_no = 0
    i = 0  # pointer into session_minutes

    # Helper: ensure we have option bars for a leg; returns (sec_id, bars).
    async def ensure_leg_data(strike: int, opt_type: str) -> tuple[int | None, list[Bar]]:
        return await resolve_and_fetch_option(
            client, option_map, bar_cache, underlying, expiry, strike, opt_type, session,
        )

    while i < len(session_minutes):
        m = session_minutes[i]
        spot = spot_by_minute.get(m, 0.0)
        if spot <= 0:
            i += 1
            continue
        # Cycle start
        cycle_no += 1
        atm = nearest_strike(strikes, spot, interval)
        ce_strike = atm + STRIKE_OFFSET_CE * interval
        pe_strike = atm + STRIKE_OFFSET_PE * interval

        _ce_sec, ce_bars = await ensure_leg_data(ce_strike, "CE")
        _pe_sec, pe_bars = await ensure_leg_data(pe_strike, "PE")
        # If neither strike resolves, advance to next minute and retry as new cycle next iter.
        if _ce_sec is None and _pe_sec is None:
            i += 1
            cycle_no -= 1
            continue

        legs: list[Leg] = []
        if _ce_sec is not None:
            legs.append(Leg(slot=1, kind="BASE", option_type="CE", strike=ce_strike,
                             security_id=_ce_sec, lots=1, lot_size=spec["lot_size"]))
        if _pe_sec is not None:
            legs.append(Leg(slot=2, kind="BASE", option_type="PE", strike=pe_strike,
                             security_id=_pe_sec, lots=1, lot_size=spec["lot_size"]))

        leg_bars: dict[int, list[Bar]] = {}
        if _ce_sec is not None:
            leg_bars[1] = ce_bars
        if _pe_sec is not None:
            leg_bars[2] = pe_bars

        peak_mtm = 0.0
        trough_mtm = 0.0
        cycle_exit_reason = ""
        cycle_started_m = m
        lazy_ce_done = False
        lazy_pe_done = False
        legs_entered = 0

        # Advance minute by minute until cycle exits.
        while i < len(session_minutes):
            m_now = session_minutes[i]
            spot_now = spot_by_minute.get(m_now, spot)
            if spot_now > 0:
                spot = spot_now

            # 1) Evaluate each leg's bar at this minute.
            for leg in legs:
                lb = leg_bars.get(leg.slot)
                if lb is None:
                    continue
                bar = minute_bar_at(lb, m_now)
                if bar is None or bar.close <= 0:
                    continue
                leg.last_price = bar.close
                if leg.status == "WATCHING":
                    if bar.return_pct >= MOMENTUM_THRESHOLD_PCT:
                        leg.entry_price = bar.close
                        sl_pct = BASE_LEG_SL_PCT if leg.kind == "BASE" else LAZY_LEG_SL_PCT
                        leg.sl_price = round(bar.close * (1 - sl_pct / 100.0), 2)
                        leg.status = "ACTIVE"
                        legs_entered += 1
                elif leg.status == "ACTIVE":
                    if bar.close <= leg.sl_price:
                        leg.exit_price = bar.close
                        leg.status = "STOPPED"
                        leg.exit_reason = "LEG_SL"
                        # Lazy-leg scheduling on opposite side.
                        if LAZY_ENABLED and leg.kind == "BASE":
                            if leg.option_type == "CE" and not lazy_pe_done:
                                lazy_pe_done = True
                                fresh_atm = nearest_strike(strikes, spot, interval)
                                lazy_strike = fresh_atm + STRIKE_OFFSET_PE * interval
                                ls_sec, ls_bars = await ensure_leg_data(lazy_strike, "PE")
                                if ls_sec is not None:
                                    new = Leg(slot=3, kind="LAZY", option_type="PE",
                                              strike=lazy_strike, security_id=ls_sec,
                                              lots=1, lot_size=spec["lot_size"])
                                    legs.append(new)
                                    leg_bars[3] = ls_bars
                            elif leg.option_type == "PE" and not lazy_ce_done:
                                lazy_ce_done = True
                                fresh_atm = nearest_strike(strikes, spot, interval)
                                lazy_strike = fresh_atm + STRIKE_OFFSET_CE * interval
                                ls_sec, ls_bars = await ensure_leg_data(lazy_strike, "CE")
                                if ls_sec is not None:
                                    new = Leg(slot=4, kind="LAZY", option_type="CE",
                                              strike=lazy_strike, security_id=ls_sec,
                                              lots=1, lot_size=spec["lot_size"])
                                    legs.append(new)
                                    leg_bars[4] = ls_bars

            # 2) Cycle MTM (realized + unrealized).
            mtm = sum(l.realized_pnl for l in legs) + sum(l.unrealized_pnl for l in legs)
            if mtm > peak_mtm:
                peak_mtm = mtm
            if mtm < trough_mtm:
                trough_mtm = mtm

            # 3) Exit checks — priority: session_close, max_loss, target.
            # Session-close handled below on loop exit.
            if mtm <= -abs(spec["max_loss"]):
                cycle_exit_reason = "MTM_MAX_LOSS"
                break
            if mtm >= spec["target"]:
                cycle_exit_reason = "MTM_TARGET"
                break

            # 4) Session close
            mt_ist = datetime.fromtimestamp(m_now, tz=IST).time()
            if mt_ist >= SESSION_END:
                cycle_exit_reason = "SESSION_CLOSE"
                break

            i += 1

        # Force-close any still-ACTIVE legs at their last_price.
        for leg in legs:
            if leg.status == "ACTIVE":
                leg.exit_price = leg.last_price or leg.entry_price
                leg.status = "STOPPED"
                leg.exit_reason = cycle_exit_reason or "SESSION_CLOSE"

        if not cycle_exit_reason:
            cycle_exit_reason = "SESSION_CLOSE"

        final_mtm = sum(l.realized_pnl for l in legs)
        started_dt = datetime.fromtimestamp(cycle_started_m, tz=IST)
        ended_dt = datetime.fromtimestamp(session_minutes[min(i, len(session_minutes)-1)], tz=IST)
        results.append(CycleResult(
            underlying=underlying, session_date=session, cycle_no=cycle_no,
            started_at=started_dt, ended_at=ended_dt, atm_at_start=atm,
            exit_reason=cycle_exit_reason, cycle_pnl=round(final_mtm, 2),
            peak_mtm=round(peak_mtm, 2), trough_mtm=round(trough_mtm, 2),
            legs_entered=legs_entered,
        ))

        # Next cycle on the next minute.
        i += 1

    return results


# --------------------------------------------------------------------------
# Aggregation + printing
# --------------------------------------------------------------------------

def fmt_inr(v: float) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}{v:,.2f}"


def summarise(cycles: list[CycleResult]) -> dict:
    if not cycles:
        return {"n": 0, "pnl": 0.0, "wins": 0, "losses": 0, "win_rate": 0.0, "avg": 0.0}
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
    }


def group_by(cycles: list[CycleResult], key):
    groups: dict = {}
    for c in cycles:
        k = key(c)
        groups.setdefault(k, []).append(c)
    return groups


def print_markdown(cycles: list[CycleResult], days_requested: int,
                   sessions_with_data: int, sessions_skipped: int) -> None:
    print("\n# Backtest report\n")
    print(f"- Requested window: last **{days_requested} days**")
    print(f"- Sessions with data: **{sessions_with_data}**")
    print(f"- Sessions skipped (no option data / holiday): **{sessions_skipped}**")
    print(f"- Total cycles simulated: **{len(cycles)}**\n")

    print("## Totals\n")
    print("| Scope | Cycles | P&L (Rs) | Wins | Losses | Win rate | Avg P&L |")
    print("|:--|--:|--:|--:|--:|--:|--:|")
    for scope in ("ALL", "NIFTY", "BANKNIFTY"):
        subset = cycles if scope == "ALL" else [c for c in cycles if c.underlying == scope]
        s = summarise(subset)
        print(f"| {scope} | {s['n']} | {fmt_inr(s['pnl'])} | {s['wins']} | {s['losses']} | "
              f"{s['win_rate']:.1f}% | {fmt_inr(s['avg'])} |")

    # By month
    print("\n## By month\n")
    print("| Month | Cycles | P&L (Rs) | Win rate | Avg P&L |")
    print("|:--|--:|--:|--:|--:|")
    for k in sorted(group_by(cycles, lambda c: c.session_date.strftime("%Y-%m"))):
        subset = [c for c in cycles if c.session_date.strftime("%Y-%m") == k]
        s = summarise(subset)
        print(f"| {k} | {s['n']} | {fmt_inr(s['pnl'])} | {s['win_rate']:.1f}% | {fmt_inr(s['avg'])} |")

    # By ISO-week
    print("\n## By week (ISO)\n")
    print("| ISO week | Cycles | P&L (Rs) | Win rate | Avg P&L |")
    print("|:--|--:|--:|--:|--:|")
    weeks = sorted(group_by(cycles, lambda c: c.session_date.strftime("%G-W%V")))
    for k in weeks:
        subset = [c for c in cycles if c.session_date.strftime("%G-W%V") == k]
        s = summarise(subset)
        print(f"| {k} | {s['n']} | {fmt_inr(s['pnl'])} | {s['win_rate']:.1f}% | {fmt_inr(s['avg'])} |")

    # By day
    print("\n## By day\n")
    print("| Date | Cycles | P&L (Rs) | Win rate | Avg P&L |")
    print("|:--|--:|--:|--:|--:|")
    for d in sorted(group_by(cycles, lambda c: c.session_date)):
        subset = [c for c in cycles if c.session_date == d]
        s = summarise(subset)
        print(f"| {d} | {s['n']} | {fmt_inr(s['pnl'])} | {s['win_rate']:.1f}% | {fmt_inr(s['avg'])} |")


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def prior_trading_days(n: int, ref: date | None = None) -> list[date]:
    """Return last N weekdays strictly before `ref` (exclusive). NSE holidays
    are skipped implicitly — Dhan will simply return empty bars and we skip."""
    ref = ref or date.today()
    days = []
    d = ref - timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--index", choices=["NIFTY", "BANKNIFTY", "both"], default="both")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    load_dotenv(repo_root / ".env")
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    token = os.getenv("DHAN_ACCESS_TOKEN", "")
    if not client_id or not token:
        print("ERROR: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set in .env")
        return

    data_dir = Path(os.getenv("VOLSCALP_DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = repo_root / data_dir
    cache_dir = data_dir / "instruments"
    candidates = sorted(cache_dir.glob("scrip_master_*.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        print(f"ERROR: no scrip master under {cache_dir}/. Run the app once to download it.")
        return
    print(f"# Loading scrip master: {candidates[0]}")
    option_map = load_option_map(candidates[0])
    print(f"# Option rows: {len(option_map)} "
          f"(NIFTY: {sum(1 for (u,*_) in option_map if u == 'NIFTY')}, "
          f"BANKNIFTY: {sum(1 for (u,*_) in option_map if u == 'BANKNIFTY')})")

    sessions = prior_trading_days(args.days)
    print(f"# Sessions to try: {sessions[0]} .. {sessions[-1]} ({len(sessions)} days)")

    indices = ["NIFTY", "BANKNIFTY"] if args.index == "both" else [args.index]

    headers = {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    all_cycles: list[CycleResult] = []
    skipped = 0
    sessions_with_data = 0
    bar_cache: dict[tuple[int, date], list[Bar]] = {}

    async with httpx.AsyncClient(base_url=DHAN_BASE, headers=headers) as client:
        for session in sessions:
            session_has_data = False
            for underlying in indices:
                spec = INDEX_SPECS[underlying]
                idx_bars = await fetch_intraday_bars(
                    client, spec["security_id"], "IDX_I", "INDEX", session, session,
                )
                await asyncio.sleep(0.15)
                if not idx_bars:
                    continue
                cycles = await simulate_day(
                    client, option_map, bar_cache, underlying, session, idx_bars,
                )
                if cycles:
                    all_cycles.extend(cycles)
                    session_has_data = True
                    print(f"#   {session} {underlying}: {len(cycles)} cycles, "
                          f"P&L {fmt_inr(sum(c.cycle_pnl for c in cycles))}")
            if session_has_data:
                sessions_with_data += 1
            else:
                skipped += 1
                print(f"#   {session}: skipped (no usable data)")

    print_markdown(all_cycles, args.days, sessions_with_data, skipped)


if __name__ == "__main__":
    asyncio.run(main())
