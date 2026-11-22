"""装配账户进程并运行数据、策略、定仓与风控、成交、记账流水线。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from talaria.account.contracts import (
    DataEntry,
    DecisionOrigin,
    EntryCandidate,
    ExecutionEvent,
    FillSlot,
    MarginValuation,
    RejectedPlanRecord,
    RiskSlot,
    SizingSlot,
)
from talaria.account.execution import AccountProcessError, ExecutionGate
from talaria.account.ledger import open_ledger
from talaria.account.ownership import hold_sub_accounts
from talaria.account.plugins.parquet import parquet_replay
from talaria.account.scheme import (
    Mode,
    PluginContext,
    PluginRegistry,
    SchemeError,
    Slot,
    TradingScheme,
    load_scheme,
)
from talaria.components import ComponentMeta, ControllerPatch, Strategy
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    directory: Path


class _EventClock:
    def __init__(self) -> None:
        self._now: int | None = None

    def now(self) -> int:
        if self._now is None:
            raise AccountProcessError("数据入口尚未推进时钟")
        return self._now

    def advance(self, as_of: int) -> None:
        if type(as_of) is not int or not 0 <= as_of <= 2**63 - 1:
            raise AccountProcessError("数据入口的 as_of 必须是 UTC 毫秒整数")
        if self._now is not None and as_of < self._now:
            raise AccountProcessError("数据入口的 as_of 不得倒退")
        self._now = as_of


@dataclass(frozen=True, slots=True)
class _Plugins:
    data: DataEntry
    strategy: Strategy
    sizing: SizingSlot
    risk: RiskSlot
    fill: FillSlot


def default_registry() -> PluginRegistry:
    registry = PluginRegistry()
    registry.register("parquet_replay", parquet_replay)
    return registry


def _assemble(
    scheme: TradingScheme, scheme_dir: Path, registry: PluginRegistry, context: PluginContext,
) -> _Plugins:
    expected_interfaces = {
        Slot.DATA: DataEntry, Slot.SIZING: SizingSlot, Slot.RISK: RiskSlot, Slot.FILL: FillSlot,
    }
    definitions = []
    for slot in (Slot.DATA, Slot.STRATEGY, Slot.SIZING, Slot.RISK, Slot.FILL):
        selection = scheme.slots.selection(slot)
        plugin = registry.resolve(
            slot, selection.reference, scheme.interface_version, scheme_dir, mode=context.mode,
        )
        definitions.append((slot, selection, plugin))

    slots = {}
    for slot, selection, definition in definitions:
        try:
            plugin = definition.create(context, selection.params)
        except Exception as exc:
            raise SchemeError(f"{slot} 插槽插件 {selection.reference}：{exc}") from exc
        if slot == Slot.STRATEGY:
            meta = getattr(plugin, "meta", None)
            matches = (
                isinstance(meta, ComponentMeta) and meta.output_type == "strategy_decision"
                and callable(getattr(plugin, "on_bar", None))
            )
        else:
            matches = isinstance(plugin, expected_interfaces[slot])
        if not matches:
            raise SchemeError(f"{slot} 插槽插件 {selection.reference} 未实现接口")
        slots[slot.value] = plugin
    return _Plugins(**slots)


def _reject(sub_account_id: str, now: int, reason: str) -> ExecutionEvent:
    return ExecutionEvent(
        kind="CANDIDATE_REJECTED", sub_account_id=sub_account_id, ts_event=now, reason=reason,
    )


def _evaluate_intent(
    decision: SetupInsight | ControllerPatch | Abstain, gate: ExecutionGate,
    sub_account_id: str, instrument_id: str, now: int,
) -> ExecutionEvent | None:
    if isinstance(decision, Abstain):
        return None
    if isinstance(decision, ControllerPatch):
        return _reject(sub_account_id, now, "CONTROLLER_PATCH_UNSUPPORTED")
    if not isinstance(decision, SetupInsight):
        return _reject(sub_account_id, now, "INVALID_STRATEGY_DECISION")
    if decision.max_position is not None:
        return _reject(sub_account_id, now, "STRATEGY_QUANTITY_NOT_ALLOWED")
    if decision.direction == "NEUTRAL":
        return None
    if decision.valid_until is not None and decision.valid_until < now:
        return _reject(sub_account_id, now, "STRATEGY_INTENT_EXPIRED")

    candidate = EntryCandidate(
        sub_account_id=sub_account_id, instrument_id=instrument_id,
        direction=decision.direction.value, entry_low=decision.entry_low, entry_high=decision.entry_high,
        invalidation_stop=decision.invalidation_stop, take_profit=tuple(decision.take_profit),
        ts_decision=now, origin=DecisionOrigin.AGENT, valid_until=decision.valid_until,
        evidence_ids=tuple(ref.evidence_id for ref in decision.evidence_refs),
    )
    receipt = gate.submit_candidate(candidate)
    if isinstance(receipt, RejectedPlanRecord) or not receipt.accepted:
        return _reject(sub_account_id, now, receipt.reason or "FILL_REJECTED")
    return None


class AccountLedger(Protocol):
    def balances(self, sub_account_id: str) -> Mapping[tuple[str, str], Decimal]: ...
    def record(self, events: Sequence[ExecutionEvent]) -> None: ...


@dataclass(frozen=True, slots=True)
class AccountProcess:
    scheme: TradingScheme
    _plugins: _Plugins
    _clock: _EventClock

    def run(self, ledger: AccountLedger, *, valuation: MarginValuation | None = None) -> None:
        plugins = self._plugins
        accounts = self.scheme.sub_accounts
        gate = ExecutionGate(
            accounts, plugins.sizing, plugins.risk, plugins.fill,
            balances=ledger.balances, valuation=valuation,
        )
        for market in plugins.data.events():
            self._clock.advance(market.as_of)
            if market.timeframe not in SUPPORTED_TIMEFRAMES:
                raise AccountProcessError(f"数据入口使用了未知周期 {market.timeframe}")
            earliest = market.bar.ts_event + (timeframe_ms(market.timeframe) if market.bar.confirmed else 0)
            if max(earliest, market.bar.ts_available) > market.as_of:
                raise AccountProcessError("数据入口提前发布了行情")
            rejected = []
            if (ctx := market.ctx) is not None:
                if (
                    (ctx.now, ctx.instrument, ctx.timeframe)
                    != (market.as_of, market.instrument_id, market.timeframe)
                    or not market.bar.confirmed or ctx.bars()[-1:] != (market.bar,)
                ):
                    raise AccountProcessError("策略只能在已确认柱的可获取时点求值")
                decision = plugins.strategy.on_bar(ctx)
                for account in accounts:
                    rejection = _evaluate_intent(decision, gate, account, market.instrument_id, market.as_of)
                    if rejection is not None:
                        rejected.append(rejection)
            plugins.fill.on_market(market, valuation)
            ledger.record([*rejected, *gate.poll_events(market.as_of)])


@contextmanager
def open_account_process(
    scheme_path: Path, data_dir: Path, *, mode: Mode, registry: PluginRegistry | None = None,
) -> Iterator[AccountProcess]:
    """装配账户进程，模拟盘在整个运行期间独占方案声明的子账户。"""
    if mode not in ("backtest", "paper"):
        raise SchemeError(f"不支持的账户进程模式：{mode}")
    scheme = load_scheme(scheme_path)
    data_dir = data_dir.expanduser().resolve()
    clock = _EventClock()
    context = PluginContext(data_dir, mode, scheme.seed, clock.now)
    ownership = hold_sub_accounts(data_dir, scheme.sub_accounts) if mode == "paper" else nullcontext()
    with ownership:
        plugins = _assemble(scheme, scheme_path.resolve().parent, registry or default_registry(), context)
        yield AccountProcess(scheme, plugins, clock)


def run_backtest(
    scheme_path: Path, data_dir: Path, *, registry: PluginRegistry | None = None,
    valuation: MarginValuation | None = None,
) -> RunResult:
    """用交易方案回放，独占新建的 runs/<run_id> 账本。"""
    with open_account_process(scheme_path, data_dir, mode="backtest", registry=registry) as process:
        run_id = uuid4().hex
        directory = data_dir.expanduser().resolve() / "runs" / run_id
        directory.mkdir(parents=True)
        with open_ledger(directory / "ledger.sqlite", run_id, process.scheme.sub_accounts) as ledger:
            process.run(ledger, valuation=valuation)
    return RunResult(run_id, directory)
