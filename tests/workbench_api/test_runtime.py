from __future__ import annotations

import asyncio
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import SingleWriterViolationError
from talaria.foundation.feed import KlineEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.crypto_perp.instruments import CryptoPerpPack
from talaria.perception.cold_store import ColdReader, ColdStore
from talaria.perception.snapshot_store import SnapshotStore
from talaria.workbench_api.runtime import build_runtime
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateStore


def test_runtime_can_start_and_close_without_leaking_resources(
    tmp_path: Path, fixture_dir: Path, repo_root: Path
) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=fixture_dir,
        replay_events=repo_root / "tests/fixtures/realtime/demo_outage.jsonl",
    )
    initial_threads = set(threading.enumerate())

    async def start_and_close() -> None:
        initial_tasks = asyncio.all_tasks()
        runtime = await build_runtime(settings)
        await runtime.aclose()

        assert asyncio.all_tasks() == initial_tasks
        assert not any(
            thread.name.startswith("state-writer")
            for thread in set(threading.enumerate()) - initial_threads
        )
        with (
            closing(ColdStore(settings.data_dir)),
            closing(SnapshotStore(settings.data_dir)),
            closing(StateStore(settings.data_dir)),
        ):
            pass

    asyncio.run(start_and_close())
    assert not set(threading.enumerate()) - initial_threads


def test_runtime_releases_started_resources_when_single_writer_is_taken(
    tmp_path: Path, fixture_dir: Path, repo_root: Path
) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=fixture_dir,
        replay_events=repo_root / "tests/fixtures/realtime/demo_outage.jsonl",
    )

    async def fail_start() -> None:
        initial_tasks = asyncio.all_tasks()
        with pytest.raises(SingleWriterViolationError):
            await build_runtime(settings)
        assert asyncio.all_tasks() == initial_tasks

    with closing(StateStore(settings.data_dir)):
        asyncio.run(fail_start())
        with (
            closing(ColdStore(settings.data_dir)),
            closing(SnapshotStore(settings.data_dir)),
        ):
            pass


def test_alert_session_history_pages_to_one_bar_before_utc_midnight() -> None:
    from decimal import Decimal

    from talaria.workbench_api.runtime import read_alert_session_history

    minute = 60_000
    midnight = 1_793_577_600_000
    cutoff = midnight + 1_200 * minute
    value = Decimal(10)

    class History:
        def fetch_bars(self, *, symbol, timeframe, end_ts, limit):
            assert (symbol, timeframe) == ("BTCUSDT", "1m")
            assert 1 <= limit <= 1000
            last = min((end_ts - midnight) // minute, 1200)
            return [
                RawBar(
                    symbol=symbol, timeframe=timeframe, ts_event=midnight + i * minute,
                    open=value, high=value, low=value, close=value, volume=value,
                    turnover=value * value, ts_available=midnight + (i + 1) * minute,
                    ts_ingested=cutoff, ts_committed=cutoff,
                )
                for i in range(last - limit + 1, last + 1)
            ]

    bars = read_alert_session_history(
        History(), symbol="BTCUSDT", timeframe="1m", cutoff_ts=cutoff,
    )
    assert [bar.timestamp for bar in bars] == [
        midnight + i * minute for i in range(-1, 1200)
    ]


@pytest.mark.medium
@pytest.mark.spec
def test_replay_publishes_only_confirmed_bars_and_preserves_revisions_on_restart(
    tmp_path: Path, fixture_dir: Path, monkeypatch,
) -> None:
    # ADR-0018: a close and two corrections for the same event time.
    monkeypatch.setattr("talaria.perception.cold_publisher.COMPACTION_INTERVAL_SECONDS", 0.05)
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    timestamp = 1_795_045_500_000
    end = timestamp + 60_000
    replay = tmp_path / "bars.jsonl"
    rows = []
    for offset, close, confirmed in (
        (50, "99.5", False), (150, "100", True), (250, "101", True), (350, "102", True)
    ):
        bar = CompactBar(
            timestamp=timestamp, open=Decimal("100"), high=Decimal("103"),
            low=Decimal("99"), close=Decimal(close), volume=Decimal("1"),
            turnover=Decimal("100"), status="NORMAL", ts_available=end,
            precision="SECOND_VERIFIED",
        )
        event = KlineEvent(
            instrument_id=instrument.instrument_id, timeframe="1m", bar=bar,
            confirmed=confirmed, revision=0,
        )
        rows.append(json.dumps({
            "offset_ms": offset, "type": "kline", "event": event.model_dump(mode="json"),
        }))
    replay.write_text("\n".join(rows), encoding="utf-8")
    start = time.monotonic()
    settings = AppSettings(
        data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=lambda: end + 1000 + int((time.monotonic() - start) * 1000),
        features=FeatureToggles(alerts=False, scanner=False),
    )
    reader = ColdReader(settings.data_dir)

    def close_at(as_of=None):
        return [bar.close for bar in reader.read_bars(instrument, "1m", as_of=as_of)]

    async def run() -> int:
        runtime = await build_runtime(settings)
        events: asyncio.Queue[KlineEvent] = asyncio.Queue()
        runtime.supervisor.add_listener(
            lambda event: events.put_nowait(event) if isinstance(event, KlineEvent) else None
        )
        runtime.subscription_scheduler.acquire_chart((instrument.instrument_id, "1m"))
        try:
            async with asyncio.timeout(5):
                preview = await events.get()
                assert preview.confirmed is False
                assert close_at() == []
                first, second, third = [await events.get() for _ in range(3)]
                assert first.bar.ts_available == end
                assert end < second.bar.ts_available < third.bar.ts_available
                while close_at() != [Decimal("102")]:
                    await asyncio.sleep(0.01)
                assert close_at(end) == [Decimal("100")]
                assert close_at(second.bar.ts_available) == [Decimal("101")]
                while True:
                    generations = await asyncio.to_thread(
                        runtime.cold_store.generations, instrument, "1m"
                    )
                    if len(generations) == 1 and generations[0].parquet_path.name.startswith("partition-"):
                        break
                    await asyncio.sleep(0.01)
            assert close_at(end) == [Decimal("100")]
            assert close_at(second.bar.ts_available) == [Decimal("101")]
            return second.bar.ts_available
        finally:
            await runtime.aclose()

    cutoff = asyncio.run(run())
    with closing(ColdStore(settings.data_dir)):
        assert close_at(end) == [Decimal("100")]
        assert close_at(cutoff) == [Decimal("101")]
        assert close_at() == [Decimal("102")]


def _idle_replay_settings(tmp_path, fixture_dir, now_ms):
    replay = tmp_path / "events.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    return AppSettings(
        data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        replay_events=replay, now_ms=now_ms, features=FeatureToggles(alerts=False, scanner=False),
    )


