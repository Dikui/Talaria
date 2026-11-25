use std::collections::HashSet;
use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use chrono::{TimeZone, Utc};
use parquet::basic::Type as PhysicalType;
use parquet::file::reader::{FileReader, SerializedFileReader};
use rust_decimal::Decimal;
use rusqlite::Connection;
use talaria_contracts::{
    BarStatus, CompactBar, DerivativeKind, DerivativePoint, FeedEvent, InstrumentSpec, KlineEvent,
    PrecisionTier, Quote, RawBar, ReceivedKlineEvent, Trade, TradeAvailability, TradeSide,
};
use talaria_store::{
    ARCHIVE_SUBDIR, COLD_COLUMN_SCALES_METADATA, COLD_FORMAT_DECIMAL128, COLD_FORMAT_METADATA,
    COLD_INSTRUMENT_VERSION_METADATA, ColdPhase, ColdReader, ColdStore, DataDirLock,
    HotStreamBuffer, MANIFEST_DB, RETENTION_MS, STAGING_SUBDIR, StoreError, latest_visible_bars,
    partition_day, partition_month, range_visible_bars, revision_window, unpublished_revisions,
};
use tempfile::TempDir;

const MINUTE: i64 = 60_000;
const DAY: i64 = 86_400_000;

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

fn dec(s: &str) -> Decimal {
    s.parse().expect("valid decimal")
}

fn uuid_u128<U: std::str::FromStr>(v: u128) -> U
where
    <U as std::str::FromStr>::Err: std::fmt::Debug,
{
    hex::encode(v.to_be_bytes())
        .parse::<U>()
        .expect("valid uuid")
}

fn base_ts() -> i64 {
    // 2025-11-20T00:00:00Z
    1_763_596_800_000
}

fn make_bar(ts: i64, close: &str, rev: u64) -> RawBar {
    RawBar {
        symbol: "BTCUSDT".to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: ts,
        open: dec("100.0"),
        high: dec("101.0"),
        low: dec("99.0"),
        close: dec(close),
        volume: dec("1.000"),
        turnover: dec("100.5000"),
        ts_available: ts + MINUTE,
        ts_ingested: ts + MINUTE + 5,
        ts_committed: ts + MINUTE + 10,
        precision: PrecisionTier::SecondVerified,
        revision: rev,
        confirmed: true,
        history_only: false,
        synthetic: false,
    }
}

fn count_parquet_files(root: &Path) -> usize {
    if !root.exists() {
        return 0;
    }
    let mut count = 0;
    for entry in fs::read_dir(root).unwrap() {
        let p = entry.unwrap().path();
        if p.is_dir() {
            count += count_parquet_files(&p);
        } else if p.extension().and_then(|e| e.to_str()) == Some("parquet") {
            count += 1;
        }
    }
    count
}

#[test]
fn test_bars_decimal128_physical_int64_and_metadata() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let mut bar = make_bar(base_ts(), "100.5", 0);
    bar.volume = dec("12.345");
    bar.turnover = dec("809038.73");

    let store = ColdStore::open(tmp.path()).unwrap();
    let generation = store.publish_bars(&spec, "1m", &[bar.clone()]).unwrap();

    let pq_path = tmp.path().join(&generation.parquet_path);
    let file = fs::File::open(&pq_path).unwrap();
    let reader = SerializedFileReader::new(file).unwrap();
    let file_meta = reader.metadata().file_metadata();

    let kv: std::collections::HashMap<String, String> = file_meta
        .key_value_metadata()
        .unwrap()
        .iter()
        .filter_map(|kv| kv.value.as_ref().map(|v| (kv.key.clone(), v.clone())))
        .collect();
    assert_eq!(kv.get(COLD_FORMAT_METADATA).map(String::as_str), Some(COLD_FORMAT_DECIMAL128));
    assert_eq!(
        kv.get(COLD_INSTRUMENT_VERSION_METADATA).map(String::as_str),
        Some("61aeb14515aa")
    );
    let scales: serde_json::Value =
        serde_json::from_str(kv.get(COLD_COLUMN_SCALES_METADATA).unwrap()).unwrap();
    assert_eq!(
        scales,
        serde_json::json!({
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 3,
            "turnover": 4
        })
    );

    let descr = file_meta.schema_descr();
    for col in descr.columns() {
        if matches!(
            col.name(),
            "open" | "high" | "low" | "close" | "volume" | "turnover"
        ) {
            assert_eq!(
                col.physical_type(),
                PhysicalType::INT64,
                "column {} must be physical INT64",
                col.name()
            );
        }
    }

    let read_back = store.read_bars(&spec, "1m", None, None, None).unwrap();
    assert_eq!(read_back, vec![bar.clone()]);

    // Republishing read-back padded decimal bar must be idempotent.
    let mut padded = read_back;
    padded[0].turnover = dec("809038.7300");
    let second = store.publish_bars(&spec, "1m", &padded).unwrap();
    assert_eq!(second, generation);
    assert_eq!(store.generations(&spec, "1m").unwrap().len(), 1);
    store.close().unwrap();
}

#[test]
fn test_per_file_independent_scales_coexist() {
    let tmp = TempDir::new().unwrap();
    let coarse = btc_spec();
    let fine = InstrumentSpec {
        tick_size: Decimal::new(1, 2),
        qty_step: Decimal::new(1, 4),
        price_precision: 2,
        qty_precision: 4,
        ..coarse.clone()
    };
    let b1 = make_bar(base_ts(), "100.5", 0);
    let mut b2 = make_bar(base_ts() + MINUTE, "101.25", 0);
    b2.open = dec("101.25");
    b2.high = dec("102.50");
    b2.low = dec("100.75");
    b2.volume = dec("1.2345");
    b2.turnover = dec("124.9999");

    let store = ColdStore::open(tmp.path()).unwrap();
    store.publish_bars(&coarse, "1m", std::slice::from_ref(&b1)).unwrap();
    store.publish_bars(&fine, "1m", std::slice::from_ref(&b2)).unwrap();

    let rows = store.read_bars(&coarse, "1m", None, None, None).unwrap();
    assert_eq!(rows, vec![b1, b2]);
}

