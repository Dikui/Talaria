"""一次性冷库迁移：旧字符串 generation → 按列 decimal128(18, s)。

迁移只改数值表示与清单指向：旧 Parquet 文件保留在原地可回退。所有新文件先
写入并校验，最后在一个 ``BEGIN IMMEDIATE`` 事务里更新清单和游标。
原始 JSONL 由原内容哈希继续引用，失败会回滚清单事务。
"""

from __future__ import annotations

import json
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
    ARCHIVE_SUBDIR,
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
    cold_column_scales,
)
from talaria.perception.sqlite import (
    checkpoint_and_close,
    claim_writer,
    open_writer_connection,
    release_writer,
)
from talaria.workbench_api.service_lock import ServiceLock


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


def _instrument_index(pack: MarketPack) -> dict[tuple[str, str], InstrumentSpec]:
    index: dict[tuple[str, str], InstrumentSpec] = {}
    for instrument in pack.instruments():
        key = (instrument.venue, instrument.symbol)
        if key in index:
            raise ColdStoreError(f"市场包内标的重复：{instrument.venue}/{instrument.symbol}")
        index[key] = instrument
    return index


def _load_source_specs(
    path: Path, instruments: dict[tuple[str, str], InstrumentSpec]
) -> dict[str, InstrumentSpec]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ColdStoreError(f"无法读取来源规格 {path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ColdStoreError(f"来源规格应为 JSON 对象：{path}")

    fields = {"tick_size", "qty_step", "price_precision", "qty_precision"}
    specs: dict[str, InstrumentSpec] = {}
    for partition, overrides in payload.items():
        parts = partition.split("/")
        if len(parts) != 4 or not all(parts):
            raise ColdStoreError(f"来源规格键应为 venue/symbol/timeframe/generation_id：{partition}")
        venue, symbol, _, _ = parts
        instrument = instruments.get((venue, symbol))
        if instrument is None:
            raise ColdStoreError(f"来源规格标的不在市场包内：{venue}/{symbol}")
        if not isinstance(overrides, dict) or overrides.keys() != fields:
            raise ColdStoreError(f"来源规格 {partition} 需要字段 {', '.join(sorted(fields))}")
        if not (
            isinstance(overrides["tick_size"], str)
            and isinstance(overrides["qty_step"], str)
            and type(overrides["price_precision"]) is int
            and type(overrides["qty_precision"]) is int
        ):
            raise ColdStoreError(f"来源规格 {partition} 的步长需为十进制字符串，精度需为整数")
        try:
            spec = InstrumentSpec.model_validate(instrument.model_dump() | overrides)
        except ValueError as exc:
            raise ColdStoreError(f"来源规格 {partition} 无效：{exc}") from exc
        if spec.tick_size <= 0 or spec.qty_step <= 0:
            raise ColdStoreError(f"来源规格 {partition} 的步长必须为正数")
        cold_column_scales(spec)
        specs[partition] = spec
    return specs


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

    content_sha256 = _safe_path_part(str(row[3]), "content_sha256")
    archive = _path_under_data_dir(data_dir, ARCHIVE_SUBDIR / f"{content_sha256}.jsonl")
    if not archive.is_file() or _sha256_file(archive) != content_sha256:
        raise ColdStoreIntegrityError(f"原始归档缺失或哈希不符：{archive}")

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
        ),
        written,
    )


def migrate_cold_store(
    data_dir: Path | str,
    *,
    pack: MarketPack,
    source_specs: Path | None = None,
) -> ColdMigrationReport:
    """迁移旧字符串 generation，可按分区和旧 generation ID 指定来源规格。"""

    root = Path(data_dir)
    with ServiceLock(root, role="cold-migrate"):
        return _migrate_locked(root, pack=pack, source_specs=source_specs)


def _migrate_locked(
    root: Path, *, pack: MarketPack, source_specs: Path | None
) -> ColdMigrationReport:
    db_path = root / MANIFEST_DB
    if not db_path.is_file():
        return ColdMigrationReport(0, 0, 0, 0, False)

    instruments = _instrument_index(pack)
    historical_specs = _load_source_specs(source_specs, instruments) if source_specs else {}
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
            source = historical_specs.get(f"{venue}/{symbol}/{timeframe}/{row[0]}", instrument)
            migration, written = _prepare_migration(
                root,
                instrument=source,
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
                # 保留原始归档哈希和清单顺序，时点读取的并列版本沿用原优先级。
                connection.execute(
                    """
                    UPDATE generations SET generation_id = ?, parquet_path = ?, sha256 = ?
                    WHERE venue = ? AND symbol = ? AND timeframe = ? AND generation_id = ?
                    """,
                    (
                        migration.new_generation_id,
                        migration.new_path.relative_to(root).as_posix(),
                        migration.new_sha256,
                        migration.venue,
                        migration.symbol,
                        migration.timeframe,
                        migration.old_generation_id,
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
        try:
            if connection is not None:
                checkpoint_and_close(connection)
        finally:
            release_writer(db_path)
