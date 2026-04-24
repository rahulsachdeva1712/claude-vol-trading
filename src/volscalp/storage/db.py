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

    async def update_leg_entry(self, leg_row_id: int, leg: Leg) -> None:
        """Patch a leg's entry/status fields after the fill ack lands.

        Unlike `insert_leg` (called at PENDING), this is invoked from the
        reconciler when a Dhan position shows the order has filled. We
        overwrite status + entry_price + sl_price + entry_order_id in
        place so the UI + post-restart recovery both see the correct
        ACTIVE row. See FRD §5.1 / §8.2.
        """
        assert self._conn
        async with self._lock:
            await self._conn.execute(
                """UPDATE legs SET status=?, entry_price=?, sl_price=?,
                                    entry_order_id=?, entry_ts=COALESCE(entry_ts, ?)
                   WHERE id=?""",
                (
                    leg.status.value, leg.entry_price, leg.sl_price,
                    leg.entry_order_id, _utcnow_iso(), leg_row_id,
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

    async def fetch_mode_cumulative_pnl_before_date(
        self, mode_label: str, session_date_iso: str,
    ) -> float:
        """Sum of closed-cycle P&L across every session BEFORE the given
        date for this mode. Used to build a hybrid live cumulative that
        stitches prior-days' internal accounting onto today's
        broker-sourced realized (see FRD §11.4) so the two KPIs reconcile
        on a day with no prior sessions."""
        assert self._conn
        async with self._conn.execute(
            """SELECT COALESCE(SUM(c.cycle_pnl), 0)
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ?
                 AND c.ended_at IS NOT NULL
                 AND s.session_date < ?""",
            (mode_label, session_date_iso),
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

    async def fetch_daily_pnl(self, mode_label: str) -> list[dict[str, Any]]:
        """One row per trading day with that day's realised P&L, across
        every session ever recorded for this mode (paper | live). Rows
        are returned oldest-first so the caller can build a cumulative
        equity curve by running-sum without re-sorting.

        Used by the dashboard's "Cumulative P&L across days" chart
        (FRD §10.1). A day with only open cycles (nothing closed) is
        omitted — we only count cycles with `ended_at IS NOT NULL`, so
        cycle_pnl is the realised figure.
        """
        assert self._conn
        async with self._conn.execute(
            """SELECT s.session_date,
                      COALESCE(SUM(c.cycle_pnl), 0) AS day_pnl,
                      COUNT(c.id)                   AS closed_cycles
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND c.ended_at IS NOT NULL
               GROUP BY s.session_date
               ORDER BY s.session_date ASC""",
            (mode_label,),
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        cum = 0.0
        for session_date, day_pnl, closed_cycles in rows:
            day_pnl_f = float(day_pnl or 0.0)
            cum += day_pnl_f
            out.append({
                "session_date": session_date,
                "day_pnl": day_pnl_f,
                "cum_pnl": cum,
                "closed_cycles": int(closed_cycles or 0),
            })
        return out

    async def fetch_closed_trades_today(
        self, mode_label: str, today_date: str
    ) -> list[dict[str, Any]]:
        """Closed cycles for a mode on a specific session_date, newest first.

        A new DB session row is created on every process start, so by
        session_id alone the Closed Trades table empties after a restart.
        Scoping by session_date rolls up all of today's cycles across any
        sessions the engine opened today.
        """
        assert self._conn
        async with self._conn.execute(
            """SELECT c.id, c.cycle_no, c.underlying, c.cycle_pnl, c.peak_mtm, c.trough_mtm,
                      c.exit_reason, c.started_at, c.ended_at, s.session_date
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND s.session_date = ? AND c.ended_at IS NOT NULL
               ORDER BY c.id DESC""",
            (mode_label, today_date),
        ) as cur:
            rows = await cur.fetchall()
        cols = ["id", "cycle_no", "underlying", "cycle_pnl", "peak_mtm", "trough_mtm",
                "exit_reason", "started_at", "ended_at", "session_date"]
        return [dict(zip(cols, r)) for r in rows]

    async def fetch_open_cycles_with_legs(
        self, mode_label: str, underlying: str, today_date: str
    ) -> list[dict[str, Any]]:
        """Cycles for this (mode, underlying, date) that the engine never
        closed — typically because a prior process died or was restarted
        while a live position was still open.

        Returned shape:
            [{cycle_row_id, cycle_no, underlying, atm_at_start, started_at,
              legs: [{leg_row_id, slot, kind, option_type, underlying,
                      strike, expiry, security_id, trading_symbol, lot_size,
                      lots, quantity, status, entry_price, sl_price,
                      entry_order_id}, ...]}, ...]

        The engine calls this on startup (alongside KPI backfill) so it
        can adopt positions the previous run left open — the reconciler
        then bridges them from PENDING → ACTIVE once Dhan confirms fills.
        See FRD §8.2 (restart-safe adoption).
        """
        assert self._conn
        async with self._conn.execute(
            """SELECT c.id, c.cycle_no, c.underlying, c.atm_at_start, c.started_at
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND c.underlying = ? AND s.session_date = ?
                     AND c.ended_at IS NULL
               ORDER BY c.id ASC""",
            (mode_label, underlying, today_date),
        ) as cur:
            cycle_rows = await cur.fetchall()

        out: list[dict[str, Any]] = []
        for cycle_row_id, cycle_no, und, atm, started_at in cycle_rows:
            async with self._conn.execute(
                """SELECT id, slot, kind, option_type, underlying, strike, expiry,
                          security_id, trading_symbol, lot_size, lots, quantity,
                          status, entry_price, sl_price, entry_order_id
                   FROM legs
                   WHERE cycle_id = ?
                   ORDER BY slot ASC""",
                (cycle_row_id,),
            ) as leg_cur:
                leg_rows = await leg_cur.fetchall()
            legs = []
            for (lid, slot, kind, opt, lund, strike, expiry, sec_id, tsym,
                 lot_size, lots, qty, status, entry_price, sl_price,
                 entry_oid) in leg_rows:
                legs.append({
                    "leg_row_id": lid,
                    "slot": int(slot),
                    "kind": kind,
                    "option_type": opt,
                    "underlying": lund,
                    "strike": int(strike),
                    "expiry": expiry,
                    "security_id": int(sec_id) if sec_id is not None else 0,
                    "trading_symbol": tsym or "",
                    "lot_size": int(lot_size) if lot_size is not None else 0,
                    "lots": int(lots) if lots is not None else 1,
                    "quantity": int(qty) if qty is not None else 0,
                    "status": status,
                    "entry_price": float(entry_price) if entry_price is not None else 0.0,
                    "sl_price": float(sl_price) if sl_price is not None else 0.0,
                    "entry_order_id": entry_oid or "",
                })
            out.append({
                "cycle_row_id": int(cycle_row_id),
                "cycle_no": int(cycle_no),
                "underlying": und,
                "atm_at_start": int(atm) if atm is not None else 0,
                "started_at": started_at,
                "legs": legs,
            })
        return out

    async def fetch_today_engine_kpis(
        self, mode_label: str, underlying: str, today_date: str
    ) -> dict[str, Any]:
        """Reconstruct in-memory engine counters from today's closed cycles.

        Called on engine startup so the KPI strip (realized P&L, closed
        cycles, wins/losses, peak/trough, cycle counter) survives a process
        restart mid-session. Peak/trough here is the running max/min of
        cumulative realized P&L across cycle boundaries — we can't recover
        intra-cycle MTM after restart, but cycle-close boundaries are a
        faithful reconstruction of what the engine would have held at the
        moment of its last close.
        """
        assert self._conn
        async with self._conn.execute(
            """SELECT c.cycle_no, c.cycle_pnl
               FROM cycles c
               JOIN sessions s ON s.id = c.session_id
               WHERE s.mode = ? AND c.underlying = ? AND s.session_date = ?
                     AND c.ended_at IS NOT NULL
               ORDER BY c.id ASC""",
            (mode_label, underlying, today_date),
        ) as cur:
            rows = await cur.fetchall()

        realized_pnl = 0.0
        wins = 0
        losses = 0
        peak = 0.0
        trough = 0.0
        max_cycle_no = 0
        running = 0.0
        for cycle_no, cycle_pnl in rows:
            p = float(cycle_pnl) if cycle_pnl is not None else 0.0
            running += p
            if p > 0:
                wins += 1
            elif p < 0:
                losses += 1
            if running > peak:
                peak = running
            if running < trough:
                trough = running
            if cycle_no is not None and int(cycle_no) > max_cycle_no:
                max_cycle_no = int(cycle_no)
        realized_pnl = running
        return {
            "realized_pnl": realized_pnl,
            "closed_cycles": len(rows),
            "wins": wins,
            "losses": losses,
            "peak_session_pnl": peak,
            "trough_session_pnl": trough,
            "max_cycle_no": max_cycle_no,
        }
