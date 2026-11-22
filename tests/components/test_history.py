"""analysis/02 与 ADR-0019 的组件历史求值契约。"""

from contextlib import closing
from decimal import Decimal
from functools import partial

import pytest

from talaria import sdk
from talaria.foundation.bars import RangeBar
from talaria.foundation.connector import RawBar
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


@pytest.mark.parametrize(("name", "params"), [
    ("EMA", None),
    ("BOLL", {"period": 2, "std_dev": "2.5"}),
    ("VWAP", {"period": 1, "anchor": "snapshot"}),
])
def test_python_indicator_points_equal_the_chart_indicator_api(tmp_path, name, params):
    from fastapi.testclient import TestClient

    from talaria.components import ComponentMeta, Indicator, run_history
    from talaria.foundation.indicators import IndicatorBundle, IndicatorSeries
    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    cutoff = START + 5 * MINUTE
    data_dir = tmp_path / "data"
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
    history = list(run_history(
        indicator.compute, INSTRUMENT.instrument_id, "1m", start=START, end=cutoff,
        read_bars=partial(sdk.bars, data_dir=data_dir, offline=True),
    ))

    app = create_app(AppSettings(
        data_dir=data_dir, dist_dir=tmp_path / "dist", now_ms=lambda: cutoff, token="test-token",
        features=FeatureToggles(market_data=False, cold_store=False, alerts=False, scanner=False),
    ))
    auth = {"Authorization": "Bearer test-token"}
    with TestClient(app) as client:
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
                "snapshot_id": snapshot_id, "timeframe": "1m", "operator": name, "params": params,
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
    from talaria.foundation.derivatives import DerivativeKind, DerivativePoint

    def funding(ts, available, value):
        return DerivativePoint(
            instrument_id=INSTRUMENT.instrument_id, kind=DerivativeKind.FUNDING_RATE,
            ts_event=ts, ts_available=available, ts_ingested=available, ts_committed=available,
            value=Decimal(value),
        )

    first = funding(START, START, "0.01")
    late = funding(START + 1, START + MINUTE + 1, "0.03")
    second = funding(START + MINUTE, START + MINUTE, "0.02")

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
