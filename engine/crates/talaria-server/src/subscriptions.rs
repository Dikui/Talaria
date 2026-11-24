//! Upstream real-time subscription quota scheduler.
//!
//! Priority order: `strategy > active_chart > active_alerts > watchlist`.
//! Multiple callers sharing `(instrument_id, timeframe)` share one upstream
//! subscription slot on `FeedSupervisor`.

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex, OnceLock};

use talaria_contracts::watchlist::DegradationReason;
use talaria_feed::FeedSupervisor;

use crate::error::{ApiError, SUBSCRIPTION_QUOTA};

pub const WATCHLIST_TIMEFRAME: &str = "1m";

pub type SubscriptionKey = (String, String);

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub enum OwnerKind {
    Chart,
    Alert,
    Watchlist,
}

pub type Owner = (OwnerKind, String, SubscriptionKey);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SubscriptionView {
    pub selected: HashSet<String>,
    pub subscribed: HashSet<SubscriptionKey>,
    pub degraded: BTreeMap<String, DegradationReason>,
    pub chart_instruments: HashSet<String>,
    pub used: usize,
    pub quota: usize,
}

type DegradedCallback = Arc<dyn Fn(&Owner) + Send + Sync>;
type AlertQuotaCallback = Arc<dyn Fn(&[String]) + Send + Sync>;

struct Inner {
    quota: usize,
    watchlist_timeframe: String,
    accounts: HashMap<String, SubscriptionKey>,
    chart_refs: BTreeMap<SubscriptionKey, usize>,
    managed: BTreeSet<SubscriptionKey>,
    watchlist_order: Vec<String>,
    alerts: BTreeMap<String, SubscriptionKey>,
    denied: BTreeSet<Owner>,
}

/// Priority-ordered realtime subscription scheduler shared across WS and watchlist routes.
pub struct SubscriptionScheduler {
    supervisor: OnceLock<Arc<FeedSupervisor>>,
    inner: Mutex<Inner>,
    on_degraded: Option<DegradedCallback>,
    on_alert_quota: Option<AlertQuotaCallback>,
}

impl SubscriptionScheduler {
    pub fn new(
        supervisor: Option<Arc<FeedSupervisor>>,
        quota: usize,
        watchlist_timeframe: impl Into<String>,
        on_degraded: Option<DegradedCallback>,
        on_alert_quota: Option<AlertQuotaCallback>,
    ) -> Result<Self, ApiError> {
        if quota == 0 {
            return Err(ApiError::validation("realtime quota must be positive"));
        }
        let lock = OnceLock::new();
        if let Some(sup) = supervisor {
            let _ = lock.set(sup);
        }
        Ok(Self {
            supervisor: lock,
            inner: Mutex::new(Inner {
                quota,
                watchlist_timeframe: watchlist_timeframe.into(),
                accounts: HashMap::new(),
                chart_refs: BTreeMap::new(),
                managed: BTreeSet::new(),
                watchlist_order: Vec::new(),
                alerts: BTreeMap::new(),
                denied: BTreeSet::new(),
            }),
            on_degraded,
            on_alert_quota,
        })
    }

