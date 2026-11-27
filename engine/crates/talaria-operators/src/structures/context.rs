//! Shared lookahead-free DetectionContext, ATR, island segmentation, and confirmed pivot cache.

use std::cell::RefCell;
use std::collections::BTreeMap;

use rust_decimal::Decimal;
use serde::Deserialize;
use talaria_contracts::bars::{BarStatus, CompactBar};
use talaria_contracts::derivatives::DerivativePoint;
use talaria_contracts::key_levels::{SessionProfile, VwapAnchor};
use talaria_contracts::structures::{AnalysisWindow, AnchorSource, PriceScaleMode};

use crate::error::OperatorError;
use crate::gda::GdaDecimal;
use crate::key_levels::{KeyLevelParams, atr_series, bar_ms_for, normalize_gda};
use crate::sha256::sha256_hex_prefix16;

fn default_true() -> bool {
    true
}
fn default_swing_span() -> usize {
    2
}
fn default_confirm_bars() -> usize {
    2
}
fn default_atr_period() -> usize {
    14
}
fn default_pivot_atr_min() -> f64 {
    0.6
}
fn default_cluster_atr() -> f64 {
    0.4
}
fn default_touch_atr() -> f64 {
    0.2
}
fn default_gap_atr_min() -> f64 {
    0.35
}
fn default_opening_range_minutes() -> i64 {
    30
}
fn default_round_levels_per_side() -> usize {
    3
}
fn default_round_window_atr() -> f64 {
    25.0
}
fn default_volume_profile_bins() -> usize {
    24
}
fn default_value_area_ratio() -> f64 {
    0.70
}
fn default_vwap_anchor() -> VwapAnchor {
    VwapAnchor::Session
}
fn default_vwap_band_sigmas() -> String {
    "1,2".to_owned()
}
fn default_leverage_tiers() -> Vec<u32> {
    vec![5, 10, 25, 50, 100]
}
fn default_liquidation_cluster_atr() -> f64 {
    0.5
}
fn default_liquidation_min_points() -> usize {
    4
}
fn default_liquidation_max_levels() -> usize {
    8
}
fn default_liquidation_min_oi_change() -> f64 {
    0.0005
}
fn default_liquidation_max_distance_atr() -> f64 {
    12.0
}
fn default_max_levels() -> usize {
    60
}
fn default_fvg_atr_min() -> f64 {
    0.3
}
fn default_equal_level_atr() -> f64 {
    0.15
}
fn default_equal_pullback_atr() -> f64 {
    1.0
}
fn default_trendline_lookback_bars() -> usize {
    250
}
fn default_trendline_spans() -> Vec<usize> {
    vec![2, 5]
}
fn default_trendline_max_pivots() -> usize {
    40
}
fn default_anchor_source() -> AnchorSource {
    AnchorSource::Wick
}
fn default_island_gap_atr() -> f64 {
    2.0
}
fn default_min_anchor_span() -> usize {
    5
}
fn default_max_body_pierce_atr() -> f64 {
    0.05
}
fn default_max_wick_pierce_atr() -> f64 {
    0.30
}
fn default_max_pierce_ratio() -> f64 {
    0.05
}
fn default_min_touch_gap() -> usize {
    3
}
fn default_trendline_touch_atr() -> f64 {
    0.25
}
fn default_trendline_break_atr() -> f64 {
    0.25
}
fn default_trendline_recency_tau() -> usize {
    80
}
fn default_trendline_dedup_slope_atr() -> f64 {
    0.04
}
fn default_trendline_dedup_price_atr() -> f64 {
    0.50
}
fn default_max_trendlines_per_side() -> usize {
    3
}
fn default_envelope_window() -> usize {
    30
}
fn default_confluence_bandwidth_atr() -> f64 {
    0.45
}
fn default_confluence_min_constituents() -> usize {
    2
}
fn default_confluence_max_zones() -> usize {
    12
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureParams {
    #[serde(default = "default_swing_span")]
    pub swing_span: usize,
    #[serde(default = "default_confirm_bars")]
    pub confirm_bars: usize,
    #[serde(default = "default_atr_period")]
    pub atr_period: usize,
    #[serde(default = "default_pivot_atr_min")]
    pub pivot_atr_min: f64,
    #[serde(default = "default_cluster_atr")]
    pub cluster_atr: f64,
    #[serde(default = "default_touch_atr")]
    pub touch_atr: f64,
    #[serde(default = "default_gap_atr_min")]
    pub gap_atr_min: f64,
    #[serde(default = "default_opening_range_minutes")]
    pub opening_range_minutes: i64,
    #[serde(default)]
    pub round_step: Option<Decimal>,
    #[serde(default = "default_round_levels_per_side")]
    pub round_levels_per_side: usize,
    #[serde(default = "default_round_window_atr")]
    pub round_window_atr: f64,
    #[serde(default = "default_volume_profile_bins")]
    pub volume_profile_bins: usize,
    #[serde(default = "default_value_area_ratio")]
    pub value_area_ratio: f64,
    #[serde(default = "default_vwap_anchor")]
    pub vwap_anchor: VwapAnchor,
    #[serde(default)]
    pub vwap_anchor_ts: Option<i64>,
    #[serde(default = "default_vwap_band_sigmas")]
    pub vwap_band_sigmas: String,
    #[serde(default = "default_leverage_tiers")]
    pub leverage_tiers: Vec<u32>,
    #[serde(default = "default_liquidation_cluster_atr")]
    pub liquidation_cluster_atr: f64,
    #[serde(default = "default_liquidation_min_points")]
    pub liquidation_min_points: usize,
    #[serde(default = "default_liquidation_max_levels")]
    pub liquidation_max_levels: usize,
    #[serde(default = "default_liquidation_min_oi_change")]
    pub liquidation_min_oi_change: f64,
    #[serde(default = "default_liquidation_max_distance_atr")]
    pub liquidation_max_distance_atr: f64,
    #[serde(default = "default_max_levels")]
    pub max_levels: usize,

    #[serde(default = "default_fvg_atr_min")]
    pub fvg_atr_min: f64,
    #[serde(default = "default_equal_level_atr")]
    pub equal_level_atr: f64,
    #[serde(default = "default_equal_pullback_atr")]
    pub equal_pullback_atr: f64,

    #[serde(default = "default_trendline_lookback_bars")]
    pub trendline_lookback_bars: usize,
    #[serde(default = "default_trendline_spans")]
    pub trendline_spans: Vec<usize>,
    #[serde(default = "default_trendline_max_pivots")]
    pub trendline_max_pivots: usize,
    #[serde(default)]
    pub trendline_scale: PriceScaleMode,
    #[serde(default = "default_anchor_source")]
    pub trendline_anchor_source: AnchorSource,
    #[serde(default = "default_island_gap_atr")]
    pub island_gap_atr: f64,
    #[serde(default = "default_true")]
    pub respect_islands: bool,
    #[serde(default = "default_min_anchor_span")]
    pub min_anchor_span: usize,
    #[serde(default = "default_max_body_pierce_atr")]
    pub max_body_pierce_atr: f64,
    #[serde(default = "default_max_wick_pierce_atr")]
    pub max_wick_pierce_atr: f64,
    #[serde(default = "default_max_pierce_ratio")]
    pub max_pierce_ratio: f64,
    #[serde(default = "default_min_touch_gap")]
    pub min_touch_gap: usize,
    #[serde(default = "default_trendline_touch_atr")]
    pub trendline_touch_atr: f64,
    #[serde(default = "default_trendline_break_atr")]
    pub trendline_break_atr: f64,
    #[serde(default = "default_trendline_recency_tau")]
    pub trendline_recency_tau: usize,
    #[serde(default = "default_trendline_dedup_slope_atr")]
    pub trendline_dedup_slope_atr: f64,
    #[serde(default = "default_trendline_dedup_price_atr")]
    pub trendline_dedup_price_atr: f64,
    #[serde(default = "default_max_trendlines_per_side")]
    pub max_trendlines_per_side: usize,
    #[serde(default = "default_envelope_window")]
    pub envelope_window: usize,

    #[serde(default = "default_confluence_bandwidth_atr")]
    pub confluence_bandwidth_atr: f64,
    #[serde(default = "default_confluence_min_constituents")]
    pub confluence_min_constituents: usize,
    #[serde(default = "default_confluence_max_zones")]
    pub confluence_max_zones: usize,
}

impl Default for StructureParams {
    fn default() -> Self {
        Self {
            swing_span: default_swing_span(),
            confirm_bars: default_confirm_bars(),
            atr_period: default_atr_period(),
            pivot_atr_min: default_pivot_atr_min(),
            cluster_atr: default_cluster_atr(),
            touch_atr: default_touch_atr(),
            gap_atr_min: default_gap_atr_min(),
            opening_range_minutes: default_opening_range_minutes(),
            round_step: None,
            round_levels_per_side: default_round_levels_per_side(),
            round_window_atr: default_round_window_atr(),
            volume_profile_bins: default_volume_profile_bins(),
            value_area_ratio: default_value_area_ratio(),
            vwap_anchor: default_vwap_anchor(),
            vwap_anchor_ts: None,
            vwap_band_sigmas: default_vwap_band_sigmas(),
            leverage_tiers: default_leverage_tiers(),
            liquidation_cluster_atr: default_liquidation_cluster_atr(),
            liquidation_min_points: default_liquidation_min_points(),
            liquidation_max_levels: default_liquidation_max_levels(),
            liquidation_min_oi_change: default_liquidation_min_oi_change(),
            liquidation_max_distance_atr: default_liquidation_max_distance_atr(),
            max_levels: default_max_levels(),
            fvg_atr_min: default_fvg_atr_min(),
            equal_level_atr: default_equal_level_atr(),
            equal_pullback_atr: default_equal_pullback_atr(),
            trendline_lookback_bars: default_trendline_lookback_bars(),
            trendline_spans: default_trendline_spans(),
            trendline_max_pivots: default_trendline_max_pivots(),
            trendline_scale: PriceScaleMode::Linear,
            trendline_anchor_source: default_anchor_source(),
            island_gap_atr: default_island_gap_atr(),
            respect_islands: true,
            min_anchor_span: default_min_anchor_span(),
            max_body_pierce_atr: default_max_body_pierce_atr(),
            max_wick_pierce_atr: default_max_wick_pierce_atr(),
            max_pierce_ratio: default_max_pierce_ratio(),
            min_touch_gap: default_min_touch_gap(),
            trendline_touch_atr: default_trendline_touch_atr(),
            trendline_break_atr: default_trendline_break_atr(),
            trendline_recency_tau: default_trendline_recency_tau(),
            trendline_dedup_slope_atr: default_trendline_dedup_slope_atr(),
            trendline_dedup_price_atr: default_trendline_dedup_price_atr(),
            max_trendlines_per_side: default_max_trendlines_per_side(),
            envelope_window: default_envelope_window(),
            confluence_bandwidth_atr: default_confluence_bandwidth_atr(),
            confluence_min_constituents: default_confluence_min_constituents(),
            confluence_max_zones: default_confluence_max_zones(),
        }
    }
}

fn fmt_g(v: f64) -> String {
    if v == 0.0 {
        return "0".to_owned();
    }
    let s = format!("{v}");
    s
}

fn to_dec(v: f64) -> Decimal {
    Decimal::try_from(v).unwrap_or(Decimal::ZERO)
}

impl StructureParams {
    pub fn to_key_level_params(&self) -> KeyLevelParams {
        KeyLevelParams {
            swing_span: self.swing_span,
            confirm_bars: self.confirm_bars,
            atr_period: self.atr_period,
            pivot_atr_min: to_dec(self.pivot_atr_min),
            cluster_atr: to_dec(self.cluster_atr),
            touch_atr: to_dec(self.touch_atr),
            gap_atr_min: to_dec(self.gap_atr_min),
            opening_range_minutes: self.opening_range_minutes as u32,
            round_step: self.round_step,
            round_levels_per_side: self.round_levels_per_side as i32,
            round_window_atr: to_dec(self.round_window_atr),
            volume_profile_bins: self.volume_profile_bins,
            value_area_ratio: to_dec(self.value_area_ratio),
            vwap_anchor: self.vwap_anchor,
            vwap_anchor_ts: self.vwap_anchor_ts,
            vwap_band_sigmas: self.vwap_band_sigmas.clone(),
            leverage_tiers: self.leverage_tiers.clone(),
            liquidation_cluster_atr: to_dec(self.liquidation_cluster_atr),
            liquidation_min_points: self.liquidation_min_points,
            liquidation_max_levels: self.liquidation_max_levels,
            liquidation_min_oi_change: to_dec(self.liquidation_min_oi_change),
            liquidation_max_distance_atr: to_dec(self.liquidation_max_distance_atr),
            max_levels: self.max_levels,
        }
    }

    pub fn validate(&self) -> Result<(), OperatorError> {
        self.to_key_level_params().validate()?;
        if self.fvg_atr_min <= 0.0
            || self.equal_level_atr <= 0.0
            || self.equal_pullback_atr <= 0.0
        {
            return Err(OperatorError::InvalidStructureParam(
                "SMC ATR thresholds must be positive".to_owned(),
            ));
        }
        if self.trendline_lookback_bars < 10 || self.trendline_max_pivots < 2 {
            return Err(OperatorError::InvalidStructureParam(
                "trendline_lookback_bars >= 10 and trendline_max_pivots >= 2".to_owned(),
            ));
        }
        if self.trendline_spans.is_empty() || self.trendline_spans.iter().any(|&s| s < 1) {
            return Err(OperatorError::InvalidStructureParam(
                "trendline_spans must be non-empty positive integers".to_owned(),
            ));
        }
        if self.min_anchor_span < 1 || self.min_touch_gap < 1 || self.trendline_recency_tau < 1 {
            return Err(OperatorError::InvalidStructureParam(
                "min_anchor_span / min_touch_gap / trendline_recency_tau >= 1".to_owned(),
            ));
        }
        if self.max_body_pierce_atr < 0.0 || self.max_wick_pierce_atr < 0.0 {
            return Err(OperatorError::InvalidStructureParam(
                "pierce tolerances cannot be negative".to_owned(),
            ));
        }
        if !(0.0..=1.0).contains(&self.max_pierce_ratio) {
            return Err(OperatorError::InvalidStructureParam(
                "max_pierce_ratio must be in [0, 1]".to_owned(),
            ));
        }
        if self.max_trendlines_per_side < 1 || self.envelope_window < 5 {
            return Err(OperatorError::InvalidStructureParam(
                "max_trendlines_per_side >= 1 and envelope_window >= 5".to_owned(),
            ));
        }
        if self.confluence_bandwidth_atr <= 0.0 || self.confluence_min_constituents < 2 {
            return Err(OperatorError::InvalidStructureParam(
                "confluence_bandwidth_atr > 0 and confluence_min_constituents >= 2".to_owned(),
            ));
        }
        if self.confluence_max_zones < 1 {
            return Err(OperatorError::InvalidStructureParam(
                "confluence_max_zones >= 1".to_owned(),
            ));
        }
        Ok(())
    }

    pub fn as_parameters(&self) -> Result<BTreeMap<String, String>, OperatorError> {
        let mut map = self.to_key_level_params().as_parameters()?;
        map.insert("fvg_atr_min".to_owned(), fmt_g(self.fvg_atr_min));
        map.insert("equal_level_atr".to_owned(), fmt_g(self.equal_level_atr));
        map.insert("equal_pullback_atr".to_owned(), fmt_g(self.equal_pullback_atr));
        map.insert(
            "trendline_lookback_bars".to_owned(),
            self.trendline_lookback_bars.to_string(),
        );
        let spans: Vec<String> = self.trendline_spans.iter().map(ToString::to_string).collect();
        map.insert("trendline_spans".to_owned(), spans.join(","));
        map.insert(
            "trendline_max_pivots".to_owned(),
            self.trendline_max_pivots.to_string(),
        );
        map.insert(
            "trendline_scale".to_owned(),
            match self.trendline_scale {
                PriceScaleMode::Linear => "LINEAR".to_owned(),
                PriceScaleMode::Log => "LOG".to_owned(),
            },
        );
        map.insert(
            "trendline_anchor_source".to_owned(),
            match self.trendline_anchor_source {
                AnchorSource::Wick => "WICK".to_owned(),
                AnchorSource::Body => "BODY".to_owned(),
            },
        );
        map.insert("island_gap_atr".to_owned(), fmt_g(self.island_gap_atr));
        map.insert(
            "respect_islands".to_owned(),
            if self.respect_islands { "true" } else { "false" }.to_owned(),
        );
        map.insert("min_anchor_span".to_owned(), self.min_anchor_span.to_string());
        map.insert(
            "max_body_pierce_atr".to_owned(),
            fmt_g(self.max_body_pierce_atr),
        );
        map.insert(
            "max_wick_pierce_atr".to_owned(),
            fmt_g(self.max_wick_pierce_atr),
        );
        map.insert("max_pierce_ratio".to_owned(), fmt_g(self.max_pierce_ratio));
        map.insert("min_touch_gap".to_owned(), self.min_touch_gap.to_string());
        map.insert(
            "trendline_touch_atr".to_owned(),
            fmt_g(self.trendline_touch_atr),
        );
        map.insert(
            "trendline_break_atr".to_owned(),
            fmt_g(self.trendline_break_atr),
        );
        map.insert(
            "trendline_recency_tau".to_owned(),
            self.trendline_recency_tau.to_string(),
        );
        map.insert(
            "trendline_dedup_slope_atr".to_owned(),
            fmt_g(self.trendline_dedup_slope_atr),
        );
        map.insert(
            "trendline_dedup_price_atr".to_owned(),
            fmt_g(self.trendline_dedup_price_atr),
        );
        map.insert(
            "max_trendlines_per_side".to_owned(),
            self.max_trendlines_per_side.to_string(),
        );
        map.insert("envelope_window".to_owned(), self.envelope_window.to_string());
        map.insert(
            "confluence_bandwidth_atr".to_owned(),
            fmt_g(self.confluence_bandwidth_atr),
        );
        map.insert(
            "confluence_min_constituents".to_owned(),
            self.confluence_min_constituents.to_string(),
        );
        map.insert(
            "confluence_max_zones".to_owned(),
            self.confluence_max_zones.to_string(),
        );
        Ok(map)
    }
}

pub fn make_structure_id(
    instrument_id: &str,
    detector_id: &str,
    kind: &str,
    timeframe: &str,
    anchor_ts: i64,
    anchor_price: Decimal,
    tag: &str,
) -> Result<String, OperatorError> {
    let price_str = normalize_gda(&GdaDecimal::from_decimal(anchor_price))?;
    let payload = format!(
        "{instrument_id}|{detector_id}|{kind}|{timeframe}|{anchor_ts}|{price_str}|{tag}"
    );
    Ok(format!("st_{}", sha256_hex_prefix16(payload.as_bytes())))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PivotSide {
    High,
    Low,
}

#[derive(Debug, Clone)]
pub struct ConfirmedPivot {
    pub side: PivotSide,
    pub price: Decimal,
    pub body_price: Decimal,
    pub index: usize,
    pub atr: Option<GdaDecimal>,
    pub prominence: Decimal,
    pub ts_available: i64,
    pub formed_ts: i64,
    pub island_id: usize,
    pub span: usize,
}

fn compute_island_ids(
    visible_with_gaps: &[CompactBar],
    atr_visible: &[Option<GdaDecimal>],
    island_gap_atr: f64,
) -> Vec<usize> {
    let mut island_ids = Vec::with_capacity(atr_visible.len());
    let mut island_id = 0usize;
    let mut seen_gap = false;
    let mut prev_close: Option<Decimal> = None;
    let mut prev_atr: Option<GdaDecimal> = None;
    let mut visible_idx = 0usize;
    let gap_mult = GdaDecimal::from_decimal(
        Decimal::try_from(island_gap_atr).unwrap_or_else(|_| Decimal::new(2, 0)),
    );

    for bar in visible_with_gaps {
        if bar.status == BarStatus::FeedGap {
            if !island_ids.is_empty() {
                seen_gap = true;
            }
            prev_close = None;
            prev_atr = None;
            continue;
        }
        if seen_gap {
            island_id += 1;
            seen_gap = false;
        } else if let (Some(pc), Some(open), Some(pa)) = (prev_close, bar.open, &prev_atr)
            && pa.is_positive()
        {
            let diff = GdaDecimal::from_decimal((open - pc).abs());
            let thresh = gap_mult.mul(pa);
            if diff.cmp_numeric(&thresh) != core::cmp::Ordering::Less {
                island_id += 1;
            }
        }
        island_ids.push(island_id);
        prev_close = bar.close;
        prev_atr = atr_visible.get(visible_idx).cloned().flatten();
        visible_idx += 1;
    }
    island_ids
}

pub struct DetectionContext {
    pub instrument_id: String,
    pub timeframe: String,
    pub cutoff_ts: i64,
    pub bar_ms: i64,
    pub params: StructureParams,
    pub session_profile: SessionProfile,
    pub visible_with_gaps: Vec<CompactBar>,
    pub visible: Vec<CompactBar>,
    pub atr: Vec<Option<GdaDecimal>>,
    pub island_ids: Vec<usize>,
    pub derivative_points: Vec<DerivativePoint>,
    pub analysis_window: Option<AnalysisWindow>,
    pivot_cache: RefCell<BTreeMap<(usize, usize, String), Vec<ConfirmedPivot>>>,
}

impl DetectionContext {
    pub fn build(
        instrument_id: &str,
        timeframe: &str,
        bars: &[CompactBar],
        cutoff_ts: i64,
        session_profile: Option<&SessionProfile>,
        params: Option<&StructureParams>,
        derivative_points: &[DerivativePoint],
    ) -> Result<Self, OperatorError> {
        let resolved_params = params.cloned().unwrap_or_default();
        resolved_params.validate()?;
        let profile = session_profile.cloned().unwrap_or_else(|| SessionProfile {
            timezone: "UTC".to_owned(),
            day_start: "00:00".to_owned(),
            sessions: Vec::new(),
        });

        let mut visible_with_gaps: Vec<CompactBar> = bars
            .iter()
            .filter(|b| b.ts_available <= cutoff_ts)
            .cloned()
            .collect();
        visible_with_gaps.sort_by_key(|b| b.timestamp);

        let visible: Vec<CompactBar> = visible_with_gaps
            .iter()
            .filter(|b| b.status != BarStatus::FeedGap)
            .cloned()
            .collect();

        let atr = if visible.is_empty() {
            Vec::new()
        } else {
            atr_series(&visible, resolved_params.atr_period)?
        };
        let island_ids = if visible.is_empty() {
            Vec::new()
        } else {
            compute_island_ids(&visible_with_gaps, &atr, resolved_params.island_gap_atr)
        };
        let analysis_window = if visible.is_empty() {
            None
        } else {
            Some(AnalysisWindow {
                from_ts: visible.first().expect("non-empty").timestamp,
                to_ts: visible.last().expect("non-empty").timestamp,
            })
        };
        let filtered_derivs = derivative_points
            .iter()
            .filter(|p| p.ts_available <= cutoff_ts)
            .cloned()
            .collect();

        Ok(Self {
            instrument_id: instrument_id.to_owned(),
            timeframe: timeframe.to_owned(),
            cutoff_ts,
            bar_ms: bar_ms_for(timeframe),
            params: resolved_params,
            session_profile: profile,
            visible_with_gaps,
            visible,
            atr,
            island_ids,
            derivative_points: filtered_derivs,
            analysis_window,
            pivot_cache: RefCell::new(BTreeMap::new()),
        })
    }

    pub fn latest_atr(&self) -> Decimal {
        for item in self.atr.iter().rev() {
            if let Some(v) = item
                && v.is_positive()
                && let Ok(d) = v.to_decimal()
            {
                return d;
            }
        }
        for bar in self.visible.iter().rev() {
            if let Some(close) = bar.close
                && close > Decimal::ZERO
            {
                let scaled = close * Decimal::new(5, 3);
                let min_v = Decimal::new(1, 6);
                return scaled.max(min_v);
            }
        }
        Decimal::ONE
    }

    pub fn bar_atr(&self, index: usize) -> Decimal {
        if let Some(Some(v)) = self.atr.get(index)
            && v.is_positive()
            && let Ok(d) = v.to_decimal()
        {
            return d;
        }
        if let Some(bar) = self.visible.get(index)
            && let Some(close) = bar.close
            && close > Decimal::ZERO
        {
            let scaled = close * Decimal::new(5, 3);
            let min_v = Decimal::new(1, 6);
            return scaled.max(min_v);
        }
        self.latest_atr()
    }

    pub fn confirmed_pivots(
        &self,
        span: Option<usize>,
        confirm_bars: Option<usize>,
        pivot_atr_min: Option<f64>,
    ) -> Result<Vec<ConfirmedPivot>, OperatorError> {
        let s = span.unwrap_or(self.params.swing_span);
        let c = confirm_bars.unwrap_or(self.params.confirm_bars);
        let p_min = pivot_atr_min.unwrap_or(self.params.pivot_atr_min);
        let key = (s, c, format!("{p_min}"));
        if let Some(cached) = self.pivot_cache.borrow().get(&key) {
            return Ok(cached.clone());
        }

        let bars = &self.visible;
        if bars.len() < 2 * s + 1 {
            self.pivot_cache.borrow_mut().insert(key, Vec::new());
            return Ok(Vec::new());
        }

        let confirm_offset = s.max(c);
        let mult = GdaDecimal::from_decimal(
            Decimal::try_from(p_min).unwrap_or(Decimal::ZERO),
        );
        let mut pivots = Vec::new();

        for index in s..(bars.len() - s) {
            let confirm_index = index + confirm_offset;
            if confirm_index >= bars.len() {
                continue;
            }
            let confirm_bar = &bars[confirm_index];
            if confirm_bar.ts_available > self.cutoff_ts {
                continue;
            }
            let bar = &bars[index];
            let (Some(open), Some(high), Some(low), Some(close)) =
                (bar.open, bar.high, bar.low, bar.close)
            else {
                continue;
            };
            let local_atr = self.atr.get(index).cloned().flatten();
            let threshold = local_atr
                .as_ref()
                .map(|a| a.mul(&mult))
                .unwrap_or_else(GdaDecimal::zero);

            let window_high = bars[(index - s)..=(index + s)]
                .iter()
                .filter_map(|b| b.high)
                .max();
            let window_low = bars[(index - s)..=(index + s)]
                .iter()
                .filter_map(|b| b.low)
                .min();

            if window_high == Some(high)
                && let Some(neigh_max) = bars[(index - s)..index]
                    .iter()
                    .chain(bars[(index + 1)..=(index + s)].iter())
                    .filter_map(|b| b.high)
                    .max()
            {
                let prom = high - neigh_max;
                if GdaDecimal::from_decimal(prom).cmp_numeric(&threshold)
                    != core::cmp::Ordering::Less
                {
                    pivots.push(ConfirmedPivot {
                        side: PivotSide::High,
                        price: high,
                        body_price: open.max(close),
                        index,
                        atr: local_atr.clone(),
                        prominence: prom,
                        ts_available: confirm_bar.ts_available,
                        formed_ts: bar.timestamp,
                        island_id: self.island_ids[index],
                        span: s,
                    });
                }
            }

            if window_low == Some(low)
                && let Some(neigh_min) = bars[(index - s)..index]
                    .iter()
                    .chain(bars[(index + 1)..=(index + s)].iter())
                    .filter_map(|b| b.low)
                    .min()
            {
                let prom = neigh_min - low;
                if GdaDecimal::from_decimal(prom).cmp_numeric(&threshold)
                    != core::cmp::Ordering::Less
                {
                    pivots.push(ConfirmedPivot {
                        side: PivotSide::Low,
                        price: low,
                        body_price: open.min(close),
                        index,
                        atr: local_atr,
                        prominence: prom,
                        ts_available: confirm_bar.ts_available,
                        formed_ts: bar.timestamp,
                        island_id: self.island_ids[index],
                        span: s,
                    });
                }
            }
        }

        self.pivot_cache.borrow_mut().insert(key, pivots.clone());
        Ok(pivots)
    }
}
