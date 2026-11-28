import json
import os
from contextlib import closing, nullcontext
from decimal import Decimal
from shutil import copyfile

import httpx
import pytest

from talaria import sdk
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import InstrumentNotFoundError
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.service_endpoint import (
    BARS_HOT_REVISION_HEADER,
    DATA_DIR_HEADER,
    NOW_MS_HEADER,
    data_dir_id,
    service_descriptor_path,
)
from talaria.service_lock import ROLE_SERVICE, ROLE_USAGE_SCAN, ServiceLock

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
START = 1_793_491_200_000  # 2026-11-01 UTC
MINUTE = 60_000


def _bar(ts_event: int, close: str, *, revision: int = 0) -> RawBar:
    value = Decimal(close)
    available = ts_event + MINUTE + revision * 1000
    return RawBar(
        symbol=INSTRUMENT.symbol, timeframe="1m", ts_event=ts_event,
        open=value, high=value, low=value, close=value, volume=Decimal("0.1"),
        turnover=value * Decimal("0.1"), ts_available=available, ts_ingested=available,
        ts_committed=available, revision=revision,
    )


def _write_descriptor(data_dir, pid):
    descriptor = service_descriptor_path(data_dir)
    descriptor.parent.mkdir(exist_ok=True)
    descriptor.write_text(
        json.dumps({"version": 1, "base_url": "http://127.0.0.1:8765", "pid": pid}), encoding="utf-8",
    )


@pytest.mark.regression
@pytest.mark.parametrize("role,pid_offset", [(None, 0), (ROLE_USAGE_SCAN, 0), (ROLE_SERVICE, 1)])
def test_sdk_ignores_descriptor_without_matching_service_lock(tmp_path, monkeypatch, role, pid_offset):
    # data-access/04: a descriptor must belong to the process holding the service lock.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100"),)))
    _write_descriptor(tmp_path, os.getpid() + pid_offset)

    def unexpected_request(transport, request):
        pytest.fail(f"unverified service contacted at {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_request)
    with nullcontext() if role is None else ServiceLock(tmp_path, role=role):
        result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, START, data_dir=tmp_path)

    assert not result.tail_completed
    assert [bar.close for bar in result] == [Decimal("100")]


@pytest.mark.regression
@pytest.mark.parametrize("error", [httpx.RemoteProtocolError, httpx.ReadError, httpx.ReadTimeout])
def test_sdk_reads_local_bars_when_service_disconnects_during_tail(tmp_path, monkeypatch, error):
    # data-access/04: discovery succeeds before the transport fails during the range request.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100"),)))

    def respond(transport, request):
        if request.url.path == "/api/health":
            return httpx.Response(200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(START)})
        raise error("connection lost", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        _write_descriptor(tmp_path, lock.pid)
        result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, START, data_dir=tmp_path)

    assert not result.tail_completed
    assert [bar.close for bar in result] == [Decimal("100")]


def test_sdk_propagates_backfill_timeout_response(tmp_path, monkeypatch):
    # data-access/03 and 04: a retryable HTTP 504 remains visible to the caller.
    def respond(transport, request):
        if request.url.path == "/api/health":
            return httpx.Response(200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(START)})
        return httpx.Response(504, json={"code": "BARS_BACKFILL_TIMEOUT", "message": "Please retry"})

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        _write_descriptor(tmp_path, lock.pid)
        with pytest.raises(httpx.HTTPStatusError) as exc:
            sdk.bars(INSTRUMENT.instrument_id, "1m", START, START, data_dir=tmp_path)

    assert exc.value.response.status_code == 504


@pytest.mark.regression
@pytest.mark.parametrize("descriptor", [b"{", b"\xff", b'{"version": 2}'], ids=["json", "encoding", "version"])
def test_sdk_uses_local_bars_with_invalid_descriptor(tmp_path, descriptor):
    # data-access/04: even a live lock can coexist with an unreadable legacy descriptor.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100"),)))
    with ServiceLock(tmp_path):
        service_descriptor_path(tmp_path).write_bytes(descriptor)
        result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, START, data_dir=tmp_path)

    assert not result.tail_completed
    assert [bar.close for bar in result] == [Decimal("100")]


