//! Deterministic, lookahead-free Key Level engine matching Python `foundation/key_levels`.
//!
//! All prices, tolerances, ATRs, volume profiles, VWAPs, and liquidation buckets are computed
//! using exact `rust_decimal::Decimal` / IBM GDA `prec = 28` arithmetic (zero `f64`).
//! Only the heuristic UI ranking field `KeyLevel.strength: f64` reproduces Python's IEEE-754
//! `float` scoring and CPython `round(x, 3)` rounding so all golden vectors match bit-for-bit.

use std::collections::{BTreeMap, BTreeSet};
use std::str::FromStr;

use rust_decimal::Decimal;
use serde::{Deserialize, Deserializer, Serialize};
use talaria_contracts::{
    BarStatus, CompactBar, DerivativeKind, DerivativePoint, EvidenceClass, KEY_LEVEL_METHOD_VERSION,
    KeyLevel, KeyLevelKind, KeyLevelSet, KeyLevelStatus, SessionProfile, VwapAnchor,
    format_normalized_decimal_str, kind_category, timeframe_ms,
};

use crate::error::OperatorError;
use crate::gda::GdaDecimal;
use crate::operators::latest_session_vwap;
use crate::sha256::sha256_hex_prefix16;
use crate::tz::{
    TimeZoneRule, day_start_ms, latest_session_window, month_start_ms, previous_day_start_ms,
    week_start_ms,
};

const SEC_MS: i64 = 1_000;
const DAY_MS: i64 = 86_400_000;
const QUANTUM_EXP: i32 = -6;
const MAX_STRENGTH: f64 = 0.95;

fn deserialize_decimal_any<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
    let value = serde_json::Value::deserialize(deserializer)?;
    value_to_decimal(&value).map_err(serde::de::Error::custom)
}

fn deserialize_decimal_opt_any<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<Option<Decimal>, D::Error> {
    let value = Option::<serde_json::Value>::deserialize(deserializer)?;
    match value {
        None => Ok(None),
        Some(serde_json::Value::Null) => Ok(None),
        Some(v) => value_to_decimal(&v).map(Some).map_err(serde::de::Error::custom),
    }
}

fn deserialize_vwap_anchor<'de, D: Deserializer<'de>>(
    deserializer: D,
) -> Result<VwapAnchor, D::Error> {
    let s = String::deserialize(deserializer)?;
    match s.to_ascii_uppercase().as_str() {
        "SNAPSHOT" => Ok(VwapAnchor::Snapshot),
        "SESSION" => Ok(VwapAnchor::Session),
        "TIMESTAMP" => Ok(VwapAnchor::Timestamp),
        _ => Err(serde::de::Error::custom(format!("invalid vwap_anchor: {s}"))),
    }
}

fn value_to_decimal(v: &serde_json::Value) -> Result<Decimal, String> {
    match v {
        serde_json::Value::String(s) => Decimal::from_str(s).map_err(|e| e.to_string()),
        serde_json::Value::Number(n) => Decimal::from_str(&n.to_string()).map_err(|e| e.to_string()),
        other => Err(format!("expected decimal string or number, got {other}")),
    }
}

/// Pure-function Key Level parameters recorded in every emitted `KeyLevel.parameters`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct KeyLevelParams {
    pub swing_span: usize,
    pub confirm_bars: usize,
    pub atr_period: usize,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub pivot_atr_min: Decimal,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub cluster_atr: Decimal,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub touch_atr: Decimal,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub gap_atr_min: Decimal,
    pub opening_range_minutes: u32,
    #[serde(deserialize_with = "deserialize_decimal_opt_any")]
    pub round_step: Option<Decimal>,
    pub round_levels_per_side: i32,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub round_window_atr: Decimal,
    pub volume_profile_bins: usize,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub value_area_ratio: Decimal,
    #[serde(deserialize_with = "deserialize_vwap_anchor")]
    pub vwap_anchor: VwapAnchor,
    pub vwap_anchor_ts: Option<i64>,
    pub vwap_band_sigmas: String,
    pub leverage_tiers: Vec<u32>,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub liquidation_cluster_atr: Decimal,
    pub liquidation_min_points: usize,
    pub liquidation_max_levels: usize,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub liquidation_min_oi_change: Decimal,
    #[serde(deserialize_with = "deserialize_decimal_any")]
    pub liquidation_max_distance_atr: Decimal,
    pub max_levels: usize,
}

impl Default for KeyLevelParams {
    fn default() -> Self {
        Self {
            swing_span: 2,
            confirm_bars: 2,
            atr_period: 14,
            pivot_atr_min: Decimal::new(6, 1),
            cluster_atr: Decimal::new(4, 1),
            touch_atr: Decimal::new(2, 1),
            gap_atr_min: Decimal::new(35, 2),
            opening_range_minutes: 30,
            round_step: None,
            round_levels_per_side: 3,
            round_window_atr: Decimal::from(25),
            volume_profile_bins: 24,
            value_area_ratio: Decimal::new(7, 1),
            vwap_anchor: VwapAnchor::Session,
            vwap_anchor_ts: None,
            vwap_band_sigmas: "1,2".to_owned(),
            leverage_tiers: vec![5, 10, 25, 50, 100],
            liquidation_cluster_atr: Decimal::new(5, 1),
            liquidation_min_points: 4,
            liquidation_max_levels: 8,
            liquidation_min_oi_change: Decimal::new(5, 4),
            liquidation_max_distance_atr: Decimal::from(12),
            max_levels: 60,
        }
    }
}

impl KeyLevelParams {
    pub fn validate(&self) -> Result<(), OperatorError> {
        if self.swing_span < 1 || self.confirm_bars < 1 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "swing_span and confirm_bars must be >= 1".to_owned(),
            ));
        }
        if self.atr_period < 2 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "atr_period must be >= 2".to_owned(),
            ));
        }
        if self.opening_range_minutes < 1 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "opening_range_minutes must be >= 1".to_owned(),
            ));
        }
        if self.round_levels_per_side < 0 || self.max_levels < 1 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "round_levels_per_side must be >= 0 and max_levels >= 1".to_owned(),
            ));
        }
        if !(2..=512).contains(&self.volume_profile_bins) {
            return Err(OperatorError::InvalidKeyLevelParam(
                "volume_profile_bins must be in [2, 512]".to_owned(),
            ));
        }
        if self.value_area_ratio <= Decimal::ZERO || self.value_area_ratio > Decimal::ONE {
            return Err(OperatorError::InvalidKeyLevelParam(
                "value_area_ratio must be in (0, 1]".to_owned(),
            ));
        }
        if self.vwap_anchor == VwapAnchor::Timestamp && self.vwap_anchor_ts.is_none() {
            return Err(OperatorError::InvalidKeyLevelParam(
                "vwap_anchor=TIMESTAMP requires vwap_anchor_ts".to_owned(),
            ));
        }
        if let Some(ts) = self.vwap_anchor_ts
            && ts < 0
        {
            return Err(OperatorError::InvalidKeyLevelParam(
                "vwap_anchor_ts must be >= 0".to_owned(),
            ));
        }
        let sigmas = parse_sigmas(&self.vwap_band_sigmas)?;
        if sigmas.is_empty() || sigmas.iter().any(|s| *s <= Decimal::ZERO) {
            return Err(OperatorError::InvalidKeyLevelParam(
                "vwap_band_sigmas must be a non-empty list of positive numbers".to_owned(),
            ));
        }
        if !(1..=6).contains(&self.leverage_tiers.len())
            || self.leverage_tiers.iter().any(|&t| !(1..=200).contains(&t))
        {
            return Err(OperatorError::InvalidKeyLevelParam(
                "leverage_tiers must contain 1..6 integers in 1..200".to_owned(),
            ));
        }
        if self.liquidation_min_points < 2 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "liquidation_min_points must be >= 2".to_owned(),
            ));
        }
        if self.liquidation_max_levels < 1 {
            return Err(OperatorError::InvalidKeyLevelParam(
                "liquidation_max_levels must be >= 1".to_owned(),
            ));
        }
        if self.liquidation_min_oi_change < Decimal::ZERO {
            return Err(OperatorError::InvalidKeyLevelParam(
                "liquidation_min_oi_change must be >= 0".to_owned(),
            ));
        }
        if self.liquidation_max_distance_atr <= Decimal::ZERO {
            return Err(OperatorError::InvalidKeyLevelParam(
                "liquidation_max_distance_atr must be > 0".to_owned(),
            ));
        }
        Ok(())
    }

    pub fn as_parameters(&self) -> Result<BTreeMap<String, String>, OperatorError> {
        let sigmas = parse_sigmas(&self.vwap_band_sigmas)?;
        let sigmas_str = sigmas
            .iter()
            .map(|s| format_normalized_decimal_str(*s))
            .collect::<Vec<_>>()
            .join(",");
        let tiers_str = self
            .leverage_tiers
            .iter()
            .map(|t| t.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let anchor_str = match self.vwap_anchor {
            VwapAnchor::Snapshot => "snapshot",
            VwapAnchor::Session => "session",
            VwapAnchor::Timestamp => "timestamp",
        };
        let mut map = BTreeMap::new();
        map.insert("swing_span".to_owned(), self.swing_span.to_string());
        map.insert("confirm_bars".to_owned(), self.confirm_bars.to_string());
        map.insert("atr_period".to_owned(), self.atr_period.to_string());
        map.insert(
            "pivot_atr_min".to_owned(),
            format_normalized_decimal_str(self.pivot_atr_min),
        );
        map.insert(
            "cluster_atr".to_owned(),
            format_normalized_decimal_str(self.cluster_atr),
        );
        map.insert(
            "touch_atr".to_owned(),
            format_normalized_decimal_str(self.touch_atr),
        );
        map.insert(
            "gap_atr_min".to_owned(),
            format_normalized_decimal_str(self.gap_atr_min),
        );
        map.insert(
            "opening_range_minutes".to_owned(),
            self.opening_range_minutes.to_string(),
        );
        map.insert(
            "round_step".to_owned(),
            match self.round_step {
                None => "auto".to_owned(),
                Some(s) => s.to_string(),
            },
        );
        map.insert(
            "round_levels_per_side".to_owned(),
            self.round_levels_per_side.to_string(),
        );
        map.insert(
            "round_window_atr".to_owned(),
            format_normalized_decimal_str(self.round_window_atr),
        );
        map.insert(
            "volume_profile_bins".to_owned(),
            self.volume_profile_bins.to_string(),
        );
        map.insert(
            "value_area_ratio".to_owned(),
            format_normalized_decimal_str(self.value_area_ratio),
        );
        map.insert("vwap_anchor".to_owned(), anchor_str.to_owned());
        map.insert(
            "vwap_anchor_ts".to_owned(),
            match self.vwap_anchor_ts {
                None => "auto".to_owned(),
                Some(ts) => ts.to_string(),
            },
        );
        map.insert("vwap_band_sigmas".to_owned(), sigmas_str);
        map.insert("leverage_tiers".to_owned(), tiers_str);
        map.insert(
            "liquidation_cluster_atr".to_owned(),
            format_normalized_decimal_str(self.liquidation_cluster_atr),
        );
        map.insert(
            "liquidation_min_points".to_owned(),
            self.liquidation_min_points.to_string(),
        );
        map.insert(
            "liquidation_max_levels".to_owned(),
            self.liquidation_max_levels.to_string(),
        );
        map.insert(
            "liquidation_min_oi_change".to_owned(),
            format_normalized_decimal_str(self.liquidation_min_oi_change),
        );
        map.insert(
            "liquidation_max_distance_atr".to_owned(),
            format_normalized_decimal_str(self.liquidation_max_distance_atr),
        );
        map.insert("max_levels".to_owned(), self.max_levels.to_string());
        Ok(map)
    }
}

