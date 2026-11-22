"""预警规则与触发记录的跨进程契约。

预警规则是用户意图，触发记录是不可变的证据索引：触发时冻结的
``snapshot_id`` 指向当时的行情快照，前端据此回看现场。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from talaria.foundation.rules import Rule
from talaria.foundation.types import ContractModel, DecimalStr


class AlertTriggerKind(StrEnum):
    """触发记录的展示类别。保留既有记录的分类值。"""

    METRIC_THRESHOLD = "METRIC_THRESHOLD"
    INDICATOR_SIGNAL = "INDICATOR_SIGNAL"
    OVERLAY_CROSS = "OVERLAY_CROSS"
    LEVEL_EVENT = "LEVEL_EVENT"
    RULE = "RULE"


class AlertStatus(StrEnum):
    """规则状态机：草案/待命/冷却/过期/禁用。"""

    DRAFT = "DRAFT"
    ARMED = "ARMED"
    COOLDOWN = "COOLDOWN"
    EXPIRED = "EXPIRED"
    DISABLED = "DISABLED"


class AlertCreate(ContractModel):
    """创建预警的请求体；``draft=True`` 时只落草案，确认前绝不触发。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=16)
    condition: Rule
    note: str = Field(default="", max_length=500)
    draft: bool = False
    repeat: bool = True
    cooldown_ms: int = Field(default=60_000, ge=0, le=86_400_000)
    expires_at: int | None = Field(default=None, ge=0)


class AlertPatch(ContractModel):
    """局部修改；至少提供一个字段。``enabled`` 控制禁用与重新启用。"""

    condition: Rule | None = None
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
    condition: Rule
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
    condition_kind: AlertTriggerKind
    triggered_ts: int
    snapshot_id: str
    observed: DecimalStr | None = None
    threshold: DecimalStr | None = None
    message: str
    note: str = ""


class AlertTriggerListResponse(ContractModel):
    triggers: list[AlertTriggerRecord]
