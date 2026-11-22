from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from threading import Event, current_thread

import httpx
import pytest
from anyio import to_thread
from fastapi.testclient import TestClient

from talaria import sdk
from talaria.foundation.bars import MAX_RANGE_BARS, CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import KlineEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.packs.testing import FixtureConnector
from talaria.perception.cold_store import ColdBatch, ColdReader, ColdStore
from talaria.workbench_api.app import create_app
from talaria.workbench_api.routes import bars as bars_route
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import service_descriptor_path, write_service_descriptor
from talaria.workbench_api.service_lock import ServiceLock
from talaria.workbench_api.settings import AppSettings

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
TIMEFRAME = "1m"
MINUTE = 60_000
START = 1_795_045_500_000


class Market:
    def __init__(self, tmp_path: Path, fixture_dir: Path | None) -> None:
        self.now = START
        # Use an idle replay in place of a live exchange connection.
        replay = tmp_path / "events.jsonl"
        replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
        self.settings = AppSettings(
            data_dir=tmp_path / "data", dist_dir=tmp_path / "dist", fixture_dir=fixture_dir,
            replay_events=replay, now_ms=lambda: self.now,
            features=FeatureToggles(alerts=False, scanner=False),
        )
        self.app = create_app(self.settings)

    def archive(self, *bars: RawBar) -> None:
        with closing(ColdStore(self.settings.data_dir)) as store:
            store.publish(ColdBatch(INSTRUMENT, TIMEFRAME, bars))

    def ingest(self, client: TestClient, *events: KlineEvent) -> None:
        async def deliver() -> None:
            supervisor = self.app.state.runtime.supervisor
            supervisor.subscribe(instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME)
            for event in events:
                supervisor.ingest(event)

        client.portal.call(deliver)


def _raw(timestamp: int, close: str, *, revision: int = 0, available: int | None = None) -> RawBar:
    value = Decimal(close)
    ts_available = timestamp + MINUTE if available is None else available
    return RawBar(
        symbol=INSTRUMENT.symbol, timeframe=TIMEFRAME, ts_event=timestamp,
        open=value, high=value, low=value, close=value, volume=Decimal("1"), turnover=value,
        ts_available=ts_available, ts_ingested=ts_available, ts_committed=ts_available,
        revision=revision,
    )


def _event(timestamp: int, close: str, *, confirmed: bool) -> KlineEvent:
    bar: CompactBar = _raw(timestamp, close).to_compact()
    return KlineEvent(
        instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME,
        bar=bar, confirmed=confirmed, revision=0,
    )


def _get(client: TestClient, start: int, end: int, **options) -> list[dict]:
    response = client.get("/api/bars", params={
        "instrument": INSTRUMENT.instrument_id, "timeframe": TIMEFRAME,
        "start": start, "end": end, **options,
    })
    assert response.status_code == 200, response.text
    return response.json()["bars"]


def _disk_full(*args, **kwargs):
    raise OSError("test cold write failure")


@contextmanager
def _published_endpoint(market: Market):
    with ServiceLock(market.settings.data_dir) as lock:
        write_service_descriptor(
            service_descriptor_path(market.settings.data_dir), base_url="http://127.0.0.1:8765",
            token="test-token", pid=lock.pid, features=market.settings.features,
        )
        yield


