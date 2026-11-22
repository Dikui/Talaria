"""冷库原子发布：内容哈希归档 → staging 质检 → 不可变 generation → 清单短事务。

发布顺序与崩溃语义（docs/architecture.md §3.2）：

1. 原始标准化柱按内容哈希写入 ``data/raw_archive/<sha256>.jsonl``；同内容复用；
2. 写入 ``data/staging/<content_sha256>/bars.parquet``，读回后做完整性、单调性与
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
from bisect import bisect_left
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from pathlib import Path
from typing import Callable, Iterable, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import ColdStoreError, ColdStoreIntegrityError, ColdStoreQualityError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.types import PrecisionTier
from talaria.perception.raw_store import bar_record
from talaria.perception.raw_store import latest_visible_bars as latest_visible_bars
from talaria.perception.sqlite import (
    checkpoint_and_close,
    claim_writer,
    open_writer_connection,
    release_writer,
)

ARCHIVE_SUBDIR = Path("data") / "raw_archive"
STAGING_SUBDIR = Path("data") / "staging"
COLD_SUBDIR = Path("data") / "cold"
MANIFEST_DB = Path("data") / "data_manifest.sqlite"

#: Parquet 元数据键：格式标记、逐列 scale、标的规格版本。读取侧按文件还原，
#: 不假设整个冷库只有一种 scale（ADR-0017）。
COLD_FORMAT_METADATA = b"talaria.cold.format"
COLD_FORMAT_DECIMAL128 = b"decimal128.v1"
COLD_COLUMN_SCALES_METADATA = b"talaria.cold.column_scales"
COLD_INSTRUMENT_VERSION_METADATA = b"talaria.cold.instrument_spec_sha256"

#: 冷库 Parquet 数值列统一使用 precision=18；物理类型由 pyarrow 写成 INT64。
COLD_DECIMAL_PRECISION = 18
#: 费率类列固定 scale=8（ADR-0017）；当前 K 线冷库没有费率列，保留常量供衍生品切片。
RATE_COLUMN_SCALE = 8

_PRICE_COLUMNS = ("open", "high", "low", "close")
_QUANTITY_COLUMNS = ("volume",)
_TURNOVER_COLUMNS = ("turnover",)
_DECIMAL_COLUMNS = _PRICE_COLUMNS + _QUANTITY_COLUMNS + _TURNOVER_COLUMNS


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
    """一次待发布的标准化柱批次；时间戳非递减，同一时间戳按 revision 严格递增。"""

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


def cold_column_scales(instrument: InstrumentSpec) -> dict[str, int]:
    """返回该标的的逐列 decimal128 scale（ADR-0017）。

    价格列用价格精度，数量列用数量精度，``turnover`` 用两者之和；费率列固定 8。
    """

    price_scale = instrument.price_precision
    quantity_scale = instrument.qty_precision
    if price_scale < 0 or quantity_scale < 0:
        raise ColdStoreQualityError("标的精度不能为负")
    if price_scale + quantity_scale > COLD_DECIMAL_PRECISION:
        raise ColdStoreQualityError(
            f"价格精度与数量精度之和超出 decimal128({COLD_DECIMAL_PRECISION}, s)："
            f"{price_scale} + {quantity_scale}"
        )
    scales: dict[str, int] = {}
    for name in _PRICE_COLUMNS:
        scales[name] = price_scale
    for name in _QUANTITY_COLUMNS:
        scales[name] = quantity_scale
    for name in _TURNOVER_COLUMNS:
        scales[name] = price_scale + quantity_scale
    return scales


def instrument_spec_version(instrument: InstrumentSpec) -> str:
    """对影响 cold 列 scale 的规格字段求稳定摘要，写入 Parquet 元数据。"""

    payload = json.dumps(
        {
            "tick_size": format(instrument.tick_size, "f"),
            "qty_step": format(instrument.qty_step, "f"),
            "price_precision": instrument.price_precision,
            "qty_precision": instrument.qty_precision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _cold_schema(scales: dict[str, int]) -> pa.Schema:
    fields = [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
    ]
    for name in _DECIMAL_COLUMNS:
        fields.append(
            pa.field(
                name,
                pa.decimal128(COLD_DECIMAL_PRECISION, scales[name]),
                nullable=False,
            )
        )
    fields.extend(
        [
            pa.field("ts_available", pa.int64(), nullable=False),
            pa.field("ts_ingested", pa.int64(), nullable=False),
            pa.field("ts_committed", pa.int64(), nullable=False),
            pa.field("precision", pa.dictionary(pa.int8(), pa.string()), nullable=False),
            pa.field("revision", pa.int64(), nullable=False),
        ]
    )
    return pa.schema(fields)


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


def _content_bytes(bars: Iterable[RawBar], *, canonical_decimals: bool = False) -> bytes:
    def encode(bar: RawBar) -> bytes:
        record = bar_record(bar)
        if canonical_decimals:
            for name in _DECIMAL_COLUMNS:
                value = str(record[name])
                if "." in value:
                    value = value.rstrip("0").rstrip(".")
                record[name] = "0" if value == "-0" else value
        return (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")

    return b"".join(encode(bar) for bar in bars)


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


def _fits_decimal128(value: Decimal, scale: int) -> tuple[bool, str]:
    """判断有限十进制值能否无损放进 ``decimal128(18, scale)``。"""

    if not value.is_finite():
        return False, "不是有限十进制数"
    # scaleb 在足够大的局部精度下是精确移位；再用整数系数判断是否超过 18 位。
    with localcontext() as context:
        context.prec = max(40, len(value.as_tuple().digits) + scale + 2)
        scaled = value.scaleb(scale)
        if scaled != scaled.to_integral_value():
            return False, f"小数位超出列 scale={scale}"
        coefficient = int(scaled)
    if len(str(abs(coefficient))) > COLD_DECIMAL_PRECISION:
        return (
            False,
            f"有效数字超出 decimal128({COLD_DECIMAL_PRECISION}, {scale})",
        )
    return True, ""


def _validate_bars(
    bars: Sequence[RawBar],
    *,
    instrument: InstrumentSpec,
    timeframe: str,
) -> None:
    """完整性、单调性、OHLC 合法性与价格/数量精度质检。"""

    if not bars:
        raise ColdStoreQualityError("批次为空，拒绝发布")
    scales = cold_column_scales(instrument)
    tick_size = instrument.tick_size
    qty_step = instrument.qty_step
    previous_ts: int | None = None
    previous_revision: int | None = None
    for index, bar in enumerate(bars):
        where = f"第 {index} 根柱"
        if bar.symbol != instrument.symbol:
            raise ColdStoreQualityError(f"{where} symbol 与标的规格不一致")
        if bar.timeframe != timeframe:
            raise ColdStoreQualityError(f"{where} timeframe 与批次不一致")
        if bar.revision < 0:
            raise ColdStoreQualityError(f"{where} revision 为负")
        if previous_ts is not None:
            if bar.ts_event < previous_ts or (
                bar.ts_event == previous_ts
                and previous_revision is not None
                and bar.revision <= previous_revision
            ):
                raise ColdStoreQualityError(
                    f"{where} 时间戳与修订未严格递增："
                    f"{bar.ts_event}/{bar.revision}"
                )
        for name in _DECIMAL_COLUMNS:
            fits, reason = _fits_decimal128(getattr(bar, name), scales[name])
            if not fits:
                raise ColdStoreQualityError(f"{where} {name} {reason}")
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
        previous_revision = bar.revision


def _write_cold_parquet(
    path: Path, bars: Sequence[RawBar], *, instrument: InstrumentSpec
) -> None:
    scales = cold_column_scales(instrument)
    rows = [
        {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "ts_event": bar.ts_event,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "volume": bar.volume,
            "turnover": bar.turnover,
            "ts_available": bar.ts_available,
            "ts_ingested": bar.ts_ingested,
            "ts_committed": bar.ts_committed,
            "precision": bar.precision.value,
            "revision": bar.revision,
        }
        for bar in bars
    ]
    table = pa.Table.from_pylist(rows, schema=_cold_schema(scales))
    metadata = {
        COLD_FORMAT_METADATA: COLD_FORMAT_DECIMAL128,
        COLD_COLUMN_SCALES_METADATA: json.dumps(
            scales, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        COLD_INSTRUMENT_VERSION_METADATA: instrument_spec_version(instrument).encode("ascii"),
    }
    table = table.replace_schema_metadata(metadata)
    # ADR-0017：decimal128(18, s) 的 Parquet 物理类型必须是 INT64，而不是
    # fixed_len_byte_array；pyarrow 需显式开启该选项。
    pq.write_table(
        table, path, compression="zstd", store_decimal_as_integer=True
    )


def _column_scales_from_metadata(metadata: dict[bytes, bytes]) -> dict[str, int]:
    raw = metadata.get(COLD_COLUMN_SCALES_METADATA)
    if raw is None:
        raise ColdStoreIntegrityError("decimal128 文件缺少逐列 scale 元数据")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ColdStoreIntegrityError("decimal128 逐列 scale 元数据不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise ColdStoreIntegrityError("decimal128 逐列 scale 元数据不是对象")
    scales: dict[str, int] = {}
    for name in _DECIMAL_COLUMNS:
        value = payload.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or value > COLD_DECIMAL_PRECISION
        ):
            raise ColdStoreIntegrityError(f"decimal128 列 scale 非法：{name}={value!r}")
        scales[name] = value
    return scales


def _verify_decimal_schema(schema: pa.Schema, scales: dict[str, int]) -> None:
    """确认文件物理类型与元数据声明的 scale 一致，读取方不以全局 scale 猜测。"""

    for name, scale in scales.items():
        if name not in schema.names:
            raise ColdStoreIntegrityError(f"decimal128 文件缺少列：{name}")
        expected = pa.decimal128(COLD_DECIMAL_PRECISION, scale)
        if schema.field(name).type != expected:
            raise ColdStoreIntegrityError(
                f"decimal128 列 {name} 类型与元数据不符："
                f"{schema.field(name).type} != {expected}"
            )
    if "revision" not in schema.names or schema.field("revision").type != pa.int64():
        raise ColdStoreIntegrityError("decimal128 文件缺少 int64 revision 列")


def _cold_parquet_is_decimal128(path: Path) -> bool:
    metadata = pq.read_schema(path).metadata or {}
    return metadata.get(COLD_FORMAT_METADATA) == COLD_FORMAT_DECIMAL128


def _read_cold_parquet(path: Path) -> list[RawBar]:
    table = pq.read_table(path)
    metadata = table.schema.metadata or {}
    format_marker = metadata.get(COLD_FORMAT_METADATA)
    if format_marker is not None and format_marker != COLD_FORMAT_DECIMAL128:
        raise ColdStoreIntegrityError(f"未知冷库数值格式：{format_marker!r}")
    if format_marker == COLD_FORMAT_DECIMAL128:
        _verify_decimal_schema(table.schema, _column_scales_from_metadata(metadata))

    def decimal_value(value: object) -> Decimal:
        if isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    return [
        RawBar(
            symbol=str(row["symbol"]),
            timeframe=str(row["timeframe"]),
            ts_event=int(row["ts_event"]),
            open=decimal_value(row["open"]),
            high=decimal_value(row["high"]),
            low=decimal_value(row["low"]),
            close=decimal_value(row["close"]),
            volume=decimal_value(row["volume"]),
            turnover=decimal_value(row["turnover"]),
            ts_available=int(row["ts_available"]),
            ts_ingested=int(row["ts_ingested"]),
            ts_committed=int(row["ts_committed"]),
            precision=PrecisionTier(str(row["precision"])),
            revision=int(row.get("revision", 0) or 0),
        )
        for row in table.to_pylist()
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
            _validate_bars(
                batch.bars,
                instrument=batch.instrument,
                timeframe=batch.timeframe,
            )
            generation_id = _sha256_bytes(_content_bytes(batch.bars, canonical_decimals=True))
            existing = self._select_generation(batch, generation_id)
            if existing is not None:
                self._verify_generation_file(existing)
                return existing

            committed_ts = max(bar.ts_committed for bar in batch.bars)
            cursor = self.cursor(batch.instrument, batch.timeframe)
            if cursor is not None and batch.bars[0].ts_event <= cursor.last_ts:
                existing = self._select_matching_generation(batch, committed_ts)
                if existing is not None:
                    return existing
                # 修订批次可以回写已覆盖的事件时间（ADR-0018），但 revision=0 的旧柱
                # 仍然只能追加，防止普通发布悄悄改写历史。
                if any(
                    bar.ts_event <= cursor.last_ts and bar.revision == 0
                    for bar in batch.bars
                ):
                    raise ColdStoreQualityError(
                        "批次包含不晚于冷库游标且 revision=0 的柱，拒绝非追加发布"
                        "（append-only monotonic cursor）"
                    )

            content = _content_bytes(batch.bars)
            archived = self._archive_content(content, _sha256_bytes(content))
            self._write_staging(archived, batch)
            self._quality_check(archived, batch)
            final = self._promote(archived, batch, generation_id)
            generation = ColdGeneration(
                generation_id=generation_id,
                venue=batch.instrument.venue,
                symbol=batch.instrument.symbol,
                timeframe=batch.timeframe,
                parquet_path=final.relative_to(self._data_dir),
                sha256=_sha256_file(final),
                content_sha256=archived.sha256,
                first_ts=batch.bars[0].ts_event,
                last_ts=batch.bars[-1].ts_event,
                bars=len(batch.bars),
                committed_ts=committed_ts,
            )
            self._commit_manifest(generation)
            self._hook(ColdPhase.MANIFEST_AFTER)
            return generation

    def generations(self, instrument: InstrumentSpec, timeframe: str) -> list[ColdGeneration]:
        with self._lock:
            self._ensure_open()
            return self._select_generations(instrument, timeframe)

    def read_bars(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        as_of: int | None = None,
    ) -> list[RawBar]:
        """只读取清单列出的文件；逐文件校验哈希后按统一读取语义选版本。"""

        with self._lock:
            self._ensure_open()
            decoded: list[RawBar] = []
            for generation in self._select_generations(instrument, timeframe):
                decoded.extend(self._read_generation(generation))
            return latest_visible_bars(decoded, as_of=as_of)

    def published_revisions(
        self, instrument: InstrumentSpec, timeframe: str, timestamps: set[int]
    ) -> set[tuple[int, int]]:
        """返回指定事件时间已发布的 (事件时间, 修订号)，包括历史版本。"""

        with self._lock:
            self._ensure_open()
            sorted_times = sorted(timestamps)
            revisions: set[tuple[int, int]] = set()
            if not sorted_times:
                return revisions
            for generation in self._select_generations(instrument, timeframe):
                index = bisect_left(sorted_times, generation.first_ts)
                if index < len(sorted_times) and sorted_times[index] <= generation.last_ts:
                    revisions.update(
                        (bar.ts_event, bar.revision)
                        for bar in self._read_generation(generation)
                        if bar.ts_event in timestamps
                    )
            return revisions

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

    def _write_staging(self, archived: ArchivedBatch, batch: ColdBatch) -> None:
        self._hook(ColdPhase.STAGING_BEFORE)
        staging_dir = (self._data_dir / STAGING_SUBDIR) / archived.sha256
        # 上一次崩溃可能留下残片；发布永远从干净 staging 重建，绝不信任旧文件。
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        temporary = staging_dir / "bars.parquet.tmp"
        _write_cold_parquet(temporary, batch.bars, instrument=batch.instrument)
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

    def _generation_file(self, batch: ColdBatch, generation_id: str) -> Path:
        venue = _safe_path_part(batch.instrument.venue, "venue")
        symbol = _safe_path_part(batch.instrument.symbol, "symbol")
        timeframe = _safe_path_part(batch.timeframe, "timeframe")
        return (
            self._data_dir
            / COLD_SUBDIR
            / venue
            / symbol
            / timeframe
            / f"{generation_id}.parquet"
        )

    def _promote(self, archived: ArchivedBatch, batch: ColdBatch, generation_id: str) -> Path:
        self._hook(ColdPhase.RENAME_BEFORE)
        final = self._generation_file(batch, generation_id)
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
                        generation_id = CASE
                            WHEN excluded.last_ts >= cursors.last_ts
                            THEN excluded.generation_id
                            ELSE cursors.generation_id
                        END,
                        last_ts = MAX(cursors.last_ts, excluded.last_ts),
                        bars = CASE
                            WHEN excluded.last_ts >= cursors.last_ts
                            THEN excluded.bars
                            ELSE cursors.bars
                        END,
                        updated_ts = MAX(cursors.updated_ts, excluded.updated_ts)
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

    def _select_matching_generation(
        self, batch: ColdBatch, committed_ts: int
    ) -> ColdGeneration | None:
        """查找旧标识下数值等价的批次，只读时间范围和行数匹配的文件。"""

        rows = self._connection.execute(
            f"""
            SELECT {_GENERATION_COLUMNS} FROM generations
            WHERE venue = ? AND symbol = ? AND timeframe = ?
                AND first_ts = ? AND last_ts = ? AND bars = ? AND committed_ts = ?
            ORDER BY rowid
            """,
            (
                batch.instrument.venue,
                batch.instrument.symbol,
                batch.timeframe,
                batch.bars[0].ts_event,
                batch.bars[-1].ts_event,
                len(batch.bars),
                committed_ts,
            ),
        )
        bars = list(batch.bars)
        for row in rows:
            generation = self._generation_from_row(batch.instrument, batch.timeframe, row)
            if generation is not None and self._read_generation(generation) == bars:
                return generation
        return None

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

    def _read_generation(self, generation: ColdGeneration) -> list[RawBar]:
        return _read_cold_parquet(self._verify_generation_file(generation))

    def _verify_generation_file(self, generation: ColdGeneration) -> Path:
        path = self._path_under_root(generation.parquet_path)
        if not path.is_file() or _sha256_file(path) != generation.sha256:
            raise ColdStoreIntegrityError(
                f"generation 文件缺失或哈希不符：{generation.parquet_path}"
            )
        return path

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
