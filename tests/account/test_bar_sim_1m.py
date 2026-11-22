"""trading/03、ADR-0003 与架构 §6.4 的默认成交契约。"""

from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
import sys

import pytest

from talaria.account.contracts import (
    ApprovedExecutionPlan, EntryCandidate, ExitBarrierSpec, MarketEvent, SizingDecision,
)
from talaria.account.host import default_registry, run_backtest
from talaria.account.ledger import read_journal
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, Slot
from talaria.foundation.bars import RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.types import PrecisionTier
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.small, pytest.mark.spec]
MINUTE = 60_000
INSTRUMENT = "BTC-USDT-PERP"


def engine(tmp_path, **params):
    plugin = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    return plugin.create(PluginContext(tmp_path, "backtest", 7, lambda: 0), params)


def approved(*, plan_id="p", round_id="r", add_on=False, decision=MINUTE, order="LIMIT",
             qty="2", low="95", high="100", stop="90", targets=("120",), direction="LONG", **barriers):
    return ApprovedExecutionPlan(
        plan_id=plan_id, round_id=round_id,
        candidate=EntryCandidate(
            sub_account_id="one", instrument_id=INSTRUMENT, direction=direction,
            origin="AGENT", order_type=order, ts_decision=decision,
            entry_low=low, entry_high=high, invalidation_stop=stop, take_profit=targets,
            round_id=round_id if add_on else None,
        ),
        sizing=SizingDecision(quantity=qty, basis="测试定仓"),
        barriers=ExitBarrierSpec(
            stop=stop, take_profit=targets,
            max_holding_ms=barriers.pop("max_holding_ms", 10 * MINUTE), **barriers,
        ),
    )


def market(ts, *, open="105", high="110", low="99", close="105", volume="10",
           timeframe="1m", confirmed=True, available=None, points=()):
    now = ts + MINUTE if available is None else available
    bar = RangeBar(
        ts_event=ts, ts_available=now, open=open, high=high, low=low, close=close,
        volume=volume, turnover="0", precision=PrecisionTier.SECOND_VERIFIED,
        revision=0, confirmed=confirmed,
    )
    return MarketEvent(now, INSTRUMENT, timeframe, bar, derivative_points=points)


def step(fill, event, valuation=None):
    fill.on_market(event, valuation)
    return fill.poll_events(event.as_of)


def test_latency_wall_excludes_ohlc_from_a_bar_that_straddles_arrival(tmp_path):
    fill = engine(tmp_path, decision_latency_ms=1_000)
    assert fill.submit_plan(approved(decision=MINUTE + 5_000)).accepted
    assert step(fill, market(MINUTE)) == []
    [entry] = [event for event in step(fill, market(2 * MINUTE)) if event.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price, entry.reason) == (2 * MINUTE + 1, D(100), "MAKER")


@pytest.mark.parametrize(("order", "opening", "lowest", "kind", "reason", "price", "fee"), [
    ("LIMIT", "105", "100", None, None, None, None),
    ("LIMIT", "105", "99.9", "ENTRY_FILLED", "MAKER", "100", "0.04"),
    ("LIMIT", "98.9", "98", "ENTRY_FILLED", "TAKER", "100", "0.12"),
    ("POST_ONLY", "99", "98", "PLAN_REJECTED", "POST_ONLY_WOULD_TAKE", None, None),
    ("MARKET", "101", "99", "ENTRY_FILLED", "TAKER", "102.1", "0.12252"),
])
def test_order_type_distinguishes_strict_maker_penetration_and_taker_costs(
    tmp_path, order, opening, lowest, kind, reason, price, fee,
):
    fill = engine(tmp_path, spread_pct="0.02", volume_participation="0.20")
    high = "110" if order == "MARKET" else "100"
    assert fill.submit_plan(approved(order=order, high=high)).accepted
    events = step(fill, market(2 * MINUTE, open=opening, low=lowest))
    if kind is None:
        assert events == []
        return
    event = next(event for event in events if event.kind == kind)
    assert event.reason == reason
    if price is not None:
        assert event.price == D(price)
        assert next(p.amount for p in event.postings if p.account == "EXPENSE_EXECUTION_FEE") == D(fee)


@pytest.mark.parametrize(("opening", "fill_price"), [("100", "89.9"), ("85", "84.9")])
def test_stop_wins_over_target_and_gaps_use_the_open(tmp_path, opening, fill_price):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE))
    events = step(fill, market(3 * MINUTE, open=opening, high="125", low="80", close="120"))
    [exit_fill] = [e for e in events if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.price, exit_fill.position.phase) == (
        "STOP_LOSS", D(fill_price), "CLOSED",
    )
    assert [p.amount for p in exit_fill.postings if p.account == "POSITION_COST"] == [D("-100")]


