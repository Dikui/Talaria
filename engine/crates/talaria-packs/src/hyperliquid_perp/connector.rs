//! Hyperliquid `POST /info` REST & offline fixture connectors with strict `Decimal` boundary parsing.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use reqwest::Client;
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::{
    DerivativeKind, PrecisionTier, RawBar, RawDerivativePoint, parse_decimal_str, timeframe_ms,
};

use crate::error::PackError;
use crate::hyperliquid_perp::precision_for_hl_coin;
use crate::okx_perp::connector::quantize_dp;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector};

/// Default Hyperliquid REST info base URL.
pub const DEFAULT_BASE_URL: &str = "https://api.hyperliquid.xyz";
/// Maximum recent candles retained by Hyperliquid REST `candleSnapshot`.
pub const MAX_RECENT_CANDLES: usize = 5000;
/// Maximum candles typically served per `candleSnapshot` query window.
pub const CANDLE_PAGE_LIMIT: usize = 500;
const RATE_SCALE: u32 = 8;

pub type AsyncSleepFn = Arc<dyn Fn(Duration) -> BoxFuture<'static, ()> + Send + Sync>;

fn default_sleeper() -> AsyncSleepFn {
    Arc::new(|d| Box::pin(tokio::time::sleep(d)))
}

/// Map Talaria timeframe label to Hyperliquid interval token.
pub fn hl_interval_for(timeframe: &str) -> Result<&'static str, PackError> {
    match timeframe {
        "1m" => Ok("1m"),
        "5m" => Ok("5m"),
        "15m" => Ok("15m"),
        "1h" => Ok("1h"),
        "4h" => Ok("4h"),
        "1d" => Ok("1d"),
        other => Err(PackError::FeedUnavailable(format!(
            "unsupported Hyperliquid timeframe `{other}`"
        ))),
    }
}

pub(crate) fn parse_hl_decimal_str(value: &Value, field: &str) -> Result<Decimal, PackError> {
    let raw = value.as_str().ok_or_else(|| {
        PackError::FeedUnavailable(format!("Hyperliquid field `{field}` must be a string"))
    })?;
    parse_decimal_str(raw)
        .map_err(|err| PackError::FeedUnavailable(format!("invalid Hyperliquid `{field}`: {err}")))
}

