//! Cold store single writer (`ColdStore`) with 4-stage atomic publish, compaction, retention,
//! orphan cleanup, and lockless snapshot/manifest reader (`ColdReader`).

use std::collections::{BTreeMap, HashSet};
use std::fs::{self, File, OpenOptions};
use std::io::{ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::Utc;
use rusqlite::Connection;
use serde::Serialize;
use sha2::{Digest, Sha256};
use talaria_contracts::{
    DerivativeKind, DerivativePoint, InstrumentSpec, Quote, RawBar, SUPPORTED_TIMEFRAMES, TickKind,
    Trade, TradeAvailability, TradeSide, format_decimal_str, format_normalized_decimal_str,
    latest_visible_derivatives,
};

use crate::decimal_codec::{
    derivative_order_cmp, quote_order_cmp, trade_order_cmp, validate_bars, validate_derivatives,
    validate_quotes, validate_trades,
};
use crate::error::{Result, StoreError};
use crate::manifest::{
    all_referenced_content_sha256, all_referenced_parquet_paths, canonical_db_key, checkpoint_wal,
    claim_writer, commit_manifest, open_reader_connection, open_writer_connection, path_under_root,
    posix_rel_path, prune_all_retired, prune_retired, read_cursor, read_manifest_version,
    release_writer, select_distinct_series, select_generation_by_id, select_generations,
    select_matching_bounds,
};
use crate::parquet_io::{
    read_bars_parquet, read_derivatives_parquet, read_quotes_parquet, read_trades_parquet,
    write_bars_parquet, write_derivatives_parquet, write_quotes_parquet, write_trades_parquet,
};
use crate::revisions::{latest_visible_bars, latest_visible_quotes, latest_visible_trades};
use crate::types::{
    ARCHIVE_SUBDIR, ArchivedBatch, COLD_SUBDIR, ColdBatch, ColdCursor, ColdGeneration, ColdManifest,
    ColdPhase, ColdSnapshot, CompactionReport, DerivativeBatch, FaultHook, MANIFEST_DB,
    OrphanCleanupReport, STAGING_SUBDIR, TICK_BATCH_ROWS, TickRows, parse_derivative_kind,
    parse_tick_kind, partition_day, partition_month, safe_path_part, tick_kind_str,
};

const ALL_DERIVATIVE_KINDS: [DerivativeKind; 3] = [
    DerivativeKind::MarkPrice,
    DerivativeKind::FundingRate,
    DerivativeKind::OpenInterest,
];
const ALL_TICK_KINDS: [TickKind; 2] = [TickKind::Trades, TickKind::Quotes];

fn current_time_ms() -> i64 {
    Utc::now().timestamp_millis()
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let digest = Sha256::digest(bytes);
    hex::encode(digest)
}

fn sha256_file(path: &Path) -> Result<String> {
    let mut file = File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buf = [0_u8; 64 * 1024];
    loop {
        let n = file.read(&mut buf)?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

fn fsync_dir(dir: &Path) -> Result<()> {
    let file = File::open(dir)?;
    file.sync_all()?;
    Ok(())
}

fn atomic_write(path: &Path, payload: &[u8]) -> Result<()> {
    let parent = path.parent().ok_or_else(|| {
        StoreError::Store(format!("cannot atomic_write without parent: {}", path.display()))
    })?;
    fs::create_dir_all(parent)?;
    let name = path
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| StoreError::Store("invalid target filename".to_owned()))?;
    let nonce = Utc::now().timestamp_nanos_opt().unwrap_or_default();
    let tmp = parent.join(format!(".{name}.{}.{nonce}.tmp", std::process::id()));

    let write_res = (|| -> Result<()> {
        let mut handle = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&tmp)?;
        handle.write_all(payload)?;
        handle.flush()?;
        handle.sync_all()?;
        drop(handle);
        fs::rename(&tmp, path)?;
        fsync_dir(parent)?;
        Ok(())
    })();

    if write_res.is_err() {
        let _ = fs::remove_file(&tmp);
    }
    write_res
}

// =========================================================================
// Canonical Archive Content Encoding
// =========================================================================

#[derive(Serialize)]
struct ArchiveBarRecord<'a> {
    close: String,
    high: String,
    low: String,
    open: String,
    precision: &'static str,
    revision: u64,
    symbol: &'a str,
    timeframe: &'a str,
    ts_available: i64,
    ts_committed: i64,
    ts_event: i64,
    ts_ingested: i64,
    turnover: String,
    volume: String,
}

fn bar_content_bytes(bars: &[RawBar], canonical_decimals: bool) -> Result<Vec<u8>> {
    let fmt = |d| {
        if canonical_decimals {
            format_normalized_decimal_str(d)
        } else {
            format_decimal_str(d)
        }
    };
    let mut out = Vec::new();
    for bar in bars {
        let rec = ArchiveBarRecord {
            close: fmt(bar.close),
            high: fmt(bar.high),
            low: fmt(bar.low),
            open: fmt(bar.open),
            precision: bar.precision.as_str(),
            revision: bar.revision,
            symbol: &bar.symbol,
            timeframe: &bar.timeframe,
            ts_available: bar.ts_available,
            ts_committed: bar.ts_committed,
            ts_event: bar.ts_event,
            ts_ingested: bar.ts_ingested,
            turnover: fmt(bar.turnover),
            volume: fmt(bar.volume),
        };
        serde_json::to_writer(&mut out, &rec)?;
        out.push(b'\n');
    }
    Ok(out)
}

#[derive(Serialize)]
struct ArchiveDerivativeRecord<'a> {
    instrument_id: &'a str,
    kind: &'static str,
    precision: &'static str,
    revision: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    settled: Option<bool>,
    timeframe: &'a Option<String>,
    ts_available: i64,
    ts_committed: i64,
    ts_event: i64,
    ts_ingested: i64,
    ts_published: Option<i64>,
    value: String,
}

fn derivative_content_bytes(rows: &[DerivativePoint]) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    for p in rows {
        let rec = ArchiveDerivativeRecord {
            instrument_id: &p.instrument_id,
            kind: p.kind.as_str(),
            precision: p.precision.as_str(),
            revision: p.revision,
            settled: p.settled,
            timeframe: &p.timeframe,
            ts_available: p.ts_available,
            ts_committed: p.ts_committed,
            ts_event: p.ts_event,
            ts_ingested: p.ts_ingested,
            ts_published: p.ts_published,
            value: format_normalized_decimal_str(p.value),
        };
        serde_json::to_writer(&mut out, &rec)?;
        out.push(b'\n');
    }
    Ok(out)
}

fn tick_content_bytes(instrument: &InstrumentSpec, rows: TickRows<'_>) -> Result<Vec<u8>> {
    let mut out = Vec::new();
    let header = serde_json::json!([instrument.instrument_id, tick_kind_str(rows.kind())]);
    serde_json::to_writer(&mut out, &header)?;
    out.push(b'\n');

    match rows {
        TickRows::Trades(trades) => {
            for t in trades {
                let side = match t.side {
                    TradeSide::Buy => "Buy",
                    TradeSide::Sell => "Sell",
                };
                let avail = match t.availability {
                    TradeAvailability::Measured => "measured",
                    TradeAvailability::Inferred => "inferred",
                };
                let row = serde_json::json!([
                    t.ts_event,
                    t.ts_available,
                    t.ts_ingested,
                    t.ts_committed,
                    t.revision,
                    t.precision.as_str(),
                    format_decimal_str(t.price),
                    format_decimal_str(t.size),
                    hex::encode(t.trade_id.as_bytes()),
                    side,
                    avail,
                    t.archive_day,
                    t.archive_index,
                    t.sequence,
                    t.packet_id.map(|p| hex::encode(p.as_bytes())),
                    t.packet_index,
                ]);
                serde_json::to_writer(&mut out, &row)?;
                out.push(b'\n');
            }
        }
        TickRows::Quotes(quotes) => {
            for q in quotes {
                let row = serde_json::json!([
                    q.ts_event,
                    q.ts_available,
                    q.ts_ingested,
                    q.ts_committed,
                    q.revision,
                    q.precision.as_str(),
                    format_decimal_str(q.bid_price),
                    format_decimal_str(q.bid_size),
                    format_decimal_str(q.ask_price),
                    format_decimal_str(q.ask_size),
                    q.connection_id.map(|c| hex::encode(c.as_bytes())),
                    q.update_id,
                ]);
                serde_json::to_writer(&mut out, &row)?;
                out.push(b'\n');
            }
        }
    }
    Ok(out)
}

