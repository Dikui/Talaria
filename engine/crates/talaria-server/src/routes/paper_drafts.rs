//! Paper trading draft lifecycle, outbound queue, and evidence refreeze endpoints (ADR-0025).
//!
//! Migrates the 9 Python `workbench_api/routes/paper.py` draft endpoints (plus `discard`)
//! into `talaria-data`, strictly holding the intent queue without executing any trading logic.

use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::routing::{get, post, put};
use rust_decimal::Decimal;
use talaria_contracts::bars::{CompactBar, classify_flat_bar};
use talaria_contracts::snapshot::SnapshotRequest;
use talaria_contracts::{
    ConfirmedDraft, Direction, DraftConfirmation, DraftContent, DraftDeliveryRequest,
    DraftDiscardRequest, DraftEdit, DraftEvidence, DraftReceipt, DraftRecord,
};
use talaria_store::range_visible_bars;

use crate::error::{ApiError, ApiJson, ApiPath};
use crate::routes::freeze_snapshot;
use crate::server::AppState;
use crate::state_store::{
    paper_drafts_acknowledge, paper_drafts_claim, paper_drafts_confirm, paper_drafts_discard,
    paper_drafts_edit, paper_drafts_list, paper_drafts_save,
};

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/paper/draft-evidence", post(freeze_draft_evidence))
        .route("/api/paper/drafts", get(list_drafts).post(save_draft))
        .route("/api/paper/drafts/{draft_id}", put(edit_draft))
        .route("/api/paper/drafts/{draft_id}/confirm", post(confirm_draft))
        .route("/api/paper/drafts/{draft_id}/discard", post(discard_draft))
        .route(
            "/api/paper/accounts/{account}/next-draft",
            post(next_draft),
        )
        .route(
            "/api/paper/accounts/{account}/draft-receipt",
            post(acknowledge_draft_receipt),
        )
}

fn is_valid_account_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    bytes[1..]
        .iter()
        .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_' || *b == b'-')
}

