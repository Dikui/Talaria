//! Errors produced by market packs, connectors, and real-time transports.

use thiserror::Error;

use talaria_contracts::ContractError;

/// Unified error type for `talaria-packs`.
#[derive(Debug, Error)]
pub enum PackError {
    #[error("instrument not found: {0}")]
    InstrumentNotFound(String),

    #[error("invalid request: {0}")]
    InvalidRequest(String),

    #[error("feed unavailable: {0}")]
    FeedUnavailable(String),

    #[error("feed transport error: {0}")]
    FeedTransport(String),

    #[error("trade archive error: {0}")]
    Archive(String),

    #[error(transparent)]
    Contract(#[from] ContractError),
}
