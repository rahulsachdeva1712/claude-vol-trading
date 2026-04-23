"""Market data service — glues the Dhan WS, candle builder, and spot tracker.

Flow:
    DhanMarketFeed → tick queue → MarketDataService.run() → CandleBuilder
                                                           → spot cache
                                                           → strategy callbacks
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from ..logging_setup import get_logger
from ..models import Bar, Tick
from .candles import CandleBuilder

log = get_logger(__name__)


TickHandler = Callable[[Tick], Awaitable[None] | None]
BarHandler = Callable[[Bar], Awaitable[None] | None]

_HEARTBEAT_INTERVAL_S = 30.0


class MarketDataService:
    """Owns the tick queue and candle builder; dispatches to listeners."""

    def __init__(self, tick_queue: asyncio.Queue[Tick]):
        self.tick_queue = tick_queue
        self._tick_listeners: list[TickHandler] = []
        self._bar_close_listeners: list[BarHandler] = []
        self._bar_update_listeners: list[BarHandler] = []
        self._spot_by_name: dict[str, float] = {}  # "NIFTY" / "BANKNIFTY"
        self._spot_security_map: dict[int, str] = {}
        self._stop = asyncio.Event()

        self._candles = CandleBuilder(
            on_bar_close=self._on_bar_close,
            on_bar_update=self._on_bar_update,
        )

        # Diagnostics — cheap counters for the periodic heartbeat log.
        self.ticks_total: int = 0
        self.bars_closed_total: int = 0
        self._ticks_since_heartbeat: int = 0
        self._bars_since_heartbeat: int = 0
        self._unique_securities: set[int] = set()

    # ---- registration ------------------------------------------------------

    def on_tick(self, handler: TickHandler) -> None:
        self._tick_listeners.append(handler)

    def on_bar_close(self, handler: BarHandler) -> None:
        self._bar_close_listeners.append(handler)

    def on_bar_update(self, handler: BarHandler) -> None:
        self._bar_update_listeners.append(handler)

    def register_spot(self, security_id: int, name: str) -> None:
        self._spot_security_map[security_id] = name

    def spot(self, name: str) -> float:
        return self._spot_by_name.get(name, 0.0)

    def last_ltp(self, security_id: int) -> float:
        return self._candles.last_ltp(security_id)

    def current_bar(self, security_id: int) -> Bar | None:
        return self._candles.current_bar(security_id)

    # ---- lifecycle ---------------------------------------------------------

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        log.info("market_data_service_started")
        last_beat = time.monotonic()
        while not self._stop.is_set():
            try:
                tick = await asyncio.wait_for(self.tick_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                if time.monotonic() - last_beat >= _HEARTBEAT_INTERVAL_S:
                    self._emit_heartbeat()
                    last_beat = time.monotonic()
                continue

            self.ticks_total += 1
            self._ticks_since_heartbeat += 1
            self._unique_securities.add(tick.security_id)

            # Spot capture for underlyings.
            name = self._spot_security_map.get(tick.security_id)
            if name:
                self._spot_by_name[name] = tick.ltp

            # Candle aggregation.
            self._candles.ingest(tick)

            # Tick listeners (strategy hot path).
            for lst in self._tick_listeners:
                try:
                    result = lst(tick)
                    if asyncio.iscoroutine(result):
                        asyncio.create_task(result)
                except Exception as e:  # noqa: BLE001
                    log.warning("tick_listener_error", error=str(e))

            if time.monotonic() - last_beat >= _HEARTBEAT_INTERVAL_S:
                self._emit_heartbeat()
                last_beat = time.monotonic()

        log.info("market_data_service_stopped")

    def _emit_heartbeat(self) -> None:
        log.info(
            "market_data_heartbeat",
            ticks_total=self.ticks_total,
            bars_closed_total=self.bars_closed_total,
            ticks_last_interval=self._ticks_since_heartbeat,
            bars_last_interval=self._bars_since_heartbeat,
            unique_securities=len(self._unique_securities),
            spots={k: round(v, 2) for k, v in self._spot_by_name.items() if v > 0},
            queue_depth=self.tick_queue.qsize(),
        )
        self._ticks_since_heartbeat = 0
        self._bars_since_heartbeat = 0

    # ---- bar callbacks -----------------------------------------------------

    def _on_bar_close(self, bar: Bar) -> None:
        self.bars_closed_total += 1
        self._bars_since_heartbeat += 1
        for lst in self._bar_close_listeners:
            try:
                result = lst(bar)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:  # noqa: BLE001
                log.warning("bar_close_listener_error", error=str(e))

    def _on_bar_update(self, bar: Bar) -> None:
        for lst in self._bar_update_listeners:
            try:
                result = lst(bar)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception as e:  # noqa: BLE001
                log.warning("bar_update_listener_error", error=str(e))
