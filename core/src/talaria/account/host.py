"""装配账户进程并运行数据、策略、定仓与风控、成交、记账流水线。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from copy import copy, deepcopy
from dataclasses import dataclass, replace
from functools import cache
from decimal import Decimal
from pathlib import Path
import json
import re
import sys
from threading import RLock
from typing import Literal, Protocol
from uuid import uuid4

import httpx
from pydantic import Field

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
from talaria.account.ledger import LedgerWriter, open_ledger
from talaria.account.ownership import AccountOwnership, hold_sub_accounts
from talaria.account.parameters import validate_parameters
from talaria.account.paper import DraftIntake, PaperInbox
from talaria.account.paper_ledger import PaperLedger, open_paper_ledger
from talaria.account.run_export import export_run
from talaria.account.plugins.bar_sim_1m import BarSim1m, bar_sim_1m
from talaria.account.plugins.l1_trade_sim import L1TradeSim, l1_trade_sim
from talaria.account.plugins.live_ws import LiveWS, PaperMark, PaperPulse, live_ws
from talaria.account.plugins.parquet import (
    ParquetReplay, ReplayMarkAvailability, ReplayParams, TickMarketEvent, capture_replay_snapshot, parquet_replay,
)
from talaria.account.plugins.r_unit import RUnitSizer, r_unit
from talaria.account.plugins.seven_step import SevenStepRisk, seven_step
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import (
    Mode,
    PluginContext,
    PluginRegistry,
    SchemeError,
    Slot,
    SlotPlugin,
    TradingScheme,
    check_plugin,
    load_python_module,
    load_scheme,
    python_reference,
)
from talaria.account.strategy_code import ContentHash, StrategyCodeRegistry, StrategySource
from talaria.account.strategy_isolation import IsolatedStrategy, isolated_strategy_plugin
from talaria.account.strategy_snapshot import StrategySnapshot
from talaria.components import ComponentMeta, ControllerPatch, Ctx, Strategy
from talaria.foundation.bars import RangeBar
from talaria.foundation.collaboration.contracts import Abstain, SetupInsight
from talaria.foundation.indicators import IndicatorParams
from talaria.foundation.margin import MARGIN_VALUATION_VERSION
from talaria.foundation.operators import IndicatorName
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES, timeframe_ms
from talaria.foundation.types import ContractModel
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdReader, ColdSnapshot


class RunCompletion(ContractModel):
    run_id: str
    status: Literal["succeeded", "failed"]
    error: str | None = None
    signal_bars: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    directory: Path

    @classmethod
    def new(cls, data_dir: Path) -> RunResult:
        run_id = uuid4().hex
        return cls(run_id, data_dir / "runs" / run_id)

    @property
    def state_dir(self) -> Path:
        return self.directory / "state"

    @property
    def config_path(self) -> Path:
        return self.directory / "run-config.json"

    @property
    def _status_path(self) -> Path:
        return self.directory / "run-status.json"

    def prepare(self, config: RunConfig) -> None:
        self.directory.mkdir(parents=True)
        try:
            self.config_path.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
            self.state_dir.mkdir()
        except (OSError, ValueError) as exc:
            self.mark_failed(str(exc) or type(exc).__name__)
            raise

    def read_completion(self) -> RunCompletion:
        completion = RunCompletion.model_validate_json(self._status_path.read_text(encoding="utf-8"))
        if completion.run_id != self.run_id or (completion.status == "failed") != bool(completion.error):
            raise SchemeError(f"{self.run_id}：无效的回放完成状态")
        return completion

    def mark_failed(self, error: str) -> None:
        self._write_completion(RunCompletion(run_id=self.run_id, status="failed", error=error))

    def _write_completion(self, completion: RunCompletion) -> None:
        temporary = self._status_path.with_suffix(".tmp")
        temporary.write_text(completion.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._status_path)

    @contextmanager
    def track(self) -> Iterator[RunCompletion]:
        completion = RunCompletion(run_id=self.run_id, status="succeeded")
        try:
            yield completion
        except BaseException as exc:
            self.mark_failed(str(exc) or type(exc).__name__)
            raise
        else:
            self._write_completion(completion)


class FilePluginCode(ContractModel):
    path: Path
    source: StrategySource
    sha256: ContentHash


class RunConfig(ContractModel):
    version: Literal[1]
    scheme: TradingScheme
    scheme_dir: Path
    data_dir: Path
    indicator_defaults: dict[IndicatorName, IndicatorParams]
    deterministic_only: bool = Field(default=False, exclude_if=lambda value: not value)
    cold_snapshot: ColdSnapshot | None = Field(default=None, exclude_if=lambda value: value is None)
    file_plugins: dict[Slot, FilePluginCode] | None = Field(default=None, exclude_if=lambda value: value is None)


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
    registry.register("live_ws", live_ws)
    registry.register("r_unit", r_unit)
    registry.register("seven_step", seven_step)
    registry.register("bar_sim_1m", bar_sim_1m)
    registry.register("l1_trade_sim", l1_trade_sim)
    return registry


def _assemble(
    scheme: TradingScheme, scheme_dir: Path, registry: PluginRegistry, context: PluginContext,
    file_plugins: dict[Slot, SlotPlugin],
) -> _Plugins:
    expected_interfaces = {
        Slot.DATA: DataEntry, Slot.SIZING: SizingSlot, Slot.RISK: RiskSlot, Slot.FILL: FillSlot,
    }
    definitions = []
    for slot in (Slot.DATA, Slot.STRATEGY, Slot.SIZING, Slot.RISK, Slot.FILL):
        selection = scheme.slots.selection(slot, context.mode)
        plugin = file_plugins.get(slot) or registry.resolve(
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
        if slot == Slot.STRATEGY and not isinstance(plugin, IsolatedStrategy):
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
class _Pipeline:
    consume: Callable[[MarketEvent, int, Ctx | None, Callable], None]
    finish: Callable[[], None]


@dataclass(frozen=True, slots=True)
class AccountProcess:
    scheme: TradingScheme
    _plugins: _Plugins
    _clock: _EventClock
    _indicator_defaults: Mapping[IndicatorName, IndicatorParams]
    _ownership: AccountOwnership | None
    _data_dir: Path | None = None

    def run(
        self, ledger: AccountLedger, *, valuation: MarginValuation | None = None, inbox: PaperInbox | None = None,
    ) -> int:
        """运行账户流水线，返回实际完成求值的信号柱数。"""
        with ExitStack() as resources:
            return self._run(ledger, resources, valuation=valuation, inbox=inbox)

    def _run(
        self, ledger: AccountLedger, resources: ExitStack, *,
        valuation: MarginValuation | None, inbox: PaperInbox | None,
    ) -> int:
        guard = self._ownership.guard if self._ownership is not None else nullcontext
        with guard():
            pass

        plugins = self._plugins
        data = plugins.data
        execution_replay = isinstance(plugins.fill, BarSim1m) and isinstance(data, ParquetReplay)
        tick_fill = plugins.fill if execution_replay and isinstance(plugins.fill, L1TradeSim) else None
        paper = isinstance(ledger, PaperLedger)
        # 各账本独立提交。同一输入中途宕机时，只有游标相同的子账户共享订阅。
        streams = {}
        groups = [(account,) for account in self.scheme.sub_accounts] if paper else [self.scheme.sub_accounts]
        for accounts in groups:
            state = ledger.checkpoint(accounts[0]) if paper else None
            feed_state = state.get("feed") if state else None
            key = json.dumps(feed_state, sort_keys=True)
            if state is None and paper and isinstance(data, LiveWS) and any(
                event.position is not None and event.sub_account_id == accounts[0] for event in ledger.previous
            ):
                key = accounts[0]  # 首次升级时按该账本的历史覆盖起点补齐资金费。
            if key not in streams:
                source = copy(data) if isinstance(data, LiveWS) else data
                if isinstance(source, LiveWS):
                    source.restore_checkpoint(feed_state if feed_state is not None else data.checkpoint())
                streams[key] = source, []
            source, pipelines = streams[key]
            risk = plugins.risk
            fill = plugins.fill
            if len(groups) > 1:
                risk = (SevenStepRisk(get_market_pack(DEFAULT_PACK), self.scheme.account_risk, paper=True)
                        if isinstance(risk, SevenStepRisk) else deepcopy(risk))
                fill = deepcopy(fill)
            process = replace(self, scheme=self.scheme.model_copy(update={"sub_accounts": accounts}),
                              _plugins=replace(plugins, data=source, risk=risk, fill=fill))
            pipeline = process._pipeline(ledger, valuation, inbox, execution_replay, state)
            pipelines.append(pipeline)

        def read_events(data) -> Iterator[MarketEvent]:
            if tick_fill is not None:
                return data.events(execution=True, quote_max_age_ms=tick_fill.quote_max_age_ms)
            events = data.events(execution=True) if execution_replay else data.events()
            if not isinstance(data, LiveWS):
                return events

            lock = RLock()

            def close() -> None:
                with lock:
                    events.close()

            def iterate() -> Iterator[MarketEvent]:
                while True:
                    with lock:
                        event = next(events, None)
                    if event is None:
                        return
                    yield event

            resources.callback(close)
            return iterate()

        subscriptions = []
        for source, pipelines in streams.values():
            markets = (plugins.strategy.market_events(lambda feed=source: read_events(feed))
                       if isinstance(plugins.strategy, IsolatedStrategy) else read_events(source))
            if tick_fill is not None:
                markets = tick_fill.clock_events(markets, end=source.end_event())
            subscriptions.append((iter(markets), pipelines))

        def deliveries():
            while subscriptions:
                for subscription in tuple(subscriptions):
                    markets, pipelines = subscription
                    try:
                        market = next(markets, None)
                    except KeyboardInterrupt:
                        for _, active in subscriptions:
                            for pipeline in active:
                                pipeline.finish()
                        raise
                    if market is None:
                        for pipeline in pipelines:
                            pipeline.finish()
                        subscriptions.remove(subscription)
                    else:
                        yield market, pipelines

        last_delivery = 0
        signal_bars = 0
        for market, pipelines in deliveries():
            with guard():
                if not isinstance(market, MarketEvent):
                    raise AccountProcessError("数据入口须返回 MarketEvent")
                if len(streams) > 1 and market.as_of < last_delivery:
                    market = replace(market, as_of=last_delivery)
                last_delivery = market.as_of
                self._clock.advance(market.as_of)
                now = self._clock.now()
                try:
                    signal_close, ctx = (now, None) if isinstance(market, PaperPulse) else self._prepare_market(market, now)
                except AccountProcessError as exc:
                    account = self.scheme.sub_accounts[0]
                    with ledger.transaction(account) if paper else nullcontext():
                        previous = ledger.checkpoint(account) if paper else None
                        ledger.record([ExecutionEvent(kind="DATA_REJECTED", sub_account_id=account,
                                                      ts_event=now, reason=str(exc))])
                        if previous is not None:
                            ledger.save_checkpoint(account, previous)
                    raise

                @cache
                def decide():
                    nonlocal signal_bars
                    decision = plugins.strategy.on_bar(ctx)
                    signal_bars += 1
                    return decision

                for pipeline in pipelines:
                    pipeline.consume(market, signal_close, ctx, decide)
        return signal_bars

    def _pipeline(
        self, ledger: AccountLedger, valuation: MarginValuation | None, inbox: PaperInbox | None,
        execution_replay: bool, state: dict | None,
    ) -> _Pipeline:
        plugins = self._plugins
        accounts = self.scheme.sub_accounts
        account = accounts[0]
        data = plugins.data
        paper = isinstance(ledger, PaperLedger)
        default_risk = isinstance(plugins.risk, SevenStepRisk) or isinstance(plugins.sizing, RUnitSizer)
        default_fill = isinstance(plugins.fill, BarSim1m)
        if valuation is None and (default_risk or default_fill):
            valuation = get_market_pack(DEFAULT_PACK).margin_valuation()
        if valuation is not None and getattr(valuation, "interface_version", None) != MARGIN_VALUATION_VERSION:
            raise SchemeError(f"保证金估值须兼容接口版本 {MARGIN_VALUATION_VERSION}")
        tick_replay = execution_replay and isinstance(plugins.fill, L1TradeSim)
        tick_step_options = {"tick_stream": True} if tick_replay else {}
        minute_execution = (execution_replay and not tick_replay) or isinstance(data, LiveWS)

        def record(events):
            guard = self._ownership.guard if self._ownership is not None else nullcontext
            with guard():
                if default_fill and isinstance(ledger, LedgerWriter):
                    ledger.record(events, allow_late_events=True)
                else:
                    ledger.record(events)

        gate = ExecutionGate(accounts, plugins.sizing, plugins.risk, plugins.fill, balances=ledger.balances,
                             valuation=valuation, record=record, allow_late_events=default_fill)
        now = 0
        execution_through = 0
        signal_through = -1
        previous = [e for e in ledger.previous if e.sub_account_id == account] if paper else []
        if state is not None:
            if state["version"] != 1:
                raise AccountProcessError("不支持的模拟盘检查点版本")
            if isinstance(data, LiveWS) and state["instrument"] != data.params.instrument:
                raise AccountProcessError("恢复账本所需的实时标的与方案不符")
            if state["fill"] is not None:
                plugins.fill.restore_checkpoint(state["fill"])
            if state["risk"] is not None:
                plugins.risk.restore_checkpoint(state["risk"])
            now, execution_through, signal_through = state["now"], state["execution_through"], state["signal_through"]
        elif any(e.position is not None for e in previous):
            if not default_fill:
                raise AccountProcessError("旧模拟盘账本需要原成交插件的恢复支持")
            plugins.fill.restore_closed_history(previous)
            gate.restore_closed_history(previous)
            if isinstance(data, LiveWS):
                first = min(e.ts_event for e in previous if e.position is not None)
                data.resume_history(first)

        def checkpoint():
            if paper:
                ledger.save_checkpoint(account, {
                    "version": 1, "now": now, "execution_through": execution_through, "signal_through": signal_through,
                    "instrument": data.params.instrument if isinstance(data, LiveWS) else None,
                    "feed": data.checkpoint() if isinstance(data, LiveWS) else None,
                    "gate": gate.checkpoint(),
                    "fill": plugins.fill.checkpoint() if default_fill else None,
                    "risk": plugins.risk.checkpoint() if isinstance(plugins.risk, SevenStepRisk) else None,
                })

        with ledger.transaction(account) if paper else nullcontext():
            if state is not None:
                gate.restore_checkpoint(state["gate"])
            else:
                self._initialize_risk_accounts(ledger, gate, default_risk=default_risk or default_fill)
                for event in previous:
                    if event.kind == "TRADING_STATE_CHANGED" and event.reason:
                        reason, _, value = event.reason.rpartition(":")
                        if reason != "PAPER_STOPPING":
                            gate.set_trading_state(account, TradingState(value), reason)
            checkpoint()

        def submit_draft(candidate):
            receipt = gate.submit_candidate(candidate)
            gate.poll_events(candidate.ts_decision)
            return receipt

        intake = DraftIntake(inbox.freeze, submit_draft, gate.record_host_event, previous) if paper and inbox else None

        def process_market(market, now, signal_close, ctx, decide):
            nonlocal execution_through, signal_through
            visible = tuple(
                point for point in market.derivative_points if point.visible_at(now)
            )
            execution_bar = market.bar
            if execution_replay and ctx is not None:
                execution_bar = None
            elif minute_execution and execution_bar is not None and market.timeframe == "1m":
                execution_through = execution_bar.ts_event + timeframe_ms(market.timeframe)
            fill_market = replace(
                market, bar=execution_bar.model_copy() if execution_bar is not None else None, ctx=None, source=None,
                derivative_points=tuple(point.model_copy() for point in visible),
            )
            if isinstance(plugins.fill, BarSim1m):
                pricing_only = isinstance(market, (ReplayMarkAvailability, PaperMark))
                if pricing_only:
                    gate.observe_marks_for_approval(now, visible)
                for points in plugins.fill.market_steps(
                    fill_market, valuation, balances=ledger.balances,
                    execution_through=execution_through if minute_execution else None,
                    pricing_only=pricing_only, **tick_step_options,
                ):
                    gate.poll_events(now, points=points)
            else:
                plugins.fill.on_market(fill_market, valuation)
                gate.poll_events(now, points=visible)
            if ctx is not None and (not paper or signal_close > signal_through):
                signal_through = signal_close
                rejected = []
                decision = decide()
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

        def consume(market, signal_close, ctx, decide):
            nonlocal now
            now = market.as_of
            with ledger.transaction(account) if paper else nullcontext():
                if isinstance(market, PaperPulse):
                    self._handle_paper_pulse(market, gate, execution_through)
                elif not (paper and market.bar is not None and market.timeframe == "1m" and signal_close <= execution_through):
                    process_market(market, now, signal_close, ctx, decide)
                checkpoint()
            if isinstance(market, PaperPulse) and market.ready and intake is not None:
                draft = inbox.claim(account, market.instrument_id, now)
                if draft is not None:
                    with ledger.transaction(account):
                        intake.stage(draft, now)
                        checkpoint()
                    with ledger.transaction(account):
                        receipt = intake.receive(draft, now)
                        checkpoint()
                    if receipt is not None:
                        inbox.acknowledge(account, receipt)

        def finish():
            if paper and inbox and now:
                with ledger.transaction(account):
                    self._reduce_paper(gate, now, "PAPER_STOPPING")
                    checkpoint()
            if execution_replay and not tick_replay:
                end = data.end_event()
                self._clock.advance(end.as_of)
                for points in plugins.fill.market_steps(end, valuation, balances=ledger.balances):
                    gate.poll_events(end.as_of, points=points)

        return _Pipeline(consume, finish)

    def _reduce_paper(self, gate: ExecutionGate, now: int, reason: str) -> None:
        for account in self.scheme.sub_accounts:
            if gate.account_state(account).trading_state == TradingState.ACTIVE:
                gate.set_trading_state(account, TradingState.REDUCING, reason)
                gate.record_host_event(ExecutionEvent(
                    kind="TRADING_STATE_CHANGED", sub_account_id=account, ts_event=now,
                    reason=f"{reason}:REDUCING",
                ))
        gate.poll_events(now)

    def _handle_paper_pulse(
        self, market: PaperPulse, gate: ExecutionGate, execution_through: int,
    ) -> None:
        now = market.as_of
        fill = self._plugins.fill
        if market.reduce_reason:
            self._reduce_paper(gate, now, market.reduce_reason)
        if isinstance(fill, BarSim1m):
            for points in fill.market_steps(market, None, execution_through=execution_through):
                gate.poll_events(now, points=points)
        else:
            gate.poll_events(now)

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
        if bar is None and market.source is None and market.ctx is None and (
            market.derivative_points or isinstance(market, TickMarketEvent)
        ):
            return now, None
        if not isinstance(bar, RangeBar):
            raise AccountProcessError("MarketEvent 须携带 RangeBar 或独立衍生品观测")
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
        signal_as_of = now if source.signal_as_of is None else source.signal_as_of
        if not max(close, bar.ts_available) <= signal_as_of <= now:
            raise AccountProcessError("信号求值截止时刻须位于确认与下单时钟之间")
        read_revisions = source.read_derivative_revisions
        ctx = Ctx(
            market.instrument_id, market.timeframe, source.history_start, signal_as_of,
            _read_bars=source.read_bars, _read_derivatives=source.read_derivatives,
            _bar_ts=bar.ts_event, _discovery_tail_completed=source.tail_completed,
            _indicator_defaults=self._indicator_defaults, _data_dir=self._data_dir,
            _read_derivative_revisions=(
                (lambda kind: read_revisions(market.instrument_id, kind, signal_as_of))
                if read_revisions is not None else None
            ),
        )
        if isinstance(self._plugins.strategy, IsolatedStrategy):
            return close, self._plugins.strategy.verify_signal_bar(ctx, bar)
        if ctx.bars()[-1:] != (bar,):
            raise AccountProcessError("策略求值的行情与已确认柱不一致")
        return close, ctx


type _FilePlugins = dict[Slot, tuple[FilePluginCode, str, StrategySnapshot]]


def _prepare_files(config: RunConfig, mode: Mode, *, replay: bool = False) -> _FilePlugins:
    records = StrategyCodeRegistry(config.data_dir)
    snapshots = {}
    files = {}
    for slot in (Slot.STRATEGY, Slot.DATA, Slot.SIZING, Slot.RISK, Slot.FILL):
        reference = config.scheme.slots.selection(slot, mode).reference
        if ":" not in reference:
            continue
        if mode == "paper" and slot != Slot.STRATEGY:
            raise SchemeError(f"{slot}：模拟盘尚未支持从交易方案加载该插槽的 Python 插件")
        path, symbol = python_reference(reference, config.scheme_dir)
        expected = (config.file_plugins or {}).get(slot)
        if replay and expected is None:
            raise SchemeError(f"{slot} 插槽 {path}：运行配置缺少策略代码哈希，无法复现回放")
        if path not in snapshots:
            snapshots[path] = records.source_for_run(path, mode)
        origin, snapshot = snapshots[path]
        code = FilePluginCode(path=path, source=origin, sha256=snapshot.digest)
        if expected is not None and code != expected:
            raise SchemeError(f"{slot} 插槽 {path}：策略代码哈希、来源或路径与运行配置不符")
        if config.deterministic_only and origin == "agent":
            raise SchemeError(
                f"{slot} 插槽 {path} 按 agent 来源处理。"
                "认知成本和评估证据等级尚未纳入确定性回放。"
            )
        if slot != Slot.STRATEGY and origin != "human":
            raise SchemeError(f"{slot} 插槽不能加载隔离策略，宿主文件插件须由交易员登记为 human：{path}")
        files[slot] = code, symbol, snapshot
    return files


@contextmanager
def _open_process(
    config: RunConfig, mode: Mode, registry: PluginRegistry | None, *,
    files: _FilePlugins | None = None, run: RunResult | None = None,
) -> Iterator[AccountProcess]:
    scheme = config.scheme
    clock = _EventClock()
    context = PluginContext(
        run.state_dir if run is not None else config.data_dir, mode, scheme.seed, clock.now, scheme.account_risk,
        run_id=run.run_id if run is not None else None,
        cold_reader=ColdReader(config.data_dir, snapshot=config.cold_snapshot) if run is not None else None,
    )
    ownership = hold_sub_accounts(config.data_dir, scheme.sub_accounts) if mode == "paper" else nullcontext()
    if files is None:
        files = _prepare_files(config, mode)
    with ownership as held, ExitStack() as lifetime:
        definitions = {}
        modules = {}
        for slot, (code, symbol, snapshot) in files.items():
            path = code.path
            try:
                if code.source == "agent":
                    entry = lifetime.enter_context(snapshot.materialize())
                    definitions[slot] = isolated_strategy_plugin(
                        entry, path.parent, symbol, scheme.interface_version, lifetime, data_dir=config.data_dir,
                    )
                else:
                    if path not in modules:
                        entry = lifetime.enter_context(snapshot.materialize())
                        module = load_python_module(entry, entry.read_bytes())
                        modules[path] = module
                        lifetime.callback(sys.modules.pop, module.__name__, None)
                    definitions[slot] = check_plugin(getattr(modules[path], symbol), slot, scheme.interface_version)
            except Exception as exc:
                raise SchemeError(f"{slot} 插槽插件 {path}:{symbol}：{exc}") from exc
        plugins = _assemble(scheme, config.scheme_dir, registry or default_registry(), context, definitions)
        yield AccountProcess(scheme, plugins, clock, config.indicator_defaults, held, config.data_dir)


def resolve_run_config(
    scheme_path: Path, data_dir: Path, *, deterministic_only: bool = False,
    indicator_defaults: dict[IndicatorName, IndicatorParams] | None = None,
) -> RunConfig:
    """固定交易方案和生效的指标默认值，供账户进程加载。"""
    scheme = load_scheme(scheme_path)
    if scheme.slots.selection(Slot.SIZING).reference == "r_unit" or (
        scheme.slots.selection(Slot.RISK).reference == "seven_step"
    ):
        scheme = scheme.model_copy(update={"account_risk": {
            account: scheme.account_risk.get(account) or AccountRiskConfig() for account in scheme.sub_accounts
        }})
    data_dir = data_dir.expanduser().resolve()
    config = RunConfig(
        version=1, scheme=scheme, scheme_dir=scheme_path.resolve().parent, data_dir=data_dir,
        indicator_defaults=sdk.indicator_defaults(data_dir=data_dir) if indicator_defaults is None else indicator_defaults,
        deterministic_only=deterministic_only,
    )
    if deterministic_only:
        try:
            files = _prepare_files(config, "backtest")
            if Slot.STRATEGY not in files:
                default_registry().resolve(
                    Slot.STRATEGY, scheme.slots.selection(Slot.STRATEGY).reference,
                    scheme.interface_version, config.scheme_dir,
                )
        except SchemeError as exc:
            raise SchemeError(f"{scheme_path}：仅支持确定性策略。{exc}") from exc
        config = config.model_copy(update={"file_plugins": {slot: code for slot, (code, _, _) in files.items()}})
    return config


@contextmanager
def open_account_process(
    scheme_path: Path, data_dir: Path, *, mode: Mode, registry: PluginRegistry | None = None,
) -> Iterator[AccountProcess]:
    """装配账户进程，模拟盘在整个运行期间独占方案声明的子账户。"""
    if mode not in ("backtest", "paper"):
        raise SchemeError(f"不支持的账户进程模式：{mode}")
    config = resolve_run_config(scheme_path, data_dir)
    if mode == "paper":
        with _open_process(config, mode, registry) as process:
            yield process
        return
    config, files = _prepare_new_backtest(config)
    run = RunResult.new(config.data_dir)
    run.prepare(config)
    with run.track(), _open_process(config, mode, registry, files=files, run=run) as process:
        yield process


def _prepare_new_backtest(config: RunConfig) -> tuple[RunConfig, _FilePlugins]:
    selection = config.scheme.slots.selection(Slot.DATA)
    if selection.reference == "parquet_replay":
        try:
            params = ReplayParams.model_validate(selection.params)
        except ValueError as exc:
            raise SchemeError(f"data 插槽插件 parquet_replay：{exc}") from exc
        tick_instruments = (params.instrument,) if config.scheme.slots.selection(Slot.FILL).reference == "l1_trade_sim" else ()
        snapshot = capture_replay_snapshot(config.data_dir, (params,), tick_instruments=tick_instruments)
        config = config.model_copy(update={"cold_snapshot": snapshot})
    files = _prepare_files(config, "backtest")
    return config.model_copy(update={"file_plugins": {slot: code for slot, (code, _, _) in files.items()}}), files


def run_backtest(
    scheme_path: Path, data_dir: Path | None, *, registry: PluginRegistry | None = None,
    valuation: MarginValuation | None = None, replay: bool = False, run_id: str | None = None,
) -> RunResult:
    """回测交易方案或运行配置产物，支持由批次预先创建的运行目录。"""
    if run_id is not None and (not replay or data_dir is not None or not re.fullmatch(r"[0-9a-f]{32}", run_id)):
        raise SchemeError("预分配 run_id 只能用于原目录下的运行配置")
    files = None
    if replay:
        config = RunConfig.model_validate_json(scheme_path.read_text(encoding="utf-8"))
        if data_dir is not None:
            config = config.model_copy(update={"data_dir": data_dir.expanduser().resolve()})
    else:
        config = resolve_run_config(scheme_path, data_dir if data_dir is not None else Path(".talaria"))
        config, files = _prepare_new_backtest(config)
    if run_id is None:
        run = RunResult.new(config.data_dir)
        run.prepare(config)
    else:
        run = RunResult(run_id, config.data_dir / "runs" / run_id)
        if scheme_path.resolve() != run.config_path.resolve() or not run.state_dir.is_dir():
            raise SchemeError(f"{run_id}：运行配置与预分配目录不符")
        # 同一预分配目录只能由一个账户进程执行。
        if run._status_path.exists():
            raise SchemeError(f"{run_id}：回放已结束")
        (run.directory / ".started").touch(exist_ok=False)
    with run.track() as completion:
        if replay:
            if config.scheme.slots.selection(Slot.DATA).reference == "parquet_replay" and config.cold_snapshot is None:
                raise SchemeError("运行配置缺少冷库快照，无法复现回放")
            files = _prepare_files(config, "backtest", replay=True)
        with _open_process(config, "backtest", registry, files=files, run=run) as process:
            with open_ledger(run.directory / "ledger.sqlite", run.run_id, process.scheme.sub_accounts) as ledger:
                completion.signal_bars = process.run(ledger, valuation=valuation)
        export_run(run.directory, config=config, completion=completion)
    return run


def run_paper(scheme_path: Path, data_dir: Path, *, registry: PluginRegistry | None = None) -> None:
    """在同一账户宿主中运行实时行情与草案提交，按子账户追加账本。"""
    config = resolve_run_config(scheme_path, data_dir)
    with _open_process(config, "paper", registry) as process:
        if process._ownership is None:
            raise AccountProcessError("模拟盘需要持有子账户排他锁")
        with (
            open_paper_ledger(config.data_dir, process.scheme.sub_accounts, process._ownership) as ledger,
            httpx.Client(trust_env=False, timeout=3) as client,
        ):
            inbox = PaperInbox(config.data_dir, process.scheme.sub_accounts, client)
            process.run(ledger, inbox=inbox)