fn is_valid_content_hash(value: &str) -> bool {
    value.len() == 64 && value.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn validate_trader(value: &str, field: &str) -> Result<String, ApiError> {
    let trimmed = value.trim();
    if trimmed.is_empty() || trimmed.chars().count() > 80 {
        return Err(ApiError::validation(format!("{field} 长度须在 1..=80 之间")));
    }
    Ok(trimmed.to_owned())
}

fn validate_draft_content(content: &DraftContent) -> Result<(), ApiError> {
    if !is_valid_account_id(&content.sub_account_id) {
        return Err(ApiError::validation("sub_account_id 格式不合法"));
    }
    if content.instrument_id.is_empty() || content.instrument_id.len() > 128 {
        return Err(ApiError::validation("instrument_id 长度须在 1..=128 之间"));
    }
    if content.snapshot_id.is_empty() || content.snapshot_id.len() > 64 {
        return Err(ApiError::validation("snapshot_id 长度须在 1..=64 之间"));
    }
    if let Some(ref cid) = content.consultation_id
        && (cid.is_empty() || cid.len() > 64)
    {
        return Err(ApiError::validation("consultation_id 长度须在 1..=64 之间"));
    }
    if content.entry_low <= Decimal::ZERO
        || content.entry_high <= Decimal::ZERO
        || content.invalidation_stop <= Decimal::ZERO
    {
        return Err(ApiError::validation("价格须大于 0"));
    }
    if content.take_profit.is_empty() || content.take_profit.len() > 4 {
        return Err(ApiError::validation("take_profit 须包含 1 到 4 个目标价"));
    }
    if content.take_profit.iter().any(|p| *p <= Decimal::ZERO) {
        return Err(ApiError::validation("take_profit 价格须大于 0"));
    }
    if let Some(valid_until) = content.valid_until
        && valid_until < 0
    {
        return Err(ApiError::validation("valid_until 不得为负"));
    }
    if content.round_id.is_none() != content.inherited_barriers.is_none() {
        return Err(ApiError::validation("加仓需要目标回合和沿用的屏障"));
    }
    if let Some(ref round_id) = content.round_id
        && round_id.is_empty()
    {
        return Err(ApiError::validation("round_id 不能为空"));
    }
    if let Some(ref barriers) = content.inherited_barriers {
        if barriers.max_holding_ms <= 0 {
            return Err(ApiError::validation("max_holding_ms 须大于 0"));
        }
        if barriers.take_profit.is_empty() {
            return Err(ApiError::validation("沿用的屏障须包含至少一个止盈价"));
        }
        if let Some(ref ts) = barriers.trailing_stop
            && (ts.activation_pct <= Decimal::ZERO
                || ts.activation_pct >= Decimal::ONE
                || ts.distance_pct <= Decimal::ZERO
                || ts.distance_pct >= Decimal::ONE)
        {
            return Err(ApiError::validation("移动止损比例须在 (0, 1) 之间"));
        }
        if barriers.stop != content.invalidation_stop
            || barriers.take_profit != content.take_profit
        {
            return Err(ApiError::validation("加仓止损、止盈须与沿用的屏障一致"));
        }
    }
    if content.entry_high < content.entry_low {
        return Err(ApiError::validation("入场价带上下界倒置"));
    }
    let valid_geometry = match content.direction {
        Direction::Long => {
            let min_tp = content.take_profit.iter().min().copied().unwrap();
            content.invalidation_stop < content.entry_low && min_tp > content.entry_high
        }
        Direction::Short => {
            let max_tp = content.take_profit.iter().max().copied().unwrap();
            content.invalidation_stop > content.entry_high && max_tp < content.entry_low
        }
    };
    if !valid_geometry {
        return Err(ApiError::validation("止损与止盈须位于入场价带两侧"));
    }
    Ok(())
}

async fn resolve_basis_cutoff(
    state: &Arc<AppState>,
    content: &DraftContent,
) -> Result<i64, ApiError> {
    let snapshots = Arc::clone(&state.snapshots);
    let snapshot_id = content.snapshot_id.clone();
    let meta = tokio::task::spawn_blocking(move || snapshots.get_meta(&snapshot_id))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
    if meta.instrument_id != content.instrument_id || meta.cutoff_ts > state.now_ms() {
        return Err(ApiError::validation("草案与证据快照的标的或时刻不符"));
    }
    Ok(meta.cutoff_ts)
}

async fn freeze_draft_evidence(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<SnapshotRequest>,
) -> Result<ApiJson<DraftEvidence>, ApiError> {
    let Some(cutoff) = payload.cutoff_ts else {
        return Err(ApiError::validation(
            "草案重冻结需要明确的截止时刻与 1m 周期",
        ));
    };
    if payload.timeframes != ["1m"] {
        return Err(ApiError::validation(
            "草案重冻结需要明确的截止时刻与 1m 周期",
        ));
    }

    let opening = cutoff.div_euclid(60_000) * 60_000;
    let revisions = state
        .hot_buffer
        .revisions(&payload.instrument_id, "1m", opening, Some(opening));
    let committed: Vec<_> = revisions
        .into_iter()
        .filter(|row| row.ts_committed <= cutoff)
        .collect();
    let visible = range_visible_bars(&[], &committed, Some(cutoff), true);
    let current_bar: Option<CompactBar> = visible
        .into_iter()
        .find(|row| !row.confirmed && !row.synthetic)
        .and_then(|raw| {
            let status = classify_flat_bar(raw.open, raw.high, raw.low, raw.close, raw.volume);
            raw.to_compact(status).ok()
        });

    let snapshot = freeze_snapshot(Arc::clone(&state), payload).await?;
    let snapshots = Arc::clone(&state.snapshots);
    let snapshot_id = snapshot.snapshot_id.clone();
    let bars = tokio::task::spawn_blocking(move || -> Result<Vec<CompactBar>, ApiError> {
        let (_meta, all_bars) = snapshots.get_bars(&snapshot_id, "1m")?;
        let start = all_bars.len().saturating_sub(1000);
        Ok(all_bars[start..].to_vec())
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(DraftEvidence {
        snapshot_id: snapshot.snapshot_id,
        instrument_id: snapshot.instrument_id,
        cutoff_ts: snapshot.cutoff_ts,
        bars,
        current_bar,
    }))
}

async fn list_drafts(
    State(state): State<Arc<AppState>>,
) -> Result<ApiJson<Vec<DraftRecord>>, ApiError> {
    let now = state.now_ms();
    let rows = state
        .state_store
        .read(move |conn| paper_drafts_list(conn, now))
        .await?;
    Ok(ApiJson(rows))
}

async fn save_draft(
    State(state): State<Arc<AppState>>,
    ApiJson(content): ApiJson<DraftContent>,
) -> Result<ApiJson<DraftRecord>, ApiError> {
    validate_draft_content(&content)?;
    let basis = resolve_basis_cutoff(&state, &content).await?;
    let record = state
        .state_store
        .write(move |conn, now| paper_drafts_save(conn, &content, basis, now))
        .await?;
    Ok(ApiJson(record))
}

async fn edit_draft(
    State(state): State<Arc<AppState>>,
    ApiPath(draft_id): ApiPath<String>,
    ApiJson(payload): ApiJson<DraftEdit>,
) -> Result<ApiJson<DraftRecord>, ApiError> {
    if payload.version < 1 {
        return Err(ApiError::validation("version 须 >= 1"));
    }
    validate_draft_content(&payload.content)?;
    let basis = resolve_basis_cutoff(&state, &payload.content).await?;
    let record = state
        .state_store
        .write(move |conn, now| {
            paper_drafts_edit(conn, &draft_id, payload.version, &payload.content, basis, now)
        })
        .await?;
    Ok(ApiJson(record))
}

async fn confirm_draft(
    State(state): State<Arc<AppState>>,
    ApiPath(draft_id): ApiPath<String>,
    ApiJson(mut payload): ApiJson<DraftConfirmation>,
) -> Result<ApiJson<DraftRecord>, ApiError> {
    if payload.version < 1 {
        return Err(ApiError::validation("version 须 >= 1"));
    }
    if !is_valid_content_hash(&payload.content_hash) {
        return Err(ApiError::validation("content_hash 须为 64 位小写十六进制"));
    }
    payload.confirmed_by = validate_trader(&payload.confirmed_by, "confirmed_by")?;
    let record = state
        .state_store
        .write(move |conn, now| paper_drafts_confirm(conn, &draft_id, &payload, now))
        .await?;
    Ok(ApiJson(record))
}

async fn discard_draft(
    State(state): State<Arc<AppState>>,
    ApiPath(draft_id): ApiPath<String>,
    ApiJson(mut payload): ApiJson<DraftDiscardRequest>,
) -> Result<ApiJson<DraftRecord>, ApiError> {
    if payload.version < 1 {
        return Err(ApiError::validation("version 须 >= 1"));
    }
    if let Some(ref who) = payload.discarded_by {
        payload.discarded_by = Some(validate_trader(who, "discarded_by")?);
    }
    if let Some(ref reason) = payload.reason
        && reason.chars().count() > 256
    {
        return Err(ApiError::validation("reason 长度不得超过 256"));
    }
    let record = state
        .state_store
        .write(move |conn, now| paper_drafts_discard(conn, &draft_id, &payload, now))
        .await?;
    Ok(ApiJson(record))
}

async fn next_draft(
    State(state): State<Arc<AppState>>,
    ApiPath(account): ApiPath<String>,
    ApiJson(payload): ApiJson<DraftDeliveryRequest>,
) -> Result<ApiJson<Option<ConfirmedDraft>>, ApiError> {
    if !is_valid_account_id(&account) {
        return Err(ApiError::validation("account 格式不合法"));
    }
    let draft = state
        .state_store
        .write(move |conn, now| paper_drafts_claim(conn, &account, &payload.instrument_id, now))
        .await?;
    Ok(ApiJson(draft))
}

async fn acknowledge_draft_receipt(
    State(state): State<Arc<AppState>>,
    ApiPath(account): ApiPath<String>,
    ApiJson(payload): ApiJson<DraftReceipt>,
) -> Result<ApiJson<DraftReceipt>, ApiError> {
    if !is_valid_account_id(&account) {
        return Err(ApiError::validation("account 格式不合法"));
    }
    if payload.version < 1 {
        return Err(ApiError::validation("version 须 >= 1"));
    }
    let receipt = state
        .state_store
        .write(move |conn, now| paper_drafts_acknowledge(conn, &account, &payload, now))
        .await?;
    Ok(ApiJson(receipt))
}
