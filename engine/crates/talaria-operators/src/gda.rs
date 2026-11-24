//! Exact IBM General Decimal Arithmetic (`prec = 28`, `ROUND_HALF_EVEN`) over
//! base-`10^9` unsigned integer limbs, lossless-bridged to `rust_decimal::Decimal`.
//!
//! Python's `foundation/operators/core.py` runs under `Context(prec=28, rounding=ROUND_HALF_EVEN)`.
//! Native `rust_decimal::Decimal` uses a 96-bit binary mantissa (up to 29 digits when
//! leading digits <= 7, and max scale 28), which differs from Python `prec=28` on repeating
//! fractions such as `5/3` or `EMA` with `period=6`. This module reproduces Python's exact
//! significant-digit rounding and trailing-zero scale semantics with zero `f64`.

use core::cmp::Ordering;
use core::fmt;
use rust_decimal::Decimal;

use crate::error::OperatorError;

const BASE: u64 = 1_000_000_000;
const BASE_DIGITS: usize = 9;
pub const OPERATOR_PREC: usize = 28;

const POW10_U32: [u32; 10] = [
    1,
    10,
    100,
    1_000,
    10_000,
    100_000,
    1_000_000,
    10_000_000,
    100_000_000,
    1_000_000_000,
];

/// Arbitrary-precision unsigned integer stored in base `10^9` little-endian limbs.
#[derive(Clone, PartialEq, Eq, Default)]
pub struct BigDec {
    limbs: Vec<u32>,
}

impl fmt::Debug for BigDec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "BigDec({})", self.to_ascii())
    }
}

impl BigDec {
    pub fn zero() -> Self {
        Self { limbs: Vec::new() }
    }

    pub fn from_u128(mut value: u128) -> Self {
        if value == 0 {
            return Self::zero();
        }
        let mut limbs = Vec::with_capacity(4);
        let base = BASE as u128;
        while value > 0 {
            limbs.push((value % base) as u32);
            value /= base;
        }
        Self { limbs }
    }

    pub fn is_zero(&self) -> bool {
        self.limbs.is_empty()
    }

    pub fn is_odd(&self) -> bool {
        self.limbs.first().is_some_and(|limb| (limb & 1) == 1)
    }

    pub fn digits(&self) -> usize {
        if self.limbs.is_empty() {
            return 0;
        }
        let top = *self.limbs.last().expect("non-empty limbs");
        (self.limbs.len() - 1) * BASE_DIGITS + decimal_len_u32(top)
    }

    fn normalize(&mut self) {
        while self.limbs.last() == Some(&0) {
            self.limbs.pop();
        }
    }

    pub fn cmp_val(&self, other: &Self) -> Ordering {
        if self.limbs.len() != other.limbs.len() {
            return self.limbs.len().cmp(&other.limbs.len());
        }
        for i in (0..self.limbs.len()).rev() {
            match self.limbs[i].cmp(&other.limbs[i]) {
                Ordering::Equal => {}
                ord => return ord,
            }
        }
        Ordering::Equal
    }

    pub fn add_val(&self, other: &Self) -> Self {
        let len = self.limbs.len().max(other.limbs.len());
        let mut out = Vec::with_capacity(len + 1);
        let mut carry: u64 = 0;
        for i in 0..len {
            let a = self.limbs.get(i).copied().unwrap_or(0) as u64;
            let b = other.limbs.get(i).copied().unwrap_or(0) as u64;
            let sum = a + b + carry;
            if sum >= BASE {
                out.push((sum - BASE) as u32);
                carry = 1;
            } else {
                out.push(sum as u32);
                carry = 0;
            }
        }
        if carry > 0 {
            out.push(carry as u32);
        }
        let mut res = Self { limbs: out };
        res.normalize();
        res
    }

