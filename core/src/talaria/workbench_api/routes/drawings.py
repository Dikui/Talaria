"""人工画线与图表视窗接口。"""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Query, Request
from talaria.foundation.drawings import (
    ChartView,
    DrawingClearResponse,
    DrawingCreate,
    DrawingListResponse,
    DrawingPatch,
    DrawingRecord,
    DrawingVersionList,
)
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/drawings", response_model=DrawingListResponse)
async def list_drawings(
    request: Request,
    instrument_id: str = Query(..., description="标的 id"),
    timeframe: str = Query(..., description="当前 K 线周期"),
    from_ts: int | None = Query(None, ge=0, description="可视窗口左边界（UTC 毫秒）"),
    to_ts: int | None = Query(None, ge=0, description="可视窗口右边界（UTC 毫秒）"),
) -> DrawingListResponse:
    return await get_runtime(request).drawings.list_drawings(
        instrument_id, timeframe, from_ts=from_ts, to_ts=to_ts
    )


@router.post("/api/drawings", response_model=DrawingRecord, status_code=201)
async def create_drawing(
    payload: DrawingCreate, request: Request
) -> DrawingRecord:
    return await get_runtime(request).drawings.create_drawing(payload)


@router.delete("/api/drawings", response_model=DrawingClearResponse)
async def clear_drawings(
    request: Request,
    instrument_id: str = Query(..., description="要清理的标的 id"),
) -> DrawingClearResponse:
    cleared = await get_runtime(request).drawings.clear_drawings(instrument_id)
    return DrawingClearResponse(instrument_id=instrument_id, cleared=cleared)


@router.get("/api/drawings/{drawing_id}", response_model=DrawingRecord)
async def get_drawing(
    drawing_id: str,
    request: Request,
    version: int | None = Query(None, ge=1, description="读取指定历史版本"),
) -> DrawingRecord:
    return await get_runtime(request).drawings.get_drawing(drawing_id, version=version)


@router.get(
    "/api/drawings/{drawing_id}/versions", response_model=DrawingVersionList
)
async def get_drawing_versions(
    drawing_id: str, request: Request
) -> DrawingVersionList:
    return await get_runtime(request).drawings.list_versions(drawing_id)


@router.patch("/api/drawings/{drawing_id}", response_model=DrawingRecord)
async def patch_drawing(
    drawing_id: str, payload: DrawingPatch, request: Request
) -> DrawingRecord:
    return await get_runtime(request).drawings.patch_drawing(drawing_id, payload)


@router.delete("/api/drawings/{drawing_id}", response_model=DrawingRecord)
async def delete_drawing(drawing_id: str, request: Request) -> DrawingRecord:
    return await get_runtime(request).drawings.delete_drawing(drawing_id)


@router.post("/api/drawings/{drawing_id}/restore", response_model=DrawingRecord)
async def restore_drawing(drawing_id: str, request: Request) -> DrawingRecord:
    return await get_runtime(request).drawings.restore_drawing(drawing_id)


@router.get("/api/chart-view", response_model=ChartView)
async def get_chart_view(
    request: Request,
    instrument_id: str = Query(..., description="标的 id"),
    timeframe: str = Query(..., description="K 线周期"),
    from_ts: int = Query(..., ge=0, description="可视时间窗左边界（UTC 毫秒）"),
    to_ts: int = Query(..., ge=0, description="可视时间窗右边界（UTC 毫秒）"),
    price_min: Decimal = Query(..., description="可视价格窗下界（十进制字符串）"),
    price_max: Decimal = Query(..., description="可视价格窗上界（十进制字符串）"),
) -> ChartView:
    return await get_runtime(request).drawings.get_chart_view(
        instrument_id,
        timeframe,
        from_ts=from_ts,
        to_ts=to_ts,
        price_min=price_min,
        price_max=price_max,
    )
