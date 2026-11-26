"""模拟盘草案、干预命令、定仓预演与账本只读投影的跨进程契约（L0 零 I/O）。"""

from enum import StrEnum
from hashlib import sha256
from typing import Annotated, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from talaria.foundation.bars import CompactBar
from talaria.foundation.margin import Direction
from talaria.foundation.types import ContractModel, DecimalStr

ACCOUNT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
AccountId = Annotated[str, StringConstraints(pattern=ACCOUNT_ID_PATTERN)]
Price = Annotated[DecimalStr, Field(gt=0)]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Trader = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class DraftStatus(StrEnum):
    """暂存交易草案的生命周期状态。"""

    STAGED = "STAGED"
    MODIFIED = "MODIFIED"
    CONFIRMED = "CONFIRMED"
    DISCARDED = "DISCARDED"
    EXPIRED = "EXPIRED"


class TradingState(StrEnum):
    """子账户或全局系统交易状态。"""

    ACTIVE = "ACTIVE"
    REDUCING = "REDUCING"
    HALTED = "HALTED"


class MarginMode(StrEnum):
    """子账户保证金模式。"""

    ISOLATED = "ISOLATED"
    CROSS = "CROSS"


class PaperProcessState(StrEnum):
    """模拟盘账户进程存活状态。"""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    OFFLINE = "OFFLINE"


class PaperFillKind(StrEnum):
    """模拟盘成交事件类型。"""

    ENTRY_FILLED = "ENTRY_FILLED"
    EXIT_FILLED = "EXIT_FILLED"


class PaperCommandKind(StrEnum):
    """人工干预命令类型。"""

    TIGHTEN_STOP = "TIGHTEN_STOP"
    MOVE_TO_BREAKEVEN = "MOVE_TO_BREAKEVEN"
    WIDEN_STOP = "WIDEN_STOP"
    MODIFY_TAKE_PROFIT = "MODIFY_TAKE_PROFIT"
    CANCEL_ENTRY = "CANCEL_ENTRY"
    CLOSE_ALL = "CLOSE_ALL"
    REDUCE_RATIO = "REDUCE_RATIO"
    SET_TRADING_STATE = "SET_TRADING_STATE"


class TrailingStopSpec(ContractModel):
    model_config = ConfigDict(frozen=True, strict=True)
    activation_pct: Annotated[DecimalStr, Field(strict=False, gt=0, lt=1)]
    distance_pct: Annotated[DecimalStr, Field(strict=False, gt=0, lt=1)]


class ExitBarrierSpec(ContractModel):
    model_config = ConfigDict(frozen=True)
    stop: DecimalStr
    take_profit: tuple[DecimalStr, ...] = Field(min_length=1)
    max_holding_ms: int = Field(gt=0)
    trailing_stop: TrailingStopSpec | None = None


class DraftContent(ContractModel):
    model_config = ConfigDict(frozen=True)

    sub_account_id: AccountId
    instrument_id: str = Field(min_length=1, max_length=128)
    snapshot_id: str = Field(min_length=1, max_length=64)
    consultation_id: str | None = Field(default=None, min_length=1, max_length=64)
    direction: Direction
    entry_low: Price
    entry_high: Price
    invalidation_stop: Price
    take_profit: tuple[Price, ...] = Field(min_length=1, max_length=4)
    valid_until: int | None = Field(default=None, ge=0)

    round_id: str | None = Field(default=None, min_length=1)
    inherited_barriers: ExitBarrierSpec | None = None

    @model_validator(mode="after")
    def _geometry(self) -> Self:
        barriers = self.inherited_barriers
        if (self.round_id is None) != (barriers is None):
            raise ValueError("加仓需要目标回合和沿用的屏障")
        if barriers and (barriers.stop, barriers.take_profit) != (self.invalidation_stop, self.take_profit):
            raise ValueError("加仓止损、止盈须与沿用的屏障一致")
        if self.entry_high < self.entry_low:
            raise ValueError("入场价带上下界倒置")
        if self.direction == "LONG":
            valid = self.invalidation_stop < self.entry_low and min(self.take_profit) > self.entry_high
        else:
            valid = self.invalidation_stop > self.entry_high and max(self.take_profit) < self.entry_low
        if not valid:
            raise ValueError("止损与止盈须位于入场价带两侧")
        return self

    def digest(self) -> str:
        # 已确认的旧版开仓草案沿用原规范化字节。
        excluded = {"round_id", "inherited_barriers"} if self.round_id is None else set()
        return sha256(self.model_dump_json(exclude=excluded).encode()).hexdigest()


