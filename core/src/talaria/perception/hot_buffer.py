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
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.market import InstrumentSpec

DEFAULT_MAX_BARS_PER_SERIES = 5_000


class HotBarProvider(Protocol):
    """冻结快照时可注入的热端柱来源。"""

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> Sequence[RawBar]:
        """返回该通道最近柱的修订（按事件时间升序）。"""


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
        self._series: dict[
            tuple[str, str], dict[int, dict[tuple[int, int], RawBar]]
        ] = {}
        self._confirmed: dict[tuple[str, str], set[int]] = {}
        self._lock = threading.Lock()

    def ingest(self, event: FeedEvent) -> None:
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
            ts_ingested=now,
            ts_committed=now,
            precision=bar.precision,
            revision=event.revision,
        )
        key = (event.instrument_id, event.timeframe)
        with self._lock:
            series = self._series.setdefault(key, {})
            confirmed = self._confirmed.setdefault(key, set())
            if raw.ts_event in confirmed:
                if not event.confirmed:
                    return
                raw = replace(raw, ts_available=max(raw.ts_available, now))
            versions = series.setdefault(raw.ts_event, {})
            if event.confirmed:
                confirmed.add(raw.ts_event)
                versions.pop((raw.ts_available, -1), None)
            # 收盘前同一可获取时刻的推送只需保留最后一版。
            version_key = (raw.ts_available, raw.revision if event.confirmed else -1)
            previous = versions.get(version_key)
            if previous is None or raw.revision >= previous.revision:
                versions[version_key] = raw
            while len(series) > self._max_bars:
                oldest = min(series)
                series.pop(oldest)
                confirmed.discard(oldest)

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> list[RawBar]:
        with self._lock:
            series = self._series.get((instrument_id, timeframe), {})
            return [
                series[timestamp][version]
                for timestamp in sorted(series)
                for version in sorted(series[timestamp])
            ]
