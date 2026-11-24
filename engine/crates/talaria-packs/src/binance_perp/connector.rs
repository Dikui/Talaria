//! Binance USD-M Futures REST connector, offline fixture connector, and strict boundary parsers.

use std::fs;
use std::path::{Path, PathBuf};
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

/// Default Binance USD-M Futures REST base URL.
pub const DEFAULT_BASE_URL: &str = "https://fapi.binance.com";
/// Maximum klines / mark-price klines per Binance `/fapi/v1/*` page.
pub const MAX_LIMIT: usize = 1500;
/// Maximum funding settlement rows per Binance `/fapi/v1/fundingRate` page.
pub const FUNDING_LIMIT: usize = 1000;
/// Maximum open-interest observations per Binance `/futures/data/openInterestHist` page.
pub const OI_LIMIT: usize = 500;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe label to Binance USD-M Futures interval token.
pub fn rest_interval_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" => Ok("1m"),
        "5m" => Ok("5m"),
        "15m" => Ok("15m"),
        "1h" => Ok("1h"),
        "4h" => Ok("4h"),
        "1d" => Ok("1d"),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported Binance Futures timeframe `{other}`"
        ))),
    }
}

/// Map Talaria timeframe label to Binance `/futures/data/openInterestHist` `period` token.
pub fn oi_period_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" | "5m" => Ok("5m"),
        "15m" => Ok("15m"),
        "1h" => Ok("1h"),
        "4h" => Ok("4h"),
        "1d" => Ok("1d"),
        other => Err(PackError::FeedUnavailable(format!(
            "Binance open interest does not support timeframe `{other}`"
        ))),
    }
}

/// Strictly parse a Binance JSON string field into `Decimal`, rejecting JSON floats/numbers.
pub(crate) fn parse_binance_decimal_str(value: &Value, field: &str) -> Result<Decimal, PackError> {
    let raw = value.as_str().ok_or_else(|| {
        PackError::FeedUnavailable(format!(
            "Binance field `{field}` must be a DecimalStr string"
        ))
    })?;
    parse_decimal_str(raw)
        .map_err(|err| PackError::FeedUnavailable(format!("invalid Binance `{field}`: {err}")))
}

/// Parse an integer millisecond timestamp represented as a JSON integer or numeric string.
pub(crate) fn parse_binance_i64(value: &Value, field: &str) -> Result<i64, PackError> {
    if let Some(n) = value.as_i64() {
        return Ok(n);
    }
    if let Some(s) = value.as_str() {
        return s.parse::<i64>().map_err(|_| {
            PackError::FeedUnavailable(format!("invalid Binance integer in `{field}`: {s}"))
        });
    }
    Err(PackError::FeedUnavailable(format!(
        "Binance field `{field}` must be an integer or numeric string"
    )))
}

/// Validate a Binance array payload, surfacing structured `{"code": ..., "msg": ...}` errors.
pub(crate) fn parse_binance_array(payload: &Value) -> Result<&Vec<Value>, PackError> {
    if let Some(arr) = payload.as_array() {
        return Ok(arr);
    }
    if let Some(obj) = payload.as_object()
        && obj.contains_key("code")
    {
        let code = obj.get("code").and_then(Value::as_i64);
        let msg = obj
            .get("msg")
            .and_then(Value::as_str)
            .unwrap_or("unknown Binance error");
        return Err(PackError::FeedUnavailable(format!(
            "Binance returned error {code:?}: {msg}"
        )));
    }
    Err(PackError::FeedUnavailable(
        "Binance payload must be a JSON array".to_owned(),
    ))
}

/// Parse a Binance `/fapi/v1/klines` row:
/// `[openTime, open, high, low, close, volume, closeTime, quoteAssetVolume, ...]`.
pub(crate) fn parse_kline_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawBar, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("Binance kline row must be an array".to_owned())
    })?;
    if arr.len() < 8 {
        return Err(PackError::FeedUnavailable(
            "Binance kline row must contain at least 8 fields".to_owned(),
        ));
    }
    let ts_event = parse_binance_i64(&arr[0], "openTime")?;
    let open = parse_binance_decimal_str(&arr[1], "open")?;
    let high = parse_binance_decimal_str(&arr[2], "high")?;
    let low = parse_binance_decimal_str(&arr[3], "low")?;
    let close = parse_binance_decimal_str(&arr[4], "close")?;
    let volume = parse_binance_decimal_str(&arr[5], "volume")?;
    let turnover = parse_binance_decimal_str(&arr[7], "quoteAssetVolume")?;
    let ts_available = ts_event + interval;

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
        ts_ingested: ts_ingested.unwrap_or(ts_available),
        ts_committed: ts_committed.unwrap_or(ts_available),
        precision: PrecisionTier::SecondVerified,
        revision: 0,
        confirmed: true,
        history_only: false,
        synthetic: false,
    })
}

