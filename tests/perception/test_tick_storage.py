"""data-access/07: point-in-time reads of live trades and BBO quotes."""

import asyncio
from decimal import Decimal
from uuid import UUID

import pyarrow.parquet as pq
import pytest

from talaria import sdk
from talaria.foundation.feed import OrderBookEvent, TradeEvent
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore

pytestmark = [pytest.mark.small, pytest.mark.spec]

INSTRUMENT = "BTC-USDT-PERP"
MIDNIGHT = 1_763_596_800_000
NOON = MIDNIGHT + 43_200_000


def test_sdk_selects_visible_tick_revisions_after_compaction(tmp_path) -> None:
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    assert spec is not None
    store = ColdStore(tmp_path)
    trade_time = NOON - 20
    quote_time = NOON - 10
    first, second, later = (UUID(int=n) for n in range(1, 4))

    def trade(trade_id, ts, price="100.1", received=None):
        return TradeEvent(
            instrument_id=INSTRUMENT, trade_id=trade_id, side="Buy",
            price=Decimal(price), size=Decimal("0.123"), ts_event=ts,
            ts_ingested=ts if received is None else received,
        )

    def quote(bid, ts, received):
        return OrderBookEvent(
            instrument_id=INSTRUMENT, bid_price=Decimal(bid), bid_size=Decimal("1.234"),
            ask_price=Decimal("101.0"), ask_size=Decimal("0.001"), ts_event=ts,
            ts_ingested=received,
        )

    async def publish(*events):
        publisher = ColdPublisher(
            store, instruments=[spec], connector=None, policy=BackfillPolicy(),
            now_ms=lambda: NOON + 100,
        )
        for event in events:
            publisher.ingest_ticks(event)
        await publisher.close()

    try:
        asyncio.run(publish(
            trade(first, trade_time), trade(second, trade_time), quote("100.0", quote_time, quote_time),
        ))
        asyncio.run(publish(
            trade(first, trade_time, "100.2", NOON + 1),
            trade(later, NOON + 2),
            quote("100.0", quote_time, NOON + 1),
            quote("100.5", quote_time, NOON + 2),
        ))
        store.compact([spec], now_ms=MIDNIGHT + 86_400_000)
        generations = store.generations(spec, "trades")
        assert len(generations) == 1
        path = generations[0].parquet_path
        assert path.parts[-3:-1] == ("trades", "2025-11-20")
        schema = pq.ParquetFile(tmp_path / path).schema
        by_name = {schema.column(index).name: schema.column(index) for index in range(len(schema))}
        assert (by_name["price"].physical_type, by_name["price"].precision, by_name["price"].scale) == (
            "INT64", 18, spec.price_precision,
        )
        assert (by_name["size"].physical_type, by_name["size"].scale) == ("INT64", spec.qty_precision)
    finally:
        store.close()

    morning = sdk.trades(INSTRUMENT, MIDNIGHT, as_of=NOON, data_dir=tmp_path)
    assert {row.trade_id: (row.price, row.revision) for row in morning} == {
        first: (Decimal("100.1"), 0), second: (Decimal("100.1"), 0),
    }
    latest = sdk.trades(INSTRUMENT, MIDNIGHT, data_dir=tmp_path)
    assert {row.trade_id: (row.price, row.revision) for row in latest} == {
        first: (Decimal("100.2"), 1), second: (Decimal("100.1"), 0), later: (Decimal("100.1"), 0),
    }
    assert latest.to_frame()["size"].dtype == "float64"
    assert [row.bid_price for row in sdk.quotes(INSTRUMENT, MIDNIGHT, as_of=NOON, data_dir=tmp_path)] == [
        Decimal("100.0"),
    ]
    quotes = sdk.quotes(INSTRUMENT, quote_time, quote_time, data_dir=tmp_path)
    assert [(row.bid_price, row.revision, row.ts_available) for row in quotes] == [
        (Decimal("100.5"), 1, NOON + 2),
    ]
    assert quotes.to_frame()["ask_price"].dtype == "float64"
