"""关键位读取、WS 投影与忽略反馈的 API 契约。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTRUMENT = "BTC-USDT-PERP"


def _settings(data_dir: Path, fixture_dir: Path, dist: Path) -> AppSettings:
    return AppSettings(
        data_dir=data_dir,
        dist_dir=dist,
        fixture_dir=fixture_dir,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return dist


@pytest.fixture
def app(tmp_path, fixture_dir, dist_dir):
    return create_app(_settings(tmp_path / "data", fixture_dir, dist_dir))


@pytest.fixture
def client(app):
    with TestClient(app) as instance:
        yield instance


def _snapshot(client: TestClient) -> dict:
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": INSTRUMENT, "timeframes": ["1m", "5m"]},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _levels(client: TestClient, snapshot_id: str, **params) -> dict:
    query = {"timeframe": "1m", **params}
    response = client.get(
        f"/api/snapshots/{snapshot_id}/key-levels", params=query
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_key_levels_are_pit_filtered_and_reproducible(client) -> None:
    meta = _snapshot(client)
    first = _levels(client, meta["snapshot_id"])
    second = _levels(client, meta["snapshot_id"])

    assert first["snapshot_id"] == meta["snapshot_id"]
    assert first["instrument_id"] == INSTRUMENT
    assert first["timeframe"] == "1m"
    assert first["cutoff_ts"] == meta["cutoff_ts"]
    assert first["method_version"] == "key-levels@2"
    assert first["levels"]
    assert [item["level_id"] for item in first["levels"]] == [
        item["level_id"] for item in second["levels"]
    ]
    for level in first["levels"]:
        assert level["ts_available"] <= meta["cutoff_ts"]
        assert level["formed_ts"] <= level["ts_available"]
        assert isinstance(level["level_id"], str)
        if level["price"] is not None:
            assert isinstance(level["price"], str)
        else:
            assert isinstance(level["price_low"], str)
            assert isinstance(level["price_high"], str)


def test_key_levels_filter_by_category_and_strength(client) -> None:
    meta = _snapshot(client)
    structure = _levels(client, meta["snapshot_id"], categories="STRUCTURE")
    assert structure["levels"]
    assert {item["category"] for item in structure["levels"]} == {"STRUCTURE"}

    high = _levels(client, meta["snapshot_id"], min_strength=0.96)
    assert high["levels"] == []

    invalid = client.get(
        f"/api/snapshots/{meta['snapshot_id']}/key-levels",
        params={"timeframe": "1m", "categories": "UNKNOWN"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_key_levels_sort_by_price(client) -> None:
    meta = _snapshot(client)
    payload = _levels(client, meta["snapshot_id"], order="price_desc")
    prices = [
        float(item["price"])
        if item["price"] is not None
        else (float(item["price_low"]) + float(item["price_high"])) / 2
        for item in payload["levels"]
    ]
    assert prices == sorted(prices, reverse=True)


def test_unknown_snapshot_returns_404(client) -> None:
    response = client.get(
        "/api/snapshots/missing/key-levels", params={"timeframe": "1m"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SNAPSHOT_NOT_FOUND"


@pytest.mark.spec
def test_key_levels_ws_projection_replays_latest_cutoff(client) -> None:
    # analysis/01：后冻结的历史快照不覆盖图表的最新版本。
    meta = _snapshot(client)
    older = client.post(
        "/api/snapshots",
        json={
            "instrument_id": INSTRUMENT, "timeframes": ["1m"],
            "cutoff_ts": meta["cutoff_ts"] - 60_000,
        },
        headers=AUTH,
    )
    assert older.status_code == 200, older.text
    with client.websocket_connect("/ws/desk") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"
        websocket.send_json(
            {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
        )
        message = websocket.receive_json()
        assert message["type"] == "levels"
        payload = message["data"]
        assert payload["snapshot_id"] == meta["snapshot_id"]
        assert payload["cutoff_ts"] == meta["cutoff_ts"]
        assert payload["levels"]


def test_key_levels_ws_projection_broadcasts_new_snapshot(client) -> None:
    with client.websocket_connect("/ws/desk") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_json(
            {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
        )
        meta = _snapshot(client)
        message = websocket.receive_json()
        while message["type"] != "levels":
            message = websocket.receive_json()
        assert message["data"]["snapshot_id"] == meta["snapshot_id"]


def test_future_cutoff_is_readable_but_never_projected_to_the_chart(client) -> None:
    current = _snapshot(client)
    subscription = {"op": "subscribe", "instrument_id": INSTRUMENT, "timeframe": "1m"}
    with client.websocket_connect("/ws/desk") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_json(subscription)
        assert websocket.receive_json()["data"]["snapshot_id"] == current["snapshot_id"]

        future = client.post(
            "/api/snapshots",
            json={"instrument_id": INSTRUMENT, "timeframes": ["1m"], "cutoff_ts": NOW_MS + 1},
            headers=AUTH,
        )
        assert future.status_code == 200, future.text
        assert _levels(client, future.json()["snapshot_id"])["cutoff_ts"] == NOW_MS + 1

        replacement = _snapshot(client)
        message = websocket.receive_json()
        assert message["data"]["snapshot_id"] == replacement["snapshot_id"]

    with client.websocket_connect("/ws/desk") as websocket:
        assert websocket.receive_json()["type"] == "hello"
        websocket.send_json(subscription)
        assert websocket.receive_json()["data"]["snapshot_id"] == replacement["snapshot_id"]


def test_ignored_key_levels_persist_across_restart(
    tmp_path, fixture_dir, dist_dir
) -> None:
    settings = _settings(tmp_path / "data", fixture_dir, dist_dir)
    level_id = "kl_test_level_0001"
    with TestClient(create_app(settings)) as first:
        response = first.put(
            "/api/key-levels/ignored",
            json={"instrument_id": INSTRUMENT, "level_id": level_id, "ignored": True},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        assert response.json()["entries"] == [
            {
                "instrument_id": INSTRUMENT,
                "level_id": level_id,
                "ignored": True,
                "updated_ts": NOW_MS,
            }
        ]

    with TestClient(create_app(settings)) as restarted:
        listing = restarted.get(
            "/api/key-levels/ignored", params={"instrument_id": INSTRUMENT}
        )
        assert listing.status_code == 200
        assert listing.json()["entries"][0]["level_id"] == level_id
        assert listing.json()["entries"][0]["ignored"] is True


def test_ignore_write_requires_bearer_token(client) -> None:
    response = client.put(
        "/api/key-levels/ignored",
        json={"instrument_id": INSTRUMENT, "level_id": "kl_x", "ignored": True},
    )
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


def test_ignore_list_is_read_only_without_token(client) -> None:
    response = client.get(
        "/api/key-levels/ignored", params={"instrument_id": INSTRUMENT}
    )
    assert response.status_code == 200
    assert response.json() == {"instrument_id": INSTRUMENT, "entries": []}


def test_ignore_unset_keeps_audit_row(client) -> None:
    body = {"instrument_id": INSTRUMENT, "level_id": "kl_toggle", "ignored": True}
    assert client.put("/api/key-levels/ignored", json=body, headers=AUTH).status_code == 200
    body["ignored"] = False
    response = client.put("/api/key-levels/ignored", json=body, headers=AUTH)
    assert response.status_code == 200
    assert response.json()["entries"][0]["ignored"] is False


def test_key_levels_include_observed_volume_and_estimated_liquidation(client) -> None:
    meta = _snapshot(client)
    payload = _levels(client, meta["snapshot_id"], vwap_anchor="snapshot")

    by_kind = {item["kind"] for item in payload["levels"]}
    assert {"POC", "VAH", "VAL", "ANCHORED_VWAP"} <= by_kind
    volume = [
        item for item in payload["levels"] if item["category"] == "VOLUME"
    ]
    assert volume
    assert {item["evidence_class"] for item in volume} == {"OBSERVED"}
    assert all(item["method_version"] == "key-levels@2" for item in volume)

    liquidation = [
        item for item in payload["levels"] if item["category"] == "LIQUIDATION"
    ]
    assert liquidation
    assert {item["evidence_class"] for item in liquidation} == {"ESTIMATED"}
    assert {item["kind"] for item in liquidation} == {"LIQUIDATION_ZONE"}
    assert payload["notes"] == []
    params = payload["parameters"]
    assert params["leverage_tiers"] == "5,10,25,50,100"
    assert params["value_area_ratio"] == "0.7"


def test_evidence_class_filter_splits_observed_and_estimated(client) -> None:
    meta = _snapshot(client)
    observed = _levels(client, meta["snapshot_id"], evidence_class="OBSERVED")
    assert observed["levels"]
    assert {item["evidence_class"] for item in observed["levels"]} == {"OBSERVED"}
    assert not [
        item for item in observed["levels"] if item["category"] == "LIQUIDATION"
    ]
    assert observed["notes"] == []

    estimated = _levels(client, meta["snapshot_id"], evidence_class="ESTIMATED")
    assert estimated["levels"]
    assert {item["evidence_class"] for item in estimated["levels"]} == {"ESTIMATED"}
    assert {item["category"] for item in estimated["levels"]} == {"LIQUIDATION"}

    invalid = client.get(
        f"/api/snapshots/{meta['snapshot_id']}/key-levels",
        params={"timeframe": "1m", "evidence_class": "MAYBE"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_vwap_anchor_is_configurable(client) -> None:
    meta = _snapshot(client)
    # The 1m fixture starts and ends on the same UTC day. Only 5m crosses midnight.
    same_day = _levels(client, meta["snapshot_id"], vwap_anchor="session")
    assert all(item["kind"] != "ANCHORED_VWAP" for item in same_day["levels"])

    session = _levels(client, meta["snapshot_id"], timeframe="5m", vwap_anchor="session")
    session_vwap = [
        item for item in session["levels"] if item["kind"] == "ANCHORED_VWAP"
    ]
    assert session_vwap
    anchor_ts = session_vwap[0]["formed_ts"]
    assert anchor_ts == 1_793_577_600_000  # 2026-11-02 00:00 UTC

    payload = _levels(
        client,
        meta["snapshot_id"],
        timeframe="5m",
        vwap_anchor="timestamp",
        vwap_anchor_ts=anchor_ts,
    )
    vwap = [item for item in payload["levels"] if item["kind"] == "ANCHORED_VWAP"]
    assert vwap
    assert all(item["formed_ts"] == anchor_ts for item in vwap)
    centers = [
        item for item in vwap if item["parameters"]["band_sigma"] == "0"
    ]
    assert len(centers) == 1

    missing = client.get(
        f"/api/snapshots/{meta['snapshot_id']}/key-levels",
        params={"timeframe": "1m", "vwap_anchor": "timestamp"},
    )
    assert missing.status_code == 422
    assert missing.json()["code"] == "VALIDATION_ERROR"

    invalid = client.get(
        f"/api/snapshots/{meta['snapshot_id']}/key-levels",
        params={"timeframe": "1m", "vwap_anchor": "weekly"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "VALIDATION_ERROR"


def test_missing_oi_returns_empty_liquidation_with_reason(client) -> None:
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": "1000BONK-USDT-PERP", "timeframes": ["1m"]},
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    meta = response.json()
    payload = _levels(client, meta["snapshot_id"])
    assert not [
        item for item in payload["levels"] if item["category"] == "LIQUIDATION"
    ]
    assert payload["notes"]
    assert "没有可用的持仓量数据" in payload["notes"][0]
