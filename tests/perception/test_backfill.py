"""首次回填：分页、页数上限、无新数据时停止。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.connector import RawBar
from talaria.perception.backfill import BackfillPolicy, ensure_history
from talaria.perception.raw_store import RawStore

pytestmark = pytest.mark.small

MINUTE = 60_000


def _bar(ts: int) -> RawBar:
    return RawBar(
        symbol="BTCUSDT",
        timeframe="1m",
        ts_event=ts,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=Decimal("1"),
        turnover=Decimal("100"),
        ts_available=ts + MINUTE,
        ts_ingested=ts + MINUTE,
        ts_committed=ts + MINUTE,
    )


class StubConnector:
    def __init__(self, batches: list[list[RawBar]]) -> None:
        self._batches = batches
        self.calls: list[dict[str, int | None]] = []

    def fetch_bars(self, *, symbol, timeframe, end_ts=None, limit=1000):
        self.calls.append({"end_ts": end_ts, "limit": limit})
        return self._batches.pop(0) if self._batches else []


def test_stops_when_connector_has_no_more_history(tmp_path) -> None:
    connector = StubConnector([[_bar(0), _bar(MINUTE)]])
    ensure_history(
        raw_store=RawStore(tmp_path, raw_archive_id="test"),
        connector=connector,
        symbol="BTCUSDT",
        timeframe="1m",
        cutoff_ts=10**12,
        policy=BackfillPolicy(target_bars=5, max_pages=5),
    )
    assert len(connector.calls) == 2
    assert connector.calls[1]["end_ts"] == -1


def test_respects_max_pages(tmp_path) -> None:
    connector = StubConnector([[_bar(0)], [_bar(-MINUTE)], [_bar(-2 * MINUTE)]])
    ensure_history(
        raw_store=RawStore(tmp_path, raw_archive_id="test"),
        connector=connector,
        symbol="BTCUSDT",
        timeframe="1m",
        cutoff_ts=10**12,
        policy=BackfillPolicy(target_bars=100, max_pages=2),
    )
    assert len(connector.calls) == 2


def test_policy_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError):
        BackfillPolicy(target_bars=0)
    with pytest.raises(ValueError):
        BackfillPolicy(page_limit=1001)
