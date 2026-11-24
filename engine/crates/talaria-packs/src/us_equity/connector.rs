//! Alpaca Market Data v2 REST connector and offline fixture connector with strict IEX/SIP separation.

use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Utc};
use reqwest::Client;
use rust_decimal::Decimal;
use serde_json::{Map, Value};
use talaria_contracts::{PrecisionTier, RawBar, parse_decimal_str, timeframe_ms};

use crate::error::PackError;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector, default_clock};
use crate::us_equity::AlpacaDataFeed;
use crate::us_equity::session::is_within_feed_hours;

/// Default Alpaca Market Data v2 REST base URL.
pub const DEFAULT_BASE_URL: &str = "https://data.alpaca.markets";
/// Maximum bars per Alpaca `/v2/stocks/bars` page.
pub const MAX_LIMIT: usize = 10_000;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe label to Alpaca Market Data v2 `timeframe` parameter.
pub fn alpaca_timeframe_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" => Ok("1Min"),
        "5m" => Ok("5Min"),
        "15m" => Ok("15Min"),
        "1h" => Ok("1Hour"),
        "4h" => Ok("4Hour"),
        "1d" => Ok("1Day"),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported Alpaca timeframe `{other}`"
        ))),
    }
}

/// Convert an Alpaca raw JSON numeric or DecimalStr token into `Decimal` at the boundary.
pub(crate) fn parse_alpaca_decimal(value: &Value, field: &str) -> Result<Decimal, PackError> {
    match value {
        Value::String(s) => parse_decimal_str(s).map_err(|err| {
            PackError::FeedUnavailable(format!("invalid Alpaca `{field}` DecimalStr: {err}"))
        }),
        Value::Number(num) => {
            if let Some(i) = num.as_i64() {
                return Ok(Decimal::from(i));
            }
            if let Some(u) = num.as_u64() {
                return Ok(Decimal::from(u));
            }
            let Some(f) = num.as_f64() else {
                return Err(PackError::FeedUnavailable(format!(
                    "non-finite Alpaca numeric `{field}`"
                )));
            };
            if !f.is_finite() {
                return Err(PackError::FeedUnavailable(format!(
                    "non-finite Alpaca numeric `{field}`"
                )));
            }
            Decimal::from_str(&num.to_string()).map_err(|err| {
                PackError::FeedUnavailable(format!("cannot convert Alpaca `{field}` to Decimal: {err}"))
            })
        }
        _ => Err(PackError::FeedUnavailable(format!(
            "Alpaca field `{field}` must be a DecimalStr string or finite JSON number"
        ))),
    }
}

/// Parse an Alpaca RFC3339 timestamp string or integer UTC millisecond timestamp.
pub(crate) fn parse_alpaca_timestamp_ms(value: &Value, field: &str) -> Result<i64, PackError> {
    if let Some(ms) = value.as_i64() {
        return Ok(ms);
    }
    if let Some(s) = value.as_str() {
        if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
            return Ok(dt.timestamp_millis());
        }
        if let Ok(ms) = s.parse::<i64>() {
            return Ok(ms);
        }
    }
    Err(PackError::FeedUnavailable(format!(
        "Alpaca timestamp `{field}` must be RFC3339 or integer UTC ms"
    )))
}

fn format_rfc3339_ms(ts_ms: i64) -> Result<String, PackError> {
    let dt = DateTime::<Utc>::from_timestamp_millis(ts_ms).ok_or_else(|| {
        PackError::InvalidRequest(format!("out-of-range UTC millisecond timestamp: {ts_ms}"))
    })?;
    Ok(dt.to_rfc3339())
}

/// Enforce explicit feed match when payload carries a `"feed"` tag (forbid silent fallback).
fn verify_explicit_feed(obj: &Map<String, Value>, expected: AlpacaDataFeed) -> Result<(), PackError> {
    if let Some(raw_feed) = obj.get("feed").and_then(Value::as_str)
        && !raw_feed.eq_ignore_ascii_case(expected.as_str())
    {
        return Err(PackError::FeedUnavailable(format!(
            "Alpaca feed mismatch: requested `{}`, payload carries `{raw_feed}` (silent fallback forbidden)",
            expected.as_str()
        )));
    }
    Ok(())
}

fn extract_bars_slice<'a>(
    obj: &'a Map<String, Value>,
    symbol: &str,
) -> Result<&'a Vec<Value>, PackError> {
    let bars_val = obj
        .get("bars")
        .ok_or_else(|| PackError::FeedUnavailable("Alpaca response missing `bars`".to_owned()))?;
    if let Some(arr) = bars_val.as_array() {
        return Ok(arr);
    }
    if let Some(map) = bars_val.as_object() {
        return match map.get(symbol) {
            Some(val) => val.as_array().ok_or_else(|| {
                PackError::FeedUnavailable(format!("Alpaca `bars.{symbol}` must be an array"))
            }),
            None => {
                // Empty multi-symbol page returns `{}` when no bars match the window.
                static EMPTY: Vec<Value> = Vec::new();
                Ok(&EMPTY)
            }
        };
    }
    Err(PackError::FeedUnavailable(
        "Alpaca `bars` must be an array or symbol map".to_owned(),
    ))
}

