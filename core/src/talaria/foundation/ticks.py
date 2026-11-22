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


def trade_version(row: Trade) -> tuple:
    return (
        row.availability == "measured", row.revision, row.ts_ingested, row.ts_committed,
        row.price, -row.size, row.side, row.ts_event, tick_order(row),
    )


def tick_order(row: Trade | Quote) -> tuple:
    if isinstance(row, Trade):
        trade_order = (
            row.availability,
            row.archive_day if row.archive_day is not None else -1,
            row.archive_index if row.archive_index is not None else -1,
            row.sequence if row.sequence is not None else -1,
            (row.packet_id or row.trade_id).bytes,
            row.packet_index if row.packet_index is not None else -1,
            row.trade_id.bytes,
        )
        return row.ts_event, trade_order, row.revision, row.ts_ingested
    quote_order = (
        row.connection_id.bytes if row.connection_id is not None else b"",
        row.update_id if row.update_id is not None else -1,
    )
    return row.ts_event, quote_order, row.revision, row.ts_ingested


def relative_tick_order_known(first: Trade | Quote, second: Trade | Quote) -> bool:
    if isinstance(first, Quote) and isinstance(second, Quote):
        return (first.connection_id is not None and first.connection_id == second.connection_id
                and first.update_id is not None and second.update_id is not None
                and first.update_id != second.update_id)
    if not isinstance(first, Trade) or not isinstance(second, Trade) or first.availability != second.availability:
        return False
    if first.availability == "inferred":
        return (first.archive_day is not None and second.archive_day is not None
                and first.archive_index is not None and second.archive_index is not None
                and (first.archive_day, first.archive_index) < (second.archive_day, second.archive_index))
    if first.packet_id is not None and first.packet_id == second.packet_id:
        return (first.packet_index is not None and second.packet_index is not None
                and first.packet_index < second.packet_index)
    return first.sequence is not None and second.sequence is not None and first.sequence < second.sequence
