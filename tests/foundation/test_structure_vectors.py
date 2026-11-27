"""统一市场结构跨语言黄金向量回归测试（Python 权威输出 ↔ Rust talaria-operators）。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.structures import StructureParams, compute_structure_set
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.regression]

VECTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "operator_vectors"
    / "structures.json"
)


def _bar(raw: dict) -> CompactBar:
    if raw["status"] == "FEED_GAP":
        return CompactBar(
            timestamp=raw["timestamp"],
            open=None,
            high=None,
            low=None,
            close=None,
            volume=None,
            turnover=None,
            status=BarStatus.FEED_GAP,
            ts_available=raw["ts_available"],
            precision=PrecisionTier(raw["precision"]),
        )
    return CompactBar(
        timestamp=raw["timestamp"],
        open=Decimal(raw["open"]),
        high=Decimal(raw["high"]),
        low=Decimal(raw["low"]),
        close=Decimal(raw["close"]),
        volume=Decimal(raw["volume"]),
        turnover=Decimal(raw["turnover"]),
        status=BarStatus(raw["status"]),
        ts_available=raw["ts_available"],
        precision=PrecisionTier(raw["precision"]),
    )


def _project_feature(feat) -> dict:
    return json.loads(feat.model_dump_json())


def test_structure_golden_vectors() -> None:
    payload = json.loads(VECTOR_PATH.read_text(encoding="utf-8"))
    assert payload["operator"] == "structures"
    assert payload["method_version"] == "structures@1"
    assert payload["cases"]

    for case in payload["cases"]:
        bars = [_bar(raw) for raw in case["bars"]]
        raw_params = dict(case.get("params", {}))
        if "round_step" in raw_params and raw_params["round_step"] is not None:
            raw_params["round_step"] = Decimal(raw_params["round_step"])
        if "trendline_spans" in raw_params:
            raw_params["trendline_spans"] = tuple(raw_params["trendline_spans"])
        params = StructureParams(**raw_params)
        detector_ids = tuple(case["detector_ids"])
        result = compute_structure_set(
            instrument_id=payload["instrument_id"],
            timeframe=payload["timeframe"],
            bars=bars,
            cutoff_ts=case["cutoff_ts"],
            snapshot_id="vec_snap",
            params=params,
            detector_ids=detector_ids,
        )
        actual = [_project_feature(f) for f in result.structures]
        assert actual == case["expected"], f"case {case['name']} mismatch"
