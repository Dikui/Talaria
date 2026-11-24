//! Error types for the Talaria cold store, SQLite manifest, lock, and hot buffer.

use talaria_contracts::ContractError;
use thiserror::Error;

/// Convenience alias for `talaria-store` operations.
pub type Result<T> = std::result::Result<T, StoreError>;

/// Errors emitted by `talaria-store`, aligned with Python `ColdStoreError` codes.
#[derive(Debug, Error)]
pub enum StoreError {
    /// Batch failed schema, precision, monotonic ordering, OHLC, or step-size validation.
    #[error("quality error: {0}")]
    Quality(String),

    /// On-disk parquet metadata, schema, SHA-256, or path containment failed verification.
    #[error("integrity error: {0}")]
    Integrity(String),

    /// A second in-process writer attempted to open the same manifest SQLite path.
    #[error("single writer violation: {0}")]
    SingleWriterViolation(String),

    /// Another process holds `<data-dir>/runtime/service.lock`.
    #[error("service locked: {0}")]
    ServiceLocked(String),

    /// General store operation error (closed store, invalid path component, etc.).
    #[error("store error: {0}")]
    Store(String),

    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    #[error("arrow error: {0}")]
    Arrow(#[from] arrow::error::ArrowError),

    #[error("parquet error: {0}")]
    Parquet(#[from] parquet::errors::ParquetError),

    #[error("json error: {0}")]
    Json(#[from] serde_json::Error),

    #[error("contract error: {0}")]
    Contract(#[from] ContractError),
}
