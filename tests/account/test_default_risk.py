"""trading/02 / ADR-0015：默认定仓、风控及其账户进程接线。"""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import replace
from decimal import Decimal as D
from threading import Event

import pytest

from talaria.account.contracts import (
    AccountState, ApprovedExecutionPlan, BarrierChangeRequest, DecisionOrigin, EntryCandidate,
    ExecutionEvent, ExitBarrierSpec, LedgerPosting, PlanReceipt, RoundExposure, RoundStatus,
    SizingDecision, TradingState,
)
from talaria.account.execution import ExecutionGate
from talaria.account.plugins.r_unit import RUnitSizer
from talaria.account.plugins.seven_step import SevenStepRisk
from talaria.account.risk_config import AccountRiskConfig, QuietWindow, TrailingStopSpec
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.margin import MarginPosition
from talaria.foundation.market import SessionTrait, TradingSession
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

pytestmark = [pytest.mark.small, pytest.mark.spec]

BTC = "BTC-USDT-PERP"
ETH = "ETH-USDT-PERP"
PACK = CRYPTO_PERP_PACK
VALUATION = PACK.margin_valuation()


def candidate(**changes):
    return EntryCandidate(**({
        "sub_account_id": "one", "instrument_id": BTC, "direction": "LONG",
        "entry_low": "99", "entry_high": "100", "invalidation_stop": "90",
        "take_profit": ("130",), "origin": DecisionOrigin.HUMAN, "ts_decision": 60_000,
    } | changes))


def point(instrument=BTC, value="100", now=60_000, kind=DerivativeKind.MARK_PRICE):
    return DerivativePoint(
        instrument_id=instrument, kind=kind, value=value, ts_event=now,
        ts_available=now, ts_ingested=now, ts_committed=now,
    )


def account(**changes):
    return AccountState(**({
        "sub_account_id": "one", "balances": {("USDT", "CASH_AVAILABLE"): D("10000")},
        "rounds": {}, "trading_state": TradingState.ACTIVE, "as_of": 60_000,
        "marks": {BTC: point()},
    } | changes))


@pytest.mark.parametrize(("entry", "expected"), [
    (candidate(), "4.901"),
    (candidate(direction="SHORT", invalidation_stop="110", take_profit=("70",)), "4.465"),
])
def test_r_unit_uses_worst_entry_friction_and_rounds_down(entry, expected):
    sizer = RUnitSizer(PACK, {"one": AccountRiskConfig(friction_buffer_pct="0.002")})
    decision = sizer.size(entry, account(), VALUATION)
    assert decision.quantity == D(expected)
    assert decision.basis


def test_preview_reports_liquidation_buffer_without_mutating_account():
    state = account()
    sizer = RUnitSizer(PACK, {"one": AccountRiskConfig(margin_mode="ISOLATED", leverage=5)})
    preview = sizer.preview_sizing(candidate(), state, VALUATION)
    assert preview.sizing == sizer.size(candidate(), state, VALUATION)
    assert preview.liquidation_price.quantize(D("0.1")) == D("80.4")
    assert preview.liquidation_distance.quantize(D("0.1")) == D("9.6")
    assert not state.rounds


def evaluate(entry=None, state=None, quantity="4", **params):
    risk = SevenStepRisk(PACK, {"one": AccountRiskConfig(**params)})
    return risk.evaluate_candidate(
        candidate() if entry is None else entry,
        SizingDecision(quantity=quantity, basis="定仓插槽指定数量"),
        account() if state is None else state,
        VALUATION,
    )


@pytest.mark.parametrize(("entry", "state", "reason"), [
    (candidate(), account(trading_state=TradingState.REDUCING), "TRADING_STATE_BLOCKED"),
    (candidate(invalidation_stop="99"), account(), "INVALID_STOP_GEOMETRY"),
])
def test_step_one_requires_active_account_and_valid_stop(entry, state, reason):
    assert evaluate(entry, state).reason == reason


