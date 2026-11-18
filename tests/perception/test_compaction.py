"""紧凑柱序列：缺口、零成交、cutoff 与去重语义。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from talaria.foundation.bars import BarStatus
from talaria.foundation.connector import RawBar
from talaria.perception.compaction import MAX_GAP_FILL, compact_series

pytestmark = pytest.mark.small

MINUTE = 60_000


def _bar(
    ts: int,
    *,
    price: str = "100",
    volume: str = "1",
    timeframe: str = "1m",
    available: int | None = None,
    precision: str = "SECOND_VERIFIED",
) -> RawBar:
    """构造一根已收盘原始柱；默认 ts_available = ts + 周期。"""
    value = Decimal(price)
    available_ts = ts + MINUTE if available is None else available
    return RawBar(
        symbol="BTCUSDT",
        timeframe=timeframe,
        ts_event=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal(volume),
        turnover=Decimal(volume) * value,
        ts_available=available_ts,
        ts_ingested=available_ts,
        ts_committed=available_ts,
        precision=precision,
    )


def test_missing_periods_become_feed_gap_bars() -> None:
    # 00:00 与 00:03 之间缺 00:01、00:02。
    series = compact_series(
        [_bar(0), _bar(3 * MINUTE)], timeframe="1m", cutoff_ts=1_000_000
    )
    assert [bar.timestamp for bar in series] == [0, MINUTE, 2 * MINUTE, 3 * MINUTE]
    gaps = [bar for bar in series if bar.status is BarStatus.FEED_GAP]
    assert len(gaps) == 2
    assert all(
        bar.open is None
        and bar.high is None
        and bar.low is None
        and bar.close is None
        and bar.volume is None
        and bar.turnover is None
        for bar in gaps
    )
    assert all(bar.ts_available == bar.timestamp + MINUTE for bar in gaps)


def test_zero_volume_flat_bar_is_no_trades() -> None:
    series = compact_series([_bar(0, volume="0")], timeframe="1m", cutoff_ts=1_000_000)
    assert series[0].status is BarStatus.NO_TRADES


def test_positive_volume_bar_is_normal() -> None:
    series = compact_series([_bar(0, volume="0.001")], timeframe="1m", cutoff_ts=1_000_000)
    assert series[0].status is BarStatus.NORMAL


def test_cutoff_excludes_bars_not_yet_available() -> None:
    early = _bar(0)
    late = _bar(MINUTE)
    series = compact_series([early, late], timeframe="1m", cutoff_ts=MINUTE)
    assert [bar.timestamp for bar in series] == [0]


def test_same_timestamp_last_ingested_wins() -> None:
    first = _bar(0, price="100")
    second = _bar(0, price="200")
    series = compact_series([first, second], timeframe="1m", cutoff_ts=1_000_000)
    assert len(series) == 1
    assert series[0].close == Decimal("200")


def test_gap_larger_than_safety_limit_is_not_expanded() -> None:
    far = (MAX_GAP_FILL + 2) * MINUTE
    series = compact_series([_bar(0), _bar(far)], timeframe="1m", cutoff_ts=10**12)
    assert [bar.status for bar in series] == [BarStatus.NORMAL, BarStatus.NORMAL]


def test_gap_inherits_precision_of_following_bar() -> None:
    series = compact_series(
        [_bar(0), _bar(2 * MINUTE, precision="SESSION_BOUND")],
        timeframe="1m",
        cutoff_ts=1_000_000,
    )
    assert series[1].status is BarStatus.FEED_GAP
    assert series[1].precision.value == "SESSION_BOUND"


def test_large_real_gap_shape() -> None:
    """BTCUSDT 1m fixture 的 780..784 缺口在插入后形成连续时间轴。"""
    start = int(datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp() * 1000)
    bars = [_bar(start + index * MINUTE) for index in range(0, 10) if index not in {3, 4}]
    series = compact_series(bars, timeframe="1m", cutoff_ts=10**15)
    assert len(series) == 10
    assert [bar.timestamp for bar in series] == [start + index * MINUTE for index in range(10)]
    assert [bar.status for bar in series].count(BarStatus.FEED_GAP) == 2
