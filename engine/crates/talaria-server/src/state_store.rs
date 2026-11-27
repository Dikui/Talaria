//! Local SQLite workbench state store (`state/workbench_state.sqlite`) in WAL mode
//! with a dedicated single-writer actor thread and lockless WAL snapshot reads.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::thread;
use std::time::Duration;

use rusqlite::{Connection, OpenFlags, OptionalExtension, TransactionBehavior, params};
use rust_decimal::Decimal;
use serde::Serialize;
use sha2::{Digest, Sha256};
use talaria_contracts::decimal::{serde_decimal_str, serde_decimal_str_vec};
use talaria_contracts::{
    AgentAnnotationRecord, AlertRecord, AlertStatus, AlertTriggerRecord,
    AnnotationAdoptionRecord, AnnotationSessionSummary, AnnotationStatus, BriefingContent,
    BriefingStatus, ConfirmedDraft, Direction, DraftConfirmation, DraftContent,
    DraftDiscardRequest, DraftReceipt, DraftRecord, DraftStatus, DrawingKind, DrawingPoint,
    DrawingRecord, DrawingStyle, EvidenceRef, KeyLevelAdoptionRecord, KeyLevelIgnoreRecord,
    PaperCommand, PaperCommandReceipt, PaperCommandRecord, ScannerBriefing, error_codes,
};
use tokio::sync::{mpsc, oneshot};
use uuid::Uuid;

use crate::error::ApiError;

pub const WORKBENCH_STATE_DB: &str = "state/workbench_state.sqlite";
pub const LEGACY_COLLAB_DB: &str = "state/collaboration_state.sqlite";
pub const SYSTEM_GROUP_POSITION: i64 = 1_000_000;
pub const SCANNER_FOCUS_GROUP_ID: &str = "scanner-focus";

