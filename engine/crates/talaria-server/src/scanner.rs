//! Deterministic numerical market scanner (`ScannerEngine`).

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::sync::{Arc, Mutex};

use rust_decimal::Decimal;
use rust_decimal::prelude::ToPrimitive;
use sha2::{Digest, Sha256};
use talaria_contracts::bars::{BarStatus, CompactBar};
use talaria_contracts::derivatives::{
    DerivativeKind, DerivativePoint, latest_visible_derivatives, oi_timeframe_for,
};
use talaria_contracts::drawings::DrawingRecord;
use talaria_contracts::feed::{DerivativeTickEvent, FeedEvent, FeedHealthStatus};
use talaria_contracts::key_levels::KeyLevel;
use talaria_contracts::rules::{Rule, RuleExpression, RuleOperand, RuleTiming};
use talaria_contracts::scanner::{
    ScannerConditionKind, ScannerEvidence, ScannerSeverity, ScannerTrigger, ScannerTriggerRoute,
};
use talaria_contracts::timeframe_ms;
use talaria_operators::compute_key_levels;

use crate::error::ApiError;
use crate::rules::{EvalContext, RuleState, observe_rule, validate_rule_complexity};

const KIND_ORDER: [ScannerConditionKind; 5] = [
    ScannerConditionKind::LiquidityAnomaly,
    ScannerConditionKind::VolatilityExpansion,
    ScannerConditionKind::FundingExtreme,
    ScannerConditionKind::OiDivergence,
    ScannerConditionKind::KeyLevelEvent,
];

#[derive(Debug, Clone)]
pub struct ScannerConfig {
    pub timeframe: String,
    pub max_bars: usize,
    pub liquidity_window: usize,
    pub liquidity_min_samples: usize,
    pub liquidity_zscore: f64,
    pub liquidity_quantile: f64,
    pub volatility_window: usize,
    pub volatility_min_samples: usize,
    pub volatility_quantile: f64,
    pub volatility_ratio: f64,
    pub atr_period: usize,
    pub funding_lookback: usize,
    pub funding_min_samples: usize,
    pub funding_high_quantile: f64,
    pub funding_low_quantile: f64,
    pub funding_max_age_ms: i64,
    pub oi_lookback: usize,
    pub oi_min_samples: usize,
    pub oi_price_move_min: f64,
    pub oi_change_min: f64,
    pub key_level_min_strength: f64,
    pub key_level_touch_tolerance: f64,
    pub cooldown_ms: i64,
    pub focus_ttl_ms: i64,
}

impl Default for ScannerConfig {
    fn default() -> Self {
        Self {
            timeframe: "1m".to_owned(),
            max_bars: 300,
            liquidity_window: 60,
            liquidity_min_samples: 20,
            liquidity_zscore: 2.5,
            liquidity_quantile: 0.98,
            volatility_window: 60,
            volatility_min_samples: 20,
            volatility_quantile: 0.95,
            volatility_ratio: 1.5,
            atr_period: 14,
            funding_lookback: 90,
            funding_min_samples: 24,
            funding_high_quantile: 0.95,
            funding_low_quantile: 0.05,
            funding_max_age_ms: 36 * 60 * 60 * 1000,
            oi_lookback: 6,
            oi_min_samples: 3,
            oi_price_move_min: 0.005,
            oi_change_min: 0.01,
            key_level_min_strength: 0.7,
            key_level_touch_tolerance: 0.001,
            cooldown_ms: 5 * 60 * 1000,
            focus_ttl_ms: 30 * 60 * 1000,
        }
    }
}

type NowFn = Arc<dyn Fn() -> i64 + Send + Sync>;
type FreezeSnapshotFn = Arc<dyn Fn(&str, &str, i64) -> String + Send + Sync>;
type RuleLevelsProvider = Arc<dyn Fn(&str, &str, i64) -> Vec<KeyLevel> + Send + Sync>;

struct ScanCandidate {
    instrument_id: String,
    bars: Vec<CompactBar>,
    funding: Vec<DerivativePoint>,
    oi: Vec<DerivativePoint>,
    mark: Vec<DerivativePoint>,
}

struct Inner {
    bars: HashMap<(String, String), BTreeMap<i64, CompactBar>>,
    stale: HashSet<String>,
    funding: HashMap<String, Vec<DerivativePoint>>,
    open_interest: HashMap<String, Vec<DerivativePoint>>,
    mark_price: HashMap<String, Vec<DerivativePoint>>,
    rule_hits: HashMap<(String, String), (i64, i64)>,
    last_trigger_at: HashMap<(String, ScannerConditionKind), i64>,
    last_trigger_bar: HashMap<(String, ScannerConditionKind), i64>,
    focus: HashMap<String, i64>,
    triggers: Vec<ScannerTrigger>,
    universe: BTreeSet<String>,
}

