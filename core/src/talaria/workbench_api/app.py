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
import logging
import secrets
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import AsyncIterator, Callable, Mapping

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from talaria import __version__
from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.alerts import (
    AlertCreate,
    AlertListResponse,
    AlertPatch,
    AlertRecord,
    AlertStatus,
    AlertTriggerListResponse,
    AlertTriggerRecord,
)
from talaria.foundation.annotations import (
    AgentAnnotationCreate,
    AgentAnnotationRecord,
    AnnotationAdoptResponse,
    AnnotationClearRequest,
    AnnotationClearResponse,
    AnnotationListResponse,
    AnnotationSessionsResponse,
)
from talaria.foundation.collaboration import (
    ConsultationListResponse,
    ConsultationRecord,
    ConsultRequest,
    ConsultResponse,
    HostResultRequest,
    UsageReport,
    UsageReportResponse,
    UsageSummaryResponse,
)
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
    NotFoundError,
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
from talaria.foundation.key_levels import (
    EvidenceClass,
    KeyLevel,
    KeyLevelAdoptionRequest,
    KeyLevelCategory,
    KeyLevelFeedbackSummary,
    KeyLevelIgnoreList,
    KeyLevelIgnoreRequest,
    KeyLevelParams,
    KeyLevelSet,
    VwapAnchor,
)
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.scanner import (
    ScannerConditionKind,
    ScannerFocusResponse,
    ScannerScanRequest,
    ScannerScanResponse,
    ScannerSeverity,
    ScannerTrigger,
    ScannerTriggerListResponse,
)
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
from talaria.workbench_api.alert_service import AlertService
from talaria.workbench_api.annotation_service import AnnotationService
from talaria.workbench_api.collaboration_service import CollaborationService
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.key_level_feedback import KeyLevelFeedbackService
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.scanner_service import HISTORY_LIMIT, ScannerService
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateWriterQueue
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler
from talaria.workbench_api.watchlist import WatchlistService

logger = logging.getLogger(__name__)

AUTH_SCHEME = "Bearer"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "VALIDATION_ERROR": 422,
    "NOT_FOUND": 404,
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


def _ws_levels_payload(level_set: KeyLevelSet) -> dict[str, object]:
    return {"type": "levels", "data": level_set.model_dump(mode="json")}


def _enqueue(queue: asyncio.Queue[dict[str, object]], payload: dict[str, object]) -> None:
    """队列满时丢弃最旧事件，保证关键位投影不会阻塞行情帧。"""
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()
    queue.put_nowait(payload)


def _broadcast_levels(app: FastAPI, key: tuple[str, str], level_set: KeyLevelSet) -> None:
    """把新的关键位集合单向投影给订阅了该标的/周期的连接。"""
    payload = _ws_levels_payload(level_set)
    for websocket, filters in list(app.state.ws_filters.items()):
        if key not in filters:
            continue
        queue = app.state.ws_queues.get(websocket)
        if queue is None:
            continue
        _enqueue(queue, payload)


async def _send_loop(websocket: WebSocket, queue: asyncio.Queue[dict[str, object]]) -> None:
    try:
        while True:
            await websocket.send_json(await queue.get())
    except Exception:  # 客户端断开或运行时已关闭，直接结束发送循环
        return


def _broadcast_alert(app: FastAPI, trigger: AlertTriggerRecord) -> None:
    payload = {"type": "alert", "data": trigger.model_dump(mode="json")}
    for queue in list(app.state.ws_queues.values()):
        _enqueue(queue, payload)


async def _alert_tick_loop(alert_service: AlertService) -> None:
    while True:
        await asyncio.sleep(1)
        try:
            await alert_service.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("预警冷却/过期推进失败")


def _broadcast_scanner(app: FastAPI, trigger: ScannerTrigger) -> None:
    payload = {"type": "scanner", "data": trigger.model_dump(mode="json")}
    for queue in list(app.state.ws_queues.values()):
        _enqueue(queue, payload)


async def _scanner_tick_loop(scanner_service: ScannerService) -> None:
    while True:
        await asyncio.sleep(3)
        try:
            await scanner_service.scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("数值扫描 tick 失败")


