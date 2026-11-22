"""trading/01 与 ADR-0015 的止损路由、回合和获批数量契约。"""

from decimal import Decimal

import pytest

from talaria.account.contracts import (
    ApprovedBarrierAmendment,
    ApprovedExecutionPlan,
    BarrierChangeRequest,
    BarrierReceipt,
    EntryCandidate,
    ExecutionEvent,
    LedgerPosting,
    PlanReceipt,
    RejectedPlanRecord,
    RoundStatus,
    SizingDecision,
)
from talaria.account.execution import AccountProcessError, ExecutionGate

pytestmark = [pytest.mark.small, pytest.mark.spec]


class TestSlots:
    """用同步测试插件替代定仓、风控与成交。"""

    __test__ = False

    def __init__(self):
        self.allow_widening = False
        self.allowed_slippage = None
        self.stop_requests = []
        self.events = []

    def size(self, candidate, account, valuation):
        return SizingDecision(quantity="2", basis="固定测试数量")

    def evaluate_candidate(self, candidate, sizing, account, valuation):
        return ApprovedExecutionPlan(
            plan_id=f"plan-{candidate.ts_decision}", round_id="round",
            candidate=candidate, sizing=sizing, allowed_slippage=self.allowed_slippage,
        )

    def approve_barrier_widening(self, request, account, valuation):
        if self.allow_widening:
            return ApprovedBarrierAmendment(amendment_id="approved-stop", request=request)
        return RejectedPlanRecord(reason="RISK_LIMIT")

    def submit_plan(self, plan):
        return PlanReceipt(accepted=True)

    def tighten_barrier(self, request):
        self.stop_requests.append(request)
        return BarrierReceipt(accepted=True)

    def apply_approved_amendment(self, amendment):
        self.stop_requests.append(amendment)
        return BarrierReceipt(accepted=True)

    def intervene_position(self, round_id, action, ratio, reason):
        return RoundStatus(
            round_id=round_id, sub_account_id="example", instrument_id="BTC-USDT-PERP",
            direction="LONG", quantity="0", stop="9",
        )

    def poll_events(self, now):
        events, self.events = self.events, []
        return events


def candidate(direction, **changes):
    return EntryCandidate(
        sub_account_id="example", instrument_id="BTC-USDT-PERP", direction=direction,
        entry_low="10", entry_high="10", invalidation_stop="9" if direction == "LONG" else "11",
        take_profit=("12" if direction == "LONG" else "8",), origin="HUMAN",
        **({"ts_decision": 100} | changes),
    )


def execute_entry(gate, slots, candidate, *, plan_id, position_quantity, fill_quantity, price="10"):
    position = RoundStatus(
        round_id="round", sub_account_id=candidate.sub_account_id, instrument_id=candidate.instrument_id,
        direction=candidate.direction, quantity=position_quantity, stop=candidate.invalidation_stop,
    )
    cost = Decimal(fill_quantity) * Decimal(price) * (1 if candidate.direction == "LONG" else -1)
    slots.events.append(ExecutionEvent(
        kind="ENTRY_FILLED", sub_account_id="example", ts_event=candidate.ts_decision + 1,
        plan_id=plan_id, quantity=fill_quantity, price=price, position=position,
        postings=(
            LedgerPosting(account="POSITION_COST", currency="USD", amount=cost),
            LedgerPosting(account="TRADE_PAYABLE", currency="USD", amount=-cost),
        ),
    ))
    return gate.poll_events(candidate.ts_decision + 1)


@pytest.mark.parametrize(("direction", "tighten", "widen"), [
    ("LONG", "9.5", "8"), ("SHORT", "10.5", "12"),
])
def test_only_widening_needs_risk_approval(direction, tighten, widen):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate(direction)
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="2", fill_quantity="2")

    tighter = BarrierChangeRequest(sub_account_id="example", round_id="round", stop=tighten)
    wider = BarrierChangeRequest(sub_account_id="example", round_id="round", stop=widen)
    assert gate.change_barrier(tighter).accepted
    assert gate.change_barrier(wider).reason == "RISK_LIMIT"
    assert slots.stop_requests == [tighter]

    slots.allow_widening = True
    assert gate.change_barrier(wider).accepted
    assert slots.stop_requests[1] == ApprovedBarrierAmendment(amendment_id="approved-stop", request=wider)


