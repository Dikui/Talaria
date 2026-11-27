"""市场结构几何原语纯函数求值与画线采纳映射。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.drawings import DrawingKind, DrawingPoint
from talaria.foundation.operators.core import with_operator_context
from talaria.foundation.structures.contracts import (
    ChannelGeometry,
    HorizontalLineGeometry,
    HorizontalZoneGeometry,
    PriceScaleMode,
    SlopeLineGeometry,
    StructureAnchor,
    StructureGeometry,
)

_PRICE_QUANTUM = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class GeometryBounds:
    """给定时刻的上下边界；单线结构 ``lower == upper``。"""

    lower: Decimal
    upper: Decimal


def _quantize_price(value: Decimal) -> Decimal:
    return value.quantize(_PRICE_QUANTUM)


def interpolate_slope_price(
    anchor_start: StructureAnchor,
    anchor_end: StructureAnchor,
    ts: int,
    *,
    scale: PriceScaleMode = PriceScaleMode.LINEAR,
) -> Decimal:
    """在线性或对数坐标系下插值/外推斜线在 ``ts`` 时刻的价格。"""

    t1, p1 = anchor_start.ts, anchor_start.price
    t2, p2 = anchor_end.ts, anchor_end.price
    if t2 == t1 or ts == t1:
        return p1
    if ts == t2:
        return p2
    if p1 == p2:
        return p1
    ratio = Decimal(ts - t1) / Decimal(t2 - t1)
    if scale is PriceScaleMode.LOG:
        if p1 <= 0 or p2 <= 0:
            raise ValueError("LOG 坐标插值要求锚点价格 > 0")
        log_delta = (p2 / p1).ln() * ratio
        return _quantize_price(p1 * log_delta.exp())
    return _quantize_price(p1 + (p2 - p1) * ratio)


def _slope_bounds(geometry: SlopeLineGeometry, ts: int) -> GeometryBounds | None:
    if ts < geometry.anchor_start.ts:
        return None
    if not geometry.extend_right and ts > geometry.anchor_end.ts:
        return None
    price = interpolate_slope_price(
        geometry.anchor_start,
        geometry.anchor_end,
        ts,
        scale=geometry.scale,
    )
    return GeometryBounds(price, price)


@with_operator_context
def geometry_bounds_at(geometry: StructureGeometry, ts: int) -> GeometryBounds | None:
    """返回任意几何原语在 ``ts`` 时刻的价格上下界；不可见时返回 ``None``。"""

    if isinstance(geometry, HorizontalLineGeometry):
        return GeometryBounds(geometry.price, geometry.price)

    if isinstance(geometry, HorizontalZoneGeometry):
        if geometry.start_ts is not None and ts < geometry.start_ts:
            return None
        if geometry.end_ts is not None and ts > geometry.end_ts:
            return None
        return GeometryBounds(geometry.price_low, geometry.price_high)

    if isinstance(geometry, SlopeLineGeometry):
        return _slope_bounds(geometry, ts)

    if isinstance(geometry, ChannelGeometry):
        upper = _slope_bounds(geometry.upper, ts)
        lower = _slope_bounds(geometry.lower, ts)
        if upper is None or lower is None:
            return None
        return GeometryBounds(
            min(lower.lower, upper.lower),
            max(lower.upper, upper.upper),
        )

    raise TypeError(f"不支持的几何原语：{type(geometry)!r}")


def geometry_distance(price: Decimal, bounds: GeometryBounds) -> Decimal:
    """价格到边界的距离；落在区间内部返回 ``Decimal('0')``。"""

    if price < bounds.lower:
        return bounds.lower - price
    if price > bounds.upper:
        return price - bounds.upper
    return Decimal("0")


def geometry_to_drawing(
    geometry: StructureGeometry,
    *,
    fallback_ts: int,
    bar_ms: int,
) -> tuple[DrawingKind, list[DrawingPoint]]:
    """将市场结构几何原语映射为人工画线 ``DrawingKind`` 与锚点序列。"""

    span_ms = max(bar_ms, 1) * 10
    if isinstance(geometry, HorizontalLineGeometry):
        return DrawingKind.HORIZONTAL_LINE, [
            DrawingPoint(ts=fallback_ts, price=geometry.price)
        ]

    if isinstance(geometry, HorizontalZoneGeometry):
        start = geometry.start_ts if geometry.start_ts is not None else fallback_ts
        end = geometry.end_ts if geometry.end_ts is not None else start + span_ms
        if end <= start:
            end = start + span_ms
        return DrawingKind.RECTANGLE, [
            DrawingPoint(ts=start, price=geometry.price_high),
            DrawingPoint(ts=end, price=geometry.price_low),
        ]

    if isinstance(geometry, SlopeLineGeometry):
        kind = DrawingKind.RAY if geometry.extend_right else DrawingKind.SEGMENT
        return kind, [
            DrawingPoint(ts=geometry.anchor_start.ts, price=geometry.anchor_start.price),
            DrawingPoint(ts=geometry.anchor_end.ts, price=geometry.anchor_end.price),
        ]

    if isinstance(geometry, ChannelGeometry):
        return DrawingKind.PARALLEL_CHANNEL, [
            DrawingPoint(
                ts=geometry.lower.anchor_start.ts,
                price=geometry.lower.anchor_start.price,
            ),
            DrawingPoint(
                ts=geometry.lower.anchor_end.ts,
                price=geometry.lower.anchor_end.price,
            ),
            DrawingPoint(
                ts=geometry.upper.anchor_start.ts,
                price=geometry.upper.anchor_start.price,
            ),
        ]

    raise TypeError(f"不支持的几何原语：{type(geometry)!r}")