fn parse_sigmas(value: &str) -> Result<Vec<Decimal>, OperatorError> {
    let mut out = Vec::new();
    for part in value.split(',') {
        let trimmed = part.trim();
        if trimmed.is_empty() {
            continue;
        }
        let d = Decimal::from_str(trimmed).map_err(|err| {
            OperatorError::InvalidKeyLevelParam(format!("invalid vwap_band_sigmas '{trimmed}': {err}"))
        })?;
        out.push(d);
    }
    Ok(out)
}

/// Complete output of a Key Level computation, including notes explaining empty estimated sets.
#[derive(Debug, Clone, PartialEq)]
pub struct KeyLevelComputation {
    pub levels: Vec<KeyLevel>,
    pub notes: Vec<String>,
}

#[derive(Debug, Clone)]
struct Pivot {
    side: KeyLevelKind,
    price: GdaDecimal,
    index: usize,
    atr: Option<GdaDecimal>,
    prominence: GdaDecimal,
    ts_available: i64,
    formed_ts: i64,
}

#[derive(Debug, Clone)]
struct TouchStats {
    count: u64,
    last_ts: Option<i64>,
    volume_weight: GdaDecimal,
}

fn kind_str(kind: KeyLevelKind) -> &'static str {
    match kind {
        KeyLevelKind::SwingHigh => "SWING_HIGH",
        KeyLevelKind::SwingLow => "SWING_LOW",
        KeyLevelKind::PrevDayHigh => "PREV_DAY_HIGH",
        KeyLevelKind::PrevDayLow => "PREV_DAY_LOW",
        KeyLevelKind::PrevDayClose => "PREV_DAY_CLOSE",
        KeyLevelKind::WeekOpen => "WEEK_OPEN",
        KeyLevelKind::MonthOpen => "MONTH_OPEN",
        KeyLevelKind::OpeningRange => "OPENING_RANGE",
        KeyLevelKind::SessionHigh => "SESSION_HIGH",
        KeyLevelKind::SessionLow => "SESSION_LOW",
        KeyLevelKind::Gap => "GAP",
        KeyLevelKind::RoundNumber => "ROUND_NUMBER",
        KeyLevelKind::Poc => "POC",
        KeyLevelKind::Vah => "VAH",
        KeyLevelKind::Val => "VAL",
        KeyLevelKind::AnchoredVwap => "ANCHORED_VWAP",
        KeyLevelKind::LiquidationZone => "LIQUIDATION_ZONE",
    }
}

fn normalize_gda(price: &GdaDecimal) -> Result<String, OperatorError> {
    let d = price.to_decimal()?;
    Ok(format_normalized_decimal_str(d))
}

fn level_id(
    instrument_id: &str,
    kind: KeyLevelKind,
    timeframe: &str,
    anchor_ts: i64,
    anchor_price: &GdaDecimal,
    tag: &str,
) -> Result<String, OperatorError> {
    let norm = normalize_gda(anchor_price)?;
    let payload = format!(
        "{}|{}|{}|{}|{}|{}",
        instrument_id,
        kind_str(kind),
        timeframe,
        anchor_ts,
        norm,
        tag
    );
    Ok(format!("kl_{}", sha256_hex_prefix16(payload.as_bytes())))
}

/// CPython-compatible `round(max(0.05, min(0.95, value)), 3)` on IEEE-754 `f64` using exact
/// binary64 value comparison (so exact ties like `.57450000000000001066` match CPython `0.575`).
fn round_strength(value: f64) -> f64 {
    let clamped = value.clamp(0.05, MAX_STRENGTH);
    let bits = clamped.to_bits();
    let exp_bits = ((bits >> 52) & 0x7ff) as i32;
    let mant = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    let shift = exp_bits - 1023 - 52;
    debug_assert!(shift < 0 && (-shift) <= 64);
    let den: u128 = 1u128 << (-shift);
    let num: u128 = (mant as u128) * 1000;
    let mut q = (num / den) as u64;
    let rem = num % den;
    let twice_rem = rem * 2;
    if twice_rem > den || (twice_rem == den && (q & 1) == 1) {
        q += 1;
    }
    (q as f64) / 1000.0
}

fn gda_to_f64(val: &GdaDecimal) -> Result<f64, OperatorError> {
    let dec = val.to_decimal()?;
    dec.to_string()
        .parse::<f64>()
        .map_err(|_| OperatorError::DecimalOverflow("f64 parse"))
}

fn decimal_volume_weight(part: &GdaDecimal, total: &GdaDecimal) -> Result<GdaDecimal, OperatorError> {
    if !total.is_positive() {
        return Ok(GdaDecimal::zero());
    }
    part.div(total)?.quantize_exp(QUANTUM_EXP)
}

fn zone_tolerance(
    atr: Option<&GdaDecimal>,
    price: &GdaDecimal,
    ratio: Decimal,
) -> GdaDecimal {
    if let Some(a) = atr
        && a.is_positive()
    {
        return a.mul(&GdaDecimal::from_decimal(ratio));
    }
    let min_tol = GdaDecimal::from_decimal(Decimal::new(1, 6)); // 0.000001
    let scaled = price.mul(&GdaDecimal::from_decimal(Decimal::new(5, 4))); // 0.0005
    if scaled.cmp_numeric(&min_tol) != core::cmp::Ordering::Less {
        scaled
    } else {
        min_tol
    }
}

fn true_ranges(bars: &[CompactBar]) -> Vec<Option<GdaDecimal>> {
    let mut ranges = Vec::with_capacity(bars.len());
    let mut previous_close: Option<GdaDecimal> = None;
    for bar in bars {
        let (Some(h), Some(l), Some(c)) = (bar.high, bar.low, bar.close) else {
            ranges.push(None);
            previous_close = None;
            continue;
        };
        let high = GdaDecimal::from_decimal(h);
        let low = GdaDecimal::from_decimal(l);
        let close = GdaDecimal::from_decimal(c);
        match &previous_close {
            None => ranges.push(Some(high.sub(&low))),
            Some(prev) => {
                let top = max_gda(&high, prev);
                let bot = min_gda(&low, prev);
                ranges.push(Some(top.sub(&bot)));
            }
        }
        previous_close = Some(close);
    }
    ranges
}

fn atr_series(bars: &[CompactBar], period: usize) -> Result<Vec<Option<GdaDecimal>>, OperatorError> {
    let ranges = true_ranges(bars);
    let mut result = vec![None; bars.len()];
    for index in 0..bars.len() {
        let start = (index + 1).saturating_sub(period);
        let window = &ranges[start..=index];
        if window.iter().any(Option::is_none) {
            continue;
        }
        let mut sum = GdaDecimal::zero();
        for item in window.iter().flatten() {
            sum = sum.add(item);
        }
        result[index] = Some(sum.div(&GdaDecimal::from_i64(window.len() as i64))?);
    }
    Ok(result)
}

fn max_gda(a: &GdaDecimal, b: &GdaDecimal) -> GdaDecimal {
    if a.cmp_numeric(b) != core::cmp::Ordering::Less {
        a.clone()
    } else {
        b.clone()
    }
}

fn min_gda(a: &GdaDecimal, b: &GdaDecimal) -> GdaDecimal {
    if a.cmp_numeric(b) != core::cmp::Ordering::Greater {
        a.clone()
    } else {
        b.clone()
    }
}

