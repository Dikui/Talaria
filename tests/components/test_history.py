"""analysis/02 与 ADR-0019 的组件历史求值契约。"""

from contextlib import closing
from decimal import Decimal
from functools import partial

import pytest

from talaria import sdk
from talaria.foundation.bars import RangeBar
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.timeframes import timeframe_ms
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
START = 1_793_491_200_000
MINUTE = 60_000


def _bar(ts, close, *, timeframe="1m", delay=0, revision=0, volume="1"):
    value = Decimal(close)
    available = ts + timeframe_ms(timeframe) + delay
    return RawBar(
        symbol=INSTRUMENT.symbol, timeframe=timeframe, ts_event=ts,
        open=value, high=value, low=value, close=value, volume=Decimal(volume),
        turnover=value * Decimal(volume), ts_available=available, ts_ingested=available,
        ts_committed=available, revision=revision,
    )


def _derivative(ts, available, value, *, kind=DerivativeKind.FUNDING_RATE, timeframe=None, settled=True):
    return DerivativePoint(
        instrument_id=INSTRUMENT.instrument_id, kind=kind, timeframe=timeframe,
        ts_event=ts, ts_available=available, ts_ingested=available, ts_committed=available,
        ts_published=ts if kind is DerivativeKind.FUNDING_RATE and settled else None,
        value=Decimal(value),
    )


def test_history_evaluates_only_at_confirmed_base_closes():
    from talaria.components import run_history

    preview = _bar(START + MINUTE, "20")
    open_bar = RangeBar.model_validate(preview, from_attributes=True).model_copy(
        update={"confirmed": False},
    )
    closed = RangeBar.model_validate(_bar(START, "10"), from_attributes=True)
    future = RangeBar.model_validate(_bar(START + 2 * MINUTE, "30"), from_attributes=True)
    calls = []

    def read_bars(instrument, timeframe, *, start, end, as_of):
        # Simulates a source with a preview and an early final payload for a forming slot.
        return (closed, open_bar, future)

    def evaluate(ctx):
        calls.append(ctx.now)
        return tuple(bar.close for bar in ctx.bars())

    result = list(run_history(
        evaluate, INSTRUMENT.instrument_id, "1m", start=START,
        end=START + 2 * MINUTE + 8_000, read_bars=read_bars,
    ))

    assert calls == [START + MINUTE]
    assert [(step.as_of, step.output) for step in result] == [(START + MINUTE, (Decimal("10"),))]


def test_truncated_history_is_a_prefix_and_higher_timeframe_never_repaints(tmp_path):
    from talaria.components import run_history

    instrument = INSTRUMENT.instrument_id
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START, "10"),
            _bar(START, "99", delay=5 * MINUTE + 1, revision=1),
            *(_bar(START + i * MINUTE, str(10 + i)) for i in range(1, 12)),
        ]))
        store.publish(ColdBatch(INSTRUMENT, "5m", [
            _bar(START, "50", timeframe="5m", delay=8_000),
            _bar(START + 5 * MINUTE, "90", timeframe="5m"),
        ]))

    def evaluate(ctx):
        return ctx.indicator("SMA", period=1)[0].values["value"], tuple(
            (bar.ts_event, bar.close) for bar in ctx.bars("5m")
        ), tuple(
            point.values["value"] for point in ctx.indicator("SMA", timeframe="5m", period=1)
        )

    def run(end):
        return list(run_history(
            evaluate, instrument, "1m", start=START, end=end,
            read_bars=partial(sdk.bars, data_dir=tmp_path, offline=True),
        ))

    full = run(START + 12 * MINUTE)
    for minutes in range(13):
        for offset in (0, 8_000):
            cutoff = START + minutes * MINUTE + offset
            assert run(cutoff) == [step for step in full if step.as_of <= cutoff]
    assert full[4].output == (Decimal("10"), (), ())
    first_higher = ((START, Decimal("50")),)
    assert full[5].output == (Decimal("10"), first_higher, (Decimal("50"),))
    assert [step.output for step in full[6:9]] == [(Decimal("99"), first_higher, (Decimal("50"),))] * 3
    assert full[9].output == (Decimal("99"), ((START + 5 * MINUTE, Decimal("90")),), (Decimal("90"),))


