"""Dhan instrument master — downloads, caches, and indexes option contracts.

Dhan publishes a daily instrument master as CSV at:
    https://images.dhan.co/api-data/api-scrip-master-detailed.csv

We cache it locally for one trading day. After that we re-download.
"""
from __future__ import annotations

import asyncio
import io
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd

from ..config import IndexName
from ..logging_setup import get_logger
from ..models import OptionInstrument, OptionType

log = get_logger(__name__)

DHAN_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
CACHE_TTL_HOURS = 20   # re-download after 20h so we always have today's master

# Column names can change across Dhan revisions; we remap defensively.
_COLUMN_ALIASES = {
    "SEM_SMST_SECURITY_ID": "security_id",
    "SEM_EXM_EXCH_ID": "exchange",
    "SEM_SEGMENT": "segment",
    "SEM_INSTRUMENT_NAME": "instrument",
    "SEM_EXPIRY_DATE": "expiry",
    "SEM_STRIKE_PRICE": "strike",
    "SEM_OPTION_TYPE": "option_type",
    "SEM_LOT_UNITS": "lot_size",
    "SEM_TRADING_SYMBOL": "symbol",
    "SEM_CUSTOM_SYMBOL": "custom_symbol",
    "SM_SYMBOL_NAME": "underlying_name",
}


class InstrumentMaster:
    """Loads and indexes the Dhan scrip master for NIFTY / BANKNIFTY options."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._df: pd.DataFrame | None = None
        self._by_id: dict[int, OptionInstrument] = {}
        self._by_key: dict[tuple[str, str, int, OptionType], OptionInstrument] = {}

    # ---- load / refresh ----------------------------------------------------

    async def ensure_loaded(self, force: bool = False) -> None:
        cache = self.cache_dir / f"scrip_master_{date.today().isoformat()}.csv"
        fresh = cache.exists() and (
            datetime.now() - datetime.fromtimestamp(cache.stat().st_mtime) < timedelta(hours=CACHE_TTL_HOURS)
        )
        if force or not fresh:
            await self._download(cache)
        await asyncio.to_thread(self._load_from_disk, cache)

    async def _download(self, dest: Path) -> None:
        log.info("dhan_master_download_start", url=DHAN_MASTER_URL)
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(DHAN_MASTER_URL)
            resp.raise_for_status()
        dest.write_bytes(resp.content)
        log.info("dhan_master_download_done", bytes=len(resp.content), path=str(dest))

    def _load_from_disk(self, path: Path) -> None:
        log.info("dhan_master_load_start", path=str(path))
        df = pd.read_csv(path, low_memory=False)
        df = df.rename(columns={k: v for k, v in _COLUMN_ALIASES.items() if k in df.columns})

        # Filter option rows for NIFTY / BANKNIFTY.
        if "instrument" not in df.columns:
            raise RuntimeError("Dhan master missing 'instrument' column; schema changed.")
        options = df[df["instrument"].isin(["OPTIDX", "OPTION"])].copy()

        # Derive underlying name from trading symbol prefix as a fallback.
        def _underlying(row) -> str:
            sym = str(row.get("symbol", "")).upper()
            if sym.startswith("BANKNIFTY"):
                return "BANKNIFTY"
            if sym.startswith("NIFTY"):
                return "NIFTY"
            return str(row.get("underlying_name", "")).upper()

        options["underlying"] = options.apply(_underlying, axis=1)
        options = options[options["underlying"].isin(["NIFTY", "BANKNIFTY"])]

        # Normalise types.
        options["strike"] = pd.to_numeric(options["strike"], errors="coerce").fillna(0).astype(int)
        options["security_id"] = pd.to_numeric(options["security_id"], errors="coerce").fillna(0).astype(int)
        options["lot_size"] = pd.to_numeric(options.get("lot_size", 0), errors="coerce").fillna(0).astype(int)
        options["option_type"] = options["option_type"].astype(str).str.upper().str.strip()
        options["expiry"] = pd.to_datetime(options["expiry"], errors="coerce").dt.date.astype(str)
        options["segment"] = options.get("segment", "NSE_FNO").astype(str).str.upper()

        self._df = options
        self._by_id.clear()
        self._by_key.clear()
        for row in options.itertuples(index=False):
            try:
                ot = OptionType(row.option_type)
            except ValueError:
                continue
            inst = OptionInstrument(
                security_id=int(row.security_id),
                exchange_segment=str(row.segment),
                underlying=row.underlying,
                strike=int(row.strike),
                option_type=ot,
                expiry=str(row.expiry),
                lot_size=int(row.lot_size),
                trading_symbol=str(row.symbol),
            )
            self._by_id[inst.security_id] = inst
            self._by_key[(inst.underlying, inst.expiry, inst.strike, ot)] = inst

        log.info("dhan_master_loaded", option_count=len(self._by_id))

    # ---- lookups -----------------------------------------------------------

    def pick_expiry(self, underlying: IndexName, kind: str = "monthly") -> str:
        """Return ISO date of the next monthly expiry for the given index.

        Heuristic: group all available expiries, pick the last Thursday in
        each month, then return the nearest future one.
        """
        if self._df is None:
            raise RuntimeError("InstrumentMaster not loaded")
        df = self._df[self._df["underlying"] == underlying.value]
        expiries = sorted({e for e in df["expiry"].unique() if e and e != "NaT"})
        if not expiries:
            raise RuntimeError(f"No expiries found for {underlying}")

        today = date.today().isoformat()
        future = [e for e in expiries if e >= today]
        if kind == "monthly":
            # Monthly = last expiry of each month; pick the first such date >= today.
            from collections import defaultdict
            buckets: dict[str, list[str]] = defaultdict(list)
            for e in future:
                ym = e[:7]
                buckets[ym].append(e)
            monthlies = sorted(max(v) for v in buckets.values())
            if not monthlies:
                return future[0]
            return monthlies[0]
        return future[0]

    def get_by_id(self, security_id: int) -> OptionInstrument | None:
        return self._by_id.get(security_id)

    def get(self, underlying: str, expiry: str, strike: int, option_type: OptionType) -> OptionInstrument | None:
        return self._by_key.get((underlying, expiry, strike, option_type))

    def strikes_for(self, underlying: str, expiry: str) -> list[int]:
        if self._df is None:
            return []
        df = self._df
        mask = (df["underlying"] == underlying) & (df["expiry"] == expiry)
        return sorted({int(s) for s in df.loc[mask, "strike"].unique() if s})

    def nearest_strike(self, underlying: str, expiry: str, spot: float, interval: int) -> int:
        """Closest listed strike. If spot sits exactly between two, pick the lower (per FRD §12.1)."""
        strikes = self.strikes_for(underlying, expiry)
        if not strikes:
            # Fallback to pure-math rounding.
            return int((spot // interval) * interval)
        # Prefer listed strikes.
        lower = max((s for s in strikes if s <= spot), default=strikes[0])
        higher = min((s for s in strikes if s > spot), default=lower)
        if abs(spot - lower) <= abs(higher - spot):
            return lower
        return higher