/// Parse one Hyperliquid candle object `{"t","T","s","i","o","c","h","l","v","n"}`.
pub(crate) fn parse_hl_candle(
    row: &Value,
    coin: &str,
    timeframe: &str,
    interval: i64,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawBar, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Hyperliquid candle must be a JSON object".to_owned())
    })?;
    let ts_event = obj
        .get("t")
        .and_then(Value::as_i64)
        .ok_or_else(|| PackError::FeedUnavailable("Hyperliquid candle missing integer `t`".to_owned()))?;
    let (price_dp, qty_dp) = precision_for_hl_coin(coin);
    let price_u = price_dp.max(0) as u32;
    let qty_u = qty_dp.max(0) as u32;

    let open = quantize_dp(parse_hl_decimal_str(&obj["o"], "o")?, price_u);
    let high = quantize_dp(parse_hl_decimal_str(&obj["h"], "h")?, price_u);
    let low = quantize_dp(parse_hl_decimal_str(&obj["l"], "l")?, price_u);
    let close = quantize_dp(parse_hl_decimal_str(&obj["c"], "c")?, price_u);
    let volume = quantize_dp(parse_hl_decimal_str(&obj["v"], "v")?, qty_u);
    let turnover = quantize_dp(close * volume, price_u + qty_u);

    let ts_available = ts_event + interval;
    let ingested = ts_ingested.unwrap_or(ts_available);
    let committed = ts_committed.unwrap_or(ts_available);

    Ok(RawBar {
        symbol: coin.to_owned(),
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

/// Parse one Hyperliquid `fundingHistory` object `{"coin","fundingRate","premium","time"}`.
pub(crate) fn parse_hl_funding(
    row: &Value,
    coin: &str,
    ts_ingested: Option<i64>,
    ts_committed: Option<i64>,
) -> Result<RawDerivativePoint, PackError> {
    let obj = row.as_object().ok_or_else(|| {
        PackError::FeedUnavailable("Hyperliquid funding row must be a JSON object".to_owned())
    })?;
    let ts_event = obj
        .get("time")
        .and_then(Value::as_i64)
        .ok_or_else(|| PackError::FeedUnavailable("Hyperliquid funding missing `time`".to_owned()))?;
    let rate = quantize_dp(parse_hl_decimal_str(&obj["fundingRate"], "fundingRate")?, RATE_SCALE);
    let ingested = ts_ingested.unwrap_or(ts_event);
    let committed = ts_committed.unwrap_or(ts_event);
    Ok(RawDerivativePoint {
        kind: DerivativeKind::FundingRate,
        symbol: coin.to_owned(),
        timeframe: None,
        ts_event,
        value: rate,
        ts_available: ts_event,
        ts_ingested: ingested,
        ts_committed: committed,
        ts_published: Some(ts_event),
        precision: PrecisionTier::SecondVerified,
    })
}

/// Parse Hyperliquid `metaAndAssetCtxs` snapshot for `coin`, returning `(mark_price, open_interest)`.
pub(crate) fn parse_hl_meta_ctx(
    payload: &Value,
    coin: &str,
    timeframe: &str,
    ts_now: i64,
) -> Result<(Option<RawDerivativePoint>, Option<RawDerivativePoint>), PackError> {
    let Some(outer) = payload.as_array() else {
        return Err(PackError::FeedUnavailable(
            "Hyperliquid metaAndAssetCtxs must be a 2-element array".to_owned(),
        ));
    };
    if outer.len() < 2 {
        return Ok((None, None));
    }
    let Some(universe) = outer[0].get("universe").and_then(Value::as_array) else {
        return Ok((None, None));
    };
    let Some(ctxs) = outer[1].as_array() else {
        return Ok((None, None));
    };
    let idx = universe
        .iter()
        .position(|item| item.get("name").and_then(Value::as_str) == Some(coin));
    let Some(idx) = idx else {
        return Ok((None, None));
    };
    let Some(ctx) = ctxs.get(idx).and_then(Value::as_object) else {
        return Ok((None, None));
    };
    let (price_dp, qty_dp) = precision_for_hl_coin(coin);

    let mark = ctx
        .get("markPx")
        .and_then(|v| parse_hl_decimal_str(v, "markPx").ok())
        .map(|v| RawDerivativePoint {
            kind: DerivativeKind::MarkPrice,
            symbol: coin.to_owned(),
            timeframe: Some(timeframe.to_owned()),
            ts_event: ts_now,
            value: quantize_dp(v, price_dp.max(0) as u32),
            ts_available: ts_now,
            ts_ingested: ts_now,
            ts_committed: ts_now,
            ts_published: Some(ts_now),
            precision: PrecisionTier::SecondVerified,
        });

    let oi = ctx
        .get("openInterest")
        .and_then(|v| parse_hl_decimal_str(v, "openInterest").ok())
        .map(|v| RawDerivativePoint {
            kind: DerivativeKind::OpenInterest,
            symbol: coin.to_owned(),
            timeframe: Some(timeframe.to_owned()),
            ts_event: ts_now,
            value: quantize_dp(v, qty_dp.max(0) as u32),
            ts_available: ts_now,
            ts_ingested: ts_now,
            ts_committed: ts_now,
            ts_published: Some(ts_now),
            precision: PrecisionTier::SecondVerified,
        });

    Ok((mark, oi))
}

/// Async Hyperliquid `/info` REST connector.
#[derive(Clone)]
pub struct HyperliquidConnector {
    base_url: String,
    client: Client,
    retries: usize,
    now_ms: ClockFn,
    sleeper: AsyncSleepFn,
}

impl HyperliquidConnector {
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

    async fn post_info(&self, body: Value) -> Result<Value, PackError> {
        let url = format!("{}/info", self.base_url);
        let mut last_error: Option<String> = None;

        for attempt in 0..=self.retries {
            let result = async {
                let response = self.client.post(&url).json(&body).send().await?;
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
            "Hyperliquid /info unavailable: {}",
            last_error.unwrap_or_else(|| "request failed".to_owned())
        )))
    }
}

impl HistoryConnector for HyperliquidConnector {
    fn feed_mode(&self) -> &str {
        "hyperliquid"
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
            let hl_tf = hl_interval_for(timeframe)?;
            let now = (self.now_ms)();
            let effective_end = end_ts.unwrap_or(now);
            let span_count = limit.clamp(1, CANDLE_PAGE_LIMIT) as i64;
            let earliest_allowed = now - (MAX_RECENT_CANDLES as i64) * interval;
            if effective_end < earliest_allowed && self.base_url == DEFAULT_BASE_URL {
                return Ok(Vec::new());
            }
            let start_time = (effective_end - span_count * interval).max(0);
            let payload = self
                .post_info(json!({
                    "type": "candleSnapshot",
                    "req": {
                        "coin": symbol,
                        "interval": hl_tf,
                        "startTime": start_time,
                        "endTime": effective_end,
                    }
                }))
                .await?;

            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Hyperliquid candleSnapshot must return an array".to_owned())
            })?;
            let mut bars = Vec::with_capacity(rows.len());
            for row in rows {
                let bar = parse_hl_candle(row, symbol, timeframe, interval, Some(now), Some(now))?;
                if bar.ts_event + interval > now {
                    continue;
                }
                if end_ts.is_some_and(|e| bar.ts_event > e) {
                    continue;
                }
                bars.push(bar);
            }
            bars.sort_by_key(|b| b.ts_event);
            if bars.len() > limit {
                bars.drain(..bars.len() - limit);
            }
            Ok(bars)
        })
    }

    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        _end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let _ = timeframe_ms(timeframe)?;
            let payload = self.post_info(json!({"type": "metaAndAssetCtxs"})).await?;
            let now = (self.now_ms)();
            let (mark, _) = parse_hl_meta_ctx(&payload, symbol, timeframe, now)?;
            Ok(mark.into_iter().collect())
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
            let now = (self.now_ms)();
            let effective_end = end_ts.unwrap_or(now);
            let start_time = (effective_end - (limit.min(500) as i64) * 3_600_000).max(0);
            let payload = self
                .post_info(json!({
                    "type": "fundingHistory",
                    "coin": symbol,
                    "startTime": start_time,
                    "endTime": effective_end,
                }))
                .await?;
            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Hyperliquid fundingHistory must return an array".to_owned())
            })?;
            let mut out = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_hl_funding(row, symbol, Some(now), Some(now))?;
                if end_ts.is_some_and(|e| pt.ts_event > e) {
                    continue;
                }
                out.push(pt);
            }
            out.sort_by_key(|p| p.ts_event);
            if out.len() > limit {
                out.drain(..out.len() - limit);
            }
            Ok(out)
        })
    }

    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        _end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let _ = timeframe_ms(timeframe)?;
            let payload = self.post_info(json!({"type": "metaAndAssetCtxs"})).await?;
            let now = (self.now_ms)();
            let (_, oi) = parse_hl_meta_ctx(&payload, symbol, timeframe, now)?;
            Ok(oi.into_iter().collect())
        })
    }
}

