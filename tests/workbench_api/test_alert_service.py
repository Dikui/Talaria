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
    AlertRecord,
    AlertStatus,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.indicators import IndicatorParams
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
from talaria.workbench_api.drawing_service import DrawingService
from talaria.workbench_api.indicator_defaults import IndicatorDefaultsService
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


def _indicator_alert(
    alert_id: str,
    indicator: str,
    *,
    period: int | None = None,
    anchor: str | None = None,
    threshold: str = "12",
) -> AlertRecord:
    condition = AlertCondition(
        kind=AlertConditionKind.INDICATOR_SIGNAL,
        direction=AlertDirection.ABOVE,
        indicator=indicator,
        indicator_period=period,
        indicator_anchor=anchor,
        threshold=Decimal(threshold),
    )
    return AlertRecord(
        alert_id=alert_id,
        instrument_id=INSTRUMENT,
        timeframe="1m",
        condition=condition,
        status=AlertStatus.ARMED,
        repeat=True,
        cooldown_ms=0,
        created_ts=NOW_MS,
        updated_ts=NOW_MS,
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
        drawings=DrawingService(queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock),
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
            raw_store=RawStore(tmp_path, raw_archive_id="test"),
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


def test_start_migrates_legacy_indicator_params_to_explicit_values(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        try:
            legacy = [
                _indicator_alert("alr_sma", "SMA"),
                _indicator_alert("alr_vwap", "VWAP", threshold="100"),
                _indicator_alert("alr_ema", "EMA", period=50),
            ]
            await writer.run(
                lambda store: [store.alerts.write(record) for record in legacy]
            )
            service = AlertService(
                state_writer=writer,
                drawings=DrawingService(
                    queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock
                ),
                instruments=INSTRUMENTS,
                now_ms=clock,
            )
            await service.start()
            try:
                records = {
                    record.alert_id: record for record in await service.list_alerts()
                }
                assert records["alr_sma"].condition.indicator_period == 20
                assert records["alr_sma"].condition.indicator_anchor is None
                assert records["alr_vwap"].condition.indicator_period == 1
                assert records["alr_vwap"].condition.indicator_anchor == "snapshot"
                assert records["alr_ema"].condition.indicator_period == 50
            finally:
                await service.close()
        finally:
            await writer.close()

        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        try:
            service = AlertService(
                state_writer=writer,
                drawings=DrawingService(
                    queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock
                ),
                instruments=INSTRUMENTS,
                now_ms=clock,
            )
            await service.start()
            try:
                records = {
                    record.alert_id: record for record in await service.list_alerts()
                }
                assert records["alr_sma"].condition.indicator_period == 20
                assert records["alr_vwap"].condition.indicator_anchor == "snapshot"
            finally:
                await service.close()
        finally:
            await writer.close()

    asyncio.run(scenario())


def test_create_alert_fills_effective_defaults_and_explicit_values_win(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, service = await _make_service(tmp_path, clock)
        try:
            ema = await service.create_alert(
                _create(
                    condition=AlertCondition(
                        kind=AlertConditionKind.INDICATOR_SIGNAL,
                        direction=AlertDirection.ABOVE,
                        indicator="EMA",
                        threshold=Decimal("12"),
                    )
                )
            )
            assert ema.condition.indicator_period == 9
            assert ema.condition.indicator_anchor is None

            vwap = await service.create_alert(
                _create(
                    condition=AlertCondition(
                        kind=AlertConditionKind.INDICATOR_SIGNAL,
                        direction=AlertDirection.ABOVE,
                        indicator="VWAP",
                        threshold=Decimal("12"),
                    )
                )
            )
            assert vwap.condition.indicator_period == 1
            assert vwap.condition.indicator_anchor == "session"

            defaults = IndicatorDefaultsService(writer)
            await defaults.set("EMA", IndicatorParams(period=30))
            custom = await service.create_alert(
                _create(
                    condition=AlertCondition(
                        kind=AlertConditionKind.INDICATOR_SIGNAL,
                        direction=AlertDirection.ABOVE,
                        indicator="EMA",
                        indicator_period=5,
                        threshold=Decimal("12"),
                    )
                )
            )
            assert custom.condition.indicator_period == 5

            inherited = await service.create_alert(
                _create(
                    condition=AlertCondition(
                        kind=AlertConditionKind.INDICATOR_SIGNAL,
                        direction=AlertDirection.ABOVE,
                        indicator="EMA",
                        threshold=Decimal("12"),
                    )
                )
            )
            assert inherited.condition.indicator_period == 30

            # 已存预警保留创建时的显式参数，不随后续默认值变化。
            persisted = await service.get_alert(ema.alert_id)
            assert persisted.condition.indicator_period == 9
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_legacy_alert_after_migration_triggers_like_explicit_legacy_period(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        try:
            legacy = _indicator_alert("alr_legacy", "SMA", threshold="12")
            explicit = _indicator_alert(
                "alr_explicit", "SMA", period=20, threshold="12"
            )
            await writer.run(
                lambda store: [
                    store.alerts.write(record) for record in (legacy, explicit)
                ]
            )
            service = AlertService(
                state_writer=writer,
                drawings=DrawingService(
                    queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock
                ),
                instruments=INSTRUMENTS,
                now_ms=clock,
            )
            await service.start()
            try:
                for index in range(23):
                    close = "10"
                    if index >= 20:
                        close = str(20 + (index - 20) * 10)
                    service.handle_event(_kline(NOW_MS + index * MINUTE, close))
                await service.flush()

                triggers = await service.list_triggers()
                assert {trigger.alert_id for trigger in triggers} == {
                    "alr_legacy",
                    "alr_explicit",
                }
                assert len({trigger.triggered_ts for trigger in triggers}) == 1
                assert len({trigger.observed for trigger in triggers}) == 1
                assert all("SMA(20)" in trigger.message for trigger in triggers)
            finally:
                await service.close()
        finally:
            await writer.close()

    asyncio.run(scenario())


def _legacy_crosses(events, name, period, threshold):
    # Independent reference for the default seeds and arithmetic at 5cd280c.
    closes = [event.bar.close for event in events]
    if name == "EMA":
        source = closes
        seed_index = period - 1
        alpha = Decimal(2) / (period + 1)
        recur = lambda prev, current: prev + alpha * (current - prev)
    else:
        # Flat OHLC, so each valid TR after bar 0 is the close-to-close move.
        source = [abs(right - left) for left, right in zip(closes, closes[1:])]
        seed_index = period
        recur = lambda prev, current: (prev * (period - 1) + current) / period
    value = sum(source[:period]) / period
    result = []
    for event, current in zip(events[seed_index + 1:], source[period:], strict=True):
        new_value = recur(value, current)
        if value < threshold <= new_value:
            result.append((event.bar.ts_available, new_value))
        value = new_value
    return result


@pytest.mark.parametrize(
    ("name", "period", "threshold", "prices"),
    [
        ("EMA", 20, "12", ["10", "40"] + ["10"] * 18 + ["40", "50"]),
        ("ATR", 14, "2", ["10"] * 14 + ["50"] * 6 + ["10", "11"]),
    ],
)
def test_migrated_ema_and_atr_alerts_match_baseline_source_bars(
    tmp_path, name, period, threshold, prices
) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        service = AlertService(
            state_writer=writer,
            drawings=DrawingService(queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock),
            instruments=INSTRUMENTS,
            now_ms=clock,
        )
        try:
            record = _indicator_alert("alr_historical", name, threshold=threshold)
            await writer.run(lambda store: store.alerts.write(record))
            await service.start()
            events = [_kline(NOW_MS + index * MINUTE, price) for index, price in enumerate(prices)]
            for event in events:
                clock.now = event.bar.ts_available
                service.handle_event(event)
            await service.flush()
            observed = await service.list_triggers()
            expected = _legacy_crosses(events, name, period, Decimal(threshold))
            assert expected  # The comparison includes a genuine crossing after warmup.
            assert [(item.triggered_ts, item.observed) for item in reversed(observed)] == expected
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_alert_migration_after_v1_preserves_seed_provenance_and_boll_default(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        service = AlertService(
            state_writer=writer,
            drawings=DrawingService(queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock),
            instruments=INSTRUMENTS,
            now_ms=clock,
        )
        try:
            old = _indicator_alert("alr_old", "EMA", period=20)
            old.condition = AlertCondition(
                kind="INDICATOR_SIGNAL", direction="ABOVE",
                indicator="EMA", indicator_period=20,
                compare_indicator="ATR", compare_period=14,
            )
            later = _indicator_alert("alr_after_v1", "EMA", period=20)
            later.created_ts = NOW_MS + 1
            boll = _indicator_alert("alr_boll", "BOLL", period=20)
            await writer.run(lambda store: [store.alerts.write(item) for item in (old, later, boll)])
            await writer.set_meta("alerts.explicit_params.v1", 1, ts=NOW_MS + 1)
            await service.start()
            old = await service.get_alert(old.alert_id)
            later = await service.get_alert(later.alert_id)
            boll = await service.get_alert(boll.alert_id)
            assert old.condition.indicator_seed == old.condition.compare_seed == "legacy_sma"
            assert later.condition.indicator_seed == "pine"
            assert boll.condition.indicator_std_dev == Decimal(2)
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


def test_new_alert_pins_boll_multiplier_and_vwap_anchor_from_user_defaults(tmp_path) -> None:
    async def scenario() -> None:
        clock = Clock()
        writer, service = await _make_service(tmp_path, clock)
        try:
            defaults = IndicatorDefaultsService(writer)
            await defaults.set("BOLL", IndicatorParams(period=2, std_dev="3"))
            await defaults.set("VWAP", IndicatorParams(period=1, anchor="snapshot"))
            main = await service.create_alert(_create(condition=AlertCondition(
                kind="INDICATOR_SIGNAL", direction="ABOVE", indicator="BOLL",
                indicator_channel="upper", threshold="15",
            )))
            comparison = await service.create_alert(_create(condition=AlertCondition(
                kind="INDICATOR_SIGNAL", direction="ABOVE", indicator="VWAP",
                compare_indicator="BOLL",
            )))
            await defaults.clear("BOLL")
            await defaults.clear("VWAP")
            for index, close in enumerate(("10", "10", "13")):
                event = _kline(NOW_MS + index * MINUTE, close)
                clock.now = event.bar.ts_available
                service.handle_event(event)
            await service.flush()
            triggers = await service.list_triggers(alert_id=main.alert_id)
            assert [trigger.observed for trigger in triggers] == [Decimal(16)]
            assert (await service.get_alert(main.alert_id)).condition.indicator_std_dev == Decimal(3)
            assert comparison.condition.compare_std_dev == Decimal(3)
            assert comparison.condition.indicator_anchor == "snapshot"
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())


@pytest.mark.parametrize("continuous", [True, False])
def test_session_vwap_alert_backfills_utc_boundary_on_a_fresh_start(
    tmp_path, continuous
) -> None:
    async def scenario() -> None:
        midnight = NOW_MS // 86_400_000 * 86_400_000
        clock = Clock(midnight + 601 * MINUTE)
        writer = StateWriterQueue(tmp_path, now_ms=clock)
        await writer.start()
        history_end = 600 if continuous else 599
        history = [
            _kline(midnight + index * MINUTE, "10").bar
            for index in range(-1, history_end)
        ]

        async def session_history(instrument_id, timeframe):
            assert (instrument_id, timeframe) == (INSTRUMENT, "1m")
            return history

        service = AlertService(
            state_writer=writer,
            drawings=DrawingService(queue=writer, pack=CRYPTO_PERP_PACK, now_ms=clock),
            instruments=INSTRUMENTS,
            now_ms=clock,
            session_history=session_history,
        )
        try:
            await service.start()
            vwap = await service.create_alert(_create(condition=AlertCondition(
                kind="INDICATOR_SIGNAL", direction="ABOVE", indicator="VWAP", threshold="12",
            )))
            sma = await service.create_alert(_create(condition=AlertCondition(
                kind="INDICATOR_SIGNAL", direction="ABOVE", indicator="SMA",
                indicator_period=3, threshold="12",
            )))
            service.handle_event(_kline(midnight + 600 * MINUTE, "10"))
            clock.now += MINUTE
            service.handle_event(_kline(midnight + 601 * MINUTE, "10000"))
            await service.flush()
            observed = await service.list_triggers()
            expected = [
                (vwap.alert_id, Decimal(16010) / Decimal(602))
            ] if continuous else []
            assert [(record.alert_id, record.observed) for record in observed] == expected
            assert not await service.list_triggers(alert_id=sma.alert_id)
        finally:
            await _shutdown(writer, service)

    asyncio.run(scenario())
