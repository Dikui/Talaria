"""数值扫描与简报接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi import HTTPException
from talaria.foundation.briefings import (
    BriefingConsultRequest,
    BriefingConsultResponse,
    BriefingListResponse,
    BriefingResultRequest,
    ScannerBriefing,
)
from talaria.foundation.rules import RuleOverlay
from talaria.foundation.scanner import (
    ScannerConditionKind,
    ScannerFocusResponse,
    ScannerScanRequest,
    ScannerScanResponse,
    ScannerSeverity,
    ScannerTriggerListResponse,
)
from talaria.foundation.snapshot import ErrorResponse
from talaria.workbench_api.scanner_service import HISTORY_LIMIT
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/scanner/triggers", response_model=ScannerTriggerListResponse)
def list_scanner_triggers(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    instrument_id: str | None = Query(default=None),
    kind: ScannerConditionKind | None = Query(default=None),
    severity: ScannerSeverity | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
) -> ScannerTriggerListResponse:
    scanner = get_runtime(request).require_scanner()
    matches = scanner.list_triggers(
        limit=HISTORY_LIMIT,
        instrument_id=instrument_id,
        kind=kind,
        severity=severity,
        snapshot_id=snapshot_id,
    )
    return ScannerTriggerListResponse(
        triggers=matches[:limit],
        total=len(matches),
        generated_ts=request.app.state.settings.now_ms(),
    )


@router.get("/api/scanner/focus", response_model=ScannerFocusResponse)
def get_scanner_focus(request: Request) -> ScannerFocusResponse:
    scanner = get_runtime(request).require_scanner()
    return scanner.focus(request.app.state.settings.now_ms())


@router.get(
    "/api/scanner/briefings", response_model=BriefingListResponse
)
async def list_scanner_briefings(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    unread_only: bool = Query(default=False),
) -> BriefingListResponse:
    service = get_runtime(request).briefings
    briefings = await service.list(limit=limit, unread_only=unread_only)
    unread = await service.unread_count()
    return BriefingListResponse(
        briefings=briefings,
        unread=unread,
        generated_ts=request.app.state.settings.now_ms(),
    )


@router.post(
    "/api/scanner/briefings/{briefing_id}/read",
    response_model=ScannerBriefing,
    responses={409: {"model": ErrorResponse, "description": "扫描功能已关闭"}},
)
async def mark_scanner_briefing_read(
    briefing_id: str, request: Request
) -> ScannerBriefing:
    runtime = get_runtime(request)
    runtime.require_scanner()
    return await runtime.briefings.mark_read(briefing_id)


@router.post(
    "/api/scanner/briefings/{briefing_id}/consult",
    response_model=BriefingConsultResponse,
)
async def consult_scanner_briefing(
    briefing_id: str,
    request: Request,
    payload: BriefingConsultRequest | None = None,
) -> BriefingConsultResponse:
    service = get_runtime(request).briefings
    return await service.consult_seed(
        briefing_id, payload.question if payload is not None else None
    )


@router.post(
    "/api/scanner/briefings/{briefing_id}/result",
    response_model=ScannerBriefing,
    responses={409: {"model": ErrorResponse, "description": "扫描功能已关闭"}},
)
async def submit_scanner_briefing_result(
    briefing_id: str,
    payload: BriefingResultRequest,
    request: Request,
) -> ScannerBriefing:
    runtime = get_runtime(request)
    runtime.require_scanner()
    return await runtime.briefings.submit_result(briefing_id, payload)


@router.post("/api/scanner/scan", response_model=ScannerScanResponse)
async def scan_now(
    request: Request, payload: ScannerScanRequest | None = None
) -> ScannerScanResponse:
    runtime = get_runtime(request)
    scanner = runtime.require_scanner()
    requested = (
        list(dict.fromkeys(payload.instrument_ids))
        if payload is not None and payload.instrument_ids is not None
        else scanner.universe
    )
    try:
        rule = payload.rule if payload is not None else None
        drawings = {}
        if rule is not None:
            for drawing_id in dict.fromkeys(
                node.drawing_id for node in rule.nodes() if isinstance(node, RuleOverlay)
            ):
                drawings[drawing_id] = await runtime.drawings.get_drawing(drawing_id)
        triggers = await scanner.scan(requested, rule=rule, drawing=drawings.get)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ScannerScanResponse(
        triggers=triggers,
        scanned_instruments=requested,
        generated_ts=request.app.state.settings.now_ms(),
    )
