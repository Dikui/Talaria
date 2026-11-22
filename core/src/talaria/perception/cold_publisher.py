"""快照到冷库的增量发布：追加新柱和未发布的修订。

``SnapshotBuilder`` 冻结快照时，交给发布器该周期所有可见的修订。
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
    by_revision: dict[tuple[int, int], RawBar] = {}
    for bar in bars:
        if not _is_real_bar(bar):
            continue
        key = (bar.ts_event, bar.revision)
        current = by_revision.get(key)
        if current is None or (bar.ts_available, bar.ts_ingested) >= (
            current.ts_available,
            current.ts_ingested,
        ):
            by_revision[key] = bar
    return [by_revision[key] for key in sorted(by_revision)]


class ColdPublisher:
    """单个 ``(instrument, timeframe)`` 的增量发布器。"""

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
            fresh = candidates
            if cursor is not None:
                floor = cursor.last_ts
                revised_times = {
                    bar.ts_event for bar in candidates
                    if bar.ts_event <= floor and bar.revision > 0
                }
                published = self._store.published_revisions(
                    self._instrument, self._timeframe, revised_times
                )
                fresh = [
                    bar
                    for bar in candidates
                    if bar.ts_event > floor or (
                        bar.revision > 0 and (bar.ts_event, bar.revision) not in published
                    )
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
