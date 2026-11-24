//! Human chart overlay (`HUMAN_DRAWING`) and chart view contracts.

use serde::{Deserialize, Serialize};

use crate::decimal::{DecimalStr, serde_decimal_str};
use crate::market::InstrumentSpec;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DrawingKind {
    HorizontalLine,
    Ray,
    Segment,
    ParallelChannel,
    FibRetracement,
    Rectangle,
    TextLabel,
}

impl DrawingKind {
    pub const fn expected_points(self) -> usize {
        match self {
            Self::HorizontalLine | Self::TextLabel => 1,
            Self::Ray | Self::Segment | Self::FibRetracement | Self::Rectangle => 2,
            Self::ParallelChannel => 3,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DrawingLineStyle {
    Solid,
    Dashed,
    Dotted,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingPoint {
    pub ts: i64,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
}

fn default_color() -> String {
    "#4c8dff".to_owned()
}

fn default_line_width() -> u8 {
    1
}

fn default_line_style() -> DrawingLineStyle {
    DrawingLineStyle::Solid
}

fn default_fill_opacity() -> f64 {
    0.12
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingStyle {
    #[serde(default = "default_color")]
    pub color: String,
    #[serde(default = "default_line_width")]
    pub line_width: u8,
    #[serde(default = "default_line_style")]
    pub line_style: DrawingLineStyle,
    #[serde(default = "default_fill_opacity")]
    pub fill_opacity: f64,
}

impl Default for DrawingStyle {
    fn default() -> Self {
        Self {
            color: default_color(),
            line_width: default_line_width(),
            line_style: default_line_style(),
            fill_opacity: default_fill_opacity(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingCreate {
    pub instrument_id: String,
    pub kind: DrawingKind,
    #[serde(default)]
    pub visible_timeframes: Vec<String>,
    pub points: Vec<DrawingPoint>,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub style: DrawingStyle,
}

#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingPatch {
    #[serde(default)]
    pub visible_timeframes: Option<Vec<String>>,
    #[serde(default)]
    pub points: Option<Vec<DrawingPoint>>,
    #[serde(default)]
    pub text: Option<String>,
    #[serde(default)]
    pub style: Option<DrawingStyle>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingRecord {
    pub drawing_id: String,
    pub instrument_id: String,
    pub kind: DrawingKind,
    pub visible_timeframes: Vec<String>,
    pub points: Vec<DrawingPoint>,
    pub text: String,
    pub style: DrawingStyle,
    pub version: u64,
    pub created_ts: i64,
    pub updated_ts: i64,
    #[serde(default)]
    pub deleted_at: Option<i64>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingListResponse {
    pub instrument_id: String,
    pub timeframe: String,
    pub drawings: Vec<DrawingRecord>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingVersionList {
    pub drawing_id: String,
    pub versions: Vec<DrawingRecord>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrawingClearResponse {
    pub instrument_id: String,
    pub cleared: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChartViewWindow {
    pub from_ts: i64,
    pub to_ts: i64,
    #[serde(with = "serde_decimal_str")]
    pub price_min: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub price_max: DecimalStr,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChartView {
    pub instrument: InstrumentSpec,
    pub timeframe: String,
    pub window: ChartViewWindow,
    pub drawings: Vec<DrawingRecord>,
}
