//! Parquet `decimal128(18, s)` cold store, SQLite manifest, PIT revision helpers,
//! `HotStreamBuffer`, and `fs2` data-directory locking.

pub mod decimal_codec;
pub mod error;
pub mod hot_buffer;
pub mod lock;
pub mod manifest;
pub mod parquet_io;
pub mod revisions;
pub mod store;
pub mod types;

pub use decimal_codec::{
    cold_column_scales, decimal_to_i128, derivative_scale, i128_to_decimal, tick_column_scales,
    validate_bars, validate_derivatives, validate_quote, validate_quotes, validate_trade,
    validate_trades,
};
pub use error::{Result, StoreError};
pub use hot_buffer::{DEFAULT_MAX_BARS_PER_SERIES, HotSlot, HotStreamBuffer, SnapshotEvent};
pub use lock::{DataDirLock, LockHolder, service_lock_path};
pub use parquet_io::{
    bars_to_record_batch, derivatives_to_record_batch, quotes_to_record_batch, read_bars_parquet,
    read_derivatives_parquet, read_quotes_parquet, read_trades_parquet, trades_to_record_batch,
    write_bars_parquet, write_derivatives_parquet, write_quotes_parquet, write_trades_parquet,
};
pub use revisions::{
    latest_visible_bars, latest_visible_quotes, latest_visible_revisions, latest_visible_trades,
    range_visible_bars, reconcile_revisions, revision_window, unpublished_revisions,
};
pub use store::{ColdReader, ColdStore};
pub use types::{
    ACTIVE_TAIL_MERGE_MIN_FILES, ACTIVE_TAIL_TARGET_ROWS, ARCHIVE_SUBDIR, ArchivedBatch,
    COLD_COLUMN_SCALES_METADATA, COLD_DECIMAL_PRECISION, COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA, COLD_INSTRUMENT_VERSION_METADATA, COLD_SUBDIR, ColdBatch, ColdCursor,
    ColdGeneration, ColdManifest, ColdPhase, ColdSnapshot, CompactionReport, DerivativeBatch,
    FaultHook, MANIFEST_DB, OrphanCleanupReport, RATE_COLUMN_SCALE, RETAINED_MANIFESTS,
    RETENTION_MS, STAGING_SUBDIR, TICK_BATCH_ROWS, TickRows, partition_day, partition_month,
};
