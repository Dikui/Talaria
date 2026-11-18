"""快照到冷库的增量发布：按游标只发布新柱，严禁倒灌或重放旧柱。

``SnapshotBuilder`` 每次冻结快照都会把该周期实际可见的标准化柱交给
``ColdPublisher``。发布器自己完成排序、同时间戳去重（后出现者胜）和游标过滤，
冷库侧再做一次 append-only 守卫，因此旧批次最多被跳过，绝不会让游标回退。
"""

from __future__ import annotations

import threading
from typing import Protocol, Sequence

from talaria.foundation.connector import RawBar
from talaria.foundation.market import InstrumentSpec
from talaria.perception.cold_store import ColdBatch, ColdGeneration, ColdStore


class ColdBarPublisher(Protocol):
    """``SnapshotBuilder`` 需要的冷库发布端口。"""

    def publish_snapshot_bars(self, bars: Sequence[RawBar]) -> ColdGeneration | None: ...


def _is_real_bar(bar: RawBar) -> bool:
    """冷库只接受真实成交柱；空值/缺口柱由快照的 FEED_GAP 表达，不归档。"""

    return all(
        value is not None
        for value in (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.turnover,
        )
    )


def _sorted_unique(bars: Sequence[RawBar]) -> list[RawBar]:
    """按事件时间排序，同时间戳保留调用方传入的最后一个版本。"""

    ordered = sorted((bar for bar in bars if _is_real_bar(bar)), key=lambda bar: bar.ts_event)
    by_timestamp: dict[int, RawBar] = {}
    for bar in ordered:
        by_timestamp[bar.ts_event] = bar
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


class ColdPublisher:
    """单个 ``(instrument, timeframe)`` 的增量发布器，游标是唯一进度真相。"""

    def __init__(
        self,
        store: ColdStore,
        *,
        instrument: InstrumentSpec,
        timeframe: str,
    ) -> None:
        self._store = store
        self._instrument = instrument
        self._timeframe = timeframe
        self._lock = threading.Lock()

    def publish_snapshot_bars(self, bars: Sequence[RawBar]) -> ColdGeneration | None:
        candidates = _sorted_unique(bars)
        if not candidates:
            return None
        with self._lock:
            cursor = self._store.cursor(self._instrument, self._timeframe)
            floor = cursor.last_ts if cursor is not None else None
            fresh = [
                bar
                for bar in candidates
                if floor is None or bar.ts_event > floor
            ]
            if not fresh:
                return None
            return self._store.publish(
                ColdBatch(
                    instrument=self._instrument,
                    timeframe=self._timeframe,
                    bars=fresh,
                )
            )
