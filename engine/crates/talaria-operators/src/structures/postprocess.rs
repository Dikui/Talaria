//! Pluggable post-processors: lifecycle evaluation, 2D trendline dedup, KDE confluence, and budget filtering.

use std::collections::BTreeMap;

use rust_decimal::Decimal;
use talaria_contracts::bars::BarStatus;
use talaria_contracts::key_levels::EvidenceClass;
use talaria_contracts::structures::{
    PriceScaleMode, STRUCTURE_METHOD_VERSION, StructureCategory, StructureFamily, StructureFeature,
    StructureGeometry, StructureStatus,
};

use crate::error::OperatorError;
use crate::gda::GdaDecimal;
use crate::structures::context::{DetectionContext, make_structure_id};
use crate::structures::detectors::LEGACY_LEVEL_DETECTORS;
use crate::structures::geometry::{geometry_bounds_at, geometry_distance};
use crate::structures::registry::PostProcessor;

fn confluence_category_weight(cat: StructureCategory) -> f64 {
    match cat {
        StructureCategory::Structure | StructureCategory::Volume => 1.15,
        StructureCategory::Session | StructureCategory::Trend => 1.0,
        StructureCategory::Gap => 0.9,
        StructureCategory::Liquidation => 0.85,
        StructureCategory::RoundNumber => 0.75,
        StructureCategory::Confluence => 0.0,
    }
}

fn decimal_weight(part: Decimal, total: Decimal) -> Result<Decimal, OperatorError> {
    if total <= Decimal::ZERO {
        return Ok(Decimal::ZERO);
    }
    let ratio = GdaDecimal::from_decimal(part).div(&GdaDecimal::from_decimal(total))?;
    ratio.quantize_exp(-6)?.to_decimal()
}

pub struct LifecyclePostProcessor;
impl PostProcessor for LifecyclePostProcessor {
    fn processor_id(&self) -> &'static str {
        "lifecycle@1"
    }

    fn process(
        &self,
        ctx: &DetectionContext,
        features: Vec<StructureFeature>,
    ) -> Result<Vec<StructureFeature>, OperatorError> {
        let bars = &ctx.visible;
        if bars.is_empty() {
            return Ok(features);
        }
        let latest_ts = bars.last().unwrap().timestamp;
        let latest_close = bars.last().unwrap().close;
        let latest_atr = ctx.latest_atr();
        let mut updated = Vec::with_capacity(features.len());

        for mut item in features {
            if LEGACY_LEVEL_DETECTORS.contains(&item.detector_id.as_str())
                || item.family == StructureFamily::Confluence
            {
                updated.push(item);
                continue;
            }
            let is_slope = matches!(item.geometry, StructureGeometry::SlopeLine { .. });
            let touch_mult = Decimal::try_from(if is_slope {
                ctx.params.trendline_touch_atr
            } else {
                ctx.params.touch_atr
            })
            .unwrap_or(Decimal::ZERO);
            let break_mult = Decimal::try_from(if is_slope {
                ctx.params.trendline_break_atr
            } else {
                0.0
            })
            .unwrap_or(Decimal::ZERO);

            let mut touch_ts: Vec<i64> = Vec::new();
            let mut touched_vol = Decimal::ZERO;
            let mut total_vol = Decimal::ZERO;
            let mut break_dir: Option<bool> = None; // true = up, false = down
            let mut last_break_idx: Option<usize> = None;

            let is_support_only =
                matches!(item.kind.as_str(), "FVG_BULL" | "EQUAL_LOWS") || item.kind.ends_with("_SUPPORT");
            let is_resistance_only =
                matches!(item.kind.as_str(), "FVG_BEAR" | "EQUAL_HIGHS") || item.kind.ends_with("_RESISTANCE");

            for (idx, bar) in bars.iter().enumerate() {
                if bar.status == BarStatus::FeedGap {
                    continue;
                }
                let (Some(high), Some(low)) = (bar.high, bar.low) else {
                    continue;
                };
                if bar.timestamp < item.formed_ts {
                    continue;
                }
                let Some(bounds) = geometry_bounds_at(&item.geometry, bar.timestamp)? else {
                    continue;
                };
                let atr_i = ctx.bar_atr(idx);
                let tol = atr_i * touch_mult;
                let vol = bar.volume.unwrap_or(Decimal::ZERO);
                total_vol += vol;

                if high >= bounds.lower - tol && low <= bounds.upper + tol {
                    let ok_gap = !is_slope
                        || touch_ts.last().is_none_or(|&prev| {
                            bar.timestamp - prev >= ctx.bar_ms * (ctx.params.min_touch_gap as i64)
                        });
                    if ok_gap {
                        touch_ts.push(bar.timestamp);
                        touched_vol += vol;
                    }
                }

                if bar.ts_available > item.ts_available
                    && let Some(close) = bar.close
                {
                    let break_tol = atr_i * break_mult;
                    if close > bounds.upper + break_tol && !is_support_only {
                        if break_dir.is_none() {
                            break_dir = Some(true);
                        }
                        last_break_idx = Some(idx);
                    } else if close < bounds.lower - break_tol && !is_resistance_only {
                        if break_dir.is_none() {
                            break_dir = Some(false);
                        }
                        last_break_idx = Some(idx);
                    }
                }
            }

            let mut status = StructureStatus::Active;
            if let (Some(up), Some(lc)) = (break_dir, latest_close) {
                if let Some(latest_bounds) = geometry_bounds_at(&item.geometry, latest_ts)? {
                    let break_tol = latest_atr * break_mult;
                    if lc >= latest_bounds.lower && lc <= latest_bounds.upper {
                        status = StructureStatus::Active;
                    } else if up {
                        status = if lc > latest_bounds.upper + break_tol {
                            StructureStatus::Broken
                        } else {
                            StructureStatus::Flipped
                        };
                    } else {
                        status = if lc < latest_bounds.lower - break_tol {
                            StructureStatus::Broken
                        } else {
                            StructureStatus::Flipped
                        };
                    }
                    if is_slope
                        && status == StructureStatus::Active
                        && geometry_distance(lc, &latest_bounds) > latest_atr * Decimal::TEN
                    {
                        status = StructureStatus::Expired;
                    }
                } else if last_break_idx.is_some() {
                    status = StructureStatus::Broken;
                }
            }

            let merged_touches = if touch_ts.is_empty() {
                item.touch_timestamps.clone()
            } else {
                touch_ts
            };
            item.status = status;
            item.touch_count = item.touch_count.max(merged_touches.len() as u64);
            item.last_touch_ts = merged_touches.last().copied().or(item.last_touch_ts);
            item.touch_timestamps = merged_touches;
            if total_vol > Decimal::ZERO {
                item.volume_weight = decimal_weight(touched_vol, total_vol)?;
            }
            updated.push(item);
        }
        Ok(updated)
    }
}

