"""收盘求值与按时点读取数据。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from talaria import sdk
from talaria.components.contracts import Series
from talaria.components.operators import OPERATORS
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeKind,
    DerivativePoint,
)
from talaria.foundation.indicators import IndicatorPoint
from talaria.foundation.operators import DERIVATIVE_INDICATORS
from talaria.foundation.operators.inputs import derivative_samples
from talaria.foundation.timeframes import last_closed_bar_start, timeframe_ms
from talaria.perception.compaction import compact_series


class BarReader(Protocol):
    """SDK 范围读取接口，返回按事件时间升序排列的可见修订。"""

    def __call__(
        self, instrument: str, timeframe: str, *, start: int, end: int, as_of: int,
    ) -> Sequence[RangeBar]: ...


class DerivativeReader(Protocol):
    def __call__(
        self, instrument: str, kind: DerivativeKind, *, start: int, end: int, as_of: int,
    ) -> Sequence[DerivativePoint]: ...


@dataclass(frozen=True, slots=True)
class Ctx:
    """组件在单个收盘时刻可见的数据，所有时点均为 UTC 毫秒。"""

    instrument: str
    timeframe: str
    start: int
    now: int
    _read_bars: BarReader = field(default=sdk.bars, repr=False, compare=False)
    _read_derivatives: DerivativeReader | None = field(default=None, repr=False, compare=False)
    _bars_by_timeframe: dict[str, tuple[RangeBar, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _derivatives_by_kind: dict[DerivativeKind, tuple[DerivativePoint, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    def _closed_bars(self, timeframe: str) -> tuple[RangeBar, ...]:
        if timeframe not in self._bars_by_timeframe:
            end = last_closed_bar_start(self.now, timeframe_ms(timeframe))
            visible: tuple[RangeBar, ...] = ()
            if end >= self.start:
                rows = self._read_bars(
                    self.instrument, timeframe, start=self.start, end=end, as_of=self.now,
                )
                visible = tuple(
                    bar for bar in rows
                    if bar.confirmed and self.start <= bar.ts_event <= end and bar.ts_available <= self.now
                )
            self._bars_by_timeframe[timeframe] = visible
        return self._bars_by_timeframe[timeframe]

    def bars(self, timeframe: str | None = None) -> tuple[RangeBar, ...]:
        """返回确认柱。大于求值周期的查询最多返回最近一根。"""
        requested = self.timeframe if timeframe is None else timeframe
        bars = self._closed_bars(requested)
        return bars[-1:] if timeframe_ms(requested) > timeframe_ms(self.timeframe) else bars

    def derivatives(self, kind: DerivativeKind | str) -> tuple[DerivativePoint, ...]:
        """返回截止当前时点可获取的衍生品点，数据源可由研究调用方提供。"""
        kind = DerivativeKind(kind)
        if self._read_derivatives is None:
            return ()
        if kind not in self._derivatives_by_kind:
            rows = self._read_derivatives(
                self.instrument, kind, start=self.start, end=self.now, as_of=self.now,
            )
            self._derivatives_by_kind[kind] = tuple(sorted(
                (
                    point for point in rows
                    if point.instrument_id == self.instrument and point.kind is kind
                    and self.start <= point.ts_event <= self.now and point.ts_available <= self.now
                ),
                key=lambda point: point.ts_event,
            ))
        return self._derivatives_by_kind[kind]

    def indicator(
        self, name: str, *, timeframe: str | None = None, version: str = "1", **params: object,
    ) -> Series:
        """计算已注册算子的 Decimal 序列，跨大周期只返回最后一个点。"""
        operator = OPERATORS.get(name, version=version)
        requested = self.timeframe if timeframe is None else timeframe
        bars = compact_series(self._closed_bars(requested), timeframe=requested, cutoff_ts=self.now)
        samples = None
        if name in DERIVATIVE_INDICATORS:
            oi_timeframe = OI_TIMEFRAME_BY_TIMEFRAME[requested]
            oi = [
                point for point in self.derivatives(DerivativeKind.OPEN_INTEREST)
                if point.timeframe == oi_timeframe
            ]
            samples = derivative_samples(bars, self.derivatives(DerivativeKind.FUNDING_RATE), oi)
        points = operator.compute(bars, params, derivatives=samples)
        if timeframe_ms(requested) > timeframe_ms(self.timeframe):
            bars, points = bars[-1:], points[-1:]
        return tuple(
            IndicatorPoint(timestamp=bar.timestamp, values=values)
            for bar, values in zip(bars, points, strict=True)
        )


@dataclass(frozen=True, slots=True)
class Evaluation[T]:
    """一个收盘求值的截止时刻及其组件输出。"""

    as_of: int
    output: T


def run_history[T](
    evaluate: Callable[[Ctx], T],
    instrument: str,
    timeframe: str,
    *,
    start: int,
    end: int,
    history_start: int | None = None,
    read_bars: BarReader = sdk.bars,
    read_derivatives: DerivativeReader | None = None,
) -> Iterator[Evaluation[T]]:
    """在 Notebook 逐根求值，例如 `run_history(indicator.compute, ...)`。

    `start` 为首个开盘槽的下界，`end` 为收盘截止时刻，均包含。
    `history_start` 可向前扩展取数窗口用于预热。若在预定收盘时刻
    缺少当前确认柱，则跳过本次求值，后续时刻仍可读取随后确认的历史柱。
    """
    history_start = start if history_start is None else history_start
    if not 0 <= history_start <= start <= end:
        raise ValueError("须满足 0 <= history_start <= start <= end")
    interval = timeframe_ms(timeframe)
    first_close = ((start + interval - 1) // interval + 1) * interval
    for cutoff in range(first_close, end + 1, interval):
        ctx = Ctx(
            instrument, timeframe, history_start, cutoff,
            _read_bars=read_bars, _read_derivatives=read_derivatives,
        )
        bars = ctx.bars()
        if bars and bars[-1].ts_event == cutoff - interval:
            yield Evaluation(as_of=cutoff, output=evaluate(ctx))
