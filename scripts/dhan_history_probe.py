"""Probe Dhan v2 historical 1-minute intraday API for coverage depth.

Tells us, in ~30 seconds:
  - How far back Dhan returns 1-min bars for the index (NIFTY / BANKNIFTY)
  - How far back Dhan returns 1-min bars for an actual ATM-ish option contract
  - The exact error code when a window is unavailable

Reads DHAN_CLIENT_ID + DHAN_ACCESS_TOKEN from .env. Uses the cached
scrip-master CSV under data/instruments/ to pick a current monthly ATM
NIFTY CE and BANKNIFTY CE as the option probe targets.

Run:
    .\\.venv\\Scripts\\python.exe scripts\\dhan_history_probe.py

Paste the printed table back so we can decide what backtest window is
actually achievable.
"""
from __future__ import annotations

import asyncio
import csv
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

DHAN_BASE = "https://api.dhan.co/v2"


def _resolve_env_path(repo_root: Path) -> Path:
    """Resolve which .env file to load. Priority:
      1. $VOLSCALP_ENV_FILE (absolute or CWD-relative) if set & exists
      2. ~/Documents/shared/.env (shared across projects, portable via Path.home())
      3. <repo_root>/.env (legacy fallback)
    """
    override = os.getenv("VOLSCALP_ENV_FILE", "").strip()
    if override:
        p = Path(override).expanduser()
        if p.is_file():
            return p
    shared = Path.home() / "Documents" / "shared" / ".env"
    if shared.is_file():
        return shared
    return repo_root / ".env"


# Approximate days-back targets the strategy backtest cares about.
LOOKBACKS = [
    ("T-1d",     1),
    ("T-1w",     7),
    ("T-1m",    30),
    ("T-3m",    90),
    ("T-6m",   180),
    ("T-12m",  365),
    ("T-24m",  730),
]


def _snap_to_weekday(d: date) -> date:
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


def _find_atm_ce(csv_path: Path, underlying: str) -> tuple[int, str, str] | None:
    """Pick the median-strike CE of the nearest future monthly expiry."""
    today_iso = date.today().isoformat()
    rows: list[dict] = []
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("INSTRUMENT", "").strip().upper() == "OPTIDX"
                and row.get("UNDERLYING_SYMBOL", "").strip().upper() == underlying
                and row.get("OPTION_TYPE", "").strip().upper() == "CE"):
                rows.append(row)
    if not rows:
        return None
    expiries = sorted({r.get("SM_EXPIRY_DATE", "")[:10] for r in rows
                       if r.get("SM_EXPIRY_DATE", "")[:10] >= today_iso})
    if not expiries:
        return None
    expiry = expiries[0]
    same = [r for r in rows if r.get("SM_EXPIRY_DATE", "").startswith(expiry)]
    same.sort(key=lambda r: int(float(r.get("STRIKE_PRICE", 0) or 0)))
    if not same:
        return None
    pick = same[len(same) // 2]
    return int(pick["SECURITY_ID"]), pick.get("DISPLAY_NAME", ""), expiry


async def _probe(client: httpx.AsyncClient, sec_id: int, segment: str,
                 instrument: str, from_dt: date, to_dt: date) -> dict:
    """One historical-intraday call. Returns dict with status + bars/error."""
    payload = {
        "securityId": str(sec_id),
        "exchangeSegment": segment,
        "instrument": instrument,
        "interval": "1",
        "oi": False,
        "fromDate": datetime.combine(from_dt, time(9, 15)).strftime("%Y-%m-%d %H:%M:%S"),
        "toDate":   datetime.combine(to_dt,   time(15, 30)).strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        r = await client.post("/charts/intraday", json=payload, timeout=30.0)
        if r.status_code != 200:
            return {"status": r.status_code, "info": r.text[:120].replace("\n", " ")}
        body = r.json() or {}
        ts = body.get("timestamp") or []
        return {"status": 200, "info": f"{len(ts)} bars"}
    except Exception as e:  # noqa: BLE001
        return {"status": "EXC", "info": f"{type(e).__name__}: {str(e)[:80]}"}


async def main() -> None:
    # Resolve paths from the script's own location so CWD doesn't matter.
    repo_root = Path(__file__).resolve().parent.parent
    env_path = _resolve_env_path(repo_root)
    load_dotenv(env_path)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    token = os.getenv("DHAN_ACCESS_TOKEN", "")
    if not client_id or not token:
        print(f"ERROR: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set in {env_path}")
        return

    # Locate cached scrip master (today, then walk back a few days).
    data_dir = Path(os.getenv("VOLSCALP_DATA_DIR", "./data"))
    if not data_dir.is_absolute():
        data_dir = repo_root / data_dir
    cache_dir = data_dir / "instruments"
    csv_path: Path | None = None
    for back in range(0, 7):
        cand = cache_dir / f"scrip_master_{(date.today() - timedelta(days=back)).isoformat()}.csv"
        if cand.exists():
            csv_path = cand
            break
    if csv_path is None:
        # Fall back to any scrip_master_*.csv in the directory (most recent).
        if cache_dir.exists():
            candidates = sorted(cache_dir.glob("scrip_master_*.csv"),
                                key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                csv_path = candidates[0]
    if csv_path is None:
        print(f"ERROR: no scrip master under {cache_dir}/. Start the app once to download it.")
        return
    print(f"Scrip master: {csv_path}")

    nifty = _find_atm_ce(csv_path, "NIFTY")
    bank  = _find_atm_ce(csv_path, "BANKNIFTY")
    if not nifty or not bank:
        print("ERROR: could not locate current monthly ATM CE for both indices in the master.")
        return

    print(f"NIFTY     probe: sec={nifty[0]} sym={nifty[1]} expiry={nifty[2]}")
    print(f"BANKNIFTY probe: sec={bank[0]}  sym={bank[1]}  expiry={bank[2]}")

    targets = [
        ("NIFTY index",          13,       "IDX_I",   "INDEX"),
        ("BANKNIFTY index",      25,       "IDX_I",   "INDEX"),
        (f"NIFTY opt {nifty[1]}",     nifty[0], "NSE_FNO", "OPTIDX"),
        (f"BANKNIFTY opt {bank[1]}",  bank[0],  "NSE_FNO", "OPTIDX"),
    ]

    headers = {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    print()
    print(f"{'TARGET':<46} {'LOOKBACK':<8} {'FROM':<12} {'TO':<12} {'STATUS':<7} INFO")
    print("-" * 140)
    async with httpx.AsyncClient(base_url=DHAN_BASE, headers=headers) as client:
        for name, sec, seg, inst in targets:
            for label, days_back in LOOKBACKS:
                center = _snap_to_weekday(date.today() - timedelta(days=days_back))
                from_dt = center - timedelta(days=4)
                to_dt = center
                res = await _probe(client, sec, seg, inst, from_dt, to_dt)
                print(f"{name[:45]:<46} {label:<8} {from_dt}   {to_dt}   {str(res['status']):<7} {res['info']}")
                await asyncio.sleep(0.4)  # gentle on rate limits


if __name__ == "__main__":
    asyncio.run(main())
