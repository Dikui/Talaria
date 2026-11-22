"""仅用于宿主验收的定仓、风控和成交插件。"""

from decimal import Decimal
from random import Random

from talaria.account.contracts import (
    ApprovedBarrierAmendment,
    ApprovedExecutionPlan,
    ExecutionEvent,
    LedgerPosting,
    PlanReceipt,
    RejectedPlanRecord,
    RoundStatus,
    SizingDecision,
)
from talaria.account.scheme import Slot, SlotPlugin


class FixedSizing:
    def size(self, candidate, account_state, valuation):
        return SizingDecision(quantity=Decimal(2), basis="测试固定数量")


class ExampleRisk:
    def __init__(self, *, resize=False, deny=False):
        self.resize = resize
        self.deny = deny

    def evaluate_candidate(self, candidate, sizing, account_state, valuation):
        if self.deny:
            return RejectedPlanRecord(reason="TEST_RISK_LIMIT")
        if self.resize:
            sizing = SizingDecision(quantity=Decimal(3), basis="测试篡改定仓")
        return ApprovedExecutionPlan(
            plan_id=f"{candidate.sub_account_id}-{candidate.ts_decision}",
            round_id=f"{candidate.sub_account_id}-round",
            candidate=candidate,
            sizing=sizing,
        )

    def approve_barrier_widening(self, request, account_state, valuation):
        return ApprovedBarrierAmendment(amendment_id="test-stop", request=request)


class ExampleFill:
    def __init__(self, context, *, unapproved=False):
        self.now = context.now
        self.random = Random(context.seed)
        self.unapproved = unapproved
        self.pending = []
        self.events = []

    def submit_plan(self, plan):
        self.pending.append(plan)
        return PlanReceipt(accepted=True)

    def on_market(self, event, valuation):
        assert self.now() == event.as_of
        for plan in self.pending[:]:
            # 示例仅在决策之后首次看见新柱时造一笔成交。
            if event.bar.ts_event < plan.candidate.ts_decision:
                continue
            self.pending.remove(plan)
            price = event.bar.close + Decimal(self.random.randrange(100)) / 100
            cost = price * plan.sizing.quantity
            position = RoundStatus(
                round_id=plan.round_id, sub_account_id=plan.candidate.sub_account_id,
                instrument_id=plan.candidate.instrument_id, direction=plan.candidate.direction,
                quantity=plan.sizing.quantity, stop=plan.candidate.invalidation_stop,
            )
            self.events.append(ExecutionEvent(
                kind="ENTRY_FILLED", ts_event=event.as_of, sub_account_id=position.sub_account_id,
                plan_id="unapproved" if self.unapproved else plan.plan_id,
                quantity=plan.sizing.quantity, position=position,
                postings=(
                    LedgerPosting(account="POSITION_COST", currency="USDT", amount=cost),
                    LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount=-cost),
                ),
            ))

    def poll_events(self, up_to_ts):
        events, self.events = self.events, []
        return events

    def tighten_barrier(self, req):
        raise AssertionError("回放样例中没有止损调整")

    def apply_approved_amendment(self, amendment):
        raise AssertionError("回放样例中没有止损调整")

    def intervene_position(self, round_id, action, ratio, reason):
        raise AssertionError("回放样例中没有持仓干预")

    def set_trading_state(self, sub_account_id, state, reason):
        raise AssertionError("回放样例中没有交易状态调整")


sizing = SlotPlugin(Slot.SIZING, (1,), lambda _context, _params: FixedSizing())
risk = SlotPlugin(Slot.RISK, (1,), lambda _context, params: ExampleRisk(**params))
fill = SlotPlugin(Slot.FILL, (1,), lambda context, params: ExampleFill(context, **params))
