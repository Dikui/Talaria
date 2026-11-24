//! Bar status, compact chart bars, raw connector bars, and range query contracts.

use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_normalized, serde_decimal_str_opt};
use crate::error::ContractError;
use crate::types::PrecisionTier;

pub const MAX_RANGE_BARS: usize = 10_000;

/// Source status of a compact chart bar.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum BarStatus {
    Normal,
    NoTrades,
    FeedGap,
}

/// Classify a zero-volume flat bar as `NO_TRADES`, otherwise `NORMAL`.
pub fn classify_flat_bar(
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
) -> BarStatus {
    if volume.is_zero() && open == high && high == low && low == close {
        BarStatus::NoTrades
    } else {
        BarStatus::Normal
    }
}

/// Chart-consumable compact bar (`timestamp == ts_event`, UTC ms).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct CompactBar {
    pub timestamp: i64,
    #[serde(with = "serde_decimal_str_opt")]
    pub open: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    pub high: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    pub low: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    pub close: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    pub volume: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    pub turnover: Option<DecimalStr>,
    pub status: BarStatus,
    pub ts_available: i64,
    pub precision: PrecisionTier,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawCompactBar {
    timestamp: i64,
    #[serde(with = "serde_decimal_str_opt")]
    open: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    high: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    low: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    close: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    volume: Option<DecimalStr>,
    #[serde(with = "serde_decimal_str_opt")]
    turnover: Option<DecimalStr>,
    status: BarStatus,
    ts_available: i64,
    precision: PrecisionTier,
}

impl CompactBar {
    /// Validate `FEED_GAP` nullness vs `NORMAL`/`NO_TRADES` completeness.
    pub fn validate(&self) -> Result<(), ContractError> {
        let fields = [
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.turnover,
        ];
        match self.status {
            BarStatus::FeedGap => {
                if fields.iter().any(Option::is_some) {
                    return Err(ContractError::Validation(
                        "FEED_GAP bar must have all OHLCV/turnover fields as null",
                    ));
                }
            }
            BarStatus::Normal | BarStatus::NoTrades => {
                if fields.iter().any(Option::is_none) {
                    return Err(ContractError::Validation(
                        "NORMAL/NO_TRADES bar must have complete OHLCV/turnover fields",
                    ));
                }
            }
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for CompactBar {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawCompactBar::deserialize(deserializer)?;
        let bar = Self {
            timestamp: raw.timestamp,
            open: raw.open,
            high: raw.high,
            low: raw.low,
            close: raw.close,
            volume: raw.volume,
            turnover: raw.turnover,
            status: raw.status,
            ts_available: raw.ts_available,
            precision: raw.precision,
        };
        bar.validate().map_err(serde::de::Error::custom)?;
        Ok(bar)
    }
}

fn default_true() -> bool {
    true
}

/// Standardized raw bar emitted by connectors and stored in cold/raw archive.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawBar {
    pub symbol: String,
    pub timeframe: String,
    pub ts_event: i64,
    #[serde(with = "serde_decimal_str")]
    pub open: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub high: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub close: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub volume: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub turnover: DecimalStr,
    pub ts_available: i64,
    pub ts_ingested: i64,
    pub ts_committed: i64,
    #[serde(default = "default_precision")]
    pub precision: PrecisionTier,
    #[serde(default)]
    pub revision: u64,
    #[serde(default = "default_true")]
    pub confirmed: bool,
    #[serde(default)]
    pub history_only: bool,
    #[serde(default)]
    pub synthetic: bool,
}

fn default_precision() -> PrecisionTier {
    PrecisionTier::SecondVerified
}

impl RawBar {
    /// Project a raw bar into a `CompactBar` unless `status` is `FEED_GAP`.
    pub fn to_compact(&self, status: BarStatus) -> Result<CompactBar, ContractError> {
        if status == BarStatus::FeedGap {
            return Err(ContractError::Validation(
                "RawBar cannot be converted to FEED_GAP",
            ));
        }
        Ok(CompactBar {
            timestamp: self.ts_event,
            open: Some(self.open),
            high: Some(self.high),
            low: Some(self.low),
            close: Some(self.close),
            volume: Some(self.volume),
            turnover: Some(self.turnover),
            status,
            ts_available: self.ts_available,
            precision: self.precision,
        })
    }
}

/// Range query bar item (normalized `DecimalStr` representation on the wire).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RangeBar {
    pub ts_event: i64,
    #[serde(with = "serde_decimal_str_normalized")]
    pub open: DecimalStr,
    #[serde(with = "serde_decimal_str_normalized")]
    pub high: DecimalStr,
    #[serde(with = "serde_decimal_str_normalized")]
    pub low: DecimalStr,
    #[serde(with = "serde_decimal_str_normalized")]
    pub close: DecimalStr,
    #[serde(with = "serde_decimal_str_normalized")]
    pub volume: DecimalStr,
    #[serde(with = "serde_decimal_str_normalized")]
    pub turnover: DecimalStr,
    pub ts_available: i64,
    pub precision: PrecisionTier,
    pub confirmed: bool,
    pub revision: u64,
}

/// Range query parameters (`GET /api/bars`, `GET /api/bars.arrow`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct BarsQuery {
    pub instrument: String,
    pub timeframe: String,
    pub start: i64,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub as_of: Option<i64>,
    #[serde(default)]
    pub include_open: bool,
    #[serde(default)]
    pub tail_only: bool,
    #[serde(default)]
    pub resync_from: Option<i64>,
    #[serde(default)]
    pub available_since: Option<i64>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawBarsQuery {
    instrument: String,
    timeframe: String,
    start: i64,
    #[serde(default)]
    end: Option<i64>,
    #[serde(default)]
    as_of: Option<i64>,
    #[serde(default)]
    include_open: bool,
    #[serde(default)]
    tail_only: bool,
    #[serde(default)]
    resync_from: Option<i64>,
    #[serde(default)]
    available_since: Option<i64>,
}

impl BarsQuery {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.instrument.is_empty() {
            return Err(ContractError::Validation("instrument must not be empty"));
        }
        if self.start < 0 {
            return Err(ContractError::Validation("start must be non-negative"));
        }
        if let Some(end) = self.end
            && end < self.start
        {
            return Err(ContractError::Validation("end must not precede start"));
        }
        if let Some(as_of) = self.as_of
            && as_of < 0
        {
            return Err(ContractError::Validation("as_of must be non-negative"));
        }
        if (self.available_since.is_some() || self.resync_from.is_some()) && self.include_open {
            return Err(ContractError::Validation(
                "strategy resync only returns confirmed revisions",
            ));
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for BarsQuery {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawBarsQuery::deserialize(deserializer)?;
        let query = Self {
            instrument: raw.instrument,
            timeframe: raw.timeframe,
            start: raw.start,
            end: raw.end,
            as_of: raw.as_of,
            include_open: raw.include_open,
            tail_only: raw.tail_only,
            resync_from: raw.resync_from,
            available_since: raw.available_since,
        };
        query.validate().map_err(serde::de::Error::custom)?;
        Ok(query)
    }
}

/// Bars response sorted ascending by `ts_event`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BarsResponse {
    pub instrument_id: String,
    pub timeframe: String,
    pub generation: u64,
    pub bars: Vec<RangeBar>,
}
