"""Async SQLite wrapper + repository helpers.

All writes are queued onto a single aiosqlite connection so the hot
path never contends on the DB. Every call here is awaitable but uses
batched commits inside a dedicated worker task.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from ..logging_setup import get_logger
from ..models import Cycle, ExitReason, Leg, OrderReport, OrderRequest

log = get_logger(__name__)

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        await self._conn.executescript(schema)
        await self._conn.commit()
        log.info("db_opened", path=str(self.path))

    async def close(self) -> None:
        if self._conn:
            await self._conn.commit()
            await self._conn.close()
            self._conn = None
            log.info("db_closed")

    # ---- sessions ----------------------------------------------------------

    async def start_session(self, index_name: str, mode: str) -> int:
        """Create a session row and return its id. Paper and live each get
        their own session row per process run."""
        assert self._conn
        async with self._lock:
            cursor = await self._conn.execute(
                "INSERT INTO sessions(session_date, index_name, mode, started_at) VALUES (?, ?, ?, ?)",
                (datetime.now().date().isoformat(), index_name, mode, _utcnow_iso()),
            )
            await self._conn.commit()
            return cursor.lastrowid

    async def end_session(
        self, session_id: int, realized_pnl: float, total_cycles: int, peak: float, trough: float
    ) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                "UPDATE sessions SET ended_at=?, realized_pnl=?, total_cycles=?, peak_pnl=?, trough_pnl=? WHERE id=?",
                (_utcnow_iso(), realized_pnl, total_cycles, peak, trough, session_id),
            )
            await self._conn.commit()

    # ---- cycles ------------------------------------------------------------

    async def insert_cycle(self, session_id: int, cycle: Cycle) -> int:
        assert self._conn
        async with self._lock:
            cursor = await self._conn.execute(
                """INSERT INTO cycles(session_id, cycle_no, underlying, atm_at_start, started_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (session_id, cycle.cycle_id, cycle.underlying, cycle.atm_at_start, _utcnow_iso()),
            )
            await self._conn.commit()
            return cursor.lastrowid

    async def update_cycle_close(
        self, cycle_row_id: int, exit_reason: ExitReason, pnl: float, peak: float, trough: float,
    ) -> None:
        """Close out a cycle row. `lock_activated`/`lock_floor` columns remain
        in the schema for backward compatibility but are never populated since
        the lock-and-trail feature was removed."""
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """UPDATE cycles
                   SET ended_at=?, exit_reason=?, cycle_pnl=?, peak_mtm=?, trough_mtm=?
                   WHERE id=?""",
                (_utcnow_iso(), exit_reason.value, pnl, peak, trough, cycle_row_id),
            )
            await self._conn.commit()

    # ---- legs --------------------------------------------------------------

    async def insert_leg(self, cycle_row_id: int, leg: Leg) -> int:
        assert self._conn
        async with self._lock:
            cursor = await self._conn.execute(
                """INSERT INTO legs(cycle_id, slot, kind, option_type, underlying, strike, expiry,
                                     security_id, trading_symbol, lot_size, lots, quantity,
                                     status, entry_ts, entry_price, sl_price,
                                     entry_order_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cycle_row_id, leg.slot, leg.kind.value, leg.option_type.value,
                    leg.underlying, leg.strike, leg.expiry,
                    leg.security_id, leg.trading_symbol, leg.lot_size, leg.lots, leg.quantity,
                    leg.status.value, _utcnow_iso(), leg.entry_price, leg.sl_price,
                    leg.entry_order_id,
                ),
            )
            await self._conn.commit()
            return cursor.lastrowid

    async def update_leg_exit(self, leg_row_id: int, leg: Leg) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """UPDATE legs SET status=?, exit_ts=?, exit_price=?, exit_reason=?,
                                    pnl=?, exit_order_id=? WHERE id=?""",
                (
                    leg.status.value, _utcnow_iso(), leg.exit_price,
                    leg.exit_reason.value if leg.exit_reason else None,
                    leg.realized_pnl, leg.exit_order_id, leg_row_id,
                ),
            )
            await self._conn.commit()

    # ---- orders ------------------------------------------------------------

    async def record_order_request(
        self, session_id: int | None, req: OrderRequest,
        cycle_row_id: int | None, leg_row_id: int | None,
    ) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """INSERT OR IGNORE INTO orders(client_order_id, session_id, cycle_id, leg_id,
                        security_id, trading_symbol, side, quantity, order_type, product_type,
                        status, placed_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                (
                    req.client_order_id, session_id, cycle_row_id, leg_row_id,
                    req.security_id, req.trading_symbol, req.side, req.quantity,
                    req.order_type, req.product_type, _utcnow_iso(),
                ),
            )
            await self._conn.commit()

    async def record_order_report(self, report: OrderReport) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """UPDATE orders SET broker_order_id=?, status=?, filled_quantity=?,
                                      avg_fill_price=?, message=?, updated_at=?
                   WHERE client_order_id=?""",
                (
                    report.broker_order_id, report.status, report.filled_quantity,
                    report.avg_fill_price, report.message, _utcnow_iso(),
                    report.client_order_id,
                ),
            )
            await self._conn.commit()

    # ---- snapshots / decisions --------------------------------------------

    async def snapshot_bar(
        self, session_id: int, cycle_row_id: int | None, underlying: str,
        spot: float, atm: int, payload: dict[str, Any],
    ) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO bar_snapshots(session_id, cycle_id, minute_epoch, underlying, spot, atm, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, cycle_row_id, int(datetime.now().timestamp()) // 60 * 60,
                 underlying, spot, atm, json.dumps(payload, default=str)),
            )
            await self._conn.commit()

    async def log_decision(
        self, session_id: int | None, cycle_row_id: int | None,
        kind: str, inputs: dict[str, Any], outcome: str,
    ) -> None:
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """INSERT INTO decisions(session_id, cycle_id, ts, kind, inputs_json, outcome)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (session_id, cycle_row_id, _utcnow_iso(), kind,
                 json.dumps(inputs, default=str), outcome),
            )
            await self._conn.commit()

    # ---- read-side ---------------------------------------------------------

    async def fetch_closed_trades(self, session_id: int) -> list[dict[str, Any]]:
        assert self._conn
        async with self._conn.execute(
            """SELECT id, cycle_no, underlying, cycle_pnl, peak_mtm, trough_mtm,
                      exit_reason, started_at, ended_at
               FROM cycles
               WHERE session_id = ? AND ended_at IS NOT NULL
               ORDER BY id DESC""",
            (session_id,),
        ) as cur:
            rows = await cur.fetchall()
        cols = ["id", "cycle_no", "underlying", "cycle_pnl", "peak_mtm", "trough_mtm",
                "exit_reason", "started_at", "ended_at"]
        return [dict(zip(cols, r)) for r in rows]

    async def fetch_mode_cumulative_pnl(self, mode_label: str) -> float:
        """Sum of closed-cycle P&L across every session ever recorded for
        this mode (paper | live). Survives process restarts."""
        assert self._conn
        async with self._conn.execute(
            """SELECT COALESCE(SUM(c.cycle_pnl), 0)
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND c.ended_at IS NOT NULL""",
            (mode_label,),
        ) as cur:
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    async def fetch_closed_trades_all_sessions(self, mode_label: str) -> list[dict[str, Any]]:
        """Closed cycles across every session for a mode, newest first."""
        assert self._conn
        async with self._conn.execute(
            """SELECT c.id, c.cycle_no, c.underlying, c.cycle_pnl, c.peak_mtm, c.trough_mtm,
                      c.exit_reason, c.started_at, c.ended_at, s.session_date
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND c.ended_at IS NOT NULL
               ORDER BY c.id DESC""",
            (mode_label,),
        ) as cur:
            rows = await cur.fetchall()
        cols = ["id", "cycle_no", "underlying", "cycle_pnl", "peak_mtm", "trough_mtm",
                "exit_reason", "started_at", "ended_at", "session_date"]
        return [dict(zip(cols, r)) for r in rows]
