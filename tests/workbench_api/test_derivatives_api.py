"""衍生品快通道：PIT 截断、十进制字符串、空 OI、指标读取与 WS 事件。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.derivatives import (
    DerivativeKind,
    DerivativePoint,
    RawDerivativePoint,
)
from talaria.foundation.feed import (
    DerivativeTickEvent,
    FeedHealthEvent,
    FeedHealthStatus,
    KlineEvent,
    OrderBookEvent,
)
from talaria.foundation.snapshot import SnapshotMeta, SnapshotMode
from talaria.foundation.types import PrecisionTier
from talaria.perception.snapshot_store import (
    SnapshotStore,
    read_derivative_points,
    write_derivative_parquet,
    write_snapshot_parquet,
)
from talaria.workbench_api.app import create_app
from talaria.workbench_api.routes.desk_ws import _wanted, _ws_payload
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def dist_dir(tmp_path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>Talaria</title>", encoding="utf-8")
    return dist


@pytest.fixture
def client(tmp_path, fixture_dir, dist_dir):
    settings = AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=dist_dir,
        fixture_dir=fixture_dir,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )
    with TestClient(create_app(settings)) as instance:
        yield instance


def _snapshot(client: TestClient, instrument_id: str, timeframes: list[str]) -> dict:
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": instrument_id, "timeframes": timeframes},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _derivatives(client: TestClient, snapshot_id: str, timeframe: str = "1m"):
    return client.get(
        f"/api/snapshots/{snapshot_id}/derivatives", params={"timeframe": timeframe}
    )


def test_bundle_contains_three_series_with_decimal_strings_and_summary(client) -> None:
    snapshot = _snapshot(client, "BTC-USDT-PERP", ["1m"])
    response = _derivatives(client, snapshot["snapshot_id"])
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["snapshot_id"] == snapshot["snapshot_id"]
    assert body["instrument_id"] == "BTC-USDT-PERP"
    assert body["timeframe"] == "1m"
    assert body["cutoff_ts"] == NOW_MS
    series = {item["kind"]: item for item in body["series"]}
    assert set(series) == {"mark_price", "funding_rate", "open_interest"}
    assert series["mark_price"]["timeframe"] == "1m"
    assert series["funding_rate"]["timeframe"] is None
    assert series["open_interest"]["timeframe"] == "5m"
    for item in series.values():
        assert item["points"]
        for point in item["points"]:
            assert isinstance(point["value"], str)
            assert point["ts_available"] <= NOW_MS
            Decimal(point["value"])

    mark = series["mark_price"]["points"][-1]
    funding = series["funding_rate"]["points"][-1]
    oi = series["open_interest"]["points"][-1]
    summary = body["summary"]
    assert summary["mark_price"] == mark["value"]
    assert summary["mark_price_ts"] == mark["ts_event"]
    assert summary["funding_rate"] == funding["value"]
    assert summary["funding_settled_ts"] == funding["ts_event"]
    expected_funding = funding["ts_event"] + 8 * 3_600_000
    while expected_funding <= NOW_MS:
        expected_funding += 8 * 3_600_000
    assert summary["next_funding_ts"] == expected_funding
    assert summary["next_funding_ts"] > NOW_MS
    assert summary["open_interest"] == oi["value"]
    assert summary["open_interest_ts"] == oi["ts_event"]
    assert summary["oi_history_available"] is True


def test_new_listing_without_oi_history_returns_empty_state(client) -> None:
    snapshot = _snapshot(client, "1000BONK-USDT-PERP", ["1m"])
    body = _derivatives(client, snapshot["snapshot_id"]).json()
    series = {item["kind"]: item for item in body["series"]}
    assert series["mark_price"]["points"]
    assert series["open_interest"]["points"] == []
    assert series["funding_rate"]["points"] == []
    assert body["summary"]["open_interest"] is None
    assert body["summary"]["open_interest_ts"] is None
    assert body["summary"]["oi_history_available"] is False
    assert body["summary"]["funding_rate"] is None


def test_unavailable_snapshot_and_timeframe_are_404(client) -> None:
    assert _derivatives(client, "missing").status_code == 404
    snapshot = _snapshot(client, "BTC-USDT-PERP", ["1m"])
    response = _derivatives(client, snapshot["snapshot_id"], timeframe="5m")
    assert response.status_code == 404


def test_reader_rechecks_available_time_against_cutoff(client, tmp_path) -> None:
    snapshot = _snapshot(client, "BTC-USDT-PERP", ["1m"])
    settings = client.app.state.settings
    store = client.app.state.runtime.snapshot_store
    if True:
        future_ts = NOW_MS + 3_600_000
        future = DerivativePoint(
            instrument_id="BTC-USDT-PERP",
            kind=DerivativeKind.MARK_PRICE,
            timeframe="1m",
            value=Decimal("99999"),
            ts_event=NOW_MS,
            ts_available=future_ts,
            precision=PrecisionTier.SECOND_VERIFIED,
            ts_ingested=NOW_MS,
            ts_committed=future_ts,
        )
        meta = SnapshotMeta(
            snapshot_id="future-sidecar",
            instrument_id="BTC-USDT-PERP",
            timeframes=["1m"],
            cutoff_ts=NOW_MS,
            created_ts=NOW_MS,
            mode=SnapshotMode.HISTORICAL_PIT,
            bar_counts={"1m": 0},
        )
        parquet = write_snapshot_parquet(settings.data_dir, "future-sidecar", [])
        sidecar = write_derivative_parquet(settings.data_dir, "future-sidecar", [future])
        store.register(meta, parquet, derivatives_path=sidecar)

    body = _derivatives(client, "future-sidecar").json()
    series = {item["kind"]: item for item in body["series"]}
    assert series["mark_price"]["points"] == []
    assert body["summary"]["mark_price"] is None
    assert read_derivative_points(settings.data_dir, sidecar)[0].ts_available > NOW_MS


def test_derivative_indicators_and_operator_return_backend_values(client) -> None:
    snapshot = _snapshot(client, "BTC-USDT-PERP", ["1m"])
    response = client.get(
        f"/api/snapshots/{snapshot['snapshot_id']}/indicators",
        params={"timeframe": "1m", "names": "FUNDING_QUANTILE,OI_DIVERGENCE", "limit": 300},
    )
    assert response.status_code == 200, response.text
    bundle = response.json()
    assert [item["name"] for item in bundle["series"]] == [
        "FUNDING_QUANTILE",
        "OI_DIVERGENCE",
    ]
    for item in bundle["series"]:
        assert item["pane"] == "sub"
        assert len(item["points"]) == 300
        assert item["summary"]["valid_points"] > 0
        if item["name"] == "OI_DIVERGENCE":
            assert isinstance(item["summary"]["latest"]["value"], str)

    response = client.post(
        "/api/operators/compute",
        json={
            "snapshot_id": snapshot["snapshot_id"],
            "timeframe": "1m",
            "operator": "NET_ODDS",
            "params": {"period": 20},
            "limit": 100,
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    series = response.json()
    assert series["name"] == "NET_ODDS"
    assert series["summary"]["latest"]["value"] is not None


def test_ws_payload_exposes_derivatives_without_touching_existing_types() -> None:
    tick = DerivativeTickEvent(
        instrument_id="BTC-USDT-PERP",
        mark_price=Decimal("101.5"),
        funding_rate=Decimal("0.0001"),
        next_funding_ts=NOW_MS + 8 * 3_600_000,
        open_interest=Decimal("12345.6"),
        ts_event=NOW_MS,
        ts_ingested=NOW_MS,
    )
    payload = _ws_payload(tick)
    assert payload["type"] == "derivatives"
    assert payload["data"]["mark_price"] == "101.5"
    assert payload["data"]["funding_rate"] == "0.0001"
    assert payload["data"]["next_funding_ts"] == NOW_MS + 8 * 3_600_000
    assert payload["data"]["open_interest"] == "12345.6"
    assert _wanted(tick, {("BTC-USDT-PERP", "1m")}) is True
    assert _wanted(tick, {("ETH-USDT-PERP", "1m")}) is False

    bar = KlineEvent(
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        bar=client_bar(),
        confirmed=True,
        revision=0,
    )
    assert _ws_payload(bar)["type"] == "bar"
    health = FeedHealthEvent(
        instrument_id="BTC-USDT-PERP",
        timeframe="1m",
        status=FeedHealthStatus.LIVE,
        ts=NOW_MS,
    )
    assert _ws_payload(health)["type"] == "health"
    assert _ws_payload(
        OrderBookEvent(
            instrument_id="BTC-USDT-PERP",
            bid_price=Decimal("1"),
            bid_size=Decimal("1"),
            ask_price=Decimal("2"),
            ask_size=Decimal("1"),
            ts_event=NOW_MS,
            ts_ingested=NOW_MS,
        )
    )["type"] == "book"


def client_bar():
    from talaria.foundation.bars import BarStatus, CompactBar

    return CompactBar(
        timestamp=NOW_MS - 60_000,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1"),
        turnover=Decimal("100.5"),
        status=BarStatus.NORMAL,
        ts_available=NOW_MS,
        precision=PrecisionTier.SECOND_VERIFIED,
    )
