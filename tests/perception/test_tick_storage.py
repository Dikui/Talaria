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


@pytest.mark.regression
@pytest.mark.parametrize("online", [False, True])
def test_sdk_ticks_default_to_service_or_offline_clock(tmp_path, monkeypatch, online):
    from contextlib import closing
    from types import SimpleNamespace

    import httpx

    from talaria.foundation.ticks import Quote, TickKind, Trade
    from talaria.service_endpoint import DATA_DIR_HEADER, NOW_MS_HEADER, data_dir_id

    now = NOON
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    with closing(ColdStore(tmp_path)) as store:
        for i, visible in enumerate([now, now + 1]):
            times = dict(instrument_id=INSTRUMENT, ts_event=now - 2 + i, ts_available=visible,
                         ts_ingested=visible, ts_committed=visible)
            store.publish_ticks(spec, TickKind.TRADES, [Trade(
                **times, trade_id=UUID(int=i + 1), side="Buy", price=Decimal("100.0"), size=Decimal("1"),
            )])
            store.publish_ticks(spec, TickKind.QUOTES, [Quote(
                **times, bid_price=Decimal("100.0"), ask_price=Decimal("100.1"),
                bid_size=Decimal("1"), ask_size=Decimal("1"),
            )])

    def get_health(request):
        assert request.url.path == "/api/health"
        return httpx.Response(200, headers={
            DATA_DIR_HEADER: data_dir_id(tmp_path), NOW_MS_HEADER: str(now),
        })

    client = httpx.Client
    monkeypatch.setattr(sdk, "read_service_endpoint", lambda root: "http://service" if online else None)
    monkeypatch.setattr(sdk, "time", SimpleNamespace(time_ns=lambda: (now + 9 if online else now) * 10**6))
    monkeypatch.setattr(sdk.httpx, "Client", lambda **kw: client(transport=httpx.MockTransport(get_health), **kw))

    for read in (sdk.trades, sdk.quotes):
        assert [row.ts_event for row in read(INSTRUMENT, now - 3, data_dir=tmp_path)] == [now - 2]
        assert [row.ts_event for row in read(INSTRUMENT, now - 3, now + 5, data_dir=tmp_path)] == [now - 2]
        assert [row.ts_event for row in read(INSTRUMENT, now - 3, as_of=now + 5, data_dir=tmp_path)] == [
            now - 2, now - 1,
        ]


@pytest.mark.regression
@pytest.mark.parametrize("recover", [True, False])
def test_failed_tick_write_is_retried_or_logs_the_lost_time_range(tmp_path, monkeypatch, caplog, recover):
    from contextlib import closing, suppress

    from talaria.perception.cold_store import ColdPhase

    attempts = 0

    def write_fault(phase):
        nonlocal attempts
        if phase is ColdPhase.MANIFEST_BEFORE:
            attempts += 1
            if attempts == 1 or not recover:
                raise OSError("injected disk failure")

    monkeypatch.setattr("talaria.perception.tick_publisher.TICK_FLUSH_SECONDS", 0.01)
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)

    async def scenario():
        with closing(ColdStore(tmp_path, fault_hook=write_fault)) as store:
            publisher = ColdPublisher(
                store, instruments=[spec], connector=None, policy=BackfillPolicy(),
                now_ms=lambda: NOON + 200,
            )
            for i in (2, 5):
                publisher.ingest_ticks(TradeEvent(
                    instrument_id=INSTRUMENT, trade_id=UUID(int=i), side="Buy", price=Decimal("100.1"),
                    size=Decimal("1"), ts_event=NOON + i, ts_ingested=NOON + 100,
                ))
            task = asyncio.create_task(publisher.flush_ticks_periodically())
            try:
                async with asyncio.timeout(1):
                    while True:
                        rows = sdk.trades(INSTRUMENT, NOON, as_of=NOON + 200, data_dir=tmp_path)
                        if (recover and rows) or (not recover and "行情冷库缺口" in caplog.text):
                            break
                        await asyncio.sleep(0.01)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                await publisher.close()
            if recover:
                assert [row.ts_event for row in rows] == [NOON + 2, NOON + 5]
                assert attempts == 2
            else:
                assert not rows
                assert attempts == 3
                assert f"[{NOON + 2}, {NOON + 5}]" in caplog.text

    asyncio.run(scenario())