def test_bars_span_cold_and_unpublished_hot_ranges(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: hold the last confirmed bar before persistence at the cold writer seam.
    market = Market(tmp_path, fixture_dir)
    tail = START + MINUTE
    market.archive(_raw(START, "100.5"))
    market.now = tail + MINUTE

    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with TestClient(market.app) as client:
        market.ingest(
            client, _event(START, "100.5", confirmed=True), _event(tail, "101.2", confirmed=True),
        )
        bars = _get(client, START, tail)
        assert [bar["ts_event"] for bar in bars] == [START, tail]
        assert [bar["confirmed"] for bar in bars] == [True, True]
        assert [bar["revision"] for bar in bars] == [0, 0]
        assert all(isinstance(bar["close"], str) for bar in bars)
        assert [Decimal(bar["close"]) for bar in bars] == [Decimal("100.5"), Decimal("101.2")]
        assert _get(client, tail, tail) == bars[1:]


def test_sdk_matches_range_with_unpublished_correction_and_excludes_open_bar(
    tmp_path, fixture_dir, monkeypatch,
):
    # data-access/04: use the running HTTP app at the transport seam without binding TCP.
    market = Market(tmp_path, fixture_dir)
    second = START + MINUTE
    end = second + MINUTE
    market.archive(_raw(START, "100"), _raw(second, "101"))
    market.now = end + 2_000
    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with _published_endpoint(market), TestClient(market.app) as client:
        market.ingest(client, _event(START, "102.3", confirmed=True), _event(end, "103", confirmed=False))

        def serve_sdk_request(transport, request):
            assert request.url.host == "127.0.0.1"
            response = client.send(request)
            if request.url.path == "/api/bars":
                assert [bar["ts_event"] for bar in response.json()["bars"]] == (
                    [START] if int(request.url.params.get("as_of", market.now)) == market.now else []
                )
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        for as_of in (None, market.now - 1):
            options = {} if as_of is None else {"as_of": as_of}
            expected = _get(client, START, end, **options)
            result = sdk.bars(
                INSTRUMENT.instrument_id, TIMEFRAME, START, end, as_of,
                data_dir=market.settings.data_dir,
            )
            assert result.tail_completed
            assert [bar.model_dump(mode="json") for bar in result] == expected
            assert [(bar.close, bar.revision) for bar in result] == [
                (Decimal("102.3"), 1) if as_of is None else (Decimal("100"), 0),
                (Decimal("101"), 0),
            ]
        local = sdk.bars(
            INSTRUMENT.instrument_id, TIMEFRAME, START, end, data_dir=market.settings.data_dir, offline=True,
        )
        assert not local.tail_completed and [bar.close for bar in local] == [Decimal("100"), Decimal("101")]


def test_sdk_backfills_empty_cold_store_then_reads_it_after_service_stops(
    tmp_path, fixture_dir, monkeypatch,
):
    # data-access/04: fixture REST runs through the service's real backfill and cold writer.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    market.now = end + MINUTE
    monkeypatch.setenv("TALARIA_DATA_DIR", str(market.settings.data_dir))
    assert not market.settings.data_dir.exists()
    def exchange_history(connector, **kwargs):
        return [_raw(START, "100.5"), _raw(end, "101.2")]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", exchange_history)
    with _published_endpoint(market), TestClient(market.app) as client:
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _, request: client.send(request))
        online = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, end)
        assert online.tail_completed
        expected = _get(client, START, end)
        assert [bar.model_dump(mode="json") for bar in online] == expected
        assert [bar.close for bar in online] == [Decimal("100.5"), Decimal("101.2")]

    def service_stopped(transport, request):
        raise httpx.ConnectError("Connection refused", request=request)

    # Simulate an ungraceful shutdown with the published descriptor still on disk.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", service_stopped)
    local = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, end)
    assert not local.tail_completed
    assert tuple(local) == tuple(online)


@pytest.mark.regression
def test_sdk_retries_tail_when_manifest_advances_after_response(tmp_path, fixture_dir, monkeypatch):
    # data-access/04 and ADR-0018: interleave a failed hot write and a successful cold write
    # between the service's response and the SDK's manifest read.
    market = Market(tmp_path, fixture_dir)
    second = START + MINUTE
    market.archive(_raw(START, "100"), _raw(second, "200"))
    market.now = second + MINUTE + 1_000
    publish = ColdStore.publish
    published = Event()

    def publish_correction(store, batch):
        if batch.bars[0].close == Decimal("101"):
            _disk_full()
        result = publish(store, batch)
        if batch.bars[0].close == Decimal("201"):
            published.set()
        return result

    monkeypatch.setattr(ColdStore, "publish", publish_correction)
    with _published_endpoint(market), TestClient(market.app) as client:
        assert [bar["close"] for bar in _get(client, START, second)] == ["100", "200"]

        def serve_sdk_request(transport, request):
            response = client.send(request)
            if request.url.path == "/api/bars" and not published.is_set():
                market.ingest(client, _event(START, "101", confirmed=True))
                market.ingest(client, _event(second, "201", confirmed=True))
                assert published.wait(5)
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, second, data_dir=market.settings.data_dir)
        assert result.tail_completed
        assert [bar.model_dump(mode="json") for bar in result] == _get(client, START, second)
        assert [bar.close for bar in result] == [Decimal("101"), Decimal("201")]