fn compaction_archive_bytes(group: &[ColdGeneration]) -> Result<Vec<u8>> {
    #[derive(Serialize)]
    struct SourceRef<'a> {
        source_sha256: &'a str,
    }
    let mut out = Vec::new();
    for item in group {
        serde_json::to_writer(
            &mut out,
            &SourceRef {
                source_sha256: &item.content_sha256,
            },
        )?;
        out.push(b'\n');
    }
    Ok(out)
}

// =========================================================================
// Internal Batch Wrapper
// =========================================================================

enum PublishPayload<'a> {
    Bars(&'a ColdBatch<'a>),
    Derivatives(&'a DerivativeBatch<'a>),
    Trades {
        instrument: &'a InstrumentSpec,
        rows: &'a [Trade],
    },
    Quotes {
        instrument: &'a InstrumentSpec,
        rows: &'a [Quote],
    },
}

impl<'a> PublishPayload<'a> {
    fn instrument(&self) -> &'a InstrumentSpec {
        match self {
            Self::Bars(b) => b.instrument,
            Self::Derivatives(b) => b.instrument,
            Self::Trades { instrument, .. } => instrument,
            Self::Quotes { instrument, .. } => instrument,
        }
    }

    fn series_key(&self) -> &'a str {
        match self {
            Self::Bars(b) => b.timeframe,
            Self::Derivatives(b) => b.kind.as_str(),
            Self::Trades { .. } => "trades",
            Self::Quotes { .. } => "quotes",
        }
    }

    fn partition_key(&self) -> Result<String> {
        match self {
            Self::Bars(b) => partition_month(b.bars[0].ts_event),
            Self::Derivatives(b) => partition_month(b.rows[0].ts_event),
            Self::Trades { rows, .. } => partition_day(rows[0].ts_event),
            Self::Quotes { rows, .. } => partition_day(rows[0].ts_event),
        }
    }

    fn bounds(&self) -> (i64, i64, i64, i64) {
        match self {
            Self::Bars(b) => (
                b.bars.first().expect("non-empty").ts_event,
                b.bars.last().expect("non-empty").ts_event,
                b.bars.len() as i64,
                b.bars.iter().map(|r| r.ts_committed).max().unwrap_or(0),
            ),
            Self::Derivatives(b) => (
                b.rows.first().expect("non-empty").ts_event,
                b.rows.last().expect("non-empty").ts_event,
                b.rows.len() as i64,
                b.rows.iter().map(|r| r.ts_committed).max().unwrap_or(0),
            ),
            Self::Trades { rows, .. } => (
                rows.first().expect("non-empty").ts_event,
                rows.last().expect("non-empty").ts_event,
                rows.len() as i64,
                rows.iter().map(|r| r.ts_committed).max().unwrap_or(0),
            ),
            Self::Quotes { rows, .. } => (
                rows.first().expect("non-empty").ts_event,
                rows.last().expect("non-empty").ts_event,
                rows.len() as i64,
                rows.iter().map(|r| r.ts_committed).max().unwrap_or(0),
            ),
        }
    }
}

// =========================================================================
// ColdStore (Single Writer)
// =========================================================================

struct StoreInner {
    data_dir: PathBuf,
    db_key: Option<PathBuf>,
    conn: Option<Connection>,
    fault_hook: Option<FaultHook>,
}

impl StoreInner {
    fn hook(&self, phase: ColdPhase) -> Result<()> {
        if let Some(cb) = &self.fault_hook {
            cb(phase)?;
        }
        Ok(())
    }

    fn conn(&self) -> Result<&Connection> {
        self.conn
            .as_ref()
            .ok_or_else(|| StoreError::Store("ColdStore is closed".to_owned()))
    }

    fn conn_mut(&mut self) -> Result<&mut Connection> {
        self.conn
            .as_mut()
            .ok_or_else(|| StoreError::Store("ColdStore is closed".to_owned()))
    }

    fn close(&mut self) -> Result<()> {
        if let Some(conn) = self.conn.take() {
            let res = checkpoint_wal(&conn);
            drop(conn);
            if let Some(key) = self.db_key.take() {
                release_writer(&key);
            }
            res?;
        } else if let Some(key) = self.db_key.take() {
            release_writer(&key);
        }
        Ok(())
    }

    /// Stage 1: write content-addressed payload to `data/raw_archive/<sha256>.jsonl`.
    fn archive_content(&self, content: &[u8], sha256: &str) -> Result<ArchivedBatch> {
        self.hook(ColdPhase::ArchiveBefore)?;
        let rel = PathBuf::from(ARCHIVE_SUBDIR).join(format!("{sha256}.jsonl"));
        let full = self.data_dir.join(&rel);
        if full.exists() {
            if sha256_file(&full)? != sha256 {
                return Err(StoreError::Integrity(format!(
                    "archive content sha256 mismatch: {}",
                    rel.display()
                )));
            }
        } else {
            atomic_write(&full, content)?;
        }
        self.hook(ColdPhase::ArchiveAfter)?;
        Ok(ArchivedBatch {
            path: rel,
            sha256: sha256.to_owned(),
            bytes: content.len(),
        })
    }

    /// Stage 2a: clean any leftover staging dir and write `data/staging/<content_sha256>/bars.parquet`.
    fn write_staging(&self, archived: &ArchivedBatch, payload: &PublishPayload<'_>) -> Result<PathBuf> {
        self.hook(ColdPhase::StagingBefore)?;
        let staging_dir = self.data_dir.join(STAGING_SUBDIR).join(&archived.sha256);
        if staging_dir.exists() {
            fs::remove_dir_all(&staging_dir)?;
        }
        fs::create_dir_all(&staging_dir)?;
        let tmp = staging_dir.join("bars.parquet.tmp");
        let final_staging = staging_dir.join("bars.parquet");

        match payload {
            PublishPayload::Bars(b) => write_bars_parquet(&tmp, b.bars, b.instrument)?,
            PublishPayload::Derivatives(b) => {
                write_derivatives_parquet(&tmp, b.rows, b.instrument, b.kind)?;
            }
            PublishPayload::Trades { instrument, rows } => {
                write_trades_parquet(&tmp, rows, instrument)?;
            }
            PublishPayload::Quotes { instrument, rows } => {
                write_quotes_parquet(&tmp, rows, instrument)?;
            }
        }

        {
            let file = File::open(&tmp)?;
            file.sync_all()?;
        }
        fs::rename(&tmp, &final_staging)?;
        fsync_dir(&staging_dir)?;
        self.hook(ColdPhase::StagingAfter)?;
        Ok(final_staging)
    }

