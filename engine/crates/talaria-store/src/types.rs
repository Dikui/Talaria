//! Core metadata types, path constants, and publish phases for the cold store.

use std::path::PathBuf;
use std::sync::Arc;

use chrono::{DateTime, Utc};
use talaria_contracts::{DerivativeKind, DerivativePoint, InstrumentSpec, Quote, RawBar, TickKind, Trade};

use crate::error::{Result, StoreError};

/// Subdirectory under `data_dir` for content-addressed raw JSONL archives.
pub const ARCHIVE_SUBDIR: &str = "data/raw_archive";
/// Subdirectory under `data_dir` for staging parquet files during 4-stage publish.
pub const STAGING_SUBDIR: &str = "data/staging";
/// Subdirectory under `data_dir` for immutable cold parquet partitions.
pub const COLD_SUBDIR: &str = "data/cold";
/// Relative path under `data_dir` for the WAL manifest SQLite database.
pub const MANIFEST_DB: &str = "data/data_manifest.sqlite";

/// Minimum retention window for retired files (24 hours in milliseconds).
pub const RETENTION_MS: i64 = 24 * 60 * 60 * 1000;
/// Minimum number of subsequent manifest versions kept before pruning retired files.
pub const RETAINED_MANIFESTS: i64 = 3;
/// Maximum rows per compacted tick partition file.
pub const TICK_BATCH_ROWS: usize = 64_000;

/// Parquet schema metadata key identifying the numeric format.
pub const COLD_FORMAT_METADATA: &str = "talaria.cold.format";
/// Expected value for `COLD_FORMAT_METADATA`.
pub const COLD_FORMAT_DECIMAL128: &str = "decimal128.v1";
/// Parquet schema metadata key containing per-column decimal scales JSON.
pub const COLD_COLUMN_SCALES_METADATA: &str = "talaria.cold.column_scales";
/// Parquet schema metadata key recording the 12-char `InstrumentSpec` hash prefix.
pub const COLD_INSTRUMENT_VERSION_METADATA: &str = "talaria.cold.instrument_spec_sha256";

/// Fixed decimal128 precision written to cold Parquet files (physical `INT64`).
pub const COLD_DECIMAL_PRECISION: u8 = 18;
/// Fixed scale for funding rate columns (ADR-0017).
pub const RATE_COLUMN_SCALE: i8 = 8;

/// Publish stages where a test fault hook may be invoked.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum ColdPhase {
    ArchiveBefore,
    ArchiveAfter,
    StagingBefore,
    StagingAfter,
    QcBefore,
    QcAfter,
    RenameBefore,
    RenameAfter,
    ManifestBefore,
    ManifestAfter,
}

/// Optional synchronous hook invoked around each publish phase (primarily for crash simulation tests).
pub type FaultHook = Arc<dyn Fn(ColdPhase) -> Result<()> + Send + Sync>;

/// Result of content-addressed raw JSONL archival.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ArchivedBatch {
    /// Path relative to `data_dir`.
    pub path: PathBuf,
    pub sha256: String,
    pub bytes: usize,
}

/// An immutable generation registered in `data_manifest.sqlite`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct ColdGeneration {
    pub generation_id: String,
    pub venue: String,
    pub symbol: String,
    pub timeframe: String,
    /// Path relative to `data_dir`.
    pub parquet_path: PathBuf,
    pub sha256: String,
    pub content_sha256: String,
    pub first_ts: i64,
    pub last_ts: i64,
    pub bars: i64,
    pub committed_ts: i64,
}

/// An immutable manifest view captured in a single SQLite read transaction.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColdManifest {
    pub version: i64,
    pub generations: Vec<ColdGeneration>,
}

/// A pinned multi-dataset cold manifest snapshot for deterministic replay/queries.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColdSnapshot {
    pub bars: ColdManifest,
    pub derivatives: ColdManifest,
    pub ticks: Option<ColdManifest>,
}

