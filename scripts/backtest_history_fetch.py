"""Populate the rollingoption cache consumed by scripts/backtest_2y.py.

Calls Dhan's ``/v2/charts/rollingoption`` endpoint (Expired Options Data
add-on) once per (index, month, option_type, signed_offset) and writes
the JSON response verbatim to::

    data/backtest_cache/rollingoption/{INDEX}/{YYYY-MM}/{CE|PE}_ATM{signed_offset}.json

Idempotent: if a file already exists on disk it is skipped — reruns only
hit the API for the gaps. Matches the shape expected by
``MonthCache.load()`` in backtest_2y.py: each file contains the raw
Dhan payload with top-level ``data.ce`` or ``data.pe`` containing
parallel arrays (timestamp/open/high/low/close/volume/oi/spot/strike).

Dhan endpoint quirks (discovered by probing 2026-04):
  - ``expiryCode`` must be ``1`` (current monthly expiry as of the
    fromDate window). ``0`` returns DH-905 "expiryCode is required".
  - ``fromDate`` / ``toDate`` are **date-only** ("YYYY-MM-DD"). Passing
    a "YYYY-MM-DD HH:MM:SS" value errors with DH-905. We clamp each
    request to the intersection of the caller's window with that
    month's [1st, last] so we respect the --from/--to range.
  - ``strike`` is either "ATM", "ATM+N", "ATM-N" (string) or a bare
    numeric ``N`` (interpreted as the CE/PE-specific offset). We use
    the string form for clarity.
  - ``drvOptionType`` is "CALL" / "PUT". Response is always
    ``{data: {ce: {...}, pe: {...}}}`` — only the requested side is
    populated, the other side's arrays are empty.

Usage::

    .venv\\Scripts\\python.exe scripts\\backtest_history_fetch.py \\
        --from 2025-10-24 --to 2026-04-23 --fan 6

Rate limited at 0.5s per call to match the previous fetcher (see
``out/history_fetch.log``). ~168 calls for 6 months × 2 indices × 14
offsets → ~3 minutes.
"""
from __future__ import annotations

import argparse
import asyncio
import calendar
import json
import os
import time as _time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from dotenv import load_dotenv

DHAN_BASE = "https://api.dhan.co/v2"

# Index -> Dhan security_id for the underlying INDEX (used as the
# rollingoption "anchor" from which ATM±N is resolved).
INDEX_SECURITY_ID = {
    "NIFTY": 13,
    "BANKNIFTY": 25,
}


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


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _iter_months(start: date, end: date) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


def _month_window(year: int, month: int, range_from: date, range_to: date) -> tuple[date, date]:
    first = date(year, month, 1)
    last = date(year, month, calendar.monthrange(year, month)[1])
    return (max(first, range_from), min(last, range_to))


def _signed_label(offset: int) -> str:
    return f"+{offset}" if offset >= 0 else f"{offset}"


def _strike_token(offset: int) -> str:
    if offset == 0:
        return "ATM"
    return f"ATM+{offset}" if offset > 0 else f"ATM{offset}"  # negative already has -


async def _fetch_one(
    client: httpx.AsyncClient, *,
    security_id: int, option_type: str, offset: int,
    from_dt: date, to_dt: date,
) -> dict | None:
    payload = {
        "exchangeSegment": "NSE_FNO",
        "interval": "1",
        "securityId": security_id,
        "instrument": "OPTIDX",
        "expiryFlag": "MONTH",
        "expiryCode": 1,
        "strike": _strike_token(offset),
        "drvOptionType": "CALL" if option_type == "CE" else "PUT",
        "requiredData": ["open", "high", "low", "close", "volume", "oi", "spot", "strike"],
        "fromDate": from_dt.strftime("%Y-%m-%d"),
        "toDate":   to_dt.strftime("%Y-%m-%d"),
    }
    # Retry once on transient 5xx / rate limits.
    for attempt in (1, 2, 3):
        try:
            r = await client.post("/charts/rollingoption", json=payload, timeout=60.0)
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError) as e:
            if attempt == 3:
                return {"_error": f"transport: {type(e).__name__}: {e}"}
            await asyncio.sleep(1.5 * attempt)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:  # noqa: BLE001
                return {"_error": f"json-parse: {r.text[:200]}"}
        if r.status_code in (429, 500, 502, 503, 504):
            if attempt == 3:
                return {"_error": f"http {r.status_code}: {r.text[:200]}"}
            # Back off harder on 429.
            await asyncio.sleep(2.0 * attempt if r.status_code == 429 else 1.0 * attempt)
            continue
        # Hard fail (400 etc.) — don't retry.
        return {"_error": f"http {r.status_code}: {r.text[:200]}"}
    return {"_error": "exhausted retries"}


