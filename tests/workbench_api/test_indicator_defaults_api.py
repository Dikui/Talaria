"""指标默认值接口：工厂默认、用户覆盖、清除与 Bearer 保护。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
NOW_MS = 1_795_022_974_103


@pytest.fixture
def app(tmp_path: Path, fixture_dir: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()
    (dist_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    return create_app(
        AppSettings(
            data_dir=data_dir,
            dist_dir=dist_dir,
            fixture_dir=fixture_dir,
            token=TOKEN,
            now_ms=lambda: NOW_MS,
        )
    )


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def test_factory_defaults_align_with_pine_v6(client) -> None:
    response = client.get("/api/indicator-defaults")
    assert response.status_code == 200
    defaults = response.json()["defaults"]

    assert defaults["EMA"] == {"period": 9, "std_dev": None, "anchor": None}
    assert defaults["SMA"] == {"period": 9, "std_dev": None, "anchor": None}
    assert defaults["RSI"]["period"] == 14
    assert defaults["ATR"]["period"] == 14
    assert defaults["BOLL"] == {"period": 20, "std_dev": "2", "anchor": None}
    assert defaults["VWAP"] == {"period": 1, "std_dev": None, "anchor": "session"}


def test_override_clear_and_bearer_protection(client) -> None:
    assert client.put("/api/indicator-defaults/EMA", json={"period": 30}).status_code == 401

    updated = client.put(
        "/api/indicator-defaults/EMA", json={"period": 30}, headers=AUTH
    )
    assert updated.status_code == 200
    assert updated.json()["defaults"]["EMA"]["period"] == 30
    assert client.get("/api/indicator-defaults").json()["defaults"]["EMA"]["period"] == 30

    cleared = client.delete("/api/indicator-defaults/EMA", headers=AUTH)
    assert cleared.status_code == 200
    assert cleared.json()["defaults"]["EMA"]["period"] == 9


def test_override_rejects_invalid_or_non_vwap_anchor(client) -> None:
    ema_anchor = client.put(
        "/api/indicator-defaults/EMA",
        json={"period": 20, "anchor": "session"},
        headers=AUTH,
    )
    assert ema_anchor.status_code == 422

    vwap_anchor = client.put(
        "/api/indicator-defaults/VWAP",
        json={"period": 1, "anchor": "week"},
        headers=AUTH,
    )
    assert vwap_anchor.status_code == 422

    boll_period = client.put(
        "/api/indicator-defaults/BOLL",
        json={"period": 1, "std_dev": "2"},
        headers=AUTH,
    )
    assert boll_period.status_code == 422

    unknown = client.put(
        "/api/indicator-defaults/NOPE", json={"period": 2}, headers=AUTH
    )
    assert unknown.status_code == 422


def test_dsh_can_preflight_saving_indicator_default(client) -> None:
    origin = "http://127.0.0.1:8766"
    response = client.options(
        "/api/indicator-defaults/EMA",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "PUT" in response.headers["access-control-allow-methods"]


def test_concurrent_defaults_save_and_clear_preserve_other_indicators(tmp_path) -> None:
    from talaria.foundation.indicators import IndicatorParams
    from talaria.workbench_api.indicator_defaults import IndicatorDefaultsService
    from talaria.workbench_api.state_store import StateWriterQueue

    async def scenario() -> None:
        writer = StateWriterQueue(tmp_path)
        await writer.start()
        try:
            defaults = IndicatorDefaultsService(writer)
            await asyncio.gather(
                defaults.set("EMA", IndicatorParams(period=25)),
                defaults.set("ATR", IndicatorParams(period=17)),
            )
            result = await defaults.effective()
            assert (result["EMA"].period, result["ATR"].period) == (25, 17)

            await asyncio.gather(
                defaults.clear("EMA"),
                defaults.set("ATR", IndicatorParams(period=18)),
            )
            result = await defaults.effective()
            assert (result["EMA"].period, result["ATR"].period) == (9, 18)
        finally:
            await writer.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("name", "params"),
    [
        ("EMA", {"period": True}),
        ("EMA", {"period": "9"}),
        ("EMA", {"period": 9, "std_dev": "2"}),
        ("VWAP", {"period": 1, "std_dev": "2"}),
    ],
)
def test_defaults_reject_non_integer_period_and_irrelevant_params(
    client, name, params
) -> None:
    assert client.put(f"/api/indicator-defaults/{name}", json=params, headers=AUTH).status_code == 422
