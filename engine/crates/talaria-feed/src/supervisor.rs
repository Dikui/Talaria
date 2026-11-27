//! `FeedSupervisor` state machine (`LIVE -> STALE -> RECOVERING -> LIVE`), gap/no-trades synthesis,
//! revision deduplication, broadcast channel fan-out, and `BarPublishSink` integration.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use rust_decimal::Decimal;
use talaria_contracts::{
    BarStatus, CompactBar, FeedEvent, FeedHealthEvent, FeedHealthReport, FeedHealthStatus,
    InstrumentSpec, KlineEvent, PrecisionTier, RawBar, TransportEvent, classify_flat_bar,
    timeframe_ms, worst_status,
};
use talaria_packs::{
    BackfillWindow, FeedTransport, HistoryConnector, TransportFrame, backfill_bars,
};
use tokio::sync::{Mutex as AsyncMutex, broadcast, mpsc, watch};
use tokio::task::JoinHandle;
use tracing::warn;

use crate::clock::FeedClock;
use crate::error::FeedError;
use crate::sink::{BarPublishSink, NoopBarPublishSink};

/// Default channel staleness threshold in UTC milliseconds.
pub const DEFAULT_STALE_AFTER_MS: i64 = 12_000;
/// Grace window after bar close timestamp where first confirmation keeps `ts_available == close_ts`.
pub const BAR_CLOSE_GRACE_MS: i64 = 5_000;
/// Default staleness tick interval in milliseconds.
pub const TICK_INTERVAL_MS: u64 = 500;
/// Default initial exponential backoff delay on transport loss.
pub const BACKOFF_INITIAL_MS: u64 = 200;
/// Default maximum exponential backoff delay on transport loss.
pub const BACKOFF_MAX_MS: u64 = 5_000;
/// Maximum historical bars requested during outage backfill.
pub const MAX_BACKFILL_BARS: usize = 1000;

/// Configuration parameters for [`FeedSupervisor`].
#[derive(Debug, Clone)]
pub struct FeedSupervisorConfig {
    pub stale_after_ms: i64,
    /// Optional staleness polling cadence (`None` disables background tick task for replay/unit loops).
    pub tick_interval: Option<Duration>,
    pub backoff_initial: Duration,
    pub backoff_max: Duration,
    pub broadcast_capacity: usize,
    pub persist_public_trades: bool,
}

impl Default for FeedSupervisorConfig {
    fn default() -> Self {
        Self {
            stale_after_ms: DEFAULT_STALE_AFTER_MS,
            tick_interval: Some(Duration::from_millis(TICK_INTERVAL_MS)),
            backoff_initial: Duration::from_millis(BACKOFF_INITIAL_MS),
            backoff_max: Duration::from_millis(BACKOFF_MAX_MS),
            broadcast_capacity: 4096,
            persist_public_trades: true,
        }
    }
}

type ChannelKey = (String, String);

#[derive(Debug, Clone)]
struct BarVersion {
    bar: CompactBar,
    confirmed: bool,
    revision: u64,
    source_confirmed: bool,
    received_ts: i64,
}

#[derive(Debug, Clone)]
struct ChannelState {
    instrument_id: String,
    timeframe: String,
    subscribed_ts: i64,
    status: FeedHealthStatus,
    last_event_ts: Option<i64>,
    last_activity_ts: Option<i64>,
    stale_since: Option<i64>,
    reconnect_count: u64,
}

impl ChannelState {
    fn health_event(&self, now: i64) -> FeedHealthEvent {
        FeedHealthEvent {
            instrument_id: self.instrument_id.clone(),
            timeframe: self.timeframe.clone(),
            status: self.status,
            ts: now,
            last_event_ts: self.last_event_ts,
            stale_since: self.stale_since,
            reconnect_count: self.reconnect_count,
        }
    }
}

enum SubscriptionOp {
    Subscribe(String, String),
    Unsubscribe(String, String),
    ForceReconnect,
}

enum PostNotice {
    Event(FeedEvent),
    ReceivedFrame(TransportFrame),
    Subscribe {
        instrument: InstrumentSpec,
        timeframe: String,
        subscribed_ts: i64,
    },
    BarClose {
        instrument: InstrumentSpec,
        timeframe: String,
        bar: CompactBar,
        received_ts: i64,
        revision: u64,
    },
    RecoveredBar {
        instrument: InstrumentSpec,
        bar: RawBar,
    },
}

struct SupervisorCore {
    instruments: BTreeMap<String, InstrumentSpec>,
    stale_after_ms: i64,
    persist_public_trades: bool,
    /// Insertion-ordered channel keys so `health().channels` matches subscription order.
    channel_order: Vec<ChannelKey>,
    channels: BTreeMap<ChannelKey, ChannelState>,
    series: BTreeMap<ChannelKey, BTreeMap<i64, BarVersion>>,
    outage_lost_at: Option<i64>,
    outage_anchors: BTreeMap<ChannelKey, i64>,
    outage_windows: BTreeMap<ChannelKey, Vec<(i64, i64)>>,
    resume_pending: BTreeSet<ChannelKey>,
}

