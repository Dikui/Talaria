//! Alert rule lifecycle and trigger history contracts.

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str_opt};
use crate::rules::Rule;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AlertTriggerKind {
    MetricThreshold,
    IndicatorSignal,
    OverlayCross,
    LevelEvent,
    Rule,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AlertStatus {
    Draft,
    Armed,
    Cooldown,
    Expired,
    Disabled,
}

fn default_true() -> bool {
    true
}

fn default_cooldown_ms() -> u64 {
    60_000
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertCreate {
    pub instrument_id: String,
    pub timeframe: String,
    pub condition: Rule,
    #[serde(default)]
    pub note: String,
    #[serde(default)]
    pub draft: bool,
    #[serde(default = "default_true")]
    pub repeat: bool,
    #[serde(default = "default_cooldown_ms")]
    pub cooldown_ms: u64,
    #[serde(default)]
    pub expires_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertPatch {
    #[serde(default)]
    pub condition: Option<Rule>,
    #[serde(default)]
    pub note: Option<String>,
    #[serde(default)]
    pub repeat: Option<bool>,
    #[serde(default)]
    pub cooldown_ms: Option<u64>,
    #[serde(default)]
    pub expires_at: Option<i64>,
    #[serde(default)]
    pub enabled: Option<bool>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertRecord {
    pub alert_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub condition: Rule,
    pub status: AlertStatus,
    pub repeat: bool,
    pub cooldown_ms: u64,
    #[serde(default)]
    pub expires_at: Option<i64>,
    #[serde(default)]
    pub note: String,
    pub created_ts: i64,
    pub updated_ts: i64,
    #[serde(default)]
    pub last_triggered_ts: Option<i64>,
    #[serde(default)]
    pub next_arm_ts: Option<i64>,
    #[serde(default)]
    pub trigger_count: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertListResponse {
    pub alerts: Vec<AlertRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertTriggerRecord {
    pub trigger_id: String,
    pub alert_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub condition_kind: AlertTriggerKind,
    pub triggered_ts: i64,
    pub snapshot_id: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub observed: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub threshold: Option<DecimalStr>,
    pub message: String,
    #[serde(default)]
    pub note: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AlertTriggerListResponse {
    pub triggers: Vec<AlertTriggerRecord>,
}
