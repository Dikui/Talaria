"""快照构建时的冷库发布：只发可见柱，失败不阻塞快照。

来源：任务单 06 的 daemon 冷热一体验收与 lead review（m00382）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

import pytest

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import ColdStoreQualityError
from talaria.foundation.snapshot import SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import SnapshotStore

pytestmark = pytest.mark.medium

NOW_MS = 1_795_022_974_103


class RecordingPublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[RawBar]] = []
        self.fail = fail

    def publish_snapshot_bars(self, bars: Sequence[RawBar]):
        self.calls.append(list(bars))
        if self.fail:
            raise ColdStoreQualityError("模拟冷库质检失败")
        return None


@pytest.fixture
def cold_publisher():
    return RecordingPublisher()


@pytest.fixture
def builder(tmp_path, fixture_dir, cold_publisher):
    snapshot_store = SnapshotStore(tmp_path)
    instance = SnapshotBuilder(
        data_dir=tmp_path,
        connector=FixtureConnector(fixture_dir),
        raw_store=RawStore(tmp_path, raw_archive_id="test"),
        snapshot_store=snapshot_store,
        policy=BackfillPolicy(target_bars=3000, max_pages=5),
        now_ms=lambda: NOW_MS,
        cold_publishers={("BTC-USDT-PERP", "1m"): cold_publisher},
    )
    yield instance, snapshot_store
    snapshot_store.close()


def _request() -> SnapshotRequest:
    return SnapshotRequest(
        instrument_id="BTC-USDT-PERP", timeframes=["1m"], cutoff_ts=NOW_MS
    )


def test_builder_publishes_only_bars_visible_at_cutoff(builder, cold_publisher) -> None:
    builder_instance, _ = builder
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")

    meta = builder_instance.build(instrument=instrument, request=_request())

    assert meta.bar_counts == {"1m": 905}
    assert len(cold_publisher.calls) == 1
    published = cold_publisher.calls[0]
    assert published
    assert all(bar.ts_available <= NOW_MS for bar in published)
    assert [bar.ts_event for bar in published] == sorted(bar.ts_event for bar in published)


def test_cold_publish_failure_does_not_fail_snapshot(
    builder, cold_publisher, caplog
) -> None:
    builder_instance, snapshot_store = builder
    cold_publisher.fail = True
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")

    meta = builder_instance.build(instrument=instrument, request=_request())

    assert meta.bar_counts == {"1m": 905}
    assert snapshot_store.get(meta.snapshot_id) is not None
    assert cold_publisher.calls
    assert "冷库发布失败" in caplog.text


def test_builder_does_not_publish_hot_bars_beyond_cutoff(
    tmp_path, fixture_dir, cold_publisher
) -> None:
    future = RawBar(
        symbol="BTCUSDT",
        timeframe="1m",
        ts_event=NOW_MS + 120_000,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal("100.5"),
        volume=Decimal("1.0"),
        turnover=Decimal("100.5"),
        ts_available=NOW_MS + 180_000,
        ts_ingested=NOW_MS + 180_000,
        ts_committed=NOW_MS + 180_000,
    )

    class FutureHotBars:
        def recent_bars(self, *, instrument_id: str, timeframe: str):
            return [future] if (instrument_id, timeframe) == ("BTC-USDT-PERP", "1m") else []

    snapshot_store = SnapshotStore(tmp_path)
    builder_instance = SnapshotBuilder(
        data_dir=tmp_path,
        connector=FixtureConnector(fixture_dir),
        raw_store=RawStore(tmp_path, raw_archive_id="test"),
        snapshot_store=snapshot_store,
        policy=BackfillPolicy(target_bars=3000, max_pages=5),
        now_ms=lambda: NOW_MS,
        hot_bars=FutureHotBars(),
        cold_publishers={("BTC-USDT-PERP", "1m"): cold_publisher},
    )
    try:
        instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
        builder_instance.build(instrument=instrument, request=_request())
    finally:
        snapshot_store.close()

    assert cold_publisher.calls
    published = cold_publisher.calls[0]
    assert all(bar.ts_available <= NOW_MS for bar in published)
    assert future.ts_event not in {bar.ts_event for bar in published}

