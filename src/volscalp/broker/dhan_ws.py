"""Dhan market-feed WebSocket client.

Dhan's live market-feed WS (v2) accepts a subscription list and streams
binary-packed ticker / quote / depth messages. This wrapper:

    * connects and authenticates with client_id + access_token
    * sends a subscribe frame for a caller-supplied instrument list
    * parses quote packets into `Tick` objects
    * pushes Ticks into an asyncio.Queue (the engine's hot path)
    * reconnects with exponential backoff on failure

Protocol reference (Dhan v2 market feed):
    wss://api-feed.dhan.co?version=2&token=<ACCESS_TOKEN>&clientId=<CLIENT_ID>&authType=2

The binary protocol is little-endian. We support the most relevant
response codes:
    2 = Ticker packet (LTP only)
    4 = Quote packet  (LTP + bid/ask + volume + OHLC)
    5 = OI packet
    6 = Prev close
    8 = Full market depth (20-level)
    50 = Connection feedback / disconnect

Subscription request codes:
    15 = Ticker
    17 = Quote
    21 = 20-depth
    23 = Full (quote + OI + prev close)

We default to Quote (`17`) and upgrade only if the config demands.
"""
from __future__ import annotations

import asyncio
import json
import struct
import time
from collections.abc import Iterable

import websockets
from websockets.client import WebSocketClientProtocol

from ..logging_setup import get_logger
from ..models import Tick

log = get_logger(__name__)

WS_URL = "wss://api-feed.dhan.co"

# --- protocol constants ---
RESP_TICKER = 2
RESP_QUOTE = 4
RESP_OI = 5
RESP_PREV_CLOSE = 6
RESP_DEPTH = 8
RESP_FEEDBACK = 50

REQ_SUBSCRIBE_TICKER = 15
REQ_SUBSCRIBE_QUOTE = 17
REQ_SUBSCRIBE_DEPTH = 21
REQ_UNSUBSCRIBE = 16


def _subscribe_payload(request_code: int, instruments: list[tuple[str, int]]) -> str:
    """Build a subscribe JSON payload.

    Dhan expects:
        {"RequestCode": 17,
         "InstrumentCount": N,
         "InstrumentList": [{"ExchangeSegment":"NSE_FNO","SecurityId":"12345"}, ...]}
    """
    return json.dumps(
        {
            "RequestCode": request_code,
            "InstrumentCount": len(instruments),
            "InstrumentList": [
                {"ExchangeSegment": seg, "SecurityId": str(sid)} for seg, sid in instruments
            ],
        }
    )


def _parse_packet(buf: bytes) -> Tick | None:
    """Parse one binary packet from Dhan's feed into a Tick, if it's a quote/ticker.

    Header layout (8 bytes): <B H B I>
        response_code (1 byte)
        message_len   (2 bytes)
        exch_segment  (1 byte)
        security_id   (4 bytes)
    """
    if len(buf) < 8:
        return None
    code, _msg_len, seg_id, sec_id = struct.unpack_from("<BHBI", buf, 0)

    now_ns = time.time_ns()

    if code == RESP_TICKER and len(buf) >= 16:
        ltp, ltt = struct.unpack_from("<fI", buf, 8)
        return Tick(
            security_id=sec_id,
            exchange_segment=_segment_name(seg_id),
            ltp=float(ltp),
            ltq=0,
            ltt_epoch=int(ltt),
            ts_ns=now_ns,
        )

    if code == RESP_QUOTE and len(buf) >= 50:
        # Layout from Dhan docs: LTP(f), LTQ(H), LTT(I), ATP(f), Volume(I),
        # TotalSellQty(I), TotalBuyQty(I), Open(f), Close(f), High(f), Low(f)
        # plus bid/ask in newer builds; we decode conservatively.
        (ltp, ltq, ltt, _atp, volume, _sell_q, _buy_q, _open, _close, _high, _low) = struct.unpack_from(
            "<fHIfIIIffff", buf, 8
        )
        bid = ask = 0.0
        bid_qty = ask_qty = 0
        if len(buf) >= 66:
            # Optional best bid/ask (may differ across firmware versions).
            try:
                bid, bid_qty, ask, ask_qty = struct.unpack_from("<fIfI", buf, 50)
            except struct.error:
                pass
        return Tick(
            security_id=sec_id,
            exchange_segment=_segment_name(seg_id),
            ltp=float(ltp),
            ltq=int(ltq),
            ltt_epoch=int(ltt),
            bid=float(bid),
            ask=float(ask),
            bid_qty=int(bid_qty),
            ask_qty=int(ask_qty),
            volume=int(volume),
            ts_ns=now_ns,
        )

    # RESP_OI / RESP_PREV_CLOSE / RESP_DEPTH / RESP_FEEDBACK — ignored for now.
    return None