struct Hit {
    kind: ScannerConditionKind,
    severity: ScannerSeverity,
    title: String,
    summary: String,
    evidence: Vec<ScannerEvidence>,
    tags: Vec<String>,
}

/// Numerical scanner detecting 5 anomaly classes and manual BarClose rule scans.
pub struct ScannerEngine {
    now_ms: NowFn,
    config: ScannerConfig,
    freeze_snapshot: FreezeSnapshotFn,
    rule_levels_at: Mutex<Option<RuleLevelsProvider>>,
    inner: Mutex<Inner>,
}

impl ScannerEngine {
    pub fn new(
        now_ms: NowFn,
        config: ScannerConfig,
        freeze_snapshot: FreezeSnapshotFn,
    ) -> Self {
        Self {
            now_ms,
            config,
            freeze_snapshot,
            rule_levels_at: Mutex::new(None),
            inner: Mutex::new(Inner {
                bars: HashMap::new(),
                stale: HashSet::new(),
                funding: HashMap::new(),
                open_interest: HashMap::new(),
                mark_price: HashMap::new(),
                rule_hits: HashMap::new(),
                last_trigger_at: HashMap::new(),
                last_trigger_bar: HashMap::new(),
                focus: HashMap::new(),
                triggers: Vec::new(),
                universe: BTreeSet::new(),
            }),
        }
    }

    pub fn set_rule_levels_provider(&self, provider: RuleLevelsProvider) {
        *self.rule_levels_at.lock().unwrap() = Some(provider);
    }

    pub fn timeframe(&self) -> &str {
        &self.config.timeframe
    }

    pub fn seed_bars(&self, instrument_id: &str, timeframe: &str, bars: &[CompactBar]) {
        let mut guard = self.inner.lock().unwrap();
        let map = guard
            .bars
            .entry((instrument_id.to_owned(), timeframe.to_owned()))
            .or_default();
        for bar in bars {
            map.insert(bar.timestamp, bar.clone());
        }
        trim_bar_map(map, self.config.max_bars.max(600));
    }

    pub fn seed_derivatives(&self, instrument_id: &str, points: &[DerivativePoint]) {
        let mut guard = self.inner.lock().unwrap();
        for pt in points {
            if pt.instrument_id == instrument_id {
                insert_deriv_point(&mut guard, pt.clone(), self.config.max_bars * 4);
            }
        }
    }

    pub fn set_universe(&self, instrument_ids: &[String]) {
        let mut guard = self.inner.lock().unwrap();
        guard.universe = instrument_ids.iter().cloned().collect();
    }

    pub fn handle_event(&self, event: &FeedEvent) {
        let mut guard = self.inner.lock().unwrap();
        match event {
            FeedEvent::Kline(k) => {
                if k.timeframe != self.config.timeframe || !k.confirmed {
                    return;
                }
                let map = guard
                    .bars
                    .entry((k.instrument_id.clone(), k.timeframe.clone()))
                    .or_default();
                map.insert(k.bar.timestamp, k.bar.clone());
                trim_bar_map(map, self.config.max_bars.max(600));
            }
            FeedEvent::Health(h) => {
                if h.status == FeedHealthStatus::Stale {
                    guard.stale.insert(h.instrument_id.clone());
                } else if h.status == FeedHealthStatus::Live {
                    guard.stale.remove(&h.instrument_id);
                }
            }
            FeedEvent::DerivativeTick(d) => {
                ingest_deriv_tick(&mut guard, d, self.config.max_bars * 4);
            }
            FeedEvent::OrderBook(_) | FeedEvent::Trade(_) => {}
        }
    }

    pub fn list_triggers(&self, instrument_id: Option<&str>, limit: usize) -> Vec<ScannerTrigger> {
        let guard = self.inner.lock().unwrap();
        guard
            .triggers
            .iter()
            .rev()
            .filter(|t| instrument_id.is_none_or(|id| t.instrument_id == id))
            .take(limit)
            .cloned()
            .collect()
    }

    pub fn active_focus(&self, now_override: Option<i64>) -> Vec<(String, i64)> {
        let now = now_override.unwrap_or_else(|| (self.now_ms)());
        let mut guard = self.inner.lock().unwrap();
        let ttl = self.config.focus_ttl_ms;
        guard.focus.retain(|_, &mut ts| now - ts < ttl);
        let mut out: Vec<(String, i64)> = guard
            .focus
            .iter()
            .map(|(k, &v)| (k.clone(), v))
            .collect();
        out.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
        out
    }

