"""data-access/08 的离线查询、修订与结算契约。"""

import asyncio
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


def test_ticker_crossing_backfills_confirmed_settlement_and_preserves_live_observations(
    tmp_path, fixture_dir,
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
        publisher = ColdPublisher(
            store, instruments=[spec], connector=SettlementConnector(fixture_dir),
            policy=BackfillPolicy(), now_ms=lambda: now,
        )
        try:
            publisher.ingest_derivatives(DerivativeTickEvent(
                instrument_id=INSTRUMENT, ts_event=now, ts_ingested=now,
                mark_price=Decimal("100.1"), funding_rate=Decimal("0.00004"),
                next_funding_ts=boundary, open_interest=Decimal("123.456"),
            ))
            assert not sdk.funding_settlements(INSTRUMENT, boundary, data_dir=tmp_path)
            now = boundary + 100
            publisher.ingest_derivatives(DerivativeTickEvent(
                instrument_id=INSTRUMENT, ts_event=boundary, ts_ingested=now,
                funding_rate=Decimal("0.00003"), next_funding_ts=boundary + 28_800_000,
            ))
            # 常规历史请求针对同一结算点重跑，必须保留实时记录的可见时刻。
            for _ in range(2):
                future = publisher.request_derivative_history(spec, ("1m",), cutoff_ts=now)
                assert future is not None
                await asyncio.wrap_future(future)
        finally:
            await publisher.close()

    store = cold_store.ColdStore(tmp_path)
    try:
        asyncio.run(ingest(store))
    finally:
        store.close()

    assert not sdk.funding_settlements(INSTRUMENT, boundary, as_of=boundary, data_dir=tmp_path)
    [point] = sdk.funding_settlements(INSTRUMENT, boundary, as_of=now, data_dir=tmp_path)
    assert point.value == Decimal("0.00005")
    assert point.ts_available == now
    rows = sdk.derivatives(INSTRUMENT, "funding_rate", boundary - 1, boundary, data_dir=tmp_path)
    assert [row.value for row in rows] == [
        Decimal("0.00004"), Decimal("0.00003"), Decimal("0.00005"),
    ]
    assert sdk.derivatives(INSTRUMENT, "open_interest", boundary - 1, boundary, data_dir=tmp_path)[0].value == Decimal("123.456")