    /// Requires `self >= other`.
    pub fn sub_val(&self, other: &Self) -> Self {
        let mut out = Vec::with_capacity(self.limbs.len());
        let mut borrow: i64 = 0;
        for i in 0..self.limbs.len() {
            let a = self.limbs[i] as i64;
            let b = other.limbs.get(i).copied().unwrap_or(0) as i64;
            let mut diff = a - b - borrow;
            if diff < 0 {
                diff += BASE as i64;
                borrow = 1;
            } else {
                borrow = 0;
            }
            out.push(diff as u32);
        }
        debug_assert_eq!(borrow, 0);
        let mut res = Self { limbs: out };
        res.normalize();
        res
    }

    pub fn mul_val(&self, other: &Self) -> Self {
        if self.is_zero() || other.is_zero() {
            return Self::zero();
        }
        let mut out = vec![0u64; self.limbs.len() + other.limbs.len()];
        for (i, &a) in self.limbs.iter().enumerate() {
            let mut carry: u64 = 0;
            let a64 = a as u64;
            for (j, &b) in other.limbs.iter().enumerate() {
                let acc = out[i + j] + a64 * (b as u64) + carry;
                out[i + j] = acc % BASE;
                carry = acc / BASE;
            }
            out[i + other.limbs.len()] += carry;
        }
        let mut carry = 0u64;
        let mut limbs = Vec::with_capacity(out.len());
        for v in out {
            let acc = v + carry;
            limbs.push((acc % BASE) as u32);
            carry = acc / BASE;
        }
        while carry > 0 {
            limbs.push((carry % BASE) as u32);
            carry /= BASE;
        }
        let mut res = Self { limbs };
        res.normalize();
        res
    }

    pub fn mul_u32(&self, k: u32) -> Self {
        if self.is_zero() || k == 0 {
            return Self::zero();
        }
        if k == 1 {
            return self.clone();
        }
        let mut out = Vec::with_capacity(self.limbs.len() + 1);
        let mut carry = 0u64;
        let k64 = k as u64;
        for &limb in &self.limbs {
            let prod = (limb as u64) * k64 + carry;
            out.push((prod % BASE) as u32);
            carry = prod / BASE;
        }
        if carry > 0 {
            out.push(carry as u32);
        }
        Self { limbs: out }
    }

    pub fn mul_pow10(&self, k: usize) -> Self {
        if self.is_zero() || k == 0 {
            return self.clone();
        }
        let whole_limbs = k / BASE_DIGITS;
        let rem_digits = k % BASE_DIGITS;
        let shifted = self.mul_u32(POW10_U32[rem_digits]);
        if whole_limbs == 0 {
            return shifted;
        }
        let mut limbs = Vec::with_capacity(whole_limbs + shifted.limbs.len());
        limbs.resize(whole_limbs, 0);
        limbs.extend_from_slice(&shifted.limbs);
        Self { limbs }
    }

    /// Returns `(self / 10^k, self % 10^k)`.
    pub fn div_mod_pow10(&self, k: usize) -> (Self, Self) {
        if self.is_zero() || k == 0 {
            return (self.clone(), Self::zero());
        }
        let whole_limbs = k / BASE_DIGITS;
        let rem_digits = k % BASE_DIGITS;
        if whole_limbs >= self.limbs.len() {
            return (Self::zero(), self.clone());
        }
        let divisor = POW10_U32[rem_digits];
        let mut rem_limbs = self.limbs[..whole_limbs].to_vec();
        let mut q_limbs = Vec::with_capacity(self.limbs.len() - whole_limbs);

        if divisor == 1 {
            q_limbs.extend_from_slice(&self.limbs[whole_limbs..]);
        } else {
            let mut carry: u64 = 0;
            let div64 = divisor as u64;
            let mut rev_q = vec![0u32; self.limbs.len() - whole_limbs];
            for idx in (whole_limbs..self.limbs.len()).rev() {
                let cur = carry * BASE + (self.limbs[idx] as u64);
                rev_q[idx - whole_limbs] = (cur / div64) as u32;
                carry = cur % div64;
            }
            q_limbs = rev_q;
            rem_limbs.push(carry as u32);
        }

        let mut q = Self { limbs: q_limbs };
        let mut r = Self { limbs: rem_limbs };
        q.normalize();
        r.normalize();
        (q, r)
    }