    /// Stage 2b: read back staged Parquet and verify schema, metadata, monotonicity, and row equality.
    fn quality_check(
        &self,
        staging_path: &Path,
        payload: &PublishPayload<'_>,
        compacted: bool,
    ) -> Result<()> {
        self.hook(ColdPhase::QcBefore)?;
        match payload {
            PublishPayload::Bars(b) => {
                let decoded = read_bars_parquet(staging_path, None, None)?;
                validate_bars(&decoded, b.instrument, b.timeframe, compacted)?;
                if decoded != b.bars {
                    return Err(StoreError::Integrity(
                        "staging bars parquet readback mismatches input batch".to_owned(),
                    ));
                }
            }
            PublishPayload::Derivatives(b) => {
                let decoded = read_derivatives_parquet(staging_path, None, None, None)?;
                validate_derivatives(&decoded, b.instrument, b.kind, compacted)?;
                if decoded != b.rows {
                    return Err(StoreError::Integrity(
                        "staging derivatives parquet readback mismatches input batch".to_owned(),
                    ));
                }
            }
            PublishPayload::Trades { instrument, rows } => {
                let decoded = read_trades_parquet(
                    staging_path,
                    &instrument.instrument_id,
                    None,
                    None,
                    None,
                )?;
                validate_trades(&decoded, instrument)?;
                if decoded != *rows {
                    return Err(StoreError::Integrity(
                        "staging trades parquet readback mismatches input batch".to_owned(),
                    ));
                }
            }
            PublishPayload::Quotes { instrument, rows } => {
                let decoded =
                    read_quotes_parquet(staging_path, &instrument.instrument_id, None, None)?;
                validate_quotes(&decoded, instrument)?;
                if decoded != *rows {
                    return Err(StoreError::Integrity(
                        "staging quotes parquet readback mismatches input batch".to_owned(),
                    ));
                }
            }
        }
        self.hook(ColdPhase::QcAfter)?;
        Ok(())
    }

    /// Stage 3: fsync and atomically rename staged Parquet into `data/cold/...`.
    fn promote(
        &self,
        staging_path: &Path,
        payload: &PublishPayload<'_>,
        generation_id: &str,
        compacted: bool,
    ) -> Result<PathBuf> {
        self.hook(ColdPhase::RenameBefore)?;
        let instrument = payload.instrument();
        let kind_prefix = if compacted { "partition" } else { "tail" };
        let version = instrument.instrument_spec_sha256();
        let partition = payload.partition_key()?;
        let final_path = self
            .data_dir
            .join(COLD_SUBDIR)
            .join(safe_path_part(&instrument.venue, "venue")?)
            .join(safe_path_part(&instrument.symbol, "symbol")?)
            .join(safe_path_part(payload.series_key(), "timeframe")?)
            .join(partition)
            .join(format!("{kind_prefix}-{version}-{generation_id}.parquet"));

        let parent = final_path
            .parent()
            .ok_or_else(|| StoreError::Store("missing cold parent directory".to_owned()))?;
        fs::create_dir_all(parent)?;

        if final_path.exists() {
            if sha256_file(&final_path)? != sha256_file(staging_path)? {
                return Err(StoreError::Integrity(format!(
                    "immutable generation already exists with different bytes: {}",
                    final_path.display()
                )));
            }
            let _ = fs::remove_file(staging_path);
        } else {
            fs::rename(staging_path, &final_path)?;
            fsync_dir(parent)?;
        }

        self.hook(ColdPhase::RenameAfter)?;
        if let Some(staging_dir) = staging_path.parent() {
            let _ = fs::remove_dir_all(staging_dir);
        }
        Ok(final_path)
    }

    fn prepare_generation(
        &self,
        payload: &PublishPayload<'_>,
        generation_id: &str,
        archive_content: Vec<u8>,
        compacted: bool,
    ) -> Result<ColdGeneration> {
        self.prepare_generation_with_archive_mode(
            payload,
            generation_id,
            archive_content,
            compacted,
            true,
        )
    }

    fn prepare_generation_with_archive_mode(
        &self,
        payload: &PublishPayload<'_>,
        generation_id: &str,
        archive_content: Vec<u8>,
        compacted: bool,
        write_raw_archive: bool,
    ) -> Result<ColdGeneration> {
        let content_sha = sha256_bytes(&archive_content);
        let archived = if write_raw_archive {
            self.archive_content(&archive_content, &content_sha)?
        } else {
            ArchivedBatch {
                path: PathBuf::from(ARCHIVE_SUBDIR).join(format!("{content_sha}.jsonl")),
                sha256: content_sha,
                bytes: archive_content.len(),
            }
        };
        let staging_path = self.write_staging(&archived, payload)?;
        self.quality_check(&staging_path, payload, compacted)?;
        let final_path = self.promote(&staging_path, payload, generation_id, compacted)?;

        let rel_path = final_path
            .strip_prefix(&self.data_dir)
            .map_err(|_| StoreError::Integrity("promoted parquet outside data_dir".to_owned()))?
            .to_path_buf();
        let parquet_sha = sha256_file(&final_path)?;
        let (first_ts, last_ts, bars, committed_ts) = payload.bounds();
        let instrument = payload.instrument();

        Ok(ColdGeneration {
            generation_id: generation_id.to_owned(),
            venue: instrument.venue.clone(),
            symbol: instrument.symbol.clone(),
            timeframe: payload.series_key().to_owned(),
            parquet_path: rel_path,
            sha256: parquet_sha,
            content_sha256: archived.sha256,
            first_ts,
            last_ts,
            bars,
            committed_ts,
        })
    }

    fn commit_generation(
        &mut self,
        generation: &ColdGeneration,
        replaced: &[ColdGeneration],
        retired_ts: Option<i64>,
    ) -> Result<i64> {
        self.hook(ColdPhase::ManifestBefore)?;
        let ts = retired_ts.unwrap_or_else(current_time_ms);
        let conn = self.conn_mut()?;
        let version = commit_manifest(conn, generation, replaced, ts)?;
        self.hook(ColdPhase::ManifestAfter)?;
        Ok(version)
    }

    fn compact_ticks(
        &mut self,
        instrument: &InstrumentSpec,
        kind: TickKind,
        now_ms: i64,
        report: &mut CompactionReport,
    ) -> Result<()> {
        let version = instrument.instrument_spec_sha256();
        let today = partition_day(now_ms)?;
        let tail_prefix = format!("tail-{version}-");
        let partition_prefix = format!("partition-{version}-");

        let generations =
            select_generations(self.conn()?, instrument, tick_kind_str(kind), None, None)?;
        let mut days: BTreeMap<String, Vec<ColdGeneration>> = BTreeMap::new();
        for generation_item in generations {
            let day = partition_day(generation_item.first_ts)?;
            let name = generation_item
                .parquet_path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("");
            if day < today && (name.starts_with(&tail_prefix) || name.starts_with(&partition_prefix))
            {
                days.entry(day).or_default().push(generation_item);
            }
        }

        for (_day, day_gens) in days {
            let mut group: Vec<ColdGeneration> = Vec::new();
            let mut size: usize = 0;
            for generation_item in day_gens {
                let rows_count = usize::try_from(generation_item.bars).unwrap_or(0);
                if !group.is_empty() && size + rows_count > TICK_BATCH_ROWS {
                    self.compact_tick_group(instrument, kind, &group, now_ms, report)?;
                    group.clear();
                    size = 0;
                }
                size += rows_count;
                group.push(generation_item);
            }
            self.compact_tick_group(instrument, kind, &group, now_ms, report)?;
        }
        Ok(())
    }

    fn compact_tick_group(
        &mut self,
        instrument: &InstrumentSpec,
        kind: TickKind,
        group: &[ColdGeneration],
        now_ms: i64,
        report: &mut CompactionReport,
    ) -> Result<()> {
        let has_tail = group.iter().any(|item| {
            item.parquet_path
                .file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|s| s.starts_with("tail-"))
        });
        if group.len() < 2 || !has_tail {
            return Ok(());
        }

        let archive = compaction_archive_bytes(group)?;
        let mut gen_hasher = Sha256::new();
        gen_hasher.update(b"tick-partition\n");
        gen_hasher.update(&archive);
        let generation_id = hex::encode(gen_hasher.finalize());