fn touch_stats(
    bars: &[CompactBar],
    start_index: usize,
    low: &GdaDecimal,
    high: &GdaDecimal,
    tolerance: &GdaDecimal,
) -> Result<TouchStats, OperatorError> {
    let mut count = 0u64;
    let mut last_ts = None;
    let mut touched_volume = GdaDecimal::zero();
    let mut total_volume = GdaDecimal::zero();
    let lower_bound = low.sub(tolerance);
    let upper_bound = high.add(tolerance);

    for bar in &bars[start_index..] {
        if bar.status == BarStatus::FeedGap {
            continue;
        }
        let (Some(h), Some(l)) = (bar.high, bar.low) else {
            continue;
        };
        let bar_high = GdaDecimal::from_decimal(h);
        let bar_low = GdaDecimal::from_decimal(l);
        let volume = GdaDecimal::from_decimal(bar.volume.unwrap_or(Decimal::ZERO));
        total_volume = total_volume.add(&volume);
        if bar_high.cmp_numeric(&lower_bound) != core::cmp::Ordering::Less
            && bar_low.cmp_numeric(&upper_bound) != core::cmp::Ordering::Greater
        {
            count += 1;
            last_ts = Some(bar.timestamp);
            touched_volume = touched_volume.add(&volume);
        }
    }

    Ok(TouchStats {
        count,
        last_ts,
        volume_weight: decimal_volume_weight(&touched_volume, &total_volume)?,
    })
}

fn level_status(
    bars: &[CompactBar],
    low: &GdaDecimal,
    high: &GdaDecimal,
    ts_available: i64,
) -> KeyLevelStatus {
    let after: Vec<&CompactBar> = bars
        .iter()
        .filter(|bar| bar.ts_available > ts_available)
        .collect();
    if after.is_empty() {
        return KeyLevelStatus::Active;
    }

    let mut direction: Option<bool> = None; // true = up, false = down
    for bar in &after {
        let Some(c) = bar.close else {
            continue;
        };
        let close = GdaDecimal::from_decimal(c);
        if close.cmp_numeric(high) == core::cmp::Ordering::Greater {
            direction = Some(true);
            break;
        }
        if close.cmp_numeric(low) == core::cmp::Ordering::Less {
            direction = Some(false);
            break;
        }
    }

    let Some(up) = direction else {
        return KeyLevelStatus::Active;
    };
    let Some(last_close) = after
        .iter()
        .rev()
        .find_map(|bar| bar.close.map(GdaDecimal::from_decimal))
    else {
        return KeyLevelStatus::Active;
    };

    if last_close.cmp_numeric(low) != core::cmp::Ordering::Less
        && last_close.cmp_numeric(high) != core::cmp::Ordering::Greater
    {
        return KeyLevelStatus::Active;
    }
    if up {
        if last_close.cmp_numeric(high) == core::cmp::Ordering::Greater {
            KeyLevelStatus::Broken
        } else {
            KeyLevelStatus::Flipped
        }
    } else if last_close.cmp_numeric(low) == core::cmp::Ordering::Less {
        KeyLevelStatus::Broken
    } else {
        KeyLevelStatus::Flipped
    }
}

fn recency(cutoff_ts: i64, last_ts: Option<i64>, bar_ms: i64) -> f64 {
    let Some(last) = last_ts else {
        return 0.0;
    };
    if bar_ms <= 0 {
        return 0.0;
    }
    let age = (cutoff_ts - last).max(0) as f64 / ((bar_ms * 200) as f64);
    (1.0 - age).max(0.0)
}

fn merge_parameters(
    base: &BTreeMap<String, String>,
    extra: &[(&str, String)],
) -> BTreeMap<String, String> {
    let mut merged = base.clone();
    for (k, v) in extra {
        merged.insert((*k).to_owned(), v.clone());
    }
    merged
}

fn bar_ms_for(timeframe: &str) -> i64 {
    timeframe_ms(timeframe).unwrap_or(60_000)
}

#[allow(clippy::too_many_arguments)]
fn structure_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    atr: &[Option<GdaDecimal>],
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let span = params.swing_span;
    let confirm_offset = span.max(params.confirm_bars);
    let mut pivots = Vec::new();
    let pivot_atr_min = GdaDecimal::from_decimal(params.pivot_atr_min);

    if bars.len() > span * 2 {
        for index in span..(bars.len() - span) {
            let confirm_index = index + confirm_offset;
            if confirm_index >= bars.len() {
                continue;
            }
            let confirm_bar = &bars[confirm_index];
            if confirm_bar.ts_available > cutoff_ts {
                continue;
            }
            let (Some(h_dec), Some(l_dec)) = (bars[index].high, bars[index].low) else {
                continue;
            };
            let high = GdaDecimal::from_decimal(h_dec);
            let low = GdaDecimal::from_decimal(l_dec);

            let window_high = bars[(index - span)..=(index + span)]
                .iter()
                .filter_map(|b| b.high.map(GdaDecimal::from_decimal))
                .max_by(|a, b| a.cmp_numeric(b));
            let window_low = bars[(index - span)..=(index + span)]
                .iter()
                .filter_map(|b| b.low.map(GdaDecimal::from_decimal))
                .min_by(|a, b| a.cmp_numeric(b));
            let local_atr = atr[index].as_ref();

            if window_high
                .as_ref()
                .is_some_and(|wh| high.cmp_numeric(wh) == core::cmp::Ordering::Equal)
            {
                let max_neighbour = ((index - span)..index)
                    .chain((index + 1)..=(index + span))
                    .filter_map(|pos| bars[pos].high.map(GdaDecimal::from_decimal))
                    .max_by(|a, b| a.cmp_numeric(b));
                if let Some(mn) = max_neighbour {
                    let prominence = high.sub(&mn);
                    let threshold = local_atr
                        .cloned()
                        .unwrap_or_else(GdaDecimal::zero)
                        .mul(&pivot_atr_min);
                    if prominence.cmp_numeric(&threshold) != core::cmp::Ordering::Less {
                        pivots.push(Pivot {
                            side: KeyLevelKind::SwingHigh,
                            price: high.clone(),
                            index,
                            atr: local_atr.cloned(),
                            prominence,
                            ts_available: confirm_bar.ts_available,
                            formed_ts: bars[index].timestamp,
                        });
                    }
                }
            }

            if window_low
                .as_ref()
                .is_some_and(|wl| low.cmp_numeric(wl) == core::cmp::Ordering::Equal)
            {
                let min_neighbour = ((index - span)..index)
                    .chain((index + 1)..=(index + span))
                    .filter_map(|pos| bars[pos].low.map(GdaDecimal::from_decimal))
                    .min_by(|a, b| a.cmp_numeric(b));
                if let Some(mn) = min_neighbour {
                    let prominence = mn.sub(&low);
                    let threshold = local_atr
                        .cloned()
                        .unwrap_or_else(GdaDecimal::zero)
                        .mul(&pivot_atr_min);
                    if prominence.cmp_numeric(&threshold) != core::cmp::Ordering::Less {
                        pivots.push(Pivot {
                            side: KeyLevelKind::SwingLow,
                            price: low,
                            index,
                            atr: local_atr.cloned(),
                            prominence,
                            ts_available: confirm_bar.ts_available,
                            formed_ts: bars[index].timestamp,
                        });
                    }
                }
            }
        }
    }

    let mut levels = Vec::new();
    for side in [KeyLevelKind::SwingHigh, KeyLevelKind::SwingLow] {
        let mut side_pivots: Vec<&Pivot> = pivots.iter().filter(|p| p.side == side).collect();
        side_pivots.sort_by(|a, b| match a.price.cmp_numeric(&b.price) {
            core::cmp::Ordering::Equal => a.index.cmp(&b.index),
            ord => ord,
        });

        let mut clusters: Vec<Vec<&Pivot>> = Vec::new();
        for pivot in side_pivots {
            let mut target_idx = None;
            for (c_idx, cluster) in clusters.iter().enumerate() {
                let mut tol = zone_tolerance(pivot.atr.as_ref(), &pivot.price, params.cluster_atr);
                let mut max_member_price = cluster[0].price.clone();
                for member in cluster {
                    let mt = zone_tolerance(member.atr.as_ref(), &member.price, params.cluster_atr);
                    tol = max_gda(&tol, &mt);
                    max_member_price = max_gda(&max_member_price, &member.price);
                }
                let diff = pivot.price.sub(&max_member_price);
                if diff.cmp_numeric(&tol) != core::cmp::Ordering::Greater {
                    target_idx = Some(c_idx);
                    break;
                }
            }
            if let Some(idx) = target_idx {
                clusters[idx].push(pivot);
            } else {
                clusters.push(vec![pivot]);
            }
        }

        for cluster in clusters {
            let earliest = cluster
                .iter()
                .copied()
                .min_by_key(|p| p.index)
                .expect("non-empty cluster");
            let mut lowest = cluster[0].price.clone();
            let mut highest = cluster[0].price.clone();
            let mut touch_tol =
                zone_tolerance(cluster[0].atr.as_ref(), &cluster[0].price, params.touch_atr);
            let mut ts_available = cluster[0].ts_available;
            let mut quality = 0.0f64;

            for pivot in &cluster {
                lowest = min_gda(&lowest, &pivot.price);
                highest = max_gda(&highest, &pivot.price);
                let pt = zone_tolerance(pivot.atr.as_ref(), &pivot.price, params.touch_atr);
                touch_tol = max_gda(&touch_tol, &pt);
                ts_available = ts_available.max(pivot.ts_available);
                if let Some(a) = &pivot.atr
                    && a.is_positive()
                {
                    let ratio = pivot.prominence.div(a)?;
                    let capped = min_gda(&GdaDecimal::from_i64(1), &ratio);
                    let q = gda_to_f64(&capped)?;
                    if q > quality {
                        quality = q;
                    }
                }
            }

            let stats = touch_stats(bars, earliest.index, &lowest, &highest, &touch_tol)?;
            let last_ts = stats.last_ts.unwrap_or(earliest.formed_ts);
            let vw_f64 = gda_to_f64(&stats.volume_weight)?;
            let strength = 0.28
                + 0.08 * (stats.count.min(6) as f64)
                + 0.22 * vw_f64
                + 0.12 * recency(cutoff_ts, Some(last_ts), bar_ms)
                + 0.15 * quality;
            let single = lowest.cmp_numeric(&highest) == core::cmp::Ordering::Equal;

            let zone_low_dec = lowest.to_decimal()?;
            let zone_high_dec = highest.to_decimal()?;

            levels.push(KeyLevel {
                level_id: level_id(
                    instrument_id,
                    side,
                    timeframe,
                    earliest.formed_ts,
                    &earliest.price,
                    "",
                )?,
                instrument_id: instrument_id.to_owned(),
                kind: side,
                category: kind_category(side),
                evidence_class: EvidenceClass::Observed,
                timeframe: timeframe.to_owned(),
                price: if single { Some(zone_low_dec) } else { None },
                price_low: if single { None } else { Some(zone_low_dec) },
                price_high: if single { None } else { Some(zone_high_dec) },
                strength: round_strength(strength),
                status: level_status(bars, &lowest, &highest, ts_available),
                ts_available,
                formed_ts: earliest.formed_ts,
                method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
                parameters: base_params.clone(),
                touch_count: stats.count,
                last_touch_ts: stats.last_ts,
                volume_weight: stats.volume_weight.to_decimal()?,
            });
        }
    }

    Ok(levels)
}