    pub fn scan(
        &self,
        instrument_ids: Option<&[String]>,
        now_override: Option<i64>,
        rule: Option<&Rule>,
        drawing_lookup: &dyn Fn(&str) -> Option<DrawingRecord>,
        include_gate_hits: bool,
    ) -> Result<(Vec<ScannerTrigger>, Vec<String>), ApiError> {
        let fingerprint = if let Some(r) = rule {
            validate_rule_complexity(r)?;
            if r.timing != RuleTiming::BarClose {
                return Err(ApiError::validation("手动扫描只支持收盘求值规则"));
            }
            let payload = serde_json::to_string(r).unwrap_or_default();
            hex::encode(Sha256::digest(payload.as_bytes()))
        } else {
            String::new()
        };

        let now = now_override.unwrap_or_else(|| (self.now_ms)());
        let rule_levels_at = self.rule_levels_at.lock().unwrap().clone();
        let tf = self.config.timeframe.clone();
        let tf_ms = timeframe_ms(&tf)?;

        let candidates: Vec<ScanCandidate> = {
            let mut guard = self.inner.lock().unwrap();
            let ttl = self.config.focus_ttl_ms;
            guard.focus.retain(|_, &mut ts| now - ts < ttl);
            if let Some(ids) = instrument_ids {
                guard.universe = ids.iter().cloned().collect();
            }
            let cooldown = self.config.cooldown_ms;
            let bar_timestamps: HashSet<(String, i64)> = guard
                .bars
                .iter()
                .filter(|((_, bar_tf), _)| bar_tf == &tf)
                .flat_map(|((inst, _), map)| map.keys().map(|&ts| (inst.clone(), ts)))
                .collect();
            guard.rule_hits.retain(|(inst, _), (bar_ts, hit_ts)| {
                (now - *hit_ts) < cooldown || bar_timestamps.contains(&(inst.clone(), *bar_ts))
            });

            let universe: Vec<String> = guard.universe.iter().cloned().collect();
            let mut out = Vec::with_capacity(universe.len());
            for inst in universe {
                if guard.stale.contains(&inst) {
                    continue;
                }
                let Some(map) = guard.bars.get(&(inst.clone(), tf.clone())) else {
                    continue;
                };
                let visible: Vec<CompactBar> = map
                    .values()
                    .filter(|b| b.ts_available <= now)
                    .cloned()
                    .collect();
                let Some(last) = visible.last() else {
                    continue;
                };
                if last.status == BarStatus::FeedGap {
                    continue;
                }
                let mut usable: Vec<CompactBar> = visible
                    .into_iter()
                    .filter(|b| b.status != BarStatus::FeedGap)
                    .collect();
                if usable.len() > self.config.max_bars {
                    usable.drain(0..usable.len() - self.config.max_bars);
                }
                let funding = guard.funding.get(&inst).cloned().unwrap_or_default();
                let oi = guard.open_interest.get(&inst).cloned().unwrap_or_default();
                let mark = guard.mark_price.get(&inst).cloned().unwrap_or_default();
                out.push(ScanCandidate {
                    instrument_id: inst,
                    bars: usable,
                    funding,
                    oi,
                    mark,
                });
            }
            out
        };

        let scanned: Vec<String> = candidates.iter().map(|c| c.instrument_id.clone()).collect();
        let mut triggers = Vec::new();
        let mut snapshot_cache: HashMap<(String, i64), String> = HashMap::new();

        for cand in candidates {
            let inst = cand.instrument_id;
            let bars = cand.bars;
            let funding = cand.funding;
            let oi = cand.oi;
            let mark = cand.mark;
            let Some(last) = bars.last() else {
                continue;
            };
            let bar_ts = last.timestamp;

            if let Some(r) = rule {
                let allowed = {
                    let guard = self.inner.lock().unwrap();
                    can_fire(
                        guard.rule_hits.get(&(inst.clone(), fingerprint.clone())).copied(),
                        bar_ts,
                        now,
                        self.config.cooldown_ms,
                    )
                };
                if allowed
                    && let Some(hit) = evaluate_rule_hit(
                        &inst,
                        &tf,
                        tf_ms,
                        &bars,
                        &funding,
                        &oi,
                        &mark,
                        r,
                        drawing_lookup,
                        rule_levels_at.as_ref(),
                    )
                {
                    let as_of = last.ts_available.max(bar_ts + tf_ms);
                    let snapshot_id = snapshot_cache
                        .entry((inst.clone(), as_of))
                        .or_insert_with(|| (self.freeze_snapshot)(&inst, &tf, as_of))
                        .clone();
                    if !snapshot_id.is_empty() {
                        let trg = make_trigger(&inst, &tf, bar_ts, &fingerprint, now, snapshot_id, hit);
                        let mut guard = self.inner.lock().unwrap();
                        guard.rule_hits.insert((inst.clone(), fingerprint.clone()), (bar_ts, now));
                        guard.focus.insert(inst.clone(), now);
                        guard.triggers.push(trg.clone());
                        triggers.push(trg);
                    } else {
                        continue;
                    }
                }
                if !include_gate_hits {
                    continue;
                }
            }

            for kind in KIND_ORDER {
                let allowed = {
                    let guard = self.inner.lock().unwrap();
                    let last_bar = guard.last_trigger_bar.get(&(inst.clone(), kind)).copied();
                    let last_at = guard.last_trigger_at.get(&(inst.clone(), kind)).copied();
                    can_fire(
                        last_bar.zip(last_at),
                        bar_ts,
                        now,
                        self.config.cooldown_ms,
                    )
                };
                if !allowed {
                    continue;
                }
                let hit = match kind {
                    ScannerConditionKind::LiquidityAnomaly => liquidity_hit(&self.config, &bars),
                    ScannerConditionKind::VolatilityExpansion => volatility_hit(&self.config, &bars),
                    ScannerConditionKind::FundingExtreme => {
                        funding_hit(&self.config, &funding, now)
                    }
                    ScannerConditionKind::OiDivergence => {
                        oi_hit(&self.config, &oi, &bars, now)
                    }
                    ScannerConditionKind::KeyLevelEvent => {
                        key_level_hit(&self.config, &inst, &tf, &bars, &oi, now)
                    }
                    ScannerConditionKind::Rule => None,
                };
                if let Some(hit) = hit {
                    let snapshot_id = snapshot_cache
                        .entry((inst.clone(), now))
                        .or_insert_with(|| (self.freeze_snapshot)(&inst, &tf, now))
                        .clone();
                    if !snapshot_id.is_empty() {
                        let trg = make_trigger(&inst, &tf, bar_ts, "", now, snapshot_id, hit);
                        let mut guard = self.inner.lock().unwrap();
                        guard.last_trigger_at.insert((inst.clone(), kind), now);
                        guard.last_trigger_bar.insert((inst.clone(), kind), bar_ts);
                        guard.focus.insert(inst.clone(), now);
                        guard.triggers.push(trg.clone());
                        triggers.push(trg);
                    }
                }
            }
        }

        // Bound retained in-memory trigger ring buffer
        {
            let mut guard = self.inner.lock().unwrap();
            if guard.triggers.len() > 1000 {
                let extra = guard.triggers.len() - 1000;
                guard.triggers.drain(0..extra);
            }
        }

        Ok((triggers, scanned))
    }
}

