"""实时热端 K 线内存环：冻结快照时提供尚未归档的切片。

``FeedSupervisor`` 是行情状态机，不对外暴露原始柱；本缓冲以监听器方式订阅
``KlineEvent``（在 app 中接线），只保留每个 ``(instrument, timeframe)`` 最近的
柱及其不同时点可见的修订，并把系统接收/校验完成时刻记为
``Committed Time``。快照构建时与已归档历史一同按截止时点选版。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Protocol

from talaria.foundation.bars import BarStatus
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import FeedEvent, KlineEvent, ReceivedKlineEvent
from talaria.foundation.market import InstrumentSpec

DEFAULT_MAX_BARS_PER_SERIES = 5_000


class HotBarProvider(Protocol):
    """冻结快照时可注入的热端柱来源。"""

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> Sequence[RawBar]:
        """返回该通道最近柱的修订（按事件时间升序）。"""


class HotSlot:
    """可固定一个时间槽，后台读者在线程中复制版本；被热缓冲淘汰仍能完成本次读。"""

    def __init__(self) -> None:
        self._versions: dict[int, RawBar] = {}
        self._lock = threading.Lock()

    def add(self, bar: RawBar) -> None:
        with self._lock:
            self._versions.setdefault(bar.revision, bar)

    def revisions(self) -> list[RawBar]:
        with self._lock:
            bars = list(self._versions.values())
        return sorted(bars, key=lambda bar: bar.revision)


class HotStreamBuffer:
    """单进程内存环；线程安全，监听器与快照线程可并发调用。"""

    def __init__(
        self,
        *,
        instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int],
        max_bars_per_series: int = DEFAULT_MAX_BARS_PER_SERIES,
    ) -> None:
        if max_bars_per_series <= 0:
            raise ValueError("max_bars_per_series 必须为正数")
        self._symbols = {spec.instrument_id: spec.symbol for spec in instruments}
        self._now_ms = now_ms
        self._max_bars = max_bars_per_series
        self._series: dict[tuple[str, str], dict[int, HotSlot]] = {}
        self._lock = threading.Lock()

    def ingest(self, event: FeedEvent) -> RawBar | None:
        """订阅回调；只消化已闭合或进行中的普通柱，FEED_GAP 交由紧凑化重建。"""

        if not isinstance(event, KlineEvent):
            return
        bar = event.bar
        if bar.status is BarStatus.FEED_GAP:
            return
        if any(
            value is None
            for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover)
        ):
            return
        symbol = self._symbols.get(event.instrument_id)
        if symbol is None:
            return
        now = self._now_ms()
        received = event.ts_ingested if isinstance(event, ReceivedKlineEvent) else now
        raw = RawBar(
            symbol=symbol,
            timeframe=event.timeframe,
            ts_event=bar.timestamp,
            open=bar.open,  # type: ignore[arg-type]
            high=bar.high,  # type: ignore[arg-type]
            low=bar.low,  # type: ignore[arg-type]
            close=bar.close,  # type: ignore[arg-type]
            volume=bar.volume,  # type: ignore[arg-type]
            turnover=bar.turnover,  # type: ignore[arg-type]
            ts_available=bar.ts_available,
            ts_ingested=received,
            ts_committed=max(now, received),
            precision=bar.precision,
            revision=event.revision,
            confirmed=event.confirmed,
            history_only=isinstance(event, ReceivedKlineEvent) and event.history_only,
            synthetic=isinstance(event, ReceivedKlineEvent) and event.synthetic,
        )
        key = (event.instrument_id, event.timeframe)
        with self._lock:
            series = self._series.setdefault(key, {})
            # 保留最近收到更新的时间槽，迟到修订也能接续上一版。
            slot = series.pop(raw.ts_event, None) or HotSlot()
            slot.add(raw)
            series[raw.ts_event] = slot
            while len(series) > self._max_bars:
                series.pop(next(iter(series)))
        return raw

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> list[RawBar]:
        """快照中，已收盘时间槽的下一次修订从实际接收后才可见。"""
        versions = self.revisions(instrument_id=instrument_id, timeframe=timeframe)
        selected: dict[tuple[int, int, int], RawBar] = {}
        confirmed: set[int] = set()
        for bar in versions:
            if bar.ts_event in confirmed:
                if not bar.confirmed:
                    continue
                bar = replace(bar, ts_available=max(bar.ts_available, bar.ts_committed))
            key = (bar.ts_event, bar.ts_available, -1)
            if bar.confirmed:
                confirmed.add(bar.ts_event)
                selected.pop(key, None)
                key = (bar.ts_event, bar.ts_available, bar.revision)
            selected[key] = bar
        return [selected[key] for key in sorted(selected)]

    def slot(self, key: tuple[str, str], ts: int) -> HotSlot | None:
        with self._lock:
            return self._series.get(key, {}).get(ts)

    def revisions(
        self, *, instrument_id: str, timeframe: str, start_ts: int = 0, end_ts: int | None = None,
    ) -> list[RawBar]:
        """按事件时间与修订排序读取热缓冲中的完整发布版本。"""
        key = (instrument_id, timeframe)
        if start_ts == end_ts:
            slot = self.slot(key, start_ts)
            return [] if slot is None else slot.revisions()
        with self._lock:
            series = self._series.get(key, {}).copy()
        return [
            bar for ts in sorted(series) if start_ts <= ts and (end_ts is None or ts <= end_ts)
            for bar in series[ts].revisions()
        ]