fn window_bars(bars: &[CompactBar], start_ms: i64, end_ms: i64) -> Vec<(usize, &CompactBar)> {
    bars.iter()
        .enumerate()
        .filter(|(_, b)| b.timestamp >= start_ms && b.timestamp < end_ms)
        .collect()
}

fn window_complete(window: &[(usize, &CompactBar)], start_ms: i64, end_ms: i64, bar_ms: i64) -> bool {
    let Some((_, first)) = window.first() else {
        return false;
    };
    let Some((_, last)) = window.last() else {
        return false;
    };
    let max_offset = GdaDecimal::from_i64(bar_ms).mul(&GdaDecimal::from_decimal(Decimal::new(15, 1)));
    let actual_offset = GdaDecimal::from_i64(first.timestamp - start_ms);
    if actual_offset.cmp_numeric(&max_offset) == core::cmp::Ordering::Greater {
        return false;
    }
    last.ts_available >= end_ms - bar_ms
}

#[allow(clippy::too_many_arguments)]
fn build_session_level(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    params: &KeyLevelParams,
    bar_ms: i64,
    kind: KeyLevelKind,
    price: GdaDecimal,
    formed_ts: i64,
    ts_available: i64,
    anchor_ts: i64,
    tag: &str,
    parameters: BTreeMap<String, String>,
    base_strength: f64,
    touch_index: usize,
    zone: Option<(GdaDecimal, GdaDecimal)>,
) -> Result<KeyLevel, OperatorError> {
    let (low, high) = match &zone {
        Some((zl, zh)) => (zl.clone(), zh.clone()),
        None => (price.clone(), price.clone()),
    };
    let tolerance = zone_tolerance(None, &price, params.touch_atr);
    let stats = touch_stats(bars, touch_index, &low, &high, &tolerance)?;
    let last_ts = stats.last_ts.unwrap_or(formed_ts);
    let vw_f64 = gda_to_f64(&stats.volume_weight)?;
    let final_strength = base_strength
        + 0.08 * (stats.count.min(4) as f64)
        + 0.12 * vw_f64
        + 0.08 * recency(cutoff_ts, Some(last_ts), bar_ms);

    let (p_single, p_low, p_high) = match zone {
        None => (Some(price.to_decimal()?), None, None),
        Some((zl, zh)) => (None, Some(zl.to_decimal()?), Some(zh.to_decimal()?)),
    };

    Ok(KeyLevel {
        level_id: level_id(instrument_id, kind, timeframe, anchor_ts, &price, tag)?,
        instrument_id: instrument_id.to_owned(),
        kind,
        category: kind_category(kind),
        evidence_class: EvidenceClass::Observed,
        timeframe: timeframe.to_owned(),
        price: p_single,
        price_low: p_low,
        price_high: p_high,
        strength: round_strength(final_strength),
        status: level_status(bars, &low, &high, ts_available),
        ts_available,
        formed_ts,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters,
        touch_count: stats.count,
        last_touch_ts: stats.last_ts,
        volume_weight: stats.volume_weight.to_decimal()?,
    })
}

#[allow(clippy::too_many_arguments)]
fn session_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
    profile: &SessionProfile,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let tz = TimeZoneRule::from_name(&profile.timezone);
    let current_day = day_start_ms(cutoff_ts, tz, &profile.day_start);
    let previous_day = previous_day_start_ms(current_day, tz, &profile.day_start);
    let previous_window = window_bars(bars, previous_day, current_day);
    let mut levels = Vec::new();

    if window_complete(&previous_window, previous_day, current_day, bar_ms) {
        let usable: Vec<(usize, &CompactBar)> = previous_window
            .into_iter()
            .filter(|(_, b)| b.status != BarStatus::FeedGap)
            .collect();
        if let Some((first_index, _)) = usable.first().copied() {
            let low_price = usable
                .iter()
                .filter_map(|(_, b)| b.low.map(GdaDecimal::from_decimal))
                .min_by(|a, b| a.cmp_numeric(b));
            let high_price = usable
                .iter()
                .filter_map(|(_, b)| b.high.map(GdaDecimal::from_decimal))
                .max_by(|a, b| a.cmp_numeric(b));
            let close_price = usable
                .iter()
                .rev()
                .find_map(|(_, b)| b.close.map(GdaDecimal::from_decimal));
            let common = merge_parameters(
                base_params,
                &[
                    ("timezone", profile.timezone.clone()),
                    ("day_start", profile.day_start.clone()),
                ],
            );
            for (kind, maybe_price) in [
                (KeyLevelKind::PrevDayHigh, high_price),
                (KeyLevelKind::PrevDayLow, low_price),
            ] {
                if let Some(price) = maybe_price {
                    levels.push(build_session_level(
                        instrument_id,
                        timeframe,
                        bars,
                        cutoff_ts,
                        params,
                        bar_ms,
                        kind,
                        price,
                        previous_day,
                        current_day,
                        previous_day,
                        &profile.day_start,
                        common.clone(),
                        0.4,
                        first_index,
                        None,
                    )?);
                }
            }
            if let Some(price) = close_price {
                levels.push(build_session_level(
                    instrument_id,
                    timeframe,
                    bars,
                    cutoff_ts,
                    params,
                    bar_ms,
                    KeyLevelKind::PrevDayClose,
                    price,
                    previous_day,
                    current_day,
                    previous_day,
                    &profile.day_start,
                    common,
                    0.36,
                    first_index,
                    None,
                )?);
            }
        }
    }

    let max_bar_offset =
        GdaDecimal::from_i64(bar_ms).mul(&GdaDecimal::from_decimal(Decimal::new(15, 1)));

    let month_start = month_start_ms(cutoff_ts, tz, &profile.day_start);
    let month_window = window_bars(bars, month_start, cutoff_ts + 1);
    if let Some((first_idx, first)) = month_window.first().copied()
        && GdaDecimal::from_i64(first.timestamp - month_start).cmp_numeric(&max_bar_offset)
            != core::cmp::Ordering::Greater
        && let Some(open) = first.open
    {
        levels.push(build_session_level(
            instrument_id,
            timeframe,
            bars,
            cutoff_ts,
            params,
            bar_ms,
            KeyLevelKind::MonthOpen,
            GdaDecimal::from_decimal(open),
            first.timestamp,
            first.ts_available,
            month_start,
            "month",
            merge_parameters(base_params, &[("timezone", profile.timezone.clone())]),
            0.34,
            first_idx,
            None,
        )?);
    }

    let week_start = week_start_ms(cutoff_ts, tz, &profile.day_start);
    let week_window = window_bars(bars, week_start, cutoff_ts + 1);
    if let Some((first_idx, first)) = week_window.first().copied()
        && GdaDecimal::from_i64(first.timestamp - week_start).cmp_numeric(&max_bar_offset)
            != core::cmp::Ordering::Greater
        && let Some(open) = first.open
    {
        levels.push(build_session_level(
            instrument_id,
            timeframe,
            bars,
            cutoff_ts,
            params,
            bar_ms,
            KeyLevelKind::WeekOpen,
            GdaDecimal::from_decimal(open),
            first.timestamp,
            first.ts_available,
            week_start,
            "week",
            merge_parameters(base_params, &[("timezone", profile.timezone.clone())]),
            0.4,
            first_idx,
            None,
        )?);
    }

    let range_ms = (params.opening_range_minutes as i64) * 60 * SEC_MS;
    if bar_ms <= range_ms {
        let range_end = current_day + range_ms;
        if cutoff_ts >= range_end {
            let range_window = window_bars(bars, current_day, range_end);
            if window_complete(&range_window, current_day, range_end, bar_ms) {
                let usable: Vec<(usize, &CompactBar)> = range_window
                    .into_iter()
                    .filter(|(_, b)| b.status != BarStatus::FeedGap)
                    .collect();
                if let Some((first_index, _)) = usable.first().copied() {
                    let high_price = usable
                        .iter()
                        .filter_map(|(_, b)| b.high.map(GdaDecimal::from_decimal))
                        .max_by(|a, b| a.cmp_numeric(b));
                    let low_price = usable
                        .iter()
                        .filter_map(|(_, b)| b.low.map(GdaDecimal::from_decimal))
                        .min_by(|a, b| a.cmp_numeric(b));
                    if let (Some(hp), Some(lp)) = (high_price, low_price)
                        && hp.cmp_numeric(&lp) != core::cmp::Ordering::Equal
                    {
                        levels.push(build_session_level(
                            instrument_id,
                            timeframe,
                            bars,
                            cutoff_ts,
                            params,
                            bar_ms,
                            KeyLevelKind::OpeningRange,
                            lp.clone(),
                            current_day,
                            range_end,
                            current_day,
                            &params.opening_range_minutes.to_string(),
                            merge_parameters(
                                base_params,
                                &[("minutes", params.opening_range_minutes.to_string())],
                            ),
                            0.38,
                            first_index,
                            Some((lp, hp)),
                        )?);
                    }
                }
            }
        }
    }

    if !profile.sessions.is_empty() && bar_ms <= 3_600_000 {
        for session in &profile.sessions {
            let tz_name = session
                .timezone
                .as_deref()
                .unwrap_or(&profile.timezone);
            let session_tz = TimeZoneRule::from_name(tz_name);
            let Some((start_ms, end_ms)) =
                latest_session_window(cutoff_ts, &session.start, &session.end, session_tz)
            else {
                continue;
            };
            let sess_window = window_bars(bars, start_ms, end_ms);
            if !window_complete(&sess_window, start_ms, end_ms, bar_ms) {
                continue;
            }
            let usable: Vec<(usize, &CompactBar)> = sess_window
                .into_iter()
                .filter(|(_, b)| b.status != BarStatus::FeedGap)
                .collect();
            let Some((first_index, _)) = usable.first().copied() else {
                continue;
            };
            let high_price = usable
                .iter()
                .filter_map(|(_, b)| b.high.map(GdaDecimal::from_decimal))
                .max_by(|a, b| a.cmp_numeric(b));
            let low_price = usable
                .iter()
                .filter_map(|(_, b)| b.low.map(GdaDecimal::from_decimal))
                .min_by(|a, b| a.cmp_numeric(b));
            let common = merge_parameters(
                base_params,
                &[
                    ("session", session.name.clone()),
                    ("timezone", tz_name.to_owned()),
                ],
            );
            if let (Some(hp), Some(lp)) = (high_price, low_price) {
                for (kind, price) in [
                    (KeyLevelKind::SessionHigh, hp),
                    (KeyLevelKind::SessionLow, lp),
                ] {
                    levels.push(build_session_level(
                        instrument_id,
                        timeframe,
                        bars,
                        cutoff_ts,
                        params,
                        bar_ms,
                        kind,
                        price,
                        start_ms,
                        end_ms,
                        start_ms,
                        &session.name,
                        common.clone(),
                        0.34,
                        first_index,
                        None,
                    )?);
                }
            }
        }
    }

    Ok(levels)
}

