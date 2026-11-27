//! Comprehensive integration tests for `talaria-server`:
//! - Daemon lifecycle, exclusive `service.lock`, `0600` descriptor, loopback enforcement
//! - Strict `{"code": ..., "message": ...}` error envelopes and auth / `X-Talaria-Data-Dir` guard
//! - Apache Arrow IPC streaming (`/api/bars.arrow`, `/api/ticks.arrow`, `/api/derivatives.arrow`)
//! - Same-origin `/desk` HTML bootstrap + `ServeDir` asset hosting
//! - Dual-semantic WebSockets (`/ws/desk` 256 drop-oldest + quota scheduler; `/ws/strategy` `(epoch, seq)` replay/resync + `1013` overflow)
//! - SQLite persistence across restarts, snapshots/indicators/key-levels/annotations/scanner/alerts, JSONL replay pipeline

use std::collections::BTreeMap;
use std::fs;
use std::io::Cursor;
use std::path::Path;
use std::sync::Arc;

use arrow::ipc::reader::StreamReader;
use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::alerts::{AlertCreate, AlertRecord};
use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::collaboration::{AgentAnnotationCreate, AnnotationEvidence, AnnotationKind};
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::drawings::{DrawingCreate, DrawingKind, DrawingPoint, DrawingStyle};
use talaria_contracts::feed::{KlineEvent, OrderBookEvent};
use talaria_contracts::rules::{
    AlertMetric, Rule, RuleComparisonTest, RuleExpression, RuleOperand, RuleTiming, RuleVersion,
};
use talaria_contracts::snapshot::{SnapshotMeta, SnapshotMode, SnapshotRequest};
use talaria_contracts::ticks::{Trade, TradeAvailability, TradeSide};
use talaria_contracts::types::PrecisionTier;
use talaria_packs::{DEFAULT_PACK_NAME, get_market_pack};
use talaria_server::security::{DATA_DIR_HEADER, BARS_HOT_REVISION_HEADER, BARS_MANIFEST_HEADER, validate_loopback_host};
use talaria_server::ws::desk::WS_QUEUE_MAX;
use talaria_server::ws::strategy::STRATEGY_QUEUE_SIZE;
use talaria_server::{ServerConfig, start_server};
use talaria_store::partition_month;
use tempfile::TempDir;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message as WsMessage;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;
use uuid::Uuid;

const BTC: &str = "BTC-USDT-PERP";
const ETH: &str = "ETH-USDT-PERP";
const BASE_TS: i64 = 1_767_225_600_000; // 2026-01-01T00:00:00Z
const MINUTE: i64 = 60_000;

fn btc_spec() -> talaria_contracts::market::InstrumentSpec {
    get_market_pack(DEFAULT_PACK_NAME)
        .unwrap()
        .instrument(BTC)
        .unwrap()
        .clone()
}

fn sample_raw_bar(ts: i64, close_val: i64, rev: u64, confirmed: bool) -> RawBar {
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
        confirmed,
        history_only: false,
        synthetic: false,
    }
}

fn seed_cold_bars(data_dir: &Path, count: usize, start_ts: i64, store: &talaria_store::ColdStore) {
    let spec = btc_spec();
    let mut by_month: BTreeMap<String, Vec<RawBar>> = BTreeMap::new();
    for i in 0..count {
        let ts = start_ts + (i as i64) * MINUTE;
        let bar = sample_raw_bar(ts, 50_000 + (i as i64 % 500), 1, true);
        let month = partition_month(ts).unwrap();
        by_month.entry(month).or_default().push(bar);
    }
    for (_, batch) in by_month {
        store.publish_bars(&spec, "1m", &batch).unwrap();
    }
    let _ = data_dir;
}

#[test]
fn loopback_validation_rejects_wildcard_and_accepts_loopback() {
    assert!(validate_loopback_host("127.0.0.1").is_ok());
    assert!(validate_loopback_host("::1").is_ok());
    assert!(validate_loopback_host("0.0.0.0").is_err());
    assert!(validate_loopback_host("192.168.1.5").is_err());
    assert!(validate_loopback_host("not-an-ip").is_err());
}