    pub fn div_mod_u32(&self, d: u32) -> (Self, u32) {
        debug_assert!(d > 0);
        if self.is_zero() {
            return (Self::zero(), 0);
        }
        let d64 = d as u64;
        let mut q_limbs = vec![0u32; self.limbs.len()];
        let mut carry = 0u64;
        for idx in (0..self.limbs.len()).rev() {
            let cur = carry * BASE + (self.limbs[idx] as u64);
            q_limbs[idx] = (cur / d64) as u32;
            carry = cur % d64;
        }
        let mut q = Self { limbs: q_limbs };
        q.normalize();
        (q, carry as u32)
    }

    /// Exact long division `(quotient, remainder)` in base `10^9` using per-limb binary search.
    pub fn div_rem(&self, divisor: &Self) -> (Self, Self) {
        debug_assert!(!divisor.is_zero());
        if self.cmp_val(divisor) == Ordering::Less {
            return (Self::zero(), self.clone());
        }
        if divisor.limbs.len() == 1 {
            let (q, r) = self.div_mod_u32(divisor.limbs[0]);
            return (q, Self::from_u128(r as u128));
        }

        let mut q_limbs = vec![0u32; self.limbs.len()];
        let mut rem = Self::zero();
        for idx in (0..self.limbs.len()).rev() {
            // rem = rem * BASE + self.limbs[idx]
            if !rem.is_zero() {
                rem.limbs.insert(0, self.limbs[idx]);
            } else if self.limbs[idx] != 0 {
                rem.limbs.push(self.limbs[idx]);
            }
            if rem.cmp_val(divisor) == Ordering::Less {
                continue;
            }
            let mut lo: u32 = 1;
            let mut hi: u32 = (BASE - 1) as u32;
            let mut best: u32 = 0;
            while lo <= hi {
                let mid = lo + (hi - lo) / 2;
                let prod = divisor.mul_u32(mid);
                match prod.cmp_val(&rem) {
                    Ordering::Less => {
                        best = mid;
                        lo = mid + 1;
                    }
                    Ordering::Equal => {
                        best = mid;
                        break;
                    }
                    Ordering::Greater => {
                        hi = mid - 1;
                    }
                }
            }
            if best > 0 {
                let prod = divisor.mul_u32(best);
                rem = rem.sub_val(&prod);
                q_limbs[idx] = best;
            }
        }
        let mut q = Self { limbs: q_limbs };
        q.normalize();
        rem.normalize();
        (q, rem)
    }

    pub fn add_one(&self) -> Self {
        if self.is_zero() {
            return Self { limbs: vec![1] };
        }
        let mut limbs = self.limbs.clone();
        let mut carry = 1u64;
        for limb in &mut limbs {
            let acc = (*limb as u64) + carry;
            if acc >= BASE {
                *limb = (acc - BASE) as u32;
                carry = 1;
            } else {
                *limb = acc as u32;
                carry = 0;
                break;
            }
        }
        if carry > 0 {
            limbs.push(carry as u32);
        }
        Self { limbs }
    }

    pub fn pow10(k: usize) -> Self {
        let whole_limbs = k / BASE_DIGITS;
        let rem_digits = k % BASE_DIGITS;
        let mut limbs = vec![0u32; whole_limbs + 1];
        limbs[whole_limbs] = POW10_U32[rem_digits];
        Self { limbs }
    }

    /// Returns exact `u128` value if `self < 2^96` (or `u128::MAX` check).
    pub fn to_u128(&self) -> Option<u128> {
        let mut acc = 0u128;
        let base = BASE as u128;
        for &limb in self.limbs.iter().rev() {
            acc = acc.checked_mul(base)?.checked_add(limb as u128)?;
        }
        Some(acc)
    }