#[allow(clippy::too_many_arguments)]
fn gap_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    atr: &[Option<GdaDecimal>],
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let mut levels = Vec::new();
    let gap_atr_min = GdaDecimal::from_decimal(params.gap_atr_min);

    for index in 1..bars.len() {
        let current = &bars[index];
        let previous = &bars[index - 1];
        let (Some(ch), Some(cl), Some(ph), Some(pl)) =
            (current.high, current.low, previous.high, previous.low)
        else {
            continue;
        };
        let cur_high = GdaDecimal::from_decimal(ch);
        let cur_low = GdaDecimal::from_decimal(cl);
        let prev_high = GdaDecimal::from_decimal(ph);
        let prev_low = GdaDecimal::from_decimal(pl);

        let (direction, zone_low, zone_high) =
            if cur_low.cmp_numeric(&prev_high) == core::cmp::Ordering::Greater {
                ("up", prev_high, cur_low)
            } else if cur_high.cmp_numeric(&prev_low) == core::cmp::Ordering::Less {
                ("down", cur_high, prev_low)
            } else {
                continue;
            };

        let local_atr = atr[index].as_ref();
        let size = zone_high.sub(&zone_low);
        if let Some(a) = local_atr
            && size.cmp_numeric(&a.mul(&gap_atr_min)) == core::cmp::Ordering::Less
        {
            continue;
        }

        let mut filled = false;
        for later in &bars[(index + 1)..] {
            let (Some(lh), Some(ll)) = (later.high, later.low) else {
                continue;
            };
            let later_high = GdaDecimal::from_decimal(lh);
            let later_low = GdaDecimal::from_decimal(ll);
            if direction == "up"
                && later_low.cmp_numeric(&zone_low) != core::cmp::Ordering::Greater
            {
                filled = true;
                break;
            }
            if direction == "down"
                && later_high.cmp_numeric(&zone_high) != core::cmp::Ordering::Less
            {
                filled = true;
                break;
            }
        }
        if filled {
            continue;
        }

        let tol_ref = current
            .close
            .map(GdaDecimal::from_decimal)
            .unwrap_or_else(|| zone_high.clone());
        let tolerance = zone_tolerance(local_atr, &tol_ref, params.touch_atr);
        let stats = touch_stats(bars, index, &zone_low, &zone_high, &tolerance)?;
        let last_ts = stats.last_ts.unwrap_or(current.timestamp);
        let size_score = if let Some(a) = local_atr
            && a.is_positive()
        {
            let denom = a.mul(&GdaDecimal::from_i64(3));
            let ratio = min_gda(&GdaDecimal::from_i64(1), &size.div(&denom)?);
            gda_to_f64(&ratio)?
        } else {
            1.0
        };
        let vw_f64 = gda_to_f64(&stats.volume_weight)?;
        let strength = 0.34
            + 0.2 * size_score
            + 0.1 * vw_f64
            + 0.12 * recency(cutoff_ts, Some(last_ts), bar_ms)
            + 0.06;

        levels.push(KeyLevel {
            level_id: level_id(
                instrument_id,
                KeyLevelKind::Gap,
                timeframe,
                current.timestamp,
                &zone_low,
                direction,
            )?,
            instrument_id: instrument_id.to_owned(),
            kind: KeyLevelKind::Gap,
            category: kind_category(KeyLevelKind::Gap),
            evidence_class: EvidenceClass::Observed,
            timeframe: timeframe.to_owned(),
            price: None,
            price_low: Some(zone_low.to_decimal()?),
            price_high: Some(zone_high.to_decimal()?),
            strength: round_strength(strength),
            status: level_status(bars, &zone_low, &zone_high, current.ts_available),
            ts_available: current.ts_available,
            formed_ts: current.timestamp,
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: merge_parameters(base_params, &[("direction", direction.to_owned())]),
            touch_count: stats.count,
            last_touch_ts: stats.last_ts,
            volume_weight: stats.volume_weight.to_decimal()?,
        });
    }

    Ok(levels)
}

fn round_step_for(price: &GdaDecimal) -> GdaDecimal {
    let digits = price.adjusted() + 1;
    let exponent = (digits - 2).clamp(-6, 6);
    GdaDecimal {
        sign: false,
        coeff: crate::gda::BigDec::from_u128(1),
        exp: exponent,
    }
}

fn round_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    atr: &[Option<GdaDecimal>],
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let Some(last) = bars.last() else {
        return Ok(Vec::new());
    };
    let Some(last_close_dec) = last.close else {
        return Ok(Vec::new());
    };
    let last_close = GdaDecimal::from_decimal(last_close_dec);
    if !last_close.is_positive() {
        return Ok(Vec::new());
    }

    let step = match params.round_step {
        Some(s) => GdaDecimal::from_decimal(s),
        None => round_step_for(&last_close),
    };
    if !step.is_positive() {
        return Ok(Vec::new());
    }

    let base = last_close.div(&step)?.floor_to_integral().mul(&step);
    let local_atr = atr.last().and_then(|a| a.as_ref());
    let window = if let Some(a) = local_atr
        && a.is_positive()
    {
        a.mul(&GdaDecimal::from_decimal(params.round_window_atr))
    } else {
        last_close.mul(&GdaDecimal::from_decimal(Decimal::new(2, 2))) // 0.02
    };

    let step_norm = normalize_gda(&step)?;
    let mut levels = Vec::new();
    for offset in -params.round_levels_per_side..=params.round_levels_per_side {
        let price = base.add(&step.mul(&GdaDecimal::from_i64(offset as i64)));
        if !price.is_positive() {
            continue;
        }
        let distance = price.sub(&last_close).abs();
        if distance.cmp_numeric(&window) == core::cmp::Ordering::Greater {
            continue;
        }
        let tolerance = zone_tolerance(local_atr, &price, params.touch_atr);
        let stats = touch_stats(bars, 0, &price, &price, &tolerance)?;
        let proximity = if window.is_positive() {
            let ratio = distance.div(&window)?;
            1.0 - gda_to_f64(&ratio)?
        } else {
            0.0
        };
        let vw_f64 = gda_to_f64(&stats.volume_weight)?;
        let strength = 0.24
            + 0.3 * proximity
            + 0.05 * (stats.count.min(5) as f64)
            + 0.1 * vw_f64;

        levels.push(KeyLevel {
            level_id: level_id(
                instrument_id,
                KeyLevelKind::RoundNumber,
                timeframe,
                0,
                &price,
                &step_norm,
            )?,
            instrument_id: instrument_id.to_owned(),
            kind: KeyLevelKind::RoundNumber,
            category: kind_category(KeyLevelKind::RoundNumber),
            evidence_class: EvidenceClass::Observed,
            timeframe: timeframe.to_owned(),
            price: Some(price.to_decimal()?),
            price_low: None,
            price_high: None,
            strength: round_strength(strength),
            status: KeyLevelStatus::Active,
            ts_available: last.ts_available,
            formed_ts: last.timestamp,
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: merge_parameters(base_params, &[("step", step_norm.clone())]),
            touch_count: stats.count,
            last_touch_ts: stats.last_ts,
            volume_weight: stats.volume_weight.to_decimal()?,
        });
    }
    Ok(levels)
}

