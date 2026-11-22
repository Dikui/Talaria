"""用组件运行时的确认时点离线回放已发布的 Parquet 柱。"""

from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, replace
from fractions import Fraction
from functools import partial
from heapq import merge
from itertools import groupby
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from talaria import sdk
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.components.runtime import confirmed_bars
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.ticks import Quote, TickKind, Trade, trade_version
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader, ColdSnapshot

type Milliseconds = Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]


class ReplayParams(ContractModel):
    instrument: str
    timeframe: str
    start: Milliseconds
    end: Milliseconds
    history_start: Milliseconds | None = None

    @model_validator(mode="after")
    def _validate_range(self) -> Self:
        warmup = self.start if self.history_start is None else self.history_start
        if not warmup <= self.start <= self.end:
            raise ValueError("须满足 0 <= history_start <= start <= end")
        if self.timeframe not in SUPPORTED_TIMEFRAMES:
            raise ValueError(f"不支持的周期：{self.timeframe}")
        if get_market_pack(DEFAULT_PACK).instrument(self.instrument) is None:
            raise ValueError(f"未知标的：{self.instrument}")
        return self


def capture_replay_snapshot(
    data_dir: Path, windows: Iterable[ReplayParams], *, tick_instruments: Collection[str] = (),
) -> ColdSnapshot:
    instruments: dict[str, int] = {}
    for params in windows:
        instruments[params.instrument] = max(instruments.get(params.instrument, 0), params.end)
    pack = get_market_pack(DEFAULT_PACK)
    return ColdReader(data_dir).capture_snapshot(
        ((pack.instrument(instrument), end) for instrument, end in instruments.items()),
        tick_instruments=tick_instruments,
    )


@dataclass(frozen=True, slots=True)
class ReplayMarkAvailability(MarketEvent):
    """供审批和结算使用的已到达标记价，强平随成交时间线推进。"""


@dataclass(frozen=True, slots=True, kw_only=True)
class TickMarketEvent(MarketEvent):
    """同一可获取时刻的行情。空批次表示订单延迟届满。"""

    ticks: tuple[Trade | Quote, ...] = ()
    fallback_slots: frozenset[int] = frozenset()
    seed: bool = False


def tick_market_order(event: MarketEvent) -> tuple[int, int]:
    if isinstance(event, TickMarketEvent):
        priority = 0 if event.seed else 3
    elif event.derivative_points:
        priority = 1
    else:
        priority = 2 if event.source is None and event.ctx is None else 4
    return event.as_of, priority


