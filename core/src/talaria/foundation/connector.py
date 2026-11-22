"""行情连接器协议与标准化原始柱。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.types import PrecisionTier


@dataclass(frozen=True, slots=True)
class RawBar:
    """连接器输出的标准化原始柱；金额保持 Decimal，不经过 float。"""

    symbol: str
    timeframe: str
    ts_event: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    turnover: Decimal
    ts_available: int
    ts_ingested: int
    ts_committed: int
    precision: PrecisionTier = PrecisionTier.SECOND_VERIFIED
    revision: int = 0
    # 仅热缓冲需要这些状态，冷库与原始归档只保存确认的历史柱。
    confirmed: bool = True

    def to_compact(self, *, status: BarStatus = BarStatus.NORMAL) -> CompactBar:
        if status is BarStatus.FEED_GAP:
            raise ValueError("原始柱不能转换为 FEED_GAP")
        return CompactBar(
            timestamp=self.ts_event,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
            turnover=self.turnover,
            status=status,
            ts_available=self.ts_available,
            precision=self.precision,
        )


class MarketDataConnector(Protocol):
    """向更早历史分页拉取已闭合 K 线。"""

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 1000,
    ) -> Sequence[RawBar]:
        """返回 ``ts_event <= end_ts`` 且按 ``ts_event`` 升序的至多 ``limit`` 根柱。

        ``end_ts`` 为空表示从最新一根开始。实现必须排除尚未闭合的当前柱，并在
        上游不可用时抛 ``FeedUnavailableError``。
        """