fn quantize6(val: &GdaDecimal) -> Result<GdaDecimal, OperatorError> {
    val.quantize_exp(QUANTUM_EXP)
}

#[allow(clippy::too_many_arguments)]
fn volume_profile_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    atr: &[Option<GdaDecimal>],
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
    profile: &SessionProfile,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let tz = TimeZoneRule::from_name(&profile.timezone);
    let session_start = day_start_ms(cutoff_ts, tz, &profile.day_start);
    let mut window: Vec<&CompactBar> = bars
        .iter()
        .filter(|b| b.timestamp >= session_start)
        .collect();
    let mut profile_anchor = session_start;
    if window.is_empty() {
        window = bars.iter().collect();
        profile_anchor = bars.first().map(|b| b.timestamp).unwrap_or(cutoff_ts);
    }

    let usable_refs: Vec<&CompactBar> = window
        .into_iter()
        .filter(|b| b.volume.is_some() && b.high.is_some() && b.low.is_some())
        .collect();
    let mut total_volume = GdaDecimal::zero();
    for bar in &usable_refs {
        total_volume = total_volume.add(&GdaDecimal::from_decimal(bar.volume.unwrap()));
    }
    if usable_refs.is_empty() || !total_volume.is_positive() {
        return Ok(Vec::new());
    }

    let low_price = usable_refs
        .iter()
        .map(|b| GdaDecimal::from_decimal(b.low.unwrap()))
        .min_by(|a, b| a.cmp_numeric(b))
        .unwrap();
    let high_price = usable_refs
        .iter()
        .map(|b| GdaDecimal::from_decimal(b.high.unwrap()))
        .max_by(|a, b| a.cmp_numeric(b))
        .unwrap();
    if high_price.cmp_numeric(&low_price) == core::cmp::Ordering::Less {
        return Ok(Vec::new());
    }

    let last_bar = usable_refs.last().unwrap();
    let last_available = last_bar.ts_available;
    let rec = recency(cutoff_ts, Some(last_bar.timestamp), bar_ms);
    let formed_ts = profile_anchor;

    let (prices, poc_share, va_share, edge_low_share, edge_high_share) =
        if high_price.cmp_numeric(&low_price) == core::cmp::Ordering::Equal {
            let p = quantize6(&low_price)?;
            let one = GdaDecimal::from_i64(1);
            (
                [
                    (KeyLevelKind::Poc, p.clone()),
                    (KeyLevelKind::Vah, p.clone()),
                    (KeyLevelKind::Val, p),
                ],
                one.clone(),
                one.clone(),
                one.clone(),
                one,
            )
        } else {
            let bins = params.volume_profile_bins;
            let width = high_price
                .sub(&low_price)
                .div(&GdaDecimal::from_i64(bins as i64))?;
            if !width.is_positive() {
                return Ok(Vec::new());
            }
            let mut bin_volumes = vec![GdaDecimal::zero(); bins];
            for bar in &usable_refs {
                let b_low = GdaDecimal::from_decimal(bar.low.unwrap());
                let b_high = GdaDecimal::from_decimal(bar.high.unwrap());
                let b_vol = GdaDecimal::from_decimal(bar.volume.unwrap());
                let raw_start = b_low.sub(&low_price).div(&width)?.to_i64_floor()?;
                let raw_end = b_high.sub(&low_price).div(&width)?.to_i64_floor()?;
                let start = raw_start.clamp(0, (bins - 1) as i64) as usize;
                let end = raw_end.clamp(start as i64, (bins - 1) as i64) as usize;
                let share = b_vol.div(&GdaDecimal::from_i64((end - start + 1) as i64))?;
                for bv in &mut bin_volumes[start..=end] {
                    *bv = bv.add(&share);
                }
            }

            let mut poc_index = 0usize;
            for idx in 1..bins {
                if bin_volumes[idx].cmp_numeric(&bin_volumes[poc_index])
                    == core::cmp::Ordering::Greater
                {
                    poc_index = idx;
                }
            }
            let target = total_volume.mul(&GdaDecimal::from_decimal(params.value_area_ratio));
            let mut low_index = poc_index;
            let mut high_index = poc_index;
            let mut value_volume = bin_volumes[poc_index].clone();
            while value_volume.cmp_numeric(&target) == core::cmp::Ordering::Less {
                let left = if low_index > 0 {
                    Some(&bin_volumes[low_index - 1])
                } else {
                    None
                };
                let right = if high_index < bins - 1 {
                    Some(&bin_volumes[high_index + 1])
                } else {
                    None
                };
                match (left, right) {
                    (None, None) => break,
                    (Some(lv), None) => {
                        low_index -= 1;
                        value_volume = value_volume.add(lv);
                    }
                    (Some(lv), Some(rv)) if lv.cmp_numeric(rv) != core::cmp::Ordering::Less => {
                        low_index -= 1;
                        value_volume = value_volume.add(lv);
                    }
                    (_, Some(rv)) => {
                        high_index += 1;
                        value_volume = value_volume.add(rv);
                    }
                }
            }

            let poc_s = bin_volumes[poc_index].div(&total_volume)?;
            let va_s = value_volume.div(&total_volume)?;
            let edge_l_s = bin_volumes[low_index].div(&total_volume)?;
            let edge_h_s = bin_volumes[high_index].div(&total_volume)?;

            let half = GdaDecimal::from_decimal(Decimal::new(5, 1));
            let poc_price = quantize6(
                &low_price.add(&width.mul(&GdaDecimal::from_i64(poc_index as i64).add(&half))),
            )?;
            let vah_price =
                quantize6(&low_price.add(&width.mul(&GdaDecimal::from_i64((high_index + 1) as i64))))?;
            let val_price =
                quantize6(&low_price.add(&width.mul(&GdaDecimal::from_i64(low_index as i64))))?;
            (
                [
                    (KeyLevelKind::Poc, poc_price),
                    (KeyLevelKind::Vah, vah_price),
                    (KeyLevelKind::Val, val_price),
                ],
                poc_s,
                va_s,
                edge_l_s,
                edge_h_s,
            )
        };

    let usable_owned: Vec<CompactBar> = usable_refs.into_iter().cloned().collect();
    let atr_last = atr.last().and_then(|a| a.as_ref());
    let poc_share_f64 = gda_to_f64(&poc_share)?;
    let va_share_f64 = gda_to_f64(&va_share)?;
    let edge_low_f64 = gda_to_f64(&edge_low_share)?;
    let edge_high_f64 = gda_to_f64(&edge_high_share)?;

    let common_params = merge_parameters(
        base_params,
        &[
            ("source", "volume_profile".to_owned()),
            ("profile_anchor", "session".to_owned()),
            ("profile_start", formed_ts.to_string()),
        ],
    );

    let mut levels = Vec::with_capacity(3);
    for (kind, price) in prices {
        let tolerance = zone_tolerance(atr_last, &price, params.touch_atr);
        let stats = touch_stats(&usable_owned, 0, &price, &price, &tolerance)?;
        let strength = if kind == KeyLevelKind::Poc {
            0.30 + 0.30 * poc_share_f64 + 0.10 * va_share_f64 + 0.10 * rec
        } else {
            let edge = if kind == KeyLevelKind::Vah {
                edge_high_f64
            } else {
                edge_low_f64
            };
            0.28 + 0.18 * va_share_f64 + 0.06 * edge + 0.10 * rec
        };

        levels.push(KeyLevel {
            level_id: level_id(
                instrument_id,
                kind,
                timeframe,
                formed_ts,
                &price,
                "profile",
            )?,
            instrument_id: instrument_id.to_owned(),
            kind,
            category: kind_category(kind),
            evidence_class: EvidenceClass::Observed,
            timeframe: timeframe.to_owned(),
            price: Some(price.to_decimal()?),
            price_low: None,
            price_high: None,
            strength: round_strength(strength),
            status: KeyLevelStatus::Active,
            ts_available: last_available,
            formed_ts,
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: common_params.clone(),
            touch_count: stats.count,
            last_touch_ts: stats.last_ts,
            volume_weight: stats.volume_weight.to_decimal()?,
        });
    }

    Ok(levels)
}

