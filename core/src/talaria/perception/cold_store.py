"""冷库原子发布：内容哈希归档 → staging 质检 → 不可变 generation → 清单短事务。

发布顺序与崩溃语义（docs/architecture.md §3.2）：

1. 原始标准化柱按内容哈希写入 ``data/raw_archive/<sha256>.jsonl``；
   合并归档记录输入批次的内容哈希，复用原始归档；
2. 写入 ``data/staging/<content_sha256>/bars.parquet``，读回后做完整性、单调性与
   精度质检；失败只留下未引用残片，绝不进入清单；
3. ``fsync`` 后原子 rename 到 ``data/cold/<venue>/<symbol>/<timeframe>/<month>/``；
4. 在 ``data/data_manifest.sqlite`` 的短事务中发布清单。

``data_manifest.sqlite`` 是读取侧唯一发布真相：读取只按清单显式列出的路径打开
Parquet，禁止目录通配。任何阶段崩溃最多留下未引用的孤儿文件，读取侧看不到
半成品批次。
"""

from __future__ import annotations

import hashlib
import gzip
import json
import os
import shutil
import sqlite3
import threading
import time
import uuid
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from heapq import heappop, heappush
from itertools import groupby, pairwise
from pathlib import Path
from typing import Callable, Collection, Iterable, Iterator, Sequence

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import (
    DerivativeKind, DerivativePoint, derivative_identity, latest_visible_derivatives,
)
from talaria.foundation.errors import ColdStoreError, ColdStoreIntegrityError, ColdStoreQualityError
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.revisions import latest_visible_revisions
from talaria.foundation.ticks import Quote, TickKind, Trade, relative_tick_order_known, tick_identity, tick_order
from talaria.foundation.timeframes import SUPPORTED_TIMEFRAMES
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
RETENTION_MS = 24 * 60 * 60 * 1000
RETAINED_MANIFESTS = 3
TICK_BATCH_ROWS = 64_000
_TICK_REPLAY_PAGE_MS = 5 * 60_000
_TICK_DECIMALS = {
    TickKind.TRADES: ("price", "size"),
    TickKind.QUOTES: ("bid_price", "bid_size", "ask_price", "ask_size"),
}
_TICK_TIMES = ("ts_event", "ts_available", "ts_ingested", "ts_committed", "revision")
_TICK_METADATA = {
    TickKind.TRADES: {
        "availability": pa.string(), "archive_day": pa.int64(), "archive_index": pa.int64(),
        "sequence": pa.int64(), "packet_id": pa.binary(16), "packet_index": pa.int64(),
    },
    TickKind.QUOTES: {"connection_id": pa.binary(16), "update_id": pa.int64()},
}

#: Parquet 元数据键：格式标记、逐列 scale、标的规格版本。读取侧按文件还原，
#: 不假设整个冷库只有一种 scale（ADR-0017）。
COLD_FORMAT_METADATA = b"talaria.cold.format"
COLD_FORMAT_DECIMAL128 = b"decimal128.v1"
COLD_COLUMN_SCALES_METADATA = b"talaria.cold.column_scales"
COLD_INSTRUMENT_VERSION_METADATA = b"talaria.cold.instrument_spec_sha256"

#: 冷库 Parquet 数值列统一使用 precision=18；物理类型由 pyarrow 写成 INT64。
COLD_DECIMAL_PRECISION = 18
#: 费率类列固定 scale=8（ADR-0017）。
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
    """同月的标准化柱批次；时间戳非递减，同一时间戳按 revision 严格递增。"""

    instrument: InstrumentSpec
    timeframe: str
    bars: Sequence[RawBar]

    @property
    def rows(self) -> Sequence[RawBar]:
        return self.bars


@dataclass(frozen=True, slots=True)
class DerivativeBatch:
    instrument: InstrumentSpec
    kind: DerivativeKind
    rows: Sequence[DerivativePoint]

    @property
    def timeframe(self) -> str:
        # 清单的 timeframe 是历史列名。衍生品的分区键是类别，周期留在行内。
        return self.kind.value


@dataclass(frozen=True, slots=True)
class TickBatch:
    """同一标的、数据集与 UTC 日的行情修订。"""

    instrument: InstrumentSpec
    kind: TickKind
    rows: Sequence[Trade | Quote]

    @property
    def timeframe(self) -> str:
        return self.kind.value


