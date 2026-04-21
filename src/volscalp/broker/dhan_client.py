"""Thin async REST wrapper around the Dhan v2 HTTP API.

We intentionally avoid relying solely on the blocking dhanhq SDK inside
the hot path — orders go through an async httpx client so the event loop
never blocks on network I/O. The official SDK is still used for TOTP
login helpers and instrument constants where it adds value.
"""
from __future__ import annotations

from typing import Any

import httpx

from ..logging_setup import get_logger
from ..models import OrderReport, OrderRequest

log = get_logger(__name__)

DHAN_BASE = "https://api.dhan.co/v2"


class DhanClient:
    """Async REST client for Dhan orders + positions."""

    def __init__(self, client_id: str, access_token: str, timeout_s: float = 5.0):
        self.client_id = client_id
        self.access_token = access_token
        self._http = httpx.AsyncClient(
            base_url=DHAN_BASE,
            timeout=timeout_s,
            headers={
                "access-token": access_token,
                "client-id": client_id,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            http2=True,
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ---- orders ------------------------------------------------------------

    async def place_order(self, req: OrderRequest) -> OrderReport:
        payload: dict[str, Any] = {
            "dhanClientId": self.client_id,
            "correlationId": req.client_order_id,
            "transactionType": req.side,
            "exchangeSegment": req.exchange_segment,
            "productType": req.product_type,
            "orderType": req.order_type,
            "validity": "DAY",
            "securityId": str(req.security_id),
            "quantity": req.quantity,
            "price": req.price if req.order_type != "MARKET" else 0,
            "tradingSymbol": req.trading_symbol,
        }
        try:
            r = await self._http.post("/orders", json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            log.error("dhan_place_order_http_error", error=str(e), correlation_id=req.client_order_id)
            return OrderReport(
                client_order_id=req.client_order_id,
                broker_order_id="",
                security_id=req.security_id,
                side=req.side,
                quantity=req.quantity,
                filled_quantity=0,
                avg_fill_price=0.0,
                status="FAILED",
                message=str(e),
            )

        status = (data.get("orderStatus") or "PENDING").upper()
        return OrderReport(
            client_order_id=req.client_order_id,
            broker_order_id=str(data.get("orderId", "")),
            security_id=req.security_id,
            side=req.side,
            quantity=req.quantity,
            filled_quantity=int(data.get("tradedQuantity", 0) or 0),
            avg_fill_price=float(data.get("averageTradedPrice", 0) or 0),
            status=status if status in ("PENDING", "OPEN", "FILLED", "REJECTED", "CANCELLED") else "PENDING",
            message=str(data.get("remarks", "")),
        )

    async def get_order(self, broker_order_id: str) -> dict[str, Any]:
        r = await self._http.get(f"/orders/{broker_order_id}")
        r.raise_for_status()
        return r.json()

    async def list_orders(self) -> list[dict[str, Any]]:
        r = await self._http.get("/orders")
        r.raise_for_status()
        return r.json() if isinstance(r.json(), list) else []

    # ---- positions ---------------------------------------------------------

    async def positions(self) -> list[dict[str, Any]]:
        """Current day positions from Dhan."""
        r = await self._http.get("/positions")
        r.raise_for_status()
        body = r.json()
        return body if isinstance(body, list) else body.get("data", [])

    async def funds(self) -> dict[str, Any]:
        r = await self._http.get("/fundlimit")
        r.raise_for_status()
        return r.json()
