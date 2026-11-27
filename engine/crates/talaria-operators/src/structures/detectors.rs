//! Built-in 10 deterministic `StructureDetector` implementations.

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use talaria_contracts::key_levels::{EvidenceClass, KeyLevel, KeyLevelCategory, KeyLevelStatus, VwapAnchor};
use talaria_contracts::structures::{
    AnchorSource, PriceScaleMode, STRUCTURE_METHOD_VERSION, StructureAnchor, StructureCategory,
    StructureFamily, StructureFeature, StructureGeometry, StructureStatus,
};

use crate::error::OperatorError;
use crate::key_levels::{
    anchored_vwap_levels, gap_levels, liquidation_levels, round_levels, session_levels,
    structure_levels, volume_profile_levels,
};
use crate::structures::context::{ConfirmedPivot, DetectionContext, PivotSide, make_structure_id};
use crate::structures::geometry::geometry_bounds_at;
use crate::structures::registry::StructureDetector;

pub const LEGACY_LEVEL_DETECTORS: &[&str] = &[
    "swing_levels@1",
    "session_levels@1",
    "gap_levels@1",
    "round_numbers@1",
    "volume_profile@1",
    "liquidation_zones@1",
];

fn clamp_strength(v: f64) -> f64 {
    let clamped = v.clamp(0.05, 0.95);
    (clamped * 1000.0).round() / 1000.0
}

fn key_level_kind_str(kind: talaria_contracts::key_levels::KeyLevelKind) -> &'static str {
    use talaria_contracts::key_levels::KeyLevelKind::*;
    match kind {
        SwingHigh => "SWING_HIGH",
        SwingLow => "SWING_LOW",
        PrevDayHigh => "PREV_DAY_HIGH",
        PrevDayLow => "PREV_DAY_LOW",
        PrevDayClose => "PREV_DAY_CLOSE",
        WeekOpen => "WEEK_OPEN",
        MonthOpen => "MONTH_OPEN",
        OpeningRange => "OPENING_RANGE",
        SessionHigh => "SESSION_HIGH",
        SessionLow => "SESSION_LOW",
        Gap => "GAP",
        RoundNumber => "ROUND_NUMBER",
        Poc => "POC",
        Vah => "VAH",
        Val => "VAL",
        AnchoredVwap => "ANCHORED_VWAP",
        LiquidationZone => "LIQUIDATION_ZONE",
    }
}

fn convert_key_level(
    level: KeyLevel,
    detector_id: &str,
    ctx: &DetectionContext,
) -> StructureFeature {
    let (family, geometry) = if let Some(price) = level.price {
        (
            StructureFamily::Level,
            StructureGeometry::HorizontalLine { price },
        )
    } else {
        (
            StructureFamily::Zone,
            StructureGeometry::HorizontalZone {
                price_low: level.price_low.unwrap_or(Decimal::ZERO),
                price_high: level.price_high.unwrap_or(Decimal::ZERO),
                start_ts: None,
                end_ts: None,
            },
        )
    };
    let category = match level.category {
        KeyLevelCategory::Structure => StructureCategory::Structure,
        KeyLevelCategory::Session => StructureCategory::Session,
        KeyLevelCategory::Gap => StructureCategory::Gap,
        KeyLevelCategory::RoundNumber => StructureCategory::RoundNumber,
        KeyLevelCategory::Volume => StructureCategory::Volume,
        KeyLevelCategory::Liquidation => StructureCategory::Liquidation,
    };
    let status = match level.status {
        KeyLevelStatus::Active => StructureStatus::Active,
        KeyLevelStatus::Broken => StructureStatus::Broken,
        KeyLevelStatus::Flipped => StructureStatus::Flipped,
    };
    let st_id = format!("st_{}", level.level_id.strip_prefix("kl_").unwrap_or(&level.level_id));
    let touch_timestamps = level.last_touch_ts.into_iter().collect();
    StructureFeature {
        structure_id: st_id,
        instrument_id: level.instrument_id,
        detector_id: detector_id.to_owned(),
        family,
        category,
        kind: key_level_kind_str(level.kind).to_owned(),
        evidence_class: level.evidence_class,
        timeframe: level.timeframe.clone(),
        source_timeframe: Some(level.timeframe),
        geometry,
        strength: level.strength,
        status,
        ts_available: level.ts_available,
        formed_ts: level.formed_ts,
        analysis_window: ctx.analysis_window.clone().expect("non-empty window"),
        touch_count: level.touch_count,
        touch_timestamps,
        last_touch_ts: level.last_touch_ts,
        volume_weight: level.volume_weight,
        constituent_ids: Vec::new(),
        method_version: STRUCTURE_METHOD_VERSION.to_owned(),
        parameters: level.parameters,
    }
}

