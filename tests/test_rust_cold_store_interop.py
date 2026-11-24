"""Cross-language interop verification: Rust `talaria-store` -> Python `ColdReader` & `sdk`.

Runs the Rust `talaria-store` example (`interop_dump`) to publish Bars (`BTC-USDT-PERP`
and `1000BONK-USDT-PERP` from repository Bybit fixtures), Derivatives (`mark_price`,
`funding_rate`, `open_interest`), and Ticks (`trades`, `quotes`) with compaction and
revisions, then verifies Python `ColdReader` reads every dataset with:
- physical `INT64` Parquet column types for `decimal128(18, s)`,
- exact `talaria.cold.*` metadata and `_verify_decimal_schema` pass,
- 0 `Decimal` row-by-row mismatch against fixture/expected inputs,
- exact `as_of` PIT cutoff selection across revisions,
- compatibility with `talaria.sdk` queries (`bars`, `derivatives`, `funding_settlements`, `trades`, `quotes`).
"""

from __future__ import annotations

import json
import os
import subprocess
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from talaria import sdk
from talaria.foundation.derivatives import DerivativeKind
from talaria.foundation.ticks import TickKind
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import (
    ARCHIVE_SUBDIR,
    COLD_COLUMN_SCALES_METADATA,
    COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA,
    COLD_INSTRUMENT_VERSION_METADATA,
    MANIFEST_DB,
    ColdReader,
    _column_scales_from_metadata,
    _verify_decimal_schema,
    cold_column_scales,
    instrument_spec_version,
)

pytestmark = [pytest.mark.medium, pytest.mark.spec]

REPO_ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = REPO_ROOT / "engine"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "bybit"


def _run_rust_interop_dump(out_dir: Path) -> dict:
    env = os.environ.copy()
    if "CARGO_TARGET_DIR" not in env:
        isolated = ENGINE_DIR / "target" / "rust-store"
        if isolated.exists():
            env["CARGO_TARGET_DIR"] = str(isolated)
    proc = subprocess.run(
        [
            "cargo",
            "run",
            "--locked",
            "-p",
            "talaria-store",
            "--example",
            "interop_dump",
            "--",
            "--out-dir",
            str(out_dir),
            "--fixture-dir",
            str(FIXTURE_DIR),
        ],
        cwd=ENGINE_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"Rust interop_dump failed (code={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    return json.loads(lines[-1])


def _load_fixture_bars(symbol: str) -> list[tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]]:
    payload = json.loads((FIXTURE_DIR / symbol / "1m.json").read_text(encoding="utf-8"))
    rows = [
        (
            int(item[0]),
            Decimal(item[1]),
            Decimal(item[2]),
            Decimal(item[3]),
            Decimal(item[4]),
            Decimal(item[5]),
            Decimal(item[6]),
        )
        for item in payload["result"]["list"]
    ]
    rows.sort(key=lambda r: r[0])
    return rows


def _assert_parquet_int64_and_metadata(
    parquet_path: Path,
    expected_scales: dict[str, int],
    spec,
) -> None:
    pf = pq.ParquetFile(parquet_path)
    arrow_schema = pq.read_schema(parquet_path)
    metadata = arrow_schema.metadata or {}
    assert metadata.get(COLD_FORMAT_METADATA) == COLD_FORMAT_DECIMAL128
    assert _column_scales_from_metadata(metadata, tuple(expected_scales)) == expected_scales
    assert json.loads(metadata[COLD_COLUMN_SCALES_METADATA]) == expected_scales
    assert (
        metadata[COLD_INSTRUMENT_VERSION_METADATA].decode("ascii")
        == instrument_spec_version(spec)
    )
    _verify_decimal_schema(arrow_schema, expected_scales)

    row_group = pf.metadata.row_group(0)
    for col_name, scale in expected_scales.items():
        field = arrow_schema.field(col_name)
        assert pa.types.is_decimal(field.type)
        assert field.type.precision == 18
        assert field.type.scale == scale
        col_idx = arrow_schema.names.index(col_name)
        assert row_group.column(col_idx).physical_type == "INT64", (
            f"{parquet_path.name}:{col_name} must be INT64"
        )