fn anchored_vwap_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
) -> Result<Vec<KeyLevel>, OperatorError> {
    let mut precomputed_vwap: Option<GdaDecimal> = None;
    let (anchor_ts, first_ts) = match params.vwap_anchor {
        VwapAnchor::Session => {
            let Some((fts, vwap_dec)) = latest_session_vwap(bars)? else {
                return Ok(Vec::new());
            };
            precomputed_vwap = Some(GdaDecimal::from_decimal(vwap_dec));
            let a_ts = fts.div_euclid(DAY_MS) * DAY_MS;
            (a_ts, fts)
        }
        VwapAnchor::Snapshot => {
            let ts = bars[0].timestamp;
            (ts, ts)
        }
        VwapAnchor::Timestamp => {
            let ts = params.vwap_anchor_ts.ok_or_else(|| {
                OperatorError::InvalidKeyLevelParam("vwap_anchor=TIMESTAMP requires vwap_anchor_ts".to_owned())
            })?;
            (ts, ts)
        }
    };

    let contributing: Vec<(usize, &CompactBar)> = bars
        .iter()
        .enumerate()
        .filter(|(_, b)| {
            b.timestamp >= first_ts
                && b.volume.is_some_and(|v| v > Decimal::ZERO)
                && b.high.is_some()
                && b.low.is_some()
                && b.close.is_some()
        })
        .collect();
    if contributing.is_empty() {
        return Ok(Vec::new());
    }

    let mut sum_volume = GdaDecimal::zero();
    let mut sum_price_volume = GdaDecimal::zero();
    let mut sum_price_squared_volume = GdaDecimal::zero();
    let three = GdaDecimal::from_i64(3);

    for (_, bar) in &contributing {
        let high = GdaDecimal::from_decimal(bar.high.unwrap());
        let low = GdaDecimal::from_decimal(bar.low.unwrap());
        let close = GdaDecimal::from_decimal(bar.close.unwrap());
        let volume = GdaDecimal::from_decimal(bar.volume.unwrap());
        let typical = high.add(&low).add(&close).div(&three)?;
        sum_volume = sum_volume.add(&volume);
        if precomputed_vwap.is_none() {
            sum_price_volume = sum_price_volume.add(&typical.mul(&volume));
        }
        sum_price_squared_volume =
            sum_price_squared_volume.add(&typical.mul(&typical).mul(&volume));
    }

    if !sum_volume.is_positive() {
        return Ok(Vec::new());
    }
    let vwap = match precomputed_vwap {
        Some(v) => v,
        None => sum_price_volume.div(&sum_volume)?,
    };
    let variance = sum_price_squared_volume
        .div(&sum_volume)?
        .sub(&vwap.mul(&vwap));
    let sigma = if variance.is_positive() {
        variance.sqrt()?
    } else {
        GdaDecimal::zero()
    };

    let (first_index, _) = contributing[0];
    let (_, last_contrib) = contributing.last().copied().unwrap();
    let last_available = last_contrib.ts_available;
    let rec = recency(cutoff_ts, Some(last_contrib.timestamp), bar_ms);
    let sample_score = ((contributing.len() as f64) / 50.0).min(1.0);
    let positive_bars: Vec<CompactBar> = bars
        .iter()
        .filter(|b| b.volume.is_some_and(|v| v > Decimal::ZERO))
        .cloned()
        .collect();

    let sigmas = parse_sigmas(&params.vwap_band_sigmas)?;
    let mut multipliers = Vec::with_capacity(1 + sigmas.len() * 2);
    multipliers.push(Decimal::ZERO);
    for band in sigmas {
        multipliers.push(-band);
        multipliers.push(band);
    }

    let mut levels = Vec::new();
    for mult_dec in multipliers {
        let mult_gda = GdaDecimal::from_decimal(mult_dec);
        let price = quantize6(&vwap.add(&sigma.mul(&mult_gda)))?;
        if !price.is_positive() {
            continue;
        }
        let tolerance = zone_tolerance(None, &price, params.touch_atr);
        let stats = touch_stats(bars, first_index, &price, &price, &tolerance)?;
        let mult_f64 = gda_to_f64(&mult_gda)?;
        let strength =
            0.34 + 0.14 * sample_score + 0.08 * rec - 0.05 * mult_f64.abs().min(3.0);

        let mult_norm = format_normalized_decimal_str(mult_dec);
        let tag = if mult_dec.is_zero() {
            "vwap".to_owned()
        } else if mult_dec.is_sign_negative() {
            format!("vwap{mult_norm}")
        } else {
            format!("vwap+{mult_norm}")
        };

        levels.push(KeyLevel {
            level_id: level_id(
                instrument_id,
                KeyLevelKind::AnchoredVwap,
                timeframe,
                anchor_ts,
                &price,
                &tag,
            )?,
            instrument_id: instrument_id.to_owned(),
            kind: KeyLevelKind::AnchoredVwap,
            category: kind_category(KeyLevelKind::AnchoredVwap),
            evidence_class: EvidenceClass::Observed,
            timeframe: timeframe.to_owned(),
            price: Some(price.to_decimal()?),
            price_low: None,
            price_high: None,
            strength: round_strength(strength),
            status: level_status(&positive_bars, &price, &price, last_available),
            ts_available: last_available,
            formed_ts: anchor_ts,
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: merge_parameters(
                base_params,
                &[
                    ("source", "anchored_vwap".to_owned()),
                    ("anchor_ts", anchor_ts.to_string()),
                    ("band_sigma", mult_norm),
                ],
            ),
            touch_count: stats.count,
            last_touch_ts: stats.last_ts,
            volume_weight: stats.volume_weight.to_decimal()?,
        });
    }

    Ok(levels)
}

struct LiqEvent {
    notional: GdaDecimal,
    leverage: u32,
    is_long: bool,
    ts_event: i64,
    ts_available: i64,
}

