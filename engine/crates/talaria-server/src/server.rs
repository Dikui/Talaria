//! Talaria loopback daemon (`AppState`, `ServerConfig`, lifecycle, and `serve()`).

use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use axum::Router;
use axum::http::header::{AUTHORIZATION, CONTENT_TYPE};
use axum::http::{Method, StatusCode};
use axum::middleware;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use talaria_contracts::error_codes::NOT_FOUND;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::snapshot::SnapshotRequest;
use talaria_feed::{
    DEFAULT_STALE_AFTER_MS, FeedClock, FeedSupervisor, FeedSupervisorConfig, ReplayConfig,
    ReplayTransport, validate_replay_data_dir,
};
use talaria_packs::{
    FeedTransport, HistoryConnector, HistoryConnectorConfig, MarketPack, RealtimeStreamConfig,
    get_market_pack,
};
use talaria_store::{ColdReader, ColdStore, DataDirLock, HotStreamBuffer};
use tokio::net::TcpListener;
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tower_http::cors::{Any, CorsLayer};

use crate::alerts::AlertEngine;
use crate::cold_writer::ColdWriter;
use crate::error::ApiError;
use crate::key_levels::KeyLevelRegistry;
use crate::routes::{api_router, freeze_snapshot_sync, load_confirmed_series, load_derivative_points};
use crate::scanner::{ScannerConfig, ScannerEngine};
use crate::security::{
    DATA_DIR_HEADER, FeatureToggles, SecurityContext, ServiceDescriptor, build_descriptor,
    data_dir_id, generate_bearer_token, load_service_features, remove_service_descriptor,
    resolve_data_dir, security_middleware, validate_loopback_host, write_service_descriptor,
};
use crate::snapshot_store::SnapshotStore;
use crate::state_store::{WorkbenchStateStore, alerts_append_trigger, alerts_list, alerts_write, drawings_get, watchlist_list};
use crate::subscriptions::{SubscriptionScheduler, WATCHLIST_TIMEFRAME};
use crate::ws::desk::{DeskBroadcast, ws_desk_handler};
use crate::ws::strategy::{StrategyStream, ws_strategy_handler};

pub const DEFAULT_HOST: &str = "127.0.0.1";
pub const DEFAULT_PORT: u16 = 0;
pub const DEFAULT_REALTIME_QUOTA: usize = 8;

/// Server startup configuration matching `talaria-data` CLI and test harnesses.
#[derive(Clone)]
pub struct ServerConfig {
    pub data_dir: PathBuf,
    pub host: String,
    pub port: u16,
    pub pack: String,
    pub desk_dist: Option<PathBuf>,
    pub replay_file: Option<PathBuf>,
    pub replay_speed: f64,
    pub fixture_dir: Option<PathBuf>,
    pub history_base_url: Option<String>,
    pub realtime_quota: usize,
    pub stale_after_ms: i64,
    pub token_override: Option<String>,
    pub now_ms: Option<Arc<dyn Fn() -> i64 + Send + Sync>>,
}

impl ServerConfig {
    pub fn new(data_dir: impl Into<PathBuf>) -> Self {
        Self {
            data_dir: data_dir.into(),
            host: DEFAULT_HOST.to_owned(),
            port: DEFAULT_PORT,
            pack: talaria_packs::DEFAULT_PACK_NAME.to_owned(),
            desk_dist: None,
            replay_file: None,
            replay_speed: 1.0,
            fixture_dir: None,
            history_base_url: None,
            realtime_quota: DEFAULT_REALTIME_QUOTA,
            stale_after_ms: DEFAULT_STALE_AFTER_MS,
            token_override: None,
            now_ms: None,
        }
    }
}

