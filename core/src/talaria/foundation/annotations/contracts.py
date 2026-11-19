"""智能体标注契约：图谱层 ``AGENT_ANNOTATION`` 的唯一数据结构。

标注只表达「价格区间 + 时间锚点 + 可核验证据」，不携带指标数值，也不复制
前端的任何几何计算。所有校验在 Python 端完成后才允许落库。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from talaria.foundation.types import ContractModel, DecimalStr


class AnnotationKind(StrEnum):
    """标注几何：水平线或价格带；与人工画线保持独立图层。"""

    LEVEL = "LEVEL"
    ZONE = "ZONE"


class AnnotationStatus(StrEnum):
    """标注生命周期；``ARCHIVED`` 仅因 12 条上限被动产生，仍保留审计。"""

    ACTIVE = "ACTIVE"
    ADOPTED = "ADOPTED"
    DISMISSED = "DISMISSED"
    ARCHIVED = "ARCHIVED"


class AnnotationEvidence(ContractModel):
    """一条可核验证据：引用快照内关键位，或引用窗口内的具体 K 线。

    ``level_id`` 与 ``kline_ts`` 必须恰好提供一个。K 线时间戳允许与真实
    bar 相差一根周期以内（由服务端按 ``timeframe`` 显式判定）。
    """

    level_id: str | None = Field(default=None, min_length=1, max_length=120)
    kline_ts: int | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def _exactly_one_reference(self) -> AnnotationEvidence:
        references = [
            value for value in (self.level_id, self.kline_ts) if value is not None
        ]
        if len(references) != 1:
            raise ValueError("证据必须且只能提供 level_id 或 kline_ts 之一")
        return self


class AgentAnnotationCreate(ContractModel):
    """智能体写入载荷；状态永远由服务端决定，客户端不能指定。"""

    snapshot_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    session_id: str = Field(min_length=1, max_length=120)
    round: int = Field(ge=0)
    kind: AnnotationKind
    label: str = Field(min_length=1, max_length=200)
    rationale: str = Field(default="", max_length=2000)
    price_low: DecimalStr
    price_high: DecimalStr
    ts_anchor: int = Field(ge=0)
    evidence_refs: list[AnnotationEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_prices(self) -> AgentAnnotationCreate:
        if self.price_low > self.price_high:
            raise ValueError("price_low 不得高于 price_high")
        if self.kind is AnnotationKind.LEVEL and self.price_low != self.price_high:
            raise ValueError("LEVEL 标注必须使用单一价格（price_low = price_high）")
        return self


class AgentAnnotationRecord(ContractModel):
    """持久化后的标注；``adopted_drawing_id`` 指向独立的人工画线副本。"""

    annotation_id: str
    snapshot_id: str
    instrument_id: str
    timeframe: str
    session_id: str
    round: int
    kind: AnnotationKind
    status: AnnotationStatus
    label: str
    rationale: str
    price_low: DecimalStr
    price_high: DecimalStr
    ts_anchor: int
    evidence_refs: list[AnnotationEvidence]
    created_ts: int
    updated_ts: int
    adopted_drawing_id: str | None = None
    archived_reason: str | None = None


class AnnotationListResponse(ContractModel):
    annotations: list[AgentAnnotationRecord]


class AnnotationSessionSummary(ContractModel):
    session_id: str
    instrument_ids: list[str]
    annotation_count: int
    latest_ts: int


class AnnotationSessionsResponse(ContractModel):
    sessions: list[AnnotationSessionSummary]


class AnnotationAdoptionRecord(ContractModel):
    """采纳质量反馈：标注几何 → 人工画线的独立副本。"""

    annotation_id: str
    instrument_id: str
    snapshot_id: str
    timeframe: str
    session_id: str
    round: int
    drawing_id: str
    adopted_ts: int


class AnnotationAdoptResponse(ContractModel):
    annotation: AgentAnnotationRecord
    adoption: AnnotationAdoptionRecord


class AnnotationClearRequest(ContractModel):
    instrument_id: str = Field(min_length=1, max_length=64)
    session_id: str | None = Field(default=None, min_length=1, max_length=120)


class AnnotationClearResponse(ContractModel):
    instrument_id: str
    session_id: str | None
    cleared: int
