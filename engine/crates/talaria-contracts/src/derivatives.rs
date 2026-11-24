//! Derivative data contracts (`mark_price`, `funding_rate`, `open_interest`).

use std::collections::BTreeMap;

use serde::{Deserialize, Deserializer, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::error::ContractError;
use crate::timeframes::timeframe_ms;
use crate::types::PrecisionTier;

/// Derivative series kind.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DerivativeKind {
    MarkPrice,
    FundingRate,
    OpenInterest,
}

impl DerivativeKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::MarkPrice => "mark_price",
            Self::FundingRate => "funding_rate",
            Self::OpenInterest => "open_interest",
        }
    }
}

/// Map bar timeframe to open-interest granularity (`1m` uses `5m`).
pub fn oi_timeframe_for(timeframe: &str) -> Option<&'static str> {
    match timeframe {
        "1m" | "5m" => Some("5m"),
        "15m" => Some("15m"),
        "1h" => Some("1h"),
        "4h" => Some("4h"),
        "1d" => Some("1d"),
        _ => None,
    }
}

fn default_precision() -> PrecisionTier {
    PrecisionTier::SecondVerified
}

/// Five-timestamp derivative observation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DerivativePoint {
    pub instrument_id: String,
    pub kind: DerivativeKind,
    #[serde(default)]
    pub timeframe: Option<String>,
    #[serde(with = "serde_decimal_str")]
    pub value: DecimalStr,
    pub ts_event: i64,
    #[serde(default)]
    pub ts_published: Option<i64>,
    pub ts_available: i64,
    #[serde(default = "default_precision")]
    pub precision: PrecisionTier,
    pub ts_ingested: i64,
    pub ts_committed: i64,
    #[serde(default)]
    pub revision: u64,
    #[serde(default)]
    pub settled: Option<bool>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDerivativePointPayload {
    instrument_id: String,
    kind: DerivativeKind,
    #[serde(default)]
    timeframe: Option<String>,
    #[serde(with = "serde_decimal_str")]
    value: DecimalStr,
    ts_event: i64,
    #[serde(default)]
    ts_published: Option<i64>,
    ts_available: i64,
    #[serde(default = "default_precision")]
    precision: PrecisionTier,
    ts_ingested: i64,
    ts_committed: i64,
    #[serde(default)]
    revision: u64,
    #[serde(default)]
    settled: Option<bool>,
}

impl DerivativePoint {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.ts_available < self.ts_event {
            return Err(ContractError::InvalidTemporalOrder(
                "ts_available cannot precede ts_event",
            ));
        }
        if self.ts_committed < self.ts_ingested {
            return Err(ContractError::InvalidTemporalOrder(
                "ts_committed cannot precede ts_ingested",
            ));
        }
        Ok(())
    }

    pub fn is_funding_settlement(&self) -> bool {
        if self.kind != DerivativeKind::FundingRate {
            return false;
        }
        if let Some(settled) = self.settled {
            return settled;
        }
        self.ts_published == Some(self.ts_event)
    }

    pub fn observation_time(&self) -> Result<i64, ContractError> {
        if self.kind == DerivativeKind::MarkPrice
            && let Some(tf) = &self.timeframe
        {
            return Ok(self.ts_event + timeframe_ms(tf)?);
        }
        Ok(self.ts_event)
    }

    pub fn visible_at(&self, as_of: i64) -> bool {
        match self.observation_time() {
            Ok(obs) => {
                0 <= self.ts_event
                    && self.ts_event <= obs
                    && obs <= self.ts_available
                    && self.ts_available <= as_of
            }
            Err(_) => false,
        }
    }
}

impl<'de> Deserialize<'de> for DerivativePoint {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawDerivativePointPayload::deserialize(deserializer)?;
        let point = Self {
            instrument_id: raw.instrument_id,
            kind: raw.kind,
            timeframe: raw.timeframe,
            value: raw.value,
            ts_event: raw.ts_event,
            ts_published: raw.ts_published,
            ts_available: raw.ts_available,
            precision: raw.precision,
            ts_ingested: raw.ts_ingested,
            ts_committed: raw.ts_committed,
            revision: raw.revision,
            settled: raw.settled,
        };
        point.validate().map_err(serde::de::Error::custom)?;
        Ok(point)
    }
}