    pub fn to_ascii(&self) -> String {
        if self.is_zero() {
            return "0".to_owned();
        }
        let mut out = format!("{}", self.limbs[self.limbs.len() - 1]);
        for &limb in self.limbs[..self.limbs.len() - 1].iter().rev() {
            out.push_str(&format!("{limb:09}"));
        }
        out
    }
}

fn decimal_len_u32(mut v: u32) -> usize {
    let mut len = 1;
    while v >= 10 {
        v /= 10;
        len += 1;
    }
    len
}

/// Exact IBM General Decimal Arithmetic value (`(-1)^sign * coeff * 10^exp`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GdaDecimal {
    pub sign: bool,
    pub coeff: BigDec,
    pub exp: i32,
}

impl GdaDecimal {
    pub fn zero() -> Self {
        Self {
            sign: false,
            coeff: BigDec::zero(),
            exp: 0,
        }
    }

    pub fn from_i64(value: i64) -> Self {
        Self {
            sign: value < 0,
            coeff: BigDec::from_u128(value.unsigned_abs() as u128),
            exp: 0,
        }
    }

    pub fn from_decimal(d: Decimal) -> Self {
        Self {
            sign: d.is_sign_negative(),
            coeff: BigDec::from_u128(d.mantissa().unsigned_abs()),
            exp: -(d.scale() as i32),
        }
    }

    pub fn to_decimal(&self) -> Result<Decimal, OperatorError> {
        if self.coeff.is_zero() {
            let scale = if self.exp >= 0 {
                0
            } else {
                ((-self.exp) as u32).min(28)
            };
            let mut out = Decimal::from_i128_with_scale(0, scale);
            if self.sign {
                out.set_sign_negative(true);
            }
            return Ok(out);
        }

        let mut coeff = self.coeff.clone();
        let mut exp = self.exp;

        if exp > 0 {
            let shift = exp as usize;
            if coeff.digits() + shift > 29 {
                return Err(OperatorError::DecimalOverflow("positive exponent exceeds Decimal range"));
            }
            coeff = coeff.mul_pow10(shift);
            exp = 0;
        } else if -exp > 28 {
            let drop = (-exp - 28) as usize;
            let (q, rem) = coeff.div_mod_pow10(drop);
            coeff = round_half_even_shifted(q, &rem, drop);
            exp = -28;
        }

        let scale = (-exp) as u32;
        let mag = coeff
            .to_u128()
            .ok_or(OperatorError::DecimalOverflow("mantissa exceeds u128"))?;
        // rust_decimal supports 96-bit unsigned mantissa: max = 2^96 - 1.
        const MAX_U96: u128 = (1u128 << 96) - 1;
        if mag > MAX_U96 {
            // If 29 digits exceeded 96 bits (e.g. unrounded input), round to 28 digits.
            let drop = coeff.digits().saturating_sub(OPERATOR_PREC);
            if drop > 0 && scale >= drop as u32 {
                let (q, rem) = coeff.div_mod_pow10(drop);
                let rounded = round_half_even_shifted(q, &rem, drop);
                let mag28 = rounded
                    .to_u128()
                    .ok_or(OperatorError::DecimalOverflow("mantissa exceeds u96"))?;
                let mut out = Decimal::from_i128_with_scale(mag28 as i128, scale - drop as u32);
                if self.sign {
                    out.set_sign_negative(true);
                }
                return Ok(out);
            }
            return Err(OperatorError::DecimalOverflow("mantissa exceeds 96 bits"));
        }

        let mut out = Decimal::from_i128_with_scale(mag as i128, scale);
        if self.sign {
            out.set_sign_negative(true);
        }
        Ok(out)
    }

    pub fn is_zero(&self) -> bool {
        self.coeff.is_zero()
    }

