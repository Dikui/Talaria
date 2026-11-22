"""人工画线（HUMAN_DRAWING）契约与可见性判定。

画线坐标全部是「时间-价格」结构化图元：时间戳是 UTC 毫秒整数，价格一律使用
``DecimalStr``，避免 JSON number 在往返中丢精度。像素换算只发生在浏览器最后一跳，
本模块不包含任何绘制逻辑。

可见性规则与任务单 05 对齐：

- ``visible_timeframes`` 为空表示「全周期可见」；否则只有列出的周期能看到；
- 单点图元（水平线、文本标签）在时间轴上无限延伸，只要价格落在窗口内即可见；
- 射线从第一个锚点向时间轴右侧无限延伸，因此左边界不限制可见性；
- 其余图元按锚点包围盒与时间/价格窗口求交。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from talaria.foundation.market import InstrumentSpec
from talaria.foundation.types import ContractModel, DecimalStr


class DrawingKind(StrEnum):
    """任务单 05 支持的七种人工画线图元。"""

    HORIZONTAL_LINE = "HORIZONTAL_LINE"
    RAY = "RAY"
    SEGMENT = "SEGMENT"
    PARALLEL_CHANNEL = "PARALLEL_CHANNEL"
    FIB_RETRACEMENT = "FIB_RETRACEMENT"
    RECTANGLE = "RECTANGLE"
    TEXT_LABEL = "TEXT_LABEL"


class DrawingLineStyle(StrEnum):
    """线型只影响前端绘制，不影响坐标语义。"""

    SOLID = "solid"
    DASHED = "dashed"
    DOTTED = "dotted"


#: 每种图元必须提供的锚点数量；顺序即前端交互的取点顺序。
KIND_POINT_COUNTS: dict[DrawingKind, int] = {
    DrawingKind.HORIZONTAL_LINE: 1,
    DrawingKind.RAY: 2,
    DrawingKind.SEGMENT: 2,
    DrawingKind.PARALLEL_CHANNEL: 3,
    DrawingKind.FIB_RETRACEMENT: 2,
    DrawingKind.RECTANGLE: 2,
    DrawingKind.TEXT_LABEL: 1,
}

#: 斐波那契回撤档位；0 在 p0，1 在 p1。
FIB_LEVELS: tuple[Decimal, ...] = (
    Decimal("0"),
    Decimal("0.236"),
    Decimal("0.382"),
    Decimal("0.5"),
    Decimal("0.618"),
    Decimal("0.786"),
    Decimal("1"),
)


class DrawingPoint(ContractModel):
    """一个锚点：UTC 毫秒时间戳 + 十进制字符串价格。"""

    ts: int = Field(ge=0)
    price: DecimalStr

    @field_validator("price")
    @classmethod
    def _finite_price(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("price 必须是有限十进制数")
        return value


class DrawingStyle(ContractModel):
    """画线样式；后端只做范围校验，不解释颜色。"""

    color: str = Field(default="#4c8dff", min_length=4, max_length=32)
    line_width: int = Field(default=1, ge=1, le=6)
    line_style: DrawingLineStyle = DrawingLineStyle.SOLID
    fill_opacity: float = Field(default=0.12, ge=0.0, le=1.0)


def _validate_point_count(kind: DrawingKind, points: list[DrawingPoint]) -> None:
    expected = KIND_POINT_COUNTS[kind]
    if len(points) != expected:
        raise ValueError(f"{kind.value} 需要 {expected} 个锚点，收到 {len(points)} 个")


class DrawingCreate(ContractModel):
    """创建画线请求；缺失字段使用默认值。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    kind: DrawingKind
    visible_timeframes: list[str] = Field(default_factory=list, max_length=16)
    points: list[DrawingPoint] = Field(min_length=1, max_length=8)
    text: str = Field(default="", max_length=500)
    style: DrawingStyle = Field(default_factory=DrawingStyle)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        _validate_point_count(self.kind, self.points)
        if self.kind is DrawingKind.TEXT_LABEL and not self.text.strip():
            raise ValueError("TEXT_LABEL 的 text 不能为空")
        return self