@pytest.mark.regression
def test_history_evaluates_late_confirmations_at_availability_in_event_order(tmp_path):
    from talaria.components import run_history

    available = START + 3 * MINUTE + 8_000
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START, "10", delay=2 * MINUTE + 8_000),
            _bar(START, "90", delay=3 * MINUTE, revision=1),
            _bar(START + MINUTE, "20", delay=MINUTE + 8_000),
            _bar(START + 2 * MINUTE, "30"),
        ]))

    def run(end):
        return list(run_history(
            lambda ctx: ctx.bars()[-1].close, INSTRUMENT.instrument_id, "1m",
            start=START, end=end, read_bars=partial(sdk.bars, data_dir=tmp_path, offline=True),
        ))

    result = run(START + 5 * MINUTE)
    assert [(step.as_of, step.output) for step in result] == [
        (START + 3 * MINUTE, Decimal(30)), (available, Decimal(10)), (available, Decimal(20)),
    ]
    assert run(available - 1) == result[:1]
    assert run(available) == result


@pytest.mark.regression
def test_higher_timeframe_warms_up_before_a_replay_window(tmp_path):
    from talaria.components import run_history

    window = START + 16 * MINUTE
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START + i * MINUTE, "10") for i in range(17)
        ]))
        store.publish(ColdBatch(INSTRUMENT, "5m", [
            _bar(START + i * 5 * MINUTE, close, timeframe="5m")
            for i, close in enumerate(("10", "20", "30", "40"))
        ]))

    def evaluate(ctx):
        return (
            tuple(bar.close for bar in ctx.bars("5m")),
            tuple(point.values["value"] for point in ctx.indicator("SMA", timeframe="5m", period=3)),
        )

    def run(start):
        return list(run_history(
            evaluate, INSTRUMENT.instrument_id, "1m", start=start, end=window + MINUTE,
            read_bars=partial(sdk.bars, data_dir=tmp_path, offline=True),
        ))

    continuous = run(START)[-1:]
    assert continuous[0].output == ((Decimal(30),), (Decimal(20),))
    assert run(window) == continuous