impl SupervisorCore {
    fn new(
        instruments: Vec<InstrumentSpec>,
        stale_after_ms: i64,
        persist_public_trades: bool,
    ) -> Self {
        let mut by_id = BTreeMap::new();
        for spec in instruments {
            by_id.insert(spec.instrument_id.clone(), spec);
        }
        Self {
            instruments: by_id,
            stale_after_ms,
            persist_public_trades,
            channel_order: Vec::new(),
            channels: BTreeMap::new(),
            series: BTreeMap::new(),
            outage_lost_at: None,
            outage_anchors: BTreeMap::new(),
            outage_windows: BTreeMap::new(),
            resume_pending: BTreeSet::new(),
        }
    }

    fn subscribe(
        &mut self,
        instrument_id: &str,
        timeframe: &str,
        now: i64,
    ) -> Result<(bool, Vec<PostNotice>), FeedError> {
        let spec = self
            .instruments
            .get(instrument_id)
            .ok_or_else(|| FeedError::InstrumentNotFound(instrument_id.to_owned()))?
            .clone();
        timeframe_ms(timeframe).map_err(|_| {
            FeedError::InvalidRequest(format!("unsupported timeframe `{timeframe}`"))
        })?;

        let key = (instrument_id.to_owned(), timeframe.to_owned());
        if self.channels.contains_key(&key) {
            return Ok((false, Vec::new()));
        }

        let (status, last_activity_ts, stale_since) = match self.outage_lost_at {
            Some(lost_at) => {
                self.resume_pending.insert(key.clone());
                (FeedHealthStatus::Stale, None, Some(lost_at))
            }
            None => (FeedHealthStatus::Live, Some(now), None),
        };

        let channel = ChannelState {
            instrument_id: instrument_id.to_owned(),
            timeframe: timeframe.to_owned(),
            subscribed_ts: now,
            status,
            last_event_ts: None,
            last_activity_ts,
            stale_since,
            reconnect_count: 0,
        };
        let health = channel.health_event(now);
        self.channel_order.push(key.clone());
        self.channels.insert(key.clone(), channel);
        self.series.entry(key).or_default();

        let notices = vec![
            PostNotice::Event(FeedEvent::Health(health)),
            PostNotice::Subscribe {
                instrument: spec,
                timeframe: timeframe.to_owned(),
                subscribed_ts: now,
            },
        ];
        Ok((true, notices))
    }

    fn unsubscribe(&mut self, instrument_id: &str, timeframe: &str) -> bool {
        let key = (instrument_id.to_owned(), timeframe.to_owned());
        if self.channels.remove(&key).is_none() {
            return false;
        }
        self.channel_order.retain(|k| k != &key);
        true
    }

    fn ingest(&mut self, frame: TransportFrame, now: i64) -> Vec<PostNotice> {
        let mut notices = Vec::new();
        match &frame {
            TransportFrame::Event(TransportEvent::Kline(ev)) => {
                self.accept_kline(ev.clone(), None, now, &mut notices);
            }
            TransportFrame::ReceivedKline(rk) => {
                self.accept_kline(rk.event.clone(), Some(rk.ts_ingested), now, &mut notices);
            }
            TransportFrame::Event(TransportEvent::OrderBook(ev)) => {
                if self.accept_instrument_event(&ev.instrument_id, now, None, &mut notices) {
                    notices.push(PostNotice::ReceivedFrame(frame.clone()));
                    notices.push(PostNotice::Event(FeedEvent::OrderBook(ev.clone())));
                }
            }
            TransportFrame::ReceivedOrderBook(rb) => {
                if self.accept_instrument_event(&rb.event.instrument_id, now, None, &mut notices) {
                    notices.push(PostNotice::ReceivedFrame(frame.clone()));
                    notices.push(PostNotice::Event(FeedEvent::OrderBook(rb.event.clone())));
                }
            }
            TransportFrame::Event(TransportEvent::DerivativeTick(ev)) => {
                if self.accept_instrument_event(
                    &ev.instrument_id,
                    now,
                    Some(ev.ts_event),
                    &mut notices,
                ) {
                    notices.push(PostNotice::ReceivedFrame(frame.clone()));
                    notices.push(PostNotice::Event(FeedEvent::DerivativeTick(ev.clone())));
                }
            }
            TransportFrame::Event(TransportEvent::Trade(ev)) => {
                if self.accept_instrument_event(&ev.instrument_id, now, None, &mut notices)
                    && self.persist_public_trades
                {
                    notices.push(PostNotice::ReceivedFrame(frame.clone()));
                    notices.push(PostNotice::Event(FeedEvent::Trade(ev.clone())));
                }
            }
            TransportFrame::ReceivedTrade(rt) => {
                if self.accept_instrument_event(&rt.event.instrument_id, now, None, &mut notices)
                    && self.persist_public_trades
                {
                    notices.push(PostNotice::ReceivedFrame(frame.clone()));
                    notices.push(PostNotice::Event(FeedEvent::Trade(rt.event.clone())));
                }
            }
        }
        notices
    }

