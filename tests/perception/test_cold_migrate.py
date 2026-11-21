"""data-access/01：一次性迁移旧字符串冷库到 decimal128（ADR-0017/0018）。"""

from __future__ import annotations

import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from talaria.cli.main import main
from talaria.foundation.connector import RawBar
from talaria.foundation.errors import (
    ColdStoreError,
    ColdStoreIntegrityError,
    ColdStoreQualityError,
)
from talaria.foundation.types import PrecisionTier
from talaria.packs import CRYPTO_PERP_PACK, DEFAULT_PACK
from talaria.perception.cold_migrate import migrate_cold_store
from talaria.perception.cold_store import (
    COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA,
    MANIFEST_DB,
    ColdBatch,
    ColdStore,
)

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000

pytestmark = [pytest.mark.medium, pytest.mark.spec]

_LEGACY_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string()),
        pa.field("timeframe", pa.string()),
        pa.field("ts_event", pa.int64()),
        pa.field("open", pa.string()),
        pa.field("high", pa.string()),
        pa.field("low", pa.string()),
        pa.field("close", pa.string()),
        pa.field("volume", pa.string()),
        pa.field("turnover", pa.string()),
        pa.field("ts_available", pa.int64()),
        pa.field("ts_ingested", pa.int64()),
        pa.field("ts_committed", pa.int64()),
        pa.field("precision", pa.string()),
    ]
)


def _bar(
    ts: int,
    *,
    open_: str = "100.0",
    high: str = "101.0",
    low: str = "99.0",
    close: str = "100.5",
    volume: str = "1.0",
    turnover: str = "100.5",
    revision: int = 0,
    ts_available: int | None = None,
    ts_ingested: int | None = None,
    ts_committed: int | None = None,
    symbol: str = "BTCUSDT",
):
    return RawBar(
        symbol=symbol,
        timeframe=TIMEFRAME,
        ts_event=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(turnover),
        ts_available=ts_available if ts_available is not None else ts + MINUTE,
        ts_ingested=ts_ingested if ts_ingested is not None else ts + MINUTE + 5,
        ts_committed=ts_committed if ts_committed is not None else ts + MINUTE + 10,
        precision=PrecisionTier.SECOND_VERIFIED,
        revision=revision,
    )