@pytest.mark.parametrize(("name", "params", "override"), [
    ("EMA", None, None),
    ("BOLL", {"period": 2, "std_dev": "2.5"}, None),
    ("VWAP", {"period": 1, "anchor": "snapshot"}, None),
    pytest.param("BOLL", None, {"period": 2, "std_dev": "3"}, marks=pytest.mark.regression),
    pytest.param(
        "BOLL", {"std_dev": "2.5"}, {"period": 2, "std_dev": "3"}, marks=pytest.mark.regression,
    ),
])
def test_python_indicator_points_equal_the_chart_indicator_api(
    tmp_path, monkeypatch, name, params, override,
):
    from fastapi.testclient import TestClient

    from talaria.components import ComponentMeta, Indicator, run_history
    from talaria.foundation.indicators import IndicatorBundle, IndicatorSeries
    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    cutoff = START + 5 * MINUTE
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TALARIA_DATA_DIR", str(data_dir))
    with closing(ColdStore(data_dir)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START, "10"),
            _bar(START + MINUTE, "10", volume="0"),
            _bar(START + 3 * MINUTE, "13"),
            _bar(START + 4 * MINUTE, "15"),
            _bar(START + 4 * MINUTE, "90", delay=15_000, revision=1),
        ]))

    class PythonIndicator:
        meta = ComponentMeta(name, "1", ("bars",), {}, "series")

        def compute(self, ctx):
            return ctx.indicator(name, **(params or {}))

    indicator: Indicator = PythonIndicator()
    app = create_app(AppSettings(
        data_dir=data_dir, dist_dir=tmp_path / "dist", now_ms=lambda: cutoff, token="test-token",
        features=FeatureToggles(market_data=False, cold_store=False, alerts=False, scanner=False),
    ))
    auth = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
        if override is not None:
            response = client.put(f"/api/indicator-defaults/{name}", headers=auth, json=override)
            assert response.status_code == 200, response.text
        history = list(run_history(
            indicator.compute, INSTRUMENT.instrument_id, "1m", start=START, end=cutoff,
            read_bars=partial(sdk.bars, offline=True),
        ))
        snapshot = client.post("/api/snapshots", headers=auth, json={
            "instrument_id": INSTRUMENT.instrument_id, "timeframes": ["1m"], "cutoff_ts": cutoff,
        })
        assert snapshot.status_code == 200, snapshot.text
        snapshot_id = snapshot.json()["snapshot_id"]
        if params is None:
            response = client.get(
                f"/api/snapshots/{snapshot_id}/indicators",
                params={"timeframe": "1m", "names": name},
            )
            assert response.status_code == 200, response.text
            chart = IndicatorBundle.model_validate_json(response.content).series[0]
        else:
            response = client.post("/api/operators/compute", headers=auth, json={
                "snapshot_id": snapshot_id, "timeframe": "1m", "operator": name,
                "params": (override or {}) | params,
            })
            assert response.status_code == 200, response.text
            chart = IndicatorSeries.model_validate_json(response.content)

    assert history[-1].output == tuple(chart.points)
    assert all(value is None for value in chart.points[2].values.values())
    assert [step.output[-1] for step in history] == [point for i, point in enumerate(chart.points) if i != 2]


def test_signal_and_strategy_use_only_derivatives_visible_at_the_evaluation(tmp_path):
    from talaria.components import (
        ComponentMeta, ControllerPatch, Signal, SignalEvent, Strategy, run_history,
    )
    first = _derivative(START, START, "0.01")
    late = _derivative(START + 1, START + MINUTE + 1, "0.03")
    second = _derivative(START + MINUTE, START + MINUTE, "0.02")

    def read_derivatives(instrument, kind, *, start, end, as_of):
        # Supplies a late observation outside the cutoff as an injected snapshot source.
        return (first, late, second) if kind is DerivativeKind.FUNDING_RATE else ()

    class FundingSignal:
        meta = ComponentMeta("funding", "1", ("funding_rate",), {}, "signal_events")

        def evaluate(self, ctx):
            return [
                SignalEvent(ctx.now, "funding", {"rate": point.value})
                for point in ctx.derivatives("funding_rate")
            ]

    signal: Signal = FundingSignal()

    class FundingStrategy:
        meta = ComponentMeta("sizing", "1", ("funding",), {}, "strategy_decision")

        def on_bar(self, ctx):
            events = signal.evaluate(ctx)
            rank = ctx.indicator("FUNDING_QUANTILE", period=2)[-1].values["value"]
            return ControllerPatch(
                {"count": len(events), "rank": rank or Decimal(0)}, ctx.now + MINUTE, "research",
            )

    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START, "10"), _bar(START + MINUTE, "11"),
        ]))

    strategy: Strategy = FundingStrategy()
    result = list(run_history(
        strategy.on_bar, INSTRUMENT.instrument_id, "1m", start=START, end=START + 2 * MINUTE,
        read_bars=partial(sdk.bars, data_dir=tmp_path, offline=True), read_derivatives=read_derivatives,
    ))
    assert [step.output.parameters for step in result] == [
        {"count": 2, "rank": Decimal(0)}, {"count": 3, "rank": Decimal("0.75")},
    ]


