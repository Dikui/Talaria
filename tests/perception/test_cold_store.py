"""冷库发布：内容哈希归档、staging 质检、不可变 generation 与清单短事务。

来源：任务单 06 与 docs/architecture.md §3.2 / §5.2。测试只通过 ``ColdStore``
的公开边界（archive / publish / generations / read_bars / cursor）验证行为，
读取侧永远以 ``data/data_manifest.sqlite`` 中列出的文件清单为唯一真相。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import (
    ColdStoreIntegrityError,
    ColdStoreQualityError,
    SingleWriterViolationError,
)
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import (
    MANIFEST_DB,
    ColdBatch,
    ColdPhase,
    ColdStore,
)

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
assert INSTRUMENT is not None
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000


class InjectedFault(Exception):
    """模拟发布流程某一阶段崩溃。"""


class Fault:
    def __init__(self, phase: ColdPhase | None) -> None:
        self.phase = phase

    def __call__(self, phase: ColdPhase) -> None:
        if phase is self.phase:
            raise InjectedFault(phase.value)


def _bar(
    ts: int,
    *,
    open_: str = "100.0",
    high: str = "101.0",
    low: str = "99.0",
    close: str = "100.5",
    volume: str = "1.0",
    turnover: str = "100.5",
    ts_available: int | None = None,
    ts_ingested: int | None = None,
    ts_committed: int | None = None,
) -> RawBar:
    return RawBar(
        symbol="BTCUSDT",
        timeframe=TIMEFRAME,
        ts_event=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(turnover),
        ts_available=ts + MINUTE if ts_available is None else ts_available,
        ts_ingested=ts + MINUTE + 5 if ts_ingested is None else ts_ingested,
        ts_committed=ts + MINUTE + 10 if ts_committed is None else ts_committed,
    )


def _bars(count: int = 3, *, start: int = BASE) -> list[RawBar]:
    return [_bar(start + index * MINUTE) for index in range(count)]


def _batch(bars: list[RawBar] | None = None) -> ColdBatch:
    return ColdBatch(instrument=INSTRUMENT, timeframe=TIMEFRAME, bars=bars or _bars())


@pytest.fixture
def store(tmp_path: Path):
    instance = ColdStore(tmp_path)
    yield instance
    instance.close()


def test_archive_is_content_addressed_and_reused(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    try:
        batch = _batch()
        first = cold.archive(batch)
        second = cold.archive(batch)

        assert first.sha256 == second.sha256
        assert first.path == second.path
        assert first.path == Path("data") / "raw_archive" / f"{first.sha256}.jsonl"
        payload = (tmp_path / first.path).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == first.sha256
        assert payload.count(b"\n") == 3
        assert b'"ts_committed"' in payload
    finally:
        cold.close()


def test_publish_commits_generation_and_preserves_committed_time(
    store: ColdStore, tmp_path: Path
) -> None:
    bars = _bars()
    generation = store.publish(_batch(bars))

    assert generation.generation_id == store.archive(_batch(bars)).sha256
    assert generation.parquet_path == (
        Path("data") / "cold" / "bybit" / "BTCUSDT" / TIMEFRAME / f"{generation.generation_id}.parquet"
    )
    assert generation.first_ts == BASE
    assert generation.last_ts == BASE + 2 * MINUTE
    assert generation.bars == 3
    assert generation.committed_ts == BASE + 2 * MINUTE + MINUTE + 10
    parquet_bytes = (tmp_path / generation.parquet_path).read_bytes()
    assert hashlib.sha256(parquet_bytes).hexdigest() == generation.sha256

    assert store.generations(INSTRUMENT, TIMEFRAME) == [generation]
    assert store.read_bars(INSTRUMENT, TIMEFRAME) == bars
    cursor = store.cursor(INSTRUMENT, TIMEFRAME)
    assert cursor is not None
    assert cursor.generation_id == generation.generation_id
    assert cursor.last_ts == generation.last_ts
    assert cursor.bars == 3


def test_duplicate_batch_is_idempotent(store: ColdStore) -> None:
    first = store.publish(_batch())
    second = store.publish(_batch())

    assert second == first
    assert len(store.generations(INSTRUMENT, TIMEFRAME)) == 1


def test_archive_rejects_corrupted_content(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    try:
        archived = cold.archive(_batch())
        (tmp_path / archived.path).write_bytes(b"corrupted")
        with pytest.raises(ColdStoreIntegrityError):
            cold.archive(_batch())
    finally:
        cold.close()


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda bars: bars.__setitem__(1, replace(bars[1], ts_event=bars[0].ts_event)),
            id="duplicate-timestamp",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(0, replace(bars[0], high=Decimal("100.2"))),
            id="high-below-close",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(0, replace(bars[0], low=Decimal("100.4"))),
            id="low-above-open",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(0, replace(bars[0], volume=Decimal("-1"))),
            id="negative-volume",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(0, replace(bars[0], close=Decimal("100.55"))),
            id="price-off-tick",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(0, replace(bars[0], volume=Decimal("1.0005"))),
            id="quantity-off-step",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(
                0, replace(bars[0], ts_available=bars[0].ts_event - 1)
            ),
            id="available-before-event",
        ),
        pytest.param(
            lambda bars: bars.__setitem__(
                0, replace(bars[0], ts_committed=bars[0].ts_ingested - 1)
            ),
            id="committed-before-ingested",
        ),
    ],
)
def test_publish_rejects_batch_failing_quality_check(tmp_path: Path, mutate) -> None:
    bars = _bars()
    mutate(bars)
    cold = ColdStore(tmp_path)
    try:
        with pytest.raises(ColdStoreQualityError):
            cold.publish(_batch(bars))
        assert cold.generations(INSTRUMENT, TIMEFRAME) == []
        assert cold.read_bars(INSTRUMENT, TIMEFRAME) == []
        assert not list((tmp_path / "data" / "cold").rglob("*.parquet"))
    finally:
        cold.close()


@pytest.mark.parametrize(
    "phase",
    [
        ColdPhase.ARCHIVE_BEFORE,
        ColdPhase.ARCHIVE_AFTER,
        ColdPhase.STAGING_BEFORE,
        ColdPhase.STAGING_AFTER,
        ColdPhase.QC_BEFORE,
        ColdPhase.QC_AFTER,
        ColdPhase.RENAME_BEFORE,
        ColdPhase.RENAME_AFTER,
        ColdPhase.MANIFEST_BEFORE,
        ColdPhase.MANIFEST_AFTER,
    ],
)
def test_injected_crash_never_exposes_partial_generation(
    tmp_path: Path, phase: ColdPhase
) -> None:
    fault = Fault(phase)
    cold = ColdStore(tmp_path, fault_hook=fault)
    try:
        with pytest.raises(InjectedFault):
            cold.publish(_batch())

        # 清单是唯一发布真相：除 manifest 已提交后，读取侧看不到任何批次。
        if phase is ColdPhase.MANIFEST_AFTER:
            assert len(cold.generations(INSTRUMENT, TIMEFRAME)) == 1
            assert cold.read_bars(INSTRUMENT, TIMEFRAME) == _bars()
        else:
            assert cold.generations(INSTRUMENT, TIMEFRAME) == []
            assert cold.read_bars(INSTRUMENT, TIMEFRAME) == []

        fault.phase = None
        generation = cold.publish(_batch())
        assert len(cold.generations(INSTRUMENT, TIMEFRAME)) == 1
        assert cold.read_bars(INSTRUMENT, TIMEFRAME) == _bars()
        assert generation.bars == 3
    finally:
        cold.close()


def test_stale_staging_fragment_is_replaced(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    try:
        archived = cold.archive(_batch())
        staging = tmp_path / "data" / "staging" / archived.sha256
        staging.mkdir(parents=True)
        (staging / "bars.parquet").write_bytes(b"partial parquet from a crashed run")

        cold.publish(_batch())

        assert cold.read_bars(INSTRUMENT, TIMEFRAME) == _bars()
    finally:
        cold.close()


def test_reader_ignores_parquet_files_outside_the_manifest(store: ColdStore, tmp_path: Path) -> None:
    generation = store.publish(_batch())
    target = tmp_path / generation.parquet_path
    stray = target.with_name("stray.parquet")
    stray.write_bytes(target.read_bytes())

    assert store.read_bars(INSTRUMENT, TIMEFRAME) == _bars()

    stray.write_bytes(b"not a parquet file")
    assert store.read_bars(INSTRUMENT, TIMEFRAME) == _bars()


def test_reader_rejects_tampered_generation_file(store: ColdStore, tmp_path: Path) -> None:
    generation = store.publish(_batch())
    target = tmp_path / generation.parquet_path
    target.write_bytes(target.read_bytes() + b"tampered")

    with pytest.raises(ColdStoreIntegrityError):
        store.read_bars(INSTRUMENT, TIMEFRAME)


def test_close_and_reopen_preserves_generations_and_bytes(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    first = cold.publish(_batch(_bars(2)))
    second = cold.publish(_batch(_bars(2, start=BASE + 2 * MINUTE)))
    expected_generations = cold.generations(INSTRUMENT, TIMEFRAME)
    expected_bars = cold.read_bars(INSTRUMENT, TIMEFRAME)
    payloads = {
        generation.generation_id: (tmp_path / generation.parquet_path).read_bytes()
        for generation in expected_generations
    }
    cold.close()

    reopened = ColdStore(tmp_path)
    try:
        assert reopened.generations(INSTRUMENT, TIMEFRAME) == expected_generations
        assert reopened.read_bars(INSTRUMENT, TIMEFRAME) == expected_bars
        for generation in expected_generations:
            assert (tmp_path / generation.parquet_path).read_bytes() == payloads[
                generation.generation_id
            ]
        assert reopened.cursor(INSTRUMENT, TIMEFRAME).generation_id == second.generation_id
        assert first.generation_id != second.generation_id
    finally:
        reopened.close()


def test_manifest_uses_wal_and_checkpoints_on_close(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    cold.publish(_batch())
    connection = sqlite3.connect(tmp_path / MANIFEST_DB)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()
    cold.close()

    wal = Path(f"{tmp_path / MANIFEST_DB}-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_second_writer_is_rejected_until_close(tmp_path: Path) -> None:
    cold = ColdStore(tmp_path)
    try:
        with pytest.raises(SingleWriterViolationError):
            ColdStore(tmp_path)
    finally:
        cold.close()

    reopened = ColdStore(tmp_path)
    reopened.close()