def test_offline_reads_visible_revisions_from_the_manifest_only(tmp_path, monkeypatch):
    # data-access/04 and ADR-0018: an unlisted but otherwise valid file is invisible.
    data_dir = tmp_path / "data"
    monkeypatch.setenv("TALARIA_DATA_DIR", str(data_dir))
    original = _bar(START, "100.1")
    correction = _bar(START, "101.2", revision=1)
    with closing(ColdStore(data_dir)) as store:
        published = store.publish(ColdBatch(INSTRUMENT, "1m", (original, correction)))
    other_dir = tmp_path / "unlisted"
    with closing(ColdStore(other_dir)) as store:
        unlisted = store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START + MINUTE, "102"),)))
    copyfile(other_dir / unlisted.parquet_path, (data_dir / published.parquet_path).with_name("orphan.parquet"))

    earlier = sdk.bars(INSTRUMENT.instrument_id, "1m", "2026-11-01", START + MINUTE, original.ts_available)
    latest = sdk.bars(INSTRUMENT.instrument_id, "1m", START, START + MINUTE, offline=True)
    assert [(bar.ts_event, bar.close, bar.revision, bar.confirmed) for bar in earlier] == [
        (START, Decimal("100.1"), 0, True),
    ]
    assert isinstance(earlier[0].close, Decimal)
    assert [(bar.ts_event, bar.close, bar.revision) for bar in latest] == [(START, Decimal("101.2"), 1)]
    assert not earlier.tail_completed and not latest.tail_completed


@pytest.mark.regression
@pytest.mark.parametrize("offline", [True, False])
def test_sdk_without_service_uses_local_last_closed_bar_as_default_end(tmp_path, monkeypatch, offline):
    # data-access/04: a cold manifest can contain data newer than the local clock.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100"), _bar(START + MINUTE, "200"))))
    monkeypatch.setattr("talaria.sdk.time.time_ns", lambda: (START + MINUTE + 30_000) * 1_000_000)

    result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, data_dir=tmp_path, offline=offline)

    assert not result.tail_completed
    assert [bar.ts_event for bar in result] == [START]


@pytest.mark.regression
@pytest.mark.parametrize(("start", "end"), [
    ("2026-11-01T00:00:00.0009Z", "2026-11-01T00:00:00.0001Z"),
    ("2026-11-01T01:00:00.000000009+01:00", "2026-11-01T00:00:00.000000001Z"),
])
def test_sdk_rejects_reversed_iso_range_at_original_precision(tmp_path, start, end):
    # data-access/04: comparing truncated milliseconds would accept both reversed ranges.
    with pytest.raises(ValueError, match="end 不得早于 start"):
        sdk.bars(INSTRUMENT.instrument_id, "1m", start, end, data_dir=tmp_path, offline=True)


@pytest.mark.regression
@pytest.mark.parametrize("start", ["2026-11-01T00:00:00.0001Z", "2026-11-01T00:00:00.000000001Z"])
def test_sdk_rounds_iso_range_inward(tmp_path, start):
    # data-access/04: boundary candles just outside the ISO interval are excluded.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (
            _bar(START, "100"), _bar(START + MINUTE, "200"), _bar(START + 2 * MINUTE, "300"),
        )))

    result = sdk.bars(
        INSTRUMENT.instrument_id, "1m", start, "2026-11-01T00:01:59.999999Z",
        data_dir=tmp_path, offline=True,
    )

    assert [bar.ts_event for bar in result] == [START + MINUTE]


