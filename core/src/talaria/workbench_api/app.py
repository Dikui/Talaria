"""FastAPI 应用工厂：本地看盘网关 + 静态 SPA 托管。

安全边界：
- 只由 CLI 绑定回环地址；
- 所有 ``/api`` 写操作必须带 ``Authorization: Bearer <token>``；
- ``/desk`` 通过 SameSite=Strict 的 cookie 下发 token，token 本身不用于 cookie 鉴权；
- 不启用 CORS。
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from talaria import __version__
from talaria.foundation.errors import TalariaError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import ChartPage, ErrorResponse, SnapshotMeta, SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK, BybitLinearConnector
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.settings import AppSettings

AUTH_SCHEME = "Bearer"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "VALIDATION_ERROR": 422,
    "NOT_FOUND": 404,
    "INSTRUMENT_NOT_FOUND": 404,
    "SNAPSHOT_NOT_FOUND": 404,
    "FEED_UNAVAILABLE": 503,
    "NOT_IMPLEMENTED": 501,
    "INTERNAL": 500,
}

_MISSING_DIST_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Talaria 前端尚未构建</title>
<style>body{font-family:system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;line-height:1.7;color:#1f2328}code{background:#f3f4f6;padding:.15rem .35rem;border-radius:.25rem}</style>
</head><body>
<h1>前端构建产物缺失</h1>
<p>请先在仓库根目录执行 <code>pnpm -C dsh install &amp;&amp; pnpm -C dsh build</code>，然后刷新本页。</p>
<p>后端 API 仍可通过 <code>/api/health</code> 检查。</p>
</body></html>"""


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    body = ErrorResponse(code=code, message=message).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=body)


def _desk_response(index_path: Path, token: str) -> HTMLResponse:
    if not index_path.is_file():
        response = HTMLResponse(_MISSING_DIST_HTML, status_code=200)
    else:
        response = HTMLResponse(index_path.read_text(encoding="utf-8"))
    # HttpOnly 关闭：同源 JS 需要读取后放进 Authorization，cookie 本身不参与鉴权。
    response.set_cookie(
        "talaria_token",
        token,
        httponly=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(settings: AppSettings) -> FastAPI:
    token = settings.token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        connector = (
            FixtureConnector(settings.fixture_dir)
            if settings.fixture_dir is not None
            else BybitLinearConnector(settings.bybit_base_url)
        )
        store = SnapshotStore(settings.data_dir)
        builder = SnapshotBuilder(
            data_dir=settings.data_dir,
            connector=connector,
            raw_store=RawStore(settings.data_dir),
            snapshot_store=store,
            policy=BackfillPolicy(
                target_bars=settings.backfill_bars,
                max_pages=settings.max_pages,
            ),
            now_ms=settings.now_ms,
        )
        app.state.service = WorkbenchService(
            pack=CRYPTO_PERP_PACK,
            connector=connector,
            data_dir=settings.data_dir,
            snapshot_store=store,
            builder=builder,
        )
        try:
            yield
        finally:
            close = getattr(connector, "close", None)
            if callable(close):
                close()
            store.close()

    app = FastAPI(title="Talaria 分析工作台", version=__version__, lifespan=lifespan)
    app.state.token = token
    app.state.settings = settings

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

    def service(request: Request) -> WorkbenchService:
        return request.app.state.service

    @app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        active = service(request)
        return {"ok": True, "version": __version__, "feed_mode": active.feed_mode}

    @app.get("/api/instruments", response_model=list[InstrumentSpec])
    def instruments(request: Request) -> list[InstrumentSpec]:
        return list(service(request).instruments())

    @app.post("/api/snapshots", response_model=SnapshotMeta)
    def create_snapshot(payload: SnapshotRequest, request: Request) -> SnapshotMeta:
        return service(request).create_snapshot(payload)

    @app.get("/api/snapshots/{snapshot_id}", response_model=SnapshotMeta)
    def get_snapshot(snapshot_id: str, request: Request) -> SnapshotMeta:
        return service(request).get_snapshot(snapshot_id)

    @app.get("/api/snapshots/{snapshot_id}/bars", response_model=ChartPage)
    def get_bars(
        snapshot_id: str,
        request: Request,
        timeframe: str = Query(..., description="K 线周期"),
        limit: int = Query(300, ge=1, le=1000),
        before_ts: int | None = Query(None, ge=0, description="只返回早于该时间戳的柱"),
    ) -> ChartPage:
        return service(request).get_bars(
            snapshot_id,
            timeframe=timeframe,
            limit=limit,
            before_ts=before_ts,
        )

    dist_dir = settings.dist_dir
    index_path = dist_dir / "index.html"

    @app.get("/desk", response_class=HTMLResponse)
    @app.get("/desk/{path:path}", response_class=HTMLResponse)
    def desk(path: str = "") -> HTMLResponse:
        return _desk_response(index_path, token)

    @app.get("/assets/{asset_path:path}")
    def assets(asset_path: str):
        base = (dist_dir / "assets").resolve()
        candidate = (base / asset_path).resolve()
        if not candidate.is_relative_to(base) or not candidate.is_file():
            return HTMLResponse("资源不存在", status_code=404)
        return FileResponse(candidate)

    return app