class DraftEdit(ContractModel):
    version: int = Field(ge=1)
    content: DraftContent


class DraftConfirmation(ContractModel):
    version: int = Field(ge=1)
    content_hash: ContentHash
    confirmed_by: Trader


class DraftDiscardRequest(ContractModel):
    """丢弃未领取交易草案的请求体。"""

    version: int = Field(ge=1)
    discarded_by: Trader | None = None
    reason: str | None = Field(default=None, max_length=256)


class DraftReceipt(ContractModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    version: int = Field(ge=1)
    accepted: bool
    plan_id: str | None = None
    reason: str | None = None
    snapshot_id: str | None = None
    quantity: Price | None = Field(default=None, exclude_if=lambda value: value is None)


class ConfirmedDraft(DraftConfirmation):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    content: DraftContent
    basis_cutoff_ts: int
    created_ts: int
    confirmed_ts: int

    @model_validator(mode="after")
    def _check_confirmation(self) -> Self:
        if self.content_hash != self.content.digest() or self.confirmed_ts < self.created_ts:
            raise ValueError("确认版本的内容哈希或时间无效")
        return self

    @property
    def deliberation_ms(self) -> int:
        return self.confirmed_ts - self.created_ts


class DraftRecord(ContractModel):
    draft_id: str
    version: int
    content_hash: ContentHash
    content: DraftContent
    status: DraftStatus
    claimed: bool
    receipt: DraftReceipt | None = None


class DraftDeliveryRequest(ContractModel):
    instrument_id: str


class DraftEvidence(ContractModel):
    snapshot_id: str
    instrument_id: str
    cutoff_ts: int
    bars: list[CompactBar]
    current_bar: CompactBar | None = None


class PaperRound(ContractModel):
    round_id: str
    sub_account_id: AccountId
    instrument_id: str
    direction: Direction
    quantity: DecimalStr
    stop: DecimalStr
    take_profit: tuple[DecimalStr, ...] = ()
    phase: str | None = None
    barriers: ExitBarrierSpec | None = None
    entry_low: DecimalStr | None = None
    entry_high: DecimalStr | None = None
    entry_price: DecimalStr | None = None
    mark_price: DecimalStr | None = None
    unrealized_pnl: DecimalStr | None = None
    opened_ts: int | None = Field(default=None, ge=0)
    closed_ts: int | None = Field(default=None, ge=0)


class PaperFill(ContractModel):
    event_id: str
    round_id: str
    instrument_id: str
    kind: PaperFillKind
    ts_event: int
    quantity: Price
    price: Price
    fee: DecimalStr | None = None
    realized_pnl: DecimalStr | None = None
    reason: str | None = None


class PaperAccountState(ContractModel):
    """子账户资金、风控状态与定仓配置快照。"""

    currency: str = Field(default="USDT", min_length=1)
    equity: DecimalStr
    cash_available: DecimalStr
    margin_used: DecimalStr
    trading_state: TradingState
    trading_state_reason: str | None = None
    intraday_drawdown_pct: DecimalStr
    peak_drawdown_pct: DecimalStr
    open_r: DecimalStr
    risk_reserved: DecimalStr
    margin_mode: MarginMode | None = None
    leverage: DecimalStr | None = None
    r_unit_pct: DecimalStr | None = None
    max_single_r_pct: DecimalStr | None = None
    friction_buffer_pct: DecimalStr | None = None
    liquidation_buffer_pct: DecimalStr | None = None
    liquidation_buffer_ticks: int | None = Field(default=None, ge=0)


class PaperFeedStatus(ContractModel):
    """模拟盘账户进程的行情订阅与服务连通降级状态。"""

    healthy: bool = True
    reduce_reason: str | None = None
    service_warning: str | None = None
    updated_ts: int | None = Field(default=None, ge=0)


class PaperProcessInfo(ContractModel):
    """模拟盘账户进程元数据与心跳信号。"""

    pid: int = Field(ge=1)
    scheme: str = Field(min_length=1)
    started_ts: int = Field(ge=0)
    heartbeat_ts: int = Field(ge=0)
    status: PaperProcessState = PaperProcessState.RUNNING
    stopped_ts: int | None = Field(default=None, ge=0)


class PaperAccountView(ContractModel):
    sub_account_id: AccountId
    positions: list[PaperRound]
    rounds: list[PaperRound]
    fills: list[PaperFill]
    ledger_seq: int = Field(default=0, ge=0)
    account_state: PaperAccountState | None = None
    feed_status: PaperFeedStatus | None = None
    process: PaperProcessInfo | None = None


class PaperCommandPayload(ContractModel):
    """人工干预命令的参数载荷，全部价格与比例均为十进制字符串。"""

    model_config = ConfigDict(frozen=True)

    stop: Price | None = None
    take_profit: tuple[Price, ...] | None = Field(default=None, min_length=1, max_length=4)
    ratio: Annotated[DecimalStr, Field(gt=0, le=1)] | None = None
    trading_state: TradingState | None = None
    reason: str | None = Field(default=None, max_length=256)


class PaperCommand(ContractModel):
    """常驻服务出站至模拟盘账户进程的人工干预命令。"""

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1, max_length=64)
    sub_account_id: AccountId
    round_id: str | None = Field(default=None, min_length=1, max_length=64)
    kind: PaperCommandKind
    payload: PaperCommandPayload = Field(default_factory=PaperCommandPayload)
    based_on_ledger_seq: int = Field(ge=0)
    issued_by: Trader
    issued_ts: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_kind_payload(self) -> Self:
        if self.kind is PaperCommandKind.SET_TRADING_STATE:
            if self.round_id is not None:
                raise ValueError("SET_TRADING_STATE 不应绑定 round_id")
            if self.payload.trading_state is None:
                raise ValueError("SET_TRADING_STATE 必须提供 payload.trading_state")
            return self

        if self.round_id is None:
            raise ValueError(f"{self.kind.value} 必须提供 round_id")
        if self.kind in (PaperCommandKind.TIGHTEN_STOP, PaperCommandKind.WIDEN_STOP):
            if self.payload.stop is None:
                raise ValueError(f"{self.kind.value} 必须提供 payload.stop")
        elif self.kind is PaperCommandKind.MODIFY_TAKE_PROFIT:
            if self.payload.take_profit is None:
                raise ValueError("MODIFY_TAKE_PROFIT 必须提供 payload.take_profit")
        elif self.kind is PaperCommandKind.REDUCE_RATIO:
            if self.payload.ratio is None:
                raise ValueError("REDUCE_RATIO 必须提供 payload.ratio")
        return self