@pytest.mark.regression
def test_replay_window_includes_the_latest_prior_derivative_value():
    from talaria.components import run_history

    latest = _derivative(START - MINUTE, START - 1, "0.02")
    late = _derivative(START - 2 * MINUTE, START + 1, "0.03")

    def read_derivatives(instrument, kind, *, start, end, as_of):
        return [
            point for point in (latest, late)
            if point.kind == kind and start <= point.ts_event <= end and point.ts_available <= as_of
        ]

    def evaluate(ctx):
        return (
            ctx.derivatives("funding_rate"),
            ctx.indicator("FUNDING_QUANTILE", period=1)[-1].values["value"],
        )

    row = RangeBar.model_validate(_bar(START, "10"), from_attributes=True)
    result = list(run_history(
        evaluate, INSTRUMENT.instrument_id, "1m", start=START, end=START + MINUTE,
        read_bars=lambda *args, **kwargs: (row,), read_derivatives=read_derivatives,
    ))
    assert result[0].output == ((latest,), Decimal("0.5"))


@pytest.mark.regression
@pytest.mark.parametrize("name", ["FUNDING_QUANTILE", "OI_DIVERGENCE", "NET_ODDS"])
def test_missing_derivative_source_produces_missing_indicator_values(name):
    from talaria.components import run_history

    rows = tuple(
        RangeBar.model_validate(_bar(START + i * MINUTE, close), from_attributes=True)
        for i, close in enumerate(("10", "11", "11"))
    )
    result = list(run_history(
        lambda ctx: ctx.indicator(name, period=1), INSTRUMENT.instrument_id, "1m",
        start=START, end=START + len(rows) * MINUTE, read_bars=lambda *args, **kwargs: rows,
    ))
    assert [point.values["value"] for point in result[-1].output] == [None, None, None]


@pytest.mark.regression
def test_net_odds_accepts_an_observed_zero_funding_rate():
    from talaria.components import run_history

    rows = tuple(
        RangeBar.model_validate(_bar(START + i * MINUTE, close), from_attributes=True)
        for i, close in enumerate(("10", "11"))
    )
    zero = _derivative(START, START, "0")
    result = list(run_history(
        lambda ctx: ctx.indicator("NET_ODDS", period=1), INSTRUMENT.instrument_id, "1m",
        start=START, end=START + len(rows) * MINUTE, read_bars=lambda *args, **kwargs: rows,
        read_derivatives=lambda instrument, kind, **kwargs: (zero,) if kind == zero.kind else (),
    ))
    assert result[-1].output[-1].values["value"] == Decimal(1)


@pytest.mark.regression
@pytest.mark.parametrize("explicit_reader", [False, True])
def test_history_net_odds_does_not_treat_a_ticker_rate_as_a_settlement(tmp_path, explicit_reader):
    """data-access/08：实时资金费率可读，净赔率仍以结算事件计资金成本。"""
    from talaria.components import run_history

    rows = [_bar(START, "100"), _bar(START + MINUTE, "102")]
    estimate = _derivative(START + MINUTE, START + MINUTE, "0.001", settled=False)
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", rows))
        store.publish_derivatives(INSTRUMENT, estimate.kind, [estimate])

    result = list(run_history(
        lambda ctx: (
            ctx.derivatives("funding_rate"),
            ctx.indicator("NET_ODDS", period=1)[-1].values["value"],
        ),
        INSTRUMENT.instrument_id, "1m", start=START, end=START + 2 * MINUTE, data_dir=tmp_path,
        read_derivatives=partial(sdk.derivatives, data_dir=tmp_path) if explicit_reader else None,
    ))
    assert [point.value for point in result[-1].output[0]] == [Decimal("0.001")]
    assert result[-1].output[1] is None