def _count_bars(body: dict, option_type: str) -> int:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return 0
    leg = data.get("ce" if option_type == "CE" else "pe")
    if not isinstance(leg, dict):
        return 0
    ts = leg.get("timestamp")
    return len(ts) if isinstance(ts, list) else 0


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="from_date", type=_parse_date, required=True)
    ap.add_argument("--to",   dest="to_date",   type=_parse_date, required=True)
    ap.add_argument("--fan", type=int, default=6,
                    help="Max |offset| to fetch (CE: 0..+fan, PE: 0..-fan). Default 6.")
    ap.add_argument("--index", default="both", choices=["NIFTY", "BANKNIFTY", "both"])
    ap.add_argument("--cache-root", default=None,
                    help="Override cache root (default: $VOLSCALP_DATA_DIR/backtest_cache/rollingoption "
                         "or <repo>/data/backtest_cache/rollingoption).")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="Seconds between API calls (rate limit). Default 0.5s.")
    args = ap.parse_args()

    if args.from_date > args.to_date:
        print("ERROR: --from must be <= --to")
        return 2

    repo_root = Path(__file__).resolve().parent.parent
    env_path = _resolve_env_path(repo_root)
    load_dotenv(env_path)
    client_id = os.getenv("DHAN_CLIENT_ID", "")
    token = os.getenv("DHAN_ACCESS_TOKEN", "")
    if not client_id or not token:
        print(f"ERROR: DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN not set in {env_path}")
        return 2

    if args.cache_root:
        cache_root = Path(args.cache_root).expanduser().resolve()
    else:
        data_dir = Path(os.getenv("VOLSCALP_DATA_DIR", "") or (repo_root / "data"))
        if not data_dir.is_absolute():
            data_dir = repo_root / data_dir
        cache_root = data_dir / "backtest_cache" / "rollingoption"
    cache_root.mkdir(parents=True, exist_ok=True)

    indices = ["NIFTY", "BANKNIFTY"] if args.index == "both" else [args.index]
    months = _iter_months(args.from_date, args.to_date)

    # Build the job list: (index, YYYY-MM, option_type, signed_offset, from, to).
    jobs: list[tuple[str, str, str, int, date, date]] = []
    for idx in indices:
        for (y, m) in months:
            fm, to = _month_window(y, m, args.from_date, args.to_date)
            ym = f"{y:04d}-{m:02d}"
            for off in range(0, args.fan + 1):
                jobs.append((idx, ym, "CE", +off, fm, to))
            for off in range(0, args.fan + 1):
                jobs.append((idx, ym, "PE", -off, fm, to))

    already_cached = 0
    pending_jobs: list[tuple[str, str, str, int, date, date]] = []
    for j in jobs:
        idx, ym, opt, off, fm, to = j
        fname = f"{opt}_ATM{_signed_label(off)}.json"
        path = cache_root / idx / ym / fname
        if path.exists() and path.stat().st_size > 10:
            already_cached += 1
        else:
            pending_jobs.append(j)

    eta_s = int(len(pending_jobs) * args.sleep * 1.1)
    print(f"# env: {env_path}")
    print(f"# cache root: {cache_root}")
    print(f"# indices: {indices}")
    print(f"# range: {args.from_date} .. {args.to_date}")
    print(f"# months: {len(months)} (first={months[0][0]:04d}-{months[0][1]:02d} "
          f"last={months[-1][0]:04d}-{months[-1][1]:02d})")
    print(f"# fan: {args.fan}  (offsets: CE +0..+{args.fan}, PE 0..-{args.fan})")
    print(f"# jobs: {len(jobs)}")
    print(f"# already cached: {already_cached}  pending: {len(pending_jobs)}")
    print(f"# sleep/call: {args.sleep}s  projected eta: ~{eta_s}s", flush=True)

    if not pending_jobs:
        print("# nothing to do (all cached).")
        return 0

    headers = {
        "access-token": token,
        "client-id": client_id,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    started = _time.monotonic()
    fetched = 0
    failed = 0
    empty = 0
    total_jobs = len(jobs)
    progress_idx = already_cached

    async with httpx.AsyncClient(base_url=DHAN_BASE, headers=headers) as client:
        for (idx, ym, opt, off, fm, to) in pending_jobs:
            progress_idx += 1
            fname = f"{opt}_ATM{_signed_label(off)}.json"
            path = cache_root / idx / ym / fname
            path.parent.mkdir(parents=True, exist_ok=True)

            sec = INDEX_SECURITY_ID[idx]
            body = await _fetch_one(
                client,
                security_id=sec, option_type=opt, offset=off,
                from_dt=fm, to_dt=to,
            )
            if body is None or not isinstance(body, dict) or body.get("_error"):
                failed += 1
                err = (body or {}).get("_error", "no body") if isinstance(body, dict) else "no body"
                print(f"[{progress_idx}/{total_jobs}] {idx}\\{ym}\\{fname}  FAIL  {err}", flush=True)
                await asyncio.sleep(args.sleep)
                continue

            bars = _count_bars(body, opt)
            if bars == 0:
                empty += 1
            else:
                fetched += 1

            path.write_text(json.dumps(body), encoding="utf-8")
            elapsed = int(_time.monotonic() - started)
            remaining = max(0, int((len(pending_jobs) - (progress_idx - already_cached))
                                    * args.sleep * 1.05))
            # Log every 10th in the main path + the final line.
            if (progress_idx % 10 == 0) or progress_idx == total_jobs:
                print(f"[{progress_idx}/{total_jobs}] {idx}\\{ym}\\{fname}  bars={bars}  "
                      f"elapsed={elapsed}s  eta={remaining}s", flush=True)
            await asyncio.sleep(args.sleep)

    print()
    print(f"# done: fetched={fetched} skipped={already_cached} failed={failed} empty={empty}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
