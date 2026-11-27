//! Deterministic Rule Evaluator, Drawing Geometry Bounds, Rule Preview, and Agent Annotation Hard Gate.

use std::collections::{BTreeMap, HashMap, HashSet};

use rust_decimal::Decimal;
use talaria_contracts::bars::{BarStatus, CompactBar};
use talaria_contracts::collaboration::AgentAnnotationCreate;
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint, oi_timeframe_for};
use talaria_contracts::drawings::{DrawingKind, DrawingPoint, DrawingRecord};
use talaria_contracts::error_codes::SNAPSHOT_NOT_FOUND;
use talaria_contracts::feed::{DerivativeTickEvent, KlineEvent, OrderBookEvent};
use talaria_contracts::indicators::{
    IndicatorName, IndicatorParams, IndicatorPoint, VwapAnchorMode,
};
use talaria_contracts::key_levels::{KeyLevel, KeyLevelStatus};
use talaria_contracts::market::InstrumentSpec;
use talaria_contracts::rules::{
    AlertDirection, AlertLevelEvent, AlertMetric, AlertOverlayMode, MAX_RULE_DEPTH,
    MAX_RULE_NODES, Rule, RuleBarField, RuleComparisonTest, RuleEdge, RuleExpression,
    RuleIndicatorName, RuleIndicatorParams, RuleIndicatorWindow, RuleOperand, RulePane,
    RulePreviewIndicator, RulePreviewResponse, RulePreviewSignal, RuleTiming,
};
use talaria_contracts::snapshot::SnapshotMeta;
use talaria_contracts::structures::{StructureFamily, StructureFeature, StructureStatus};
use talaria_contracts::timeframe_ms;
use talaria_operators::{
    DerivativeSample, GdaDecimal, GeometryBounds, OperatorSpec, compute_indicator_series,
    compute_series, derivative_samples, geometry_bounds_at, legacy_alert_points,
};

use crate::error::ApiError;

pub const DEFAULT_RULE_HISTORY: usize = 600;
const ATR_PERIOD: u32 = 14;
const FUTURE_BARS_LIMIT: i64 = 100;
const KLINE_EVIDENCE_TOLERANCE_BARS: i64 = 1;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct OverlayBounds {
    pub lower: Decimal,
    pub upper: Decimal,
}

fn interpolate_gda(first: &DrawingPoint, second: &DrawingPoint, ts: i64) -> Option<GdaDecimal> {
    let first_price = GdaDecimal::from_decimal(first.price);
    if second.ts == first.ts {
        return Some(first_price);
    }
    let second_price = GdaDecimal::from_decimal(second.price);
    let span = GdaDecimal::from_i64(second.ts - first.ts);
    let progress = GdaDecimal::from_i64(ts - first.ts);
    let delta = second_price.sub(&first_price).mul(&progress).div(&span).ok()?;
    Some(first_price.add(&delta))
}

fn time_span(points: &[DrawingPoint]) -> (i64, i64) {
    let mut low = i64::MAX;
    let mut high = i64::MIN;
    for p in points {
        low = low.min(p.ts);
        high = high.max(p.ts);
    }
    (low, high)
}

/// Deterministic geometric bounds for a `StructureFeature` at timestamp `ts`.
pub fn structure_bounds_at(feature: &StructureFeature, ts: i64) -> Option<OverlayBounds> {
    let bounds: GeometryBounds = geometry_bounds_at(&feature.geometry, ts).ok().flatten()?;
    Some(OverlayBounds {
        lower: bounds.lower,
        upper: bounds.upper,
    })
}

/// Deterministic geometric bounds for a human drawing at timestamp `ts`.
///
/// Uses IBM GDA (`prec = 28`, `ROUND_HALF_EVEN`) via [`GdaDecimal`] so slope
/// interpolation and ratio arithmetic match Python `Decimal` character-for-character.
pub fn overlay_bounds(drawing: &DrawingRecord, ts: i64, ratio: Decimal) -> Option<OverlayBounds> {
    if drawing.points.is_empty() || drawing.deleted_at.is_some() {
        return None;
    }
    let points = &drawing.points;
    match drawing.kind {
        DrawingKind::HorizontalLine => Some(OverlayBounds {
            lower: points[0].price,
            upper: points[0].price,
        }),
        DrawingKind::TextLabel => None,
        DrawingKind::Ray => {
            if points.len() < 2 {
                return None;
            }
            let (first, second) = (&points[0], &points[1]);
            if (second.ts > first.ts && ts < first.ts) || (second.ts < first.ts && ts > first.ts) {
                return None;
            }
            let price = interpolate_gda(first, second, ts)?.to_decimal().ok()?;
            Some(OverlayBounds {
                lower: price,
                upper: price,
            })
        }
        DrawingKind::Segment => {
            if points.len() < 2 {
                return None;
            }
            let (low, high) = time_span(&points[..2]);
            if ts < low || ts > high {
                return None;
            }
            let price = interpolate_gda(&points[0], &points[1], ts)?.to_decimal().ok()?;
            Some(OverlayBounds {
                lower: price,
                upper: price,
            })
        }
        DrawingKind::FibRetracement => {
            if points.len() < 2 {
                return None;
            }
            let (low, high) = time_span(&points[..2]);
            if ts < low || ts > high {
                return None;
            }
            let p0 = GdaDecimal::from_decimal(points[0].price);
            let p1 = GdaDecimal::from_decimal(points[1].price);
            let r = GdaDecimal::from_decimal(ratio);
            let price = p0.add(&p1.sub(&p0).mul(&r)).to_decimal().ok()?;
            Some(OverlayBounds {
                lower: price,
                upper: price,
            })
        }
        DrawingKind::Rectangle => {
            if points.len() < 2 {
                return None;
            }
            let (low, high) = time_span(&points[..2]);
            if ts < low || ts > high {
                return None;
            }
            let lower = points[0].price.min(points[1].price);
            let upper = points[0].price.max(points[1].price);
            Some(OverlayBounds { lower, upper })
        }
        DrawingKind::ParallelChannel => {
            if points.len() < 3 {
                return None;
            }
            let (first, second, third) = (&points[0], &points[1], &points[2]);
            let (low, high) = time_span(&points[..2]);
            if ts < low || ts > high {
                return None;
            }
            let base = interpolate_gda(first, second, ts)?;
            let anchor = interpolate_gda(first, second, third.ts)?;
            let offset = GdaDecimal::from_decimal(third.price).sub(&anchor);
            let zero = GdaDecimal::zero();
            let lower_off = if offset.cmp_numeric(&zero).is_le() {
                &offset
            } else {
                &zero
            };
            let upper_off = if offset.cmp_numeric(&zero).is_ge() {
                &offset
            } else {
                &zero
            };
            Some(OverlayBounds {
                lower: base.add(lower_off).to_decimal().ok()?,
                upper: base.add(upper_off).to_decimal().ok()?,
            })
        }
    }
}

pub fn level_bounds(level: &KeyLevel) -> Option<(Decimal, Decimal)> {
    if let Some(price) = level.price {
        Some((price, price))
    } else if let (Some(low), Some(high)) = (level.price_low, level.price_high) {
        Some((low, high))
    } else {
        None
    }
}

