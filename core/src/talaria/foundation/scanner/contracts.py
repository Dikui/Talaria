"""纯数值扫描器的跨进程契约。

扫描器只消费 ``MarketEventFeed`` 的行情事件与 07 的衍生品序列，输出
``ScannerTrigger``；它不依赖任何模型、网络或提示词。Evidence Snapshot 的
``snapshot_id`` 在 Trigger 生成前冻结，保证卡片与图表引用同一份证据。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from talaria.foundation.types import ContractModel, DecimalStr


class ScannerConditionKind(StrEnum):
    """任务单要求的五类数值异动。"""

    LIQUIDITY_ANOMALY = "LIQUIDITY_ANOMALY"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    FUNDING_EXTREME = "FUNDING_EXTREME"
    OI_DIVERGENCE = "OI_DIVERGENCE"
    KEY_LEVEL_EVENT = "KEY_LEVEL_EVENT"


class ScannerSeverity(StrEnum):
    """异动严重度；仅用于展示排序，不触发模型。"""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ScannerTriggerRoute(StrEnum):
    """触发路由；本任务只落地 ``LOCAL``。"""

    LOCAL = "LOCAL"
    CO_PILOT_CONSULT = "CO_PILOT_CONSULT"


class ScannerEvidence(ContractModel):
    """一条可复核的数值证据，值与阈值一律用十进制字符串。"""

    metric: str
    value: DecimalStr
    threshold: DecimalStr | None = None
    note: str | None = None


class ScannerTrigger(ContractModel):
    """一次数值异动命中；``snapshot_id`` 指向冻结后的 Evidence Snapshot。"""

    trigger_id: str
    instrument_id: str
    timeframe: str
    kind: ScannerConditionKind
    severity: ScannerSeverity
    title: str
    summary: str
    evidence: list[ScannerEvidence] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    created_ts: int
    snapshot_id: str
    route: ScannerTriggerRoute = ScannerTriggerRoute.LOCAL


class ScannerTriggerListResponse(ContractModel):
    """按时间倒序返回的异动列表。"""

    triggers: list[ScannerTrigger]
    total: int
    generated_ts: int


class ScannerFocusItem(ContractModel):
    """SCANNER_FOCUS 系统分组中的一行，由最近的 Trigger 聚合而来。"""

    instrument_id: str
    last_trigger_ts: int
    trigger_count: int
    severity: ScannerSeverity
    kinds: list[ScannerConditionKind] = Field(default_factory=list)
    last_title: str | None = None
    last_timeframe: str | None = None
    last_snapshot_id: str | None = None


class ScannerFocusResponse(ContractModel):
    """焦点分组视图；``instrument_ids`` 可直接与 watchlist 分组对照。"""

    group_id: str
    name: str
    items: list[ScannerFocusItem]
    instrument_ids: list[str]
    generated_ts: int


class ScannerScanRequest(ContractModel):
    """手动扫描一次；省略标的时扫描当前 Watchlist。"""

    instrument_ids: list[str] | None = None


class ScannerScanResponse(ContractModel):
    """一次扫描的结果与范围，便于复现。"""

    triggers: list[ScannerTrigger]
    scanned_instruments: list[str]
    generated_ts: int
