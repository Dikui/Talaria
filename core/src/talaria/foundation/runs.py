"""回测导出投影与工作台逐笔回放的 L0 零 I/O 契约。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from talaria.foundation.margin import Direction
from talaria.foundation.paper import AccountId
from talaria.foundation.ticks import TickKind
from talaria.foundation.types import ContractModel, DecimalStr


class RunStatus(StrEnum):
    """回放运行终态。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RunFillKind(StrEnum):
    """回合内单笔成交方向事件类型。"""

    ENTRY_FILLED = "ENTRY_FILLED"
    EXIT_FILLED = "EXIT_FILLED"


class RunFillReason(StrEnum):
    """归因标准化后的开平仓原因简称。"""

    ENTRY = "ENTRY"
    SL = "SL"
    TP = "TP"
    TIME = "TIME"
    LIQUIDATION = "LIQUIDATION"
    OTHER = "OTHER"


class RunBarrierChangeKind(StrEnum):
    """保护屏障变更类型。"""

    BARRIER_TIGHTENED = "BARRIER_TIGHTENED"
    BARRIER_AMENDED = "BARRIER_AMENDED"


class RunRoundPhase(StrEnum):
    """仓位闭合回合的生命周期阶段。"""

    PENDING_ENTRY = "PENDING_ENTRY"
    PARTIALLY_FILLED_PROTECTED = "PARTIALLY_FILLED_PROTECTED"
    ACTIVE_PROTECTED = "ACTIVE_PROTECTED"
    EXITING = "EXITING"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    LIQUIDATED = "LIQUIDATED"


class RunRoundFill(ContractModel):
    """回合同级归档中的单笔成交明细。"""

    event_id: str = Field(min_length=1)
    ts_event: int = Field(ge=0, le=2**63 - 1)
    kind: RunFillKind
    side: Literal["buy", "sell"]
    price: DecimalStr
    quantity: DecimalStr
    reason: RunFillReason
    raw_reason: str | None = None
    fill_tier: Literal["l1", "trade", "bar_1m", "mark"] | None = None
    realized_pnl: DecimalStr | None = None
    fee: DecimalStr | None = None


class RunBarrierChange(ContractModel):
    """回合存续期内的止损/止盈屏障变更。"""

    event_id: str = Field(min_length=1)
    ts_event: int = Field(ge=0, le=2**63 - 1)
    kind: RunBarrierChangeKind
    stop: DecimalStr
    take_profit: list[DecimalStr] = Field(default_factory=list)
    reason: str | None = None
    amendment_id: str | None = None


class RunRoundRecord(ContractModel):
    """`rounds.parquet` 与 `GET /api/runs/{run_id}/rounds` 的单个仓位闭合回合记录。"""

    round_id: str = Field(min_length=1)
    sub_account_id: AccountId
    currency: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    direction: Direction
    phase: RunRoundPhase
    opened_ts: int | None = Field(default=None, ge=0, le=2**63 - 1)
    closed_ts: int | None = Field(default=None, ge=0, le=2**63 - 1)
    entry_price: DecimalStr | None = None
    exit_price: DecimalStr | None = None
    max_quantity: DecimalStr
    remaining_quantity: DecimalStr
    stop: DecimalStr
    take_profit: list[DecimalStr] = Field(default_factory=list)
    exit_reason: RunFillReason | None = None
    raw_exit_reason: str | None = None
    net_pnl: DecimalStr
    fees: DecimalStr
    funding: DecimalStr
    liquidation_fees: DecimalStr
    mae: DecimalStr | None = None
    mfe: DecimalStr | None = None
    fills: list[RunRoundFill] = Field(default_factory=list)
    barrier_changes: list[RunBarrierChange] = Field(default_factory=list)


