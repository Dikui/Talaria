//! Interop fixture emitter for `tests/test_rust_cold_store_interop.py`.
//!
//! Writes Bars (`BTC-USDT-PERP` from Bybit fixture + `1000BONK-USDT-PERP` + revisions),
//! Derivatives (`mark_price`, `funding_rate`, `open_interest`), and Ticks (`trades`, `quotes`)
//! using `talaria-store`'s 4-stage atomic publish + compaction, then prints a JSON manifest
//! describing expected values for Python `ColdReader` verification.

use std::env;
use std::fs;
use std::path::{Path, PathBuf};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::{
    DerivativeKind, DerivativePoint, InstrumentSpec, PrecisionTier, Quote, RawBar, Trade,
    TradeAvailability, TradeSide,
};
use talaria_store::{ColdStore, RETENTION_MS};

#[derive(Deserialize)]
struct BybitResp {
    result: BybitResult,
}

#[derive(Deserialize)]
struct BybitResult {
    list: Vec<[String; 7]>,
}

#[derive(Serialize)]
struct InteropSummary {
    out_dir: String,
    btc_bars_count: usize,
    eth_bars_count: usize,
    mark_price_count: usize,
    funding_rate_count: usize,
    open_interest_count: usize,
    trades_count: usize,
    quotes_count: usize,
    first_btc_ts: i64,
    last_btc_ts: i64,
    rev_bar_ts: i64,
    rev_bar_v0_avail: i64,
    rev_bar_v1_avail: i64,
    mark_rev_ts: i64,
    mark_v0_avail: i64,
    mark_v1_avail: i64,
    funding_settled_ts: i64,
    funding_unsettled_ts: i64,
    trade_ts: i64,
    trade_v0_avail: i64,
    trade_v1_avail: i64,
    quote_ts: i64,
    quote_v0_avail: i64,
    quote_v1_avail: i64,
    retired_exists_before_prune: bool,
    retired_pruned_after_retention: bool,
}

fn btc_spec() -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "BTCUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::new(1, 1),
        qty_step: Decimal::new(1, 3),
        min_qty: Decimal::new(1, 3),
        price_precision: 1,
        qty_precision: 3,
    }
}

fn eth_spec() -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: "ETH-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "ETHUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::new(1, 2),
        qty_step: Decimal::new(1, 2),
        min_qty: Decimal::new(1, 2),
        price_precision: 2,
        qty_precision: 2,
    }
}

fn dec(s: &str) -> Decimal {
    s.parse().expect("valid decimal literal")
}

fn uuid_from_u128<U: std::str::FromStr>(v: u128) -> U
where
    <U as std::str::FromStr>::Err: std::fmt::Debug,
{
    hex::encode(v.to_be_bytes())
        .parse::<U>()
        .expect("valid uuid hex")
}

fn load_bybit_bars(fixture_path: &Path, symbol: &str, timeframe: &str) -> Vec<RawBar> {
    let raw = fs::read_to_string(fixture_path).expect("read bybit fixture");
    let parsed: BybitResp = serde_json::from_str(&raw).expect("parse bybit json");
    let mut rows: Vec<(i64, [String; 7])> = parsed
        .result
        .list
        .into_iter()
        .map(|item| {
            let ts: i64 = item[0].parse().expect("ts_event");
            (ts, item)
        })
        .collect();
    rows.sort_by_key(|(ts, _)| *ts);
    rows.into_iter()
        .map(|(ts, item)| {
            let avail = ts + 60_000;
            RawBar {
                symbol: symbol.to_owned(),
                timeframe: timeframe.to_owned(),
                ts_event: ts,
                open: dec(&item[1]),
                high: dec(&item[2]),
                low: dec(&item[3]),
                close: dec(&item[4]),
                volume: dec(&item[5]),
                turnover: dec(&item[6]),
                ts_available: avail,
                ts_ingested: avail + 5,
                ts_committed: avail + 10,
                precision: PrecisionTier::SecondVerified,
                revision: 0,
                confirmed: true,
                history_only: false,
                synthetic: false,
            }
        })
        .collect()
}

