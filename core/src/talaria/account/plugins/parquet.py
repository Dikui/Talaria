"""用组件运行时的确认时点离线回放已发布的 Parquet 柱。"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
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
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
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


def capture_replay_snapshot(data_dir: Path, windows: Iterable[ReplayParams]) -> ColdSnapshot:
    instruments: dict[str, int] = {}
    for params in windows:
        instruments[params.instrument] = max(instruments.get(params.instrument, 0), params.end)
    pack = get_market_pack(DEFAULT_PACK)
    return ColdReader(data_dir).capture_snapshot(
        (pack.instrument(instrument), end) for instrument, end in instruments.items()
    )


@dataclass(frozen=True, slots=True)
class ReplayMarkAvailability(MarketEvent):
    """供审批和结算使用的已到达标记价，强平随成交时间线推进。"""


class ParquetReplay:
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        if context.mode != "backtest":
            raise SchemeError("parquet_replay 仅支持回测")
        self._params = ReplayParams.model_validate(params)
        self._reader = context.cold_reader or ColdReader(context.data_dir)
        self._reader.check_snapshot_files()
        self._mark_age = max((config.mark_max_age_ms for config in context.account_risk.values()), default=900_000)

    def events(self, *, execution: bool = False) -> Iterator[MarketEvent]:
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
