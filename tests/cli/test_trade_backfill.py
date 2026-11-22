"""data-access/07: import an injected Bybit gzip/CSV fixture through the CLI."""

import gzip
from decimal import Decimal
from uuid import UUID

import pytest

from talaria import sdk
from talaria.cli.main import main
from talaria.service_lock import probe_lock

pytestmark = [pytest.mark.small, pytest.mark.spec]
INSTRUMENT = "BTC-USDT-PERP"
DAY = "2025-11-20"
BASE = 1_763_596_800_000
FIRST_ID = UUID(int=1)
SECOND_ID = UUID(int=2)


@pytest.fixture
def archive(tmp_path, monkeypatch):
    from talaria.packs.crypto_perp import trade_archive

    path = tmp_path / f"BTCUSDT{DAY}.csv.gz"
    with gzip.open(path, "wt") as output:
        output.write(
            "timestamp,symbol,side,size,price,tickDirection,trdMatchID,grossValue,homeNotional,foreignNotional,RPI\n"
            f"1763596800.1234,BTCUSDT,Buy,0.001,100.1,PlusTick,{FIRST_ID},0,0,0,0\n"
            f"1763640000.1239,BTCUSDT,Sell,0.003,100.2,MinusTick,{SECOND_ID},0,0,0,0\n"
        )

    def download(url, target):
        # The local gzip replaces the network; the stopped-service CLI must take the lock first.
        assert probe_lock(tmp_path) is not None
        assert url == f"https://public.bybit.com/trading/BTCUSDT/BTCUSDT{DAY}.csv.gz"
        target.write(path.read_bytes())

    monkeypatch.setattr(trade_archive, "download_bybit_archive", download)
    return path