const SCHEMA_SQL: &str = "
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_groups (
    group_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    kind TEXT NOT NULL,
    is_system INTEGER NOT NULL CHECK (is_system IN (0, 1)),
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    group_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (group_id, instrument_id),
    FOREIGN KEY (group_id) REFERENCES watchlist_groups(group_id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS watchlist_items_group_idx
    ON watchlist_items(group_id, position);
CREATE TABLE IF NOT EXISTS drawings (
    drawing_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER
);
CREATE INDEX IF NOT EXISTS drawings_by_instrument
    ON drawings (instrument_id, deleted_at);
CREATE TABLE IF NOT EXISTS drawing_versions (
    drawing_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    visible_timeframes TEXT NOT NULL,
    points TEXT NOT NULL,
    text TEXT NOT NULL,
    style TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER,
    PRIMARY KEY (drawing_id, version)
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    repeat_flag INTEGER NOT NULL CHECK (repeat_flag IN (0, 1)),
    next_arm_ts INTEGER,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    deleted_at INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS alerts_by_instrument
    ON alerts (instrument_id, deleted_at);
CREATE INDEX IF NOT EXISTS alerts_by_status
    ON alerts (status, deleted_at);
CREATE TABLE IF NOT EXISTS alert_triggers (
    trigger_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    triggered_ts INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS alert_triggers_by_time
    ON alert_triggers (triggered_ts DESC);
CREATE INDEX IF NOT EXISTS alert_triggers_by_alert
    ON alert_triggers (alert_id, triggered_ts DESC);
CREATE TABLE IF NOT EXISTS key_level_ignores (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    ignored INTEGER NOT NULL CHECK (ignored IN (0, 1)),
    updated_ts INTEGER NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_ignores_by_instrument
    ON key_level_ignores (instrument_id, updated_ts);
CREATE TABLE IF NOT EXISTS key_level_adoptions (
    instrument_id TEXT NOT NULL,
    level_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    drawing_id TEXT NOT NULL,
    adopted_ts INTEGER NOT NULL,
    timeframe TEXT NOT NULL,
    PRIMARY KEY (instrument_id, level_id)
);
CREATE INDEX IF NOT EXISTS key_level_adoptions_by_instrument
    ON key_level_adoptions (instrument_id, adopted_ts DESC);
CREATE TABLE IF NOT EXISTS agent_annotations (
    annotation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    session_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    ts_anchor INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    updated_ts INTEGER NOT NULL,
    adopted_drawing_id TEXT,
    archived_reason TEXT,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS agent_annotations_by_instrument
    ON agent_annotations (instrument_id, status, created_ts);
CREATE INDEX IF NOT EXISTS agent_annotations_by_snapshot
    ON agent_annotations (snapshot_id, status);
CREATE INDEX IF NOT EXISTS agent_annotations_by_session
    ON agent_annotations (session_id, status);
CREATE TABLE IF NOT EXISTS annotation_adoptions (
    annotation_id TEXT PRIMARY KEY,
    instrument_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    session_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    drawing_id TEXT NOT NULL,
    adopted_ts INTEGER NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS annotation_adoptions_by_instrument
    ON annotation_adoptions (instrument_id, adopted_ts DESC);
CREATE TABLE IF NOT EXISTS scanner_briefings (
    briefing_id TEXT PRIMARY KEY,
    trigger_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    status TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    read_ts INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS scanner_briefings_by_time
    ON scanner_briefings (created_ts DESC, briefing_id DESC);
CREATE TABLE IF NOT EXISTS paper_drafts (
    draft_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    sub_account_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    content TEXT NOT NULL,
    basis_cutoff_ts INTEGER NOT NULL,
    created_ts INTEGER NOT NULL,
    confirmation TEXT,
    claimed INTEGER NOT NULL DEFAULT 0,
    receipt TEXT,
    discarded_at INTEGER,
    discarded_by TEXT,
    discard_reason TEXT
);
CREATE TABLE IF NOT EXISTS paper_submissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    UNIQUE (draft_id, version)
);
CREATE INDEX IF NOT EXISTS paper_draft_inbox
    ON paper_drafts (sub_account_id, instrument_id)
    WHERE confirmation IS NOT NULL AND receipt IS NULL AND discarded_at IS NULL;
CREATE TABLE IF NOT EXISTS paper_commands (
    command_id TEXT PRIMARY KEY,
    sub_account_id TEXT NOT NULL,
    command TEXT NOT NULL,
    claimed INTEGER NOT NULL DEFAULT 0,
    receipt TEXT,
    created_ts INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_command_submissions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    command_id TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS paper_command_inbox
    ON paper_commands (sub_account_id)
    WHERE receipt IS NULL;
CREATE TABLE IF NOT EXISTS custom_instruments (
    pack_name TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    PRIMARY KEY (pack_name, instrument_id)
);
";

const MIGRATION_KEY: &str = "migration.from_collaboration_v1";
const PAPER_DRAFTS_MIGRATION_KEY: &str = "migration.paper_drafts_from_collaboration_v1";
pub const DRAFT_CONFLICT: &str = "DRAFT_CONFLICT";
pub const COMMAND_CONFLICT: &str = "COMMAND_CONFLICT";

type WriteTask = Box<dyn FnOnce(&mut Connection, i64) + Send + 'static>;

enum WriterCommand {
    Run(WriteTask),
    Shutdown(oneshot::Sender<Result<(), ApiError>>),
}

pub type NowMsFn = Arc<dyn Fn() -> i64 + Send + Sync>;

/// Thread-safe handle to the `state/workbench_state.sqlite` single-writer actor and WAL reader.
#[derive(Clone)]
pub struct WorkbenchStateStore {
    db_path: PathBuf,
    now_ms: NowMsFn,
    tx: mpsc::UnboundedSender<WriterCommand>,
}

impl WorkbenchStateStore {
    /// Open `state/workbench_state.sqlite` in WAL mode, migrate legacy tables from
    /// `state/collaboration_state.sqlite` if present, and spawn the single-writer actor thread.
    pub fn open(data_dir: &Path, now_ms: NowMsFn) -> Result<Self, ApiError> {
        let db_path = data_dir.join(WORKBENCH_STATE_DB);
        let legacy_path = data_dir.join(LEGACY_COLLAB_DB);
        if let Some(parent) = db_path.parent() {
            fs::create_dir_all(parent).map_err(|e| ApiError::internal(e.to_string()))?;
        }

        let (tx, mut rx) = mpsc::unbounded_channel::<WriterCommand>();
        let (init_tx, init_rx) = std::sync::mpsc::channel::<Result<(), ApiError>>();
        let writer_db_path = db_path.clone();
        let writer_now = Arc::clone(&now_ms);

        thread::Builder::new()
            .name("workbench-state-writer".to_owned())
            .spawn(move || {
                let mut conn = match open_writer_connection(&writer_db_path, &legacy_path, writer_now()) {
                    Ok(c) => {
                        let _ = init_tx.send(Ok(()));
                        c
                    }
                    Err(err) => {
                        let _ = init_tx.send(Err(err));
                        return;
                    }
                };

                while let Some(cmd) = rx.blocking_recv() {
                    match cmd {
                        WriterCommand::Run(task) => {
                            let now = writer_now();
                            task(&mut conn, now);
                        }
                        WriterCommand::Shutdown(reply) => {
                            let res = checkpoint_wal(&conn);
                            drop(conn);
                            let _ = reply.send(res);
                            break;
                        }
                    }
                }
            })
            .map_err(|e| ApiError::internal(format!("failed to spawn state writer thread: {e}")))?;

        init_rx
            .recv()
            .map_err(|e| ApiError::internal(format!("state writer init channel failed: {e}")))??;

        Ok(Self {
            db_path,
            now_ms,
            tx,
        })
    }

    pub fn db_path(&self) -> &Path {
        &self.db_path
    }

    pub fn now_ms(&self) -> i64 {
        (self.now_ms)()
    }

    /// Execute a serialized write closure on the dedicated writer thread.
    pub async fn write<T, F>(&self, f: F) -> Result<T, ApiError>
    where
        T: Send + 'static,
        F: FnOnce(&mut Connection, i64) -> Result<T, ApiError> + Send + 'static,
    {
        let (reply_tx, reply_rx) = oneshot::channel();
        let task: WriteTask = Box::new(move |conn, now| {
            let result = f(conn, now);
            let _ = reply_tx.send(result);
        });
        self.tx
            .send(WriterCommand::Run(task))
            .map_err(|_| ApiError::internal("state writer actor has stopped"))?;
        reply_rx
            .await
            .map_err(|_| ApiError::internal("state writer actor dropped reply"))?
    }

    /// Execute a lockless read-only WAL snapshot query on a blocking thread.
    pub async fn read<T, F>(&self, f: F) -> Result<T, ApiError>
    where
        T: Send + 'static,
        F: FnOnce(&Connection) -> Result<T, ApiError> + Send + 'static,
    {
        let path = self.db_path.clone();
        tokio::task::spawn_blocking(move || {
            let conn = open_reader_connection(&path)?;
            f(&conn)
        })
        .await
        .map_err(|e| ApiError::internal(format!("state read join error: {e}")))?
    }

    /// Synchronous read helper for startup or sync callbacks.
    pub fn read_sync<T, F>(&self, f: F) -> Result<T, ApiError>
    where
        F: FnOnce(&Connection) -> Result<T, ApiError>,
    {
        let conn = open_reader_connection(&self.db_path)?;
        f(&conn)
    }

    /// Checkpoint and truncate WAL, then shut down the dedicated writer thread cleanly.
    pub async fn shutdown(&self) -> Result<(), ApiError> {
        let (reply_tx, reply_rx) = oneshot::channel();
        if self.tx.send(WriterCommand::Shutdown(reply_tx)).is_err() {
            return Ok(());
        }
        reply_rx.await.unwrap_or(Ok(()))
    }
}

fn map_sqlite_err(err: rusqlite::Error) -> ApiError {
    ApiError::internal(format!("SQLite error: {err}"))
}

fn open_writer_connection(
    db_path: &Path,
    legacy_path: &Path,
    now: i64,
) -> Result<Connection, ApiError> {
    let mut conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )
    .map_err(map_sqlite_err)?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(map_sqlite_err)?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA synchronous=FULL;
         PRAGMA foreign_keys=ON;",
    )
    .map_err(map_sqlite_err)?;
    conn.execute_batch(SCHEMA_SQL).map_err(map_sqlite_err)?;
    ensure_paper_drafts_columns(&conn)?;
    maybe_migrate_legacy(&mut conn, legacy_path, now)?;
    maybe_migrate_legacy_paper_drafts(&mut conn, legacy_path, now)?;
    Ok(conn)
}

fn open_reader_connection(db_path: &Path) -> Result<Connection, ApiError> {
    let conn = Connection::open_with_flags(db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)
        .map_err(map_sqlite_err)?;
    conn.busy_timeout(Duration::from_secs(5))
        .map_err(map_sqlite_err)?;
    conn.execute_batch("PRAGMA query_only=ON; BEGIN;")
        .map_err(map_sqlite_err)?;
    Ok(conn)
}

fn checkpoint_wal(conn: &Connection) -> Result<(), ApiError> {
    conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);")
        .map_err(map_sqlite_err)?;
    Ok(())
}

fn maybe_migrate_legacy(
    conn: &mut Connection,
    legacy_path: &Path,
    now: i64,
) -> Result<(), ApiError> {
    let already: Option<String> = conn
        .query_row(
            "SELECT value FROM meta WHERE key = ?1",
            params![MIGRATION_KEY],
            |r| r.get(0),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    if already.is_some() || !legacy_path.is_file() {
        return Ok(());
    }

    let legacy_str = legacy_path.to_string_lossy().into_owned();
    conn.execute("ATTACH DATABASE ?1 AS legacy", params![legacy_str])
        .map_err(map_sqlite_err)?;

    let migrate_res = (|| -> Result<(), ApiError> {
        let mut stmt = conn
            .prepare("SELECT name FROM legacy.sqlite_master WHERE type = 'table'")
            .map_err(map_sqlite_err)?;
        let table_rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(map_sqlite_err)?;
        let mut legacy_tables = HashSet::new();
        for item in table_rows {
            legacy_tables.insert(item.map_err(map_sqlite_err)?);
        }
        drop(stmt);

        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(map_sqlite_err)?;
        let copy_order = [
            "meta",
            "events",
            "watchlist_groups",
            "watchlist_items",
            "drawings",
            "drawing_versions",
            "alerts",
            "alert_triggers",
            "key_level_ignores",
            "key_level_adoptions",
            "agent_annotations",
            "annotation_adoptions",
        ];
        for tbl in copy_order {
            if legacy_tables.contains(tbl) {
                let main_cols = table_columns(&tx, "main", tbl)?;
                let legacy_cols: HashSet<String> =
                    table_columns(&tx, "legacy", tbl)?.into_iter().collect();
                let shared_cols: Vec<String> = main_cols
                    .into_iter()
                    .filter(|col| legacy_cols.contains(col))
                    .map(|col| format!("\"{col}\""))
                    .collect();
                if !shared_cols.is_empty() {
                    let cols_sql = shared_cols.join(", ");
                    let sql = format!(
                        "INSERT OR IGNORE INTO main.\"{tbl}\" ({cols_sql}) SELECT {cols_sql} FROM legacy.\"{tbl}\""
                    );
                    tx.execute(&sql, []).map_err(map_sqlite_err)?;
                }
            }
        }
        if legacy_tables.contains("scanner_briefings") {
            migrate_legacy_scanner_briefings(&tx)?;
        }
        tx.execute(
            "INSERT INTO main.meta (key, value, updated_ts) VALUES (?1, ?2, ?3)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
            params![MIGRATION_KEY, "true", now],
        )
        .map_err(map_sqlite_err)?;
        tx.commit().map_err(map_sqlite_err)?;
        Ok(())
    })();

    let _ = conn.execute("DETACH DATABASE legacy", []);
    migrate_res
}

fn table_columns(conn: &Connection, schema: &str, table: &str) -> Result<Vec<String>, ApiError> {
    let sql = format!("PRAGMA {schema}.table_info(\"{table}\")");
    let mut stmt = conn.prepare(&sql).map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(map_sqlite_err)?;
    let mut cols = Vec::new();
    for col in rows {
        cols.push(col.map_err(map_sqlite_err)?);
    }
    Ok(cols)
}

fn migrate_legacy_scanner_briefings(conn: &Connection) -> Result<(), ApiError> {
    let legacy_cols: HashSet<String> =
        table_columns(conn, "legacy", "scanner_briefings")?
            .into_iter()
            .collect();
    if legacy_cols.contains("payload") {
        let main_cols = table_columns(conn, "main", "scanner_briefings")?;
        let shared: Vec<String> = main_cols
            .into_iter()
            .filter(|col| legacy_cols.contains(col))
            .map(|col| format!("\"{col}\""))
            .collect();
        if !shared.is_empty() {
            let cols_sql = shared.join(", ");
            let sql = format!(
                "INSERT OR IGNORE INTO main.scanner_briefings ({cols_sql}) SELECT {cols_sql} FROM legacy.scanner_briefings"
            );
            conn.execute(&sql, []).map_err(map_sqlite_err)?;
        }
        return Ok(());
    }

    if !(legacy_cols.contains("id") && legacy_cols.contains("instrument")) {
        tracing::warn!(
            "skipping unrecognized legacy scanner_briefings schema: {:?}",
            legacy_cols
        );
        return Ok(());
    }

    let mut select_stmt = conn
        .prepare(
            "SELECT id, trigger_id, snapshot_id, instrument, timeframe, status, summary, evidence_refs, abstain_reason, created_ts, read_ts FROM legacy.scanner_briefings",
        )
        .map_err(map_sqlite_err)?;
    let rows = select_stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, String>(3)?,
                r.get::<_, String>(4)?,
                r.get::<_, String>(5)?,
                r.get::<_, Option<String>>(6)?,
                r.get::<_, String>(7)?,
                r.get::<_, Option<String>>(8)?,
                r.get::<_, i64>(9)?,
                r.get::<_, Option<i64>>(10)?,
            ))
        })
        .map_err(map_sqlite_err)?;

    let mut insert_stmt = conn
        .prepare(
            "INSERT OR IGNORE INTO main.scanner_briefings
             (briefing_id, trigger_id, snapshot_id, instrument_id, timeframe, status, created_ts, read_ts, payload)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        )
        .map_err(map_sqlite_err)?;

    for row in rows {
        let (
            briefing_id,
            trigger_id,
            snapshot_id,
            instrument_id,
            timeframe,
            status_raw,
            summary_raw,
            evidence_raw,
            abstain_reason,
            created_ts,
            read_ts,
        ) = row.map_err(map_sqlite_err)?;

        let status = match serde_json::from_value::<BriefingStatus>(serde_json::Value::String(
            status_raw.clone(),
        )) {
            Ok(val) => val,
            Err(err) => {
                tracing::warn!(
                    briefing_id = %briefing_id,
                    status = %status_raw,
                    "skipping legacy scanner_briefing row with invalid status: {err}"
                );
                continue;
            }
        };
        let summary = match summary_raw {
            Some(ref raw) if !raw.trim().is_empty() => {
                match serde_json::from_str::<BriefingContent>(raw) {
                    Ok(val) => Some(val),
                    Err(err) => {
                        tracing::warn!(
                            briefing_id = %briefing_id,
                            "skipping legacy scanner_briefing row with invalid summary: {err}"
                        );
                        continue;
                    }
                }
            }
            _ => None,
        };
        let evidence_refs = match serde_json::from_str::<Vec<EvidenceRef>>(&evidence_raw) {
            Ok(val) => val,
            Err(err) => {
                tracing::warn!(
                    briefing_id = %briefing_id,
                    "skipping legacy scanner_briefing row with invalid evidence_refs: {err}"
                );
                continue;
            }
        };

        let briefing = ScannerBriefing {
            id: briefing_id.clone(),
            trigger_id: trigger_id.clone(),
            snapshot_id: snapshot_id.clone(),
            instrument_id: instrument_id.clone(),
            timeframe: timeframe.clone(),
            status,
            summary,
            evidence_refs,
            abstain_reason,
            created_ts,
            read_ts,
        };
        let payload = serde_json::to_string(&briefing)
            .map_err(|e| ApiError::internal(format!("failed to encode briefing payload: {e}")))?;

        insert_stmt
            .execute(params![
                briefing_id,
                trigger_id,
                snapshot_id,
                instrument_id,
                timeframe,
                status_raw,
                created_ts,
                read_ts,
                payload
            ])
            .map_err(map_sqlite_err)?;
    }

    Ok(())
}

fn ensure_paper_drafts_columns(conn: &Connection) -> Result<(), ApiError> {
    let cols: HashSet<String> = table_columns(conn, "main", "paper_drafts")?
        .into_iter()
        .collect();
    if !cols.contains("discarded_at") {
        conn.execute("ALTER TABLE paper_drafts ADD COLUMN discarded_at INTEGER", [])
            .map_err(map_sqlite_err)?;
    }
    if !cols.contains("discarded_by") {
        conn.execute("ALTER TABLE paper_drafts ADD COLUMN discarded_by TEXT", [])
            .map_err(map_sqlite_err)?;
    }
    if !cols.contains("discard_reason") {
        conn.execute("ALTER TABLE paper_drafts ADD COLUMN discard_reason TEXT", [])
            .map_err(map_sqlite_err)?;
    }
    conn.execute(
        "UPDATE paper_drafts SET receipt = NULL
         WHERE json_extract(receipt, '$.reason') IN ('DRAFT_INTERRUPTED', 'DRAFT_DATA_UNAVAILABLE')",
        [],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

fn maybe_migrate_legacy_paper_drafts(
    conn: &mut Connection,
    legacy_path: &Path,
    now: i64,
) -> Result<(), ApiError> {
    let already: Option<String> = conn
        .query_row(
            "SELECT value FROM meta WHERE key = ?1",
            params![PAPER_DRAFTS_MIGRATION_KEY],
            |r| r.get(0),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    if already.is_some() || !legacy_path.is_file() {
        return Ok(());
    }

    let legacy_str = legacy_path.to_string_lossy().into_owned();
    conn.execute("ATTACH DATABASE ?1 AS legacy_paper", params![legacy_str])
        .map_err(map_sqlite_err)?;

    let migrate_res = (|| -> Result<(), ApiError> {
        let mut stmt = conn
            .prepare("SELECT name FROM legacy_paper.sqlite_master WHERE type = 'table'")
            .map_err(map_sqlite_err)?;
        let table_rows = stmt
            .query_map([], |r| r.get::<_, String>(0))
            .map_err(map_sqlite_err)?;
        let mut legacy_tables = HashSet::new();
        for item in table_rows {
            legacy_tables.insert(item.map_err(map_sqlite_err)?);
        }
        drop(stmt);

        let tx = conn
            .transaction_with_behavior(TransactionBehavior::Immediate)
            .map_err(map_sqlite_err)?;

        if legacy_tables.contains("paper_drafts") {
            let legacy_cols: HashSet<String> =
                table_columns(&tx, "legacy_paper", "paper_drafts")?
                    .into_iter()
                    .collect();
            let has_discarded = legacy_cols.contains("discarded_at");
            let discard_filter = if has_discarded {
                "AND discarded_at IS NULL"
            } else {
                ""
            };
            let insert_sql = format!(
                "INSERT OR IGNORE INTO main.paper_drafts
                 (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts, confirmation, claimed, receipt)
                 SELECT draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts, confirmation, claimed,
                        CASE
                            WHEN json_extract(receipt, '$.reason') IN ('DRAFT_INTERRUPTED', 'DRAFT_DATA_UNAVAILABLE') THEN NULL
                            ELSE receipt
                        END
                 FROM legacy_paper.paper_drafts
                 WHERE (receipt IS NULL OR json_extract(receipt, '$.reason') IN ('DRAFT_INTERRUPTED', 'DRAFT_DATA_UNAVAILABLE'))
                 {discard_filter}"
            );
            tx.execute(&insert_sql, []).map_err(map_sqlite_err)?;

            if legacy_tables.contains("paper_submissions") {
                tx.execute(
                    "INSERT OR IGNORE INTO main.paper_submissions(draft_id, version)
                     SELECT s.draft_id, s.version
                     FROM legacy_paper.paper_submissions s
                     JOIN main.paper_drafts d ON d.draft_id = s.draft_id AND d.version = s.version
                     WHERE d.confirmation IS NOT NULL AND d.receipt IS NULL AND d.discarded_at IS NULL
                     ORDER BY s.seq ASC",
                    [],
                )
                .map_err(map_sqlite_err)?;
            }
            tx.execute(
                "INSERT OR IGNORE INTO main.paper_submissions(draft_id, version)
                 SELECT draft_id, version FROM main.paper_drafts
                 WHERE confirmation IS NOT NULL AND receipt IS NULL AND discarded_at IS NULL
                 ORDER BY json_extract(confirmation, '$.confirmed_ts'), rowid",
                [],
            )
            .map_err(map_sqlite_err)?;
        }

        tx.execute(
            "INSERT INTO main.meta (key, value, updated_ts) VALUES (?1, ?2, ?3)
             ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
            params![PAPER_DRAFTS_MIGRATION_KEY, "true", now],
        )
        .map_err(map_sqlite_err)?;
        tx.commit().map_err(map_sqlite_err)?;
        Ok(())
    })();

    let _ = conn.execute("DETACH DATABASE legacy_paper", []);
    migrate_res
}

// ============================================================================
// Paper Drafts & Outbound Submission Queue Repository (ADR-0025)
// ============================================================================

/// Canonical digest matching Python `DraftContent.digest()` byte-for-byte.
/// When `round_id` is `None`, `round_id` and `inherited_barriers` are excluded
/// so existing open-position drafts keep the exact legacy SHA-256 hash.
pub fn draft_content_digest(content: &DraftContent) -> Result<String, ApiError> {
    #[derive(Serialize)]
    struct LegacyDraftContent<'a> {
        sub_account_id: &'a str,
        instrument_id: &'a str,
        snapshot_id: &'a str,
        consultation_id: &'a Option<String>,
        direction: Direction,
        #[serde(with = "serde_decimal_str")]
        entry_low: Decimal,
        #[serde(with = "serde_decimal_str")]
        entry_high: Decimal,
        #[serde(with = "serde_decimal_str")]
        invalidation_stop: Decimal,
        #[serde(with = "serde_decimal_str_vec")]
        take_profit: &'a [Decimal],
        valid_until: Option<i64>,
    }

    let bytes = if content.round_id.is_none() {
        let legacy = LegacyDraftContent {
            sub_account_id: &content.sub_account_id,
            instrument_id: &content.instrument_id,
            snapshot_id: &content.snapshot_id,
            consultation_id: &content.consultation_id,
            direction: content.direction,
            entry_low: content.entry_low,
            entry_high: content.entry_high,
            invalidation_stop: content.invalidation_stop,
            take_profit: &content.take_profit,
            valid_until: content.valid_until,
        };
        serde_json::to_vec(&legacy)
    } else {
        serde_json::to_vec(content)
    }
    .map_err(|e| ApiError::internal(format!("failed to encode draft content for digest: {e}")))?;
    Ok(hex::encode(Sha256::digest(&bytes)))
}

fn is_draft_expired(content: &DraftContent, now: i64) -> bool {
    content.valid_until.is_some_and(|deadline| now > deadline)
}

#[allow(clippy::too_many_arguments)]
fn build_draft_record(
    draft_id: String,
    version: u64,
    content_raw: &str,
    confirmation_raw: Option<String>,
    claimed: bool,
    receipt_raw: Option<String>,
    discarded_at: Option<i64>,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let content: DraftContent = serde_json::from_str(content_raw)
        .map_err(|e| ApiError::internal(format!("corrupt paper_drafts content: {e}")))?;
    let content_hash = draft_content_digest(&content)?;
    let receipt: Option<DraftReceipt> = match receipt_raw {
        Some(raw) => Some(
            serde_json::from_str(&raw)
                .map_err(|e| ApiError::internal(format!("corrupt paper_drafts receipt: {e}")))?,
        ),
        None => None,
    };
    let status = if discarded_at.is_some() {
        DraftStatus::Discarded
    } else if receipt.is_some() || claimed {
        DraftStatus::Confirmed
    } else if is_draft_expired(&content, now) {
        DraftStatus::Expired
    } else if confirmation_raw.is_some() {
        DraftStatus::Confirmed
    } else if version == 1 {
        DraftStatus::Staged
    } else {
        DraftStatus::Modified
    };
    Ok(DraftRecord {
        draft_id,
        version,
        content_hash,
        content,
        status,
        claimed,
        receipt,
    })
}

pub fn paper_drafts_list(conn: &Connection, now: i64) -> Result<Vec<DraftRecord>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT draft_id, version, content, confirmation, claimed, receipt, discarded_at
             FROM paper_drafts ORDER BY rowid DESC",
        )
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, Option<String>>(3)?,
                r.get::<_, i64>(4)?,
                r.get::<_, Option<String>>(5)?,
                r.get::<_, Option<i64>>(6)?,
            ))
        })
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for row in rows {
        let (draft_id, version, content_raw, confirmation, claimed, receipt, discarded_at) =
            row.map_err(map_sqlite_err)?;
        out.push(build_draft_record(
            draft_id,
            version.max(1) as u64,
            &content_raw,
            confirmation,
            claimed != 0,
            receipt,
            discarded_at,
            now,
        )?);
    }
    Ok(out)
}

