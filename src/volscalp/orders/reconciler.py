"""Periodic reconciliation between app-tracked positions and Dhan's position book.

We only count trades tagged with our `correlationId`, so manual trades
placed directly on Dhan (outside the app) are filtered out of our MTM.
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..broker.dhan_client import DhanClient
from ..logging_setup import get_logger

log = get_logger(__name__)


class PositionReconciler:
    def __init__(self, client: DhanClient, interval_s: float = 1.0):
        self.client = client
        self.interval_s = max(0.5, interval_s)
        self._known_order_ids: set[str] = set()
        self._last_snapshot: list[dict[str, Any]] = []
        self._stop = asyncio.Event()

    def track_order(self, broker_order_id: str) -> None:
        if broker_order_id:
            self._known_order_ids.add(str(broker_order_id))

    def stop(self) -> None:
        self._stop.set()

    @property
    def last_snapshot(self) -> list[dict[str, Any]]:
        return list(self._last_snapshot)

    async def run(self) -> None:
        log.info("reconciler_started", interval_s=self.interval_s)
        while not self._stop.is_set():
            try:
                positions = await self.client.positions()
                ours = [p for p in positions if self._is_ours(p)]
                self._last_snapshot = ours
            except Exception as e:  # noqa: BLE001
                log.warning("reconciler_poll_failed", error=str(e))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass
        log.info("reconciler_stopped")

    def _is_ours(self, pos: dict[str, Any]) -> bool:
        # Dhan positions endpoint doesn't return correlationId directly on every
        # release; match via any order-id field we know belongs to us.
        for key in ("orderId", "originalOrderId", "correlationId"):
            if str(pos.get(key, "")) in self._known_order_ids:
                return True
        return False
