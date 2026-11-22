"""模拟盘草案与账本只读投影的跨进程契约。"""

from hashlib import sha256
from typing import Annotated, Literal, Self

from pydantic import ConfigDict, Field, StringConstraints, model_validator

from talaria.foundation.bars import CompactBar
from talaria.foundation.margin import Direction
from talaria.foundation.types import ContractModel, DecimalStr

ACCOUNT_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
AccountId = Annotated[str, StringConstraints(pattern=ACCOUNT_ID_PATTERN)]
Price = Annotated[DecimalStr, Field(gt=0)]
ContentHash = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Trader = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


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


class DraftReceipt(ContractModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    version: int = Field(ge=1)
    accepted: bool
    plan_id: str | None = None
    reason: str | None = None
    snapshot_id: str | None = None


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
    status: Literal["STAGED", "MODIFIED", "CONFIRMED"]
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


class PaperFill(ContractModel):
    event_id: str
    round_id: str
    instrument_id: str
    kind: Literal["ENTRY_FILLED", "EXIT_FILLED"]
    ts_event: int
    quantity: Price
    price: Price


class PaperAccountView(ContractModel):
    sub_account_id: AccountId
    positions: list[PaperRound]
    rounds: list[PaperRound]
    fills: list[PaperFill]
