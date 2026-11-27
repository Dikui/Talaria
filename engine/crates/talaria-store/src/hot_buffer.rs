//! Thread-safe in-memory sliding window buffer for live/unarchived kline revisions (`HotStreamBuffer`).

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Instant;

use talaria_contracts::{
    BarStatus, FeedEvent, InstrumentSpec, KlineEvent, RawBar, ReceivedKlineEvent,
};

use crate::error::{Result, StoreError};

/// Default per-series capacity matching Python `DEFAULT_MAX_BARS_PER_SERIES`.
pub const DEFAULT_MAX_BARS_PER_SERIES: usize = 5_000;

/// Recorded feed event in the hot buffer with a monotonic sequence timestamp.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SnapshotEvent {
    pub order_ns: u64,
    pub event: FeedEvent,
}

/// Thread-safe revision container for a single `(instrument_id, timeframe, ts_event)` slot.
#[derive(Debug, Default)]
pub struct HotSlot {
    versions: Mutex<BTreeMap<u64, RawBar>>,
}

impl HotSlot {
    pub fn new() -> Self {
        Self {
            versions: Mutex::new(BTreeMap::new()),
        }
    }

    /// Insert a bar revision if that revision number has not yet been recorded.
    pub fn add(&self, bar: RawBar) {
        if let Ok(mut guard) = self.versions.lock() {
            guard.entry(bar.revision).or_insert(bar);
        }
    }

    /// Return all revisions for this time slot sorted ascending by `revision`.
    pub fn revisions(&self) -> Vec<RawBar> {
        let guard = self.versions.lock().expect("HotSlot mutex poisoned");
        guard.values().cloned().collect()
    }
}

struct SeriesRing {
    slots: BTreeMap<i64, Arc<HotSlot>>,
    lru_order: VecDeque<i64>,
    touch_seq: HashMap<i64, u64>,
    next_seq: u64,
}

impl SeriesRing {
    fn new() -> Self {
        Self {
            slots: BTreeMap::new(),
            lru_order: VecDeque::new(),
            touch_seq: HashMap::new(),
            next_seq: 0,
        }
    }

    fn get_or_insert(&mut self, ts_event: i64, max_bars: usize) -> Arc<HotSlot> {
        self.next_seq += 1;
        let seq = self.next_seq;
        self.touch_seq.insert(ts_event, seq);
        self.lru_order.push_back(ts_event);

        let slot = self
            .slots
            .entry(ts_event)
            .or_insert_with(|| Arc::new(HotSlot::new()))
            .clone();

        while self.slots.len() > max_bars {
            if let Some(candidate) = self.lru_order.pop_front() {
                // Only evict if this pop corresponds to the latest touch for `candidate`.
                let is_latest = self
                    .touch_seq
                    .get(&candidate)
                    .is_some_and(|&_s| !self.lru_order.contains(&candidate));
                if is_latest {
                    self.touch_seq.remove(&candidate);
                    self.slots.remove(&candidate);
                }
            } else {
                break;
            }
        }
        slot
    }
}

struct BufferState {
    series: HashMap<(String, String), SeriesRing>,
    events: HashMap<String, VecDeque<SnapshotEvent>>,
    series_versions: HashMap<(String, String), u64>,
}

/// Thread-safe in-memory sliding window buffer retaining recent kline revisions per series.
pub struct HotStreamBuffer {
    symbols: RwLock<HashMap<String, String>>,
    now_ms: Arc<dyn Fn() -> i64 + Send + Sync>,
    max_bars: usize,
    epoch: String,
    started_at: Instant,
    state: Mutex<BufferState>,
}

