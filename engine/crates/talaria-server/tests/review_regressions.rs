//! Regression tests for all 9 v0.0.30 issues and the alert/scanner evidence snapshot fix.

use std::collections::BTreeMap;
use std::fs;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, AtomicI64, AtomicUsize, Ordering};

use axum::Router;
use axum::extract::Query;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::get;
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::alerts::{AlertCreate, AlertRecord, AlertTriggerRecord};
use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::feed::{DerivativeTickEvent, FeedEvent, KlineEvent};
use talaria_contracts::rules::{
    AlertMetric, Rule, RuleComparisonTest, RuleExpression, RuleOperand, RuleTiming, RuleVersion,
};
use talaria_contracts::snapshot::{SnapshotMeta, SnapshotMode, SnapshotRequest};
use talaria_contracts::types::PrecisionTier;
use talaria_feed::BarPublishSink;
use talaria_packs::{DEFAULT_PACK_NAME, get_market_pack};
use talaria_server::alerts::AlertEngine;
use talaria_server::cold_writer::ColdWriter;
use talaria_server::key_levels::KeyLevelRegistry;
use talaria_server::routes::publish_bars_to_cold_store;
use talaria_server::scanner::{ScannerConfig, ScannerEngine};
use talaria_server::state_store::WorkbenchStateStore;
use talaria_server::structures::StructureRegistry;
use talaria_server::ws::desk::DeskBroadcast;
use talaria_server::{ServerConfig, start_server};
use talaria_store::{ColdReader, ColdStore, HotStreamBuffer};
use tempfile::TempDir;
use tokio::net::TcpListener;

const BTC: &str = "BTC-USDT-PERP";
const BASE_TS: i64 = 1_767_225_600_000; // 2026-01-01T00:00:00Z
const MINUTE: i64 = 60_000;
const HOUR: i64 = 3_600_000;
const DAY: i64 = 86_400_000;

fn btc_spec() -> talaria_contracts::market::InstrumentSpec {
    get_market_pack(DEFAULT_PACK_NAME)
        .unwrap()
        .instrument(BTC)
        .unwrap()
        .clone()
}

fn dec(s: &str) -> Decimal {
    s.parse().expect("valid decimal")
}

fn raw_bar(ts: i64, close_val: i64, rev: u64) -> RawBar {
    let price = Decimal::from(close_val);
    let vol = Decimal::from(10);
    RawBar {
        symbol: "BTCUSDT".to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: ts,
        open: price,
        high: price + Decimal::from(5),
        low: price - Decimal::from(5),
        close: price,
        volume: vol,
        turnover: price * vol,
        ts_available: ts + MINUTE,
        ts_ingested: ts + MINUTE,
        ts_committed: ts + MINUTE,
        precision: PrecisionTier::SecondVerified,
        revision: rev,
        confirmed: true,
        history_only: false,
        synthetic: false,
    }
}

fn bybit_kline_json(rows: &[(i64, i64)]) -> String {
    let list: Vec<Value> = rows
        .iter()
        .rev()
        .map(|(ts, close)| {
            json!([
                ts.to_string(),
                close.to_string(),
                (close + 10).to_string(),
                (close - 10).to_string(),
                close.to_string(),
                "10.000",
                (close * 10).to_string(),
            ])
        })
        .collect();
    json!({
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": list,
        }
    })
    .to_string()
}

