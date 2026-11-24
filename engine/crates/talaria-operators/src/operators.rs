//! Deterministic technical & derivative indicator operators using exact GDA `prec = 28`
//! `ROUND_HALF_EVEN` decimal arithmetic backed by `rust_decimal::Decimal`.

use std::collections::{BTreeMap, VecDeque};

use rust_decimal::Decimal;
use talaria_contracts::{
    BarStatus, CompactBar, DerivativePoint, IndicatorName, IndicatorParams, IndicatorPoint,
    IndicatorSeries, IndicatorSummary, MAX_INDICATOR_PERIOD, VwapAnchorMode,
};

use crate::error::OperatorError;
use crate::gda::GdaDecimal;

const DAY_MS: i64 = 86_400_000;
const QUANTILE_EXP: i32 = -8;
const RATIO_EXP: i32 = -8;
const ODDS_EXP: i32 = -6;

pub type OperatorPoint = BTreeMap<String, Option<Decimal>>;

/// Derivative context aligned to a single bar (point-in-time by `ts_available`).
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct DerivativeSample {
    pub funding_rate: Option<Decimal>,
    pub funding_ts: Option<i64>,
    pub open_interest: Option<Decimal>,
    pub oi_ts: Option<i64>,
    pub mark_price: Option<Decimal>,
    pub mark_ts: Option<i64>,
}

/// Validated immutable parameter specification for an operator instance.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OperatorSpec {
    pub name: IndicatorName,
    pub period: u32,
    pub std_dev: Option<Decimal>,
    pub anchor: Option<VwapAnchorMode>,
}

impl OperatorSpec {
    /// Validate and construct an `OperatorSpec` with defaults applied per indicator.
    pub fn new(
        name: IndicatorName,
        period: Option<u32>,
        std_dev: Option<Decimal>,
        anchor: Option<VwapAnchorMode>,
    ) -> Result<Self, OperatorError> {
        let resolved_period = period.unwrap_or_else(|| name.default_period());
        if !(1..=MAX_INDICATOR_PERIOD).contains(&resolved_period) {
            return Err(OperatorError::InvalidPeriod {
                name: indicator_label(name),
                period: resolved_period,
                max: MAX_INDICATOR_PERIOD,
            });
        }

        let resolved_std_dev = if name == IndicatorName::Boll {
            if resolved_period < 2 {
                return Err(OperatorError::InvalidBollPeriod(resolved_period));
            }
            let sd = std_dev.unwrap_or_else(|| Decimal::from(2));
            if sd <= Decimal::ZERO {
                return Err(OperatorError::InvalidStdDev(sd.to_string()));
            }
            Some(sd)
        } else {
            if std_dev.is_some() {
                return Err(OperatorError::UnexpectedStdDev(
                    indicator_label(name).to_owned(),
                ));
            }
            None
        };

        let resolved_anchor = if name == IndicatorName::Vwap {
            Some(anchor.unwrap_or(VwapAnchorMode::Session))
        } else {
            if anchor.is_some() {
                return Err(OperatorError::UnexpectedAnchor(
                    indicator_label(name).to_owned(),
                ));
            }
            None
        };

        Ok(Self {
            name,
            period: resolved_period,
            std_dev: resolved_std_dev,
            anchor: resolved_anchor,
        })
    }

    pub fn from_params(
        name: IndicatorName,
        params: Option<&IndicatorParams>,
    ) -> Result<Self, OperatorError> {
        match params {
            None => Self::new(name, None, None, None),
            Some(p) => Self::new(name, Some(p.period), p.std_dev, p.anchor),
        }
    }

    pub fn to_contract_params(&self) -> IndicatorParams {
        IndicatorParams {
            period: self.period,
            std_dev: self.std_dev,
            anchor: self.anchor,
        }
    }
}

fn indicator_label(name: IndicatorName) -> &'static str {
    match name {
        IndicatorName::Ema => "EMA",
        IndicatorName::Sma => "SMA",
        IndicatorName::Rsi => "RSI",
        IndicatorName::Atr => "ATR",
        IndicatorName::Boll => "BOLL",
        IndicatorName::Vwap => "VWAP",
        IndicatorName::FundingQuantile => "FUNDING_QUANTILE",
        IndicatorName::OiDivergence => "OI_DIVERGENCE",
        IndicatorName::NetOdds => "NET_ODDS",
    }
}

fn is_derivative_indicator(name: IndicatorName) -> bool {
    matches!(
        name,
        IndicatorName::FundingQuantile | IndicatorName::OiDivergence | IndicatorName::NetOdds
    )
}