fn convert_all(
    levels: Vec<KeyLevel>,
    detector_id: &str,
    ctx: &DetectionContext,
) -> Vec<StructureFeature> {
    levels
        .into_iter()
        .map(|lv| convert_key_level(lv, detector_id, ctx))
        .collect()
}

pub struct SwingLevelsDetector;
impl StructureDetector for SwingLevelsDetector {
    fn detector_id(&self) -> &'static str {
        "swing_levels@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Level, StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let levels = structure_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            ctx.cutoff_ts,
            &ctx.atr,
            &kp,
            &base,
            ctx.bar_ms,
        )?;
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct SessionLevelsDetector;
impl StructureDetector for SessionLevelsDetector {
    fn detector_id(&self) -> &'static str {
        "session_levels@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Level, StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let levels = session_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            ctx.cutoff_ts,
            &kp,
            &base,
            ctx.bar_ms,
            &ctx.session_profile,
        )?;
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct GapLevelsDetector;
impl StructureDetector for GapLevelsDetector {
    fn detector_id(&self) -> &'static str {
        "gap_levels@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let levels = gap_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            ctx.cutoff_ts,
            &ctx.atr,
            &kp,
            &base,
            ctx.bar_ms,
        )?;
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct RoundNumbersDetector;
impl StructureDetector for RoundNumbersDetector {
    fn detector_id(&self) -> &'static str {
        "round_numbers@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Level]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let levels = round_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            &ctx.atr,
            &kp,
            &base,
        )?;
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct VolumeProfileDetector;
impl StructureDetector for VolumeProfileDetector {
    fn detector_id(&self) -> &'static str {
        "volume_profile@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Level, StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let mut levels = volume_profile_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            ctx.cutoff_ts,
            &ctx.atr,
            &kp,
            &base,
            ctx.bar_ms,
            &ctx.session_profile,
        )?;
        let vwap_bars = if kp.vwap_anchor == VwapAnchor::Session {
            &ctx.visible_with_gaps
        } else {
            &ctx.visible
        };
        let vwap = anchored_vwap_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            vwap_bars,
            ctx.cutoff_ts,
            &kp,
            &base,
            ctx.bar_ms,
        )?;
        levels.extend(vwap);
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct LiquidationZonesDetector;
impl StructureDetector for LiquidationZonesDetector {
    fn detector_id(&self) -> &'static str {
        "liquidation_zones@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let kp = ctx.params.to_key_level_params();
        let base = kp.as_parameters()?;
        let (levels, _notes) = liquidation_levels(
            &ctx.instrument_id,
            &ctx.timeframe,
            &ctx.visible,
            ctx.cutoff_ts,
            &ctx.atr,
            &kp,
            &base,
            ctx.bar_ms,
            &ctx.derivative_points,
        )?;
        Ok(convert_all(levels, self.detector_id(), ctx))
    }
}

pub struct FvgDetector;
impl StructureDetector for FvgDetector {
    fn detector_id(&self) -> &'static str {
        "fvg@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let bars = &ctx.visible;
        let Some(window) = &ctx.analysis_window else {
            return Ok(Vec::new());
        };
        if bars.len() < 3 {
            return Ok(Vec::new());
        }
        let min_mult = Decimal::try_from(ctx.params.fvg_atr_min).unwrap_or(Decimal::ZERO);
        let start_idx = bars.len().saturating_sub(ctx.params.trendline_lookback_bars).max(2);
        let params_map = ctx.params.as_parameters()?;
        let mut out = Vec::new();