#[tokio::test]
async fn v030_1_derivative_resend_preserves_first_visibility_and_records_value_revisions() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let now = Arc::new(AtomicI64::new(BASE_TS + 1_000));
    let now_c = Arc::clone(&now);
    let now_fn: Arc<dyn Fn() -> i64 + Send + Sync> = Arc::new(move || now_c.load(Ordering::SeqCst));

    let store = Arc::new(ColdStore::open(tmp.path()).unwrap());
    let reader = Arc::new(ColdReader::new(tmp.path()));
    let hot = Arc::new(HotStreamBuffer::with_default_capacity(
        std::slice::from_ref(&spec),
        Arc::clone(&now_fn),
    ).unwrap());
    let desk = Arc::new(DeskBroadcast::new());
    let kl = Arc::new(KeyLevelRegistry::new(Arc::clone(&now_fn)));
    let st = Arc::new(StructureRegistry::new(Arc::clone(&now_fn)));
    let alerts = Arc::new(AlertEngine::new(Arc::clone(&now_fn), 600));
    let scanner = Arc::new(ScannerEngine::new(
        Arc::clone(&now_fn),
        ScannerConfig::default(),
        Arc::new(|_, _, _, _| String::new()),
    ));
    let state_store = Arc::new(WorkbenchStateStore::open(tmp.path(), Arc::clone(&now_fn)).unwrap());

    let writer = ColdWriter::spawn(
        Arc::clone(&now_fn),
        std::slice::from_ref(&spec),
        Some(Arc::clone(&store)),
        Arc::clone(&reader),
        hot,
        desk,
        None,
        kl,
        st,
        alerts,
        scanner,
        state_store,
    );

    // Cross-batch identical resend: first received at t1, resent identical 1s later at t2.
    let t1 = BASE_TS + 1_000;
    let t2 = BASE_TS + 2_000;
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("100")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS,
        ts_ingested: t1,
    }));
    writer.flush().await;

    now.store(t2, Ordering::SeqCst);
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("100")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS,
        ts_ingested: t2,
    }));
    writer.flush().await;

    let between = reader
        .read_derivatives(&spec, DerivativeKind::MarkPrice, None, None, Some(t1 + 500), false)
        .unwrap();
    assert_eq!(between.len(), 1);
    assert_eq!(between[0].value, dec("100"));
    assert_eq!(between[0].ts_available, t1);

    // Same-batch identical resend + value change at a new ts_event.
    let ts2 = BASE_TS + 10_000;
    let t3 = BASE_TS + 10_000;
    let t4 = BASE_TS + 11_000;
    let t5 = BASE_TS + 12_000;
    now.store(t5, Ordering::SeqCst);
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("105")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: ts2,
        ts_ingested: t3,
    }));
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("105")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: ts2,
        ts_ingested: t4,
    }));
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("108")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: ts2,
        ts_ingested: t5,
    }));
    writer.flush().await;

    let at_t3 = reader
        .read_derivatives(&spec, DerivativeKind::MarkPrice, Some(ts2), Some(ts2), Some(t3 + 500), false)
        .unwrap();
    assert_eq!(at_t3.len(), 1);
    assert_eq!(at_t3[0].value, dec("105"));
    assert_eq!(at_t3[0].revision, 0);

    let at_t5 = reader
        .read_derivatives(&spec, DerivativeKind::MarkPrice, Some(ts2), Some(ts2), Some(t5), false)
        .unwrap();
    assert_eq!(at_t5.len(), 1);
    assert_eq!(at_t5[0].value, dec("108"));
    assert_eq!(at_t5[0].revision, 1);

    writer.shutdown().await;
}

#[tokio::test]
async fn v030_2_first_backfill_and_repeat_freeze_produce_identical_bars_and_funding() {
    let tmp = TempDir::new().unwrap();
    let fix = TempDir::new().unwrap();
    let replay_path = tmp.path().join("replay.jsonl");
    fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    // 200 hourly bars (> 8 days) and 2 funding settlements (one 192h before cutoff, outside 7d default!).
    let bars_rows: Vec<(i64, i64)> = (0..200).map(|i| (BASE_TS + i * HOUR, 50_000 + i)).collect();
    let cutoff = BASE_TS + 200 * HOUR;
    let btc_dir = fix.path().join("BTCUSDT");
    fs::create_dir_all(btc_dir.join("derivatives")).unwrap();
    fs::write(btc_dir.join("1h.json"), bybit_kline_json(&bars_rows)).unwrap();
    fs::write(
        btc_dir.join("derivatives/funding.json"),
        json!({
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "symbol": "BTCUSDT",
                "list": [
                    {"symbol": "BTCUSDT", "fundingRate": "0.0002", "fundingRateTimestamp": (BASE_TS + 192 * HOUR).to_string()},
                    {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": (BASE_TS + 8 * HOUR).to_string()}
                ]
            }
        })
        .to_string(),
    )
    .unwrap();

    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.fixture_dir = Some(fix.path().to_path_buf());
    cfg.now_ms = Some(Arc::new(move || cutoff + HOUR));

    let srv = start_server(cfg).await.unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    let req_body = SnapshotRequest {
        instrument_id: BTC.to_owned(),
        timeframes: vec!["1h".to_owned()],
        cutoff_ts: Some(cutoff),
        mode: SnapshotMode::HistoricalPit,
    };
    let s1: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&req_body)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let s2: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&req_body)
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    assert_eq!(s1.bar_counts.get("1h"), Some(&200));
    assert_eq!(s2.bar_counts.get("1h"), Some(&200));

    let d1: Value = client
        .get(format!("{}/api/snapshots/{}/derivatives?timeframe=1h", srv.base_url(), s1.snapshot_id))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let d2: Value = client
        .get(format!("{}/api/snapshots/{}/derivatives?timeframe=1h", srv.base_url(), s2.snapshot_id))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    let f1 = d1["series"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["kind"] == "funding_rate")
        .unwrap()["points"]
        .as_array()
        .unwrap();
    let f2 = d2["series"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["kind"] == "funding_rate")
        .unwrap()["points"]
        .as_array()
        .unwrap();
    assert_eq!(f1.len(), 2);
    assert_eq!(f1, f2);

    srv.shutdown().await;
}