def test_an_add_on_uses_its_own_risk_approval_and_quantity():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="2", fill_quantity="2")

    add_on = candidate("LONG", ts_decision=102, round_id="round")
    assert gate.submit_candidate(add_on).accepted
    execute_entry(gate, slots, add_on, plan_id="plan-102", position_quantity="3", fill_quantity="1")
    assert gate.account_state("example").rounds["round"].quantity == Decimal(3)

    with pytest.raises(AccountProcessError, match="超出.*定仓数量"):
        execute_entry(gate, slots, add_on, plan_id="plan-102", position_quantity="5", fill_quantity="2")


def test_two_pending_openings_cannot_use_the_same_round_id():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    assert gate.submit_candidate(candidate("LONG")).accepted
    with pytest.raises(AccountProcessError, match="新的回合 ID"):
        gate.submit_candidate(candidate("LONG", ts_decision=101))


def test_first_fill_checks_approved_stop_even_with_a_pending_round():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    pending = RoundStatus(
        round_id="round", sub_account_id="example", instrument_id=entry.instrument_id,
        direction="LONG", quantity="0", stop="9.5",
    )
    slots.events.append(ExecutionEvent(
        kind="ENTRY_PENDING", sub_account_id="example", ts_event=100,
        plan_id="plan-100", position=pending,
    ))
    gate.poll_events(100)

    slots.events.append(ExecutionEvent(
        kind="ENTRY_FILLED", sub_account_id="example", ts_event=101, plan_id="plan-100",
        quantity="1", position=pending.model_copy(update={"quantity": Decimal(1)}),
    ))
    with pytest.raises(AccountProcessError, match="止损与获批计划不一致"):
        gate.poll_events(101)


@pytest.mark.parametrize("terminal", ["CANCEL_ENTRY", "PLAN_CANCELLED", "PLAN_TERMINATED"])
def test_terminal_plan_cannot_fill_with_its_old_approval(terminal):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    if terminal == "CANCEL_ENTRY":
        gate.intervene_position("round", terminal, None, "test")
    else:
        slots.events.append(ExecutionEvent(
            kind=terminal, sub_account_id="example", ts_event=100, plan_id="plan-100",
        ))
        gate.poll_events(100)
    with pytest.raises(AccountProcessError, match="未经风控批准"):
        execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="1", fill_quantity="1")


def test_tightening_is_measured_from_last_accepted_stop():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="2", fill_quantity="2")
    assert gate.change_barrier(BarrierChangeRequest(
        sub_account_id="example", round_id="round", stop="9.8",
    )).accepted
    assert gate.change_barrier(BarrierChangeRequest(
        sub_account_id="example", round_id="round", stop="9.5",
    )).reason == "RISK_LIMIT"


@pytest.mark.parametrize(("quantity", "stop"), [("1.5", "9"), ("0.5", "8")])
def test_reduce_ratio_rejects_risk_increase_from_last_accepted_status(monkeypatch, quantity, stop):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="2", fill_quantity="2")
    original = gate.account_state("example").rounds["round"]
    responses = iter([
        original.model_copy(update={"quantity": Decimal(1)}),
        original.model_copy(update={"quantity": Decimal(quantity), "stop": Decimal(stop)}),
    ])
    monkeypatch.setattr(slots, "intervene_position", lambda *_: next(responses))
    gate.intervene_position("round", "REDUCE_RATIO", Decimal("0.5"), "test")
    with pytest.raises(AccountProcessError, match="减风险"):
        gate.intervene_position("round", "REDUCE_RATIO", Decimal("0.5"), "test")


