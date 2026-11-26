//! Deterministic, zero-I/O sizing preview matching Python `RUnitSizer` & `CryptoPerpMarginValuation`
//! character-for-character (ADR-0010 / spec D4).
//!
//! All intermediate arithmetic uses exact arbitrary-precision rationals over `BigDec` so floor
//! lot division, exact finite decimals, and 8-dp `ROUND_HALF_EVEN` outputs have zero drift vs
//! Python `fractions.Fraction`.

use core::cmp::Ordering;
use core::str::FromStr;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::decimal::{
    DecimalStr, format_normalized_decimal_str, serde_decimal_str, serde_decimal_str_opt,
};
use talaria_contracts::{Direction, MarginMode, SizingPreviewResponse};

use crate::gda::BigDec;

pub const DEFAULT_MAINTENANCE_RATE: &str = "0.005";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SizingPreviewInput {
    pub direction: Direction,
    #[serde(with = "serde_decimal_str")]
    pub entry_low: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub entry_high: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub invalidation_stop: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub qty_step: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub min_qty: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub contract_multiplier: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub equity: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub r_unit_pct: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub max_single_r_pct: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub friction_buffer_pct: DecimalStr,
    pub margin_mode: MarginMode,
    #[serde(with = "serde_decimal_str")]
    pub leverage: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub maintenance_rate: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub existing_round_risk: DecimalStr,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub existing_round_stop: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub override_quantity: Option<DecimalStr>,
}

/// Exact decimal number `(-1)^neg * mantissa * 10^(-scale)` backed by `BigDec`.
#[derive(Clone, Debug, PartialEq, Eq)]
struct ExactDec {
    neg: bool,
    mantissa: BigDec,
    scale: usize,
}

impl ExactDec {
    fn zero() -> Self {
        Self {
            neg: false,
            mantissa: BigDec::zero(),
            scale: 0,
        }
    }

    fn from_decimal(d: Decimal) -> Self {
        if d.is_zero() {
            return Self::zero();
        }
        let neg = d.is_sign_negative();
        let abs_mantissa = d.mantissa().unsigned_abs();
        let mut res = Self {
            neg,
            mantissa: BigDec::from_u128(abs_mantissa),
            scale: d.scale() as usize,
        };
        res.strip_trailing_zeros();
        res
    }

    fn from_i64(v: i64) -> Self {
        if v == 0 {
            return Self::zero();
        }
        Self {
            neg: v < 0,
            mantissa: BigDec::from_u128(v.unsigned_abs() as u128),
            scale: 0,
        }
    }

    fn is_zero(&self) -> bool {
        self.mantissa.is_zero()
    }

    fn is_negative(&self) -> bool {
        self.neg && !self.mantissa.is_zero()
    }

    fn strip_trailing_zeros(&mut self) {
        if self.mantissa.is_zero() {
            self.neg = false;
            self.scale = 0;
            return;
        }
        while self.scale > 0 {
            let (q, rem) = self.mantissa.div_mod_u32(10);
            if rem == 0 {
                self.mantissa = q;
                self.scale -= 1;
            } else {
                break;
            }
        }
    }

    fn align(a: &Self, b: &Self) -> (BigDec, BigDec, usize) {
        if a.scale == b.scale {
            (a.mantissa.clone(), b.mantissa.clone(), a.scale)
        } else if a.scale > b.scale {
            (a.mantissa.clone(), b.mantissa.mul_pow10(a.scale - b.scale), a.scale)
        } else {
            (a.mantissa.mul_pow10(b.scale - a.scale), b.mantissa.clone(), b.scale)
        }
    }

