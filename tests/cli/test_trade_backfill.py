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
    assert [(row.trade_id, row.ts_event, row.price) for row in sdk.trades(
        INSTRUMENT, BASE, as_of=f"{DAY}T12:00:00Z", data_dir=tmp_path,
    )] == [(FIRST_ID, BASE + 123, Decimal("100.1"))]
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
