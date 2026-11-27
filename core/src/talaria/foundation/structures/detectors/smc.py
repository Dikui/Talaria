"""无前视 SMC/ICT 增量检测器：三柱失衡缺口（FVG）与等高/等低流动性池（EQH/EQL）。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.structures.context import ConfirmedPivot, DetectionContext, make_structure_id
from talaria.foundation.structures.contracts import (
    HorizontalZoneGeometry,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)


def _clamp_strength(value: float) -> float:
    return round(max(0.05, min(0.95, value)), 3)


@dataclass(frozen=True, slots=True)
class FvgDetector:
    """三柱 Fair Value Gap 检测器；严格在第 3 根柱收盘时可见，零前视。"""

    detector_id: str = "fvg@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.ZONE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        bars = ctx.visible
        if len(bars) < 3 or ctx.analysis_window is None:
            return []
        min_mult = Decimal(str(ctx.params.fvg_atr_min))
        features: list[StructureFeature] = []
        start_idx = max(2, len(bars) - ctx.params.trendline_lookback_bars)

        for idx in range(start_idx, len(bars)):
            if not (ctx.island_ids[idx - 2] == ctx.island_ids[idx - 1] == ctx.island_ids[idx]):
                continue
            b0, b1, b2 = bars[idx - 2], bars[idx - 1], bars[idx]
            if (
                b0.high is None
                or b0.low is None
                or b2.high is None
                or b2.low is None
            ):
                continue
            atr = ctx.bar_atr(idx)
            min_gap = atr * min_mult
            if b2.low > b0.high and (b2.low - b0.high) >= min_gap:
                gap_low, gap_high = b0.high, b2.low
                kind = "FVG_BULL"
            elif b2.high < b0.low and (b0.low - b2.high) >= min_gap:
                gap_low, gap_high = b2.high, b0.low
                kind = "FVG_BEAR"
            else:
                continue

            ratio = float((gap_high - gap_low) / atr) if atr > 0 else 1.0
            strength = _clamp_strength(0.35 + min(0.45, ratio * 0.25))
            features.append(
                StructureFeature(
                    structure_id=make_structure_id(
                        ctx.instrument_id,
                        self.detector_id,
                        kind,
                        ctx.timeframe,
                        b1.timestamp,
                        gap_low,
                    ),
                    instrument_id=ctx.instrument_id,
                    detector_id=self.detector_id,
                    family=StructureFamily.ZONE,
                    category=StructureCategory.GAP,
                    kind=kind,
                    timeframe=ctx.timeframe,
                    source_timeframe=ctx.timeframe,
                    geometry=HorizontalZoneGeometry(
                        price_low=gap_low,
                        price_high=gap_high,
                        start_ts=b0.timestamp,
                    ),
                    strength=strength,
                    status=StructureStatus.ACTIVE,
                    ts_available=b2.ts_available,
                    formed_ts=b1.timestamp,
                    analysis_window=ctx.analysis_window,
                    parameters=ctx.params.as_parameters(),
                )
            )
        return features


@dataclass(frozen=True, slots=True)
class EqualLevelsDetector:
    """基于确认摆动点的等高（EQH）/ 等低（EQL）流动性池检测器。"""

    detector_id: str = "equal_levels@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.ZONE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        if ctx.analysis_window is None or not ctx.visible:
            return []
        pivots = ctx.confirmed_pivots()
        if len(pivots) < 2:
            return []

        tol_mult = Decimal(str(ctx.params.equal_level_atr))
        pullback_mult = Decimal(str(ctx.params.equal_pullback_atr))
        features: list[StructureFeature] = []

        for side, kind in (("HIGH", "EQUAL_HIGHS"), ("LOW", "EQUAL_LOWS")):
            side_pivots = [p for p in pivots if p.side == side]
            used: set[int] = set()
            for i, anchor in enumerate(side_pivots):
                if i in used:
                    continue
                cluster: list[ConfirmedPivot] = [anchor]
                atr_ref = anchor.atr if anchor.atr and anchor.atr > 0 else ctx.bar_atr(anchor.index)
                tol = atr_ref * tol_mult
                min_pullback = atr_ref * pullback_mult
                for j in range(i + 1, len(side_pivots)):
                    candidate = side_pivots[j]
                    if abs(candidate.price - anchor.price) > tol:
                        continue
                    prev = cluster[-1]
                    if candidate.index - prev.index < 2:
                        continue
                    between = ctx.visible[prev.index + 1 : candidate.index]
                    if side == "HIGH":
                        lows = [b.low for b in between if b.low is not None]
                        has_pullback = bool(lows) and (min(anchor.price, candidate.price) - min(lows)) >= min_pullback
                    else:
                        highs = [b.high for b in between if b.high is not None]
                        has_pullback = bool(highs) and (max(highs) - max(anchor.price, candidate.price)) >= min_pullback
                    if has_pullback:
                        cluster.append(candidate)
                        used.add(j)

                if len(cluster) < 2:
                    continue
                used.add(i)
                zone_low = min(p.price for p in cluster)
                zone_high = max(p.price for p in cluster)
                earliest = cluster[0]
                ts_available = max(p.ts_available for p in cluster)
                strength = _clamp_strength(0.45 + 0.12 * min(len(cluster), 4))
                features.append(
                    StructureFeature(
                        structure_id=make_structure_id(
                            ctx.instrument_id,
                            self.detector_id,
                            kind,
                            ctx.timeframe,
                            earliest.formed_ts,
                            zone_low,
                        ),
                        instrument_id=ctx.instrument_id,
                        detector_id=self.detector_id,
                        family=StructureFamily.ZONE,
                        category=StructureCategory.STRUCTURE,
                        kind=kind,
                        timeframe=ctx.timeframe,
                        source_timeframe=ctx.timeframe,
                        geometry=HorizontalZoneGeometry(
                            price_low=zone_low,
                            price_high=zone_high,
                            start_ts=earliest.formed_ts,
                        ),
                        strength=strength,
                        status=StructureStatus.ACTIVE,
                        ts_available=ts_available,
                        formed_ts=earliest.formed_ts,
                        analysis_window=ctx.analysis_window,
                        touch_count=len(cluster),
                        touch_timestamps=[p.formed_ts for p in cluster],
                        last_touch_ts=cluster[-1].formed_ts,
                        parameters=ctx.params.as_parameters(),
                    )
                )
        return features
