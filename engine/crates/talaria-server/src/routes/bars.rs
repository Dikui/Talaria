//! Health, feed health, instruments, JSON range `/api/bars`, and history backfill endpoints.

use std::collections::HashSet;
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderValue, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use chrono::NaiveDate;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::bars::{BarsQuery, MAX_RANGE_BARS, RangeBar};
use talaria_contracts::derivatives::DerivativeKind;
use talaria_contracts::error_codes::{
    BARS_RANGE_TOO_LARGE, FEATURE_DISABLED, FEED_UNAVAILABLE,
};
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::{SUPPORTED_TIMEFRAMES, last_closed_bar_start};
use talaria_packs::{
    BackfillWindow, CatalogInstrument, DEFAULT_SEARCH_LIMIT, HistoryConnectorConfig,
    backfill_bars, backfill_derivatives,
};
use talaria_store::{partition_day, range_visible_bars, revision_window};

use crate::error::{ApiError, ApiJson, ApiQuery};
use crate::routes::{
    find_instrument, manifest_range_fingerprint, publish_bars_to_cold_store,
    publish_derivatives_to_cold_store, validate_timeframe,
};
use crate::security::{
    BARS_HOT_REVISION_HEADER, BARS_HOT_REVISION_UNSTABLE, BARS_MANIFEST_HEADER, DATA_DIR_HEADER,
    NOW_MS_HEADER,
};
use crate::server::AppState;
use crate::state_store::custom_instruments_upsert;
use crate::ws::strategy::raw_to_range_bar;

const RANGE_LIMIT_MESSAGE: &str = "一次最多查询 10,000 根 K 线，请分段查询";

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/health", get(health))
        .route("/api/feed/health", get(feed_health))
        .route("/api/instruments", get(instruments).post(add_instrument))
        .route("/api/instruments/search", get(search_instruments))
        .route("/api/bars", get(get_range_bars))
        .route("/api/derivatives/backfill", get(backfill_derivatives_handler))
        .route("/api/trades/backfill", post(backfill_trades_handler))
}

#[derive(Serialize)]
struct HealthResponse<'a> {
    ok: bool,
    version: &'static str,
    feed_mode: &'a str,
}

async fn health(State(state): State<Arc<AppState>>) -> Response {
    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&state.security.data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    if let Ok(v) = HeaderValue::from_str(&state.now_ms().to_string()) {
        headers.insert(NOW_MS_HEADER, v);
    }
    (
        StatusCode::OK,
        headers,
        ApiJson(HealthResponse {
            ok: true,
            version: env!("CARGO_PKG_VERSION"),
            feed_mode: state.feed_mode_str(),
        }),
    )
        .into_response()
}

async fn feed_health(State(state): State<Arc<AppState>>) -> Result<ApiJson<talaria_contracts::feed::FeedHealthReport>, ApiError> {
    let Some(sup) = &state.supervisor else {
        return Err(ApiError::service_unavailable(
            FEED_UNAVAILABLE,
            "当前运行在离线样本模式，没有实时行情流；请用 --replay-events 或实盘模式启动",
        ));
    };
    Ok(ApiJson(sup.health()))
}

async fn instruments(State(state): State<Arc<AppState>>) -> Response {
    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&state.security.data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    (StatusCode::OK, headers, ApiJson(state.instruments_snapshot())).into_response()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SearchInstrumentsQuery {
    #[serde(default)]
    q: String,
    #[serde(default)]
    venue: Option<String>,
    #[serde(default)]
    limit: Option<usize>,
}

#[derive(Serialize)]
struct SearchInstrumentsResponse {
    pack: String,
    query: String,
    results: Vec<CatalogInstrument>,
}

async fn search_instruments(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<SearchInstrumentsQuery>,
) -> Result<ApiJson<SearchInstrumentsResponse>, ApiError> {
    let limit = query.limit.unwrap_or(DEFAULT_SEARCH_LIMIT).clamp(1, 200);
    let venue = query
        .venue
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty());
    let hcfg = HistoryConnectorConfig {
        base_url: state.history_base_url.clone(),
        fixture_dir: state.fixture_dir.clone(),
        ..HistoryConnectorConfig::default()
    };
    let mut results = state
        .pack
        .search_catalog(&query.q, venue, limit, &hcfg)
        .await
        .map_err(|e| {
            ApiError::service_unavailable(FEED_UNAVAILABLE, format!("在线检索标的失败：{e}"))
        })?;
    for item in &mut results {
        item.registered = state.has_instrument(&item.spec.instrument_id);
    }
    Ok(ApiJson(SearchInstrumentsResponse {
        pack: state.pack_name.clone(),
        query: query.q,
        results,
    }))
}

