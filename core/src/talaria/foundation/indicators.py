"""指标快通道的对外契约：请求、单序列、摘要与批量结果。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from talaria.foundation.operators import (
    CHANNELS,
    DEFAULT_PERIODS,
    MAX_PERIOD,
    IndicatorName,
    pane_for,
)
from talaria.foundation.types import ContractModel, DecimalStr


class IndicatorParams(ContractModel):
    """一个指标实例的参数；BOLL 额外使用 ``std_dev``。"""

    period: int = Field(ge=1, le=MAX_PERIOD)
    std_dev: DecimalStr | None = None
    anchor: Literal["snapshot", "session"] | None = None

    @classmethod
    def defaults_for(cls, name: IndicatorName) -> IndicatorParams:
        return cls(
            period=DEFAULT_PERIODS[name],
            std_dev=None,
            anchor="session" if name is IndicatorName.VWAP else None,
        )


class IndicatorSummary(ContractModel):
    """序列末端的可用值，以及预热与有效点数。"""

    latest: dict[str, DecimalStr | None]
    warmup_bars: int = Field(ge=0)
    valid_points: int = Field(ge=0)


class IndicatorPoint(ContractModel):
    """一根紧凑柱上的指标值；键为通道名（BOLL 为 mid/upper/lower）。"""

    timestamp: int
    values: dict[str, DecimalStr | None]


class IndicatorSeries(ContractModel):
    """一个指标实例的完整序列；``points`` 与请求窗口严格等长同序。"""

    name: IndicatorName
    pane: Literal["main", "sub"]
    params: IndicatorParams
    points: list[IndicatorPoint]
    summary: IndicatorSummary


class IndicatorRequest(ContractModel):
    """批量指标请求；省略参数时使用各指标默认值。"""

    snapshot_id: str
    timeframe: str
    names: list[str] = Field(min_length=1, max_length=len(IndicatorName))
    params: dict[str, IndicatorParams] | None = None
    limit: int | None = Field(default=None, ge=1, le=5000)


class OperatorRequest(ContractModel):
    """单指标算子请求；允许传入非默认参数。"""

    snapshot_id: str
    timeframe: str
    operator: IndicatorName
    params: IndicatorParams | None = None
    limit: int | None = Field(default=None, ge=1, le=5000)


class IndicatorBundle(ContractModel):
    """同一快照、周期下的一组指标序列。"""

    snapshot_id: str
    instrument_id: str
    timeframe: str
    series: list[IndicatorSeries]


class IndicatorDefaultsResponse(ContractModel):
    """全部指标当前生效的默认参数。"""

    defaults: dict[IndicatorName, IndicatorParams]


__all__ = [
    "CHANNELS",
    "IndicatorBundle",
    "IndicatorDefaultsResponse",
    "IndicatorName",
    "IndicatorParams",
    "IndicatorPoint",
    "IndicatorRequest",
    "IndicatorSeries",
    "IndicatorSummary",
    "OperatorRequest",
    "pane_for",
]
