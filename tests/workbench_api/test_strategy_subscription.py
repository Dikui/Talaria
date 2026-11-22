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


# 审查修复，data-access/06。ASGI 替代 TCP，REST 替身模拟离线期间的交易所。
def raw_bar(ts=BASE, price="100", revision=0):
    from talaria.foundation.connector import RawBar
    from talaria.packs.crypto_perp import CRYPTO_PERP_PACK

    value = Decimal(price)
    return RawBar(
        symbol=CRYPTO_PERP_PACK.instrument(BTC).symbol, timeframe="1m", ts_event=ts,
        open=value, high=value, low=value, close=value, volume=Decimal(2), turnover=value * 2,
        ts_available=ts + MINUTE, ts_ingested=ts + MINUTE, ts_committed=ts + MINUTE,
        revision=revision,
    )


def archive(config, *bars):
    from contextlib import closing
    from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
    from talaria.perception.cold_store import ColdBatch, ColdStore

    with closing(ColdStore(config.data_dir)) as store:
        store.publish(ColdBatch(CRYPTO_PERP_PACK.instrument(BTC), "1m", bars))


def range_params(ts, **extra):
    return {"instrument": BTC, "timeframe": "1m", "start": ts, "end": ts, **extra}


@pytest.mark.regression
def test_sdk_sees_successive_old_corrections_with_archived_revision_offset(tmp_path, monkeypatch):
    # One-slot hot tail reproduces immediate eviction of an old corrected slot.
    from talaria import sdk
    from talaria.perception.hot_buffer import HotStreamBuffer

    original = HotStreamBuffer.__init__
    monkeypatch.setattr(HotStreamBuffer, "__init__", lambda self, **kw: original(
        self, **kw, max_bars_per_series=1,
    ))
    config = settings(tmp_path)
    archive(config, raw_bar(revision=12))
    app = create_app(config)

    async def run():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            install_sdk_transport(monkeypatch, app, lambda *a, **kw: asgi_socket(app))
            async with aclosing(sdk.subscribe(BTC, "1m", data_dir=config.data_dir)) as events:
                pending = asyncio.create_task(anext(events))
                runtime = app.state.runtime
                await wait_for_channel(runtime, "1m")
                runtime.supervisor.ingest(kline(BASE + MINUTE))
                await asyncio.wait_for(pending, 2)
                for rev, value in [(13, "101"), (14, "102")]:
                    runtime.supervisor.ingest(kline(value=value))
                    event = await asyncio.wait_for(anext(events), 2)
                    bars = (await client.get("/api/bars", params=range_params(BASE))).json()["bars"]
                    assert (event.bar.revision, event.bar.close) == (rev, Decimal(value))
                    assert bars == [event.bar.model_dump(mode="json")]
    asyncio.run(run())


@pytest.mark.regression
def test_recovered_close_uses_receipt_of_ws_correction_before_and_after_publish(tmp_path, monkeypatch):
    from threading import Event
    from talaria.perception.cold_store import ColdStore

    entered, release = Event(), Event()
    publish = ColdStore.publish
    def hold(store, batch):
        entered.set()
        assert release.wait(5)
        return publish(store, batch)
    monkeypatch.setattr(ColdStore, "publish", hold)
    now = BASE + MINUTE + 3_000
    config = replace(settings(tmp_path, cold_store=True), now_ms=lambda: now)
    app = create_app(config)
    with TestClient(app) as client, client.websocket_connect("/ws/strategy", headers=AUTH) as ws:
        ws.send_json(request())
        ws.receive_json()
        supervisor = app.state.runtime.supervisor
        try:
            client.portal.call(lambda: supervisor.transport_restored(backfill=[raw_bar()]))
            assert entered.wait(2)
            assert ws.receive_json()["bar"]["ts_available"] == BASE + MINUTE
            client.portal.call(supervisor.ingest, kline(value="101"))
            update = ws.receive_json()["bar"]
            assert update["ts_available"] == now
        finally:
            release.set()
    with TestClient(create_app(config)) as client:
        assert client.get("/api/bars", params=range_params(BASE)).json()["bars"] == [update]