class DrawingPatch(ContractModel):
    """移动/改点/改文本/改样式；至少提供一个字段，``kind`` 不可变。"""

    visible_timeframes: list[str] | None = Field(default=None, max_length=16)
    points: list[DrawingPoint] | None = Field(default=None, min_length=1, max_length=8)
    text: str | None = Field(default=None, max_length=500)
    style: DrawingStyle | None = None

    @model_validator(mode="after")
    def _check_non_empty(self) -> Self:
        if (
            self.visible_timeframes is None
            and self.points is None
            and self.text is None
            and self.style is None
        ):
            raise ValueError("PATCH 至少要提供一个可修改字段")
        return self


class DrawingRecord(ContractModel):
    """一条画线的某个版本；``deleted_at`` 非空表示软删除。"""

    drawing_id: str
    instrument_id: str
    kind: DrawingKind
    visible_timeframes: list[str]
    points: list[DrawingPoint]
    text: str
    style: DrawingStyle
    version: int = Field(ge=1)
    created_ts: int
    updated_ts: int
    deleted_at: int | None = None


class DrawingListResponse(ContractModel):
    """``GET /api/drawings`` 的可见画线集合。"""

    instrument_id: str
    timeframe: str
    drawings: list[DrawingRecord]


class DrawingVersionList(ContractModel):
    """一条画线的版本链，按版本号升序。"""

    drawing_id: str
    versions: list[DrawingRecord]


class DrawingClearResponse(ContractModel):
    """按 Instrument 清理的软删除计数。"""

    instrument_id: str
    cleared: int


class ChartViewWindow(ContractModel):
    """``get_chart_view`` 返回的可视时间-价格窗口。"""

    from_ts: int
    to_ts: int
    price_min: DecimalStr
    price_max: DecimalStr


class ChartView(ContractModel):
    """共享分析接口 ``get_chart_view``：窗口内只读的画线视图。"""

    instrument: InstrumentSpec
    timeframe: str
    window: ChartViewWindow
    drawings: list[DrawingRecord]


def _time_extent(
    kind: DrawingKind, points: list[DrawingPoint]
) -> tuple[int | None, int | None]:
    """返回图元在时间轴上的闭区间；``None`` 分别表示向历史/未来无限延伸。"""

    stamps = [point.ts for point in points]
    if kind in (DrawingKind.HORIZONTAL_LINE, DrawingKind.TEXT_LABEL):
        return None, None
    if kind is DrawingKind.RAY:
        # 射线从 p0 穿过 p1 无限延伸：p1 在右侧向右延伸，否则向左延伸。
        if stamps[1] >= stamps[0]:
            return stamps[0], None
        return None, stamps[0]
    return min(stamps), max(stamps)


def visible_in_time(
    kind: DrawingKind,
    points: list[DrawingPoint],
    from_ts: int | None,
    to_ts: int | None,
) -> bool:
    """判断图元是否与时间窗口 ``[from_ts, to_ts]`` 相交。"""

    if from_ts is None and to_ts is None:
        return True
    start, end = _time_extent(kind, points)
    if end is not None and from_ts is not None and end < from_ts:
        return False
    if start is not None and to_ts is not None and start > to_ts:
        return False
    return True


def _price_span(
    kind: DrawingKind, points: list[DrawingPoint]
) -> tuple[Decimal, Decimal]:
    """图元在价格轴上覆盖的闭区间（射线/平行通道按锚点近似）。"""

    prices = [point.price for point in points]
    return min(prices), max(prices)


def visible_in_price(
    kind: DrawingKind,
    points: list[DrawingPoint],
    price_min: Decimal | None,
    price_max: Decimal | None,
) -> bool:
    """判断图元是否与可视价格区间相交。"""

    if price_min is None and price_max is None:
        return True
    low, high = _price_span(kind, points)
    if price_min is not None and high < price_min:
        return False
    if price_max is not None and low > price_max:
        return False
    return True


def visible_in_window(
    record: DrawingRecord,
    *,
    timeframe: str | None,
    from_ts: int | None,
    to_ts: int | None,
    price_min: Decimal | None = None,
    price_max: Decimal | None = None,
) -> bool:
    """组合周期、时间与价格三重可见性判定。"""

    if timeframe is not None and record.visible_timeframes and timeframe not in record.visible_timeframes:
        return False
    return visible_in_time(
        record.kind, record.points, from_ts, to_ts
    ) and visible_in_price(record.kind, record.points, price_min, price_max)
