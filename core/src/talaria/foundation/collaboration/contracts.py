"""协同会诊的跨进程契约：会诊包、证据引用、Setup Insight 与 Abstain。"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import Field, field_validator, model_validator

from talaria.foundation.drawings import DrawingKind, DrawingPoint
from talaria.foundation.types import ContractModel, DecimalStr


class AbstainReason(StrEnum):
    """拒绝给出伪结论的稳定原因码；前端按此渲染黄色降级卡。"""

    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    GEOMETRY_INVERTED = "GEOMETRY_INVERTED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    HOST_OFFLINE = "HOST_OFFLINE"
    MODEL_INVALID_OUTPUT = "MODEL_INVALID_OUTPUT"


class ConsultationStatus(StrEnum):
    """一次会诊的生命周期；结果只能从 PENDING 落到其余三态。"""

    PENDING = "PENDING"
    READY = "READY"
    ABSTAIN = "ABSTAIN"
    FAILED = "FAILED"


class SetupDirection(StrEnum):
    """Setup 的方向；NEUTRAL 只允许描述观望条件，不生成入场几何。"""

    LONG = "LONG"
    SHORT = "SHORT"
    NEUTRAL = "NEUTRAL"


class EvidenceKind(StrEnum):
    """证据来源类别；只允许来自冻结快照内的只读工具或人工画线。"""

    SNAPSHOT = "SNAPSHOT"
    CHART_VIEW = "CHART_VIEW"
    KEY_LEVEL = "KEY_LEVEL"
    INDICATOR = "INDICATOR"
    OPERATOR = "OPERATOR"
    HUMAN_DRAWING = "HUMAN_DRAWING"


class CollabWindow(ContractModel):
    """框选或可视窗口：时间闭区间 + 价格闭区间。"""

    from_ts: int = Field(ge=0)
    to_ts: int = Field(ge=0)
    price_min: DecimalStr
    price_max: DecimalStr

    @model_validator(mode="after")
    def _check_bounds(self) -> Self:
        if self.to_ts < self.from_ts:
            raise ValueError("to_ts 不能早于 from_ts")
        if self.price_min > self.price_max:
            raise ValueError("price_min 不能大于 price_max")
        return self


class DrawingRef(ContractModel):
    """会诊包里冻结的人工画线快照，不引用可变的当前版本。"""

    drawing_id: str = Field(min_length=1, max_length=64)
    kind: DrawingKind
    points: list[DrawingPoint] = Field(min_length=1, max_length=4)
    text: str = ""


class CatalystEvent(ContractModel):
    """窗口内的日历事件；初版为空列表，字段先固定下来避免后续破坏契约。"""

    event_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    ts: int = Field(ge=0)
    kind: str = Field(default="CATALYST", min_length=1, max_length=32)
    source: str = Field(default="catalyst_calendar", min_length=1, max_length=64)


class EvidenceRef(ContractModel):
    """可核对的证据引用；``evidence_id`` 是工具/画线/关键位的稳定 ID。"""

    evidence_id: str = Field(min_length=1, max_length=160)
    kind: EvidenceKind
    label: str = Field(min_length=1, max_length=200)
    detail: str = Field(default="", max_length=500)
    price_low: DecimalStr | None = None
    price_high: DecimalStr | None = None

    @model_validator(mode="after")
    def _check_price_pair(self) -> Self:
        if (self.price_low is None) != (self.price_high is None):
            raise ValueError("price_low 与 price_high 必须同时提供")
        if (
            self.price_low is not None
            and self.price_high is not None
            and self.price_low > self.price_high
        ):
            raise ValueError("price_low 不能大于 price_high")
        return self


class ConsultRequest(ContractModel):
    """前端发起会诊的请求；``snapshot_id`` 必须由快通道先冻结。"""

    snapshot_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=2000)
    visible: CollabWindow
    selection: CollabWindow | None = None
    drawing_ids: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("question")
    @classmethod
    def _strip_question(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("会诊问题不能为空")
        return stripped

    @model_validator(mode="after")
    def _check_drawing_ids(self) -> Self:
        if len(set(self.drawing_ids)) != len(self.drawing_ids):
            raise ValueError("drawing_ids 不能重复")
        return self


class ConsultationPackage(ContractModel):
    """冻结后交给宿主的完整增量；提示词第三段与证据校验共用它。"""

    consultation_id: str = Field(min_length=1, max_length=64)
    snapshot_id: str = Field(min_length=1, max_length=64)
    instrument_id: str = Field(min_length=1, max_length=64)
    timeframe: str = Field(min_length=1, max_length=8)
    visible: CollabWindow
    selection: CollabWindow | None = None
    drawings: list[DrawingRef] = Field(default_factory=list)
    catalysts: list[CatalystEvent] = Field(default_factory=list)
    question: str = Field(min_length=1, max_length=2000)
    snapshot_cutoff_ts: int = Field(ge=0)
    evidence_catalog: list[EvidenceRef] = Field(default_factory=list)
    created_ts: int = Field(ge=0)


class SetupInsight(ContractModel):
    """带证据的可执行观察；不是订单，不携带任何写操作。"""

    direction: SetupDirection
    summary: str = Field(min_length=1, max_length=4000)
    entry_low: DecimalStr
    entry_high: DecimalStr
    invalidation_stop: DecimalStr
    take_profit: list[DecimalStr] = Field(min_length=1, max_length=4)
    max_position: DecimalStr | None = None
    valid_until: int | None = Field(default=None, ge=0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)

    @model_validator(mode="after")
    def _check_entry_band(self) -> Self:
        if self.entry_low > self.entry_high:
            raise ValueError("entry_low 不能大于 entry_high")
        if self.max_position is not None and self.max_position <= 0:
            raise ValueError("max_position 必须大于 0")
        return self


class Abstain(ContractModel):
    """明确的拒绝：原因码 + 人类可读说明 + 可核对的证据引用。"""

    reason_code: AbstainReason
    message: str = Field(min_length=1, max_length=1000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=40)


class ConsultationRecord(ContractModel):
    """持久化的一次会诊；前端轮询该结构渲染会话流。"""

    consultation_id: str
    snapshot_id: str
    instrument_id: str
    timeframe: str
    status: ConsultationStatus
    package: ConsultationPackage
    insight: SetupInsight | None = None
    abstain: Abstain | None = None
    error: str | None = None
    dsh_session_id: str | None = Field(default=None, max_length=160)
    dsh_turn_id: str | None = Field(default=None, max_length=64)
    created_ts: int
    updated_ts: int
    completed_ts: int | None = None


class ConsultResponse(ContractModel):
    """``POST /api/collaboration/consult`` 的快速回执。"""

    consultation_id: str
    snapshot_id: str
    status: ConsultationStatus


class ConsultationListResponse(ContractModel):
    consultations: list[ConsultationRecord] = Field(default_factory=list)


class HostResultRequest(ContractModel):
    """宿主回调；二选一给出 insight 或 abstain，也可只回文本让服务端解析。"""

    text: str | None = Field(default=None, max_length=20000)
    insight: SetupInsight | None = None
    abstain: Abstain | None = None
    error: str | None = Field(default=None, max_length=1000)
    dsh_session_id: str | None = Field(default=None, max_length=128)
    dsh_turn_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _check_one_result(self) -> Self:
        provided = sum(
            value is not None
            for value in (self.text, self.insight, self.abstain, self.error)
        )
        if provided != 1:
            raise ValueError("text/insight/abstain/error 必须且只能提供一个")
        return self


class UsageReport(ContractModel):
    """一轮 Token 用量；未知字段用 None 表示缺失，绝不按 0 入账。"""

    dsh_session_id: str = Field(min_length=1, max_length=128)
    dsh_turn_id: str = Field(min_length=1, max_length=64)
    input_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    source: str = Field(default="host", min_length=1, max_length=32)

    @model_validator(mode="after")
    def _check_any_value(self) -> Self:
        if (
            self.input_tokens is None
            and self.cached_input_tokens is None
            and self.output_tokens is None
            and self.source != "missing"
        ):
            raise ValueError("至少需要一个 Token 计数字段；缺失不能按 0 上报")
        return self


class UsageRow(ContractModel):
    dsh_session_id: str
    dsh_turn_id: str
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    source: str
    reported_ts: int


class UsageReportResponse(ContractModel):
    recorded: bool
    dsh_session_id: str
    dsh_turn_id: str
    duplicate: bool = False


class UsageSummary(ContractModel):
    """按会话与全局聚合；缺失字段以 ``missing_reports`` 单独计数。"""

    dsh_session_id: str | None = None
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    turns: int = 0
    sessions: int = 0
    missing_reports: int = 0
    updated_ts: int = 0


class UsageSummaryResponse(ContractModel):
    summary: UsageSummary
    sessions: list[UsageSummary] = Field(default_factory=list)
    recent: list[UsageRow] = Field(default_factory=list)
