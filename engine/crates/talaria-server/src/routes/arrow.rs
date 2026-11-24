//! Zero-copy Apache Arrow IPC streaming endpoints:
//! - `GET /api/bars.arrow`
//! - `GET /api/ticks.arrow`
//! - `GET /api/derivatives.arrow`

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::File;
use std::io::Read as _;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};

use arrow::array::Int64Array;
use arrow::ipc::writer::StreamWriter;
use arrow::record_batch::RecordBatch;
use axum::Router;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::header::{CACHE_CONTROL, CONTENT_TYPE};
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use talaria_contracts::bars::RawBar;
use talaria_contracts::derivatives::DerivativeKind;
use talaria_contracts::last_closed_bar_start;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::ticks::TickKind;
use talaria_store::{
    ColdGeneration, ColdManifest, ColdReader, TICK_BATCH_ROWS, bars_to_record_batch,
    derivatives_to_record_batch, quotes_to_record_batch, range_visible_bars, read_bars_parquet,
    trades_to_record_batch,
};

use crate::error::{ApiError, ApiQuery};
use crate::routes::{find_instrument, manifest_range_fingerprint, validate_timeframe};
use crate::security::{
    BARS_HOT_REVISION_HEADER, BARS_HOT_REVISION_UNSTABLE, BARS_MANIFEST_HEADER, DATA_DIR_HEADER,
    NOW_MS_HEADER,
};
use crate::server::AppState;

pub const ARROW_STREAM_CONTENT_TYPE: &str = "application/vnd.apache.arrow.stream";

const MAX_GEN_CACHE_BYTES: usize = 256 * 1024 * 1024;
const MAX_STREAM_CACHE_BYTES: usize = 512 * 1024 * 1024;
const MAX_STREAM_CACHE_ENTRIES: usize = 4;

#[derive(Clone)]
struct CachedGeneration {
    batch: RecordBatch,
    max_ts_available: i64,
    all_confirmed: bool,
    strictly_monotonic: bool,
    bytes_est: usize,
}

#[derive(Clone)]
struct CachedIpcStream {
    bytes: Bytes,
    max_ts_available: i64,
    all_confirmed: bool,
}

type GenKey = (String, String);
type StreamKey = (String, String, i64, i64, String);

/// Bounded content-addressed Arrow IPC & per-generation cold batch cache.
#[derive(Default)]
pub struct ColdArrowCache {
    generations: Mutex<GenCacheInner>,
    streams: Mutex<StreamCacheInner>,
}

#[derive(Default)]
struct GenCacheInner {
    map: HashMap<GenKey, Arc<CachedGeneration>>,
    order: VecDeque<GenKey>,
    total_bytes: usize,
}

#[derive(Default)]
struct StreamCacheInner {
    map: HashMap<StreamKey, CachedIpcStream>,
    order: VecDeque<StreamKey>,
    total_bytes: usize,
}

impl ColdArrowCache {
    pub fn new() -> Self {
        Self::default()
    }

    fn get_generation(&self, key: &GenKey) -> Option<Arc<CachedGeneration>> {
        let guard = self.generations.lock().expect("gen cache poisoned");
        guard.map.get(key).cloned()
    }

    fn insert_generation(&self, key: GenKey, value: Arc<CachedGeneration>) {
        if value.bytes_est > MAX_GEN_CACHE_BYTES {
            return;
        }
        let mut guard = self.generations.lock().expect("gen cache poisoned");
        if guard.map.contains_key(&key) {
            return;
        }
        while guard.total_bytes + value.bytes_est > MAX_GEN_CACHE_BYTES {
            let Some(oldest) = guard.order.pop_front() else {
                break;
            };
            if let Some(removed) = guard.map.remove(&oldest) {
                guard.total_bytes = guard.total_bytes.saturating_sub(removed.bytes_est);
            }
        }
        guard.total_bytes += value.bytes_est;
        guard.order.push_back(key.clone());
        guard.map.insert(key, value);
    }

    fn get_stream(&self, key: &StreamKey) -> Option<CachedIpcStream> {
        let guard = self.streams.lock().expect("stream cache poisoned");
        guard.map.get(key).cloned()
    }

