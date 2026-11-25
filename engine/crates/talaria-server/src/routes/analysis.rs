//! Snapshots, operators/indicators, indicator defaults, rule preview, and key levels endpoints.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::{get, post};
use rust_decimal::Decimal;
use serde::Deserialize;
use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::{
    DerivativeBundle, DerivativeKind, DerivativePoint, DerivativeSeries, DerivativeSummary,
    oi_timeframe_for,
};
use talaria_contracts::drawings::{DrawingKind, DrawingPoint, DrawingRecord, DrawingStyle};
use talaria_contracts::error_codes::{NOT_FOUND, SNAPSHOT_NOT_FOUND};
use talaria_contracts::indicators::{
    IndicatorBundle, IndicatorDefaultsResponse, IndicatorName, IndicatorParams, IndicatorSeries,
    OperatorRequest,
};
use talaria_contracts::key_levels::{
    EvidenceClass, KeyLevel, KeyLevelAdoptionRecord, KeyLevelAdoptionRequest, KeyLevelCategory,
    KeyLevelFeedbackSummary, KeyLevelIgnoreList, KeyLevelIgnoreRequest, KeyLevelSet,
    KEY_LEVEL_METHOD_VERSION, SessionProfile, VwapAnchor,
};
use talaria_contracts::rules::{
    RuleExpression, RuleIndicatorName, RuleIndicatorParams, RuleOperand, RulePreviewRequest,
    RulePreviewResponse,
};
use talaria_contracts::snapshot::{ChartPage, SnapshotMeta, SnapshotRequest};
use talaria_contracts::timeframe_ms;
use talaria_operators::{
    DerivativeSample, KeyLevelParams, OperatorSpec, compute_indicator_series,
    compute_key_levels_with_notes, derivative_samples,
};
use uuid::Uuid;

use crate::error::{ApiError, ApiJson, ApiPath, ApiQuery};
use crate::routes::{
    find_instrument, freeze_snapshot, load_confirmed_series, load_derivative_points,
    parse_indicator_name, validate_timeframe,
};
use crate::rules::preview_rule;
use crate::state_store::{
    INDICATOR_DEFAULTS_META_KEY, drawings_create, drawings_get, key_level_get_adoption,
    key_level_list_adoptions, key_level_list_ignores, key_level_record_adoption,
    key_level_set_ignore, load_indicator_defaults_overrides, meta_get, meta_set,
};
use crate::server::AppState;

pub fn router() -> Router<Arc<AppState>> {
    Router::new()
        .route("/api/snapshots", post(create_snapshot))
        .route("/api/snapshots/{snapshot_id}", get(get_snapshot))
        .route("/api/snapshots/{snapshot_id}/bars", get(get_snapshot_bars))
        .route(
            "/api/snapshots/{snapshot_id}/derivatives",
            get(get_snapshot_derivatives),
        )
        .route(
            "/api/snapshots/{snapshot_id}/indicators",
            get(get_snapshot_indicators),
        )
        .route("/api/indicators", get(get_indicators_alias))
        .route("/api/operators/compute", post(compute_operator))
        .route("/api/indicator-defaults", get(get_indicator_defaults))
        .route(
            "/api/indicator-defaults/{name}",
            axum::routing::put(put_indicator_default).delete(delete_indicator_default),
        )
        .route(
            "/api/snapshots/{snapshot_id}/rule-preview",
            post(preview_rule_handler),
        )
        .route(
            "/api/snapshots/{snapshot_id}/key-levels",
            get(get_snapshot_key_levels),
        )
        .route("/api/key-levels", get(get_key_levels_alias))
        .route("/api/key-levels/at", get(get_key_levels_at))
        .route(
            "/api/key-levels/ignored",
            get(list_ignored_key_levels).put(set_ignored_key_level),
        )
        .route("/api/key-levels/{level_id}/adopt", post(adopt_key_level))
        .route("/api/key-levels/feedback", get(key_level_feedback))
}

async fn create_snapshot(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<SnapshotRequest>,
) -> Result<(StatusCode, ApiJson<SnapshotMeta>), ApiError> {
    let meta = freeze_snapshot(state, payload).await?;
    Ok((StatusCode::OK, ApiJson(meta)))
}