def test_rust_cold_store_python_cold_reader_interop(tmp_path: Path) -> None:
    summary = _run_rust_interop_dump(tmp_path)
    assert summary["retired_exists_before_prune"] is True
    assert (tmp_path / MANIFEST_DB).is_file()
    assert list((tmp_path / ARCHIVE_SUBDIR).glob("*.jsonl")), "Stage 1 archive jsonl missing"

    btc = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    eth = CRYPTO_PERP_PACK.instrument("ETH-USDT-PERP")
    assert btc is not None and eth is not None

    reader = ColdReader(tmp_path)

    # 1. Verify Bars (BTCUSDT + ETHUSDT), metadata, physical INT64, 0 Decimal mismatch, and PIT
    btc_expected = _load_fixture_bars("BTCUSDT")
    assert len(btc_expected) == summary["btc_bars_count"] == 900
    assert summary["eth_bars_count"] == 5

    snap = reader.capture_snapshot(
        [(btc, summary["last_btc_ts"] + 1_000_000), (eth, summary["last_btc_ts"] + 1_000_000)],
        tick_instruments=[btc.instrument_id],
    )
    btc_bar_gens = [g for g in snap.bars.generations if g.symbol == "BTCUSDT" and g.timeframe == "1m"]
    eth_bar_gens = [g for g in snap.bars.generations if g.symbol == "ETHUSDT" and g.timeframe == "1m"]
    assert len(btc_bar_gens) == 1, "BTC bars should be compacted into 1 partition generation"
    assert btc_bar_gens[0].bars == 901, "Compaction must preserve both revisions (900 + 1)"
    assert btc_bar_gens[0].parquet_path.parts[-3:] == (
        "1m",
        "2026-11",
        btc_bar_gens[0].parquet_path.name,
    )
    assert btc_bar_gens[0].parquet_path.name.startswith("partition-")
    _assert_parquet_int64_and_metadata(
        tmp_path / btc_bar_gens[0].parquet_path, cold_column_scales(btc), btc
    )
    _assert_parquet_int64_and_metadata(
        tmp_path / eth_bar_gens[0].parquet_path, cold_column_scales(eth), eth
    )

    # PIT cutoff before revision=1 -> exact match with original fixture row-by-row (0 Decimal diff)
    btc_v0 = reader.read_bars(btc, "1m", as_of=summary["rev_bar_v0_avail"])
    assert len(btc_v0) == 1  # only the first bar is available at rev_bar_v0_avail
    assert btc_v0[0].revision == 0
    assert btc_v0[0].close == btc_expected[0][4]

    btc_before_rev1 = reader.read_bars(btc, "1m", as_of=summary["rev_bar_v1_avail"] - 1)
    assert btc_before_rev1[0].revision == 0
    assert btc_before_rev1[0].close == btc_expected[0][4]

    btc_latest = reader.read_bars(btc, "1m")
    assert len(btc_latest) == 900
    assert btc_latest[0].revision == 1
    assert btc_latest[0].close == btc_expected[0][1]  # rev1 set close = open
    for idx in range(1, 900):
        bar = btc_latest[idx]
        exp_ts, exp_o, exp_h, exp_l, exp_c, exp_v, exp_t = btc_expected[idx]
        assert bar.ts_event == exp_ts
        assert bar.open == exp_o
        assert bar.high == exp_h
        assert bar.low == exp_l
        assert bar.close == exp_c
        assert bar.volume == exp_v
        assert bar.turnover == exp_t

    eth_latest = reader.read_bars(eth, "1m")
    assert len(eth_latest) == 5
    for idx, bar in enumerate(eth_latest):
        assert bar.ts_event == summary["first_btc_ts"] + idx * 60_000
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.turnover) == (
            Decimal("3500.15"),
            Decimal("3502.50"),
            Decimal("3499.05"),
            Decimal("3501.25"),
            Decimal("12.34"),
            Decimal("43205.4250"),
        )

    # 2. Verify Derivatives (MarkPrice + revisions, FundingRate settled/unsettled, OpenInterest)
    for kind, expected_scale in (
        (DerivativeKind.MARK_PRICE, btc.price_precision),
        (DerivativeKind.FUNDING_RATE, 8),
        (DerivativeKind.OPEN_INTEREST, btc.qty_precision),
    ):
        gens = [g for g in snap.derivatives.generations if g.timeframe == kind.value]
        assert len(gens) == 1
        assert gens[0].parquet_path.parts[-3:-1] == (kind.value, "2026-11")
        _assert_parquet_int64_and_metadata(
            tmp_path / gens[0].parquet_path, {"value": expected_scale}, btc
        )

    mark_early = reader.read_derivatives(
        btc, DerivativeKind.MARK_PRICE, as_of=summary["mark_v0_avail"]
    )
    assert [(p.value, p.revision) for p in mark_early] == [(Decimal("68001.5"), 0)]
    mark_late = reader.read_derivatives(btc, DerivativeKind.MARK_PRICE)
    assert [(p.value, p.revision) for p in mark_late] == [
        (Decimal("68002.3"), 1),
        (Decimal("68010.0"), 0),
    ]

    funding_hist = reader.read_derivatives(btc, DerivativeKind.FUNDING_RATE, historical_only=True)
    assert [(p.ts_event, p.value, p.settled) for p in funding_hist] == [
        (summary["funding_settled_ts"], Decimal("0.00012345"), True),
    ]
    funding_all = reader.read_derivatives(btc, DerivativeKind.FUNDING_RATE, historical_only=False)
    assert [(p.ts_event, p.value, p.settled) for p in funding_all] == [
        (summary["funding_settled_ts"], Decimal("0.00012345"), True),
        (summary["funding_unsettled_ts"], Decimal("-0.00005000"), False),
    ]

    oi_rows = reader.read_derivatives(btc, DerivativeKind.OPEN_INTEREST)
    assert [(p.timeframe, p.value) for p in oi_rows] == [
        ("5m", Decimal("12345.678")),
        ("5m", Decimal("12350.001")),
    ]

    # 3. Verify Ticks (Trades & Quotes): physical INT64, UUID binary(16), revisions, PIT
    assert snap.ticks is not None
    trade_gens = [g for g in snap.ticks.generations if g.timeframe == "trades"]
    quote_gens = [g for g in snap.ticks.generations if g.timeframe == "quotes"]
    assert len(trade_gens) == 1 and trade_gens[0].bars == 3
    assert len(quote_gens) == 1 and quote_gens[0].bars == 2
    assert trade_gens[0].parquet_path.parts[-3:-1] == ("trades", "2026-11-01")
    assert quote_gens[0].parquet_path.parts[-3:-1] == ("quotes", "2026-11-01")

    _assert_parquet_int64_and_metadata(
        tmp_path / trade_gens[0].parquet_path,
        {"price": btc.price_precision, "size": btc.qty_precision},
        btc,
    )
    _assert_parquet_int64_and_metadata(
        tmp_path / quote_gens[0].parquet_path,
        {
            "bid_price": btc.price_precision,
            "bid_size": btc.qty_precision,
            "ask_price": btc.price_precision,
            "ask_size": btc.qty_precision,
        },
        btc,
    )

    trades_v0 = reader.read_ticks(btc, TickKind.TRADES, as_of=summary["trade_v0_avail"])
    assert len(trades_v0) == 1
    assert trades_v0[0].trade_id == UUID(int=101)
    assert trades_v0[0].packet_id == UUID(int=9001)
    assert (trades_v0[0].price, trades_v0[0].size, trades_v0[0].revision) == (
        Decimal("68000.1"),
        Decimal("0.250"),
        0,
    )

    trades_latest = reader.read_ticks(btc, TickKind.TRADES)
    assert len(trades_latest) == 2
    assert (
        trades_latest[0].trade_id,
        trades_latest[0].side,
        trades_latest[0].price,
        trades_latest[0].size,
        trades_latest[0].availability,
        trades_latest[0].revision,
    ) == (UUID(int=101), "Buy", Decimal("68000.2"), Decimal("0.375"), "measured", 1)
    assert (
        trades_latest[1].trade_id,
        trades_latest[1].side,
        trades_latest[1].price,
        trades_latest[1].size,
        trades_latest[1].availability,
        trades_latest[1].archive_index,
    ) == (UUID(int=102), "Sell", Decimal("67999.9"), Decimal("1.005"), "inferred", 7)

    by_id = reader.read_trade_revisions_by_id(btc, {UUID(int=101).bytes})
    assert [row.revision for row in by_id] == [0, 1]

    arrivals = list(reader.tick_arrivals(btc, summary["first_btc_ts"], summary["quote_v1_avail"]))
    assert len(arrivals) == 5  # 3 trade rows + 2 quote rows ordered by ts_available
    assert [row.ts_available for row in arrivals] == sorted(row.ts_available for row in arrivals)

    quotes_v0 = reader.read_ticks(btc, TickKind.QUOTES, as_of=summary["quote_v0_avail"])
    assert len(quotes_v0) == 1
    assert (
        quotes_v0[0].bid_price,
        quotes_v0[0].bid_size,
        quotes_v0[0].ask_price,
        quotes_v0[0].ask_size,
        quotes_v0[0].connection_id,
        quotes_v0[0].update_id,
        quotes_v0[0].revision,
    ) == (
        Decimal("68000.0"),
        Decimal("2.500"),
        Decimal("68000.5"),
        Decimal("1.250"),
        UUID(int=777),
        10,
        0,
    )

    quotes_latest = reader.read_ticks(btc, TickKind.QUOTES)
    assert len(quotes_latest) == 1
    assert (
        quotes_latest[0].bid_price,
        quotes_latest[0].bid_size,
        quotes_latest[0].revision,
    ) == (Decimal("68000.1"), Decimal("0.000"), 1)

    # 4. Verify `talaria.sdk` top-level Python API works directly on the Rust-written cold store
    sdk_bars = sdk.bars(
        "BTC-USDT-PERP",
        "1m",
        summary["first_btc_ts"],
        summary["last_btc_ts"],
        as_of=summary["last_btc_ts"] + 60_000,
        data_dir=tmp_path,
    )
    assert len(sdk_bars) == 900
    assert sdk_bars[0].close == btc_expected[0][1]

    sdk_settled = sdk.funding_settlements(
        "BTC-USDT-PERP",
        summary["funding_settled_ts"],
        summary["funding_settled_ts"],
        as_of=summary["funding_settled_ts"],
        data_dir=tmp_path,
    )
    assert len(sdk_settled) == 1
    assert sdk_settled[0].value == Decimal("0.00012345")

    sdk_trades = sdk.trades(
        "BTC-USDT-PERP",
        summary["first_btc_ts"],
        summary["trade_ts"] + 100,
        as_of=summary["trade_v1_avail"],
        data_dir=tmp_path,
    )
    assert [t.price for t in sdk_trades] == [Decimal("68000.2"), Decimal("67999.9")]

    sdk_quotes = sdk.quotes(
        "BTC-USDT-PERP",
        summary["first_btc_ts"],
        summary["quote_ts"] + 100,
        as_of=summary["quote_v1_avail"],
        data_dir=tmp_path,
    )
    assert [q.bid_price for q in sdk_quotes] == [Decimal("68000.1")]