@pytest.mark.regression
def test_funding_operator_can_use_a_reader_with_only_funding_data():
    from talaria.components import run_history

    row = RangeBar.model_validate(_bar(START, "10"), from_attributes=True)
    point = _derivative(START, START, "0.01")

    def funding_reader(instrument, kind, **kwargs):
        if kind != point.kind:
            raise ValueError(f"no series for {kind}")
        return (point,)

    result = list(run_history(
        lambda ctx: ctx.indicator("FUNDING_QUANTILE", period=1), INSTRUMENT.instrument_id, "1m",
        start=START, end=START + MINUTE, read_bars=lambda *args, **kwargs: (row,),
        read_derivatives=funding_reader,
    ))
    assert result[0].output[0].values["value"] == Decimal("0.5")


@pytest.mark.regression
def test_registered_custom_operator_receives_its_declared_mark_price():
    from talaria.components import run_history
    from talaria.components.operators import OPERATORS, GoldenVector, Operator
    from talaria.foundation.operators import DerivativeSample
    from talaria.perception.compaction import compact_series

    row = RangeBar.model_validate(_bar(START, "10"), from_attributes=True)
    bars = tuple(compact_series((row,), timeframe="1m", cutoff_ts=START + MINUTE))
    mark = _derivative(START, START, "11", kind=DerivativeKind.MARK_PRICE, timeframe="1m")
    other_timeframe = mark.model_copy(update={"timeframe": "5m", "value": Decimal(99)})

    def compute(bars, params, *, derivatives):
        return tuple({"value": sample.mark_price} for sample in derivatives)

    operator = Operator(
        name="test_mark_price", version="1", parameters_schema={}, compute=compute,
        golden=GoldenVector(
            bars, {}, ({"value": Decimal(11)},), (DerivativeSample(mark_price=Decimal(11)),),
        ),
        derivative_inputs=(DerivativeKind.MARK_PRICE,),
    )
    OPERATORS.register(operator)
    result = list(run_history(
        lambda ctx: ctx.indicator(operator.name), INSTRUMENT.instrument_id, "1m",
        start=START, end=START + MINUTE, read_bars=lambda *args, **kwargs: (row,),
        read_derivatives=lambda *args, **kwargs: (mark, other_timeframe),
    ))
    assert result[0].output[0].values["value"] == Decimal(11)


@pytest.mark.regression
@pytest.mark.parametrize("incomplete", ["none", "schedule", "base", "higher"])
def test_replay_reports_incomplete_sdk_reads_in_the_context_and_result(incomplete):
    from talaria.components import run_history

    base = RangeBar.model_validate(_bar(START, "10"), from_attributes=True)
    higher = RangeBar.model_validate(
        _bar(START - 5 * MINUTE, "9", timeframe="5m"), from_attributes=True,
    )
    end = START + 2 * MINUTE

    def read_bars(instrument, timeframe, *, start, end, as_of):
        if timeframe == "5m":
            return sdk.BarsResult((higher,), tail_completed=incomplete != "higher")
        source = "base" if as_of == START + MINUTE else "schedule"
        return sdk.BarsResult((base,), tail_completed=incomplete != source)

    def evaluate(ctx):
        assert ctx.bars("5m") == (higher,)
        return ctx.tail_completed

    result, = run_history(
        evaluate, INSTRUMENT.instrument_id, "1m", start=START, end=end, read_bars=read_bars,
    )
    assert result.tail_completed == result.output == (incomplete == "none")


@pytest.mark.regression
def test_replay_keeps_only_the_highest_visible_confirmed_revision_from_an_alternate_reader():
    from talaria.components import run_history

    def range_bar(ts, close, **kwargs):
        return RangeBar.model_validate(_bar(ts, close, **kwargs), from_attributes=True)

    current = range_bar(START + MINUTE, "20")
    original = range_bar(START, "10")
    revised = range_bar(START, "11", delay=1, revision=1)
    future = range_bar(START, "99", delay=3 * MINUTE, revision=2)
    preview = range_bar(START, "88", revision=3).model_copy(update={"confirmed": False})
    rows = (current, future, preview, revised, original)
    result = list(run_history(
        lambda ctx: ctx.bars(), INSTRUMENT.instrument_id, "1m",
        start=START, end=START + 2 * MINUTE, read_bars=lambda *args, **kwargs: rows,
    ))
    assert [(step.as_of, step.output) for step in result] == [
        (START + MINUTE, (original,)),
        (START + 2 * MINUTE, (revised, current)),
    ]


