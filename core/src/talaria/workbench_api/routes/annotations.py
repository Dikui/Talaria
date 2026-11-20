"""智能体图表标注接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from talaria.foundation.annotations import (
    AgentAnnotationCreate,
    AgentAnnotationRecord,
    AnnotationAdoptResponse,
    AnnotationClearRequest,
    AnnotationClearResponse,
    AnnotationListResponse,
    AnnotationSessionsResponse,
)
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.post("/api/annotations", response_model=AgentAnnotationRecord, status_code=201)
async def create_annotation(
    payload: AgentAnnotationCreate, request: Request
) -> AgentAnnotationRecord:
    return await get_runtime(request).annotations.create_annotation(payload)


@router.get("/api/annotations", response_model=AnnotationListResponse)
async def list_annotations(
    request: Request,
    instrument_id: str | None = Query(default=None),
    snapshot_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> AnnotationListResponse:
    return await get_runtime(request).annotations.list_annotations(
        instrument_id=instrument_id,
        snapshot_id=snapshot_id,
        session_id=session_id,
        include_archived=include_archived,
    )


@router.get("/api/annotations/sessions", response_model=AnnotationSessionsResponse)
async def list_annotation_sessions(request: Request) -> AnnotationSessionsResponse:
    return await get_runtime(request).annotations.list_sessions()


@router.post(
    "/api/annotations/{annotation_id}/adopt",
    response_model=AnnotationAdoptResponse,
)
async def adopt_annotation(
    annotation_id: str, request: Request
) -> AnnotationAdoptResponse:
    return await get_runtime(request).annotations.adopt(annotation_id)


@router.post(
    "/api/annotations/{annotation_id}/dismiss",
    response_model=AgentAnnotationRecord,
)
async def dismiss_annotation(
    annotation_id: str, request: Request
) -> AgentAnnotationRecord:
    return await get_runtime(request).annotations.dismiss(annotation_id)


@router.post(
    "/api/annotations/clear",
    response_model=AnnotationClearResponse,
)
async def clear_annotations(
    payload: AnnotationClearRequest, request: Request
) -> AnnotationClearResponse:
    return await get_runtime(request).annotations.clear(payload)