@pytest.mark.regression
def test_sdk_preserves_hot_tail_when_another_series_advances_manifest(tmp_path, fixture_dir, monkeypatch):
    # data-access/09: delay the real cold write while driving the SDK through the ASGI range route.
    market = Market(tmp_path, fixture_dir)
    tail = START + MINUTE
    market.archive(_raw(START, "100"))
    market.now = tail + MINUTE
    eth = CRYPTO_PERP_PACK.instrument("ETH-USDT-PERP")
    release = Event()
    publish = ColdStore.publish

    def delay_btc(store, batch):
        if batch.instrument == INSTRUMENT:
            assert release.wait(5)
        return publish(store, batch)

    monkeypatch.setattr(ColdStore, "publish", delay_btc)
    with _published_endpoint(market), TestClient(market.app) as client:
        market.ingest(client, _event(tail, "101", confirmed=True))
        eth_slots = iter((START, tail))

        def serve_sdk_request(transport, request):
            response = client.send(request)
            if request.url.path == "/api/bars":
                assert [bar["ts_event"] for bar in response.json()["bars"]] == [tail]
                row = replace(_raw(next(eth_slots), "200"), symbol=eth.symbol)
                market.app.state.runtime.cold_store.publish(ColdBatch(eth, TIMEFRAME, (row,)))
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        try:
            result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, tail, data_dir=market.settings.data_dir)
            assert [(bar.ts_event, bar.close) for bar in result] == [(START, Decimal("100")), (tail, Decimal("101"))]
            assert result.tail_completed
        finally:
            release.set()

    local = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, tail, data_dir=market.settings.data_dir)
    assert not local.tail_completed
    assert tuple(local) == tuple(result)


@pytest.mark.regression
def test_sdk_reports_retryable_error_when_target_manifest_keeps_changing(tmp_path, fixture_dir, monkeypatch):
    # data-access/09: the real route answers every attempt, but the target file set keeps changing.
    market = Market(tmp_path, fixture_dir)
    market.archive(_raw(START, "100"))
    market.now = START + MINUTE + 2_000
    with _published_endpoint(market), TestClient(market.app) as client:
        revisions = iter((1, 2))

        def serve_sdk_request(transport, request):
            response = client.send(request)
            if request.url.path == "/api/bars":
                assert response.status_code == 200
                revision = next(revisions)
                row = _raw(START, str(100 + revision), revision=revision, available=market.now)
                market.app.state.runtime.cold_store.publish(ColdBatch(INSTRUMENT, TIMEFRAME, (row,)))
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        with pytest.raises(sdk.BarsConsistencyError, match="请重试"):
            sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, START, data_dir=market.settings.data_dir)


@pytest.mark.regression
@pytest.mark.parametrize("keeps_changing", [False, True], ids=["stable-after-retry", "retry-exhausted"])
def test_sdk_requires_one_hot_revision_across_segments(tmp_path, monkeypatch, keeps_changing):
    # data-access/09: A is corrected before B, so A0 and B1 never coexist.
    market = Market(tmp_path, None)
    second = START + MAX_RANGE_BARS * MINUTE
    first = second - MINUTE
    market.now = second + MINUTE + 1_000
    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with _published_endpoint(market), TestClient(market.app) as client:
        market.ingest(client, _event(first, "100", confirmed=True), _event(second, "200", confirmed=True))
        corrections = iter(range(1, 3 if keeps_changing else 2))

        def serve_sdk_request(transport, request):
            response = client.send(request)
            if request.url.path == "/api/bars" and int(request.url.params["start"]) == START:
                revision = next(corrections, None)
                if revision is not None:
                    market.ingest(
                        client, _event(first, str(100 + revision), confirmed=True),
                        _event(second, str(200 + revision), confirmed=True),
                    )
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        with pytest.raises(sdk.BarsConsistencyError, match="请重试") if keeps_changing else nullcontext():
            result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, second, data_dir=market.settings.data_dir)
            assert result.tail_completed
            assert [(bar.ts_event, bar.close, bar.revision) for bar in result] == [
                (first, Decimal("101"), 1), (second, Decimal("201"), 1),
            ]


@pytest.mark.regression
@pytest.mark.parametrize("at_close", [True, False], ids=["as-of-close", "latest"])
def test_sdk_selects_visible_revisions_across_segments_with_different_manifests(tmp_path, monkeypatch, at_close):
    # data-access/09: publish into the second segment after the first segment's real HTTP response.
    market = Market(tmp_path, None)
    boundary = START + MAX_RANGE_BARS * MINUTE
    before_boundary = boundary - MINUTE
    cutoff = boundary + MINUTE
    market.now = cutoff + 1_000
    market.archive(_raw(before_boundary, "100", revision=3))
    eth = CRYPTO_PERP_PACK.instrument("ETH-USDT-PERP")
    release = Event()
    publish = ColdStore.publish

    def delay_correction(store, batch):
        if batch.bars[0].ts_event == before_boundary:
            assert release.wait(5)
        return publish(store, batch)

    monkeypatch.setattr(ColdStore, "publish", delay_correction)
    with _published_endpoint(market), TestClient(market.app) as client:
        market.ingest(client, _event(before_boundary, "101", confirmed=True))
        store = market.app.state.runtime.cold_store
        eth_slots = iter(range(START, START + 4 * MINUTE, MINUTE))
        published_boundary = False

        def serve_sdk_request(transport, request):
            nonlocal published_boundary
            response = client.send(request)
            if request.url.path == "/api/bars":
                assert response.status_code == 200
                if not published_boundary:
                    store.publish(ColdBatch(INSTRUMENT, TIMEFRAME, (
                        _raw(boundary, "200"),
                        _raw(boundary, "201", revision=1, available=market.now),
                    )))
                    published_boundary = True
                row = replace(_raw(next(eth_slots), "300"), symbol=eth.symbol)
                store.publish(ColdBatch(eth, TIMEFRAME, (row,)))
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        try:
            result = sdk.bars(
                INSTRUMENT.instrument_id, TIMEFRAME, START, boundary, as_of=cutoff if at_close else None,
                data_dir=market.settings.data_dir,
            )
            assert result.tail_completed
            assert [(bar.ts_event, bar.close, bar.revision) for bar in result] == (
                [(before_boundary, Decimal("100"), 3), (boundary, Decimal("200"), 0)] if at_close else
                [(before_boundary, Decimal("101"), 4), (boundary, Decimal("201"), 1)]
            )
        finally:
            release.set()


