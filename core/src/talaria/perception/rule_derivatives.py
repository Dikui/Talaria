"""为一次规则或扫描观察提供冷库修订，补入尚未归档的实时和回填数据。"""

from collections import deque
from collections.abc import Callable, Iterable, Sequence
from functools import cache

from talaria.foundation.derivatives import DerivativeKind, DerivativePoint, derivative_identity
from talaria.foundation.feed import DerivativeTickEvent
from talaria.foundation.operators import MAX_PERIOD
from talaria.foundation.timeframes import timeframe_ms


ArchiveReader = Callable[[str, DerivativeKind, int], Sequence[DerivativePoint]]
RevisionSeries = Callable[[DerivativeKind], Sequence[DerivativePoint]]


class RuleDerivatives:
    def __init__(self, limit: int, read_archive: ArchiveReader | None = None):
        self._limit = limit
        self._read_archive = read_archive
        self._series: dict[
            tuple[str, DerivativeKind, str | None, bool], dict[tuple[int, int, int], DerivativePoint]
        ] = {}
        self._ticks: dict[tuple[str, DerivativeKind], deque[DerivativePoint]] = {}
        self._oi_buckets: dict[str, dict[int, DerivativePoint]] = {}

    def seed(self, points: Iterable[DerivativePoint]) -> None:
        for point in points:
            series_key = (point.instrument_id, point.kind, point.timeframe, point.is_funding_settlement)
            series = self._series.setdefault(series_key, {})
            key = (point.ts_event, point.revision, point.ts_available)
            previous = series.get(key)
            if previous is None or point.ts_ingested >= previous.ts_ingested:
                series[key] = point
            if len(series) > 2 * MAX_PERIOD:
                for stale in sorted(series)[:-MAX_PERIOD - 2]:
                    del series[stale]

    def handle_tick(self, tick: DerivativeTickEvent) -> None:
        for point in tick.points():
            ticks = self._ticks.setdefault((tick.instrument_id, point.kind), deque(maxlen=self._limit))
            if point.kind is DerivativeKind.OPEN_INTEREST and len(ticks) == self._limit:
                old = ticks[0]
                # 高频逐笔窗口之外，每个 5m 持仓量槽仍保留末次读数供门禁使用。
                buckets = self._oi_buckets.setdefault(tick.instrument_id, {})
                slot = old.ts_event // timeframe_ms("5m")
                previous = buckets.get(slot)
                if previous is None or (old.ts_event, old.ts_ingested) >= (previous.ts_event, previous.ts_ingested):
                    buckets[slot] = old
                if len(buckets) > self._limit:
                    del buckets[min(buckets)]
            ticks.append(point)

    def observation(self, instrument: str, cutoff: int) -> RevisionSeries:
        @cache
        def revisions(kind: DerivativeKind) -> tuple[DerivativePoint, ...]:
            archive = self._read_archive(instrument, kind, cutoff) if self._read_archive else ()
            archived = {derivative_identity(point) for point in archive}
            # 连接器给出的历史基线没有冷库修订号，不能覆盖已归档事件的可见时间。
            points = []
            for (symbol, category, _, _), series in self._series.items():
                if symbol == instrument and category is kind:
                    points.extend(point for point in series.values() if derivative_identity(point) not in archived)
            if kind is DerivativeKind.OPEN_INTEREST:
                points.extend(self._oi_buckets.get(instrument, {}).values())
            points.extend(self._ticks.get((instrument, kind), ()))
            return (*points, *archive)

        return revisions
