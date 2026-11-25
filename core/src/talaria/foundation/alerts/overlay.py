"""画线几何的预警求值：在时间轴上插值出上下边界。

同一份几何同时服务绘制与预警，因此修改画线锚点后下一次求值自然使用
最新几何，不需要重建预警规则。无法用于跨越判定的图元返回 ``None``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from talaria.foundation.drawings import DrawingKind, DrawingPoint, DrawingRecord
from talaria.foundation.operators.core import with_operator_context

#: 矩形/通道等有厚度的图元，方向决定使用上沿还是下沿。
THICK_KINDS = frozenset(
    {
        DrawingKind.PARALLEL_CHANNEL,
        DrawingKind.RECTANGLE,
    }
)


@dataclass(frozen=True, slots=True)
class OverlayBounds:
    """某个时刻的上下边界；单线图元上下边界相同。"""

    lower: Decimal
    upper: Decimal


def _interpolate(first: DrawingPoint, second: DrawingPoint, ts: int) -> Decimal:
    if second.ts == first.ts:
        return first.price
    span = Decimal(second.ts - first.ts)
    progress = Decimal(ts - first.ts)
    return first.price + (second.price - first.price) * progress / span


def _time_span(points: list[DrawingPoint]) -> tuple[int, int]:
    timestamps = [point.ts for point in points]
    return min(timestamps), max(timestamps)


@with_operator_context
def overlay_bounds(
    drawing: DrawingRecord,
    ts: int,
    *,
    ratio: Decimal = Decimal("0.5"),
) -> OverlayBounds | None:
    """返回 ``ts`` 时刻的边界；该时刻图元不可见时返回 ``None``。"""

    points = list(drawing.points)
    if not points or drawing.deleted_at is not None:
        return None
    kind = drawing.kind

    if kind is DrawingKind.HORIZONTAL_LINE:
        return OverlayBounds(points[0].price, points[0].price)

    if kind is DrawingKind.TEXT_LABEL:
        return None

    if kind is DrawingKind.RAY:
        first, second = points[0], points[1]
        if second.ts > first.ts and ts < first.ts:
            return None
        if second.ts < first.ts and ts > first.ts:
            return None
        price = _interpolate(first, second, ts)
        return OverlayBounds(price, price)

    if kind is DrawingKind.SEGMENT:
        first, second = points[0], points[1]
        low, high = _time_span([first, second])
        if ts < low or ts > high:
            return None
        price = _interpolate(first, second, ts)
        return OverlayBounds(price, price)

    if kind is DrawingKind.FIB_RETRACEMENT:
        first, second = points[0], points[1]
        low, high = _time_span([first, second])
        if ts < low or ts > high:
            return None
        price = first.price + (second.price - first.price) * ratio
        return OverlayBounds(price, price)

    if kind is DrawingKind.RECTANGLE:
        first, second = points[0], points[1]
        low, high = _time_span([first, second])
        if ts < low or ts > high:
            return None
        lower = min(first.price, second.price)
        upper = max(first.price, second.price)
        return OverlayBounds(lower, upper)

    if kind is DrawingKind.PARALLEL_CHANNEL:
        first, second, third = points[0], points[1], points[2]
        low, high = _time_span([first, second])
        if ts < low or ts > high:
            return None
        base = _interpolate(first, second, ts)
        anchor = _interpolate(first, second, third.ts)
        offset = third.price - anchor
        return OverlayBounds(base + min(offset, Decimal(0)), base + max(offset, Decimal(0)))

    raise ValueError(f"未知画线类型：{drawing.kind}")
