//! Asynchronous Cold Store writer and `BarPublishSink` bridge.
//!
//! Connects `talaria-feed`'s `FeedSupervisor` to:
//! - `HotStreamBuffer` (in-memory zero-latency streaming tail)
//! - `DeskBroadcast` & `StrategyStream` (real-time WebSocket fanout)
//! - `ColdStore` (month/day partitioned Parquet generation commits)
//! - `KeyLevelRegistry`, `AlertEngine`, and `ScannerEngine`

use std::collections::{BTreeMap, HashMap};
use std::sync::{Arc, Mutex};
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
use crate::ws::desk::DeskBroadcast;
use crate::ws::strategy::{StrategyKey, StrategyStream};

type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type PendingBarsMap = HashMap<(String, String, String), (InstrumentSpec, String, Vec<RawBar>)>;
type PendingDerivsMap =
    HashMap<(String, DerivativeKind, String), (InstrumentSpec, DerivativeKind, Vec<DerivativePoint>)>;
type PendingTradesMap = HashMap<(String, String), (InstrumentSpec, Vec<Trade>)>;

enum ColdCmd {
    Bar {
        instrument: InstrumentSpec,
        timeframe: String,
        bar: RawBar,
    },
    Derivative(InstrumentSpec, DerivativePoint),
    Trade(InstrumentSpec, Trade),
    Flush(oneshot::Sender<()>),
    Shutdown(oneshot::Sender<()>),
}