pub fn paper_drafts_get(
    conn: &Connection,
    draft_id: &str,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let row = conn
        .query_row(
            "SELECT draft_id, version, content, confirmation, claimed, receipt, discarded_at
             FROM paper_drafts WHERE draft_id = ?1",
            params![draft_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, Option<String>>(3)?,
                    r.get::<_, i64>(4)?,
                    r.get::<_, Option<String>>(5)?,
                    r.get::<_, Option<i64>>(6)?,
                ))
            },
        )
        .optional()
        .map_err(map_sqlite_err)?;
    let Some((id, version, content_raw, confirmation, claimed, receipt, discarded_at)) = row else {
        return Err(ApiError::not_found(error_codes::NOT_FOUND, "找不到交易草案"));
    };
    build_draft_record(
        id,
        version.max(1) as u64,
        &content_raw,
        confirmation,
        claimed != 0,
        receipt,
        discarded_at,
        now,
    )
}

pub fn paper_drafts_save(
    conn: &mut Connection,
    content: &DraftContent,
    basis_cutoff_ts: i64,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let draft_id = Uuid::new_v4().simple().to_string();
    let encoded = serde_json::to_string(content).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO paper_drafts
         (draft_id, version, sub_account_id, instrument_id, content, basis_cutoff_ts, created_ts)
         VALUES (?1, 1, ?2, ?3, ?4, ?5, ?6)",
        params![
            draft_id,
            content.sub_account_id,
            content.instrument_id,
            encoded,
            basis_cutoff_ts,
            now
        ],
    )
    .map_err(map_sqlite_err)?;
    paper_drafts_get(conn, &draft_id, now)
}

pub fn paper_drafts_edit(
    conn: &mut Connection,
    draft_id: &str,
    version: u64,
    content: &DraftContent,
    basis_cutoff_ts: i64,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let record = paper_drafts_get(conn, draft_id, now)?;
    if record.version != version
        || record.claimed
        || record.receipt.is_some()
        || matches!(record.status, DraftStatus::Discarded | DraftStatus::Expired)
    {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "草案已更新、已终结或已送达账户，请重新读取",
        ));
    }
    let encoded = serde_json::to_string(content).map_err(|e| ApiError::internal(e.to_string()))?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "UPDATE paper_drafts
         SET version = version + 1, sub_account_id = ?1, instrument_id = ?2,
             content = ?3, basis_cutoff_ts = ?4, confirmation = NULL
         WHERE draft_id = ?5",
        params![
            content.sub_account_id,
            content.instrument_id,
            encoded,
            basis_cutoff_ts,
            draft_id
        ],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    paper_drafts_get(conn, draft_id, now)
}

pub fn paper_drafts_confirm(
    conn: &mut Connection,
    draft_id: &str,
    confirmation: &DraftConfirmation,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let record = paper_drafts_get(conn, draft_id, now)?;
    if (record.version, record.content_hash.as_str())
        != (confirmation.version, confirmation.content_hash.as_str())
    {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "草案版本已更新，请重新审阅后确认",
        ));
    }
    if matches!(record.status, DraftStatus::Discarded | DraftStatus::Expired) {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "草案已丢弃或已过期，无法确认",
        ));
    }
    if record.status == DraftStatus::Confirmed {
        return Ok(record);
    }

    let (basis_cutoff_ts, created_ts): (i64, i64) = conn
        .query_row(
            "SELECT basis_cutoff_ts, created_ts FROM paper_drafts WHERE draft_id = ?1",
            params![draft_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .map_err(map_sqlite_err)?;
    let confirmed = ConfirmedDraft {
        version: confirmation.version,
        content_hash: confirmation.content_hash.clone(),
        confirmed_by: confirmation.confirmed_by.trim().to_owned(),
        draft_id: draft_id.to_owned(),
        content: record.content,
        basis_cutoff_ts,
        created_ts,
        confirmed_ts: created_ts.max(now),
    };
    let encoded =
        serde_json::to_string(&confirmed).map_err(|e| ApiError::internal(e.to_string()))?;

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO paper_submissions(draft_id, version) VALUES (?1, ?2)",
        params![draft_id, record.version as i64],
    )
    .map_err(map_sqlite_err)?;
    tx.execute(
        "UPDATE paper_drafts SET confirmation = ?1 WHERE draft_id = ?2",
        params![encoded, draft_id],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    paper_drafts_get(conn, draft_id, now)
}

pub fn paper_drafts_discard(
    conn: &mut Connection,
    draft_id: &str,
    discard: &DraftDiscardRequest,
    now: i64,
) -> Result<DraftRecord, ApiError> {
    let record = paper_drafts_get(conn, draft_id, now)?;
    if record.status == DraftStatus::Discarded && record.version == discard.version {
        return Ok(record);
    }
    if record.version != discard.version
        || record.claimed
        || record.receipt.is_some()
        || record.status == DraftStatus::Expired
    {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "草案版本已更新、已过期或已送达账户，无法丢弃",
        ));
    }
    let discarded_by = discard.discarded_by.as_deref().map(str::trim);
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "DELETE FROM paper_submissions WHERE draft_id = ?1",
        params![draft_id],
    )
    .map_err(map_sqlite_err)?;
    tx.execute(
        "UPDATE paper_drafts
         SET confirmation = NULL, discarded_at = ?1, discarded_by = ?2, discard_reason = ?3
         WHERE draft_id = ?4",
        params![now, discarded_by, discard.reason.as_deref(), draft_id],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    paper_drafts_get(conn, draft_id, now)
}