    pub fn set_supervisor(&self, supervisor: Arc<FeedSupervisor>) {
        let _ = self.supervisor.set(supervisor);
        let (to_unsub, to_sub, newly_denied, changed_alerts) = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            guard.managed.clear();
            Self::compute_assignment(&mut guard)
        };
        self.apply_supervisor_changes(to_unsub, to_sub, newly_denied, changed_alerts);
    }

    pub fn quota(&self) -> usize {
        self.inner.lock().expect("scheduler poisoned").quota
    }

    pub fn used(&self) -> usize {
        self.inner.lock().expect("scheduler poisoned").managed.len()
    }

    pub fn denied_alerts(&self) -> Vec<String> {
        let guard = self.inner.lock().expect("scheduler poisoned");
        let mut out: BTreeSet<String> = BTreeSet::new();
        for (kind, id, _) in &guard.denied {
            if *kind == OwnerKind::Alert {
                out.insert(id.clone());
            }
        }
        out.into_iter().collect()
    }

    pub fn check_account(&self, owner: &str, key: &SubscriptionKey) -> Result<(), ApiError> {
        let guard = self.inner.lock().expect("scheduler poisoned");
        if guard.accounts.get(owner) == Some(key) {
            return Ok(());
        }
        if guard.accounts.contains_key(owner) {
            return Err(ApiError::validation(
                "same strategy owner already holds another channel",
            ));
        }
        let account_keys: HashSet<&SubscriptionKey> = guard.accounts.values().collect();
        if !account_keys.contains(key) && account_keys.len() >= guard.quota {
            return Err(quota_error(key));
        }
        Ok(())
    }

    pub fn acquire_account(&self, owner: &str, key: SubscriptionKey) -> Result<(), ApiError> {
        let (to_unsub, to_sub, newly_denied, changed_alerts) = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            if guard.accounts.get(owner) == Some(&key) {
                return Ok(());
            }
            if guard.accounts.contains_key(owner) {
                return Err(ApiError::validation(
                    "same strategy owner already holds another channel",
                ));
            }
            let account_keys: HashSet<&SubscriptionKey> = guard.accounts.values().collect();
            if !account_keys.contains(&key) && account_keys.len() >= guard.quota {
                return Err(quota_error(&key));
            }
            guard.accounts.insert(owner.to_owned(), key);
            Self::compute_assignment(&mut guard)
        };
        self.apply_supervisor_changes(to_unsub, to_sub, newly_denied, changed_alerts);
        Ok(())
    }

    pub fn release_account(&self, owner: &str) {
        let ops = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            if guard.accounts.remove(owner).is_some() {
                Some(Self::compute_assignment(&mut guard))
            } else {
                None
            }
        };
        if let Some((to_unsub, to_sub, newly_denied, changed_alerts)) = ops {
            self.apply_supervisor_changes(to_unsub, to_sub, newly_denied, changed_alerts);
        }
    }

    pub fn acquire_chart(&self, key: SubscriptionKey) -> Result<(), ApiError> {
        let ops = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            let mut protected: HashSet<SubscriptionKey> =
                guard.accounts.values().cloned().collect();
            protected.extend(guard.chart_refs.keys().cloned());
            if !protected.contains(&key) && protected.len() >= guard.quota {
                return Err(quota_error(&key));
            }
            *guard.chart_refs.entry(key).or_insert(0) += 1;
            Self::compute_assignment(&mut guard)
        };
        self.apply_supervisor_changes(ops.0, ops.1, ops.2, ops.3);
        Ok(())
    }

    pub fn release_chart(&self, key: &SubscriptionKey) {
        let ops = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            match guard.chart_refs.get_mut(key) {
                Some(cnt) if *cnt > 1 => {
                    *cnt -= 1;
                    None
                }
                Some(_) => {
                    guard.chart_refs.remove(key);
                    Some(Self::compute_assignment(&mut guard))
                }
                None => None,
            }
        };
        if let Some((to_unsub, to_sub, newly_denied, changed_alerts)) = ops {
            self.apply_supervisor_changes(to_unsub, to_sub, newly_denied, changed_alerts);
        }
    }

    pub fn reconcile(
        &self,
        instrument_ids: &[String],
        alerts: Option<&BTreeMap<String, SubscriptionKey>>,
    ) -> SubscriptionView {
        let (ops, view) = {
            let mut guard = self.inner.lock().expect("scheduler poisoned");
            let mut seen = HashSet::new();
            let mut order = Vec::with_capacity(instrument_ids.len());
            for id in instrument_ids {
                if seen.insert(id.clone()) {
                    order.push(id.clone());
                }
            }
            guard.watchlist_order = order;
            if let Some(alert_map) = alerts {
                guard.alerts = alert_map.clone();
            }
            let ops = Self::compute_assignment(&mut guard);
            let view = Self::build_view(&guard);
            (ops, view)
        };
        self.apply_supervisor_changes(ops.0, ops.1, ops.2, ops.3);
        view
    }

    pub fn current_view(&self) -> SubscriptionView {
        let guard = self.inner.lock().expect("scheduler poisoned");
        Self::build_view(&guard)
    }

    fn build_view(guard: &Inner) -> SubscriptionView {
        let selected: HashSet<String> = guard
            .managed
            .iter()
            .filter(|(_, tf)| tf == &guard.watchlist_timeframe)
            .map(|(inst, _)| inst.clone())
            .collect();
        let mut degraded = BTreeMap::new();
        for inst in &guard.watchlist_order {
            if !selected.contains(inst) {
                degraded.insert(inst.clone(), DegradationReason::Quota);
            }
        }
        let chart_instruments: HashSet<String> =
            guard.chart_refs.keys().map(|(inst, _)| inst.clone()).collect();
        SubscriptionView {
            selected,
            subscribed: guard.managed.iter().cloned().collect(),
            degraded,
            chart_instruments,
            used: guard.managed.len(),
            quota: guard.quota,
        }
    }

    fn compute_assignment(
        guard: &mut Inner,
    ) -> (
        Vec<SubscriptionKey>,
        Vec<SubscriptionKey>,
        Vec<Owner>,
        Option<Vec<String>>,
    ) {
        let mut wanted: BTreeSet<SubscriptionKey> = BTreeSet::new();
        let mut denied: BTreeSet<Owner> = BTreeSet::new();

        // Priority 1: strategy accounts
        for key in guard.accounts.values() {
            if wanted.contains(key) || wanted.len() < guard.quota {
                wanted.insert(key.clone());
            }
        }

        // Priority 2: active chart viewers
        for key in guard.chart_refs.keys() {
            if wanted.contains(key) || wanted.len() < guard.quota {
                wanted.insert(key.clone());
            } else {
                let id = format!("{}:{}", key.0, key.1);
                denied.insert((OwnerKind::Chart, id, key.clone()));
            }
        }

        // Priority 3: active alerts (any timeframe)
        for (alert_id, key) in &guard.alerts {
            if wanted.contains(key) || wanted.len() < guard.quota {
                wanted.insert(key.clone());
            } else {
                denied.insert((OwnerKind::Alert, alert_id.clone(), key.clone()));
            }
        }

        // Priority 4: watchlist on 1m
        for inst in &guard.watchlist_order {
            let key = (inst.clone(), guard.watchlist_timeframe.clone());
            if wanted.contains(&key) || wanted.len() < guard.quota {
                wanted.insert(key);
            } else {
                denied.insert((OwnerKind::Watchlist, inst.clone(), key));
            }
        }

        let to_unsub: Vec<SubscriptionKey> = guard.managed.difference(&wanted).cloned().collect();
        let to_sub: Vec<SubscriptionKey> = wanted.difference(&guard.managed).cloned().collect();
        for k in &to_unsub {
            guard.managed.remove(k);
        }
        for k in &to_sub {
            guard.managed.insert(k.clone());
        }

        let prev_alerts = denied_alert_ids(&guard.denied);
        let next_alerts = denied_alert_ids(&denied);
        let newly_denied: Vec<Owner> = denied.difference(&guard.denied).cloned().collect();
        guard.denied = denied;
        let changed_alerts = if prev_alerts != next_alerts {
            Some(next_alerts)
        } else {
            None
        };
        (to_unsub, to_sub, newly_denied, changed_alerts)
    }

    fn apply_supervisor_changes(
        &self,
        to_unsub: Vec<SubscriptionKey>,
        to_sub: Vec<SubscriptionKey>,
        newly_denied: Vec<Owner>,
        changed_alerts: Option<Vec<String>>,
    ) {
        if let Some(sup) = self.supervisor.get() {
            for (inst, tf) in to_unsub {
                sup.unsubscribe(&inst, &tf);
            }
            for (inst, tf) in to_sub {
                if let Err(err) = sup.subscribe(&inst, &tf) {
                    tracing::warn!(instrument = %inst, timeframe = %tf, error = %err, "subscribe failed");
                }
            }
        }
        if let Some(cb) = &self.on_degraded {
            for owner in &newly_denied {
                cb(owner);
            }
        }
        if let (Some(cb), Some(denied_alerts)) = (&self.on_alert_quota, &changed_alerts) {
            cb(denied_alerts);
        }
    }
}

fn denied_alert_ids(denied: &BTreeSet<Owner>) -> Vec<String> {
    let mut ids = BTreeSet::new();
    for (kind, id, _) in denied {
        if *kind == OwnerKind::Alert {
            ids.insert(id.clone());
        }
    }
    ids.into_iter().collect()
}

fn quota_error(key: &SubscriptionKey) -> ApiError {
    ApiError::with_status(
        axum::http::StatusCode::TOO_MANY_REQUESTS,
        SUBSCRIPTION_QUOTA,
        format!("实时订阅配额已满：{} {}", key.0, key.1),
    )
}
