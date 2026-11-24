//! Deterministic Key Level contracts, categories, status, and feedback payloads.

use std::collections::BTreeMap;

use serde::{Deserialize, Deserializer, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::error::ContractError;
use crate::market::{SessionTrait, TradingSession};

pub const KEY_LEVEL_METHOD_VERSION: &str = "key-levels@2";

/// Granular key level classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum KeyLevelKind {
    SwingHigh,
    SwingLow,
    PrevDayHigh,
    PrevDayLow,
    PrevDayClose,
    WeekOpen,
    MonthOpen,
    OpeningRange,
    SessionHigh,
    SessionLow,
    Gap,
    RoundNumber,
    Poc,
    Vah,
    Val,
    AnchoredVwap,
    LiquidationZone,
}

/// Stable UI category for key level filtering.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum KeyLevelCategory {
    Structure,
    Session,
    Gap,
    RoundNumber,
    Volume,
    Liquidation,
}

/// Lifecycle state of a key level.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum KeyLevelStatus {
    Active,
    Broken,
    Flipped,
}

/// Whether the level is directly observed or assumption-estimated.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EvidenceClass {
    Observed,
    Estimated,
}

/// Anchored VWAP anchor selection mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum VwapAnchor {
    Snapshot,
    Session,
    Timestamp,
}

/// Map a granular `KeyLevelKind` to its stable UI category.
pub const fn kind_category(kind: KeyLevelKind) -> KeyLevelCategory {
    match kind {
        KeyLevelKind::SwingHigh | KeyLevelKind::SwingLow => KeyLevelCategory::Structure,
        KeyLevelKind::PrevDayHigh
        | KeyLevelKind::PrevDayLow
        | KeyLevelKind::PrevDayClose
        | KeyLevelKind::WeekOpen
        | KeyLevelKind::MonthOpen
        | KeyLevelKind::OpeningRange
        | KeyLevelKind::SessionHigh
        | KeyLevelKind::SessionLow => KeyLevelCategory::Session,
        KeyLevelKind::Gap => KeyLevelCategory::Gap,
        KeyLevelKind::RoundNumber => KeyLevelCategory::RoundNumber,
        KeyLevelKind::Poc
        | KeyLevelKind::Vah
        | KeyLevelKind::Val
        | KeyLevelKind::AnchoredVwap => KeyLevelCategory::Volume,
        KeyLevelKind::LiquidationZone => KeyLevelCategory::Liquidation,
    }
}

fn default_evidence_class() -> EvidenceClass {
    EvidenceClass::Observed
}

fn default_method_version() -> String {
    KEY_LEVEL_METHOD_VERSION.to_owned()
}

/// Confirmed, lookahead-free Key Level (either single `price` or `[price_low, price_high]` zone).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct KeyLevel {
    pub level_id: String,
    pub instrument_id: String,
    pub kind: KeyLevelKind,
    pub category: KeyLevelCategory,
    #[serde(default = "default_evidence_class")]
    pub evidence_class: EvidenceClass,
    pub timeframe: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price_low: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub price_high: Option<DecimalStr>,
    pub strength: f64,
    pub status: KeyLevelStatus,
    pub ts_available: i64,
    pub formed_ts: i64,
    #[serde(default = "default_method_version")]
    pub method_version: String,
    #[serde(default)]
    pub parameters: BTreeMap<String, String>,
    pub touch_count: u64,
    #[serde(default)]
    pub last_touch_ts: Option<i64>,
    #[serde(with = "serde_decimal_str")]
    pub volume_weight: DecimalStr,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawKeyLevel {
    level_id: String,
    instrument_id: String,
    kind: KeyLevelKind,
    category: KeyLevelCategory,
    #[serde(default = "default_evidence_class")]
    evidence_class: EvidenceClass,
    timeframe: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    price_low: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    price_high: Option<DecimalStr>,
    strength: f64,
    status: KeyLevelStatus,
    ts_available: i64,
    formed_ts: i64,
    #[serde(default = "default_method_version")]
    method_version: String,
    #[serde(default)]
    parameters: BTreeMap<String, String>,
    touch_count: u64,
    #[serde(default)]
    last_touch_ts: Option<i64>,
    #[serde(with = "serde_decimal_str")]
    volume_weight: DecimalStr,
}

