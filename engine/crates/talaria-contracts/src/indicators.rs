//! Indicator identifiers, parameter defaults, and fast-path indicator bundles.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str_opt, serde_decimal_str_opt_map};

pub const MAX_INDICATOR_PERIOD: u32 = 5000;

/// Supported technical and derivative indicators sharing the deterministic operator core.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum IndicatorName {
    Ema,
    Sma,
    Rsi,
    Atr,
    Boll,
    Vwap,
    FundingQuantile,
    OiDivergence,
    NetOdds,
}

impl IndicatorName {
    pub const fn default_period(self) -> u32 {
        match self {
            Self::Ema | Self::Sma => 9,
            Self::Rsi | Self::Atr => 14,
            Self::Boll | Self::NetOdds => 20,
            Self::Vwap => 1,
            Self::FundingQuantile | Self::OiDivergence => 30,
        }
    }

    pub const fn channels(self) -> &'static [&'static str] {
        match self {
            Self::Boll => &["mid", "upper", "lower"],
            _ => &["value"],
        }
    }

    pub const fn pane(self) -> IndicatorPane {
        match self {
            Self::Rsi
            | Self::Atr
            | Self::FundingQuantile
            | Self::OiDivergence
            | Self::NetOdds => IndicatorPane::Sub,
            Self::Ema | Self::Sma | Self::Boll | Self::Vwap => IndicatorPane::Main,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum IndicatorPane {
    Main,
    Sub,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum VwapAnchorMode {
    Snapshot,
    Session,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorParams {
    pub period: u32,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub std_dev: Option<DecimalStr>,
    #[serde(default)]
    pub anchor: Option<VwapAnchorMode>,
}

impl IndicatorParams {
    pub fn defaults_for(name: IndicatorName) -> Self {
        Self {
            period: name.default_period(),
            std_dev: None,
            anchor: if name == IndicatorName::Vwap {
                Some(VwapAnchorMode::Session)
            } else {
                None
            },
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorSummary {
    #[serde(with = "serde_decimal_str_opt_map")]
    pub latest: BTreeMap<String, Option<DecimalStr>>,
    pub warmup_bars: usize,
    pub valid_points: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorPoint {
    pub timestamp: i64,
    #[serde(with = "serde_decimal_str_opt_map")]
    pub values: BTreeMap<String, Option<DecimalStr>>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorSeries {
    pub name: IndicatorName,
    pub pane: IndicatorPane,
    pub params: IndicatorParams,
    pub points: Vec<IndicatorPoint>,
    pub summary: IndicatorSummary,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorRequest {
    pub snapshot_id: String,
    pub timeframe: String,
    pub names: Vec<String>,
    #[serde(default)]
    pub params: Option<BTreeMap<String, IndicatorParams>>,
    #[serde(default)]
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct OperatorRequest {
    pub snapshot_id: String,
    pub timeframe: String,
    pub operator: IndicatorName,
    #[serde(default)]
    pub params: Option<IndicatorParams>,
    #[serde(default)]
    pub limit: Option<usize>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorBundle {
    pub snapshot_id: String,
    pub instrument_id: String,
    pub timeframe: String,
    pub series: Vec<IndicatorSeries>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IndicatorDefaultsResponse {
    pub defaults: BTreeMap<IndicatorName, IndicatorParams>,
}
