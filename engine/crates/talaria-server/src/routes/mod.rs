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
pub mod paper_accounts;
pub mod paper_commands;
pub mod paper_drafts;
pub mod paper_sizing;
pub mod runs;
pub mod state;
pub mod ticks;

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use axum::Router;
use sha2::{Digest, Sha256};
use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::derivatives::{
    DerivativeKind, DerivativePoint, latest_visible_derivatives, oi_timeframe_for,
};
use talaria_contracts::error_codes::{INSTRUMENT_NOT_FOUND, SNAPSHOT_NOT_FOUND, VALIDATION_ERROR};
use talaria_contracts::feed::FeedHealthStatus;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::snapshot::{SnapshotMeta, SnapshotMode, SnapshotRequest};
use talaria_contracts::{SUPPORTED_TIMEFRAMES, last_closed_bar_start, timeframe_ms};
use talaria_feed::{FeedSupervisor, compact_series};
use talaria_packs::{
    BackfillWindow, HistoryConnector, MarketPack, backfill_bars, backfill_derivatives,
};
use talaria_store::{
    ColdManifest, ColdReader, ColdStore, HotStreamBuffer, partition_month, range_visible_bars,
};
use uuid::Uuid;

use crate::error::ApiError;
use crate::key_levels::KeyLevelRegistry;
use crate::server::{AppState, BackfillCachedError, BackfillCooldownEntry};
use crate::snapshot_store::{FrozenSnapshot, SnapshotStore};
use crate::structures::StructureRegistry;

pub fn api_router(state: std::sync::Arc<AppState>) -> Router<std::sync::Arc<AppState>> {
    Router::new()
        .merge(arrow::router())
        .merge(bars::router())
        .merge(analysis::router())
        .merge(ticks::router())
        .merge(runs::router())
        .merge(state::router())
        .merge(paper_accounts::router())
        .merge(paper_commands::router())
        .merge(paper_drafts::router())
        .merge(paper_sizing::router())
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
    state.find_instrument(instrument_id).ok_or_else(|| {
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
            store.publish_derivatives_reconciled(&spec, kind, &batch)?;
        }
        Ok(())
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?
}

const SNAPSHOT_HISTORY_TARGET_BARS: usize = 1500;
const SNAPSHOT_DERIV_LOOKBACK_PADDING_MS: i64 = 24 * 60 * 60 * 1000;
const SNAPSHOT_DERIV_DEFAULT_WINDOW_MS: i64 = 7 * 24 * 60 * 60 * 1000;
const BACKFILL_COOLDOWN_MS: i64 = 60_000;

struct WindowedBars {
    cold: Vec<RawBar>,
    hot: Vec<RawBar>,
    visible: Vec<RawBar>,
}

fn snapshot_bar_read_start(closed_end: i64, interval: i64) -> i64 {
    closed_end.saturating_sub(interval.saturating_mul((SNAPSHOT_HISTORY_TARGET_BARS as i64) * 2))
}

fn read_snapshot_window_bars(
    state: &AppState,
    spec: &InstrumentSpec,
    tf: &str,
    closed_end: i64,
    cutoff_ts: i64,
    interval: i64,
) -> Result<WindowedBars, ApiError> {
    read_snapshot_window_bars_from(
        &state.reader,
        &state.hot_buffer,
        spec,
        tf,
        closed_end,
        cutoff_ts,
        interval,
    )
}

fn read_snapshot_window_bars_from(
    reader: &ColdReader,
    hot_buffer: &HotStreamBuffer,
    spec: &InstrumentSpec,
    tf: &str,
    closed_end: i64,
    cutoff_ts: i64,
    interval: i64,
) -> Result<WindowedBars, ApiError> {
    let window_start = snapshot_bar_read_start(closed_end, interval).max(0);
    let cold = reader
        .read_revisions(spec, tf, Some(window_start), Some(closed_end))
        .map_err(ApiError::from)?;
    let hot = hot_buffer.revisions(&spec.instrument_id, tf, window_start, Some(closed_end));
    let visible = range_visible_bars(&cold, &hot, Some(cutoff_ts), false);
    if visible.len() >= SNAPSHOT_HISTORY_TARGET_BARS || window_start == 0 {
        return Ok(WindowedBars { cold, hot, visible });
    }
    // Bounded window had fewer than target bars (e.g. market holidays, sparse fixtures, or sparse hot data);
    // widen to full history if older cold generations OR older hot bars exist before `window_start`.
    let has_older_cold = reader
        .generations(spec, tf)
        .map(|gens| gens.iter().any(|g| g.first_ts < window_start))
        .unwrap_or(false);
    let has_older_hot = hot_buffer.has_older_than(&spec.instrument_id, tf, window_start);
    if !has_older_cold && !has_older_hot {
        return Ok(WindowedBars { cold, hot, visible });
    }
    let full_cold = reader
        .read_revisions(spec, tf, None, Some(closed_end))
        .map_err(ApiError::from)?;
    let full_hot = hot_buffer.revisions(&spec.instrument_id, tf, 0, Some(closed_end));
    let full_visible = range_visible_bars(&full_cold, &full_hot, Some(cutoff_ts), false);
    Ok(WindowedBars {
        cold: full_cold,
        hot: full_hot,
        visible: full_visible,
    })
}

fn get_backfill_cooldown(state: &AppState, key: &str, now: i64) -> Option<BackfillCooldownEntry> {
    if state.fixture_dir.is_some() {
        return None;
    }
    let mut map = state.backfill_cooldown.lock().ok()?;
    if let Some(entry) = map.get(key) {
        if now < entry.until_ms {
            return Some(entry.clone());
        }
        map.remove(key);
    }
    None
}

fn mark_backfill_cooldown(
    state: &AppState,
    key: String,
    now: i64,
    last_error: Option<BackfillCachedError>,
) {
    if state.fixture_dir.is_some() {
        return;
    }
    if let Ok(mut map) = state.backfill_cooldown.lock() {
        map.retain(|_, entry| now < entry.until_ms);
        map.insert(
            key,
            BackfillCooldownEntry {
                until_ms: now.saturating_add(BACKFILL_COOLDOWN_MS),
                last_error,
            },
        );
    }
}

pub struct FreezeContext<'a> {
    pub instruments: &'a [InstrumentSpec],
    pub pack: &'a Arc<dyn MarketPack>,
    pub reader: &'a ColdReader,
    pub hot_buffer: &'a Arc<HotStreamBuffer>,
    pub snapshots: &'a Arc<SnapshotStore>,
    pub key_levels: &'a Arc<KeyLevelRegistry>,
    pub structures: &'a Arc<StructureRegistry>,
    pub supervisor: Option<&'a Arc<FeedSupervisor>>,
    pub market_data_enabled: bool,
    pub now_ms: i64,
}

