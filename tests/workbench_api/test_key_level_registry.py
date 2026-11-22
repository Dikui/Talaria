"""analysis/01：扫描、预警与图表共享按截止时间选版的关键位。"""

from __future__ import annotations

import asyncio
import threading
from contextlib import closing
from decimal import Decimal

import pytest

from talaria.foundation.alerts import AlertCreate, AlertStatus
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import KlineEvent, ReceivedKlineEvent
from talaria.foundation.key_levels import KeyLevelKind, KeyLevelSet, KeyLevelStatus, compute_key_levels
from talaria.foundation.snapshot import SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.crypto_perp.instruments import CryptoPerpPack
from talaria.packs.testing import FixtureConnector
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.perception.key_level_registry import KeyLevelRegistry
from talaria.perception.raw_store import RawStore
from talaria.replay import ReplayFeed
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


def _event(bar, *, received_ts=None):
    if received_ts is not None:
        return ReceivedKlineEvent(
            instrument_id=INSTRUMENT, timeframe=bar.timeframe,
            bar=bar.to_compact(), confirmed=False, revision=0, ts_ingested=received_ts,
        )
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


@pytest.mark.parametrize("source", ["cold", "snapshot_archive"])
def test_startup_warms_existing_realtime_watchlist_with_cold_writes_disabled(
    tmp_path, fixture_dir, source,
):
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: CUTOFF, backfill_bars=40,
        features=FeatureToggles(alerts=False, scanner=False, cold_store=False),
    )
    bars = [*(_bar(index) for index in range(40)), _bar(40, "200")]
    if source == "cold":
        _publish_cold(settings, bars)
    else:
        _raw_store(settings).append(SYMBOL, "1m", bars)

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


def test_empty_cold_startup_warms_after_subscription_backfill(tmp_path, monkeypatch):
    now = CUTOFF
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    bars = [_bar(index) for index in range(40)]
    level = _round_number(compute_key_levels(
        instrument_id=INSTRUMENT, timeframe="1m", cutoff_ts=CUTOFF,
        bars=[bar.to_compact() for bar in bars],
    ))
    release_history = threading.Event()

    class History:
        # Replace the exchange REST boundary while preserving subscription backfill.
        def fetch_bars(self, *, symbol, timeframe, end_ts, limit):
            assert release_history.wait(timeout=5)
            return [bar for bar in bars if bar.ts_event <= end_ts][-limit:]

    monkeypatch.setattr(CryptoPerpPack, "create_history_connector", lambda *a, **kw: History())
    monkeypatch.setattr(CryptoPerpPack, "create_realtime_stream", lambda *a, **kw: ReplayFeed(replay))

    async def scenario():
        nonlocal now
        first = await build_runtime(AppSettings(
            data_dir=tmp_path, dist_dir=tmp_path / "dist", now_ms=lambda: now,
            features=FeatureToggles(market_data=False, scanner=False),
        ))
        try:
            alert = await first.require_alerts().create_alert(_alert(level.level_id))
        finally:
            await first.aclose()

        restarted = await build_runtime(AppSettings(
            data_dir=tmp_path, dist_dir=tmp_path / "dist", now_ms=lambda: now,
            backfill_bars=40, features=FeatureToggles(scanner=False),
        ))
        try:
            assert restarted.key_levels.latest((INSTRUMENT, "1m")) is None
            release_history.set()
            async with asyncio.timeout(2):
                while restarted.key_levels.latest((INSTRUMENT, "1m")) is None:
                    await asyncio.sleep(0.01)

            for bar in (_bar(40, "99"), _bar(41)):
                now = bar.ts_available
                restarted.supervisor.ingest(_event(bar))
            recorded, = await restarted.require_alerts().list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("100")
        finally:
            release_history.set()
            await restarted.aclose()

    asyncio.run(scenario())


def test_delayed_break_alert_uses_level_status_at_bar_close(tmp_path, fixture_dir):
    now = CUTOFF
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, features=FeatureToggles(scanner=False),
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

            runtime.supervisor.ingest(_event(baseline))
            runtime.supervisor.ingest(_event(breakout))
            received = runtime.supervisor.recent_bars(instrument_id=INSTRUMENT, timeframe="1m")
            assert received[-1].ts_available == now
            assert await alerts.list_triggers(alert_id=alert.alert_id) == []

            runtime.supervisor.ingest(_event(_bar(42, "113")))
            later = _bar(43, "113")
            now = later.ts_available
            runtime.supervisor.ingest(_event(later))
            recorded, = await alerts.list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("113")
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_intrabar_touch_uses_version_visible_at_receipt(tmp_path, fixture_dir):
    now = CUTOFF + 1_000
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, backfill_bars=40,
        features=FeatureToggles(scanner=False),
    )
    _raw_store(settings).append(SYMBOL, "1m", [_bar(index) for index in range(40)])

    async def scenario():
        nonlocal now
        runtime = await build_runtime(settings)
        try:
            runtime.workbench.create_snapshot(SnapshotRequest(
                instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=now,
            ))
            version = runtime.key_levels.latest((INSTRUMENT, "1m"))
            level = _round_number(version.levels)
            alerts = runtime.require_alerts()
            alert = await alerts.create_alert(_alert(level.level_id))

            now = CUTOFF + 4_000
            runtime.key_levels.register(version.model_copy(update={"cutoff_ts": now, "levels": []}))
            runtime.supervisor.ingest(_event(_bar(40, "99"), received_ts=CUTOFF + 2_000))
            runtime.supervisor.ingest(_event(_bar(40), received_ts=CUTOFF + 3_000))
            recorded, = await alerts.list_triggers(alert_id=alert.alert_id)
            assert recorded.observed == Decimal("100")

            now = CUTOFF + MINUTE
            snapshot = runtime.workbench.create_snapshot(SnapshotRequest(
                instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=now,
            ))
            assert snapshot.bar_counts == {"1m": 40}
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