    fn heartbeat(&mut self, now: i64) -> Vec<PostNotice> {
        let mut notices = Vec::new();
        for key in &self.channel_order {
            if let Some(channel) = self.channels.get_mut(key) {
                let prev = channel.last_activity_ts.unwrap_or(now);
                channel.last_activity_ts = Some(prev.max(now));
                if channel.status == FeedHealthStatus::Stale {
                    channel.status = FeedHealthStatus::Live;
                    channel.stale_since = None;
                    notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
                }
            }
        }
        notices
    }

    fn transport_lost(&mut self, now: i64) -> Vec<PostNotice> {
        if self.channels.is_empty() {
            return Vec::new();
        }
        if self.outage_lost_at.is_none() {
            self.outage_lost_at = Some(now);
        }
        let mut notices = Vec::new();
        for key in &self.channel_order {
            let Some(channel) = self.channels.get_mut(key) else {
                continue;
            };
            if let Some(series) = self.series.get(key)
                && let Some(&max_ts) = series.keys().next_back()
            {
                self.outage_anchors.entry(key.clone()).or_insert(max_ts);
            }
            self.resume_pending.insert(key.clone());
            if channel.status == FeedHealthStatus::Stale {
                continue;
            }
            channel.status = FeedHealthStatus::Stale;
            channel.stale_since = Some(channel.last_activity_ts.unwrap_or(now));
            notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
            warn!(
                "feed transport lost: {} {}",
                channel.instrument_id, channel.timeframe
            );
        }
        notices
    }

    fn transport_restored(&mut self, now: i64, backfill: &[RawBar]) -> Vec<PostNotice> {
        let mut notices = Vec::new();
        if let Some(lost_at) = self.outage_lost_at.take() {
            let elapsed = (now - lost_at).max(0);
            let anchors = std::mem::take(&mut self.outage_anchors);
            for (key, anchor) in anchors {
                self.outage_windows
                    .entry(key)
                    .or_default()
                    .push((anchor, anchor + elapsed));
            }
        }

        for key in &self.channel_order {
            if let Some(channel) = self.channels.get_mut(key) {
                channel.status = FeedHealthStatus::Recovering;
                channel.reconnect_count += 1;
                notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
            }
        }

        for bar in backfill {
            let Some(key) = self.key_for_symbol(&bar.symbol, &bar.timeframe) else {
                continue;
            };
            let already_confirmed = self
                .series
                .get(&key)
                .and_then(|s| s.get(&bar.ts_event))
                .is_some_and(|rec| rec.source_confirmed);
            if already_confirmed {
                continue;
            }
            let status = classify_flat_bar(bar.open, bar.high, bar.low, bar.close, bar.volume);
            let Ok(compact) = bar.to_compact(status) else {
                continue;
            };
            self.apply_bar(
                &key,
                compact,
                true,
                now,
                true,
                Some(bar.ts_ingested),
                true,
                &mut notices,
            );
            let revision = self
                .series
                .get(&key)
                .and_then(|s| s.get(&bar.ts_event))
                .map_or(0, |r| r.revision);
            let mut recovered = bar.clone();
            recovered.revision = revision;
            if let Some(spec) = self.instruments.get(&key.0) {
                notices.push(PostNotice::RecoveredBar {
                    instrument: spec.clone(),
                    bar: recovered,
                });
            }
        }

        let keys = self.channel_order.clone();
        for key in &keys {
            self.fill_outage_windows(key, now, &mut notices);
            if let Some(channel) = self.channels.get_mut(key) {
                channel.status = FeedHealthStatus::Live;
                channel.stale_since = None;
                channel.last_activity_ts = Some(now);
                notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
            }
        }

        notices
    }

    fn tick(&mut self, now: i64) -> (Vec<PostNotice>, bool) {
        let mut notices = Vec::new();
        for key in &self.channel_order {
            let Some(channel) = self.channels.get_mut(key) else {
                continue;
            };
            if channel.status != FeedHealthStatus::Live {
                continue;
            }
            let since = channel.last_activity_ts.unwrap_or(channel.subscribed_ts);
            if now - since >= self.stale_after_ms {
                channel.status = FeedHealthStatus::Stale;
                channel.stale_since = Some(since);
                notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
            }
        }
        // Trigger an automatic transport reconnect if every subscribed channel has remained STALE
        // for an additional `stale_after_ms` beyond the initial staleness threshold (i.e. 2x total
        // silence) while no outage reconnect is already underway.
        let reconnect_threshold = self.stale_after_ms.saturating_mul(2);
        let should_force_reconnect = self.outage_lost_at.is_none()
            && !self.channels.is_empty()
            && self.channels.values().all(|ch| {
                ch.status == FeedHealthStatus::Stale
                    && ch
                        .stale_since
                        .is_some_and(|since| now.saturating_sub(since) >= reconnect_threshold)
            });
        (notices, should_force_reconnect)
    }

    fn health(&self, feed_mode: &str, now: i64) -> FeedHealthReport {
        let channels: Vec<FeedHealthEvent> = self
            .channel_order
            .iter()
            .filter_map(|key| self.channels.get(key))
            .map(|c| c.health_event(now))
            .collect();
        let status = worst_status(channels.iter().map(|c| c.status));
        FeedHealthReport {
            feed_mode: feed_mode.to_owned(),
            status,
            channels,
        }
    }