#[test]
fn test_quality_rejection_whole_batch_atomic() {
    let spec = btc_spec();
    let t0 = base_ts();
    let t1 = t0 + MINUTE;

    let cases: Vec<(&str, Vec<RawBar>)> = vec![
        ("empty", vec![]),
        ("scale-overflow-price", {
            let mut b = make_bar(t1, "100.55", 0);
            b.high = dec("101.0");
            vec![make_bar(t0, "100.5", 0), b]
        }),
        ("scale-overflow-turnover", {
            let mut b = make_bar(t1, "100.5", 0);
            b.turnover = dec("100.50001");
            vec![make_bar(t0, "100.5", 0), b]
        }),
        ("beyond-18-digits", {
            let mut b = make_bar(t1, "100.5", 0);
            b.turnover = dec("123456789012345678.9");
            vec![make_bar(t0, "100.5", 0), b]
        }),
        ("high-below-close", {
            let mut b = make_bar(t0, "100.5", 0);
            b.high = dec("100.2");
            vec![b]
        }),
        ("low-above-open", {
            let mut b = make_bar(t0, "100.5", 0);
            b.low = dec("100.4");
            vec![b]
        }),
        ("negative-volume", {
            let mut b = make_bar(t0, "100.5", 0);
            b.volume = dec("-1.0");
            vec![b]
        }),
        ("price-off-tick", {
            let coarse_tick = InstrumentSpec {
                tick_size: Decimal::new(5, 1), // 0.5 tick with precision 1
                ..spec.clone()
            };
            let _ = coarse_tick;
            // Use precision 1 with tick_size 0.5 tested separately below; here test qty_step.
            let mut b = make_bar(t0, "100.5", 0);
            b.volume = dec("1.0005");
            vec![b]
        }),
        ("duplicate-timestamp-revision", vec![make_bar(t0, "100.5", 0), make_bar(t0, "100.6", 0)]),
        ("non-monotonic-timestamp", vec![make_bar(t1, "100.5", 0), make_bar(t0, "100.5", 0)]),
        ("available-before-event", {
            let mut b = make_bar(t0, "100.5", 0);
            b.ts_available = b.ts_event - 1;
            vec![b]
        }),
        ("committed-before-ingested", {
            let mut b = make_bar(t0, "100.5", 0);
            b.ts_committed = b.ts_ingested - 1;
            vec![b]
        }),
    ];

    for (label, bars) in cases {
        let tmp = TempDir::new().unwrap();
        let store = ColdStore::open(tmp.path()).unwrap();
        let res = store.publish_bars(&spec, "1m", &bars);
        assert!(
            matches!(res, Err(StoreError::Quality(_))),
            "case {label} should return StoreError::Quality, got {res:?}"
        );
        assert!(store.generations(&spec, "1m").unwrap().is_empty(), "{label}");
        assert_eq!(count_parquet_files(&tmp.path().join("data/cold")), 0, "{label}");
    }

    // Explicit step alignment rejection (fits decimal128 scale, fails tick_size/qty_step).
    let step_spec = InstrumentSpec {
        tick_size: Decimal::new(5, 2), // 0.05
        qty_step: Decimal::new(2, 2),  // 0.02
        price_precision: 2,
        qty_precision: 2,
        ..spec
    };
    let tmp = TempDir::new().unwrap();
    let store = ColdStore::open(tmp.path()).unwrap();
    let mut bad_price = make_bar(t0, "100.01", 0);
    bad_price.volume = dec("1.00");
    assert!(matches!(
        store.publish_bars(&step_spec, "1m", &[bad_price]),
        Err(StoreError::Quality(_))
    ));
    let mut bad_qty = make_bar(t0, "100.05", 0);
    bad_qty.volume = dec("1.01");
    assert!(matches!(
        store.publish_bars(&step_spec, "1m", &[bad_qty]),
        Err(StoreError::Quality(_))
    ));
}

#[test]
fn test_cross_partition_rejection_all_datasets() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let store = ColdStore::open(tmp.path()).unwrap();

    let nov_end = Utc.with_ymd_and_hms(2025, 11, 30, 23, 59, 0).unwrap().timestamp_millis();
    let dec_start = Utc.with_ymd_and_hms(2025, 12, 1, 0, 0, 0).unwrap().timestamp_millis();
    assert_ne!(partition_month(nov_end).unwrap(), partition_month(dec_start).unwrap());

    // Bars cross-month
    assert!(matches!(
        store.publish_bars(
            &spec,
            "1m",
            &[make_bar(nov_end, "100.5", 0), make_bar(dec_start, "100.6", 0)]
        ),
        Err(StoreError::Quality(_))
    ));

    // Derivatives cross-month
    let d1 = DerivativePoint {
        instrument_id: spec.instrument_id.clone(),
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        value: dec("0.0001"),
        ts_event: nov_end,
        ts_published: Some(nov_end),
        ts_available: nov_end,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: nov_end,
        ts_committed: nov_end,
        revision: 0,
        settled: Some(true),
    };
    let d2 = DerivativePoint {
        ts_event: dec_start,
        ts_published: Some(dec_start),
        ts_available: dec_start,
        ts_ingested: dec_start,
        ts_committed: dec_start,
        ..d1.clone()
    };
    assert!(matches!(
        store.publish_derivatives(&spec, DerivativeKind::FundingRate, &[d1, d2]),
        Err(StoreError::Quality(_))
    ));

    // Trades & Quotes cross-UTC-day
    let day1 = Utc.with_ymd_and_hms(2025, 11, 20, 23, 59, 59).unwrap().timestamp_millis();
    let day2 = Utc.with_ymd_and_hms(2025, 11, 21, 0, 0, 0).unwrap().timestamp_millis();
    assert_ne!(partition_day(day1).unwrap(), partition_day(day2).unwrap());

    let t1 = Trade {
        instrument_id: spec.instrument_id.clone(),
        ts_event: day1,
        ts_available: day1,
        ts_ingested: day1,
        ts_committed: day1,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: uuid_u128(1),
        side: TradeSide::Buy,
        price: dec("100.1"),
        size: dec("0.100"),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: Some(1),
        packet_id: None,
        packet_index: None,
    };
    let t2 = Trade {
        ts_event: day2,
        ts_available: day2,
        ts_ingested: day2,
        ts_committed: day2,
        trade_id: uuid_u128(2),
        ..t1.clone()
    };
    assert!(matches!(
        store.publish_trades(&spec, &[t1, t2]),
        Err(StoreError::Quality(_))
    ));

    let q1 = Quote {
        instrument_id: spec.instrument_id.clone(),
        ts_event: day1,
        ts_available: day1,
        ts_ingested: day1,
        ts_committed: day1,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        bid_price: dec("100.0"),
        bid_size: dec("1.0"),
        ask_price: dec("100.1"),
        ask_size: dec("1.0"),
        connection_id: None,
        update_id: None,
    };
    let q2 = Quote {
        ts_event: day2,
        ts_available: day2,
        ts_ingested: day2,
        ts_committed: day2,
        ..q1.clone()
    };
    assert!(matches!(
        store.publish_quotes(&spec, &[q1, q2]),
        Err(StoreError::Quality(_))
    ));
}

