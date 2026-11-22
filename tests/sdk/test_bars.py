import json
from contextlib import closing
from decimal import Decimal
from shutil import copyfile

import httpx
import pytest

from talaria import sdk
from talaria.foundation.connector import RawBar
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore
from talaria.service_endpoint import service_descriptor_path

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


def test_sdk_splits_http_range_without_skipping_the_boundary(tmp_path, monkeypatch):
    # data-access/04 and 03: the SDK can span the service's 10,000-bar limit.
    boundary = START + 10_000 * MINUTE
    descriptor = service_descriptor_path(tmp_path)
    descriptor.parent.mkdir()
    descriptor.write_text(
        json.dumps({"version": 1, "base_url": "http://127.0.0.1:8765"}), encoding="utf-8",
    )
    served = []

    def respond(transport, request):
        query = request.url.params
        start, end = int(query["start"]), int(query["end"])
        served.append((start, end))
        timestamp = boundary - MINUTE if start == START else boundary
        bar = {
            "ts_event": timestamp, "open": "100", "high": "100", "low": "100", "close": "100",
            "volume": "0.1", "turnover": "10", "ts_available": timestamp + MINUTE,
            "precision": "SECOND_VERIFIED", "confirmed": True, "revision": 0,
        }
        return httpx.Response(200, json={
            "instrument_id": INSTRUMENT.instrument_id, "timeframe": "1m", "bars": [bar],
        })

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", respond)
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