pub fn validate_rule_complexity(rule: &Rule) -> Result<(), ApiError> {
    fn walk(node: &RuleExpression, depth: usize, count: &mut usize) -> Result<(), ApiError> {
        *count += 1;
        if *count > MAX_RULE_NODES {
            return Err(ApiError::validation(format!(
                "rule exceeds max node limit ({MAX_RULE_NODES})"
            )));
        }
        if depth > MAX_RULE_DEPTH {
            return Err(ApiError::validation(format!(
                "rule exceeds max depth ({MAX_RULE_DEPTH})"
            )));
        }
        match node {
            RuleExpression::All { args } | RuleExpression::Any { args } => {
                if args.is_empty() {
                    return Err(ApiError::validation("all/any expression requires >= 1 child"));
                }
                for child in args {
                    walk(child, depth + 1, count)?;
                }
            }
            RuleExpression::Not { args } => {
                if args.len() != 1 {
                    return Err(ApiError::validation("not expression requires exactly 1 child"));
                }
                walk(&args[0], depth + 1, count)?;
            }
            RuleExpression::Compare { .. }
            | RuleExpression::Overlay { .. }
            | RuleExpression::LevelEvent { .. }
            | RuleExpression::StructureEvent { .. } => {}
        }
        Ok(())
    }
    let mut nodes = 0;
    walk(&rule.expression, 1, &mut nodes)
}