class RunAccountCurrencySummary(ContractModel):
    """按 `(sub_account_id, currency)` 聚合的回放收益与回撤汇总。"""

    sub_account_id: AccountId
    currency: str = Field(min_length=1)
    round_count: int = Field(ge=0)
    trade_count: int = Field(ge=0)
    net_pnl: DecimalStr
    max_drawdown: DecimalStr
    fees: DecimalStr
    funding: DecimalStr
    liquidation_fees: DecimalStr
    initial_capital: DecimalStr | None = None
    final_equity: DecimalStr | None = None


class RunExportArtifacts(ContractModel):
    """`runs/<run_id>/` 下由账户进程原子产出的导出文件相对路径。"""

    rounds_path: str = "rounds.parquet"
    equity_path: str = "equity.parquet"
    overlays_path: str | None = None


class RunExportSummary(ContractModel):
    """`summary.json` 原子就绪标记与回放元数据摘要。"""

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    status: RunStatus
    error: str | None = None
    scheme: str = Field(min_length=1)
    fill_plugin: str = Field(min_length=1)
    instrument_id: str | None = None
    instruments: list[str] = Field(default_factory=list)
    timeframe: str | None = None
    start: int | None = Field(default=None, ge=0, le=2**63 - 1)
    end: int | None = Field(default=None, ge=0, le=2**63 - 1)
    signal_bars: int | None = Field(default=None, ge=0)
    pnl_basis: Literal["booked"] = "booked"
    artifacts: RunExportArtifacts = Field(default_factory=RunExportArtifacts)
    accounts: list[RunAccountCurrencySummary] = Field(default_factory=list)


class RunListResponse(ContractModel):
    """`GET /api/runs` 只读响应（只含存在 `summary.json` 的就绪运行）。"""

    runs: list[RunExportSummary] = Field(default_factory=list)


class RunRoundsResponse(ContractModel):
    """`GET /api/runs/{run_id}/rounds` 只读响应。"""

    run_id: str = Field(min_length=1)
    rounds: list[RunRoundRecord] = Field(default_factory=list)


class RunEquityPoint(ContractModel):
    """`equity.parquet` 与 `GET /api/runs/{run_id}/equity` 的逐分录已实现净值节点。"""

    sub_account_id: AccountId
    currency: str = Field(min_length=1)
    seq: int = Field(ge=1)
    ts_event: int = Field(ge=0, le=2**63 - 1)
    event_id: str = Field(min_length=1)
    event_kind: str = Field(min_length=1)
    round_id: str | None = None
    pnl_basis: Literal["booked"] = "booked"
    realized_pnl_delta: DecimalStr
    fee_delta: DecimalStr
    funding_delta: DecimalStr
    liquidation_fee_delta: DecimalStr
    net_pnl: DecimalStr
    equity: DecimalStr
    peak_pnl: DecimalStr
    drawdown: DecimalStr
    max_drawdown: DecimalStr


class RunEquityResponse(ContractModel):
    """`GET /api/runs/{run_id}/equity` 只读响应。"""

    run_id: str = Field(min_length=1)
    pnl_basis: Literal["booked"] = "booked"
    points: list[RunEquityPoint] = Field(default_factory=list)


class ReplayTick(ContractModel):
    """主线 A 轻量化逐笔回放节点，对应 Vela `ReplayTick` 形状。"""

    price: DecimalStr
    open: DecimalStr | None = None
    high: DecimalStr | None = None
    low: DecimalStr | None = None
    volume: DecimalStr | None = None


class TicksJsonResponse(ContractModel):
    """`GET /api/ticks` 轻量 JSON 逐笔响应。"""

    instrument_id: str = Field(min_length=1)
    kind: TickKind = TickKind.TRADES
    start: int = Field(ge=0, le=2**63 - 1)
    end: int = Field(ge=0, le=2**63 - 1)
    as_of: int | None = Field(default=None, ge=0, le=2**63 - 1)
    aggregated: bool = False
    ticks: list[ReplayTick] = Field(default_factory=list)
