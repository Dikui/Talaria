"""L0 统一实时行情事件协议。

实时公共流（Bybit）、离线回放与后续 Scanner / AlertEngine 共用这里的契约：
- ``KlineEvent``：一根 K 线的一次发布版本，``revision`` 在同一 timestamp 上递增，
  进行中与已闭合用 ``confirmed`` 区分；
- ``OrderBookEvent``：最优一档买卖盘口；
- ``FeedHealthEvent``：某个标的的供流健康，只允许 LIVE / STALE / RECOVERING；
- ``MarketEventFeed``：订阅、健康与事件监听的统一对外协议。

``FeedTransport`` 是更底层的传输接口：一次连接内把原始帧解析成事件，
断线由 ``FeedTransportError`` 表达，重连与回填策略由上层 ``FeedSupervisor`` 负责。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Callable, Protocol, TypeAlias, runtime_checkable

from pydantic import Field, model_validator

from talaria.foundation.bars import CompactBar
from talaria.foundation.types import ContractModel, DecimalStr


class FeedHealthStatus(StrEnum):
    """供流健康状态。"""

    LIVE = "LIVE"
    STALE = "STALE"
    RECOVERING = "RECOVERING"


class KlineEvent(ContractModel):
    """一次 K 线发布：``revision`` 对同一 timestamp 单调递增，绝不覆盖历史版本。"""

    instrument_id: str
    timeframe: str
    bar: CompactBar
    confirmed: bool
    revision: int = Field(ge=0)


class ReceivedKlineEvent(KlineEvent):
    """传输层收到的 K 线，携带解析时记录的本地接收时刻。"""

    ts_ingested: int = Field(exclude=True)
    # REST 恢复及合成柱只补缺。
    history_only: bool = Field(default=False, exclude=True)


class OrderBookEvent(ContractModel):
    """最优一档盘口；``ts_event`` 为交易所时间，``ts_ingested`` 为本地接收时间。"""

    instrument_id: str
    bid_price: DecimalStr
    bid_size: DecimalStr
    ask_price: DecimalStr
    ask_size: DecimalStr
    ts_event: int
    ts_ingested: int

    @model_validator(mode="after")
    def _bid_not_above_ask(self) -> OrderBookEvent:
        if self.bid_price > self.ask_price:
            raise ValueError("买价不能高于卖价")
        return self


class DerivativeTickEvent(ContractModel):
    """衍生品实时读数：标记价、资金费率、下次结算时间与持仓量。

    一次 ticker 推送可能只带其中一部分字段；全部缺失的推送不构成有效事件。
    """

    instrument_id: str
    mark_price: DecimalStr | None = None
    funding_rate: DecimalStr | None = None
    next_funding_ts: int | None = None
    open_interest: DecimalStr | None = None
    ts_event: int
    ts_ingested: int

    @model_validator(mode="after")
    def _at_least_one_reading(self) -> DerivativeTickEvent:
        if (
            self.mark_price is None
            and self.funding_rate is None
            and self.next_funding_ts is None
            and self.open_interest is None
        ):
            raise ValueError("衍生品事件至少需要一个读数")
        return self


class FeedHealthEvent(ContractModel):
    """某个标的 + 周期的供流健康。``stale_since`` 是最后一次收到传输活动的时刻。

    健康按 ``(instrument_id, timeframe)`` 区分通道：不同周期的订阅与断流互不影响，
    客户端切换周期时不会用旧周期的健康状态覆盖新周期。
    """

    instrument_id: str
    timeframe: str
    status: FeedHealthStatus
    ts: int
    last_event_ts: int | None = None
    stale_since: int | None = None
    reconnect_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _stale_since_matches_status(self) -> FeedHealthEvent:
        if self.status is FeedHealthStatus.STALE and self.stale_since is None:
            raise ValueError("STALE 必须记录断流起点")
        if self.status is FeedHealthStatus.LIVE and self.stale_since is not None:
            raise ValueError("LIVE 不能带断流起点")
        return self


class FeedHealthReport(ContractModel):
    """快通道健康查询的响应：整体状态 + 每个订阅通道的明细。"""

    feed_mode: str
    status: FeedHealthStatus
    channels: list[FeedHealthEvent]


FeedEvent: TypeAlias = (
    KlineEvent | OrderBookEvent | DerivativeTickEvent | FeedHealthEvent
)


class FeedTransport(Protocol):
    """一次实时连接的传输接口；断线以 ``FeedTransportError`` 抛出。"""

    feed_mode: str

    async def connect(self) -> None:
        """建立连接并完成订阅握手；失败抛 ``FeedTransportError``。"""

    async def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        """为后续事件增加订阅。"""

    async def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        """取消订阅。"""

    async def recv(self) -> KlineEvent | OrderBookEvent | DerivativeTickEvent | None:
        """读取下一条行情事件；``None`` 表示心跳活动，传输失联抛 ``FeedTransportError``。"""

    async def close(self) -> None:
        """关闭连接；可重复调用。"""


FeedListener: TypeAlias = Callable[[FeedEvent], None]


@runtime_checkable
class MarketEventFeed(Protocol):
    """实时行情的统一对外协议：Watchlist 调度订阅，Scanner / AlertEngine 消费事件。"""

    feed_mode: str

    def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        """增加一个订阅；重复订阅是幂等的。"""

    def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        """移除一个订阅；未订阅时无操作。"""

    def health(self) -> FeedHealthReport:
        """返回当前供流健康快照。"""

    def add_listener(self, listener: FeedListener) -> Callable[[], None]:
        """注册事件监听，返回取消函数。"""


def worst_status(statuses: list[FeedHealthStatus]) -> FeedHealthStatus:
    """聚合多个通道：STALE 优先于 RECOVERING，RECOVERING 优先于 LIVE。"""
    for status in (
        FeedHealthStatus.STALE,
        FeedHealthStatus.RECOVERING,
        FeedHealthStatus.LIVE,
    ):
        if status in statuses:
            return status
    return FeedHealthStatus.LIVE