pub struct DeduplicatorPostProcessor;
impl PostProcessor for DeduplicatorPostProcessor {
    fn processor_id(&self) -> &'static str {
        "deduplicator@1"
    }

    fn process(
        &self,
        ctx: &DetectionContext,
        features: Vec<StructureFeature>,
    ) -> Result<Vec<StructureFeature>, OperatorError> {
        let Some(last) = ctx.visible.last() else {
            return Ok(features);
        };
        let latest_ts = last.timestamp;
        let atr_now = ctx.latest_atr().to_string().parse::<f64>().unwrap_or(0.0);
        if atr_now <= 0.0 {
            return Ok(features);
        }
        let bar_ms = ctx.bar_ms.max(1) as f64;

        let mut non_trends = Vec::new();
        let mut trend_groups: BTreeMap<(String, String), Vec<StructureFeature>> = BTreeMap::new();
        for item in features {
            if item.family == StructureFamily::Trendline
                && matches!(item.geometry, StructureGeometry::SlopeLine { .. })
            {
                trend_groups
                    .entry((item.detector_id.clone(), item.kind.clone()))
                    .or_default()
                    .push(item);
            } else {
                non_trends.push(item);
            }
        }

        let mut kept_trends = Vec::new();
        let slope_tol = ctx.params.trendline_dedup_slope_atr;
        let price_tol = ctx.params.trendline_dedup_price_atr;

        for (_, mut candidates) in trend_groups {
            candidates.sort_by(|a, b| {
                b.strength
                    .total_cmp(&a.strength)
                    .then_with(|| b.ts_available.cmp(&a.ts_available))
                    .then_with(|| a.structure_id.cmp(&b.structure_id))
            });
            let mut accepted: Vec<(f64, f64)> = Vec::new();
            for item in candidates {
                let Some(bounds_now) = geometry_bounds_at(&item.geometry, latest_ts)? else {
                    continue;
                };
                let StructureGeometry::SlopeLine {
                    anchor_start,
                    anchor_end,
                    scale,
                    ..
                } = &item.geometry
                else {
                    continue;
                };
                let proj_now = bounds_now.lower.to_string().parse::<f64>().unwrap_or(0.0);
                let bars_span = ((anchor_end.ts - anchor_start.ts) as f64 / bar_ms).max(1.0);
                let slope_per_bar = if *scale == PriceScaleMode::Log {
                    let p_end = anchor_end.price.to_string().parse::<f64>().unwrap_or(1.0);
                    let p_start = anchor_start.price.to_string().parse::<f64>().unwrap_or(1.0);
                    proj_now * (p_end / p_start).ln() / bars_span
                } else {
                    (anchor_end.price - anchor_start.price)
                        .to_string()
                        .parse::<f64>()
                        .unwrap_or(0.0)
                        / bars_span
                };
                let norm_slope = slope_per_bar / atr_now;
                let norm_price = proj_now / atr_now;
                if accepted
                    .iter()
                    .any(|&(s0, p0)| (norm_slope - s0).abs() < slope_tol && (norm_price - p0).abs() < price_tol)
                {
                    continue;
                }
                accepted.push((norm_slope, norm_price));
                kept_trends.push(item);
            }
        }
        non_trends.extend(kept_trends);
        Ok(non_trends)
    }
}

