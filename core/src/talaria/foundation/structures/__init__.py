"""统一市场结构引擎（L0 纯契约与纯计算）公共出口。"""

from __future__ import annotations

from typing import Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.key_levels.contracts import SessionProfile
from talaria.foundation.structures.context import ConfirmedPivot, DetectionContext, make_structure_id
from talaria.foundation.structures.contracts import (
    STRUCTURE_GEOMETRY_MODELS,
    STRUCTURE_METHOD_VERSION,
    AnalysisWindow,
    AnchorSource,
    ChannelGeometry,
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAdoptionRecord,
    StructureAdoptionRequest,
    StructureAnchor,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureFeedbackSummary,
    StructureGeometry,
    StructureIgnoreList,
    StructureIgnoreRecord,
    StructureIgnoreRequest,
    StructureParams,
    StructureSet,
    StructureStatus,
)
from talaria.foundation.structures.detectors import (
    ConvexEnvelopeDetector,
    EqualLevelsDetector,
    FvgDetector,
    GapLevelsDetector,
    LiquidationZonesDetector,
    PivotTrendlineDetector,
    RoundNumbersDetector,
    SessionLevelsDetector,
    SwingLevelsDetector,
    VolumeProfileDetector,
)
from talaria.foundation.structures.geometry import (
    GeometryBounds,
    geometry_bounds_at,
    geometry_distance,
    geometry_to_drawing,
    interpolate_slope_price,
)
from talaria.foundation.structures.postprocess import (
    BudgetFilterPostProcessor,
    ConfluencePostProcessor,
    DeduplicatorPostProcessor,
    LifecyclePostProcessor,
)
from talaria.foundation.structures.registry import (
    DetectorRegistry,
    PostProcessor,
    StructureDetector,
)


def default_structure_registry() -> DetectorRegistry:
    """构造包含全部 10 个内置检测器与 4 个标准后处理器的默认注册表。"""

    registry = DetectorRegistry()
    registry.register_detector(SwingLevelsDetector())
    registry.register_detector(SessionLevelsDetector())
    registry.register_detector(GapLevelsDetector())
    registry.register_detector(RoundNumbersDetector())
    registry.register_detector(VolumeProfileDetector())
    registry.register_detector(LiquidationZonesDetector())
    registry.register_detector(FvgDetector())
    registry.register_detector(EqualLevelsDetector())
    registry.register_detector(PivotTrendlineDetector())
    registry.register_detector(ConvexEnvelopeDetector())

    registry.register_postprocessor(LifecyclePostProcessor())
    registry.register_postprocessor(DeduplicatorPostProcessor())
    registry.register_postprocessor(ConfluencePostProcessor())
    registry.register_postprocessor(BudgetFilterPostProcessor())
    return registry


def compute_structure_set(
    *,
    instrument_id: str,
    timeframe: str,
    bars: Sequence[CompactBar],
    cutoff_ts: int,
    snapshot_id: str = "",
    as_of_ts: int | None = None,
    session_profile: SessionProfile | None = None,
    params: StructureParams | None = None,
    derivative_points: Sequence[DerivativePoint] = (),
    detector_ids: Sequence[str] | None = None,
    registry: DetectorRegistry | None = None,
) -> StructureSet:
    """在严格 ``ts_available <= cutoff_ts`` 纪律下计算完整的 ``StructureSet``。"""

    ctx = DetectionContext.build(
        instrument_id=instrument_id,
        timeframe=timeframe,
        bars=bars,
        cutoff_ts=cutoff_ts,
        session_profile=session_profile,
        params=params,
        derivative_points=derivative_points,
    )
    active_registry = registry or default_structure_registry()
    return active_registry.compute_structures(
        ctx,
        snapshot_id=snapshot_id,
        as_of_ts=as_of_ts,
        detector_ids=detector_ids,
    )


__all__ = [
    "STRUCTURE_GEOMETRY_MODELS",
    "STRUCTURE_METHOD_VERSION",
    "AnalysisWindow",
    "AnchorSource",
    "BudgetFilterPostProcessor",
    "ChannelGeometry",
    "ConfluencePostProcessor",
    "ConfirmedPivot",
    "ConvexEnvelopeDetector",
    "DeduplicatorPostProcessor",
    "DetectionContext",
    "DetectorRegistry",
    "EqualLevelsDetector",
    "FvgDetector",
    "GapLevelsDetector",
    "GeometryBounds",
    "HorizontalLineGeometry",
    "HorizontalZoneGeometry",
    "LifecyclePostProcessor",
    "LiquidationZonesDetector",
    "PivotTrendlineDetector",
    "PostProcessor",
    "PriceScaleMode",
    "RoundNumbersDetector",
    "SessionLevelsDetector",
    "SlopeLineGeometry",
    "StructureAdoptionRecord",
    "StructureAdoptionRequest",
    "StructureAnchor",
    "StructureCategory",
    "StructureDetector",
    "StructureFamily",
    "StructureFeature",
    "StructureFeedbackSummary",
    "StructureGeometry",
    "StructureIgnoreList",
    "StructureIgnoreRecord",
    "StructureIgnoreRequest",
    "StructureParams",
    "StructureSet",
    "StructureStatus",
    "SwingLevelsDetector",
    "VolumeProfileDetector",
    "compute_structure_set",
    "default_structure_registry",
    "geometry_bounds_at",
    "geometry_distance",
    "geometry_to_drawing",
    "interpolate_slope_price",
    "make_structure_id",
]
