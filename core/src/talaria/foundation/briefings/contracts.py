"""扫描命中后的协同简报契约。

简报只描述冻结快照内的触发原因、关键价位参考、风险与证据引用；没有有效
观点时必须给出稳定的 Abstain 原因码，不能输出伪结论。所有价格字段沿用
``DecimalStr``，JSON 边界只出现十进制字符串。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from talaria.foundation.collaboration import Abstain, EvidenceRef, UsageReport
from talaria.foundation.types import ContractModel, DecimalStr


class BriefingStatus(StrEnum):
    """一次简报的生命周期。"""

    PENDING = "PENDING"
    READY = "READY"
    ABSTAIN = "ABSTAIN"
    FAILED = "FAILED"


class BriefingLevelRef(ContractModel):
    """关键价位参考；价格未知时只保留观察说明。"""

    label: str = Field(min_length=1, max_length=200)
    price: DecimalStr | None = None
    note: str = Field(default="", max_length=300)


class BriefingContent(ContractModel):
    """简报正文的四段结构；证据列表单独放在 ``ScannerBriefing`` 上。"""

    trigger_reason: str = Field(min_length=1, max_length=2000)
    key_levels: list[BriefingLevelRef] = Field(default_factory=list, max_length=12)
    risks: list[str] = Field(default_factory=list, max_length=12)


class ScannerBriefing(ContractModel):
    """持久化的一条简报；``snapshot_id`` 必须与触发它的扫描冻结快照一致。"""

    id: str = Field(min_length=1, max_length=64)
    trigger_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=128)
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    status: BriefingStatus
    summary: BriefingContent | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)
    abstain_reason: str | None = Field(default=None, max_length=32)
    created_ts: int = Field(ge=0)
    read_ts: int | None = Field(default=None, ge=0)


class BriefingListResponse(ContractModel):
    briefings: list[ScannerBriefing] = Field(default_factory=list)
    unread: int = 0
    generated_ts: int = Field(ge=0)


class BriefingConsultRequest(ContractModel):
    """把简报带入右栏对话时可选的自定义问题。"""

    question: str | None = Field(default=None, max_length=2000)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("带入对话的问题不能为空")
        return stripped


class BriefingConsultResponse(ContractModel):
    """右侧协同对话的预填种子；不会在这里立即调用模型。"""

    briefing_id: str
    snapshot_id: str
    instrument_id: str
    timeframe: str
    question: str = Field(min_length=1, max_length=2000)


class BriefingResultRequest(ContractModel):
    """宿主回调；``text`` 由服务端解析成四段结构，也可直接给结构化内容。"""

    text: str | None = Field(default=None, max_length=20000)
    content: BriefingContent | None = None
    abstain: Abstain | None = None
    error: str | None = Field(default=None, max_length=1000)
    dsh_session_id: str | None = Field(default=None, max_length=128)
    dsh_turn_id: str | None = Field(default=None, max_length=64)
    usage: UsageReport | None = None

    @model_validator(mode="after")
    def _check_one_result(self) -> Self:
        provided = sum(
            value is not None
            for value in (self.text, self.content, self.abstain, self.error)
        )
        if provided != 1:
            raise ValueError("text/content/abstain/error 必须且只能提供一个")
        return self
