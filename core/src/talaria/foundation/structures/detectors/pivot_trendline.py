"""确认摆动点自动趋势线检测器（面向看盘工作台与人机协同分析）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from talaria.foundation.structures.context import ConfirmedPivot, DetectionContext, make_structure_id
from talaria.foundation.structures.contracts import (
    AnchorSource,
    SlopeLineGeometry,
    StructureAnchor,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)
from talaria.foundation.structures.geometry import geometry_bounds_at


def _collect_multiscale_pivots(ctx: DetectionContext) -> tuple[list[ConfirmedPivot], list[ConfirmedPivot]]:
    """收集多尺度确认 Pivot 并按 ``(side, index)`` 去重。"""

    min_index = max(0, len(ctx.visible) - ctx.params.trendline_lookback_bars)
    by_key: dict[tuple[str, int], ConfirmedPivot] = {}
    for span in ctx.params.trendline_spans:
        for pivot in ctx.confirmed_pivots(span=span, confirm_bars=max(span, ctx.params.confirm_bars)):
            if pivot.index < min_index:
                continue
            key = (pivot.side, pivot.index)
            existing = by_key.get(key)
            if existing is None or pivot.span > existing.span or pivot.prominence > existing.prominence:
                by_key[key] = pivot

    def select_side(side: str) -> list[ConfirmedPivot]:
        items = [p for p in by_key.values() if p.side == side]
        # 若数量超过预算，优先保留最显著的锚点，再按时间升序排列
        if len(items) > ctx.params.trendline_max_pivots:
            items.sort(key=lambda p: (-float(p.prominence), p.index))
            items = items[: ctx.params.trendline_max_pivots]
        items.sort(key=lambda p: p.index)
        return items

    return select_side("LOW"), select_side("HIGH")


def _anchor_price(pivot: ConfirmedPivot, source: AnchorSource) -> Decimal:
    return pivot.body_price if source is AnchorSource.BODY else pivot.price


@dataclass(frozen=True, slots=True)
class PivotTrendlineDetector:
    """多尺度确认摆动点趋势线：缺口隔离 + 实体/影线穿刺过滤 + 多因子评分。"""

    detector_id: str = "pivot_trendline@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.TRENDLINE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        bars = ctx.visible
        if len(bars) < ctx.params.min_anchor_span + 2 or ctx.analysis_window is None:
            return []

        lows, highs = _collect_multiscale_pivots(ctx)
        params = ctx.params
        scale = params.trendline_scale
        source = params.trendline_anchor_source
        body_pierce_mult = Decimal(str(params.max_body_pierce_atr))
        wick_pierce_mult = Decimal(str(params.max_wick_pierce_atr))
        touch_mult = Decimal(str(params.trendline_touch_atr))
        features: list[StructureFeature] = []

        for side_pivots, is_support, kind in (
            (lows, True, "STRUCTURAL_SUPPORT"),
            (highs, False, "STRUCTURAL_RESISTANCE"),
        ):
            n_pivots = len(side_pivots)
            for i in range(n_pivots):
                p1 = side_pivots[i]
                price1 = _anchor_price(p1, source)
                if scale.value == "LOG" and price1 <= 0:
                    continue
                for j in range(i + 1, n_pivots):
                    p2 = side_pivots[j]
                    if p2.index - p1.index < params.min_anchor_span:
                        continue
                    if params.respect_islands and p1.island_id != p2.island_id:
                        continue
                    price2 = _anchor_price(p2, source)
                    if scale.value == "LOG" and price2 <= 0:
                        continue
                    if p1.formed_ts >= p2.formed_ts:
                        continue

                    geometry = SlopeLineGeometry(
                        anchor_start=StructureAnchor(ts=p1.formed_ts, price=price1),
                        anchor_end=StructureAnchor(ts=p2.formed_ts, price=price2),
                        scale=scale,
                        extend_right=True,
                    )

                    valid = True
                    wick_pierce_count = 0
                    post_break_pierced = False
                    dev_atrs: list[float] = []
                    touch_indices: list[int] = []
                    total_bars = len(bars) - p1.index

                    for k in range(p1.index, len(bars)):
                        bar = bars[k]
                        if (
                            bar.open is None
                            or bar.close is None
                            or bar.high is None
                            or bar.low is None
                        ):
                            continue
                        bounds = geometry_bounds_at(geometry, bar.timestamp)
                        if bounds is None:
                            valid = False
                            break
                        line_price = bounds.lower
                        atr_k = ctx.bar_atr(k)
                        body_low = min(bar.open, bar.close)
                        body_high = max(bar.open, bar.close)

                        if is_support:
                            body_pen = line_price - body_low
                            wick_pen = line_price - bar.low
                            dist = abs(bar.low - line_price)
                        else:
                            body_pen = body_high - line_price
                            wick_pen = bar.high - line_price
                            dist = abs(bar.high - line_price)

                        # 两锚点之间禁止任何有效实体/超限影线穿透；锚点之后允许末尾刚破位（交由生命周期判 BROKEN）
                        in_anchor_span = k <= p2.index
                        is_tail_zone = k >= len(bars) - 6
                        if body_pen > atr_k * body_pierce_mult:
                            if in_anchor_span or not is_tail_zone:
                                valid = False
                                break
                            post_break_pierced = True
                        elif wick_pen > atr_k * wick_pierce_mult:
                            if in_anchor_span or not is_tail_zone:
                                valid = False
                                break
                            wick_pierce_count += 1
                        elif wick_pen > Decimal("0") and k not in (p1.index, p2.index):
                            wick_pierce_count += 1

                        if not post_break_pierced and dist <= atr_k * touch_mult:
                            if not touch_indices or (k - touch_indices[-1]) >= params.min_touch_gap:
                                touch_indices.append(k)

                        mid = (bar.high + bar.low) / Decimal("2")
                        dev_atrs.append(float(abs(mid - line_price) / atr_k))

                    if not valid:
                        continue
                    pierce_ratio = wick_pierce_count / max(1, total_bars)
                    if pierce_ratio > params.max_pierce_ratio:
                        continue

                    touch_count = max(2, len(touch_indices))
                    touch_score = min(1.0, (touch_count - 1) / 4.0)
                    span_score = min(
                        1.0,
                        (len(bars) - 1 - p1.index) / max(1.0, 0.6 * params.trendline_lookback_bars),
                    )
                    med_dev = median(dev_atrs) if dev_atrs else 4.0
                    fit_score = max(0.0, 1.0 - med_dev / 4.0)
                    last_touch_idx = touch_indices[-1] if touch_indices else p2.index
                    age_bars = max(0, len(bars) - 1 - last_touch_idx)
                    recency_score = math.exp(-age_bars / params.trendline_recency_tau)
                    pierce_penalty = 0.2 * min(1.0, pierce_ratio / max(params.max_pierce_ratio, 1e-6))

                    raw_strength = (
                        0.35 * touch_score
                        + 0.25 * span_score
                        + 0.20 * fit_score
                        + 0.20 * recency_score
                        - pierce_penalty
                    )
                    strength = round(max(0.05, min(0.95, raw_strength)), 3)
                    ts_available = max(p1.ts_available, p2.ts_available)
                    touch_ts = [bars[idx].timestamp for idx in touch_indices] or [p1.formed_ts, p2.formed_ts]

                    features.append(
                        StructureFeature(
                            structure_id=make_structure_id(
                                ctx.instrument_id,
                                self.detector_id,
                                f"{kind}_{scale.value}_{source.value}",
                                ctx.timeframe,
                                p1.formed_ts,
                                price1,
                                tag=f"{p2.formed_ts}:{price2}",
                            ),
                            instrument_id=ctx.instrument_id,
                            detector_id=self.detector_id,
                            family=StructureFamily.TRENDLINE,
                            category=StructureCategory.TREND,
                            kind=kind,
                            timeframe=ctx.timeframe,
                            source_timeframe=ctx.timeframe,
                            geometry=geometry,
                            strength=strength,
                            status=StructureStatus.ACTIVE,
                            ts_available=ts_available,
                            formed_ts=p1.formed_ts,
                            analysis_window=ctx.analysis_window,
                            touch_count=touch_count,
                            touch_timestamps=touch_ts,
                            last_touch_ts=touch_ts[-1],
                            parameters=params.as_parameters(),
                        )
                    )
        return features
