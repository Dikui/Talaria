"""在回测中按可获取时间用逐笔和买一卖一驱动共用的仓位执行器。"""

from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from heapq import merge
from itertools import chain, groupby, pairwise
from math import floor
from uuid import UUID

from pydantic import Field, StrictInt

from talaria.account.contracts import FillTier, MarginValuation, MarketEvent, RoundPhase
from talaria.account.plugins.bar_sim_1m import (
    BalanceReader, BarSim1m, BarSimParams, PositionExecutor, _BarLiquidity, _Entry,
)
from talaria.account.plugins.parquet import TickMarketEvent, TickReplay, tick_market_order
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, SchemeError, Slot, SlotPlugin
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.ticks import Quote, Trade, relative_tick_order_known, tick_order, trade_version
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import decimal_from_finite_fraction as decimal_exact
from talaria.perception.cold_store import ColdReader

_MINUTE = timeframe_ms("1m")


class L1TradeSimParams(BarSimParams):
    quote_max_age_ms: StrictInt = Field(default=3_000, ge=0)


@dataclass(slots=True)
class _Book:
    quote: Quote
    depth: dict[int, Fraction]


def _quote_updates(rows: Iterable[Quote]) -> Iterator[Quote]:
    """连接 UUID 只是身份。摄取时间能区分代际时沿用该顺位。"""
    connections: dict[UUID, list[Quote]] = {}
    unknown: dict[int, list[Quote]] = {}
    for row in rows:
        if row.connection_id is None or row.update_id is None:
            unknown.setdefault(row.ts_ingested, []).append(row)
        else:
            connections.setdefault(row.connection_id, []).append(row)
    streams = [
        deque((row,) for row in sorted(updates, key=lambda row: row.update_id))
        for updates in connections.values()
    ]
    if unknown:
        streams.append(deque(tuple(unknown[ingest]) for ingest in sorted(unknown)))
    while streams:
        earliest = min(stream[0][0].ts_ingested for stream in streams)
        tied = [row for stream in streams if stream[0][0].ts_ingested == earliest for row in stream.popleft()]
        streams = [stream for stream in streams if stream]
        if len(tied) == 1:
            yield tied[0]
            continue
        # 两侧独立取最不利价和最少可见量，避免用 UUID 或文件类目充当顺位。
        basis = max(tied, key=_quote_version)
        yield replace(
            basis, connection_id=None, update_id=None,
            bid_price=min(row.bid_price for row in tied), ask_price=max(row.ask_price for row in tied),
            bid_size=min(row.bid_size for row in tied), ask_size=min(row.ask_size for row in tied),
        )


def _quote_version(row: Quote) -> tuple:
    return (
        row.revision, row.ts_ingested, row.ts_committed,
        row.ask_price, -row.bid_price, -row.ask_size, -row.bid_size, row.ts_event,
    )


