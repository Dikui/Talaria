"""按确认的 1m 行情模拟成交并托管每个仓位回合。"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
from math import ceil, floor

from pydantic import Field, StrictInt, field_validator

from talaria.account.contracts import (
    ApprovedBarrierAmendment, ApprovedExecutionPlan, BarrierChangeRequest, BarrierReceipt,
    Direction, ExecutionEvent, ExitBarrierSpec, Intervention, LedgerPosting, MarginValuation,
    MarketEvent, PlanReceipt, RoundPhase, RoundStatus, TradingState,
)
from talaria.account.plugins.risk_math import collateral_balance, sign
from talaria.account.risk_config import AccountRiskConfig, Positive, Rate
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, Slot, SlotPlugin
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint, latest_visible_derivatives
from talaria.foundation.margin import MarginPortfolio, MarginPosition
from talaria.foundation.market import InstrumentSpec, MarginTrait
from talaria.foundation.types import ContractModel, decimal_from_finite_fraction as decimal_exact
from talaria.packs import DEFAULT_PACK, get_market_pack

_MINUTE = 60_000
_DEFAULT_RISK = AccountRiskConfig()
_TERMINAL = {RoundPhase.CLOSED, RoundPhase.CANCELLED, RoundPhase.LIQUIDATED}
_STATE_ORDER = (TradingState.ACTIVE, TradingState.REDUCING, TradingState.HALTED)
_BASIS_CONTEXT = Context(prec=50, rounding=ROUND_HALF_EVEN)
type BalanceReader = Callable[[str], Mapping[tuple[str, str], Decimal]]


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
    liquidation_fee_pct: Rate = Decimal(0)

    @field_validator("spread_pct", "maker_fee_pct", "volume_participation", "liquidation_fee_pct", mode="before")
    @classmethod
    def _decimal_string(cls, value: object) -> Decimal:
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


@dataclass(frozen=True, slots=True)
class _Reduction:
    quantity: Fraction
    requested_at: int
    reason: str


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
        self.stop, self.trailing = stop, trailing
        self.stop_changes.append((timestamp, stop, trailing))

    def stop_at(self, timestamp: int) -> tuple[Decimal, bool]:
        index = bisect_right(self.stop_changes, timestamp, key=lambda item: item[0])
        return self.stop_changes[index - 1][1:] if index else (self.barriers.stop, False)


class BarSim1m:
    def __init__(self, context: PluginContext, params: PluginParams) -> None:
        self._params = BarSimParams.model_validate(params)
        self._pack = get_market_pack(DEFAULT_PACK)
        self._configs: Mapping[str, AccountRiskConfig] = context.account_risk
        self._now = 0
        self._rounds: dict[str, PositionExecutor] = {}
        self._live_rounds: dict[str, PositionExecutor] = {}
        self._plans: set[str] = set()
        self._states: dict[str | None, TradingState] = {}
        self._events: list[ExecutionEvent] = []
        self._last_slots: dict[str, int] = {}
        self._last_prices: dict[str, tuple[int, Decimal]] = {}
        self._amendments: set[str] = set()
        self._marks: dict[str, list[tuple[int, DerivativePoint]]] = {}
        self._funding: dict[tuple[str, int], DerivativePoint] = {}
        self._cash_delta: dict[str, Fraction] = {}

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

    def _emit(self, executor: PositionExecutor, kind: str, **details) -> None:
        event = ExecutionEvent(
            kind=kind, sub_account_id=executor.account, ts_event=self._now,
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
                reduction = _Reduction(prior + additional, self._now, reason)
                executor.reduction = reduction
                if reduction.quantity == executor.quantity:
                    self._begin_exit(executor, reason)
                last = self._last_prices.get(executor.instrument.instrument_id)
                if last is not None and last[0] == self._now:
                    self._exit(executor, reduction.quantity, last[1], reason)
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

    def _taker_price(self, executor: PositionExecutor, price: Decimal, side: int) -> Decimal:
        friction = Fraction(self._params.spread_pct) / 2 + Fraction(executor.config.slippage_pct)
        raw = Fraction(price) * (1 + side * friction)
        tick = Fraction(executor.instrument.tick_size)
        ticks = ceil(raw / tick) if side == 1 else floor(raw / tick)
        return decimal_exact(max(1, ticks) * tick)

    def _match_entry(
        self, executor: PositionExecutor, entry: _Entry, bar: RangeBar,
    ) -> tuple[Decimal, str] | None:
        plan = entry.plan
        candidate = plan.candidate
        side = executor.side
        limit = candidate.entry_high if side == 1 else candidate.entry_low
        quote = Fraction(bar.open) * (1 + side * Fraction(self._params.spread_pct) / 2)
        marketable = side * (Fraction(limit) - quote) >= 0
        order = candidate.order_type
        if order == "POST_ONLY" and not entry.resting and marketable:
            self._end_entry(executor, plan.plan_id, "PLAN_REJECTED", "POST_ONLY_WOULD_TAKE")
            return None
        at_market = not entry.resting and marketable and order == "LIMIT"
        if order == "MARKET" or at_market:
            price = self._taker_price(executor, bar.open, side)
            slip = Fraction(plan.allowed_slippage or 0)
            low, high = Fraction(candidate.entry_low) - slip, Fraction(candidate.entry_high) + slip
            if low <= price <= high and (order == "MARKET" or side * (limit - price) >= 0):
                return price, "TAKER"
            if order == "MARKET" or (price < low if side == 1 else price > high):
                self._end_entry(executor, plan.plan_id, "PLAN_REJECTED", "ENTRY_PRICE_OUTSIDE_APPROVAL")
                return None
        entry.resting = True
        extreme = bar.low if side == 1 else bar.high
        if side * (limit - extreme) > 0 and (not at_market or side * (bar.open - extreme) > 0):
            return limit, "MAKER"
        return None

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
        executor.holdings.append((self._now, executor.quantity))
        if executor.opened_at is None:
            executor.opened_at = self._now
        entry.remaining -= qty
        complete = not entry.remaining
        if complete:
            del executor.entries[entry.plan.plan_id]
        executor.update_entry_phase()
        self._emit(
            executor, "ENTRY_FILLED", plan_id=entry.plan.plan_id, quantity=decimal_exact(qty),
            price=price, reason=liquidity, postings=executor.postings(
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

    def _exit(
        self, executor: PositionExecutor, qty: Fraction, reference: Decimal, reason: str,
        *, maker: bool = False, liquidated: bool = False,
    ) -> None:
        before = executor.exposure()
        assert 0 < qty <= executor.quantity
        if qty == executor.quantity:
            self._begin_exit(executor, reason)
        price = reference if maker or liquidated else self._taker_price(executor, reference, -executor.side)
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
        executor.holdings.append((self._now, executor.quantity))
        executor.reduction = None
        if not executor.quantity:
            executor.phase = RoundPhase.LIQUIDATED if liquidated else RoundPhase.CLOSED
        self._emit(
            executor, "EXIT_FILLED", quantity=decimal_exact(qty), price=price, reason=reason,
            postings=executor.postings(
                ("TRADE_PAYABLE", payable), ("POSITION_COST", cost_change),
                ("REALIZED_PNL", -(payable + cost_change)),
                ("MARGIN_USED", -released_margin), ("EXPENSE_EXECUTION_FEE", fee),
                ("EXPENSE_LIQUIDATION_FEE", penalty), ("CASH_AVAILABLE", released_margin - fee - penalty),
            ),
        )
        if liquidated:
            self._emit(executor, "ROUND_LIQUIDATED", reason=reason)
        executor.assert_reduced(before)

    def _bar_exit(self, executor: PositionExecutor, bar: RangeBar, *, intrabar_entry: bool = False) -> bool:
        if not executor.quantity:
            return False
        side = executor.side
        adverse, favorable = (bar.low, bar.high) if side == 1 else (bar.high, bar.low)
        stop, trailing = executor.stop_at(bar.ts_event)
        reduction = executor.reduction
        reduced = False
        if reduction is not None and reduction.requested_at <= bar.ts_event and side * (bar.open - stop) > 0:
            self._exit(executor, reduction.quantity, bar.open, reduction.reason)
            if not executor.quantity:
                return True
            reduction = None
            reduced = True
        if side * (adverse - stop) <= 0:
            reference = bar.open if side * (bar.open - stop) < 0 else stop
            reason = "TRAILING_STOP" if trailing else "STOP_LOSS"
            self._exit(executor, executor.quantity, reference, reason)
            return True
        close = bar.ts_event + _MINUTE
        close_stop, close_trailing = executor.stop_at(close)
        if close_stop != stop and side * (bar.close - close_stop) <= 0:
            reason = "TRAILING_STOP" if close_trailing else "STOP_LOSS"
            self._exit(executor, executor.quantity, bar.close, reason)
            return True
        if reduction is not None and reduction.requested_at <= close:
            self._exit(executor, reduction.quantity, bar.close, reduction.reason)
            return True
        target = min(executor.barriers.take_profit, key=lambda price: side * price)
        # 挂单盘中成交时，收盘价才能证明有利行情在入场之后。
        if intrabar_entry:
            favorable = bar.close
        if side * (favorable - target) > 0:
            self._exit(executor, executor.quantity, target, "TAKE_PROFIT", maker=True)
            return True
        return reduced

    def _ratchet(self, executor: PositionExecutor, favorable: Decimal) -> None:
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
            executor.set_stop(self._now, stop, trailing=True)
            executor.assert_reduced(before)
            self._emit(executor, "BARRIER_TIGHTENED", reason="TRAILING_STOP")

    def _observe_mark(self, point: DerivativePoint) -> None:
        history = self._marks.setdefault(point.instrument_id, [])
        # 晚到的旧观测可以在当时参与估值，不能替换已经知道的更新观测。
        at = max(point.ts_available, history[-1][0]) if history else point.ts_available
        if history:
            previous = history[-1][1]
            if (point.observation_time, point.revision, point.ts_available) <= (
                previous.observation_time, previous.revision, previous.ts_available,
            ):
                return
        history.append((at, point))

    def _mark_at(self, instrument: str, at: int, max_age: int) -> Decimal | None:
        history = self._marks.get(instrument, ())
        index = bisect_right(history, at, key=lambda item: item[0])
        if index:
            point = history[index - 1][1]
            if at - point.observation_time <= max_age:
                return point.value
        return None

    def _settle_funding(self, point: DerivativePoint) -> Iterator[tuple[DerivativePoint, ...]]:
        key = point.instrument_id, point.ts_event
        previous = self._funding.get(key)
        if previous is not None and point.revision <= previous.revision:
            return
        self._funding[key] = point
        rate = Fraction(point.value) - (Fraction(previous.value) if previous is not None else 0)
        for executor in self._rounds.values():
            if executor.instrument.instrument_id != point.instrument_id:
                continue
            index = bisect_left(executor.holdings, point.ts_event, key=lambda item: item[0])
            if not index or not (held := executor.holdings[index - 1][1]):
                continue
            mark = self._mark_at(point.instrument_id, point.ts_event, executor.config.mark_max_age_ms)
            if mark is None:
                self._emit(executor, "FUNDING_UNPRICED", settlement_ts=point.ts_event, reason="MARK_PRICE_UNAVAILABLE")
            else:
                cash = -executor.side * held * Fraction(mark) * Fraction(executor.margin.contract_multiplier) * rate
                if executor.config.margin_mode == "ISOLATED" and executor.quantity:
                    executor.isolated_carry += _allocate(cash, min(held, executor.quantity), held)
                self._emit(
                    executor, "FUNDING_SETTLED", settlement_ts=point.ts_event,
                    reason="FUNDING_RATE_REVISION" if previous is not None else None,
                    postings=executor.postings(("CASH_AVAILABLE", cash), ("FUNDING", -cash)),
                )
            yield ()

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
        accounts: dict[str, list[PositionExecutor]] = {}
        for executor in self._live_rounds.values():
            if executor.quantity:
                accounts.setdefault(executor.account, []).append(executor)
        for executors in accounts.values():
            config = executors[0].config
            marks = {
                executor.round_id: self._mark_at(
                    executor.instrument.instrument_id, self._now, config.mark_max_age_ms,
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
                    as_of=self._now,
                )
                breached = executors if estimate.equity <= estimate.maintenance_margin else ()
            else:
                breached = [
                    executor for executor in marked
                    if self._isolated_breached(executor, marks[executor.round_id], valuation)
                ]
            for executor in breached:
                if executor.quantity:
                    self._exit(executor, executor.quantity, marks[executor.round_id], "MARGIN_LIQUIDATION", liquidated=True)
                    yield ()

    def _isolated_breached(self, executor: PositionExecutor, mark: Decimal, valuation: MarginValuation) -> bool:
        position = executor.margin_position()
        collateral = decimal_exact(executor.margin_used + executor.isolated_carry)
        if collateral < 0:
            estimate = valuation.cross_margin(
                MarginPortfolio(collateral, executor.config.leverage, (position,)),
                {position.instrument_id: mark}, as_of=self._now,
            )
            return estimate.equity <= estimate.maintenance_margin
        boundary = valuation.isolated_liquidation_price(
            position.instrument_id, position.direction, position.quantity, position.entry_price,
            collateral, as_of=self._now,
        )
        return executor.side * (mark - boundary) <= 0

    def on_market(self, event: MarketEvent, valuation: MarginValuation | None) -> None:
        for _ in self.market_steps(event, valuation):
            pass

    def market_steps(
        self, event: MarketEvent, valuation: MarginValuation | None, *, balances: BalanceReader | None = None,
    ) -> Iterator[tuple[DerivativePoint, ...]]:
        """宿主在每次让出后入账并检查熔断，再继续下一笔撮合。"""
        if event.as_of < self._now:
            raise ValueError("模拟成交时钟不得倒退")
        self._now = event.as_of
        points = tuple(point for point in event.derivative_points if point.visible_at(self._now))
        observations = sorted(latest_visible_derivatives(points), key=lambda point: point.ts_available)
        for point in observations:
            if point.kind is DerivativeKind.MARK_PRICE and point.value > 0:
                self._observe_mark(point)
        yield tuple(observations)
        for point in observations:
            if point.is_funding_settlement:
                yield from self._settle_funding(point)
        if valuation is not None:
            yield from self._liquidations(valuation, balances)
        for executor in tuple(self._live_rounds.values()):
            for plan_id, entry in tuple(executor.entries.items()):
                expiry = entry.plan.candidate.valid_until
                if expiry is not None and self._now > expiry:
                    self._end_entry(executor, plan_id, "PLAN_EXPIRED", "ENTRY_TIMEOUT")
            if executor.quantity and executor.phase != RoundPhase.EXITING:
                deadline = executor.opened_at + executor.barriers.max_holding_ms
                if self._now >= deadline:
                    executor.reduction = _Reduction(executor.quantity, deadline, "MAX_HOLDING_TIME")
                    self._begin_exit(executor, "MAX_HOLDING_TIME")
        bar = event.bar
        if (
            event.timeframe != "1m" or bar is None or not bar.confirmed or bar.volume <= 0
            or not 0 < bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
            or not max(bar.ts_event + _MINUTE, bar.ts_available) <= self._now
            or bar.ts_event <= self._last_slots.get(event.instrument_id, -1)
        ):
            yield ()
            return
        self._last_slots[event.instrument_id] = bar.ts_event
        self._last_prices[event.instrument_id] = bar.ts_event + _MINUTE, bar.close
        budget = Fraction(bar.volume) * Fraction(self._params.volume_participation)
        for executor in tuple(self._live_rounds.values()):
            if executor.instrument.instrument_id != event.instrument_id or executor.phase in _TERMINAL:
                continue
            was_open = bool(executor.quantity)
            if was_open and self._bar_exit(executor, bar):
                yield ()
                if valuation is not None:
                    yield from self._liquidations(valuation, balances)
                continue
            step = Fraction(executor.instrument.qty_step)
            intrabar_entry = False
            for plan_id, entry in tuple(executor.entries.items()):
                if plan_id not in executor.entries or bar.ts_event < entry.ready_at:
                    continue
                match = self._match_entry(executor, entry, bar)
                qty = floor(min(budget, entry.remaining) / step) * step
                if match is not None and qty >= Fraction(executor.instrument.min_qty):
                    self._fill_entry(executor, entry, *match, qty)
                    budget -= qty
                    yield ()
                    if valuation is not None:
                        yield from self._liquidations(valuation, balances)
                    if not was_open:
                        intrabar_entry = match[1] == "MAKER"
                    if not was_open and self._bar_exit(executor, bar, intrabar_entry=intrabar_entry):
                        yield ()
                        if valuation is not None:
                            yield from self._liquidations(valuation, balances)
                        break
                    was_open = True
            if executor.quantity and executor.phase != RoundPhase.EXITING:
                favorable = bar.close if intrabar_entry else (bar.high if executor.side == 1 else bar.low)
                self._ratchet(executor, favorable)
        yield ()

    def poll_events(self, up_to_ts: int) -> list[ExecutionEvent]:
        ready = [event for event in self._events if event.ts_event <= up_to_ts]
        self._events = [event for event in self._events if event.ts_event > up_to_ts]
        return ready


bar_sim_1m = SlotPlugin(Slot.FILL, (INTERFACE_VERSION,), BarSim1m, BarSimParams)
