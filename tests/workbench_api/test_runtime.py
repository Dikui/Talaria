from __future__ import annotations

import asyncio
import json
import time
import threading
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import CompactBar
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
