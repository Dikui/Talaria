"""看盘 API：鉴权、健康检查、快照、分页、错误映射与静态托管。"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.errors import FeedUnavailableError
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.cold_store import ColdReader, ColdStore
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "test-token"
NOW_MS = 1_795_022_974_103
AUTH = {"Authorization": f"Bearer {TOKEN}"}


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


def _create(client: TestClient, **overrides):
    body = {"instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"]}
    body.update(overrides)
    return client.post("/api/snapshots", json=body, headers=AUTH)


def test_health_reports_version_and_fixture_mode(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "version": "0.0.2", "feed_mode": "fixture"}


def test_collaboration_reports_solo_without_host(client) -> None:
    payload = client.get("/api/collaboration").json()
    assert payload["mode"] == "solo"
    assert payload["host"] == "disabled"
    assert "单机模式" in payload["message"]


def test_collaboration_uses_injected_host_status(
    tmp_path, fixture_dir, dist_dir
) -> None:
    def status() -> dict[str, object]:
        return {"mode": "dual", "host": "offline", "message": "快通道正常"}

    app = create_app(
        _app_settings(tmp_path, fixture_dir, dist=dist_dir),
        collaboration_status=status,
    )
    with TestClient(app) as client:
        payload = client.get("/api/collaboration").json()
    assert payload == {"mode": "dual", "host": "offline", "message": "快通道正常"}


def test_instruments_returns_four_specs_with_decimal_strings(client) -> None:
    response = client.get("/api/instruments")
    assert response.status_code == 200
    payload = response.json()
    assert {item["instrument_id"] for item in payload} == {
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
        "SOL-USDT-PERP",
        "1000BONK-USDT-PERP",
    }
    assert payload[0]["tick_size"] == "0.1"
    assert isinstance(payload[0]["tick_size"], str)


def test_write_requires_bearer_token(client) -> None:
    payload = {"instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"]}
    missing = client.post("/api/snapshots", json=payload)
    assert missing.status_code == 401
    assert missing.json()["code"] == "UNAUTHORIZED"

    wrong = client.post(
        "/api/snapshots", json=payload, headers={"Authorization": "Bearer wrong"}
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "UNAUTHORIZED"


def test_cookie_alone_does_not_authorize_writes(client) -> None:
    client.cookies.set("talaria_token", TOKEN)
    response = client.post(
        "/api/snapshots",
        json={"instrument_id": "BTC-USDT-PERP", "timeframes": ["1m"]},
    )
    assert response.status_code == 401


def test_desk_sets_readable_strict_cookie(client) -> None:
    response = client.get("/desk")
    assert response.status_code == 200
    set_cookie = response.headers["set-cookie"].lower()
    assert "talaria_token=" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "httponly" not in set_cookie
    assert response.cookies.get("talaria_token") == TOKEN


# 平台 02 / ADR-0020：兜底页可独立建立写操作会话，联动 iframe 只加载前端。
@pytest.mark.parametrize("panel", ["chart", "alerts", "scanner", "levels"])
def test_desk_panel_deep_link_serves_app_and_sets_session(client, panel) -> None:
    response = client.get(f"/desk/{panel}")
    assert response.status_code == 200
    assert "<title>Talaria</title>" in response.text
    assert response.cookies.get("talaria_token") == TOKEN
    assert response.headers["cache-control"] == "no-store"


def test_desk_link_relay_serves_app_without_setting_session(client) -> None:
    response = client.get("/desk/link")
    assert response.status_code == 200
    assert "<title>Talaria</title>" in response.text
    assert "set-cookie" not in response.headers


def test_desk_spa_fallback_only_covers_desk_paths(client) -> None:
    assert client.get("/desk/anything").status_code == 200
    assert client.get("/").status_code == 404


def test_missing_dist_returns_chinese_instructions(tmp_path, fixture_dir) -> None:
    missing = tmp_path / "missing-dist"
    app = create_app(_app_settings(tmp_path, fixture_dir, dist=missing))
    with TestClient(app) as client:
        response = client.get("/desk")
    assert response.status_code == 200
    assert "前端构建产物缺失" in response.text
    assert "pnpm -C dsh" in response.text


def test_built_assets_are_served_from_dist(client, dist_dir) -> None:
    asset = dist_dir / "assets" / "app.js"
    asset.parent.mkdir()
    asset.write_text("console.log('talaria')", encoding="utf-8")
    response = client.get("/assets/app.js")
    assert response.status_code == 200
    assert response.text == "console.log('talaria')"
    assert client.get("/assets/../index.html").status_code in {404, 200}
    assert client.get("/assets/nope.js").status_code == 404


def test_no_cors_headers(client) -> None:
    response = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in {key.lower() for key in response.headers}


def test_create_snapshot_and_read_meta(client) -> None:
    response = _create(client)
    assert response.status_code == 200
    meta = response.json()
    assert meta["instrument_id"] == "BTC-USDT-PERP"
    assert meta["mode"] == "HISTORICAL_PIT"
    assert meta["cutoff_ts"] == NOW_MS
    assert meta["created_ts"] == NOW_MS
    assert meta["bar_counts"] == {"1m": 905}
    assert len(meta["snapshot_id"]) == 32

    stored = client.get(f"/api/snapshots/{meta['snapshot_id']}")
    assert stored.status_code == 200
    assert stored.json() == meta


def test_observed_replay_is_explicitly_not_implemented(client) -> None:
    response = _create(client, mode="OBSERVED_REPLAY")
    assert response.status_code == 501
    assert response.json()["code"] == "NOT_IMPLEMENTED"


def test_unknown_instrument_is_404(client) -> None:
    response = _create(client, instrument_id="DOGE-USDT-PERP")
    assert response.status_code == 404
    assert response.json()["code"] == "INSTRUMENT_NOT_FOUND"


def test_unsupported_timeframe_is_422(client) -> None:
    response = _create(client, timeframes=["2m"])
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_empty_timeframes_is_422(client) -> None:
    response = _create(client, timeframes=[])
    assert response.status_code == 422


def test_unknown_snapshot_and_timeframe_are_404(client) -> None:
    assert client.get("/api/snapshots/missing").status_code == 404
    created = _create(client).json()
    response = client.get(
        f"/api/snapshots/{created['snapshot_id']}/bars", params={"timeframe": "5m"}
    )
    assert response.status_code == 404
    assert response.json()["code"] == "SNAPSHOT_NOT_FOUND"


def test_bar_limit_validation(client) -> None:
    created = _create(client).json()
    base = f"/api/snapshots/{created['snapshot_id']}/bars"
    assert client.get(base, params={"timeframe": "1m", "limit": 0}).status_code == 422
    assert client.get(base, params={"timeframe": "1m", "limit": 1001}).status_code == 422
    assert client.get(base, params={"timeframe": "1m", "before_ts": -1}).status_code == 422


def test_latest_page_is_ascending_and_flags(client) -> None:
    created = _create(client).json()
    response = client.get(
        f"/api/snapshots/{created['snapshot_id']}/bars",
        params={"timeframe": "1m", "limit": 300},
    )
    assert response.status_code == 200
    page = response.json()
    assert page["has_more_forward"] is True
    assert page["has_more_backward"] is False
    timestamps = [bar["timestamp"] for bar in page["bars"]]
    assert len(timestamps) == 300
    assert timestamps == sorted(timestamps)
    assert isinstance(page["bars"][0]["open"], str)


def test_forward_pagination_walks_backwards_without_overlap(client) -> None:
    created = _create(client).json()
    base = f"/api/snapshots/{created['snapshot_id']}/bars"
    first = client.get(base, params={"timeframe": "1m", "limit": 300}).json()
    second = client.get(
        base,
        params={"timeframe": "1m", "limit": 300, "before_ts": first["bars"][0]["timestamp"]},
    ).json()
    third = client.get(
        base,
        params={"timeframe": "1m", "limit": 300, "before_ts": second["bars"][0]["timestamp"]},
    ).json()
    fourth = client.get(
        base,
        params={"timeframe": "1m", "limit": 300, "before_ts": third["bars"][0]["timestamp"]},
    ).json()

    assert second["has_more_forward"] is True
    assert second["has_more_backward"] is True
    assert third["has_more_forward"] is True
    assert third["has_more_backward"] is True
    assert fourth["has_more_forward"] is False
    assert fourth["has_more_backward"] is True
    assert len(fourth["bars"]) == 5
    assert max(bar["timestamp"] for bar in second["bars"]) < min(
        bar["timestamp"] for bar in first["bars"]
    )
    assert max(bar["timestamp"] for bar in third["bars"]) < min(
        bar["timestamp"] for bar in second["bars"]
    )
    assert max(bar["timestamp"] for bar in fourth["bars"]) < min(
        bar["timestamp"] for bar in third["bars"]
    )
    all_timestamps = [
        bar["timestamp"] for page in (first, second, third, fourth) for bar in page["bars"]
    ]
    assert len(all_timestamps) == 905
    assert len(set(all_timestamps)) == 905


def test_full_snapshot_preserves_feed_gaps_and_no_trades(client) -> None:
    created = _create(client).json()
    page = client.get(
        f"/api/snapshots/{created['snapshot_id']}/bars",
        params={"timeframe": "1m", "limit": 1000},
    ).json()
    assert len(page["bars"]) == 905
    statuses = [bar["status"] for bar in page["bars"]]
    assert statuses.count("FEED_GAP") == 5
    assert statuses.count("NO_TRADES") == 1
    gap = next(bar for bar in page["bars"] if bar["status"] == "FEED_GAP")
    assert gap["open"] is None
    assert gap["turnover"] is None
    timestamps = [bar["timestamp"] for bar in page["bars"]]
    assert timestamps == list(range(timestamps[0], timestamps[0] + 905 * 60_000, 60_000))


def test_feed_failure_maps_to_503(tmp_path, dist_dir) -> None:
    empty_fixture = tmp_path / "empty-fixture"
    empty_fixture.mkdir()
    app = create_app(_app_settings(tmp_path, empty_fixture, dist=dist_dir))
    with TestClient(app) as client:
        response = _create(client)
    assert response.status_code == 503
    assert response.json()["code"] == "FEED_UNAVAILABLE"


def test_lifespan_creates_and_checkpoints_collaboration_state(
    tmp_path, fixture_dir, dist_dir
) -> None:
    app = create_app(_app_settings(tmp_path, fixture_dir, dist=dist_dir))
    with TestClient(app):
        pass

    db = tmp_path / "data" / "state" / "collaboration_state.sqlite"
    assert db.is_file()
    wal = Path(f"{db}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_validation_error_is_wrapped_in_error_response(client) -> None:
    response = client.post(
        "/api/snapshots", content=json.dumps({}), headers={**AUTH, "Content-Type": "application/json"}
    )
    assert response.status_code == 422
    payload = response.json()
    assert payload["code"] == "VALIDATION_ERROR"
    assert "instrument_id" in payload["message"]


# data-access/02: a chart request fills cold history independently of snapshot creation.
def test_chart_history_is_readable_from_cold_store_after_shutdown(
    tmp_path, fixture_dir, dist_dir
) -> None:
    settings = _app_settings(tmp_path, fixture_dir, dist=dist_dir)
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    reader = ColdReader(settings.data_dir)
    app = create_app(settings)
    with TestClient(app) as client:
        snapshot = _create(client)
        assert snapshot.status_code == 200
        snapshot_id = snapshot.json()["snapshot_id"]
        assert reader.read_bars(instrument, "1m") == []

        page = client.get(f"/api/snapshots/{snapshot_id}/bars?timeframe=1m&limit=10")
        assert page.status_code == 200
        expected = [(bar["timestamp"], Decimal(bar["close"])) for bar in page.json()["bars"]]
        assert len(expected) == 10

    bars = reader.read_bars(instrument, "1m")
    assert [(bar.ts_event, bar.close) for bar in bars] == expected

    with TestClient(create_app(settings)) as client:
        assert client.get(f"/api/snapshots/{snapshot_id}").status_code == 200


@pytest.mark.regression
@pytest.mark.parametrize("method", [
    "fetch_mark_price_bars", "fetch_funding_settlements", "fetch_open_interest",
])
def test_derivative_feed_failure_does_not_fail_snapshot(client, monkeypatch, caplog, method):
    # Ticket 02: fail one optional upstream endpoint while K-line history remains available.
    def unavailable(*args, **kwargs):
        raise FeedUnavailableError(f"unavailable: {method}")

    monkeypatch.setattr(FixtureConnector, method, unavailable)
    snapshot = _create(client)
    assert snapshot.status_code == 200
    meta = snapshot.json()
    assert meta["bar_counts"]["1m"] > 0
    assert client.get(f"/api/snapshots/{meta['snapshot_id']}").status_code == 200
    assert f"unavailable: {method}" in caplog.text


# data-access/02: inject a disk failure at the cold writer boundary.
@pytest.mark.regression
def test_cold_disk_failure_does_not_fail_chart(tmp_path, fixture_dir, dist_dir, monkeypatch, caplog):
    def disk_full(self, batch):
        raise OSError("disk full")

    monkeypatch.setattr(ColdStore, "publish", disk_full)
    with TestClient(create_app(_app_settings(tmp_path, fixture_dir, dist=dist_dir))) as client:
        frame = _create(client).json()["snapshot_id"]
        page = client.get(f"/api/snapshots/{frame}/bars?timeframe=1m&limit=10")
        assert page.status_code == 200
        assert len(page.json()["bars"]) == 10

    assert "冷库发布失败" in caplog.text and "disk full" in caplog.text


@pytest.mark.regression
def test_failed_cold_history_submission_does_not_fail_chart_or_block_retry(
    tmp_path, fixture_dir, dist_dir, monkeypatch, caplog,
):
    # Ticket 02: a synchronous executor failure must leave the range eligible for retry.
    class FailsFirstHistorySubmission(ThreadPoolExecutor):
        failed = False

        def submit(self, *args, **kwargs):
            if self._thread_name_prefix == "cold-history" and not self.failed:
                self.failed = True
                raise OSError("history submission failed")
            return super().submit(*args, **kwargs)

    monkeypatch.setattr(
        "talaria.perception.cold_publisher.ThreadPoolExecutor", FailsFirstHistorySubmission,
    )
    settings = _app_settings(tmp_path, fixture_dir, dist=dist_dir)
    with TestClient(create_app(settings)) as client:
        snapshot_id = _create(client).json()["snapshot_id"]
        url = f"/api/snapshots/{snapshot_id}/bars?timeframe=1m&limit=10"
        for _ in range(2):
            page = client.get(url)
            assert page.status_code == 200
            assert len(page.json()["bars"]) == 10

    assert "history submission failed" in caplog.text
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    assert len(ColdReader(settings.data_dir).read_bars(instrument, "1m")) == 10


@pytest.mark.spec
def test_dsh_desktop_origin_can_make_authorized_fast_path_requests(client) -> None:
    origin = "dsh-app://app"
    response = client.options("/api/collaboration/consult", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
