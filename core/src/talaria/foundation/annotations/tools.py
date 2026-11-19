"""DSH 工具输入模型：只用于生成受控子集 schema，真实校验在 Python 服务端。"""

from __future__ import annotations

from pydantic import Field

from talaria.foundation.alerts import AlertCondition
from talaria.foundation.annotations.contracts import (
    AgentAnnotationCreate,
    AnnotationEvidence,
    AnnotationKind,
)
from talaria.foundation.collaboration.tools import ToolBase
from talaria.foundation.types import DecimalStr


class WriteAgentOverlayInput(ToolBase):
    """在冻结快照上写入一条受门控的智能体标注。"""

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

    def to_create(self) -> AgentAnnotationCreate:
        return AgentAnnotationCreate(
            snapshot_id=self.snapshot_id,
            instrument_id=self.instrument_id,
            timeframe=self.timeframe,
            session_id=self.session_id,
            round=self.round,
            kind=self.kind,
            label=self.label,
            rationale=self.rationale,
            price_low=self.price_low,
            price_high=self.price_high,
            ts_anchor=self.ts_anchor,
            evidence_refs=self.evidence_refs,
        )


class CreateAlertDraftInput(ToolBase):
    """创建一条智能体预警草案；服务端与桥接层都强制 ``draft=True``。"""

    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    condition: AlertCondition
    note: str = Field(default="", max_length=200)
    repeat: bool = True
    cooldown_ms: int = Field(default=60_000, ge=1_000, le=86_400_000)
    expires_at: int | None = Field(default=None, ge=0)
