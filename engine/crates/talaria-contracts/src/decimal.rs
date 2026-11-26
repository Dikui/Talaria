//! Fixed-point `DecimalStr` parsing, formatting, and strict Serde adapters.

use core::fmt;
use core::str::FromStr;
use rust_decimal::Decimal;
use serde::de::{self, DeserializeSeed, MapAccess, SeqAccess, Visitor};
use serde::ser::{SerializeMap, SerializeSeq};
use serde::{Deserializer, Serializer};
use std::collections::BTreeMap;

use crate::error::ContractError;

/// Canonical regex for `DecimalStr` across Python, Rust, TypeScript, and JSON Schema.
pub const DECIMAL_PATTERN: &str = r"^-?(?:0|[1-9]\d*)(?:\.\d+)?$";

/// Fixed-point decimal domain alias (`rust_decimal::Decimal`).
pub type DecimalStr = Decimal;

/// Validate that a string strictly matches `DECIMAL_PATTERN` without scientific notation,
/// leading `+`, leading zeros (`01`), or dangling decimal points (`.5`, `1.`).
pub fn validate_decimal_str(raw: &str) -> Result<(), ContractError> {
    let unsigned = raw.strip_prefix('-').unwrap_or(raw);
    if unsigned.is_empty() {
        return Err(ContractError::InvalidDecimalStr(raw.to_owned()));
    }

    let (int_part, frac_part) = match unsigned.split_once('.') {
        Some((int_part, frac_part)) => (int_part, Some(frac_part)),
        None => (unsigned, None),
    };

    let int_bytes = int_part.as_bytes();
    let valid_int = match int_bytes {
        [b'0'] => true,
        [b'1'..=b'9', rest @ ..] => rest.iter().all(u8::is_ascii_digit),
        _ => false,
    };
    if !valid_int {
        return Err(ContractError::InvalidDecimalStr(raw.to_owned()));
    }

    if let Some(frac) = frac_part
        && (frac.is_empty() || !frac.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(ContractError::InvalidDecimalStr(raw.to_owned()));
    }

    Ok(())
}

/// Parse a `DecimalStr` enforcing `DECIMAL_PATTERN`.
pub fn parse_decimal_str(raw: &str) -> Result<Decimal, ContractError> {
    validate_decimal_str(raw)?;
    Decimal::from_str(raw).map_err(|_| ContractError::InvalidDecimalStr(raw.to_owned()))
}

/// Format a `Decimal` as a plain decimal string (never scientific notation).
pub fn format_decimal_str(value: Decimal) -> String {
    value.to_string()
}

/// Format a `Decimal` stripping redundant trailing fractional zeros (`RangeBar` wire format).
pub fn format_normalized_decimal_str(value: Decimal) -> String {
    if value.is_zero() {
        return "0".to_owned();
    }
    let text = value.to_string();
    if let Some((int_part, frac_part)) = text.split_once('.') {
        let trimmed = frac_part.trim_end_matches('0');
        if trimmed.is_empty() {
            int_part.to_owned()
        } else {
            format!("{int_part}.{trimmed}")
        }
    } else {
        text
    }
}

struct DecimalStrVisitor;

impl Visitor<'_> for DecimalStrVisitor {
    type Value = Decimal;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a DecimalStr string matching ^-?(?:0|[1-9]\\d*)(?:\\.\\d+)?$")
    }

    fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
        parse_decimal_str(v).map_err(E::custom)
    }
}

/// Serde adapter enforcing `DecimalStr` JSON string serialization and deserialization.
pub mod serde_decimal_str {
    use super::*;

    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&format_decimal_str(*value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        deserializer.deserialize_str(DecimalStrVisitor)
    }
}

/// Serde adapter for `RangeBar` normalized `DecimalStr` fields (strips trailing fractional zeros).
pub mod serde_decimal_str_normalized {
    use super::*;

    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&format_normalized_decimal_str(*value))
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        deserializer.deserialize_str(DecimalStrVisitor)
    }
}

struct OptionDecimalStrVisitor;

impl<'de> Visitor<'de> for OptionDecimalStrVisitor {
    type Value = Option<Decimal>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("null or a DecimalStr string")
    }

    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_str(DecimalStrVisitor).map(Some)
    }
}