def test_step_two_checks_the_quantity_from_sizing_slot():
    assert evaluate(quantity="10").reason == "MAX_SINGLE_R_EXCEEDED"


def test_step_three_counts_pending_correlation_cluster_risk():
    reserved = RoundExposure("eth", MarginPosition(ETH, "LONG", D(8), D(100)), D(90))
    state = account(reservations=(reserved,), marks={BTC: point(), ETH: point(ETH)})
    params = {"correlation_clusters": {BTC: "majors", ETH: "majors"}, "cluster_max_r": 2}
    assert evaluate(state=state, **params).reason == "CLUSTER_R_EXCEEDED"


def test_step_four_observes_event_quiet_windows():
    assert evaluate(quiet_windows=[QuietWindow(start=59_000, end=61_000)]).reason == "EVENT_QUIET_WINDOW"


def test_step_four_rejects_off_grid_order_quantity():
    assert evaluate(quantity="4.0001").reason == "ORDER_CONSTRAINT"


def test_step_five_requires_stop_before_isolated_liquidation():
    entry = candidate(invalidation_stop="70", take_profit=("180",))
    assert evaluate(entry, quantity="1", leverage=5, margin_mode="ISOLATED").reason == "MARGIN_OR_LIQUIDATION"


def test_step_five_never_uses_a_future_or_missing_mark():
    for marks in ({}, {BTC: point(now=60_001)}, {BTC: point(now=0)}):
        assert evaluate(state=account(marks=marks), mark_max_age_ms=59_999).reason == "MARGIN_VALUATION_UNAVAILABLE"


def test_step_six_deducts_fees_slippage_and_adverse_funding():
    entry = candidate(take_profit=("116.1",))
    costs = {"taker_fee_pct": "0.001", "slippage_pct": "0.001", "funding_rate_buffer": 0}
    assert isinstance(evaluate(entry, **costs), ApprovedExecutionPlan)
    rate = point(value="0.004", kind=DerivativeKind.FUNDING_RATE)
    assert evaluate(entry, account(funding_rates={BTC: rate}), **costs).reason == "NET_REWARD_RISK_TOO_LOW"


def test_step_seven_issues_immutable_plan_with_original_sizing():
    entry = candidate()
    plan = evaluate(entry)
    assert isinstance(plan, ApprovedExecutionPlan)
    assert plan.candidate == entry
    assert plan.sizing == SizingDecision(quantity="4", basis="定仓插槽指定数量")
    assert plan.round_id and plan.plan_id
    assert plan.barriers.stop == D(90) and plan.barriers.take_profit == (D(130),)
    with pytest.raises(ValueError, match="frozen"):
        plan.round_id = "changed"


def open_account(*, stop="90", quantity="4", mark="100", reservations=()):
    position = RoundStatus(
        round_id="active", sub_account_id="one", instrument_id=BTC, direction="LONG",
        quantity=quantity, stop=stop,
    )
    exposure = RoundExposure("active", MarginPosition(BTC, "LONG", D(quantity), D(100)), D(stop))
    return account(
        rounds={"active": position}, positions=(exposure,), reservations=reservations,
        barriers={"active": ExitBarrierSpec(
            stop=stop, take_profit=(D(130),), max_holding_ms=123_000,
            trailing_stop=TrailingStopSpec(activation_pct="0.02", distance_pct="0.01"),
        )},
        marks={BTC: point(value=mark)},
    )


def test_opposite_candidate_is_rejected_with_open_round_code():
    entry = candidate(direction="SHORT", invalidation_stop="110", take_profit=("70",))
    assert evaluate(entry, open_account()).reason == "OPPOSITE_POSITION_OPEN"


def test_add_on_reuses_round_barriers_and_counts_total_risk():
    entry = candidate(round_id="active", take_profit=("160",))
    state = open_account()
    approved = evaluate(entry, state)
    assert approved.round_id == "active" and approved.barriers == state.barriers["active"]
    assert evaluate(entry, state, quantity="6").reason == "MAX_SINGLE_R_EXCEEDED"
    assert evaluate(candidate(round_id="active", invalidation_stop="91"), state).reason == "ADD_ON_STOP_MISMATCH"


