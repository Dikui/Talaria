//! Event-driven deterministic Alert Engine.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use rust_decimal::Decimal;
use sha2::{Digest, Sha256};
use talaria_contracts::alerts::{
    AlertRecord, AlertStatus, AlertTriggerKind, AlertTriggerRecord,
};
use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::drawings::{DrawingKind, DrawingRecord};
use talaria_contracts::feed::{
    DerivativeTickEvent, FeedEvent, FeedHealthEvent, FeedHealthStatus,
};
use talaria_contracts::key_levels::KeyLevel;
use talaria_contracts::rules::{
    AlertDirection, AlertLevelEvent, AlertMetric, AlertOverlayMode, RuleComparisonTest,
    RuleExpression, RuleIndicatorName, RuleOperand, RuleTiming,
};
use talaria_contracts::structures::StructureFeature;
use talaria_contracts::timeframe_ms;
use uuid::Uuid;

use crate::error::ApiError;
use crate::rules::{
    DEFAULT_RULE_HISTORY, EvalContext, FeedEventRef, LevelSide, RuleObservation, RuleState,
    level_bounds, observe_rule, prior_root_false, rule_indicator_to_spec, validate_rule_complexity,
};

type SeriesKey = (String, String);
type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type HistoryProvider = Arc<dyn Fn(&str, &str) -> Vec<CompactBar> + Send + Sync>;
type DrawingProvider = Arc<dyn Fn(&str) -> Option<DrawingRecord> + Send + Sync>;
type LevelProvider = Arc<dyn Fn(&str, &str, i64) -> HashMap<String, KeyLevel> + Send + Sync>;
type StructureProvider =
    Arc<dyn Fn(&str, &str, i64) -> HashMap<String, StructureFeature> + Send + Sync>;
type DerivativeArchiveProvider =
    Arc<dyn Fn(&str, DerivativeKind, i64) -> Vec<DerivativePoint> + Send + Sync>;
type FreezeSnapshotFn = Arc<dyn Fn(&AlertRecord, i64, &[DerivativePoint]) -> String + Send + Sync>;
type OnTriggerFn = Arc<dyn Fn(&AlertTriggerRecord, &AlertRecord) + Send + Sync>;

#[derive(Debug, Clone, Default)]
struct AlertRuntime {
    condition_true: Option<bool>,
    baseline_pending: bool,
    last_bar_key: Option<(i64, u64)>,
    partial_inputs: HashSet<Vec<String>>,
    last_cross_ts: Option<i64>,
    rule_state: RuleState,
}

#[derive(Default)]
struct ConfirmedWindow {
    limit: usize,
    bars: BTreeMap<i64, CompactBar>,
}

impl ConfirmedWindow {
    fn new(limit: usize) -> Self {
        Self {
            limit: limit.max(64),
            bars: BTreeMap::new(),
        }
    }

    fn put(&mut self, bar: CompactBar) {
        self.bars.insert(bar.timestamp, bar);
        while self.bars.len() > self.limit {
            if let Some(&first_ts) = self.bars.keys().next() {
                self.bars.remove(&first_ts);
            }
        }
    }

    fn visible(&self, cutoff_ts: i64, through: Option<i64>) -> Vec<CompactBar> {
        self.bars
            .values()
            .filter(|b| b.ts_available <= cutoff_ts && through.is_none_or(|t| b.timestamp <= t))
            .cloned()
            .collect()
    }

    fn latest_slot(&self, cutoff_ts: i64) -> Option<i64> {
        self.bars
            .values()
            .rev()
            .find(|b| b.ts_available <= cutoff_ts)
            .map(|b| b.timestamp)
    }
}

#[derive(Default)]
struct DerivativeBuffer {
    limit: usize,
    mark_price: Vec<DerivativePoint>,
    funding_rate: Vec<DerivativePoint>,
    open_interest: Vec<DerivativePoint>,
}

impl DerivativeBuffer {
    fn new(limit: usize) -> Self {
        Self {
            limit: limit.max(128),
            ..Default::default()
        }
    }

    fn push(&mut self, pt: DerivativePoint) {
        let vec = match pt.kind {
            DerivativeKind::MarkPrice => &mut self.mark_price,
            DerivativeKind::FundingRate => &mut self.funding_rate,
            DerivativeKind::OpenInterest => &mut self.open_interest,
        };
        if let Some(existing) = vec.iter_mut().find(|x| {
            x.ts_event == pt.ts_event
                && x.timeframe == pt.timeframe
                && x.is_funding_settlement() == pt.is_funding_settlement()
        }) {
            if pt.revision >= existing.revision {
                *existing = pt;
            }
        } else {
            vec.push(pt);
            vec.sort_by_key(|x| (x.ts_event, x.revision));
            if vec.len() > self.limit {
                let extra = vec.len() - self.limit;
                vec.drain(0..extra);
            }
        }
    }

