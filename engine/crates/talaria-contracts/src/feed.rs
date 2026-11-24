//! Real-time feed event and health state contracts.

use std::cmp::max;

use serde::{Deserialize, Deserializer, Serialize};
use uuid::Uuid;

use crate::bars::CompactBar;
use crate::decimal::{DecimalStr, serde_decimal_str, serde_decimal_str_opt};
use crate::derivatives::{DerivativeKind, DerivativePoint};
use crate::error::ContractError;
use crate::ticks::TradeSide;
use crate::types::PrecisionTier;

/// Feed channel health status (`LIVE` / `STALE` / `RECOVERING`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum FeedHealthStatus {
    Live,
    Stale,
    Recovering,
}

impl FeedHealthStatus {
    const fn severity_rank(self) -> u8 {
        match self {
            Self::Live => 0,
            Self::Recovering => 1,
            Self::Stale => 2,
        }
    }
}

/// Return the worst health status across a sequence (empty defaults to `STALE`).
pub fn worst_status(statuses: impl IntoIterator<Item = FeedHealthStatus>) -> FeedHealthStatus {
    statuses
        .into_iter()
        .max_by_key(|s| s.severity_rank())
        .unwrap_or(FeedHealthStatus::Stale)
}

/// Real-time or replay kline publication with monotonic `revision` per timestamp.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KlineEvent {
    pub instrument_id: String,
    pub timeframe: String,
    pub bar: CompactBar,
    pub confirmed: bool,
    pub revision: u64,
}

/// Transport-layer kline event carrying local ingest time and recovery metadata.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedKlineEvent {
    pub event: KlineEvent,
    pub ts_ingested: i64,
    pub history_only: bool,
    pub synthetic: bool,
}

/// Top-of-book L1 quote event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct OrderBookEvent {
    pub instrument_id: String,
    #[serde(with = "serde_decimal_str")]
    pub bid_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub bid_size: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub ask_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub ask_size: DecimalStr,
    pub ts_event: i64,
    pub ts_ingested: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawOrderBookEvent {
    instrument_id: String,
    #[serde(with = "serde_decimal_str")]
    bid_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    bid_size: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    ask_price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    ask_size: DecimalStr,
    ts_event: i64,
    ts_ingested: i64,
}

impl OrderBookEvent {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.bid_price > self.ask_price {
            return Err(ContractError::Validation(
                "bid_price cannot exceed ask_price",
            ));
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for OrderBookEvent {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawOrderBookEvent::deserialize(deserializer)?;
        let event = Self {
            instrument_id: raw.instrument_id,
            bid_price: raw.bid_price,
            bid_size: raw.bid_size,
            ask_price: raw.ask_price,
            ask_size: raw.ask_size,
            ts_event: raw.ts_event,
            ts_ingested: raw.ts_ingested,
        };
        event.validate().map_err(serde::de::Error::custom)?;
        Ok(event)
    }
}

/// Transport-layer L1 quote carrying connection/update ordering metadata.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedOrderBookEvent {
    pub event: OrderBookEvent,
    pub ts_published: Option<i64>,
    pub connection_id: Uuid,
    pub update_id: u64,
}

/// Real-time public trade event.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TradeEvent {
    pub instrument_id: String,
    pub trade_id: Uuid,
    pub side: TradeSide,
    #[serde(with = "serde_decimal_str")]
    pub price: DecimalStr,
    #[serde(with = "serde_decimal_str")]
    pub size: DecimalStr,
    pub ts_event: i64,
    pub ts_ingested: i64,
}

/// Real-time derivative reading (`mark_price`, `funding_rate`, `next_funding_ts`, `open_interest`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct DerivativeTickEvent {
    pub instrument_id: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub mark_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub funding_rate: Option<DecimalStr>,
    #[serde(default)]
    pub next_funding_ts: Option<i64>,
    #[serde(default, with = "serde_decimal_str_opt")]
    pub open_interest: Option<DecimalStr>,
    pub ts_event: i64,
    pub ts_ingested: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawDerivativeTickEvent {
    instrument_id: String,
    #[serde(default, with = "serde_decimal_str_opt")]
    mark_price: Option<DecimalStr>,
    #[serde(default, with = "serde_decimal_str_opt")]
    funding_rate: Option<DecimalStr>,
    #[serde(default)]
    next_funding_ts: Option<i64>,
    #[serde(default, with = "serde_decimal_str_opt")]
    open_interest: Option<DecimalStr>,
    ts_event: i64,
    ts_ingested: i64,
}