/// High-watermark cursor tracked in `data_manifest.sqlite`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ColdCursor {
    pub venue: String,
    pub symbol: String,
    pub timeframe: String,
    pub generation_id: String,
    pub last_ts: i64,
    pub bars: i64,
    pub updated_ts: i64,
}

/// Summary of a compaction + retention pruning pass.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct CompactionReport {
    pub compacted_groups: usize,
    pub retired_files_added: usize,
    pub pruned_files: usize,
}

/// Summary of crash orphan cleanup.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct OrphanCleanupReport {
    pub staging_dirs_removed: usize,
    pub temp_files_removed: usize,
    pub unreferenced_parquet_removed: usize,
}

/// A validated same-month batch of confirmed `RawBar` records.
#[derive(Debug, Clone)]
pub struct ColdBatch<'a> {
    pub instrument: &'a InstrumentSpec,
    pub timeframe: &'a str,
    pub bars: &'a [RawBar],
}

/// A validated same-month batch of `DerivativePoint` records.
#[derive(Debug, Clone)]
pub struct DerivativeBatch<'a> {
    pub instrument: &'a InstrumentSpec,
    pub kind: DerivativeKind,
    pub rows: &'a [DerivativePoint],
}

/// A validated same-UTC-day batch of `Trade` or `Quote` records.
#[derive(Debug, Clone)]
pub enum TickRows<'a> {
    Trades(&'a [Trade]),
    Quotes(&'a [Quote]),
}

impl TickRows<'_> {
    pub fn kind(&self) -> TickKind {
        match self {
            Self::Trades(_) => TickKind::Trades,
            Self::Quotes(_) => TickKind::Quotes,
        }
    }
}

/// Validate a single relative path segment (venue, symbol, timeframe).
pub fn safe_path_part<'a>(value: &'a str, label: &str) -> Result<&'a str> {
    if value.is_empty() || value == "." || value == ".." || value.contains('/') || value.contains('\\')
    {
        return Err(StoreError::Store(format!("invalid {label}: {value:?}")));
    }
    Ok(value)
}

/// Compute `YYYY-MM` UTC partition directory for `ts_event` (milliseconds).
pub fn partition_month(ts_event: i64) -> Result<String> {
    if ts_event < 0 {
        return Err(StoreError::Quality(format!(
            "ts_event must be non-negative: {ts_event}"
        )));
    }
    let dt = DateTime::<Utc>::from_timestamp_millis(ts_event)
        .ok_or_else(|| StoreError::Quality(format!("invalid ts_event millis: {ts_event}")))?;
    Ok(dt.format("%Y-%m").to_string())
}

/// Compute `YYYY-MM-DD` UTC partition directory for `ts_event` (milliseconds).
pub fn partition_day(ts_event: i64) -> Result<String> {
    if ts_event < 0 {
        return Err(StoreError::Quality(format!(
            "ts_event must be non-negative: {ts_event}"
        )));
    }
    let dt = DateTime::<Utc>::from_timestamp_millis(ts_event)
        .ok_or_else(|| StoreError::Quality(format!("invalid ts_event millis: {ts_event}")))?;
    Ok(dt.format("%Y-%m-%d").to_string())
}

/// String representation of `TickKind` matching Python `TickKind.value`.
pub const fn tick_kind_str(kind: TickKind) -> &'static str {
    match kind {
        TickKind::Trades => "trades",
        TickKind::Quotes => "quotes",
    }
}

/// Parse `TickKind` from manifest `timeframe` column.
pub fn parse_tick_kind(value: &str) -> Option<TickKind> {
    match value {
        "trades" => Some(TickKind::Trades),
        "quotes" => Some(TickKind::Quotes),
        _ => None,
    }
}

/// Parse `DerivativeKind` from manifest `timeframe` column.
pub fn parse_derivative_kind(value: &str) -> Option<DerivativeKind> {
    match value {
        "mark_price" => Some(DerivativeKind::MarkPrice),
        "funding_rate" => Some(DerivativeKind::FundingRate),
        "open_interest" => Some(DerivativeKind::OpenInterest),
        _ => None,
    }
}
