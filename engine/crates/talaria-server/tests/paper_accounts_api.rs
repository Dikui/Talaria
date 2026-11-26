//! Ticket 03 / ADR-0025 integration tests for read-only paper account projections:
//! - `GET /api/paper/accounts`
//! - `GET /api/paper/accounts/{account}` (including >5s stale heartbeat & STOPPED -> OFFLINE)
//! - `GET /api/paper/barriers?instrument=` (active positions + PENDING_ENTRY bands)
//! - Proof that `talaria-data` NEVER opens `ledger.sqlite` or probes `owner.lock`
//! - `/ws/desk` `paper_account` broadcast via `paper_watcher`

use std::fs::{self, OpenOptions};
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use futures_util::StreamExt;
use reqwest::StatusCode;
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::collaboration::{
    ExitBarrierSpec, MarginMode, PaperAccountState, PaperAccountView, PaperFeedStatus, PaperFill,
    PaperFillKind, PaperProcessInfo, PaperProcessState, PaperRound, TradingState,
};
use talaria_contracts::market::Direction;
use talaria_server::{ServerConfig, start_server};
use tempfile::TempDir;
use tokio_tungstenite::connect_async;

const BTC: &str = "BTC-USDT-PERP";
const ETH: &str = "ETH-USDT-PERP";
const BASE_NOW: i64 = 1_767_225_600_000;

fn test_config(data_dir: &Path, clock: Arc<AtomicI64>) -> ServerConfig {
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
    cfg.now_ms = Some(Arc::new(move || clock.load(Ordering::SeqCst)));
    cfg
}

fn write_view(data_dir: &Path, view: &PaperAccountView) {
    let dir = data_dir.join("accounts").join(&view.sub_account_id);
    fs::create_dir_all(&dir).unwrap();
    let tmp = dir.join(".view.json.tmp");
    let target = dir.join("view.json");
    let payload = serde_json::to_vec(view).unwrap();
    fs::write(&tmp, payload).unwrap();
    fs::rename(&tmp, &target).unwrap();
}

fn sample_view(
    account: &str,
    ledger_seq: u64,
    heartbeat_ts: i64,
    status: PaperProcessState,
    stopped_ts: Option<i64>,
) -> PaperAccountView {
    let active_round = PaperRound {
        round_id: format!("r-open-{account}"),
        sub_account_id: account.to_owned(),
        instrument_id: BTC.to_owned(),
        direction: Direction::Long,
        quantity: Decimal::new(5, 1),
        stop: Decimal::from(49_500),
        take_profit: vec![Decimal::from(52_000)],
        phase: Some("ACTIVE_PROTECTED".to_owned()),
        barriers: Some(ExitBarrierSpec {
            stop: Decimal::from(49_500),
            take_profit: vec![Decimal::from(52_000)],
            max_holding_ms: 86_400_000,
            trailing_stop: None,
        }),
        entry_low: None,
        entry_high: None,
        entry_price: Some(Decimal::from(50_000)),
        mark_price: Some(Decimal::from(50_400)),
        unrealized_pnl: Some(Decimal::from(200)),
        opened_ts: Some(BASE_NOW - 60_000),
        closed_ts: None,
    };
    let pending_round = PaperRound {
        round_id: format!("r-pending-{account}"),
        sub_account_id: account.to_owned(),
        instrument_id: BTC.to_owned(),
        direction: Direction::Short,
        quantity: Decimal::ZERO,
        stop: Decimal::from(51_500),
        take_profit: vec![Decimal::from(48_500)],
        phase: Some("PENDING_ENTRY".to_owned()),
        barriers: None,
        entry_low: Some(Decimal::from(50_800)),
        entry_high: Some(Decimal::from(51_000)),
        entry_price: None,
        mark_price: None,
        unrealized_pnl: None,
        opened_ts: None,
        closed_ts: None,
    };
    let closed_round = PaperRound {
        round_id: format!("r-closed-{account}"),
        sub_account_id: account.to_owned(),
        instrument_id: BTC.to_owned(),
        direction: Direction::Long,
        quantity: Decimal::ZERO,
        stop: Decimal::from(48_000),
        take_profit: vec![Decimal::from(50_000)],
        phase: Some("CLOSED".to_owned()),
        barriers: None,
        entry_low: None,
        entry_high: None,
        entry_price: Some(Decimal::from(49_000)),
        mark_price: None,
        unrealized_pnl: None,
        opened_ts: Some(BASE_NOW - 180_000),
        closed_ts: Some(BASE_NOW - 120_000),
    };
    let other_inst_round = PaperRound {
        round_id: format!("r-eth-{account}"),
        sub_account_id: account.to_owned(),
        instrument_id: ETH.to_owned(),
        direction: Direction::Long,
        quantity: Decimal::from(1),
        stop: Decimal::from(2_900),
        take_profit: vec![Decimal::from(3_200)],
        phase: Some("ACTIVE_PROTECTED".to_owned()),
        barriers: None,
        entry_low: None,
        entry_high: None,
        entry_price: Some(Decimal::from(3_000)),
        mark_price: Some(Decimal::from(3_050)),
        unrealized_pnl: Some(Decimal::from(50)),
        opened_ts: Some(BASE_NOW - 30_000),
        closed_ts: None,
    };

    PaperAccountView {
        sub_account_id: account.to_owned(),
        positions: vec![active_round.clone(), other_inst_round.clone()],
        rounds: vec![active_round, pending_round, closed_round, other_inst_round],
        fills: vec![PaperFill {
            event_id: "event-10".to_owned(),
            round_id: format!("r-open-{account}"),
            instrument_id: BTC.to_owned(),
            kind: PaperFillKind::EntryFilled,
            ts_event: BASE_NOW - 60_000,
            quantity: Decimal::new(5, 1),
            price: Decimal::from(50_000),
            fee: Some(Decimal::from(15)),
            realized_pnl: None,
            reason: Some("TAKER".to_owned()),
        }],
        ledger_seq,
        account_state: Some(PaperAccountState {
            currency: "USDT".to_owned(),
            equity: Decimal::from(10_250),
            cash_available: Decimal::from(9_200),
            margin_used: Decimal::from(800),
            trading_state: TradingState::Active,
            trading_state_reason: None,
            intraday_drawdown_pct: Decimal::ZERO,
            peak_drawdown_pct: Decimal::ZERO,
            open_r: Decimal::new(12, 1),
            risk_reserved: Decimal::from(250),
            margin_mode: Some(MarginMode::Cross),
            leverage: Some(Decimal::from(5)),
            r_unit_pct: Some(Decimal::new(5, 3)),
            max_single_r_pct: Some(Decimal::new(1, 2)),
            friction_buffer_pct: Some(Decimal::new(2, 3)),
            liquidation_buffer_pct: Some(Decimal::ZERO),
            liquidation_buffer_ticks: Some(1),
        }),
        feed_status: Some(PaperFeedStatus {
            healthy: true,
            reduce_reason: None,
            service_warning: None,
            updated_ts: Some(BASE_NOW),
        }),
        process: Some(PaperProcessInfo {
            pid: 4242,
            scheme: "paper-main".to_owned(),
            started_ts: BASE_NOW - 300_000,
            heartbeat_ts,
            status,
            stopped_ts,
        }),
    }
}

