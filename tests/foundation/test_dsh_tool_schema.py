"""DSH 工具定义必须落在 0.2.2-rc.3 支持的 JSON Schema 子集内。"""

from __future__ import annotations

import json

import pytest

from talaria.foundation.contracts_io import DSH_TOOL_PATH, check_contracts

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


def _plugin_copy() -> dict:
    from talaria.foundation.contracts_io import repo_root

    path = repo_root() / "dsh" / "packages" / "talaria-dsh-plugin" / "get_snapshot.tool.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_plugin_tool_copy_matches_canonical() -> None:
    canonical = json.loads(DSH_TOOL_PATH.read_text(encoding="utf-8"))
    assert _plugin_copy() == canonical


def test_generated_tool_has_no_schema_or_defs_and_zero_drift() -> None:
    assert check_contracts() == []
    tool = json.loads(DSH_TOOL_PATH.read_text(encoding="utf-8"))
    assert tool["name"] == "get_snapshot"
    assert "$schema" not in tool
    assert "$defs" not in tool["inputSchema"]
    _walk(tool["inputSchema"])
    assert tool["inputSchema"]["required"] == ["instrument_id", "timeframes"]
    assert tool["inputSchema"]["additionalProperties"] is False
    assert tool["inputSchema"]["properties"]["timeframes"]["type"] == "array"
    assert "anyOf" not in json.dumps(tool["inputSchema"])
