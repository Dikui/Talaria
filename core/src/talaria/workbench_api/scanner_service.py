"""扫描器的装配层：Tick、去重、焦点持久化、路由与广播。

扫描本身是纯同步数值计算（``ScannerEngine``）；本服务只负责把它接到
SQLite 状态队列、WebSocket 广播和 08 的 Watchlist 分组上。它不导入模型
客户端，也不会因为路由占位通道而发起网络请求。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable, Sequence

from talaria.foundation.bars import CompactBar
from talaria.foundation.derivatives import DerivativePoint
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.scanner.contracts import (
    ScannerConditionKind,
    ScannerFocusItem,
    ScannerFocusResponse,
    ScannerSeverity,
    ScannerTrigger,
    ScannerTriggerRoute,
)
from talaria.perception.scanner.engine import KIND_ORDER, ScannerConfig, ScannerEngine
from talaria.perception.scanner.routing import TriggerRoutingPolicy
from talaria.workbench_api.state_store import (
    SCANNER_FOCUS_GROUP_ID,
    StateStore,
    StateWriterQueue,
)

logger = logging.getLogger(__name__)

SCANNER_FOCUS_NAME = "扫描焦点"
HISTORY_LIMIT = 200
SEVERITY_RANK: dict[ScannerSeverity, int] = {
    ScannerSeverity.INFO: 0,
    ScannerSeverity.WARNING: 1,
    ScannerSeverity.CRITICAL: 2,
}


class ScannerService:
    """按固定节拍扫描 Watchlist，并将命中写入本地工作台。"""

    def __init__(
        self,
        *,
        state_writer: StateWriterQueue,
        instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int],
        freeze_snapshot: Callable[[str, str, int], str],
        publish: Callable[[ScannerTrigger], None] | None = None,
        on_change: Callable[[], object] | None = None,
        config: ScannerConfig | None = None,
        routing: TriggerRoutingPolicy | None = None,
        focus_name: str = SCANNER_FOCUS_NAME,
    ) -> None:
        self._state_writer = state_writer
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._publish = publish
        self._on_change = on_change
        self._routing = routing or TriggerRoutingPolicy()
        self._focus_name = focus_name
        self._engine = ScannerEngine(
            now_ms=now_ms,
            config=config,
            freeze_snapshot=freeze_snapshot,
        )
        self._history: deque[ScannerTrigger] = deque(maxlen=HISTORY_LIMIT)
        self._lock = asyncio.Lock()
        self._last_focus_ids: tuple[str, ...] | None = None

    # ------------------------------------------------------------------ 生命周期

    @property
    def engine(self) -> ScannerEngine:
        return self._engine

    @property
    def universe(self) -> list[str]:
        return sorted(self._instruments)

    def seed_bars(
        self,
        instrument_id: str,
        timeframe: str,
        bars: Iterable[CompactBar],
    ) -> None:
        self._engine.seed_bars(instrument_id, timeframe, bars)

    def seed_derivatives(
        self,
        instrument_id: str,
        points: Iterable[DerivativePoint],
    ) -> None:
        self._engine.seed_derivatives(instrument_id, points)

    def handle_event(self, event: object) -> None:
        """可直接注册为 ``FeedSupervisor`` 监听器的同步入口。"""

        self._engine.handle_event(event)

    # ------------------------------------------------------------------ 扫描

    async def scan(
        self,
        instrument_ids: Sequence[str] | None = None,
    ) -> list[ScannerTrigger]:
        """扫描一轮；同一时刻的并发调用串行化，避免重复冻结。"""

        async with self._lock:
            now = self._now_ms()
            requested = (
                list(dict.fromkeys(instrument_ids))
                if instrument_ids is not None
                else self.universe
            )
            unknown = [
                instrument_id
                for instrument_id in requested
                if instrument_id not in self._instruments
            ]
            if unknown:
                raise ValueError(f"未知标的：{', '.join(unknown)}")
            triggers = self._engine.scan(requested, now=now)
            routed: list[ScannerTrigger] = []
            for trigger in triggers:
                routed.append(self._apply_route(trigger))
            for trigger in routed:
                self._history.append(trigger)
                if self._publish is not None:
                    try:
                        self._publish(trigger)
                    except Exception:
                        logger.exception("广播扫描异动失败：%s", trigger.trigger_id)
            await self._sync_focus(now)
            return routed

    async def scan_now(
        self, instrument_ids: Sequence[str] | None = None
    ) -> list[ScannerTrigger]:
        return await self.scan(instrument_ids)

    def _apply_route(self, trigger: ScannerTrigger) -> ScannerTrigger:
        try:
            route = self._routing.route(trigger)
        except Exception:
            logger.exception("协同会诊路由占位失败，降级为本地提醒")
            route = ScannerTriggerRoute.LOCAL
        return trigger if route is trigger.route else trigger.model_copy(
            update={"route": route}
        )

    async def _sync_focus(self, now: int) -> None:
        ids = tuple(self._engine.active_focus(now))
        if ids == self._last_focus_ids:
            return

        def _apply(store: StateStore) -> None:
            # 空焦点且尚未建组时不要凭空创建“扫描焦点”分组，否则首次启动的
            # 空自选引导会被一个空系统分组顶掉。已经存在的分组继续保留。
            if not ids and store.get_watchlist_group(SCANNER_FOCUS_GROUP_ID) is None:
                return
            store.upsert_system_group(self._focus_name, list(ids))

        await self._state_writer.run(_apply)
        self._last_focus_ids = ids
        if self._on_change is not None:
            try:
                result = self._on_change()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("扫描焦点变化后刷新自选池失败")

    # ------------------------------------------------------------------ 查询

    def list_triggers(
        self,
        *,
        limit: int = 50,
        instrument_id: str | None = None,
        kind: ScannerConditionKind | None = None,
        severity: ScannerSeverity | None = None,
        snapshot_id: str | None = None,
    ) -> list[ScannerTrigger]:
        if limit <= 0:
            return []
        selected = [
            trigger
            for trigger in reversed(self._history)
            if (instrument_id is None or trigger.instrument_id == instrument_id)
            and (kind is None or trigger.kind is kind)
            and (severity is None or trigger.severity is severity)
            and (snapshot_id is None or trigger.snapshot_id == snapshot_id)
        ]
        return selected[: min(limit, HISTORY_LIMIT)]

    def focus(self, now: int | None = None) -> ScannerFocusResponse:
        timestamp = self._now_ms() if now is None else now
        active_ids = self._engine.active_focus(timestamp)
        by_instrument: dict[str, list[ScannerTrigger]] = {}
        for trigger in self._history:
            if timestamp - trigger.created_ts < self._engine.config.focus_ttl_ms:
                by_instrument.setdefault(trigger.instrument_id, []).append(trigger)

        items: list[ScannerFocusItem] = []
        for instrument_id in active_ids:
            triggers = by_instrument.get(instrument_id, [])
            if not triggers:
                continue
            kinds = [
                kind
                for kind in KIND_ORDER
                if any(trigger.kind is kind for trigger in triggers)
            ]
            latest_trigger = max(
                triggers, key=lambda trigger: (trigger.created_ts, trigger.trigger_id)
            )
            latest = latest_trigger.created_ts
            items.append(
                ScannerFocusItem(
                    instrument_id=instrument_id,
                    last_trigger_ts=latest,
                    trigger_count=len(triggers),
                    severity=max(
                        (trigger.severity for trigger in triggers),
                        key=lambda value: SEVERITY_RANK[value],
                    ),
                    kinds=kinds,
                    last_title=latest_trigger.title,
                    last_timeframe=latest_trigger.timeframe,
                    last_snapshot_id=latest_trigger.snapshot_id,
                )
            )
        items.sort(key=lambda item: (-item.last_trigger_ts, item.instrument_id))
        return ScannerFocusResponse(
            group_id=SCANNER_FOCUS_GROUP_ID,
            name=self._focus_name,
            items=items,
            instrument_ids=[item.instrument_id for item in items],
            generated_ts=timestamp,
        )

    def close(self) -> None:
        self._history.clear()
