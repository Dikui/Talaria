"""用组件运行时的确认时点离线回放已发布的 Parquet 柱。"""

from collections.abc import Iterator
from functools import partial
from heapq import merge
from itertools import groupby
from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from talaria import sdk
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.components.runtime import confirmed_bars
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader

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


class ParquetReplay:
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        if context.mode != "backtest":
            raise SchemeError("parquet_replay 仅支持回测")
        self._params = ReplayParams.model_validate(params)
        self._data_dir = context.data_dir
        self._mark_age = max((config.mark_max_age_ms for config in context.account_risk.values()), default=900_000)

    def events(self, *, execution: bool = False) -> Iterator[MarketEvent]:
        params = self._params
        strategy = self._bars(params.timeframe, strategy=True)
        if not execution:
            yield from strategy
            return
        execution_bars = self._bars("1m", strategy=False) if params.timeframe != "1m" else ()
        yield from merge(self._derivatives(), execution_bars, strategy, key=lambda event: event.as_of)

    def _bars(self, timeframe: str, *, strategy: bool) -> Iterator[MarketEvent]:
        params = self._params
        read = partial(sdk.bars, data_dir=self._data_dir, offline=True)
        history_start = params.start if params.history_start is None else params.history_start
        for as_of, bar, tail_completed in confirmed_bars(
            read, params.instrument, timeframe, start=params.start, end=params.end,
        ):
            source = MarketData(history_start, read, tail_completed=tail_completed) if strategy else None
            yield MarketEvent(as_of, params.instrument, timeframe, bar, source=source)

    def _derivatives(self) -> Iterator[MarketEvent]:
        params = self._params
        spec = get_market_pack(DEFAULT_PACK).instrument(params.instrument)
        reader = ColdReader(self._data_dir)
        # 逐版重放可获取时间，不能用区间末端的最高版本替代当时已知的值。
        marks = reader.read_derivative_revisions(
            spec, DerivativeKind.MARK_PRICE,
            start_ts=max(0, params.start - self._mark_age - timeframe_ms("1d")), end_ts=params.end,
        )
        rates = reader.read_derivative_revisions(
            spec, DerivativeKind.FUNDING_RATE, start_ts=params.start, end_ts=params.end,
        )
        seed = max(
            (point for point in marks if point.visible_at(params.start) and point.ts_available < params.start),
            key=lambda point: (point.observation_time, point.revision, point.ts_available),
            default=None,
        )
        points = sorted(
            (point for point in (*marks, *rates) if params.start <= point.ts_available and point.visible_at(params.end)),
            key=lambda point: (point.ts_available, point.observation_time, point.revision),
        )
        if seed is not None:
            points.insert(0, seed)
        for timestamp, group in groupby(points, key=lambda point: max(params.start, point.ts_available)):
            yield MarketEvent(timestamp, params.instrument, params.timeframe, None, derivative_points=tuple(group))


parquet_replay = SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), ParquetReplay, ReplayParams)
