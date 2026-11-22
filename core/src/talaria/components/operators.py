"""组件可调用的版本化算子与注册门槛。计算由 foundation/operators 提供。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import partial
from typing import Protocol

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.operators import (
    DEFAULT_PERIODS,
    MAX_PERIOD,
    DerivativeSample,
    IndicatorName,
    Point,
    compute_series,
    spec_from_params,
)
from talaria.foundation.operators.spec import VWAP_ANCHORS
from talaria.foundation.types import PrecisionTier


class OperatorCompute(Protocol):
    def __call__(
        self, bars: Sequence[CompactBar], params: Mapping[str, object], *,
        derivatives: Sequence[DerivativeSample] | None,
    ) -> Sequence[Point]: ...


@dataclass(frozen=True, slots=True)
class GoldenVector:
    bars: tuple[CompactBar, ...]
    params: Mapping[str, object]
    expected: tuple[Point, ...]
    derivatives: tuple[DerivativeSample, ...] | None = None


@dataclass(frozen=True, slots=True)
class Operator:
    name: str
    version: str
    parameters_schema: Mapping[str, object]
    compute: OperatorCompute
    golden: GoldenVector | None


class OperatorRegistry:
    def __init__(self) -> None:
        self._operators: dict[tuple[str, str], Operator] = {}

    def register(self, operator: Operator) -> None:
        key = (operator.name, operator.version)
        if key in self._operators:
            raise ValueError(f"算子已注册：{operator.name} v{operator.version}")
        vector = operator.golden
        if vector is None or not vector.bars or len(vector.bars) != len(vector.expected):
            raise ValueError(f"算子 {operator.name} 必须附带非空且与输入对齐的黄金向量")
        with localcontext(Context(prec=28, rounding=ROUND_HALF_EVEN)):
            actual = operator.compute(vector.bars, vector.params, derivatives=vector.derivatives)
        if tuple(actual) != vector.expected:
            raise ValueError(f"算子 {operator.name} 的计算结果与黄金向量不一致")
        self._operators[key] = operator

    def get(self, name: str, *, version: str) -> Operator:
        return self._operators[name, version]


def _compute(
    name: IndicatorName, bars: Sequence[CompactBar], params: Mapping[str, object], *,
    derivatives: Sequence[DerivativeSample] | None,
) -> tuple[Point, ...]:
    return compute_series(bars, spec_from_params(name, params), derivatives=derivatives).points


def _parameters_schema(name: IndicatorName) -> dict[str, object]:
    properties: dict[str, object] = {
        "period": {
            "type": "integer", "minimum": 2 if name is IndicatorName.BOLL else 1,
            "maximum": MAX_PERIOD, "default": DEFAULT_PERIODS[name],
        },
    }
    if name is IndicatorName.BOLL:
        properties["std_dev"] = {
            "type": "string", "pattern": r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$",
            "default": "2",
        }
    if name is IndicatorName.VWAP:
        properties["anchor"] = {"enum": list(VWAP_ANCHORS), "default": "session"}
    return {"type": "object", "properties": properties, "additionalProperties": False}


def _value_pair(first: str | None, second: str) -> tuple[Point, ...]:
    return ({"value": None if first is None else Decimal(first)}, {"value": Decimal(second)})


def _golden_vectors() -> dict[IndicatorName, GoldenVector]:
    # 两根平价柱从 10 到 12，各成交 1 单位。期望由 ADR-0010 的公式手算。
    # contracts/operator_vectors/ 另有覆盖缺口、增量及预热的回归向量。
    bars = tuple(
        CompactBar(
            timestamp=ts, open=price, high=price, low=price, close=price,
            volume=Decimal(1), turnover=price, status=BarStatus.NORMAL,
            ts_available=ts + 60_000, precision=PrecisionTier.SECOND_VERIFIED,
        )
        for ts, price in ((0, Decimal(10)), (60_000, Decimal(12)))
    )
    derivatives = (DerivativeSample(funding_rate=Decimal(0), open_interest=Decimal(100)),) * 2
    return {
        IndicatorName.EMA: GoldenVector(bars, {"period": 3}, _value_pair("10", "11")),
        IndicatorName.SMA: GoldenVector(bars, {"period": 2}, _value_pair(None, "11")),
        IndicatorName.RSI: GoldenVector(bars, {"period": 1}, _value_pair(None, "100")),
        IndicatorName.ATR: GoldenVector(bars, {"period": 2}, _value_pair(None, "1")),
        IndicatorName.BOLL: GoldenVector(
            bars, {"period": 2},
            (
                {"mid": None, "upper": None, "lower": None},
                {"mid": Decimal(11), "upper": Decimal(13), "lower": Decimal(9)},
            ),
        ),
        IndicatorName.VWAP: GoldenVector(bars, {"anchor": "snapshot"}, _value_pair("10", "11")),
        IndicatorName.FUNDING_QUANTILE: GoldenVector(
            bars, {"period": 1}, _value_pair("0.5", "0.5"), derivatives,
        ),
        IndicatorName.OI_DIVERGENCE: GoldenVector(
            bars, {"period": 1}, _value_pair(None, "0.2"), derivatives,
        ),
        IndicatorName.NET_ODDS: GoldenVector(
            bars, {"period": 1}, _value_pair(None, "1"), derivatives,
        ),
    }


OPERATORS = OperatorRegistry()
for _name, _golden in _golden_vectors().items():
    OPERATORS.register(Operator(
        name=_name, version="1", parameters_schema=_parameters_schema(_name),
        compute=partial(_compute, _name), golden=_golden,
    ))