fn trim_bar_map(map: &mut BTreeMap<i64, CompactBar>, limit: usize) {
    while map.len() > limit {
        if let Some(&first_ts) = map.keys().next() {
            map.remove(&first_ts);
        }
    }
}

fn insert_deriv_point(inner: &mut Inner, pt: DerivativePoint, limit: usize) {
    let map = match pt.kind {
        DerivativeKind::MarkPrice => &mut inner.mark_price,
        DerivativeKind::FundingRate => &mut inner.funding,
        DerivativeKind::OpenInterest => &mut inner.open_interest,
    };
    let vec = map.entry(pt.instrument_id.clone()).or_default();
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
        if vec.len() > limit {
            let extra = vec.len() - limit;
            vec.drain(0..extra);
        }
    }
}

fn ingest_deriv_tick(inner: &mut Inner, tick: &DerivativeTickEvent, limit: usize) {
    let ts_available = tick.ts_event.max(tick.ts_ingested);
    if let Some(mark) = tick.mark_price {
        insert_deriv_point(
            inner,
            DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::MarkPrice,
                ts_event: tick.ts_event,
                ts_published: None,
                ts_available,
                value: mark,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_available,
                revision: 0,
                settled: None,
            },
            limit,
        );
    }
    if let Some(funding) = tick.funding_rate {
        insert_deriv_point(
            inner,
            DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::FundingRate,
                ts_event: tick.ts_event,
                ts_published: Some(tick.ts_event),
                ts_available,
                value: funding,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_available,
                revision: 0,
                settled: Some(true),
            },
            limit,
        );
    }
    if let Some(oi) = tick.open_interest {
        insert_deriv_point(
            inner,
            DerivativePoint {
                instrument_id: tick.instrument_id.clone(),
                kind: DerivativeKind::OpenInterest,
                ts_event: tick.ts_event,
                ts_published: None,
                ts_available,
                value: oi,
                timeframe: None,
                precision: talaria_contracts::PrecisionTier::SecondVerified,
                ts_ingested: tick.ts_ingested,
                ts_committed: ts_available,
                revision: 0,
                settled: None,
            },
            limit,
        );
    }
}

fn can_fire(last: Option<(i64, i64)>, bar_ts: i64, now: i64, cooldown_ms: i64) -> bool {
    match last {
        None => true,
        Some((last_bar_ts, last_at)) => bar_ts > last_bar_ts && (now - last_at) >= cooldown_ms,
    }
}