def test_standard_signal_receives_historical_geometry_and_confirmed_event():
    from talaria.components import compile_rule, run_history
    from talaria.foundation.drawings import DrawingRecord
    from talaria.foundation.rules import Rule

    drawing = DrawingRecord(drawing_id="line", kind="HORIZONTAL_LINE", instrument_id=INSTRUMENT.instrument_id,
                            visible_timeframes=["1m"], points=[dict(ts=START, price="100")],
                            created_ts=START, updated_ts=START, version=1, text="", style={})
    rule = Rule.model_validate(dict(version="1", timing="bar_close", expression=dict(
        op="overlay", drawing_id="line", direction="ABOVE", mode="CLOSE_BREAK")))
    bars = [RangeBar.model_validate(_bar(START + i * MINUTE, price), from_attributes=True)
            for i, price in enumerate(["99", "101"])]
    def drawing_at(drawing_id, as_of):
        return drawing if drawing_id == "line" and as_of >= drawing.created_ts else None
    def run(ctx):
        assert ctx.event.bar.timestamp + MINUTE == ctx.now
        return compile_rule(rule).evaluate(ctx)
    results = list(run_history(run, INSTRUMENT.instrument_id, "1m", start=START, end=START + 2 * MINUTE,
                               read_bars=lambda *a, **kw: bars, drawing_at=drawing_at))
    assert [bool(step.output) for step in results] == [False, True]


@pytest.mark.regression
@pytest.mark.parametrize("explicit_reader", [False, True])
def test_indicator_uses_each_bars_first_confirmation_to_select_derivative_revisions(tmp_path, explicit_reader):
    # data-access/08、analysis/02：后来的柱和结算修订都不会改变第一根柱的求值时点。
    from talaria.components import run_history

    original = _derivative(START, START, "0.0001")
    revised = _derivative(START, START + MINUTE + 1, "0.0002").model_copy(update={"revision": 1})
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START, "100"),
            _bar(START, "101", revision=1, delay=MINUTE - 1),
            _bar(START + MINUTE, "102"),
        ]))
        store.publish_derivatives(INSTRUMENT, original.kind, [original, revised])

    first, second = run_history(
        lambda ctx: ctx.indicator("FUNDING_QUANTILE", period=2),
        INSTRUMENT.instrument_id, "1m", start=START, end=START + 2 * MINUTE, data_dir=tmp_path,
        read_derivatives=partial(sdk.derivatives, data_dir=tmp_path) if explicit_reader else None,
    )
    assert [point.values["value"] for point in first.output] == [None]
    assert [point.values["value"] for point in second.output] == [None, Decimal("0.75")]


@pytest.mark.regression
def test_derivatives_preserve_pre_window_settlement_and_estimate_independently(tmp_path):
    # data-access/08：结算和 ticker 预估都供组件读取，各保留窗口前最近的一点。
    from talaria.components import run_history

    actual = _derivative(START - 2, START - 2, "0.0001")
    estimate = _derivative(START - 1, START - 1, "0.0002", settled=False)
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [_bar(START, "100")]))
        store.publish_derivatives(INSTRUMENT, actual.kind, [actual, estimate])

    [step] = run_history(
        lambda ctx: ctx.derivatives("funding_rate"),
        INSTRUMENT.instrument_id, "1m", start=START, end=START + MINUTE, data_dir=tmp_path,
    )
    assert [(point.is_funding_settlement, point.value) for point in step.output] == [
        (True, actual.value), (False, estimate.value),
    ]


