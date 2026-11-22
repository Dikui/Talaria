"""Feed Supervisor：把实时传输事件整理成可复用的行情事件流。

职责边界（docs/architecture.md §3.2）：
- 只转发当前订阅的标的；Watchlist 通过 ``subscribe`` / ``unsubscribe`` 动态调度；
- 同一 timestamp 的每次内容变化递增 ``revision``，按 timestamp 去重、后写者胜；
- 供流窗口内无成交补齐 ``NO_TRADES``（前收平盘零量），传输失联补齐 ``FEED_GAP``；
- 传输失联把通道置为 ``STALE``，重连后 ``RECOVERING`` → 回填 → ``LIVE``；
- 所有判定使用注入时钟，测试与回放不需要真实等待。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal

from talaria.foundation.bars import BarStatus, CompactBar, classify_flat_bar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.errors import (
    FeedTransportError,
    InstrumentNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedEvent,
    FeedHealthEvent,
    FeedHealthReport,
    FeedHealthStatus,
    FeedListener,
    FeedTransport,
    KlineEvent,
    MarketEventFeed,
    OrderBookEvent,
    ReceivedKlineEvent,
    TradeEvent,
    TransportEvent,
    worst_status,
)
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import PrecisionTier

logger = logging.getLogger(__name__)

DEFAULT_STALE_AFTER_MS = 12_000
BAR_CLOSE_GRACE_MS = 5_000
TICK_INTERVAL_SECONDS = 0.5
BACKOFF_INITIAL_SECONDS = 0.2
BACKOFF_MAX_SECONDS = 5.0
#: 回填时最多向前扫描多少根柱，避免异常数据撑爆内存。
MAX_BACKFILL_BARS = 1000


def _default_now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(slots=True)
class _BarVersion:
    bar: CompactBar
    confirmed: bool
    revision: int
    source_confirmed: bool
    received_ts: int


@dataclass(slots=True)
class _Channel:
    instrument_id: str
    timeframe: str
    subscribed_ts: int
    status: FeedHealthStatus = FeedHealthStatus.LIVE
    last_event_ts: int | None = None
    last_activity_ts: int | None = None
    stale_since: int | None = None
    reconnect_count: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.instrument_id, self.timeframe)


@dataclass(slots=True)
class _SubscriptionOp:
    kind: str
    instrument_id: str
    timeframe: str


@dataclass(slots=True)
class _Inbound:
    kind: str
    payload: object | None = None
    received_ts: int | None = None


class FeedSupervisor(MarketEventFeed):
    """单连接的行情状态机；一个 supervisor 服务多个订阅通道。"""

    def __init__(
        self,
        *,
        stream: FeedTransport,
        connector: MarketDataConnector | None = None,
        instruments: Sequence[InstrumentSpec],
        feed_mode: str | None = None,
        now_ms: Callable[[], int] | None = None,
        stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
        on_subscribe: Callable[[InstrumentSpec, str, int], None] | None = None,
        on_bar_close: Callable[[InstrumentSpec, str, CompactBar, int, int], None] | None = None,
        on_recovered_bar: Callable[[InstrumentSpec, RawBar], None] | None = None,
        tick_interval_seconds: float = TICK_INTERVAL_SECONDS,
        backoff_initial_seconds: float = BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = BACKOFF_MAX_SECONDS,
    ) -> None:
        if stale_after_ms <= 0:
            raise ValueError("stale_after_ms 必须为正数")
        self._stream = stream
        self._connector = connector
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self.feed_mode = feed_mode or getattr(stream, "feed_mode", "unknown")
        self._now_ms = now_ms or _default_now_ms
        self._stale_after_ms = stale_after_ms
        self._on_subscribe = on_subscribe
        self._on_bar_close = on_bar_close
        self._on_recovered_bar = on_recovered_bar
        self._tick_interval_seconds = tick_interval_seconds
        self._backoff_initial_seconds = backoff_initial_seconds
        self._backoff_max_seconds = backoff_max_seconds

        self._channels: dict[tuple[str, str], _Channel] = {}
        self._series: dict[tuple[str, str], dict[int, _BarVersion]] = {}
        self._outage_lost_at: int | None = None
        self._outage_anchors: dict[tuple[str, str], int] = {}
        self._outage_windows: dict[tuple[str, str], list[tuple[int, int]]] = {}
        self._resume_pending: set[tuple[str, str]] = set()
        self._listeners: list[FeedListener] = []

        self._ops: asyncio.Queue[_SubscriptionOp] = asyncio.Queue()
        self._inbound: asyncio.Queue[_Inbound] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    # ------------------------------------------------------------------ 订阅

    def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        if instrument_id not in self._instruments:
            raise InstrumentNotFoundError(f"未知标的：{instrument_id}")
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise InvalidRequestError(f"不支持的周期：{timeframe}")
        key = (instrument_id, timeframe)
        if key in self._channels:
            return
        now = self._now_ms()
        channel = _Channel(
            instrument_id=instrument_id,
            timeframe=timeframe,
            subscribed_ts=now,
            last_activity_ts=now,
        )
        self._channels[key] = channel
        self._series.setdefault(key, {})
        self._ops.put_nowait(_SubscriptionOp("subscribe", instrument_id, timeframe))
        self._emit_health(channel, now)
        if self._on_subscribe is not None:
            self._on_subscribe(self._instruments[instrument_id], timeframe, now)

    def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        if self._channels.pop((instrument_id, timeframe), None) is None:
            return
        self._ops.put_nowait(_SubscriptionOp("unsubscribe", instrument_id, timeframe))

    def add_listener(self, listener: FeedListener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # ------------------------------------------------------------------ 状态机

    def ingest(
        self,
        event: TransportEvent,
        *,
        now: int | None = None,
    ) -> None:
        now = self._now_ms() if now is None else now
        if isinstance(event, KlineEvent):
            self._accept_kline(event, now)
        else:
            self._accept_instrument_event(event, now)

    def heartbeat(self, *, now: int | None = None) -> None:
        """传输层心跳：只刷新活跃度，不产生行情事件。"""
        now = self._now_ms() if now is None else now
        for channel in self._channels.values():
            channel.last_activity_ts = max(channel.last_activity_ts or now, now)
            if channel.status is FeedHealthStatus.STALE:
                channel.status = FeedHealthStatus.LIVE
                channel.stale_since = None
                self._emit_health(channel, now)

    def transport_lost(self, *, now: int | None = None) -> None:
        now = self._now_ms() if now is None else now
        if not self._channels:
            return
        if self._outage_lost_at is None:
            self._outage_lost_at = now
        for channel in self._channels.values():
            series = self._series.get(channel.key, {})
            if series:
                # 断流锚点用事件时间（最后一根已知柱），离线回放的历史数据也能对齐。
                self._outage_anchors.setdefault(channel.key, max(series))
            self._resume_pending.add(channel.key)
            if channel.status is FeedHealthStatus.STALE:
                continue
            channel.status = FeedHealthStatus.STALE
            channel.stale_since = channel.last_activity_ts or now
            self._emit_health(channel, now)
            logger.warning("行情断流：%s %s", channel.instrument_id, channel.timeframe)

    def transport_restored(
        self,
        *,
        now: int | None = None,
        backfill: Sequence[RawBar] = (),
    ) -> None:
        now = self._now_ms() if now is None else now
        if self._outage_lost_at is not None:
            elapsed = max(now - self._outage_lost_at, 0)
            for key, anchor in self._outage_anchors.items():
                self._outage_windows.setdefault(key, []).append((anchor, anchor + elapsed))
            self._outage_anchors.clear()
            self._outage_lost_at = None

        for channel in self._channels.values():
            channel.status = FeedHealthStatus.RECOVERING
            channel.reconnect_count += 1
            self._emit_health(channel, now)

        for bar in backfill:
            key = self._key_for_symbol(bar.symbol, bar.timeframe)
            if key is None:
                continue
            record = self._series[key].get(bar.ts_event)
            if record is not None and record.source_confirmed:
                continue
            status = classify_flat_bar(
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            self._apply_bar(
                key, bar.to_compact(status=status), confirmed=True, now=now, source=True,
                received_ts=bar.ts_ingested, recovered=True,
            )
            if self._on_recovered_bar is not None:
                revision = self._series[key][bar.ts_event].revision
                self._on_recovered_bar(self._instruments[key[0]], replace(bar, revision=revision))

        for channel in self._channels.values():
            self._fill_outage_windows(channel, now)
            channel.status = FeedHealthStatus.LIVE
            channel.stale_since = None
            channel.last_activity_ts = now
            self._emit_health(channel, now)

    def tick(self, *, now: int | None = None) -> None:
        now = self._now_ms() if now is None else now
        for channel in self._channels.values():
            if channel.status is not FeedHealthStatus.LIVE:
                continue
            since = channel.last_activity_ts or channel.subscribed_ts
            if now - since >= self._stale_after_ms:
                channel.status = FeedHealthStatus.STALE
                channel.stale_since = since
                self._emit_health(channel, now)

    def health(self) -> FeedHealthReport:
        now = self._now_ms()
        channels = [self._health_event(channel, now) for channel in self._channels.values()]
        return FeedHealthReport(
            feed_mode=self.feed_mode,
            status=worst_status([channel.status for channel in channels]),
            channels=channels,
        )

    def has_channel(self, instrument_id: str, timeframe: str) -> bool:
        return (instrument_id, timeframe) in self._channels

    def channel_health(
        self, instrument_id: str, timeframe: str
    ) -> FeedHealthEvent | None:
        """当前订阅通道的健康快照；新连接复用一个已存在通道时用于回放状态。"""
        channel = self._channels.get((instrument_id, timeframe))
        if channel is None:
            return None
        return self._health_event(channel, self._now_ms())

    def recent_bars(
        self,
        *,
        instrument_id: str,
        timeframe: str,
        after_ts: int | None = None,
        confirmed_only: bool = False,
    ) -> list[CompactBar]:
        """返回当前订阅通道内按 timestamp 去重后的最新柱（可按时间截断）。

        只读快照，不改变状态机；供指标查询在冻结快照之后追加实时尾段。
        """
        series = self._series.get((instrument_id, timeframe), {})
        return [
            series[timestamp].bar
            for timestamp in sorted(series)
            if (after_ts is None or timestamp > after_ts)
            and (not confirmed_only or series[timestamp].confirmed)
        ]

    # ------------------------------------------------------------------ 事件整理

    def _accept_kline(self, event: KlineEvent, now: int) -> None:
        key = (event.instrument_id, event.timeframe)
        channel = self._channels.get(key)
        if channel is None:
            return
        series = self._series[key]
        previous_ts = max(series) if series else None
        if previous_ts is not None and event.bar.timestamp > previous_ts:
            self._fill_missing(
                key,
                start=previous_ts + timeframe_ms(channel.timeframe),
                stop=event.bar.timestamp,
                now=now,
                force_gap=key in self._resume_pending,
            )
        self._resume_pending.discard(key)
        self._apply_bar(
            key, event.bar, confirmed=event.confirmed, now=now, source=True,
            received_ts=event.ts_ingested if isinstance(event, ReceivedKlineEvent) else now,
        )

    def _accept_instrument_event(self, event: OrderBookEvent | DerivativeTickEvent | TradeEvent, now: int) -> None:
        if not any(
            channel.instrument_id == event.instrument_id
            for channel in self._channels.values()
        ):
            return
        self._mark_activity(
            event.instrument_id, now, timeframe=None,
            event_ts=event.ts_event if isinstance(event, DerivativeTickEvent) else None,
        )
        self._emit(event)

    def _apply_bar(
        self,
        key: tuple[str, str],
        bar: CompactBar,
        *,
        confirmed: bool,
        now: int,
        source: bool = False,
        received_ts: int | None = None,
        recovered: bool = False,
    ) -> None:
        series = self._series.setdefault(key, {})
        record = series.get(bar.timestamp)
        received = now if received_ts is None else received_ts
        # 交易所也可能确认零量柱，合成柱通过来源标记区分。
        source_confirmed = source and confirmed and not recovered and bar.status is not BarStatus.FEED_GAP
        if recovered or source_confirmed:
            close_ts = bar.timestamp + timeframe_ms(key[1])
            if recovered:
                available = close_ts
            elif record is not None and record.source_confirmed:
                previous = record.bar.ts_available
                unchanged = bar.model_copy(update={"ts_available": previous}) == record.bar
                available = previous if unchanged else max(previous, received)
            else:
                available = close_ts if received <= close_ts + BAR_CLOSE_GRACE_MS else received
            bar = bar.model_copy(update={"ts_available": available})
        same_version = False
        if record is not None:
            if record.source_confirmed and not confirmed:
                return  # 已收盘的柱不能退回进行中
            same_kind = record.confirmed == confirmed and record.source_confirmed == source_confirmed
            same_version = same_kind and record.bar == bar
        revision = 0 if record is None else record.revision + 1
        if same_version:
            assert record is not None
            revision = record.revision
            received = record.received_ts
        if not same_version:
            series[bar.timestamp] = _BarVersion(
                bar=bar, confirmed=confirmed, revision=revision, source_confirmed=source_confirmed,
                received_ts=received,
            )
            self._mark_activity(key[0], now, timeframe=key[1], event_ts=now)
            self._emit(ReceivedKlineEvent(
                instrument_id=key[0],
                timeframe=key[1],
                bar=bar,
                confirmed=confirmed,
                revision=revision,
                ts_ingested=received,
                history_only=recovered,
                synthetic=not source,
            ))
        if source_confirmed and self._on_bar_close is not None:
            # 热端先接收发布，重发仍可补一次失败的写入。
            self._on_bar_close(self._instruments[key[0]], key[1], bar, received, revision)

    def _fill_missing(
        self,
        key: tuple[str, str],
        *,
        start: int,
        stop: int,
        now: int,
        force_gap: bool = False,
    ) -> None:
        channel = self._channels[key]
        interval = timeframe_ms(channel.timeframe)
        series = self._series.setdefault(key, {})
        timestamp = start
        while timestamp < stop:
            if timestamp + interval <= now and timestamp not in series:
                if force_gap:
                    self._synthesize_feed_gap(key, timestamp, interval, now)
                else:
                    self._synthesize_no_trades(key, timestamp, interval, now)
            timestamp += interval

    def _fill_outage_windows(self, channel: _Channel, now: int) -> None:
        """补断流窗口内缺失的柱；窗口按事件时间记录，离线回放与实盘同一口径。"""

        window_list = self._outage_windows.get(channel.key, [])
        if not window_list:
            return
        interval = timeframe_ms(channel.timeframe)
        series = self._series.setdefault(channel.key, {})
        for started, ended in window_list:
            timestamp = started + interval
            while timestamp <= ended:
                if timestamp + interval <= now and timestamp not in series:
                    self._synthesize_feed_gap(channel.key, timestamp, interval, now)
                timestamp += interval

    def _synthesize_feed_gap(
        self, key: tuple[str, str], timestamp: int, interval: int, now: int
    ) -> None:
        self._apply_bar(
            key,
            CompactBar(
                timestamp=timestamp,
                open=None,
                high=None,
                low=None,
                close=None,
                volume=None,
                turnover=None,
                status=BarStatus.FEED_GAP,
                ts_available=timestamp + interval,
                precision=PrecisionTier.SECOND_VERIFIED,
            ),
            confirmed=True,
            now=now,
        )

    def _synthesize_no_trades(
        self, key: tuple[str, str], timestamp: int, interval: int, now: int
    ) -> None:
        previous_close = self._previous_close(key, timestamp)
        if previous_close is None:
            return
        self._apply_bar(
            key,
            CompactBar(
                timestamp=timestamp,
                open=previous_close,
                high=previous_close,
                low=previous_close,
                close=previous_close,
                volume=Decimal(0),
                turnover=Decimal(0),
                status=BarStatus.NO_TRADES,
                ts_available=timestamp + interval,
                precision=PrecisionTier.SECOND_VERIFIED,
            ),
            confirmed=True,
            now=now,
        )

    def _previous_close(self, key: tuple[str, str], timestamp: int) -> Decimal | None:
        series = self._series.get(key, {})
        for candidate in sorted(series, reverse=True):
            if candidate >= timestamp:
                continue
            close = series[candidate].bar.close
            if close is not None:
                return close
        return None

    def _key_for_symbol(self, symbol: str, timeframe: str) -> tuple[str, str] | None:
        for instrument_id, spec in self._instruments.items():
            key = (instrument_id, timeframe)
            if spec.symbol == symbol and key in self._channels:
                return key
        return None

    def _mark_activity(
        self,
        instrument_id: str,
        now: int,
        *,
        timeframe: str | None,
        event_ts: int | None = None,
    ) -> None:
        for channel in self._channels.values():
            if channel.instrument_id != instrument_id:
                continue
            if timeframe is not None and channel.timeframe != timeframe:
                continue
            channel.last_activity_ts = now
            channel.last_event_ts = now if event_ts is None else event_ts
            if channel.status is FeedHealthStatus.STALE:
                channel.status = FeedHealthStatus.LIVE
                channel.stale_since = None
                self._emit_health(channel, now)

    # ------------------------------------------------------------------ 输出

    def _health_event(self, channel: _Channel, now: int) -> FeedHealthEvent:
        return FeedHealthEvent(
            instrument_id=channel.instrument_id,
            timeframe=channel.timeframe,
            status=channel.status,
            ts=now,
            last_event_ts=channel.last_event_ts,
            stale_since=channel.stale_since,
            reconnect_count=channel.reconnect_count,
        )

    def _emit_health(self, channel: _Channel, now: int) -> None:
        self._emit(self._health_event(channel, now))

    def _emit(self, event: FeedEvent) -> None:
        for listener in list(self._listeners):
            listener(event)

    # ------------------------------------------------------------------ 运行循环

    async def start(self) -> None:
        """建立连接并启动消费、心跳与重连循环；可安全重复调用。"""
        if self._tasks:
            return
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._serve(), name="talaria-feed-serve"),
            asyncio.create_task(self._tick_loop(), name="talaria-feed-tick"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []
        await self._stream.close()

    async def _serve(self) -> None:
        backoff = self._backoff_initial_seconds
        backfill: Sequence[RawBar] = ()
        while not self._stop.is_set():
            try:
                # 上一条连接剩余的事件和订阅操作已过期，订阅以通道集合为准。
                self._inbound = asyncio.Queue()
                await self._stream.connect()
                await self._resubscribe_all()
                backoff = self._backoff_initial_seconds
                if self._outage_lost_at is not None:
                    # 只有真正重连成功后才进入 RECOVERING 并回填，避免故障期间误报 LIVE。
                    self.transport_restored(now=self._now_ms(), backfill=backfill)
                    backfill = ()
                pumps = [
                    asyncio.create_task(self._pump_stream(), name="talaria-feed-stream"),
                    asyncio.create_task(self._pump_ops(), name="talaria-feed-ops"),
                ]
                try:
                    await self._consume()
                finally:
                    for pump in pumps:
                        pump.cancel()
                    for pump in pumps:
                        with contextlib.suppress(asyncio.CancelledError):
                            await pump
            except FeedTransportError as exc:
                logger.warning("实时流断开：%s", exc)
                now = self._now_ms()
                self.transport_lost(now=now)
                backfill = await asyncio.to_thread(self._fetch_backfill, now)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 防御性兜底，避免循环静默退出
                logger.exception("实时流循环异常")
            await self._sleep_or_stop(backoff)
            backoff = min(backoff * 2, self._backoff_max_seconds)
        await self._stream.close()

    async def _consume(self) -> None:
        while not self._stop.is_set():
            inbound = await self._inbound.get()
            if inbound.kind == "lost":
                raise inbound.payload  # type: ignore[misc]
            if inbound.kind == "heartbeat":
                self.heartbeat(now=self._now_ms())
                continue
            if inbound.kind == "op":
                await self._apply_op(inbound.payload)  # type: ignore[arg-type]
                continue
            self.ingest(inbound.payload, now=inbound.received_ts)  # type: ignore[arg-type]

    async def _pump_stream(self) -> None:
        while True:
            try:
                event = await self._stream.recv()
            except FeedTransportError as exc:
                await self._inbound.put(_Inbound("lost", exc))
                return
            if event is None:
                await self._inbound.put(_Inbound("heartbeat"))
            else:
                await self._inbound.put(_Inbound("event", event, self._now_ms()))

    async def _pump_ops(self) -> None:
        while True:
            op = await self._ops.get()
            await self._inbound.put(_Inbound("op", op))

    async def _resubscribe_all(self) -> None:
        """新连接建立后按当前通道集合重发订阅，并丢弃连接期间排队的旧操作。"""

        while True:
            try:
                self._ops.get_nowait()
            except asyncio.QueueEmpty:
                break
        for channel in list(self._channels.values()):
            await self._stream.subscribe(
                instrument_id=channel.instrument_id, timeframe=channel.timeframe
            )

    async def _apply_op(self, op: _SubscriptionOp) -> None:
        if op.kind == "subscribe":
            await self._stream.subscribe(
                instrument_id=op.instrument_id, timeframe=op.timeframe
            )
        else:
            await self._stream.unsubscribe(
                instrument_id=op.instrument_id, timeframe=op.timeframe
            )

    async def _tick_loop(self) -> None:
        while not self._stop.is_set():
            await self._sleep_or_stop(self._tick_interval_seconds)
            self.tick(now=self._now_ms())

    async def _sleep_or_stop(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    def _fetch_backfill(self, now: int) -> list[RawBar]:
        if self._connector is None:
            return []
        bars: list[RawBar] = []
        for channel in list(self._channels.values()):
            spec = self._instruments[channel.instrument_id]
            series = self._series.get(channel.key, {})
            last_ts = max(series) if series else None
            try:
                fetched = self._connector.fetch_bars(
                    symbol=spec.symbol,
                    timeframe=channel.timeframe,
                    limit=MAX_BACKFILL_BARS,
                )
            except Exception:
                logger.exception("重连回填失败：%s %s", spec.symbol, channel.timeframe)
                continue
            if last_ts is not None:
                fetched = [bar for bar in fetched if bar.ts_event >= last_ts]
            bars.extend(fetched)
        return bars
