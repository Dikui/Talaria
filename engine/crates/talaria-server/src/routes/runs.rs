//! Read-only `/api/runs*` endpoints projecting `Account Process` `run_export` files.
//!
//! Strictly treats `summary.json` as the atomic ready marker and NEVER opens `ledger.sqlite`.

use std::cmp::Reverse;
use std::fs::{self, File};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use arrow::array::{Array, Int64Array, LargeStringArray, StringArray};
use arrow::record_batch::RecordBatch;
use axum::Router;
use axum::extract::State;
use axum::routing::get;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use serde::Deserialize;
use talaria_contracts::decimal::parse_decimal_str;
use talaria_contracts::error_codes::NOT_FOUND;
use talaria_contracts::market::Direction;
use talaria_contracts::runs::{
    RunBarrierChange, RunEquityPoint, RunEquityResponse, RunExportSummary, RunFillReason,
    RunListResponse, RunRoundFill, RunRoundPhase, RunRoundRecord, RunRoundsResponse,
};

use crate::error::{ApiError, ApiJson, ApiPath, ApiQuery};
use crate::server::AppState;

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/runs", get(list_runs))
        .route("/api/runs/{run_id}", get(get_run_summary))
        .route("/api/runs/{run_id}/rounds", get(get_run_rounds))
        .route("/api/runs/{run_id}/equity", get(get_run_equity))
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunDetailQuery {
    #[serde(default)]
    pub sub_account_id: Option<String>,
    #[serde(default)]
    pub currency: Option<String>,
}

fn validate_run_id(run_id: &str) -> Result<(), ApiError> {
    if run_id.is_empty()
        || run_id.len() > 128
        || !run_id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
    {
        return Err(ApiError::validation("invalid run_id"));
    }
    Ok(())
}

fn safe_artifact_path(run_dir: &Path, rel: &str) -> Result<PathBuf, ApiError> {
    let rel_path = Path::new(rel);
    if rel.is_empty()
        || rel_path.is_absolute()
        || rel_path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(ApiError::internal(format!(
            "invalid run artifact relative path: {rel}"
        )));
    }
    Ok(run_dir.join(rel_path))
}

fn read_summary_file(run_dir: &Path) -> Result<Option<RunExportSummary>, ApiError> {
    let summary_path = run_dir.join("summary.json");
    if !summary_path.is_file() {
        return Ok(None);
    }
    let text = fs::read_to_string(&summary_path).map_err(|e| {
        ApiError::internal(format!(
            "failed to read {}: {e}",
            summary_path.display()
        ))
    })?;
    let summary: RunExportSummary = serde_json::from_str(&text).map_err(|e| {
        ApiError::internal(format!(
            "invalid summary.json in {}: {e}",
            summary_path.display()
        ))
    })?;
    Ok(Some(summary))
}

fn require_ready_summary(data_dir: &Path, run_id: &str) -> Result<(PathBuf, RunExportSummary), ApiError> {
    validate_run_id(run_id)?;
    let run_dir = data_dir.join("runs").join(run_id);
    if !run_dir.is_dir() {
        return Err(ApiError::not_found(
            NOT_FOUND,
            format!("run {run_id} not found"),
        ));
    }
    let summary = read_summary_file(&run_dir)?.ok_or_else(|| {
        ApiError::not_found(
            NOT_FOUND,
            format!("run {run_id} export not ready (missing summary.json)"),
        )
    })?;
    Ok((run_dir, summary))
}

