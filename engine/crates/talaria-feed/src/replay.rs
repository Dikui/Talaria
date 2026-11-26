//! Deterministic JSONL replay transport (`ReplayTransport`) and production data-dir safety guard.

use std::collections::BTreeSet;
use std::fs::{self, File};
use std::io::{BufRead, BufReader};
use std::path::{Component, Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use serde_json::Value;
use talaria_contracts::{DerivativeTickEvent, KlineEvent, OrderBookEvent, TransportEvent};
use talaria_packs::{BoxFuture, FeedTransport, PackError, TransportFrame};
use tokio::sync::Mutex as AsyncMutex;

use crate::clock::{FeedClock, SystemClock};
use crate::error::FeedError;

/// Default relative production data directory guarded against replay writes.
pub const DEFAULT_DATA_DIR: &str = ".talaria";
/// Default idle heartbeat interval in milliseconds emitted after the replay script ends.
pub const DEFAULT_IDLE_HEARTBEAT_MS: u64 = 5_000;

/// Kind of event line inside a deterministic JSONL replay script.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ReplayLineKind {
    Kline,
    Book,
    Derivative,
    Heartbeat,
    Disconnect,
    Reconnect,
}

/// Parsed and validated line of a JSONL replay file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplayLine {
    pub offset_ms: i64,
    pub kind: ReplayLineKind,
    pub kline: Option<KlineEvent>,
    pub book: Option<OrderBookEvent>,
    pub derivative: Option<DerivativeTickEvent>,
    pub reason: String,
}

fn parse_replay_line(raw: Value, line_number: usize) -> Result<ReplayLine, FeedError> {
    let obj = raw.as_object().ok_or_else(|| {
        FeedError::InvalidRequest(format!("replay line {line_number} is not a JSON object"))
    })?;

    let offset_ms = obj
        .get("offset_ms")
        .and_then(Value::as_i64)
        .filter(|ms| *ms >= 0)
        .ok_or_else(|| {
            FeedError::InvalidRequest(format!(
                "replay line {line_number} is missing a valid non-negative integer `offset_ms`"
            ))
        })?;

    let kind_str = obj
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| {
            FeedError::InvalidRequest(format!("replay line {line_number} is missing `type`"))
        })?;

    let mut kline = None;
    let mut book = None;
    let mut derivative = None;
    let kind = match kind_str {
        "kline" => {
            let event_val = obj.get("event").cloned().ok_or_else(|| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} `kline` is missing `event`"
                ))
            })?;
            let parsed: KlineEvent = serde_json::from_value(event_val).map_err(|err| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} invalid KlineEvent: {err}"
                ))
            })?;
            kline = Some(parsed);
            ReplayLineKind::Kline
        }
        "book" => {
            let event_val = obj.get("event").cloned().ok_or_else(|| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} `book` is missing `event`"
                ))
            })?;
            let parsed: OrderBookEvent = serde_json::from_value(event_val).map_err(|err| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} invalid OrderBookEvent: {err}"
                ))
            })?;
            book = Some(parsed);
            ReplayLineKind::Book
        }
        "derivative" => {
            let event_val = obj.get("event").cloned().ok_or_else(|| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} `derivative` is missing `event`"
                ))
            })?;
            let parsed: DerivativeTickEvent = serde_json::from_value(event_val).map_err(|err| {
                FeedError::InvalidRequest(format!(
                    "replay line {line_number} invalid DerivativeTickEvent: {err}"
                ))
            })?;
            derivative = Some(parsed);
            ReplayLineKind::Derivative
        }
        "heartbeat" => ReplayLineKind::Heartbeat,
        "disconnect" => ReplayLineKind::Disconnect,
        "reconnect" => ReplayLineKind::Reconnect,
        other => {
            return Err(FeedError::InvalidRequest(format!(
                "replay line {line_number} has invalid type `{other}`"
            )));
        }
    };

    let reason = obj
        .get("reason")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_owned();

    Ok(ReplayLine {
        offset_ms,
        kind,
        kline,
        book,
        derivative,
        reason,
    })
}

