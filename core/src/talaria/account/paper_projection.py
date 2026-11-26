"""模拟盘账户只读投影（ADR-0025）：从权威账本与执行门推导 `accounts/<sub_account_id>/view.json`。

写入约定沿用 ADR-0024：
- 仅由持有 `owner.lock` 的模拟盘账户进程单写，按 `.tmp -> fsync -> os.replace` 与目录 `fsync` 原子落盘；
- 相同账本与执行门状态下重复推导，`heartbeat_ts` 除外逐字节一致；
- 最近成交列表保留最新 `MAX_PROJECTION_FILLS`（200）笔，既覆盖面板与图表标记需求，又保持投影体积有界，完整历史留在权威账本 `ledger.sqlite`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import os
from pathlib import Path
import re

from talaria.account.contracts import (
    AccountState,
    ApprovedExecutionPlan,
    ExitBarrierSpec,
    MarginValuation,
    RoundPhase,
    RoundStatus,
)
from talaria.account.ledger import RunJournal
from talaria.account.plugins.risk_math import collateral_balance, equity as compute_equity, exposure_risk
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.market import MarketPack
from talaria.foundation.paper import (
    ACCOUNT_ID_PATTERN,
    MarginMode,
    PaperAccountState,
    PaperAccountView,
    PaperFeedStatus,
    PaperFill,
    PaperFillKind,
    PaperProcessInfo,
    PaperRound,
    TradingState,
)
from talaria.foundation.types import decimal_from_finite_fraction
from talaria.packs import DEFAULT_PACK, get_market_pack

#: 单份只读投影最多保留的最近成交条数（最新在前），避免长周期模拟盘导致 view.json 无限膨胀。
MAX_PROJECTION_FILLS = 200

_ZERO = Decimal(0)
_TERMINAL_PHASES = frozenset({RoundPhase.CLOSED, RoundPhase.CANCELLED, RoundPhase.LIQUIDATED})
_TERMINAL_EVENT_KINDS = frozenset({"EXIT_FILLED", "ROUND_CLOSED", "ROUND_CANCELLED", "ROUND_LIQUIDATED"})


def _normalize_decimal(value: Decimal) -> Decimal:
    if value == 0:
        return _ZERO
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return Decimal(text) if text and text != "-0" else _ZERO


def _clean_decimal(value: Fraction, *, max_places: int = 8) -> Decimal:
    if value == 0:
        return _ZERO
    denom = value.denominator
    twos = 0
    while denom % 2 == 0:
        denom //= 2
        twos += 1
    fives = 0
    while denom % 5 == 0:
        denom //= 5
        fives += 1
    if denom == 1 and max(twos, fives) <= max_places:
        return _normalize_decimal(decimal_from_finite_fraction(value))
    quant = Decimal(10) ** -max_places
    prec = max(28, len(str(abs(value.numerator))) + max_places + 6)
    with localcontext(prec=prec, rounding=ROUND_HALF_EVEN):
        quotient = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(quant)
    return _normalize_decimal(quotient)


def projection_path(data_dir: Path, sub_account_id: str) -> Path:
    """返回子账户只读投影路径 `<data_dir>/accounts/<sub_account_id>/view.json`。"""
    if not re.fullmatch(ACCOUNT_ID_PATTERN, sub_account_id):
        raise ValueError(f"非法的子账户标识：{sub_account_id}")
    return data_dir / "accounts" / sub_account_id / "view.json"


def serialize_projection(view: PaperAccountView) -> bytes:
    """将 `PaperAccountView` 规范化序列化为确定性 UTF-8 JSON 字节。"""
    return (view.model_dump_json() + "\n").encode("utf-8")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_projection(path: Path, view: PaperAccountView) -> None:
    """按 ADR-0024 / ADR-0025 原子写入 `.tmp -> fsync -> os.replace` 与目录 `fsync`。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = serialize_projection(view)
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


@dataclass
class _RoundAcc:
    position: RoundStatus
    take_profit: tuple[Decimal, ...] = ()
    barriers: ExitBarrierSpec | None = None
    opened_ts: int | None = None
    closed_ts: int | None = None
    entry_qty_sum: Fraction = Fraction(0)
    entry_notional_sum: Fraction = Fraction(0)


def _multiplier_for(pack: MarketPack, instrument_id: str) -> Fraction:
    try:
        return Fraction(pack.traits(instrument_id).margin.contract_multiplier)
    except KeyError:
        return Fraction(1)