class TickReplay:
    def __init__(self, reader: ColdReader, instrument: str, bars: Iterable[RangeBar]) -> None:
        self._reader = reader
        self._spec = get_market_pack(DEFAULT_PACK).instrument(instrument)
        self._minute = timeframe_ms("1m")
        self._bars = {
            bar.ts_event: bar for bar in bars
            if bar.confirmed and bar.volume > 0
            and 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        }
        self._coverage: dict[int, bool] = {}
        self._page = -1

    def covers(self, opening: int) -> bool:
        if opening not in self._bars:
            return False
        width = 5 * self._minute
        page = opening // width * width
        if self._page != page:
            end = min(page + width - 1, 2**63 - 1)
            rows = self._reader.read_tick_revisions(
                self._spec, TickKind.TRADES, start_ts=page, end_ts=end, as_of=end,
            )
            # 固定清单只为 1m 档位判定覆盖。成交时仍按可获取时间逐条选版。
            first = {}
            for row in rows:
                previous = first.get(row.trade_id)
                if previous is None or row.ts_available < previous.ts_available or (
                    row.ts_available == previous.ts_available and trade_version(row) > trade_version(previous)
                ):
                    first[row.trade_id] = row
            slots: dict[int, list[Trade]] = {}
            for row in first.values():
                slot = row.ts_event // self._minute * self._minute
                if row.ts_available < slot + self._minute:
                    slots.setdefault(slot, []).append(row)
            self._coverage = {}
            for slot, trades in slots.items():
                bar = self._bars.get(slot)
                if bar is not None:
                    self._coverage[slot] = (
                        min(row.price for row in trades) == bar.low
                        and max(row.price for row in trades) == bar.high
                        and sum(Fraction(row.size) for row in trades) == Fraction(bar.volume)
                    )
            self._page = page
        return self._coverage.get(opening, False)

    def events(self, start: int, end: int, *, seed_from: int | None = None) -> Iterator[TickMarketEvent]:
        rows = self._reader.tick_arrivals(self._spec, start if seed_from is None else seed_from, end)
        for at, group in groupby(rows, key=lambda row: row.ts_available):
            ticks = tuple(group)
            slots = {
                row.ts_event // self._minute * self._minute for row in ticks
                if isinstance(row, Trade) and at < (row.ts_event // self._minute + 1) * self._minute
            }
            yield TickMarketEvent(
                max(start, at), self._spec.instrument_id, "1m", None, ticks=ticks, seed=at < start,
                fallback_slots=frozenset(slot for slot in slots if slot in self._bars and not self.covers(slot)),
            )


class ParquetReplay:
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        if context.mode != "backtest":
            raise SchemeError("parquet_replay 仅支持回测")
        self._params = ReplayParams.model_validate(params)
        self._reader = context.cold_reader or ColdReader(context.data_dir)
        self._reader.check_snapshot_files()
        self._mark_age = max((config.mark_max_age_ms for config in context.account_risk.values()), default=900_000)

    def events(self, *, execution: bool = False, quote_max_age_ms: int | None = None) -> Iterator[MarketEvent]:
        params = self._params
        strategy = self._bars(params.timeframe, strategy=True)
        if not execution:
            yield from strategy
            return
        signals = list(strategy)
        bars = (
            [replace(event, source=None) for event in signals]
            if params.timeframe == "1m" else list(self._bars("1m", strategy=False))
        )
        if quote_max_age_ms is not None:
            replay = TickReplay(self._reader, params.instrument, (event.bar for event in bars))
            ticks = replay.events(
                params.start, params.end, seed_from=max(0, params.start - max(quote_max_age_ms, timeframe_ms("1m"))),
            )
            for event in merge(ticks, self._derivatives(), bars, signals, key=tick_market_order):
                if event.bar is not None and event.source is None and replay.covers(event.bar.ts_event):
                    continue
                yield event
            return
        marks: list[tuple[int, DerivativePoint]] = []
        settlements: list[DerivativePoint] = []
        estimates = []
        visible_marks = []
        for event in self._derivatives():
            projected = []
            observed = []
            for point in event.derivative_points:
                if point.kind == DerivativeKind.MARK_PRICE:
                    marks.append((event.as_of, point))
                    observed.append(point)
                elif point.is_funding_settlement:
                    settlements.append(point)
                else:
                    projected.append(point)
            if observed:
                visible_marks.append(ReplayMarkAvailability(
                    event.as_of, event.instrument_id, event.timeframe, None, derivative_points=tuple(observed),
                ))
            if projected:
                estimates.append(replace(event, derivative_points=tuple(projected)))
        execution, late_settlements = self._execution_events(bars, marks, settlements)
        yield from merge(visible_marks, execution, late_settlements, estimates, signals, key=lambda event: event.as_of)

    def end_event(self) -> MarketEvent:
        params = self._params
        return MarketEvent(params.end, params.instrument, params.timeframe, None)

    def _execution_events(
        self, bars: list[MarketEvent], marks: list[tuple[int, DerivativePoint]],
        settlements: list[DerivativePoint],
    ) -> tuple[list[MarketEvent], list[MarketEvent]]:
        """迟到确认会推迟后续撮合，策略信号仍按各自的可获取时间交付。"""
        marks.sort(key=lambda item: (item[1].observation_time, item[1].revision, item[0]))
        settlements.sort(key=lambda point: (point.ts_event, point.revision, point.ts_available))
        execution = []
        late = []
        mark_index = settlement_index = 0
        ready = self._params.start
        for event in sorted(bars, key=lambda item: item.bar.ts_event):
            opening = event.bar.ts_event
            close = opening + timeframe_ms("1m")
            while mark_index < len(marks) and marks[mark_index][1].observation_time <= opening:
                arrived, point = marks[mark_index]
                ready = max(ready, arrived)
                mark_index += 1
                execution.append(MarketEvent(ready, event.instrument_id, "1m", None, derivative_points=(point,)))
            ready = max(ready, event.as_of)
            observations = []
            while mark_index < len(marks) and marks[mark_index][1].observation_time <= close:
                arrived, point = marks[mark_index]
                ready = max(ready, arrived)
                observations.append(point)
                mark_index += 1
            while settlement_index < len(settlements) and settlements[settlement_index].ts_event <= close:
                point = settlements[settlement_index]
                settlement_index += 1
                if opening <= point.ts_event and point.ts_available <= ready:
                    observations.append(point)
                else:
                    late.append(point)
            execution.append(replace(event, as_of=ready, derivative_points=tuple(observations)))
        for arrived, point in marks[mark_index:]:
            ready = max(ready, arrived)
            execution.append(MarketEvent(ready, self._params.instrument, "1m", None, derivative_points=(point,)))
        late.extend(settlements[settlement_index:])
        arrivals = [
            MarketEvent(point.ts_available, self._params.instrument, "1m", None, derivative_points=(point,))
            for point in sorted(late, key=lambda point: (point.ts_available, point.ts_event, point.revision))
        ]
        return execution, arrivals

    def _bars(self, timeframe: str, *, strategy: bool) -> Iterator[MarketEvent]:
        params = self._params
        read = partial(sdk.bars, _cold_reader=self._reader)
        history_start = params.start if params.history_start is None else params.history_start
        for as_of, bar, tail_completed in confirmed_bars(
            read, params.instrument, timeframe, start=params.start, end=params.end,
        ):
            source = MarketData(
                history_start, read, tail_completed=tail_completed,
                read_derivative_revisions=self._derivative_revisions,
            ) if strategy else None
            yield MarketEvent(as_of, params.instrument, timeframe, bar, source=source)

    def _derivative_revisions(self, instrument: str, kind: DerivativeKind, end: int) -> list[DerivativePoint]:
        spec = get_market_pack(DEFAULT_PACK).instrument(instrument)
        return self._reader.read_derivative_revisions(spec, kind, start_ts=0, end_ts=end)

    def _derivatives(self) -> Iterator[MarketEvent]:
        params = self._params
        spec = get_market_pack(DEFAULT_PACK).instrument(params.instrument)
        # 逐版重放可获取时间，不能用区间末端的最高版本替代当时已知的值。
        marks = self._reader.read_derivative_revisions(
            spec, DerivativeKind.MARK_PRICE,
            start_ts=max(0, params.start - self._mark_age - timeframe_ms("1d")), end_ts=params.end,
        )
        rates = self._reader.read_derivative_revisions(
            spec, DerivativeKind.FUNDING_RATE, end_ts=params.end,
        )
        def warmup(rows):
            return max(
                (point for point in rows if point.visible_at(params.start) and point.ts_available < params.start),
                key=lambda point: (point.observation_time, point.revision, point.ts_available),
                default=None,
            )
        points = sorted(
            (point for point in (*marks, *rates) if params.start <= point.ts_available and point.visible_at(params.end)),
            key=lambda point: (point.ts_available, point.observation_time, point.revision),
        )
        seeds = (warmup(marks), warmup(point for point in rates if not point.is_funding_settlement))
        points[:0] = [seed for seed in seeds if seed is not None]
        for timestamp, group in groupby(points, key=lambda point: max(params.start, point.ts_available)):
            yield MarketEvent(timestamp, params.instrument, params.timeframe, None, derivative_points=tuple(group))


parquet_replay = SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), ParquetReplay, ReplayParams)
