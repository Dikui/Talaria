"""关键位引擎契约：确认延迟无前视、区间聚合、状态迁移与稳定身份。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels import (
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelParams,
    KeyLevelStatus,
    SessionProfile,
    compute_key_levels,
)
from talaria.foundation.market import TradingSession
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.spec]

MINUTE = 60_000
HOUR = 60 * MINUTE
T0 = 1_700_000_000_000 - (1_700_000_000_000 % MINUTE)


def _bar(
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    timeframe_ms: int = MINUTE,
    volume: str = "10",
) -> CompactBar:
    timestamp = T0 + index * timeframe_ms
    return CompactBar(
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(volume) * Decimal(close),
        status=BarStatus.NORMAL,
        ts_available=timestamp + timeframe_ms,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _levels(
    bars: list[CompactBar],
    cutoff_ts: int,
    *,
    timeframe: str = "1m",
    params: KeyLevelParams | None = None,
    profile: SessionProfile | None = None,
):
    return compute_key_levels(
        instrument_id="BTC-USDT-PERP",
        timeframe=timeframe,
        bars=bars,
        cutoff_ts=cutoff_ts,
        session_profile=profile,
        params=params,
    )


def _only(levels, kind: KeyLevelKind):
    matches = [level for level in levels if level.kind is kind]
    assert len(matches) == 1, [level.model_dump() for level in levels]
    return matches[0]


def test_swing_pivot_is_invisible_until_confirmation_bar_closes() -> None:
    bars = [
        _bar(i, "100", "101", "99", "100") for i in range(5)
    ] + [
        _bar(5, "100", "120", "100", "105"),
        _bar(6, "105", "106", "101", "105"),
        _bar(7, "105", "106", "101", "105"),
        _bar(8, "105", "106", "101", "105"),
    ]

    before = _levels(bars, bars[6].ts_available)
    assert not [level for level in before if level.kind is KeyLevelKind.SWING_HIGH]

    after = _levels(bars, bars[7].ts_available)
    level = _only(after, KeyLevelKind.SWING_HIGH)
    assert level.price == Decimal("120")
    assert level.ts_available == bars[7].ts_available
    assert level.ts_available > bars[5].timestamp
    assert level.formed_ts == bars[5].timestamp
    assert level.status is KeyLevelStatus.ACTIVE
    assert level.category is KeyLevelCategory.STRUCTURE
    assert level.touch_count >= 1
    assert level.method_version


def test_swing_pivots_cluster_into_zone_and_keep_stable_id() -> None:
    bars = [_bar(i, "100", "101", "99", "100") for i in range(5)]
    # 两个相邻摆动高点，价差落在聚簇容差内。
    bars += [
        _bar(5, "100", "120.2", "100", "105"),
        _bar(6, "105", "106", "101", "105"),
        _bar(7, "105", "106", "101", "105"),
        _bar(8, "105", "106", "101", "105"),
        _bar(9, "105", "106", "103", "105"),
        _bar(10, "105", "106", "103", "105"),
        _bar(11, "105", "106", "103", "105"),
        _bar(12, "105", "120.6", "103", "110"),
        _bar(13, "110", "111", "106", "110"),
        _bar(14, "110", "111", "106", "110"),
    ]
    params = KeyLevelParams(cluster_atr=2.0, pivot_atr_min=0.2, touch_atr=0.5)
    cutoff = bars[14].ts_available

    levels = _levels(bars, cutoff, params=params)
    zone = _only(levels, KeyLevelKind.SWING_HIGH)
    assert zone.price is None
    assert zone.price_low == Decimal("120.2")
    assert zone.price_high == Decimal("120.6")
    assert zone.touch_count >= 2

    reversed_levels = _levels(list(reversed(bars)), cutoff, params=params)
    assert _only(reversed_levels, KeyLevelKind.SWING_HIGH).level_id == zone.level_id

    extended = bars + [_bar(i, "110", "111", "106", "110") for i in range(15, 18)]
    extended_levels = _levels(extended, extended[17].ts_available, params=params)
    assert _only(extended_levels, KeyLevelKind.SWING_HIGH).level_id == zone.level_id


def test_support_status_transitions_active_broken_flipped() -> None:
    bars = [
        _bar(0, "105", "106", "104", "105"),
        _bar(1, "105", "106", "104", "105"),
        _bar(2, "105", "106", "104", "105"),
        _bar(3, "101", "102", "100", "101"),
        _bar(4, "103", "105", "103", "104"),
        _bar(5, "103", "105", "103", "104"),
    ]
    active = _levels(bars, bars[5].ts_available)
    support = _only(active, KeyLevelKind.SWING_LOW)
    assert support.status is KeyLevelStatus.ACTIVE

    broken_bars = bars + [_bar(6, "102", "102", "97", "98")]
    broken = _only(_levels(broken_bars, broken_bars[6].ts_available), KeyLevelKind.SWING_LOW)
    assert broken.level_id == support.level_id
    assert broken.status is KeyLevelStatus.BROKEN

    flipped_bars = broken_bars + [_bar(7, "101", "103", "101", "102")]
    flipped = _only(_levels(flipped_bars, flipped_bars[7].ts_available), KeyLevelKind.SWING_LOW)
    assert flipped.level_id == support.level_id
    assert flipped.status is KeyLevelStatus.FLIPPED


def test_session_reference_levels_follow_injected_profile() -> None:
    start = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bars: list[CompactBar] = []
    for index in range(30):
        timestamp = start + index * HOUR
        high = "110" if index == 5 else "105"
        low = "95" if index == 14 else "99"
        close = "104" if index < 24 else "106"
        bars.append(
            CompactBar(
                timestamp=timestamp,
                open=Decimal("100"),
                high=Decimal(high),
                low=Decimal(low),
                close=Decimal(close),
                volume=Decimal("10"),
                turnover=Decimal("1000"),
                status=BarStatus.NORMAL,
                ts_available=timestamp + HOUR,
                precision=PrecisionTier.SESSION_BOUND,
            )
        )
    cutoff = bars[-1].ts_available
    profile = SessionProfile(
        timezone="UTC",
        sessions=(
            TradingSession(name="asia", start="00:00", end="08:00"),
        ),
    )

    levels = _levels(
        bars,
        cutoff,
        timeframe="1h",
        profile=profile,
        params=KeyLevelParams(opening_range_minutes=30),
    )
    prev_high = _only(levels, KeyLevelKind.PREV_DAY_HIGH)
    prev_low = _only(levels, KeyLevelKind.PREV_DAY_LOW)
    prev_close = _only(levels, KeyLevelKind.PREV_DAY_CLOSE)
    assert prev_high.price == Decimal("110")
    assert prev_low.price == Decimal("95")
    assert prev_close.price == Decimal("104")
    assert prev_high.ts_available == start + 24 * HOUR
    assert prev_high.category is KeyLevelCategory.SESSION

    session_high = _only(levels, KeyLevelKind.SESSION_HIGH)
    session_low = _only(levels, KeyLevelKind.SESSION_LOW)
    assert session_high.price == Decimal("110")
    assert session_low.price == Decimal("99")
    assert session_high.parameters["session"] == "asia"
    assert session_high.ts_available == start + 8 * HOUR


def test_week_and_month_open_require_boundary_history() -> None:
    # 2025-03-03 是周一，也是 3 月第一个完整周；只提供从 3 月 1 日起的数据。
    start = int(datetime(2025, 3, 1, tzinfo=timezone.utc).timestamp() * 1000)
    bars: list[CompactBar] = []
    for index in range(24 * 3 + 6):
        timestamp = start + index * HOUR
        bars.append(
            CompactBar(
                timestamp=timestamp,
                open=Decimal("200"),
                high=Decimal("201"),
                low=Decimal("199"),
                close=Decimal("200"),
                volume=Decimal("5"),
                turnover=Decimal("1000"),
                status=BarStatus.NORMAL,
                ts_available=timestamp + HOUR,
                precision=PrecisionTier.SESSION_BOUND,
            )
        )
    cutoff = bars[35].ts_available  # 3 月 2 日中午：本月已开始，但本周尚未开始
    levels = _levels(bars, cutoff, timeframe="1h", profile=SessionProfile(timezone="UTC"))
    month_open = _only(levels, KeyLevelKind.MONTH_OPEN)
    assert month_open.price == Decimal("200")
    assert month_open.ts_available == bars[0].ts_available
    assert not [level for level in levels if level.kind is KeyLevelKind.WEEK_OPEN]

    week_start = int(datetime(2025, 3, 3, tzinfo=timezone.utc).timestamp() * 1000)
    week_bars = [
        bar.model_copy(
            update={
                "timestamp": bar.timestamp + (week_start - start),
                "ts_available": bar.ts_available + (week_start - start),
            }
        )
        for bar in bars[:8]
    ]
    week_levels = _levels(
        week_bars,
        week_bars[-1].ts_available,
        timeframe="1h",
        profile=SessionProfile(timezone="UTC"),
    )
    week_open = _only(week_levels, KeyLevelKind.WEEK_OPEN)
    assert week_open.price == Decimal("200")
    assert week_open.ts_available == week_bars[0].ts_available


def test_unfilled_gap_is_emitted_and_filled_gap_is_dropped() -> None:
    bars = [
        _bar(0, "100", "100", "99", "99.5"),
        _bar(1, "101", "102", "101", "101.5"),  # 向上跳空 [100, 101]
        _bar(2, "101.5", "102", "100.5", "101"),  # 部分回补，未完全回补
        _bar(3, "101", "102", "101", "101.5"),
        _bar(4, "101.5", "102", "101", "101.5"),
        _bar(5, "103", "104", "103", "103.5"),  # 第二个向上跳空 [102, 103]
        _bar(6, "103.5", "104", "103", "103.5"),
        _bar(7, "104", "104.5", "102", "103"),  # 完全回补第二个缺口
    ]
    params = KeyLevelParams(gap_atr_min=0.0)
    levels = _levels(bars, bars[7].ts_available, params=params)
    gaps = [level for level in levels if level.kind is KeyLevelKind.GAP]
    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.price_low == Decimal("100")
    assert gap.price_high == Decimal("101")
    assert gap.ts_available == bars[1].ts_available
    assert gap.category is KeyLevelCategory.GAP


def test_round_number_levels_are_derived_from_price_magnitude() -> None:
    bars = [_bar(i, "60000", "60100", "59900", "60000") for i in range(20)]
    params = KeyLevelParams(round_levels_per_side=2)
    levels = _levels(bars, bars[-1].ts_available, params=params)
    rounds = [level for level in levels if level.kind is KeyLevelKind.ROUND_NUMBER]
    prices = {level.price for level in rounds}
    assert {Decimal("58000"), Decimal("59000"), Decimal("60000"), Decimal("61000"), Decimal("62000")} <= prices
    assert all(level.category is KeyLevelCategory.ROUND_NUMBER for level in rounds)
    assert all(level.price == level.price.quantize(Decimal("1000")) for level in rounds)

    reversed_levels = _levels(list(reversed(bars)), bars[-1].ts_available, params=params)
    reversed_rounds = [
        level.level_id for level in reversed_levels if level.kind is KeyLevelKind.ROUND_NUMBER
    ]
    assert reversed_rounds == [level.level_id for level in rounds]


def test_engine_never_emits_levels_after_cutoff() -> None:
    bars = [
        _bar(i, "100", "110" if i == 3 else "101", "99", "100") for i in range(20)
    ]
    cutoff = bars[6].ts_available
    for level in _levels(bars, cutoff):
        assert level.ts_available <= cutoff
        assert level.formed_ts <= cutoff