@pytest.mark.parametrize(("kind", "plan_id", "postings", "message"), [
    ("ENTRY_FILLED", "plan-101", (), "缺少复式分录"),
    ("ENTRY_FILLED", "plan-101", (10, -9), "借贷不平"),
    ("ENTRY_FILLED", "plan-101", (11, -11), "分录与成交不一致"),
    ("EXIT_FILLED", None, (), "缺少复式分录"),
    ("EXIT_FILLED", "denied-plan", (10, -10), "未经风控批准"),
])
def test_bad_fill_does_not_change_risk_position(kind, plan_id, postings, message, monkeypatch):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="1", fill_quantity="1")
    add_on = candidate("LONG", ts_decision=101, round_id="round")
    assert gate.submit_candidate(add_on).accepted
    if plan_id == "denied-plan":
        monkeypatch.setattr(slots, "evaluate_candidate", lambda *_: RejectedPlanRecord(reason="TEST_RISK_LIMIT"))
        assert gate.submit_candidate(add_on).reason == "TEST_RISK_LIMIT"
    before = gate.account_state("example").rounds["round"]
    after = before.model_copy(update={"quantity": Decimal(2 if kind == "ENTRY_FILLED" else 0)})
    slots.events.append(ExecutionEvent(
        kind=kind, sub_account_id="example", ts_event=102, plan_id=plan_id, quantity="1",
        position=after, price="10", postings=tuple(
            LedgerPosting(account=account, currency="USD", amount=amount)
            for account, amount in zip(("POSITION_COST", "TRADE_PAYABLE"), postings)
        ),
    ))
    with pytest.raises(AccountProcessError, match=message):
        gate.poll_events(102)
    assert gate.account_state("example").rounds["round"] == before


@pytest.mark.parametrize(("price", "slippage", "accepted"), [
    ("9.99", None, False),
    ("10.01", None, False),
    ("10", None, True),
    ("9.5", "0.5", True),
    ("10.5", "0.5", True),
    ("10.51", "0.5", False),
])
def test_entry_fill_must_be_inside_approved_range_with_slippage(price, slippage, accepted):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    slots.allowed_slippage = slippage
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    args = dict(plan_id="plan-100", position_quantity="1", fill_quantity="1", price=price)
    if accepted:
        execute_entry(gate, slots, entry, **args)
        assert gate.account_state("example").rounds["round"].quantity == Decimal(1)
    else:
        with pytest.raises(AccountProcessError, match="入场区间"):
            execute_entry(gate, slots, entry, **args)
        assert not gate.account_state("example").rounds


def test_expired_approval_cannot_fill():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG", valid_until=100)
    assert gate.submit_candidate(entry).accepted

    with pytest.raises(AccountProcessError, match="计划已过期"):
        execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="1", fill_quantity="1")


def test_round_cancellation_without_a_plan_id_retires_the_pending_entry():
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    slots.events.append(ExecutionEvent(
        kind="ENTRY_CANCELLED", sub_account_id="example", ts_event=100,
        position=RoundStatus(
            round_id="round", sub_account_id="example", instrument_id=entry.instrument_id,
            direction="LONG", quantity="0", stop=entry.invalidation_stop,
        ),
    ))
    gate.poll_events(100)
    with pytest.raises(AccountProcessError, match="未经风控批准"):
        execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="1", fill_quantity="1")


def test_risk_does_not_see_a_fill_when_the_ledger_write_fails():
    slots = TestSlots()

    def fail_record(events):
        raise OSError("账本不可写")

    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {}, record=fail_record)
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    with pytest.raises(OSError, match="账本不可写"):
        execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="1", fill_quantity="1")
    assert not gate.account_state("example").rounds


@pytest.mark.parametrize(("cost", "accepted"), [("-20", True), ("-22", False)])
def test_full_exit_releases_the_round_cost_from_its_ledger(cost, accepted):
    slots = TestSlots()
    gate = ExecutionGate(("example",), slots, slots, slots, balances=lambda _: {})
    entry = candidate("LONG")
    assert gate.submit_candidate(entry).accepted
    execute_entry(gate, slots, entry, plan_id="plan-100", position_quantity="2", fill_quantity="2")
    before = gate.account_state("example").rounds["round"]
    postings = [
        LedgerPosting(account="TRADE_PAYABLE", currency="USD", amount="22"),
        LedgerPosting(account="POSITION_COST", currency="USD", amount=cost),
    ]
    if accepted:
        postings.append(LedgerPosting(account="REALIZED_PNL", currency="USD", amount="-2"))
    exit_fill = ExecutionEvent(
        kind="EXIT_FILLED", sub_account_id="example", ts_event=102, quantity="2", price="11",
        position=before.model_copy(update={"quantity": Decimal(0)}), postings=tuple(postings),
    )
    slots.events.append(exit_fill)
    if accepted:
        assert gate.poll_events(102) == [exit_fill]
        assert not gate.account_state("example").rounds
    else:
        with pytest.raises(AccountProcessError, match="持仓成本分录与成交不一致"):
            gate.poll_events(102)
        assert gate.account_state("example").rounds["round"] == before