pub fn paper_drafts_claim(
    conn: &mut Connection,
    account: &str,
    instrument: &str,
    now: i64,
) -> Result<Option<ConfirmedDraft>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT d.draft_id, d.confirmation, d.claimed
             FROM paper_drafts d
             JOIN paper_submissions s USING (draft_id, version)
             WHERE d.sub_account_id = ?1
               AND d.instrument_id = ?2
               AND d.confirmation IS NOT NULL
               AND d.receipt IS NULL
               AND d.discarded_at IS NULL
             ORDER BY d.claimed DESC, s.seq ASC",
        )
        .map_err(map_sqlite_err)?;
    let candidates: Vec<(String, String, bool)> = stmt
        .query_map(params![account, instrument], |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, i64>(2)? != 0,
            ))
        })
        .map_err(map_sqlite_err)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(map_sqlite_err)?;
    drop(stmt);

    for (draft_id, confirmation_raw, claimed) in candidates {
        let confirmed: ConfirmedDraft = serde_json::from_str(&confirmation_raw)
            .map_err(|e| ApiError::internal(format!("corrupt confirmed draft: {e}")))?;
        if !claimed && is_draft_expired(&confirmed.content, now) {
            continue;
        }
        if !claimed {
            conn.execute(
                "UPDATE paper_drafts SET claimed = 1 WHERE draft_id = ?1",
                params![draft_id],
            )
            .map_err(map_sqlite_err)?;
        }
        return Ok(Some(confirmed));
    }
    Ok(None)
}

pub fn paper_drafts_acknowledge(
    conn: &mut Connection,
    account: &str,
    receipt: &DraftReceipt,
    now: i64,
) -> Result<DraftReceipt, ApiError> {
    if matches!(
        receipt.reason.as_deref(),
        Some("DRAFT_INTERRUPTED" | "DRAFT_DATA_UNAVAILABLE")
    ) {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "缺证据或处理中断的草案仍待处理",
        ));
    }
    let record = paper_drafts_get(conn, &receipt.draft_id, now)?;
    if !record.claimed
        || record.content.sub_account_id != account
        || record.version != receipt.version
    {
        return Err(ApiError::conflict(
            DRAFT_CONFLICT,
            "回执与已送达的草案版本不一致",
        ));
    }
    if let Some(existing) = record.receipt {
        if existing != *receipt {
            return Err(ApiError::conflict(DRAFT_CONFLICT, "草案已登记另一份回执"));
        }
        return Ok(existing);
    }
    let encoded = serde_json::to_string(receipt).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "UPDATE paper_drafts SET receipt = ?1 WHERE draft_id = ?2",
        params![encoded, receipt.draft_id],
    )
    .map_err(map_sqlite_err)?;
    Ok(receipt.clone())
}

// ============================================================================
// Paper Intervention Command Queue (ADR-0025 D1/D3)
// ============================================================================

fn parse_command_record(
    command_raw: String,
    claimed_int: i64,
    receipt_raw: Option<String>,
) -> Result<PaperCommandRecord, ApiError> {
    let command: PaperCommand = serde_json::from_str(&command_raw)
        .map_err(|e| ApiError::internal(format!("corrupt paper_commands payload: {e}")))?;
    let receipt = match receipt_raw {
        Some(text) => Some(
            serde_json::from_str::<PaperCommandReceipt>(&text)
                .map_err(|e| ApiError::internal(format!("corrupt paper_commands receipt: {e}")))?,
        ),
        None => None,
    };
    Ok(PaperCommandRecord {
        command,
        claimed: claimed_int != 0,
        receipt,
    })
}

pub fn paper_commands_get(
    conn: &Connection,
    command_id: &str,
) -> Result<Option<PaperCommandRecord>, ApiError> {
    let row = conn
        .query_row(
            "SELECT command, claimed, receipt FROM paper_commands WHERE command_id = ?1",
            params![command_id],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .optional()
        .map_err(map_sqlite_err)?;
    match row {
        Some((cmd_raw, claimed, receipt_raw)) => {
            Ok(Some(parse_command_record(cmd_raw, claimed, receipt_raw)?))
        }
        None => Ok(None),
    }
}

pub fn paper_commands_list(
    conn: &Connection,
    sub_account_id: Option<&str>,
) -> Result<Vec<PaperCommandRecord>, ApiError> {
    let mut rows = Vec::new();
    if let Some(account) = sub_account_id {
        let mut stmt = conn
            .prepare(
                "SELECT c.command, c.claimed, c.receipt
                 FROM paper_commands c
                 JOIN paper_command_submissions s USING (command_id)
                 WHERE c.sub_account_id = ?1
                 ORDER BY s.seq DESC",
            )
            .map_err(map_sqlite_err)?;
        let iter = stmt
            .query_map(params![account], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ))
            })
            .map_err(map_sqlite_err)?;
        for item in iter {
            let (cmd_raw, claimed, receipt_raw) = item.map_err(map_sqlite_err)?;
            rows.push(parse_command_record(cmd_raw, claimed, receipt_raw)?);
        }
    } else {
        let mut stmt = conn
            .prepare(
                "SELECT c.command, c.claimed, c.receipt
                 FROM paper_commands c
                 JOIN paper_command_submissions s USING (command_id)
                 ORDER BY s.seq DESC",
            )
            .map_err(map_sqlite_err)?;
        let iter = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, i64>(1)?,
                    r.get::<_, Option<String>>(2)?,
                ))
            })
            .map_err(map_sqlite_err)?;
        for item in iter {
            let (cmd_raw, claimed, receipt_raw) = item.map_err(map_sqlite_err)?;
            rows.push(parse_command_record(cmd_raw, claimed, receipt_raw)?);
        }
    }
    Ok(rows)
}

pub fn paper_commands_submit(
    conn: &mut Connection,
    command: &PaperCommand,
    now: i64,
) -> Result<PaperCommandRecord, ApiError> {
    if let Some(existing) = paper_commands_get(conn, &command.command_id)? {
        if existing.command == *command {
            return Ok(existing);
        }
        return Err(ApiError::conflict(
            COMMAND_CONFLICT,
            "command_id 已存在且命令内容不一致",
        ));
    }
    let encoded = serde_json::to_string(command).map_err(|e| ApiError::internal(e.to_string()))?;
    let tx = conn.transaction().map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO paper_commands (command_id, sub_account_id, command, claimed, receipt, created_ts)
         VALUES (?1, ?2, ?3, 0, NULL, ?4)",
        params![command.command_id, command.sub_account_id, encoded, now],
    )
    .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO paper_command_submissions (command_id) VALUES (?1)",
        params![command.command_id],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(PaperCommandRecord {
        command: command.clone(),
        claimed: false,
        receipt: None,
    })
}

pub fn paper_commands_claim(
    conn: &mut Connection,
    account: &str,
) -> Result<Option<PaperCommand>, ApiError> {
    let candidate: Option<(String, String, bool)> = conn
        .query_row(
            "SELECT c.command_id, c.command, c.claimed
             FROM paper_commands c
             JOIN paper_command_submissions s USING (command_id)
             WHERE c.sub_account_id = ?1 AND c.receipt IS NULL
             ORDER BY c.claimed DESC, s.seq ASC
             LIMIT 1",
            params![account],
            |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)? != 0,
                ))
            },
        )
        .optional()
        .map_err(map_sqlite_err)?;
    let Some((command_id, command_raw, claimed)) = candidate else {
        return Ok(None);
    };
    let command: PaperCommand = serde_json::from_str(&command_raw)
        .map_err(|e| ApiError::internal(format!("corrupt paper command: {e}")))?;
    if !claimed {
        conn.execute(
            "UPDATE paper_commands SET claimed = 1 WHERE command_id = ?1",
            params![command_id],
        )
        .map_err(map_sqlite_err)?;
    }
    Ok(Some(command))
}

pub fn paper_commands_acknowledge(
    conn: &mut Connection,
    account: &str,
    receipt: &PaperCommandReceipt,
) -> Result<PaperCommandReceipt, ApiError> {
    let Some(record) = paper_commands_get(conn, &receipt.command_id)? else {
        return Err(ApiError::not_found(
            error_codes::NOT_FOUND,
            format!("干预命令不存在：{}", receipt.command_id),
        ));
    };
    if !record.claimed || record.command.sub_account_id != account {
        return Err(ApiError::conflict(
            COMMAND_CONFLICT,
            "回执与已送达的干预命令子账户不一致",
        ));
    }
    if let Some(existing) = record.receipt {
        if existing.accepted != receipt.accepted
            || existing.reason != receipt.reason
            || existing.ledger_seq != receipt.ledger_seq
        {
            return Err(ApiError::conflict(
                COMMAND_CONFLICT,
                "干预命令已登记另一份回执",
            ));
        }
        return Ok(existing);
    }
    let normalized = PaperCommandReceipt {
        sub_account_id: Some(account.to_owned()),
        ..receipt.clone()
    };
    let encoded =
        serde_json::to_string(&normalized).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "UPDATE paper_commands SET receipt = ?1 WHERE command_id = ?2",
        params![encoded, receipt.command_id],
    )
    .map_err(map_sqlite_err)?;
    Ok(normalized)
}

// ============================================================================
// Meta Repository
// ============================================================================

pub fn meta_get(conn: &Connection, key: &str) -> Result<Option<serde_json::Value>, ApiError> {
    let raw: Option<String> = conn
        .query_row("SELECT value FROM meta WHERE key = ?1", params![key], |r| r.get(0))
        .optional()
        .map_err(map_sqlite_err)?;
    match raw {
        None => Ok(None),
        Some(text) => serde_json::from_str(&text)
            .map(Some)
            .map_err(|e| ApiError::internal(format!("corrupt meta json for {key}: {e}"))),
    }
}

pub fn meta_set(
    conn: &Connection,
    key: &str,
    value: &serde_json::Value,
    now: i64,
) -> Result<(), ApiError> {
    let encoded = serde_json::to_string(value).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO meta (key, value, updated_ts) VALUES (?1, ?2, ?3)
         ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_ts = excluded.updated_ts",
        params![key, encoded, now],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

pub fn append_event(
    conn: &Connection,
    kind: &str,
    payload: &serde_json::Value,
    now: i64,
) -> Result<i64, ApiError> {
    let encoded = serde_json::to_string(payload).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO events (kind, payload, ts) VALUES (?1, ?2, ?3)",
        params![kind, encoded, now],
    )
    .map_err(map_sqlite_err)?;
    Ok(conn.last_insert_rowid())
}

// ============================================================================
// Watchlist Repository
// ============================================================================

#[derive(Debug, Clone)]
pub struct WatchlistGroupRow {
    pub group_id: String,
    pub name: String,
    pub position: i64,
    pub kind: String,
    pub is_system: bool,
}

#[derive(Debug, Clone)]
pub struct WatchlistItemRow {
    pub group_id: String,
    pub instrument_id: String,
    pub position: i64,
}

#[derive(Debug, Clone)]
pub struct WatchlistRecords {
    pub groups: Vec<WatchlistGroupRow>,
    pub items: Vec<WatchlistItemRow>,
}

impl WatchlistRecords {
    pub fn items_for<'a>(&'a self, group_id: &'a str) -> impl Iterator<Item = &'a WatchlistItemRow> {
        self.items.iter().filter(move |item| item.group_id == group_id)
    }
}

