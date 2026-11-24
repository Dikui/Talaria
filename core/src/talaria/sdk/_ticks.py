"""逐笔和买一卖一范围结果的研究视图。"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from talaria.foundation.ticks import Quote, TickTime, Trade
from talaria.sdk._arrow import quotes_to_arrow, trades_to_arrow
from talaria.sdk._duckdb import asof_join, query_duckdb

if TYPE_CHECKING:
    import pyarrow as pa
    from pandas import DataFrame


class _TickResult[Row: TickTime](tuple[Row, ...]):
    _fields: ClassVar[dict[str, str]]

    def to_arrow(self) -> "pa.Table":
        """转换为保留精确 decimal128 数值的 PyArrow 表。"""
        raise NotImplementedError

    def query_duckdb(
        self,
        sql: str,
        *,
        table: str = "ticks",
        params: Sequence[object] | Mapping[str, object] | None = None,
        as_arrow: bool = False,
    ) -> "DataFrame | pa.Table":
        """在只读内存 DuckDB 上查询当前结果。"""
        return query_duckdb(sql, tables={table: self.to_arrow()}, params=params, as_arrow=as_arrow)

    def asof_join(
        self,
        other: Any,
        *,
        on: str = "ts_event",
        by: Sequence[str] | str | None = None,
        right_columns: Sequence[str] | None = None,
        right_prefix: str = "right_",
        tolerance_ms: int | None = None,
        as_arrow: bool = False,
    ) -> "DataFrame | pa.Table":
        """基于 DuckDB ``ASOF LEFT JOIN`` 与另一序列按时点对齐。"""
        return asof_join(
            self, other, on=on, by=by, right_columns=right_columns,
            right_prefix=right_prefix, tolerance_ms=tolerance_ms, as_arrow=as_arrow,
        )

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

    def to_arrow(self) -> "pa.Table":
        """转换为保留精确 decimal128 数值的 PyArrow 表。"""
        return trades_to_arrow(self)


class QuotesResult(_TickResult[Quote]):
    """按事件时间排序的 Decimal 买一卖一。"""

    _fields = {
        **dict.fromkeys(("bid_price", "bid_size", "ask_price", "ask_size"), "float64"),
        "connection_id": "string", "update_id": "Int64",
    }

    def to_arrow(self) -> "pa.Table":
        """转换为保留精确 decimal128 数值的 PyArrow 表。"""
        return quotes_to_arrow(self)
