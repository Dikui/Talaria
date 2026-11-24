"""衍生品范围结果的研究视图。"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from talaria.foundation.derivatives import DerivativePoint
from talaria.sdk._arrow import derivatives_to_arrow
from talaria.sdk._duckdb import asof_join, query_duckdb

if TYPE_CHECKING:
    import pyarrow as pa
    from pandas import DataFrame


class DerivativesResult(tuple[DerivativePoint, ...]):
    """按事件时间排序的 Decimal 衍生品点。"""

    def to_arrow(self) -> "pa.Table":
        """转换为保留精确 decimal128 数值的 PyArrow 表。"""
        return derivatives_to_arrow(self)

    def query_duckdb(
        self,
        sql: str,
        *,
        table: str = "derivatives",
        params: Sequence[object] | Mapping[str, object] | None = None,
        as_arrow: bool = False,
    ) -> "DataFrame | pa.Table":
        """在只读内存 DuckDB 上查询当前衍生品结果。"""
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

        return pd.DataFrame({
            "instrument_id": pd.Series((point.instrument_id for point in self), dtype="string"),
            "kind": pd.Series((point.kind for point in self), dtype="string"),
            "timeframe": pd.Series((point.timeframe for point in self), dtype="string"),
            "value": pd.Series((float(point.value) for point in self), dtype="float64"),
            "ts_event": pd.Series((point.ts_event for point in self), dtype="int64"),
            "ts_published": pd.Series((point.ts_published for point in self), dtype="Int64"),
            "settled": pd.Series((point.settled for point in self), dtype="boolean"),
            "ts_available": pd.Series((point.ts_available for point in self), dtype="int64"),
            "ts_ingested": pd.Series((point.ts_ingested for point in self), dtype="int64"),
            "ts_committed": pd.Series((point.ts_committed for point in self), dtype="int64"),
            "precision": pd.Series((point.precision for point in self), dtype="string"),
            "revision": pd.Series((point.revision for point in self), dtype="int64"),
        })