pub fn watchlist_list(conn: &Connection) -> Result<WatchlistRecords, ApiError> {
    let mut g_stmt = conn
        .prepare(
            "SELECT group_id, name, position, kind, is_system
             FROM watchlist_groups
             ORDER BY is_system, position, group_id",
        )
        .map_err(map_sqlite_err)?;
    let g_rows = g_stmt
        .query_map([], |r| {
            Ok(WatchlistGroupRow {
                group_id: r.get(0)?,
                name: r.get(1)?,
                position: r.get(2)?,
                kind: r.get(3)?,
                is_system: r.get::<_, i64>(4)? != 0,
            })
        })
        .map_err(map_sqlite_err)?;
    let mut groups = Vec::new();
    for item in g_rows {
        groups.push(item.map_err(map_sqlite_err)?);
    }

    let mut i_stmt = conn
        .prepare(
            "SELECT group_id, instrument_id, position
             FROM watchlist_items
             ORDER BY group_id, position, instrument_id",
        )
        .map_err(map_sqlite_err)?;
    let i_rows = i_stmt
        .query_map([], |r| {
            Ok(WatchlistItemRow {
                group_id: r.get(0)?,
                instrument_id: r.get(1)?,
                position: r.get(2)?,
            })
        })
        .map_err(map_sqlite_err)?;
    let mut items = Vec::new();
    for item in i_rows {
        items.push(item.map_err(map_sqlite_err)?);
    }
    Ok(WatchlistRecords { groups, items })
}

pub fn watchlist_get_group(
    conn: &Connection,
    group_id: &str,
) -> Result<Option<WatchlistGroupRow>, ApiError> {
    conn.query_row(
        "SELECT group_id, name, position, kind, is_system
         FROM watchlist_groups WHERE group_id = ?1",
        params![group_id],
        |r| {
            Ok(WatchlistGroupRow {
                group_id: r.get(0)?,
                name: r.get(1)?,
                position: r.get(2)?,
                kind: r.get(3)?,
                is_system: r.get::<_, i64>(4)? != 0,
            })
        },
    )
    .optional()
    .map_err(map_sqlite_err)
}

fn require_manual_group(conn: &Connection, group_id: &str) -> Result<WatchlistGroupRow, ApiError> {
    let group = watchlist_get_group(conn, group_id)?
        .ok_or_else(|| ApiError::not_found(error_codes::NOT_FOUND, format!("分组不存在：{group_id}")))?;
    if group.is_system || group.kind == "SCANNER_FOCUS" {
        return Err(ApiError::validation("系统分组只读"));
    }
    Ok(group)
}

pub fn watchlist_create_group(conn: &mut Connection, name: &str, now: i64) -> Result<String, ApiError> {
    let clean = name.trim();
    if clean.is_empty() {
        return Err(ApiError::validation("分组名称不能为空"));
    }
    let group_id = Uuid::new_v4().simple().to_string();
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    let pos: i64 = tx
        .query_row(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_groups WHERE is_system = 0",
            [],
            |r| r.get(0),
        )
        .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO watchlist_groups
            (group_id, name, position, kind, is_system, created_ts, updated_ts)
         VALUES (?1, ?2, ?3, 'MANUAL', 0, ?4, ?5)",
        params![group_id, clean, pos, now, now],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(group_id)
}

pub fn watchlist_rename_group(
    conn: &mut Connection,
    group_id: &str,
    name: &str,
    now: i64,
) -> Result<(), ApiError> {
    let clean = name.trim();
    if clean.is_empty() {
        return Err(ApiError::validation("分组名称不能为空"));
    }
    require_manual_group(conn, group_id)?;
    let affected = conn
        .execute(
            "UPDATE watchlist_groups SET name = ?1, updated_ts = ?2 WHERE group_id = ?3",
            params![clean, now, group_id],
        )
        .map_err(map_sqlite_err)?;
    if affected == 0 {
        return Err(ApiError::not_found(
            error_codes::NOT_FOUND,
            format!("分组不存在：{group_id}"),
        ));
    }
    Ok(())
}

