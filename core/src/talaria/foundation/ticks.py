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
    order_uncertain: bool = False  # 查询时该毫秒内存在顺序无法判定的并列。


@dataclass(frozen=True, slots=True, kw_only=True)
class Trade(TickTime):
    trade_id: UUID
    side: Literal["Buy", "Sell"]
    price: Decimal
    size: Decimal
    availability: Literal["measured", "inferred"] = "measured"
    archive_day: int | None = None  # 归档日的 UTC 零点毫秒。
    archive_index: int | None = None
    sequence: int | None = None
    packet_id: UUID | None = None
    packet_index: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Quote(TickTime):
    bid_price: Decimal
    bid_size: Decimal
    ask_price: Decimal
    ask_size: Decimal
    connection_id: UUID | None = None
    update_id: int | None = None


def tick_identity(row: Trade | Quote) -> UUID | tuple[UUID, int] | int:
    if isinstance(row, Trade):
        return row.trade_id
    if row.connection_id is not None and row.update_id is not None:
        return row.connection_id, row.update_id
    return row.ts_event


def tick_payload(row: Trade | Quote) -> tuple:
    if isinstance(row, Trade):
        return row.side, row.price, row.size
    return row.bid_price, row.bid_size, row.ask_price, row.ask_size
