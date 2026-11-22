"""账户进程的版本化插槽接口。时点均为 UTC 毫秒。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol, runtime_checkable

from pydantic import ConfigDict, Field, JsonValue

from talaria.components import Ctx
from talaria.components.runtime import BarReader, DerivativeReader
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.margin import Direction as Direction
from talaria.foundation.margin import MarginEstimate as MarginEstimate
from talaria.foundation.margin import MarginPosition
from talaria.foundation.margin import MarginValuation as MarginValuation
from talaria.foundation.paper import DraftReceipt, ExitBarrierSpec
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
    draft_plan_id: str | None = None
    inherited_barriers: ExitBarrierSpec | None = None
    signal_bar_close: int | None = Field(default=None, ge=0)
    order_type: Literal["LIMIT", "MARKET", "POST_ONLY"] = "LIMIT"
    snapshot_id: str | None = None
    deliberation_ms: int | None = Field(default=None, ge=0)


class SizingDecision(_Value):
    quantity: DecimalStr = Field(gt=0)
    basis: str = Field(min_length=1)


class SizingPreview(_Value):
    sizing: SizingDecision
    liquidation_price: DecimalStr | None = None
    liquidation_distance: DecimalStr | None = None


class SizingRejected(ValueError):
    """有效意图无法定出可下单的数量。"""


class ApprovedExecutionPlan(_Value):
    plan_id: str = Field(min_length=1)
    round_id: str = Field(min_length=1)
    candidate: EntryCandidate
    sizing: SizingDecision
    allowed_slippage: DecimalStr | None = Field(default=None, ge=0)  # 价格单位。
    barriers: ExitBarrierSpec | None = None


class RejectedPlanRecord(_Value):
    reason: str = Field(min_length=1)


class BarrierChangeRequest(_Value):
    sub_account_id: str
    round_id: str
    stop: DecimalStr


class ApprovedBarrierAmendment(_Value):
    amendment_id: str = Field(min_length=1)
    request: BarrierChangeRequest


class RoundPhase(StrEnum):
    PENDING_ENTRY = "PENDING_ENTRY"
    PARTIALLY_FILLED_PROTECTED = "PARTIALLY_FILLED_PROTECTED"
    ACTIVE_PROTECTED = "ACTIVE_PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    LIQUIDATED = "LIQUIDATED"


class RoundStatus(_Value):
    round_id: str
    sub_account_id: str
    instrument_id: str
    direction: Direction
    quantity: DecimalStr = Field(ge=0)
    stop: DecimalStr
    phase: RoundPhase | None = None


@dataclass(frozen=True, slots=True)
class RoundExposure:
    round_id: str
    position: MarginPosition
    stop: Decimal
    max_entry_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AccountState:
    sub_account_id: str
    balances: Mapping[tuple[str, str], Decimal]  # (currency, ledger account)
    rounds: Mapping[str, RoundStatus]
    trading_state: TradingState
    as_of: int = 0
    positions: tuple[RoundExposure, ...] = ()
    reservations: tuple[RoundExposure, ...] = ()
    barriers: Mapping[str, ExitBarrierSpec] = field(default_factory=dict)
    marks: Mapping[str, DerivativePoint] = field(default_factory=dict)
    funding_rates: Mapping[str, DerivativePoint] = field(default_factory=dict)


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
    settlement_ts: int | None = Field(default=None, ge=0)
    take_profit: tuple[DecimalStr, ...] = ()
    draft_id: str | None = None
    draft_version: int | None = None
    barriers: ExitBarrierSpec | None = None
    draft_receipt: DraftReceipt | None = None
    deliberation_ms: int | None = Field(default=None, ge=0)


@dataclass(frozen=True, slots=True)
class MarketData:
    """策略求值的原始数据源。宿主按时钟和信号柱构造 Ctx。"""

    history_start: int
    read_bars: BarReader
    read_derivatives: DerivativeReader | None = None
    tail_completed: bool = True
    signal_as_of: int | None = None

    @classmethod
    def from_ctx(cls, ctx: Ctx) -> MarketData:
        # 兼容已有入口，仅读取数据源，不使用入口的求值逻辑或缓存。
        return cls(ctx.start, ctx._read_bars, ctx._read_derivatives, ctx._discovery_tail_completed)


@dataclass(frozen=True, slots=True)
class MarketEvent:
    """可获取时间驱动时钟。触发求值的确认柱携带原始数据源，衍生品可独立推进时钟。"""

    as_of: int
    instrument_id: str
    timeframe: str
    bar: RangeBar | None
    ctx: Ctx | None = None  # 兼容已注册的数据入口，新插件使用 source。
    source: MarketData | None = None
    derivative_points: tuple[DerivativePoint, ...] = ()


@runtime_checkable
class DataEntry(Protocol):
    def events(self) -> Iterator[MarketEvent]: ...


class PlanReceipt(_Value):
    accepted: bool
    plan_id: str | None = None
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
