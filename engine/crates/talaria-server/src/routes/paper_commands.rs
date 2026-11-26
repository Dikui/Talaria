//! Paper trading manual intervention command queue endpoints (ADR-0025 D1/D3).
//!
//! `talaria-data` validates command schema & online account liveness, persists per-sub-account
//! serial outbound commands via `WorkbenchStateStore`, and never executes trading logic itself.

use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::routing::{get, post};
use rust_decimal::Decimal;
use serde::Deserialize;
use talaria_contracts::collaboration::{
    PaperCommand, PaperCommandKind, PaperCommandReceipt, PaperCommandRecord, PaperProcessState,
};
use talaria_contracts::error_codes::NOT_FOUND;

use crate::error::{ApiError, ApiJson, ApiPath, ApiQuery};
use crate::routes::paper_accounts::{read_projection, validate_account_id};
use crate::server::AppState;
use crate::state_store::{
    paper_commands_acknowledge, paper_commands_claim, paper_commands_list, paper_commands_submit,
};

pub const ACCOUNT_OFFLINE: &str = "ACCOUNT_OFFLINE";

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/paper/commands", get(list_commands).post(submit_command))
        .route(
            "/api/paper/accounts/{account}/next-command",
            post(next_command),
        )
        .route(
            "/api/paper/accounts/{account}/command-receipt",
            post(acknowledge_command_receipt),
        )
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CommandsQuery {
    #[serde(default)]
    pub sub_account_id: Option<String>,
}

fn validate_command(cmd: &mut PaperCommand) -> Result<(), ApiError> {
    if cmd.command_id.is_empty() || cmd.command_id.chars().count() > 64 {
        return Err(ApiError::validation("command_id 长度须在 1..=64 之间"));
    }
    validate_account_id(&cmd.sub_account_id)?;
    let issuer = cmd.issued_by.trim();
    if issuer.is_empty() || issuer.chars().count() > 80 {
        return Err(ApiError::validation("issued_by 长度须在 1..=80 之间"));
    }
    cmd.issued_by = issuer.to_owned();
    if cmd.issued_ts < 0 {
        return Err(ApiError::validation("issued_ts 不得为负"));
    }
    if let Some(ref reason) = cmd.payload.reason
        && reason.chars().count() > 256
    {
        return Err(ApiError::validation("reason 长度不得超过 256"));
    }

    match cmd.kind {
        PaperCommandKind::SetTradingState => {
            if cmd.round_id.is_some() {
                return Err(ApiError::validation("SET_TRADING_STATE 不应绑定 round_id"));
            }
            if cmd.payload.trading_state.is_none() {
                return Err(ApiError::validation(
                    "SET_TRADING_STATE 必须提供 payload.trading_state",
                ));
            }
        }
        kind => {
            let Some(ref round_id) = cmd.round_id else {
                return Err(ApiError::validation("持仓干预命令必须提供 round_id"));
            };
            if round_id.is_empty() || round_id.chars().count() > 64 {
                return Err(ApiError::validation("round_id 长度须在 1..=64 之间"));
            }
            match kind {
                PaperCommandKind::TightenStop | PaperCommandKind::WidenStop => {
                    let Some(stop) = cmd.payload.stop else {
                        return Err(ApiError::validation("止损调整命令必须提供 payload.stop"));
                    };
                    if stop <= Decimal::ZERO {
                        return Err(ApiError::validation("payload.stop 须大于 0"));
                    }
                }
                PaperCommandKind::ModifyTakeProfit => {
                    let Some(ref targets) = cmd.payload.take_profit else {
                        return Err(ApiError::validation(
                            "MODIFY_TAKE_PROFIT 必须提供 payload.take_profit",
                        ));
                    };
                    if targets.is_empty() || targets.len() > 4 {
                        return Err(ApiError::validation(
                            "payload.take_profit 须包含 1 到 4 个目标价",
                        ));
                    }
                    if targets.iter().any(|p| *p <= Decimal::ZERO) {
                        return Err(ApiError::validation("take_profit 价格须大于 0"));
                    }
                }
                PaperCommandKind::ReduceRatio => {
                    let Some(ratio) = cmd.payload.ratio else {
                        return Err(ApiError::validation("REDUCE_RATIO 必须提供 payload.ratio"));
                    };
                    if ratio <= Decimal::ZERO || ratio > Decimal::ONE {
                        return Err(ApiError::validation("payload.ratio 须在 (0, 1] 之间"));
                    }
                }
                PaperCommandKind::MoveToBreakeven
                | PaperCommandKind::CancelEntry
                | PaperCommandKind::CloseAll => {}
                PaperCommandKind::SetTradingState => unreachable!(),
            }
        }
    }
    Ok(())
}

async fn submit_command(
    State(state): State<Arc<AppState>>,
    ApiJson(mut command): ApiJson<PaperCommand>,
) -> Result<ApiJson<PaperCommandRecord>, ApiError> {
    validate_command(&mut command)?;

    let data_dir = state.data_dir.clone();
    let account = command.sub_account_id.clone();
    let now_ms = state.now_ms();
    let view = tokio::task::spawn_blocking(move || read_projection(&data_dir, &account, now_ms))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
    let Some(view) = view else {
        return Err(ApiError::not_found(
            NOT_FOUND,
            format!("子账户 {} 不存在或尚无只读投影", command.sub_account_id),
        ));
    };
    let is_online = view
        .process
        .as_ref()
        .is_some_and(|p| p.status == PaperProcessState::Running);
    if !is_online {
        return Err(ApiError::conflict(
            ACCOUNT_OFFLINE,
            format!("子账户 {} 当前离线，无法提交干预命令", command.sub_account_id),
        ));
    }

    let record = state
        .state_store
        .write(move |conn, now| paper_commands_submit(conn, &command, now))
        .await?;
    Ok(ApiJson(record))
}

async fn list_commands(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<CommandsQuery>,
) -> Result<ApiJson<Vec<PaperCommandRecord>>, ApiError> {
    if let Some(ref account) = query.sub_account_id {
        validate_account_id(account)?;
    }
    let sub_account_id = query.sub_account_id;
    let rows = state
        .state_store
        .read(move |conn| paper_commands_list(conn, sub_account_id.as_deref()))
        .await?;
    Ok(ApiJson(rows))
}

async fn next_command(
    State(state): State<Arc<AppState>>,
    ApiPath(account): ApiPath<String>,
) -> Result<ApiJson<Option<PaperCommand>>, ApiError> {
    validate_account_id(&account)?;
    let cmd = state
        .state_store
        .write(move |conn, _now| paper_commands_claim(conn, &account))
        .await?;
    Ok(ApiJson(cmd))
}

async fn acknowledge_command_receipt(
    State(state): State<Arc<AppState>>,
    ApiPath(account): ApiPath<String>,
    ApiJson(receipt): ApiJson<PaperCommandReceipt>,
) -> Result<ApiJson<PaperCommandReceipt>, ApiError> {
    validate_account_id(&account)?;
    if receipt.command_id.is_empty() || receipt.command_id.chars().count() > 64 {
        return Err(ApiError::validation("command_id 长度须在 1..=64 之间"));
    }
    let stored = state
        .state_store
        .write(move |conn, _now| paper_commands_acknowledge(conn, &account, &receipt))
        .await?;
    Ok(ApiJson(stored))
}
