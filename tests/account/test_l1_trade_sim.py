"""trading/05 的逐笔顺序和点差降级契约。"""

from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
from uuid import UUID

import pytest

from talaria.account.contracts import MarketEvent
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.host import default_registry
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, SchemeError, Slot
from talaria.foundation.ticks import Quote, TickKind, Trade
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader, ColdStore

from test_bar_sim_1m import INSTRUMENT, MINUTE, approved, derivative, market, step

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


def test_paper_mode_rejects_l1_fill_at_assembly(tmp_path):
    registry = default_registry()
    plugin = registry.resolve(Slot.FILL, "l1_trade_sim", INTERFACE_VERSION, tmp_path, mode="paper")
    with pytest.raises(SchemeError, match="l1_trade_sim 仅支持回测"):
        plugin.create(PluginContext(tmp_path, "paper", 7, lambda: 0), {})


@pytest.mark.parametrize("bar_volume", ["30", "0"])
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
    assert fill.submit_plan(approved(decision=at - 1, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price, entry.fill_tier) == (at, D(price), tier)


def test_fallback_ignores_trades_only_available_after_the_minute(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[trade(opening + 1, 125, 1, available=opening + MINUTE + 1)])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry, exit_fill] = [
        e for e in step(fill, market(opening, open="100", low="80", high="125"))
        if e.kind in {"ENTRY_FILLED", "EXIT_FILLED"}
    ]
    assert (entry.fill_tier, exit_fill.fill_tier, exit_fill.reason) == ("bar_1m", "bar_1m", "STOP_LOSS")


def test_quote_only_bar_uses_latest_book_update_for_taker_price(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, quotes=[
        quote(opening - 5, "99.8", "100.3", 2),
        quote(opening - 5, "100.0", "100.5", 1),
    ])
    assert fill.submit_plan(approved(decision=opening - 1, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, open="100")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price, entry.fill_tier) == (opening, D("100.4"), "l1")


def test_unknown_same_millisecond_order_uses_conservative_stop(tmp_path):
    opening = 2 * MINUTE
    at = opening + 10
    unordered = [
        replace(trade(at, 125, 2), sequence=None),
        replace(trade(at, 80, 3), sequence=None),
    ]
    fill = engine(tmp_path, trades=[trade(opening + 1, 100, 1), *unordered])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [exit_fill] = [
        e for e in step(fill, market(opening, open="100", low="80", high="125", volume="30")) if e.kind == "EXIT_FILLED"
    ]
    assert (exit_fill.reason, exit_fill.ts_event) == ("STOP_LOSS", at)


def test_partial_tick_stop_keeps_its_reason_after_ohlc_fallback(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[
        trade(opening + 1, 100, 1),
        replace(trade(opening + 2, 89, 2), size=D(5)),
    ])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [partial] = [e for e in step(fill, market(opening, open="100", low="89", high="100", close="89", volume="15")) if e.kind == "EXIT_FILLED"]
    assert (partial.reason, partial.position.quantity, partial.fill_tier) == ("STOP_LOSS", D("0.5"), "trade")
    [remainder] = [e for e in step(
        fill, market(opening + MINUTE, open="125", low="120", high="126", close="125"),
    ) if e.kind == "EXIT_FILLED"]
    assert (remainder.reason, remainder.position.quantity, remainder.fill_tier) == ("STOP_LOSS", D(0), "bar_1m")


@pytest.mark.parametrize(("source", "price"), [("l1", "73.9"), ("trade", "74.9"), ("mark", "69.9")])
def test_mark_triggers_liquidation_with_the_best_visible_execution_price(tmp_path, source, price):
    opening = 2 * MINUTE
    at = opening + MINUTE + 2
    trades = [trade(at - 1, 75, 1)] if source == "trade" else []
    quotes = [quote(at - 1, 74, 76, 1)] if source == "l1" else []
    spec = get_market_pack(DEFAULT_PACK)
    fill = engine(tmp_path, trades=trades, quotes=quotes, risk=AccountRiskConfig(margin_mode="ISOLATED"))
    assert fill.submit_plan(approved(qty="1", stop="60")).accepted
    step(fill, market(opening), spec.margin_valuation())
    mark = MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(derivative("mark_price", at, "70"),))
    [exit_fill] = [e for e in step(fill, mark, spec.margin_valuation()) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.fill_tier, exit_fill.price) == ("MARGIN_LIQUIDATION", source, D(price))


@pytest.mark.regression
def test_trade_uses_its_visible_version_and_late_trade_crosses_the_minute(tmp_path):
    opening = 2 * MINUTE
    first = trade(opening + 10, 100, 1)
    revision = replace(first, price=D(150), revision=1, ts_available=opening + 20)
    stop = trade(opening + MINUTE - 1, 80, 2, available=opening + MINUTE + 2)
    fill = engine(tmp_path, trades=[first, revision, stop])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (opening + 10, D("100.1"))
    [exit_fill] = [e for e in step(fill, market(opening + MINUTE, volume="0")) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.ts_event, exit_fill.reason) == (opening + MINUTE + 2, "STOP_LOSS")


