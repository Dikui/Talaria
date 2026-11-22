"""工作台与实时行情健康探针。"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response
from talaria import __version__
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.feed import FeedHealthReport
from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/health", responses={200: {"headers": {
    DATA_DIR_HEADER: {"description": "数据目录标识", "schema": {"type": "string"}},
    NOW_MS_HEADER: {"description": "服务当前时刻（UTC 毫秒）", "schema": {"type": "integer"}},
}}})
def health(request: Request, response: Response) -> dict[str, object]:
    active = get_runtime(request).workbench
    settings = request.app.state.settings
    response.headers[DATA_DIR_HEADER] = data_dir_id(settings.data_dir)
    response.headers[NOW_MS_HEADER] = str(settings.now_ms())
    return {"ok": True, "version": __version__, "feed_mode": active.feed_mode}


@router.get("/api/feed/health", response_model=FeedHealthReport)
def feed_health(request: Request) -> FeedHealthReport:
    supervisor = get_runtime(request).supervisor
    if supervisor is None:
        raise FeedUnavailableError(
            "当前运行在离线样本模式，没有实时行情流；请用 --replay-events 或实盘模式启动"
        )
    return supervisor.health()