#[test]
fn test_all_three_datasets_roundtrip_and_pit() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    // 1. Derivatives: FundingRate (settled & unsettled + scale=8), MarkPrice, OpenInterest
    let settled = DerivativePoint {
        instrument_id: spec.instrument_id.clone(),
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        value: dec("0.00005"),
        ts_event: t0,
        ts_published: Some(t0),
        ts_available: t0,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: t0 + 1,
        ts_committed: t0 + 2,
        revision: 0,
        settled: Some(true),
    };
    let unsettled = DerivativePoint {
        value: dec("0.00003"),
        ts_event: t0 + 1_000,
        ts_published: None,
        ts_available: t0 + 1_000,
        ts_ingested: t0 + 1_000,
        ts_committed: t0 + 1_000,
        settled: Some(false),
        ..settled.clone()
    };
    let g_fr = store
        .publish_derivatives(&spec, DerivativeKind::FundingRate, &[settled.clone(), unsettled.clone()])
        .unwrap();
    assert!(
        g_fr.parquet_path
            .to_string_lossy()
            .contains("bybit/BTCUSDT/funding_rate/2025-11/tail-61aeb14515aa-")
    );
    assert_eq!(
        store
            .read_derivatives(&spec, DerivativeKind::FundingRate, None, None, None, true)
            .unwrap(),
        vec![settled.clone()]
    );
    assert_eq!(
        store
            .read_derivatives(&spec, DerivativeKind::FundingRate, None, None, None, false)
            .unwrap(),
        vec![settled, unsettled]
    );
    // Funding rate > 8 decimals rejected
    let bad_fr = DerivativePoint {
        instrument_id: spec.instrument_id.clone(),
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        value: dec("0.000000001"),
        ts_event: t0,
        ts_published: Some(t0),
        ts_available: t0,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: t0,
        ts_committed: t0,
        revision: 0,
        settled: Some(true),
    };
    assert!(matches!(
        store.publish_derivatives(&spec, DerivativeKind::FundingRate, &[bad_fr]),
        Err(StoreError::Quality(_))
    ));

    // 2. Ticks: Trades (measured overrides inferred + read_trade_revisions_by_id + order_uncertain)
    let trade_m = Trade {
        instrument_id: spec.instrument_id.clone(),
        ts_event: t0,
        ts_available: t0 + 50,
        ts_ingested: t0 + 50,
        ts_committed: t0 + 60,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: uuid_u128(10),
        side: TradeSide::Buy,
        price: dec("100.1"),
        size: dec("0.250"),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: None, // missing sequence -> intra-ms tie with id2 becomes order_uncertain
        packet_id: None,
        packet_index: None,
    };
    let trade_inf = Trade {
        availability: TradeAvailability::Inferred,
        price: dec("100.0"),
        ts_available: t0,
        archive_day: Some(t0),
        archive_index: Some(0),
        ..trade_m.clone()
    };
    let trade_other = Trade {
        trade_id: uuid_u128(11),
        side: TradeSide::Sell,
        price: dec("100.2"),
        ..trade_m.clone()
    };
    let g_tr = store
        .publish_trades(&spec, &[trade_inf, trade_m.clone(), trade_other.clone()])
        .unwrap();
    assert!(
        g_tr.parquet_path
            .to_string_lossy()
            .contains("bybit/BTCUSDT/trades/2025-11-20/tail-61aeb14515aa-")
    );
    let visible_trades = store.read_trades(&spec, None, None, None).unwrap();
    assert_eq!(visible_trades.len(), 2);
    assert!(visible_trades.iter().all(|t| t.order_uncertain));
    assert_eq!(visible_trades[0].price, dec("100.1"));

    let mut id_filter = HashSet::new();
    id_filter.insert(*trade_m.trade_id.as_bytes());
    let by_id = ColdReader::new(tmp.path())
        .read_trade_revisions_by_id(&spec, &id_filter)
        .unwrap();
    assert_eq!(by_id.len(), 2); // inferred + measured both persisted

    // 3. Quotes (bid_size = 0 allowed, bid_price > ask_price rejected)
    let q = Quote {
        instrument_id: spec.instrument_id.clone(),
        ts_event: t0,
        ts_available: t0,
        ts_ingested: t0,
        ts_committed: t0,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        bid_price: dec("100.0"),
        bid_size: dec("0.000"),
        ask_price: dec("100.1"),
        ask_size: dec("1.500"),
        connection_id: Some(uuid_u128(99)),
        update_id: Some(42),
    };
    let g_qt = store.publish_quotes(&spec, std::slice::from_ref(&q)).unwrap();
    assert!(
        g_qt.parquet_path
            .to_string_lossy()
            .contains("bybit/BTCUSDT/quotes/2025-11-20/tail-61aeb14515aa-")
    );
    assert_eq!(store.read_quotes(&spec, None, None, None).unwrap(), vec![q.clone()]);

    let bad_q = Quote {
        bid_price: dec("100.2"),
        ask_price: dec("100.1"),
        ..q
    };
    assert!(matches!(
        store.publish_quotes(&spec, &[bad_q]),
        Err(StoreError::Quality(_))
    ));
}