fn make_trigger(
    instrument_id: &str,
    timeframe: &str,
    bar_ts: i64,
    fingerprint: &str,
    now: i64,
    snapshot_id: String,
    hit: Hit,
) -> ScannerTrigger {
    let mut payload = format!(
        "{instrument_id}|{timeframe}|{}|{bar_ts}",
        serde_json::to_string(&hit.kind).unwrap_or_default().replace('"', "")
    );
    if !fingerprint.is_empty() {
        payload.push('|');
        payload.push_str(fingerprint);
    }
    let digest = hex::encode(Sha256::digest(payload.as_bytes()));
    let trigger_id = format!("scn_{}", &digest[..16]);
    ScannerTrigger {
        trigger_id,
        instrument_id: instrument_id.to_owned(),
        timeframe: timeframe.to_owned(),
        kind: hit.kind,
        severity: hit.severity,
        title: hit.title,
        summary: hit.summary,
        evidence: hit.evidence,
        tags: hit.tags,
        created_ts: now,
        snapshot_id,
        route: ScannerTriggerRoute::Local,
    }
}

#[allow(clippy::too_many_arguments)]
fn evaluate_rule_hit(
    instrument_id: &str,
    timeframe: &str,
    tf_ms: i64,
    bars: &[CompactBar],
    funding: &[DerivativePoint],
    open_interest: &[DerivativePoint],
    mark_price: &[DerivativePoint],
    rule: &Rule,
    drawing_lookup: &dyn Fn(&str) -> Option<DrawingRecord>,
    rule_levels_at: Option<&RuleLevelsProvider>,
) -> Option<Hit> {
    let current = bars.last()?;
    let needs_levels = rule_has_level_refs(&rule.expression);
    let cutoff = current.ts_available.max(current.timestamp + tf_ms);
    let levels = if needs_levels {
        if let Some(lp) = rule_levels_at {
            lp(instrument_id, timeframe, cutoff)
        } else {
            compute_key_levels(instrument_id, timeframe, bars, cutoff, None, None, open_interest)
                .unwrap_or_default()
        }
        .into_iter()
        .map(|l| (l.level_id.clone(), l))
        .collect()
    } else {
        HashMap::new()
    };

    let mut state = RuleState::default();
    let mut ctx = EvalContext {
        instrument_id,
        timeframe,
        now_ms: cutoff,
        bars,
        funding,
        open_interest,
        mark_price,
        levels: &levels,
        drawing_lookup,
        event: None,
        bar_ts_override: Some(current.timestamp),
        indicator_cache: BTreeMap::new(),
    };
    let obs = observe_rule(rule, &mut ctx, &mut state, true).ok().flatten()?;
    if !obs.matched {
        return None;
    }
    let mut evidence = Vec::new();
    if let Some(val) = obs.observed {
        evidence.push(ScannerEvidence {
            metric: "rule".to_owned(),
            value: val,
            threshold: obs.threshold,
            note: None,
        });
    }
    Some(Hit {
        kind: ScannerConditionKind::Rule,
        severity: ScannerSeverity::Info,
        title: "规则命中".to_owned(),
        summary: "收盘规则成立".to_owned(),
        evidence,
        tags: Vec::new(),
    })
}

fn rule_has_level_refs(expr: &RuleExpression) -> bool {
    match expr {
        RuleExpression::LevelEvent { .. } => true,
        RuleExpression::Compare { left, right, .. } => {
            matches!(left.as_ref(), RuleOperand::LevelPrice { .. })
                || matches!(right.as_ref(), RuleOperand::LevelPrice { .. })
        }
        RuleExpression::All { args }
        | RuleExpression::Any { args }
        | RuleExpression::Not { args } => args.iter().any(rule_has_level_refs),
        RuleExpression::Overlay { .. } => false,
    }
}

fn dec_f64(d: Option<Decimal>) -> f64 {
    d.and_then(|v| v.to_f64()).unwrap_or(0.0)
}

fn f64_to_dec(v: f64) -> Decimal {
    Decimal::from_f64_retain((v * 1_000_000.0).round() / 1_000_000.0).unwrap_or(Decimal::ZERO)
}

fn percentile_rank(values: &[f64], point: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut less = 0usize;
    let mut equal = 0usize;
    for &v in values {
        if v < point {
            less += 1;
        } else if (v - point).abs() <= f64::EPSILON {
            equal += 1;
        }
    }
    (less as f64 + 0.5 * equal as f64) / values.len() as f64
}

fn mean(values: &[f64]) -> f64 {
    if values.is_empty() {
        0.0
    } else {
        values.iter().sum::<f64>() / values.len() as f64
    }
}

fn pstdev(values: &[f64], mu: f64) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let var = values.iter().map(|v| (v - mu) * (v - mu)).sum::<f64>() / values.len() as f64;
    var.sqrt()
}

