"""O(W) 凸包切边全局最优包络线检测器（零确认滞后，面向策略与突破扫描）。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.bars import CompactBar
from talaria.foundation.structures.context import DetectionContext, make_structure_id
from talaria.foundation.structures.contracts import (
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAnchor,
    StructureCategory,
    StructureFamily,
    StructureFeature,
    StructureStatus,
)


def _cross(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _lower_hull(points: list[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
    """Andrew's Monotone Chain 下凸包：返回按 x 升序的支撑顶点列表。"""

    hull: list[tuple[int, float, float]] = []
    for pt in points:
        while len(hull) >= 2 and _cross(hull[-2][1], hull[-2][2], hull[-1][1], hull[-1][2], pt[1], pt[2]) <= 0.0:
            hull.pop()
        hull.append(pt)
    return hull


def _upper_hull(points: list[tuple[int, float, float]]) -> list[tuple[int, float, float]]:
    """Andrew's Monotone Chain 上凸包：返回按 x 升序的阻力顶点列表。"""

    hull: list[tuple[int, float, float]] = []
    for pt in points:
        while len(hull) >= 2 and _cross(hull[-2][1], hull[-2][2], hull[-1][1], hull[-1][2], pt[1], pt[2]) >= 0.0:
            hull.pop()
        hull.append(pt)
    return hull


