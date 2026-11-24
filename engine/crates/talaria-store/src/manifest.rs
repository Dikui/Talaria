//! SQLite WAL manifest store, single-writer in-process guard, and retention pruning.

use std::collections::HashSet;
use std::fs;
use std::io::ErrorKind;
use std::path::{Component, Path, PathBuf};
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use talaria_contracts::InstrumentSpec;

use crate::error::{Result, StoreError};
use crate::types::{ColdCursor, ColdGeneration, RETAINED_MANIFESTS, RETENTION_MS};

static WRITER_CLAIMS: LazyLock<Mutex<HashSet<PathBuf>>> =
    LazyLock::new(|| Mutex::new(HashSet::new()));

const SCHEMA_SQL: &str = "
CREATE TABLE IF NOT EXISTS generations (
    generation_id TEXT NOT NULL,
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    parquet_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    first_ts INTEGER NOT NULL,
    last_ts INTEGER NOT NULL,
    bars INTEGER NOT NULL,
    committed_ts INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, timeframe, generation_id)
);
CREATE TABLE IF NOT EXISTS cursors (
    venue TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    last_ts INTEGER NOT NULL,
    bars INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (venue, symbol, timeframe)
);
CREATE TABLE IF NOT EXISTS manifest_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
INSERT OR IGNORE INTO manifest_version VALUES (1, 0);
CREATE TABLE IF NOT EXISTS retired_files (
    parquet_path TEXT PRIMARY KEY,
    retired_ts INTEGER NOT NULL,
    manifest_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS generations_series_range
ON generations (venue, symbol, timeframe, first_ts, last_ts);
";

/// Normalize a manifest DB path so symlinked temp dirs (e.g. `/var` vs `/private/var` on macOS)
/// map to the same canonical key.
pub fn canonical_db_key(db_path: &Path) -> Result<PathBuf> {
    let parent = db_path.parent().ok_or_else(|| {
        StoreError::Store(format!("manifest path has no parent: {}", db_path.display()))
    })?;
    fs::create_dir_all(parent)?;
    let canonical_parent = fs::canonicalize(parent)?;
    let name = db_path.file_name().ok_or_else(|| {
        StoreError::Store(format!("manifest path has no filename: {}", db_path.display()))
    })?;
    Ok(canonical_parent.join(name))
}

/// Register an in-process exclusive writer claim for `db_key`.
pub fn claim_writer(db_key: &Path) -> Result<()> {
    let mut claims = WRITER_CLAIMS
        .lock()
        .map_err(|_| StoreError::Store("writer claim mutex poisoned".to_owned()))?;
    if claims.contains(db_key) {
        return Err(StoreError::SingleWriterViolation(format!(
            "another writer is already open in this process: {}",
            db_key.display()
        )));
    }
    claims.insert(db_key.to_path_buf());
    Ok(())
}

/// Idempotently release an in-process writer claim.
pub fn release_writer(db_key: &Path) {
    if let Ok(mut claims) = WRITER_CLAIMS.lock() {
        claims.remove(db_key);
    }
}

/// Open the manifest SQLite connection in WAL mode with `synchronous=FULL` and ensure schema exists.
pub fn open_writer_connection(db_path: &Path) -> Result<Connection> {
    if let Some(parent) = db_path.parent() {
        fs::create_dir_all(parent)?;
    }
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )?;
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=FULL;",
    )?;
    conn.execute_batch(SCHEMA_SQL)?;
    Ok(conn)
}

/// Open a lockless read-only manifest connection (never acquires a write lock or modifies the DB).
pub fn open_reader_connection(db_path: &Path) -> Result<Connection> {
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    conn.busy_timeout(Duration::from_secs(5))?;
    Ok(conn)
}

/// Truncate WAL on writer shutdown.
pub fn checkpoint_wal(conn: &Connection) -> Result<()> {
    conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")?;
    Ok(())
}

