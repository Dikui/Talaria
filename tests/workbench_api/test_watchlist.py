"""任务单 08：Watchlist 分组/条目、排序、配额降级、STALE 与鉴权。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from talaria.feed.supervisor import FeedSupervisor
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.feed import (
    FeedHealthEvent,
    FeedHealthReport,
    FeedHealthStatus,
)
from talaria.foundation.types import PrecisionTier
from talaria.foundation.watchlist import DegradationReason
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.workbench_api.app import create_app
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateStore, StateWriterQueue
from talaria.workbench_api.subscriptions import RealtimeSubscriptionScheduler
from talaria.workbench_api.watchlist import WatchlistService

pytestmark = [pytest.mark.medium, pytest.mark.spec]

NOW_MS = 1_795_022_974_103
TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
BTC = "BTC-USDT-PERP"
ETH = "ETH-USDT-PERP"
BONK = "1000BONK-USDT-PERP"


def _settings(
    tmp_path: Path,
    fixture_dir: Path,
    *,
    replay: Path | None = None,
    realtime_quota: int = 8,
    now_ms=lambda: NOW_MS,
) -> AppSettings:
    return AppSettings(
        data_dir=tmp_path / "data",
        dist_dir=tmp_path / "dist",
        fixture_dir=fixture_dir,
        replay_events=replay,
        replay_speed=100.0,
        realtime_quota=realtime_quota,
        token=TOKEN,
        now_ms=now_ms,
    )


def _client(settings: AppSettings) -> TestClient:
    return TestClient(create_app(settings))


@pytest.fixture()
def client(tmp_path: Path, fixture_dir: Path) -> Iterator[TestClient]:
    with _client(_settings(tmp_path, fixture_dir)) as active:
        yield active


def _create_group(client: TestClient, name: str) -> str:
    response = client.post(
        "/api/watchlist/groups", json={"name": name}, headers=AUTH
    )
    assert response.status_code == 200, response.text
    return next(
        group["group_id"]
        for group in response.json()["groups"]
        if group["name"] == name
    )


def test_group_and_item_crud_persists_across_restart(
    tmp_path: Path, fixture_dir: Path
) -> None:
    settings = _settings(tmp_path, fixture_dir)
    with _client(settings) as client:
        empty = client.get("/api/watchlist").json()
        assert empty["groups"] == []
        assert empty["realtime_quota"] == 0
        assert empty["realtime_used"] == 0

        created = client.post(
            "/api/watchlist/groups", json={"name": " 核心 "}, headers=AUTH
        )
        assert created.status_code == 200
        group = created.json()["groups"][0]
        assert group["name"] == "核心"
        assert group["kind"] == "MANUAL"
        assert group["is_system"] is False
        group_id = group["group_id"]

        added = client.post(
            "/api/watchlist/items",
            json={"group_id": group_id, "instrument_id": BTC},
            headers=AUTH,
        )
        assert added.status_code == 200, added.text
        item = added.json()["groups"][0]["items"][0]
        assert item["latest_price"] == "68685.3"
        assert item["quote_source"] == "SNAPSHOT"
        assert item["realtime"] is False
        assert item["degraded"] is True
        assert item["degradation_reason"] == "NO_FEED"
        assert item["stale"] is False
        assert item["feed_status"] is None

        client.post(
            "/api/watchlist/items",
            json={"group_id": group_id, "instrument_id": ETH},
            headers=AUTH,
        )
        reordered = client.put(
            "/api/watchlist/items/order",
            json={"group_id": group_id, "ids": [ETH, BTC]},
            headers=AUTH,
        )
        assert reordered.status_code == 200
        items = reordered.json()["groups"][0]["items"]
        assert [entry["instrument_id"] for entry in items] == [ETH, BTC]
        assert [entry["position"] for entry in items] == [0, 1]

        invalid = client.put(
            "/api/watchlist/items/order",
            json={"group_id": group_id, "ids": [ETH]},
            headers=AUTH,
        )
        assert invalid.status_code == 422

        removed = client.delete(
            f"/api/watchlist/items/{BTC}", headers=AUTH
        )
        assert removed.status_code == 200
        assert [
            entry["instrument_id"]
            for entry in removed.json()["groups"][0]["items"]
        ] == [ETH]

        renamed = client.patch(
            f"/api/watchlist/groups/{group_id}",
            json={"name": "核心观察"},
            headers=AUTH,
        )
        assert renamed.status_code == 200
        assert renamed.json()["groups"][0]["name"] == "核心观察"

        second_id = _create_group(client, "宏观")
        groups_ordered = client.put(
            "/api/watchlist/groups/order",
            json={"ids": [second_id, group_id]},
            headers=AUTH,
        )
        assert groups_ordered.status_code == 200
        assert [group["group_id"] for group in groups_ordered.json()["groups"]] == [
            second_id,
            group_id,
        ]
        assert [
            group["position"] for group in groups_ordered.json()["groups"]
        ] == [0, 1]

        deleted = client.delete(
            f"/api/watchlist/groups/{second_id}", headers=AUTH
        )
        assert deleted.status_code == 200
        assert [group["group_id"] for group in deleted.json()["groups"]] == [
            group_id
        ]

    with _client(settings) as client:
        persisted = client.get("/api/watchlist").json()
        assert persisted["groups"][0]["group_id"] == group_id
        assert persisted["groups"][0]["name"] == "核心观察"
        assert [
            item["instrument_id"] for item in persisted["groups"][0]["items"]
        ] == [ETH]


def test_writes_require_bearer_and_unknown_ids_are_not_found(
    client: TestClient,
) -> None:
    write_calls = [
        ("POST", "/api/watchlist/groups", {"name": "x"}),
        ("POST", "/api/watchlist/items", {"group_id": "g", "instrument_id": BTC}),
        ("PATCH", "/api/watchlist/groups/g", {"name": "x"}),
        ("DELETE", "/api/watchlist/groups/g", None),
        ("PUT", "/api/watchlist/groups/order", {"ids": ["g"]}),
        ("PUT", "/api/watchlist/items/order", {"group_id": "g", "ids": []}),
        ("DELETE", f"/api/watchlist/items/{BTC}", None),
    ]
    for method, path, payload in write_calls:
        response = client.request(method, path, json=payload)
        assert response.status_code == 401, (method, path, response.text)
        assert response.json()["code"] == "UNAUTHORIZED"

    assert client.get("/api/watchlist").status_code == 200

    empty_name = client.post(
        "/api/watchlist/groups", json={"name": "   "}, headers=AUTH
    )
    assert empty_name.status_code == 422
    assert empty_name.json()["code"] == "VALIDATION_ERROR"

    unknown_group = client.post(
        "/api/watchlist/items",
        json={"group_id": "missing", "instrument_id": BTC},
        headers=AUTH,
    )
    assert unknown_group.status_code == 404
    assert unknown_group.json()["code"] == "NOT_FOUND"

    group_id = _create_group(client, "核心")
    unknown_instrument = client.post(
        "/api/watchlist/items",
        json={"group_id": group_id, "instrument_id": "NOPE-USDT-PERP"},
        headers=AUTH,
    )
    assert unknown_instrument.status_code == 404
    assert unknown_instrument.json()["code"] == "INSTRUMENT_NOT_FOUND"

    client.post(
        "/api/watchlist/items",
        json={"group_id": group_id, "instrument_id": BTC},
        headers=AUTH,
    )
    duplicate = client.post(
        "/api/watchlist/items",
        json={"group_id": group_id, "instrument_id": BTC},
        headers=AUTH,
    )
    assert duplicate.status_code == 422

    unknown_patch = client.patch(
        "/api/watchlist/groups/missing", json={"name": "x"}, headers=AUTH
    )
    assert unknown_patch.status_code == 404

    unknown_delete = client.delete(
        "/api/watchlist/items/ETH-USDT-PERP", headers=AUTH
    )
    assert unknown_delete.status_code == 404


def test_alert_tier_gets_quota_before_ordinary_watchlist() -> None:
    supervisor = _FakeSupervisor()
    scheduler = RealtimeSubscriptionScheduler(supervisor, quota=1)

    view = scheduler.reconcile([ETH, BONK], alerted=[BONK])

    assert BONK in view.selected
    assert view.degraded[ETH] is DegradationReason.QUOTA
    assert supervisor.subscribed == {(BONK, "1m")}


def test_system_group_is_seeded_read_only_and_ordered_last(
    tmp_path: Path, fixture_dir: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    store = StateStore(data_dir, now_ms=lambda: NOW_MS)
    try:
        store.upsert_system_group(
            "扫描器聚焦", [BTC, BONK], group_id="scanner-focus"
        )
    finally:
        store.close()

    settings = _settings(tmp_path, fixture_dir)
    with _client(settings) as client:
        watchlist = client.get("/api/watchlist").json()
        system = watchlist["groups"][0]
        assert system["kind"] == "SCANNER_FOCUS"
        assert system["is_system"] is True
        assert [item["instrument_id"] for item in system["items"]] == [BTC, BONK]
        assert system["items"][0]["latest_price"] == "68685.3"
        assert system["items"][1]["latest_price"] == "0.023560"

        group_id = _create_group(client, "核心")
        groups = client.get("/api/watchlist").json()["groups"]
        assert [group["group_id"] for group in groups] == [
            group_id,
            "scanner-focus",
        ]
        assert [group["position"] for group in groups] == [0, 1]

        for method, path, payload in [
            ("PATCH", "/api/watchlist/groups/scanner-focus", {"name": "改"}),
            ("DELETE", "/api/watchlist/groups/scanner-focus", None),
            (
                "POST",
                "/api/watchlist/items",
                {"group_id": "scanner-focus", "instrument_id": ETH},
            ),
            (
                "PUT",
                "/api/watchlist/items/order",
                {"group_id": "scanner-focus", "ids": [BONK, BTC]},
            ),
        ]:
            response = client.request(method, path, json=payload, headers=AUTH)
            assert response.status_code == 422, (method, path, response.text)
            assert response.json()["message"] == "系统分组只读"

        invalid_order = client.put(
            "/api/watchlist/groups/order",
            json={"ids": ["scanner-focus", group_id]},
            headers=AUTH,
        )
        assert invalid_order.status_code == 422
        assert invalid_order.json()["message"] == "系统分组不可调整顺序"

        valid_order = client.put(
            "/api/watchlist/groups/order",
            json={"ids": [group_id, "scanner-focus"]},
            headers=AUTH,
        )
        assert valid_order.status_code == 200
        assert valid_order.json()["groups"][1]["group_id"] == "scanner-focus"
        assert valid_order.json()["groups"][1]["items"][0]["instrument_id"] == BTC


def _wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("等待 Watchlist 订阅重分配超时")


def test_realtime_quota_prioritizes_chart_then_watchlist(
    tmp_path: Path, fixture_dir: Path
) -> None:
    replay = fixture_dir.parent / "realtime" / "in_progress.jsonl"
    app = create_app(
        _settings(
            tmp_path,
            fixture_dir,
            replay=replay,
            realtime_quota=1,
            now_ms=lambda: int(time.time() * 1000),
        )
    )
    with TestClient(app) as client:
        group_id = _create_group(client, "自选")
        for instrument_id in (BONK, ETH):
            response = client.post(
                "/api/watchlist/items",
                json={"group_id": group_id, "instrument_id": instrument_id},
                headers=AUTH,
            )
            assert response.status_code == 200, response.text

        initial = client.get("/api/watchlist").json()
        assert initial["realtime_quota"] == 1
        assert initial["realtime_used"] == 1
        first, second = initial["groups"][0]["items"]
        assert (first["instrument_id"], first["realtime"]) == (BONK, True)
        assert (second["instrument_id"], second["realtime"]) == (ETH, False)
        assert second["degraded"] is True
        assert second["degradation_reason"] == "QUOTA"

        with client.websocket_connect("/ws/desk") as websocket:
            assert websocket.receive_json()["type"] == "hello"
            websocket.send_json(
                {
                    "op": "subscribe",
                    "instrument_id": ETH,
                    "timeframe": "1m",
                }
            )
            deadline = time.monotonic() + 3
            while True:
                current = client.get("/api/watchlist").json()
                items = current["groups"][0]["items"]
                if [item["realtime"] for item in items] == [False, True]:
                    break
                if time.monotonic() > deadline:
                    raise AssertionError(current)
                time.sleep(0.02)
            assert current["realtime_used"] == 1
            assert items[0]["degradation_reason"] == "QUOTA"
            channels = client.get("/api/feed/health").json()["channels"]
            assert [(channel["instrument_id"], channel["timeframe"]) for channel in channels] == [
                (ETH, "1m")
            ]

        _wait_for(
            lambda: client.get("/api/watchlist").json()["groups"][0]["items"][0][
                "realtime"
            ]
        )
        after_close = client.get("/api/watchlist").json()
        assert after_close["realtime_used"] == 1
        assert after_close["groups"][0]["items"][0]["instrument_id"] == BONK
        assert after_close["groups"][0]["items"][0]["realtime"] is True
        assert after_close["groups"][0]["items"][1]["realtime"] is False
        channels = client.get("/api/feed/health").json()["channels"]
        assert [(channel["instrument_id"], channel["timeframe"]) for channel in channels] == [
            (BONK, "1m")
        ]


def _bar(timestamp: int, close: str) -> CompactBar:
    value = Decimal(close)
    return CompactBar(
        timestamp=timestamp,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        turnover=value,
        status=BarStatus.NORMAL,
        ts_available=timestamp + 1,
        precision=PrecisionTier.SECOND_VERIFIED,
    )


class _FakeSupervisor:
    def __init__(self) -> None:
        self.status = FeedHealthStatus.LIVE
        self.subscribed: set[tuple[str, str]] = set()

    def subscribe(self, *, instrument_id: str, timeframe: str) -> None:
        self.subscribed.add((instrument_id, timeframe))

    def unsubscribe(self, *, instrument_id: str, timeframe: str) -> None:
        self.subscribed.discard((instrument_id, timeframe))

    def recent_bars(
        self, *, instrument_id: str, timeframe: str
    ) -> Sequence[CompactBar]:
        return [_bar(1_000, "100"), _bar(1_060, "110")]

    def health(self) -> FeedHealthReport:
        stale_since = 1_060 if self.status is FeedHealthStatus.STALE else None
        event = FeedHealthEvent(
            instrument_id=BTC,
            timeframe="1m",
            status=self.status,
            ts=1_120,
            last_event_ts=1_060,
            stale_since=stale_since,
        )
        return FeedHealthReport(
            feed_mode="replay", status=self.status, channels=[event]
        )


def test_supervisor_quote_priority_and_feed_health_stale(tmp_path: Path) -> None:
    async def scenario() -> None:
        queue = StateWriterQueue(tmp_path / "data", now_ms=lambda: NOW_MS)
        await queue.start()
        try:
            group_id = await queue.run(
                lambda store: store.create_watchlist_group("核心")
            )
            await queue.run(
                lambda store: store.add_watchlist_item(group_id, BTC)
            )
            supervisor = _FakeSupervisor()
            scheduler = RealtimeSubscriptionScheduler(
                supervisor, quota=2  # type: ignore[arg-type]
            )
            service = WatchlistService(
                state_writer=queue,
                instruments=CRYPTO_PERP_PACK.instruments(),
                connector=None,
                supervisor=supervisor,  # type: ignore[arg-type]
                scheduler=scheduler,
                now_ms=lambda: NOW_MS,
            )

            live = (await service.get_watchlist()).groups[0].items[0]
            assert live.latest_price == Decimal("110")
            assert live.change_pct == Decimal("10.00")
            assert live.quote_ts == 1_060
            assert live.quote_source == "SUPERVISOR"
            assert live.realtime is True
            assert live.feed_status == "LIVE"
            assert live.stale is False
            assert supervisor.subscribed == {(BTC, "1m")}

            supervisor.status = FeedHealthStatus.STALE
            stale = (await service.get_watchlist()).groups[0].items[0]
            assert stale.feed_status == "STALE"
            assert stale.stale is True
            assert stale.latest_price == Decimal("110")
        finally:
            await queue.close()

    asyncio.run(scenario())


def test_chart_refcount_survives_one_client_disconnect(
    tmp_path: Path, fixture_dir: Path
) -> None:
    replay = fixture_dir.parent / "realtime" / "in_progress.jsonl"
    app = create_app(
        _settings(
            tmp_path,
            fixture_dir,
            replay=replay,
            realtime_quota=1,
            now_ms=lambda: int(time.time() * 1000),
        )
    )
    with TestClient(app) as client:
        group_id = _create_group(client, "自选")
        for instrument_id in (BONK, ETH):
            client.post(
                "/api/watchlist/items",
                json={"group_id": group_id, "instrument_id": instrument_id},
                headers=AUTH,
            )

        with client.websocket_connect("/ws/desk") as first:
            assert first.receive_json()["type"] == "hello"
            first.send_json(
                {"op": "subscribe", "instrument_id": ETH, "timeframe": "1m"}
            )
            with client.websocket_connect("/ws/desk") as second:
                assert second.receive_json()["type"] == "hello"
                second.send_json(
                    {"op": "subscribe", "instrument_id": ETH, "timeframe": "1m"}
                )
                _wait_for(
                    lambda: client.get("/api/watchlist").json()["groups"][0][
                        "items"
                    ][1]["realtime"]
                )
            time.sleep(0.1)
            channels = client.get("/api/feed/health").json()["channels"]
            assert [
                (channel["instrument_id"], channel["timeframe"])
                for channel in channels
            ] == [(ETH, "1m")]

        _wait_for(
            lambda: client.get("/api/watchlist").json()["groups"][0]["items"][0][
                "realtime"
            ]
        )
        channels = client.get("/api/feed/health").json()["channels"]
        assert [
            (channel["instrument_id"], channel["timeframe"]) for channel in channels
        ] == [(BONK, "1m")]