    fn handle_tick(&mut self, tick: &DerivativeTickEvent) {
        let ts_avail = tick.ts_event.max(tick.ts_ingested);
        if let Some(mark) = tick.mark_price {
            self.push(DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::MarkPrice,
                ts_event: tick.ts_event,
                ts_published: None,
                ts_available: ts_avail,
                value: mark,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_avail,
                revision: 0,
                settled: None,
            });
        }
        if let Some(funding) = tick.funding_rate {
            self.push(DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::FundingRate,
                ts_event: tick.ts_event,
                ts_published: Some(tick.ts_event),
                ts_available: ts_avail,
                value: funding,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_avail,
                revision: 0,
                settled: Some(true),
            });
        }
        if let Some(oi) = tick.open_interest {
            self.push(DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::OpenInterest,
                ts_event: tick.ts_event,
                ts_published: None,
                ts_available: ts_avail,
                value: oi,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_avail,
                revision: 0,
                settled: None,
            });
        }
    }
}

struct Inner {
    alerts: BTreeMap<String, AlertRecord>,
    runtimes: HashMap<String, AlertRuntime>,
    windows: HashMap<SeriesKey, ConfirmedWindow>,
    seeded: HashSet<SeriesKey>,
    derivatives: HashMap<String, DerivativeBuffer>,
    drawings: HashMap<String, DrawingRecord>,
    stale: HashSet<SeriesKey>,
    skip_through: HashMap<SeriesKey, i64>,
}

/// Deterministic event-driven Alert Engine.
pub struct AlertEngine {
    now_ms: NowFn,
    bar_limit: usize,
    history_provider: Mutex<Option<HistoryProvider>>,
    drawing_provider: Mutex<Option<DrawingProvider>>,
    level_provider: Mutex<Option<LevelProvider>>,
    structure_provider: Mutex<Option<StructureProvider>>,
    derivative_archive: Mutex<Option<DerivativeArchiveProvider>>,
    freeze_snapshot: Mutex<Option<FreezeSnapshotFn>>,
    on_trigger: Mutex<Option<OnTriggerFn>>,
    inner: Mutex<Inner>,
}

impl AlertEngine {
    pub fn new(now_ms: NowFn, bar_limit: usize) -> Self {
        Self {
            now_ms,
            bar_limit: bar_limit.max(DEFAULT_RULE_HISTORY),
            history_provider: Mutex::new(None),
            drawing_provider: Mutex::new(None),
            level_provider: Mutex::new(None),
            structure_provider: Mutex::new(None),
            derivative_archive: Mutex::new(None),
            freeze_snapshot: Mutex::new(None),
            on_trigger: Mutex::new(None),
            inner: Mutex::new(Inner {
                alerts: BTreeMap::new(),
                runtimes: HashMap::new(),
                windows: HashMap::new(),
                seeded: HashSet::new(),
                derivatives: HashMap::new(),
                drawings: HashMap::new(),
                stale: HashSet::new(),
                skip_through: HashMap::new(),
            }),
        }
    }

    pub fn set_callbacks(
        &self,
        history_provider: Option<HistoryProvider>,
        drawing_provider: Option<DrawingProvider>,
        level_provider: Option<LevelProvider>,
        derivative_archive: Option<DerivativeArchiveProvider>,
        freeze_snapshot: Option<FreezeSnapshotFn>,
        on_trigger: Option<OnTriggerFn>,
    ) {
        *self.history_provider.lock().unwrap() = history_provider;
        *self.drawing_provider.lock().unwrap() = drawing_provider;
        *self.level_provider.lock().unwrap() = level_provider;
        *self.derivative_archive.lock().unwrap() = derivative_archive;
        *self.freeze_snapshot.lock().unwrap() = freeze_snapshot;
        *self.on_trigger.lock().unwrap() = on_trigger;
    }

    pub fn set_structure_provider(&self, provider: Option<StructureProvider>) {
        *self.structure_provider.lock().unwrap() = provider;
    }

    pub fn load(&self, alerts: Vec<AlertRecord>) {
        for alert in alerts {
            let _ = self.put(alert, true);
        }
    }