def _seed_legacy(
    data_dir: Path,
    bars: list,
    *,
    generation_id: str = "legacy-generation-0",
    venue: str = "bybit",
    symbol: str = "BTCUSDT",
    timeframe: str = TIMEFRAME,
) -> Path:
    """用旧发布格式造一个冷库清单 + 字符串 Parquet generation。"""

    ColdStore(data_dir).close()
    relative = (
        Path("data") / "cold" / venue / symbol / timeframe / f"{generation_id}.parquet"
    )
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "ts_event": bar.ts_event,
            "open": format(bar.open, "f"),
            "high": format(bar.high, "f"),
            "low": format(bar.low, "f"),
            "close": format(bar.close, "f"),
            "volume": format(bar.volume, "f"),
            "turnover": format(bar.turnover, "f"),
            "ts_available": bar.ts_available,
            "ts_ingested": bar.ts_ingested,
            "ts_committed": bar.ts_committed,
            "precision": bar.precision.value,
        }
        for bar in bars
    ]
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_LEGACY_SCHEMA), path, compression="zstd"
    )
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    connection = sqlite3.connect(data_dir / MANIFEST_DB)
    try:
        connection.execute(
            """
            INSERT INTO generations (
                generation_id, venue, symbol, timeframe, parquet_path, sha256,
                content_sha256, first_ts, last_ts, bars, committed_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                generation_id,
                venue,
                symbol,
                timeframe,
                relative.as_posix(),
                sha256,
                hashlib.sha256(generation_id.encode("utf-8")).hexdigest(),
                bars[0].ts_event,
                bars[-1].ts_event,
                len(bars),
                max(bar.ts_committed for bar in bars),
            ),
        )
        connection.execute(
            """
            INSERT INTO cursors (
                venue, symbol, timeframe, generation_id, last_ts, bars, updated_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, symbol, timeframe) DO UPDATE SET
                generation_id = excluded.generation_id,
                last_ts = excluded.last_ts,
                bars = excluded.bars,
                updated_ts = excluded.updated_ts
            """,
            (
                venue,
                symbol,
                timeframe,
                generation_id,
                bars[-1].ts_event,
                len(bars),
                max(bar.ts_committed for bar in bars),
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return relative


def _manifest_rows(data_dir: Path) -> tuple[list[tuple], list[tuple]]:
    connection = sqlite3.connect(data_dir / MANIFEST_DB)
    try:
        generations = connection.execute(
            """
            SELECT generation_id, parquet_path, sha256, first_ts, last_ts, bars
            FROM generations ORDER BY rowid
            """
        ).fetchall()
        cursors = connection.execute(
            "SELECT generation_id, last_ts, bars FROM cursors ORDER BY rowid"
        ).fetchall()
        return generations, cursors
    finally:
        connection.close()


def test_migrate_rewrites_legacy_generation_and_preserves_every_decimal(
    tmp_path: Path,
) -> None:
    bars = [
        _bar(BASE, volume="12.345", turnover="809038.73"),
        _bar(BASE + MINUTE),
    ]
    old_relative = _seed_legacy(tmp_path, bars)
    old_path = tmp_path / old_relative
    old_bytes = old_path.read_bytes()

    report = migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert report.migrated_generations == 1
    assert report.already_current_generations == 0
    assert report.updated_cursors == 1
    assert report.manifest_switched is True
    assert old_path.read_bytes() == old_bytes

    store = ColdStore(tmp_path)
    try:
        generations = store.generations(INSTRUMENT, TIMEFRAME)
        assert len(generations) == 1
        generation = generations[0]
        assert generation.parquet_path != old_relative
        assert generation.content_sha256 == generation.generation_id
        assert generation.first_ts == BASE
        assert generation.last_ts == BASE + MINUTE

        read_back = store.read_bars(INSTRUMENT, TIMEFRAME)
        assert read_back == bars
        assert [bar.turnover for bar in read_back] == [
            Decimal("809038.73"),
            Decimal("100.5"),
        ]
        cursor = store.cursor(INSTRUMENT, TIMEFRAME)
        assert cursor is not None
        assert cursor.generation_id == generation.generation_id
        assert cursor.last_ts == BASE + MINUTE
    finally:
        store.close()

    schema = pq.read_schema(tmp_path / generation.parquet_path)
    assert schema.metadata[COLD_FORMAT_METADATA] == COLD_FORMAT_DECIMAL128
    assert schema.field("close").type.scale == 1
    assert schema.field("turnover").type.scale == 4


def test_migrated_partition_and_new_precision_partition_read_together(
    tmp_path: Path,
) -> None:
    coarse_bar = _bar(BASE)
    _seed_legacy(tmp_path, [coarse_bar])
    migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    fine_instrument = INSTRUMENT.model_copy(
        update={
            "tick_size": Decimal("0.01"),
            "qty_step": Decimal("0.0001"),
            "price_precision": 2,
            "qty_precision": 4,
        }
    )
    fine_bar = _bar(
        BASE + MINUTE,
        open_="101.25",
        high="102.50",
        low="100.75",
        close="101.25",
        volume="1.2345",
        turnover="124.9999",
    )
    store = ColdStore(tmp_path)
    try:
        store.publish(
            ColdBatch(instrument=fine_instrument, timeframe=TIMEFRAME, bars=[fine_bar])
        )
        assert store.read_bars(fine_instrument, TIMEFRAME) == [coarse_bar, fine_bar]
        generations = store.generations(fine_instrument, TIMEFRAME)
        assert len(generations) == 2
    finally:
        store.close()

    coarse_schema = pq.read_schema(tmp_path / generations[0].parquet_path)
    fine_schema = pq.read_schema(tmp_path / generations[1].parquet_path)
    assert coarse_schema.field("turnover").type.scale == 4
    assert fine_schema.field("turnover").type.scale == 6


def test_migrate_is_idempotent_and_does_not_touch_manifest_or_files(
    tmp_path: Path,
) -> None:
    _seed_legacy(tmp_path, [_bar(BASE), _bar(BASE + MINUTE)])
    first = migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)
    assert first.migrated_generations == 1

    rows_before = _manifest_rows(tmp_path)
    store = ColdStore(tmp_path)
    try:
        new_path = tmp_path / store.generations(INSTRUMENT, TIMEFRAME)[0].parquet_path
    finally:
        store.close()
    bytes_before = new_path.read_bytes()

    second = migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert second.migrated_generations == 0
    assert second.already_current_generations == 1
    assert second.manifest_switched is False
    assert _manifest_rows(tmp_path) == rows_before
    assert new_path.read_bytes() == bytes_before


def test_migrate_failure_leaves_old_manifest_untouched(tmp_path: Path) -> None:
    good = _bar(BASE)
    bad = _bar(BASE + MINUTE)
    _seed_legacy(tmp_path, [good], generation_id="legacy-good")
    bad_relative = _seed_legacy(tmp_path, [bad], generation_id="legacy-bad")
    bad_path = tmp_path / bad_relative
    bad_path.write_bytes(bad_path.read_bytes() + b"corrupt")
    rows_before = _manifest_rows(tmp_path)

    with pytest.raises(ColdStoreIntegrityError, match="哈希"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert _manifest_rows(tmp_path) == rows_before
    assert (tmp_path / "data" / "cold" / "bybit" / "BTCUSDT" / "1m" / "legacy-good.parquet").is_file()
    assert len(_manifest_rows(tmp_path)[0]) == 2


def test_migrate_rejects_legacy_values_outside_new_scale(tmp_path: Path) -> None:
    _seed_legacy(tmp_path, [_bar(BASE, turnover="100.50001")])
    rows_before = _manifest_rows(tmp_path)

    with pytest.raises(ColdStoreQualityError, match="scale"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert _manifest_rows(tmp_path) == rows_before
    assert len(list((tmp_path / "data" / "cold").rglob("*.parquet"))) == 1


def test_migrate_rejects_instrument_missing_from_pack(tmp_path: Path) -> None:
    _seed_legacy(tmp_path, [_bar(BASE, symbol="NOPEUSDT")], symbol="NOPEUSDT")
    rows_before = _manifest_rows(tmp_path)

    with pytest.raises(ColdStoreError, match="市场包"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert _manifest_rows(tmp_path) == rows_before


def test_cold_migrate_cli_migrates_then_reports_no_work(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_legacy(tmp_path, [_bar(BASE), _bar(BASE + MINUTE)])

    assert main(["cold-migrate", "--data-dir", str(tmp_path), "--pack", DEFAULT_PACK]) == 0
    output = capsys.readouterr().out
    assert "迁移 1 个 generation" in output

    store = ColdStore(tmp_path)
    try:
        assert [bar.ts_event for bar in store.read_bars(INSTRUMENT, TIMEFRAME)] == [
            BASE,
            BASE + MINUTE,
        ]
    finally:
        store.close()

    assert main(["cold-migrate", "--data-dir", str(tmp_path), "--pack", DEFAULT_PACK]) == 0
    assert "无需迁移" in capsys.readouterr().out