class PaperCommandReceipt(ContractModel):
    """模拟盘账户进程处理人工干预命令后的回执。"""

    model_config = ConfigDict(frozen=True)

    command_id: str = Field(min_length=1, max_length=64)
    sub_account_id: AccountId | None = None
    accepted: bool
    reason: str | None = None
    ledger_seq: int | None = Field(default=None, ge=0)
    processed_ts: int | None = Field(default=None, ge=0)


class PaperCommandRecord(ContractModel):
    """常驻服务保存的干预命令及领取/回执状态。"""

    command: PaperCommand
    claimed: bool = False
    receipt: PaperCommandReceipt | None = None


class SizingPreviewRequest(ContractModel):
    """工作台向常驻服务发起的无副作用定仓预演请求。"""

    model_config = ConfigDict(frozen=True)

    sub_account_id: AccountId
    instrument_id: str = Field(min_length=1, max_length=128)
    direction: Direction
    entry_low: Price
    entry_high: Price
    invalidation_stop: Price
    take_profit: tuple[Price, ...] = Field(default=(), max_length=4)
    round_id: str | None = Field(default=None, min_length=1, max_length=64)
    override_quantity: Price | None = None


class SizingPreviewResponse(ContractModel):
    """定仓预演结果，仅用于前端展示，权威数量仍由账户进程计算。"""

    model_config = ConfigDict(frozen=True)

    quantity: DecimalStr | None = None
    basis: str | None = None
    risk_amount_r: DecimalStr | None = None
    risk_pct_of_equity: DecimalStr | None = None
    est_liq_price: DecimalStr | None = None
    est_liq_distance: DecimalStr | None = None
    est_liq_distance_pct: DecimalStr | None = None
    rejected_reason: str | None = None
    from_offline_snapshot: bool = False
    based_on_ledger_seq: int | None = Field(default=None, ge=0)