@pytest.mark.regression
@pytest.mark.parametrize("switched_after_health", [False, True])
def test_sdk_rejects_service_using_another_data_directory(tmp_path, fixture_dir, monkeypatch, switched_after_health):
    # data-access/04: a port can now belong to a service serving a different directory.
    local_dir, remote_dir = tmp_path / "local", tmp_path / "remote"
    local_dir.mkdir()
    remote_dir.mkdir()
    local, remote = Market(local_dir, fixture_dir), Market(remote_dir, fixture_dir)
    local.archive(_raw(START, "100"))
    remote.archive(_raw(START, "200"))
    paths = []
    with _published_endpoint(local), TestClient(local.app) as local_client, TestClient(remote.app) as remote_client:
        def serve_sdk_request(transport, request):
            paths.append(request.url.path)
            if switched_after_health and request.url.path == "/api/health":
                return local_client.send(request)
            return remote_client.send(request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, START, data_dir=local.settings.data_dir)

    assert not result.tail_completed
    assert [bar.close for bar in result] == [Decimal("100")]
    assert ("/api/bars" in paths) == switched_after_health


@pytest.mark.regression
@pytest.mark.parametrize("include_open", [False, True])
def test_range_default_end_is_last_closed_bar(tmp_path, fixture_dir, include_open):
    # data-access/04: a replay clock can lag confirmed data already in the manifest.
    market = Market(tmp_path, fixture_dir)
    market.now = START + MINUTE + 30_000
    market.archive(_raw(START, "100"), _raw(START + MINUTE, "200"))
    with TestClient(market.app) as client:
        response = client.get("/api/bars", params={
            "instrument": INSTRUMENT.instrument_id, "timeframe": TIMEFRAME,
            "start": START, "include_open": include_open,
        })
        assert response.status_code == 200
        assert [bar["ts_event"] for bar in response.json()["bars"]] == [START]


@pytest.mark.regression
def test_online_sdk_default_end_uses_service_clock(tmp_path, fixture_dir, monkeypatch):
    # data-access/04: the SDK's wall clock is ahead of the service's replay clock.
    market = Market(tmp_path, fixture_dir)
    market.now = START + MINUTE + 30_000
    market.archive(_raw(START, "100"), _raw(START + MINUTE, "200"))
    monkeypatch.setattr("talaria.sdk.time.time_ns", lambda: (market.now + MINUTE) * 1_000_000)
    with _published_endpoint(market), TestClient(market.app) as client:
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _, request: client.send(request))
        result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, data_dir=market.settings.data_dir)

    assert result.tail_completed
    assert [bar.ts_event for bar in result] == [START]


@pytest.mark.regression
def test_online_sdk_defaults_to_service_clock_for_cold_revisions(tmp_path, monkeypatch):
    # data-access/09: the later cold revision is visible one millisecond after the service clock.
    market = Market(tmp_path, None)
    market.now = 130_000
    market.archive(_raw(MINUTE, "100"), _raw(MINUTE, "101", revision=1, available=130_001))
    with _published_endpoint(market), TestClient(market.app) as client:
        expected = _get(client, MINUTE, MINUTE)
        assert [bar["revision"] for bar in expected] == [0]
        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda _, request: client.send(request))
        result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, MINUTE, MINUTE, data_dir=market.settings.data_dir)

    assert result.tail_completed
    assert [bar.model_dump(mode="json") for bar in result] == expected


