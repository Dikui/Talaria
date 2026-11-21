"""黄金向量回归：逐值锁定前导空值、NO_TRADES、FEED_GAP 重预热与增量一致。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.bars import CompactBar
from talaria.foundation.operators import (
    DerivativeSample,
    compute_series,
    spec_from_params,
    update_series,
)

pytestmark = [pytest.mark.small, pytest.mark.regression]

VECTOR_DIR = Path(__file__).resolve().parents[3] / "contracts" / "operator_vectors"


def _is_operator_vector(path: Path) -> bool:
    """关键位等更高层引擎的向量自带 cases，不属于单算子 params 契约。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return "operator" in payload and "params" in payload


VECTOR_PATHS = sorted(path for path in VECTOR_DIR.glob("*.json") if _is_operator_vector(path))


VECTOR_EPOCH_MS = 1_700_000_000_000


def _samples(vector: dict) -> list[DerivativeSample] | None:
    raw = vector.get("derivatives")
    if raw is None:
        return None
    samples: list[DerivativeSample] = []
    for item in raw:
        funding_offset = item.get("funding_offset")
        samples.append(
            DerivativeSample(
                funding_rate=(
                    None
                    if item.get("funding_rate") is None
                    else Decimal(item["funding_rate"])
                ),
                funding_ts=(
                    None
                    if funding_offset is None
                    else VECTOR_EPOCH_MS + funding_offset * 60_000
                ),
                open_interest=(
                    None
                    if item.get("open_interest") is None
                    else Decimal(item["open_interest"])
                ),
                oi_ts=None,
            )
        )
    return samples


def _bars(raw: list[dict]) -> list[CompactBar]:
    return [
        CompactBar(
            timestamp=1_700_000_000_000 + item["offset"] * 60_000,
            open=Decimal(item["open"]) if item["open"] is not None else None,
            high=Decimal(item["high"]) if item["high"] is not None else None,
            low=Decimal(item["low"]) if item["low"] is not None else None,
            close=Decimal(item["close"]) if item["close"] is not None else None,
            volume=Decimal(item["volume"]) if item["volume"] is not None else None,
            turnover=Decimal(item["turnover"]) if item["turnover"] is not None else None,
            status=item["status"],
            ts_available=1_700_000_000_000 + (item["offset"] + 1) * 60_000,
            precision="SECOND_VERIFIED",
        )
        for item in raw
    ]


@pytest.mark.parametrize("path", VECTOR_PATHS, ids=lambda path: path.stem)
def test_vector_values_match_exactly(path: Path) -> None:
    vector = json.loads(path.read_text(encoding="utf-8"))
    spec = spec_from_params(vector["operator"], vector["params"])
    bars = _bars(vector["bars"])
    samples = _samples(vector)
    points = compute_series(bars, spec, derivatives=samples).points
    expected = vector["expected"]

    assert len(points) == len(expected) == len(bars)
    for index, (point, want) in enumerate(zip(points, expected, strict=True)):
        for channel, raw_value in want.items():
            want_value = None if raw_value is None else Decimal(raw_value)
            assert point[channel] == want_value, (
                f"{vector['operator']}[{index}].{channel}={point[channel]!r}，"
                f"期望 {want_value!r}"
            )

    leading = next(
        (index for index, point in enumerate(points) if any(v is not None for v in point.values())),
        len(points),
    )
    assert leading == vector["warmup_bars"]


@pytest.mark.parametrize("path", VECTOR_PATHS, ids=lambda path: path.stem)
def test_batch_and_incremental_match_for_every_split(path: Path) -> None:
    vector = json.loads(path.read_text(encoding="utf-8"))
    spec = spec_from_params(vector["operator"], vector["params"])
    bars = _bars(vector["bars"])
    samples = _samples(vector)
    batch = compute_series(bars, spec, derivatives=samples)

    for split in range(len(bars) + 1):
        state = compute_series(
            bars[:split],
            spec,
            derivatives=None if samples is None else samples[:split],
        )
        state = update_series(
            state,
            bars[split:],
            spec,
            derivatives=None if samples is None else samples[split:],
        )
        assert state.points == batch.points


@pytest.mark.parametrize("path", VECTOR_PATHS, ids=lambda path: path.stem)
def test_feed_gap_null_then_full_rewarmup(path: Path) -> None:
    vector = json.loads(path.read_text(encoding="utf-8"))
    spec = spec_from_params(vector["operator"], vector["params"])
    bars = _bars(vector["bars"])
    samples = _samples(vector)
    points = compute_series(bars, spec, derivatives=samples).points
    channels = tuple(vector["expected"][0])

    gap_index = next(
        index for index, item in enumerate(vector["bars"]) if item["status"] == "FEED_GAP"
    )
    assert all(value is None for value in points[gap_index].values())
    warmup = vector.get("post_gap_warmup", vector["warmup_bars"])
    if warmup:
        rewarm = points[gap_index + 1 : gap_index + 1 + warmup]
        assert len(rewarm) == warmup
        assert all(all(value is None for value in point.values()) for point in rewarm)
    if gap_index + 1 + warmup < len(points):
        assert any(
            value is not None for value in points[gap_index + 1 + warmup].values()
        )
