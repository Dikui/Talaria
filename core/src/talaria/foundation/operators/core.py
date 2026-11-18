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
from decimal import Decimal
from typing import Protocol, Sequence

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.operators.spec import IndicatorName, IndicatorSpec

Point = dict[str, Decimal | None]


class _Engine(Protocol):
    def reset(self) -> None: ...

    def push(self, bar: CompactBar) -> Point: ...


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
    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
        self._alpha = Decimal(2) / Decimal(self._period + 1)
        self._seed: list[Decimal] = []
        self._ema: Decimal | None = None

    def reset(self) -> None:
        self._seed.clear()
        self._ema = None

    def push(self, bar: CompactBar) -> Point:
        close = _close_of(bar)
        if self._ema is None:
            self._seed.append(close)
            if len(self._seed) < self._period:
                return {"value": None}
            self._ema = sum(self._seed) / Decimal(self._period)
            self._seed.clear()
            return {"value": self._ema}
        self._ema = self._ema + self._alpha * (close - self._ema)
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
    def __init__(self, spec: IndicatorSpec) -> None:
        self._period = int(spec.period)
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
        if previous is None:
            return {"value": None}
        true_range = _true_range(bar, previous)
        if self._atr is None:
            self._true_ranges.append(true_range)
            if len(self._true_ranges) < self._period:
                return {"value": None}
            self._atr = sum(self._true_ranges) / Decimal(self._period)
            self._true_ranges.clear()
            return {"value": self._atr}
        weight = Decimal(self._period - 1)
        self._atr = (self._atr * weight + true_range) / Decimal(self._period)
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

    def reset(self) -> None:
        self._cumulative_pv = Decimal(0)
        self._cumulative_volume = Decimal(0)
        self._day = None

    def push(self, bar: CompactBar) -> Point:
        day = bar.timestamp // 86_400_000
        if self._session_anchored and self._day is not None and day != self._day:
            self._cumulative_pv = Decimal(0)
            self._cumulative_volume = Decimal(0)
        self._day = day
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


_ENGINES: dict[IndicatorName, type[_Engine]] = {
    IndicatorName.SMA: _SmaEngine,
    IndicatorName.EMA: _EmaEngine,
    IndicatorName.RSI: _RsiEngine,
    IndicatorName.ATR: _AtrEngine,
    IndicatorName.BOLL: _BollEngine,
    IndicatorName.VWAP: _VwapEngine,
}


@dataclass(frozen=True, slots=True)
class OperatorState:
    """可继续增量的算子状态；``points`` 与已处理柱严格等长同序。"""

    spec: IndicatorSpec
    points: tuple[Point, ...]
    _engine: _Engine

    def merge(self, other: OperatorState) -> OperatorState:
        return OperatorState(
            spec=self.spec,
            points=self.points + other.points,
            _engine=other._engine,
        )


def _new_engine(spec: IndicatorSpec) -> _Engine:
    return _ENGINES[spec.name](spec)


def _push(
    engine: _Engine, bars: Sequence[CompactBar], spec: IndicatorSpec
) -> list[Point]:
    points: list[Point] = []
    for bar in bars:
        if bar.status is BarStatus.FEED_GAP:
            engine.reset()
            points.append(_empty(spec))
            continue
        points.append(engine.push(bar))
    return points


def compute_series(bars: Sequence[CompactBar], spec: IndicatorSpec) -> OperatorState:
    """批量计算；输出与 ``bars`` 等长，预热期为前导 ``None``。"""
    engine = _new_engine(spec)
    points = _push(engine, bars, spec)
    return OperatorState(spec=spec, points=tuple(points), _engine=engine)


def update_series(
    previous_state: OperatorState,
    new_bars: Sequence[CompactBar],
    spec: IndicatorSpec,
) -> OperatorState:
    """在已有状态上追加新柱；贡献与 ``compute_series`` 的逐值结果完全一致。"""
    if spec != previous_state.spec:
        raise ValueError("增量更新的指标参数与已有状态不一致")
    if not new_bars:
        return previous_state
    engine = copy.deepcopy(previous_state._engine)
    points = _push(engine, new_bars, spec)
    return OperatorState(
        spec=spec,
        points=previous_state.points + tuple(points),
        _engine=engine,
    )
