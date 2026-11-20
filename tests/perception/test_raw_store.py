"""原始柱 JSONL：固定路径、fsync 追加、按 timestamp 去重。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from talaria.foundation.connector import RawBar
from talaria.perception.raw_store import RawStore

pytestmark = pytest.mark.small


def _bar(ts: int, close: str) -> RawBar:
    value = Decimal(close)
    return RawBar(
        symbol="BTCUSDT",
        timeframe="1m",
        ts_event=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("1"),
        turnover=value,
        ts_available=ts + 60_000,
        ts_ingested=ts + 60_000,
        ts_committed=ts + 60_000,
    )


def test_append_and_read_roundtrip_preserves_decimals(tmp_path) -> None:
    store = RawStore(tmp_path, raw_archive_id="test")
    store.append("BTCUSDT", "1m", [_bar(0, "101.10")])
    bars = store.read("BTCUSDT", "1m")
    assert len(bars) == 1
    assert bars[0].close == Decimal("101.10")


# 任务单 04：原始柱落盘目录由调用方配置。
@pytest.mark.spec
def test_append_writes_to_configured_raw_archive(tmp_path) -> None:
    store = RawStore(tmp_path, raw_archive_id="test_market")
    store.append("BTCUSDT", "1m", [_bar(0, "101.10")])

    archive = tmp_path / "data" / "raw" / "test_market" / "BTCUSDT" / "1m.jsonl"
    assert archive.is_file()


def test_last_ingested_wins_for_duplicate_timestamp(tmp_path) -> None:
    store = RawStore(tmp_path, raw_archive_id="test")
    store.append("BTCUSDT", "1m", [_bar(0, "100")])
    store.append("BTCUSDT", "1m", [_bar(0, "200")])
    bars = store.read("BTCUSDT", "1m")
    assert len(bars) == 1
    assert bars[0].close == Decimal("200")


def test_read_returns_timestamps_in_ascending_order(tmp_path) -> None:
    store = RawStore(tmp_path, raw_archive_id="test")
    store.append("BTCUSDT", "1m", [_bar(120_000, "102"), _bar(0, "100"), _bar(60_000, "101")])
    assert [bar.ts_event for bar in store.read("BTCUSDT", "1m")] == [0, 60_000, 120_000]


def test_missing_file_reads_as_empty_list(tmp_path) -> None:
    assert RawStore(tmp_path, raw_archive_id="test").read("ETHUSDT", "1h") == []