@asynccontextmanager
async def _subscribed_supervisor(settings, instrument):
    runtime = await build_runtime(settings)
    try:
        runtime.supervisor.subscribe(instrument_id=instrument.instrument_id, timeframe="1m")
        yield runtime.supervisor
    finally:
        await runtime.aclose()


def _confirmed_kline(instrument, timestamp, close, *, volume="1", status=BarStatus.NORMAL):
    bar = CompactBar(
        timestamp=timestamp, open=close, high=close, low=close, close=close,
        volume=volume, turnover=str(Decimal(close) * Decimal(volume)), status=status,
        ts_available=timestamp + 60_000, precision="SECOND_VERIFIED",
    )
    return KlineEvent(
        instrument_id=instrument.instrument_id, timeframe="1m", bar=bar, confirmed=True, revision=0,
    )


@pytest.mark.medium
@pytest.mark.regression
@pytest.mark.parametrize("initial", ["failed_write", "restart"])
def test_late_revision_is_only_available_from_receipt(tmp_path, fixture_dir, monkeypatch, initial):
    # ADR-0018: if a write fails or the service restarts, the correction cannot appear early.
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    timestamp = 1_795_045_500_000
    end = timestamp + 60_000
    now = end + 1_000
    settings = _idle_replay_settings(tmp_path, fixture_dir, lambda: now)
    publish = ColdStore.publish

    def publish_with_failure(store, batch):
        if batch.bars[0].close == Decimal("100"):
            raise OSError("simulated first write failure")
        return publish(store, batch)

    if initial == "failed_write":
        monkeypatch.setattr(ColdStore, "publish", publish_with_failure)

    async def run():
        nonlocal now
        correction = _confirmed_kline(instrument, timestamp, "101")
        async with _subscribed_supervisor(settings, instrument) as supervisor:
            supervisor.ingest(_confirmed_kline(instrument, timestamp, "100"))
            if initial == "failed_write":
                now = end + 3_000
                supervisor.ingest(correction)
        if initial == "restart":
            now = end + 15_000
            async with _subscribed_supervisor(settings, instrument) as supervisor:
                available = []
                supervisor.add_listener(
                    lambda event: available.append(event.bar.ts_available)
                    if isinstance(event, KlineEvent) else None
                )
                supervisor.ingest(correction)
                assert available == [now]

    asyncio.run(run())
    reader = ColdReader(settings.data_dir)
    earlier = [Decimal("100")] if initial == "restart" else []
    assert [bar.close for bar in reader.read_bars(instrument, "1m", as_of=now - 1)] == earlier
    assert [(bar.close, bar.ts_available) for bar in reader.read_bars(instrument, "1m")] == [
        (Decimal("101"), now),
    ]