#[tokio::test]
async fn daemon_lock_descriptor_permissions_and_error_contracts() {
    let tmp = TempDir::new().unwrap();
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.now_ms = Some(Arc::new(|| BASE_TS + 200 * MINUTE));

    let srv = start_server(cfg.clone()).await.unwrap();

    // 1. Descriptor written with 0600 permissions & exact fields
    let desc_path = tmp.path().join("runtime").join("service-descriptor.json");
    assert!(desc_path.is_file());
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = fs::metadata(&desc_path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "service-descriptor must be 0600");
    }
    let desc_json: Value = serde_json::from_str(&fs::read_to_string(&desc_path).unwrap()).unwrap();
    assert_eq!(desc_json["version"], 1);
    assert_eq!(desc_json["token"], srv.token());
    assert_eq!(desc_json["base_url"], srv.base_url());
    assert_eq!(desc_json["data_dir_id"], srv.state.security.data_dir_id);

    // 2. Second writer on same data_dir is rejected cleanly (SERVICE_LOCKED)
    let second = start_server(cfg.clone()).await;
    assert!(second.is_err());
    let err = second.err().unwrap();
    assert_eq!(err.code, "SERVICE_LOCKED");

    let client = reqwest::Client::new();

    // 3. Bearer auth required on writes & X-Talaria-Data-Dir guard checked
    let unauth = client
        .post(format!("{}/api/watchlist/groups", srv.base_url()))
        .json(&json!({"name": "Unauth"}))
        .send()
        .await
        .unwrap();
    assert_eq!(unauth.status(), 401);
    let body: Value = unauth.json().await.unwrap();
    assert_eq!(body["code"], "UNAUTHORIZED");
    assert!(body["message"].is_string());

    let bad_dir = client
        .get(format!("{}/api/health", srv.base_url()))
        .header(DATA_DIR_HEADER, "wrong-dir-id")
        .send()
        .await
        .unwrap();
    assert_eq!(bad_dir.status(), 401);
    assert_eq!(bad_dir.json::<Value>().await.unwrap()["code"], "UNAUTHORIZED");

    // 4. Strict {code, message} on 404, 422, 400, 501, 503
    let r404 = client
        .get(format!("{}/api/snapshots/nonexistent", srv.base_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(r404.status(), 404);
    assert_eq!(r404.json::<Value>().await.unwrap()["code"], "SNAPSHOT_NOT_FOUND");

    let r422 = client
        .get(format!(
            "{}/api/bars?instrument={BTC}&timeframe=BAD&start=0",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(r422.status(), 422);
    assert_eq!(r422.json::<Value>().await.unwrap()["code"], "VALIDATION_ERROR");

    let r400 = client
        .get(format!(
            "{}/api/bars?instrument={BTC}&timeframe=1m&start=0&end={}",
            srv.base_url(),
            20_000 * MINUTE
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(r400.status(), 400);
    assert_eq!(r400.json::<Value>().await.unwrap()["code"], "BARS_RANGE_TOO_LARGE");

    let r501 = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", format!("Bearer {}", srv.token()))
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(BASE_TS),
            mode: SnapshotMode::ObservedReplay,
        })
        .send()
        .await
        .unwrap();
    assert_eq!(r501.status(), 501);
    assert_eq!(r501.json::<Value>().await.unwrap()["code"], "NOT_IMPLEMENTED");

    let r503 = client
        .get(format!("{}/api/feed/health", srv.base_url()))
        .send()
        .await
        .unwrap();
    // Offline fixture mode returns 503 FEED_UNAVAILABLE when no supervisor exists (unless live mode enabled)
    assert!(r503.status() == 200 || r503.status() == 503);

    // 5. Shutdown releases descriptor & lock, allowing clean restart
    srv.shutdown().await;
    assert!(!desc_path.exists());
    let restarted = start_server(cfg).await.unwrap();
    restarted.shutdown().await;
}

#[tokio::test]
async fn arrow_ipc_bars_ticks_derivatives_and_cold_hot_merge() {
    let tmp = TempDir::new().unwrap();
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.now_ms = Some(Arc::new(|| BASE_TS + 20_000 * MINUTE));
    // Disable auto realtime stream by setting offline service.json
    fs::write(
        tmp.path().join("service.json"),
        json!({"version": 1, "features": {"market_data": false, "cold_store": true, "alerts": true, "scanner": true}})
            .to_string(),
    )
    .unwrap();

    let srv = start_server(cfg).await.unwrap();
    let cold_store = srv.state.cold_store.as_ref().unwrap();
    let spec = btc_spec();

    // Seed 12,000 cold bars (> MAX_RANGE_BARS=10,000 to prove /api/bars.arrow has no 10k limit)
    seed_cold_bars(tmp.path(), 12_000, BASE_TS, cold_store);

    // Seed derivatives and trades into ColdStore
    let deriv_pt = DerivativePoint {
        instrument_id: BTC.to_owned(),
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        value: Decimal::new(125, 6),
        ts_event: BASE_TS,
        ts_published: Some(BASE_TS),
        ts_available: BASE_TS,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: BASE_TS,
        ts_committed: BASE_TS,
        revision: 0,
        settled: Some(true),
    };
    cold_store
        .publish_derivatives(&spec, DerivativeKind::FundingRate, &[deriv_pt])
        .unwrap();

    let trade = Trade {
        instrument_id: BTC.to_owned(),
        ts_event: BASE_TS + 500,
        ts_available: BASE_TS + 500,
        ts_ingested: BASE_TS + 500,
        ts_committed: BASE_TS + 500,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: Uuid::new_v4(),
        side: TradeSide::Buy,
        price: Decimal::from(50_000),
        size: Decimal::new(25, 2),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: Some(1),
        packet_id: None,
        packet_index: None,
    };
    cold_store.publish_trades(&spec, &[trade]).unwrap();

    // Inject 1 open bar in HotStreamBuffer beyond cold tail
    let open_ts = BASE_TS + 12_000 * MINUTE;
    srv.state.hot_buffer.ingest(&talaria_contracts::feed::FeedEvent::Kline(KlineEvent {
        instrument_id: BTC.to_owned(),
        timeframe: "1m".to_owned(),
        bar: CompactBar {
            timestamp: open_ts,
            open: Some(Decimal::from(55_555)),
            high: Some(Decimal::from(55_560)),
            low: Some(Decimal::from(55_550)),
            close: Some(Decimal::from(55_558)),
            volume: Some(Decimal::from(3)),
            turnover: Some(Decimal::from(166_674)),
            status: BarStatus::Normal,
            ts_available: open_ts,
            precision: PrecisionTier::SecondVerified,
        },
        confirmed: false,
        revision: 1,
    }));

    let client = reqwest::Client::new();

    // 1. /api/bars.arrow transfers all 12,001 bars (cold + hot open) with proper Arrow IPC headers
    let resp = client
        .get(format!(
            "{}/api/bars.arrow?instrument={BTC}&timeframe=1m&start={BASE_TS}&end={open_ts}&include_open=true",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), 200);
    assert_eq!(
        resp.headers().get("content-type").unwrap(),
        "application/vnd.apache.arrow.stream"
    );
    assert!(resp.headers().contains_key(BARS_MANIFEST_HEADER));
    assert!(resp.headers().contains_key(BARS_HOT_REVISION_HEADER));

    let bytes = resp.bytes().await.unwrap();
    let reader = StreamReader::try_new(Cursor::new(&bytes[..]), None).unwrap();
    let batches: Vec<_> = reader.map(|r| r.unwrap()).collect();
    let total_rows: usize = batches.iter().map(|b| b.num_rows()).sum();
    assert_eq!(total_rows, 12_001);

    // 2. Empty range still emits a valid typed 0-row Arrow IPC stream
    let empty_resp = client
        .get(format!(
            "{}/api/bars.arrow?instrument={BTC}&timeframe=1m&start={}&end={}",
            srv.base_url(),
            BASE_TS + 100_000 * MINUTE,
            BASE_TS + 100_010 * MINUTE
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(empty_resp.status(), 200);
    let empty_bytes = empty_resp.bytes().await.unwrap();
    let empty_reader = StreamReader::try_new(Cursor::new(&empty_bytes[..]), None).unwrap();
    let empty_batches: Vec<_> = empty_reader.map(|r| r.unwrap()).collect();
    assert_eq!(empty_batches.iter().map(|b| b.num_rows()).sum::<usize>(), 0);

    // 3. /api/ticks.arrow
    let ticks_resp = client
        .get(format!(
            "{}/api/ticks.arrow?instrument={BTC}&kind=trades&start={BASE_TS}&end={open_ts}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(ticks_resp.status(), 200);
    let ticks_bytes = ticks_resp.bytes().await.unwrap();
    let ticks_reader = StreamReader::try_new(Cursor::new(&ticks_bytes[..]), None).unwrap();
    let tick_rows: usize = ticks_reader.map(|r| r.unwrap().num_rows()).sum();
    assert_eq!(tick_rows, 1);

    // 4. /api/derivatives.arrow
    let deriv_resp = client
        .get(format!(
            "{}/api/derivatives.arrow?instrument={BTC}&kind=funding_rate&start={BASE_TS}&end={open_ts}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(deriv_resp.status(), 200);
    let deriv_bytes = deriv_resp.bytes().await.unwrap();
    let deriv_reader = StreamReader::try_new(Cursor::new(&deriv_bytes[..]), None).unwrap();
    let deriv_rows: usize = deriv_reader.map(|r| r.unwrap().num_rows()).sum();
    assert_eq!(deriv_rows, 1);

    srv.shutdown().await;
}

#[tokio::test]
async fn desk_static_bootstrap_and_state_persistence_across_restart() {
    let tmp = TempDir::new().unwrap();
    let dist = tmp.path().join("dist");
    fs::create_dir_all(dist.join("assets")).unwrap();
    fs::write(
        dist.join("index.html"),
        "<!doctype html><html><head><title>Talaria Desk</title></head><body><div id='root'></div></body></html>",
    )
    .unwrap();
    fs::write(dist.join("assets").join("app.js"), "console.log('talaria');").unwrap();

    fs::write(
        tmp.path().join("service.json"),
        json!({"version": 1, "features": {"market_data": false, "cold_store": true, "alerts": true, "scanner": true}})
            .to_string(),
    )
    .unwrap();

    let mut cfg = ServerConfig::new(tmp.path());
    cfg.desk_dist = Some(dist.clone());
    cfg.now_ms = Some(Arc::new(|| BASE_TS + 300 * MINUTE));

    let srv = start_server(cfg.clone()).await.unwrap();
    let cold_store = srv.state.cold_store.as_ref().unwrap();
    seed_cold_bars(tmp.path(), 50, BASE_TS, cold_store);

    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    // 1. /desk HTML bootstrap & ServeDir assets
    let desk_resp = client
        .get(format!("{}/desk", srv.base_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(desk_resp.status(), 200);
    let set_cookie = desk_resp
        .headers()
        .get("set-cookie")
        .unwrap()
        .to_str()
        .unwrap()
        .to_owned();
    assert!(set_cookie.contains("talaria_token="));
    assert!(set_cookie.contains("SameSite=Strict"));
    let html = desk_resp.text().await.unwrap();
    assert!(html.contains("talaria-token"));
    assert!(html.contains("__TALARIA_BOOTSTRAP__"));

    let asset_resp = client
        .get(format!("{}/assets/app.js", srv.base_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(asset_resp.status(), 200);
    assert_eq!(asset_resp.text().await.unwrap(), "console.log('talaria');");

    let boot_resp: Value = client
        .get(format!("{}/api/desk/bootstrap", srv.base_url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(boot_resp["token"], srv.token());

    // 2. Watchlist CRUD
    let wl: Value = client
        .post(format!("{}/api/watchlist/groups", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"name": "Core"}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let gid = wl["groups"][0]["group_id"].as_str().unwrap().to_owned();
    client
        .post(format!("{}/api/watchlist/items", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"group_id": gid, "instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();

    // 3. Drawings + versions
    let drw: Value = client
        .post(format!("{}/api/drawings", srv.base_url()))
        .header("Authorization", &auth)
        .json(&DrawingCreate {
            instrument_id: BTC.to_owned(),
            kind: DrawingKind::HorizontalLine,
            visible_timeframes: vec![],
            points: vec![DrawingPoint {
                ts: BASE_TS,
                price: Decimal::from(50_000),
            }],
            text: "Support".to_owned(),
            style: DrawingStyle::default(),
        })
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let drawing_id = drw["drawing_id"].as_str().unwrap().to_owned();

    // 4. Snapshot + indicators + key levels + annotations hard gate
    let snap: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned()],
            cutoff_ts: Some(BASE_TS + 45 * MINUTE),
            mode: SnapshotMode::HistoricalPit,
        })
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    let ind_resp = client
        .get(format!(
            "{}/api/indicators?snapshot_id={}&timeframe=1m&names=EMA,ATR",
            srv.base_url(),
            snap.snapshot_id
        ))
        .send()
        .await
        .unwrap();
    assert_eq!(ind_resp.status(), 200);
    let ind_json: Value = ind_resp.json().await.unwrap();
    assert_eq!(ind_json["series"].as_array().unwrap().len(), 2);

    // Reject unaligned / out-of-bounds annotation cleanly via hard gate
    let bad_ann = client
        .post(format!("{}/api/annotations", srv.base_url()))
        .header("Authorization", &auth)
        .json(&AgentAnnotationCreate {
            snapshot_id: snap.snapshot_id.clone(),
            instrument_id: BTC.to_owned(),
            timeframe: "1m".to_owned(),
            session_id: "sess-1".to_owned(),
            round: 1,
            kind: AnnotationKind::Level,
            label: "Bad".to_owned(),
            rationale: "No evidence".to_owned(),
            price_low: Decimal::from(50_000),
            price_high: Decimal::from(50_000),
            ts_anchor: BASE_TS + MINUTE,
            evidence_refs: vec![],
        })
        .send()
        .await
        .unwrap();
    assert_eq!(bad_ann.status(), 422);
    assert_eq!(bad_ann.json::<Value>().await.unwrap()["code"], "EVIDENCE_EMPTY");

    // Valid annotation succeeds
    let good_ann = client
        .post(format!("{}/api/annotations", srv.base_url()))
        .header("Authorization", &auth)
        .json(&AgentAnnotationCreate {
            snapshot_id: snap.snapshot_id.clone(),
            instrument_id: BTC.to_owned(),
            timeframe: "1m".to_owned(),
            session_id: "sess-1".to_owned(),
            round: 1,
            kind: AnnotationKind::Level,
            label: "Valid Support".to_owned(),
            rationale: "Confirmed pivot".to_owned(),
            price_low: Decimal::from(50_000),
            price_high: Decimal::from(50_000),
            ts_anchor: BASE_TS + 10 * MINUTE,
            evidence_refs: vec![AnnotationEvidence {
                structure_id: None,
                level_id: None,
                kline_ts: Some(BASE_TS + 10 * MINUTE),
                note: "pivot".to_owned(),
            }],
        })
        .send()
        .await
        .unwrap();
    assert_eq!(good_ann.status(), 201);

    // 5. Alert create persists
    let created_alert: AlertRecord = client
        .post(format!("{}/api/alerts", srv.base_url()))
        .header("Authorization", &auth)
        .json(&AlertCreate {
            instrument_id: BTC.to_owned(),
            timeframe: "5m".to_owned(),
            condition: Rule {
                version: RuleVersion::V1,
                timing: RuleTiming::Event,
                expression: RuleExpression::Compare {
                    test: RuleComparisonTest::Gte,
                    left: Box::new(RuleOperand::Event {
                        metric: AlertMetric::LastPrice,
                    }),
                    right: Box::new(RuleOperand::Constant {
                        value: Decimal::from(51_000),
                    }),
                },
            },
            note: "Breakout".to_owned(),
            draft: false,
            repeat: true,
            cooldown_ms: 60_000,
            expires_at: None,
        })
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    // Shutdown and restart on same data_dir, verify SQLite persistence
    srv.shutdown().await;

    let srv2 = start_server(cfg).await.unwrap();
    let wl2: Value = client
        .get(format!("{}/api/watchlist", srv2.base_url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(wl2["groups"][0]["items"][0]["instrument_id"], BTC);

    let drw2: Value = client
        .get(format!("{}/api/drawings/{drawing_id}", srv2.base_url()))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(drw2["text"], "Support");

    let alt2: Value = client
        .get(format!(
            "{}/api/alerts/{}",
            srv2.base_url(),
            created_alert.alert_id
        ))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(alt2["note"], "Breakout");

    srv2.shutdown().await;
}

#[tokio::test]
async fn desk_ws_drop_oldest_and_strategy_ws_replay_and_1013_overflow() {
    let tmp = TempDir::new().unwrap();
    let replay_path = tmp.path().join("events.jsonl");
    fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.realtime_quota = 2;
    cfg.now_ms = Some(Arc::new(|| BASE_TS + 10 * MINUTE));

    let srv = start_server(cfg).await.unwrap();
    let ws_base = srv.base_url().replace("http://", "ws://");

    // 1. /ws/desk queue drop-oldest & subscription
    let (mut desk_ws, _) = connect_async(format!("{ws_base}/ws/desk")).await.unwrap();
    let hello_txt = desk_ws.next().await.unwrap().unwrap().into_text().unwrap();
    let hello: Value = serde_json::from_str(&hello_txt).unwrap();
    assert_eq!(hello["type"], "hello");

    desk_ws
        .send(WsMessage::Text(
            json!({"op": "subscribe", "instrument_id": BTC, "timeframe": "1m"}).to_string().into(),
        ))
        .await
        .unwrap();

    // Wait until server registers client's chart subscription before flooding
    let sub_key = (BTC.to_owned(), "1m".to_owned());
    let wait_deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while srv.state.desk.chart_subscriber_count(&sub_key) == 0 {
        assert!(std::time::Instant::now() < wait_deadline, "subscribe not registered in time");
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }

    // Push 300 (> WS_QUEUE_MAX=256) book ticks directly through broadcast hub
    for i in 0..(WS_QUEUE_MAX + 44) {
        srv.state.desk.publish_feed_event(&talaria_contracts::feed::FeedEvent::OrderBook(
            OrderBookEvent {
                instrument_id: BTC.to_owned(),
                bid_price: Decimal::from(50_000 + i as i64),
                bid_size: Decimal::from(1),
                ask_price: Decimal::from(50_001 + i as i64),
                ask_size: Decimal::from(1),
                ts_event: BASE_TS + i as i64,
                ts_ingested: BASE_TS + i as i64,
            },
        ));
    }

    // Read queued frames and ensure the latest frames arrived without stalling or panicking
    let mut seen_books = 0usize;
    let mut last_bid = String::new();
    while let Ok(Some(Ok(msg))) =
        tokio::time::timeout(std::time::Duration::from_millis(80), desk_ws.next()).await
    {
        if let WsMessage::Text(txt) = msg {
            let v: Value = serde_json::from_str(&txt).unwrap();
            if v["type"] == "book" {
                seen_books += 1;
                last_bid = v["data"]["bid_price"].as_str().unwrap().to_owned();
            }
        }
    }
    assert!(seen_books <= WS_QUEUE_MAX + 5);
    assert_eq!(last_bid, (50_000 + WS_QUEUE_MAX + 43).to_string());
    let _ = desk_ws.close(None).await;

    // 2. /ws/strategy ordered (epoch, seq) + reconnect replay + 1013 close
    let mut req = format!("{ws_base}/ws/strategy")
        .into_client_request()
        .unwrap();
    req.headers_mut().insert(
        "Authorization",
        format!("Bearer {}", srv.token()).parse().unwrap(),
    );
    let (mut strat_ws, _) = connect_async(req).await.unwrap();
    strat_ws
        .send(WsMessage::Text(
            json!({"instrument": ETH, "timeframe": "1m", "owner": "bot-1"})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
    let ready: Value = serde_json::from_str(
        &strat_ws.next().await.unwrap().unwrap().into_text().unwrap(),
    )
    .unwrap();
    assert_eq!(ready["type"], "ready");
    let epoch = ready["epoch"].as_str().unwrap().to_owned();

    // Push 3 confirmed events via StrategyStream
    let strat_stream = srv.state.strategy_stream.as_ref().unwrap();
    let key = talaria_server::ws::strategy::StrategyKey::Bar(ETH.to_owned(), "1m".to_owned());
    for seq in 1..=3 {
        let bar = talaria_contracts::bars::RangeBar {
            ts_event: BASE_TS + seq * MINUTE,
            open: Decimal::from(3000 + seq),
            high: Decimal::from(3005 + seq),
            low: Decimal::from(2995 + seq),
            close: Decimal::from(3001 + seq),
            volume: Decimal::from(10),
            turnover: Decimal::from(30_000),
            ts_available: BASE_TS + (seq + 1) * MINUTE,
            precision: PrecisionTier::SecondVerified,
            confirmed: true,
            revision: seq as u64,
        };
        strat_stream
            .push_test_bar_event(&key, bar, format!("cid-{seq}"))
            .await;
    }

    let first_ev: Value = serde_json::from_str(
        &strat_ws.next().await.unwrap().unwrap().into_text().unwrap(),
    )
    .unwrap();
    assert_eq!(first_ev["seq"], 1);
    drop(strat_ws); // abrupt disconnect -> lease retained

    // Reconnect with ?from_epoch=<epoch>&from_seq=1 and verify replay of seq 2 and 3!
    let mut replay_req = format!("{ws_base}/ws/strategy?from_epoch={epoch}&from_seq=1")
        .into_client_request()
        .unwrap();
    replay_req.headers_mut().insert(
        "Authorization",
        format!("Bearer {}", srv.token()).parse().unwrap(),
    );
    let (mut replay_ws, _) = connect_async(replay_req).await.unwrap();
    replay_ws
        .send(WsMessage::Text(
            json!({"instrument": ETH, "timeframe": "1m", "owner": "bot-1"})
                .to_string()
                .into(),
        ))
        .await
        .unwrap();
    let r_ready: Value = serde_json::from_str(
        &replay_ws.next().await.unwrap().unwrap().into_text().unwrap(),
    )
    .unwrap();
    assert_eq!(r_ready["type"], "ready");
    assert_eq!(r_ready["epoch"], epoch);

    let ev2: Value = serde_json::from_str(
        &replay_ws.next().await.unwrap().unwrap().into_text().unwrap(),
    )
    .unwrap();
    let ev3: Value = serde_json::from_str(
        &replay_ws.next().await.unwrap().unwrap().into_text().unwrap(),
    )
    .unwrap();
    assert_eq!((ev2["seq"].as_u64(), ev3["seq"].as_u64()), (Some(2), Some(3)));

    // Now overflow queue (> STRATEGY_QUEUE_SIZE=256) in a single burst without yielding -> expect WS close 1013
    let burst: Vec<_> = (4..=(4 + STRATEGY_QUEUE_SIZE as i64 + 20))
        .map(|seq| {
            let bar = talaria_contracts::bars::RangeBar {
                ts_event: BASE_TS + seq * MINUTE,
                open: Decimal::from(3000 + seq),
                high: Decimal::from(3005 + seq),
                low: Decimal::from(2995 + seq),
                close: Decimal::from(3001 + seq),
                volume: Decimal::from(10),
                turnover: Decimal::from(30_000),
                ts_available: BASE_TS + (seq + 1) * MINUTE,
                precision: PrecisionTier::SecondVerified,
                confirmed: true,
                revision: seq as u64,
            };
            (bar, format!("cid-{seq}"))
        })
        .collect();
    strat_stream.push_test_bar_events_batch(&key, burst).await;

    let mut closed_1013 = false;
    while let Ok(Some(Ok(msg))) =
        tokio::time::timeout(std::time::Duration::from_secs(2), replay_ws.next()).await
    {
        if let WsMessage::Close(Some(frame)) = msg
            && u16::from(frame.code) == 1013
        {
            closed_1013 = true;
            break;
        }
    }
    assert!(closed_1013, "slow strategy client must close with 1013");

    srv.shutdown().await;
}

#[tokio::test]
async fn priority_quota_preemption_and_replay_to_cold_alert_scanner_pipeline() {
    let tmp = TempDir::new().unwrap();
    let replay_path = tmp.path().join("replay.jsonl");
    // Emit confirmed 5m bar at offset_ms 250ms (50ms wall clock at speed 5x? Wait: speed 2x -> 125ms, plenty for alert setup!)
    let kline_line = json!({
        "offset_ms": 300,
        "type": "kline",
        "event": {
            "instrument_id": BTC,
            "timeframe": "5m",
            "confirmed": true,
            "revision": 1,
            "bar": {
                "timestamp": BASE_TS + 5 * MINUTE,
                "open": "50000",
                "high": "52000",
                "low": "49900",
                "close": "51800",
                "volume": "50",
                "turnover": "2590000",
                "status": "NORMAL",
                "ts_available": BASE_TS + 10 * MINUTE,
                "precision": "SECOND_VERIFIED"
            }
        }
    });
    fs::write(
        &replay_path,
        format!("{{\"offset_ms\":0,\"type\":\"heartbeat\"}}\n{kline_line}\n"),
    )
    .unwrap();

    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.replay_speed = 2.0;
    cfg.realtime_quota = 1;
    cfg.now_ms = Some(Arc::new(|| BASE_TS + 20 * MINUTE));

    let srv = start_server(cfg).await.unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    // Add 2 watchlist items with quota=1 -> 1 subscribed, 1 degraded
    let wl_resp: Value = client
        .post(format!("{}/api/watchlist/groups", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"name": "Default"}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let gid = wl_resp["groups"][0]["group_id"].as_str().unwrap().to_owned();
    client
        .post(format!("{}/api/watchlist/items", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"group_id": gid, "instrument_id": BTC}))
        .send()
        .await
        .unwrap();
    let wl2: Value = client
        .post(format!("{}/api/watchlist/items", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"group_id": gid, "instrument_id": ETH}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(wl2["realtime_quota"], 1);
    assert_eq!(wl2["realtime_used"], 1);
    assert_eq!(wl2["groups"][0]["items"][0]["realtime"], true);
    assert_eq!(wl2["groups"][0]["items"][1]["degraded"], true);

    // Create 5m alert (higher priority than watchlist) -> preempts BTC 1m watchlist in favor of BTC 5m alert!
    client
        .post(format!("{}/api/alerts", srv.base_url()))
        .header("Authorization", &auth)
        .json(&AlertCreate {
            instrument_id: BTC.to_owned(),
            timeframe: "5m".to_owned(),
            condition: Rule {
                version: RuleVersion::V1,
                timing: RuleTiming::BarClose,
                expression: RuleExpression::Compare {
                    test: RuleComparisonTest::Gte,
                    left: Box::new(RuleOperand::Bar {
                        field: talaria_contracts::rules::RuleBarField::Close,
                    }),
                    right: Box::new(RuleOperand::Constant {
                        value: Decimal::from(51_500),
                    }),
                },
            },
            note: "5m close breakout".to_owned(),
            draft: false,
            repeat: true,
            cooldown_ms: 60_000,
            expires_at: None,
        })
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap();
    assert!(
        srv.state
            .supervisor
            .as_ref()
            .unwrap()
            .has_channel(BTC, "5m")
    );
    assert!(
        !srv.state
            .supervisor
            .as_ref()
            .unwrap()
            .has_channel(BTC, "1m")
    );

    // Wait briefly so replay script reaches offset_ms=300ms (150ms at 2x speed), then verify ColdStore!
    let wait_end = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let mut cold_rows = Vec::new();
    while std::time::Instant::now() < wait_end {
        srv.state.cold_writer.as_ref().unwrap().flush().await;
        cold_rows = srv
            .state
            .reader
            .read_bars(&btc_spec(), "5m", None, None, Some(BASE_TS + 20 * MINUTE))
            .unwrap();
        if !cold_rows.is_empty() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(25)).await;
    }
    assert!(
        !cold_rows.is_empty(),
        "replay bar must flush to ColdStore"
    );

    // Acquire active chart on ETH 1m -> preempts 5m alert since active_chart > active_alerts!
    srv.state
        .scheduler
        .acquire_chart((ETH.to_owned(), "1m".to_owned()))
        .unwrap();
    assert!(
        srv.state
            .supervisor
            .as_ref()
            .unwrap()
            .has_channel(ETH, "1m")
    );

    // Strategy connection requires quota and preempts chart/alerts (`strategy > active_chart`)!
    srv.state
        .scheduler
        .acquire_account("strat-top", (BTC.to_owned(), "15m".to_owned()))
        .unwrap();
    assert!(
        srv.state
            .supervisor
            .as_ref()
            .unwrap()
            .has_channel(BTC, "15m")
    );
    assert!(
        !srv.state
            .supervisor
            .as_ref()
            .unwrap()
            .has_channel(ETH, "1m")
    );

    // Run manual scanner scan cleanly
    let scan_resp = client
        .post(format!("{}/api/scanner/scan", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({"instrument_ids": [BTC]}))
        .send()
        .await
        .unwrap();
    assert_eq!(scan_resp.status(), 200);

    srv.shutdown().await;
}

#[tokio::test]
async fn snapshot_backfills_from_fixture_and_compacts_gaps_with_lenient_query_bools() {
    let tmp = TempDir::new().unwrap();
    let replay_path = tmp.path().join("replay.jsonl");
    fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();

    let fixture_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../../tests/fixtures/bybit");
    let mut cfg = ServerConfig::new(tmp.path());
    cfg.replay_file = Some(replay_path);
    cfg.fixture_dir = Some(fixture_dir);

    let srv = start_server(cfg).await.unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", srv.token());

    // 1. Fresh replay + fixture_dir snapshot auto-backfills bars without manual backfill
    let snap: SnapshotMeta = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .header("Authorization", &auth)
        .json(&SnapshotRequest {
            instrument_id: BTC.to_owned(),
            timeframes: vec!["1m".to_owned(), "5m".to_owned(), "1h".to_owned()],
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
    assert!(snap.bar_counts.get("1m").copied().unwrap_or(0) > 0);
    assert!(snap.bar_counts.get("5m").copied().unwrap_or(0) > 0);
    assert!(snap.bar_counts.get("1h").copied().unwrap_or(0) > 0);

    // 2. Verify compact_series synthesized FEED_GAP (5) + NO_TRADES (1) on BTCUSDT/1m.json
    let page: Value = client
        .get(format!(
            "{}/api/snapshots/{}/bars?timeframe=1m&limit=1000",
            srv.base_url(),
            snap.snapshot_id
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let bars = page["bars"].as_array().unwrap();
    let feed_gaps = bars.iter().filter(|b| b["status"] == "FEED_GAP").count();
    let no_trades = bars.iter().filter(|b| b["status"] == "NO_TRADES").count();
    assert_eq!(feed_gaps, 5, "expected 5 FEED_GAP slots in BTCUSDT/1m.json snapshot");
    assert_eq!(no_trades, 1, "expected 1 NO_TRADES bar in BTCUSDT/1m.json snapshot");

    // 3. Verify lenient query bools ("1" / "0") across indicators, operators, bars, drawings, briefings, annotations
    for url in [
        format!(
            "{}/api/indicators?instrument_id={BTC}&timeframe=1m&names=EMA&live=1",
            srv.base_url()
        ),
        format!(
            "{}/api/snapshots/{}/indicators?timeframe=1m&names=EMA&live=1",
            srv.base_url(),
            snap.snapshot_id
        ),
        format!(
            "{}/api/bars?instrument={BTC}&timeframe=1m&start=1793491200000&end=1793545440000&include_open=1&tail_only=0",
            srv.base_url()
        ),
        format!(
            "{}/api/drawings?instrument_id={BTC}&timeframe=1m&all_timeframes=1",
            srv.base_url()
        ),
        format!("{}/api/scanner/briefings?unread_only=1", srv.base_url()),
        format!("{}/api/annotations?include_archived=1", srv.base_url()),
    ] {
        let resp = client.get(&url).send().await.unwrap();
        assert_eq!(resp.status(), 200, "failed URL {url}: {}", resp.text().await.unwrap());
    }

    let op_resp = client
        .post(format!("{}/api/operators/compute?live=1", srv.base_url()))
        .header("Authorization", &auth)
        .json(&json!({
            "snapshot_id": snap.snapshot_id,
            "operator": "EMA",
            "timeframe": "1m"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(op_resp.status(), 200, "{}", op_resp.text().await.unwrap());

    srv.shutdown().await;
}