/// Serde adapter for `Option<Decimal>` (`DecimalStr | null`).
pub mod serde_decimal_str_opt {
    use super::*;

    pub fn serialize<S: Serializer>(
        value: &Option<Decimal>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        match value {
            Some(v) => serializer.serialize_str(&format_decimal_str(*v)),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Decimal>, D::Error> {
        deserializer.deserialize_option(OptionDecimalStrVisitor)
    }
}

struct DecimalStrSeed;

impl<'de> DeserializeSeed<'de> for DecimalStrSeed {
    type Value = Decimal;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_str(DecimalStrVisitor)
    }
}

struct OptDecimalStrSeed;

impl<'de> DeserializeSeed<'de> for OptDecimalStrSeed {
    type Value = Option<Decimal>;

    fn deserialize<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_option(OptionDecimalStrVisitor)
    }
}

struct DecimalStrVecVisitor;

impl<'de> Visitor<'de> for DecimalStrVecVisitor {
    type Value = Vec<Decimal>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("an array of DecimalStr strings")
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
        let mut out = Vec::with_capacity(seq.size_hint().unwrap_or(0));
        while let Some(value) = seq.next_element_seed(DecimalStrSeed)? {
            out.push(value);
        }
        Ok(out)
    }
}

/// Serde adapter for `Vec<Decimal>` (`DecimalStr[]`).
pub mod serde_decimal_str_vec {
    use super::*;

    pub fn serialize<S: Serializer>(values: &[Decimal], serializer: S) -> Result<S::Ok, S::Error> {
        let mut seq = serializer.serialize_seq(Some(values.len()))?;
        for value in values {
            seq.serialize_element(&format_decimal_str(*value))?;
        }
        seq.end()
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Vec<Decimal>, D::Error> {
        deserializer.deserialize_seq(DecimalStrVecVisitor)
    }
}

struct DecimalStrOptMapVisitor;

impl<'de> Visitor<'de> for DecimalStrOptMapVisitor {
    type Value = BTreeMap<String, Option<Decimal>>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("a map of string to DecimalStr | null")
    }

    fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
        let mut out = BTreeMap::new();
        while let Some(key) = map.next_key::<String>()? {
            let parsed = map.next_value_seed(OptDecimalStrSeed)?;
            out.insert(key, parsed);
        }
        Ok(out)
    }
}

/// Serde adapter for `BTreeMap<String, Option<Decimal>>`.
pub mod serde_decimal_str_opt_map {
    use super::*;

    pub fn serialize<S: Serializer>(
        values: &BTreeMap<String, Option<Decimal>>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        let mut map = serializer.serialize_map(Some(values.len()))?;
        for (k, v) in values {
            match v {
                Some(d) => map.serialize_entry(k, &format_decimal_str(*d))?,
                None => map.serialize_entry(k, &Option::<&str>::None)?,
            }
        }
        map.end()
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<BTreeMap<String, Option<Decimal>>, D::Error> {
        deserializer.deserialize_map(DecimalStrOptMapVisitor)
    }
}

struct OptionDecimalStrVecVisitor;

impl<'de> Visitor<'de> for OptionDecimalStrVecVisitor {
    type Value = Option<Vec<Decimal>>;

    fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("null or an array of DecimalStr strings")
    }

    fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
        Ok(None)
    }

    fn visit_some<D: Deserializer<'de>>(self, deserializer: D) -> Result<Self::Value, D::Error> {
        deserializer.deserialize_seq(DecimalStrVecVisitor).map(Some)
    }
}

/// Serde adapter for `Option<Vec<Decimal>>` (`DecimalStr[] | null`).
pub mod serde_decimal_str_vec_opt {
    use super::*;

    pub fn serialize<S: Serializer>(
        values: &Option<Vec<Decimal>>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        match values {
            Some(items) => {
                let mut seq = serializer.serialize_seq(Some(items.len()))?;
                for value in items {
                    seq.serialize_element(&format_decimal_str(*value))?;
                }
                seq.end()
            }
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Vec<Decimal>>, D::Error> {
        deserializer.deserialize_option(OptionDecimalStrVecVisitor)
    }
}