@pytest.mark.regression
def test_online_sdk_keeps_same_visibility_cutoff_across_segments(tmp_path, monkeypatch):
    # data-access/09: the service clock crosses a revision's visibility time between segments.
    market = Market(tmp_path, None)
    second = START + MAX_RANGE_BARS * MINUTE
    cutoff = second + MINUTE
    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with _published_endpoint(market), TestClient(market.app) as client:
        market.now = cutoff
        market.ingest(client, _event(second, "100", confirmed=True))
        market.now = cutoff + 1
        market.ingest(client, _event(second, "101", confirmed=True))
        market.now = cutoff

        def serve_sdk_request(transport, request):
            response = client.send(request)
            if request.url.path == "/api/bars":
                market.now = cutoff + 1
            return response

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", serve_sdk_request)
        result = sdk.bars(INSTRUMENT.instrument_id, TIMEFRAME, START, second, data_dir=market.settings.data_dir)

    assert result.tail_completed
    assert [(bar.ts_event, bar.close, bar.revision) for bar in result] == [(second, Decimal("100"), 0)]


def test_as_of_selects_confirmed_revision_after_visibility_cutoff(tmp_path, fixture_dir):
    # data-access/03: Bybit's preview uses the scheduled close timestamp on the wire.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    with TestClient(market.app) as client:
        market.now = START + 1_000
        market.ingest(client, _event(START, "98", confirmed=False))
        market.now = START + 2_000
        market.ingest(client, _event(START, "99", confirmed=False))
        market.now = end + 6_000
        assert _get(client, START, START) == []
        assert _get(client, START, START, include_open=False) == []
        assert _get(client, START, START, include_open=True)[0]["confirmed"] is False

        market.ingest(client, _event(START, "100", confirmed=True))
        market.now = end + 7_000
        market.ingest(client, _event(START, "101", confirmed=True))
        assert _get(client, START, START, as_of=end + 5_999) == []
        preview = _get(client, START, START, as_of=START + 1_000, include_open=True)[0]
        assert (preview["close"], preview["confirmed"], preview["revision"]) == ("98", False, 0)
        assert _get(client, START, START, as_of=end, include_open=True)[0]["close"] == "99"
        first = _get(client, START, START, as_of=end + 6_000)[0]
        assert Decimal(first["close"]) == 100
        assert (first["confirmed"], first["revision"]) == (True, 2)
        latest = _get(client, START, START)[0]
        assert Decimal(latest["close"]) == 101
        assert (latest["confirmed"], latest["revision"]) == (True, 3)

    cold = ColdReader(market.settings.data_dir).read_bars(INSTRUMENT, TIMEFRAME)
    assert [(bar.close, bar.revision) for bar in cold] == [(Decimal("101"), 3)]