def test_nearest_take_profit_closes_the_whole_round(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", targets=("130", "120"))).accepted
    step(fill, market(2 * MINUTE))
    [trade] = [e for e in step(fill, market(3 * MINUTE, high="125")) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.position.phase) == ("TAKE_PROFIT", D(120), "CLOSED")


def test_first_partial_fill_is_protected_in_its_own_bar(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    events = step(fill, market(2 * MINUTE, low="89", high="125"))
    assert [(e.kind, e.position.quantity, e.position.phase) for e in events] == [
        ("ENTRY_FILLED", D(1), "PARTIALLY_FILLED_PROTECTED"),
        ("ROUND_EXITING", D(1), "EXITING"),
        ("PLAN_CANCELLED", D(1), "EXITING"),
        ("EXIT_FILLED", D(0), "CLOSED"),
    ]
    assert step(fill, market(3 * MINUTE)) == []


def test_partial_entry_and_add_on_share_barriers_and_first_fill_deadline(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(max_holding_ms=3 * MINUTE)).accepted
    [entry] = step(fill, market(2 * MINUTE))
    assert (entry.position.quantity, entry.position.phase) == (D(1), "PARTIALLY_FILLED_PROTECTED")

    status = fill.intervene_position("r", "CANCEL_ENTRY", None, "USER")
    assert (status.quantity, status.phase) == (D(1), "ACTIVE_PROTECTED")
    [cancel] = fill.poll_events(3 * MINUTE)
    assert (cancel.kind, cancel.plan_id) == ("PLAN_CANCELLED", "p")

    assert fill.submit_plan(approved(
        plan_id="add", add_on=True, decision=3 * MINUTE, qty="1", high="101", targets=("160",),
    )).accepted
    [entry, _] = step(fill, market(4 * MINUTE))
    assert (entry.plan_id, entry.position.round_id, entry.position.quantity, entry.position.phase) == (
        "add", "r", D(2), "ACTIVE_PROTECTED",
    )
    assert not [e for e in step(fill, market(5 * MINUTE)) if e.kind == "EXIT_FILLED"]
    [trade] = [e for e in step(fill, market(6 * MINUTE, volume="20")) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.quantity, trade.position.phase) == (
        "MAX_HOLDING_TIME", D("104.9"), D(2), "CLOSED",
    )
    assert [p.amount for p in trade.postings if p.account == "POSITION_COST"] == [D("-201")]


def test_trailing_stop_ratchets_and_never_retreats(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", targets=("150",), trailing_stop={
        "activation_pct": "0.1", "distance_pct": "0.05",
    })).accepted
    step(fill, market(2 * MINUTE))
    events = step(fill, market(3 * MINUTE, open="115", high="120", low="115", close="117"))
    [adjustment] = events
    assert (adjustment.kind, adjustment.position.stop) == ("BARRIER_TIGHTENED", D(114))
    assert step(fill, market(4 * MINUTE, open="117", high="119", low="115", close="116")) == []
    [trade] = [e for e in step(
        fill, market(5 * MINUTE, open="115", high="117", low="113", close="115"),
    ) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.position.stop) == ("TRAILING_STOP", D("113.9"), D(114))


def test_direct_risk_reduction_preserves_round_identity_and_rejects_stop_widening(tmp_path):
    from talaria.account.contracts import ApprovedBarrierAmendment, BarrierChangeRequest

    fill = engine(tmp_path, volume_participation="1")
    assert fill.submit_plan(approved()).accepted
    step(fill, market(2 * MINUTE))
    request = BarrierChangeRequest(sub_account_id="one", round_id="r", stop="85")
    assert not fill.tighten_barrier(request).accepted
    amendment = ApprovedBarrierAmendment(amendment_id="signed", request=request)
    assert fill.apply_approved_amendment(amendment).accepted
    assert not fill.apply_approved_amendment(amendment).accepted
    assert fill.tighten_barrier(request.model_copy(update={"stop": D(92)})).accepted
    result = fill.intervene_position("r", "REDUCE_RATIO", D("0.5"), "USER")
    assert (result.quantity, result.stop, result.phase) == (D(1), D(92), "ACTIVE_PROTECTED")
    result = fill.intervene_position("r", "CLOSE_ALL", None, "USER")
    assert (result.quantity, result.phase) == (D(0), "CLOSED")
    assert fill.intervene_position("r", "CLOSE_ALL", None, "USER") == result
    assert [trade.quantity for trade in fill.poll_events(3 * MINUTE) if trade.kind == "EXIT_FILLED"] == [D(1), D(1)]