    fn channel_health(&self, instrument_id: &str, timeframe: &str, now: i64) -> Option<FeedHealthEvent> {
        self.channels
            .get(&(instrument_id.to_owned(), timeframe.to_owned()))
            .map(|c| c.health_event(now))
    }

    fn recent_bars(
        &self,
        instrument_id: &str,
        timeframe: &str,
        after_ts: Option<i64>,
        confirmed_only: bool,
    ) -> Vec<CompactBar> {
        let Some(series) = self
            .series
            .get(&(instrument_id.to_owned(), timeframe.to_owned()))
        else {
            return Vec::new();
        };
        series
            .iter()
            .filter(|(ts, rec)| {
                after_ts.is_none_or(|after| **ts > after) && (!confirmed_only || rec.confirmed)
            })
            .map(|(_, rec)| rec.bar.clone())
            .collect()
    }

    fn accept_kline(
        &mut self,
        event: KlineEvent,
        received_ts: Option<i64>,
        now: i64,
        notices: &mut Vec<PostNotice>,
    ) {
        let key = (event.instrument_id.clone(), event.timeframe.clone());
        if !self.channels.contains_key(&key) {
            return;
        }
        let previous_ts = self
            .series
            .get(&key)
            .and_then(|s| s.keys().next_back().copied());
        if let Some(prev) = previous_ts
            && event.bar.timestamp > prev
            && let Ok(interval) = timeframe_ms(&key.1)
        {
            let force_gap = self.resume_pending.contains(&key);
            self.fill_missing(&key, prev + interval, event.bar.timestamp, now, force_gap, notices);
        }
        self.resume_pending.remove(&key);
        self.apply_bar(
            &key,
            event.bar,
            event.confirmed,
            now,
            true,
            received_ts,
            false,
            notices,
        );
    }

    fn accept_instrument_event(
        &mut self,
        instrument_id: &str,
        now: i64,
        event_ts: Option<i64>,
        notices: &mut Vec<PostNotice>,
    ) -> bool {
        let has_inst = self
            .channels
            .values()
            .any(|c| c.instrument_id == instrument_id);
        if !has_inst {
            return false;
        }
        self.mark_activity(instrument_id, now, None, event_ts, notices);
        true
    }

    #[allow(clippy::too_many_arguments)]
    fn apply_bar(
        &mut self,
        key: &ChannelKey,
        mut bar: CompactBar,
        confirmed: bool,
        now: i64,
        source: bool,
        received_ts: Option<i64>,
        recovered: bool,
        notices: &mut Vec<PostNotice>,
    ) {
        let mut received = received_ts.unwrap_or(now);
        let source_confirmed =
            source && confirmed && !recovered && bar.status != BarStatus::FeedGap;
        let existing = self.series.get(key).and_then(|s| s.get(&bar.timestamp)).cloned();

        if (recovered || source_confirmed)
            && let Ok(interval) = timeframe_ms(&key.1)
        {
            let close_ts = bar.timestamp + interval;
            let available = if recovered {
                close_ts
            } else if let Some(rec) = &existing
                && rec.source_confirmed
            {
                let previous = rec.bar.ts_available;
                let mut candidate = bar.clone();
                candidate.ts_available = previous;
                if candidate == rec.bar {
                    previous
                } else {
                    previous.max(received)
                }
            } else if received <= close_ts + BAR_CLOSE_GRACE_MS {
                close_ts
            } else {
                received
            };
            bar.ts_available = available;
        }

        let mut same_version = false;
        if let Some(rec) = &existing {
            if rec.source_confirmed && !confirmed {
                return;
            }
            let same_kind = rec.confirmed == confirmed && rec.source_confirmed == source_confirmed;
            same_version = same_kind && rec.bar == bar;
        }

        let mut revision = existing.as_ref().map_or(0, |r| r.revision + 1);
        if let Some(rec) = &existing
            && same_version
        {
            revision = rec.revision;
            received = rec.received_ts;
        }

        if !same_version {
            let series = self.series.entry(key.clone()).or_default();
            series.insert(
                bar.timestamp,
                BarVersion {
                    bar: bar.clone(),
                    confirmed,
                    revision,
                    source_confirmed,
                    received_ts: received,
                },
            );
            self.mark_activity(&key.0, now, Some(&key.1), Some(now), notices);
            let kline = KlineEvent {
                instrument_id: key.0.clone(),
                timeframe: key.1.clone(),
                bar: bar.clone(),
                confirmed,
                revision,
            };
            notices.push(PostNotice::Event(FeedEvent::Kline(kline)));
        }

        if source_confirmed
            && let Some(spec) = self.instruments.get(&key.0)
        {
            notices.push(PostNotice::BarClose {
                instrument: spec.clone(),
                timeframe: key.1.clone(),
                bar,
                received_ts: received,
                revision,
            });
        }
    }

