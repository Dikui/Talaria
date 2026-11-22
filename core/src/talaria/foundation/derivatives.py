"""衍生品数据契约：标记价、资金费率结算与持仓量。

衍生品是市场能力而非核心分支：连接器按 ``DerivativeDataConnector`` 协议提供
历史点，Market Pack 通过 ``DerivativeCapabilityTrait`` 声明支持的种类。核心
只处理 ``DerivativePoint``，不出现任何交易所或资产类别判断。

每个点携带五重时点：交易所事件时间 ``ts_event``、发布/结算时间
``ts_published``（交易所未公开时为 ``None``）、可用时间 ``ts_available`` 与
精度 ``precision``、本地接收时间 ``ts_ingested``、提交时间 ``ts_committed``。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Iterable, Protocol, Sequence, runtime_checkable

from pydantic import Field, model_validator

from talaria.foundation.types import (
    ContractModel,
    DecimalStr,
    PrecisionTier,
)

OI_TIMEFRAME_BY_TIMEFRAME: dict[str, str] = {
    "1m": "5m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d",
}
"""持仓量最小粒度为 5 分钟；1 分钟图使用 5 分钟持仓量序列。"""


class DerivativeKind(StrEnum):
    """衍生品序列种类。"""

    MARK_PRICE = "mark_price"
    FUNDING_RATE = "funding_rate"
    OPEN_INTEREST = "open_interest"


class DerivativePoint(ContractModel):
    """一个带五重时点的衍生品观测值。"""

    instrument_id: str
    kind: DerivativeKind
    timeframe: str | None = None
    value: DecimalStr
    ts_event: int
    ts_published: int | None = None
    ts_available: int
    precision: PrecisionTier = PrecisionTier.SECOND_VERIFIED
    ts_ingested: int
    ts_committed: int
    revision: int = Field(default=0, ge=0)

    @property
    def is_funding_settlement(self) -> bool:
        """结算历史有交易所结算时点，ticker 费率是当期的实时观测。"""
        return self.kind is DerivativeKind.FUNDING_RATE and self.ts_published == self.ts_event

    @model_validator(mode="after")
    def _timestamps_are_ordered(self) -> DerivativePoint:
        if self.ts_available < self.ts_event:
            raise ValueError("ts_available 不能早于 ts_event")
        if self.ts_committed < self.ts_ingested:
            raise ValueError("ts_committed 不能早于 ts_ingested")
        return self


def derivative_identity(point: DerivativePoint) -> tuple[str, str, str, bool, int]:
    return (
        point.instrument_id, point.kind.value, point.timeframe or "",
        point.is_funding_settlement, point.ts_event,
    )


def latest_visible_derivatives(
    points: Iterable[DerivativePoint], *, as_of: int | None = None,
) -> list[DerivativePoint]:
    """先按可获取时间截断，再按种类、周期和事件时间选版。"""
    selected: dict[tuple[str, str, str, bool, int], DerivativePoint] = {}
    for point in points:
        if as_of is not None and point.ts_available > as_of:
            continue
        key = derivative_identity(point)
        previous = selected.get(key)
        if previous is None or (
            point.revision, point.ts_available, point.ts_ingested
        ) >= (previous.revision, previous.ts_available, previous.ts_ingested):
            selected[key] = point
    return sorted(selected.values(), key=lambda point: (point.ts_event, derivative_identity(point)))


class DerivativeSeries(ContractModel):
    """同一标的、同一时点语义的衍生品序列。"""

    kind: DerivativeKind
    timeframe: str | None
    points: list[DerivativePoint]


class DerivativeSummary(ContractModel):
    """快照内最新衍生品读数；缺失值为 ``None``，前端不做任何补算。"""

    mark_price: DecimalStr | None = None
    mark_price_ts: int | None = None
    funding_rate: DecimalStr | None = None
    funding_settled_ts: int | None = None
    next_funding_ts: int | None = None
    open_interest: DecimalStr | None = None
    open_interest_ts: int | None = None
    oi_history_available: bool = False


class DerivativeBundle(ContractModel):
    """``GET /api/snapshots/{id}/derivatives`` 的响应。"""

    snapshot_id: str
    instrument_id: str
    timeframe: str
    cutoff_ts: int
    series: list[DerivativeSeries]
    summary: DerivativeSummary


@dataclass(frozen=True, slots=True)
class RawDerivativePoint:
    """连接器返回的原始衍生品点，尚未绑定标的。"""

    kind: DerivativeKind
    symbol: str
    timeframe: str | None
    ts_event: int
    value: Decimal
    ts_available: int
    ts_ingested: int
    ts_committed: int
    ts_published: int | None = None
    precision: PrecisionTier = PrecisionTier.SECOND_VERIFIED

    def to_point(self, instrument_id: str) -> DerivativePoint:
        return DerivativePoint(
            instrument_id=instrument_id,
            kind=self.kind,
            timeframe=self.timeframe,
            value=self.value,
            ts_event=self.ts_event,
            ts_published=self.ts_published,
            ts_available=self.ts_available,
            precision=self.precision,
            ts_ingested=self.ts_ingested,
            ts_committed=self.ts_committed,
        )


@runtime_checkable
class DerivativeDataConnector(Protocol):
    """免密钥衍生品历史数据能力；由具体 Market Pack 的连接器实现。"""

    def fetch_mark_price_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 1000,
    ) -> Sequence[RawDerivativePoint]:
        """标记价 K 线，仅返回已闭合且 ``ts_event <= end_ts`` 的项。"""

    def fetch_funding_settlements(
        self,
        *,
        symbol: str,
        end_ts: int | None = None,
        limit: int = 200,
    ) -> Sequence[RawDerivativePoint]:
        """资金费率结算历史，仅返回 ``ts_event <= end_ts`` 的项。"""

    def fetch_open_interest(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 200,
    ) -> Sequence[RawDerivativePoint]:
        """持仓量序列（最小 5 分钟），仅返回已闭合的项。"""
