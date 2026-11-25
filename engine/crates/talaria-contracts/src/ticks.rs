//! Tick-level trade and top-of-book L1 quote contracts.

use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::types::PrecisionTier;

/// Tick dataset kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TickKind {
    Trades,
    Quotes,
}

/// Trade aggressor side.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TradeSide {
    Buy,
    Sell,
}

/// Trade timestamp provenance class.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TradeAvailability {
    Measured,
    Inferred,
}

fn default_precision() -> PrecisionTier {
    PrecisionTier::SecondVerified
}

fn default_availability() -> TradeAvailability {
    TradeAvailability::Measured
}

/// Public trade tick with full causal timestamps and intra-millisecond tie-breakers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Trade {
    pub instrument_id: String,
    pub ts_event: i64,
    pub ts_available: i64,
    pub ts_ingested: i64,
    pub ts_committed: i64,
    #[serde(default)]
    pub revision: u64,
    #[serde(default = "default_precision")]
    pub precision: PrecisionTier,
    #[serde(default)]
    pub order_uncertain: bool,
    pub trade_id: Uuid,
    pub side: TradeSide,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub size: DecimalStr,
    #[serde(default = "default_availability")]
    pub availability: TradeAvailability,
    #[serde(default)]
    pub archive_day: Option<i64>,
    #[serde(default)]
    pub archive_index: Option<i64>,
    #[serde(default)]
    pub sequence: Option<i64>,
    #[serde(default)]
    pub packet_id: Option<Uuid>,
    #[serde(default)]
    pub packet_index: Option<i64>,
}

/// Top-of-book L1 quote tick with causal timestamps.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Quote {
    pub instrument_id: String,
    pub ts_event: i64,
    pub ts_available: i64,
    pub ts_ingested: i64,
    pub ts_committed: i64,
    #[serde(default)]
    pub revision: u64,
    #[serde(default = "default_precision")]
    pub precision: PrecisionTier,
    #[serde(default)]
    pub order_uncertain: bool,
    #[serde(with = "serde_decimal_str")]
    pub bid_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub bid_size: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub ask_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub ask_size: DecimalStr,
    #[serde(default)]
    pub connection_id: Option<Uuid>,
    #[serde(default)]
    pub update_id: Option<i64>,
}

/// Lightweight intrabar tick update matching Vela `ReplayTick` shape.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReplayTick {
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub open: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub high: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub low: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub volume: Option<DecimalStr>,
}

/// Lightweight JSON response for `GET /api/ticks`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicksJsonResponse {
    pub instrument_id: String,
    #[serde(default = "default_tick_kind")]
    pub kind: TickKind,
    pub start: i64,
    pub end: i64,
    #[serde(default)]
    pub as_of: Option<i64>,
    #[serde(default)]
    pub aggregated: bool,
    #[serde(default)]
    pub ticks: Vec<ReplayTick>,
}

fn default_tick_kind() -> TickKind {
    TickKind::Trades
}
