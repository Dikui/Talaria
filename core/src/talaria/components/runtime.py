"""收盘求值与按时点读取数据。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial
from operator import attrgetter
from pathlib import Path
from typing import Protocol, overload

from talaria import sdk
from talaria.components.contracts import Series
from talaria.components.operators import OPERATORS, Operator
from talaria.foundation.bars import BarStatus, CompactBar, RangeBar, classify_flat_bar
from talaria.foundation.derivatives import (
    OI_TIMEFRAME_BY_TIMEFRAME,
    DerivativeKind,
    DerivativePoint,
    derivative_identity,
    latest_visible_derivatives,
)
from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.feed import FeedEvent, KlineEvent
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.indicators import IndicatorParams, IndicatorPoint
from talaria.foundation.operators import MAX_PERIOD, IndicatorName, spec_from_params
from talaria.foundation.operators.core import OperatorCursor, DerivativeSample
from talaria.foundation.operators.inputs import derivative_samples
from talaria.foundation.operators.spec import IndicatorSpec
from talaria.foundation.revisions import latest_visible_revisions
from talaria.foundation.rules import RuleIndicator
from talaria.foundation.timeframes import last_closed_bar_start, timeframe_ms
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader
from talaria.perception.compaction import MAX_GAP_FILL, compact_series
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


class _SeriesView[T](Sequence[T]):
    """Zero-copy prefix view over a growing list, supporting tuple equality."""

    __slots__ = ("_items", "_length", "_epoch")

    def __init__(self, items: Sequence[T], length: int, epoch: int = 0) -> None:
        self._items = items
        self._length = length
        self._epoch = epoch

    def __len__(self) -> int:
        return self._length

    @overload
    def __getitem__(self, index: int) -> T: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[T, ...]: ...

    def __getitem__(self, index: int | slice) -> T | tuple[T, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            if step == 1:
                return tuple(self._items[start:stop])
            return tuple(self._items[i] for i in range(start, stop, step))
        if index < 0:
            index += self._length
        if not 0 <= index < self._length:
            raise IndexError(index)
        return self._items[index]

    def __iter__(self) -> Iterator[T]:
        items = self._items
        for i in range(self._length):
            yield items[i]

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return len(other) == self._length and all(
                self._items[i] == other[i] for i in range(self._length)
            )
        if isinstance(other, _SeriesView):
            return self._length == other._length and (
                (self._items is other._items and self._epoch == other._epoch)
                or all(self._items[i] == other._items[i] for i in range(self._length))
            )
        return NotImplemented

    def __repr__(self) -> str:
        return repr(self[:])


class _CachedBarsResult(Sequence[RangeBar]):
    """Prefix-backed BarsResult view avoiding O(N^2) slicing and copying."""

    __slots__ = ("_items", "_length", "_epoch", "tail_completed")

    def __init__(self, items: Sequence[RangeBar], length: int, epoch: int, *, tail_completed: bool) -> None:
        self._items = items
        self._length = length
        self._epoch = epoch
        self.tail_completed = tail_completed

    def __len__(self) -> int:
        return self._length

    @overload
    def __getitem__(self, index: int) -> RangeBar: ...

    @overload
    def __getitem__(self, index: slice) -> tuple[RangeBar, ...]: ...

    def __getitem__(self, index: int | slice) -> RangeBar | tuple[RangeBar, ...]:
        if isinstance(index, slice):
            start, stop, step = index.indices(self._length)
            if step == 1:
                return tuple(self._items[start:stop])
            return tuple(self._items[i] for i in range(start, stop, step))
        if index < 0:
            index += self._length
        if not 0 <= index < self._length:
            raise IndexError(index)
        return self._items[index]

    def __iter__(self) -> Iterator[RangeBar]:
        items = self._items
        for i in range(self._length):
            yield items[i]

    def as_view(self) -> _SeriesView[RangeBar]:
        return _SeriesView(self._items, self._length, self._epoch)


def _compact_from_range(bar: RangeBar) -> CompactBar:
    status = classify_flat_bar(
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
    )
    return CompactBar(
        timestamp=bar.ts_event,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        turnover=bar.turnover,
        status=status,
        ts_available=bar.ts_available,
        precision=bar.precision,
    )


class _ColdSeriesIndex:
    """Single-read incremental bar and compaction index for a ColdReader series."""

    def __init__(
        self, reader: ColdReader, instrument_id: str, timeframe: str, *, start: int, end: int,
    ) -> None:
        self._reader = reader
        self._spec = get_market_pack(DEFAULT_PACK).instrument(instrument_id)
        self._timeframe = timeframe
        self._interval = timeframe_ms(timeframe)
        self._loaded_start = start
        self._loaded_end = end
        self._revisions: list = []
        self._by_available: list[tuple[int, int, object]] = []
        self._avail_ptr = 0
        self._last_as_of = -1
        self._epoch = 0
        self._chosen: dict[int, tuple[tuple[int, int, int, int], RangeBar]] = {}
        self._ts_events: list[int] = []
        self._range_bars: list[RangeBar] = []
        self._compact_ts: list[int] = []
        self._compact_bars: list[CompactBar] = []
        self._load(start, end)

    def _load(self, start: int, end: int) -> None:
        self._loaded_start = start
        self._loaded_end = end
        raws = self._reader.read_revisions(
            self._spec, self._timeframe, start_ts=start, end_ts=end,
        )
        self._revisions = [raw for raw in raws if getattr(raw, "confirmed", True)]
        self._by_available = sorted(
            ((raw.ts_available, idx, raw) for idx, raw in enumerate(self._revisions)),
            key=lambda item: (item[0], item[1]),
        )
        self._reset_cursor()

    def _reset_cursor(self) -> None:
        self._avail_ptr = 0
        self._last_as_of = -1
        self._epoch += 1
        self._chosen.clear()
        self._ts_events.clear()
        self._range_bars.clear()
        self._compact_ts.clear()
        self._compact_bars.clear()

    def ensure_range(self, start: int, end: int) -> None:
        if start < self._loaded_start or end > self._loaded_end:
            self._load(min(start, self._loaded_start), max(end, self._loaded_end))

    def _append_compact_bar(self, rb: RangeBar) -> None:
        if self._compact_ts:
            missing = (rb.ts_event - self._compact_ts[-1]) // self._interval - 1
            if 0 < missing <= MAX_GAP_FILL:
                for step in range(1, missing + 1):
                    gap_ts = self._compact_ts[-1] + self._interval
                    self._compact_ts.append(gap_ts)
                    self._compact_bars.append(CompactBar(
                        timestamp=gap_ts,
                        open=None,
                        high=None,
                        low=None,
                        close=None,
                        volume=None,
                        turnover=None,
                        status=BarStatus.FEED_GAP,
                        ts_available=gap_ts + self._interval,
                        precision=rb.precision,
                    ))
        self._compact_ts.append(rb.ts_event)
        self._compact_bars.append(_compact_from_range(rb))

    def _rebuild_compact_from(self, bar_pos: int) -> None:
        if bar_pos <= 0:
            self._compact_ts.clear()
            self._compact_bars.clear()
            for rb in self._range_bars:
                self._append_compact_bar(rb)
            return
        prev_ts = self._range_bars[bar_pos - 1].ts_event
        prev_cpos = bisect_left(self._compact_ts, prev_ts)
        del self._compact_ts[prev_cpos + 1:]
        del self._compact_bars[prev_cpos + 1:]
        for rb in self._range_bars[bar_pos:]:
            self._append_compact_bar(rb)

    def advance(self, as_of: int) -> None:
        if as_of < self._last_as_of:
            self._reset_cursor()
        by_avail = self._by_available
        n = len(by_avail)
        ptr = self._avail_ptr
        while ptr < n and by_avail[ptr][0] <= as_of:
            _, idx, raw = by_avail[ptr]
            ptr += 1
            version = (raw.revision, raw.ts_available, raw.ts_ingested, idx)
            current = self._chosen.get(raw.ts_event)
            if current is not None and version < current[0]:
                continue
            rb = RangeBar.model_validate(raw, from_attributes=True)
            self._chosen[raw.ts_event] = (version, rb)
            if current is None and (not self._ts_events or raw.ts_event > self._ts_events[-1]):
                self._ts_events.append(raw.ts_event)
                self._range_bars.append(rb)
                self._append_compact_bar(rb)
            else:
                self._epoch += 1
                pos = bisect_left(self._ts_events, raw.ts_event)
                if current is not None:
                    prev_rb = self._range_bars[pos]
                    self._range_bars[pos] = rb
                    if pos == len(self._range_bars) - 1 and rb.precision == prev_rb.precision:
                        self._compact_bars[-1] = _compact_from_range(rb)
                    else:
                        self._rebuild_compact_from(pos)
                else:
                    self._ts_events.insert(pos, raw.ts_event)
                    self._range_bars.insert(pos, rb)
                    self._rebuild_compact_from(pos)
        self._avail_ptr = ptr
        self._last_as_of = as_of

    def query_bars(self, start: int, end: int, as_of: int) -> _CachedBarsResult | sdk.BarsResult:
        if end < start:
            return sdk.BarsResult((), tail_completed=False)
        self.ensure_range(start, end)
        self.advance(as_of)
        if not self._ts_events:
            return sdk.BarsResult((), tail_completed=False)
        lo = 0 if start <= self._ts_events[0] else bisect_left(self._ts_events, start)
        hi = len(self._ts_events) if end >= self._ts_events[-1] else bisect_right(self._ts_events, end)
        if lo == 0:
            return _CachedBarsResult(self._range_bars, hi, self._epoch, tail_completed=False)
        return sdk.BarsResult(tuple(self._range_bars[lo:hi]), tail_completed=False)

    def query_compact(self, start: int, end: int, as_of: int) -> Sequence[CompactBar]:
        if end < start:
            return ()
        self.ensure_range(start, end)
        self.advance(as_of)
        if not self._compact_ts:
            return ()
        if start <= self._compact_ts[0]:
            hi = len(self._compact_ts) if end >= self._compact_ts[-1] else bisect_right(self._compact_ts, end)
            return _SeriesView(self._compact_bars, hi, self._epoch)
        lo = bisect_left(self._compact_ts, start)
        hi = len(self._compact_ts) if end >= self._compact_ts[-1] else bisect_right(self._compact_ts, end)
        return tuple(self._compact_bars[lo:hi])

    def confirmed_bars(
        self, start: int, end: int, first_cache: dict[tuple[str, int], tuple[int, RangeBar, bool]],
    ) -> list[tuple[int, RangeBar, bool]]:
        last_slot = last_closed_bar_start(end, self._interval)
        if last_slot < start:
            return []
        self.ensure_range(start, last_slot)
        by_slot: dict[int, list] = {}
        for raw in self._revisions:
            if start <= raw.ts_event <= last_slot and raw.ts_available <= end:
                by_slot.setdefault(raw.ts_event, []).append(raw)
        times: list[tuple[int, RangeBar, bool]] = []
        ingested = lambda row: row.ts_ingested
        identity = lambda row: row.ts_event
        for slot, slot_revs in by_slot.items():
            close = slot + self._interval
            latest = latest_visible_revisions(slot_revs, as_of=end, identity=identity, ingested=ingested)
            if not latest:
                continue
            bar = latest[0]
            earlier_complete = True
            while bar.ts_available > close:
                earlier_complete = False
                earlier = latest_visible_revisions(
                    slot_revs, as_of=bar.ts_available - 1, identity=identity, ingested=ingested,
                )
                if not earlier:
                    break
                bar = earlier[0]
            cutoff = max(close, bar.ts_available)
            rb = RangeBar.model_validate(bar, from_attributes=True)
            first_cache[(self._timeframe, slot)] = (cutoff, rb, earlier_complete)
            times.append((cutoff, rb, False))
        return sorted(times, key=lambda step: (step[0], step[1].ts_event))


class _ColdBarCache:
    """Per-run cache of _ColdSeriesIndex instances keyed by (instrument, timeframe)."""

    def __init__(self, reader: ColdReader, default_start: int, default_end: int) -> None:
        self._reader = reader
        self._default_start = default_start
        self._default_end = default_end
        self._series: dict[tuple[str, str], _ColdSeriesIndex] = {}

    def series(self, instrument: str, timeframe: str, *, start: int | None = None, end: int | None = None) -> _ColdSeriesIndex:
        key = (instrument, timeframe)
        index = self._series.get(key)
        if index is None:
            s = self._default_start if start is None else min(start, self._default_start)
            e = self._default_end if end is None else max(end, self._default_end)
            index = _ColdSeriesIndex(self._reader, instrument, timeframe, start=s, end=e)
            self._series[key] = index
        return index


@dataclass(slots=True)
class _IndicatorCursorState:
    cursor: OperatorCursor
    points: list[IndicatorPoint]
    processed_len: int
    backing_id: int = 0
    epoch: int = -1
    bars_tuple: tuple[CompactBar, ...] = ()
    samples_tuple: tuple[DerivativeSample, ...] | None = None


@dataclass(slots=True)
class _ReplaySession:
    """Per-run state shared across Ctx instances in run_history and ParquetReplay."""

    cold_cache: _ColdBarCache | None = None
    reader: BarReader | None = None
    first_confirmations: dict[tuple[str, int], tuple[int, RangeBar, bool]] = field(default_factory=dict)
    indicator_cursors: dict[tuple, _IndicatorCursorState] = field(default_factory=dict)


class _ColdCachedReader:
    """BarReader backed by _ColdBarCache with an attached _ReplaySession."""

    def __init__(self, reader: ColdReader, default_start: int, default_end: int) -> None:
        self._cache = _ColdBarCache(reader, default_start, default_end)
        self._talaria_replay_session = _ReplaySession(cold_cache=self._cache, reader=self)

    def __call__(
        self, instrument: str, timeframe: str, *, start: int, end: int, as_of: int,
    ) -> _CachedBarsResult | sdk.BarsResult:
        return self._cache.series(instrument, timeframe, start=start, end=end).query_bars(start, end, as_of)


def cached_cold_bar_reader(reader: ColdReader, *, start: int, end: int) -> BarReader:
    """Build a per-run cached ColdReader BarReader with incremental Ctx session."""
    return _ColdCachedReader(reader, start, end)


def _read_closed_bars(
    read: BarReader, instrument: str, timeframe: str, *, start: int, end: int, as_of: int,
) -> sdk.BarsResult | _CachedBarsResult:
    if end < start:
        return sdk.BarsResult((), tail_completed=True)
    if isinstance(read, _ColdCachedReader):
        return read(instrument, timeframe, start=start, end=end, as_of=as_of)
    rows = read(instrument, timeframe, start=start, end=end, as_of=as_of)
    if isinstance(rows, _CachedBarsResult):
        return rows
    visible = latest_visible_bars(
        (bar for bar in rows if bar.confirmed and start <= bar.ts_event <= end),
        as_of=as_of,
    )
    return sdk.BarsResult(
        tuple(bar.model_copy() for bar in visible), tail_completed=getattr(rows, "tail_completed", True),
    )


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


@dataclass(slots=True)
class RuleIndicatorHistory:
    bars: tuple[CompactBar, ...]
    cursor: OperatorCursor
    tail: Series = ()

    def extend(self, ctx: Ctx, bars: tuple[CompactBar, ...], operator: Operator) -> Series:
        new = bars[len(self.bars):]
        if new:
            samples = ctx._operator_samples(operator, ctx.timeframe, new, include_ticks=True)
            points = self.cursor.append(new, samples)
            self.tail = (*self.tail, *(IndicatorPoint(timestamp=b.timestamp, values=v)
                                      for b, v in zip(new[-2:], points[-2:], strict=True)))[-2:]
            self.bars = bars
        return self.tail


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
    _data_dir: str | Path | None = field(default=None, repr=False, compare=False, kw_only=True)
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
    _read_derivative_revisions: Callable[[DerivativeKind], Sequence[DerivativePoint]] | None = field(
        default=None, repr=False, compare=False, kw_only=True,
    )

    _confirmed_compact: Sequence[CompactBar] | None = field(
        default=None, repr=False, compare=False, kw_only=True,
    )
    _first_confirmed_at: Callable[[int], int | None] | None = field(
        default=None, repr=False, compare=False, kw_only=True,
    )
    _indicator_history_limit: int | None = field(default=None, repr=False, compare=False, kw_only=True)

    _indicator_history: dict[tuple, RuleIndicatorHistory] | None = field(default=None, kw_only=True, compare=False)
    _session_prefix: Sequence[CompactBar] = field(default=(), repr=False, compare=False, kw_only=True)
    _replay_session: _ReplaySession | None = field(default=None, repr=False, compare=False, kw_only=True)
    _rule_indicators: dict[tuple[object, ...], Series] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )
    _indicators_cache: dict[tuple[object, ...], Series] = field(
        default_factory=dict, init=False, repr=False, compare=False,
    )

    def _session(self) -> _ReplaySession | None:
        return self._replay_session or getattr(self._read_bars, "_talaria_replay_session", None)

    def _cold_cache(self) -> _ColdBarCache | None:
        session = self._session()
        if session is not None and session.cold_cache is not None and self._read_bars is session.reader:
            return session.cold_cache
        return None

    def compact_bars(self) -> Sequence[CompactBar]:
        """保留有界实时窗口中的缺口，同时按可获取时点截断确认柱。"""
        if self._confirmed_compact is None:
            cold = self._cold_cache()
            if cold is not None:
                interval = timeframe_ms(self.timeframe)
                end = last_closed_bar_start(self.now, interval)
                if self._bar_ts is not None:
                    end = min(end, self._bar_ts)
                return cold.series(self.instrument, self.timeframe, start=self.start, end=end).query_compact(
                    self.start, end, self.now,
                )
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

    def _closed_bars(self, timeframe: str) -> sdk.BarsResult | _CachedBarsResult:
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

    def bars(self, timeframe: str | None = None) -> Sequence[RangeBar]:
        """返回确认柱。大于求值周期的查询最多返回最近一根。"""
        requested = self.timeframe if timeframe is None else timeframe
        bars = self._closed_bars(requested)
        if timeframe_ms(requested) > timeframe_ms(self.timeframe):
            return bars[-1:]
        if isinstance(bars, _CachedBarsResult):
            return bars.as_view()
        return bars[:]

    def derivatives(self, kind: DerivativeKind | str, *, as_of: int | None = None) -> tuple[DerivativePoint, ...]:
        """按事件时间返回可获取的衍生品点，含每个序列窗口前最近一点。"""
        kind = DerivativeKind(kind)
        cutoff = self.now if as_of is None else min(as_of, self.now)
        if cutoff < self.now and self._read_derivatives is not None:
            # 外部读取器可能仅返回截止时刻的最高版本。
            rows = self._read_derivatives(self.instrument, kind, start=0, end=cutoff, as_of=cutoff)
        else:
            rows = self._derivative_revisions(kind)
        prior: dict[tuple[str | None, bool], DerivativePoint] = {}
        in_window = []
        for point in latest_visible_derivatives(rows, as_of=cutoff):
            if point.instrument_id != self.instrument or point.kind is not kind or point.ts_event < 0:
                continue
            if point.ts_event < self.start:
                prior[point.timeframe, point.is_funding_settlement] = point
            else:
                in_window.append(point)
        return (*sorted(prior.values(), key=_DERIVATIVE_EVENT_ORDER), *in_window)

    def _derivative_revisions(self, kind: DerivativeKind) -> tuple[DerivativePoint, ...]:
        if kind not in self._derivatives_by_kind:
            if self._read_derivative_revisions is not None:
                rows = self._read_derivative_revisions(kind)
            elif self._read_derivatives is None:
                rows = sdk._archived_derivative_revisions(
                    self.instrument, kind, end=self.now, data_dir=self._data_dir,
                )
            else:
                rows = self._read_derivatives(self.instrument, kind, start=0, end=self.now, as_of=self.now)
            self._derivatives_by_kind[kind] = tuple(
                point for point in rows
                if point.instrument_id == self.instrument and point.kind is kind
                and 0 <= point.ts_event <= self.now and point.ts_available <= self.now
            )
        return self._derivatives_by_kind[kind]

    def _indicator_derivatives(
        self, kind: DerivativeKind, timeframe: str | None, earliest_cutoff: int, *, include_ticks: bool,
    ) -> list[DerivativePoint]:
        def matches(point: DerivativePoint) -> bool:
            return (point.timeframe == timeframe or (include_ticks and point.timeframe is None)) and (
                kind is not DerivativeKind.FUNDING_RATE or point.is_funding_settlement
            )

        points = [point for point in self._derivative_revisions(kind) if matches(point)]
        read = self._read_derivatives
        if read is not None:
            # sdk.derivatives 等读取器每次只返回最高可见修订，按需回查被遮住的版本。
            for latest in latest_visible_derivatives(points):
                identity = derivative_identity(latest)
                point = latest
                while point.revision > 0 and point.ts_available > earliest_cutoff:
                    cutoff = point.ts_available - 1
                    earlier = latest_visible_derivatives(
                        (prior for prior in read(
                            self.instrument, kind, start=point.ts_event, end=point.ts_event, as_of=cutoff,
                        ) if derivative_identity(prior) == identity),
                        as_of=cutoff,
                    )
                    if not earlier:
                        break
                    point = earlier[0]
                    points.append(point)
        return points

    def _bar_evaluation_times(self, timeframe: str, bars: Sequence[CompactBar]) -> list[int]:
        interval = timeframe_ms(timeframe)
        if timeframe == self.timeframe and self._confirmed_compact is not None:
            times = []
            for bar in bars:
                first = self._first_confirmed_at(bar.timestamp) if self._first_confirmed_at else None
                times.append(max(bar.timestamp + interval, bar.ts_available if first is None else first))
            return times
        closed = self._closed_bars(timeframe)
        rows = {row.ts_event: row for row in closed}
        tail_completed = closed.tail_completed
        session = self._session()
        first_cache = session.first_confirmations if session is not None else None
        times = []
        for bar in bars:
            row = rows.get(bar.timestamp)
            if row is None:
                times.append(bar.ts_available)
                continue
            close = row.ts_event + interval
            if row.ts_available <= close:
                times.append(close)
                continue
            key = (timeframe, row.ts_event)
            cached = first_cache.get(key) if first_cache is not None else None
            if cached is not None:
                cutoff, _, complete = cached
            else:
                cutoff, first_bar, complete = _first_confirmation(
                    self._read_bars, self.instrument, timeframe, row, interval,
                )
                if first_cache is not None:
                    first_cache[key] = (cutoff, first_bar, complete)
            tail_completed &= complete
            times.append(cutoff)
        if closed.tail_completed and not tail_completed:
            self._bars_by_timeframe[timeframe] = sdk.BarsResult(tuple(closed), tail_completed=False)
        return times

    def _compact_for_timeframe(self, requested: str) -> Sequence[CompactBar]:
        if requested == self.timeframe:
            return self.compact_bars()
        cold = self._cold_cache()
        if cold is not None:
            interval = timeframe_ms(requested)
            start = self.start
            if interval > timeframe_ms(self.timeframe):
                start = max(0, (start // interval - MAX_PERIOD - 1) * interval)
            end = last_closed_bar_start(self.now, interval)
            return cold.series(self.instrument, requested, start=start, end=end).query_compact(start, end, self.now)
        return compact_series(self._closed_bars(requested), timeframe=requested, cutoff_ts=self.now)

    def indicator(
        self, name: str, *, timeframe: str | None = None, version: str = "1",
        _history_limit: int | None = None, _include_ticks: bool = False,
        _output_limit: int | None = None, **params: object,
    ) -> Series:
        """计算已注册算子的 Decimal 序列，跨大周期只返回最后一个点。"""
        operator = OPERATORS.get(name, version=version)
        if operator.indicator_name is not None:
            defaults = self._indicator_defaults.get(operator.indicator_name)
            if defaults is not None:
                params = defaults.model_dump(exclude_none=True) | params
        requested = self.timeframe if timeframe is None else timeframe
        cache_key: tuple[object, ...] | None = None
        try:
            cache_key = (
                name, version, requested, _history_limit, _include_ticks, _output_limit,
                tuple(sorted(params.items())),
            )
            cached = self._indicators_cache.get(cache_key)
            if cached is not None:
                return cached
        except TypeError:
            cache_key = None

        session = self._session()
        if session is not None and operator.indicator_name is not None:
            result = self._indicator_incremental(
                session, operator, requested, params,
                history_limit=_history_limit, include_ticks=_include_ticks, output_limit=_output_limit,
            )
        else:
            bars = self._compact_for_timeframe(requested)
            if operator.indicator_name is IndicatorName.VWAP and params.get("anchor", "session") == "session":
                if self._session_prefix:
                    bars = tuple(bar for bar in self._session_prefix if bar.ts_available <= self.now) + tuple(bars)
            elif _history_limit is not None:
                period = int(params.get("period", 1))
                bars = bars[-max(_history_limit, period + 2):]
            samples = self._operator_samples(operator, requested, bars, include_ticks=_include_ticks)
            points = operator.compute(bars, params, derivatives=samples)
            if timeframe_ms(requested) > timeframe_ms(self.timeframe):
                bars, points = bars[-1:], points[-1:]
            if _output_limit is not None:
                bars, points = bars[-_output_limit:], points[-_output_limit:]
            result = tuple(
                IndicatorPoint(timestamp=bar.timestamp, values=values)
                for bar, values in zip(bars, points, strict=True)
            )
        if cache_key is not None:
            self._indicators_cache[cache_key] = result
        return result

    def _indicator_incremental(
        self,
        session: _ReplaySession,
        operator: Operator,
        requested: str,
        params: Mapping[str, object],
        *,
        history_limit: int | None,
        include_ticks: bool,
        output_limit: int | None,
    ) -> Sequence[IndicatorPoint]:
        assert operator.indicator_name is not None
        spec = spec_from_params(operator.indicator_name, params)
        bars = self._compact_for_timeframe(requested)
        session_vwap_prefix = (
            operator.indicator_name is IndicatorName.VWAP
            and spec.anchor == "session"
            and bool(self._session_prefix)
        )
        if session_vwap_prefix:
            bars = tuple(bar for bar in self._session_prefix if bar.ts_available <= self.now) + tuple(bars)
        elif history_limit is not None:
            bars = bars[-max(history_limit, int(spec.period) + 2):]

        cursor_key = (
            self.instrument, requested, self.start, operator.name, operator.version,
            spec, include_ticks, history_limit, session_vwap_prefix,
        )
        state = session.indicator_cursors.get(cursor_key)
        legacy = operator.name.startswith("legacy_")

        if isinstance(bars, _SeriesView) and not operator.derivative_inputs:
            n = len(bars)
            backing_id = id(bars._items)
            epoch = bars._epoch
            if (
                state is not None
                and state.backing_id == backing_id
                and state.epoch == epoch
                and n >= state.processed_len
            ):
                if n > state.processed_len:
                    new_bars = bars._items[state.processed_len:n]
                    new_points = state.cursor.append(new_bars, None)
                    state.points.extend(
                        IndicatorPoint.model_construct(timestamp=b.timestamp, values=v)
                        for b, v in zip(new_bars, new_points, strict=True)
                    )
                    state.processed_len = n
            else:
                cursor = OperatorCursor(spec, legacy=legacy)
                all_bars = bars._items[:n]
                all_points = cursor.append(all_bars, None)
                pts = [
                    IndicatorPoint.model_construct(timestamp=b.timestamp, values=v)
                    for b, v in zip(all_bars, all_points, strict=True)
                ]
                state = _IndicatorCursorState(
                    cursor=cursor, points=pts, processed_len=n, backing_id=backing_id, epoch=epoch,
                )
                session.indicator_cursors[cursor_key] = state
        else:
            bars_seq = bars if isinstance(bars, tuple) else tuple(bars)
            samples = self._operator_samples(operator, requested, bars_seq, include_ticks=include_ticks)
            samples_tuple = None if samples is None else tuple(samples)
            n = len(bars_seq)
            if (
                state is not None
                and n >= state.processed_len
                and state.backing_id == 0
                and bars_seq[:state.processed_len] == state.bars_tuple
                and (samples_tuple is None or (
                    state.samples_tuple is not None
                    and samples_tuple[:state.processed_len] == state.samples_tuple
                ))
            ):
                if n > state.processed_len:
                    new_bars = bars_seq[state.processed_len:]
                    new_samples = None if samples_tuple is None else samples_tuple[state.processed_len:]
                    new_points = state.cursor.append(new_bars, new_samples)
                    state.points.extend(
                        IndicatorPoint.model_construct(timestamp=b.timestamp, values=v)
                        for b, v in zip(new_bars, new_points, strict=True)
                    )
                    state.bars_tuple = bars_seq
                    state.samples_tuple = samples_tuple
                    state.processed_len = n
            else:
                cursor = OperatorCursor(spec, legacy=legacy)
                all_points = cursor.append(bars_seq, samples_tuple)
                pts = [
                    IndicatorPoint.model_construct(timestamp=b.timestamp, values=v)
                    for b, v in zip(bars_seq, all_points, strict=True)
                ]
                state = _IndicatorCursorState(
                    cursor=cursor, points=pts, processed_len=n,
                    bars_tuple=bars_seq, samples_tuple=samples_tuple,
                )
                session.indicator_cursors[cursor_key] = state

        n = state.processed_len
        if timeframe_ms(requested) > timeframe_ms(self.timeframe):
            res = (state.points[n - 1],) if n > 0 else ()
            return res[-output_limit:] if output_limit is not None else res
        if output_limit is not None:
            start_idx = max(0, n - output_limit)
            return tuple(state.points[start_idx:n])
        return _SeriesView(state.points, n, state.epoch)

    def _operator_samples(self, operator: Operator, requested: str, bars: Sequence[CompactBar],
                          *, include_ticks: bool) -> Sequence[DerivativeSample] | None:
        if operator.derivative_inputs:
            timeframes = {
                DerivativeKind.FUNDING_RATE: None,
                DerivativeKind.OPEN_INTEREST: OI_TIMEFRAME_BY_TIMEFRAME[requested],
                DerivativeKind.MARK_PRICE: requested,
            }
            evaluations = self._bar_evaluation_times(requested, bars)
            earliest = min(evaluations, default=self.now)
            inputs = {
                kind: self._indicator_derivatives(kind, timeframes[kind], earliest, include_ticks=include_ticks)
                for kind in operator.derivative_inputs
            }
            return derivative_samples(
                bars, inputs.get(DerivativeKind.FUNDING_RATE, ()), inputs.get(DerivativeKind.OPEN_INTEREST, ()),
                mark_price=inputs.get(DerivativeKind.MARK_PRICE, ()), as_of=evaluations,
            )
        return None

    def _rule_indicator(self, operand: RuleIndicator, history_limit: int) -> Series:
        params = operand.params
        limit = (self._indicator_history_limit or history_limit) if operand.window == "alert" else None
        key = (operand.name, operand.version, params.period, params.anchor, params.std_dev, limit)
        if key in self._rule_indicators:
            return self._rule_indicators[key]
        settings = params.model_dump(exclude_none=True)
        if self._indicator_history is None:
            self._rule_indicators[key] = self.indicator(
                operand.name, version=operand.version, _history_limit=limit,
                _include_ticks=True, _output_limit=2, **settings,
            )
        else:
            operator = OPERATORS.get(operand.name, version=operand.version)
            defaults = self._indicator_defaults.get(operand.indicator_name)
            if defaults is not None:
                settings = defaults.model_dump(exclude_none=True) | settings
            spec = spec_from_params(operand.indicator_name, settings)
            bars = self.compact_bars()
            if spec.name is IndicatorName.VWAP and spec.anchor == "session":
                bars = tuple(b for b in self._session_prefix if b.ts_available <= self.now) + bars
            elif limit and spec.name not in {IndicatorName.SMA, IndicatorName.BOLL}:
                bars = bars[-max(limit, spec.period + 2):]
            # Revisions, late insertions and sliding recursive windows require
            # a new seed. Ordinary appends advance the shared operator once.
            history = self._indicator_history.get(key)
            if history is None or bars[:len(history.bars)] != history.bars:
                history = RuleIndicatorHistory((), OperatorCursor(spec, legacy=operand.name.startswith("legacy_")))
                self._indicator_history[key] = history
            self._rule_indicators[key] = history.extend(self, bars, operator)
        return self._rule_indicators[key]


@dataclass(frozen=True, slots=True)
class Evaluation[T]:
    """一根确认柱的求值截止时刻及其组件输出。"""

    as_of: int
    output: T
    tail_completed: bool = True


def _first_confirmation(
    read: BarReader, instrument: str, timeframe: str, bar: RangeBar, interval: int,
) -> tuple[int, RangeBar, bool]:
    slot = bar.ts_event
    close = slot + interval
    tail_completed = True
    while bar.ts_available > close:
        # 读取器只返回最高可见修订。退到它可见前，找出首次确认时点。
        earlier = _read_closed_bars(
            read, instrument, timeframe, start=slot, end=slot, as_of=bar.ts_available - 1,
        )
        tail_completed &= earlier.tail_completed
        if not earlier:
            break
        bar = earlier[0]
    return max(close, bar.ts_available), bar, tail_completed


def confirmed_bars(
    read: BarReader, instrument: str, timeframe: str, *, start: int, end: int,
) -> list[tuple[int, RangeBar, bool]]:
    """返回各柱首次确认可用的时点、该版本的行情和补尾状态。"""
    session = getattr(read, "_talaria_replay_session", None)
    if isinstance(read, _ColdCachedReader) and session is not None and session.cold_cache is not None:
        return session.cold_cache.series(instrument, timeframe, start=start, end=end).confirmed_bars(
            start, end, session.first_confirmations,
        )
    interval = timeframe_ms(timeframe)
    latest = _read_closed_bars(
        read, instrument, timeframe, start=start,
        end=last_closed_bar_start(end, interval), as_of=end,
    )
    first_cache = session.first_confirmations if session is not None else None
    times = []
    for bar in latest:
        cutoff, first, complete = _first_confirmation(read, instrument, timeframe, bar, interval)
        if first_cache is not None:
            first_cache[(timeframe, bar.ts_event)] = (cutoff, first, complete)
        times.append((cutoff, first, complete and latest.tail_completed))
    return sorted(times, key=lambda step: (step[0], step[1].ts_event))


def _resolve_cold_reader(
    read_bars: BarReader | None, data_dir: str | Path | None, *, start: int, end: int,
) -> BarReader | None:
    if read_bars is None:
        root = sdk._data_root(data_dir)
        if sdk.read_service_endpoint(root) is None:
            return cached_cold_bar_reader(ColdReader(root), start=start, end=end)
        return None
    if isinstance(read_bars, partial) and read_bars.func is sdk.bars and not read_bars.args:
        kw = read_bars.keywords
        if any(k not in ("data_dir", "offline", "_cold_reader") for k in kw):
            return None
        cold_reader = kw.get("_cold_reader")
        if cold_reader is not None:
            return cached_cold_bar_reader(cold_reader, start=start, end=end)
        root = sdk._data_root(kw.get("data_dir", data_dir))
        if kw.get("offline", False) or sdk.read_service_endpoint(root) is None:
            return cached_cold_bar_reader(ColdReader(root), start=start, end=end)
    return None


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
    `data_dir` 为默认行情读取器和工作台指标默认值所用的数据目录。
    """
    history_start = start if history_start is None else history_start
    if not 0 <= history_start <= start <= end:
        raise ValueError("须满足 0 <= history_start <= start <= end")
    cached_reader = _resolve_cold_reader(read_bars, data_dir, start=history_start, end=end)
    if cached_reader is not None:
        effective_read_bars = cached_reader
        session = getattr(cached_reader, "_talaria_replay_session")
    else:
        effective_read_bars = partial(sdk.bars, data_dir=data_dir) if read_bars is None else read_bars
        session = _ReplaySession()
    confirmations = confirmed_bars(effective_read_bars, instrument, timeframe, start=start, end=end)
    defaults = sdk.indicator_defaults(data_dir=data_dir) if confirmations else {}
    state = RuleState()
    tf_ms = timeframe_ms(timeframe)
    for cutoff, bar, tail_completed in confirmations:
        slot = bar.ts_event
        event = KlineEvent(
            instrument_id=instrument, timeframe=timeframe, confirmed=True, revision=bar.revision,
            bar=_compact_from_range(bar),
        )
        levels = levels_at(instrument, timeframe, slot + tf_ms) if levels_at else ()

        def drawing(drawing_id: str, as_of: int = cutoff) -> DrawingRecord | None:
            return drawing_at(drawing_id, as_of) if drawing_at else None

        ctx = Ctx(
            instrument, timeframe, history_start, cutoff,
            _read_bars=effective_read_bars, _read_derivatives=read_derivatives, _data_dir=data_dir, _bar_ts=slot,
            _discovery_tail_completed=tail_completed, _indicator_defaults=defaults,
            _replay_session=session,
            event=event, drawing=drawing, levels={level.level_id: level for level in levels}, rule_state=state,
        )
        bars = ctx.bars()
        if bars and bars[-1].ts_event == slot:
            output = evaluate(ctx)
            yield Evaluation(as_of=cutoff, output=output, tail_completed=ctx.tail_completed)