def test_reducing_cancels_remainders_and_keeps_protection(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    step(fill, market(2 * MINUTE))
    fill.set_trading_state(None, "REDUCING", "USER")
    [cancel] = fill.poll_events(3 * MINUTE)
    assert (cancel.kind, cancel.position.quantity, cancel.position.phase) == (
        "PLAN_CANCELLED", D(1), "ACTIVE_PROTECTED",
    )
    assert not fill.submit_plan(approved(plan_id="other", round_id="next")).accepted
    [trade] = [e for e in step(fill, market(3 * MINUTE, low="80")) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.quantity) == ("STOP_LOSS", D(1))


def derivative(kind, ts, value, *, available=None, settled=None, revision=0):
    known = ts if available is None else available
    return DerivativePoint(
        kind=kind, instrument_id=INSTRUMENT, value=value, ts_event=ts, ts_available=known,
        ts_ingested=known, ts_committed=known, settled=settled, revision=revision,
    )


@pytest.mark.medium
def test_offline_replay_books_mark_liquidation_and_late_funding_reproducibly(
    tmp_path, monkeypatch, register_host_fixtures,
):
    from talaria import sdk

    def network(*args, **kwargs):
        pytest.fail("回测不应探测常驻服务")

    monkeypatch.setattr(sdk, "read_service_endpoint", network)
    instrument = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    start = 1_793_491_200_000
    settled_at = start + 3 * MINUTE + 10_000
    liquidated_at = settled_at + 10_000
    known_at = liquidated_at + 10_000
    corrected_at = known_at + 1_000
    bars = [
        ("100", "101", "99", "100"),
        ("100", "101", "99", "100"),
        ("110", "112", "104", "109"),
        ("109", "114", "95", "110"),
    ]
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(instrument, "1m", [
            RawBar(
                symbol=instrument.symbol, timeframe="1m", ts_event=start + i * MINUTE,
                open=D(opening), high=D(high), low=D(low), close=D(close),
                volume=D(100), turnover=D(10000), ts_available=start + (i + 1) * MINUTE,
                ts_ingested=start + (i + 1) * MINUTE, ts_committed=start + (i + 1) * MINUTE,
            )
            for i, (opening, high, low, close) in enumerate(bars)
        ]))
        marks = [
            derivative("mark_price", start, "105"),
            derivative("mark_price", settled_at, "101"),
            derivative("mark_price", liquidated_at, "70"),
        ]
        store.publish_derivatives(instrument, marks[0].kind, marks)
        funding = [
            derivative("funding_rate", settled_at, "0.1", settled=False),
            derivative("funding_rate", settled_at, "0.00005", available=known_at, settled=True),
            derivative("funding_rate", settled_at, "0.00006", available=corrected_at, settled=True, revision=1),
        ]
        store.publish_derivatives(instrument, funding[0].kind, funding)

    strategy = Path(__file__).parent / "fixtures" / "replay_strategy.py"
    (tmp_path / strategy.name).write_text(strategy.read_text().replace(
        "entry_high=bar.close + 2", "entry_high=bar.close + 5",
    ).replace("invalidation_stop=bar.close - 2, take_profit=[bar.close + 2]",
              "invalidation_stop=bar.close - 10, take_profit=[bar.close + 35]"))
    scheme = tmp_path / "sim.yaml"
    scheme.write_text(f"""
scheme: default-bar-sim
interface_version: 2
seed: 7
sub_accounts: [one]
account_risk:
  one: {{margin_mode: ISOLATED}}
slots:
  data:
    plugin: parquet_replay
    params: {{instrument: {INSTRUMENT}, timeframe: 1m, start: {start}, end: {start + 4 * MINUTE}}}
  strategy: {{python: {strategy.name}, params: {{trigger_price: "100"}}}}
  sizing: r_unit
  risk: seven_step
  fill: {{plugin: bar_sim_1m, params: {{decision_latency_ms: 5, maker_fee_pct: "0.0002"}}}}
""")
    register_host_fixtures(scheme, tmp_path)
    first, second = [read_journal(run_backtest(scheme, tmp_path).directory / "ledger.sqlite") for _ in range(2)]
    assert first == second
    [entry] = [e for e in first.events if e.kind == "ENTRY_FILLED"]
    assert (entry.ts_event, entry.price) == (start + 2 * MINUTE + 1, D(105))
    [liquidation] = [e for e in first.events if e.kind == "ROUND_LIQUIDATED"]
    [trade] = [e for e in first.events if e.kind == "EXIT_FILLED"]
    assert (liquidation.ts_event, liquidation.position.phase, trade.price) == (
        liquidated_at, "LIQUIDATED", D("69.9"),
    )
    cashflows = [e for e in first.events if e.kind == "FUNDING_SETTLED"]
    assert [(e.ts_event, e.settlement_ts) for e in cashflows] == [
        (settled_at, settled_at), (settled_at, settled_at),
    ]
    postings = {row.event_id: row.postings for row in first.entries}
    assert [
        next(p.amount for p in postings[e.event_id] if p.account == "CASH_AVAILABLE")
        for e in cashflows
    ] == [
        -entry.quantity * D(101) * D("0.00005"),
        -entry.quantity * D(101) * D("0.00001"),
    ]
    assert all(sum(p.amount for p in entry.postings) == 0 for entry in first.entries)


@pytest.mark.medium
@pytest.mark.regression
@pytest.mark.skipif(sys.platform != "darwin", reason="生产策略隔离使用 macOS")
def test_isolated_strategy_signal_fills_on_a_later_1m_bar(tmp_path, without_seatbelt):
    # trading/03 × trading/06：5m 信号触发策略，后续 1m 柱用于撮合。
    from talaria.account.strategy_code import StrategyCodeRegistry

    instrument = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    start = 1_793_491_200_000
    signal_at = start + 5 * MINUTE
    fill_at = signal_at + 2 * MINUTE

    def raw_bar(timeframe, ts, available, *, opening=100, high=100, low=100, close=100):
        return RawBar(
            symbol=instrument.symbol, timeframe=timeframe, ts_event=ts,
            open=D(opening), high=D(high), low=D(low), close=D(close), volume=D(100), turnover=D(10000),
            ts_available=available, ts_ingested=available, ts_committed=available,
        )

    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(instrument, "5m", [raw_bar("5m", start, signal_at)]))
        store.publish(ColdBatch(instrument, "1m", [
            raw_bar("1m", signal_at - MINUTE, signal_at),
            raw_bar("1m", fill_at - MINUTE, fill_at, opening=104, high=105, low=101, close=104),
        ]))
        marks = [derivative("mark_price", signal_at, "100"), derivative("mark_price", fill_at, "104")]
        store.publish_derivatives(instrument, marks[0].kind, marks)

    fixture = Path(__file__).parent / "fixtures" / "replay_strategy.py"
    strategy = tmp_path / fixture.name
    strategy.write_text(fixture.read_text().replace(
        "        bar = ctx.bars()[-1]",
        f'        assert ctx.timeframe == "5m" and ctx.now == {signal_at}\n'
        f'        assert [bar.ts_event for bar in ctx.bars("1m")] == [{signal_at - MINUTE}]\n'
        '        assert [point.value for point in ctx.derivatives("mark_price")] == [Decimal("100")]\n'
        "        bar = ctx.bars()[-1]",
    ).replace("take_profit=[bar.close + 2]", "take_profit=[bar.close + 20]"))
    StrategyCodeRegistry(tmp_path).register(strategy, "agent")
    scheme = tmp_path / "sim.yaml"
    scheme.write_text(f"""\
scheme: isolated-bar-sim
interface_version: {INTERFACE_VERSION}
sub_accounts: [one]
slots:
  data:
    plugin: parquet_replay
    params: {{instrument: {INSTRUMENT}, timeframe: 5m, start: {start}, end: {fill_at}}}
  strategy: {{python: {strategy.name}, params: {{trigger_price: "100"}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""")

    journal = read_journal(run_backtest(scheme, tmp_path).directory / "ledger.sqlite")
    assert [(event.ts_event, event.price) for event in journal.events if event.kind == "ENTRY_FILLED"] == [
        (fill_at - MINUTE + 1, D(102)),
    ]