        let new_gen = match kind {
            TickKind::Trades => {
                let mut rows = Vec::new();
                for item in group {
                    let path = verify_generation_file(&self.data_dir, item)?;
                    rows.extend(read_trades_parquet(
                        &path,
                        &instrument.instrument_id,
                        None,
                        None,
                        None,
                    )?);
                }
                rows.sort_by(trade_order_cmp);
                self.prepare_generation(
                    &PublishPayload::Trades {
                        instrument,
                        rows: &rows,
                    },
                    &generation_id,
                    archive,
                    true,
                )?
            }
            TickKind::Quotes => {
                let mut rows = Vec::new();
                for item in group {
                    let path = verify_generation_file(&self.data_dir, item)?;
                    rows.extend(read_quotes_parquet(
                        &path,
                        &instrument.instrument_id,
                        None,
                        None,
                    )?);
                }
                rows.sort_by(quote_order_cmp);
                self.prepare_generation(
                    &PublishPayload::Quotes {
                        instrument,
                        rows: &rows,
                    },
                    &generation_id,
                    archive,
                    true,
                )?
            }
        };

        self.commit_generation(&new_gen, group, Some(now_ms))?;
        report.compacted_groups += 1;
        report.retired_files_added += group.len();
        Ok(())
    }

    fn compact_series(
        &mut self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        now_ms: i64,
        report: &mut CompactionReport,
    ) -> Result<()> {
        let version = instrument.instrument_spec_sha256();
        let tail_prefix = format!("tail-{version}-");
        let partition_prefix = format!("partition-{version}-");

        let generations = select_generations(self.conn()?, instrument, timeframe, None, None)?;
        let mut groups: BTreeMap<String, Vec<ColdGeneration>> = BTreeMap::new();
        for generation_item in generations {
            let month = partition_month(generation_item.first_ts)?;
            let name = generation_item
                .parquet_path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("");
            if name.starts_with(&tail_prefix) || name.starts_with(&partition_prefix) {
                groups.entry(month).or_default().push(generation_item);
            } else {
                let last_month = partition_month(generation_item.last_ts)?;
                let drop_keys: Vec<String> = groups
                    .keys()
                    .filter(|gm| **gm >= month && **gm <= last_month)
                    .cloned()
                    .collect();
                for k in drop_keys {
                    groups.remove(&k);
                }
            }
        }

        for (_month, group) in groups {
            let has_tail = group.iter().any(|item| {
                item.parquet_path
                    .file_name()
                    .and_then(|n| n.to_str())
                    .is_some_and(|s| s.starts_with(&tail_prefix))
            });
            if !has_tail {
                continue;
            }
            let archive = compaction_archive_bytes(&group)?;

            let new_gen = if let Some(deriv_kind) = parse_derivative_kind(timeframe) {
                let mut points = Vec::new();
                for item in &group {
                    let path = verify_generation_file(&self.data_dir, item)?;
                    points.extend(read_derivatives_parquet(&path, None, None, None)?);
                }
                points.sort_by(derivative_order_cmp);
                let content = derivative_content_bytes(&points)?;
                let mut hasher = Sha256::new();
                hasher.update(b"partition\n");
                hasher.update(&content);
                let generation_id = hex::encode(hasher.finalize());
                let batch = DerivativeBatch {
                    instrument,
                    kind: deriv_kind,
                    rows: &points,
                };
                self.prepare_generation(
                    &PublishPayload::Derivatives(&batch),
                    &generation_id,
                    archive,
                    true,
                )?
            } else {
                let mut bars = Vec::new();
                for item in &group {
                    let path = verify_generation_file(&self.data_dir, item)?;
                    bars.extend(read_bars_parquet(&path, None, None)?);
                }
                dedup_bars_same_revision(&mut bars);
                let content = bar_content_bytes(&bars, true)?;
                let mut hasher = Sha256::new();
                hasher.update(b"partition\n");
                hasher.update(&content);
                let generation_id = hex::encode(hasher.finalize());
                let batch = ColdBatch {
                    instrument,
                    timeframe,
                    bars: &bars,
                };
                self.prepare_generation(&PublishPayload::Bars(&batch), &generation_id, archive, true)?
            };

            self.commit_generation(&new_gen, &group, Some(now_ms))?;
            report.compacted_groups += 1;
            report.retired_files_added += group.len();
        }
        Ok(())
    }

    fn deduplicate_bar_series(
        &mut self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        now_ms: i64,
        report: &mut CompactionReport,
    ) -> Result<()> {
        let version = instrument.instrument_spec_sha256();
        let tail_prefix = format!("tail-{version}-");
        let partition_prefix = format!("partition-{version}-");

        let generations = select_generations(self.conn()?, instrument, timeframe, None, None)?;
        let mut groups: BTreeMap<String, Vec<ColdGeneration>> = BTreeMap::new();
        for generation_item in generations {
            let month = partition_month(generation_item.first_ts)?;
            let name = generation_item
                .parquet_path
                .file_name()
                .and_then(|n| n.to_str())
                .unwrap_or("");
            if name.starts_with(&tail_prefix) || name.starts_with(&partition_prefix) {
                groups.entry(month).or_default().push(generation_item);
            } else {
                let last_month = partition_month(generation_item.last_ts)?;
                let drop_keys: Vec<String> = groups
                    .keys()
                    .filter(|gm| **gm >= month && **gm <= last_month)
                    .cloned()
                    .collect();
                for k in drop_keys {
                    groups.remove(&k);
                }
            }
        }

        for (_month, group) in groups {
            let mut bars = Vec::new();
            for item in &group {
                let path = verify_generation_file(&self.data_dir, item)?;
                bars.extend(read_bars_parquet(&path, None, None)?);
            }
            let raw_len = bars.len();
            dedup_bars_same_revision(&mut bars);
            if group.len() <= 1 && bars.len() == raw_len {
                continue;
            }
            let archive = compaction_archive_bytes(&group)?;
            let content = bar_content_bytes(&bars, true)?;
            let mut hasher = Sha256::new();
            hasher.update(b"partition\n");
            hasher.update(&content);
            let generation_id = hex::encode(hasher.finalize());
            let batch = ColdBatch {
                instrument,
                timeframe,
                bars: &bars,
            };
            let new_gen = self.prepare_generation(
                &PublishPayload::Bars(&batch),
                &generation_id,
                archive,
                true,
            )?;
            self.commit_generation(&new_gen, &group, Some(now_ms))?;
            report.compacted_groups += 1;
            report.retired_files_added += group.len();
        }
        Ok(())
    }

    fn cleanup_staging_and_temps(&self) -> Result<(usize, usize)> {
        let mut staging_removed = 0usize;
        let staging_root = self.data_dir.join(STAGING_SUBDIR);
        if staging_root.is_dir() {
            for entry in fs::read_dir(&staging_root)? {
                let entry = entry?;
                let path = entry.path();
                if path.is_dir() {
                    fs::remove_dir_all(&path)?;
                } else {
                    fs::remove_file(&path)?;
                }
                staging_removed += 1;
            }
        }

        let mut temp_removed = 0usize;
        for subdir in [ARCHIVE_SUBDIR, COLD_SUBDIR] {
            let root = self.data_dir.join(subdir);
            if root.is_dir() {
                temp_removed += remove_temp_files_recursive(&root)?;
            }
        }
        Ok((staging_removed, temp_removed))
    }
}

impl Drop for StoreInner {
    fn drop(&mut self) {
        if let Some(conn) = self.conn.take() {
            let _ = checkpoint_wal(&conn);
            drop(conn);
        }
        if let Some(key) = self.db_key.take() {
            release_writer(&key);
        }
    }
}

/// Single-writer cold store engine implementing 4-stage atomic publish, compaction, retention,
/// and crash orphan cleanup. Thread-safe (`Send + Sync`).
pub struct ColdStore {
    data_dir: PathBuf,
    db_path: PathBuf,
    inner: Mutex<StoreInner>,
}

