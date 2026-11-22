"""K 线状态与紧凑柱序列契约。"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, field_serializer, model_validator

from talaria.foundation.types import ContractModel, DecimalStr, PrecisionTier, decimal_to_string

MAX_RANGE_BARS = 10_000


class BarStatus(StrEnum):
    """一根紧凑柱的来源状态。"""

    NORMAL = "NORMAL"
    NO_TRADES = "NO_TRADES"
    FEED_GAP = "FEED_GAP"


def classify_flat_bar(
    *,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
) -> BarStatus:
    """零成交量且四价相等判定为 ``NO_TRADES``，其余为 ``NORMAL``。

    实时流与历史压缩共用同一判定，避免两处口径漂移。
    """

    if volume == 0 and open == high == low == close:
        return BarStatus.NO_TRADES
    return BarStatus.NORMAL


class CompactBar(ContractModel):
    """图表可直接消费的紧凑柱；与 KLineChart ``dataList`` 一一对应。

    ``timestamp`` 等于 ``ts_event``（UTC 毫秒）。``FEED_GAP`` 柱保留时间槽，
    但 OHLCV 全为 null，前端可跳过绘制并计数；API 不得丢弃这些柱。
    """

    timestamp: int
    open: DecimalStr | None
    high: DecimalStr | None
    low: DecimalStr | None
    close: DecimalStr | None
    volume: DecimalStr | None
    turnover: DecimalStr | None
    status: BarStatus
    ts_available: int
    precision: PrecisionTier

    @model_validator(mode="after")
    def _check_gap_fields(self) -> Self:
        values = (self.open, self.high, self.low, self.close, self.volume, self.turnover)
        if self.status is BarStatus.FEED_GAP:
            if any(value is not None for value in values):
                raise ValueError("FEED_GAP 柱的 OHLCV 必须全部为 null")
        elif any(value is None for value in values):
            raise ValueError(f"{self.status} 柱的 OHLCV 必须完整")
        return self


class RangeBar(ContractModel):
    """按范围查询返回的一根 K 线，时点均为 UTC 毫秒。"""

    ts_event: int
    open: DecimalStr
    high: DecimalStr
    low: DecimalStr
    close: DecimalStr
    volume: DecimalStr
    turnover: DecimalStr
    ts_available: int
    precision: PrecisionTier
    confirmed: bool = Field(description="已确认收盘。进行中和合成柱为 false。")
    revision: int = Field(ge=0)

    @field_serializer("open", "high", "low", "close", "volume", "turnover", when_used="json")
    def _serialize_decimal(self, value: Decimal) -> str:
        # Parquet 的列精度会补尾零，范围查询在写盘前后使用同一字符串。
        if value == 0:
            return "0"
        text = decimal_to_string(value)
        return text.rstrip("0").rstrip(".") if "." in text else text


class BarsQuery(ContractModel):
    """查询标的、周期与事件时间范围。所有时点均为 UTC 毫秒。"""

    instrument: str = Field(min_length=1)
    timeframe: str
    start: int = Field(ge=0, le=2**63 - 1, description="事件时间下界（含）")
    end: int | None = Field(
        None, ge=0, le=2**63 - 1,
        description=f"事件时间上界（含），省略时取服务当前时间。每次最多查询 {MAX_RANGE_BARS:,} 根。",
    )
    as_of: int | None = Field(None, ge=0, le=2**63 - 1, description="可获取时间上界（含）")
    include_open: bool = Field(False, description="包含进行中的柱，无可见真实柱时可返回合成柱。")
    tail_only: bool = Field(False, description="仅返回清单中尚无的可见柱及修订，历史回填照常写入冷库。")

    @model_validator(mode="after")
    def _check_range(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("end 不得早于 start")
        return self


class BarsResponse(ContractModel):
    """指定标的与周期的 K 线，按事件时间升序排列。"""

    instrument_id: str
    timeframe: str
    bars: list[RangeBar] = Field(max_length=MAX_RANGE_BARS)