class L1TradeSim(BarSim1m):
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        if context.mode != "backtest":
            raise SchemeError("l1_trade_sim 仅支持回测，模拟盘请使用 bar_sim_1m")
        settings = L1TradeSimParams.model_validate(params)
        super().__init__(context, settings)
        self.quote_max_age_ms = settings.quote_max_age_ms
        self._reader = context.cold_reader or ColdReader(context.data_dir)
        self._books: dict[str, _Book] = {}
        self._closed_connections: dict[str, set[UUID]] = {}
        self._seen_trades: dict[str, set[bytes]] = {}
        self._last_trades: dict[str, list[Trade]] = {}
        self._residuals: dict[tuple[str, str], tuple[Fraction, Fraction]] = {}
        self._tick_tier: FillTier | None = None
        self._trade_budget: Fraction | None = None
        self._uncertain_prices: tuple[Decimal, Decimal] | None = None
        self._timer_through = -1

    def _book(self, instrument: str) -> _Book | None:
        book = self._books.get(instrument)
        if book is not None and book.quote.ts_available <= self._event_time <= book.quote.ts_event + self.quote_max_age_ms:
            return book
        return None

    def _book_price(self, instrument: str, side: int) -> Decimal | None:
        if self._tick_tier is None:
            return None
        book = self._book(instrument)
        if book is None:
            return None
        quote = book.quote
        if side == 1:
            return quote.ask_price if quote.ask_size else None
        return quote.bid_price if quote.bid_size else None

    def _fill_tier(self, executor: PositionExecutor, side: int, *, maker: bool) -> FillTier:
        if not maker and self._book_price(executor.instrument.instrument_id, side) is not None:
            return "l1"
        return self._tick_tier or "bar_1m"

    def _liquidation_price(self, executor: PositionExecutor, mark: Decimal) -> tuple[Decimal, FillTier]:
        side = -executor.side
        prices = [
            trade.price for trade in self._last_trades.get(executor.instrument.instrument_id, ())
            if trade.ts_available <= self._event_time and self._event_time - trade.ts_event < _MINUTE
        ]
        if prices:
            reference = max(prices) if side == 1 else min(prices)
            return self._price_with_friction(executor, reference, side), "trade"
        price, _ = super()._liquidation_price(executor, mark)
        return price, "mark"

    def _liquidate(
        self, executor: PositionExecutor, qty: Fraction, mark: Decimal,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        reason = "MARGIN_LIQUIDATION"
        side = -executor.side
        book = self._book(executor.instrument.instrument_id)
        remaining = qty
        price, tier = self._liquidation_price(executor, mark)
        if book is not None:
            step = Fraction(executor.instrument.qty_step)
            visible = floor(min(qty, book.depth[side]) / step) * step
            quote = book.quote.ask_price if side == 1 else book.quote.bid_price
            if visible:
                self._begin_exit(executor, reason)
                quoted = self._price_with_friction(executor, quote, side, quoted=True)
                book.depth[side] -= visible
                self._exit(executor, visible, mark, reason, liquidated=True, liquidation_price=(quoted, "l1"))
                yield ()
                remaining -= visible
            if remaining and (book.quote.ask_size if side == 1 else book.quote.bid_size):
                quoted = self._price_with_friction(executor, quote, side, quoted=True)
                tick = Fraction(executor.instrument.tick_size)
                beyond = decimal_exact(max(tick, Fraction(quoted) + side * tick))
                price = max(price, beyond) if side == 1 else min(price, beyond)
        if remaining:
            self._exit(executor, remaining, mark, reason, liquidated=True, liquidation_price=(price, tier))
            yield ()

    def _observe_quote(self, row: Quote) -> bool:
        instrument = row.instrument_id
        book = self._books.get(instrument)
        if book is not None:
            previous = book.quote
            if row.ts_event < previous.ts_event:
                return False
            if row.connection_id is not None:
                closed = self._closed_connections.setdefault(instrument, set())
                if row.connection_id in closed:
                    return False
                if row.connection_id == previous.connection_id and row.update_id is not None and previous.update_id is not None:
                    if (row.update_id, row.revision) <= (previous.update_id, previous.revision):
                        return False
                    if row.update_id == previous.update_id:
                        # 修订更改的是同一次报价，已经使用的深度仍须扣除。
                        consumed = {
                            1: Fraction(previous.ask_size) - book.depth[1],
                            -1: Fraction(previous.bid_size) - book.depth[-1],
                        }
                        book.quote = row
                        book.depth = {
                            1: max(Fraction(0), Fraction(row.ask_size) - consumed[1]),
                            -1: max(Fraction(0), Fraction(row.bid_size) - consumed[-1]),
                        }
                        return True
                elif previous.connection_id is not None:
                    closed.add(previous.connection_id)
            elif row.ts_event == previous.ts_event and (
                row.ts_available, row.ts_ingested, row.revision, row.ts_committed
            ) < (
                previous.ts_available, previous.ts_ingested, previous.revision, previous.ts_committed
            ):
                return False
            elif row == previous:
                return False
        self._books[instrument] = _Book(row, {1: Fraction(row.ask_size), -1: Fraction(row.bid_size)})
        return True

    def _timers_due(self, after: int, through: int) -> list[tuple[int, str]]:
        due = set()
        for executor in self._live_rounds.values():
            instrument = executor.instrument.instrument_id
            due.update(
                (entry.ready_at, instrument) for entry in executor.entries.values()
                if after < entry.ready_at <= through
            )
            if executor.quantity and executor.opened_at is not None and executor.phase != RoundPhase.EXITING:
                deadline = executor.opened_at + executor.barriers.max_holding_ms
                if after < deadline <= through:
                    due.add((deadline, instrument))
        return sorted(due)

    def clock_events(self, markets: Iterable[MarketEvent], *, end: MarketEvent | None = None) -> Iterator[MarketEvent]:
        """延迟届满时使用当时的盘口，成交在下一次策略求值前交付。"""
        if end is not None:
            markets = chain(markets, (TickMarketEvent(end.as_of, end.instrument_id, "1m", None, seed=True),))
        for at, group in groupby(markets, key=lambda event: event.as_of):
            due = self._timers_due(self._timer_through, at)
            while due and due[0][0] < at:
                timestamp = due[0][0]
                _, simultaneous = next(groupby(due, key=lambda timer: timer[0]))
                for _, instrument in simultaneous:
                    yield TickMarketEvent(timestamp, instrument, "1m", None)
                due = self._timers_due(timestamp, at)
            timers = [TickMarketEvent(at, instrument, "1m", None) for _, instrument in due]
            for event in group:
                if timers and (event.source is not None or event.ctx is not None):
                    yield from timers
                    timers.clear()
                yield event
            yield from timers
            self._timer_through = at

    def market_steps(
        self, event: MarketEvent, valuation: MarginValuation | None, *, balances: BalanceReader | None = None,
        execution_through: int | None = None, pricing_only: bool = False, tick_stream: bool = False,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        if tick_stream or isinstance(event, TickMarketEvent):
            yield from super().market_steps(
                event, valuation, balances=balances, execution_through=execution_through, pricing_only=pricing_only,
            )
            return
        # 直接使用成交插槽时，按调用提供的时钟边界读同一条逐笔时间线。
        bar = event.bar if event.timeframe == "1m" else None
        if bar is not None and max(bar.ts_event + _MINUTE, bar.ts_available) > event.as_of:
            bar = None
        opening = bar.ts_event if bar is not None else min(
            (entry.ready_at for executor in self._live_rounds.values() for entry in executor.entries.values()),
            default=event.as_of,
        )
        start = self._now if self._now else min(opening, event.as_of)
        replay = TickReplay(self._reader, event.instrument_id, () if bar is None else (bar,))
        seed_from = None if self._now else max(0, start - max(_MINUTE, self.quote_max_age_ms))
        ticks = replay.events(start, event.as_of, seed_from=seed_from)
        if bar is not None and replay.covers(bar.ts_event):
            event = replace(event, bar=None)
        markets = merge((event,), ticks, key=tick_market_order)
        for market in self.clock_events(markets):
            yield from super().market_steps(market, valuation, balances=balances)

    def _observation_step(self, event: MarketEvent, point: DerivativePoint) -> int:
        return event.as_of

    def _matching_timeline(self, event: MarketEvent, bar: RangeBar | None) -> Iterator[tuple[int, str | TickMarketEvent]]:
        if isinstance(event, TickMarketEvent):
            yield event.as_of, event
        else:
            yield from super()._matching_timeline(event, bar)

    @staticmethod
    def _entry_limit(executor: PositionExecutor, entry: _Entry) -> Decimal:
        candidate = entry.plan.candidate
        return candidate.entry_high if executor.side == 1 else candidate.entry_low

    def _match_entry(
        self, executor: PositionExecutor, entry: _Entry, bar: RangeBar, *, at_open: bool,
    ) -> tuple[Decimal, str] | None:
        match = super()._match_entry(executor, entry, bar, at_open=at_open)
        return (self._entry_limit(executor, entry), "MAKER") if match is not None and match[1] == "MAKER" else match

    def _bar_actions(
        self, bar: RangeBar, instrument: str, liquidity: _BarLiquidity,
        stage: str, open_positions: set[str], protected: set[str],
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        if stage == "close":
            # 柱内实际开户可能在合成的不利阶段之后。柱确认时补上仍在仓位上的止损保护。
            for executor in tuple(self._live_rounds.values()):
                if (
                    executor.instrument.instrument_id != instrument or executor.round_id in protected
                    or executor.opened_at is None or not bar.ts_event + 1 < executor.opened_at < self._event_time
                ):
                    continue
                stop, trailing = executor.stop_at(self._event_time)
                adverse = bar.low if executor.side == 1 else bar.high
                if executor.side * (adverse - stop) <= 0:
                    reason = "TRAILING_STOP" if trailing else "STOP_LOSS"
                    if self._protect(executor, liquidity, stop, reason):
                        yield ()
                    protected.add(executor.round_id)
        yield from super()._bar_actions(bar, instrument, liquidity, stage, open_positions, protected)

    def _depth_limit(self, executor: PositionExecutor, side: int, quantity: Fraction, *, maker: bool) -> Fraction:
        if not maker and self._book_price(executor.instrument.instrument_id, side) is not None:
            return min(quantity, self._book(executor.instrument.instrument_id).depth[side])
        if self._trade_budget is not None:
            return min(quantity, self._trade_budget)
        return quantity

    def _entry_quantity(
        self, executor: PositionExecutor, liquidity: _BarLiquidity, entry: _Entry, *, maker: bool,
    ) -> Fraction:
        available = self._depth_limit(executor, executor.side, liquidity.entry, maker=maker)
        return super()._entry_quantity(executor, _BarLiquidity(available, liquidity.exit), entry, maker=maker)

    def _exit_quantity(
        self, executor: PositionExecutor, liquidity: _BarLiquidity, quantity: Fraction, *, maker: bool = False,
    ) -> Fraction:
        quantity = self._depth_limit(executor, -executor.side, quantity, maker=maker)
        return super()._exit_quantity(executor, liquidity, quantity, maker=maker)

    def _consume(self, executor: PositionExecutor, side: int, qty: Fraction, *, maker: bool) -> None:
        if not maker and self._book_price(executor.instrument.instrument_id, side) is not None:
            self._book(executor.instrument.instrument_id).depth[side] -= qty
        elif self._trade_budget is not None:
            self._trade_budget -= qty

    def _fill_entry(
        self, executor: PositionExecutor, entry: _Entry, price: Decimal, liquidity: str, qty: Fraction,
    ) -> None:
        self._consume(executor, executor.side, qty, maker=liquidity == "MAKER")
        super()._fill_entry(executor, entry, price, liquidity, qty)

    def _exit(
        self, executor: PositionExecutor, qty: Fraction, reference: Decimal, reason: str,
        *, maker: bool = False, liquidated: bool = False, liquidation_price: tuple[Decimal, FillTier | None] | None = None,
    ) -> None:
        if maker:
            reference = min(executor.barriers.take_profit, key=lambda price: executor.side * price)
        if not liquidated:
            self._consume(executor, -executor.side, qty, maker=maker)
        super()._exit(
            executor, qty, reference, reason, maker=maker, liquidated=liquidated, liquidation_price=liquidation_price,
        )

    def _match_at_tick(
        self, executor: PositionExecutor, entry: _Entry, tick: Trade | None,
    ) -> tuple[Decimal, str] | None:
        side = executor.side
        if not entry.resting:
            if tick is None:
                reference = self._book_price(executor.instrument.instrument_id, side)
            elif self._uncertain_prices is not None:
                reference = self._uncertain_prices[1 if side == 1 else 0]
            else:
                reference = tick.price
            if reference is not None:
                return self._match_arriving_entry(executor, entry, reference)
        elif tick is not None and tick.side == ("Sell" if side == 1 else "Buy"):
            limit = self._entry_limit(executor, entry)
            if side * (limit - tick.price) > 0:
                return limit, "MAKER"
        return None

    def _liquidity(self, trade: Trade) -> _BarLiquidity:
        volume = Fraction(trade.size)
        key = trade.instrument_id, trade.side
        entry_remainder, exit_remainder = self._residuals.get(key, (Fraction(0), Fraction(0)))
        entry_rate = Fraction(self._params.volume_participation)
        exit_rate = Fraction(self._params.exit_volume_participation or self._params.volume_participation)
        entry = volume * entry_rate + entry_remainder
        exit_qty = volume * exit_rate + exit_remainder
        step = Fraction(self._pack.instrument(trade.instrument_id).qty_step)
        self._residuals[key] = entry % step, exit_qty % step
        return _BarLiquidity(entry, exit_qty)

    def _match_tick(self, instrument: str, trade: Trade | None) -> Iterator[tuple[DerivativePoint, ...]]:
        at = self._event_time
        executors = tuple(
            executor for executor in self._live_rounds.values() if executor.instrument.instrument_id == instrument
        )
        if not executors:
            return
        self._tick_tier = "trade" if trade is not None else "l1"
        if trade is not None:
            liquidity = self._liquidity(trade)
            self._trade_budget = max(liquidity.entry, liquidity.exit)
        else:
            book = self._book(instrument)
            if book is None:
                self._tick_tier = None
                return
            quote = book.quote
        try:
            for executor in executors:
                side = executor.side
                if trade is not None:
                    entry_liquidity = exit_liquidity = liquidity
                    price = trade.price
                else:
                    entry_liquidity = _BarLiquidity(book.depth[side], Fraction(0))
                    exit_liquidity = _BarLiquidity(Fraction(0), book.depth[-side])
                    price = quote.bid_price if side == 1 and quote.bid_size else (
                        quote.ask_price if side == -1 and quote.ask_size else None
                    )
                if self._enforce_holding_deadline(executor, at):
                    yield ()
                yield from self._match_entries(
                    executor, at, entry_liquidity, lambda entry: self._match_at_tick(executor, entry, trade),
                    one_per_slot=False,
                )
                if price is None or not executor.held_at(at):
                    continue
                if executor.stop_pending:
                    if self._fill_reduction(executor, exit_liquidity, price):
                        yield ()
                    continue
                stop, trailing = executor.stop_at(at)
                if side * (price - stop) <= 0:
                    self._protect(executor, exit_liquidity, price, "TRAILING_STOP" if trailing else "STOP_LOSS")
                    yield ()
                    continue
                uncertain = self._uncertain_prices
                if uncertain is not None and side * (uncertain[0 if side == 1 else 1] - stop) <= 0:
                    continue
                target = min(executor.barriers.take_profit, key=lambda value: side * value)
                if (
                    trade is not None and trade.side == ("Buy" if side == 1 else "Sell")
                    and side * (price - target) > 0
                    and self._protect(executor, exit_liquidity, target, "TAKE_PROFIT", maker=True)
                ):
                    yield ()
                request = executor.reduction
                if request is not None and request.requested_at <= at:
                    if self._fill_reduction(executor, exit_liquidity, price):
                        yield ()
                if (
                    uncertain is None and executor.quantity and executor.phase != RoundPhase.EXITING
                    and self._ratchet(executor, price)
                ):
                    yield ()
        finally:
            self._tick_tier = None
            self._trade_budget = None

    def _observe_trade(self, trade: Trade, *, seen: bool) -> None:
        instrument = trade.instrument_id
        previous = self._last_trades.get(instrument, [])
        if seen:
            for index, row in enumerate(previous):
                if row.trade_id == trade.trade_id and trade_version(trade) > trade_version(row):
                    previous[index] = trade
                    break
            return
        if not previous or trade.ts_event >= max(row.ts_event for row in previous):
            if self._uncertain_prices is not None and previous and (
                trade.ts_event, trade.ts_available
            ) == (previous[-1].ts_event, previous[-1].ts_available):
                previous.append(trade)
            else:
                self._last_trades[instrument] = [trade]

    def _tick_actions(self, event: TickMarketEvent) -> Iterator[tuple[DerivativePoint, ...]]:
        if not event.ticks:
            if not event.seed:
                yield from self._match_tick(event.instrument_id, None)
            return
        # 先在整个可获取时点选版。修订可以改动事件时间。
        trades_by_id: dict[UUID, Trade] = {}
        quotes_by_update: dict[tuple[UUID, int], Quote] = {}
        unsequenced_quotes = []
        for row in event.ticks:
            if isinstance(row, Trade):
                previous = trades_by_id.get(row.trade_id)
                if previous is None or trade_version(row) > trade_version(previous):
                    trades_by_id[row.trade_id] = row
            elif row.connection_id is not None and row.update_id is not None:
                key = row.connection_id, row.update_id
                previous_quote = quotes_by_update.get(key)
                if previous_quote is None or _quote_version(row) > _quote_version(previous_quote):
                    quotes_by_update[key] = row
            else:
                unsequenced_quotes.append(row)
        ordered = sorted(
            chain(trades_by_id.values(), quotes_by_update.values(), unsequenced_quotes),
            key=lambda row: row.ts_event,
        )
        seen_trades = self._seen_trades.setdefault(event.instrument_id, set())
        for _, group in groupby(ordered, key=lambda row: row.ts_event):
            rows = tuple(group)
            trades = sorted((row for row in rows if isinstance(row, Trade)), key=tick_order)
            fresh = [row for row in trades if row.trade_id.bytes not in seen_trades]
            prices = [row.price for row in fresh]
            if prices and min(prices) != max(prices) and any(
                not relative_tick_order_known(first, second) for first, second in pairwise(fresh)
            ):
                self._uncertain_prices = min(prices), max(prices)
            try:
                for trade in trades:
                    seen = trade.trade_id.bytes in seen_trades
                    self._observe_trade(trade, seen=seen)
                    if seen:
                        continue
                    seen_trades.add(trade.trade_id.bytes)
                    if not event.seed and trade.ts_event // _MINUTE * _MINUTE not in event.fallback_slots:
                        yield from self._match_tick(trade.instrument_id, trade)
            finally:
                self._uncertain_prices = None
            for row in _quote_updates(row for row in rows if isinstance(row, Quote)):
                if self._observe_quote(row) and not event.seed:
                    yield from self._match_tick(row.instrument_id, None)


l1_trade_sim = SlotPlugin(Slot.FILL, (INTERFACE_VERSION,), L1TradeSim, L1TradeSimParams)
