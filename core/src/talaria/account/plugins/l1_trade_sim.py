"""用逐笔路径与买一卖一驱动共用的仓位执行器。"""

from collections.abc import Iterator, Sequence
from decimal import Decimal
from fractions import Fraction
from itertools import groupby

from pydantic import Field, StrictInt

from talaria.account.contracts import FillTier, MarketEvent, RoundPhase
from talaria.account.plugins.bar_sim_1m import BarSim1m, BarSimParams, PositionExecutor, _BarLiquidity, _Entry
from talaria.account.plugins.parquet import ReplayTickWindow
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, Slot, SlotPlugin
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.ticks import Quote, TickKind, Trade
from talaria.foundation.timeframes import timeframe_ms
from talaria.perception.cold_store import ColdReader

_MINUTE = timeframe_ms("1m")


class L1TradeSimParams(BarSimParams):
    quote_max_age_ms: StrictInt = Field(default=3_000, ge=0)


class _Book:
    def __init__(self, rows: Sequence[Quote], max_age: int) -> None:
        self._updates = iter(sorted(rows, key=lambda row: (row.ts_available, row.ts_ingested, row.ts_event)))
        self._next = next(self._updates, None)
        self._current: Quote | None = None
        self._max_age = max_age

    def at(self, timestamp: int) -> Quote | None:
        while self._next is not None and self._next.ts_available <= timestamp:
            update = self._next
            self._next = next(self._updates, None)
            current = self._current
            if current is not None:
                if (
                    update.connection_id is not None and update.connection_id == current.connection_id
                    and update.update_id is not None and current.update_id is not None
                ):
                    if (update.update_id, update.revision) < (current.update_id, current.revision):
                        continue
                elif (update.ts_event, update.revision) < (current.ts_event, current.revision):
                    continue
            self._current = update
        current = self._current
        if current is not None and current.ts_available <= timestamp and timestamp - current.ts_event <= self._max_age:
            return current
        return None


