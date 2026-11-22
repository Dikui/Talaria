"""Cold publication and history backfill contracts from ADR-0018 and ticket 02."""

from __future__ import annotations

import asyncio
import threading
from contextlib import closing
from decimal import Decimal

import pytest

from talaria.foundation.connector import RawBar
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.cold_publisher import ColdPublisher
from talaria.perception.cold_store import ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.regression]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000
NOW = BASE + 3 * MINUTE


def historical_bar(timestamp: int, close: str) -> RawBar:
    price = Decimal(close)
    return RawBar(
        symbol=INSTRUMENT.symbol, timeframe=TIMEFRAME, ts_event=timestamp,
        open=price, high=price, low=price, close=price, volume=Decimal("1"), turnover=price,
        ts_available=NOW, ts_ingested=NOW, ts_committed=NOW,
    )


def test_history_only_fills_bars_missing_when_written(tmp_path):
    # Pause the REST response while the real cold writer publishes a realtime revision.
    requested = threading.Event()
    release = threading.Event()

    class History:
        def fetch_bars(self, **kwargs):
            requested.set()
            assert release.wait(timeout=5)
            return [historical_bar(BASE, "90"), historical_bar(BASE + MINUTE, "95")]

    async def run(store):
        publisher = ColdPublisher(
            store, instruments=[INSTRUMENT], connector=History(),
            policy=BackfillPolicy(), now_ms=lambda: NOW,
        )
        try:
            publisher.request_history(INSTRUMENT, TIMEFRAME, start_ts=BASE, end_ts=BASE + MINUTE)
            assert await asyncio.to_thread(requested.wait, 2)
            for price, available in (("100", BASE + MINUTE), ("101", NOW)):
                bar = historical_bar(BASE, price).to_compact().model_copy(
                    update={"ts_available": available},
                )
                publisher.record_close(INSTRUMENT, TIMEFRAME, bar, available)
        finally:
            release.set()
            await publisher.close()

    with closing(ColdStore(tmp_path)) as store:
        asyncio.run(run(store))
        bars = store.read_bars(INSTRUMENT, TIMEFRAME)
        assert [(bar.close, bar.revision, bar.ts_available) for bar in bars] == [
            (Decimal("101"), 1, NOW),
            (Decimal("95"), 0, BASE + 2 * MINUTE),
        ]
