//! L0 contract validation and domain error codes.

use thiserror::Error;

/// Canonical error codes shared across HTTP/WS boundaries.
pub mod error_codes {
    pub const INTERNAL: &str = "INTERNAL";
    pub const UNAUTHORIZED: &str = "UNAUTHORIZED";
    pub const NOT_FOUND: &str = "NOT_FOUND";
    pub const INSTRUMENT_NOT_FOUND: &str = "INSTRUMENT_NOT_FOUND";
    pub const SNAPSHOT_NOT_FOUND: &str = "SNAPSHOT_NOT_FOUND";
    pub const DRAWING_NOT_FOUND: &str = "DRAWING_NOT_FOUND";
    pub const ALERT_NOT_FOUND: &str = "ALERT_NOT_FOUND";
    pub const ANNOTATION_NOT_FOUND: &str = "ANNOTATION_NOT_FOUND";
    pub const CONSULTATION_NOT_FOUND: &str = "CONSULTATION_NOT_FOUND";
    pub const BRIEFING_NOT_FOUND: &str = "BRIEFING_NOT_FOUND";
    pub const FEED_UNAVAILABLE: &str = "FEED_UNAVAILABLE";
    pub const NOT_IMPLEMENTED: &str = "NOT_IMPLEMENTED";
    pub const VALIDATION_ERROR: &str = "VALIDATION_ERROR";
    pub const BARS_RANGE_TOO_LARGE: &str = "BARS_RANGE_TOO_LARGE";
    pub const BARS_BACKFILL_TIMEOUT: &str = "BARS_BACKFILL_TIMEOUT";
    pub const SERVICE_LOCKED: &str = "SERVICE_LOCKED";
    pub const FEATURE_DISABLED: &str = "FEATURE_DISABLED";
    pub const SINGLE_WRITER_VIOLATION: &str = "SINGLE_WRITER_VIOLATION";
    pub const COLD_STORE_ERROR: &str = "COLD_STORE_ERROR";
    pub const COLD_STORE_INTEGRITY: &str = "COLD_STORE_INTEGRITY";
    pub const COLD_STORE_QUALITY: &str = "COLD_STORE_QUALITY";
}

/// Errors produced when parsing or validating L0 contract invariants.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ContractError {
    #[error("invalid DecimalStr `{0}`: must match ^-?(?:0|[1-9]\\d*)(?:\\.\\d+)?$")]
    InvalidDecimalStr(String),

    #[error("unknown timeframe `{0}`")]
    UnknownTimeframe(String),

    #[error("temporal ordering violation: {0}")]
    InvalidTemporalOrder(&'static str),

    #[error("contract invariant violation: {0}")]
    Validation(&'static str),
}
