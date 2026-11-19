"""扫描 API：查询、手动扫描、WS 推送、Bearer 写保护与焦点分组。"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.bars import CompactBar
from talaria.foundation.types import PrecisionTier
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "scanner-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}
INSTRUMENT = "BTC-USDT-PERP"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bybit"


def _settings(tmp_path: Path, dist: Path) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=dist,
        fixture_dir=FIXTURES,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(
        "<!doctype html><title>Talaria</title>", encoding="utf-8"
    )
    return dist


@pytest.fixture
def app(tmp_path: Path, dist_dir: Path):
    return create_app(_settings(tmp_path, dist_dir))


@pytest.fixture
def client(app):
    with TestClient(app) as instance:
        yield instance


def _spike_bar() -> CompactBar:
    rows = json.loads(
        (FIXTURES / "BTCUSDT" / "1m.json").read_text(encoding="utf-8")
    )["result"]["list"]
    latest = max(int(row[0]) for row in rows)
    timestamp = latest + 60_000
    return CompactBar(
        timestamp=timestamp,
        open=Decimal("100"),
        high=Decimal("130"),
        low=Decimal("99"),
        close=Decimal("128"),
        volume=Decimal("999999"),
        turnover=Decimal("99999999"),
        status="NORMAL",  # type: ignore[arg-type]
        ts_available=timestamp + 60_000,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


def _seed_spike(client: TestClient) -> None:
    scanner = client.app.state.scanner_service
    scanner.seed_bars(INSTRUMENT, "1m", [_spike_bar()])


def test_scanner_endpoints_are_public_for_reads_and_require_bearer_for_scan(
    client: TestClient,
) -> None:
    assert client.get("/api/scanner/triggers").status_code == 200
    assert client.get("/api/scanner/focus").status_code == 200
    assert client.post("/api/scanner/scan", json={}).status_code == 401

    response = client.post("/api/scanner/scan", json={}, headers=AUTH)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["scanned_instruments"] == [
        "1000BONK-USDT-PERP",
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
        "SOL-USDT-PERP",
    ]
    assert isinstance(body["triggers"], list)
    assert body["generated_ts"] == NOW_MS

    assert client.post(
        "/api/scanner/scan",
        json={"instrument_ids": ["NOPE-USDT-PERP"]},
        headers=AUTH,
    ).status_code == 422


def test_scan_returns_trigger_focus_group_and_ws_message(client: TestClient) -> None:
    _seed_spike(client)
    with client.websocket_connect("/ws/desk") as websocket:
        hello = websocket.receive_json()
        assert hello["type"] == "hello"

        response = client.post(
            "/api/scanner/scan",
            json={"instrument_ids": [INSTRUMENT]},
            headers=AUTH,
        )
        assert response.status_code == 200, response.text
        triggers = response.json()["triggers"]
        assert triggers
        assert triggers[0]["instrument_id"] == INSTRUMENT
        assert triggers[0]["snapshot_id"]
        assert triggers[0]["timeframe"] == "1m"

        scanner_message = None
        for _ in range(4):
            message = websocket.receive_json()
            if message["type"] == "scanner":
                scanner_message = message
                break
        assert scanner_message is not None
        assert scanner_message["data"]["trigger_id"] == triggers[0]["trigger_id"]

    listed = client.get(
        "/api/scanner/triggers",
        params={"instrument_id": INSTRUMENT, "limit": 10},
    )
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["total"] >= 1
    assert all(
        item["instrument_id"] == INSTRUMENT for item in payload["triggers"]
    )

    focus = client.get("/api/scanner/focus").json()
    assert focus["group_id"] == "scanner-focus"
    assert INSTRUMENT in focus["instrument_ids"]

    watchlist = client.get("/api/watchlist").json()
    system = [
        group for group in watchlist["groups"] if group["group_id"] == "scanner-focus"
    ]
    assert len(system) == 1
    assert system[0]["kind"] == "SCANNER_FOCUS"
    assert system[0]["is_system"] is True
    assert INSTRUMENT in [item["instrument_id"] for item in system[0]["items"]]


def test_repeat_scan_on_same_bar_does_not_duplicate_triggers(
    client: TestClient,
) -> None:
    first = client.post(
        "/api/scanner/scan",
        json={"instrument_ids": [INSTRUMENT]},
        headers=AUTH,
    )
    assert first.status_code == 200
    second = client.post(
        "/api/scanner/scan",
        json={"instrument_ids": [INSTRUMENT]},
        headers=AUTH,
    )
    assert second.status_code == 200
    assert second.json()["triggers"] == []
