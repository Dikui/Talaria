//! OKX V5 public REST & offline fixture connectors with strict `Decimal` boundary parsing.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rust_decimal::{Decimal, RoundingStrategy};
use serde_json::Value;
use talaria_contracts::{
    DerivativeKind, PrecisionTier, RawBar, RawDerivativePoint, parse_decimal_str, timeframe_ms,
};

use crate::error::PackError;
use crate::okx_perp::precision_for_okx_symbol;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector};

/// Default OKX V5 REST API base URL.
pub const DEFAULT_BASE_URL: &str = "https://www.okx.com";
/// Recent candles endpoint page limit (`/api/v5/market/candles`).
pub const CANDLES_LIMIT: usize = 300;
/// History candles endpoint page limit (`/api/v5/market/history-candles`).
pub const HISTORY_CANDLES_LIMIT: usize = 100;
/// Funding rate history page limit (`/api/v5/public/funding-rate-history`).
pub const FUNDING_LIMIT: usize = 100;
/// Open-interest history page limit (`/api/v5/rubik/stat/contracts/open-interest-history`).
pub const OI_LIMIT: usize = 100;
/// Fixed scale for cold funding rate storage (`talaria-store` ADR-0017).
const RATE_SCALE: u32 = 8;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe label to OKX V5 `bar` token.
pub fn okx_bar_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" => Ok("1m"),
        "5m" => Ok("5m"),
        "15m" => Ok("15m"),
        "1h" => Ok("1H"),
        "4h" => Ok("4H"),
        "1d" => Ok("1Dutc"),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported OKX timeframe `{other}`"
        ))),
    }
}

/// Map Talaria timeframe label to OKX `/api/v5/rubik/stat/contracts/open-interest-history` `period`.
pub fn okx_oi_period_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" | "5m" => Ok("5m"),
        "15m" => Ok("15m"),
        "1h" => Ok("1H"),
        "4h" => Ok("4H"),
        "1d" => Ok("1D"),
        other => Err(PackError::FeedUnavailable(format!(
            "OKX open interest does not support timeframe `{other}`"
        ))),
    }
}

/// Strictly parse an OKX JSON string/integer timestamp in UTC ms.
pub(crate) fn parse_okx_i64(value: &Value, field: &str) -> Result<i64, PackError> {
    if let Some(n) = value.as_i64() {
        return Ok(n);
    }
    if let Some(s) = value.as_str() {
        return s
            .parse::<i64>()
            .map_err(|_| PackError::FeedUnavailable(format!("invalid OKX integer `{field}`: {s}")));
    }
    Err(PackError::FeedUnavailable(format!(
        "OKX field `{field}` must be an integer or numeric string"
    )))
}

/// Strictly parse an OKX JSON string decimal field without touching binary floats.
pub(crate) fn parse_okx_decimal_str(value: &Value, field: &str) -> Result<Decimal, PackError> {
    let raw = value.as_str().ok_or_else(|| {
        PackError::FeedUnavailable(format!("OKX field `{field}` must be a DecimalStr string"))
    })?;
    parse_decimal_str(raw)
        .map_err(|err| PackError::FeedUnavailable(format!("invalid OKX `{field}`: {err}")))
}

pub(crate) fn quantize_dp(value: Decimal, max_dp: u32) -> Decimal {
    let norm = value.normalize();
    if norm.scale() <= max_dp {
        norm
    } else {
        norm.round_dp_with_strategy(max_dp, RoundingStrategy::MidpointNearestEven)
            .normalize()
    }
}

/// Validate an OKX V5 envelope (`code == "0"`) and return `data` array.
pub(crate) fn parse_okx_envelope_data(payload: &Value) -> Result<&Vec<Value>, PackError> {
    let obj = payload
        .as_object()
        .ok_or_else(|| PackError::FeedUnavailable("OKX payload must be a JSON object".to_owned()))?;
    let code = obj.get("code").and_then(Value::as_str);
    if code != Some("0") {
        let msg = obj
            .get("msg")
            .and_then(Value::as_str)
            .unwrap_or("unknown error");
        return Err(PackError::FeedUnavailable(format!(
            "OKX returned error {code:?}: {msg}"
        )));
    }
    obj.get("data")
        .and_then(Value::as_array)
        .ok_or_else(|| PackError::FeedUnavailable("OKX payload missing `data` array".to_owned()))
}

