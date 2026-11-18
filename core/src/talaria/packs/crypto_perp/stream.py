"""Bybit V5 Linear 公共 WebSocket 传输。

协议要点（https://bybit-exchange.github.io/docs/v5/ws/connect）：
- 端点 ``wss://stream.bybit.com/v5/public/linear``；
- K 线主题 ``kline.{interval}.{symbol}``，盘口主题 ``orderbook.1.{symbol}``；
- 订阅后等待 ``{"success": true, "op": "subscribe"}`` 应答，失败抛 ``FeedTransportError``；
- 应用层 ``{"op": "ping"}`` 保活，超时未收到任何消息视为断流；
- 断流由 Supervisor 统一做指数退避重连，本类只负责单条连接的建立与关闭。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from decimal import Decimal

from websockets.asyncio.client import ClientConnection
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from talaria.foundation.bars import CompactBar, classify_flat_bar
from talaria.foundation.errors import FeedTransportError
from talaria.foundation.feed import KlineEvent, OrderBookEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.types import PrecisionTier

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://stream.bybit.com/v5/public/linear"
#: Talaria 周期 → Bybit V5 K 线 interval。
INTERVAL_BY_TIMEFRAME = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "1h": "60",
    "4h": "240",
    "1d": "D",
}
TIMEFRAME_BY_INTERVAL = {interval: name for name, interval in INTERVAL_BY_TIMEFRAME.items()}

_ACK_OPS = frozenset({"subscribe", "unsubscribe"})
_LOST = object()


def _default_now_ms() -> int:
    return int(time.time() * 1000)


class BybitPublicStream:
    """单条公共 WS 连接；订阅、心跳与消息解析都在这里完成。"""

    feed_mode = "bybit"

    def __init__(
        self,
        *,
        instruments: Sequence[InstrumentSpec],
        ws_url: str = DEFAULT_WS_URL,
        now_ms: Callable[[], int] | None = None,
        ack_timeout_seconds: float = 5.0,
        ping_interval_seconds: float | None = 20.0,
        ping_timeout_seconds: float = 10.0,
    ) -> None:
        self._ws_url = ws_url
        self._symbol_by_instrument = {
            spec.instrument_id: spec.symbol for spec in instruments
        }
        self._instrument_by_symbol = {
            spec.symbol: spec.instrument_id for spec in instruments
        }
        self._now_ms = now_ms or _default_now_ms
        self._ack_timeout_seconds = ack_timeout_seconds
        self._ping_interval_seconds = ping_interval_seconds
        self._ping_timeout_seconds = ping_timeout_seconds

        self._ws: ClientConnection | None = None
        self._events: asyncio.Queue[object] = asyncio.Queue()
        self._acks: list[asyncio.Future[None]] = []
        self._subscriptions: set[tuple[str, str]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._pinger: asyncio.Task[None] | None = None
        self._last_message_at: float = 0.0
        self._closed = False

    # ------------------------------------------------------------- FeedTransport

    async def connect(self) -> None:
        if self._closed:
            raise FeedTransportError("Bybit 行情连接已关闭")
        if self._ws is not None:
            return
        try:
            self._ws = await ws_connect(
                self._ws_url,
                open_timeout=self._ack_timeout_seconds,
                ping_interval=None,
                proxy=None,
                max_queue=None,
            )
        except (OSError, WebSocketException, asyncio.TimeoutError) as exc:
            raise FeedTransportError(f"无法连接 Bybit 公共行情：{exc}") from exc
        self._last_message_at = time.monotonic()
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop(), name="talaria-bybit-read")
        if self._ping_interval_seconds is not None:
            self._pinger = asyncio.create_task(self._ping_loop(), name="talaria-bybit-ping")

    async def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        symbol = self._symbol_by_instrument.get(instrument_id)
        if symbol is None:
            raise FeedTransportError(f"未知标的：{instrument_id}")
        interval = INTERVAL_BY_TIMEFRAME.get(timeframe)
        if interval is None:
            raise FeedTransportError(f"不支持的周期：{timeframe}")
        topics = [f"kline.{interval}.{symbol}", f"orderbook.1.{symbol}"]
        self._subscriptions.add((instrument_id, timeframe))
        try:
            await self._request({"op": "subscribe", "args": topics})
        except FeedTransportError:
            self._subscriptions.discard((instrument_id, timeframe))
            raise

    async def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        symbol = self._symbol_by_instrument.get(instrument_id)
        interval = INTERVAL_BY_TIMEFRAME.get(timeframe)
        if symbol is None or interval is None:
            return
        self._subscriptions.discard((instrument_id, timeframe))
        topics = [f"kline.{interval}.{symbol}"]
        if not any(inst == instrument_id for inst, _ in self._subscriptions):
            topics.append(f"orderbook.1.{symbol}")
        with contextlib.suppress(FeedTransportError):
            await self._request({"op": "unsubscribe", "args": topics})

    async def recv(self) -> KlineEvent | OrderBookEvent | None:
        item = await self._events.get()
        if item is _LOST:
            raise FeedTransportError("Bybit 公共行情连接已断开")
        return item  # type: ignore[return-value]

    async def close(self) -> None:
        self._closed = True
        await self._teardown()

    # ------------------------------------------------------------- 内部

    async def _request(self, payload: dict[str, object]) -> None:
        ws = self._ws
        if ws is None:
            raise FeedTransportError("Bybit 公共行情尚未连接")
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._acks.append(future)
        try:
            await ws.send(json.dumps(payload))
        except (OSError, WebSocketException) as exc:
            self._acks.remove(future)
            raise FeedTransportError(f"订阅请求发送失败：{exc}") from exc
        try:
            await asyncio.wait_for(future, timeout=self._ack_timeout_seconds)
        except TimeoutError as exc:
            raise FeedTransportError("等待 Bybit 订阅应答超时") from exc
        finally:
            if future in self._acks:
                self._acks.remove(future)

    async def _read_loop(self) -> None:
        ws = self._ws
        assert ws is not None
        try:
            async for raw in ws:
                self._last_message_at = time.monotonic()
                try:
                    payload = json.loads(raw)
                except (TypeError, json.JSONDecodeError):
                    logger.warning("忽略无法解析的 Bybit 消息")
                    continue
                if not isinstance(payload, dict):
                    continue
                op = payload.get("op")
                if op in _ACK_OPS:
                    self._resolve_ack(payload)
                    continue
                if op in {"ping", "pong"}:
                    if op == "ping":
                        await ws.send(json.dumps({"op": "pong"}))
                    continue
                for event in self._parse(payload):
                    await self._events.put(event)
            if not self._closed:
                # 正常关闭握手时迭代会直接结束，同样视为断流。
                await self._events.put(_LOST)
        except asyncio.CancelledError:
            raise
        except (ConnectionClosed, OSError) as exc:
            if not self._closed:
                logger.warning("Bybit 公共行情断开：%s", exc)
                await self._events.put(_LOST)
        except Exception:  # pragma: no cover - 防御性兜底
            logger.exception("Bybit 消息循环异常")
            await self._events.put(_LOST)
        finally:
            await self._teardown()

    async def _ping_loop(self) -> None:
        assert self._ping_interval_seconds is not None
        try:
            while True:
                await asyncio.sleep(self._ping_interval_seconds)
                ws = self._ws
                if ws is None:
                    return
                if time.monotonic() - self._last_message_at > self._ping_interval_seconds + self._ping_timeout_seconds:
                    logger.warning("Bybit 心跳超时，主动断开连接")
                    await ws.close()
                    return
                await ws.send(json.dumps({"op": "ping"}))
        except asyncio.CancelledError:
            raise
        except (OSError, WebSocketException):
            return

    async def _teardown(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (self._reader, self._pinger)
            if task is not None and task is not current
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._reader = self._pinger = None
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        for future in self._acks:
            if not future.done():
                future.set_exception(FeedTransportError("订阅应答等待被中断"))
        self._acks.clear()

    def _resolve_ack(self, payload: dict[str, object]) -> None:
        if not self._acks:
            return
        future = self._acks.pop(0)
        if future.done():
            return
        if payload.get("success") is False:
            message = payload.get("ret_msg") or "订阅被 Bybit 拒绝"
            future.set_exception(FeedTransportError(str(message)))
        else:
            future.set_result(None)

    # ------------------------------------------------------------- 解析

    def _parse(self, payload: dict[str, object]) -> list[KlineEvent | OrderBookEvent]:
        topic = payload.get("topic")
        if not isinstance(topic, str):
            return []
        data = payload.get("data")
        if isinstance(data, list):
            events: list[KlineEvent | OrderBookEvent] = []
            for item in data:
                events.extend(self._parse_item(topic, item, payload))
            return events
        if isinstance(data, dict):
            return self._parse_item(topic, data, payload)
        return []

    def _parse_item(
        self,
        topic: str,
        data: object,
        envelope: dict[str, object],
    ) -> list[KlineEvent | OrderBookEvent]:
        if not isinstance(data, dict):
            return []
        if topic.startswith("kline."):
            return self._parse_kline(data)
        if topic.startswith("orderbook."):
            return self._parse_book(data, envelope)
        return []

    def _parse_kline(self, data: dict[str, object]) -> list[KlineEvent]:
        interval = data.get("interval")
        symbol = data.get("symbol")
        if not isinstance(interval, str) or not isinstance(symbol, str):
            return []
        timeframe = TIMEFRAME_BY_INTERVAL.get(interval)
        instrument_id = self._instrument_by_symbol.get(symbol)
        if timeframe is None or instrument_id is None:
            return []
        if (instrument_id, timeframe) not in self._subscriptions:
            return []
        try:
            start = int(data["start"])
            end = int(data["end"])
            open_ = _decimal(data["open"])
            high = _decimal(data["high"])
            low = _decimal(data["low"])
            close = _decimal(data["close"])
            volume = _decimal(data["volume"])
            turnover = _decimal(data["turnover"])
        except (KeyError, TypeError, ValueError):
            logger.warning("忽略字段不全的 Bybit K 线：%s", data.get("symbol"))
            return []
        status = classify_flat_bar(
            open=open_, high=high, low=low, close=close, volume=volume
        )
        bar = CompactBar(
            timestamp=start,
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            turnover=turnover,
            status=status,
            ts_available=end,
            precision=PrecisionTier.SECOND_VERIFIED,
        )
        return [
            KlineEvent(
                instrument_id=instrument_id,
                timeframe=timeframe,
                bar=bar,
                confirmed=bool(data.get("confirm")),
                revision=0,
            )
        ]

    def _parse_book(
        self,
        data: dict[str, object],
        envelope: dict[str, object],
    ) -> list[OrderBookEvent]:
        symbol = data.get("s")
        if not isinstance(symbol, str):
            return []
        instrument_id = self._instrument_by_symbol.get(symbol)
        if instrument_id is None:
            return []
        if not any(inst == instrument_id for inst, _ in self._subscriptions):
            return []
        bids = data.get("b") or []
        asks = data.get("a") or []
        if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
            return []
        try:
            bid_price, bid_size = bids[0][0], bids[0][1]
            ask_price, ask_size = asks[0][0], asks[0][1]
            ts_event = int(data.get("ts") or envelope.get("ts") or self._now_ms())
            event = OrderBookEvent(
                instrument_id=instrument_id,
                bid_price=_decimal(bid_price),
                bid_size=_decimal(bid_size),
                ask_price=_decimal(ask_price),
                ask_size=_decimal(ask_size),
                ts_event=ts_event,
                ts_ingested=self._now_ms(),
            )
        except (IndexError, TypeError, ValueError) as exc:
            logger.warning("忽略无法解析的 Bybit 盘口：%s", exc)
            return []
        return [event]


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


__all__ = [
    "DEFAULT_WS_URL",
    "INTERVAL_BY_TIMEFRAME",
    "TIMEFRAME_BY_INTERVAL",
    "BybitPublicStream",
]
