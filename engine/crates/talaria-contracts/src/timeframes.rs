//! Supported bar timeframes and UTC millisecond alignment helpers.

use crate::error::ContractError;

pub const SUPPORTED_TIMEFRAMES: &[&str] = &["1m", "5m", "15m", "1h", "4h", "1d"];

/// Convert a timeframe label (`1m`, `5m`, `15m`, `1h`, `4h`, `1d`) to milliseconds.
pub fn timeframe_ms(timeframe: &str) -> Result<i64, ContractError> {
    match timeframe {
        "1m" => Ok(60_000),
        "5m" => Ok(300_000),
        "15m" => Ok(900_000),
        "1h" => Ok(3_600_000),
        "4h" => Ok(14_400_000),
        "1d" => Ok(86_400_000),
        other => Err(ContractError::UnknownTimeframe(other.to_owned())),
    }
}

/// Return the open timestamp (`ts_event`) of the last closed bar at `now_ms`.
pub const fn last_closed_bar_start(now_ms: i64, interval_ms: i64) -> i64 {
    (now_ms / interval_ms - 1) * interval_ms
}