impl HotStreamBuffer {
    /// Construct a `HotStreamBuffer` with an injectable millisecond clock and capacity limit.
    pub fn new(
        instruments: &[InstrumentSpec],
        now_ms: Arc<dyn Fn() -> i64 + Send + Sync>,
        max_bars_per_series: usize,
    ) -> Result<Self> {
        if max_bars_per_series == 0 {
            return Err(StoreError::Store(
                "max_bars_per_series must be positive".to_owned(),
            ));
        }
        let symbols = instruments
            .iter()
            .map(|spec| (spec.instrument_id.clone(), spec.symbol.clone()))
            .collect();
        let epoch_seed = now_ms();
        let epoch = format!("{:016x}{:08x}", epoch_seed as u64, std::process::id());
        Ok(Self {
            symbols: RwLock::new(symbols),
            now_ms,
            max_bars: max_bars_per_series,
            epoch,
            started_at: Instant::now(),
            state: Mutex::new(BufferState {
                series: HashMap::new(),
                events: HashMap::new(),
                series_versions: HashMap::new(),
            }),
        })
    }

    /// Construct a `HotStreamBuffer` with `DEFAULT_MAX_BARS_PER_SERIES`.
    pub fn with_default_capacity(
        instruments: &[InstrumentSpec],
        now_ms: Arc<dyn Fn() -> i64 + Send + Sync>,
    ) -> Result<Self> {
        Self::new(instruments, now_ms, DEFAULT_MAX_BARS_PER_SERIES)
    }

    /// Dynamically register a newly added instrument so live klines can be buffered immediately.
    pub fn register_instrument(&self, spec: &InstrumentSpec) {
        if let Ok(mut guard) = self.symbols.write() {
            guard.insert(spec.instrument_id.clone(), spec.symbol.clone());
        }
    }

    /// Ingest a transport-layer `ReceivedKlineEvent` directly.
    pub fn ingest_received_kline(&self, received: &ReceivedKlineEvent) -> Option<RawBar> {
        let now = (self.now_ms)();
        let order_ns = self.started_at.elapsed().as_nanos() as u64;
        {
            let mut guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
            let queue = guard
                .events
                .entry(received.event.instrument_id.clone())
                .or_insert_with(|| VecDeque::with_capacity(self.max_bars));
            if queue.len() >= self.max_bars {
                queue.pop_front();
            }
            queue.push_back(SnapshotEvent {
                order_ns,
                event: FeedEvent::Kline(received.event.clone()),
            });
        }
        self.ingest_kline_inner(
            &received.event,
            received.ts_ingested,
            received.history_only,
            received.synthetic,
            now,
        )
    }

    /// Ingest any `FeedEvent`, matching Python `HotStreamBuffer.ingest`.
    pub fn ingest(&self, event: &FeedEvent) -> Option<RawBar> {
        if matches!(event, FeedEvent::Trade(_)) {
            return None;
        }
        let now = (self.now_ms)();
        let order_ns = self.started_at.elapsed().as_nanos() as u64;
        let instrument_id = match event {
            FeedEvent::Kline(e) => &e.instrument_id,
            FeedEvent::OrderBook(e) => &e.instrument_id,
            FeedEvent::DerivativeTick(e) => &e.instrument_id,
            FeedEvent::Trade(e) => &e.instrument_id,
            FeedEvent::Health(e) => &e.instrument_id,
        };
        {
            let mut guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
            let queue = guard
                .events
                .entry(instrument_id.clone())
                .or_insert_with(|| VecDeque::with_capacity(self.max_bars));
            if queue.len() >= self.max_bars {
                queue.pop_front();
            }
            queue.push_back(SnapshotEvent {
                order_ns,
                event: event.clone(),
            });
        }
        let FeedEvent::Kline(kline) = event else {
            return None;
        };
        self.ingest_kline_inner(kline, now, false, false, now)
    }

