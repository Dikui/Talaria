"""实时热端 K 线内存环：冻结快照时提供尚未归档的切片。

``FeedSupervisor`` 是行情状态机，不对外暴露原始柱；本缓冲以监听器方式订阅
``KlineEvent``（在 app 中接线），只保留每个 ``(instrument, timeframe)`` 最近的
柱，并把系统接收/校验完成时刻记为 ``Committed Time``。快照构建时由
``SnapshotBuilder`` 与已归档历史合并，因此重复事件天然被 later-wins 去重。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from talaria.foundation.bars import BarStatus
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.market import InstrumentSpec

DEFAULT_MAX_BARS_PER_SERIES = 5_000


class HotBarProvider(Protocol):
    """冻结快照时可注入的热端柱来源。"""

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> Sequence[RawBar]:
        """返回该通道最近的原始柱（升序）；空通道返回空列表。"""


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
        self._series: dict[tuple[str, str], dict[int, RawBar]] = {}
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
        )
        key = (event.instrument_id, event.timeframe)
        with self._lock:
            series = self._series.setdefault(key, {})
            series[raw.ts_event] = raw
            while len(series) > self._max_bars:
                series.pop(min(series))

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> list[RawBar]:
        with self._lock:
            series = self._series.get((instrument_id, timeframe), {})
            return [series[key] for key in sorted(series)]
