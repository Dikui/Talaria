//! Read-only `/api/paper/*` account projection endpoints (ADR-0025).
//!
//! Strictly reads `<data_dir>/accounts/<sub_account_id>/view.json`.
//! NEVER opens `ledger.sqlite` and NEVER probes `owner.lock`.

use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::routing::get;
use rust_decimal::Decimal;
use serde::Deserialize;
use talaria_contracts::collaboration::{PaperAccountView, PaperProcessState, PaperRound};
use talaria_contracts::error_codes::NOT_FOUND;

use crate::error::{ApiError, ApiJson, ApiPath, ApiQuery};
use crate::server::AppState;

/// Heartbeat staleness threshold (ms) beyond which a `RUNNING` account process is reported `OFFLINE`.
pub const PAPER_HEARTBEAT_STALE_MS: i64 = 5_000;

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/paper/accounts", get(list_paper_accounts))
        .route("/api/paper/accounts/{account}", get(get_paper_account))
        .route("/api/paper/barriers", get(get_paper_barriers))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BarriersQuery {
    pub instrument: String,
}

/// Validates `^[a-z0-9][a-z0-9_-]{0,63}$` matching `ACCOUNT_ID_PATTERN`.
pub fn validate_account_id(account: &str) -> Result<(), ApiError> {
    let bytes = account.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return Err(ApiError::validation("invalid sub_account_id"));
    }
    let first = bytes[0];
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return Err(ApiError::validation("invalid sub_account_id"));
    }
    if !bytes
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_' || *b == b'-')
    {
        return Err(ApiError::validation("invalid sub_account_id"));
    }
    Ok(())
}

pub fn projection_file_path(data_dir: &Path, account: &str) -> Result<PathBuf, ApiError> {
    validate_account_id(account)?;
    Ok(data_dir.join("accounts").join(account).join("view.json"))
}

/// Lists all sub-accounts that have a readable `accounts/<id>/view.json` projection.
pub fn list_projection_accounts(data_dir: &Path) -> Vec<String> {
    let accounts_dir = data_dir.join("accounts");
    let Ok(entries) = fs::read_dir(&accounts_dir) else {
        return Vec::new();
    };
    let mut ids = Vec::new();
    for entry in entries.flatten() {
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let Some(name) = entry.file_name().to_str().map(str::to_owned) else {
            continue;
        };
        if validate_account_id(&name).is_err() {
            continue;
        }
        if path.join("view.json").is_file() {
            ids.push(name);
        }
    }
    ids.sort();
    ids
}

/// Computes live `OFFLINE` liveness from `process.status` and `process.heartbeat_ts` (ADR-0025).
pub fn normalize_projection_liveness(mut view: PaperAccountView, now_ms: i64) -> PaperAccountView {
    if let Some(ref mut process) = view.process {
        let is_stopped = process.status != PaperProcessState::Running;
        let is_stale = now_ms.saturating_sub(process.heartbeat_ts) > PAPER_HEARTBEAT_STALE_MS;
        if is_stopped || is_stale {
            process.status = PaperProcessState::Offline;
            if let Some(ref mut feed) = view.feed_status {
                feed.healthy = false;
            }
        }
    }
    view
}

/// Reads and normalizes `<data_dir>/accounts/<account>/view.json` without touching SQLite or lock files.
pub fn read_projection(
    data_dir: &Path,
    account: &str,
    now_ms: i64,
) -> Result<Option<PaperAccountView>, ApiError> {
    let path = projection_file_path(data_dir, account)?;
    let text = match fs::read_to_string(&path) {
        Ok(content) => content,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(err) => {
            return Err(ApiError::internal(format!(
                "failed to read {}: {err}",
                path.display()
            )));
        }
    };
    let parsed: PaperAccountView = serde_json::from_str(&text).map_err(|err| {
        ApiError::internal(format!("invalid view.json in {}: {err}", path.display()))
    })?;
    Ok(Some(normalize_projection_liveness(parsed, now_ms)))
}

fn is_active_round(round: &PaperRound) -> bool {
    match round.phase.as_deref() {
        Some("CLOSED" | "CANCELLED" | "LIQUIDATED") => false,
        Some(_) => true,
        None => round.quantity > Decimal::ZERO,
    }
}

async fn list_paper_accounts(State(state): State<Arc<AppState>>) -> Result<ApiJson<Vec<String>>, ApiError> {
    let data_dir = state.data_dir.clone();
    let accounts = tokio::task::spawn_blocking(move || list_projection_accounts(&data_dir))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?;
    Ok(ApiJson(accounts))
}

async fn get_paper_account(
    State(state): State<Arc<AppState>>,
    ApiPath(account): ApiPath<String>,
) -> Result<ApiJson<PaperAccountView>, ApiError> {
    validate_account_id(&account)?;
    let data_dir = state.data_dir.clone();
    let now_ms = state.now_ms();
    let account_c = account.clone();
    let view = tokio::task::spawn_blocking(move || read_projection(&data_dir, &account_c, now_ms))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
    match view {
        Some(v) => Ok(ApiJson(v)),
        None => Err(ApiError::not_found(
            NOT_FOUND,
            format!("paper account projection not found: {account}"),
        )),
    }
}

async fn get_paper_barriers(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<BarriersQuery>,
) -> Result<ApiJson<Vec<PaperRound>>, ApiError> {
    if query.instrument.trim().is_empty() {
        return Err(ApiError::validation("instrument must not be empty"));
    }
    let data_dir = state.data_dir.clone();
    let now_ms = state.now_ms();
    let instrument = query.instrument;

    let rounds = tokio::task::spawn_blocking(move || {
        let mut active = Vec::new();
        for account in list_projection_accounts(&data_dir) {
            let Ok(Some(view)) = read_projection(&data_dir, &account, now_ms) else {
                continue;
            };
            let mut seen_ids = HashSet::new();
            for round in view.rounds {
                if round.instrument_id == instrument && is_active_round(&round) {
                    seen_ids.insert(round.round_id.clone());
                    active.push(round);
                }
            }
            for pos in view.positions {
                if pos.instrument_id == instrument
                    && is_active_round(&pos)
                    && seen_ids.insert(pos.round_id.clone())
                {
                    active.push(pos);
                }
            }
        }
        active
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?;

    Ok(ApiJson(rounds))
}
