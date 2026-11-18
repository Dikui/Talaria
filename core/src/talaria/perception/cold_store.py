"""冷库原子发布：内容哈希归档 → staging 质检 → 不可变 generation → 清单短事务。

发布顺序与崩溃语义（docs/architecture.md §3.2）：

1. 原始标准化柱按内容哈希写入 ``data/raw_archive/<sha256>.jsonl``；同内容复用；
2. 写入 ``data/staging/<generation_id>/bars.parquet``，读回后做完整性、单调性与
   精度质检；失败只留下未引用残片，绝不进入清单；
3. ``fsync`` 后原子 rename 到 ``data/cold/<venue>/<symbol>/<timeframe>/<id>.parquet``；
4. 在 ``data/data_manifest.sqlite`` 的短事务中插入 generation 清单并更新游标。

``data_manifest.sqlite`` 是读取侧唯一发布真相：读取只按清单显式列出的路径打开
Parquet，禁止目录通配。任何阶段崩溃最多留下未引用的孤儿文件，读取侧看不到
半成品批次。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import ColdStoreError, ColdStoreIntegrityError, ColdStoreQualityError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.sqlite import (
    checkpoint_and_close,
    claim_writer,
    open_writer_connection,
    release_writer,
)
from talaria.foundation.types import PrecisionTier
from talaria.perception.raw_store import bar_record

ARCHIVE_SUBDIR = Path("data") / "raw_archive"
STAGING_SUBDIR = Path("data") / "staging"
COLD_SUBDIR = Path("data") / "cold"
MANIFEST_DB = Path("data") / "data_manifest.sqlite"


class ColdPhase(StrEnum):
    """可在其前后注入故障的发布阶段。"""

    ARCHIVE_BEFORE = "archive.before"
    ARCHIVE_AFTER = "archive.after"
    STAGING_BEFORE = "staging.before"
    STAGING_AFTER = "staging.after"
    QC_BEFORE = "qc.before"
    QC_AFTER = "qc.after"
    RENAME_BEFORE = "rename.before"
    RENAME_AFTER = "rename.after"
    MANIFEST_BEFORE = "manifest.before"
    MANIFEST_AFTER = "manifest.after"


FaultHook = Callable[[ColdPhase], None]


@dataclass(frozen=True, slots=True)
class ColdBatch:
    """一次待发布的标准化柱批次；时间戳必须在批次内严格递增。"""

    instrument: InstrumentSpec
    timeframe: str
    bars: Sequence[RawBar]


@dataclass(frozen=True, slots=True)
class ArchivedBatch:
    """内容哈希归档结果；路径相对数据根目录。"""

    path: Path
    sha256: str
    bytes: int


@dataclass(frozen=True, slots=True)
class ColdGeneration:
    """一条已发布、可由清单复现的不可变 generation。"""

    generation_id: str
    venue: str
    symbol: str
    timeframe: str
    parquet_path: Path
    sha256: str
    content_sha256: str
    first_ts: int
    last_ts: int
    bars: int
    committed_ts: int


@dataclass(frozen=True, slots=True)
class ColdCursor:
    """每 ``(instrument, timeframe)`` 的最新已发布游标。"""

    venue: str
    symbol: str
    timeframe: str
    generation_id: str
    last_ts: int
    bars: int
    updated_ts: int


_COLD_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
        pa.field("turnover", pa.string(), nullable=False),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
    ]
)

_SCHEMA_SQL = (
    """
    CREATE TABLE IF NOT EXISTS generations (
        generation_id TEXT NOT NULL,
        venue TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        parquet_path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        first_ts INTEGER NOT NULL,
        last_ts INTEGER NOT NULL,
        bars INTEGER NOT NULL,
        committed_ts INTEGER NOT NULL,
        PRIMARY KEY (venue, symbol, timeframe, generation_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cursors (
        venue TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        generation_id TEXT NOT NULL,
        last_ts INTEGER NOT NULL,
        bars INTEGER NOT NULL,
        updated_ts INTEGER NOT NULL,
        PRIMARY KEY (venue, symbol, timeframe)
    )
    """,
)

_GENERATION_COLUMNS = (
    "generation_id, parquet_path, sha256, content_sha256, "
    "first_ts, last_ts, bars, committed_ts"
)


def _content_bytes(bars: Iterable[RawBar]) -> bytes:
    return b"".join(
        (json.dumps(bar_record(bar), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        for bar in bars
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes) -> None:
    """同目录临时文件 + fsync + rename，保证读者只看到完整内容。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_path_part(value: str, label: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ColdStoreError(f"非法的{label}：{value!r}")
    return value


def _validate_bars(
    bars: Sequence[RawBar],
    *,
    instrument: InstrumentSpec,
    timeframe: str,
) -> None:
    """完整性、单调性、OHLC 合法性与价格/数量精度质检。"""

    if not bars:
        raise ColdStoreQualityError("批次为空，拒绝发布")
    tick_size = instrument.tick_size
    qty_step = instrument.qty_step
    previous_ts: int | None = None
    for index, bar in enumerate(bars):
        where = f"第 {index} 根柱"
        if bar.symbol != instrument.symbol:
            raise ColdStoreQualityError(f"{where} symbol 与标的规格不一致")
        if bar.timeframe != timeframe:
            raise ColdStoreQualityError(f"{where} timeframe 与批次不一致")
        if previous_ts is not None and bar.ts_event <= previous_ts:
            raise ColdStoreQualityError(f"{where} 时间戳未严格递增或重复：{bar.ts_event}")
        if bar.high < bar.low:
            raise ColdStoreQualityError(f"{where} high 低于 low")
        if bar.high < max(bar.open, bar.close):
            raise ColdStoreQualityError(f"{where} high 低于 open/close")
        if bar.low > min(bar.open, bar.close):
            raise ColdStoreQualityError(f"{where} low 高于 open/close")
        if bar.volume < 0 or bar.turnover < 0:
            raise ColdStoreQualityError(f"{where} volume/turnover 为负")
        for name, price in (
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
        ):
            if price % tick_size != 0:
                raise ColdStoreQualityError(f"{where} {name} 未对齐 tick_size={tick_size}")
        if bar.volume % qty_step != 0:
            raise ColdStoreQualityError(f"{where} volume 未对齐 qty_step={qty_step}")
        if bar.ts_available < bar.ts_event:
            raise ColdStoreQualityError(f"{where} ts_available 早于 ts_event")
        if bar.ts_committed < bar.ts_ingested:
            raise ColdStoreQualityError(f"{where} ts_committed 早于 ts_ingested")
        previous_ts = bar.ts_event


def _write_cold_parquet(path: Path, bars: Sequence[RawBar]) -> None:
    rows = [
        {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "ts_event": bar.ts_event,
            "open": format(bar.open, "f"),
            "high": format(bar.high, "f"),
            "low": format(bar.low, "f"),
            "close": format(bar.close, "f"),
            "volume": format(bar.volume, "f"),
            "turnover": format(bar.turnover, "f"),
            "ts_available": bar.ts_available,
            "ts_ingested": bar.ts_ingested,
            "ts_committed": bar.ts_committed,
            "precision": bar.precision.value,
        }
        for bar in bars
    ]
    table = pa.Table.from_pylist(rows, schema=_COLD_SCHEMA)
    pq.write_table(table, path, compression="zstd")


def _read_cold_parquet(path: Path) -> list[RawBar]:
    return [
        RawBar(
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            ts_event=int(row["ts_event"]),
            open=Decimal(str(row["open"])),
            high=Decimal(str(row["high"])),
            low=Decimal(str(row["low"])),
            close=Decimal(str(row["close"])),
            volume=Decimal(str(row["volume"])),
            turnover=Decimal(str(row["turnover"])),
            ts_available=int(row["ts_available"]),
            ts_ingested=int(row["ts_ingested"]),
            ts_committed=int(row["ts_committed"]),
            precision=PrecisionTier(str(row["precision"])),
        )
        for row in pq.read_table(path).to_pylist()
    ]


class ColdStore:
    """冷库单写者。``data_manifest.sqlite`` 是发布与读取的唯一真相。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self._closed = False
        self._db_path = self._data_dir / MANIFEST_DB
        claim_writer(self._db_path)
        try:
            self._connection = open_writer_connection(self._db_path)
            for statement in _SCHEMA_SQL:
                self._connection.execute(statement)
        except BaseException:
            release_writer(self._db_path)
            raise

    # ------------------------------------------------------------------ 公开接口

    def archive(self, batch: ColdBatch) -> ArchivedBatch:
        """按标准化柱内容哈希归档；同内容复用，已有文件哈希不符则拒绝。"""

        with self._lock:
            self._ensure_open()
            content = _content_bytes(batch.bars)
            return self._archive_content(content, _sha256_bytes(content))

    def publish(self, batch: ColdBatch) -> ColdGeneration:
        """原子发布一个批次；同内容重复发布返回已有 generation。"""

        with self._lock:
            self._ensure_open()
            if not batch.bars:
                raise ColdStoreQualityError("批次为空，拒绝发布")
            content = _content_bytes(batch.bars)
            generation_id = _sha256_bytes(content)
            existing = self._select_generation(batch, generation_id)
            if existing is not None:
                self._verify_generation_file(existing)
                return existing

            archived = self._archive_content(content, generation_id)
            self._write_staging(archived, batch.bars)
            self._quality_check(archived, batch)
            final = self._promote(archived, batch)
            generation = ColdGeneration(
                generation_id=generation_id,
                venue=batch.instrument.venue,
                symbol=batch.instrument.symbol,
                timeframe=batch.timeframe,
                parquet_path=final.relative_to(self._data_dir),
                sha256=_sha256_file(final),
                content_sha256=generation_id,
                first_ts=batch.bars[0].ts_event,
                last_ts=batch.bars[-1].ts_event,
                bars=len(batch.bars),
                committed_ts=max(bar.ts_committed for bar in batch.bars),
            )
            self._commit_manifest(generation)
            self._hook(ColdPhase.MANIFEST_AFTER)
            return generation

    def generations(self, instrument: InstrumentSpec, timeframe: str) -> list[ColdGeneration]:
        with self._lock:
            self._ensure_open()
            return self._select_generations(instrument, timeframe)

    def read_bars(self, instrument: InstrumentSpec, timeframe: str) -> list[RawBar]:
        """只读取清单列出的文件；逐文件校验哈希，同 timestamp 后发布者胜。"""

        with self._lock:
            self._ensure_open()
            merged: dict[int, RawBar] = {}
            for generation in self._select_generations(instrument, timeframe):
                path = self._path_under_root(generation.parquet_path)
                if not path.is_file():
                    raise ColdStoreIntegrityError(f"generation 文件缺失：{generation.parquet_path}")
                if _sha256_file(path) != generation.sha256:
                    raise ColdStoreIntegrityError(
                        f"generation 文件哈希不符：{generation.parquet_path}"
                    )
                for bar in _read_cold_parquet(path):
                    merged[bar.ts_event] = bar
            return [merged[key] for key in sorted(merged)]

    def cursor(self, instrument: InstrumentSpec, timeframe: str) -> ColdCursor | None:
        with self._lock:
            self._ensure_open()
            row = self._connection.execute(
                """
                SELECT generation_id, last_ts, bars, updated_ts
                FROM cursors WHERE venue = ? AND symbol = ? AND timeframe = ?
                """,
                (instrument.venue, instrument.symbol, timeframe),
            ).fetchone()
        if row is None:
            return None
        return ColdCursor(
            venue=instrument.venue,
            symbol=instrument.symbol,
            timeframe=timeframe,
            generation_id=str(row[0]),
            last_ts=int(row[1]),
            bars=int(row[2]),
            updated_ts=int(row[3]),
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

    # ------------------------------------------------------------------ 发布阶段

    def _archive_content(self, content: bytes, sha256: str) -> ArchivedBatch:
        self._hook(ColdPhase.ARCHIVE_BEFORE)
        path = self._data_dir / ARCHIVE_SUBDIR / f"{sha256}.jsonl"
        if path.exists():
            if _sha256_file(path) != sha256:
                raise ColdStoreIntegrityError(f"归档内容哈希不符：{path.relative_to(self._data_dir)}")
        else:
            _atomic_write(path, content)
        self._hook(ColdPhase.ARCHIVE_AFTER)
        return ArchivedBatch(
            path=path.relative_to(self._data_dir),
            sha256=sha256,
            bytes=len(content),
        )

    def _staging_file(self, archived: ArchivedBatch) -> Path:
        return self._data_dir / STAGING_SUBDIR / archived.sha256 / "bars.parquet"

    def _write_staging(self, archived: ArchivedBatch, bars: Sequence[RawBar]) -> None:
        self._hook(ColdPhase.STAGING_BEFORE)
        staging_dir = (self._data_dir / STAGING_SUBDIR) / archived.sha256
        # 上一次崩溃可能留下残片；发布永远从干净 staging 重建，绝不信任旧文件。
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        temporary = staging_dir / "bars.parquet.tmp"
        _write_cold_parquet(temporary, bars)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, staging_dir / "bars.parquet")
        _fsync_directory(staging_dir)
        self._hook(ColdPhase.STAGING_AFTER)

    def _quality_check(self, archived: ArchivedBatch, batch: ColdBatch) -> None:
        self._hook(ColdPhase.QC_BEFORE)
        _validate_bars(batch.bars, instrument=batch.instrument, timeframe=batch.timeframe)
        # 读回落盘后的列式文件再做一次，防止写出形状本身破坏精度/时点。
        decoded = _read_cold_parquet(self._staging_file(archived))
        _validate_bars(decoded, instrument=batch.instrument, timeframe=batch.timeframe)
        if decoded != list(batch.bars):
            raise ColdStoreIntegrityError("staging Parquet 读回内容与输入批次不一致")
        self._hook(ColdPhase.QC_AFTER)

    def _generation_file(self, batch: ColdBatch, archived: ArchivedBatch) -> Path:
        venue = _safe_path_part(batch.instrument.venue, "venue")
        symbol = _safe_path_part(batch.instrument.symbol, "symbol")
        timeframe = _safe_path_part(batch.timeframe, "timeframe")
        return (
            self._data_dir
            / COLD_SUBDIR
            / venue
            / symbol
            / timeframe
            / f"{archived.sha256}.parquet"
        )

    def _promote(self, archived: ArchivedBatch, batch: ColdBatch) -> Path:
        self._hook(ColdPhase.RENAME_BEFORE)
        final = self._generation_file(batch, archived)
        final.parent.mkdir(parents=True, exist_ok=True)
        staging = self._staging_file(archived)
        if final.exists():
            # 上次崩溃可能已完成 rename 但未提交清单；校验一致后复用不可变文件。
            if _sha256_file(final) != _sha256_file(staging):
                raise ColdStoreIntegrityError(f"不可变 generation 已存在且内容不符：{final}")
            staging.unlink(missing_ok=True)
        else:
            os.replace(staging, final)
            _fsync_directory(final.parent)
        self._hook(ColdPhase.RENAME_AFTER)
        with suppress(OSError):
            shutil.rmtree(staging.parent)
        return final

    def _commit_manifest(self, generation: ColdGeneration) -> None:
        self._hook(ColdPhase.MANIFEST_BEFORE)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._select_generation_by_parts(generation)
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO generations (
                        generation_id, venue, symbol, timeframe, parquet_path,
                        sha256, content_sha256, first_ts, last_ts, bars, committed_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generation.generation_id,
                        generation.venue,
                        generation.symbol,
                        generation.timeframe,
                        generation.parquet_path.as_posix(),
                        generation.sha256,
                        generation.content_sha256,
                        generation.first_ts,
                        generation.last_ts,
                        generation.bars,
                        generation.committed_ts,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO cursors (
                        venue, symbol, timeframe, generation_id, last_ts, bars, updated_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(venue, symbol, timeframe) DO UPDATE SET
                        generation_id = excluded.generation_id,
                        last_ts = excluded.last_ts,
                        bars = excluded.bars,
                        updated_ts = excluded.updated_ts
                    """,
                    (
                        generation.venue,
                        generation.symbol,
                        generation.timeframe,
                        generation.generation_id,
                        generation.last_ts,
                        generation.bars,
                        generation.committed_ts,
                    ),
                )
            connection.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ 清单查询

    def _select_generation(
        self, batch: ColdBatch, generation_id: str
    ) -> ColdGeneration | None:
        row = self._connection.execute(
            f"""
            SELECT {_GENERATION_COLUMNS} FROM generations
            WHERE venue = ? AND symbol = ? AND timeframe = ? AND generation_id = ?
            """,
            (
                batch.instrument.venue,
                batch.instrument.symbol,
                batch.timeframe,
                generation_id,
            ),
        ).fetchone()
        return self._generation_from_row(batch.instrument, batch.timeframe, row)

    def _select_generation_by_parts(self, generation: ColdGeneration) -> ColdGeneration | None:
        row = self._connection.execute(
            f"""
            SELECT {_GENERATION_COLUMNS} FROM generations
            WHERE venue = ? AND symbol = ? AND timeframe = ? AND generation_id = ?
            """,
            (
                generation.venue,
                generation.symbol,
                generation.timeframe,
                generation.generation_id,
            ),
        ).fetchone()
        if row is None:
            return None
        return ColdGeneration(
            generation_id=str(row[0]),
            venue=generation.venue,
            symbol=generation.symbol,
            timeframe=generation.timeframe,
            parquet_path=Path(str(row[1])),
            sha256=str(row[2]),
            content_sha256=str(row[3]),
            first_ts=int(row[4]),
            last_ts=int(row[5]),
            bars=int(row[6]),
            committed_ts=int(row[7]),
        )

    def _select_generations(
        self, instrument: InstrumentSpec, timeframe: str
    ) -> list[ColdGeneration]:
        rows = self._connection.execute(
            f"""
            SELECT {_GENERATION_COLUMNS} FROM generations
            WHERE venue = ? AND symbol = ? AND timeframe = ?
            ORDER BY rowid
            """,
            (instrument.venue, instrument.symbol, timeframe),
        ).fetchall()
        return [
            self._generation_from_row(instrument, timeframe, row)
            for row in rows
            if row is not None
        ]

    @staticmethod
    def _generation_from_row(
        instrument: InstrumentSpec, timeframe: str, row: tuple | None
    ) -> ColdGeneration | None:
        if row is None:
            return None
        return ColdGeneration(
            generation_id=str(row[0]),
            venue=instrument.venue,
            symbol=instrument.symbol,
            timeframe=timeframe,
            parquet_path=Path(str(row[1])),
            sha256=str(row[2]),
            content_sha256=str(row[3]),
            first_ts=int(row[4]),
            last_ts=int(row[5]),
            bars=int(row[6]),
            committed_ts=int(row[7]),
        )

    # ------------------------------------------------------------------ 辅助

    def _verify_generation_file(self, generation: ColdGeneration) -> None:
        path = self._path_under_root(generation.parquet_path)
        if not path.is_file() or _sha256_file(path) != generation.sha256:
            raise ColdStoreIntegrityError(
                f"generation 文件缺失或哈希不符：{generation.parquet_path}"
            )

    def _path_under_root(self, relative: Path) -> Path:
        root = self._data_dir.resolve()
        candidate = (self._data_dir / relative).resolve()
        if not candidate.is_relative_to(root):
            raise ColdStoreIntegrityError(f"清单路径越界：{relative}")
        return candidate

    def _hook(self, phase: ColdPhase) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ColdStoreError("ColdStore 已关闭")
