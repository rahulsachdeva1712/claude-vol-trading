"""Probe #2 — does the cached scrip master include OLD expiries?

If yes: we can resolve historical security_ids locally, no extra API
lookups needed, and the 2-year backtest harness is viable.

If no: we'll need another path to map (expiry, strike, option_type) to
security_id for expired contracts.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\dhan_master_coverage.py
"""
from __future__ import annotations

import csv
from collections import Counter
from datetime import date, timedelta
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent
    cache_dir = repo_root / "data" / "instruments"
    candidates = sorted(cache_dir.glob("scrip_master_*.csv"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        print(f"ERROR: no scrip master under {cache_dir}/. Run the app once.")
        return
    csv_path = candidates[0]
    print(f"Scrip master: {csv_path}\n")

    # Count OPTIDX rows by (underlying, YYYY-MM of expiry).
    by_month_nifty: Counter = Counter()
    by_month_bank: Counter = Counter()
    seen_underlyings: Counter = Counter()
    total_optidx = 0
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("INSTRUMENT", "").strip().upper() != "OPTIDX":
                continue
            total_optidx += 1
            u = row.get("UNDERLYING_SYMBOL", "").strip().upper()
            exp = row.get("SM_EXPIRY_DATE", "")[:7]   # YYYY-MM
            seen_underlyings[u] += 1
            if u == "NIFTY":
                by_month_nifty[exp] += 1
            elif u == "BANKNIFTY":
                by_month_bank[exp] += 1

    print(f"Total OPTIDX rows: {total_optidx}")
    print(f"Underlyings seen:  {dict(seen_underlyings.most_common(6))}\n")

    print("NIFTY OPTIDX count by expiry month:")
    for m in sorted(by_month_nifty):
        print(f"  {m}   {by_month_nifty[m]:>6}")
    print()
    print("BANKNIFTY OPTIDX count by expiry month:")
    for m in sorted(by_month_bank):
        print(f"  {m}   {by_month_bank[m]:>6}")

    # Highlight whether 2024 / early-2025 expiries are present.
    today = date.today()
    cutoffs = {
        "T-24m": (today - timedelta(days=730)).strftime("%Y-%m"),
        "T-18m": (today - timedelta(days=545)).strftime("%Y-%m"),
        "T-12m": (today - timedelta(days=365)).strftime("%Y-%m"),
        "T-6m":  (today - timedelta(days=180)).strftime("%Y-%m"),
    }
    print("\nAt-a-glance — rows for NIFTY expiries at historical months:")
    for k, m in cutoffs.items():
        print(f"  {k} ({m})  NIFTY={by_month_nifty.get(m, 0)}  BANKNIFTY={by_month_bank.get(m, 0)}")


if __name__ == "__main__":
    main()
