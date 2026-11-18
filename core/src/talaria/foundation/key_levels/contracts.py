"""关键位（Key Level）契约：类别、状态、构成明细与忽略反馈。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from talaria.foundation.market import SessionTrait, TradingSession
from talaria.foundation.types import ContractModel, DecimalStr

#: 算法版本；参数随每个关键位一起返回，便于复现与质量评估。
KEY_LEVEL_METHOD_VERSION = "key-levels@1"


class KeyLevelKind(StrEnum):
    """关键位类型；11 的成交量位/清算估算可直接追加而不改类别逻辑。"""

    SWING_HIGH = "SWING_HIGH"
    SWING_LOW = "SWING_LOW"
    PREV_DAY_HIGH = "PREV_DAY_HIGH"
    PREV_DAY_LOW = "PREV_DAY_LOW"
    PREV_DAY_CLOSE = "PREV_DAY_CLOSE"
    WEEK_OPEN = "WEEK_OPEN"
    MONTH_OPEN = "MONTH_OPEN"
    OPENING_RANGE = "OPENING_RANGE"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"
    GAP = "GAP"
    ROUND_NUMBER = "ROUND_NUMBER"


class KeyLevelCategory(StrEnum):
    """面向 UI 过滤的稳定大类。"""

    STRUCTURE = "STRUCTURE"
    SESSION = "SESSION"
    GAP = "GAP"
    ROUND_NUMBER = "ROUND_NUMBER"


class KeyLevelStatus(StrEnum):
    ACTIVE = "ACTIVE"
    BROKEN = "BROKEN"
    FLIPPED = "FLIPPED"


KIND_CATEGORY: dict[KeyLevelKind, KeyLevelCategory] = {
    KeyLevelKind.SWING_HIGH: KeyLevelCategory.STRUCTURE,
    KeyLevelKind.SWING_LOW: KeyLevelCategory.STRUCTURE,
    KeyLevelKind.PREV_DAY_HIGH: KeyLevelCategory.SESSION,
    KeyLevelKind.PREV_DAY_LOW: KeyLevelCategory.SESSION,
    KeyLevelKind.PREV_DAY_CLOSE: KeyLevelCategory.SESSION,
    KeyLevelKind.WEEK_OPEN: KeyLevelCategory.SESSION,
    KeyLevelKind.MONTH_OPEN: KeyLevelCategory.SESSION,
    KeyLevelKind.OPENING_RANGE: KeyLevelCategory.SESSION,
    KeyLevelKind.SESSION_HIGH: KeyLevelCategory.SESSION,
    KeyLevelKind.SESSION_LOW: KeyLevelCategory.SESSION,
    KeyLevelKind.GAP: KeyLevelCategory.GAP,
    KeyLevelKind.ROUND_NUMBER: KeyLevelCategory.ROUND_NUMBER,
}


class KeyLevel(ContractModel):
    """一个已确认、无前视的关键位；单价或区间二选一。"""

    level_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    kind: KeyLevelKind
    category: KeyLevelCategory
    timeframe: str
    price: DecimalStr | None = None
    price_low: DecimalStr | None = None
    price_high: DecimalStr | None = None
    strength: float = Field(ge=0, le=1)
    status: KeyLevelStatus
    ts_available: int = Field(ge=0)
    formed_ts: int = Field(ge=0)
    method_version: str = KEY_LEVEL_METHOD_VERSION
    parameters: dict[str, str] = Field(default_factory=dict)
    touch_count: int = Field(ge=0)
    last_touch_ts: int | None = Field(default=None, ge=0)
    volume_weight: DecimalStr

    @model_validator(mode="after")
    def _check_price_shape(self) -> Self:
        has_single = self.price is not None
        has_zone = self.price_low is not None or self.price_high is not None
        if has_single and has_zone:
            raise ValueError("price 与 price_low/price_high 只能二选一")
        if not has_single and not has_zone:
            raise ValueError("必须提供 price 或 price_low/price_high")
        if has_zone:
            if self.price_low is None or self.price_high is None:
                raise ValueError("区间关键位必须同时提供 price_low 与 price_high")
            if self.price_low > self.price_high:
                raise ValueError("price_low 不能大于 price_high")
        return self


class KeyLevelSet(ContractModel):
    """某个冻结快照与周期上的一份不可变关键位集合。"""

    snapshot_id: str
    instrument_id: str
    timeframe: str
    cutoff_ts: int = Field(ge=0)
    as_of_ts: int = Field(ge=0)
    method_version: str = KEY_LEVEL_METHOD_VERSION
    parameters: dict[str, str] = Field(default_factory=dict)
    levels: list[KeyLevel] = Field(default_factory=list)


class KeyLevelIgnoreRequest(ContractModel):
    """忽略/恢复单条系统关键位；只写协同状态库，不改冻结快照。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    level_id: str = Field(min_length=1, max_length=64)
    ignored: bool = True


class KeyLevelIgnoreRecord(ContractModel):
    instrument_id: str
    level_id: str
    ignored: bool
    updated_ts: int


class KeyLevelIgnoreList(ContractModel):
    instrument_id: str
    entries: list[KeyLevelIgnoreRecord] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """从市场包 Trait 投影出的时段配置；核心引擎不内置任何时区或交易所。"""

    timezone: str = "UTC"
    day_start: str = "00:00"
    sessions: tuple[TradingSession, ...] = ()


def session_profile_from_trait(trait: SessionTrait) -> SessionProfile:
    return SessionProfile(
        timezone=trait.timezone or "UTC",
        day_start=trait.day_start,
        sessions=trait.sessions,
    )


@dataclass(frozen=True, slots=True)
class KeyLevelParams:
    """纯函数参数；全部写入每个关键位的 ``parameters``，保证可复现。"""

    swing_span: int = 2
    confirm_bars: int = 2
    atr_period: int = 14
    pivot_atr_min: float = 0.6
    cluster_atr: float = 0.4
    touch_atr: float = 0.2
    gap_atr_min: float = 0.35
    opening_range_minutes: int = 30
    round_step: Decimal | None = None
    round_levels_per_side: int = 3
    round_window_atr: float = 25.0
    max_levels: int = 60

    def __post_init__(self) -> None:
        if self.swing_span < 1 or self.confirm_bars < 1:
            raise ValueError("swing_span 与 confirm_bars 必须 >= 1")
        if self.atr_period < 2:
            raise ValueError("atr_period 必须 >= 2")
        if self.opening_range_minutes < 1:
            raise ValueError("opening_range_minutes 必须 >= 1")
        if self.round_levels_per_side < 0 or self.max_levels < 1:
            raise ValueError("round_levels_per_side 与 max_levels 非法")

    def as_parameters(self) -> dict[str, str]:
        return {
            "swing_span": str(self.swing_span),
            "confirm_bars": str(self.confirm_bars),
            "atr_period": str(self.atr_period),
            "pivot_atr_min": f"{self.pivot_atr_min:g}",
            "cluster_atr": f"{self.cluster_atr:g}",
            "touch_atr": f"{self.touch_atr:g}",
            "gap_atr_min": f"{self.gap_atr_min:g}",
            "opening_range_minutes": str(self.opening_range_minutes),
            "round_step": "auto" if self.round_step is None else str(self.round_step),
            "round_levels_per_side": str(self.round_levels_per_side),
            "round_window_atr": f"{self.round_window_atr:g}",
            "max_levels": str(self.max_levels),
        }
