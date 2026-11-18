"""关键位黄金向量：锁定确认延迟、状态迁移、区间聚合与稳定身份。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels import (
    KeyLevelParams,
    KeyLevelStatus,
    SessionProfile,
    compute_key_levels,
)
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.regression]

VECTOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "operator_vectors"
    / "key_levels.json"
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


def _project(level) -> dict:
    return {
        "level_id": level.level_id,
        "kind": level.kind.value,
        "category": level.category.value,
        "price": None if level.price is None else format(level.price, "f"),
        "price_low": None if level.price_low is None else format(level.price_low, "f"),
        "price_high": None
        if level.price_high is None
        else format(level.price_high, "f"),
        "strength": level.strength,
        "status": level.status.value,
        "ts_available": level.ts_available,
        "formed_ts": level.formed_ts,
        "touch_count": level.touch_count,
    }


def test_key_level_golden_vectors() -> None:
    payload = _load()
    assert payload["operator"] == "key_levels"
    assert payload["method_version"] == "key-levels@2"
    assert payload["cases"]

    for case in payload["cases"]:
        bars = [_bar(raw) for raw in case["bars"]]
        params = KeyLevelParams(**case.get("params", {}))
        levels = compute_key_levels(
            instrument_id=payload["instrument_id"],
            timeframe=payload["timeframe"],
            bars=bars,
            cutoff_ts=case["cutoff_ts"],
            session_profile=SessionProfile(),
            params=params,
        )
        structure = [
            _project(level)
            for level in levels
            if level.kind.value in {"SWING_HIGH", "SWING_LOW"}
        ]
        assert structure == case["expected"], case["name"]
        for level in levels:
            assert level.ts_available <= case["cutoff_ts"], case["name"]


def test_key_level_vector_status_chain_shares_identity() -> None:
    payload = _load()
    by_name = {case["name"]: case for case in payload["cases"]}
    active = by_name["support_active_broken_flipped"]["expected"][0]
    broken = by_name["support_broken"]["expected"][0]
    flipped = by_name["support_flipped"]["expected"][0]
    assert active["level_id"] == broken["level_id"] == flipped["level_id"]
    assert active["status"] == KeyLevelStatus.ACTIVE.value
    assert broken["status"] == KeyLevelStatus.BROKEN.value
    assert flipped["status"] == KeyLevelStatus.FLIPPED.value


def test_key_level_vector_zone_keeps_anchor_id() -> None:
    payload = _load()
    zone = next(
        case for case in payload["cases"] if case["name"] == "zone_cluster"
    )["expected"][0]
    assert zone["price"] is None
    assert zone["price_low"] == "120.2"
    assert zone["price_high"] == "120.6"
