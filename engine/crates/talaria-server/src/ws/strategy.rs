//! Reliable changelog WebSocket stream (`WS /ws/strategy`) with `(epoch, seq)`
//! ordering, bounded reconnect replay, `resync` notice, and HTTP/WS close `1013`
//! on consumer queue overflow.

use std::collections::{HashMap, HashSet, VecDeque};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Query, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::{IntoResponse, Response};
use futures_util::{SinkExt, StreamExt};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use talaria_contracts::bars::{RangeBar, RawBar};
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::error_codes::{FEED_UNAVAILABLE, INSTRUMENT_NOT_FOUND, VALIDATION_ERROR};
use talaria_contracts::feed::KlineEvent;
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::strategy_subscription::{
    StrategyBarEvent, StrategyBarEventType, StrategyDerivativeEvent, StrategyDerivativeEventType,
    SubscriptionCursor, SubscriptionReady, SubscriptionReadyType, SubscriptionRequest,
};
use talaria_contracts::SUPPORTED_TIMEFRAMES;
use talaria_packs::get_market_pack;
use talaria_store::{ColdReader, HotSlot, HotStreamBuffer, range_visible_bars, revision_window};
use tokio::sync::Notify;
use tokio::task::JoinHandle;
use uuid::Uuid;

use crate::error::ApiError;
use crate::security::{DATA_DIR_HEADER, verify_bearer_header};
use crate::server::AppState;
use crate::subscriptions::SubscriptionScheduler;

pub const STRATEGY_QUEUE_SIZE: usize = 256;
pub const STRATEGY_LOG_SIZE: usize = 4096;
pub const RECONNECT_LEASE_SECONDS: u64 = 30;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub enum StrategyKey {
    Bar(String, String),
    Derivative(String, String, DerivativeKind),
}

impl StrategyKey {
    pub fn upstream(&self) -> (String, String) {
        match self {
            Self::Bar(inst, tf) => (inst.clone(), tf.clone()),
            Self::Derivative(inst, tf, _) => (inst.clone(), tf.clone()),
        }
    }

    pub fn instrument_id(&self) -> &str {
        match self {
            Self::Bar(inst, _) | Self::Derivative(inst, _, _) => inst,
        }
    }

    pub fn timeframe(&self) -> &str {
        match self {
            Self::Bar(_, tf) | Self::Derivative(_, tf, _) => tf,
        }
    }
}

#[derive(Debug, Clone)]
pub enum StrategyEvent {
    Bar(StrategyBarEvent),
    Derivative(StrategyDerivativeEvent),
}

impl StrategyEvent {
    pub fn seq(&self) -> u64 {
        match self {
            Self::Bar(e) => e.seq.unwrap_or(0),
            Self::Derivative(e) => e.seq.unwrap_or(0),
        }
    }

