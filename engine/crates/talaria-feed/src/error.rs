//! Errors produced by `talaria-feed`.

use thiserror::Error;

use talaria_contracts::ContractError;
use talaria_packs::PackError;

/// Unified error type for `FeedSupervisor`, `ReplayTransport`, and compaction.
#[derive(Debug, Error)]
pub enum FeedError {
    #[error("instrument not found: {0}")]
    InstrumentNotFound(String),

    #[error("invalid request: {0}")]
    InvalidRequest(String),

    #[error("replay data directory cannot overlap protected production directory: {0}")]
    ProtectedDataDir(String),

    #[error("feed transport error: {0}")]
    Transport(String),

    #[error(transparent)]
    Pack(#[from] PackError),

    #[error(transparent)]
    Contract(#[from] ContractError),

    #[error(transparent)]
    Io(#[from] std::io::Error),
}