# analysis/01：已过预计收盘时刻的进行中柱仍会留在热缓冲，但不能改变冻结的关键位。
@pytest.mark.regression
def test_snapshot_key_levels_exclude_unconfirmed_hot_bars(tmp_path, fixture_dir):
    open_bar = _bar(40, "200")
    now = open_bar.ts_available + 1_000
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, backfill_bars=40,
        features=FeatureToggles(scanner=False, cold_store=False),
    )
    _raw_store(settings).append(SYMBOL, "1m", [_bar(index) for index in range(40)])
    request = SnapshotRequest(instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=now)

    async def scenario():
        runtime = await build_runtime(settings)
        try:
            workbench = runtime.workbench
            baseline = workbench.create_snapshot(request)
            expected = workbench.get_key_levels(baseline.snapshot_id, timeframe="1m").levels
            assert expected

            runtime.supervisor.subscribe(instrument_id=INSTRUMENT, timeframe="1m")
            runtime.supervisor.ingest(_event(open_bar, received_ts=now))
            hot = runtime.hot_buffer.recent_bars(instrument_id=INSTRUMENT, timeframe="1m")
            assert [(bar.ts_event, bar.confirmed) for bar in hot] == [(open_bar.ts_event, False)]

            frozen = workbench.create_snapshot(request)
            assert workbench.get_key_levels(frozen.snapshot_id, timeframe="1m").levels == expected
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_alerts_share_one_version_when_first_trigger_freezes_a_snapshot(tmp_path, fixture_dir):
    now = CUTOFF
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, features=FeatureToggles(scanner=False),
    )
    _raw_store(settings).append(SYMBOL, "1m", [_bar(index) for index in range(40)])

    async def scenario():
        nonlocal now
        runtime = await build_runtime(settings)
        try:
            runtime.workbench.create_snapshot(SnapshotRequest(
                instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=now,
            ))
            version = runtime.key_levels.latest((INSTRUMENT, "1m"))
            round_level = _round_number(version.levels)
            # The first trigger's snapshot recomputes levels and drops this synthetic level.
            second_level = round_level.model_copy(update={"level_id": "kl_only_in_first_version"})
            runtime.key_levels.register(version.model_copy(update={"levels": [round_level, second_level]}))
            alerts = runtime.require_alerts()
            first = await alerts.create_alert(_alert(round_level.level_id))
            now += 1
            second = await alerts.create_alert(_alert(second_level.level_id))

            for bar in (_bar(40, "99"), _bar(41)):
                now = bar.ts_available
                runtime.supervisor.ingest(_event(bar))

            first_trigger, = await alerts.list_triggers(alert_id=first.alert_id)
            assert first_trigger.snapshot_id != version.snapshot_id
            assert second_level.level_id not in {
                level.level_id for level in runtime.key_levels.latest((INSTRUMENT, "1m")).levels
            }
            second_trigger, = await alerts.list_triggers(alert_id=second.alert_id)
            assert second_trigger.observed == Decimal("100")
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("alert_mode", ["BREAK", "TOUCH"])
def test_confirmed_bars_refresh_level_status_without_scanner_or_chart(
    tmp_path, fixture_dir, alert_mode,
):
    now = CUTOFF
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: now, backfill_bars=40,
        features=FeatureToggles(scanner=False, cold_store=False),
    )
    swing = _bar(37, high="110")
    _raw_store(settings).append(SYMBOL, "1m", [
        *(_bar(index) for index in range(37)), swing, _bar(38), _bar(39),
    ])

    async def scenario():
        nonlocal now
        runtime = await build_runtime(settings)
        pair = (INSTRUMENT, "1m")
        try:
            runtime.workbench.create_snapshot(SnapshotRequest(
                instrument_id=INSTRUMENT, timeframes=["1m"], cutoff_ts=now,
            ))
            initial = runtime.key_levels.latest(pair)
            level = next(
                level for level in initial.levels
                if level.kind is KeyLevelKind.SWING_HIGH and level.formed_ts == swing.ts_event
            )
            assert level.status is KeyLevelStatus.ACTIVE
            alerts = runtime.require_alerts()
            request = _alert(level.level_id, alert_mode)
            if alert_mode == "TOUCH":
                watchlist = await runtime.watchlist.create_group("自选")
                await runtime.watchlist.add_item(watchlist.groups[0].group_id, INSTRUMENT)
                request = request.model_copy(update={"repeat": True, "cooldown_ms": 10 * MINUTE})
            alert = await alerts.create_alert(request)

            for bar in (_bar(40, "112"), _bar(41, "110"), _bar(42, "112")):
                now = bar.ts_available
                runtime.supervisor.ingest(_event(bar))
                if alert_mode == "TOUCH" and bar.close == Decimal("110"):
                    await alerts.flush()
                    assert (await alerts.get_alert(alert.alert_id)).status is AlertStatus.COOLDOWN
                else:
                    assert runtime.key_levels.latest(pair).cutoff_ts < now
                async with asyncio.timeout(2):
                    while runtime.key_levels.latest(pair).cutoff_ts < now:
                        await asyncio.sleep(0.01)

            refreshed = runtime.key_levels.latest(pair)
            updated = next(item for item in refreshed.levels if item.level_id == level.level_id)
            assert updated.status is KeyLevelStatus.BROKEN
            if alert_mode == "BREAK":
                now += MINUTE
                runtime.supervisor.ingest(_event(_bar(43, "113")))
                recorded, = await alerts.list_triggers(alert_id=alert.alert_id)
                assert recorded.observed == Decimal("113")
        finally:
            await runtime.aclose()

    asyncio.run(scenario())


