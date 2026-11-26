//! Integration tests for Chain 3 Ticket 02: Rust `/api/paper/*` draft channel,
//! outbound queue, `DISCARDED` & lazy `EXPIRED`, `draft-evidence` (with `current_bar`),
//! and idempotent legacy migration from `state/collaboration_state.sqlite`.

use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use reqwest::StatusCode;
use rusqlite::{Connection, params};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::feed::{KlineEvent, ReceivedKlineEvent};
use talaria_contracts::types::PrecisionTier;
use talaria_contracts::{
    ConfirmedDraft, DraftConfirmation, DraftContent, DraftDiscardRequest, DraftEvidence,
    DraftReceipt, DraftRecord, DraftStatus,
};
use talaria_packs::{DEFAULT_PACK_NAME, get_market_pack};
use talaria_server::state_store::{
    LEGACY_COLLAB_DB, WorkbenchStateStore, draft_content_digest, paper_drafts_claim,
    paper_drafts_confirm, paper_drafts_list, paper_drafts_save,
};
use talaria_server::{ServerConfig, start_server};
use tempfile::TempDir;

const BTC: &str = "BTC-USDT-PERP";
const BASE_TS: i64 = 1_795_022_940_000; // aligned to 1m
const NOW_TS: i64 = 1_795_022_974_103; // inside minute BASE_TS

fn btc_spec() -> talaria_contracts::market::InstrumentSpec {
    get_market_pack(DEFAULT_PACK_NAME)
        .unwrap()
        .instrument(BTC)
        .unwrap()
        .clone()
}

fn raw_bar(ts: i64, open: i64, high: i64, low: i64, close: i64, confirmed: bool) -> RawBar {
    RawBar {
        symbol: "BTCUSDT".to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: ts,
        open: Decimal::from(open),
        high: Decimal::from(high),
        low: Decimal::from(low),
        close: Decimal::from(close),
        volume: Decimal::from(10),
        turnover: Decimal::from(close * 10),
        ts_available: if confirmed { ts + 60_000 } else { NOW_TS },
        ts_ingested: if confirmed { ts + 60_000 } else { NOW_TS },
        ts_committed: if confirmed { ts + 60_000 } else { NOW_TS },
        precision: PrecisionTier::SecondVerified,
        revision: 1,
        confirmed,
        history_only: false,
        synthetic: false,
    }
}

