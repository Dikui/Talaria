"""收盘求值与按时点读取数据。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import Protocol

from talaria import sdk
from talaria.components.contracts import Series
from talaria.components.operators import OPERATORS
from talaria.foundation.bars import CompactBar, RangeBar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeKind,
    DerivativePoint,
)
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.indicators import IndicatorParams, IndicatorPoint
from talaria.foundation.operators import MAX_PERIOD, IndicatorName
from talaria.foundation.operators.inputs import derivative_samples
from talaria.foundation.timeframes import last_closed_bar_start, timeframe_ms
from talaria.perception.compaction import compact_series
from talaria.perception.raw_store import latest_visible_bars


class BarReader(Protocol):
    """每个事件时间只返回最高可见确认修订，按事件时间升序排列。"""

    def __call__(
        self, instrument: str, timeframe: str, *, start: int, end: int, as_of: int,
    ) -> Sequence[RangeBar]: ...


class DerivativeReader(Protocol):
    """按事件时间范围读取衍生品点，`start=0` 包含窗口前的历史。"""

    def __call__(
        self, instrument: str, kind: DerivativeKind, *, start: int, end: int, as_of: int,
    ) -> Sequence[DerivativePoint]: ...


_DERIVATIVE_EVENT_ORDER = attrgetter("ts_event", "ts_available", "ts_committed")


def _read_closed_bars(
    read: BarReader, instrument: str, timeframe: str, *, start: int, end: int, as_of: int,
) -> sdk.BarsResult:
    if end < start:
        return sdk.BarsResult((), tail_completed=True)
    rows = read(instrument, timeframe, start=start, end=end, as_of=as_of)
    visible = latest_visible_bars(
        (bar for bar in rows if bar.confirmed and start <= bar.ts_event <= end),
        as_of=as_of,
    )
    return sdk.BarsResult(tuple(visible), tail_completed=getattr(rows, "tail_completed", True))


@dataclass(slots=True)
class RuleState:
    """宿主持有的求值状态，每个回踩节点独立跟踪突破。"""

    breakouts: dict[int, tuple[str, int]] = field(default_factory=dict)
    consumed_sides: dict[int, str] = field(default_factory=dict)
    matched_retests: list[int] = field(default_factory=list)

    known_inputs: set[int] = field(default_factory=set)

    def consume_matches(self) -> None:
        for key in self.matched_retests:
            breakout = self.breakouts.pop(key, None)
            if breakout is not None:
                self.consumed_sides[key] = breakout[0]
        self.matched_retests.clear()


@dataclass(frozen=True, slots=True)
class Ctx:
    """当前观察的时点与输入。now 是 UTC 毫秒，event 是触发本次观察的行情。

    drawing 按 ID 返回在 now 已可见的画线，levels 是本次观察选定的一版关键位。
    状态信号的宿主在同一回放或订阅中复用 rule_state。
    """

    instrument: str
    timeframe: str
    start: int
    now: int
    _read_bars: BarReader = field(default=sdk.bars, repr=False, compare=False)
    _read_derivatives: DerivativeReader | None = field(default=None, repr=False, compare=False)
    event: FeedEvent | None = field(default=None, kw_only=True)
    drawing: Callable[[str], DrawingRecord | None] | None = field(default=None, kw_only=True)
    levels: Mapping[str, KeyLevel] = field(default_factory=dict, kw_only=True)
    rule_state: RuleState = field(default_factory=RuleState, kw_only=True, compare=False)
    _bar_ts: int | None = field(default=None, repr=False, compare=False, kw_only=True)
    _discovery_tail_completed: bool = field(default=True, repr=False, compare=False, kw_only=True)
    _indicator_defaults: Mapping[IndicatorName, IndicatorParams] = field(
        default_factory=sdk.indicator_defaults, repr=False, compare=False, kw_only=True,
    )
    _bars_by_timeframe: dict[str, sdk.BarsResult] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _derivatives_by_kind: dict[DerivativeKind, tuple[DerivativePoint, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    _confirmed_compact: Sequence[CompactBar] | None = field(
        default=None, repr=False, compare=False, kw_only=True,
    )
    _indicator_history_limit: int | None = field(default=None, repr=False, compare=False, kw_only=True)

    _session_prefix: Sequence[CompactBar] = field(default=(), repr=False, compare=False, kw_only=True)

    def compact_bars(self) -> tuple[CompactBar, ...]:
        """保留有界实时窗口中的缺口，同时按可获取时点截断确认柱。"""
        if self._confirmed_compact is None:
            return tuple(compact_series(self.bars(), timeframe=self.timeframe, cutoff_ts=self.now))
        end = self.now - timeframe_ms(self.timeframe)
        if self._bar_ts is not None:
            end = min(end, self._bar_ts)
        return tuple(
            bar for bar in self._confirmed_compact
            if self.start <= bar.timestamp <= end and bar.ts_available <= self.now
        )

    @property
    def tail_completed(self) -> bool:
        """本次确认时点查找和已读取的柱范围均已完成补尾查询。"""
        return self._discovery_tail_completed and all(
            bars.tail_completed for bars in self._bars_by_timeframe.values()
        )

    def _closed_bars(self, timeframe: str) -> sdk.BarsResult:
        if timeframe not in self._bars_by_timeframe:
            interval = timeframe_ms(timeframe)
            start = self.start
            end = last_closed_bar_start(self.now, interval)
            if interval > timeframe_ms(self.timeframe):
                # 覆盖最长周期算子的预热，额外一根供计算首个变化量。
                start = max(0, (start // interval - MAX_PERIOD - 1) * interval)
            if timeframe == self.timeframe and self._bar_ts is not None:
                end = min(end, self._bar_ts)
            self._bars_by_timeframe[timeframe] = _read_closed_bars(
                self._read_bars, self.instrument, timeframe, start=start, end=end, as_of=self.now,
            )
        return self._bars_by_timeframe[timeframe]

    def bars(self, timeframe: str | None = None) -> tuple[RangeBar, ...]:
        """返回确认柱。大于求值周期的查询最多返回最近一根。"""
        requested = self.timeframe if timeframe is None else timeframe
        bars = self._closed_bars(requested)
        return bars[-1:] if timeframe_ms(requested) > timeframe_ms(self.timeframe) else bars[:]

    def derivatives(self, kind: DerivativeKind | str) -> tuple[DerivativePoint, ...]:
        """按事件时间返回可获取的衍生品点，含每个序列窗口前最近一点。"""
        prior: dict[str | None, DerivativePoint] = {}
        in_window = []
        for point in self._visible_derivatives(DerivativeKind(kind)):
            if point.ts_event < self.start:
                prior[point.timeframe] = point
            else:
                in_window.append(point)
        return (*sorted(prior.values(), key=_DERIVATIVE_EVENT_ORDER), *in_window)

    def _visible_derivatives(self, kind: DerivativeKind) -> tuple[DerivativePoint, ...]:
        if self._read_derivatives is None:
            return ()
        if kind not in self._derivatives_by_kind:
            rows = self._read_derivatives(
                self.instrument, kind, start=0, end=self.now, as_of=self.now,
            )
            self._derivatives_by_kind[kind] = tuple(sorted(
                (
                    point for point in rows
                    if point.instrument_id == self.instrument and point.kind is kind
                    and 0 <= point.ts_event <= self.now and point.ts_available <= self.now
                ),
                key=_DERIVATIVE_EVENT_ORDER,
            ))
        return self._derivatives_by_kind[kind]

    def indicator(
        self, name: str, *, timeframe: str | None = None, version: str = "1",
        _history_limit: int | None = None, **params: object,
    ) -> Series:
        """计算已注册算子的 Decimal 序列，跨大周期只返回最后一个点。"""
        operator = OPERATORS.get(name, version=version)
        if operator.indicator_name is not None:
            defaults = self._indicator_defaults.get(operator.indicator_name)
            if defaults is not None:
                params = defaults.model_dump(exclude_none=True) | params
        requested = self.timeframe if timeframe is None else timeframe
        bars = (
            self.compact_bars() if requested == self.timeframe
            else compact_series(self._closed_bars(requested), timeframe=requested, cutoff_ts=self.now)
        )
        if operator.indicator_name is IndicatorName.VWAP and params.get("anchor", "session") == "session":
            if self._session_prefix:
                bars = tuple(bar for bar in self._session_prefix if bar.ts_available <= self.now) + tuple(bars)
        elif _history_limit is not None:
            period = int(params.get("period", 1))
            bars = bars[-max(_history_limit, period + 2):]
        samples = None
        if operator.derivative_inputs:
            timeframes = {
                DerivativeKind.FUNDING_RATE: None,
                DerivativeKind.OPEN_INTEREST: OI_TIMEFRAME_BY_TIMEFRAME[requested],
                DerivativeKind.MARK_PRICE: requested,
            }
            inputs = {
                kind: tuple(
                    point for point in self._visible_derivatives(kind)
                    if point.timeframe == timeframes[kind]
                )
                for kind in operator.derivative_inputs
            }
            samples = derivative_samples(
                bars, inputs.get(DerivativeKind.FUNDING_RATE, ()),
                inputs.get(DerivativeKind.OPEN_INTEREST, ()),
                mark_price=inputs.get(DerivativeKind.MARK_PRICE, ()),
            )
        points = operator.compute(bars, params, derivatives=samples)
        if timeframe_ms(requested) > timeframe_ms(self.timeframe):
            bars, points = bars[-1:], points[-1:]
        return tuple(
            IndicatorPoint(timestamp=bar.timestamp, values=values)
            for bar, values in zip(bars, points, strict=True)
        )


@dataclass(frozen=True, slots=True)
class Evaluation[T]:
    """一根确认柱的求值截止时刻及其组件输出。"""

    as_of: int
    output: T
    tail_completed: bool = True


def _confirmation_times(
    read: BarReader, instrument: str, timeframe: str, *, start: int, end: int,
) -> list[tuple[int, int, bool, RangeBar]]:
    interval = timeframe_ms(timeframe)
    latest = _read_closed_bars(
        read, instrument, timeframe, start=start,
        end=last_closed_bar_start(end, interval), as_of=end,
    )
    times = []
    for bar in latest:
        slot = bar.ts_event
        close = slot + interval
        tail_completed = latest.tail_completed
        while bar.ts_available > close:
            # 读取器只返回最高可见修订。退到它可见前，找出首次确认时点。
            earlier = _read_closed_bars(
                read, instrument, timeframe, start=slot, end=slot, as_of=bar.ts_available - 1,
            )
            tail_completed &= earlier.tail_completed
            if not earlier:
                break
            bar = earlier[0]
        times.append((max(close, bar.ts_available), slot, tail_completed, bar))
    return sorted(times, key=lambda item: item[:2])


def run_history[T](
    evaluate: Callable[[Ctx], T],
    instrument: str,
    timeframe: str,
    *,
    start: int,
    end: int,
    history_start: int | None = None,
    read_bars: BarReader | None = None,
    read_derivatives: DerivativeReader | None = None,
    drawing_at: Callable[[str, int], DrawingRecord | None] | None = None,
    levels_at: Callable[[str, str, int], Sequence[KeyLevel]] | None = None,
    data_dir: str | Path | None = None,
) -> Iterator[Evaluation[T]]:
    """在 Notebook 逐根求值，例如 `run_history(indicator.compute, ...)`。

    `start` 为首个开盘槽的下界，`end` 为可获取时间的上界，均包含。
    每根柱首次确认可见时求值，同一时刻按开盘槽排序。
    `history_start` 可向前扩展取数窗口用于预热。
    `drawing_at(id, as_of)` 按可见时点取画线，`levels_at(instrument, timeframe, close)` 按收盘时刻取关键位。
    `data_dir` 为默认 K 线读取器和工作台指标默认值所用的数据目录。
    """
    history_start = start if history_start is None else history_start
    if not 0 <= history_start <= start <= end:
        raise ValueError("须满足 0 <= history_start <= start <= end")
    if read_bars is None:
        read_bars = partial(sdk.bars, data_dir=data_dir)
    confirmations = _confirmation_times(read_bars, instrument, timeframe, start=start, end=end)
    defaults = sdk.indicator_defaults(data_dir=data_dir) if confirmations else {}
    state = RuleState()
    for cutoff, slot, tail_completed, bar in confirmations:
        event = KlineEvent(instrument_id=instrument, timeframe=timeframe, confirmed=True, revision=bar.revision,
                           bar=compact_series([bar], timeframe=timeframe, cutoff_ts=cutoff)[0])
        levels = levels_at(instrument, timeframe, slot + timeframe_ms(timeframe)) if levels_at else ()

        def drawing(drawing_id: str, as_of: int = cutoff) -> DrawingRecord | None:
            return drawing_at(drawing_id, as_of) if drawing_at else None

        ctx = Ctx(
            instrument, timeframe, history_start, cutoff,
            _read_bars=read_bars, _read_derivatives=read_derivatives, _bar_ts=slot,
            _discovery_tail_completed=tail_completed, _indicator_defaults=defaults,
            event=event, drawing=drawing, levels={level.level_id: level for level in levels}, rule_state=state,
        )
        bars = ctx.bars()
        if bars and bars[-1].ts_event == slot:
            output = evaluate(ctx)
            yield Evaluation(as_of=cutoff, output=output, tail_completed=ctx.tail_completed)
