"""衍生品范围结果的研究视图。"""

from typing import TYPE_CHECKING

from talaria.foundation.derivatives import DerivativePoint

if TYPE_CHECKING:
    from pandas import DataFrame


class DerivativesResult(tuple[DerivativePoint, ...]):
    """按事件时间排序的 Decimal 衍生品点。"""

    def to_frame(self) -> "DataFrame":
        import pandas as pd

        return pd.DataFrame({
            "instrument_id": pd.Series((point.instrument_id for point in self), dtype="string"),
            "kind": pd.Series((point.kind for point in self), dtype="string"),
            "timeframe": pd.Series((point.timeframe for point in self), dtype="string"),
            "value": pd.Series((float(point.value) for point in self), dtype="float64"),
            "ts_event": pd.Series((point.ts_event for point in self), dtype="int64"),
            "ts_published": pd.Series((point.ts_published for point in self), dtype="Int64"),
            "ts_available": pd.Series((point.ts_available for point in self), dtype="int64"),
            "ts_ingested": pd.Series((point.ts_ingested for point in self), dtype="int64"),
            "ts_committed": pd.Series((point.ts_committed for point in self), dtype="int64"),
            "precision": pd.Series((point.precision for point in self), dtype="string"),
            "revision": pd.Series((point.revision for point in self), dtype="int64"),
        })