    fn cmp_exact(&self, other: &Self) -> Ordering {
        if self.is_zero() && other.is_zero() {
            return Ordering::Equal;
        }
        match (self.is_negative(), other.is_negative()) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            (false, false) => {
                let (a, b, _) = Self::align(self, other);
                a.cmp_val(&b)
            }
            (true, true) => {
                let (a, b, _) = Self::align(self, other);
                b.cmp_val(&a)
            }
        }
    }

    fn add(&self, other: &Self) -> Self {
        if self.is_zero() {
            return other.clone();
        }
        if other.is_zero() {
            return self.clone();
        }
        let (a, b, scale) = Self::align(self, other);
        let mut out = if self.neg == other.neg {
            Self {
                neg: self.neg,
                mantissa: a.add_val(&b),
                scale,
            }
        } else {
            match a.cmp_val(&b) {
                Ordering::Equal => Self::zero(),
                Ordering::Greater => Self {
                    neg: self.neg,
                    mantissa: a.sub_val(&b),
                    scale,
                },
                Ordering::Less => Self {
                    neg: other.neg,
                    mantissa: b.sub_val(&a),
                    scale,
                },
            }
        };
        out.strip_trailing_zeros();
        out
    }

    fn sub(&self, other: &Self) -> Self {
        let mut neg_other = other.clone();
        if !neg_other.is_zero() {
            neg_other.neg = !neg_other.neg;
        }
        self.add(&neg_other)
    }

    fn mul(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return Self::zero();
        }
        let mut out = Self {
            neg: self.neg ^ other.neg,
            mantissa: self.mantissa.mul_val(&other.mantissa),
            scale: self.scale + other.scale,
        };
        out.strip_trailing_zeros();
        out
    }

    fn to_plain_string(&self) -> String {
        if self.is_zero() {
            return "0".to_owned();
        }
        let digits = self.mantissa.to_ascii();
        let sign = if self.neg { "-" } else { "" };
        if self.scale == 0 {
            return format!("{sign}{digits}");
        }
        if digits.len() <= self.scale {
            let zeros = "0".repeat(self.scale - digits.len());
            format!("{sign}0.{zeros}{digits}")
        } else {
            let split = digits.len() - self.scale;
            format!("{sign}{}.{}", &digits[..split], &digits[split..])
        }
    }

    fn to_decimal(&self) -> Decimal {
        Decimal::from_str(&self.to_plain_string())
            .expect("sizing exact decimal fits rust_decimal")
            .normalize()
    }
}

/// Exact rational `num / den` over `ExactDec` (with `den > 0`).
#[derive(Clone, Debug)]
struct ExactRat {
    neg: bool,
    num: BigDec,
    den: BigDec,
}

impl ExactRat {
    fn from_exact_dec(d: &ExactDec) -> Self {
        if d.is_zero() {
            return Self {
                neg: false,
                num: BigDec::zero(),
                den: BigDec::from_u128(1),
            };
        }
        Self {
            neg: d.neg,
            num: d.mantissa.clone(),
            den: BigDec::from_u128(1).mul_pow10(d.scale),
        }
    }

    fn from_div(num: &ExactDec, den: &ExactDec) -> Self {
        debug_assert!(!den.is_zero());
        if num.is_zero() {
            return Self::from_exact_dec(&ExactDec::zero());
        }
        let neg = num.neg ^ den.neg;
        let (n, d) = if num.scale >= den.scale {
            (num.mantissa.clone(), den.mantissa.mul_pow10(num.scale - den.scale))
        } else {
            (num.mantissa.mul_pow10(den.scale - num.scale), den.mantissa.clone())
        };
        Self { neg, num: n, den: d }
    }

    fn sub(&self, other: &Self) -> Self {
        let mut neg_other = other.clone();
        if !neg_other.num.is_zero() {
            neg_other.neg = !neg_other.neg;
        }
        self.add(&neg_other)
    }