@pytest.mark.regression
def test_http_fill_is_delivered_even_when_ws_confirms_identical_content(tmp_path, fixture_dir, monkeypatch):
    from talaria.packs.testing import FixtureConnector
    monkeypatch.setattr(FixtureConnector, "fetch_bars", lambda *a, **kw: [raw_bar()])
    app = create_app(replace(settings(tmp_path, cold_store=True), fixture_dir=fixture_dir))

    async def run():
        async with app.router.lifespan_context(app), asgi_socket(app) as ws, httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            await ws.send(json.dumps(request()))
            await ws.recv()
            bars = (await client.get("/api/bars", params=range_params(BASE))).json()["bars"]
            assert json.loads(await asyncio.wait_for(ws.recv(), 1))["bar"] == bars[0]
            app.state.runtime.supervisor.ingest(kline())
            app.state.runtime.supervisor.ingest(kline(value="101"))
            assert json.loads(await asyncio.wait_for(ws.recv(), 1))["bar"]["close"] == "101"
    asyncio.run(run())


@pytest.mark.regression
def test_future_close_waits_until_service_clock_reaches_availability(tmp_path):
    now = BASE + MINUTE - 20
    app = create_app(replace(settings(tmp_path), now_ms=lambda: now))
    async def run():
        nonlocal now
        async with app.router.lifespan_context(app), asgi_socket(app) as ws:
            await ws.send(json.dumps(request()))
            await ws.recv()
            app.state.runtime.supervisor.ingest(kline())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ws.recv(), .08)
            now += 20
            assert json.loads(await asyncio.wait_for(ws.recv(), 1))["bar"]["ts_available"] == now
    asyncio.run(run())


@pytest.mark.regression
def test_expired_lease_refreshes_existing_exchange_slot_and_diffs_subscription_baseline(
    tmp_path, fixture_dir, monkeypatch,
):
    from talaria import sdk
    from talaria.packs.testing import FixtureConnector
    from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
    from talaria.perception.cold_store import ColdBatch

    now = BASE + MINUTE
    config = replace(settings(tmp_path, cold_store=True), now_ms=lambda: now, fixture_dir=fixture_dir)
    archive(config, raw_bar())
    app = create_app(config)
    exchange = {BASE: raw_bar()}
    monkeypatch.setattr(FixtureConnector, "fetch_bars", lambda _, end_ts, limit, **kw: [
        exchange[ts] for ts in sorted(exchange) if ts <= end_ts
    ][-limit:])
    monkeypatch.setattr("talaria.workbench_api.strategy_stream.RECONNECT_LEASE_SECONDS", .01)

    async def run():
        nonlocal now
        async with app.router.lifespan_context(app):
            socket = None
            @asynccontextmanager
            async def connect(*a, **kw):
                nonlocal socket
                async with asgi_socket(app) as socket:
                    yield socket
            install_sdk_transport(monkeypatch, app, connect)
            async with aclosing(sdk.subscribe(BTC, "1m", data_dir=config.data_dir)) as events:
                pending = asyncio.create_task(anext(events))
                runtime = app.state.runtime
                await wait_for_channel(runtime, "1m")
                now += 1
                runtime.supervisor.ingest(kline())  # 同值 WS 确认可以比冷库这一版晚到。
                now += MINUTE - 1
                exchange[BASE + MINUTE] = raw_bar(BASE + MINUTE)
                runtime.supervisor.ingest(kline(BASE + MINUTE))
                assert (await pending).bar.ts_event == BASE + MINUTE
                await socket.disconnect()
                while runtime.supervisor.has_channel(BTC, "1m"):
                    await asyncio.sleep(.01)
                # 同一个可获取窗口：订阅前的旧值、新回填的更老柱、已落盘槽的新值。
                older = raw_bar(BASE - 10 * MINUTE, "88")
                exchange[older.ts_event] = older
                runtime.cold_store.publish(ColdBatch(CRYPTO_PERP_PACK.instrument(BTC), "1m", [older]))
                now += 1
                exchange[BASE] = raw_bar(BASE, "109")
                received = [await asyncio.wait_for(anext(events), 3) for _ in range(2)]
                assert [(ev.bar.ts_event, ev.bar.close) for ev in received] == [
                    (older.ts_event, Decimal(88)), (BASE, Decimal(109)),
                ]
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    authoritative = (await client.get("/api/bars", params=range_params(BASE))).json()["bars"]
                assert authoritative == [received[-1].bar.model_dump(mode="json")]
    asyncio.run(asyncio.wait_for(run(), 6))


