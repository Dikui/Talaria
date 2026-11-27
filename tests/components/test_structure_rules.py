from __future__ import annotations

from decimal import Decimal

from talaria.components.rules import compile_rule
from talaria.components.runtime import Ctx, RuleState
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels import EvidenceClass
from talaria.foundation.types import PrecisionTier
from talaria.foundation.rules import (
    AlertLevelEvent,
    Rule,
    RuleBar,
    RuleComparison,
    RuleStructureEvent,
    RuleStructurePrice,
)
from talaria.foundation.structures import (
    AnalysisWindow,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAnchor,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)


def _bar(ts: int, o: str, h: str, l: str, c: str) -> CompactBar:
    return CompactBar(
        timestamp=ts,
        ts_available=ts + 60_000,
        status=BarStatus.NORMAL,
        precision=PrecisionTier.SECOND_VERIFIED,
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(l),
        close=Decimal(c),
        volume=Decimal("10"),
        turnover=Decimal("1000"),
    )


def test_structure_trendline_break_retest_and_price_linear_and_log() -> None:
    t0 = 1_767_225_600_000
    t1 = t0 + 60_000
    t2 = t0 + 120_000
    bars = [
        _bar(t0, "102", "104", "101", "103"),
        _bar(t1, "103", "114", "103", "113"),  # breaks above linear slope (110)
        _bar(t2, "113", "118", "114", "117"),  # retests near projected 115? wait: let low <= 115 < close
    ]
    trend = StructureFeature(
        structure_id="st_trend_1",
        instrument_id="BTC-USDT-PERP",
        detector_id="pivot_trendline@1",
        family=StructureFamily.TRENDLINE,
        category=StructureCategory.TREND,
        kind="TRENDLINE_RESISTANCE",
        evidence_class=EvidenceClass.OBSERVED,
        timeframe="1m",
        source_timeframe="1m",
        geometry=SlopeLineGeometry(
            anchor_start=StructureAnchor(ts=t0, price=Decimal("105")),
            anchor_end=StructureAnchor(ts=t1, price=Decimal("110")),
            scale=PriceScaleMode.LINEAR,
            extend_right=True,
        ),
        strength=0.85,
        status=StructureStatus.ACTIVE,
        ts_available=t0 + 60_000,
        formed_ts=t0,
        touch_count=2,
        volume_weight=Decimal("0"),
        analysis_window=AnalysisWindow(from_ts=t0, to_ts=t1),
    )

    compiled_break = compile_rule(
        Rule(
            version="1",
            timing="bar_close",
            expression=RuleStructureEvent(
                op="structure_event",
                structure_id="st_trend_1",
                event=AlertLevelEvent.BREAK,
                structure_family=StructureFamily.TRENDLINE,
            ),
        )
    )
    ctx_t1 = Ctx(
        "BTC-USDT-PERP",
        "1m",
        0,
        t1 + 60_000,
        structures={"st_trend_1": trend},
        _confirmed_compact=bars[:2],
        _bar_ts=t1,
    )
    obs_break = compiled_break.observe(ctx_t1)
    assert obs_break is not None and obs_break.matched is True
    assert obs_break.threshold == Decimal("110")
    assert obs_break.level_direction == "UP"

    # RETEST state machine across t1 -> t2 (projected price at t2 is 115)
    compiled_retest = compile_rule(
        Rule(
            version="1",
            timing="bar_close",
            expression=RuleStructureEvent(
                op="structure_event",
                structure_id="st_trend_1",
                event=AlertLevelEvent.RETEST,
            ),
        )
    )
    state = RuleState()
    obs_r1 = compiled_retest.observe(
        Ctx(
            "BTC-USDT-PERP",
            "1m",
            0,
            t1 + 60_000,
            structures={"st_trend_1": trend},
            rule_state=state,
            _confirmed_compact=bars[:2],
            _bar_ts=t1,
        )
    )
    assert obs_r1 is not None and obs_r1.matched is False
    obs_r2 = compiled_retest.observe(
        Ctx(
            "BTC-USDT-PERP",
            "1m",
            0,
            t2 + 60_000,
            structures={"st_trend_1": trend},
            rule_state=state,
            _confirmed_compact=bars,
            _bar_ts=t2,
        )
    )
    assert obs_r2 is not None and obs_r2.matched is True
    assert obs_r2.threshold == Decimal("115")

    # RuleStructurePrice with LOG scale HorizontalZone
    zone = StructureFeature(
        structure_id="st_zone_1",
        instrument_id="BTC-USDT-PERP",
        detector_id="fvg@1",
        family=StructureFamily.ZONE,
        category=StructureCategory.GAP,
        kind="FVG_BULL",
        evidence_class=EvidenceClass.OBSERVED,
        timeframe="1m",
        geometry=HorizontalZoneGeometry(
            price_low=Decimal("100"),
            price_high=Decimal("108"),
            start_ts=t0,
            end_ts=None,
        ),
        strength=0.8,
        status=StructureStatus.ACTIVE,
        ts_available=t0 + 60_000,
        formed_ts=t0,
        touch_count=1,
        analysis_window=AnalysisWindow(from_ts=t0, to_ts=t0),
    )
    compiled_price = compile_rule(
        Rule(
            version="1",
            timing="bar_close",
            expression=RuleComparison(
                op="compare",
                test="gt",
                left=RuleBar(source="bar", field="close"),
                right=RuleStructurePrice(
                    source="structure_price",
                    structure_id="st_zone_1",
                    edge="upper",
                ),
            ),
        )
    )
    obs_p = compiled_price.observe(
        Ctx(
            "BTC-USDT-PERP",
            "1m",
            0,
            t1 + 60_000,
            structures={"st_zone_1": zone},
            _confirmed_compact=bars[:2],
            _bar_ts=t1,
        )
    )
    assert obs_p is not None and obs_p.matched is True
    assert obs_p.threshold == Decimal("108")