/// Parse a Binance `/fapi/v1/markPriceKlines` row `[openTime, open, high, low, close, ...]`.
pub(crate) fn parse_mark_price_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("Binance mark-price row must be an array".to_owned())
    })?;
    if arr.len() < 5 {
        return Err(PackError::FeedUnavailable(
            "Binance mark-price row must contain at least 5 fields".to_owned(),
        ));
    }
    let ts_event = parse_binance_i64(&arr[0], "openTime")?;
    let value = parse_binance_decimal_str(&arr[4], "close")?;
    let ts_available = ts_event + interval;

    Ok(RawDerivativePoint {
        kind: DerivativeKind::MarkPrice,
        symbol: symbol.to_owned(),
        timeframe: Some(timeframe.to_owned()),
        ts_event,
        value,
        ts_available,
        ts_ingested: ts_ingested.unwrap_or(ts_available),
        ts_committed: ts_committed.unwrap_or(ts_available),
        ts_published: None,
        precision: PrecisionTier::SecondVerified,
    })
}

/// Parse a Binance `/fapi/v1/fundingRate` row `{"fundingTime", "fundingRate", ...}`.
pub(crate) fn parse_funding_row(
    row: &Value,
    symbol: &str,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Binance funding row must be a JSON object".to_owned())
    })?;
    let ts_val = obj
        .get("fundingTime")
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingTime`".to_owned()))?;
    let rate_val = obj
        .get("fundingRate")
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingRate`".to_owned()))?;
    let ts_event = parse_binance_i64(ts_val, "fundingTime")?;
    let value = parse_binance_decimal_str(rate_val, "fundingRate")?;

    Ok(RawDerivativePoint {
        kind: DerivativeKind::FundingRate,
        symbol: symbol.to_owned(),
        timeframe: None,
        ts_event,
        value,
        ts_available: ts_event,
        ts_ingested: ts_ingested.unwrap_or(ts_event),
        ts_committed: ts_committed.unwrap_or(ts_event),
        ts_published: Some(ts_event),
        precision: PrecisionTier::SecondVerified,
    })
}

/// Parse a Binance `/futures/data/openInterestHist` row `{"timestamp", "sumOpenInterest", ...}`.
pub(crate) fn parse_open_interest_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Binance open-interest row must be a JSON object".to_owned())
    })?;
    let ts_val = obj
        .get("timestamp")
        .ok_or_else(|| PackError::FeedUnavailable("missing `timestamp`".to_owned()))?;
    let oi_val = obj
        .get("sumOpenInterest")
        .ok_or_else(|| PackError::FeedUnavailable("missing `sumOpenInterest`".to_owned()))?;
    let ts_event = parse_binance_i64(ts_val, "timestamp")?;
    let value = parse_binance_decimal_str(oi_val, "sumOpenInterest")?;
    let ts_available = ts_event + interval;

    Ok(RawDerivativePoint {
        kind: DerivativeKind::OpenInterest,
        symbol: symbol.to_owned(),
        timeframe: Some(timeframe.to_owned()),
        ts_event,
        value,
        ts_available,
        ts_ingested: ts_ingested.unwrap_or(ts_available),
        ts_committed: ts_committed.unwrap_or(ts_available),
        ts_published: None,
        precision: PrecisionTier::SecondVerified,
    })
}

fn trim_and_sort_ascending<T, F>(
    items: Vec<T>,
    end_ts: Option<i64>,
    limit: usize,
    max_limit: usize,
    ts_of: F,
) -> Vec<T>
where
    F: Fn(&T) -> i64,
{
    let capped = limit.min(max_limit);
    let mut selected: Vec<(i64, T)> = items
        .into_iter()
        .filter_map(|item| {
            let ts = ts_of(&item);
            if end_ts.is_some_and(|end| ts > end) {
                None
            } else {
                Some((ts, item))
            }
        })
        .collect();
    selected.sort_by_key(|(ts, _)| *ts);
    if selected.len() > capped {
        let drop_count = selected.len() - capped;
        selected.drain(..drop_count);
    }
    selected.into_iter().map(|(_, item)| item).collect()
}