    pub fn put(&self, alert: AlertRecord, reset: bool) -> Result<(), ApiError> {
        validate_rule_complexity(&alert.condition)?;
        let key = (alert.instrument_id.clone(), alert.timeframe.clone());
        let mut drawing_ids = Vec::new();
        collect_drawing_ids(&alert.condition.expression, &mut drawing_ids);
        let should_seed = {
            let mut guard = self.inner.lock().unwrap();
            let prev = guard.alerts.get(&alert.alert_id);
            if reset || prev.is_none_or(|p| p.condition != alert.condition) {
                guard
                    .runtimes
                    .insert(alert.alert_id.clone(), AlertRuntime::default());
            }
            guard.alerts.insert(alert.alert_id.clone(), alert);
            guard
                .windows
                .entry(key.clone())
                .or_insert_with(|| ConfirmedWindow::new(self.bar_limit));
            !guard.seeded.contains(&key)
        };
        if should_seed
            && let Some(provider) = self.history_provider.lock().unwrap().clone()
        {
            let bars = provider(&key.0, &key.1);
            self.seed_bars(&key.0, &key.1, &bars);
        }
        if let Some(d_provider) = self.drawing_provider.lock().unwrap().clone() {
            for id in drawing_ids {
                if let Some(drawing) = d_provider(&id) {
                    self.refresh_drawings(&[drawing]);
                }
            }
        }
        Ok(())
    }

    pub fn remove(&self, alert_id: &str) {
        let mut guard = self.inner.lock().unwrap();
        guard.alerts.remove(alert_id);
        guard.runtimes.remove(alert_id);
    }

    pub fn get(&self, alert_id: &str) -> Option<AlertRecord> {
        self.inner.lock().unwrap().alerts.get(alert_id).cloned()
    }

    pub fn list_alerts(&self) -> Vec<AlertRecord> {
        let guard = self.inner.lock().unwrap();
        let mut out: Vec<AlertRecord> = guard.alerts.values().cloned().collect();
        out.sort_by(|a, b| (a.created_ts, &a.alert_id).cmp(&(b.created_ts, &b.alert_id)));
        out
    }

    /// Returns `(alert_id -> (instrument_id, timeframe))` for ARMED alerts (used by quota scheduler).
    pub fn active_alert_subscriptions(&self) -> BTreeMap<String, (String, String)> {
        let guard = self.inner.lock().unwrap();
        let mut out = BTreeMap::new();
        for alert in guard.alerts.values() {
            if alert.status == AlertStatus::Armed {
                out.insert(
                    alert.alert_id.clone(),
                    (alert.instrument_id.clone(), alert.timeframe.clone()),
                );
            }
        }
        out
    }

    pub fn suspend_channel(&self, instrument_id: &str, timeframe: &str) {
        let mut guard = self.inner.lock().unwrap();
        guard
            .stale
            .insert((instrument_id.to_owned(), timeframe.to_owned()));
    }

    pub fn refresh_drawings(&self, drawings: &[DrawingRecord]) {
        let mut guard = self.inner.lock().unwrap();
        for d in drawings {
            if d.deleted_at.is_some() {
                guard.drawings.remove(&d.drawing_id);
            } else {
                guard.drawings.insert(d.drawing_id.clone(), d.clone());
            }
        }
    }

    pub fn drop_drawings(&self, ids: &[String]) {
        let mut guard = self.inner.lock().unwrap();
        for id in ids {
            guard.drawings.remove(id);
        }
    }

    pub fn seed_bars(&self, instrument_id: &str, timeframe: &str, bars: &[CompactBar]) {
        let mut guard = self.inner.lock().unwrap();
        let key = (instrument_id.to_owned(), timeframe.to_owned());
        guard.seeded.insert(key.clone());
        let win = guard
            .windows
            .entry(key)
            .or_insert_with(|| ConfirmedWindow::new(self.bar_limit));
        for bar in bars {
            win.put(bar.clone());
        }
    }

    pub fn seed_derivatives(&self, points: &[DerivativePoint]) {
        let mut guard = self.inner.lock().unwrap();
        for pt in points {
            let buf = guard
                .derivatives
                .entry(pt.instrument_id.clone())
                .or_insert_with(|| DerivativeBuffer::new(self.bar_limit * 4));
            buf.push(pt.clone());
        }
    }

