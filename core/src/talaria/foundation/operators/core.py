"""纯函数算子核：输入 N 根紧凑柱，输出严格等长 N 个值。

语义（docs/architecture.md §3.1）：
- 预热期输出前导 ``None``；
- ``NO_TRADES`` 按前收平盘零量保态递推；
- ``FEED_GAP`` 当根输出 ``None``，并从下一根开始重跑完整预热；
- 批量 ``compute_series`` 与增量 ``update_series`` 共享同一递推器，结果逐值一致。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from decimal import (
    ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext,
)
from functools import wraps
from typing import Callable, Protocol, Sequence

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.operators.spec import (
    DERIVATIVE_INDICATORS,
    IndicatorName,
    IndicatorSpec,
)
from talaria.foundation.timeframes import TIMEFRAME_MS

Point = dict[str, Decimal | None]
_QUANTILE_QUANT = Decimal("0.00000001")
_RATIO_QUANT = Decimal("0.00000001")
_ODDS_QUANT = Decimal("0.000001")
_OPERATOR_CONTEXT = Context(
    prec=28, rounding=ROUND_HALF_EVEN, Emin=-999999, Emax=999999,
    capitals=1, clamp=0, flags=[], traps=[InvalidOperation, DivisionByZero, Overflow],
)


def with_operator_context[**P, T](compute: Callable[P, T]) -> Callable[P, T]:
    """以黄金向量约定的 Decimal 精度执行算子。"""
    @wraps(compute)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with localcontext(_OPERATOR_CONTEXT):
            return compute(*args, **kwargs)

    return wrapped


@dataclass(frozen=True, slots=True)
class DerivativeSample:
    """与一根 K 线对齐的衍生品上下文（按 ``ts_available`` 向前取值）。"""

    funding_rate: Decimal | None = None
    funding_ts: int | None = None
    open_interest: Decimal | None = None
    oi_ts: int | None = None
    mark_price: Decimal | None = None
    mark_ts: int | None = None


class _Engine(Protocol):
    def reset(self) -> None: ...

    def push(self, bar: CompactBar) -> Point: ...


class _DerivativeEngine(Protocol):
    def reset(self) -> None: ...

    def push(self, bar: CompactBar, sample: DerivativeSample) -> Point: ...


def _empty(spec: IndicatorSpec) -> Point:
    return {channel: None for channel in spec.channels}


class _SmaEngine:
    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._window: list[Decimal] = []

    def reset(self) -> None:
        self._window.clear()

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        self._window.append(close)
        if len(self._window) > self._period:
            self._window.pop(0)
        if len(self._window) < self._period:
            return {"value": None}
        return {"value": sum(self._window) / Decimal(self._period)}


class _EmaEngine:
    def __init__(self, spec: IndicatorSpec, *, legacy: bool = False) -> None:
        self._period = int(spec.period)
        self._alpha = Decimal(2) / Decimal(self._period + 1)
        self._legacy = legacy
        self._seed: list[Decimal] = []
        self._ema: Decimal | None = None

    def reset(self) -> None:
        self._seed.clear()
        self._ema = None

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        if self._ema is None:
            if self._legacy:
                self._seed.append(close)
                if len(self._seed) < self._period:
                    return {"value": None}
                self._ema = sum(self._seed) / Decimal(self._period)
                self._seed.clear()
            else:
                self._ema = close
        elif self._legacy:
            self._ema += self._alpha * (close - self._ema)
        else:
            self._ema = self._alpha * close + (1 - self._alpha) * self._ema
        return {"value": self._ema}


class _RsiEngine:
    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._previous_close: Decimal | None = None
        self._gains: list[Decimal] = []
        self._losses: list[Decimal] = []
        self._avg_gain: Decimal | None = None
        self._avg_loss: Decimal | None = None

    def reset(self) -> None:
        self._previous_close = None
        self._gains.clear()
        self._losses.clear()
        self._avg_gain = None
        self._avg_loss = None

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        previous = self._previous_close
        self._previous_close = close
        if previous is None:
            return {"value": None}
        change = close - previous
        gain = change if change > 0 else Decimal(0)
        loss = -change if change < 0 else Decimal(0)
        if self._avg_gain is None:
            self._gains.append(gain)
            self._losses.append(loss)
            if len(self._gains) < self._period:
                return {"value": None}
            self._avg_gain = sum(self._gains) / Decimal(self._period)
            self._avg_loss = sum(self._losses) / Decimal(self._period)
            self._gains.clear()
            self._losses.clear()
        else:
            weight = Decimal(self._period - 1)
            self._avg_gain = (self._avg_gain * weight + gain) / Decimal(self._period)
            self._avg_loss = (self._avg_loss * weight + loss) / Decimal(self._period)
        return {"value": _wild_rsi(self._avg_gain, self._avg_loss)}


def _wild_rsi(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    total = avg_gain + avg_loss
    if total == 0:
        return Decimal(50)
    return Decimal(100) * avg_gain / total


class _AtrEngine:
    def __init__(self, spec: IndicatorSpec, *, legacy: bool = False) -> None:
        self._period = int(spec.period)
        self._alpha = Decimal(1) / Decimal(self._period)
        self._legacy = legacy
        self._previous_close: Decimal | None = None
        self._true_ranges: list[Decimal] = []
        self._atr: Decimal | None = None

    def reset(self) -> None:
        self._previous_close = None
        self._true_ranges.clear()
        self._atr = None

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        previous = self._previous_close
        self._previous_close = close
        if self._legacy and previous is None:
            return {"value": None}
        true_range = (
            _true_range(bar, previous)
            if previous is not None
            else _high_of(bar) - _low_of(bar)
        )
        if self._atr is None:
            self._true_ranges.append(true_range)
            if len(self._true_ranges) < self._period:
                return {"value": None}
            self._atr = sum(self._true_ranges) / Decimal(self._period)
            self._true_ranges.clear()
            return {"value": self._atr}
        if self._legacy:
            self._atr = (self._atr * (self._period - 1) + true_range) / self._period
        else:
            self._atr = self._alpha * true_range + (1 - self._alpha) * self._atr
        return {"value": self._atr}


class _BollEngine:
    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._std_dev = spec.std_dev or Decimal(2)
        self._window: list[Decimal] = []

    def reset(self) -> None:
        self._window.clear()

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        self._window.append(close)
        if len(self._window) > self._period:
            self._window.pop(0)
        if len(self._window) < self._period:
            return {"mid": None, "upper": None, "lower": None}
        middle = sum(self._window) / Decimal(self._period)
        variance = sum((value - middle) ** 2 for value in self._window) / Decimal(
            self._period
        )
        deviation = variance.sqrt()
        return {
            "mid": middle,
            "upper": middle + self._std_dev * deviation,
            "lower": middle - self._std_dev * deviation,
        }


class _VwapEngine:
    def __init__(self, spec: IndicatorSpec) -> None:
        self._session_anchored = spec.anchor == "session"
        self._cumulative_pv = Decimal(0)
        self._cumulative_volume = Decimal(0)
        self._day: int | None = None
        self.anchor_ts: int | None = None

    def reset(self) -> None:
        self._cumulative_pv = Decimal(0)
        self._cumulative_volume = Decimal(0)
        self._day = None
        self.anchor_ts = None

    def push(self, bar: CompactBar) -> Point:
        if self._session_anchored:
            day = bar.timestamp // TIMEFRAME_MS["1d"]
            if self._day is None:
                self._day = day
                return {"value": None}
            if day != self._day:
                self._day = day
                self._cumulative_pv = Decimal(0)
                self._cumulative_volume = Decimal(0)
                self.anchor_ts = bar.timestamp
            if self.anchor_ts is None:
                return {"value": None}
        volume = bar.volume or Decimal(0)
        if volume != 0:
            typical = (_high_of(bar) + _low_of(bar) + _close_of(bar)) / Decimal(3)
            self._cumulative_pv += typical * volume
            self._cumulative_volume += volume
        if self._cumulative_volume == 0:
            return {"value": None}
        return {"value": self._cumulative_pv / self._cumulative_volume}


def _close_of(bar: CompactBar) -> Decimal:
    assert bar.close is not None
    return bar.close


def _high_of(bar: CompactBar) -> Decimal:
    assert bar.high is not None
    return bar.high


def _low_of(bar: CompactBar) -> Decimal:
    assert bar.low is not None
    return bar.low


def _true_range(bar: CompactBar, previous_close: Decimal) -> Decimal:
    high = _high_of(bar)
    low = _low_of(bar)
    return max(
        high - low,
        abs(high - previous_close),
        abs(low - previous_close),
    )


class _FundingQuantileEngine:
    """当前资金费率在最近 ``period`` 根 K 线窗口内的分位（0..1）。

    窗口按 K 线采样，因此是持仓时间加权的分位；相等值按半权计。
    """

    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._rates: list[Decimal] = []

    def reset(self) -> None:
        self._rates.clear()

    def push(self, bar: CompactBar, sample: DerivativeSample) -> Point:
        rate = sample.funding_rate
        if rate is None:
            return {"value": None}
        self._rates.append(rate)
        if len(self._rates) > self._period:
            self._rates.pop(0)
        if len(self._rates) < self._period:
            return {"value": None}
        less = sum(1 for value in self._rates if value < rate)
        equal = sum(1 for value in self._rates if value == rate)
        rank = (Decimal(less) + Decimal(equal) / Decimal(2)) / Decimal(
            len(self._rates)
        )
        return {"value": rank.quantize(_QUANTILE_QUANT)}


class _OiDivergenceEngine:
    """价格收益率与持仓量收益率之差，正值为价升仓减背离。"""

    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._window: list[tuple[Decimal, Decimal]] = []

    def reset(self) -> None:
        self._window.clear()

    def push(self, bar: CompactBar, sample: DerivativeSample) -> Point:
        oi = sample.open_interest
        if oi is None:
            return {"value": None}
        close = _close_of(bar)
        self._window.append((close, oi))
        if len(self._window) > self._period + 1:
            self._window.pop(0)
        if len(self._window) < self._period + 1:
            return {"value": None}
        start_close, start_oi = self._window[0]
        if start_close == 0 or start_oi == 0:
            return {"value": None}
        price_return = (close - start_close) / start_close
        oi_return = (oi - start_oi) / start_oi
        return {"value": (price_return - oi_return).quantize(_RATIO_QUANT)}


class _NetOddsEngine:
    """资金费率调整后的净赔率几何验算（见任务单 Comments）。

    ``net_odds = (窗口价格变动 - 窗口资金成本) / 窗口平均真实波幅``；
    真实波幅作为几何风险单位，资金成本按结算时点的标记价即时折算。
    """

    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._previous_close: Decimal | None = None
        self._closes: list[tuple[int, Decimal]] = []
        self._true_ranges: list[Decimal] = []
        self._settlements: list[tuple[int, Decimal]] = []

    def reset(self) -> None:
        self._previous_close = None
        self._closes.clear()
        self._true_ranges.clear()
        self._settlements.clear()

    def push(self, bar: CompactBar, sample: DerivativeSample) -> Point:
        close = _close_of(bar)
        previous = self._previous_close
        if previous is None:
            true_range = None
        else:
            true_range = _true_range(bar, previous)
        self._previous_close = close
        if true_range is not None:
            self._true_ranges.append(true_range)
            if len(self._true_ranges) > self._period:
                self._true_ranges.pop(0)
        self._closes.append((bar.timestamp, close))
        if len(self._closes) > self._period + 1:
            self._closes.pop(0)
        if sample.funding_ts is None or sample.funding_rate is None:
            return {"value": None}
        settlement = (sample.funding_ts, sample.funding_rate)
        if self._settlements and self._settlements[-1][0] == sample.funding_ts:
            self._settlements[-1] = settlement
        else:
            self._settlements.append(settlement)
        if len(self._closes) < self._period + 1 or len(self._true_ranges) < self._period:
            return {"value": None}
        atr = sum(self._true_ranges) / Decimal(self._period)
        if atr == 0:
            return {"value": None}
        start_ts, start_close = self._closes[0]
        funding_cost = (
            sum(
                rate
                for ts, rate in self._settlements
                if start_ts < ts <= bar.timestamp
            )
            * close
        )
        self._settlements = [
            entry for entry in self._settlements if entry[0] > start_ts
        ]
        net = (close - start_close - funding_cost) / atr
        return {"value": net.quantize(_ODDS_QUANT)}


_ENGINES: dict[IndicatorName, type[_Engine]] = {
    IndicatorName.SMA: _SmaEngine,
    IndicatorName.EMA: _EmaEngine,
    IndicatorName.RSI: _RsiEngine,
    IndicatorName.ATR: _AtrEngine,
    IndicatorName.BOLL: _BollEngine,
    IndicatorName.VWAP: _VwapEngine,
}

_DERIVATIVE_ENGINES: dict[
    IndicatorName, type[_FundingQuantileEngine] | type[_OiDivergenceEngine] | type[_NetOddsEngine]
] = {
    IndicatorName.FUNDING_QUANTILE: _FundingQuantileEngine,
    IndicatorName.OI_DIVERGENCE: _OiDivergenceEngine,
    IndicatorName.NET_ODDS: _NetOddsEngine,
}


@dataclass(frozen=True, slots=True)
class OperatorState:
    """可继续增量的算子状态；``points`` 与已处理柱严格等长同序。"""

    spec: IndicatorSpec
    points: tuple[Point, ...]
    _engine: _Engine | _DerivativeEngine

    def merge(self, other: OperatorState) -> OperatorState:
        return OperatorState(
            spec=self.spec,
            points=self.points + other.points,
            _engine=other._engine,
        )


def _new_engine(spec: IndicatorSpec) -> _Engine | _DerivativeEngine:
    if spec.name in DERIVATIVE_INDICATORS:
        return _DERIVATIVE_ENGINES[spec.name](spec)
    return _ENGINES[spec.name](spec)


def _push(
    engine: _Engine | _DerivativeEngine,
    bars: Sequence[CompactBar],
    spec: IndicatorSpec,
    derivatives: Sequence[DerivativeSample] | None,
    previous_points: Sequence[Point] = (),
) -> list[Point]:
    points: list[Point] = []
    if spec.name in DERIVATIVE_INDICATORS:
        if derivatives is None:
            raise ValueError(f"{spec.name} 需要衍生品样本")
        if len(derivatives) != len(bars):
            raise ValueError("衍生品样本必须与 K 线等长")
        derivative_engine = engine
        assert isinstance(
            derivative_engine,
            (_FundingQuantileEngine, _OiDivergenceEngine, _NetOddsEngine),
        )
        for bar, sample in zip(bars, derivatives):
            if bar.status is BarStatus.FEED_GAP:
                derivative_engine.reset()
                points.append(_empty(spec))
                continue
            if bar.status is BarStatus.NO_TRADES:
                source = (
                    points[-1]
                    if points
                    else (previous_points[-1] if previous_points else None)
                )
                points.append(dict(source) if source is not None else _empty(spec))
                continue
            points.append(derivative_engine.push(bar, sample))
        return points
    bar_engine = engine
    assert not isinstance(
        bar_engine, (_FundingQuantileEngine, _OiDivergenceEngine, _NetOddsEngine)
    )
    for bar in bars:
        if bar.status is BarStatus.FEED_GAP:
            bar_engine.reset()
            points.append(_empty(spec))
            continue
        points.append(bar_engine.push(bar))
    return points


@with_operator_context
def compute_series(
    bars: Sequence[CompactBar],
    spec: IndicatorSpec,
    *,
    derivatives: Sequence[DerivativeSample] | None = None,
) -> OperatorState:
    """批量计算；输出与 ``bars`` 等长，预热期为前导 ``None``。

    衍生品指标必须传入与 ``bars`` 等长、逐根对齐的 ``derivatives``。
    """
    engine = _new_engine(spec)
    points = _push(engine, bars, spec, derivatives)
    return OperatorState(spec=spec, points=tuple(points), _engine=engine)


@with_operator_context
def legacy_alert_points(bars: Sequence[CompactBar], spec: IndicatorSpec) -> list[Point]:
    """复现迁移前已保存预警的 EMA / ATR（5cd280c）。"""
    if spec.name is IndicatorName.EMA:
        engine = _EmaEngine(spec, legacy=True)
    elif spec.name is IndicatorName.ATR:
        engine = _AtrEngine(spec, legacy=True)
    else:
        raise ValueError(f"{spec.name} 不支持旧版预警播种")
    return _push(engine, bars, spec, None)


@with_operator_context
def latest_session_vwap(bars: Sequence[CompactBar]) -> tuple[int, Decimal] | None:
    """Return the current session's first contributing window timestamp and VWAP."""
    spec = IndicatorSpec(IndicatorName.VWAP, anchor="session")
    engine = _VwapEngine(spec)
    points = _push(engine, bars, spec, None)
    value = points[-1]["value"] if points else None
    if value is None or engine.anchor_ts is None:
        return None
    return engine.anchor_ts, value


@with_operator_context
def update_series(
    previous_state: OperatorState,
    new_bars: Sequence[CompactBar],
    spec: IndicatorSpec,
    *,
    derivatives: Sequence[DerivativeSample] | None = None,
) -> OperatorState:
    """在已有状态上追加新柱；贡献与 ``compute_series`` 的逐值结果完全一致。"""
    if spec != previous_state.spec:
        raise ValueError("增量更新的指标参数与已有状态不一致")
    if not new_bars:
        return previous_state
    engine = copy.deepcopy(previous_state._engine)
    points = _push(
        engine, new_bars, spec, derivatives, previous_points=previous_state.points
    )
    return OperatorState(
        spec=spec,
        points=previous_state.points + tuple(points),
        _engine=engine,
    )