def test_restart_warmup_preserves_liquidation_level_from_scanner(tmp_path, fixture_dir):
    now = 1_795_022_974_103
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    bars = FixtureConnector(fixture_dir).fetch_bars(symbol=SYMBOL, timeframe="1m", limit=900)

    def settings(scanner: bool) -> AppSettings:
        return AppSettings(
            data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
            replay_events=replay, now_ms=lambda: now, backfill_bars=900,
            features=FeatureToggles(scanner=scanner),
        )

    _publish_cold(settings(True), bars)

    async def scenario():
        nonlocal now
        first = await build_runtime(settings(True))
        try:
            scanner = first.require_scanner()
            scan_bars = [bar.to_compact() for bar in bars[-40:]]
            scan_bars[-1] = scan_bars[-1].model_copy(update={"turnover": Decimal("100000000")})
            scanner.seed_bars(INSTRUMENT, "1m", scan_bars)
            trigger, *_ = await scanner.scan([INSTRUMENT])
            level = next(
                item for item in first.workbench.get_key_levels(trigger.snapshot_id, timeframe="1m").levels
                if item.kind is KeyLevelKind.LIQUIDATION_ZONE
            )
            alert = await first.require_alerts().create_alert(_alert(level.level_id))
        finally:
            await first.aclose()

        restarted = await build_runtime(settings(False))
        try:
            warmed = restarted.key_levels.latest((INSTRUMENT, "1m"))
            assert level.level_id in {item.level_id for item in warmed.levels}
            for bar in (_bar(40, str(level.price_low - 1)), _bar(41, str(level.price_low))):
                now = bar.ts_available
                restarted.supervisor.ingest(_event(bar))
            recorded, = await restarted.require_alerts().list_triggers(alert_id=alert.alert_id)
            assert recorded.threshold == level.price_low
        finally:
            await restarted.aclose()

    asyncio.run(scenario())


def test_registry_retains_256_distinct_cutoffs_per_series(caplog):
    registry = KeyLevelRegistry(now_ms=lambda: CUTOFF + 256)
    bars = [_bar(index).to_compact() for index in range(40)]
    level = _round_number(compute_key_levels(
        instrument_id=INSTRUMENT, timeframe="1m", cutoff_ts=CUTOFF, bars=bars,
    ))
    version = KeyLevelSet(
        snapshot_id="original", instrument_id=INSTRUMENT, timeframe="1m",
        cutoff_ts=CUTOFF, as_of_ts=CUTOFF, levels=[level],
    )
    other_pair = version.model_copy(update={"timeframe": "5m"})
    registry.register(other_pair)
    for offset in reversed(range(257)):
        registry.register(version.model_copy(update={"cutoff_ts": CUTOFF + offset}))
    replacement = version.model_copy(update={
        "snapshot_id": "replacement", "cutoff_ts": CUTOFF + 256, "levels": [],
    })
    registry.register(replacement)

    with caplog.at_level("DEBUG", logger="talaria.perception.key_level_registry"):
        assert registry.levels_at(INSTRUMENT, "1m", CUTOFF) == []
    assert "早于最早关键位版本" in caplog.text
    assert registry.levels_at(INSTRUMENT, "1m", CUTOFF + 1) == [level]
    assert registry.levels_at(INSTRUMENT, "1m", CUTOFF + 256) == []
    assert registry.latest((INSTRUMENT, "1m")) == replacement
    assert registry.latest((INSTRUMENT, "5m")) == other_pair
