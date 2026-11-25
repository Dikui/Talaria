//! Zero-I/O L0 contracts for Track B `run_export` projection (`summary.json`, `rounds.parquet`,
//! `equity.parquet`, and read-only `/api/runs*` responses).

use serde::{Deserialize, Serialize};

use crate::decimal::{
    DecimalStr, serde_decimal_str, serde_decimal_str_opt, serde_decimal_str_vec,
};
use crate::market::Direction;

fn default_schema_version() -> u32 {
    1
}

fn default_pnl_basis() -> String {
    "booked".to_owned()
}

fn default_rounds_path() -> String {
    "rounds.parquet".to_owned()
}

fn default_equity_path() -> String {
    "equity.parquet".to_owned()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunStatus {
    Succeeded,
    Failed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RunFillKind {
    EntryFilled,
    ExitFilled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RunFillReason {
    Entry,
    Sl,
    Tp,
    Time,
    Liquidation,
    Other,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RunBarrierChangeKind {
    BarrierTightened,
    BarrierAmended,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RunRoundPhase {
    PendingEntry,
    PartiallyFilledProtected,
    ActiveProtected,
    Exiting,
    Closed,
    Cancelled,
    Liquidated,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunFillSide {
    Buy,
    Sell,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RunFillTier {
    L1,
    Trade,
    #[serde(rename = "bar_1m")]
    Bar1m,
    Mark,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRoundFill {
    pub event_id: String,
    pub ts_event: i64,
    pub kind: RunFillKind,
    pub side: RunFillSide,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub quantity: DecimalStr,
    pub reason: RunFillReason,
    #[serde(default)]
    pub raw_reason: Option<String>,
    #[serde(default)]
    pub fill_tier: Option<RunFillTier>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub realized_pnl: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub fee: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunBarrierChange {
    pub event_id: String,
    pub ts_event: i64,
    pub kind: RunBarrierChangeKind,
    #[serde(with = "serde_decimal_str")]
    pub stop: DecimalStr,
    #[serde(default, with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default)]
    pub reason: Option<String>,
    #[serde(default)]
    pub amendment_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRoundRecord {
    pub round_id: String,
    pub sub_account_id: String,
    pub currency: String,
    pub instrument_id: String,
    pub direction: Direction,
    pub phase: RunRoundPhase,
    #[serde(default)]
    pub opened_ts: Option<i64>,
    #[serde(default)]
    pub closed_ts: Option<i64>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub entry_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub exit_price: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str")]
    pub max_quantity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub remaining_quantity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub stop: DecimalStr,
    #[serde(default, with = "serde_decimal_str_vec")]
    pub take_profit: Vec<DecimalStr>,
    #[serde(default)]
    pub exit_reason: Option<RunFillReason>,
    #[serde(default)]
    pub raw_exit_reason: Option<String>,
    #[serde(with = "serde_decimal_str")]
    pub net_pnl: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub fees: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub funding: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub liquidation_fees: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub mae: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub mfe: Option<DecimalStr>,
    #[serde(default)]
    pub fills: Vec<RunRoundFill>,
    #[serde(default)]
    pub barrier_changes: Vec<RunBarrierChange>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunAccountCurrencySummary {
    pub sub_account_id: String,
    pub currency: String,
    pub round_count: u64,
    pub trade_count: u64,
    #[serde(with = "serde_decimal_str")]
    pub net_pnl: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub max_drawdown: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub fees: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub funding: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub liquidation_fees: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub initial_capital: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub final_equity: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunExportArtifacts {
    #[serde(default = "default_rounds_path")]
    pub rounds_path: String,
    #[serde(default = "default_equity_path")]
    pub equity_path: String,
    #[serde(default)]
    pub overlays_path: Option<String>,
}

impl Default for RunExportArtifacts {
    fn default() -> Self {
        Self {
            rounds_path: default_rounds_path(),
            equity_path: default_equity_path(),
            overlays_path: None,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunExportSummary {
    #[serde(default = "default_schema_version")]
    pub schema_version: u32,
    pub run_id: String,
    pub status: RunStatus,
    #[serde(default)]
    pub error: Option<String>,
    pub scheme: String,
    pub fill_plugin: String,
    #[serde(default)]
    pub instrument_id: Option<String>,
    #[serde(default)]
    pub instruments: Vec<String>,
    #[serde(default)]
    pub timeframe: Option<String>,
    #[serde(default)]
    pub start: Option<i64>,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub signal_bars: Option<u64>,
    #[serde(default = "default_pnl_basis")]
    pub pnl_basis: String,
    #[serde(default)]
    pub artifacts: RunExportArtifacts,
    #[serde(default)]
    pub accounts: Vec<RunAccountCurrencySummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunListResponse {
    #[serde(default)]
    pub runs: Vec<RunExportSummary>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunRoundsResponse {
    pub run_id: String,
    #[serde(default)]
    pub rounds: Vec<RunRoundRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunEquityPoint {
    pub sub_account_id: String,
    pub currency: String,
    pub seq: i64,
    pub ts_event: i64,
    pub event_id: String,
    pub event_kind: String,
    #[serde(default)]
    pub round_id: Option<String>,
    #[serde(default = "default_pnl_basis")]
    pub pnl_basis: String,
    #[serde(with = "serde_decimal_str")]
    pub realized_pnl_delta: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub fee_delta: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub funding_delta: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub liquidation_fee_delta: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub net_pnl: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub equity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub peak_pnl: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub drawdown: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub max_drawdown: DecimalStr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RunEquityResponse {
    pub run_id: String,
    #[serde(default = "default_pnl_basis")]
    pub pnl_basis: String,
    #[serde(default)]
    pub points: Vec<RunEquityPoint>,
}
