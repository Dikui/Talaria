"""data-access/06：策略 WS、SDK 恢复与上游订阅配额。"""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.feed import KlineEvent
from talaria.foundation.types import PrecisionTier
from talaria.workbench_api.app import create_app
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.settings import AppSettings

pytestmark = [pytest.mark.medium, pytest.mark.spec]

BTC = "BTC-USDT-PERP"
ETH = "ETH-USDT-PERP"
MINUTE = 60_000
BASE = 1_800_000_000_000
AUTH = {"Authorization": "Bearer test-token"}


def settings(tmp_path, *, quota=2, cold_store=False):
    replay = tmp_path / "events.jsonl"
    replay.write_text(json.dumps({"offset_ms": 0, "type": "heartbeat"}) + "\n")
    return AppSettings(
        data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", replay_events=replay,
        token="test-token", now_ms=lambda: BASE + 10 * MINUTE, realtime_quota=quota,
        features=FeatureToggles(cold_store=cold_store, scanner=False),
    )


def kline(timestamp=BASE, value="100", *, timeframe="1m", confirmed=True):
    price = Decimal(value)
    return KlineEvent(
        instrument_id=BTC, timeframe=timeframe, confirmed=confirmed, revision=0,
        bar=CompactBar(
            timestamp=timestamp, open=price, high=price, low=price, close=price,
            volume=Decimal(2), turnover=price * 2, status=BarStatus.NORMAL,
            ts_available=timestamp + (5 if timeframe == "5m" else 1) * MINUTE,
            precision=PrecisionTier.SECOND_VERIFIED,
        ),
    )


def request(instrument=BTC, timeframe="1m", **fields):
    return {"instrument": instrument, "timeframe": timeframe, "owner": "test-owner", **fields}


def test_strategy_socket_receives_only_confirmed_revisions_without_a_window(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as client, client.websocket_connect("/ws/strategy", headers=AUTH) as ws:
        ws.send_json(request())
        ready = ws.receive_json()
        assert ready["type"] == "ready"
        assert ready["seq"] == 0
        assert [(channel["instrument_id"], channel["timeframe"])
                for channel in client.get("/api/feed/health").json()["channels"]] == [(BTC, "1m")]
        supervisor = app.state.runtime.supervisor
        client.portal.call(supervisor.ingest, kline(confirmed=False))
        client.portal.call(supervisor.ingest, kline())
        client.portal.call(supervisor.ingest, kline(value="101"))
        first, second = ws.receive_json(), ws.receive_json()
        assert [(event["epoch"], event["seq"]) for event in (first, second)] == [
            (ready["epoch"], 1), (ready["epoch"], 2),
        ]
        assert [event["bar"]["close"] for event in (first, second)] == ["100", "101"]
        assert [event["bar"]["revision"] for event in (first, second)] == [1, 2]


@pytest.mark.regression
def test_sdk_replays_old_revision_and_close_during_reconnect_after_overflow(tmp_path, monkeypatch):
    from talaria import sdk

    app = create_app(settings(tmp_path))
    # Tiny queue and a blocked ASGI send reproduce the backpressure of a slow transport.
    monkeypatch.setattr("talaria.workbench_api.strategy_stream.QUEUE_SIZE", 2)

    async def scenario():
        async with app.router.lifespan_context(app):
            runtime = app.state.runtime
            blocked = asyncio.Event()
            disconnected = asyncio.Event()
            overflow_closed = asyncio.Event()
            reached = asyncio.Event()
            connections = 0

            def produce(event):
                runtime.supervisor.ingest(event)

            @asynccontextmanager
            async def connect(*args, **kwargs):
                nonlocal connections
                connections += 1
                attempt = connections

                async def before_send(message):
                    if message.get("type") != "websocket.send":
                        return
                    payload = json.loads(message["text"])
                    if attempt == 1 and payload.get("seq") == 2:
                        blocked.set()
                        await asyncio.Future()
                    if attempt == 2 and payload.get("type") == "ready":
                        # The connection already holds its live queue and has copied the journal.
                        produce(kline(BASE + 2 * MINUTE))
                        reached.set()

                if attempt == 2:
                    await disconnected.wait()
                    produce(kline(BASE, "105"))
                async with asgi_socket(app, before_send=before_send, on_close=overflow_closed.set) as ws:
                    try:
                        yield ws
                    finally:
                        if attempt == 1:
                            assert ws.close_code == 1013

            install_sdk_transport(monkeypatch, app, connect)

            async with aclosing(sdk.subscribe(BTC, "1m", data_dir=tmp_path)) as events:
                first = asyncio.create_task(anext(events))
                await wait_for_channel(runtime, "1m")
                produce(kline())
                assert (await first).seq == 1
                produce(kline(BASE + MINUTE))
                await blocked.wait()
                for price in ("101", "102", "103"):
                    produce(kline(BASE, price))
                # Leave the SDK idle until the ASGI session closes for overflow.
                async with asyncio.timeout(5):
                    await overflow_closed.wait()
                disconnected.set()
                received = [await anext(events) for _ in range(6)]
                assert reached.is_set()
                assert [event.seq for event in received] == list(range(2, 8))
                assert [event.bar.close for event in received] == [
                    Decimal(100), Decimal(101), Decimal(102), Decimal(103), Decimal(105), Decimal(100),
                ]
                assert [event.bar.ts_event for event in received] == [
                    BASE + MINUTE, BASE, BASE, BASE, BASE, BASE + 2 * MINUTE,
                ]

    async def run():
        async with asyncio.timeout(10):
            await scenario()

    asyncio.run(run())


async def wait_for_channel(runtime, timeframe):
    while not runtime.supervisor.has_channel(BTC, timeframe):
        await asyncio.sleep(0)


@asynccontextmanager
async def asgi_socket(app, *, before_send=None, on_close=None):
    # Workbench ASGI and the SDK execute normally. Only socket I/O is substituted.
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    inbound, outbound = asyncio.Queue(), asyncio.Queue()

    class Socket:
        close_code = None

        async def send(self, message):
            await inbound.put({"type": "websocket.receive", "text": message})

        async def recv(self):
            message = await outbound.get()
            if message["type"] == "websocket.close":
                self.close_code = message["code"]
                raise ConnectionClosedError(Close(self.close_code, message.get("reason", "")), None)
            assert message["type"] == "websocket.send"
            return message["text"]

        async def close(self, code=1000):
            await inbound.put({"type": "websocket.disconnect", "code": code})

        async def disconnect(self):
            await self.close(code=1006)
            await outbound.put({"type": "websocket.close", "code": 1006})

    async def send(message):
        if before_send is not None:
            await before_send(message)
        if message["type"] == "websocket.close" and on_close is not None:
            on_close()
        await outbound.put(message)

    socket = Socket()
    scope = {
        "type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "path": "/ws/strategy", "root_path": "", "query_string": b"", "scheme": "ws",
        "headers": [(b"authorization", b"Bearer test-token")],
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 12345),
        "subprotocols": [],
    }
    task = asyncio.create_task(app(scope, inbound.get, send))
    await inbound.put({"type": "websocket.connect"})
    try:
        assert (await outbound.get())["type"] == "websocket.accept"
        yield socket
    finally:
        await socket.close()
        await task


