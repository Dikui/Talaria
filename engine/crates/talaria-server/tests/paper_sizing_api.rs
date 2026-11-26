//! Integration tests & p99 < 5ms latency gate for `POST /api/paper/sizing-preview` (Ticket 05).

use std::fs;
use std::path::Path;
use std::str::FromStr;
use std::sync::Arc;
use std::time::Instant;

use reqwest::StatusCode;
use rust_decimal::Decimal;
use serde_json::json;
use talaria_contracts::{
    Direction, MarginMode, PaperAccountState, PaperAccountView, PaperProcessInfo,
    PaperProcessState, SizingPreviewResponse, TradingState,
};
use talaria_server::{ServerConfig, start_server};
use tempfile::TempDir;

const BTC: &str = "BTC-USDT-PERP";
const NOW_TS: i64 = 1_795_022_974_103;

fn offline_config(data_dir: &Path, now_ms: i64) -> ServerConfig {
    fs::write(
        data_dir.join("service.json"),
        json!({
            "version": 1,
            "features": {
                "market_data": false,
                "cold_store": true,
                "alerts": false,
                "scanner": false
            }
        })
        .to_string(),
    )
    .unwrap();
    let mut cfg = ServerConfig::new(data_dir);
    cfg.now_ms = Some(Arc::new(move || now_ms));
    cfg
}

fn write_projection(data_dir: &Path, account: &str, heartbeat_ts: i64, status: PaperProcessState) {
    let dir = data_dir.join("accounts").join(account);
    fs::create_dir_all(&dir).unwrap();
    let view = PaperAccountView {
        sub_account_id: account.to_owned(),
        positions: vec![],
        rounds: vec![],
        fills: vec![],
        ledger_seq: 17,
        account_state: Some(PaperAccountState {
            currency: "USDT".to_owned(),
            equity: Decimal::from(10_000),
            cash_available: Decimal::from(10_000),
            margin_used: Decimal::ZERO,
            trading_state: TradingState::Active,
            trading_state_reason: None,
            intraday_drawdown_pct: Decimal::ZERO,
            peak_drawdown_pct: Decimal::ZERO,
            open_r: Decimal::ZERO,
            risk_reserved: Decimal::ZERO,
            margin_mode: Some(MarginMode::Isolated),
            leverage: Some(Decimal::from(5)),
            r_unit_pct: Some(Decimal::from_str("0.005").unwrap()),
            max_single_r_pct: Some(Decimal::from_str("0.01").unwrap()),
            friction_buffer_pct: Some(Decimal::from_str("0.002").unwrap()),
            liquidation_buffer_pct: Some(Decimal::ZERO),
            liquidation_buffer_ticks: Some(1),
        }),
        feed_status: None,
        process: Some(PaperProcessInfo {
            pid: 9999,
            scheme: "btc-breakout-paper".to_owned(),
            started_ts: NOW_TS - 60_000,
            heartbeat_ts,
            status,
            stopped_ts: None,
        }),
    };
    fs::write(dir.join("view.json"), serde_json::to_string_pretty(&view).unwrap()).unwrap();
}

fn client_with_token(token: &str) -> reqwest::Client {
    let mut headers = reqwest::header::HeaderMap::new();
    headers.insert(
        reqwest::header::AUTHORIZATION,
        format!("Bearer {token}").parse().unwrap(),
    );
    reqwest::Client::builder()
        .default_headers(headers)
        .build()
        .unwrap()
}