@pytest.mark.medium
@pytest.mark.spec
def test_first_live_subscription_backfills_history_across_months(tmp_path: Path, monkeypatch) -> None:
    # data-access/02: replace the exchange transport and REST connector.
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    month_start = int(datetime(2025, 12, 1, tzinfo=UTC).timestamp() * 1000)
    minute = 60_000
    now = month_start + minute
    value = Decimal("100")

    class History:
        def fetch_bars(self, *, symbol, timeframe, end_ts, limit):
            assert (symbol, timeframe, end_ts, limit) == (instrument.symbol, "1m", month_start, 2)
            return [
                RawBar(
                    symbol=symbol, timeframe=timeframe, ts_event=ts,
                    open=value, high=value, low=value, close=value,
                    volume=Decimal("1"), turnover=value, ts_available=ts + minute,
                    ts_ingested=now, ts_committed=now,
                )
                for ts in (month_start - minute, month_start)
            ]

    class IdleTransport:
        feed_mode = "test"

        async def connect(self):
            pass

        async def subscribe(self, **kwargs):
            pass

        async def close(self):
            pass

        async def recv(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(CryptoPerpPack, "create_history_connector", lambda *a, **kw: History())
    monkeypatch.setattr(CryptoPerpPack, "create_realtime_stream", lambda *a, **kw: IdleTransport())
    settings = AppSettings(
        data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", now_ms=lambda: now,
        backfill_bars=2, features=FeatureToggles(alerts=False, scanner=False),
    )

    async def subscribe() -> None:
        runtime = await build_runtime(settings)
        try:
            runtime.subscription_scheduler.acquire_chart((instrument.instrument_id, "1m"))
        finally:
            await runtime.aclose()

    asyncio.run(subscribe())
    with closing(ColdStore(settings.data_dir)) as store:
        bars = store.read_bars(instrument, "1m")
        assert [bar.ts_event for bar in bars] == [month_start - minute, month_start]
        assert all(bar.close == value and bar.revision == 0 for bar in bars)
        assert [
            generation.parquet_path.parent.name for generation in store.generations(instrument, "1m")
        ] == ["2025-11", "2025-12"]


@pytest.mark.medium
@pytest.mark.regression
def test_reconnect_history_only_fills_missing_cold_bars(tmp_path, fixture_dir):
    # Ticket 02: the recovery REST response can include an already stored realtime bar.
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    timestamp = 1_795_045_500_000
    minute = 60_000
    now = timestamp + 4 * minute
    settings = _idle_replay_settings(tmp_path, fixture_dir, lambda: now)

    def raw(ts, price):
        value = Decimal(price)
        return RawBar(
            symbol=instrument.symbol, timeframe="1m", ts_event=ts,
            open=value, high=value, low=value, close=value, volume=Decimal("1"), turnover=value,
            ts_available=now, ts_ingested=now, ts_committed=now,
        )

    async def run():
        async with _subscribed_supervisor(settings, instrument) as supervisor:
            supervisor.ingest(_confirmed_kline(instrument, timestamp, "101"))
            supervisor.transport_lost(now=now)
            supervisor.transport_restored(now=now, backfill=[
                raw(timestamp, "90"), raw(timestamp + minute, "95"),
            ])

    asyncio.run(run())
    rows = ColdReader(settings.data_dir).read_bars(instrument, "1m")
    assert [(bar.close, bar.revision, bar.ts_available) for bar in rows] == [
        (Decimal("101"), 0, now), (Decimal("95"), 0, timestamp + 2 * minute),
    ]


@pytest.mark.medium
@pytest.mark.regression
def test_cold_submission_failure_does_not_interrupt_realtime_feed(
    tmp_path, fixture_dir, monkeypatch, caplog,
):
    # Ticket 02: failure before a write starts must still deliver the confirmed event.
    class RejectingExecutor(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            raise OSError("write submission failed")

    monkeypatch.setattr("talaria.perception.cold_publisher.ThreadPoolExecutor", RejectingExecutor)
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    timestamp = 1_795_045_500_000
    now = timestamp + 60_000
    settings = _idle_replay_settings(tmp_path, fixture_dir, lambda: now)

    async def run():
        async with _subscribed_supervisor(settings, instrument) as supervisor:
            events = []
            supervisor.add_listener(events.append)
            event = _confirmed_kline(instrument, timestamp, "100")
            supervisor.ingest(event)
            assert [observed.bar for observed in events] == [event.bar]

    asyncio.run(run())
    assert "write submission failed" in caplog.text


@pytest.mark.medium
@pytest.mark.regression
def test_only_exchange_no_trades_bars_are_published_to_cold_store(tmp_path, fixture_dir):
    # Ticket 02: a missing minute is synthesized hot, while a real zero-volume close is archived.
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    start = 1_795_045_500_000
    minute = 60_000
    settings = _idle_replay_settings(tmp_path, fixture_dir, lambda: start + 3 * minute)

    async def run():
        async with _subscribed_supervisor(settings, instrument) as supervisor:
            supervisor.ingest(_confirmed_kline(instrument, start, "100"))
            supervisor.ingest(_confirmed_kline(
                instrument, start + 2 * minute, "100", volume="0", status=BarStatus.NO_TRADES,
            ))
            hot = supervisor.recent_bars(instrument_id=instrument.instrument_id, timeframe="1m")
            assert hot[1].timestamp == start + minute and hot[1].status is BarStatus.NO_TRADES

    asyncio.run(run())
    cold = ColdReader(settings.data_dir).read_bars(instrument, "1m")
    assert [(bar.ts_event, bar.volume) for bar in cold] == [
        (start, Decimal(1)), (start + 2 * minute, Decimal(0)),
    ]