        for idx in start_idx..bars.len() {
            if ctx.island_ids[idx - 2] != ctx.island_ids[idx - 1]
                || ctx.island_ids[idx - 1] != ctx.island_ids[idx]
            {
                continue;
            }
            let (b0, b1, b2) = (&bars[idx - 2], &bars[idx - 1], &bars[idx]);
            let (Some(b0h), Some(b0l), Some(b2h), Some(b2l)) = (b0.high, b0.low, b2.high, b2.low)
            else {
                continue;
            };
            let atr = ctx.bar_atr(idx);
            let min_gap = atr * min_mult;
            let (gap_low, gap_high, kind) = if b2l > b0h && (b2l - b0h) >= min_gap {
                (b0h, b2l, "FVG_BULL")
            } else if b2h < b0l && (b0l - b2h) >= min_gap {
                (b2h, b0l, "FVG_BEAR")
            } else {
                continue;
            };

            let ratio = if atr > Decimal::ZERO {
                ((gap_high - gap_low) / atr).to_string().parse::<f64>().unwrap_or(1.0)
            } else {
                1.0
            };
            let strength = clamp_strength(0.35 + (ratio * 0.25).min(0.45));
            out.push(StructureFeature {
                structure_id: make_structure_id(
                    &ctx.instrument_id,
                    self.detector_id(),
                    kind,
                    &ctx.timeframe,
                    b1.timestamp,
                    gap_low,
                    "",
                )?,
                instrument_id: ctx.instrument_id.clone(),
                detector_id: self.detector_id().to_owned(),
                family: StructureFamily::Zone,
                category: StructureCategory::Gap,
                kind: kind.to_owned(),
                evidence_class: EvidenceClass::Observed,
                timeframe: ctx.timeframe.clone(),
                source_timeframe: Some(ctx.timeframe.clone()),
                geometry: StructureGeometry::HorizontalZone {
                    price_low: gap_low,
                    price_high: gap_high,
                    start_ts: Some(b0.timestamp),
                    end_ts: None,
                },
                strength,
                status: StructureStatus::Active,
                ts_available: b2.ts_available,
                formed_ts: b1.timestamp,
                analysis_window: window.clone(),
                touch_count: 0,
                touch_timestamps: Vec::new(),
                last_touch_ts: None,
                volume_weight: Decimal::ZERO,
                constituent_ids: Vec::new(),
                method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                parameters: params_map.clone(),
            });
        }
        Ok(out)
    }
}

pub struct EqualLevelsDetector;
impl StructureDetector for EqualLevelsDetector {
    fn detector_id(&self) -> &'static str {
        "equal_levels@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Zone]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let Some(window) = &ctx.analysis_window else {
            return Ok(Vec::new());
        };
        if ctx.visible.is_empty() {
            return Ok(Vec::new());
        }
        let pivots = ctx.confirmed_pivots(None, None, None)?;
        if pivots.len() < 2 {
            return Ok(Vec::new());
        }
        let tol_mult = Decimal::try_from(ctx.params.equal_level_atr).unwrap_or(Decimal::ZERO);
        let pb_mult = Decimal::try_from(ctx.params.equal_pullback_atr).unwrap_or(Decimal::ZERO);
        let params_map = ctx.params.as_parameters()?;
        let mut out = Vec::new();

        for (side, kind) in [(PivotSide::High, "EQUAL_HIGHS"), (PivotSide::Low, "EQUAL_LOWS")] {
            let side_pivots: Vec<&ConfirmedPivot> =
                pivots.iter().filter(|p| p.side == side).collect();
            let mut used = vec![false; side_pivots.len()];

            for i in 0..side_pivots.len() {
                if used[i] {
                    continue;
                }
                let anchor = side_pivots[i];
                let mut cluster = vec![anchor];
                let atr_ref = anchor
                    .atr
                    .as_ref()
                    .and_then(|a| if a.is_positive() { a.to_decimal().ok() } else { None })
                    .unwrap_or_else(|| ctx.bar_atr(anchor.index));
                let tol = atr_ref * tol_mult;
                let min_pb = atr_ref * pb_mult;

                for j in (i + 1)..side_pivots.len() {
                    let cand = side_pivots[j];
                    if (cand.price - anchor.price).abs() > tol {
                        continue;
                    }
                    let prev = *cluster.last().expect("non-empty cluster");
                    if cand.index.saturating_sub(prev.index) < 2 {
                        continue;
                    }
                    let between = &ctx.visible[(prev.index + 1)..cand.index];
                    let has_pullback = if side == PivotSide::High {
                        between
                            .iter()
                            .filter_map(|b| b.low)
                            .min()
                            .is_some_and(|m| anchor.price.min(cand.price) - m >= min_pb)
                    } else {
                        between
                            .iter()
                            .filter_map(|b| b.high)
                            .max()
                            .is_some_and(|m| m - anchor.price.max(cand.price) >= min_pb)
                    };
                    if has_pullback {
                        cluster.push(cand);
                        used[j] = true;
                    }
                }

                if cluster.len() < 2 {
                    continue;
                }
                used[i] = true;
                let zone_low = cluster.iter().map(|p| p.price).min().unwrap();
                let zone_high = cluster.iter().map(|p| p.price).max().unwrap();
                let earliest = cluster[0];
                let ts_available = cluster.iter().map(|p| p.ts_available).max().unwrap();
                let last_touch_ts = cluster.last().map(|p| p.formed_ts);
                let touch_timestamps: Vec<i64> = cluster.iter().map(|p| p.formed_ts).collect();
                let strength = clamp_strength(0.45 + 0.12 * (cluster.len().min(4) as f64));

                out.push(StructureFeature {
                    structure_id: make_structure_id(
                        &ctx.instrument_id,
                        self.detector_id(),
                        kind,
                        &ctx.timeframe,
                        earliest.formed_ts,
                        zone_low,
                        "",
                    )?,
                    instrument_id: ctx.instrument_id.clone(),
                    detector_id: self.detector_id().to_owned(),
                    family: StructureFamily::Zone,
                    category: StructureCategory::Structure,
                    kind: kind.to_owned(),
                    evidence_class: EvidenceClass::Observed,
                    timeframe: ctx.timeframe.clone(),
                    source_timeframe: Some(ctx.timeframe.clone()),
                    geometry: StructureGeometry::HorizontalZone {
                        price_low: zone_low,
                        price_high: zone_high,
                        start_ts: Some(earliest.formed_ts),
                        end_ts: None,
                    },
                    strength,
                    status: StructureStatus::Active,
                    ts_available,
                    formed_ts: earliest.formed_ts,
                    analysis_window: window.clone(),
                    touch_count: cluster.len() as u64,
                    touch_timestamps,
                    last_touch_ts,
                    volume_weight: Decimal::ZERO,
                    constituent_ids: Vec::new(),
                    method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                    parameters: params_map.clone(),
                });
            }
        }
        Ok(out)
    }
}