def test_time_barrier_enters_exiting_without_using_a_bar_closed_before_deadline(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", max_holding_ms=MINUTE + 1)).accepted
    step(fill, market(2 * MINUTE))
    deadline = 4 * MINUTE + 1
    events = step(fill, MarketEvent(deadline, INSTRUMENT, "1m", None))
    assert [(e.kind, e.position.phase) for e in events] == [("ROUND_EXITING", "EXITING")]
    assert step(fill, market(3 * MINUTE, available=deadline + 1)) == []
    [trade] = [e for e in step(fill, market(4 * MINUTE, close="110")) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price) == ("MAX_HOLDING_TIME", D("104.9"))


def test_marked_account_equity_triggers_cross_liquidation(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0,
        {"one": AccountRiskConfig(initial_capital="25")},
    )
    fill = definition.create(context, {"liquidation_fee_pct": "0.01"})
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE), valuation)
    now = 3 * MINUTE + 1
    tick = derivative("mark_price", now, "75")
    events = step(fill, MarketEvent(now, INSTRUMENT, "1m", None, derivative_points=(tick,)), valuation)
    [trade] = [e for e in events if e.kind == "EXIT_FILLED"]
    assert (trade.price, trade.position.phase) == (D("74.9"), "LIQUIDATED")
    assert next(p.amount for p in trade.postings if p.account == "EXPENSE_LIQUIDATION_FEE") == D("0.749")


def test_post_only_resting_short_can_fill_when_a_later_bar_gaps_through_the_limit(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(
        order="POST_ONLY", qty="1", stop="110", targets=("80",), direction="SHORT",
    )).accepted
    assert step(fill, market(2 * MINUTE, open="94", high="95", low="92", close="93")) == []
    [trade] = [e for e in step(fill, market(
        3 * MINUTE, open="107", high="109", low="105", close="108",
    )) if e.kind == "ENTRY_FILLED"]
    assert (trade.reason, trade.price) == ("MAKER", D(95))
    [exit_fill] = [e for e in step(fill, market(
        4 * MINUTE, open="115", high="122", low="75", close="81",
    )) if e.kind == "EXIT_FILLED"]
    assert (exit_fill.reason, exit_fill.price) == ("STOP_LOSS", D("115.1"))


def test_liquidation_rechecks_the_mark_as_soon_as_an_entry_increases_exposure(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0, {"one": AccountRiskConfig(margin_mode="ISOLATED")},
    )
    fill = definition.create(context, {})
    assert fill.submit_plan(approved(qty="1")).accepted
    tick = derivative("mark_price", 3 * MINUTE, "70")
    events = step(fill, market(2 * MINUTE, points=(tick,)), CRYPTO_PERP_PACK.margin_valuation())
    assert [e.kind for e in events] == [
        "ENTRY_FILLED", "PLAN_COMPLETED", "ROUND_EXITING", "EXIT_FILLED", "ROUND_LIQUIDATED",
    ]
    assert events[-1].position.phase == "LIQUIDATED"


def open_through_gate(tmp_path, *, config=None):
    from talaria.account.execution import ExecutionGate
    from talaria.account.plugins.seven_step import SevenStepRisk
    from talaria.account.risk_config import AccountRiskConfig

    class FixedSizing:
        # 用固定数量替换定仓，保留真实风控、记账投影与成交。
        def size(self, candidate, state, valuation):
            return SizingDecision(quantity="2", basis="固定测试数量")

    config = config or AccountRiskConfig()
    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(tmp_path, "backtest", 7, lambda: 0, {"one": config})
    fill = definition.create(context, {})
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    recorded = []
    balance = {("USDT", "CASH_AVAILABLE"): config.initial_capital}

    def record(events):
        recorded.extend(events)
        for event in events:
            for posting in event.postings:
                key = posting.currency, posting.account
                balance[key] = balance.get(key, D(0)) + posting.amount

    gate = ExecutionGate(
        ("one",), FixedSizing(), SevenStepRisk(CRYPTO_PERP_PACK, {"one": config}), fill,
        balances=lambda _: balance, valuation=valuation, record=record, allow_late_events=True,
    )

    def drive(event):
        for points in fill.market_steps(event, valuation, balances=lambda _: balance):
            gate.poll_events(event.as_of, points=points)

    drive(market(0, points=(derivative("mark_price", MINUTE, "100"),)))
    assert gate.submit_candidate(approved().candidate).accepted
    drive(market(2 * MINUTE))
    return gate, drive, recorded


