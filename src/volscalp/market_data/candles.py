"""In-memory 1-minute candle builder.

Feeds Ticks in; emits a completed Bar the first time we see a tick that
belongs to a new minute. Cheap and allocation-light — one dict lookup
plus at most one Bar allocation per minute per instrument.
"""
from __future__ import annotations

from collections.abc import Callable

from ..logging_setup import get_logger
from ..models import Bar, Tick, minute_of

log = get_logger(__name__)


class CandleBuilder:
    """Per-instrument 1-minute OHLCV aggregator.

    Callbacks:
        on_bar_close(Bar) — fired once when a bar's minute has ended.
        on_bar_update(Bar) — fired on every tick for the currently-open bar
            (so the strategy can react intrabar for SLs if it wants).
    """

    def __init__(
        self,
        on_bar_close: Callable[[Bar], None],
        on_bar_update: Callable[[Bar], None] | None = None,
    ):
        self._open: dict[int, Bar] = {}      # security_id -> live bar
        self._last_ltp: dict[int, float] = {}
        self.on_bar_close = on_bar_close
        self.on_bar_update = on_bar_update

    def ingest(self, tick: Tick) -> None:
        """Process one tick; may trigger on_bar_close for the previous minute."""
        if tick.ltp <= 0:
            return

        minute = minute_of(tick.ltt_epoch or (tick.ts_ns // 1_000_000_000))
        open_bar = self._open.get(tick.security_id)

        if open_bar is None:
            self._open[tick.security_id] = Bar(
                security_id=tick.security_id,
                minute_epoch=minute,
                open=tick.ltp,
                high=tick.ltp,
                low=tick.ltp,
                close=tick.ltp,
                volume=tick.ltq,
                oi=tick.oi,
                tick_count=1,
            )
            self._last_ltp[tick.security_id] = tick.ltp
            if self.on_bar_update:
                self.on_bar_update(self._open[tick.security_id])
            return

        if minute == open_bar.minute_epoch:
            if tick.ltp > open_bar.high:
                open_bar.high = tick.ltp
            if tick.ltp < open_bar.low:
                open_bar.low = tick.ltp
            open_bar.close = tick.ltp
            open_bar.volume += tick.ltq
            if tick.oi:
                open_bar.oi = tick.oi
            open_bar.tick_count += 1
            self._last_ltp[tick.security_id] = tick.ltp
            if self.on_bar_update:
                self.on_bar_update(open_bar)
            return

        # New minute: close the previous bar, start a fresh one.
        closed = open_bar
        self.on_bar_close(closed)

        new_bar = Bar(
            security_id=tick.security_id,
            minute_epoch=minute,
            open=tick.ltp,
            high=tick.ltp,
            low=tick.ltp,
            close=tick.ltp,
            volume=tick.ltq,
            oi=tick.oi,
            tick_count=1,
        )
        self._open[tick.security_id] = new_bar
        self._last_ltp[tick.security_id] = tick.ltp
        if self.on_bar_update:
            self.on_bar_update(new_bar)

    def last_ltp(self, security_id: int) -> float:
        return self._last_ltp.get(security_id, 0.0)

    def current_bar(self, security_id: int) -> Bar | None:
        return self._open.get(security_id)
