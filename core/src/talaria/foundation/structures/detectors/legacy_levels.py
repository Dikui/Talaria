"""将现有 6 大类 17 种无前视水平关键位封装为标准结构检测器插件。"""

from __future__ import annotations

from dataclasses import dataclass

from talaria.foundation.key_levels.contracts import KeyLevel, KeyLevelParams, VwapAnchor
from talaria.foundation.key_levels.engine import (
    _anchored_vwap_levels,
    _gap_levels,
    _liquidation_levels,
    _round_levels,
    _session_levels,
    _structure_levels,
    _volume_profile_levels,
)
from talaria.foundation.structures.context import DetectionContext
from talaria.foundation.structures.contracts import (
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)

LEGACY_LEVEL_DETECTORS: frozenset[str] = frozenset(
    {
        "swing_levels@1",
        "session_levels@1",
        "gap_levels@1",
        "round_numbers@1",
        "volume_profile@1",
        "liquidation_zones@1",
    }
)


def _to_structure_feature(
    level: KeyLevel,
    *,
    detector_id: str,
    ctx: DetectionContext,
) -> StructureFeature:
    if level.price is not None:
        family = StructureFamily.LEVEL
        geometry: HorizontalLineGeometry | HorizontalZoneGeometry = HorizontalLineGeometry(
            price=level.price
        )
    else:
        assert level.price_low is not None and level.price_high is not None
        family = StructureFamily.ZONE
        geometry = HorizontalZoneGeometry(
            price_low=level.price_low,
            price_high=level.price_high,
        )
    assert ctx.analysis_window is not None
    return StructureFeature(
        structure_id="st_" + level.level_id.removeprefix("kl_"),
        instrument_id=level.instrument_id,
        detector_id=detector_id,
        family=family,
        category=StructureCategory(level.category.value),
        kind=level.kind.value,
        evidence_class=level.evidence_class,
        timeframe=level.timeframe,
        source_timeframe=level.timeframe,
        geometry=geometry,
        strength=level.strength,
        status=StructureStatus(level.status.value),
        ts_available=level.ts_available,
        formed_ts=level.formed_ts,
        analysis_window=ctx.analysis_window,
        touch_count=level.touch_count,
        touch_timestamps=([level.last_touch_ts] if level.last_touch_ts is not None else []),
        last_touch_ts=level.last_touch_ts,
        volume_weight=level.volume_weight,
        constituent_ids=[],
        parameters=dict(level.parameters),
    )


def _convert_all(
    levels: list[KeyLevel], *, detector_id: str, ctx: DetectionContext
) -> list[StructureFeature]:
    return [_to_structure_feature(lv, detector_id=detector_id, ctx=ctx) for lv in levels]


@dataclass(frozen=True, slots=True)
class SwingLevelsDetector:
    detector_id: str = "swing_levels@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL, StructureFamily.ZONE)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params: KeyLevelParams = ctx.params.to_key_level_params()
        levels = _structure_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            atr=list(ctx.atr),
            params=kl_params,
            bar_ms=ctx.bar_ms,
        )
        return _convert_all(levels, detector_id=self.detector_id, ctx=ctx)


@dataclass(frozen=True, slots=True)
class SessionLevelsDetector:
    detector_id: str = "session_levels@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL, StructureFamily.ZONE)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params = ctx.params.to_key_level_params()
        levels = _session_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            params=kl_params,
            bar_ms=ctx.bar_ms,
            profile=ctx.session_profile,
        )
        return _convert_all(levels, detector_id=self.detector_id, ctx=ctx)


@dataclass(frozen=True, slots=True)
class GapLevelsDetector:
    detector_id: str = "gap_levels@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.ZONE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params = ctx.params.to_key_level_params()
        levels = _gap_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            atr=list(ctx.atr),
            params=kl_params,
            bar_ms=ctx.bar_ms,
        )
        return _convert_all(levels, detector_id=self.detector_id, ctx=ctx)


@dataclass(frozen=True, slots=True)
class RoundNumbersDetector:
    detector_id: str = "round_numbers@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params = ctx.params.to_key_level_params()
        levels = _round_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            atr=list(ctx.atr),
            params=kl_params,
            bar_ms=ctx.bar_ms,
        )
        return _convert_all(levels, detector_id=self.detector_id, ctx=ctx)


@dataclass(frozen=True, slots=True)
class VolumeProfileDetector:
    detector_id: str = "volume_profile@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.LEVEL, StructureFamily.ZONE)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params = ctx.params.to_key_level_params()
        levels = _volume_profile_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            atr=list(ctx.atr),
            params=kl_params,
            bar_ms=ctx.bar_ms,
            profile=ctx.session_profile,
        )
        vwap_bars = (
            list(ctx.visible_with_gaps)
            if kl_params.vwap_anchor is VwapAnchor.SESSION
            else list(ctx.visible)
        )
        vwap_levels = _anchored_vwap_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=vwap_bars,
            cutoff_ts=ctx.cutoff_ts,
            params=kl_params,
            bar_ms=ctx.bar_ms,
        )
        return _convert_all(
            [*levels, *vwap_levels], detector_id=self.detector_id, ctx=ctx
        )


@dataclass(frozen=True, slots=True)
class LiquidationZonesDetector:
    detector_id: str = "liquidation_zones@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.ZONE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        kl_params = ctx.params.to_key_level_params()
        levels, _notes = _liquidation_levels(
            instrument_id=ctx.instrument_id,
            timeframe=ctx.timeframe,
            bars=list(ctx.visible),
            cutoff_ts=ctx.cutoff_ts,
            atr=list(ctx.atr),
            params=kl_params,
            bar_ms=ctx.bar_ms,
            derivative_points=ctx.derivative_points,
        )
        return _convert_all(levels, detector_id=self.detector_id, ctx=ctx)
