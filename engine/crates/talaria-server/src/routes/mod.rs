//! HTTP route handlers organized by domain:
//! - `arrow`: Apache Arrow IPC streaming (`/api/bars.arrow`, `/api/ticks.arrow`, `/api/derivatives.arrow`)
//! - `bars`: Health, instruments, JSON range bars, and history backfill endpoints
//! - `analysis`: Snapshots, operators/indicators, rule preview, key levels & feedback
//! - `state`: Watchlist, drawings, alerts, scanner/briefings, annotations, collaboration
//! - `desk_static`: Same-origin `/desk` SPA bootstrap and `ServeDir` static assets

pub mod analysis;
pub mod arrow;
pub mod bars;
pub mod desk_static;
pub mod state;

use std::collections::{BTreeMap, HashSet};
use std::sync::Arc;

use axum::Router;
use sha2::{Digest, Sha256};
use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint, oi_timeframe_for};
use talaria_contracts::error_codes::{INSTRUMENT_NOT_FOUND, SNAPSHOT_NOT_FOUND, VALIDATION_ERROR};
use talaria_contracts::feed::FeedHealthStatus;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::snapshot::{SnapshotMeta, SnapshotMode, SnapshotRequest};
use talaria_contracts::{SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms};
use talaria_feed::compact_series;
use talaria_packs::{
    BackfillWindow, HistoryConnector, MarketPack, backfill_bars, backfill_derivatives,
};
use talaria_store::{ColdManifest, ColdStore, partition_month, range_visible_bars};
use uuid::Uuid;

use crate::error::ApiError;
use crate::server::AppState;
use crate::snapshot_store::FrozenSnapshot;

pub fn api_router(state: std::sync::Arc<AppState>) -> Router<std::sync::Arc<AppState>> {
    Router::new()
        .merge(arrow::router())
        .merge(bars::router())
        .merge(analysis::router())
        .merge(state::router())
        .merge(desk_static::router(state))
}

pub fn manifest_range_fingerprint(
    manifest: &ColdManifest,
    start_ts: i64,
    end_ts: Option<i64>,
) -> String {
    let entries: Vec<(String, &str)> = manifest
        .generations
        .iter()
        .filter(|g| g.last_ts >= start_ts && end_ts.is_none_or(|e| g.first_ts <= e))
        .map(|g| {
            (
                g.parquet_path.to_string_lossy().replace('\\', "/"),
                g.sha256.as_str(),
            )
        })
        .collect();
    let encoded = serde_json::to_string(&entries).unwrap_or_else(|_| "[]".to_owned());
    hex::encode(Sha256::digest(encoded.as_bytes()))
}

pub fn find_instrument(state: &AppState, instrument_id: &str) -> Result<InstrumentSpec, ApiError> {
    state
        .instruments
        .iter()
        .find(|s| s.instrument_id == instrument_id)
        .cloned()
        .ok_or_else(|| {
            ApiError::not_found(INSTRUMENT_NOT_FOUND, format!("未知标的：{instrument_id}"))
        })
}

pub fn validate_timeframe(timeframe: &str) -> Result<i64, ApiError> {
    if !SUPPORTED_TIMEFRAMES.contains(&timeframe) {
        return Err(ApiError::validation(format!("不支持的周期：{timeframe}")));
    }
    timeframe_ms(timeframe).map_err(|e| ApiError::validation(e.to_string()))
}

pub fn raw_bars_to_compact(rows: &[RawBar], include_open: bool) -> Vec<CompactBar> {
    let mut out = Vec::with_capacity(rows.len());
    for raw in rows {
        if !include_open && !raw.confirmed {
            continue;
        }
        let status = talaria_contracts::bars::classify_flat_bar(
            raw.open,
            raw.high,
            raw.low,
            raw.close,
            raw.volume,
        );
        if let Ok(compact) = raw.to_compact(status) {
            out.push(compact);
        }
    }
    out
}

