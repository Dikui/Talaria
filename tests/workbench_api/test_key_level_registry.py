"""analysis/01：扫描、预警与图表共享按截止时间选版的关键位。"""

from __future__ import annotations

import asyncio
from contextlib import closing
from decimal import Decimal

import pytest

from talaria.foundation.alerts import AlertCreate
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import KlineEvent
from talaria.foundation.key_levels import KeyLevelKind, KeyLevelStatus, compute_key_levels
from talaria.foundation.snapshot import SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.perception.raw_store import RawStore
from talaria.workbench_api.runtime import build_runtime
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = "BTC-USDT-PERP"
SYMBOL = "BTCUSDT"
MINUTE = 60_000
START = 1_795_045_500_000
CUTOFF = START + 40 * MINUTE


def _bar(index, close="100", *, high=None, volume="10"):
    timestamp = START + index * MINUTE
    price = Decimal(close)
    size = Decimal(volume)
    return RawBar(
        symbol=SYMBOL, timeframe="1m", ts_event=timestamp,
        open=price, high=Decimal(high) if high is not None else price, low=price,
        close=price, volume=size, turnover=price * size,
        ts_available=timestamp + MINUTE,
        ts_ingested=timestamp + MINUTE, ts_committed=timestamp + MINUTE,
    )


def _event(bar):
    return KlineEvent(
        instrument_id=INSTRUMENT, timeframe=bar.timeframe,
        bar=bar.to_compact(), confirmed=True, revision=0,
    )


def _round_number(levels):
    return next(
        level
        for level in levels
        if level.kind is KeyLevelKind.ROUND_NUMBER and level.price == Decimal("100")
    )


def _alert(level_id, level_event="TOUCH"):
    return AlertCreate(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        condition={"kind": "LEVEL_EVENT", "level_id": level_id, "level_event": level_event},
    )


def _raw_store(settings):
    return RawStore(settings.data_dir, raw_archive_id=CRYPTO_PERP_PACK.raw_archive_id)


def _publish_cold(settings, bars):
    with closing(ColdStore(settings.data_dir)) as cold:
        cold.publish(ColdBatch(CRYPTO_PERP_PACK.instrument(INSTRUMENT), "1m", bars))


def test_scan_snapshot_supplies_level_id_to_alert_without_a_chart(tmp_path):
    now = CUTOFF
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist",
        now_ms=lambda: now, features=FeatureToggles(market_data=False),
    )
    bars = [_bar(index) for index in range(39)] + [_bar(39, volume="100")]
    _raw_store(settings).append(SYMBOL, "1m", bars)

    async def scenario():
        nonlocal now
        runtime = await build_runtime(settings)
        try:
            scanner = runtime.require_scanner()
            scanner.seed_bars(INSTRUMENT, "1m", [bar.to_compact() for bar in bars])
            trigger, *_ = await scanner.scan([INSTRUMENT])
            level = _round_number(
                runtime.workbench.get_key_levels(trigger.snapshot_id, timeframe="1m").levels
            )
            alerts = runtime.require_alerts()
            alert = await alerts.create_alert(_alert(level.level_id))

            for bar in (_bar(40, "99"), _bar(41)):
                now = bar.ts_available
                alerts.handle_event(_event(bar))

            recorded, = await alerts.list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("100")
            latest = runtime.key_levels.latest((INSTRUMENT, "1m"))
            assert latest.snapshot_id == recorded.snapshot_id
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_restarted_alert_reads_cold_levels_without_scanner_or_chart(tmp_path, fixture_dir):
    now = CUTOFF
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, features=FeatureToggles(scanner=False),
    )
    bars = [_bar(index) for index in range(40)]
    level = _round_number(compute_key_levels(
        instrument_id=INSTRUMENT, timeframe="1m", cutoff_ts=CUTOFF,
        bars=[bar.to_compact() for bar in bars],
    ))

    async def scenario():
        nonlocal now
        first = await build_runtime(settings)
        try:
            alert = await first.require_alerts().create_alert(_alert(level.level_id))
        finally:
            await first.aclose()
        _publish_cold(settings, bars)

        restarted = await build_runtime(settings)
        try:
            assert restarted.scanner is None
            assert restarted.supervisor.has_channel(INSTRUMENT, "1m")
            for bar in (_bar(40, "99"), _bar(41)):
                now = bar.ts_available
                restarted.supervisor.ingest(_event(bar))
            recorded, = await restarted.require_alerts().list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("100")
        finally:
            await restarted.aclose()

    asyncio.run(scenario())


def test_startup_warms_existing_realtime_watchlist_from_visible_cold_bars(
    tmp_path, fixture_dir,
):
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: CUTOFF,
        features=FeatureToggles(alerts=False, scanner=False, cold_store=False),
    )
    _publish_cold(settings, [*(_bar(index) for index in range(40)), _bar(40, "200")])

    async def scenario():
        first = await build_runtime(settings)
        try:
            watchlist = await first.watchlist.create_group("自选")
            await first.watchlist.add_item(watchlist.groups[0].group_id, INSTRUMENT)
        finally:
            await first.aclose()

        restarted = await build_runtime(settings)
        try:
            assert restarted.supervisor.has_channel(INSTRUMENT, "1m")
            latest = restarted.key_levels.latest((INSTRUMENT, "1m"))
            assert latest.cutoff_ts == CUTOFF
            assert _round_number(latest.levels).status is KeyLevelStatus.ACTIVE
            evidence = restarted.workbench.get_snapshot(latest.snapshot_id)
            assert evidence.bar_counts == {"1m": 40}
        finally:
            await restarted.aclose()

    asyncio.run(scenario())


def test_delayed_break_alert_uses_level_status_at_event_time(tmp_path):
    now = CUTOFF
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist",
        now_ms=lambda: now, features=FeatureToggles(market_data=False, scanner=False),
    )
    bars = [_bar(index) for index in range(37)]
    bars.extend([_bar(37, high="110"), _bar(38), _bar(39)])
    baseline, breakout = _bar(40, "110"), _bar(41, "112")
    _raw_store(settings).append(
        SYMBOL, "1m", [*bars, baseline, breakout, _bar(42, "112"), _bar(43, "112")]
    )

    def freeze(workbench, cutoff):
        snapshot = workbench.create_snapshot(SnapshotRequest(
            instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=cutoff,
        ))
        return next(
            level
            for level in workbench.get_key_levels(snapshot.snapshot_id, timeframe="1m").levels
            if level.kind is KeyLevelKind.SWING_HIGH and level.formed_ts == bars[37].ts_event
        )

    async def scenario():
        nonlocal now
        runtime = await build_runtime(settings)
        try:
            active = freeze(runtime.workbench, now)
            alerts = runtime.require_alerts()
            alert = await alerts.create_alert(_alert(active.level_id, "BREAK"))

            now = CUTOFF + 4 * MINUTE
            broken = freeze(runtime.workbench, now)
            assert active.level_id == broken.level_id
            assert active.status is KeyLevelStatus.ACTIVE
            assert broken.status is KeyLevelStatus.BROKEN

            alerts.handle_event(_event(baseline))
            alerts.handle_event(_event(breakout))
            assert await alerts.list_triggers(alert_id=alert.alert_id) == []

            later = _bar(44, "113")
            now = later.ts_available
            alerts.handle_event(_event(later))
            recorded, = await alerts.list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("113")
        finally:
            await runtime.aclose()

    asyncio.run(scenario())