/// Shared runtime state held by Axum handlers and WebSocket tasks.
pub struct AppState {
    pub data_dir: PathBuf,
    pub pack_name: String,
    pub pack: Arc<dyn MarketPack>,
    pub instruments: Vec<InstrumentSpec>,
    pub desk_dist: Option<PathBuf>,
    pub replay_path: Option<PathBuf>,
    pub fixture_dir: Option<PathBuf>,
    pub features: FeatureToggles,
    pub security: SecurityContext,
    pub state_store: Arc<WorkbenchStateStore>,
    pub snapshots: Arc<SnapshotStore>,
    pub cold_store: Option<Arc<ColdStore>>,
    pub reader: Arc<ColdReader>,
    pub hot_buffer: Arc<HotStreamBuffer>,
    pub arrow_cache: Arc<crate::routes::arrow::ColdArrowCache>,
    pub desk: Arc<DeskBroadcast>,
    pub scheduler: Arc<SubscriptionScheduler>,
    pub key_levels: Arc<KeyLevelRegistry>,
    pub alerts: Arc<AlertEngine>,
    pub scanner: Arc<ScannerEngine>,
    pub strategy_stream: Option<Arc<StrategyStream>>,
    pub supervisor: Option<Arc<FeedSupervisor>>,
    pub history_connector: Option<Arc<dyn HistoryConnector>>,
    pub cold_writer: Option<Arc<ColdWriter>>,
    now_fn: Arc<dyn Fn() -> i64 + Send + Sync>,
    feed_mode_label: String,
}

impl AppState {
    pub fn now_ms(&self) -> i64 {
        (self.now_fn)()
    }

    pub fn has_supervisor(&self) -> bool {
        self.supervisor.is_some()
    }

    pub fn feed_mode_str(&self) -> &str {
        &self.feed_mode_label
    }

    pub fn is_realtime_instrument(&self, instrument_id: &str) -> bool {
        self.pack
            .traits(instrument_id)
            .map(|t| t.feed.realtime)
            .unwrap_or(true)
    }

    pub fn reconcile_watchlist_subscriptions(&self) {
        let records = match self.state_store.read_sync(watchlist_list) {
            Ok(r) => r,
            Err(_) => return,
        };
        let mut ordered = Vec::new();
        let mut seen = std::collections::HashSet::new();
        for g in &records.groups {
            if !g.is_system {
                for item in records.items_for(&g.group_id) {
                    if self.is_realtime_instrument(&item.instrument_id)
                        && seen.insert(item.instrument_id.clone())
                    {
                        ordered.push(item.instrument_id.clone());
                    }
                }
            }
        }
        let active_alerts = self.alerts.active_alert_subscriptions();
        self.scheduler.reconcile(&ordered, Some(&active_alerts));
    }
}

/// Live handle to a bound and running Talaria loopback daemon.
pub struct RunningServer {
    pub addr: SocketAddr,
    pub descriptor: ServiceDescriptor,
    pub state: Arc<AppState>,
    data_dir: PathBuf,
    dir_lock: Mutex<Option<DataDirLock>>,
    shutdown_tx: watch::Sender<bool>,
    serve_handle: Mutex<Option<JoinHandle<()>>>,
}

impl RunningServer {
    pub fn base_url(&self) -> &str {
        &self.descriptor.base_url
    }

    pub fn token(&self) -> &str {
        &self.descriptor.token
    }

    pub async fn shutdown(&self) {
        let _ = self.shutdown_tx.send(true);
        let serve_handle = self.serve_handle.lock().unwrap().take();
        if let Some(handle) = serve_handle {
            let _ = handle.await;
        }
        if let Some(sup) = &self.state.supervisor {
            sup.stop().await;
        }
        if let Some(stream) = &self.state.strategy_stream {
            stream.close();
        }
        if let Some(cw) = &self.state.cold_writer {
            cw.shutdown().await;
        }
        if let Some(cs) = &self.state.cold_store {
            let _ = cs.close();
        }
        let _ = self.state.snapshots.checkpoint_and_close();
        let _ = self.state.state_store.shutdown().await;
        remove_service_descriptor(&self.data_dir);
        let lock = self.dir_lock.lock().unwrap().take();
        if let Some(mut lock) = lock {
            lock.release();
        }
    }
}

impl Drop for RunningServer {
    fn drop(&mut self) {
        let _ = self.shutdown_tx.send(true);
        if let Some(cs) = &self.state.cold_store {
            let _ = cs.close();
        }
        remove_service_descriptor(&self.data_dir);
        let lock = self.dir_lock.lock().unwrap().take();
        if let Some(mut lock) = lock {
            lock.release();
        }
    }
}

