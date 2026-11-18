"""快照、图表分页与错误包裹契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from talaria.foundation.bars import CompactBar
from talaria.foundation.types import ContractModel


class SnapshotMode(StrEnum):
    """快照的时间语义。"""

    HISTORICAL_PIT = "HISTORICAL_PIT"
    OBSERVED_REPLAY = "OBSERVED_REPLAY"


class SnapshotRequest(ContractModel):
    """创建快照的请求。``cutoff_ts`` 为空时使用服务端当前时间。"""

    instrument_id: str
    timeframes: list[str] = Field(min_length=1)
    cutoff_ts: int | None = None
    mode: SnapshotMode = SnapshotMode.HISTORICAL_PIT


class SnapshotMeta(ContractModel):
    """快照清单：读取时只使用这里记录的字段与文件路径。"""

    snapshot_id: str
    instrument_id: str
    timeframes: list[str]
    cutoff_ts: int
    created_ts: int
    mode: SnapshotMode
    bar_counts: dict[str, int]


class ChartPage(ContractModel):
    """图表分页：``bars`` 按时间升序，并标明两个方向是否还有更多数据。"""

    snapshot_id: str
    instrument_id: str
    timeframe: str
    bars: list[CompactBar]
    has_more_forward: bool
    has_more_backward: bool


class ErrorResponse(ContractModel):
    """所有非 2xx 响应的统一包裹。"""

    code: str
    message: str
