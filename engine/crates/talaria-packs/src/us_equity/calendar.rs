//! Anti-lookahead corporate actions and earnings calendar handling (`ts_available <= as_of`).

use std::fs;
use std::path::Path;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::{CatalystEvent, PrecisionTier, parse_decimal_str};

use crate::error::PackError;

/// Equity corporate or earnings event classification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EquityEventKind {
    Earnings,
    Dividend,
    Split,
    Spinoff,
    Merger,
}

impl EquityEventKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Earnings => "earnings",
            Self::Dividend => "dividend",
            Self::Split => "split",
            Self::Spinoff => "spinoff",
            Self::Merger => "merger",
        }
    }
}

/// Point-in-time release status matching `core/src/talaria/foundation/calendar.py`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum EquityEventStatus {
    Released,
    Unreleased,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawEquityCalendarEvent {
    event_id: String,
    instrument_id: String,
    kind: EquityEventKind,
    title: String,
    ts_event: i64,
    schedule_available_ts: i64,
    ts_available: i64,
    precision: PrecisionTier,
    #[serde(default)]
    cash_amount: Option<String>,
    #[serde(default)]
    split_ratio: Option<String>,
    #[serde(default)]
    eps_estimate: Option<String>,
    #[serde(default)]
    eps_actual: Option<String>,
}

/// Corporate action or earnings calendar record with causal availability provenance.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EquityCalendarEvent {
    pub event_id: String,
    pub instrument_id: String,
    pub kind: EquityEventKind,
    pub title: String,
    pub ts_event: i64,
    pub schedule_available_ts: i64,
    pub ts_available: i64,
    pub precision: PrecisionTier,
    pub cash_amount: Option<Decimal>,
    pub split_ratio: Option<Decimal>,
    pub eps_estimate: Option<Decimal>,
    pub eps_actual: Option<Decimal>,
}

impl EquityCalendarEvent {
    /// Enforce causal non-negative timestamps and `ts_available >= schedule_available_ts`.
    pub fn validate(&self) -> Result<(), PackError> {
        if self.event_id.is_empty() || self.instrument_id.is_empty() {
            return Err(PackError::InvalidRequest(
                "calendar event requires non-empty event_id and instrument_id".to_owned(),
            ));
        }
        if self.ts_event < 0 || self.schedule_available_ts < 0 || self.ts_available < 0 {
            return Err(PackError::InvalidRequest(format!(
                "calendar event `{}` timestamps must be non-negative",
                self.event_id
            )));
        }
        if self.ts_available < self.schedule_available_ts {
            return Err(PackError::InvalidRequest(format!(
                "calendar event `{}` ts_available ({}) precedes schedule_available_ts ({})",
                self.event_id, self.ts_available, self.schedule_available_ts
            )));
        }
        Ok(())
    }

    /// Return `true` only when the full event outcome is market-visible at `as_of`.
    pub fn is_available_at(&self, as_of: i64) -> bool {
        self.ts_available <= as_of
    }
}

/// Point-in-time observation of an [`EquityCalendarEvent`] evaluated at `as_of`.
///
/// When `ts_available > as_of`, `status` is `Unreleased`, `effective_available_ts` is `None`,
/// and outcome fields (`eps_actual`) are redacted to prevent lookahead leakage.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EquityCalendarObservation {
    pub event_id: String,
    pub instrument_id: String,
    pub kind: EquityEventKind,
    pub title: String,
    pub ts_event: i64,
    pub status: EquityEventStatus,
    pub effective_available_ts: Option<i64>,
    pub cash_amount: Option<Decimal>,
    pub split_ratio: Option<Decimal>,
    pub eps_estimate: Option<Decimal>,
    pub eps_actual: Option<Decimal>,
}

impl EquityCalendarObservation {
    /// Project a released observation into `talaria_contracts::CatalystEvent`.
    pub fn to_catalyst_event(&self) -> Option<CatalystEvent> {
        let ts = self.effective_available_ts?;
        Some(CatalystEvent {
            event_id: self.event_id.clone(),
            title: self.title.clone(),
            ts,
            kind: self.kind.as_str().to_ascii_uppercase(),
            source: "alpaca_calendar".to_owned(),
        })
    }
}