#[test]
fn test_4_stage_fault_injection_and_recovery() {
    let phases = [
        ColdPhase::ArchiveBefore,
        ColdPhase::ArchiveAfter,
        ColdPhase::StagingBefore,
        ColdPhase::StagingAfter,
        ColdPhase::QcBefore,
        ColdPhase::QcAfter,
        ColdPhase::RenameBefore,
        ColdPhase::RenameAfter,
        ColdPhase::ManifestBefore,
        ColdPhase::ManifestAfter,
    ];
    let spec = btc_spec();
    let bars = vec![make_bar(base_ts(), "100.5", 0), make_bar(base_ts() + MINUTE, "100.6", 0)];

    for phase in phases {
        let tmp = TempDir::new().unwrap();
        let hook = Arc::new(move |p: ColdPhase| -> talaria_store::Result<()> {
            if p == phase {
                Err(StoreError::Store(format!("injected fault at {p:?}")))
            } else {
                Ok(())
            }
        });
        let store = ColdStore::open_with_fault_hook(tmp.path(), Some(hook)).unwrap();
        let res = store.publish_bars(&spec, "1m", &bars);
        assert!(res.is_err(), "expected fault at {phase:?}");

        let reader = ColdReader::new(tmp.path());
        if phase == ColdPhase::ManifestAfter {
            assert_eq!(reader.generations(&spec, "1m").unwrap().len(), 1);
            assert_eq!(reader.read_bars(&spec, "1m", None, None, None).unwrap(), bars);
        } else {
            assert_eq!(reader.generations(&spec, "1m").unwrap().len(), 0);
            assert!(reader.read_bars(&spec, "1m", None, None, None).unwrap().is_empty());
        }

        // Clear fault hook and retry: must cleanly recover and become idempotent.
        store.set_fault_hook(None);
        let gen_after = store.publish_bars(&spec, "1m", &bars).unwrap();
        assert_eq!(gen_after.bars, 2);
        assert_eq!(reader.generations(&spec, "1m").unwrap().len(), 1);
        assert_eq!(reader.read_bars(&spec, "1m", None, None, None).unwrap(), bars);
    }
}

#[test]
fn test_compaction_preserves_all_revisions_across_datasets() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    // Bars: 2 separate tail files with rev=0 and rev=1 for same ts_event
    let b0 = make_bar(t0, "100.5", 0);
    let mut b1 = make_bar(t0, "101.0", 1);
    b1.ts_available = t0 + 2 * MINUTE;
    b1.ts_ingested = t0 + 2 * MINUTE + 5;
    b1.ts_committed = t0 + 2 * MINUTE + 10;
    store.publish_bars(&spec, "1m", std::slice::from_ref(&b0)).unwrap();
    store.publish_bars(&spec, "1m", std::slice::from_ref(&b1)).unwrap();

    // Derivatives: 2 tail files with rev=0 and rev=1
    let d0 = DerivativePoint {
        instrument_id: spec.instrument_id.clone(),
        kind: DerivativeKind::MarkPrice,
        timeframe: Some("1m".to_owned()),
        value: dec("100.5"),
        ts_event: t0,
        ts_published: None,
        ts_available: t0 + MINUTE,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: t0 + MINUTE,
        ts_committed: t0 + MINUTE,
        revision: 0,
        settled: None,
    };
    let d1 = DerivativePoint {
        value: dec("101.2"),
        ts_available: t0 + 2 * MINUTE,
        ts_ingested: t0 + 2 * MINUTE,
        ts_committed: t0 + 2 * MINUTE,
        revision: 1,
        ..d0.clone()
    };
    store.publish_derivatives(&spec, DerivativeKind::MarkPrice, std::slice::from_ref(&d0)).unwrap();
    store.publish_derivatives(&spec, DerivativeKind::MarkPrice, std::slice::from_ref(&d1)).unwrap();

    // Trades: 2 tail files on day 2025-11-20 with rev=0 and rev=1
    let tr0 = Trade {
        instrument_id: spec.instrument_id.clone(),
        ts_event: t0,
        ts_available: t0 + 10,
        ts_ingested: t0 + 10,
        ts_committed: t0 + 10,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: uuid_u128(555),
        side: TradeSide::Buy,
        price: dec("100.1"),
        size: dec("0.500"),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: Some(1),
        packet_id: None,
        packet_index: None,
    };
    let tr1 = Trade {
        price: dec("100.2"),
        ts_available: t0 + 100,
        ts_ingested: t0 + 100,
        ts_committed: t0 + 100,
        revision: 1,
        ..tr0.clone()
    };
    store.publish_trades(&spec, std::slice::from_ref(&tr0)).unwrap();
    store.publish_trades(&spec, std::slice::from_ref(&tr1)).unwrap();

    // Compact on next day (so ticks on 2025-11-20 also compact)
    let report = store.compact(std::slice::from_ref(&spec), Some(t0 + DAY)).unwrap();
    assert_eq!(report.compacted_groups, 3);
    assert_eq!(report.retired_files_added, 6);

    let bar_gens = store.generations(&spec, "1m").unwrap();
    assert_eq!(bar_gens.len(), 1);
    assert_eq!(bar_gens[0].bars, 2);
    assert!(bar_gens[0].parquet_path.to_string_lossy().contains("partition-"));

    let mark_gens = store.generations(&spec, "mark_price").unwrap();
    assert_eq!(mark_gens.len(), 1);
    assert_eq!(mark_gens[0].bars, 2);

    let trade_gens = store.generations(&spec, "trades").unwrap();
    assert_eq!(trade_gens.len(), 1);
    assert_eq!(trade_gens[0].bars, 2);

    // PIT cutoff queries after compaction must still see rev=0 before rev=1 ts_available!
    let reader = ColdReader::new(tmp.path());
    assert_eq!(
        reader.read_bars(&spec, "1m", None, None, Some(t0 + MINUTE)).unwrap(),
        vec![b0]
    );
    assert_eq!(
        reader.read_bars(&spec, "1m", None, None, Some(t0 + 2 * MINUTE)).unwrap(),
        vec![b1]
    );
    assert_eq!(
        reader
            .read_derivatives(&spec, DerivativeKind::MarkPrice, None, None, Some(t0 + MINUTE), false)
            .unwrap(),
        vec![d0]
    );
    assert_eq!(
        reader
            .read_derivatives(&spec, DerivativeKind::MarkPrice, None, None, Some(t0 + 2 * MINUTE), false)
            .unwrap(),
        vec![d1]
    );
    assert_eq!(
        reader.read_trades(&spec, None, None, Some(t0 + 10)).unwrap(),
        vec![tr0]
    );
    assert_eq!(
        reader.read_trades(&spec, None, None, Some(t0 + 100)).unwrap(),
        vec![tr1]
    );
}

