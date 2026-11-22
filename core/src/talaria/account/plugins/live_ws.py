"""模拟盘实时数据入口，复用策略订阅的确认与断线恢复。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial

from pydantic import Field, field_validator

from talaria import sdk
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.strategy_subscription import StrategyBarEvent
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack


class LiveParams(ContractModel):
    instrument: str
    timeframe: str = "1m"
    history_bars: int = Field(default=500, ge=1, le=8192)
    reduce_on_disconnect: bool = True
    subscription_timeout_ms: int = Field(default=30_000, ge=1000)
    derivatives: bool = True

    @field_validator("timeframe")
    @classmethod
    def _period(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("未知行情周期")
        return value


@dataclass(frozen=True, slots=True)
class PaperPulse(MarketEvent):
    ready: bool = False
    reduce_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PaperMark(MarketEvent):
    """先更新可见估值，等对应 1m 柱到达时再按发生时刻推进成交。"""


class LiveWS:
    def __init__(
        self, context: PluginContext, params: PluginParams, *,
        subscribe=sdk.subscribe, subscribe_derivatives=sdk.subscribe_derivatives,
        now_ms=lambda: time.time_ns() // 1_000_000,
    ) -> None:
        if context.mode != "paper":
            raise SchemeError("live_ws 仅支持模拟盘")
        self.params = LiveParams.model_validate(params)
        self._data_dir = context.data_dir
        self._now = now_ms
        self._subscribe = subscribe
        self._subscribe_derivatives = subscribe_derivatives
        pack = get_market_pack(DEFAULT_PACK)
        if pack.instrument(self.params.instrument) is None:
            raise SchemeError(f"未知标的：{self.params.instrument}")

    async def _events(self):
        params = self.params
        instrument = params.instrument
        # 1m 同时作为成交输入和订阅活性探针，慢周期只负责触发策略。
        channels = dict.fromkeys(("1m", params.timeframe), self._subscribe)
        trait = get_market_pack(DEFAULT_PACK).traits(instrument).derivatives
        if params.derivatives and trait is not None and trait.realtime:
            channels.update({
                kind: self._subscribe_derivatives
                for kind in (DerivativeKind.MARK_PRICE, DerivativeKind.FUNDING_RATE)
                if kind in trait.kinds
            })

        queue = asyncio.Queue(maxsize=256)
        connected = set()
        ever_connected = set()
        longest_bar_delay = 120_000
        startup = time.monotonic()
        next_pulse = startup + 1
        first_ready = None
        last_slot = {}
        pending: list[DerivativePoint] = []
        loss = None
        read_bars = partial(sdk.bars, data_dir=self._data_dir)
        read_derivatives = partial(sdk.derivatives, data_dir=self._data_dir)

        async def pump(channel, subscribe):
            nonlocal loss
            def connection(ready: bool) -> None:
                nonlocal loss
                if ready:
                    connected.add(channel)
                    ever_connected.add(channel)
                else:
                    connected.discard(channel)
                    if channel in ever_connected:
                        loss = "SUBSCRIPTION_LOST"

            try:
                async with aclosing(subscribe(
                    instrument, channel, data_dir=self._data_dir, on_connection=connection,
                )) as messages:
                    async for message in messages:
                        await queue.put(message)
            except sdk.SubscriptionRejectedError as exc:
                loss = exc.code
                connected.discard(channel)
                await queue.put(None)
            else:
                connection(False)
                await queue.put(None)

        tasks = [asyncio.create_task(pump(channel, subscribe)) for channel, subscribe in channels.items()]
        now = self._now()
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=max(.001, next_pulse - time.monotonic()))
                except TimeoutError:
                    event = None
                for task in tasks:
                    if task.done():
                        task.result()
                now = max(now, self._now())
                ready = connected == channels.keys()
                tick = time.monotonic()
                if not ready and (tick - startup) * 1000 >= params.subscription_timeout_ms:
                    loss = loss or "SUBSCRIPTION_UNAVAILABLE"
                minute = last_slot.get("1m")
                if (first_ready is not None and minute is None and now - first_ready > longest_bar_delay) or (
                    minute is not None and now - (minute + 60_000) > longest_bar_delay
                ):
                    loss = loss or "CONFIRMED_MINUTE_STALE"
                    ready = False
                if ready and first_ready is None:
                    first_ready = now
                reduce = loss if params.reduce_on_disconnect else None
                loss = None
                if event is None or tick >= next_pulse or reduce is not None:
                    next_pulse = tick + 1
                    yield PaperPulse(
                        now, instrument, params.timeframe, None,
                        ready=ready and minute is not None and minute + 60_000 >= now // 60_000 * 60_000,
                        reduce_reason=reduce,
                    )
                if isinstance(event, StrategyBarEvent):
                    bar = event.bar
                    previous = last_slot.get(event.timeframe)
                    if previous is not None and bar.ts_event <= previous:
                        continue
                    if previous is not None and event.timeframe == "1m" and bar.ts_event != previous + 60_000:
                        if params.reduce_on_disconnect:
                            yield PaperPulse(now, instrument, params.timeframe, None, reduce_reason="CONFIRMED_MINUTE_GAP")
                    last_slot[event.timeframe] = bar.ts_event
                    now = max(now, bar.ts_available, bar.ts_event + timeframe_ms(event.timeframe))
                    source = None
                    if (
                        ready and event.timeframe == params.timeframe
                        and now - bar.ts_event - timeframe_ms(event.timeframe) < longest_bar_delay
                    ):
                        history_start = max(0, bar.ts_event - (params.history_bars - 1) * timeframe_ms(event.timeframe))
                        source = MarketData(history_start, read_bars, read_derivatives)
                    points = ()
                    if event.timeframe == "1m":
                        close = bar.ts_event + 60_000
                        points = tuple(point for point in pending if point.observation_time <= close)
                        pending = [point for point in pending if point.observation_time > close]
                    if source is not None or event.timeframe == "1m":
                        yield MarketEvent(now, instrument, event.timeframe, bar, source=source, derivative_points=points)
                elif event is not None:
                    point = event.point
                    now = max(now, point.ts_available, point.observation_time)
                    if minute is not None and point.observation_time <= minute + 60_000:
                        # 迟到的结算和更正仍进入成交与记账。
                        yield MarketEvent(now, instrument, "1m", None, derivative_points=(point,))
                    else:
                        if len(pending) >= 4096:
                            if params.reduce_on_disconnect:
                                yield PaperPulse(now, instrument, params.timeframe, None,
                                                 reduce_reason="DERIVATIVE_BACKLOG")
                            # 收盘途径不可用时仍交付定价与结算，避免无界等待。
                            yield MarketEvent(now, instrument, "1m", None, derivative_points=tuple(pending))
                            pending.clear()
                        pending.append(point)
                        if point.kind is DerivativeKind.MARK_PRICE:
                            yield PaperMark(now, instrument, "1m", None, derivative_points=(point,))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def events(self) -> Iterator[MarketEvent]:
        with asyncio.Runner() as runner:
            messages = self._events()
            try:
                while True:
                    yield runner.run(anext(messages))
            finally:
                runner.run(messages.aclose())


live_ws = SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), LiveWS, LiveParams)