@pytest.mark.regression
@pytest.mark.parametrize("preview", [False, True])
def test_resync_incomplete_history_is_retryable(tmp_path, fixture_dir, monkeypatch, preview):
    from talaria.packs.testing import FixtureConnector

    def fetch(*args, **kwargs):
        if preview:
            return [raw_bar()]
        raise OSError("offline")
    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch)
    app = create_app(replace(settings(tmp_path, cold_store=True), fixture_dir=fixture_dir))
    with TestClient(app) as client, client.websocket_connect("/ws/strategy", headers=AUTH) as ws:
        ws.send_json(request())
        ws.receive_json()
        if preview:
            client.portal.call(app.state.runtime.supervisor.ingest, kline(confirmed=False))
        response = client.get("/api/bars", params=range_params(BASE, resync_from=BASE))
        assert response.status_code in (503, 504), response.text


@pytest.mark.regression
def test_restart_delivers_new_content_even_with_same_revision_number(tmp_path, monkeypatch):
    from talaria import sdk
    config = settings(tmp_path)
    apps = [create_app(config), create_app(config)]
    async def run():
        active = apps[0]
        socket = None
        @asynccontextmanager
        async def connect(*a, **kw):
            nonlocal socket
            async with asgi_socket(active) as socket:
                yield socket
        class RangeTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                return await httpx.ASGITransport(app=active).handle_async_request(request)

        install_sdk_transport(monkeypatch, active, connect, transport=RangeTransport())
        async with aclosing(sdk.subscribe(BTC, "1m", data_dir=config.data_dir)) as events:
            async with active.router.lifespan_context(active):
                first = asyncio.create_task(anext(events))
                await wait_for_channel(active.state.runtime, "1m")
                active.state.runtime.supervisor.ingest(kline())
                assert (await first).bar.revision == 0
                await socket.disconnect()
            active = apps[1]
            async with active.router.lifespan_context(active):
                pending = asyncio.create_task(anext(events))
                await wait_for_channel(active.state.runtime, "1m")
                active.state.runtime.supervisor.ingest(kline(value="110"))
                event = await asyncio.wait_for(pending, 2)
                assert (event.bar.revision, event.bar.close) == (0, Decimal(110))
    asyncio.run(asyncio.wait_for(run(), 5))


@pytest.mark.regression
def test_ordinary_alert_releases_quota_during_cooldown(tmp_path, fixture_dir):
    app = create_app(replace(settings(tmp_path, quota=1), fixture_dir=fixture_dir))
    with TestClient(app) as client:
        alert_id = create_5m_alert(client, cooldown_ms=60_000)
        for i, price in enumerate(["100", "100", "130"]):
            client.portal.call(app.state.runtime.supervisor.ingest, kline(BASE + i*5*MINUTE, price, timeframe="5m"))
        client.portal.call(app.state.runtime.alerts.flush)
        assert client.get("/api/alerts/" + alert_id).json()["status"] == "COOLDOWN"
        assert not app.state.runtime.supervisor.has_channel(BTC, "5m")


@pytest.mark.regression
def test_evicted_alert_reestablishes_baseline_after_quota_returns(tmp_path, fixture_dir):
    app = create_app(replace(settings(tmp_path, quota=1), fixture_dir=fixture_dir))
    with TestClient(app) as client:
        alert_id = create_5m_alert(client)
        runtime = app.state.runtime
        for ts in (BASE - 5*MINUTE, BASE):
            client.portal.call(runtime.supervisor.ingest, kline(ts, timeframe="5m"))
        with client.websocket_connect("/ws/desk") as ws:
            ws.receive_json()
            ws.send_json({"op": "subscribe", "instrument_id": ETH, "timeframe": "1m"})
            while ws.receive_json()["type"] != "health":
                pass
        assert runtime.supervisor.has_channel(BTC, "5m")
        client.portal.call(runtime.supervisor.ingest, kline(BASE + 5*MINUTE, "150", timeframe="5m"))
        assert client.get("/api/alerts/triggers", params={"alert_id": alert_id}).json()["triggers"] == []


