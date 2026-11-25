//! Coinbase Exchange REST & offline fixture connectors with strict `Decimal` parsing.

use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, SecondsFormat, Utc};
use reqwest::Client;
use rust_decimal::Decimal;
use serde_json::Value;
use talaria_contracts::{PrecisionTier, RawBar, parse_decimal_str, timeframe_ms};

use crate::coinbase_spot::precision_for_coinbase_product;
use crate::error::PackError;
use crate::okx_perp::connector::quantize_dp;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector};

/// Default Coinbase Exchange REST API base URL.
pub const DEFAULT_BASE_URL: &str = "https://api.exchange.coinbase.com";
/// Maximum candles returned per Coinbase REST `/candles` page.
pub const MAX_CANDLES: usize = 300;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe to Coinbase Exchange `(granularity_sec, needs_4h_aggregation)`.
pub fn coinbase_granularity_for(timeframe: &str) -> Result<(i64, bool), PackError> {
    match timeframe {
        "1m" => Ok((60, false)),
        "5m" => Ok((300, false)),
        "15m" => Ok((900, false)),
        "1h" => Ok((3600, false)),
        "4h" => Ok((3600, true)),
        "1d" => Ok((86400, false)),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported Coinbase timeframe `{other}`"
        ))),
    }
}

/// Strictly decode a JSON string OR JSON number (`Number::to_string()`, never `as_f64()`) into `Decimal`.
pub(crate) fn parse_cb_decimal(value: &Value, field: &str) -> Result<Decimal, PackError> {
    if let Some(s) = value.as_str() {
        return parse_decimal_str(s)
            .map_err(|err| PackError::FeedUnavailable(format!("invalid Coinbase `{field}`: {err}")));
    }
    if let Some(num) = value.as_number() {
        return parse_decimal_str(&num.to_string())
            .map_err(|err| PackError::FeedUnavailable(format!("invalid Coinbase `{field}`: {err}")));
    }
    Err(PackError::FeedUnavailable(format!(
        "Coinbase field `{field}` must be string or number"
    )))
}

/// Parse Coinbase `/products/{id}/candles` row `[time_sec, low, high, open, close, volume]`.
pub(crate) fn parse_coinbase_candle_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawBar, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("Coinbase candle row must be an array".to_owned())
    })?;
    if arr.len() < 6 {
        return Err(PackError::FeedUnavailable(
            "Coinbase candle row shorter than 6 columns".to_owned(),
        ));
    }
    let ts_sec = arr[0]
        .as_i64()
        .or_else(|| arr[0].as_str().and_then(|s| s.parse::<i64>().ok()))
        .ok_or_else(|| PackError::FeedUnavailable("Coinbase candle missing `time`".to_owned()))?;
    let ts_event = ts_sec
        .checked_mul(1000)
        .ok_or_else(|| PackError::FeedUnavailable("Coinbase timestamp overflow".to_owned()))?;

    let (price_dp, qty_dp) = precision_for_coinbase_product(symbol);
    let price_u = price_dp.max(0) as u32;
    let qty_u = qty_dp.max(0) as u32;

    let low = quantize_dp(parse_cb_decimal(&arr[1], "low")?, price_u);
    let high = quantize_dp(parse_cb_decimal(&arr[2], "high")?, price_u);
    let open = quantize_dp(parse_cb_decimal(&arr[3], "open")?, price_u);
    let close = quantize_dp(parse_cb_decimal(&arr[4], "close")?, price_u);
    let volume = quantize_dp(parse_cb_decimal(&arr[5], "volume")?, qty_u);
    let turnover = quantize_dp(close * volume, price_u + qty_u);

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
        revision: 1,
        confirmed: true,
        history_only: false,
        synthetic: false,
    })
}

/// Aggregate sorted 1h bars into aligned closed 4h bars in pure `Decimal`.
pub(crate) fn aggregate_hourly_to_4h(
    hourly: Vec<RawBar>,
    symbol: &str,
    now_ms: i64,
) -> Vec<RawBar> {
    const FOUR_HOURS_MS: i64 = 4 * 3_600_000;
    let (price_dp, qty_dp) = precision_for_coinbase_product(symbol);
    let mut grouped: BTreeMap<i64, Vec<RawBar>> = BTreeMap::new();
    for bar in hourly {
        let bucket = bar.ts_event - bar.ts_event.rem_euclid(FOUR_HOURS_MS);
        grouped.entry(bucket).or_default().push(bar);
    }

    let mut out = Vec::new();
    for (bucket_ts, mut slice) in grouped {
        if bucket_ts + FOUR_HOURS_MS > now_ms {
            continue;
        }
        slice.sort_by_key(|b| b.ts_event);
        let first = &slice[0];
        let last = &slice[slice.len() - 1];
        let mut high = first.high;
        let mut low = first.low;
        let mut volume = Decimal::ZERO;
        let mut turnover = Decimal::ZERO;
        for b in &slice {
            if b.high > high {
                high = b.high;
            }
            if b.low < low {
                low = b.low;
            }
            volume += b.volume;
            turnover += b.turnover;
        }
        let price_u = price_dp.max(0) as u32;
        let qty_u = qty_dp.max(0) as u32;
        out.push(RawBar {
            symbol: symbol.to_owned(),
            timeframe: "4h".to_owned(),
            ts_event: bucket_ts,
            open: first.open,
            high,
            low,
            close: last.close,
            volume: quantize_dp(volume, qty_u),
            turnover: quantize_dp(turnover, price_u + qty_u),
            ts_available: bucket_ts + FOUR_HOURS_MS,
            ts_ingested: now_ms,
            ts_committed: now_ms,
            precision: PrecisionTier::SecondVerified,
            revision: 1,
            confirmed: true,
            history_only: false,
            synthetic: false,
        });
    }
    out
}