def install_sdk_transport(monkeypatch, app, connect, *, transport=None):
    from talaria.sdk import _subscription
    from talaria.service_endpoint import ServiceConnection

    monkeypatch.setattr(
        _subscription, "read_service_connection",
        lambda _: ServiceConnection("http://127.0.0.1:8000", "test-token"),
    )
    monkeypatch.setattr(_subscription, "connect", connect)
    http_client = httpx.AsyncClient
    monkeypatch.setattr(
        _subscription, "AsyncClient",
        lambda **kwargs: http_client(transport=transport or httpx.ASGITransport(app=app), **kwargs),
    )


@pytest.mark.regression
def test_sdk_resyncs_all_missed_revisions_after_service_restart(tmp_path, monkeypatch):
    from talaria import sdk

    config = settings(tmp_path, cold_store=True)
    first_app, second_app = create_app(config), create_app(config)

    async def scenario():
        active_app = first_app
        socket = None
        recovering = False

        @asynccontextmanager
        async def connect(*args, **kwargs):
            nonlocal socket
            async with asgi_socket(active_app) as ws:
                socket = ws
                yield ws

        class RangeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal recovering
                transport = httpx.ASGITransport(app=active_app)
                response = await transport.handle_async_request(request)
                if request.url.path == "/api/bars" and not recovering:
                    recovering = True
                    # A close after range reads, while the strategy socket is already attached.
                    active_app.state.runtime.supervisor.ingest(kline(BASE + 2 * MINUTE))
                return response

        install_sdk_transport(monkeypatch, first_app, connect, transport=RangeTransport())
        async with aclosing(sdk.subscribe(BTC, "1m", data_dir=config.data_dir)) as events:
            async with first_app.router.lifespan_context(first_app):
                first = asyncio.create_task(anext(events))
                runtime = first_app.state.runtime
                await wait_for_channel(runtime, "1m")
                runtime.supervisor.ingest(kline())
                initial = await first
                assert initial.seq == 1
                await socket.disconnect()

            active_app = second_app
            async with second_app.router.lifespan_context(second_app):
                runtime = second_app.state.runtime
                runtime.subscription_scheduler.acquire_chart((BTC, "1m"))
                async with asgi_socket(second_app) as observer:
                    await observer.send(json.dumps(request(owner="observer")))
                    assert json.loads(await observer.recv())["type"] == "ready"
                    for event in [kline(value="101"), kline(value="102"), kline(BASE + MINUTE)]:
                        runtime.supervisor.ingest(event)
                        assert json.loads(await observer.recv())["type"] == "bar"

                recovered = [await anext(events) for _ in range(4)]
                assert recovering
                assert [event.bar.ts_event for event in recovered] == [
                    BASE, BASE, BASE + MINUTE, BASE + 2 * MINUTE,
                ]
                assert [event.bar.close for event in recovered] == [
                    Decimal(101), Decimal(102), Decimal(100), Decimal(100),
                ]
                assert [event.bar.revision for event in recovered] == [1, 2, 0, 0]
                assert [event.seq is None for event in recovered] == [True, True, True, False]
                assert all(event.epoch != initial.epoch for event in recovered)

    async def run():
        async with asyncio.timeout(5):
            await scenario()

    asyncio.run(run())