fn offline_config(data_dir: &Path, clock: Arc<AtomicI64>) -> ServerConfig {
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

fn seed_bars(state: &talaria_server::server::AppState) {
    let spec = btc_spec();
    let cold_store = state.cold_store.as_ref().expect("cold_store");
    let closed_bars = vec![
        raw_bar(BASE_TS - 120_000, 100, 101, 99, 100, true),
        raw_bar(BASE_TS - 60_000, 100, 102, 99, 101, true),
    ];
    cold_store.publish_bars(&spec, "1m", &closed_bars).unwrap();
    // In-progress open 1m bar at BASE_TS for current_bar verification.
    state.hot_buffer.ingest_received_kline(&ReceivedKlineEvent {
        event: KlineEvent {
            instrument_id: BTC.to_owned(),
            timeframe: "1m".to_owned(),
            bar: CompactBar {
                timestamp: BASE_TS,
                open: Some(Decimal::from(101)),
                high: Some(Decimal::from(103)),
                low: Some(Decimal::from(100)),
                close: Some(Decimal::from(102)),
                volume: Some(Decimal::from(10)),
                turnover: Some(Decimal::from(1020)),
                status: BarStatus::Normal,
                ts_available: NOW_TS,
                precision: PrecisionTier::SecondVerified,
            },
            confirmed: false,
            revision: 1,
        },
        ts_ingested: NOW_TS,
        history_only: false,
        synthetic: false,
    });
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

/// Ports Python `test_only_the_current_confirmed_draft_can_reach_the_account`.
#[tokio::test]
async fn only_current_confirmed_draft_reaches_account_and_receipt_is_single() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(NOW_TS));
    let server = start_server(offline_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    seed_bars(&server.state);

    let client = client_with_token(server.token());
    let base = server.base_url().to_owned();

    let snap: Value = client
        .post(format!("{base}/api/snapshots"))
        .json(&json!({"instrument_id": BTC, "timeframes": ["1m"]}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let snap_id = snap["snapshot_id"].as_str().unwrap().to_owned();

    let content = json!({
        "sub_account_id": "sa-one",
        "instrument_id": BTC,
        "snapshot_id": snap_id,
        "direction": "LONG",
        "entry_low": "100",
        "entry_high": "101",
        "invalidation_stop": "98",
        "take_profit": ["104"]
    });
    let saved_resp = client
        .post(format!("{base}/api/paper/drafts"))
        .json(&content)
        .send()
        .await
        .unwrap();
    assert_eq!(saved_resp.status(), StatusCode::OK);
    let draft: DraftRecord = saved_resp.json().await.unwrap();
    assert_eq!(draft.status, DraftStatus::Staged);

    let deliver_url = format!("{base}/api/paper/accounts/sa-one/next-draft");
    let empty_delivery: Option<ConfirmedDraft> = client
        .post(&deliver_url)
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(empty_delivery.is_none());

    // Confirm v1
    let confirm_v1 = client
        .post(format!("{base}/api/paper/drafts/{}/confirm", draft.draft_id))
        .json(&json!({
            "version": draft.version,
            "content_hash": draft.content_hash,
            "confirmed_by": "交易员"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(confirm_v1.status(), StatusCode::OK);

    // Edit v1 -> v2 invalidates v1 confirmation
    let mut edited_content = content.clone();
    edited_content["entry_high"] = json!("102");
    let edited: DraftRecord = client
        .put(format!("{base}/api/paper/drafts/{}", draft.draft_id))
        .json(&json!({"version": 1, "content": edited_content}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(edited.version, 2);
    assert_eq!(edited.status, DraftStatus::Modified);

    // Confirming stale v1 fails with 409 DRAFT_CONFLICT
    let stale_confirm = client
        .post(format!("{base}/api/paper/drafts/{}/confirm", draft.draft_id))
        .json(&json!({
            "version": 1,
            "content_hash": draft.content_hash,
            "confirmed_by": "交易员"
        }))
        .send()
        .await
        .unwrap();
    assert_eq!(stale_confirm.status(), StatusCode::CONFLICT);

    // Unconfirmed v2 cannot be claimed
    let none_v2: Option<ConfirmedDraft> = client
        .post(&deliver_url)
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(none_v2.is_none());

    // Confirm v2 (twice -> idempotent)
    let confirm_v2_body = json!({
        "version": edited.version,
        "content_hash": edited.content_hash,
        "confirmed_by": "交易员"
    });
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/confirm", draft.draft_id))
            .json(&confirm_v2_body)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/confirm", draft.draft_id))
            .json(&confirm_v2_body)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );

    // Other account cannot claim it
    let other_acct: Option<ConfirmedDraft> = client
        .post(format!("{base}/api/paper/accounts/sa-two/next-draft"))
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(other_acct.is_none());

    // Claim returns v2, and redelivery is idempotent while unacknowledged
    let delivery: ConfirmedDraft = client
        .post(&deliver_url)
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(delivery.version, 2);
    assert_eq!(delivery.content.entry_high, Decimal::from(102));
    assert_eq!(delivery.confirmed_by, "交易员");
    assert_eq!(delivery.confirmed_ts, NOW_TS);
    let redelivery: ConfirmedDraft = client
        .post(&deliver_url)
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(redelivery, delivery);

    // Claimed draft cannot be edited or discarded
    assert_eq!(
        client
            .put(format!("{base}/api/paper/drafts/{}", draft.draft_id))
            .json(&json!({"version": 2, "content": edited_content}))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::CONFLICT
    );
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/discard", draft.draft_id))
            .json(&json!({"version": 2}))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::CONFLICT
    );

    // Provisional receipts (DRAFT_INTERRUPTED / DRAFT_DATA_UNAVAILABLE) are rejected with 409
    for provisional in ["DRAFT_INTERRUPTED", "DRAFT_DATA_UNAVAILABLE"] {
        assert_eq!(
            client
                .post(format!("{base}/api/paper/accounts/sa-one/draft-receipt"))
                .json(&json!({
                    "draft_id": draft.draft_id,
                    "version": 2,
                    "accepted": false,
                    "reason": provisional
                }))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::CONFLICT
        );
    }

    // Terminal receipt succeeds and drains queue; conflicting second receipt returns 409
    let receipt = DraftReceipt {
        draft_id: draft.draft_id.clone(),
        version: 2,
        accepted: false,
        plan_id: None,
        reason: Some("DRAFT_STALE".to_owned()),
        snapshot_id: None,
        quantity: None,
    };
    assert_eq!(
        client
            .post(format!("{base}/api/paper/accounts/sa-one/draft-receipt"))
            .json(&receipt)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    // Identical receipt is idempotent
    assert_eq!(
        client
            .post(format!("{base}/api/paper/accounts/sa-one/draft-receipt"))
            .json(&receipt)
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    // Different receipt fails with 409
    assert_eq!(
        client
            .post(format!("{base}/api/paper/accounts/sa-one/draft-receipt"))
            .json(&json!({
                "draft_id": draft.draft_id,
                "version": 2,
                "accepted": true,
                "plan_id": "other"
            }))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::CONFLICT
    );

    let after_ack: Option<ConfirmedDraft> = client
        .post(&deliver_url)
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(after_ack.is_none());

    let listed: Vec<DraftRecord> = client
        .get(format!("{base}/api/paper/drafts"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(listed[0].receipt, Some(receipt));

    server.shutdown().await;
}

/// Ports Python `test_refreeze_binds_new_snapshot_to_cutoff_and_instrument` + verifies `current_bar`.
#[tokio::test]
async fn draft_evidence_refreezes_snapshot_and_attaches_open_current_bar() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(NOW_TS));
    let server = start_server(offline_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    seed_bars(&server.state);

    let client = client_with_token(server.token());
    let base = server.base_url().to_owned();

    // Missing cutoff_ts or wrong timeframe -> 422
    assert_eq!(
        client
            .post(format!("{base}/api/paper/draft-evidence"))
            .json(&json!({"instrument_id": BTC, "timeframes": ["5m"], "cutoff_ts": NOW_TS}))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::UNPROCESSABLE_ENTITY
    );

    let resp = client
        .post(format!("{base}/api/paper/draft-evidence"))
        .json(&json!({"instrument_id": BTC, "timeframes": ["1m"], "cutoff_ts": NOW_TS}))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::OK);
    let evidence: DraftEvidence = resp.json().await.unwrap();
    assert_eq!(evidence.cutoff_ts, NOW_TS);
    assert_eq!(evidence.instrument_id, BTC);
    assert!(!evidence.bars.is_empty());
    let current = evidence.current_bar.expect("open current_bar must be attached");
    assert_eq!(current.timestamp, BASE_TS);
    assert_eq!(current.high, Some(Decimal::from(103)));

    let snap_meta: Value = client
        .get(format!("{base}/api/snapshots/{}", evidence.snapshot_id))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(snap_meta["cutoff_ts"].as_i64(), Some(NOW_TS));

    server.shutdown().await;
}

/// Ports Python `test_claim_follows_confirmation_order_with_equal_timestamps`
/// and `test_confirmation_retry_after_database_interrupt_keeps_submission_order`.
#[tokio::test]
async fn claim_follows_confirmation_seq_even_with_equal_timestamps_and_after_interrupt() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(NOW_TS));
    let server = start_server(offline_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    seed_bars(&server.state);

    let client = client_with_token(server.token());
    let base = server.base_url().to_owned();

    let snap: Value = client
        .post(format!("{base}/api/snapshots"))
        .json(&json!({"instrument_id": BTC, "timeframes": ["1m"]}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let snap_id = snap["snapshot_id"].as_str().unwrap().to_owned();
    let content = json!({
        "sub_account_id": "sa-one",
        "instrument_id": BTC,
        "snapshot_id": snap_id,
        "direction": "LONG",
        "entry_low": "100",
        "entry_high": "101",
        "invalidation_stop": "98",
        "take_profit": ["104"]
    });
    let first: DraftRecord = client
        .post(format!("{base}/api/paper/drafts"))
        .json(&content)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let second: DraftRecord = client
        .post(format!("{base}/api/paper/drafts"))
        .json(&content)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    // Confirm second before first at the exact same timestamp
    for d in [&second, &first] {
        assert_eq!(
            client
                .post(format!("{base}/api/paper/drafts/{}/confirm", d.draft_id))
                .json(&json!({
                    "version": 1,
                    "content_hash": d.content_hash,
                    "confirmed_by": "trader"
                }))
                .send()
                .await
                .unwrap()
                .status(),
            StatusCode::OK
        );
    }

    let claimed: ConfirmedDraft = client
        .post(format!("{base}/api/paper/accounts/sa-one/next-draft"))
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed.draft_id, second.draft_id);

    server.shutdown().await;

    // Also verify SQLite transaction atomicity on confirmation interrupt (ports test #6)
    let tx_dir = TempDir::new().unwrap();
    let db_path = tx_dir.path().join("state/workbench_state.sqlite");
    let store = WorkbenchStateStore::open(tx_dir.path(), Arc::new(|| 1000)).unwrap();
    let parsed_content: DraftContent = serde_json::from_value(content).unwrap();
    let c1 = parsed_content.clone();
    let c2 = parsed_content.clone();
    let (d1, d2) = store
        .write(move |conn, now| {
            let a = paper_drafts_save(conn, &c1, 1000, now)?;
            let b = paper_drafts_save(conn, &c2, 1000, now)?;
            Ok((a, b))
        })
        .await
        .unwrap();
    let conf = DraftConfirmation {
        version: 1,
        content_hash: d1.content_hash.clone(),
        confirmed_by: "trader".to_owned(),
    };
    let d1_id = d1.draft_id.clone();
    let conf_c = conf.clone();
    let failed = store
        .write(move |conn, now| {
            conn.execute_batch(
                "CREATE TRIGGER interrupt BEFORE UPDATE OF confirmation ON paper_drafts
                 BEGIN SELECT RAISE(ABORT, 'interrupted'); END;",
            )
            .unwrap();
            let res = paper_drafts_confirm(conn, &d1_id, &conf_c, now);
            conn.execute_batch("DROP TRIGGER interrupt;").unwrap();
            res
        })
        .await;
    assert!(failed.is_err());

    let d1_id = d1.draft_id.clone();
    let d2_id = d2.draft_id.clone();
    let claimed_after = store
        .write(move |conn, now| {
            paper_drafts_confirm(conn, &d2_id, &conf, now)?;
            paper_drafts_confirm(conn, &d1_id, &conf, now)?;
            paper_drafts_claim(conn, "sa-one", BTC, now)
        })
        .await
        .unwrap()
        .unwrap();
    assert_eq!(claimed_after.draft_id, d2.draft_id);
    assert!(db_path.is_file());
    store.shutdown().await.unwrap();
}

/// Verifies `DISCARDED` (unclaimed only) and lazy `EXPIRED` (`valid_until`, never delivered).
#[tokio::test]
async fn discard_and_lazy_expiration_enforce_terminal_states() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(NOW_TS));
    let server = start_server(offline_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    seed_bars(&server.state);

    let client = client_with_token(server.token());
    let base = server.base_url().to_owned();

    let snap: Value = client
        .post(format!("{base}/api/snapshots"))
        .json(&json!({"instrument_id": BTC, "timeframes": ["1m"]}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let snap_id = snap["snapshot_id"].as_str().unwrap().to_owned();

    // 1. Discard a staged/confirmed unclaimed draft -> DISCARDED, never delivered
    let d1: DraftRecord = client
        .post(format!("{base}/api/paper/drafts"))
        .json(&json!({
            "sub_account_id": "sa-one",
            "instrument_id": BTC,
            "snapshot_id": snap_id,
            "direction": "LONG",
            "entry_low": "100",
            "entry_high": "101",
            "invalidation_stop": "98",
            "take_profit": ["104"]
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/confirm", d1.draft_id))
            .json(&json!({
                "version": 1,
                "content_hash": d1.content_hash,
                "confirmed_by": "trader"
            }))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );
    let discarded: DraftRecord = client
        .post(format!("{base}/api/paper/drafts/{}/discard", d1.draft_id))
        .json(&DraftDiscardRequest {
            version: 1,
            discarded_by: Some("trader".to_owned()),
            reason: Some("changed mind".to_owned()),
        })
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(discarded.status, DraftStatus::Discarded);
    // Discarded draft cannot be confirmed or claimed
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/confirm", d1.draft_id))
            .json(&json!({
                "version": 1,
                "content_hash": d1.content_hash,
                "confirmed_by": "trader"
            }))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::CONFLICT
    );
    let claimed_discarded: Option<ConfirmedDraft> = client
        .post(format!("{base}/api/paper/accounts/sa-one/next-draft"))
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(claimed_discarded.is_none());

    // 2. Draft with valid_until expires lazily when clock advances past deadline
    let deadline = NOW_TS + 10_000;
    let d2: DraftRecord = client
        .post(format!("{base}/api/paper/drafts"))
        .json(&json!({
            "sub_account_id": "sa-one",
            "instrument_id": BTC,
            "snapshot_id": snap_id,
            "direction": "LONG",
            "entry_low": "100",
            "entry_high": "101",
            "invalidation_stop": "98",
            "take_profit": ["104"],
            "valid_until": deadline
        }))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(d2.status, DraftStatus::Staged);
    assert_eq!(
        client
            .post(format!("{base}/api/paper/drafts/{}/confirm", d2.draft_id))
            .json(&json!({
                "version": 1,
                "content_hash": d2.content_hash,
                "confirmed_by": "trader"
            }))
            .send()
            .await
            .unwrap()
            .status(),
        StatusCode::OK
    );

    // Advance clock past deadline before account claims -> status becomes EXPIRED and next-draft skips it
    clock.store(deadline + 1, Ordering::SeqCst);
    let listed: Vec<DraftRecord> = client
        .get(format!("{base}/api/paper/drafts"))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    let d2_listed = listed.iter().find(|r| r.draft_id == d2.draft_id).unwrap();
    assert_eq!(d2_listed.status, DraftStatus::Expired);

    let claimed_expired: Option<ConfirmedDraft> = client
        .post(format!("{base}/api/paper/accounts/sa-one/next-draft"))
        .json(&json!({"instrument_id": BTC}))
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(claimed_expired.is_none());

    server.shutdown().await;
}

/// Verifies idempotent migration of unfinished drafts from legacy `state/collaboration_state.sqlite`.
#[tokio::test]
async fn migrates_unfinished_paper_drafts_from_legacy_collaboration_db_idempotently() {
    let dir = TempDir::new().unwrap();
    let state_dir = dir.path().join("state");
    fs::create_dir_all(&state_dir).unwrap();
    let legacy_path = dir.path().join(LEGACY_COLLAB_DB);

    let content: DraftContent = serde_json::from_value(json!({
        "sub_account_id": "sa-legacy",
        "instrument_id": BTC,
        "snapshot_id": "snap_legacy",
        "direction": "LONG",
        "entry_low": "100",
        "entry_high": "101",
        "invalidation_stop": "98",
        "take_profit": ["104"]
    }))
    .unwrap();
    let content_json = serde_json::to_string(&content).unwrap();
    let hash = draft_content_digest(&content).unwrap();
    let confirmed_json = serde_json::to_string(&ConfirmedDraft {
        version: 1,
        content_hash: hash,
        confirmed_by: "trader".to_owned(),
        draft_id: "drf_unfinished".to_owned(),
        content: content.clone(),
        basis_cutoff_ts: 1000,
        created_ts: 1000,
        confirmed_ts: 1005,
    })
    .unwrap();

    {
        let conn = Connection::open(&legacy_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE paper_drafts (
                draft_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                sub_account_id TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                content TEXT NOT NULL,
                basis_cutoff_ts INTEGER NOT NULL,
                created_ts INTEGER NOT NULL,
                confirmation TEXT,
                claimed INTEGER NOT NULL DEFAULT 0,
                receipt TEXT
            );
            CREATE TABLE paper_submissions (
                seq INTEGER PRIMARY KEY,
                draft_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                UNIQUE (draft_id, version)
            );",
        )
        .unwrap();
        // 1. Unfinished confirmed draft (should be imported & claimable)
        conn.execute(
            "INSERT INTO paper_drafts (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts, confirmation, claimed, receipt)
             VALUES ('drf_unfinished', 1, 'sa-legacy', ?1, ?2, 1000, 1000, ?3, 0, NULL)",
            params![BTC, content_json, confirmed_json],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO paper_submissions (seq, draft_id, version) VALUES (1, 'drf_unfinished', 1)",
            [],
        )
        .unwrap();
        // 2. Provisional interrupted draft (should be imported with receipt reset to NULL)
        conn.execute(
            "INSERT INTO paper_drafts (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts, confirmation, claimed, receipt)
             VALUES ('drf_interrupted', 1, 'sa-legacy', ?1, ?2, 1000, 1000, NULL, 0, ?3)",
            params![
                BTC,
                content_json,
                json!({"draft_id": "drf_interrupted", "version": 1, "accepted": false, "reason": "DRAFT_INTERRUPTED"}).to_string()
            ],
        )
        .unwrap();
        // 3. Terminal resolved draft (should NOT be imported)
        conn.execute(
            "INSERT INTO paper_drafts (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts, confirmation, claimed, receipt)
             VALUES ('drf_terminal', 1, 'sa-legacy', ?1, ?2, 1000, 1000, ?3, 1, ?4)",
            params![
                BTC,
                content_json,
                confirmed_json,
                json!({"draft_id": "drf_terminal", "version": 1, "accepted": true, "plan_id": "p1"}).to_string()
            ],
        )
        .unwrap();
    }

    // First open migrates unfinished drafts
    let store1 = WorkbenchStateStore::open(dir.path(), Arc::new(|| 2000)).unwrap();
    let list1 = store1.read_sync(|c| paper_drafts_list(c, 2000)).unwrap();
    assert_eq!(list1.len(), 2);
    assert!(list1.iter().any(|d| d.draft_id == "drf_unfinished" && d.status == DraftStatus::Confirmed));
    assert!(list1.iter().any(|d| d.draft_id == "drf_interrupted" && d.receipt.is_none()));
    assert!(!list1.iter().any(|d| d.draft_id == "drf_terminal"));
    store1.shutdown().await.unwrap();

    // Second open is idempotent (no duplicate rows, still claimable)
    let store2 = WorkbenchStateStore::open(dir.path(), Arc::new(|| 2000)).unwrap();
    let list2 = store2.read_sync(|c| paper_drafts_list(c, 2000)).unwrap();
    assert_eq!(list2.len(), 2);
    let claimed = store2
        .write(|conn, now| paper_drafts_claim(conn, "sa-legacy", BTC, now))
        .await
        .unwrap()
        .expect("migrated confirmed draft must be claimable");
    assert_eq!(claimed.draft_id, "drf_unfinished");
    store2.shutdown().await.unwrap();
}
