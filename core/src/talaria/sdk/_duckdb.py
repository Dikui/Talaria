"""基于 DuckDB（纯内存只读）与 Arrow 的轻量研究分析与跨标的 ASOF JOIN 辅助。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import duckdb
import pyarrow as pa

from talaria.foundation.errors import InvalidRequestError
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.perception.cold_store import ColdManifest, ColdReader
from talaria.sdk._resolve import default_data_dir, resolve_instrument_spec

if TYPE_CHECKING:
    from pandas import DataFrame

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_READ_ONLY_HEAD = re.compile(r"^\s*(?:SELECT|WITH|FROM|DESCRIBE|SHOW|SUMMARIZE)\b", re.IGNORECASE)
_FORBIDDEN_KEYWORDS = re.compile(
    r"\b(?:ATTACH|DETACH|COPY|EXPORT|IMPORT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|TRUNCATE|REPLACE|PRAGMA)\b",
    re.IGNORECASE,
)


def _validate_identifier(name: str, label: str = "标识符") -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise InvalidRequestError(f"非法的{label}：{name!r}")
    return name


def _validate_readonly_sql(sql: str) -> str:
    stripped = sql.strip()
    if not stripped:
        raise InvalidRequestError("DuckDB 查询语句不能为空")
    without_trailing = stripped.rstrip(";").strip()
    if ";" in without_trailing:
        raise InvalidRequestError("仅允许执行单条只读 DuckDB 查询语句")
    if not _READ_ONLY_HEAD.match(without_trailing) or _FORBIDDEN_KEYWORDS.search(without_trailing):
        raise InvalidRequestError("DuckDB 研究接口仅允许只读 SELECT/WITH 分析查询，禁止持久化写入")
    return without_trailing


def _to_arrow_relation(value: Any) -> Any:
    if hasattr(value, "to_arrow") and callable(value.to_arrow):
        return value.to_arrow()
    if isinstance(value, (pa.Table, pa.RecordBatch)):
        return value
    return value


def _manifest_table(root: Path) -> pa.Table:
    reader = ColdReader(root)
    by_key: dict[tuple[str, str], object] = {}
    for spec in reader.list_instrument_specs():
        by_key.setdefault((spec.venue, spec.symbol), spec)
    for spec in get_market_pack(DEFAULT_PACK).instruments():
        by_key.setdefault((spec.venue, spec.symbol), spec)
    specs = list(by_key.values())
    max_end = 2**63 - 1
    snapshot = reader.capture_snapshot(
        [(spec, max_end) for spec in specs],
        tick_instruments=[spec.instrument_id for spec in specs],
    )
    rows: list[dict[str, object]] = []
    datasets: tuple[tuple[str, ColdManifest | None], ...] = (
        ("bars", snapshot.bars),
        ("derivatives", snapshot.derivatives),
        ("ticks", snapshot.ticks),
    )
    for dataset, manifest in datasets:
        if manifest is None:
            continue
        for gen in manifest.generations:
            rows.append({
                "dataset": dataset,
                "generation_id": gen.generation_id,
                "venue": gen.venue,
                "symbol": gen.symbol,
                "timeframe": gen.timeframe,
                "parquet_path": str((root / gen.parquet_path).resolve()),
                "relative_path": gen.parquet_path.as_posix(),
                "sha256": gen.sha256,
                "first_ts": gen.first_ts,
                "last_ts": gen.last_ts,
                "rows": gen.bars,
                "committed_ts": gen.committed_ts,
            })
    schema = pa.schema([
        pa.field("dataset", pa.string(), nullable=False),
        pa.field("generation_id", pa.string(), nullable=False),
        pa.field("venue", pa.string(), nullable=False),
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("parquet_path", pa.string(), nullable=False),
        pa.field("relative_path", pa.string(), nullable=False),
        pa.field("sha256", pa.string(), nullable=False),
        pa.field("first_ts", pa.int64(), nullable=False),
        pa.field("last_ts", pa.int64(), nullable=False),
        pa.field("rows", pa.int64(), nullable=False),
        pa.field("committed_ts", pa.int64(), nullable=False),
    ])
    return pa.Table.from_pylist(rows, schema=schema)


def cold_parquet_paths(
    instrument: str,
    timeframe: str = "1m",
    *,
    data_dir: str | Path | None = None,
    start: int | None = None,
    end: int | None = None,
) -> tuple[Path, ...]:
    """按 manifest 清单解析当前可见的冷库 Parquet 绝对路径列表。"""
    root = default_data_dir(data_dir)
    reader = ColdReader(root)
    spec = resolve_instrument_spec(instrument, root=root, offline=True, reader=reader)
    manifest, _ = reader.read_manifest_snapshot(
        spec,
        timeframe,
        start_ts=start,
        end_ts=end,
    )
    return tuple((root / gen.parquet_path).resolve() for gen in manifest.generations)


def query_duckdb(
    sql: str,
    *,
    tables: Mapping[str, Any] | None = None,
    data_dir: str | Path | None = None,
    params: Sequence[object] | Mapping[str, object] | None = None,
    as_arrow: bool = False,
) -> "DataFrame | pa.Table":
    """在内存 DuckDB 实例上对 Arrow / 结果视图 / 清单 Parquet 执行只读 SQL 查询。"""
    cleaned_sql = _validate_readonly_sql(sql)
    with duckdb.connect(":memory:") as connection:
        if data_dir is not None:
            root = Path(data_dir).expanduser().resolve()
            connection.register("talaria_manifest", _manifest_table(root))
        if tables:
            for name, value in tables.items():
                ident = _validate_identifier(name, "表名")
                connection.register(ident, _to_arrow_relation(value))
        if params is None:
            relation = connection.sql(cleaned_sql)
            return relation.to_arrow_table() if as_arrow else relation.df()
        cursor = connection.execute(cleaned_sql, params)
        return cursor.to_arrow_table() if as_arrow else cursor.df()


def asof_join(
    left: Any,
    right: Any,
    *,
    on: str = "ts_event",
    by: Sequence[str] | str | None = None,
    right_columns: Sequence[str] | None = None,
    right_prefix: str = "right_",
    tolerance_ms: int | None = None,
    as_arrow: bool = False,
) -> "DataFrame | pa.Table":
    """使用 DuckDB 原生 ``ASOF LEFT JOIN`` 对齐两条时序结果（例如跨标的或与衍生品对齐）。"""
    join_col = _validate_identifier(on, "关联时间列")
    by_cols = (
        [_validate_identifier(by, "分组列")]
        if isinstance(by, str)
        else [_validate_identifier(col, "分组列") for col in (by or ())]
    )
    left_rel = _to_arrow_relation(left)
    right_rel = _to_arrow_relation(right)

    with duckdb.connect(":memory:") as connection:
        connection.register("_left_tbl", left_rel)
        connection.register("_right_tbl", right_rel)
        left_names = [row[0] for row in connection.execute("DESCRIBE _left_tbl").fetchall()]
        right_names = [row[0] for row in connection.execute("DESCRIBE _right_tbl").fetchall()]
        if join_col not in left_names or join_col not in right_names:
            raise InvalidRequestError(f"两边表都必须包含时间列 {join_col!r}")
        for col in by_cols:
            if col not in left_names or col not in right_names:
                raise InvalidRequestError(f"两边表都必须包含分组列 {col!r}")

        if right_columns is None:
            selected_right = [name for name in right_names if name not in by_cols]
        else:
            selected_right = [_validate_identifier(col, "右表列") for col in right_columns]

        select_parts = [f"l.\"{col}\"" for col in left_names]
        seen_names = set(left_names)
        for col in selected_right:
            alias = f"{right_prefix}{col}" if right_prefix or col in seen_names else col
            _validate_identifier(alias, "输出列名")
            select_parts.append(f"r.\"{col}\" AS \"{alias}\"")

        predicates = [f"l.\"{col}\" = r.\"{col}\"" for col in by_cols]
        predicates.append(f"l.\"{join_col}\" >= r.\"{join_col}\"")
        if tolerance_ms is not None:
            if tolerance_ms < 0:
                raise InvalidRequestError("tolerance_ms 不得为负数")
            predicates.append(f"(l.\"{join_col}\" - r.\"{join_col}\") <= {int(tolerance_ms)}")

        sql = (
            f"SELECT {', '.join(select_parts)} "
            f"FROM _left_tbl AS l "
            f"ASOF LEFT JOIN _right_tbl AS r ON {' AND '.join(predicates)} "
            f"ORDER BY l.\"{join_col}\""
        )
        relation = connection.sql(sql)
        return relation.to_arrow_table() if as_arrow else relation.df()
