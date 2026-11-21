"""热端切片物化：HotStreamBuffer、快照合并、cutoff 截断与重启逐字节重现。

来源：任务单 06、docs/architecture.md §3.2 与 §5.2。测试通过
``SnapshotBuilder`` / ``SnapshotStore`` 的公开边界与冻结后的 Parquet 字节验证，
不依赖内存环结构。
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.connector import RawBar
from talaria.foundation.feed import KlineEvent
from talaria.foundation.snapshot import SnapshotRequest
from talaria.foundation.types import PrecisionTier
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.backfill import BackfillPolicy
from talaria.perception.hot_buffer import HotStreamBuffer
from talaria.perception.raw_store import RawStore
from talaria.perception.snapshot_builder import SnapshotBuilder
from talaria.perception.snapshot_store import (
    SnapshotStore,
    read_snapshot_bars,
    read_snapshot_entries,
)

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
assert INSTRUMENT is not None
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000
NOW = BASE + 10 * MINUTE


def _raw(ts: int, *, close: str = "100.0", committed: int | None = None) -> RawBar:
    value = Decimal(close)
    return RawBar(
        symbol=INSTRUMENT.symbol,
        timeframe=TIMEFRAME,
        ts_event=ts,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("1"),
        turnover=value,
        ts_available=ts + MINUTE,
        ts_ingested=ts + MINUTE + 5,
        ts_committed=ts + MINUTE + 10 if committed is None else committed,
    )


class StubConnector:
    def __init__(self, bars: list[RawBar] | None = None) -> None:
        self.bars = list(bars or [])
        self.calls = 0

    def fetch_bars(
        self,
        *,
        symbol: str,
        timeframe: str,
        end_ts: int | None = None,
        limit: int = 1000,
    ) -> list[RawBar]:
        self.calls += 1
        return list(self.bars)


class StubHotBars:
    def __init__(self, bars: list[RawBar]) -> None:
        self.bars = list(bars)

    def recent_bars(self, *, instrument_id: str, timeframe: str) -> list[RawBar]:
        assert instrument_id == INSTRUMENT.instrument_id
        return [bar for bar in self.bars if bar.timeframe == timeframe]


def _builder(
    tmp_path: Path,
    *,
    history: list[RawBar],
    hot: StubHotBars | None = None,
) -> tuple[SnapshotBuilder, RawStore, SnapshotStore]:
    raw_store = RawStore(tmp_path, raw_archive_id="test")
    raw_store.append(INSTRUMENT.symbol, TIMEFRAME, history)
    snapshot_store = SnapshotStore(tmp_path)
    builder = SnapshotBuilder(
        data_dir=tmp_path,
        connector=StubConnector(),
        raw_store=raw_store,
        snapshot_store=snapshot_store,
        policy=BackfillPolicy(target_bars=2, max_pages=1),
        now_ms=lambda: NOW,
        hot_bars=hot,
    )
    return builder, raw_store, snapshot_store


def _request(cutoff: int) -> SnapshotRequest:
    return SnapshotRequest(
        instrument_id=INSTRUMENT.instrument_id, timeframes=[TIMEFRAME], cutoff_ts=cutoff
    )


def test_snapshot_merges_unarchived_hot_bars_and_truncates_at_cutoff(tmp_path: Path) -> None:
    history = [_raw(BASE), _raw(BASE + MINUTE)]
    hot_inside = _raw(BASE + 2 * MINUTE, close="102.0", committed=NOW - 5)
    hot_future = _raw(BASE + 3 * MINUTE, close="103.0", committed=NOW + 5_000)
    builder, raw_store, snapshot_store = _builder(
        tmp_path, history=history, hot=StubHotBars([hot_inside, hot_future])
    )
    try:
        meta = builder.build(
            instrument=INSTRUMENT,
            request=_request(cutoff=BASE + 3 * MINUTE),
        )
        record = snapshot_store.get(meta.snapshot_id)
        assert record is not None
        entries = read_snapshot_entries(tmp_path, record.parquet_path, TIMEFRAME)
        assert [entry.bar.timestamp for entry in entries] == [
            BASE,
            BASE + MINUTE,
            BASE + 2 * MINUTE,
        ]
        assert entries[-1].bar.close == Decimal("102.0")
        assert entries[-1].ts_committed == NOW - 5
        assert entries[0].ts_committed == BASE + MINUTE + 10
        # 热端切片只进入冻结快照，不污染已归档原始存储。
        assert [bar.ts_event for bar in raw_store.read(INSTRUMENT.symbol, TIMEFRAME)] == [
            BASE,
            BASE + MINUTE,
        ]
        assert record.committed_ts == NOW - 5
    finally:
        snapshot_store.close()


def test_hot_revision_wins_over_archived_same_timestamp(tmp_path: Path) -> None:
    history = [_raw(BASE), _raw(BASE + MINUTE, close="100.0")]
    revision = _raw(BASE + MINUTE, close="101.0", committed=NOW)
    builder, _, snapshot_store = _builder(
        tmp_path, history=history, hot=StubHotBars([revision])
    )
    try:
        meta = builder.build(instrument=INSTRUMENT, request=_request(cutoff=NOW))
        record = snapshot_store.get(meta.snapshot_id)
        assert record is not None
        entries = read_snapshot_entries(tmp_path, record.parquet_path, TIMEFRAME)
        assert [entry.bar.timestamp for entry in entries] == [BASE, BASE + MINUTE]
        assert entries[-1].bar.close == Decimal("101.0")
        assert entries[-1].ts_committed == NOW
    finally:
        snapshot_store.close()


def test_archived_higher_revision_wins_over_later_hot_bar(tmp_path: Path) -> None:
    archived = replace(_raw(BASE, close="101.0"), revision=1)
    hot = _raw(BASE, close="99.0", committed=NOW - 5)
    builder, _, snapshot_store = _builder(
        tmp_path, history=[archived], hot=StubHotBars([hot])
    )
    try:
        meta = builder.build(instrument=INSTRUMENT, request=_request(cutoff=NOW))
        record = snapshot_store.get(meta.snapshot_id)
        assert record is not None
        entries = read_snapshot_entries(tmp_path, record.parquet_path, TIMEFRAME)
        assert [entry.bar.close for entry in entries] == [Decimal("101.0")]
        assert entries[0].ts_committed == BASE + MINUTE + 10
    finally:
        snapshot_store.close()


def test_snapshot_is_byte_identical_after_store_restart(tmp_path: Path) -> None:
    history = [_raw(BASE), _raw(BASE + MINUTE)]
    hot = StubHotBars([_raw(BASE + 2 * MINUTE, committed=NOW)])
    builder, _, snapshot_store = _builder(tmp_path, history=history, hot=hot)
    meta = builder.build(instrument=INSTRUMENT, request=_request(cutoff=NOW))
    record = snapshot_store.get(meta.snapshot_id)
    assert record is not None
    before_entries = read_snapshot_entries(tmp_path, record.parquet_path, TIMEFRAME)
    before_bytes = (tmp_path / record.parquet_path).read_bytes()
    before_digest = hashlib.sha256(before_bytes).hexdigest()
    assert all(entry.ts_committed is not None for entry in before_entries)
    snapshot_store.close()

    reopened = SnapshotStore(tmp_path)
    try:
        stored = reopened.get(meta.snapshot_id)
        assert stored is not None
        assert stored.meta == meta
        assert (tmp_path / stored.parquet_path).read_bytes() == before_bytes
        assert hashlib.sha256((tmp_path / stored.parquet_path).read_bytes()).hexdigest() == (
            before_digest
        )
        assert read_snapshot_entries(tmp_path, stored.parquet_path, TIMEFRAME) == before_entries
        assert read_snapshot_bars(tmp_path, stored.parquet_path, TIMEFRAME) == [
            entry.bar for entry in before_entries
        ]
    finally:
        reopened.close()


def test_reader_accepts_legacy_parquet_without_committed_column(tmp_path: Path) -> None:
    legacy = pa.table(
        {
            "timeframe": ["1m"],
            "timestamp": [BASE],
            "open": ["100.0"],
            "high": ["101.0"],
            "low": ["99.0"],
            "close": ["100.5"],
            "volume": ["1.0"],
            "turnover": ["100.5"],
            "status": ["NORMAL"],
            "ts_available": [BASE + MINUTE],
            "precision": ["SECOND_VERIFIED"],
        }
    )
    path = Path("state") / "hot_snapshots" / "legacy.parquet"
    (tmp_path / path).parent.mkdir(parents=True)
    pq.write_table(legacy, tmp_path / path)

    assert read_snapshot_bars(tmp_path, path, "1m") == [
        CompactBar(
            timestamp=BASE,
            open=Decimal("100.0"),
            high=Decimal("101.0"),
            low=Decimal("99.0"),
            close=Decimal("100.5"),
            volume=Decimal("1.0"),
            turnover=Decimal("100.5"),
            status=BarStatus.NORMAL,
            ts_available=BASE + MINUTE,
            precision=PrecisionTier.SECOND_VERIFIED,
        )
    ]
    assert read_snapshot_entries(tmp_path, path, "1m")[0].ts_committed is None


def test_hot_buffer_tracks_latest_revision_and_commit_time() -> None:
    clock = {"now": NOW}
    buffer = HotStreamBuffer(instruments=[INSTRUMENT], now_ms=lambda: clock["now"])
    compact = CompactBar(
        timestamp=BASE,
        open=Decimal("100.0"),
        high=Decimal("101.0"),
        low=Decimal("99.0"),
        close=Decimal("100.5"),
        volume=Decimal("1.0"),
        turnover=Decimal("100.5"),
        status=BarStatus.NORMAL,
        ts_available=BASE + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )
    buffer.ingest(
        KlineEvent(
            instrument_id=INSTRUMENT.instrument_id,
            timeframe=TIMEFRAME,
            bar=compact,
            confirmed=False,
            revision=0,
        )
    )
    first = buffer.recent_bars(instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME)
    assert len(first) == 1
    assert first[0].ts_committed == NOW

    clock["now"] = NOW + 1_000
    buffer.ingest(
        KlineEvent(
            instrument_id=INSTRUMENT.instrument_id,
            timeframe=TIMEFRAME,
            bar=compact.model_copy(update={"close": Decimal("100.8")}),
            confirmed=True,
            revision=1,
        )
    )
    latest = buffer.recent_bars(instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME)
    assert len(latest) == 1
    assert latest[0].close == Decimal("100.8")
    assert latest[0].ts_committed == NOW + 1_000
    assert latest[0].revision == 1


def test_hot_buffer_skips_feed_gap_bars() -> None:
    buffer = HotStreamBuffer(instruments=[INSTRUMENT], now_ms=lambda: NOW)
    gap = CompactBar(
        timestamp=BASE,
        open=None,
        high=None,
        low=None,
        close=None,
        volume=None,
        turnover=None,
        status=BarStatus.FEED_GAP,
        ts_available=BASE + MINUTE,
        precision=PrecisionTier.SECOND_VERIFIED,
    )
    buffer.ingest(
        KlineEvent(
            instrument_id=INSTRUMENT.instrument_id,
            timeframe=TIMEFRAME,
            bar=gap,
            confirmed=True,
            revision=0,
        )
    )
    assert buffer.recent_bars(instrument_id=INSTRUMENT.instrument_id, timeframe=TIMEFRAME) == []