fn median(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 0.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(|a, b| a.total_cmp(b));
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        sorted[mid]
    } else {
        0.5 * (sorted[mid - 1] + sorted[mid])
    }
}

fn liquidity_hit(cfg: &ScannerConfig, bars: &[CompactBar]) -> Option<Hit> {
    let start = bars.len().saturating_sub(cfg.liquidity_window);
    let window = &bars[start..];
    if window.len() < cfg.liquidity_min_samples + 1 {
        return None;
    }
    let (current, baseline) = window.split_last()?;
    let use_turnover = baseline.iter().any(|b| b.turnover.is_some_and(|t| t > Decimal::ZERO));
    let metric_name = if use_turnover { "成交额" } else { "成交量" };
    let val_of = |b: &CompactBar| dec_f64(if use_turnover { b.turnover } else { b.volume });

    let values: Vec<f64> = baseline.iter().map(val_of).collect();
    let cur_val = val_of(current);
    if cur_val <= 0.0 || values.len() < cfg.liquidity_min_samples {
        return None;
    }
    let mu = mean(&values);
    let dev = pstdev(&values, mu);
    let zscore = if dev > 0.0 { (cur_val - mu) / dev } else { 0.0 };
    let quantile = percentile_rank(&values, cur_val);
    let med = median(&values);
    let ratio = if med > 0.0 { cur_val / med } else { 0.0 };
    if cur_val <= mu || (zscore < cfg.liquidity_zscore && quantile < cfg.liquidity_quantile) {
        return None;
    }
    let severity = if zscore >= 4.0 || quantile >= 0.995 {
        ScannerSeverity::Critical
    } else {
        ScannerSeverity::Warning
    };
    Some(Hit {
        kind: ScannerConditionKind::LiquidityAnomaly,
        severity,
        title: "流动性异常".to_owned(),
        summary: format!(
            "{metric_name}较近 {} 根均值放大 {:.1}σ（分位 {:.0}%，倍数 {:.2}×）",
            values.len(),
            zscore,
            quantile * 100.0,
            ratio
        ),
        evidence: vec![
            ScannerEvidence {
                metric: "zscore".to_owned(),
                value: f64_to_dec(zscore),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "quantile".to_owned(),
                value: f64_to_dec(quantile),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "ratio".to_owned(),
                value: f64_to_dec(ratio),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "value".to_owned(),
                value: f64_to_dec(cur_val),
                threshold: None,
                note: None,
            },
        ],
        tags: vec![
            "流动性".to_owned(),
            cfg.timeframe.clone(),
            format!("z={zscore:.1}"),
        ],
    })
}

fn volatility_hit(cfg: &ScannerConfig, bars: &[CompactBar]) -> Option<Hit> {
    let start = bars
        .len()
        .saturating_sub(cfg.volatility_window + cfg.atr_period);
    let window = &bars[start..];
    if window.len() < 2 {
        return None;
    }
    let ranges: Vec<f64> = window
        .windows(2)
        .map(|w| {
            let h = dec_f64(w[1].high);
            let l = dec_f64(w[1].low);
            let pc = dec_f64(w[0].close);
            (h - l).max((h - pc).abs()).max((l - pc).abs())
        })
        .collect();
    if ranges.len() < cfg.volatility_min_samples + 1 {
        return None;
    }
    let (&cur_tr, rest) = ranges.split_last()?;
    let b_start = rest.len().saturating_sub(cfg.volatility_window);
    let baseline = &rest[b_start..];
    if baseline.len() < cfg.volatility_min_samples {
        return None;
    }
    let atr_start = baseline.len().saturating_sub(cfg.atr_period);
    let atr = mean(&baseline[atr_start..]);
    let quantile = percentile_rank(baseline, cur_tr);
    let ratio = if atr > 0.0 { cur_tr / atr } else { 0.0 };
    if quantile < cfg.volatility_quantile || ratio < cfg.volatility_ratio {
        return None;
    }
    let severity = if ratio >= 2.5 || quantile >= 0.99 {
        ScannerSeverity::Critical
    } else {
        ScannerSeverity::Warning
    };
    Some(Hit {
        kind: ScannerConditionKind::VolatilityExpansion,
        severity,
        title: "波动率扩张".to_owned(),
        summary: format!(
            "真实波幅 {:.2} 为近 {} 根 ATR 的 {:.2} 倍（分位 {:.0}%）",
            cur_tr,
            cfg.atr_period,
            ratio,
            quantile * 100.0
        ),
        evidence: vec![
            ScannerEvidence {
                metric: "true_range".to_owned(),
                value: f64_to_dec(cur_tr),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "atr".to_owned(),
                value: f64_to_dec(atr),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "ratio".to_owned(),
                value: f64_to_dec(ratio),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "quantile".to_owned(),
                value: f64_to_dec(quantile),
                threshold: None,
                note: None,
            },
        ],
        tags: vec![
            "波动率".to_owned(),
            cfg.timeframe.clone(),
            format!("×{ratio:.1}"),
        ],
    })
}

