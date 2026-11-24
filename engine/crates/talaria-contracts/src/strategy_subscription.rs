//! Reliable strategy WebSocket subscription protocol (`WS /ws/strategy`) with `(epoch, seq)` resync.

use serde::{Deserialize, Serialize};

use crate::bars::RangeBar;
use crate::derivatives::{DerivativeKind, DerivativePoint};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionCursor {
    pub epoch: String,
    pub seq: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionRequest {
    pub instrument: String,
    pub timeframe: String,
    pub owner: String,
    #[serde(default)]
    pub after: Option<SubscriptionCursor>,
    #[serde(default)]
    pub include_baseline: bool,
    #[serde(default)]
    pub report_replay_through: bool,
    #[serde(default)]
    pub kind: Option<DerivativeKind>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum SubscriptionReadyType {
    Ready,
    Resync,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SubscriptionReady {
    pub epoch: String,
    pub seq: u64,
    #[serde(rename = "type")]
    pub kind: SubscriptionReadyType,
    pub server_ts: i64,
    pub started_ts: i64,
    #[serde(default)]
    pub baseline_count: usize,
    #[serde(default)]
    pub replay_through: Option<i64>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StrategyBarEventType {
    #[default]
    Bar,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyBarEvent {
    #[serde(rename = "type", default)]
    pub kind: StrategyBarEventType,
    pub instrument_id: String,
    pub timeframe: String,
    pub epoch: String,
    pub seq: Option<u64>,
    pub bar: RangeBar,
    pub content_id: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum StrategyDerivativeEventType {
    #[default]
    Derivative,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StrategyDerivativeEvent {
    #[serde(rename = "type", default)]
    pub event_type: StrategyDerivativeEventType,
    pub epoch: String,
    pub seq: Option<u64>,
    pub point: DerivativePoint,
    pub content_id: String,
}
