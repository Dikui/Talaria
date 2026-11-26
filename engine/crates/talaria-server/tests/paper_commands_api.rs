//! Ticket 04 / ADR-0025 integration tests for the paper intervention command queue:
//! - Rejects offline or missing sub-accounts on `POST /api/paper/commands`
//! - Validates command kinds & payloads
//! - Serial FIFO `seq` ordering per sub-account, idempotent submission, crash redelivery, idempotent receipt

use std::fs;
use std::path::Path;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};

use reqwest::StatusCode;
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::collaboration::{
    PaperAccountView, PaperCommand, PaperCommandKind, PaperCommandPayload, PaperCommandReceipt,
    PaperCommandRecord, PaperProcessInfo, PaperProcessState, TradingState,
};
use talaria_server::{ServerConfig, start_server};
use tempfile::TempDir;

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

fn write_account_projection(
    data_dir: &Path,
    account: &str,
    heartbeat_ts: i64,
    status: PaperProcessState,
) {
    let dir = data_dir.join("accounts").join(account);
    fs::create_dir_all(&dir).unwrap();
    let view = PaperAccountView {
        sub_account_id: account.to_owned(),
        positions: vec![],
        rounds: vec![],
        fills: vec![],
        ledger_seq: 10,
        account_state: None,
        feed_status: None,
        process: Some(PaperProcessInfo {
            pid: 9001,
            scheme: "paper-test".to_owned(),
            started_ts: BASE_NOW - 10_000,
            heartbeat_ts,
            status,
            stopped_ts: if status == PaperProcessState::Stopped {
                Some(heartbeat_ts)
            } else {
                None
            },
        }),
    };
    fs::write(dir.join("view.json"), serde_json::to_vec(&view).unwrap()).unwrap();
}

fn make_cmd(command_id: &str, account: &str, kind: PaperCommandKind) -> PaperCommand {
    let (round_id, payload) = match kind {
        PaperCommandKind::SetTradingState => (
            None,
            PaperCommandPayload {
                trading_state: Some(TradingState::Reducing),
                reason: Some("manual_pause".to_owned()),
                ..Default::default()
            },
        ),
        PaperCommandKind::ReduceRatio => (
            Some("r-1".to_owned()),
            PaperCommandPayload {
                ratio: Some(Decimal::new(5, 1)),
                ..Default::default()
            },
        ),
        PaperCommandKind::TightenStop => (
            Some("r-1".to_owned()),
            PaperCommandPayload {
                stop: Some(Decimal::from(49_800)),
                ..Default::default()
            },
        ),
        _ => (Some("r-1".to_owned()), PaperCommandPayload::default()),
    };
    PaperCommand {
        command_id: command_id.to_owned(),
        sub_account_id: account.to_owned(),
        round_id,
        kind,
        payload,
        based_on_ledger_seq: 10,
        issued_by: "trader".to_owned(),
        issued_ts: BASE_NOW,
    }
}