/// Parse OKX V5 candle row `[ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]`.
/// Returns `Ok(None)` when `require_closed` is `true` and `confirm != "1"`.
pub(crate) fn parse_okx_kline_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    require_closed: bool,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<Option<(RawBar, bool)>, PackError> {
    let arr = row
        .as_array()
        .ok_or_else(|| PackError::FeedUnavailable("OKX candle row must be an array".to_owned()))?;
    if arr.len() < 9 {
        return Err(PackError::FeedUnavailable(
            "OKX candle row shorter than 9 columns".to_owned(),
        ));
    }
    let is_closed = arr[8].as_str() == Some("1");
    if require_closed && !is_closed {
        return Ok(None);
    }

    let (price_dp, qty_dp) = precision_for_okx_symbol(symbol);
    let price_u = price_dp.max(0) as u32;
    let qty_u = qty_dp.max(0) as u32;

    let ts_event = parse_okx_i64(&arr[0], "ts")?;
    let open = quantize_dp(parse_okx_decimal_str(&arr[1], "open")?, price_u);
    let high = quantize_dp(parse_okx_decimal_str(&arr[2], "high")?, price_u);
    let low = quantize_dp(parse_okx_decimal_str(&arr[3], "low")?, price_u);
    let close = quantize_dp(parse_okx_decimal_str(&arr[4], "close")?, price_u);
    let volume = quantize_dp(parse_okx_decimal_str(&arr[6], "volCcy")?, qty_u);
    let turnover = quantize_dp(
        parse_okx_decimal_str(&arr[7], "volCcyQuote")?,
        price_u + qty_u,
    );

    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(Some((
        RawBar {
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
        },
        is_closed,
    )))
}

/// Parse OKX mark-price candle row `[ts, o, h, l, c, confirm]`.
pub(crate) fn parse_okx_mark_price_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<Option<RawDerivativePoint>, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("OKX mark-price candle row must be an array".to_owned())
    })?;
    if arr.len() < 6 {
        return Err(PackError::FeedUnavailable(
            "OKX mark-price row shorter than 6 columns".to_owned(),
        ));
    }
    if arr[5].as_str() != Some("1") {
        return Ok(None);
    }
    let (price_dp, _) = precision_for_okx_symbol(symbol);
    let ts_event = parse_okx_i64(&arr[0], "ts")?;
    let value = quantize_dp(
        parse_okx_decimal_str(&arr[4], "markClose")?,
        price_dp.max(0) as u32,
    );
    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(Some(RawDerivativePoint {
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
    }))
}

