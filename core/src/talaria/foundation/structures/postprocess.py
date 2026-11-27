"""市场结构可插拔后处理管线：生命周期、聚类去重、多源共振聚合、预算裁剪。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.bars import BarStatus
from talaria.foundation.structures.context import DetectionContext, make_structure_id
from talaria.foundation.structures.contracts import (
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)
from talaria.foundation.structures.detectors.legacy_levels import LEGACY_LEVEL_DETECTORS
from talaria.foundation.structures.geometry import geometry_bounds_at, geometry_distance

_QUANTUM = Decimal("0.000001")

_CONFLUENCE_CATEGORY_WEIGHT: dict[StructureCategory, float] = {
    StructureCategory.STRUCTURE: 1.15,
    StructureCategory.VOLUME: 1.15,
    StructureCategory.SESSION: 1.0,
    StructureCategory.GAP: 0.9,
    StructureCategory.ROUND_NUMBER: 0.75,
    StructureCategory.LIQUIDATION: 0.85,
    StructureCategory.TREND: 1.0,
    StructureCategory.CONFLUENCE: 0.0,
}


def _decimal_weight(part: Decimal, total: Decimal) -> Decimal:
    if total <= 0:
        return Decimal("0")
    return (part / total).quantize(_QUANTUM)


@dataclass(frozen=True, slots=True)
class LifecyclePostProcessor:
    """统一通过 ``geometry_bounds_at`` 评估触及统计与 ACTIVE/BROKEN/FLIPPED/EXPIRED。"""

    processor_id: str = "lifecycle@1"

    def process(
        self, ctx: DetectionContext, features: list[StructureFeature]
    ) -> list[StructureFeature]:
        bars = ctx.visible
        if not bars:
            return features
        latest_ts = bars[-1].timestamp
        latest_close = bars[-1].close
        latest_atr = ctx.latest_atr
        updated: list[StructureFeature] = []

        for item in features:
            if item.detector_id in LEGACY_LEVEL_DETECTORS or item.family is StructureFamily.CONFLUENCE:
                updated.append(item)
                continue

            is_slope = isinstance(item.geometry, SlopeLineGeometry)
            touch_mult = Decimal(
                str(ctx.params.trendline_touch_atr if is_slope else ctx.params.touch_atr)
            )
            break_mult = Decimal(
                str(ctx.params.trendline_break_atr if is_slope else 0)
            )

            touch_ts: list[int] = []
            touched_vol = Decimal("0")
            total_vol = Decimal("0")
            break_dir: str | None = None
            last_break_idx: int | None = None

            for idx, bar in enumerate(bars):
                if bar.status is BarStatus.FEED_GAP or bar.high is None or bar.low is None:
                    continue
                if bar.timestamp < item.formed_ts:
                    continue
                bounds = geometry_bounds_at(item.geometry, bar.timestamp)
                if bounds is None:
                    continue
                atr_i = ctx.bar_atr(idx)
                tol = atr_i * touch_mult
                vol = bar.volume or Decimal("0")
                total_vol += vol

                if bar.high >= bounds.lower - tol and bar.low <= bounds.upper + tol:
                    if not is_slope or not touch_ts or (bar.timestamp - touch_ts[-1]) >= ctx.bar_ms * ctx.params.min_touch_gap:
                        touch_ts.append(bar.timestamp)
                        touched_vol += vol

                if bar.ts_available > item.ts_available and bar.close is not None:
                    break_tol = atr_i * break_mult
                    is_support_only = item.kind in {"FVG_BULL", "EQUAL_LOWS"} or item.kind.endswith("_SUPPORT")
                    is_resistance_only = item.kind in {"FVG_BEAR", "EQUAL_HIGHS"} or item.kind.endswith("_RESISTANCE")
                    if bar.close > bounds.upper + break_tol and not is_support_only:
                        if break_dir is None:
                            break_dir = "up"
                        last_break_idx = idx
                    elif bar.close < bounds.lower - break_tol and not is_resistance_only:
                        if break_dir is None:
                            break_dir = "down"
                        last_break_idx = idx

            status = StructureStatus.ACTIVE
            if break_dir is not None and latest_close is not None:
                latest_bounds = geometry_bounds_at(item.geometry, latest_ts)
                if latest_bounds is not None:
                    break_tol = latest_atr * break_mult
                    if latest_bounds.lower <= latest_close <= latest_bounds.upper:
                        status = StructureStatus.ACTIVE
                    elif break_dir == "up":
                        status = (
                            StructureStatus.BROKEN
                            if latest_close > latest_bounds.upper + break_tol
                            else StructureStatus.FLIPPED
                        )
                    else:
                        status = (
                            StructureStatus.BROKEN
                            if latest_close < latest_bounds.lower - break_tol
                            else StructureStatus.FLIPPED
                        )
                    if (
                        is_slope
                        and status is StructureStatus.ACTIVE
                        and geometry_distance(latest_close, latest_bounds) > latest_atr * Decimal("10")
                    ):
                        status = StructureStatus.EXPIRED
                elif last_break_idx is not None:
                    status = StructureStatus.BROKEN

            merged_touches = touch_ts or list(item.touch_timestamps)
            updated.append(
                item.model_copy(
                    update={
                        "status": status,
                        "touch_count": max(item.touch_count, len(merged_touches)),
                        "touch_timestamps": merged_touches,
                        "last_touch_ts": merged_touches[-1] if merged_touches else item.last_touch_ts,
                        "volume_weight": _decimal_weight(touched_vol, total_vol)
                        if total_vol > 0
                        else item.volume_weight,
                    }
                )
            )
        return updated


@dataclass(frozen=True, slots=True)
class DeduplicatorPostProcessor:
    """在归一化二维特征空间聚类去重相似趋势线。"""

    processor_id: str = "deduplicator@1"

    def process(
        self, ctx: DetectionContext, features: list[StructureFeature]
    ) -> list[StructureFeature]:
        if not ctx.visible:
            return features
        latest_ts = ctx.visible[-1].timestamp
        atr_now = float(ctx.latest_atr)
        if atr_now <= 0:
            return features
        bar_ms = max(ctx.bar_ms, 1)

        non_trendlines: list[StructureFeature] = []
        trend_groups: dict[tuple[str, str], list[StructureFeature]] = {}
        for item in features:
            if item.family is StructureFamily.TRENDLINE and isinstance(item.geometry, SlopeLineGeometry):
                trend_groups.setdefault((item.detector_id, item.kind), []).append(item)
            else:
                non_trendlines.append(item)

        kept_trends: list[StructureFeature] = []
        slope_tol = ctx.params.trendline_dedup_slope_atr
        price_tol = ctx.params.trendline_dedup_price_atr

        for group_key in sorted(trend_groups):
            candidates = sorted(
                trend_groups[group_key],
                key=lambda x: (-x.strength, -x.ts_available, x.structure_id),
            )
            accepted: list[tuple[float, float]] = []
            for item in candidates:
                geom = item.geometry
                assert isinstance(geom, SlopeLineGeometry)
                bounds_now = geometry_bounds_at(geom, latest_ts)
                if bounds_now is None:
                    continue
                proj_now = float(bounds_now.lower)
                bars_span = max(1.0, (geom.anchor_end.ts - geom.anchor_start.ts) / bar_ms)
                if geom.scale is PriceScaleMode.LOG:
                    p_end = float(geom.anchor_end.price)
                    p_start = float(geom.anchor_start.price)
                    # 按当前绝对价位换算等价每柱斜率
                    slope_per_bar = proj_now * math.log(p_end / p_start) / bars_span
                else:
                    slope_per_bar = float(geom.anchor_end.price - geom.anchor_start.price) / bars_span
                norm_slope = slope_per_bar / atr_now
                norm_price = proj_now / atr_now

                if any(
                    abs(norm_slope - s0) < slope_tol and abs(norm_price - p0) < price_tol
                    for s0, p0 in accepted
                ):
                    continue
                accepted.append((norm_slope, norm_price))
                kept_trends.append(item)

        return [*non_trendlines, *kept_trends]


@dataclass(frozen=True, slots=True)
class ConfluencePostProcessor:
    """确定性高斯核密度聚合：将多源邻近水平结构合成为 ``CONFLUENCE_ZONE``。"""

    processor_id: str = "confluence@1"

    def process(
        self, ctx: DetectionContext, features: list[StructureFeature]
    ) -> list[StructureFeature]:
        if ctx.analysis_window is None or not ctx.visible:
            return features

        atr_now = float(ctx.latest_atr)
        if atr_now <= 0:
            return features

        # 收集所有 ACTIVE 水平位与水平带代表点
        sources: list[tuple[StructureFeature, list[float], float]] = []
        min_price = float("inf")
        max_price = float("-inf")
        for item in features:
            if item.status is not StructureStatus.ACTIVE:
                continue
            weight = _CONFLUENCE_CATEGORY_WEIGHT.get(item.category, 0.0) * item.strength
            if weight <= 0:
                continue
            if isinstance(item.geometry, HorizontalLineGeometry):
                p = float(item.geometry.price)
                sources.append((item, [p], weight))
                min_price = min(min_price, p)
                max_price = max(max_price, p)
            elif isinstance(item.geometry, HorizontalZoneGeometry):
                low = float(item.geometry.price_low)
                high = float(item.geometry.price_high)
                pts = [low, (low + high) / 2.0, high] if high > low else [low]
                sources.append((item, pts, weight))
                min_price = min(min_price, low)
                max_price = max(max_price, high)

        if len(sources) < ctx.params.confluence_min_constituents:
            return features

        h = max(1e-9, ctx.params.confluence_bandwidth_atr * atr_now)
        step = max(1e-9, 0.1 * atr_now)
        grid_start = min_price - h
        grid_end = max_price + h
        n_steps = min(2000, max(3, int((grid_end - grid_start) / step) + 1))
        actual_step = (grid_end - grid_start) / max(1, n_steps - 1)

        grid_prices = [grid_start + i * actual_step for i in range(n_steps)]
        densities: list[float] = []
        for gp in grid_prices:
            density = 0.0
            for _, pts, weight in sources:
                per_pt_w = weight / len(pts)
                for pt in pts:
                    z = (gp - pt) / h
                    density += per_pt_w * math.exp(-0.5 * z * z)
            densities.append(density)

        # 提取局部极大值
        peak_indices = [
            i
            for i in range(1, n_steps - 1)
            if densities[i] >= densities[i - 1] and densities[i] > densities[i + 1]
        ]
        if not peak_indices:
            return features

        max_density = max(densities[i] for i in peak_indices)
        if max_density <= 0:
            return features

        zones: list[StructureFeature] = []
        min_constituents = ctx.params.confluence_min_constituents
        for idx in peak_indices:
            peak_p = grid_prices[idx]
            peak_d = densities[idx]
            # 找覆盖在该峰 ±h 内的独立成员
            members = [
                item
                for item, pts, _ in sources
                if any(abs(pt - peak_p) <= h for pt in pts)
            ]
            if len(members) < min_constituents:
                continue

            # 半峰宽边界
            half_d = peak_d * 0.5
            left = idx
            while left > 0 and densities[left - 1] >= half_d:
                left -= 1
            right = idx
            while right < n_steps - 1 and densities[right + 1] >= half_d:
                right += 1

            low_dec = Decimal(str(round(grid_prices[left], 8))).quantize(_QUANTUM)
            high_dec = Decimal(str(round(grid_prices[right], 8))).quantize(_QUANTUM)
            if low_dec > high_dec:
                low_dec, high_dec = high_dec, low_dec

            member_ids = sorted(m.structure_id for m in members)
            ts_available = max(m.ts_available for m in members)
            formed_ts = min(m.formed_ts for m in members)
            strength = round(
                max(0.05, min(0.95, 0.35 + 0.55 * (peak_d / max_density))), 3
            )
            zones.append(
                StructureFeature(
                    structure_id=make_structure_id(
                        ctx.instrument_id,
                        self.processor_id,
                        "CONFLUENCE_ZONE",
                        ctx.timeframe,
                        formed_ts,
                        low_dec,
                        tag=",".join(member_ids[:4]),
                    ),
                    instrument_id=ctx.instrument_id,
                    detector_id=self.processor_id,
                    family=StructureFamily.CONFLUENCE,
                    category=StructureCategory.CONFLUENCE,
                    kind="CONFLUENCE_ZONE",
                    timeframe=ctx.timeframe,
                    source_timeframe=ctx.timeframe,
                    geometry=HorizontalZoneGeometry(
                        price_low=low_dec,
                        price_high=high_dec,
                    ),
                    strength=strength,
                    status=StructureStatus.ACTIVE,
                    ts_available=ts_available,
                    formed_ts=formed_ts,
                    analysis_window=ctx.analysis_window,
                    touch_count=sum(m.touch_count for m in members),
                    constituent_ids=member_ids,
                    parameters=ctx.params.as_parameters(),
                )
            )

        return [*features, *zones]


@dataclass(frozen=True, slots=True)
class BudgetFilterPostProcessor:
    """按结构家族限额裁剪并确定性排序，防止前端渲染过载。"""

    processor_id: str = "budget_filter@1"

    def process(
        self, ctx: DetectionContext, features: list[StructureFeature]
    ) -> list[StructureFeature]:
        levels: list[StructureFeature] = []
        confluences: list[StructureFeature] = []
        channels: list[StructureFeature] = []
        trend_groups: dict[tuple[str, str], list[StructureFeature]] = {}

        for item in features:
            if item.family in (StructureFamily.LEVEL, StructureFamily.ZONE):
                levels.append(item)
            elif item.family is StructureFamily.CONFLUENCE:
                confluences.append(item)
            elif item.family is StructureFamily.CHANNEL:
                channels.append(item)
            elif item.family is StructureFamily.TRENDLINE:
                trend_groups.setdefault((item.detector_id, item.kind), []).append(item)

        def rank_key(item: StructureFeature) -> tuple[float, int, str]:
            return (-item.strength, -item.ts_available, item.structure_id)

        kept: list[StructureFeature] = []
        kept.extend(sorted(levels, key=rank_key)[: ctx.params.max_levels])
        kept.extend(sorted(confluences, key=rank_key)[: ctx.params.confluence_max_zones])
        kept.extend(sorted(channels, key=rank_key)[: ctx.params.max_trendlines_per_side * 2])
        for group_key in sorted(trend_groups):
            kept.extend(
                sorted(trend_groups[group_key], key=rank_key)[
                    : ctx.params.max_trendlines_per_side
                ]
            )
        return sorted(kept, key=rank_key)
