"""市场无关的标的描述与五项能力 Trait。

核心层只认识这些结构，不允许出现 Bybit 或「加密永续」分支；具体市场的取值由
``talaria.packs`` 注册。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from talaria.foundation.types import ContractModel, DecimalStr


class InstrumentSpec(ContractModel):
    """一个可交易标的的静态规格。"""

    instrument_id: str
    venue: str
    symbol: str
    asset_class: str
    tick_size: DecimalStr
    qty_step: DecimalStr
    min_qty: DecimalStr
    price_precision: int
    qty_precision: int


@dataclass(frozen=True, slots=True)
class FeedCapabilityTrait:
    """行情能力：是否有实时流、轮询与历史数据。"""

    realtime: bool
    polling: bool
    history: bool


@dataclass(frozen=True, slots=True)
class MarginTrait:
    """保证金与杠杆能力。"""

    margin_asset: str
    max_leverage: Decimal


@dataclass(frozen=True, slots=True)
class CarryTrait:
    """持仓成本能力（资金费率等）。"""

    funding_interval_hours: int | None


@dataclass(frozen=True, slots=True)
class TradingSession:
    """包内声明的单一时段窗口（当地时间，``HH:MM``）。"""

    name: str
    start: str
    end: str
    timezone: str | None = None


@dataclass(frozen=True, slots=True)
class SessionTrait:
    """交易时段能力；具体时区与分段窗口完全由市场包提供。"""

    session_kind: str
    timezone: str | None = None
    day_start: str = "00:00"
    sessions: tuple[TradingSession, ...] = ()


@dataclass(frozen=True, slots=True)
class OrderConstraintTrait:
    """下单约束能力。"""

    post_only: bool
    max_market_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class TraitBundle:
    """一个标的的五项能力组合。"""

    feed: FeedCapabilityTrait
    margin: MarginTrait
    carry: CarryTrait
    session: SessionTrait
    order_constraint: OrderConstraintTrait


class MarketPack(Protocol):
    """市场规则包：把某一市场的差异限制在包内。"""

    name: str

    def instruments(self) -> Sequence[InstrumentSpec]:
        """返回该包提供的全部标的。"""

    def instrument(self, instrument_id: str) -> InstrumentSpec | None:
        """按标识查找标的。"""

    def traits(self, instrument_id: str) -> TraitBundle:
        """返回标的的五项能力；未知标的应抛 ``KeyError``。"""