type _PublishBatch = ColdBatch | DerivativeBatch | TickBatch


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
class ColdManifest:
    """一次清单读事务选出的不可变文件，保留读取顺序。"""

    version: int
    generations: tuple[ColdGeneration, ...]

    def range_fingerprint(self, start_ts: int, end_ts: int, *, compact: bool = False) -> str:
        """标识与该范围相交的文件及其内容，不受其他序列发布影响。"""
        files = [
            (item.parquet_path.as_posix(), item.sha256) for item in self.generations
            if item.last_ts >= start_ts and item.first_ts <= end_ts
        ]
        encoded = (
            json.dumps(files, separators=(",", ":")) if compact else json.dumps(files)
        )
        return _sha256_bytes(encoded.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ColdSnapshot:
    """同一读事务固定的回放清单。"""

    bars: ColdManifest
    derivatives: ColdManifest
    ticks: ColdManifest | None = None


@dataclass(frozen=True, slots=True)
class ColdCursor:
    """兼容旧冷库迁移工具的末尾位置。"""

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
    """
    CREATE TABLE IF NOT EXISTS manifest_version (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL
    )
    """,
    "INSERT OR IGNORE INTO manifest_version VALUES (1, 0)",
    """
    CREATE TABLE IF NOT EXISTS retired_files (
        parquet_path TEXT PRIMARY KEY,
        retired_ts INTEGER NOT NULL,
        manifest_version INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS generations_series_range
    ON generations (venue, symbol, timeframe, first_ts, last_ts)
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
    allow_equal_revisions: bool = False,
) -> None:
    """完整性、单调性、OHLC 合法性与价格/数量精度质检。"""

    if not bars:
        raise ColdStoreQualityError("批次为空，拒绝发布")
    scales = cold_column_scales(instrument)
    tick_size = instrument.tick_size
    qty_step = instrument.qty_step
    previous: tuple[int, int] | None = None
    for index, bar in enumerate(bars):
        where = f"第 {index} 根柱"
        if bar.symbol != instrument.symbol:
            raise ColdStoreQualityError(f"{where} symbol 与标的规格不一致")
        if bar.timeframe != timeframe:
            raise ColdStoreQualityError(f"{where} timeframe 与批次不一致")
        if bar.revision < 0:
            raise ColdStoreQualityError(f"{where} revision 为负")
        current = (bar.ts_event, bar.revision)
        if previous is not None and (
            current < previous or (current == previous and not allow_equal_revisions)
        ):
            raise ColdStoreQualityError(f"{where} 时间戳与修订未递增：{current}")
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
        previous = current


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
    _write_decimal_parquet(path, table, scales, instrument)


def _write_decimal_parquet(
    path: Path, table: pa.Table, scales: dict[str, int], instrument: InstrumentSpec,
) -> None:
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


def _column_scales_from_metadata(
    metadata: dict[bytes, bytes], columns: Sequence[str] = _DECIMAL_COLUMNS,
) -> dict[str, int]:
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
    for name in columns:
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


def _read_range_table(
    path: Path, start_ts: int | None, end_ts: int | None, *,
    expression: ds.Expression | None = None,
) -> pa.Table:
    filters = []
    if start_ts is not None:
        filters.append(("ts_event", ">=", start_ts))
    if end_ts is not None:
        filters.append(("ts_event", "<=", end_ts))
    if expression is not None:
        if filters:
            expression = expression & pq.filters_to_expression(filters)
        return pq.read_table(path, filters=expression)
    return pq.read_table(path, filters=filters or None)


def _read_cold_parquet(
    path: Path, *, start_ts: int | None = None, end_ts: int | None = None
) -> list[RawBar]:
    table = _read_range_table(path, start_ts, end_ts)
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


def _derivative_scale(instrument: InstrumentSpec, kind: DerivativeKind) -> int:
    return {
        DerivativeKind.MARK_PRICE: instrument.price_precision,
        DerivativeKind.OPEN_INTEREST: instrument.qty_precision,
        DerivativeKind.FUNDING_RATE: RATE_COLUMN_SCALE,
    }[kind]


def _derivative_order(point: DerivativePoint) -> tuple:
    return (point.ts_event, *derivative_identity(point)[:-1], point.revision)


def _validate_derivatives(batch: DerivativeBatch, *, allow_equal_revisions: bool = False) -> None:
    scale = _derivative_scale(batch.instrument, batch.kind)
    if not 0 <= scale <= COLD_DECIMAL_PRECISION:
        raise ColdStoreQualityError(f"非法衍生品精度：{scale}")
    previous: tuple | None = None
    for point in batch.rows:
        if point.instrument_id != batch.instrument.instrument_id or point.kind != batch.kind:
            raise ColdStoreQualityError("衍生品标的或类别与批次不一致")
        order = _derivative_order(point)
        if previous is not None and (order < previous or (order == previous and not allow_equal_revisions)):
            raise ColdStoreQualityError(f"衍生品时间戳与修订未递增：{order}")
        if point.revision < 0 or point.ts_available < point.ts_event or point.ts_committed < point.ts_ingested:
            raise ColdStoreQualityError("衍生品时点或修订非法")
        fits, reason = _fits_decimal128(point.value, scale)
        if not fits:
            raise ColdStoreQualityError(f"{point.kind} {reason}")
        previous = order


def _derivative_content(rows: Sequence[DerivativePoint]) -> bytes:
    def record(point: DerivativePoint) -> str:
        data = point.model_dump(mode="json", exclude={"settled"} if point.settled is None else None)
        value = data["value"]
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        data["value"] = "0" if value == "-0" else value
        return json.dumps(data, sort_keys=True)

    return "".join(record(point) + "\n" for point in rows).encode("utf-8")


def _write_derivative_cold_parquet(path: Path, batch: DerivativeBatch) -> None:
    scale = _derivative_scale(batch.instrument, batch.kind)
    schema = pa.schema([
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("kind", pa.string(), nullable=False),
        pa.field("timeframe", pa.string()),
        pa.field("value", pa.decimal128(COLD_DECIMAL_PRECISION, scale), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("ts_published", pa.int64()),
        pa.field("settled", pa.bool_()),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        pa.field("revision", pa.int64(), nullable=False),
    ])
    table = pa.Table.from_pylist([point.model_dump() for point in batch.rows], schema=schema)
    _write_decimal_parquet(path, table, {"value": scale}, batch.instrument)


def _read_derivative_cold_parquet(
    path: Path, *, start_ts: int | None = None, end_ts: int | None = None,
    historical_kind: DerivativeKind | None = None,
) -> list[DerivativePoint]:
    expression = None
    if historical_kind is DerivativeKind.FUNDING_RATE:
        legacy = ds.field("ts_published") == ds.field("ts_event")
        if "settled" in pq.read_schema(path).names:
            settled = ds.field("settled")
            expression = (settled == ds.scalar(True)) | (settled.is_null() & legacy)
        else:
            expression = legacy
    elif historical_kind is not None:
        expression = ds.field("timeframe").is_valid()
    table = _read_range_table(path, start_ts, end_ts, expression=expression)
    metadata = table.schema.metadata or {}
    if metadata.get(COLD_FORMAT_METADATA) != COLD_FORMAT_DECIMAL128:
        raise ColdStoreIntegrityError(f"未知衍生品冷库数值格式：{path}")
    _verify_decimal_schema(table.schema, _column_scales_from_metadata(metadata, ("value",)))
    return [DerivativePoint.model_validate(row) for row in table.to_pylist()]


def partition_day(ts_event: int) -> str:
    return datetime.fromtimestamp(ts_event // 1000, tz=UTC).date().isoformat()


def tick_column_scales(instrument: InstrumentSpec, kind: TickKind) -> dict[str, int]:
    return {
        column: instrument.price_precision if column.endswith("price") else instrument.qty_precision
        for column in _TICK_DECIMALS[kind]
    }


def validate_tick(
    row: Trade | Quote, instrument: InstrumentSpec, kind: TickKind, scales: dict[str, int],
) -> None:
    expected = Trade if kind is TickKind.TRADES else Quote
    if not isinstance(row, expected) or row.instrument_id != instrument.instrument_id:
        raise ColdStoreQualityError("行情标的或类别与批次不一致")
    if row.ts_event < 0 or row.revision < 0 or row.ts_available < row.ts_event or row.ts_committed < row.ts_ingested:
        raise ColdStoreQualityError("行情时点或修订非法")
    for column, scale in scales.items():
        value = getattr(row, column)
        fits, reason = _fits_decimal128(value, scale)
        if not fits:
            raise ColdStoreQualityError(f"{column} {reason}")
        if value < 0 or (value == 0 and (isinstance(row, Trade) or column.endswith("price"))):
            raise ColdStoreQualityError(f"{column} 非法：{value}")
    if isinstance(row, Trade):
        if row.price % instrument.tick_size != 0:
            raise ColdStoreQualityError(f"price 未对齐 tick_size={instrument.tick_size}")
        if row.size % instrument.qty_step != 0:
            raise ColdStoreQualityError(f"size 未对齐 qty_step={instrument.qty_step}")
    if isinstance(row, Quote) and row.bid_price > row.ask_price:
        raise ColdStoreQualityError("买一价高于卖一价")


def _validate_ticks(batch: TickBatch) -> None:
    scales = tick_column_scales(batch.instrument, batch.kind)
    if any(not 0 <= scale <= COLD_DECIMAL_PRECISION for scale in scales.values()):
        raise ColdStoreQualityError(f"非法行情精度：{scales}")
    previous = None
    for row in batch.rows:
        validate_tick(row, batch.instrument, batch.kind, scales)
        order = tick_order(row)
        if previous is not None and order < previous:
            raise ColdStoreQualityError("行情批次未按事件时间排序")
        previous = order


def _tick_content(batch: TickBatch) -> bytes:
    # 用压缩归档保留完整标准化输入，高频数据不重复保存每行的列名。
    def record(row: Trade | Quote) -> str:
        values = [getattr(row, field) for field in _TICK_TIMES]
        values.append(row.precision.value)
        values.extend(str(getattr(row, column)) for column in _TICK_DECIMALS[batch.kind])
        if isinstance(row, Trade):
            values.extend((row.trade_id.hex, row.side))
        values.extend(getattr(row, name) for name in _TICK_METADATA[batch.kind])
        return json.dumps(values, separators=(",", ":"), default=str)

    header = json.dumps([batch.instrument.instrument_id, batch.kind])
    return gzip.compress(
        (header + "\n" + "\n".join(map(record, batch.rows)) + "\n").encode(),
        compresslevel=1, mtime=0,
    )


def _write_tick_parquet(path: Path, batch: TickBatch) -> None:
    scales = tick_column_scales(batch.instrument, batch.kind)
    fields = [
        *(pa.field(name, pa.int64(), nullable=False) for name in _TICK_TIMES),
        pa.field("precision", pa.dictionary(pa.int8(), pa.string()), nullable=False),
        *(pa.field(name, pa.decimal128(COLD_DECIMAL_PRECISION, scale), nullable=False)
          for name, scale in scales.items()),
        *(pa.field(name, dtype) for name, dtype in _TICK_METADATA[batch.kind].items()),
    ]
    data = {field.name: [getattr(row, field.name) for row in batch.rows] for field in fields}
    for name, dtype in _TICK_METADATA[batch.kind].items():
        if dtype == pa.binary(16):
            data[name] = [value.bytes if value is not None else None for value in data[name]]
    if batch.kind is TickKind.TRADES:
        fields.extend([
            pa.field("trade_id", pa.binary(16), nullable=False),
            pa.field("side", pa.bool_(), nullable=False),
        ])
        data["trade_id"] = [row.trade_id.bytes for row in batch.rows]
        data["side"] = [row.side == "Buy" for row in batch.rows]
    table = pa.Table.from_pydict(data, schema=pa.schema(fields))
    _write_decimal_parquet(path, table, scales, batch.instrument)


def _read_tick_parquet(
    path: Path, instrument_id: str, kind: TickKind, *,
    start_ts: int | None = None, end_ts: int | None = None,
    expression: ds.Expression | None = None,
) -> list[Trade | Quote]:
    table = _read_range_table(path, start_ts, end_ts, expression=expression)
    metadata = table.schema.metadata or {}
    if metadata.get(COLD_FORMAT_METADATA) != COLD_FORMAT_DECIMAL128:
        raise ColdStoreIntegrityError(f"未知行情冷库数值格式：{path}")
    _verify_decimal_schema(table.schema, _column_scales_from_metadata(metadata, _TICK_DECIMALS[kind]))
    rows = []
    for values in table.to_pylist():
        values["precision"] = PrecisionTier(values["precision"])
        for name, dtype in _TICK_METADATA[kind].items():
            if dtype == pa.binary(16) and values.get(name) is not None:
                values[name] = uuid.UUID(bytes=values[name])
        if kind is TickKind.TRADES:
            values["trade_id"] = uuid.UUID(bytes=values["trade_id"])
            values["side"] = "Buy" if values["side"] else "Sell"
            # 旧归档回填保留事件时刻，实时可获取时刻至少为接收时刻。
            if "availability" not in values and values["ts_available"] < values["ts_ingested"]:
                values["availability"] = "inferred"
            rows.append(Trade(instrument_id=instrument_id, **values))
        else:
            rows.append(Quote(instrument_id=instrument_id, **values))
    return rows


def _tick_arrival_ranges(path: Path, start: int, end: int) -> list[tuple[int, int]]:
    with pq.ParquetFile(path) as parquet:
        column = parquet.schema_arrow.get_field_index("ts_available")
        ranges = []
        for index in range(parquet.num_row_groups):
            stats = parquet.metadata.row_group(index).column(column).statistics
            if stats is None or not stats.has_min_max:
                return [(start, end)]
            first, last = max(start, stats.min), min(end, stats.max)
            if first <= last:
                ranges.append((first, last))
    return ranges


def _read_matching_trades(path: Path, instrument_id: str, expression: ds.Expression) -> list[Trade | Quote]:
    # 无命中的文件只扫描 ID 列。
    matches = pq.read_table(path, columns=["trade_id"], filters=expression)
    return _read_tick_parquet(path, instrument_id, TickKind.TRADES, expression=expression) if len(matches) else []


def _trade_id_filter(ids: set[bytes]) -> ds.Expression:
    return ds.field("trade_id").isin(pa.array(ids, type=pa.binary(16)))


def partition_month(ts_event: int) -> str:
    return datetime.fromtimestamp(ts_event // 1000, tz=UTC).strftime("%Y-%m")


def _generation_from_row(
    instrument: InstrumentSpec, timeframe: str, row: tuple
) -> ColdGeneration:
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


def _select_generations(
    connection: sqlite3.Connection,
    instrument: InstrumentSpec,
    timeframe: str,
    *,
    start_ts: int | None = None,
    end_ts: int | None = None,
) -> list[ColdGeneration]:
    query = f"""
        SELECT {_GENERATION_COLUMNS} FROM generations
        WHERE venue = ? AND symbol = ? AND timeframe = ?
    """
    params: list[str | int] = [instrument.venue, instrument.symbol, timeframe]
    if start_ts is not None:
        query += " AND last_ts >= ?"
        params.append(start_ts)
    if end_ts is not None:
        query += " AND first_ts <= ?"
        params.append(end_ts)
    return [
        _generation_from_row(instrument, timeframe, row)
        for row in connection.execute(query + " ORDER BY rowid", params)
    ]


def _path_under_root(data_dir: Path, relative: Path) -> Path:
    root = data_dir.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ColdStoreIntegrityError(f"清单路径越界：{relative}")
    return candidate


def _verify_generation_file(data_dir: Path, generation: ColdGeneration) -> Path:
    path = _path_under_root(data_dir, generation.parquet_path)
    if _sha256_file(path) != generation.sha256:
        raise ColdStoreIntegrityError(f"generation 文件哈希不符：{generation.parquet_path}")
    return path


def _manifest_version(db: sqlite3.Connection) -> int:
    versioned = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manifest_version'"
    ).fetchone()
    # 旧清单及首次初始化中的空清单没有版本记录。
    row = db.execute("SELECT version FROM manifest_version WHERE id = 1").fetchone() if versioned else None
    return 0 if row is None else int(row[0])


class ColdReader:
    """按一次清单查询读取不可变文件，常驻服务停止后也可用。"""

    def __init__(self, data_dir: Path | str, *, snapshot: ColdSnapshot | None = None) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._db_path = self._data_dir / MANIFEST_DB
        self._snapshot = snapshot
        self._pinned_series: dict[tuple[str, str, str], list[ColdGeneration]] = {}
        self._verified_ticks: dict[ColdGeneration, Path] = {}
        self._trade_page: tuple[InstrumentSpec, int, list[Trade | Quote]] | None = None
        if snapshot is not None:
            for manifest in (snapshot.bars, snapshot.derivatives, snapshot.ticks):
                if manifest is not None:
                    for generation in manifest.generations:
                        key = generation.venue, generation.symbol, generation.timeframe
                        self._pinned_series.setdefault(key, []).append(generation)

    def capture_snapshot(
        self, instruments: Iterable[tuple[InstrumentSpec, int]], *, tick_instruments: Collection[str] = (),
    ) -> ColdSnapshot:
        """固定标的截至指定事件时间的文件，保留跨周期预热和历史衍生品所需的数据。"""
        if not self._db_path.is_file():
            empty = ColdManifest(0, ())
            return ColdSnapshot(empty, empty, empty if tick_instruments else None)
        with closing(sqlite3.connect(f"{self._db_path.as_uri()}?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            version = _manifest_version(db)
            bars = []
            derivatives = []
            ticks = []
            for instrument, end in instruments:
                for timeframe in SUPPORTED_TIMEFRAMES:
                    bars.extend(_select_generations(db, instrument, timeframe, end_ts=end))
                for kind in DerivativeKind:
                    derivatives.extend(_select_generations(db, instrument, kind.value, end_ts=end))
                if instrument.instrument_id in tick_instruments:
                    for kind in TickKind:
                        ticks.extend(_select_generations(db, instrument, kind.value, end_ts=end))
        return ColdSnapshot(
            ColdManifest(version, tuple(bars)), ColdManifest(version, tuple(derivatives)),
            ColdManifest(version, tuple(ticks)) if tick_instruments else None,
        )

    def check_snapshot_files(self) -> None:
        if self._snapshot is None:
            return
        for series in self._pinned_series.values():
            for generation in series:
                if not _path_under_root(self._data_dir, generation.parquet_path).is_file():
                    raise ColdStoreIntegrityError(f"回放清单文件缺失：{generation.parquet_path}")

    def read_bars(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        as_of: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[RawBar]:
        revisions = self.read_revisions(instrument, timeframe, start_ts=start_ts, end_ts=end_ts)
        return latest_visible_bars(revisions, as_of=as_of)

    def read_revisions(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[RawBar]:
        """从同一版清单读取范围内的全部修订。"""
        _, revisions = self.read_manifest_revisions(
            instrument, timeframe, start_ts=start_ts, end_ts=end_ts,
        )
        return revisions

    def read_manifest_revisions(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> tuple[int, list[RawBar]]:
        """返回同一 SQLite 读事务选出的清单代号及对应文件中的修订。"""
        manifest, revisions = self.read_manifest_snapshot(
            instrument, timeframe, start_ts=start_ts, end_ts=end_ts,
        )
        return manifest.version, revisions

    def read_manifest_snapshot(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> tuple[ColdManifest, list[RawBar]]:
        """返回选出的清单及其修订，文件退役时整次重读一次。"""
        return self._read_manifest(
            instrument, timeframe, _read_cold_parquet, start_ts=start_ts, end_ts=end_ts,
        )

    def read_derivative_revisions(
        self, instrument: InstrumentSpec, kind: DerivativeKind, *,
        start_ts: int | None = None, end_ts: int | None = None,
        historical_only: bool = False,
    ) -> list[DerivativePoint]:
        def read(path: Path, *, start_ts: int | None, end_ts: int | None) -> list[DerivativePoint]:
            return _read_derivative_cold_parquet(
                path, start_ts=start_ts, end_ts=end_ts, historical_kind=kind if historical_only else None,
            )

        _, rows = self._read_manifest(
            instrument, kind.value, read, start_ts=start_ts, end_ts=end_ts,
        )
        return rows

    def read_derivatives(
        self, instrument: InstrumentSpec, kind: DerivativeKind, *,
        start_ts: int | None = None, end_ts: int | None = None, as_of: int | None = None,
        historical_only: bool = False,
    ) -> list[DerivativePoint]:
        return latest_visible_derivatives(
            self.read_derivative_revisions(
                instrument, kind, start_ts=start_ts, end_ts=end_ts, historical_only=historical_only,
            ),
            as_of=as_of,
        )

    def read_tick_revisions(
        self, instrument: InstrumentSpec, kind: TickKind, *,
        start_ts: int | None = None, end_ts: int | None = None,
        event_times: set[int] | None = None, as_of: int | None = None,
    ) -> list[Trade | Quote]:
        cutoff = ds.field("ts_available") <= as_of if as_of is not None else None
        events = ds.field("ts_event").isin(event_times) if event_times is not None else None
        expression = events
        if cutoff is not None:
            expression = cutoff if events is None else events & cutoff

        def read(generations: Sequence[ColdGeneration]) -> list[Trade | Quote]:
            return [
                row for generation in generations for row in _read_tick_parquet(
                    self._verify_tick_file(generation), instrument.instrument_id, kind,
                    start_ts=start_ts, end_ts=end_ts, expression=expression,
                )
            ]

        _, rows = self._read_generations(
            instrument, kind.value, read, start_ts=start_ts, end_ts=end_ts,
        )
        return rows

    def tick_arrivals(
        self, instrument: InstrumentSpec, start: int, end: int,
    ) -> Iterator[Trade | Quote]:
        """按可获取时间读取有数据的页，包含事件时间落在页外的迟到记录。"""
        if end < start:
            return
        pending = []
        available = ds.field("ts_available")

        def spans(generations: Sequence[ColdGeneration]) -> list[tuple[int, int, ColdGeneration]]:
            return [
                (first, last, generation)
                for generation in generations
                for first, last in _tick_arrival_ranges(
                    _path_under_root(self._data_dir, generation.parquet_path), max(start, generation.first_ts), end,
                )
            ]

        for kind in TickKind:
            _, ranges = self._read_generations(instrument, kind.value, spans, start_ts=None, end_ts=end)
            for first, last, generation in ranges:
                pending.append((first, last, kind, len(pending), generation))
        pending.sort(reverse=True)
        active = []
        while pending or active:
            opening = max(start, pending[-1][0]) if not active else opening + _TICK_REPLAY_PAGE_MS
            if opening > end:
                break
            close = min(end, opening + _TICK_REPLAY_PAGE_MS - 1)
            while pending and pending[-1][0] <= close:
                _, last, kind, index, generation = pending.pop()
                heappush(active, (last, index, kind, generation))
            expression = (available >= opening) & (available <= close)
            generations = {(kind, generation) for _, _, kind, generation in active}
            rows = [
                row for kind, generation in generations for row in _read_tick_parquet(
                    self._verify_tick_file(generation), instrument.instrument_id, kind, expression=expression,
                )
            ]
            yield from sorted(rows, key=lambda row: row.ts_available)
            while active and active[0][0] <= close:
                heappop(active)

    def read_trade_revisions_by_id(self, instrument: InstrumentSpec, ids: set[bytes]) -> list[Trade]:
        expression = _trade_id_filter(ids)

        def read(path: Path, **_bounds) -> list[Trade | Quote]:
            return _read_matching_trades(path, instrument.instrument_id, expression)

        _, rows = self._read_manifest(
            instrument, TickKind.TRADES.value, read, start_ts=None, end_ts=None,
        )
        return [row for row in rows if isinstance(row, Trade)]

    def _trade_range_revisions(
        self, instrument: InstrumentSpec, start_ts: int | None, end_ts: int | None,
    ) -> list[Trade | Quote]:
        def read(generations: Sequence[ColdGeneration]) -> list[Trade | Quote]:
            paths: dict[ColdGeneration, Path] = {}
            rows: list[Trade | Quote] = []
            lower = 0 if start_ts is None else start_ts
            upper = end_ts if end_ts is not None else (1 << 63) - 1
            for generation in generations:
                if generation.last_ts >= lower and generation.first_ts <= upper:
                    path = self._verify_tick_file(generation)
                    paths[generation] = path
                    rows.extend(_read_tick_parquet(
                        path, instrument.instrument_id, TickKind.TRADES, start_ts=start_ts, end_ts=end_ts,
                    ))
            outside = [
                generation for generation in generations
                if generation.first_ts < lower or generation.last_ts > upper
            ]
            if rows and outside:
                # 同一成交 ID 的另一来源或修订可能在范围外，仍须由本次清单选版。
                ids = {row.trade_id.bytes for row in rows if isinstance(row, Trade)}
                ts = ds.field("ts_event")
                expression = _trade_id_filter(ids) & ((ts < lower) | (ts > upper))
                for generation in outside:
                    path = paths.get(generation) or self._verify_tick_file(generation)
                    rows.extend(_read_matching_trades(path, instrument.instrument_id, expression))
            return rows

        _, rows = self._read_generations(instrument, TickKind.TRADES.value, read, start_ts=None, end_ts=None)
        return rows

    def _verify_tick_file(self, generation: ColdGeneration) -> Path:
        if self._snapshot is None:
            return _verify_generation_file(self._data_dir, generation)
        # 回放只读取不可变的固定文件，同一运行校验每个文件一次。
        path = self._verified_ticks.get(generation)
        if path is None:
            path = self._verified_ticks[generation] = _verify_generation_file(self._data_dir, generation)
        return path

    def _replay_trade_range(self, instrument: InstrumentSpec, start: int, end: int) -> list[Trade | Quote]:
        page_start = start // _TICK_REPLAY_PAGE_MS * _TICK_REPLAY_PAGE_MS
        page_end = min(page_start + _TICK_REPLAY_PAGE_MS - 1, (1 << 63) - 1)
        if end > page_end:
            return self._trade_range_revisions(instrument, start, end)
        if self._trade_page is None or self._trade_page[:2] != (instrument, page_start):
            self._trade_page = instrument, page_start, self._trade_range_revisions(instrument, page_start, page_end)
        rows = self._trade_page[2]
        ids = {row.trade_id for row in rows if start <= row.ts_event <= end}
        return [row for row in rows if row.trade_id in ids]

    def read_ticks(
        self, instrument: InstrumentSpec, kind: TickKind, *,
        start_ts: int | None = None, end_ts: int | None = None, as_of: int | None = None,
    ) -> list[Trade | Quote]:
        if kind is TickKind.TRADES:
            if (
                self._snapshot is not None and start_ts is not None and end_ts is not None
                and 0 <= end_ts - start_ts < _TICK_REPLAY_PAGE_MS
            ):
                rows = self._replay_trade_range(instrument, start_ts, end_ts)
            else:
                rows = self._trade_range_revisions(instrument, start_ts, end_ts)
            # 推定时间只补实测没有覆盖的成交。先按来源选定，再截断可获取时间。
            measured = {row.trade_id for row in rows if isinstance(row, Trade) and row.availability == "measured"}
            rows = [
                row for row in rows
                if isinstance(row, Trade) and (row.availability == "measured" or row.trade_id not in measured)
            ]
        else:
            rows = self.read_tick_revisions(instrument, kind, start_ts=start_ts, end_ts=end_ts)
        visible = latest_visible_revisions(
            sorted(rows, key=tick_order), as_of=as_of, identity=tick_identity,
            ingested=lambda row: row.ts_ingested,
        )
        in_range = [
            row for row in visible
            if (start_ts is None or row.ts_event >= start_ts) and (end_ts is None or row.ts_event <= end_ts)
        ]
        ordered = []
        for _, group in groupby(sorted(in_range, key=tick_order), key=lambda row: row.ts_event):
            ties = list(group)
            if any(not relative_tick_order_known(first, second) for first, second in pairwise(ties)):
                ties = [replace(row, order_uncertain=True) for row in ties]
            ordered.extend(ties)
        return ordered

    def _read_manifest[T](
        self, instrument: InstrumentSpec, series: str, read_parquet: Callable[..., list[T]], *,
        start_ts: int | None, end_ts: int | None,
    ) -> tuple[ColdManifest, list[T]]:
        def read(generations: Sequence[ColdGeneration]) -> list[T]:
            return [
                row for generation in generations
                for row in read_parquet(
                    _verify_generation_file(self._data_dir, generation), start_ts=start_ts, end_ts=end_ts,
                )
            ]

        return self._read_generations(instrument, series, read, start_ts=start_ts, end_ts=end_ts)

    def _read_generations[T](
        self, instrument: InstrumentSpec, series: str, read: Callable[[Sequence[ColdGeneration]], list[T]], *,
        start_ts: int | None, end_ts: int | None,
    ) -> tuple[ColdManifest, list[T]]:
        if self._snapshot is not None:
            key = instrument.venue, instrument.symbol, series
            if series in SUPPORTED_TIMEFRAMES:
                pinned = self._snapshot.bars
            elif series in (TickKind.TRADES, TickKind.QUOTES):
                pinned = self._snapshot.ticks
                if pinned is None:
                    raise ColdStoreIntegrityError("回放清单缺少逐笔与盘口文件清单")
            else:
                pinned = self._snapshot.derivatives
            generations = tuple(
                item for item in self._pinned_series.get(key, ())
                if (start_ts is None or item.last_ts >= start_ts) and (end_ts is None or item.first_ts <= end_ts)
            )
            try:
                return ColdManifest(pinned.version, generations), read(generations)
            except FileNotFoundError as exc:
                raise ColdStoreIntegrityError(f"回放清单文件缺失：{exc.filename or exc}") from exc
        if not self._db_path.is_file():
            return ColdManifest(0, ()), []
        for attempt in range(2):
            with closing(sqlite3.connect(f"{self._db_path.as_uri()}?mode=ro", uri=True)) as db:
                db.execute("BEGIN")
                version = _manifest_version(db)
                generations = _select_generations(
                    db, instrument, series, start_ts=start_ts, end_ts=end_ts,
                )
            try:
                decoded = read(generations)
            except FileNotFoundError as exc:
                if attempt:
                    raise ColdStoreIntegrityError(
                        f"generation 文件缺失：{exc.filename or exc}"
                    ) from exc
            else:
                return ColdManifest(version, tuple(generations)), decoded
        raise AssertionError("清单读取重试结束")


class ColdStore:
    """冷库单写者。``data_manifest.sqlite`` 是发布与读取的唯一真相。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._reader = ColdReader(self._data_dir)
        self._fault_hook = fault_hook
        self._lock = threading.RLock()
        self._closed = False
        self._db_path = self._data_dir / MANIFEST_DB
        claim_writer(self._db_path)
        try:
            self._connection = open_writer_connection(self._db_path)
            try:
                for statement in _SCHEMA_SQL:
                    self._connection.execute(statement)
            except BaseException:
                self._connection.close()
                raise
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
        """原子发布同月的尾部批次，复用清单里同内容的 generation。"""

        with self._lock:
            self._ensure_open()
            _validate_bars(batch.bars, instrument=batch.instrument, timeframe=batch.timeframe)
            if partition_month(batch.bars[0].ts_event) != partition_month(batch.bars[-1].ts_event):
                raise ColdStoreQualityError("尾部批次不能跨月")
            generation_id = _sha256_bytes(_content_bytes(batch.bars, canonical_decimals=True))
            existing = self._select_generation(batch, generation_id)
            if existing is not None:
                _verify_generation_file(self._data_dir, existing)
                return existing
            existing = self._select_matching_generation(batch)
            if existing is not None:
                return existing

            generation = self._prepare_generation(batch, generation_id)
            self._commit_manifest(generation)
            return generation

    def publish_derivatives(
        self, instrument: InstrumentSpec, kind: DerivativeKind, rows: Sequence[DerivativePoint],
    ) -> ColdGeneration:
        """发布同月、同类别的衍生品修订。"""
        batch = DerivativeBatch(instrument, kind, rows)
        with self._lock:
            self._ensure_open()
            if not rows:
                raise ColdStoreQualityError("衍生品批次为空，拒绝发布")
            _validate_derivatives(batch)
            if partition_month(rows[0].ts_event) != partition_month(rows[-1].ts_event):
                raise ColdStoreQualityError("尾部批次不能跨月")
            generation_id = _sha256_bytes(_derivative_content(rows))
            existing = self._select_generation(batch, generation_id)
            if existing is not None:
                _verify_generation_file(self._data_dir, existing)
                return existing
            generation = self._prepare_generation(batch, generation_id)
            self._commit_manifest(generation)
            return generation

    def publish_ticks(
        self, instrument: InstrumentSpec, kind: TickKind, rows: Sequence[Trade | Quote],
    ) -> ColdGeneration:
        """原子发布一个 UTC 日内的行情批次。"""
        with self._lock:
            self._ensure_open()
            batch = TickBatch(instrument, kind, sorted(rows, key=tick_order))
            if not batch.rows or partition_day(batch.rows[0].ts_event) != partition_day(batch.rows[-1].ts_event):
                raise ColdStoreQualityError("行情批次必须属于同一 UTC 日且非空")
            _validate_ticks(batch)
            content = _tick_content(batch)
            generation_id = _sha256_bytes(content)
            existing = self._select_generation(batch, generation_id)
            if existing is not None:
                _verify_generation_file(self._data_dir, existing)
                return existing
            generation = self._prepare_generation(batch, generation_id, tick_content=content)
            self._commit_manifest(generation)
            return generation

    def read_tick_revisions(
        self, instrument: InstrumentSpec, kind: TickKind, *, start_ts: int, end_ts: int,
        event_times: set[int] | None = None,
    ) -> list[Trade | Quote]:
        self._ensure_open()
        return self._reader.read_tick_revisions(
            instrument, kind, start_ts=start_ts, end_ts=end_ts, event_times=event_times,
        )

    def read_trade_revisions_by_id(self, instrument: InstrumentSpec, ids: set[bytes]) -> list[Trade]:
        self._ensure_open()
        return self._reader.read_trade_revisions_by_id(instrument, ids)

    def generations(self, instrument: InstrumentSpec, timeframe: str) -> list[ColdGeneration]:
        with self._lock:
            self._ensure_open()
            return _select_generations(self._connection, instrument, timeframe)

    def read_bars(
        self,
        instrument: InstrumentSpec,
        timeframe: str,
        *,
        as_of: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[RawBar]:
        self._ensure_open()
        return self._reader.read_bars(
            instrument, timeframe, as_of=as_of, start_ts=start_ts, end_ts=end_ts,
        )

    def read_revisions(
        self, instrument: InstrumentSpec, timeframe: str, *, start_ts: int, end_ts: int,
    ) -> list[RawBar]:
        self._ensure_open()
        return self._reader.read_revisions(instrument, timeframe, start_ts=start_ts, end_ts=end_ts)

    def read_derivative_revisions(
        self, instrument: InstrumentSpec, kind: DerivativeKind, *, start_ts: int, end_ts: int,
    ) -> list[DerivativePoint]:
        self._ensure_open()
        return self._reader.read_derivative_revisions(instrument, kind, start_ts=start_ts, end_ts=end_ts)

    def read_derivatives(
        self, instrument: InstrumentSpec, kind: DerivativeKind, *,
        start_ts: int | None = None, end_ts: int | None = None, as_of: int | None = None,
        historical_only: bool = False,
    ) -> list[DerivativePoint]:
        self._ensure_open()
        return self._reader.read_derivatives(
            instrument, kind, start_ts=start_ts, end_ts=end_ts, as_of=as_of, historical_only=historical_only,
        )

    def compact(self, instruments: Sequence[InstrumentSpec], *, now_ms: int | None = None) -> None:
        """合并同月、同标的规格的文件，保留每一行。"""

        specs = {(spec.venue, spec.symbol): spec for spec in instruments}
        with self._lock:
            self._ensure_open()
            series = self._connection.execute(
                "SELECT DISTINCT venue, symbol, timeframe FROM generations"
            ).fetchall()
            for venue, symbol, timeframe in series:
                instrument = specs.get((venue, symbol))
                if instrument is None:
                    continue
                if timeframe in TickKind:
                    self._compact_ticks(instrument, TickKind(timeframe), now_ms)
                else:
                    self._compact_series(instrument, timeframe, now_ms)
            self._prune_retired(time.time_ns() // 1_000_000 if now_ms is None else now_ms)

    def _compact_ticks(self, instrument: InstrumentSpec, kind: TickKind, now_ms: int | None) -> None:
        # 活跃日保持分钟尾部。已结束的日分批合并，限制读取与质检的峰值内存。
        version = instrument_spec_version(instrument)
        today = partition_day(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
        tail_prefix = f"tail-{version}-"
        partition_prefix = f"partition-{version}-"
        days: dict[str, list[ColdGeneration]] = {}
        for generation in _select_generations(self._connection, instrument, kind.value):
            day = partition_day(generation.first_ts)
            if day < today and generation.parquet_path.name.startswith((tail_prefix, partition_prefix)):
                days.setdefault(day, []).append(generation)
        for generations in days.values():
            group: list[ColdGeneration] = []
            size = 0
            for generation in generations:
                if group and size + generation.bars > TICK_BATCH_ROWS:
                    self._compact_tick_group(instrument, kind, group, now_ms)
                    group, size = [], 0
                group.append(generation)
                size += generation.bars
            self._compact_tick_group(instrument, kind, group, now_ms)

    def _compact_tick_group(
        self, instrument: InstrumentSpec, kind: TickKind,
        group: Sequence[ColdGeneration], now_ms: int | None,
    ) -> None:
        if len(group) < 2 or not any(item.parquet_path.name.startswith("tail-") for item in group):
            return
        rows = [
            row for item in group for row in _read_tick_parquet(
                _verify_generation_file(self._data_dir, item), instrument.instrument_id, kind,
            )
        ]
        rows.sort(key=tick_order)
        archive = "".join(
            json.dumps({"source_sha256": item.content_sha256}) + "\n" for item in group
        ).encode("utf-8")
        generation = self._prepare_generation(
            TickBatch(instrument, kind, rows), _sha256_bytes(b"tick-partition\n" + archive), source_archive=archive,
        )
        self._commit_manifest(generation, replaced=group, retired_ts=now_ms)

    def _compact_series(
        self, instrument: InstrumentSpec, timeframe: str, now_ms: int | None
    ) -> None:
        # 旧规格的文件保留。只合并同月中最后一段同规格文件，维持跨规格并列行的优先级。
        version = instrument_spec_version(instrument)
        tail_prefix = f"tail-{version}-"
        partition_prefix = f"partition-{version}-"
        groups: dict[str, list[ColdGeneration]] = {}
        for generation in _select_generations(self._connection, instrument, timeframe):
            month = partition_month(generation.first_ts)
            if generation.parquet_path.name.startswith((tail_prefix, partition_prefix)):
                groups.setdefault(month, []).append(generation)
            else:
                last_month = partition_month(generation.last_ts)
                for group_month in list(groups):
                    if month <= group_month <= last_month:
                        groups.pop(group_month)
        for group in groups.values():
            if not any(item.parquet_path.name.startswith(tail_prefix) for item in group):
                continue
            if timeframe in DerivativeKind:
                points = [
                    point for item in group for point in _read_derivative_cold_parquet(
                        _verify_generation_file(self._data_dir, item),
                    )
                ]
                points.sort(key=_derivative_order)
                batch = DerivativeBatch(instrument, DerivativeKind(timeframe), points)
                content = _derivative_content(points)
            else:
                bars = [bar for item in group for bar in self._read_generation(item)]
                bars.sort(key=lambda bar: (bar.ts_event, bar.revision))
                batch = ColdBatch(instrument, timeframe, bars)
                content = _content_bytes(bars, canonical_decimals=True)
            # 与尾部批次的内容标识分开，清单交换前可安全重试。
            generation_id = _sha256_bytes(b"partition\n" + content)
            archive = "".join(
                json.dumps({"source_sha256": item.content_sha256}) + "\n" for item in group
            ).encode("utf-8")
            generation = self._prepare_generation(batch, generation_id, source_archive=archive)
            self._commit_manifest(generation, replaced=group, retired_ts=now_ms)

    def _prune_retired(self, now: int) -> None:
        retired = self._connection.execute(
            """
            SELECT parquet_path FROM retired_files
            WHERE retired_ts <= ?
                AND manifest_version <= (SELECT version - ? FROM manifest_version)
            """,
            (now - RETENTION_MS, RETAINED_MANIFESTS),
        ).fetchall()
        for (relative,) in retired:
            _path_under_root(self._data_dir, Path(relative)).unlink(missing_ok=True)
            self._connection.execute("DELETE FROM retired_files WHERE parquet_path = ?", (relative,))

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

    def _archive_content(self, content: bytes, sha256: str, *, compressed: bool = False) -> ArchivedBatch:
        self._hook(ColdPhase.ARCHIVE_BEFORE)
        suffix = ".jsonl.gz" if compressed else ".jsonl"
        path = self._data_dir / ARCHIVE_SUBDIR / f"{sha256}{suffix}"
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

    def _write_staging(self, archived: ArchivedBatch, batch: _PublishBatch) -> None:
        self._hook(ColdPhase.STAGING_BEFORE)
        staging_dir = (self._data_dir / STAGING_SUBDIR) / archived.sha256
        # 上一次崩溃可能留下残片；发布永远从干净 staging 重建，绝不信任旧文件。
        if staging_dir.exists():
            shutil.rmtree(staging_dir)
        staging_dir.mkdir(parents=True)
        temporary = staging_dir / "bars.parquet.tmp"
        if isinstance(batch, DerivativeBatch):
            _write_derivative_cold_parquet(temporary, batch)
        elif isinstance(batch, TickBatch):
            _write_tick_parquet(temporary, batch)
        else:
            _write_cold_parquet(temporary, batch.bars, instrument=batch.instrument)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, staging_dir / "bars.parquet")
        _fsync_directory(staging_dir)
        self._hook(ColdPhase.STAGING_AFTER)

    def _quality_check(
        self, archived: ArchivedBatch, batch: _PublishBatch, *, compacted: bool
    ) -> None:
        self._hook(ColdPhase.QC_BEFORE)
        path = self._staging_file(archived)
        if isinstance(batch, DerivativeBatch):
            decoded = _read_derivative_cold_parquet(path)
            _validate_derivatives(
                DerivativeBatch(batch.instrument, batch.kind, decoded), allow_equal_revisions=compacted,
            )
        elif isinstance(batch, TickBatch):
            decoded = _read_tick_parquet(path, batch.instrument.instrument_id, batch.kind)
            _validate_ticks(TickBatch(batch.instrument, batch.kind, decoded))
        else:
            decoded = _read_cold_parquet(path)
            _validate_bars(
                decoded, instrument=batch.instrument, timeframe=batch.timeframe,
                allow_equal_revisions=compacted,
            )
        if decoded != list(batch.rows):
            raise ColdStoreIntegrityError("staging Parquet 读回内容与输入批次不一致")
        self._hook(ColdPhase.QC_AFTER)

    def _prepare_generation(
        self, batch: _PublishBatch, generation_id: str, *,
        source_archive: bytes | None = None, tick_content: bytes | None = None,
    ) -> ColdGeneration:
        compacted = source_archive is not None
        if source_archive is not None:
            content = source_archive
        elif tick_content is not None:
            content = tick_content
        elif isinstance(batch, DerivativeBatch):
            content = _derivative_content(batch.rows)
        else:
            content = _content_bytes(batch.bars)
        archived = self._archive_content(content, _sha256_bytes(content), compressed=tick_content is not None)
        self._write_staging(archived, batch)
        self._quality_check(archived, batch, compacted=compacted)
        final = self._promote(archived, batch, generation_id, compacted=compacted)
        return ColdGeneration(
            generation_id=generation_id,
            venue=batch.instrument.venue,
            symbol=batch.instrument.symbol,
            timeframe=batch.timeframe,
            parquet_path=final.relative_to(self._data_dir),
            sha256=_sha256_file(final),
            content_sha256=archived.sha256,
            first_ts=batch.rows[0].ts_event,
            last_ts=batch.rows[-1].ts_event,
            bars=len(batch.rows),
            committed_ts=max(row.ts_committed for row in batch.rows),
        )

    def _promote(
        self, archived: ArchivedBatch, batch: _PublishBatch, generation_id: str, *, compacted: bool
    ) -> Path:
        self._hook(ColdPhase.RENAME_BEFORE)
        kind = "partition" if compacted else "tail"
        version = instrument_spec_version(batch.instrument)
        partition = partition_day if isinstance(batch, TickBatch) else partition_month
        final = (
            self._data_dir / COLD_SUBDIR
            / _safe_path_part(batch.instrument.venue, "venue")
            / _safe_path_part(batch.instrument.symbol, "symbol")
            / _safe_path_part(batch.timeframe, "timeframe")
            / partition(batch.rows[0].ts_event)
            / f"{kind}-{version}-{generation_id}.parquet"
        )
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

    def _commit_manifest(
        self,
        generation: ColdGeneration,
        *,
        replaced: Sequence[ColdGeneration] = (),
        retired_ts: int | None = None,
    ) -> None:
        self._hook(ColdPhase.MANIFEST_BEFORE)
        connection = self._connection
        try:
            connection.execute("BEGIN IMMEDIATE")
            (version,) = connection.execute(
                "UPDATE manifest_version SET version = version + 1 RETURNING version"
            ).fetchone()
            if retired_ts is None:
                retired_ts = time.time_ns() // 1_000_000
            connection.executemany(
                "INSERT INTO retired_files VALUES (?, ?, ?)",
                [(item.parquet_path.as_posix(), retired_ts, version) for item in replaced],
            )
            connection.executemany(
                "DELETE FROM generations WHERE venue = ? AND symbol = ? AND timeframe = ? "
                "AND generation_id = ?",
                [(item.venue, item.symbol, item.timeframe, item.generation_id) for item in replaced],
            )
            # 重发已被合并的原批次时，重新引用完整文件并取消其回收。
            connection.execute(
                "DELETE FROM retired_files WHERE parquet_path = ?",
                (generation.parquet_path.as_posix(),),
            )
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
        self._hook(ColdPhase.MANIFEST_AFTER)

    # ------------------------------------------------------------------ 清单查询

    def _select_matching_generation(self, batch: ColdBatch) -> ColdGeneration | None:
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
                max(bar.ts_committed for bar in batch.bars),
            ),
        )
        bars = list(batch.bars)
        for row in rows:
            generation = _generation_from_row(batch.instrument, batch.timeframe, row)
            if generation is not None and self._read_generation(generation) == bars:
                return generation
        return None

    def _select_generation(
        self, batch: _PublishBatch, generation_id: str
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
        return _generation_from_row(batch.instrument, batch.timeframe, row) if row else None

    def _read_generation(self, generation: ColdGeneration) -> list[RawBar]:
        return _read_cold_parquet(_verify_generation_file(self._data_dir, generation))

    def _hook(self, phase: ColdPhase) -> None:
        if self._fault_hook is not None:
            self._fault_hook(phase)

    def _ensure_open(self) -> None:
        if self._closed:
            raise ColdStoreError("ColdStore 已关闭")


def tick_capacity(data_dir: Path, day: str | None = None) -> list[tuple[str, str, str, str, int, int]]:
    """按 UTC 日统计清单当前引用的逐笔和买一卖一 Parquet 行数与字节数。"""
    data_dir = data_dir.resolve()
    db_path = data_dir / MANIFEST_DB
    if not db_path.is_file():
        return []
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
        entries = db.execute(
            "SELECT venue, symbol, timeframe, parquet_path, first_ts, bars FROM generations "
            "WHERE timeframe IN (?, ?)",
            (TickKind.TRADES, TickKind.QUOTES),
        ).fetchall()
    totals: dict[tuple[str, str, str, str], tuple[int, int]] = {}
    for venue, symbol, kind, path, first_ts, count in entries:
        partition = partition_day(first_ts)
        if day is not None and day != partition:
            continue
        key = partition, venue, symbol, kind
        rows, size = totals.get(key, (0, 0))
        totals[key] = rows + count, size + _path_under_root(data_dir, Path(path)).stat().st_size
    return [(*key, *values) for key, values in sorted(totals.items())]
