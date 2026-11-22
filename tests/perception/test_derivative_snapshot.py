"""衍生品时点读取：严格 PIT、附属 Parquet、重启后按快照 ID 读回、空 OI。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.snapshot import SnapshotMode, SnapshotRequest
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import (
    SnapshotStore,
    read_derivative_points,
)

pytestmark = pytest.mark.medium

CUTOFF_MS = 1_793_550_000_000


@pytest.fixture
def builder(tmp_path, fixture_dir):
    connector = FixtureConnector(fixture_dir)
    cold = ColdStore(tmp_path)
    snapshot_store = SnapshotStore(tmp_path)
    policy = BackfillPolicy(target_bars=1000, max_pages=3)
    publisher = ColdPublisher(
        cold, instruments=CRYPTO_PERP_PACK.instruments(), connector=connector,
        policy=policy, now_ms=lambda: CUTOFF_MS,
    )
    yield SnapshotBuilder(
        data_dir=tmp_path,
        connector=connector,
        raw_store=RawStore(tmp_path, raw_archive_id="test"),
        snapshot_store=snapshot_store,
        policy=policy,
        now_ms=lambda: CUTOFF_MS,
        cold_publisher=publisher,
    )
    asyncio.run(publisher.close())
    snapshot_store.close()
    cold.close()


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


def test_sidecar_is_registered_and_strictly_truncated_at_available_time(tmp_path, builder) -> None:
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


def test_sidecar_survives_restart_and_is_read_by_manifest_only(tmp_path, builder) -> None:
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


def test_new_listing_without_oi_history_still_materializes_mark_price(tmp_path, builder) -> None:
    meta = _build(builder, "1000BONK-USDT-PERP", ["1m"])
    record = builder._snapshot_store.get(meta.snapshot_id)
    assert record is not None and record.derivatives_path is not None
    points = read_derivative_points(tmp_path, record.derivatives_path)
    assert any(point.kind is DerivativeKind.MARK_PRICE for point in points)
    assert not any(point.kind is DerivativeKind.OPEN_INTEREST for point in points)
    assert not any(point.kind is DerivativeKind.FUNDING_RATE for point in points)


def test_derivative_series_respect_timeframe_mapping(tmp_path, builder) -> None:
    meta = _build(builder, "BTC-USDT-PERP", ["1m"])
    record = builder._snapshot_store.get(meta.snapshot_id)
    assert record is not None and record.derivatives_path is not None
    points = read_derivative_points(tmp_path, record.derivatives_path)
    oi = [point for point in points if point.kind is DerivativeKind.OPEN_INTEREST]
    mark = [point for point in points if point.kind is DerivativeKind.MARK_PRICE]
    assert oi and all(point.timeframe == "5m" for point in oi)
    assert len(mark) >= len(oi)


@pytest.mark.regression
@pytest.mark.parametrize("cold_enabled", [False, True])
def test_snapshot_preserves_the_main_branch_derivative_history(tmp_path, fixture_dir, cold_enabled):
    # data-access/08：通过服务开关验证主线直接取数路径仍可用。
    from fastapi.testclient import TestClient

    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    app = create_app(AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        token="test-token", now_ms=lambda: CUTOFF_MS, backfill_bars=10,
        features=FeatureToggles(market_data=True, cold_store=cold_enabled, scanner=False),
    ))
    with TestClient(app) as client:
        result = client.post("/api/snapshots", headers={"Authorization": "Bearer test-token"}, json={
            "instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"], "cutoff_ts": CUTOFF_MS,
        })
        assert result.status_code == 200, result.text
        bundle = client.get(
            f"/api/snapshots/{result.json()['snapshot_id']}/derivatives", params={"timeframe": "1m"},
        )
        assert bundle.status_code == 200, bundle.text
        # 使用 main 的 SnapshotBuilder 跑同一 BTC fixture 得到的整页历史。
        assert {
            series["kind"]: (len(points), points[0]["ts_event"], points[-1]["ts_event"])
            for series in bundle.json()["series"] if (points := series["points"])
        } == {
            "mark_price": (900, 1793491200000, 1793545440000),
            "funding_rate": (30, 1792656000000, 1793491200000),
            "open_interest": (196, 1793491200000, 1793549700000),
        }