#[test]
fn test_retention_requires_both_24h_and_3_manifest_versions() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    let first = store.publish_bars(&spec, "1m", &[make_bar(t0, "100.5", 0)]).unwrap();
    store.publish_bars(&spec, "1m", &[make_bar(t0 + MINUTE, "100.6", 0)]).unwrap();
    store.compact(std::slice::from_ref(&spec), Some(t0)).unwrap();
    let retired_path = tmp.path().join(&first.parquet_path);
    assert!(retired_path.is_file());

    // >= 24h passed, but < 3 manifest versions since retirement: must survive!
    store.compact(std::slice::from_ref(&spec), Some(t0 + RETENTION_MS)).unwrap();
    assert!(retired_path.is_file());

    // Add 3 manifest commits (versions bump +3), but 1ms short of 24h: must survive!
    for offset in [2, 3, 4] {
        store
            .publish_bars(&spec, "1m", &[make_bar(t0 + offset * MINUTE, "100.7", 0)])
            .unwrap();
    }
    store.compact(std::slice::from_ref(&spec), Some(t0 + RETENTION_MS - 1)).unwrap();
    assert!(retired_path.is_file());

    // Both >= 24h AND >= 3 manifest versions satisfied: must be pruned!
    let report = store.compact(std::slice::from_ref(&spec), Some(t0 + RETENTION_MS)).unwrap();
    assert!(report.pruned_files >= 2); // first + second retired tails
    assert!(!retired_path.exists());
    assert_eq!(store.read_bars(&spec, "1m", None, None, None).unwrap().len(), 5);
}

#[test]
fn test_orphan_cleanup_removes_staging_temps_and_unreferenced_cold() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    let gen1 = store.publish_bars(&spec, "1m", &[make_bar(t0, "100.5", 0)]).unwrap();
    store.publish_bars(&spec, "1m", &[make_bar(t0 + MINUTE, "100.6", 0)]).unwrap();
    store.compact(std::slice::from_ref(&spec), Some(t0)).unwrap();

    // Inject crash leftovers: incomplete staging dir, stray .tmp files, and unreferenced cold parquet.
    let stale_staging = tmp.path().join(STAGING_SUBDIR).join("crashed-hash");
    fs::create_dir_all(&stale_staging).unwrap();
    fs::write(stale_staging.join("bars.parquet.tmp"), b"partial").unwrap();

    let stray_archive_tmp = tmp.path().join(ARCHIVE_SUBDIR).join(".orphan.tmp");
    fs::create_dir_all(stray_archive_tmp.parent().unwrap()).unwrap();
    fs::write(&stray_archive_tmp, b"tmp").unwrap();

    let unreferenced_parquet = tmp
        .path()
        .join("data/cold/bybit/BTCUSDT/1m/2025-11/orphan-unreferenced.parquet");
    fs::write(&unreferenced_parquet, b"orphan").unwrap();

    let report = store.cleanup_orphans().unwrap();
    assert_eq!(report.staging_dirs_removed, 1);
    assert_eq!(report.temp_files_removed, 1);
    assert_eq!(report.unreferenced_parquet_removed, 1);
    assert!(!stale_staging.exists());
    assert!(!stray_archive_tmp.exists());
    assert!(!unreferenced_parquet.exists());

    // Retired file and active partition file MUST remain intact!
    assert!(tmp.path().join(&gen1.parquet_path).is_file());
    assert_eq!(store.read_bars(&spec, "1m", None, None, None).unwrap().len(), 2);

    // Reopening ColdStore also auto-cleans staging orphans.
    fs::create_dir_all(&stale_staging).unwrap();
    fs::write(stale_staging.join("bars.parquet"), b"partial").unwrap();
    store.close().unwrap();

    let reopened = ColdStore::open(tmp.path()).unwrap();
    assert!(!stale_staging.exists());
    assert_eq!(reopened.read_bars(&spec, "1m", None, None, None).unwrap().len(), 2);
}