    pub fn is_positive(&self) -> bool {
        !self.sign && !self.coeff.is_zero()
    }

    pub fn neg(&self) -> Self {
        Self {
            sign: !self.sign,
            coeff: self.coeff.clone(),
            exp: self.exp,
        }
    }

    pub fn abs(&self) -> Self {
        Self {
            sign: false,
            coeff: self.coeff.clone(),
            exp: self.exp,
        }
    }

    /// Compare numerically (ignoring trailing zero scale differences, `-0 == +0`).
    pub fn cmp_numeric(&self, other: &Self) -> Ordering {
        if self.coeff.is_zero() && other.coeff.is_zero() {
            return Ordering::Equal;
        }
        if self.coeff.is_zero() {
            return if other.sign {
                Ordering::Greater
            } else {
                Ordering::Less
            };
        }
        if other.coeff.is_zero() {
            return if self.sign {
                Ordering::Less
            } else {
                Ordering::Greater
            };
        }
        match (self.sign, other.sign) {
            (true, false) => Ordering::Less,
            (false, true) => Ordering::Greater,
            (false, false) => cmp_abs_val(&self.coeff, self.exp, &other.coeff, other.exp),
            (true, true) => cmp_abs_val(&other.coeff, other.exp, &self.coeff, self.exp),
        }
    }

    /// Adjusted exponent (`floor(log10(|self|))`), matching Python `Decimal.adjusted()`.
    pub fn adjusted(&self) -> i32 {
        if self.coeff.is_zero() {
            return 0;
        }
        self.coeff.digits() as i32 + self.exp - 1
    }

    /// GDA addition under `prec = 28`, `ROUND_HALF_EVEN`.
    pub fn add(&self, other: &Self) -> Self {
        let min_exp = self.exp.min(other.exp);
        if self.coeff.is_zero() && other.coeff.is_zero() {
            return Self {
                sign: self.sign && other.sign,
                coeff: BigDec::zero(),
                exp: min_exp,
            };
        }

        // Fast-path sticky clamp when exponent separation is huge (> PREC + 3).
        let (a_coeff, a_exp, b_coeff, b_exp) =
            align_for_add(&self.coeff, self.exp, &other.coeff, other.exp);
        let target_exp = a_exp.min(b_exp);
        let a_aligned = a_coeff.mul_pow10((a_exp - target_exp) as usize);
        let b_aligned = b_coeff.mul_pow10((b_exp - target_exp) as usize);

        let (res_sign, res_coeff) = if self.sign == other.sign {
            (self.sign, a_aligned.add_val(&b_aligned))
        } else {
            match a_aligned.cmp_val(&b_aligned) {
                Ordering::Greater => (self.sign, a_aligned.sub_val(&b_aligned)),
                Ordering::Less => (other.sign, b_aligned.sub_val(&a_aligned)),
                Ordering::Equal => (false, BigDec::zero()),
            }
        };

        round_to_prec28(res_sign, res_coeff, target_exp)
    }

    /// GDA subtraction under `prec = 28`, `ROUND_HALF_EVEN`.
    pub fn sub(&self, other: &Self) -> Self {
        self.add(&other.neg())
    }

    /// GDA multiplication under `prec = 28`, `ROUND_HALF_EVEN`.
    pub fn mul(&self, other: &Self) -> Self {
        let sign = self.sign ^ other.sign;
        let exp = self.exp + other.exp;
        if self.coeff.is_zero() || other.coeff.is_zero() {
            return Self {
                sign,
                coeff: BigDec::zero(),
                exp,
            };
        }
        let prod = self.coeff.mul_val(&other.coeff);
        round_to_prec28(sign, prod, exp)
    }