@pytest.mark.regression
def test_liquidation_prices_from_processed_trade_before_same_millisecond_trade(tmp_path):
    opening = 2 * MINUTE
    at = opening + 30
    pack = get_market_pack(DEFAULT_PACK)
    fill = engine(tmp_path, trades=[
        trade(opening + 10, 100, 1), trade(at - 1, 95, 2), trade(at, 80, 3),
    ], risk=AccountRiskConfig(margin_mode="ISOLATED"))
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    mark = derivative("mark_price", at, "70")
    [exit_fill] = [e for e in step(
        fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(mark,)), pack.margin_valuation(),
    ) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.ts_event, exit_fill.reason, exit_fill.price) == (at, "MARGIN_LIQUIDATION", D("94.9"))


@pytest.mark.regression
@pytest.mark.parametrize("reverse", [False, True])
def test_tied_quotes_are_adverse_and_follow_the_new_connection_update_sequence(tmp_path, reverse):
    opening = 2 * MINUTE
    at = opening + 10
    # 同代际序号 2 不能提前供序号 1 撮合。跨代际的 UUID 本身不编码顺位。
    quotes = [
        quote(at, 99, 106, 1),
        quote(at, 99, 102, 2),
        replace(quote(at - 5, 99, 115, 50), connection_id=UUID(int=20)),
    ]
    fill = engine(tmp_path, quotes=quotes[::-1] if reverse else quotes)
    assert fill.submit_plan(approved(decision=at - 1, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (at, D("106.1"))


@pytest.mark.regression
def test_delay_expiry_matches_previous_fresh_quote_before_later_trade(tmp_path):
    opening = 2 * MINUTE
    fill = engine(
        tmp_path, quotes=[quote(opening + 1, 98, 100, 1)],
        trades=[trade(opening + 20, 115, 1)], decision_latency_ms=5,
    )
    assert fill.submit_plan(approved(decision=opening, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.fill_tier, entry.price) == (opening + 5, "l1", D("100.1"))


@pytest.mark.regression
@pytest.mark.parametrize(("tick_price", "tick_size"), [(125, "10"), (80, "9")])
def test_partial_trade_coverage_uses_conservative_bar_stop(tmp_path, tick_price, tick_size):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[replace(trade(opening + MINUTE + 10, tick_price, 1), size=D(tick_size))])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    step(fill, market(opening, open="100"))
    [exit_fill] = [e for e in step(
        fill, market(opening + MINUTE, open="100", low="80", high="125"),
    ) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.fill_tier, exit_fill.price) == ("STOP_LOSS", "bar_1m", D("89.9"))


@pytest.mark.regression
def test_ohlc_stop_never_prices_from_pre_drop_book(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, quotes=[quote(opening - 10, 100, 101, 1)])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [exit_fill] = [e for e in step(
        fill, market(opening, open="100", low="80"),
    ) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.fill_tier, exit_fill.price) == ("bar_1m", D("89.9"))


@pytest.mark.regression
def test_maker_uses_opposing_aggressor_at_limit_without_price_improvement(tmp_path):
    opening = 2 * MINUTE
    rows = [trade(opening + i, price, i) for i, price in ((1, 102), (2, 99), (3, 100), (4, 98))]
    rows[2] = replace(rows[2], side="Sell")
    rows[3] = replace(rows[3], side="Sell")
    fill = engine(tmp_path, trades=rows)
    assert fill.submit_plan(approved(qty="1", order="POST_ONLY")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.reason, entry.price) == (opening + 4, "MAKER", D(100))


@pytest.mark.regression
def test_taker_waits_for_fresh_depth_after_consuming_best_offer(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[trade(opening + 2, 100, 1)], quotes=[
        replace(quote(opening + 1, 99, 100, 1), ask_size=D("0.3")),
        replace(quote(opening + 3, 100, 102, 2), ask_size=D("0.7")),
    ])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    entries = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert [(e.ts_event, e.price, e.quantity) for e in entries] == [
        (opening + 1, D("100.1"), D("0.3")), (opening + 3, D("102.1"), D("0.7")),
    ]


@pytest.mark.regression
def test_empty_offer_cannot_reject_post_only_order(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, quotes=[
        replace(quote(opening + 1, 96, 97, 1), ask_size=D(0)),
        quote(opening + 2, 101, 102, 2),
    ])
    assert fill.submit_plan(approved(qty="1", order="POST_ONLY")).accepted
    assert not step(fill, market(opening, volume="0"))


@pytest.mark.regression
def test_equal_price_unsequenced_trades_still_ratchet_and_exit(tmp_path):
    opening = 2 * MINUTE
    at = opening + 10
    fill = engine(tmp_path, trades=[
        trade(opening + 1, 100, 1),
        replace(trade(at, 110, 2), sequence=None),
        replace(trade(at, 110, 3), sequence=None),
        trade(at + 1, 103, 4),
    ])
    assert fill.submit_plan(approved(
        qty="1", order="MARKET", high="110",
        trailing_stop={"activation_pct": "0.04", "distance_pct": "0.05"},
    )).accepted
    [exit_fill] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.ts_event, exit_fill.reason) == (at + 1, "TRAILING_STOP")


@pytest.mark.regression
def test_trade_participation_preserves_quantity_step_remainder(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path, trades=[
        replace(trade(opening + i, 100, i), size=D("0.006")) for i in (1, 2)
    ])
    assert fill.submit_plan(approved(qty="0.001", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.quantity) == (opening + 2, D("0.001"))


@pytest.mark.regression
def test_first_visible_revision_wins_even_if_it_moves_event_time(tmp_path):
    opening = 2 * MINUTE
    at = opening + 20
    old = trade(at - 2, 100, 1, available=at)
    corrected = replace(old, ts_event=at - 1, price=D(102), revision=1)
    fill = engine(tmp_path, trades=[old, corrected])
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (at, D("102.1"))


@pytest.mark.regression
def test_fallback_protects_position_opened_by_midminute_quote(tmp_path):
    opening = 2 * MINUTE
    at = opening + 20
    fill = engine(tmp_path, quotes=[quote(at, 99, 100, 1)])
    assert fill.submit_plan(approved(decision=at - 1, qty="1", order="MARKET", high="110")).accepted
    [entry, exit_fill] = [
        e for e in step(fill, market(opening, low="80")) if e.fill_tier is not None
    ]
    assert (entry.ts_event, entry.fill_tier) == (at, "l1")
    assert (exit_fill.ts_event, exit_fill.reason, exit_fill.price) == (
        opening + MINUTE, "STOP_LOSS", D("89.9"),
    )


@pytest.mark.regression
def test_liquidation_prices_only_visible_quantity_at_best_bid(tmp_path):
    opening = 2 * MINUTE
    at = opening + 30
    pack = get_market_pack(DEFAULT_PACK)
    fill = engine(tmp_path, trades=[trade(opening + 10, 100, 1), trade(at - 2, 96, 2)], quotes=[
        replace(quote(at - 1, 94, 95, 1), bid_size=D("0.3")),
    ], risk=AccountRiskConfig(margin_mode="ISOLATED"))
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    mark = derivative("mark_price", at, "70")
    fills = [e for e in step(
        fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(mark,)), pack.margin_valuation(),
    ) if e.kind == "EXIT_FILLED"]
    assert [(e.quantity, e.fill_tier) for e in fills] == [(D("0.3"), "l1"), (D("0.7"), "trade")]
    assert fills[0].price == D("93.9")
    assert fills[1].price < fills[0].price
    assert all(e.reason == "MARGIN_LIQUIDATION" for e in fills)


@pytest.mark.regression
def test_l1_bar_fallback_maker_entry_keeps_limit_price(tmp_path):
    opening = 2 * MINUTE
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", order="POST_ONLY")).accepted
    assert not step(fill, market(opening, low="100"))
    [entry] = [e for e in step(
        fill, market(opening + MINUTE, open="97", low="97"),
    ) if e.kind == "ENTRY_FILLED"]
    assert (entry.reason, entry.fill_tier, entry.price) == ("MAKER", "bar_1m", D("100"))


@pytest.mark.regression
@pytest.mark.parametrize("reverse", [False, True])
def test_unsequenced_same_millisecond_connections_use_adverse_side(tmp_path, reverse):
    opening = 2 * MINUTE
    at = opening + 10
    spec = get_market_pack(DEFAULT_PACK).instrument(INSTRUMENT)
    rows = [
        quote(at, 99, 102, 2),
        replace(quote(at, 100, 106, 1), connection_id=UUID(int=1)),
    ]
    with closing(ColdStore(tmp_path)) as store:
        for row in rows[::-1] if reverse else rows:
            store.publish_ticks(spec, TickKind.QUOTES, [row])
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(decision=at - 1, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (at, D("106.1"))


@pytest.mark.regression
def test_same_visible_quote_revision_wins_when_event_time_changes(tmp_path):
    opening = 2 * MINUTE
    at = opening + 20
    old = quote(at - 2, 99, 100, 1, available=at)
    corrected = replace(old, ts_event=at - 1, ask_price=D(102), revision=1)
    fill = engine(tmp_path, quotes=[old, corrected])
    assert fill.submit_plan(approved(decision=at - 1, qty="1", order="MARKET", high="110")).accepted
    [entry] = [e for e in step(fill, market(opening, volume="0")) if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (at, D("102.1"))


@pytest.mark.regression
def test_liquidation_chooses_adverse_last_price_when_prior_trade_order_is_unknown(tmp_path):
    opening = 2 * MINUTE
    at = opening + 30
    pack = get_market_pack(DEFAULT_PACK)
    fill = engine(tmp_path, trades=[
        trade(opening + 10, 100, 1),
        replace(trade(at - 1, 95, 2), sequence=None),
        replace(trade(at - 1, 100, 3), sequence=None),
    ], risk=AccountRiskConfig(margin_mode="ISOLATED"))
    assert fill.submit_plan(approved(qty="1", order="MARKET", high="110")).accepted
    mark = derivative("mark_price", at, "70")
    [exit_fill] = [e for e in step(
        fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(mark,)), pack.margin_valuation(),
    ) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.price) == ("MARGIN_LIQUIDATION", D("94.9"))