#[derive(Deserialize)]
#[serde(untagged)]
enum AddInstrumentRequest {
    Wrapped { spec: InstrumentSpec },
    Direct(InstrumentSpec),
}

#[derive(Serialize)]
struct AddInstrumentResponse {
    created: bool,
    instrument: InstrumentSpec,
    instruments: Vec<InstrumentSpec>,
}

fn validate_instrument_spec(spec: &InstrumentSpec) -> Result<(), ApiError> {
    if spec.instrument_id.trim().is_empty()
        || spec.venue.trim().is_empty()
        || spec.symbol.trim().is_empty()
        || spec.asset_class.trim().is_empty()
    {
        return Err(ApiError::validation(
            "标的规格字段 instrument_id、venue、symbol、asset_class 不能为空",
        ));
    }
    if spec.tick_size <= Decimal::ZERO
        || spec.qty_step <= Decimal::ZERO
        || spec.min_qty <= Decimal::ZERO
    {
        return Err(ApiError::validation(
            "tick_size、qty_step、min_qty 必须大于 0",
        ));
    }
    if !(0..=8).contains(&spec.price_precision) || !(0..=8).contains(&spec.qty_precision) {
        return Err(ApiError::validation(
            "price_precision 与 qty_precision 必须在 0..=8 之间",
        ));
    }
    Ok(())
}

async fn add_instrument(
    State(state): State<Arc<AppState>>,
    ApiJson(req): ApiJson<AddInstrumentRequest>,
) -> Result<ApiJson<AddInstrumentResponse>, ApiError> {
    let spec = match req {
        AddInstrumentRequest::Wrapped { spec } => spec,
        AddInstrumentRequest::Direct(spec) => spec,
    };
    validate_instrument_spec(&spec)?;

    let already_exists = state.has_instrument(&spec.instrument_id);
    if !already_exists {
        state.register_instrument_in_memory(spec.clone())?;
        let pack_name = state.pack_name.clone();
        let spec_c = spec.clone();
        state
            .state_store
            .write(move |conn, now| custom_instruments_upsert(conn, &pack_name, &spec_c, now))
            .await?;
        if let Some(cs) = &state.cold_store {
            let cs_c = Arc::clone(cs);
            let spec_c2 = spec.clone();
            let _ = tokio::task::spawn_blocking(move || cs_c.register_instrument_specs(&[spec_c2]))
                .await;
        }
    }

    let current = state
        .find_instrument(&spec.instrument_id)
        .unwrap_or(spec);
    Ok(ApiJson(AddInstrumentResponse {
        created: !already_exists,
        instrument: current,
        instruments: state.instruments_snapshot(),
    }))
}

