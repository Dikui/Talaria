//! Core L0 temporal precision tiers and 5-timestamp causal provenance (`TemporalStamp`).

use serde::{Deserialize, Deserializer, Serialize};

use crate::error::ContractError;

/// Confidence level of an `Available Time` timestamp.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PrecisionTier {
    SecondVerified,
    SessionBound,
    DateConservative,
}

impl PrecisionTier {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::SecondVerified => "SECOND_VERIFIED",
            Self::SessionBound => "SESSION_BOUND",
            Self::DateConservative => "DATE_CONSERVATIVE",
        }
    }
}

/// Five-timestamp causal provenance record (UTC milliseconds).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct TemporalStamp {
    pub ts_event: i64,
    #[serde(default)]
    pub ts_published: Option<i64>,
    pub ts_available: i64,
    pub precision: PrecisionTier,
    pub ts_ingested: i64,
    pub ts_committed: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawTemporalStamp {
    ts_event: i64,
    #[serde(default)]
    ts_published: Option<i64>,
    ts_available: i64,
    precision: PrecisionTier,
    ts_ingested: i64,
    ts_committed: i64,
}

impl TemporalStamp {
    /// Construct and validate a 5-timestamp `TemporalStamp`.
    pub fn new(
        ts_event: i64,
        ts_published: Option<i64>,
        ts_available: i64,
        precision: PrecisionTier,
        ts_ingested: i64,
        ts_committed: i64,
    ) -> Result<Self, ContractError> {
        let stamp = Self {
            ts_event,
            ts_published,
            ts_available,
            precision,
            ts_ingested,
            ts_committed,
        };
        stamp.validate()?;
        Ok(stamp)
    }

    /// Enforce causal timestamp invariants: `ts_available >= ts_event` and `ts_committed >= ts_ingested`.
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
}

impl<'de> Deserialize<'de> for TemporalStamp {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawTemporalStamp::deserialize(deserializer)?;
        let stamp = Self {
            ts_event: raw.ts_event,
            ts_published: raw.ts_published,
            ts_available: raw.ts_available,
            precision: raw.precision,
            ts_ingested: raw.ts_ingested,
            ts_committed: raw.ts_committed,
        };
        stamp.validate().map_err(serde::de::Error::custom)?;
        Ok(stamp)
    }
}