#[tokio::test]
async fn v030_3_omitted_cutoff_is_pinned_before_async_backfill_across_minute_boundary() {
    let tmp = TempDir::new().unwrap();
    let t_start = BASE_TS + 10 * MINUTE + 30_000;
    let t_after = BASE_TS + 12 * MINUTE + 5_000;
    let clock = Arc::new(AtomicI64::new(t_start));
    let clock_http = Arc::clone(&clock);

    let app = Router::new().route(
        "/v5/market/kline",
        get(move |Query(q): Query<BTreeMap<String, String>>| {
            let clock_http = Arc::clone(&clock_http);
            async move {
                // Advance wall clock across a minute boundary during upstream fetch.
                clock_http.store(t_after, Ordering::SeqCst);
                let _ = q;
                let body = bybit_kline_json(&[
                    (BASE_TS + 9 * MINUTE, 50_000),
                    (BASE_TS + 10 * MINUTE, 50_100),
                    (BASE_TS + 11 * MINUTE, 50_200),
                ]);
                (StatusCode::OK, body)
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mock_addr = listener.local_addr().unwrap();
    let mock_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });

    let clock_srv = Arc::clone(&clock);
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.history_base_url = Some(format!("http://{mock_addr}"));
    cfg.now_ms = Some(Arc::new(move || clock_srv.load(Ordering::SeqCst)));

    let srv = start_server(cfg).await.unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    let s1: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: None,
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(s1.cutoff_ts, t_start);

    let s2: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(s1.cutoff_ts),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    let b1: Value = client
        .get(format!("{}/api/snapshots/{}/bars?timeframe=1m&limit=100", srv.base_url(), s1.snapshot_id))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let b2: Value = client
        .get(format!("{}/api/snapshots/{}/bars?timeframe=1m&limit=100", srv.base_url(), s2.snapshot_id))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(b1["bars"], b2["bars"]);
    assert_eq!(
        b1["bars"].as_array().unwrap().last().unwrap()["timestamp"],
        BASE_TS + 9 * MINUTE
    );

    mock_handle.abort();
    srv.shutdown().await;
}

#[tokio::test]
async fn v030_4_historical_replay_flush_updates_key_levels_and_structures() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    // Wall clock is 100 days after BASE_TS (well past 900 * 1m = 15h window).
    let wall_now = BASE_TS + 100 * DAY;
    let now_fn: Arc<dyn Fn() -> i64 + Send + Sync> = Arc::new(move || wall_now);

    let store = Arc::new(ColdStore::open(tmp.path()).unwrap());
    let reader = Arc::new(ColdReader::new(tmp.path()));
    let hot = Arc::new(HotStreamBuffer::with_default_capacity(
        std::slice::from_ref(&spec),
        Arc::clone(&now_fn),
    ).unwrap());
    let desk = Arc::new(DeskBroadcast::new());
    let kl = Arc::new(KeyLevelRegistry::new(Arc::clone(&now_fn)));
    let st = Arc::new(StructureRegistry::new(Arc::clone(&now_fn)));
    let alerts = Arc::new(AlertEngine::new(Arc::clone(&now_fn), 600));
    let scanner = Arc::new(ScannerEngine::new(
        Arc::clone(&now_fn),
        ScannerConfig::default(),
        Arc::new(|_, _, _, _| String::new()),
    ));
    let state_store = Arc::new(WorkbenchStateStore::open(tmp.path(), Arc::clone(&now_fn)).unwrap());

    let writer = ColdWriter::spawn(
        Arc::clone(&now_fn),
        std::slice::from_ref(&spec),
        Some(store),
        reader,
        hot,
        desk,
        None,
        Arc::clone(&kl),
        Arc::clone(&st),
        alerts,
        scanner,
        state_store,
    );

    for i in 0..80 {
        let ts = BASE_TS + i * MINUTE;
        let close = 50_000 + if i % 2 == 0 { i * 15 } else { -i * 10 };
        let compact = CompactBar {
            timestamp: ts,
            open: Some(Decimal::from(close - 5)),
            high: Some(Decimal::from(close + 25)),
            low: Some(Decimal::from(close - 25)),
            close: Some(Decimal::from(close)),
            volume: Some(Decimal::from(100)),
            turnover: Some(Decimal::from(close * 100)),
            ts_available: ts + MINUTE,
            precision: PrecisionTier::SecondVerified,
            status: BarStatus::Normal,
        };
        writer.on_bar_close(&spec, "1m", &compact, ts + MINUTE, 0);
    }
    writer.flush().await;

    assert!(
        !kl.levels_at(BTC, "1m", wall_now).is_empty(),
        "expected key levels to be registered for historical replay"
    );
    assert!(
        !st.structures_at(BTC, "1m", wall_now).is_empty(),
        "expected structures to be registered for historical replay"
    );
    writer.shutdown().await;
}

#[tokio::test]
async fn v030_5_sparse_hot_buffer_bars_far_apart_are_preserved_in_snapshot() {
    let tmp = TempDir::new().unwrap();
    let cutoff = BASE_TS + 70 * HOUR;
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.now_ms = Some(Arc::new(move || cutoff));
    let srv = start_server(cfg).await.unwrap();

    for ts in [BASE_TS, BASE_TS + 60 * HOUR] {
        srv.state.hot_buffer.ingest(&FeedEvent::Kline(KlineEvent {
            instrument_id: BTC.to_owned(),
            timeframe: "1m".to_owned(),
            bar: CompactBar {
                timestamp: ts,
                open: Some(dec("50000")),
                high: Some(dec("50100")),
                low: Some(dec("49900")),
                close: Some(dec("50050")),
                volume: Some(dec("5")),
                turnover: Some(dec("250250")),
                ts_available: ts + MINUTE,
                precision: PrecisionTier::SecondVerified,
                status: BarStatus::Normal,
            },
            confirmed: true,
            revision: 0,
        }));
    }

    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());
    let snap: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(cutoff),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    let page: Value = client
        .get(format!("{}/api/snapshots/{}/bars?timeframe=1m&limit=100", srv.base_url(), snap.snapshot_id))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let timestamps: Vec<i64> = page["bars"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|b| b["status"] == "NORMAL")
        .map(|b| b["timestamp"].as_i64().unwrap())
        .collect();
    assert_eq!(timestamps, vec![BASE_TS, BASE_TS + 60 * HOUR]);

    srv.shutdown().await;
}

