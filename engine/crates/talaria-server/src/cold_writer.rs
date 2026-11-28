//! Asynchronous Cold Store writer and `BarPublishSink` bridge.
//!
//! Connects `talaria-feed`'s `FeedSupervisor` to:
//! - `HotStreamBuffer` (in-memory zero-latency streaming tail)
//! - `DeskBroadcast` & `StrategyStream` (real-time WebSocket fanout)
//! - `ColdStore` (month/day partitioned Parquet generation commits)
//! - `KeyLevelRegistry`, `AlertEngine`, and `ScannerEngine`

use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use talaria_contracts::bars::{BarStatus, CompactBar, RawBar};
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::feed::FeedEvent;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::ticks::Trade;
use talaria_feed::BarPublishSink;
use talaria_packs::TransportFrame;
use talaria_store::{ColdReader, ColdStore, HotStreamBuffer, partition_day, partition_month};
use tokio::sync::{mpsc, oneshot};
use tokio::task::JoinHandle;

use crate::alerts::AlertEngine;
use crate::key_levels::KeyLevelRegistry;
use crate::scanner::ScannerEngine;
use crate::state_store::{WorkbenchStateStore, alerts_write};
use crate::structures::StructureRegistry;
use crate::ws::desk::DeskBroadcast;
use crate::ws::strategy::{StrategyKey, StrategyStream};

type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type PendingBarsMap = HashMap<(String, String, String), (InstrumentSpec, String, Vec<RawBar>)>;
type PendingDerivsMap =
    HashMap<(String, DerivativeKind, String), (InstrumentSpec, DerivativeKind, Vec<DerivativePoint>)>;
type PendingTradesMap = HashMap<(String, String), (InstrumentSpec, Vec<Trade>)>;

/// Low-latency flush interval for closed bars (bars close at most once per timeframe).
const BAR_FLUSH_INTERVAL: Duration = Duration::from_millis(50);
/// Batched flush interval for high-frequency derivatives & trades to avoid Parquet file explosion.
const BULK_FLUSH_INTERVAL: Duration = Duration::from_secs(5);
/// Early-flush threshold per pending derivative/trade batch to cap in-memory buffering.
const BULK_FLUSH_MAX_ROWS: usize = 2_000;
/// Periodic background compaction interval while the daemon stays running.
pub(crate) const PERIODIC_COMPACT_INTERVAL: Duration = Duration::from_secs(300);

enum ColdCmd {
    Bar {
        instrument: InstrumentSpec,
        timeframe: String,
        bar: RawBar,
    },
    Derivative(InstrumentSpec, DerivativePoint),
    Trade(InstrumentSpec, Trade),
    Flush(oneshot::Sender<()>),
    Compact(oneshot::Sender<()>),
    Shutdown(oneshot::Sender<()>),
}

#[derive(Default)]
struct DrainedControl {
    flush_waiters: Vec<oneshot::Sender<()>>,
    compact_waiters: Vec<oneshot::Sender<()>>,
    shutdown_waiters: Vec<oneshot::Sender<()>>,
}

struct CompactFlagGuard(Arc<AtomicBool>);

impl Drop for CompactFlagGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::Release);
    }
}

pub struct ColdWriter {
    now_ms: NowFn,
    instruments: Arc<RwLock<HashMap<String, InstrumentSpec>>>,
    cold_store: Option<Arc<ColdStore>>,
    hot_buffer: Arc<HotStreamBuffer>,
    desk: Arc<DeskBroadcast>,
    strategy_stream: Option<Arc<StrategyStream>>,
    alerts: Arc<AlertEngine>,
    scanner: Arc<ScannerEngine>,
    state_store: Arc<WorkbenchStateStore>,
    tx: mpsc::UnboundedSender<ColdCmd>,
    worker: Mutex<Option<JoinHandle<()>>>,
}