/// Read and strictly validate a deterministic JSONL replay script (offsets must be non-decreasing).
pub fn read_replay_file(path: impl AsRef<Path>) -> Result<Vec<ReplayLine>, FeedError> {
    let path = path.as_ref();
    let file = File::open(path)?;
    let reader = BufReader::new(file);
    let mut lines = Vec::new();
    let mut previous = -1i64;

    for (idx, line_res) in reader.lines().enumerate() {
        let line_number = idx + 1;
        let text = line_res?;
        let trimmed = text.trim();
        if trimmed.is_empty() {
            continue;
        }
        let raw: Value = serde_json::from_str(trimmed).map_err(|err| {
            FeedError::InvalidRequest(format!(
                "replay line {line_number} is not valid JSON: {err}"
            ))
        })?;
        let item = parse_replay_line(raw, line_number)?;
        if item.offset_ms < previous {
            return Err(FeedError::InvalidRequest(format!(
                "replay line {line_number} has out-of-order offset_ms ({} < {previous})",
                item.offset_ms
            )));
        }
        previous = item.offset_ms;
        lines.push(item);
    }

    if lines.is_empty() {
        return Err(FeedError::InvalidRequest(
            "replay file is empty".to_owned(),
        ));
    }
    Ok(lines)
}

/// Normalize a path against `cwd`, resolving symlinks for the longest existing prefix so
/// non-existent nested paths under symlinked or case-insensitive directories still resolve cleanly.
fn normalize_resolving_prefix(path: &Path) -> Result<PathBuf, FeedError> {
    let abs = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()?.join(path)
    };

    // Collapse `.` and `..` lexically first before checking existing prefixes.
    let mut lexical = PathBuf::new();
    for comp in abs.components() {
        match comp {
            Component::CurDir => {}
            Component::ParentDir => {
                lexical.pop();
            }
            other => lexical.push(other.as_os_str()),
        }
    }

    let mut tail: Vec<std::ffi::OsString> = Vec::new();
    let mut probe = lexical.as_path();
    loop {
        if let Ok(canon) = fs::canonicalize(probe) {
            let mut out = canon;
            for seg in tail.iter().rev() {
                out.push(seg);
            }
            return Ok(out);
        }
        let Some(name) = probe.file_name() else {
            return Ok(lexical);
        };
        tail.push(name.to_os_string());
        match probe.parent() {
            Some(parent) => probe = parent,
            None => return Ok(lexical),
        }
    }
}

fn casefold_path(path: &Path) -> PathBuf {
    let folded = path.to_string_lossy().to_lowercase();
    PathBuf::from(folded)
}

#[cfg(unix)]
fn same_directory_identity(left: &Path, right: &Path) -> bool {
    use std::os::unix::fs::MetadataExt;
    match (fs::metadata(left), fs::metadata(right)) {
        (Ok(lm), Ok(rm)) => lm.dev() == rm.dev() && lm.ino() == rm.ino(),
        _ => false,
    }
}

#[cfg(not(unix))]
fn same_directory_identity(_left: &Path, _right: &Path) -> bool {
    false
}

/// Return `true` when `left` and `right` refer to the same directory or one is nested under the other
/// (including symlink resolution and case-folded comparison for case-insensitive filesystems).
pub fn data_dirs_overlap(left: &Path, right: &Path) -> Result<bool, FeedError> {
    let left_norm = normalize_resolving_prefix(left)?;
    let right_norm = normalize_resolving_prefix(right)?;
    if same_directory_identity(&left_norm, &right_norm) {
        return Ok(true);
    }
    let left_folded = casefold_path(&left_norm);
    let right_folded = casefold_path(&right_norm);
    Ok(left_folded == right_folded
        || left_folded.starts_with(&right_folded)
        || right_folded.starts_with(&left_folded))
}

/// Enforce the safety guard forbidding replay mode from writing into the default production data dir
/// (`.talaria`), `$TALARIA_DATA_DIR`, or any caller-supplied protected service directories.
pub fn validate_replay_data_dir(
    data_dir: Option<&Path>,
    extra_protected: &[PathBuf],
) -> Result<PathBuf, FeedError> {
    let env_dir = std::env::var_os("TALARIA_DATA_DIR")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from);
    validate_replay_data_dir_with_env(data_dir, env_dir.as_deref(), extra_protected)
}