    fn insert_stream(&self, key: StreamKey, value: CachedIpcStream) {
        let len = value.bytes.len();
        if len > MAX_STREAM_CACHE_BYTES {
            return;
        }
        let mut guard = self.streams.lock().expect("stream cache poisoned");
        if guard.map.contains_key(&key) {
            return;
        }
        while guard.order.len() >= MAX_STREAM_CACHE_ENTRIES
            || guard.total_bytes + len > MAX_STREAM_CACHE_BYTES
        {
            let Some(oldest) = guard.order.pop_front() else {
                break;
            };
            if let Some(removed) = guard.map.remove(&oldest) {
                guard.total_bytes = guard.total_bytes.saturating_sub(removed.bytes.len());
            }
        }
        guard.total_bytes += len;
        guard.order.push_back(key.clone());
        guard.map.insert(key, value);
    }
}

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/bars.arrow", get(get_bars_arrow))
        .route("/api/ticks.arrow", get(get_ticks_arrow))
        .route("/api/derivatives.arrow", get(get_derivatives_arrow))
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArrowBarsQuery {
    pub instrument: String,
    pub timeframe: String,
    #[serde(default)]
    pub start: i64,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub as_of: Option<i64>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    pub include_open: bool,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    pub tail_only: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArrowTicksQuery {
    pub instrument: String,
    #[serde(default = "default_tick_kind")]
    pub kind: String,
    #[serde(default)]
    pub start: i64,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub as_of: Option<i64>,
}

fn default_tick_kind() -> String {
    "trades".to_owned()
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ArrowDerivativesQuery {
    pub instrument: String,
    pub kind: String,
    #[serde(default)]
    pub start: i64,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub as_of: Option<i64>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    pub historical_only: bool,
}

fn serialize_record_batch_ipc(batch: &RecordBatch) -> Result<Bytes, ApiError> {
    serialize_record_batches_ipc(std::slice::from_ref(batch), &batch.schema())
}

fn serialize_record_batches_ipc(
    batches: &[RecordBatch],
    schema: &arrow::datatypes::SchemaRef,
) -> Result<Bytes, ApiError> {
    let total_rows: usize = batches.iter().map(RecordBatch::num_rows).sum();
    let est = total_rows.saturating_mul(160).max(4096);
    let mut buf = Vec::with_capacity(est);
    {
        let mut writer = StreamWriter::try_new(&mut buf, schema)
            .map_err(|e| ApiError::internal(format!("arrow ipc init failed: {e}")))?;
        for batch in batches {
            if batch.num_rows() > 0 {
                writer
                    .write(batch)
                    .map_err(|e| ApiError::internal(format!("arrow ipc write failed: {e}")))?;
            }
        }
        writer
            .finish()
            .map_err(|e| ApiError::internal(format!("arrow ipc finish failed: {e}")))?;
    }
    Ok(Bytes::from(buf))
}

fn safe_generation_path(data_dir: &Path, rel: &Path) -> Result<PathBuf, ApiError> {
    if rel.is_absolute()
        || rel
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(ApiError::internal(format!(
            "invalid cold generation relative path: {}",
            rel.display()
        )));
    }
    Ok(data_dir.join(rel))
}

fn verify_and_load_generation(
    data_dir: &Path,
    spec: &InstrumentSpec,
    generation: &ColdGeneration,
) -> Result<Arc<CachedGeneration>, ApiError> {
    let path = safe_generation_path(data_dir, &generation.parquet_path)?;
    let mut file = File::open(&path)
        .map_err(|e| ApiError::internal(format!("open cold generation {}: {e}", path.display())))?;
    let mut hasher = Sha256::new();
    let mut buf = [0u8; 64 * 1024];
    loop {
        let n = file
            .read(&mut buf)
            .map_err(|e| ApiError::internal(format!("read cold generation {}: {e}", path.display())))?;
        if n == 0 {
            break;
        }
        hasher.update(&buf[..n]);
    }
    let digest = hex::encode(hasher.finalize());
    if digest != generation.sha256 {
        return Err(ApiError::internal(format!(
            "generation sha256 mismatch: {}",
            generation.parquet_path.display()
        )));
    }
    let bars = read_bars_parquet(&path, None, None)?;
    let mut strictly_monotonic = true;
    let mut all_confirmed = true;
    let mut max_ts_available = i64::MIN;
    let mut prev_ts: Option<i64> = None;
    for bar in &bars {
        if let Some(prev) = prev_ts
            && bar.ts_event <= prev
        {
            strictly_monotonic = false;
        }
        prev_ts = Some(bar.ts_event);
        if !bar.confirmed {
            all_confirmed = false;
        }
        if bar.ts_available > max_ts_available {
            max_ts_available = bar.ts_available;
        }
    }
    let batch = bars_to_record_batch(&bars, spec)?;
    let bytes_est = batch.num_rows().saturating_mul(160);
    Ok(Arc::new(CachedGeneration {
        batch,
        max_ts_available,
        all_confirmed,
        strictly_monotonic,
        bytes_est,
    }))
}

fn slice_batch_by_ts_range(batch: &RecordBatch, start: i64, end: i64) -> Option<RecordBatch> {
    if batch.num_rows() == 0 {
        return None;
    }
    let ts_idx = batch.schema().index_of("ts_event").ok()?;
    let ts_arr = batch.column(ts_idx).as_any().downcast_ref::<Int64Array>()?;
    let vals = ts_arr.values();
    let lo = vals.partition_point(|&ts| ts < start);
    let hi = vals.partition_point(|&ts| ts <= end);
    if lo >= hi {
        return None;
    }
    if lo == 0 && hi == batch.num_rows() {
        Some(batch.clone())
    } else {
        Some(batch.slice(lo, hi - lo))
    }
}

struct FastColdQuery<'a> {
    spec: &'a InstrumentSpec,
    timeframe: &'a str,
    start: i64,
    end: i64,
    effective_as_of: i64,
    include_open: bool,
}