/// Locate the missing span from the right-most recent gap (or tail behind `closed_end`)
/// within the snapshot lookback window so snapshots always stay current on the right edge.
fn find_right_missing_range(
    visible: &[RawBar],
    closed_end: i64,
    interval: i64,
    is_24x7: bool,
) -> Option<(i64, i64)> {
    let last_ts = visible.last()?.ts_event;
    let recent_floor = closed_end
        .saturating_sub(
            interval.saturating_mul((SNAPSHOT_HISTORY_TARGET_BARS as i64).saturating_sub(1)),
        )
        .max(0);
    let tail_gap = (last_ts < closed_end)
        .then(|| ((last_ts + interval).max(recent_floor), closed_end));

    let internal_gap = if is_24x7 {
        visible
            .windows(2)
            .rev()
            .take_while(|pair| pair[1].ts_event >= recent_floor)
            .find_map(|pair| {
                let diff = pair[1].ts_event - pair[0].ts_event;
                if diff > interval {
                    let start = (pair[0].ts_event + interval).max(recent_floor);
                    let end = pair[1].ts_event - interval;
                    (start <= end).then_some((start, end))
                } else {
                    None
                }
            })
    } else {
        None
    };

    match (internal_gap, tail_gap) {
        (Some((g_start, _)), Some((_, t_end))) => {
            if ((t_end - g_start) / interval + 1) as usize <= SNAPSHOT_HISTORY_TARGET_BARS {
                Some((g_start, t_end))
            } else {
                tail_gap
            }
        }
        _ => tail_gap.or(internal_gap),
    }
}

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

    let mut preloaded_bars: HashMap<String, Vec<RawBar>> = HashMap::new();
    let mut preloaded_derivs: Option<Vec<DerivativePoint>> = None;

    if let (Some(connector), Some(cold_store)) = (&state.history_connector, &state.cold_store)
        && (state.replay_path.is_none() || state.fixture_dir.is_some())
    {
        let mut earliest_bar_ts: Option<i64> = None;
        let is_24x7 = state
            .pack
            .traits(&spec.instrument_id)
            .map(|t| t.session.session_kind == "24x7")
            .unwrap_or(false);

        for tf in &req.timeframes {
            let interval = timeframe_ms(tf).map_err(|e| ApiError::validation(e.to_string()))?;
            let closed_end = last_closed_bar_start(cutoff_ts, interval);
            let state_c = Arc::clone(&state);
            let spec_c = spec.clone();
            let tf_c = tf.clone();
            let WindowedBars {
                mut cold,
                mut hot,
                mut visible,
            } = tokio::task::spawn_blocking(move || {
                read_snapshot_window_bars(&state_c, &spec_c, &tf_c, closed_end, cutoff_ts, interval)
            })
            .await
            .map_err(|e| ApiError::internal(e.to_string()))??;

            // Phase 1: Fill right-edge tail gap (and recent internal gaps for 24x7 venues)
            // even when cold store already holds SNAPSHOT_HISTORY_TARGET_BARS older bars.
            if let Some((missing_start, missing_end)) =
                find_right_missing_range(&visible, closed_end, interval, is_24x7)
                && missing_start <= missing_end
            {
                let tail_cooldown_key = format!(
                    "bars-tail:{}:{tf}:{missing_start}:{missing_end}",
                    spec.instrument_id
                );
                if get_backfill_cooldown(&state, &tail_cooldown_key, now).is_none() {
                    let missing_count = (((missing_end - missing_start) / interval) + 1).max(1) as usize;
                    let res = backfill_bars(
                        connector.as_ref(),
                        &spec.symbol,
                        tf,
                        BackfillWindow {
                            start_ts: Some(missing_start),
                            end_ts: Some(missing_end),
                            target_count: missing_count
                                .saturating_add(1)
                                .min(SNAPSHOT_HISTORY_TARGET_BARS),
                            max_pages: 5,
                            page_limit: 1000,
                        },
                    )
                    .await;
                    let mut did_tail_publish = false;
                    match res {
                        Ok(fetched) => {
                            let existing_confirmed_ts: HashSet<i64> = cold
                                .iter()
                                .chain(hot.iter().filter(|b| b.confirmed && !b.synthetic))
                                .map(|b| b.ts_event)
                                .collect();
                            let to_publish: Vec<RawBar> = fetched
                                .into_iter()
                                .filter(|b| {
                                    b.confirmed
                                        && !b.synthetic
                                        && b.ts_available <= cutoff_ts
                                        && !existing_confirmed_ts.contains(&b.ts_event)
                                })
                                .collect();
                            if to_publish.is_empty() {
                                mark_backfill_cooldown(&state, tail_cooldown_key, now, None);
                            } else {
                                did_tail_publish = true;
                                publish_bars_to_cold_store(
                                    Arc::clone(cold_store),
                                    spec.clone(),
                                    tf.clone(),
                                    to_publish,
                                )
                                .await?;
                            }
                        }
                        Err(err) => {
                            let api_err = ApiError::from(err);
                            mark_backfill_cooldown(
                                &state,
                                tail_cooldown_key,
                                now,
                                Some(BackfillCachedError {
                                    status: api_err.status,
                                    code: api_err.code,
                                    message: api_err.message.clone(),
                                }),
                            );
                            tracing::warn!(
                                instrument = %spec.instrument_id,
                                timeframe = %tf,
                                error = %api_err,
                                "snapshot right-tail backfill failed; continuing with local history"
                            );
                        }
                    }
                    if did_tail_publish {
                        let state_c = Arc::clone(&state);
                        let spec_c = spec.clone();
                        let tf_c = tf.clone();
                        let reloaded = tokio::task::spawn_blocking(move || {
                            read_snapshot_window_bars(
                                &state_c,
                                &spec_c,
                                &tf_c,
                                closed_end,
                                cutoff_ts,
                                interval,
                            )
                        })
                        .await
                        .map_err(|e| ApiError::internal(e.to_string()))??;
                        cold = reloaded.cold;
                        hot = reloaded.hot;
                        visible = reloaded.visible;
                    }
                }
            }

            // Phase 2: Ensure left-edge history depth when total visible bars < target.
            let mut did_publish = false;
            let end_ts = visible
                .first()
                .map(|b| b.ts_event.saturating_sub(1))
                .unwrap_or(cutoff_ts);
            let cooldown_end = visible
                .first()
                .map(|b| b.ts_event.saturating_sub(1))
                .unwrap_or(closed_end);
            let bar_cooldown_key = format!("bars:{}:{tf}:{cooldown_end}", spec.instrument_id);
            let has_sup_bars = state
                .supervisor
                .as_ref()
                .is_some_and(|s| !s.recent_bars(&spec.instrument_id, tf, None, true).is_empty());

            if visible.len() < SNAPSHOT_HISTORY_TARGET_BARS {
                if let Some(cooldown) = get_backfill_cooldown(&state, &bar_cooldown_key, now) {
                    if visible.is_empty()
                        && !has_sup_bars
                        && let Some(cached) = cooldown.last_error
                    {
                        return Err(ApiError::new(cached.status, cached.code, cached.message));
                    }
                } else {
                    let res = backfill_bars(
                        connector.as_ref(),
                        &spec.symbol,
                        tf,
                        BackfillWindow {
                            start_ts: None,
                            end_ts: Some(end_ts),
                            target_count: SNAPSHOT_HISTORY_TARGET_BARS - visible.len(),
                            max_pages: 5,
                            page_limit: 1000,
                        },
                    )
                    .await;

                    match res {
                        Ok(fetched) => {
                            let existing_ts: HashSet<i64> = cold
                                .iter()
                                .chain(hot.iter().filter(|b| b.confirmed && !b.synthetic))
                                .map(|b| b.ts_event)
                                .collect();
                            let to_publish: Vec<RawBar> = fetched
                                .into_iter()
                                .filter(|b| {
                                    b.confirmed
                                        && !b.synthetic
                                        && b.ts_available <= cutoff_ts
                                        && !existing_ts.contains(&b.ts_event)
                                })
                                .collect();
                            if to_publish.is_empty() {
                                mark_backfill_cooldown(&state, bar_cooldown_key, now, None);
                            } else {
                                did_publish = true;
                                publish_bars_to_cold_store(
                                    Arc::clone(cold_store),
                                    spec.clone(),
                                    tf.clone(),
                                    to_publish,
                                )
                                .await?;
                            }
                        }
                        Err(err) => {
                            let api_err = ApiError::from(err);
                            mark_backfill_cooldown(
                                &state,
                                bar_cooldown_key,
                                now,
                                Some(BackfillCachedError {
                                    status: api_err.status,
                                    code: api_err.code,
                                    message: api_err.message.clone(),
                                }),
                            );
                            if visible.is_empty() && !has_sup_bars {
                                return Err(api_err);
                            }
                            tracing::warn!(
                                instrument = %spec.instrument_id,
                                timeframe = %tf,
                                error = %api_err,
                                "snapshot history backfill failed; continuing with local history"
                            );
                        }
                    }
                }
            }

            if did_publish {
                let state_c = Arc::clone(&state);
                let spec_c = spec.clone();
                let tf_c = tf.clone();
                visible = tokio::task::spawn_blocking(move || {
                    read_snapshot_window_bars(
                        &state_c,
                        &spec_c,
                        &tf_c,
                        closed_end,
                        cutoff_ts,
                        interval,
                    )
                })
                .await
                .map_err(|e| ApiError::internal(e.to_string()))??
                .visible;
            }

            let tail_start = visible.len().saturating_sub(SNAPSHOT_HISTORY_TARGET_BARS);
            if let Some(first_bar) = visible.get(tail_start) {
                earliest_bar_ts = Some(
                    earliest_bar_ts.map_or(first_bar.ts_event, |cur| cur.min(first_bar.ts_event)),
                );
            }
            preloaded_bars.insert(tf.clone(), visible);
        }

        // Best-effort derivative backfill for snapshots when local cold derivatives in window are empty
        let deriv_start = Some(
            earliest_bar_ts
                .map(|ts| ts.saturating_sub(SNAPSHOT_DERIV_LOOKBACK_PADDING_MS))
                .unwrap_or_else(|| cutoff_ts.saturating_sub(SNAPSHOT_DERIV_DEFAULT_WINDOW_MS)),
        );
        let state_c = Arc::clone(&state);
        let spec_c = spec.clone();
        let (funding, oi, mark) = tokio::task::spawn_blocking(move || {
            load_derivative_points_since(&state_c, &spec_c, deriv_start, cutoff_ts)
        })
        .await
        .map_err(|e| ApiError::internal(e.to_string()))?;

        let deriv_cooldown_key = format!("derivs:{}:{}", spec.instrument_id, cutoff_ts / 60_000);
        if funding.is_empty()
            && oi.is_empty()
            && mark.is_empty()
            && get_backfill_cooldown(&state, &deriv_cooldown_key, now).is_none()
        {
            let mut targets: Vec<(DerivativeKind, Option<&str>)> =
                vec![(DerivativeKind::FundingRate, None)];
            for tf in &req.timeframes {
                targets.push((DerivativeKind::MarkPrice, Some(tf.as_str())));
                if let Some(oi_tf) = oi_timeframe_for(tf) {
                    targets.push((DerivativeKind::OpenInterest, Some(oi_tf)));
                }
            }
            let mut published_any_deriv = false;
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
                    if !points.is_empty()
                        && publish_derivatives_to_cold_store(
                            Arc::clone(cold_store),
                            spec.clone(),
                            kind,
                            points,
                        )
                        .await
                        .is_ok()
                    {
                        published_any_deriv = true;
                    }
                }
            }
            if !published_any_deriv {
                mark_backfill_cooldown(&state, deriv_cooldown_key, now, None);
            }
        }

        let state_c = Arc::clone(&state);
        let spec_c = spec.clone();
        let tfs_c = req.timeframes.clone();
        preloaded_derivs = Some(
            tokio::task::spawn_blocking(move || {
                load_snapshot_derivatives(
                    &state_c.reader,
                    &spec_c,
                    &tfs_c,
                    earliest_bar_ts,
                    deriv_start,
                    cutoff_ts,
                )
            })
            .await
            .map_err(|e| ApiError::internal(e.to_string()))?,
        );
    }

    let mut pinned_req = req;
    pinned_req.cutoff_ts = Some(cutoff_ts);

    let state_c = Arc::clone(&state);
    tokio::task::spawn_blocking(move || {
        let instruments = state_c.instruments_snapshot();
        let ctx = FreezeContext {
            instruments: &instruments,
            pack: &state_c.pack,
            reader: &state_c.reader,
            hot_buffer: &state_c.hot_buffer,
            snapshots: &state_c.snapshots,
            key_levels: &state_c.key_levels,
            structures: &state_c.structures,
            supervisor: state_c.supervisor.as_ref(),
            market_data_enabled: state_c.features.market_data,
            now_ms: now,
        };
        freeze_snapshot_with_context(
            &ctx,
            &pinned_req,
            Some(preloaded_bars),
            preloaded_derivs,
            &[],
            false,
        )
    })
    .await
    .map_err(|e| ApiError::internal(e.to_string()))?
}