fn empty_point(name: IndicatorName) -> OperatorPoint {
    let mut map = BTreeMap::new();
    for &ch in name.channels() {
        map.insert(ch.to_owned(), None);
    }
    map
}

fn single_point(value: Option<Decimal>) -> OperatorPoint {
    let mut map = BTreeMap::new();
    map.insert("value".to_owned(), value);
    map
}

fn require_field(
    field: &'static str,
    val: Option<Decimal>,
    status: BarStatus,
) -> Result<GdaDecimal, OperatorError> {
    val.map(GdaDecimal::from_decimal)
        .ok_or_else(|| OperatorError::MissingBarField {
            field,
            status: format!("{status:?}"),
        })
}

fn true_range_gda(
    bar: &CompactBar,
    previous_close: &GdaDecimal,
) -> Result<GdaDecimal, OperatorError> {
    let high = require_field("high", bar.high, bar.status)?;
    let low = require_field("low", bar.low, bar.status)?;
    let r1 = high.sub(&low);
    let r2 = high.sub(previous_close).abs();
    let r3 = low.sub(previous_close).abs();
    Ok(max_gda(r1, max_gda(r2, r3)))
}

fn max_gda(a: GdaDecimal, b: GdaDecimal) -> GdaDecimal {
    if a.cmp_numeric(&b) != core::cmp::Ordering::Less {
        a
    } else {
        b
    }
}

#[derive(Debug, Clone)]
struct SmaEngine {
    period: usize,
    window: VecDeque<GdaDecimal>,
}

impl SmaEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            period: spec.period as usize,
            window: VecDeque::with_capacity(spec.period as usize + 1),
        }
    }

    fn reset(&mut self) {
        self.window.clear();
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        self.window.push_back(close);
        if self.window.len() > self.period {
            self.window.pop_front();
        }
        if self.window.len() < self.period {
            return Ok(single_point(None));
        }
        let mut sum = GdaDecimal::zero();
        for item in &self.window {
            sum = sum.add(item);
        }
        let denom = GdaDecimal::from_i64(self.period as i64);
        let avg = sum.div(&denom)?.to_decimal()?;
        Ok(single_point(Some(avg)))
    }
}

#[derive(Debug, Clone)]
struct EmaEngine {
    period: usize,
    alpha: GdaDecimal,
    one_minus_alpha: GdaDecimal,
    legacy: bool,
    seed: Vec<GdaDecimal>,
    ema: Option<GdaDecimal>,
}

impl EmaEngine {
    fn new(spec: &OperatorSpec, legacy: bool) -> Result<Self, OperatorError> {
        let two = GdaDecimal::from_i64(2);
        let denom = GdaDecimal::from_i64(spec.period as i64 + 1);
        let alpha = two.div(&denom)?;
        let one_minus_alpha = GdaDecimal::from_i64(1).sub(&alpha);
        Ok(Self {
            period: spec.period as usize,
            alpha,
            one_minus_alpha,
            legacy,
            seed: Vec::new(),
            ema: None,
        })
    }

    fn reset(&mut self) {
        self.seed.clear();
        self.ema = None;
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        let next = match &self.ema {
            None => {
                if self.legacy {
                    self.seed.push(close);
                    if self.seed.len() < self.period {
                        return Ok(single_point(None));
                    }
                    let mut sum = GdaDecimal::zero();
                    for item in &self.seed {
                        sum = sum.add(item);
                    }
                    let avg = sum.div(&GdaDecimal::from_i64(self.period as i64))?;
                    self.seed.clear();
                    avg
                } else {
                    close
                }
            }
            Some(prev) => {
                if self.legacy {
                    prev.add(&self.alpha.mul(&close.sub(prev)))
                } else {
                    self.alpha.mul(&close).add(&self.one_minus_alpha.mul(prev))
                }
            }
        };
        let dec = next.to_decimal()?;
        self.ema = Some(next);
        Ok(single_point(Some(dec)))
    }
}

#[derive(Debug, Clone)]
struct RsiEngine {
    period: usize,
    previous_close: Option<GdaDecimal>,
    gains: Vec<GdaDecimal>,
    losses: Vec<GdaDecimal>,
    avg_gain: Option<GdaDecimal>,
    avg_loss: Option<GdaDecimal>,
}