impl ColdWriter {
    #[allow(clippy::too_many_arguments)]
    pub fn spawn(
        now_ms: NowFn,
        instruments: &[InstrumentSpec],
        cold_store: Option<Arc<ColdStore>>,
        reader: Arc<ColdReader>,
        hot_buffer: Arc<HotStreamBuffer>,
        desk: Arc<DeskBroadcast>,
        strategy_stream: Option<Arc<StrategyStream>>,
        key_levels: Arc<KeyLevelRegistry>,
        structures: Arc<StructureRegistry>,
        alerts: Arc<AlertEngine>,
        scanner: Arc<ScannerEngine>,
        state_store: Arc<WorkbenchStateStore>,
    ) -> Arc<Self> {
        let mut inst_map = HashMap::with_capacity(instruments.len());
        for spec in instruments {
            inst_map.insert(spec.instrument_id.clone(), spec.clone());
        }
        let shared_instruments = Arc::new(RwLock::new(inst_map));
        let (tx, mut rx) = mpsc::unbounded_channel();

        let w_store = cold_store.clone();
        let w_reader = reader;
        let w_strategy = strategy_stream.clone();
        let w_levels = Arc::clone(&key_levels);
        let w_structures = Arc::clone(&structures);
        let w_now = Arc::clone(&now_ms);
        let w_instruments = Arc::clone(&shared_instruments);

        let handle = tokio::spawn(async move {
            let mut pending_bars: PendingBarsMap = HashMap::new();
            let mut pending_derivs: PendingDerivsMap = HashMap::new();
            let mut pending_trades: PendingTradesMap = HashMap::new();
            let compacting = Arc::new(AtomicBool::new(false));
            let mut compact_task: Option<JoinHandle<()>> = None;

            let mut bar_flush_interval = tokio::time::interval(BAR_FLUSH_INTERVAL);
            bar_flush_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

            let mut bulk_flush_interval = tokio::time::interval_at(
                tokio::time::Instant::now() + BULK_FLUSH_INTERVAL,
                BULK_FLUSH_INTERVAL,
            );
            bulk_flush_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

            let mut compact_interval = tokio::time::interval_at(
                tokio::time::Instant::now() + PERIODIC_COMPACT_INTERVAL,
                PERIODIC_COMPACT_INTERVAL,
            );
            compact_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

            loop {
                tokio::select! {
                    _ = bar_flush_interval.tick() => {
                        flush_bars(
                            w_store.as_ref(),
                            &w_reader,
                            w_strategy.as_ref(),
                            &w_levels,
                            &w_structures,
                            &w_now,
                            &mut pending_bars,
                        ).await;
                    }
                    _ = bulk_flush_interval.tick() => {
                        flush_bulk(
                            w_store.as_ref(),
                            w_strategy.as_ref(),
                            &mut pending_derivs,
                            &mut pending_trades,
                        ).await;
                    }
                    _ = compact_interval.tick() => {
                        flush_pending(
                            w_store.as_ref(),
                            &w_reader,
                            w_strategy.as_ref(),
                            &w_levels,
                            &w_structures,
                            &w_now,
                            &mut pending_bars,
                            &mut pending_derivs,
                            &mut pending_trades,
                        ).await;
                        if let Some(store) = &w_store
                            && !compacting.load(Ordering::Acquire)
                        {
                            let now = (w_now)();
                            let store_check = Arc::clone(store);
                            let need_compact = tokio::task::spawn_blocking(move || {
                                store_check.has_compactable_work(Some(now)).unwrap_or(false)
                            })
                            .await
                            .unwrap_or(false);
                            if need_compact
                                && compacting
                                    .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
                                    .is_ok()
                            {
                                let all_specs: Vec<InstrumentSpec> = w_instruments
                                    .read()
                                    .map(|m| m.values().cloned().collect())
                                    .unwrap_or_default();
                                let store_c = Arc::clone(store);
                                let flag = Arc::clone(&compacting);
                                compact_task = Some(tokio::spawn(async move {
                                    let _guard = CompactFlagGuard(flag);
                                    match tokio::task::spawn_blocking(move || {
                                        store_c.compact_periodic(&all_specs, Some(now))
                                    })
                                    .await
                                    {
                                        Ok(Ok(report)) => {
                                            if report.compacted_groups > 0 || report.pruned_files > 0 {
                                                tracing::info!(
                                                    compacted_groups = report.compacted_groups,
                                                    retired_files_added = report.retired_files_added,
                                                    pruned_files = report.pruned_files,
                                                    "periodic cold store compaction finished"
                                                );
                                            }
                                        }
                                        Ok(Err(err)) => {
                                            tracing::warn!(error = %err, "periodic cold store compaction failed");
                                        }
                                        Err(err) => {
                                            tracing::warn!(error = %err, "periodic cold store compaction task panicked");
                                        }
                                    }
                                }));
                            }
                        }
                    }
                    cmd = rx.recv() => {
                        match cmd {
                            Some(ColdCmd::Bar { instrument, timeframe, bar }) => {
                                let month = partition_month(bar.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                                let key = (instrument.instrument_id.clone(), timeframe.clone(), month);
                                pending_bars
                                    .entry(key)
                                    .or_insert_with(|| (instrument, timeframe, Vec::new()))
                                    .2
                                    .push(bar);
                            }
                            Some(ColdCmd::Derivative(instrument, pt)) => {
                                let month = partition_month(pt.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                                let key = (instrument.instrument_id.clone(), pt.kind, month);
                                let entry = pending_derivs
                                    .entry(key)
                                    .or_insert_with(|| (instrument, pt.kind, Vec::new()));
                                entry.2.push(pt);
                                if entry.2.len() >= BULK_FLUSH_MAX_ROWS {
                                    flush_bulk(
                                        w_store.as_ref(),
                                        w_strategy.as_ref(),
                                        &mut pending_derivs,
                                        &mut pending_trades,
                                    )
                                    .await;
                                }
                            }
                            Some(ColdCmd::Trade(instrument, trade)) => {
                                let day = partition_day(trade.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                                let key = (instrument.instrument_id.clone(), day);
                                let entry = pending_trades
                                    .entry(key)
                                    .or_insert_with(|| (instrument, Vec::new()));
                                entry.1.push(trade);
                                if entry.1.len() >= BULK_FLUSH_MAX_ROWS {
                                    flush_bulk(
                                        w_store.as_ref(),
                                        w_strategy.as_ref(),
                                        &mut pending_derivs,
                                        &mut pending_trades,
                                    )
                                    .await;
                                }
                            }
                            cmd @ (Some(ColdCmd::Flush(_))
                            | Some(ColdCmd::Compact(_))
                            | Some(ColdCmd::Shutdown(_))
                            | None) => {
                                let is_channel_closed = cmd.is_none();
                                let mut control = DrainedControl::default();
                                match cmd {
                                    Some(ColdCmd::Flush(done)) => control.flush_waiters.push(done),
                                    Some(ColdCmd::Compact(done)) => control.compact_waiters.push(done),
                                    Some(ColdCmd::Shutdown(done)) => control.shutdown_waiters.push(done),
                                    None => {}
                                    _ => unreachable!(),
                                }
                                drain_rx_into_pending(
                                    &mut rx,
                                    &mut pending_bars,
                                    &mut pending_derivs,
                                    &mut pending_trades,
                                    &mut control,
                                );
                                flush_pending(
                                    w_store.as_ref(),
                                    &w_reader,
                                    w_strategy.as_ref(),
                                    &w_levels,
                                    &w_structures,
                                    &w_now,
                                    &mut pending_bars,
                                    &mut pending_derivs,
                                    &mut pending_trades,
                                )
                                .await;
                                notify_waiters(control.flush_waiters);
                                if is_channel_closed || !control.shutdown_waiters.is_empty() {
                                    run_shutdown_compact_and_close(
                                        w_store.as_ref(),
                                        &w_instruments,
                                        &mut compact_task,
                                    )
                                    .await;
                                    notify_waiters(control.compact_waiters);
                                    notify_waiters(control.shutdown_waiters);
                                    break;
                                }
                                if !control.compact_waiters.is_empty() {
                                    run_periodic_compact(
                                        w_store.as_ref(),
                                        &w_instruments,
                                        &w_now,
                                        &mut compact_task,
                                    )
                                    .await;
                                    notify_waiters(control.compact_waiters);
                                }
                            }
                        }
                    }
                }
            }
        });

        Arc::new(Self {
            now_ms,
            instruments: shared_instruments,
            cold_store,
            hot_buffer,
            desk,
            strategy_stream,
            alerts,
            scanner,
            state_store,
            tx,
            worker: Mutex::new(Some(handle)),
        })
    }

    pub async fn flush(&self) {
        let (tx, rx) = oneshot::channel();
        if self.tx.send(ColdCmd::Flush(tx)).is_ok() {
            let _ = rx.await;
        }
    }

    pub async fn compact_periodic_now(&self) {
        let (tx, rx) = oneshot::channel();
        if self.tx.send(ColdCmd::Compact(tx)).is_ok() {
            let _ = rx.await;
        }
    }

    pub async fn shutdown(&self) {
        let (tx, rx) = oneshot::channel();
        if self.tx.send(ColdCmd::Shutdown(tx)).is_ok() {
            let _ = rx.await;
        }
        let handle = self.worker.lock().unwrap().take();
        if let Some(h) = handle {
            let _ = h.await;
        }
    }

    pub fn cold_store(&self) -> Option<&Arc<ColdStore>> {
        self.cold_store.as_ref()
    }

    pub fn register_instrument(&self, spec: InstrumentSpec) {
        if let Ok(mut guard) = self.instruments.write() {
            guard.insert(spec.instrument_id.clone(), spec);
        }
    }
}

impl BarPublishSink for ColdWriter {
    fn on_bar_close(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        bar: &CompactBar,
        received_ts: i64,
        revision: u64,
    ) {
        if bar.status == BarStatus::FeedGap {
            return;
        }
        let (Some(open), Some(high), Some(low), Some(close), Some(volume), Some(turnover)) = (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.turnover,
        ) else {
            return;
        };
        let now = (self.now_ms)();
        let raw = RawBar {
            symbol: instrument.symbol.clone(),
            timeframe: timeframe.to_owned(),
            ts_event: bar.timestamp,
            open,
            high,
            low,
            close,
            volume,
            turnover,
            ts_available: bar.ts_available,
            ts_ingested: received_ts,
            ts_committed: now.max(received_ts),
            precision: bar.precision,
            revision,
            confirmed: true,
            history_only: false,
            synthetic: false,
        };
        if self.cold_store.is_some() {
            let _ = self.tx.send(ColdCmd::Bar {
                instrument: instrument.clone(),
                timeframe: timeframe.to_owned(),
                bar: raw,
            });
        }
    }

    fn on_recovered_bar(&self, instrument: &InstrumentSpec, bar: &RawBar) {
        if self.cold_store.is_some() {
            let _ = self.tx.send(ColdCmd::Bar {
                instrument: instrument.clone(),
                timeframe: bar.timeframe.clone(),
                bar: bar.clone(),
            });
        }
    }

    fn on_event(&self, event: &FeedEvent) {
        let ingested = self.hot_buffer.ingest(event);
        if let FeedEvent::Kline(k) = event
            && let Some(raw) = &ingested
            && raw.confirmed
            && !raw.synthetic
            && let Some(stream) = &self.strategy_stream
        {
            stream.on_confirmed_kline(k, raw.ts_event);
        }

        self.desk.publish_feed_event(event);
        self.scanner.handle_event(event);
        let changed_alerts = self.alerts.handle_event(event);
        if !changed_alerts.is_empty() {
            let store = Arc::clone(&self.state_store);
            tokio::spawn(async move {
                let _ = store
                    .write(move |conn, _| {
                        for a in &changed_alerts {
                            alerts_write(conn, a)?;
                        }
                        Ok(())
                    })
                    .await;
            });
        }

        if let FeedEvent::DerivativeTick(d) = event
            && self.cold_store.is_some()
            && let Some(spec) = self
                .instruments
                .read()
                .ok()
                .and_then(|m| m.get(&d.instrument_id).cloned())
        {
            let now = (self.now_ms)().max(d.ts_ingested);
            let ts_avail = d.ts_event.max(d.ts_ingested);
            if let Some(mark) = d.mark_price {
                let _ = self.tx.send(ColdCmd::Derivative(
                    spec.clone(),
                    DerivativePoint {
                        instrument_id: d.instrument_id.clone(),
                        kind: DerivativeKind::MarkPrice,
                        ts_event: d.ts_event,
                        ts_published: None,
                        ts_available: ts_avail,
                        value: mark,
                        timeframe: None,
                        precision: talaria_contracts::PrecisionTier::SecondVerified,
                        ts_ingested: d.ts_ingested,
                        ts_committed: now,
                        revision: 0,
                        settled: None,
                    },
                ));
            }
            if let Some(rate) = d.funding_rate {
                let _ = self.tx.send(ColdCmd::Derivative(
                    spec.clone(),
                    DerivativePoint {
                        instrument_id: d.instrument_id.clone(),
                        kind: DerivativeKind::FundingRate,
                        ts_event: d.ts_event,
                        ts_published: Some(d.ts_event),
                        ts_available: ts_avail,
                        value: rate,
                        timeframe: None,
                        precision: talaria_contracts::PrecisionTier::SecondVerified,
                        ts_ingested: d.ts_ingested,
                        ts_committed: now,
                        revision: 0,
                        settled: Some(true),
                    },
                ));
            }
            if let Some(oi) = d.open_interest {
                let _ = self.tx.send(ColdCmd::Derivative(
                    spec.clone(),
                    DerivativePoint {
                        instrument_id: d.instrument_id.clone(),
                        kind: DerivativeKind::OpenInterest,
                        ts_event: d.ts_event,
                        ts_published: None,
                        ts_available: ts_avail,
                        value: oi,
                        timeframe: None,
                        precision: talaria_contracts::PrecisionTier::SecondVerified,
                        ts_ingested: d.ts_ingested,
                        ts_committed: now,
                        revision: 0,
                        settled: None,
                    },
                ));
            }
        }
    }

    fn on_received_frame(&self, frame: &TransportFrame) {
        if self.cold_store.is_none() {
            return;
        }
        if let TransportFrame::ReceivedTrade(rt) = frame
            && let Some(spec) = self
                .instruments
                .read()
                .ok()
                .and_then(|m| m.get(&rt.event.instrument_id).cloned())
            && let Ok(trade) = rt.to_trade((self.now_ms)())
        {
            let _ = self.tx.send(ColdCmd::Trade(spec.clone(), trade));
        }
    }
}

fn drain_rx_into_pending(
    rx: &mut mpsc::UnboundedReceiver<ColdCmd>,
    pending_bars: &mut PendingBarsMap,
    pending_derivs: &mut PendingDerivsMap,
    pending_trades: &mut PendingTradesMap,
    control: &mut DrainedControl,
) {
    while let Ok(cmd) = rx.try_recv() {
        match cmd {
            ColdCmd::Bar {
                instrument,
                timeframe,
                bar,
            } => {
                let month = partition_month(bar.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                let key = (instrument.instrument_id.clone(), timeframe.clone(), month);
                pending_bars
                    .entry(key)
                    .or_insert_with(|| (instrument, timeframe, Vec::new()))
                    .2
                    .push(bar);
            }
            ColdCmd::Derivative(instrument, pt) => {
                let month = partition_month(pt.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                let key = (instrument.instrument_id.clone(), pt.kind, month);
                pending_derivs
                    .entry(key)
                    .or_insert_with(|| (instrument, pt.kind, Vec::new()))
                    .2
                    .push(pt);
            }
            ColdCmd::Trade(instrument, trade) => {
                let day = partition_day(trade.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                let key = (instrument.instrument_id.clone(), day);
                pending_trades
                    .entry(key)
                    .or_insert_with(|| (instrument, Vec::new()))
                    .1
                    .push(trade);
            }
            ColdCmd::Flush(done) => {
                control.flush_waiters.push(done);
            }
            ColdCmd::Compact(done) => {
                control.compact_waiters.push(done);
            }
            ColdCmd::Shutdown(done) => {
                control.shutdown_waiters.push(done);
                break;
            }
        }
    }
}

fn notify_waiters(waiters: Vec<oneshot::Sender<()>>) {
    for done in waiters {
        let _ = done.send(());
    }
}

async fn run_periodic_compact(
    cold_store: Option<&Arc<ColdStore>>,
    instruments: &Arc<RwLock<HashMap<String, InstrumentSpec>>>,
    now_ms: &NowFn,
    compact_task: &mut Option<JoinHandle<()>>,
) {
    if let Some(handle) = compact_task.take() {
        let _ = handle.await;
    }
    let Some(store) = cold_store else {
        return;
    };
    let now = (now_ms)();
    let store_check = Arc::clone(store);
    let need_compact = tokio::task::spawn_blocking(move || {
        store_check.has_compactable_work(Some(now)).unwrap_or(false)
    })
    .await
    .unwrap_or(false);
    if need_compact {
        let all_specs: Vec<InstrumentSpec> = instruments
            .read()
            .map(|m| m.values().cloned().collect())
            .unwrap_or_default();
        let store_c = Arc::clone(store);
        let _ = tokio::task::spawn_blocking(move || store_c.compact_periodic(&all_specs, Some(now)))
            .await;
    }
}

async fn run_shutdown_compact_and_close(
    cold_store: Option<&Arc<ColdStore>>,
    instruments: &Arc<RwLock<HashMap<String, InstrumentSpec>>>,
    compact_task: &mut Option<JoinHandle<()>>,
) {
    if let Some(handle) = compact_task.take() {
        let _ = handle.await;
    }
    let Some(store) = cold_store else {
        return;
    };
    let all_specs: Vec<InstrumentSpec> = instruments
        .read()
        .map(|m| m.values().cloned().collect())
        .unwrap_or_default();
    let store_c = Arc::clone(store);
    let _ = tokio::task::spawn_blocking(move || {
        let _ = store_c.compact(&all_specs, None);
        let _ = store_c.close();
    })
    .await;
}

#[allow(clippy::too_many_arguments)]
async fn flush_pending(
    cold_store: Option<&Arc<ColdStore>>,
    reader: &Arc<ColdReader>,
    strategy_stream: Option<&Arc<StrategyStream>>,
    key_levels: &Arc<KeyLevelRegistry>,
    structures: &Arc<StructureRegistry>,
    now_ms: &NowFn,
    pending_bars: &mut PendingBarsMap,
    pending_derivs: &mut PendingDerivsMap,
    pending_trades: &mut PendingTradesMap,
) -> bool {
    let wrote_bars = flush_bars(
        cold_store,
        reader,
        strategy_stream,
        key_levels,
        structures,
        now_ms,
        pending_bars,
    )
    .await;
    let wrote_bulk = flush_bulk(cold_store, strategy_stream, pending_derivs, pending_trades).await;
    wrote_bars || wrote_bulk
}

#[allow(clippy::too_many_arguments)]
async fn flush_bars(
    cold_store: Option<&Arc<ColdStore>>,
    reader: &Arc<ColdReader>,
    strategy_stream: Option<&Arc<StrategyStream>>,
    key_levels: &Arc<KeyLevelRegistry>,
    structures: &Arc<StructureRegistry>,
    now_ms: &NowFn,
    pending_bars: &mut PendingBarsMap,
) -> bool {
    let Some(store) = cold_store else {
        pending_bars.clear();
        return false;
    };
    if pending_bars.is_empty() {
        return false;
    }

    let mut wrote_any = false;
    let batches: Vec<_> = pending_bars.drain().map(|(_, v)| v).collect();
    for (instrument, timeframe, mut bars) in batches {
        bars.sort_by_key(|b| (b.ts_event, b.revision));
        let min_ts = bars.first().map(|b| b.ts_event).unwrap_or(0);
        let max_ts = bars.last().map(|b| b.ts_event).unwrap_or(0);
        let max_avail = bars.iter().map(|b| b.ts_available).max().unwrap_or(0);
        let store_c = Arc::clone(store);
        let inst_c = instrument.clone();
        let tf_c = timeframe.clone();
        let res =
            tokio::task::spawn_blocking(move || store_c.publish_bars(&inst_c, &tf_c, &bars)).await;
        match res {
            Ok(Ok(_)) => {
                wrote_any = true;
                if let Some(stream) = strategy_stream {
                    stream.changed(
                        StrategyKey::Bar(instrument.instrument_id.clone(), timeframe.clone()),
                        min_ts,
                        Some(max_ts),
                        None,
                    );
                }
                // Refresh KeyLevelRegistry and StructureRegistry anchored to the latest visible series
                // timestamp (handling historical replays and long market closures outside wall-clock 900x).
                let reader_c = Arc::clone(reader);
                let levels_c = Arc::clone(key_levels);
                let structs_c = Arc::clone(structures);
                let now = now_ms();
                let as_of = now.max(max_avail);
                let inst_c = instrument.clone();
                let tf_c = timeframe.clone();
                let _ = tokio::task::spawn_blocking(move || {
                    let gens = reader_c.generations(&inst_c, &tf_c).unwrap_or_default();
                    let anchor_ts = gens
                        .iter()
                        .map(|g| g.last_ts)
                        .filter(|&ts| ts <= as_of)
                        .max()
                        .unwrap_or(max_ts);
                    let start_ts = talaria_contracts::timeframe_ms(&tf_c)
                        .ok()
                        .map(|iv| anchor_ts.saturating_sub(iv.saturating_mul(900)).max(0));
                    let mut raw = reader_c
                        .read_bars(&inst_c, &tf_c, start_ts, None, Some(as_of))
                        .unwrap_or_default();
                    if raw.is_empty()
                        && start_ts.is_some_and(|s| gens.iter().any(|g| g.first_ts < s))
                    {
                        raw = reader_c
                            .read_bars(&inst_c, &tf_c, None, None, Some(as_of))
                            .unwrap_or_default();
                    }
                    let mut compact = Vec::with_capacity(raw.len().min(600));
                    for r in raw.iter().rev().take(600).collect::<Vec<_>>().into_iter().rev() {
                        let status = talaria_contracts::classify_flat_bar(
                            r.open, r.high, r.low, r.close, r.volume,
                        );
                        if let Ok(c) = r.to_compact(status) {
                            compact.push(c);
                        }
                    }
                    if !compact.is_empty() {
                        let cutoff = compact.last().map(|b| b.ts_available).unwrap_or(as_of);
                        let snapshot_id =
                            format!("live-{}-{}-{}", inst_c.instrument_id, tf_c, cutoff);
                        let _ = levels_c.compute_and_register(
                            snapshot_id.clone(),
                            &inst_c.instrument_id,
                            &tf_c,
                            &compact,
                            cutoff,
                            now,
                            None,
                            None,
                            &[],
                        );
                        let _ = structs_c.compute_and_register(
                            snapshot_id,
                            &inst_c.instrument_id,
                            &tf_c,
                            &compact,
                            cutoff,
                            now,
                            None,
                            None,
                            &[],
                        );
                    }
                })
                .await;
            }
            Ok(Err(err)) => {
                tracing::warn!(instrument = %instrument.instrument_id, timeframe = %timeframe, error = %err, "cold bar publish failed");
            }
            Err(err) => {
                tracing::warn!(error = %err, "cold bar publish task panicked");
            }
        }
    }
    wrote_any
}

async fn flush_bulk(
    cold_store: Option<&Arc<ColdStore>>,
    strategy_stream: Option<&Arc<StrategyStream>>,
    pending_derivs: &mut PendingDerivsMap,
    pending_trades: &mut PendingTradesMap,
) -> bool {
    let Some(store) = cold_store else {
        pending_derivs.clear();
        pending_trades.clear();
        return false;
    };

    let mut wrote_any = false;

    if !pending_derivs.is_empty() {
        let batches: Vec<_> = pending_derivs.drain().map(|(_, v)| v).collect();
        for (instrument, kind, rows) in batches {
            let store_c = Arc::clone(store);
            let inst_c = instrument.clone();
            let res = tokio::task::spawn_blocking(move || {
                store_c.publish_derivatives_reconciled(&inst_c, kind, &rows)
            })
            .await;
            if let Ok(Ok(Some(gen_info))) = res {
                wrote_any = true;
                if let Some(stream) = strategy_stream {
                    stream.derivative_changed(
                        &instrument.instrument_id,
                        kind,
                        gen_info.first_ts,
                        gen_info.last_ts,
                    );
                }
            }
        }
    }

    if !pending_trades.is_empty() {
        let batches: Vec<_> = pending_trades.drain().map(|(_, v)| v).collect();
        for (instrument, rows) in batches {
            let store_c = Arc::clone(store);
            let inst_c = instrument.clone();
            let res = tokio::task::spawn_blocking(move || store_c.publish_trades(&inst_c, &rows))
                .await;
            if let Ok(Ok(_)) = res {
                wrote_any = true;
            }
        }
    }

    wrote_any
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scanner::ScannerConfig;
    use rust_decimal::Decimal;
    use talaria_contracts::types::PrecisionTier;
    use talaria_packs::{DEFAULT_PACK_NAME, get_market_pack};
    use tempfile::TempDir;

    const BASE_TS: i64 = 1_767_225_600_000; // 2026-01-01T00:00:00Z
    const MINUTE: i64 = 60_000;
    const DAY: i64 = 86_400_000;

    fn test_bar(ts: i64, close_val: i64) -> RawBar {
        let price = Decimal::from(close_val);
        let vol = Decimal::from(10);
        RawBar {
            symbol: "BTCUSDT".to_owned(),
            timeframe: "1m".to_owned(),
            ts_event: ts,
            open: price,
            high: price + Decimal::from(5),
            low: price - Decimal::from(5),
            close: price,
            volume: vol,
            turnover: price * vol,
            ts_available: ts + MINUTE,
            ts_ingested: ts + MINUTE,
            ts_committed: ts + MINUTE,
            precision: PrecisionTier::SecondVerified,
            revision: 0,
            confirmed: true,
            history_only: false,
            synthetic: false,
        }
    }

    #[tokio::test]
    async fn concurrent_flush_and_compact_persists_and_compacts() {
        let tmp = TempDir::new().unwrap();
        let spec = get_market_pack(DEFAULT_PACK_NAME)
            .unwrap()
            .instrument("BTC-USDT-PERP")
            .unwrap()
            .clone();
        // Wall clock 45 days later so 2026-01 is a past month eligible for periodic compaction.
        let now_fn: NowFn = Arc::new(|| BASE_TS + 45 * DAY);
        let store = Arc::new(ColdStore::open(tmp.path()).unwrap());
        let reader = Arc::new(ColdReader::new(tmp.path()));
        let hot = Arc::new(
            HotStreamBuffer::with_default_capacity(
                std::slice::from_ref(&spec),
                Arc::clone(&now_fn),
            )
            .unwrap(),
        );
        let desk = Arc::new(DeskBroadcast::new());
        let kl = Arc::new(KeyLevelRegistry::new(Arc::clone(&now_fn)));
        let st = Arc::new(StructureRegistry::new(Arc::clone(&now_fn)));
        let alerts = Arc::new(AlertEngine::new(Arc::clone(&now_fn), 600));
        let scanner = Arc::new(ScannerEngine::new(
            Arc::clone(&now_fn),
            ScannerConfig::default(),
            Arc::new(|_, _, _, _| String::new()),
        ));
        let state_store =
            Arc::new(WorkbenchStateStore::open(tmp.path(), Arc::clone(&now_fn)).unwrap());

        let writer = ColdWriter::spawn(
            Arc::clone(&now_fn),
            std::slice::from_ref(&spec),
            Some(store),
            Arc::clone(&reader),
            hot,
            desk,
            None,
            kl,
            st,
            alerts,
            scanner,
            state_store,
        );

        // First generation tail file.
        writer.on_recovered_bar(&spec, &test_bar(BASE_TS, 50_000));
        writer.flush().await;
        assert_eq!(reader.generations(&spec, "1m").unwrap().len(), 1);

        // Second bar followed immediately by queued Flush + Compact commands.
        writer.on_recovered_bar(&spec, &test_bar(BASE_TS + MINUTE, 50_010));
        let (bars_after_flush, ()) = tokio::join!(
            async {
                writer.flush().await;
                reader.read_bars(&spec, "1m", None, None, None).unwrap()
            },
            writer.compact_periodic_now(),
        );

        assert_eq!(
            bars_after_flush.len(),
            2,
            "flush() must wait until newly queued bars are persisted"
        );
        let gens = reader.generations(&spec, "1m").unwrap();
        assert_eq!(
            gens.len(),
            1,
            "queued Compact must execute periodic compaction rather than being dropped"
        );
        assert!(
            gens[0]
                .parquet_path
                .file_name()
                .unwrap()
                .to_str()
                .unwrap()
                .starts_with("partition-"),
            "compacted generation should be merged into partition-*"
        );

        writer.shutdown().await;
    }
}