@pytest.mark.regression
@pytest.mark.parametrize("fail_first_write", [False, True])
def test_cold_publisher_backfills_a_market_supplied_trade_archive(tmp_path, fail_first_write):
    from contextlib import closing, contextmanager
    from datetime import date

    from talaria.foundation.ticks import TickKind, Trade
    from talaria.perception.cold_store import ColdPhase, ColdReader

    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT).model_copy(update={
        "venue": "example", "asset_class": "spot", "instrument_id": "EXAMPLE-INSTRUMENT",
    })
    trade = Trade(
        instrument_id=spec.instrument_id, trade_id=UUID(int=42), side="Buy",
        price=Decimal("100.1"), size=Decimal("1"), availability="inferred",
        ts_event=NOON, ts_available=NOON, ts_ingested=NOON + 5, ts_committed=NOON + 5,
    )

    class Archive:
        @contextmanager
        def open_trade_archive(self, instrument, day, now_ms):
            assert (instrument, day) == (spec, date(2025, 11, 20))
            yield lambda: iter([trade])

    def write_fault(phase):
        nonlocal fail_first_write
        if phase is ColdPhase.MANIFEST_BEFORE and fail_first_write:
            fail_first_write = False
            raise OSError("injected archive write failure")

    async def publish(store):
        publisher = ColdPublisher(
            store, instruments=[spec], connector=None, policy=BackfillPolicy(),
            now_ms=lambda: NOON + 5, archive_reader=Archive(),
        )
        try:
            assert await publisher.backfill_trades(spec, date(2025, 11, 20)) == 1
            assert await publisher.backfill_trades(spec, date(2025, 11, 20)) == 0
        finally:
            await publisher.close()

    with closing(ColdStore(tmp_path, fault_hook=write_fault)) as store:
        asyncio.run(publish(store))

    assert ColdReader(tmp_path).read_ticks(spec, TickKind.TRADES) == [trade]


@pytest.mark.regression
@pytest.mark.parametrize(("column", "value", "step"), [
    ("price", "100.01", "tick_size"), ("size", "0.01", "qty_step"),
])
def test_trade_writer_rejects_values_that_fit_scale_but_miss_instrument_step(tmp_path, column, value, step):
    from contextlib import closing
    from dataclasses import replace

    from talaria.foundation.errors import ColdStoreQualityError
    from talaria.foundation.ticks import TickKind, Trade
    from talaria.perception.cold_store import ColdReader

    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT).model_copy(update={
        "tick_size": Decimal("0.05"), "price_precision": 2, "qty_step": Decimal("0.02"), "qty_precision": 2,
    })
    trade = Trade(
        instrument_id=INSTRUMENT, trade_id=UUID(int=42), side="Buy",
        price=Decimal("100.05"), size=Decimal("0.20"),
        ts_event=NOON, ts_available=NOON, ts_ingested=NOON, ts_committed=NOON,
    )
    invalid = replace(trade, trade_id=UUID(int=43), **{column: Decimal(value)})
    with closing(ColdStore(tmp_path)) as store:
        with pytest.raises(ColdStoreQualityError, match=step):
            store.publish_ticks(spec, TickKind.TRADES, [trade, invalid])
    assert not ColdReader(tmp_path).read_ticks(spec, TickKind.TRADES)


@pytest.mark.regression
def test_tick_arrivals_skip_empty_pages_between_parquet_row_groups(tmp_path, monkeypatch):
    from contextlib import closing
    from uuid import UUID

    import pyarrow.parquet as pq

    from talaria.foundation.ticks import TickKind, Trade
    from talaria.perception.cold_store import ColdReader, ColdStore

    write_table = pq.write_table

    def write_one_tick_per_row_group(*args, **kwargs):
        return write_table(*args, row_group_size=1, **kwargs)

    monkeypatch.setattr(pq, "write_table", write_one_tick_per_row_group)
    spec = CRYPTO_PERP_PACK.instrument(INSTRUMENT)
    arrivals = (MIDNIGHT + 1, NOON + 1)
    with closing(ColdStore(tmp_path)) as store:
        store.publish_ticks(spec, TickKind.TRADES, [
            Trade(
                instrument_id=spec.instrument_id, trade_id=UUID(int=i), side="Buy", price=Decimal(100),
                size=Decimal(1), ts_event=MIDNIGHT, ts_available=at, ts_ingested=at, ts_committed=at,
            )
            for i, at in enumerate(arrivals, 1)
        ])

    read_table = pq.read_table
    decoded = []

    def observe_parquet_reads(*args, **kwargs):
        rows = read_table(*args, **kwargs)
        decoded.append(len(rows))
        return rows

    monkeypatch.setattr(pq, "read_table", observe_parquet_reads)
    rows = ColdReader(tmp_path).tick_arrivals(spec, MIDNIGHT, NOON + 1)
    assert next(rows).ts_available == arrivals[0]
    assert decoded == [1]
    assert [row.ts_available for row in rows] == [arrivals[1]]
    assert decoded == [1, 1]
