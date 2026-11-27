"""L0 统一市场结构引擎测试：4 种几何原语、无前视、双趋势线、SMC、共振带与插拔扩展。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.drawings import DrawingKind
from talaria.foundation.structures import (
    AnchorSource,
    ChannelGeometry,
    DetectionContext,
    DetectorRegistry,
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAnchor,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureParams,
    StructureStatus,
    compute_structure_set,
    default_structure_registry,
    geometry_bounds_at,
    geometry_to_drawing,
)
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.spec]

MINUTE = 60_000
T0 = 1_700_000_000_000
INSTRUMENT = "BTC-USDT-PERP"


def _bar(
    index: int,
    open_: str | None,
    high: str | None,
    low: str | None,
    close: str | None,
    *,
    status: BarStatus = BarStatus.NORMAL,
    volume: str = "10",
) -> CompactBar:
    ts = T0 + index * MINUTE
    if status is BarStatus.FEED_GAP:
        return CompactBar(
            timestamp=ts,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=None,
            turnover=None,
            status=BarStatus.FEED_GAP,
            ts_available=ts + MINUTE,
            precision=PrecisionTier.SECOND_VERIFIED,
        )
    assert open_ is not None and high is not None and low is not None and close is not None
    return CompactBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(volume) * Decimal(close),
        status=status,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _rising_channel_bars(count: int = 55) -> list[CompactBar]:
    """构造带多轮清晰摆动高低点的上升趋势 K 线序列。"""
    bars: list[CompactBar] = []
    for i in range(count):
        base = Decimal("100") + Decimal(i) * Decimal("0.6")
        cycle = i % 10
        if cycle == 2:
            low = base - Decimal("2.0")
            high = base + Decimal("1.5")
            open_ = base - Decimal("0.5")
            close = base + Decimal("0.8")
        elif cycle == 7:
            low = base - Decimal("0.8")
            high = base + Decimal("3.2")
            open_ = base + Decimal("0.2")
            close = base + Decimal("1.2")
        else:
            low = base - Decimal("1.0")
            high = base + Decimal("1.6")
            open_ = base
            close = base + Decimal("0.5")
        bars.append(_bar(i, str(open_), str(high), str(low), str(close)))
    return bars


def test_geometry_bounds_at_and_drawing_mapping_cover_all_four_primitives() -> None:
    line = HorizontalLineGeometry(price=Decimal("100"))
    assert geometry_bounds_at(line, T0).lower == Decimal("100")
    assert geometry_to_drawing(line, fallback_ts=T0, bar_ms=MINUTE)[0] is DrawingKind.HORIZONTAL_LINE

    zone = HorizontalZoneGeometry(
        price_low=Decimal("99"),
        price_high=Decimal("101"),
        start_ts=T0 + MINUTE,
        end_ts=T0 + 5 * MINUTE,
    )
    assert geometry_bounds_at(zone, T0) is None
    assert geometry_bounds_at(zone, T0 + 2 * MINUTE).upper == Decimal("101")
    assert geometry_bounds_at(zone, T0 + 6 * MINUTE) is None
    assert geometry_to_drawing(zone, fallback_ts=T0, bar_ms=MINUTE)[0] is DrawingKind.RECTANGLE

    slope_lin = SlopeLineGeometry(
        anchor_start=StructureAnchor(ts=T0, price=Decimal("100")),
        anchor_end=StructureAnchor(ts=T0 + 2 * MINUTE, price=Decimal("104")),
        scale=PriceScaleMode.LINEAR,
        extend_right=True,
    )
    assert geometry_bounds_at(slope_lin, T0 - MINUTE) is None
    assert geometry_bounds_at(slope_lin, T0 + MINUTE).lower == Decimal("102")
    assert geometry_bounds_at(slope_lin, T0 + 3 * MINUTE).lower == Decimal("106")
    assert geometry_to_drawing(slope_lin, fallback_ts=T0, bar_ms=MINUTE)[0] is DrawingKind.RAY

    slope_seg = slope_lin.model_copy(update={"extend_right": False})
    assert geometry_bounds_at(slope_seg, T0 + 3 * MINUTE) is None
    assert geometry_to_drawing(slope_seg, fallback_ts=T0, bar_ms=MINUTE)[0] is DrawingKind.SEGMENT

    slope_log = SlopeLineGeometry(
        anchor_start=StructureAnchor(ts=T0, price=Decimal("100")),
        anchor_end=StructureAnchor(ts=T0 + 2 * MINUTE, price=Decimal("400")),
        scale=PriceScaleMode.LOG,
        extend_right=True,
    )
    mid_log = geometry_bounds_at(slope_log, T0 + MINUTE)
    assert mid_log is not None
    assert abs(mid_log.lower - Decimal("200")) < Decimal("0.0001")

    channel = ChannelGeometry(
        upper=SlopeLineGeometry(
            anchor_start=StructureAnchor(ts=T0, price=Decimal("110")),
            anchor_end=StructureAnchor(ts=T0 + 2 * MINUTE, price=Decimal("114")),
        ),
        lower=slope_lin,
    )
    ch_bounds = geometry_bounds_at(channel, T0 + MINUTE)
    assert ch_bounds is not None
    assert ch_bounds.lower == Decimal("102")
    assert ch_bounds.upper == Decimal("112")
    assert geometry_to_drawing(channel, fallback_ts=T0, bar_ms=MINUTE)[0] is DrawingKind.PARALLEL_CHANNEL


def test_strict_no_lookahead_invariance() -> None:
    bars = _rising_channel_bars(55)
    cutoff_idx = 35
    cutoff_ts = bars[cutoff_idx].ts_available

    full_set = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=cutoff_ts,
        snapshot_id="snap_full",
    )
    truncated_set = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars[: cutoff_idx + 1],
        cutoff_ts=cutoff_ts,
        snapshot_id="snap_full",
    )

    assert full_set.structures == truncated_set.structures
    assert all(feat.ts_available <= cutoff_ts for feat in full_set.structures)
    assert all(feat.analysis_window.to_ts <= cutoff_ts for feat in full_set.structures)


def test_pivot_trendline_respects_islands_log_scale_and_dedup() -> None:
    bars = _rising_channel_bars(50)
    # 在中间插入一个大跳空缺口切分 island_id
    gapped_bars = list(bars)
    for i in range(25, len(gapped_bars)):
        b = gapped_bars[i]
        shift = Decimal("40")
        gapped_bars[i] = _bar(
            i,
            str(b.open + shift),
            str(b.high + shift),
            str(b.low + shift),
            str(b.close + shift),
        )

    cutoff_ts = gapped_bars[-1].ts_available
    respect_set = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=gapped_bars,
        cutoff_ts=cutoff_ts,
        detector_ids=("pivot_trendline@1",),
        params=StructureParams(
            trendline_scale=PriceScaleMode.LOG,
            trendline_anchor_source=AnchorSource.WICK,
            respect_islands=True,
            island_gap_atr=2.0,
        ),
    )
    gap_ts = gapped_bars[25].timestamp
    for feat in respect_set.structures:
        assert isinstance(feat.geometry, SlopeLineGeometry)
        assert feat.geometry.scale is PriceScaleMode.LOG
        # 开启 Islands 隔离后，单条趋势线的两个锚点不得跨越跳空边界
        assert not (
            feat.geometry.anchor_start.ts < gap_ts <= feat.geometry.anchor_end.ts
        )

    assert len(respect_set.structures) <= 6


def test_convex_envelope_is_non_violating_and_zero_lag() -> None:
    bars = _rising_channel_bars(40)
    cutoff_ts = bars[-1].ts_available
    window = 25
    result = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=cutoff_ts,
        detector_ids=("convex_envelope@1",),
        params=StructureParams(envelope_window=window),
    )
    assert len(result.structures) == 2
    kinds = {f.kind for f in result.structures}
    assert kinds == {"ENVELOPE_SUPPORT", "ENVELOPE_RESISTANCE"}

    window_bars = bars[-window:]
    for feat in result.structures:
        assert feat.ts_available == cutoff_ts
        for bar in window_bars:
            bounds = geometry_bounds_at(feat.geometry, bar.timestamp)
            assert bounds is not None
            if feat.kind == "ENVELOPE_SUPPORT":
                assert bounds.lower <= bar.low + Decimal("0.0001")
            else:
                assert bounds.upper >= bar.high - Decimal("0.0001")


def test_fvg_zero_lookahead_and_lifecycle_break() -> None:
    # 构造明确三柱多头失衡缺口，随后在第 5 根大阴线跌破回补
    bars = [
        _bar(0, "100", "101", "99", "100.5"),
        _bar(1, "100.5", "105", "100.2", "104.8"),
        _bar(2, "104.8", "106", "103.5", "105.5"),  # FVG: [101, 103.5] 在本根收盘才可见
        _bar(3, "105.5", "106.2", "104.5", "105.8"),
        _bar(4, "105.5", "105.6", "99.5", "100.0"),  # 跌穿 101 -> BROKEN
    ]
    at_bar1 = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[1].ts_available,
        detector_ids=("fvg@1",),
    )
    assert at_bar1.structures == []

    at_bar2 = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[2].ts_available,
        detector_ids=("fvg@1",),
    )
    assert len(at_bar2.structures) == 1
    fvg = at_bar2.structures[0]
    assert fvg.kind == "FVG_BULL"
    assert fvg.ts_available == bars[2].ts_available
    assert fvg.status is StructureStatus.ACTIVE

    at_bar4 = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[4].ts_available,
        detector_ids=("fvg@1",),
    )
    assert len(at_bar4.structures) == 1
    assert at_bar4.structures[0].status is StructureStatus.BROKEN


def test_equal_levels_and_confluence_zone_aggregation() -> None:
    # 构造两次触及 120 附近且中间有深度回调的 K 线，触发 EQUAL_HIGHS + SWING_HIGH + ROUND_NUMBER 共振
    pattern = [
        ("100", "101", "99", "100"),
        ("100", "102", "99", "101"),
        ("108", "120.0", "107", "114"),   # Swing high #1 (prominence = 11.0)
        ("108", "109.0", "104", "105"),
        ("104", "106", "100", "101"),
        ("101", "102", "95", "96"),       # Deep pullback
        ("98", "102", "97", "101"),
        ("102", "108", "101", "107"),
        ("110", "120.1", "109", "115"),   # Swing high #2 -> EQUAL_HIGHS
        ("108", "109.5", "105", "106"),
        ("105", "107", "102", "103"),
        ("103", "105", "101", "102"),
    ]
    bars = [_bar(i, *vals) for i, vals in enumerate(pattern)]
    result = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        detector_ids=("swing_levels@1", "equal_levels@1", "round_numbers@1"),
        params=StructureParams(round_step=Decimal("2")),
    )
    kinds = {f.kind for f in result.structures}
    assert "SWING_HIGH" in kinds
    assert "EQUAL_HIGHS" in kinds
    assert "CONFLUENCE_ZONE" in kinds
    confluences = [f for f in result.structures if f.kind == "CONFLUENCE_ZONE"]
    assert all(len(c.constituent_ids) >= 2 for c in confluences)


def test_custom_detector_plugs_into_registry_without_modifying_core() -> None:
    @dataclass(frozen=True)
    class MidpointChannelDetector:
        detector_id: str = "custom_midpoint@1"
        version: str = "1"
        families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL,)

        def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
            assert ctx.analysis_window is not None
            last = ctx.visible[-1]
            mid = (last.high + last.low) / Decimal("2")
            return [
                StructureFeature(
                    structure_id="st_custom_mid_1",
                    instrument_id=ctx.instrument_id,
                    detector_id=self.detector_id,
                    family=StructureFamily.LEVEL,
                    category=StructureCategory.STRUCTURE,
                    kind="CUSTOM_MIDPOINT",
                    timeframe=ctx.timeframe,
                    source_timeframe=ctx.timeframe,
                    geometry=HorizontalLineGeometry(price=mid),
                    strength=0.77,
                    status=StructureStatus.ACTIVE,
                    ts_available=last.ts_available,
                    formed_ts=last.timestamp,
                    analysis_window=ctx.analysis_window,
                )
            ]

    registry: DetectorRegistry = default_structure_registry()
    registry.register_detector(MidpointChannelDetector())
    assert "custom_midpoint@1" in registry.list_detectors()

    bars = _rising_channel_bars(20)
    result = compute_structure_set(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bars=bars,
        cutoff_ts=bars[-1].ts_available,
        detector_ids=("custom_midpoint@1",),
        registry=registry,
    )
    assert len(result.structures) == 1
    assert result.structures[0].kind == "CUSTOM_MIDPOINT"
    assert result.structures[0].detector_id == "custom_midpoint@1"
