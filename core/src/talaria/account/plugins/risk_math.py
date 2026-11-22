"""默认定仓和风控插件共用的无副作用计算。"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction

from talaria.account.contracts import AccountState, Direction, EntryCandidate, RoundExposure
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.margin import MarginPendingEntry, MarginPortfolio, MarginPosition, MarginValuation
from talaria.foundation.market import MarketPack
from talaria.foundation.types import decimal_from_finite_fraction

_ZERO = Decimal(0)
_COLLATERAL_ACCOUNTS = ("CASH_AVAILABLE", "CASH_RESERVED", "MARGIN_USED")


def sign(direction: Direction) -> int:
    return 1 if direction == "LONG" else -1


def worst_entry(candidate: EntryCandidate) -> Decimal:
    return candidate.entry_high if candidate.direction == "LONG" else candidate.entry_low


def valid_entry(candidate: EntryCandidate, stop: Decimal) -> bool:
    low, high = candidate.entry_low, candidate.entry_high
    if not 0 < low <= high or stop <= 0:
        return False
    return stop < low if candidate.direction == "LONG" else stop > high


def unit_risk(
    direction: Direction, entry: Decimal, stop: Decimal, config: AccountRiskConfig,
) -> Fraction:
    entry_value = Fraction(entry)
    return max(Fraction(0), sign(direction) * (entry_value - Fraction(stop))
               + entry_value * Fraction(config.friction_buffer_pct))


def exposure_risk(
    exposure: RoundExposure, config: AccountRiskConfig, pack: MarketPack,
) -> Fraction:
    position = exposure.position
    return (
        unit_risk(position.direction, position.entry_price, exposure.stop, config)
        * Fraction(position.quantity) * Fraction(pack.traits(position.instrument_id).margin.contract_multiplier)
    )


def candidate_exposure(candidate: EntryCandidate, quantity: Decimal, stop: Decimal) -> RoundExposure:
    return RoundExposure(
        candidate.round_id if candidate.round_id is not None else "",
        MarginPosition(candidate.instrument_id, candidate.direction, quantity, worst_entry(candidate)),
        stop, candidate.entry_high,
    )


def collateral_balance(balances: Mapping[tuple[str, str], Decimal], currency: str) -> Decimal:
    collateral = sum((
        Fraction(balances.get((currency, name), _ZERO)) for name in _COLLATERAL_ACCOUNTS
    ), Fraction(0))
    # 平仓毛利贷记 REALIZED_PNL，亏损借记，结转时不另改现金余额。
    collateral -= Fraction(balances.get((currency, "REALIZED_PNL"), _ZERO))
    return decimal_from_finite_fraction(collateral)


def portfolio(
    state: AccountState, config: AccountRiskConfig, exposures: Sequence[RoundExposure],
    pending_entries: Sequence[RoundExposure] = (),
) -> MarginPortfolio:
    pending = tuple(
        MarginPendingEntry(
            p.instrument_id, p.direction, p.quantity, p.entry_price, item.max_entry_price or p.entry_price,
        )
        for item in pending_entries for p in (item.position,)
    )
    return MarginPortfolio(
        collateral_balance(state.balances, config.currency), config.leverage,
        tuple(item.position for item in exposures), pending,
    )


def visible_marks(
    state: AccountState, config: AccountRiskConfig, positions: Sequence[MarginPosition], now: int,
    *, require_fresh: bool = True,
) -> dict[str, Decimal]:
    marks = {}
    for instrument in {position.instrument_id for position in positions}:
        point = state.marks.get(instrument)
        if (
            point is None or point.instrument_id != instrument or point.kind != DerivativeKind.MARK_PRICE
            or point.value <= 0
            or not point.visible_at(now)
            or (require_fresh and now - point.observation_time > config.mark_max_age_ms)
        ):
            raise ValueError(f"缺少有效标记价：{instrument}")
        marks[instrument] = point.value
    return marks


def equity(
    state: AccountState, config: AccountRiskConfig, valuation: MarginValuation | None, now: int,
    *, require_fresh_marks: bool = True,
) -> Decimal:
    holdings = portfolio(state, config, state.positions)
    if {item.round_id for item in state.positions} != state.rounds.keys():
        raise ValueError("缺少回合持仓成本")
    if not holdings.positions:
        return holdings.collateral
    if valuation is None:
        raise ValueError("缺少保证金估值")
    marks = visible_marks(state, config, holdings.positions, now, require_fresh=require_fresh_marks)
    return valuation.cross_margin(holdings, marks, as_of=now).equity


def isolated_liquidation(
    exposures: Sequence[RoundExposure], config: AccountRiskConfig, pack: MarketPack,
    valuation: MarginValuation, now: int,
) -> Decimal:
    position = exposures[0].position
    quantity = sum((item.position.quantity for item in exposures), _ZERO)
    notional = sum((item.position.entry_price * item.position.quantity for item in exposures), _ZERO)
    multiplier = pack.traits(position.instrument_id).margin.contract_multiplier
    return valuation.isolated_liquidation_price(
        position.instrument_id, position.direction, quantity, notional / quantity,
        notional * multiplier / config.leverage, as_of=now,
    )


def liquidation_buffer(instrument: str, stop: Decimal, config: AccountRiskConfig, pack: MarketPack) -> Decimal:
    return max(
        stop * config.liquidation_buffer_pct,
        pack.instrument(instrument).tick_size * config.liquidation_buffer_ticks,
    )


def isolated_stop_allows(
    exposures: Sequence[RoundExposure], config: AccountRiskConfig, pack: MarketPack,
    valuation: MarginValuation, now: int, mark: Decimal | None = None,
) -> bool:
    exposure = exposures[0]
    position = exposure.position
    side = sign(position.direction)
    stop = exposure.stop - side * liquidation_buffer(position.instrument_id, exposure.stop, config, pack)
    liquidation = isolated_liquidation(exposures, config, pack, valuation, now)
    return side * (stop - liquidation) > 0 and (mark is None or side * (mark - liquidation) > 0)


def margin_allows(
    state: AccountState, positions: tuple[RoundExposure, ...], pending: tuple[RoundExposure, ...],
    config: AccountRiskConfig, pack: MarketPack, valuation: MarginValuation, now: int,
    *, widening_round: str | None = None,
) -> bool:
    holdings = portfolio(state, config, positions, pending)
    marks = visible_marks(state, config, (*holdings.positions, *holdings.pending_entries), now)
    marked = valuation.cross_margin(holdings, marks, as_of=now)
    if marked.equity < marked.initial_margin or marked.equity <= marked.maintenance_margin:
        return False

    by_round: dict[str, list[RoundExposure]] = {}
    exposures = (*positions, *pending)
    for exposure in exposures:
        by_round.setdefault(exposure.round_id, []).append(exposure)
    stress = marks.copy()
    for same_round in by_round.values():
        exposure = same_round[0]
        position = exposure.position
        instrument = position.instrument_id
        side = sign(position.direction)
        mark = marks[instrument]
        # 保本后仍活跃的仓位可能已有止损触发，压力测试不可把当前价上调为锁定收益。
        boundary = exposure.stop - side * liquidation_buffer(instrument, exposure.stop, config, pack)
        if config.margin_mode == "ISOLATED":
            checked = same_round if exposure.round_id != widening_round else [
                item for item in positions if item.round_id == widening_round
            ]
            if not isolated_stop_allows(checked, config, pack, valuation, now, mark):
                return False
        else:
            if side == 1:
                floor = pack.instrument(instrument).tick_size
                stress[instrument] = min(stress[instrument], max(floor, boundary))
            else:
                stress[instrument] = max(stress[instrument], boundary)
    if config.margin_mode == "ISOLATED":
        allocated = sum((
            (item.max_entry_price or item.position.entry_price) * item.position.quantity
            * pack.traits(item.position.instrument_id).margin.contract_multiplier / config.leverage
            for item in exposures
        ), _ZERO)
        return allocated <= holdings.collateral
    stressed = valuation.cross_margin(holdings, stress, as_of=now)
    return stressed.equity > stressed.maintenance_margin