impl RsiEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            period: spec.period as usize,
            previous_close: None,
            gains: Vec::new(),
            losses: Vec::new(),
            avg_gain: None,
            avg_loss: None,
        }
    }

    fn reset(&mut self) {
        self.previous_close = None;
        self.gains.clear();
        self.losses.clear();
        self.avg_gain = None;
        self.avg_loss = None;
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        let previous = self.previous_close.take();
        self.previous_close = Some(close.clone());
        let Some(prev) = previous else {
            return Ok(single_point(None));
        };
        let change = close.sub(&prev);
        let zero = GdaDecimal::zero();
        let gain = if change.is_positive() {
            change.clone()
        } else {
            zero.clone()
        };
        let loss = if change.cmp_numeric(&zero) == core::cmp::Ordering::Less {
            change.neg()
        } else {
            zero
        };

        let period_dec = GdaDecimal::from_i64(self.period as i64);
        match (&self.avg_gain, &self.avg_loss) {
            (None, _) | (_, None) => {
                self.gains.push(gain);
                self.losses.push(loss);
                if self.gains.len() < self.period {
                    return Ok(single_point(None));
                }
                let mut sum_g = GdaDecimal::zero();
                for g in &self.gains {
                    sum_g = sum_g.add(g);
                }
                let mut sum_l = GdaDecimal::zero();
                for l in &self.losses {
                    sum_l = sum_l.add(l);
                }
                self.avg_gain = Some(sum_g.div(&period_dec)?);
                self.avg_loss = Some(sum_l.div(&period_dec)?);
                self.gains.clear();
                self.losses.clear();
            }
            (Some(ag), Some(al)) => {
                let weight = GdaDecimal::from_i64(self.period as i64 - 1);
                let next_ag = ag.mul(&weight).add(&gain).div(&period_dec)?;
                let next_al = al.mul(&weight).add(&loss).div(&period_dec)?;
                self.avg_gain = Some(next_ag);
                self.avg_loss = Some(next_al);
            }
        }

        let ag = self.avg_gain.as_ref().expect("avg_gain seeded");
        let al = self.avg_loss.as_ref().expect("avg_loss seeded");
        let total = ag.add(al);
        let rsi = if total.is_zero() {
            Decimal::from(50)
        } else {
            GdaDecimal::from_i64(100)
                .mul(ag)
                .div(&total)?
                .to_decimal()?
        };
        Ok(single_point(Some(rsi)))
    }
}

#[derive(Debug, Clone)]
struct AtrEngine {
    period: usize,
    alpha: GdaDecimal,
    one_minus_alpha: GdaDecimal,
    legacy: bool,
    previous_close: Option<GdaDecimal>,
    true_ranges: Vec<GdaDecimal>,
    atr: Option<GdaDecimal>,
}

impl AtrEngine {
    fn new(spec: &OperatorSpec, legacy: bool) -> Result<Self, OperatorError> {
        let alpha = GdaDecimal::from_i64(1).div(&GdaDecimal::from_i64(spec.period as i64))?;
        let one_minus_alpha = GdaDecimal::from_i64(1).sub(&alpha);
        Ok(Self {
            period: spec.period as usize,
            alpha,
            one_minus_alpha,
            legacy,
            previous_close: None,
            true_ranges: Vec::new(),
            atr: None,
        })
    }

    fn reset(&mut self) {
        self.previous_close = None;
        self.true_ranges.clear();
        self.atr = None;
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        let previous = self.previous_close.take();
        self.previous_close = Some(close);
        if self.legacy && previous.is_none() {
            return Ok(single_point(None));
        }
        let tr = match &previous {
            Some(prev) => true_range_gda(bar, prev)?,
            None => {
                let high = require_field("high", bar.high, bar.status)?;
                let low = require_field("low", bar.low, bar.status)?;
                high.sub(&low)
            }
        };

        let next = match &self.atr {
            None => {
                self.true_ranges.push(tr);
                if self.true_ranges.len() < self.period {
                    return Ok(single_point(None));
                }
                let mut sum = GdaDecimal::zero();
                for item in &self.true_ranges {
                    sum = sum.add(item);
                }
                let avg = sum.div(&GdaDecimal::from_i64(self.period as i64))?;
                self.true_ranges.clear();
                avg
            }
            Some(prev_atr) => {
                if self.legacy {
                    let period_dec = GdaDecimal::from_i64(self.period as i64);
                    let weight = GdaDecimal::from_i64(self.period as i64 - 1);
                    prev_atr.mul(&weight).add(&tr).div(&period_dec)?
                } else {
                    self.alpha.mul(&tr).add(&self.one_minus_alpha.mul(prev_atr))
                }
            }
        };
        let dec = next.to_decimal()?;
        self.atr = Some(next);
        Ok(single_point(Some(dec)))
    }
}

#[derive(Debug, Clone)]
struct BollEngine {
    period: usize,
    std_dev: GdaDecimal,
    window: VecDeque<GdaDecimal>,
}