@pytest.mark.regression
@pytest.mark.parametrize("cold_store", [False, True])
def test_confirming_after_twenty_thousand_previews_keeps_event_loop_responsive(tmp_path, cold_store, record_property):
    from time import perf_counter
    config = settings(tmp_path, cold_store=cold_store)
    app = create_app(config)
    async def run():
        async with app.router.lifespan_context(app), asgi_socket(app) as ws:
            await ws.send(json.dumps(request()))
            await ws.recv()
            runtime = app.state.runtime
            for i in range(20_000):
                runtime.supervisor.ingest(kline(value=str(i + 1), confirmed=False))
            stalls = []
            async def heartbeat():
                while True:
                    start = perf_counter()
                    await asyncio.sleep(.001)
                    stalls.append(perf_counter() - start)
            timer = asyncio.create_task(heartbeat())
            await asyncio.sleep(0)
            try:
                runtime.supervisor.ingest(kline())
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                    message, response = await asyncio.wait_for(asyncio.gather(
                        ws.recv(), client.get("/api/bars", params=range_params(BASE)),
                    ), 3)
                assert response.json()["bars"] == [json.loads(message)["bar"]]
                await asyncio.sleep(.002)
                record_property("max_stall_ms", round(max(stalls) * 1000, 2))
                assert max(stalls) < .015
            finally:
                timer.cancel()
    asyncio.run(run())


@pytest.mark.regression
def test_new_desk_connection_sees_current_alert_quota_denials_and_recovery(tmp_path):
    app = create_app(settings(tmp_path, quota=1))
    with TestClient(app) as client, client.websocket_connect("/ws/desk") as chart:
        chart.receive_json()
        chart.send_json({"op": "subscribe", "instrument_id": BTC, "timeframe": "1m"})
        assert chart.receive_json()["type"] == "health"
        alert_id = create_5m_alert(client)
        with client.websocket_connect("/ws/desk") as desk:
            assert desk.receive_json()["denied_alerts"] == [alert_id]
            chart.send_json({"op": "unsubscribe", "instrument_id": BTC, "timeframe": "1m"})
            assert desk.receive_json() == {"type": "alert_quota", "denied_alerts": []}


@pytest.mark.regression
def test_partial_range_retries_original_cursor_before_delivering_sorted_revisions(tmp_path, monkeypatch):
    """先返回一页，再报告下一页回填失败；恢复时第一段也可能已补到更老的柱。"""
    from talaria import sdk
    from talaria.foundation.bars import RangeBar
    from talaria.service_endpoint import DATA_DIR_HEADER, data_dir_id
    from websockets.exceptions import ConnectionClosedError

    requests = []
    attempts = 0
    ready = {"epoch": "first", "seq": 0, "server_ts": BASE, "started_ts": BASE}
    @asynccontextmanager
    async def connect(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        class Socket:
            async def send(self, message):
                requests.append(json.loads(message)["after"])
            async def recv(self):
                if attempts == 1:
                    if self.handshake:
                        raise ConnectionClosedError(None, None)
                    self.handshake = True
                    return json.dumps({"type": "ready", **ready})
                return json.dumps({**ready, "type": "resync", "epoch": "next", "seq": 9})
            handshake = False
            async def close(self, code):
                pass
        yield Socket()

    def serve(request):
        start = int(request.url.params["start"])
        end = int(request.url.params["end"])
        if start == 0 and end == BASE - MINUTE:
            return httpx.Response(400, json={"code": "BARS_RANGE_TOO_LARGE"})
        if start and attempts == 2:
            return httpx.Response(503)
        rows = [] if start else [raw_bar(2 * MINUTE)]
        if not start and attempts >= 3:
            rows.insert(0, raw_bar(MINUTE))
        return httpx.Response(200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path)}, json={
            "instrument_id": BTC, "timeframe": "1m", "generation": 1,
            "bars": [RangeBar.model_validate(row, from_attributes=True).model_dump(mode="json") for row in rows],
        })
    install_sdk_transport(monkeypatch, None, connect, transport=httpx.MockTransport(serve))
    async def run():
        async with aclosing(sdk.subscribe(BTC, "1m", data_dir=tmp_path)) as events:
            first = await anext(events)
            assert requests == [None, {"epoch": "first", "seq": 0}, {"epoch": "first", "seq": 0}]
            assert [first.bar.ts_event, (await anext(events)).bar.ts_event] == [MINUTE, 2 * MINUTE]
    asyncio.run(asyncio.wait_for(run(), 3))


