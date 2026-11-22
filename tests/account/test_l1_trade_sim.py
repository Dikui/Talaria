"""trading/05 的逐笔顺序和点差降级契约。"""

from contextlib import closing
from decimal import Decimal as D
from uuid import UUID

import pytest

from talaria.account.host import default_registry
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, Slot
from talaria.foundation.ticks import Quote, TickKind, Trade
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader, ColdStore

from test_bar_sim_1m import INSTRUMENT, MINUTE, approved, market, step

pytestmark = [pytest.mark.small, pytest.mark.spec]


def trade(ts, price, sequence, *, available=None):
    return Trade(
        instrument_id=INSTRUMENT, ts_event=ts, ts_available=ts if available is None else available,
        ts_ingested=ts, ts_committed=ts, trade_id=UUID(int=sequence),
        price=D(price), size=D(10), side="Buy", sequence=sequence,
    )


def quote(ts, bid, ask, update, *, available=None):
    return Quote(
        instrument_id=INSTRUMENT, ts_event=ts, ts_available=ts if available is None else available,
        ts_ingested=ts, ts_committed=ts, bid_price=D(bid), ask_price=D(ask),
        bid_size=D(10), ask_size=D(10), connection_id=UUID(int=10), update_id=update,
    )


def engine(tmp_path, trades=(), quotes=(), *, risk=None, **params):
    spec = get_market_pack(DEFAULT_PACK).instrument(INSTRUMENT)
    with closing(ColdStore(tmp_path)) as store:
        if trades:
            store.publish_ticks(spec, TickKind.TRADES, trades)
        if quotes:
            store.publish_ticks(spec, TickKind.QUOTES, quotes)
    plugin = default_registry().resolve(Slot.FILL, "l1_trade_sim", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0, {"one": risk} if risk is not None else {},
        cold_reader=ColdReader(tmp_path),
    )
    return plugin.create(context, params)


@pytest.mark.parametrize("bar_volume", ["10", "0"])
def test_same_millisecond_trade_sequence_takes_profit_before_stop(tmp_path, bar_volume):
    opening = 2 * MINUTE
    first = opening + 200
    both = first + 10
    fill = engine(tmp_path, trades=[trade(first, 100, 1), trade(both, 80, 3), trade(both, 125, 2)])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    events = step(fill, market(opening, open="100", low="80", high="125", volume=bar_volume))
    [entry, exit_fill] = [event for event in events if event.kind in {"ENTRY_FILLED", "EXIT_FILLED"}]
    assert (entry.ts_event, entry.fill_tier) == (first, "trade")
    assert (exit_fill.ts_event, exit_fill.reason, exit_fill.fill_tier) == (both, "TAKE_PROFIT", "trade")


@pytest.mark.parametrize(("quote_offset", "quote_delay", "tier", "price"), [
    (None, 0, "trade", "101.1"),
    (-1_001, 0, "trade", "101.1"),
    (-5, 10, "trade", "101.1"),
    (-5, 0, "l1", "100.4"),
    (5, 0, "trade", "101.1"),
])
def test_taker_uses_only_quotes_visible_and_fresh_at_the_trade(
    tmp_path, quote_offset, quote_delay, tier, price,
):
    opening = 2 * MINUTE
    at = opening + 2_000
    quotes = []
    if quote_offset is not None:
        observed = at + quote_offset
        quotes.append(quote(observed, "99.8", "100.3", 1, available=observed + quote_delay))
    fill = engine(
        tmp_path, trades=[trade(at, 100, 1)], quotes=quotes,
        quote_max_age_ms=1_000, spread_pct="0.02",
    )
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, open="100")) if e.kind == "ENTRY_FILLED"]
    assert (entry.price, entry.fill_tier) == (D(price), tier)


def test_fallback_ignores_trades_only_available_after_the_minute(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[trade(opening + 1, 125, 1, available=opening + MINUTE + 1)])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry, exit_fill] = [
        e for e in step(fill, market(opening, open="100", low="80", high="125", available=opening + MINUTE + 10))
        if e.kind in {"ENTRY_FILLED", "EXIT_FILLED"}
    ]
    assert (entry.fill_tier, exit_fill.fill_tier, exit_fill.reason) == ("bar_1m", "bar_1m", "STOP_LOSS")


def test_quote_only_bar_uses_latest_book_update_for_taker_price(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, quotes=[
        quote(opening - 5, "99.8", "100.3", 2),
        quote(opening - 5, "100.0", "100.5", 1),
    ])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, open="100")) if e.kind == "ENTRY_FILLED"]
    assert (entry.price, entry.fill_tier) == (D("100.4"), "l1")


def test_unknown_same_millisecond_order_uses_conservative_stop(tmp_path):
    from dataclasses import replace

    opening = 2 * MINUTE
    at = opening + 10
    unordered = [
        replace(trade(at, 125, 2), sequence=None),
        replace(trade(at, 80, 3), sequence=None),
    ]
    fill = engine(tmp_path, trades=[trade(opening + 1, 100, 1), *unordered])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [exit_fill] = [
        e for e in step(fill, market(opening, open="100", low="80", high="125")) if e.kind == "EXIT_FILLED"
    ]
    assert (exit_fill.reason, exit_fill.ts_event) == ("STOP_LOSS", at)


def test_partial_tick_stop_keeps_its_reason_after_ohlc_fallback(tmp_path):
    from dataclasses import replace

    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[
        trade(opening + 1, 100, 1),
        replace(trade(opening + 2, 89, 2), size=D(5)),
    ])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [partial] = [e for e in step(fill, market(opening, open="100", low="89")) if e.kind == "EXIT_FILLED"]
    assert (partial.reason, partial.position.quantity, partial.fill_tier) == ("STOP_LOSS", D("0.5"), "trade")
    [remainder] = [e for e in step(
        fill, market(opening + MINUTE, open="125", low="120", high="126", close="125"),
    ) if e.kind == "EXIT_FILLED"]
    assert (remainder.reason, remainder.position.quantity, remainder.fill_tier) == ("STOP_LOSS", D(0), "bar_1m")


@pytest.mark.parametrize(("source", "price"), [("l1", "73.9"), ("trade", "74.9"), ("mark", "69.9")])
def test_mark_triggers_liquidation_with_the_best_visible_execution_price(tmp_path, source, price):
    from talaria.account.contracts import MarketEvent
    from talaria.account.risk_config import AccountRiskConfig

    from test_bar_sim_1m import derivative

    opening = 2 * MINUTE
    at = opening + MINUTE + 2
    trades = [trade(at - 1, 75, 1)] if source == "trade" else []
    quotes = [quote(at - 1, 74, 76, 1)] if source == "l1" else []
    spec = get_market_pack(DEFAULT_PACK)
    fill = engine(tmp_path, trades=trades, quotes=quotes, risk=AccountRiskConfig(margin_mode="ISOLATED"))
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(opening), spec.margin_valuation())
    mark = MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(derivative("mark_price", at, "70"),))
    [exit_fill] = [e for e in step(fill, mark, spec.margin_valuation()) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.fill_tier, exit_fill.price) == ("MARGIN_LIQUIDATION", source, D(price))
