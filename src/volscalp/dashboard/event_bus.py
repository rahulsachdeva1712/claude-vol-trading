"""Simple fan-out bus for pushing engine events to dashboard clients.

Every subscriber gets its own bounded queue. Slow consumers drop oldest.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class EventBus:
    def __init__(self, queue_size: int = 1024):
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        self._queue_size = queue_size

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._queue_size)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    async def broadcast(self, kind: str, payload: dict[str, Any]) -> None:
        msg = {"kind": kind, "ts_ns": time.time_ns(), "payload": payload}
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(msg)
                except asyncio.QueueEmpty:
                    pass
