//! Bybit V5 Linear REST connector and shared strict Bybit V5 payload decoders.

use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rust_decimal::Decimal;
use serde_json::Value;
use talaria_contracts::{
    DerivativeKind, PrecisionTier, RawBar, RawDerivativePoint, parse_decimal_str, timeframe_ms,
};

use crate::error::PackError;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector, default_clock};

/// Default Bybit V5 Linear REST base URL.
pub const DEFAULT_BASE_URL: &str = "https://api.bybit.com";
/// Maximum klines / mark-price klines per Bybit V5 page.
pub const MAX_LIMIT: usize = 1000;
/// Maximum funding settlement rows per Bybit V5 page.
pub const FUNDING_LIMIT: usize = 200;
/// Maximum open-interest observations per Bybit V5 page.
pub const OI_LIMIT: usize = 200;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe label to Bybit V5 REST/WS interval token.
pub fn rest_interval_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" => Ok("1"),
        "5m" => Ok("5"),
        "15m" => Ok("15"),
        "1h" => Ok("60"),
        "4h" => Ok("240"),
        "1d" => Ok("D"),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported Bybit timeframe `{other}`"
        ))),
    }
}

/// Map Talaria timeframe label to Bybit V5 open-interest `intervalTime` parameter.
pub fn oi_interval_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" | "5m" => Ok("5min"),
        "15m" => Ok("15min"),
        "1h" => Ok("1h"),
        "4h" => Ok("4h"),
        "1d" => Ok("1d"),
        other => Err(PackError::FeedUnavailable(format!(
            "Bybit open interest does not support timeframe `{other}`"
        ))),
    }
}

/// Strictly parse a JSON string field into `Decimal` without touching floating point.
pub(crate) fn parse_json_decimal_str(value: &Value, field: &str) -> Result<Decimal, PackError> {
    let raw = value.as_str().ok_or_else(|| {
        PackError::FeedUnavailable(format!("Bybit field `{field}` must be a DecimalStr string"))
    })?;
    parse_decimal_str(raw)
        .map_err(|err| PackError::FeedUnavailable(format!("invalid Bybit `{field}`: {err}")))
}

/// Parse an integer millisecond timestamp represented as either a JSON integer or string.
pub(crate) fn parse_json_i64(value: &Value, field: &str) -> Result<i64, PackError> {
    if let Some(n) = value.as_i64() {
        return Ok(n);
    }
    if let Some(s) = value.as_str() {
        return s
            .parse::<i64>()
            .map_err(|_| PackError::FeedUnavailable(format!("invalid integer in `{field}`: {s}")));
    }
    Err(PackError::FeedUnavailable(format!(
        "Bybit field `{field}` must be an integer or numeric string"
    )))
}

/// Validate a Bybit V5 response envelope (`retCode == 0`) and return `result.list`.
pub(crate) fn parse_bybit_envelope_list(payload: &Value) -> Result<&Vec<Value>, PackError> {
    let obj = payload
        .as_object()
        .ok_or_else(|| PackError::FeedUnavailable("Bybit payload must be a JSON object".to_owned()))?;
    let ret_code = obj.get("retCode").and_then(Value::as_i64);
    if ret_code != Some(0) {
        let msg = obj
            .get("retMsg")
            .and_then(Value::as_str)
            .unwrap_or("unknown error");
        return Err(PackError::FeedUnavailable(format!(
            "Bybit returned error {ret_code:?}: {msg}"
        )));
    }
    obj.get("result")
        .and_then(Value::as_object)
        .and_then(|res| res.get("list"))
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedUnavailable("Bybit payload missing `result.list`".to_owned()))
}

/// Parse a Bybit V5 kline row `[start, open, high, low, close, volume, turnover]`.
pub(crate) fn parse_kline_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawBar, PackError> {
    let arr = row
        .as_array()
        .ok_or_else(|| PackError::FeedUnavailable("Bybit kline row must be an array".to_owned()))?;
    if arr.len() < 7 {
        return Err(PackError::FeedUnavailable(
            "Bybit kline row must contain at least 7 fields".to_owned(),
        ));
    }
    let ts_event = parse_json_i64(&arr[0], "start")?;
    let open = parse_json_decimal_str(&arr[1], "open")?;
    let high = parse_json_decimal_str(&arr[2], "high")?;
    let low = parse_json_decimal_str(&arr[3], "low")?;
    let close = parse_json_decimal_str(&arr[4], "close")?;
    let volume = parse_json_decimal_str(&arr[5], "volume")?;
    let turnover = parse_json_decimal_str(&arr[6], "turnover")?;
    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(RawBar {
        symbol: symbol.to_owned(),
        timeframe: timeframe.to_owned(),
        ts_event,
        open,
        high,
        low,
        close,
        volume,
        turnover,
        ts_available,
        ts_ingested: ingested,
        ts_committed: committed,
        precision: PrecisionTier::SecondVerified,
        revision: 0,
        confirmed: true,
        history_only: false,
        synthetic: false,
    })
}