/// Resolve a relative manifest path strictly inside `data_dir`.
pub fn path_under_root(data_dir: &Path, relative: &Path) -> Result<PathBuf> {
    if relative.is_absolute() {
        return Err(StoreError::Integrity(format!(
            "manifest path escapes root (absolute): {}",
            relative.display()
        )));
    }
    for comp in relative.components() {
        match comp {
            Component::Normal(_) | Component::CurDir => {}
            Component::ParentDir | Component::RootDir | Component::Prefix(_) => {
                return Err(StoreError::Integrity(format!(
                    "manifest path escapes root: {}",
                    relative.display()
                )));
            }
        }
    }
    let root = fs::canonicalize(data_dir)?;
    let candidate = root.join(relative);
    if candidate.exists() {
        let resolved = fs::canonicalize(&candidate)?;
        if !resolved.starts_with(&root) {
            return Err(StoreError::Integrity(format!(
                "manifest path resolves outside root: {}",
                relative.display()
            )));
        }
        Ok(resolved)
    } else {
        Ok(candidate)
    }
}

/// Read the current `manifest_version` (returns `0` if absent).
pub fn read_manifest_version(conn: &Connection) -> Result<i64> {
    let has_table: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'manifest_version'",
            [],
            |row| row.get(0),
        )
        .optional()?;
    if has_table.is_none() {
        return Ok(0);
    }
    let version: Option<i64> = conn
        .query_row(
            "SELECT version FROM manifest_version WHERE id = 1",
            [],
            |row| row.get(0),
        )
        .optional()?;
    Ok(version.unwrap_or(0))
}

fn generation_from_row(
    venue: &str,
    symbol: &str,
    timeframe: &str,
    row: &rusqlite::Row<'_>,
) -> rusqlite::Result<ColdGeneration> {
    let path_str: String = row.get(1)?;
    Ok(ColdGeneration {
        generation_id: row.get(0)?,
        venue: venue.to_owned(),
        symbol: symbol.to_owned(),
        timeframe: timeframe.to_owned(),
        parquet_path: PathBuf::from(path_str),
        sha256: row.get(2)?,
        content_sha256: row.get(3)?,
        first_ts: row.get(4)?,
        last_ts: row.get(5)?,
        bars: row.get(6)?,
        committed_ts: row.get(7)?,
    })
}

/// Select active generations intersecting `[start_ts, end_ts]` ordered by `rowid`.
pub fn select_generations(
    conn: &Connection,
    instrument: &InstrumentSpec,
    timeframe: &str,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
) -> Result<Vec<ColdGeneration>> {
    let mut sql = String::from(
        "SELECT generation_id, parquet_path, sha256, content_sha256, first_ts, last_ts, bars, committed_ts \
         FROM generations WHERE venue = ?1 AND symbol = ?2 AND timeframe = ?3",
    );
    let mut params_vec: Vec<rusqlite::types::Value> = vec![
        instrument.venue.clone().into(),
        instrument.symbol.clone().into(),
        timeframe.to_owned().into(),
    ];
    if let Some(s) = start_ts {
        sql.push_str(&format!(" AND last_ts >= ?{}", params_vec.len() + 1));
        params_vec.push(s.into());
    }
    if let Some(e) = end_ts {
        sql.push_str(&format!(" AND first_ts <= ?{}", params_vec.len() + 1));
        params_vec.push(e.into());
    }
    sql.push_str(" ORDER BY rowid");

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params_vec), |row| {
        generation_from_row(&instrument.venue, &instrument.symbol, timeframe, row)
    })?;
    let mut out = Vec::new();
    for item in rows {
        out.push(item?);
    }
    Ok(out)
}

/// Look up a single generation by `(venue, symbol, timeframe, generation_id)`.
pub fn select_generation_by_id(
    conn: &Connection,
    instrument: &InstrumentSpec,
    timeframe: &str,
    generation_id: &str,
) -> Result<Option<ColdGeneration>> {
    let mut stmt = conn.prepare(
        "SELECT generation_id, parquet_path, sha256, content_sha256, first_ts, last_ts, bars, committed_ts \
         FROM generations WHERE venue = ?1 AND symbol = ?2 AND timeframe = ?3 AND generation_id = ?4",
    )?;
    let row = stmt
        .query_row(
            params![
                instrument.venue,
                instrument.symbol,
                timeframe,
                generation_id
            ],
            |r| generation_from_row(&instrument.venue, &instrument.symbol, timeframe, r),
        )
        .optional()?;
    Ok(row)
}