fn main() {
    let args: Vec<String> = env::args().collect();
    let mut out_dir: Option<PathBuf> = None;
    let mut fixture_dir: Option<PathBuf> = None;
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--out-dir" => {
                i += 1;
                out_dir = args.get(i).map(PathBuf::from);
            }
            "--fixture-dir" => {
                i += 1;
                fixture_dir = args.get(i).map(PathBuf::from);
            }
            other if !other.starts_with('-') && out_dir.is_none() => {
                out_dir = Some(PathBuf::from(other));
            }
            other => panic!("unexpected argument: {other}"),
        }
        i += 1;
    }

    let out_dir = out_dir.expect("usage: interop_dump --out-dir <DIR> [--fixture-dir <DIR>]");
    let fixture_dir = fixture_dir.unwrap_or_else(|| {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../../tests/fixtures/bybit")
    });

    let btc = btc_spec();
    let eth = eth_spec();

    let mut btc_bars = load_bybit_bars(&fixture_dir.join("BTCUSDT/1m.json"), "BTCUSDT", "1m");
    let first_btc_ts = btc_bars.first().unwrap().ts_event;
    let last_btc_ts = btc_bars.last().unwrap().ts_event;

    // Split BTC bars into two tail batches + add a revision=1 bar for the first timestamp.
    let split_idx = btc_bars.len() / 2;
    let tail1 = btc_bars[..split_idx].to_vec();
    let tail2 = btc_bars[split_idx..].to_vec();
    let rev_bar_ts = first_btc_ts;
    let rev_bar_v0_avail = btc_bars[0].ts_available;
    let rev_bar_v1_avail = rev_bar_v0_avail + 300_000;
    let mut rev_bar = btc_bars[0].clone();
    rev_bar.close = rev_bar.open;
    rev_bar.high = rev_bar.high.max(rev_bar.close);
    rev_bar.low = rev_bar.low.min(rev_bar.close);
    rev_bar.revision = 1;
    rev_bar.ts_available = rev_bar_v1_avail;
    rev_bar.ts_ingested = rev_bar_v1_avail + 2;
    rev_bar.ts_committed = rev_bar_v1_avail + 5;

    // ETH bars exercising price_precision=2, qty_precision=2, turnover_scale=4.
    let eth_bars: Vec<RawBar> = (0..5)
        .map(|idx| {
            let ts = first_btc_ts + idx * 60_000;
            let avail = ts + 60_000;
            RawBar {
                symbol: "ETHUSDT".to_owned(),
                timeframe: "1m".to_owned(),
                ts_event: ts,
                open: dec("3500.15"),
                high: dec("3502.50"),
                low: dec("3499.05"),
                close: dec("3501.25"),
                volume: dec("12.34"),
                turnover: dec("43205.4250"),
                ts_available: avail,
                ts_ingested: avail + 5,
                ts_committed: avail + 10,
                precision: PrecisionTier::SecondVerified,
                revision: 0,
                confirmed: true,
                history_only: false,
                synthetic: false,
            }
        })
        .collect();

    let store = ColdStore::open(&out_dir).expect("open ColdStore");
    let g_tail1 = store.publish_bars(&btc, "1m", &tail1).expect("publish tail1");
    store.publish_bars(&btc, "1m", &tail2).expect("publish tail2");
    store
        .publish_bars(&btc, "1m", std::slice::from_ref(&rev_bar))
        .expect("publish rev1");
    store
        .publish_bars(&eth, "1m", &eth_bars)
        .expect("publish eth");

    // Derivatives: MarkPrice (with revision 0 and 1), FundingRate (settled & unsettled), OpenInterest.
    let mark_rev_ts = first_btc_ts;
    let mark_v0_avail = mark_rev_ts + 60_000;
    let mark_v1_avail = mark_rev_ts + 120_000;
    let mark_v0 = DerivativePoint {
        instrument_id: btc.instrument_id.clone(),
        kind: DerivativeKind::MarkPrice,
        timeframe: Some("1m".to_owned()),
        value: dec("68001.5"),
        ts_event: mark_rev_ts,
        ts_published: None,
        ts_available: mark_v0_avail,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: mark_v0_avail + 1,
        ts_committed: mark_v0_avail + 2,
        revision: 0,
        settled: None,
    };
    let mark_v1 = DerivativePoint {
        value: dec("68002.3"),
        ts_available: mark_v1_avail,
        ts_ingested: mark_v1_avail + 1,
        ts_committed: mark_v1_avail + 2,
        revision: 1,
        ..mark_v0.clone()
    };
    let mark_next = DerivativePoint {
        ts_event: mark_rev_ts + 60_000,
        value: dec("68010.0"),
        ts_available: mark_rev_ts + 120_000,
        ts_ingested: mark_rev_ts + 120_001,
        ts_committed: mark_rev_ts + 120_002,
        ..mark_v0.clone()
    };
    store
        .publish_derivatives(&btc, DerivativeKind::MarkPrice, std::slice::from_ref(&mark_v0))
        .expect("publish mark v0");
    store
        .publish_derivatives(&btc, DerivativeKind::MarkPrice, &[mark_v1.clone(), mark_next])
        .expect("publish mark v1+next");

    let funding_settled_ts = first_btc_ts;
    let funding_unsettled_ts = first_btc_ts + 60_000;
    let funding_rows = vec![
        DerivativePoint {
            instrument_id: btc.instrument_id.clone(),
            kind: DerivativeKind::FundingRate,
            timeframe: None,
            value: dec("0.00012345"),
            ts_event: funding_settled_ts,
            ts_published: Some(funding_settled_ts),
            ts_available: funding_settled_ts,
            precision: PrecisionTier::SecondVerified,
            ts_ingested: funding_settled_ts + 10,
            ts_committed: funding_settled_ts + 20,
            revision: 0,
            settled: Some(true),
        },
        DerivativePoint {
            instrument_id: btc.instrument_id.clone(),
            kind: DerivativeKind::FundingRate,
            timeframe: None,
            value: dec("-0.00005000"),
            ts_event: funding_unsettled_ts,
            ts_published: None,
            ts_available: funding_unsettled_ts + 500,
            precision: PrecisionTier::SecondVerified,
            ts_ingested: funding_unsettled_ts + 500,
            ts_committed: funding_unsettled_ts + 510,
            revision: 0,
            settled: Some(false),
        },
    ];
    store
        .publish_derivatives(&btc, DerivativeKind::FundingRate, &funding_rows)
        .expect("publish funding");

    let oi_rows = vec![
        DerivativePoint {
            instrument_id: btc.instrument_id.clone(),
            kind: DerivativeKind::OpenInterest,
            timeframe: Some("5m".to_owned()),
            value: dec("12345.678"),
            ts_event: first_btc_ts,
            ts_published: None,
            ts_available: first_btc_ts + 300_000,
            precision: PrecisionTier::SecondVerified,
            ts_ingested: first_btc_ts + 300_001,
            ts_committed: first_btc_ts + 300_002,
            revision: 0,
            settled: None,
        },
        DerivativePoint {
            instrument_id: btc.instrument_id.clone(),
            kind: DerivativeKind::OpenInterest,
            timeframe: Some("5m".to_owned()),
            value: dec("12350.001"),
            ts_event: first_btc_ts + 300_000,
            ts_published: None,
            ts_available: first_btc_ts + 600_000,
            precision: PrecisionTier::SecondVerified,
            ts_ingested: first_btc_ts + 600_001,
            ts_committed: first_btc_ts + 600_002,
            revision: 0,
            settled: None,
        },
    ];
    store
        .publish_derivatives(&btc, DerivativeKind::OpenInterest, &oi_rows)
        .expect("publish oi");

    // Ticks: Trades & Quotes on 2026-11-01 (compacted at 2026-11-02+ so they merge into partition!)
    let trade_ts = first_btc_ts + 1_000;
    let trade_v0_avail = trade_ts + 10;
    let trade_v1_avail = trade_ts + 500;
    let trade1_id: u128 = 101;
    let trade2_id: u128 = 102;
    let packet1: u128 = 9001;

    let trade1_v0 = Trade {
        instrument_id: btc.instrument_id.clone(),
        ts_event: trade_ts,
        ts_available: trade_v0_avail,
        ts_ingested: trade_v0_avail,
        ts_committed: trade_v0_avail + 5,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: uuid_from_u128(trade1_id),
        side: TradeSide::Buy,
        price: dec("68000.1"),
        size: dec("0.250"),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: Some(1),
        packet_id: Some(uuid_from_u128(packet1)),
        packet_index: Some(0),
    };
    let trade2 = Trade {
        instrument_id: btc.instrument_id.clone(),
        ts_event: trade_ts + 5,
        ts_available: trade_v0_avail + 5,
        ts_ingested: trade_v0_avail + 5,
        ts_committed: trade_v0_avail + 10,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: uuid_from_u128(trade2_id),
        side: TradeSide::Sell,
        price: dec("67999.9"),
        size: dec("1.005"),
        availability: TradeAvailability::Inferred,
        archive_day: Some(first_btc_ts),
        archive_index: Some(7),
        sequence: None,
        packet_id: None,
        packet_index: None,
    };
    let trade1_v1 = Trade {
        price: dec("68000.2"),
        size: dec("0.375"),
        ts_available: trade_v1_avail,
        ts_ingested: trade_v1_avail,
        ts_committed: trade_v1_avail + 5,
        revision: 1,
        sequence: Some(2),
        ..trade1_v0.clone()
    };
    store
        .publish_trades(&btc, &[trade1_v0, trade2])
        .expect("publish trades batch 1");
    store
        .publish_trades(&btc, &[trade1_v1])
        .expect("publish trades batch 2");

    let quote_ts = first_btc_ts + 2_000;
    let quote_v0_avail = quote_ts + 10;
    let quote_v1_avail = quote_ts + 400;
    let conn1: u128 = 777;
    let quote_v0 = Quote {
        instrument_id: btc.instrument_id.clone(),
        ts_event: quote_ts,
        ts_available: quote_v0_avail,
        ts_ingested: quote_v0_avail,
        ts_committed: quote_v0_avail + 2,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        bid_price: dec("68000.0"),
        bid_size: dec("2.500"),
        ask_price: dec("68000.5"),
        ask_size: dec("1.250"),
        connection_id: Some(uuid_from_u128(conn1)),
        update_id: Some(10),
    };
    let quote_v1 = Quote {
        bid_price: dec("68000.1"),
        bid_size: dec("0.000"),
        ts_available: quote_v1_avail,
        ts_ingested: quote_v1_avail,
        ts_committed: quote_v1_avail + 2,
        revision: 1,
        ..quote_v0.clone()
    };
    store
        .publish_quotes(&btc, &[quote_v0])
        .expect("publish quotes batch 1");
    store
        .publish_quotes(&btc, &[quote_v1])
        .expect("publish quotes batch 2");

    // Compact everything (on next day so tick day 2026-11-01 also compacts!).
    let compact_ts = first_btc_ts + 86_400_000;
    store
        .compact(&[btc.clone(), eth.clone()], Some(compact_ts))
        .expect("compact");

    // Verify retired tail file stays retained immediately after compaction (< 24h & < 3 versions).
    let retired_abs = out_dir.join(&g_tail1.parquet_path);
    let retired_exists_before_prune = retired_abs.is_file();

    let _ = RETENTION_MS;
    store.close().expect("close store");

    btc_bars.push(rev_bar);
    let summary = InteropSummary {
        out_dir: out_dir.display().to_string(),
        btc_bars_count: btc_bars.len() - 1, // unique timestamps (900)
        eth_bars_count: eth_bars.len(),
        mark_price_count: 2,
        funding_rate_count: 2,
        open_interest_count: 2,
        trades_count: 2,
        quotes_count: 1,
        first_btc_ts,
        last_btc_ts,
        rev_bar_ts,
        rev_bar_v0_avail,
        rev_bar_v1_avail,
        mark_rev_ts,
        mark_v0_avail,
        mark_v1_avail,
        funding_settled_ts,
        funding_unsettled_ts,
        trade_ts,
        trade_v0_avail,
        trade_v1_avail,
        quote_ts,
        quote_v0_avail,
        quote_v1_avail,
        retired_exists_before_prune,
        retired_pruned_after_retention: false,
    };
    println!("{}", serde_json::to_string(&summary).unwrap());
}
