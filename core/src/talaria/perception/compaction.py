"""把原始柱整理成图表紧凑序列。

规则：
- 只保留 ``ts_available <= cutoff_ts`` 的柱（HISTORICAL_PIT）；
- 同一 timestamp 以后摄入者为准；
- 按周期检测缺口，插入 ``FEED_GAP`` 柱（安全上限，避免异常数据撑爆内存）；
- 零成交量且开高低收相等的柱标为 ``NO_TRADES``；
- 其余为 ``NORMAL``。
"""

from __future__ import annotations

from typing import Sequence

from talaria.foundation.bars import BarStatus, CompactBar, RangeBar, classify_flat_bar
from talaria.foundation.connector import RawBar
from talaria.foundation.timeframes import timeframe_ms

#: 单个缺口最多补多少根；超过时说明数据源本身有问题，不展开。
MAX_GAP_FILL = 1000


def compact_series(
    bars: Sequence[RawBar | RangeBar],
    *,
    timeframe: str,
    cutoff_ts: int,
) -> list[CompactBar]:
    interval = timeframe_ms(timeframe)
    eligible = [bar for bar in bars if bar.ts_available <= cutoff_ts]

    # 同一 timestamp 以后摄入者为准（输入顺序近似摄入顺序）。
    deduped: dict[int, RawBar | RangeBar] = {}
    for bar in eligible:
        deduped[bar.ts_event] = bar
    ordered = [deduped[key] for key in sorted(deduped)]

    compact: list[CompactBar] = []
    previous_ts: int | None = None
    for bar in ordered:
        if previous_ts is not None:
            missing = (bar.ts_event - previous_ts) // interval - 1
            if 0 < missing <= MAX_GAP_FILL:
                for step in range(1, missing + 1):
                    gap_ts = previous_ts + step * interval
                    compact.append(
                        CompactBar(
                            timestamp=gap_ts,
                            open=None,
                            high=None,
                            low=None,
                            close=None,
                            volume=None,
                            turnover=None,
                            status=BarStatus.FEED_GAP,
                            ts_available=gap_ts + interval,
                            precision=bar.precision,
                        )
                    )
        status = classify_flat_bar(
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        compact.append(CompactBar(
            timestamp=bar.ts_event, open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume, turnover=bar.turnover, status=status,
            ts_available=bar.ts_available, precision=bar.precision,
        ))
        previous_ts = bar.ts_event
    return compact
