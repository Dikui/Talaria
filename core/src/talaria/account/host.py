"""装配账户进程并运行数据、策略、定仓与风控、成交、记账流水线。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from talaria import sdk
from talaria.account.contracts import (
    DataEntry,
    DecisionOrigin,
    EntryCandidate,
    ExecutionEvent,
    FillSlot,
    LedgerPosting,
    MarginValuation,
    MarketData,
    MarketEvent,
    RiskSlot,
    SizingSlot,
    TradingState,
)
from talaria.account.execution import AccountProcessError, ExecutionGate
from talaria.account.ledger import open_ledger
from talaria.account.ownership import AccountOwnership, hold_sub_accounts
from talaria.account.parameters import validate_parameters
from talaria.account.plugins.parquet import parquet_replay
from talaria.account.plugins.r_unit import RUnitSizer, r_unit
from talaria.account.plugins.seven_step import SevenStepRisk, seven_step
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import (
    Mode,
    PluginContext,
    PluginRegistry,
    SchemeError,
    Slot,
    TradingScheme,
    load_scheme,
)
from talaria.components import ComponentMeta, ControllerPatch, Ctx, Strategy
from talaria.foundation.bars import RangeBar
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.margin import MARGIN_VALUATION_VERSION
from talaria.foundation.operators import IndicatorName
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    directory: Path


class _RunConfig(ContractModel):
    version: Literal[1]
    scheme: TradingScheme
    scheme_dir: Path
    data_dir: Path
    indicator_defaults: dict[IndicatorName, IndicatorParams]


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
    registry.register("r_unit", r_unit)
    registry.register("seven_step", seven_step)
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
            schema = definition.parameters_schema
            if schema is not None or slot != Slot.STRATEGY:
                validate_parameters(selection.params, schema if schema is not None else {})
            plugin_context = replace(context, account_risk=deepcopy(context.account_risk))
            plugin = definition.create(plugin_context, deepcopy(selection.params))
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
        if slot == Slot.STRATEGY:
            try:
                validate_parameters(selection.params, meta.parameters_schema)
            except (TypeError, ValueError) as exc:
                raise SchemeError(f"{slot} 插槽插件 {selection.reference}：{exc}") from exc
        slots[slot.value] = plugin
    return _Plugins(**slots)


def _reject(sub_account_id: str, now: int, reason: str) -> ExecutionEvent:
    return ExecutionEvent(
        kind="CANDIDATE_REJECTED", sub_account_id=sub_account_id, ts_event=now, reason=reason,
    )


def _evaluate_intent(
    decision: SetupInsight | ControllerPatch | Abstain, gate: ExecutionGate,
    sub_account_id: str, instrument_id: str, now: int, signal_bar_close: int,
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

    matching_rounds = [
        position for position in gate.account_state(sub_account_id).rounds.values()
        if (position.instrument_id, position.direction) == (instrument_id, decision.direction.value)
    ]
    position = matching_rounds[0] if len(matching_rounds) == 1 else None
    candidate = EntryCandidate(
        sub_account_id=sub_account_id, instrument_id=instrument_id,
        direction=decision.direction.value, entry_low=decision.entry_low, entry_high=decision.entry_high,
        invalidation_stop=position.stop if position is not None else decision.invalidation_stop,
        take_profit=tuple(decision.take_profit), round_id=position.round_id if position is not None else None,
        ts_decision=now, origin=DecisionOrigin.AGENT, valid_until=decision.valid_until,
        signal_bar_close=signal_bar_close,
        evidence_ids=tuple(ref.evidence_id for ref in decision.evidence_refs),
    )
    gate.submit_candidate(candidate)
    return None


class AccountLedger(Protocol):
    def balances(self, sub_account_id: str) -> Mapping[tuple[str, str], Decimal]: ...
    def record(self, events: Sequence[ExecutionEvent]) -> None: ...


@dataclass(frozen=True, slots=True)
class AccountProcess:
    scheme: TradingScheme
    _plugins: _Plugins
    _clock: _EventClock
    _indicator_defaults: Mapping[IndicatorName, IndicatorParams]
    _ownership: AccountOwnership | None

    def run(self, ledger: AccountLedger, *, valuation: MarginValuation | None = None) -> None:
        guard = self._ownership.guard if self._ownership is not None else nullcontext
        with guard():
            pass

        def record(events: Sequence[ExecutionEvent]) -> None:
            with guard():
                ledger.record(events)

        plugins = self._plugins
        accounts = self.scheme.sub_accounts
        default_risk = isinstance(plugins.risk, SevenStepRisk) or isinstance(plugins.sizing, RUnitSizer)
        if valuation is None and default_risk:
            valuation = get_market_pack(DEFAULT_PACK).margin_valuation()
        if valuation is not None and getattr(valuation, "interface_version", None) != MARGIN_VALUATION_VERSION:
            raise SchemeError(f"保证金估值须兼容接口版本 {MARGIN_VALUATION_VERSION}")
        gate = ExecutionGate(
            accounts, plugins.sizing, plugins.risk, plugins.fill,
            balances=ledger.balances, valuation=valuation, record=record,
        )
        self._initialize_risk_accounts(ledger, gate, default_risk=default_risk)
        now = 0
        for market in plugins.data.events():
            with guard():
                pass
            try:
                if not isinstance(market, MarketEvent) or not isinstance(market.bar, RangeBar):
                    raise AccountProcessError("数据入口须返回携带 RangeBar 的 MarketEvent")
                self._clock.advance(market.as_of)
                now = self._clock.now()
                signal_close, ctx = self._prepare_market(market, now)
            except AccountProcessError as exc:
                gate.record_host_event(ExecutionEvent(
                    kind="DATA_REJECTED", sub_account_id=accounts[0], ts_event=now, reason=str(exc),
                ))
                raise

            visible = tuple(
                point for point in market.derivative_points if point.visible_at(now)
            )
            fill_market = replace(
                market, bar=market.bar.model_copy(), ctx=None, source=None,
                derivative_points=tuple(point.model_copy() for point in visible),
            )
            plugins.fill.on_market(fill_market, valuation)
            gate.poll_events(now, points=visible)
            if ctx is not None:
                rejected = []
                decision = plugins.strategy.on_bar(ctx)
                try:
                    for account in accounts:
                        try:
                            rejection = _evaluate_intent(
                                decision, gate, account, market.instrument_id, now, signal_close,
                            )
                        except AccountProcessError as exc:
                            rejected.append(_reject(account, now, str(exc)))
                            raise
                        if rejection is not None:
                            rejected.append(rejection)
                    gate.poll_events(now)
                finally:
                    for rejection in rejected:
                        gate.record_host_event(rejection)

    def _initialize_risk_accounts(
        self, ledger: AccountLedger, gate: ExecutionGate, *, default_risk: bool,
    ) -> None:
        for account in self.scheme.sub_accounts:
            config = self.scheme.account_risk.get(account)
            if config is None:
                if not default_risk:
                    continue
                config = AccountRiskConfig()
            if not ledger.balances(account):
                gate.record_host_event(ExecutionEvent(
                    kind="ACCOUNT_FUNDED", sub_account_id=account, ts_event=0,
                    postings=(
                        LedgerPosting(account="CASH_AVAILABLE", currency=config.currency, amount=config.initial_capital),
                        LedgerPosting(
                            account="OWNER_CAPITAL", currency=config.currency, amount=config.initial_capital.copy_negate(),
                        ),
                    ),
                ))
            initial_state = TradingState(config.trading_state)
            if initial_state != TradingState.ACTIVE:
                gate.set_trading_state(account, initial_state, "ACCOUNT_CONFIG")

    def _prepare_market(self, market: MarketEvent, now: int) -> tuple[int, Ctx | None]:
        if market.timeframe not in SUPPORTED_TIMEFRAMES:
            raise AccountProcessError(f"数据入口使用了未知周期 {market.timeframe}")
        bar = market.bar
        close = bar.ts_event + timeframe_ms(market.timeframe) if bar.confirmed else bar.ts_event
        if max(close, bar.ts_available) > now:
            raise AccountProcessError("数据入口提前发布了行情")
        source = market.source
        if source is None and market.ctx is not None:
            source = MarketData.from_ctx(market.ctx)
        if source is None:
            return close, None
        if not bar.confirmed or not 0 <= source.history_start <= bar.ts_event:
            raise AccountProcessError("策略只能在已确认柱的可获取时点求值")
        ctx = Ctx(
            market.instrument_id, market.timeframe, source.history_start, now,
            _read_bars=source.read_bars, _read_derivatives=source.read_derivatives,
            _bar_ts=bar.ts_event, _discovery_tail_completed=source.tail_completed,
            _indicator_defaults=self._indicator_defaults,
        )
        if ctx.bars()[-1:] != (bar,):
            raise AccountProcessError("策略求值的行情与已确认柱不一致")
        return close, ctx


@contextmanager
def _open_process(
    config: _RunConfig, mode: Mode, registry: PluginRegistry | None,
) -> Iterator[AccountProcess]:
    scheme = config.scheme
    clock = _EventClock()
    context = PluginContext(config.data_dir, mode, scheme.seed, clock.now, scheme.account_risk)
    ownership = hold_sub_accounts(config.data_dir, scheme.sub_accounts) if mode == "paper" else nullcontext()
    with ownership as held:
        plugins = _assemble(scheme, config.scheme_dir, registry or default_registry(), context)
        yield AccountProcess(scheme, plugins, clock, config.indicator_defaults, held)


def _resolve_config(scheme_path: Path, data_dir: Path) -> _RunConfig:
    scheme = load_scheme(scheme_path)
    if scheme.slots.selection(Slot.SIZING).reference == "r_unit" or (
        scheme.slots.selection(Slot.RISK).reference == "seven_step"
    ):
        scheme = scheme.model_copy(update={"account_risk": {
            account: scheme.account_risk.get(account) or AccountRiskConfig() for account in scheme.sub_accounts
        }})
    data_dir = data_dir.expanduser().resolve()
    return _RunConfig(
        version=1, scheme=scheme, scheme_dir=scheme_path.resolve().parent, data_dir=data_dir,
        indicator_defaults=sdk.indicator_defaults(data_dir=data_dir),
    )


@contextmanager
def open_account_process(
    scheme_path: Path, data_dir: Path, *, mode: Mode, registry: PluginRegistry | None = None,
) -> Iterator[AccountProcess]:
    """装配账户进程，模拟盘在整个运行期间独占方案声明的子账户。"""
    if mode not in ("backtest", "paper"):
        raise SchemeError(f"不支持的账户进程模式：{mode}")
    with _open_process(_resolve_config(scheme_path, data_dir), mode, registry) as process:
        yield process


def run_backtest(
    scheme_path: Path, data_dir: Path | None, *, registry: PluginRegistry | None = None,
    valuation: MarginValuation | None = None, replay: bool = False,
) -> RunResult:
    """回测交易方案或运行配置产物，写入新建的 runs/<run_id>。"""
    if replay:
        config = _RunConfig.model_validate_json(scheme_path.read_text(encoding="utf-8"))
        if data_dir is not None:
            config = config.model_copy(update={"data_dir": data_dir.expanduser().resolve()})
    else:
        config = _resolve_config(scheme_path, data_dir if data_dir is not None else Path(".talaria"))
    with _open_process(config, "backtest", registry) as process:
        run_id = uuid4().hex
        directory = config.data_dir / "runs" / run_id
        directory.mkdir(parents=True)
        (directory / "run-config.json").write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
        with open_ledger(directory / "ledger.sqlite", run_id, process.scheme.sub_accounts) as ledger:
            process.run(ledger, valuation=valuation)
    return RunResult(run_id, directory)
