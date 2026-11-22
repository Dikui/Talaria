"""组装工作台服务与后台任务，不依赖 ASGI 应用。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.alerts import AlertRecord
from talaria.foundation.bars import CompactBar
from talaria.foundation.connector import MarketDataConnector, RawBar
from talaria.foundation.errors import FeatureDisabledError, TalariaError
from talaria.foundation.feed import FeedTransport
from talaria.foundation.key_levels import KeyLevel
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import SnapshotRequest
from talaria.foundation.timeframes import TIMEFRAME_MS
from talaria.packs import get_market_pack
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore
from talaria.perception.compaction import compact_series
from talaria.perception.hot_buffer import HotStreamBuffer
from talaria.perception.raw_store import RawStore
from talaria.perception.scanner.routing import TriggerRoutingPolicy
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore
from talaria.replay import ReplayFeed
from talaria.workbench_api.alert_service import AlertService
from talaria.workbench_api.annotation_service import AnnotationService
from talaria.workbench_api.briefing_service import BriefingService
from talaria.workbench_api.collaboration_service import CollaborationService
from talaria.workbench_api.desk_broadcast import DeskBroadcast
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.indicator_defaults import IndicatorDefaultsService
from talaria.workbench_api.key_level_feedback import KeyLevelFeedbackService
from talaria.workbench_api.scanner_service import ScannerService
from talaria.workbench_api.service import WorkbenchService
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateWriterQueue
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler
from talaria.workbench_api.usage_service import UsageService
from talaria.workbench_api.watchlist import WatchlistService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    workbench: WorkbenchService
    drawings: DrawingService
    annotations: AnnotationService
    collaborations: CollaborationService
    usage: UsageService
    briefings: BriefingService
    key_level_feedback: KeyLevelFeedbackService
    indicator_defaults: IndicatorDefaultsService
    alerts: AlertService | None
    scanner: ScannerService | None
    watchlist: WatchlistService
    supervisor: FeedSupervisor | None
    subscription_scheduler: RealtimeSubscriptionScheduler | None
    cold_store: ColdStore | None
    cold_publisher: ColdPublisher | None
    snapshot_store: SnapshotStore
    hot_buffer: HotStreamBuffer | None
    state_writer: StateWriterQueue
    desk: DeskBroadcast
    _resources: AsyncExitStack = field(repr=False)

    def require_alerts(self) -> AlertService:
        if self.alerts is None:
            raise FeatureDisabledError("预警功能已在数据目录 service.json 中关闭")
        return self.alerts

    def require_scanner(self) -> ScannerService:
        if self.scanner is None:
            raise FeatureDisabledError("扫描功能已在数据目录 service.json 中关闭")
        return self.scanner

    async def aclose(self) -> None:
        """按启动的逆序停止任务、排空状态写入并关闭存储。"""
        await self._resources.aclose()


async def build_runtime(
    settings: AppSettings,
    *,
    collaboration_host: Callable[[], str | None] | None = None,
) -> Runtime:
    async with AsyncExitStack() as resources:
        pack = get_market_pack(settings.pack)
        features = settings.features
        connector = None
        if features.market_data:
            connector = (
                FixtureConnector(settings.fixture_dir)
                if settings.fixture_dir is not None
                else pack.create_history_connector(base_url=settings.history_base_url)
            )
            close_connector = getattr(connector, "close", None)
            if callable(close_connector):
                resources.callback(close_connector)
        instruments = pack.instruments()
        instruments_by_id = {spec.instrument_id: spec for spec in instruments}

        stream: FeedTransport | None = None
        if features.market_data:
            if settings.replay_events is not None:
                stream = ReplayFeed(settings.replay_events, speed=settings.replay_speed)
            elif settings.fixture_dir is None:
                stream = pack.create_realtime_stream(now_ms=settings.now_ms)
        hot_buffer = (
            HotStreamBuffer(instruments=instruments, now_ms=settings.now_ms)
            if stream is not None
            else None
        )
        policy = BackfillPolicy(target_bars=settings.backfill_bars, max_pages=settings.max_pages)
        supervisor: FeedSupervisor | None = None
        cold_store: ColdStore | None = None
        cold_publisher: ColdPublisher | None = None
        if features.cold_store:
            try:
                cold_store = ColdStore(settings.data_dir)
            except Exception as exc:
                logger.warning("冷库初始化失败：%s", exc)
            else:
                resources.callback(cold_store.close)
                cold_publisher = ColdPublisher(
                    cold_store, instruments=instruments, connector=connector,
                    policy=policy, now_ms=settings.now_ms, hot_buffer=hot_buffer,
                    has_channel=lambda instrument_id, timeframe: (
                        supervisor is not None and supervisor.has_channel(instrument_id, timeframe)
                    ),
                )
                resources.push_async_callback(cold_publisher.close)
                compactor = asyncio.create_task(
                    cold_publisher.compact_periodically(), name="talaria-cold-compaction",
                )
                resources.push_async_callback(_stop_task, compactor)
        snapshot_store = SnapshotStore(settings.data_dir)
        resources.callback(snapshot_store.close)
        if stream is not None:
            supervisor = FeedSupervisor(
                stream=stream,
                # 回放文件自带恢复段，不依赖 REST 回填；实盘断流才走 REST。
                connector=None if settings.replay_events is not None else connector,
                instruments=instruments,
                now_ms=settings.now_ms,
                stale_after_ms=settings.stale_after_ms,
                on_bar_close=cold_publisher.record_close if cold_publisher else None,
                on_recovered_bar=cold_publisher.record_recovered_bar if cold_publisher else None,
                on_subscribe=(
                    cold_publisher.backfill_subscription
                    if cold_publisher is not None and settings.replay_events is None else None
                ),
            )
            resources.push_async_callback(supervisor.stop)

        raw_store = RawStore(settings.data_dir, raw_archive_id=pack.raw_archive_id)
        builder = SnapshotBuilder(
            data_dir=settings.data_dir,
            connector=connector,
            raw_store=raw_store,
            snapshot_store=snapshot_store,
            policy=policy,
            now_ms=settings.now_ms,
            hot_bars=hot_buffer,
        )
        workbench = WorkbenchService(
            pack=pack,
            connector=connector,
            data_dir=settings.data_dir,
            snapshot_store=snapshot_store,
            builder=builder,
            cold_publisher=cold_publisher,
            feed_mode=stream.feed_mode if stream is not None else None,
        )
        desk = DeskBroadcast()
        if supervisor is not None:
            await supervisor.start()
            if hot_buffer is not None:
                resources.callback(supervisor.add_listener(hot_buffer.ingest))

        state_writer = StateWriterQueue(settings.data_dir, now_ms=settings.now_ms)
        resources.push_async_callback(state_writer.close)
        await state_writer.start()
        drawings = DrawingService(queue=state_writer, pack=pack, now_ms=settings.now_ms)
        annotations = AnnotationService(
            queue=state_writer,
            workbench=workbench,
            drawings=drawings,
            pack=pack,
            now_ms=settings.now_ms,
        )
        host_url = collaboration_host or (lambda: None)
        collaborations = CollaborationService(
            queue=state_writer,
            workbench=workbench,
            drawings=drawings,
            market_skill=pack.market_skill,
            host_url=host_url,
            now_ms=settings.now_ms,
        )
        resources.push_async_callback(collaborations.close)
        usage = UsageService(state_writer)
        briefings = BriefingService(
            queue=state_writer,
            usage=usage,
            workbench=workbench,
            host_url=host_url,
            now_ms=settings.now_ms,
            publish=desk.publish_briefing,
        )
        resources.push_async_callback(briefings.close)
        key_level_feedback = KeyLevelFeedbackService(
            state_writer=state_writer, drawings=drawings, now_ms=settings.now_ms
        )
        indicator_defaults = IndicatorDefaultsService(state_writer)
        scheduler = (
            RealtimeSubscriptionScheduler(supervisor, quota=settings.realtime_quota)
            if supervisor is not None
            else None
        )

        async def rebalance_watchlist() -> None:
            with suppress(TalariaError):
                await watchlist.rebalance()

        def freeze_snapshot(instrument_id: str, timeframe: str, now: int) -> str:
            meta = builder.build(
                instrument=instruments_by_id[instrument_id],
                request=SnapshotRequest(
                    instrument_id=instrument_id, timeframes=[timeframe], cutoff_ts=now
                ),
            )
            return meta.snapshot_id

        def freeze_alert_snapshot(alert: AlertRecord, now: int) -> str:
            return freeze_snapshot(alert.instrument_id, alert.timeframe, now)

        def bar_history(instrument_id: str, timeframe: str):
            assert supervisor is not None
            return supervisor.recent_bars(instrument_id=instrument_id, timeframe=timeframe)

        async def session_history(instrument_id: str, timeframe: str) -> list[CompactBar]:
            now = settings.now_ms()
            day_ms = TIMEFRAME_MS["1d"]
            session_start = now // day_ms * day_ms
            live = bar_history(instrument_id, timeframe)
            if live and live[0].timestamp < session_start <= live[-1].timestamp:
                return []
            return await asyncio.to_thread(
                read_alert_session_history,
                connector,
                symbol=instruments_by_id[instrument_id].symbol,
                timeframe=timeframe,
                cutoff_ts=now,
            )

        def alert_key_level(
            instrument_id: str, timeframe: str, level_id: str, cutoff_ts: int
        ) -> KeyLevel | None:
            """用最近一次快照已冻结的关键位求值，避免引擎按实时窗口重算出不同 id。"""
            level_set = desk.levels_for((instrument_id, timeframe))
            if level_set is None:
                return None
            level = next((item for item in level_set.levels if item.level_id == level_id), None)
            if level is None or level.ts_available > cutoff_ts:
                return None
            return level

        alerts: AlertService | None = None
        if features.alerts:
            alerts = AlertService(
                state_writer=state_writer,
                drawings=drawings,
                instruments=instruments,
                now_ms=settings.now_ms,
                bar_history=bar_history if supervisor is not None else None,
                session_history=(
                    session_history
                    if supervisor is not None and settings.replay_events is None
                    else None
                ),
                key_level_provider=alert_key_level,
                freeze_snapshot=freeze_alert_snapshot,
                on_change=rebalance_watchlist,
                on_trigger=desk.publish_alert,
                indicator_defaults=indicator_defaults,
            )
        watchlist = WatchlistService(
            state_writer=state_writer,
            instruments=instruments,
            connector=connector,
            raw_store=raw_store if not features.market_data else None,
            supervisor=supervisor,
            scheduler=scheduler,
            now_ms=settings.now_ms,
            alerted_instruments=(
                alerts.active_instrument_ids if alerts is not None else (lambda: ())
            ),
        )
        scanner: ScannerService | None = None
        if features.scanner:
            scanner = ScannerService(
                state_writer=state_writer,
                instruments=instruments,
                now_ms=settings.now_ms,
                freeze_snapshot=freeze_snapshot,
                publish=desk.publish_scanner,
                on_change=rebalance_watchlist,
                routing=TriggerRoutingPolicy(co_pilot=briefings, co_pilot_enabled=True),
            )
            resources.callback(scanner.close)
            if isinstance(connector, FixtureConnector) and stream is None:
                _seed_fixture_scanner(scanner, connector, instruments)
            if supervisor is not None:
                resources.callback(supervisor.add_listener(scanner.handle_event))
            scanner_ticker = asyncio.create_task(
                _scanner_tick_loop(scanner), name="talaria-scanner-tick"
            )
            resources.push_async_callback(_stop_task, scanner_ticker)

        if alerts is not None:
            resources.push_async_callback(alerts.close)
            await alerts.start()
            if supervisor is not None:
                resources.callback(supervisor.add_listener(alerts.handle_event))
            alert_ticker = asyncio.create_task(
                _alert_tick_loop(alerts), name="talaria-alert-tick"
            )
            resources.push_async_callback(_stop_task, alert_ticker)
        await watchlist.rebalance()

        return Runtime(
            workbench=workbench,
            drawings=drawings,
            annotations=annotations,
            collaborations=collaborations,
            usage=usage,
            briefings=briefings,
            key_level_feedback=key_level_feedback,
            indicator_defaults=indicator_defaults,
            alerts=alerts,
            scanner=scanner,
            watchlist=watchlist,
            supervisor=supervisor,
            subscription_scheduler=scheduler,
            cold_store=cold_store,
            cold_publisher=cold_publisher,
            snapshot_store=snapshot_store,
            hot_buffer=hot_buffer,
            state_writer=state_writer,
            desk=desk,
            _resources=resources.pop_all(),
        )


def read_alert_session_history(
    connector: MarketDataConnector, *, symbol: str, timeframe: str, cutoff_ts: int
) -> list[CompactBar]:
    """Page closed bars through the current UTC session and its preceding bar."""
    interval = TIMEFRAME_MS[timeframe]
    day_ms = TIMEFRAME_MS["1d"]
    start = max(0, cutoff_ts // day_ms * day_ms - interval)
    end = cutoff_ts - interval
    raw: list[RawBar] = []
    while end >= start:
        page = connector.fetch_bars(
            symbol=symbol,
            timeframe=timeframe,
            end_ts=end,
            limit=min(1000, (end - start) // interval + 1),
        )
        if not page:
            break
        raw.extend(bar for bar in page if start <= bar.ts_event <= end)
        end = page[0].ts_event - 1
    return compact_series(raw, timeframe=timeframe, cutoff_ts=cutoff_ts)


def _seed_fixture_scanner(
    scanner: ScannerService,
    connector: FixtureConnector,
    instruments: Sequence[InstrumentSpec],
) -> None:
    """离线样本没有实时流；预先回填一轮供演示与验收回看异动。"""
    for spec in instruments:
        try:
            raw_bars = connector.fetch_bars(symbol=spec.symbol, timeframe="1m", limit=240)
            scanner.seed_bars(
                spec.instrument_id, "1m", [bar.to_compact() for bar in raw_bars]
            )
        except Exception:
            logger.warning("扫描器回填 K 线失败：%s", spec.instrument_id)
        points = []
        try:
            points.extend(connector.fetch_funding_settlements(symbol=spec.symbol, limit=120))
        except Exception:
            logger.warning("扫描器回填资金费率失败：%s", spec.instrument_id)
        try:
            points.extend(
                connector.fetch_open_interest(symbol=spec.symbol, timeframe="5m", limit=120)
            )
        except Exception:
            logger.warning("扫描器回填持仓量失败：%s", spec.instrument_id)
        if points:
            scanner.seed_derivatives(
                spec.instrument_id,
                [point.to_point(spec.instrument_id) for point in points],
            )


async def _stop_task(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _alert_tick_loop(alert_service: AlertService) -> None:
    while True:
        await asyncio.sleep(1)
        try:
            await alert_service.tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("预警冷却/过期推进失败")


async def _scanner_tick_loop(scanner_service: ScannerService) -> None:
    while True:
        await asyncio.sleep(3)
        try:
            await scanner_service.scan()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("数值扫描 tick 失败")