#[derive(Serialize)]
struct BarsResponse {
    instrument_id: String,
    timeframe: String,
    generation: i64,
    bars: Vec<RangeBar>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RangeBarsQuery {
    instrument: String,
    timeframe: String,
    #[serde(default)]
    start: i64,
    #[serde(default)]
    end: Option<i64>,
    #[serde(default)]
    as_of: Option<i64>,
    #[serde(default)]
    available_since: Option<i64>,
    #[serde(default)]
    resync_from: Option<i64>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    include_open: bool,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    tail_only: bool,
}

impl From<RangeBarsQuery> for BarsQuery {
    fn from(q: RangeBarsQuery) -> Self {
        Self {
            instrument: q.instrument,
            timeframe: q.timeframe,
            start: q.start,
            end: q.end,
            as_of: q.as_of,
            available_since: q.available_since,
            resync_from: q.resync_from,
            include_open: q.include_open,
            tail_only: q.tail_only,
        }
    }
}

async fn get_range_bars(
    State(state): State<Arc<AppState>>,
    ApiQuery(raw_query): ApiQuery<RangeBarsQuery>,
) -> Result<Response, ApiError> {
    let query: BarsQuery = raw_query.into();
    query.validate().map_err(ApiError::from)?;
    let spec = find_instrument(&state, &query.instrument)?;
    let interval = validate_timeframe(&query.timeframe)?;

    let now = state.now_ms();
    let aligned_start = if query.start == 0 {
        0
    } else {
        (query.start + interval - 1) / interval * interval
    };
    let end = query
        .end
        .unwrap_or_else(|| last_closed_bar_start(now, interval));
    let mut history_start = aligned_start;
    if let Some(avail) = query.available_since {
        history_start = history_start.max(last_closed_bar_start(avail, interval));
    }
    if let Some(resync) = query.resync_from {
        history_start = history_start.max((resync + interval - 1) / interval * interval);
    }
    if end >= history_start && ((end - history_start) / interval + 1) as usize > MAX_RANGE_BARS {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            BARS_RANGE_TOO_LARGE,
            RANGE_LIMIT_MESSAGE,
        ));
    }

    let cutoff = query.as_of.map(|a| a.min(now)).unwrap_or(now);
    let history_end = end.min(cutoff - interval);

    let (mut manifest, mut cold, mut hot, mut hot_revision) = {
        let reader = Arc::clone(&state.reader);
        let hot_buffer = Arc::clone(&state.hot_buffer);
        let spec_c = spec.clone();
        let tf = query.timeframe.clone();
        let start = query.start;
        let tail_only = query.tail_only;
        tokio::task::spawn_blocking(move || -> Result<_, ApiError> {
            let pre_rev = if tail_only {
                hot_buffer.series_version(&spec_c.instrument_id, &tf)
            } else {
                "none".to_owned()
            };
            let (m, c) = reader.read_manifest_snapshot(&spec_c, &tf, Some(start), Some(end))?;
            let h = hot_buffer.revisions(&spec_c.instrument_id, &tf, start, Some(end));
            let post_rev = if tail_only {
                let p = hot_buffer.series_version(&spec_c.instrument_id, &tf);
                if p == pre_rev {
                    pre_rev
                } else {
                    BARS_HOT_REVISION_UNSTABLE.to_owned()
                }
            } else {
                "none".to_owned()
            };
            Ok((m, c, h, post_rev))
        })
        .await
        .map_err(|e| ApiError::internal(format!("bars read error: {e}")))??
    };

    // On-demand history backfill when gaps exist and connector + cold_store are available
    let has_rest = state.history_connector.is_some()
        && state.cold_store.is_some()
        && (state.replay_path.is_none() || state.fixture_dir.is_some());
    if (query.resync_from.is_some() || (has_rest && history_start <= history_end)) && has_rest
        && history_start <= history_end
    {
        let mut expected_slots: Vec<i64> = Vec::new();
        let covered: HashSet<i64> = hot
            .iter()
            .filter(|b| b.confirmed && !b.synthetic)
            .map(|b| b.ts_event)
            .chain(cold.iter().filter(|b| b.confirmed).map(|b| b.ts_event))
            .collect();
        let mut cur = history_start;
        while cur <= history_end {
            if query.resync_from.is_some() || !covered.contains(&cur) {
                expected_slots.push(cur);
            }
            cur += interval;
        }
        if !expected_slots.is_empty()
            && let (Some(connector), Some(cold_store)) = (&state.history_connector, &state.cold_store)
        {
            let missing_start = *expected_slots.first().unwrap();
            let missing_end = *expected_slots.last().unwrap();
            let max_pages = if state.is_realtime_instrument(&spec.instrument_id) {
                BackfillWindow::default().max_pages
            } else {
                2
            };
            let res = backfill_bars(
                connector.as_ref(),
                &spec.symbol,
                &query.timeframe,
                BackfillWindow {
                    start_ts: Some(missing_start),
                    end_ts: Some(missing_end + interval),
                    max_pages,
                    ..BackfillWindow::default()
                },
            )
            .await;
            match res {
                Ok(fetched) => {
                    let mut existing: HashMap<i64, u64> = HashMap::new();
                    for b in cold
                        .iter()
                        .chain(hot.iter().filter(|b| b.confirmed && !b.synthetic))
                    {
                        existing
                            .entry(b.ts_event)
                            .and_modify(|rev| *rev = (*rev).max(b.revision))
                            .or_insert(b.revision);
                    }
                    let mut to_publish = Vec::new();
                    for mut bar in fetched {
                        if !bar.confirmed || bar.synthetic {
                            continue;
                        }
                        if let Some(&prev_rev) = existing.get(&bar.ts_event) {
                            if query.resync_from.is_some() {
                                bar.revision = prev_rev + 1;
                                to_publish.push(bar);
                            }
                        } else {
                            to_publish.push(bar);
                        }
                    }
                    if !to_publish.is_empty() {
                        publish_bars_to_cold_store(
                            Arc::clone(cold_store),
                            spec.clone(),
                            query.timeframe.clone(),
                            to_publish,
                        )
                        .await?;
                    }
                }
                Err(err) => {
                    if query.resync_from.is_some() {
                        return Err(ApiError::service_unavailable(
                            FEED_UNAVAILABLE,
                            format!("范围重取失败：{err}"),
                        ));
                    }
                }
            }
            // Reload after backfill
            let reader = Arc::clone(&state.reader);
            let hot_buffer = Arc::clone(&state.hot_buffer);
            let spec_c = spec.clone();
            let tf = query.timeframe.clone();
            let start = query.start;
            let tail_only = query.tail_only;
            let reloaded = tokio::task::spawn_blocking(move || -> Result<_, ApiError> {
                let pre_rev = if tail_only {
                    hot_buffer.series_version(&spec_c.instrument_id, &tf)
                } else {
                    "none".to_owned()
                };
                let (m, c) = reader.read_manifest_snapshot(&spec_c, &tf, Some(start), Some(end))?;
                let h = hot_buffer.revisions(&spec_c.instrument_id, &tf, start, Some(end));
                Ok((m, c, h, pre_rev))
            })
            .await
            .map_err(|e| ApiError::internal(e.to_string()))??;
            manifest = reloaded.0;
            cold = reloaded.1;
            hot = reloaded.2;
            hot_revision = reloaded.3;
        }
    }

    let mut visible = if query.resync_from.is_some() {
        let resync_as_of = query.as_of.map(|_| cutoff).unwrap_or(now);
        let (vis, _) = revision_window(&cold, &hot, 0, Some(resync_as_of));
        let present: HashSet<i64> = vis.iter().map(|b| b.ts_event).collect();
        let mut missing = false;
        let mut cur = history_start;
        while cur <= history_end {
            if !present.contains(&cur) {
                missing = true;
                break;
            }
            cur += interval;
        }
        if missing {
            return Err(ApiError::service_unavailable(
                FEED_UNAVAILABLE,
                "范围重取缺少已确认柱，请重试",
            ));
        }
        vis
    } else if let Some(avail_since) = query.available_since {
        revision_window(&cold, &hot, avail_since, Some(cutoff)).0
    } else {
        let effective_as_of = query.as_of.unwrap_or(now);
        range_visible_bars(&cold, &hot, Some(effective_as_of), query.include_open)
    };

    let fingerprint = if query.tail_only {
        let archived: HashSet<(i64, u64)> =
            cold.iter().map(|b| (b.ts_event, b.revision)).collect();
        visible.retain(|b| !archived.contains(&(b.ts_event, b.revision)));
        Some(manifest_range_fingerprint(&manifest, query.start, Some(end)))
    } else {
        None
    };

    if visible.len() > MAX_RANGE_BARS {
        return Err(ApiError::new(
            StatusCode::BAD_REQUEST,
            BARS_RANGE_TOO_LARGE,
            RANGE_LIMIT_MESSAGE,
        ));
    }

    let bars: Vec<RangeBar> = visible.iter().map(raw_to_range_bar).collect();
    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&state.security.data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    if let Some(fp) = fingerprint {
        if let Ok(v) = HeaderValue::from_str(&fp) {
            headers.insert(BARS_MANIFEST_HEADER, v);
        }
        if let Ok(v) = HeaderValue::from_str(&hot_revision) {
            headers.insert(BARS_HOT_REVISION_HEADER, v);
        }
    }

    Ok((
        StatusCode::OK,
        headers,
        ApiJson(BarsResponse {
            instrument_id: spec.instrument_id,
            timeframe: query.timeframe,
            generation: manifest.version,
            bars,
        }),
    )
        .into_response())
}

