"""常驻服务逐笔历史补数入口。"""

from datetime import date

from fastapi import APIRouter, Request, Response

from talaria.foundation.errors import (
    FeatureDisabledError, FeedUnavailableError, InstrumentNotFoundError, InvalidRequestError,
)
from talaria.foundation.snapshot import ErrorResponse
from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.post(
    "/api/trades/backfill",
    responses={
        200: {"headers": {DATA_DIR_HEADER: {"description": "数据目录标识", "schema": {"type": "string"}}}},
        503: {"model": ErrorResponse, "description": "逐笔历史补数失败，可以重试"},
    },
)
async def backfill_trades(request: Request, response: Response, instrument: str, day: date) -> int:
    """下载一个已结束的 UTC 日，返回新增的成交笔数。"""
    runtime = get_runtime(request)
    spec = next((row for row in runtime.workbench.instruments() if row.instrument_id == instrument), None)
    if spec is None:
        raise InstrumentNotFoundError(f"未知标的：{instrument}")
    if runtime.cold_publisher is None:
        raise FeatureDisabledError("冷库未启用")
    try:
        inserted = await runtime.cold_publisher.backfill_trades(spec, day)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc
    except Exception as exc:
        raise FeedUnavailableError(f"逐笔历史补数失败：{exc}") from exc
    response.headers[DATA_DIR_HEADER] = data_dir_id(request.app.state.settings.data_dir)
    return inserted
