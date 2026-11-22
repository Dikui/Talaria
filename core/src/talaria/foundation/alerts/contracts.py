"""预警规则与触发记录的跨进程契约。

预警规则是用户意图，触发记录是不可变的证据索引：触发时冻结的
``snapshot_id`` 指向当时的行情快照，前端据此回看现场。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from talaria.foundation.key_levels.contracts import KeyLevelKind
from talaria.foundation.operators import CHANNELS, IndicatorName
from talaria.foundation.types import ContractModel, DecimalStr

IndicatorSeed = Literal["pine", "legacy_sma"]


class AlertConditionKind(StrEnum):
    """规则条件的来源；前端不复制任何指标、几何或关键位数学。"""

    METRIC_THRESHOLD = "METRIC_THRESHOLD"
    INDICATOR_SIGNAL = "INDICATOR_SIGNAL"
    OVERLAY_CROSS = "OVERLAY_CROSS"
    LEVEL_EVENT = "LEVEL_EVENT"


class AlertMetric(StrEnum):
    """可直接比较的盘口或衍生品标量。"""

    LAST_PRICE = "last_price"
    BID_PRICE = "bid_price"
    ASK_PRICE = "ask_price"
    MARK_PRICE = "mark_price"
    FUNDING_RATE = "funding_rate"
    OPEN_INTEREST = "open_interest"


class AlertDirection(StrEnum):
    """比较方向：向上穿越/高于，或向下穿越/低于。"""

    ABOVE = "ABOVE"
    BELOW = "BELOW"


class AlertOverlayMode(StrEnum):
    """画线触发的两种判定：盘中触碰或收盘确认突破。"""

    TOUCH = "TOUCH"
    CLOSE_BREAK = "CLOSE_BREAK"


class AlertLevelEvent(StrEnum):
    """关键位事件：盘中触及、收盘突破、突破后的回踩确认。"""

    TOUCH = "TOUCH"
    BREAK = "BREAK"
    RETEST = "RETEST"


class AlertStatus(StrEnum):
    """规则状态机：草案/待命/冷却/过期/禁用。"""

    DRAFT = "DRAFT"
    ARMED = "ARMED"
    COOLDOWN = "COOLDOWN"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"


class AlertCondition(ContractModel):
    """一条可评估条件；字段组合由 ``kind`` 决定。"""

    kind: AlertConditionKind
    direction: AlertDirection | None = None
    metric: AlertMetric | None = None
    threshold: DecimalStr | None = None
    indicator: IndicatorName | None = None
    indicator_period: int | None = Field(default=None, ge=1, le=5000)
    indicator_anchor: Literal["snapshot", "session"] | None = None
    indicator_seed: IndicatorSeed | None = None
    indicator_std_dev: DecimalStr | None = Field(default=None, gt=0)
    indicator_channel: str | None = Field(default=None, max_length=16)
    compare_indicator: IndicatorName | None = None
    compare_period: int | None = Field(default=None, ge=1, le=5000)
    compare_anchor: Literal["snapshot", "session"] | None = None
    compare_seed: IndicatorSeed | None = None
    compare_std_dev: DecimalStr | None = Field(default=None, gt=0)
    drawing_id: str | None = Field(default=None, max_length=64)
    overlay_mode: AlertOverlayMode | None = None
    overlay_ratio: DecimalStr | None = None
    level_id: str | None = Field(default=None, max_length=64)
    level_event: AlertLevelEvent | None = None
    level_kind: KeyLevelKind | None = None

    @model_validator(mode="after")
    def _validate_shape(self) -> AlertCondition:
        if self.kind is AlertConditionKind.METRIC_THRESHOLD:
            if self.metric is None or self.threshold is None:
                raise ValueError("METRIC_THRESHOLD 需要 metric 与 threshold")
            if any(
                value is not None
                for value in (
                    self.indicator,
                    self.indicator_period,
                    self.indicator_anchor,
                    self.indicator_seed,
                    self.indicator_std_dev,
                    self.indicator_channel,
                    self.compare_indicator,
                    self.compare_period,
                    self.compare_anchor,
                    self.compare_seed,
                    self.compare_std_dev,
                    self.drawing_id,
                    self.overlay_mode,
                    self.overlay_ratio,
                    self.level_id,
                    self.level_event,
                    self.level_kind,
                )
            ):
                raise ValueError("METRIC_THRESHOLD 不接受指标、画线或关键位字段")
            return self

        if self.kind is AlertConditionKind.INDICATOR_SIGNAL:
            if self.indicator is None:
                raise ValueError("INDICATOR_SIGNAL 需要 indicator")
            if self.indicator in {
                IndicatorName.FUNDING_QUANTILE,
                IndicatorName.OI_DIVERGENCE,
                IndicatorName.NET_ODDS,
            }:
                raise ValueError("衍生品指标暂不支持预警")
            if self.threshold is None and self.compare_indicator is None:
                raise ValueError("INDICATOR_SIGNAL 需要 threshold 或 compare_indicator")
            if self.threshold is not None and self.compare_indicator is not None:
                raise ValueError("threshold 与 compare_indicator 只能二选一")
            if self.indicator_channel is not None and self.indicator_channel not in CHANNELS[
                self.indicator
            ]:
                raise ValueError(f"{self.indicator} 不支持通道 {self.indicator_channel}")
            if (
                self.indicator_anchor is not None
                and self.indicator is not IndicatorName.VWAP
            ):
                raise ValueError("只有 VWAP 支持 anchor 参数")
            if self.compare_indicator is not None and self.compare_indicator in {
                IndicatorName.FUNDING_QUANTILE,
                IndicatorName.OI_DIVERGENCE,
                IndicatorName.NET_ODDS,
            }:
                raise ValueError("衍生品指标暂不支持预警")
            if (
                self.compare_anchor is not None
                and self.compare_indicator is not IndicatorName.VWAP
            ):
                raise ValueError("只有 VWAP 支持 anchor 参数")
            for name, seed, std_dev in (
                (self.indicator, self.indicator_seed, self.indicator_std_dev),
                (self.compare_indicator, self.compare_seed, self.compare_std_dev),
            ):
                if seed is not None and name not in {IndicatorName.EMA, IndicatorName.ATR}:
                    raise ValueError("只有 EMA 和 ATR 支持 seed 参数")
                if std_dev is not None and name is not IndicatorName.BOLL:
                    raise ValueError("只有 BOLL 支持 std_dev 参数")
            if any(
                value is not None
                for value in (
                    self.metric,
                    self.drawing_id,
                    self.overlay_mode,
                    self.overlay_ratio,
                    self.level_id,
                    self.level_event,
                    self.level_kind,
                )
            ):
                raise ValueError("INDICATOR_SIGNAL 不接受 metric、画线或关键位字段")
            return self

        if self.kind is AlertConditionKind.LEVEL_EVENT:
            if self.level_id is None or self.level_event is None:
                raise ValueError("LEVEL_EVENT 需要 level_id 与 level_event")
            if self.direction is not None:
                raise ValueError("LEVEL_EVENT 的方向由关键位事件决定，无需 direction")
            if any(
                value is not None
                for value in (
                    self.metric,
                    self.threshold,
                    self.indicator,
                    self.indicator_period,
                    self.indicator_anchor,
                    self.indicator_seed,
                    self.indicator_std_dev,
                    self.indicator_channel,
                    self.compare_indicator,
                    self.compare_period,
                    self.compare_anchor,
                    self.compare_seed,
                    self.compare_std_dev,
                    self.drawing_id,
                    self.overlay_mode,
                    self.overlay_ratio,
                )
            ):
                raise ValueError("LEVEL_EVENT 不接受指标、画线或阈值字段")
            return self

        if self.direction is None:
            raise ValueError(f"{self.kind} 需要 direction")
        if self.level_id is not None or self.level_event is not None or self.level_kind is not None:
            raise ValueError(f"{self.kind} 不接受关键位字段")
        if self.drawing_id is None or self.overlay_mode is None:
            raise ValueError("OVERLAY_CROSS 需要 drawing_id 与 overlay_mode")
        if any(
            value is not None
            for value in (
                self.metric,
                self.threshold,
                self.indicator,
                self.indicator_period,
                self.indicator_anchor,
                self.indicator_seed,
                self.indicator_std_dev,
                self.indicator_channel,
                self.compare_indicator,
                self.compare_period,
                self.compare_anchor,
                self.compare_seed,
                self.compare_std_dev,
            )
        ):
            raise ValueError("OVERLAY_CROSS 不接受 metric 或指标字段")
        if self.overlay_ratio is not None:
            ratio = Decimal(self.overlay_ratio)
            if ratio < 0 or ratio > 1:
                raise ValueError("overlay_ratio 必须在 0 与 1 之间")
        return self


class AlertCreate(ContractModel):
    """创建预警的请求体；``draft=True`` 时只落草案，确认前绝不触发。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=16)
    condition: AlertCondition
    note: str = Field(default="", max_length=500)
    draft: bool = False
    repeat: bool = True
    cooldown_ms: int = Field(default=60_000, ge=0, le=86_400_000)
    expires_at: int | None = Field(default=None, ge=0)


