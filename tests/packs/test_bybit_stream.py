"""Bybit V5 公共 WebSocket：使用本机假服务器验证协议行为，不访问外网。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from websockets.asyncio.server import serve

from talaria.foundation.bars import BarStatus
from talaria.foundation.errors import FeedTransportError
from talaria.foundation.feed import KlineEvent, OrderBookEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK, BybitPublicStream
from talaria.packs.crypto_perp.stream import INTERVAL_BY_TIMEFRAME, TIMEFRAME_BY_INTERVAL

pytestmark = [pytest.mark.small, pytest.mark.spec]

BASE = 1_763_596_800_000
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"

Handler = Callable[[object], Awaitable[None]]


class Harness:
    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    async def run(self, scenario: Callable[[str], Awaitable[None]]) -> None:
        async with serve(self._handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            await asyncio.wait_for(scenario(f"ws://127.0.0.1:{port}"), timeout=5)


def make_stream(url: str, **kwargs: object) -> BybitPublicStream:
    return BybitPublicStream(
        instruments=CRYPTO_PERP_PACK.instruments(), ws_url=url, **kwargs
    )


def kline_payload(
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1",
    close: str = "100",
    volume: str = "1",
    confirm: bool = False,
    start: int = BASE,
) -> dict:
    return {
        "topic": f"kline.{interval}.{symbol}",
        "type": "snapshot",
        "ts": BASE,
        "data": {
            "start": start,
            "end": start + MINUTE,
            "interval": interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": volume,
            "turnover": "100",
            "confirm": confirm,
            "timestamp": start + 1_000,
            "symbol": symbol,
        },
    }


def book_payload(*, symbol: str = "BTCUSDT", bid: str = "100.0", ask: str = "100.1") -> dict:
    return {
        "topic": f"orderbook.1.{symbol}",
        "type": "snapshot",
        "ts": BASE + 500,
        "data": {
            "s": symbol,
            "b": [[bid, "2.5"]],
            "a": [[ask, "3.5"]],
            "ts": BASE + 500,
            "u": 1,
        },
    }


def test_subscribe_ack_then_kline_and_orderbook() -> None:
    received: list[dict] = []

    async def handler(ws: object) -> None:
        received.append(json.loads(await ws.recv()))  # type: ignore[attr-defined]
        await ws.send(json.dumps({"success": True, "ret_msg": "subscribe", "op": "subscribe"}))  # type: ignore[attr-defined]
        await ws.send(json.dumps(kline_payload()))  # type: ignore[attr-defined]
        await ws.send(json.dumps(book_payload()))  # type: ignore[attr-defined]
        await ws.wait_closed()  # type: ignore[attr-defined]

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        bar = await stream.recv()
        book = await stream.recv()
        await stream.close()

        assert received[0]["op"] == "subscribe"
        assert received[0]["args"] == ["kline.1.BTCUSDT", "orderbook.1.BTCUSDT"]
        assert isinstance(bar, KlineEvent)
        assert (bar.instrument_id, bar.timeframe) == (INSTRUMENT, "1m")
        assert bar.bar.timestamp == BASE
        assert bar.bar.close == Decimal("100")
        assert bar.bar.status is BarStatus.NORMAL
        assert bar.confirmed is False
        assert isinstance(book, OrderBookEvent)
        assert book.bid_price == Decimal("100.0")
        assert book.ask_price == Decimal("100.1")
        assert book.bid_size == Decimal("2.5")

    asyncio.run(Harness(handler).run(scenario))


def test_flat_zero_volume_bar_is_no_trades() -> None:
    async def handler(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(json.dumps({"success": True, "op": "subscribe"}))  # type: ignore[attr-defined]
        await ws.send(json.dumps(kline_payload(volume="0", close="99")))  # type: ignore[attr-defined]
        await ws.send(  # type: ignore[attr-defined]
            json.dumps(kline_payload(volume="1", close="100", start=BASE + MINUTE))
        )
        await ws.wait_closed()  # type: ignore[attr-defined]

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        flat = await stream.recv()
        normal = await stream.recv()
        await stream.close()
        assert isinstance(flat, KlineEvent) and flat.bar.status is BarStatus.NO_TRADES
        assert isinstance(normal, KlineEvent) and normal.bar.status is BarStatus.NORMAL

    asyncio.run(Harness(handler).run(scenario))


def test_rejected_subscription_raises_feed_transport_error() -> None:
    async def handler(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(  # type: ignore[attr-defined]
            json.dumps(
                {
                    "success": False,
                    "ret_msg": "error:handler not found",
                    "op": "subscribe",
                }
            )
        )

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        with pytest.raises(FeedTransportError, match="handler not found"):
            await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        await stream.close()

    asyncio.run(Harness(handler).run(scenario))


def test_closed_connection_surfaces_transport_error() -> None:
    async def handler(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(json.dumps({"success": True, "op": "subscribe"}))  # type: ignore[attr-defined]
        await ws.close()  # type: ignore[attr-defined]

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        with pytest.raises(FeedTransportError):
            await stream.recv()
        await stream.close()

    asyncio.run(Harness(handler).run(scenario))


def test_application_ping_and_pong_keep_connection_alive() -> None:
    seen: list[dict] = []

    async def handler(ws: object) -> None:
        await ws.recv()  # type: ignore[attr-defined]
        await ws.send(json.dumps({"success": True, "op": "subscribe"}))  # type: ignore[attr-defined]
        message = json.loads(await ws.recv())  # type: ignore[attr-defined]
        seen.append(message)
        if message.get("op") == "ping":
            await ws.send(json.dumps({"op": "pong"}))  # type: ignore[attr-defined]
        await ws.wait_closed()  # type: ignore[attr-defined]

    async def scenario(url: str) -> None:
        stream = make_stream(
            url, ping_interval_seconds=0.05, ping_timeout_seconds=2.0
        )
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        await asyncio.sleep(0.2)
        await stream.close()
        assert seen and seen[0]["op"] == "ping"

    asyncio.run(Harness(handler).run(scenario))


def test_interval_mapping_round_trip() -> None:
    assert INTERVAL_BY_TIMEFRAME["1d"] == "D"
    assert TIMEFRAME_BY_INTERVAL["D"] == "1d"
    assert set(INTERVAL_BY_TIMEFRAME) == {"1m", "5m", "15m", "1h", "4h", "1d"}
