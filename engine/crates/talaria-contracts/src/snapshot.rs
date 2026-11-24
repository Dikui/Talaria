//! Snapshot, chart pagination, and error envelope contracts.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::bars::CompactBar;

/// Snapshot causal time semantics.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SnapshotMode {
    HistoricalPit,
    ObservedReplay,
}

fn default_snapshot_mode() -> SnapshotMode {
    SnapshotMode::HistoricalPit
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotRequest {
    pub instrument_id: String,
    pub timeframes: Vec<String>,
    #[serde(default)]
    pub cutoff_ts: Option<i64>,
    #[serde(default = "default_snapshot_mode")]
    pub mode: SnapshotMode,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SnapshotMeta {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframes: Vec<String>,
    pub cutoff_ts: i64,
    pub created_ts: i64,
    pub mode: SnapshotMode,
    pub bar_counts: BTreeMap<String, usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChartPage {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub bars: Vec<CompactBar>,
    pub has_more_forward: bool,
    pub has_more_backward: bool,
}

/// Uniform error envelope for all non-2xx HTTP responses (`{code, message}`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ErrorResponse {
    pub code: String,
    pub message: String,
}
