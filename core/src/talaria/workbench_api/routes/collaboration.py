"""协同宿主状态、会诊与用量接口。"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Query, Request
from talaria.foundation.collaboration import (
    ConsultRequest,
    ConsultResponse,
    ConsultationListResponse,
    ConsultationRecord,
    HostResultRequest,
    UsageReport,
    UsageReportResponse,
    UsageSummaryResponse,
)
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/collaboration")
def collaboration(request: Request) -> Mapping[str, object]:
    provider = request.app.state.collaboration_status
    if provider is None:
        return {
            "mode": "solo",
            "host": "disabled",
            "url": None,
            "detail": "单机模式未启动 DSH 协同宿主",
            "message": "单机模式：图表由 Python 守护进程直接提供，不依赖 DSH 宿主。",
        }
    return provider()


@router.post(
    "/api/collaboration/consult",
    response_model=ConsultResponse,
    status_code=202,
)
async def consult(
    payload: ConsultRequest, request: Request
) -> ConsultResponse:
    return await get_runtime(request).collaborations.consult(payload)


@router.get(
    "/api/collaboration/consultations",
    response_model=ConsultationListResponse,
)
async def list_consultations(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
) -> ConsultationListResponse:
    return ConsultationListResponse(
        consultations=await get_runtime(request).collaborations.list(limit=limit)
    )


@router.get(
    "/api/collaboration/consultations/{consultation_id}",
    response_model=ConsultationRecord,
)
async def get_consultation(
    consultation_id: str, request: Request
) -> ConsultationRecord:
    return await get_runtime(request).collaborations.get(consultation_id)


@router.post(
    "/api/collaboration/consultations/{consultation_id}/result",
    response_model=ConsultationRecord,
)
async def submit_consultation_result(
    consultation_id: str,
    payload: HostResultRequest,
    request: Request,
) -> ConsultationRecord:
    return await get_runtime(request).collaborations.submit_result(consultation_id, payload)


@router.get(
    "/api/collaboration/usage",
    response_model=UsageSummaryResponse,
)
async def collaboration_usage(
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
) -> UsageSummaryResponse:
    return await get_runtime(request).usage.summary(limit=limit)


@router.post(
    "/api/collaboration/usage",
    response_model=UsageReportResponse,
)
async def report_collaboration_usage(
    payload: UsageReport, request: Request
) -> UsageReportResponse:
    return await get_runtime(request).usage.report(payload)
