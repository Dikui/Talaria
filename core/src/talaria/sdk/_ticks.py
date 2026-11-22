"""逐笔和买一卖一范围结果的研究视图。"""

from typing import TYPE_CHECKING, ClassVar

from talaria.foundation.ticks import Quote, TickTime, Trade

if TYPE_CHECKING:
    from pandas import DataFrame


class _TickResult[Row: TickTime](tuple[Row, ...]):
    _fields: ClassVar[dict[str, str]]

    def to_frame(self) -> "DataFrame":
        import pandas as pd

        fields = {
            "instrument_id": "string",
            "ts_event": "int64",
            **self._fields,
            "ts_available": "int64",
            "ts_ingested": "int64",
            "ts_committed": "int64",
            "precision": "string",
            "revision": "int64",
            "order_uncertain": "bool",
        }
        return pd.DataFrame({
            field: pd.Series((getattr(row, field) for row in self), dtype=dtype)
            for field, dtype in fields.items()
        })


class TradesResult(_TickResult[Trade]):
    """按事件时间排序的 Decimal 逐笔成交。"""

    _fields = {
        "trade_id": "string", "side": "string", "price": "float64", "size": "float64",
        "availability": "string",
        "archive_day": "Int64", "archive_index": "Int64", "sequence": "Int64",
        "packet_id": "string", "packet_index": "Int64",
    }


class QuotesResult(_TickResult[Quote]):
    """按事件时间排序的 Decimal 买一卖一。"""

    _fields = {
        **dict.fromkeys(("bid_price", "bid_size", "ask_price", "ask_size"), "float64"),
        "connection_id": "string", "update_id": "Int64",
    }