impl KeyLevel {
    pub fn validate(&self) -> Result<(), ContractError> {
        let has_single = self.price.is_some();
        let has_zone = self.price_low.is_some() || self.price_high.is_some();
        if has_single && has_zone {
            return Err(ContractError::Validation(
                "KeyLevel must specify either price or price_low/price_high, not both",
            ));
        }
        if !has_single && !has_zone {
            return Err(ContractError::Validation(
                "KeyLevel must specify either price or price_low/price_high",
            ));
        }
        if has_zone {
            match (self.price_low, self.price_high) {
                (Some(low), Some(high)) if low <= high => {}
                (Some(_), Some(_)) => {
                    return Err(ContractError::Validation(
                        "KeyLevel price_low cannot exceed price_high",
                    ));
                }
                _ => {
                    return Err(ContractError::Validation(
                        "Zone KeyLevel requires both price_low and price_high",
                    ));
                }
            }
        }
        if !(0.0..=1.0).contains(&self.strength) {
            return Err(ContractError::Validation(
                "KeyLevel strength must be in [0.0, 1.0]",
            ));
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for KeyLevel {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawKeyLevel::deserialize(deserializer)?;
        let level = Self {
            level_id: raw.level_id,
            instrument_id: raw.instrument_id,
            kind: raw.kind,
            category: raw.category,
            evidence_class: raw.evidence_class,
            timeframe: raw.timeframe,
            price: raw.price,
            price_low: raw.price_low,
            price_high: raw.price_high,
            strength: raw.strength,
            status: raw.status,
            ts_available: raw.ts_available,
            formed_ts: raw.formed_ts,
            method_version: raw.method_version,
            parameters: raw.parameters,
            touch_count: raw.touch_count,
            last_touch_ts: raw.last_touch_ts,
            volume_weight: raw.volume_weight,
        };
        level.validate().map_err(serde::de::Error::custom)?;
        Ok(level)
    }
}

/// Immutable set of key levels computed for a frozen snapshot & timeframe.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelSet {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub cutoff_ts: i64,
    pub as_of_ts: i64,
    #[serde(default = "default_method_version")]
    pub method_version: String,
    #[serde(default)]
    pub parameters: BTreeMap<String, String>,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default)]
    pub levels: Vec<KeyLevel>,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelIgnoreRequest {
    pub instrument_id: String,
    pub level_id: String,
    #[serde(default = "default_true")]
    pub ignored: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelIgnoreRecord {
    pub instrument_id: String,
    pub level_id: String,
    pub ignored: bool,
    pub updated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelIgnoreList {
    pub instrument_id: String,
    #[serde(default)]
    pub entries: Vec<KeyLevelIgnoreRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelAdoptionRequest {
    pub snapshot_id: String,
    pub timeframe: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelAdoptionRecord {
    pub instrument_id: String,
    pub level_id: String,
    pub snapshot_id: String,
    pub drawing_id: String,
    pub adopted_ts: i64,
    pub timeframe: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KeyLevelFeedbackSummary {
    pub instrument_id: String,
    #[serde(default)]
    pub adopted_level_ids: Vec<String>,
    #[serde(default)]
    pub ignored_level_ids: Vec<String>,
}

/// Asset-agnostic session profile projected from `SessionTrait`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SessionProfile {
    pub timezone: String,
    pub day_start: String,
    pub sessions: Vec<TradingSession>,
}

impl From<&SessionTrait> for SessionProfile {
    fn from(trait_: &SessionTrait) -> Self {
        Self {
            timezone: trait_
                .timezone
                .clone()
                .unwrap_or_else(|| "UTC".to_owned()),
            day_start: trait_.day_start.clone(),
            sessions: trait_.sessions.clone(),
        }
    }
}
