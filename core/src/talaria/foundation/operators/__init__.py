"""唯一一份技术指标实现：纯函数、固定语义、批量与增量一致。"""

from __future__ import annotations

from talaria.foundation.operators.core import (
    DerivativeSample,
    OperatorState,
    Point,
    compute_series,
    update_series,
)
from talaria.foundation.operators.spec import (
    CHANNELS,
    DEFAULT_PERIODS,
    DERIVATIVE_INDICATORS,
    MAX_PERIOD,
    SUB_PANE_NAMES,
    IndicatorName,
    IndicatorSpec,
    pane_for,
    spec_from_params,
)

__all__ = [
    "CHANNELS",
    "DEFAULT_PERIODS",
    "DERIVATIVE_INDICATORS",
    "DerivativeSample",
    "MAX_PERIOD",
    "SUB_PANE_NAMES",
    "IndicatorName",
    "IndicatorSpec",
    "OperatorState",
    "Point",
    "compute_series",
    "pane_for",
    "spec_from_params",
    "update_series",
]