impl BollEngine {
    fn new(spec: &OperatorSpec) -> Self {
        let sd = spec.std_dev.unwrap_or_else(|| Decimal::from(2));
        Self {
            period: spec.period as usize,
            std_dev: GdaDecimal::from_decimal(sd),
            window: VecDeque::with_capacity(spec.period as usize + 1),
        }
    }

    fn reset(&mut self) {
        self.window.clear();
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        self.window.push_back(close);
        if self.window.len() > self.period {
            self.window.pop_front();
        }
        if self.window.len() < self.period {
            return Ok(empty_point(IndicatorName::Boll));
        }

        let n = GdaDecimal::from_i64(self.period as i64);
        let mut sum = GdaDecimal::zero();
        for v in &self.window {
            sum = sum.add(v);
        }
        let middle = sum.div(&n)?;

        let mut sq_sum = GdaDecimal::zero();
        for v in &self.window {
            let diff = v.sub(&middle);
            sq_sum = sq_sum.add(&diff.mul(&diff));
        }
        let variance = sq_sum.div(&n)?;
        let deviation = variance.sqrt()?;
        let band = self.std_dev.mul(&deviation);
        let upper = middle.add(&band);
        let lower = middle.sub(&band);

        let mut map = BTreeMap::new();
        map.insert("mid".to_owned(), Some(middle.to_decimal()?));
        map.insert("upper".to_owned(), Some(upper.to_decimal()?));
        map.insert("lower".to_owned(), Some(lower.to_decimal()?));
        Ok(map)
    }
}

#[derive(Debug, Clone)]
struct VwapEngine {
    session_anchored: bool,
    cumulative_pv: GdaDecimal,
    cumulative_volume: GdaDecimal,
    day: Option<i64>,
    pub anchor_ts: Option<i64>,
}

impl VwapEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            session_anchored: spec.anchor == Some(VwapAnchorMode::Session),
            cumulative_pv: GdaDecimal::zero(),
            cumulative_volume: GdaDecimal::zero(),
            day: None,
            anchor_ts: None,
        }
    }

    fn reset(&mut self) {
        self.cumulative_pv = GdaDecimal::zero();
        self.cumulative_volume = GdaDecimal::zero();
        self.day = None;
        self.anchor_ts = None;
    }

    fn push(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        if self.session_anchored {
            let day = bar.timestamp.div_euclid(DAY_MS);
            match self.day {
                None => {
                    self.day = Some(day);
                    return Ok(single_point(None));
                }
                Some(prev_day) if day != prev_day => {
                    self.day = Some(day);
                    self.cumulative_pv = GdaDecimal::zero();
                    self.cumulative_volume = GdaDecimal::zero();
                    self.anchor_ts = Some(bar.timestamp);
                }
                Some(_) => {}
            }
            if self.anchor_ts.is_none() {
                return Ok(single_point(None));
            }
        }

        let volume = GdaDecimal::from_decimal(bar.volume.unwrap_or(Decimal::ZERO));
        if !volume.is_zero() {
            let high = require_field("high", bar.high, bar.status)?;
            let low = require_field("low", bar.low, bar.status)?;
            let close = require_field("close", bar.close, bar.status)?;
            let typical = high.add(&low).add(&close).div(&GdaDecimal::from_i64(3))?;
            self.cumulative_pv = self.cumulative_pv.add(&typical.mul(&volume));
            self.cumulative_volume = self.cumulative_volume.add(&volume);
        }

        if self.cumulative_volume.is_zero() {
            return Ok(single_point(None));
        }
        let vwap = self.cumulative_pv.div(&self.cumulative_volume)?.to_decimal()?;
        Ok(single_point(Some(vwap)))
    }
}

#[derive(Debug, Clone)]
struct FundingQuantileEngine {
    period: usize,
    rates: VecDeque<GdaDecimal>,
}

impl FundingQuantileEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            period: spec.period as usize,
            rates: VecDeque::with_capacity(spec.period as usize + 1),
        }
    }

    fn reset(&mut self) {
        self.rates.clear();
    }

    fn push(
        &mut self,
        _bar: &CompactBar,
        sample: &DerivativeSample,
    ) -> Result<OperatorPoint, OperatorError> {
        let Some(rate_dec) = sample.funding_rate else {
            return Ok(single_point(None));
        };
        let rate = GdaDecimal::from_decimal(rate_dec);
        self.rates.push_back(rate.clone());
        if self.rates.len() > self.period {
            self.rates.pop_front();
        }
        if self.rates.len() < self.period {
            return Ok(single_point(None));
        }
        let mut less = 0i64;
        let mut equal = 0i64;
        for value in &self.rates {
            match value.cmp_numeric(&rate) {
                core::cmp::Ordering::Less => less += 1,
                core::cmp::Ordering::Equal => equal += 1,
                core::cmp::Ordering::Greater => {}
            }
        }
        let half_equal = GdaDecimal::from_i64(equal).div(&GdaDecimal::from_i64(2))?;
        let rank = GdaDecimal::from_i64(less)
            .add(&half_equal)
            .div(&GdaDecimal::from_i64(self.rates.len() as i64))?;
        let quantized = rank.quantize_exp(QUANTILE_EXP)?.to_decimal()?;
        Ok(single_point(Some(quantized)))
    }
}

