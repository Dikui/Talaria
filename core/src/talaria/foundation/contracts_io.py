"""契约生成物：JSON Schema、TypeScript 类型与 DSH 工具子集。

生成逻辑全部用 Python 实现且必须确定：UTF-8、两个空格缩进、键排序、末尾换行。
``uv run talaria contract-check --write`` 是唯一正确的更新方式。
"""

from __future__ import annotations

import copy
import json
import re
import types
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Iterable, Literal, Union, get_args, get_origin

from pydantic import BaseModel

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.drawings import (
    ChartView,
    ChartViewWindow,
    DrawingClearResponse,
    DrawingCreate,
    DrawingKind,
    DrawingLineStyle,
    DrawingListResponse,
    DrawingPatch,
    DrawingPoint,
    DrawingRecord,
    DrawingStyle,
    DrawingVersionList,
)
from talaria.foundation.derivatives import (
    DerivativeBundle,
    DerivativeKind,
    DerivativePoint,
    DerivativeSeries,
    DerivativeSummary,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthReport,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.indicators import (
    IndicatorBundle,
    IndicatorName,
    IndicatorParams,
    IndicatorPoint,
    IndicatorRequest,
    IndicatorSeries,
    IndicatorSummary,
    OperatorRequest,
)
from talaria.foundation.market import InstrumentSpec
from talaria.foundation.snapshot import (
    ChartPage,
    ErrorResponse,
    SnapshotMeta,
    SnapshotMode,
    SnapshotRequest,
)
from talaria.foundation.types import DECIMAL_PATTERN, PrecisionTier, TemporalStamp
from talaria.foundation.watchlist import (
    DegradationReason,
    QuoteSource,
    Watchlist,
    WatchlistGroup,
    WatchlistGroupCreate,
    WatchlistGroupKind,
    WatchlistGroupUpdate,
    WatchlistItem,
    WatchlistItemCreate,
    WatchlistItemOrder,
    WatchlistOrder,
)

#: 显式的模型顺序决定生成文件顺序与 TS 输出顺序，避免依赖导入副作用。
SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "temporal_stamp": TemporalStamp,
    "instrument_spec": InstrumentSpec,
    "compact_bar": CompactBar,
    "kline_event": KlineEvent,
    "order_book_event": OrderBookEvent,
    "derivative_tick_event": DerivativeTickEvent,
    "derivative_point": DerivativePoint,
    "derivative_series": DerivativeSeries,
    "derivative_summary": DerivativeSummary,
    "derivative_bundle": DerivativeBundle,
    "feed_health_event": FeedHealthEvent,
    "feed_health_report": FeedHealthReport,
    "snapshot_request": SnapshotRequest,
    "snapshot_meta": SnapshotMeta,
    "chart_page": ChartPage,
    "error_response": ErrorResponse,
    "indicator_params": IndicatorParams,
    "indicator_summary": IndicatorSummary,
    "indicator_point": IndicatorPoint,
    "indicator_series": IndicatorSeries,
    "indicator_request": IndicatorRequest,
    "operator_request": OperatorRequest,
    "indicator_bundle": IndicatorBundle,
    "drawing_point": DrawingPoint,
    "drawing_style": DrawingStyle,
    "drawing_create": DrawingCreate,
    "drawing_patch": DrawingPatch,
    "drawing_record": DrawingRecord,
    "drawing_list_response": DrawingListResponse,
    "drawing_version_list": DrawingVersionList,
    "drawing_clear_response": DrawingClearResponse,
    "chart_view_window": ChartViewWindow,
    "chart_view": ChartView,
    "watchlist_group_create": WatchlistGroupCreate,
    "watchlist_group_update": WatchlistGroupUpdate,
    "watchlist_item_create": WatchlistItemCreate,
    "watchlist_order": WatchlistOrder,
    "watchlist_item_order": WatchlistItemOrder,
    "watchlist_item": WatchlistItem,
    "watchlist_group": WatchlistGroup,
    "watchlist": Watchlist,
}

#: 枚举的固定输出顺序。
ENUM_TYPES: tuple[type[StrEnum], ...] = (
    PrecisionTier,
    BarStatus,
    FeedHealthStatus,
    SnapshotMode,
    IndicatorName,
    DerivativeKind,
    DrawingKind,
    DrawingLineStyle,
    WatchlistGroupKind,
    QuoteSource,
    DegradationReason,
)

#: 所有必须是十进制字符串的字段名（用于独立于生成逻辑的检查）。
DECIMAL_FIELDS: frozenset[str] = frozenset(
    {
        "open",
        "high",
        "low",
        "close",
        "volume",
        "turnover",
        "tick_size",
        "qty_step",
        "min_qty",
        "bid_price",
        "bid_size",
        "ask_price",
        "ask_size",
        "std_dev",
        "price",
        "price_min",
        "price_max",
        "latest_price",
        "change_pct",
        "value",
        "mark_price",
        "funding_rate",
        "open_interest",
    }
)