impl DerivativeTickEvent {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.mark_price.is_none()
            && self.funding_rate.is_none()
            && self.next_funding_ts.is_none()
            && self.open_interest.is_none()
        {
            return Err(ContractError::Validation(
                "DerivativeTickEvent requires at least one reading",
            ));
        }
        Ok(())
    }

    pub fn points(&self) -> Vec<DerivativePoint> {
        let mut out = Vec::new();
        let ts_available = max(self.ts_event, self.ts_ingested);
        for (kind, value) in [
            (DerivativeKind::MarkPrice, self.mark_price),
            (DerivativeKind::FundingRate, self.funding_rate),
            (DerivativeKind::OpenInterest, self.open_interest),
        ] {
            if let Some(v) = value {
                out.push(DerivativePoint {
                    instrument_id: self.instrument_id.clone(),
                    kind,
                    timeframe: None,
                    value: v,
                    ts_event: self.ts_event,
                    ts_published: None,
                    ts_available,
                    precision: PrecisionTier::SecondVerified,
                    ts_ingested: self.ts_ingested,
                    ts_committed: self.ts_ingested,
                    revision: 0,
                    settled: if kind == DerivativeKind::FundingRate {
                        Some(false)
                    } else {
                        None
                    },
                });
            }
        }
        out
    }
}

impl<'de> Deserialize<'de> for DerivativeTickEvent {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawDerivativeTickEvent::deserialize(deserializer)?;
        let event = Self {
            instrument_id: raw.instrument_id,
            mark_price: raw.mark_price,
            funding_rate: raw.funding_rate,
            next_funding_ts: raw.next_funding_ts,
            open_interest: raw.open_interest,
            ts_event: raw.ts_event,
            ts_ingested: raw.ts_ingested,
        };
        event.validate().map_err(serde::de::Error::custom)?;
        Ok(event)
    }
}

/// Per-channel feed health update.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct FeedHealthEvent {
    pub instrument_id: String,
    pub timeframe: String,
    pub status: FeedHealthStatus,
    pub ts: i64,
    #[serde(default)]
    pub last_event_ts: Option<i64>,
    #[serde(default)]
    pub stale_since: Option<i64>,
    #[serde(default)]
    pub reconnect_count: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RawFeedHealthEvent {
    instrument_id: String,
    timeframe: String,
    status: FeedHealthStatus,
    ts: i64,
    #[serde(default)]
    last_event_ts: Option<i64>,
    #[serde(default)]
    stale_since: Option<i64>,
    #[serde(default)]
    reconnect_count: u64,
}

impl FeedHealthEvent {
    pub fn validate(&self) -> Result<(), ContractError> {
        if self.status == FeedHealthStatus::Stale && self.stale_since.is_none() {
            return Err(ContractError::Validation(
                "STALE FeedHealthEvent must record stale_since",
            ));
        }
        if self.status == FeedHealthStatus::Live && self.stale_since.is_some() {
            return Err(ContractError::Validation(
                "LIVE FeedHealthEvent must not carry stale_since",
            ));
        }
        Ok(())
    }
}

impl<'de> Deserialize<'de> for FeedHealthEvent {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        let raw = RawFeedHealthEvent::deserialize(deserializer)?;
        let event = Self {
            instrument_id: raw.instrument_id,
            timeframe: raw.timeframe,
            status: raw.status,
            ts: raw.ts,
            last_event_ts: raw.last_event_ts,
            stale_since: raw.stale_since,
            reconnect_count: raw.reconnect_count,
        };
        event.validate().map_err(serde::de::Error::custom)?;
        Ok(event)
    }
}

/// Aggregate fast-path feed health response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FeedHealthReport {
    pub feed_mode: String,
    pub status: FeedHealthStatus,
    pub channels: Vec<FeedHealthEvent>,
}

/// Events emitted by a `FeedTransport`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportEvent {
    Kline(KlineEvent),
    OrderBook(OrderBookEvent),
    DerivativeTick(DerivativeTickEvent),
    Trade(TradeEvent),
}

/// Unified feed event emitted by `FeedSupervisor` to subscribers.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FeedEvent {
    Kline(KlineEvent),
    OrderBook(OrderBookEvent),
    DerivativeTick(DerivativeTickEvent),
    Trade(TradeEvent),
    Health(FeedHealthEvent),
}
