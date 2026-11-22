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

    @model_validator(mode="after")
    def _geometry(self) -> Self:
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
        return sha256(self.model_dump_json().encode()).hexdigest()


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