pub struct ConfluencePostProcessor;
impl PostProcessor for ConfluencePostProcessor {
    fn processor_id(&self) -> &'static str {
        "confluence@1"
    }

    fn process(
        &self,
        ctx: &DetectionContext,
        features: Vec<StructureFeature>,
    ) -> Result<Vec<StructureFeature>, OperatorError> {
        let Some(window) = &ctx.analysis_window else {
            return Ok(features);
        };
        if ctx.visible.is_empty() {
            return Ok(features);
        }
        let atr_now = ctx.latest_atr().to_string().parse::<f64>().unwrap_or(0.0);
        if atr_now <= 0.0 {
            return Ok(features);
        }

        let mut sources: Vec<(&StructureFeature, Vec<f64>, f64)> = Vec::new();
        let mut min_price = f64::INFINITY;
        let mut max_price = f64::NEG_INFINITY;

        for item in &features {
            if item.status != StructureStatus::Active {
                continue;
            }
            let weight = confluence_category_weight(item.category) * item.strength;
            if weight <= 0.0 {
                continue;
            }
            match &item.geometry {
                StructureGeometry::HorizontalLine { price } => {
                    let p = price.to_string().parse::<f64>().unwrap_or(0.0);
                    min_price = min_price.min(p);
                    max_price = max_price.max(p);
                    sources.push((item, vec![p], weight));
                }
                StructureGeometry::HorizontalZone {
                    price_low,
                    price_high,
                    ..
                } => {
                    let low = price_low.to_string().parse::<f64>().unwrap_or(0.0);
                    let high = price_high.to_string().parse::<f64>().unwrap_or(0.0);
                    let pts = if high > low {
                        vec![low, (low + high) / 2.0, high]
                    } else {
                        vec![low]
                    };
                    min_price = min_price.min(low);
                    max_price = max_price.max(high);
                    sources.push((item, pts, weight));
                }
                _ => {}
            }
        }

        if sources.len() < ctx.params.confluence_min_constituents {
            return Ok(features);
        }

        let h = (ctx.params.confluence_bandwidth_atr * atr_now).max(1e-9);
        let step = (0.1 * atr_now).max(1e-9);
        let grid_start = min_price - h;
        let grid_end = max_price + h;
        let n_steps = (((grid_end - grid_start) / step) as usize + 1).clamp(3, 2000);
        let actual_step = (grid_end - grid_start) / ((n_steps - 1).max(1) as f64);

        let grid_prices: Vec<f64> = (0..n_steps)
            .map(|i| grid_start + (i as f64) * actual_step)
            .collect();
        let mut densities = Vec::with_capacity(n_steps);
        for &gp in &grid_prices {
            let mut density = 0.0;
            for (_, pts, weight) in &sources {
                let per_pt_w = weight / (pts.len() as f64);
                for &pt in pts {
                    let z = (gp - pt) / h;
                    density += per_pt_w * (-0.5 * z * z).exp();
                }
            }
            densities.push(density);
        }

        let peak_indices: Vec<usize> = (1..(n_steps - 1))
            .filter(|&i| densities[i] >= densities[i - 1] && densities[i] > densities[i + 1])
            .collect();
        if peak_indices.is_empty() {
            return Ok(features);
        }
        let max_density = peak_indices
            .iter()
            .map(|&i| densities[i])
            .fold(0.0f64, f64::max);
        if max_density <= 0.0 {
            return Ok(features);
        }

        let params_map = ctx.params.as_parameters()?;
        let mut zones = Vec::new();
        for &idx in &peak_indices {
            let peak_p = grid_prices[idx];
            let peak_d = densities[idx];
            let members: Vec<&StructureFeature> = sources
                .iter()
                .filter(|(_, pts, _)| pts.iter().any(|&pt| (pt - peak_p).abs() <= h))
                .map(|(item, _, _)| *item)
                .collect();
            if members.len() < ctx.params.confluence_min_constituents {
                continue;
            }

            let half_d = peak_d * 0.5;
            let mut left = idx;
            while left > 0 && densities[left - 1] >= half_d {
                left -= 1;
            }
            let mut right = idx;
            while right < n_steps - 1 && densities[right + 1] >= half_d {
                right += 1;
            }

            let mut low_dec = format!("{:.6}", (grid_prices[left] * 1e8).round() / 1e8)
                .parse::<Decimal>()
                .unwrap_or(Decimal::ZERO);
            let mut high_dec = format!("{:.6}", (grid_prices[right] * 1e8).round() / 1e8)
                .parse::<Decimal>()
                .unwrap_or(Decimal::ZERO);
            if low_dec > high_dec {
                core::mem::swap(&mut low_dec, &mut high_dec);
            }

            let mut member_ids: Vec<String> = members.iter().map(|m| m.structure_id.clone()).collect();
            member_ids.sort();
            let ts_available = members.iter().map(|m| m.ts_available).max().unwrap();
            let formed_ts = members.iter().map(|m| m.formed_ts).min().unwrap();
            let touch_count: u64 = members.iter().map(|m| m.touch_count).sum();
            let strength = ((0.35 + 0.55 * (peak_d / max_density)).clamp(0.05, 0.95) * 1000.0).round() / 1000.0;
            let tag = member_ids.iter().take(4).cloned().collect::<Vec<_>>().join(",");

            zones.push(StructureFeature {
                structure_id: make_structure_id(
                    &ctx.instrument_id,
                    self.processor_id(),
                    "CONFLUENCE_ZONE",
                    &ctx.timeframe,
                    formed_ts,
                    low_dec,
                    &tag,
                )?,
                instrument_id: ctx.instrument_id.clone(),
                detector_id: self.processor_id().to_owned(),
                family: StructureFamily::Confluence,
                category: StructureCategory::Confluence,
                kind: "CONFLUENCE_ZONE".to_owned(),
                evidence_class: EvidenceClass::Observed,
                timeframe: ctx.timeframe.clone(),
                source_timeframe: Some(ctx.timeframe.clone()),
                geometry: StructureGeometry::HorizontalZone {
                    price_low: low_dec,
                    price_high: high_dec,
                    start_ts: None,
                    end_ts: None,
                },
                strength,
                status: StructureStatus::Active,
                ts_available,
                formed_ts,
                analysis_window: window.clone(),
                touch_count,
                touch_timestamps: Vec::new(),
                last_touch_ts: None,
                volume_weight: Decimal::ZERO,
                constituent_ids: member_ids,
                method_version: STRUCTURE_METHOD_VERSION.to_owned(),
                parameters: params_map.clone(),
            });
        }

        let mut all = features;
        all.extend(zones);
        Ok(all)
    }
}