async fn get_snapshot(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
) -> Result<ApiJson<SnapshotMeta>, ApiError> {
    let snapshots = Arc::clone(&state.snapshots);
    let meta = tokio::task::spawn_blocking(move || snapshots.get_meta(&snapshot_id))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
    Ok(ApiJson(meta))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SnapshotBarsQuery {
    timeframe: String,
    #[serde(default = "default_300")]
    limit: usize,
    #[serde(default)]
    before_ts: Option<i64>,
}

fn default_300() -> usize {
    300
}

async fn get_snapshot_bars(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<SnapshotBarsQuery>,
) -> Result<ApiJson<ChartPage>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    let limit = q.limit.clamp(1, 1000);
    let snapshots = Arc::clone(&state.snapshots);
    let tf = q.timeframe.clone();
    let before_ts = q.before_ts;

    let page = tokio::task::spawn_blocking(move || -> Result<ChartPage, ApiError> {
        let (meta, all_bars) = snapshots.get_bars(&snapshot_id, &tf)?;
        let total = all_bars.len();
        let filtered: Vec<CompactBar> = match before_ts {
            Some(ts) => all_bars.into_iter().filter(|b| b.timestamp < ts).collect(),
            None => all_bars,
        };
        let has_more_backward = filtered.len() > limit;
        let has_more_forward = before_ts.is_some() && filtered.len() < total;
        let start = filtered.len().saturating_sub(limit);
        let bars = filtered[start..].to_vec();
        Ok(ChartPage {
            snapshot_id,
            instrument_id: meta.instrument_id,
            timeframe: tf,
            bars,
            has_more_forward,
            has_more_backward,
        })
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(page))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct TimeframeQuery {
    timeframe: String,
}

async fn get_snapshot_derivatives(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<TimeframeQuery>,
) -> Result<ApiJson<DerivativeBundle>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    let snapshots = Arc::clone(&state.snapshots);
    let tf = q.timeframe.clone();

    let bundle = tokio::task::spawn_blocking(move || -> Result<DerivativeBundle, ApiError> {
        let (meta, all_points) = snapshots.get_derivatives(&snapshot_id, &tf)?;
        let oi_tf = oi_timeframe_for(&tf);

        let mark_pts: Vec<DerivativePoint> = all_points
            .iter()
            .filter(|p| {
                p.kind == DerivativeKind::MarkPrice
                    && p.timeframe.as_deref().is_none_or(|pt_tf| pt_tf == tf)
            })
            .cloned()
            .collect();
        let funding_pts: Vec<DerivativePoint> = all_points
            .iter()
            .filter(|p| p.kind == DerivativeKind::FundingRate)
            .cloned()
            .collect();
        let oi_pts: Vec<DerivativePoint> = all_points
            .iter()
            .filter(|p| {
                p.kind == DerivativeKind::OpenInterest
                    && p.timeframe
                        .as_deref()
                        .is_none_or(|pt_tf| Some(pt_tf) == oi_tf)
            })
            .cloned()
            .collect();

        let latest_mark = mark_pts.last().cloned();
        let latest_funding = funding_pts
            .iter()
            .rev()
            .find(|p| p.is_funding_settlement())
            .cloned()
            .or_else(|| funding_pts.last().cloned());
        let latest_oi = oi_pts.last().cloned();

        let summary = DerivativeSummary {
            mark_price: latest_mark.as_ref().map(|p| p.value),
            mark_price_ts: latest_mark.as_ref().map(|p| p.ts_event),
            funding_rate: latest_funding.as_ref().map(|p| p.value),
            funding_settled_ts: latest_funding.as_ref().map(|p| p.ts_event),
            next_funding_ts: None,
            open_interest: latest_oi.as_ref().map(|p| p.value),
            open_interest_ts: latest_oi.as_ref().map(|p| p.ts_event),
            oi_history_available: !oi_pts.is_empty(),
        };

        let series = vec![
            DerivativeSeries {
                kind: DerivativeKind::MarkPrice,
                timeframe: Some(tf.clone()),
                points: mark_pts,
            },
            DerivativeSeries {
                kind: DerivativeKind::FundingRate,
                timeframe: None,
                points: funding_pts,
            },
            DerivativeSeries {
                kind: DerivativeKind::OpenInterest,
                timeframe: oi_tf.map(str::to_owned),
                points: oi_pts,
            },
        ];

        Ok(DerivativeBundle {
            snapshot_id,
            instrument_id: meta.instrument_id,
            timeframe: tf,
            cutoff_ts: meta.cutoff_ts,
            series,
            summary,
        })
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(bundle))
}

fn default_indicator_map(
    overrides: &BTreeMap<IndicatorName, IndicatorParams>,
) -> BTreeMap<IndicatorName, IndicatorParams> {
    let all = [
        IndicatorName::Ema,
        IndicatorName::Sma,
        IndicatorName::Rsi,
        IndicatorName::Atr,
        IndicatorName::Boll,
        IndicatorName::Vwap,
        IndicatorName::FundingQuantile,
        IndicatorName::OiDivergence,
        IndicatorName::NetOdds,
    ];
    let mut map = BTreeMap::new();
    for name in all {
        map.insert(
            name,
            overrides
                .get(&name)
                .cloned()
                .unwrap_or_else(|| IndicatorParams::defaults_for(name)),
        );
    }
    map
}

async fn effective_indicator_defaults(
    state: &AppState,
) -> Result<BTreeMap<IndicatorName, IndicatorParams>, ApiError> {
    let overrides = state.state_store.read(load_indicator_defaults_overrides).await?;
    Ok(default_indicator_map(&overrides))
}

async fn get_indicator_defaults(
    State(state): State<Arc<AppState>>,
) -> Result<ApiJson<IndicatorDefaultsResponse>, ApiError> {
    let defaults = effective_indicator_defaults(&state).await?;
    Ok(ApiJson(IndicatorDefaultsResponse { defaults }))
}

async fn put_indicator_default(
    State(state): State<Arc<AppState>>,
    ApiPath(name_str): ApiPath<String>,
    ApiJson(payload): ApiJson<IndicatorParams>,
) -> Result<ApiJson<IndicatorDefaultsResponse>, ApiError> {
    let name = parse_indicator_name(&name_str)?;
    let _ = OperatorSpec::from_params(name, Some(&payload))?;

    let key = serde_json::to_value(name)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| format!("{name:?}").to_uppercase());
    let param_val = serde_json::to_value(&payload).map_err(|e| ApiError::internal(e.to_string()))?;

    state
        .state_store
        .write(move |conn, now| {
            let mut map = match meta_get(conn, INDICATOR_DEFAULTS_META_KEY)? {
                Some(serde_json::Value::Object(obj)) => obj,
                _ => serde_json::Map::new(),
            };
            map.insert(key, param_val);
            meta_set(
                conn,
                INDICATOR_DEFAULTS_META_KEY,
                &serde_json::Value::Object(map),
                now,
            )?;
            Ok(())
        })
        .await?;

    let defaults = effective_indicator_defaults(&state).await?;
    Ok(ApiJson(IndicatorDefaultsResponse { defaults }))
}

async fn delete_indicator_default(
    State(state): State<Arc<AppState>>,
    ApiPath(name_str): ApiPath<String>,
) -> Result<ApiJson<IndicatorDefaultsResponse>, ApiError> {
    let name = parse_indicator_name(&name_str)?;
    let key = serde_json::to_value(name)
        .ok()
        .and_then(|v| v.as_str().map(str::to_owned))
        .unwrap_or_else(|| format!("{name:?}").to_uppercase());

    state
        .state_store
        .write(move |conn, now| {
            if let Some(serde_json::Value::Object(mut obj)) =
                meta_get(conn, INDICATOR_DEFAULTS_META_KEY)?
            {
                obj.remove(&key);
                meta_set(
                    conn,
                    INDICATOR_DEFAULTS_META_KEY,
                    &serde_json::Value::Object(obj),
                    now,
                )?;
            }
            Ok(())
        })
        .await?;

    let defaults = effective_indicator_defaults(&state).await?;
    Ok(ApiJson(IndicatorDefaultsResponse { defaults }))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IndicatorsQuery {
    timeframe: String,
    names: String,
    #[serde(default = "default_300")]
    limit: usize,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    live: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct IndicatorsAliasQuery {
    #[serde(default)]
    snapshot_id: Option<String>,
    #[serde(default)]
    instrument: Option<String>,
    #[serde(default)]
    instrument_id: Option<String>,
    timeframe: String,
    names: String,
    #[serde(default = "default_300")]
    limit: usize,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    live: bool,
    #[serde(default)]
    as_of: Option<i64>,
    #[serde(default)]
    start: Option<i64>,
    #[serde(default)]
    end: Option<i64>,
}

#[allow(clippy::too_many_arguments)]
fn compute_bundle_sync(
    state: &AppState,
    snapshot_id: String,
    instrument_id: String,
    timeframe: &str,
    bars: Vec<CompactBar>,
    derivs: &[DerivativePoint],
    names: &[IndicatorName],
    effective: &BTreeMap<IndicatorName, IndicatorParams>,
    limit: usize,
    live: bool,
) -> Result<IndicatorBundle, ApiError> {
    let mut combined = bars;
    if live && let Some(sup) = &state.supervisor {
        let after_ts = combined.last().map(|b| b.timestamp);
        let extra = sup.recent_bars(&instrument_id, timeframe, after_ts, false);
        combined.extend(extra);
    }

    let tf_ms = timeframe_ms(timeframe)?;
    let funding: Vec<DerivativePoint> = derivs
        .iter()
        .filter(|p| p.kind == DerivativeKind::FundingRate)
        .cloned()
        .collect();
    let oi: Vec<DerivativePoint> = derivs
        .iter()
        .filter(|p| p.kind == DerivativeKind::OpenInterest)
        .cloned()
        .collect();
    let mark: Vec<DerivativePoint> = derivs
        .iter()
        .filter(|p| p.kind == DerivativeKind::MarkPrice)
        .cloned()
        .collect();

    let needs_derivs = names.iter().any(|n| {
        matches!(
            n,
            IndicatorName::FundingQuantile | IndicatorName::OiDivergence | IndicatorName::NetOdds
        )
    });
    let samples: Option<Vec<DerivativeSample>> = if needs_derivs && !combined.is_empty() {
        let as_of: Vec<i64> = combined
            .iter()
            .map(|b| b.ts_available.max(b.timestamp + tf_ms))
            .collect();
        Some(derivative_samples(
            &combined,
            &funding,
            &oi,
            &mark,
            Some(&as_of),
        )?)
    } else {
        None
    };

    let mut series = Vec::with_capacity(names.len());
    for &name in names {
        let params = effective.get(&name);
        let spec = OperatorSpec::from_params(name, params)?;
        let out = compute_indicator_series(&combined, &spec, Some(limit), samples.as_deref())?;
        series.push(out);
    }

    Ok(IndicatorBundle {
        snapshot_id,
        instrument_id,
        timeframe: timeframe.to_owned(),
        series,
    })
}

async fn get_snapshot_indicators(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<IndicatorsQuery>,
) -> Result<ApiJson<IndicatorBundle>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    let limit = q.limit.clamp(1, 5000);
    let mut parsed_names = Vec::new();
    for raw in q.names.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        parsed_names.push(parse_indicator_name(raw)?);
    }
    if parsed_names.is_empty() {
        return Err(ApiError::validation("names 至少要包含一个指标"));
    }

    let effective = effective_indicator_defaults(&state).await?;
    let state_c = Arc::clone(&state);
    let tf = q.timeframe.clone();
    let live = q.live;

    let bundle = tokio::task::spawn_blocking(move || -> Result<IndicatorBundle, ApiError> {
        let (meta, bars) = state_c.snapshots.get_bars(&snapshot_id, &tf)?;
        let (_, derivs) = state_c.snapshots.get_derivatives(&snapshot_id, &tf)?;
        compute_bundle_sync(
            &state_c,
            snapshot_id,
            meta.instrument_id,
            &tf,
            bars,
            &derivs,
            &parsed_names,
            &effective,
            limit,
            live,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(bundle))
}

async fn get_indicators_alias(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<IndicatorsAliasQuery>,
) -> Result<ApiJson<IndicatorBundle>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    if q.as_of.is_some_and(|ts| ts < 0) {
        return Err(ApiError::validation("as_of must be non-negative"));
    }
    if q.start.is_some_and(|ts| ts < 0) {
        return Err(ApiError::validation("start must be non-negative"));
    }
    if q.end.is_some_and(|ts| ts < 0) {
        return Err(ApiError::validation("end must be non-negative"));
    }
    if let (Some(start_ts), Some(end_ts)) = (q.start, q.end)
        && end_ts < start_ts
    {
        return Err(ApiError::validation("end must not precede start"));
    }
    let limit = q.limit.clamp(1, 5000);
    let mut parsed_names = Vec::new();
    for raw in q.names.split(',').map(str::trim).filter(|s| !s.is_empty()) {
        parsed_names.push(parse_indicator_name(raw)?);
    }
    if parsed_names.is_empty() {
        return Err(ApiError::validation("names 至少要包含一个指标"));
    }
    let effective = effective_indicator_defaults(&state).await?;
    let state_c = Arc::clone(&state);
    let tf = q.timeframe.clone();
    let live = q.live;
    let as_of_opt = q.as_of;
    let start_opt = q.start;
    let end_opt = q.end;

    if let Some(snap_id) = q.snapshot_id {
        let bundle = tokio::task::spawn_blocking(move || -> Result<IndicatorBundle, ApiError> {
            let (meta, mut bars) = state_c.snapshots.get_bars(&snap_id, &tf)?;
            let (_, derivs) = state_c.snapshots.get_derivatives(&snap_id, &tf)?;
            if let Some(as_of) = as_of_opt {
                bars.retain(|b| b.ts_available <= as_of);
            }
            if let Some(end_ts) = end_opt {
                bars.retain(|b| b.timestamp <= end_ts);
            }
            let compute_limit = if start_opt.is_some() {
                bars.len().max(1)
            } else {
                limit
            };
            let mut bundle = compute_bundle_sync(
                &state_c,
                snap_id,
                meta.instrument_id,
                &tf,
                bars,
                &derivs,
                &parsed_names,
                &effective,
                compute_limit,
                live && as_of_opt.is_none(),
            )?;
            if start_opt.is_some() || end_opt.is_some() {
                filter_bundle_points_by_range(&mut bundle, start_opt, end_opt, limit);
            }
            Ok(bundle)
        })
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
        return Ok(ApiJson(bundle));
    }

    let inst_id = q
        .instrument_id
        .or(q.instrument)
        .ok_or_else(|| ApiError::validation("snapshot_id or instrument_id is required"))?;
    let spec = find_instrument(&state, &inst_id)?;
    let now = state.now_ms();
    let effective_as_of = as_of_opt.map(|a| a.min(now)).unwrap_or(now);
    let effective_live = live && as_of_opt.is_none();
    let bundle = tokio::task::spawn_blocking(move || -> Result<IndicatorBundle, ApiError> {
        let mut bars = load_confirmed_series(&state_c, &spec.instrument_id, &tf, effective_as_of, 5000)?;
        if let Some(end_ts) = end_opt {
            bars.retain(|b| b.timestamp <= end_ts);
        }
        let (funding, oi, mark) = load_derivative_points(&state_c, &spec, effective_as_of);
        let mut derivs = Vec::with_capacity(funding.len() + oi.len() + mark.len());
        derivs.extend(funding);
        derivs.extend(oi);
        derivs.extend(mark);
        let compute_limit = if start_opt.is_some() {
            bars.len().max(1)
        } else {
            limit
        };
        let mut bundle = compute_bundle_sync(
            &state_c,
            "live".to_owned(),
            spec.instrument_id,
            &tf,
            bars,
            &derivs,
            &parsed_names,
            &effective,
            compute_limit,
            effective_live,
        )?;
        if start_opt.is_some() || end_opt.is_some() {
            filter_bundle_points_by_range(&mut bundle, start_opt, end_opt, limit);
        }
        Ok(bundle)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(bundle))
}

fn filter_bundle_points_by_range(
    bundle: &mut IndicatorBundle,
    start: Option<i64>,
    end: Option<i64>,
    limit: usize,
) {
    for series in &mut bundle.series {
        series.points.retain(|p| {
            start.is_none_or(|s| p.timestamp >= s) && end.is_none_or(|e| p.timestamp <= e)
        });
        if series.points.len() > limit {
            let drain_end = series.points.len() - limit;
            series.points.drain(0..drain_end);
        }
        let latest = series
            .points
            .last()
            .map(|p| p.values.clone())
            .unwrap_or_else(|| {
                series
                    .name
                    .channels()
                    .iter()
                    .map(|c| ((*c).to_owned(), None))
                    .collect()
            });
        let warmup_bars = series
            .points
            .iter()
            .position(|p| p.values.values().all(Option::is_some))
            .unwrap_or(series.points.len());
        let valid_points = series
            .points
            .iter()
            .filter(|p| p.values.values().all(Option::is_some))
            .count();
        series.summary.latest = latest;
        series.summary.warmup_bars = warmup_bars;
        series.summary.valid_points = valid_points;
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct LiveQuery {
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_bool")]
    live: bool,
}

async fn compute_operator(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<LiveQuery>,
    ApiJson(payload): ApiJson<OperatorRequest>,
) -> Result<ApiJson<IndicatorSeries>, ApiError> {
    let _ = validate_timeframe(&payload.timeframe)?;
    let limit = payload.limit.unwrap_or(300).clamp(1, 5000);
    let effective = effective_indicator_defaults(&state).await?;
    let state_c = Arc::clone(&state);
    let live = q.live;

    let series = tokio::task::spawn_blocking(move || -> Result<IndicatorSeries, ApiError> {
        let (meta, mut bars) = state_c
            .snapshots
            .get_bars(&payload.snapshot_id, &payload.timeframe)?;
        let (_, derivs) = state_c
            .snapshots
            .get_derivatives(&payload.snapshot_id, &payload.timeframe)?;
        if live && let Some(sup) = &state_c.supervisor {
            let after_ts = bars.last().map(|b| b.timestamp);
            bars.extend(sup.recent_bars(&meta.instrument_id, &payload.timeframe, after_ts, false));
        }
        let params = payload
            .params
            .as_ref()
            .or_else(|| effective.get(&payload.operator));
        let spec = OperatorSpec::from_params(payload.operator, params)?;
        let tf_ms = timeframe_ms(&payload.timeframe)?;
        let samples = if matches!(
            payload.operator,
            IndicatorName::FundingQuantile | IndicatorName::OiDivergence | IndicatorName::NetOdds
        ) && !bars.is_empty()
        {
            let funding: Vec<DerivativePoint> = derivs
                .iter()
                .filter(|p| p.kind == DerivativeKind::FundingRate)
                .cloned()
                .collect();
            let oi: Vec<DerivativePoint> = derivs
                .iter()
                .filter(|p| p.kind == DerivativeKind::OpenInterest)
                .cloned()
                .collect();
            let mark: Vec<DerivativePoint> = derivs
                .iter()
                .filter(|p| p.kind == DerivativeKind::MarkPrice)
                .cloned()
                .collect();
            let as_of: Vec<i64> = bars
                .iter()
                .map(|b| b.ts_available.max(b.timestamp + tf_ms))
                .collect();
            Some(derivative_samples(&bars, &funding, &oi, &mark, Some(&as_of))?)
        } else {
            None
        };
        Ok(compute_indicator_series(
            &bars,
            &spec,
            Some(limit),
            samples.as_deref(),
        )?)
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(series))
}

async fn preview_rule_handler(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
    ApiJson(mut payload): ApiJson<RulePreviewRequest>,
) -> Result<ApiJson<RulePreviewResponse>, ApiError> {
    let _ = validate_timeframe(&payload.timeframe)?;
    let defaults = effective_indicator_defaults(&state).await?;
    pin_rule_indicator_params(&mut payload.rule.expression, &defaults);

    let meta = {
        let snapshots = Arc::clone(&state.snapshots);
        let sid = snapshot_id.clone();
        tokio::task::spawn_blocking(move || snapshots.get_meta(&sid))
            .await
            .map_err(|e| ApiError::internal(e.to_string()))??
    };
    if !meta.timeframes.iter().any(|t| t == &payload.timeframe) {
        return Err(ApiError::not_found(
            SNAPSHOT_NOT_FOUND,
            format!("快照 {snapshot_id} 不包含周期 {}", payload.timeframe),
        ));
    }

    let mut drawing_ids = Vec::new();
    collect_rule_drawing_ids(&payload.rule.expression, &mut drawing_ids);
    let mut drawings_map: HashMap<String, Vec<DrawingRecord>> = HashMap::new();
    for id in drawing_ids {
        let id_c = id.clone();
        let versions = state
            .state_store
            .read(move |conn| drawings_list_versions(conn, &id_c))
            .await?;
        if let Some(last) = versions.last()
            && last.instrument_id != meta.instrument_id
        {
            return Err(ApiError::validation("画线不属于该标的"));
        }
        drawings_map.insert(id, versions);
    }

    let state_c = Arc::clone(&state);
    let tf = payload.timeframe.clone();
    let rule = payload.rule.clone();
    let limit = payload.limit;
    let sid = snapshot_id.clone();

    let mut response = tokio::task::spawn_blocking(move || -> Result<RulePreviewResponse, ApiError> {
        let (meta, bars) = state_c.snapshots.get_bars(&sid, &tf)?;
        let (_, derivs) = state_c.snapshots.get_derivatives(&sid, &tf)?;
        let funding: Vec<DerivativePoint> = derivs
            .iter()
            .filter(|p| p.kind == DerivativeKind::FundingRate)
            .cloned()
            .collect();
        let oi: Vec<DerivativePoint> = derivs
            .iter()
            .filter(|p| p.kind == DerivativeKind::OpenInterest)
            .cloned()
            .collect();
        let mark: Vec<DerivativePoint> = derivs
            .iter()
            .filter(|p| p.kind == DerivativeKind::MarkPrice)
            .cloned()
            .collect();

        let levels_fn = |cutoff: i64| -> HashMap<String, KeyLevel> {
            state_c
                .key_levels
                .lookup_level_map(&meta.instrument_id, &tf, cutoff)
        };
        let drawing_lookup = |id: &str| -> Option<DrawingRecord> {
            drawings_map
                .get(id)
                .and_then(|vers| vers.last().cloned())
        };

        preview_rule(
            &meta.instrument_id,
            &tf,
            &rule,
            &bars,
            &funding,
            &oi,
            &mark,
            &levels_fn,
            &drawing_lookup,
            limit,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    // Populate unavailable_current_levels if rule references levels
    let mut level_refs = Vec::new();
    collect_rule_level_refs(&response.rule.expression, &mut level_refs);
    if !level_refs.is_empty() {
        let current = state
            .key_levels
            .lookup_level_map(&meta.instrument_id, &payload.timeframe, state.now_ms());
        let mut missing = Vec::new();
        let mut seen = HashSet::new();
        for (id, expected_kind) in level_refs {
            let unavailable = match current.get(&id) {
                None => true,
                Some(lv) => expected_kind.is_some_and(|k| k != lv.kind),
            };
            if unavailable && seen.insert(id.clone()) {
                missing.push(id);
            }
        }
        response.unavailable_current_levels = missing;
    }

    Ok(ApiJson(response))
}

use crate::state_store::drawings_list_versions;

fn pin_rule_indicator_params(
    expr: &mut RuleExpression,
    defaults: &BTreeMap<IndicatorName, IndicatorParams>,
) {
    match expr {
        RuleExpression::Compare { left, right, .. } => {
            pin_operand(left, defaults);
            pin_operand(right, defaults);
        }
        RuleExpression::All { args }
        | RuleExpression::Any { args }
        | RuleExpression::Not { args } => {
            for child in args {
                pin_rule_indicator_params(child, defaults);
            }
        }
        RuleExpression::Overlay { .. } | RuleExpression::LevelEvent { .. } => {}
    }
}

fn pin_operand(
    op: &mut RuleOperand,
    defaults: &BTreeMap<IndicatorName, IndicatorParams>,
) {
    if let RuleOperand::Indicator { name, params, .. } = op
        && !matches!(
            name,
            RuleIndicatorName::LegacyEma | RuleIndicatorName::LegacyAtr
        )
        && let Ok((canonical, _, _)) = rule_indicator_to_spec(*name, params)
        && let Some(def) = defaults.get(&canonical)
    {
        *params = RuleIndicatorParams {
            period: Some(params.period.unwrap_or(def.period)),
            anchor: params.anchor.or(def.anchor),
            std_dev: params.std_dev.or(def.std_dev),
        };
    }
}

use crate::rules::rule_indicator_to_spec;

pub(crate) fn collect_rule_drawing_ids(expr: &RuleExpression, out: &mut Vec<String>) {
    match expr {
        RuleExpression::Overlay { drawing_id, .. } => out.push(drawing_id.clone()),
        RuleExpression::Compare { left, right, .. } => {
            if let RuleOperand::DrawingPrice { drawing_id, .. } = left.as_ref() {
                out.push(drawing_id.clone());
            }
            if let RuleOperand::DrawingPrice { drawing_id, .. } = right.as_ref() {
                out.push(drawing_id.clone());
            }
        }
        RuleExpression::All { args }
        | RuleExpression::Any { args }
        | RuleExpression::Not { args } => {
            for child in args {
                collect_rule_drawing_ids(child, out);
            }
        }
        RuleExpression::LevelEvent { .. } => {}
    }
}

fn collect_rule_level_refs(
    expr: &RuleExpression,
    out: &mut Vec<(String, Option<talaria_contracts::key_levels::KeyLevelKind>)>,
) {
    match expr {
        RuleExpression::LevelEvent {
            level_id,
            level_kind,
            ..
        } => out.push((level_id.clone(), *level_kind)),
        RuleExpression::Compare { left, right, .. } => {
            if let RuleOperand::LevelPrice {
                level_id,
                level_kind,
                ..
            } = left.as_ref()
            {
                out.push((level_id.clone(), *level_kind));
            }
            if let RuleOperand::LevelPrice {
                level_id,
                level_kind,
                ..
            } = right.as_ref()
            {
                out.push((level_id.clone(), *level_kind));
            }
        }
        RuleExpression::All { args }
        | RuleExpression::Any { args }
        | RuleExpression::Not { args } => {
            for child in args {
                collect_rule_level_refs(child, out);
            }
        }
        RuleExpression::Overlay { .. } => {}
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyLevelsQuery {
    timeframe: String,
    #[serde(default)]
    categories: Option<String>,
    #[serde(default)]
    min_strength: f64,
    #[serde(default = "default_order")]
    order: String,
    #[serde(default = "default_200")]
    limit: usize,
    #[serde(default = "default_all")]
    evidence_class: String,
    #[serde(default = "default_session")]
    vwap_anchor: String,
    #[serde(default)]
    vwap_anchor_ts: Option<i64>,
    #[serde(default = "default_value_area")]
    value_area_ratio: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyLevelsAliasQuery {
    #[serde(default)]
    snapshot_id: Option<String>,
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default)]
    instrument: Option<String>,
    timeframe: String,
    #[serde(default)]
    as_of: Option<i64>,
    #[serde(default)]
    categories: Option<String>,
    #[serde(default)]
    min_strength: f64,
    #[serde(default = "default_order")]
    order: String,
    #[serde(default = "default_200")]
    limit: usize,
    #[serde(default = "default_all")]
    evidence_class: String,
    #[serde(default = "default_session")]
    vwap_anchor: String,
    #[serde(default)]
    vwap_anchor_ts: Option<i64>,
    #[serde(default = "default_value_area")]
    value_area_ratio: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct KeyLevelsAtQuery {
    #[serde(default)]
    instrument_id: Option<String>,
    #[serde(default)]
    instrument: Option<String>,
    timeframe: String,
    as_of: i64,
    #[serde(default)]
    categories: Option<String>,
    #[serde(default)]
    min_strength: f64,
    #[serde(default = "default_order")]
    order: String,
    #[serde(default = "default_200")]
    limit: usize,
    #[serde(default = "default_all")]
    evidence_class: String,
    #[serde(default = "default_session")]
    vwap_anchor: String,
    #[serde(default)]
    vwap_anchor_ts: Option<i64>,
    #[serde(default = "default_value_area")]
    value_area_ratio: f64,
}

fn default_order() -> String {
    "strength_desc".to_owned()
}
fn default_200() -> usize {
    200
}
fn default_all() -> String {
    "ALL".to_owned()
}
fn default_session() -> String {
    "session".to_owned()
}
fn default_value_area() -> f64 {
    0.70
}

fn parse_key_level_filters(
    categories: Option<&str>,
    min_strength: f64,
    evidence_class: &str,
    vwap_anchor: &str,
    vwap_anchor_ts: Option<i64>,
    value_area_ratio: f64,
) -> Result<(HashSet<KeyLevelCategory>, Option<EvidenceClass>, KeyLevelParams), ApiError> {
    if !(0.0..=1.0).contains(&min_strength) {
        return Err(ApiError::validation("min_strength must be in [0, 1]"));
    }
    if !(0.0 < value_area_ratio && value_area_ratio <= 1.0) {
        return Err(ApiError::validation("value_area_ratio must be in (0, 1]"));
    }
    let mut parsed_cats = HashSet::new();
    if let Some(raw) = categories {
        for tok in raw.split(',').map(str::trim).filter(|s| !s.is_empty()) {
            let quoted = serde_json::to_string(&tok.to_uppercase()).unwrap_or_default();
            let cat: KeyLevelCategory = serde_json::from_str(&quoted)
                .map_err(|_| ApiError::validation(format!("未知的关键位类别：{tok}")))?;
            parsed_cats.insert(cat);
        }
    }
    let ev_token = evidence_class.trim().to_uppercase();
    let evidence = match ev_token.as_str() {
        "ALL" => None,
        "OBSERVED" => Some(EvidenceClass::Observed),
        "ESTIMATED" => Some(EvidenceClass::Estimated),
        _ => {
            return Err(ApiError::validation(format!(
                "未知的证据类别：{evidence_class}"
            )));
        }
    };
    let anchor = match vwap_anchor.trim().to_uppercase().as_str() {
        "SNAPSHOT" => VwapAnchor::Snapshot,
        "SESSION" => VwapAnchor::Session,
        "TIMESTAMP" => VwapAnchor::Timestamp,
        _ => {
            return Err(ApiError::validation(format!(
                "未知的锚定 VWAP 锚点：{vwap_anchor}"
            )));
        }
    };
    if anchor == VwapAnchor::Timestamp && vwap_anchor_ts.is_none() {
        return Err(ApiError::validation("vwap_anchor=timestamp 时必须提供 vwap_anchor_ts"));
    }

    let mut kl_params = KeyLevelParams::default();
    kl_params.vwap_anchor = anchor;
    kl_params.vwap_anchor_ts = vwap_anchor_ts;
    kl_params.value_area_ratio =
        Decimal::from_f64_retain((value_area_ratio * 10000.0).round() / 10000.0)
            .unwrap_or(kl_params.value_area_ratio);
    kl_params.validate()?;

    Ok((parsed_cats, evidence, kl_params))
}

#[allow(clippy::too_many_arguments)]
fn compute_snapshot_key_levels(
    state: &AppState,
    snapshot_id: &str,
    timeframe: &str,
    categories: &HashSet<KeyLevelCategory>,
    min_strength: f64,
    order: &str,
    limit: usize,
    evidence: Option<EvidenceClass>,
    kl_params: &KeyLevelParams,
) -> Result<KeyLevelSet, ApiError> {
    let (meta, bars) = state.snapshots.get_bars(snapshot_id, timeframe)?;
    let (_, derivs) = state.snapshots.get_derivatives(snapshot_id, timeframe)?;
    let session_profile = state
        .pack
        .traits(&meta.instrument_id)
        .ok()
        .map(|t| SessionProfile::from(&t.session));

    let computation = compute_key_levels_with_notes(
        &meta.instrument_id,
        timeframe,
        &bars,
        meta.cutoff_ts,
        session_profile.as_ref(),
        Some(kl_params),
        &derivs,
    )?;

    let mut levels = computation.levels;
    filter_and_sort_levels(&mut levels, categories, min_strength, evidence, order, limit);

    Ok(KeyLevelSet {
        snapshot_id: snapshot_id.to_owned(),
        instrument_id: meta.instrument_id,
        timeframe: timeframe.to_owned(),
        cutoff_ts: meta.cutoff_ts,
        as_of_ts: meta.cutoff_ts,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters: kl_params.as_parameters()?,
        notes: computation.notes,
        levels,
    })
}

fn filter_and_sort_levels(
    levels: &mut Vec<KeyLevel>,
    categories: &HashSet<KeyLevelCategory>,
    min_strength: f64,
    evidence: Option<EvidenceClass>,
    order: &str,
    limit: usize,
) {
    levels.retain(|lv| {
        (categories.is_empty() || categories.contains(&lv.category))
            && lv.strength >= min_strength
            && evidence.is_none_or(|e| lv.evidence_class == e)
    });
    match order {
        "price_asc" => levels.sort_by(|a, b| {
            let pa = a.price.or(a.price_low).unwrap_or(Decimal::ZERO);
            let pb = b.price.or(b.price_low).unwrap_or(Decimal::ZERO);
            pa.cmp(&pb)
        }),
        "price_desc" => levels.sort_by(|a, b| {
            let pa = a.price.or(a.price_high).unwrap_or(Decimal::ZERO);
            let pb = b.price.or(b.price_high).unwrap_or(Decimal::ZERO);
            pb.cmp(&pa)
        }),
        "recent_desc" => levels.sort_by_key(|a| std::cmp::Reverse(a.ts_available)),
        _ => levels.sort_by(|a, b| {
            b.strength
                .total_cmp(&a.strength)
                .then_with(|| b.ts_available.cmp(&a.ts_available))
                .then_with(|| a.level_id.cmp(&b.level_id))
        }),
    }
    levels.truncate(limit.clamp(1, 500));
}

async fn get_snapshot_key_levels(
    State(state): State<Arc<AppState>>,
    ApiPath(snapshot_id): ApiPath<String>,
    ApiQuery(q): ApiQuery<KeyLevelsQuery>,
) -> Result<ApiJson<KeyLevelSet>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    let (cats, evidence, kl_params) = parse_key_level_filters(
        q.categories.as_deref(),
        q.min_strength,
        &q.evidence_class,
        &q.vwap_anchor,
        q.vwap_anchor_ts,
        q.value_area_ratio,
    )?;
    let state_c = Arc::clone(&state);
    let tf = q.timeframe.clone();
    let order = q.order.clone();
    let limit = q.limit;

    let set = tokio::task::spawn_blocking(move || {
        compute_snapshot_key_levels(
            &state_c,
            &snapshot_id,
            &tf,
            &cats,
            q.min_strength,
            &order,
            limit,
            evidence,
            &kl_params,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    Ok(ApiJson(set))
}

async fn get_key_levels_alias(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<KeyLevelsAliasQuery>,
) -> Result<ApiJson<KeyLevelSet>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    let (cats, evidence, kl_params) = parse_key_level_filters(
        q.categories.as_deref(),
        q.min_strength,
        &q.evidence_class,
        &q.vwap_anchor,
        q.vwap_anchor_ts,
        q.value_area_ratio,
    )?;

    if let Some(snap_id) = q.snapshot_id {
        let state_c = Arc::clone(&state);
        let tf = q.timeframe.clone();
        let order = q.order.clone();
        let limit = q.limit;
        let set = tokio::task::spawn_blocking(move || {
            compute_snapshot_key_levels(
                &state_c,
                &snap_id,
                &tf,
                &cats,
                q.min_strength,
                &order,
                limit,
                evidence,
                &kl_params,
            )
        })
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
        return Ok(ApiJson(set));
    }

    let inst_id = q
        .instrument_id
        .or(q.instrument)
        .ok_or_else(|| ApiError::validation("snapshot_id or instrument_id is required"))?;
    if let Some(as_of) = q.as_of {
        if as_of < 0 {
            return Err(ApiError::validation("as_of must be non-negative"));
        }
        let state_c = Arc::clone(&state);
        let tf = q.timeframe.clone();
        let order = q.order.clone();
        let limit = q.limit;
        let set = tokio::task::spawn_blocking(move || {
            compute_key_levels_at_sync(
                &state_c,
                &inst_id,
                &tf,
                as_of,
                &cats,
                q.min_strength,
                &order,
                limit,
                evidence,
                &kl_params,
            )
        })
        .await
        .map_err(|e| ApiError::internal(e.to_string()))??;
        return Ok(ApiJson(set));
    }
    let spec = find_instrument(&state, &inst_id)?;
    let mut level_set = state
        .key_levels
        .latest(&spec.instrument_id, &q.timeframe)
        .unwrap_or_else(|| KeyLevelSet {
            snapshot_id: "registry".to_owned(),
            instrument_id: spec.instrument_id.clone(),
            timeframe: q.timeframe.clone(),
            cutoff_ts: state.now_ms(),
            as_of_ts: state.now_ms(),
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: BTreeMap::new(),
            notes: Vec::new(),
            levels: Vec::new(),
        });
    filter_and_sort_levels(
        &mut level_set.levels,
        &cats,
        q.min_strength,
        evidence,
        &q.order,
        q.limit,
    );
    Ok(ApiJson(level_set))
}

#[allow(clippy::too_many_arguments)]
fn compute_key_levels_at_sync(
    state: &AppState,
    instrument_id: &str,
    timeframe: &str,
    as_of: i64,
    categories: &HashSet<KeyLevelCategory>,
    min_strength: f64,
    order: &str,
    limit: usize,
    evidence: Option<EvidenceClass>,
    kl_params: &KeyLevelParams,
) -> Result<KeyLevelSet, ApiError> {
    let spec = find_instrument(state, instrument_id)?;
    let effective_as_of = as_of.min(state.now_ms());
    let bars = load_confirmed_series(state, &spec.instrument_id, timeframe, effective_as_of, 5000)?;
    let (funding, oi, mark) = load_derivative_points(state, &spec, effective_as_of);
    let mut derivs = Vec::with_capacity(funding.len() + oi.len() + mark.len());
    derivs.extend(funding);
    derivs.extend(oi);
    derivs.extend(mark);
    let session_profile = state
        .pack
        .traits(&spec.instrument_id)
        .ok()
        .map(|t| SessionProfile::from(&t.session));

    let computation = compute_key_levels_with_notes(
        &spec.instrument_id,
        timeframe,
        &bars,
        effective_as_of,
        session_profile.as_ref(),
        Some(kl_params),
        &derivs,
    )?;

    let mut levels = computation.levels;
    filter_and_sort_levels(&mut levels, categories, min_strength, evidence, order, limit);

    Ok(KeyLevelSet {
        snapshot_id: "at".to_owned(),
        instrument_id: spec.instrument_id,
        timeframe: timeframe.to_owned(),
        cutoff_ts: effective_as_of,
        as_of_ts: effective_as_of,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters: kl_params.as_parameters()?,
        notes: computation.notes,
        levels,
    })
}

async fn get_key_levels_at(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<KeyLevelsAtQuery>,
) -> Result<ApiJson<KeyLevelSet>, ApiError> {
    let _ = validate_timeframe(&q.timeframe)?;
    if q.as_of < 0 {
        return Err(ApiError::validation("as_of must be non-negative"));
    }
    let inst_id = q
        .instrument_id
        .or(q.instrument)
        .ok_or_else(|| ApiError::validation("instrument or instrument_id is required"))?;
    let (cats, evidence, kl_params) = parse_key_level_filters(
        q.categories.as_deref(),
        q.min_strength,
        &q.evidence_class,
        &q.vwap_anchor,
        q.vwap_anchor_ts,
        q.value_area_ratio,
    )?;
    let state_c = Arc::clone(&state);
    let tf = q.timeframe.clone();
    let order = q.order.clone();
    let limit = q.limit;
    let as_of = q.as_of;
    let set = tokio::task::spawn_blocking(move || {
        compute_key_levels_at_sync(
            &state_c,
            &inst_id,
            &tf,
            as_of,
            &cats,
            q.min_strength,
            &order,
            limit,
            evidence,
            &kl_params,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;
    Ok(ApiJson(set))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct InstrumentQuery {
    instrument_id: String,
}

async fn list_ignored_key_levels(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<InstrumentQuery>,
) -> Result<ApiJson<KeyLevelIgnoreList>, ApiError> {
    let _ = find_instrument(&state, &q.instrument_id)?;
    let inst = q.instrument_id.clone();
    let entries = state
        .state_store
        .read(move |conn| key_level_list_ignores(conn, &inst))
        .await?;
    Ok(ApiJson(KeyLevelIgnoreList {
        instrument_id: q.instrument_id,
        entries,
    }))
}

async fn set_ignored_key_level(
    State(state): State<Arc<AppState>>,
    ApiJson(payload): ApiJson<KeyLevelIgnoreRequest>,
) -> Result<ApiJson<KeyLevelIgnoreList>, ApiError> {
    let _ = find_instrument(&state, &payload.instrument_id)?;
    let inst = payload.instrument_id.clone();
    let inst_read = inst.clone();
    let entries = state
        .state_store
        .write(move |conn, now| {
            key_level_set_ignore(conn, &inst, &payload.level_id, payload.ignored, now)?;
            key_level_list_ignores(conn, &inst)
        })
        .await?;
    Ok(ApiJson(KeyLevelIgnoreList {
        instrument_id: inst_read,
        entries,
    }))
}

async fn adopt_key_level(
    State(state): State<Arc<AppState>>,
    ApiPath(level_id): ApiPath<String>,
    ApiJson(payload): ApiJson<KeyLevelAdoptionRequest>,
) -> Result<ApiJson<DrawingRecord>, ApiError> {
    let _ = validate_timeframe(&payload.timeframe)?;
    let state_c = Arc::clone(&state);
    let snap_id = payload.snapshot_id.clone();
    let tf = payload.timeframe.clone();
    let kl_params = KeyLevelParams::default();

    let level_set = tokio::task::spawn_blocking(move || {
        compute_snapshot_key_levels(
            &state_c,
            &snap_id,
            &tf,
            &HashSet::new(),
            0.0,
            "strength_desc",
            500,
            None,
            &kl_params,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))??;

    let level = level_set
        .levels
        .into_iter()
        .find(|lv| lv.level_id == level_id)
        .ok_or_else(|| ApiError::not_found(NOT_FOUND, format!("关键位 {level_id} 不存在")))?;

    let inst_id = level.instrument_id.clone();
    let lvl_id = level.level_id.clone();
    let snap_id = payload.snapshot_id.clone();
    let tf = payload.timeframe.clone();

    let (kind, points) = if let Some(price) = level.price {
        (
            DrawingKind::HorizontalLine,
            vec![DrawingPoint {
                ts: level.formed_ts.max(0),
                price,
            }],
        )
    } else if let (Some(low), Some(high)) = (level.price_low, level.price_high) {
        (
            DrawingKind::Rectangle,
            vec![
                DrawingPoint {
                    ts: level.formed_ts.max(0),
                    price: low,
                },
                DrawingPoint {
                    ts: level.ts_available.max(level.formed_ts).max(0),
                    price: high,
                },
            ],
        )
    } else {
        return Err(ApiError::validation("关键位缺少价格坐标"));
    };

    let record = state
        .state_store
        .write(move |conn, now| {
            if let Some(existing) = key_level_get_adoption(conn, &inst_id, &lvl_id)?
                && let Ok(drw) = drawings_get(conn, &existing.drawing_id, None)
                && drw.deleted_at.is_none()
            {
                return Ok(drw);
            }
            let drw = DrawingRecord {
                drawing_id: format!("drw_{}", Uuid::new_v4().simple()),
                instrument_id: inst_id.clone(),
                kind,
                visible_timeframes: vec![],
                points,
                text: format!("{:?}", level.kind),
                style: DrawingStyle::default(),
                version: 1,
                created_ts: now,
                updated_ts: now,
                deleted_at: None,
            };
            drawings_create(conn, &drw)?;
            key_level_record_adoption(
                conn,
                &KeyLevelAdoptionRecord {
                    instrument_id: inst_id,
                    level_id: lvl_id,
                    snapshot_id: snap_id,
                    drawing_id: drw.drawing_id.clone(),
                    adopted_ts: now,
                    timeframe: tf,
                },
            )?;
            Ok(drw)
        })
        .await?;

    state.alerts.refresh_drawings(std::slice::from_ref(&record));
    Ok(ApiJson(record))
}

async fn key_level_feedback(
    State(state): State<Arc<AppState>>,
    ApiQuery(q): ApiQuery<InstrumentQuery>,
) -> Result<ApiJson<KeyLevelFeedbackSummary>, ApiError> {
    let _ = find_instrument(&state, &q.instrument_id)?;
    let inst = q.instrument_id.clone();
    let (adopted_level_ids, ignored_level_ids) = state
        .state_store
        .read(move |conn| {
            let adoptions = key_level_list_adoptions(conn, &inst)?;
            let ignores = key_level_list_ignores(conn, &inst)?;
            Ok((
                adoptions.into_iter().map(|a| a.level_id).collect(),
                ignores
                    .into_iter()
                    .filter(|i| i.ignored)
                    .map(|i| i.level_id)
                    .collect(),
            ))
        })
        .await?;

    Ok(ApiJson(KeyLevelFeedbackSummary {
        instrument_id: q.instrument_id,
        adopted_level_ids,
        ignored_level_ids,
    }))
}
