"""一次性冷库迁移：旧字符串 generation → 按列 decimal128(18, s)。

迁移只改数值表示与清单指向：旧 Parquet 文件保留在原地可回退。所有新文件先
写入并校验，最后在一个 ``BEGIN IMMEDIATE`` 事务里删除旧清单行、插入新行并
改写指向旧 generation 的游标；任一步失败都不会留下半迁移的清单。
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import ColdStoreError, ColdStoreIntegrityError
from talaria.foundation.market import InstrumentSpec, MarketPack
from talaria.perception.cold_store import (
    COLD_SUBDIR,
    MANIFEST_DB,
    _GENERATION_COLUMNS,
    _cold_parquet_is_decimal128,
    _content_bytes,
    _fsync_directory,
    _read_cold_parquet,
    _safe_path_part,
    _sha256_bytes,
    _sha256_file,
    _validate_bars,
    _write_cold_parquet,
)
from talaria.perception.sqlite import (
    checkpoint_and_close,
    claim_writer,
    open_writer_connection,
    release_writer,
)


@dataclass(frozen=True, slots=True)
class ColdMigrationReport:
    """一次迁移的可核对结果；重复执行时 ``migrated_generations`` 为 0。"""

    migrated_generations: int
    already_current_generations: int
    updated_cursors: int
    written_files: int
    manifest_switched: bool


@dataclass(frozen=True, slots=True)
class _Migration:
    venue: str
    symbol: str
    timeframe: str
    old_generation_id: str
    new_generation_id: str
    new_path: Path
    new_sha256: str
    new_first_ts: int
    new_last_ts: int
    new_bars: int
    new_committed_ts: int


def _instrument_index(pack: MarketPack) -> dict[tuple[str, str], InstrumentSpec]:
    index: dict[tuple[str, str], InstrumentSpec] = {}
    for instrument in pack.instruments():
        key = (instrument.venue, instrument.symbol)
        if key in index:
            raise ColdStoreError(f"市场包内标的重复：{instrument.venue}/{instrument.symbol}")
        index[key] = instrument
    return index


def _path_under_data_dir(data_dir: Path, relative: Path) -> Path:
    root = data_dir.resolve()
    candidate = (data_dir / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ColdStoreIntegrityError(f"清单路径越界：{relative}")
    return candidate


def _write_new_generation(
    data_dir: Path,
    *,
    instrument: InstrumentSpec,
    venue: str,
    symbol: str,
    timeframe: str,
    generation_id: str,
    bars: Sequence[RawBar],
) -> tuple[Path, str, bool]:
    """原子写出新格式文件；已存在且内容一致时复用，返回（路径、哈希、是否新写）。"""

    venue = _safe_path_part(venue, "venue")
    symbol = _safe_path_part(symbol, "symbol")
    timeframe = _safe_path_part(timeframe, "timeframe")
    final = (
        data_dir
        / COLD_SUBDIR
        / venue
        / symbol
        / timeframe
        / f"{generation_id}.parquet"
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    temporary = final.with_name(f".{final.name}.{uuid.uuid4().hex}.tmp")
    written = False
    try:
        _write_cold_parquet(temporary, bars, instrument=instrument)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary_sha = _sha256_file(temporary)
        if final.exists():
            if _sha256_file(final) != temporary_sha:
                raise ColdStoreIntegrityError(
                    f"迁移目标 generation 已存在且内容不符：{final}"
                )
        else:
            os.replace(temporary, final)
            _fsync_directory(final.parent)
            written = True
        return final, temporary_sha, written
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_migration(
    data_dir: Path,
    *,
    instrument: InstrumentSpec,
    timeframe: str,
    row: tuple,
) -> tuple[_Migration | None, bool]:
    """读取一条旧 generation 并写出新文件；返回（迁移描述、是否新写文件）。

    ``migration`` 为 ``None`` 表示该文件已带 decimal128 元数据，无需迁移。
    """

    generation_id, parquet_path, sha256 = row[0], row[1], row[2]
    path = _path_under_data_dir(data_dir, Path(str(parquet_path)))
    if not path.is_file():
        raise ColdStoreIntegrityError(f"generation 文件缺失：{parquet_path}")
    if _sha256_file(path) != str(sha256):
        raise ColdStoreIntegrityError(f"generation 文件哈希不符：{parquet_path}")

    if _cold_parquet_is_decimal128(path):
        return None, False

    bars = _read_cold_parquet(path)
    if not bars:
        raise ColdStoreIntegrityError(f"legacy generation 没有数据：{parquet_path}")
    _validate_bars(bars, instrument=instrument, timeframe=timeframe)

    content = _content_bytes(bars)
    new_generation_id = _sha256_bytes(content)
    final, new_sha256, written = _write_new_generation(
        data_dir,
        instrument=instrument,
        venue=instrument.venue,
        symbol=instrument.symbol,
        timeframe=timeframe,
        generation_id=new_generation_id,
        bars=bars,
    )
    decoded = _read_cold_parquet(final)
    _validate_bars(decoded, instrument=instrument, timeframe=timeframe)
    if decoded != list(bars):
        raise ColdStoreIntegrityError("迁移后 Parquet 读回内容与旧 generation 不一致")

    return (
        _Migration(
            venue=instrument.venue,
            symbol=instrument.symbol,
            timeframe=timeframe,
            old_generation_id=str(generation_id),
            new_generation_id=new_generation_id,
            new_path=final,
            new_sha256=new_sha256,
            new_first_ts=bars[0].ts_event,
            new_last_ts=bars[-1].ts_event,
            new_bars=len(bars),
            new_committed_ts=max(bar.ts_committed for bar in bars),
        ),
        written,
    )


def migrate_cold_store(data_dir: Path | str, *, pack: MarketPack) -> ColdMigrationReport:
    """把清单中的旧字符串 generation 全部迁移到 decimal128；幂等、失败不切清单。"""

    root = Path(data_dir)
    db_path = root / MANIFEST_DB
    if not db_path.is_file():
        return ColdMigrationReport(0, 0, 0, 0, False)

    instruments = _instrument_index(pack)
    claim_writer(db_path)
    connection: sqlite3.Connection | None = None
    try:
        connection = open_writer_connection(db_path)
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "generations" not in tables:
            return ColdMigrationReport(0, 0, 0, 0, False)

        rows = connection.execute(
            f"""
            SELECT {_GENERATION_COLUMNS}, venue, symbol, timeframe
            FROM generations
            ORDER BY rowid
            """
        ).fetchall()

        migrations: list[_Migration] = []
        already_current = 0
        written_files = 0
        for row in rows:
            venue, symbol, timeframe = str(row[8]), str(row[9]), str(row[10])
            instrument = instruments.get((venue, symbol))
            if instrument is None:
                raise ColdStoreError(
                    f"冷库清单中的标的不在市场包内：{venue}/{symbol}"
                )
            timeframe = _safe_path_part(timeframe, "timeframe")
            migration, written = _prepare_migration(
                root,
                instrument=instrument,
                timeframe=timeframe,
                row=row[:8],
            )
            if migration is None:
                already_current += 1
            else:
                migrations.append(migration)
                written_files += int(written)

        if not migrations:
            return ColdMigrationReport(
                0, already_current, 0, written_files, False
            )

        updated_cursors = 0
        try:
            connection.execute("BEGIN IMMEDIATE")
            for migration in migrations:
                connection.execute(
                    """
                    DELETE FROM generations
                    WHERE venue = ? AND symbol = ? AND timeframe = ? AND generation_id = ?
                    """,
                    (
                        migration.venue,
                        migration.symbol,
                        migration.timeframe,
                        migration.old_generation_id,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO generations (
                        generation_id, venue, symbol, timeframe, parquet_path,
                        sha256, content_sha256, first_ts, last_ts, bars, committed_ts
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        migration.new_generation_id,
                        migration.venue,
                        migration.symbol,
                        migration.timeframe,
                        migration.new_path.relative_to(root).as_posix(),
                        migration.new_sha256,
                        migration.new_generation_id,
                        migration.new_first_ts,
                        migration.new_last_ts,
                        migration.new_bars,
                        migration.new_committed_ts,
                    ),
                )
                updated_cursors += connection.execute(
                    """
                    UPDATE cursors SET generation_id = ?
                    WHERE venue = ? AND symbol = ? AND timeframe = ? AND generation_id = ?
                    """,
                    (
                        migration.new_generation_id,
                        migration.venue,
                        migration.symbol,
                        migration.timeframe,
                        migration.old_generation_id,
                    ),
                ).rowcount
            connection.execute("COMMIT")
        except BaseException:
            with suppress(sqlite3.Error):
                connection.execute("ROLLBACK")
            raise

        return ColdMigrationReport(
            migrated_generations=len(migrations),
            already_current_generations=already_current,
            updated_cursors=updated_cursors,
            written_files=written_files,
            manifest_switched=True,
        )
    finally:
        if connection is not None:
            checkpoint_and_close(connection)
        release_writer(db_path)