#[derive(Debug, Clone)]
struct OiDivergenceEngine {
    period: usize,
    window: VecDeque<(GdaDecimal, GdaDecimal)>,
}

impl OiDivergenceEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            period: spec.period as usize,
            window: VecDeque::with_capacity(spec.period as usize + 2),
        }
    }

    fn reset(&mut self) {
        self.window.clear();
    }

    fn push(
        &mut self,
        bar: &CompactBar,
        sample: &DerivativeSample,
    ) -> Result<OperatorPoint, OperatorError> {
        let Some(oi_dec) = sample.open_interest else {
            return Ok(single_point(None));
        };
        let oi = GdaDecimal::from_decimal(oi_dec);
        let close = require_field("close", bar.close, bar.status)?;
        self.window.push_back((close.clone(), oi.clone()));
        if self.window.len() > self.period + 1 {
            self.window.pop_front();
        }
        if self.window.len() < self.period + 1 {
            return Ok(single_point(None));
        }
        let (start_close, start_oi) = &self.window[0];
        if start_close.is_zero() || start_oi.is_zero() {
            return Ok(single_point(None));
        }
        let price_return = close.sub(start_close).div(start_close)?;
        let oi_return = oi.sub(start_oi).div(start_oi)?;
        let div = price_return
            .sub(&oi_return)
            .quantize_exp(RATIO_EXP)?
            .to_decimal()?;
        Ok(single_point(Some(div)))
    }
}

#[derive(Debug, Clone)]
struct NetOddsEngine {
    period: usize,
    previous_close: Option<GdaDecimal>,
    closes: VecDeque<(i64, GdaDecimal)>,
    true_ranges: VecDeque<GdaDecimal>,
    settlements: Vec<(i64, GdaDecimal)>,
}

impl NetOddsEngine {
    fn new(spec: &OperatorSpec) -> Self {
        Self {
            period: spec.period as usize,
            previous_close: None,
            closes: VecDeque::with_capacity(spec.period as usize + 2),
            true_ranges: VecDeque::with_capacity(spec.period as usize + 1),
            settlements: Vec::new(),
        }
    }

    fn reset(&mut self) {
        self.previous_close = None;
        self.closes.clear();
        self.true_ranges.clear();
        self.settlements.clear();
    }

    fn push(
        &mut self,
        bar: &CompactBar,
        sample: &DerivativeSample,
    ) -> Result<OperatorPoint, OperatorError> {
        let close = require_field("close", bar.close, bar.status)?;
        let previous = self.previous_close.take();
        let true_range = match &previous {
            Some(prev) => Some(true_range_gda(bar, prev)?),
            None => None,
        };
        self.previous_close = Some(close.clone());
        if let Some(tr) = true_range {
            self.true_ranges.push_back(tr);
            if self.true_ranges.len() > self.period {
                self.true_ranges.pop_front();
            }
        }
        self.closes.push_back((bar.timestamp, close.clone()));
        if self.closes.len() > self.period + 1 {
            self.closes.pop_front();
        }
        let (Some(funding_ts), Some(funding_rate_dec)) = (sample.funding_ts, sample.funding_rate)
        else {
            return Ok(single_point(None));
        };
        let settlement = (funding_ts, GdaDecimal::from_decimal(funding_rate_dec));
        if let Some(last) = self.settlements.last_mut()
            && last.0 == funding_ts
        {
            *last = settlement;
        } else {
            self.settlements.push(settlement);
        }

        if self.closes.len() < self.period + 1 || self.true_ranges.len() < self.period {
            return Ok(single_point(None));
        }

        let mut tr_sum = GdaDecimal::zero();
        for tr in &self.true_ranges {
            tr_sum = tr_sum.add(tr);
        }
        let atr = tr_sum.div(&GdaDecimal::from_i64(self.period as i64))?;
        if atr.is_zero() {
            return Ok(single_point(None));
        }

        let (start_ts, start_close) = (&self.closes[0].0, &self.closes[0].1);
        let mut rate_sum = GdaDecimal::zero();
        for (ts, rate) in &self.settlements {
            if ts > start_ts && *ts <= bar.timestamp {
                rate_sum = rate_sum.add(rate);
            }
        }
        let funding_cost = rate_sum.mul(&close);
        self.settlements.retain(|(ts, _)| ts > start_ts);
        let net = close
            .sub(start_close)
            .sub(&funding_cost)
            .div(&atr)?
            .quantize_exp(ODDS_EXP)?
            .to_decimal()?;
        Ok(single_point(Some(net)))
    }
}