def test_funding_drawdown_cancels_pending_entry_before_same_step_matching(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    gate, drive, recorded = open_through_gate(tmp_path, config=AccountRiskConfig(
        daily_soft_drawdown_pct="0.0001", daily_hard_drawdown_pct="0.0002",
    ))
    point = derivative("funding_rate", 3 * MINUTE, "0.03", settled=True)
    drive(market(3 * MINUTE, points=(point,)))
    assert [event.kind for event in recorded] == [
        "ENTRY_FILLED", "FUNDING_SETTLED", "TRADING_STATE_CHANGED", "PLAN_CANCELLED",
    ]
    assert gate.account_state("one").trading_state == "HALTED"
    assert not gate.account_state("one").reservations


def test_queued_reduction_and_consecutive_barrier_changes_book_in_execution_order(tmp_path):
    from talaria.account.contracts import BarrierChangeRequest

    gate, _, recorded = open_through_gate(tmp_path)
    assert gate.intervene_position("round-1", "REDUCE_RATIO", D("0.5"), "USER").quantity == D("0.5")
    assert gate.change_barrier(BarrierChangeRequest(sub_account_id="one", round_id="round-1", stop="91")).accepted
    assert gate.change_barrier(BarrierChangeRequest(sub_account_id="one", round_id="round-1", stop="92")).accepted
    gate.poll_events(3 * MINUTE)
    assert [(event.kind, event.position.stop) for event in recorded] == [
        ("ENTRY_FILLED", D(90)),
        ("EXIT_FILLED", D(90)),
        ("BARRIER_TIGHTENED", D(91)),
        ("BARRIER_TIGHTENED", D(92)),
    ]


def test_manual_close_waits_when_latest_confirmation_reports_an_older_close(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE, available=3 * MINUTE + 5_000))
    status = fill.intervene_position("r", "CLOSE_ALL", None, "USER")
    assert (status.quantity, status.phase) == (D(1), "EXITING")
    [trade] = [e for e in step(fill, market(3 * MINUTE, close="110")) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price) == ("USER", D("109.9"))


def test_limit_remainder_rests_after_a_partial_taker_fill(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    first = step(fill, market(2 * MINUTE, open="98", low="97"))
    assert first[0].reason == "TAKER"
    [trade] = [e for e in step(fill, market(3 * MINUTE, open="98", low="97")) if e.kind == "ENTRY_FILLED"]
    assert (trade.reason, trade.price) == ("MAKER", D(98))
    assert next(p.amount for p in trade.postings if p.account == "EXPENSE_EXECUTION_FEE") == D("0.0196")


def test_liquidation_executes_at_an_adverse_price_with_taker_friction(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0, {"one": AccountRiskConfig(margin_mode="ISOLATED")},
    )
    fill = definition.create(context, {"spread_pct": "0.02"})
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE), valuation)
    at = 3 * MINUTE + 1
    tick = derivative("mark_price", at, "70")
    [trade] = [e for e in step(
        fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(tick,)), valuation,
    ) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price) == ("MARGIN_LIQUIDATION", D("69.2"))


@pytest.mark.parametrize(("lower", "opening", "highest", "lowest", "expected", "exit_reason", "exit_price"), [
    ("95", "98", "125", "97", "98", "TAKE_PROFIT", "120"),
    ("85", "88", "125", "87", "88", "STOP_LOSS", "87.9"),
    ("95", "88", "94", "87", "100", "STOP_LOSS", "87.9"),
])
def test_resting_entry_uses_crossed_open_and_keeps_approved_limit_through_full_gap(
    tmp_path, lower, opening, highest, lowest, expected, exit_reason, exit_price,
):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", low=lower)).accepted
    assert not step(fill, market(2 * MINUTE, low="101"))
    events = step(fill, market(3 * MINUTE, open=opening, high=highest, low=lowest, close=opening))
    [entry, trade] = [e for e in events if e.kind in {"ENTRY_FILLED", "EXIT_FILLED"}]
    assert (entry.price, entry.reason, entry.ts_event) == (D(expected), "MAKER", 3 * MINUTE)
    assert (trade.reason, trade.price) == (exit_reason, D(exit_price))


def test_penetrated_remainder_increases_the_loss_at_the_same_bar_stop(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    step(fill, market(2 * MINUTE))
    events = step(fill, market(3 * MINUTE, low="89", high="125", volume="30"))
    assert [(e.kind, e.quantity) for e in events if e.quantity] == [
        ("ENTRY_FILLED", D(1)), ("EXIT_FILLED", D(2)),
    ]
    trade = next(e for e in events if e.kind == "EXIT_FILLED")
    assert next(p.amount for p in trade.postings if p.account == "REALIZED_PNL") == D("20.2")


def test_profit_crossed_at_the_open_precedes_the_later_intrabar_stop(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE))
    [trade] = [e for e in step(fill, market(
        3 * MINUTE, open="125", high="126", low="85", close="100",
    )) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.ts_event) == ("TAKE_PROFIT", D(125), 3 * MINUTE)


def test_crossed_profit_precedes_holding_deadline(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(order="MARKET", qty="1", max_holding_ms=MINUTE)).accepted
    step(fill, market(2 * MINUTE, open="98", low="97"))
    [trade] = [e for e in step(
        fill, market(3 * MINUTE, high="125"),
    ) if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price) == ("TAKE_PROFIT", D(120))


def test_crossed_profit_still_executes_after_queued_partial_reduction(tmp_path):
    fill = engine(tmp_path, volume_participation="1")
    assert fill.submit_plan(approved()).accepted
    step(fill, market(2 * MINUTE, available=3 * MINUTE + 1))
    fill.intervene_position("r", "REDUCE_RATIO", D("0.5"), "USER")
    events = step(fill, market(4 * MINUTE, high="125"))
    assert [(e.reason, e.quantity) for e in events if e.kind == "EXIT_FILLED"] == [
        ("USER", D(1)), ("TAKE_PROFIT", D(1)),
    ]


