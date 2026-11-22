"""简报 API：列表、已读、带入对话、宿主结果与 Bearer 写保护。"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from talaria.foundation.bars import CompactBar
from talaria.foundation.types import PrecisionTier
from talaria.workbench_api.app import create_app
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings

pytestmark = pytest.mark.medium

TOKEN = "briefing-token"
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


def _install_fake_host(client: TestClient) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def dispatch(url: str, payload: Mapping[str, Any]) -> None:
        calls.append({"url": url, **dict(payload)})

    service = client.app.state.runtime.briefings
    service._host_url = lambda: "http://127.0.0.1:65530"  # type: ignore[assignment]
    service._dispatcher = dispatch  # type: ignore[assignment]
    # 后台扫描 tick 可能已占用默认并发/令牌；测试聚焦 BTC 时放宽并清空冷却。
    service._max_concurrency = 4
    service._bucket._capacity = 10.0
    service._bucket._tokens = 10.0
    service._cooldown_until.clear()
    return calls


def _wait_briefing(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get("/api/scanner/briefings")
        assert response.status_code == 200
        items = [
            item
            for item in response.json()["briefings"]
            if item["instrument_id"] == INSTRUMENT
        ]
        if items:
            return items[0]
        time.sleep(0.02)
    raise AssertionError("简报未生成")


def _trigger_briefing(client: TestClient) -> dict[str, Any]:
    scanner = client.app.state.runtime.scanner
    scanner.seed_bars(INSTRUMENT, "1m", [_spike_bar()])
    response = client.post("/api/scanner/scan", json={}, headers=AUTH)
    assert response.status_code == 200, response.text
    return _wait_briefing(client)


def test_briefing_reads_are_public_and_writes_require_bearer(
    client: TestClient,
) -> None:
    assert client.get("/api/scanner/briefings").status_code == 200
    assert client.post("/api/scanner/briefings/brf_x/read").status_code == 401
    assert client.post(
        "/api/scanner/briefings/brf_x/consult", json={}
    ).status_code == 401
    assert client.post(
        "/api/scanner/briefings/brf_x/result", json={"text": "{}"}
    ).status_code == 401


def test_briefing_binds_snapshot_and_supports_read_consult_result(
    client: TestClient,
) -> None:
    calls = _install_fake_host(client)
    briefing = _trigger_briefing(client)
    assert briefing["status"] == "PENDING"
    assert briefing["snapshot_id"]
    call = next(item for item in calls if item["briefing_id"] == briefing["id"])
    assert call["snapshot_id"] == briefing["snapshot_id"]
    assert briefing["id"] in call["prompt"]

    read = client.post(
        f"/api/scanner/briefings/{briefing['id']}/read", headers=AUTH
    )
    assert read.status_code == 200
    assert read.json()["read_ts"] == NOW_MS
    unread = client.get("/api/scanner/briefings?unread_only=true").json()[
        "briefings"
    ]
    assert all(item["id"] != briefing["id"] for item in unread)

    result = client.post(
        f"/api/scanner/briefings/{briefing['id']}/result",
        json={
            "content": {
                "trigger_reason": "放量突破前高",
                "key_levels": [
                    {"label": "前高", "price": "101.5", "note": "观察承接"}
                ],
                "risks": ["追高风险"],
            },
            "dsh_session_id": "dsh-1",
            "dsh_turn_id": "7",
            "usage": {
                "dsh_session_id": "dsh-1",
                "dsh_turn_id": "7",
                "input_tokens": 120,
                "output_tokens": 40,
            },
        },
        headers=AUTH,
    )
    assert result.status_code == 200, result.text
    ready = result.json()
    assert ready["status"] == "READY"
    assert ready["summary"]["trigger_reason"] == "放量突破前高"
    assert "snapshot:" + briefing["snapshot_id"] in {
        ref["evidence_id"] for ref in ready["evidence_refs"]
    }

    consult = client.post(
        f"/api/scanner/briefings/{briefing['id']}/consult",
        json={"question": "这次放量能延续吗？"},
        headers=AUTH,
    )
    assert consult.status_code == 200, consult.text
    seed = consult.json()
    assert seed["snapshot_id"] == briefing["snapshot_id"]
    assert seed["question"] == "这次放量能延续吗？"

    listed = client.get("/api/scanner/briefings").json()
    listed_item = next(
        item for item in listed["briefings"] if item["id"] == briefing["id"]
    )
    assert listed_item["read_ts"] == NOW_MS

    usage = client.get("/api/collaboration/usage").json()
    assert usage["summary"]["turns"] == 1
    assert usage["summary"]["input_tokens"] == 120


def test_consult_requires_ready_briefing(client: TestClient) -> None:
    calls = _install_fake_host(client)
    briefing = _trigger_briefing(client)
    assert calls
    response = client.post(
        f"/api/scanner/briefings/{briefing['id']}/consult", headers=AUTH
    )
    assert response.status_code == 422


@pytest.mark.regression
@pytest.mark.parametrize(
    ("action", "payload"),
    [("read", None), ("result", {"error": "宿主返回失败"})],
)
def test_disabled_scanner_keeps_existing_briefing_read_only(
    tmp_path, dist_dir, action, payload
) -> None:
    # platform/01: 已经生成的简报仍可查，但关闭扫描后不再写入简报表。
    settings = _settings(tmp_path, dist_dir)
    with TestClient(create_app(settings)) as client:
        _install_fake_host(client)
        briefing = _trigger_briefing(client)
        assert briefing["status"] == "PENDING"
        assert briefing["read_ts"] is None

    disabled = replace(settings, features=FeatureToggles(scanner=False))
    with TestClient(create_app(disabled)) as client:
        response = client.post(
            f"/api/scanner/briefings/{briefing['id']}/{action}",
            json=payload,
            headers=AUTH,
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "FEATURE_DISABLED"
        listing = client.get("/api/scanner/briefings")

    assert listing.status_code == 200, listing.text
    assert briefing in listing.json()["briefings"]
