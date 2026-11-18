"""FastAPI 应用工厂：本地看盘网关 + 静态 SPA 托管。

安全边界：
- 只由 CLI 绑定回环地址；
- 所有 ``/api`` 写操作必须带 ``Authorization: Bearer <token>``；
- ``/desk`` 通过 SameSite=Strict 的 cookie 下发 token，token 本身不用于 cookie 鉴权；
- 不启用 CORS。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from talaria import __version__
from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.drawings import (
    ChartView,
    DrawingClearResponse,
    DrawingCreate,
    DrawingListResponse,
    DrawingPatch,
    DrawingRecord,
    DrawingVersionList,
)
from talaria.foundation.errors import (
    FeedUnavailableError,
    InvalidRequestError,
    TalariaError,
)
from talaria.foundation.derivatives import DerivativeBundle
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedEvent,
    FeedHealthEvent,
    FeedHealthReport,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.indicators import (
    IndicatorBundle,
    IndicatorSeries,
    OperatorRequest,
)
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import (
    ChartPage,
    ErrorResponse,
    SnapshotMeta,
    SnapshotRequest,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.foundation.watchlist import (
    Watchlist,
    WatchlistGroupCreate,
    WatchlistGroupUpdate,
    WatchlistItemCreate,
    WatchlistItemOrder,
    WatchlistOrder,
)
from talaria.packs.crypto_perp import (
    CRYPTO_PERP_PACK,
    BybitLinearConnector,
    BybitPublicStream,
)
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore
from talaria.perception.hot_buffer import HotStreamBuffer
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore
from talaria.replay import ReplayFeed
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateWriterQueue
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler
from talaria.workbench_api.watchlist import WatchlistService

AUTH_SCHEME = "Bearer"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "VALIDATION_ERROR": 422,
    "NOT_FOUND": 404,
    "INSTRUMENT_NOT_FOUND": 404,
    "SNAPSHOT_NOT_FOUND": 404,
    "DRAWING_NOT_FOUND": 404,
    "FEED_UNAVAILABLE": 503,
    "NOT_IMPLEMENTED": 501,
    "INTERNAL": 500,
}
#: 单个 WebSocket 客户端的广播缓冲；慢客户端只丢自己的旧消息，不阻塞其他人。
WS_QUEUE_MAX = 256

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


def _ws_payload(event: FeedEvent) -> dict[str, object]:
    if isinstance(event, KlineEvent):
        return {"type": "bar", "data": event.model_dump(mode="json")}
    if isinstance(event, OrderBookEvent):
        return {"type": "book", "data": event.model_dump(mode="json")}
    if isinstance(event, DerivativeTickEvent):
        return {"type": "derivatives", "data": event.model_dump(mode="json")}
    return {"type": "health", "data": event.model_dump(mode="json")}


def _wanted(event: FeedEvent, subscriptions: set[tuple[str, str]]) -> bool:
    if isinstance(event, KlineEvent):
        return (event.instrument_id, event.timeframe) in subscriptions
    if isinstance(event, FeedHealthEvent):
        return (event.instrument_id, event.timeframe) in subscriptions
    return any(
        instrument_id == event.instrument_id for instrument_id, _ in subscriptions
    )


async def _send_loop(websocket: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    try:
        while True:
            await websocket.send_json(await queue.get())
    except Exception:  # 客户端断开或运行时已关闭，直接结束发送循环
        return


def create_app(settings: AppSettings) -> FastAPI:
    token = settings.token or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        connector = (
            FixtureConnector(settings.fixture_dir)
            if settings.fixture_dir is not None
            else BybitLinearConnector(settings.bybit_base_url)
        )
        instruments = CRYPTO_PERP_PACK.instruments()
        stream = None
        feed_mode: str | None = None
        if settings.replay_events is not None:
            stream = ReplayFeed(settings.replay_events, speed=settings.replay_speed)
            feed_mode = "replay"
        elif settings.fixture_dir is None:
            stream = BybitPublicStream(
                instruments=instruments, now_ms=settings.now_ms
            )
            feed_mode = "bybit"

        cold_store = ColdStore(settings.data_dir)
        cold_publishers = {
            (instrument.instrument_id, timeframe): ColdPublisher(
                cold_store, instrument=instrument, timeframe=timeframe
            )
            for instrument in instruments
            for timeframe in SUPPORTED_TIMEFRAMES
        }
        store = SnapshotStore(settings.data_dir)
        hot_buffer = (
            HotStreamBuffer(instruments=instruments, now_ms=settings.now_ms)
            if stream is not None
            else None
        )
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
            hot_bars=hot_buffer,
            cold_publishers=cold_publishers,
        )
        app.state.service = WorkbenchService(
            pack=CRYPTO_PERP_PACK,
            connector=connector,
            data_dir=settings.data_dir,
            snapshot_store=store,
            builder=builder,
            feed_mode=feed_mode,
        )
        app.state.ws_subscribers = {}
        supervisor: FeedSupervisor | None = None
        remove_hot_listener = None
        if stream is not None:
            supervisor = FeedSupervisor(
                stream=stream,
                # 回放文件自带恢复段，不依赖 REST 回填；实盘断流才走 REST。
                connector=None if feed_mode == "replay" else connector,
                instruments=instruments,
                feed_mode=feed_mode,
                now_ms=settings.now_ms,
                stale_after_ms=settings.stale_after_ms,
            )
            await supervisor.start()
            if hot_buffer is not None:
                remove_hot_listener = supervisor.add_listener(hot_buffer.ingest)
        app.state.supervisor = supervisor
        app.state.hot_buffer = hot_buffer
        app.state.cold_store = cold_store
        state_writer = StateWriterQueue(settings.data_dir, now_ms=settings.now_ms)
        await state_writer.start()
        app.state.state_writer = state_writer
        app.state.drawing_service = DrawingService(
            queue=state_writer, pack=CRYPTO_PERP_PACK, now_ms=settings.now_ms
        )
        scheduler = (
            RealtimeSubscriptionScheduler(
                supervisor, quota=settings.realtime_quota
            )
            if supervisor is not None
            else None
        )
        watchlist = WatchlistService(
            state_writer=state_writer,
            instruments=instruments,
            connector=connector,
            supervisor=supervisor,
            scheduler=scheduler,
            now_ms=settings.now_ms,
        )
        app.state.subscription_scheduler = scheduler
        app.state.watchlist = watchlist
        try:
            yield
        finally:
            if remove_hot_listener is not None:
                remove_hot_listener()
            if supervisor is not None:
                await supervisor.stop()
            close = getattr(connector, "close", None)
            if callable(close):
                close()
            store.close()
            await state_writer.close()
            cold_store.close()

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

    def drawings(request: Request) -> DrawingService:
        return request.app.state.drawing_service

    @app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        active = service(request)
        return {"ok": True, "version": __version__, "feed_mode": active.feed_mode}

    @app.get("/api/feed/health", response_model=FeedHealthReport)
    def feed_health(request: Request) -> FeedHealthReport:
        supervisor: FeedSupervisor | None = request.app.state.supervisor
        if supervisor is None:
            raise FeedUnavailableError(
                "当前运行在离线样本模式，没有实时行情流；请用 --replay-events 或实盘模式启动"
            )
        return supervisor.health()

    @app.get("/api/instruments", response_model=list[InstrumentSpec])
    def instruments(request: Request) -> list[InstrumentSpec]:
        return list(service(request).instruments())

    @app.get("/api/drawings", response_model=DrawingListResponse)
    async def list_drawings(
        request: Request,
        instrument_id: str = Query(..., description="标的 id"),
        timeframe: str = Query(..., description="当前 K 线周期"),
        from_ts: int | None = Query(None, ge=0, description="可视窗口左边界（UTC 毫秒）"),
        to_ts: int | None = Query(None, ge=0, description="可视窗口右边界（UTC 毫秒）"),
    ) -> DrawingListResponse:
        return await drawings(request).list_drawings(
            instrument_id, timeframe, from_ts=from_ts, to_ts=to_ts
        )

    @app.post("/api/drawings", response_model=DrawingRecord, status_code=201)
    async def create_drawing(
        payload: DrawingCreate, request: Request
    ) -> DrawingRecord:
        return await drawings(request).create_drawing(payload)

    @app.delete("/api/drawings", response_model=DrawingClearResponse)
    async def clear_drawings(
        request: Request,
        instrument_id: str = Query(..., description="要清理的标的 id"),
    ) -> DrawingClearResponse:
        cleared = await drawings(request).clear_drawings(instrument_id)
        return DrawingClearResponse(instrument_id=instrument_id, cleared=cleared)

    @app.get("/api/drawings/{drawing_id}", response_model=DrawingRecord)
    async def get_drawing(
        drawing_id: str,
        request: Request,
        version: int | None = Query(None, ge=1, description="读取指定历史版本"),
    ) -> DrawingRecord:
        return await drawings(request).get_drawing(drawing_id, version=version)

    @app.get(
        "/api/drawings/{drawing_id}/versions", response_model=DrawingVersionList
    )
    async def get_drawing_versions(
        drawing_id: str, request: Request
    ) -> DrawingVersionList:
        return await drawings(request).list_versions(drawing_id)

    @app.patch("/api/drawings/{drawing_id}", response_model=DrawingRecord)
    async def patch_drawing(
        drawing_id: str, payload: DrawingPatch, request: Request
    ) -> DrawingRecord:
        return await drawings(request).patch_drawing(drawing_id, payload)

    @app.delete("/api/drawings/{drawing_id}", response_model=DrawingRecord)
    async def delete_drawing(drawing_id: str, request: Request) -> DrawingRecord:
        return await drawings(request).delete_drawing(drawing_id)

    @app.post("/api/drawings/{drawing_id}/restore", response_model=DrawingRecord)
    async def restore_drawing(drawing_id: str, request: Request) -> DrawingRecord:
        return await drawings(request).restore_drawing(drawing_id)

    @app.get("/api/chart-view", response_model=ChartView)
    async def get_chart_view(
        request: Request,
        instrument_id: str = Query(..., description="标的 id"),
        timeframe: str = Query(..., description="K 线周期"),
        from_ts: int = Query(..., ge=0, description="可视时间窗左边界（UTC 毫秒）"),
        to_ts: int = Query(..., ge=0, description="可视时间窗右边界（UTC 毫秒）"),
        price_min: Decimal = Query(..., description="可视价格窗下界（十进制字符串）"),
        price_max: Decimal = Query(..., description="可视价格窗上界（十进制字符串）"),
    ) -> ChartView:
        return await drawings(request).get_chart_view(
            instrument_id,
            timeframe,
            from_ts=from_ts,
            to_ts=to_ts,
            price_min=price_min,
            price_max=price_max,
        )
    @app.get("/api/watchlist", response_model=Watchlist)
    async def get_watchlist(request: Request) -> Watchlist:
        return await request.app.state.watchlist.get_watchlist()

    @app.post("/api/watchlist/groups", response_model=Watchlist)
    async def create_watchlist_group(
        request: Request, payload: WatchlistGroupCreate
    ) -> Watchlist:
        return await request.app.state.watchlist.create_group(payload.name)

    @app.put("/api/watchlist/groups/order", response_model=Watchlist)
    async def order_watchlist_groups(
        request: Request, payload: WatchlistOrder
    ) -> Watchlist:
        return await request.app.state.watchlist.order_groups(payload.ids)

    @app.patch("/api/watchlist/groups/{group_id}", response_model=Watchlist)
    async def rename_watchlist_group(
        request: Request, group_id: str, payload: WatchlistGroupUpdate
    ) -> Watchlist:
        return await request.app.state.watchlist.rename_group(
            group_id, payload.name
        )

    @app.delete("/api/watchlist/groups/{group_id}", response_model=Watchlist)
    async def delete_watchlist_group(
        request: Request, group_id: str
    ) -> Watchlist:
        return await request.app.state.watchlist.delete_group(group_id)

    @app.post("/api/watchlist/items", response_model=Watchlist)
    async def add_watchlist_item(
        request: Request, payload: WatchlistItemCreate
    ) -> Watchlist:
        return await request.app.state.watchlist.add_item(
            payload.group_id, payload.instrument_id
        )

    @app.put("/api/watchlist/items/order", response_model=Watchlist)
    async def order_watchlist_items(
        request: Request, payload: WatchlistItemOrder
    ) -> Watchlist:
        return await request.app.state.watchlist.order_items(
            payload.group_id, payload.ids
        )

    @app.delete(
        "/api/watchlist/items/{instrument_id}",
        response_model=Watchlist,
    )
    async def remove_watchlist_item(
        request: Request, instrument_id: str
    ) -> Watchlist:
        return await request.app.state.watchlist.remove_item(instrument_id)

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

    @app.get(
        "/api/snapshots/{snapshot_id}/derivatives", response_model=DerivativeBundle
    )
    def get_derivatives(
        snapshot_id: str,
        request: Request,
        timeframe: str = Query(..., description="K 线周期"),
    ) -> DerivativeBundle:
        return service(request).get_derivatives(snapshot_id, timeframe=timeframe)

    @app.get(
        "/api/snapshots/{snapshot_id}/indicators", response_model=IndicatorBundle
    )
    def get_indicators(
        snapshot_id: str,
        request: Request,
        timeframe: str = Query(..., description="K 线周期"),
        names: str = Query(..., description="逗号分隔的指标名，如 EMA,RSI"),
        limit: int = Query(300, ge=1, le=5000, description="返回最近 N 个点"),
        live: bool = Query(
            False, description="追加 WS 已接收的实时尾段（降级增量路径）"
        ),
    ) -> IndicatorBundle:
        indicators = [item.strip().upper() for item in names.split(",") if item.strip()]
        if not indicators:
            raise InvalidRequestError("names 至少要包含一个指标")
        workbench = service(request)
        extra_bars = []
        supervisor: FeedSupervisor | None = request.app.state.supervisor
        if live and supervisor is not None:
            # 冻结快照的最后时间戳之后才是实时尾段；WS 同 timestamp 的修订由
            # supervisor 去重后只返回最新一版。
            page = workbench.get_bars(
                snapshot_id,
                timeframe=timeframe,
                limit=1,
                before_ts=None,
            )
            after_ts = page.bars[-1].timestamp if page.bars else None
            extra_bars = supervisor.recent_bars(
                instrument_id=page.instrument_id,
                timeframe=timeframe,
                after_ts=after_ts,
            )
        return workbench.get_indicators(
            snapshot_id,
            timeframe=timeframe,
            names=indicators,
            limit=limit,
            extra_bars=extra_bars,
        )

    @app.post("/api/operators/compute", response_model=IndicatorSeries)
    def compute_operator(
        payload: OperatorRequest,
        request: Request,
        live: bool = Query(
            False, description="追加 WS 已接收的实时尾段（降级增量路径）"
        ),
    ) -> IndicatorSeries:
        workbench = service(request)
        extra_bars = []
        supervisor: FeedSupervisor | None = request.app.state.supervisor
        if live and supervisor is not None:
            page = workbench.get_bars(
                payload.snapshot_id,
                timeframe=payload.timeframe,
                limit=1,
                before_ts=None,
            )
            after_ts = page.bars[-1].timestamp if page.bars else None
            extra_bars = supervisor.recent_bars(
                instrument_id=page.instrument_id,
                timeframe=payload.timeframe,
                after_ts=after_ts,
            )
        return workbench.compute_operator(
            payload.snapshot_id,
            timeframe=payload.timeframe,
            operator=payload.operator,
            limit=payload.limit or 300,
            params=payload.params,
            extra_bars=extra_bars,
        )

    @app.websocket("/ws/desk")
    async def desk_socket(websocket: WebSocket) -> None:
        await websocket.accept()
        supervisor: FeedSupervisor | None = websocket.app.state.supervisor
        await websocket.send_json(
            {
                "type": "hello",
                "feed_mode": websocket.app.state.service.feed_mode,
                "server_ts": settings.now_ms(),
                "live": supervisor is not None,
            }
        )
        if supervisor is None:
            try:
                while True:
                    await websocket.receive_text()
            except (WebSocketDisconnect, RuntimeError):
                return

        scheduler: RealtimeSubscriptionScheduler = (
            websocket.app.state.subscription_scheduler
        )
        watchlist: WatchlistService = websocket.app.state.watchlist
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        subscriptions: set[tuple[str, str]] = set()
        retainers: dict[tuple[str, str], int] = websocket.app.state.ws_subscribers

        async def rebalance_watchlist() -> None:
            with contextlib.suppress(TalariaError):
                await watchlist.rebalance()

        def listener(event: FeedEvent) -> None:
            if not _wanted(event, subscriptions):
                return
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            queue.put_nowait(_ws_payload(event))

        def retain(key: tuple[str, str]) -> None:
            retainers[key] = retainers.get(key, 0) + 1

        def release(key: tuple[str, str]) -> None:
            remaining = retainers.get(key, 0) - 1
            if remaining > 0:
                retainers[key] = remaining
            else:
                retainers.pop(key, None)

        remove_listener = supervisor.add_listener(listener)
        sender = asyncio.create_task(_send_loop(websocket, queue))
        try:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                op = message.get("op")
                instrument_id = str(message.get("instrument_id") or "")
                timeframe = str(message.get("timeframe") or "")
                key = (instrument_id, timeframe)
                if op == "subscribe":
                    if key in subscriptions:
                        continue
                    subscriptions.add(key)
                    try:
                        scheduler.acquire_chart(key)
                    except TalariaError as exc:
                        subscriptions.discard(key)
                        await websocket.send_json(
                            {
                                "type": "error",
                                "code": exc.code,
                                "message": exc.message,
                            }
                        )
                        continue
                    retain(key)
                    await rebalance_watchlist()
                elif op == "unsubscribe":
                    if key not in subscriptions:
                        continue
                    subscriptions.discard(key)
                    release(key)
                    scheduler.release_chart(key)
                    if retainers.get(key, 0) == 0:
                        await rebalance_watchlist()
                else:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "code": "VALIDATION_ERROR",
                            "message": "未知的订阅操作",
                        }
                    )
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            remove_listener()
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            for key in subscriptions:
                release(key)
                scheduler.release_chart(key)
            await rebalance_watchlist()

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