@pytest.mark.regression
def test_sdk_accepts_iso_range_with_no_whole_millisecond(tmp_path):
    # data-access/04: a valid interval can become empty when projected onto millisecond events.
    with closing(ColdStore(tmp_path)) as store:
        store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100"),)))
    result = sdk.bars(
        INSTRUMENT.instrument_id, "1m", "2026-11-01T00:00:00.0001Z", "2026-11-01T00:00:00.0009Z",
        data_dir=tmp_path, offline=True,
    )
    assert not result


def test_sdk_splits_http_range_without_skipping_the_boundary(tmp_path, monkeypatch):
    # data-access/04 and 03: the SDK can span the service's 10,000-bar limit.
    boundary = START + 10_000 * MINUTE
    served = []

    def respond(transport, request):
        if request.url.path == "/api/health":
            return httpx.Response(200, headers={
                DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(boundary + MINUTE),
            })
        if request.url.path == "/api/bars.arrow":
            # Legacy JSON services without Arrow IPC trigger MAX_RANGE_BARS segmentation on /api/bars.
            return httpx.Response(404)
        if request.url.path == "/api/instruments":
            return httpx.Response(404)
        query = request.url.params
        start, end = int(query["start"]), int(query["end"])
        served.append((start, end))
        timestamp = boundary - MINUTE if start == START else boundary
        bar = {
            "ts_event": timestamp, "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "0.1", "turnover": "10", "ts_available": timestamp + MINUTE,
            "precision": "SECOND_VERIFIED", "confirmed": True, "revision": 0,
        }
        return httpx.Response(
            200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path), BARS_HOT_REVISION_HEADER: "test:0"},
            json={"instrument_id": INSTRUMENT.instrument_id, "timeframe": "1m", "generation": 0, "bars": [bar]},
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        _write_descriptor(tmp_path, lock.pid)
        result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, boundary, data_dir=tmp_path)
    assert served == [(START, boundary - 1), (boundary, boundary)]
    assert result.tail_completed and [bar.ts_event for bar in result] == [boundary - MINUTE, boundary]


@pytest.mark.parametrize("has_data", [True, False])
def test_frame_has_stable_columns_and_dtypes(tmp_path, has_data):
    # data-access/04: frame conversion uses float prices while the source stays exact.
    if has_data:
        with closing(ColdStore(tmp_path)) as store:
            store.publish(ColdBatch(INSTRUMENT, "1m", (_bar(START, "100.1"),)))
    result = sdk.bars(INSTRUMENT.instrument_id, "1m", START, data_dir=tmp_path, offline=True)
    frame = result.to_frame()
    assert list(frame.dtypes.astype(str).items()) == [
        ("ts_event", "int64"),
        ("open", "float64"), ("high", "float64"), ("low", "float64"), ("close", "float64"),
        ("volume", "float64"), ("turnover", "float64"),
        ("ts_available", "int64"), ("precision", "string"), ("confirmed", "bool"), ("revision", "int64"),
    ]
    if has_data:
        assert frame.iloc[0].to_dict() == {
            "ts_event": START, "open": 100.1, "high": 100.1, "low": 100.1, "close": 100.1,
            "volume": 0.1, "turnover": 10.01, "ts_available": START + MINUTE,
            "precision": "SECOND_VERIFIED", "confirmed": True, "revision": 0,
        }
        assert isinstance(result[0].close, Decimal)
    else:
        assert frame.empty