def test_stop_past_breakeven_releases_cluster_budget():
    state = open_account(stop="101", mark="110", quantity="40")
    state = account(
        rounds=state.rounds, positions=state.positions, barriers=state.barriers,
        marks={BTC: point(value="110"), ETH: point(ETH)},
    )
    params = {"correlation_clusters": {BTC: "majors", ETH: "majors"}, "cluster_max_r": 1}
    assert isinstance(evaluate(candidate(instrument_id=ETH), state, **params), ApprovedExecutionPlan)


def test_barrier_widening_rechecks_total_risk_and_returns_approval_only_if_safe():
    risk = SevenStepRisk(PACK, {"one": AccountRiskConfig()})
    state = open_account()
    request = BarrierChangeRequest(sub_account_id="one", round_id="active", stop="85")
    assert risk.approve_barrier_widening(request, state, VALUATION).request == request
    too_far = request.model_copy(update={"stop": D(70)})
    assert risk.approve_barrier_widening(too_far, state, VALUATION).reason == "MAX_SINGLE_R_EXCEEDED"


class HoldingFill:
    """可阻塞提交、排队成交的成交插槽替身。"""

    def __init__(self):
        self.plans = []
        self.events = []
        self.states = {}
        self.interventions = []
        self.positions = {}

    def submit_plan(self, plan):
        self.plans.append(plan)
        return PlanReceipt(accepted=True)

    def poll_events(self, now):
        events, self.events = self.events, []
        return events

    def set_trading_state(self, account, state, reason):
        self.states[account] = state

    def intervene_position(self, round_id, action, ratio, reason):
        self.interventions.append((round_id, action))
        plan = next(plan for plan in self.plans if plan.round_id == round_id)
        position = RoundStatus(
            round_id=round_id, sub_account_id=plan.candidate.sub_account_id,
            instrument_id=plan.candidate.instrument_id, direction=plan.candidate.direction,
            quantity=0, stop=plan.barriers.stop,
        )
        if action == "CANCEL_ENTRY":
            return self.positions.get(round_id, position)
        return position

    def entry_fill(self, plan, quantity, now):
        cost = quantity * D(100)
        position = RoundStatus(
            round_id=plan.round_id, sub_account_id=plan.candidate.sub_account_id,
            instrument_id=plan.candidate.instrument_id, direction=plan.candidate.direction,
            quantity=quantity, stop=plan.barriers.stop,
        )
        self.positions[plan.round_id] = position
        self.events.append(ExecutionEvent(
            kind="ENTRY_FILLED", sub_account_id=plan.candidate.sub_account_id, ts_event=now,
            plan_id=plan.plan_id, quantity=quantity, price=100,
            position=position,
            postings=(
                LedgerPosting(account="POSITION_COST", currency="USDT", amount=cost),
                LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount=-cost),
            ),
        ))


class NotionalSizer:
    """替换 R 定仓插槽，按固定名义金额给出数量。"""

    def __init__(self, notional):
        self.notional = D(notional)

    def size(self, entry, state, valuation):
        return SizingDecision(quantity=self.notional / entry.entry_high, basis="固定名义金额")


