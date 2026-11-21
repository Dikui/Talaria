"""冷库数值格式：按列 scale 的 decimal128(18, s) 与逐文件元数据。

来源：data-access/01 任务单、ADR-0017（按列精度）与 ADR-0018（修订/时点）。
测试通过 ``ColdStore`` 的公开边界与落盘 Parquet 的 schema / metadata 验证行为，
不假设全库使用同一个 scale。
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from talaria.foundation.connector import RawBar
from talaria.foundation.errors import ColdStoreQualityError
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import (
    COLD_COLUMN_SCALES_METADATA,
    COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA,
    COLD_INSTRUMENT_VERSION_METADATA,
    ColdBatch,
    ColdStore,
    cold_column_scales,
    instrument_spec_version,
)

pytestmark = [pytest.mark.medium, pytest.mark.spec]

INSTRUMENT = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
assert INSTRUMENT is not None
TIMEFRAME = "1m"
BASE = 1_763_596_800_000
MINUTE = 60_000


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
        revision=revision,
    )


def _batch(bars: list[RawBar]) -> ColdBatch:
    return ColdBatch(instrument=INSTRUMENT, timeframe=TIMEFRAME, bars=bars)


@pytest.fixture
def store(tmp_path: Path):
    instance = ColdStore(tmp_path)
    yield instance
    instance.close()


def test_cold_parquet_uses_per_column_decimal128_and_records_metadata(
    store: ColdStore, tmp_path: Path
) -> None:
    # 仓库样本中的成交额 809038.73 必须无损落入 BTC 的 turnover scale=4。
    bars = [_bar(BASE, volume="12.345", turnover="809038.73")]
    generation = store.publish(_batch(bars))

    schema = pq.read_schema(tmp_path / generation.parquet_path)
    scales = cold_column_scales(INSTRUMENT)
    assert scales == {
        "open": 1,
        "high": 1,
        "low": 1,
        "close": 1,
        "volume": 3,
        "turnover": 4,
    }
    for name, scale in scales.items():
        field = schema.field(name)
        assert pa.types.is_decimal(field.type), name
        assert field.type.precision == 18, name
        assert field.type.scale == scale, name
        segment = pq.ParquetFile(tmp_path / generation.parquet_path).metadata.row_group(0)
        assert segment.column(schema.names.index(name)).physical_type == "INT64", name

    metadata = schema.metadata or {}
    assert metadata[COLD_FORMAT_METADATA] == COLD_FORMAT_DECIMAL128
    assert json.loads(metadata[COLD_COLUMN_SCALES_METADATA]) == scales
    assert (
        metadata[COLD_INSTRUMENT_VERSION_METADATA].decode("ascii")
        == instrument_spec_version(INSTRUMENT)
    )
    assert pa.types.is_dictionary(schema.field("precision").type)
    assert schema.field("revision").type == pa.int64()

    read_back = store.read_bars(INSTRUMENT, TIMEFRAME)
    assert read_back == bars
    assert read_back[0].turnover == Decimal("809038.73")


@pytest.mark.parametrize(
    ("column", "value", "match"),
    [
        pytest.param("turnover", "100.50001", "scale", id="turnover-beyond-scale"),
        pytest.param("close", "100.55", "scale", id="price-beyond-scale"),
        pytest.param(
            "turnover", "123456789012345678.9", "decimal128", id="beyond-18-digits"
        ),
        pytest.param("turnover", "NaN", "有限", id="non-finite"),
    ],
)
def test_publish_rejects_values_outside_column_decimal128(
    store: ColdStore, tmp_path: Path, column: str, value: str, match: str
) -> None:
    bars = [_bar(BASE), _bar(BASE + MINUTE)]
    bars[1] = replace(bars[1], **{column: Decimal(value)})

    with pytest.raises(ColdStoreQualityError, match=match):
        store.publish(_batch(bars))

    assert store.generations(INSTRUMENT, TIMEFRAME) == []
    assert not list((tmp_path / "data" / "cold").rglob("*.parquet"))


def test_generation_keeps_two_revisions_of_one_ts(
    store: ColdStore, tmp_path: Path
) -> None:
    first = _bar(BASE)
    revision = _bar(
        BASE,
        close="101.0",
        revision=1,
        ts_available=BASE + 10 * MINUTE,
        ts_ingested=BASE + 10 * MINUTE + 5,
        ts_committed=BASE + 10 * MINUTE + 10,
    )
    generation = store.publish(_batch([first, revision]))

    table = pq.read_table(tmp_path / generation.parquet_path)
    assert table.num_rows == 2
    assert table.column("revision").to_pylist() == [0, 1]
    assert [bar.revision for bar in store.read_bars(INSTRUMENT, TIMEFRAME)] == [1]
    assert (
        store.read_bars(INSTRUMENT, TIMEFRAME, as_of=BASE + MINUTE)[0].close
        == Decimal("100.5")
    )


def test_read_applies_as_of_then_returns_latest_revision(store: ColdStore) -> None:
    first = store.publish(_batch([_bar(BASE), _bar(BASE + MINUTE)]))
    revision = _bar(
        BASE,
        close="101.0",
        revision=1,
        ts_available=BASE + 10 * MINUTE,
        ts_ingested=BASE + 10 * MINUTE + 5,
        ts_committed=BASE + 10 * MINUTE + 10,
    )
    store.publish(_batch([revision]))

    early = store.read_bars(INSTRUMENT, TIMEFRAME, as_of=BASE + 2 * MINUTE)
    assert [bar.close for bar in early] == [Decimal("100.5"), Decimal("100.5")]
    assert [bar.revision for bar in early] == [0, 0]

    before_revision = store.read_bars(
        INSTRUMENT, TIMEFRAME, as_of=BASE + 10 * MINUTE - 1
    )
    assert before_revision[0].close == Decimal("100.5")

    at_revision = store.read_bars(INSTRUMENT, TIMEFRAME, as_of=BASE + 10 * MINUTE)
    assert [bar.close for bar in at_revision] == [Decimal("101.0"), Decimal("100.5")]
    assert at_revision[0].revision == 1

    latest = store.read_bars(INSTRUMENT, TIMEFRAME)
    assert latest == at_revision
    assert len(store.generations(INSTRUMENT, TIMEFRAME)) == 2

    # 修订批次不推进游标：最后事件时间与最新整批 generation 都保持原值。
    cursor = store.cursor(INSTRUMENT, TIMEFRAME)
    assert cursor is not None
    assert cursor.last_ts == BASE + MINUTE
    assert cursor.generation_id == first.generation_id


def test_reader_decodes_each_partition_with_its_own_scales(
    store: ColdStore, tmp_path: Path
) -> None:
    coarse = store.publish(_batch([_bar(BASE)]))
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
    fine = store.publish(
        ColdBatch(instrument=fine_instrument, timeframe=TIMEFRAME, bars=[fine_bar])
    )

    coarse_schema = pq.read_schema(tmp_path / coarse.parquet_path)
    fine_schema = pq.read_schema(tmp_path / fine.parquet_path)
    assert coarse_schema.field("close").type.scale == 1
    assert fine_schema.field("close").type.scale == 2
    assert coarse_schema.field("turnover").type.scale == 4
    assert fine_schema.field("turnover").type.scale == 6

    expected = [_bar(BASE), fine_bar]
    assert store.read_bars(fine_instrument, TIMEFRAME) == expected
    assert store.read_bars(INSTRUMENT, TIMEFRAME) == expected
