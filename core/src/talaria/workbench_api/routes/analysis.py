"""标的、快照、指标与关键位分析接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from talaria.foundation.drawings import DrawingRecord
from talaria.foundation.errors import (
    InvalidRequestError,
    NotFoundError,
)
from talaria.foundation.derivatives import DerivativeBundle
from talaria.foundation.indicators import (
    IndicatorBundle,
    IndicatorDefaultsResponse,
    IndicatorParams,
    IndicatorSeries,
    OperatorRequest,
)
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevelAdoptionRequest,
    KeyLevelCategory,
    KeyLevelFeedbackSummary,
    KeyLevelIgnoreList,
    KeyLevelIgnoreRequest,
    KeyLevelParams,
    KeyLevelSet,
    VwapAnchor,
)
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.operators import IndicatorName
from talaria.foundation.snapshot import (
    ChartPage,
    SnapshotMeta,
    SnapshotRequest,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/instruments", response_model=list[InstrumentSpec])
def instruments(request: Request) -> list[InstrumentSpec]:
    return list(get_runtime(request).workbench.instruments())


@router.post("/api/snapshots", response_model=SnapshotMeta)
async def create_snapshot(
    payload: SnapshotRequest, request: Request
) -> SnapshotMeta:
    runtime = get_runtime(request)
    workbench = runtime.workbench
    meta = workbench.create_snapshot(payload)
    for timeframe in meta.timeframes:
        level_set = workbench.get_key_levels(
            meta.snapshot_id, timeframe=timeframe
        )
        key: tuple[str, str] = (meta.instrument_id, timeframe)
        runtime.desk.publish_levels(key, level_set)
    return meta


@router.get("/api/snapshots/{snapshot_id}", response_model=SnapshotMeta)
def get_snapshot(snapshot_id: str, request: Request) -> SnapshotMeta:
    return get_runtime(request).workbench.get_snapshot(snapshot_id)


@router.get("/api/snapshots/{snapshot_id}/bars", response_model=ChartPage)
def get_bars(
    snapshot_id: str,
    request: Request,
    timeframe: str = Query(..., description="K 线周期"),
    limit: int = Query(300, ge=1, le=1000),
    before_ts: int | None = Query(None, ge=0, description="只返回早于该时间戳的柱"),
) -> ChartPage:
    return get_runtime(request).workbench.get_bars(
        snapshot_id,
        timeframe=timeframe,
        limit=limit,
        before_ts=before_ts,
    )


@router.get(
    "/api/snapshots/{snapshot_id}/derivatives", response_model=DerivativeBundle
)
def get_derivatives(
    snapshot_id: str,
    request: Request,
    timeframe: str = Query(..., description="K 线周期"),
) -> DerivativeBundle:
    return get_runtime(request).workbench.get_derivatives(snapshot_id, timeframe=timeframe)


@router.get("/api/indicator-defaults", response_model=IndicatorDefaultsResponse)
async def get_indicator_defaults(request: Request) -> IndicatorDefaultsResponse:
    defaults = await get_runtime(request).indicator_defaults.effective()
    return IndicatorDefaultsResponse(defaults=defaults)


@router.put("/api/indicator-defaults/{name}", response_model=IndicatorDefaultsResponse)
async def put_indicator_default(
    name: IndicatorName,
    payload: IndicatorParams,
    request: Request,
) -> IndicatorDefaultsResponse:
    service = get_runtime(request).indicator_defaults
    try:
        await service.set(name, payload)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc
    defaults = await service.effective()
    return IndicatorDefaultsResponse(defaults=defaults)


@router.delete(
    "/api/indicator-defaults/{name}", response_model=IndicatorDefaultsResponse
)
async def delete_indicator_default(
    name: IndicatorName, request: Request
) -> IndicatorDefaultsResponse:
    service = get_runtime(request).indicator_defaults
    await service.clear(name)
    defaults = await service.effective()
    return IndicatorDefaultsResponse(defaults=defaults)


@router.get(
    "/api/snapshots/{snapshot_id}/indicators", response_model=IndicatorBundle
)
async def get_indicators(
    snapshot_id: str,
    request: Request,
    timeframe: str = Query(..., description="K 线周期"),
    names: str = Query(..., description="逗号分隔的指标名，如 EMA,RSI"),
    limit: int = Query(300, ge=1, le=5000, description="返回最近 N 个点"),
    live: bool = Query(
        False, description="追加 WS 已接收的实时尾段（降级增量路径）"
    ),
) -> IndicatorBundle:
    indicators = [item.strip().upper() for item in names.split(",") if item.strip()]
    if not indicators:
        raise InvalidRequestError("names 至少要包含一个指标")
    runtime = get_runtime(request)
    workbench = runtime.workbench
    effective = await runtime.indicator_defaults.effective()
    params = {
        name.value: value for name, value in effective.items() if name.value in indicators
    }
    extra_bars = []
    supervisor = runtime.supervisor
    if live and supervisor is not None:
        # 冻结快照的最后时间戳之后才是实时尾段；WS 同 timestamp 的修订由
        # supervisor 去重后只返回最新一版。
        page = await run_in_threadpool(
            workbench.get_bars,
            snapshot_id,
            timeframe=timeframe,
            limit=1,
            before_ts=None,
        )
        after_ts = page.bars[-1].timestamp if page.bars else None
        extra_bars = supervisor.recent_bars(
            instrument_id=page.instrument_id,
            timeframe=timeframe,
            after_ts=after_ts,
        )
    return await run_in_threadpool(
        workbench.get_indicators,
        snapshot_id,
        timeframe=timeframe,
        names=indicators,
        limit=limit,
        params=params,
        extra_bars=extra_bars,
    )


@router.get(
    "/api/snapshots/{snapshot_id}/key-levels", response_model=KeyLevelSet
)
def get_key_levels(
    snapshot_id: str,
    request: Request,
    timeframe: str = Query(..., description="K 线周期"),
    categories: str | None = Query(
        None, description="逗号分隔的类别，如 STRUCTURE,SESSION"
    ),
    min_strength: float = Query(0.0, ge=0, le=1),
    order: str = Query("strength_desc", description="排序方式"),
    limit: int = Query(200, ge=1, le=500),
    evidence_class: str = Query(
        "ALL", description="OBSERVED | ESTIMATED | ALL"
    ),
    vwap_anchor: str = Query(
        "session", description="锚定 VWAP 锚点：snapshot | session | timestamp"
    ),
    vwap_anchor_ts: int | None = Query(None, ge=0),
    value_area_ratio: float = Query(0.70, gt=0, le=1),
) -> KeyLevelSet:
    parsed: list[KeyLevelCategory] = []
    if categories:
        for token in categories.split(","):
            value = token.strip().upper()
            if not value:
                continue
            try:
                parsed.append(KeyLevelCategory(value))
            except ValueError as exc:
                raise InvalidRequestError(f"未知的关键位类别：{value}") from exc
    evidence_token = evidence_class.strip().upper()
    if evidence_token not in {"ALL", "OBSERVED", "ESTIMATED"}:
        raise InvalidRequestError(f"未知的证据类别：{evidence_class}")
    evidence = (
        None if evidence_token == "ALL" else EvidenceClass(evidence_token)
    )
    anchor_token = vwap_anchor.strip().upper()
    try:
        anchor = VwapAnchor(anchor_token)
    except ValueError as exc:
        raise InvalidRequestError(f"未知的锚定 VWAP 锚点：{vwap_anchor}") from exc
    if anchor is VwapAnchor.TIMESTAMP and vwap_anchor_ts is None:
        raise InvalidRequestError("vwap_anchor=timestamp 时必须提供 vwap_anchor_ts")
    params = KeyLevelParams(
        vwap_anchor=anchor,
        vwap_anchor_ts=vwap_anchor_ts,
        value_area_ratio=value_area_ratio,
    )
    return get_runtime(request).workbench.get_key_levels(
        snapshot_id,
        timeframe=timeframe,
        categories=parsed or None,
        min_strength=min_strength,
        order=order,
        top_n=limit,
        params=params,
        evidence_class=evidence,
    )


@router.get("/api/key-levels/ignored", response_model=KeyLevelIgnoreList)
async def list_ignored_key_levels(
    request: Request,
    instrument_id: str = Query(..., description="标的"),
) -> KeyLevelIgnoreList:
    return await get_runtime(request).key_level_feedback.list(instrument_id)


@router.put("/api/key-levels/ignored", response_model=KeyLevelIgnoreList)
async def set_ignored_key_level(
    payload: KeyLevelIgnoreRequest, request: Request
) -> KeyLevelIgnoreList:
    return await get_runtime(request).key_level_feedback.set_ignored(payload)


@router.post("/api/key-levels/{level_id}/adopt", response_model=DrawingRecord)
async def adopt_key_level(
    level_id: str, payload: KeyLevelAdoptionRequest, request: Request
) -> DrawingRecord:
    if payload.timeframe not in SUPPORTED_TIMEFRAMES:
        raise InvalidRequestError(f"不支持的周期 {payload.timeframe!r}")
    level_set = get_runtime(request).workbench.get_key_levels(
        payload.snapshot_id, timeframe=payload.timeframe
    )
    level = next(
        (item for item in level_set.levels if item.level_id == level_id),
        None,
    )
    if level is None:
        raise NotFoundError(f"关键位 {level_id} 不存在")
    return await get_runtime(request).key_level_feedback.adopt(
        level,
        snapshot_id=payload.snapshot_id,
        timeframe=payload.timeframe,
    )


@router.get(
    "/api/key-levels/feedback", response_model=KeyLevelFeedbackSummary
)
async def key_level_feedback(
    request: Request,
    instrument_id: str = Query(..., description="标的"),
) -> KeyLevelFeedbackSummary:
    return await get_runtime(request).key_level_feedback.feedback(instrument_id)


@router.post("/api/operators/compute", response_model=IndicatorSeries)
def compute_operator(
    payload: OperatorRequest,
    request: Request,
    live: bool = Query(
        False, description="追加 WS 已接收的实时尾段（降级增量路径）"
    ),
) -> IndicatorSeries:
    workbench = get_runtime(request).workbench
    extra_bars = []
    supervisor = get_runtime(request).supervisor
    if live and supervisor is not None:
        page = workbench.get_bars(
            payload.snapshot_id,
            timeframe=payload.timeframe,
            limit=1,
            before_ts=None,
        )
        after_ts = page.bars[-1].timestamp if page.bars else None
        extra_bars = supervisor.recent_bars(
            instrument_id=page.instrument_id,
            timeframe=payload.timeframe,
            after_ts=after_ts,
        )
    return workbench.compute_operator(
        payload.snapshot_id,
        timeframe=payload.timeframe,
        operator=payload.operator,
        limit=payload.limit or 300,
        params=payload.params,
        extra_bars=extra_bars,
    )