fn funding_hit(cfg: &ScannerConfig, funding: &[DerivativePoint], now: i64) -> Option<Hit> {
    let settlements: Vec<DerivativePoint> = funding
        .iter()
        .filter(|p| p.is_funding_settlement())
        .cloned()
        .collect();
    let visible = latest_visible_derivatives(settlements, Some(now));
    if visible.len() < cfg.funding_min_samples + 1 {
        return None;
    }
    let latest = visible.last()?;
    if now - latest.ts_event > cfg.funding_max_age_ms {
        return None;
    }
    let start = visible.len().saturating_sub(cfg.funding_lookback + 1);
    let history: Vec<f64> = visible[start..visible.len() - 1]
        .iter()
        .map(|p| dec_f64(Some(p.value)))
        .collect();
    if history.len() < cfg.funding_min_samples {
        return None;
    }
    let val = dec_f64(Some(latest.value));
    let quantile = percentile_rank(&history, val);
    let high = quantile >= cfg.funding_high_quantile;
    let low = quantile <= cfg.funding_low_quantile;
    if !high && !low {
        return None;
    }
    let severity = if quantile >= 0.99 || quantile <= 0.01 {
        ScannerSeverity::Critical
    } else {
        ScannerSeverity::Warning
    };
    let side = if high { "高位" } else { "低位" };
    Some(Hit {
        kind: ScannerConditionKind::FundingExtreme,
        severity,
        title: "资金费率极值".to_owned(),
        summary: format!(
            "资金费率 {:+.4}% 处于近 {} 次结算的 {:.0}% 分位（{side}极值）",
            val * 100.0,
            history.len(),
            quantile * 100.0
        ),
        evidence: vec![
            ScannerEvidence {
                metric: "funding_rate".to_owned(),
                value: latest.value,
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "quantile".to_owned(),
                value: f64_to_dec(quantile),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "settlements".to_owned(),
                value: Decimal::from(history.len()),
                threshold: None,
                note: None,
            },
        ],
        tags: vec![
            "资金费率".to_owned(),
            format!("{side}极值"),
            cfg.timeframe.clone(),
        ],
    })
}

fn oi_hit(
    cfg: &ScannerConfig,
    oi_points: &[DerivativePoint],
    bars: &[CompactBar],
    now: i64,
) -> Option<Hit> {
    let oi_tf = oi_timeframe_for(&cfg.timeframe).unwrap_or("5m");
    let interval = timeframe_ms(oi_tf).ok()?;
    let filtered: Vec<DerivativePoint> = oi_points
        .iter()
        .filter(|p| p.timeframe.is_none() || p.timeframe.as_deref() == Some(oi_tf))
        .cloned()
        .collect();
    let visible = latest_visible_derivatives(filtered, Some(now));
    let mut buckets: BTreeMap<i64, Decimal> = BTreeMap::new();
    for p in &visible {
        if p.timeframe.is_none() {
            let bucket = (p.ts_event / interval) * interval;
            buckets.insert(bucket, p.value);
        }
    }
    for p in &visible {
        if p.timeframe.as_deref() == Some(oi_tf) {
            buckets.insert(p.ts_event, p.value);
        }
    }
    let points: Vec<(i64, Decimal)> = buckets.into_iter().collect();
    if points.len() < cfg.oi_min_samples {
        return None;
    }
    let start = points.len().saturating_sub(cfg.oi_lookback + 1);
    let window = &points[start..];
    let (first_ts, first_oi) = window.first().copied()?;
    let (last_ts, last_oi) = window.last().copied()?;
    if first_oi.is_zero() {
        return None;
    }
    let close_at = |ts: i64| -> Option<f64> {
        let pos = bars.partition_point(|b| b.timestamp <= ts);
        if pos == 0 {
            None
        } else {
            bars[pos - 1].close.and_then(|d| d.to_f64())
        }
    };
    let start_price = close_at(first_ts)?;
    let end_price = close_at(last_ts)?;
    if start_price == 0.0 || (end_price - start_price).abs() <= f64::EPSILON {
        return None;
    }
    let price_change = (end_price - start_price) / start_price;
    let oi_change = ((last_oi - first_oi) / first_oi).to_f64()?;
    if price_change.abs() < cfg.oi_price_move_min
        || oi_change.abs() < cfg.oi_change_min
        || price_change * oi_change >= 0.0
    {
        return None;
    }
    let severity = if price_change.abs() >= cfg.oi_price_move_min * 2.0
        && oi_change.abs() >= cfg.oi_change_min * 2.0
    {
        ScannerSeverity::Critical
    } else {
        ScannerSeverity::Warning
    };
    let dir = if price_change < 0.0 {
        "价跌仓增"
    } else {
        "价涨仓减"
    };
    let minutes = ((last_ts - first_ts) / 60_000).max(0);
    Some(Hit {
        kind: ScannerConditionKind::OiDivergence,
        severity,
        title: "持仓量背离".to_owned(),
        summary: format!(
            "近 {minutes} 分钟价格 {:+.2}%，持仓量 {:+.2}%（{dir}）",
            price_change * 100.0,
            oi_change * 100.0
        ),
        evidence: vec![
            ScannerEvidence {
                metric: "price_change".to_owned(),
                value: f64_to_dec(price_change),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "oi_change".to_owned(),
                value: f64_to_dec(oi_change),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "oi_points".to_owned(),
                value: Decimal::from(window.len()),
                threshold: None,
                note: Some(oi_tf.to_owned()),
            },
        ],
        tags: vec!["OI 背离".to_owned(), dir.to_owned(), oi_tf.to_owned()],
    })
}