#[derive(Debug, Clone)]
pub enum FeedEventRef<'a> {
    Kline(&'a KlineEvent),
    Book(&'a OrderBookEvent),
    Derivative(&'a DerivativeTickEvent),
}

impl<'a> FeedEventRef<'a> {
    pub fn instrument_id(&self) -> &str {
        match self {
            Self::Kline(e) => &e.instrument_id,
            Self::Book(e) => &e.instrument_id,
            Self::Derivative(e) => &e.instrument_id,
        }
    }

    pub fn visible_at(&self, tf_ms: i64) -> i64 {
        match self {
            Self::Kline(e) => {
                if e.confirmed {
                    e.bar.ts_available.max(e.bar.timestamp + tf_ms)
                } else {
                    e.bar.timestamp
                }
            }
            Self::Book(e) => e.ts_event.max(e.ts_ingested),
            Self::Derivative(e) => e.ts_event.max(e.ts_ingested),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum LevelSide {
    Up,
    Down,
}

#[derive(Debug, Clone, Default)]
pub struct RuleState {
    pub known_inputs: HashSet<String>,
    pub breakouts: HashMap<String, (LevelSide, i64)>,
    pub consumed_sides: HashMap<String, LevelSide>,
    pub matched_retests: Vec<String>,
}

impl RuleState {
    pub fn consume_matches(&mut self) {
        for key in self.matched_retests.drain(..) {
            if let Some((side, _)) = self.breakouts.remove(&key) {
                self.consumed_sides.insert(key, side);
            }
        }
    }
}

#[derive(Debug, Clone)]
pub struct RuleObservation {
    pub matched: bool,
    pub observed: Option<Decimal>,
    pub threshold: Option<Decimal>,
    pub drawing_kind: Option<DrawingKind>,
    pub level: Option<KeyLevel>,
    pub structure: Option<StructureFeature>,
    pub level_event: Option<AlertLevelEvent>,
    pub level_direction: Option<LevelSide>,
    pub crossed_at: Option<i64>,
}

pub struct EvalContext<'a> {
    pub instrument_id: &'a str,
    pub timeframe: &'a str,
    pub now_ms: i64,
    pub bars: &'a [CompactBar],
    pub funding: &'a [DerivativePoint],
    pub open_interest: &'a [DerivativePoint],
    pub mark_price: &'a [DerivativePoint],
    pub levels: &'a HashMap<String, KeyLevel>,
    pub structures: &'a HashMap<String, StructureFeature>,
    pub drawing_lookup: &'a dyn Fn(&str) -> Option<DrawingRecord>,
    pub event: Option<FeedEventRef<'a>>,
    pub bar_ts_override: Option<i64>,
    pub indicator_cache: BTreeMap<String, Vec<IndicatorPoint>>,
}

pub fn rule_indicator_to_spec(
    name: RuleIndicatorName,
    params: &RuleIndicatorParams,
) -> Result<(IndicatorName, IndicatorParams, bool), ApiError> {
    let (canonical, is_legacy) = match name {
        RuleIndicatorName::Ema => (IndicatorName::Ema, false),
        RuleIndicatorName::Sma => (IndicatorName::Sma, false),
        RuleIndicatorName::Rsi => (IndicatorName::Rsi, false),
        RuleIndicatorName::Atr => (IndicatorName::Atr, false),
        RuleIndicatorName::Boll => (IndicatorName::Boll, false),
        RuleIndicatorName::Vwap => (IndicatorName::Vwap, false),
        RuleIndicatorName::FundingQuantile => (IndicatorName::FundingQuantile, false),
        RuleIndicatorName::OiDivergence => (IndicatorName::OiDivergence, false),
        RuleIndicatorName::NetOdds => (IndicatorName::NetOdds, false),
        RuleIndicatorName::LegacyEma => (IndicatorName::Ema, true),
        RuleIndicatorName::LegacyAtr => (IndicatorName::Atr, true),
    };
    let ind_params = IndicatorParams {
        period: params.period.unwrap_or_else(|| canonical.default_period()),
        std_dev: params.std_dev,
        anchor: params.anchor.or_else(|| {
            if canonical == IndicatorName::Vwap {
                Some(VwapAnchorMode::Session)
            } else {
                None
            }
        }),
    };
    Ok((canonical, ind_params, is_legacy))
}

fn operand_cache_key(
    name: RuleIndicatorName,
    window: RuleIndicatorWindow,
    snapshot_id: &Option<String>,
    params: &RuleIndicatorParams,
) -> String {
    format!(
        "{name:?}:{window:?}:{}:{}:{}:{}",
        snapshot_id.as_deref().unwrap_or(""),
        params.period.unwrap_or(0),
        params.std_dev.map(|d| d.to_string()).unwrap_or_default(),
        params.anchor.map(|a| format!("{a:?}")).unwrap_or_default()
    )
}

pub fn compute_rule_indicator_points(
    bars: &[CompactBar],
    timeframe: &str,
    name: RuleIndicatorName,
    params: &RuleIndicatorParams,
    funding: &[DerivativePoint],
    open_interest: &[DerivativePoint],
    mark_price: &[DerivativePoint],
) -> Result<Vec<IndicatorPoint>, ApiError> {
    let (canonical, ind_params, is_legacy) = rule_indicator_to_spec(name, params)?;
    let spec = OperatorSpec::from_params(canonical, Some(&ind_params))?;
    if is_legacy {
        let pts = legacy_alert_points(bars, &spec)?;
        return Ok(bars
            .iter()
            .zip(pts)
            .map(|(b, values)| IndicatorPoint {
                timestamp: b.timestamp,
                values,
            })
            .collect());
    }
    let samples: Option<Vec<DerivativeSample>> = match canonical {
        IndicatorName::FundingQuantile | IndicatorName::OiDivergence | IndicatorName::NetOdds => {
            let tf_ms = timeframe_ms(timeframe)?;
            let as_of: Vec<i64> = bars
                .iter()
                .map(|b| b.ts_available.max(b.timestamp + tf_ms))
                .collect();
            Some(derivative_samples(
                bars,
                funding,
                open_interest,
                mark_price,
                Some(&as_of),
            )?)
        }
        _ => None,
    };
    let state = compute_series(bars, &spec, samples.as_deref())?;
    Ok(bars
        .iter()
        .zip(state.points)
        .map(|(b, values)| IndicatorPoint {
            timestamp: b.timestamp,
            values,
        })
        .collect())
}

pub fn observe_rule(
    rule: &Rule,
    ctx: &mut EvalContext<'_>,
    state: &mut RuleState,
    consume_retests: bool,
) -> Result<Option<RuleObservation>, ApiError> {
    let tf_ms = timeframe_ms(ctx.timeframe)?;
    state.known_inputs.clear();

    if let Some(ev) = &ctx.event {
        if ev.instrument_id() != ctx.instrument_id || ev.visible_at(tf_ms) > ctx.now_ms {
            return Ok(None);
        }
        if let FeedEventRef::Kline(k) = ev
            && k.timeframe != ctx.timeframe
        {
            return Ok(None);
        }
    }

    let mut effective_bar_ts = ctx.bar_ts_override;
    if let Some(FeedEventRef::Kline(k)) = ctx.event {
        let slot = if k.confirmed {
            k.bar.timestamp
        } else {
            k.bar.timestamp - tf_ms
        };
        effective_bar_ts = Some(match effective_bar_ts {
            Some(prev) => prev.min(slot),
            None => slot,
        });
    }

    let bar_index: Option<usize> = match rule.timing {
        RuleTiming::Event => {
            if ctx.event.is_none() {
                return Ok(None);
            }
            if let Some(FeedEventRef::Kline(_)) = ctx.event {
                ctx.bars.len().checked_sub(1)
            } else {
                None
            }
        }
        RuleTiming::BarClose => {
            if let Some(ev) = &ctx.event {
                match ev {
                    FeedEventRef::Kline(k) if k.confirmed => {}
                    _ => return Ok(None),
                }
            }
            let Some(last) = ctx.bars.last() else {
                return Ok(None);
            };
            if last.status == BarStatus::FeedGap {
                return Ok(None);
            }
            if let Some(expected_ts) = effective_bar_ts
                && last.timestamp != expected_ts
            {
                return Ok(None);
            }
            Some(ctx.bars.len() - 1)
        }
    };

    let event_kline_bar = match ctx.event {
        Some(FeedEventRef::Kline(k)) => Some(k.bar.clone()),
        _ => None,
    };
    let current_bar_slice = match rule.timing {
        RuleTiming::Event => {
            if let Some(FeedEventRef::Kline(k)) = ctx.event
                && !k.confirmed
            {
                ctx.bars
            } else {
                ctx.bars
            }
        }
        RuleTiming::BarClose => ctx.bars,
    };

    let mut evaluator = RuleEvaluator {
        ctx,
        bar_idx: bar_index,
        event_kline_bar,
        tf_ms,
        state,
        all_bars: current_bar_slice,
    };
    evaluator.state.matched_retests.clear();
    let obs = evaluator.evaluate(&rule.expression, "root")?;
    if consume_retests && obs.as_ref().is_some_and(|o| o.matched) {
        evaluator.state.consume_matches();
    }
    Ok(obs)
}

pub fn prior_root_false(rule: &Rule, ctx: &mut EvalContext<'_>) -> Result<bool, ApiError> {
    if ctx.bars.len() < 2 {
        return Ok(false);
    }
    let tf_ms = timeframe_ms(ctx.timeframe)?;
    let prior = &ctx.bars[ctx.bars.len() - 2];
    let mut prior_ctx = EvalContext {
        instrument_id: ctx.instrument_id,
        timeframe: ctx.timeframe,
        now_ms: prior.ts_available.max(prior.timestamp + tf_ms),
        bars: &ctx.bars[..ctx.bars.len() - 1],
        funding: ctx.funding,
        open_interest: ctx.open_interest,
        mark_price: ctx.mark_price,
        levels: ctx.levels,
        structures: ctx.structures,
        drawing_lookup: ctx.drawing_lookup,
        event: None,
        bar_ts_override: Some(prior.timestamp),
        indicator_cache: BTreeMap::new(),
    };
    let mut tmp_state = RuleState::default();
    let res = observe_rule(rule, &mut prior_ctx, &mut tmp_state, true)?;
    Ok(res.is_some_and(|o| !o.matched))
}

struct RuleEvaluator<'a, 'b> {
    ctx: &'a mut EvalContext<'b>,
    bar_idx: Option<usize>,
    event_kline_bar: Option<CompactBar>,
    tf_ms: i64,
    state: &'a mut RuleState,
    all_bars: &'b [CompactBar],
}

impl<'a, 'b> RuleEvaluator<'a, 'b> {
    fn current_bar(&self) -> Option<&CompactBar> {
        if let Some(kbar) = &self.event_kline_bar {
            return Some(kbar);
        }
        self.bar_idx.and_then(|idx| self.all_bars.get(idx))
    }

    fn previous_bar(&self) -> Option<&CompactBar> {
        let cur = self.current_bar()?;
        if let Some(idx) = self.bar_idx
            && idx > 0
        {
            let prev = &self.all_bars[idx - 1];
            if prev.timestamp + self.tf_ms == cur.timestamp {
                return Some(prev);
            }
        }
        None
    }

    fn evaluate(
        &mut self,
        node: &RuleExpression,
        path: &str,
    ) -> Result<Option<RuleObservation>, ApiError> {
        match node {
            RuleExpression::All { args } | RuleExpression::Any { args } => {
                let is_all = matches!(node, RuleExpression::All { .. });
                let mut results = Vec::with_capacity(args.len());
                for (i, child) in args.iter().enumerate() {
                    results.push(self.evaluate(child, &format!("{path}.{i}"))?);
                }
                let mut known_count = 0usize;
                let mut matched = Vec::new();
                let mut failed: Option<RuleObservation> = None;
                for res in results.into_iter().flatten() {
                    known_count += 1;
                    if res.matched {
                        matched.push(res);
                    } else if failed.is_none() {
                        failed = Some(res);
                    }
                }
                if is_all && let Some(f) = failed {
                    return Ok(Some(f));
                }
                if !matched.is_empty() && (!is_all || matched.len() == args.len()) {
                    if let Some(crossed) = matched.iter().find(|r| r.crossed_at.is_some()) {
                        return Ok(Some(crossed.clone()));
                    }
                    return Ok(matched.into_iter().next());
                }
                if known_count == args.len() {
                    Ok(failed)
                } else {
                    Ok(None)
                }
            }
            RuleExpression::Not { args } => {
                let child = self.evaluate(&args[0], &format!("{path}.0"))?;
                Ok(child.map(|r| RuleObservation {
                    matched: !r.matched,
                    observed: r.observed,
                    threshold: r.threshold,
                    drawing_kind: None,
                    level: None,
                    structure: None,
                    level_event: None,
                    level_direction: None,
                    crossed_at: None,
                }))
            }
            RuleExpression::Compare { test, left, right } => {
                let res = self.eval_compare(*test, left, right)?;
                if res.is_some() {
                    self.state.known_inputs.insert(path.to_owned());
                }
                Ok(res)
            }
            RuleExpression::Overlay {
                drawing_id,
                mode,
                direction,
                ratio,
            } => {
                let Some(bar) = self.current_bar().cloned() else {
                    return Ok(None);
                };
                let res = self.eval_overlay(drawing_id, *mode, *direction, *ratio, &bar);
                if res.is_some() {
                    self.state.known_inputs.insert(path.to_owned());
                }
                Ok(res)
            }
            RuleExpression::LevelEvent {
                level_id,
                event,
                level_kind,
            } => {
                let Some(bar) = self.current_bar().cloned() else {
                    return Ok(None);
                };
                let res = self.eval_level_event(path, level_id, *event, *level_kind, &bar);
                if res.is_some() {
                    self.state.known_inputs.insert(path.to_owned());
                }
                Ok(res)
            }
            RuleExpression::StructureEvent {
                structure_id,
                event,
                structure_family,
            } => {
                let Some(bar) = self.current_bar().cloned() else {
                    return Ok(None);
                };
                let res =
                    self.eval_structure_event(path, structure_id, *event, *structure_family, &bar);
                if res.is_some() {
                    self.state.known_inputs.insert(path.to_owned());
                }
                Ok(res)
            }
        }
    }

    fn eval_compare(
        &mut self,
        test: RuleComparisonTest,
        left: &RuleOperand,
        right: &RuleOperand,
    ) -> Result<Option<RuleObservation>, ApiError> {
        let Some(lhs) = self.eval_operand(left, false)? else {
            return Ok(None);
        };
        let Some(rhs) = self.eval_operand(right, false)? else {
            return Ok(None);
        };
        let mut matched = compare_values(test, lhs, rhs);
        if matches!(
            test,
            RuleComparisonTest::CrossAbove | RuleComparisonTest::CrossBelow
        ) {
            if !matched {
                return Ok(Some(RuleObservation {
                    matched: false,
                    observed: Some(lhs),
                    threshold: Some(rhs),
                    drawing_kind: None,
                    level: None,
                    structure: None,
                    level_event: None,
                    level_direction: None,
                    crossed_at: None,
                }));
            }
            let Some(cur_ts) = self.current_bar().map(|b| b.timestamp) else {
                return Ok(None);
            };
            if self.previous_bar().is_none() {
                return Ok(None);
            }
            let Some(prev_l) = self.eval_operand(left, true)? else {
                return Ok(None);
            };
            let Some(prev_r) = self.eval_operand(right, true)? else {
                return Ok(None);
            };
            matched = matched && !compare_values(test, prev_l, prev_r);
            return Ok(Some(RuleObservation {
                matched,
                observed: Some(lhs),
                threshold: Some(rhs),
                drawing_kind: None,
                level: None,
                structure: None,
                level_event: None,
                level_direction: None,
                crossed_at: matched.then_some(cur_ts),
            }));
        }
        Ok(Some(RuleObservation {
            matched,
            observed: Some(lhs),
            threshold: Some(rhs),
            drawing_kind: None,
            level: None,
            structure: None,
            level_event: None,
            level_direction: None,
            crossed_at: None,
        }))
    }

    fn eval_operand(
        &mut self,
        operand: &RuleOperand,
        previous: bool,
    ) -> Result<Option<Decimal>, ApiError> {
        match operand {
            RuleOperand::Constant { value } => Ok(Some(*value)),
            RuleOperand::Event { metric } => {
                let Some(ev) = &self.ctx.event else {
                    return Ok(None);
                };
                Ok(event_metric_value(*metric, ev))
            }
            RuleOperand::Derivative { kind } => {
                let target_tf = match kind {
                    DerivativeKind::MarkPrice => Some(self.ctx.timeframe.to_owned()),
                    DerivativeKind::FundingRate => None,
                    DerivativeKind::OpenInterest => {
                        oi_timeframe_for(self.ctx.timeframe).map(str::to_owned)
                    }
                };
                let cutoff = if previous {
                    let Some(prior) = self.previous_bar() else {
                        return Ok(None);
                    };
                    prior.ts_available.max(prior.timestamp + self.tf_ms)
                } else {
                    self.ctx.now_ms
                };
                let slice = match kind {
                    DerivativeKind::MarkPrice => self.ctx.mark_price,
                    DerivativeKind::FundingRate => self.ctx.funding,
                    DerivativeKind::OpenInterest => self.ctx.open_interest,
                };
                let found = slice.iter().rev().find(|pt| {
                    pt.ts_available <= cutoff
                        && (pt.timeframe.as_deref() == target_tf.as_deref()
                            || pt.timeframe.is_none())
                        && (*kind != DerivativeKind::FundingRate || pt.is_funding_settlement())
                });
                Ok(found.map(|p| p.value))
            }
            RuleOperand::LevelPrice {
                level_id,
                level_kind,
                edge,
            } => {
                let Some(lv) = self.visible_level(level_id, *level_kind) else {
                    return Ok(None);
                };
                let Some((low, high)) = level_bounds(&lv) else {
                    return Ok(None);
                };
                Ok(Some(match edge {
                    RuleEdge::Upper => high,
                    RuleEdge::Lower => low,
                }))
            }
            RuleOperand::StructurePrice {
                structure_id,
                structure_family,
                edge,
            } => {
                let Some(st) = self.visible_structure(structure_id, *structure_family) else {
                    return Ok(None);
                };
                let ts = if previous {
                    self.previous_bar().map(|b| b.timestamp)
                } else {
                    self.current_bar()
                        .map(|b| b.timestamp)
                        .or(match &self.ctx.event {
                            Some(FeedEventRef::Book(b)) => Some(b.ts_event),
                            Some(FeedEventRef::Derivative(d)) => Some(d.ts_event),
                            Some(FeedEventRef::Kline(k)) => Some(k.bar.timestamp),
                            None => None,
                        })
                };
                let Some(ts) = ts else {
                    return Ok(None);
                };
                let Some(bounds) = structure_bounds_at(&st, ts) else {
                    return Ok(None);
                };
                Ok(Some(match edge {
                    RuleEdge::Upper => bounds.upper,
                    RuleEdge::Lower => bounds.lower,
                }))
            }
            RuleOperand::DrawingPrice {
                drawing_id,
                edge,
                ratio,
            } => {
                let ts = if previous {
                    self.previous_bar().map(|b| b.timestamp)
                } else {
                    self.current_bar()
                        .map(|b| b.timestamp)
                        .or(match &self.ctx.event {
                            Some(FeedEventRef::Book(b)) => Some(b.ts_event),
                            Some(FeedEventRef::Derivative(d)) => Some(d.ts_event),
                            Some(FeedEventRef::Kline(k)) => Some(k.bar.timestamp),
                            None => None,
                        })
                };
                let Some(ts) = ts else {
                    return Ok(None);
                };
                let Some((_, bounds)) = self.drawing_bounds(drawing_id, *ratio, ts) else {
                    return Ok(None);
                };
                Ok(Some(match edge {
                    RuleEdge::Upper => bounds.upper,
                    RuleEdge::Lower => bounds.lower,
                }))
            }
            RuleOperand::Bar { field } => {
                let bar = if previous {
                    self.previous_bar()
                } else {
                    self.current_bar()
                };
                Ok(bar.and_then(|b| match field {
                    RuleBarField::Open => b.open,
                    RuleBarField::High => b.high,
                    RuleBarField::Low => b.low,
                    RuleBarField::Close => b.close,
                    RuleBarField::Volume => b.volume,
                    RuleBarField::Turnover => b.turnover,
                }))
            }
            RuleOperand::Indicator {
                name,
                window,
                snapshot_id,
                channel,
                params,
                ..
            } => {
                let key = operand_cache_key(*name, *window, snapshot_id, params);
                if !self.ctx.indicator_cache.contains_key(&key) {
                    let pts = compute_rule_indicator_points(
                        self.all_bars,
                        self.ctx.timeframe,
                        *name,
                        params,
                        self.ctx.funding,
                        self.ctx.open_interest,
                        self.ctx.mark_price,
                    )?;
                    self.ctx.indicator_cache.insert(key.clone(), pts);
                }
                let points = &self.ctx.indicator_cache[&key];
                let (canonical, _, _) = rule_indicator_to_spec(*name, params)?;
                let ch = channel
                    .as_deref()
                    .unwrap_or_else(|| canonical.channels()[0]);
                if previous {
                    let Some(prev) = self.previous_bar() else {
                        return Ok(None);
                    };
                    if points.len() < 2 || points[points.len() - 2].timestamp != prev.timestamp {
                        return Ok(None);
                    }
                    return Ok(points[points.len() - 2].values.get(ch).copied().flatten());
                }
                Ok(points
                    .last()
                    .and_then(|pt| pt.values.get(ch).copied().flatten()))
            }
        }
    }

    fn drawing_bounds(
        &self,
        drawing_id: &str,
        ratio: Option<Decimal>,
        ts: i64,
    ) -> Option<(DrawingRecord, OverlayBounds)> {
        let drawing = (self.ctx.drawing_lookup)(drawing_id)?;
        if drawing.deleted_at.is_some()
            || drawing.instrument_id != self.ctx.instrument_id
            || drawing.updated_ts > self.ctx.now_ms
        {
            return None;
        }
        let ratio = ratio.unwrap_or_else(|| Decimal::new(5, 1));
        let bounds = overlay_bounds(&drawing, ts, ratio)?;
        Some((drawing, bounds))
    }

    fn eval_overlay(
        &self,
        drawing_id: &str,
        mode: AlertOverlayMode,
        direction: AlertDirection,
        ratio: Option<Decimal>,
        bar: &CompactBar,
    ) -> Option<RuleObservation> {
        let (drawing, bounds) = self.drawing_bounds(drawing_id, ratio, bar.timestamp)?;
        let above = direction == AlertDirection::Above;
        let threshold = if above { bounds.upper } else { bounds.lower };
        let (price, matched) = match mode {
            AlertOverlayMode::Touch => {
                let p = if above { bar.high? } else { bar.low? };
                let ok = if above {
                    p >= threshold
                } else {
                    p <= threshold
                };
                (p, ok)
            }
            AlertOverlayMode::CloseBreak => {
                let p = bar.close?;
                let ok = if above {
                    p > threshold
                } else {
                    p < threshold
                };
                (p, ok)
            }
        };
        Some(RuleObservation {
            matched,
            observed: bar.close.or(Some(price)),
            threshold: Some(threshold),
            drawing_kind: Some(drawing.kind),
            level: None,
            structure: None,
            level_event: None,
            level_direction: None,
            crossed_at: None,
        })
    }

    fn visible_structure(
        &self,
        structure_id: &str,
        structure_family: Option<StructureFamily>,
    ) -> Option<StructureFeature> {
        let st = self.ctx.structures.get(structure_id)?;
        if st.instrument_id != self.ctx.instrument_id || st.ts_available > self.ctx.now_ms {
            return None;
        }
        if let Some(expected_family) = structure_family
            && st.family != expected_family
        {
            return None;
        }
        Some(st.clone())
    }

    fn eval_structure_event(
        &mut self,
        path: &str,
        structure_id: &str,
        event: AlertLevelEvent,
        structure_family: Option<StructureFamily>,
        bar: &CompactBar,
    ) -> Option<RuleObservation> {
        let st = self.visible_structure(structure_id, structure_family)?;
        let bounds = structure_bounds_at(&st, bar.timestamp)?;
        let (lower, upper) = (bounds.lower, bounds.upper);
        let close = bar.close?;
        let high = bar.high?;
        let low = bar.low?;

        if event == AlertLevelEvent::Touch {
            let matched = low <= upper && high >= lower;
            let threshold = if close >= upper { upper } else { lower };
            return Some(RuleObservation {
                matched,
                observed: Some(close),
                threshold: Some(threshold),
                drawing_kind: None,
                level: None,
                structure: Some(st),
                level_event: Some(event),
                level_direction: None,
                crossed_at: None,
            });
        }

        let side = if close > upper {
            Some(LevelSide::Up)
        } else if close < lower {
            Some(LevelSide::Down)
        } else {
            None
        };

        let mut crossed = false;
        if let Some(s) = side
            && st.status == StructureStatus::Active
        {
            let prev = self.previous_bar()?;
            let prev_close = prev.close?;
            let prev_bounds = structure_bounds_at(&st, prev.timestamp)?;
            crossed = match s {
                LevelSide::Up => prev_close <= prev_bounds.upper,
                LevelSide::Down => prev_close >= prev_bounds.lower,
            };
        }

        let broke = side.is_some() && (st.status != StructureStatus::Active || crossed);
        if event == AlertLevelEvent::Break {
            let threshold = if side == Some(LevelSide::Up) || (side.is_none() && close >= upper) {
                upper
            } else {
                lower
            };
            return Some(RuleObservation {
                matched: broke,
                observed: Some(close),
                threshold: Some(threshold),
                drawing_kind: None,
                level: None,
                structure: Some(st),
                level_event: Some(event),
                level_direction: if broke { side } else { None },
                crossed_at: None,
            });
        }

        let key = path.to_owned();
        if self.state.consumed_sides.get(&key).copied() != side {
            self.state.consumed_sides.remove(&key);
        }
        let mut current_break = self.state.breakouts.get(&key).copied();
        if broke
            && let Some(s) = side
        {
            let already_dir = current_break.map(|b| b.0);
            if already_dir != Some(s) && !self.state.consumed_sides.contains_key(&key) {
                let entry = (s, bar.timestamp);
                self.state.breakouts.insert(key.clone(), entry);
                current_break = Some(entry);
            }
        }
        let direction = current_break.map(|b| b.0);
        let threshold = if direction == Some(LevelSide::Up) || (direction.is_none() && close >= upper)
        {
            upper
        } else {
            lower
        };
        let mut matched = false;
        if let Some((dir, breakout_ts)) = current_break
            && bar.timestamp > breakout_ts
        {
            matched = match dir {
                LevelSide::Up => low <= upper && upper < close,
                LevelSide::Down => close < lower && lower <= high,
            };
            if matched {
                self.state.matched_retests.push(key);
            }
        }
        Some(RuleObservation {
            matched,
            observed: Some(close),
            threshold: Some(threshold),
            drawing_kind: None,
            level: None,
            structure: Some(st),
            level_event: Some(event),
            level_direction: direction,
            crossed_at: None,
        })
    }

    fn visible_level(
        &self,
        level_id: &str,
        level_kind: Option<talaria_contracts::key_levels::KeyLevelKind>,
    ) -> Option<KeyLevel> {
        let lv = self.ctx.levels.get(level_id)?;
        if lv.instrument_id != self.ctx.instrument_id || lv.ts_available > self.ctx.now_ms {
            return None;
        }
        if let Some(expected_kind) = level_kind
            && lv.kind != expected_kind
        {
            return None;
        }
        Some(lv.clone())
    }

    fn eval_level_event(
        &mut self,
        path: &str,
        level_id: &str,
        event: AlertLevelEvent,
        level_kind: Option<talaria_contracts::key_levels::KeyLevelKind>,
        bar: &CompactBar,
    ) -> Option<RuleObservation> {
        let lv = self.visible_level(level_id, level_kind)?;
        let (lower, upper) = level_bounds(&lv)?;
        let close = bar.close?;
        let high = bar.high?;
        let low = bar.low?;

        if event == AlertLevelEvent::Touch {
            let matched = low <= upper && high >= lower;
            let threshold = if close >= upper { upper } else { lower };
            return Some(RuleObservation {
                matched,
                observed: Some(close),
                threshold: Some(threshold),
                drawing_kind: None,
                level: Some(lv),
                structure: None,
                level_event: Some(event),
                level_direction: None,
                crossed_at: None,
            });
        }

        let side = if close > upper {
            Some(LevelSide::Up)
        } else if close < lower {
            Some(LevelSide::Down)
        } else {
            None
        };
        if side.is_some() && lv.status == KeyLevelStatus::Active {
            return None;
        }
        if event == AlertLevelEvent::Break {
            let threshold = if side == Some(LevelSide::Up) {
                upper
            } else {
                lower
            };
            return Some(RuleObservation {
                matched: side.is_some(),
                observed: Some(close),
                threshold: Some(threshold),
                drawing_kind: None,
                level: Some(lv),
                structure: None,
                level_event: Some(event),
                level_direction: side,
                crossed_at: None,
            });
        }

        // Retest state machine
        let key = path.to_owned();
        if self.state.consumed_sides.get(&key).copied() != side {
            self.state.consumed_sides.remove(&key);
        }
        let mut current_break = self.state.breakouts.get(&key).copied();
        if let Some(s) = side {
            let already_dir = current_break.map(|b| b.0);
            if already_dir != Some(s) && !self.state.consumed_sides.contains_key(&key) {
                let entry = (s, bar.timestamp);
                self.state.breakouts.insert(key.clone(), entry);
                current_break = Some(entry);
            }
        }
        let direction = current_break.map(|b| b.0);
        let threshold = if direction == Some(LevelSide::Up) || (direction.is_none() && close >= upper) {
            upper
        } else {
            lower
        };
        let mut matched = false;
        if let Some((dir, breakout_ts)) = current_break
            && bar.timestamp > breakout_ts
            && lv.status != KeyLevelStatus::Active
        {
            matched = match dir {
                LevelSide::Up => low <= upper && upper < close,
                LevelSide::Down => close < lower && lower <= high,
            };
            if matched {
                self.state.matched_retests.push(key);
            }
        }
        Some(RuleObservation {
            matched,
            observed: Some(close),
            threshold: Some(threshold),
            drawing_kind: None,
            level: Some(lv),
            structure: None,
            level_event: Some(event),
            level_direction: direction,
            crossed_at: None,
        })
    }
}

fn compare_values(test: RuleComparisonTest, lhs: Decimal, rhs: Decimal) -> bool {
    match test {
        RuleComparisonTest::Lt | RuleComparisonTest::CrossBelow => lhs < rhs,
        RuleComparisonTest::Lte => lhs <= rhs,
        RuleComparisonTest::Gt | RuleComparisonTest::CrossAbove => lhs > rhs,
        RuleComparisonTest::Gte => lhs >= rhs,
        RuleComparisonTest::Eq => lhs == rhs,
    }
}

fn event_metric_value(metric: AlertMetric, ev: &FeedEventRef<'_>) -> Option<Decimal> {
    match (metric, ev) {
        (AlertMetric::LastPrice, FeedEventRef::Kline(k)) => k.bar.close,
        (AlertMetric::BidPrice, FeedEventRef::Book(b)) => Some(b.bid_price),
        (AlertMetric::AskPrice, FeedEventRef::Book(b)) => Some(b.ask_price),
        (AlertMetric::MarkPrice, FeedEventRef::Derivative(d)) => d.mark_price,
        (AlertMetric::FundingRate, FeedEventRef::Derivative(d)) => d.funding_rate,
        (AlertMetric::OpenInterest, FeedEventRef::Derivative(d)) => d.open_interest,
        _ => None,
    }
}

/// Preview rule evaluation across a slice of confirmed historical bars (used by `/api/alerts/preview`).
#[allow(clippy::too_many_arguments)]
pub fn preview_rule(
    instrument_id: &str,
    timeframe: &str,
    rule: &Rule,
    bars: &[CompactBar],
    funding: &[DerivativePoint],
    open_interest: &[DerivativePoint],
    mark_price: &[DerivativePoint],
    levels_by_cutoff: &dyn Fn(i64) -> HashMap<String, KeyLevel>,
    structures_by_cutoff: &dyn Fn(i64) -> HashMap<String, StructureFeature>,
    drawing_lookup: &dyn Fn(&str) -> Option<DrawingRecord>,
    limit: usize,
) -> Result<RulePreviewResponse, ApiError> {
    validate_rule_complexity(rule)?;
    let tf_ms = timeframe_ms(timeframe)?;
    let start = bars.len().saturating_sub(limit.max(1));
    let window_bars = &bars[start..];

    let mut condition_true = Vec::new();
    let mut would_trigger = Vec::new();
    let mut rule_state = RuleState::default();
    let mut prev_cond: Option<bool> = None;

    for idx in start..bars.len() {
        let prefix = &bars[..=idx];
        let bar = &bars[idx];
        if bar.status == BarStatus::FeedGap {
            continue;
        }
        let observed_at = bar.ts_available.max(bar.timestamp + tf_ms);
        let lvl_map = levels_by_cutoff(observed_at);
        let st_map = structures_by_cutoff(observed_at);
        let mut ctx = EvalContext {
            instrument_id,
            timeframe,
            now_ms: observed_at,
            bars: prefix,
            funding,
            open_interest,
            mark_price,
            levels: &lvl_map,
            structures: &st_map,
            drawing_lookup,
            event: None,
            bar_ts_override: Some(bar.timestamp),
            indicator_cache: BTreeMap::new(),
        };
        if let Some(obs) = observe_rule(rule, &mut ctx, &mut rule_state, true)? {
            let signal = RulePreviewSignal {
                timestamp: bar.timestamp,
                observed_at,
                price: bar.close,
            };
            if obs.matched {
                condition_true.push(signal.clone());
                let became_true = prev_cond == Some(false)
                    || (prev_cond.is_none() && obs.crossed_at.is_some());
                if became_true {
                    would_trigger.push(signal);
                }
            }
            prev_cond = Some(obs.matched);
        }
    }

    // Collect chart indicators referenced by operands for overlay rendering
    let mut indicators = Vec::new();
    let mut seen_operands = HashSet::new();
    collect_indicator_operands(&rule.expression, &mut |operand| {
        if let RuleOperand::Indicator {
            name,
            window,
            snapshot_id,
            params,
            ..
        } = operand
        {
            let key = operand_cache_key(*name, *window, snapshot_id, params);
            if !seen_operands.insert(key) {
                return;
            }
            if let Ok((canonical, ind_params, is_legacy)) = rule_indicator_to_spec(*name, params) {
                let pane = match canonical.pane() {
                    talaria_contracts::indicators::IndicatorPane::Main => RulePane::Main,
                    talaria_contracts::indicators::IndicatorPane::Sub => RulePane::Sub,
                };
                let pts = if is_legacy {
                    OperatorSpec::from_params(canonical, Some(&ind_params))
                        .and_then(|s| legacy_alert_points(window_bars, &s))
                        .map(|v| {
                            window_bars
                                .iter()
                                .zip(v)
                                .map(|(b, values)| IndicatorPoint {
                                    timestamp: b.timestamp,
                                    values,
                                })
                                .collect()
                        })
                        .unwrap_or_default()
                } else {
                    OperatorSpec::from_params(canonical, Some(&ind_params))
                        .and_then(|s| compute_indicator_series(window_bars, &s, None, None))
                        .map(|s| s.points)
                        .unwrap_or_default()
                };
                indicators.push(RulePreviewIndicator {
                    operand: operand.clone(),
                    pane,
                    points: pts,
                });
            }
        }
    });

    Ok(RulePreviewResponse {
        rule: rule.clone(),
        unavailable_current_levels: Vec::new(),
        history_incomplete: false,
        condition_true,
        would_trigger,
        indicators,
    })
}

fn collect_indicator_operands(node: &RuleExpression, f: &mut dyn FnMut(&RuleOperand)) {
    match node {
        RuleExpression::Compare { left, right, .. } => {
            f(left);
            f(right);
        }
        RuleExpression::All { args }
        | RuleExpression::Any { args }
        | RuleExpression::Not { args } => {
            for child in args {
                collect_indicator_operands(child, f);
            }
        }
        RuleExpression::Overlay { .. }
        | RuleExpression::LevelEvent { .. }
        | RuleExpression::StructureEvent { .. } => {}
    }
}

/// Deterministic annotation hard gate (`snapshot -> evidence -> prices -> time`).
pub fn evaluate_annotation_gate(
    ann: &AgentAnnotationCreate,
    instrument: &InstrumentSpec,
    meta: &SnapshotMeta,
    bars: &[CompactBar],
    valid_level_ids: &HashSet<String>,
) -> Result<(), ApiError> {
    if meta.instrument_id != ann.instrument_id {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "SNAPSHOT_MISMATCH",
            format!(
                "快照属于 {}，但标注声明 {}",
                meta.instrument_id, ann.instrument_id
            ),
        ));
    }
    if instrument.instrument_id != ann.instrument_id {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "SNAPSHOT_MISMATCH",
            format!("未知标的 {}", ann.instrument_id),
        ));
    }
    if !meta.timeframes.iter().any(|tf| tf == &ann.timeframe) {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "SNAPSHOT_MISMATCH",
            format!("快照不包含周期 {}", ann.timeframe),
        ));
    }

    if ann.evidence_refs.is_empty() {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "EVIDENCE_EMPTY",
            "智能体标注必须至少引用一条快照证据",
        ));
    }
    let tf_ms = timeframe_ms(&ann.timeframe)?;
    let tolerance = KLINE_EVIDENCE_TOLERANCE_BARS * tf_ms;
    let timestamps: Vec<i64> = bars.iter().map(|b| b.timestamp).collect();
    let mut unresolved = Vec::new();
    for ev in &ann.evidence_refs {
        if let Some(structure_id) = &ev.structure_id {
            if !valid_level_ids.contains(structure_id) {
                unresolved.push(format!("structure_id={structure_id}"));
            }
            continue;
        }
        if let Some(level_id) = &ev.level_id {
            if !valid_level_ids.contains(level_id) {
                unresolved.push(format!("level_id={level_id}"));
            }
            continue;
        }
        let Some(kline_ts) = ev.kline_ts else {
            unresolved.push("missing evidence ref".to_owned());
            continue;
        };
        let pos = timestamps.partition_point(|&ts| ts < kline_ts);
        let near = [pos.checked_sub(1), Some(pos)]
            .into_iter()
            .flatten()
            .filter_map(|i| timestamps.get(i))
            .any(|&ts| (ts - kline_ts).abs() <= tolerance);
        if !near {
            unresolved.push(format!("kline_ts={kline_ts}"));
        }
    }
    if !unresolved.is_empty() {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "EVIDENCE_UNRESOLVED",
            format!("以下证据无法在快照中定位：{}", unresolved.join("、")),
        ));
    }

    if ann.price_low <= Decimal::ZERO || ann.price_high <= Decimal::ZERO || ann.price_low > ann.price_high {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "PRICE_INVALID",
            "标注价格必须大于 0 且 price_low <= price_high",
        ));
    }
    let tick = instrument.tick_size;
    if tick > Decimal::ZERO && ((ann.price_low % tick) != Decimal::ZERO || (ann.price_high % tick) != Decimal::ZERO) {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "PRICE_NOT_TICK_ALIGNED",
            format!("价格必须对齐最小变动价位 {tick}"),
        ));
    }

    let envelope = atr_envelope(bars)?;
    let Some((env_low, env_high)) = envelope else {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "PRICE_OUT_OF_BOUNDS",
            "快照窗口内无法计算 ATR_14，拒绝无界价格",
        ));
    };
    if ann.price_low < env_low || ann.price_high > env_high {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "PRICE_OUT_OF_BOUNDS",
            format!("价格必须落在 [{env_low}, {env_high}]（窗口极值 ±3×ATR_14）"),
        ));
    }

    let Some(first_bar) = bars.first() else {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            SNAPSHOT_NOT_FOUND,
            "快照窗口为空",
        ));
    };
    let last_bar = bars.last().expect("bars non-empty");
    if ann.ts_anchor < first_bar.timestamp {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "TIME_BEFORE_SNAPSHOT",
            format!(
                "时间锚点 {} 早于快照起点 {}",
                ann.ts_anchor, first_bar.timestamp
            ),
        ));
    }
    let horizon = last_bar.timestamp + FUTURE_BARS_LIMIT * tf_ms;
    if ann.ts_anchor > horizon {
        return Err(ApiError::with_status(
            axum::http::StatusCode::UNPROCESSABLE_ENTITY,
            "TIME_TOO_FAR_FUTURE",
            format!("时间锚点最多向未来推演 {FUTURE_BARS_LIMIT} 根，最晚 {horizon}"),
        ));
    }
    Ok(())
}