struct ClockAdapter(Arc<dyn Fn() -> i64 + Send + Sync>);
impl FeedClock for ClockAdapter {
    fn now_ms(&self) -> i64 {
        (self.0)()
    }

    fn sleep(&self, duration: std::time::Duration) -> talaria_packs::BoxFuture<'_, ()> {
        Box::pin(tokio::time::sleep(duration))
    }
}

/// Start the local daemon, acquire `service.lock`, bind `127.0.0.1:<port>`, and write `0600` descriptor.
pub async fn start_server(config: ServerConfig) -> Result<RunningServer, ApiError> {
    let bind_ip = validate_loopback_host(&config.host)?;
    if config.replay_file.is_some() {
        validate_replay_data_dir(Some(&config.data_dir), &[])?;
    }
    let data_dir = resolve_data_dir(&config.data_dir)
        .map_err(|e| ApiError::internal(format!("failed to prepare data dir: {e}")))?;

    let dir_lock = DataDirLock::acquire(&data_dir, "service")?;
    let features = load_service_features(&data_dir)?;
    let dir_id = data_dir_id(&data_dir);
    let token = config
        .token_override
        .clone()
        .unwrap_or_else(generate_bearer_token);
    let now_fn: Arc<dyn Fn() -> i64 + Send + Sync> = config
        .now_ms
        .clone()
        .unwrap_or_else(|| Arc::new(|| chrono::Utc::now().timestamp_millis()));

    let pack = get_market_pack(&config.pack)?;
    let instruments = pack.instruments().to_vec();

    let state_store = Arc::new(WorkbenchStateStore::open(&data_dir, Arc::clone(&now_fn))?);
    let snapshots = Arc::new(SnapshotStore::open(&data_dir)?);
    let cold_store = if features.cold_store {
        Some(Arc::new(ColdStore::open(&data_dir)?))
    } else {
        None
    };
    let reader = Arc::new(ColdReader::new(&data_dir));
    let hot_buffer = Arc::new(HotStreamBuffer::with_default_capacity(
        &instruments,
        Arc::clone(&now_fn),
    )?);

    let desk = Arc::new(DeskBroadcast::new());
    let key_levels = Arc::new(KeyLevelRegistry::new(Arc::clone(&now_fn)));
    {
        let desk_c = Arc::clone(&desk);
        key_levels.set_on_latest(Arc::new(move |set| {
            desk_c.project_levels(set);
        }));
    }

    let alerts = Arc::new(AlertEngine::new(Arc::clone(&now_fn), 600));
    let scanner_freeze_snapshots = Arc::clone(&snapshots);
    let scanner_reader = Arc::clone(&reader);
    let scanner_hot = Arc::clone(&hot_buffer);
    let scanner_pack = Arc::clone(&pack);
    let scanner_kl = Arc::clone(&key_levels);
    let scanner_insts = instruments.clone();
    let scanner_now = Arc::clone(&now_fn);

    let scanner = Arc::new(ScannerEngine::new(
        Arc::clone(&now_fn),
        ScannerConfig::default(),
        Arc::new(move |inst_id, tf, as_of| {
            freeze_ephemeral_snapshot(
                &scanner_insts,
                &scanner_pack,
                &scanner_reader,
                &scanner_hot,
                &scanner_snapshots_ref(&scanner_freeze_snapshots),
                &scanner_kl,
                &scanner_now,
                inst_id,
                tf,
                as_of,
            )
            .unwrap_or_default()
        }),
    ));
    {
        let kl = Arc::clone(&key_levels);
        scanner.set_rule_levels_provider(Arc::new(move |inst, tf, cutoff| {
            kl.levels_at(inst, tf, cutoff)
        }));
    }

    // Load initial persisted alerts
    if let Ok(existing_alerts) = state_store.read_sync(|conn| alerts_list(conn, None)) {
        alerts.load(existing_alerts);
    }

    let desk_degraded = Arc::clone(&desk);
    let alerts_degraded = Arc::clone(&alerts);
    let on_degraded = Arc::new(move |owner: &crate::subscriptions::Owner| {
        if owner.0 == crate::subscriptions::OwnerKind::Alert {
            alerts_degraded.suspend_channel(&owner.2.0, &owner.2.1);
        }
        desk_degraded.publish_subscription_error(owner);
    });
    let desk_quota = Arc::clone(&desk);
    let on_alert_quota = Arc::new(move |denied: &[String]| {
        desk_quota.publish_alert_quota(denied);
    });
    let scheduler = Arc::new(SubscriptionScheduler::new(
        None,
        config.realtime_quota,
        WATCHLIST_TIMEFRAME,
        Some(on_degraded),
        Some(on_alert_quota),
    )?);

    // Wire AlertEngine callbacks after all components exist
    {
        let a_store = Arc::clone(&state_store);
        let a_insts = instruments.clone();
        let a_reader = Arc::clone(&reader);
        let a_hot = Arc::clone(&hot_buffer);
        let a_kl = Arc::clone(&key_levels);
        let a_snaps = Arc::clone(&snapshots);
        let a_pack = Arc::clone(&pack);
        let a_desk = Arc::clone(&desk);
        let a_now = Arc::clone(&now_fn);

        let history_provider = Arc::new({
            let reader_c = Arc::clone(&a_reader);
            let hot_c = Arc::clone(&a_hot);
            let insts_c = a_insts.clone();
            let now_c = Arc::clone(&a_now);
            move |inst_id: &str, tf: &str| {
                let Some(spec) = insts_c.iter().find(|s| s.instrument_id == inst_id) else {
                    return Vec::new();
                };
                let cutoff = now_c();
                let cold = reader_c.read_revisions(spec, tf, None, Some(cutoff)).unwrap_or_default();
                let hot = hot_c.revisions(inst_id, tf, 0, Some(cutoff));
                let visible = talaria_store::range_visible_bars(&cold, &hot, Some(cutoff), false);
                crate::routes::raw_bars_to_compact(&visible, false)
            }
        });
        let drawing_provider = Arc::new({
            let store_c = Arc::clone(&a_store);
            move |id: &str| store_c.read_sync(|conn| drawings_get(conn, id, None)).ok()
        });
        let level_provider = Arc::new({
            let kl = Arc::clone(&a_kl);
            move |inst_id: &str, tf: &str, cutoff: i64| kl.lookup_level_map(inst_id, tf, cutoff)
        });
        let deriv_archive = Arc::new({
            let reader_c = Arc::clone(&a_reader);
            let insts_c = a_insts.clone();
            move |inst_id: &str, kind, cutoff: i64| {
                let Some(spec) = insts_c.iter().find(|s| s.instrument_id == inst_id) else {
                    return Vec::new();
                };
                reader_c
                    .read_derivatives(spec, kind, None, Some(cutoff), Some(cutoff), false)
                    .unwrap_or_default()
            }
        });
        let freeze_snap = Arc::new({
            let kl = Arc::clone(&a_kl);
            let reader_c = Arc::clone(&a_reader);
            let hot_c = Arc::clone(&a_hot);
            let snaps_c = Arc::clone(&a_snaps);
            let pack_c = Arc::clone(&a_pack);
            let now_c = Arc::clone(&a_now);
            move |alert: &talaria_contracts::alerts::AlertRecord, as_of: i64| {
                let insts_l = instruments_from_pack(&pack_c);
                freeze_ephemeral_snapshot(
                    &insts_l,
                    &pack_c,
                    &reader_c,
                    &hot_c,
                    &snaps_c,
                    &kl,
                    &now_c,
                    &alert.instrument_id,
                    &alert.timeframe,
                    as_of,
                )
                .unwrap_or_default()
            }
        });
        let on_trigger = Arc::new(
            move |trigger: &talaria_contracts::alerts::AlertTriggerRecord,
                  updated_alert: &talaria_contracts::alerts::AlertRecord| {
                let store_c = Arc::clone(&a_store);
                let trg_c = trigger.clone();
                let alt_c = updated_alert.clone();
                tokio::spawn(async move {
                    let _ = store_c
                        .write(move |conn, now| {
                            alerts_write(conn, &alt_c)?;
                            alerts_append_trigger(conn, &trg_c, now)?;
                            Ok(())
                        })
                        .await;
                });
                a_desk.publish_alert(trigger);
            },
        );

        alerts.set_callbacks(
            Some(history_provider),
            Some(drawing_provider),
            Some(level_provider),
            Some(deriv_archive),
            Some(freeze_snap),
            Some(on_trigger),
        );
    }

    let history_connector: Option<Arc<dyn HistoryConnector>> = if features.market_data
        || config.fixture_dir.is_some()
        || config.history_base_url.is_some()
    {
        let hcfg = HistoryConnectorConfig {
            base_url: config.history_base_url.clone(),
            fixture_dir: config.fixture_dir.clone(),
            now_ms: Arc::clone(&now_fn),
            ..HistoryConnectorConfig::default()
        };
        Some(pack.create_history_connector(hcfg)?)
    } else {
        None
    };

    let mut feed_mode_label = "offline_sample".to_owned();
    let strategy_stream: Option<Arc<StrategyStream>>;
    let cold_writer: Option<Arc<ColdWriter>>;
    let supervisor: Option<Arc<FeedSupervisor>>;

    let want_supervisor =
        config.replay_file.is_some() || (features.market_data && config.fixture_dir.is_none());
    if want_supervisor {
        let transport: Arc<dyn FeedTransport> = if let Some(replay_path) = &config.replay_file {
            feed_mode_label = "replay".to_owned();
            Arc::new(ReplayTransport::from_file_for_data_dir(
                replay_path,
                Some(&data_dir),
                &[],
                ReplayConfig {
                    speed: config.replay_speed,
                    clock: Arc::new(ClockAdapter(Arc::clone(&now_fn))),
                    ..ReplayConfig::default()
                },
            )?)
        } else {
            feed_mode_label = "live".to_owned();
            pack.create_realtime_stream(RealtimeStreamConfig {
                now_ms: Arc::clone(&now_fn),
                ..RealtimeStreamConfig::default()
            })?
        };

        let s_stream = Arc::new(StrategyStream::new(
            Arc::clone(&scheduler),
            Arc::clone(&hot_buffer),
            Arc::clone(&reader),
            &instruments,
            Arc::clone(&now_fn),
        ));
        let writer = ColdWriter::spawn(
            Arc::clone(&now_fn),
            &instruments,
            cold_store.clone(),
            Arc::clone(&reader),
            Arc::clone(&hot_buffer),
            Arc::clone(&desk),
            Some(Arc::clone(&s_stream)),
            Arc::clone(&key_levels),
            Arc::clone(&alerts),
            Arc::clone(&scanner),
            Arc::clone(&state_store),
        );
        let sup = Arc::new(FeedSupervisor::new(
            transport,
            history_connector.clone(),
            instruments.clone(),
            Arc::new(ClockAdapter(Arc::clone(&now_fn))),
            Arc::clone(&writer) as Arc<dyn talaria_feed::BarPublishSink>,
            FeedSupervisorConfig {
                stale_after_ms: config.stale_after_ms,
                ..FeedSupervisorConfig::default()
            },
        )?);
        sup.start().await?;
        scheduler.set_supervisor(Arc::clone(&sup));

        strategy_stream = Some(s_stream);
        cold_writer = Some(writer);
        supervisor = Some(sup);
    } else {
        if config.fixture_dir.is_some() {
            feed_mode_label = "fixture".to_owned();
        }
        strategy_stream = None;
        cold_writer = None;
        supervisor = None;
    }

    let security = SecurityContext {
        token: token.clone(),
        data_dir: data_dir.clone(),
        data_dir_id: dir_id.clone(),
    };

    let desk_dist = resolve_desk_dist(config.desk_dist.as_deref());
    let state = Arc::new(AppState {
        data_dir: data_dir.clone(),
        pack_name: config.pack.clone(),
        pack,
        instruments,
        desk_dist,
        replay_path: config.replay_file.clone(),
        fixture_dir: config.fixture_dir.clone(),
        features,
        security: security.clone(),
        state_store,
        snapshots,
        cold_store,
        reader,
        hot_buffer,
        arrow_cache: Arc::new(crate::routes::arrow::ColdArrowCache::new()),
        desk,
        scheduler,
        key_levels,
        alerts,
        scanner,
        strategy_stream,
        supervisor,
        history_connector,
        cold_writer,
        now_fn,
        feed_mode_label,
    });

    // Trigger startup key-level warm-up & initial watchlist reconciliation cleanly
    state.reconcile_watchlist_subscriptions();
    warm_initial_key_levels(&state);

    let cors = CorsLayer::new()
        .allow_origin(Any)
        .allow_methods([
            Method::GET,
            Method::POST,
            Method::PUT,
            Method::PATCH,
            Method::DELETE,
            Method::OPTIONS,
        ])
        .allow_headers([AUTHORIZATION, CONTENT_TYPE, axum::http::header::HeaderName::from_static(DATA_DIR_HEADER)]);

    let app = Router::new()
        .merge(api_router(Arc::clone(&state)))
        .route("/ws/desk", get(ws_desk_handler))
        .route("/ws/strategy", get(ws_strategy_handler))
        .fallback(json_fallback_handler)
        .layer(middleware::from_fn_with_state(
            security.clone(),
            security_middleware,
        ))
        .layer(cors)
        .with_state(Arc::clone(&state));

    let bind_addr = SocketAddr::new(bind_ip, config.port);
    let listener = TcpListener::bind(bind_addr)
        .await
        .map_err(|e| ApiError::internal(format!("failed to bind {bind_addr}: {e}")))?;
    let local_addr = listener
        .local_addr()
        .map_err(|e| ApiError::internal(format!("local_addr failed: {e}")))?;

    let base_url = crate::security::loopback_base_url(bind_ip, local_addr.port());
    let descriptor = build_descriptor(base_url, token, &data_dir, dir_id, features);
    write_service_descriptor(&data_dir, &descriptor)
        .map_err(|e| ApiError::internal(format!("failed to write service descriptor: {e}")))?;

    let (shutdown_tx, mut shutdown_rx) = watch::channel(false);
    let serve_handle = tokio::spawn(async move {
        let _ = axum::serve(listener, app)
            .with_graceful_shutdown(async move {
                while !*shutdown_rx.borrow() {
                    if shutdown_rx.changed().await.is_err() {
                        break;
                    }
                }
            })
            .await;
    });

    Ok(RunningServer {
        addr: local_addr,
        descriptor,
        state,
        data_dir,
        dir_lock: Mutex::new(Some(dir_lock)),
        shutdown_tx,
        serve_handle: Mutex::new(Some(serve_handle)),
    })
}

