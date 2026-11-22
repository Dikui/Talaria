"""定仓、风控与成交共用的只读保证金估值契约。"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol

from pydantic import ConfigDict

from talaria.foundation.types import ContractModel, DecimalStr

type Direction = Literal["LONG", "SHORT"]


@dataclass(frozen=True, slots=True)
class MarginPosition:
    instrument_id: str
    direction: Direction
    quantity: Decimal
    entry_price: Decimal


@dataclass(frozen=True, slots=True)
class MarginPortfolio:
    collateral: Decimal
    leverage: Decimal
    positions: tuple[MarginPosition, ...]


class MarginEstimate(ContractModel):
    model_config = ConfigDict(frozen=True)

    equity: DecimalStr
    initial_margin: DecimalStr
    maintenance_margin: DecimalStr


class MarginValuation(Protocol):
    """输入价格应为截止时点可见的标记价，或风控指定的压力场景价格。"""

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