    fn add(&self, other: &Self) -> Self {
        let lhs = self.num.mul_val(&other.den);
        let rhs = other.num.mul_val(&self.den);
        let den = self.den.mul_val(&other.den);
        if self.neg == other.neg {
            let num = lhs.add_val(&rhs);
            let neg = if num.is_zero() { false } else { self.neg };
            Self { neg, num, den }
        } else {
            match lhs.cmp_val(&rhs) {
                Ordering::Equal => Self::from_exact_dec(&ExactDec::zero()),
                Ordering::Greater => Self {
                    neg: self.neg,
                    num: lhs.sub_val(&rhs),
                    den,
                },
                Ordering::Less => Self {
                    neg: other.neg,
                    num: rhs.sub_val(&lhs),
                    den,
                },
            }
        }
    }

    fn mul_sign(&self, s: i64) -> Self {
        if self.num.is_zero() {
            return self.clone();
        }
        Self {
            neg: if s < 0 { !self.neg } else { self.neg },
            num: self.num.clone(),
            den: self.den.clone(),
        }
    }

    fn div(&self, other: &Self) -> Self {
        debug_assert!(!other.num.is_zero());
        if self.num.is_zero() {
            return self.clone();
        }
        Self {
            neg: self.neg ^ other.neg,
            num: self.num.mul_val(&other.den),
            den: self.den.mul_val(&other.num),
        }
    }

    fn max_zero(self) -> Self {
        if self.neg {
            Self::from_exact_dec(&ExactDec::zero())
        } else {
            self
        }
    }

    /// Exact `ROUND_HALF_EVEN` to `max_places` decimal places, stripping trailing zeros.
    fn to_clean_decimal(&self, max_places: usize) -> Decimal {
        if self.num.is_zero() {
            return Decimal::ZERO;
        }
        let scaled_num = self.num.mul_pow10(max_places);
        let (mut q, rem) = scaled_num.div_rem(&self.den);
        let twice_rem = rem.mul_u32(2);
        let round_up = match twice_rem.cmp_val(&self.den) {
            Ordering::Greater => true,
            Ordering::Less => false,
            Ordering::Equal => q.is_odd(),
        };
        if round_up {
            q = q.add_val(&BigDec::from_u128(1));
        }
        let mut exact = ExactDec {
            neg: self.neg && !q.is_zero(),
            mantissa: q,
            scale: max_places,
        };
        exact.strip_trailing_zeros();
        exact.to_decimal()
    }
}

/// Exact floor division `floor(num / den)` for `num >= 0, den > 0`.
fn exact_floor_div(num: &ExactDec, den: &ExactDec) -> ExactDec {
    debug_assert!(!den.is_zero() && !den.is_negative());
    if num.is_zero() || num.is_negative() {
        return ExactDec::zero();
    }
    let (a, b, _) = ExactDec::align(num, den);
    let (q, _) = a.div_rem(&b);
    ExactDec {
        neg: false,
        mantissa: q,
        scale: 0,
    }
}

/// Exact divisibility check `(value % step) == 0` for `value > 0, step > 0`.
fn is_exact_multiple(value: &ExactDec, step: &ExactDec) -> bool {
    let (a, b, _) = ExactDec::align(value, step);
    let (_, rem) = a.div_rem(&b);
    rem.is_zero()
}

fn rejected(reason: &str) -> SizingPreviewResponse {
    SizingPreviewResponse {
        quantity: None,
        basis: None,
        risk_amount_r: None,
        risk_pct_of_equity: None,
        est_liq_price: None,
        est_liq_distance: None,
        est_liq_distance_pct: None,
        rejected_reason: Some(reason.to_owned()),
        from_offline_snapshot: false,
        based_on_ledger_seq: None,
    }
}

