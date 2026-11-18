"""关键位离线评估的行为契约（任务单 12）。

覆盖：无前视、固定种子可复现、随机基线数量、全类别枚举、ATR 预热跳过、
ATR 倍数阈值、空窗口以及时间解析。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from talaria.evaluation.key_levels import (
    KeyLevelEvaluationConfig,
    evaluate_key_levels,
    parse_time_ms,
    registered_level_kinds,
)
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.key_levels import (
    KEY_LEVEL_METHOD_VERSION,
    KIND_CATEGORY,
    KeyLevel,
    KeyLevelKind,
    KeyLevelStatus,
)
from talaria.foundation.types import PrecisionTier

pytestmark = [pytest.mark.small, pytest.mark.spec]

MINUTE = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % MINUTE)
INSTRUMENT = "BTC-USDT-PERP"


def _bar(
    index: int,
    open_: str,
    high: str,
    low: str,
    close: str,
    *,
    status: BarStatus = BarStatus.NORMAL,
    volume: str = "10",
) -> CompactBar:
    timestamp = T0 + index * MINUTE
    return CompactBar(
        timestamp=timestamp,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(volume) * Decimal(close),
        status=status,
        ts_available=timestamp + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _market_bars(count: int = 80) -> list[CompactBar]:
    """确定性锯齿行情：每 16 根形成一个摆动，供引擎识别结构位。"""
    bars: list[CompactBar] = []
    for i in range(count):
        center = Decimal("100") + Decimal((i % 16) - 8) * Decimal("0.5")
        open_ = center + Decimal("0.4")
        close = center - Decimal("0.4")
        high = max(open_, close) + Decimal("1.5")
        low = min(open_, close) - Decimal("1.5")
        bars.append(_bar(i, str(open_), str(high), str(low), str(close)))
    return bars


def _config(
    bars: list[CompactBar],
    *,
    baseline_samples: int = 50,
    horizon_bars: int = 10,
    atr_multiple: float = 1.0,
    seed: int = 7,
    start: int = 20,
    end: int = 59,
) -> KeyLevelEvaluationConfig:
    return KeyLevelEvaluationConfig(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        from_ts=bars[start].timestamp,
        to_ts=bars[end].timestamp,
        atr_multiple=atr_multiple,
        horizon_bars=horizon_bars,
        baseline_samples=baseline_samples,
        seed=seed,
    )


def test_parse_time_accepts_epoch_milliseconds_and_iso_offsets() -> None:
    assert parse_time_ms("1793491200000") == 1793491200000
    assert parse_time_ms(1793491200000) == 1793491200000
    assert parse_time_ms("2026-11-01T00:00:00Z") == 1793491200000
    assert parse_time_ms("2026-11-01T08:00:00+08:00") == 1793491200000
    naive = datetime(2026, 11, 1, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert parse_time_ms("2026-11-01 00:00:00") == int(naive.timestamp() * 1000)
    with pytest.raises(ValueError):
        parse_time_ms("not-a-time")


def test_config_rejects_inverted_window_and_negative_parameters() -> None:
    bars = _market_bars()
    valid = _config(bars)
    with pytest.raises(ValueError):
        replace(valid, to_ts=valid.from_ts - 1)
    with pytest.raises(ValueError):
        replace(valid, atr_multiple=0)
    with pytest.raises(ValueError):
        replace(valid, horizon_bars=0)
    with pytest.raises(ValueError):
        replace(valid, baseline_samples=-1)


def test_window_without_cutoff_returns_empty_samples_for_every_registered_kind() -> None:
    bars = _market_bars()
    config = replace(
        _config(bars),
        from_ts=bars[-1].timestamp + MINUTE,
        to_ts=bars[-1].timestamp + 10 * MINUTE,
    )

    payload = evaluate_key_levels(bars, config).as_dict()

    assert [row["kind"] for row in payload["results"]] == list(registered_level_kinds())
    assert payload["skipped"] == 0
    for row in payload["results"]:
        assert row["sample_count"] == 0
        assert row["reaction_rate"] is None
        assert row["mean_reaction_atr"] is None
        assert row["baseline_sample_count"] == 0
        assert row["baseline_reaction_rate"] is None
        assert row["baseline_mean_reaction_atr"] is None


def test_evaluation_never_passes_bars_beyond_the_cutoff(monkeypatch) -> None:
    import talaria.evaluation.key_levels as module

    bars = _market_bars(70)
    calls: list[tuple[int, int]] = []
    real = module.compute_key_levels

    def spy(**kwargs):
        cutoff = kwargs["cutoff_ts"]
        latest = max(bar.ts_available for bar in kwargs["bars"])
        calls.append((cutoff, latest))
        assert latest <= cutoff
        return real(**kwargs)

    monkeypatch.setattr(module, "compute_key_levels", spy)
    evaluate_key_levels(bars, _config(bars, baseline_samples=5))

    assert calls
    assert all(latest <= cutoff for cutoff, latest in calls)


def test_future_bar_forming_a_later_gap_does_not_change_past_results() -> None:
    bars = _market_bars(70)
    config = _config(bars, baseline_samples=20)

    baseline = evaluate_key_levels(bars, config).as_dict()
    gap_bar = _bar(70, "200", "201", "199", "200")
    with_future_gap = evaluate_key_levels([*bars, gap_bar], config).as_dict()

    assert with_future_gap["results"] == baseline["results"]
    assert with_future_gap["skipped"] == baseline["skipped"]


def test_real_samples_only_count_touches_inside_the_evaluation_window(
    monkeypatch,
) -> None:
    import talaria.evaluation.key_levels as module

    level = KeyLevel(
        level_id="kl_window_probe",
        instrument_id=INSTRUMENT,
        kind=KeyLevelKind.SWING_HIGH,
        category=KIND_CATEGORY[KeyLevelKind.SWING_HIGH],
        timeframe="1m",
        price=Decimal("100"),
        strength=0.5,
        status=KeyLevelStatus.ACTIVE,
        ts_available=T0,
        formed_ts=T0,
        method_version=KEY_LEVEL_METHOD_VERSION,
        touch_count=1,
        volume_weight=Decimal("0"),
    )
    bars = [_bar(i, "100", "101", "99", "100") for i in range(20)]
    bars += [_bar(i, "110", "111", "109", "110") for i in range(20, 40)]
    config = KeyLevelEvaluationConfig(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        from_ts=bars[20].timestamp,
        to_ts=bars[39].timestamp,
        horizon_bars=2,
        baseline_samples=0,
    )
    monkeypatch.setattr(module, "compute_key_levels", lambda **_: [level])

    report = evaluate_key_levels(bars, config)
    row = next(row for row in report.results if row.kind == "SWING_HIGH")

    assert row.sample_count == 0
    assert row.skipped_count == 0


def test_fixed_seed_reproduces_identical_report() -> None:
    bars = _market_bars()
    config = _config(bars, baseline_samples=30, seed=42)

    first = evaluate_key_levels(bars, config)
    second = evaluate_key_levels(bars, config)

    assert first.as_dict() == second.as_dict()
    assert first.as_dict()["seed"] == 42
    json.dumps(first.as_dict())


def test_random_baseline_uses_requested_sample_count() -> None:
    bars = _market_bars()
    report = evaluate_key_levels(bars, _config(bars, baseline_samples=50))

    rows = [row for row in report.as_dict()["results"] if row["sample_count"] > 0]
    assert rows
    for row in rows:
        assert row["baseline_sample_count"] == 50
        assert row["baseline_reaction_rate"] is not None
        assert 0.0 <= row["baseline_reaction_rate"] <= 1.0
        assert row["baseline_mean_reaction_atr"] >= 0.0


def test_atr_multiple_scales_the_reaction_threshold() -> None:
    bars = _market_bars()
    loose = evaluate_key_levels(
        bars, _config(bars, atr_multiple=0.01, baseline_samples=0)
    )
    strict = evaluate_key_levels(
        bars, _config(bars, atr_multiple=100.0, baseline_samples=0)
    )

    loose_rows = [row for row in loose.as_dict()["results"] if row["sample_count"] > 0]
    strict_rows = [row for row in strict.as_dict()["results"] if row["sample_count"] > 0]
    assert loose_rows
    assert strict_rows
    assert all(row["mean_reaction_atr"] > 0.0 for row in loose_rows)
    assert all(row["reaction_rate"] >= 0.9 for row in loose_rows)
    assert all(row["reaction_rate"] == 0.0 for row in strict_rows)


def test_short_history_skips_samples_until_atr_is_available() -> None:
    bars = [
        _bar(i, "100", "101", "99", "100", status=BarStatus.NO_TRADES)
        for i in range(14)
    ]
    config = KeyLevelEvaluationConfig(
        instrument_id=INSTRUMENT,
        timeframe="1m",
        from_ts=bars[0].timestamp,
        to_ts=bars[9].timestamp,
        horizon_bars=2,
        baseline_samples=5,
        seed=1,
    )

    report = evaluate_key_levels(bars, config)

    assert report.skipped > 0
    assert report.as_dict()["skipped"] == report.skipped
    assert report.as_dict()["baseline_skipped"] == report.baseline_skipped
    assert all(row.sample_count == 0 for row in report.results)


def test_registry_kind_without_hardcoded_branch_is_evaluated(monkeypatch) -> None:
    import talaria.evaluation.key_levels as module

    bars = _market_bars()
    monkeypatch.setattr(
        module, "registered_level_kinds", lambda: ("SWING_HIGH", "VOLUME_PROFILE_POC")
    )

    report = evaluate_key_levels(bars, _config(bars, baseline_samples=5))

    rows = report.as_dict()["results"]
    # 注册表类别优先输出；引擎实际发出但注册表未列出的类别追加在后，不丢数据。
    assert [row["kind"] for row in rows[:2]] == [
        "SWING_HIGH",
        "VOLUME_PROFILE_POC",
    ]
    unregistered = rows[1]
    assert unregistered["sample_count"] == 0
    assert unregistered["reaction_rate"] is None
    assert unregistered["baseline_sample_count"] == 5


def test_report_has_machine_readable_parameter_fields() -> None:
    bars = _market_bars()
    payload = evaluate_key_levels(bars, _config(bars, seed=9)).as_dict()

    for key in (
        "instrument_id",
        "timeframe",
        "from_ts",
        "to_ts",
        "atr_period",
        "atr_multiple",
        "horizon_bars",
        "baseline_samples",
        "seed",
        "method_version",
        "reaction_definition",
        "total_sample_count",
        "skipped",
        "results",
    ):
        assert key in payload
    assert payload["atr_period"] == 14
    assert payload["seed"] == 9
    for key in (
        "kind",
        "sample_count",
        "reaction_rate",
        "mean_reaction_atr",
        "skipped_count",
        "baseline_sample_count",
        "baseline_reaction_rate",
        "baseline_mean_reaction_atr",
    ):
        assert key in payload["results"][0]