def test_concurrent_plans_quote_against_reserved_risk_and_release_on_cancel():
    fill = HoldingFill()
    entered, release = Event(), Event()

    def submit(plan):
        fill.plans.append(plan)
        entered.set()
        assert release.wait(5)
        return PlanReceipt(accepted=True)

    fill.submit_plan = submit
    configs = {"one": AccountRiskConfig(correlation_clusters={BTC: "majors", ETH: "majors"}, cluster_max_r=1)}
    risk = SevenStepRisk(PACK, configs)
    gate = ExecutionGate(("one",), NotionalSizer(400), risk, fill, balances=lambda _: account().balances,
                         valuation=VALUATION)
    gate.observe_market(60_000, (point(), point(ETH)))
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(gate.submit_candidate, candidate())
        assert entered.wait(5)
        second = workers.submit(gate.submit_candidate, candidate(instrument_id=ETH))
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=0.05)
        finally:
            release.set()
        assert first.result().accepted
        assert second.result().reason == "CLUSTER_R_EXCEEDED"
    gate.intervene_position(fill.plans[0].round_id, "CANCEL_ENTRY", None, "test")
    assert gate.submit_candidate(candidate(instrument_id=ETH)).accepted


def test_partially_filled_plan_reserves_only_remaining_quantity_until_terminal_event():
    fill = HoldingFill()
    risk = SevenStepRisk(PACK, {})
    gate = ExecutionGate(("one",), NotionalSizer(400), risk, fill, balances=lambda _: account().balances,
                         valuation=VALUATION)
    gate.observe_market(60_000, (point(),))
    assert gate.submit_candidate(candidate()).accepted
    plan, = fill.plans
    fill.entry_fill(plan, D(2), 60_001)
    gate.poll_events(60_001)
    state = gate.account_state("one")
    assert state.positions[0].position.quantity == D(2)
    assert state.reservations[0].position.quantity == D(2)
    fill.events.append(ExecutionEvent(
        kind="PLAN_EXPIRED", sub_account_id="one", ts_event=60_002, plan_id=plan.plan_id,
    ))
    gate.poll_events(60_002)
    assert not gate.account_state("one").reservations
    assert gate.account_state("one").positions[0].position.quantity == D(2)


def test_swapping_only_sizing_slot_still_rejects_excessive_notional_and_records_reason():
    risk = SevenStepRisk(PACK, {})
    fill = HoldingFill()
    recorded = []
    for sizing in (RUnitSizer(PACK, {}), NotionalSizer(1100)):
        gate = ExecutionGate(("one",), sizing, risk, fill, balances=lambda _: account().balances,
                             valuation=VALUATION, record=recorded.extend)
        gate.observe_market(60_000, (point(),))
        receipt = gate.submit_candidate(candidate())
        gate.poll_events(60_000)
    assert len(fill.plans) == 1
    assert receipt.reason == "MAX_SINGLE_R_EXCEEDED"
    event, = recorded
    assert (event.kind, event.reason) == ("CANDIDATE_REJECTED", receipt.reason)


def test_mark_price_drawdown_transitions_respect_account_isolation_and_close_on_halt():
    fill = HoldingFill()
    configs = {"one": AccountRiskConfig(
        daily_soft_drawdown_pct="0.005", daily_hard_drawdown_pct="0.01",
        liquidate_on_halt=True,
    )}
    risk = SevenStepRisk(PACK, configs)
    gate = ExecutionGate(("one", "two"), NotionalSizer(400), risk, fill, balances=lambda _: account().balances,
                         valuation=VALUATION)
    gate.observe_market(60_000, (point(),))
    assert gate.submit_candidate(candidate()).accepted
    plan, = fill.plans
    fill.entry_fill(plan, D(3), 60_001)
    gate.poll_events(60_001)
    gate.observe_market(60_002, (point(value="83", now=60_002),))
    assert gate.account_state("one").trading_state == TradingState.REDUCING
    assert gate.account_state("two").trading_state == TradingState.ACTIVE
    assert (plan.round_id, "CANCEL_ENTRY") in fill.interventions
    gate.observe_market(60_003, (point(value="66", now=60_003),))
    assert gate.account_state("one").trading_state == TradingState.HALTED
    assert fill.interventions[-1] == (plan.round_id, "CLOSE_ALL")


