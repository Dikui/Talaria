"""K 线状态与紧凑柱序列契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import model_validator

from talaria.foundation.types import ContractModel, DecimalStr, PrecisionTier


class BarStatus(StrEnum):
    """一根紧凑柱的来源状态。"""

    NORMAL = "NORMAL"
    NO_TRADES = "NO_TRADES"
    FEED_GAP = "FEED_GAP"


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