    pub fn tick(&self, now_override: Option<i64>) -> Vec<AlertRecord> {
        let now = now_override.unwrap_or_else(|| (self.now_ms)());
        let mut guard = self.inner.lock().unwrap();
        let mut changed = Vec::new();
        let ids: Vec<String> = guard.alerts.keys().cloned().collect();
        for id in ids {
            if let Some(alert) = guard.alerts.get(&id).cloned() {
                let mut updated = alert;
                let mut reset_runtime = false;
                if advance_alert_status(&mut updated, now, &mut reset_runtime) {
                    if reset_runtime {
                        let rt = guard.runtimes.entry(id.clone()).or_default();
                        rt.baseline_pending = true;
                        rt.condition_true = None;
                        rt.rule_state = RuleState::default();
                        rt.partial_inputs.clear();
                    }
                    guard.alerts.insert(id, updated.clone());
                    changed.push(updated);
                }
            }
        }
        changed
    }

    /// Consume one feed or health event, returning updated `AlertRecord`s to persist.
    pub fn handle_event(&self, event: &FeedEvent) -> Vec<AlertRecord> {
        if let FeedEvent::Health(h) = event {
            self.handle_health(h);
            return Vec::new();
        }

        let now = (self.now_ms)();
        let instrument_id = match event {
            FeedEvent::Kline(k) => k.instrument_id.as_str(),
            FeedEvent::OrderBook(b) => b.instrument_id.as_str(),
            FeedEvent::DerivativeTick(d) => d.instrument_id.as_str(),
            FeedEvent::Trade(t) => t.instrument_id.as_str(),
            FeedEvent::Health(_) => return Vec::new(),
        };

        // Ensure derivative ticks and confirmed bars are buffered first
        {
            let mut guard = self.inner.lock().unwrap();
            if let FeedEvent::DerivativeTick(d) = event {
                let buf = guard
                    .derivatives
                    .entry(d.instrument_id.clone())
                    .or_insert_with(|| DerivativeBuffer::new(self.bar_limit * 4));
                buf.handle_tick(d);
            }
            if let FeedEvent::Kline(k) = event
                && k.confirmed
            {
                let key = (k.instrument_id.clone(), k.timeframe.clone());
                if let Some(win) = guard.windows.get_mut(&key) {
                    win.put(k.bar.clone());
                }
            }
        }

        let level_provider = self.level_provider.lock().unwrap().clone();
        let structure_provider = self.structure_provider.lock().unwrap().clone();
        let drawing_provider = self.drawing_provider.lock().unwrap().clone();
        let derivative_archive = self.derivative_archive.lock().unwrap().clone();
        let freeze_snapshot = self.freeze_snapshot.lock().unwrap().clone();
        let on_trigger = self.on_trigger.lock().unwrap().clone();

        let (pending, mut changed) = {
            let mut guard = self.inner.lock().unwrap();
            let mut changed: BTreeMap<String, AlertRecord> = BTreeMap::new();
            let mut pending: Vec<(AlertRecord, RuleObservation, i64, Vec<DerivativePoint>)> = Vec::new();

            let mut alert_ids: Vec<String> = guard
                .alerts
                .values()
                .filter(|a| {
                    a.instrument_id == instrument_id
                        && !guard
                            .stale
                            .contains(&(a.instrument_id.clone(), a.timeframe.clone()))
                })
                .map(|a| a.alert_id.clone())
                .collect();
            alert_ids.sort();

            for alert_id in alert_ids {
                let Some(mut alert) = guard.alerts.get(&alert_id).cloned() else {
                    continue;
                };
                let mut reset_runtime = false;
                if advance_alert_status(&mut alert, now, &mut reset_runtime) {
                    if reset_runtime {
                        let rt = guard.runtimes.entry(alert_id.clone()).or_default();
                        rt.baseline_pending = true;
                        rt.condition_true = None;
                        rt.rule_state = RuleState::default();
                        rt.partial_inputs.clear();
                    }
                    guard.alerts.insert(alert_id.clone(), alert.clone());
                    changed.insert(alert_id.clone(), alert.clone());
                }
                if alert.status != AlertStatus::Armed {
                    continue;
                }
                let tf_ms = match timeframe_ms(&alert.timeframe) {
                    Ok(v) => v,
                    Err(_) => continue,
                };
                if let FeedEvent::Kline(k) = event {
                    if k.timeframe != alert.timeframe {
                        continue;
                    }
                    if let Some(&skip_ts) = guard
                        .skip_through
                        .get(&(k.instrument_id.clone(), k.timeframe.clone()))
                        && k.bar.timestamp <= skip_ts
                    {
                        continue;
                    }
                }
                if alert.condition.timing == RuleTiming::BarClose {
                    match event {
                        FeedEvent::Kline(k) if k.confirmed => {}
                        _ => continue,
                    }
                }

                let ev_ref = match event {
                    FeedEvent::Kline(k) => FeedEventRef::Kline(k),
                    FeedEvent::OrderBook(b) => FeedEventRef::Book(b),
                    FeedEvent::DerivativeTick(d) => FeedEventRef::Derivative(d),
                    _ => continue,
                };
                let visible_cutoff = now.max(ev_ref.visible_at(tf_ms));

                let (bars, bar_ts_override, eval_cutoff) = {
                    let key = (alert.instrument_id.clone(), alert.timeframe.clone());
                    let Some(win) = guard.windows.get(&key) else {
                        continue;
                    };
                    let mut bar_ts = None;
                    let mut cutoff = ev_ref.visible_at(tf_ms);
                    if let FeedEvent::Kline(k) = event {
                        bar_ts = Some(k.bar.timestamp);
                        if !k.confirmed {
                            cutoff = cutoff.max(now);
                            bar_ts = Some(k.bar.timestamp - tf_ms);
                        } else if alert.condition.timing == RuleTiming::BarClose {
                            let receipt = cutoff.max(now);
                            if let Some(latest) = win.latest_slot(receipt)
                                && latest > k.bar.timestamp
                            {
                                if k.revision == 0 {
                                    continue;
                                }
                                cutoff = receipt;
                                bar_ts = Some(latest);
                            }
                        }
                    }
                    let mut bars = win.visible(cutoff, bar_ts);
                    // When timing=Event with an unconfirmed kline, append/keep bar awareness via FeedEventRef
                    if bars.is_empty()
                        && let FeedEvent::Kline(k) = event
                        && !k.confirmed
                    {
                        bars.push(k.bar.clone());
                    }
                    (bars, bar_ts, cutoff.max(visible_cutoff))
                };

                let mut funding = guard
                    .derivatives
                    .get(&alert.instrument_id)
                    .map(|b| b.funding_rate.clone())
                    .unwrap_or_default();
                let mut open_interest = guard
                    .derivatives
                    .get(&alert.instrument_id)
                    .map(|b| b.open_interest.clone())
                    .unwrap_or_default();
                let mut mark_price = guard
                    .derivatives
                    .get(&alert.instrument_id)
                    .map(|b| b.mark_price.clone())
                    .unwrap_or_default();

                if let Some(arch) = &derivative_archive {
                    if funding.is_empty() {
                        funding = arch(&alert.instrument_id, DerivativeKind::FundingRate, eval_cutoff);
                    }
                    if open_interest.is_empty() {
                        open_interest =
                            arch(&alert.instrument_id, DerivativeKind::OpenInterest, eval_cutoff);
                    }
                    if mark_price.is_empty() {
                        mark_price = arch(&alert.instrument_id, DerivativeKind::MarkPrice, eval_cutoff);
                    }
                }

                let levels = level_provider
                    .as_ref()
                    .map(|lp| lp(&alert.instrument_id, &alert.timeframe, eval_cutoff))
                    .unwrap_or_default();
                let structures = structure_provider
                    .as_ref()
                    .map(|sp| sp(&alert.instrument_id, &alert.timeframe, eval_cutoff))
                    .unwrap_or_default();

                let drawings_snapshot = guard.drawings.clone();
                let drawing_lookup = |id: &str| -> Option<DrawingRecord> {
                    drawings_snapshot
                        .get(id)
                        .cloned()
                        .or_else(|| drawing_provider.as_ref().and_then(|dp| dp(id)))
                };

                let rt = guard.runtimes.entry(alert_id.clone()).or_default();
                if let FeedEvent::Kline(k) = event
                    && rt.last_bar_key == Some((k.bar.timestamp, k.revision))
                {
                    continue;
                }

                let mut ctx = EvalContext {
                    instrument_id: &alert.instrument_id,
                    timeframe: &alert.timeframe,
                    now_ms: eval_cutoff,
                    bars: &bars,
                    funding: &funding,
                    open_interest: &open_interest,
                    mark_price: &mark_price,
                    levels: &levels,
                    structures: &structures,
                    drawing_lookup: &drawing_lookup,
                    event: if alert.condition.timing == RuleTiming::Event {
                        Some(ev_ref.clone())
                    } else {
                        None
                    },
                    bar_ts_override,
                    indicator_cache: BTreeMap::new(),
                };

                let obs_res = observe_rule(&alert.condition, &mut ctx, &mut rt.rule_state, true);
                let Ok(obs_opt) = obs_res else {
                    continue;
                };
                let mut inputs: Vec<String> = rt.rule_state.known_inputs.iter().cloned().collect();
                inputs.sort();
                let Some(obs) = obs_opt else {
                    if alert.condition.timing == RuleTiming::Event
                        && !inputs.is_empty()
                        && !rt.baseline_pending
                    {
                        rt.partial_inputs.insert(inputs);
                    }
                    continue;
                };
                if let FeedEvent::Kline(k) = event {
                    rt.last_bar_key = Some((k.bar.timestamp, k.revision));
                }

                let mut previous = if rt.baseline_pending {
                    None
                } else {
                    rt.condition_true
                };
                if rt.partial_inputs.contains(&inputs) {
                    previous = Some(false);
                }
                rt.partial_inputs.clear();

                let initial_cross = if previous.is_none()
                    && !rt.baseline_pending
                    && obs.crossed_at.is_some()
                {
                    prior_root_false(&alert.condition, &mut ctx).unwrap_or(false)
                } else {
                    false
                };

                rt.baseline_pending = false;
                rt.condition_true = Some(obs.matched);
                if !obs.matched || (previous != Some(false) && !initial_cross) {
                    continue;
                }
                if let Some(cross_ts) = obs.crossed_at {
                    if rt.last_cross_ts == Some(cross_ts) {
                        continue;
                    }
                    rt.last_cross_ts = Some(cross_ts);
                }

                let mut trigger_derivs = Vec::with_capacity(
                    mark_price.len() + funding.len() + open_interest.len(),
                );
                trigger_derivs.extend(mark_price);
                trigger_derivs.extend(funding);
                trigger_derivs.extend(open_interest);
                pending.push((alert, obs, eval_cutoff, trigger_derivs));
            }
            (pending, changed)
        };

        // Outside lock: freeze snapshots & emit trigger callbacks cleanly
        for (mut alert, obs, as_of, trigger_derivs) in pending {
            let snapshot_id = freeze_snapshot
                .as_ref()
                .map(|cb| cb(&alert, as_of, &trigger_derivs))
                .unwrap_or_default();
            let trigger = AlertTriggerRecord {
                trigger_id: format!("trg_{}", Uuid::new_v4().simple()),
                alert_id: alert.alert_id.clone(),
                instrument_id: alert.instrument_id.clone(),
                timeframe: alert.timeframe.clone(),
                condition_kind: trigger_kind(&alert),
                triggered_ts: now,
                snapshot_id,
                observed: obs.observed,
                threshold: trigger_threshold(&alert, &obs),
                message: format_trigger_message(&alert, &obs),
                note: alert.note.clone(),
            };
            alert.trigger_count += 1;
            alert.last_triggered_ts = Some(now);
            alert.updated_ts = now;
            if !alert.repeat {
                alert.status = AlertStatus::Expired;
                alert.next_arm_ts = None;
            } else if alert.cooldown_ms > 0 {
                alert.status = AlertStatus::Cooldown;
                alert.next_arm_ts = Some(now + alert.cooldown_ms as i64);
            } else {
                alert.status = AlertStatus::Armed;
                alert.next_arm_ts = None;
            }
            {
                let mut guard = self.inner.lock().unwrap();
                guard.alerts.insert(alert.alert_id.clone(), alert.clone());
            }
            changed.insert(alert.alert_id.clone(), alert.clone());
            if let Some(cb) = &on_trigger {
                cb(&trigger, &alert);
            }
        }

        changed.into_values().collect()
    }

