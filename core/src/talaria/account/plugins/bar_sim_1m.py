"""按确认的 1m 行情模拟成交并托管每个仓位回合。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from heapq import merge
from math import ceil, floor

from pydantic import Field, StrictInt, field_validator

from talaria.account.contracts import (
    ApprovedBarrierAmendment, ApprovedExecutionPlan, BarrierChangeRequest, BarrierReceipt,
    Direction, ExecutionEvent, ExitBarrierSpec, FillTier, Intervention, LedgerPosting, MarginValuation,
    MarketEvent, PlanReceipt, RoundPhase, RoundStatus, TradingState,
)
from talaria.account.plugins.risk_math import collateral_balance, sign
from talaria.account.risk_config import AccountRiskConfig, Positive, Rate
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, Slot, SlotPlugin
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint, latest_visible_derivatives
from talaria.foundation.margin import MarginPortfolio, MarginPosition
from talaria.foundation.market import InstrumentSpec, MarginTrait
from talaria.foundation.ticks import Quote, Trade
from talaria.foundation.types import ContractModel, decimal_from_finite_fraction as decimal_exact
from talaria.packs import DEFAULT_PACK, get_market_pack

_MINUTE = 60_000
_DEFAULT_RISK = AccountRiskConfig()
_TERMINAL = {RoundPhase.CLOSED, RoundPhase.CANCELLED, RoundPhase.LIQUIDATED}
_STATE_ORDER = (TradingState.ACTIVE, TradingState.REDUCING, TradingState.HALTED)
_BASIS_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
type BalanceReader = Callable[[str], Mapping[tuple[str, str], Decimal]]
type MarkHistory = dict[str, list[tuple[int, DerivativePoint]]]


def _divide(value: Fraction) -> Decimal:
    with localcontext(_BASIS_CONTEXT):
        return Decimal(value.numerator) / Decimal(value.denominator)


def _allocate(total: Fraction, quantity: Fraction, held: Fraction) -> Fraction:
    if quantity == held:
        return total
    return Fraction(_divide(total * quantity / held))


class BarSimParams(ContractModel):
    decision_latency_ms: StrictInt = Field(default=1, gt=0)
    spread_pct: Rate = Decimal("0.0002")
    maker_fee_pct: Rate = Decimal("0.0002")
    volume_participation: Positive = Field(default=Decimal("0.10"), le=1)
    exit_volume_participation: Positive | None = Field(default=None, le=1)
    liquidation_fee_pct: Rate = Decimal(0)

    @field_validator(
        "spread_pct", "maker_fee_pct", "volume_participation", "exit_volume_participation",
        "liquidation_fee_pct", mode="before",
    )
    @classmethod
    def _decimal_string(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            try:
                return Decimal(value)
            except InvalidOperation:
                pass
        raise ValueError("成交参数必须为十进制字符串")


@dataclass(slots=True)
class _Entry:
    plan: ApprovedExecutionPlan
    ready_at: int
    remaining: Fraction
    resting: bool = False
    last_fill_slot: int | None = None


@dataclass(frozen=True, slots=True)
class _Reduction:
    quantity: Fraction
    requested_at: int
    reason: str


@dataclass(slots=True)
class _BarLiquidity:
    entry: Fraction
    exit: Fraction


@dataclass(slots=True)
class _FundingBooking:
    price: Decimal | None
    cash: Fraction = Fraction(0)


@dataclass(slots=True)
class _Settlement:
    point: DerivativePoint
    bookings: dict[str, _FundingBooking] = field(default_factory=dict)
    unpriced: set[str] = field(default_factory=set)


@dataclass(slots=True)
class PositionExecutor:
    """一个回合共用入场队列、数量、成本和保护屏障。"""

    round_id: str
    account: str
    direction: Direction
    instrument: InstrumentSpec
    margin: MarginTrait
    config: AccountRiskConfig
    barriers: ExitBarrierSpec
    stop: Decimal
    entries: dict[str, _Entry] = field(default_factory=dict)
    phase: RoundPhase = RoundPhase.PENDING_ENTRY
    quantity: Fraction = Fraction(0)
    cost: Fraction = Fraction(0)
    margin_used: Fraction = Fraction(0)
    isolated_carry: Fraction = Fraction(0)
    opened_at: int | None = None
    trailing: bool = False
    trailing_activated: bool = False
    stop_changes: list[tuple[int, Decimal, bool]] = field(default_factory=list)
    reduction: _Reduction | None = None
    holdings: list[tuple[int, Fraction]] = field(default_factory=list)

    @property
    def side(self) -> int:
        return sign(self.direction)

    @property
    def stop_pending(self) -> bool:
        return self.reduction is not None and self.reduction.reason in {"STOP_LOSS", "TRAILING_STOP"}

    def status(self) -> RoundStatus:
        return RoundStatus(
            round_id=self.round_id, sub_account_id=self.account, instrument_id=self.instrument.instrument_id,
            direction=self.direction, quantity=decimal_exact(self.quantity), stop=self.stop, phase=self.phase,
        )

    def update_entry_phase(self) -> None:
        if self.quantity:
            self.phase = RoundPhase.PARTIALLY_FILLED_PROTECTED if self.entries else RoundPhase.ACTIVE_PROTECTED
        else:
            self.phase = RoundPhase.PENDING_ENTRY if self.entries else RoundPhase.CANCELLED

    def postings(self, *amounts: tuple[str, Fraction]) -> tuple[LedgerPosting, ...]:
        return tuple(
            LedgerPosting(account=account, currency=self.margin.margin_asset, amount=decimal_exact(amount))
            for account, amount in amounts if amount
        )

    def exposure(self) -> tuple[Fraction, Fraction, Decimal]:
        # 每个方向的持仓、在途数量及止损距离分别受限。
        stop_distance = self.stop.copy_negate() if self.side == 1 else self.stop
        return self.quantity, sum((e.remaining for e in self.entries.values()), Fraction(0)), stop_distance

    def assert_reduced(self, before: tuple[Fraction, Fraction, Decimal]) -> None:
        assert all(after <= old for after, old in zip(self.exposure(), before, strict=True))

    def margin_position(self) -> MarginPosition:
        return MarginPosition(
            self.instrument.instrument_id, self.direction, decimal_exact(self.quantity),
            _divide(self.cost / (self.quantity * Fraction(self.margin.contract_multiplier))),
        )

    def set_stop(self, timestamp: int, stop: Decimal, *, trailing: bool) -> None:
        index = bisect_right(self.stop_changes, timestamp, key=lambda change: change[0])
        self.stop_changes.insert(index, (timestamp, stop, trailing))
        _, self.stop, self.trailing = self.stop_changes[-1]

    def stop_at(self, timestamp: int) -> tuple[Decimal, bool]:
        index = bisect_right(self.stop_changes, timestamp, key=lambda item: item[0])
        return self.stop_changes[index - 1][1:] if index else (self.barriers.stop, False)

    def held_at(self, timestamp: int, *, before: bool = False) -> Fraction:
        locate = bisect_left if before else bisect_right
        index = locate(self.holdings, timestamp, key=lambda holding: holding[0])
        return self.holdings[index - 1][1] if index else Fraction(0)

    def exitable_at(self, timestamp: int) -> Fraction:
        index = bisect_right(self.holdings, timestamp, key=lambda holding: holding[0])
        if not index:
            return Fraction(0)
        # 迟到柱只处理当时已持有且此后尚未减掉的数量。
        return min(quantity for _, quantity in self.holdings[index - 1:])

    def record_fill(self, timestamp: int, change: Fraction) -> None:
        index = bisect_right(self.holdings, timestamp, key=lambda holding: holding[0])
        held = self.holdings[index - 1][1] if index else Fraction(0)
        for i in range(index, len(self.holdings)):
            at, quantity = self.holdings[i]
            self.holdings[i] = (at, quantity + change)
        self.holdings.insert(index, (timestamp, held + change))

    def funding_retention(self, timestamp: int, held: Fraction) -> Fraction:
        index = bisect_left(self.holdings, timestamp, key=lambda holding: holding[0])
        retained = Fraction(1) if held else Fraction(0)
        for _, after in self.holdings[index:]:
            if held > after:
                retained *= after / held
            held = after
        return retained


class BarSim1m:
    def __init__(self, context: PluginContext, params: PluginParams | BarSimParams) -> None:
        self._params = BarSimParams.model_validate(params)
        self._pack = get_market_pack(DEFAULT_PACK)
        self._configs: Mapping[str, AccountRiskConfig] = context.account_risk
        self._now = 0
        self._execution_time: int | None = None
        self._rounds: dict[str, PositionExecutor] = {}
        self._live_rounds: dict[str, PositionExecutor] = {}
        self._plans: set[str] = set()
        self._states: dict[str | None, TradingState] = {}
        self._events: list[ExecutionEvent] = []
        self._seen_slots: dict[str, set[int]] = {}
        self._last_prices: dict[str, tuple[int, Decimal]] = {}
        self._last_liquidity: dict[str, _BarLiquidity] = {}
        self._amendments: set[str] = set()
        self._marks: MarkHistory = {}
        self._pricing_marks: MarkHistory = {}
        self._funding: dict[str, dict[int, _Settlement]] = {}
        self._last_funding_ts: dict[str, int] = {}
        self._unpriced: dict[str, set[int]] = {}
        self._cash_delta: dict[str, Fraction] = {}
        self._funding_rechecks: dict[str, int] = {}

    def _state(self, account: str) -> TradingState:
        return max(
            (self._states.get(key, TradingState.ACTIVE) for key in (None, account)),
            key=_STATE_ORDER.index,
        )

    def submit_plan(self, plan: ApprovedExecutionPlan) -> PlanReceipt:
        if not isinstance(plan, ApprovedExecutionPlan):
            raise TypeError("入场须提交 ApprovedExecutionPlan")
        candidate = plan.candidate
        if self._state(candidate.sub_account_id) != TradingState.ACTIVE:
            return PlanReceipt(accepted=False, reason="TRADING_STATE_BLOCKED")
        if plan.plan_id in self._plans:
            return PlanReceipt(accepted=False, reason="DUPLICATE_PLAN")
        ready_at = max(self._now, candidate.ts_decision + self._params.decision_latency_ms)
        if candidate.valid_until is not None and ready_at > candidate.valid_until:
            return PlanReceipt(accepted=False, reason="PLAN_EXPIRED")
        executor = self._rounds.get(plan.round_id)
        if candidate.round_id is not None:
            if (
                executor is None or not executor.quantity or executor.phase == RoundPhase.EXITING
                or candidate.round_id != plan.round_id
                or (candidate.sub_account_id, candidate.instrument_id, candidate.direction, candidate.invalidation_stop)
                != (executor.account, executor.instrument.instrument_id, executor.direction, executor.stop)
            ):
                return PlanReceipt(accepted=False, reason="ADD_ON_ROUND_MISMATCH")
        else:
            if executor is not None:
                return PlanReceipt(accepted=False, reason="DUPLICATE_ROUND")
            instrument = self._pack.instrument(candidate.instrument_id)
            if instrument is None:
                return PlanReceipt(accepted=False, reason="UNKNOWN_INSTRUMENT")
            config = self._configs.get(candidate.sub_account_id, _DEFAULT_RISK)
            barriers = plan.barriers or ExitBarrierSpec(
                stop=candidate.invalidation_stop, take_profit=candidate.take_profit,
                max_holding_ms=config.max_holding_ms, trailing_stop=config.trailing_stop,
            )
            if (
                not 0 < candidate.entry_low <= candidate.entry_high or barriers.stop <= 0
                or barriers.stop != candidate.invalidation_stop
                or any(target <= 0 for target in barriers.take_profit)
            ):
                return PlanReceipt(accepted=False, reason="INVALID_PROTECTION")
            executor = PositionExecutor(
                plan.round_id, candidate.sub_account_id, candidate.direction, instrument,
                self._pack.traits(candidate.instrument_id).margin, config, barriers, barriers.stop,
            )
            self._rounds[plan.round_id] = executor
            self._live_rounds[plan.round_id] = executor
        executor.entries[plan.plan_id] = _Entry(plan, ready_at, Fraction(plan.sizing.quantity))
        self._plans.add(plan.plan_id)
        return PlanReceipt(accepted=True)

    def _emit(self, executor: PositionExecutor, kind: str, *, timestamp: int | None = None, **details) -> None:
        event = ExecutionEvent(
            kind=kind, sub_account_id=executor.account,
            ts_event=self._event_time if timestamp is None else timestamp,
            position=executor.status(), **details,
        )
        self._events.append(event)
        cash = sum((
            Fraction(posting.amount) * (1 if posting.account == "CASH_AVAILABLE" else -1)
            for posting in event.postings if posting.account in {"CASH_AVAILABLE", "REALIZED_PNL"}
        ), Fraction(0))
        # 独立使用插槽时也保留足够的状态做全仓估值。宿主运行时从账本读取。
        self._cash_delta[executor.account] = self._cash_delta.get(executor.account, Fraction(0)) + cash
        if executor.phase in _TERMINAL:
            self._live_rounds.pop(executor.round_id, None)

    def _end_entry(self, executor: PositionExecutor, plan_id: str, kind: str, reason: str) -> None:
        del executor.entries[plan_id]
        if executor.phase != RoundPhase.EXITING:
            executor.update_entry_phase()
        self._emit(executor, kind, plan_id=plan_id, reason=reason)

    def _cancel_entries(self, executor: PositionExecutor, reason: str) -> None:
        before = executor.exposure()
        for plan_id in tuple(executor.entries):
            self._end_entry(executor, plan_id, "PLAN_CANCELLED", reason)
        executor.assert_reduced(before)

    def tighten_barrier(self, req: BarrierChangeRequest) -> BarrierReceipt:
        return self._change_stop(req)

    def apply_approved_amendment(self, amendment: ApprovedBarrierAmendment) -> BarrierReceipt:
        if not isinstance(amendment, ApprovedBarrierAmendment):
            raise TypeError("放宽止损须提交 ApprovedBarrierAmendment")
        if amendment.amendment_id in self._amendments:
            return BarrierReceipt(accepted=False, reason="DUPLICATE_AMENDMENT")
        if self._state(amendment.request.sub_account_id) != TradingState.ACTIVE:
            return BarrierReceipt(accepted=False, reason="TRADING_STATE_BLOCKED")
        receipt = self._change_stop(amendment.request, amendment_id=amendment.amendment_id)
        if receipt.accepted:
            self._amendments.add(amendment.amendment_id)
        return receipt

    def _change_stop(self, request: BarrierChangeRequest, *, amendment_id: str | None = None) -> BarrierReceipt:
        executor = self._rounds.get(request.round_id)
        if executor is None or executor.account != request.sub_account_id or not executor.quantity:
            return BarrierReceipt(accepted=False, reason="ROUND_NOT_OPEN")
        new_stop = request.stop
        if new_stop <= 0 or (Fraction(new_stop) / Fraction(executor.instrument.tick_size)).denominator != 1:
            return BarrierReceipt(accepted=False, reason="ORDER_CONSTRAINT")
        widens = executor.side * (new_stop - executor.stop) < 0
        if amendment_id is None and widens:
            return BarrierReceipt(accepted=False, reason="STOP_WIDENING_REQUIRES_APPROVAL")
        if new_stop != executor.stop:
            before = executor.exposure()
            executor.set_stop(self._now, new_stop, trailing=False)
            if amendment_id is None:
                executor.assert_reduced(before)
            self._emit(
                executor, "BARRIER_TIGHTENED" if amendment_id is None else "BARRIER_AMENDED",
                amendment_id=amendment_id,
            )
        return BarrierReceipt(accepted=True)

    def intervene_position(
        self, round_id: str, action: Intervention, ratio: Decimal | None, reason: str,
    ) -> RoundStatus:
        if action not in ("CANCEL_ENTRY", "CLOSE_ALL", "REDUCE_RATIO"):
            raise ValueError(f"不支持的减风险操作：{action}")
        if action == "REDUCE_RATIO":
            if ratio is None or not ratio.is_finite() or not 0 < ratio <= 1:
                raise ValueError("减仓比例必须大于 0 且不大于 1")
        elif ratio is not None:
            raise ValueError("只有按比例减仓允许指定比例")
        executor = self._rounds[round_id]
        before = executor.exposure()
        if action == "CANCEL_ENTRY":
            self._cancel_entries(executor, reason)
        elif executor.quantity:
            prior = executor.reduction.quantity if executor.reduction is not None else Fraction(0)
            held = executor.quantity - prior
            step = Fraction(executor.instrument.qty_step)
            additional = held if ratio is None else floor(held * Fraction(ratio) / step) * step
            if additional:
                reduction = _Reduction(prior + additional, self._event_time, reason)
                executor.reduction = reduction
                if reduction.quantity == executor.quantity:
                    self._begin_exit(executor, reason)
                last = self._last_prices.get(executor.instrument.instrument_id)
                if (
                    reason != "DRAWDOWN_LIMIT" and self._execution_time is None
                    and last is not None and last[0] == self._now
                ):
                    self._fill_reduction(executor, self._last_liquidity[executor.instrument.instrument_id], last[1])
        elif action == "CLOSE_ALL":
            self._cancel_entries(executor, reason)
        executor.assert_reduced(before)
        return executor.status()

    def set_trading_state(self, sub_account_id: str | None, state: TradingState, reason: str) -> None:
        state = TradingState(state)
        current = self._states.get(sub_account_id, TradingState.ACTIVE)
        if state == TradingState.ACTIVE or _STATE_ORDER.index(state) < _STATE_ORDER.index(current):
            raise ValueError("减风险通道只允许切到更保守的交易状态")
        self._states[sub_account_id] = state
        for executor in tuple(self._live_rounds.values()):
            if sub_account_id is None or executor.account == sub_account_id:
                assert self._state(executor.account) != TradingState.ACTIVE
                self._cancel_entries(executor, reason)

    def _book_price(self, instrument: str, side: int) -> Decimal | None:
        return None

    def _fill_tier(self, executor: PositionExecutor, side: int, *, maker: bool) -> FillTier | None:
        return None

    def _price_with_friction(
        self, executor: PositionExecutor, price: Decimal, side: int, *, quoted: bool = False,
    ) -> Decimal:
        friction = Fraction(executor.config.slippage_pct)
        if not quoted:
            friction += Fraction(self._params.spread_pct) / 2
        raw = Fraction(price) * (1 + side * friction)
        tick = Fraction(executor.instrument.tick_size)
        ticks = ceil(raw / tick) if side == 1 else floor(raw / tick)
        return decimal_exact(max(1, ticks) * tick)

    def _taker_price(self, executor: PositionExecutor, price: Decimal, side: int) -> Decimal:
        quote = self._book_price(executor.instrument.instrument_id, side)
        return self._price_with_friction(executor, price if quote is None else quote, side, quoted=quote is not None)

    def _liquidation_price(self, executor: PositionExecutor, mark: Decimal) -> tuple[Decimal, FillTier | None]:
        return self._price_with_friction(executor, mark, -executor.side), None

    def _match_entry(
        self, executor: PositionExecutor, entry: _Entry, bar: RangeBar, *, at_open: bool,
    ) -> tuple[Decimal, str] | None:
        plan = entry.plan
        candidate = plan.candidate
        side = executor.side
        limit = candidate.entry_high if side == 1 else candidate.entry_low
        order = candidate.order_type
        if not at_open:
            extreme = bar.low if side == 1 else bar.high
            if entry.resting and side * (limit - extreme) > 0:
                return limit, "MAKER"
            return None
        if entry.resting:
            if side * (limit - bar.open) > 0:
                price = bar.open if candidate.entry_low <= bar.open <= candidate.entry_high else limit
                return price, "MAKER"
            return None
        return self._match_arriving_entry(executor, entry, bar.open)

    def _match_arriving_entry(
        self, executor: PositionExecutor, entry: _Entry, reference: Decimal,
    ) -> tuple[Decimal, str] | None:
        plan = entry.plan
        candidate = plan.candidate
        side = executor.side
        limit = candidate.entry_high if side == 1 else candidate.entry_low
        order = candidate.order_type
        book = self._book_price(executor.instrument.instrument_id, side)
        quote = Fraction(book) if book is not None else (
            Fraction(reference) * (1 + side * Fraction(self._params.spread_pct) / 2)
        )
        marketable = side * (Fraction(limit) - quote) >= 0
        if order == "POST_ONLY" and marketable:
            self._end_entry(executor, plan.plan_id, "PLAN_REJECTED", "POST_ONLY_WOULD_TAKE")
            return None
        if order == "MARKET" or order == "LIMIT" and marketable:
            price = self._taker_price(executor, reference, side)
            slip = Fraction(plan.allowed_slippage or 0)
            low, high = Fraction(candidate.entry_low) - slip, Fraction(candidate.entry_high) + slip
            if low <= price <= high and (order == "MARKET" or side * (limit - price) >= 0):
                return price, "TAKER"
            if order == "MARKET" or (price < low if side == 1 else price > high):
                self._end_entry(executor, plan.plan_id, "PLAN_REJECTED", "ENTRY_PRICE_OUTSIDE_APPROVAL")
                return None
        entry.resting = True
        return None

    @property
    def _event_time(self) -> int:
        return self._now if self._execution_time is None else self._execution_time

    def _record_fill(self, executor: PositionExecutor, change: Fraction) -> None:
        at = self._event_time
        executor.record_fill(at, change)
        if self._last_funding_ts.get(executor.instrument.instrument_id, -1) > at:
            self._funding_rechecks[executor.round_id] = min(at, self._funding_rechecks.get(executor.round_id, at))

    def _fill_entry(
        self, executor: PositionExecutor, entry: _Entry, price: Decimal, liquidity: str, qty: Fraction,
    ) -> None:
        notional = qty * Fraction(price) * Fraction(executor.margin.contract_multiplier)
        signed_cost = executor.side * notional
        rate = self._params.maker_fee_pct if liquidity == "MAKER" else executor.config.taker_fee_pct
        fee = notional * Fraction(rate)
        initial_margin = Fraction(_divide(notional / Fraction(executor.config.leverage)))
        executor.quantity += qty
        executor.cost += notional
        executor.margin_used += initial_margin
        self._record_fill(executor, qty)
        if executor.opened_at is None or self._event_time < executor.opened_at:
            executor.opened_at = self._event_time
        entry.remaining -= qty
        if entry.plan.candidate.order_type == "LIMIT":
            entry.resting = True
        complete = not entry.remaining
        if complete:
            del executor.entries[entry.plan.plan_id]
        executor.update_entry_phase()
        self._emit(
            executor, "ENTRY_FILLED", plan_id=entry.plan.plan_id, quantity=decimal_exact(qty),
            price=price, reason=liquidity,
            fill_tier=self._fill_tier(executor, executor.side, maker=liquidity == "MAKER"),
            postings=executor.postings(
                ("POSITION_COST", signed_cost), ("TRADE_PAYABLE", -signed_cost),
                ("MARGIN_USED", initial_margin), ("CASH_AVAILABLE", -initial_margin - fee),
                ("EXPENSE_EXECUTION_FEE", fee),
            ),
        )
        if complete:
            self._emit(executor, "PLAN_COMPLETED", plan_id=entry.plan.plan_id)

    def _begin_exit(self, executor: PositionExecutor, reason: str) -> None:
        if executor.phase != RoundPhase.EXITING:
            executor.phase = RoundPhase.EXITING
            self._emit(executor, "ROUND_EXITING", reason=reason)
        self._cancel_entries(executor, reason)

    def _enforce_holding_deadline(self, executor: PositionExecutor, through: int) -> bool:
        if executor.phase == RoundPhase.EXITING or executor.opened_at is None or not executor.quantity:
            return False
        deadline = executor.opened_at + executor.barriers.max_holding_ms
        if deadline > through:
            return False
        previous = self._execution_time
        self._execution_time = deadline
        executor.reduction = _Reduction(executor.quantity, deadline, "MAX_HOLDING_TIME")
        self._begin_exit(executor, "MAX_HOLDING_TIME")
        self._execution_time = previous
        return True

    def _exit(
        self, executor: PositionExecutor, qty: Fraction, reference: Decimal, reason: str,
        *, maker: bool = False, liquidated: bool = False,
    ) -> None:
        before = executor.exposure()
        assert 0 < qty <= executor.quantity
        if qty == executor.quantity:
            self._begin_exit(executor, reason)
        if liquidated:
            price, tier = self._liquidation_price(executor, reference)
        else:
            price = reference if maker else self._taker_price(executor, reference, -executor.side)
            tier = self._fill_tier(executor, -executor.side, maker=maker)
        notional = qty * Fraction(price) * Fraction(executor.margin.contract_multiplier)
        cost = _allocate(executor.cost, qty, executor.quantity)
        released_margin = _allocate(executor.margin_used, qty, executor.quantity)
        payable = executor.side * notional
        cost_change = -executor.side * cost
        rate = self._params.maker_fee_pct if maker else executor.config.taker_fee_pct
        fee = notional * Fraction(rate)
        penalty = notional * Fraction(self._params.liquidation_fee_pct) if liquidated else Fraction(0)
        executor.isolated_carry -= _allocate(executor.isolated_carry, qty, executor.quantity)
        executor.quantity -= qty
        executor.cost -= cost
        executor.margin_used -= released_margin
        self._record_fill(executor, -qty)
        executor.reduction = None
        if not executor.quantity:
            executor.phase = RoundPhase.LIQUIDATED if liquidated else RoundPhase.CLOSED
        self._emit(
            executor, "EXIT_FILLED", quantity=decimal_exact(qty), price=price, reason=reason,
            fill_tier=tier,
            postings=executor.postings(
                ("TRADE_PAYABLE", payable), ("POSITION_COST", cost_change),
                ("REALIZED_PNL", -(payable + cost_change)),
                ("MARGIN_USED", -released_margin), ("EXPENSE_EXECUTION_FEE", fee),
                ("EXPENSE_LIQUIDATION_FEE", penalty), ("CASH_AVAILABLE", released_margin - fee - penalty),
            ),
        )
        if liquidated and not executor.quantity:
            self._emit(executor, "ROUND_LIQUIDATED", reason=reason)
        executor.assert_reduced(before)

    def _exit_quantity(self, executor: PositionExecutor, liquidity: _BarLiquidity, quantity: Fraction) -> Fraction:
        step = Fraction(executor.instrument.qty_step)
        return floor(min(liquidity.exit, quantity) / step) * step

    def _fill_reduction(self, executor: PositionExecutor, liquidity: _BarLiquidity, price: Decimal) -> bool:
        request = executor.reduction
        qty = self._exit_quantity(executor, liquidity, min(request.quantity, executor.exitable_at(self._event_time)))
        if not qty:
            return False
        if request.quantity == executor.quantity:
            self._begin_exit(executor, request.reason)
        liquidity.exit -= qty
        self._exit(executor, qty, price, request.reason)
        if request.quantity > qty:
            executor.reduction = _Reduction(request.quantity - qty, request.requested_at, request.reason)
        return True

    def _protect(
        self, executor: PositionExecutor, liquidity: _BarLiquidity, reference: Decimal,
        reason: str, *, maker: bool = False,
    ) -> bool:
        held = executor.exitable_at(self._event_time)
        if not held:
            return False
        qty = self._exit_quantity(executor, liquidity, held)
        if not maker and held == executor.quantity:
            executor.reduction = _Reduction(held, self._event_time, reason)
            self._begin_exit(executor, reason)
        elif qty:
            self._cancel_entries(executor, reason)
        if qty:
            liquidity.exit -= qty
            request = executor.reduction
            self._exit(executor, qty, reference, reason, maker=maker)
            if request is not None and request.quantity > qty:
                executor.reduction = _Reduction(request.quantity - qty, request.requested_at, request.reason)
        return bool(qty) or not maker

    def _match_entries(
        self, executor: PositionExecutor, slot: int, liquidity: _BarLiquidity,
        match_order: Callable[[_Entry], tuple[Decimal, str] | None], *, one_per_slot: bool = True,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        step = Fraction(executor.instrument.qty_step)
        for entry in tuple(executor.entries.values()):
            if (
                entry.plan.plan_id not in executor.entries or slot < entry.ready_at
                or one_per_slot and entry.last_fill_slot == slot
            ):
                continue
            expiry = entry.plan.candidate.valid_until
            if expiry is not None and self._event_time > expiry:
                continue
            match = match_order(entry)
            if entry.plan.plan_id not in executor.entries:
                yield ()
            qty = floor(min(liquidity.entry, entry.remaining) / step) * step
            if match is not None and qty >= Fraction(executor.instrument.min_qty):
                self._fill_entry(executor, entry, *match, qty)
                entry.last_fill_slot = slot
                liquidity.entry -= qty
                yield ()

    def _bar_actions(
        self, bar: RangeBar, instrument: str, liquidity: _BarLiquidity,
        stage: str, open_positions: set[str], protected: set[str],
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        for executor in tuple(self._live_rounds.values()):
            if executor.instrument.instrument_id != instrument or executor.round_id in protected:
                continue
            side = executor.side
            adverse, favorable = (bar.low, bar.high) if side == 1 else (bar.high, bar.low)
            stop, trailing = executor.stop_at(self._event_time)
            stop_reason = "TRAILING_STOP" if trailing else "STOP_LOSS"
            target = min(executor.barriers.take_profit, key=lambda price: side * price)
            if stage in {"open", "adverse"} and self._enforce_holding_deadline(executor, self._event_time):
                yield ()
            if stage == "open":
                if executor.stop_pending and executor.reduction.requested_at <= self._event_time:
                    if self._fill_reduction(executor, liquidity, bar.open):
                        yield ()
                    protected.add(executor.round_id)
                    continue
                yield from self._match_entries(
                    executor, bar.ts_event, liquidity, lambda entry: self._match_entry(executor, entry, bar, at_open=True),
                )
                if not executor.held_at(self._event_time):
                    continue
                open_positions.add(executor.round_id)
                if side * (bar.open - stop) <= 0:
                    self._protect(executor, liquidity, bar.open, stop_reason)
                    protected.add(executor.round_id)
                    yield ()
                    continue
                if side * (bar.open - target) > 0:
                    if self._protect(executor, liquidity, bar.open, "TAKE_PROFIT", maker=True):
                        yield ()
                    if not executor.quantity:
                        continue
                request = executor.reduction
                if request is not None and request.requested_at <= self._event_time:
                    hard_stop_ahead = side * (adverse - stop) <= 0
                    profit_ahead = side * (favorable - target) > 0
                    wait_for_barriers = (
                        request.reason in {"DRAWDOWN_LIMIT", "MAX_HOLDING_TIME"} and hard_stop_ahead
                        or request.reason == "MAX_HOLDING_TIME" and profit_ahead
                    )
                    if not wait_for_barriers and self._fill_reduction(executor, liquidity, bar.open):
                        yield ()
            elif stage == "adverse":
                yield from self._match_entries(
                    executor, bar.ts_event, liquidity, lambda entry: self._match_entry(executor, entry, bar, at_open=False),
                )
                if executor.held_at(self._event_time) and side * (adverse - stop) <= 0:
                    self._protect(executor, liquidity, stop, stop_reason)
                    protected.add(executor.round_id)
                    yield ()
            elif executor.held_at(self._event_time) and executor.quantity:
                was_open = executor.round_id in open_positions
                if stage == "favorable":
                    price = favorable if was_open else bar.close
                    if side * (price - target) > 0:
                        if self._protect(executor, liquidity, target, "TAKE_PROFIT", maker=True):
                            yield ()
                    if executor.quantity and executor.phase != RoundPhase.EXITING:
                        tightened = self._ratchet(executor, price)
                        if tightened:
                            yield ()
                            if side * (adverse - tightened) <= 0:
                                self._protect(executor, liquidity, tightened, "TRAILING_STOP")
                                yield ()
                if stage == "close" and executor.quantity:
                    earlier_stop, _ = executor.stop_at(bar.ts_event + 2)
                    if stop != earlier_stop and side * (bar.close - stop) <= 0:
                        self._protect(executor, liquidity, bar.close, stop_reason)
                        yield ()
                        continue
                    request = executor.reduction
                    if (
                        request is not None and bar.ts_event < request.requested_at <= self._event_time
                        and request.reason not in {"DRAWDOWN_LIMIT", "MAX_HOLDING_TIME", "STOP_LOSS", "TRAILING_STOP"}
                        and self._fill_reduction(executor, liquidity, bar.close)
                    ):
                        yield ()

    def _ratchet(self, executor: PositionExecutor, favorable: Decimal) -> Decimal | None:
        spec = executor.barriers.trailing_stop
        if spec is None:
            return
        side = executor.side
        entry = executor.cost / (executor.quantity * Fraction(executor.margin.contract_multiplier))
        price = Fraction(favorable)
        if not executor.trailing_activated and side * (price - entry) < entry * Fraction(spec.activation_pct):
            return
        executor.trailing_activated = True
        stop = price * (1 - side * Fraction(spec.distance_pct))
        tick = Fraction(executor.instrument.tick_size)
        ticks = floor(stop / tick) if side == 1 else ceil(stop / tick)
        stop = decimal_exact(ticks * tick)
        if side * (stop - executor.stop) > 0:
            before = executor.exposure()
            executor.set_stop(self._event_time, stop, trailing=True)
            executor.assert_reduced(before)
            self._emit(executor, "BARRIER_TIGHTENED", reason="TRAILING_STOP")
            return stop
        return None

    @staticmethod
    def _observe_mark(marks: MarkHistory, point: DerivativePoint) -> None:
        history = marks.setdefault(point.instrument_id, [])
        at = point.observation_time
        index = bisect_left(history, at, key=lambda item: item[0])
        if index < len(history) and history[index][0] == at:
            previous = history[index][1]
            if (point.revision, point.ts_available, point.ts_committed) > (
                previous.revision, previous.ts_available, previous.ts_committed,
            ):
                history[index] = at, point
        else:
            history.insert(index, (at, point))

    @staticmethod
    def _mark_at(marks: MarkHistory, instrument: str, at: int, max_age: int) -> Decimal | None:
        history = marks.get(instrument, ())
        index = bisect_right(history, at, key=lambda item: item[0])
        if index:
            timestamp, point = history[index - 1]
            if at - timestamp <= max_age:
                return point.value
        return None

    def _book_funding(
        self, executor: PositionExecutor, settlement: _Settlement, reason: str | None = None,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        point = settlement.point
        at = point.ts_event
        held = executor.held_at(at, before=True)
        booking = settlement.bookings.get(executor.round_id)
        if not held and booking is None:
            return
        price = booking.price if booking is not None else None
        if price is None:
            price = self._mark_at(self._pricing_marks, point.instrument_id, at, executor.config.mark_max_age_ms)
        if price is None:
            if booking is None:
                settlement.bookings[executor.round_id] = _FundingBooking(None)
                settlement.unpriced.add(executor.round_id)
                self._unpriced.setdefault(point.instrument_id, set()).add(at)
                self._emit(executor, "FUNDING_UNPRICED", timestamp=at, settlement_ts=at,
                           reason="MARK_PRICE_UNAVAILABLE")
                yield ()
            return
        cash = -executor.side * held * Fraction(price) * Fraction(executor.margin.contract_multiplier) * Fraction(point.value)
        delta = cash - (booking.cash if booking is not None else 0)
        settlement.bookings[executor.round_id] = _FundingBooking(price, cash)
        settlement.unpriced.discard(executor.round_id)
        if not settlement.unpriced:
            self._unpriced.get(point.instrument_id, set()).discard(at)
        if booking is not None and booking.price is not None and not delta:
            return
        if executor.config.margin_mode == "ISOLATED" and executor.quantity:
            retained = executor.funding_retention(at, held)
            executor.isolated_carry += _allocate(delta, retained, Fraction(1))
        self._emit(
            executor, "FUNDING_SETTLED", timestamp=at, settlement_ts=at, reason=reason,
            postings=executor.postings(("CASH_AVAILABLE", delta), ("FUNDING", -delta)),
        )
        yield ()

    def _settle_funding(self, point: DerivativePoint) -> Iterator[tuple[DerivativePoint, ...]]:
        history = self._funding.setdefault(point.instrument_id, {})
        settlement = history.get(point.ts_event)
        initial = settlement is None
        if initial:
            settlement = history[point.ts_event] = _Settlement(point)
            self._last_funding_ts[point.instrument_id] = max(point.ts_event, self._last_funding_ts.get(point.instrument_id, 0))
        elif point.revision > settlement.point.revision:
            settlement.point = point
        else:
            return
        for executor in self._rounds.values():
            if executor.instrument.instrument_id == point.instrument_id:
                yield from self._book_funding(executor, settlement, None if initial else "FUNDING_RATE_REVISION")

    def _retry_funding_prices(self, instrument: str) -> Iterator[tuple[DerivativePoint, ...]]:
        for at in sorted(self._unpriced.get(instrument, ())):
            settlement = self._funding[instrument][at]
            for round_id in sorted(settlement.unpriced):
                yield from self._book_funding(self._rounds[round_id], settlement)

    def _reconcile_funding(self) -> Iterator[tuple[DerivativePoint, ...]]:
        for round_id, fill_at in tuple(self._funding_rechecks.items()):
            del self._funding_rechecks[round_id]
            executor = self._rounds[round_id]
            history = self._funding.get(executor.instrument.instrument_id, {})
            for at in sorted(at for at in history if fill_at < at):
                yield from self._book_funding(executor, history[at], "FUNDING_POSITION_CORRECTION")

    def _collateral(self, executor: PositionExecutor, balances: BalanceReader | None) -> Decimal:
        config = executor.config
        if balances is not None:
            return collateral_balance(balances(executor.account), config.currency)
        allocated = sum((
            item.margin_used for item in self._live_rounds.values() if item.account == executor.account
        ), Fraction(0))
        return decimal_exact(
            Fraction(config.initial_capital) + self._cash_delta.get(executor.account, Fraction(0)) + allocated,
        )

    def _liquidations(
        self, valuation: MarginValuation, balances: BalanceReader | None,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        at = self._event_time
        accounts: dict[str, list[PositionExecutor]] = {}
        for executor in self._live_rounds.values():
            if executor.quantity and executor.held_at(at):
                accounts.setdefault(executor.account, []).append(executor)
        for executors in accounts.values():
            config = executors[0].config
            marks = {
                executor.round_id: self._mark_at(
                    self._marks, executor.instrument.instrument_id, at, config.mark_max_age_ms,
                ) for executor in executors
            }
            marked = [executor for executor in executors if marks[executor.round_id] is not None]
            if config.margin_mode == "CROSS":
                if len(marked) != len(executors):
                    continue
                estimate = valuation.cross_margin(
                    MarginPortfolio(
                        self._collateral(executors[0], balances), config.leverage,
                        tuple(executor.margin_position() for executor in executors),
                    ),
                    {executor.instrument.instrument_id: marks[executor.round_id] for executor in executors},
                    as_of=self._event_time,
                )
                breached = executors if estimate.equity <= estimate.maintenance_margin else ()
            else:
                breached = [
                    executor for executor in marked
                    if self._isolated_breached(executor, marks[executor.round_id], valuation)
                ]
            for executor in breached:
                qty = executor.exitable_at(at)
                if qty:
                    self._exit(executor, qty, marks[executor.round_id], "MARGIN_LIQUIDATION", liquidated=True)
                    yield ()

    def _isolated_breached(self, executor: PositionExecutor, mark: Decimal, valuation: MarginValuation) -> bool:
        position = executor.margin_position()
        collateral = decimal_exact(executor.margin_used + executor.isolated_carry)
        if collateral < 0:
            estimate = valuation.cross_margin(
                MarginPortfolio(collateral, executor.config.leverage, (position,)),
                {position.instrument_id: mark}, as_of=self._event_time,
            )
            return estimate.equity <= estimate.maintenance_margin
        boundary = valuation.isolated_liquidation_price(
            position.instrument_id, position.direction, position.quantity, position.entry_price,
            collateral, as_of=self._event_time,
        )
        return executor.side * (mark - boundary) <= 0

    def on_market(self, event: MarketEvent, valuation: MarginValuation | None) -> None:
        for _ in self.market_steps(event, valuation):
            pass

    def _confirmed_bar(self, event: MarketEvent) -> RangeBar | None:
        bar = event.bar
        if (
            event.timeframe != "1m" or bar is None or not bar.confirmed or bar.volume <= 0
            or not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            or max(bar.ts_event + _MINUTE, bar.ts_available) > self._now
        ):
            return None
        seen = self._seen_slots.setdefault(event.instrument_id, set())
        if bar.ts_event in seen:
            return None
        seen.add(bar.ts_event)
        return bar

    def _advance_barriers(self, through: int) -> None:
        for executor in tuple(self._live_rounds.values()):
            for plan_id, entry in tuple(executor.entries.items()):
                expiry = entry.plan.candidate.valid_until
                if expiry is not None and through > expiry:
                    self._execution_time = expiry + 1
                    self._end_entry(executor, plan_id, "PLAN_EXPIRED", "ENTRY_TIMEOUT")
            self._enforce_holding_deadline(executor, through)

    def _matching_timeline(
        self, event: MarketEvent, bar: RangeBar | None,
    ) -> Iterable[tuple[int, str | Trade | Quote]]:
        if bar is not None:
            # OHLC 不含逐笔时点。盘中阶段用开盘后的有序时间槽表示。
            return (
                (bar.ts_event, "open"),
                (bar.ts_event + 1, "adverse"),
                (bar.ts_event + 2, "favorable"),
                (bar.ts_event + _MINUTE, "close"),
            )
        return ()

    def _tick_actions(self, tick: Trade | Quote) -> Iterator[tuple[DerivativePoint, ...]]:
        return iter(())

    def market_steps(
        self, event: MarketEvent, valuation: MarginValuation | None, *, balances: BalanceReader | None = None,
        execution_through: int | None = None,
        pricing_only: bool = False,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        """按事件时点推进成交，逐笔交回宿主入账并检查熔断。"""
        if event.as_of < self._now:
            raise ValueError("模拟成交时钟不得倒退")
        self._now = event.as_of
        points = [point for point in event.derivative_points if point.visible_at(self._now)]
        priced = set()
        for point in points:
            if point.kind == DerivativeKind.MARK_PRICE and point.value > 0:
                self._observe_mark(self._pricing_marks, point)
                priced.add(point.instrument_id)
        if pricing_only:
            for instrument in sorted(priced):
                yield from self._retry_funding_prices(instrument)
            yield ()
            return
        bar = self._confirmed_bar(event)
        observations = latest_visible_derivatives(
            point for point in points if point.kind != DerivativeKind.MARK_PRICE and not point.is_funding_settlement
        )
        observations.extend(sorted(
            (point for point in points if point.kind == DerivativeKind.MARK_PRICE or point.is_funding_settlement),
            key=lambda point: (point.observation_time, point.ts_available, point.revision),
        ))
        timeline = sorted(((point.observation_time, point) for point in observations), key=lambda step: step[0])
        if bar is not None:
            volume = Fraction(bar.volume)
            liquidity = _BarLiquidity(
                volume * Fraction(self._params.volume_participation),
                volume * Fraction(self._params.exit_volume_participation or self._params.volume_participation),
            )
        open_positions: set[str] = set()
        protected: set[str] = set()
        try:
            for timestamp, item in merge(timeline, self._matching_timeline(event, bar), key=lambda step: step[0]):
                self._execution_time = timestamp
                if isinstance(item, DerivativePoint):
                    if item.kind == DerivativeKind.MARK_PRICE and item.value > 0:
                        self._observe_mark(self._marks, item)
                    yield (item,)
                    if item.is_funding_settlement:
                        yield from self._settle_funding(item)
                    elif item.kind == DerivativeKind.MARK_PRICE:
                        yield from self._retry_funding_prices(item.instrument_id)
                    if valuation is not None:
                        yield from self._liquidations(valuation, balances)
                elif isinstance(item, str):
                    yield from self._bar_actions(bar, event.instrument_id, liquidity, item, open_positions, protected)
                    if valuation is not None:
                        yield from self._liquidations(valuation, balances)
                    if item == "close":
                        last = self._last_prices.get(event.instrument_id)
                        if last is None or timestamp > last[0]:
                            self._last_prices[event.instrument_id] = timestamp, bar.close
                            self._last_liquidity[event.instrument_id] = liquidity
                else:
                    for points in self._tick_actions(item):
                        yield points
                        if valuation is not None:
                            yield from self._liquidations(valuation, balances)
                yield from self._reconcile_funding()
            self._advance_barriers(self._now if execution_through is None else execution_through)
            self._execution_time = None
            yield ()
        finally:
            self._execution_time = None

    def poll_events(self, up_to_ts: int) -> list[ExecutionEvent]:
        ready = [event for event in self._events if event.ts_event <= up_to_ts]
        self._events = [event for event in self._events if event.ts_event > up_to_ts]
        return ready


bar_sim_1m = SlotPlugin(Slot.FILL, (INTERFACE_VERSION,), BarSim1m, BarSimParams)
