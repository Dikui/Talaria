"""账户进程回放/回测导出：从权威账本生成契约化 Parquet 与原子就绪标记 summary.json。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow as pa
import pyarrow.parquet as pq

from talaria.account.contracts import ExecutionEvent
from talaria.account.ledger import RunJournal, read_journal
from talaria.account.scheme import Slot
from talaria.foundation.errors import TalariaError
from talaria.foundation.runs import (
    RunAccountCurrencySummary,
    RunBarrierChange,
    RunBarrierChangeKind,
    RunEquityPoint,
    RunExportArtifacts,
    RunExportSummary,
    RunFillKind,
    RunFillReason,
    RunRoundFill,
    RunRoundPhase,
    RunRoundRecord,
    RunStatus,
)
from talaria.foundation.timeframes import timeframe_ms
from talaria.foundation.types import decimal_from_finite_fraction as decimal_exact
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader

if TYPE_CHECKING:
    from talaria.account.host import RunCompletion, RunConfig

ROUNDS_SCHEMA = pa.schema(
    [
        pa.field("round_id", pa.string(), nullable=False),
        pa.field("sub_account_id", pa.string(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("direction", pa.string(), nullable=False),
        pa.field("phase", pa.string(), nullable=False),
        pa.field("opened_ts", pa.int64(), nullable=True),
        pa.field("closed_ts", pa.int64(), nullable=True),
        pa.field("entry_price", pa.string(), nullable=True),
        pa.field("exit_price", pa.string(), nullable=True),
        pa.field("max_quantity", pa.string(), nullable=False),
        pa.field("remaining_quantity", pa.string(), nullable=False),
        pa.field("stop", pa.string(), nullable=False),
        pa.field("take_profit_json", pa.string(), nullable=False),
        pa.field("exit_reason", pa.string(), nullable=True),
        pa.field("raw_exit_reason", pa.string(), nullable=True),
        pa.field("net_pnl", pa.string(), nullable=False),
        pa.field("fees", pa.string(), nullable=False),
        pa.field("funding", pa.string(), nullable=False),
        pa.field("liquidation_fees", pa.string(), nullable=False),
        pa.field("mae", pa.string(), nullable=True),
        pa.field("mfe", pa.string(), nullable=True),
        pa.field("fills_json", pa.string(), nullable=False),
        pa.field("barrier_changes_json", pa.string(), nullable=False),
    ]
)

EQUITY_SCHEMA = pa.schema(
    [
        pa.field("sub_account_id", pa.string(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("event_kind", pa.string(), nullable=False),
        pa.field("round_id", pa.string(), nullable=True),
        pa.field("pnl_basis", pa.string(), nullable=False),
        pa.field("realized_pnl_delta", pa.string(), nullable=False),
        pa.field("fee_delta", pa.string(), nullable=False),
        pa.field("funding_delta", pa.string(), nullable=False),
        pa.field("liquidation_fee_delta", pa.string(), nullable=False),
        pa.field("net_pnl", pa.string(), nullable=False),
        pa.field("equity", pa.string(), nullable=False),
        pa.field("peak_pnl", pa.string(), nullable=False),
        pa.field("drawdown", pa.string(), nullable=False),
        pa.field("max_drawdown", pa.string(), nullable=False),
    ]
)

_EXIT_REASON_MAP: dict[str, RunFillReason] = {
    "STOP_LOSS": RunFillReason.SL,
    "TRAILING_STOP": RunFillReason.SL,
    "TAKE_PROFIT": RunFillReason.TP,
    "MAX_HOLDING_TIME": RunFillReason.TIME,
    "MARGIN_LIQUIDATION": RunFillReason.LIQUIDATION,
}


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _atomic_write_parquet(path: Path, table: pa.Table) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "wb") as handle:
        pq.write_table(table, handle, compression="zstd")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _normalize_fill_reason(kind: str, raw_reason: str | None) -> RunFillReason:
    if kind == "ENTRY_FILLED":
        return RunFillReason.ENTRY
    if raw_reason is None:
        return RunFillReason.OTHER
    return _EXIT_REASON_MAP.get(raw_reason, RunFillReason.OTHER)


def _fill_side(direction: str, kind: str) -> str:
    if direction == "LONG":
        return "buy" if kind == "ENTRY_FILLED" else "sell"
    return "sell" if kind == "ENTRY_FILLED" else "buy"


def _event_postings_map(
    event: ExecutionEvent,
    entries_by_event_id: dict[str, tuple] | None = None,
) -> dict[str, dict[str, Fraction]]:
    by_currency: dict[str, dict[str, Fraction]] = defaultdict(lambda: defaultdict(Fraction))
    postings = (
        entries_by_event_id.get(event.event_id, event.postings)
        if entries_by_event_id is not None and event.event_id is not None
        else event.postings
    )
    for posting in postings:
        by_currency[posting.currency][posting.account] += Fraction(posting.amount)
    return by_currency


@dataclass
class _BookedAccountState:
    initial_capital: Fraction | None = None
    net_pnl: Fraction = Fraction(0)
    peak_pnl: Fraction = Fraction(0)
    max_drawdown: Fraction = Fraction(0)
    fees: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)
    liquidation_fees: Fraction = Fraction(0)
    seq: int = 0
    trade_count: int = 0

    def book(
        self,
        *,
        pnl: Fraction,
        fees: Fraction,
        funding: Fraction,
        liquidation_fees: Fraction,
    ) -> tuple[Fraction, Fraction]:
        self.fees += fees
        self.funding += funding
        self.liquidation_fees += liquidation_fees
        self.net_pnl += pnl - fees + funding - liquidation_fees
        self.peak_pnl = max(self.peak_pnl, self.net_pnl)
        drawdown = self.peak_pnl - self.net_pnl
        self.max_drawdown = max(self.max_drawdown, drawdown)
        self.seq += 1
        return drawdown, self.max_drawdown


@dataclass
class _RoundAccumulator:
    round_id: str
    sub_account_id: str
    currency: str
    instrument_id: str
    direction: str
    phase: str
    stop: Decimal
    take_profit: list[Decimal] = field(default_factory=list)
    opened_ts: int | None = None
    closed_ts: int | None = None
    max_quantity: Fraction = Fraction(0)
    remaining_quantity: Fraction = Fraction(0)
    entry_qty_sum: Fraction = Fraction(0)
    entry_notional_sum: Fraction = Fraction(0)
    exit_qty_sum: Fraction = Fraction(0)
    exit_notional_sum: Fraction = Fraction(0)
    exit_reason: RunFillReason | None = None
    raw_exit_reason: str | None = None
    net_pnl: Fraction = Fraction(0)
    fees: Fraction = Fraction(0)
    funding: Fraction = Fraction(0)
    liquidation_fees: Fraction = Fraction(0)
    multiplier: Fraction | None = None
    fills: list[RunRoundFill] = field(default_factory=list)
    barrier_changes: list[RunBarrierChange] = field(default_factory=list)


def _compute_round_excursions(
    acc: _RoundAccumulator,
    *,
    bars: list[tuple[int, int, Decimal, Decimal]] | None,
) -> tuple[Decimal | None, Decimal | None]:
    """根据开仓期间成交价与区间柱高低点计算 MAE / MFE（绝对计价金额，非负）。"""
    if acc.opened_ts is None or not acc.fills or not bars:
        return None, None
    multiplier = acc.multiplier or Fraction(1)
    side = 1 if acc.direction == "LONG" else -1
    max_adverse = Fraction(0)
    max_favorable = Fraction(0)

    def observe_price(price: Decimal, qty: Fraction, avg_entry: Fraction) -> None:
        nonlocal max_adverse, max_favorable
        if qty <= 0:
            return
        delta = (Fraction(price) - avg_entry) * qty * multiplier * side
        if delta < 0:
            max_adverse = max(max_adverse, -delta)
        elif delta > 0:
            max_favorable = max(max_favorable, delta)

    # 按时间顺序推进持仓均价，并检查严格落在持有期内的每根 K 线高低点。
    fill_idx = 0
    held = Fraction(0)
    total_cost = Fraction(0)
    fills = acc.fills
    end_bound = acc.closed_ts if acc.closed_ts is not None else bars[-1][1]

    def apply_fill(fill: RunRoundFill) -> None:
        nonlocal held, total_cost
        qty = Fraction(fill.quantity)
        px = Fraction(fill.price)
        if held > 0:
            observe_price(fill.price, held, total_cost / held)
        if fill.kind == RunFillKind.ENTRY_FILLED:
            held += qty
            total_cost += qty * px
        else:
            if held > 0:
                reduced = min(qty, held)
                total_cost -= total_cost * (reduced / held)
                held -= reduced

    for bar_start, bar_end, bar_low, bar_high in bars:
        if bar_end <= acc.opened_ts or bar_start > end_bound:
            continue
        while fill_idx < len(fills) and fills[fill_idx].ts_event <= bar_start:
            apply_fill(fills[fill_idx])
            fill_idx += 1
        # 只统计完全处于持有窗口内的柱，避免把入场前或出场后同柱影线计入偏移。
        if held > 0 and bar_start >= acc.opened_ts and bar_end <= end_bound:
            avg_entry = total_cost / held
            observe_price(bar_low, held, avg_entry)
            observe_price(bar_high, held, avg_entry)

    while fill_idx < len(fills):
        apply_fill(fills[fill_idx])
        fill_idx += 1

    return decimal_exact(max_adverse), decimal_exact(max_favorable)


def _load_round_bars(
    config: RunConfig | None,
    *,
    data_dir: Path | None,
    instrument_id: str,
    timeframe: str | None,
    start: int,
    end: int,
) -> list[tuple[int, int, Decimal, Decimal]] | None:
    resolved_data_dir = data_dir or (config.data_dir if config is not None else None)
    if resolved_data_dir is None:
        return None
    snapshot = config.cold_snapshot if config is not None else None
    spec = get_market_pack(DEFAULT_PACK).instrument(instrument_id)
    if spec is None:
        return None
    tf = "1m" if snapshot is not None else (timeframe or "1m")
    try:
        reader = ColdReader(resolved_data_dir, snapshot=snapshot)
        bars = reader.read_bars(spec, tf, start_ts=start, end_ts=end, as_of=end)
        if not bars and tf != (timeframe or "1m") and timeframe is not None:
            tf = timeframe
            bars = reader.read_bars(spec, tf, start_ts=start, end_ts=end, as_of=end)
    except (OSError, TalariaError, ValueError):
        return None
    if not bars:
        return None
    tf_ms = timeframe_ms(tf)
    return [(bar.ts_event, bar.ts_event + tf_ms, bar.low, bar.high) for bar in bars]


def _rounds_to_table(rounds: list[RunRoundRecord]) -> pa.Table:
    rows = {name: [] for name in ROUNDS_SCHEMA.names}
    for item in rounds:
        rows["round_id"].append(item.round_id)
        rows["sub_account_id"].append(item.sub_account_id)
        rows["currency"].append(item.currency)
        rows["instrument_id"].append(item.instrument_id)
        rows["direction"].append(item.direction)
        rows["phase"].append(item.phase.value)
        rows["opened_ts"].append(item.opened_ts)
        rows["closed_ts"].append(item.closed_ts)
        rows["entry_price"].append(str(item.entry_price) if item.entry_price is not None else None)
        rows["exit_price"].append(str(item.exit_price) if item.exit_price is not None else None)
        rows["max_quantity"].append(str(item.max_quantity))
        rows["remaining_quantity"].append(str(item.remaining_quantity))
        rows["stop"].append(str(item.stop))
        rows["take_profit_json"].append(
            json.dumps([str(tp) for tp in item.take_profit], separators=(",", ":"))
        )
        rows["exit_reason"].append(item.exit_reason.value if item.exit_reason is not None else None)
        rows["raw_exit_reason"].append(item.raw_exit_reason)
        rows["net_pnl"].append(str(item.net_pnl))
        rows["fees"].append(str(item.fees))
        rows["funding"].append(str(item.funding))
        rows["liquidation_fees"].append(str(item.liquidation_fees))
        rows["mae"].append(str(item.mae) if item.mae is not None else None)
        rows["mfe"].append(str(item.mfe) if item.mfe is not None else None)
        rows["fills_json"].append(
            json.dumps([fill.model_dump(mode="json") for fill in item.fills], separators=(",", ":"), sort_keys=True)
        )
        rows["barrier_changes_json"].append(
            json.dumps(
                [change.model_dump(mode="json") for change in item.barrier_changes],
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    return pa.table(rows, schema=ROUNDS_SCHEMA)


def _equity_to_table(points: list[RunEquityPoint]) -> pa.Table:
    rows = {name: [] for name in EQUITY_SCHEMA.names}
    for point in points:
        rows["sub_account_id"].append(point.sub_account_id)
        rows["currency"].append(point.currency)
        rows["seq"].append(point.seq)
        rows["ts_event"].append(point.ts_event)
        rows["event_id"].append(point.event_id)
        rows["event_kind"].append(point.event_kind)
        rows["round_id"].append(point.round_id)
        rows["pnl_basis"].append(point.pnl_basis)
        rows["realized_pnl_delta"].append(str(point.realized_pnl_delta))
        rows["fee_delta"].append(str(point.fee_delta))
        rows["funding_delta"].append(str(point.funding_delta))
        rows["liquidation_fee_delta"].append(str(point.liquidation_fee_delta))
        rows["net_pnl"].append(str(point.net_pnl))
        rows["equity"].append(str(point.equity))
        rows["peak_pnl"].append(str(point.peak_pnl))
        rows["drawdown"].append(str(point.drawdown))
        rows["max_drawdown"].append(str(point.max_drawdown))
    return pa.table(rows, schema=EQUITY_SCHEMA)


def export_run(
    run_dir: Path,
    *,
    config: RunConfig | None = None,
    completion: RunCompletion | None = None,
    data_dir: Path | None = None,
) -> RunExportSummary:
    """从运行目录中的配置与账本原子导出 `rounds.parquet`、`equity.parquet` 和 `summary.json`。

    写入顺序固定为：`.tmp -> fsync -> os.replace` 先落盘 `rounds.parquet` 与 `equity.parquet`，
    最后落盘 `summary.json` 作为就绪标记。相同输入重复导出结果逐字节一致。
    """
    from talaria.account.host import RunCompletion as HostRunCompletion, RunConfig as HostRunConfig

    run_dir = run_dir.expanduser().resolve()
    config_path = run_dir / "run-config.json"
    if config is None and config_path.is_file():
        config = HostRunConfig.model_validate_json(config_path.read_text(encoding="utf-8"))

    status_path = run_dir / "run-status.json"
    if completion is None:
        if not status_path.is_file():
            raise FileNotFoundError(f"运行目录缺少完成状态文件：{status_path}")
        completion = HostRunCompletion.model_validate_json(status_path.read_text(encoding="utf-8"))

    ledger_path = run_dir / "ledger.sqlite"
    journal = read_journal(ledger_path) if ledger_path.is_file() else RunJournal((), ())

    # 解析方案与区间元数据。
    scheme_name = config.scheme.scheme if config is not None else run_dir.name
    fill_plugin = config.scheme.slots.selection(Slot.FILL).reference if config is not None else "unknown"
    replay_instrument: str | None = None
    replay_timeframe: str | None = None
    replay_start: int | None = None
    replay_end: int | None = None
    if config is not None:
        data_selection = config.scheme.slots.selection(Slot.DATA)
        if data_selection.reference == "parquet_replay":
            params = data_selection.params
            raw_inst = params.get("instrument")
            raw_tf = params.get("timeframe")
            raw_start = params.get("start")
            raw_end = params.get("end")
            if isinstance(raw_inst, str):
                replay_instrument = raw_inst
            if isinstance(raw_tf, str):
                replay_timeframe = raw_tf
            if isinstance(raw_start, int):
                replay_start = raw_start
            if isinstance(raw_end, int):
                replay_end = raw_end

    # 按 (sub_account_id, currency) 收集初始本金。
    account_states: dict[tuple[str, str], _BookedAccountState] = {}
    default_currency_by_account: dict[str, str] = {}
    if config is not None:
        for account_id in config.scheme.sub_accounts:
            risk_cfg = config.scheme.account_risk.get(account_id)
            if risk_cfg is not None:
                default_currency_by_account[account_id] = risk_cfg.currency
                account_states[(account_id, risk_cfg.currency)] = _BookedAccountState(
                    initial_capital=Fraction(risk_cfg.initial_capital),
                )

    postings_by_event_id = {entry.event_id: entry.postings for entry in journal.entries}

    for event in journal.events:
        if event.kind == "ACCOUNT_FUNDED":
            for currency, postings in _event_postings_map(event, postings_by_event_id).items():
                cash = postings.get("CASH_AVAILABLE", Fraction(0))
                if cash > 0:
                    state = account_states.setdefault((event.sub_account_id, currency), _BookedAccountState())
                    state.initial_capital = cash if state.initial_capital is None else state.initial_capital
                    default_currency_by_account.setdefault(event.sub_account_id, currency)

    # 推进已实现净值曲线（复用 summarize_ledger 口径，按 (sub_account_id, currency) 隔离）。
    events_by_id: dict[str, ExecutionEvent] = {
        event.event_id: event for event in journal.events if event.event_id is not None
    }
    equity_points: list[RunEquityPoint] = []
    for entry in journal.entries:
        event = events_by_id.get(entry.event_id)
        event_kind = event.kind if event is not None else "LEDGER_ENTRY"
        round_id = event.position.round_id if (event is not None and event.position is not None) else None
        by_currency: dict[str, dict[str, Fraction]] = defaultdict(lambda: defaultdict(Fraction))
        for posting in entry.postings:
            by_currency[posting.currency][posting.account] += Fraction(posting.amount)
        for currency in sorted(by_currency):
            postings = by_currency[currency]
            state = account_states.setdefault((entry.sub_account_id, currency), _BookedAccountState())
            pnl_delta = -postings["REALIZED_PNL"]
            fee_delta = postings["EXPENSE_EXECUTION_FEE"]
            funding_delta = -postings["FUNDING"]
            liq_delta = postings["EXPENSE_LIQUIDATION_FEE"]
            drawdown, max_dd = state.book(
                pnl=pnl_delta,
                fees=fee_delta,
                funding=funding_delta,
                liquidation_fees=liq_delta,
            )
            equity_value = (state.initial_capital or Fraction(0)) + state.net_pnl
            equity_points.append(
                RunEquityPoint(
                    sub_account_id=entry.sub_account_id,
                    currency=currency,
                    seq=state.seq,
                    ts_event=entry.ts_event,
                    event_id=entry.event_id,
                    event_kind=event_kind,
                    round_id=round_id,
                    pnl_basis="booked",
                    realized_pnl_delta=decimal_exact(pnl_delta),
                    fee_delta=decimal_exact(fee_delta),
                    funding_delta=decimal_exact(funding_delta),
                    liquidation_fee_delta=decimal_exact(liq_delta),
                    net_pnl=decimal_exact(state.net_pnl),
                    equity=decimal_exact(equity_value),
                    peak_pnl=decimal_exact(state.peak_pnl),
                    drawdown=decimal_exact(drawdown),
                    max_drawdown=decimal_exact(max_dd),
                )
            )

    # 按 (sub_account_id, round_id) 聚合回合明细。
    round_accumulators: dict[tuple[str, str], _RoundAccumulator] = {}
    distinct_rounds_by_key: dict[tuple[str, str], set[str]] = defaultdict(set)
    instruments_seen: set[str] = set()
    if replay_instrument is not None:
        instruments_seen.add(replay_instrument)

    for index, event in enumerate(journal.events, start=1):
        position = event.position
        if position is None:
            continue
        event_id = event.event_id or f"event-{index}"
        instruments_seen.add(position.instrument_id)
        postings_by_curr = _event_postings_map(event, postings_by_event_id)
        currency = (
            next(iter(sorted(postings_by_curr)))
            if postings_by_curr
            else default_currency_by_account.get(event.sub_account_id, "USDT")
        )
        key = (event.sub_account_id, position.round_id)
        acc = round_accumulators.get(key)
        if acc is None:
            phase_val = position.phase.value if position.phase is not None else RunRoundPhase.PENDING_ENTRY.value
            acc = _RoundAccumulator(
                round_id=position.round_id,
                sub_account_id=event.sub_account_id,
                currency=currency,
                instrument_id=position.instrument_id,
                direction=position.direction,
                phase=phase_val,
                stop=position.stop,
                take_profit=list(event.take_profit) if event.take_profit else [],
                remaining_quantity=Fraction(position.quantity),
            )
            round_accumulators[key] = acc
        distinct_rounds_by_key[(event.sub_account_id, acc.currency)].add(position.round_id)

        if position.phase is not None:
            acc.phase = position.phase.value
        acc.stop = position.stop
        acc.remaining_quantity = Fraction(position.quantity)
        acc.max_quantity = max(acc.max_quantity, acc.remaining_quantity)
        if event.take_profit:
            acc.take_profit = list(event.take_profit)
        elif event.barriers is not None and event.barriers.take_profit:
            acc.take_profit = list(event.barriers.take_profit)

        event_postings = postings_by_curr.get(acc.currency, {})
        pnl_delta = -event_postings.get("REALIZED_PNL", Fraction(0))
        fee_delta = event_postings.get("EXPENSE_EXECUTION_FEE", Fraction(0))
        funding_delta = -event_postings.get("FUNDING", Fraction(0))
        liq_delta = event_postings.get("EXPENSE_LIQUIDATION_FEE", Fraction(0))
        acc.fees += fee_delta
        acc.funding += funding_delta
        acc.liquidation_fees += liq_delta
        acc.net_pnl += pnl_delta - fee_delta + funding_delta - liq_delta

        if event.kind in ("ENTRY_FILLED", "EXIT_FILLED") and event.price is not None and event.quantity is not None:
            qty_frac = Fraction(event.quantity)
            px_frac = Fraction(event.price)
            if acc.multiplier is None and "POSITION_COST" in event_postings and qty_frac > 0 and px_frac > 0:
                acc.multiplier = abs(event_postings["POSITION_COST"]) / (qty_frac * px_frac)
            fill_kind = RunFillKind(event.kind)
            reason = _normalize_fill_reason(event.kind, event.reason)
            acc.fills.append(
                RunRoundFill(
                    event_id=event_id,
                    ts_event=event.ts_event,
                    kind=fill_kind,
                    side=_fill_side(acc.direction, event.kind),
                    price=event.price,
                    quantity=event.quantity,
                    reason=reason,
                    raw_reason=event.reason,
                    fill_tier=event.fill_tier,
                    realized_pnl=decimal_exact(pnl_delta) if event.kind == "EXIT_FILLED" else None,
                    fee=decimal_exact(fee_delta) if fee_delta != 0 else None,
                )
            )
            if event.kind == "ENTRY_FILLED":
                if acc.opened_ts is None or event.ts_event < acc.opened_ts:
                    acc.opened_ts = event.ts_event
                acc.entry_qty_sum += qty_frac
                acc.entry_notional_sum += qty_frac * px_frac
            else:
                acc.exit_qty_sum += qty_frac
                acc.exit_notional_sum += qty_frac * px_frac
                acc.exit_reason = reason
                acc.raw_exit_reason = event.reason
                if acc.remaining_quantity == 0:
                    acc.closed_ts = event.ts_event
                    state = account_states.setdefault((event.sub_account_id, acc.currency), _BookedAccountState())
                    state.trade_count += 1
        elif event.kind in ("BARRIER_TIGHTENED", "BARRIER_AMENDED"):
            acc.barrier_changes.append(
                RunBarrierChange(
                    event_id=event_id,
                    ts_event=event.ts_event,
                    kind=RunBarrierChangeKind(event.kind),
                    stop=position.stop,
                    take_profit=list(event.take_profit) if event.take_profit else list(acc.take_profit),
                    reason=event.reason,
                    amendment_id=event.amendment_id,
                )
            )

    # 加载可用 K 线计算可选 MAE / MFE。
    bars_cache: dict[str, list[tuple[int, int, Decimal, Decimal]] | None] = {}
    round_records: list[RunRoundRecord] = []
    for key in sorted(round_accumulators, key=lambda item: (
        item[0],
        round_accumulators[item].opened_ts if round_accumulators[item].opened_ts is not None else 2**63 - 1,
        item[1],
    )):
        acc = round_accumulators[key]
        bars = bars_cache.get(acc.instrument_id)
        if acc.instrument_id not in bars_cache and acc.opened_ts is not None:
            start_ts = max(0, (replay_start or acc.opened_ts) - timeframe_ms(replay_timeframe or "1m"))
            end_ts = replay_end or acc.closed_ts or max((f.ts_event for f in acc.fills), default=acc.opened_ts)
            bars = _load_round_bars(
                config,
                data_dir=data_dir,
                instrument_id=acc.instrument_id,
                timeframe=replay_timeframe,
                start=start_ts,
                end=end_ts,
            )
            bars_cache[acc.instrument_id] = bars
        mae, mfe = _compute_round_excursions(acc, bars=bars)
        round_records.append(
            RunRoundRecord(
                round_id=acc.round_id,
                sub_account_id=acc.sub_account_id,
                currency=acc.currency,
                instrument_id=acc.instrument_id,
                direction=acc.direction,
                phase=RunRoundPhase(acc.phase),
                opened_ts=acc.opened_ts,
                closed_ts=acc.closed_ts,
                entry_price=decimal_exact(acc.entry_notional_sum / acc.entry_qty_sum) if acc.entry_qty_sum > 0 else None,
                exit_price=decimal_exact(acc.exit_notional_sum / acc.exit_qty_sum) if acc.exit_qty_sum > 0 else None,
                max_quantity=decimal_exact(acc.max_quantity),
                remaining_quantity=decimal_exact(acc.remaining_quantity),
                stop=acc.stop,
                take_profit=acc.take_profit,
                exit_reason=acc.exit_reason,
                raw_exit_reason=acc.raw_exit_reason,
                net_pnl=decimal_exact(acc.net_pnl),
                fees=decimal_exact(acc.fees),
                funding=decimal_exact(acc.funding),
                liquidation_fees=decimal_exact(acc.liquidation_fees),
                mae=mae,
                mfe=mfe,
                fills=acc.fills,
                barrier_changes=acc.barrier_changes,
            )
        )

    equity_points.sort(key=lambda pt: (pt.sub_account_id, pt.currency, pt.seq))

    accounts_summary: list[RunAccountCurrencySummary] = []
    for account_id, currency in sorted(account_states):
        state = account_states[(account_id, currency)]
        initial_cap = state.initial_capital
        final_eq = (initial_cap + state.net_pnl) if initial_cap is not None else None
        accounts_summary.append(
            RunAccountCurrencySummary(
                sub_account_id=account_id,
                currency=currency,
                round_count=len(distinct_rounds_by_key.get((account_id, currency), set())),
                trade_count=state.trade_count,
                net_pnl=decimal_exact(state.net_pnl),
                max_drawdown=decimal_exact(state.max_drawdown),
                fees=decimal_exact(state.fees),
                funding=decimal_exact(state.funding),
                liquidation_fees=decimal_exact(state.liquidation_fees),
                initial_capital=decimal_exact(initial_cap) if initial_cap is not None else None,
                final_equity=decimal_exact(final_eq) if final_eq is not None else None,
            )
        )

    instruments_sorted = sorted(instruments_seen)
    primary_instrument = replay_instrument or (instruments_sorted[0] if len(instruments_sorted) == 1 else None)
    summary = RunExportSummary(
        schema_version=1,
        run_id=completion.run_id,
        status=RunStatus(completion.status),
        error=completion.error,
        scheme=scheme_name,
        fill_plugin=fill_plugin,
        instrument_id=primary_instrument,
        instruments=instruments_sorted,
        timeframe=replay_timeframe,
        start=replay_start,
        end=replay_end,
        signal_bars=completion.signal_bars,
        pnl_basis="booked",
        artifacts=RunExportArtifacts(
            rounds_path="rounds.parquet",
            equity_path="equity.parquet",
            overlays_path=None,
        ),
        accounts=accounts_summary,
    )

    rounds_table = _rounds_to_table(round_records)
    equity_table = _equity_to_table(equity_points)
    _atomic_write_parquet(run_dir / "rounds.parquet", rounds_table)
    _atomic_write_parquet(run_dir / "equity.parquet", equity_table)
    # 必须最后原子写入 summary.json 作为只读服务的就绪标记。
    _atomic_write_bytes(run_dir / "summary.json", (summary.model_dump_json(indent=2) + "\n").encode("utf-8"))
    return summary