    fn handle_health(&self, event: &FeedHealthEvent) {
        let key = (event.instrument_id.clone(), event.timeframe.clone());
        let mut guard = self.inner.lock().unwrap();
        if event.status == FeedHealthStatus::Live {
            if guard.stale.remove(&key) {
                if let Some(last_ts) = event.last_event_ts {
                    guard.skip_through.insert(key.clone(), last_ts);
                }
                let ids: Vec<String> = guard
                    .alerts
                    .values()
                    .filter(|a| (a.instrument_id.as_str(), a.timeframe.as_str()) == (&key.0, &key.1))
                    .map(|a| a.alert_id.clone())
                    .collect();
                for id in ids {
                    let rt = guard.runtimes.entry(id).or_default();
                    rt.baseline_pending = true;
                    rt.condition_true = None;
                    rt.rule_state = RuleState::default();
                    rt.partial_inputs.clear();
                }
            }
        } else {
            guard.stale.insert(key);
        }
    }
}

fn advance_alert_status(alert: &mut AlertRecord, now: i64, reset_runtime: &mut bool) -> bool {
    if matches!(
        alert.status,
        AlertStatus::Draft | AlertStatus::Disabled | AlertStatus::Expired
    ) {
        return false;
    }
    let mut changed = false;
    if let Some(exp) = alert.expires_at
        && now >= exp
    {
        alert.status = AlertStatus::Expired;
        alert.next_arm_ts = None;
        changed = true;
    } else if alert.status == AlertStatus::Cooldown
        && let Some(next_arm) = alert.next_arm_ts
        && now >= next_arm
    {
        alert.status = AlertStatus::Armed;
        alert.next_arm_ts = None;
        *reset_runtime = true;
        changed = true;
    }
    if changed {
        alert.updated_ts = now;
    }
    changed
}