def create_app(
    settings: AppSettings,
    collaboration_status: Callable[[], Mapping[str, object]] | None = None,
    collaboration_host: Callable[[], str | None] | None = None,
) -> FastAPI:
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
        instruments_by_id = {spec.instrument_id: spec for spec in instruments}
        app.state.ws_subscribers = {}
        app.state.level_sets = {}
        app.state.ws_queues = {}
        app.state.ws_filters = {}
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
        app.state.annotation_service = AnnotationService(
            queue=state_writer,
            workbench=app.state.service,
            drawings=app.state.drawing_service,
            pack=CRYPTO_PERP_PACK,
            now_ms=settings.now_ms,
        )
        app.state.collaboration_service = CollaborationService(
            queue=state_writer,
            workbench=app.state.service,
            drawings=app.state.drawing_service,
            host_url=collaboration_host or (lambda: None),
            now_ms=settings.now_ms,
        )
        app.state.key_level_feedback = KeyLevelFeedbackService(
            state_writer=state_writer,
            drawings=app.state.drawing_service,
            now_ms=settings.now_ms,
        )
        scheduler = (
            RealtimeSubscriptionScheduler(
                supervisor, quota=settings.realtime_quota
            )
            if supervisor is not None
            else None
        )

        async def _alert_changed() -> None:
            with contextlib.suppress(TalariaError):
                await watchlist.rebalance()

        def _freeze_alert_snapshot(alert: AlertRecord, now: int) -> str:
            meta = builder.build(
                instrument=instruments_by_id[alert.instrument_id],
                request=SnapshotRequest(
                    instrument_id=alert.instrument_id,
                    timeframes=[alert.timeframe],
                    cutoff_ts=now,
                ),
            )
            return meta.snapshot_id

        def bar_history(instrument_id: str, timeframe: str):
            assert supervisor is not None
            return supervisor.recent_bars(
                instrument_id=instrument_id, timeframe=timeframe
            )

        def alert_key_level(
            instrument_id: str, timeframe: str, level_id: str, cutoff_ts: int
        ) -> KeyLevel | None:
            """用最近一次快照已冻结的关键位求值，避免引擎按实时窗口重算出不同 id。"""

            level_set = app.state.level_sets.get((instrument_id, timeframe))
            if level_set is None:
                return None
            level = next(
                (item for item in level_set.levels if item.level_id == level_id),
                None,
            )
            if level is None or level.ts_available > cutoff_ts:
                return None
            return level

        alert_service = AlertService(
            state_writer=state_writer,
            instruments=instruments,
            now_ms=settings.now_ms,
            bar_history=bar_history if supervisor is not None else None,
            key_level_provider=alert_key_level,
            freeze_snapshot=_freeze_alert_snapshot,
            on_change=_alert_changed,
            on_trigger=lambda trigger: _broadcast_alert(app, trigger),
        )
        watchlist = WatchlistService(
            state_writer=state_writer,
            instruments=instruments,
            connector=connector,
            supervisor=supervisor,
            scheduler=scheduler,
            now_ms=settings.now_ms,
            alerted_instruments=alert_service.active_instrument_ids,
        )
        app.state.subscription_scheduler = scheduler
        app.state.watchlist = watchlist

        async def _scanner_changed() -> None:
            with contextlib.suppress(TalariaError):
                await watchlist.rebalance()

        def _freeze_scanner_snapshot(
            instrument_id: str, timeframe: str, now: int
        ) -> str:
            meta = builder.build(
                instrument=instruments_by_id[instrument_id],
                request=SnapshotRequest(
                    instrument_id=instrument_id,
                    timeframes=[timeframe],
                    cutoff_ts=now,
                ),
            )
            return meta.snapshot_id

        scanner_service = ScannerService(
            state_writer=state_writer,
            instruments=instruments,
            now_ms=settings.now_ms,
            freeze_snapshot=_freeze_scanner_snapshot,
            publish=lambda trigger: _broadcast_scanner(app, trigger),
            on_change=_scanner_changed,
        )
        if settings.fixture_dir is not None and feed_mode is None:
            # 离线样本没有实时流；启动时用同一 connector 回填一轮，扫描与 UI
            # 才能在没有网络的演示/验收环境里产出可回看的异动。
            for spec in instruments:
                try:
                    raw_bars = connector.fetch_bars(
                        symbol=spec.symbol, timeframe="1m", limit=240
                    )
                    scanner_service.seed_bars(
                        spec.instrument_id,
                        "1m",
                        [bar.to_compact() for bar in raw_bars],
                    )
                except Exception:
                    logger.warning("扫描器回填 K 线失败：%s", spec.instrument_id)
                points = []
                try:
                    points.extend(
                        connector.fetch_funding_settlements(
                            symbol=spec.symbol, limit=120
                        )
                    )
                except Exception:
                    logger.warning("扫描器回填资金费率失败：%s", spec.instrument_id)
                try:
                    points.extend(
                        connector.fetch_open_interest(
                            symbol=spec.symbol, timeframe="5m", limit=120
                        )
                    )
                except Exception:
                    logger.warning("扫描器回填持仓量失败：%s", spec.instrument_id)
                if points:
                    scanner_service.seed_derivatives(
                        spec.instrument_id,
                        [point.to_point(spec.instrument_id) for point in points],
                    )
        app.state.scanner_service = scanner_service
        remove_scanner_listener = (
            supervisor.add_listener(scanner_service.handle_event)
            if supervisor is not None
            else None
        )
        scanner_ticker = asyncio.create_task(_scanner_tick_loop(scanner_service))
        await alert_service.start()
        app.state.alert_service = alert_service
        remove_alert_listener = (
            supervisor.add_listener(alert_service.handle_event)
            if supervisor is not None
            else None
        )
        alert_ticker = asyncio.create_task(_alert_tick_loop(alert_service))
        await watchlist.rebalance()
        try:
            yield
        finally:
            scanner_ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scanner_ticker
            alert_ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await alert_ticker
            if remove_scanner_listener is not None:
                remove_scanner_listener()
            scanner_service.close()
            if remove_alert_listener is not None:
                remove_alert_listener()
            await alert_service.close()
            await app.state.collaboration_service.close()
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
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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

    def service(request: Request) -> WorkbenchService:
        return request.app.state.service

    def drawings(request: Request) -> DrawingService:
        return request.app.state.drawing_service

    def collaborations(request: Request) -> CollaborationService:
        return request.app.state.collaboration_service

    def annotations(request: Request) -> AnnotationService:
        return request.app.state.annotation_service

    @app.get("/api/health")
    def health(request: Request) -> dict[str, object]:
        active = service(request)
        return {"ok": True, "version": __version__, "feed_mode": active.feed_mode}

    @app.get("/api/collaboration")
    def collaboration(request: Request) -> Mapping[str, object]:
        provider = request.app.state.collaboration_status
        if provider is None:
            return {
                "mode": "solo",
                "host": "disabled",
                "url": None,
                "detail": "单机模式未启动 DSH 协同宿主",
                "message": "单机模式：图表由 Python 守护进程直接提供，不依赖 DSH 宿主。",
            }
        return provider()

    @app.post(
        "/api/collaboration/consult",
        response_model=ConsultResponse,
        status_code=202,
    )
    async def consult(
        payload: ConsultRequest, request: Request
    ) -> ConsultResponse:
        return await collaborations(request).consult(payload)

    @app.get(
        "/api/collaboration/consultations",
        response_model=ConsultationListResponse,
    )
    async def list_consultations(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
    ) -> ConsultationListResponse:
        return ConsultationListResponse(
            consultations=await collaborations(request).list(limit=limit)
        )

    @app.get(
        "/api/collaboration/consultations/{consultation_id}",
        response_model=ConsultationRecord,
    )
    async def get_consultation(
        consultation_id: str, request: Request
    ) -> ConsultationRecord:
        return await collaborations(request).get(consultation_id)

    @app.post(
        "/api/collaboration/consultations/{consultation_id}/result",
        response_model=ConsultationRecord,
    )
    async def submit_consultation_result(
        consultation_id: str,
        payload: HostResultRequest,
        request: Request,
    ) -> ConsultationRecord:
        return await collaborations(request).submit_result(consultation_id, payload)

    @app.get(
        "/api/collaboration/usage",
        response_model=UsageSummaryResponse,
    )
    async def collaboration_usage(
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
    ) -> UsageSummaryResponse:
        return await collaborations(request).usage(limit=limit)

    @app.post(
        "/api/collaboration/usage",
        response_model=UsageReportResponse,
    )
    async def report_collaboration_usage(
        payload: UsageReport, request: Request
    ) -> UsageReportResponse:
        return await collaborations(request).report_usage(payload)

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

    @app.post("/api/annotations", response_model=AgentAnnotationRecord, status_code=201)
    async def create_annotation(
        payload: AgentAnnotationCreate, request: Request
    ) -> AgentAnnotationRecord:
        return await annotations(request).create_annotation(payload)

    @app.get("/api/annotations", response_model=AnnotationListResponse)
    async def list_annotations(
        request: Request,
        instrument_id: str | None = Query(default=None),
        snapshot_id: str | None = Query(default=None),
        session_id: str | None = Query(default=None),
        include_archived: bool = Query(default=False),
    ) -> AnnotationListResponse:
        return await annotations(request).list_annotations(
            instrument_id=instrument_id,
            snapshot_id=snapshot_id,
            session_id=session_id,
            include_archived=include_archived,
        )

    @app.get("/api/annotations/sessions", response_model=AnnotationSessionsResponse)
    async def list_annotation_sessions(request: Request) -> AnnotationSessionsResponse:
        return await annotations(request).list_sessions()

    @app.post(
        "/api/annotations/{annotation_id}/adopt",
        response_model=AnnotationAdoptResponse,
    )
    async def adopt_annotation(
        annotation_id: str, request: Request
    ) -> AnnotationAdoptResponse:
        return await annotations(request).adopt(annotation_id)

    @app.post(
        "/api/annotations/{annotation_id}/dismiss",
        response_model=AgentAnnotationRecord,
    )
    async def dismiss_annotation(
        annotation_id: str, request: Request
    ) -> AgentAnnotationRecord:
        return await annotations(request).dismiss(annotation_id)

    @app.post(
        "/api/annotations/clear",
        response_model=AnnotationClearResponse,
    )
    async def clear_annotations(
        payload: AnnotationClearRequest, request: Request
    ) -> AnnotationClearResponse:
        return await annotations(request).clear(payload)

    @app.get("/api/alerts", response_model=AlertListResponse)
    async def list_alerts(
        request: Request,
        status: AlertStatus | None = Query(default=None, description="按状态过滤"),
    ) -> AlertListResponse:
        alerts = await request.app.state.alert_service.list_alerts(status)
        return AlertListResponse(alerts=alerts)

    @app.post("/api/alerts", response_model=AlertRecord, status_code=201)
    async def create_alert(payload: AlertCreate, request: Request) -> AlertRecord:
        return await request.app.state.alert_service.create_alert(payload)

    @app.get("/api/alerts/triggers", response_model=AlertTriggerListResponse)
    async def list_alert_triggers(
        request: Request,
        alert_id: str | None = Query(default=None),
        instrument_id: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> AlertTriggerListResponse:
        triggers = await request.app.state.alert_service.list_triggers(
            alert_id=alert_id, instrument_id=instrument_id, limit=limit
        )
        return AlertTriggerListResponse(triggers=triggers)

    @app.get("/api/alerts/{alert_id}", response_model=AlertRecord)
    async def get_alert(alert_id: str, request: Request) -> AlertRecord:
        return await request.app.state.alert_service.get_alert(alert_id)

    @app.patch("/api/alerts/{alert_id}", response_model=AlertRecord)
    async def patch_alert(
        alert_id: str, payload: AlertPatch, request: Request
    ) -> AlertRecord:
        return await request.app.state.alert_service.patch_alert(alert_id, payload)

    @app.delete("/api/alerts/{alert_id}", response_model=AlertRecord)
    async def delete_alert(alert_id: str, request: Request) -> AlertRecord:
        return await request.app.state.alert_service.delete_alert(alert_id)

    @app.post("/api/alerts/{alert_id}/confirm", response_model=AlertRecord)
    async def confirm_alert(alert_id: str, request: Request) -> AlertRecord:
        return await request.app.state.alert_service.confirm_alert(alert_id)

    @app.post("/api/alerts/{alert_id}/dismiss", response_model=AlertRecord)
    async def dismiss_alert(alert_id: str, request: Request) -> AlertRecord:
        return await request.app.state.alert_service.patch_alert(
            alert_id, AlertPatch(enabled=False)
        )

    @app.get("/api/scanner/triggers", response_model=ScannerTriggerListResponse)
    def list_scanner_triggers(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        instrument_id: str | None = Query(default=None),
        kind: ScannerConditionKind | None = Query(default=None),
        severity: ScannerSeverity | None = Query(default=None),
        snapshot_id: str | None = Query(default=None),
    ) -> ScannerTriggerListResponse:
        scanner: ScannerService = request.app.state.scanner_service
        matches = scanner.list_triggers(
            limit=HISTORY_LIMIT,
            instrument_id=instrument_id,
            kind=kind,
            severity=severity,
            snapshot_id=snapshot_id,
        )
        return ScannerTriggerListResponse(
            triggers=matches[:limit],
            total=len(matches),
            generated_ts=request.app.state.settings.now_ms(),
        )

    @app.get("/api/scanner/focus", response_model=ScannerFocusResponse)
    def get_scanner_focus(request: Request) -> ScannerFocusResponse:
        scanner: ScannerService = request.app.state.scanner_service
        return scanner.focus(request.app.state.settings.now_ms())

    @app.post("/api/scanner/scan", response_model=ScannerScanResponse)
    async def scan_now(
        request: Request, payload: ScannerScanRequest | None = None
    ) -> ScannerScanResponse:
        scanner: ScannerService = request.app.state.scanner_service
        requested = (
            list(dict.fromkeys(payload.instrument_ids))
            if payload is not None and payload.instrument_ids is not None
            else scanner.universe
        )
        try:
            triggers = await scanner.scan(requested)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ScannerScanResponse(
            triggers=triggers,
            scanned_instruments=requested,
            generated_ts=request.app.state.settings.now_ms(),
        )

    @app.post("/api/snapshots", response_model=SnapshotMeta)
    async def create_snapshot(
        payload: SnapshotRequest, request: Request
    ) -> SnapshotMeta:
        workbench = service(request)
        meta = workbench.create_snapshot(payload)
        for timeframe in meta.timeframes:
            level_set = workbench.get_key_levels(
                meta.snapshot_id, timeframe=timeframe
            )
            key: tuple[str, str] = (meta.instrument_id, timeframe)
            request.app.state.level_sets[key] = level_set
            _broadcast_levels(request.app, key, level_set)
        return meta

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

    @app.get(
        "/api/snapshots/{snapshot_id}/key-levels", response_model=KeyLevelSet
    )
    def get_key_levels(
        snapshot_id: str,
        request: Request,
        timeframe: str = Query(..., description="K 线周期"),
        categories: str | None = Query(
            None, description="逗号分隔的类别，如 STRUCTURE,SESSION"
        ),
        min_strength: float = Query(0.0, ge=0, le=1),
        order: str = Query("strength_desc", description="排序方式"),
        limit: int = Query(200, ge=1, le=500),
        evidence_class: str = Query(
            "ALL", description="OBSERVED | ESTIMATED | ALL"
        ),
        vwap_anchor: str = Query(
            "session", description="锚定 VWAP 锚点：snapshot | session | timestamp"
        ),
        vwap_anchor_ts: int | None = Query(None, ge=0),
        value_area_ratio: float = Query(0.70, gt=0, le=1),
    ) -> KeyLevelSet:
        parsed: list[KeyLevelCategory] = []
        if categories:
            for token in categories.split(","):
                value = token.strip().upper()
                if not value:
                    continue
                try:
                    parsed.append(KeyLevelCategory(value))
                except ValueError as exc:
                    raise InvalidRequestError(f"未知的关键位类别：{value}") from exc
        evidence_token = evidence_class.strip().upper()
        if evidence_token not in {"ALL", "OBSERVED", "ESTIMATED"}:
            raise InvalidRequestError(f"未知的证据类别：{evidence_class}")
        evidence = (
            None if evidence_token == "ALL" else EvidenceClass(evidence_token)
        )
        anchor_token = vwap_anchor.strip().upper()
        try:
            anchor = VwapAnchor(anchor_token)
        except ValueError as exc:
            raise InvalidRequestError(f"未知的锚定 VWAP 锚点：{vwap_anchor}") from exc
        if anchor is VwapAnchor.TIMESTAMP and vwap_anchor_ts is None:
            raise InvalidRequestError("vwap_anchor=timestamp 时必须提供 vwap_anchor_ts")
        params = KeyLevelParams(
            vwap_anchor=anchor,
            vwap_anchor_ts=vwap_anchor_ts,
            value_area_ratio=value_area_ratio,
        )
        return service(request).get_key_levels(
            snapshot_id,
            timeframe=timeframe,
            categories=parsed or None,
            min_strength=min_strength,
            order=order,
            top_n=limit,
            params=params,
            evidence_class=evidence,
        )

    @app.get("/api/key-levels/ignored", response_model=KeyLevelIgnoreList)
    async def list_ignored_key_levels(
        request: Request,
        instrument_id: str = Query(..., description="标的"),
    ) -> KeyLevelIgnoreList:
        return await request.app.state.key_level_feedback.list(instrument_id)

    @app.put("/api/key-levels/ignored", response_model=KeyLevelIgnoreList)
    async def set_ignored_key_level(
        payload: KeyLevelIgnoreRequest, request: Request
    ) -> KeyLevelIgnoreList:
        return await request.app.state.key_level_feedback.set_ignored(payload)

    @app.post("/api/key-levels/{level_id}/adopt", response_model=DrawingRecord)
    async def adopt_key_level(
        level_id: str, payload: KeyLevelAdoptionRequest, request: Request
    ) -> DrawingRecord:
        if payload.timeframe not in SUPPORTED_TIMEFRAMES:
            raise InvalidRequestError(f"不支持的周期 {payload.timeframe!r}")
        level_set = service(request).get_key_levels(
            payload.snapshot_id, timeframe=payload.timeframe
        )
        level = next(
            (item for item in level_set.levels if item.level_id == level_id),
            None,
        )
        if level is None:
            raise NotFoundError(f"关键位 {level_id} 不存在")
        return await request.app.state.key_level_feedback.adopt(
            level,
            snapshot_id=payload.snapshot_id,
            timeframe=payload.timeframe,
        )

    @app.get(
        "/api/key-levels/feedback", response_model=KeyLevelFeedbackSummary
    )
    async def key_level_feedback(
        request: Request,
        instrument_id: str = Query(..., description="标的"),
    ) -> KeyLevelFeedbackSummary:
        return await request.app.state.key_level_feedback.feedback(instrument_id)

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
        app = websocket.app
        supervisor: FeedSupervisor | None = app.state.supervisor
        await websocket.send_json(
            {
                "type": "hello",
                "feed_mode": app.state.service.feed_mode,
                "server_ts": settings.now_ms(),
                "live": supervisor is not None,
            }
        )
        scheduler: RealtimeSubscriptionScheduler | None = (
            app.state.subscription_scheduler
        )
        watchlist: WatchlistService = app.state.watchlist
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=WS_QUEUE_MAX)
        subscriptions: set[tuple[str, str]] = set()
        retainers: dict[tuple[str, str], int] = app.state.ws_subscribers
        app.state.ws_queues[websocket] = queue
        app.state.ws_filters[websocket] = subscriptions

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

        remove_listener = (
            supervisor.add_listener(listener) if supervisor is not None else None
        )
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
                    if scheduler is not None:
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
                    else:
                        valid_ids = {
                            spec.instrument_id for spec in app.state.service.instruments()
                        }
                        if instrument_id not in valid_ids:
                            subscriptions.discard(key)
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "code": "INSTRUMENT_NOT_FOUND",
                                    "message": f"未知标的：{instrument_id}",
                                }
                            )
                            continue
                        if timeframe not in SUPPORTED_TIMEFRAMES:
                            subscriptions.discard(key)
                            await websocket.send_json(
                                {
                                    "type": "error",
                                    "code": "VALIDATION_ERROR",
                                    "message": f"不支持的周期：{timeframe}",
                                }
                            )
                            continue
                    if supervisor is not None:
                        retain(key)
                        await rebalance_watchlist()
                    cached = app.state.level_sets.get(key)
                    if cached is not None:
                        _enqueue(queue, _ws_levels_payload(cached))
                elif op == "unsubscribe":
                    if key not in subscriptions:
                        continue
                    subscriptions.discard(key)
                    if supervisor is not None:
                        release(key)
                    if scheduler is not None:
                        scheduler.release_chart(key)
                    if supervisor is not None and retainers.get(key, 0) == 0:
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
            app.state.ws_queues.pop(websocket, None)
            app.state.ws_filters.pop(websocket, None)
            if remove_listener is not None:
                remove_listener()
            sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
            for key in subscriptions:
                if supervisor is not None:
                    release(key)
                if scheduler is not None:
                    scheduler.release_chart(key)
            if supervisor is not None:
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