#[tokio::test]
async fn paper_accounts_endpoints_read_only_projection_offline_detection_and_no_ledger_access() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(BASE_NOW));
    let server = start_server(test_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", server.token());

    // Empty when no projections exist yet.
    let list: Vec<String> = client
        .get(format!("{}/api/paper/accounts", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(list.is_empty());

    // Create two accounts: sa-live (fresh heartbeat) and sa-stopped (gracefully stopped).
    let live_view = sample_view("sa-live", 10, BASE_NOW - 1_000, PaperProcessState::Running, None);
    let stopped_view = sample_view(
        "sa-stopped",
        5,
        BASE_NOW - 500,
        PaperProcessState::Stopped,
        Some(BASE_NOW - 500),
    );
    write_view(dir.path(), &live_view);
    write_view(dir.path(), &stopped_view);

    // Proof server NEVER opens ledger.sqlite or probes owner.lock:
    // put corrupt/unreadable files AND rename ledger.sqlite away.
    let sa_live_dir = dir.path().join("accounts").join("sa-live");
    fs::write(sa_live_dir.join("ledger.sqlite.renamed"), b"not-a-sqlite-db").unwrap();
    let lock_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(false)
        .open(sa_live_dir.join("owner.lock"))
        .unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        // Put a corrupt, chmod 000 ledger.sqlite alongside view.json.
        let bad_ledger = sa_live_dir.join("ledger.sqlite");
        fs::write(&bad_ledger, b"corrupt-unreadable").unwrap();
        fs::set_permissions(&bad_ledger, fs::Permissions::from_mode(0o000)).unwrap();
    }

    let list: Vec<String> = client
        .get(format!("{}/api/paper/accounts", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list, vec!["sa-live".to_owned(), "sa-stopped".to_owned()]);

    // Fresh RUNNING account remains RUNNING.
    let fetched_live: PaperAccountView = client
        .get(format!("{}/api/paper/accounts/sa-live", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        fetched_live.process.as_ref().unwrap().status,
        PaperProcessState::Running
    );
    assert!(fetched_live.feed_status.as_ref().unwrap().healthy);

    // Gracefully STOPPED process reports OFFLINE while retaining stopped_ts.
    let fetched_stopped: PaperAccountView = client
        .get(format!("{}/api/paper/accounts/sa-stopped", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        fetched_stopped.process.as_ref().unwrap().status,
        PaperProcessState::Offline
    );
    assert_eq!(
        fetched_stopped.process.as_ref().unwrap().stopped_ts,
        Some(BASE_NOW - 500)
    );
    assert!(!fetched_stopped.feed_status.as_ref().unwrap().healthy);

    // Barriers aggregate active + PENDING_ENTRY rounds for BTC across accounts, excluding CLOSED & ETH.
    let barriers: Vec<PaperRound> = client
        .get(format!(
            "{}/api/paper/barriers?instrument={BTC}",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let round_ids: Vec<&str> = barriers.iter().map(|r| r.round_id.as_str()).collect();
    assert_eq!(barriers.len(), 4);
    assert!(round_ids.contains(&"r-open-sa-live"));
    assert!(round_ids.contains(&"r-pending-sa-live"));
    assert!(round_ids.contains(&"r-open-sa-stopped"));
    assert!(round_ids.contains(&"r-pending-sa-stopped"));
    assert!(!round_ids.iter().any(|id| id.contains("closed") || id.contains("eth")));

    // Advance server clock > 5s without refreshing heartbeat -> sa-live turns OFFLINE.
    clock.store(BASE_NOW + 5_500, Ordering::SeqCst);
    let fetched_stale: PaperAccountView = client
        .get(format!("{}/api/paper/accounts/sa-live", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        fetched_stale.process.as_ref().unwrap().status,
        PaperProcessState::Offline
    );
    assert!(!fetched_stale.feed_status.as_ref().unwrap().healthy);

    // Unknown account -> 404; invalid id -> 422.
    let resp_404 = client
        .get(format!("{}/api/paper/accounts/no-such-account", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap();
    assert_eq!(resp_404.status(), StatusCode::NOT_FOUND);

    let resp_422 = client
        .get(format!("{}/api/paper/accounts/BAD_ID!", server.base_url()))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap();
    assert_eq!(resp_422.status(), StatusCode::UNPROCESSABLE_ENTITY);

    drop(lock_file);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let bad_ledger = sa_live_dir.join("ledger.sqlite");
        let _ = fs::set_permissions(&bad_ledger, fs::Permissions::from_mode(0o600));
    }
    server.shutdown().await;
}

#[tokio::test]
async fn paper_watcher_broadcasts_paper_account_updates_and_offline_transition_over_ws_desk() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(BASE_NOW));
    let server = start_server(test_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();

    let ws_url = format!(
        "{}/ws/desk?token={}",
        server.base_url().replacen("http", "ws", 1),
        server.token()
    );
    let (mut ws, _) = connect_async(&ws_url).await.unwrap();

    // Consume initial hello frame.
    let hello_raw = tokio::time::timeout(Duration::from_secs(3), ws.next())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    let hello: Value = serde_json::from_str(&hello_raw.into_text().unwrap()).unwrap();
    assert_eq!(hello["type"], "hello");

    // 1. Write initial projection -> expect paper_account WS frame with RUNNING.
    let v1 = sample_view("sa-ws", 1, BASE_NOW, PaperProcessState::Running, None);
    write_view(dir.path(), &v1);

    let msg1 = recv_paper_account(&mut ws).await;
    assert_eq!(msg1.sub_account_id, "sa-ws");
    assert_eq!(msg1.ledger_seq, 1);
    assert_eq!(msg1.process.as_ref().unwrap().status, PaperProcessState::Running);

    // 2. Bump ledger_seq -> expect updated paper_account WS frame.
    let v2 = sample_view("sa-ws", 2, BASE_NOW, PaperProcessState::Running, None);
    write_view(dir.path(), &v2);

    let msg2 = recv_paper_account(&mut ws).await;
    assert_eq!(msg2.ledger_seq, 2);
    assert_eq!(msg2.process.as_ref().unwrap().status, PaperProcessState::Running);

    // 3. Advance clock > 5s without heartbeat update -> watcher broadcasts OFFLINE transition.
    clock.store(BASE_NOW + 6_000, Ordering::SeqCst);
    let msg3 = recv_paper_account(&mut ws).await;
    assert_eq!(msg3.sub_account_id, "sa-ws");
    assert_eq!(msg3.process.as_ref().unwrap().status, PaperProcessState::Offline);

    server.shutdown().await;
}

async fn recv_paper_account<S>(ws: &mut S) -> PaperAccountView
where
    S: futures_util::Stream<Item = Result<tokio_tungstenite::tungstenite::Message, tokio_tungstenite::tungstenite::Error>>
        + Unpin,
{
    let deadline = tokio::time::Instant::now() + Duration::from_secs(4);
    loop {
        let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
        let frame = tokio::time::timeout(remaining, ws.next())
            .await
            .expect("timed out waiting for paper_account ws frame")
            .expect("ws stream ended")
            .expect("ws read error");
        if let tokio_tungstenite::tungstenite::Message::Text(txt) = frame {
            let parsed: Value = serde_json::from_str(&txt).unwrap();
            if parsed["type"] == "paper_account" {
                return serde_json::from_value(parsed["data"].clone()).unwrap();
            }
        }
    }
}