enum StrCol<'a> {
    Utf8(&'a StringArray),
    Large(&'a LargeStringArray),
}

impl<'a> StrCol<'a> {
    fn from_batch(batch: &'a RecordBatch, name: &str) -> Result<Self, ApiError> {
        let idx = batch
            .schema()
            .index_of(name)
            .map_err(|_| ApiError::internal(format!("missing parquet column {name}")))?;
        let col = batch.column(idx);
        if let Some(arr) = col.as_any().downcast_ref::<StringArray>() {
            return Ok(Self::Utf8(arr));
        }
        if let Some(arr) = col.as_any().downcast_ref::<LargeStringArray>() {
            return Ok(Self::Large(arr));
        }
        Err(ApiError::internal(format!(
            "parquet column {name} has unexpected type {:?}",
            col.data_type()
        )))
    }

    fn opt(&self, row: usize) -> Option<&'a str> {
        match self {
            Self::Utf8(arr) => (!arr.is_null(row)).then(|| arr.value(row)),
            Self::Large(arr) => (!arr.is_null(row)).then(|| arr.value(row)),
        }
    }

    fn req(&self, row: usize, name: &str) -> Result<&'a str, ApiError> {
        self.opt(row).ok_or_else(|| {
            ApiError::internal(format!("parquet column {name} is null at row {row}"))
        })
    }
}

fn i64_col<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Int64Array, ApiError> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| ApiError::internal(format!("missing parquet column {name}")))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(|| ApiError::internal(format!("parquet column {name} is not Int64")))
}

fn i64_req(arr: &Int64Array, row: usize, name: &str) -> Result<i64, ApiError> {
    if arr.is_null(row) {
        return Err(ApiError::internal(format!(
            "parquet column {name} is null at row {row}"
        )));
    }
    Ok(arr.value(row))
}

fn i64_opt(arr: &Int64Array, row: usize) -> Option<i64> {
    (!arr.is_null(row)).then(|| arr.value(row))
}

fn parse_dec_req(col: &StrCol<'_>, row: usize, name: &str) -> Result<rust_decimal::Decimal, ApiError> {
    let raw = col.req(row, name)?;
    parse_decimal_str(raw)
        .map_err(|e| ApiError::internal(format!("invalid decimal in column {name}: {e}")))
}

fn parse_dec_opt(
    col: &StrCol<'_>,
    row: usize,
    name: &str,
) -> Result<Option<rust_decimal::Decimal>, ApiError> {
    match col.opt(row) {
        Some(raw) => parse_decimal_str(raw)
            .map(Some)
            .map_err(|e| ApiError::internal(format!("invalid decimal in column {name}: {e}"))),
        None => Ok(None),
    }
}

fn parse_str_enum<T: serde::de::DeserializeOwned>(raw: &str, col: &str) -> Result<T, ApiError> {
    serde_json::from_value(serde_json::Value::String(raw.to_owned()))
        .map_err(|e| ApiError::internal(format!("invalid enum value {raw:?} in column {col}: {e}")))
}