#[tokio::test]
async fn v030_6_snapshot_carries_forward_funding_rate_older_than_24h_lookback() {
    let tmp = TempDir::new().unwrap();
    let spec = btc_spec();
    let bar_start = BASE_TS + 48 * HOUR;
    let cutoff = bar_start + 10 * MINUTE;

    let store = ColdStore::open(tmp.path()).unwrap();
    let bars: Vec<RawBar> = (0..5)
        .map(|i| raw_bar(bar_start + i * MINUTE, 50_000 + i, 0))
        .collect();
    store.publish_bars(&spec, "1m", &bars).unwrap();
    store
        .publish_derivatives(
            &spec,
            DerivativeKind::FundingRate,
            &[DerivativePoint {
                instrument_id: BTC.to_owned(),
                kind: DerivativeKind::FundingRate,
                ts_event: BASE_TS,
                ts_published: Some(BASE_TS),
                ts_available: BASE_TS,
                value: dec("0.00015"),
                timeframe: None,
                precision: PrecisionTier::SecondVerified,
                ts_ingested: BASE_TS,
                ts_committed: BASE_TS,
                revision: 0,
                settled: Some(true),
            }],
        )
        .unwrap();
    store.close().unwrap();

    let mut cfg = ServerConfig::new(tmp.path());
    cfg.now_ms = Some(Arc::new(move || cutoff));
    let srv = start_server(cfg).await.unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    let snap: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(cutoff),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    let derivs: Value = client
        .get(format!("{}/api/snapshots/{}/derivatives?timeframe=1m", srv.base_url(), snap.snapshot_id))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        derivs["summary"]["funding_rate"]
            .as_str()
            .unwrap()
            .parse::<Decimal>()
            .unwrap(),
        dec("0.00015")
    );
    assert_eq!(derivs["summary"]["funding_settled_ts"], BASE_TS);

    srv.shutdown().await;
}