@dataclass(frozen=True, slots=True)
class ConvexEnvelopeDetector:
    """固定回望窗口下的上/下凸包最优切线（严格无穿越、绝对因果无前视）。"""

    detector_id: str = "convex_envelope@1"
    version: str = "1"
    families: tuple[StructureFamily, ...] = (StructureFamily.TRENDLINE,)

    def detect(self, ctx: DetectionContext) -> list[StructureFeature]:
        bars = ctx.visible
        if len(bars) < 5 or ctx.analysis_window is None:
            return []

        w = min(ctx.params.envelope_window, len(bars))
        start_idx = len(bars) - w
        latest_island = ctx.island_ids[-1]
        # 仅在同一连续无断流孤岛内取末尾窗口，避免跨缺口拉出畸变斜率
        if ctx.params.respect_islands:
            while start_idx < len(bars) - 1 and ctx.island_ids[start_idx] != latest_island:
                start_idx += 1
        window_bars: list[CompactBar] = [
            b
            for b in bars[start_idx:]
            if b.low is not None and b.high is not None and b.close is not None
        ]
        if len(window_bars) < 5:
            return []

        scale = ctx.params.trendline_scale
        t0 = window_bars[0].timestamp
        bar_ms = max(ctx.bar_ms, 1)

        low_pts: list[tuple[int, float, float]] = []
        high_pts: list[tuple[int, float, float]] = []
        closes_y: list[tuple[float, float]] = []

        for idx, bar in enumerate(window_bars):
            assert bar.low is not None and bar.high is not None and bar.close is not None
            x = (bar.timestamp - t0) / bar_ms
            if scale is PriceScaleMode.LOG:
                if bar.low <= 0 or bar.high <= 0 or bar.close <= 0:
                    return []
                y_low = math.log(float(bar.low))
                y_high = math.log(float(bar.high))
                y_close = math.log(float(bar.close))
            else:
                y_low = float(bar.low)
                y_high = float(bar.high)
                y_close = float(bar.close)
            low_pts.append((idx, x, y_low))
            high_pts.append((idx, x, y_high))
            closes_y.append((x, y_close))

        latest_atr = float(ctx.latest_atr)
        last_close = float(window_bars[-1].close or 1)
        norm_unit = (
            latest_atr / max(last_close, 1e-9)
            if scale is PriceScaleMode.LOG
            else max(latest_atr, 1e-9)
        )
        ts_available = window_bars[-1].ts_available
        features: list[StructureFeature] = []

        for hull_pts, is_support, kind in (
            (_lower_hull(low_pts), True, "ENVELOPE_SUPPORT"),
            (_upper_hull(high_pts), False, "ENVELOPE_RESISTANCE"),
        ):
            if len(hull_pts) < 2:
                continue
            best_edge: tuple[int, int, float] | None = None
            for h_idx in range(len(hull_pts) - 1):
                i1, x1, y1 = hull_pts[h_idx]
                i2, x2, y2 = hull_pts[h_idx + 1]
                if x2 <= x1:
                    continue
                slope = (y2 - y1) / (x2 - x1)
                sse = 0.0
                for x_k, y_close_k in closes_y:
                    y_hat = y1 + slope * (x_k - x1)
                    err = y_close_k - y_hat
                    sse += err * err
                if best_edge is None or sse < best_edge[2]:
                    best_edge = (i1, i2, sse)

            if best_edge is None:
                continue
            i1, i2, sse = best_edge
            b1, b2 = window_bars[i1], window_bars[i2]
            raw_p1 = b1.low if is_support else b1.high
            raw_p2 = b2.low if is_support else b2.high
            assert raw_p1 is not None and raw_p2 is not None

            # 将凸包切线投影至窗口首尾端点，使整段回望窗口均可直接求值 geometry_bounds_at
            x1 = (b1.timestamp - t0) / bar_ms
            x2 = (b2.timestamp - t0) / bar_ms
            x_last = (window_bars[-1].timestamp - t0) / bar_ms
            if scale is PriceScaleMode.LOG:
                y1 = math.log(float(raw_p1))
                y2 = math.log(float(raw_p2))
                slope = (y2 - y1) / (x2 - x1)
                p_start_val = math.exp(y1 + slope * (0.0 - x1))
                p_end_val = math.exp(y1 + slope * (x_last - x1))
            else:
                y1 = float(raw_p1)
                y2 = float(raw_p2)
                slope = (y2 - y1) / (x2 - x1)
                p_start_val = y1 + slope * (0.0 - x1)
                p_end_val = y1 + slope * (x_last - x1)

            anchor_start_price = Decimal(f"{p_start_val:.8f}")
            anchor_end_price = Decimal(f"{p_end_val:.8f}")
            if scale is PriceScaleMode.LOG and (anchor_start_price <= 0 or anchor_end_price <= 0):
                continue

            rmse_norm = math.sqrt(sse / len(closes_y)) / max(norm_unit, 1e-9)
            strength = round(max(0.05, min(0.95, 0.85 - 0.08 * rmse_norm)), 3)

            features.append(
                StructureFeature(
                    structure_id=make_structure_id(
                        ctx.instrument_id,
                        self.detector_id,
                        f"{kind}_{scale.value}",
                        ctx.timeframe,
                        b1.timestamp,
                        raw_p1,
                        tag=f"{b2.timestamp}:{raw_p2}:{len(window_bars)}",
                    ),
                    instrument_id=ctx.instrument_id,
                    detector_id=self.detector_id,
                    family=StructureFamily.TRENDLINE,
                    category=StructureCategory.TREND,
                    kind=kind,
                    timeframe=ctx.timeframe,
                    source_timeframe=ctx.timeframe,
                    geometry=SlopeLineGeometry(
                        anchor_start=StructureAnchor(
                            ts=window_bars[0].timestamp,
                            price=anchor_start_price,
                        ),
                        anchor_end=StructureAnchor(
                            ts=window_bars[-1].timestamp,
                            price=anchor_end_price,
                        ),
                        scale=scale,
                        extend_right=True,
                    ),
                    strength=strength,
                    status=StructureStatus.ACTIVE,
                    ts_available=ts_available,
                    formed_ts=window_bars[0].timestamp,
                    analysis_window=ctx.analysis_window,
                    touch_count=2,
                    touch_timestamps=[b1.timestamp, b2.timestamp],
                    last_touch_ts=b2.timestamp,
                    parameters=ctx.params.as_parameters(),
                )
            )
        return features