impl ColdStore {
    /// Open the cold store in `data_dir`, acquiring the in-process single-writer claim and
    /// initializing `data/data_manifest.sqlite` in WAL mode.
    pub fn open(data_dir: impl AsRef<Path>) -> Result<Self> {
        Self::open_with_fault_hook(data_dir, None)
    }

    /// Open the cold store with an optional synchronous fault injection hook for crash testing.
    pub fn open_with_fault_hook(
        data_dir: impl AsRef<Path>,
        fault_hook: Option<FaultHook>,
    ) -> Result<Self> {
        let root = data_dir.as_ref();
        fs::create_dir_all(root)?;
        let data_dir = fs::canonicalize(root)?;
        let db_path = data_dir.join(MANIFEST_DB);
        let db_key = canonical_db_key(&db_path)?;

        claim_writer(&db_key)?;
        let conn = match open_writer_connection(&db_path) {
            Ok(c) => c,
            Err(err) => {
                release_writer(&db_key);
                return Err(err);
            }
        };

        let inner = StoreInner {
            data_dir: data_dir.clone(),
            db_key: Some(db_key),
            conn: Some(conn),
            fault_hook,
        };
        let _ = inner.cleanup_staging_and_temps();

        Ok(Self {
            data_dir,
            db_path,
            inner: Mutex::new(inner),
        })
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn set_fault_hook(&self, fault_hook: Option<FaultHook>) {
        if let Ok(mut inner) = self.inner.lock() {
            inner.fault_hook = fault_hook;
        }
    }

    fn lock_inner(&self) -> Result<std::sync::MutexGuard<'_, StoreInner>> {
        self.inner
            .lock()
            .map_err(|_| StoreError::Store("ColdStore mutex poisoned".to_owned()))
    }

    /// Explicitly checkpoint WAL and release the single-writer claim.
    pub fn close(&self) -> Result<()> {
        let mut inner = self.lock_inner()?;
        inner.close()
    }

    /// Archive normalized raw bars to `data/raw_archive/<sha256>.jsonl`.
    pub fn archive_bars(&self, batch: &ColdBatch<'_>) -> Result<ArchivedBatch> {
        let inner = self.lock_inner()?;
        let _ = inner.conn()?;
        let content = bar_content_bytes(batch.bars, false)?;
        let sha = sha256_bytes(&content);
        inner.archive_content(&content, &sha)
    }

    fn publish_with_archive_mode(
        &self,
        batch: &ColdBatch<'_>,
        write_raw_archive: bool,
    ) -> Result<ColdGeneration> {
        let mut inner = self.lock_inner()?;
        let _ = inner.conn()?;
        validate_bars(batch.bars, batch.instrument, batch.timeframe, false)?;

        let canonical = bar_content_bytes(batch.bars, true)?;
        let generation_id = sha256_bytes(&canonical);

        {
            let conn = inner.conn()?;
            if let Some(existing) =
                select_generation_by_id(conn, batch.instrument, batch.timeframe, &generation_id)?
            {
                verify_generation_file(&inner.data_dir, &existing)?;
                return Ok(existing);
            }
            let (first_ts, last_ts, count, committed_ts) = PublishPayload::Bars(batch).bounds();
            let candidates = select_matching_bounds(
                conn,
                batch.instrument,
                batch.timeframe,
                first_ts,
                last_ts,
                count,
                committed_ts,
            )?;
            for cand in candidates {
                let path = verify_generation_file(&inner.data_dir, &cand)?;
                if read_bars_parquet(&path, None, None)? == batch.bars {
                    return Ok(cand);
                }
            }
        }

        let archive = bar_content_bytes(batch.bars, false)?;
        let generation = inner.prepare_generation_with_archive_mode(
            &PublishPayload::Bars(batch),
            &generation_id,
            archive,
            false,
            write_raw_archive,
        )?;
        inner.commit_generation(&generation, &[], None)?;
        Ok(generation)
    }

    /// Atomically publish a same-month batch of `RawBar` rows. Idempotent for equivalent batches.
    pub fn publish(&self, batch: &ColdBatch<'_>) -> Result<ColdGeneration> {
        self.publish_with_archive_mode(batch, true)
    }

    /// Convenience wrapper for `publish(&ColdBatch { ... })`.
    pub fn publish_bars(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        bars: &[RawBar],
    ) -> Result<ColdGeneration> {
        self.publish(&ColdBatch {
            instrument,
            timeframe,
            bars,
        })
    }

    /// Publish a same-month batch of `RawBar` rows without writing an uncompressed JSONL copy
    /// into `data/raw_archive/` (used by bulk historical archive backfills).
    pub fn publish_bars_without_raw_archive(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        bars: &[RawBar],
    ) -> Result<ColdGeneration> {
        self.publish_with_archive_mode(
            &ColdBatch {
                instrument,
                timeframe,
                bars,
            },
            false,
        )
    }

    /// Atomically publish a same-month batch of `DerivativePoint` rows.
    pub fn publish_derivatives(
        &self,
        instrument: &InstrumentSpec,
        kind: DerivativeKind,
        rows: &[DerivativePoint],
    ) -> Result<ColdGeneration> {
        let mut inner = self.lock_inner()?;
        let _ = inner.conn()?;
        validate_derivatives(rows, instrument, kind, false)?;

        let content = derivative_content_bytes(rows)?;
        let generation_id = sha256_bytes(&content);

        if let Some(existing) =
            select_generation_by_id(inner.conn()?, instrument, kind.as_str(), &generation_id)?
        {
            verify_generation_file(&inner.data_dir, &existing)?;
            return Ok(existing);
        }

        let batch = DerivativeBatch {
            instrument,
            kind,
            rows,
        };
        let generation = inner.prepare_generation(
            &PublishPayload::Derivatives(&batch),
            &generation_id,
            content,
            false,
        )?;
        inner.commit_generation(&generation, &[], None)?;
        Ok(generation)
    }

    /// Atomically publish a same-UTC-day batch of `Trade` ticks (sorted by `tick_order`).
    pub fn publish_trades(
        &self,
        instrument: &InstrumentSpec,
        rows: &[Trade],
    ) -> Result<ColdGeneration> {
        let mut inner = self.lock_inner()?;
        let _ = inner.conn()?;
        let mut sorted = rows.to_vec();
        sorted.sort_by(trade_order_cmp);
        validate_trades(&sorted, instrument)?;

        let content = tick_content_bytes(instrument, TickRows::Trades(&sorted))?;
        let generation_id = sha256_bytes(&content);

        if let Some(existing) =
            select_generation_by_id(inner.conn()?, instrument, "trades", &generation_id)?
        {
            verify_generation_file(&inner.data_dir, &existing)?;
            return Ok(existing);
        }

        let generation = inner.prepare_generation(
            &PublishPayload::Trades {
                instrument,
                rows: &sorted,
            },
            &generation_id,
            content,
            false,
        )?;
        inner.commit_generation(&generation, &[], None)?;
        Ok(generation)
    }

    /// Atomically publish a same-UTC-day batch of `Quote` ticks (sorted by `tick_order`).
    pub fn publish_quotes(
        &self,
        instrument: &InstrumentSpec,
        rows: &[Quote],
    ) -> Result<ColdGeneration> {
        let mut inner = self.lock_inner()?;
        let _ = inner.conn()?;
        let mut sorted = rows.to_vec();
        sorted.sort_by(quote_order_cmp);
        validate_quotes(&sorted, instrument)?;

        let content = tick_content_bytes(instrument, TickRows::Quotes(&sorted))?;
        let generation_id = sha256_bytes(&content);

        if let Some(existing) =
            select_generation_by_id(inner.conn()?, instrument, "quotes", &generation_id)?
        {
            verify_generation_file(&inner.data_dir, &existing)?;
            return Ok(existing);
        }

        let generation = inner.prepare_generation(
            &PublishPayload::Quotes {
                instrument,
                rows: &sorted,
            },
            &generation_id,
            content,
            false,
        )?;
        inner.commit_generation(&generation, &[], None)?;
        Ok(generation)
    }

