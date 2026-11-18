"""成交量位、锚定 VWAP 与清算密集区黄金向量。

向量锁定：POC/VAH/VAL 价格与强度、锚定 VWAP ±σ 带、零量柱不推进时间、
清算区的 ESTIMATED 证据类别与杠杆参数、无前视截断，以及 OI 不足时的空集原因。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.key_levels import (
    KeyLevelParams,
    compute_key_levels_with_notes,
)
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.regression]

VECTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "operator_vectors"
    / "key_levels_volume.json"
)


def _load() -> dict:
    return json.loads(VECTOR_PATH.read_text(encoding="utf-8"))


def _bar(raw: dict) -> CompactBar:
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


def _point(raw: dict, instrument_id: str) -> DerivativePoint:
    return DerivativePoint(
        instrument_id=instrument_id,
        kind=DerivativeKind(raw["kind"]),
        timeframe=raw.get("timeframe"),
        value=Decimal(raw["value"]),
        ts_event=raw["ts_event"],
        ts_published=raw.get("ts_published"),
        ts_available=raw["ts_available"],
        precision=PrecisionTier(raw.get("precision", "SECOND_VERIFIED")),
        ts_ingested=raw["ts_ingested"],
        ts_committed=raw["ts_committed"],
    )


def _params(raw: dict) -> KeyLevelParams:
    values = dict(raw)
    if isinstance(values.get("leverage_tiers"), list):
        values["leverage_tiers"] = tuple(values["leverage_tiers"])
    return KeyLevelParams(**values)


def _project(level, expected_parameters: dict[str, str]) -> dict:
    return {
        "kind": level.kind.value,
        "category": level.category.value,
        "evidence_class": level.evidence_class.value,
        "price": None if level.price is None else format(level.price, "f"),
        "price_low": None if level.price_low is None else format(level.price_low, "f"),
        "price_high": None
        if level.price_high is None
        else format(level.price_high, "f"),
        "strength": level.strength,
        "status": level.status.value,
        "ts_available": level.ts_available,
        "formed_ts": level.formed_ts,
        "parameters": {
            key: level.parameters[key] for key in expected_parameters
        },
    }


def test_key_level_volume_golden_vectors() -> None:
    payload = _load()
    assert payload["operator"] == "key_level_volume"
    assert payload["method_version"] == "key-levels@2"
    assert payload["cases"]

    for case in payload["cases"]:
        bars = [_bar(raw) for raw in case["bars"]]
        points = [
            _point(raw, payload["instrument_id"])
            for raw in case["derivative_points"]
        ]
        computation = compute_key_levels_with_notes(
            instrument_id=payload["instrument_id"],
            timeframe=payload["timeframe"],
            bars=bars,
            cutoff_ts=case["cutoff_ts"],
            params=_params(case["params"]),
            derivative_points=points,
        )
        for level in computation.levels:
            assert level.ts_available <= case["cutoff_ts"], case["name"]
            assert level.formed_ts <= level.ts_available, case["name"]

        included = set(case["include_kinds"])
        projected = [
            _project(level, case["expected"]["levels"][0]["parameters"])
            if case["expected"]["levels"]
            else None
            for level in computation.levels
            if level.kind.value in included
        ]
        if not case["expected"]["levels"]:
            projected = []
        assert projected == case["expected"]["levels"], case["name"]
        assert computation.notes == case["expected"].get("notes", []), case["name"]


def test_key_level_volume_vectors_have_stable_identity() -> None:
    payload = _load()
    case = next(
        item
        for item in payload["cases"]
        if item["name"] == "liquidation_estimate_from_oi"
    )
    bars = [_bar(raw) for raw in case["bars"]]
    points = [
            _point(raw, payload["instrument_id"])
            for raw in case["derivative_points"]
        ]
    first = compute_key_levels_with_notes(
        instrument_id=payload["instrument_id"],
        timeframe=payload["timeframe"],
        bars=bars,
        cutoff_ts=case["cutoff_ts"],
        params=_params(case["params"]),
        derivative_points=points,
    )
    second = compute_key_levels_with_notes(
        instrument_id=payload["instrument_id"],
        timeframe=payload["timeframe"],
        bars=list(reversed(bars)),
        cutoff_ts=case["cutoff_ts"],
        params=_params(case["params"]),
        derivative_points=list(reversed(points)),
    )
    assert [level.level_id for level in first.levels] == [
        level.level_id for level in second.levels
    ]
    liquidation = [
        level for level in first.levels if level.kind.value == "LIQUIDATION_ZONE"
    ]
    assert liquidation
    assert all(
        level.evidence_class.value == "ESTIMATED" for level in liquidation
    )