type LoadedGenerationChunk = Vec<(usize, Arc<CachedGeneration>)>;

fn try_fast_cold_arrow(
    reader: &ColdReader,
    cache: &ColdArrowCache,
    q: FastColdQuery<'_>,
) -> Result<Option<(Bytes, String)>, ApiError> {
    let FastColdQuery {
        spec,
        timeframe,
        start,
        end,
        effective_as_of,
        include_open,
    } = q;
    let mut range_gens: Vec<ColdGeneration> = reader
        .generations(spec, timeframe)?
        .into_iter()
        .filter(|g| g.last_ts >= start && g.first_ts <= end)
        .collect();
    if range_gens.is_empty() {
        return Ok(None);
    }
    range_gens.sort_by_key(|g| (g.first_ts, g.last_ts));
    let mut prev_last: Option<i64> = None;
    for g in &range_gens {
        if let Some(p) = prev_last
            && g.first_ts <= p
        {
            // Overlapping uncompacted revisions: use exact revision merge path.
            return Ok(None);
        }
        prev_last = Some(g.last_ts);
    }

    let fingerprint = manifest_range_fingerprint(
        &ColdManifest {
            version: 0,
            generations: range_gens.clone(),
        },
        start,
        Some(end),
    );
    let stream_key = (
        spec.instrument_id.clone(),
        timeframe.to_owned(),
        start,
        end,
        fingerprint.clone(),
    );
    if let Some(hit) = cache.get_stream(&stream_key)
        && hit.max_ts_available <= effective_as_of
        && (include_open || hit.all_confirmed)
    {
        return Ok(Some((hit.bytes, fingerprint)));
    }

    let mut resolved: Vec<Option<Arc<CachedGeneration>>> = vec![None; range_gens.len()];
    let mut missing: Vec<(usize, &ColdGeneration)> = Vec::new();
    for (idx, g) in range_gens.iter().enumerate() {
        let key = (
            g.parquet_path.to_string_lossy().replace('\\', "/"),
            g.sha256.clone(),
        );
        if let Some(cached) = cache.get_generation(&key) {
            resolved[idx] = Some(cached);
        } else {
            missing.push((idx, g));
        }
    }

    if !missing.is_empty() {
        let data_dir = reader.data_dir();
        let workers = std::thread::available_parallelism()
            .map_or(4, |n| n.get())
            .min(missing.len())
            .max(1);
        let chunk_size = missing.len().div_ceil(workers);
        let mut results: Vec<Result<LoadedGenerationChunk, ApiError>> =
            Vec::with_capacity(workers);

        std::thread::scope(|scope| {
            let mut handles = Vec::with_capacity(workers);
            for chunk in missing.chunks(chunk_size) {
                handles.push(scope.spawn(move || {
                    let mut local = Vec::with_capacity(chunk.len());
                    for &(idx, g) in chunk {
                        let loaded = verify_and_load_generation(data_dir, spec, g)?;
                        local.push((idx, loaded));
                    }
                    Ok(local)
                }));
            }
            for h in handles {
                match h.join() {
                    Ok(res) => results.push(res),
                    Err(_) => return Err(ApiError::internal("parallel cold generation reader panicked")),
                }
            }
            Ok(())
        })?;

        for res in results {
            for (idx, entry) in res? {
                let g = &range_gens[idx];
                let key = (
                    g.parquet_path.to_string_lossy().replace('\\', "/"),
                    g.sha256.clone(),
                );
                cache.insert_generation(key, Arc::clone(&entry));
                resolved[idx] = Some(entry);
            }
        }
    }

    let mut max_ts_available = i64::MIN;
    let mut all_confirmed = true;
    let mut batches = Vec::with_capacity(resolved.len());
    for (idx, item_opt) in resolved.into_iter().enumerate() {
        let item = item_opt.ok_or_else(|| ApiError::internal("missing resolved generation"))?;
        if !item.strictly_monotonic
            || item.max_ts_available > effective_as_of
            || (!include_open && !item.all_confirmed)
        {
            return Ok(None);
        }
        max_ts_available = max_ts_available.max(item.max_ts_available);
        all_confirmed &= item.all_confirmed;
        let g = &range_gens[idx];
        if g.first_ts < start || g.last_ts > end {
            if let Some(sliced) = slice_batch_by_ts_range(&item.batch, start, end) {
                batches.push(sliced);
            }
        } else if item.batch.num_rows() > 0 {
            batches.push(item.batch.clone());
        }
    }

    let schema = match batches.first() {
        Some(first) => first.schema(),
        None => bars_to_record_batch(&[], spec)?.schema(),
    };
    let ipc_bytes = serialize_record_batches_ipc(&batches, &schema)?;
    cache.insert_stream(
        stream_key,
        CachedIpcStream {
            bytes: ipc_bytes.clone(),
            max_ts_available,
            all_confirmed,
        },
    );
    Ok(Some((ipc_bytes, fingerprint)))
}

