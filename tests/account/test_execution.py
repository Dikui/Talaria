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
        self.stop_requests = []
        self.events = []

    def size(self, candidate, account, valuation):
        return SizingDecision(quantity="2", basis="固定测试数量")

    def evaluate_candidate(self, candidate, sizing, account, valuation):
        return ApprovedExecutionPlan(
            plan_id=f"plan-{candidate.ts_decision}", round_id="round",
            candidate=candidate, sizing=sizing,
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


def execute_entry(gate, slots, candidate, *, plan_id, position_quantity, fill_quantity):
    position = RoundStatus(
        round_id="round", sub_account_id=candidate.sub_account_id, instrument_id=candidate.instrument_id,
        direction=candidate.direction, quantity=position_quantity, stop=candidate.invalidation_stop,
    )
    slots.events.append(ExecutionEvent(
        kind="ENTRY_FILLED", sub_account_id="example", ts_event=candidate.ts_decision + 1,
        plan_id=plan_id, quantity=fill_quantity, position=position,
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