pub struct BudgetFilterPostProcessor;
impl PostProcessor for BudgetFilterPostProcessor {
    fn processor_id(&self) -> &'static str {
        "budget_filter@1"
    }

    fn process(
        &self,
        ctx: &DetectionContext,
        features: Vec<StructureFeature>,
    ) -> Result<Vec<StructureFeature>, OperatorError> {
        let mut levels = Vec::new();
        let mut confluences = Vec::new();
        let mut channels = Vec::new();
        let mut trend_groups: BTreeMap<(String, String), Vec<StructureFeature>> = BTreeMap::new();

        for item in features {
            match item.family {
                StructureFamily::Level | StructureFamily::Zone => levels.push(item),
                StructureFamily::Confluence => confluences.push(item),
                StructureFamily::Channel => channels.push(item),
                StructureFamily::Trendline => {
                    trend_groups
                        .entry((item.detector_id.clone(), item.kind.clone()))
                        .or_default()
                        .push(item);
                }
            }
        }

        let cmp_rank = |a: &StructureFeature, b: &StructureFeature| {
            b.strength
                .total_cmp(&a.strength)
                .then_with(|| b.ts_available.cmp(&a.ts_available))
                .then_with(|| a.structure_id.cmp(&b.structure_id))
        };

        levels.sort_by(cmp_rank);
        levels.truncate(ctx.params.max_levels);
        confluences.sort_by(cmp_rank);
        confluences.truncate(ctx.params.confluence_max_zones);
        channels.sort_by(cmp_rank);
        channels.truncate(ctx.params.max_trendlines_per_side * 2);

        let mut kept = Vec::new();
        kept.extend(levels);
        kept.extend(confluences);
        kept.extend(channels);
        for (_, mut group) in trend_groups {
            group.sort_by(cmp_rank);
            group.truncate(ctx.params.max_trendlines_per_side);
            kept.extend(group);
        }
        kept.sort_by(cmp_rank);
        Ok(kept)
    }
}