def test_trailing_stop_can_execute_in_its_activation_bar(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1", targets=("150",), trailing_stop={
        "activation_pct": "0.1", "distance_pct": "0.05",
    })).accepted
    step(fill, market(2 * MINUTE))
    events = step(fill, market(3 * MINUTE, high="120", low="100", close="117"))
    [trade] = [e for e in events if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.position.stop) == ("TRAILING_STOP", D("113.9"), D(114))


def test_late_first_confirmation_can_stop_an_open_position(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE))
    step(fill, market(4 * MINUTE))
    delayed = market(3 * MINUTE, open="85", low="84", available=5 * MINUTE + 1)
    [trade] = [e for e in step(fill, delayed) if e.kind == "EXIT_FILLED"]
    assert (trade.ts_event, trade.price) == (3 * MINUTE, D("84.9"))
    assert not step(fill, delayed)


@pytest.mark.parametrize(("exit_participation", "volume"), [(None, "10"), ("0.05", "20")])
def test_stop_only_executes_up_to_bar_participation(tmp_path, exit_participation, volume):
    fill = engine(tmp_path, exit_volume_participation=exit_participation)
    assert fill.submit_plan(approved()).accepted
    step(fill, market(2 * MINUTE, volume="20"))
    [first] = [e for e in step(fill, market(3 * MINUTE, low="89", volume=volume)) if e.kind == "EXIT_FILLED"]
    assert (first.quantity, first.position.quantity, first.position.phase) == (D(1), D(1), "EXITING")
    [second] = [e for e in step(
        fill, market(4 * MINUTE, open="105", close="109", volume=volume),
    ) if e.kind == "EXIT_FILLED"]
    assert (second.quantity, second.price, second.reason) == (D(1), D("104.9"), "STOP_LOSS")


def test_open_fill_beats_intrabar_expiry_and_sets_holding_deadline(tmp_path):
    fill = engine(tmp_path)
    plan = approved(order="MARKET", qty="1", max_holding_ms=MINUTE)
    plan = plan.model_copy(update={"candidate": plan.candidate.model_copy(update={
        "valid_until": 2 * MINUTE + 1,
    })})
    assert fill.submit_plan(plan).accepted
    [entry] = [e for e in step(fill, market(2 * MINUTE, open="98", low="97")) if e.kind == "ENTRY_FILLED"]
    assert entry.ts_event == 2 * MINUTE
    [trade] = [e for e in step(fill, market(3 * MINUTE, close="109")) if e.kind == "EXIT_FILLED"]
    assert (trade.ts_event, trade.price, trade.reason) == (3 * MINUTE, D("104.9"), "MAX_HOLDING_TIME")


@pytest.mark.parametrize(("low", "reason", "price", "at"), [
    ("89", "STOP_LOSS", "89.9", 3 * MINUTE + 1),
    ("98", "DRAWDOWN_LIMIT", "104.9", 4 * MINUTE),
])
def test_drawdown_close_uses_hard_stop_or_the_next_executable_open(tmp_path, low, reason, price, at):
    from talaria.account.risk_config import AccountRiskConfig

    _, drive, recorded = open_through_gate(tmp_path, config=AccountRiskConfig(
        daily_soft_drawdown_pct="0.0001", daily_hard_drawdown_pct="0.0002",
        liquidate_on_halt=True,
    ))
    point = derivative("funding_rate", 3 * MINUTE + 1, "0.03", settled=True)
    drive(market(3 * MINUTE, low=low, close="109", points=(point,)))
    drive(market(4 * MINUTE, close="108"))
    [trade] = [e for e in recorded if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.ts_event) == (reason, D(price), at)


def test_every_mark_in_a_batch_is_checked_in_observation_order(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0, {"one": AccountRiskConfig(margin_mode="ISOLATED")},
    )
    fill = definition.create(context, {})
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE), valuation)
    at = 3 * MINUTE + 100
    low = derivative("mark_price", at, "70", available=at + 10)
    recovery = derivative("mark_price", at + 1, "105", available=at + 10)
    event = MarketEvent(at + 10, INSTRUMENT, "1m", None, derivative_points=(recovery, low))
    [trade] = [e for e in step(fill, event, valuation) if e.kind == "EXIT_FILLED"]
    assert (trade.ts_event, trade.reason, trade.price) == (at, "MARGIN_LIQUIDATION", D("69.9"))


def test_late_settlement_only_charges_the_position_held_at_settlement(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    at = 3 * MINUTE - 1
    mark = derivative("mark_price", at, "100")
    step(fill, market(2 * MINUTE, points=(mark,)))
    step(fill, market(3 * MINUTE))
    settlement = derivative("funding_rate", at, "0.01", available=4 * MINUTE + 1, settled=True)
    [flow] = step(fill, MarketEvent(settlement.ts_available, INSTRUMENT, "1m", None, derivative_points=(settlement,)))
    assert (flow.kind, flow.ts_event, flow.settlement_ts, flow.position.quantity) == (
        "FUNDING_SETTLED", at, at, D(2),
    )
    assert next(p.amount for p in flow.postings if p.account == "CASH_AVAILABLE") == D(-1)


def test_late_bar_fill_backfills_the_held_quantity_for_a_known_settlement(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(order="MARKET", qty="1")).accepted
    at = 2 * MINUTE + 10
    mark = derivative("mark_price", at, "100")
    settlement = derivative("funding_rate", at, "0.01", settled=True)
    assert step(fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(settlement, mark))) == []
    events = step(fill, market(2 * MINUTE, open="98", low="97"))
    [flow] = [e for e in events if e.kind == "FUNDING_SETTLED"]
    assert flow.ts_event == at
    assert next(p.amount for p in flow.postings if p.account == "CASH_AVAILABLE") == D(-1)