fn format_iso_ms(ts_ms: i64) -> String {
    DateTime::<Utc>::from_timestamp_millis(ts_ms)
        .unwrap_or_else(Utc::now)
        .to_rfc3339_opts(SecondsFormat::Secs, true)
}

/// Async Coinbase Exchange REST connector.
#[derive(Clone)]
pub struct CoinbaseSpotConnector {
    base_url: String,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl CoinbaseSpotConnector {
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

    pub fn with_sleeper(mut self, sleeper: AsyncSleepFn) -> Self {
        self.sleeper = sleeper;
        self
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
            "Coinbase spot market data unavailable: {}",
            last_error.unwrap_or_else(|| "request failed".to_owned())
        )))
    }
}

impl HistoryConnector for CoinbaseSpotConnector {
    fn feed_mode(&self) -> &str {
        "coinbase"
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
            let _ = timeframe_ms(timeframe)?;
            let (granularity_sec, needs_4h) = coinbase_granularity_for(timeframe)?;
            let gran_ms = granularity_sec * 1000;
            let now = (self.now_ms)();
            let effective_end = end_ts.unwrap_or(now);

            let max_bars = if needs_4h {
                (limit.saturating_mul(4)).min(MAX_CANDLES)
            } else {
                limit.min(MAX_CANDLES)
            };
            let start_ms = effective_end - (max_bars as i64) * gran_ms;
            let path = format!("/products/{symbol}/candles");
            let params = [
                ("granularity", granularity_sec.to_string()),
                ("start", format_iso_ms(start_ms)),
                ("end", format_iso_ms(effective_end)),
            ];
            let payload = self.request_json(&path, &params).await?;
            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Coinbase candles must return an array".to_owned())
            })?;
            let fetch_tf = if needs_4h { "1h" } else { timeframe };
            let mut raw_bars = Vec::with_capacity(rows.len());
            for row in rows {
                let bar = parse_coinbase_candle_row(
                    row,
                    symbol,
                    fetch_tf,
                    gran_ms,
                    Some(now),
                    Some(now),
                )?;
                if bar.ts_event + gran_ms > now {
                    continue;
                }
                if end_ts.is_some_and(|e| bar.ts_event > e) {
                    continue;
                }
                raw_bars.push(bar);
            }
            raw_bars.sort_by_key(|b| b.ts_event);

            if needs_4h {
                let mut agg = aggregate_hourly_to_4h(raw_bars, symbol, now);
                if agg.len() > limit {
                    agg.drain(..agg.len() - limit);
                }
                return Ok(agg);
            }

            if raw_bars.len() > limit {
                raw_bars.drain(..raw_bars.len() - limit);
            }
            Ok(raw_bars)
        })
    }
}

/// Offline deterministic fixture connector reading `tests/fixtures/coinbase/`.
#[derive(Debug, Clone)]
pub struct CoinbaseFixtureConnector {
    fixture_dir: PathBuf,
}

impl CoinbaseFixtureConnector {
    pub fn new(fixture_dir: impl Into<PathBuf>) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
        }
    }
}

impl HistoryConnector for CoinbaseFixtureConnector {
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
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let interval = timeframe_ms(timeframe)?;
            let direct = self.fixture_dir.join(symbol).join(format!("{timeframe}.json"));
            let (path, fetch_tf, fetch_iv, aggregate) = if direct.is_file() {
                (direct, timeframe, interval, false)
            } else if timeframe == "4h" {
                (
                    self.fixture_dir.join(symbol).join("1h.json"),
                    "1h",
                    3_600_000,
                    true,
                )
            } else {
                (direct, timeframe, interval, false)
            };

            if !path.is_file() {
                return Err(PackError::FeedUnavailable(format!(
                    "missing Coinbase offline fixture `{}`",
                    path.display()
                )));
            }
            let raw = fs::read_to_string(&path).map_err(|err| {
                PackError::FeedUnavailable(format!("cannot read `{}`: {err}", path.display()))
            })?;
            let payload: Value = serde_json::from_str(&raw).map_err(|err| {
                PackError::FeedUnavailable(format!("invalid JSON in `{}`: {err}", path.display()))
            })?;
            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Coinbase candle fixture must be an array".to_owned())
            })?;
            let mut bars = Vec::with_capacity(rows.len());
            for row in rows {
                let b = parse_coinbase_candle_row(row, symbol, fetch_tf, fetch_iv, None, None)?;
                if end_ts.is_some_and(|e| b.ts_event > e) {
                    continue;
                }
                bars.push(b);
            }
            bars.sort_by_key(|b| b.ts_event);
            if aggregate {
                bars = aggregate_hourly_to_4h(bars, symbol, i64::MAX);
            }
            if bars.len() > limit {
                bars.drain(..bars.len() - limit);
            }
            Ok(bars)
        })
    }
}