fn collect_multiscale_pivots(
    ctx: &DetectionContext,
) -> Result<(Vec<ConfirmedPivot>, Vec<ConfirmedPivot>), OperatorError> {
    let min_index = ctx
        .visible
        .len()
        .saturating_sub(ctx.params.trendline_lookback_bars);
    let mut by_key: BTreeMap<(u8, usize), ConfirmedPivot> = BTreeMap::new();
    for &span in &ctx.params.trendline_spans {
        let confirm = span.max(ctx.params.confirm_bars);
        for pivot in ctx.confirmed_pivots(Some(span), Some(confirm), None)? {
            if pivot.index < min_index {
                continue;
            }
            let side_key = match pivot.side {
                PivotSide::Low => 0u8,
                PivotSide::High => 1u8,
            };
            let key = (side_key, pivot.index);
            let replace = match by_key.get(&key) {
                None => true,
                Some(existing) => {
                    pivot.span > existing.span || pivot.prominence > existing.prominence
                }
            };
            if replace {
                by_key.insert(key, pivot);
            }
        }
    }

    let select_side = |target: PivotSide| {
        let mut items: Vec<ConfirmedPivot> = by_key
            .values()
            .filter(|p| p.side == target)
            .cloned()
            .collect();
        if items.len() > ctx.params.trendline_max_pivots {
            items.sort_by(|a, b| b.prominence.cmp(&a.prominence).then_with(|| a.index.cmp(&b.index)));
            items.truncate(ctx.params.trendline_max_pivots);
        }
        items.sort_by_key(|p| p.index);
        items
    };
    Ok((select_side(PivotSide::Low), select_side(PivotSide::High)))
}

fn anchor_price(pivot: &ConfirmedPivot, source: AnchorSource) -> Decimal {
    match source {
        AnchorSource::Body => pivot.body_price,
        AnchorSource::Wick => pivot.price,
    }
}

fn median_f64(values: &[f64]) -> f64 {
    if values.is_empty() {
        return 4.0;
    }
    let mut sorted = values.to_vec();
    sorted.sort_by(f64::total_cmp);
    let mid = sorted.len() / 2;
    if sorted.len() % 2 == 1 {
        sorted[mid]
    } else {
        (sorted[mid - 1] + sorted[mid]) / 2.0
    }
}

