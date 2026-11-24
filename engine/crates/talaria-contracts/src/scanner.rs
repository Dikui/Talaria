//! Deterministic numerical scanner contracts.

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::rules::Rule;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ScannerConditionKind {
    LiquidityAnomaly,
    VolatilityExpansion,
    FundingExtreme,
    OiDivergence,
    KeyLevelEvent,
    Rule,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ScannerSeverity {
    Info,
    Warning,
    Critical,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ScannerTriggerRoute {
    #[default]
    Local,
    CoPilotConsult,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerEvidence {
    pub metric: String,
    #[serde(with = "serde_decimal_str")]
    pub value: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub threshold: Option<DecimalStr>,
    #[serde(default)]
    pub note: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerTrigger {
    pub trigger_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub kind: ScannerConditionKind,
    pub severity: ScannerSeverity,
    pub title: String,
    pub summary: String,
    #[serde(default)]
    pub evidence: Vec<ScannerEvidence>,
    #[serde(default)]
    pub tags: Vec<String>,
    pub created_ts: i64,
    pub snapshot_id: String,
    #[serde(default)]
    pub route: ScannerTriggerRoute,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerTriggerListResponse {
    pub triggers: Vec<ScannerTrigger>,
    pub total: usize,
    pub generated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerFocusItem {
    pub instrument_id: String,
    pub last_trigger_ts: i64,
    pub trigger_count: usize,
    pub severity: ScannerSeverity,
    #[serde(default)]
    pub kinds: Vec<ScannerConditionKind>,
    #[serde(default)]
    pub last_title: Option<String>,
    #[serde(default)]
    pub last_timeframe: Option<String>,
    #[serde(default)]
    pub last_snapshot_id: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerFocusResponse {
    pub group_id: String,
    pub name: String,
    pub items: Vec<ScannerFocusItem>,
    pub instrument_ids: Vec<String>,
    pub generated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerScanRequest {
    #[serde(default)]
    pub instrument_ids: Option<Vec<String>>,
    #[serde(default)]
    pub rule: Option<Rule>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScannerScanResponse {
    pub triggers: Vec<ScannerTrigger>,
    pub scanned_instruments: Vec<String>,
    pub generated_ts: i64,
}
