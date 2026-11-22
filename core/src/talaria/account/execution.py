"""账户宿主的风控路由和已批准执行记录校验。"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from threading import RLock
from types import MappingProxyType

from talaria.account.checkpoint import RuntimeCheckpoint
from talaria.account.contracts import (
    AccountState,
    ApprovedBarrierAmendment,
    ApprovedExecutionPlan,
    BarrierChangeRequest,
    BarrierReceipt,
    EntryCandidate,
    ExecutionEvent,
    ExitBarrierSpec,
    FillSlot,
    Intervention,
    MarginValuation,
    PlanReceipt,
    RejectedPlanRecord,
    RiskSlot,
    RoundExposure,
    RoundStatus,
    SizingDecision,
    SizingRejected,
    SizingSlot,
    TradingState,
)
from talaria.account.ledger import LedgerError, check_postings
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.margin import MarginPosition
from talaria.foundation.types import decimal_from_finite_fraction
from talaria.packs import DEFAULT_PACK, get_market_pack


_LOG = logging.getLogger(__name__)

_FILL_EVENTS = frozenset({"ENTRY_FILLED", "EXIT_FILLED"})
_UTC_DAY_MS = 86_400_000
_TRADE_ACCOUNTS = frozenset({"TRADE_PAYABLE", "POSITION_COST"})
_TERMINAL_PLAN_EVENTS = frozenset({
    "ENTRY_CANCELLED", "PLAN_CANCELLED", "PLAN_COMPLETED", "PLAN_EXPIRED",
    "PLAN_REJECTED", "PLAN_TERMINATED",
})


class AccountProcessError(ValueError):
    """账户进程收到不符合插槽契约的数据。"""


def _identity(position: RoundStatus) -> tuple[str, str, str, str]:
    return position.round_id, position.sub_account_id, position.instrument_id, position.direction


def _widens(position: RoundStatus, stop: Decimal) -> bool:
    return stop < position.stop if position.direction == "LONG" else stop > position.stop


@dataclass(frozen=True, slots=True)
class _FillUpdate:
    total_filled: Fraction | None
    currency: str
    remaining_cost: Fraction


class _GateCheckpoint(RuntimeCheckpoint):
    states: dict[str, TradingState]
    state_reasons: dict[str, str]
    positions: dict[str, RoundStatus]
    position_costs: dict[str, tuple[str, Fraction]]
    pending_reductions: dict[str, Fraction]
    round_origins: dict[str, RoundStatus]
    barriers: dict[str, ExitBarrierSpec]
    plans: dict[str, ApprovedExecutionPlan]
    submitted_plan_rounds: dict[str, str | None]
    filled: dict[str, Fraction]
    amendment_ids: set[str]
    last_execution_ts: int
    market_as_of: int
    marks: dict[str, DerivativePoint]
    approval_marks: dict[str, DerivativePoint]
    funding_rates: dict[str, DerivativePoint]
    audit: dict[str, list[ExecutionEvent]]


class ExecutionGate:
    """把策略、人工和 agent 的候选交给同一风控通道。"""

    def __init__(
        self, sub_accounts: Sequence[str], sizing: SizingSlot, risk: RiskSlot, fill: FillSlot,
        *, balances: Callable[[str], Mapping[tuple[str, str], Decimal]],
        valuation: MarginValuation | None = None,
        record: Callable[[Sequence[ExecutionEvent]], None] | None = None,
        allow_late_events: bool = False,
    ) -> None:
        self._sizing = sizing
        self._risk = risk
        self._fill = fill
        self._balances = balances
        self._valuation = valuation
        self._record = record
        self._allow_late_events = allow_late_events
        self._states = dict.fromkeys(sub_accounts, TradingState.ACTIVE)
        self._state_reasons: dict[str, str] = {}
        self._approvals = {account: RLock() for account in sub_accounts}
        self._submit_lock = RLock()
        self._positions: dict[str, RoundStatus] = {}
        self._position_costs: dict[str, tuple[str, Fraction]] = {}
        # 直接干预的回执可能先于成交入账，用其限制后续直接干预的数量。
        self._pending_reductions: dict[str, Fraction] = {}
        self._round_origins: dict[str, RoundStatus] = {}
        self._barriers: dict[str, ExitBarrierSpec] = {}
        self._plans: dict[str, ApprovedExecutionPlan] = {}
        self._submitted_plan_rounds: dict[str, str | None] = {}
        self._filled: dict[str, Fraction] = {}
        self._amendment_ids: set[str] = set()
        self._last_execution_ts = -1
        self._market_as_of = 0
        self._marks: dict[str, DerivativePoint] = {}
        self._approval_marks: dict[str, DerivativePoint] = {}
        self._funding_rates: dict[str, DerivativePoint] = {}
        self._audit: dict[str, list[ExecutionEvent]] = {account: [] for account in sub_accounts}
        self._drawdown_transition = getattr(risk, "drawdown_transition", None)
        self._start_trading_day = getattr(risk, "start_trading_day", None)
        self._entry_fill_allows = getattr(risk, "entry_fill_allows", None)
        self._market_pack = get_market_pack(DEFAULT_PACK)

    def checkpoint(self) -> dict:
        return _GateCheckpoint.capture(self)

    def restore_checkpoint(self, state: dict) -> None:
        _GateCheckpoint.restore(self, state)
        if self._states.keys() != self._approvals.keys():
            raise AccountProcessError("检查点所属子账户不符")
        for account, reason in self._state_reasons.copy().items():
            if reason == "PAPER_STOPPING":
                self._states[account] = TradingState.ACTIVE
                self._state_reasons.pop(account)
                self._fill.resume_after_stop(account)
                self._write_event(ExecutionEvent(kind="TRADING_STATE_CHANGED", sub_account_id=account,
                                                 ts_event=max(self._market_as_of, self._last_execution_ts),
                                                 reason="PAPER_RESUMED:ACTIVE"))

    def restore_closed_history(self, events: Sequence[ExecutionEvent]) -> None:
        for event in events:
            position = event.position
            if position is not None:
                self._round_origins.setdefault(position.round_id, position)
                self._positions[position.round_id] = position
            self._last_execution_ts = max(self._last_execution_ts, event.ts_event)

    def _approval_lock(self, account: str) -> RLock:
        try:
            return self._approvals[account]
        except KeyError:
            raise AccountProcessError(f"账户进程不承载子账户 {account}") from None

    @contextmanager
    def _all_accounts(self):
        with ExitStack() as stack:
            for lock in self._approvals.values():
                stack.enter_context(lock)
            yield

    def account_state(self, sub_account_id: str) -> AccountState:
        with self._approval_lock(sub_account_id):
            return self._account_state(sub_account_id, for_approval=True)

    def _account_state(self, sub_account_id: str, *, for_approval: bool = False) -> AccountState:
        rounds = {
            key: position for key, position in self._positions.copy().items()
            if position.sub_account_id == sub_account_id and position.quantity > 0
        }
        exposures = []
        for key, position in rounds.items():
            _, cost = self._position_costs[key]
            exposures.append(self._position_exposure(position, cost))
        reservations = []
        for plan in self._plans.copy().values():
            candidate = plan.candidate
            if candidate.sub_account_id != sub_account_id:
                continue
            remaining = Fraction(plan.sizing.quantity) - self._filled.get(plan.plan_id, Fraction(0))
            if remaining <= 0:
                continue
            position = rounds.get(plan.round_id)
            entry = candidate.entry_high if candidate.direction == "LONG" else candidate.entry_low
            slip = Fraction(plan.allowed_slippage or 0)
            entry = Fraction(entry) + (slip if candidate.direction == "LONG" else -slip)
            max_entry_price = decimal_from_finite_fraction(Fraction(candidate.entry_high) + slip)
            reservations.append(RoundExposure(plan.round_id, MarginPosition(
                candidate.instrument_id, candidate.direction,
                decimal_from_finite_fraction(remaining), decimal_from_finite_fraction(entry),
            ), position.stop if position is not None else candidate.invalidation_stop, max_entry_price))
        marks = self._approval_marks if for_approval else self._marks
        return AccountState(
            sub_account_id, MappingProxyType(dict(self._balances(sub_account_id))), MappingProxyType(rounds),
            self._states[sub_account_id], max(self._market_as_of, self._last_execution_ts),
            tuple(exposures), tuple(reservations),
            MappingProxyType({key: self._barriers[key] for key in rounds if key in self._barriers}),
            MappingProxyType({key: point.model_copy() for key, point in marks.items()}),
            MappingProxyType({key: point.model_copy() for key, point in self._funding_rates.items()}),
        )

    def _contract_multiplier(self, instrument_id: str) -> Fraction:
        try:
            return Fraction(self._market_pack.traits(instrument_id).margin.contract_multiplier)
        except KeyError as exc:
            raise AccountProcessError(f"成交引用未知标的 {instrument_id}") from exc

    def _position_exposure(self, position: RoundStatus, cost: Fraction) -> RoundExposure:
        entry = abs(cost) / (Fraction(position.quantity) * self._contract_multiplier(position.instrument_id))
        return RoundExposure(position.round_id, MarginPosition(
            position.instrument_id, position.direction, position.quantity,
            Decimal(entry.numerator) / Decimal(entry.denominator),
        ), position.stop)

    def submit_candidate(self, candidate: EntryCandidate) -> PlanReceipt | RejectedPlanRecord:
        if not isinstance(candidate, EntryCandidate):
            raise AccountProcessError("开仓或加仓须先提交不含数量的候选意图")
        with self._approval_lock(candidate.sub_account_id):
            decision = self._submit_candidate(candidate)
            if isinstance(decision, RejectedPlanRecord) or not decision.accepted:
                self._audit[candidate.sub_account_id].append(ExecutionEvent(
                    kind="CANDIDATE_REJECTED", sub_account_id=candidate.sub_account_id,
                    ts_event=min(candidate.ts_decision, max(self._market_as_of, self._last_execution_ts)),
                    reason=decision.reason or "FILL_REJECTED",
                ))
            return decision

    def _submit_candidate(self, candidate: EntryCandidate) -> PlanReceipt | RejectedPlanRecord:
        state = self._account_state(candidate.sub_account_id, for_approval=True)
        if state.trading_state != TradingState.ACTIVE:
            return RejectedPlanRecord(reason="TRADING_STATE_BLOCKED")
        if candidate.ts_decision > state.as_of:
            return RejectedPlanRecord(reason="FUTURE_DECISION")
        if candidate.round_id is not None:
            position = state.rounds.get(candidate.round_id)
            if position is None or position.instrument_id != candidate.instrument_id:
                return RejectedPlanRecord(reason="ADD_ON_ROUND_MISMATCH")
            if position.direction != candidate.direction:
                return RejectedPlanRecord(reason="OPPOSITE_POSITION_OPEN")
            if candidate.inherited_barriers is not None:
                barriers = state.barriers.get(candidate.round_id)
                if barriers is None or barriers.model_copy(update={"stop": position.stop}) != candidate.inherited_barriers:
                    return RejectedPlanRecord(reason="ADD_ON_BARRIERS_CHANGED")
        try:
            sizing = self._sizing.size(candidate, state, self._valuation)
        except SizingRejected as exc:
            return RejectedPlanRecord(reason=str(exc))
        if not isinstance(sizing, SizingDecision):
            raise AccountProcessError("定仓插件须返回 SizingDecision")
        decision = self._risk.evaluate_candidate(candidate, sizing, state, self._valuation)
        if isinstance(decision, RejectedPlanRecord):
            return decision
        if not isinstance(decision, ApprovedExecutionPlan):
            raise AccountProcessError("风控插件须返回 ApprovedExecutionPlan 或 RejectedPlanRecord")
        if decision.candidate != candidate or decision.sizing != sizing:
            raise AccountProcessError("风控插件不得更改候选意图或定仓结果")
        with self._submit_lock:
            if candidate.draft_plan_id is not None:
                decision = decision.model_copy(update={"plan_id": candidate.draft_plan_id})
            return self._submit_approved(decision)

    def _submit_approved(self, decision: ApprovedExecutionPlan) -> PlanReceipt:
        candidate = decision.candidate
        if decision.plan_id in self._submitted_plan_rounds:
            raise AccountProcessError(f"重复的获批计划 ID：{decision.plan_id}")
        if candidate.round_id is not None and decision.round_id != candidate.round_id:
            raise AccountProcessError("风控插件不得更改加仓回合")
        if candidate.round_id is None and decision.round_id in self._round_origins:
            raise AccountProcessError("开仓计划需要新的回合 ID")

        receipt = self._fill.submit_plan(decision)
        if not isinstance(receipt, PlanReceipt):
            raise AccountProcessError("成交插件须返回 PlanReceipt")
        self._submitted_plan_rounds[decision.plan_id] = decision.round_id if receipt.accepted else None
        if receipt.accepted:
            self._plans[decision.plan_id] = decision
            if candidate.round_id is None:
                self._round_origins[decision.round_id] = RoundStatus(
                    round_id=decision.round_id, sub_account_id=candidate.sub_account_id,
                    instrument_id=candidate.instrument_id, direction=candidate.direction,
                    quantity=0, stop=candidate.invalidation_stop,
                )
                if decision.barriers is not None:
                    self._barriers[decision.round_id] = decision.barriers
        if receipt.accepted and candidate.draft_plan_id is not None:
            return receipt.model_copy(update={"plan_id": decision.plan_id})
        return receipt

    def change_barrier(self, request: BarrierChangeRequest) -> BarrierReceipt | RejectedPlanRecord:
        self._approval_lock(request.sub_account_id)
        with self._all_accounts():
            # 先入账旧止损下已发生的成交，再用修改回执更新当前止损。
            now = max(self._market_as_of, self._last_execution_ts)
            while pending := self._fill.poll_events(now):
                self._process_batch(now, (), pending)
            state = self._account_state(request.sub_account_id, for_approval=True)
            decision = self._change_barrier(request, state)
            if isinstance(decision, RejectedPlanRecord) or not decision.accepted:
                self._audit[request.sub_account_id].append(ExecutionEvent(
                    kind="BARRIER_REJECTED", sub_account_id=request.sub_account_id,
                    ts_event=state.as_of, reason=decision.reason or "FILL_REJECTED",
                ))
            return decision

    def _change_barrier(
        self, request: BarrierChangeRequest, state: AccountState,
    ) -> BarrierReceipt | RejectedPlanRecord:
        position = state.rounds.get(request.round_id)
        if position is None:
            return RejectedPlanRecord(reason="ROUND_NOT_OPEN")
        if not _widens(position, request.stop):
            receipt = self._fill.tighten_barrier(request)
            self._accept_barrier(position, request, receipt)
            return receipt

        if state.trading_state != TradingState.ACTIVE:
            return RejectedPlanRecord(reason="TRADING_STATE_BLOCKED")
        decision = self._risk.approve_barrier_widening(request, state, self._valuation)
        if isinstance(decision, RejectedPlanRecord):
            return decision
        if not isinstance(decision, ApprovedBarrierAmendment) or decision.request != request:
            raise AccountProcessError("风控插件须批准原始止损调整请求")
        if decision.amendment_id in self._amendment_ids:
            raise AccountProcessError(f"重复的止损审批 ID：{decision.amendment_id}")
        receipt = self._fill.apply_approved_amendment(decision)
        self._accept_barrier(position, request, receipt)
        if receipt.accepted:
            self._amendment_ids.add(decision.amendment_id)
        return receipt

    def _accept_barrier(
        self, position: RoundStatus, request: BarrierChangeRequest, receipt: BarrierReceipt,
    ) -> None:
        if not isinstance(receipt, BarrierReceipt):
            reason = f"回合 {request.round_id} 的成交插件须返回 BarrierReceipt"
            self._record_rejection(max(0, self._last_execution_ts), reason, request.sub_account_id)
            raise AccountProcessError(reason)
        if receipt.accepted:
            self._positions[request.round_id] = position.model_copy(update={"stop": request.stop})

    def intervene_position(
        self, round_id: str, action: Intervention, ratio: Decimal | None, reason: str,
    ) -> RoundStatus:
        origin = self._round_origins.get(round_id)
        if origin is None:
            raise AccountProcessError(f"减风险操作引用未知回合 {round_id}")
        with self._approval_lock(origin.sub_account_id):
            return self._intervene_position(round_id, action, ratio, reason)

    def _intervene_position(
        self, round_id: str, action: Intervention, ratio: Decimal | None, reason: str,
    ) -> RoundStatus:
        if action not in ("CANCEL_ENTRY", "CLOSE_ALL", "REDUCE_RATIO"):
            raise AccountProcessError(f"不支持的减风险操作：{action}")
        if action == "REDUCE_RATIO":
            if ratio is None or not ratio.is_finite() or not 0 < ratio <= 1:
                raise AccountProcessError("减仓比例必须大于 0 且不大于 1")
        elif ratio is not None:
            raise AccountProcessError("只有按比例减仓允许指定比例")
        previous = self._positions.get(round_id)
        if previous is None and action == "CANCEL_ENTRY":
            previous = self._round_origins.get(round_id)
        if previous is None:
            raise AccountProcessError(f"减风险操作引用未知回合 {round_id}")
        before = self._pending_reductions.get(round_id, previous.quantity)
        position = self._fill.intervene_position(round_id, action, ratio, reason)
        if (
            not isinstance(position, RoundStatus) or _identity(position) != _identity(previous)
            or not 0 <= position.quantity <= before or _widens(previous, position.stop)
        ):
            reason = f"减风险回执不得更改回合 {round_id} 的归属、增加持仓或放宽止损"
            self._record_rejection(max(0, self._last_execution_ts), reason, previous.sub_account_id)
            raise AccountProcessError(reason)
        if round_id in self._positions:
            self._positions[round_id] = previous.model_copy(update={"stop": position.stop})
        if position.quantity < before:
            self._pending_reductions[round_id] = Fraction(position.quantity)
        if action == "CANCEL_ENTRY" or (action == "CLOSE_ALL" and position.quantity == 0):
            self._retire_round_plans(round_id)
        return position

    def _retire_round_plans(self, round_id: str) -> None:
        for plan_id, plan in tuple(self._plans.items()):
            if plan.round_id == round_id:
                del self._plans[plan_id]
                self._filled.pop(plan_id, None)

    def _retire_terminal_plans(self, event: ExecutionEvent) -> None:
        if event.kind in _TERMINAL_PLAN_EVENTS and event.plan_id in self._plans:
            del self._plans[event.plan_id]
            self._filled.pop(event.plan_id, None)
        position = event.position
        if position is None:
            return
        if (event.kind == "ENTRY_CANCELLED" and event.plan_id is None) or (
            position.quantity == 0 and event.kind in {
                "EXIT_FILLED", "ROUND_CANCELLED", "ROUND_CLOSED", "ROUND_LIQUIDATED",
            }
        ):
            self._retire_round_plans(position.round_id)

    def set_trading_state(
        self, sub_account_id: str | None, state: TradingState, reason: str,
    ) -> None:
        lock = self._all_accounts() if sub_account_id is None else self._approval_lock(sub_account_id)
        with lock:
            self._set_trading_state(sub_account_id, state, reason)

    def _set_trading_state(
        self, sub_account_id: str | None, state: TradingState, reason: str,
    ) -> None:
        if state not in (TradingState.REDUCING, TradingState.HALTED):
            raise AccountProcessError("减风险通道仅允许 REDUCING 或 HALTED")
        accounts = self._states if sub_account_id is None else (sub_account_id,)
        for account in accounts:
            current = self._states[account]
            if current == TradingState.HALTED and state == TradingState.REDUCING:
                raise AccountProcessError("减风险通道不允许从 HALTED 恢复为 REDUCING")
        for account in accounts:
            self._states[account] = state
            self._state_reasons[account] = reason
        try:
            self._fill.set_trading_state(sub_account_id, state, reason)
        finally:
            if state == TradingState.HALTED:
                for plan_id, plan in tuple(self._plans.items()):
                    if plan.candidate.sub_account_id in accounts:
                        del self._plans[plan_id]
                        self._filled.pop(plan_id, None)

    def observe_market(self, as_of: int, points: Sequence[DerivativePoint]) -> None:
        with self._all_accounts():
            self._process_batch(as_of, points, ())

    def observe_marks_for_approval(self, as_of: int, points: Sequence[DerivativePoint]) -> None:
        with self._all_accounts():
            for point in points:
                if point.kind == DerivativeKind.MARK_PRICE and point.value > 0 and point.visible_at(as_of):
                    self._update_observation(self._approval_marks, point)
                elif point.kind == DerivativeKind.FUNDING_RATE and not point.is_funding_settlement and point.visible_at(as_of):
                    self._update_observation(self._funding_rates, point)

    @staticmethod
    def _update_observation(observations: dict[str, DerivativePoint], point: DerivativePoint) -> None:
        previous = observations.get(point.instrument_id)
        if previous is None or (point.observation_time, point.revision, point.ts_available, point.ts_committed) >= (
            previous.observation_time, previous.revision, previous.ts_available, previous.ts_committed,
        ):
            observations[point.instrument_id] = point.model_copy()

    def _advance_time(self, now: int) -> None:
        previous = max(self._market_as_of, self._last_execution_ts)
        midnight = now // _UTC_DAY_MS * _UTC_DAY_MS
        if previous < midnight and self._start_trading_day is not None:
            self._market_as_of = midnight
            for account in self._states:
                self._start_trading_day(self._account_state(account), self._valuation)
        self._market_as_of = max(previous, now)

    def _check_drawdowns(self, errors: list[AccountProcessError | LedgerError]) -> None:
        if self._drawdown_transition is not None:
            for account in self._states:
                transition = self._drawdown_transition(self._account_state(account), self._valuation)
                if transition is not None:
                    self._apply_drawdown(account, *transition, errors=errors)

    def _apply_drawdown(
        self, account: str, state: TradingState, liquidate: bool, *,
        errors: list[AccountProcessError | LedgerError],
    ) -> None:
        reason = "DRAWDOWN_LIMIT"
        snapshot = self._account_state(account)
        self._set_trading_state(account, state, reason)
        self._write_event(ExecutionEvent(
            kind="TRADING_STATE_CHANGED", sub_account_id=account, ts_event=self._market_as_of,
            reason=f"{reason}:{state.value}",
        ))
        actions: list[tuple[str, Intervention]] = [
            (round_id, "CANCEL_ENTRY") for round_id in dict.fromkeys(item.round_id for item in snapshot.reservations)
        ]
        if liquidate:
            actions.extend((round_id, "CLOSE_ALL") for round_id in snapshot.rounds)
        for round_id, action in actions:
            try:
                self._intervene_position(round_id, action, None, reason)
            except AccountProcessError as exc:
                # 插槽可能已生成同批的后续成交。违规回执不得阻止其余减仓入账。
                errors.append(exc)

    def poll_events(self, now: int, *, points: Sequence[DerivativePoint] = ()) -> list[ExecutionEvent]:
        with self._all_accounts():
            return self._process_batch(now, points, self._fill.poll_events(now))

    def _process_batch(
        self, now: int, points: Sequence[DerivativePoint], events: Sequence[ExecutionEvent],
    ) -> list[ExecutionEvent]:
        # 同一可获取时刻先处理观测，再校验该时刻的成交。观测按实际观测时刻及修订顺序处理。
        observations = sorted(
            (point for point in points if point.visible_at(now)),
            key=lambda point: (point.ts_available, point.observation_time, point.ts_committed),
        )
        timeline = [(point.ts_available, point) for point in observations]
        timeline.extend((event.ts_event if isinstance(event, ExecutionEvent) else now, event) for event in events)
        accepted = []
        errors: list[AccountProcessError | LedgerError] = []
        for timestamp, event in sorted(timeline, key=lambda item: item[0]):
            if isinstance(event, DerivativePoint):
                self._advance_time(timestamp)
                if event.kind == DerivativeKind.MARK_PRICE and event.value > 0:
                    self._update_observation(self._marks, event)
                    self._update_observation(self._approval_marks, event)
                elif event.kind == DerivativeKind.FUNDING_RATE:
                    self._update_observation(self._funding_rates, event)
            else:
                try:
                    fill = self._check_event(event, now)
                except (AccountProcessError, LedgerError) as exc:
                    if isinstance(event, ExecutionEvent):
                        self._record_rejection(min(event.ts_event, now), str(exc), event.sub_account_id, event.plan_id)
                    else:
                        self._record_rejection(now, str(exc))
                    errors.append(exc)
                    continue
                self._advance_time(timestamp)
                self._write_event(event)
                self._accept_event(event, fill)
                accepted.append(event)
            self._check_drawdowns(errors)
        self._advance_time(now)
        self._check_drawdowns(errors)
        self._flush_audit(now)
        if errors:
            first_error = errors[0]
            raise AccountProcessError(str(first_error)) from first_error
        return accepted

    def _flush_audit(self, now: int) -> None:
        pending = sorted(
            (event for events in self._audit.values() for event in events if event.ts_event <= now),
            key=lambda event: event.ts_event,
        )
        for event in pending:
            self._write_event(event.model_copy(update={"ts_event": max(event.ts_event, self._last_execution_ts)}))
            self._audit[event.sub_account_id].remove(event)

    def record_host_event(self, event: ExecutionEvent) -> None:
        self._write_event(event)

    def _write_event(self, event: ExecutionEvent) -> None:
        if self._record is not None:
            self._record([event])
        self._last_execution_ts = max(self._last_execution_ts, event.ts_event)

    def _record_rejection(
        self, timestamp: int, reason: str, account: str | None = None, plan_id: str | None = None,
    ) -> None:
        _LOG.error("拒绝成交插件回报：%s", reason)
        if self._record is not None:
            self._write_event(ExecutionEvent(
                kind="EXECUTION_REJECTED",
                sub_account_id=account if account in self._states else next(iter(self._states)),
                ts_event=max(self._last_execution_ts, timestamp),
                plan_id=plan_id, reason=reason,
            ))

    def _check_event(self, event: ExecutionEvent, now: int) -> _FillUpdate | None:
        if not isinstance(event, ExecutionEvent):
            raise AccountProcessError("成交插件须返回 ExecutionEvent")
        earliest = 0 if self._allow_late_events else max(self._market_as_of, self._last_execution_ts)
        if not earliest <= event.ts_event <= now:
            raise AccountProcessError("成交事件时点超前或倒退")
        if event.sub_account_id not in self._states:
            raise AccountProcessError(f"成交事件引用未知子账户 {event.sub_account_id}")
        plan = self._plans.get(event.plan_id)
        is_fill = event.kind in _FILL_EVENTS
        if event.plan_id is not None and is_fill and plan is None:
            if (
                event.kind != "EXIT_FILLED" or event.position is None
                or self._submitted_plan_rounds.get(event.plan_id) != event.position.round_id
            ):
                raise AccountProcessError("成交未经风控批准或计划已失效")
        if plan is not None and (
            plan.candidate.sub_account_id != event.sub_account_id
            or (event.position is not None and event.position.round_id != plan.round_id)
        ):
            raise AccountProcessError("成交事件与获批计划的子账户或回合不一致")
        if event.kind == "ENTRY_FILLED" and plan is not None and (
            plan.candidate.valid_until is not None and event.ts_event > plan.candidate.valid_until
        ):
            raise AccountProcessError("成交引用的计划已过期")
        check_postings(event)
        position = event.position
        if not is_fill and (
            event.quantity is not None or event.price is not None
            or any(posting.account in _TRADE_ACCOUNTS for posting in event.postings)
        ):
            raise AccountProcessError("成交须使用 ENTRY_FILLED 或 EXIT_FILLED")
        if position is None:
            if is_fill:
                raise AccountProcessError("成交缺少回合状态")
            return None
        if position.sub_account_id != event.sub_account_id:
            raise AccountProcessError("成交事件与回合的子账户不一致")
        origin = self._round_origins.get(position.round_id)
        if origin is None or _identity(origin) != _identity(position):
            raise AccountProcessError("成交事件引用未知回合或更改回合的归属、标的或方向")
        previous = self._positions.get(position.round_id)

        before = Fraction(previous.quantity) if previous is not None else Fraction(0)
        increase = Fraction(position.quantity) - before
        total = None
        if event.kind == "ENTRY_FILLED" or increase > 0:
            total = self._check_entry(event, position, increase, previous)
        elif event.kind == "EXIT_FILLED" or increase < 0:
            if event.kind != "EXIT_FILLED" or increase >= 0:
                raise AccountProcessError("持仓减少须回报 EXIT_FILLED")
            if event.quantity is None or Fraction(event.quantity) != -increase:
                raise AccountProcessError("平仓成交数量与回合变化不一致")
        if previous is not None and previous.quantity > 0 and _widens(previous, position.stop):
            raise AccountProcessError("放宽止损未经风控批准")
        if not is_fill:
            return None
        currency, remaining_cost = self._check_fill_postings(event, position)
        if event.kind == "ENTRY_FILLED" and self._entry_fill_allows is not None:
            exposure = self._position_exposure(position, remaining_cost)
            if not self._entry_fill_allows(event.sub_account_id, exposure, self._valuation, event.ts_event):
                raise AccountProcessError("开仓成交后的强平价未保留硬止损安全距离")
        return _FillUpdate(total, currency, remaining_cost)

    def _check_fill_postings(self, event: ExecutionEvent, position: RoundStatus) -> tuple[str, Fraction]:
        if not event.postings:
            raise AccountProcessError("成交缺少复式分录")
        if event.price is None or event.quantity is None:
            raise AccountProcessError("成交缺少单价或数量")
        expected = Fraction(event.price) * Fraction(event.quantity) * self._contract_multiplier(position.instrument_id)
        buying = (event.kind == "ENTRY_FILLED") == (position.direction == "LONG")
        settlement: dict[str, Fraction] = defaultdict(Fraction)
        position_cost: dict[str, Fraction] = defaultdict(Fraction)
        for posting in event.postings:
            if posting.account == "TRADE_PAYABLE":
                settlement[posting.currency] += Fraction(posting.amount)
            elif posting.account == "POSITION_COST":
                position_cost[posting.currency] += Fraction(posting.amount)
        if list(settlement.values()) != [-expected if buying else expected]:
            raise AccountProcessError("对价分录与成交不一致")
        currency, amount = next(iter(settlement.items()))
        previous_currency, previous_cost = self._position_costs.get(position.round_id, (currency, Fraction(0)))
        if currency != previous_currency or position_cost.keys() != settlement.keys():
            raise AccountProcessError("持仓成本分录与成交不一致")
        cost_change = position_cost[currency]
        remaining_cost = previous_cost + cost_change
        if event.kind == "ENTRY_FILLED":
            if cost_change != -amount:
                raise AccountProcessError("持仓成本分录与成交不一致")
        elif position.quantity == 0:
            if remaining_cost != 0:
                raise AccountProcessError("持仓成本分录与成交不一致：全平须结清持仓成本")
        elif remaining_cost * previous_cost <= 0 or abs(remaining_cost) >= abs(previous_cost):
            raise AccountProcessError("持仓成本分录与成交不一致：减仓须冲减部分持仓成本")
        return currency, remaining_cost

    def _accept_event(self, event: ExecutionEvent, fill: _FillUpdate | None) -> None:
        position = event.position
        if position is not None:
            self._positions[position.round_id] = position
            if fill is not None:
                if fill.total_filled is not None:
                    self._filled[event.plan_id] = fill.total_filled
                if position.quantity == 0:
                    self._position_costs.pop(position.round_id, None)
                else:
                    self._position_costs[position.round_id] = (fill.currency, fill.remaining_cost)
            target = self._pending_reductions.get(position.round_id)
            if target is not None:
                if position.quantity <= target:
                    del self._pending_reductions[position.round_id]
                elif event.kind == "ENTRY_FILLED":
                    self._pending_reductions[position.round_id] = target + Fraction(event.quantity)
        self._retire_terminal_plans(event)

    def _check_entry(
        self, event: ExecutionEvent, position: RoundStatus,
        increase: Fraction, previous: RoundStatus | None,
    ) -> Fraction:
        plan = self._plans.get(event.plan_id)
        if self._states[event.sub_account_id] == TradingState.HALTED:
            raise AccountProcessError("HALTED 状态拒绝开仓或加仓成交")
        if event.kind != "ENTRY_FILLED" or plan is None:
            raise AccountProcessError("开仓或加仓成交未经风控批准")
        candidate = plan.candidate
        if candidate.signal_bar_close is not None:
            if event.ts_event <= candidate.signal_bar_close:
                raise AccountProcessError("成交时间必须晚于信号柱收盘")
            if event.ts_event == candidate.ts_decision:
                raise AccountProcessError("成交不得复用发出意图时的信号柱行情")
        if event.ts_event < candidate.ts_decision:
            raise AccountProcessError("成交与获批计划不一致")
        if (previous is None or previous.quantity == 0) and position.stop != candidate.invalidation_stop:
            raise AccountProcessError("开仓回合的止损与获批计划不一致")
        if increase <= 0 or event.quantity is None or Fraction(event.quantity) != increase:
            raise AccountProcessError("成交数量与回合变化不一致")
        if event.price is None:
            raise AccountProcessError("成交缺少单价")
        slippage = Fraction(plan.allowed_slippage or 0)
        low = Fraction(candidate.entry_low) - slippage
        high = Fraction(candidate.entry_high) + slippage
        if not low <= Fraction(event.price) <= high:
            raise AccountProcessError("成交价超出获批入场区间及允许滑点")
        total = self._filled.get(plan.plan_id, Fraction(0)) + increase
        if total > Fraction(plan.sizing.quantity):
            raise AccountProcessError("成交数量超出风控批准的定仓数量")
        return total