    /// Publish either `Trades` or `Quotes` uniformly.
    pub fn publish_ticks(
        &self,
        instrument: &InstrumentSpec,
        rows: TickRows<'_>,
    ) -> Result<ColdGeneration> {
        match rows {
            TickRows::Trades(trades) => self.publish_trades(instrument, trades),
            TickRows::Quotes(quotes) => self.publish_quotes(instrument, quotes),
        }
    }

    /// Query active generations for `(instrument, timeframe)` ordered by `rowid`.
    pub fn generations(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
    ) -> Result<Vec<ColdGeneration>> {
        let inner = self.lock_inner()?;
        select_generations(inner.conn()?, instrument, timeframe, None, None)
    }

    /// Query high-watermark cursor for `(instrument, timeframe)`.
    pub fn cursor(&self, instrument: &InstrumentSpec, timeframe: &str) -> Result<Option<ColdCursor>> {
        let inner = self.lock_inner()?;
        read_cursor(inner.conn()?, instrument, timeframe)
    }

    /// Read PIT-filtered bars directly through an internal `ColdReader`.
    pub fn read_bars(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<RawBar>> {
        {
            let inner = self.lock_inner()?;
            let _ = inner.conn()?;
        }
        ColdReader::new(&self.data_dir).read_bars(instrument, timeframe, start_ts, end_ts, as_of)
    }

    /// Read all bar revisions directly through an internal `ColdReader`.
    pub fn read_revisions(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
    ) -> Result<Vec<RawBar>> {
        {
            let inner = self.lock_inner()?;
            let _ = inner.conn()?;
        }
        ColdReader::new(&self.data_dir).read_revisions(instrument, timeframe, start_ts, end_ts)
    }

    /// Read PIT-filtered derivatives directly.
    pub fn read_derivatives(
        &self,
        instrument: &InstrumentSpec,
        kind: DerivativeKind,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
        historical_only: bool,
    ) -> Result<Vec<DerivativePoint>> {
        {
            let inner = self.lock_inner()?;
            let _ = inner.conn()?;
        }
        ColdReader::new(&self.data_dir).read_derivatives(
            instrument,
            kind,
            start_ts,
            end_ts,
            as_of,
            historical_only,
        )
    }

    /// Read PIT-filtered trades directly.
    pub fn read_trades(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<Trade>> {
        {
            let inner = self.lock_inner()?;
            let _ = inner.conn()?;
        }
        ColdReader::new(&self.data_dir).read_trades(instrument, start_ts, end_ts, as_of)
    }

    /// Read PIT-filtered quotes directly.
    pub fn read_quotes(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<Quote>> {
        {
            let inner = self.lock_inner()?;
            let _ = inner.conn()?;
        }
        ColdReader::new(&self.data_dir).read_quotes(instrument, start_ts, end_ts, as_of)
    }

    /// Compact tail files into partition files (preserving ALL revisions!) and prune retired files
    /// that satisfy BOTH `>= 24h` age and `>= 3` subsequent manifest versions.
    pub fn compact(
        &self,
        instruments: &[InstrumentSpec],
        now_ms: Option<i64>,
    ) -> Result<CompactionReport> {
        let mut inner = self.lock_inner()?;
        let effective_now = now_ms.unwrap_or_else(current_time_ms);
        let specs: BTreeMap<(&str, &str), &InstrumentSpec> = instruments
            .iter()
            .map(|s| ((s.venue.as_str(), s.symbol.as_str()), s))
            .collect();

        let series = select_distinct_series(inner.conn()?)?;
        let mut report = CompactionReport::default();

        for (venue, symbol, timeframe) in series {
            let Some(&instrument) = specs.get(&(venue.as_str(), symbol.as_str())) else {
                continue;
            };
            if let Some(tick_kind) = parse_tick_kind(&timeframe) {
                inner.compact_ticks(instrument, tick_kind, effective_now, &mut report)?;
            } else {
                inner.compact_series(instrument, &timeframe, effective_now, &mut report)?;
            }
        }

        report.pruned_files = prune_retired(inner.conn()?, &inner.data_dir, effective_now)?;
        Ok(report)
    }

    /// Deduplicate identical `(ts_event, revision)` bar rows inside existing monthly partitions
    /// and consolidate multi-partition months for the matching instruments.
    pub fn deduplicate_partitions(
        &self,
        instruments: &[InstrumentSpec],
        now_ms: Option<i64>,
    ) -> Result<CompactionReport> {
        let mut inner = self.lock_inner()?;
        let effective_now = now_ms.unwrap_or_else(current_time_ms);
        let specs: BTreeMap<(&str, &str), &InstrumentSpec> = instruments
            .iter()
            .map(|s| ((s.venue.as_str(), s.symbol.as_str()), s))
            .collect();

        let series = select_distinct_series(inner.conn()?)?;
        let mut report = CompactionReport::default();
        for (venue, symbol, timeframe) in series {
            let Some(&instrument) = specs.get(&(venue.as_str(), symbol.as_str())) else {
                continue;
            };
            if parse_tick_kind(&timeframe).is_some() || parse_derivative_kind(&timeframe).is_some() {
                continue;
            }
            inner.deduplicate_bar_series(instrument, &timeframe, effective_now, &mut report)?;
        }
        Ok(report)
    }

    /// Delete all `retired_files` from disk immediately (bypassing the 24h retention window) and
    /// clear the `retired_files` table while keeping all active `generations` intact.
    pub fn prune_retired_now(&self) -> Result<usize> {
        let inner = self.lock_inner()?;
        let conn = inner.conn()?;
        prune_all_retired(conn, &inner.data_dir)
    }

    /// Remove `.jsonl` files under `data/raw_archive/` whose `content_sha256` stem is not
    /// referenced by any active row in `generations`, returning `(removed_files, removed_bytes)`.
    pub fn prune_unreferenced_raw_archive(&self) -> Result<(usize, u64)> {
        let inner = self.lock_inner()?;
        let conn = inner.conn()?;
        let referenced = all_referenced_content_sha256(conn)?;
        let archive_root = inner.data_dir.join(ARCHIVE_SUBDIR);
        if !archive_root.is_dir() {
            return Ok((0, 0));
        }
        let mut removed_files = 0usize;
        let mut removed_bytes = 0u64;
        for entry in fs::read_dir(&archive_root)? {
            let entry = entry?;
            let path = entry.path();
            if path.is_file()
                && path.extension().and_then(|e| e.to_str()) == Some("jsonl")
                && let Some(stem) = path.file_stem().and_then(|s| s.to_str())
                && !referenced.contains(stem)
            {
                let size = fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
                fs::remove_file(&path)?;
                removed_files += 1;
                removed_bytes += size;
            }
        }
        Ok((removed_files, removed_bytes))
    }

    /// Remove crash orphan artifacts: incomplete staging dirs, stray `.tmp` files, and
    /// unreferenced `.parquet` files under `data/cold/` not present in `generations` or `retired_files`.
    pub fn cleanup_orphans(&self) -> Result<OrphanCleanupReport> {
        let inner = self.lock_inner()?;
        let conn = inner.conn()?;
        let (staging_dirs_removed, temp_files_removed) = inner.cleanup_staging_and_temps()?;
        let referenced = all_referenced_parquet_paths(conn)?;

        let mut unreferenced_parquet_removed = 0usize;
        let cold_root = inner.data_dir.join(COLD_SUBDIR);
        if cold_root.is_dir() {
            unreferenced_parquet_removed =
                remove_unreferenced_parquets(&inner.data_dir, &cold_root, &referenced)?;
        }

        Ok(OrphanCleanupReport {
            staging_dirs_removed,
            temp_files_removed,
            unreferenced_parquet_removed,
        })
    }
}

fn dedup_bars_same_revision(bars: &mut Vec<RawBar>) {
    bars.sort_by_key(|b| (b.ts_event, b.revision, b.ts_committed));
    let mut out: Vec<RawBar> = Vec::with_capacity(bars.len());
    for bar in bars.drain(..) {
        if let Some(prev) = out.last_mut()
            && prev.ts_event == bar.ts_event
            && prev.revision == bar.revision
        {
            *prev = bar;
        } else {
            out.push(bar);
        }
    }
    *bars = out;
}

fn remove_temp_files_recursive(dir: &Path) -> Result<usize> {
    let mut removed = 0usize;
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            removed += remove_temp_files_recursive(&path)?;
        } else if path
            .file_name()
            .and_then(|n| n.to_str())
            .is_some_and(|name| name.ends_with(".tmp"))
        {
            fs::remove_file(&path)?;
            removed += 1;
        }
    }
    Ok(removed)
}

fn remove_unreferenced_parquets(
    data_dir: &Path,
    dir: &Path,
    referenced: &HashSet<String>,
) -> Result<usize> {
    let mut removed = 0usize;
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        if path.is_dir() {
            removed += remove_unreferenced_parquets(data_dir, &path, referenced)?;
        } else if path.extension().and_then(|e| e.to_str()) == Some("parquet") {
            let rel = path
                .strip_prefix(data_dir)
                .map_err(|_| StoreError::Integrity("cold parquet outside data_dir".to_owned()))?;
            let posix = posix_rel_path(rel);
            if !referenced.contains(&posix) {
                fs::remove_file(&path)?;
                removed += 1;
            }
        }
    }
    Ok(removed)
}