@pytest.mark.regression
def test_last_release_discards_journal_while_range_worker_is_reading(tmp_path, monkeypatch):
    from threading import Event
    from talaria.perception.cold_store import ColdReader

    app = create_app(settings(tmp_path))
    async def run():
        async with app.router.lifespan_context(app):
            entered, finish = Event(), Event()
            read = ColdReader.read_manifest_revisions
            def hold(*args, **kwargs):
                entered.set()
                assert finish.wait(3)
                return read(*args, **kwargs)
            try:
                async with asgi_socket(app) as ws:
                    await ws.send(json.dumps(request()))
                    ready = json.loads(await ws.recv())
                    monkeypatch.setattr(ColdReader, "read_manifest_revisions", hold)
                    app.state.runtime.supervisor.ingest(kline())
                    assert await asyncio.to_thread(entered.wait, 1)
            finally:
                finish.set()
            async with asgi_socket(app) as resumed:
                await resumed.send(json.dumps(request(after={"seq": ready["seq"], "epoch": ready["epoch"]})))
                assert json.loads(await resumed.recv())["type"] == "resync"
    asyncio.run(asyncio.wait_for(run(), 4))


@pytest.mark.regression
def test_restart_with_renumbered_identical_content_delivers_only_actual_changes(tmp_path, monkeypatch):
    from talaria import sdk

    config = settings(tmp_path)
    app = create_app(config)
    async def run():
        socket = None
        @asynccontextmanager
        async def connect(*a, **kw):
            nonlocal socket
            async with asgi_socket(app) as socket:
                yield socket
        install_sdk_transport(monkeypatch, app, connect)
        async with aclosing(sdk.subscribe(BTC, "1m", data_dir=config.data_dir)) as events:
            async with app.router.lifespan_context(app):
                first = asyncio.create_task(anext(events))
                await wait_for_channel(app.state.runtime, "1m")
                app.state.runtime.supervisor.ingest(kline(confirmed=False))
                app.state.runtime.supervisor.ingest(kline())
                assert (await first).bar.revision == 1
                await socket.disconnect()
            async with app.router.lifespan_context(app):
                next_event = asyncio.create_task(anext(events))
                await wait_for_channel(app.state.runtime, "1m")
                for value in ("100", "101", "100"):
                    app.state.runtime.supervisor.ingest(kline(value=value))
                assert (await next_event).bar.close == Decimal(101)
                # 冷库可表示同毫秒内变回原值的另一修订，也必须交付。
                assert (await anext(events)).bar.revision == 2
    asyncio.run(asyncio.wait_for(run(), 4))


@pytest.mark.regression
def test_live_range_keeps_confirmations_evicted_while_read_is_queued(tmp_path, monkeypatch):
    from talaria.perception.hot_buffer import HotStreamBuffer

    original = HotStreamBuffer.__init__
    monkeypatch.setattr(HotStreamBuffer, "__init__", lambda self, **kw: original(
        self, **kw, max_bars_per_series=1,
    ))
    app = create_app(settings(tmp_path))
    async def run():
        async with app.router.lifespan_context(app), asgi_socket(app) as ws:
            await ws.send(json.dumps(request()))
            await ws.recv()
            for ts in (BASE, BASE + MINUTE):
                app.state.runtime.supervisor.ingest(kline(ts))
            assert [json.loads(await ws.recv())["bar"]["ts_event"] for _ in range(2)] == [BASE, BASE + MINUTE]
    asyncio.run(asyncio.wait_for(run(), 2))