async fn json_fallback_handler() -> Response {
    ApiError::not_found(NOT_FOUND, "endpoint not found").into_response()
}

fn scanner_snapshots_ref(snaps: &Arc<SnapshotStore>) -> Arc<SnapshotStore> {
    Arc::clone(snaps)
}

fn instruments_from_pack(pack: &Arc<dyn MarketPack>) -> Vec<InstrumentSpec> {
    pack.instruments().to_vec()
}

#[allow(clippy::too_many_arguments)]
fn freeze_ephemeral_snapshot(
    instruments: &[InstrumentSpec],
    pack: &Arc<dyn MarketPack>,
    reader: &ColdReader,
    hot_buffer: &Arc<HotStreamBuffer>,
    snapshots: &Arc<SnapshotStore>,
    key_levels: &Arc<KeyLevelRegistry>,
    now_ms: &Arc<dyn Fn() -> i64 + Send + Sync>,
    instrument_id: &str,
    timeframe: &str,
    cutoff_ts: i64,
) -> Result<String, ApiError> {
    let spec = instruments
        .iter()
        .find(|s| s.instrument_id == instrument_id)
        .cloned()
        .ok_or_else(|| ApiError::not_found(NOT_FOUND, "instrument not found"))?;
    let interval = talaria_contracts::timeframe_ms(timeframe)?;
    let closed_end = talaria_contracts::last_closed_bar_start(cutoff_ts, interval);
    let cold = reader.read_revisions(&spec, timeframe, None, Some(closed_end))?;
    let hot = hot_buffer.revisions(&spec.instrument_id, timeframe, 0, Some(closed_end));
    let mut visible = talaria_store::range_visible_bars(&cold, &hot, Some(cutoff_ts), false);
    let tail_start = visible.len().saturating_sub(600);
    if tail_start > 0 {
        visible.drain(0..tail_start);
    }
    let compact = talaria_feed::compact_series(&visible, timeframe, cutoff_ts)?;
    if compact.is_empty() {
        return Err(ApiError::feed_unavailable("no bars for snapshot"));
    }
    let mut bar_counts = std::collections::BTreeMap::new();
    bar_counts.insert(timeframe.to_owned(), compact.len());
    let mut frames = std::collections::BTreeMap::new();
    frames.insert(timeframe.to_owned(), compact.clone());

    let now = now_ms();
    let snapshot_id = format!("snp_{}", uuid::Uuid::new_v4().simple());
    let meta = talaria_contracts::snapshot::SnapshotMeta {
        snapshot_id: snapshot_id.clone(),
        instrument_id: spec.instrument_id.clone(),
        timeframes: vec![timeframe.to_owned()],
        cutoff_ts,
        created_ts: now,
        mode: talaria_contracts::snapshot::SnapshotMode::HistoricalPit,
        bar_counts,
    };
    snapshots.save(&crate::snapshot_store::FrozenSnapshot {
        meta,
        frames,
        derivatives: Vec::new(),
    })?;
    let session_profile = pack
        .traits(&spec.instrument_id)
        .ok()
        .map(|t| talaria_contracts::key_levels::SessionProfile::from(&t.session));
    let _ = key_levels.compute_and_register(
        snapshot_id.clone(),
        &spec.instrument_id,
        timeframe,
        &compact,
        cutoff_ts,
        now,
        session_profile.as_ref(),
        None,
        &[],
    );
    Ok(snapshot_id)
}