/// Race-free variant of [`validate_replay_data_dir`] accepting an explicit `env_data_dir` override.
pub fn validate_replay_data_dir_with_env(
    data_dir: Option<&Path>,
    env_data_dir: Option<&Path>,
    extra_protected: &[PathBuf],
) -> Result<PathBuf, FeedError> {
    let target = data_dir.ok_or_else(|| {
        FeedError::ProtectedDataDir(
            "replay mode requires an explicit isolated --data-dir".to_owned(),
        )
    })?;
    let resolved_target = normalize_resolving_prefix(target)?;

    let mut protected = vec![PathBuf::from(DEFAULT_DATA_DIR)];
    if let Some(env_dir) = env_data_dir
        && !env_dir.as_os_str().is_empty()
    {
        protected.push(env_dir.to_path_buf());
    }
    protected.extend(extra_protected.iter().cloned());

    for guard in &protected {
        if data_dirs_overlap(&resolved_target, guard)? {
            return Err(FeedError::ProtectedDataDir(format!(
                "replay data_dir `{}` overlaps protected production directory `{}`",
                resolved_target.display(),
                guard.display()
            )));
        }
    }

    Ok(resolved_target)
}

/// Configuration for [`ReplayTransport`].
#[derive(Clone)]
pub struct ReplayConfig {
    /// Positive speed multiplier (`1.0` = real time, `2.0` = 2x faster).
    pub speed: f64,
    /// Injectable virtual or system clock.
    pub clock: Arc<dyn FeedClock>,
    /// Idle heartbeat interval after replay script EOF (before `/ speed`).
    pub idle_heartbeat_ms: u64,
}

impl Default for ReplayConfig {
    fn default() -> Self {
        Self {
            speed: 1.0,
            clock: Arc::new(SystemClock::new()),
            idle_heartbeat_ms: DEFAULT_IDLE_HEARTBEAT_MS,
        }
    }
}

struct ReplayCursor {
    index: usize,
    started_at_ms: Option<i64>,
    connected: bool,
}

/// Deterministic JSONL [`FeedTransport`] implementing `demo_outage.jsonl` et al.
pub struct ReplayTransport {
    lines: Vec<ReplayLine>,
    speed: f64,
    clock: Arc<dyn FeedClock>,
    idle_heartbeat_ms: u64,
    closed: AtomicBool,
    exhausted: AtomicBool,
    subscriptions: StdMutex<BTreeSet<(String, String)>>,
    cursor: AsyncMutex<ReplayCursor>,
}

impl ReplayTransport {
    /// Load and validate a JSONL replay file with the given [`ReplayConfig`].
    pub fn from_file(path: impl AsRef<Path>, config: ReplayConfig) -> Result<Self, FeedError> {
        if !config.speed.is_finite() || config.speed <= 0.0 {
            return Err(FeedError::InvalidRequest(format!(
                "replay speed must be positive and finite, got {}",
                config.speed
            )));
        }
        let lines = read_replay_file(path)?;
        Ok(Self {
            lines,
            speed: config.speed,
            clock: config.clock,
            idle_heartbeat_ms: config.idle_heartbeat_ms,
            closed: AtomicBool::new(false),
            exhausted: AtomicBool::new(false),
            subscriptions: StdMutex::new(BTreeSet::new()),
            cursor: AsyncMutex::new(ReplayCursor {
                index: 0,
                started_at_ms: None,
                connected: false,
            }),
        })
    }

    /// Load a replay script after verifying `target_data_dir` does not overlap any protected
    /// production data directory.
    pub fn from_file_for_data_dir(
        path: impl AsRef<Path>,
        target_data_dir: Option<&Path>,
        extra_protected: &[PathBuf],
        config: ReplayConfig,
    ) -> Result<Self, FeedError> {
        validate_replay_data_dir(target_data_dir, extra_protected)?;
        Self::from_file(path, config)
    }

    /// Number of validated lines in the loaded replay script.
    pub fn line_count(&self) -> usize {
        self.lines.len()
    }

    /// Return `true` once the replay script has consumed all lines and entered idle heartbeats.
    pub fn is_script_exhausted(&self) -> bool {
        self.exhausted.load(Ordering::Acquire)
    }

    async fn wait_until(&self, started_at_ms: i64, offset_ms: i64) {
        let scaled_ms = ((offset_ms as f64) / self.speed).round() as i64;
        let target_ms = started_at_ms.saturating_add(scaled_ms);
        let delay_ms = target_ms - self.clock.elapsed_ms();
        if delay_ms > 0 {
            self.clock
                .sleep(Duration::from_millis(delay_ms as u64))
                .await;
        }
    }

    fn is_kline_subscribed(&self, instrument_id: &str, timeframe: &str) -> bool {
        self.subscriptions
            .lock()
            .expect("subscriptions lock")
            .contains(&(instrument_id.to_owned(), timeframe.to_owned()))
    }

