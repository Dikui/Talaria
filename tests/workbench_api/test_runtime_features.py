from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.errors import FeatureDisabledError
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK, CryptoPerpPack
from talaria.packs.testing import FixtureConnector
from talaria.perception.raw_store import RawStore
from talaria.workbench_api.app import create_app
from talaria.workbench_api.runtime import build_runtime
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
REPLAY = REPO_ROOT / "tests" / "fixtures" / "realtime" / "demo_outage.jsonl"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": "Bearer test-token"}
BTC = "BTC-USDT-PERP"

pytestmark = pytest.mark.medium


def _settings(
    tmp_path: Path,
    features: FeatureToggles,
    *,
    replay: bool = True,
) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=FIXTURES,
        replay_events=REPLAY if replay else None,
        token="test-token",
        now_ms=lambda: NOW_MS,
        features=features,
    )


async def _assert_runtime(settings: AppSettings, check) -> None:
    runtime = await build_runtime(settings)
    try:
        check(runtime)
    finally:
        await runtime.aclose()


def _build_and_check(
    tmp_path: Path, features: FeatureToggles, check, *, replay: bool = True
) -> None:
    asyncio.run(_assert_runtime(_settings(tmp_path, features, replay=replay), check))


def test_every_feature_enabled_assembles_full_runtime(tmp_path):
    def check(runtime) -> None:
        assert runtime.supervisor is not None
        assert runtime.subscription_scheduler is not None
        assert runtime.hot_buffer is not None
        assert runtime.cold_store is not None
        assert runtime.alerts is not None
        assert runtime.scanner is not None
        assert runtime.require_alerts() is runtime.alerts
        assert runtime.require_scanner() is runtime.scanner

    _build_and_check(tmp_path, FeatureToggles(), check)


def test_market_data_disabled_skips_feed_engine(tmp_path):
    def check(runtime) -> None:
        assert runtime.supervisor is None
        assert runtime.subscription_scheduler is None
        assert runtime.hot_buffer is None
        assert runtime.cold_store is not None
        assert runtime.alerts is not None
        assert runtime.scanner is not None

    _build_and_check(tmp_path, FeatureToggles(market_data=False), check)


@pytest.fixture
def offline_market(tmp_path, fixture_dir, monkeypatch):
    # 用固定的历史响应模拟远端边界，旧实现会取回填数据并写入原始库。
    connector = FixtureConnector(fixture_dir)
    connector_calls = []

    def create_connector(self, *, base_url=None):
        connector_calls.append(base_url)
        return connector

    monkeypatch.setattr(CryptoPerpPack, "create_history_connector", create_connector)
    settings = replace(
        _settings(tmp_path, FeatureToggles(market_data=False), replay=False),
        fixture_dir=None,
        backfill_bars=2,
    )
    raw_store = RawStore(settings.data_dir, raw_archive_id=CRYPTO_PERP_PACK.raw_archive_id)
    local_bar = connector.fetch_bars(symbol="BTCUSDT", timeframe="1m", limit=3)[0]
    yield settings, raw_store, local_bar
    assert connector_calls == []


@pytest.mark.regression
@pytest.mark.parametrize("has_history", [True, False])
def test_market_data_disabled_snapshot_uses_only_local_history(offline_market, has_history):
    # platform/01: 缺历史时保留明确错误，已有历史可冻结成快照。
    settings, raw_store, local_bar = offline_market
    if has_history:
        raw_store.append("BTCUSDT", "1m", [local_bar])

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/snapshots", headers=AUTH,
            json={"instrument_id": BTC, "timeframes": ["1m"]},
        )

    assert raw_store.read("BTCUSDT", "1m") == ([local_bar] if has_history else [])
    if has_history:
        assert response.status_code == 200, response.text
        assert response.json()["bar_counts"] == {"1m": 1}
    else:
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "FEATURE_DISABLED"
        assert "缺少本地历史" in response.json()["message"]