def build_paper_account_view(
    sub_account_id: str,
    journal: RunJournal,
    account_state: AccountState,
    risk_config: AccountRiskConfig,
    *,
    valuation: MarginValuation | None = None,
    pack: MarketPack | None = None,
    pending_plans: Sequence[ApprovedExecutionPlan] = (),
    trading_state_reason: str | None = None,
    drawdown_bounds: tuple[Decimal, Decimal] | None = None,
    feed_status: PaperFeedStatus | None = None,
    process: PaperProcessInfo | None = None,
    max_fills: int = MAX_PROJECTION_FILLS,
) -> PaperAccountView:
    """由权威账本日志与执行门状态纯函数推导 `PaperAccountView`。"""
    resolved_pack = pack if pack is not None else get_market_pack(DEFAULT_PACK)
    postings_by_event = {entry.event_id: entry.postings for entry in journal.entries}

    rounds_acc: dict[str, _RoundAcc] = {}
    fills_raw: list[PaperFill] = []

    for event in journal.events:
        if event.sub_account_id != sub_account_id:
            continue
        position = event.position
        if position is None:
            continue

        prev = rounds_acc.get(position.round_id)
        targets = (
            event.take_profit
            or (event.barriers.take_profit if event.barriers is not None else ())
            or (prev.take_profit if prev is not None else ())
        )
        barriers = event.barriers or (prev.barriers if prev is not None else None)
        if barriers is not None and barriers.stop != position.stop:
            barriers = barriers.model_copy(update={"stop": position.stop})

        acc = prev
        if acc is None:
            acc = _RoundAcc(position=position, take_profit=targets, barriers=barriers)
            rounds_acc[position.round_id] = acc
        else:
            acc.position = position
            acc.take_profit = targets
            acc.barriers = barriers

        if event.kind == "ENTRY_FILLED" and event.price is not None and event.quantity is not None:
            if acc.opened_ts is None:
                acc.opened_ts = event.ts_event
            acc.closed_ts = None
            acc.entry_qty_sum += Fraction(event.quantity)
            acc.entry_notional_sum += Fraction(event.quantity) * Fraction(event.price)

        if position.quantity == 0 and (
            event.kind in _TERMINAL_EVENT_KINDS or position.phase in _TERMINAL_PHASES
        ):
            acc.closed_ts = event.ts_event

        if event.kind in ("ENTRY_FILLED", "EXIT_FILLED") and event.price is not None and event.quantity is not None:
            postings = postings_by_event.get(event.event_id or "", event.postings)
            fee_sum = Fraction(0)
            has_fee = False
            pnl_sum = Fraction(0)
            has_pnl = False
            for posting in postings:
                if posting.account in ("EXPENSE_EXECUTION_FEE", "EXPENSE_LIQUIDATION_FEE"):
                    fee_sum += Fraction(posting.amount)
                    has_fee = True
                elif posting.account == "REALIZED_PNL":
                    # 复式分录中盈利贷记（负），亏损借记（正），对交易员取反。
                    pnl_sum -= Fraction(posting.amount)
                    has_pnl = True
            fills_raw.append(
                PaperFill(
                    event_id=event.event_id or f"fill-{len(fills_raw) + 1}",
                    round_id=position.round_id,
                    instrument_id=position.instrument_id,
                    kind=PaperFillKind(event.kind),
                    ts_event=event.ts_event,
                    quantity=_normalize_decimal(event.quantity),
                    price=_normalize_decimal(event.price),
                    fee=_clean_decimal(fee_sum) if has_fee else None,
                    realized_pnl=_clean_decimal(pnl_sum) if has_pnl else None,
                    reason=event.reason,
                )
            )

    plans_by_round: dict[str, list[ApprovedExecutionPlan]] = {}
    for plan in pending_plans:
        if plan.candidate.sub_account_id != sub_account_id:
            continue
        plans_by_round.setdefault(plan.round_id, []).append(plan)
        if plan.round_id not in rounds_acc:
            candidate = plan.candidate
            rounds_acc[plan.round_id] = _RoundAcc(
                position=RoundStatus(
                    round_id=plan.round_id,
                    sub_account_id=sub_account_id,
                    instrument_id=candidate.instrument_id,
                    direction=candidate.direction,
                    quantity=_ZERO,
                    stop=plan.barriers.stop if plan.barriers is not None else candidate.invalidation_stop,
                    phase=RoundPhase.PENDING_ENTRY,
                ),
                take_profit=plan.barriers.take_profit if plan.barriers is not None else candidate.take_profit,
                barriers=plan.barriers,
            )

    exposures_by_round = {exp.round_id: exp for exp in account_state.positions}

    rounds: list[PaperRound] = []
    for round_id in reversed(list(rounds_acc.keys())):
        acc = rounds_acc[round_id]
        pos = acc.position
        is_open = pos.quantity > 0

        entry_price: Decimal | None = None
        if is_open and round_id in exposures_by_round:
            entry_price = _clean_decimal(Fraction(exposures_by_round[round_id].position.entry_price))
        elif acc.entry_qty_sum > 0:
            entry_price = _clean_decimal(acc.entry_notional_sum / acc.entry_qty_sum)

        mark_price: Decimal | None = None
        unrealized_pnl: Decimal | None = None
        if is_open:
            mark_point = account_state.marks.get(pos.instrument_id)
            if mark_point is not None and mark_point.value > 0:
                mark_price = _normalize_decimal(mark_point.value)
                if entry_price is not None:
                    side = 1 if pos.direction == "LONG" else -1
                    mult = _multiplier_for(resolved_pack, pos.instrument_id)
                    pnl_frac = (
                        Fraction(side)
                        * (Fraction(mark_price) - Fraction(entry_price))
                        * Fraction(pos.quantity)
                        * mult
                    )
                    unrealized_pnl = _clean_decimal(pnl_frac)

        round_plans = plans_by_round.get(round_id)
        entry_low: Decimal | None = None
        entry_high: Decimal | None = None
        if round_plans:
            entry_low = _normalize_decimal(min(p.candidate.entry_low for p in round_plans))
            entry_high = _normalize_decimal(max(p.candidate.entry_high for p in round_plans))

        phase_value = pos.phase.value if isinstance(pos.phase, RoundPhase) else pos.phase
        rounds.append(
            PaperRound(
                round_id=pos.round_id,
                sub_account_id=pos.sub_account_id,
                instrument_id=pos.instrument_id,
                direction=pos.direction,
                quantity=_normalize_decimal(pos.quantity),
                stop=_normalize_decimal(pos.stop),
                take_profit=tuple(_normalize_decimal(tp) for tp in acc.take_profit),
                phase=phase_value,
                barriers=acc.barriers,
                entry_low=entry_low,
                entry_high=entry_high,
                entry_price=entry_price,
                mark_price=mark_price,
                unrealized_pnl=unrealized_pnl,
                opened_ts=acc.opened_ts,
                closed_ts=acc.closed_ts,
            )
        )

    positions = [row for row in rounds if row.quantity > 0]
    fills = list(reversed(fills_raw))[: max(0, max_fills)]

    currency = risk_config.currency
    collateral = collateral_balance(account_state.balances, currency)
    margin_used = _normalize_decimal(account_state.balances.get((currency, "MARGIN_USED"), _ZERO))
    cash_reserved = account_state.balances.get((currency, "CASH_RESERVED"), _ZERO)
    cash_available = _clean_decimal(Fraction(collateral) - Fraction(margin_used) - Fraction(cash_reserved))

    try:
        net_equity = _normalize_decimal(
            compute_equity(
                account_state,
                risk_config,
                valuation,
                account_state.as_of,
                require_fresh_marks=False,
            )
        )
    except (KeyError, ValueError):
        unrealized_sum = sum(
            (Fraction(row.unrealized_pnl) for row in positions if row.unrealized_pnl is not None),
            Fraction(0),
        )
        net_equity = _clean_decimal(Fraction(collateral) + unrealized_sum)

    risk_reserved_frac = Fraction(0)
    for exp in (*account_state.positions, *account_state.reservations):
        try:
            risk_reserved_frac += exposure_risk(exp, risk_config, resolved_pack)
        except KeyError:
            continue
    risk_reserved = _clean_decimal(risk_reserved_frac)
    r_denom = Fraction(net_equity) * Fraction(risk_config.r_unit_pct)
    open_r = _clean_decimal(risk_reserved_frac / r_denom, max_places=4) if r_denom > 0 else _ZERO

    if drawdown_bounds is not None:
        opening, peak = drawdown_bounds
    else:
        opening = risk_config.initial_capital
        peak = max(risk_config.initial_capital, net_equity)

    intraday_dd = (
        max(Fraction(0), (Fraction(opening) - Fraction(net_equity)) / Fraction(opening))
        if opening > 0
        else Fraction(0)
    )
    peak_dd = (
        max(Fraction(0), (Fraction(peak) - Fraction(net_equity)) / Fraction(peak))
        if peak > 0
        else Fraction(0)
    )

    state_snapshot = PaperAccountState(
        currency=currency,
        equity=net_equity,
        cash_available=cash_available,
        margin_used=margin_used,
        trading_state=TradingState(account_state.trading_state.value),
        trading_state_reason=trading_state_reason,
        intraday_drawdown_pct=_clean_decimal(intraday_dd, max_places=6),
        peak_drawdown_pct=_clean_decimal(peak_dd, max_places=6),
        open_r=open_r,
        risk_reserved=risk_reserved,
        margin_mode=MarginMode(risk_config.margin_mode),
        leverage=_normalize_decimal(risk_config.leverage),
        r_unit_pct=_normalize_decimal(risk_config.r_unit_pct),
        max_single_r_pct=_normalize_decimal(risk_config.max_single_r_pct),
        friction_buffer_pct=_normalize_decimal(risk_config.friction_buffer_pct),
        liquidation_buffer_pct=_normalize_decimal(risk_config.liquidation_buffer_pct),
        liquidation_buffer_ticks=risk_config.liquidation_buffer_ticks,
    )

    return PaperAccountView(
        sub_account_id=sub_account_id,
        positions=positions,
        rounds=rounds,
        fills=fills,
        ledger_seq=len(journal.events),
        account_state=state_snapshot,
        feed_status=feed_status,
        process=process,
    )
