"""预警服务：契约校验、持久化编排与订阅优先级联动。

引擎只做纯逻辑求值；本服务负责把规则写入 ``StateWriterQueue``、冻结触发
证据、广播通知并让 08 调度器重新分配订阅配额。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any
from uuid import uuid4

from talaria.foundation.alerts import (
    AlertCreate,
    AlertPatch,
    AlertRecord,
    AlertStatus,
    AlertTriggerRecord,
    overlay_bounds,
)
from talaria.foundation.bars import CompactBar
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.errors import (
    InvalidRequestError,
    TalariaError,
)
from talaria.foundation.feed import FeedEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.rules import (
    Rule, RuleComparison, RuleIndicator,
    RuleDrawingPrice, RuleLevelEvent, RuleLevelPrice, RuleOverlay,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.perception.alerts import AlertEngine
from talaria.perception.alerts.engine import KeyLevelProvider
from talaria.perception.rule_derivatives import ArchiveReader
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.indicator_defaults import IndicatorDefaultsService, pin_rule_indicator_params
from talaria.workbench_api.state_store import StateStore, StateWriterQueue

logger = logging.getLogger(__name__)

_NOT_ALERTABLE_KINDS = frozenset({DrawingKind.TEXT_LABEL})

_ALERT_PARAMS_V1_KEY = "alerts.explicit_params.v1"


def _has_level_rule(record: AlertRecord) -> bool:
    return any(isinstance(node, (RuleLevelPrice, RuleLevelEvent)) for node in record.condition.references())


class AlertService:
    """预警的用例层；所有写操作都经单写队列串行提交。"""

    def __init__(
        self,
        *,
        state_writer: StateWriterQueue,
        drawings: DrawingService,
        instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int],
        bar_history: Callable[[str, str], Sequence[Any]] | None = None,
        read_archived_derivatives: ArchiveReader | None = None,
        session_history: Callable[[str, str], Awaitable[Sequence[CompactBar]]] | None = None,
        key_level_provider: KeyLevelProvider | None = None,
        freeze_snapshot: Callable[[AlertRecord, int], str] | None = None,
        on_change: Callable[[], Awaitable[None]] | None = None,
        on_trigger: Callable[[AlertTriggerRecord], None] | None = None,
        indicator_defaults: IndicatorDefaultsService | None = None,
    ) -> None:
        self._state_writer = state_writer
        self._indicator_defaults = indicator_defaults or IndicatorDefaultsService(
            state_writer
        )
        self._key_level_provider = key_level_provider
        self._drawings = drawings
        self._session_history = session_history
        self._session_history_seeded: set[tuple[str, str]] = set()
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._on_change = on_change
        self._on_trigger = on_trigger
        self._pending: set[asyncio.Task[Any]] = set()
        self._engine = AlertEngine(
            now_ms=now_ms,
            history_provider=bar_history,
            read_archived_derivatives=read_archived_derivatives,
            key_level_provider=key_level_provider,
            freeze_snapshot=freeze_snapshot,
            on_trigger=self._handle_trigger,
        )
        self._overlay_refresh_lock = asyncio.Lock()

    # ------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        await self._state_writer.run(self._migrate_alerts)
        records = await self._state_writer.read(lambda store: store.alerts.list())
        for record in records:
            await self._seed_vwap_session(record)
        self._engine.load(records)
        expired = self._engine.tick()
        if expired:
            await self._persist_alerts(expired)
        await self.refresh_overlays()

    async def close(self) -> None:
        await self.flush()

    @property
    def engine(self) -> AlertEngine:
        return self._engine

    def active_instrument_ids(self) -> tuple[str, ...]:
        return self._engine.active_instrument_ids()

    def active_realtime_alerts(self) -> dict[str, tuple[str, str]]:
        return {
            alert.alert_id: (alert.instrument_id, alert.timeframe)
            for alert in self._engine.list_alerts()
            if alert.status is AlertStatus.ARMED or (
                alert.status is AlertStatus.COOLDOWN and _has_level_rule(alert)
            )
        }

    def active_key_level_pairs(self) -> set[tuple[str, str]]:
        return {
            (alert.instrument_id, alert.timeframe)
            for alert in self._engine.list_alerts()
            if _has_level_rule(alert)
            and alert.status in {AlertStatus.ARMED, AlertStatus.COOLDOWN}
        }

    def handle_event(self, event: FeedEvent) -> None:
        """同步监听入口；状态变化异步落盘，不阻塞 feed 分发。"""

        changed = self._engine.handle_event(event)
        if changed:
            self._schedule(self._persist_alerts_and_notify_change(changed))

    async def tick(self) -> None:
        changed = self._engine.tick()
        await self.refresh_overlays()
        if changed:
            await self._persist_alerts_and_notify_change(changed)

    async def flush(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------------ 规则

    async def list_alerts(self, status: AlertStatus | None = None) -> list[AlertRecord]:
        return await self._state_writer.read(
            lambda store: store.alerts.list(status=status)
        )

    async def get_alert(self, alert_id: str) -> AlertRecord:
        record = self._engine.get(alert_id)
        if record is not None:
            return record
        return await self._state_writer.read(lambda store: store.alerts.get(alert_id))

    async def create_alert(self, payload: AlertCreate) -> AlertRecord:
        self._check_instrument(payload.instrument_id)
        self._check_timeframe(payload.timeframe)
        if payload.expires_at is not None and payload.expires_at <= self._now_ms():
            raise InvalidRequestError("expires_at 必须晚于当前时间")
        condition = await self._validate_rule(
            payload.condition, payload.instrument_id, payload.timeframe
        )
        now = self._now_ms()
        record = AlertRecord(
            alert_id=f"alr_{uuid4().hex}",
            instrument_id=payload.instrument_id,
            timeframe=payload.timeframe,
            condition=condition,
            status=AlertStatus.DRAFT if payload.draft else AlertStatus.ARMED,
            repeat=payload.repeat,
            cooldown_ms=payload.cooldown_ms,
            expires_at=payload.expires_at,
            note=payload.note,
            created_ts=now,
            updated_ts=now,
        )
        await self._seed_vwap_session(record)
        await self._state_writer.run(lambda store: store.alerts.write(record))
        self._engine.put(record, reset=True)
        await self.refresh_overlays()
        await self._notify_change()
        return record

    async def confirm_alert(self, alert_id: str) -> AlertRecord:
        record = await self.get_alert(alert_id)
        if record.status is AlertStatus.ARMED:
            return record
        if record.status is not AlertStatus.DRAFT:
            raise InvalidRequestError("只有草案预警可以确认激活")
        if record.expires_at is not None and record.expires_at <= self._now_ms():
            raise InvalidRequestError("预警已过期，请先编辑过期时间")
        updated = record.model_copy(
            update={"status": AlertStatus.ARMED, "updated_ts": self._now_ms()}
        )
        await self._seed_vwap_session(updated)
        await self._state_writer.run(lambda store: store.alerts.write(updated))
        self._engine.put(updated, reset=True)
        await self.refresh_overlays()
        await self._notify_change()
        return updated

    async def patch_alert(self, alert_id: str, patch: AlertPatch) -> AlertRecord:
        record = await self.get_alert(alert_id)
        normalized = None
        if patch.condition is not None:
            normalized = await self._validate_rule(
                patch.condition, record.instrument_id, record.timeframe, previous=record.condition
            )
        if patch.expires_at is not None and patch.expires_at <= self._now_ms():
            raise InvalidRequestError("expires_at 必须晚于当前时间")
        now = self._now_ms()
        updates: dict[str, Any] = {"updated_ts": now}
        reset = False
        if normalized is not None:
            updates["condition"] = normalized
            reset = reset or normalized != record.condition
        if patch.note is not None:
            updates["note"] = patch.note
        if patch.repeat is not None:
            updates["repeat"] = patch.repeat
        if patch.cooldown_ms is not None:
            updates["cooldown_ms"] = patch.cooldown_ms
        if patch.expires_at is not None:
            updates["expires_at"] = patch.expires_at
        if patch.enabled is not None:
            if patch.enabled:
                if record.status in {AlertStatus.DISABLED, AlertStatus.EXPIRED}:
                    updates["status"] = AlertStatus.ARMED
                    updates["next_arm_ts"] = None
                    reset = True
            elif record.status is not AlertStatus.DISABLED:
                updates["status"] = AlertStatus.DISABLED
                updates["next_arm_ts"] = None
                reset = True
        updated = record.model_copy(update=updates)
        await self._state_writer.run(lambda store: store.alerts.write(updated))
        self._engine.put(updated, reset=reset)
        await self._seed_vwap_session(updated)
        await self.refresh_overlays()
        await self._notify_change()
        return updated

    async def _seed_vwap_session(self, record: AlertRecord) -> None:
        if self._session_history is None or record.status not in {
            AlertStatus.ARMED, AlertStatus.COOLDOWN,
        }:
            return
        if not any(
            isinstance(operand, RuleIndicator) and operand.params.anchor == "session"
            for node in record.condition.nodes() if isinstance(node, RuleComparison)
            for operand in (node.left, node.right)
        ):
            return
        key = (record.instrument_id, record.timeframe)
        if key in self._session_history_seeded:
            return
        try:
            history = await self._session_history(*key)
        except Exception:
            logger.exception("载入 session VWAP 预警历史柱失败：%s %s", *key)
            return
        if history:
            self._engine.seed_session_bars(*key, history)
        self._session_history_seeded.add(key)

    async def delete_alert(self, alert_id: str) -> AlertRecord:
        record = await self._state_writer.run(
            lambda store: store.alerts.soft_delete(alert_id)
        )
        self._engine.remove(alert_id)
        await self._notify_change()
        return record

    async def list_triggers(
        self,
        *,
        alert_id: str | None = None,
        instrument_id: str | None = None,
        limit: int = 100,
    ) -> list[AlertTriggerRecord]:
        await self.flush()
        return await self._state_writer.read(
            lambda store: store.alerts.list_triggers(
                alert_id=alert_id,
                instrument_id=instrument_id,
                limit=limit,
            )
        )

    async def refresh_overlays(self) -> None:
        # 先后启动的刷新按顺序应用，避免旧画线晚到后覆盖新版本。
        async with self._overlay_refresh_lock:
            records: list[DrawingRecord] = []
            missing: list[str] = []
            for drawing_id in self._engine.overlay_alerts():
                try:
                    records.append(await self._drawings.get_drawing(drawing_id))
                except TalariaError:
                    missing.append(drawing_id)
            self._engine.refresh_drawings(records)
            self._engine.drop_drawings(missing)

    # ----------------------------------------------------------------- 内部

    def _handle_trigger(self, trigger: AlertTriggerRecord, alert: AlertRecord) -> None:
        task = asyncio.get_running_loop().create_task(
            self._persist_trigger(trigger, alert)
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _schedule(self, coroutine: Awaitable[Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("无事件循环，预警状态将留待下一次写操作落盘")
            return
        task = loop.create_task(coroutine)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _persist_trigger(
        self, trigger: AlertTriggerRecord, alert: AlertRecord
    ) -> None:
        await self._state_writer.run(
            lambda store: (
                store.alerts.write(alert),
                store.alerts.append_trigger(trigger),
            )
        )
        if self._on_trigger is not None:
            try:
                self._on_trigger(trigger)
            except Exception:
                logger.exception("预警通知广播失败：%s", trigger.alert_id)
        await self._notify_change()

    async def _persist_alerts(self, alerts: Sequence[AlertRecord]) -> None:
        await self._state_writer.run(
            lambda store: [store.alerts.write(alert) for alert in alerts]
        )

    async def _persist_alerts_and_notify_change(self, alerts: Sequence[AlertRecord]) -> None:
        await self._persist_alerts(alerts)
        await self._notify_change()

    async def _notify_change(self) -> None:
        if self._on_change is None:
            return
        try:
            await self._on_change()
        except Exception:
            logger.exception("预警状态变化后重新平衡订阅失败")

    def _check_instrument(self, instrument_id: str) -> None:
        if instrument_id not in self._instruments:
            from talaria.foundation.errors import InstrumentNotFoundError

            raise InstrumentNotFoundError(f"标的不存在：{instrument_id}")

    @staticmethod
    def _check_timeframe(timeframe: str) -> None:
        if timeframe not in SUPPORTED_TIMEFRAMES:
            raise InvalidRequestError(f"不支持的周期：{timeframe}")

    async def _validate_rule(
        self, rule: Rule, instrument_id: str, timeframe: str, *, previous: Rule | None = None,
    ) -> Rule:
        # 固定保存时的参数，使之后修改工作台默认值不会改变已启用的规则。
        result = rule.model_copy(deep=True)
        defaults = None
        levels = None
        for node in result.references():
            if isinstance(node, (RuleLevelEvent, RuleLevelPrice)):
                if levels is None:
                    provider = self._key_level_provider
                    levels = {lv.level_id: lv for lv in provider(instrument_id, timeframe, self._now_ms())} if provider else {}
                level = levels.get(node.level_id)
                if level is None or (node.level_kind is not None and node.level_kind != level.kind):
                    raise InvalidRequestError("关键位在当前版本中不存在或类型不匹配")
            else:
                await self._validate_overlay(node, instrument_id)
        for node in result.nodes():
            if isinstance(node, RuleComparison) and any(
                isinstance(operand, RuleIndicator) for operand in (node.left, node.right)
            ):
                if defaults is None:
                    defaults = await self._indicator_defaults.effective()
        try:
            if defaults is not None:
                pin_rule_indicator_params(result.expression, previous.expression if previous is not None else None, defaults)
            return Rule.model_validate(result.model_dump())
        except ValueError as exc:
            raise InvalidRequestError(str(exc)) from exc

    async def _validate_overlay(self, node: RuleOverlay | RuleDrawingPrice, instrument_id: str) -> None:
        drawing = await self._drawings.get_drawing(node.drawing_id, after_pending_writes=True)
        if drawing.deleted_at is not None:
            raise InvalidRequestError("画线已删除，无法挂载预警")
        if drawing.instrument_id != instrument_id:
            raise InvalidRequestError("画线不属于该标的")
        if drawing.kind in _NOT_ALERTABLE_KINDS:
            raise InvalidRequestError("文本标签不支持预警")
        bounds = overlay_bounds(
            drawing, drawing.points[0].ts,
            ratio=Decimal("0.5") if node.ratio is None else node.ratio,
        )
        if bounds is None:
            raise InvalidRequestError("该画线在当前时刻不可见")

    @staticmethod
    def _migrate_alerts(store: StateStore) -> None:
        v1 = store.meta.get_with_timestamp(_ALERT_PARAMS_V1_KEY)
        store.alerts.migrate_rules(pine_cutover_ts=v1[1] if v1 is not None else None)