#[test]
fn test_reader_integrity_and_snapshot_pinning() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    let g1 = store.publish_bars(&spec, "1m", &[make_bar(t0, "100.5", 0)]).unwrap();
    let reader = ColdReader::new(tmp.path());

    // Stray parquet outside manifest is ignored.
    let target = tmp.path().join(&g1.parquet_path);
    let stray = target.with_file_name("stray.parquet");
    fs::write(&stray, b"corrupt").unwrap();
    assert_eq!(reader.read_bars(&spec, "1m", None, None, None).unwrap().len(), 1);

    // Capture immutable snapshot before adding a second bar.
    let snap = reader.capture_snapshot(&[(&spec, t0 + 10 * MINUTE)], &[]).unwrap();
    store.publish_bars(&spec, "1m", &[make_bar(t0 + MINUTE, "100.6", 0)]).unwrap();

    let pinned_reader = ColdReader::with_snapshot(tmp.path(), snap);
    assert_eq!(
        pinned_reader.read_bars(&spec, "1m", None, None, None).unwrap().len(),
        1
    );
    assert_eq!(reader.read_bars(&spec, "1m", None, None, None).unwrap().len(), 2);

    // Tampering a tracked generation file raises StoreError::Integrity.
    let mut bytes = fs::read(&target).unwrap();
    bytes.extend_from_slice(b"tamper");
    fs::write(&target, bytes).unwrap();
    assert!(matches!(
        reader.read_bars(&spec, "1m", None, None, None),
        Err(StoreError::Integrity(_))
    ));
}

#[test]
fn test_single_writer_data_dir_lock_and_lockless_reader() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();

    // Read-only ColdReader works on empty directory without creating manifest or taking write lock.
    let reader = ColdReader::new(tmp.path());
    assert!(reader.read_bars(&spec, "1m", None, None, None).unwrap().is_empty());
    assert!(!tmp.path().join(MANIFEST_DB).exists());

    // DataDirLock acquires exclusive fs2 lock and rejects second acquirer.
    let mut lock1 = DataDirLock::acquire(tmp.path(), "service").unwrap();
    assert!(DataDirLock::probe(tmp.path()).unwrap().is_some());
    assert!(matches!(
        DataDirLock::acquire(tmp.path(), "second"),
        Err(StoreError::ServiceLocked(_))
    ));

    // ColdStore can be opened inside the same service process while DataDirLock is held,
    // and rejects a second in-process ColdStore writer on the same manifest path.
    let store = ColdStore::open(tmp.path()).unwrap();
    assert!(matches!(
        ColdStore::open(tmp.path()),
        Err(StoreError::SingleWriterViolation(_))
    ));
    store.publish_bars(&spec, "1m", &[make_bar(t0, "100.5", 0)]).unwrap();

    // Lockless ColdReader reads concurrently without touching service.lock or writer registry.
    assert_eq!(reader.read_bars(&spec, "1m", None, None, None).unwrap().len(), 1);

    // Closing ColdStore checkpoints WAL and releases writer claim.
    store.close().unwrap();
    let wal_path = tmp.path().join(format!("{MANIFEST_DB}-wal"));
    if wal_path.exists() {
        assert_eq!(fs::metadata(&wal_path).unwrap().len(), 0);
    }
    let reopened = ColdStore::open(tmp.path()).unwrap();
    assert_eq!(reopened.read_bars(&spec, "1m", None, None, None).unwrap().len(), 1);
    reopened.close().unwrap();

    lock1.release();
    assert!(DataDirLock::probe(tmp.path()).unwrap().is_none());
    let lock2 = DataDirLock::acquire(tmp.path(), "service2").unwrap();
    drop(lock2);
    assert!(DataDirLock::probe(tmp.path()).unwrap().is_none());

    // Check SQLite journal_mode=wal
    let conn = Connection::open(tmp.path().join(MANIFEST_DB)).unwrap();
    let mode: String = conn
        .query_row("PRAGMA journal_mode", [], |r| r.get(0))
        .unwrap();
    assert_eq!(mode, "wal");
}

