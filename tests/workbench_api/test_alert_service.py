"""预警服务：草案确认、触发证据落盘、冷却释放订阅与状态持久化。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.alerts import (
    AlertCondition,
    AlertConditionKind,
    AlertCreate,
    AlertDirection,
    AlertMetric,
    AlertPatch,
    AlertStatus,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.types import PrecisionTier
from talaria.foundation.feed import KlineEvent
from talaria.foundation.snapshot import SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore
from talaria.workbench_api.alert_service import AlertService
from talaria.workbench_api.state_store import StateWriterQueue

pytestmark = pytest.mark.medium

INSTRUMENT = "BTC-USDT-PERP"
NOW_MS = 1_795_022_974_103
MINUTE = 60_000
INSTRUMENTS = CRYPTO_PERP_PACK.instruments()


class Clock:
    def __init__(self, now: int = NOW_MS) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


def _metric_condition(
    threshold: str = "105", direction=AlertDirection.ABOVE
) -> AlertCondition:
    return AlertCondition(
        kind=AlertConditionKind.METRIC_THRESHOLD,
        metric=AlertMetric.LAST_PRICE,
        direction=direction,
        threshold=Decimal(threshold),
    )


def _kline(ts: int, close: str) -> KlineEvent:
    value = Decimal(close)
    bar = CompactBar(
        timestamp=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        turnover=value,
        status=BarStatus.NORMAL,
        ts_available=ts + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )
    return KlineEvent(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        bar=bar,
        confirmed=True,
        revision=0,
    )


def _create(**overrides) -> AlertCreate:
    body = {
        "instrument_id": INSTRUMENT,
        "timeframe": "1m",
        "condition": _metric_condition(),
        "note": "",
        "cooldown_ms": 60_000,
        "repeat": True,
    }
    body.update(overrides)
    return AlertCreate(**body)


async def _make_service(
    tmp_path: Path,
    clock: Clock,
    *,
    freeze_snapshot=None,
) -> tuple[StateWriterQueue, AlertService]:
    writer = StateWriterQueue(tmp_path, now_ms=clock)
    await writer.start()
    service = AlertService(
        state_writer=writer,
        instruments=INSTRUMENTS,
        now_ms=clock,
        freeze_snapshot=freeze_snapshot,
    )
    await service.start()
    return writer, service


async def _shutdown(writer: StateWriterQueue, service: AlertService) -> None:
    await service.close()
    await writer.close()


def test_draft_never_triggers_until_confirmed_and_confirmed_trigger_is_persisted(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, service = await _make_service(
            tmp_path, clock, freeze_snapshot=lambda alert, now: "snap-evidence"
        )
        try:
            draft = await service.create_alert(_create(draft=True))
            assert draft.status is AlertStatus.DRAFT

            service.handle_event(_kline(NOW_MS, "101"))
            service.handle_event(_kline(NOW_MS + MINUTE, "106"))
            await service.flush()
            assert await service.list_triggers() == []
            assert await service.list_alerts(AlertStatus.DRAFT) == [draft]

            armed = await service.confirm_alert(draft.alert_id)
            assert armed.status is AlertStatus.ARMED

            # 确认后的第一根只建立基线；第二根才构成向上穿越。
            service.handle_event(_kline(NOW_MS + 2 * MINUTE, "101"))
            service.handle_event(_kline(NOW_MS + 3 * MINUTE, "106"))
            await service.flush()

            triggers = await service.list_triggers()
            assert len(triggers) == 1
            trigger = triggers[0]
            assert trigger.alert_id == draft.alert_id
            assert trigger.snapshot_id == "snap-evidence"
            assert trigger.instrument_id == INSTRUMENT
            assert trigger.condition_kind is AlertConditionKind.METRIC_THRESHOLD
            assert trigger.observed == Decimal("106")
            assert trigger.threshold == Decimal("105")
            assert "上穿" in trigger.message
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_cooldown_expiry_and_delete_release_alert_tier(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, service = await _make_service(tmp_path, clock)
        try:
            record = await service.create_alert(_create(cooldown_ms=10_000))
            assert service.active_instrument_ids() == (INSTRUMENT,)

            service.handle_event(_kline(NOW_MS, "100"))
            service.handle_event(_kline(NOW_MS + MINUTE, "106"))
            await service.flush()

            cooling = await service.get_alert(record.alert_id)
            assert cooling.status is AlertStatus.COOLDOWN
            assert cooling.next_arm_ts == NOW_MS + 10_000
            assert service.active_instrument_ids() == ()

            clock.now = cooling.next_arm_ts
            await service.tick()
            rearmed = await service.get_alert(record.alert_id)
            assert rearmed.status is AlertStatus.ARMED
            assert rearmed.next_arm_ts is None
            assert service.active_instrument_ids() == (INSTRUMENT,)

            disabled = await service.patch_alert(
                record.alert_id, AlertPatch(enabled=False)
            )
            assert disabled.status is AlertStatus.DISABLED
            assert service.active_instrument_ids() == ()

            back = await service.patch_alert(
                record.alert_id, AlertPatch(enabled=True)
            )
            assert back.status is AlertStatus.ARMED
            assert service.active_instrument_ids() == (INSTRUMENT,)

            await service.delete_alert(record.alert_id)
            assert service.active_instrument_ids() == ()
            assert await service.list_alerts() == []
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_alert_state_and_trigger_survive_service_restart(tmp_path: Path) -> None:
    async def first_run() -> str:
        clock = Clock()
        writer, service = await _make_service(
            tmp_path, clock, freeze_snapshot=lambda alert, now: "snap-restart"
        )
        try:
            record = await service.create_alert(_create(note="突破 105 后观察"))
            service.handle_event(_kline(NOW_MS, "100"))
            service.handle_event(_kline(NOW_MS + MINUTE, "106"))
            await service.flush()
            return record.alert_id
        finally:
            await _shutdown(writer, service)

    async def second_run(alert_id: str) -> None:
        clock = Clock(NOW_MS + 30_000)
        writer, service = await _make_service(tmp_path, clock)
        try:
            records = await service.list_alerts()
            assert [record.alert_id for record in records] == [alert_id]
            assert records[0].status is AlertStatus.COOLDOWN
            assert records[0].trigger_count == 1
            triggers = await service.list_triggers(alert_id=alert_id)
            assert [trigger.snapshot_id for trigger in triggers] == ["snap-restart"]
            assert triggers[0].note == "突破 105 后观察"
        finally:
            await _shutdown(writer, service)

    alert_id = asyncio.run(first_run())
    asyncio.run(second_run(alert_id))


def test_editing_threshold_uses_new_value_without_recreating(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, service = await _make_service(tmp_path, clock)
        try:
            record = await service.create_alert(_create())
            patched = await service.patch_alert(
                record.alert_id,
                AlertPatch(condition=_metric_condition(threshold="108")),
            )
            assert patched.condition.threshold == Decimal("108")
            assert patched.status is AlertStatus.ARMED

            # 106 对旧阈值 105 是穿越，对新阈值 108 不是；只有 109 才触发。
            service.handle_event(_kline(NOW_MS, "100"))
            service.handle_event(_kline(NOW_MS + MINUTE, "106"))
            await service.flush()
            assert await service.list_triggers() == []

            service.handle_event(_kline(NOW_MS + 2 * MINUTE, "109"))
            await service.flush()
            triggers = await service.list_triggers()
            assert len(triggers) == 1
            assert triggers[0].threshold == Decimal("108")
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_trigger_freezes_registered_evidence_snapshot(tmp_path: Path, fixture_dir: Path) -> None:
    """触发回调拿到的 snapshot_id 必须是已登记、可回读的快照。"""

    async def scenario() -> str:
        clock = Clock()
        snapshot_store = SnapshotStore(tmp_path)
        builder = SnapshotBuilder(
            data_dir=tmp_path,
            connector=FixtureConnector(fixture_dir),
            raw_store=RawStore(tmp_path),
            snapshot_store=snapshot_store,
            policy=BackfillPolicy(target_bars=3000, max_pages=5),
            now_ms=clock,
        )
        instruments_by_id = {spec.instrument_id: spec for spec in INSTRUMENTS}

        def freeze(alert, now: int) -> str:
            return builder.build(
                instrument=instruments_by_id[alert.instrument_id],
                request=SnapshotRequest(
                    instrument_id=alert.instrument_id,
                    timeframes=[alert.timeframe],
                    cutoff_ts=now,
                ),
            ).snapshot_id

        writer, service = await _make_service(tmp_path, clock, freeze_snapshot=freeze)
        try:
            await service.create_alert(_create())
            service.handle_event(_kline(NOW_MS - MINUTE, "100"))
            service.handle_event(_kline(NOW_MS, "106"))
            await service.flush()
            triggers = await service.list_triggers()
            assert len(triggers) == 1
            snapshot_id = triggers[0].snapshot_id
            assert snapshot_id
            assert snapshot_store.get(snapshot_id) is not None
            return snapshot_id
        finally:
            await _shutdown(writer, service)
            snapshot_store.close()

    assert asyncio.run(scenario())
