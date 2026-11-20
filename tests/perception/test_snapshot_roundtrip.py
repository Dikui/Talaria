"""快照物化：Parquet 逐字段读回、FEED_GAP/NO_TRADES、不可覆盖与显式路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from talaria.foundation.bars import BarStatus
from talaria.foundation.snapshot import SnapshotMode, SnapshotRequest
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.compaction import compact_series
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import (
    SnapshotStore,
    read_snapshot_bars,
    write_snapshot_parquet,
)
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector

pytestmark = pytest.mark.medium

NOW_MS = 1_795_022_974_103


@pytest.fixture
def builder(tmp_path, fixture_dir):
    connector = FixtureConnector(fixture_dir)
    raw_store = RawStore(tmp_path, raw_archive_id="test")
    snapshot_store = SnapshotStore(tmp_path)
    instance = SnapshotBuilder(
        data_dir=tmp_path,
        connector=connector,
        raw_store=raw_store,
        snapshot_store=snapshot_store,
        policy=BackfillPolicy(target_bars=3000, max_pages=5),
        now_ms=lambda: NOW_MS,
    )
    yield instance, raw_store, snapshot_store
    snapshot_store.close()


def test_build_materializes_gap_and_no_trades(builder) -> None:
    builder_instance, raw_store, snapshot_store = builder
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    request = SnapshotRequest(
        instrument_id="BTC-USDT-PERP",
        timeframes=["1m"],
        cutoff_ts=NOW_MS,
        mode=SnapshotMode.HISTORICAL_PIT,
    )
    meta = builder_instance.build(instrument=instrument, request=request)
    record = snapshot_store.get(meta.snapshot_id)
    assert record is not None
    assert record.parquet_path == Path("state") / "hot_snapshots" / f"{meta.snapshot_id}.parquet"

    bars = read_snapshot_bars(builder_instance._data_dir, record.parquet_path, "1m")
    assert meta.bar_counts == {"1m": 905}
    assert len(bars) == 905
    assert any(bar.status is BarStatus.FEED_GAP for bar in bars)
    assert any(bar.status is BarStatus.NO_TRADES for bar in bars)

    expected = compact_series(
        raw_store.read(instrument.symbol, "1m"), timeframe="1m", cutoff_ts=NOW_MS
    )
    assert bars == expected


def test_read_back_is_fieldwise_identical_to_frozen_meta(builder) -> None:
    builder_instance, raw_store, snapshot_store = builder
    instrument = CRYPTO_PERP_PACK.instrument("ETH-USDT-PERP")
    request = SnapshotRequest(
        instrument_id="ETH-USDT-PERP", timeframes=["1m", "5m"], cutoff_ts=NOW_MS
    )
    meta = builder_instance.build(instrument=instrument, request=request)
    stored = snapshot_store.get(meta.snapshot_id)
    assert stored is not None
    assert stored.meta == meta
    assert stored.meta.timeframes == ["1m", "5m"]
    assert stored.meta.bar_counts == {"1m": 400, "5m": 240}
    assert read_snapshot_bars(builder_instance._data_dir, stored.parquet_path, "1m") == compact_series(
        raw_store.read("ETHUSDT", "1m"), timeframe="1m", cutoff_ts=NOW_MS
    )
    assert read_snapshot_bars(builder_instance._data_dir, stored.parquet_path, "5m") == compact_series(
        raw_store.read("ETHUSDT", "5m"), timeframe="5m", cutoff_ts=NOW_MS
    )


def test_parquet_file_cannot_be_overwritten(builder, tmp_path) -> None:
    write_snapshot_parquet(tmp_path, "fixed", [])
    with pytest.raises(FileExistsError):
        write_snapshot_parquet(tmp_path, "fixed", [])


def test_reader_uses_manifest_path_not_directory_glob(builder, tmp_path) -> None:
    builder_instance, _, snapshot_store = builder
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    meta = builder_instance.build(
        instrument=instrument,
        request=SnapshotRequest(instrument_id=instrument.instrument_id, timeframes=["1m"], cutoff_ts=NOW_MS),
    )
    record = snapshot_store.get(meta.snapshot_id)
    assert record is not None
    stray = tmp_path / "state" / "hot_snapshots" / "stray.parquet"
    stray.write_bytes(b"not a parquet file")
    bars = read_snapshot_bars(tmp_path, record.parquet_path, "1m")
    assert bars[0].timestamp == min(bar.timestamp for bar in bars)


def test_reader_rejects_path_outside_data_dir(tmp_path) -> None:
    from talaria.foundation.errors import SnapshotNotFoundError

    with pytest.raises(SnapshotNotFoundError):
        read_snapshot_bars(tmp_path, Path("../outside.parquet"), "1m")