    /// GDA division under `prec = 28`, `ROUND_HALF_EVEN`.
    pub fn div(&self, other: &Self) -> Result<Self, OperatorError> {
        if other.coeff.is_zero() {
            return Err(OperatorError::DivisionByZero);
        }
        let sign = self.sign ^ other.sign;
        let ideal_exp = self.exp - other.exp;
        if self.coeff.is_zero() {
            return Ok(Self {
                sign,
                coeff: BigDec::zero(),
                exp: ideal_exp,
            });
        }

        let d_a = self.coeff.digits() as i32;
        let d_b = other.coeff.digits() as i32;
        let cmp_norm = if d_b >= d_a {
            self.coeff
                .mul_pow10((d_b - d_a) as usize)
                .cmp_val(&other.coeff)
        } else {
            self.coeff
                .cmp_val(&other.coeff.mul_pow10((d_a - d_b) as usize))
        };

        // Choose shift so floor((a * 10^shift) / b) has exactly OPERATOR_PREC (28) digits.
        let shift = if cmp_norm != Ordering::Less {
            (OPERATOR_PREC as i32 - 1) + d_b - d_a
        } else {
            (OPERATOR_PREC as i32) + d_b - d_a
        };

        let (dividend, divisor) = if shift >= 0 {
            (self.coeff.mul_pow10(shift as usize), other.coeff.clone())
        } else {
            (self.coeff.clone(), other.coeff.mul_pow10((-shift) as usize))
        };

        let (mut q, rem) = dividend.div_rem(&divisor);
        let mut exp = ideal_exp - shift;

        if rem.is_zero() {
            while exp < ideal_exp {
                let (next_q, mod10) = q.div_mod_u32(10);
                if mod10 != 0 {
                    break;
                }
                q = next_q;
                exp += 1;
            }
            return Ok(Self {
                sign,
                coeff: q,
                exp,
            });
        }

        // Inexact: round q to OPERATOR_PREC digits using ROUND_HALF_EVEN.
        let twice_rem = rem.add_val(&rem);
        let round_up = match twice_rem.cmp_val(&divisor) {
            Ordering::Greater => true,
            Ordering::Equal => q.is_odd(),
            Ordering::Less => false,
        };
        if round_up {
            q = q.add_one();
            if q.digits() > OPERATOR_PREC {
                let (next_q, _) = q.div_mod_u32(10);
                q = next_q;
                exp += 1;
            }
        }

        Ok(Self {
            sign,
            coeff: q,
            exp,
        })
    }

    /// GDA square root under `prec = 28`, `ROUND_HALF_EVEN`.
    pub fn sqrt(&self) -> Result<Self, OperatorError> {
        if self.sign && !self.coeff.is_zero() {
            return Err(OperatorError::NegativeSqrt);
        }
        let ideal_exp = self.exp.div_euclid(2);
        if self.coeff.is_zero() {
            return Ok(Self {
                sign: self.sign,
                coeff: BigDec::zero(),
                exp: ideal_exp,
            });
        }

        let d = self.coeff.digits() as i32;
        // Choose shift k so (self.exp - k) is even and coeff * 10^k has 55 or 56 digits,
        // meaning floor(sqrt(scaled)) lies in [10^27, 10^28 - 1].
        let mut k = (2 * OPERATOR_PREC as i32 - 1) - d;
        if (self.exp - k).rem_euclid(2) != 0 {
            k += 1;
        }
        debug_assert!(k >= 0 || d > 56);

        let (scaled, lost_nonzero) = if k >= 0 {
            (self.coeff.mul_pow10(k as usize), false)
        } else {
            let (q, rem) = self.coeff.div_mod_pow10((-k) as usize);
            (q, !rem.is_zero())
        };
        let mut exp = (self.exp - k) / 2;

        // Binary search s in [10^27, 10^28) for largest s with s^2 <= scaled.
        let mut lo = BigDec::pow10(OPERATOR_PREC - 1);
        let mut hi = BigDec::pow10(OPERATOR_PREC);
        let mut best = lo.clone();
        while lo.cmp_val(&hi) == Ordering::Less {
            let diff = hi.sub_val(&lo);
            let (half, _) = diff.div_mod_u32(2);
            let mid = lo.add_val(&half);
            let sq = mid.mul_val(&mid);
            match sq.cmp_val(&scaled) {
                Ordering::Less => {
                    best = mid.clone();
                    lo = mid.add_one();
                }
                Ordering::Equal => {
                    best = mid;
                    break;
                }
                Ordering::Greater => {
                    hi = mid;
                }
            }
        }

        let sq = best.mul_val(&best);
        let rem = scaled.sub_val(&sq);

        if rem.is_zero() && !lost_nonzero {
            while exp < ideal_exp {
                let (next_s, mod10) = best.div_mod_u32(10);
                if mod10 != 0 {
                    break;
                }
                best = next_s;
                exp += 1;
            }
            return Ok(Self {
                sign: self.sign,
                coeff: best,
                exp,
            });
        }

        // For irrational sqrt: true root > best + 0.5 iff scaled > best^2 + best + 0.25,
        // i.e., rem > best (for integers, or rem == best && lost_nonzero).
        let round_up = match rem.cmp_val(&best) {
            Ordering::Greater => true,
            Ordering::Equal => lost_nonzero,
            Ordering::Less => false,
        };
        if round_up {
            best = best.add_one();
            if best.digits() > OPERATOR_PREC {
                let (next_s, _) = best.div_mod_u32(10);
                best = next_s;
                exp += 1;
            }
        }

        Ok(Self {
            sign: self.sign,
            coeff: best,
            exp,
        })
    }

