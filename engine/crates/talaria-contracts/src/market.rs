//! Asset-agnostic `InstrumentSpec` and the six Capability Traits (`TraitBundle`).

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

use crate::decimal::{DecimalStr, format_decimal_str, serde_decimal_str, serde_decimal_str_opt};
use crate::derivatives::DerivativeKind;

/// Static specification of a tradable instrument.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstrumentSpec {
    pub instrument_id: String,
    pub venue: String,
    pub symbol: String,
    pub asset_class: String,
    #[serde(with = "serde_decimal_str")]
    pub tick_size: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub qty_step: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub min_qty: DecimalStr,
    pub price_precision: i32,
    pub qty_precision: i32,
}

#[derive(Serialize)]
struct CanonicalSpecPayload {
    price_precision: i32,
    qty_precision: i32,
    qty_step: String,
    tick_size: String,
}

impl InstrumentSpec {
    /// Compute the canonical 12-character hex SHA-256 prefix matching Python's
    /// `talaria.perception.cold_store.instrument_spec_version`.
    pub fn instrument_spec_sha256(&self) -> String {
        let payload = CanonicalSpecPayload {
            price_precision: self.price_precision,
            qty_precision: self.qty_precision,
            qty_step: format_decimal_str(self.qty_step),
            tick_size: format_decimal_str(self.tick_size),
        };
        let encoded = serde_json::to_string(&payload).expect("canonical spec serialization");
        let digest = Sha256::digest(encoded.as_bytes());
        hex::encode(digest)[..12].to_owned()
    }
}

/// Market feed capability flags.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeedCapabilityTrait {
    pub realtime: bool,
    pub polling: bool,
    pub history: bool,
}

fn default_contract_multiplier() -> Decimal {
    Decimal::ONE
}

/// Margin and leverage capability specification.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarginTrait {
    pub margin_asset: String,
    #[serde(with = "serde_decimal_str")]
    pub max_leverage: DecimalStr,
    #[serde(default = "default_contract_multiplier", with = "serde_decimal_str")]
    pub contract_multiplier: DecimalStr,
}

/// Holding carry / funding settlement cadence capability.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CarryTrait {
    #[serde(default)]
    pub funding_interval_hours: Option<u32>,
}

/// Single declared trading session window (`HH:MM`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradingSession {
    pub name: String,
    pub start: String,
    pub end: String,
    #[serde(default)]
    pub timezone: Option<String>,
}

fn default_day_start() -> String {
    "00:00".to_owned()
}

/// Market trading session schedule capability.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionTrait {
    pub session_kind: String,
    #[serde(default)]
    pub timezone: Option<String>,
    #[serde(default = "default_day_start")]
    pub day_start: String,
    #[serde(default)]
    pub sessions: Vec<TradingSession>,
}

/// Order submission constraints capability.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OrderConstraintTrait {
    pub post_only: bool,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub max_market_qty: Option<DecimalStr>,
}

/// Optional derivative data capability.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DerivativeCapabilityTrait {
    pub kinds: Vec<DerivativeKind>,
    pub history: bool,
    pub realtime: bool,
}

/// Complete capability bundle for an instrument (5 required traits + optional derivatives).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TraitBundle {
    pub feed: FeedCapabilityTrait,
    pub margin: MarginTrait,
    pub carry: CarryTrait,
    pub session: SessionTrait,
    pub order_constraint: OrderConstraintTrait,
    #[serde(default)]
    pub derivatives: Option<DerivativeCapabilityTrait>,
}

/// Trade position direction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Direction {
    Long,
    Short,
}

/// Read-only margin estimate projection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct MarginEstimate {
    #[serde(with = "serde_decimal_str")]
    pub equity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub initial_margin: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub maintenance_margin: DecimalStr,
}
