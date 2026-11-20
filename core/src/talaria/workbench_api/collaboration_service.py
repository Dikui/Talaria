"""协同会诊服务：快通道冻结、会诊包拼装、宿主派发与 Token 留痕。

快通道（切图、画线、预警、快照冻结）永远不经过这里；本服务只在冻结之后
读取快照与画线，把会诊包交给 DSH 宿主，并把宿主结果落进
``state/collaboration_state.sqlite``。宿主慢、离线或超时只会让本次会诊
变成 Abstain，不会阻塞图表。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any

import httpx

from talaria.foundation.collaboration import (
    Abstain,
    AbstainReason,
    CollabWindow,
    ConsultationPackage,
    ConsultationRecord,
    ConsultationStatus,
    ConsultRequest,
    ConsultResponse,
    DrawingRef,
    EvidenceKind,
    EvidenceRef,
    HostResultRequest,
    SetupInsight,
    build_consultation_prompt,
    canonicalize_insight,
    evaluate_insight,
    parse_host_analysis,
    stale_abstain,
)
from talaria.foundation.drawings import ChartView, DrawingRecord
from talaria.foundation.errors import InvalidRequestError
from talaria.foundation.key_levels.contracts import KeyLevelSet
from talaria.foundation.snapshot import SnapshotMeta
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.state_store import StateWriterQueue

HostDispatcher = Callable[[str, Mapping[str, Any]], Awaitable[None]]

DEFAULT_STALE_AFTER_MS = 15 * 60_000
DEFAULT_OVERALL_TIMEOUT_S = 120.0
MAX_EVIDENCE = 40


def _price_ref(
    evidence_id: str,
    kind: EvidenceKind,
    label: str,
    low: Decimal | None,
    high: Decimal | None,
    *,
    detail: str = "",
) -> EvidenceRef:
    if low is None or high is None:
        return EvidenceRef(
            evidence_id=evidence_id, kind=kind, label=label, detail=detail
        )
    return EvidenceRef(
        evidence_id=evidence_id,
        kind=kind,
        label=label,
        detail=detail,
        price_low=low,
        price_high=high,
    )


def _drawing_ref(drawing: DrawingRecord) -> DrawingRef:
    return DrawingRef(
        drawing_id=drawing.drawing_id,
        kind=drawing.kind,
        points=list(drawing.points),
        text=drawing.text,
    )


def _drawing_evidence(drawing: DrawingRecord) -> EvidenceRef:
    prices = [point.price for point in drawing.points]
    label = f"人工画线 {drawing.kind.value}"
    return _price_ref(
        f"drawing:{drawing.drawing_id}",
        EvidenceKind.HUMAN_DRAWING,
        label,
        min(prices),
        max(prices),
        detail=drawing.text,
    )


def _level_evidence(level: Any) -> EvidenceRef:
    if level.price is not None:
        low = high = level.price
    else:
        low = level.price_low
        high = level.price_high
    detail = (
        f"类别 {level.category.value} · 证据 {level.evidence_class.value} · "
        f"强度 {level.strength:.2f}"
    )
    return _price_ref(
        f"level:{level.level_id}",
        EvidenceKind.KEY_LEVEL,
        f"关键位 {level.kind.value}",
        low,
        high,
        detail=detail,
    )


def _chart_evidence(package_id: str, timeframe: str, view: ChartView) -> EvidenceRef:
    window = view.window
    return _price_ref(
        f"chartview:{package_id}:{timeframe}",
        EvidenceKind.CHART_VIEW,
        f"可视窗口 {timeframe}",
        window.price_min,
        window.price_max,
        detail=f"时间 {window.from_ts}–{window.to_ts}",
    )


class CollaborationService:
    def __init__(
        self,
        *,
        queue: StateWriterQueue,
        workbench: WorkbenchService,
        drawings: DrawingService,
        market_skill: str,
        host_url: Callable[[], str | None],
        now_ms: Callable[[], int],
        stale_after_ms: int = DEFAULT_STALE_AFTER_MS,
        overall_timeout_s: float = DEFAULT_OVERALL_TIMEOUT_S,
        dispatcher: HostDispatcher | None = None,
        http_timeout_s: float = 10.0,
    ) -> None:
        self._queue = queue
        self._workbench = workbench
        self._drawings = drawings
        self._market_skill = market_skill
        self._host_url = host_url
        self._now_ms = now_ms
        self._stale_after_ms = stale_after_ms
        self._overall_timeout_s = overall_timeout_s
        self._http_timeout_s = http_timeout_s
        self._dispatcher = dispatcher or self._post_to_host
        self._events: dict[str, asyncio.Event] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    # -------------------------------------------------------------- 快照读取

    def _meta(self, snapshot_id: str) -> SnapshotMeta:
        return self._workbench.get_snapshot(snapshot_id)

    def _key_levels(self, snapshot_id: str, timeframe: str) -> KeyLevelSet:
        return self._workbench.get_key_levels(snapshot_id, timeframe=timeframe, top_n=30)

    # -------------------------------------------------------------- 会诊包

    async def _build_package(self, request: ConsultRequest) -> ConsultationPackage:
        meta = self._meta(request.snapshot_id)
        if meta.instrument_id != request.instrument_id:
            raise InvalidRequestError(
                f"快照 {meta.snapshot_id} 属于 {meta.instrument_id}，"
                f"与请求的 {request.instrument_id} 不一致"
            )
        if request.timeframe not in meta.timeframes:
            raise InvalidRequestError(
                f"快照 {meta.snapshot_id} 未包含周期 {request.timeframe}"
            )

        view = await self._drawings.get_chart_view(
            meta.instrument_id,
            request.timeframe,
            from_ts=request.visible.from_ts,
            to_ts=request.visible.to_ts,
            price_min=request.visible.price_min,
            price_max=request.visible.price_max,
        )

        wanted = set(request.drawing_ids)
        drawings = [
            drawing
            for drawing in view.drawings
            if not wanted or drawing.drawing_id in wanted
        ]

        levels = self._key_levels(request.snapshot_id, request.timeframe)
        evidence: list[EvidenceRef] = [
            EvidenceRef(
                evidence_id=f"snapshot:{meta.snapshot_id}",
                kind=EvidenceKind.SNAPSHOT,
                label=f"快照 {meta.snapshot_id}",
                detail=(
                    f"{meta.instrument_id} · {request.timeframe} · "
                    f"截止 {meta.cutoff_ts}"
                ),
            )
        ]

        consultation_id = f"cons_{uuid.uuid4().hex}"
        evidence.append(
            _chart_evidence(consultation_id, request.timeframe, view)
        )
        evidence.extend(_level_evidence(level) for level in levels.levels)
        evidence.extend(_drawing_evidence(drawing) for drawing in drawings)

        package = ConsultationPackage(
            consultation_id=consultation_id,
            snapshot_id=meta.snapshot_id,
            instrument_id=meta.instrument_id,
            timeframe=request.timeframe,
            visible=request.visible,
            selection=request.selection,
            drawings=[_drawing_ref(drawing) for drawing in drawings],
            catalysts=[],
            question=request.question,
            snapshot_cutoff_ts=meta.cutoff_ts,
            evidence_catalog=evidence[:MAX_EVIDENCE],
            created_ts=self._now_ms(),
        )
        return package

    # -------------------------------------------------------------- 发起会诊

    async def consult(self, request: ConsultRequest) -> ConsultResponse:
        package = await self._build_package(request)
        abstain = stale_abstain(
            package,
            now_ms=self._now_ms(),
            stale_after_ms=self._stale_after_ms,
        )
        prompt = build_consultation_prompt(package, market_skill=self._market_skill)
        record = ConsultationRecord(
            consultation_id=package.consultation_id,
            snapshot_id=package.snapshot_id,
            instrument_id=package.instrument_id,
            timeframe=package.timeframe,
            status=ConsultationStatus.PENDING,
            package=package,
            created_ts=package.created_ts,
            updated_ts=package.created_ts,
        )
        if abstain is not None:
            record = record.model_copy(
                update={
                    "status": ConsultationStatus.ABSTAIN,
                    "abstain": abstain,
                    "completed_ts": self._now_ms(),
                }
            )
            await self._queue.run(
                lambda store: store.consultations.create(record, prompt=prompt)
            )
            return ConsultResponse(
                consultation_id=record.consultation_id,
                snapshot_id=record.snapshot_id,
                status=record.status,
            )

        await self._queue.run(
            lambda store: store.consultations.create(record, prompt=prompt)
        )
        self._track(self._run(record.consultation_id, prompt))
        return ConsultResponse(
            consultation_id=record.consultation_id,
            snapshot_id=record.snapshot_id,
            status=record.status,
        )

    def _track(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, consultation_id: str, prompt: str) -> None:
        event = asyncio.Event()
        self._events[consultation_id] = event
        try:
            url = self._host_url()
            if not url:
                await self._finish_abstain(
                    consultation_id,
                    AbstainReason.HOST_OFFLINE,
                    "协同宿主离线：本轮未调用模型；图表快通道不受影响，"
                    "请稍后重试或先启动 DSH 宿主。",
                )
                return
            payload = {
                "consultation_id": consultation_id,
                "prompt": prompt,
                "timeout_ms": int(self._overall_timeout_s * 1000),
            }
            try:
                await self._dispatcher(url, payload)
            except (httpx.HTTPError, OSError, RuntimeError) as exc:
                await self._finish_abstain(
                    consultation_id,
                    AbstainReason.HOST_OFFLINE,
                    f"协同宿主不可达：{exc}；图表快通道不受影响。",
                )
                return
            try:
                await asyncio.wait_for(event.wait(), timeout=self._overall_timeout_s)
            except TimeoutError:
                await self._finish_abstain(
                    consultation_id,
                    AbstainReason.MODEL_TIMEOUT,
                    f"推理超过 {self._overall_timeout_s:.0f} 秒未返回；"
                    "已取消本轮结论，不输出伪结论。",
                )
        finally:
            self._events.pop(consultation_id, None)

    async def _post_to_host(self, url: str, payload: Mapping[str, Any]) -> None:
        async with httpx.AsyncClient(
            timeout=self._http_timeout_s, trust_env=False
        ) as client:
            response = await client.post(
                f"{url.rstrip('/')}/talaria/consult", json=dict(payload)
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"宿主返回 {response.status_code}：{response.text[:200]}"
            )

    # -------------------------------------------------------------- 结果落库

    async def _finish_abstain(
        self,
        consultation_id: str,
        reason: AbstainReason,
        message: str,
        *,
        evidence_refs: list[EvidenceRef] | None = None,
    ) -> ConsultationRecord:
        def operation(store: Any) -> ConsultationRecord:
            record = store.consultations.get(consultation_id)
            if record.status is not ConsultationStatus.PENDING:
                return record
            now = self._now_ms()
            updated = record.model_copy(
                update={
                    "status": ConsultationStatus.ABSTAIN,
                    "abstain": Abstain(
                        reason_code=reason,
                        message=message,
                        evidence_refs=evidence_refs or [],
                    ),
                    "updated_ts": now,
                    "completed_ts": now,
                }
            )
            store.consultations.update(updated)
            return updated

        result = await self._queue.run(operation)
        event = self._events.get(consultation_id)
        if event is not None:
            event.set()
        return result

    async def submit_result(
        self,
        consultation_id: str,
        payload: HostResultRequest,
    ) -> ConsultationRecord:
        record = await self._queue.read(
            lambda store: store.consultations.get(consultation_id)
        )
        if record.status is not ConsultationStatus.PENDING:
            return record

        now = self._now_ms()

        if payload.error is not None:
            updated = record.model_copy(
                update={
                    "status": ConsultationStatus.FAILED,
                    "error": payload.error,
                    "updated_ts": now,
                    "completed_ts": now,
                }
            )
            return await self._commit(updated, payload)

        if payload.abstain is not None:
            updated = record.model_copy(
                update={
                    "status": ConsultationStatus.ABSTAIN,
                    "abstain": payload.abstain,
                    "updated_ts": now,
                    "completed_ts": now,
                }
            )
            return await self._commit(updated, payload)

        insight = payload.insight
        if insight is None and payload.text is not None:
            try:
                parsed = parse_host_analysis(payload.text)
            except ValueError as exc:
                return await self._finish_abstain(
                    consultation_id,
                    AbstainReason.MODEL_INVALID_OUTPUT,
                    f"宿主输出无法解析为 Setup Insight 或 Abstain：{exc}",
                )
            if isinstance(parsed, Abstain):
                updated = record.model_copy(
                    update={
                        "status": ConsultationStatus.ABSTAIN,
                        "abstain": parsed,
                        "updated_ts": now,
                        "completed_ts": now,
                    }
                )
                return await self._commit(updated, payload)
            insight = parsed

        if insight is None:
            return await self._finish_abstain(
                consultation_id,
                AbstainReason.MODEL_INVALID_OUTPUT,
                "宿主没有返回 insight 或 abstain 内容。",
            )

        stale = stale_abstain(
            record.package,
            now_ms=now,
            stale_after_ms=self._stale_after_ms,
        )
        if stale is not None:
            updated = record.model_copy(
                update={
                    "status": ConsultationStatus.ABSTAIN,
                    "abstain": stale,
                    "updated_ts": now,
                    "completed_ts": now,
                }
            )
            return await self._commit(updated, payload)

        canonical = canonicalize_insight(record.package, insight)
        conflict = evaluate_insight(record.package, canonical)
        if conflict is not None:
            updated = record.model_copy(
                update={
                    "status": ConsultationStatus.ABSTAIN,
                    "abstain": conflict,
                    "updated_ts": now,
                    "completed_ts": now,
                }
            )
            return await self._commit(updated, payload)

        updated = record.model_copy(
            update={
                "status": ConsultationStatus.READY,
                "insight": canonical,
                "updated_ts": now,
                "completed_ts": now,
            }
        )
        return await self._commit(updated, payload)

    async def _commit(
        self,
        record: ConsultationRecord,
        payload: HostResultRequest,
    ) -> ConsultationRecord:
        def operation(store: Any) -> ConsultationRecord:
            current = store.consultations.get(record.consultation_id)
            if current.status is not ConsultationStatus.PENDING:
                return current
            saved = store.consultations.update(
                record,
                host_session_id=payload.dsh_session_id,
                host_turn_id=payload.dsh_turn_id,
            )
            return saved

        saved = await self._queue.run(operation)
        event = self._events.get(record.consultation_id)
        if event is not None:
            event.set()
        return saved

    # -------------------------------------------------------------- 查询与用量

    async def get(self, consultation_id: str) -> ConsultationRecord:
        return await self._queue.read(
            lambda store: store.consultations.get(consultation_id)
        )

    async def list(self, *, limit: int = 50) -> list[ConsultationRecord]:
        return await self._queue.read(lambda store: store.consultations.list(limit=limit))

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._events.clear()
