"""Python 自检：核对 `contracts/sizing_vectors/` 全部定仓黄金向量与权威实现逐字符一致。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.cli.sizing_vectors import (
    SizingPreviewCaseInput,
    VECTOR_FILENAMES,
    compute_authoritative_sizing_preview,
    default_vector_dir,
)
from talaria.foundation.paper import MarginMode
from talaria.foundation.types import decimal_to_string


def _norm(val: Decimal | None) -> str | None:
    if val is None:
        return None
    if val == 0:
        return "0"
    text = decimal_to_string(val)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _load_cases() -> list[tuple[str, str, dict, dict]]:
    root = default_vector_dir()
    cases: list[tuple[str, str, dict, dict]] = []
    for filename in VECTOR_FILENAMES:
        path = root / filename
        assert path.is_file(), f"缺少定仓黄金向量文件：{path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data["cases"]:
            cases.append((filename, entry["name"], entry["input"], entry["expected"]))
    return cases


@pytest.mark.parametrize(
    ("suite", "name", "raw_input", "expected"),
    _load_cases(),
    ids=lambda item: item if isinstance(item, str) else "",
)
def test_python_sizing_vectors_self_check(
    suite: str, name: str, raw_input: dict, expected: dict
) -> None:
    case = SizingPreviewCaseInput(
        direction=raw_input["direction"],
        entry_low=Decimal(raw_input["entry_low"]),
        entry_high=Decimal(raw_input["entry_high"]),
        invalidation_stop=Decimal(raw_input["invalidation_stop"]),
        qty_step=Decimal(raw_input["qty_step"]),
        min_qty=Decimal(raw_input["min_qty"]),
        contract_multiplier=Decimal(raw_input["contract_multiplier"]),
        equity=Decimal(raw_input["equity"]),
        r_unit_pct=Decimal(raw_input["r_unit_pct"]),
        max_single_r_pct=Decimal(raw_input["max_single_r_pct"]),
        friction_buffer_pct=Decimal(raw_input["friction_buffer_pct"]),
        margin_mode=MarginMode(raw_input["margin_mode"]),
        leverage=Decimal(raw_input["leverage"]),
        maintenance_rate=Decimal(raw_input["maintenance_rate"]),
        existing_round_risk=Decimal(raw_input["existing_round_risk"]),
        existing_round_stop=(
            None if raw_input["existing_round_stop"] is None else Decimal(raw_input["existing_round_stop"])
        ),
        override_quantity=(
            None if raw_input["override_quantity"] is None else Decimal(raw_input["override_quantity"])
        ),
    )
    resp = compute_authoritative_sizing_preview(case)
    actual = {
        "quantity": _norm(resp.quantity),
        "basis": resp.basis,
        "risk_amount_r": _norm(resp.risk_amount_r),
        "risk_pct_of_equity": _norm(resp.risk_pct_of_equity),
        "est_liq_price": _norm(resp.est_liq_price),
        "est_liq_distance": _norm(resp.est_liq_distance),
        "est_liq_distance_pct": _norm(resp.est_liq_distance_pct),
        "rejected_reason": resp.rejected_reason,
    }
    assert actual == expected, f"Python vector mismatch in {suite}:{name}"
