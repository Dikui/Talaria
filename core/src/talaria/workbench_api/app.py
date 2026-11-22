"""FastAPI 应用工厂：本地工作台快通道与静态 SPA 托管。"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from talaria import __version__
from talaria.foundation.errors import TalariaError
from talaria.foundation.snapshot import ErrorResponse
from talaria.workbench_api.routes import (
    alerts,
    analysis,
    annotations,
    bars,
    collaboration,
    derivatives,
    desk_ws,
    drawings,
    health,
    paper,
    scanner,
    static,
    strategy_ws,
    ticks,
    watchlist,
)
from talaria.workbench_api.runtime import build_runtime
from talaria.workbench_api.settings import AppSettings

AUTH_SCHEME = "Bearer"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "VALIDATION_ERROR": 422,
    "BARS_RANGE_TOO_LARGE": 400,
    "BARS_BACKFILL_TIMEOUT": 504,
    "NOT_FOUND": 404,
    "DRAFT_CONFLICT": 409,
    "INSTRUMENT_NOT_FOUND": 404,
    "SNAPSHOT_NOT_FOUND": 404,
    "DRAWING_NOT_FOUND": 404,
    "ALERT_NOT_FOUND": 404,
    "ANNOTATION_NOT_FOUND": 404,
    "SNAPSHOT_MISMATCH": 422,
    "EVIDENCE_EMPTY": 422,
    "EVIDENCE_UNRESOLVED": 422,
    "PRICE_INVALID": 422,
    "PRICE_NOT_TICK_ALIGNED": 422,
    "PRICE_OUT_OF_BOUNDS": 422,
    "TIME_BEFORE_SNAPSHOT": 422,
    "TIME_TOO_FAR_FUTURE": 422,
    "CONSULTATION_NOT_FOUND": 404,
    "BRIEFING_NOT_FOUND": 404,
    "FEED_UNAVAILABLE": 503,
    "NOT_IMPLEMENTED": 501,
    "FEATURE_DISABLED": 409,
    "SERVICE_LOCKED": 409,
    "INTERNAL": 500,
}


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(code=code, message=message).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=body)


def create_app(
    settings: AppSettings,
    collaboration_status: Callable[[], Mapping[str, object]] | None = None,
    collaboration_host: Callable[[], str | None] | None = None,
) -> FastAPI:
    token = settings.token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        runtime = await build_runtime(settings, collaboration_host=collaboration_host)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.aclose()

    app = FastAPI(title="Talaria 分析工作台", version=__version__, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["dsh-app://app"],
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=600,
    )
    app.state.token = token
    app.state.settings = settings
    app.state.collaboration_status = collaboration_status

    @app.middleware("http")
    async def require_bearer(request: Request, call_next):
        if request.url.path.startswith("/api") and request.method in WRITE_METHODS:
            header = request.headers.get("authorization", "")
            scheme, _, credential = header.partition(" ")
            if (
                scheme.lower() != AUTH_SCHEME.lower()
                or not credential
                or not secrets.compare_digest(credential, token)
            ):
                return _error(401, "UNAUTHORIZED", "写操作需要有效的 Bearer token")
        return await call_next(request)

    @app.exception_handler(TalariaError)
    async def talaria_error_handler(request: Request, exc: TalariaError) -> JSONResponse:
        status = ERROR_STATUS.get(exc.code, 500)
        return _error(status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        parts = []
        for error in exc.errors():
            location = ".".join(str(item) for item in error.get("loc", ()))
            parts.append(f"{location}: {error.get('msg', '参数错误')}")
        return _error(422, "VALIDATION_ERROR", "；".join(parts) or "参数错误")

    app.include_router(health.router)
    app.include_router(paper.router)
    app.include_router(analysis.router)
    app.include_router(bars.router)
    app.include_router(derivatives.router)
    app.include_router(ticks.router)
    app.include_router(drawings.router)
    app.include_router(watchlist.router)
    app.include_router(annotations.router)
    app.include_router(alerts.router)
    app.include_router(scanner.router)
    app.include_router(collaboration.router)
    app.include_router(desk_ws.router)
    app.include_router(strategy_ws.router)
    app.include_router(static.router)
    return app