fn compute_isolated_liquidation(
    direction: Direction,
    entry: &ExactDec,
    stop: &ExactDec,
    leverage: &ExactDec,
    maintenance_rate: &ExactDec,
    margin_mode: MarginMode,
) -> (Option<Decimal>, Option<Decimal>, Option<Decimal>) {
    if margin_mode != MarginMode::Isolated {
        return (None, None, None);
    }
    let s = match direction {
        Direction::Long => 1_i64,
        Direction::Short => -1_i64,
    };
    let s_rat = ExactRat::from_exact_dec(&ExactDec::from_i64(s));
    let entry_rat = ExactRat::from_exact_dec(entry);
    let stop_rat = ExactRat::from_exact_dec(stop);
    let mmr_rat = ExactRat::from_exact_dec(maintenance_rate);

    let s_entry = entry_rat.mul_sign(s);
    let margin_per_unit = ExactRat::from_div(entry, leverage);
    let num = s_entry.sub(&margin_per_unit);
    let den = s_rat.sub(&mmr_rat);
    let liq_rat = num.div(&den).max_zero();

    let liq_price = liq_rat.to_clean_decimal(8);
    let dist_rat = stop_rat.sub(&liq_rat).mul_sign(s);
    let liq_dist = dist_rat.to_clean_decimal(8);
    let liq_dist_pct = dist_rat.div(&entry_rat).to_clean_decimal(8);
    (Some(liq_price), Some(liq_dist), Some(liq_dist_pct))
}