#[tokio::test]
async fn v030_7_backfill_failure_cooldown_preserves_error_on_retry_and_isolates_cutoffs() {
    let tmp = TempDir::new().unwrap();
    let fail_upstream = Arc::new(AtomicBool::new(true));
    let hits = Arc::new(AtomicUsize::new(0));
    let fail_c = Arc::clone(&fail_upstream);
    let hits_c = Arc::clone(&hits);

    let app = Router::new().route(
        "/v5/market/kline",
        get(move || {
            let fail_c = Arc::clone(&fail_c);
            let hits_c = Arc::clone(&hits_c);
            async move {
                hits_c.fetch_add(1, Ordering::SeqCst);
                if fail_c.load(Ordering::SeqCst) {
                    (StatusCode::INTERNAL_SERVER_ERROR, "upstream error".to_owned()).into_response()
                } else {
                    (StatusCode::OK, bybit_kline_json(&[(BASE_TS, 50_000)])).into_response()
                }
            }
        }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let mock_addr = listener.local_addr().unwrap();
    let mock_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, app).await;
    });

    let now_ts = BASE_TS + 10 * MINUTE;
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.history_base_url = Some(format!("http://{mock_addr}"));
    cfg.now_ms = Some(Arc::new(move || now_ts));
    let srv = start_server(cfg).await.unwrap();

    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    let r1 = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(now_ts),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap();
    assert!(!r1.status().is_success(), "first failure must propagate error");
    let hits_after_first = hits.load(Ordering::SeqCst);

    // Retry same request inside cooldown: must STILL fail (never return 200 empty snapshot) and skip upstream.
    let r2 = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(now_ts),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap();
    assert!(!r2.status().is_success(), "cooldown retry must still return upstream failure");
    assert_eq!(hits.load(Ordering::SeqCst), hits_after_first);

    // Independent historical cutoff is NOT blocked by the first window's cooldown.
    fail_upstream.store(false, Ordering::SeqCst);
    let r3 = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(BASE_TS + MINUTE),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap();
    assert_eq!(r3.status(), 200);

    mock_handle.abort();
    srv.shutdown().await;
}

#[tokio::test]
async fn v030_8_rest_backfill_tail_files_trigger_periodic_compaction() {
    let tmp = TempDir::new().unwrap();
    let replay_path = tmp.path().join("replay.jsonl");
    fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    // Past month relative to wall clock so compact_periodic merges tails into partition-*.
    let wall_now = BASE_TS + 45 * DAY;
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.now_ms = Some(Arc::new(move || wall_now));
    let srv = start_server(cfg).await.unwrap();

    let store = Arc::clone(srv.state.cold_store.as_ref().unwrap());
    let spec = btc_spec();
    publish_bars_to_cold_store(
        Arc::clone(&store),
        spec.clone(),
        "1m".to_owned(),
        vec![raw_bar(BASE_TS, 50_000, 0)],
    )
    .await
    .unwrap();
    publish_bars_to_cold_store(
        Arc::clone(&store),
        spec.clone(),
        "1m".to_owned(),
        vec![raw_bar(BASE_TS + MINUTE, 50_010, 0)],
    )
    .await
    .unwrap();

    assert_eq!(srv.state.reader.generations(&spec, "1m").unwrap().len(), 2);
    srv.state.cold_writer.as_ref().unwrap().compact_periodic_now().await;

    let gens = srv.state.reader.generations(&spec, "1m").unwrap();
    assert_eq!(gens.len(), 1);
    assert!(
        gens[0]
            .parquet_path
            .file_name()
            .unwrap()
            .to_str()
            .unwrap()
            .starts_with("partition-")
    );

    srv.shutdown().await;
}