/// Async Binance USD-M Futures REST [`HistoryConnector`].
#[derive(Clone)]
pub struct BinanceFuturesConnector {
    base_url: String,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl BinanceFuturesConnector {
    /// Construct a connector targeting `base_url` (or [`DEFAULT_BASE_URL`]).
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
        let client = builder.build().map_err(|err| {
            PackError::FeedUnavailable(format!("failed to build HTTP client: {err}"))
        })?;
        Ok(Self {
            base_url,
            client,
            retries,
            now_ms,
            sleeper: default_sleeper(),
        })
    }

    /// Construct with default configuration targeting [`DEFAULT_BASE_URL`].
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
            "Binance Futures market data unavailable: {}",
            last_error.unwrap_or_else(|| "unknown HTTP failure".to_owned())
        )))
    }
}

impl HistoryConnector for BinanceFuturesConnector {
    fn feed_mode(&self) -> &str {
        "binance"
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
                ("symbol", symbol.to_owned()),
                ("interval", interval_str.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self.request_json("/fapi/v1/klines", &params).await?;
            let rows = parse_binance_array(&payload)?;
            let now = (self.now_ms)();
            let mut bars = Vec::with_capacity(rows.len());
            for row in rows {
                let bar = parse_kline_row(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if bar.ts_event + interval > now {
                    continue;
                }
                bars.push(bar);
            }
            Ok(trim_and_sort_ascending(bars, end_ts, limit, MAX_LIMIT, |b| b.ts_event))
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
                ("symbol", symbol.to_owned()),
                ("interval", interval_str.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self.request_json("/fapi/v1/markPriceKlines", &params).await?;
            let rows = parse_binance_array(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_mark_price_row(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if pt.ts_event + interval > now {
                    continue;
                }
                points.push(pt);
            }
            Ok(trim_and_sort_ascending(
                points,
                end_ts,
                limit,
                MAX_LIMIT,
                |p| p.ts_event,
            ))
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
                ("symbol", symbol.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self.request_json("/fapi/v1/fundingRate", &params).await?;
            let rows = parse_binance_array(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_funding_row(row, symbol, Some(now), Some(now))?;
                if pt.ts_event > now {
                    continue;
                }
                points.push(pt);
            }
            Ok(trim_and_sort_ascending(
                points,
                end_ts,
                limit,
                FUNDING_LIMIT,
                |p| p.ts_event,
            ))
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
            let period = oi_period_for(timeframe)?;
            let interval = timeframe_ms(timeframe)?;
            let capped = limit.min(OI_LIMIT);
            let mut params = vec![
                ("symbol", symbol.to_owned()),
                ("period", period.to_owned()),
                ("limit", capped.to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", end.to_string()));
            }

            let payload = self
                .request_json("/futures/data/openInterestHist", &params)
                .await?;
            let rows = parse_binance_array(&payload)?;
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
            Ok(trim_and_sort_ascending(
                points,
                end_ts,
                limit,
                OI_LIMIT,
                |p| p.ts_event,
            ))
        })
    }
}

/// Offline deterministic [`HistoryConnector`] reading `tests/fixtures/binance/` `fapi` JSON arrays.
#[derive(Debug, Clone)]
pub struct BinanceFixtureConnector {
    fixture_dir: PathBuf,
}

impl BinanceFixtureConnector {
    /// Construct an offline connector reading Binance fixture arrays from `fixture_dir`.
    pub fn new(fixture_dir: impl Into<PathBuf>) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
        }
    }

    pub fn fixture_dir(&self) -> &Path {
        &self.fixture_dir
    }

    fn read_array(&self, path: &Path) -> Result<Vec<Value>, PackError> {
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing Binance offline fixture file `{}`",
                path.display()
            )));
        }
        let text = fs::read_to_string(path).map_err(|err| {
            PackError::FeedUnavailable(format!("cannot read fixture `{}`: {err}", path.display()))
        })?;
        let payload: Value = serde_json::from_str(&text).map_err(|err| {
            PackError::FeedUnavailable(format!(
                "invalid JSON in Binance fixture `{}`: {err}",
                path.display()
            ))
        })?;
        Ok(parse_binance_array(&payload)?.clone())
    }

    fn select_latest_ascending<T, F>(
        &self,
        path: &Path,
        end_ts: Option<i64>,
        limit: usize,
        max_limit: usize,
        mut parse_row: F,
    ) -> Result<Vec<T>, PackError>
    where
        F: FnMut(&Value) -> Result<(i64, T), PackError>,
    {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let rows = self.read_array(path)?;
        let mut items_with_ts = Vec::with_capacity(rows.len());
        for row in &rows {
            let (ts_event, item) = parse_row(row)?;
            if end_ts.is_some_and(|end| ts_event > end) {
                continue;
            }
            items_with_ts.push((ts_event, item));
        }
        items_with_ts.sort_by_key(|(ts, _)| *ts);
        let capped = limit.min(max_limit);
        if items_with_ts.len() > capped {
            let drop_count = items_with_ts.len() - capped;
            items_with_ts.drain(..drop_count);
        }
        Ok(items_with_ts.into_iter().map(|(_, item)| item).collect())
    }

    pub fn fetch_bars_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawBar>, PackError> {
        let _ = rest_interval_for(timeframe)?;
        let interval = timeframe_ms(timeframe)?;
        let primary = self.fixture_dir.join(symbol).join(format!("{timeframe}.json"));
        let path = if primary.is_file() {
            primary
        } else {
            self.fixture_dir
                .join(symbol)
                .join(format!("klines_{timeframe}.json"))
        };
        self.select_latest_ascending(&path, end_ts, limit, MAX_LIMIT, |row| {
            let bar = parse_kline_row(row, symbol, timeframe, interval, None, None)?;
            Ok((bar.ts_event, bar))
        })
    }

    pub fn fetch_mark_price_bars_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawDerivativePoint>, PackError> {
        let _ = rest_interval_for(timeframe)?;
        let interval = timeframe_ms(timeframe)?;
        let primary = self
            .fixture_dir
            .join(symbol)
            .join("derivatives")
            .join(format!("mark_price_{timeframe}.json"));
        let path = if primary.is_file() {
            primary
        } else {
            self.fixture_dir.join(symbol).join(format!("{timeframe}.json"))
        };
        self.select_latest_ascending(&path, end_ts, limit, MAX_LIMIT, |row| {
            let pt = parse_mark_price_row(row, symbol, timeframe, interval, None, None)?;
            Ok((pt.ts_event, pt))
        })
    }

    pub fn fetch_funding_settlements_sync(
        &self,
        symbol: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawDerivativePoint>, PackError> {
        let path = self
            .fixture_dir
            .join(symbol)
            .join("derivatives")
            .join("funding.json");
        self.select_latest_ascending(&path, end_ts, limit, FUNDING_LIMIT, |row| {
            let pt = parse_funding_row(row, symbol, None, None)?;
            Ok((pt.ts_event, pt))
        })
    }

    pub fn fetch_open_interest_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawDerivativePoint>, PackError> {
        let period = oi_period_for(timeframe)?;
        let interval = timeframe_ms(timeframe)?;
        let primary = self
            .fixture_dir
            .join(symbol)
            .join("derivatives")
            .join(format!("open_interest_{timeframe}.json"));
        let path = if primary.is_file() {
            primary
        } else {
            self.fixture_dir
                .join(symbol)
                .join("derivatives")
                .join(format!("open_interest_{period}.json"))
        };
        self.select_latest_ascending(&path, end_ts, limit, OI_LIMIT, |row| {
            let pt = parse_open_interest_row(row, symbol, timeframe, interval, None, None)?;
            Ok((pt.ts_event, pt))
        })
    }
}

impl HistoryConnector for BinanceFixtureConnector {
    fn feed_mode(&self) -> &str {
        "fixture"
    }

    fn fetch_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawBar>, PackError>> {
        Box::pin(async move { self.fetch_bars_sync(symbol, timeframe, end_ts, limit) })
    }

    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move { self.fetch_mark_price_bars_sync(symbol, timeframe, end_ts, limit) })
    }

    fn fetch_funding_settlements<'a>(
        &'a self,
        symbol: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move { self.fetch_funding_settlements_sync(symbol, end_ts, limit) })
    }

    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move { self.fetch_open_interest_sync(symbol, timeframe, end_ts, limit) })
    }
}