pub struct PivotTrendlineDetector;
impl StructureDetector for PivotTrendlineDetector {
    fn detector_id(&self) -> &'static str {
        "pivot_trendline@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Trendline]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let bars = &ctx.visible;
        let Some(window) = &ctx.analysis_window else {
            return Ok(Vec::new());
        };
        if bars.len() < ctx.params.min_anchor_span + 2 {
            return Ok(Vec::new());
        }
        let (lows, highs) = collect_multiscale_pivots(ctx)?;
        let params = &ctx.params;
        let scale = params.trendline_scale;
        let source = params.trendline_anchor_source;
        let body_mult = Decimal::try_from(params.max_body_pierce_atr).unwrap_or(Decimal::ZERO);
        let wick_mult = Decimal::try_from(params.max_wick_pierce_atr).unwrap_or(Decimal::ZERO);
        let touch_mult = Decimal::try_from(params.trendline_touch_atr).unwrap_or(Decimal::ZERO);
        let params_map = params.as_parameters()?;
        let scale_str = match scale {
            PriceScaleMode::Linear => "LINEAR",
            PriceScaleMode::Log => "LOG",
        };
        let source_str = match source {
            AnchorSource::Wick => "WICK",
            AnchorSource::Body => "BODY",
        };
        let mut out = Vec::new();

