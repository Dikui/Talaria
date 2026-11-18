"""成交量化关键位、锚定 VWAP 与清算估算（任务单 11）。"""

from __future__ import annotations

from decimal import Decimal

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.key_levels import (
    KEY_LEVEL_METHOD_VERSION,
    KeyLevelCategory,
    KeyLevelKind,
    KeyLevelParams,
    SessionProfile,
    compute_key_levels,
    compute_key_levels_with_notes,
)
from talaria.foundation.types import PrecisionTier

DAY = 1_699_920_000_000  # 2023-11-14 00:00:00 UTC
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"


def make_bar(
    index: int,
    price: str,
    volume: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> CompactBar:
    close_value = Decimal(price)
    return CompactBar(
        timestamp=DAY + index * MINUTE,
        open=close_value,
        high=Decimal(high) if high is not None else close_value,
        low=Decimal(low) if low is not None else close_value,
        close=close_value,
        volume=Decimal(volume),
        turnover=Decimal(volume) * close_value,
        status=BarStatus.NORMAL,
        ts_available=DAY + (index + 1) * MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def oi_point(index: int, value: str, *, visible_offset: int = 0) -> DerivativePoint:
    ts_event = DAY + index * MINUTE
    ts_available = ts_event + MINUTE + visible_offset
    return DerivativePoint(
        instrument_id=INSTRUMENT,
        kind=DerivativeKind.OPEN_INTEREST,
        timeframe="1m",
        value=Decimal(value),
        ts_event=ts_event,
        ts_available=ts_available,
        ts_ingested=ts_available,
        ts_committed=ts_available,
    )


def by_kind(levels, *kinds: KeyLevelKind):
    wanted = set(kinds)
    return [level for level in levels if level.kind in wanted]


def test_volume_profile_poc_vah_val_without_lookahead() -> None:
    bars = [
        make_bar(0, "100", "1"),
        make_bar(1, "110", "5"),
        make_bar(2, "120", "2"),
    ]
    cutoff = bars[-1].ts_available
    levels = compute_key_levels(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=cutoff,
        session_profile=SessionProfile(),
        params=KeyLevelParams(volume_profile_bins=4, value_area_ratio=0.7),
    )
    volume_levels = by_kind(
        levels, KeyLevelKind.POC, KeyLevelKind.VAH, KeyLevelKind.VAL
    )
    assert {level.kind for level in volume_levels} == {
        KeyLevelKind.POC,
        KeyLevelKind.VAH,
        KeyLevelKind.VAL,
    }
    by_name = {level.kind: level for level in volume_levels}
    assert by_name[KeyLevelKind.POC].price == Decimal("112.5")
    assert by_name[KeyLevelKind.VAH].price == Decimal("120")
    assert by_name[KeyLevelKind.VAL].price == Decimal("110")
    for level in volume_levels:
        assert level.category is KeyLevelCategory.VOLUME
        assert level.evidence_class.value == "OBSERVED"
        assert level.method_version == KEY_LEVEL_METHOD_VERSION
        assert level.status.value == "ACTIVE"
        assert level.formed_ts == bars[0].timestamp
        assert level.ts_available == cutoff
        assert level.parameters["volume_profile_bins"] == "4"
        assert level.parameters["value_area_ratio"] == "0.7"


def test_volume_profile_ignores_bars_not_yet_available() -> None:
    bars = [
        make_bar(0, "100", "1"),
        make_bar(1, "110", "5"),
    ]
    future = make_bar(2, "130", "100")
    future = future.model_copy(update={"ts_available": DAY + 10 * MINUTE})
    cutoff = bars[-1].ts_available
    levels = compute_key_levels(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=[*bars, future],
        cutoff_ts=cutoff,
        params=KeyLevelParams(volume_profile_bins=4),
    )
    poc = by_kind(levels, KeyLevelKind.POC)
    assert len(poc) == 1
    assert poc[0].price is not None
    assert poc[0].price < Decimal("130")


def test_anchored_vwap_snapshot_anchor_bands_and_zero_volume_bar() -> None:
    bars = [
        make_bar(0, "100", "1"),
        make_bar(1, "120", "1"),
        make_bar(2, "130", "0"),
    ]
    cutoff = bars[-1].ts_available
    levels = compute_key_levels(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=cutoff,
        params=KeyLevelParams(vwap_anchor="SNAPSHOT", vwap_band_sigmas="1,2"),
    )
    vwap_levels = by_kind(levels, KeyLevelKind.ANCHORED_VWAP)
    by_price = {level.price: level for level in vwap_levels}
    assert set(by_price) == {
        Decimal("90"),
        Decimal("100"),
        Decimal("110"),
        Decimal("120"),
        Decimal("130"),
    }
    center = by_price[Decimal("110")]
    assert center.parameters["band_sigma"] == "0"
    assert center.parameters["vwap_anchor"] == "snapshot"
    assert center.formed_ts == bars[0].timestamp
    assert center.ts_available == bars[1].ts_available
    assert center.ts_available < cutoff
    for price, sigma in (
        (Decimal("100"), "-1"),
        (Decimal("120"), "1"),
        (Decimal("90"), "-2"),
        (Decimal("130"), "2"),
    ):
        assert by_price[price].parameters["band_sigma"] == sigma
        assert by_price[price].category is KeyLevelCategory.VOLUME
        assert by_price[price].evidence_class.value == "OBSERVED"
        assert by_price[price].ts_available == bars[1].ts_available
    assert center.strength > by_price[Decimal("120")].strength
    assert by_price[Decimal("120")].strength > by_price[Decimal("130")].strength


def test_anchored_vwap_timestamp_anchor() -> None:
    bars = [make_bar(0, "100", "1"), make_bar(1, "120", "1"), make_bar(2, "140", "1")]
    anchor = bars[1].timestamp
    levels = compute_key_levels(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        params=KeyLevelParams(
            vwap_anchor="TIMESTAMP",
            vwap_anchor_ts=anchor,
            vwap_band_sigmas="1",
        ),
    )
    vwap_levels = by_kind(levels, KeyLevelKind.ANCHORED_VWAP)
    center = next(level for level in vwap_levels if level.parameters["band_sigma"] == "0")
    assert center.price == Decimal("130")
    assert center.formed_ts == anchor
    assert len(vwap_levels) == 3


def test_liquidation_estimate_uses_oi_change_and_leverage_tiers() -> None:
    bars = [make_bar(i, str(100 + i * 2), "10") for i in range(6)]
    points = [oi_point(i, str(1000 + i * 10)) for i in range(6)]
    result = compute_key_levels_with_notes(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        derivative_points=points,
        params=KeyLevelParams(
            leverage_tiers=(5, 10),
            liquidation_cluster_atr=0.5,
            liquidation_min_points=4,
            liquidation_max_levels=8,
        ),
    )
    estimated = [
        level
        for level in result.levels
        if level.category is KeyLevelCategory.LIQUIDATION
    ]
    assert estimated, "预期从 OI 增量推导出清算密集区"
    assert all(level.evidence_class.value == "ESTIMATED" for level in estimated)
    assert all(level.kind is KeyLevelKind.LIQUIDATION_ZONE for level in estimated)
    assert all(level.price is None for level in estimated)
    assert all(level.price_low is not None and level.price_high is not None for level in estimated)
    assert all(level.price_low <= level.price_high for level in estimated)
    assert all(level.parameters["leverage_tiers"] == "5,10" for level in estimated)
    assert all(level.ts_available <= bars[-1].ts_available for level in estimated)
    assert result.notes == []


def test_liquidation_estimate_returns_empty_with_reason_when_oi_insufficient() -> None:
    bars = [make_bar(i, str(100 + i), "10") for i in range(6)]
    points = [oi_point(0, "1000"), oi_point(1, "1010")]
    result = compute_key_levels_with_notes(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        derivative_points=points,
        params=KeyLevelParams(liquidation_min_points=4),
    )
    assert not [
        level
        for level in result.levels
        if level.category is KeyLevelCategory.LIQUIDATION
    ]
    assert result.notes
    assert "持仓量样本不足" in result.notes[0]
    assert "2/4" in result.notes[0]

    empty = compute_key_levels_with_notes(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        derivative_points=[],
        params=KeyLevelParams(liquidation_min_points=4),
    )
    assert not [
        level for level in empty.levels if level.category is KeyLevelCategory.LIQUIDATION
    ]
    assert empty.notes
    assert "没有可用的持仓量数据" in empty.notes[0]


def test_liquidation_estimate_excludes_future_oi_points() -> None:
    bars = [make_bar(i, str(100 + i * 2), "10") for i in range(6)]
    points = [oi_point(i, str(1000 + i * 10)) for i in range(6)]
    future = oi_point(10, "99999", visible_offset=10 * MINUTE)
    cutoff = bars[2].ts_available
    result = compute_key_levels_with_notes(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=cutoff,
        derivative_points=[*points, future],
        params=KeyLevelParams(liquidation_min_points=3, liquidation_max_levels=8),
    )
    for level in result.levels:
        assert level.ts_available <= cutoff
    assert all(level.formed_ts <= cutoff for level in result.levels)


def test_level_ids_are_stable_across_calls() -> None:
    bars = [
        make_bar(0, "100", "1"),
        make_bar(1, "110", "5"),
        make_bar(2, "120", "2"),
        make_bar(3, "120", "0"),
    ]
    kwargs = dict(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        params=KeyLevelParams(vwap_anchor="SNAPSHOT", vwap_band_sigmas="1"),
    )
    first = compute_key_levels(**kwargs)
    second = compute_key_levels(**kwargs)
    assert [level.level_id for level in first] == [level.level_id for level in second]