/// Freeze a deterministic PIT snapshot into `SnapshotStore` and register its `KeyLevelSet` and `StructureSet`.
pub fn freeze_snapshot_sync(
    state: &AppState,
    req: &SnapshotRequest,
) -> Result<SnapshotMeta, ApiError> {
    let instruments = state.instruments_snapshot();
    let ctx = FreezeContext {
        instruments: &instruments,
        pack: &state.pack,
        reader: &state.reader,
        hot_buffer: &state.hot_buffer,
        snapshots: &state.snapshots,
        key_levels: &state.key_levels,
        structures: &state.structures,
        supervisor: state.supervisor.as_ref(),
        market_data_enabled: state.features.market_data,
        now_ms: state.now_ms(),
    };
    freeze_snapshot_with_context(&ctx, req, None, None, &[], false)
}

pub fn freeze_snapshot_with_context(
    ctx: &FreezeContext<'_>,
    req: &SnapshotRequest,
    mut preloaded_bars: Option<HashMap<String, Vec<RawBar>>>,
    preloaded_derivs: Option<Vec<DerivativePoint>>,
    extra_derivatives: &[DerivativePoint],
    allow_empty_bars_with_derivatives: bool,
) -> Result<SnapshotMeta, ApiError> {
    if req.mode == SnapshotMode::ObservedReplay {
        return Err(ApiError::not_implemented(
            "OBSERVED_REPLAY snapshots are not enabled on talaria-data yet",
        ));
    }
    let spec = ctx
        .instruments
        .iter()
        .find(|s| s.instrument_id == req.instrument_id)
        .cloned()
        .ok_or_else(|| {
            ApiError::not_found(
                INSTRUMENT_NOT_FOUND,
                format!("未知标的：{}", req.instrument_id),
            )
        })?;
    if req.timeframes.is_empty() {
        return Err(ApiError::validation("timeframes must not be empty"));
    }
    for tf in &req.timeframes {
        let _ = validate_timeframe(tf)?;
    }

    let now = ctx.now_ms;
    let raw_cutoff = req.cutoff_ts.unwrap_or(now);
    let cutoff_ts = raw_cutoff.min(now);

    let mut frames: BTreeMap<String, Vec<CompactBar>> = BTreeMap::new();
    let mut bar_counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut earliest_bar_ts: Option<i64> = None;

    for tf in &req.timeframes {
        let interval = timeframe_ms(tf).map_err(|e| ApiError::validation(e.to_string()))?;
        let closed_end = last_closed_bar_start(cutoff_ts, interval);
        let mut visible = match preloaded_bars.as_mut().and_then(|m| m.remove(tf)) {
            Some(v) => v,
            None => read_snapshot_window_bars_from(
                ctx.reader,
                ctx.hot_buffer,
                &spec,
                tf,
                closed_end,
                cutoff_ts,
                interval,
            )?
            .visible,
        };
        let compact = if !visible.is_empty() {
            let tail_start = visible.len().saturating_sub(SNAPSHOT_HISTORY_TARGET_BARS);
            if tail_start > 0 {
                visible.drain(0..tail_start);
            }
            if let Some(first_bar) = visible.first() {
                earliest_bar_ts = Some(
                    earliest_bar_ts.map_or(first_bar.ts_event, |cur| cur.min(first_bar.ts_event)),
                );
            }
            compact_series(&visible, tf, cutoff_ts)?
        } else if let Some(sup) = ctx.supervisor {
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

    let mut derivatives = match preloaded_derivs {
        Some(pts) => pts,
        None => {
            let deriv_start = earliest_bar_ts
                .map(|ts| ts.saturating_sub(SNAPSHOT_DERIV_LOOKBACK_PADDING_MS))
                .or_else(|| {
                    allow_empty_bars_with_derivatives
                        .then(|| cutoff_ts.saturating_sub(SNAPSHOT_DERIV_DEFAULT_WINDOW_MS))
                });
            load_snapshot_derivatives(
                ctx.reader,
                &spec,
                &req.timeframes,
                earliest_bar_ts,
                deriv_start,
                cutoff_ts,
            )
        }
    };
    if !extra_derivatives.is_empty() {
        derivatives.extend(
            extra_derivatives
                .iter()
                .filter(|pt| pt.instrument_id == spec.instrument_id && pt.ts_available <= cutoff_ts)
                .cloned(),
        );
        derivatives = latest_visible_derivatives(derivatives, Some(cutoff_ts));
    }

    let total_bars: usize = bar_counts.values().sum();
    if total_bars == 0 {
        if allow_empty_bars_with_derivatives && !derivatives.is_empty() {
            // Derivative/price alert before first closed bar still produces an auditable evidence snapshot.
        } else if !ctx.market_data_enabled {
            return Err(ApiError::conflict(
                talaria_contracts::error_codes::FEED_UNAVAILABLE,
                "行情采集已关闭，且缺少本地历史",
            ));
        } else if allow_empty_bars_with_derivatives {
            return Err(ApiError::feed_unavailable("no bars or derivatives for snapshot"));
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
    ctx.snapshots.save(&frozen)?;

    let session_profile = ctx
        .pack
        .traits(&spec.instrument_id)
        .ok()
        .map(|t| talaria_contracts::key_levels::SessionProfile::from(&t.session));

    for (tf, bars) in &frames {
        if bars.iter().any(|b| b.status != BarStatus::FeedGap) {
            let _ = ctx.key_levels.compute_and_register(
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
            let _ = ctx.structures.compute_and_register(
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
    load_derivative_points_since(state, instrument, None, as_of)
}

fn load_derivative_points_since(
    state: &AppState,
    instrument: &InstrumentSpec,
    start_ts: Option<i64>,
    as_of: i64,
) -> (Vec<DerivativePoint>, Vec<DerivativePoint>, Vec<DerivativePoint>) {
    load_derivative_points_since_reader(&state.reader, instrument, start_ts, as_of)
}

fn load_derivative_points_since_reader(
    reader: &ColdReader,
    instrument: &InstrumentSpec,
    start_ts: Option<i64>,
    as_of: i64,
) -> (Vec<DerivativePoint>, Vec<DerivativePoint>, Vec<DerivativePoint>) {
    let funding = reader
        .read_derivatives(
            instrument,
            DerivativeKind::FundingRate,
            start_ts,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    let oi = reader
        .read_derivatives(
            instrument,
            DerivativeKind::OpenInterest,
            start_ts,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    let mark = reader
        .read_derivatives(
            instrument,
            DerivativeKind::MarkPrice,
            start_ts,
            Some(as_of),
            Some(as_of),
            false,
        )
        .unwrap_or_default();
    (funding, oi, mark)
}

fn load_snapshot_derivatives(
    reader: &ColdReader,
    instrument: &InstrumentSpec,
    timeframes: &[String],
    earliest_bar_ts: Option<i64>,
    deriv_start: Option<i64>,
    cutoff_ts: i64,
) -> Vec<DerivativePoint> {
    let (mut funding, mut oi, mut mark) =
        load_derivative_points_since_reader(reader, instrument, deriv_start, cutoff_ts);

    if let Some(start_ts) = deriv_start {
        let anchor_ts = earliest_bar_ts.unwrap_or(cutoff_ts);
        if !funding
            .iter()
            .any(|p| p.is_funding_settlement() && p.ts_event <= anchor_ts)
            && let Ok(Some(prior)) = reader.read_last_visible_derivative_before(
                instrument,
                DerivativeKind::FundingRate,
                start_ts,
                Some(cutoff_ts),
                true,
                None,
            )
        {
            funding.push(prior);
        }

        let mut seen_oi_tfs: HashSet<Option<&str>> = HashSet::new();
        let mut seen_mark_tfs: HashSet<&str> = HashSet::new();
        for tf in timeframes {
            let oi_tf = oi_timeframe_for(tf);
            if seen_oi_tfs.insert(oi_tf)
                && !oi.iter().any(|p| {
                    p.ts_event <= anchor_ts && p.timeframe.as_deref().is_none_or(|t| Some(t) == oi_tf)
                })
                && let Ok(Some(prior)) = reader.read_last_visible_derivative_before(
                    instrument,
                    DerivativeKind::OpenInterest,
                    start_ts,
                    Some(cutoff_ts),
                    false,
                    oi_tf,
                )
            {
                oi.push(prior);
            }
            if seen_mark_tfs.insert(tf.as_str())
                && !mark.iter().any(|p| {
                    p.ts_event <= anchor_ts
                        && p.timeframe.as_deref().is_none_or(|t| t == tf.as_str())
                })
                && let Ok(Some(prior)) = reader.read_last_visible_derivative_before(
                    instrument,
                    DerivativeKind::MarkPrice,
                    start_ts,
                    Some(cutoff_ts),
                    false,
                    Some(tf.as_str()),
                )
            {
                mark.push(prior);
            }
        }
    }

    let mut combined = Vec::with_capacity(mark.len() + funding.len() + oi.len());
    combined.extend(mark);
    combined.extend(funding);
    combined.extend(oi);
    latest_visible_derivatives(combined, Some(cutoff_ts))
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

#[cfg(test)]
mod tests {
    use super::*;
    use crate::server::{ServerConfig, start_server};
    use tempfile::TempDir;

    #[tokio::test]
    async fn mark_backfill_cooldown_prunes_expired_entries() {
        let tmp = TempDir::new().unwrap();
        let replay_path = tmp.path().join("replay.jsonl");
        std::fs::write(&replay_path, "{\"offset_ms\":0,\"type\":\"heartbeat\"}\n").unwrap();
        let mut cfg = ServerConfig::new(tmp.path());
        cfg.replay_file = Some(replay_path);
        let srv = start_server(cfg).await.unwrap();

        let t0 = 1_767_225_600_000;
        mark_backfill_cooldown(&srv.state, "expired-key".to_owned(), t0, None);

        let t_after = t0 + BACKFILL_COOLDOWN_MS;
        mark_backfill_cooldown(&srv.state, "active-key".to_owned(), t_after, None);
        mark_backfill_cooldown(&srv.state, "newer-key".to_owned(), t_after + 1, None);

        {
            let map = srv.state.backfill_cooldown.lock().unwrap();
            assert!(!map.contains_key("expired-key"));
            assert!(map.contains_key("active-key"));
            assert!(map.contains_key("newer-key"));
            assert_eq!(map.len(), 2);
        }

        srv.shutdown().await;
    }
}