        for (side_pivots, is_support, kind) in [
            (&lows, true, "STRUCTURAL_SUPPORT"),
            (&highs, false, "STRUCTURAL_RESISTANCE"),
        ] {
            for i in 0..side_pivots.len() {
                let p1 = &side_pivots[i];
                let price1 = anchor_price(p1, source);
                if scale == PriceScaleMode::Log && price1 <= Decimal::ZERO {
                    continue;
                }
                for j in (i + 1)..side_pivots.len() {
                    let p2 = &side_pivots[j];
                    if p2.index - p1.index < params.min_anchor_span {
                        continue;
                    }
                    if params.respect_islands && p1.island_id != p2.island_id {
                        continue;
                    }
                    let price2 = anchor_price(p2, source);
                    if scale == PriceScaleMode::Log && price2 <= Decimal::ZERO {
                        continue;
                    }
                    if p1.formed_ts >= p2.formed_ts {
                        continue;
                    }

                    let geometry = StructureGeometry::SlopeLine {
                        anchor_start: StructureAnchor {
                            ts: p1.formed_ts,
                            price: price1,
                        },
                        anchor_end: StructureAnchor {
                            ts: p2.formed_ts,
                            price: price2,
                        },
                        scale,
                        extend_right: true,
                    };

                    let mut valid = true;
                    let mut wick_pierce_count = 0usize;
                    let mut post_break_pierced = false;
                    let mut dev_atrs = Vec::new();
                    let mut touch_indices: Vec<usize> = Vec::new();
                    let total_bars = bars.len() - p1.index;

                    for k in p1.index..bars.len() {
                        let bar = &bars[k];
                        let (Some(open), Some(close), Some(high), Some(low)) =
                            (bar.open, bar.close, bar.high, bar.low)
                        else {
                            continue;
                        };
                        let Some(bounds) = geometry_bounds_at(&geometry, bar.timestamp)? else {
                            valid = false;
                            break;
                        };
                        let line_price = bounds.lower;
                        let atr_k = ctx.bar_atr(k);
                        let body_low = open.min(close);
                        let body_high = open.max(close);

                        let (body_pen, wick_pen, dist) = if is_support {
                            (line_price - body_low, line_price - low, (low - line_price).abs())
                        } else {
                            (body_high - line_price, high - line_price, (high - line_price).abs())
                        };

                        let in_anchor_span = k <= p2.index;
                        let is_tail_zone = k >= bars.len().saturating_sub(6);
                        if body_pen > atr_k * body_mult {
                            if in_anchor_span || !is_tail_zone {
                                valid = false;
                                break;
                            }
                            post_break_pierced = true;
                        } else if wick_pen > atr_k * wick_mult {
                            if in_anchor_span || !is_tail_zone {
                                valid = false;
                                break;
                            }
                            wick_pierce_count += 1;
                        } else if wick_pen > Decimal::ZERO && k != p1.index && k != p2.index {
                            wick_pierce_count += 1;
                        }

                        if !post_break_pierced && dist <= atr_k * touch_mult {
                            let ok_gap = touch_indices
                                .last()
                                .is_none_or(|&prev| k - prev >= params.min_touch_gap);
                            if ok_gap {
                                touch_indices.push(k);
                            }
                        }

                        let mid = (high + low) / Decimal::TWO;
                        let dev = ((mid - line_price).abs() / atr_k)
                            .to_string()
                            .parse::<f64>()
                            .unwrap_or(4.0);
                        dev_atrs.push(dev);
                    }

                    if !valid {
                        continue;
                    }
                    let pierce_ratio = wick_pierce_count as f64 / (total_bars.max(1) as f64);
                    if pierce_ratio > params.max_pierce_ratio {
                        continue;
                    }

                    let touch_count = touch_indices.len().max(2);
                    let touch_score = ((touch_count - 1) as f64 / 4.0).min(1.0);
                    let span_score = ((bars.len() - 1 - p1.index) as f64
                        / (0.6 * (params.trendline_lookback_bars as f64)).max(1.0))
                        .min(1.0);
                    let med_dev = median_f64(&dev_atrs);
                    let fit_score = (1.0 - med_dev / 4.0).max(0.0);
                    let last_touch_idx = touch_indices.last().copied().unwrap_or(p2.index);
                    let age_bars = (bars.len() - 1).saturating_sub(last_touch_idx);
                    let recency_score = (-(age_bars as f64) / (params.trendline_recency_tau as f64)).exp();
                    let pierce_penalty =
                        0.2 * (pierce_ratio / params.max_pierce_ratio.max(1e-6)).min(1.0);

                    let strength = clamp_strength(
                        0.35 * touch_score
                            + 0.25 * span_score
                            + 0.20 * fit_score
                            + 0.20 * recency_score
                            - pierce_penalty,
                    );
                    let ts_available = p1.ts_available.max(p2.ts_available);
                    let touch_ts: Vec<i64> = if touch_indices.is_empty() {
                        vec![p1.formed_ts, p2.formed_ts]
                    } else {
                        touch_indices.iter().map(|&idx| bars[idx].timestamp).collect()
                    };
                    let last_touch_ts = touch_ts.last().copied();
                    let tag = format!("{}:{}", p2.formed_ts, price2);

                    out.push(StructureFeature {
                        structure_id: make_structure_id(
                            &ctx.instrument_id,
                            self.detector_id(),
                            &format!("{kind}_{scale_str}_{source_str}"),
                            &ctx.timeframe,
                            p1.formed_ts,
                            price1,
                            &tag,
                        )?,
                        instrument_id: ctx.instrument_id.clone(),
                        detector_id: self.detector_id().to_owned(),
                        family: StructureFamily::Trendline,
                        category: StructureCategory::Trend,
                        kind: kind.to_owned(),
                        evidence_class: EvidenceClass::Observed,
                        timeframe: ctx.timeframe.clone(),
                        source_timeframe: Some(ctx.timeframe.clone()),
                        geometry,
                        strength,
                        status: StructureStatus::Active,
                        ts_available,
                        formed_ts: p1.formed_ts,
                        analysis_window: window.clone(),
                        touch_count: touch_count as u64,
                        touch_timestamps: touch_ts,
                        last_touch_ts,
                        volume_weight: Decimal::ZERO,
                        constituent_ids: Vec::new(),
                        method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                        parameters: params_map.clone(),
                    });
                }
            }
        }
        Ok(out)
    }
}

