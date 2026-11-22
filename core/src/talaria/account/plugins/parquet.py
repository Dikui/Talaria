"""用组件运行时的确认时点离线回放已发布的 Parquet 柱。"""

from collections.abc import Iterator
from functools import partial
from typing import Annotated, Self

from pydantic import Field, StrictInt, model_validator

from talaria import sdk
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.components.runtime import confirmed_bars
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack

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

    def events(self) -> Iterator[MarketEvent]:
        params = self._params
        read = partial(sdk.bars, data_dir=self._data_dir, offline=True)
        history_start = params.start if params.history_start is None else params.history_start
        for as_of, bar, tail_completed in confirmed_bars(
            read, params.instrument, params.timeframe, start=params.start, end=params.end,
        ):
            yield MarketEvent(
                as_of, params.instrument, params.timeframe, bar,
                source=MarketData(history_start, read, tail_completed=tail_completed),
            )


parquet_replay = SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), ParquetReplay, ReplayParams)