fn collect_drawing_ids(node: &RuleExpression, out: &mut Vec<String>) {
    match node {
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
                collect_drawing_ids(child, out);
            }
        }
        RuleExpression::LevelEvent { .. } | RuleExpression::StructureEvent { .. } => {}
    }
}

fn trigger_kind(alert: &AlertRecord) -> AlertTriggerKind {
    match &alert.condition.expression {
        RuleExpression::Overlay { .. } => AlertTriggerKind::OverlayCross,
        RuleExpression::LevelEvent { .. } | RuleExpression::StructureEvent { .. } => {
            AlertTriggerKind::LevelEvent
        }
        RuleExpression::Compare {
            test: RuleComparisonTest::Gte | RuleComparisonTest::Lte,
            left,
            right,
            ..
        } => {
            match (left.as_ref(), right.as_ref()) {
                (RuleOperand::Event { .. }, RuleOperand::Constant { .. }) => {
                    AlertTriggerKind::MetricThreshold
                }
                (
                    RuleOperand::Indicator { .. },
                    RuleOperand::Constant { .. } | RuleOperand::Indicator { .. },
                ) => AlertTriggerKind::IndicatorSignal,
                _ => AlertTriggerKind::Rule,
            }
        }
        _ => AlertTriggerKind::Rule,
    }
}