pub async fn publish_bars_to_cold_store(
    store: Arc<ColdStore>,
    spec: InstrumentSpec,
    timeframe: String,
    bars: Vec<RawBar>,
) -> Result<(), ApiError> {
    if bars.is_empty() {
        return Ok(());
    }
    tokio::task::spawn_blocking(move || -> Result<(), ApiError> {
        let mut by_month: BTreeMap<String, Vec<RawBar>> = BTreeMap::new();
        for b in bars {
            let m = partition_month(b.ts_event).unwrap_or_else(|_| "unknown".to_owned());
            by_month.entry(m).or_default().push(b);
        }
        for (_, mut batch) in by_month {
            batch.sort_by_key(|b| (b.ts_event, b.revision));
            store.publish_bars(&spec, &timeframe, &batch)?;
        }
        Ok(())
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?
}

pub async fn publish_derivatives_to_cold_store(
    store: Arc<ColdStore>,
    spec: InstrumentSpec,
    kind: DerivativeKind,
    points: Vec<DerivativePoint>,
) -> Result<(), ApiError> {
    if points.is_empty() {
        return Ok(());
    }
    tokio::task::spawn_blocking(move || -> Result<(), ApiError> {
        let mut by_month: BTreeMap<String, Vec<DerivativePoint>> = BTreeMap::new();
        for p in points {
            let m = partition_month(p.ts_event).unwrap_or_else(|_| "unknown".to_owned());
            by_month.entry(m).or_default().push(p);
        }
        for (_, mut batch) in by_month {
            batch.sort_by_key(|r| (r.ts_event, r.revision));
            store.publish_derivatives(&spec, kind, &batch)?;
        }
        Ok(())
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?
}

const SNAPSHOT_HISTORY_TARGET_BARS: usize = 1500;

/// Ensure history via `history_connector` (when enabled) and freeze a deterministic PIT snapshot.
pub async fn freeze_snapshot(
    state: Arc<AppState>,
    req: SnapshotRequest,
) -> Result<SnapshotMeta, ApiError> {
    if req.mode == SnapshotMode::ObservedReplay {
        return Err(ApiError::not_implemented(
            "OBSERVED_REPLAY snapshots are not enabled on talaria-data yet",
        ));
    }
    let spec = find_instrument(&state, &req.instrument_id)?;
    if req.timeframes.is_empty() {
        return Err(ApiError::validation("timeframes must not be empty"));
    }
    for tf in &req.timeframes {
        let _ = validate_timeframe(tf)?;
    }

    let now = state.now_ms();
    let cutoff_ts = req.cutoff_ts.unwrap_or(now).min(now);

    if let (Some(connector), Some(cold_store)) = (&state.history_connector, &state.cold_store)
        && (state.replay_path.is_none() || state.fixture_dir.is_some())
    {
        for tf in &req.timeframes {
            let interval = timeframe_ms(tf).map_err(|e| ApiError::validation(e.to_string()))?;
            let closed_end = last_closed_bar_start(cutoff_ts, interval);
            let reader = Arc::clone(&state.reader);
            let hot_buffer = Arc::clone(&state.hot_buffer);
            let spec_c = spec.clone();
            let tf_c = tf.clone();
            let (cold, hot) = tokio::task::spawn_blocking(move || -> Result<_, ApiError> {
                let c = reader.read_revisions(&spec_c, &tf_c, None, Some(closed_end))?;
                let h = hot_buffer.revisions(&spec_c.instrument_id, &tf_c, 0, Some(closed_end));
                Ok((c, h))
            })
            .await
            .map_err(|e| ApiError::internal(e.to_string()))??;

            let visible = range_visible_bars(&cold, &hot, Some(cutoff_ts), false);
            if visible.len() < SNAPSHOT_HISTORY_TARGET_BARS {
                let end_ts = visible
                    .first()
                    .map(|b| b.ts_event.saturating_sub(1))
                    .unwrap_or(cutoff_ts);
                let res = backfill_bars(
                    connector.as_ref(),
                    &spec.symbol,
                    tf,
                    BackfillWindow {
                        start_ts: None,
                        end_ts: Some(end_ts),
                        target_count: SNAPSHOT_HISTORY_TARGET_BARS - visible.len(),
                        max_pages: 3,
                        page_limit: 1000,
                    },
                )
                .await;

                match res {
                    Ok(fetched) => {
                        let existing_ts: HashSet<i64> = cold
                            .iter()
                            .chain(hot.iter())
                            .map(|b| b.ts_event)
                            .collect();
                        let to_publish: Vec<RawBar> = fetched
                            .into_iter()
                            .filter(|b| {
                                b.confirmed
                                    && b.ts_available <= cutoff_ts
                                    && !existing_ts.contains(&b.ts_event)
                            })
                            .collect();
                        publish_bars_to_cold_store(
                            Arc::clone(cold_store),
                            spec.clone(),
                            tf.clone(),
                            to_publish,
                        )
                        .await?;
                    }
                    Err(err) => {
                        let has_sup_bars = state
                            .supervisor
                            .as_ref()
                            .is_some_and(|s| !s.recent_bars(&spec.instrument_id, tf, None, true).is_empty());
                        if visible.is_empty() && !has_sup_bars {
                            return Err(ApiError::from(err));
                        }
                        tracing::warn!(
                            instrument = %spec.instrument_id,
                            timeframe = %tf,
                            error = %err,
                            "snapshot history backfill failed; continuing with local history"
                        );
                    }
                }
            }
        }

        // Best-effort derivative backfill for snapshots when local cold derivatives are empty
        let state_c = Arc::clone(&state);
        let spec_c = spec.clone();
        let (funding, oi, mark) =
            tokio::task::spawn_blocking(move || load_derivative_points(&state_c, &spec_c, cutoff_ts))
                .await
                .map_err(|e| ApiError::internal(e.to_string()))?;
        if funding.is_empty() && oi.is_empty() && mark.is_empty() {
            let mut targets: Vec<(DerivativeKind, Option<&str>)> =
                vec![(DerivativeKind::FundingRate, None)];
            for tf in &req.timeframes {
                targets.push((DerivativeKind::MarkPrice, Some(tf.as_str())));
                if let Some(oi_tf) = oi_timeframe_for(tf) {
                    targets.push((DerivativeKind::OpenInterest, Some(oi_tf)));
                }
            }
            for (kind, d_tf) in targets {
                if let Ok(pts) = backfill_derivatives(
                    connector.as_ref(),
                    &spec.symbol,
                    kind,
                    d_tf,
                    BackfillWindow {
                        start_ts: None,
                        end_ts: Some(cutoff_ts),
                        target_count: 500,
                        max_pages: 2,
                        page_limit: 500,
                    },
                )
                .await
                {
                    let points: Vec<DerivativePoint> = pts
                        .into_iter()
                        .filter(|p| p.ts_available <= cutoff_ts)
                        .map(|p| p.to_point(&spec.instrument_id))
                        .collect();
                    let _ = publish_derivatives_to_cold_store(
                        Arc::clone(cold_store),
                        spec.clone(),
                        kind,
                        points,
                    )
                    .await;
                }
            }
        }
    }

    let state_c = Arc::clone(&state);
    tokio::task::spawn_blocking(move || freeze_snapshot_sync(&state_c, &req))
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?
}

/// Freeze a deterministic PIT snapshot into `SnapshotStore` and register its `KeyLevelSet`.
pub fn freeze_snapshot_sync(
    state: &AppState,
    req: &SnapshotRequest,
) -> Result<SnapshotMeta, ApiError> {
    if req.mode == SnapshotMode::ObservedReplay {
        return Err(ApiError::not_implemented(
            "OBSERVED_REPLAY snapshots are not enabled on talaria-data yet",
        ));
    }
    let spec = find_instrument(state, &req.instrument_id)?;
    if req.timeframes.is_empty() {
        return Err(ApiError::validation("timeframes must not be empty"));
    }
    for tf in &req.timeframes {
        let _ = validate_timeframe(tf)?;
    }

    let now = state.now_ms();
    let raw_cutoff = req.cutoff_ts.unwrap_or(now);
    let cutoff_ts = raw_cutoff.min(now);

    let mut frames: BTreeMap<String, Vec<CompactBar>> = BTreeMap::new();
    let mut bar_counts: BTreeMap<String, usize> = BTreeMap::new();

    for tf in &req.timeframes {
        let interval = timeframe_ms(tf).map_err(|e| ApiError::validation(e.to_string()))?;
        let closed_end = last_closed_bar_start(cutoff_ts, interval);
        let cold = state
            .reader
            .read_revisions(&spec, tf, None, Some(closed_end))
            .map_err(ApiError::from)?;
        let hot = state
            .hot_buffer
            .revisions(&spec.instrument_id, tf, 0, Some(closed_end));
        let mut visible = range_visible_bars(&cold, &hot, Some(cutoff_ts), false);
        let compact = if !visible.is_empty() {
            let tail_start = visible.len().saturating_sub(SNAPSHOT_HISTORY_TARGET_BARS);
            if tail_start > 0 {
                visible.drain(0..tail_start);
            }
            compact_series(&visible, tf, cutoff_ts)?
        } else if let Some(sup) = &state.supervisor {
            sup.recent_bars(&spec.instrument_id, tf, None, true)
                .into_iter()
                .filter(|b| b.timestamp <= closed_end && b.ts_available <= cutoff_ts)
                .collect()
        } else {
            Vec::new()
        };
        bar_counts.insert(tf.clone(), compact.len());
        frames.insert(tf.clone(), compact);
    }

    let total_bars: usize = bar_counts.values().sum();
    if total_bars == 0 && !state.features.market_data {
        return Err(ApiError::conflict(
            talaria_contracts::error_codes::FEED_UNAVAILABLE,
            "行情采集已关闭，且缺少本地历史",
        ));
    }

    let mut derivatives: Vec<DerivativePoint> = Vec::new();
    for kind in [
        talaria_contracts::derivatives::DerivativeKind::MarkPrice,
        talaria_contracts::derivatives::DerivativeKind::FundingRate,
        talaria_contracts::derivatives::DerivativeKind::OpenInterest,
    ] {
        if let Ok(pts) = state
            .reader
            .read_derivatives(&spec, kind, None, Some(cutoff_ts), Some(cutoff_ts), false)
        {
            derivatives.extend(pts);
        }
    }

    let snapshot_id = format!("snp_{}", Uuid::new_v4().simple());
    let meta = SnapshotMeta {
        snapshot_id: snapshot_id.clone(),
        instrument_id: spec.instrument_id.clone(),
        timeframes: req.timeframes.clone(),
        cutoff_ts,
        created_ts: now,
        mode: SnapshotMode::HistoricalPit,
        bar_counts,
    };

    let frozen = FrozenSnapshot {
        meta: meta.clone(),
        frames: frames.clone(),
        derivatives: derivatives.clone(),
    };
    state.snapshots.save(&frozen)?;

    let session_profile = state
        .pack
        .traits(&spec.instrument_id)
        .ok()
        .map(|t| talaria_contracts::key_levels::SessionProfile::from(&t.session));

    for (tf, bars) in &frames {
        if bars.iter().any(|b| b.status != BarStatus::FeedGap) {
            let _ = state.key_levels.compute_and_register(
                snapshot_id.clone(),
                &spec.instrument_id,
                tf,
                bars,
                cutoff_ts,
                now,
                session_profile.as_ref(),
                None,
                &derivatives,
            );
        }
    }

    Ok(meta)
}

pub fn load_confirmed_series(
    state: &AppState,
    instrument_id: &str,
    timeframe: &str,
    cutoff_ts: i64,
    limit: usize,
) -> Result<Vec<CompactBar>, ApiError> {
    let spec = find_instrument(state, instrument_id)?;
    let cold = state
        .reader
        .read_revisions(&spec, timeframe, None, Some(cutoff_ts))?;
    let hot = state
        .hot_buffer
        .revisions(instrument_id, timeframe, 0, Some(cutoff_ts));
    let mut visible = range_visible_bars(&cold, &hot, Some(cutoff_ts), false);
    if visible.len() > limit {
        visible.drain(0..visible.len() - limit);
    }
    let mut compact = raw_bars_to_compact(&visible, false);
    if compact.is_empty()
        && let Some(sup) = &state.supervisor
    {
        compact = sup
            .recent_bars(instrument_id, timeframe, None, true)
            .into_iter()
            .filter(|b| b.ts_available <= cutoff_ts)
            .collect();
        if compact.len() > limit {
            compact.drain(0..compact.len() - limit);
        }
    }
    Ok(compact)
}

pub fn load_derivative_points(
    state: &AppState,
    instrument: &InstrumentSpec,
    as_of: i64,
) -> (Vec<DerivativePoint>, Vec<DerivativePoint>, Vec<DerivativePoint>) {
    let funding = state
        .reader
        .read_derivatives(
            instrument,
            talaria_contracts::derivatives::DerivativeKind::FundingRate,
            None,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    let oi = state
        .reader
        .read_derivatives(
            instrument,
            talaria_contracts::derivatives::DerivativeKind::OpenInterest,
            None,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    let mark = state
        .reader
        .read_derivatives(
            instrument,
            talaria_contracts::derivatives::DerivativeKind::MarkPrice,
            None,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    (funding, oi, mark)
}

pub fn parse_indicator_name(raw: &str) -> Result<talaria_contracts::IndicatorName, ApiError> {
    let trimmed = raw.trim().to_uppercase();
    let quoted = serde_json::to_string(&trimmed).unwrap_or_default();
    serde_json::from_str(&quoted)
        .map_err(|_| ApiError::validation(format!("未知指标：{raw}")))
}

#[allow(dead_code)]
fn _unused_markers(
    _p: &dyn MarketPack,
    _c: &dyn HistoryConnector,
    _f: FeedHealthStatus,
    _w: BackfillWindow,
) {
    let _ = (backfill_bars, SNAPSHOT_NOT_FOUND, VALIDATION_ERROR);
}