use std::collections::HashMap;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BackfillDerivativesQuery {
    instrument: String,
    kind: DerivativeKind,
    start: i64,
    end: i64,
    #[serde(default)]
    as_of: Option<i64>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    refresh: bool,
}

async fn backfill_derivatives_handler(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<BackfillDerivativesQuery>,
) -> Result<Response, ApiError> {
    if q.start < 0 || q.end < 0 {
        return Err(ApiError::validation("start and end must be non-negative"));
    }
    let spec = find_instrument(&state, &q.instrument)?;
    let _ = (q.as_of, q.refresh);

    if let (Some(connector), Some(cold_store)) = (&state.history_connector, &state.cold_store)
        && (state.replay_path.is_none() || state.fixture_dir.is_some())
    {
        let tfs: Vec<Option<&'static str>> = match q.kind {
            DerivativeKind::FundingRate => vec![None],
            DerivativeKind::MarkPrice => SUPPORTED_TIMEFRAMES.iter().copied().map(Some).collect(),
            DerivativeKind::OpenInterest => vec![Some("5m"), Some("15m"), Some("1h"), Some("4h"), Some("1d")],
        };
        for tf in tfs {
            let pts = backfill_derivatives(
                connector.as_ref(),
                &spec.symbol,
                q.kind,
                tf,
                BackfillWindow {
                    start_ts: Some(q.start),
                    end_ts: Some(q.end),
                    ..BackfillWindow::default()
                },
            )
            .await
            .map_err(|e| ApiError::service_unavailable(FEED_UNAVAILABLE, format!("衍生品历史回填失败：{e}")))?;
            if !pts.is_empty() {
                let points = pts
                    .into_iter()
                    .map(|p| p.to_point(&spec.instrument_id))
                    .collect();
                publish_derivatives_to_cold_store(
                    Arc::clone(cold_store),
                    spec.clone(),
                    q.kind,
                    points,
                )
                .await?;
            }
        }
    }

    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&state.security.data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    if let Ok(v) = HeaderValue::from_str(&state.now_ms().to_string()) {
        headers.insert(NOW_MS_HEADER, v);
    }
    Ok((StatusCode::NO_CONTENT, headers).into_response())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct BackfillTradesQuery {
    instrument: String,
    day: NaiveDate,
}

