"""Python 实时订阅协议。序号仅在同一服务代与标的周期内有序。"""

from typing import Literal

from pydantic import Field

from talaria.foundation.bars import RangeBar
from talaria.foundation.types import ContractModel


class SubscriptionCursor(ContractModel):
    epoch: str = Field(min_length=1)
    seq: int = Field(ge=0)


class SubscriptionRequest(ContractModel):
    instrument: str = Field(min_length=1)
    timeframe: str
    owner: str = Field(min_length=1, max_length=64)
    after: SubscriptionCursor | None = None


class SubscriptionReady(SubscriptionCursor):
    type: Literal["ready", "resync"]
    server_ts: int
    started_ts: int


class StrategyBarEvent(ContractModel):
    """范围重同步时，柱修订没有当前服务分配的变更序号。"""

    type: Literal["bar"] = "bar"
    instrument_id: str
    timeframe: str
    epoch: str
    seq: int | None = Field(ge=1)
    bar: RangeBar
