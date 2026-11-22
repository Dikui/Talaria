"""指标快通道 API：默认参数、窗口、FEED_GAP 断线与算子鉴权。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.bars import CompactBar
from talaria.foundation.rules import RulePreviewRequest
from talaria.workbench_api.app import create_app
from talaria.workbench_api.rule_preview import evaluate_preview
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GAP_TS = 1_793_538_240_000
EMA_PERIOD = 9


def _app_settings(tmp_path: Path, fixture_dir: Path | None, *, dist: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=dist,
        fixture_dir=fixture_dir,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def dist_dir(tmp_path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Talaria</title>", encoding="utf-8")
    return dist


@pytest.fixture
def client(tmp_path, fixture_dir, dist_dir):
    app = create_app(_app_settings(tmp_path, fixture_dir, dist=dist_dir))
    with TestClient(app) as instance:
        yield instance


@pytest.fixture
def snapshot_id(client) -> str:
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    return response.json()["snapshot_id"]


def _bars(client: TestClient, snapshot_id: str, limit: int = 1000) -> list[dict]:
    response = client.get(
        f"/api/snapshots/{snapshot_id}/bars",
        params={"timeframe": "1m", "limit": limit},
    )
    assert response.status_code == 200
    return response.json()["bars"]


def _indicators(client: TestClient, snapshot_id: str, **params):
    query = {"timeframe": "1m", "names": "EMA,RSI"}
    query.update({key: value for key, value in params.items() if value is not None})
    return client.get(f"/api/snapshots/{snapshot_id}/indicators", params=query)


# alerts-and-scanning/02: preview through the same authenticated API used by the chart.
def test_rule_preview_aligns_server_signals_and_referenced_indicators(client, snapshot_id) -> None:
    bars = _bars(client, snapshot_id, limit=3)
    expression = {
        "op": "compare",
        "test": "eq",
        "left": {"source": "bar", "field": "close"},
        "right": {"source": "indicator", "name": "SMA", "params": {"period": 1}},
    }
    response = client.post(
        f"/api/snapshots/{snapshot_id}/rule-preview",
        json={
            "timeframe": "1m", "limit": 3,
            "rule": {
                "version": "1", "timing": "bar_close",
                "expression": {"op": "all", "args": [{"op": "any", "args": [expression]}, expression]},
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert [{k: row[k] for k in ("timestamp", "price")} for row in result["condition_true"]] == [
        {"timestamp": bar["timestamp"], "price": bar["close"]} for bar in bars
    ]
    series, = result["indicators"]
    assert series["operand"]["params"]["period"] == 1
    assert series["points"] == [
        {"timestamp": bar["timestamp"], "values": {"value": bar["close"]}} for bar in bars
    ]


def test_rule_preview_keeps_referenced_operator_warmup_null(client, snapshot_id) -> None:
    response = client.post(
        f"/api/snapshots/{snapshot_id}/rule-preview",
        json={
            "timeframe": "1m", "limit": 3,
            "rule": {
                "version": "1", "timing": "bar_close", "expression": {
                    "op": "compare", "test": "gt",
                    "left": {"source": "indicator", "name": "SMA", "params": {"period": 5000}},
                    "right": {"source": "constant", "value": "0"},
                },
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    assert response.json()["condition_true"] == []
    line, = response.json()["indicators"]
    assert [point["values"] for point in line["points"]] == [{"value": None}] * 3


def test_rule_preview_does_not_use_a_late_bar_in_earlier_observations() -> None:
    def bar(timestamp: int, available: int, price: str) -> CompactBar:
        return CompactBar(
            timestamp=timestamp, ts_available=available, precision="SECOND_VERIFIED", status="NORMAL",
            open=price, close=price, high=price, low=price, volume="1", turnover=price,
        )

    bars = [bar(0, 180_001, "100"), bar(60_000, 120_000, "20"), bar(120_000, 180_000, "30")]
    request = RulePreviewRequest.model_validate({
        "timeframe": "1m", "rule": {
            "version": "1", "timing": "bar_close",
            "expression": {
                "op": "compare", "test": "gt",
                "left": {"source": "indicator", "name": "SMA", "params": {"period": 2}},
                "right": {"source": "constant", "value": "20"},
            },
        },
    })
    preview = evaluate_preview(request, "BTC-USDT-PERP", 180_001, bars, lambda: (), {}, lambda _: ())
    assert [signal.timestamp for signal in preview.condition_true] == [120_000]
    line, = preview.indicators
    assert [point.values["value"] for point in line.points] == [None, None, Decimal(25)]


def test_previewed_event_rule_can_be_saved_to_alerts_with_observations_from_frozen_events(client, snapshot_id) -> None:
    rule = {
        "version": "1", "timing": "event", "expression": {
            "op": "compare", "test": "gte",
            "left": {"source": "event", "metric": "last_price"},
            "right": {"source": "indicator", "name": "EMA"},
        },
    }
    url = f"/api/snapshots/{snapshot_id}/rule-preview"
    payload = {"timeframe": "1m", "limit": 3, "rule": rule}
    assert client.post(url, json=payload).status_code == 401
    preview = client.post(url, json=payload, headers=AUTH)
    assert preview.status_code == 200, preview.text
    assert all(hit["observed_at"] >= hit["timestamp"] + 60_000 for hit in preview.json()["condition_true"])
    line, = preview.json()["indicators"]
    assert line["operand"]["params"]["period"] == EMA_PERIOD
    assert all(point["values"]["value"] is not None for point in line["points"])
    assert client.get("/api/alerts").json()["alerts"] == []

    condition = preview.json()["rule"]
    saved = client.post(
        "/api/alerts",
        json={"instrument_id": "BTC-USDT-PERP", "timeframe": "1m", "condition": condition},
        headers=AUTH,
    )
    assert saved.status_code == 201, saved.text
    assert saved.json()["status"] == "ARMED"
    assert saved.json()["condition"] == condition
    assert client.get("/api/alerts").json()["alerts"] == [saved.json()]


@pytest.mark.parametrize(("name", "period"), [("UNKNOWN", 1), ("BOLL", 1)])
def test_rule_preview_rejects_unknown_operators_and_invalid_parameters(client, snapshot_id, name, period) -> None:
    response = client.post(
        f"/api/snapshots/{snapshot_id}/rule-preview",
        json={
            "timeframe": "1m",
            "rule": {
                "version": "1", "timing": "bar_close", "expression": {
                    "op": "compare", "test": "gt",
                    "left": {"source": "indicator", "name": name, "params": {"period": period}},
                    "right": {"source": "constant", "value": "0"},
                },
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 422


def test_bundle_aligns_with_latest_bars(client, snapshot_id) -> None:
    bars = _bars(client, snapshot_id, limit=300)
    response = _indicators(client, snapshot_id)
    assert response.status_code == 200
    bundle = response.json()
    assert bundle["snapshot_id"] == snapshot_id
    assert bundle["instrument_id"] == "BTC-USDT-PERP"
    assert bundle["timeframe"] == "1m"
    assert [series["name"] for series in bundle["series"]] == ["EMA", "RSI"]

    periods = {"EMA": EMA_PERIOD, "RSI": 14}
    for series in bundle["series"]:
        assert series["pane"] == ("main" if series["name"] == "EMA" else "sub")
        assert series["params"] == {
            "period": periods[series["name"]],
            "std_dev": None,
            "anchor": None,
        }
        assert len(series["points"]) == 300
        assert [point["timestamp"] for point in series["points"]] == [
            bar["timestamp"] for bar in bars
        ]
        for point in series["points"]:
            assert set(point["values"]) == {"value"}
            value = point["values"]["value"]
            assert value is None or isinstance(value, str)
        summary = series["summary"]
        assert summary["latest"] == series["points"][-1]["values"]
        assert summary["warmup_bars"] >= 0
        assert summary["valid_points"] <= 300
    ema = bundle["series"][0]
    assert ema["summary"]["latest"]["value"] is not None


def test_feed_gap_is_null_and_forces_full_rewarmup(client, snapshot_id) -> None:
    bars = _bars(client, snapshot_id, limit=1000)
    status_by_ts = {bar["timestamp"]: bar["status"] for bar in bars}
    gap_timestamps = [ts for ts, status in status_by_ts.items() if status == "FEED_GAP"]
    assert gap_timestamps and GAP_TS in gap_timestamps

    bundle = _indicators(
        client, snapshot_id, names="EMA,ATR,BOLL", limit=300
    ).json()
    points_by_name = {
        series["name"]: {point["timestamp"]: point["values"] for point in series["points"]}
        for series in bundle["series"]
    }

    for name, points in points_by_name.items():
        for timestamp in gap_timestamps:
            if timestamp in points:
                assert all(value is None for value in points[timestamp].values())

    ema_points = points_by_name["EMA"]
    assert ema_points[GAP_TS + 60_000]["value"] is not None


def test_default_window_is_latest_three_hundred(client, snapshot_id) -> None:
    response = _indicators(client, snapshot_id)
    assert response.status_code == 200
    for series in response.json()["series"]:
        assert len(series["points"]) == 300


def test_invalid_indicator_parameters_are_422(client, snapshot_id) -> None:
    base = f"/api/snapshots/{snapshot_id}/indicators"
    assert client.get(base, params={"timeframe": "1m", "names": "XYZ"}).status_code == 422
    assert client.get(base, params={"timeframe": "1m", "names": ""}).status_code == 422
    assert (
        client.get(base, params={"timeframe": "1m", "names": "EMA", "limit": 0}).status_code
        == 422
    )
    assert (
        client.get(base, params={"timeframe": "1m", "names": "EMA", "limit": 5001}).status_code
        == 422
    )


def test_unknown_snapshot_or_timeframe_is_404(client, snapshot_id) -> None:
    assert (
        client.get(
            "/api/snapshots/missing/indicators",
            params={"timeframe": "1m", "names": "EMA"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/snapshots/{snapshot_id}/indicators",
            params={"timeframe": "5m", "names": "EMA"},
        ).status_code
        == 404
    )


def test_operator_requires_bearer(client, snapshot_id) -> None:
    body = {"snapshot_id": snapshot_id, "timeframe": "1m", "operator": "EMA"}
    missing = client.post("/api/operators/compute", json=body)
    assert missing.status_code == 401
    wrong = client.post(
        "/api/operators/compute",
        json=body,
        headers={"Authorization": "Bearer wrong"},
    )
    assert wrong.status_code == 401


def test_operator_compute_uses_non_default_params(client, snapshot_id) -> None:
    response = client.post(
        "/api/operators/compute",
        json={
            "snapshot_id": snapshot_id,
            "timeframe": "1m",
            "operator": "EMA",
            "params": {"period": 5},
            "limit": 50,
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    series = response.json()
    assert series["name"] == "EMA"
    assert series["pane"] == "main"
    assert series["params"]["period"] == 5
    assert len(series["points"]) == 50
    assert series["summary"]["latest"] == series["points"][-1]["values"]


def test_operator_boll_channels_and_std_dev(client, snapshot_id) -> None:
    response = client.post(
        "/api/operators/compute",
        json={
            "snapshot_id": snapshot_id,
            "timeframe": "1m",
            "operator": "BOLL",
            "params": {"period": 10, "std_dev": "2.5"},
            "limit": 40,
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    series = response.json()
    assert series["params"] == {
        "period": 10,
        "std_dev": "2.5",
        "anchor": None,
    }
    for point in series["points"]:
        assert set(point["values"]) == {"mid", "upper", "lower"}
        if point["values"]["mid"] is None:
            continue
        mid = Decimal(point["values"]["mid"])
        assert Decimal(point["values"]["upper"]) > mid
        assert Decimal(point["values"]["lower"]) < mid


def test_operator_rejects_unknown_operator(client, snapshot_id) -> None:
    response = client.post(
        "/api/operators/compute",
        json={"snapshot_id": snapshot_id, "timeframe": "1m", "operator": "FOO"},
        headers=AUTH,
    )
    assert response.status_code == 422


class _LiveTail:
    def __init__(self, bars: list[dict]) -> None:
        self._bars = bars
        self.calls: list[tuple[str, str, int | None]] = []

    def recent_bars(
        self,
        *,
        instrument_id: str,
        timeframe: str,
        after_ts: int | None = None,
    ) -> list[CompactBar]:
        self.calls.append((instrument_id, timeframe, after_ts))
        return [
            bar
            for bar in self._bars
            if after_ts is None or bar.timestamp > after_ts
        ]


def test_live_query_appends_ws_bars(client, snapshot_id) -> None:
    frozen = _indicators(client, snapshot_id).json()
    last = _bars(client, snapshot_id, limit=1)[-1]
    live = {
        **last,
        "timestamp": last["timestamp"] + 60_000,
        "open": "69000",
        "high": "69100",
        "low": "68900",
        "close": "69000",
        "volume": "1.5",
        "turnover": "103500",
        "status": "NORMAL",
        "ts_available": last["ts_available"] + 60_000,
    }
    tail = _LiveTail([CompactBar.model_validate(live)])
    client.app.state.runtime.supervisor = tail

    bundle = _indicators(client, snapshot_id, live=1).json()
    assert tail.calls == [("BTC-USDT-PERP", "1m", last["timestamp"])]
    series = bundle["series"][0]
    assert series["points"][-1]["timestamp"] == live["timestamp"]
    assert series["summary"]["latest"] != frozen["series"][0]["summary"]["latest"]
    assert series["summary"]["latest"] == series["points"][-1]["values"]

    custom = client.post(
        "/api/operators/compute",
        params={"live": 1},
        json={
            "snapshot_id": snapshot_id,
            "timeframe": "1m",
            "operator": "EMA",
            "params": {"period": 5},
            "limit": 20,
        },
        headers=AUTH,
    )
    assert custom.status_code == 200
    assert custom.json()["points"][-1]["timestamp"] == live["timestamp"]

    still_frozen = _indicators(client, snapshot_id, live=0).json()
    assert still_frozen["series"][0]["points"][-1]["timestamp"] == last["timestamp"]


def test_get_indicators_uses_effective_user_defaults(client, snapshot_id) -> None:
    updated = client.put(
        "/api/indicator-defaults/EMA", json={"period": 30}, headers=AUTH
    )
    assert updated.status_code == 200

    series = _indicators(client, snapshot_id).json()["series"][0]
    assert series["name"] == "EMA"
    assert series["params"]["period"] == 30

    client.delete("/api/indicator-defaults/EMA", headers=AUTH)
    restored = _indicators(client, snapshot_id).json()["series"][0]
    assert restored["params"]["period"] == 9


def test_indicator_get_computes_outside_the_event_loop(
    client, snapshot_id, monkeypatch
) -> None:
    import asyncio

    workbench = client.app.state.runtime.workbench
    compute = workbench.get_indicators
    on_event_loop = []

    def compute_in_worker(*args, **kwargs):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_event_loop.append(False)
        else:
            on_event_loop.append(True)
        return compute(*args, **kwargs)

    monkeypatch.setattr(workbench, "get_indicators", compute_in_worker)
    assert _indicators(client, snapshot_id).status_code == 200
    assert on_event_loop == [False]


def test_preview_rejects_a_level_id_absent_from_frozen_versions(client, snapshot_id):
    response = client.post(f"/api/snapshots/{snapshot_id}/rule-preview", headers=AUTH, json={
        "timeframe": "1m", "rule": {"version": "1", "timing": "bar_close",
        "expression": {"op": "level_event", "level_id": "never-existed", "event": "TOUCH"}}})
    assert response.status_code == 422
    assert "never-existed" in response.text


def test_preview_reports_missing_historical_version_instead_of_zero_hits(client, snapshot_id):
    levels = client.get(f"/api/snapshots/{snapshot_id}/key-levels", params={"timeframe": "1m"}).json()["levels"]
    response = client.post(f"/api/snapshots/{snapshot_id}/rule-preview", headers=AUTH, json={
        "timeframe": "1m", "rule": {"version": "1", "timing": "bar_close",
        "expression": {"op": "level_event", "level_id": levels[0]["level_id"], "event": "TOUCH"}}})
    assert response.status_code == 422
    assert "历史版本不可用" in response.text


@pytest.mark.parametrize("reason", ["disconnect", "superseded"])
def test_preview_cancels_server_worker(client, snapshot_id, monkeypatch, reason):
    import asyncio
    from threading import Event
    from starlette.requests import Request
    from fastapi import HTTPException
    from talaria.workbench_api.routes.analysis import preview_rule

    started, stopped, disconnected = Event(), Event(), Event()
    payload = RulePreviewRequest(timeframe="1m", rule={
        "version": "1", "timing": "bar_close", "expression": {"op": "compare", "test": "gt",
        "left": {"source": "bar", "field": "close"}, "right": {"source": "constant", "value": "0"}}})
    service = client.app.state.runtime.workbench
    original = service.preview_rule

    def blocking(*args, **kwargs):
        if started.is_set():
            return original(*args, **kwargs)
        started.set()
        try:
            from time import monotonic, sleep
            deadline = monotonic() + 2
            while monotonic() < deadline:
                kwargs["check_cancelled"]()
                sleep(0.005)
            pytest.fail("superseded worker kept computing")
        finally:
            stopped.set()
    monkeypatch.setattr(service, "preview_rule", blocking)

    async def scenario():
        async def receive():
            if disconnected.is_set():
                return {"type": "http.disconnect"}
            await asyncio.Future()
        scope = {"type": "http", "app": client.app, "headers": [(b"x-preview-client", b"chart-panel")]}
        first = asyncio.create_task(preview_rule(snapshot_id, payload, Request(scope, receive)))
        assert await asyncio.to_thread(started.wait, 1)
        if reason == "disconnect":
            disconnected.set()
        else:
            await preview_rule(snapshot_id, payload, Request(scope, receive))
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(first, 1)
        assert error.value.status_code == 499
        assert await asyncio.to_thread(stopped.wait, 1)
    asyncio.run(scenario())


def test_obsolete_level_can_be_previewed_but_current_save_is_disabled(tmp_path, fixture_dir, dist_dir):
    from talaria.foundation.key_levels import KeyLevelSet

    now = GAP_TS - 120_000
    app = create_app(AppSettings(data_dir=tmp_path, dist_dir=dist_dir, fixture_dir=fixture_dir,
                                 token=TOKEN, now_ms=lambda: now))
    with TestClient(app) as client:
        def freeze():
            response = client.post("/api/snapshots", headers=AUTH,
                                   json={"instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"]})
            assert response.status_code == 200
            return response.json()["snapshot_id"]

        freeze()
        registry = app.state.runtime.key_levels
        level_id = registry.latest(("BTC-USDT-PERP", "1m")).levels[0].level_id
        from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
        from talaria.packs.testing import FixtureConnector
        from talaria.perception.raw_store import RawStore
        RawStore(tmp_path, raw_archive_id=CRYPTO_PERP_PACK.raw_archive_id).append("BTCUSDT", "1m",
            FixtureConnector(fixture_dir).fetch_bars(symbol="BTCUSDT", timeframe="1m", end_ts=NOW_MS, limit=300))
        now = NOW_MS
        snapshot_id = freeze()
        registry.register(KeyLevelSet(instrument_id="BTC-USDT-PERP", timeframe="1m",
                          cutoff_ts=now, as_of_ts=now, snapshot_id="empty", levels=[]))
        rule = {"version": "1", "timing": "bar_close",
                "expression": {"op": "level_event", "level_id": level_id, "event": "TOUCH"}}
        preview = client.post(f"/api/snapshots/{snapshot_id}/rule-preview", headers=AUTH,
                              json={"timeframe": "1m", "rule": rule})
        assert preview.status_code == 200, preview.text
        assert preview.json()["unavailable_current_levels"] == [level_id]
        assert client.post("/api/alerts", headers=AUTH, json={"instrument_id": "BTC-USDT-PERP",
                           "timeframe": "1m", "condition": rule}).status_code == 422
