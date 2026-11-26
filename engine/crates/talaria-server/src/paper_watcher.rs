//! Background watcher polling `<data_dir>/accounts/*/view.json` (~500ms) and broadcasting
//! `paper_account` WebSocket events via `DeskBroadcast` on meaningful state or liveness changes.
//!
//! Strictly read-only on `view.json`; never opens `ledger.sqlite` or `owner.lock`.

use std::collections::{HashMap, HashSet};
use std::path::Path;
use std::sync::Arc;
use std::time::Duration;

use sha2::{Digest, Sha256};
use talaria_contracts::collaboration::{PaperAccountView, PaperProcessState};
use tokio::sync::watch;
use tokio::task::JoinHandle;

use crate::routes::paper_accounts::{list_projection_accounts, read_projection};
use crate::server::AppState;
use crate::ws::desk::DeskBroadcast;

pub const DEFAULT_PAPER_WATCHER_INTERVAL: Duration = Duration::from_millis(500);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AccountSnapshotKey {
    pub ledger_seq: u64,
    pub process_status: Option<PaperProcessState>,
    pub content_hash: [u8; 32],
}

fn snapshot_key(view: &PaperAccountView) -> AccountSnapshotKey {
    let process_status = view.process.as_ref().map(|p| p.status);
    let mut normalized = view.clone();
    if let Some(ref mut proc_info) = normalized.process {
        // Exclude volatile heartbeat_ts so identical keepalive ticks do not spam /ws/desk.
        proc_info.heartbeat_ts = 0;
    }
    let bytes = serde_json::to_vec(&normalized).unwrap_or_default();
    let digest = Sha256::digest(&bytes);
    let mut hash = [0u8; 32];
    hash.copy_from_slice(&digest);
    AccountSnapshotKey {
        ledger_seq: view.ledger_seq,
        process_status,
        content_hash: hash,
    }
}

/// Scans all `accounts/*/view.json` once and broadcasts `paper_account` for any new or changed view.
pub fn poll_paper_projections_once(
    data_dir: &Path,
    now_ms: i64,
    desk: &DeskBroadcast,
    seen: &mut HashMap<String, AccountSnapshotKey>,
) -> usize {
    let accounts = list_projection_accounts(data_dir);
    let current_set: HashSet<&str> = accounts.iter().map(String::as_str).collect();
    seen.retain(|account, _| current_set.contains(account.as_str()));

    let mut broadcast_count = 0;
    for account in accounts {
        let Ok(Some(view)) = read_projection(data_dir, &account, now_ms) else {
            continue;
        };
        let key = snapshot_key(&view);
        let changed = match seen.get(&account) {
            Some(prev) => prev != &key,
            None => true,
        };
        if changed {
            seen.insert(account, key);
            desk.broadcast_paper_account(&view);
            broadcast_count += 1;
        }
    }
    broadcast_count
}

/// Spawns the lightweight background projection watcher tied to the server shutdown channel.
pub fn spawn_paper_watcher(
    state: Arc<AppState>,
    mut shutdown_rx: watch::Receiver<bool>,
    poll_interval: Duration,
) -> JoinHandle<()> {
    tokio::spawn(async move {
        let mut ticker = tokio::time::interval(poll_interval);
        ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut seen: HashMap<String, AccountSnapshotKey> = HashMap::new();

        loop {
            tokio::select! {
                _ = ticker.tick() => {
                    let data_dir = state.data_dir.clone();
                    let now_ms = state.now_ms();
                    let desk = Arc::clone(&state.desk);
                    let mut cache = std::mem::take(&mut seen);
                    let res = tokio::task::spawn_blocking(move || {
                        poll_paper_projections_once(&data_dir, now_ms, &desk, &mut cache);
                        cache
                    }).await;
                    if let Ok(updated) = res {
                        seen = updated;
                    }
                }
                changed = shutdown_rx.changed() => {
                    if changed.is_err() || *shutdown_rx.borrow() {
                        break;
                    }
                }
            }
        }
    })
}