fn parse_bar_object(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    feed: AlpacaDataFeed,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<Option<RawBar>, PackError> {
    let obj = row
        .as_object()
        .ok_or_else(|| PackError::FeedUnavailable("Alpaca bar row must be a JSON object".to_owned()))?;
    verify_explicit_feed(obj, feed)?;

    let ts_val = obj
        .get("t")
        .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `t`".to_owned()))?;
    let ts_event = parse_alpaca_timestamp_ms(ts_val, "t")?;

    // Intraday bars on IEX are strictly bounded to 08:00-17:00 ET; SIP covers 04:00-20:00 ET.
    if interval < 86_400_000 && !is_within_feed_hours(feed, ts_event) {
        return Ok(None);
    }

    let open = parse_alpaca_decimal(
        obj.get("o")
            .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `o`".to_owned()))?,
        "o",
    )?;
    let high = parse_alpaca_decimal(
        obj.get("h")
            .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `h`".to_owned()))?,
        "h",
    )?;
    let low = parse_alpaca_decimal(
        obj.get("l")
            .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `l`".to_owned()))?,
        "l",
    )?;
    let close = parse_alpaca_decimal(
        obj.get("c")
            .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `c`".to_owned()))?,
        "c",
    )?;
    let volume = parse_alpaca_decimal(
        obj.get("v")
            .ok_or_else(|| PackError::FeedUnavailable("missing Alpaca bar `v`".to_owned()))?,
        "v",
    )?;
    let turnover = if let Some(vw_val) = obj.get("vw").filter(|v| !v.is_null()) {
        let vw = parse_alpaca_decimal(vw_val, "vw")?;
        (vw * volume).round_dp(6)
    } else if let Some(turn_val) = obj.get("turnover").filter(|v| !v.is_null()) {
        parse_alpaca_decimal(turn_val, "turnover")?
    } else {
        (close * volume).round_dp(6)
    };
    let ts_available = ts_event + interval;

    Ok(Some(RawBar {
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
    }))
}

pub(crate) fn parse_alpaca_bars_response(
    payload: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    feed: AlpacaDataFeed,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<(Vec<RawBar>, Option<String>), PackError> {
    let obj = payload.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Alpaca response must be a JSON object".to_owned())
    })?;
    if let Some(msg) = obj.get("message").and_then(Value::as_str)
        && !obj.contains_key("bars")
    {
        return Err(PackError::FeedUnavailable(format!("Alpaca error: {msg}")));
    }
    verify_explicit_feed(obj, feed)?;
    let rows = extract_bars_slice(obj, symbol)?;
    let mut bars = Vec::with_capacity(rows.len());
    for row in rows {
        if let Some(bar) =
            parse_bar_object(row, symbol, timeframe, interval, feed, ts_ingested, ts_committed)?
        {
            bars.push(bar);
        }
    }
    let next_token = obj
        .get("next_page_token")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
        .map(str::to_owned);
    Ok((bars, next_token))
}

/// Async Alpaca Market Data v2 REST [`HistoryConnector`] bound to an explicit [`AlpacaDataFeed`].
#[derive(Clone)]
pub struct AlpacaHistoryConnector {
    base_url: String,
    feed: AlpacaDataFeed,
    api_key: Option<String>,
    api_secret: Option<String>,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl AlpacaHistoryConnector {
    /// Construct an Alpaca REST connector explicitly bound to `feed` (`Iex` or `Sip`).
    pub fn new(
        base_url: impl Into<String>,
        feed: AlpacaDataFeed,
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
            feed,
            api_key: std::env::var("APCA_API_KEY_ID").ok(),
            api_secret: std::env::var("APCA_API_SECRET_KEY").ok(),
            client,
            retries,
            now_ms,
            sleeper: default_sleeper(),
        })
    }

    /// Construct with default configuration targeting [`DEFAULT_BASE_URL`].
    pub fn default_live(feed: AlpacaDataFeed) -> Result<Self, PackError> {
        Self::new(
            DEFAULT_BASE_URL,
            feed,
            Duration::from_secs(10),
            2,
            default_clock(),
        )
    }

    /// Attach explicit Alpaca API credentials for authenticated requests.
    pub fn with_credentials(
        mut self,
        api_key: impl Into<String>,
        api_secret: impl Into<String>,
    ) -> Self {
        self.api_key = Some(api_key.into());
        self.api_secret = Some(api_secret.into());
        self
    }

    /// Override the retry backoff sleeper (e.g., instant sleeper in unit tests).
    pub fn with_sleeper(mut self, sleeper: AsyncSleepFn) -> Self {
        self.sleeper = sleeper;
        self
    }

    pub fn feed(&self) -> AlpacaDataFeed {
        self.feed
    }

    pub fn base_url(&self) -> &str {
        &self.base_url
    }

    async fn request_json(&self, path: &str, params: &[(&str, String)]) -> Result<Value, PackError> {
        let url = format!("{}{}", self.base_url, path);
        let mut last_error: Option<String> = None;

        for attempt in 0..=self.retries {
            let result = async {
                let mut req = self.client.get(&url).query(params);
                if let (Some(key), Some(secret)) = (&self.api_key, &self.api_secret) {
                    req = req
                        .header("APCA-API-KEY-ID", key)
                        .header("APCA-API-SECRET-KEY", secret);
                }
                let response = req.send().await?;
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
            "Alpaca {} market data unavailable: {}",
            self.feed.as_str(),
            last_error.unwrap_or_else(|| "unknown HTTP failure".to_owned())
        )))
    }
}