    fn ingest_kline_inner(
        &self,
        kline: &KlineEvent,
        ts_ingested: i64,
        history_only: bool,
        synthetic: bool,
        now: i64,
    ) -> Option<RawBar> {
        let bar = &kline.bar;
        if bar.status == BarStatus::FeedGap {
            return None;
        }
        let (Some(open), Some(high), Some(low), Some(close), Some(volume), Some(turnover)) = (
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
            bar.turnover,
        ) else {
            return None;
        };
        let symbol = self
            .symbols
            .read()
            .ok()?
            .get(&kline.instrument_id)?
            .clone();
        let raw = RawBar {
            symbol,
            timeframe: kline.timeframe.clone(),
            ts_event: bar.timestamp,
            open,
            high,
            low,
            close,
            volume,
            turnover,
            ts_available: bar.ts_available,
            ts_ingested,
            ts_committed: now.max(ts_ingested),
            precision: bar.precision,
            revision: kline.revision,
            confirmed: kline.confirmed,
            history_only,
            synthetic,
        };
        let key = (kline.instrument_id.clone(), kline.timeframe.clone());
        let mut guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
        let max_bars = self.max_bars;
        let slot = guard
            .series
            .entry(key.clone())
            .or_insert_with(SeriesRing::new)
            .get_or_insert(raw.ts_event, max_bars);
        slot.add(raw.clone());
        let ver = guard.series_versions.entry(key).or_insert(0);
        *ver += 1;
        Some(raw)
    }

    /// Return recent buffered `SnapshotEvent`s for `instrument_id`.
    pub fn recent_events(&self, instrument_id: &str) -> Vec<SnapshotEvent> {
        let guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
        guard
            .events
            .get(instrument_id)
            .map(|q| q.iter().cloned().collect())
            .unwrap_or_default()
    }

    /// Return an opaque version string `{epoch}:{seq}` for cache invalidation.
    pub fn series_version(&self, instrument_id: &str, timeframe: &str) -> String {
        let guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
        let seq = guard
            .series_versions
            .get(&(instrument_id.to_owned(), timeframe.to_owned()))
            .copied()
            .unwrap_or(0);
        format!("{}:{seq}", self.epoch)
    }

    /// Look up the pinned `HotSlot` for `(instrument_id, timeframe, ts_event)`.
    pub fn slot(&self, instrument_id: &str, timeframe: &str, ts_event: i64) -> Option<Arc<HotSlot>> {
        let guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
        guard
            .series
            .get(&(instrument_id.to_owned(), timeframe.to_owned()))
            .and_then(|ring| ring.slots.get(&ts_event).cloned())
    }

    /// Return all stored revisions in `(ts_event, revision)` order over `[start_ts, end_ts]`.
    pub fn revisions(
        &self,
        instrument_id: &str,
        timeframe: &str,
        start_ts: i64,
        end_ts: Option<i64>,
    ) -> Vec<RawBar> {
        if end_ts == Some(start_ts) {
            return self
                .slot(instrument_id, timeframe, start_ts)
                .map(|s| s.revisions())
                .unwrap_or_default();
        }
        let slots: Vec<Arc<HotSlot>> = {
            let guard = self.state.lock().expect("HotStreamBuffer mutex poisoned");
            let Some(ring) = guard
                .series
                .get(&(instrument_id.to_owned(), timeframe.to_owned()))
            else {
                return Vec::new();
            };
            let upper = end_ts.unwrap_or(i64::MAX);
            if start_ts > upper {
                return Vec::new();
            }
            ring.slots
                .range(start_ts..=upper)
                .map(|(_, s)| s.clone())
                .collect()
        };
        let mut out = Vec::new();
        for slot in slots {
            out.extend(slot.revisions());
        }
        out
    }

    /// Return snapshot-ready bars matching Python `HotStreamBuffer.recent_bars`.
    pub fn recent_bars(&self, instrument_id: &str, timeframe: &str) -> Vec<RawBar> {
        let versions = self.revisions(instrument_id, timeframe, 0, None);
        let mut selected: BTreeMap<(i64, i64, i128), RawBar> = BTreeMap::new();
        let mut confirmed: HashSet<i64> = HashSet::new();

        for mut bar in versions {
            if confirmed.contains(&bar.ts_event) {
                if !bar.confirmed {
                    continue;
                }
                bar.ts_available = bar.ts_available.max(bar.ts_committed);
            }
            let mut key = (bar.ts_event, bar.ts_available, -1_i128);
            if bar.confirmed {
                confirmed.insert(bar.ts_event);
                selected.remove(&key);
                key = (bar.ts_event, bar.ts_available, i128::from(bar.revision));
            }
            selected.insert(key, bar);
        }
        selected.into_values().collect()
    }
}
