"""回放传输：把 JSONL 行情脚本按时间轴重放为 ``FeedTransport``。

JSONL 每行一个对象，``offset_ms`` 是相对回放起点的墙钟偏移：

- ``{"offset_ms": 0, "type": "kline", "event": {KlineEvent}}``
- ``{"offset_ms": 0, "type": "book", "event": {OrderBookEvent}}``
- ``{"offset_ms": 0, "type": "heartbeat"}``
- ``{"offset_ms": 8000, "type": "disconnect", "reason": "..."}``
- ``{"offset_ms": 12000, "type": "reconnect"}``

``disconnect`` 让 ``recv`` 抛出 ``FeedTransportError``，下一次 ``connect`` 会等到
``reconnect`` 偏移后才恢复；``reconnect`` 行本身不产生行情事件。回放结束后
保持静默心跳，便于长时间观察界面而不误判断流。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from talaria.foundation.errors import FeedTransportError, InvalidRequestError
from talaria.foundation.feed import KlineEvent, OrderBookEvent

SleepFn = Callable[[float], Awaitable[None]]

_ITEM_TYPES = frozenset({"kline", "book", "heartbeat", "disconnect", "reconnect"})


@dataclass(frozen=True, slots=True)
class _Line:
    offset_ms: int
    kind: str
    event: KlineEvent | OrderBookEvent | None = None
    reason: str = ""


def _parse_line(raw: Any, *, line_number: int) -> _Line:
    if not isinstance(raw, dict):
        raise InvalidRequestError(f"回放文件第 {line_number} 行不是 JSON 对象")
    offset_ms = raw.get("offset_ms")
    if not isinstance(offset_ms, int) or offset_ms < 0:
        raise InvalidRequestError(f"回放文件第 {line_number} 行缺少合法 offset_ms")
    kind = raw.get("type")
    if kind not in _ITEM_TYPES:
        raise InvalidRequestError(f"回放文件第 {line_number} 行 type 不合法：{kind!r}")
    event: KlineEvent | OrderBookEvent | None = None
    if kind == "kline":
        event = KlineEvent.model_validate(raw.get("event"))
    elif kind == "book":
        event = OrderBookEvent.model_validate(raw.get("event"))
    reason = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
    return _Line(offset_ms=offset_ms, kind=kind, event=event, reason=reason)


def read_replay_file(path: str | Path) -> list[_Line]:
    """读取并校验回放脚本；偏移必须非递减。"""

    lines: list[_Line] = []
    previous = -1
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, text in enumerate(handle, start=1):
            stripped = text.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise InvalidRequestError(
                    f"回放文件第 {line_number} 行不是合法 JSON：{exc.msg}"
                ) from exc
            item = _parse_line(raw, line_number=line_number)
            if item.offset_ms < previous:
                raise InvalidRequestError(f"回放文件第 {line_number} 行 offset_ms 逆序")
            previous = item.offset_ms
            lines.append(item)
    if not lines:
        raise InvalidRequestError("回放文件为空")
    return lines


class ReplayFeed:
    """按 ``offset_ms`` 指定的节奏输出事件的传输层。"""

    feed_mode = "replay"

    def __init__(
        self,
        path: str | Path,
        *,
        speed: float = 1.0,
        sleep: SleepFn = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        idle_heartbeat_seconds: float = 5.0,
    ) -> None:
        if speed <= 0:
            raise InvalidRequestError("回放速度必须为正数")
        self._lines = read_replay_file(path)
        self._index = 0
        self._speed = speed
        self._sleep = sleep
        self._clock = clock
        self._idle_heartbeat_seconds = idle_heartbeat_seconds
        self._started_at: float | None = None
        self._connected = False
        self._closed = asyncio.Event()
        self._subscriptions: set[tuple[str, str]] = set()

    # ------------------------------------------------------------- FeedTransport

    async def connect(self) -> None:
        if self._closed.is_set():
            raise FeedTransportError("回放已关闭")
        if self._started_at is None:
            self._started_at = self._clock()
            self._connected = True
            return
        while self._index < len(self._lines) and self._lines[self._index].kind == "reconnect":
            await self._wait_until(self._lines[self._index].offset_ms)
            self._index += 1
        self._connected = True

    async def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        self._subscriptions.add((instrument_id, timeframe))

    async def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        self._subscriptions.discard((instrument_id, timeframe))

    async def recv(self) -> KlineEvent | OrderBookEvent | None:
        if not self._connected:
            raise FeedTransportError("回放尚未连接")
        while self._index < len(self._lines):
            line = self._lines[self._index]
            self._index += 1
            if line.kind == "disconnect":
                await self._wait_until(line.offset_ms)
                self._connected = False
                raise FeedTransportError(line.reason or "回放注入断流")
            if line.kind == "reconnect":
                continue
            await self._wait_until(line.offset_ms)
            if line.kind == "heartbeat":
                return None
            event = line.event
            if isinstance(event, KlineEvent):
                if (event.instrument_id, event.timeframe) in self._subscriptions:
                    return event
                continue
            if isinstance(event, OrderBookEvent) and self._book_subscribed(
                event.instrument_id
            ):
                return event
            continue
        # 脚本结束：保持静默心跳，直到任务被取消或显式关闭。
        await self._sleep(self._idle_heartbeat_seconds / self._speed)
        if self._closed.is_set():
            raise FeedTransportError("回放已关闭")
        return None

    async def close(self) -> None:
        self._connected = False
        self._closed.set()

    # ------------------------------------------------------------- 内部

    async def _wait_until(self, offset_ms: int) -> None:
        assert self._started_at is not None
        target = self._started_at + offset_ms / 1000 / self._speed
        delay = target - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _book_subscribed(self, instrument_id: str) -> bool:
        return any(instrument == instrument_id for instrument, _ in self._subscriptions)


__all__ = ["ReplayFeed", "read_replay_file"]