/// Parse a Bybit V5 mark-price kline row `[start, open, high, low, close]`.
pub(crate) fn parse_mark_price_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("Bybit mark-price row must be an array".to_owned())
    })?;
    if arr.len() < 5 {
        return Err(PackError::FeedUnavailable(
            "Bybit mark-price row must contain at least 5 fields".to_owned(),
        ));
    }
    let ts_event = parse_json_i64(&arr[0], "start")?;
    let value = parse_json_decimal_str(&arr[4], "close")?;
    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(RawDerivativePoint {
        kind: DerivativeKind::MarkPrice,
        symbol: symbol.to_owned(),
        timeframe: Some(timeframe.to_owned()),
        ts_event,
        value,
        ts_available,
        ts_ingested: ingested,
        ts_committed: committed,
        ts_published: None,
        precision: PrecisionTier::SecondVerified,
    })
}

/// Parse a Bybit V5 funding settlement row `{"fundingRateTimestamp", "fundingRate"}`.
pub(crate) fn parse_funding_row(
    row: &Value,
    symbol: &str,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Bybit funding row must be a JSON object".to_owned())
    })?;
    let ts_val = obj
        .get("fundingRateTimestamp")
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingRateTimestamp`".to_owned()))?;
    let rate_val = obj
        .get("fundingRate")
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingRate`".to_owned()))?;
    let ts_event = parse_json_i64(ts_val, "fundingRateTimestamp")?;
    let value = parse_json_decimal_str(rate_val, "fundingRate")?;
    let ingested = ts_ingested.unwrap_or(ts_event);
    let committed = ts_committed.unwrap_or(ts_event);

    Ok(RawDerivativePoint {
        kind: DerivativeKind::FundingRate,
        symbol: symbol.to_owned(),
        timeframe: None,
        ts_event,
        value,
        ts_available: ts_event,
        ts_ingested: ingested,
        ts_committed: committed,
        ts_published: Some(ts_event),
        precision: PrecisionTier::SecondVerified,
    })
}

/// Parse a Bybit V5 open-interest row `{"timestamp", "openInterest"}`.
pub(crate) fn parse_open_interest_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Bybit open-interest row must be a JSON object".to_owned())
    })?;
    let ts_val = obj
        .get("timestamp")
        .ok_or_else(|| PackError::FeedUnavailable("missing `timestamp`".to_owned()))?;
    let oi_val = obj
        .get("openInterest")
        .ok_or_else(|| PackError::FeedUnavailable("missing `openInterest`".to_owned()))?;
    let ts_event = parse_json_i64(ts_val, "timestamp")?;
    let value = parse_json_decimal_str(oi_val, "openInterest")?;
    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(RawDerivativePoint {
        kind: DerivativeKind::OpenInterest,
        symbol: symbol.to_owned(),
        timeframe: Some(timeframe.to_owned()),
        ts_event,
        value,
        ts_available,
        ts_ingested: ingested,
        ts_committed: committed,
        ts_published: None,
        precision: PrecisionTier::SecondVerified,
    })
}