/// Find candidate generations sharing exact `(first_ts, last_ts, bars, committed_ts)` bounds.
pub fn select_matching_bounds(
    conn: &Connection,
    instrument: &InstrumentSpec,
    timeframe: &str,
    first_ts: i64,
    last_ts: i64,
    bars: i64,
    committed_ts: i64,
) -> Result<Vec<ColdGeneration>> {
    let mut stmt = conn.prepare(
        "SELECT generation_id, parquet_path, sha256, content_sha256, first_ts, last_ts, bars, committed_ts \
         FROM generations \
         WHERE venue = ?1 AND symbol = ?2 AND timeframe = ?3 \
           AND first_ts = ?4 AND last_ts = ?5 AND bars = ?6 AND committed_ts = ?7 \
         ORDER BY rowid",
    )?;
    let rows = stmt.query_map(
        params![
            instrument.venue,
            instrument.symbol,
            timeframe,
            first_ts,
            last_ts,
            bars,
            committed_ts
        ],
        |r| generation_from_row(&instrument.venue, &instrument.symbol, timeframe, r),
    )?;
    let mut out = Vec::new();
    for item in rows {
        out.push(item?);
    }
    Ok(out)
}

/// Query all distinct `(venue, symbol, timeframe)` series present in `generations`.
pub fn select_distinct_series(conn: &Connection) -> Result<Vec<(String, String, String)>> {
    let mut stmt = conn.prepare("SELECT DISTINCT venue, symbol, timeframe FROM generations")?;
    let rows = stmt.query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))?;
    let mut out = Vec::new();
    for item in rows {
        out.push(item?);
    }
    Ok(out)
}

/// Read the cursor row for `(venue, symbol, timeframe)`.
pub fn read_cursor(
    conn: &Connection,
    instrument: &InstrumentSpec,
    timeframe: &str,
) -> Result<Option<ColdCursor>> {
    let row = conn
        .query_row(
            "SELECT generation_id, last_ts, bars, updated_ts \
             FROM cursors WHERE venue = ?1 AND symbol = ?2 AND timeframe = ?3",
            params![instrument.venue, instrument.symbol, timeframe],
            |r| {
                Ok(ColdCursor {
                    venue: instrument.venue.clone(),
                    symbol: instrument.symbol.clone(),
                    timeframe: timeframe.to_owned(),
                    generation_id: r.get(0)?,
                    last_ts: r.get(1)?,
                    bars: r.get(2)?,
                    updated_ts: r.get(3)?,
                })
            },
        )
        .optional()?;
    Ok(row)
}

/// Normalize relative path to POSIX `/` string for SQLite compatibility with Python `ColdReader`.
pub fn posix_rel_path(path: &Path) -> String {
    path.components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect::<Vec<_>>()
        .join("/")
}