    /// Quantize to `target_exp` (e.g. `-6` for `0.000001`, `-8` for `0.00000001`) using `ROUND_HALF_EVEN`.
    pub fn quantize_exp(&self, target_exp: i32) -> Result<Self, OperatorError> {
        if self.exp == target_exp {
            if self.coeff.digits() > OPERATOR_PREC {
                return Err(OperatorError::DecimalOverflow("quantize exceeds 28 digits"));
            }
            return Ok(self.clone());
        }
        if self.exp > target_exp {
            let shift = (self.exp - target_exp) as usize;
            if !self.coeff.is_zero() && self.coeff.digits() + shift > OPERATOR_PREC {
                return Err(OperatorError::DecimalOverflow("quantize exceeds 28 digits"));
            }
            return Ok(Self {
                sign: self.sign,
                coeff: self.coeff.mul_pow10(shift),
                exp: target_exp,
            });
        }
        let drop = (target_exp - self.exp) as usize;
        if self.coeff.is_zero() {
            return Ok(Self {
                sign: self.sign,
                coeff: BigDec::zero(),
                exp: target_exp,
            });
        }
        let (q, rem) = self.coeff.div_mod_pow10(drop);
        let rounded = round_half_even_shifted(q, &rem, drop);
        if rounded.digits() > OPERATOR_PREC {
            return Err(OperatorError::DecimalOverflow("quantize exceeds 28 digits"));
        }
        Ok(Self {
            sign: self.sign,
            coeff: rounded,
            exp: target_exp,
        })
    }

    /// `ROUND_FLOOR` to integer value (matches Python `Decimal.to_integral_value(rounding=ROUND_FLOOR)`).
    pub fn floor_to_integral(&self) -> Self {
        if self.coeff.is_zero() || self.exp >= 0 {
            return Self {
                sign: self.sign,
                coeff: self.coeff.mul_pow10(self.exp.max(0) as usize),
                exp: 0,
            };
        }
        let drop = (-self.exp) as usize;
        let (q, rem) = self.coeff.div_mod_pow10(drop);
        let out_coeff = if self.sign && !rem.is_zero() {
            q.add_one()
        } else {
            q
        };
        Self {
            sign: self.sign,
            coeff: out_coeff,
            exp: 0,
        }
    }

