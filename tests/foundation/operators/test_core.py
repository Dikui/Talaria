"""单一指标算子核：预热、NO_TRADES、FEED_GAP 与批量/增量一致性。

来源：任务单 04（.scratch/v1-analysis-workbench/issues/04-technical-indicators-and-chart-panes.md）。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Sequence

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.operators import (
    IndicatorName,
    IndicatorSpec,
    compute_series,
    spec_from_params,
    update_series,
)

pytestmark = [pytest.mark.small, pytest.mark.spec]

MINUTE = 60_000
BASE_TS = 1_700_000_000_000


def bar(
    index: int,
    *,
    close: str = "1",
    high: str | None = None,
    low: str | None = None,
    volume: str = "1",
    status: BarStatus = BarStatus.NORMAL,
) -> CompactBar:
    """构造紧凑柱；默认四价相等、成交量 1。"""
    timestamp = BASE_TS + index * MINUTE
    if status is BarStatus.FEED_GAP:
        return CompactBar(
            timestamp=timestamp,
            open=None,
            high=None,
            low=None,
            close=None,
            volume=None,
            turnover=None,
            status=status,
            ts_available=timestamp + MINUTE,
            precision="SECOND_VERIFIED",
        )
    close_value = Decimal(close)
    return CompactBar(
        timestamp=timestamp,
        open=close_value,
        high=Decimal(high) if high is not None else close_value,
        low=Decimal(low) if low is not None else close_value,
        close=close_value,
        volume=Decimal(volume),
        turnover=Decimal(volume) * close_value,
        status=status,
        ts_available=timestamp + MINUTE,
        precision="SECOND_VERIFIED",
    )


def no_trades(index: int, *, close: str) -> CompactBar:
    return bar(index, close=close, volume="0", status=BarStatus.NO_TRADES)


def flat_gap(index: int) -> CompactBar:
    return bar(index, status=BarStatus.FEED_GAP)


def values(bars: Sequence[CompactBar], spec: IndicatorSpec) -> list[Decimal | None]:
    """取单通道算子的 ``value`` 通道，供逐值断言。"""
    return [point["value"] for point in compute_series(bars, spec).points]


DAY_MS = 86_400_000
DAY0 = BASE_TS // DAY_MS * DAY_MS


def day_bar(day: int, minute: int, *, close: str, volume: str = "1") -> CompactBar:
    """构造落在指定 UTC 日内的柱。"""
    timestamp = DAY0 + day * DAY_MS + minute * MINUTE
    return bar(0, close=close, volume=volume).model_copy(
        update={"timestamp": timestamp, "ts_available": timestamp + MINUTE}
    )


def gap_at(day: int, minute: int) -> CompactBar:
    timestamp = DAY0 + day * DAY_MS + minute * MINUTE
    return bar(0, status=BarStatus.FEED_GAP).model_copy(
        update={"timestamp": timestamp, "ts_available": timestamp + MINUTE}
    )


def test_sma_has_period_minus_one_leading_nones() -> None:
    bars = [bar(i, close=str(i + 1)) for i in range(5)]
    result = values(bars, IndicatorSpec(name=IndicatorName.SMA, period=3))
    assert result == [None, None, Decimal("2"), Decimal("3"), Decimal("4")]


@pytest.mark.regression
def test_late_older_derivatives_do_not_replace_newer_events() -> None:
    # analysis/02: 图表和组件都应选开盘时已知的最新结算和持仓量。
    from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
    from talaria.foundation.operators.inputs import derivative_samples

    def points(kind):
        return [
            DerivativePoint(
                instrument_id="test", kind=kind, value=Decimal(value),
                ts_event=BASE_TS + event, ts_available=BASE_TS + available,
                ts_ingested=BASE_TS + available, ts_committed=BASE_TS + available,
            )
            for event, available, value in ((-1, 0, "2"), (-2, 1, "1"), (-1, MINUTE + 1, "3"))
        ]

    samples = derivative_samples(
        [bar(i) for i in range(3)],
        points(DerivativeKind.FUNDING_RATE), points(DerivativeKind.OPEN_INTEREST),
    )
    assert [(sample.funding_rate, sample.open_interest) for sample in samples] == [
        (Decimal(2), Decimal(2)), (Decimal(2), Decimal(2)), (Decimal(3), Decimal(3)),
    ]


def test_sma_keeps_state_through_no_trades() -> None:
    bars = [
        bar(0, close="1"),
        bar(1, close="2"),
        bar(2, close="3"),
        no_trades(3, close="3"),
        bar(4, close="5"),
    ]
    result = values(bars, IndicatorSpec(name=IndicatorName.SMA, period=3))
    assert result == [
        None,
        None,
        Decimal("2"),
        Decimal("8") / Decimal("3"),
        Decimal("11") / Decimal("3"),
    ]


def test_feed_gap_is_none_and_restarts_full_warmup() -> None:
    bars = [
        bar(0, close="1"),
        bar(1, close="2"),
        bar(2, close="3"),
        bar(3, close="4"),
        flat_gap(4),
        bar(5, close="5"),
        bar(6, close="6"),
        bar(7, close="7"),
        bar(8, close="8"),
    ]
    result = values(bars, IndicatorSpec(name=IndicatorName.SMA, period=3))
    assert result == [
        None,
        None,
        Decimal("2"),
        Decimal("3"),
        None,
        None,
        None,
        Decimal("6"),
        Decimal("7"),
    ]


def test_incremental_update_matches_batch_computation() -> None:
    spec = IndicatorSpec(name=IndicatorName.SMA, period=3)
    bars = [
        bar(0, close="1"),
        bar(1, close="2"),
        no_trades(2, close="2"),
        flat_gap(3),
        bar(4, close="4"),
        bar(5, close="5"),
        bar(6, close="6"),
        bar(7, close="7"),
    ]
    state = compute_series(bars[:3], spec)
    state = update_series(state, bars[3:5], spec)
    state = update_series(state, bars[5:], spec)
    batch = compute_series(bars, spec)
    assert state.points == batch.points


def test_output_length_always_equals_input_length() -> None:
    spec = IndicatorSpec(name=IndicatorName.SMA, period=1)
    bars = [bar(0, close="1"), flat_gap(1), bar(2, close="3")]
    assert len(compute_series(bars, spec).points) == len(bars)


def test_ema_seeds_with_first_close_then_smooths() -> None:
    bars = [bar(i, close=str(i + 1)) for i in range(5)]
    result = values(bars, IndicatorSpec(name=IndicatorName.EMA, period=3))
    assert result == [
        Decimal("1"),
        Decimal("1.5"),
        Decimal("2.25"),
        Decimal("3.125"),
        Decimal("4.0625"),
    ]


def test_ema_keeps_state_through_no_trades() -> None:
    bars = [
        bar(0, close="1"),
        bar(1, close="2"),
        bar(2, close="3"),
        no_trades(3, close="3"),
        bar(4, close="5"),
    ]
    result = values(bars, IndicatorSpec(name=IndicatorName.EMA, period=3))
    assert result == [
        Decimal("1"),
        Decimal("1.5"),
        Decimal("2.25"),
        Decimal("2.625"),
        Decimal("3.8125"),
    ]


def test_rsi_uses_wilder_smoothing() -> None:
    bars = [bar(i, close=str(i + 1)) for i in range(5)]
    result = values(bars, IndicatorSpec(name=IndicatorName.RSI, period=3))
    assert result == [None, None, None, Decimal("100"), Decimal("100")]


def test_rsi_is_fifty_when_gains_and_losses_balance() -> None:
    bars = [bar(0, close="1"), bar(1, close="2"), bar(2, close="1")]
    result = values(bars, IndicatorSpec(name=IndicatorName.RSI, period=2))
    assert result == [None, None, Decimal("50")]


def test_atr_seeds_with_sma_of_true_ranges_including_first_bar() -> None:
    bars = [
        bar(0, close="10", high="15", low="5"),
        bar(1, close="11", high="12", low="10"),
        bar(2, close="13", high="14", low="12"),
        bar(3, close="12", high="13", low="11"),
    ]
    result = values(bars, IndicatorSpec(name=IndicatorName.ATR, period=2))
    assert result == [None, Decimal("6"), Decimal("4.5"), Decimal("3.25")]


def test_atr_treats_no_trades_as_zero_true_range() -> None:
    bars = [
        bar(0, close="9.5", high="10", low="9"),
        bar(1, close="10.5", high="11", low="10"),
        no_trades(2, close="10.5"),
    ]
    result = values(bars, IndicatorSpec(name=IndicatorName.ATR, period=2))
    assert result == [None, Decimal("1.25"), Decimal("0.625")]


def test_boll_uses_population_deviation_and_configurable_multiplier() -> None:
    bars = [bar(0, close="1"), bar(1, close="3"), bar(2, close="5")]
    result = compute_series(
        bars,
        IndicatorSpec(name=IndicatorName.BOLL, period=2, std_dev=Decimal("2")),
    ).points
    assert result[0] == {"mid": None, "upper": None, "lower": None}
    assert result[1] == {
        "mid": Decimal("2"),
        "upper": Decimal("4"),
        "lower": Decimal("0"),
    }
    assert result[2] == {
        "mid": Decimal("4"),
        "upper": Decimal("6"),
        "lower": Decimal("2"),
    }


def test_vwap_carries_on_zero_volume_and_resets_after_gap() -> None:
    bars = [
        bar(0, close="9", high="10", low="8"),
        bar(1, close="12", high="13", low="11"),
        bar(2, close="15", high="16", low="14"),
        no_trades(3, close="15"),
        flat_gap(4),
        bar(5, close="18", high="19", low="17", volume="2"),
    ]
    result = values(
        bars, IndicatorSpec(name=IndicatorName.VWAP, anchor="snapshot")
    )
    assert result == [
        Decimal("9"),
        Decimal("10.5"),
        Decimal("12"),
        Decimal("12"),
        None,
        Decimal("18"),
    ]


@pytest.mark.parametrize(
    "spec",
    [
        IndicatorSpec(name=IndicatorName.EMA, period=3),
        IndicatorSpec(name=IndicatorName.SMA, period=3),
        IndicatorSpec(name=IndicatorName.RSI, period=3),
        IndicatorSpec(name=IndicatorName.ATR, period=3),
        IndicatorSpec(name=IndicatorName.BOLL, period=3),
        IndicatorSpec(name=IndicatorName.VWAP),
    ],
)
def test_incremental_matches_batch_for_every_operator(spec: IndicatorSpec) -> None:
    bars = [
        bar(0, close="1", high="1.2", low="0.8"),
        bar(1, close="2", high="2.4", low="1.6"),
        no_trades(2, close="2"),
        bar(3, close="3", high="3.6", low="2.4"),
        flat_gap(4),
        bar(5, close="4", high="4.8", low="3.2"),
        bar(6, close="5", high="6", low="4"),
        bar(7, close="6", high="7.2", low="4.8"),
    ]
    state = compute_series(bars[:2], spec)
    for index in range(2, len(bars)):
        state = update_series(state, bars[index : index + 1], spec)
        assert state.points[-1] == compute_series(bars[: index + 1], spec).points[-1]
    assert state.points == compute_series(bars, spec).points


def test_rejects_invalid_period_and_unknown_params() -> None:
    with pytest.raises(ValueError):
        IndicatorSpec(name=IndicatorName.SMA, period=0)
    with pytest.raises(ValueError):
        IndicatorSpec(name=IndicatorName.BOLL, period=1)
    with pytest.raises(ValueError):
        spec_from_params(IndicatorName.EMA, {"period": 3, "bogus": "1"})
    with pytest.raises(ValueError):
        spec_from_params(IndicatorName.EMA, {"period": "three"})


def test_vwap_session_skips_until_first_boundary_and_resets_daily() -> None:
    spec = spec_from_params("VWAP", {"anchor": "session"})
    bars = [
        day_bar(0, 1, close="100"),
        day_bar(0, 2, close="110"),
        day_bar(1, 1, close="200"),
        day_bar(1, 2, close="210"),
        day_bar(2, 1, close="300"),
    ]
    points = compute_series(bars, spec).points
    assert [point["value"] for point in points] == [
        None,
        None,
        Decimal("200"),
        Decimal("205"),
        Decimal("300"),
    ]


def test_vwap_session_rewarms_after_gap_within_same_day() -> None:
    spec = spec_from_params("VWAP", {"anchor": "session"})
    bars = [
        day_bar(0, 1, close="100"),
        gap_at(0, 2),
        day_bar(0, 3, close="110"),
        day_bar(1, 1, close="200"),
    ]
    values_ = [point["value"] for point in compute_series(bars, spec).points]
    assert values_ == [None, None, None, Decimal("200")]


def test_defaults_follow_tradingview_ema_and_sma_nine() -> None:
    assert spec_from_params("EMA").period == 9
    assert spec_from_params("SMA").period == 9
    assert spec_from_params("VWAP").anchor == "session"
    bars = [bar(i, close=value) for i, value in enumerate(["100", "101", "102"])]
    assert values(bars, spec_from_params("EMA")) == [
        Decimal("100"),
        Decimal("100.2"),
        Decimal("100.56"),
    ]


def test_anchor_is_rejected_for_non_vwap() -> None:
    with pytest.raises(ValueError, match="不接受 anchor"):
        spec_from_params("EMA", {"anchor": "session"})


def test_ema_uses_pine_decimal_operation_order_with_repeating_alpha() -> None:
    bars = [bar(0, close="1"), bar(1, close="6")]
    assert values(bars, spec_from_params("EMA", {"period": 5})) == [
        Decimal("1"), Decimal("2.666666666666666666666666667"),
    ]


def test_atr_uses_pine_rma_decimal_operation_order_with_repeating_alpha() -> None:
    bars = [bar(index, close="2", low="1", high="3") for index in range(3)]
    bars.append(bar(3, close="2", low="1", high="2"))
    assert values(bars, spec_from_params("ATR", {"period": 3})) == [
        None, None, Decimal("2"), Decimal("1.666666666666666666666666666"),
    ]
