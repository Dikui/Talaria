//! Errors returned by deterministic operators and the Key Level engine.

use thiserror::Error;

/// Error raised by operator validation, arithmetic, or Key Level evaluation.
#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum OperatorError {
    #[error("unknown indicator: {0}")]
    UnknownIndicator(String),

    #[error("{name} period must be in 1..={max}, got {period}")]
    InvalidPeriod {
        name: &'static str,
        period: u32,
        max: u32,
    },

    #[error("BOLL period must be at least 2, got {0}")]
    InvalidBollPeriod(u32),

    #[error("BOLL std_dev must be finite and positive, got {0}")]
    InvalidStdDev(String),

    #[error("{0} does not accept std_dev parameter")]
    UnexpectedStdDev(String),

    #[error("{0} does not accept anchor parameter")]
    UnexpectedAnchor(String),

    #[error("{0} requires derivative samples")]
    MissingDerivativeSamples(String),

    #[error("derivative samples length ({samples}) must match bars length ({bars})")]
    DerivativeLengthMismatch { bars: usize, samples: usize },

    #[error("incremental update params do not match previous operator state")]
    SpecMismatch,

    #[error("bar is missing required OHLCV field '{field}' for status {status}")]
    MissingBarField { field: &'static str, status: String },

    #[error("decimal arithmetic overflow or out-of-range value: {0}")]
    DecimalOverflow(&'static str),

    #[error("division by zero in decimal operator")]
    DivisionByZero,

    #[error("square root of negative decimal")]
    NegativeSqrt,

    #[error("invalid key level parameter: {0}")]
    InvalidKeyLevelParam(String),

    #[error("{0} does not support legacy alert seeding")]
    UnsupportedLegacyOperator(String),
}
