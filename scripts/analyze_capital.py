"""Peak concurrent capital analysis for volscalp.

For each (index, session) in the cached backtest window, re-simulates the
strategy, collects every leg's (entry_epoch, exit_epoch, entry_price,
quantity, underlying), and computes the **per-minute sum of long-option
debit across ALL concurrently-open legs in BOTH indices**. That sum is
the instantaneous capital blocked at the broker (long options are paid
up-front, no margining beyond the debit).

Reports:
  - Peak capital per session (combined NIFTY+BANKNIFTY)
  - Top-N peak-capital days with per-leg breakdown on the peak minute
  - Distribution (mean, p50, p75, p95, p99, max)
  - Recommended Dhan working capital (peak x buffer)

Run:
    python scripts/analyze_capital.py --from 2025-10-24 --to 2026-04-23

Reuses the simulate_session / cache machinery from backtest_2y.py so
strategy parity is guaranteed.
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Import the existing simulator — ensures strategy parity with the main
# backtest (MOMENTUM_THRESHOLD_PCT, STRIKE_OFFSET_*, SL, lazy rules, etc.).
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from backtest_2y import (  # noqa: E402
    INDEX_SPECS, CacheStore, LegTrade, ReplayStats, SessionRiskGate,
    simulate_session,
)

IST = ZoneInfo("Asia/Kolkata")


def _iter_sessions(start: date, end: date) -> list[date]:
    out = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    k = (len(vs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(vs) - 1)
    frac = k - lo
    return vs[lo] * (1 - frac) + vs[hi] * frac


def _fmt_inr(v: float) -> str:
    return f"Rs {v:,.0f}"


def _session_minute_range(legs: list[LegTrade]) -> tuple[int, int]:
    """Min entry_epoch and max exit_epoch across legs in the session."""
    entries = [int(lt.entry_time.timestamp()) for lt in legs]
    exits = [int(lt.exit_time.timestamp()) for lt in legs]
    return (min(entries), max(exits))


def analyze_session(
    session: date, indices: list[str], store: CacheStore, fan: int,
) -> tuple[float, int, list[LegTrade]]:
    """Simulate both indices for one session, return (peak_capital,
    peak_minute_epoch, legs_present_at_peak)."""
    all_legs: list[LegTrade] = []
    stats = ReplayStats()
    for underlying in indices:
        gate = SessionRiskGate(lock_trigger=None, trail_drop=None,
                               daily_loss_limit=None, halt_mode="soft")
        simulate_session(
            underlying, session, store, fan, stats,
            leg_sl_slip_bps=100,
            disable_fixed_strike=False,
            leg_trades_out=all_legs,
            gate=gate,
        )

    if not all_legs:
        return 0.0, 0, []

    # Walk every minute from earliest entry to latest exit; sum capital
    # across legs that are OPEN at that minute. Capital per leg =
    # entry_price * quantity (lots * lot_size). A leg is open on
    # [entry_epoch, exit_epoch). We use "entry_epoch inclusive,
    # exit_epoch exclusive" — the exit minute's bar is what closed the
    # leg, so the capital is released at the end of that minute. Using
    # <= instead of < shifts max by at most one minute and doesn't
    # change the headline.
    #
    # Quantity comes from INDEX_SPECS[underlying]['lot_size'] * 1 lot
    # (backtest default). The live config could scale this via
    # lots_per_trade; that multiplier scales the answer linearly.
    leg_caps: list[tuple[int, int, float, str]] = []  # (enter, exit, cap, idx)
    for lt in all_legs:
        lot_size = INDEX_SPECS[lt.underlying]["lot_size"]
        quantity = lot_size  # 1 lot baseline
        cap = lt.entry_price * quantity
        leg_caps.append((
            int(lt.entry_time.timestamp()),
            int(lt.exit_time.timestamp()),
            cap,
            lt.underlying,
        ))

    t_start, t_end = _session_minute_range(all_legs)

    peak_cap = 0.0
    peak_min = t_start
    t = t_start
    while t <= t_end:
        cap_now = sum(c for (e, x, c, _u) in leg_caps if e <= t < x)
        if cap_now > peak_cap:
            peak_cap = cap_now
            peak_min = t
        t += 60

    return peak_cap, peak_min, all_legs


def _legs_open_at(legs: list[LegTrade], epoch: int) -> list[LegTrade]:
    out = []
    for lt in legs:
        e = int(lt.entry_time.timestamp())
        x = int(lt.exit_time.timestamp())
        if e <= epoch < x:
            out.append(lt)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    ap.add_argument("--to",   dest="to_date",   type=_parse_date, required=True)
    ap.add_argument("--fan", type=int, default=6)
    ap.add_argument("--data-fan", type=int, default=6)
    ap.add_argument("--cache-root", default=None)
    ap.add_argument("--lots", type=int, default=1,
                    help="Scale capital linearly by this many lots/trade. "
                         "Backtest baseline is 1; set to your intended "
                         "per-trade lots to see total capital needed.")
    ap.add_argument("--top", type=int, default=15,
                    help="Top-N peak days to break out (default 15).")
    args = ap.parse_args()

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
        return 2

    indices = ["NIFTY", "BANKNIFTY"]
    store = CacheStore(cache_root, data_fan=args.data_fan)
    sessions = _iter_sessions(args.from_date, args.to_date)

    print(f"# cache root: {cache_root}")
    print(f"# sessions in range: {len(sessions)} "
          f"(first={sessions[0]} last={sessions[-1]})")
    print(f"# lots/trade: {args.lots}")
    print(f"# indices: {indices}\n", flush=True)

    results: list[tuple[date, float, int, list[LegTrade]]] = []
    skipped = 0
    for i, session in enumerate(sessions, start=1):
        peak, peak_min, legs = analyze_session(session, indices, store, args.fan)
        if not legs:
            skipped += 1
            continue
        results.append((session, peak * args.lots, peak_min, legs))
        if i % 25 == 0 or i == len(sessions):
            print(f"# progress {i}/{len(sessions)} sessions "
                  f"(peak so far: {_fmt_inr(max((r[1] for r in results), default=0))})",
                  flush=True)

    if not results:
        print("No sessions produced any legs. Check cache coverage.")
        return 0

    peaks = [p for (_d, p, _m, _legs) in results]
    n = len(peaks)
    print("\n# === Peak concurrent capital (long-option debit) ===\n")
    print(f"- Sessions with trades: **{n}**")
    print(f"- Sessions skipped (no legs entered): **{skipped}**")
    print(f"- Lots per trade scaled by: **{args.lots}**\n")

    print("## Distribution\n")
    print("| Stat | Value |")
    print("|:--|--:|")
    print(f"| Mean | {_fmt_inr(sum(peaks) / n)} |")
    print(f"| Median (p50) | {_fmt_inr(_percentile(peaks, 50))} |")
    print(f"| p75 | {_fmt_inr(_percentile(peaks, 75))} |")
    print(f"| p90 | {_fmt_inr(_percentile(peaks, 90))} |")
    print(f"| p95 | {_fmt_inr(_percentile(peaks, 95))} |")
    print(f"| p99 | {_fmt_inr(_percentile(peaks, 99))} |")
    print(f"| **Max** | **{_fmt_inr(max(peaks))}** |")
    print()

    print(f"## Top {args.top} peak-capital days\n")
    print("| Rank | Date | Peak capital | Peak minute (IST) "
          "| Legs open @ peak | Per-leg breakdown |")
    print("|--:|:--|--:|:--|--:|:--|")
    top_sorted = sorted(results, key=lambda r: r[1], reverse=True)[: args.top]
    for rank, (d, peak, peak_min, legs) in enumerate(top_sorted, start=1):
        peak_time = datetime.fromtimestamp(peak_min, tz=IST).strftime("%H:%M")
        open_legs = _legs_open_at(legs, peak_min)
        per_leg = "; ".join(
            f"{lt.underlying[:4]}.{lt.option_type} "
            f"K={int(lt.entry_strike)} "
            f"px={lt.entry_price:.1f}"
            for lt in open_legs
        )
        print(
            f"| {rank} | {d.isoformat()} | {_fmt_inr(peak)} | {peak_time} "
            f"| {len(open_legs)} | {per_leg} |"
        )

    # By month — what's the monthly max?
    print("\n## Monthly max peak\n")
    print("| Month | Sessions | Max peak capital | Avg peak capital |")
    print("|:--|--:|--:|--:|")
    by_month: dict[str, list[float]] = defaultdict(list)
    for d, peak, _m, _legs in results:
        by_month[d.strftime("%Y-%m")].append(peak)
    for k in sorted(by_month):
        vs = by_month[k]
        print(f"| {k} | {len(vs)} | {_fmt_inr(max(vs))} "
              f"| {_fmt_inr(sum(vs)/len(vs))} |")

    # Recommendations
    max_peak = max(peaks)
    p99 = _percentile(peaks, 99)
    print("\n## Capital sizing recommendation\n")
    print(f"- Historical **max** peak (over {n} trading days): {_fmt_inr(max_peak)}")
    print(f"- 99th percentile: {_fmt_inr(p99)}")
    print(f"- Suggested baseline working capital in Dhan "
          f"(**max x 1.5 buffer**): **{_fmt_inr(max_peak * 1.5)}**")
    print(f"- Aggressive (max x 1.2): {_fmt_inr(max_peak * 1.2)}")
    print(f"- Conservative (max x 2.0): {_fmt_inr(max_peak * 2.0)}")
    print()
    print("Notes:")
    print(f"- Numbers are for **{args.lots} lot(s) per trade**. Multiply "
          "linearly if you run more lots.")
    print("- Only long-option debit is counted. No SPAN/ELM margin for "
          "shorts (this strategy doesn't sell).")
    print("- Capital is the minute-by-minute sum of (entry_price x "
          "lot_size) across all simultaneously-open legs in both indices.")
    print("- Peak days are rare tail events; the median tells you what "
          "you'll actually use most days.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