class L1TradeSim(BarSim1m):
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        settings = L1TradeSimParams.model_validate(params)
        super().__init__(context, settings)
        self._quote_max_age = settings.quote_max_age_ms
        self._reader = context.cold_reader or ColdReader(context.data_dir)
        self._tick_tier: FillTier | None = None
        self._books: dict[str, _Book] = {}
        self._uncertain_prices: tuple[Decimal, Decimal] | None = None
        self._matched_books: dict[str, Quote] = {}

    def _book_price(self, instrument: str, side: int) -> Decimal | None:
        book = self._books.get(instrument)
        return self._quote_price(None if book is None else book.at(self._event_time), side)

    @staticmethod
    def _quote_price(quote: Quote | None, side: int) -> Decimal | None:
        if quote is None:
            return None
        if side == 1:
            return quote.ask_price if quote.ask_size > 0 else None
        return quote.bid_price if quote.bid_size > 0 else None

    def _fill_tier(self, executor: PositionExecutor, side: int, *, maker: bool) -> FillTier:
        if not maker and self._book_price(executor.instrument.instrument_id, side) is not None:
            return "l1"
        return self._tick_tier or "bar_1m"

    def _liquidation_price(self, executor: PositionExecutor, mark: Decimal) -> tuple[Decimal, FillTier]:
        at = self._event_time
        side = -executor.side
        quotes = self._reader.read_tick_revisions(
            executor.instrument, TickKind.QUOTES, start_ts=max(0, at - self._quote_max_age), end_ts=at, as_of=at,
        )
        quote = self._quote_price(_Book(quotes, self._quote_max_age).at(at), side)
        if quote is not None:
            return self._price_with_friction(executor, quote, side, quoted=True), "l1"
        trades = self._reader.read_ticks(
            executor.instrument, TickKind.TRADES, start_ts=max(0, at - _MINUTE + 1), end_ts=at, as_of=at,
        )
        if trades:
            return self._price_with_friction(executor, trades[-1].price, side), "trade"
        price, _ = super()._liquidation_price(executor, mark)
        return price, "mark"

    def _matching_timeline(
        self, event: MarketEvent, bar: RangeBar | None,
    ) -> Iterator[tuple[int, str | Trade | Quote]]:
        if not self._live_rounds:
            yield from super()._matching_timeline(event, bar)
            return
        raw = event.bar
        if bar is not None:
            start = bar.ts_event
            end = start + _MINUTE - 1
        elif isinstance(event, ReplayTickWindow):
            start, end = event.start, event.end
        elif (
            raw is not None and event.timeframe == "1m" and raw.confirmed
            and max(raw.ts_event + _MINUTE, raw.ts_available) <= event.as_of
            and raw.ts_event not in self._seen_slots.get(event.instrument_id, ())
        ):
            start, end = raw.ts_event, raw.ts_event + _MINUTE - 1
        else:
            return
        cutoff = min(event.as_of, end + 1)
        spec = self._pack.instrument(event.instrument_id)
        quotes = self._reader.read_tick_revisions(
            spec, TickKind.QUOTES, start_ts=max(0, start - self._quote_max_age), end_ts=cutoff, as_of=cutoff,
        )
        self._books[event.instrument_id] = _Book(quotes, self._quote_max_age)
        trades = self._reader.read_ticks(
            spec, TickKind.TRADES, start_ts=start, end_ts=end, as_of=cutoff,
        )
        if raw is not None and bar is None and (trades or quotes):
            self._seen_slots.setdefault(event.instrument_id, set()).add(start)
        if not trades:
            if bar is not None:
                yield from super()._matching_timeline(event, bar)
            else:
                # 同一毫秒只按当时最新的买一卖一撮合。
                quote_times = sorted(
                    ((max(start, row.ts_available), row) for row in quotes if row.ts_available <= end),
                    key=lambda pair: pair[0],
                )
                for timestamp, updates in groupby(quote_times, key=lambda pair: pair[0]):
                    yield timestamp, next(updates)[1]
            return
        for timestamp, bucket in groupby(trades, key=lambda row: row.ts_event):
            rows = tuple(bucket)
            if rows[0].order_uncertain:
                self._uncertain_prices = min(row.price for row in rows), max(row.price for row in rows)
            try:
                for trade in rows:
                    if not self._live_rounds:
                        return
                    yield timestamp, trade
            finally:
                self._uncertain_prices = None

    def _match_at_price(
        self, executor: PositionExecutor, entry: _Entry, reference: Decimal,
    ) -> tuple[Decimal, str] | None:
        if not entry.resting:
            return self._match_arriving_entry(executor, entry, reference)
        candidate = entry.plan.candidate
        limit = candidate.entry_high if executor.side == 1 else candidate.entry_low
        if executor.side * (limit - reference) > 0:
            price = reference if candidate.entry_low <= reference <= candidate.entry_high else limit
            return price, "MAKER"
        return None

    def _liquidity(self, size: Decimal) -> _BarLiquidity:
        volume = Fraction(size)
        return _BarLiquidity(
            volume * Fraction(self._params.volume_participation),
            volume * Fraction(self._params.exit_volume_participation or self._params.volume_participation),
        )

    def _tick_actions(self, tick: Trade | Quote) -> Iterator[tuple[DerivativePoint, ...]]:
        if not self._live_rounds:
            return
        at = self._event_time
        if isinstance(tick, Trade):
            self._tick_tier = "trade"
            liquidity = self._liquidity(tick.size)
            buy_price = sell_price = tick.price
            buy_liquidity = sell_liquidity = liquidity
        else:
            quote = self._books[tick.instrument_id].at(at)
            if quote is None or quote == self._matched_books.get(tick.instrument_id):
                return
            self._matched_books[tick.instrument_id] = quote
            self._tick_tier = "l1"
            buy_price, sell_price = quote.ask_price, quote.bid_price
            buy_liquidity, sell_liquidity = self._liquidity(quote.ask_size), self._liquidity(quote.bid_size)
        try:
            for executor in tuple(self._live_rounds.values()):
                if executor.instrument.instrument_id != tick.instrument_id:
                    continue
                side = executor.side
                if side == 1:
                    entry_price, exit_price = buy_price, sell_price
                    entry_liquidity, exit_liquidity = buy_liquidity, sell_liquidity
                else:
                    entry_price, exit_price = sell_price, buy_price
                    entry_liquidity, exit_liquidity = sell_liquidity, buy_liquidity
                if self._enforce_holding_deadline(executor, at):
                    yield ()
                yield from self._match_entries(
                    executor, at, entry_liquidity, lambda entry: self._match_at_price(executor, entry, entry_price),
                    one_per_slot=False,
                )
                if not executor.held_at(at):
                    continue
                if executor.stop_pending:
                    if self._fill_reduction(executor, exit_liquidity, exit_price):
                        yield ()
                    continue
                stop, trailing = executor.stop_at(at)
                if side * (exit_price - stop) <= 0:
                    self._protect(executor, exit_liquidity, exit_price, "TRAILING_STOP" if trailing else "STOP_LOSS")
                    yield ()
                    continue
                if self._uncertain_prices is not None:
                    adverse = self._uncertain_prices[0 if side == 1 else 1]
                    if side * (adverse - stop) <= 0:
                        continue
                target = min(executor.barriers.take_profit, key=lambda price: side * price)
                if side * (exit_price - target) > 0:
                    if self._protect(executor, exit_liquidity, exit_price, "TAKE_PROFIT", maker=True):
                        yield ()
                request = executor.reduction
                if (
                    request is not None and request.requested_at <= at
                    and self._fill_reduction(executor, exit_liquidity, exit_price)
                ):
                    yield ()
                if (
                    self._uncertain_prices is None and executor.quantity and executor.phase != RoundPhase.EXITING
                    and self._ratchet(executor, exit_price)
                ):
                    yield ()
        finally:
            self._tick_tier = None


l1_trade_sim = SlotPlugin(Slot.FILL, (INTERFACE_VERSION,), L1TradeSim, L1TradeSimParams)
