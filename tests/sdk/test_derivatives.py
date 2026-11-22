"""data-access/08 的离线查询、修订与结算契约。"""

import asyncio
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from talaria import sdk
from talaria.components.runtime import run_history
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.errors import ColdStoreQualityError
from talaria.foundation.feed import DerivativeTickEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception import cold_store
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher

pytestmark = [pytest.mark.medium, pytest.mark.spec]
INSTRUMENT = "BTC-USDT-PERP"


def test_offline_queries_preserve_settlement_precision_and_select_visible_mark_revision(
    tmp_path, fixture_dir, monkeypatch,
):
    # data-access/08, ADR-0017 与 ADR-0018
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    funding = next(
        row.to_point(INSTRUMENT)
        for row in FixtureConnector(fixture_dir).fetch_funding_settlements(symbol=spec.symbol)
        if row.value == Decimal("0.00005")
    )
    ts = funding.ts_event
    mark = DerivativePoint(
        instrument_id=INSTRUMENT, kind=DerivativeKind.MARK_PRICE, timeframe="1m",
        value=Decimal("100.1"), ts_event=ts, ts_available=ts + 60_000,
        ts_ingested=ts + 60_000, ts_committed=ts + 60_000,
    )
    revised = mark.model_copy(update={
        "value": Decimal("100.2"), "revision": 1, "ts_available": ts + 61_000,
        "ts_ingested": ts + 61_000, "ts_committed": ts + 61_000,
    })
    store = cold_store.ColdStore(tmp_path)
    try:
        generation = store.publish_derivatives(spec, DerivativeKind.FUNDING_RATE, [funding])
        store.publish_derivatives(spec, DerivativeKind.MARK_PRICE, [mark])
        store.publish_derivatives(spec, DerivativeKind.MARK_PRICE, [revised])
        price = Decimal("100.1")
        store.publish(cold_store.ColdBatch(spec, "1m", [RawBar(
            symbol=spec.symbol, timeframe="1m", ts_event=ts,
            open=price, high=price, low=price, close=price, volume=Decimal(1), turnover=price,
            ts_available=ts + 60_000, ts_ingested=ts + 60_000, ts_committed=ts + 60_000,
        )]))
        store.compact([spec])
    finally:
        store.close()

    monkeypatch.setenv("TALARIA_DATA_DIR", str(tmp_path))
    assert list(sdk.derivatives(INSTRUMENT, "mark_price", ts, ts, ts + 59_999)) == []
    assert sdk.derivatives(INSTRUMENT, "mark_price", ts, ts)[0].value == Decimal("100.2")
    [step] = run_history(
        lambda ctx: ctx.derivatives("mark_price")[0].value,
        INSTRUMENT, "1m", start=ts, end=ts + 60_000, data_dir=tmp_path,
    )
    assert step.output == Decimal("100.1")
    assert not sdk.funding_settlements(INSTRUMENT, ts, ts, ts - 1)
    [settlement] = sdk.funding_settlements(INSTRUMENT, ts, ts, ts)
    assert (settlement.ts_event, settlement.value, settlement.ts_available) == (
        ts, Decimal("0.00005"), ts,
    )
    schema = pq.read_metadata(tmp_path / generation.parquet_path).schema
    column = schema.column(schema.names.index("value"))
    assert (column.physical_type, column.precision, column.scale) == ("INT64", 18, 8)


@pytest.mark.parametrize("invalid", ["0.000000001", "10000000000"])
def test_derivative_writer_rejects_entire_out_of_precision_batch(tmp_path, invalid):
    # ADR-0017: reject one value with excess scale or more than 18 digits.
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    point = DerivativePoint(
        instrument_id=INSTRUMENT, kind=DerivativeKind.FUNDING_RATE, value=Decimal("0.00005"),
        ts_event=1, ts_available=1, ts_ingested=1, ts_committed=1,
    )
    store = cold_store.ColdStore(tmp_path)
    try:
        with pytest.raises(ColdStoreQualityError):
            store.publish_derivatives(spec, point.kind, [
                point, point.model_copy(update={"value": Decimal(invalid), "revision": 1}),
            ])
        assert not sdk.derivatives(INSTRUMENT, point.kind, 0, data_dir=tmp_path)
    finally:
        store.close()