#[tokio::test]
async fn cross_module_2_mark_price_alert_freezes_evidence_before_and_after_bar_close() {
    let tmp = TempDir::new().unwrap();
    let replay_path = tmp.path().join("replay.jsonl");
    fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    let clock = Arc::new(AtomicI64::new(BASE_TS + 10_000));
    let clock_c = Arc::clone(&clock);
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.now_ms = Some(Arc::new(move || clock_c.load(Ordering::SeqCst)));
    let srv = start_server(cfg).await.unwrap();

    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());
    let alert: AlertRecord = client
        .post(format!("{}/api/alerts", srv.base_url()))
        .header("Authorization", &auth)
        .json(&AlertCreate {
            instrument_id: BTC.to_owned(),
            timeframe: "1m".to_owned(),
            condition: Rule {
                version: RuleVersion::V1,
                timing: RuleTiming::Event,
                expression: RuleExpression::Compare {
                    test: RuleComparisonTest::Gte,
                    left: Box::new(RuleOperand::Event {
                        metric: AlertMetric::MarkPrice,
                    }),
                    right: Box::new(RuleOperand::Constant {
                        value: dec("100"),
                    }),
                },
            },
            note: "mark >= 100".to_owned(),
            draft: false,
            repeat: true,
            cooldown_ms: 0,
            expires_at: None,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();

    let writer = srv.state.cold_writer.as_ref().unwrap();
    // 1. Before any closed bar: baseline 95, then cross at 105.
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("95")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS + 10_000,
        ts_ingested: BASE_TS + 10_000,
    }));
    clock.store(BASE_TS + 20_000, Ordering::SeqCst);
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("105")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS + 20_000,
        ts_ingested: BASE_TS + 20_000,
    }));

    // 2. Closed 1m bar arrives, then drop below 100 and cross again at 110 (before 5s bulk flush!).
    clock.store(BASE_TS + MINUTE, Ordering::SeqCst);
    writer.on_event(&FeedEvent::Kline(KlineEvent {
        instrument_id: BTC.to_owned(),
        timeframe: "1m".to_owned(),
        bar: CompactBar {
            timestamp: BASE_TS,
            open: Some(dec("95")),
            high: Some(dec("105")),
            low: Some(dec("94")),
            close: Some(dec("102")),
            volume: Some(dec("10")),
            turnover: Some(dec("1020")),
            ts_available: BASE_TS + MINUTE,
            precision: PrecisionTier::SecondVerified,
            status: BarStatus::Normal,
        },
        confirmed: true,
        revision: 0,
    }));
    clock.store(BASE_TS + MINUTE + 5_000, Ordering::SeqCst);
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("99")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS + MINUTE + 5_000,
        ts_ingested: BASE_TS + MINUTE + 5_000,
    }));
    clock.store(BASE_TS + MINUTE + 10_000, Ordering::SeqCst);
    writer.on_event(&FeedEvent::DerivativeTick(DerivativeTickEvent {
        instrument_id: BTC.to_owned(),
        mark_price: Some(dec("110")),
        funding_rate: None,
        next_funding_ts: None,
        open_interest: None,
        ts_event: BASE_TS + MINUTE + 10_000,
        ts_ingested: BASE_TS + MINUTE + 10_000,
    }));

    // Wait briefly for async on_trigger state_store persistence.
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let mut triggers: Vec<AlertTriggerRecord> = Vec::new();
    while std::time::Instant::now() < deadline {
        let resp: Value = client
            .get(format!(
                "{}/api/alerts/triggers?alert_id={}",
                srv.base_url(),
                alert.alert_id
            ))
            .send()
            .await
            .unwrap()
            .error_for_status()
            .unwrap()
            .json()
            .await
            .unwrap();
        triggers = serde_json::from_value(resp["triggers"].clone()).unwrap();
        if triggers.len() >= 2 {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    assert_eq!(triggers.len(), 2);
    let trg_105 = triggers.iter().find(|t| t.observed == Some(dec("105"))).unwrap();
    let trg_110 = triggers.iter().find(|t| t.observed == Some(dec("110"))).unwrap();
    assert!(!trg_105.snapshot_id.is_empty());
    assert!(!trg_110.snapshot_id.is_empty());

    let d105: Value = client
        .get(format!("{}/api/snapshots/{}/derivatives?timeframe=1m", srv.base_url(), trg_105.snapshot_id))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        d105["summary"]["mark_price"]
            .as_str()
            .unwrap()
            .parse::<Decimal>()
            .unwrap(),
        dec("105")
    );

    let d110: Value = client
        .get(format!("{}/api/snapshots/{}/derivatives?timeframe=1m", srv.base_url(), trg_110.snapshot_id))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        d110["summary"]["mark_price"]
            .as_str()
            .unwrap()
            .parse::<Decimal>()
            .unwrap(),
        dec("110")
    );

    srv.shutdown().await;
}