pub fn watchlist_delete_group(conn: &mut Connection, group_id: &str) -> Result<(), ApiError> {
    require_manual_group(conn, group_id)?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    let affected = tx
        .execute(
            "DELETE FROM watchlist_groups WHERE group_id = ?1",
            params![group_id],
        )
        .map_err(map_sqlite_err)?;
    if affected == 0 {
        return Err(ApiError::not_found(
            error_codes::NOT_FOUND,
            format!("分组不存在：{group_id}"),
        ));
    }
    tx.execute(
        "DELETE FROM watchlist_items WHERE group_id = ?1",
        params![group_id],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn watchlist_reorder_groups(
    conn: &mut Connection,
    ids: &[String],
    now: i64,
) -> Result<(), ApiError> {
    let records = watchlist_list(conn)?;
    let all_ids: Vec<String> = records.groups.iter().map(|g| g.group_id.clone()).collect();
    let unique: HashSet<&str> = ids.iter().map(String::as_str).collect();
    let expected: HashSet<&str> = all_ids.iter().map(String::as_str).collect();
    if ids.len() != unique.len() || unique != expected {
        return Err(ApiError::validation("排序必须覆盖全部分组且不能重复"));
    }
    let system_ids: HashSet<&str> = records
        .groups
        .iter()
        .filter(|g| g.is_system)
        .map(|g| g.group_id.as_str())
        .collect();
    for (idx, gid) in ids.iter().enumerate() {
        if system_ids.contains(gid.as_str()) && all_ids[idx] != *gid {
            return Err(ApiError::validation("系统分组不可调整顺序"));
        }
    }
    let manual_ids: Vec<&str> = ids
        .iter()
        .map(String::as_str)
        .filter(|id| !system_ids.contains(*id))
        .collect();

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    for (pos, gid) in manual_ids.into_iter().enumerate() {
        tx.execute(
            "UPDATE watchlist_groups SET position = ?1, updated_ts = ?2 WHERE group_id = ?3 AND is_system = 0",
            params![pos as i64, now, gid],
        )
        .map_err(map_sqlite_err)?;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn watchlist_add_item(
    conn: &mut Connection,
    group_id: &str,
    instrument_id: &str,
    now: i64,
) -> Result<(), ApiError> {
    require_manual_group(conn, group_id)?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    let pos: i64 = tx
        .query_row(
            "SELECT COALESCE(MAX(position) + 1, 0) FROM watchlist_items WHERE group_id = ?1",
            params![group_id],
            |r| r.get(0),
        )
        .map_err(map_sqlite_err)?;
    match tx.execute(
        "INSERT INTO watchlist_items (group_id, instrument_id, position, created_ts, updated_ts)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![group_id, instrument_id, pos, now, now],
    ) {
        Ok(_) => {
            tx.commit().map_err(map_sqlite_err)?;
            Ok(())
        }
        Err(rusqlite::Error::SqliteFailure(_, _)) => {
            Err(ApiError::validation("标的已在分组中"))
        }
        Err(err) => Err(map_sqlite_err(err)),
    }
}

pub fn watchlist_remove_item(conn: &mut Connection, instrument_id: &str) -> Result<(), ApiError> {
    let records = watchlist_list(conn)?;
    let manual_groups: HashSet<&str> = records
        .groups
        .iter()
        .filter(|g| !g.is_system)
        .map(|g| g.group_id.as_str())
        .collect();
    let removable: Vec<&str> = records
        .items
        .iter()
        .filter(|item| item.instrument_id == instrument_id && manual_groups.contains(item.group_id.as_str()))
        .map(|item| item.group_id.as_str())
        .collect();
    if removable.is_empty() {
        let in_system = records.items.iter().any(|item| {
            item.instrument_id == instrument_id && !manual_groups.contains(item.group_id.as_str())
        });
        if in_system {
            return Err(ApiError::validation("系统分组只读"));
        }
        return Err(ApiError::not_found(
            error_codes::NOT_FOUND,
            format!("标的未加入自选：{instrument_id}"),
        ));
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    for gid in removable {
        tx.execute(
            "DELETE FROM watchlist_items WHERE group_id = ?1 AND instrument_id = ?2",
            params![gid, instrument_id],
        )
        .map_err(map_sqlite_err)?;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn watchlist_reorder_items(
    conn: &mut Connection,
    group_id: &str,
    ids: &[String],
    now: i64,
) -> Result<(), ApiError> {
    let group = require_manual_group(conn, group_id)?;
    let records = watchlist_list(conn)?;
    let current: HashSet<&str> = records
        .items_for(&group.group_id)
        .map(|item| item.instrument_id.as_str())
        .collect();
    let wanted: HashSet<&str> = ids.iter().map(String::as_str).collect();
    if ids.len() != wanted.len() || wanted != current {
        return Err(ApiError::validation("排序必须覆盖分组内全部标的且不能重复"));
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    for (pos, instrument_id) in ids.iter().enumerate() {
        tx.execute(
            "UPDATE watchlist_items SET position = ?1, updated_ts = ?2
             WHERE group_id = ?3 AND instrument_id = ?4",
            params![pos as i64, now, group_id, instrument_id],
        )
        .map_err(map_sqlite_err)?;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn watchlist_upsert_system_group(
    conn: &mut Connection,
    group_id: &str,
    name: &str,
    instrument_ids: &[String],
    now: i64,
) -> Result<(), ApiError> {
    let clean = name.trim();
    if clean.is_empty() {
        return Err(ApiError::validation("分组名称不能为空"));
    }
    let mut seen = HashSet::new();
    let mut wanted = Vec::new();
    for id in instrument_ids {
        if seen.insert(id.clone()) {
            wanted.push(id.clone());
        }
    }
    let existing = watchlist_get_group(conn, group_id)?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    if existing.is_none() {
        let count: i64 = tx
            .query_row(
                "SELECT COUNT(*) FROM watchlist_groups WHERE is_system = 1",
                [],
                |r| r.get(0),
            )
            .map_err(map_sqlite_err)?;
        let pos = SYSTEM_GROUP_POSITION + count;
        tx.execute(
            "INSERT INTO watchlist_groups
                (group_id, name, position, kind, is_system, created_ts, updated_ts)
             VALUES (?1, ?2, ?3, 'SCANNER_FOCUS', 1, ?4, ?5)",
            params![group_id, clean, pos, now, now],
        )
        .map_err(map_sqlite_err)?;
    } else {
        tx.execute(
            "UPDATE watchlist_groups
             SET name = ?1, kind = 'SCANNER_FOCUS', is_system = 1, updated_ts = ?2
             WHERE group_id = ?3",
            params![clean, now, group_id],
        )
        .map_err(map_sqlite_err)?;
    }
    tx.execute(
        "DELETE FROM watchlist_items WHERE group_id = ?1",
        params![group_id],
    )
    .map_err(map_sqlite_err)?;
    for (pos, instrument_id) in wanted.iter().enumerate() {
        tx.execute(
            "INSERT INTO watchlist_items (group_id, instrument_id, position, created_ts, updated_ts)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![group_id, instrument_id, pos as i64, now, now],
        )
        .map_err(map_sqlite_err)?;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

// ============================================================================
// Drawings Repository
// ============================================================================

const DRAWING_COLUMNS: &str = "drawing_id, instrument_id, kind, visible_timeframes, points, text, style, version, created_ts, updated_ts, deleted_at";

fn drawing_from_row(row: &rusqlite::Row<'_>) -> Result<DrawingRecord, ApiError> {
    let kind_str: String = row.get(2).map_err(map_sqlite_err)?;
    let vt_str: String = row.get(3).map_err(map_sqlite_err)?;
    let pts_str: String = row.get(4).map_err(map_sqlite_err)?;
    let style_str: String = row.get(6).map_err(map_sqlite_err)?;

    let kind: DrawingKind = serde_json::from_value(serde_json::Value::String(kind_str))
        .map_err(|e| ApiError::internal(format!("invalid drawing kind in db: {e}")))?;
    let visible_timeframes: Vec<String> = serde_json::from_str(&vt_str)
        .map_err(|e| ApiError::internal(format!("invalid visible_timeframes json: {e}")))?;
    let points: Vec<DrawingPoint> = serde_json::from_str(&pts_str)
        .map_err(|e| ApiError::internal(format!("invalid points json: {e}")))?;
    let style: DrawingStyle = serde_json::from_str(&style_str)
        .map_err(|e| ApiError::internal(format!("invalid style json: {e}")))?;

    Ok(DrawingRecord {
        drawing_id: row.get(0).map_err(map_sqlite_err)?,
        instrument_id: row.get(1).map_err(map_sqlite_err)?,
        kind,
        visible_timeframes,
        points,
        text: row.get(5).map_err(map_sqlite_err)?,
        style,
        version: row.get::<_, i64>(7).map_err(map_sqlite_err)? as u64,
        created_ts: row.get(8).map_err(map_sqlite_err)?,
        updated_ts: row.get(9).map_err(map_sqlite_err)?,
        deleted_at: row.get(10).map_err(map_sqlite_err)?,
    })
}

fn write_drawing_latest(tx: &rusqlite::Transaction<'_>, record: &DrawingRecord) -> Result<(), ApiError> {
    let kind_val = serde_json::to_value(record.kind)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let kind_str = kind_val.as_str().unwrap_or("SEGMENT");
    let vt_str = serde_json::to_string(&record.visible_timeframes)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let pts_str = serde_json::to_string(&record.points)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let style_str = serde_json::to_string(&record.style)
        .map_err(|e| ApiError::internal(e.to_string()))?;

    tx.execute(
        &format!(
            "INSERT INTO drawings ({DRAWING_COLUMNS})
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)
             ON CONFLICT(drawing_id) DO UPDATE SET
                 instrument_id = excluded.instrument_id,
                 kind = excluded.kind,
                 visible_timeframes = excluded.visible_timeframes,
                 points = excluded.points,
                 text = excluded.text,
                 style = excluded.style,
                 version = excluded.version,
                 created_ts = excluded.created_ts,
                 updated_ts = excluded.updated_ts,
                 deleted_at = excluded.deleted_at"
        ),
        params![
            record.drawing_id,
            record.instrument_id,
            kind_str,
            vt_str,
            pts_str,
            record.text,
            style_str,
            record.version as i64,
            record.created_ts,
            record.updated_ts,
            record.deleted_at,
        ],
    )
    .map_err(map_sqlite_err)?;

    tx.execute(
        &format!(
            "INSERT INTO drawing_versions ({DRAWING_COLUMNS})
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)"
        ),
        params![
            record.drawing_id,
            record.version as i64,
            record.instrument_id,
            kind_str,
            vt_str,
            pts_str,
            record.text,
            style_str,
            record.created_ts,
            record.updated_ts,
            record.deleted_at,
        ],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

pub fn drawings_create(
    conn: &mut Connection,
    record: &DrawingRecord,
) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    write_drawing_latest(&tx, record)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn drawings_get(
    conn: &Connection,
    drawing_id: &str,
    version: Option<u64>,
) -> Result<DrawingRecord, ApiError> {
    let res = match version {
        None => conn.query_row(
            &format!("SELECT {DRAWING_COLUMNS} FROM drawings WHERE drawing_id = ?1"),
            params![drawing_id],
            |row| Ok(drawing_from_row(row)),
        ),
        Some(v) => conn.query_row(
            &format!(
                "SELECT {DRAWING_COLUMNS} FROM drawing_versions WHERE drawing_id = ?1 AND version = ?2"
            ),
            params![drawing_id, v as i64],
            |row| Ok(drawing_from_row(row)),
        ),
    };
    match res.optional().map_err(map_sqlite_err)? {
        Some(inner) => inner,
        None => Err(ApiError::not_found(
            error_codes::DRAWING_NOT_FOUND,
            format!("画线 {drawing_id} 不存在"),
        )),
    }
}

pub fn drawings_list_versions(
    conn: &Connection,
    drawing_id: &str,
) -> Result<Vec<DrawingRecord>, ApiError> {
    let mut stmt = conn
        .prepare(&format!(
            "SELECT {DRAWING_COLUMNS} FROM drawing_versions WHERE drawing_id = ?1 ORDER BY version"
        ))
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![drawing_id], |row| Ok(drawing_from_row(row)))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(map_sqlite_err)??);
    }
    if out.is_empty() {
        return Err(ApiError::not_found(
            error_codes::DRAWING_NOT_FOUND,
            format!("画线 {drawing_id} 不存在"),
        ));
    }
    Ok(out)
}

pub fn drawings_update(conn: &mut Connection, record: &DrawingRecord) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    write_drawing_latest(&tx, record)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn drawings_list(
    conn: &Connection,
    instrument_id: &str,
    timeframe: Option<&str>,
    from_ts: Option<i64>,
    to_ts: Option<i64>,
    price_min: Option<Decimal>,
    price_max: Option<Decimal>,
) -> Result<Vec<DrawingRecord>, ApiError> {
    let mut stmt = conn
        .prepare(&format!(
            "SELECT {DRAWING_COLUMNS} FROM drawings
             WHERE instrument_id = ?1 AND deleted_at IS NULL
             ORDER BY created_ts, drawing_id"
        ))
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![instrument_id], |row| Ok(drawing_from_row(row)))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for r in rows {
        let rec = r.map_err(map_sqlite_err)??;
        if drawing_visible_in_window(&rec, timeframe, from_ts, to_ts, price_min, price_max) {
            out.push(rec);
        }
    }
    Ok(out)
}

pub fn drawings_clear(
    conn: &mut Connection,
    instrument_id: &str,
    now: i64,
) -> Result<u64, ApiError> {
    let active = drawings_list(conn, instrument_id, None, None, None, None, None)?;
    let count = active.len() as u64;
    if count == 0 {
        return Ok(0);
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    for mut rec in active {
        rec.version += 1;
        rec.updated_ts = now;
        rec.deleted_at = Some(now);
        write_drawing_latest(&tx, &rec)?;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(count)
}

pub fn drawing_visible_in_window(
    record: &DrawingRecord,
    timeframe: Option<&str>,
    from_ts: Option<i64>,
    to_ts: Option<i64>,
    price_min: Option<Decimal>,
    price_max: Option<Decimal>,
) -> bool {
    if let Some(tf) = timeframe
        && !record.visible_timeframes.is_empty()
        && !record.visible_timeframes.iter().any(|item| item == tf)
    {
        return false;
    }
    if !visible_in_time(record.kind, &record.points, from_ts, to_ts) {
        return false;
    }
    visible_in_price(&record.points, price_min, price_max)
}

fn time_extent(kind: DrawingKind, points: &[DrawingPoint]) -> (Option<i64>, Option<i64>) {
    if points.is_empty() {
        return (None, None);
    }
    match kind {
        DrawingKind::HorizontalLine | DrawingKind::TextLabel => (None, None),
        DrawingKind::Ray => {
            if points.len() >= 2 && points[1].ts >= points[0].ts {
                (Some(points[0].ts), None)
            } else {
                (None, Some(points[0].ts))
            }
        }
        _ => {
            let min_ts = points.iter().map(|p| p.ts).min();
            let max_ts = points.iter().map(|p| p.ts).max();
            (min_ts, max_ts)
        }
    }
}

fn visible_in_time(
    kind: DrawingKind,
    points: &[DrawingPoint],
    from_ts: Option<i64>,
    to_ts: Option<i64>,
) -> bool {
    if from_ts.is_none() && to_ts.is_none() {
        return true;
    }
    let (start, end) = time_extent(kind, points);
    if let (Some(e), Some(from)) = (end, from_ts)
        && e < from
    {
        return false;
    }
    if let (Some(s), Some(to)) = (start, to_ts)
        && s > to
    {
        return false;
    }
    true
}

fn visible_in_price(
    points: &[DrawingPoint],
    price_min: Option<Decimal>,
    price_max: Option<Decimal>,
) -> bool {
    if price_min.is_none() && price_max.is_none() {
        return true;
    }
    let Some(low) = points.iter().map(|p| p.price).min() else {
        return true;
    };
    let Some(high) = points.iter().map(|p| p.price).max() else {
        return true;
    };
    if let Some(min_p) = price_min
        && high < min_p
    {
        return false;
    }
    if let Some(max_p) = price_max
        && low > max_p
    {
        return false;
    }
    true
}

// ============================================================================
// Alerts Repository
// ============================================================================

pub fn alerts_write(conn: &Connection, record: &AlertRecord) -> Result<(), ApiError> {
    let status_val = serde_json::to_value(record.status)
        .map_err(|e| ApiError::internal(e.to_string()))?;
    let status_str = status_val.as_str().unwrap_or("ARMED");
    let payload = serde_json::to_string(record).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO alerts
            (alert_id, instrument_id, timeframe, status, repeat_flag, next_arm_ts,
             created_ts, updated_ts, deleted_at, payload)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, NULL, ?9)
         ON CONFLICT(alert_id) DO UPDATE SET
             instrument_id = excluded.instrument_id,
             timeframe = excluded.timeframe,
             status = excluded.status,
             repeat_flag = excluded.repeat_flag,
             next_arm_ts = excluded.next_arm_ts,
             created_ts = excluded.created_ts,
             updated_ts = excluded.updated_ts,
             deleted_at = COALESCE(excluded.deleted_at, alerts.deleted_at),
             payload = excluded.payload",
        params![
            record.alert_id,
            record.instrument_id,
            record.timeframe,
            status_str,
            if record.repeat { 1i64 } else { 0i64 },
            record.next_arm_ts,
            record.created_ts,
            record.updated_ts,
            payload,
        ],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

pub fn alerts_get(conn: &Connection, alert_id: &str) -> Result<AlertRecord, ApiError> {
    let row: Option<(Option<i64>, String)> = conn
        .query_row(
            "SELECT deleted_at, payload FROM alerts WHERE alert_id = ?1",
            params![alert_id],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    let Some((deleted_at, payload)) = row else {
        return Err(ApiError::not_found(
            error_codes::ALERT_NOT_FOUND,
            format!("预警 {alert_id} 不存在"),
        ));
    };
    if deleted_at.is_some() {
        return Err(ApiError::not_found(
            error_codes::ALERT_NOT_FOUND,
            format!("预警 {alert_id} 不存在"),
        ));
    }
    serde_json::from_str(&payload)
        .map_err(|e| ApiError::internal(format!("corrupt alert payload {alert_id}: {e}")))
}

pub fn alerts_list(
    conn: &Connection,
    status: Option<AlertStatus>,
) -> Result<Vec<AlertRecord>, ApiError> {
    let rows = match status {
        None => {
            let mut stmt = conn
                .prepare(
                    "SELECT payload FROM alerts WHERE deleted_at IS NULL
                     ORDER BY created_ts DESC, alert_id DESC",
                )
                .map_err(map_sqlite_err)?;
            let mapped = stmt
                .query_map([], |r| r.get::<_, String>(0))
                .map_err(map_sqlite_err)?;
            mapped.collect::<Result<Vec<_>, _>>().map_err(map_sqlite_err)?
        }
        Some(st) => {
            let st_val = serde_json::to_value(st).map_err(|e| ApiError::internal(e.to_string()))?;
            let st_str = st_val.as_str().unwrap_or("ARMED");
            let mut stmt = conn
                .prepare(
                    "SELECT payload FROM alerts WHERE deleted_at IS NULL AND status = ?1
                     ORDER BY created_ts DESC, alert_id DESC",
                )
                .map_err(map_sqlite_err)?;
            let mapped = stmt
                .query_map(params![st_str], |r| r.get::<_, String>(0))
                .map_err(map_sqlite_err)?;
            mapped.collect::<Result<Vec<_>, _>>().map_err(map_sqlite_err)?
        }
    };
    let mut out = Vec::with_capacity(rows.len());
    for raw in rows {
        let rec: AlertRecord = serde_json::from_str(&raw)
            .map_err(|e| ApiError::internal(format!("corrupt alert payload: {e}")))?;
        out.push(rec);
    }
    Ok(out)
}

pub fn alerts_soft_delete(
    conn: &Connection,
    alert_id: &str,
    now: i64,
) -> Result<AlertRecord, ApiError> {
    let rec = alerts_get(conn, alert_id)?;
    conn.execute(
        "UPDATE alerts SET deleted_at = ?1, updated_ts = ?2 WHERE alert_id = ?3",
        params![now, now, alert_id],
    )
    .map_err(map_sqlite_err)?;
    Ok(rec)
}

pub fn alerts_append_trigger(
    conn: &Connection,
    trigger: &AlertTriggerRecord,
    now: i64,
) -> Result<(), ApiError> {
    let payload = serde_json::to_string(trigger).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO alert_triggers
            (trigger_id, alert_id, instrument_id, timeframe, triggered_ts, snapshot_id, payload, created_ts)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![
            trigger.trigger_id,
            trigger.alert_id,
            trigger.instrument_id,
            trigger.timeframe,
            trigger.triggered_ts,
            trigger.snapshot_id,
            payload,
            now,
        ],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

pub fn alerts_list_triggers(
    conn: &Connection,
    alert_id: Option<&str>,
    instrument_id: Option<&str>,
    limit: usize,
) -> Result<Vec<AlertTriggerRecord>, ApiError> {
    let capped = limit.clamp(1, 500) as i64;
    let mut clauses = Vec::new();
    let mut args: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(aid) = alert_id {
        clauses.push(format!("alert_id = ?{}", args.len() + 1));
        args.push(aid.to_owned().into());
    }
    if let Some(iid) = instrument_id {
        clauses.push(format!("instrument_id = ?{}", args.len() + 1));
        args.push(iid.to_owned().into());
    }
    let where_clause = if clauses.is_empty() {
        String::new()
    } else {
        format!("WHERE {}", clauses.join(" AND "))
    };
    args.push(capped.into());
    let sql = format!(
        "SELECT payload FROM alert_triggers {where_clause}
         ORDER BY triggered_ts DESC, trigger_id DESC
         LIMIT ?{}",
        args.len()
    );
    let mut stmt = conn.prepare(&sql).map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(args), |r| r.get::<_, String>(0))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for raw in rows {
        let raw_str = raw.map_err(map_sqlite_err)?;
        let rec: AlertTriggerRecord = serde_json::from_str(&raw_str)
            .map_err(|e| ApiError::internal(format!("corrupt trigger payload: {e}")))?;
        out.push(rec);
    }
    Ok(out)
}

// ============================================================================
// KeyLevel Feedback Repository
// ============================================================================

pub fn key_level_list_ignores(
    conn: &Connection,
    instrument_id: &str,
) -> Result<Vec<KeyLevelIgnoreRecord>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT instrument_id, level_id, ignored, updated_ts
             FROM key_level_ignores
             WHERE instrument_id = ?1
             ORDER BY updated_ts DESC, level_id",
        )
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![instrument_id], |r| {
            Ok(KeyLevelIgnoreRecord {
                instrument_id: r.get(0)?,
                level_id: r.get(1)?,
                ignored: r.get::<_, i64>(2)? != 0,
                updated_ts: r.get(3)?,
            })
        })
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(map_sqlite_err)?);
    }
    Ok(out)
}

pub fn key_level_set_ignore(
    conn: &mut Connection,
    instrument_id: &str,
    level_id: &str,
    ignored: bool,
    now: i64,
) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO key_level_ignores (instrument_id, level_id, ignored, updated_ts)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(instrument_id, level_id) DO UPDATE SET
             ignored = excluded.ignored,
             updated_ts = excluded.updated_ts",
        params![instrument_id, level_id, if ignored { 1i64 } else { 0i64 }, now],
    )
    .map_err(map_sqlite_err)?;
    append_event(
        &tx,
        "key_level.ignore",
        &serde_json::json!({
            "instrument_id": instrument_id,
            "level_id": level_id,
            "ignored": ignored,
        }),
        now,
    )?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn key_level_get_adoption(
    conn: &Connection,
    instrument_id: &str,
    level_id: &str,
) -> Result<Option<KeyLevelAdoptionRecord>, ApiError> {
    conn.query_row(
        "SELECT instrument_id, level_id, snapshot_id, drawing_id, adopted_ts, timeframe
         FROM key_level_adoptions WHERE instrument_id = ?1 AND level_id = ?2",
        params![instrument_id, level_id],
        |r| {
            Ok(KeyLevelAdoptionRecord {
                instrument_id: r.get(0)?,
                level_id: r.get(1)?,
                snapshot_id: r.get(2)?,
                drawing_id: r.get(3)?,
                adopted_ts: r.get(4)?,
                timeframe: r.get(5)?,
            })
        },
    )
    .optional()
    .map_err(map_sqlite_err)
}

pub fn key_level_list_adoptions(
    conn: &Connection,
    instrument_id: &str,
) -> Result<Vec<KeyLevelAdoptionRecord>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT instrument_id, level_id, snapshot_id, drawing_id, adopted_ts, timeframe
             FROM key_level_adoptions
             WHERE instrument_id = ?1
             ORDER BY adopted_ts DESC, level_id",
        )
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![instrument_id], |r| {
            Ok(KeyLevelAdoptionRecord {
                instrument_id: r.get(0)?,
                level_id: r.get(1)?,
                snapshot_id: r.get(2)?,
                drawing_id: r.get(3)?,
                adopted_ts: r.get(4)?,
                timeframe: r.get(5)?,
            })
        })
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for r in rows {
        out.push(r.map_err(map_sqlite_err)?);
    }
    Ok(out)
}

pub fn key_level_record_adoption(
    conn: &mut Connection,
    rec: &KeyLevelAdoptionRecord,
) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT INTO key_level_adoptions
            (instrument_id, level_id, snapshot_id, drawing_id, adopted_ts, timeframe)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(instrument_id, level_id) DO UPDATE SET
             snapshot_id = excluded.snapshot_id,
             drawing_id = excluded.drawing_id,
             adopted_ts = excluded.adopted_ts,
             timeframe = excluded.timeframe",
        params![
            rec.instrument_id,
            rec.level_id,
            rec.snapshot_id,
            rec.drawing_id,
            rec.adopted_ts,
            rec.timeframe,
        ],
    )
    .map_err(map_sqlite_err)?;
    append_event(
        &tx,
        "key_level.adopt",
        &serde_json::json!({
            "instrument_id": rec.instrument_id,
            "level_id": rec.level_id,
            "snapshot_id": rec.snapshot_id,
            "drawing_id": rec.drawing_id,
            "timeframe": rec.timeframe,
        }),
        rec.adopted_ts,
    )?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

// ============================================================================
// Annotations Repository
// ============================================================================

fn write_annotation_row(
    conn: &Connection,
    record: &AgentAnnotationRecord,
) -> Result<(), ApiError> {
    let kind_val = serde_json::to_value(record.kind).map_err(|e| ApiError::internal(e.to_string()))?;
    let status_val = serde_json::to_value(record.status).map_err(|e| ApiError::internal(e.to_string()))?;
    let payload = serde_json::to_string(record).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO agent_annotations (
            annotation_id, instrument_id, snapshot_id, timeframe, session_id,
            round, kind, status, ts_anchor, created_ts, updated_ts,
            adopted_drawing_id, archived_reason, payload
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)
        ON CONFLICT(annotation_id) DO UPDATE SET
            instrument_id = excluded.instrument_id,
            snapshot_id = excluded.snapshot_id,
            timeframe = excluded.timeframe,
            session_id = excluded.session_id,
            round = excluded.round,
            kind = excluded.kind,
            status = excluded.status,
            ts_anchor = excluded.ts_anchor,
            created_ts = excluded.created_ts,
            updated_ts = excluded.updated_ts,
            adopted_drawing_id = excluded.adopted_drawing_id,
            archived_reason = excluded.archived_reason,
            payload = excluded.payload",
        params![
            record.annotation_id,
            record.instrument_id,
            record.snapshot_id,
            record.timeframe,
            record.session_id,
            record.round as i64,
            kind_val.as_str().unwrap_or("LEVEL"),
            status_val.as_str().unwrap_or("ACTIVE"),
            record.ts_anchor,
            record.created_ts,
            record.updated_ts,
            record.adopted_drawing_id,
            record.archived_reason,
            payload,
        ],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

pub fn annotations_insert(
    conn: &mut Connection,
    record: &AgentAnnotationRecord,
    limit: usize,
) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    let existing: Vec<String> = {
        let mut stmt = tx
            .prepare(
                "SELECT payload FROM agent_annotations
                 WHERE instrument_id = ?1 AND status = 'ACTIVE'
                 ORDER BY round, created_ts, annotation_id",
            )
            .map_err(map_sqlite_err)?;
        let rows = stmt
            .query_map(params![record.instrument_id], |r| r.get::<_, String>(0))
            .map_err(map_sqlite_err)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(map_sqlite_err)?
    };
    let overflow = (existing.len() + 1).saturating_sub(limit);
    for raw in existing.into_iter().take(overflow) {
        let mut current: AgentAnnotationRecord = serde_json::from_str(&raw)
            .map_err(|e| ApiError::internal(format!("corrupt annotation payload: {e}")))?;
        current.status = AnnotationStatus::Archived;
        current.archived_reason = Some(format!("超过单标的 {limit} 条上限，按最旧轮次归档"));
        current.updated_ts = record.created_ts;
        write_annotation_row(&tx, &current)?;
    }
    write_annotation_row(&tx, record)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn annotations_get(
    conn: &Connection,
    annotation_id: &str,
) -> Result<AgentAnnotationRecord, ApiError> {
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload FROM agent_annotations WHERE annotation_id = ?1",
            params![annotation_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    let Some(raw) = raw else {
        return Err(ApiError::not_found(
            error_codes::ANNOTATION_NOT_FOUND,
            format!("标注 {annotation_id} 不存在"),
        ));
    };
    serde_json::from_str(&raw)
        .map_err(|e| ApiError::internal(format!("corrupt annotation payload: {e}")))
}

pub fn annotations_update(
    conn: &mut Connection,
    record: &AgentAnnotationRecord,
) -> Result<(), ApiError> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    write_annotation_row(&tx, record)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn annotations_list(
    conn: &Connection,
    instrument_id: Option<&str>,
    snapshot_id: Option<&str>,
    session_id: Option<&str>,
    include_archived: bool,
) -> Result<Vec<AgentAnnotationRecord>, ApiError> {
    let mut clauses = vec!["1 = 1".to_owned()];
    let mut args: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(iid) = instrument_id {
        clauses.push(format!("instrument_id = ?{}", args.len() + 1));
        args.push(iid.to_owned().into());
    }
    if let Some(sid) = snapshot_id {
        clauses.push(format!("snapshot_id = ?{}", args.len() + 1));
        args.push(sid.to_owned().into());
    }
    if let Some(sess) = session_id {
        clauses.push(format!("session_id = ?{}", args.len() + 1));
        args.push(sess.to_owned().into());
    }
    if !include_archived {
        clauses.push(format!("status != ?{}", args.len() + 1));
        args.push("ARCHIVED".to_owned().into());
    }
    let sql = format!(
        "SELECT payload FROM agent_annotations WHERE {} ORDER BY created_ts DESC, annotation_id DESC",
        clauses.join(" AND ")
    );
    let mut stmt = conn.prepare(&sql).map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(args), |r| r.get::<_, String>(0))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for raw in rows {
        let s = raw.map_err(map_sqlite_err)?;
        let rec: AgentAnnotationRecord = serde_json::from_str(&s)
            .map_err(|e| ApiError::internal(format!("corrupt annotation payload: {e}")))?;
        out.push(rec);
    }
    Ok(out)
}

pub fn annotations_list_sessions(
    conn: &Connection,
) -> Result<Vec<AnnotationSessionSummary>, ApiError> {
    let all = annotations_list(conn, None, None, None, true)?;
    let mut grouped: HashMap<String, (Vec<String>, usize, i64)> = HashMap::new();
    for rec in all {
        let entry = grouped.entry(rec.session_id).or_insert((Vec::new(), 0, 0));
        if !entry.0.contains(&rec.instrument_id) {
            entry.0.push(rec.instrument_id);
        }
        entry.1 += 1;
        entry.2 = entry.2.max(rec.created_ts);
    }
    let mut out: Vec<AnnotationSessionSummary> = grouped
        .into_iter()
        .map(|(session_id, (mut instrument_ids, count, latest_ts))| {
            instrument_ids.sort();
            AnnotationSessionSummary {
                session_id,
                instrument_ids,
                annotation_count: count,
                latest_ts,
            }
        })
        .collect();
    out.sort_by(|a, b| b.latest_ts.cmp(&a.latest_ts).then_with(|| a.session_id.cmp(&b.session_id)));
    Ok(out)
}

pub fn annotations_get_adoption(
    conn: &Connection,
    annotation_id: &str,
) -> Result<Option<AnnotationAdoptionRecord>, ApiError> {
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload FROM annotation_adoptions WHERE annotation_id = ?1",
            params![annotation_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    match raw {
        None => Ok(None),
        Some(s) => serde_json::from_str(&s)
            .map(Some)
            .map_err(|e| ApiError::internal(format!("corrupt annotation adoption: {e}"))),
    }
}

pub fn annotations_record_adoption(
    conn: &mut Connection,
    adoption: &AnnotationAdoptionRecord,
) -> Result<(), ApiError> {
    let payload = serde_json::to_string(adoption).map_err(|e| ApiError::internal(e.to_string()))?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    tx.execute(
        "INSERT OR REPLACE INTO annotation_adoptions (
            annotation_id, instrument_id, snapshot_id, timeframe,
            session_id, round, drawing_id, adopted_ts, payload
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            adoption.annotation_id,
            adoption.instrument_id,
            adoption.snapshot_id,
            adoption.timeframe,
            adoption.session_id,
            adoption.round as i64,
            adoption.drawing_id,
            adoption.adopted_ts,
            payload,
        ],
    )
    .map_err(map_sqlite_err)?;
    tx.commit().map_err(map_sqlite_err)?;
    Ok(())
}

pub fn annotations_clear(
    conn: &mut Connection,
    instrument_id: &str,
    session_id: Option<&str>,
    now: i64,
) -> Result<u64, ApiError> {
    let active = annotations_list(conn, Some(instrument_id), None, session_id, false)?;
    let mut cleared = 0u64;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(map_sqlite_err)?;
    for mut rec in active {
        if rec.status != AnnotationStatus::Active {
            continue;
        }
        rec.status = AnnotationStatus::Archived;
        rec.archived_reason = Some("已手动清理".to_owned());
        rec.updated_ts = now;
        write_annotation_row(&tx, &rec)?;
        cleared += 1;
    }
    tx.commit().map_err(map_sqlite_err)?;
    Ok(cleared)
}

// ============================================================================
// Scanner Briefings Repository
// ============================================================================

pub fn briefings_list(
    conn: &Connection,
    limit: usize,
    unread_only: bool,
) -> Result<(Vec<ScannerBriefing>, usize), ApiError> {
    let unread_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM scanner_briefings WHERE read_ts IS NULL",
            [],
            |r| r.get(0),
        )
        .map_err(map_sqlite_err)?;
    let sql = if unread_only {
        "SELECT payload FROM scanner_briefings WHERE read_ts IS NULL
         ORDER BY created_ts DESC, briefing_id DESC LIMIT ?1"
    } else {
        "SELECT payload FROM scanner_briefings
         ORDER BY created_ts DESC, briefing_id DESC LIMIT ?1"
    };
    let mut stmt = conn.prepare(sql).map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![limit.clamp(1, 200) as i64], |r| r.get::<_, String>(0))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for raw in rows {
        let s = raw.map_err(map_sqlite_err)?;
        let item: ScannerBriefing = serde_json::from_str(&s)
            .map_err(|e| ApiError::internal(format!("corrupt briefing payload: {e}")))?;
        out.push(item);
    }
    Ok((out, unread_count.max(0) as usize))
}