fn read_rounds_parquet(path: &Path) -> Result<Vec<RunRoundRecord>, ApiError> {
    if !path.is_file() {
        return Err(ApiError::not_found(
            NOT_FOUND,
            format!("rounds artifact missing: {}", path.display()),
        ));
    }
    let file = File::open(path)
        .map_err(|e| ApiError::internal(format!("open {}: {e}", path.display())))?;
    let reader = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| ApiError::internal(format!("read parquet {}: {e}", path.display())))?
        .build()
        .map_err(|e| ApiError::internal(format!("build parquet reader {}: {e}", path.display())))?;

    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res
            .map_err(|e| ApiError::internal(format!("parquet batch error: {e}")))?;
        if batch.num_rows() == 0 {
            continue;
        }
        let round_id = StrCol::from_batch(&batch, "round_id")?;
        let sub_account_id = StrCol::from_batch(&batch, "sub_account_id")?;
        let currency = StrCol::from_batch(&batch, "currency")?;
        let instrument_id = StrCol::from_batch(&batch, "instrument_id")?;
        let direction = StrCol::from_batch(&batch, "direction")?;
        let phase = StrCol::from_batch(&batch, "phase")?;
        let opened_ts = i64_col(&batch, "opened_ts")?;
        let closed_ts = i64_col(&batch, "closed_ts")?;
        let entry_price = StrCol::from_batch(&batch, "entry_price")?;
        let exit_price = StrCol::from_batch(&batch, "exit_price")?;
        let max_quantity = StrCol::from_batch(&batch, "max_quantity")?;
        let remaining_quantity = StrCol::from_batch(&batch, "remaining_quantity")?;
        let stop = StrCol::from_batch(&batch, "stop")?;
        let take_profit_json = StrCol::from_batch(&batch, "take_profit_json")?;
        let exit_reason = StrCol::from_batch(&batch, "exit_reason")?;
        let raw_exit_reason = StrCol::from_batch(&batch, "raw_exit_reason")?;
        let net_pnl = StrCol::from_batch(&batch, "net_pnl")?;
        let fees = StrCol::from_batch(&batch, "fees")?;
        let funding = StrCol::from_batch(&batch, "funding")?;
        let liquidation_fees = StrCol::from_batch(&batch, "liquidation_fees")?;
        let mae = StrCol::from_batch(&batch, "mae")?;
        let mfe = StrCol::from_batch(&batch, "mfe")?;
        let fills_json = StrCol::from_batch(&batch, "fills_json")?;
        let barrier_changes_json = StrCol::from_batch(&batch, "barrier_changes_json")?;

        for row in 0..batch.num_rows() {
            let tp_raw: Vec<String> =
                serde_json::from_str(take_profit_json.req(row, "take_profit_json")?).map_err(
                    |e| ApiError::internal(format!("invalid take_profit_json: {e}")),
                )?;
            let take_profit = tp_raw
                .iter()
                .map(|s| {
                    parse_decimal_str(s)
                        .map_err(|e| ApiError::internal(format!("invalid take_profit decimal: {e}")))
                })
                .collect::<Result<Vec<_>, _>>()?;

            let fills: Vec<RunRoundFill> =
                serde_json::from_str(fills_json.req(row, "fills_json")?)
                    .map_err(|e| ApiError::internal(format!("invalid fills_json: {e}")))?;
            let barrier_changes: Vec<RunBarrierChange> =
                serde_json::from_str(barrier_changes_json.req(row, "barrier_changes_json")?)
                    .map_err(|e| ApiError::internal(format!("invalid barrier_changes_json: {e}")))?;

            let parsed_exit_reason = exit_reason
                .opt(row)
                .map(|s| parse_str_enum::<RunFillReason>(s, "exit_reason"))
                .transpose()?;

            out.push(RunRoundRecord {
                round_id: round_id.req(row, "round_id")?.to_owned(),
                sub_account_id: sub_account_id.req(row, "sub_account_id")?.to_owned(),
                currency: currency.req(row, "currency")?.to_owned(),
                instrument_id: instrument_id.req(row, "instrument_id")?.to_owned(),
                direction: parse_str_enum::<Direction>(
                    direction.req(row, "direction")?,
                    "direction",
                )?,
                phase: parse_str_enum::<RunRoundPhase>(phase.req(row, "phase")?, "phase")?,
                opened_ts: i64_opt(opened_ts, row),
                closed_ts: i64_opt(closed_ts, row),
                entry_price: parse_dec_opt(&entry_price, row, "entry_price")?,
                exit_price: parse_dec_opt(&exit_price, row, "exit_price")?,
                max_quantity: parse_dec_req(&max_quantity, row, "max_quantity")?,
                remaining_quantity: parse_dec_req(&remaining_quantity, row, "remaining_quantity")?,
                stop: parse_dec_req(&stop, row, "stop")?,
                take_profit,
                exit_reason: parsed_exit_reason,
                raw_exit_reason: raw_exit_reason.opt(row).map(str::to_owned),
                net_pnl: parse_dec_req(&net_pnl, row, "net_pnl")?,
                fees: parse_dec_req(&fees, row, "fees")?,
                funding: parse_dec_req(&funding, row, "funding")?,
                liquidation_fees: parse_dec_req(&liquidation_fees, row, "liquidation_fees")?,
                mae: parse_dec_opt(&mae, row, "mae")?,
                mfe: parse_dec_opt(&mfe, row, "mfe")?,
                fills,
                barrier_changes,
            });
        }
    }
    Ok(out)
}

