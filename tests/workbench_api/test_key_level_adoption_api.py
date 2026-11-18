"""关键位一键采纳与质量反馈的 HTTP 契约。"""

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
TIMEFRAME = "1m"


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
def client(tmp_path, fixture_dir, dist_dir):
    app = create_app(_settings(tmp_path / "data", fixture_dir, dist_dir))
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


def _levels(client: TestClient, snapshot_id: str) -> list[dict]:
    response = client.get(
        f"/api/snapshots/{snapshot_id}/key-levels",
        params={"timeframe": TIMEFRAME},
    )
    assert response.status_code == 200, response.text
    return response.json()["levels"]


def _adopt(client: TestClient, level_id: str, snapshot_id: str):
    return client.post(
        f"/api/key-levels/{level_id}/adopt",
        json={"snapshot_id": snapshot_id, "timeframe": TIMEFRAME},
        headers=AUTH,
    )


def test_adopt_returns_human_drawing_and_records_feedback(client) -> None:
    meta = _snapshot(client)
    levels = _levels(client, meta["snapshot_id"])
    priced = next(level for level in levels if level["price"] is not None)
    zoned = next(level for level in levels if level["price"] is None)

    response = _adopt(client, priced["level_id"], meta["snapshot_id"])
    assert response.status_code == 200, response.text
    drawing = response.json()
    assert drawing["kind"] == "HORIZONTAL_LINE"
    assert drawing["instrument_id"] == INSTRUMENT
    assert drawing["visible_timeframes"] == [TIMEFRAME]
    assert drawing["points"][0]["price"] == priced["price"]
    assert priced["level_id"] in drawing["text"]
    assert drawing["text"].startswith("来源关键位")

    listing = client.get(
        "/api/drawings",
        params={"instrument_id": INSTRUMENT, "timeframe": TIMEFRAME},
    )
    assert listing.status_code == 200
    assert [item["drawing_id"] for item in listing.json()["drawings"]] == [
        drawing["drawing_id"]
    ]

    zone = _adopt(client, zoned["level_id"], meta["snapshot_id"])
    assert zone.status_code == 200, zone.text
    rectangle = zone.json()
    assert rectangle["kind"] == "RECTANGLE"
    assert [point["price"] for point in rectangle["points"]] == [
        zoned["price_low"],
        zoned["price_high"],
    ]

    feedback = client.get(
        "/api/key-levels/feedback", params={"instrument_id": INSTRUMENT}
    )
    assert feedback.status_code == 200
    payload = feedback.json()
    assert payload["instrument_id"] == INSTRUMENT
    assert set(payload["adopted_level_ids"]) == {
        priced["level_id"],
        zoned["level_id"],
    }
    assert payload["ignored_level_ids"] == []


def test_adoption_and_ignores_survive_restart(
    tmp_path, fixture_dir, dist_dir
) -> None:
    settings = _settings(tmp_path / "data", fixture_dir, dist_dir)
    with TestClient(create_app(settings)) as first:
        meta = _snapshot(first)
        level = _levels(first, meta["snapshot_id"])[0]
        drawing = _adopt(first, level["level_id"], meta["snapshot_id"]).json()
        ignored = first.put(
            "/api/key-levels/ignored",
            json={
                "instrument_id": INSTRUMENT,
                "level_id": "kl_ignored_0001",
                "ignored": True,
            },
            headers=AUTH,
        )
        assert ignored.status_code == 200

    with TestClient(create_app(settings)) as restarted:
        feedback = restarted.get(
            "/api/key-levels/feedback", params={"instrument_id": INSTRUMENT}
        )
        assert feedback.status_code == 200
        payload = feedback.json()
        assert payload["adopted_level_ids"] == [level["level_id"]]
        assert payload["ignored_level_ids"] == ["kl_ignored_0001"]
        fetched = restarted.get(f"/api/drawings/{drawing['drawing_id']}")
        assert fetched.status_code == 200
        assert fetched.json()["points"][0]["price"] == drawing["points"][0]["price"]


def test_adopt_rejects_unknown_level_and_missing_token(client) -> None:
    meta = _snapshot(client)
    unknown = _adopt(client, "kl_missing_level", meta["snapshot_id"])
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "NOT_FOUND"

    level = _levels(client, meta["snapshot_id"])[0]
    unauthorized = client.post(
        f"/api/key-levels/{level['level_id']}/adopt",
        json={"snapshot_id": meta["snapshot_id"], "timeframe": TIMEFRAME},
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "UNAUTHORIZED"

    bad_body = client.post(
        f"/api/key-levels/{level['level_id']}/adopt",
        json={"snapshot_id": meta["snapshot_id"], "timeframe": "7m"},
        headers=AUTH,
    )
    assert bad_body.status_code == 422
    assert bad_body.json()["code"] == "VALIDATION_ERROR"


def test_feedback_is_read_only_without_token(client) -> None:
    response = client.get(
        "/api/key-levels/feedback", params={"instrument_id": INSTRUMENT}
    )
    assert response.status_code == 200
    assert response.json() == {
        "instrument_id": INSTRUMENT,
        "adopted_level_ids": [],
        "ignored_level_ids": [],
    }