    fn fill_missing(
        &mut self,
        key: &ChannelKey,
        start: i64,
        stop: i64,
        now: i64,
        force_gap: bool,
        notices: &mut Vec<PostNotice>,
    ) {
        let Ok(interval) = timeframe_ms(&key.1) else {
            return;
        };
        let mut ts = start;
        while ts < stop {
            let missing = !self
                .series
                .get(key)
                .is_some_and(|s| s.contains_key(&ts));
            if ts + interval <= now && missing {
                if force_gap {
                    self.synthesize_feed_gap(key, ts, interval, now, notices);
                } else {
                    self.synthesize_no_trades(key, ts, interval, now, notices);
                }
            }
            ts += interval;
        }
    }

    fn fill_outage_windows(
        &mut self,
        key: &ChannelKey,
        now: i64,
        notices: &mut Vec<PostNotice>,
    ) {
        let Some(windows) = self.outage_windows.get(key).cloned() else {
            return;
        };
        let Ok(interval) = timeframe_ms(&key.1) else {
            return;
        };
        for (started, ended) in windows {
            let mut ts = started + interval;
            while ts <= ended {
                let missing = !self
                    .series
                    .get(key)
                    .is_some_and(|s| s.contains_key(&ts));
                if ts + interval <= now && missing {
                    self.synthesize_feed_gap(key, ts, interval, now, notices);
                }
                ts += interval;
            }
        }
    }

    fn synthesize_feed_gap(
        &mut self,
        key: &ChannelKey,
        timestamp: i64,
        interval: i64,
        now: i64,
        notices: &mut Vec<PostNotice>,
    ) {
        let bar = CompactBar {
            timestamp,
            open: None,
            high: None,
            low: None,
            close: None,
            volume: None,
            turnover: None,
            status: BarStatus::FeedGap,
            ts_available: timestamp + interval,
            precision: PrecisionTier::SecondVerified,
        };
        self.apply_bar(key, bar, true, now, false, None, false, notices);
    }

    fn synthesize_no_trades(
        &mut self,
        key: &ChannelKey,
        timestamp: i64,
        interval: i64,
        now: i64,
        notices: &mut Vec<PostNotice>,
    ) {
        let Some(prev_close) = self.previous_close(key, timestamp) else {
            return;
        };
        let bar = CompactBar {
            timestamp,
            open: Some(prev_close),
            high: Some(prev_close),
            low: Some(prev_close),
            close: Some(prev_close),
            volume: Some(Decimal::ZERO),
            turnover: Some(Decimal::ZERO),
            status: BarStatus::NoTrades,
            ts_available: timestamp + interval,
            precision: PrecisionTier::SecondVerified,
        };
        self.apply_bar(key, bar, true, now, false, None, false, notices);
    }

    fn previous_close(&self, key: &ChannelKey, timestamp: i64) -> Option<Decimal> {
        let series = self.series.get(key)?;
        for (&candidate_ts, rec) in series.range(..timestamp).rev() {
            if candidate_ts >= timestamp {
                continue;
            }
            if let Some(close) = rec.bar.close {
                return Some(close);
            }
        }
        None
    }

    fn key_for_symbol(&self, symbol: &str, timeframe: &str) -> Option<ChannelKey> {
        for key in &self.channel_order {
            if key.1 == timeframe
                && let Some(spec) = self.instruments.get(&key.0)
                && spec.symbol == symbol
            {
                return Some(key.clone());
            }
        }
        None
    }

    fn mark_activity(
        &mut self,
        instrument_id: &str,
        now: i64,
        timeframe: Option<&str>,
        event_ts: Option<i64>,
        notices: &mut Vec<PostNotice>,
    ) {
        for key in &self.channel_order {
            let Some(channel) = self.channels.get_mut(key) else {
                continue;
            };
            if channel.instrument_id != instrument_id {
                continue;
            }
            if let Some(tf) = timeframe
                && channel.timeframe != tf
            {
                continue;
            }
            channel.last_activity_ts = Some(now);
            channel.last_event_ts = Some(event_ts.unwrap_or(now));
            if channel.status == FeedHealthStatus::STALE_STATUS {
                channel.status = FeedHealthStatus::Live;
                channel.stale_since = None;
                notices.push(PostNotice::Event(FeedEvent::Health(channel.health_event(now))));
            }
        }
    }
}

trait HealthStatusCompat {
    const STALE_STATUS: FeedHealthStatus = FeedHealthStatus::Stale;
}

impl HealthStatusCompat for FeedHealthStatus {}

struct RunnerHandles {
    stop_tx: watch::Sender<bool>,
    tasks: Vec<JoinHandle<()>>,
}

impl Drop for RunnerHandles {
    fn drop(&mut self) {
        let _ = self.stop_tx.send(true);
        for task in &self.tasks {
            task.abort();
        }
    }
}