impl HistoryConnector for AlpacaHistoryConnector {
    fn feed_mode(&self) -> &str {
        self.feed.feed_mode()
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
            let alpaca_tf = alpaca_timeframe_for(timeframe)?;
            let interval = timeframe_ms(timeframe)?;
            let capped = limit.min(MAX_LIMIT);
            let now = (self.now_ms)();

            let mut collected: Vec<RawBar> = Vec::new();
            let mut page_token: Option<String> = None;
            let mut pages = 0usize;
            const MAX_INTERNAL_PAGES: usize = 8;

            while collected.len() < capped && pages < MAX_INTERNAL_PAGES {
                pages += 1;
                let remaining = capped - collected.len();
                let mut params = vec![
                    ("symbols", symbol.to_owned()),
                    ("timeframe", alpaca_tf.to_owned()),
                    ("feed", self.feed.as_str().to_owned()),
                    ("sort", "desc".to_owned()),
                    ("limit", remaining.to_string()),
                ];
                if let Some(end) = end_ts {
                    params.push(("end", format_rfc3339_ms(end)?));
                }
                if let Some(token) = &page_token {
                    params.push(("page_token", token.clone()));
                }

                let payload = self.request_json("/v2/stocks/bars", &params).await?;
                let (mut page_bars, next) = parse_alpaca_bars_response(
                    &payload,
                    symbol,
                    timeframe,
                    interval,
                    self.feed,
                    Some(now),
                    Some(now),
                )?;
                page_bars.retain(|bar| {
                    bar.ts_event + interval <= now && end_ts.is_none_or(|end| bar.ts_event <= end)
                });
                collected.extend(page_bars);

                match next {
                    Some(tok) if !collected.is_empty() => page_token = Some(tok),
                    _ => break,
                }
            }

            collected.sort_by_key(|b| b.ts_event);
            collected.dedup_by_key(|b| b.ts_event);
            if collected.len() > capped {
                let drop_count = collected.len() - capped;
                collected.drain(..drop_count);
            }
            Ok(collected)
        })
    }
}

/// Offline deterministic [`HistoryConnector`] reading `tests/fixtures/alpaca/{iex|sip}/...`.
///
/// Strictly refuses silent fallback between `iex` and `sip` fixture directories or payloads.
#[derive(Debug, Clone)]
pub struct AlpacaFixtureConnector {
    fixture_dir: PathBuf,
    feed: AlpacaDataFeed,
}

impl AlpacaFixtureConnector {
    /// Construct a fixture connector strictly scoped to `feed` (`<fixture_dir>/{iex|sip}`).
    pub fn new(fixture_dir: impl Into<PathBuf>, feed: AlpacaDataFeed) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
            feed,
        }
    }

    pub fn feed(&self) -> AlpacaDataFeed {
        self.feed
    }

    pub fn fixture_dir(&self) -> &Path {
        &self.fixture_dir
    }

    pub fn fetch_bars_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawBar>, PackError> {
        if limit == 0 {
            return Ok(Vec::new());
        }
        let _ = alpaca_timeframe_for(timeframe)?;
        let interval = timeframe_ms(timeframe)?;

        // Never fall back to the sibling feed directory (`sip` vs `iex`).
        let path = self
            .fixture_dir
            .join(self.feed.as_str())
            .join(symbol)
            .join(format!("{timeframe}.json"));
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing Alpaca `{}` offline fixture `{}` (silent cross-feed fallback forbidden)",
                self.feed.as_str(),
                path.display()
            )));
        }
        let text = fs::read_to_string(&path).map_err(|err| {
            PackError::FeedUnavailable(format!("cannot read fixture `{}`: {err}", path.display()))
        })?;
        let payload: Value = serde_json::from_str(&text).map_err(|err| {
            PackError::FeedUnavailable(format!(
                "invalid JSON in Alpaca fixture `{}`: {err}",
                path.display()
            ))
        })?;
        let (mut bars, _) =
            parse_alpaca_bars_response(&payload, symbol, timeframe, interval, self.feed, None, None)?;
        bars.retain(|bar| end_ts.is_none_or(|end| bar.ts_event <= end));
        bars.sort_by_key(|b| b.ts_event);
        bars.dedup_by_key(|b| b.ts_event);
        let capped = limit.min(MAX_LIMIT);
        if bars.len() > capped {
            let drop_count = bars.len() - capped;
            bars.drain(..drop_count);
        }
        Ok(bars)
    }
}

impl HistoryConnector for AlpacaFixtureConnector {
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
}