fn key_level_hit(
    cfg: &ScannerConfig,
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    oi_points: &[DerivativePoint],
    now: i64,
) -> Option<Hit> {
    if bars.len() < 3 {
        return None;
    }
    let previous = &bars[bars.len() - 2];
    let current = &bars[bars.len() - 1];
    let oi_tf = oi_timeframe_for(&cfg.timeframe);
    let filtered_oi: Vec<DerivativePoint> = oi_points
        .iter()
        .filter(|p| p.timeframe.is_none() || p.timeframe.as_deref() == oi_tf)
        .cloned()
        .collect();
    let visible_oi = latest_visible_derivatives(filtered_oi, Some(now));
    let levels = compute_key_levels(
        instrument_id,
        timeframe,
        bars,
        now,
        None,
        None,
        &visible_oi,
    )
    .ok()?;

    let prev_close = dec_f64(previous.close);
    let cur_close = dec_f64(current.close);
    let cur_high = dec_f64(current.high);
    let cur_low = dec_f64(current.low);

    let mut best: Option<(KeyLevel, bool, f64)> = None;
    for lv in levels {
        if lv.strength < cfg.key_level_min_strength
            || lv.formed_ts >= current.timestamp
            || lv.ts_available > now
        {
            continue;
        }
        let (low, high) = if let Some(p) = lv.price {
            let v = dec_f64(Some(p));
            (v, v)
        } else if let (Some(l), Some(h)) = (lv.price_low, lv.price_high) {
            (dec_f64(Some(l)), dec_f64(Some(h)))
        } else {
            continue;
        };
        if low <= 0.0 || high <= 0.0 {
            continue;
        }
        let crossed_up = prev_close <= high && cur_close > high;
        let crossed_down = prev_close >= low && cur_close < low;
        let breakout = crossed_up || crossed_down;
        let tol = cfg.key_level_touch_tolerance;
        let touched = cur_high >= low * (1.0 - tol) && cur_low <= high * (1.0 + tol);
        if !breakout && !touched {
            continue;
        }
        let mid = 0.5 * (low + high);
        let dist = ((cur_close - mid).abs()) / mid;
        let replace = match &best {
            None => true,
            Some((b_lv, b_brk, b_dist)) => {
                (breakout, lv.strength, -dist) > (*b_brk, b_lv.strength, -*b_dist)
            }
        };
        if replace {
            best = Some((lv, breakout, dist));
        }
    }

    let (lv, breakout, dist) = best?;
    let action = if breakout { "突破" } else { "触及" };
    let price_dec = lv.price.or(lv.price_high).unwrap_or(Decimal::ZERO);
    let severity = if breakout && lv.strength >= 0.85 {
        ScannerSeverity::Critical
    } else {
        ScannerSeverity::Warning
    };
    Some(Hit {
        kind: ScannerConditionKind::KeyLevelEvent,
        severity,
        title: "关键位异动".to_owned(),
        summary: format!(
            "价格{action} {:?} ({price_dec})，强度 {:.2}",
            lv.kind, lv.strength
        ),
        evidence: vec![
            ScannerEvidence {
                metric: "level_price".to_owned(),
                value: price_dec,
                threshold: None,
                note: Some(lv.level_id),
            },
            ScannerEvidence {
                metric: "strength".to_owned(),
                value: f64_to_dec(lv.strength),
                threshold: None,
                note: None,
            },
            ScannerEvidence {
                metric: "distance_pct".to_owned(),
                value: f64_to_dec(dist),
                threshold: None,
                note: None,
            },
        ],
        tags: vec!["关键位".to_owned(), action.to_owned(), cfg.timeframe.clone()],
    })
}
