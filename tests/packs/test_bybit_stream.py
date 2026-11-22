"""Bybit V5 公共 WebSocket：使用本机假服务器验证协议行为，不访问外网。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from websockets.asyncio.server import serve

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.bars import BarStatus
from talaria.foundation.errors import FeedTransportError
from talaria.foundation.feed import DerivativeTickEvent, KlineEvent, OrderBookEvent
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


@pytest.mark.regression
@pytest.mark.parametrize(
    ("url", "proxy_var", "no_proxy", "destination"),
    [
        pytest.param(
            "wss://stream.bybit.com/v5/public/linear", "HTTPS_PROXY", None, "proxy.example",
            id="https-proxy",
        ),
        pytest.param(
            "ws://stream.bybit.com", "HTTP_PROXY", None, "proxy.example", id="http-proxy"
        ),
        pytest.param(
            "wss://stream.bybit.com", "HTTPS_PROXY", "stream.bybit.com", "stream.bybit.com",
            id="no-proxy",
        ),
        pytest.param(
            "ws://127.0.0.1", "HTTP_PROXY", None, "127.0.0.1", id="ipv4-loopback"
        ),
        pytest.param(
            "ws://localhost", "HTTP_PROXY", None, "localhost", id="localhost"
        ),
        pytest.param(
            "ws://[::1]", "HTTP_PROXY", None, "::1", id="ipv6-loopback"
        ),
    ],
)
def test_bybit_websocket_routes_connections_using_proxy_environment(
    monkeypatch, url, proxy_var, no_proxy, destination
) -> None:
    # platform/04: stop at the event loop's TCP connection for both direct and proxy paths.
    proxy_variables = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "WS_PROXY", "WSS_PROXY", "SOCKS_PROXY",
    )
    for name in proxy_variables:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.delenv("REQUEST_METHOD", raising=False)
    monkeypatch.setenv(proxy_var, "http://proxy.example:8080")
    if no_proxy is not None:
        monkeypatch.setenv("NO_PROXY", no_proxy)

    async def scenario() -> None:
        async def connect(_factory, host, port, **kwargs):
            assert host == destination
            raise ConnectionRefusedError("test TCP connection")

        monkeypatch.setattr(asyncio.get_running_loop(), "create_connection", connect)
        stream = make_stream(url)
        with pytest.raises(FeedTransportError, match="test TCP connection"):
            await stream.connect()

    asyncio.run(scenario())


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
        "data": [
            {
                "start": start,
                "end": start + MINUTE - 1,
                "interval": interval,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": volume,
                "turnover": "100",
                "confirm": confirm,
                "timestamp": start + 1_000,
            },
        ],
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


def tickers_payload(
    *,
    symbol: str = "BTCUSDT",
    mark_price: str = "101.5",
    funding_rate: str = "0.0001",
    next_funding_time: int = BASE + 8 * 3_600_000,
    open_interest: str = "12345.6",
) -> dict:
    return {
        "topic": f"tickers.{symbol}",
        "type": "snapshot",
        "ts": BASE + 500,
        "data": {
            "symbol": symbol,
            "markPrice": mark_price,
            "fundingRate": funding_rate,
            "nextFundingTime": str(next_funding_time),
            "openInterest": open_interest,
            "lastPrice": "101.4",
        },
    }


def test_subscribe_ack_then_kline_and_orderbook() -> None:
    received: list[dict] = []

    async def handler(ws: object) -> None:
        received.append(json.loads(await ws.recv()))  # type: ignore[attr-defined]
        await ws.send(json.dumps({"success": True, "ret_msg": "subscribe", "op": "subscribe"}))  # type: ignore[attr-defined]
        await ws.send(json.dumps(kline_payload()))  # type: ignore[attr-defined]
        await ws.send(json.dumps(book_payload()))  # type: ignore[attr-defined]
        await ws.send(json.dumps(tickers_payload()))  # type: ignore[attr-defined]
        await ws.wait_closed()  # type: ignore[attr-defined]

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        bar = await stream.recv()
        book = await stream.recv()
        tick = await stream.recv()
        await stream.close()

        assert received[0]["op"] == "subscribe"
        assert received[0]["args"] == [
            "kline.1.BTCUSDT",
            "orderbook.1.BTCUSDT",
            "tickers.BTCUSDT",
            "publicTrade.BTCUSDT",
        ]
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
        assert isinstance(tick, DerivativeTickEvent)
        assert tick.instrument_id == INSTRUMENT
        assert tick.mark_price == Decimal("101.5")
        assert tick.funding_rate == Decimal("0.0001")
        assert tick.next_funding_ts == BASE + 8 * 3_600_000
        assert tick.open_interest == Decimal("12345.6")

    asyncio.run(Harness(handler).run(scenario))


def test_public_trade_batches_and_l1_use_exchange_event_time(monkeypatch, tmp_path) -> None:
    # data-access/07: inject a WebSocket with matching-engine and wall-clock times.
    from talaria import sdk
    from talaria.perception.backfill import BackfillPolicy
    from talaria.perception.cold_publisher import ColdPublisher
    from talaria.perception.cold_store import ColdStore

    async def scenario() -> None:
        requests = asyncio.Queue()
        trade = {
            "s": "BTCUSDT", "T": BASE + 10, "S": "Buy", "p": "100.1", "v": "0.123",
            "i": "11111111-0000-0000-0000-000000000004", "seq": 82,
        }

        async def frames():
            await requests.get()
            yield json.dumps({"success": True, "op": "subscribe"})
            yield json.dumps({
                "topic": "publicTrade.BTCUSDT",
                "ts": BASE + 2000,
                "data": [trade, dict(trade, p="broken"), dict(
                    trade, S="Sell", i="11111111-0000-0000-0000-000000000002",
                )],
            })
            yield json.dumps({
                "topic": "publicTrade.BTCUSDT", "ts": BASE + 2000,
                "data": [dict(trade, i="11111111-0000-0000-0000-000000000003", seq=81)],
            })
            yield json.dumps({
                "topic": "publicTrade.BTCUSDT", "ts": BASE + 2002,
                "data": [dict(trade, i="11111111-0000-0000-0000-000000000001")],
            })
            yield json.dumps(dict(book_payload(), cts=BASE + 11, ts=BASE + 2001))

        ws = MagicMock(send=AsyncMock(side_effect=requests.put), close=AsyncMock())
        ws.__aiter__.side_effect = frames
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(return_value=ws))
        stream = make_stream("ws://unused", ping_interval_seconds=None, now_ms=lambda: BASE + 1000)
        await stream.connect()
        try:
            await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
            request = json.loads(ws.send.await_args.args[0])
            assert "publicTrade.BTCUSDT" in request["args"]
            first, second, earlier, tied, quote = [await asyncio.wait_for(stream.recv(), 1) for _ in range(5)]
            assert [(row.ts_event, row.ts_ingested, row.side, row.price, row.size) for row in (first, second)] == [
                (BASE + 10, BASE + 1000, "Buy", Decimal("100.1"), Decimal("0.123")),
                (BASE + 10, BASE + 1000, "Sell", Decimal("100.1"), Decimal("0.123")),
            ]
            assert first.trade_id != second.trade_id
            assert (quote.ts_event, quote.ts_ingested) == (BASE + 11, BASE + 1000)
            store = ColdStore(tmp_path)
            try:
                publisher = ColdPublisher(
                    store, instruments=CRYPTO_PERP_PACK.instruments(), connector=None,
                    policy=BackfillPolicy(), now_ms=lambda: BASE + 3000,
                )
                for row in (first, second, earlier, tied, quote):
                    publisher.ingest_ticks(row)
                await publisher.close()
            finally:
                store.close()
            assert not sdk.trades(INSTRUMENT, BASE, as_of=BASE + 1999, data_dir=tmp_path)
            trades = sdk.trades(INSTRUMENT, BASE, as_of=BASE + 2000, data_dir=tmp_path)
            assert {(row.ts_available, row.availability) for row in trades} == {(BASE + 2000, "measured")}
            assert [row.trade_id for row in trades] == [earlier.trade_id, first.trade_id, second.trade_id]
            assert [(row.sequence, row.packet_index, row.order_uncertain) for row in trades] == [
                (81, 0, False), (82, 0, False), (82, 2, False),
            ]
            ambiguous = sdk.trades(INSTRUMENT, BASE, as_of=BASE + 2002, data_dir=tmp_path)
            assert all(row.order_uncertain for row in ambiguous)
            ids = [row.trade_id for row in ambiguous]
            assert ids.index(first.trade_id) < ids.index(second.trade_id)
            assert not sdk.quotes(INSTRUMENT, BASE, as_of=BASE + 2000, data_dir=tmp_path)
            assert sdk.quotes(INSTRUMENT, BASE, as_of=BASE + 2001, data_dir=tmp_path)[0].ts_available == BASE + 2001
        finally:
            await stream.close()

    asyncio.run(scenario())


@pytest.mark.regression
def test_tickers_merge_deltas_per_instrument_and_skip_empty_readings(monkeypatch, caplog) -> None:
    # platform/04: a fake WebSocket sends Bybit V5 ticker frames without binding a port.
    def delta(topic_symbol: str, offset: int, **fields: str) -> dict:
        return {
            "topic": f"tickers.{topic_symbol}", "type": "delta", "ts": BASE + offset, "data": fields,
        }

    messages = [
        tickers_payload(),
        delta("ETHUSDT", 600, symbol="ETHUSDT", lastPrice="102"),
        delta("BTCUSDT", 700, lastPrice="102"),
        delta("BTCUSDT", 800, fundingRate="0"),
        delta("BTCUSDT", 900, openInterest="12346.7"),
        delta("BTCUSDT", 750, fundingRate="0.0002"),
        delta("BTCUSDT", 950, lastPrice="104"),
        {
            "topic": "tickers.BTCUSDT", "type": "snapshot", "ts": BASE + 1000,
            "data": {"symbol": "BTCUSDT", "markPrice": "103"},
        },
        delta("BTCUSDT", 1100, lastPrice="104"),
    ]

    async def scenario() -> list[DerivativeTickEvent]:
        requests: asyncio.Queue[str] = asyncio.Queue()

        async def frames():
            for _ in range(2):
                await requests.get()
                yield json.dumps({"success": True, "op": "subscribe"})
            for message in messages:
                yield json.dumps(message)

        ws = MagicMock(send=AsyncMock(side_effect=requests.put), close=AsyncMock())
        ws.__aiter__.side_effect = frames
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(return_value=ws))
        stream = make_stream("ws://unused", ping_interval_seconds=None)
        await stream.connect()
        events = []
        try:
            await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
            await stream.subscribe(instrument_id="ETH-USDT-PERP", timeframe="1m")
            with pytest.raises(FeedTransportError, match="连接已断开"):
                while True:
                    event = await asyncio.wait_for(stream.recv(), timeout=1)
                    assert isinstance(event, DerivativeTickEvent)
                    events.append(event)
        finally:
            await stream.close()
        return events

    events = asyncio.run(scenario())
    assert [(event.instrument_id, event.ts_event - BASE) for event in events] == [
        (INSTRUMENT, offset) for offset in (500, 700, 800, 900, 750, 950, 1000, 1100)
    ]
    assert events[1].mark_price == Decimal("101.5")
    assert events[1].funding_rate == Decimal("0.0001")
    assert events[3].funding_rate == Decimal("0")
    assert events[3].open_interest == Decimal("12346.7")
    assert events[3].next_funding_ts == BASE + 8 * 3_600_000
    assert events[4].mark_price == Decimal("101.5")
    assert events[4].open_interest is None
    assert events[4].funding_rate == Decimal("0.0002")
    assert events[5].funding_rate == Decimal("0")
    assert events[5].open_interest == Decimal("12346.7")
    latest = events[-1]
    assert latest.mark_price == Decimal("103")
    assert (latest.funding_rate, latest.open_interest, latest.next_funding_ts) == (None, None, None)
    assert not caplog.records


@pytest.mark.regression
def test_kline_without_symbol_in_data_uses_topic(monkeypatch) -> None:
    # 任务单 02：用假的 WebSocket 连接提供 Bybit V5 报文，无需绑定端口。
    async def scenario() -> None:
        ws = MagicMock(send=AsyncMock(), close=AsyncMock())
        ws.__aiter__.return_value = [
            json.dumps({"success": True, "op": "subscribe"}),
            json.dumps(kline_payload(confirm=True)),
        ]
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(return_value=ws))
        stream = make_stream("ws://unused", ping_interval_seconds=None)
        await stream.connect()
        try:
            await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
            event = await asyncio.wait_for(stream.recv(), timeout=1)
            assert isinstance(event, KlineEvent)
            assert (event.instrument_id, event.timeframe, event.confirmed) == (INSTRUMENT, "1m", True)
        finally:
            await stream.close()

    asyncio.run(scenario())


@pytest.mark.regression
def test_bybit_available_time_uses_parser_receipt_even_when_consumer_is_delayed(monkeypatch) -> None:
    # ADR-0018: use the parsed frame's time for both close and correction.
    async def scenario() -> None:
        end = BASE + MINUTE
        now = end + 5_000

        async def frames():
            nonlocal now
            yield json.dumps({"success": True, "op": "subscribe"})
            yield json.dumps(kline_payload(confirm=True))
            now = end + 8_000
            yield json.dumps(kline_payload(confirm=True, close="101"))
            now = end + 20_000

        ws = MagicMock(send=AsyncMock(), close=AsyncMock())
        ws.__aiter__.side_effect = frames
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(return_value=ws))
        stream = make_stream("ws://unused", now_ms=lambda: now, ping_interval_seconds=None)
        supervisor = FeedSupervisor(stream=stream, instruments=CRYPTO_PERP_PACK.instruments())
        supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        observed = []
        supervisor.add_listener(observed.append)
        await stream.connect()
        try:
            await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
            for _ in range(2):
                supervisor.ingest(await stream.recv(), now=now)
        finally:
            await stream.close()
        assert [event.bar.ts_available for event in observed] == [end, end + 8_000]

    asyncio.run(scenario())


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


def test_tickers_subscription_is_shared_across_timeframes() -> None:
    received: list[dict] = []

    async def handler(ws: object) -> None:
        while True:
            raw = await ws.recv()  # type: ignore[attr-defined]
            received.append(json.loads(raw))
            await ws.send(  # type: ignore[attr-defined]
                json.dumps({"success": True, "op": received[-1]["op"]})
            )

    async def scenario(url: str) -> None:
        stream = make_stream(url, ping_interval_seconds=None)
        await stream.connect()
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        await stream.subscribe(instrument_id=INSTRUMENT, timeframe="5m")
        assert received[1]["args"] == ["kline.5.BTCUSDT"]
        await stream.unsubscribe(instrument_id=INSTRUMENT, timeframe="1m")
        assert received[2]["args"] == ["kline.1.BTCUSDT"]
        await stream.unsubscribe(instrument_id=INSTRUMENT, timeframe="5m")
        assert received[3]["args"] == [
            "kline.5.BTCUSDT", "orderbook.1.BTCUSDT", "tickers.BTCUSDT", "publicTrade.BTCUSDT",
        ]
        await stream.close()

    try:
        asyncio.run(Harness(handler).run(scenario))
    finally:
        pass


@pytest.mark.regression
def test_supervisor_replays_all_topics_on_a_new_connection(monkeypatch) -> None:
    async def scenario() -> None:
        sockets = []
        requests = [[], []]
        subscribed = asyncio.Event()

        def socket(index):
            sent = asyncio.Queue()

            async def frames():
                for _ in range(2):
                    requests[index].append(json.loads(await sent.get())["args"])
                    yield json.dumps({"success": True, "op": "subscribe"})
                if index == 1:
                    subscribed.set()
                    await asyncio.Future()

            ws = MagicMock(send=AsyncMock(side_effect=sent.put), close=AsyncMock())
            ws.__aiter__.side_effect = frames
            return ws

        sockets.extend(socket(index) for index in range(2))
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(side_effect=sockets))
        supervisor = FeedSupervisor(
            stream=make_stream("ws://unused", ping_interval_seconds=None),
            instruments=CRYPTO_PERP_PACK.instruments(), backoff_initial_seconds=0,
        )
        supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
        supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="5m")
        await supervisor.start()
        try:
            await asyncio.wait_for(subscribed.wait(), 2)
            assert requests == [[
                ["kline.1.BTCUSDT", "orderbook.1.BTCUSDT", "tickers.BTCUSDT", "publicTrade.BTCUSDT"],
                ["kline.5.BTCUSDT"],
            ]] * 2
        finally:
            await supervisor.stop()

    asyncio.run(scenario())


@pytest.mark.regression
def test_l1_ignores_old_updates_and_rebuilds_from_reconnect_snapshot(monkeypatch, tmp_path) -> None:
    from talaria import sdk
    from talaria.perception.backfill import BackfillPolicy
    from talaria.perception.cold_publisher import ColdPublisher
    from talaria.perception.cold_store import ColdStore

    def book(update_id, bid, *, message_type="snapshot"):
        packet = book_payload(bid=bid)
        packet["data"]["u"] = update_id
        return dict(packet, type=message_type, cts=BASE + 10)

    async def scenario() -> None:
        def socket(*packets):
            subscribed = asyncio.Event()

            async def frames():
                await subscribed.wait()
                yield json.dumps({"success": True, "op": "subscribe"})
                for packet in packets:
                    yield json.dumps(packet)

            ws = MagicMock(send=AsyncMock(side_effect=lambda _: subscribed.set()), close=AsyncMock())
            ws.__aiter__.side_effect = frames
            return ws

        sockets = [
            socket(book(8, "99.0", message_type="delta"), book(10, "100.0"),
                   book(11, "100.1"), book(10, "99.1"), book(11, "99.2")),
            socket(book(12, "99.3", message_type="delta"), book(1, "99.4")),
        ]
        monkeypatch.setattr("talaria.packs.crypto_perp.stream.ws_connect", AsyncMock(side_effect=sockets))
        stream = make_stream("ws://unused", ping_interval_seconds=None, now_ms=lambda: BASE + 1000)
        store = ColdStore(tmp_path)
        publisher = ColdPublisher(
            store, instruments=CRYPTO_PERP_PACK.instruments(), connector=None,
            policy=BackfillPolicy(), now_ms=lambda: BASE + 1100,
        )
        groups = []
        try:
            for _ in sockets:
                await stream.connect()
                await stream.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
                updates = []
                with pytest.raises(FeedTransportError):
                    while True:
                        event = await asyncio.wait_for(stream.recv(), 1)
                        updates.append(event)
                        publisher.ingest_ticks(event)
                groups.append(updates)
        finally:
            await stream.close()
            await publisher.close()
            store.close()

        assert [[row.bid_price for row in group] for group in groups] == [
            [Decimal("100.0"), Decimal("100.1")], [Decimal("99.4")],
        ]
        rows = sdk.quotes(INSTRUMENT, BASE, as_of=BASE + 1100, data_dir=tmp_path)
        first_connection = [row for row in rows if row.connection_id == groups[0][0].connection_id]
        assert [(row.update_id, row.bid_price) for row in first_connection] == [
            (10, Decimal("100.0")), (11, Decimal("100.1")),
        ]
        reset, = [row for row in rows if row.connection_id == groups[1][0].connection_id]
        assert (reset.update_id, reset.bid_price) == (1, Decimal("99.4"))
        assert all(row.order_uncertain for row in rows)

    asyncio.run(scenario())