def _segment_name(seg_id: int) -> str:
    return {
        1: "NSE_EQ",
        2: "NSE_FNO",
        3: "NSE_CURRENCY",
        4: "BSE_EQ",
        5: "MCX_COMM",
        7: "BSE_FNO",
        8: "BSE_CURRENCY",
        9: "IDX_I",
    }.get(seg_id, "UNKNOWN")


class DhanMarketFeed:
    """Asyncio Dhan WS client that pushes Ticks into an output queue."""

    def __init__(
        self,
        client_id: str,
        access_token: str,
        out_queue: asyncio.Queue[Tick],
        backoffs: list[float],
        feed_mode: str = "quote",
    ):
        self.client_id = client_id
        self.access_token = access_token
        self.out = out_queue
        self.backoffs = backoffs or [1, 2, 4, 8, 16, 30]
        self.feed_mode = feed_mode
        self._subscriptions: set[tuple[str, int]] = set()
        self._ws: WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._first_tick_logged: bool = False
        self._last_msg_mono: float = 0.0

    @property
    def request_code(self) -> int:
        return {
            "ticker": REQ_SUBSCRIBE_TICKER,
            "quote": REQ_SUBSCRIBE_QUOTE,
            "depth_20": REQ_SUBSCRIBE_DEPTH,
        }.get(self.feed_mode, REQ_SUBSCRIBE_QUOTE)

    def stop(self) -> None:
        self._stop.set()

    async def run(self, initial_subs: Iterable[tuple[str, int]] | None = None) -> None:
        """Connect, subscribe, pump ticks until stopped."""
        if initial_subs:
            self._subscriptions.update(initial_subs)

        attempt = 0
        while not self._stop.is_set():
            backoff = self.backoffs[min(attempt, len(self.backoffs) - 1)]
            try:
                url = (
                    f"{WS_URL}?version=2"
                    f"&token={self.access_token}"
                    f"&clientId={self.client_id}"
                    f"&authType=2"
                )
                log.info(
                    "dhan_ws_connecting",
                    feed_mode=self.feed_mode,
                    request_code=self.request_code,
                    subs=len(self._subscriptions),
                    client_id=self.client_id,
                )
                async with websockets.connect(
                    url,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    log.info("dhan_ws_connected")
                    if self._subscriptions:
                        payload = _subscribe_payload(self.request_code, list(self._subscriptions))
                        await ws.send(payload)
                        sample = list(self._subscriptions)[:3]
                        log.info(
                            "dhan_ws_subscribed",
                            count=len(self._subscriptions),
                            request_code=self.request_code,
                            sample=sample,
                        )
                    else:
                        log.warning("dhan_ws_no_initial_subscriptions")
                    await self._pump(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                attempt += 1
                log.warning(
                    "dhan_ws_disconnected",
                    error=str(e), error_type=type(e).__name__,
                    next_retry_s=backoff, attempt=attempt,
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    continue
            finally:
                self._ws = None
                self._first_tick_logged = False

    async def _pump(self, ws: WebSocketClientProtocol) -> None:
        binary_count = 0
        parsed_count = 0
        async for msg in ws:
            if self._stop.is_set():
                break
            self._last_msg_mono = time.monotonic()
            if isinstance(msg, bytes):
                binary_count += 1
                tick = _parse_packet(msg)
                if tick is not None:
                    parsed_count += 1
                    if not self._first_tick_logged:
                        log.info(
                            "dhan_ws_first_tick",
                            security_id=tick.security_id,
                            segment=tick.exchange_segment,
                            ltp=tick.ltp,
                            binary_msgs_before=binary_count,
                        )
                        self._first_tick_logged = True
                    try:
                        self.out.put_nowait(tick)
                    except asyncio.QueueFull:
                        try:
                            self.out.get_nowait()
                            self.out.put_nowait(tick)
                        except asyncio.QueueEmpty:
                            pass
                elif binary_count <= 3:
                    # Log a few early unparsed frames so we can diagnose
                    # protocol drift without flooding the log.
                    head = msg[:24].hex()
                    log.info("dhan_ws_binary_unparsed", n=binary_count, head_hex=head, length=len(msg))
            else:
                # Dhan sends JSON control/ack frames — surface them, they're
                # the fastest way to see "invalid token" / "subscription too
                # large" etc.
                log.warning("dhan_ws_text_frame", text=str(msg)[:400])

    async def subscribe(self, instruments: Iterable[tuple[str, int]]) -> None:
        new = set(instruments) - self._subscriptions
        self._subscriptions.update(instruments)
        if self._ws and new:
            await self._ws.send(_subscribe_payload(self.request_code, list(new)))

    async def unsubscribe(self, instruments: Iterable[tuple[str, int]]) -> None:
        drop = set(instruments) & self._subscriptions
        self._subscriptions.difference_update(drop)
        if self._ws and drop:
            await self._ws.send(_subscribe_payload(REQ_UNSUBSCRIBE, list(drop)))
