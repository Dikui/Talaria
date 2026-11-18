"""人工画线 API：CRUD、版本链、软删除、可见范围、鉴权与 get_chart_view。"""

from __future__ import annotations

import json
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

#: 1h 周期向下取整的起点，便于构造稳定窗口。
HOUR = 3_600_000
T0 = 1_795_020_000_000
T1 = T0 + HOUR
T2 = T0 + 2 * HOUR


def _settings(tmp_path: Path, dist: Path, fixture_dir: Path) -> AppSettings:
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
def app(tmp_path, fixture_dir, dist_dir):
    return create_app(_settings(tmp_path, dist_dir, fixture_dir))


@pytest.fixture
def client(app):
    with TestClient(app) as instance:
        yield instance


def _create_body(**overrides):
    body = {
        "instrument_id": INSTRUMENT,
        "kind": "SEGMENT",
        "visible_timeframes": ["1h"],
        "points": [
            {"ts": T0, "price": "101.10"},
            {"ts": T1, "price": "105.25"},
        ],
        "text": "",
    }
    body.update(overrides)
    return body


def _create(client: TestClient, **overrides) -> dict:
    response = client.post("/api/drawings", json=_create_body(**overrides), headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def _list(client: TestClient, timeframe: str = "1h", **params) -> dict:
    query = {"instrument_id": INSTRUMENT, "timeframe": timeframe, **params}
    response = client.get("/api/drawings", params=query)
    assert response.status_code == 200, response.text
    return response.json()


def test_create_returns_version_one_and_decimal_strings(client) -> None:
    payload = _create(client)
    assert payload["drawing_id"].startswith("drw_")
    assert payload["version"] == 1
    assert payload["deleted_at"] is None
    assert payload["created_ts"] == NOW_MS
    assert payload["updated_ts"] == NOW_MS
    # 价格必须是 JSON 字符串，不能是 number。
    assert payload["points"][0]["price"] == "101.10"
    assert isinstance(payload["points"][0]["price"], str)
    assert json.loads(json.dumps(payload))["points"][1]["price"] == "105.25"


def test_patch_moves_points_and_appends_version(client) -> None:
    created = _create(client)
    drawing_id = created["drawing_id"]
    response = client.patch(
        f"/api/drawings/{drawing_id}",
        json={
            "points": [
                {"ts": T0 + 60_000, "price": "100.00"},
                {"ts": T2, "price": "110.00"},
            ],
            "style": {"color": "#ff8800", "line_width": 2},
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    patched = response.json()
    assert patched["version"] == 2
    assert patched["created_ts"] == created["created_ts"]
    assert patched["points"][0]["price"] == "100.00"

    version_one = client.get(
        f"/api/drawings/{drawing_id}", params={"version": 1}
    )
    assert version_one.status_code == 200
    assert version_one.json()["points"][1]["price"] == "105.25"
    assert version_one.json()["version"] == 1

    chain = client.get(f"/api/drawings/{drawing_id}/versions")
    assert chain.status_code == 200
    versions = chain.json()["versions"]
    assert [item["version"] for item in versions] == [1, 2]
    assert versions[0]["points"][0]["price"] == "101.10"
    assert versions[1]["style"]["color"] == "#ff8800"


def test_soft_delete_hides_listing_and_restore_revives(client) -> None:
    created = _create(client)
    drawing_id = created["drawing_id"]
    response = client.delete(f"/api/drawings/{drawing_id}", headers=AUTH)
    assert response.status_code == 200, response.text
    deleted = response.json()
    assert deleted["version"] == 2
    assert deleted["deleted_at"] == NOW_MS
    assert _list(client)["drawings"] == []

    restored = client.post(
        f"/api/drawings/{drawing_id}/restore", headers=AUTH
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["version"] == 3
    assert restored.json()["deleted_at"] is None
    assert [item["drawing_id"] for item in _list(client)["drawings"]] == [drawing_id]

    chain = client.get(f"/api/drawings/{drawing_id}/versions").json()["versions"]
    assert [item["version"] for item in chain] == [1, 2, 3]
    assert chain[1]["deleted_at"] == NOW_MS
    assert chain[2]["deleted_at"] is None


def test_clear_by_instrument_soft_deletes_only_that_instrument(client) -> None:
    first = _create(client)
    _create(client, instrument_id="ETH-USDT-PERP")
    response = client.delete(
        "/api/drawings", params={"instrument_id": INSTRUMENT}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"instrument_id": INSTRUMENT, "cleared": 1}
    assert all(
        item["instrument_id"] == "ETH-USDT-PERP"
        for item in _list(client, instrument_id="ETH-USDT-PERP")["drawings"]
    )
    assert client.get(f"/api/drawings/{first['drawing_id']}").json()["deleted_at"] is not None


def test_visibility_filters_time_window_and_timeframe(client) -> None:
    in_window = _create(client)
    _create(
        client,
        visible_timeframes=["1h"],
        points=[{"ts": T2 + 10 * HOUR, "price": "99.00"}, {"ts": T2 + 11 * HOUR, "price": "100.00"}],
    )
    _create(client, visible_timeframes=["5m"])

    payload = _list(client, from_ts=T0, to_ts=T1)
    assert [item["drawing_id"] for item in payload["drawings"]] == [in_window["drawing_id"]]

    # 5m 周期只能看到声明了 5m 的画线。
    other = _list(client, timeframe="5m", from_ts=T0, to_ts=T1)
    assert [item["visible_timeframes"] for item in other["drawings"]] == [["5m"]]


def test_empty_visible_timeframes_means_all_timeframes(client) -> None:
    _create(client, visible_timeframes=[])
    for timeframe in ("1m", "5m", "1h", "1d"):
        drawings = _list(client, timeframe=timeframe)["drawings"]
        assert len(drawings) == 1
        assert drawings[0]["visible_timeframes"] == []


def test_horizontal_line_is_always_visible_in_time(client) -> None:
    line = _create(
        client,
        kind="HORIZONTAL_LINE",
        points=[{"ts": T0, "price": "101.10"}],
    )
    payload = _list(client, from_ts=T2 + HOUR, to_ts=T2 + 2 * HOUR)
    assert [item["drawing_id"] for item in payload["drawings"]] == [line["drawing_id"]]


def test_get_chart_view_returns_window_and_price_filtered_drawings(client) -> None:
    inside = _create(client)
    _create(
        client,
        kind="HORIZONTAL_LINE",
        points=[{"ts": T0, "price": "1000.00"}],
    )
    response = client.get(
        "/api/chart-view",
        params={
            "instrument_id": INSTRUMENT,
            "timeframe": "1h",
            "from_ts": T0,
            "to_ts": T1,
            "price_min": "100",
            "price_max": "110",
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["instrument"]["instrument_id"] == INSTRUMENT
    assert payload["instrument"]["tick_size"] == "0.1"
    assert payload["timeframe"] == "1h"
    assert payload["window"] == {
        "from_ts": T0,
        "to_ts": T1,
        "price_min": "100",
        "price_max": "110",
    }
    assert [item["drawing_id"] for item in payload["drawings"]] == [inside["drawing_id"]]
    assert isinstance(payload["window"]["price_min"], str)


def test_get_chart_view_rejects_inverted_price_window(client) -> None:
    response = client.get(
        "/api/chart-view",
        params={
            "instrument_id": INSTRUMENT,
            "timeframe": "1h",
            "from_ts": T0,
            "to_ts": T1,
            "price_min": "110",
            "price_max": "100",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", "/api/drawings", _create_body()),
        ("patch", "/api/drawings/drw_x", {"text": "x"}),
        ("delete", "/api/drawings/drw_x", None),
        ("post", "/api/drawings/drw_x/restore", None),
        ("delete", "/api/drawings?instrument_id=BTC-USDT-PERP", None),
    ],
)
def test_write_endpoints_require_bearer_token(client, method, path, body) -> None:
    response = getattr(client, method)(path, **({"json": body} if body else {}))
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"

    wrong = getattr(client, method)(
        path,
        headers={"Authorization": "Bearer wrong-token"},
        **({"json": body} if body else {}),
    )
    assert wrong.status_code == 401


def test_read_endpoints_do_not_require_token(client) -> None:
    assert client.get("/api/drawings", params={"instrument_id": INSTRUMENT, "timeframe": "1h"}).status_code == 200


def test_validation_and_not_found_mapping(client) -> None:
    bad_points = client.post(
        "/api/drawings",
        json=_create_body(kind="PARALLEL_CHANNEL", points=[{"ts": T0, "price": "1"}]),
        headers=AUTH,
    )
    assert bad_points.status_code == 422

    unknown_instrument = client.post(
        "/api/drawings", json=_create_body(instrument_id="NOPE"), headers=AUTH
    )
    assert unknown_instrument.status_code == 404
    assert unknown_instrument.json()["code"] == "INSTRUMENT_NOT_FOUND"

    bad_timeframe = client.get(
        "/api/drawings", params={"instrument_id": INSTRUMENT, "timeframe": "7m"}
    )
    assert bad_timeframe.status_code == 422

    assert client.get("/api/drawings/drw_missing").status_code == 404
    assert client.patch(
        "/api/drawings/drw_missing", json={"text": "x"}, headers=AUTH
    ).status_code == 404
    assert client.patch(
        "/api/drawings/drw_missing", json={}, headers=AUTH
    ).status_code == 422

    created = _create(client)
    client.delete(f"/api/drawings/{created['drawing_id']}", headers=AUTH)
    patched_deleted = client.patch(
        f"/api/drawings/{created['drawing_id']}", json={"text": "x"}, headers=AUTH
    )
    assert patched_deleted.status_code == 404


def test_drawings_persist_across_app_restart(tmp_path, fixture_dir, dist_dir) -> None:
    with TestClient(create_app(_settings(tmp_path, dist_dir, fixture_dir))) as first:
        created = _create(first)
    with TestClient(create_app(_settings(tmp_path, dist_dir, fixture_dir))) as second:
        payload = second.get(f"/api/drawings/{created['drawing_id']}").json()
        assert payload["version"] == 1
        assert payload["points"][0]["price"] == "101.10"
        listed = _list(second)["drawings"]
        assert [item["drawing_id"] for item in listed] == [created["drawing_id"]]