def test_restart_correction_continues_archived_revision(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: the durable revision stays stable once the pending correction lands.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    market.archive(_raw(START, "100", revision=3))
    market.now = end + 3_000
    with monkeypatch.context() as patch:
        patch.setattr(ColdStore, "publish", _disk_full)
        with TestClient(market.app) as client:
            market.ingest(client, _event(START, "101", confirmed=True))
            before = _get(client, START, START, as_of=end)[0]
            latest = _get(client, START, START)[0]
            assert (Decimal(before["close"]), before["revision"]) == (Decimal("100"), 3)
            assert (Decimal(latest["close"]), latest["revision"]) == (Decimal("101"), 4)
            assert latest["ts_available"] == market.now

    with TestClient(market.app) as client:
        market.ingest(client, _event(START, "101", confirmed=True))

    cold = ColdReader(market.settings.data_dir)
    first = cold.read_bars(INSTRUMENT, TIMEFRAME, as_of=end)[0]
    latest = cold.read_bars(INSTRUMENT, TIMEFRAME)[0]
    assert (first.close, first.revision) == (Decimal("100"), 3)
    assert (latest.close, latest.revision, latest.ts_available) == (Decimal("101"), 4, market.now)


@pytest.mark.regression
def test_failed_cold_write_keeps_earlier_as_of_revision(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: simulate a failed Parquet write followed by a successful correction.
    market = Market(tmp_path, fixture_dir)
    market.archive(_raw(START, "100", revision=3))
    first_received = START + MINUTE + 1_000
    published = Event()
    publish = ColdStore.publish

    def fail_first_correction(store, batch):
        if batch.bars[0].close == Decimal("101"):
            _disk_full()
        result = publish(store, batch)
        published.set()
        return result

    monkeypatch.setattr(ColdStore, "publish", fail_first_correction)
    with TestClient(market.app) as client:
        market.now = first_received
        market.ingest(client, _event(START, "101", confirmed=True))
        earlier = _get(client, START, START, as_of=first_received)
        assert (Decimal(earlier[0]["close"]), earlier[0]["revision"]) == (Decimal("101"), 4)

        market.now += 1_000
        market.ingest(client, _event(START, "102", confirmed=True))
        assert published.wait(5)
        latest = _get(client, START, START)
        assert (Decimal(latest[0]["close"]), latest[0]["revision"]) == (Decimal("102"), 5)
        assert _get(client, START, START, as_of=first_received) == earlier


@pytest.mark.regression
def test_as_of_keeps_earlier_revision_when_a_later_close_lands_during_read(
    tmp_path, fixture_dir, monkeypatch,
):
    # The query captures the earlier hot version before the cold read can see the later write.
    market = Market(tmp_path, fixture_dir)
    market.archive(_raw(START, "100", revision=3))
    market.now = START + MINUTE + 1_000
    cutoff = market.now
    reading, release, published = Event(), Event(), Event()
    read_revisions, publish = ColdReader.read_manifest_snapshot, ColdStore.publish

    def pause_query_until_later_write(reader, *args, **kwargs):
        if current_thread().name.startswith("asyncio_"):
            reading.set()
            assert release.wait(5)
        return read_revisions(reader, *args, **kwargs)

    def fail_first_correction(store, batch):
        if batch.bars[0].close == Decimal("101"):
            _disk_full()
        result = publish(store, batch)
        published.set()
        return result

    monkeypatch.setattr(ColdReader, "read_manifest_snapshot", pause_query_until_later_write)
    monkeypatch.setattr(ColdStore, "publish", fail_first_correction)
    with TestClient(market.app) as client, ThreadPoolExecutor(1) as requests:
        market.ingest(client, _event(START, "101", confirmed=True))
        pending = requests.submit(_get, client, START, START, as_of=cutoff)
        try:
            assert reading.wait(5)
            market.now += 1_000
            market.ingest(client, _event(START, "102", confirmed=True))
            assert published.wait(5)
        finally:
            release.set()
        bar = pending.result(timeout=5)[0]
        assert (bar["close"], bar["revision"]) == ("101", 4)


@pytest.mark.regression
def test_hot_preview_revision_follows_archived_revision(tmp_path, fixture_dir):
    # data-access/03: a distinct preview and cold confirmation cannot share a revision.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    market.archive(_raw(START, "100"))
    with TestClient(market.app) as client:
        market.now = end - 1_000
        market.ingest(client, _event(START, "99", confirmed=False))
        preview = _get(client, START, START, as_of=market.now, include_open=True)[0]
        confirmed = _get(client, START, START, as_of=end, include_open=True)[0]
        assert (preview["close"], preview["revision"]) == ("99", 1)
        assert (Decimal(confirmed["close"]), confirmed["revision"]) == (Decimal("100"), 0)


@pytest.mark.regression
def test_ws_confirmation_after_skipped_recovery_uses_ws_receipt(tmp_path, fixture_dir, monkeypatch):
    # ADR-0018: REST only fills missing slots, even when WS later agrees with that response.
    market = Market(tmp_path, fixture_dir)
    end = START + MINUTE
    market.archive(_raw(START, "100"))
    attempted, release, published = Event(), Event(), Event()
    publish = ColdStore.publish

    def delay_confirmation(store, batch):
        attempted.set()
        assert release.wait(5)
        result = publish(store, batch)
        published.set()
        return result

    monkeypatch.setattr(ColdStore, "publish", delay_confirmation)
    with TestClient(market.app) as client:
        try:
            market.now = end + 6_000
            market.ingest(client)

            async def reconnect() -> None:
                supervisor = market.app.state.runtime.supervisor
                supervisor.transport_lost()
                supervisor.transport_restored(backfill=[_raw(START, "101", available=market.now)])

            client.portal.call(reconnect)
            earlier = _get(client, START, START, as_of=end + 7_000)
            assert Decimal(earlier[0]["close"]) == 100

            market.now = end + 8_000
            market.ingest(client, _event(START, "101", confirmed=True))
            assert attempted.wait(5)
            assert _get(client, START, START, as_of=end + 7_000) == earlier
            confirmed = _get(client, START, START, as_of=market.now)
            assert (Decimal(confirmed[0]["close"]), confirmed[0]["ts_available"]) == (101, market.now)

            release.set()
            assert published.wait(5)
            assert _get(client, START, START, as_of=end + 7_000) == earlier
            assert _get(client, START, START, as_of=market.now) == confirmed
        finally:
            release.set()


@pytest.mark.regression
def test_exchange_close_replaces_a_synthetic_bar_at_the_grace_cutoff(tmp_path, fixture_dir, monkeypatch):
    # data-access/02: a synthetic zero-volume bar is only a hot fallback.
    market = Market(tmp_path, fixture_dir)
    synthetic_ts = START + MINUTE
    close_ts = synthetic_ts + MINUTE
    monkeypatch.setattr(ColdStore, "publish", _disk_full)
    with TestClient(market.app) as client:
        market.now = synthetic_ts
        market.ingest(client, _event(START, "100", confirmed=True))
        market.now = close_ts + 1_000
        market.ingest(client, _event(close_ts, "102", confirmed=False))
        assert _get(client, synthetic_ts, synthetic_ts) == []
        synthetic = _get(client, synthetic_ts, synthetic_ts, include_open=True)[0]
        assert (synthetic["close"], synthetic["volume"], synthetic["confirmed"]) == ("100", "0", False)
        market.ingest(client, _event(synthetic_ts, "101", confirmed=True))
        actual = _get(client, synthetic_ts, synthetic_ts, as_of=close_ts)[0]
        assert (actual["close"], actual["ts_available"], actual["revision"]) == ("101", close_ts, 1)


@pytest.mark.regression
def test_synthetic_bar_never_masks_real_preview_across_confirmation_write(
    tmp_path, fixture_dir, monkeypatch,
):
    # ADR-0016: a real preview remains visible at a cutoff before the late close.
    market = Market(tmp_path, fixture_dir)
    timestamp = START + MINUTE
    end = timestamp + MINUTE
    attempted, release, published = Event(), Event(), Event()
    publish = ColdStore.publish

    def delay_confirmation(store, batch):
        if batch.bars[0].ts_event != timestamp:
            return publish(store, batch)
        attempted.set()
        assert release.wait(5)
        result = publish(store, batch)
        published.set()
        return result

    monkeypatch.setattr(ColdStore, "publish", delay_confirmation)
    with TestClient(market.app) as client:
        try:
            market.now = timestamp
            market.ingest(client, _event(START, "100", confirmed=True))
            market.now = end + 1_000
            market.ingest(client, _event(end, "102", confirmed=False))
            market.now = end + 2_000
            market.ingest(client, _event(timestamp, "98", confirmed=False))
            cutoff = market.now
            preview = _get(client, timestamp, timestamp, as_of=cutoff, include_open=True)
            assert (preview[0]["close"], preview[0]["confirmed"]) == ("98", False)
            assert _get(client, timestamp, timestamp, as_of=cutoff) == []

            market.now = end + 6_000
            market.ingest(client, _event(timestamp, "101", confirmed=True))
            assert attempted.wait(5)
            assert _get(client, timestamp, timestamp, as_of=cutoff, include_open=True) == preview
            release.set()
            assert published.wait(5)
            assert _get(client, timestamp, timestamp, as_of=cutoff, include_open=True) == preview
            assert _get(client, timestamp, timestamp, as_of=cutoff) == []
        finally:
            release.set()


def test_range_backfills_missing_history_without_confirming_a_live_preview(
    tmp_path, fixture_dir, monkeypatch,
):
    # data-access/02, ADR-0018: substitute fixture REST while leaving the writer active.
    market = Market(tmp_path, fixture_dir)
    tail = START + MINUTE
    market.now = tail + MINUTE + 10_000

    def fetch_bars(connector, *, symbol, timeframe, end_ts, limit):
        candidates = (_raw(START, "100"), _raw(tail, "102"))
        return [bar for bar in candidates if bar.ts_event <= end_ts][-limit:]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client:
        market.ingest(client, _event(tail, "101", confirmed=False))
        bars = _get(client, START, tail)
        assert [(bar["ts_event"], Decimal(bar["close"])) for bar in bars] == [(START, Decimal("100"))]
        assert _get(client, START, tail, as_of=tail - 1) == []
        with_preview = _get(client, START, tail, include_open=True)
        assert [(bar["ts_event"], bar["confirmed"]) for bar in with_preview] == [
            (START, True), (tail, False),
        ]

    offline = ColdReader(market.settings.data_dir).read_bars(INSTRUMENT, TIMEFRAME)
    assert [(bar.ts_event, bar.close) for bar in offline] == [(START, Decimal("100"))]


@pytest.mark.regression
def test_range_backfill_rechecks_preview_received_during_rest(tmp_path, fixture_dir, monkeypatch):
    # ADR-0018: the range fetch starts with an empty slot and completes after a live preview.
    market = Market(tmp_path, fixture_dir)
    market.now = START + MINUTE + 10_000
    requested, release = Event(), Event()

    def fetch_bars(connector, **kwargs):
        requested.set()
        assert release.wait(5)
        return [_raw(START, "100")]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client, ThreadPoolExecutor(1) as requests:
        pending = requests.submit(_get, client, START, START)
        try:
            assert requested.wait(5)
            market.ingest(client, _event(START, "101", confirmed=False))
        finally:
            release.set()
        assert pending.result(timeout=5) == []
        preview = _get(client, START, START, include_open=True)[0]
        assert (preview["close"], preview["confirmed"]) == ("101", False)

    assert ColdReader(market.settings.data_dir).read_bars(INSTRUMENT, TIMEFRAME) == []


@pytest.mark.regression
def test_range_backfill_can_replace_old_preview_after_unsubscribe(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: a preview from a closed subscription does not cover the slot.
    market = Market(tmp_path, fixture_dir)

    def fetch_bars(connector, **kwargs):
        return [_raw(START, "100")]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client:
        market.now = START + 5_000
        market.ingest(client, _event(START, "99", confirmed=False))

        async def unsubscribe() -> None:
            market.app.state.runtime.supervisor.unsubscribe(
                instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME,
            )

        client.portal.call(unsubscribe)
        market.now = START + MINUTE + 10_000
        bars = _get(client, START, START)
        assert [(bar["close"], bar["confirmed"]) for bar in bars] == [("100", True)]


@pytest.mark.regression
@pytest.mark.parametrize("explicit_end", [True, False])
def test_range_rejects_more_than_ten_thousand_slots(tmp_path, fixture_dir, explicit_end):
    # data-access/03: both an explicit and the default end enforce the inclusive slot limit.
    market = Market(tmp_path, fixture_dir)
    market.now = START + 10_001 * MINUTE
    params = {"instrument": INSTRUMENT.instrument_id, "timeframe": TIMEFRAME, "start": START}
    if explicit_end:
        params["end"] = market.now
    with TestClient(market.app) as client:
        response = client.get("/api/bars", params=params)
        assert response.status_code == 400
        assert "分段" in response.text


@pytest.mark.regression
def test_range_backfill_fetches_all_ten_thousand_slots(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: produce a full REST history extending past the subscription's five pages.
    market = Market(tmp_path, fixture_dir)
    end = START + 9_999 * MINUTE
    market.now = end + MINUTE

    def fetch_bars(connector, *, symbol, timeframe, end_ts, limit):
        return [_raw(ts, "100") for ts in range(end_ts - (limit - 1) * MINUTE, end_ts + 1, MINUTE)]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client:
        bars = _get(client, START, end)
        assert [bar["ts_event"] for bar in bars] == list(range(START, end + 1, MINUTE))


@pytest.mark.regression
def test_concurrent_range_backfills_leave_health_responsive(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: constrain the shared pool so a blocked synchronous request exhausts it.
    market = Market(tmp_path, fixture_dir)
    market.now = START + 2 * MINUTE
    requested, release = Event(), Event()

    def fetch_bars(connector, **kwargs):
        requested.set()
        assert release.wait(5)
        return [_raw(START, "100")]

    async def constrain_request_pool() -> None:
        to_thread.current_default_thread_limiter().total_tokens = 1

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    with TestClient(market.app) as client, ThreadPoolExecutor(3) as requests:
        client.portal.call(constrain_request_pool)
        pending = [requests.submit(_get, client, START, START) for _ in range(2)]
        try:
            assert requested.wait(5)
            health = requests.submit(client.get, "/api/health").result(timeout=2)
            assert health.status_code == 200
            assert health.json()["ok"] is True
            assert all(not request.done() for request in pending)
        finally:
            release.set()
        assert all(request.result(timeout=5)[0]["close"] == "100" for request in pending)


@pytest.mark.regression
def test_range_backfill_timeout_returns_retryable_504(tmp_path, fixture_dir, monkeypatch):
    # data-access/03: a timed-out waiter can retry while the shared backfill finishes.
    market = Market(tmp_path, fixture_dir)
    market.now = START + 2 * MINUTE
    requested, release = Event(), Event()

    def fetch_bars(connector, **kwargs):
        requested.set()
        assert release.wait(5)
        return [_raw(START, "100")]

    monkeypatch.setattr(FixtureConnector, "fetch_bars", fetch_bars)
    monkeypatch.setattr(bars_route, "HISTORY_TIMEOUT_SECONDS", 0.1, raising=False)
    params = {
        "instrument": INSTRUMENT.instrument_id, "timeframe": TIMEFRAME, "start": START, "end": START,
    }
    with TestClient(market.app) as client, ThreadPoolExecutor(1) as requests:
        pending = requests.submit(client.get, "/api/bars", params=params)
        try:
            assert requested.wait(5)
            response = pending.result(timeout=2)
            assert response.status_code == 504
            assert "重试" in response.json()["message"]
        finally:
            release.set()
        monkeypatch.setattr(bars_route, "HISTORY_TIMEOUT_SECONDS", 5)
        assert _get(client, START, START)[0]["close"] == "100"