    fn is_book_subscribed(&self, instrument_id: &str) -> bool {
        self.subscriptions
            .lock()
            .expect("subscriptions lock")
            .iter()
            .any(|(inst, _)| inst == instrument_id)
    }
}

impl FeedTransport for ReplayTransport {
    fn feed_mode(&self) -> &str {
        "replay"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            if self.closed.load(Ordering::Acquire) {
                return Err(PackError::FeedTransport(
                    "replay transport is closed".to_owned(),
                ));
            }
            let mut cursor = self.cursor.lock().await;
            if self.closed.load(Ordering::Acquire) {
                return Err(PackError::FeedTransport(
                    "replay transport is closed".to_owned(),
                ));
            }
            let Some(started) = cursor.started_at_ms else {
                cursor.started_at_ms = Some(self.clock.elapsed_ms());
                cursor.connected = true;
                return Ok(());
            };
            while cursor.index < self.lines.len()
                && self.lines[cursor.index].kind == ReplayLineKind::Reconnect
            {
                let offset = self.lines[cursor.index].offset_ms;
                self.wait_until(started, offset).await;
                cursor.index += 1;
            }
            cursor.connected = true;
            Ok(())
        })
    }

    fn subscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            self.subscriptions
                .lock()
                .expect("subscriptions lock")
                .insert((instrument_id.to_owned(), timeframe.to_owned()));
            Ok(())
        })
    }

    fn unsubscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            self.subscriptions
                .lock()
                .expect("subscriptions lock")
                .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
            Ok(())
        })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            let mut cursor = self.cursor.lock().await;
            if !cursor.connected || self.closed.load(Ordering::Acquire) {
                return Err(PackError::FeedTransport(
                    "replay transport is not connected".to_owned(),
                ));
            }
            let started = cursor.started_at_ms.ok_or_else(|| {
                PackError::FeedTransport("replay transport is not connected".to_owned())
            })?;

            while cursor.index < self.lines.len() {
                let line = self.lines[cursor.index].clone();
                if line.kind == ReplayLineKind::Reconnect {
                    cursor.index += 1;
                    continue;
                }
                // Wait BEFORE advancing `index` so cancelling `recv()` never drops a line.
                self.wait_until(started, line.offset_ms).await;
                if self.closed.load(Ordering::Acquire) {
                    return Err(PackError::FeedTransport(
                        "replay transport is closed".to_owned(),
                    ));
                }
                cursor.index += 1;

                match line.kind {
                    ReplayLineKind::Disconnect => {
                        cursor.connected = false;
                        let reason = if line.reason.is_empty() {
                            "replay injected disconnect".to_owned()
                        } else {
                            line.reason
                        };
                        return Err(PackError::FeedTransport(reason));
                    }
                    ReplayLineKind::Heartbeat => return Ok(None),
                    ReplayLineKind::Kline => {
                        if let Some(event) = line.kline
                            && self.is_kline_subscribed(&event.instrument_id, &event.timeframe)
                        {
                            return Ok(Some(TransportFrame::Event(TransportEvent::Kline(event))));
                        }
                    }
                    ReplayLineKind::Book => {
                        if let Some(event) = line.book
                            && self.is_book_subscribed(&event.instrument_id)
                        {
                            return Ok(Some(TransportFrame::Event(TransportEvent::OrderBook(
                                event,
                            ))));
                        }
                    }
                    ReplayLineKind::Derivative => {
                        if let Some(event) = line.derivative
                            && self.is_book_subscribed(&event.instrument_id)
                        {
                            return Ok(Some(TransportFrame::Event(TransportEvent::DerivativeTick(
                                event,
                            ))));
                        }
                    }
                    ReplayLineKind::Reconnect => {}
                }
            }

            self.exhausted.store(true, Ordering::Release);
            let idle_ms = ((self.idle_heartbeat_ms as f64) / self.speed).round() as u64;
            if idle_ms > 0 {
                self.clock.sleep(Duration::from_millis(idle_ms)).await;
            }
            if self.closed.load(Ordering::Acquire) {
                return Err(PackError::FeedTransport(
                    "replay transport is closed".to_owned(),
                ));
            }
            Ok(None)
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            self.closed.store(true, Ordering::Release);
            if let Ok(mut cursor) = self.cursor.try_lock() {
                cursor.connected = false;
            }
            Ok(())
        })
    }
}