fn cross(ax: f64, ay: f64, bx: f64, by: f64, cx: f64, cy: f64) -> f64 {
    (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
}

fn lower_hull(points: &[(usize, f64, f64)]) -> Vec<(usize, f64, f64)> {
    let mut hull: Vec<(usize, f64, f64)> = Vec::new();
    for &pt in points {
        while hull.len() >= 2 {
            let a = hull[hull.len() - 2];
            let b = hull[hull.len() - 1];
            if cross(a.1, a.2, b.1, b.2, pt.1, pt.2) <= 0.0 {
                hull.pop();
            } else {
                break;
            }
        }
        hull.push(pt);
    }
    hull
}

fn upper_hull(points: &[(usize, f64, f64)]) -> Vec<(usize, f64, f64)> {
    let mut hull: Vec<(usize, f64, f64)> = Vec::new();
    for &pt in points {
        while hull.len() >= 2 {
            let a = hull[hull.len() - 2];
            let b = hull[hull.len() - 1];
            if cross(a.1, a.2, b.1, b.2, pt.1, pt.2) >= 0.0 {
                hull.pop();
            } else {
                break;
            }
        }
        hull.push(pt);
    }
    hull
}

fn parse_fixed8(val: f64) -> Result<Decimal, OperatorError> {
    let text = format!("{val:.8}");
    text.parse::<Decimal>()
        .map_err(|_| OperatorError::InvalidStructureParam(format!("invalid decimal {text}")))
}

pub struct ConvexEnvelopeDetector;
impl StructureDetector for ConvexEnvelopeDetector {
    fn detector_id(&self) -> &'static str {
        "convex_envelope@1"
    }
    fn version(&self) -> &'static str {
        "1"
    }
    fn families(&self) -> &'static [StructureFamily] {
        &[StructureFamily::Trendline]
    }
    fn detect(&self, ctx: &DetectionContext) -> Result<Vec<StructureFeature>, OperatorError> {
        let bars = &ctx.visible;
        let Some(window) = &ctx.analysis_window else {
            return Ok(Vec::new());
        };
        if bars.len() < 5 {
            return Ok(Vec::new());
        }

        let w = ctx.params.envelope_window.min(bars.len());
        let mut start_idx = bars.len() - w;
        let latest_island = *ctx.island_ids.last().expect("non-empty");
        if ctx.params.respect_islands {
            while start_idx < bars.len() - 1 && ctx.island_ids[start_idx] != latest_island {
                start_idx += 1;
            }
        }
        let window_bars: Vec<_> = bars[start_idx..]
            .iter()
            .filter(|b| b.low.is_some() && b.high.is_some() && b.close.is_some())
            .collect();
        if window_bars.len() < 5 {
            return Ok(Vec::new());
        }

        let scale = ctx.params.trendline_scale;
        let scale_str = match scale {
            PriceScaleMode::Linear => "LINEAR",
            PriceScaleMode::Log => "LOG",
        };
        let t0 = window_bars[0].timestamp;
        let bar_ms = ctx.bar_ms.max(1) as f64;
        let mut low_pts = Vec::with_capacity(window_bars.len());
        let mut high_pts = Vec::with_capacity(window_bars.len());
        let mut closes_y = Vec::with_capacity(window_bars.len());

        for (idx, bar) in window_bars.iter().enumerate() {
            let (low, high, close) = (bar.low.unwrap(), bar.high.unwrap(), bar.close.unwrap());
            let x = (bar.timestamp - t0) as f64 / bar_ms;
            let (yl, yh, yc) = if scale == PriceScaleMode::Log {
                if low <= Decimal::ZERO || high <= Decimal::ZERO || close <= Decimal::ZERO {
                    return Ok(Vec::new());
                }
                (
                    low.to_string().parse::<f64>().unwrap_or(1.0).ln(),
                    high.to_string().parse::<f64>().unwrap_or(1.0).ln(),
                    close.to_string().parse::<f64>().unwrap_or(1.0).ln(),
                )
            } else {
                (
                    low.to_string().parse::<f64>().unwrap_or(0.0),
                    high.to_string().parse::<f64>().unwrap_or(0.0),
                    close.to_string().parse::<f64>().unwrap_or(0.0),
                )
            };
            low_pts.push((idx, x, yl));
            high_pts.push((idx, x, yh));
            closes_y.push((x, yc));
        }

        let latest_atr = ctx.latest_atr().to_string().parse::<f64>().unwrap_or(1.0);
        let last_close = window_bars
            .last()
            .and_then(|b| b.close)
            .unwrap_or(Decimal::ONE)
            .to_string()
            .parse::<f64>()
            .unwrap_or(1.0);
        let norm_unit = if scale == PriceScaleMode::Log {
            latest_atr / last_close.max(1e-9)
        } else {
            latest_atr.max(1e-9)
        };
        let ts_available = window_bars.last().unwrap().ts_available;
        let params_map = ctx.params.as_parameters()?;
        let mut out = Vec::new();

        for (hull_pts, is_support, kind) in [
            (lower_hull(&low_pts), true, "ENVELOPE_SUPPORT"),
            (upper_hull(&high_pts), false, "ENVELOPE_RESISTANCE"),
        ] {
            if hull_pts.len() < 2 {
                continue;
            }
            let mut best_edge: Option<(usize, usize, f64)> = None;
            for h_idx in 0..(hull_pts.len() - 1) {
                let (i1, x1, y1) = hull_pts[h_idx];
                let (i2, x2, y2) = hull_pts[h_idx + 1];
                if x2 <= x1 {
                    continue;
                }
                let slope = (y2 - y1) / (x2 - x1);
                let mut sse = 0.0;
                for &(xk, yc) in &closes_y {
                    let err = yc - (y1 + slope * (xk - x1));
                    sse += err * err;
                }
                if best_edge.is_none_or(|b| sse < b.2) {
                    best_edge = Some((i1, i2, sse));
                }
            }
            let Some((i1, i2, sse)) = best_edge else {
                continue;
            };
            let (b1, b2) = (window_bars[i1], window_bars[i2]);
            let raw_p1 = if is_support { b1.low.unwrap() } else { b1.high.unwrap() };
            let raw_p2 = if is_support { b2.low.unwrap() } else { b2.high.unwrap() };

            let x1 = (b1.timestamp - t0) as f64 / bar_ms;
            let x2 = (b2.timestamp - t0) as f64 / bar_ms;
            let x_last = (window_bars.last().unwrap().timestamp - t0) as f64 / bar_ms;
            let (p_start_val, p_end_val) = if scale == PriceScaleMode::Log {
                let y1 = raw_p1.to_string().parse::<f64>().unwrap_or(1.0).ln();
                let y2 = raw_p2.to_string().parse::<f64>().unwrap_or(1.0).ln();
                let slope = (y2 - y1) / (x2 - x1);
                (
                    (y1 + slope * (0.0 - x1)).exp(),
                    (y1 + slope * (x_last - x1)).exp(),
                )
            } else {
                let y1 = raw_p1.to_string().parse::<f64>().unwrap_or(0.0);
                let y2 = raw_p2.to_string().parse::<f64>().unwrap_or(0.0);
                let slope = (y2 - y1) / (x2 - x1);
                (y1 + slope * (0.0 - x1), y1 + slope * (x_last - x1))
            };

            let anchor_start_price = parse_fixed8(p_start_val)?;
            let anchor_end_price = parse_fixed8(p_end_val)?;
            if scale == PriceScaleMode::Log
                && (anchor_start_price <= Decimal::ZERO || anchor_end_price <= Decimal::ZERO)
            {
                continue;
            }
            let rmse_norm = (sse / (closes_y.len() as f64)).sqrt() / norm_unit.max(1e-9);
            let strength = clamp_strength(0.85 - 0.08 * rmse_norm);
            let tag = format!("{}:{}:{}", b2.timestamp, raw_p2, window_bars.len());

            out.push(StructureFeature {
                structure_id: make_structure_id(
                    &ctx.instrument_id,
                    self.detector_id(),
                    &format!("{kind}_{scale_str}"),
                    &ctx.timeframe,
                    b1.timestamp,
                    raw_p1,
                    &tag,
                )?,
                instrument_id: ctx.instrument_id.clone(),
                detector_id: self.detector_id().to_owned(),
                family: StructureFamily::Trendline,
                category: StructureCategory::Trend,
                kind: kind.to_owned(),
                evidence_class: EvidenceClass::Observed,
                timeframe: ctx.timeframe.clone(),
                source_timeframe: Some(ctx.timeframe.clone()),
                geometry: StructureGeometry::SlopeLine {
                    anchor_start: StructureAnchor {
                        ts: window_bars[0].timestamp,
                        price: anchor_start_price,
                    },
                    anchor_end: StructureAnchor {
                        ts: window_bars.last().unwrap().timestamp,
                        price: anchor_end_price,
                    },
                    scale,
                    extend_right: true,
                },
                strength,
                status: StructureStatus::Active,
                ts_available,
                formed_ts: window_bars[0].timestamp,
                analysis_window: window.clone(),
                touch_count: 2,
                touch_timestamps: vec![b1.timestamp, b2.timestamp],
                last_touch_ts: Some(b2.timestamp),
                volume_weight: Decimal::ZERO,
                constituent_ids: Vec::new(),
                method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                parameters: params_map.clone(),
            });
        }
        Ok(out)
    }
}
