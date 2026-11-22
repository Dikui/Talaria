"""定仓、风控与成交共用的只读保证金估值契约。"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import ConfigDict

from talaria.foundation.types import ContractModel, DecimalStr

type Direction = Literal["LONG", "SHORT"]
MARGIN_VALUATION_VERSION = 2


@dataclass(frozen=True, slots=True)
class MarginPosition:
    instrument_id: str
    direction: Direction
    quantity: Decimal
    entry_price: Decimal


@dataclass(frozen=True, slots=True)
class MarginPendingEntry(MarginPosition):
    """入场价取亏损最差值，最大入场价用于预留名义金额。"""

    max_entry_price: Decimal


@dataclass(frozen=True, slots=True)
class MarginPortfolio:
    collateral: Decimal
    leverage: Decimal
    positions: tuple[MarginPosition, ...]
    pending_entries: tuple[MarginPendingEntry, ...] = ()


class MarginEstimate(ContractModel):
    model_config = ConfigDict(frozen=True)

    equity: DecimalStr
    initial_margin: DecimalStr
    maintenance_margin: DecimalStr


class MarginValuation(Protocol):
    """估值使用可见标记价或压力价，待成交订单只计占用及潜在亏损。"""

    interface_version: int

    def isolated_liquidation_price(
        self, instrument_id: str, direction: Direction, quantity: Decimal,
        entry_price: Decimal, isolated_margin: Decimal, *, as_of: int,
    ) -> Decimal: ...

    def cross_margin(
        self, portfolio: MarginPortfolio, mark_prices: Mapping[str, Decimal], *, as_of: int,
    ) -> MarginEstimate: ...

    def cross_liquidation_price(
        self, portfolio: MarginPortfolio, mark_prices: Mapping[str, Decimal],
        instrument_id: str, *, as_of: int,
    ) -> Decimal | None: ...
