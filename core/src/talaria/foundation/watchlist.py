"""Watchlist（自选观察池）契约：分组、排序与行情展示。

任务单 08 只提供手工分组 CRUD 与排序；``SCANNER_FOCUS`` 是扫描器动态分组，
本单只读展示，写入由任务单 15 接入。分组与条目的持久化落在
``state/collaboration_state.sqlite``，所有写操作经 ``StateWriterQueue`` 串行化。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from talaria.foundation.feed import FeedHealthStatus
from talaria.foundation.types import ContractModel, DecimalStr


class WatchlistGroupKind(StrEnum):
    """分组来源：手工维护，或由扫描器动态归集。"""

    MANUAL = "MANUAL"
    SCANNER_FOCUS = "SCANNER_FOCUS"


class QuoteSource(StrEnum):
    """最新价的来源；前端必须能区分实时流与快照/回退。"""

    SUPERVISOR = "SUPERVISOR"
    SNAPSHOT = "SNAPSHOT"
    UNAVAILABLE = "UNAVAILABLE"


class DegradationReason(StrEnum):
    """降级原因：实时配额不足，或根本没有实时流。"""

    QUOTA = "QUOTA"
    NO_FEED = "NO_FEED"


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name:
        raise ValueError("分组名称不能为空")
    return name


class WatchlistGroupCreate(ContractModel):
    """创建手工分组。"""

    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _clean_name(value)


class WatchlistGroupUpdate(ContractModel):
    """重命名手工分组。"""

    name: str = Field(min_length=1, max_length=60)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return _clean_name(value)


class WatchlistItemCreate(ContractModel):
    """向手工分组添加标的。"""

    group_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)


class WatchlistOrder(ContractModel):
    """分组排序：``ids`` 必须覆盖当前全部分组（系统分组位置固定）。"""

    ids: list[str] = Field(min_length=1)


class WatchlistItemOrder(ContractModel):
    """分组内条目排序：``ids`` 必须与该分组当前条目标识完全一致。"""

    group_id: str = Field(min_length=1)
    ids: list[str]


class WatchlistItem(ContractModel):
    """一个自选标的的实时状态与最近行情。"""

    instrument_id: str
    position: int = Field(ge=0)
    latest_price: DecimalStr | None = None
    change_pct: DecimalStr | None = None
    quote_ts: int | None = Field(default=None, ge=0)
    quote_source: QuoteSource
    realtime: bool
    feed_status: FeedHealthStatus | None = None
    stale: bool
    degraded: bool
    degradation_reason: DegradationReason | None = None

    @model_validator(mode="after")
    def _degradation_matches_reason(self) -> WatchlistItem:
        if self.degraded and self.degradation_reason is None:
            raise ValueError("降级标的必须记录原因")
        if not self.degraded and self.degradation_reason is not None:
            raise ValueError("未降级标的不能带降级原因")
        return self


class WatchlistGroup(ContractModel):
    """标签分组；``is_system`` 为真时前端只读展示。"""

    group_id: str
    name: str
    position: int = Field(ge=0)
    kind: WatchlistGroupKind
    is_system: bool
    items: list[WatchlistItem]


class Watchlist(ContractModel):
    """完整 Watchlist 视图：分组、条目以及实时订阅配额使用情况。"""

    groups: list[WatchlistGroup]
    realtime_quota: int = Field(ge=0)
    realtime_used: int = Field(ge=0)
    generated_ts: int = Field(ge=0)
