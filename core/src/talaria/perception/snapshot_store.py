"""快照物化与清单：Parquet 一次性原子写出，SQLite（WAL）登记。

读取只使用清单中显式记录的 ``parquet_path``，禁止目录通配。快照 Parquet 在
02 的列基础上新增 ``ts_committed``；读取侧兼容旧文件（缺列时该字段为 ``None``）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.errors import SnapshotNotFoundError
from talaria.foundation.snapshot import SnapshotMeta
from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    release_writer,
)
from talaria.foundation.types import PrecisionTier

STATE_SUBDIR = Path("state")
SNAPSHOT_SUBDIR = STATE_SUBDIR / "hot_snapshots"
SNAPSHOT_DB = STATE_SUBDIR / "runtime_snapshots.sqlite"

_SNAPSHOT_SCHEMA = pa.schema(
    [
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("timestamp", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=True),
        pa.field("high", pa.string(), nullable=True),
        pa.field("low", pa.string(), nullable=True),
        pa.field("close", pa.string(), nullable=True),
        pa.field("volume", pa.string(), nullable=True),
        pa.field("turnover", pa.string(), nullable=True),
        pa.field("status", pa.string(), nullable=False),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=True),
        pa.field("precision", pa.string(), nullable=False),
    ]
)


@dataclass(frozen=True, slots=True)
class SnapshotBar:
    """快照中的一根紧凑柱及其 Committed Time（旧文件可能缺失）。"""

    bar: CompactBar
    ts_committed: int | None = None


@dataclass(frozen=True, slots=True)
class SnapshotRecord:
    meta: SnapshotMeta
    parquet_path: Path
    committed_ts: int | None = None


def _decimal_or_none(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def write_snapshot_parquet(
    data_dir: Path | str,
    snapshot_id: str,
    frames: Sequence[tuple[str, Sequence[SnapshotBar]]],
) -> Path:
    """把多个周期的柱写进同一个 Parquet；返回相对 data_dir 的路径。"""
    root = Path(data_dir)
    directory = root / SNAPSHOT_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / f"{snapshot_id}.parquet"
    if final.exists():
        raise FileExistsError(f"快照文件已存在，拒绝覆盖：{final}")

    rows = [
        {
            "timeframe": timeframe,
            "timestamp": entry.bar.timestamp,
            "open": _decimal_or_none(entry.bar.open),
            "high": _decimal_or_none(entry.bar.high),
            "low": _decimal_or_none(entry.bar.low),
            "close": _decimal_or_none(entry.bar.close),
            "volume": _decimal_or_none(entry.bar.volume),
            "turnover": _decimal_or_none(entry.bar.turnover),
            "status": entry.bar.status.value,
            "ts_available": entry.bar.ts_available,
            "ts_committed": entry.ts_committed,
            "precision": entry.bar.precision.value,
        }
        for timeframe, entries in frames
        for entry in entries
    ]
    table = pa.Table.from_pylist(rows, schema=_SNAPSHOT_SCHEMA)

    temporary = directory / f".{snapshot_id}.{uuid.uuid4().hex}.tmp"
    try:
        pq.write_table(table, temporary, compression="zstd")
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return final.relative_to(root)


def _path_under_data_dir(data_dir: Path, relative: Path) -> Path:
    root = data_dir.resolve()
    candidate = (data_dir / relative).resolve()
    if not candidate.is_relative_to(root):
        raise SnapshotNotFoundError("快照清单路径越界")
    return candidate


def read_snapshot_entries(
    data_dir: Path | str,
    parquet_path: Path,
    timeframe: str,
) -> list[SnapshotBar]:
    """只读取清单给出的文件；按周期过滤并升序返回（含 Committed Time）。"""
    root = Path(data_dir)
    path = _path_under_data_dir(root, parquet_path)
    if not path.is_file():
        raise SnapshotNotFoundError(f"快照文件缺失：{parquet_path}")
    entries: list[SnapshotBar] = []
    for row in pq.read_table(path).to_pylist():
        if row["timeframe"] != timeframe:
            continue
        committed = row.get("ts_committed")
        entries.append(
            SnapshotBar(
                bar=CompactBar(
                    timestamp=int(row["timestamp"]),
                    open=None if row["open"] is None else Decimal(row["open"]),
                    high=None if row["high"] is None else Decimal(row["high"]),
                    low=None if row["low"] is None else Decimal(row["low"]),
                    close=None if row["close"] is None else Decimal(row["close"]),
                    volume=None if row["volume"] is None else Decimal(row["volume"]),
                    turnover=None if row["turnover"] is None else Decimal(row["turnover"]),
                    status=BarStatus(row["status"]),
                    ts_available=int(row["ts_available"]),
                    precision=PrecisionTier(row["precision"]),
                ),
                ts_committed=None if committed is None else int(committed),
            )
        )
    entries.sort(key=lambda entry: entry.bar.timestamp)
    return entries


def read_snapshot_bars(
    data_dir: Path | str,
    parquet_path: Path,
    timeframe: str,
) -> list[CompactBar]:
    """只读取清单给出的文件；按周期过滤并升序返回。"""
    return [
        entry.bar for entry in read_snapshot_entries(data_dir, parquet_path, timeframe)
    ]


class SnapshotStore:
    """SQLite 清单；连接可跨线程使用，由锁串行化，且同进程只允许一个写者。"""

    def __init__(self, data_dir: Path | str) -> None:
        self._data_dir = Path(data_dir)
        db_path = self._data_dir / SNAPSHOT_DB
        self._db_path = db_path
        self._lock = threading.Lock()
        self._closed = False
        claim_writer(db_path)
        try:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(db_path, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA busy_timeout=5000")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    instrument_id TEXT NOT NULL,
                    timeframes TEXT NOT NULL,
                    cutoff_ts INTEGER NOT NULL,
                    created_ts INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    bar_counts TEXT NOT NULL,
                    parquet_path TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(snapshots)")
            }
            if "committed_ts" not in columns:
                self._connection.execute(
                    "ALTER TABLE snapshots ADD COLUMN committed_ts INTEGER"
                )
            self._connection.commit()
        except BaseException:
            release_writer(db_path)
            raise

    def register(
        self,
        meta: SnapshotMeta,
        parquet_path: Path,
        *,
        committed_ts: int | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO snapshots (
                    snapshot_id, instrument_id, timeframes, cutoff_ts,
                    created_ts, mode, bar_counts, parquet_path, committed_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    meta.snapshot_id,
                    meta.instrument_id,
                    json.dumps(meta.timeframes, ensure_ascii=False),
                    meta.cutoff_ts,
                    meta.created_ts,
                    meta.mode.value,
                    json.dumps(meta.bar_counts, ensure_ascii=False),
                    parquet_path.as_posix(),
                    committed_ts,
                ),
            )
            self._connection.commit()

    def get(self, snapshot_id: str) -> SnapshotRecord | None:
        with self._lock:
            row = self._connection.execute(
                """
                SELECT instrument_id, timeframes, cutoff_ts, created_ts,
                       mode, bar_counts, parquet_path, committed_ts
                FROM snapshots WHERE snapshot_id = ?
                """,
                (snapshot_id,),
            ).fetchone()
        if row is None:
            return None
        meta = SnapshotMeta(
            snapshot_id=snapshot_id,
            instrument_id=row[0],
            timeframes=json.loads(row[1]),
            cutoff_ts=row[2],
            created_ts=row[3],
            mode=row[4],
            bar_counts=json.loads(row[5]),
        )
        return SnapshotRecord(
            meta=meta,
            parquet_path=Path(row[6]),
            committed_ts=None if row[7] is None else int(row[7]),
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                checkpoint_and_close(self._connection)
            finally:
                release_writer(self._db_path)