#[test]
fn test_hot_buffer_and_pit_reconciliation() {
    let spec = btc_spec();
    let t0 = base_ts();
    let clock = Arc::new(AtomicUsize::new((t0 + MINUTE) as usize));
    let clk_clone = clock.clone();
    let buffer = HotStreamBuffer::new(
        std::slice::from_ref(&spec),
        Arc::new(move || clk_clone.load(Ordering::SeqCst) as i64),
        2, // tiny LRU capacity to test eviction
    )
    .unwrap();

    let compact = |ts: i64, close: &str, status: BarStatus| CompactBar {
        timestamp: ts,
        open: (status != BarStatus::FeedGap).then(|| dec("100.0")),
        high: (status != BarStatus::FeedGap).then(|| dec("101.0")),
        low: (status != BarStatus::FeedGap).then(|| dec("99.0")),
        close: (status != BarStatus::FeedGap).then(|| dec(close)),
        volume: (status != BarStatus::FeedGap).then(|| dec("1.0")),
        turnover: (status != BarStatus::FeedGap).then(|| dec("100.0")),
        status,
        ts_available: ts + MINUTE,
        precision: PrecisionTier::SecondVerified,
    };

    // FeedGap is skipped by HotStreamBuffer.
    assert!(
        buffer
            .ingest(&FeedEvent::Kline(KlineEvent {
                instrument_id: spec.instrument_id.clone(),
                timeframe: "1m".to_owned(),
                bar: compact(t0, "0", BarStatus::FeedGap),
                confirmed: true,
                revision: 0,
            }))
            .is_none()
    );
    assert!(buffer.recent_bars(&spec.instrument_id, "1m").is_empty());

    // Unconfirmed rev=0 followed by confirmed rev=1.
    buffer.ingest(&FeedEvent::Kline(KlineEvent {
        instrument_id: spec.instrument_id.clone(),
        timeframe: "1m".to_owned(),
        bar: compact(t0, "100.5", BarStatus::Normal),
        confirmed: false,
        revision: 0,
    }));
    assert_eq!(buffer.recent_bars(&spec.instrument_id, "1m")[0].close, dec("100.5"));

    clock.store((t0 + MINUTE + 500) as usize, Ordering::SeqCst);
    buffer.ingest(&FeedEvent::Kline(KlineEvent {
        instrument_id: spec.instrument_id.clone(),
        timeframe: "1m".to_owned(),
        bar: compact(t0, "100.8", BarStatus::Normal),
        confirmed: true,
        revision: 1,
    }));
    let recent = buffer.recent_bars(&spec.instrument_id, "1m");
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].close, dec("100.8"));
    assert_eq!(recent[0].revision, 1);

    // LRU capacity = 2: insert t0+1m and t0+2m -> t0 evicted, t0+1m and t0+2m remain.
    buffer.ingest_received_kline(&ReceivedKlineEvent {
        event: KlineEvent {
            instrument_id: spec.instrument_id.clone(),
            timeframe: "1m".to_owned(),
            bar: compact(t0 + MINUTE, "101.0", BarStatus::Normal),
            confirmed: true,
            revision: 0,
        },
        ts_ingested: t0 + 2 * MINUTE,
        history_only: false,
        synthetic: false,
    });
    buffer.ingest_received_kline(&ReceivedKlineEvent {
        event: KlineEvent {
            instrument_id: spec.instrument_id.clone(),
            timeframe: "1m".to_owned(),
            bar: compact(t0 + 2 * MINUTE, "101.5", BarStatus::Normal),
            confirmed: true,
            revision: 0,
        },
        ts_ingested: t0 + 3 * MINUTE,
        history_only: false,
        synthetic: false,
    });
    let all_hot = buffer.revisions(&spec.instrument_id, "1m", 0, None);
    assert_eq!(
        all_hot.iter().map(|b| b.ts_event).collect::<Vec<_>>(),
        vec![t0 + MINUTE, t0 + 2 * MINUTE]
    );

    // Shared PIT cutoff & reconciliation helpers:
    let cold_bar = make_bar(t0, "100.0", 0);
    let mut hot_rev = make_bar(t0, "100.8", 0);
    hot_rev.ts_ingested = t0 + 2 * MINUTE;
    let pending = unpublished_revisions(std::slice::from_ref(&cold_bar), std::slice::from_ref(&hot_rev));
    assert_eq!(pending[&0].revision, 1);

    let visible_early = range_visible_bars(
        std::slice::from_ref(&cold_bar),
        std::slice::from_ref(&hot_rev),
        Some(t0 + MINUTE),
        false,
    );
    assert_eq!(visible_early[0].close, dec("100.0"));

    let visible_late = range_visible_bars(
        std::slice::from_ref(&cold_bar),
        std::slice::from_ref(&hot_rev),
        Some(t0 + 2 * MINUTE),
        false,
    );
    assert_eq!(visible_late[0].close, dec("100.8"));
    assert_eq!(visible_late[0].revision, 1);

    let (win, next_avail) =
        revision_window(std::slice::from_ref(&cold_bar), &[hot_rev], t0, Some(t0 + MINUTE));
    assert_eq!(win, vec![cold_bar.clone()]);
    assert_eq!(next_avail, Some(t0 + 2 * MINUTE));

    assert_eq!(latest_visible_bars([cold_bar], Some(t0)), vec![]);
}

#[test]
fn test_decimal128_18_digit_boundary_and_path_traversal_rejection() {
    let spec = InstrumentSpec {
        tick_size: Decimal::new(1, 1),
        qty_step: Decimal::new(1, 0),
        min_qty: Decimal::new(1, 0),
        price_precision: 1,
        qty_precision: 0,
        ..btc_spec()
    };
    // 18 digits exactly: 999_999_999_999_999_99.9 (18 digits at scale 1) -> accepted
    let max_18 = dec("99999999999999999.9");
    let raw = talaria_store::decimal_to_i128(max_18, 1, "close").unwrap();
    assert_eq!(raw, 999_999_999_999_999_999_i128);
    assert_eq!(talaria_store::i128_to_decimal(raw, 1, "close").unwrap(), max_18);

    let min_18 = dec("-99999999999999999.9");
    let raw_neg = talaria_store::decimal_to_i128(min_18, 1, "close").unwrap();
    assert_eq!(raw_neg, -999_999_999_999_999_999_i128);
    assert_eq!(talaria_store::i128_to_decimal(raw_neg, 1, "close").unwrap(), min_18);

    // 19 digits: 1_000_000_000_000_000_00.0 -> rejected
    let over_18 = dec("100000000000000000.0");
    assert!(matches!(
        talaria_store::decimal_to_i128(over_18, 1, "close"),
        Err(StoreError::Quality(_))
    ));

    // Path traversal inside manifest DB must be rejected with StoreError::Integrity.
    let tmp = TempDir::new().unwrap();
    let store = ColdStore::open(tmp.path()).unwrap();
    store.publish_bars(&spec, "1m", &[make_bar(base_ts(), "100.5", 0)]).unwrap();
    store.close().unwrap();

    let conn = Connection::open(tmp.path().join(MANIFEST_DB)).unwrap();
    conn.execute("UPDATE generations SET parquet_path = '../outside.parquet'", [])
        .unwrap();
    drop(conn);

    let reader = ColdReader::new(tmp.path());
    assert!(matches!(
        reader.read_bars(&spec, "1m", None, None, None),
        Err(StoreError::Integrity(_))
    ));
}