def test_settlement_uses_a_same_batch_mark_arriving_after_its_observation(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE, points=(derivative("mark_price", 2 * MINUTE, "100"),)))
    at = 3 * MINUTE + 10
    settle = derivative("funding_rate", at, "0.01", available=at + 20, settled=True)
    delayed_mark = derivative("mark_price", at, "110", available=at + 15)
    future_mark = derivative("mark_price", at + 1, "120", available=at + 20)
    event = MarketEvent(at + 20, INSTRUMENT, "1m", None, derivative_points=(settle, delayed_mark, future_mark))
    [flow] = step(fill, event)
    assert flow.kind == "FUNDING_SETTLED"
    assert next(p.amount for p in flow.postings if p.account == "CASH_AVAILABLE") == D("-1.10")


def test_unpriced_settlement_is_booked_once_after_its_mark_arrives(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE))
    at = 3 * MINUTE + 10
    settle = derivative("funding_rate", at, "0.01", settled=True)
    [warning] = step(fill, MarketEvent(at, INSTRUMENT, "1m", None, derivative_points=(settle,)))
    assert (warning.kind, warning.settlement_ts) == ("FUNDING_UNPRICED", at)
    mark = derivative("mark_price", at, "110", available=at + 100)
    event = MarketEvent(at + 100, INSTRUMENT, "1m", None, derivative_points=(settle, mark))
    [flow] = step(fill, event)
    assert (flow.kind, flow.ts_event) == ("FUNDING_SETTLED", at)
    assert next(p.amount for p in flow.postings if p.account == "CASH_AVAILABLE") == D("-1.10")
    assert not step(fill, event)


@pytest.mark.medium
def test_replay_risk_uses_last_visible_funding_estimate_from_before_start(tmp_path, register_host_fixtures):
    instrument = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    start = 1_793_491_200_000
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(instrument, "1m", [
            RawBar(
                symbol=instrument.symbol, timeframe="1m", ts_event=start,
                open=D(100), high=D(101), low=D(99), close=D(100), volume=D(10), turnover=D(1000),
                ts_available=start + MINUTE, ts_ingested=start + MINUTE, ts_committed=start + MINUTE,
            ),
        ]))
        mark = derivative("mark_price", start - 1, "100")
        store.publish_derivatives(instrument, mark.kind, [mark])
        estimate = derivative("funding_rate", start - 5, "0.1", settled=False)
        future = derivative("funding_rate", start - 2, "0", settled=False, available=start + 2 * MINUTE)
        store.publish_derivatives(instrument, estimate.kind, [estimate, future])
    strategy = Path(__file__).parent / "fixtures" / "replay_strategy.py"
    (tmp_path / strategy.name).write_text(strategy.read_text().replace(
        "invalidation_stop=bar.close - 2, take_profit=[bar.close + 2]",
        "invalidation_stop=bar.close - 2, take_profit=[bar.close + 20]",
    ))
    scheme = tmp_path / "sim.yaml"
    scheme.write_text(f"""
scheme: funding-warmup
interface_version: {INTERFACE_VERSION}
sub_accounts: [one]
slots:
  data:
    plugin: parquet_replay
    params: {{instrument: {INSTRUMENT}, timeframe: 1m, start: {start}, end: {start + MINUTE}}}
  strategy: {{python: {strategy.name}, params: {{trigger_price: "100"}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""")
    register_host_fixtures(scheme, tmp_path)
    journal = read_journal(run_backtest(scheme, tmp_path).directory / "ledger.sqlite")
    assert [(e.kind, e.reason) for e in journal.events if e.kind != "ACCOUNT_FUNDED"] == [
        ("CANDIDATE_REJECTED", "NET_REWARD_RISK_TOO_LOW"),
    ]


@pytest.mark.medium
@pytest.mark.parametrize("late_mark", [False, True])
def test_parquet_replay_executes_late_stop_before_a_newer_confirmed_profit(
    tmp_path, register_host_fixtures, late_mark,
):
    instrument = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    start = 1_793_491_200_000
    end = start + 6 * MINUTE
    rows = [
        (0, 1, "100", "101", "99", "100"),
        (2, 3, "110", "112", "100", "110"),
        (3, 6, "85", "110", "80", "105"),
        (4, 5, "110", "140", "100", "110"),
    ]
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(instrument, "1m", [
            RawBar(
                symbol=instrument.symbol, timeframe="1m",
                ts_event=start + slot * MINUTE, ts_available=start + available * MINUTE,
                ts_ingested=start + available * MINUTE, ts_committed=start + available * MINUTE,
                open=D(opening), high=D(high), low=D(low), close=D(close),
                volume=D(100), turnover=D(10000),
            )
            for slot, available, opening, high, low, close in rows
        ]))
        if late_mark:
            marks = [
                derivative("mark_price", start + 1, "103", available=start + 2 * MINUTE),
                derivative("mark_price", start + 20, "105"),
            ]
        else:
            marks = [derivative("mark_price", start, "105")]
        store.publish_derivatives(instrument, marks[0].kind, marks)
    strategy = Path(__file__).parent / "fixtures" / "replay_strategy.py"
    (tmp_path / strategy.name).write_text(strategy.read_text().replace(
        "entry_high=bar.close + 2", "entry_high=bar.close + 5",
    ).replace("invalidation_stop=bar.close - 2, take_profit=[bar.close + 2]",
              "invalidation_stop=bar.close - 10, take_profit=[bar.close + 35]"))
    scheme = tmp_path / "sim.yaml"
    scheme.write_text(f"""
scheme: late-stop
interface_version: {INTERFACE_VERSION}
sub_accounts: [one]
slots:
  data:
    plugin: parquet_replay
    params: {{instrument: {INSTRUMENT}, timeframe: 1m, start: {start}, end: {end}}}
  strategy: {{python: {strategy.name}, params: {{trigger_price: "100"}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""")
    register_host_fixtures(scheme, tmp_path)
    journal = read_journal(run_backtest(scheme, tmp_path).directory / "ledger.sqlite")
    [trade] = [e for e in journal.events if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.ts_event) == ("STOP_LOSS", D("84.9"), start + 3 * MINUTE)


