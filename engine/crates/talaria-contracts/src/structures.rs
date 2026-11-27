//! Unified Market Structure contracts: 4 geometry primitives, features, sets, and feedback.

use std::collections::BTreeMap;

use serde::{Deserialize, Deserializer, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str};
use crate::error::ContractError;
use crate::key_levels::EvidenceClass;

pub const STRUCTURE_METHOD_VERSION: &str = "structures@1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StructureFamily {
    Level,
    Zone,
    Trendline,
    Channel,
    Confluence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StructureCategory {
    Structure,
    Session,
    Gap,
    RoundNumber,
    Volume,
    Liquidation,
    Trend,
    Confluence,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum StructureStatus {
    Active,
    Broken,
    Flipped,
    Expired,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum PriceScaleMode {
    #[default]
    Linear,
    Log,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AnchorSource {
    Wick,
    Body,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureAnchor {
    pub ts: i64,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AnalysisWindow {
    pub from_ts: i64,
    pub to_ts: i64,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
pub enum SlopeLineKind {
    #[default]
    #[serde(rename = "SLOPE_LINE")]
    SlopeLine,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SlopeLineGeometry {
    #[serde(default)]
    pub kind: SlopeLineKind,
    pub anchor_start: StructureAnchor,
    pub anchor_end: StructureAnchor,
    #[serde(default)]
    pub scale: PriceScaleMode,
    #[serde(default = "default_true")]
    pub extend_right: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum StructureGeometry {
    #[serde(rename = "HORIZONTAL_LINE")]
    HorizontalLine {
        #[serde(with = "serde_decimal_str")]
        price: DecimalStr,
    },
    #[serde(rename = "HORIZONTAL_ZONE")]
    HorizontalZone {
        #[serde(with = "serde_decimal_str")]
        price_low: DecimalStr,
        #[serde(with = "serde_decimal_str")]
        price_high: DecimalStr,
        #[serde(default)]
        start_ts: Option<i64>,
        #[serde(default)]
        end_ts: Option<i64>,
    },
    #[serde(rename = "SLOPE_LINE")]
    SlopeLine {
        anchor_start: StructureAnchor,
        anchor_end: StructureAnchor,
        #[serde(default)]
        scale: PriceScaleMode,
        #[serde(default = "default_true")]
        extend_right: bool,
    },
    #[serde(rename = "CHANNEL")]
    Channel {
        upper: SlopeLineGeometry,
        lower: SlopeLineGeometry,
    },
}

impl StructureGeometry {
    pub fn validate(&self) -> Result<(), ContractError> {
        match self {
            Self::HorizontalLine { .. } => Ok(()),
            Self::HorizontalZone {
                price_low,
                price_high,
                start_ts,
                end_ts,
            } => {
                if price_low > price_high {
                    return Err(ContractError::Validation(
                        "HorizontalZone price_low cannot exceed price_high",
                    ));
                }
                if let (Some(s), Some(e)) = (start_ts, end_ts)
                    && s > e
                {
                    return Err(ContractError::Validation(
                        "HorizontalZone start_ts cannot exceed end_ts",
                    ));
                }
                Ok(())
            }
            Self::SlopeLine {
                anchor_start,
                anchor_end,
                scale,
                ..
            } => validate_slope(anchor_start, anchor_end, *scale),
            Self::Channel { upper, lower } => {
                validate_slope(&upper.anchor_start, &upper.anchor_end, upper.scale)?;
                validate_slope(&lower.anchor_start, &lower.anchor_end, lower.scale)?;
                if upper.scale != lower.scale {
                    return Err(ContractError::Validation(
                        "Channel upper and lower must use the same PriceScaleMode",
                    ));
                }
                Ok(())
            }
        }
    }
}

fn validate_slope(
    start: &StructureAnchor,
    end: &StructureAnchor,
    scale: PriceScaleMode,
) -> Result<(), ContractError> {
    if start.ts < 0 || end.ts < 0 || start.ts >= end.ts {
        return Err(ContractError::Validation(
            "SlopeLine anchor_start.ts must be strictly before anchor_end.ts",
        ));
    }
    if scale == PriceScaleMode::Log && (start.price <= DecimalStr::ZERO || end.price <= DecimalStr::ZERO) {
        return Err(ContractError::Validation(
            "SlopeLine LOG scale requires strictly positive anchor prices",
        ));
    }
    Ok(())
}

fn default_evidence_class() -> EvidenceClass {
    EvidenceClass::Observed
}

fn default_method_version() -> String {
    STRUCTURE_METHOD_VERSION.to_owned()
}

fn default_zero_decimal() -> DecimalStr {
    DecimalStr::ZERO
}

#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct StructureFeature {
    pub structure_id: String,
    pub instrument_id: String,
    pub detector_id: String,
    pub family: StructureFamily,
    pub category: StructureCategory,
    pub kind: String,
    #[serde(default = "default_evidence_class")]
    pub evidence_class: EvidenceClass,
    pub timeframe: String,
    #[serde(default)]
    pub source_timeframe: Option<String>,
    pub geometry: StructureGeometry,
    pub strength: f64,
    pub status: StructureStatus,
    pub ts_available: i64,
    pub formed_ts: i64,
    pub analysis_window: AnalysisWindow,
    #[serde(default)]
    pub touch_count: u64,
    #[serde(default)]
    pub touch_timestamps: Vec<i64>,
    #[serde(default)]
    pub last_touch_ts: Option<i64>,
    #[serde(default = "default_zero_decimal", with = "serde_decimal_str")]
    pub volume_weight: DecimalStr,
    #[serde(default)]
    pub constituent_ids: Vec<String>,
    #[serde(default = "default_method_version")]
    pub method_version: String,
    #[serde(default)]
    pub parameters: BTreeMap<String, String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawStructureFeature {
    structure_id: String,
    instrument_id: String,
    detector_id: String,
    family: StructureFamily,
    category: StructureCategory,
    kind: String,
    #[serde(default = "default_evidence_class")]
    evidence_class: EvidenceClass,
    timeframe: String,
    #[serde(default)]
    source_timeframe: Option<String>,
    geometry: StructureGeometry,
    strength: f64,
    status: StructureStatus,
    ts_available: i64,
    formed_ts: i64,
    analysis_window: AnalysisWindow,
    #[serde(default)]
    touch_count: u64,
    #[serde(default)]
    touch_timestamps: Vec<i64>,
    #[serde(default)]
    last_touch_ts: Option<i64>,
    #[serde(default = "default_zero_decimal", with = "serde_decimal_str")]
    volume_weight: DecimalStr,
    #[serde(default)]
    constituent_ids: Vec<String>,
    #[serde(default = "default_method_version")]
    method_version: String,
    #[serde(default)]
    parameters: BTreeMap<String, String>,
}

impl StructureFeature {
    pub fn validate(&self) -> Result<(), ContractError> {
        self.geometry.validate()?;
        if !(0.0..=1.0).contains(&self.strength) {
            return Err(ContractError::Validation(
                "StructureFeature strength must be in [0.0, 1.0]",
            ));
        }
        if self.analysis_window.from_ts > self.analysis_window.to_ts {
            return Err(ContractError::Validation(
                "analysis_window.from_ts cannot exceed to_ts",
            ));
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for StructureFeature {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawStructureFeature::deserialize(deserializer)?;
        let feature = Self {
            structure_id: raw.structure_id,
            instrument_id: raw.instrument_id,
            detector_id: raw.detector_id,
            family: raw.family,
            category: raw.category,
            kind: raw.kind,
            evidence_class: raw.evidence_class,
            timeframe: raw.timeframe,
            source_timeframe: raw.source_timeframe,
            geometry: raw.geometry,
            strength: raw.strength,
            status: raw.status,
            ts_available: raw.ts_available,
            formed_ts: raw.formed_ts,
            analysis_window: raw.analysis_window,
            touch_count: raw.touch_count,
            touch_timestamps: raw.touch_timestamps,
            last_touch_ts: raw.last_touch_ts,
            volume_weight: raw.volume_weight,
            constituent_ids: raw.constituent_ids,
            method_version: raw.method_version,
            parameters: raw.parameters,
        };
        feature.validate().map_err(serde::de::Error::custom)?;
        Ok(feature)
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureSet {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub cutoff_ts: i64,
    pub as_of_ts: i64,
    #[serde(default = "default_method_version")]
    pub method_version: String,
    #[serde(default)]
    pub detector_ids: Vec<String>,
    #[serde(default)]
    pub parameters: BTreeMap<String, String>,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default)]
    pub structures: Vec<StructureFeature>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureIgnoreRequest {
    pub instrument_id: String,
    pub structure_id: String,
    #[serde(default = "default_true")]
    pub ignored: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureIgnoreRecord {
    pub instrument_id: String,
    pub structure_id: String,
    pub ignored: bool,
    pub updated_ts: i64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureIgnoreList {
    pub instrument_id: String,
    #[serde(default)]
    pub entries: Vec<StructureIgnoreRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureAdoptionRequest {
    pub snapshot_id: String,
    pub timeframe: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureAdoptionRecord {
    pub instrument_id: String,
    pub structure_id: String,
    pub snapshot_id: String,
    pub drawing_id: String,
    pub adopted_ts: i64,
    pub timeframe: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StructureFeedbackSummary {
    pub instrument_id: String,
    #[serde(default)]
    pub adopted_structure_ids: Vec<String>,
    #[serde(default)]
    pub ignored_structure_ids: Vec<String>,
}