    pub fn to_i64_floor(&self) -> Result<i64, OperatorError> {
        let floored = self.floor_to_integral();
        let mag = floored
            .coeff
            .to_u128()
            .ok_or(OperatorError::DecimalOverflow("integer overflow"))?;
        if floored.sign {
            if mag == (i64::MAX as u128) + 1 {
                Ok(i64::MIN)
            } else if mag <= i64::MAX as u128 {
                Ok(-(mag as i64))
            } else {
                Err(OperatorError::DecimalOverflow("i64 underflow"))
            }
        } else if mag <= i64::MAX as u128 {
            Ok(mag as i64)
        } else {
            Err(OperatorError::DecimalOverflow("i64 overflow"))
        }
    }
}

fn cmp_abs_val(a: &BigDec, a_exp: i32, b: &BigDec, b_exp: i32) -> Ordering {
    let min_exp = a_exp.min(b_exp);
    let a_s = a.mul_pow10((a_exp - min_exp) as usize);
    let b_s = b.mul_pow10((b_exp - min_exp) as usize);
    a_s.cmp_val(&b_s)
}

fn align_for_add(a: &BigDec, a_exp: i32, b: &BigDec, b_exp: i32) -> (BigDec, i32, BigDec, i32) {
    if a.is_zero() {
        return (BigDec::zero(), a_exp.max(b_exp - 40), b.clone(), b_exp);
    }
    if b.is_zero() {
        return (a.clone(), a_exp, BigDec::zero(), b_exp.max(a_exp - 40));
    }
    let a_top = a.digits() as i32 + a_exp;
    let b_top = b.digits() as i32 + b_exp;
    let min_allowed_exp = a_top.max(b_top) - (OPERATOR_PREC as i32 + 4);
    let (ac, ae) = clamp_low_digits(a, a_exp, min_allowed_exp);
    let (bc, be) = clamp_low_digits(b, b_exp, min_allowed_exp);
    (ac, ae, bc, be)
}

fn clamp_low_digits(c: &BigDec, exp: i32, min_exp: i32) -> (BigDec, i32) {
    if exp >= min_exp {
        return (c.clone(), exp);
    }
    let drop = (min_exp - exp) as usize;
    let (q, rem) = c.div_mod_pow10(drop);
    let sticky = if !rem.is_zero() && !q.is_zero() {
        // Ensure a sticky non-zero digit at the bottom if truncated.
        let mut s = q.mul_pow10(1);
        s = s.add_one();
        return (s, min_exp - 1);
    } else if !rem.is_zero() {
        return (BigDec::from_u128(1), min_exp - 1);
    } else {
        q
    };
    (sticky, min_exp)
}

fn round_half_even_shifted(q: BigDec, rem: &BigDec, drop: usize) -> BigDec {
    if rem.is_zero() {
        return q;
    }
    let half = BigDec::pow10(drop - 1).mul_u32(5);
    let round_up = match rem.cmp_val(&half) {
        Ordering::Greater => true,
        Ordering::Equal => q.is_odd(),
        Ordering::Less => false,
    };
    if round_up { q.add_one() } else { q }
}

fn round_to_prec28(sign: bool, coeff: BigDec, exp: i32) -> GdaDecimal {
    if coeff.is_zero() {
        return GdaDecimal { sign, coeff, exp };
    }
    let d = coeff.digits();
    if d <= OPERATOR_PREC {
        return GdaDecimal { sign, coeff, exp };
    }
    let drop = d - OPERATOR_PREC;
    let (q, rem) = coeff.div_mod_pow10(drop);
    let mut rounded = round_half_even_shifted(q, &rem, drop);
    let mut new_exp = exp + drop as i32;
    if rounded.digits() > OPERATOR_PREC {
        let (next_q, _) = rounded.div_mod_u32(10);
        rounded = next_q;
        new_exp += 1;
    }
    GdaDecimal {
        sign,
        coeff: rounded,
        exp: new_exp,
    }
}
