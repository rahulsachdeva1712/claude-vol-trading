"""Periodic reconciliation between app-tracked legs and Dhan's position book.

The reconciler is what bridges Dhan's async fill model into the engine's
state machine. A REST place_order call returns ``orderStatus=PENDING``
with ``tradedQuantity=0`` on success; the real fill lands moments later.
Without this bridge, live legs would stay PENDING forever — SL never
trails, cycle MTM never counts the leg, and the dashboard shows zero
open positions even though the broker is long.

On each tick this loop:

  1. Pulls ``GET /positions`` from Dhan.
  2. For every live engine, walks ``current_cycle.legs``:
        * PENDING leg whose ``security_id`` is present in positions with
          ``buyAvg > 0`` → call ``engine.on_fill_ack(security_id, buyAvg,
          filled_qty)`` — leg goes ACTIVE, SL recomputed from the real
          fill price.
        * ACTIVE leg whose ``security_id`` no longer has a net long
          position (closed out in the portal, broker-initiated stop) →
          call ``engine.on_external_close(security_id, sellAvg)`` — leg
          goes STOPPED with reason EXTERNAL_CLOSE.

This also serves the restart-safety story: when a process boots, the
engine reloads any open cycles from SQLite and the reconciler then
confirms them against the broker. See FRD §5.1 / §5.4 / §8.2.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..broker.dhan_client import DhanClient
from ..logging_setup import get_logger
from ..models import LegStatus

log = get_logger(__name__)


def _as_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class PositionReconciler:
    """Bridges Dhan /positions polls into engine leg-state transitions."""

    def __init__(
        self,
        client: DhanClient,
        live_engines: list | None = None,
        interval_s: float = 1.0,
    ):
        self.client = client
        self.live_engines = list(live_engines or [])
        self.interval_s = max(0.5, interval_s)
        self._last_snapshot: list[dict[str, Any]] = []
        self._stop = asyncio.Event()

    def register_engine(self, engine) -> None:
        """Add a live engine after construction (main wires these post-build)."""
        if engine not in self.live_engines:
            self.live_engines.append(engine)

    def stop(self) -> None:
        self._stop.set()

    @property
    def last_snapshot(self) -> list[dict[str, Any]]:
        return list(self._last_snapshot)

    async def run(self) -> None:
        log.info(
            "reconciler_started",
            interval_s=self.interval_s,
            live_engines=[e.tag for e in self.live_engines],
        )
        while not self._stop.is_set():
            try:
                positions = await self.client.positions()
                self._last_snapshot = list(positions)
                await self._reconcile(positions)
            except Exception as e:  # noqa: BLE001
                log.warning("reconciler_poll_failed", error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
        log.info("reconciler_stopped")

    # ------------------------------------------------------------------
    # Matching logic
    # ------------------------------------------------------------------

    @staticmethod
    def _index_by_security(positions: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        """Reduce the positions list to one row per security_id.

        Dhan may return multiple rows per symbol (e.g. separate INTRADAY
        and CARRYFORWARD entries) — we sum buyQty/sellQty and take a
        qty-weighted buyAvg / sellAvg so the engine sees a single view.
        """
        out: dict[int, dict[str, Any]] = {}
        for p in positions:
            sid = _as_int(p.get("securityId"))
            if not sid:
                continue
            buy_qty = _as_int(p.get("buyQty"))
            sell_qty = _as_int(p.get("sellQty"))
            buy_avg = _as_float(p.get("buyAvg"))
            sell_avg = _as_float(p.get("sellAvg"))
            net_qty = _as_int(p.get("netQty"))
            # Prefer netQty from Dhan when present; otherwise derive.
            if net_qty == 0 and (buy_qty or sell_qty):
                net_qty = buy_qty - sell_qty

            existing = out.get(sid)
            if existing is None:
                out[sid] = {
                    "securityId": sid,
                    "buyQty": buy_qty,
                    "sellQty": sell_qty,
                    "buyAvg": buy_avg,
                    "sellAvg": sell_avg,
                    "netQty": net_qty,
                    "tradingSymbol": p.get("tradingSymbol", ""),
                }
                continue
            # Qty-weighted rollup.
            total_buy = existing["buyQty"] + buy_qty
            total_sell = existing["sellQty"] + sell_qty
            if total_buy > 0:
                existing["buyAvg"] = (
                    existing["buyAvg"] * existing["buyQty"] + buy_avg * buy_qty
                ) / total_buy
            if total_sell > 0:
                existing["sellAvg"] = (
                    existing["sellAvg"] * existing["sellQty"] + sell_avg * sell_qty
                ) / total_sell
            existing["buyQty"] = total_buy
            existing["sellQty"] = total_sell
            existing["netQty"] = existing["netQty"] + net_qty
        return out

    @staticmethod
    def _broker_realized_by_underlying(
        positions: list[dict[str, Any]], underlying: str,
    ) -> float:
        """Sum ``realizedProfit`` across every Dhan position whose
        ``tradingSymbol`` is an option on the given underlying.

        Dhan's position rows for this account use symbols like
        ``NIFTY-Apr2026-24150-CE`` / ``BANKNIFTY-Apr2026-56600-CE`` — a
        plain ``startswith(f"{underlying}-")`` discriminates NIFTY vs
        BANKNIFTY cleanly (BANKNIFTY does NOT start with ``NIFTY-``).
        Equity positions (symbols without the dash prefix) are skipped,
        as are positions on other F&O segments.
        """
        prefix = f"{underlying}-"
        total = 0.0
        for p in positions:
            ts = str(p.get("tradingSymbol", "") or "")
            if not ts.startswith(prefix):
                continue
            seg = str(p.get("exchangeSegment", "") or "")
            if seg not in ("NSE_FNO", "BSE_FNO"):
                continue
            try:
                total += float(p.get("realizedProfit", 0) or 0)
            except (TypeError, ValueError):
                continue
        return total

    async def _reconcile(self, positions: list[dict[str, Any]]) -> None:
        if not self.live_engines:
            return
        by_sid = self._index_by_security(positions)

        # Push Dhan's realized-today figure into each live engine so the
        # KPI strip shows broker truth rather than app-internal cycle
        # accounting. See FRD §8.2.
        for engine in self.live_engines:
            try:
                total_realized = self._broker_realized_by_underlying(
                    positions, engine.underlying.value,
                )
                engine.set_broker_realized(total_realized)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "broker_realized_update_failed",
                    tag=getattr(engine, "tag", "?"), error=str(exc),
                )

        for engine in self.live_engines:
            cycle = engine.current_cycle
            if not cycle:
                continue
            for leg in list(cycle.legs.values()):
                if not leg.security_id:
                    continue
                row = by_sid.get(int(leg.security_id))

                # Case 1: PENDING leg → look for fill.
                if leg.status == LegStatus.PENDING:
                    if row is None:
                        continue
                    # netQty > 0 (we're long) with an established buyAvg
                    # is the strongest signal; partial fills still count
                    # as ACTIVE so SL and MTM kick in.
                    if row["buyAvg"] > 0 and row["buyQty"] > 0 and row["netQty"] > 0:
                        try:
                            await engine.on_fill_ack(
                                int(leg.security_id),
                                float(row["buyAvg"]),
                                int(min(row["buyQty"], leg.quantity or row["buyQty"])),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "fill_ack_failed",
                                tag=engine.tag, slot=leg.slot,
                                security_id=leg.security_id, error=str(exc),
                            )
                    continue

                # Case 2: ACTIVE leg → detect external close.
                if leg.status == LegStatus.ACTIVE:
                    # netQty==0 with sellQty>0 means the position was
                    # fully sold; position missing from response means
                    # the row was reaped (also a close). Either way the
                    # engine should treat it as EXTERNAL_CLOSE.
                    if row is None:
                        try:
                            await engine.on_external_close(
                                int(leg.security_id),
                                float(leg.last_price or leg.entry_price),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "external_close_failed",
                                tag=engine.tag, slot=leg.slot,
                                error=str(exc),
                            )
                        continue
                    if row["netQty"] == 0 and row["sellQty"] > 0:
                        exit_price = (
                            row["sellAvg"] if row["sellAvg"] > 0
                            else (leg.last_price or leg.entry_price)
                        )
                        try:
                            await engine.on_external_close(
                                int(leg.security_id), float(exit_price),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "external_close_failed",
                                tag=engine.tag, slot=leg.slot,
                                error=str(exc),
                            )
                    continue

                # Case 3: EXITING leg → planned exit awaiting fill ack.
                # Same "position gone" detection as external close, but
                # finalized via on_exit_ack so the engine preserves the
                # ORIGINAL exit_reason (LEG_SL, MTM_TARGET, etc.) rather
                # than stamping it EXTERNAL_CLOSE. Until Dhan confirms
                # the SELL filled, the leg stays EXITING and the
                # position remains on the books — that's the whole
                # point of this bridge: no "closed in app, open at
                # broker" orphans.
                if leg.status == LegStatus.EXITING:
                    if row is None:
                        try:
                            await engine.on_exit_ack(
                                int(leg.security_id),
                                float(
                                    leg.exit_price
                                    or leg.last_price
                                    or leg.entry_price
                                ),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "exit_ack_failed",
                                tag=engine.tag, slot=leg.slot,
                                error=str(exc),
                            )
                        continue
                    if row["netQty"] == 0 and row["sellQty"] > 0:
                        exit_price = (
                            row["sellAvg"] if row["sellAvg"] > 0
                            else (
                                leg.exit_price
                                or leg.last_price
                                or leg.entry_price
                            )
                        )
                        try:
                            await engine.on_exit_ack(
                                int(leg.security_id), float(exit_price),
                            )
                        except Exception as exc:  # noqa: BLE001
                            log.warning(
                                "exit_ack_failed",
                                tag=engine.tag, slot=leg.slot,
                                error=str(exc),
                            )