fn trigger_threshold(alert: &AlertRecord, obs: &RuleObservation) -> Option<Decimal> {
    if let RuleExpression::Compare {
        test, left, right, ..
    } = &alert.condition.expression
        && matches!(test, RuleComparisonTest::Gte | RuleComparisonTest::Lte)
        && matches!(left.as_ref(), RuleOperand::Indicator { .. })
        && matches!(right.as_ref(), RuleOperand::Indicator { .. })
    {
        return None;
    }
    obs.threshold
}

fn format_trigger_message(alert: &AlertRecord, obs: &RuleObservation) -> String {
    match &alert.condition.expression {
        RuleExpression::LevelEvent { .. } => format_level_message(obs),
        RuleExpression::StructureEvent { .. } => format_structure_message(obs),
        RuleExpression::Compare {
            test: test @ (RuleComparisonTest::Gte | RuleComparisonTest::Lte),
            left,
            right,
            ..
        } => {
            let dir = if *test == RuleComparisonTest::Gte {
                "上穿"
            } else {
                "下穿"
            };
            match (left.as_ref(), right.as_ref()) {
                (RuleOperand::Event { metric }, RuleOperand::Constant { .. }) => {
                    format!("{}{} {}", metric_label(*metric), dir, decimal_text(obs.threshold))
                }
                (
                    RuleOperand::Indicator { name, params, .. },
                    RuleOperand::Indicator {
                        name: r_name,
                        params: r_params,
                        ..
                    },
                ) => {
                    format!(
                        "{} {} {}",
                        indicator_label(*name, params),
                        dir,
                        indicator_label(*r_name, r_params)
                    )
                }
                (RuleOperand::Indicator { name, params, .. }, RuleOperand::Constant { .. }) => {
                    format!(
                        "{} {} {}",
                        indicator_label(*name, params),
                        dir,
                        decimal_text(obs.threshold)
                    )
                }
                _ => "规则触发".to_owned(),
            }
        }
        RuleExpression::Overlay {
            mode, direction, ..
        } => {
            let _dir = if *direction == AlertDirection::Above {
                "上穿"
            } else {
                "下穿"
            };
            let label = match obs.drawing_kind {
                Some(DrawingKind::HorizontalLine) => "水平线",
                Some(DrawingKind::Ray) => "射线",
                Some(DrawingKind::Segment) => "线段",
                Some(DrawingKind::ParallelChannel) => "通道",
                Some(DrawingKind::FibRetracement) => "斐波那契",
                Some(DrawingKind::Rectangle) => "矩形",
                _ => "画线",
            };
            let edge = if matches!(
                obs.drawing_kind,
                Some(DrawingKind::ParallelChannel | DrawingKind::Rectangle)
            ) {
                if *direction == AlertDirection::Above {
                    "上沿"
                } else {
                    "下沿"
                }
            } else {
                ""
            };
            let action = if *mode == AlertOverlayMode::CloseBreak {
                "收盘突破"
            } else {
                "触碰"
            };
            format!("价格{action}{label}{edge} {}", decimal_text(obs.threshold))
        }
        _ => "规则触发".to_owned(),
    }
}

