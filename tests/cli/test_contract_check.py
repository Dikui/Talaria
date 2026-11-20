"""契约生成物必须零漂移，CLI 的 contract-check 退出码为 0。"""

from __future__ import annotations

from pathlib import Path

from talaria.cli.main import main
from talaria.cli.contracts_io import check_contracts


def test_committed_contracts_are_zero_drift(repo_root: Path) -> None:
    assert check_contracts(repo_root) == []


def test_cli_contract_check_exits_zero(repo_root: Path) -> None:
    assert main(["contract-check"]) == 0


def test_dsh_tool_subset_is_fully_inlined(repo_root: Path) -> None:
    import json

    payload = json.loads(
        (repo_root / "contracts/schemas/dsh-tools/get_snapshot.tool.json").read_text(
            encoding="utf-8"
        )
    )
    assert set(payload) == {"name", "description", "inputSchema"}
    assert payload["name"] == "get_snapshot"
    schema = payload["inputSchema"]
    assert schema["type"] == "object"
    assert "$schema" not in schema
    assert "$defs" not in schema
    assert "$ref" not in json.dumps(schema)
    assert schema["properties"]["instrument_id"]["type"] == "string"
    assert schema["properties"]["mode"]["enum"] == ["HISTORICAL_PIT", "OBSERVED_REPLAY"]


def test_generated_typescript_uses_decimal_string_alias(repo_root: Path) -> None:
    generated = (
        repo_root / "dsh/packages/talaria-contracts/src/generated.ts"
    ).read_text(encoding="utf-8")
    assert "export type DecimalString = string;" in generated
    assert "tick_size: DecimalString;" in generated
    assert "open: DecimalString | null;" in generated
    assert "timestamp: number;" in generated