fn verify_generation_file(data_dir: &Path, generation: &ColdGeneration) -> Result<PathBuf> {
    let path = path_under_root(data_dir, &generation.parquet_path)?;
    if sha256_file(&path)? != generation.sha256 {
        return Err(StoreError::Integrity(format!(
            "generation sha256 mismatch: {}",
            generation.parquet_path.display()
        )));
    }
    Ok(path)
}

// =========================================================================
// ColdReader (Lockless Read-Only)
// =========================================================================

/// Lockless read-only manifest & Parquet reader (never acquires any write lock or modifies SQLite).
pub struct ColdReader {
    data_dir: PathBuf,
    db_path: PathBuf,
    snapshot: Option<ColdSnapshot>,
    pinned_series: BTreeMap<(String, String, String), Vec<ColdGeneration>>,
}

impl ColdReader {
    /// Create a live lockless `ColdReader` reading `data/data_manifest.sqlite` on demand.
    pub fn new(data_dir: impl AsRef<Path>) -> Self {
        let raw = data_dir.as_ref();
        let resolved = fs::canonicalize(raw).unwrap_or_else(|_| raw.to_path_buf());
        let db_path = resolved.join(MANIFEST_DB);
        Self {
            data_dir: resolved,
            db_path,
            snapshot: None,
            pinned_series: BTreeMap::new(),
        }
    }

    /// Create a `ColdReader` pinned to a previously captured `ColdSnapshot`.
    pub fn with_snapshot(data_dir: impl AsRef<Path>, snapshot: ColdSnapshot) -> Self {
        let mut reader = Self::new(data_dir);
        for manifest in [
            Some(&snapshot.bars),
            Some(&snapshot.derivatives),
            snapshot.ticks.as_ref(),
        ]
        .into_iter()
        .flatten()
        {
            for generation_item in &manifest.generations {
                reader
                    .pinned_series
                    .entry((
                        generation_item.venue.clone(),
                        generation_item.symbol.clone(),
                        generation_item.timeframe.clone(),
                    ))
                    .or_default()
                    .push(generation_item.clone());
            }
        }
        reader.snapshot = Some(snapshot);
        reader
    }

    pub fn data_dir(&self) -> &Path {
        &self.data_dir
    }

    /// Capture an immutable multi-dataset `ColdSnapshot` in a single read-only SQLite transaction.
    pub fn capture_snapshot(
        &self,
        instruments: &[(&InstrumentSpec, i64)],
        tick_instruments: &[&str],
    ) -> Result<ColdSnapshot> {
        if !self.db_path.is_file() {
            let empty = ColdManifest {
                version: 0,
                generations: Vec::new(),
            };
            return Ok(ColdSnapshot {
                bars: empty.clone(),
                derivatives: empty.clone(),
                ticks: (!tick_instruments.is_empty()).then_some(empty),
            });
        }
        let conn = open_reader_connection(&self.db_path)?;
        let version = read_manifest_version(&conn)?;
        let mut bars = Vec::new();
        let mut derivatives = Vec::new();
        let mut ticks = Vec::new();
        let tick_set: HashSet<&str> = tick_instruments.iter().copied().collect();

        for &(instrument, end) in instruments {
            for &tf in SUPPORTED_TIMEFRAMES {
                bars.extend(select_generations(&conn, instrument, tf, None, Some(end))?);
            }
            for kind in ALL_DERIVATIVE_KINDS {
                derivatives.extend(select_generations(
                    &conn,
                    instrument,
                    kind.as_str(),
                    None,
                    Some(end),
                )?);
            }
            if tick_set.contains(instrument.instrument_id.as_str()) {
                for kind in ALL_TICK_KINDS {
                    ticks.extend(select_generations(
                        &conn,
                        instrument,
                        tick_kind_str(kind),
                        None,
                        Some(end),
                    )?);
                }
            }
        }

        Ok(ColdSnapshot {
            bars: ColdManifest {
                version,
                generations: bars,
            },
            derivatives: ColdManifest {
                version,
                generations: derivatives,
            },
            ticks: (!tick_instruments.is_empty()).then_some(ColdManifest {
                version,
                generations: ticks,
            }),
        })
    }

    /// Query active generations for `(instrument, series)` without holding a write lock.
    pub fn generations(
        &self,
        instrument: &InstrumentSpec,
        series: &str,
    ) -> Result<Vec<ColdGeneration>> {
        if !self.db_path.is_file() {
            return Ok(Vec::new());
        }
        let conn = open_reader_connection(&self.db_path)?;
        select_generations(&conn, instrument, series, None, None)
    }

    /// Query the high-watermark cursor for `(instrument, series)` without holding a write lock.
    pub fn cursor(&self, instrument: &InstrumentSpec, series: &str) -> Result<Option<ColdCursor>> {
        if !self.db_path.is_file() {
            return Ok(None);
        }
        let conn = open_reader_connection(&self.db_path)?;
        read_cursor(&conn, instrument, series)
    }

