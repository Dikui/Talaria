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
    AlertEngine,
    AlertPatch,
    AlertRecord,
    AlertStatus,
    AlertTriggerRecord,
    overlay_bounds,
)
from talaria.foundation.alerts.contracts import AlertCondition, AlertConditionKind
from talaria.foundation.drawings import DrawingKind, DrawingRecord
from talaria.foundation.errors import (
    AlertNotFoundError,
    InvalidRequestError,
    TalariaError,
)
from talaria.foundation.feed import FeedEvent
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.operators import (
    CHANNELS,
    DEFAULT_PERIODS,
    DERIVATIVE_INDICATORS,
    IndicatorName,
    spec_from_params,
)
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
from talaria.workbench_api.state_store import StateWriterQueue

logger = logging.getLogger(__name__)

_NOT_ALERTABLE_KINDS = frozenset({DrawingKind.TEXT_LABEL})


class AlertService:
    """预警的用例层；所有写操作都经单写队列串行提交。"""

    def __init__(
        self,
        *,
        state_writer: StateWriterQueue,
        instruments: Sequence[InstrumentSpec],
        now_ms: Callable[[], int],
        bar_history: Callable[[str, str], Sequence[Any]] | None = None,
        key_level_provider: Callable[[str, str, str, int], Any] | None = None,
        freeze_snapshot: Callable[[AlertRecord, int], str] | None = None,
        on_change: Callable[[], Awaitable[None]] | None = None,
        on_trigger: Callable[[AlertTriggerRecord], None] | None = None,
    ) -> None:
        self._state_writer = state_writer
        self._instruments = {spec.instrument_id: spec for spec in instruments}
        self._now_ms = now_ms
        self._on_change = on_change
        self._on_trigger = on_trigger
        self._pending: set[asyncio.Task[Any]] = set()
        self._engine = AlertEngine(
            now_ms=now_ms,
            history_provider=bar_history,
            key_level_provider=key_level_provider,
            freeze_snapshot=freeze_snapshot,
            on_trigger=self._handle_trigger,
        )

    # ------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        records = await self._state_writer.run(lambda store: store.list_alerts())
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

    def handle_event(self, event: FeedEvent) -> None:
        """同步监听入口；状态变化异步落盘，不阻塞 feed 分发。"""

        changed = self._engine.handle_event(event)
        if changed:
            self._schedule(self._persist_alerts(changed))

    async def tick(self) -> None:
        changed = self._engine.tick()
        await self.refresh_overlays()
        if changed:
            await self._persist_alerts(changed)
            await self._notify_change()

    async def flush(self) -> None:
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------------ 规则

    async def list_alerts(self, status: AlertStatus | None = None) -> list[AlertRecord]:
        return await self._state_writer.run(
            lambda store: store.list_alerts(status=status)
        )

    async def get_alert(self, alert_id: str) -> AlertRecord:
        record = self._engine.get(alert_id)
        if record is not None:
            return record
        return await self._state_writer.run(lambda store: store.get_alert(alert_id))

    async def create_alert(self, payload: AlertCreate) -> AlertRecord:
        self._check_instrument(payload.instrument_id)
        self._check_timeframe(payload.timeframe)
        if payload.expires_at is not None and payload.expires_at <= self._now_ms():
            raise InvalidRequestError("expires_at 必须晚于当前时间")
        await self._validate_condition(payload.condition, payload.instrument_id)
        now = self._now_ms()
        record = AlertRecord(
            alert_id=f"alr_{uuid4().hex}",
            instrument_id=payload.instrument_id,
            timeframe=payload.timeframe,
            condition=payload.condition,
            status=AlertStatus.DRAFT if payload.draft else AlertStatus.ARMED,
            repeat=payload.repeat,
            cooldown_ms=payload.cooldown_ms,
            expires_at=payload.expires_at,
            note=payload.note,
            created_ts=now,
            updated_ts=now,
        )
        await self._state_writer.run(lambda store: store.write_alert(record))
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
        await self._state_writer.run(lambda store: store.write_alert(updated))
        self._engine.put(updated, reset=True)
        await self.refresh_overlays()
        await self._notify_change()
        return updated

    async def patch_alert(self, alert_id: str, patch: AlertPatch) -> AlertRecord:
        record = await self.get_alert(alert_id)
        if patch.condition is not None:
            await self._validate_condition(patch.condition, record.instrument_id)
        if patch.expires_at is not None and patch.expires_at <= self._now_ms():
            raise InvalidRequestError("expires_at 必须晚于当前时间")
        now = self._now_ms()
        updates: dict[str, Any] = {"updated_ts": now}
        reset = False
        if patch.condition is not None:
            updates["condition"] = patch.condition
            reset = reset or patch.condition != record.condition
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
        await self._state_writer.run(lambda store: store.write_alert(updated))
        self._engine.put(updated, reset=reset)
        await self.refresh_overlays()
        await self._notify_change()
        return updated

    async def delete_alert(self, alert_id: str) -> AlertRecord:
        record = await self._state_writer.run(
            lambda store: store.soft_delete_alert(alert_id)
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
        return await self._state_writer.run(
            lambda store: store.list_alert_triggers(
                alert_id=alert_id,
                instrument_id=instrument_id,
                limit=limit,
            )
        )

    async def refresh_overlays(self) -> None:
        drawing_ids = self._engine.overlay_alerts()
        if not drawing_ids:
            return
        records: list[DrawingRecord] = []
        missing: list[str] = []
        for drawing_id in drawing_ids:
            try:
                records.append(
                    await self._state_writer.run(
                        lambda store, drawing_id=drawing_id: store.get_drawing(drawing_id)
                    )
                )
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
                store.write_alert(alert),
                store.append_alert_trigger(trigger),
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
            lambda store: [store.write_alert(alert) for alert in alerts]
        )

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

    async def _validate_condition(
        self, condition: AlertCondition, instrument_id: str
    ) -> None:
        if condition.kind is AlertConditionKind.INDICATOR_SIGNAL:
            self._validate_indicator(condition)
            return
        if condition.kind is not AlertConditionKind.OVERLAY_CROSS:
            return
        assert condition.drawing_id is not None
        drawing = await self._state_writer.run(
            lambda store: store.get_drawing(condition.drawing_id)
        )
        if drawing.deleted_at is not None:
            raise InvalidRequestError("画线已删除，无法挂载预警")
        if drawing.instrument_id != instrument_id:
            raise InvalidRequestError("画线不属于该标的")
        if drawing.kind in _NOT_ALERTABLE_KINDS:
            raise InvalidRequestError("文本标签不支持预警")
        bounds = overlay_bounds(
            drawing,
            drawing.points[0].ts,
            ratio=(
                Decimal("0.5")
                if condition.overlay_ratio is None
                else condition.overlay_ratio
            ),
        )
        if bounds is None:
            raise InvalidRequestError("该画线在当前时刻不可见")

    @staticmethod
    def _validate_indicator(condition: AlertCondition) -> None:
        for name, period in (
            (condition.indicator, condition.indicator_period),
            (condition.compare_indicator, condition.compare_period),
        ):
            if name is None:
                continue
            if name in DERIVATIVE_INDICATORS:
                raise InvalidRequestError("衍生品指标暂不支持预警")
            try:
                spec_from_params(name, {"period": period or DEFAULT_PERIODS[name]})
            except ValueError as exc:
                raise InvalidRequestError(str(exc)) from exc
            channel = condition.indicator_channel
            if channel is not None and name is condition.indicator and channel not in CHANNELS[name]:
                raise InvalidRequestError(f"{name} 不支持通道 {channel}")
