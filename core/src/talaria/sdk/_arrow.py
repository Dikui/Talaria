"""Apache Arrow IPC 流解码与无损十进制 Arrow 表转换辅助。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

import pyarrow as pa
import pyarrow.ipc as pa_ipc

from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.ticks import Quote, Trade
from talaria.foundation.types import PrecisionTier

ARROW_STREAM_CONTENT_TYPE = "application/vnd.apache.arrow.stream"


def _decimal_value(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _decimal_type(values: Sequence[Decimal], *, default_scale: int = 8) -> pa.Decimal128Type:
    if not values:
        return pa.decimal128(18, default_scale)
    scales: list[int] = []
    max_int_digits = 1
    for item in values:
        tup = item.normalize().as_tuple()
        exp = tup.exponent if isinstance(tup.exponent, int) else 0
        scale = max(0, -exp)
        scales.append(scale)
        int_digits = max(1, len(tup.digits) + min(0, exp))
        max_int_digits = max(max_int_digits, int_digits)
    scale = min(18, max(scales, default=0))
    precision = 18 if max_int_digits + scale <= 18 else 38
    return pa.decimal128(precision, scale)


def _quantize_for_arrow(value: Decimal, scale: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-scale))


def read_arrow_stream(payload: bytes) -> pa.Table:
    """将 HTTP 返回的 Arrow IPC 流解析为 Arrow Table。"""
    with pa_ipc.open_stream(payload) as reader:
        return reader.read_all()


def bars_from_arrow(
    payload: bytes,
    *,
    expected_symbol: str,
    expected_timeframe: str,
    confirmed: bool = True,
) -> list[RangeBar]:
    """将 `/api/bars.arrow` IPC 流解码为保留精确 Decimal 的 `RangeBar` 列表。"""
    table = read_arrow_stream(payload)
    bars: list[RangeBar] = []
    for row in table.to_pylist():
        if str(row["symbol"]) != expected_symbol or str(row["timeframe"]) != expected_timeframe:
            raise ValueError("服务返回的标的或周期与请求不一致")
        bars.append(RangeBar(
            ts_event=int(row["ts_event"]),
            open=_decimal_value(row["open"]),
            high=_decimal_value(row["high"]),
            low=_decimal_value(row["low"]),
            close=_decimal_value(row["close"]),
            volume=_decimal_value(row["volume"]),
            turnover=_decimal_value(row["turnover"]),
            ts_available=int(row["ts_available"]),
            precision=PrecisionTier(str(row["precision"])),
            confirmed=bool(row["confirmed"]) if "confirmed" in row and row["confirmed"] is not None else confirmed,
            revision=int(row.get("revision", 0) or 0),
        ))
    return bars


def trades_from_arrow(payload: bytes, instrument_id: str) -> list[Trade]:
    """将 `/api/ticks.arrow?kind=trades` IPC 流解码为精确 Decimal `Trade` 列表。"""
    table = read_arrow_stream(payload)
    rows: list[Trade] = []
    for row in table.to_pylist():
        packet_id = row.get("packet_id")
        rows.append(Trade(
            instrument_id=instrument_id,
            trade_id=uuid.UUID(bytes=row["trade_id"]),
            side="Buy" if row["side"] else "Sell",
            price=_decimal_value(row["price"]),
            size=_decimal_value(row["size"]),
            availability=str(row.get("availability") or "measured"),
            archive_day=row.get("archive_day"),
            archive_index=row.get("archive_index"),
            sequence=row.get("sequence"),
            packet_id=uuid.UUID(bytes=packet_id) if packet_id is not None else None,
            packet_index=row.get("packet_index"),
            ts_event=int(row["ts_event"]),
            ts_available=int(row["ts_available"]),
            ts_ingested=int(row["ts_ingested"]),
            ts_committed=int(row["ts_committed"]),
            revision=int(row.get("revision", 0) or 0),
            precision=PrecisionTier(str(row["precision"])),
        ))
    return rows


def quotes_from_arrow(payload: bytes, instrument_id: str) -> list[Quote]:
    """将 `/api/ticks.arrow?kind=quotes` IPC 流解码为精确 Decimal `Quote` 列表。"""
    table = read_arrow_stream(payload)
    rows: list[Quote] = []
    for row in table.to_pylist():
        connection_id = row.get("connection_id")
        rows.append(Quote(
            instrument_id=instrument_id,
            bid_price=_decimal_value(row["bid_price"]),
            bid_size=_decimal_value(row["bid_size"]),
            ask_price=_decimal_value(row["ask_price"]),
            ask_size=_decimal_value(row["ask_size"]),
            connection_id=uuid.UUID(bytes=connection_id) if connection_id is not None else None,
            update_id=row.get("update_id"),
            ts_event=int(row["ts_event"]),
            ts_available=int(row["ts_available"]),
            ts_ingested=int(row["ts_ingested"]),
            ts_committed=int(row["ts_committed"]),
            revision=int(row.get("revision", 0) or 0),
            precision=PrecisionTier(str(row["precision"])),
        ))
    return rows


def derivatives_from_arrow(
    payload: bytes,
    *,
    expected_instrument: str,
    expected_kind: DerivativeKind,
) -> list[DerivativePoint]:
    """将 `/api/derivatives.arrow` IPC 流解码为精确 Decimal `DerivativePoint` 列表。"""
    table = read_arrow_stream(payload)
    points: list[DerivativePoint] = []
    for row in table.to_pylist():
        point = DerivativePoint.model_validate(row)
        if point.instrument_id != expected_instrument or point.kind is not expected_kind:
            raise ValueError("服务返回的衍生品标的或类型与请求不一致")
        points.append(point)
    return points


def bars_to_arrow(rows: Sequence[RangeBar]) -> pa.Table:
    """将 `RangeBar` 序列转换为带 `decimal128` 列的 Arrow 表。"""
    dec_cols = ("open", "high", "low", "close", "volume", "turnover")
    dec_types = {
        col: _decimal_type([getattr(bar, col) for bar in rows])
        for col in dec_cols
    }
    schema = pa.schema([
        pa.field("ts_event", pa.int64(), nullable=False),
        *(pa.field(col, dec_types[col], nullable=False) for col in dec_cols),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
        pa.field("confirmed", pa.bool_(), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
    ])
    data = {
        "ts_event": [bar.ts_event for bar in rows],
        **{
            col: [_quantize_for_arrow(getattr(bar, col), dec_types[col].scale) for bar in rows]
            for col in dec_cols
        },
        "ts_available": [bar.ts_available for bar in rows],
        "precision": [bar.precision.value for bar in rows],
        "confirmed": [bar.confirmed for bar in rows],
        "revision": [bar.revision for bar in rows],
    }
    return pa.Table.from_pydict(data, schema=schema)


def trades_to_arrow(rows: Sequence[Trade]) -> pa.Table:
    """将 `Trade` 序列转换为保留 `decimal128` 精度的 Arrow 表。"""
    price_type = _decimal_type([row.price for row in rows])
    size_type = _decimal_type([row.size for row in rows])
    schema = pa.schema([
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("trade_id", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("price", price_type, nullable=False),
        pa.field("size", size_type, nullable=False),
        pa.field("availability", pa.string(), nullable=False),
        pa.field("archive_day", pa.int64(), nullable=True),
        pa.field("archive_index", pa.int64(), nullable=True),
        pa.field("sequence", pa.int64(), nullable=True),
        pa.field("packet_id", pa.string(), nullable=True),
        pa.field("packet_index", pa.int64(), nullable=True),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
        pa.field("order_uncertain", pa.bool_(), nullable=False),
    ])
    data = {
        "instrument_id": [row.instrument_id for row in rows],
        "ts_event": [row.ts_event for row in rows],
        "trade_id": [str(row.trade_id) for row in rows],
        "side": [row.side for row in rows],
        "price": [_quantize_for_arrow(row.price, price_type.scale) for row in rows],
        "size": [_quantize_for_arrow(row.size, size_type.scale) for row in rows],
        "availability": [row.availability for row in rows],
        "archive_day": [row.archive_day for row in rows],
        "archive_index": [row.archive_index for row in rows],
        "sequence": [row.sequence for row in rows],
        "packet_id": [str(row.packet_id) if row.packet_id is not None else None for row in rows],
        "packet_index": [row.packet_index for row in rows],
        "ts_available": [row.ts_available for row in rows],
        "ts_ingested": [row.ts_ingested for row in rows],
        "ts_committed": [row.ts_committed for row in rows],
        "precision": [row.precision.value for row in rows],
        "revision": [row.revision for row in rows],
        "order_uncertain": [row.order_uncertain for row in rows],
    }
    return pa.Table.from_pydict(data, schema=schema)


def quotes_to_arrow(rows: Sequence[Quote]) -> pa.Table:
    """将 `Quote` 序列转换为保留 `decimal128` 精度的 Arrow 表。"""
    dec_cols = ("bid_price", "bid_size", "ask_price", "ask_size")
    dec_types = {
        col: _decimal_type([getattr(row, col) for row in rows])
        for col in dec_cols
    }
    schema = pa.schema([
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        *(pa.field(col, dec_types[col], nullable=False) for col in dec_cols),
        pa.field("connection_id", pa.string(), nullable=True),
        pa.field("update_id", pa.int64(), nullable=True),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
        pa.field("order_uncertain", pa.bool_(), nullable=False),
    ])
    data = {
        "instrument_id": [row.instrument_id for row in rows],
        "ts_event": [row.ts_event for row in rows],
        **{
            col: [_quantize_for_arrow(getattr(row, col), dec_types[col].scale) for row in rows]
            for col in dec_cols
        },
        "connection_id": [str(row.connection_id) if row.connection_id is not None else None for row in rows],
        "update_id": [row.update_id for row in rows],
        "ts_available": [row.ts_available for row in rows],
        "ts_ingested": [row.ts_ingested for row in rows],
        "ts_committed": [row.ts_committed for row in rows],
        "precision": [row.precision.value for row in rows],
        "revision": [row.revision for row in rows],
        "order_uncertain": [row.order_uncertain for row in rows],
    }
    return pa.Table.from_pydict(data, schema=schema)


def derivatives_to_arrow(rows: Sequence[DerivativePoint]) -> pa.Table:
    """将 `DerivativePoint` 序列转换为保留 `decimal128` 精度的 Arrow 表。"""
    val_type = _decimal_type([row.value for row in rows])
    schema = pa.schema([
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=True),
        pa.field("value", val_type, nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("ts_published", pa.int64(), nullable=True),
        pa.field("settled", pa.bool_(), nullable=True),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
    ])
    data = {
        "instrument_id": [row.instrument_id for row in rows],
        "kind": [row.kind.value for row in rows],
        "timeframe": [row.timeframe for row in rows],
        "value": [_quantize_for_arrow(row.value, val_type.scale) for row in rows],
        "ts_event": [row.ts_event for row in rows],
        "ts_published": [row.ts_published for row in rows],
        "settled": [row.settled for row in rows],
        "ts_available": [row.ts_available for row in rows],
        "ts_ingested": [row.ts_ingested for row in rows],
        "ts_committed": [row.ts_committed for row in rows],
        "precision": [row.precision.value for row in rows],
        "revision": [row.revision for row in rows],
    }
    return pa.Table.from_pydict(data, schema=schema)