pub fn briefings_mark_read(
    conn: &mut Connection,
    briefing_id: &str,
    now: i64,
) -> Result<ScannerBriefing, ApiError> {
    let raw: Option<String> = conn
        .query_row(
            "SELECT payload FROM scanner_briefings WHERE briefing_id = ?1",
            params![briefing_id],
            |r| r.get(0),
        )
        .optional()
        .map_err(map_sqlite_err)?;
    let Some(raw) = raw else {
        return Err(ApiError::not_found(
            error_codes::BRIEFING_NOT_FOUND,
            format!("简报 {briefing_id} 不存在"),
        ));
    };
    let mut briefing: ScannerBriefing = serde_json::from_str(&raw)
        .map_err(|e| ApiError::internal(format!("corrupt briefing payload: {e}")))?;
    if briefing.read_ts.is_none() {
        briefing.read_ts = Some(now);
        let updated = serde_json::to_string(&briefing).map_err(|e| ApiError::internal(e.to_string()))?;
        conn.execute(
            "UPDATE scanner_briefings SET read_ts = ?1, payload = ?2 WHERE briefing_id = ?3",
            params![now, updated, briefing_id],
        )
        .map_err(map_sqlite_err)?;
    }
    Ok(briefing)
}

pub fn custom_instruments_list(
    conn: &Connection,
    pack_name: &str,
) -> Result<Vec<talaria_contracts::InstrumentSpec>, ApiError> {
    let mut stmt = conn
        .prepare(
            "SELECT spec_json FROM custom_instruments WHERE pack_name = ?1 ORDER BY created_ts ASC, instrument_id ASC",
        )
        .map_err(map_sqlite_err)?;
    let rows = stmt
        .query_map(params![pack_name], |r| r.get::<_, String>(0))
        .map_err(map_sqlite_err)?;
    let mut out = Vec::new();
    for row in rows {
        let raw = row.map_err(map_sqlite_err)?;
        if let Ok(spec) = serde_json::from_str::<talaria_contracts::InstrumentSpec>(&raw) {
            out.push(spec);
        }
    }
    Ok(out)
}