fn read_equity_parquet(path: &Path) -> Result<Vec<RunEquityPoint>, ApiError> {
    if !path.is_file() {
        return Err(ApiError::not_found(
            NOT_FOUND,
            format!("equity artifact missing: {}", path.display()),
        ));
    }
    let file = File::open(path)
        .map_err(|e| ApiError::internal(format!("open {}: {e}", path.display())))?;
    let reader = ParquetRecordBatchReaderBuilder::try_new(file)
        .map_err(|e| ApiError::internal(format!("read parquet {}: {e}", path.display())))?
        .build()
        .map_err(|e| ApiError::internal(format!("build parquet reader {}: {e}", path.display())))?;

    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res
            .map_err(|e| ApiError::internal(format!("parquet batch error: {e}")))?;
        if batch.num_rows() == 0 {
            continue;
        }
        let sub_account_id = StrCol::from_batch(&batch, "sub_account_id")?;
        let currency = StrCol::from_batch(&batch, "currency")?;
        let seq = i64_col(&batch, "seq")?;
        let ts_event = i64_col(&batch, "ts_event")?;
        let event_id = StrCol::from_batch(&batch, "event_id")?;
        let event_kind = StrCol::from_batch(&batch, "event_kind")?;
        let round_id = StrCol::from_batch(&batch, "round_id")?;
        let pnl_basis = StrCol::from_batch(&batch, "pnl_basis")?;
        let realized_pnl_delta = StrCol::from_batch(&batch, "realized_pnl_delta")?;
        let fee_delta = StrCol::from_batch(&batch, "fee_delta")?;
        let funding_delta = StrCol::from_batch(&batch, "funding_delta")?;
        let liquidation_fee_delta = StrCol::from_batch(&batch, "liquidation_fee_delta")?;
        let net_pnl = StrCol::from_batch(&batch, "net_pnl")?;
        let equity = StrCol::from_batch(&batch, "equity")?;
        let peak_pnl = StrCol::from_batch(&batch, "peak_pnl")?;
        let drawdown = StrCol::from_batch(&batch, "drawdown")?;
        let max_drawdown = StrCol::from_batch(&batch, "max_drawdown")?;

        for row in 0..batch.num_rows() {
            out.push(RunEquityPoint {
                sub_account_id: sub_account_id.req(row, "sub_account_id")?.to_owned(),
                currency: currency.req(row, "currency")?.to_owned(),
                seq: i64_req(seq, row, "seq")?,
                ts_event: i64_req(ts_event, row, "ts_event")?,
                event_id: event_id.req(row, "event_id")?.to_owned(),
                event_kind: event_kind.req(row, "event_kind")?.to_owned(),
                round_id: round_id.opt(row).map(str::to_owned),
                pnl_basis: pnl_basis.req(row, "pnl_basis")?.to_owned(),
                realized_pnl_delta: parse_dec_req(&realized_pnl_delta, row, "realized_pnl_delta")?,
                fee_delta: parse_dec_req(&fee_delta, row, "fee_delta")?,
                funding_delta: parse_dec_req(&funding_delta, row, "funding_delta")?,
                liquidation_fee_delta: parse_dec_req(
                    &liquidation_fee_delta,
                    row,
                    "liquidation_fee_delta",
                )?,
                net_pnl: parse_dec_req(&net_pnl, row, "net_pnl")?,
                equity: parse_dec_req(&equity, row, "equity")?,
                peak_pnl: parse_dec_req(&peak_pnl, row, "peak_pnl")?,
                drawdown: parse_dec_req(&drawdown, row, "drawdown")?,
                max_drawdown: parse_dec_req(&max_drawdown, row, "max_drawdown")?,
            });
        }
    }
    Ok(out)
}