class AlertPatch(ContractModel):
    """局部修改；至少提供一个字段。``enabled`` 控制禁用与重新启用。"""

    condition: AlertCondition | None = None
    note: str | None = Field(default=None, max_length=500)
    repeat: bool | None = None
    cooldown_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    expires_at: int | None = Field(default=None, ge=0)
    enabled: bool | None = None

    @model_validator(mode="after")
    def _require_field(self) -> AlertPatch:
        if not self.model_fields_set:
            raise ValueError("PATCH 至少需要一个字段")
        return self


class AlertRecord(ContractModel):
    """持久化的规则快照；``status`` 是引擎状态的权威来源。"""

    alert_id: str
    instrument_id: str
    timeframe: str
    condition: AlertCondition
    status: AlertStatus
    repeat: bool
    cooldown_ms: int
    expires_at: int | None = None
    note: str = ""
    created_ts: int
    updated_ts: int
    last_triggered_ts: int | None = None
    next_arm_ts: int | None = None
    trigger_count: int = 0


class AlertListResponse(ContractModel):
    alerts: list[AlertRecord]


class AlertTriggerRecord(ContractModel):
    """一次触发；``snapshot_id`` 可为空表示冻结失败（已记录告警）。"""

    trigger_id: str
    alert_id: str
    instrument_id: str
    timeframe: str
    condition_kind: AlertConditionKind
    triggered_ts: int
    snapshot_id: str
    observed: DecimalStr | None = None
    threshold: DecimalStr | None = None
    message: str
    note: str = ""


class AlertTriggerListResponse(ContractModel):
    triggers: list[AlertTriggerRecord]