/// Parse OKX `/api/v5/public/funding-rate-history` object row.
pub(crate) fn parse_okx_funding_row(
    row: &Value,
    symbol: &str,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("OKX funding row must be a JSON object".to_owned())
    })?;
    let ts_val = obj
        .get("fundingTime")
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingTime`".to_owned()))?;
    let rate_val = obj
        .get("realizedRate")
        .filter(|v| !v.is_null() && v.as_str().is_some_and(|s| !s.is_empty()))
        .or_else(|| obj.get("fundingRate"))
        .ok_or_else(|| PackError::FeedUnavailable("missing `fundingRate`".to_owned()))?;
    let ts_event = parse_okx_i64(ts_val, "fundingTime")?;
    let value = quantize_dp(parse_okx_decimal_str(rate_val, "fundingRate")?, RATE_SCALE);
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

/// Parse OKX `/api/v5/rubik/stat/contracts/open-interest-history` row `[ts, oiCont, oiCcy, oiUsd]`.
pub(crate) fn parse_okx_open_interest_row(
    row: &Value,
    symbol: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let arr = row.as_array().ok_or_else(|| {
        PackError::FeedUnavailable("OKX open-interest row must be an array".to_owned())
    })?;
    if arr.len() < 3 {
        return Err(PackError::FeedUnavailable(
            "OKX open-interest row shorter than 3 columns".to_owned(),
        ));
    }
    let (_, qty_dp) = precision_for_okx_symbol(symbol);
    let ts_event = parse_okx_i64(&arr[0], "ts")?;
    let value = quantize_dp(parse_okx_decimal_str(&arr[2], "oiCcy")?, qty_dp.max(0) as u32);
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

/// Async OKX V5 REST market data connector.
#[derive(Clone)]
pub struct OkxPerpConnector {
    base_url: String,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl OkxPerpConnector {
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
            "OKX market data unavailable: {}",
            last_error.unwrap_or_else(|| "request failed".to_owned())
        )))
    }
}

impl HistoryConnector for OkxPerpConnector {
    fn feed_mode(&self) -> &str {
        "okx"
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
            let bar = okx_bar_for(timeframe)?;
            let mut params = vec![
                ("instId", symbol.to_owned()),
                ("bar", bar.to_owned()),
                ("limit", limit.min(CANDLES_LIMIT).to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("after", (end.saturating_add(1)).to_string()));
            }
            let payload = self.request_json("/api/v5/market/candles", &params).await?;
            let mut list = parse_okx_envelope_data(&payload)?.clone();
            if list.is_empty() && end_ts.is_some() {
                let mut hist_params = vec![
                    ("instId", symbol.to_owned()),
                    ("bar", bar.to_owned()),
                    ("limit", limit.min(HISTORY_CANDLES_LIMIT).to_string()),
                ];
                if let Some(end) = end_ts {
                    hist_params.push(("after", (end.saturating_add(1)).to_string()));
                }
                let hist_payload = self
                    .request_json("/api/v5/market/history-candles", &hist_params)
                    .await?;
                list = parse_okx_envelope_data(&hist_payload)?.clone();
            }

            let now = (self.now_ms)();
            let mut bars = Vec::with_capacity(list.len());
            for row in &list {
                if let Some((bar_row, _)) =
                    parse_okx_kline_row(row, symbol, timeframe, interval, true, Some(now), Some(now))?
                {
                    if end_ts.is_some_and(|e| bar_row.ts_event > e) {
                        continue;
                    }
                    bars.push(bar_row);
                }
            }
            bars.sort_by_key(|b| b.ts_event);
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
            let interval = timeframe_ms(timeframe)?;
            let bar = okx_bar_for(timeframe)?;
            let mut params = vec![
                ("instId", symbol.to_owned()),
                ("bar", bar.to_owned()),
                ("limit", limit.min(HISTORY_CANDLES_LIMIT).to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("after", (end.saturating_add(1)).to_string()));
            }
            let payload = self
                .request_json("/api/v5/market/mark-price-candles", &params)
                .await?;
            let mut list = parse_okx_envelope_data(&payload)?.clone();
            if list.is_empty() && end_ts.is_some() {
                let hist_payload = self
                    .request_json("/api/v5/market/history-mark-price-candles", &params)
                    .await?;
                list = parse_okx_envelope_data(&hist_payload)?.clone();
            }

            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(list.len());
            for row in &list {
                if let Some(pt) =
                    parse_okx_mark_price_row(row, symbol, timeframe, interval, Some(now), Some(now))?
                {
                    if end_ts.is_some_and(|e| pt.ts_event > e) {
                        continue;
                    }
                    points.push(pt);
                }
            }
            points.sort_by_key(|p| p.ts_event);
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
            let mut params = vec![
                ("instId", symbol.to_owned()),
                ("limit", limit.min(FUNDING_LIMIT).to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("after", (end.saturating_add(1)).to_string()));
            }
            let payload = self
                .request_json("/api/v5/public/funding-rate-history", &params)
                .await?;
            let list = parse_okx_envelope_data(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(list.len());
            for row in list {
                let pt = parse_okx_funding_row(row, symbol, Some(now), Some(now))?;
                if end_ts.is_some_and(|e| pt.ts_event > e) {
                    continue;
                }
                points.push(pt);
            }
            points.sort_by_key(|p| p.ts_event);
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
            let interval = timeframe_ms(timeframe)?;
            let period = okx_oi_period_for(timeframe)?;
            let mut params = vec![
                ("instId", symbol.to_owned()),
                ("period", period.to_owned()),
                ("limit", limit.min(OI_LIMIT).to_string()),
            ];
            if let Some(end) = end_ts {
                params.push(("endTime", (end.saturating_add(1)).to_string()));
            }
            let payload = self
                .request_json("/api/v5/rubik/stat/contracts/open-interest-history", &params)
                .await?;
            let list = parse_okx_envelope_data(&payload)?;
            let now = (self.now_ms)();
            let mut points = Vec::with_capacity(list.len());
            for row in list {
                let pt = parse_okx_open_interest_row(
                    row,
                    symbol,
                    timeframe,
                    interval,
                    Some(now),
                    Some(now),
                )?;
                if end_ts.is_some_and(|e| pt.ts_event > e) {
                    continue;
                }
                points.push(pt);
            }
            points.sort_by_key(|p| p.ts_event);
            Ok(points)
        })
    }
}

/// Offline deterministic fixture connector reading `tests/fixtures/okx/`.
#[derive(Debug, Clone)]
pub struct OkxFixtureConnector {
    fixture_dir: PathBuf,
}

impl OkxFixtureConnector {
    pub fn new(fixture_dir: impl Into<PathBuf>) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
        }
    }

    fn read_data(&self, path: &Path) -> Result<Vec<Value>, PackError> {
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing OKX offline fixture `{}`",
                path.display()
            )));
        }
        let raw = fs::read_to_string(path).map_err(|err| {
            PackError::FeedUnavailable(format!("cannot read `{}`: {err}", path.display()))
        })?;
        let payload: Value = serde_json::from_str(&raw).map_err(|err| {
            PackError::FeedUnavailable(format!("invalid JSON in `{}`: {err}", path.display()))
        })?;
        Ok(parse_okx_envelope_data(&payload)?.clone())
    }
}

impl HistoryConnector for OkxFixtureConnector {
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
            let path = self.fixture_dir.join(symbol).join(format!("{timeframe}.json"));
            let rows = self.read_data(&path)?;
            let mut bars = Vec::new();
            for row in &rows {
                if let Some((bar, _)) =
                    parse_okx_kline_row(row, symbol, timeframe, interval, true, None, None)?
                {
                    if end_ts.is_some_and(|e| bar.ts_event > e) {
                        continue;
                    }
                    bars.push(bar);
                }
            }
            bars.sort_by_key(|b| b.ts_event);
            let cap = limit.min(CANDLES_LIMIT);
            if bars.len() > cap {
                bars.drain(..bars.len() - cap);
            }
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
            let interval = timeframe_ms(timeframe)?;
            let path = self
                .fixture_dir
                .join(symbol)
                .join("derivatives")
                .join(format!("mark_price_{timeframe}.json"));
            let rows = self.read_data(&path)?;
            let mut pts = Vec::new();
            for row in &rows {
                if let Some(pt) =
                    parse_okx_mark_price_row(row, symbol, timeframe, interval, None, None)?
                {
                    if end_ts.is_some_and(|e| pt.ts_event > e) {
                        continue;
                    }
                    pts.push(pt);
                }
            }
            pts.sort_by_key(|p| p.ts_event);
            if pts.len() > limit {
                pts.drain(..pts.len() - limit);
            }
            Ok(pts)
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
            let path = self
                .fixture_dir
                .join(symbol)
                .join("derivatives")
                .join("funding.json");
            let rows = self.read_data(&path)?;
            let mut pts = Vec::new();
            for row in &rows {
                let pt = parse_okx_funding_row(row, symbol, None, None)?;
                if end_ts.is_some_and(|e| pt.ts_event > e) {
                    continue;
                }
                pts.push(pt);
            }
            pts.sort_by_key(|p| p.ts_event);
            if pts.len() > limit {
                pts.drain(..pts.len() - limit);
            }
            Ok(pts)
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
            let interval = timeframe_ms(timeframe)?;
            let path = self
                .fixture_dir
                .join(symbol)
                .join("derivatives")
                .join(format!("open_interest_{timeframe}.json"));
            let rows = self.read_data(&path)?;
            let mut pts = Vec::new();
            for row in &rows {
                let pt = parse_okx_open_interest_row(row, symbol, timeframe, interval, None, None)?;
                if end_ts.is_some_and(|e| pt.ts_event > e) {
                    continue;
                }
                pts.push(pt);
            }
            pts.sort_by_key(|p| p.ts_event);
            if pts.len() > limit {
                pts.drain(..pts.len() - limit);
            }
            Ok(pts)
        })
    }
}
