"""DSH 工具定义必须落在 0.2.2-rc.3 支持的 JSON Schema 子集内。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from talaria.foundation.contracts_io import DSH_TOOL_PATHS, check_contracts, repo_root

pytestmark = pytest.mark.spec

_ALLOWED_KEYS = {
    "additionalProperties",
    "const",
    "default",
    "description",
    "enum",
    "examples",
    "items",
    "oneOf",
    "properties",
    "required",
    "title",
    "type",
}
_FORBIDDEN_KEYS = {"$schema", "$defs", "$ref", "definitions", "allOf", "anyOf", "not", "minItems", "minLength", "minimum", "maximum", "pattern", "format"}


def _walk(node: object, path: str = "$") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                for name, schema in value.items():
                    _walk(schema, f"{path}.properties.{name}")
                continue
            assert key not in _FORBIDDEN_KEYS, f"{path}.{key} 不属于 DSH 工具 Schema 子集"
            assert key in _ALLOWED_KEYS, f"{path}.{key} 未在允许列表内"
            _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, f"{path}[{index}]")


def _plugin_copy(name: str) -> dict:
    path: Path = repo_root() / "dsh" / "packages" / "talaria-dsh-plugin" / f"{name}.tool.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(DSH_TOOL_PATHS))
def test_plugin_tool_copy_matches_canonical(name: str) -> None:
    canonical = json.loads(DSH_TOOL_PATHS[name].read_text(encoding="utf-8"))
    assert _plugin_copy(name) == canonical


def test_generated_tool_has_no_schema_or_defs_and_zero_drift() -> None:
    assert check_contracts() == []
    for name, path in DSH_TOOL_PATHS.items():
        tool = json.loads(path.read_text(encoding="utf-8"))
        assert tool["name"] == name
        assert "$schema" not in tool
        assert "$defs" not in tool["inputSchema"]
        _walk(tool["inputSchema"])
        assert "anyOf" not in json.dumps(tool["inputSchema"])

    snapshot = json.loads(DSH_TOOL_PATHS["get_snapshot"].read_text(encoding="utf-8"))
    assert snapshot["inputSchema"]["required"] == ["instrument_id", "timeframes"]
    assert snapshot["inputSchema"]["additionalProperties"] is False
    assert snapshot["inputSchema"]["properties"]["timeframes"]["type"] == "array"


@pytest.mark.parametrize(
    "name",
    ["get_indicators", "get_key_levels", "compute_operator", "get_chart_view"],
)
def test_read_only_tools_bind_snapshot_and_decimal_strings(name: str) -> None:
    tool = json.loads(DSH_TOOL_PATHS[name].read_text(encoding="utf-8"))
    schema = tool["inputSchema"]
    assert schema["required"][0] == "snapshot_id"
    assert schema["properties"]["snapshot_id"]["type"] == "string"


def test_chart_view_prices_are_strings_for_dsh_subset() -> None:
    tool = json.loads(DSH_TOOL_PATHS["get_chart_view"].read_text(encoding="utf-8"))
    properties = tool["inputSchema"]["properties"]
    assert properties["price_min"]["type"] == "string"
    assert properties["price_max"]["type"] == "string"