#[derive(Debug, Clone)]
enum EngineKind {
    Sma(SmaEngine),
    Ema(EmaEngine),
    Rsi(RsiEngine),
    Atr(AtrEngine),
    Boll(BollEngine),
    Vwap(VwapEngine),
    FundingQuantile(FundingQuantileEngine),
    OiDivergence(OiDivergenceEngine),
    NetOdds(NetOddsEngine),
}

impl EngineKind {
    fn new(spec: &OperatorSpec, legacy: bool) -> Result<Self, OperatorError> {
        Ok(match spec.name {
            IndicatorName::Sma => Self::Sma(SmaEngine::new(spec)),
            IndicatorName::Ema => Self::Ema(EmaEngine::new(spec, legacy)?),
            IndicatorName::Rsi => Self::Rsi(RsiEngine::new(spec)),
            IndicatorName::Atr => Self::Atr(AtrEngine::new(spec, legacy)?),
            IndicatorName::Boll => Self::Boll(BollEngine::new(spec)),
            IndicatorName::Vwap => Self::Vwap(VwapEngine::new(spec)),
            IndicatorName::FundingQuantile => {
                Self::FundingQuantile(FundingQuantileEngine::new(spec))
            }
            IndicatorName::OiDivergence => Self::OiDivergence(OiDivergenceEngine::new(spec)),
            IndicatorName::NetOdds => Self::NetOdds(NetOddsEngine::new(spec)),
        })
    }

    fn reset(&mut self) {
        match self {
            Self::Sma(e) => e.reset(),
            Self::Ema(e) => e.reset(),
            Self::Rsi(e) => e.reset(),
            Self::Atr(e) => e.reset(),
            Self::Boll(e) => e.reset(),
            Self::Vwap(e) => e.reset(),
            Self::FundingQuantile(e) => e.reset(),
            Self::OiDivergence(e) => e.reset(),
            Self::NetOdds(e) => e.reset(),
        }
    }

    fn push_bar(&mut self, bar: &CompactBar) -> Result<OperatorPoint, OperatorError> {
        match self {
            Self::Sma(e) => e.push(bar),
            Self::Ema(e) => e.push(bar),
            Self::Rsi(e) => e.push(bar),
            Self::Atr(e) => e.push(bar),
            Self::Boll(e) => e.push(bar),
            Self::Vwap(e) => e.push(bar),
            Self::FundingQuantile(_) | Self::OiDivergence(_) | Self::NetOdds(_) => {
                unreachable!("derivative engine routed via push_derivative")
            }
        }
    }

    fn push_derivative(
        &mut self,
        bar: &CompactBar,
        sample: &DerivativeSample,
    ) -> Result<OperatorPoint, OperatorError> {
        match self {
            Self::FundingQuantile(e) => e.push(bar, sample),
            Self::OiDivergence(e) => e.push(bar, sample),
            Self::NetOdds(e) => e.push(bar, sample),
            _ => unreachable!("bar engine routed via push_bar"),
        }
    }
}

fn push_batch(
    engine: &mut EngineKind,
    bars: &[CompactBar],
    spec: &OperatorSpec,
    derivatives: Option<&[DerivativeSample]>,
    previous_point: Option<&OperatorPoint>,
) -> Result<Vec<OperatorPoint>, OperatorError> {
    let mut points = Vec::with_capacity(bars.len());
    if is_derivative_indicator(spec.name) {
        let samples = derivatives.ok_or_else(|| {
            OperatorError::MissingDerivativeSamples(indicator_label(spec.name).to_owned())
        })?;
        if samples.len() != bars.len() {
            return Err(OperatorError::DerivativeLengthMismatch {
                bars: bars.len(),
                samples: samples.len(),
            });
        }
        for (bar, sample) in bars.iter().zip(samples.iter()) {
            if bar.status == BarStatus::FeedGap {
                engine.reset();
                points.push(empty_point(spec.name));
                continue;
            }
            if bar.status == BarStatus::NoTrades {
                let carried = points
                    .last()
                    .or(previous_point)
                    .cloned()
                    .unwrap_or_else(|| empty_point(spec.name));
                points.push(carried);
                continue;
            }
            points.push(engine.push_derivative(bar, sample)?);
        }
        return Ok(points);
    }

    for bar in bars {
        if bar.status == BarStatus::FeedGap {
            engine.reset();
            points.push(empty_point(spec.name));
            continue;
        }
        points.push(engine.push_bar(bar)?);
    }
    Ok(points)
}