async fn backfill_trades_handler(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<BackfillTradesQuery>,
) -> Result<Response, ApiError> {
    let spec = find_instrument(&state, &q.instrument)?;
    let Some(cold_store) = &state.cold_store else {
        return Err(ApiError::conflict(FEATURE_DISABLED, "冷库未启用"));
    };
    let Some(archive_reader) = state.pack.trade_archive_reader() else {
        return Err(ApiError::service_unavailable(
            FEED_UNAVAILABLE,
            "逐笔归档读取器不可用",
        ));
    };

    let trades = archive_reader
        .fetch_daily_trades(&spec, q.day, state.now_ms())
        .await
        .map_err(|e| ApiError::service_unavailable(FEED_UNAVAILABLE, format!("逐笔历史补数失败：{e}")))?;

    let count = trades.len();
    if !trades.is_empty() {
        let store_c = Arc::clone(cold_store);
        let spec_c = spec.clone();
        tokio::task::spawn_blocking(move || -> Result<(), ApiError> {
            let mut by_day: std::collections::BTreeMap<String, Vec<talaria_contracts::ticks::Trade>> =
                std::collections::BTreeMap::new();
            for t in trades {
                let d = partition_day(t.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                by_day.entry(d).or_default().push(t);
            }
            for (_, batch) in by_day {
                store_c.publish_trades(&spec_c, &batch)?;
            }
            Ok(())
        })
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
    }

    let mut headers = HeaderMap::new();
    if let Ok(v) = HeaderValue::from_str(&state.security.data_dir_id) {
        headers.insert(DATA_DIR_HEADER, v);
    }
    Ok((StatusCode::OK, headers, ApiJson(count)).into_response())
}