/// Multi-channel market feed state machine (`LIVE -> STALE -> RECOVERING -> LIVE`).
///
/// Thread-safe and shareable via `Arc<FeedSupervisor>`. Synchronous state-machine methods never
/// hold a mutex across `.await`, and notifications are dispatched outside the state lock.
pub struct FeedSupervisor {
    transport: Arc<dyn FeedTransport>,
    connector: Option<Arc<dyn HistoryConnector>>,
    clock: Arc<dyn FeedClock>,
    sink: Arc<dyn BarPublishSink>,
    config: FeedSupervisorConfig,
    core: Arc<StdMutex<SupervisorCore>>,
    events_tx: broadcast::Sender<FeedEvent>,
    ops_tx: mpsc::UnboundedSender<SubscriptionOp>,
    ops_rx: AsyncMutex<Option<mpsc::UnboundedReceiver<SubscriptionOp>>>,
    runner: AsyncMutex<Option<RunnerHandles>>,
}

impl FeedSupervisor {
    /// Create a [`FeedSupervisor`] with explicit transport, optional backfill connector, instruments,
    /// injectable clock, persistence sink, and config.
    pub fn new(
        transport: Arc<dyn FeedTransport>,
        connector: Option<Arc<dyn HistoryConnector>>,
        instruments: Vec<InstrumentSpec>,
        clock: Arc<dyn FeedClock>,
        sink: Arc<dyn BarPublishSink>,
        config: FeedSupervisorConfig,
    ) -> Result<Self, FeedError> {
        if config.stale_after_ms <= 0 {
            return Err(FeedError::InvalidRequest(
                "stale_after_ms must be positive".to_owned(),
            ));
        }
        let (events_tx, _) = broadcast::channel(config.broadcast_capacity.max(16));
        let (ops_tx, ops_rx) = mpsc::unbounded_channel();
        let core = SupervisorCore::new(
            instruments,
            config.stale_after_ms,
            config.persist_public_trades,
        );
        Ok(Self {
            transport,
            connector,
            clock,
            sink,
            config,
            core: Arc::new(StdMutex::new(core)),
            events_tx,
            ops_tx,
            ops_rx: AsyncMutex::new(Some(ops_rx)),
            runner: AsyncMutex::new(None),
        })
    }

    /// Convenience constructor with [`NoopBarPublishSink`] and default [`FeedSupervisorConfig`].
    pub fn with_noop_sink(
        transport: Arc<dyn FeedTransport>,
        connector: Option<Arc<dyn HistoryConnector>>,
        instruments: Vec<InstrumentSpec>,
        clock: Arc<dyn FeedClock>,
    ) -> Result<Self, FeedError> {
        Self::new(
            transport,
            connector,
            instruments,
            clock,
            Arc::new(NoopBarPublishSink),
            FeedSupervisorConfig::default(),
        )
    }

    pub fn feed_mode(&self) -> &str {
        self.transport.feed_mode()
    }

    /// Subscribe to the unified [`FeedEvent`] broadcast channel (`klines`, `book`, `derivatives`, `trades`, `health`).
    pub fn subscribe_events(&self) -> broadcast::Receiver<FeedEvent> {
        self.events_tx.subscribe()
    }

    /// Subscribe to `(instrument_id, timeframe)`.
    pub fn subscribe(&self, instrument_id: &str, timeframe: &str) -> Result<(), FeedError> {
        let now = self.clock.now_ms();
        let (added, notices) = self
            .core
            .lock()
            .expect("supervisor lock")
            .subscribe(instrument_id, timeframe, now)?;
        if added {
            let _ = self.ops_tx.send(SubscriptionOp::Subscribe(
                instrument_id.to_owned(),
                timeframe.to_owned(),
            ));
        }
        self.dispatch(notices);
        Ok(())
    }

    /// Unsubscribe from `(instrument_id, timeframe)`.
    pub fn unsubscribe(&self, instrument_id: &str, timeframe: &str) {
        let removed = self
            .core
            .lock()
            .expect("supervisor lock")
            .unsubscribe(instrument_id, timeframe);
        if removed {
            let _ = self.ops_tx.send(SubscriptionOp::Unsubscribe(
                instrument_id.to_owned(),
                timeframe.to_owned(),
            ));
        }
    }

    /// Ingest one transport event or envelope directly into the state machine.
    pub fn ingest(&self, frame: impl Into<TransportFrame>, now: Option<i64>) {
        let now = now.unwrap_or_else(|| self.clock.now_ms());
        let notices = self
            .core
            .lock()
            .expect("supervisor lock")
            .ingest(frame.into(), now);
        self.dispatch(notices);
    }

    /// Refresh channel activity without generating a market data event.
    pub fn heartbeat(&self, now: Option<i64>) {
        let now = now.unwrap_or_else(|| self.clock.now_ms());
        let notices = self
            .core
            .lock()
            .expect("supervisor lock")
            .heartbeat(now);
        self.dispatch(notices);
    }

    /// Mark all active channels `STALE` and open an outage tracking window.
    pub fn transport_lost(&self, now: Option<i64>) {
        let now = now.unwrap_or_else(|| self.clock.now_ms());
        let notices = self
            .core
            .lock()
            .expect("supervisor lock")
            .transport_lost(now);
        self.dispatch(notices);
    }