/// Incremental operator state; `points` strictly matches processed `bars` in length and order.
#[derive(Debug, Clone)]
pub struct OperatorState {
    pub spec: OperatorSpec,
    pub points: Vec<OperatorPoint>,
    engine: EngineKind,
}

impl OperatorState {
    pub fn merge(&self, other: &Self) -> Self {
        let mut merged = Vec::with_capacity(self.points.len() + other.points.len());
        merged.extend_from_slice(&self.points);
        merged.extend_from_slice(&other.points);
        Self {
            spec: self.spec.clone(),
            points: merged,
            engine: other.engine.clone(),
        }
    }
}

/// Stateful single-owner cursor advancing bar-by-bar with identical gap/warmup semantics.
#[derive(Debug, Clone)]
pub struct OperatorCursor {
    pub spec: OperatorSpec,
    engine: EngineKind,
    previous: Option<OperatorPoint>,
}

impl OperatorCursor {
    pub fn new(spec: OperatorSpec, legacy: bool) -> Result<Self, OperatorError> {
        if legacy && !matches!(spec.name, IndicatorName::Ema | IndicatorName::Atr) {
            return Err(OperatorError::UnsupportedLegacyOperator(
                indicator_label(spec.name).to_owned(),
            ));
        }
        let engine = EngineKind::new(&spec, legacy)?;
        Ok(Self {
            spec,
            engine,
            previous: None,
        })
    }

    pub fn append(
        &mut self,
        bars: &[CompactBar],
        samples: Option<&[DerivativeSample]>,
    ) -> Result<Vec<OperatorPoint>, OperatorError> {
        let points = push_batch(
            &mut self.engine,
            bars,
            &self.spec,
            samples,
            self.previous.as_ref(),
        )?;
        if let Some(last) = points.last() {
            self.previous = Some(last.clone());
        }
        Ok(points)
    }
}

/// Batch compute operator series over `bars` (output length == `bars.len()`).
pub fn compute_series(
    bars: &[CompactBar],
    spec: &OperatorSpec,
    derivatives: Option<&[DerivativeSample]>,
) -> Result<OperatorState, OperatorError> {
    let mut engine = EngineKind::new(spec, false)?;
    let points = push_batch(&mut engine, bars, spec, derivatives, None)?;
    Ok(OperatorState {
        spec: spec.clone(),
        points,
        engine,
    })
}

/// Incrementally extend `previous_state` with `new_bars`, matching batch output identically.
pub fn update_series(
    previous_state: &OperatorState,
    new_bars: &[CompactBar],
    spec: &OperatorSpec,
    derivatives: Option<&[DerivativeSample]>,
) -> Result<OperatorState, OperatorError> {
    if spec != &previous_state.spec {
        return Err(OperatorError::SpecMismatch);
    }
    if new_bars.is_empty() {
        return Ok(previous_state.clone());
    }
    let mut engine = previous_state.engine.clone();
    let new_points = push_batch(
        &mut engine,
        new_bars,
        spec,
        derivatives,
        previous_state.points.last(),
    )?;
    let mut points = Vec::with_capacity(previous_state.points.len() + new_points.len());
    points.extend_from_slice(&previous_state.points);
    points.extend(new_points);
    Ok(OperatorState {
        spec: spec.clone(),
        points,
        engine,
    })
}

/// Reproduce legacy alert seeding for `EMA` and `ATR` (matching Python `legacy_alert_points`).
pub fn legacy_alert_points(
    bars: &[CompactBar],
    spec: &OperatorSpec,
) -> Result<Vec<OperatorPoint>, OperatorError> {
    if !matches!(spec.name, IndicatorName::Ema | IndicatorName::Atr) {
        return Err(OperatorError::UnsupportedLegacyOperator(
            indicator_label(spec.name).to_owned(),
        ));
    }
    let mut engine = EngineKind::new(spec, true)?;
    push_batch(&mut engine, bars, spec, None, None)
}

/// Return the current session's first contributing window timestamp and VWAP value.
pub fn latest_session_vwap(bars: &[CompactBar]) -> Result<Option<(i64, Decimal)>, OperatorError> {
    let spec = OperatorSpec::new(
        IndicatorName::Vwap,
        None,
        None,
        Some(VwapAnchorMode::Session),
    )?;
    let mut engine = EngineKind::new(&spec, false)?;
    let points = push_batch(&mut engine, bars, &spec, None, None)?;
    let value = points
        .last()
        .and_then(|pt| pt.get("value"))
        .and_then(|v| *v);
    let anchor_ts = match &engine {
        EngineKind::Vwap(vwap) => vwap.anchor_ts,
        _ => None,
    };
    match (anchor_ts, value) {
        (Some(ts), Some(val)) => Ok(Some((ts, val))),
        _ => Ok(None),
    }
}

