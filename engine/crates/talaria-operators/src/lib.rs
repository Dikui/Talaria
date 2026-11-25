//! Deterministic `rust_decimal` indicator operators and Key Level engine.

pub mod error;
pub mod gda;
pub mod key_levels;
pub mod operators;
pub mod sha256;
pub mod tz;

pub use error::OperatorError;
pub use gda::GdaDecimal;
pub use key_levels::{
    KeyLevelComputation, KeyLevelParams, compute_key_level_set, compute_key_levels,
    compute_key_levels_with_notes,
};
pub use operators::{
    DerivativeSample, OperatorCursor, OperatorPoint, OperatorSpec, OperatorState,
    compute_indicator_series, compute_series, derivative_samples, latest_session_vwap,
    legacy_alert_points, update_series,
};