def test_completed_archive_is_idempotent_and_visible_at_event_time(tmp_path, archive, capsys):
    args = ["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]
    assert main(args) == 0
    assert "2 笔" in capsys.readouterr().out
    assert main(args) == 0
    assert "0 笔" in capsys.readouterr().out
    assert not sdk.trades(INSTRUMENT, BASE, as_of=BASE + 123, data_dir=tmp_path)
    assert [(row.trade_id, row.ts_event, row.price) for row in sdk.trades(
        INSTRUMENT, BASE, as_of=f"{DAY}T12:00:00Z", data_dir=tmp_path,
    )] == [(FIRST_ID, BASE + 124, Decimal("100.1"))]
    first = sdk.trades(INSTRUMENT, BASE, as_of=BASE + 124, data_dir=tmp_path)[0]
    assert (first.ts_available, first.availability) == (BASE + 124, "inferred")
    assert [row.trade_id for row in sdk.trades(INSTRUMENT, BASE, data_dir=tmp_path)] == [FIRST_ID, SECOND_ID]

    assert main(["cold-size", "--data-dir", str(tmp_path)]) == 0
    capacity = capsys.readouterr().out.splitlines()
    assert capacity[0].split() == ["day", "venue", "symbol", "dataset", "rows", "parquet_bytes"]
    day, venue, symbol, dataset, rows, size = capacity[1].split()
    assert (day, venue, symbol, dataset, rows) == (DAY, "bybit", "BTCUSDT", "trades", "2")
    expected = sum(path.stat().st_size for path in tmp_path.glob("data/cold/bybit/BTCUSDT/trades/*/*.parquet"))
    assert int(size) == expected > 0


def test_invalid_archive_crc_cannot_publish_a_partial_day(tmp_path, archive):
    contents = bytearray(archive.read_bytes())
    contents[-8] ^= 1  # Corrupt the CRC without changing the CSV rows.
    archive.write_bytes(contents)
    assert main(["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]) == 1
    assert not sdk.trades(INSTRUMENT, BASE, data_dir=tmp_path)


@pytest.mark.regression
def test_archive_preserves_csv_order_within_a_rounded_millisecond(tmp_path, archive):
    with gzip.open(archive, "wt") as output:
        output.write(
            "timestamp,symbol,side,size,price,trdMatchID\n"
            f"1763596800.1234,BTCUSDT,Buy,0.001,100.1,{SECOND_ID}\n"
            f"1763596800.1239,BTCUSDT,Sell,0.003,100.2,{FIRST_ID}\n"
        )
    assert main(["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]) == 0
    rows = sdk.trades(INSTRUMENT, BASE, data_dir=tmp_path)
    assert [(row.trade_id, row.ts_event, row.archive_index, row.order_uncertain) for row in rows] == [
        (SECOND_ID, BASE + 124, 0, False), (FIRST_ID, BASE + 124, 1, False),
    ]


@pytest.mark.regression
def test_submillisecond_trade_at_archive_day_end_becomes_visible_on_next_day(tmp_path, archive):
    with gzip.open(archive, "wt") as output:
        output.write(
            "timestamp,symbol,side,size,price,trdMatchID\n"
            f"1763596800.1234,BTCUSDT,Buy,0.001,100.1,{FIRST_ID}\n"
            f"1763683199.9999,BTCUSDT,Sell,0.003,100.2,{SECOND_ID}\n"
        )
    assert main(["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]) == 0
    midnight = BASE + 86_400_000
    assert [row.trade_id for row in sdk.trades(INSTRUMENT, BASE, as_of=midnight - 1, data_dir=tmp_path)] == [
        FIRST_ID,
    ]
    last, = sdk.trades(INSTRUMENT, midnight, midnight, as_of=midnight, data_dir=tmp_path)
    assert (last.trade_id, last.ts_event, last.ts_available) == (SECOND_ID, midnight, midnight)


def test_cli_delegates_to_running_service_and_preserves_live_trade(tmp_path, archive, monkeypatch, capsys):
    from fastapi.testclient import TestClient

    from talaria.foundation.feed import TradeEvent
    from talaria.service_lock import ServiceLock
    from talaria.workbench_api.app import create_app
    from talaria.workbench_api.service_config import FeatureToggles
    from talaria.workbench_api.service_descriptor import service_descriptor_path, write_service_descriptor
    from talaria.workbench_api.settings import AppSettings

    settings = AppSettings(
        data_dir=tmp_path, dist_dir=tmp_path / "dist", token="local-token",
        features=FeatureToggles(market_data=False, alerts=False, scanner=False),
        now_ms=lambda: BASE + 2 * 86_400_000,
    )
    app = create_app(settings)
    with ServiceLock(tmp_path) as lock, TestClient(app) as service:
        write_service_descriptor(
            service_descriptor_path(tmp_path), base_url="http://127.0.0.1:8765",
            pid=lock.pid, token=settings.token, features=settings.features,
        )
        assert service.post("/api/trades/backfill", params={
            "instrument": INSTRUMENT, "day": DAY,
        }).status_code == 401
        publisher = app.state.runtime.cold_publisher
        assert publisher is not None

        async def ingest():
            publisher.ingest_ticks(TradeEvent(
                instrument_id=INSTRUMENT, trade_id=FIRST_ID, side="Buy",
                price=Decimal("101.0"), size=Decimal("0.001"),
                ts_event=BASE + 123, ts_ingested=BASE + 200,
            ))

        # Simulate a recent live packet sitting in the service's buffer when the command arrives.
        assert service.portal is not None
        service.portal.call(ingest)

        class LocalClient:
            """Use the ASGI service for the CLI's HTTP call without a TCP socket."""

            def __init__(self, *, trust_env, timeout):
                assert trust_env is False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def post(self, url, **kwargs):
                assert url == "http://127.0.0.1:8765/api/trades/backfill"
                return service.post("/api/trades/backfill", **kwargs)

        monkeypatch.setattr("talaria.cli.ticks.httpx.Client", LocalClient)
        assert main(["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]) == 0
        assert "新增 1 笔" in capsys.readouterr().out

    assert [(row.trade_id, row.price) for row in sdk.trades(INSTRUMENT, BASE, data_dir=tmp_path)] == [
        (FIRST_ID, Decimal("101.0")), (SECOND_ID, Decimal("100.2")),
    ]


@pytest.mark.regression
@pytest.mark.parametrize("archive_first", [True, False])
@pytest.mark.parametrize("event_time", [BASE + 124, BASE - 1], ids=["same-time", "different-day"])
def test_live_trade_wins_by_id_in_either_arrival_order(tmp_path, archive, archive_first, event_time):
    import asyncio

    from talaria.foundation.feed import TradeEvent
    from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
    from talaria.perception.backfill import BackfillPolicy
    from talaria.perception.cold_publisher import ColdPublisher
    from talaria.perception.cold_store import ColdStore

    def import_archive():
        assert main(["trade-backfill", INSTRUMENT, DAY, "--data-dir", str(tmp_path)]) == 0

    async def publish_live(store):
        publisher = ColdPublisher(
            store, instruments=CRYPTO_PERP_PACK.instruments(), connector=None,
            policy=BackfillPolicy(), now_ms=lambda: BASE + 300,
        )
        publisher.ingest_ticks(TradeEvent(
            instrument_id=INSTRUMENT, trade_id=FIRST_ID, side="Buy", price=Decimal("100.1"),
            size=Decimal("0.001"), ts_event=event_time, ts_ingested=BASE + 200,
        ))
        await publisher.close()

    if archive_first:
        import_archive()
    store = ColdStore(tmp_path)
    try:
        asyncio.run(publish_live(store))
    finally:
        store.close()
    if not archive_first:
        import_archive()

    assert not sdk.trades(INSTRUMENT, BASE - 1, as_of=BASE + 199, data_dir=tmp_path)
    row, = sdk.trades(INSTRUMENT, BASE - 1, as_of=BASE + 200, data_dir=tmp_path)
    assert (row.trade_id, row.ts_event, row.ts_available, row.ts_ingested, row.revision, row.availability) == (
        FIRST_ID, event_time, BASE + 200, BASE + 200, 0, "measured",
    )
    narrow = sdk.trades(INSTRUMENT, BASE + 124, BASE + 124, as_of=BASE + 200, data_dir=tmp_path)
    assert narrow == ((row,) if event_time == BASE + 124 else ())
