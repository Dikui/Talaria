"""指标快通道 API：默认参数、窗口、FEED_GAP 断线与算子鉴权。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.bars import CompactBar
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GAP_TS = 1_793_538_240_000
EMA_PERIOD = 20


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
    after_gap = [
        ema_points[GAP_TS + offset * 60_000]["value"]
        for offset in range(1, EMA_PERIOD)
        if GAP_TS + offset * 60_000 in ema_points
    ]
    assert len(after_gap) == EMA_PERIOD - 1
    assert all(value is None for value in after_gap)
    assert ema_points[GAP_TS + EMA_PERIOD * 60_000]["value"] is not None


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
