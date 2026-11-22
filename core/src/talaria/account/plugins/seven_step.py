"""默认七步风控与逐子账户回撤熔断。"""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import count
from zoneinfo import ZoneInfo
from uuid import uuid4

from talaria.account.contracts import (
    AccountState, ApprovedBarrierAmendment, ApprovedExecutionPlan, BarrierChangeRequest,
    EntryCandidate, ExitBarrierSpec, RejectedPlanRecord, RoundExposure, SizingDecision,
    TradingState,
)
from talaria.account.plugins.risk_math import (
    candidate_exposure, equity, exposure_risk, isolated_stop_allows, margin_allows, sign, valid_entry,
    visible_marks, worst_entry,
)
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import INTERFACE_VERSION, PluginContext, PluginParams, Slot, SlotPlugin
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.margin import MarginValuation
from talaria.foundation.market import InstrumentSpec, MarketPack, SessionTrait, TraitBundle
from talaria.packs import DEFAULT_PACK, get_market_pack

_DEFAULT = AccountRiskConfig()
_HOUR_MS = 3_600_000
_DAY_MS = 24 * _HOUR_MS
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class _Drawdown:
    day: int
    opening: Decimal
    peak: Decimal


class SevenStepRisk:
    def __init__(
        self, pack: MarketPack, accounts: Mapping[str, AccountRiskConfig], *, paper: bool = False,
    ) -> None:
        self._pack = pack
        self._accounts = accounts
        self._ids = count(1)
        self._id_prefix = f"{uuid4().hex}-" if paper else ""
        self._drawdowns: dict[str, _Drawdown] = {}

    def evaluate_candidate(
        self, candidate: EntryCandidate, sizing: SizingDecision, account_state: AccountState,
        valuation: MarginValuation | None,
    ) -> ApprovedExecutionPlan | RejectedPlanRecord:
        config = self._accounts.get(account_state.sub_account_id, _DEFAULT)
        now = candidate.ts_decision
        net_equity = self._check_account(account_state, candidate.sub_account_id, now, config, valuation)
        if isinstance(net_equity, RejectedPlanRecord):
            return net_equity
        if candidate.valid_until is not None and now > candidate.valid_until:
            return RejectedPlanRecord(reason="PLAN_EXPIRED")
        instrument = self._pack.instrument(candidate.instrument_id)
        if instrument is None:
            return RejectedPlanRecord(reason="UNKNOWN_INSTRUMENT")

        existing = (*account_state.positions, *account_state.reservations)
        matching = [item for item in existing if item.position.instrument_id == candidate.instrument_id]
        if any(item.position.direction != candidate.direction for item in matching):
            return RejectedPlanRecord(reason="OPPOSITE_POSITION_OPEN")
        barriers = self._barriers(candidate, account_state, config, bool(matching))
        if isinstance(barriers, RejectedPlanRecord):
            return barriers
        side, entry = sign(candidate.direction), worst_entry(candidate)
        if not valid_entry(candidate, barriers.stop) or any(
            target <= 0 or side * (target - entry) <= 0 for target in barriers.take_profit
        ):
            return RejectedPlanRecord(reason="INVALID_STOP_GEOMETRY")

        requested = candidate_exposure(candidate, sizing.quantity, barriers.stop)
        projected = (*existing, requested)
        reason = self._risk_limit(requested, projected, config, net_equity)
        if reason is not None:
            return RejectedPlanRecord(reason=reason)

        traits = self._pack.traits(candidate.instrument_id)
        reason = self._order_checks(candidate, sizing, barriers, instrument, traits, config)
        if reason is not None:
            return RejectedPlanRecord(reason=reason)
        reason = self._margin_check(account_state, projected, config, valuation, now)
        if reason is not None:
            return RejectedPlanRecord(reason=reason)
        if not self._net_reward_allows(
            requested, barriers, traits, account_state, config, now, candidate.valid_until,
        ):
            return RejectedPlanRecord(reason="NET_REWARD_RISK_TOO_LOW")

        number = f"{self._id_prefix}{next(self._ids)}"
        return ApprovedExecutionPlan(
            plan_id=f"plan-{number}", round_id=candidate.round_id or f"round-{number}",
            candidate=candidate, sizing=sizing, barriers=barriers,
        )

    def approve_barrier_widening(
        self, request: BarrierChangeRequest, account_state: AccountState,
        valuation: MarginValuation | None,
    ) -> ApprovedBarrierAmendment | RejectedPlanRecord:
        config = self._accounts.get(account_state.sub_account_id, _DEFAULT)
        now = account_state.as_of
        checked = self._check_account(account_state, request.sub_account_id, now, config, valuation)
        if isinstance(checked, RejectedPlanRecord):
            return checked
        status = account_state.rounds.get(request.round_id)
        if status is None:
            return RejectedPlanRecord(reason="ROUND_NOT_OPEN")
        instrument = self._pack.instrument(status.instrument_id)
        if instrument is None:
            return RejectedPlanRecord(reason="UNKNOWN_INSTRUMENT")
        side = sign(status.direction)
        if request.stop <= 0 or side * (status.stop - request.stop) <= 0:
            return RejectedPlanRecord(reason="INVALID_STOP_GEOMETRY")
        if not _on_grid(request.stop, instrument.tick_size):
            return RejectedPlanRecord(reason="ORDER_CONSTRAINT")
        projected = tuple(
            replace(item, stop=request.stop) if item.round_id == request.round_id else item
            for item in (*account_state.positions, *account_state.reservations)
        )
        affected = next(item for item in projected if item.round_id == request.round_id)
        try:
            mark = visible_marks(account_state, config, (affected.position,), now)[status.instrument_id]
        except ValueError:
            return RejectedPlanRecord(reason="MARGIN_VALUATION_UNAVAILABLE")
        if side * (mark - request.stop) <= 0:
            return RejectedPlanRecord(reason="INVALID_STOP_GEOMETRY")
        reason = self._risk_limit(affected, projected, config, checked)
        if reason is None:
            reason = self._margin_check(account_state, projected, config, valuation, now, widening_round=request.round_id)
        if reason is not None:
            return RejectedPlanRecord(reason=reason)
        return ApprovedBarrierAmendment(amendment_id=f"amendment-{self._id_prefix}{next(self._ids)}", request=request)

    def drawdown_transition(
        self, account_state: AccountState, valuation: MarginValuation | None,
    ) -> tuple[TradingState, bool] | None:
        config = self._accounts.get(account_state.sub_account_id, _DEFAULT)
        try:
            net_equity = equity(account_state, config, valuation, account_state.as_of)
        except (KeyError, ValueError):
            return None
        bounds = self._drawdown_bounds(account_state.sub_account_id, account_state.as_of, net_equity, config)
        self._drawdowns[account_state.sub_account_id] = bounds
        state = _drawdown_state(net_equity, bounds, config)
        if state == TradingState.HALTED and account_state.trading_state != TradingState.HALTED:
            return state, config.liquidate_on_halt
        if state == TradingState.REDUCING and account_state.trading_state == TradingState.ACTIVE:
            return state, False
        return None

    def start_trading_day(self, account_state: AccountState, valuation: MarginValuation | None) -> None:
        account = account_state.sub_account_id
        config = self._accounts.get(account, _DEFAULT)
        try:
            opening = equity(account_state, config, valuation, account_state.as_of, require_fresh_marks=False)
        except (KeyError, ValueError):
            return
        previous = self._drawdowns.get(account)
        peak = config.initial_capital if previous is None else previous.peak
        self._drawdowns[account] = _Drawdown(account_state.as_of // _DAY_MS, opening, max(peak, opening))

    def entry_fill_allows(
        self, account: str, exposure: RoundExposure, valuation: MarginValuation | None, now: int,
    ) -> bool:
        config = self._accounts.get(account, _DEFAULT)
        if config.margin_mode != "ISOLATED":
            return True
        try:
            return valuation is not None and isolated_stop_allows((exposure,), config, self._pack, valuation, now)
        except (KeyError, ValueError):
            return False

    def _check_account(
        self, state: AccountState, account: str, now: int, config: AccountRiskConfig,
        valuation: MarginValuation | None,
    ) -> Decimal | RejectedPlanRecord:
        if state.sub_account_id != account:
            return RejectedPlanRecord(reason="SUB_ACCOUNT_MISMATCH")
        if now < state.as_of:
            return RejectedPlanRecord(reason="STALE_DECISION")
        if state.trading_state != TradingState.ACTIVE or config.trading_state != TradingState.ACTIVE:
            return RejectedPlanRecord(reason="TRADING_STATE_BLOCKED")
        try:
            net_equity = equity(state, config, valuation, now)
        except (KeyError, ValueError):
            return RejectedPlanRecord(reason="MARGIN_VALUATION_UNAVAILABLE")
        bounds = self._drawdown_bounds(account, now, net_equity, config)
        if _drawdown_state(net_equity, bounds, config) != TradingState.ACTIVE:
            return RejectedPlanRecord(reason="DRAWDOWN_LIMIT")
        return net_equity

    def _drawdown_bounds(
        self, account: str, now: int, net_equity: Decimal, config: AccountRiskConfig,
    ) -> _Drawdown:
        previous = self._drawdowns.get(account)
        day = now // _DAY_MS
        if previous is None:
            return _Drawdown(day, config.initial_capital, max(config.initial_capital, net_equity))
        opening = previous.opening if previous.day == day else net_equity
        return _Drawdown(day, opening, max(previous.peak, net_equity))

    @staticmethod
    def _barriers(
        candidate: EntryCandidate, state: AccountState, config: AccountRiskConfig, has_round: bool,
    ) -> ExitBarrierSpec | RejectedPlanRecord:
        if candidate.round_id is None:
            if has_round:
                return RejectedPlanRecord(reason="ADD_ON_ROUND_REQUIRED")
            return ExitBarrierSpec(
                stop=candidate.invalidation_stop, take_profit=candidate.take_profit,
                max_holding_ms=config.max_holding_ms, trailing_stop=config.trailing_stop,
            )
        round_status = state.rounds.get(candidate.round_id)
        previous = state.barriers.get(candidate.round_id)
        if round_status is None or previous is None or (
            round_status.instrument_id, round_status.direction
        ) != (candidate.instrument_id, candidate.direction):
            return RejectedPlanRecord(reason="ADD_ON_ROUND_MISMATCH")
        if candidate.invalidation_stop != round_status.stop:
            return RejectedPlanRecord(reason="ADD_ON_STOP_MISMATCH")
        return previous.model_copy(update={"stop": round_status.stop})

    def _risk_limit(
        self, requested: RoundExposure, exposures: tuple[RoundExposure, ...],
        config: AccountRiskConfig, net_equity: Decimal,
    ) -> str | None:
        direction = requested.position.direction
        clusters = config.correlation_clusters
        instrument = requested.position.instrument_id
        cluster = clusters.get(instrument, instrument)
        single_loss = cluster_loss = Fraction(0)
        for exposure in exposures:
            position = exposure.position
            loss = exposure_risk(exposure, config, self._pack)
            if exposure.round_id == requested.round_id:
                single_loss += loss
            if position.direction == direction and clusters.get(position.instrument_id, position.instrument_id) == cluster:
                cluster_loss += loss
        if single_loss > Fraction(net_equity) * Fraction(config.max_single_r_pct):
            return "MAX_SINGLE_R_EXCEEDED"
        if cluster_loss > Fraction(net_equity) * Fraction(config.r_unit_pct) * Fraction(config.cluster_max_r):
            return "CLUSTER_R_EXCEEDED"
        return None

    @staticmethod
    def _order_checks(
        candidate: EntryCandidate, sizing: SizingDecision, barriers: ExitBarrierSpec,
        instrument: InstrumentSpec, traits: TraitBundle, config: AccountRiskConfig,
    ) -> str | None:
        if not _session_open(traits.session, candidate.ts_decision):
            return "SESSION_CLOSED"
        rules = traits.order_constraint
        if (
            sizing.quantity < instrument.min_qty or not _on_grid(sizing.quantity, instrument.qty_step)
            or any(not _on_grid(price, instrument.tick_size) for price in (
                candidate.entry_low, candidate.entry_high, barriers.stop, *barriers.take_profit,
            ))
            or (candidate.order_type == "POST_ONLY" and not rules.post_only)
            or (candidate.order_type == "MARKET" and rules.max_market_qty is not None
                and sizing.quantity > rules.max_market_qty)
        ):
            return "ORDER_CONSTRAINT"
        if any(
            window.start <= candidate.ts_decision < window.end
            and window.instrument_id in (None, candidate.instrument_id)
            for window in config.quiet_windows
        ):
            return "EVENT_QUIET_WINDOW"
        return None

    def _margin_check(
        self, state: AccountState, projected: tuple[RoundExposure, ...], config: AccountRiskConfig,
        valuation: MarginValuation | None, now: int,
        *, widening_round: str | None = None,
    ) -> str | None:
        if valuation is None:
            return "MARGIN_VALUATION_UNAVAILABLE"
        try:
            for item in projected:
                trait = self._pack.traits(item.position.instrument_id).margin
                if trait.margin_asset != config.currency or config.leverage > trait.max_leverage:
                    return "MARGIN_OR_LIQUIDATION"
            filled_count = len(state.positions)
            if not margin_allows(
                state, projected[:filled_count], projected[filled_count:], config, self._pack, valuation, now,
                widening_round=widening_round,
            ):
                return "MARGIN_OR_LIQUIDATION"
        except (KeyError, ValueError):
            return "MARGIN_VALUATION_UNAVAILABLE"
        return None

    @staticmethod
    def _net_reward_allows(
        requested: RoundExposure, barriers: ExitBarrierSpec, traits: TraitBundle,
        state: AccountState, config: AccountRiskConfig, now: int, valid_until: int | None,
    ) -> bool:
        position = requested.position
        entry = position.entry_price
        side = sign(position.direction)
        target = min(barriers.take_profit, key=lambda price: side * price)
        carry = _ZERO
        interval_hours = traits.carry.funding_interval_hours
        if interval_hours:
            period = interval_hours * _HOUR_MS
            if valid_until is None:
                # 无到期限制时，成交可以落在结算周期的任意相位。
                settlements = (barriers.max_holding_ms + period - 1) // period
            else:
                settlements = (valid_until + barriers.max_holding_ms) // period - now // period
            point = state.funding_rates.get(position.instrument_id)
            funding_rate = config.funding_rate_buffer
            if (
                point is not None and point.instrument_id == position.instrument_id
                and point.kind == DerivativeKind.FUNDING_RATE and 0 <= point.ts_event <= point.ts_available <= now
            ):
                funding_rate = max(funding_rate, side * point.value)
            carry = Decimal(settlements) * funding_rate * max(entry, target, barriers.stop)
        friction = config.taker_fee_pct + config.slippage_pct
        net_reward = side * (target - entry) - (entry + target) * friction - carry
        net_risk = side * (entry - barriers.stop) + (entry + barriers.stop) * friction + carry
        return net_reward >= net_risk * config.min_net_rr


def _drawdown_state(net_equity: Decimal, bounds: _Drawdown, config: AccountRiskConfig) -> TradingState:
    if net_equity <= 0 or (
        net_equity <= bounds.opening * (1 - config.daily_hard_drawdown_pct)
        or net_equity <= bounds.peak * (1 - config.peak_hard_drawdown_pct)
    ):
        return TradingState.HALTED
    if (
        net_equity <= bounds.opening * (1 - config.daily_soft_drawdown_pct)
        or net_equity <= bounds.peak * (1 - config.peak_soft_drawdown_pct)
    ):
        return TradingState.REDUCING
    return TradingState.ACTIVE


def _on_grid(value: Decimal, step: Decimal) -> bool:
    return Fraction(value) % Fraction(step) == 0


def _session_open(session: SessionTrait, now: int) -> bool:
    if session.session_kind == "24x7":
        return True
    try:
        instant = _EPOCH + timedelta(milliseconds=now)
    except OverflowError:
        return False
    for window in session.sessions:
        local_time = instant.astimezone(ZoneInfo(window.timezone or session.timezone or "UTC")).time()
        start, end = time.fromisoformat(window.start), time.fromisoformat(window.end)
        in_session = start <= local_time < end if start < end else local_time >= start or local_time < end
        if in_session:
            return True
    return False


def _create(context: PluginContext, params: PluginParams) -> SevenStepRisk:
    return SevenStepRisk(get_market_pack(DEFAULT_PACK), context.account_risk, paper=context.mode == "paper")


seven_step = SlotPlugin(Slot.RISK, (INTERFACE_VERSION,), _create)
