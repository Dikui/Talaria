"""统一市场结构离线 PIT 质量评估契约测试。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

import pytest

from talaria.evaluation.structures import (
    StructureEvaluationConfig,
    dumps_structure_report,
    evaluate_structures,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.structures import (
    DetectionContext,
    DetectorRegistry,
    HorizontalLineGeometry,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
    default_structure_registry,
)
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.spec]

MINUTE = 60_000
T0 = 1_700_000_000_000
INSTRUMENT = "BTC-USDT-PERP"


def _bar(index: int, open_: str, high: str, low: str, close: str) -> CompactBar:
    ts = T0 + index * MINUTE
    return CompactBar(
        timestamp=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("10"),
        turnover=Decimal("10") * Decimal(close),
        status=BarStatus.NORMAL,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _market_bars(count: int = 85) -> list[CompactBar]:
    bars: list[CompactBar] = []
    for i in range(count):
        center = Decimal("100") + Decimal(i) * Decimal("0.15") + Decimal((i % 12) - 6) * Decimal("0.6")
        open_ = center + Decimal("0.3")
        close = center - Decimal("0.3")
        high = max(open_, close) + Decimal("1.4")
        low = min(open_, close) - Decimal("1.4")
        bars.append(_bar(i, str(open_), str(high), str(low), str(close)))
    return bars


def test_structure_evaluation_reproducible_and_multi_geometry() -> None:
    bars = _market_bars()
    cfg = StructureEvaluationConfig(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        from_ts=bars[25].timestamp,
        to_ts=bars[65].timestamp,
        atr_multiple=0.8,
        horizon_bars=8,
        baseline_samples=20,
        seed=42,
        detector_ids=("swing_levels@1", "pivot_trendline@1", "convex_envelope@1"),
    )
    report1 = evaluate_structures(bars, cfg)
    report2 = evaluate_structures(bars, cfg)

    assert report1 == report2
    assert report1.churn_rate is not None
    assert 0.0 <= report1.churn_rate <= 1.0
    detectors_seen = {row.detector_id for row in report1.results}
    assert "convex_envelope@1" in detectors_seen

    payload = json.loads(dumps_structure_report(report1))
    assert payload["method_version"] == "structures@1"
    assert len(payload["results"]) == len(report1.results)


def test_structure_evaluation_supports_custom_registered_detector() -> None:
    @dataclass(frozen=True)
    class FixedBandDetector:
        detector_id: str = "fixed_anchor@1"
        version: str = "1"
        families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL,)

        def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
            assert ctx.analysis_window is not None
            first = ctx.visible[0]
            return [
                StructureFeature(
                    structure_id="st_fixed_eval",
                    instrument_id=ctx.instrument_id,
                    detector_id=self.detector_id,
                    family=StructureFamily.LEVEL,
                    category=StructureCategory.STRUCTURE,
                    kind="FIXED_ANCHOR",
                    timeframe=ctx.timeframe,
                    source_timeframe=ctx.timeframe,
                    geometry=HorizontalLineGeometry(price=Decimal("102")),
                    strength=0.8,
                    status=StructureStatus.ACTIVE,
                    ts_available=first.ts_available,
                    formed_ts=first.timestamp,
                    analysis_window=ctx.analysis_window,
                )
            ]

    bars = _market_bars(75)
    registry: DetectorRegistry = default_structure_registry()
    registry.register_detector(FixedBandDetector())

    cfg = StructureEvaluationConfig(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        from_ts=bars[20].timestamp,
        to_ts=bars[55].timestamp,
        horizon_bars=5,
        baseline_samples=10,
        detector_ids=("fixed_anchor@1",),
    )
    report = evaluate_structures(bars, cfg, registry=registry)
    assert len(report.results) == 1
    assert report.results[0].detector_id == "fixed_anchor@1"
    assert report.results[0].kind == "FIXED_ANCHOR"