/// Offline deterministic fixture connector reading `tests/fixtures/hyperliquid/`.
#[derive(Debug, Clone)]
pub struct HyperliquidFixtureConnector {
    fixture_dir: PathBuf,
}

impl HyperliquidFixtureConnector {
    pub fn new(fixture_dir: impl Into<PathBuf>) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
        }
    }

    fn read_json(&self, path: &Path) -> Result<Value, PackError> {
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing Hyperliquid offline fixture `{}`",
                path.display()
            )));
        }
        let raw = fs::read_to_string(path).map_err(|err| {
            PackError::FeedUnavailable(format!("cannot read `{}`: {err}", path.display()))
        })?;
        serde_json::from_str(&raw).map_err(|err| {
            PackError::FeedUnavailable(format!("invalid JSON in `{}`: {err}", path.display()))
        })
    }
}

impl HistoryConnector for HyperliquidFixtureConnector {
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
            let payload = self.read_json(&path)?;
            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Hyperliquid candle fixture must be an array".to_owned())
            })?;
            let mut bars = Vec::with_capacity(rows.len());
            for row in rows {
                let bar = parse_hl_candle(row, symbol, timeframe, interval, None, None)?;
                if end_ts.is_some_and(|e| bar.ts_event > e) {
                    continue;
                }
                bars.push(bar);
            }
            bars.sort_by_key(|b| b.ts_event);
            if bars.len() > limit {
                bars.drain(..bars.len() - limit);
            }
            Ok(bars)
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
            let payload = self.read_json(&path)?;
            let rows = payload.as_array().ok_or_else(|| {
                PackError::FeedUnavailable("Hyperliquid funding fixture must be an array".to_owned())
            })?;
            let mut pts = Vec::with_capacity(rows.len());
            for row in rows {
                let pt = parse_hl_funding(row, symbol, None, None)?;
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

    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        _end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let _ = timeframe_ms(timeframe)?;
            let path = self.fixture_dir.join("meta_asset_ctxs.json");
            let payload = self.read_json(&path)?;
            let (mark, _) = parse_hl_meta_ctx(&payload, symbol, timeframe, 0)?;
            Ok(mark.into_iter().collect())
        })
    }

    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        _end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            if limit == 0 {
                return Ok(Vec::new());
            }
            let _ = timeframe_ms(timeframe)?;
            let path = self.fixture_dir.join("meta_asset_ctxs.json");
            let payload = self.read_json(&path)?;
            let (_, oi) = parse_hl_meta_ctx(&payload, symbol, timeframe, 0)?;
            Ok(oi.into_iter().collect())
        })
    }
}
