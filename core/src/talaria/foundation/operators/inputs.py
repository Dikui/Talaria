"""为衍生品算子按每根柱的事件时点对齐可见输入。"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterator, Sequence
from itertools import groupby
from operator import attrgetter

from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativePoint, derivative_revision_order
from talaria.foundation.operators.core import DerivativeSample


def _at_bar_opens(
    bars: Sequence[CompactBar], points: Sequence[DerivativePoint], as_of: Sequence[int],
) -> Iterator[DerivativePoint | None]:
    """按开盘槽对齐事件，按该柱的可获取时间截点选版。"""
    by_event = attrgetter("ts_event")
    timestamps = []
    revisions = []
    for timestamp, group in groupby(sorted(points, key=by_event), key=by_event):
        timestamps.append(timestamp)
        revisions.append(sorted(group, key=derivative_revision_order, reverse=True))
    for bar, cutoff in zip(bars, as_of, strict=True):
        visible = None
        for index in range(bisect_right(timestamps, bar.timestamp) - 1, -1, -1):
            visible = next((point for point in revisions[index] if point.ts_available <= cutoff), None)
            if visible is not None:
                break
        yield visible


def derivative_samples(
    bars: Sequence[CompactBar],
    funding: Sequence[DerivativePoint],
    open_interest: Sequence[DerivativePoint],
    *,
    mark_price: Sequence[DerivativePoint] = (),
    as_of: Sequence[int] | None = None,
) -> list[DerivativeSample]:
    """取各开盘槽对应的最新事件。省略可获取时间截点时按开盘时间选版。"""
    if as_of is None:
        as_of = [bar.timestamp for bar in bars]
    samples: list[DerivativeSample] = []
    for funding_point, oi_point, mark_point in zip(
        _at_bar_opens(bars, funding, as_of), _at_bar_opens(bars, open_interest, as_of),
        _at_bar_opens(bars, mark_price, as_of), strict=True,
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
