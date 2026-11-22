"""data-access/01：一次性迁移旧字符串冷库到 decimal128（ADR-0017/0018）。"""

from __future__ import annotations

import hashlib
import json
import select
import sqlite3
import subprocess
import sys
from contextlib import closing
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
    ARCHIVE_SUBDIR,
    COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA,
    COLD_INSTRUMENT_VERSION_METADATA,
    MANIFEST_DB,
    ColdBatch,
    ColdStore,
    instrument_spec_version,
)

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000

pytestmark = [pytest.mark.medium, pytest.mark.spec]

_LEGACY_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("ts_event", pa.int64(), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
        pa.field("turnover", pa.string(), nullable=False),
        pa.field("ts_available", pa.int64(), nullable=False),
        pa.field("ts_ingested", pa.int64(), nullable=False),
        pa.field("ts_committed", pa.int64(), nullable=False),
        pa.field("precision", pa.string(), nullable=False),
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
    generation_id: str | None = None,
    venue: str = "bybit",
    symbol: str = "BTCUSDT",
    timeframe: str = TIMEFRAME,
) -> Path:
    """用旧发布格式造一个冷库清单 + 字符串 Parquet generation。"""

    ColdStore(data_dir).close()
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
    archive_bytes = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    content_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    generation_id = generation_id or content_sha256
    relative = Path("data") / "cold" / venue / symbol / timeframe / f"{generation_id}.parquet"
    path = data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=_LEGACY_SCHEMA), path, compression="zstd"
    )
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    archive_path = data_dir / ARCHIVE_SUBDIR / f"{content_sha256}.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(archive_bytes)
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
                content_sha256,
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
            SELECT generation_id, parquet_path, sha256, content_sha256,
                   first_ts, last_ts, bars, committed_ts
            FROM generations ORDER BY rowid
            """
        ).fetchall()
        cursors = connection.execute(
            "SELECT generation_id, last_ts, bars, updated_ts FROM cursors ORDER BY rowid"
        ).fetchall()
        return generations, cursors
    finally:
        connection.close()


# data-access/01：同一旧格式文件通过公开读入口迁移前后数值相等。
@pytest.mark.regression
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
    assert pq.read_schema(old_path).field("turnover").type == pa.string()
    store = ColdStore(tmp_path)
    try:
        before = store.read_bars(INSTRUMENT, TIMEFRAME)
    finally:
        store.close()

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
        assert generation.first_ts == BASE
        assert generation.last_ts == BASE + MINUTE

        read_back = store.read_bars(INSTRUMENT, TIMEFRAME)
        assert read_back == before
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


# data-access/01：迁移后的清单继续引用原始 JSONL 字节。
@pytest.mark.regression
def test_migration_preserves_raw_archive_references(tmp_path: Path) -> None:
    _seed_legacy(tmp_path, [_bar(BASE)], generation_id="legacy-first")
    _seed_legacy(tmp_path, [_bar(BASE + MINUTE)], generation_id="legacy-second")
    archives = {
        path.stem: path.read_bytes() for path in (tmp_path / ARCHIVE_SUBDIR).glob("*.jsonl")
    }

    migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    store = ColdStore(tmp_path)
    try:
        generations = store.generations(INSTRUMENT, TIMEFRAME)
        assert {generation.content_sha256 for generation in generations} == set(archives)
        for generation in generations:
            digest = generation.content_sha256
            payload = (tmp_path / ARCHIVE_SUBDIR / f"{digest}.jsonl").read_bytes()
            assert payload == archives[digest]
            assert hashlib.sha256(payload).hexdigest() == digest
    finally:
        store.close()


@pytest.mark.regression
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_migration_rejects_unverifiable_raw_archive(tmp_path: Path, damage: str) -> None:
    _seed_legacy(tmp_path, [_bar(BASE)])
    before = _manifest_rows(tmp_path)
    archive = next((tmp_path / ARCHIVE_SUBDIR).glob("*.jsonl"))
    if damage == "missing":
        archive.unlink()
    else:
        archive.write_bytes(b"corrupt")

    with pytest.raises(ColdStoreIntegrityError, match="原始归档"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert _manifest_rows(tmp_path) == before


# data-access/01：第二代游标更新失败时，回滚此前已执行的清单变更。
@pytest.mark.regression
def test_migration_rolls_back_manifest_after_mid_transaction_failure(tmp_path: Path) -> None:
    bars = [_bar(BASE), _bar(BASE + MINUTE)]
    first = _seed_legacy(tmp_path, bars[:1], generation_id="legacy-first")
    second = _seed_legacy(tmp_path, bars[1:], generation_id="legacy-second")
    old_files = {path: (tmp_path / path).read_bytes() for path in (first, second)}
    before = _manifest_rows(tmp_path)
    with closing(sqlite3.connect(tmp_path / MANIFEST_DB)) as connection, connection:
        connection.execute(
            """
            CREATE TRIGGER fail_second_cursor BEFORE UPDATE ON cursors
            WHEN OLD.generation_id = 'legacy-second'
            BEGIN
                SELECT RAISE(ABORT, 'injected manifest failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected manifest failure"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)

    assert _manifest_rows(tmp_path) == before
    assert all((tmp_path / path).read_bytes() == payload for path, payload in old_files.items())
    with closing(sqlite3.connect(tmp_path / MANIFEST_DB)) as connection, connection:
        connection.execute("DROP TRIGGER fail_second_cursor")

    assert migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK).migrated_generations == 2
    assert migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK).migrated_generations == 0
    store = ColdStore(tmp_path)
    try:
        assert store.read_bars(INSTRUMENT, TIMEFRAME) == bars
    finally:
        store.close()


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