fn parse_opt_decimal_str(raw: Option<String>, field: &str) -> Result<Option<Decimal>, PackError> {
    raw.map(|s| {
        parse_decimal_str(&s).map_err(|err| {
            PackError::FeedUnavailable(format!("invalid calendar decimal `{field}`: {err}"))
        })
    })
    .transpose()
}

/// Anti-lookahead calendar storing equity corporate actions and earnings releases.
#[derive(Debug, Clone, Default)]
pub struct EquityCalendar {
    events: Vec<EquityCalendarEvent>,
}

impl EquityCalendar {
    /// Construct and validate a calendar from a sequence of events.
    pub fn from_events(events: Vec<EquityCalendarEvent>) -> Result<Self, PackError> {
        for event in &events {
            event.validate()?;
        }
        Ok(Self { events })
    }

    /// Load and validate calendar events from a JSON fixture file.
    pub fn from_fixture_file(path: &Path) -> Result<Self, PackError> {
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing Alpaca calendar fixture `{}`",
                path.display()
            )));
        }
        let text = fs::read_to_string(path).map_err(|err| {
            PackError::FeedUnavailable(format!(
                "cannot read Alpaca calendar fixture `{}`: {err}",
                path.display()
            ))
        })?;
        let raw_events: Vec<RawEquityCalendarEvent> = serde_json::from_str(&text).map_err(|err| {
            PackError::FeedUnavailable(format!(
                "invalid Alpaca calendar JSON `{}`: {err}",
                path.display()
            ))
        })?;
        let mut events = Vec::with_capacity(raw_events.len());
        for raw in raw_events {
            events.push(EquityCalendarEvent {
                event_id: raw.event_id,
                instrument_id: raw.instrument_id,
                kind: raw.kind,
                title: raw.title,
                ts_event: raw.ts_event,
                schedule_available_ts: raw.schedule_available_ts,
                ts_available: raw.ts_available,
                precision: raw.precision,
                cash_amount: parse_opt_decimal_str(raw.cash_amount, "cash_amount")?,
                split_ratio: parse_opt_decimal_str(raw.split_ratio, "split_ratio")?,
                eps_estimate: parse_opt_decimal_str(raw.eps_estimate, "eps_estimate")?,
                eps_actual: parse_opt_decimal_str(raw.eps_actual, "eps_actual")?,
            });
        }
        Self::from_events(events)
    }

    /// Return only events strictly visible at `as_of` (`ts_available <= as_of`), sorted by `(ts_event, event_id)`.
    pub fn visible_events(
        &self,
        instrument_id: Option<&str>,
        as_of: i64,
    ) -> Vec<&EquityCalendarEvent> {
        let mut out: Vec<&EquityCalendarEvent> = self
            .events
            .iter()
            .filter(|ev| {
                ev.is_available_at(as_of)
                    && instrument_id.is_none_or(|id| ev.instrument_id == id)
            })
            .collect();
        out.sort_by(|a, b| (a.ts_event, &a.event_id).cmp(&(b.ts_event, &b.event_id)));
        out
    }

    /// Return point-in-time observations for scheduled events (`schedule_available_ts <= as_of`),
    /// redacting outcome fields whenever `ts_available > as_of`.
    pub fn observations_at(
        &self,
        instrument_id: Option<&str>,
        as_of: i64,
    ) -> Vec<EquityCalendarObservation> {
        let mut matching: Vec<&EquityCalendarEvent> = self
            .events
            .iter()
            .filter(|ev| {
                ev.schedule_available_ts <= as_of
                    && instrument_id.is_none_or(|id| ev.instrument_id == id)
            })
            .collect();
        matching.sort_by(|a, b| (a.ts_event, &a.event_id).cmp(&(b.ts_event, &b.event_id)));

        matching
            .into_iter()
            .map(|ev| {
                let released = ev.is_available_at(as_of);
                EquityCalendarObservation {
                    event_id: ev.event_id.clone(),
                    instrument_id: ev.instrument_id.clone(),
                    kind: ev.kind,
                    title: ev.title.clone(),
                    ts_event: ev.ts_event,
                    status: if released {
                        EquityEventStatus::Released
                    } else {
                        EquityEventStatus::Unreleased
                    },
                    effective_available_ts: if released { Some(ev.ts_available) } else { None },
                    cash_amount: ev.cash_amount,
                    split_ratio: ev.split_ratio,
                    eps_estimate: ev.eps_estimate,
                    eps_actual: if released { ev.eps_actual } else { None },
                }
            })
            .collect()
    }
}