@pytest.mark.regression
@pytest.mark.parametrize("history_first", [False, True])
def test_ticker_confirmation_and_history_agree_on_settlement_visibility(
    tmp_path, fixture_dir, history_first,
):
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    fixture = FixtureConnector(fixture_dir)
    actual = next(
        row for row in fixture.fetch_funding_settlements(symbol=spec.symbol)
        if row.value == Decimal("0.00005")
    )
    boundary = actual.ts_event
    now = boundary - 1

    # REST 历史只返回该结算点，另一个费率来自尚未结算的 ticker。
    class SettlementConnector(FixtureConnector):
        def fetch_funding_settlements(self, *, symbol, end_ts=None, limit=200):
            if end_ts is not None and end_ts < boundary:
                return []
            return [replace(actual, ts_ingested=now, ts_committed=now)]

    async def ingest(store):
        nonlocal now
        loop = asyncio.get_running_loop()
        published = asyncio.Event()

        def on_publish(instrument, kind, start, end):
            if kind is DerivativeKind.FUNDING_RATE:
                loop.call_soon_threadsafe(published.set)

        publisher = ColdPublisher(
            store, instruments=[spec], connector=SettlementConnector(fixture_dir),
            policy=BackfillPolicy(), now_ms=lambda: now, on_derivative_publish=on_publish,
        )
        try:
            publisher.ingest_derivatives(DerivativeTickEvent(
                instrument_id=INSTRUMENT, ts_event=now, ts_ingested=now,
                mark_price=Decimal("100.1"), funding_rate=Decimal("0.00004"),
                next_funding_ts=boundary, open_interest=Decimal("123.456"),
            ))
            assert not sdk.funding_settlements(INSTRUMENT, boundary, data_dir=tmp_path)
            now = boundary + 100
            async def backfill():
                future = publisher.request_derivative_history(spec, ("1m",), cutoff_ts=now)
                assert future is not None
                await asyncio.wrap_future(future)

            if history_first:
                await backfill()
            publisher.ingest_derivatives(DerivativeTickEvent(
                instrument_id=INSTRUMENT, ts_event=boundary, ts_ingested=now,
                funding_rate=Decimal("0.00003"), next_funding_ts=boundary + 28_800_000,
            ))
            if not history_first:
                async with asyncio.timeout(2):
                    await published.wait()
                assert not sdk.funding_settlements(INSTRUMENT, boundary, as_of=boundary, data_dir=tmp_path)
                [confirmed] = sdk.funding_settlements(INSTRUMENT, boundary, as_of=now, data_dir=tmp_path)
                assert confirmed.ts_available == now
            await backfill()
            await backfill()
        finally:
            await publisher.close()

    store = cold_store.ColdStore(tmp_path)
    try:
        asyncio.run(ingest(store))
    finally:
        store.close()

    assert not sdk.funding_settlements(INSTRUMENT, boundary, as_of=boundary - 1, data_dir=tmp_path)
    [point] = sdk.funding_settlements(INSTRUMENT, boundary, as_of=boundary, data_dir=tmp_path)
    assert point.value == Decimal("0.00005")
    assert point.ts_available == boundary
    assert point.revision == (0 if history_first else 1)
    rows = sdk.derivatives(INSTRUMENT, "funding_rate", boundary - 1, boundary, data_dir=tmp_path)
    assert [row.value for row in rows] == [
        Decimal("0.00004"), Decimal("0.00003"), Decimal("0.00005"),
    ]
    assert sdk.derivatives(INSTRUMENT, "open_interest", boundary - 1, boundary, data_dir=tmp_path)[0].value == Decimal("123.456")


