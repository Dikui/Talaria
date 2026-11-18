"""衍生品时点读取：严格 PIT、附属 Parquet、重启后按快照 ID 读回、空 OI。"""

from __future__ import annotations

from pathlib import Path

import pytest

from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.snapshot import SnapshotMode, SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import (
    SnapshotStore,
    read_derivative_points,
)

pytestmark = pytest.mark.medium

CUTOFF_MS = 1_793_550_000_000


def _builder(tmp_path, fixture_dir) -> SnapshotBuilder:
    connector = FixtureConnector(fixture_dir)
    return SnapshotBuilder(
        data_dir=tmp_path,
        connector=connector,
        raw_store=RawStore(tmp_path),
        snapshot_store=SnapshotStore(tmp_path),
        policy=BackfillPolicy(target_bars=1000, max_pages=3),
        now_ms=lambda: CUTOFF_MS,
    )


def _build(builder: SnapshotBuilder, instrument_id: str, timeframes: list[str]):
    return builder.build(
        instrument=CRYPTO_PERP_PACK.instrument(instrument_id),
        request=SnapshotRequest(
            instrument_id=instrument_id,
            timeframes=timeframes,
            cutoff_ts=CUTOFF_MS,
            mode=SnapshotMode.HISTORICAL_PIT,
        ),
    )


def test_sidecar_is_registered_and_strictly_truncated_at_available_time(tmp_path, fixture_dir) -> None:
    builder = _builder(tmp_path, fixture_dir)
    meta = _build(builder, "BTC-USDT-PERP", ["1m"])
    record = builder._snapshot_store.get(meta.snapshot_id)
    assert record is not None
    assert record.derivatives_path == Path("state") / "hot_snapshots" / f"{meta.snapshot_id}.derivatives.parquet"

    points = read_derivative_points(tmp_path, record.derivatives_path)
    assert {point.kind for point in points} == {
        DerivativeKind.MARK_PRICE,
        DerivativeKind.FUNDING_RATE,
        DerivativeKind.OPEN_INTEREST,
    }
    assert all(point.ts_available <= meta.cutoff_ts for point in points)
    assert all(point.ts_committed >= point.ts_ingested for point in points)
    assert all(point.ts_ingested <= CUTOFF_MS for point in points)
    funding = [point for point in points if point.kind is DerivativeKind.FUNDING_RATE]
    assert funding and all(point.ts_published == point.ts_event for point in funding)
    assert all(point.timeframe == "1m" for point in points if point.kind is DerivativeKind.MARK_PRICE)
    assert all(point.timeframe is None for point in funding)


def test_sidecar_survives_restart_and_is_read_by_manifest_only(tmp_path, fixture_dir) -> None:
    builder = _builder(tmp_path, fixture_dir)
    meta = _build(builder, "BTC-USDT-PERP", ["1m"])
    before = builder._snapshot_store.get(meta.snapshot_id)
    assert before is not None and before.derivatives_path is not None
    expected = read_derivative_points(tmp_path, before.derivatives_path)
    builder._snapshot_store.close()

    # 干扰文件不得参与读回；清单路径仍是唯一入口。
    (tmp_path / "state" / "hot_snapshots" / "stray.derivatives.parquet").write_bytes(b"x")
    reopened = SnapshotStore(tmp_path)
    try:
        after = reopened.get(meta.snapshot_id)
        assert after is not None and after.derivatives_path == before.derivatives_path
        assert read_derivative_points(tmp_path, after.derivatives_path) == expected
    finally:
        reopened.close()


def test_new_listing_without_oi_history_still_materializes_mark_price(tmp_path, fixture_dir) -> None:
    builder = _builder(tmp_path, fixture_dir)
    meta = _build(builder, "1000BONK-USDT-PERP", ["1m"])
    record = builder._snapshot_store.get(meta.snapshot_id)
    assert record is not None and record.derivatives_path is not None
    points = read_derivative_points(tmp_path, record.derivatives_path)
    assert any(point.kind is DerivativeKind.MARK_PRICE for point in points)
    assert not any(point.kind is DerivativeKind.OPEN_INTEREST for point in points)
    assert not any(point.kind is DerivativeKind.FUNDING_RATE for point in points)


def test_derivative_series_respect_timeframe_mapping(tmp_path, fixture_dir) -> None:
    builder = _builder(tmp_path, fixture_dir)
    meta = _build(builder, "BTC-USDT-PERP", ["1m"])
    record = builder._snapshot_store.get(meta.snapshot_id)
    assert record is not None and record.derivatives_path is not None
    points = read_derivative_points(tmp_path, record.derivatives_path)
    oi = [point for point in points if point.kind is DerivativeKind.OPEN_INTEREST]
    mark = [point for point in points if point.kind is DerivativeKind.MARK_PRICE]
    assert oi and all(point.timeframe == "5m" for point in oi)
    assert len(mark) >= len(oi)