fn atr_envelope(bars: &[CompactBar]) -> Result<Option<(Decimal, Decimal)>, ApiError> {
    let usable: Vec<CompactBar> = bars
        .iter()
        .filter(|b| b.status != BarStatus::FeedGap)
        .cloned()
        .collect();
    if usable.is_empty() {
        return Ok(None);
    }
    let min_low = usable.iter().filter_map(|b| b.low).min();
    let max_high = usable.iter().filter_map(|b| b.high).max();
    let (Some(min_low), Some(max_high)) = (min_low, max_high) else {
        return Ok(None);
    };
    let spec = OperatorSpec::new(IndicatorName::Atr, Some(ATR_PERIOD), None, None)
        .map_err(|e| ApiError::validation(e.to_string()))?;
    let state = compute_series(&usable, &spec, None)?;
    let atr = state
        .points
        .iter()
        .rev()
        .find_map(|p| p.get("value").copied().flatten());
    let Some(atr) = atr else {
        return Ok(None);
    };
    let band = Decimal::from(3u32) * atr;
    Ok(Some((min_low - band, max_high + band)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use talaria_contracts::bars::{BarStatus, CompactBar};
    use talaria_contracts::key_levels::EvidenceClass;
    use talaria_contracts::rules::{
        RuleBarField, RuleComparisonTest, RuleEdge, RuleOperand, RuleTiming, RuleVersion,
    };
    use talaria_contracts::structures::{
        AnalysisWindow, PriceScaleMode, STRUCTURE_METHOD_VERSION, StructureAnchor,
        StructureCategory, StructureFamily, StructureFeature, StructureGeometry, StructureStatus,
    };
    use talaria_contracts::types::PrecisionTier;

    fn dec(v: i64) -> Decimal {
        Decimal::from(v)
    }

    #[test]
    fn structure_trendline_break_and_price_eval() {
        let bars = vec![
            CompactBar {
                timestamp: 1_767_225_600_000,
                ts_available: 1_767_225_660_000,
                status: BarStatus::Normal,
                precision: PrecisionTier::SecondVerified,
                open: Some(dec(102)),
                high: Some(dec(105)),
                low: Some(dec(101)),
                close: Some(dec(103)),
                volume: Some(dec(10)),
                turnover: Some(dec(1000)),
            },
            CompactBar {
                timestamp: 1_767_225_660_000,
                ts_available: 1_767_225_720_000,
                status: BarStatus::Normal,
                precision: PrecisionTier::SecondVerified,
                open: Some(dec(103)),
                high: Some(dec(115)),
                low: Some(dec(103)),
                close: Some(dec(114)),
                volume: Some(dec(15)),
                turnover: Some(dec(1600)),
            },
        ];
        // Slope rises from 105 at bar[0] to 110 at bar[1]
        let st = StructureFeature {
            structure_id: "st_trend_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
            detector_id: "pivot_trendline@1".to_owned(),
            family: StructureFamily::Trendline,
            category: StructureCategory::Trend,
            kind: "TRENDLINE_RESISTANCE".to_owned(),
            evidence_class: EvidenceClass::Observed,
            timeframe: "1m".to_owned(),
            source_timeframe: Some("1m".to_owned()),
            geometry: StructureGeometry::SlopeLine {
                anchor_start: StructureAnchor {
                    ts: bars[0].timestamp,
                    price: dec(105),
                },
                anchor_end: StructureAnchor {
                    ts: bars[1].timestamp,
                    price: dec(110),
                },
                scale: PriceScaleMode::Linear,
                extend_right: true,
            },
            strength: 0.9,
            status: StructureStatus::Active,
            ts_available: bars[0].ts_available,
            formed_ts: bars[0].timestamp,
            analysis_window: AnalysisWindow {
                from_ts: bars[0].timestamp,
                to_ts: bars[1].timestamp,
            },
            touch_count: 2,
            touch_timestamps: vec![bars[0].timestamp, bars[1].timestamp],
            last_touch_ts: Some(bars[1].timestamp),
            volume_weight: dec(0),
            constituent_ids: Vec::new(),
            method_version: STRUCTURE_METHOD_VERSION.to_owned(),
            parameters: BTreeMap::new(),
        };
        let mut structures = HashMap::new();
        structures.insert(st.structure_id.clone(), st);
        let levels = HashMap::new();
        let drawing_lookup = |_id: &str| None;

        let rule_break = Rule {
            version: RuleVersion::V1,
            timing: RuleTiming::BarClose,
            expression: RuleExpression::StructureEvent {
                structure_id: "st_trend_1".to_owned(),
                event: AlertLevelEvent::Break,
                structure_family: Some(StructureFamily::Trendline),
            },
        };
        let mut ctx = EvalContext {
            instrument_id: "BTC-USDT-PERP",
            timeframe: "1m",
            now_ms: bars[1].ts_available,
            bars: &bars,
            funding: &[],
            open_interest: &[],
            mark_price: &[],
            levels: &levels,
            structures: &structures,
            drawing_lookup: &drawing_lookup,
            event: None,
            bar_ts_override: Some(bars[1].timestamp),
            indicator_cache: BTreeMap::new(),
        };
        let mut state = RuleState::default();
        let obs = observe_rule(&rule_break, &mut ctx, &mut state, true)
            .unwrap()
            .expect("expected observation");
        assert!(obs.matched);
        assert_eq!(obs.threshold, Some(dec(110)));
        assert_eq!(obs.level_direction, Some(LevelSide::Up));

        // Check RuleStructurePrice > threshold
        let rule_price = Rule {
            version: RuleVersion::V1,
            timing: RuleTiming::BarClose,
            expression: RuleExpression::Compare {
                test: RuleComparisonTest::Gt,
                left: Box::new(RuleOperand::Bar {
                    field: RuleBarField::Close,
                }),
                right: Box::new(RuleOperand::StructurePrice {
                    structure_id: "st_trend_1".to_owned(),
                    structure_family: Some(StructureFamily::Trendline),
                    edge: RuleEdge::Upper,
                }),
            },
        };
        let obs_price = observe_rule(&rule_price, &mut ctx, &mut state, true)
            .unwrap()
            .expect("expected price obs");
        assert!(obs_price.matched);
        assert_eq!(obs_price.threshold, Some(dec(110)));
    }
}