    /// Transition channels `RECOVERING -> LIVE`, apply REST backfill bars (gaps only), and
    /// synthesize `FEED_GAP` bars across remaining unrecovered outage slots.
    pub fn transport_restored(&self, now: Option<i64>, backfill: &[RawBar]) {
        let now = now.unwrap_or_else(|| self.clock.now_ms());
        let notices = self
            .core
            .lock()
            .expect("supervisor lock")
            .transport_restored(now, backfill);
        self.dispatch(notices);
    }

    /// Check all `LIVE` channels against `stale_after_ms` and transition silent channels to `STALE`.
    pub fn tick(&self, now: Option<i64>) {
        let now = now.unwrap_or_else(|| self.clock.now_ms());
        let (notices, should_force_reconnect) = self.core.lock().expect("supervisor lock").tick(now);
        self.dispatch(notices);
        if should_force_reconnect {
            let _ = self.ops_tx.send(SubscriptionOp::ForceReconnect);
        }
    }

    /// Dynamically register a newly added instrument across supervisor state, transport, and backfill connector.
    pub fn register_instrument(&self, spec: InstrumentSpec) {
        self.transport.register_instrument(&spec);
        if let Some(connector) = &self.connector {
            connector.register_instrument(&spec);
        }
        self.core
            .lock()
            .expect("supervisor lock")
            .instruments
            .insert(spec.instrument_id.clone(), spec);
    }

    /// Aggregate health across all currently subscribed channels.
    pub fn health(&self) -> FeedHealthReport {
        let now = self.clock.now_ms();
        self.core
            .lock()
            .expect("supervisor lock")
            .health(self.transport.feed_mode(), now)
    }

    /// Health snapshot for a single channel.
    pub fn channel_health(&self, instrument_id: &str, timeframe: &str) -> Option<FeedHealthEvent> {
        let now = self.clock.now_ms();
        self.core
            .lock()
            .expect("supervisor lock")
            .channel_health(instrument_id, timeframe, now)
    }

    pub fn has_channel(&self, instrument_id: &str, timeframe: &str) -> bool {
        self.core
            .lock()
            .expect("supervisor lock")
            .channels
            .contains_key(&(instrument_id.to_owned(), timeframe.to_owned()))
    }

    /// Read-only snapshot of deduplicated bars in a channel, sorted ascending by timestamp.
    pub fn recent_bars(
        &self,
        instrument_id: &str,
        timeframe: &str,
        after_ts: Option<i64>,
        confirmed_only: bool,
    ) -> Vec<CompactBar> {
        self.core
            .lock()
            .expect("supervisor lock")
            .recent_bars(instrument_id, timeframe, after_ts, confirmed_only)
    }

    /// Start background transport consumption, reconnect/backfill, and staleness tick tasks.
    /// Idempotent if already running.
    pub async fn start(self: &Arc<Self>) -> Result<(), FeedError> {
        let mut guard = self.runner.lock().await;
        if guard.is_some() {
            return Ok(());
        }
        let ops_rx = self
            .ops_rx
            .lock()
            .await
            .take()
            .ok_or_else(|| FeedError::InvalidRequest("supervisor cannot be restarted after stop".to_owned()))?;

        let (stop_tx, stop_rx) = watch::channel(false);
        let mut tasks = Vec::new();

        let sup_serve = Arc::clone(self);
        let stop_serve = stop_rx.clone();
        tasks.push(tokio::spawn(async move {
            sup_serve.serve_loop(ops_rx, stop_serve).await;
        }));

        if let Some(tick_interval) = self.config.tick_interval {
            let sup_tick = Arc::clone(self);
            let stop_tick = stop_rx;
            tasks.push(tokio::spawn(async move {
                sup_tick.tick_loop(tick_interval, stop_tick).await;
            }));
        }

        *guard = Some(RunnerHandles { stop_tx, tasks });
        Ok(())
    }

    /// Stop background tasks and close the underlying transport cleanly.
    pub async fn stop(&self) {
        let handles = self.runner.lock().await.take();
        if let Some(mut handles) = handles {
            let _ = handles.stop_tx.send(true);
            for task in handles.tasks.drain(..) {
                let _ = task.await;
            }
        }
        let _ = self.transport.close().await;
    }

    fn dispatch(&self, notices: Vec<PostNotice>) {
        for notice in notices {
            match notice {
                PostNotice::Event(ev) => {
                    self.sink.on_event(&ev);
                    let _ = self.events_tx.send(ev);
                }
                PostNotice::ReceivedFrame(frame) => {
                    self.sink.on_received_frame(&frame);
                }
                PostNotice::Subscribe {
                    instrument,
                    timeframe,
                    subscribed_ts,
                } => {
                    self.sink.on_subscribe(&instrument, &timeframe, subscribed_ts);
                }
                PostNotice::BarClose {
                    instrument,
                    timeframe,
                    bar,
                    received_ts,
                    revision,
                } => {
                    self.sink
                        .on_bar_close(&instrument, &timeframe, &bar, received_ts, revision);
                }
                PostNotice::RecoveredBar { instrument, bar } => {
                    self.sink.on_recovered_bar(&instrument, &bar);
                }
            }
        }
    }