/// Filter by `ts_available <= as_of` and select latest revision per identity.
pub fn latest_visible_derivatives(
    points: impl IntoIterator<Item = DerivativePoint>,
    as_of: Option<i64>,
) -> Vec<DerivativePoint> {
    type Key = (String, DerivativeKind, String, bool, i64);
    let mut selected: BTreeMap<Key, DerivativePoint> = BTreeMap::new();
    for point in points {
        if let Some(cutoff) = as_of
            && point.ts_available > cutoff
        {
            continue;
        }
        let key: Key = (
            point.instrument_id.clone(),
            point.kind,
            point.timeframe.clone().unwrap_or_default(),
            point.is_funding_settlement(),
            point.ts_event,
        );
        let replace = match selected.get(&key) {
            None => true,
            Some(prev) => {
                (point.revision, point.ts_available, point.ts_ingested)
                    >= (prev.revision, prev.ts_available, prev.ts_ingested)
            }
        };
        if replace {
            selected.insert(key, point);
        }
    }
    let mut out: Vec<DerivativePoint> = selected.into_values().collect();
    out.sort_by(|a, b| {
        (
            a.ts_event,
            &a.instrument_id,
            a.kind,
            a.timeframe.as_deref().unwrap_or(""),
            a.is_funding_settlement(),
        )
            .cmp(&(
                b.ts_event,
                &b.instrument_id,
                b.kind,
                b.timeframe.as_deref().unwrap_or(""),
                b.is_funding_settlement(),
            ))
    });
    out
}

/// Derivative series for a single kind/timeframe.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DerivativeSeries {
    pub kind: DerivativeKind,
    pub timeframe: Option<String>,
    pub points: Vec<DerivativePoint>,
}

/// Snapshot latest derivative summary.
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DerivativeSummary {
    #[serde(default, with = "serde_decimal_str_opt")]
    pub mark_price: Option<DecimalStr>,
    #[serde(default)]
    pub mark_price_ts: Option<i64>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub funding_rate: Option<DecimalStr>,
    #[serde(default)]
    pub funding_settled_ts: Option<i64>,
    #[serde(default)]
    pub next_funding_ts: Option<i64>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub open_interest: Option<DecimalStr>,
    #[serde(default)]
    pub open_interest_ts: Option<i64>,
    #[serde(default)]
    pub oi_history_available: bool,
}

/// Response for `GET /api/snapshots/{id}/derivatives`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DerivativeBundle {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub cutoff_ts: i64,
    pub series: Vec<DerivativeSeries>,
    pub summary: DerivativeSummary,
}

/// Raw connector derivative point prior to instrument binding.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RawDerivativePoint {
    pub kind: DerivativeKind,
    pub symbol: String,
    pub timeframe: Option<String>,
    pub ts_event: i64,
    #[serde(with = "serde_decimal_str")]
    pub value: DecimalStr,
    pub ts_available: i64,
    pub ts_ingested: i64,
    pub ts_committed: i64,
    #[serde(default)]
    pub ts_published: Option<i64>,
    #[serde(default = "default_precision")]
    pub precision: PrecisionTier,
}

impl RawDerivativePoint {
    pub fn to_point(&self, instrument_id: impl Into<String>) -> DerivativePoint {
        DerivativePoint {
            instrument_id: instrument_id.into(),
            kind: self.kind,
            timeframe: self.timeframe.clone(),
            value: self.value,
            ts_event: self.ts_event,
            ts_published: self.ts_published,
            ts_available: self.ts_available,
            precision: self.precision,
            ts_ingested: self.ts_ingested,
            ts_committed: self.ts_committed,
            revision: 0,
            settled: if self.kind == DerivativeKind::FundingRate {
                Some(true)
            } else {
                None
            },
        }
    }
}