@pytest.mark.regression
@pytest.mark.parametrize("has_history", [True, False])
def test_market_data_disabled_watchlist_quotes_use_only_local_history(offline_market, has_history):
    # platform/01: 无本地报价时使用自选接口既有的 UNAVAILABLE 状态。
    settings, raw_store, local_bar = offline_market
    if has_history:
        raw_store.append("BTCUSDT", "1m", [local_bar])

    with TestClient(create_app(settings)) as client:
        group = client.post("/api/watchlist/groups", headers=AUTH, json={"name": "自选"})
        group_id = group.json()["groups"][0]["group_id"]
        response = client.post(
            "/api/watchlist/items", headers=AUTH,
            json={"group_id": group_id, "instrument_id": BTC},
        )

    assert response.status_code == 200, response.text
    item = response.json()["groups"][0]["items"][0]
    assert item["quote_source"] == ("SNAPSHOT" if has_history else "UNAVAILABLE")
    assert item["latest_price"] == (str(local_bar.close) if has_history else None)
    assert item["quote_ts"] == (local_bar.ts_event if has_history else None)
    assert raw_store.read("BTCUSDT", "1m") == ([local_bar] if has_history else [])


def test_cold_store_disabled_skips_cold_store(tmp_path):
    def check(runtime) -> None:
        assert runtime.cold_store is None
        assert runtime.supervisor is not None
        assert runtime.alerts is not None
        assert runtime.scanner is not None

    _build_and_check(tmp_path, FeatureToggles(cold_store=False), check)
    # 关闭冷库写入后，连清单库都不应创建。
    assert not (tmp_path / "data" / "data" / "data_manifest.sqlite").exists()


def test_alerts_disabled_skips_alert_engine(tmp_path):
    def check(runtime) -> None:
        assert runtime.alerts is None
        assert runtime.scanner is not None
        assert runtime.watchlist is not None
        try:
            runtime.require_alerts()
        except FeatureDisabledError as exc:
            assert "service.json" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("alerts 关闭时 require_alerts 应抛出 FEATURE_DISABLED")

    _build_and_check(tmp_path, FeatureToggles(alerts=False), check)


def test_scanner_disabled_skips_scanner_engine(tmp_path):
    def check(runtime) -> None:
        assert runtime.scanner is None
        assert runtime.alerts is not None
        assert runtime.briefings is not None
        try:
            runtime.require_scanner()
        except FeatureDisabledError as exc:
            assert "service.json" in str(exc)
        else:  # pragma: no cover - 失败路径
            raise AssertionError("scanner 关闭时 require_scanner 应抛出 FEATURE_DISABLED")

    _build_and_check(tmp_path, FeatureToggles(scanner=False), check)


def test_all_features_disabled_still_builds_and_closes_cleanly(tmp_path):
    features = FeatureToggles(
        market_data=False, cold_store=False, alerts=False, scanner=False
    )

    def check(runtime) -> None:
        assert runtime.supervisor is None
        assert runtime.hot_buffer is None
        assert runtime.cold_store is None
        assert runtime.alerts is None
        assert runtime.scanner is None
        assert runtime.state_writer is not None
        assert runtime.usage is not None

    _build_and_check(tmp_path, features, check, replay=False)


def test_disabled_features_fail_closed_through_http(tmp_path):
    settings = _settings(
        tmp_path, FeatureToggles(alerts=False, scanner=False), replay=False
    )
    app = create_app(settings)
    headers = {"Authorization": "Bearer test-token"}

    with TestClient(app) as client:
        alerts = client.get("/api/alerts")
        assert alerts.status_code == 409, alerts.text
        assert alerts.json()["code"] == "FEATURE_DISABLED"
        assert "预警" in alerts.json()["message"]

        scan = client.post("/api/scanner/scan", headers=headers)
        assert scan.status_code == 409, scan.text
        assert scan.json()["code"] == "FEATURE_DISABLED"
        assert "扫描" in scan.json()["message"]

        # 未关闭的服务仍可用。
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/scanner/briefings").status_code == 200