/// Align point-in-time `DerivativePoint` observations to each bar open (`ts_available <= as_of`).
pub fn derivative_samples(
    bars: &[CompactBar],
    funding: &[DerivativePoint],
    open_interest: &[DerivativePoint],
    mark_price: &[DerivativePoint],
    as_of: Option<&[i64]>,
) -> Result<Vec<DerivativeSample>, OperatorError> {
    let cutoffs: Vec<i64> = match as_of {
        Some(slice) => {
            if slice.len() != bars.len() {
                return Err(OperatorError::DerivativeLengthMismatch {
                    bars: bars.len(),
                    samples: slice.len(),
                });
            }
            slice.to_vec()
        }
        None => bars.iter().map(|b| b.timestamp).collect(),
    };

    let funding_aligned = align_at_bar_opens(bars, funding, &cutoffs);
    let oi_aligned = align_at_bar_opens(bars, open_interest, &cutoffs);
    let mark_aligned = align_at_bar_opens(bars, mark_price, &cutoffs);

    let mut out = Vec::with_capacity(bars.len());
    for i in 0..bars.len() {
        let f = funding_aligned[i];
        let o = oi_aligned[i];
        let m = mark_aligned[i];
        out.push(DerivativeSample {
            funding_rate: f.map(|p| p.value),
            funding_ts: f.map(|p| p.ts_event),
            open_interest: o.map(|p| p.value),
            oi_ts: o.map(|p| p.ts_event),
            mark_price: m.map(|p| p.value),
            mark_ts: m.map(|p| p.ts_event),
        });
    }
    Ok(out)
}

fn align_at_bar_opens<'a>(
    bars: &[CompactBar],
    points: &'a [DerivativePoint],
    cutoffs: &[i64],
) -> Vec<Option<&'a DerivativePoint>> {
    let mut by_ts: BTreeMap<i64, Vec<&'a DerivativePoint>> = BTreeMap::new();
    for pt in points {
        by_ts.entry(pt.ts_event).or_default().push(pt);
    }
    let mut timestamps = Vec::with_capacity(by_ts.len());
    let mut revisions = Vec::with_capacity(by_ts.len());
    for (ts, mut group) in by_ts {
        group.sort_by(|a, b| {
            (b.revision, b.ts_available, b.ts_ingested).cmp(&(
                a.revision,
                a.ts_available,
                a.ts_ingested,
            ))
        });
        timestamps.push(ts);
        revisions.push(group);
    }

    let mut out = Vec::with_capacity(bars.len());
    for (bar, &cutoff) in bars.iter().zip(cutoffs.iter()) {
        let pos = timestamps.partition_point(|&ts| ts <= bar.timestamp);
        let mut selected = None;
        for idx in (0..pos).rev() {
            if let Some(&found) = revisions[idx].iter().find(|p| p.ts_available <= cutoff) {
                selected = Some(found);
                break;
            }
        }
        out.push(selected);
    }
    out
}

/// Compute an `IndicatorSeries` contract payload (sliced to trailing `limit` bars if given).
pub fn compute_indicator_series(
    bars: &[CompactBar],
    spec: &OperatorSpec,
    limit: Option<usize>,
    derivatives: Option<&[DerivativeSample]>,
) -> Result<IndicatorSeries, OperatorError> {
    let state = compute_series(bars, spec, derivatives)?;
    let total = bars.len();
    let keep = limit.unwrap_or(total).min(total);
    let start = total - keep;

    let mut points = Vec::with_capacity(keep);
    for (bar, values) in bars[start..].iter().zip(state.points[start..].iter()) {
        points.push(IndicatorPoint {
            timestamp: bar.timestamp,
            values: values.clone(),
        });
    }

    let latest = points
        .last()
        .map(|p| p.values.clone())
        .unwrap_or_else(|| empty_point(spec.name));
    let warmup_bars = points
        .iter()
        .position(|p| p.values.values().all(Option::is_some))
        .unwrap_or(points.len());
    let valid_points = points
        .iter()
        .filter(|p| p.values.values().all(Option::is_some))
        .count();

    Ok(IndicatorSeries {
        name: spec.name,
        pane: spec.name.pane(),
        params: spec.to_contract_params(),
        points,
        summary: IndicatorSummary {
            latest,
            warmup_bars,
            valid_points,
        },
    })
}