#[allow(clippy::too_many_arguments)]
fn liquidation_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    atr: &[Option<GdaDecimal>],
    params: &KeyLevelParams,
    base_params: &BTreeMap<String, String>,
    bar_ms: i64,
    derivative_points: &[DerivativePoint],
) -> Result<(Vec<KeyLevel>, Vec<String>), OperatorError> {
    let mut points: Vec<&DerivativePoint> = derivative_points
        .iter()
        .filter(|p| {
            p.kind == DerivativeKind::OpenInterest
                && p.ts_available <= cutoff_ts
                && p.value > Decimal::ZERO
        })
        .collect();
    points.sort_by_key(|p| p.ts_event);

    if points.is_empty() {
        return Ok((
            Vec::new(),
            vec!["该快照没有可用的持仓量数据，清算密集区为空。".to_owned()],
        ));
    }
    if points.len() < params.liquidation_min_points {
        return Ok((
            Vec::new(),
            vec![format!(
                "持仓量样本不足（{}/{}），清算密集区为空。",
                points.len(),
                params.liquidation_min_points
            )],
        ));
    }

    let timestamps: Vec<i64> = bars.iter().map(|b| b.timestamp).collect();
    let close_at = |ts: i64| -> Option<GdaDecimal> {
        let idx = timestamps.partition_point(|&t| t <= ts);
        if idx == 0 {
            None
        } else {
            bars[idx - 1].close.map(GdaDecimal::from_decimal)
        }
    };

    let Some(last_close) = bars
        .iter()
        .rev()
        .find_map(|b| b.close.map(GdaDecimal::from_decimal))
    else {
        return Ok((Vec::new(), vec!["缺少可用收盘价，清算密集区为空。".to_owned()]));
    };
    if !last_close.is_positive() {
        return Ok((Vec::new(), vec!["缺少可用收盘价，清算密集区为空。".to_owned()]));
    }

    let atr_last = atr.last().and_then(|a| a.as_ref());
    let width = if let Some(a) = atr_last
        && a.is_positive()
    {
        a.mul(&GdaDecimal::from_decimal(params.liquidation_cluster_atr))
    } else {
        last_close.mul(&GdaDecimal::from_decimal(Decimal::new(2, 3))) // 0.002
    };
    if !width.is_positive() {
        return Ok((Vec::new(), vec!["无法确定清算密集区宽度。".to_owned()]));
    }
    let distance_limit = if let Some(a) = atr_last
        && a.is_positive()
    {
        a.mul(&GdaDecimal::from_decimal(params.liquidation_max_distance_atr))
    } else {
        last_close.mul(&GdaDecimal::from_decimal(Decimal::new(25, 2))) // 0.25
    };

    let min_oi_change = GdaDecimal::from_decimal(params.liquidation_min_oi_change);
    let one = GdaDecimal::from_i64(1);
    let mut buckets: BTreeMap<i64, Vec<LiqEvent>> = BTreeMap::new();

    for pair in points.windows(2) {
        let prev = pair[0];
        let curr = pair[1];
        let prev_val = GdaDecimal::from_decimal(prev.value);
        let curr_val = GdaDecimal::from_decimal(curr.value);
        let delta = curr_val.sub(&prev_val);
        if !delta.is_positive() || !prev_val.is_positive() {
            continue;
        }
        let relative = delta.div(&prev_val)?;
        if relative.cmp_numeric(&min_oi_change) == core::cmp::Ordering::Less {
            continue;
        }
        let Some(price) = close_at(curr.ts_event) else {
            continue;
        };
        let Some(previous_price) = close_at(prev.ts_event) else {
            continue;
        };
        if !price.is_positive() {
            continue;
        }
        let is_long = price.cmp_numeric(&previous_price) != core::cmp::Ordering::Less;
        let notional = delta.mul(&price);

        for &leverage in &params.leverage_tiers {
            let factor = one.div(&GdaDecimal::from_i64(leverage as i64))?;
            let liq_price = if is_long {
                price.mul(&one.sub(&factor))
            } else {
                price.mul(&one.add(&factor))
            };
            if !liq_price.is_positive() {
                continue;
            }
            if liq_price.sub(&last_close).abs().cmp_numeric(&distance_limit)
                == core::cmp::Ordering::Greater
            {
                continue;
            }
            let bucket = liq_price.div(&width)?.to_i64_floor()?;
            buckets.entry(bucket).or_default().push(LiqEvent {
                notional: notional.clone(),
                leverage,
                is_long,
                ts_event: curr.ts_event,
                ts_available: curr.ts_available,
            });
        }
    }

    if buckets.is_empty() {
        return Ok((
            Vec::new(),
            vec!["持仓量没有可用的增量，清算密集区为空。".to_owned()],
        ));
    }

    let ordered_buckets: Vec<i64> = buckets.keys().copied().collect();
    let mut merged: Vec<Vec<i64>> = Vec::new();
    let mut current_group = vec![ordered_buckets[0]];
    for &b in &ordered_buckets[1..] {
        if b - *current_group.last().unwrap() <= 1 {
            current_group.push(b);
        } else {
            merged.push(current_group);
            current_group = vec![b];
        }
    }
    merged.push(current_group);

    struct GroupData {
        min_bucket: i64,
        max_bucket: i64,
        weight: GdaDecimal,
        events: Vec<LiqEvent>,
        formed_ts: i64,
        available_ts: i64,
    }

    let mut groups: Vec<GroupData> = Vec::with_capacity(merged.len());
    for group in merged {
        let min_bucket = group[0];
        let max_bucket = *group.last().unwrap();
        let mut events = Vec::new();
        for b in group {
            if let Some(evts) = buckets.remove(&b) {
                events.extend(evts);
            }
        }
        let mut weight = GdaDecimal::zero();
        let mut formed_ts = events[0].ts_event;
        let mut available_ts = events[0].ts_available;
        for ev in &events {
            weight = weight.add(&ev.notional);
            formed_ts = formed_ts.min(ev.ts_event);
            available_ts = available_ts.max(ev.ts_available);
        }
        groups.push(GroupData {
            min_bucket,
            max_bucket,
            weight,
            events,
            formed_ts,
            available_ts,
        });
    }

    let max_weight = groups
        .iter()
        .map(|g| g.weight.clone())
        .max_by(|a, b| a.cmp_numeric(b))
        .unwrap();

    let mut levels = Vec::with_capacity(groups.len());
    let width_norm = normalize_gda(&width)?;
    for g in groups {
        let lev_set: BTreeSet<u32> = g.events.iter().map(|e| e.leverage).collect();
        let leverages_str = lev_set
            .iter()
            .map(|l| l.to_string())
            .collect::<Vec<_>>()
            .join(",");
        let has_long = g.events.iter().any(|e| e.is_long);
        let has_short = g.events.iter().any(|e| !e.is_long);
        let dir_str = match (has_long, has_short) {
            (true, false) => "long",
            (false, true) => "short",
            _ => "mixed",
        };
        let low_price = quantize6(&width.mul(&GdaDecimal::from_i64(g.min_bucket)))?;
        let high_price = quantize6(&width.mul(&GdaDecimal::from_i64(g.max_bucket + 1)))?;
        let weight_ratio = gda_to_f64(&g.weight.div(&max_weight)?)?;
        let strength = 0.30
            + 0.35 * weight_ratio
            + 0.15 * recency(cutoff_ts, Some(g.available_ts), bar_ms)
            + 0.10 * ((g.events.len() as f64) / 3.0).min(1.0);
        let tag = format!("liq:{leverages_str}");

        levels.push(KeyLevel {
            level_id: level_id(
                instrument_id,
                KeyLevelKind::LiquidationZone,
                timeframe,
                g.formed_ts,
                &low_price,
                &tag,
            )?,
            instrument_id: instrument_id.to_owned(),
            kind: KeyLevelKind::LiquidationZone,
            category: kind_category(KeyLevelKind::LiquidationZone),
            evidence_class: EvidenceClass::Estimated,
            timeframe: timeframe.to_owned(),
            price: None,
            price_low: Some(low_price.to_decimal()?),
            price_high: Some(high_price.to_decimal()?),
            strength: round_strength(strength),
            status: KeyLevelStatus::Active,
            ts_available: g.available_ts,
            formed_ts: g.formed_ts,
            method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
            parameters: merge_parameters(
                base_params,
                &[
                    ("source", "oi_change_leverage".to_owned()),
                    ("direction", dir_str.to_owned()),
                    ("cluster_leverages", leverages_str),
                    ("oi_events", g.events.len().to_string()),
                    ("estimated_notional", normalize_gda(&g.weight)?),
                    ("cluster_width", width_norm.clone()),
                ],
            ),
            touch_count: 0,
            last_touch_ts: None,
            volume_weight: Decimal::ZERO,
        });
    }

    levels.sort_by(|a, b| {
        b.strength
            .total_cmp(&a.strength)
            .then_with(|| a.level_id.cmp(&b.level_id))
    });
    levels.truncate(params.liquidation_max_levels);
    Ok((levels, Vec::new()))
}

/// Compute all Key Levels visible by `cutoff_ts` together with empty-set notes.
pub fn compute_key_levels_with_notes(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    session_profile: Option<&SessionProfile>,
    params: Option<&KeyLevelParams>,
    derivative_points: &[DerivativePoint],
) -> Result<KeyLevelComputation, OperatorError> {
    let default_params = KeyLevelParams::default();
    let params = params.unwrap_or(&default_params);
    params.validate()?;

    let default_profile = SessionProfile {
        timezone: "UTC".to_owned(),
        day_start: "00:00".to_owned(),
        sessions: Vec::new(),
    };
    let profile = session_profile.unwrap_or(&default_profile);

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
    if visible.is_empty() {
        return Ok(KeyLevelComputation {
            levels: Vec::new(),
            notes: Vec::new(),
        });
    }

    let bar_ms = bar_ms_for(timeframe);
    let atr = atr_series(&visible, params.atr_period)?;
    let base_params = params.as_parameters()?;

    let mut collected = Vec::new();
    collected.extend(structure_levels(
        instrument_id,
        timeframe,
        &visible,
        cutoff_ts,
        &atr,
        params,
        &base_params,
        bar_ms,
    )?);
    collected.extend(session_levels(
        instrument_id,
        timeframe,
        &visible,
        cutoff_ts,
        params,
        &base_params,
        bar_ms,
        profile,
    )?);
    collected.extend(gap_levels(
        instrument_id,
        timeframe,
        &visible,
        cutoff_ts,
        &atr,
        params,
        &base_params,
        bar_ms,
    )?);
    collected.extend(round_levels(
        instrument_id,
        timeframe,
        &visible,
        &atr,
        params,
        &base_params,
    )?);
    collected.extend(volume_profile_levels(
        instrument_id,
        timeframe,
        &visible,
        cutoff_ts,
        &atr,
        params,
        &base_params,
        bar_ms,
        profile,
    )?);
    let vwap_bars = if params.vwap_anchor == VwapAnchor::Session {
        &visible_with_gaps[..]
    } else {
        &visible[..]
    };
    collected.extend(anchored_vwap_levels(
        instrument_id,
        timeframe,
        vwap_bars,
        cutoff_ts,
        params,
        &base_params,
        bar_ms,
    )?);

    let (liq_levels, notes) = liquidation_levels(
        instrument_id,
        timeframe,
        &visible,
        cutoff_ts,
        &atr,
        params,
        &base_params,
        bar_ms,
        derivative_points,
    )?;
    collected.extend(liq_levels);

    let mut seen_ids = BTreeSet::new();
    let mut deduped = Vec::with_capacity(collected.len());
    for level in collected {
        if seen_ids.insert(level.level_id.clone()) {
            deduped.push(level);
        }
    }

    deduped.sort_by(|a, b| {
        b.strength
            .total_cmp(&a.strength)
            .then_with(|| b.ts_available.cmp(&a.ts_available))
            .then_with(|| a.level_id.cmp(&b.level_id))
    });
    deduped.truncate(params.max_levels);

    Ok(KeyLevelComputation {
        levels: deduped,
        notes,
    })
}

/// Compute lookahead-free Key Levels visible by `cutoff_ts`.
pub fn compute_key_levels(
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    session_profile: Option<&SessionProfile>,
    params: Option<&KeyLevelParams>,
    derivative_points: &[DerivativePoint],
) -> Result<Vec<KeyLevel>, OperatorError> {
    Ok(compute_key_levels_with_notes(
        instrument_id,
        timeframe,
        bars,
        cutoff_ts,
        session_profile,
        params,
        derivative_points,
    )?
    .levels)
}

/// Compute a frozen `KeyLevelSet` contract payload.
#[allow(clippy::too_many_arguments)]
pub fn compute_key_level_set(
    snapshot_id: &str,
    instrument_id: &str,
    timeframe: &str,
    bars: &[CompactBar],
    cutoff_ts: i64,
    session_profile: Option<&SessionProfile>,
    params: Option<&KeyLevelParams>,
    derivative_points: &[DerivativePoint],
) -> Result<KeyLevelSet, OperatorError> {
    let default_params = KeyLevelParams::default();
    let effective = params.unwrap_or(&default_params);
    let computation = compute_key_levels_with_notes(
        instrument_id,
        timeframe,
        bars,
        cutoff_ts,
        session_profile,
        Some(effective),
        derivative_points,
    )?;
    Ok(KeyLevelSet {
        snapshot_id: snapshot_id.to_owned(),
        instrument_id: instrument_id.to_owned(),
        timeframe: timeframe.to_owned(),
        cutoff_ts,
        as_of_ts: cutoff_ts,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters: effective.as_parameters()?,
        notes: computation.notes,
        levels: computation.levels,
    })
}
