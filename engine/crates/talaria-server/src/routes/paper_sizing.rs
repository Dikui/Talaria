//! Side-effect-free real-time sizing preview endpoint (`POST /api/paper/sizing-preview`).
//!
//! Reads sub-account equity, risk parameters, and active round exposure strictly from the
//! read-only `<data_dir>/accounts/<sub_account_id>/view.json` projection (ADR-0025), then
//! computes preview outputs via `talaria_operators::sizing::compute_sizing_preview` (ADR-0010 / D4).
//! When the account process is offline or stale (>5s heartbeat), computes from the last projection
//! and flags `from_offline_snapshot = true`.

use std::str::FromStr;
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::routing::post;
use rust_decimal::Decimal;
use talaria_contracts::{
    Direction, MarginMode, PaperProcessState, SizingPreviewRequest, SizingPreviewResponse,
};
use talaria_operators::{DEFAULT_MAINTENANCE_RATE, SizingPreviewInput, compute_sizing_preview};

use crate::error::{ApiError, ApiJson};
use crate::routes::paper_accounts::{read_projection, validate_account_id};
use crate::server::AppState;

pub fn router() -> Router<Arc<AppState>> {
    Router::new().route("/api/paper/sizing-preview", post(preview_sizing))
}

fn rejected_response(
    reason: &str,
    from_offline_snapshot: bool,
    based_on_ledger_seq: Option<u64>,
) -> SizingPreviewResponse {
    SizingPreviewResponse {
        quantity: None,
        basis: None,
        risk_amount_r: None,
        risk_pct_of_equity: None,
        est_liq_price: None,
        est_liq_distance: None,
        est_liq_distance_pct: None,
        rejected_reason: Some(reason.to_owned()),
        from_offline_snapshot,
        based_on_ledger_seq,
    }
}

async fn preview_sizing(
    State(state): State<Arc<AppState>>,
    ApiJson(req): ApiJson<SizingPreviewRequest>,
) -> Result<ApiJson<SizingPreviewResponse>, ApiError> {
    validate_account_id(&req.sub_account_id)?;

    let Some(spec) = state.pack.instrument(&req.instrument_id) else {
        return Ok(ApiJson(rejected_response("UNKNOWN_INSTRUMENT", false, None)));
    };
    let contract_multiplier = state
        .pack
        .traits(&req.instrument_id)
        .map(|t| t.margin.contract_multiplier)
        .unwrap_or(Decimal::ONE);

    let data_dir = state.data_dir.clone();
    let account = req.sub_account_id.clone();
    let now_ms = state.now_ms();
    let view = tokio::task::spawn_blocking(move || read_projection(&data_dir, &account, now_ms))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;

    let Some(view) = view else {
        return Ok(ApiJson(rejected_response(
            "ACCOUNT_PROJECTION_UNAVAILABLE",
            true,
            None,
        )));
    };

    let from_offline_snapshot = view
        .process
        .as_ref()
        .is_none_or(|p| p.status != PaperProcessState::Running);
    let based_on_ledger_seq = Some(view.ledger_seq);

    let Some(acct_state) = view.account_state else {
        return Ok(ApiJson(rejected_response(
            "ACCOUNT_STATE_UNAVAILABLE",
            from_offline_snapshot,
            based_on_ledger_seq,
        )));
    };

    let r_unit_pct = acct_state
        .r_unit_pct
        .unwrap_or_else(|| Decimal::from_str("0.005").unwrap());
    let max_single_r_pct = acct_state
        .max_single_r_pct
        .unwrap_or_else(|| Decimal::from_str("0.01").unwrap());
    let friction_buffer_pct = acct_state
        .friction_buffer_pct
        .unwrap_or_else(|| Decimal::from_str("0.002").unwrap());
    let margin_mode = acct_state.margin_mode.unwrap_or(MarginMode::Cross);
    let leverage = acct_state
        .leverage
        .unwrap_or_else(|| Decimal::from(5));
    let maintenance_rate = Decimal::from_str(DEFAULT_MAINTENANCE_RATE).unwrap();

    let mut existing_round_stop: Option<Decimal> = None;
    let mut existing_round_risk = Decimal::ZERO;
    if let Some(ref target_round_id) = req.round_id {
        if let Some(round) = view
            .positions
            .iter()
            .chain(view.rounds.iter())
            .find(|r| r.round_id == *target_round_id)
        {
            existing_round_stop = Some(round.stop);
            if round.quantity > Decimal::ZERO {
                let entry_ref = round
                    .entry_price
                    .or(match round.direction {
                        Direction::Long => round.entry_high,
                        Direction::Short => round.entry_low,
                    })
                    .unwrap_or(round.stop);
                let side = match round.direction {
                    Direction::Long => Decimal::ONE,
                    Direction::Short => -Decimal::ONE,
                };
                let unit_r = (side * (entry_ref - round.stop) + entry_ref * friction_buffer_pct)
                    .max(Decimal::ZERO);
                existing_round_risk = unit_r * round.quantity * contract_multiplier;
            }
        }
    }

    let input = SizingPreviewInput {
        direction: req.direction,
        entry_low: req.entry_low,
        entry_high: req.entry_high,
        invalidation_stop: req.invalidation_stop,
        qty_step: spec.qty_step,
        min_qty: spec.min_qty,
        contract_multiplier,
        equity: acct_state.equity,
        r_unit_pct,
        max_single_r_pct,
        friction_buffer_pct,
        margin_mode,
        leverage,
        maintenance_rate,
        existing_round_risk,
        existing_round_stop,
        override_quantity: req.override_quantity,
    };

    let mut response = compute_sizing_preview(&input);
    response.from_offline_snapshot = from_offline_snapshot;
    response.based_on_ledger_seq = based_on_ledger_seq;
    Ok(ApiJson(response))
}