def test_sdk_online_resolves_instrument_spec_from_instruments_endpoint(tmp_path, monkeypatch):
    # Cross-module #1: online SDK resolves server-published venue/symbol from `/api/instruments`
    # and validates `/api/bars.arrow` against the server's symbol (`BTC-USDT-SWAP`).
    import io
    import pyarrow as pa
    import pyarrow.ipc as pa_ipc

    okx_spec = {
        "instrument_id": "BTC-USDT-PERP",
        "venue": "okx",
        "symbol": "BTC-USDT-SWAP",
        "asset_class": "crypto_perp",
        "tick_size": "0.1",
        "qty_step": "0.0001",
        "min_qty": "0.0001",
        "price_precision": 1,
        "qty_precision": 4,
    }
    table = pa.table({
        "symbol": ["BTC-USDT-SWAP"],
        "timeframe": ["1m"],
        "ts_event": pa.array([START], type=pa.int64()),
        "open": pa.array([Decimal("58100.0")], type=pa.decimal128(18, 1)),
        "high": pa.array([Decimal("58200.0")], type=pa.decimal128(18, 1)),
        "low": pa.array([Decimal("58050.0")], type=pa.decimal128(18, 1)),
        "close": pa.array([Decimal("58150.0")], type=pa.decimal128(18, 1)),
        "volume": pa.array([Decimal("12.0000")], type=pa.decimal128(18, 4)),
        "turnover": pa.array([Decimal("697800.00000")], type=pa.decimal128(18, 5)),
        "ts_available": pa.array([START + MINUTE], type=pa.int64()),
        "precision": ["SECOND_VERIFIED"],
        "confirmed": [True],
        "revision": pa.array([0], type=pa.int64()),
    })
    sink = io.BytesIO()
    with pa_ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    arrow_bytes = sink.getvalue()

    def respond(transport, request):
        if request.url.path == "/api/instruments":
            return httpx.Response(200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path)}, json=[okx_spec])
        if request.url.path == "/api/health":
            return httpx.Response(200, headers={
                DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(START + MINUTE),
            })
        assert request.url.path == "/api/bars.arrow"
        return httpx.Response(
            200,
            headers={
                DATA_DIR_HEADER: data_dir_id(tmp_path),
                "content-type": "application/vnd.apache.arrow.stream",
                BARS_HOT_REVISION_HEADER: "test:0",
            },
            content=arrow_bytes,
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        _write_descriptor(tmp_path, lock.pid)
        result = sdk.bars("BTC-USDT-PERP", "1m", START, START, data_dir=tmp_path)

    assert len(result) == 1
    assert result[0].close == Decimal("58150.0")


def test_sdk_online_rejects_unconfigured_default_pack_instrument(tmp_path, monkeypatch):
    # Audit #3: when `/api/instruments` returns HTTP 200, unconfigured DEFAULT_PACK symbols must not fall back to Bybit.
    okx_spec = {
        "instrument_id": "BTC-USDT-PERP",
        "venue": "okx",
        "symbol": "BTC-USDT-SWAP",
        "asset_class": "crypto_perp",
        "tick_size": "0.1",
        "qty_step": "0.0001",
        "min_qty": "0.0001",
        "price_precision": 1,
        "qty_precision": 4,
    }

    def respond(transport, request):
        if request.url.path == "/api/instruments":
            return httpx.Response(200, headers={DATA_DIR_HEADER: data_dir_id(tmp_path)}, json=[okx_spec])
        pytest.fail(f"unexpected request to {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
    with ServiceLock(tmp_path) as lock:
        _write_descriptor(tmp_path, lock.pid)
        with pytest.raises(InstrumentNotFoundError, match="在线服务未配置标的：ETH-USDT-PERP"):
            sdk.bars("ETH-USDT-PERP", "1m", START, START, data_dir=tmp_path)


def test_sdk_default_data_dir_matches_shared_home_location(tmp_path, monkeypatch):
    # Cross-module #3: default data_dir is ~/.talaria/data, overridable via TALARIA_DATA_DIR or arg.
    fake_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("TALARIA_DATA_DIR", raising=False)
    assert sdk.data_dir() == (fake_home / ".talaria" / "data").resolve()

    env_dir = tmp_path / "env-data"
    monkeypatch.setenv("TALARIA_DATA_DIR", str(env_dir))
    assert sdk.data_dir() == env_dir.resolve()

    explicit_dir = tmp_path / "explicit-data"
    assert sdk.data_dir(explicit_dir) == explicit_dir.resolve()
