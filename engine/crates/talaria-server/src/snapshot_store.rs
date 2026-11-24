//! Frozen snapshot store (`state/runtime_snapshots.sqlite`) persisting `SnapshotMeta`,
//! `CompactBar` frames, and `DerivativePoint` observations across restarts.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use talaria_contracts::{
    CompactBar, DerivativePoint, SnapshotMeta, error_codes,
};

use crate::error::ApiError;

pub const SNAPSHOT_DB: &str = "state/runtime_snapshots.sqlite";

const SCHEMA_SQL: &str = "
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    cutoff_ts INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    meta_json TEXT NOT NULL,
    derivatives_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshot_frames (
    snapshot_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bars_json TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, timeframe),
    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
        ON DELETE CASCADE
);
";

#[derive(Debug, Clone)]
pub struct FrozenSnapshot {
    pub meta: SnapshotMeta,
    pub frames: BTreeMap<String, Vec<CompactBar>>,
    pub derivatives: Vec<DerivativePoint>,
}

/// Thread-safe WAL SQLite store for frozen snapshots.
pub struct SnapshotStore {
    db_path: PathBuf,
    writer: Mutex<Connection>,
}

impl SnapshotStore {
    pub fn open(data_dir: &Path) -> Result<Self, ApiError> {
        let db_path = data_dir.join(SNAPSHOT_DB);
        if let Some(parent) = db_path.parent() {
            fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
        }
        let conn = Connection::open_with_flags(
            &db_path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
        )
        .map_err(|e| ApiError::internal(format!("open snapshot db: {e}")))?;
        conn.busy_timeout(Duration::from_secs(5))
            .map_err(|e| ApiError::internal(e.to_string()))?;
        conn.execute_batch(
            "PRAGMA journal_mode=WAL;
             PRAGMA synchronous=FULL;
             PRAGMA foreign_keys=ON;",
        )
        .map_err(|e| ApiError::internal(e.to_string()))?;
        conn.execute_batch(SCHEMA_SQL)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        Ok(Self {
            db_path,
            writer: Mutex::new(conn),
        })
    }