pub fn custom_instruments_upsert(
    conn: &mut Connection,
    pack_name: &str,
    spec: &talaria_contracts::InstrumentSpec,
    now: i64,
) -> Result<(), ApiError> {
    let payload =
        serde_json::to_string(spec).map_err(|e| ApiError::internal(e.to_string()))?;
    conn.execute(
        "INSERT INTO custom_instruments (pack_name, instrument_id, spec_json, created_ts)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(pack_name, instrument_id) DO UPDATE SET
            spec_json = excluded.spec_json",
        params![pack_name, &spec.instrument_id, payload, now],
    )
    .map_err(map_sqlite_err)?;
    Ok(())
}

/// Indicator defaults key matching Python `indicator_defaults.py`.
pub const INDICATOR_DEFAULTS_META_KEY: &str = "indicator_defaults.v1";

pub fn load_indicator_defaults_overrides(
    conn: &Connection,
) -> Result<BTreeMap<talaria_contracts::IndicatorName, talaria_contracts::IndicatorParams>, ApiError> {
    let Some(val) = meta_get(conn, INDICATOR_DEFAULTS_META_KEY)? else {
        return Ok(BTreeMap::new());
    };
    let Some(obj) = val.as_object() else {
        return Ok(BTreeMap::new());
    };
    let mut out = BTreeMap::new();
    for (k, v) in obj {
        if let (Ok(name), Ok(params)) = (
            serde_json::from_value::<talaria_contracts::IndicatorName>(serde_json::Value::String(k.clone())),
            serde_json::from_value::<talaria_contracts::IndicatorParams>(v.clone()),
        ) {
            out.insert(name, params);
        }
    }
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn migrates_legacy_eleven_column_python_scanner_briefings() {
        let dir = tempfile::tempdir().expect("tempdir");
        let state_dir = dir.path().join("state");
        fs::create_dir_all(&state_dir).expect("state dir");
        let legacy_path = dir.path().join(LEGACY_COLLAB_DB);

        {
            let conn = Connection::open(&legacy_path).expect("open legacy db");
            conn.execute_batch(
                "CREATE TABLE scanner_briefings (
                    id TEXT PRIMARY KEY,
                    trigger_id TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    instrument TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT,
                    evidence_refs TEXT NOT NULL,
                    abstain_reason TEXT,
                    created_ts INTEGER NOT NULL,
                    read_ts INTEGER
                );",
            )
            .expect("create legacy table");
            let summary = r#"{"trigger_reason":"volume spike","key_levels":[{"label":"POC","price":"62668.79","note":"pivot"}],"risks":["thin sample"]}"#;
            let evidence = r#"[{"evidence_id":"snapshot:snap1","kind":"SNAPSHOT","label":"snap","detail":"details","price_low":null,"price_high":null}]"#;
            conn.execute(
                "INSERT INTO scanner_briefings (id, trigger_id, snapshot_id, instrument, timeframe, status, summary, evidence_refs, abstain_reason, created_ts, read_ts)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, NULL, ?9, NULL)",
                params![
                    "brf_legacy_1",
                    "scn_1",
                    "snap1",
                    "BTC-USDT-PERP",
                    "1m",
                    "READY",
                    summary,
                    evidence,
                    1_700_000_000_000_i64,
                ],
            )
            .expect("insert legacy briefing");
        }

        let now_fn: NowMsFn = Arc::new(|| 1_700_000_000_500);
        let store = WorkbenchStateStore::open(dir.path(), now_fn).expect("open workbench store");
        let (items, unread) = store
            .read_sync(|conn| briefings_list(conn, 50, false))
            .expect("list briefings");
        assert_eq!(unread, 1);
        assert_eq!(items.len(), 1);
        assert_eq!(items[0].id, "brf_legacy_1");
        assert_eq!(items[0].instrument_id, "BTC-USDT-PERP");
        assert_eq!(items[0].status, BriefingStatus::Ready);
        assert_eq!(
            items[0].summary.as_ref().expect("summary").trigger_reason,
            "volume spike"
        );
        assert_eq!(items[0].evidence_refs.len(), 1);
        store.shutdown().await.expect("shutdown store");
    }
}