/// Execute Stage 4 short WAL transaction updating `manifest_version`, `retired_files`, `generations`, and `cursors`.
pub fn commit_manifest(
    conn: &mut Connection,
    generation: &ColdGeneration,
    replaced: &[ColdGeneration],
    retired_ts: i64,
) -> Result<i64> {
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let version: i64 = tx.query_row(
        "UPDATE manifest_version SET version = version + 1 RETURNING version",
        [],
        |row| row.get(0),
    )?;

    if !replaced.is_empty() {
        let mut insert_retired =
            tx.prepare("INSERT INTO retired_files VALUES (?1, ?2, ?3)")?;
        let mut delete_gen = tx.prepare(
            "DELETE FROM generations WHERE venue = ?1 AND symbol = ?2 AND timeframe = ?3 AND generation_id = ?4",
        )?;
        for item in replaced {
            insert_retired.execute(params![
                posix_rel_path(&item.parquet_path),
                retired_ts,
                version
            ])?;
            delete_gen.execute(params![
                item.venue,
                item.symbol,
                item.timeframe,
                item.generation_id
            ])?;
        }
    }

    let gen_posix = posix_rel_path(&generation.parquet_path);
    tx.execute(
        "DELETE FROM retired_files WHERE parquet_path = ?1",
        params![gen_posix],
    )?;
    tx.execute(
        "INSERT INTO generations ( \
            generation_id, venue, symbol, timeframe, parquet_path, \
            sha256, content_sha256, first_ts, last_ts, bars, committed_ts \
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        params![
            generation.generation_id,
            generation.venue,
            generation.symbol,
            generation.timeframe,
            gen_posix,
            generation.sha256,
            generation.content_sha256,
            generation.first_ts,
            generation.last_ts,
            generation.bars,
            generation.committed_ts,
        ],
    )?;
    tx.execute(
        "INSERT INTO cursors ( \
            venue, symbol, timeframe, generation_id, last_ts, bars, updated_ts \
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7) \
        ON CONFLICT(venue, symbol, timeframe) DO UPDATE SET \
            generation_id = CASE \
                WHEN excluded.last_ts >= cursors.last_ts \
                THEN excluded.generation_id \
                ELSE cursors.generation_id \
            END, \
            last_ts = MAX(cursors.last_ts, excluded.last_ts), \
            bars = CASE \
                WHEN excluded.last_ts >= cursors.last_ts \
                THEN excluded.bars \
                ELSE cursors.bars \
            END, \
            updated_ts = MAX(cursors.updated_ts, excluded.updated_ts)",
        params![
            generation.venue,
            generation.symbol,
            generation.timeframe,
            generation.generation_id,
            generation.last_ts,
            generation.bars,
            generation.committed_ts,
        ],
    )?;

    tx.commit()?;
    Ok(version)
}

/// Reclaim retired files whose `retired_ts` is at least `RETENTION_MS` old AND whose
/// `manifest_version` lags at least `RETAINED_MANIFESTS` behind the current manifest version.
pub fn prune_retired(conn: &Connection, data_dir: &Path, now_ms: i64) -> Result<usize> {
    let cutoff_ts = now_ms - RETENTION_MS;
    let paths: Vec<String> = {
        let mut stmt = conn.prepare(
            "SELECT parquet_path FROM retired_files \
             WHERE retired_ts <= ?1 \
               AND manifest_version <= (SELECT version - ?2 FROM manifest_version)",
        )?;
        let rows = stmt.query_map(params![cutoff_ts, RETAINED_MANIFESTS], |r| r.get(0))?;
        let mut collected = Vec::new();
        for r in rows {
            collected.push(r?);
        }
        collected
    };

    let mut pruned = 0usize;
    for relative in paths {
        let full = path_under_root(data_dir, Path::new(&relative))?;
        match fs::remove_file(&full) {
            Ok(()) => {}
            Err(err) if err.kind() == ErrorKind::NotFound => {}
            Err(err) => return Err(err.into()),
        }
        conn.execute(
            "DELETE FROM retired_files WHERE parquet_path = ?1",
            params![relative],
        )?;
        pruned += 1;
    }
    Ok(pruned)
}

/// Collect every relative POSIX parquet path referenced in `generations` or `retired_files`.
pub fn all_referenced_parquet_paths(conn: &Connection) -> Result<HashSet<String>> {
    let mut set = HashSet::new();
    {
        let mut stmt = conn.prepare("SELECT parquet_path FROM generations")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        for r in rows {
            set.insert(r?);
        }
    }
    {
        let mut stmt = conn.prepare("SELECT parquet_path FROM retired_files")?;
        let rows = stmt.query_map([], |r| r.get::<_, String>(0))?;
        for r in rows {
            set.insert(r?);
        }
    }
    Ok(set)
}