def test_cross_stress_checks_other_rounds_at_their_stops():
    other = RoundStatus(
        round_id="eth", sub_account_id="one", instrument_id=ETH, direction="LONG",
        quantity="98", stop="1",
    )
    held = RoundExposure("eth", MarginPosition(ETH, "LONG", D(98), D(100)), D(1))
    state = account(rounds={"eth": other}, positions=(held,), marks={BTC: point(), ETH: point(ETH)})
    entry = candidate(invalidation_stop="10", take_profit=("250",))
    assert evaluate(entry, state, quantity="4", max_single_r_pct="0.1", cluster_max_r=10).reason == (
        "MARGIN_OR_LIQUIDATION"
    )


def test_projected_unfilled_profit_does_not_fund_pending_margin():
    state = account(balances={("USDT", "CASH_AVAILABLE"): D(100)}, marks={BTC: point(value="200")})
    assert evaluate(
        state=state, quantity="1", initial_capital=100, leverage=1,
        max_single_r_pct="0.5", cluster_max_r=30,
    ).reason == "MARGIN_OR_LIQUIDATION"


def test_ledger_loss_can_trip_hard_drawdown_without_a_new_mark():
    fill = HoldingFill()
    cash = D(10000)

    def record(events):
        nonlocal cash
        for event in events:
            cash += sum(posting.amount for posting in event.postings if posting.account == "CASH_AVAILABLE")

    risk = SevenStepRisk(PACK, {})
    gate = ExecutionGate(("one",), NotionalSizer(400), risk, fill,
                         balances=lambda _: {("USDT", "CASH_AVAILABLE"): cash},
                         valuation=VALUATION, record=record)
    gate.observe_market(60_000, (point(),))
    fill.events.append(ExecutionEvent(
        kind="FUNDING_SETTLED", sub_account_id="one", ts_event=60_001,
        postings=(
            LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount="-1000"),
            LedgerPosting(account="FUNDING", currency="USDT", amount="1000"),
        ),
    ))
    gate.poll_events(60_001)
    assert cash == D(9000)
    assert gate.account_state("one").trading_state == TradingState.HALTED


def test_step_four_checks_market_session():
    class LimitedSession:
        """替换市场包的时段，保留真实标的规格。"""

        instrument = PACK.instrument

        def traits(self, instrument):
            session = SessionTrait("segmented", timezone="UTC", sessions=(TradingSession("day", "09:00", "16:00"),))
            return replace(PACK.traits(instrument), session=session)

    risk = SevenStepRisk(LimitedSession(), {})
    decision = risk.evaluate_candidate(
        candidate(), SizingDecision(quantity=4, basis="测试数量"), account(), VALUATION,
    )
    assert decision.reason == "SESSION_CLOSED"


def test_pending_round_blocks_opposite_direction_before_first_fill():
    fill = HoldingFill()
    gate = ExecutionGate(("one",), RUnitSizer(PACK, {}), SevenStepRisk(PACK, {}), fill,
                         balances=lambda _: account().balances, valuation=VALUATION)
    gate.observe_market(60_000, (point(),))
    assert gate.submit_candidate(candidate()).accepted
    opposite = candidate(direction="SHORT", invalidation_stop="110", take_profit=("70",))
    assert gate.submit_candidate(opposite).reason == "OPPOSITE_POSITION_OPEN"
    assert len(fill.plans) == 1


def test_peak_drawdown_survives_day_roll_and_never_reactivates_account():
    fill = HoldingFill()
    cash = D(10000)
    risk = SevenStepRisk(PACK, {})
    gate = ExecutionGate(("one",), NotionalSizer(400), risk, fill,
                         balances=lambda _: {("USDT", "CASH_AVAILABLE"): cash},
                         valuation=VALUATION)
    gate.observe_market(60_000, ())
    cash = D(12000)
    gate.observe_market(120_000, ())
    cash = D(9600)
    gate.observe_market(86_400_000, ())
    assert gate.account_state("one").trading_state == TradingState.HALTED
    cash = D(12000)
    gate.observe_market(86_460_000, ())
    assert gate.account_state("one").trading_state == TradingState.HALTED
