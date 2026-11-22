"""为衍生品算子按每根柱的事件时点对齐可见输入。"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from operator import attrgetter

from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.operators.core import DerivativeSample


def derivative_samples(
    bars: Sequence[CompactBar],
    funding: Sequence[DerivativePoint],
    open_interest: Sequence[DerivativePoint],
) -> list[DerivativeSample]:
    """在每根柱开盘时点选取可获取的最近费率和 OI，与图表口径一致。"""
    by_availability = attrgetter("ts_available", "ts_event")
    funding_ordered = sorted(funding, key=by_availability)
    oi_ordered = sorted(open_interest, key=by_availability)
    funding_times = [point.ts_available for point in funding_ordered]
    oi_times = [point.ts_available for point in oi_ordered]
    samples: list[DerivativeSample] = []
    for bar in bars:
        funding_count = bisect_right(funding_times, bar.timestamp)
        oi_count = bisect_right(oi_times, bar.timestamp)
        funding_point = funding_ordered[funding_count - 1] if funding_count else None
        oi_point = oi_ordered[oi_count - 1] if oi_count else None
        samples.append(DerivativeSample(
            funding_rate=None if funding_point is None else funding_point.value,
            funding_ts=None if funding_point is None else funding_point.ts_event,
            open_interest=None if oi_point is None else oi_point.value,
            oi_ts=None if oi_point is None else oi_point.ts_event,
        ))
    return samples
