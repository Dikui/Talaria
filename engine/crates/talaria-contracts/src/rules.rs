//! Versioned JSON rule expression tree contracts.

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::derivatives::DerivativeKind;
use crate::indicators::{IndicatorPoint, VwapAnchorMode};
use crate::key_levels::KeyLevelKind;
use crate::structures::StructureFamily;

pub const MAX_RULE_DEPTH: usize = 5;
pub const MAX_RULE_NODES: usize = 64;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AlertMetric {
    LastPrice,
    BidPrice,
    AskPrice,
    MarkPrice,
    FundingRate,
    OpenInterest,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AlertDirection {
    Above,
    Below,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AlertOverlayMode {
    Touch,
    CloseBreak,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum AlertLevelEvent {
    Touch,
    Break,
    Retest,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleBarField {
    Open,
    High,
    Low,
    Close,
    Volume,
    Turnover,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleComparisonTest {
    Lt,
    Lte,
    Gt,
    Gte,
    Eq,
    CrossAbove,
    CrossBelow,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleEdge {
    Lower,
    #[default]
    Upper,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleIndicatorWindow {
    Chart,
    #[default]
    Alert,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum RuleIndicatorName {
    #[serde(rename = "EMA")]
    Ema,
    #[serde(rename = "SMA")]
    Sma,
    #[serde(rename = "RSI")]
    Rsi,
    #[serde(rename = "ATR")]
    Atr,
    #[serde(rename = "BOLL")]
    Boll,
    #[serde(rename = "VWAP")]
    Vwap,
    #[serde(rename = "FUNDING_QUANTILE")]
    FundingQuantile,
    #[serde(rename = "OI_DIVERGENCE")]
    OiDivergence,
    #[serde(rename = "NET_ODDS")]
    NetOdds,
    #[serde(rename = "legacy_ema")]
    LegacyEma,
    #[serde(rename = "legacy_atr")]
    LegacyAtr,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]
pub enum RuleVersion {
    #[default]
    #[serde(rename = "1")]
    V1,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RuleTiming {
    BarClose,
    Event,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RulePane {
    Main,
    Sub,
}

#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuleIndicatorParams {
    #[serde(default)]
    pub period: Option<u32>,
    #[serde(default)]
    pub anchor: Option<VwapAnchorMode>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub std_dev: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "source", deny_unknown_fields)]
pub enum RuleOperand {
    #[serde(rename = "constant")]
    Constant {
        #[serde(with = "serde_decimal_str")]
        value: DecimalStr,
    },
    #[serde(rename = "bar")]
    Bar { field: RuleBarField },
    #[serde(rename = "event")]
    Event { metric: AlertMetric },
    #[serde(rename = "derivative")]
    Derivative { kind: DerivativeKind },
    #[serde(rename = "indicator")]
    Indicator {
        name: RuleIndicatorName,
        #[serde(default)]
        version: RuleVersion,
        #[serde(default)]
        window: RuleIndicatorWindow,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        snapshot_id: Option<String>,
        #[serde(default)]
        channel: Option<String>,
        #[serde(default)]
        params: RuleIndicatorParams,
    },
    #[serde(rename = "drawing_price")]
    DrawingPrice {
        drawing_id: String,
        #[serde(default)]
        edge: RuleEdge,
        #[serde(default, with = "serde_decimal_str_opt")]
        ratio: Option<DecimalStr>,
    },
    #[serde(rename = "level_price")]
    LevelPrice {
        level_id: String,
        #[serde(default)]
        level_kind: Option<KeyLevelKind>,
        #[serde(default)]
        edge: RuleEdge,
    },
    #[serde(rename = "structure_price")]
    StructurePrice {
        structure_id: String,
        #[serde(default)]
        structure_family: Option<StructureFamily>,
        #[serde(default)]
        edge: RuleEdge,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "op", deny_unknown_fields)]
pub enum RuleExpression {
    #[serde(rename = "compare")]
    Compare {
        test: RuleComparisonTest,
        left: Box<RuleOperand>,
        right: Box<RuleOperand>,
    },
    #[serde(rename = "overlay")]
    Overlay {
        drawing_id: String,
        mode: AlertOverlayMode,
        direction: AlertDirection,
        #[serde(default, with = "serde_decimal_str_opt")]
        ratio: Option<DecimalStr>,
    },
    #[serde(rename = "level_event")]
    LevelEvent {
        level_id: String,
        event: AlertLevelEvent,
        #[serde(default)]
        level_kind: Option<KeyLevelKind>,
    },
    #[serde(rename = "structure_event")]
    StructureEvent {
        structure_id: String,
        event: AlertLevelEvent,
        #[serde(default)]
        structure_family: Option<StructureFamily>,
    },
    #[serde(rename = "all")]
    All { args: Vec<RuleExpression> },
    #[serde(rename = "any")]
    Any { args: Vec<RuleExpression> },
    #[serde(rename = "not")]
    Not { args: Vec<RuleExpression> },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Rule {
    pub version: RuleVersion,
    pub timing: RuleTiming,
    pub expression: RuleExpression,
}

fn default_preview_limit() -> usize {
    300
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RulePreviewRequest {
    pub timeframe: String,
    pub rule: Rule,
    #[serde(default = "default_preview_limit")]
    pub limit: usize,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RulePreviewSignal {
    pub timestamp: i64,
    pub observed_at: i64,
    #[serde(with = "serde_decimal_str_opt")]
    pub price: Option<DecimalStr>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RulePreviewIndicator {
    pub operand: RuleOperand,
    pub pane: RulePane,
    pub points: Vec<IndicatorPoint>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RulePreviewResponse {
    pub rule: Rule,
    #[serde(default)]
    pub unavailable_current_levels: Vec<String>,
    #[serde(default)]
    pub history_incomplete: bool,
    pub condition_true: Vec<RulePreviewSignal>,
    pub would_trigger: Vec<RulePreviewSignal>,
    pub indicators: Vec<RulePreviewIndicator>,
}
