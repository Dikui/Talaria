"""工作台规则与扫描门禁的衍生品输入。费率序列只接收结算点。"""

from collections import deque
from collections.abc import Iterable, Iterator
from itertools import chain

from talaria.foundation.derivatives import DerivativeKind, DerivativePoint, OI_TIMEFRAME_BY_TIMEFRAME
from talaria.foundation.feed import DerivativeTickEvent
from talaria.foundation.operators import MAX_PERIOD
from talaria.foundation.timeframes import timeframe_ms


class RuleDerivatives:
    def __init__(self, limit: int):
        self._limit = limit
        self._series: dict[
            tuple[str, DerivativeKind, str | None], dict[tuple[int, int], DerivativePoint]
        ] = {}
        self._ticks: dict[tuple[str, DerivativeKind], deque[DerivativeTickEvent]] = {}
        self._oi_buckets: dict[str, dict[int, DerivativeTickEvent]] = {}

    def seed(self, points: Iterable[DerivativePoint]) -> None:
        for point in points:
            series = self._series.setdefault((point.instrument_id, point.kind, point.timeframe), {})
            key = (point.ts_event, point.ts_available)
            previous = series.get(key)
            if previous is None or point.ts_committed >= previous.ts_committed:
                series[key] = point
            if len(series) > 2 * MAX_PERIOD:
                for stale in sorted(series)[:-MAX_PERIOD - 2]:
                    del series[stale]

    def handle_tick(self, tick: DerivativeTickEvent) -> None:
        for kind in (DerivativeKind.MARK_PRICE, DerivativeKind.OPEN_INTEREST):
            if getattr(tick, kind.value) is None:
                continue
            ticks = self._ticks.setdefault((tick.instrument_id, kind), deque(maxlen=self._limit))
            if kind is DerivativeKind.OPEN_INTEREST and len(ticks) == self._limit:
                old = ticks[0]
                # 高频逐笔窗口之外，每个 5m 持仓量槽仍保留末次读数供门禁使用。
                buckets = self._oi_buckets.setdefault(tick.instrument_id, {})
                slot = old.ts_event // timeframe_ms("5m")
                previous = buckets.get(slot)
                if previous is None or (old.ts_event, old.ts_ingested) >= (previous.ts_event, previous.ts_ingested):
                    buckets[slot] = old
                if len(buckets) > self._limit:
                    del buckets[min(buckets)]
            ticks.append(tick)

    def seeded(self, instrument: str) -> Iterator[DerivativePoint]:
        for (symbol, _, _), series in self._series.items():
            if symbol == instrument:
                yield from series.values()

    def ticks(self, instrument: str, timeframe: str) -> Iterator[DerivativePoint]:
        for kind, period in (
            (DerivativeKind.MARK_PRICE, timeframe),
            (DerivativeKind.OPEN_INTEREST, OI_TIMEFRAME_BY_TIMEFRAME[timeframe]),
        ):
            recent = self._ticks.get((instrument, kind), ())
            older = self._oi_buckets.get(instrument, {}).values() if kind is DerivativeKind.OPEN_INTEREST else ()
            for tick in chain(older, recent):
                yield DerivativePoint(
                    instrument_id=instrument, kind=kind, timeframe=period, value=getattr(tick, kind.value),
                    ts_event=tick.ts_event, ts_available=max(tick.ts_event, tick.ts_ingested),
                    ts_ingested=tick.ts_ingested, ts_committed=tick.ts_ingested,
                )

    def points(self, instrument: str, timeframe: str) -> Iterator[DerivativePoint]:
        yield from self.seeded(instrument)
        yield from self.ticks(instrument, timeframe)
