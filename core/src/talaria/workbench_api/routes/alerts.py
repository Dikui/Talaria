"""盘面预警接口。"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from talaria.foundation.alerts import (
    AlertCreate,
    AlertListResponse,
    AlertPatch,
    AlertRecord,
    AlertStatus,
    AlertTriggerListResponse,
)
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/alerts", response_model=AlertListResponse)
async def list_alerts(
    request: Request,
    status: AlertStatus | None = Query(default=None, description="按状态过滤"),
) -> AlertListResponse:
    alerts = await get_runtime(request).alerts.list_alerts(status)
    return AlertListResponse(alerts=alerts)


@router.post("/api/alerts", response_model=AlertRecord, status_code=201)
async def create_alert(payload: AlertCreate, request: Request) -> AlertRecord:
    return await get_runtime(request).alerts.create_alert(payload)


@router.get("/api/alerts/triggers", response_model=AlertTriggerListResponse)
async def list_alert_triggers(
    request: Request,
    alert_id: str | None = Query(default=None),
    instrument_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> AlertTriggerListResponse:
    triggers = await get_runtime(request).alerts.list_triggers(
        alert_id=alert_id, instrument_id=instrument_id, limit=limit
    )
    return AlertTriggerListResponse(triggers=triggers)


@router.get("/api/alerts/{alert_id}", response_model=AlertRecord)
async def get_alert(alert_id: str, request: Request) -> AlertRecord:
    return await get_runtime(request).alerts.get_alert(alert_id)


@router.patch("/api/alerts/{alert_id}", response_model=AlertRecord)
async def patch_alert(
    alert_id: str, payload: AlertPatch, request: Request
) -> AlertRecord:
    return await get_runtime(request).alerts.patch_alert(alert_id, payload)


@router.delete("/api/alerts/{alert_id}", response_model=AlertRecord)
async def delete_alert(alert_id: str, request: Request) -> AlertRecord:
    return await get_runtime(request).alerts.delete_alert(alert_id)


@router.post("/api/alerts/{alert_id}/confirm", response_model=AlertRecord)
async def confirm_alert(alert_id: str, request: Request) -> AlertRecord:
    return await get_runtime(request).alerts.confirm_alert(alert_id)


@router.post("/api/alerts/{alert_id}/dismiss", response_model=AlertRecord)
async def dismiss_alert(alert_id: str, request: Request) -> AlertRecord:
    return await get_runtime(request).alerts.patch_alert(
        alert_id, AlertPatch(enabled=False)
    )