def create_5m_alert(client, **fields):
    response = client.post(
        "/api/alerts", headers=AUTH, json={
            "instrument_id": BTC, "timeframe": "5m",
            "condition": {
                "kind": "INDICATOR_SIGNAL", "indicator": "EMA", "indicator_period": 2,
                "direction": "ABOVE", "threshold": "105",
            },
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["alert_id"]


def test_lone_5m_indicator_alert_receives_5m_closes(tmp_path, fixture_dir):
    app = create_app(replace(settings(tmp_path), fixture_dir=fixture_dir))
    with TestClient(app) as client:
        alert_id = create_5m_alert(client)
        channels = client.get("/api/feed/health").json()["channels"]
        assert [(channel["instrument_id"], channel["timeframe"]) for channel in channels] == [(BTC, "5m")]
        runtime = app.state.runtime
        for timestamp, price in ((BASE - 5 * MINUTE, "100"), (BASE, "100"), (BASE + 5 * MINUTE, "130")):
            client.portal.call(runtime.supervisor.ingest, kline(timestamp, price, timeframe="5m"))
        client.portal.call(runtime.alerts.flush)
        triggers = client.get("/api/alerts/triggers", params={"alert_id": alert_id}).json()["triggers"]
        assert [trigger["timeframe"] for trigger in triggers] == ["5m"]


@pytest.mark.regression
def test_stale_chart_channel_does_not_suspend_a_healthy_alert_period(tmp_path, fixture_dir):
    now = BASE + 10 * MINUTE
    app = create_app(replace(settings(tmp_path), fixture_dir=fixture_dir, now_ms=lambda: now))
    with TestClient(app) as client, client.websocket_connect("/ws/desk") as chart:
        alert_id = create_5m_alert(client)
        assert chart.receive_json()["type"] == "hello"
        chart.send_json({"op": "subscribe", "instrument_id": BTC, "timeframe": "1m"})
        assert chart.receive_json()["type"] == "health"
        runtime = app.state.runtime
        for timestamp in (BASE - 5 * MINUTE, BASE):
            client.portal.call(runtime.supervisor.ingest, kline(timestamp, timeframe="5m"))
        now += 11_000
        client.portal.call(runtime.supervisor.ingest, kline(BASE + 5 * MINUTE, timeframe="5m", confirmed=False))
        now += 2_000
        client.portal.call(runtime.supervisor.tick)
        channels = client.get("/api/feed/health").json()["channels"]
        assert {channel["timeframe"]: channel["status"] for channel in channels} == {
            "1m": "STALE", "5m": "LIVE",
        }
        client.portal.call(runtime.supervisor.ingest, kline(BASE + 5 * MINUTE, "130", timeframe="5m"))
        client.portal.call(runtime.alerts.flush)

        triggers = client.get("/api/alerts/triggers", params={"alert_id": alert_id}).json()["triggers"]
        assert [trigger["timeframe"] for trigger in triggers] == ["5m"]


def test_full_quota_refuses_new_account_and_protects_existing_account(tmp_path):
    app = create_app(settings(tmp_path, quota=1))
    with TestClient(app) as client:
        create_5m_alert(client)
        with client.websocket_connect("/ws/strategy", headers=AUTH) as denied:
            denied.send_json(request(ETH, owner="denied"))
            report = denied.receive_json()
            assert (report["type"], report["code"]) == ("error", "SUBSCRIPTION_QUOTA")

        with client.websocket_connect("/ws/strategy", headers=AUTH) as account:
            account.send_json(request(timeframe="5m"))
            assert account.receive_json()["type"] == "ready"
            with client.websocket_connect("/ws/desk") as chart:
                assert chart.receive_json()["type"] == "hello"
                chart.send_json({"op": "subscribe", "instrument_id": ETH, "timeframe": "5m"})
                error = chart.receive_json()
                assert (error["type"], error["code"]) == ("error", "SUBSCRIPTION_QUOTA")
            channels = client.get("/api/feed/health").json()["channels"]
            assert [(channel["instrument_id"], channel["timeframe"]) for channel in channels] == [(BTC, "5m")]


def test_evicted_alert_receives_quota_notice(tmp_path):
    app = create_app(settings(tmp_path, quota=1))
    with TestClient(app) as client, client.websocket_connect("/ws/desk") as ws:
        assert ws.receive_json()["type"] == "hello"
        alert_id = create_5m_alert(client)
        ws.send_json({"op": "subscribe", "instrument_id": BTC, "timeframe": "1m"})
        notice = ws.receive_json()
        if notice["type"] == "health":
            notice = ws.receive_json()
        assert notice["code"] == "SUBSCRIPTION_QUOTA"
        assert (notice["owner_id"], notice["instrument_id"], notice["timeframe"]) == (alert_id, BTC, "5m")


@pytest.mark.regression
def test_alert_expiring_during_a_feed_event_releases_its_quota(tmp_path):
    now = BASE + 10 * MINUTE
    app = create_app(replace(settings(tmp_path, quota=1), now_ms=lambda: now))
    with TestClient(app) as client:
        create_5m_alert(client, expires_at=now + MINUTE)
        runtime = app.state.runtime
        now += MINUTE
        client.portal.call(runtime.supervisor.ingest, kline(BASE, timeframe="5m"))
        client.portal.call(runtime.alerts.flush)

        with client.websocket_connect("/ws/strategy", headers=AUTH) as account:
            account.send_json(request(ETH))
            assert account.receive_json()["type"] == "ready"
        assert client.get("/api/feed/health").json()["channels"] == []


def test_cursor_outside_log_retention_requires_resync(tmp_path, monkeypatch):
    monkeypatch.setattr("talaria.workbench_api.strategy_stream.LOG_SIZE", 2)
    app = create_app(settings(tmp_path))
    with TestClient(app) as client, client.websocket_connect("/ws/strategy", headers=AUTH) as ws:
        ws.send_json(request())
        ready = ws.receive_json()
        for price in ("100", "101", "102"):
            client.portal.call(app.state.runtime.supervisor.ingest, kline(value=price))
            assert ws.receive_json()["type"] == "bar"

        # A different socket may present a retained cursor too. Here seq 1 has fallen off.
        with client.websocket_connect("/ws/strategy", headers=AUTH) as reconnect:
            reconnect.send_json(request(owner="resuming", after={"epoch": ready["epoch"], "seq": 0}))
            status = reconnect.receive_json()
            assert (status["type"], status["epoch"], status["seq"]) == ("resync", ready["epoch"], 3)

        with client.websocket_connect("/ws/strategy", headers=AUTH) as reconnect:
            reconnect.send_json(request(owner="retained", after={"epoch": ready["epoch"], "seq": 1}))
            status = reconnect.receive_json()
            assert (status["type"], status["seq"]) == ("ready", 1)
            assert [reconnect.receive_json()["seq"] for _ in range(2)] == [2, 3]


def test_cursor_requires_resync_after_upstream_channel_was_released(tmp_path):
    app = create_app(settings(tmp_path))

    async def scenario():
        async with app.router.lifespan_context(app):
            async with asgi_socket(app) as first:
                await first.send(json.dumps(request()))
                ready = json.loads(await first.recv())
            assert not app.state.runtime.supervisor.has_channel(BTC, "1m")
            async with asgi_socket(app) as resumed:
                await resumed.send(json.dumps(request(
                    after={"epoch": ready["epoch"], "seq": ready["seq"]},
                )))
                assert json.loads(await resumed.recv())["type"] == "resync"

    async def run():
        async with asyncio.timeout(5):
            await scenario()

    asyncio.run(run())
