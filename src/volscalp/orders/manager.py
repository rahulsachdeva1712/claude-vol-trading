"""Unified order manager — routes to paper or live backend.

The strategy only ever talks to OrderManager. Switching between paper
and live is a single setting; the API contract is identical so the
strategy produces the same decisions regardless of backend.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable

from ..broker.dhan_client import DhanClient
from ..config import Mode
from ..logging_setup import get_logger
from ..models import OrderReport, OrderRequest

log = get_logger(__name__)


class OrderBackend:
    async def place(self, req: OrderRequest) -> OrderReport:   # pragma: no cover - interface
        raise NotImplementedError


class PaperBackend(OrderBackend):
    """Simulates fills at the current LTP of the target instrument.

    `price_lookup(security_id)` returns the latest LTP we have in the
    MarketDataService. If no price is known yet, the order is left as
    PENDING and retried on the next tick.
    """

    def __init__(self, price_lookup: Callable[[int], float], slippage_bps: float = 0.0):
        self._lookup = price_lookup
        self._slippage = slippage_bps / 10_000.0

    async def place(self, req: OrderRequest) -> OrderReport:
        ltp = self._lookup(req.security_id)
        if ltp <= 0:
            return OrderReport(
                client_order_id=req.client_order_id,
                broker_order_id=f"PAPER-{uuid.uuid4().hex[:8]}",
                security_id=req.security_id,
                side=req.side,
                quantity=req.quantity,
                filled_quantity=0,
                avg_fill_price=0.0,
                status="PENDING",
                message="no ltp yet",
                ts_ns=time.time_ns(),
            )
        slip = ltp * self._slippage * (1 if req.side == "BUY" else -1)
        fill_price = ltp + slip
        return OrderReport(
            client_order_id=req.client_order_id,
            broker_order_id=f"PAPER-{uuid.uuid4().hex[:8]}",
            security_id=req.security_id,
            side=req.side,
            quantity=req.quantity,
            filled_quantity=req.quantity,
            avg_fill_price=round(fill_price, 2),
            status="FILLED",
            message="paper fill",
            ts_ns=time.time_ns(),
        )


class LiveBackend(OrderBackend):
    """Live Dhan backend. Wraps DhanClient.place_order."""

    def __init__(self, client: DhanClient):
        self._client = client

    async def place(self, req: OrderRequest) -> OrderReport:
        return await self._client.place_order(req)


def new_client_order_id(prefix: str = "vs") -> str:
    return f"{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


class OrderManager:
    """Thin dispatcher. Writes every request/report into the DB via a callback."""

    def __init__(self, mode: Mode, backend: OrderBackend,
                 on_request: Callable[[OrderRequest], asyncio.Future] | None = None,
                 on_report: Callable[[OrderReport], asyncio.Future] | None = None):
        self.mode = mode
        self.backend = backend
        self._on_request = on_request
        self._on_report = on_report

    async def place(self, req: OrderRequest) -> OrderReport:
        log.info(
            "order_place",
            client_order_id=req.client_order_id,
            mode=self.mode.value,
            symbol=req.trading_symbol,
            side=req.side,
            qty=req.quantity,
        )
        if self._on_request:
            try:
                result = self._on_request(req)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:  # noqa: BLE001
                log.warning("on_request_callback_error", error=str(e))
        report = await self.backend.place(req)
        if self._on_report:
            try:
                result = self._on_report(report)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:  # noqa: BLE001
                log.warning("on_report_callback_error", error=str(e))
        log.info(
            "order_report",
            client_order_id=report.client_order_id,
            broker_order_id=report.broker_order_id,
            status=report.status,
            filled_qty=report.filled_quantity,
            avg_price=report.avg_fill_price,
        )
        return report