async fn list_runs(State(state): State<Arc<AppState>>) -> Result<ApiJson<RunListResponse>, ApiError> {
    let data_dir = state.data_dir.clone();
    let runs = tokio::task::spawn_blocking(move || -> Result<Vec<RunExportSummary>, ApiError> {
        let runs_dir = data_dir.join("runs");
        if !runs_dir.is_dir() {
            return Ok(Vec::new());
        }
        let entries = fs::read_dir(&runs_dir).map_err(|e| {
            ApiError::internal(format!("failed to read {}: {e}", runs_dir.display()))
        })?;
        let mut out = Vec::new();
        for entry_res in entries {
            let entry = match entry_res {
                Ok(e) => e,
                Err(_) => continue,
            };
            let path = entry.path();
            if !path.is_dir() {
                continue;
            }
            match read_summary_file(&path) {
                Ok(Some(summary)) => out.push(summary),
                Ok(None) => {}
                Err(err) => {
                    tracing::warn!(dir = %path.display(), error = %err, "skipping unreadable run summary");
                }
            }
        }
        out.sort_by(|a, b| {
            Reverse(a.start.unwrap_or(0))
                .cmp(&Reverse(b.start.unwrap_or(0)))
                .then_with(|| a.run_id.cmp(&b.run_id))
        });
        Ok(out)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(RunListResponse { runs }))
}

async fn get_run_summary(
    State(state): State<Arc<AppState>>,
    ApiPath(run_id): ApiPath<String>,
) -> Result<ApiJson<RunExportSummary>, ApiError> {
    let data_dir = state.data_dir.clone();
    let summary = tokio::task::spawn_blocking(move || -> Result<RunExportSummary, ApiError> {
        let (_, summary) = require_ready_summary(&data_dir, &run_id)?;
        Ok(summary)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(summary))
}

async fn get_run_rounds(
    State(state): State<Arc<AppState>>,
    ApiPath(run_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<RunDetailQuery>,
) -> Result<ApiJson<RunRoundsResponse>, ApiError> {
    let data_dir = state.data_dir.clone();
    let resp = tokio::task::spawn_blocking(move || -> Result<RunRoundsResponse, ApiError> {
        let (run_dir, summary) = require_ready_summary(&data_dir, &run_id)?;
        let rounds_path = safe_artifact_path(&run_dir, &summary.artifacts.rounds_path)?;
        let mut rounds = read_rounds_parquet(&rounds_path)?;
        if q.sub_account_id.is_some() || q.currency.is_some() {
            rounds.retain(|r| {
                q.sub_account_id
                    .as_deref()
                    .is_none_or(|sa| r.sub_account_id == sa)
                    && q.currency.as_deref().is_none_or(|c| r.currency == c)
            });
        }
        Ok(RunRoundsResponse {
            run_id: summary.run_id,
            rounds,
        })
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(resp))
}

async fn get_run_equity(
    State(state): State<Arc<AppState>>,
    ApiPath(run_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<RunDetailQuery>,
) -> Result<ApiJson<RunEquityResponse>, ApiError> {
    let data_dir = state.data_dir.clone();
    let resp = tokio::task::spawn_blocking(move || -> Result<RunEquityResponse, ApiError> {
        let (run_dir, summary) = require_ready_summary(&data_dir, &run_id)?;
        let equity_path = safe_artifact_path(&run_dir, &summary.artifacts.equity_path)?;
        let mut points = read_equity_parquet(&equity_path)?;
        if q.sub_account_id.is_some() || q.currency.is_some() {
            points.retain(|p| {
                q.sub_account_id
                    .as_deref()
                    .is_none_or(|sa| p.sub_account_id == sa)
                    && q.currency.as_deref().is_none_or(|c| p.currency == c)
            });
        }
        Ok(RunEquityResponse {
            run_id: summary.run_id,
            pnl_basis: summary.pnl_basis,
            points,
        })
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(resp))
}