    pub fn save(&self, snapshot: &FrozenSnapshot) -> Result<(), ApiError> {
        let meta_json = serde_json::to_string(&snapshot.meta)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let deriv_json = serde_json::to_string(&snapshot.derivatives)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let mut serialized_frames = Vec::with_capacity(snapshot.frames.len());
        for (tf, bars) in &snapshot.frames {
            let bars_json = serde_json::to_string(bars)
                .map_err(|e| ApiError::internal(e.to_string()))?;
            serialized_frames.push((tf.as_str(), bars_json));
        }

        let mut conn = self
            .writer
            .lock()
            .map_err(|_| ApiError::internal("snapshot store mutex poisoned"))?;
        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        tx.execute(
            "INSERT INTO snapshots
                (snapshot_id, instrument_id, cutoff_ts, created_ts, meta_json, derivatives_json)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                snapshot.meta.snapshot_id,
                snapshot.meta.instrument_id,
                snapshot.meta.cutoff_ts,
                snapshot.meta.created_ts,
                meta_json,
                deriv_json,
            ],
        )
        .map_err(|e| ApiError::internal(e.to_string()))?;
        for (tf, bars_json) in serialized_frames {
            tx.execute(
                "INSERT INTO snapshot_frames (snapshot_id, timeframe, bars_json)
                 VALUES (?1, ?2, ?3)",
                params![snapshot.meta.snapshot_id, tf, bars_json],
            )
            .map_err(|e| ApiError::internal(e.to_string()))?;
        }
        tx.commit().map_err(|e| ApiError::internal(e.to_string()))?;
        Ok(())
    }

    fn open_reader(&self) -> Result<Connection, ApiError> {
        let conn = Connection::open_with_flags(&self.db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
            .map_err(|e| ApiError::internal(e.to_string()))?;
        conn.busy_timeout(Duration::from_secs(5))
            .map_err(|e| ApiError::internal(e.to_string()))?;
        conn.execute_batch("PRAGMA query_only=ON; BEGIN;")
            .map_err(|e| ApiError::internal(e.to_string()))?;
        Ok(conn)
    }

    pub fn get_meta(&self, snapshot_id: &str) -> Result<SnapshotMeta, ApiError> {
        let conn = self.open_reader()?;
        let raw: Option<String> = conn
            .query_row(
                "SELECT meta_json FROM snapshots WHERE snapshot_id = ?1",
                params![snapshot_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let Some(raw) = raw else {
            return Err(ApiError::not_found(
                error_codes::SNAPSHOT_NOT_FOUND,
                format!("未知快照：{snapshot_id}"),
            ));
        };
        serde_json::from_str(&raw)
            .map_err(|e| ApiError::internal(format!("corrupt snapshot meta: {e}")))
    }

    pub fn get_bars(
        &self,
        snapshot_id: &str,
        timeframe: &str,
    ) -> Result<(SnapshotMeta, Vec<CompactBar>), ApiError> {
        let conn = self.open_reader()?;
        let raw_meta: Option<String> = conn
            .query_row(
                "SELECT meta_json FROM snapshots WHERE snapshot_id = ?1",
                params![snapshot_id],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let Some(raw_meta) = raw_meta else {
            return Err(ApiError::not_found(
                error_codes::SNAPSHOT_NOT_FOUND,
                format!("未知快照：{snapshot_id}"),
            ));
        };
        let meta: SnapshotMeta = serde_json::from_str(&raw_meta)
            .map_err(|e| ApiError::internal(format!("corrupt snapshot meta: {e}")))?;

        let raw_bars: Option<String> = conn
            .query_row(
                "SELECT bars_json FROM snapshot_frames WHERE snapshot_id = ?1 AND timeframe = ?2",
                params![snapshot_id, timeframe],
                |r| r.get(0),
            )
            .optional()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let Some(raw_bars) = raw_bars else {
            return Err(ApiError::not_found(
                error_codes::SNAPSHOT_NOT_FOUND,
                format!("快照 {snapshot_id} 不包含周期 {timeframe}"),
            ));
        };
        let bars: Vec<CompactBar> = serde_json::from_str(&raw_bars)
            .map_err(|e| ApiError::internal(format!("corrupt snapshot bars: {e}")))?;
        Ok((meta, bars))
    }

    pub fn get_derivatives(
        &self,
        snapshot_id: &str,
        timeframe: &str,
    ) -> Result<(SnapshotMeta, Vec<DerivativePoint>), ApiError> {
        let conn = self.open_reader()?;
        let row: Option<(String, String)> = conn
            .query_row(
                "SELECT meta_json, derivatives_json FROM snapshots WHERE snapshot_id = ?1",
                params![snapshot_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()
            .map_err(|e| ApiError::internal(e.to_string()))?;
        let Some((raw_meta, raw_deriv)) = row else {
            return Err(ApiError::not_found(
                error_codes::SNAPSHOT_NOT_FOUND,
                format!("未知快照：{snapshot_id}"),
            ));
        };
        let meta: SnapshotMeta = serde_json::from_str(&raw_meta)
            .map_err(|e| ApiError::internal(format!("corrupt snapshot meta: {e}")))?;
        if !meta.timeframes.iter().any(|tf| tf == timeframe) {
            return Err(ApiError::not_found(
                error_codes::SNAPSHOT_NOT_FOUND,
                format!("快照 {snapshot_id} 不包含周期 {timeframe}"),
            ));
        }
        let points: Vec<DerivativePoint> = serde_json::from_str(&raw_deriv)
            .map_err(|e| ApiError::internal(format!("corrupt snapshot derivatives: {e}")))?;
        Ok((meta, points))
    }

    pub fn checkpoint_and_close(&self) -> Result<(), ApiError> {
        if let Ok(conn) = self.writer.lock() {
            conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
                .map_err(|e| ApiError::internal(e.to_string()))?;
        }
        Ok(())
    }
}