fn arrow_response(
    body: Bytes,
    data_dir_id: &str,
    now_ms: i64,
    manifest_fp: Option<&str>,
    hot_rev: Option<&str>,
) -> Response {
    let mut headers = HeaderMap::new();
    headers.insert(
        CONTENT_TYPE,
        HeaderValue::from_static(ARROW_STREAM_CONTENT_TYPE),
    );
    headers.insert(CACHE_CONTROL, HeaderValue::from_static("no-store"));
    if let Ok(v) = HeaderValue::from_str(data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    if let Ok(v) = HeaderValue::from_str(&now_ms.to_string()) {
        headers.insert(NOW_MS_HEADER, v);
    }
    if let Some(fp) = manifest_fp
        && let Ok(v) = HeaderValue::from_str(fp)
    {
        headers.insert(BARS_MANIFEST_HEADER, v);
    }
    if let Some(hr) = hot_rev
        && let Ok(v) = HeaderValue::from_str(hr)
    {
        headers.insert(BARS_HOT_REVISION_HEADER, v);
    }
    (StatusCode::OK, headers, body).into_response()
}

fn can_borrow_cold_directly(cold: &[RawBar], as_of: i64, include_open: bool) -> bool {
    let mut prev_ts: Option<i64> = None;
    for bar in cold {
        if let Some(p) = prev_ts
            && bar.ts_event <= p
        {
            return false;
        }
        prev_ts = Some(bar.ts_event);
        if bar.ts_available > as_of {
            return false;
        }
        if !include_open && !bar.confirmed {
            return false;
        }
    }
    true
}

pub async fn get_bars_arrow(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<ArrowBarsQuery>,
) -> Result<Response, ApiError> {
    if query.start < 0 {
        return Err(ApiError::validation("start must be non-negative"));
    }
    if let Some(end) = query.end
        && end < query.start
    {
        return Err(ApiError::validation("end must not precede start"));
    }
    if let Some(as_of) = query.as_of
        && as_of < 0
    {
        return Err(ApiError::validation("as_of must be non-negative"));
    }
    let spec = find_instrument(&state, &query.instrument)?;
    let interval = validate_timeframe(&query.timeframe)?;
    let now = state.now_ms();
    let end = query
        .end
        .unwrap_or_else(|| last_closed_bar_start(now, interval));
    let effective_as_of = query.as_of.unwrap_or(now);

    let reader = Arc::clone(&state.reader);
    let hot_buffer = Arc::clone(&state.hot_buffer);
    let arrow_cache = Arc::clone(&state.arrow_cache);
    let tf = query.timeframe.clone();
    let start = query.start;
    let include_open = query.include_open;
    let tail_only = query.tail_only;
    let as_of_opt = query.as_of;

    let (ipc_bytes, fingerprint, hot_revision) = tokio::task::spawn_blocking(move || -> Result<(Bytes, String, String), ApiError> {
        let t0 = std::time::Instant::now();
        let pre_rev = if tail_only {
            hot_buffer.series_version(&spec.instrument_id, &tf)
        } else {
            "none".to_owned()
        };

        let hot = hot_buffer.revisions(&spec.instrument_id, &tf, start, Some(end));
        let hot_revision = if tail_only {
            let post_rev = hot_buffer.series_version(&spec.instrument_id, &tf);
            if post_rev == pre_rev {
                pre_rev
            } else {
                BARS_HOT_REVISION_UNSTABLE.to_owned()
            }
        } else {
            "none".to_owned()
        };

        if !tail_only
            && hot.is_empty()
            && let Some((ipc_bytes, fingerprint)) = try_fast_cold_arrow(
                &reader,
                &arrow_cache,
                FastColdQuery {
                    spec: &spec,
                    timeframe: &tf,
                    start,
                    end,
                    effective_as_of,
                    include_open,
                },
            )?
        {
            tracing::debug!(
                bytes = ipc_bytes.len(),
                fast_ms = t0.elapsed().as_secs_f64() * 1000.0,
                "get_bars_arrow fast path"
            );
            return Ok((ipc_bytes, fingerprint, hot_revision));
        }

        let (manifest, cold) = reader.read_manifest_snapshot(&spec, &tf, Some(start), Some(end))?;
        let t_cold = t0.elapsed();
        let fingerprint = manifest_range_fingerprint(&manifest, start, Some(end));

        // Fast zero-clone path when hot buffer is empty and cold partitions are clean
        let batch = if !tail_only
            && hot.is_empty()
            && can_borrow_cold_directly(&cold, effective_as_of, include_open)
        {
            bars_to_record_batch(&cold, &spec)?
        } else {
            let mut visible = range_visible_bars(
                &cold,
                &hot,
                as_of_opt.or(Some(now)),
                include_open,
            );
            if tail_only && !cold.is_empty() {
                let archived: HashSet<(i64, u64)> =
                    cold.iter().map(|b| (b.ts_event, b.revision)).collect();
                visible.retain(|b| !archived.contains(&(b.ts_event, b.revision)));
            }
            bars_to_record_batch(&visible, &spec)?
        };
        let t_batch = t0.elapsed();

        let ipc_bytes = serialize_record_batch_ipc(&batch)?;
        let t_ipc = t0.elapsed();
        tracing::debug!(
            rows = batch.num_rows(),
            bytes = ipc_bytes.len(),
            cold_ms = t_cold.as_secs_f64() * 1000.0,
            batch_ms = (t_batch - t_cold).as_secs_f64() * 1000.0,
            ipc_ms = (t_ipc - t_batch).as_secs_f64() * 1000.0,
            "get_bars_arrow timing"
        );
        Ok((ipc_bytes, fingerprint, hot_revision))
    })
    .await
    .map_err(|e| ApiError::internal(format!("arrow bars join error: {e}")))??;

    Ok(arrow_response(
        ipc_bytes,
        &state.security.data_dir_id,
        now,
        Some(&fingerprint),
        Some(&hot_revision),
    ))
}

pub async fn get_ticks_arrow(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<ArrowTicksQuery>,
) -> Result<Response, ApiError> {
    if query.start < 0 {
        return Err(ApiError::validation("start must be non-negative"));
    }
    if let Some(end) = query.end
        && end < query.start
    {
        return Err(ApiError::validation("end must not precede start"));
    }
    let spec = find_instrument(&state, &query.instrument)?;
    let kind = match query.kind.trim().to_ascii_lowercase().as_str() {
        "trades" | "trade" => TickKind::Trades,
        "quotes" | "quote" => TickKind::Quotes,
        other => {
            return Err(ApiError::validation(format!(
                "unsupported tick kind: {other}"
            )));
        }
    };

    let now = state.now_ms();
    let reader = Arc::clone(&state.reader);
    let start = query.start;
    let end = query.end;
    let as_of = query.as_of;

    let ipc_bytes = tokio::task::spawn_blocking(move || -> Result<Bytes, ApiError> {
        let mut buf = Vec::with_capacity(4096);
        match kind {
            TickKind::Trades => {
                let rows = reader.read_trades(&spec, Some(start), end, as_of)?;
                let schema_batch = trades_to_record_batch(&rows[..rows.len().min(TICK_BATCH_ROWS)], &spec)?;
                let mut writer = StreamWriter::try_new(&mut buf, &schema_batch.schema())
                    .map_err(|e| ApiError::internal(e.to_string()))?;
                writer
                    .write(&schema_batch)
                    .map_err(|e| ApiError::internal(e.to_string()))?;
                for chunk in rows.chunks(TICK_BATCH_ROWS).skip(1) {
                    let b = trades_to_record_batch(chunk, &spec)?;
                    writer
                        .write(&b)
                        .map_err(|e| ApiError::internal(e.to_string()))?;
                }
                writer
                    .finish()
                    .map_err(|e| ApiError::internal(e.to_string()))?;
            }
            TickKind::Quotes => {
                let rows = reader.read_quotes(&spec, Some(start), end, as_of)?;
                let schema_batch = quotes_to_record_batch(&rows[..rows.len().min(TICK_BATCH_ROWS)], &spec)?;
                let mut writer = StreamWriter::try_new(&mut buf, &schema_batch.schema())
                    .map_err(|e| ApiError::internal(e.to_string()))?;
                writer
                    .write(&schema_batch)
                    .map_err(|e| ApiError::internal(e.to_string()))?;
                for chunk in rows.chunks(TICK_BATCH_ROWS).skip(1) {
                    let b = quotes_to_record_batch(chunk, &spec)?;
                    writer
                        .write(&b)
                        .map_err(|e| ApiError::internal(e.to_string()))?;
                }
                writer
                    .finish()
                    .map_err(|e| ApiError::internal(e.to_string()))?;
            }
        }
        Ok(Bytes::from(buf))
    })
    .await
    .map_err(|e| ApiError::internal(format!("arrow ticks join error: {e}")))??;

    Ok(arrow_response(
        ipc_bytes,
        &state.security.data_dir_id,
        now,
        None,
        None,
    ))
}

pub async fn get_derivatives_arrow(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<ArrowDerivativesQuery>,
) -> Result<Response, ApiError> {
    if query.start < 0 {
        return Err(ApiError::validation("start must be non-negative"));
    }
    if let Some(end) = query.end
        && end < query.start
    {
        return Err(ApiError::validation("end must not precede start"));
    }
    let spec = find_instrument(&state, &query.instrument)?;
    let kind = parse_derivative_kind(&query.kind)?;
    let now = state.now_ms();
    let reader = Arc::clone(&state.reader);
    let start = query.start;
    let end = query.end;
    let as_of = query.as_of;
    let hist_only = query.historical_only;

    let ipc_bytes = tokio::task::spawn_blocking(move || -> Result<Bytes, ApiError> {
        let rows = reader.read_derivatives(&spec, kind, Some(start), end, as_of, hist_only)?;
        let batch = derivatives_to_record_batch(&rows, &spec, kind)?;
        serialize_record_batch_ipc(&batch)
    })
    .await
    .map_err(|e| ApiError::internal(format!("arrow derivatives join error: {e}")))??;

    Ok(arrow_response(
        ipc_bytes,
        &state.security.data_dir_id,
        now,
        None,
        None,
    ))
}

fn parse_derivative_kind(raw: &str) -> Result<DerivativeKind, ApiError> {
    match raw.trim().to_ascii_uppercase().as_str() {
        "MARK_PRICE" | "MARKPRICE" => Ok(DerivativeKind::MarkPrice),
        "FUNDING_RATE" | "FUNDINGRATE" => Ok(DerivativeKind::FundingRate),
        "OPEN_INTEREST" | "OPENINTEREST" | "OI" => Ok(DerivativeKind::OpenInterest),
        _ => Err(ApiError::validation(format!(
            "unsupported derivative kind: {raw}"
        ))),
    }
}