#[test]
fn test_compact_deduplicates_identical_revisions_and_preserves_distinct() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    store
        .publish_bars(&spec, "1m", &[make_bar(t0, "100.0", 0)])
        .unwrap();
    store
        .publish_bars(
            &spec,
            "1m",
            &[make_bar(t0, "100.0", 0), make_bar(t0 + MINUTE, "101.0", 0)],
        )
        .unwrap();
    store
        .publish_bars(&spec, "1m", &[make_bar(t0, "100.5", 1)])
        .unwrap();

    let report = store.compact(std::slice::from_ref(&spec), Some(t0)).unwrap();
    assert_eq!(report.compacted_groups, 1);

    let revs = ColdReader::new(tmp.path())
        .read_revisions(&spec, "1m", None, None)
        .unwrap();
    assert_eq!(revs.len(), 3);
    assert_eq!(revs[0].ts_event, t0);
    assert_eq!(revs[0].revision, 0);
    assert_eq!(revs[1].ts_event, t0);
    assert_eq!(revs[1].revision, 1);
    assert_eq!(revs[2].ts_event, t0 + MINUTE);
}

#[test]
fn test_deduplicate_partitions_and_prune_retired_now() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    let g1 = store
        .publish_bars(
            &spec,
            "1m",
            &[make_bar(t0, "100.0", 0), make_bar(t0 + MINUTE, "101.0", 0)],
        )
        .unwrap();
    let g2 = store
        .publish_bars(
            &spec,
            "1m",
            &[make_bar(t0 + MINUTE, "101.0", 0), make_bar(t0 + 2 * MINUTE, "100.8", 0)],
        )
        .unwrap();

    let report = store
        .deduplicate_partitions(std::slice::from_ref(&spec), Some(t0))
        .unwrap();
    assert_eq!(report.compacted_groups, 1);
    assert!(tmp.path().join(&g1.parquet_path).exists());
    assert!(tmp.path().join(&g2.parquet_path).exists());

    let revs = ColdReader::new(tmp.path())
        .read_revisions(&spec, "1m", None, None)
        .unwrap();
    assert_eq!(revs.len(), 3);

    let pruned = store.prune_retired_now().unwrap();
    assert_eq!(pruned, 2);
    assert!(!tmp.path().join(&g1.parquet_path).exists());
    assert!(!tmp.path().join(&g2.parquet_path).exists());
    assert_eq!(
        ColdReader::new(tmp.path())
            .read_bars(&spec, "1m", None, None, None)
            .unwrap()
            .len(),
        3
    );
}

#[test]
fn test_publish_without_raw_archive_and_prune_unreferenced_raw_archive() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let t0 = base_ts();
    let store = ColdStore::open(tmp.path()).unwrap();

    let g1 = store
        .publish_bars(&spec, "1m", &[make_bar(t0, "100.0", 0)])
        .unwrap();
    let g2 = store
        .publish_bars(&spec, "1m", &[make_bar(t0 + MINUTE, "101.0", 0)])
        .unwrap();
    store.compact(std::slice::from_ref(&spec), Some(t0)).unwrap();

    let g3 = store
        .publish_bars_without_raw_archive(&spec, "1m", &[make_bar(t0 + 2 * MINUTE, "100.8", 0)])
        .unwrap();
    let g3_archive = tmp
        .path()
        .join(ARCHIVE_SUBDIR)
        .join(format!("{}.jsonl", g3.content_sha256));
    assert!(!g3_archive.exists());
    assert_eq!(store.read_bars(&spec, "1m", None, None, None).unwrap().len(), 3);

    let g1_archive = tmp
        .path()
        .join(ARCHIVE_SUBDIR)
        .join(format!("{}.jsonl", g1.content_sha256));
    let g2_archive = tmp
        .path()
        .join(ARCHIVE_SUBDIR)
        .join(format!("{}.jsonl", g2.content_sha256));
    assert!(g1_archive.exists());
    assert!(g2_archive.exists());

    let (removed_files, removed_bytes) = store.prune_unreferenced_raw_archive().unwrap();
    assert_eq!(removed_files, 2);
    assert!(removed_bytes > 0);
    assert!(!g1_archive.exists());
    assert!(!g2_archive.exists());
    assert_eq!(store.read_bars(&spec, "1m", None, None, None).unwrap().len(), 3);
}

#[test]
fn migrates_legacy_binance_spot_venue_to_binance_on_open() {
    let tmp = TempDir::new().unwrap();
    let t0 = base_ts();
    let legacy_spec = InstrumentSpec {
        instrument_id: "BTC-USDT-BN-SPOT".to_owned(),
        venue: "binance_spot".to_owned(),
        symbol: "BTCUSDT-SPOT".to_owned(),
        asset_class: "crypto_spot".to_owned(),
        tick_size: Decimal::new(1, 2),
        qty_step: Decimal::new(1, 5),
        min_qty: Decimal::new(1, 5),
        price_precision: 2,
        qty_precision: 5,
    };
    let mut bar = make_bar(t0, "100.50", 0);
    bar.symbol = "BTCUSDT-SPOT".to_owned();
    bar.volume = dec("1.00000");
    bar.turnover = dec("100.50000");

    let store = ColdStore::open(tmp.path()).unwrap();
    store
        .publish_bars(&legacy_spec, "1m", std::slice::from_ref(&bar))
        .unwrap();
    store.close().unwrap();

    assert!(tmp.path().join("data/cold/binance_spot/BTCUSDT-SPOT").exists());

    // Reopen triggers automatic idempotent migration from binance_spot -> binance.
    let reopened = ColdStore::open(tmp.path()).unwrap();
    reopened.close().unwrap();

    assert!(!tmp.path().join("data/cold/binance_spot").exists());
    assert!(tmp.path().join("data/cold/binance/BTCUSDT-SPOT").exists());

    let migrated_spec = InstrumentSpec {
        venue: "binance".to_owned(),
        ..legacy_spec
    };
    let bars = ColdReader::new(tmp.path())
        .read_bars(&migrated_spec, "1m", None, None, None)
        .unwrap();
    assert_eq!(bars.len(), 1);
    assert_eq!(bars[0].symbol, "BTCUSDT-SPOT");
    assert_eq!(bars[0].close, dec("100.50"));

    // Second reopen is a clean no-op.
    let reopened_again = ColdStore::open(tmp.path()).unwrap();
    reopened_again.close().unwrap();
}