#[tokio::test]
async fn paper_commands_rejects_missing_or_offline_account_and_delivers_serially_with_idempotency() {
    let dir = TempDir::new().unwrap();
    let clock = Arc::new(AtomicI64::new(BASE_NOW));
    let server = start_server(test_config(dir.path(), Arc::clone(&clock)))
        .await
        .unwrap();
    let client = reqwest::Client::new();
    let auth = format!("Bearer {}", server.token());

    // 1. Missing sub-account -> 404.
    let resp = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&make_cmd("cmd-missing", "sa-none", PaperCommandKind::CloseAll))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::NOT_FOUND);

    // 2. Offline / stale heartbeat sub-account -> 409 ACCOUNT_OFFLINE.
    write_account_projection(
        dir.path(),
        "sa-offline",
        BASE_NOW - 10_000,
        PaperProcessState::Running,
    );
    let resp = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&make_cmd("cmd-off", "sa-offline", PaperCommandKind::CloseAll))
        .send()
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::CONFLICT);
    let body: Value = resp.json().await.unwrap();
    assert_eq!(body["code"], "ACCOUNT_OFFLINE");

    // 3. Online account accepts commands, validates invalid payloads with 422.
    write_account_projection(dir.path(), "sa-live", BASE_NOW, PaperProcessState::Running);
    let invalid_cmd = PaperCommand {
        command_id: "cmd-bad".to_owned(),
        sub_account_id: "sa-live".to_owned(),
        round_id: None, // missing round_id for CLOSE_ALL
        kind: PaperCommandKind::CloseAll,
        payload: PaperCommandPayload::default(),
        based_on_ledger_seq: 10,
        issued_by: "trader".to_owned(),
        issued_ts: BASE_NOW,
    };
    let bad_resp = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&invalid_cmd)
        .send()
        .await
        .unwrap();
    assert_eq!(bad_resp.status(), StatusCode::UNPROCESSABLE_ENTITY);

    // 4. Submit two commands serially; verify idempotent repeat & conflict on payload change.
    let cmd1 = make_cmd("cmd-1", "sa-live", PaperCommandKind::ReduceRatio);
    let cmd2 = make_cmd("cmd-2", "sa-live", PaperCommandKind::SetTradingState);

    let r1: PaperCommandRecord = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&cmd1)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r1.command, cmd1);
    assert!(!r1.claimed);
    assert!(r1.receipt.is_none());

    // Idempotent duplicate submit returns same record.
    let r1_dup: PaperCommandRecord = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&cmd1)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(r1_dup, r1);

    // Different payload with same command_id -> 409 conflict.
    let mut cmd1_mut = cmd1.clone();
    cmd1_mut.payload.ratio = Some(Decimal::new(25, 2));
    let conflict_resp = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&cmd1_mut)
        .send()
        .await
        .unwrap();
    assert_eq!(conflict_resp.status(), StatusCode::CONFLICT);

    let _: PaperCommandRecord = client
        .post(format!("{}/api/paper/commands", server.base_url()))
        .header("Authorization", &auth)
        .json(&cmd2)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    // 5. Claim delivers cmd-1 first, and crash-before-ack redelivers cmd-1 before cmd-2!
    let claimed_a: Option<PaperCommand> = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/next-command",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed_a, Some(cmd1.clone()));

    let claimed_retry: Option<PaperCommand> = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/next-command",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed_retry, Some(cmd1.clone()));

    // 6. Acknowledge cmd-1 (idempotently), then next-command advances to cmd-2.
    let receipt1 = PaperCommandReceipt {
        command_id: "cmd-1".to_owned(),
        sub_account_id: Some("sa-live".to_owned()),
        accepted: true,
        reason: None,
        ledger_seq: Some(11),
        processed_ts: Some(BASE_NOW + 100),
    };
    let ack1: PaperCommandReceipt = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/command-receipt",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .json(&receipt1)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ack1.accepted, true);
    assert_eq!(ack1.ledger_seq, Some(11));

    // Idempotent duplicate ack succeeds.
    let ack1_dup: PaperCommandReceipt = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/command-receipt",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .json(&receipt1)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ack1_dup, ack1);

    let claimed_2: Option<PaperCommand> = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/next-command",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed_2, Some(cmd2.clone()));

    let receipt2 = PaperCommandReceipt {
        command_id: "cmd-2".to_owned(),
        sub_account_id: Some("sa-live".to_owned()),
        accepted: false,
        reason: Some("COMMAND_STALE".to_owned()),
        ledger_seq: Some(12),
        processed_ts: Some(BASE_NOW + 200),
    };
    let _: PaperCommandReceipt = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/command-receipt",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .json(&receipt2)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();

    // Queue is now drained.
    let claimed_none: Option<PaperCommand> = client
        .post(format!(
            "{}/api/paper/accounts/sa-live/next-command",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(claimed_none, None);

    // 7. GET /api/paper/commands?sub_account_id=sa-live lists newest-first with receipts.
    let listed: Vec<PaperCommandRecord> = client
        .get(format!(
            "{}/api/paper/commands?sub_account_id=sa-live",
            server.base_url()
        ))
        .header("Authorization", &auth)
        .send()
        .await
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(listed.len(), 2);
    assert_eq!(listed[0].command.command_id, "cmd-2");
    assert_eq!(
        listed[0].receipt.as_ref().unwrap().reason.as_deref(),
        Some("COMMAND_STALE")
    );
    assert_eq!(listed[1].command.command_id, "cmd-1");
    assert!(listed[1].receipt.as_ref().unwrap().accepted);

    server.shutdown().await;
}