    fn current_channels(&self) -> Vec<ChannelKey> {
        self.core
            .lock()
            .expect("supervisor lock")
            .channel_order
            .clone()
    }

    fn is_outage_active(&self) -> bool {
        self.core
            .lock()
            .expect("supervisor lock")
            .outage_lost_at
            .is_some()
    }

    async fn fetch_backfill(&self) -> Vec<RawBar> {
        let Some(connector) = &self.connector else {
            return Vec::new();
        };
        let targets: Vec<(String, String, Option<i64>)> = {
            let core = self.core.lock().expect("supervisor lock");
            core.channel_order
                .iter()
                .filter_map(|key| {
                    let spec = core.instruments.get(&key.0)?;
                    let last_ts = core
                        .series
                        .get(key)
                        .and_then(|s| s.keys().next_back().copied());
                    Some((spec.symbol.clone(), key.1.clone(), last_ts))
                })
                .collect()
        };

        let mut bars = Vec::new();
        for (symbol, timeframe, last_ts) in targets {
            match backfill_bars(
                connector.as_ref(),
                &symbol,
                &timeframe,
                BackfillWindow {
                    start_ts: last_ts,
                    end_ts: None,
                    target_count: MAX_BACKFILL_BARS,
                    max_pages: 5,
                    page_limit: MAX_BACKFILL_BARS,
                },
            )
            .await
            {
                Ok(fetched) => {
                    for bar in fetched {
                        if last_ts.is_none_or(|lt| bar.ts_event >= lt) {
                            bars.push(bar);
                        }
                    }
                }
                Err(err) => {
                    warn!("reconnect backfill failed for {symbol} {timeframe}: {err}");
                }
            }
        }
        bars
    }

    async fn sleep_or_stop(&self, duration: Duration, stop_rx: &mut watch::Receiver<bool>) {
        if *stop_rx.borrow() || duration.is_zero() {
            return;
        }
        tokio::select! {
            biased;
            _ = stop_rx.changed() => {}
            _ = self.clock.sleep(duration) => {}
        }
    }

    async fn serve_loop(
        &self,
        mut ops_rx: mpsc::UnboundedReceiver<SubscriptionOp>,
        mut stop_rx: watch::Receiver<bool>,
    ) {
        let mut backoff = self.config.backoff_initial;
        let max_backoff = self.config.backoff_max;

        while !*stop_rx.borrow() {
            let connected = async {
                self.transport.connect().await?;
                while ops_rx.try_recv().is_ok() {}
                for (inst, tf) in self.current_channels() {
                    self.transport.subscribe(&inst, &tf).await?;
                }
                Ok::<(), talaria_packs::PackError>(())
            }
            .await;

            if let Err(err) = connected {
                warn!("real-time feed connect/subscribe failed: {err}");
                self.transport_lost(None);
                self.sleep_or_stop(backoff, &mut stop_rx).await;
                backoff = (backoff.saturating_mul(2)).min(max_backoff);
                continue;
            }

            backoff = self.config.backoff_initial;
            if self.is_outage_active() {
                let backfill = self.fetch_backfill().await;
                self.transport_restored(None, &backfill);
            }

            loop {
                tokio::select! {
                    biased;
                    _ = stop_rx.changed() => {
                        if *stop_rx.borrow() {
                            break;
                        }
                    }
                    maybe_op = ops_rx.recv() => {
                        let Some(op) = maybe_op else {
                            break;
                        };
                        match op {
                            SubscriptionOp::ForceReconnect => {
                                warn!("all subscribed channels remained STALE; forcing transport reconnect");
                                self.transport_lost(None);
                                break;
                            }
                            SubscriptionOp::Subscribe(inst, tf) => {
                                if let Err(err) = self.transport.subscribe(&inst, &tf).await {
                                    warn!("subscription operation failed: {err}");
                                    self.transport_lost(None);
                                    break;
                                }
                            }
                            SubscriptionOp::Unsubscribe(inst, tf) => {
                                if let Err(err) = self.transport.unsubscribe(&inst, &tf).await {
                                    warn!("subscription operation failed: {err}");
                                    self.transport_lost(None);
                                    break;
                                }
                            }
                        }
                    }
                    recv_res = self.transport.recv() => {
                        match recv_res {
                            Ok(None) => self.heartbeat(None),
                            Ok(Some(frame)) => self.ingest(frame, None),
                            Err(err) => {
                                warn!("real-time stream disconnected: {err}");
                                self.transport_lost(None);
                                break;
                            }
                        }
                    }
                }
            }

            if *stop_rx.borrow() {
                break;
            }
            self.sleep_or_stop(backoff, &mut stop_rx).await;
            backoff = (backoff.saturating_mul(2)).min(max_backoff);
        }

        let _ = self.transport.close().await;
    }

    async fn tick_loop(&self, interval: Duration, mut stop_rx: watch::Receiver<bool>) {
        while !*stop_rx.borrow() {
            self.sleep_or_stop(interval, &mut stop_rx).await;
            if *stop_rx.borrow() {
                break;
            }
            self.tick(None);
        }
    }
}