    fn read_generations<T, F>(
        &self,
        instrument: &InstrumentSpec,
        series: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        read_fn: F,
    ) -> Result<(ColdManifest, Vec<T>)>
    where
        F: Fn(&[ColdGeneration]) -> Result<Vec<T>>,
    {
        if let Some(snapshot) = &self.snapshot {
            let pinned = if SUPPORTED_TIMEFRAMES.contains(&series) {
                &snapshot.bars
            } else if parse_tick_kind(series).is_some() {
                snapshot.ticks.as_ref().ok_or_else(|| {
                    StoreError::Integrity("snapshot missing tick manifest".to_owned())
                })?
            } else {
                &snapshot.derivatives
            };
            let key = (
                instrument.venue.clone(),
                instrument.symbol.clone(),
                series.to_owned(),
            );
            let gens: Vec<ColdGeneration> = self
                .pinned_series
                .get(&key)
                .map(|items| {
                    items
                        .iter()
                        .filter(|g| {
                            start_ts.is_none_or(|s| g.last_ts >= s)
                                && end_ts.is_none_or(|e| g.first_ts <= e)
                        })
                        .cloned()
                        .collect()
                })
                .unwrap_or_default();
            let rows = read_fn(&gens).map_err(|err| match err {
                StoreError::Io(io_err) if io_err.kind() == ErrorKind::NotFound => {
                    StoreError::Integrity(format!("pinned snapshot generation missing: {io_err}"))
                }
                other => other,
            })?;
            return Ok((
                ColdManifest {
                    version: pinned.version,
                    generations: gens,
                },
                rows,
            ));
        }

        if !self.db_path.is_file() {
            return Ok((
                ColdManifest {
                    version: 0,
                    generations: Vec::new(),
                },
                Vec::new(),
            ));
        }

        for attempt in 0..2 {
            let conn = open_reader_connection(&self.db_path)?;
            let version = read_manifest_version(&conn)?;
            let gens = select_generations(&conn, instrument, series, start_ts, end_ts)?;
            drop(conn);

            match read_fn(&gens) {
                Ok(rows) => {
                    return Ok((
                        ColdManifest {
                            version,
                            generations: gens,
                        },
                        rows,
                    ));
                }
                Err(StoreError::Io(io_err)) if io_err.kind() == ErrorKind::NotFound => {
                    if attempt == 1 {
                        return Err(StoreError::Integrity(format!(
                            "generation file missing after retry: {io_err}"
                        )));
                    }
                }
                Err(other) => return Err(other),
            }
        }
        unreachable!("retry loop always returns on attempt == 1")
    }

    /// Read manifest snapshot and all raw bar revisions intersecting `[start_ts, end_ts]`.
    pub fn read_manifest_snapshot(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
    ) -> Result<(ColdManifest, Vec<RawBar>)> {
        self.read_generations(instrument, timeframe, start_ts, end_ts, |gens| {
            let mut rows = Vec::new();
            for generation_item in gens {
                let path = verify_generation_file(&self.data_dir, generation_item)?;
                rows.extend(read_bars_parquet(&path, start_ts, end_ts)?);
            }
            Ok(rows)
        })
    }

    /// Read all bar revisions intersecting `[start_ts, end_ts]`.
    pub fn read_revisions(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
    ) -> Result<Vec<RawBar>> {
        let (_, rows) = self.read_manifest_snapshot(instrument, timeframe, start_ts, end_ts)?;
        Ok(rows)
    }

    /// Read PIT-filtered bars (`ts_available <= as_of`, highest `revision` per `ts_event`).
    pub fn read_bars(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<RawBar>> {
        let revisions = self.read_revisions(instrument, timeframe, start_ts, end_ts)?;
        Ok(latest_visible_bars(revisions, as_of))
    }

    /// Read all derivative revisions intersecting `[start_ts, end_ts]`.
    pub fn read_derivative_revisions(
        &self,
        instrument: &InstrumentSpec,
        kind: DerivativeKind,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        historical_only: bool,
    ) -> Result<Vec<DerivativePoint>> {
        let hk = historical_only.then_some(kind);
        let (_, rows) = self.read_generations(instrument, kind.as_str(), start_ts, end_ts, |gens| {
            let mut out = Vec::new();
            for generation_item in gens {
                let path = verify_generation_file(&self.data_dir, generation_item)?;
                out.extend(read_derivatives_parquet(&path, start_ts, end_ts, hk)?);
            }
            Ok(out)
        })?;
        Ok(rows)
    }

    /// Read PIT-filtered derivative points (`ts_available <= as_of`, latest revision per identity).
    pub fn read_derivatives(
        &self,
        instrument: &InstrumentSpec,
        kind: DerivativeKind,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
        historical_only: bool,
    ) -> Result<Vec<DerivativePoint>> {
        let revisions = self.read_derivative_revisions(
            instrument,
            kind,
            start_ts,
            end_ts,
            historical_only,
        )?;
        Ok(latest_visible_derivatives(revisions, as_of))
    }

    /// Read all trade revisions matching a set of 16-byte `trade_id`s across generations.
    pub fn read_trade_revisions_by_id(
        &self,
        instrument: &InstrumentSpec,
        trade_ids: &HashSet<[u8; 16]>,
    ) -> Result<Vec<Trade>> {
        if trade_ids.is_empty() {
            return Ok(Vec::new());
        }
        let (_, rows) = self.read_generations(instrument, "trades", None, None, |gens| {
            let mut out = Vec::new();
            for generation_item in gens {
                let path = verify_generation_file(&self.data_dir, generation_item)?;
                out.extend(read_trades_parquet(
                    &path,
                    &instrument.instrument_id,
                    None,
                    None,
                    Some(trade_ids),
                )?);
            }
            Ok(out)
        })?;
        Ok(rows)
    }

    /// Read all trade revisions intersecting `[start_ts, end_ts]` (plus matching cross-range IDs).
    pub fn read_trade_revisions(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
    ) -> Result<Vec<Trade>> {
        let lower = start_ts.unwrap_or(0);
        let upper = end_ts.unwrap_or(i64::MAX);
        let (_, rows) = self.read_generations(instrument, "trades", None, None, |gens| {
            let mut rows = Vec::new();
            let mut outside = Vec::new();
            for generation_item in gens {
                if generation_item.last_ts >= lower && generation_item.first_ts <= upper {
                    let path = verify_generation_file(&self.data_dir, generation_item)?;
                    rows.extend(read_trades_parquet(
                        &path,
                        &instrument.instrument_id,
                        start_ts,
                        end_ts,
                        None,
                    )?);
                }
                if generation_item.first_ts < lower || generation_item.last_ts > upper {
                    outside.push(generation_item.clone());
                }
            }
            if !rows.is_empty() && !outside.is_empty() {
                let ids: HashSet<[u8; 16]> = rows.iter().map(|t| *t.trade_id.as_bytes()).collect();
                for generation_item in outside {
                    let path = verify_generation_file(&self.data_dir, &generation_item)?;
                    let matched = read_trades_parquet(
                        &path,
                        &instrument.instrument_id,
                        None,
                        None,
                        Some(&ids),
                    )?;
                    rows.extend(
                        matched
                            .into_iter()
                            .filter(|t| t.ts_event < lower || t.ts_event > upper),
                    );
                }
            }
            Ok(rows)
        })?;
        Ok(rows)
    }

    /// Read PIT-filtered `Trade` ticks matching Python `ColdReader.read_ticks(..., TRADES)`.
    pub fn read_trades(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<Trade>> {
        let rows = self.read_trade_revisions(instrument, start_ts, end_ts)?;
        Ok(latest_visible_trades(rows, start_ts, end_ts, as_of))
    }

    /// Read all `Quote` revisions intersecting `[start_ts, end_ts]`.
    pub fn read_quote_revisions(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
    ) -> Result<Vec<Quote>> {
        let (_, rows) = self.read_generations(instrument, "quotes", start_ts, end_ts, |gens| {
            let mut out = Vec::new();
            for generation_item in gens {
                let path = verify_generation_file(&self.data_dir, generation_item)?;
                out.extend(read_quotes_parquet(
                    &path,
                    &instrument.instrument_id,
                    start_ts,
                    end_ts,
                )?);
            }
            Ok(out)
        })?;
        Ok(rows)
    }

    /// Read PIT-filtered `Quote` ticks matching Python `ColdReader.read_ticks(..., QUOTES)`.
    pub fn read_quotes(
        &self,
        instrument: &InstrumentSpec,
        start_ts: Option<i64>,
        end_ts: Option<i64>,
        as_of: Option<i64>,
    ) -> Result<Vec<Quote>> {
        let rows = self.read_quote_revisions(instrument, start_ts, end_ts)?;
        Ok(latest_visible_quotes(rows, start_ts, end_ts, as_of))
    }
}
