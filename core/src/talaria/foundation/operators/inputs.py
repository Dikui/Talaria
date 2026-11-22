"""为衍生品算子按每根柱的事件时点对齐可见输入。"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from operator import attrgetter

from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.operators.core import DerivativeSample


def _at_bar_opens(
    bars: Sequence[CompactBar], points: Sequence[DerivativePoint],
) -> Iterator[DerivativePoint | None]:
    """随可获取时间推进，只留下更新了最新事件的取值。"""
    ordered = sorted(points, key=attrgetter("ts_available", "ts_event", "ts_committed"))
    latest: list[DerivativePoint] = []
    for point in ordered:
        if not latest or point.ts_event >= latest[-1].ts_event:
            latest.append(point)
    times = [point.ts_available for point in latest]
    for bar in bars:
        count = bisect_right(times, bar.timestamp)
        yield latest[count - 1] if count else None


def derivative_samples(
    bars: Sequence[CompactBar],
    funding: Sequence[DerivativePoint],
    open_interest: Sequence[DerivativePoint],
    *,
    mark_price: Sequence[DerivativePoint] = (),
) -> list[DerivativeSample]:
    """在每根柱开盘时点，为各衍生品序列选取可获取的最新事件。"""
    samples: list[DerivativeSample] = []
    for funding_point, oi_point, mark_point in zip(
        _at_bar_opens(bars, funding), _at_bar_opens(bars, open_interest),
        _at_bar_opens(bars, mark_price), strict=True,
    ):
        samples.append(DerivativeSample(
            funding_rate=None if funding_point is None else funding_point.value,
            funding_ts=None if funding_point is None else funding_point.ts_event,
            open_interest=None if oi_point is None else oi_point.value,
            oi_ts=None if oi_point is None else oi_point.ts_event,
            mark_price=None if mark_point is None else mark_point.value,
            mark_ts=None if mark_point is None else mark_point.ts_event,
        ))
    return samples