/// Pure, zero-I/O sizing preview matching Python `compute_authoritative_sizing_preview` bit-for-bit.
pub fn compute_sizing_preview(input: &SizingPreviewInput) -> SizingPreviewResponse {
    if input.qty_step <= Decimal::ZERO
        || input.min_qty <= Decimal::ZERO
        || input.contract_multiplier <= Decimal::ZERO
    {
        return rejected("INVALID_INSTRUMENT_SPEC");
    }
    if input.equity <= Decimal::ZERO {
        return rejected("NON_POSITIVE_EQUITY");
    }

    let stop_dec = input.existing_round_stop.unwrap_or(input.invalidation_stop);
    let valid_geometry = input.entry_low > Decimal::ZERO
        && input.entry_low <= input.entry_high
        && stop_dec > Decimal::ZERO
        && match input.direction {
            Direction::Long => stop_dec < input.entry_low,
            Direction::Short => stop_dec > input.entry_high,
        };
    if !valid_geometry {
        return rejected("INVALID_STOP_GEOMETRY");
    }

    let entry_dec = match input.direction {
        Direction::Long => input.entry_high,
        Direction::Short => input.entry_low,
    };
    let s = match input.direction {
        Direction::Long => 1_i64,
        Direction::Short => -1_i64,
    };

    let entry = ExactDec::from_decimal(entry_dec);
    let stop = ExactDec::from_decimal(stop_dec);
    let friction = ExactDec::from_decimal(input.friction_buffer_pct);
    let multiplier = ExactDec::from_decimal(input.contract_multiplier);
    let equity = ExactDec::from_decimal(input.equity);
    let r_unit_pct = ExactDec::from_decimal(input.r_unit_pct);
    let max_single_r_pct = ExactDec::from_decimal(input.max_single_r_pct);
    let qty_step = ExactDec::from_decimal(input.qty_step);
    let min_qty = ExactDec::from_decimal(input.min_qty);
    let existing_risk = ExactDec::from_decimal(input.existing_round_risk);
    let leverage = ExactDec::from_decimal(input.leverage);
    let maintenance_rate = ExactDec::from_decimal(input.maintenance_rate);

    let price_diff = ExactDec::from_i64(s).mul(&entry.sub(&stop));
    let friction_term = entry.mul(&friction);
    let unit_risk_raw = price_diff.add(&friction_term);
    let unit_risk_base = if unit_risk_raw.is_negative() {
        ExactDec::zero()
    } else {
        unit_risk_raw
    };
    let per_unit = unit_risk_base.mul(&multiplier);

    let is_scaling = input.existing_round_stop.is_some() || input.existing_round_risk > Decimal::ZERO;

    if let Some(override_raw) = input.override_quantity {
        let qty = ExactDec::from_decimal(override_raw);
        if qty.is_zero() || qty.is_negative() || qty.cmp_exact(&min_qty) == Ordering::Less {
            return rejected("BELOW_MIN_QUANTITY");
        }
        if !is_exact_multiple(&qty, &qty_step) {
            return rejected("INVALID_QUANTITY_STEP");
        }
        let risk_amount = per_unit.mul(&qty);
        let total_risk = existing_risk.add(&risk_amount);
        let risk_pct = ExactRat::from_div(&total_risk, &equity).to_clean_decimal(8);
        let max_allowed = equity.mul(&max_single_r_pct);
        let basis = format!(
            "manual_quantity={}, worst_entry={}, stop={}, risk_per_unit={}",
            qty.to_plain_string(),
            entry.to_plain_string(),
            stop.to_plain_string(),
            per_unit.to_plain_string()
        );
        let (est_liq_price, est_liq_distance, est_liq_distance_pct) = compute_isolated_liquidation(
            input.direction,
            &entry,
            &stop,
            &leverage,
            &maintenance_rate,
            input.margin_mode,
        );
        let rejected_reason = if total_risk.cmp_exact(&max_allowed) == Ordering::Greater {
            Some("EXCEEDS_MAX_SINGLE_R".to_owned())
        } else {
            None
        };
        return SizingPreviewResponse {
            quantity: Some(qty.to_decimal()),
            basis: Some(basis),
            risk_amount_r: Some(risk_amount.to_decimal()),
            risk_pct_of_equity: Some(risk_pct),
            est_liq_price,
            est_liq_distance,
            est_liq_distance_pct,
            rejected_reason,
            from_offline_snapshot: false,
            based_on_ledger_seq: None,
        };
    }

    let mut budget = equity.mul(&r_unit_pct);
    if is_scaling {
        let remaining = equity.mul(&max_single_r_pct).sub(&existing_risk);
        if remaining.cmp_exact(&budget) == Ordering::Less {
            budget = remaining;
        }
    }
    if budget.is_zero() || budget.is_negative() {
        return rejected("BELOW_MIN_QUANTITY");
    }

    let step_risk = per_unit.mul(&qty_step);
    if step_risk.is_zero() || step_risk.is_negative() {
        return rejected("INVALID_STOP_GEOMETRY");
    }
    let lots = exact_floor_div(&budget, &step_risk);
    let qty = lots.mul(&qty_step);
    if qty.is_zero() || qty.is_negative() || qty.cmp_exact(&min_qty) == Ordering::Less {
        return rejected("BELOW_MIN_QUANTITY");
    }

    let risk_amount = per_unit.mul(&qty);
    let total_risk = existing_risk.add(&risk_amount);
    let risk_pct = ExactRat::from_div(&total_risk, &equity).to_clean_decimal(8);
    let basis = format!(
        "risk_budget={}, worst_entry={}, stop={}, risk_per_unit={}",
        budget.to_plain_string(),
        entry.to_plain_string(),
        stop.to_plain_string(),
        per_unit.to_plain_string()
    );
    let (est_liq_price, est_liq_distance, est_liq_distance_pct) = compute_isolated_liquidation(
        input.direction,
        &entry,
        &stop,
        &leverage,
        &maintenance_rate,
        input.margin_mode,
    );

    SizingPreviewResponse {
        quantity: Some(qty.to_decimal()),
        basis: Some(basis),
        risk_amount_r: Some(risk_amount.to_decimal()),
        risk_pct_of_equity: Some(risk_pct),
        est_liq_price,
        est_liq_distance,
        est_liq_distance_pct,
        rejected_reason: None,
        from_offline_snapshot: false,
        based_on_ledger_seq: None,
    }
}

/// Convenience helper for formatting normalized `Decimal` fields in test assertions.
pub fn format_opt_normalized(val: Option<Decimal>) -> Option<String> {
    val.map(format_normalized_decimal_str)
}
