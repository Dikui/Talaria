//! Watchlist groups, items, ordering, and feed quota status contracts.

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str_opt};
use crate::feed::FeedHealthStatus;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum WatchlistGroupKind {
    Manual,
    ScannerFocus,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum QuoteSource {
    Supervisor,
    Snapshot,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DegradationReason {
    Quota,
    NoFeed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistGroupCreate {
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistGroupUpdate {
    pub name: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistItemCreate {
    pub group_id: String,
    pub instrument_id: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistOrder {
    pub ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistItemOrder {
    pub group_id: String,
    pub ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistItem {
    pub instrument_id: String,
    pub position: u32,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub latest_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub change_pct: Option<DecimalStr>,
    #[serde(default)]
    pub quote_ts: Option<i64>,
    pub quote_source: QuoteSource,
    pub realtime: bool,
    #[serde(default)]
    pub feed_status: Option<FeedHealthStatus>,
    pub stale: bool,
    pub degraded: bool,
    #[serde(default)]
    pub degradation_reason: Option<DegradationReason>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WatchlistGroup {
    pub group_id: String,
    pub name: String,
    pub position: u32,
    pub kind: WatchlistGroupKind,
    pub is_system: bool,
    pub items: Vec<WatchlistItem>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Watchlist {
    pub groups: Vec<WatchlistGroup>,
    pub realtime_quota: u32,
    pub realtime_used: u32,
    pub generated_ts: i64,
}