def test_mark_revision_does_not_erase_an_earlier_liquidation_observation(tmp_path):
    from talaria.account.risk_config import AccountRiskConfig

    definition = default_registry().resolve(Slot.FILL, "bar_sim_1m", INTERFACE_VERSION, tmp_path)
    context = PluginContext(
        tmp_path, "backtest", 7, lambda: 0, {"one": AccountRiskConfig(margin_mode="ISOLATED")},
    )
    fill = definition.create(context, {})
    valuation = CRYPTO_PERP_PACK.margin_valuation()
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE), valuation)
    at = 3 * MINUTE + 100
    mark = derivative("mark_price", at, "70", available=at + 10)
    corrected = derivative("mark_price", at, "105", available=at + 20, revision=1)
    event = MarketEvent(at + 20, INSTRUMENT, "1m", None, derivative_points=(corrected, mark))
    [trade] = [e for e in step(fill, event, valuation) if e.kind == "EXIT_FILLED"]
    assert (trade.ts_event, trade.price) == (at, D("69.9"))


def test_late_confirmed_close_uses_a_stop_tightened_during_the_bar(tmp_path):
    from talaria.account.contracts import BarrierChangeRequest

    fill = engine(tmp_path)
    assert fill.submit_plan(approved(qty="1")).accepted
    step(fill, market(2 * MINUTE))
    at = 3 * MINUTE + 10
    step(fill, MarketEvent(at, INSTRUMENT, "1m", None))
    assert fill.tighten_barrier(BarrierChangeRequest(sub_account_id="one", round_id="r", stop="95")).accepted
    events = step(fill, market(3 * MINUTE, low="92", close="94"))
    [trade] = [e for e in events if e.kind == "EXIT_FILLED"]
    assert (trade.reason, trade.price, trade.ts_event) == ("STOP_LOSS", D("93.9"), 4 * MINUTE)


def test_holding_deadline_cancels_the_remainder_before_an_open_after_a_gap(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved(max_holding_ms=MINUTE)).accepted
    step(fill, market(2 * MINUTE))
    events = step(fill, market(4 * MINUTE, open="98", low="97", volume="20"))
    assert [(e.kind, e.quantity, e.price) for e in events if e.quantity] == [
        ("EXIT_FILLED", D(1), D("97.9")),
    ]
    assert any(e.kind == "PLAN_CANCELLED" and e.reason == "MAX_HOLDING_TIME" for e in events)


def test_checkpoint_resumes_partial_order_and_preserves_closed_round_funding_timeline(tmp_path):
    fill = engine(tmp_path)
    assert fill.submit_plan(approved()).accepted
    [first] = step(fill, market(2 * MINUTE))
    assert first.position.quantity == 1
    recovered = engine(tmp_path)
    recovered.restore_checkpoint(fill.checkpoint())
    assert step(recovered, market(2 * MINUTE)) == []
    remaining = step(recovered, market(3 * MINUTE))
    assert [(event.kind, event.position.quantity) for event in remaining] == [
        ("ENTRY_FILLED", D(2)), ("PLAN_COMPLETED", D(2))]
    step(recovered, market(4 * MINUTE, low="85", volume="30"))
    after_close = engine(tmp_path)
    after_close.restore_checkpoint(recovered.checkpoint())
    points = (derivative("mark_price", 3 * MINUTE, "100"), derivative("funding_rate", 3 * MINUTE, ".01", settled=True))
    event = MarketEvent(6 * MINUTE, INSTRUMENT, "1m", None, derivative_points=points)
    [flow] = step(after_close, event)
    assert flow.kind == "FUNDING_SETTLED" and flow.position.quantity == 0
    assert [flow] == step(recovered, event)


def test_flat_legacy_journal_restores_holdings_for_a_late_settlement(tmp_path):
    original = engine(tmp_path)
    original.submit_plan(approved(qty="1"))
    events = step(original, market(2 * MINUTE)) + step(original, market(4 * MINUTE, low="85"))
    restored = engine(tmp_path)
    restored.restore_closed_history(events)
    points = (derivative("mark_price", 3 * MINUTE, "100"), derivative("funding_rate", 3 * MINUTE, ".01", settled=True))
    [flow] = step(restored, MarketEvent(6 * MINUTE, INSTRUMENT, "1m", None, derivative_points=points))
    assert (flow.kind, flow.position.round_id, flow.position.quantity) == ("FUNDING_SETTLED", "r", 0)
    assert next(p.amount for p in flow.postings if p.account == "CASH_AVAILABLE") == D("-1")