SCHEMA_DIR = Path("contracts/schemas")
DSH_TOOL_DIR = SCHEMA_DIR / "dsh-tools"
TS_PATH = Path("dsh/packages/talaria-contracts/src/generated.ts")
DSH_TOOL_PATH = DSH_TOOL_DIR / "get_snapshot.tool.json"

_HEADER = "// 本文件由 `uv run talaria contract-check --write` 生成，请勿手改。\n\n"


def repo_root() -> Path:
    """定位仓库根；contract-check 必须从真实工作树运行。"""
    candidate = Path(__file__).resolve().parents[4]
    if (candidate / "pyproject.toml").is_file() and (candidate / "core").is_dir():
        return candidate
    for parent in Path(__file__).resolve().parents:
        pyproject = parent / "pyproject.toml"
        if pyproject.is_file() and "tool.uv.workspace" in pyproject.read_text(encoding="utf-8"):
            return parent
    raise RuntimeError("找不到 Talaria 仓库根目录")


def _dump_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _contains_pattern(node: Any, pattern: str) -> bool:
    if isinstance(node, dict):
        if node.get("pattern") == pattern:
            return True
        return any(_contains_pattern(value, pattern) for value in node.values())
    if isinstance(node, list):
        return any(_contains_pattern(value, pattern) for value in node)
    return False


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def _is_decimal_property(prop: dict[str, Any]) -> bool:
    return _contains_pattern(prop, DECIMAL_PATTERN)


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 ``$defs`` 完全展开，DSH 工具子集不允许任何 ``$ref``/``$defs``。"""
    root = copy.deepcopy(schema)
    definitions = root.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = str(node["$ref"]).rsplit("/", 1)[-1]
                resolved = resolve(copy.deepcopy(definitions[name]))
                siblings = {key: resolve(value) for key, value in node.items() if key != "$ref"}
                resolved.update(siblings)
                return resolved
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    resolved = resolve(root)
    resolved.pop("$schema", None)
    resolved.pop("$defs", None)
    return resolved


def _ts_type(annotation: Any, prop: dict[str, Any]) -> str:
    if get_origin(annotation) is Annotated:
        base, *metadata = get_args(annotation)
        if base is Decimal:
            return "DecimalString"
        return _ts_type(base, prop)
    if get_origin(annotation) is Union or isinstance(annotation, types.UnionType):
        args = list(get_args(annotation))
        non_none = [item for item in args if item is not type(None)]
        if len(non_none) == 1:
            inner = _ts_type(non_none[0], prop)
            has_null = len(non_none) != len(args) or _contains_null(prop)
            return f"{inner} | null" if has_null else inner
        return " | ".join(_ts_type(item, prop) for item in non_none)

    if get_origin(annotation) is Literal:
        return " | ".join(
            json.dumps(member, ensure_ascii=False) for member in get_args(annotation)
        )
    if get_origin(annotation) is list:
        (item_annotation,) = get_args(annotation)
        item_prop = prop.get("items", {}) if isinstance(prop, dict) else {}
        return f"{_ts_type(item_annotation, item_prop)}[]"
    if get_origin(annotation) is dict:
        key_annotation, value_annotation = get_args(annotation)
        return f"Record<{_ts_type(key_annotation, {})}, {_ts_type(value_annotation, {})}>"
    if _is_decimal_property(prop):
        return "DecimalString"
    if annotation is str:
        return "string"
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    if annotation is Decimal:
        return "DecimalString"
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return annotation.__name__
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation.__name__
    raise TypeError(f"无法生成 TypeScript 类型：{annotation!r}")


def _contains_null(node: Any) -> bool:
    if isinstance(node, dict):
        if node.get("type") == "null":
            return True
        return any(_contains_null(value) for value in node.values())
    if isinstance(node, list):
        return any(_contains_null(value) for value in node)
    return False


def _render_ts() -> str:
    lines = [_HEADER]
    lines.append("export type DecimalString = string;\n\n")
    for enum_type in ENUM_TYPES:
        members = " | ".join(json.dumps(member.value, ensure_ascii=False) for member in enum_type)
        lines.append(f"export type {enum_type.__name__} = {members};\n\n")
    for model in SCHEMA_MODELS.values():
        properties = _schema_for(model).get("properties", {})
        lines.append(f"export interface {model.__name__} {{\n")
        for name, field in model.model_fields.items():
            prop = properties.get(name, {})
            ts_type = _ts_type(field.annotation, prop)
            optional = "" if field.is_required() else "?"
            lines.append(f"  {name}{optional}: {ts_type};\n")
        lines.append("}\n")
    return "".join(lines).rstrip() + "\n"


def render_contracts(root: Path | None = None) -> dict[Path, str]:
    """返回 ``相对路径 -> 期望内容``；不改动磁盘。"""
    root = root or repo_root()
    artifacts: dict[Path, str] = {}
    for filename, model in SCHEMA_MODELS.items():
        artifacts[SCHEMA_DIR / f"{filename}.schema.json"] = _dump_json(_schema_for(model))

    tool_schema = _inline_refs(_schema_for(SnapshotRequest))
    tool_payload = {
        "name": "get_snapshot",
        "description": "获取指定标的在给定截止时点的确定性历史 K 线快照。",
        "inputSchema": tool_schema,
    }
    artifacts[DSH_TOOL_PATH] = _dump_json(tool_payload)
    artifacts[TS_PATH] = _render_ts()
    return artifacts


def write_contracts(root: Path | None = None) -> list[Path]:
    root = root or repo_root()
    written: list[Path] = []
    for relative, content in sorted(render_contracts(root).items()):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(relative)
    return written


def _iter_dicts(node: Any) -> Iterable[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_dicts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item)


def _find_decimal_properties(node: Any, path: str = "$") -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for name, prop in value.items():
                    if name in DECIMAL_FIELDS and isinstance(prop, dict):
                        found.append((f"{path}.{name}", prop))
                    found.extend(_find_decimal_properties(prop, f"{path}.{name}"))
            else:
                found.extend(_find_decimal_properties(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(_find_decimal_properties(item, f"{path}[{index}]"))
    return found


def _diff(expected: str, actual: str, label: str) -> str:
    import difflib

    diff = difflib.unified_diff(
        expected.splitlines(keepends=True),
        actual.splitlines(keepends=True),
        fromfile=f"expected/{label}",
        tofile=f"actual/{label}",
        n=2,
    )
    return "".join(diff)


def check_contracts(root: Path | None = None) -> list[str]:
    """返回问题列表；空列表表示零漂移。"""
    root = root or repo_root()
    problems: list[str] = []
    expected = render_contracts(root)

    for relative, content in sorted(expected.items()):
        target = root / relative
        if not target.is_file():
            problems.append(f"缺少生成物：{relative}（运行 `uv run talaria contract-check --write`）")
            continue
        actual = target.read_text(encoding="utf-8")
        if actual != content:
            problems.append(f"生成物漂移：{relative}\n{_diff(content, actual, relative.as_posix())}")

    # 未登记的旧生成物也算漂移，避免死文件误导读者。
    actual_schemas = {
        path.name for path in (root / SCHEMA_DIR).glob("*.schema.json") if path.is_file()
    }
    expected_schemas = {path.name for path in expected if path.parent == SCHEMA_DIR}
    for extra in sorted(actual_schemas - expected_schemas):
        problems.append(f"未登记的 schema：{SCHEMA_DIR / extra}")

    ts_text = (root / TS_PATH).read_text(encoding="utf-8") if (root / TS_PATH).is_file() else ""
    if "export type DecimalString = string;" not in ts_text:
        problems.append("generated.ts 缺少 `export type DecimalString = string;`")
    for model in SCHEMA_MODELS.values():
        properties = _schema_for(model).get("properties", {})
        for name, prop in properties.items():
            if name not in DECIMAL_FIELDS or not _is_decimal_property(prop):
                continue
            if not re.search(rf"^\s*{re.escape(name)}\??: DecimalString", ts_text, re.MULTILINE):
                problems.append(f"generated.ts 的 {model.__name__}.{name} 未使用 DecimalString")

    for filename in SCHEMA_MODELS:
        schema_path = root / SCHEMA_DIR / f"{filename}.schema.json"
        if not schema_path.is_file():
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for location, prop in _find_decimal_properties(schema):
            if not _contains_pattern(prop, DECIMAL_PATTERN):
                problems.append(f"{schema_path.name} 的 {location} 不是十进制字符串 schema")
            if any(item.get("type") in {"number", "integer"} for item in _iter_dicts(prop)):
                problems.append(f"{schema_path.name} 的 {location} 含 number/integer 分支")

    tool_path = root / DSH_TOOL_PATH
    if tool_path.is_file():
        tool = json.loads(tool_path.read_text(encoding="utf-8"))
        if tool.get("name") != "get_snapshot":
            problems.append("DSH 工具子集 name 必须是 get_snapshot")
        for node in _iter_dicts(tool):
            if "$schema" in node or "$defs" in node or "$ref" in node:
                problems.append("DSH 工具子集不允许 $schema/$defs/$ref")
                break
        input_schema = tool.get("inputSchema", {})
        if input_schema.get("type") != "object":
            problems.append("DSH 工具子集的 inputSchema 必须是内联 object")
        for location, prop in _find_decimal_properties(input_schema):
            if not _contains_pattern(prop, DECIMAL_PATTERN):
                problems.append(f"DSH 工具子集的 {location} 不是十进制字符串")
    return problems
