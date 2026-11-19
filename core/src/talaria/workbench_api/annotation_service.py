"""智能体标注用例层：硬门控、12 条上限、采纳与清理。

所有落库都经 ``StateWriterQueue.run``；门控所需的价格极值、ATR 与关键位
全部来自 Python 快照服务，前端与 DSH 插件只提交候选，不做任何放行判断。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal

from talaria.foundation.annotations import (
    AgentAnnotationCreate,
    AgentAnnotationRecord,
    AnnotationAdoptResponse,
    AnnotationAdoptionRecord,
    AnnotationClearRequest,
    AnnotationClearResponse,
    AnnotationGateError,
    AnnotationKind,
    AnnotationListResponse,
    AnnotationSessionsResponse,
    AnnotationStatus,
    FUTURE_BARS_LIMIT,
    SNAPSHOT_MISMATCH,
    evaluate_annotation,
)
from talaria.foundation.drawings import (
    DrawingCreate,
    DrawingKind,
    DrawingPoint,
)
from talaria.foundation.errors import (
    SnapshotNotFoundError,
    InvalidRequestError,
)
from talaria.foundation.market import MarketPack
from talaria.foundation.timeframes import timeframe_ms
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.state_store import StateWriterQueue

#: 单一标的未采纳活跃标注上限；第 13 条先归档最旧轮次。
ANNOTATION_LIMIT = 12


class AnnotationService:
    def __init__(
        self,
        *,
        queue: StateWriterQueue,
        workbench: WorkbenchService,
        drawings: DrawingService,
        pack: MarketPack,
        now_ms: Callable[[], int],
    ) -> None:
        self._queue = queue
        self._workbench = workbench
        self._drawings = drawings
        self._pack = pack
        self._now_ms = now_ms

    # ------------------------------------------------------------ 写入

    async def create_annotation(
        self, payload: AgentAnnotationCreate
    ) -> AgentAnnotationRecord:
        instrument = self._pack.instrument(payload.instrument_id)
        if instrument is None:
            raise AnnotationGateError(
                SNAPSHOT_MISMATCH, f"未知标的 {payload.instrument_id}"
            )
        try:
            meta = self._workbench.get_snapshot(payload.snapshot_id)
            page = self._workbench.get_bars(
                payload.snapshot_id,
                timeframe=payload.timeframe,
                limit=1_000_000,
                before_ts=None,
            )
            levels = self._workbench.get_key_levels(
                payload.snapshot_id,
                timeframe=payload.timeframe,
                top_n=None,
            )
        except SnapshotNotFoundError as error:
            raise AnnotationGateError(SNAPSHOT_MISMATCH, str(error)) from error
        evaluate_annotation(
            payload,
            instrument=instrument,
            snapshot_meta=meta,
            bars=page.bars,
            valid_level_ids={level.level_id for level in levels.levels},
        )
        created_ts = self._now_ms()
        record = AgentAnnotationRecord(
            annotation_id=f"ann_{uuid.uuid4().hex}",
            snapshot_id=payload.snapshot_id,
            instrument_id=payload.instrument_id,
            timeframe=payload.timeframe,
            session_id=payload.session_id,
            round=payload.round,
            kind=payload.kind,
            status=AnnotationStatus.ACTIVE,
            label=payload.label,
            rationale=payload.rationale,
            price_low=payload.price_low,
            price_high=payload.price_high,
            ts_anchor=payload.ts_anchor,
            evidence_refs=list(payload.evidence_refs),
            created_ts=created_ts,
            updated_ts=created_ts,
        )
        inserted, _archived = await self._queue.run(
            lambda store: store.insert_annotation(record, limit=ANNOTATION_LIMIT)
        )
        return inserted

    # ------------------------------------------------------------ 读取

    async def list_annotations(
        self,
        *,
        instrument_id: str | None = None,
        snapshot_id: str | None = None,
        session_id: str | None = None,
        include_archived: bool = False,
    ) -> AnnotationListResponse:
        records = await self._queue.run(
            lambda store: store.list_annotations(
                instrument_id=instrument_id,
                snapshot_id=snapshot_id,
                session_id=session_id,
                include_archived=include_archived,
            )
        )
        return AnnotationListResponse(annotations=records)

    async def list_sessions(self) -> AnnotationSessionsResponse:
        sessions = await self._queue.run(lambda store: store.list_annotation_sessions())
        return AnnotationSessionsResponse(sessions=sessions)

    # ------------------------------------------------------------ 采纳 / 忽略 / 清理

    async def adopt(self, annotation_id: str) -> AnnotationAdoptResponse:
        record = await self._queue.run(
            lambda store: store.get_annotation(annotation_id)
        )
        if record.status is AnnotationStatus.ADOPTED:
            adoption = await self._existing_adoption(annotation_id)
            if adoption is not None:
                return AnnotationAdoptResponse(annotation=record, adoption=adoption)
        if record.status is not AnnotationStatus.ACTIVE:
            raise InvalidRequestError("只有活跃标注可以采纳为人工画线")

        request = _adoption_drawing(record)
        drawing = await self._drawings.create_drawing(request)
        adopted_ts = self._now_ms()
        adoption = AnnotationAdoptionRecord(
            annotation_id=record.annotation_id,
            instrument_id=record.instrument_id,
            snapshot_id=record.snapshot_id,
            timeframe=record.timeframe,
            session_id=record.session_id,
            round=record.round,
            drawing_id=drawing.drawing_id,
            adopted_ts=adopted_ts,
        )

        def operation(store):
            current = store.get_annotation(annotation_id)
            if current.status is AnnotationStatus.ADOPTED:
                return current
            updated = current.model_copy(
                update={
                    "status": AnnotationStatus.ADOPTED,
                    "adopted_drawing_id": drawing.drawing_id,
                    "updated_ts": adopted_ts,
                }
            )
            store.update_annotation(updated)
            store.record_annotation_adoption(adoption)
            return updated

        updated = await self._queue.run(operation)
        return AnnotationAdoptResponse(annotation=updated, adoption=adoption)

    async def _existing_adoption(
        self, annotation_id: str
    ) -> AnnotationAdoptionRecord | None:
        adoptions = await self._queue.run(
            lambda store: store.list_annotation_adoptions()
        )
        return next(
            (item for item in adoptions if item.annotation_id == annotation_id),
            None,
        )

    async def dismiss(self, annotation_id: str) -> AgentAnnotationRecord:
        def operation(store):
            current = store.get_annotation(annotation_id)
            if current.status is AnnotationStatus.DISMISSED:
                return current
            if current.status is not AnnotationStatus.ACTIVE:
                raise InvalidRequestError("只有活跃标注可以忽略")
            return store.update_annotation(
                current.model_copy(
                    update={
                        "status": AnnotationStatus.DISMISSED,
                        "updated_ts": self._now_ms(),
                    }
                )
            )

        return await self._queue.run(operation)

    async def clear(self, payload: AnnotationClearRequest) -> AnnotationClearResponse:
        if self._pack.instrument(payload.instrument_id) is None:
            raise InvalidRequestError(f"未知标的 {payload.instrument_id}")
        cleared = await self._queue.run(
            lambda store: store.clear_annotations(
                payload.instrument_id,
                session_id=payload.session_id,
                ts=self._now_ms(),
            )
        )
        return AnnotationClearResponse(
            instrument_id=payload.instrument_id,
            session_id=payload.session_id,
            cleared=cleared,
        )


def _adoption_drawing(record: AgentAnnotationRecord) -> DrawingCreate:
    """采纳后生成一条与智能体图层完全独立的人工画线。"""

    if record.kind is AnnotationKind.LEVEL:
        return DrawingCreate(
            instrument_id=record.instrument_id,
            kind=DrawingKind.HORIZONTAL_LINE,
            visible_timeframes=[record.timeframe],
            points=[
                DrawingPoint(ts=record.ts_anchor, price=Decimal(record.price_low))
            ],
            text=record.label,
        )
    end_ts = record.ts_anchor + FUTURE_BARS_LIMIT * timeframe_ms(record.timeframe)
    return DrawingCreate(
        instrument_id=record.instrument_id,
        kind=DrawingKind.RECTANGLE,
        visible_timeframes=[record.timeframe],
        points=[
            DrawingPoint(ts=record.ts_anchor, price=Decimal(record.price_low)),
            DrawingPoint(ts=end_ts, price=Decimal(record.price_high)),
        ],
        text=record.label,
    )
