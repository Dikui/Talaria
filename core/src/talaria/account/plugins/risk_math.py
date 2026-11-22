"""默认定仓和风控插件共用的无副作用计算。"""

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from talaria.account.contracts import AccountState, Direction, EntryCandidate, RoundExposure
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.margin import MarginPortfolio, MarginPosition, MarginValuation
from talaria.foundation.market import MarketPack

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
) -> Decimal:
    return max(_ZERO, sign(direction) * (entry - stop) + entry * config.friction_buffer_pct)


def exposure_risk(
    exposure: RoundExposure, config: AccountRiskConfig, pack: MarketPack,
) -> Decimal:
    position = exposure.position
    return (
        unit_risk(position.direction, position.entry_price, exposure.stop, config)
        * position.quantity * pack.traits(position.instrument_id).margin.contract_multiplier
    )


def candidate_exposure(candidate: EntryCandidate, quantity: Decimal, stop: Decimal) -> RoundExposure:
    return RoundExposure(
        candidate.round_id if candidate.round_id is not None else "",
        MarginPosition(candidate.instrument_id, candidate.direction, quantity, worst_entry(candidate)),
        stop,
    )


def portfolio(
    state: AccountState, config: AccountRiskConfig, exposures: Sequence[RoundExposure],
) -> MarginPortfolio:
    collateral = sum((
        state.balances.get((config.currency, name), _ZERO) for name in _COLLATERAL_ACCOUNTS
    ), _ZERO)
    return MarginPortfolio(collateral, config.leverage, tuple(item.position for item in exposures))


def visible_marks(
    state: AccountState, config: AccountRiskConfig, positions: Sequence[MarginPosition], now: int,
) -> dict[str, Decimal]:
    marks = {}
    for instrument in {position.instrument_id for position in positions}:
        point = state.marks.get(instrument)
        if (
            point is None or point.instrument_id != instrument or point.kind != DerivativeKind.MARK_PRICE
            or point.value <= 0
            or not 0 <= point.ts_event <= point.ts_available <= now
            or now - point.ts_event > config.mark_max_age_ms
        ):
            raise ValueError(f"缺少有效标记价：{instrument}")
        marks[instrument] = point.value
    return marks


def equity(
    state: AccountState, config: AccountRiskConfig, valuation: MarginValuation | None, now: int,
) -> Decimal:
    holdings = portfolio(state, config, state.positions)
    if {item.round_id for item in state.positions} != state.rounds.keys():
        raise ValueError("缺少回合持仓成本")
    if not holdings.positions:
        return holdings.collateral
    if valuation is None:
        raise ValueError("缺少保证金估值")
    marks = visible_marks(state, config, holdings.positions, now)
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


def margin_allows(
    state: AccountState, exposures: tuple[RoundExposure, ...], config: AccountRiskConfig,
    pack: MarketPack, valuation: MarginValuation, now: int,
) -> bool:
    holdings = portfolio(state, config, exposures)
    marks = visible_marks(state, config, holdings.positions, now)
    # 保证金计入未成交数量，未成交订单的浮盈不能充当抵押品。
    filled_count = len(state.positions)
    pending = []
    for position in holdings.positions[filled_count:]:
        mark = marks[position.instrument_id]
        if sign(position.direction) * (mark - position.entry_price) > 0:
            position = replace(position, entry_price=mark)
        pending.append(position)
    marked = valuation.cross_margin(
        replace(holdings, positions=(*holdings.positions[:filled_count], *pending)), marks, as_of=now,
    )
    if marked.equity < marked.initial_margin or marked.equity <= marked.maintenance_margin:
        return False

    by_round: dict[str, list[RoundExposure]] = {}
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
            liquidation = isolated_liquidation(same_round, config, pack, valuation, now)
            if side * (mark - liquidation) <= 0 or side * (boundary - liquidation) <= 0:
                return False
        else:
            if side == 1:
                floor = pack.instrument(instrument).tick_size
                stress[instrument] = min(stress[instrument], max(floor, boundary))
            else:
                stress[instrument] = max(stress[instrument], boundary)
    if config.margin_mode == "ISOLATED":
        allocated = sum((
            item.position.entry_price * item.position.quantity
            * pack.traits(item.position.instrument_id).margin.contract_multiplier / config.leverage
            for item in exposures
        ), _ZERO)
        return allocated <= holdings.collateral
    stressed = valuation.cross_margin(holdings, stress, as_of=now)
    return stressed.equity > stressed.maintenance_margin