pub struct ColdWriter {
    now_ms: NowFn,
    instruments: HashMap<String, InstrumentSpec>,
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
        alerts: Arc<AlertEngine>,
        scanner: Arc<ScannerEngine>,
        state_store: Arc<WorkbenchStateStore>,
    ) -> Arc<Self> {
        let mut inst_map = HashMap::with_capacity(instruments.len());
        for spec in instruments {
            inst_map.insert(spec.instrument_id.clone(), spec.clone());
        }
        let (tx, mut rx) = mpsc::unbounded_channel();

        let w_store = cold_store.clone();
        let w_reader = reader;
        let w_strategy = strategy_stream.clone();
        let w_levels = Arc::clone(&key_levels);
        let w_now = Arc::clone(&now_ms);
        let w_instruments = inst_map.clone();

        let handle = tokio::spawn(async move {
            let mut pending_bars: PendingBarsMap = HashMap::new();
            let mut pending_derivs: PendingDerivsMap = HashMap::new();
            let mut pending_trades: PendingTradesMap = HashMap::new();

            let mut flush_interval = tokio::time::interval(Duration::from_millis(50));
            flush_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

            loop {
                tokio::select! {
                    _ = flush_interval.tick() => {
                        flush_pending(
                            w_store.as_ref(),
                            &w_reader,
                            w_strategy.as_ref(),
                            &w_levels,
                            &w_now,
                            &mut pending_bars,
                            &mut pending_derivs,
                            &mut pending_trades,
                        ).await;
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
                                pending_derivs
                                    .entry(key)
                                    .or_insert_with(|| (instrument, pt.kind, Vec::new()))
                                    .2
                                    .push(pt);
                            }
                            Some(ColdCmd::Trade(instrument, trade)) => {
                                let day = partition_day(trade.ts_event).unwrap_or_else(|_| "unknown".to_owned());
                                let key = (instrument.instrument_id.clone(), day);
                                pending_trades
                                    .entry(key)
                                    .or_insert_with(|| (instrument, Vec::new()))
                                    .1
                                    .push(trade);
                            }
                            Some(ColdCmd::Flush(done)) => {
                                drain_rx_into_pending(&mut rx, &mut pending_bars, &mut pending_derivs, &mut pending_trades);
                                flush_pending(
                                    w_store.as_ref(),
                                    &w_reader,
                                    w_strategy.as_ref(),
                                    &w_levels,
                                    &w_now,
                                    &mut pending_bars,
                                    &mut pending_derivs,
                                    &mut pending_trades,
                                ).await;
                                let _ = done.send(());
                            }
                            Some(ColdCmd::Shutdown(done)) => {
                                drain_rx_into_pending(&mut rx, &mut pending_bars, &mut pending_derivs, &mut pending_trades);
                                flush_pending(
                                    w_store.as_ref(),
                                    &w_reader,
                                    w_strategy.as_ref(),
                                    &w_levels,
                                    &w_now,
                                    &mut pending_bars,
                                    &mut pending_derivs,
                                    &mut pending_trades,
                                ).await;
                                if let Some(store) = &w_store {
                                    let all_specs: Vec<InstrumentSpec> = w_instruments.values().cloned().collect();
                                    let store_c = Arc::clone(store);
                                    let _ = tokio::task::spawn_blocking(move || {
                                        let _ = store_c.compact(&all_specs, None);
                                        let _ = store_c.close();
                                    })
                                    .await;
                                }
                                let _ = done.send(());
                                break;
                            }
                            None => {
                                drain_rx_into_pending(&mut rx, &mut pending_bars, &mut pending_derivs, &mut pending_trades);
                                flush_pending(
                                    w_store.as_ref(),
                                    &w_reader,
                                    w_strategy.as_ref(),
                                    &w_levels,
                                    &w_now,
                                    &mut pending_bars,
                                    &mut pending_derivs,
                                    &mut pending_trades,
                                ).await;
                                if let Some(store) = &w_store {
                                    let all_specs: Vec<InstrumentSpec> = w_instruments.values().cloned().collect();
                                    let store_c = Arc::clone(store);
                                    let _ = tokio::task::spawn_blocking(move || {
                                        let _ = store_c.compact(&all_specs, None);
                                        let _ = store_c.close();
                                    })
                                    .await;
                                }
                                break;
                            }
                        }
                    }
                }
            }
        });

        Arc::new(Self {
            now_ms,
            instruments: inst_map,
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
            && let Some(spec) = self.instruments.get(&d.instrument_id)
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
            && let Some(spec) = self.instruments.get(&rt.event.instrument_id)
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
                let _ = done.send(());
            }
            ColdCmd::Shutdown(done) => {
                let _ = done.send(());
                break;
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
async fn flush_pending(
    cold_store: Option<&Arc<ColdStore>>,
    reader: &Arc<ColdReader>,
    strategy_stream: Option<&Arc<StrategyStream>>,
    key_levels: &Arc<KeyLevelRegistry>,
    now_ms: &NowFn,
    pending_bars: &mut PendingBarsMap,
    pending_derivs: &mut PendingDerivsMap,
    pending_trades: &mut PendingTradesMap,
) {
    let Some(store) = cold_store else {
        pending_bars.clear();
        pending_derivs.clear();
        pending_trades.clear();
        return;
    };

    if !pending_bars.is_empty() {
        let batches: Vec<_> = pending_bars.drain().map(|(_, v)| v).collect();
        for (instrument, timeframe, mut bars) in batches {
            bars.sort_by_key(|b| (b.ts_event, b.revision));
            let min_ts = bars.first().map(|b| b.ts_event).unwrap_or(0);
            let max_ts = bars.last().map(|b| b.ts_event).unwrap_or(0);
            let store_c = Arc::clone(store);
            let inst_c = instrument.clone();
            let tf_c = timeframe.clone();
            let res = tokio::task::spawn_blocking(move || {
                store_c.publish_bars(&inst_c, &tf_c, &bars)
            })
            .await;
            match res {
                Ok(Ok(_)) => {
                    if let Some(stream) = strategy_stream {
                        stream.changed(
                            StrategyKey::Bar(instrument.instrument_id.clone(), timeframe.clone()),
                            min_ts,
                            Some(max_ts),
                            None,
                        );
                    }
                    // Refresh KeyLevelRegistry from recent cold bars asynchronously
                    let reader_c = Arc::clone(reader);
                    let levels_c = Arc::clone(key_levels);
                    let now = now_ms();
                    let inst_c = instrument.clone();
                    let tf_c = timeframe.clone();
                    let _ = tokio::task::spawn_blocking(move || {
                        if let Ok(raw) = reader_c.read_bars(&inst_c, &tf_c, None, None, Some(now)) {
                            let mut compact = Vec::with_capacity(raw.len());
                            for r in raw.iter().rev().take(600).collect::<Vec<_>>().into_iter().rev() {
                                let status = talaria_contracts::classify_flat_bar(
                                    r.open, r.high, r.low, r.close, r.volume,
                                );
                                if let Ok(c) = r.to_compact(status) {
                                    compact.push(c);
                                }
                            }
                            if !compact.is_empty() {
                                let cutoff = compact.last().map(|b| b.ts_available).unwrap_or(now);
                                let snapshot_id = format!("live-{}-{}-{}", inst_c.instrument_id, tf_c, cutoff);
                                let _ = levels_c.compute_and_register(
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
    }

    if !pending_derivs.is_empty() {
        let batches: Vec<_> = pending_derivs.drain().map(|(_, v)| v).collect();
        for (instrument, kind, mut rows) in batches {
            rows.sort_by_key(|r| (r.ts_event, r.revision));
            let min_ts = rows.first().map(|r| r.ts_event).unwrap_or(0);
            let max_ts = rows.last().map(|r| r.ts_event).unwrap_or(0);
            let store_c = Arc::clone(store);
            let inst_c = instrument.clone();
            let res = tokio::task::spawn_blocking(move || {
                store_c.publish_derivatives(&inst_c, kind, &rows)
            })
            .await;
            if let Ok(Ok(_)) = res
                && let Some(stream) = strategy_stream
            {
                stream.derivative_changed(&instrument.instrument_id, kind, min_ts, max_ts);
            }
        }
    }

    if !pending_trades.is_empty() {
        let batches: Vec<_> = pending_trades.drain().map(|(_, v)| v).collect();
        for (instrument, rows) in batches {
            let store_c = Arc::clone(store);
            let inst_c = instrument.clone();
            let _ = tokio::task::spawn_blocking(move || {
                let _ = store_c.publish_trades(&inst_c, &rows);
            })
            .await;
        }
    }

    let _ = BTreeMap::<i64, ()>::new();
}