fn format_structure_message(obs: &RuleObservation) -> String {
    let label = obs
        .structure
        .as_ref()
        .map(|s| {
            if s.kind.is_empty() {
                s.detector_id.as_str()
            } else {
                s.kind.as_str()
            }
        })
        .unwrap_or("-");
    let family = obs
        .structure
        .as_ref()
        .map(|s| serde_json::to_string(&s.family).unwrap_or_default().replace('"', ""))
        .unwrap_or_else(|| "-".to_owned());
    let status = obs
        .structure
        .as_ref()
        .map(|s| serde_json::to_string(&s.status).unwrap_or_default().replace('"', ""))
        .unwrap_or_else(|| "-".to_owned());
    match obs.level_event {
        Some(AlertLevelEvent::Touch) => format!("价格触及结构 {label}（{family}）"),
        Some(AlertLevelEvent::Break) => {
            let dir = if obs.level_direction == Some(LevelSide::Up) {
                "上穿"
            } else {
                "下穿"
            };
            format!("收盘{dir}结构 {label}（{family}·{status}）")
        }
        _ => {
            let side = if obs.level_direction == Some(LevelSide::Up) {
                "上方"
            } else {
                "下方"
            };
            format!("价格回踩结构 {label} 并收盘回到{side}（{family}·{status}）")
        }
    }
}

fn format_level_message(obs: &RuleObservation) -> String {
    let bounds = obs.level.as_ref().and_then(level_bounds);
    let label = match bounds {
        Some((low, high)) if low == high => decimal_text(Some(low)),
        Some((low, high)) => format!("{}–{}", decimal_text(Some(low)), decimal_text(Some(high))),
        None => "-".to_owned(),
    };
    let kind = obs
        .level
        .as_ref()
        .map(|l| serde_json::to_string(&l.kind).unwrap_or_default().replace('"', ""))
        .unwrap_or_else(|| "-".to_owned());
    let status = obs
        .level
        .as_ref()
        .map(|l| serde_json::to_string(&l.status).unwrap_or_default().replace('"', ""))
        .unwrap_or_else(|| "-".to_owned());
    match obs.level_event {
        Some(AlertLevelEvent::Touch) => format!("价格触及关键位 {label}（{kind}）"),
        Some(AlertLevelEvent::Break) => {
            let dir = if obs.level_direction == Some(LevelSide::Up) {
                "上穿"
            } else {
                "下穿"
            };
            format!("收盘{dir}关键位 {label}（{kind}·{status}）")
        }
        _ => {
            let side = if obs.level_direction == Some(LevelSide::Up) {
                "上方"
            } else {
                "下方"
            };
            format!("价格回踩关键位 {label} 并收盘回到{side}（{kind}·{status}）")
        }
    }
}

fn metric_label(metric: AlertMetric) -> &'static str {
    match metric {
        AlertMetric::LastPrice => "价格",
        AlertMetric::BidPrice => "买一价",
        AlertMetric::AskPrice => "卖一价",
        AlertMetric::MarkPrice => "标记价格",
        AlertMetric::FundingRate => "资金费率",
        AlertMetric::OpenInterest => "持仓量",
    }
}

fn indicator_label(
    name: RuleIndicatorName,
    params: &talaria_contracts::rules::RuleIndicatorParams,
) -> String {
    if let Ok((canonical, ind_params, _)) = rule_indicator_to_spec(name, params) {
        let name_str = serde_json::to_string(&canonical)
            .unwrap_or_default()
            .replace('"', "");
        return format!("{name_str}({})", ind_params.period);
    }
    format!("{name:?}")
}

fn decimal_text(value: Option<Decimal>) -> String {
    match value {
        Some(v) => v.normalize().to_string(),
        None => "-".to_owned(),
    }
}

/// Deterministic alert trigger ID helper (for callers needing ID stability).
pub fn deterministic_alert_hash(alert_id: &str, ts: i64) -> String {
    let mut hasher = Sha256::new();
    hasher.update(alert_id.as_bytes());
    hasher.update(ts.to_le_bytes());
    hex::encode(hasher.finalize())
}