@pytest.mark.regression
@pytest.mark.parametrize("scenario", ["subscription", "retry", "reconcile"])
def test_settlements_are_recovered_without_a_ticker_at_the_boundary(
    tmp_path, fixture_dir, monkeypatch, scenario,
):
    # data-access/08 审查：模拟 REST 暂时缺少结算，ticker 跳到下一结算点。
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    fixture = FixtureConnector(fixture_dir)
    actual = fixture.fetch_funding_settlements(symbol=spec.symbol)[-1]
    boundary = actual.ts_event
    interval = 28_800_000
    now = boundary - 1
    failed = asyncio.Event()
    published = asyncio.Event()
    recovery_enabled = False
    monkeypatch.setattr("talaria.perception.derivative_publisher.FLUSH_SECONDS", 0.01)
    monkeypatch.setattr("talaria.perception.derivative_publisher.SETTLEMENT_RETRY_SECONDS", 0.02)
    monkeypatch.setattr(
        "talaria.perception.derivative_publisher.SETTLEMENT_RECONCILE_SECONDS",
        0.02 if scenario == "reconcile" else 3600, raising=False,
    )

    async def run():
        nonlocal now, recovery_enabled
        loop = asyncio.get_running_loop()

        class SettlementConnector(FixtureConnector):
            def fetch_funding_settlements(self, *, symbol, end_ts=None, limit=200):
                if end_ts < boundary:
                    return []
                if limit == 1 and end_ts == boundary:
                    if scenario == "subscription" or (scenario == "retry" and recovery_enabled):
                        return [actual]
                    loop.call_soon_threadsafe(failed.set)
                if limit > 1 and scenario == "reconcile" and recovery_enabled:
                    return [actual]
                return []

        def on_publish(instrument, kind, start, end):
            if kind == DerivativeKind.FUNDING_RATE and start <= boundary <= end:
                loop.call_soon_threadsafe(published.set)

        def tick(next_boundary):
            publisher.ingest_derivatives(DerivativeTickEvent(
                instrument_id=INSTRUMENT, ts_event=now, ts_ingested=now, next_funding_ts=next_boundary,
            ))

        with closing(cold_store.ColdStore(tmp_path)) as store:
            publisher = ColdPublisher(
                store, instruments=[spec], connector=SettlementConnector(fixture_dir),
                policy=BackfillPolicy(target_bars=1), now_ms=lambda: now,
                on_derivative_publish=on_publish, funding_intervals_ms={INSTRUMENT: interval},
            )
            publisher.backfill_subscription(spec, "1m", now)
            if scenario != "subscription":
                tick(boundary)
            now = boundary + 1
            tick(boundary + interval)
            flusher = asyncio.create_task(publisher.flush_derivatives_periodically())
            try:
                async with asyncio.timeout(1):
                    if scenario != "subscription":
                        await failed.wait()
                        if scenario == "retry":
                            now = boundary + interval + 1
                        recovery_enabled = True
                    await published.wait()
                [point] = sdk.funding_settlements(INSTRUMENT, boundary, boundary, as_of=now, data_dir=tmp_path)
                assert point.value == actual.value
                assert point.ts_available == (boundary if scenario == "reconcile" else now)
            finally:
                flusher.cancel()
                await asyncio.gather(flusher, return_exceptions=True)
                await publisher.close()

    asyncio.run(run())


@pytest.mark.regression
@pytest.mark.parametrize("online", [False, True])
def test_sdk_default_cutoffs_use_service_clock_or_local_clock(tmp_path, monkeypatch, online):
    # data-access/04、08：服务和离线时钟都早于已归档的未来可见点。
    from types import SimpleNamespace

    import httpx

    from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id

    now = 1000
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    points = [
        DerivativePoint(
            instrument_id=INSTRUMENT, kind=DerivativeKind.MARK_PRICE,
            value=Decimal("100.1"), ts_event=ts, ts_available=available,
            ts_ingested=available, ts_committed=available,
        )
        for ts, available in [(now - 2, now - 2), (now - 1, now + 1), (now + 1, now + 1)]
    ]
    with closing(cold_store.ColdStore(tmp_path)) as store:
        store.publish_derivatives(spec, DerivativeKind.MARK_PRICE, points)

    def get_health(request):
        if request.url.path == "/api/derivatives/backfill":
            return httpx.Response(204, headers={DATA_DIR_HEADER: data_dir_id(tmp_path)})
        assert request.url.path == "/api/health"
        return httpx.Response(200, headers={
            DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(now),
        })

    client = httpx.Client
    monkeypatch.setattr(sdk, "read_service_endpoint", lambda root: "http://service" if online else None)
    monkeypatch.setattr(sdk, "time", SimpleNamespace(time_ns=lambda: (now + 9 if online else now) * 10**6))
    monkeypatch.setattr(sdk.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(get_health), **kw))

    assert [point.ts_event for point in sdk.derivatives(INSTRUMENT, "mark_price", 0, data_dir=tmp_path)] == [now - 2]
    assert [point.ts_event for point in sdk.derivatives(
        INSTRUMENT, "mark_price", 0, end=now + 9, data_dir=tmp_path,
    )] == [now - 2]
    assert [point.ts_event for point in sdk.derivatives(
        INSTRUMENT, "mark_price", 0, as_of=now + 9, data_dir=tmp_path,
    )] == [now - 2, now - 1]