# data-access/01：同一标的的旧 generation 可以使用细于当前值的来源规格。
@pytest.mark.regression
def test_cli_migrates_historical_generations_with_their_source_specs(tmp_path: Path) -> None:
    fine_bar = _bar(BASE, close="100.55", turnover="456.12345")
    coarse_bar = _bar(BASE + MINUTE)
    _seed_legacy(tmp_path, [fine_bar], generation_id="legacy-fine")
    _seed_legacy(tmp_path, [coarse_bar], generation_id="legacy-coarse")
    before = _manifest_rows(tmp_path)
    with pytest.raises(ColdStoreQualityError, match="close.*scale=1"):
        migrate_cold_store(tmp_path, pack=CRYPTO_PERP_PACK)
    assert _manifest_rows(tmp_path) == before
    specs = tmp_path / "source-specs.json"
    specs.write_text(
        json.dumps(
            {
                "bybit/BTCUSDT/1m/legacy-fine": {
                    "tick_size": "0.01",
                    "qty_step": "0.001",
                    "price_precision": 2,
                    "qty_precision": 3,
                }
            }
        ),
        encoding="utf-8",
    )
    args = [
        "cold-migrate", "--data-dir", str(tmp_path), "--pack", DEFAULT_PACK,
        "--source-specs", str(specs),
    ]

    assert main(args) == 0
    assert main(args) == 0

    store = ColdStore(tmp_path)
    try:
        assert store.read_bars(INSTRUMENT, TIMEFRAME) == [fine_bar, coarse_bar]
        fine, coarse = store.generations(INSTRUMENT, TIMEFRAME)
    finally:
        store.close()
    fine_schema = pq.read_schema(tmp_path / fine.parquet_path)
    coarse_schema = pq.read_schema(tmp_path / coarse.parquet_path)
    assert fine_schema.field("close").type.scale == 2
    assert fine_schema.field("turnover").type.scale == 5
    assert coarse_schema.field("close").type.scale == 1
    fine_spec = INSTRUMENT.model_copy(
        update={"tick_size": Decimal("0.01"), "price_precision": 2}
    )
    assert fine_schema.metadata[COLD_INSTRUMENT_VERSION_METADATA].decode() == (
        instrument_spec_version(fine_spec)
    )


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


# data-access/01：竞争进程在迁移尝试打开清单、关闭清单之前均无法进入。
@pytest.mark.regression
def test_cold_migrate_cli_rejects_competing_migration_process(tmp_path: Path) -> None:
    _seed_legacy(tmp_path, [_bar(BASE)])
    args = ["cold-migrate", "--data-dir", str(tmp_path), "--pack", DEFAULT_PACK]
    # 用管道暂停真实迁移进程，使竞争发生在两个确定的连接生命周期节点。
    pause_migration = """
import sys
from unittest.mock import patch

from talaria.cli.main import main
from talaria.perception import cold_migrate

def pause(label, callback):
    def paused(*args):
        print(label, flush=True)
        if sys.stdin.readline().strip() != "continue":
            raise RuntimeError("missing test synchronization")
        return callback(*args)
    return paused

with (
    patch.object(cold_migrate, "open_writer_connection",
                 pause("opening", cold_migrate.open_writer_connection)),
    patch.object(cold_migrate, "checkpoint_and_close",
                 pause("closing", cold_migrate.checkpoint_and_close)),
):
    raise SystemExit(main(sys.argv[1:]))
"""
    migration = subprocess.Popen(
        [sys.executable, "-c", pause_migration, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert migration.stdin is not None
        assert migration.stdout is not None
        for phase in ("opening", "closing"):
            ready, _, _ = select.select([migration.stdout], [], [], 10)
            assert ready, f"migration did not reach {phase}"
            assert migration.stdout.readline().strip() == phase
            competitor = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from talaria.cli.main import main; raise SystemExit(main())",
                    *args,
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert competitor.returncode != 0, competitor.stdout
            assert "持有者" in competitor.stderr
            assert f"pid={migration.pid}" in competitor.stderr
            assert "角色=cold-migrate" in competitor.stderr
            migration.stdin.write("continue\n")
            migration.stdin.flush()

        stdout, stderr = migration.communicate(timeout=10)
        assert migration.returncode == 0, stderr
        assert "迁移 1 个 generation" in stdout
    finally:
        if migration.poll() is None:
            migration.kill()
            migration.communicate(timeout=10)

    assert main(args) == 0
