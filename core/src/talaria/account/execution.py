"""账户宿主的风控路由和已批准执行记录校验。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from types import MappingProxyType

from talaria.account.contracts import (
    AccountState,
    ApprovedBarrierAmendment,
    ApprovedExecutionPlan,
    BarrierChangeRequest,
    BarrierReceipt,
    EntryCandidate,
    ExecutionEvent,
    FillSlot,
    Intervention,
    MarginValuation,
    PlanReceipt,
    RejectedPlanRecord,
    RiskSlot,
    RoundStatus,
    SizingDecision,
    SizingSlot,
    TradingState,
)


class AccountProcessError(ValueError):
    """账户进程收到不符合插槽契约的数据。"""


def _widens(position: RoundStatus, stop: Decimal) -> bool:
    return stop < position.stop if position.direction == "LONG" else stop > position.stop


class ExecutionGate:
    """把策略、人工和 agent 的候选交给同一风控通道。"""

    def __init__(
        self, sub_accounts: Sequence[str], sizing: SizingSlot, risk: RiskSlot, fill: FillSlot,
        *, balances: Callable[[str], Mapping[tuple[str, str], Decimal]],
        valuation: MarginValuation | None = None,
    ) -> None:
        self._sizing = sizing
        self._risk = risk
        self._fill = fill
        self._balances = balances
        self._valuation = valuation
        self._states = dict.fromkeys(sub_accounts, TradingState.ACTIVE)
        self._positions: dict[str, RoundStatus] = {}
        self._opening_rounds: set[str] = set()
        self._plans: dict[str, ApprovedExecutionPlan] = {}
        self._filled: dict[str, Fraction] = {}
        self._amendments: dict[str, BarrierChangeRequest] = {}
        self._last_execution_ts = -1

    def account_state(self, sub_account_id: str) -> AccountState:
        if sub_account_id not in self._states:
            raise AccountProcessError(f"账户进程不承载子账户 {sub_account_id}")
        rounds = {
            key: position for key, position in self._positions.items()
            if position.sub_account_id == sub_account_id and position.quantity > 0
        }
        return AccountState(
            sub_account_id, self._balances(sub_account_id), MappingProxyType(rounds),
            self._states[sub_account_id],
        )

    def submit_candidate(self, candidate: EntryCandidate) -> PlanReceipt | RejectedPlanRecord:
        if not isinstance(candidate, EntryCandidate):
            raise AccountProcessError("开仓或加仓须先提交不含数量的候选意图")
        state = self.account_state(candidate.sub_account_id)
        if candidate.round_id is not None:
            position = state.rounds.get(candidate.round_id)
            if position is None or (position.instrument_id, position.direction) != (
                candidate.instrument_id, candidate.direction,
            ):
                return RejectedPlanRecord(reason="ADD_ON_ROUND_MISMATCH")
        sizing = self._sizing.size(candidate, state, self._valuation)
        if not isinstance(sizing, SizingDecision):
            raise AccountProcessError("定仓插件须返回 SizingDecision")
        decision = self._risk.evaluate_candidate(candidate, sizing, state, self._valuation)
        if isinstance(decision, RejectedPlanRecord):
            return decision
        if not isinstance(decision, ApprovedExecutionPlan):
            raise AccountProcessError("风控插件须返回 ApprovedExecutionPlan 或 RejectedPlanRecord")
        if decision.candidate != candidate or decision.sizing != sizing:
            raise AccountProcessError("风控插件不得更改候选意图或定仓结果")
        if decision.plan_id in self._plans:
            raise AccountProcessError(f"重复的获批计划 ID：{decision.plan_id}")
        if candidate.round_id is not None and decision.round_id != candidate.round_id:
            raise AccountProcessError("风控插件不得更改加仓回合")
        if candidate.round_id is None and decision.round_id in self._opening_rounds:
            raise AccountProcessError("开仓计划需要新的回合 ID")

        receipt = self._fill.submit_plan(decision)
        if not isinstance(receipt, PlanReceipt):
            raise AccountProcessError("成交插件须返回 PlanReceipt")
        if receipt.accepted:
            self._plans[decision.plan_id] = decision
            if candidate.round_id is None:
                self._opening_rounds.add(decision.round_id)
        return receipt

    def change_barrier(self, request: BarrierChangeRequest) -> BarrierReceipt | RejectedPlanRecord:
        state = self.account_state(request.sub_account_id)
        position = state.rounds.get(request.round_id)
        if position is None:
            return RejectedPlanRecord(reason="ROUND_NOT_OPEN")
        if not _widens(position, request.stop):
            return self._fill.tighten_barrier(request)

        decision = self._risk.approve_barrier_widening(request, state, self._valuation)
        if isinstance(decision, RejectedPlanRecord):
            return decision
        if not isinstance(decision, ApprovedBarrierAmendment) or decision.request != request:
            raise AccountProcessError("风控插件须批准原始止损调整请求")
        if decision.amendment_id in self._amendments:
            raise AccountProcessError(f"重复的止损审批 ID：{decision.amendment_id}")
        receipt = self._fill.apply_approved_amendment(decision)
        if not isinstance(receipt, BarrierReceipt):
            raise AccountProcessError("成交插件须返回 BarrierReceipt")
        if receipt.accepted:
            self._amendments[decision.amendment_id] = request
        return receipt

    def intervene_position(
        self, round_id: str, action: Intervention, ratio: Decimal | None, reason: str,
    ) -> RoundStatus:
        if action not in ("CANCEL_ENTRY", "CLOSE_ALL", "REDUCE_RATIO"):
            raise AccountProcessError(f"不支持的减风险操作：{action}")
        if action == "REDUCE_RATIO":
            if ratio is None or not ratio.is_finite() or not 0 < ratio <= 1:
                raise AccountProcessError("减仓比例必须大于 0 且不大于 1")
        elif ratio is not None:
            raise AccountProcessError("只有按比例减仓允许指定比例")
        return self._fill.intervene_position(round_id, action, ratio, reason)

    def set_trading_state(
        self, sub_account_id: str | None, state: TradingState, reason: str,
    ) -> None:
        if state not in (TradingState.REDUCING, TradingState.HALTED):
            raise AccountProcessError("减风险通道仅允许 REDUCING 或 HALTED")
        accounts = self._states if sub_account_id is None else (sub_account_id,)
        for account in accounts:
            current = self.account_state(account).trading_state
            if current == TradingState.HALTED and state == TradingState.REDUCING:
                raise AccountProcessError("减风险通道不允许从 HALTED 恢复为 REDUCING")
        self._fill.set_trading_state(sub_account_id, state, reason)
        for account in accounts:
            self._states[account] = state

    def poll_events(self, now: int) -> list[ExecutionEvent]:
        events = self._fill.poll_events(now)
        for event in events:
            if not isinstance(event, ExecutionEvent):
                raise AccountProcessError("成交插件须返回 ExecutionEvent")
            if not self._last_execution_ts <= event.ts_event <= now:
                raise AccountProcessError("成交事件时点超前或倒退")
            if event.sub_account_id not in self._states:
                raise AccountProcessError(f"成交事件引用未知子账户 {event.sub_account_id}")
            self._check_position_event(event)
            self._last_execution_ts = event.ts_event
        return events

    def _check_position_event(self, event: ExecutionEvent) -> None:
        position = event.position
        if position is None:
            if event.kind == "ENTRY_FILLED":
                raise AccountProcessError("开仓或加仓成交缺少回合状态")
            return
        if position.sub_account_id != event.sub_account_id:
            raise AccountProcessError("成交事件与回合的子账户不一致")
        previous = self._positions.get(position.round_id)
        if previous is not None and (
            position.sub_account_id, position.instrument_id, position.direction,
        ) != (previous.sub_account_id, previous.instrument_id, previous.direction):
            raise AccountProcessError("成交事件不得更改回合的归属、标的或方向")

        before = Fraction(previous.quantity) if previous is not None else Fraction(0)
        increase = Fraction(position.quantity) - before
        if event.kind == "ENTRY_FILLED" or increase > 0:
            self._check_entry(event, position, increase, previous)
        if previous is not None and _widens(previous, position.stop):
            request = self._amendments.pop(event.amendment_id, None)
            if request is None or (request.sub_account_id, request.round_id, request.stop) != (
                position.sub_account_id, position.round_id, position.stop,
            ):
                raise AccountProcessError("放宽止损未经风控批准")
        self._positions[position.round_id] = position

    def _check_entry(
        self, event: ExecutionEvent, position: RoundStatus,
        increase: Fraction, previous: RoundStatus | None,
    ) -> None:
        plan = self._plans.get(event.plan_id)
        if event.kind != "ENTRY_FILLED" or plan is None:
            raise AccountProcessError("开仓或加仓成交未经风控批准")
        candidate = plan.candidate
        if (position.round_id, position.sub_account_id, position.instrument_id, position.direction) != (
            plan.round_id, candidate.sub_account_id, candidate.instrument_id, candidate.direction,
        ) or event.ts_event < candidate.ts_decision:
            raise AccountProcessError("成交与获批计划不一致")
        if previous is None and position.stop != candidate.invalidation_stop:
            raise AccountProcessError("开仓回合的止损与获批计划不一致")
        if increase <= 0 or event.quantity is None or Fraction(event.quantity) != increase:
            raise AccountProcessError("成交数量与回合变化不一致")
        total = self._filled.get(plan.plan_id, Fraction(0)) + increase
        if total > Fraction(plan.sizing.quantity):
            raise AccountProcessError("成交数量超出风控批准的定仓数量")
        self._filled[plan.plan_id] = total
