"""DSH 只读工具的输入契约。

这些模型只用于生成 DSH 工具 schema；真正的校验与计算由 Talaria 守护进程
的 Python 服务完成。价格窗口刻意用字符串接收，避免 JS 侧出现浮点重算。
"""

from __future__ import annotations

from pydantic import Field

from talaria.foundation.types import ContractModel


class ToolBase(ContractModel):
    snapshot_id: str = Field(min_length=1, max_length=64)


class GetIndicatorsInput(ToolBase):
    timeframe: str = Field(min_length=1, max_length=8)
    names: list[str] = Field(min_length=1, max_length=20)
    limit: int = Field(default=300, ge=1, le=5000)


class GetKeyLevelsInput(ToolBase):
    timeframe: str = Field(min_length=1, max_length=8)
    categories: list[str] = Field(default_factory=list, max_length=6)
    limit: int = Field(default=20, ge=1, le=500)
    min_strength: float = Field(default=0.0, ge=0.0, le=1.0)


class GetStructuresInput(ToolBase):
    timeframe: str = Field(min_length=1, max_length=16)
    families: list[str] = Field(default_factory=list, max_length=5)
    categories: list[str] = Field(default_factory=list, max_length=8)
    detector_ids: list[str] = Field(default_factory=list, max_length=16)
    htf_timeframes: list[str] = Field(default_factory=list, max_length=4)
    scale: str | None = Field(default=None, max_length=16)
    min_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    limit: int = Field(default=40, ge=1, le=500)


class ComputeOperatorInput(ToolBase):
    timeframe: str = Field(min_length=1, max_length=8)
    operator: str = Field(min_length=1, max_length=32)
    params: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=300, ge=1, le=5000)


class GetChartViewInput(ToolBase):
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    from_ts: int = Field(ge=0)
    to_ts: int = Field(ge=0)
    price_min: str = Field(min_length=1, max_length=40)
    price_max: str = Field(min_length=1, max_length=40)
