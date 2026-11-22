"""逐笔与买一卖一的精确数值和时点。"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from talaria.foundation.types import PrecisionTier


class TickKind(StrEnum):
    TRADES = "trades"
    QUOTES = "quotes"


@dataclass(frozen=True, slots=True, kw_only=True)
class TickTime:
    instrument_id: str
    ts_event: int
    ts_available: int
    ts_ingested: int
    ts_committed: int
    revision: int = 0
    precision: PrecisionTier = PrecisionTier.SECOND_VERIFIED


@dataclass(frozen=True, slots=True, kw_only=True)
class Trade(TickTime):
    trade_id: UUID
    side: Literal["Buy", "Sell"]
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class Quote(TickTime):
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal


def tick_identity(row: Trade | Quote) -> UUID | int:
    return row.trade_id if isinstance(row, Trade) else row.ts_event


def tick_payload(row: Trade | Quote) -> tuple:
    if isinstance(row, Trade):
        return row.side, row.price, row.size
    return row.bid_price, row.bid_size, row.ask_price, row.ask_size