#[tokio::test]
async fn sizing_preview_online_offline_and_rejection_cases() {
    let dir = TempDir::new().unwrap();
    write_projection(dir.path(), "sa-live", NOW_TS - 500, PaperProcessState::Running);
    write_projection(dir.path(), "sa-offline", NOW_TS - 15_000, PaperProcessState::Running);

    let server = start_server(offline_config(dir.path(), NOW_TS)).await.unwrap();
    let client = client_with_token(server.token());
    let url = format!("{}/api/paper/sizing-preview", server.base_url());

    // 1. Online isolated preview
    let resp = client
        .post(&url)
        .json(&json!({
            "sub_account_id": "sa-live",
            "instrument_id": BTC,
            "direction": Direction::Long,
            "entry_low": "64500",
            "entry_high": "64600",
            "invalidation_stop": "64000",
            "take_profit": ["66000"]
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let body: SizingPreviewResponse = resp.json().await.unwrap();
    assert!(!body.from_offline_snapshot);
    assert_eq!(body.based_on_ledger_seq, Some(17));
    assert_eq!(body.rejected_reason, None);
    assert_eq!(body.quantity, Some(Decimal::from_str("0.068").unwrap()));
    assert!(body.est_liq_price.is_some());
    assert!(body.est_liq_distance.is_some());

    // 2. Offline account falls back to last projection and sets from_offline_snapshot = true
    let offline_body: SizingPreviewResponse = client
        .post(&url)
        .json(&json!({
            "sub_account_id": "sa-offline",
            "instrument_id": BTC,
            "direction": "LONG",
            "entry_low": "64500",
            "entry_high": "64600",
            "invalidation_stop": "64000"
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(offline_body.from_offline_snapshot);
    assert_eq!(offline_body.quantity, body.quantity);

    // 3. Invalid stop geometry returns 200 with rejected_reason
    let bad_geom: SizingPreviewResponse = client
        .post(&url)
        .json(&json!({
            "sub_account_id": "sa-live",
            "instrument_id": BTC,
            "direction": "LONG",
            "entry_low": "64500",
            "entry_high": "64600",
            "invalidation_stop": "64700"
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(bad_geom.rejected_reason.as_deref(), Some("INVALID_STOP_GEOMETRY"));
    assert!(bad_geom.quantity.is_none());

    // 4. Manual override exceeding max_single_r_pct returns EXCEEDS_MAX_SINGLE_R
    let too_big: SizingPreviewResponse = client
        .post(&url)
        .json(&json!({
            "sub_account_id": "sa-live",
            "instrument_id": BTC,
            "direction": "LONG",
            "entry_low": "64500",
            "entry_high": "64600",
            "invalidation_stop": "64000",
            "override_quantity": "0.2"
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(too_big.rejected_reason.as_deref(), Some("EXCEEDS_MAX_SINGLE_R"));
    assert_eq!(too_big.quantity, Some(Decimal::from_str("0.2").unwrap()));

    // 5. Missing account projection returns ACCOUNT_PROJECTION_UNAVAILABLE
    let missing: SizingPreviewResponse = client
        .post(&url)
        .json(&json!({
            "sub_account_id": "sa-missing",
            "instrument_id": BTC,
            "direction": "LONG",
            "entry_low": "64500",
            "entry_high": "64600",
            "invalidation_stop": "64000"
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        missing.rejected_reason.as_deref(),
        Some("ACCOUNT_PROJECTION_UNAVAILABLE")
    );

    // 6. Latency benchmark: 100 consecutive HTTP previews must achieve p99 < 5ms
    let req_payload = json!({
        "sub_account_id": "sa-live",
        "instrument_id": BTC,
        "direction": "LONG",
        "entry_low": "64500",
        "entry_high": "64600",
        "invalidation_stop": "64000",
        "take_profit": ["66000"]
    });
    // Warm up connection pool
    for _ in 0..5 {
        let r = client.post(&url).json(&req_payload).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::OK);
    }
    let mut latencies_us: Vec<u128> = Vec::with_capacity(100);
    for _ in 0..100 {
        let t0 = Instant::now();
        let r = client.post(&url).json(&req_payload).send().await.unwrap();
        assert_eq!(r.status(), StatusCode::OK);
        let _: SizingPreviewResponse = r.json().await.unwrap();
        latencies_us.push(t0.elapsed().as_micros());
    }
    latencies_us.sort_unstable();
    let p50_ms = latencies_us[50] as f64 / 1000.0;
    let p95_ms = latencies_us[95] as f64 / 1000.0;
    let p99_ms = latencies_us[99] as f64 / 1000.0;
    eprintln!(
        "POST /api/paper/sizing-preview latency (n=100): p50={p50_ms:.3}ms, p95={p95_ms:.3}ms, p99={p99_ms:.3}ms"
    );
    assert!(
        p99_ms < 5.0,
        "sizing preview p99 latency {p99_ms:.3}ms exceeded 5.0ms budget"
    );

    server.shutdown().await;
}
