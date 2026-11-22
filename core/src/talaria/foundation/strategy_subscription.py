"""Python 实时订阅协议。序号仅在同一服务代与标的周期内有序。"""

import hashlib
from collections.abc import Iterable, Iterator
from itertools import groupby
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
    include_baseline: bool = False


class SubscriptionReady(SubscriptionCursor):
    type: Literal["ready", "resync"]
    server_ts: int
    started_ts: int
    baseline_count: int = 0


class StrategyBarEvent(ContractModel):
    """范围重同步时，柱修订没有当前服务分配的变更序号。"""

    type: Literal["bar"] = "bar"
    instrument_id: str
    timeframe: str
    epoch: str
    seq: int | None = Field(ge=1)
    bar: RangeBar
    content_id: str


def identified_revisions(bars: Iterable[RangeBar]) -> Iterator[tuple[str, RangeBar]]:
    """输入按事件时间、权威修订排序。重启续号不改变同一份内容的身份。"""
    for _, slot in groupby(bars, key=lambda bar: bar.ts_event):
        occurrences: dict[str, tuple[int, int]] = {}
        for bar in slot:
            payload = bar.model_dump_json(exclude={"revision"})
            digest = hashlib.sha256(payload.encode()).hexdigest()
            previous, count = occurrences.get(digest, (-1, 0))
            if previous == bar.revision:
                continue
            # 同一毫秒内 A → B → A：第二次 A 是可单独重放的权威修订。
            occurrences[digest] = (bar.revision, count + 1)
            yield (f"{digest}.{count}" if count else digest), bar
