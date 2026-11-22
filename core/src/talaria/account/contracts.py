"""账户进程的版本化插槽接口。时点均为 UTC 毫秒。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, JsonValue

from talaria.components import Ctx
from talaria.components.runtime import BarReader, DerivativeReader
from talaria.foundation.bars import RangeBar
from talaria.foundation.types import ContractModel, DecimalStr


class _Value(ContractModel):
    model_config = ConfigDict(frozen=True)


class DecisionOrigin(StrEnum):
    HUMAN = "HUMAN"
    CO_PILOT = "CO_PILOT"
    AGENT = "AGENT"


class TradingState(StrEnum):
    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    HALTED = "HALTED"


type Direction = Literal["LONG", "SHORT"]
type Intervention = Literal["CANCEL_ENTRY", "CLOSE_ALL", "REDUCE_RATIO"]


class EntryCandidate(_Value):
    sub_account_id: str
    instrument_id: str
    direction: Direction
    entry_low: DecimalStr
    entry_high: DecimalStr
    invalidation_stop: DecimalStr
    take_profit: tuple[DecimalStr, ...] = Field(min_length=1)
    ts_decision: int = Field(ge=0)
    origin: DecisionOrigin
    valid_until: int | None = None
    evidence_ids: tuple[str, ...] = ()
    round_id: str | None = None  # 有回合 ID 时为加仓。
    signal_bar_close: int | None = Field(default=None, ge=0)


class SizingDecision(_Value):
    quantity: DecimalStr = Field(gt=0)
    basis: str = Field(min_length=1)


class ApprovedExecutionPlan(_Value):
    plan_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    candidate: EntryCandidate
    sizing: SizingDecision
    allowed_slippage: DecimalStr | None = Field(default=None, ge=0)  # 价格单位。


class RejectedPlanRecord(_Value):
    reason: str = Field(min_length=1)


class BarrierChangeRequest(_Value):
    sub_account_id: str
    round_id: str
    stop: DecimalStr


class ApprovedBarrierAmendment(_Value):
    amendment_id: str = Field(min_length=1)
    request: BarrierChangeRequest


class RoundStatus(_Value):
    round_id: str
    sub_account_id: str
    instrument_id: str
    direction: Direction
    quantity: DecimalStr = Field(ge=0)
    stop: DecimalStr


@dataclass(frozen=True, slots=True)
class AccountState:
    sub_account_id: str
    balances: Mapping[tuple[str, str], Decimal]  # (currency, ledger account)
    rounds: Mapping[str, RoundStatus]
    trading_state: TradingState


class MarginEstimate(_Value):
    equity: DecimalStr
    initial_margin: DecimalStr
    maintenance_margin: DecimalStr


class MarginValuation(Protocol):
    """市场规则包提供的只读估值，使用截止时刻内的标记价格。"""

    def isolated_liquidation_price(
        self, instrument_id: str, direction: Direction, quantity: Decimal,
        entry_price: Decimal, isolated_margin: Decimal, *, as_of: int,
    ) -> Decimal: ...

    def cross_margin(
        self, account_state: AccountState, mark_prices: Mapping[str, Decimal], *, as_of: int,
    ) -> MarginEstimate: ...


@runtime_checkable
class SizingSlot(Protocol):
    def size(
        self, candidate: EntryCandidate, account_state: AccountState,
        valuation: MarginValuation | None,
    ) -> SizingDecision: ...


@runtime_checkable
class RiskSlot(Protocol):
    def evaluate_candidate(
        self, candidate: EntryCandidate, sizing: SizingDecision, account_state: AccountState,
        valuation: MarginValuation | None,
    ) -> ApprovedExecutionPlan | RejectedPlanRecord: ...

    def approve_barrier_widening(
        self, request: BarrierChangeRequest, account_state: AccountState,
        valuation: MarginValuation | None,
    ) -> ApprovedBarrierAmendment | RejectedPlanRecord: ...


class LedgerPosting(_Value):
    account: str = Field(min_length=1)
    currency: str = Field(min_length=1)
    amount: DecimalStr  # 借方为正，贷方为负。


class ExecutionEvent(_Value):
    """成交以 ENTRY_FILLED 或 EXIT_FILLED 回报数量、单价、回合及复式分录。

    TRADE_PAYABLE 为成交对价（买为负，卖为正），入场的 POSITION_COST 借贷方向相反。
    出场冲减 POSITION_COST，完全平仓时结清该回合成本。
    PLAN_CANCELLED / PLAN_TERMINATED 等终态回报用 plan_id 标识失效的计划。
    """

    kind: str = Field(min_length=1)
    sub_account_id: str
    ts_event: int = Field(ge=0, le=2**63 - 1)
    event_id: str | None = None  # 由账本分配，与运行 ID 和 SQLite 行号无关。
    plan_id: str | None = None
    amendment_id: str | None = None
    quantity: DecimalStr | None = Field(default=None, gt=0)
    price: DecimalStr | None = Field(default=None, gt=0)
    position: RoundStatus | None = None
    reason: str | None = None
    postings: tuple[LedgerPosting, ...] = ()


@dataclass(frozen=True, slots=True)
class MarketData:
    """策略求值的原始数据源。宿主按时钟和信号柱构造 Ctx。"""

    history_start: int
    read_bars: BarReader
    read_derivatives: DerivativeReader | None = None
    tail_completed: bool = True

    @classmethod
    def from_ctx(cls, ctx: Ctx) -> MarketData:
        # 兼容已有入口，仅读取数据源，不使用入口的求值逻辑或缓存。
        return cls(ctx.start, ctx._read_bars, ctx._read_derivatives, ctx._discovery_tail_completed)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """可获取时间驱动时钟。触发求值的确认柱携带原始数据源。"""

    as_of: int
    instrument_id: str
    timeframe: str
    bar: RangeBar
    ctx: Ctx | None = None  # 兼容已注册的数据入口，新插件使用 source。
    source: MarketData | None = None


@runtime_checkable
class DataEntry(Protocol):
    def events(self) -> Iterator[MarketEvent]: ...


class PlanReceipt(_Value):
    accepted: bool
    reason: str | None = None


class BarrierReceipt(_Value):
    accepted: bool
    reason: str | None = None


@runtime_checkable
class FillSlot(Protocol):
    def on_market(self, event: MarketEvent, valuation: MarginValuation | None) -> None: ...
    def submit_plan(self, plan: ApprovedExecutionPlan) -> PlanReceipt: ...
    def tighten_barrier(self, req: BarrierChangeRequest) -> BarrierReceipt: ...
    def apply_approved_amendment(self, amendment: ApprovedBarrierAmendment) -> BarrierReceipt: ...
    def intervene_position(
        self, round_id: str, action: Intervention, ratio: Decimal | None, reason: str,
    ) -> RoundStatus: ...
    def set_trading_state(
        self, sub_account_id: str | None, state: TradingState, reason: str,
    ) -> None: ...
    def poll_events(self, up_to_ts: int) -> list[ExecutionEvent]: ...


class ToolSlot(Protocol):
    def invoke(self, name: str, arguments: Mapping[str, JsonValue]) -> JsonValue: ...
