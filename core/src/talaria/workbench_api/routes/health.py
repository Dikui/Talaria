"""工作台与实时行情健康探针。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from talaria import __version__
from talaria.foundation.errors import FeedUnavailableError
from talaria.foundation.feed import FeedHealthReport
from talaria.workbench_api.routes import get_runtime

router = APIRouter()


@router.get("/api/health")
def health(request: Request) -> dict[str, object]:
    active = get_runtime(request).workbench
    return {"ok": True, "version": __version__, "feed_mode": active.feed_mode}


@router.get("/api/feed/health", response_model=FeedHealthReport)
def feed_health(request: Request) -> FeedHealthReport:
    supervisor = get_runtime(request).supervisor
    if supervisor is None:
        raise FeedUnavailableError(
            "当前运行在离线样本模式，没有实时行情流；请用 --replay-events 或实盘模式启动"
        )
    return supervisor.health()