@pytest.mark.regression
def test_net_odds_uses_the_corrected_rate_once_it_is_visible(tmp_path):
    # data-access/08：同一结算的更正替换成本，结算身份和历史输出都保留。
    from talaria.components import run_history

    original = _derivative(START + 2 * MINUTE, START + 2 * MINUTE, "0.01")
    revised = _derivative(START + 2 * MINUTE, START + 4 * MINUTE + 1, "0.02").model_copy(
        update={"revision": 1},
    )
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", [
            _bar(START + i * MINUTE, price) for i, price in enumerate(("100", "102", "104", "106", "108"))
        ]))
        store.publish_derivatives(INSTRUMENT, original.kind, [original, revised])

    result = list(run_history(
        lambda ctx: ctx.indicator("NET_ODDS", period=3),
        INSTRUMENT.instrument_id, "1m", start=START, end=START + 5 * MINUTE, data_dir=tmp_path,
    ))
    assert result[-2].output[-1].values["value"] == Decimal("2.47")
    assert result[-1].output[-1].values["value"] == Decimal("1.92")


@pytest.mark.regression
def test_alert_scan_and_history_observe_the_same_archived_funding_cross(tmp_path):
    # alerts-and-scanning/01 × data-access/08：同一结算的更正跨过零，期间还有负的 ticker 预估。
    import asyncio

    from talaria.components import compile_rule, run_history
    from talaria.foundation.alerts import AlertCreate
    from talaria.foundation.feed import KlineEvent
    from talaria.foundation.rules import Rule
    from talaria.workbench_api.runtime import build_runtime
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    symbol = INSTRUMENT.instrument_id
    bars = [_bar(START, "100"), _bar(START, "101", revision=1, delay=MINUTE - 1),
            _bar(START + MINUTE, "102")]
    original = _derivative(START, START, "-0.0001")
    revised = _derivative(START, START + MINUTE + 1, "0.0002").model_copy(update={"revision": 1})
    estimate_ts = START + MINUTE + 2
    estimate = _derivative(estimate_ts, estimate_ts, "-0.0003", settled=False)
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", bars))
        store.publish_derivatives(INSTRUMENT, original.kind, [original, revised, estimate])

    rule = Rule.model_validate({"version": "1", "timing": "bar_close", "expression": {"op": "all", "args": [
        {"op": "compare", "test": "cross_above", "left": {"source": "derivative", "kind": "funding_rate"},
         "right": {"source": "constant", "value": "0"}},
        {"op": "compare", "test": "eq", "left": {"source": "indicator", "name": "FUNDING_QUANTILE",
         "params": {"period": 2}}, "right": {"source": "constant", "value": "0.75"}},
    ]}})
    history = run_history(compile_rule(rule).evaluate, symbol, "1m", start=START,
                          end=START + 2 * MINUTE, data_dir=tmp_path)
    expected = [(START + 2 * MINUTE, Decimal("0.0002"))]
    assert [(step.as_of, signal.values["observed"]) for step in history for signal in step.output] == expected

    async def replay():
        now = START
        runtime = await build_runtime(AppSettings(
            data_dir=tmp_path, dist_dir=tmp_path / "dist", now_ms=lambda: now,
            features=FeatureToggles(market_data=False),
        ))
        try:
            alerts, scanner = runtime.require_alerts(), runtime.require_scanner()
            alert = await alerts.create_alert(AlertCreate(instrument_id=symbol, timeframe="1m", condition=rule))
            hits = []
            for bar in bars:
                now = bar.ts_available
                event = KlineEvent(instrument_id=symbol, timeframe="1m", bar=bar.to_compact(),
                                   confirmed=True, revision=bar.revision)
                alerts.handle_event(event)
                scanner.handle_event(event)
                hits.extend(await scanner.scan([symbol], rule=rule))
            triggers = await alerts.list_triggers(alert_id=alert.alert_id)
            assert [(t.triggered_ts, t.observed) for t in triggers] == expected
            assert [(t.created_ts, t.evidence[0].value) for t in hits] == expected
        finally:
            await runtime.aclose()

    asyncio.run(replay())
