"""模拟盘实时数据入口，复用策略订阅的确认与断线恢复。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import aclosing
from dataclasses import dataclass
from functools import partial

import httpx
from pydantic import Field, field_validator

from talaria import sdk
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.strategy_subscription import StrategyBarEvent, SubscriptionCursor, SubscriptionRequest
from talaria.sdk._subscription import _range_revisions
from talaria.service_endpoint import data_dir_id, read_service_connection
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel


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


class _FeedCheckpoint(ContractModel):
    instrument: str
    timeframe: str
    first_minute: int | None = None
    last_slot: dict[str, int] = Field(default_factory=dict)
    buffered: dict[str, dict[int, RangeBar]] = Field(default_factory=dict)
    pending: list[DerivativePoint] = Field(default_factory=list)
    cursors: dict[str, SubscriptionCursor] = Field(default_factory=dict)
    starts: dict[str, int] = Field(default_factory=dict)


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
        self._state = _FeedCheckpoint(instrument=self.params.instrument, timeframe=self.params.timeframe)
        self._recovering = False
        self._subscribe = subscribe
        self._subscribe_derivatives = subscribe_derivatives
        self._pack = context.resolved_market_pack()
        if self._pack.instrument(self.params.instrument) is None:
            raise SchemeError(f"未知标的：{self.params.instrument}")

    def checkpoint(self) -> dict:
        return self._state.model_dump(mode="json")

    def restore_checkpoint(self, state: dict) -> None:
        restored = _FeedCheckpoint.model_validate(state)
        if (restored.instrument, restored.timeframe) != (self.params.instrument, self.params.timeframe):
            raise SchemeError("实时行情检查点与交易方案不符")
        self._state = restored
        self._recovering = bool(restored.last_slot or restored.cursors or restored.starts)

    def resume_history(self, start: int) -> None:
        first = start // 60_000 * 60_000
        self._state.first_minute = first
        for channel in ("1m", self.params.timeframe, DerivativeKind.MARK_PRICE, DerivativeKind.FUNDING_RATE):
            self._state.starts[channel] = first
        self._recovering = True

    async def _events(self):
        params = self.params
        instrument = params.instrument
        # 1m 同时作为成交输入和订阅活性探针，慢周期只负责触发策略。
        channels = dict.fromkeys(("1m", params.timeframe), self._subscribe)
        trait = self._pack.traits(instrument).derivatives
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
        last_minute_progress = startup
        next_pulse = startup + 1
        first_ready = None
        state = self._state
        last_slot = state.last_slot
        buffered = state.buffered
        for period in ("1m", params.timeframe):
            buffered.setdefault(period, {})
        sent = dict.fromkeys(channels, 0)
        consumed = sent.copy()
        ready_after = {}
        baselines = {}
        gap_task = None
        pending = state.pending
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
                    ready_after[channel] = sent[channel]
                else:
                    connected.discard(channel)
                    if channel in ever_connected:
                        loss = "SUBSCRIPTION_LOST"
                        self._recovering = True

            delay = 0.1
            after = state.cursors.get(channel)
            coverage = state.starts.get(channel, last_slot.get("1m", state.first_minute) if self._recovering else None)
            if channel in last_slot:
                coverage = last_slot[channel] + timeframe_ms(channel)

            def baseline(cursor, start):
                nonlocal after, coverage
                after, coverage = cursor, start
                baselines[channel] = (sent[channel], cursor, start)
                # 服务握手界定第一根应当处理的分钟柱。
                if channel == "1m" and state.first_minute is None:
                    state.first_minute = start
            while True:
                try:
                    async with aclosing(subscribe(
                        instrument, channel, data_dir=self._data_dir, on_connection=connection,
                        after=after, resume_from=coverage, on_cursor=baseline,
                    )) as messages:
                        async for message in messages:
                            await queue.put((channel, message))
                            sent[channel] += 1
                            if message.seq is not None:
                                after = SubscriptionCursor(epoch=message.epoch, seq=message.seq)
                            if coverage is None:
                                coverage = (message.bar.ts_event if isinstance(message, StrategyBarEvent)
                                            else message.point.observation_time)
                            delay = 0.1
                except sdk.SubscriptionRejectedError as exc:
                    if exc.code not in {"SUBSCRIPTION_QUOTA", "QUOTA_EXCEEDED", "FEED_UNAVAILABLE", "SUBSCRIPTION_UNAVAILABLE"}:
                        raise
                    connection(False)
                    loss = exc.code
                    if coverage is None:
                        coverage = self._now() // 60_000 * 60_000
                        state.starts[channel] = coverage
                        if channel == "1m":
                            state.first_minute = coverage
                else:
                    connection(False)
                await queue.put((None, None))
                await asyncio.sleep(delay)
                delay = min(5, delay * 2)

        async def fill_gap(start, end):
            service = read_service_connection(self._data_dir)
            if service is None:
                return
            request = SubscriptionRequest(instrument=instrument, timeframe="1m", owner="paper-gap")
            try:
                rows = await _range_revisions(
                    service.base_url, data_dir_id(self._data_dir), request, coverage_start=start,
                    range_start=start, now=end + 60_000, interval=60_000, seen=set(),
                )
            except (httpx.HTTPError, TimeoutError):
                return
            for identity, bar in rows:
                await queue.put((None, StrategyBarEvent(instrument_id=instrument, timeframe="1m", epoch="range",
                                                       seq=None, content_id=identity, bar=bar)))

        def signal_source(period, bar):
            interval = timeframe_ms(period)
            cutoff = max(bar.ts_event + interval, bar.ts_available)
            def pinned(symbol, timeframe, *, start, end, as_of):
                rows = read_bars(symbol, timeframe, start=start, end=end, as_of=as_of)
                if (symbol, timeframe) != (instrument, period) or not start <= bar.ts_event <= end:
                    return rows
                return sdk.BarsResult(tuple(sorted(
                    [row for row in rows if row.ts_event != bar.ts_event] + [bar], key=lambda row: row.ts_event,
                )), getattr(rows, "tail_completed", True))
            return MarketData(max(0, bar.ts_event - (params.history_bars - 1) * interval),
                              pinned, read_derivatives, signal_as_of=cutoff)

        tasks = [asyncio.create_task(pump(channel, subscribe)) for channel, subscribe in channels.items()]
        now = self._now()
        try:
            while True:
                try:
                    source_channel, event = await asyncio.wait_for(queue.get(), timeout=max(.001, next_pulse - time.monotonic()))
                except TimeoutError:
                    source_channel, event = None, None
                for task in tasks:
                    if task.done():
                        task.result()
                now = max(now, self._now())
                ready = connected == channels.keys()
                tick = time.monotonic()
                if not ready and (tick - startup) * 1000 >= params.subscription_timeout_ms:
                    loss = loss or "SUBSCRIPTION_UNAVAILABLE"
                for channel, (count, cursor, start) in tuple(baselines.items()):
                    if consumed[channel] >= count:
                        state.cursors[channel] = cursor
                        state.starts.setdefault(channel, start)
                        del baselines[channel]
                minute = last_slot.get("1m")
                if (first_ready is not None and minute is None and now - first_ready > longest_bar_delay) or (
                    (not self._recovering or (tick - last_minute_progress) * 1000 > longest_bar_delay)
                    and minute is not None and now - (minute + 60_000) > longest_bar_delay
                ):
                    loss = loss or "CONFIRMED_MINUTE_STALE"
                    ready = False
                if ready and first_ready is None:
                    first_ready = now
                reduce = loss if params.reduce_on_disconnect else None
                loss = None
                pulse_due = event is None or tick >= next_pulse
                if reduce is not None:
                    next_pulse = tick + 1
                    yield PaperPulse(now, instrument, params.timeframe, None, reduce_reason=reduce)
                if source_channel is not None:
                    consumed[source_channel] += 1
                    if event.seq is not None:
                        state.cursors[source_channel] = SubscriptionCursor(epoch=event.epoch, seq=event.seq)
                        start = event.bar.ts_event if isinstance(event, StrategyBarEvent) else event.point.observation_time
                        state.starts.setdefault(source_channel, start)
                caught_up = connected == channels.keys() and all(
                    consumed[channel] >= ready_after[channel] for channel in connected
                )
                if isinstance(event, StrategyBarEvent):
                    if not event.bar.confirmed:
                        continue
                    previous = last_slot.get(event.timeframe)
                    if event.timeframe == "1m" and previous is None and state.first_minute is not None:
                        previous = state.first_minute - 60_000
                    if previous is None or event.bar.ts_event > previous:
                        buffered[event.timeframe].setdefault(event.bar.ts_event, event.bar)
                if isinstance(event, StrategyBarEvent) or event is None:
                    if self._recovering and not caught_up:
                        if pulse_due and reduce is None:
                            next_pulse = tick + 1
                            yield PaperPulse(now, instrument, params.timeframe, None)
                        continue
                    expected = last_slot["1m"] + 60_000 if "1m" in last_slot else state.first_minute
                    latest_closed = now // 60_000 * 60_000 - 60_000
                    if (event is None and caught_up and expected is not None and expected <= latest_closed
                            and expected not in buffered["1m"] and (gap_task is None or gap_task.done())):
                        if gap_task is not None:
                            gap_task.result()
                        gap_task = asyncio.create_task(fill_gap(expected, latest_closed))
                    for period, waiting in buffered.items():
                        interval = timeframe_ms(period)
                        while waiting:
                            slot = min(waiting)
                            previous = last_slot.get(period)
                            if period == "1m" and previous is None and state.first_minute is not None:
                                previous = state.first_minute - interval
                            if period == "1m" and previous is not None and slot > previous + interval:
                                if gap_task is None or gap_task.done():
                                    if gap_task is not None:
                                        gap_task.result()
                                    gap_task = asyncio.create_task(fill_gap(previous + interval, slot - interval))
                                if params.reduce_on_disconnect and not self._recovering:
                                    yield PaperPulse(now, instrument, period, None, reduce_reason="CONFIRMED_MINUTE_GAP")
                                break
                            if period != "1m" and slot + interval > last_slot.get("1m", -60_000) + 60_000:
                                break
                            bar = waiting.pop(slot)
                            last_slot[period] = slot
                            now = max(now, bar.ts_available, slot + interval)
                            source = None
                            if (period == params.timeframe and caught_up and now - slot - interval < longest_bar_delay
                                    and (not self._recovering or slot + interval >= now // 60_000 * 60_000)):
                                source = signal_source(period, bar)
                            points = ()
                            if period == "1m":
                                last_minute_progress = tick
                                points = tuple(point for point in pending if point.observation_time <= slot + interval)
                                pending[:] = [point for point in pending if point.observation_time > slot + interval]
                            if source is not None or period == "1m":
                                yield MarketEvent(now, instrument, period, bar, source=source, derivative_points=points)
                    if caught_up and last_slot.get("1m", -60_000) + 60_000 >= now // 60_000 * 60_000:
                        self._recovering = False
                else:
                    point = event.point
                    now = max(now, point.ts_available, point.observation_time)
                    if point.kind == DerivativeKind.FUNDING_RATE and not point.is_funding_settlement:
                        yield PaperMark(now, instrument, "1m", None, derivative_points=(point,))
                    elif minute is not None and point.observation_time <= minute + 60_000:
                        # 迟到的结算和更正仍进入成交与记账。
                        yield MarketEvent(now, instrument, "1m", None, derivative_points=(point,))
                    else:
                        pending.append(point)
                        if len(pending) == 4096:
                            if params.reduce_on_disconnect:
                                yield PaperPulse(now, instrument, params.timeframe, None,
                                                 reduce_reason="DERIVATIVE_BACKLOG")
                        if point.kind is DerivativeKind.MARK_PRICE:
                            yield PaperMark(now, instrument, "1m", None, derivative_points=(point,))
                if pulse_due and reduce is None:
                    minute = last_slot.get("1m")
                    next_pulse = tick + 1
                    yield PaperPulse(
                        now, instrument, params.timeframe, None,
                        ready=caught_up and not self._recovering and not any(buffered.values())
                        and minute is not None and minute + 60_000 >= now // 60_000 * 60_000,
                    )
        finally:
            if gap_task is not None:
                tasks.append(gap_task)
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
