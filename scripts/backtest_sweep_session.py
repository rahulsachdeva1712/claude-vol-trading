"""Cartesian-product sweep over session-level lock-and-trail configs.

Reuses ``run_replay`` / ``SessionRiskGate`` from ``backtest_2y.py`` so the
strategy code stays single-source. Each config is scored on:

  - total P&L
  - cycle-level win rate
  - session-day win rate (% of (index, session_date) pairs with net positive P&L)
  - stdev of daily P&L
  - best / worst daily P&L
  - month-win-rate and ISO-week-win-rate (% of buckets net positive)

Emits a CSV leaderboard and a markdown summary. Each config is a row in
the CSV; the ``composite`` column is:

    total_pnl * session_day_win_rate / (1 + |stdev_daily_pnl / mean_daily_pnl|)

which rewards total P&L and consistency equally (the ratio blows up when
mean_daily ~ 0, hence abs+1 floor). Ties are broken by total_pnl.

Usage::

    .venv\\Scripts\\python.exe scripts\\backtest_sweep_session.py \\
        --from 2025-10-24 --to 2026-04-23 \\
        --out out/sweep_session.csv --md out/sweep_session.md
"""
from __future__ import annotations

import argparse
import csv
import itertools
import math
import os
import sys
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

# Reuse strategy + reporting code from backtest_2y.
from backtest_2y import (  # noqa: E402
    CacheStore, ReplayStats, run_replay, _iter_sessions, _fmt_inr,
    INDEX_SPECS,
)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daily_pnl(cycles) -> dict[tuple[str, date], float]:
    out: dict[tuple[str, date], float] = {}
    for c in cycles:
        k = (c.underlying, c.session_date)
        out[k] = out.get(k, 0.0) + c.cycle_pnl
    return out