@pytest.mark.regression
def test_online_sdk_backfills_an_interior_settlement_gap_before_reading(tmp_path, fixture_dir, monkeypatch):
    # data-access/03、08：用进程内常驻服务验证真实回填路径。
    import httpx
    from fastapi.testclient import TestClient

    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    first, missing, last = [
        row.to_point(INSTRUMENT)
        for row in FixtureConnector(fixture_dir).fetch_funding_settlements(symbol=spec.symbol, limit=3)
    ]
    with closing(cold_store.ColdStore(tmp_path)) as store:
        store.publish_derivatives(spec, first.kind, [first])
        store.publish_derivatives(spec, last.kind, [last])
    assert [p.ts_event for p in sdk.funding_settlements(
        INSTRUMENT, first.ts_event, last.ts_event, data_dir=tmp_path,
    )] == [first.ts_event, last.ts_event]

    config = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        now_ms=lambda: last.ts_event + 1, features=FeatureToggles(scanner=False),
    )
    client = httpx.Client
    monkeypatch.setattr(sdk, "read_service_endpoint", lambda root: "http://service")
    with TestClient(create_app(config)) as service:
        def request_to_service(request):
            return service.get(request.url.raw_path.decode())

        monkeypatch.setattr(sdk.httpx, "Client", lambda **kw: client(
            transport=httpx.MockTransport(request_to_service), **kw,
        ))
        points = sdk.funding_settlements(INSTRUMENT, first.ts_event, last.ts_event, data_dir=tmp_path)
    assert [p.ts_event for p in points] == [first.ts_event, missing.ts_event, last.ts_event]
    assert points[1].value == missing.value


@pytest.mark.regression
@pytest.mark.parametrize("published_offset", [None, 50])
def test_settlement_identity_uses_event_time_independently_of_publication(tmp_path, fixture_dir, published_offset):
    # data-access/08：同一事件毫秒内，ticker 预估和实际结算独立，发布时刻只是元数据。
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    fixture = FixtureConnector(fixture_dir)
    raw = fixture.fetch_funding_settlements(symbol=spec.symbol, limit=1)[0]
    boundary = raw.ts_event
    published = None if published_offset is None else boundary + published_offset
    actual = replace(raw, ts_published=published)

    class SettlementConnector(FixtureConnector):
        def fetch_funding_settlements(self, *, symbol, end_ts=None, limit=200):
            return [actual] if symbol == spec.symbol and end_ts >= boundary else []

    async def run(store):
        publisher = ColdPublisher(
            store, instruments=[spec], connector=SettlementConnector(fixture_dir),
            policy=BackfillPolicy(target_bars=1), now_ms=lambda: boundary + 200,
        )
        publisher.ingest_derivatives(DerivativeTickEvent(
            instrument_id=INSTRUMENT, ts_event=boundary, ts_ingested=boundary,
            funding_rate=Decimal("0.00001"),
        ))
        try:
            await asyncio.wrap_future(publisher.request_derivative_history(spec, (), cutoff_ts=boundary))
        finally:
            await publisher.close()

    with closing(cold_store.ColdStore(tmp_path)) as store:
        asyncio.run(run(store))
        store.compact([spec])

    [settled] = sdk.funding_settlements(INSTRUMENT, boundary, boundary, data_dir=tmp_path)
    assert (settled.ts_event, settled.ts_published, settled.value) == (boundary, published, raw.value)
    estimate, actual_point = sdk.derivatives(
        INSTRUMENT, "funding_rate", boundary, boundary, data_dir=tmp_path,
    )
    assert not estimate.is_funding_settlement
    assert estimate.value == Decimal("0.00001")
    assert actual_point.revision == 0
    assert actual_point == settled


@pytest.mark.regression
@pytest.mark.parametrize(("kind", "timeframe"), [("mark_price", "1m"), ("open_interest", "5m")])
def test_online_sdk_reads_supported_history_when_some_fixture_timeframes_are_absent(
    tmp_path, fixture_dir, monkeypatch, kind, timeframe,
):
    # data-access/08：SDK 查询类别时，可用周期的历史应完成回填。
    import httpx
    from fastapi.testclient import TestClient

    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.settings import AppSettings

    fixture = FixtureConnector(fixture_dir)
    fetch = fixture.fetch_mark_price_bars if kind == "mark_price" else fixture.fetch_open_interest
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    [point] = fetch(symbol=spec.symbol, timeframe=timeframe, end_ts=1_793_550_000_000, limit=1)
    config = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
        now_ms=lambda: point.ts_available + 86_400_000, features=FeatureToggles(scanner=False),
    )
    client = httpx.Client
    monkeypatch.setattr(sdk, "read_service_endpoint", lambda root: "http://service")
    with TestClient(create_app(config)) as service:
        transport = httpx.MockTransport(lambda request: service.get(request.url.raw_path.decode()))
        monkeypatch.setattr(sdk.httpx, "Client", lambda **kw: client(transport=transport, **kw))
        result = sdk.derivatives(INSTRUMENT, kind, point.ts_event - 3_600_000, point.ts_event, data_dir=tmp_path)
    assert (timeframe, point.ts_event, point.value) in [
        (row.timeframe, row.ts_event, row.value) for row in result
    ]
