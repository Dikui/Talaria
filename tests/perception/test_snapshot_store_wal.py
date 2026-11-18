"""实时快照清单库：WAL、单写者守卫与关闭 checkpoint。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from talaria.foundation.errors import SingleWriterViolationError
from talaria.foundation.snapshot import SnapshotMeta
from talaria.perception.snapshot_store import (
    SNAPSHOT_DB,
    SnapshotStore,
    write_snapshot_parquet,
)

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103


def _meta(snapshot_id: str = "snap-1") -> SnapshotMeta:
    return SnapshotMeta(
        snapshot_id=snapshot_id,
        instrument_id="BTC-USDT-PERP",
        timeframes=["1m"],
        cutoff_ts=NOW_MS,
        created_ts=NOW_MS,
        mode="HISTORICAL_PIT",
        bar_counts={"1m": 0},
    )


def test_manifest_uses_wal_and_rejects_second_writer(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    try:
        with pytest.raises(SingleWriterViolationError):
            SnapshotStore(tmp_path)
    finally:
        store.close()

    connection = sqlite3.connect(tmp_path / SNAPSHOT_DB)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_close_checkpoints_wal_and_records_survive_reopen(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    relative = write_snapshot_parquet(tmp_path, "snap-1", [])
    store.register(_meta(), relative, committed_ts=NOW_MS)
    store.close()

    wal = Path(f"{tmp_path / SNAPSHOT_DB}-wal")
    assert not wal.exists() or wal.stat().st_size == 0

    reopened = SnapshotStore(tmp_path)
    try:
        record = reopened.get("snap-1")
    finally:
        reopened.close()
    assert record is not None
    assert record.parquet_path == relative
    assert record.committed_ts == NOW_MS