def _stdev(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return var ** 0.5


def _compute_metrics(cycles: list) -> dict:
    if not cycles:
        return {
            "cycles": 0, "total_pnl": 0.0, "win_rate": 0.0,
            "session_day_win_rate": 0.0, "stdev_daily": 0.0,
            "mean_daily": 0.0, "best_day": 0.0, "worst_day": 0.0,
            "month_win_rate": 0.0, "week_win_rate": 0.0, "composite": 0.0,
        }
    total_pnl = sum(c.cycle_pnl for c in cycles)
    wins = sum(1 for c in cycles if c.cycle_pnl > 0)
    win_rate = wins / len(cycles) * 100.0

    daily = _daily_pnl(cycles)
    day_pnls = list(daily.values())
    day_pos = sum(1 for v in day_pnls if v > 0)
    session_day_win_rate = (day_pos / len(day_pnls) * 100.0) if day_pnls else 0.0

    stdev_daily = _stdev(day_pnls)
    mean_daily = (sum(day_pnls) / len(day_pnls)) if day_pnls else 0.0
    best_day = max(day_pnls) if day_pnls else 0.0
    worst_day = min(day_pnls) if day_pnls else 0.0

    # Month / ISO-week buckets over the COMBINED (both indices) daily P&L
    # — that's "what the book did per bucket", matching how the user
    # thinks about consistency across time.
    combined_daily: dict[date, float] = {}
    for (idx, d), p in daily.items():
        combined_daily[d] = combined_daily.get(d, 0.0) + p

    months: dict[str, float] = {}
    weeks: dict[str, float] = {}
    for d, p in combined_daily.items():
        months[d.strftime("%Y-%m")] = months.get(d.strftime("%Y-%m"), 0.0) + p
        weeks[d.strftime("%G-W%V")] = weeks.get(d.strftime("%G-W%V"), 0.0) + p
    month_win_rate = (sum(1 for v in months.values() if v > 0) / len(months) * 100.0) if months else 0.0
    week_win_rate = (sum(1 for v in weeks.values() if v > 0) / len(weeks) * 100.0) if weeks else 0.0

    # Composite: reward total P&L and session-day win rate; penalise
    # high CV (stdev / |mean|). Fallback: if mean_daily is ~0 we cap the
    # denominator at 1 so a degenerate run doesn't dominate.
    if abs(mean_daily) > 1e-6:
        cv = abs(stdev_daily / mean_daily)
    else:
        cv = 0.0
    composite = total_pnl * (session_day_win_rate / 100.0) / (1.0 + cv)

    return {
        "cycles": len(cycles), "total_pnl": total_pnl, "win_rate": win_rate,
        "session_day_win_rate": session_day_win_rate,
        "stdev_daily": stdev_daily, "mean_daily": mean_daily,
        "best_day": best_day, "worst_day": worst_day,
        "month_win_rate": month_win_rate, "week_win_rate": week_win_rate,
        "composite": composite,
    }


def _cfg_label(cfg: dict) -> str:
    lt = cfg["lock_trigger"]
    td = cfg["trail_drop"]
    dl = cfg["daily_loss_limit"]
    hm = cfg["halt_mode"]
    def fmt(v):
        return "off" if v is None else f"{int(v)}"
    return f"lt={fmt(lt)} td={fmt(td)} dll={fmt(dl)} hm={hm}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    ap.add_argument("--to",   dest="to_date",   type=_parse_date, required=True)
    ap.add_argument("--index", default="both",
                    choices=["NIFTY", "BANKNIFTY", "both"])
    ap.add_argument("--cache-root", default=None)
    ap.add_argument("--fan", type=int, default=6)
    ap.add_argument("--data-fan", type=int, default=6)
    ap.add_argument("--leg-sl-slip-bps", type=int, default=100)
    # Grid overrides — each is a comma-separated list. "off" = None.
    ap.add_argument("--lock-triggers", default="off,1500,2500,4000,6000,10000")
    ap.add_argument("--trail-drops",  default="off,1000,2000,3500")
    ap.add_argument("--loss-limits",  default="off,3000,5000")
    ap.add_argument("--halt-modes",   default="soft")
    ap.add_argument("--out", default="out/sweep_session.csv")
    ap.add_argument("--md",  default="out/sweep_session.md")
    ap.add_argument("--top", type=int, default=5,
                    help="Top-N configs to expand in the markdown report.")
    args = ap.parse_args()

    if args.from_date > args.to_date:
        print("ERROR: --from must be <= --to")
        return 2

    def parse_list(s: str) -> list:
        out = []
        for tok in s.split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.lower() in ("off", "none", "disabled"):
                out.append(None)
            else:
                out.append(float(tok))
        return out

    lock_triggers = parse_list(args.lock_triggers)
    trail_drops = parse_list(args.trail_drops)
    loss_limits = parse_list(args.loss_limits)
    halt_modes = [m.strip() for m in args.halt_modes.split(",") if m.strip()]

    # Cartesian product, but prune impossible combos: if lock_trigger
    # is None, trail_drop has no meaning → collapse to None.
    raw_configs = itertools.product(lock_triggers, trail_drops, loss_limits, halt_modes)
    seen = set()
    configs: list[dict] = []
    for (lt, td, dl, hm) in raw_configs:
        if lt is None:
            td = None  # Can't trail without a trigger.
        # Also: if nothing is configured, only include baseline once.
        key = (lt, td, dl, hm if (lt is not None or dl is not None) else "soft")
        if key in seen:
            continue
        seen.add(key)
        configs.append({
            "lock_trigger": lt, "trail_drop": td,
            "daily_loss_limit": dl, "halt_mode": hm,
        })

    if args.cache_root:
        cache_root = Path(args.cache_root).expanduser().resolve()
    else:
        data_dir = Path(os.getenv("VOLSCALP_DATA_DIR", "") or (REPO / "data"))
        if not data_dir.is_absolute():
            data_dir = REPO / data_dir
        cache_root = data_dir / "backtest_cache" / "rollingoption"
    if not cache_root.is_dir():
        print(f"ERROR: cache root not found: {cache_root}")
        return 2

    indices = ["NIFTY", "BANKNIFTY"] if args.index == "both" else [args.index]
    sessions = _iter_sessions(args.from_date, args.to_date)
    store = CacheStore(cache_root, data_fan=args.data_fan)

    print(f"# cache root: {cache_root}")
    print(f"# indices: {indices}")
    print(f"# sessions (weekdays): {len(sessions)} "
          f"(first={sessions[0]} last={sessions[-1]})")
    print(f"# configs to run: {len(configs)}", flush=True)

    rows: list[tuple[dict, dict, list]] = []
    for i, cfg in enumerate(configs, start=1):
        print(f"\n# [{i}/{len(configs)}] {_cfg_label(cfg)}", flush=True)
        stats = ReplayStats()
        cycles = run_replay(
            sessions=sessions, indices=indices, store=store, fan=args.fan,
            leg_sl_slip_bps=args.leg_sl_slip_bps,
            session_cfg=cfg, stats=stats,
            progress_label=f"cfg{i}",
        )
        metrics = _compute_metrics(cycles)
        print(f"  -> cycles={metrics['cycles']} pnl={_fmt_inr(metrics['total_pnl'])} "
              f"win%={metrics['win_rate']:.1f} sday%={metrics['session_day_win_rate']:.1f} "
              f"stdev={_fmt_inr(metrics['stdev_daily'])} composite={metrics['composite']:,.0f}",
              flush=True)
        rows.append((cfg, metrics, cycles))

    # Rank by composite desc, then total_pnl desc.
    rows.sort(key=lambda r: (r[1]["composite"], r[1]["total_pnl"]), reverse=True)

    # CSV.
    out_csv = Path(args.out).expanduser().resolve()
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "rank", "lock_trigger", "trail_drop", "daily_loss_limit", "halt_mode",
            "cycles", "total_pnl", "win_rate_pct",
            "session_day_win_rate_pct", "mean_daily_pnl", "stdev_daily_pnl",
            "best_day", "worst_day", "month_win_rate_pct",
            "week_win_rate_pct", "composite",
        ])
        for rank, (cfg, m, _cyc) in enumerate(rows, start=1):
            w.writerow([
                rank,
                "" if cfg["lock_trigger"] is None else f"{int(cfg['lock_trigger'])}",
                "" if cfg["trail_drop"] is None else f"{int(cfg['trail_drop'])}",
                "" if cfg["daily_loss_limit"] is None else f"{int(cfg['daily_loss_limit'])}",
                cfg["halt_mode"],
                m["cycles"], f"{m['total_pnl']:.2f}", f"{m['win_rate']:.2f}",
                f"{m['session_day_win_rate']:.2f}",
                f"{m['mean_daily']:.2f}", f"{m['stdev_daily']:.2f}",
                f"{m['best_day']:.2f}", f"{m['worst_day']:.2f}",
                f"{m['month_win_rate']:.2f}", f"{m['week_win_rate']:.2f}",
                f"{m['composite']:.2f}",
            ])
    print(f"\n# wrote CSV: {out_csv}", flush=True)

    # Markdown.
    out_md = Path(args.md).expanduser().resolve()
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Session lock-and-trail sweep")
    lines.append("")
    lines.append(f"- Range: **{args.from_date}** .. **{args.to_date}**")
    lines.append(f"- Indices: {', '.join(indices)}")
    lines.append(f"- Sessions (weekdays): {len(sessions)}")
    lines.append(f"- Leg-SL slippage: {args.leg_sl_slip_bps} bps")
    lines.append(f"- Grid: lock={lock_triggers}  trail={trail_drops}  "
                 f"loss={loss_limits}  mode={halt_modes}")
    lines.append(f"- Configs tested: **{len(rows)}**")
    lines.append("")
    lines.append("## Leaderboard (ranked by composite)")
    lines.append("")
    lines.append("| Rank | Cfg | Cyc | Total P&L | Win% | SDay% | Mean/day | Stdev/day | "
                 "Best day | Worst day | Month-win% | Week-win% | Composite |")
    lines.append("|---:|:--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for rank, (cfg, m, _cyc) in enumerate(rows, start=1):
        lines.append(
            f"| {rank} | {_cfg_label(cfg)} | {m['cycles']} | {_fmt_inr(m['total_pnl'])} "
            f"| {m['win_rate']:.1f}% | {m['session_day_win_rate']:.1f}% "
            f"| {_fmt_inr(m['mean_daily'])} | {_fmt_inr(m['stdev_daily'])} "
            f"| {_fmt_inr(m['best_day'])} | {_fmt_inr(m['worst_day'])} "
            f"| {m['month_win_rate']:.1f}% | {m['week_win_rate']:.1f}% "
            f"| {m['composite']:,.0f} |"
        )
    lines.append("")

    # Top-N expansions.
    lines.append(f"## Top {args.top} configs — full breakdown")
    lines.append("")
    for rank, (cfg, m, cyc) in enumerate(rows[: args.top], start=1):
        lines.append(f"### #{rank} — {_cfg_label(cfg)}")
        lines.append("")
        lines.append(f"- Total P&L: **{_fmt_inr(m['total_pnl'])}**")
        lines.append(f"- Cycles: **{m['cycles']}**")
        lines.append(f"- Win rate (cycle): {m['win_rate']:.1f}%")
        lines.append(f"- Session-day win rate: {m['session_day_win_rate']:.1f}%")
        lines.append(f"- Mean / Stdev daily: "
                     f"{_fmt_inr(m['mean_daily'])} / {_fmt_inr(m['stdev_daily'])}")
        lines.append(f"- Best / Worst day: "
                     f"{_fmt_inr(m['best_day'])} / {_fmt_inr(m['worst_day'])}")
        lines.append(f"- Month-win-rate: {m['month_win_rate']:.1f}%  "
                     f"Week-win-rate: {m['week_win_rate']:.1f}%")
        lines.append(f"- Composite: **{m['composite']:,.0f}**")
        lines.append("")

        # Monthly breakdown of this config.
        daily = _daily_pnl(cyc)
        combined: dict[date, float] = {}
        for (idx, d), p in daily.items():
            combined[d] = combined.get(d, 0.0) + p
        months: dict[str, tuple[int, int, float]] = {}
        for d, p in combined.items():
            ym = d.strftime("%Y-%m")
            wins, count, pnl = months.get(ym, (0, 0, 0.0))
            count += 1
            pnl += p
            if p > 0:
                wins += 1
            months[ym] = (wins, count, pnl)
        lines.append("| Month | Days | Pos days | Month P&L |")
        lines.append("|:--|--:|--:|--:|")
        for ym in sorted(months):
            wins, count, pnl = months[ym]
            lines.append(f"| {ym} | {count} | {wins} | {_fmt_inr(pnl)} |")
        lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"# wrote markdown: {out_md}", flush=True)

    # Also print top-5 to stdout.
    print("\n# Top 5 configs:")
    for rank, (cfg, m, _cyc) in enumerate(rows[:5], start=1):
        print(f"  {rank}. {_cfg_label(cfg):<55}  "
              f"pnl={_fmt_inr(m['total_pnl'])}  "
              f"sday%={m['session_day_win_rate']:.1f}  "
              f"stdev={_fmt_inr(m['stdev_daily'])}  "
              f"composite={m['composite']:,.0f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