/// Async Bybit V5 Linear REST connector paging leftward by `end` / `endTime`.
#[derive(Clone)]
pub struct BybitLinearConnector {
    base_url: String,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl BybitLinearConnector {
    /// Create a connector targeting `base_url` (or [`DEFAULT_BASE_URL`]).
    pub fn new(
        base_url: impl Into<String>,
        timeout: Duration,
        retries: usize,
        now_ms: ClockFn,
    ) -> Result<Self, PackError> {
        let base_url = base_url.into().trim_end_matches('/').to_owned();
        let mut builder = Client::builder()
            .timeout(timeout)
            .user_agent("talaria/0.1.0");
        if is_loopback_url(&base_url) {
            builder = builder.no_proxy();
        }
        let client = builder
            .build()
            .map_err(|err| PackError::FeedUnavailable(format!("failed to build HTTP client: {err}")))?;
        Ok(Self {
            base_url,
            client,
            retries,
            now_ms,
            sleeper: default_sleeper(),
        })
    }

    /// Create a connector with defaults pointing to [`DEFAULT_BASE_URL`].
    pub fn default_live() -> Result<Self, PackError> {
        Self::new(DEFAULT_BASE_URL, Duration::from_secs(10), 2, default_clock())
    }

    /// Override the retry backoff sleeper (e.g., instant sleeper in unit tests).
    pub fn with_sleeper(mut self, sleeper: AsyncSleepFn) -> Self {
        self.sleeper = sleeper;
        self
    }

    /// Base URL configured on this connector.
    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    async fn request_json(&self, path: &str, params: &[(&str, String)]) -> Result<Value, PackError> {
        let url = format!("{}{}", self.base_url, path);
        let mut last_error: Option<String> = None;

        for attempt in 0..=self.retries {
            let result = async {
                let response = self.client.get(&url).query(params).send().await?;
                let response = response.error_for_status()?;
                let payload: Value = response.json().await?;
                Ok::<Value, reqwest::Error>(payload)
            }
            .await;

            match result {
                Ok(payload) => return Ok(payload),
                Err(err) => {
                    last_error = Some(err.to_string());
                    if attempt < self.retries {
                        let backoff_ms = 200u64.saturating_mul(1u64 << attempt.min(10));
                        (self.sleeper)(Duration::from_millis(backoff_ms)).await;
                    }
                }
            }
        }

        Err(PackError::FeedUnavailable(format!(
            "Bybit market data unavailable: {}",
            last_error.unwrap_or_else(|| "unknown HTTP failure".to_owned())
        )))
    }
}

impl HistoryConnector for BybitLinearConnector {
    fn feed_mode(&self) -> &str {
        "bybit"
    }

    fn fetch_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawBar>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let interval_str = rest_interval_for(timeframe)?;
            let interval = timeframe_ms(timeframe)?;
            let capped = limit.min(MAX_LIMIT);
            let mut params = vec![
                ("category", "linear".to_owned()),
                ("symbol", symbol.to_owned()),
                ("interval", interval_str.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("end", end.to_string()));
            }

            let payload = self.request_json("/v5/market/kline", &params).await?;
            let rows = parse_bybit_envelope_list(&payload)?;
            let now = (self.now_ms)();
            let mut bars = Vec::with_capacity(rows.len());
            for row in rows {
                let bar = parse_kline_row(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if bar.ts_event + interval > now {
                    continue;
                }
                bars.push(bar);
            }
            bars.sort_by_key(|bar| bar.ts_event);
            Ok(bars)
        })
    }

    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let interval_str = rest_interval_for(timeframe)?;
            let interval = timeframe_ms(timeframe)?;
            let capped = limit.min(MAX_LIMIT);
            let mut params = vec![
                ("category", "linear".to_owned()),
                ("symbol", symbol.to_owned()),
                ("interval", interval_str.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("end", end.to_string()));
            }

            let payload = self
                .request_json("/v5/market/mark-price-kline", &params)
                .await?;
            let rows = parse_bybit_envelope_list(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_mark_price_row(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if pt.ts_event + interval > now {
                    continue;
                }
                points.push(pt);
            }
            points.sort_by_key(|pt| pt.ts_event);
            Ok(points)
        })
    }

    fn fetch_funding_settlements<'a>(
        &'a self,
        symbol: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let capped = limit.min(FUNDING_LIMIT);
            let mut params = vec![
                ("category", "linear".to_owned()),
                ("symbol", symbol.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self
                .request_json("/v5/market/funding/history", &params)
                .await?;
            let rows = parse_bybit_envelope_list(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_funding_row(row, symbol, Some(now), Some(now))?;
                if pt.ts_event > now {
                    continue;
                }
                points.push(pt);
            }
            points.sort_by_key(|pt| pt.ts_event);
            Ok(points)
        })
    }

    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let interval_time = oi_interval_for(timeframe)?;
            let interval = timeframe_ms(timeframe)?;
            let capped = limit.min(OI_LIMIT);
            let mut params = vec![
                ("category", "linear".to_owned()),
                ("symbol", symbol.to_owned()),
                ("intervalTime", interval_time.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self
                .request_json("/v5/market/open-interest", &params)
                .await?;
            let rows = parse_bybit_envelope_list(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(rows.len());
            for row in rows {
                let pt =
                    parse_open_interest_row(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if pt.ts_event + interval > now {
                    continue;
                }
                points.push(pt);
            }
            points.sort_by_key(|pt| pt.ts_event);
            Ok(points)
        })
    }
}