    pub fn to_json(&self) -> String {
        match self {
            Self::Bar(e) => serde_json::to_string(e).unwrap_or_default(),
            Self::Derivative(e) => serde_json::to_string(e).unwrap_or_default(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
struct DirtyRange {
    start: i64,
    end: Option<i64>,
}

const FULL_RANGE: DirtyRange = DirtyRange {
    start: 0,
    end: None,
};

struct SessionQueue {
    id: u64,
    events: Mutex<VecDeque<StrategyEvent>>,
    notify: Notify,
    interrupted: AtomicBool,
}

impl SessionQueue {
    fn new(id: u64) -> Self {
        Self {
            id,
            events: Mutex::new(VecDeque::with_capacity(STRATEGY_QUEUE_SIZE)),
            notify: Notify::new(),
            interrupted: AtomicBool::new(false),
        }
    }

    fn push(&self, event: StrategyEvent) -> bool {
        let ok = {
            let mut q = self.events.lock().expect("strategy queue poisoned");
            if q.len() >= STRATEGY_LOG_SIZE || q.len() >= STRATEGY_QUEUE_SIZE {
                self.interrupted.store(true, Ordering::Release);
                false
            } else {
                q.push_back(event);
                true
            }
        };
        self.notify.notify_waiters();
        ok
    }

    fn interrupt(&self) {
        self.interrupted.store(true, Ordering::Release);
        self.notify.notify_waiters();
    }

    fn is_interrupted(&self) -> bool {
        self.interrupted.load(Ordering::Acquire)
    }

    async fn next_event(&self) -> Option<StrategyEvent> {
        loop {
            if self.is_interrupted() {
                return None;
            }
            if let Some(ev) = self.events.lock().expect("strategy queue poisoned").pop_front() {
                return Some(ev);
            }
            self.notify.notified().await;
        }
    }
}

pub struct StrategySession {
    pub ready: SubscriptionReady,
    pub replay: Vec<StrategyEvent>,
    pub baseline: Vec<String>,
    queue: Arc<SessionQueue>,
}

struct Journal {
    epoch: String,
    seq: u64,
    events: VecDeque<StrategyEvent>,
    seen: HashSet<String>,
    clients: HashMap<u64, Arc<SessionQueue>>,
    dirty: VecDeque<(DirtyRange, Option<Arc<HotSlot>>)>,
    draining: bool,
    attaching: bool,
    next_available: Option<i64>,
}

impl Journal {
    fn new() -> Self {
        Self {
            epoch: Uuid::new_v4().simple().to_string(),
            seq: 0,
            events: VecDeque::with_capacity(STRATEGY_LOG_SIZE),
            seen: HashSet::new(),
            clients: HashMap::new(),
            dirty: VecDeque::new(),
            draining: false,
            attaching: false,
            next_available: None,
        }
    }

    fn invalidate(&mut self) {
        self.epoch = Uuid::new_v4().simple().to_string();
        self.seq = 0;
        self.events.clear();
        for client in self.clients.values() {
            client.interrupt();
        }
        self.clients.clear();
    }
}

struct Lease {
    key: StrategyKey,
    initial: SubscriptionCursor,
    started_ts: i64,
    baseline: Vec<String>,
    session_id: Option<u64>,
    expiry_handle: Option<JoinHandle<()>>,
}

struct Inner {
    next_session_id: u64,
    leases: HashMap<String, Lease>,
    journals: HashMap<StrategyKey, Arc<tokio::sync::Mutex<Journal>>>,
}

type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;

/// Per-channel ordered changelog stream (`WS /ws/strategy`).
pub struct StrategyStream {
    scheduler: Arc<SubscriptionScheduler>,
    hot_buffer: Arc<HotStreamBuffer>,
    reader: Arc<ColdReader>,
    instruments: HashMap<String, InstrumentSpec>,
    now_ms: NowFn,
    inner: Mutex<Inner>,
}

impl StrategyStream {
    pub fn new(
        scheduler: Arc<SubscriptionScheduler>,
        hot_buffer: Arc<HotStreamBuffer>,
        reader: Arc<ColdReader>,
        instruments: &[InstrumentSpec],
        now_ms: NowFn,
    ) -> Self {
        let mut inst_map = HashMap::with_capacity(instruments.len());
        for spec in instruments {
            inst_map.insert(spec.instrument_id.clone(), spec.clone());
        }
        Self {
            scheduler,
            hot_buffer,
            reader,
            instruments: inst_map,
            now_ms,
            inner: Mutex::new(Inner {
                next_session_id: 1,
                leases: HashMap::new(),
                journals: HashMap::new(),
            }),
        }
    }

    pub fn on_confirmed_kline(self: &Arc<Self>, event: &KlineEvent, ts_event: i64) {
        if !event.confirmed {
            return;
        }
        let key = StrategyKey::Bar(event.instrument_id.clone(), event.timeframe.clone());
        let slot = self
            .hot_buffer
            .slot(&event.instrument_id, &event.timeframe, ts_event);
        self.changed(key, ts_event, Some(ts_event), slot);
    }

    pub fn derivative_changed(
        self: &Arc<Self>,
        instrument_id: &str,
        kind: DerivativeKind,
        start: i64,
        end: i64,
    ) {
        let matching: Vec<StrategyKey> = {
            let guard = self.inner.lock().expect("strategy stream poisoned");
            guard
                .journals
                .keys()
                .filter(|k| matches!(k, StrategyKey::Derivative(i, _, d) if i == instrument_id && *d == kind))
                .cloned()
                .collect()
        };
        for key in matching {
            self.changed(key, start, Some(end), None);
        }
    }

    pub fn changed(
        self: &Arc<Self>,
        key: StrategyKey,
        start: i64,
        end: Option<i64>,
        slot: Option<Arc<HotSlot>>,
    ) {
        let journal_opt = {
            let guard = self.inner.lock().expect("strategy stream poisoned");
            guard.journals.get(&key).cloned()
        };
        let Some(journal_arc) = journal_opt else {
            return;
        };
        let stream = Arc::clone(self);
        tokio::spawn(async move {
            let should_spawn = {
                let mut journal = journal_arc.lock().await;
                let range = DirtyRange { start, end };
                if journal.dirty.len() >= STRATEGY_LOG_SIZE
                    && !journal.dirty.iter().any(|(r, _)| r == &range)
                {
                    journal.dirty.clear();
                    journal.invalidate();
                    journal.dirty.push_back((FULL_RANGE, None));
                } else if !journal.dirty.iter().any(|(r, s)| {
                    r == &range
                        && match (s, &slot) {
                            (Some(a), Some(b)) => Arc::ptr_eq(a, b),
                            (None, None) => true,
                            _ => false,
                        }
                }) {
                    journal.dirty.push_back((range, slot));
                }
                if !journal.draining {
                    journal.draining = true;
                    true
                } else {
                    false
                }
            };
            if should_spawn {
                stream.drain_journal(key, journal_arc).await;
            }
        });
    }

    async fn drain_journal(
        self: Arc<Self>,
        key: StrategyKey,
        journal_arc: Arc<tokio::sync::Mutex<Journal>>,
    ) {
        loop {
            let next_item = {
                let mut journal = journal_arc.lock().await;
                if let Some(item) = journal.dirty.pop_front() {
                    Some(item)
                } else {
                    journal.draining = false;
                    None
                }
            };
            let Some((range, slot)) = next_item else {
                break;
            };
            if let Err(err) = self
                .refresh_journal(&key, &journal_arc, range, slot, false)
                .await
            {
                tracing::error!(?key, error = %err, "strategy journal refresh failed");
                let mut journal = journal_arc.lock().await;
                journal.invalidate();
                journal.dirty.clear();
                journal.draining = false;
                break;
            }
        }
    }

    async fn refresh_journal(
        &self,
        key: &StrategyKey,
        journal_arc: &Arc<tokio::sync::Mutex<Journal>>,
        range: DirtyRange,
        slot: Option<Arc<HotSlot>>,
        baseline: bool,
    ) -> Result<(), ApiError> {
        let now = (self.now_ms)();
        let key_clone = key.clone();
        let spec = self.instruments.get(key.instrument_id()).cloned();
        let reader = Arc::clone(&self.reader);
        let hot = Arc::clone(&self.hot_buffer);

        let (rows, future) = tokio::task::spawn_blocking(move || -> Result<(IdentifiedList, Option<i64>), ApiError> {
            let Some(spec) = spec else {
                return Err(ApiError::not_found(INSTRUMENT_NOT_FOUND, "instrument not found"));
            };
            match &key_clone {
                StrategyKey::Derivative(_, _, kind) => {
                    let points = reader.read_derivative_revisions(
                        &spec,
                        *kind,
                        Some(range.start),
                        range.end,
                        false,
                    )?;
                    let future = points
                        .iter()
                        .filter(|p| p.ts_available > now)
                        .map(|p| p.ts_available)
                        .min();
                    let visible: Vec<DerivativePoint> =
                        points.into_iter().filter(|p| p.ts_available <= now).collect();
                    let identified = identified_derivative_revisions(visible)
                        .into_iter()
                        .map(|(id, p)| (id, IdentifiedPayload::Derivative(p)))
                        .collect();
                    Ok((identified, future))
                }
                StrategyKey::Bar(_, tf) => {
                    let cold = reader.read_revisions(
                        &spec,
                        tf,
                        Some(range.start),
                        range.end,
                    )?;
                    let hot_rows = if let Some(slot_arc) = slot {
                        slot_arc.revisions()
                    } else {
                        hot.revisions(&spec.instrument_id, tf, range.start, range.end)
                    };
                    let (rev_rows, future) = revision_window(&cold, &hot_rows, 0, Some(now));
                    let _ = range_visible_bars;
                    let range_bars: Vec<RangeBar> = rev_rows.iter().map(raw_to_range_bar).collect();
                    let identified = identified_revisions(&range_bars)
                        .into_iter()
                        .map(|(id, b)| (id, IdentifiedPayload::Bar(b)))
                        .collect();
                    Ok((identified, future))
                }
            }
        })
        .await
        .map_err(|e| ApiError::internal(format!("blocking read failed: {e}")))??;

        let schedule_timer = {
            let mut journal = journal_arc.lock().await;
            for (identity, payload) in rows {
                if !journal.seen.insert(identity.clone()) {
                    continue;
                }
                if !baseline {
                    publish_to_journal(key, &mut journal, identity, payload);
                }
            }
            if let Some(due) = future
                && journal.next_available.is_none_or(|prev| due < prev)
            {
                journal.next_available = Some(due);
                Some(due)
            } else {
                None
            }
        };

        if let Some(due) = schedule_timer {
            let stream = self;
            let _ = stream;
            let remain_ms = (due - (self.now_ms)()).clamp(1, 250) as u64;
            let journal_weak = Arc::downgrade(journal_arc);
            tokio::spawn(async move {
                tokio::time::sleep(Duration::from_millis(remain_ms)).await;
                if let Some(j_arc) = journal_weak.upgrade() {
                    let mut j = j_arc.lock().await;
                    j.next_available = None;
                }
            });
        }
        Ok(())
    }

    pub async fn attach(
        self: &Arc<Self>,
        owner: &str,
        key: StrategyKey,
        after: Option<SubscriptionCursor>,
        include_baseline: bool,
        report_replay_through: bool,
    ) -> Result<StrategySession, ApiError> {
        let upstream = key.upstream();
        self.scheduler.check_account(owner, &upstream)?;

        let (journal_arc, fresh, existing_lease) = {
            let mut guard = self.inner.lock().expect("strategy stream poisoned");
            if let Some(lease) = guard.leases.get(owner)
                && lease.key != key
            {
                return Err(ApiError::validation("连接所有者已持有另一个策略订阅"));
            }
            let fresh = !guard.journals.contains_key(&key);
            let journal_arc = guard
                .journals
                .entry(key.clone())
                .or_insert_with(|| Arc::new(tokio::sync::Mutex::new(Journal::new())))
                .clone();
            let had_lease = guard.leases.contains_key(owner);
            (journal_arc, fresh, had_lease)
        };

        {
            let mut journal = journal_arc.lock().await;
            journal.attaching = true;
        }

        if !existing_lease {
            if let Err(err) = self
                .refresh_journal(&key, &journal_arc, FULL_RANGE, None, fresh)
                .await
            {
                self.finish_attach_cleanup(&key, &journal_arc).await;
                return Err(err);
            }
            if let Err(err) = self.scheduler.acquire_account(owner, upstream.clone()) {
                self.finish_attach_cleanup(&key, &journal_arc).await;
                return Err(err);
            }
        }

        let session = {
            let mut journal = journal_arc.lock().await;
            let mut guard = self.inner.lock().expect("strategy stream poisoned");
            if !guard.leases.contains_key(owner) {
                let initial = SubscriptionCursor {
                    epoch: journal.epoch.clone(),
                    seq: journal.seq,
                };
                let baseline = if include_baseline && after.is_none() {
                    journal.seen.iter().cloned().collect()
                } else {
                    Vec::new()
                };
                guard.leases.insert(
                    owner.to_owned(),
                    Lease {
                        key: key.clone(),
                        initial,
                        started_ts: (self.now_ms)(),
                        baseline,
                        session_id: None,
                        expiry_handle: None,
                    },
                );
            }

            let session_id = guard.next_session_id;
            guard.next_session_id += 1;

            let lease = guard.leases.get_mut(owner).expect("lease inserted");
            if let Some(handle) = lease.expiry_handle.take() {
                handle.abort();
            }
            if let Some(old_id) = lease.session_id.replace(session_id)
                && let Some(old_q) = journal.clients.remove(&old_id)
            {
                old_q.interrupt();
            }

            let cursor = after.clone().unwrap_or_else(|| lease.initial.clone());
            let oldest = journal
                .events
                .front()
                .map(|e| e.seq())
                .unwrap_or(journal.seq + 1);
            let valid = cursor.epoch == journal.epoch
                && cursor.seq >= oldest.saturating_sub(1)
                && cursor.seq <= journal.seq;

            let baseline = if after.is_none() {
                lease.baseline.clone()
            } else {
                Vec::new()
            };
            let replay: Vec<StrategyEvent> = if valid {
                journal
                    .events
                    .iter()
                    .filter(|ev| ev.seq() > cursor.seq)
                    .cloned()
                    .collect()
            } else {
                Vec::new()
            };

            let ready = SubscriptionReady {
                epoch: journal.epoch.clone(),
                seq: if valid { cursor.seq } else { journal.seq },
                kind: if valid {
                    SubscriptionReadyType::Ready
                } else {
                    SubscriptionReadyType::Resync
                },
                server_ts: (self.now_ms)(),
                started_ts: lease.started_ts,
                baseline_count: baseline.len(),
                replay_through: report_replay_through.then_some(journal.seq as i64),
            };

            let queue = Arc::new(SessionQueue::new(session_id));
            journal.clients.insert(session_id, Arc::clone(&queue));
            journal.attaching = false;

            StrategySession {
                ready,
                replay,
                baseline,
                queue,
            }
        };

        Ok(session)
    }

    async fn finish_attach_cleanup(
        &self,
        key: &StrategyKey,
        journal_arc: &Arc<tokio::sync::Mutex<Journal>>,
    ) {
        let mut journal = journal_arc.lock().await;
        journal.attaching = false;
        let has_lease = {
            let guard = self.inner.lock().expect("strategy stream poisoned");
            guard.leases.values().any(|l| &l.key == key)
        };
        if !has_lease {
            let mut guard = self.inner.lock().expect("strategy stream poisoned");
            guard.journals.remove(key);
        }
    }

    pub fn detach(self: &Arc<Self>, owner: &str, session_id: u64, release: bool) {
        let (key_opt, q_opt) = {
            let mut guard = self.inner.lock().expect("strategy stream poisoned");
            let Some(lease) = guard.leases.get_mut(owner) else {
                return;
            };
            if lease.session_id != Some(session_id) {
                return;
            }
            lease.session_id = None;
            let key = lease.key.clone();
            if release {
                if let Some(handle) = lease.expiry_handle.take() {
                    handle.abort();
                }
                guard.leases.remove(owner);
                self.scheduler.release_account(owner);
            } else {
                let owner_owned = owner.to_owned();
                let stream = Arc::clone(self);
                let handle = tokio::spawn(async move {
                    tokio::time::sleep(Duration::from_secs(RECONNECT_LEASE_SECONDS)).await;
                    stream.release_owner(&owner_owned);
                });
                lease.expiry_handle = Some(handle);
            }
            (Some(key.clone()), guard.journals.get(&key).cloned())
        };

        if let Some(journal_arc) = q_opt {
            let stream = Arc::clone(self);
            tokio::spawn(async move {
                let mut journal = journal_arc.lock().await;
                journal.clients.remove(&session_id);
                drop(journal);
                if let Some(k) = key_opt {
                    stream.discard_if_unused(&k);
                }
            });
        }
    }

    fn release_owner(&self, owner: &str) {
        let removed = {
            let mut guard = self.inner.lock().expect("strategy stream poisoned");
            guard.leases.remove(owner)
        };
        if let Some(mut lease) = removed {
            if let Some(handle) = lease.expiry_handle.take() {
                handle.abort();
            }
            self.scheduler.release_account(owner);
            self.discard_if_unused(&lease.key);
        }
    }

    fn discard_if_unused(&self, key: &StrategyKey) {
        let mut guard = self.inner.lock().expect("strategy stream poisoned");
        if guard.leases.values().any(|l| &l.key == key) {
            return;
        }
        guard.journals.remove(key);
    }

    /// Push a raw `StrategyEvent` directly into an active journal (test helper & internal hook).
    pub async fn push_test_bar_event(&self, key: &StrategyKey, bar: RangeBar, identity: String) {
        self.push_test_bar_events_batch(key, vec![(bar, identity)])
            .await;
    }

    /// Push a burst of `StrategyEvent`s atomically under the journal lock (e.g. for overflow tests).
    pub async fn push_test_bar_events_batch(
        &self,
        key: &StrategyKey,
        items: Vec<(RangeBar, String)>,
    ) {
        let journal_arc = {
            let guard = self.inner.lock().expect("strategy stream poisoned");
            guard.journals.get(key).cloned()
        };
        if let Some(j_arc) = journal_arc {
            let mut journal = j_arc.lock().await;
            for (bar, identity) in items {
                if journal.seen.insert(identity.clone()) {
                    publish_to_journal(key, &mut journal, identity, IdentifiedPayload::Bar(bar));
                }
            }
        }
    }

    pub fn close(&self) {
        let owners: Vec<String> = {
            let guard = self.inner.lock().expect("strategy stream poisoned");
            guard.leases.keys().cloned().collect()
        };
        for owner in owners {
            self.release_owner(&owner);
        }
    }
}

enum IdentifiedPayload {
    Bar(RangeBar),
    Derivative(DerivativePoint),
}

type IdentifiedList = Vec<(String, IdentifiedPayload)>;

fn publish_to_journal(
    key: &StrategyKey,
    journal: &mut Journal,
    identity: String,
    payload: IdentifiedPayload,
) {
    journal.seq += 1;
    let event = match payload {
        IdentifiedPayload::Bar(bar) => StrategyEvent::Bar(StrategyBarEvent {
            kind: StrategyBarEventType::Bar,
            instrument_id: key.instrument_id().to_owned(),
            timeframe: key.timeframe().to_owned(),
            epoch: journal.epoch.clone(),
            seq: Some(journal.seq),
            bar,
            content_id: identity,
        }),
        IdentifiedPayload::Derivative(point) => {
            StrategyEvent::Derivative(StrategyDerivativeEvent {
                event_type: StrategyDerivativeEventType::Derivative,
                epoch: journal.epoch.clone(),
                seq: Some(journal.seq),
                point,
                content_id: identity,
            })
        }
    };
    if journal.events.len() >= STRATEGY_LOG_SIZE {
        journal.events.pop_front();
    }
    journal.events.push_back(event.clone());
    let mut dropped = Vec::new();
    for (&id, q) in &journal.clients {
        if !q.push(event.clone()) {
            dropped.push(id);
        }
    }
    for id in dropped {
        journal.clients.remove(&id);
    }
}

pub fn raw_to_range_bar(raw: &RawBar) -> RangeBar {
    RangeBar {
        ts_event: raw.ts_event,
        open: raw.open,
        high: raw.high,
        low: raw.low,
        close: raw.close,
        volume: raw.volume,
        turnover: raw.turnover,
        ts_available: raw.ts_available,
        precision: raw.precision,
        confirmed: raw.confirmed,
        revision: raw.revision,
    }
}

pub fn identified_revisions(bars: &[RangeBar]) -> Vec<(String, RangeBar)> {
    let mut out = Vec::with_capacity(bars.len());
    let mut i = 0;
    while i < bars.len() {
        let ts = bars[i].ts_event;
        let mut occurrences: HashMap<String, (u64, usize)> = HashMap::new();
        while i < bars.len() && bars[i].ts_event == ts {
            let bar = &bars[i];
            let without_rev = serde_json::json!({
                "ts_event": bar.ts_event,
                "open": bar.open.normalize().to_string(),
                "high": bar.high.normalize().to_string(),
                "low": bar.low.normalize().to_string(),
                "close": bar.close.normalize().to_string(),
                "volume": bar.volume.normalize().to_string(),
                "turnover": bar.turnover.normalize().to_string(),
                "ts_available": bar.ts_available,
                "precision": bar.precision,
                "confirmed": bar.confirmed,
            });
            let digest = hex::encode(Sha256::digest(
                serde_json::to_string(&without_rev)
                    .unwrap_or_default()
                    .as_bytes(),
            ));
            let entry = occurrences.get(&digest).copied();
            if entry.is_some_and(|(prev_rev, _)| prev_rev == bar.revision) {
                i += 1;
                continue;
            }
            let count = entry.map(|(_, c)| c).unwrap_or(0);
            occurrences.insert(digest.clone(), (bar.revision, count + 1));
            let content_id = if count == 0 {
                digest
            } else {
                format!("{digest}.{count}")
            };
            out.push((content_id, bar.clone()));
            i += 1;
        }
    }
    out
}

pub fn identified_derivative_revisions(
    mut points: Vec<DerivativePoint>,
) -> Vec<(String, DerivativePoint)> {
    points.sort_by(|a, b| {
        (
            a.ts_event,
            a.timeframe.as_deref().unwrap_or(""),
            a.is_funding_settlement(),
            a.revision,
        )
            .cmp(&(
                b.ts_event,
                b.timeframe.as_deref().unwrap_or(""),
                b.is_funding_settlement(),
                b.revision,
            ))
    });
    points
        .into_iter()
        .map(|p| {
            let kind_str = serde_json::to_string(&p.kind)
                .unwrap_or_default()
                .replace('"', "");
            let id = format!(
                "{kind_str}:{}:{}:{}:{}",
                p.timeframe.as_deref().unwrap_or(""),
                u8::from(p.is_funding_settlement()),
                p.ts_event,
                p.revision
            );
            (id, p)
        })
        .collect()
}

#[derive(Debug, Default, Deserialize)]
pub struct StrategyQuery {
    #[serde(default)]
    pub from_epoch: Option<String>,
    #[serde(default)]
    pub from_seq: Option<u64>,
    #[serde(default)]
    pub instrument: Option<String>,
    #[serde(default)]
    pub timeframe: Option<String>,
    #[serde(default)]
    pub owner: Option<String>,
    #[serde(default, deserialize_with = "crate::error::deserialize_lenient_opt_bool")]
    pub include_baseline: Option<bool>,
}

pub async fn ws_strategy_handler(
    headers: HeaderMap,
    Query(query): Query<StrategyQuery>,
    ws: WebSocketUpgrade,
    State(state): State<Arc<AppState>>,
) -> Response {
    if !verify_bearer_header(&headers, &state.security.token) {
        return (
            StatusCode::UNAUTHORIZED,
            axum::Json(serde_json::json!({
                "code": talaria_contracts::error_codes::UNAUTHORIZED,
                "message": "missing or invalid bearer token",
            })),
        )
            .into_response();
    }
    if let Some(dir_hdr) = headers.get(DATA_DIR_HEADER).and_then(|v| v.to_str().ok())
        && dir_hdr != state.security.data_dir_id
    {
        return (
            StatusCode::CONFLICT,
            axum::Json(serde_json::json!({
                "code": talaria_contracts::error_codes::SERVICE_LOCKED,
                "message": format!("data_dir_id mismatch: expected {}", state.security.data_dir_id),
            })),
        )
            .into_response();
    }
    ws.on_upgrade(move |socket| handle_strategy_socket(socket, state, query))
}

async fn reject_ws(sink: &mut futures_util::stream::SplitSink<WebSocket, Message>, code: &str, message: &str) {
    let payload = serde_json::json!({
        "type": "error",
        "code": code,
        "message": message,
    })
    .to_string();
    let _ = sink.send(Message::Text(payload.into())).await;
    let _ = sink
        .send(Message::Close(Some(CloseFrame {
            code: 1008,
            reason: code.to_owned().into(),
        })))
        .await;
}

async fn handle_strategy_socket(socket: WebSocket, state: Arc<AppState>, query: StrategyQuery) {
    let (mut sink, mut stream) = socket.split();
    let Some(strategy_stream) = state.strategy_stream.clone() else {
        reject_ws(&mut sink, FEED_UNAVAILABLE, "常驻服务未启用实时行情").await;
        return;
    };

    let query_cursor = match (&query.from_epoch, query.from_seq) {
        (Some(epoch), Some(seq)) if !epoch.is_empty() => Some(SubscriptionCursor {
            epoch: epoch.clone(),
            seq,
        }),
        (Some(epoch), None) if !epoch.is_empty() => Some(SubscriptionCursor {
            epoch: epoch.clone(),
            seq: 0,
        }),
        _ => None,
    };

    let has_query_target = query.instrument.is_some() && query.timeframe.is_some();
    let wait_timeout = if has_query_target {
        Duration::from_millis(50)
    } else {
        Duration::from_secs(5)
    };

    let mut req: Option<SubscriptionRequest> = None;
    match tokio::time::timeout(wait_timeout, stream.next()).await {
        Ok(Some(Ok(Message::Text(text)))) => {
            match serde_json::from_str::<SubscriptionRequest>(&text) {
                Ok(mut parsed) => {
                    if parsed.after.is_none() {
                        parsed.after = query_cursor.clone();
                    }
                    req = Some(parsed);
                }
                Err(_) => {
                    reject_ws(&mut sink, VALIDATION_ERROR, "无效的策略订阅请求").await;
                    return;
                }
            }
        }
        Ok(Some(Ok(Message::Close(_)))) | Ok(None) => {
            if !has_query_target {
                return;
            }
        }
        Ok(Some(Err(_))) => return,
        Ok(Some(Ok(_))) | Err(_) => {
            if has_query_target {
                req = Some(SubscriptionRequest {
                    instrument: query.instrument.clone().unwrap_or_default(),
                    timeframe: query.timeframe.clone().unwrap_or_default(),
                    owner: query.owner.clone().unwrap_or_else(|| "query-client".to_owned()),
                    after: query_cursor.clone(),
                    include_baseline: query.include_baseline.unwrap_or(false),
                    report_replay_through: false,
                    kind: None,
                });
            } else {
                reject_ws(&mut sink, VALIDATION_ERROR, "无效的策略订阅请求").await;
                return;
            }
        }
    }

    let Some(req) = req else {
        return;
    };
    let valid_instruments: HashSet<String> = state
        .instruments
        .iter()
        .map(|s| s.instrument_id.clone())
        .collect();
    if !valid_instruments.contains(&req.instrument) {
        reject_ws(
            &mut sink,
            INSTRUMENT_NOT_FOUND,
            &format!("未知标的：{}", req.instrument),
        )
        .await;
        return;
    }
    if !SUPPORTED_TIMEFRAMES.contains(&req.timeframe.as_str()) {
        reject_ws(
            &mut sink,
            VALIDATION_ERROR,
            &format!("不支持的周期：{}", req.timeframe),
        )
        .await;
        return;
    }

    let key = if let Some(kind) = req.kind {
        let supported = get_market_pack(&state.pack_name)
            .ok()
            .and_then(|pack| pack.traits(&req.instrument).ok())
            .and_then(|t| t.derivatives)
            .is_some_and(|d| d.realtime && d.kinds.contains(&kind));
        if !supported {
            reject_ws(&mut sink, FEED_UNAVAILABLE, "该标的的衍生品实时冷库不可用").await;
            return;
        }
        StrategyKey::Derivative(req.instrument.clone(), req.timeframe.clone(), kind)
    } else {
        StrategyKey::Bar(req.instrument.clone(), req.timeframe.clone())
    };

    let session = match strategy_stream
        .attach(
            &req.owner,
            key,
            req.after,
            req.include_baseline,
            req.report_replay_through,
        )
        .await
    {
        Ok(s) => s,
        Err(err) => {
            reject_ws(&mut sink, err.code, &err.message).await;
            return;
        }
    };

    let session_id = session.queue.id;
    let queue_handle = Arc::clone(&session.queue);
    let ready_json = serde_json::to_string(&session.ready).unwrap_or_default();
    if sink.send(Message::Text(ready_json.into())).await.is_err() {
        strategy_stream.detach(&req.owner, session_id, false);
        return;
    }
    for chunk in session.baseline.chunks(512) {
        let base_json = serde_json::json!({ "type": "baseline", "ids": chunk }).to_string();
        if sink.send(Message::Text(base_json.into())).await.is_err() {
            strategy_stream.detach(&req.owner, session_id, false);
            return;
        }
    }
    let mut last_seq = session.ready.seq;
    for ev in &session.replay {
        if sink.send(Message::Text(ev.to_json().into())).await.is_err() {
            strategy_stream.detach(&req.owner, session_id, false);
            return;
        }
        last_seq = ev.seq();
    }

    let mut clean_release = false;
    loop {
        tokio::select! {
            ev_opt = queue_handle.next_event() => {
                match ev_opt {
                    Some(ev) => {
                        let seq = ev.seq();
                        if seq > last_seq {
                            if sink.send(Message::Text(ev.to_json().into())).await.is_err() {
                                break;
                            }
                            last_seq = seq;
                        }
                    }
                    None => {
                        if queue_handle.is_interrupted() {
                            let _ = sink
                                .send(Message::Close(Some(CloseFrame {
                                    code: 1013,
                                    reason: "consumer queue overflow".into(),
                                })))
                                .await;
                        }
                        break;
                    }
                }
            }
            msg = stream.next() => {
                match msg {
                    Some(Ok(Message::Close(frame))) => {
                        if let Some(cf) = frame
                            && (cf.code == 1000 || cf.code == 1001)
                        {
                            clean_release = true;
                        }
                        break;
                    }
                    None => break,
                    Some(Ok(_)) => {}
                    Some(Err(_)) => break,
                }
            }
        }
    }

    strategy_stream.detach(&req.owner, session_id, clean_release);
}
