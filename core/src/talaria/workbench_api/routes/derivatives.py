"""由常驻服务回填 SDK 请求的衍生品范围。"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from talaria.foundation.derivatives import OI_TIMEFRAME_BY_TIMEFRAME, DerivativeKind
from talaria.foundation.errors import BarsBackfillTimeoutError, FeedUnavailableError, InstrumentNotFoundError
from talaria.foundation.snapshot import ErrorResponse
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id
from talaria.workbench_api.routes import get_runtime
from talaria.workbench_api.routes.bars import HISTORY_TIMEOUT_SECONDS

router = APIRouter()


@router.get(
    "/api/derivatives/backfill", status_code=204,
    responses={
        204: {"headers": {
            DATA_DIR_HEADER: {"description": "数据目录标识", "schema": {"type": "string"}},
            NOW_MS_HEADER: {"description": "回填后服务时刻（UTC 毫秒）", "schema": {"type": "integer"}},
        }},
        503: {"model": ErrorResponse, "description": "结算重同步失败，可以重试"},
        504: {"model": ErrorResponse, "description": "等待衍生品历史回填超时，可以重试"},
    },
)
async def backfill_derivatives(
    request: Request, instrument: str, kind: DerivativeKind,
    start: Annotated[int, Query(ge=0)], end: Annotated[int, Query(ge=0)],
    as_of: Annotated[int | None, Query(ge=0)] = None,
    refresh: bool = False,
) -> Response:
    runtime = get_runtime(request)
    settings = request.app.state.settings
    spec = next((row for row in runtime.workbench.instruments() if row.instrument_id == instrument), None)
    if spec is None:
        raise InstrumentNotFoundError(f"未知标的：{instrument}")
    timeframes = {
        DerivativeKind.FUNDING_RATE: (None,),
        DerivativeKind.MARK_PRICE: tuple(sorted(SUPPORTED_TIMEFRAMES)),
        DerivativeKind.OPEN_INTEREST: tuple(sorted(set(OI_TIMEFRAME_BY_TIMEFRAME.values()))),
    }
    publisher = runtime.cold_publisher
    if publisher is not None and (settings.replay_events is None or settings.fixture_dir is not None):
        now = settings.now_ms()
        future = publisher.request_derivative_range(
            spec, kind, timeframes[kind], start=start, end=end,
            cutoff=now if as_of is None else min(now, as_of),
            refresh=refresh and kind is DerivativeKind.FUNDING_RATE,
        )
        if future is not None:
            try:
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), timeout=HISTORY_TIMEOUT_SECONDS)
            except TimeoutError as exc:
                raise BarsBackfillTimeoutError("等待衍生品历史回填超时，请重试") from exc
            except Exception as exc:
                raise FeedUnavailableError("衍生品历史回填失败，请重试") from exc
    return Response(status_code=204, headers={
        DATA_DIR_HEADER: data_dir_id(settings.data_dir), NOW_MS_HEADER: str(settings.now_ms()),
    })