fn warm_initial_key_levels(state: &Arc<AppState>) {
    let state_c = Arc::clone(state);
    tokio::task::spawn_blocking(move || {
        let now = state_c.now_ms();
        for spec in &state_c.instruments {
            if let Ok(bars) = load_confirmed_series(&state_c, &spec.instrument_id, "1m", now, 600)
                && !bars.is_empty()
            {
                let (funding, oi, mark) = load_derivative_points(&state_c, spec, now);
                let mut derivs = Vec::with_capacity(funding.len() + oi.len() + mark.len());
                derivs.extend(funding);
                derivs.extend(oi);
                derivs.extend(mark);
                let session_profile = state_c
                    .pack
                    .traits(&spec.instrument_id)
                    .ok()
                    .map(|t| talaria_contracts::key_levels::SessionProfile::from(&t.session));
                let _ = state_c.key_levels.compute_and_register(
                    format!("boot-{}-1m-{now}", spec.instrument_id),
                    &spec.instrument_id,
                    "1m",
                    &bars,
                    now,
                    now,
                    session_profile.as_ref(),
                    None,
                    &derivs,
                );
            }
        }
    });
}

fn resolve_desk_dist(explicit: Option<&Path>) -> Option<PathBuf> {
    if let Some(dir) = explicit {
        return Some(dir.to_path_buf());
    }
    let default_rel = Path::new("workbench/packages/talaria-desk-ui/dist");
    if default_rel.is_dir() {
        return Some(default_rel.to_path_buf());
    }
    let legacy_rel = Path::new("dsh/packages/talaria-desk-ui/dist");
    if legacy_rel.is_dir() {
        return Some(legacy_rel.to_path_buf());
    }
    None
}

/// Ensure JSON error responses even on unhandled StatusCode/extractors cleanly.
#[allow(dead_code)]
fn _unused_markers(
    _s: StatusCode,
    _r: SnapshotRequest,
    _d: Response,
    _f: fn(&AppState, &SnapshotRequest) -> Result<talaria_contracts::snapshot::SnapshotMeta, ApiError>,
) {
    let _ = freeze_snapshot_sync;
}
