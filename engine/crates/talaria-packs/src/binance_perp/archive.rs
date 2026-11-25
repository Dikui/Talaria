//! Bulk historical backfill connector for Binance Public Data (`data.binance.vision`).
//!
//! Downloads official ZIP + `.CHECKSUM` archives under `data/futures/um/{monthly,daily}/klines/`,
//! verifies SHA-256 integrity before extracting, and decodes the 12-column CSV strictly via `Decimal`.

use std::collections::BTreeMap;
use std::io::{Cursor, Read};
use std::sync::Arc;
use std::time::Duration;

use chrono::{DateTime, Datelike, Duration as ChronoDuration, NaiveDate, Utc};
use reqwest::Client;
use serde_json::Value;
use sha2::{Digest, Sha256};
use talaria_contracts::{PrecisionTier, RawBar, parse_decimal_str, timeframe_ms};
use tokio::sync::Semaphore;
use tokio::task::JoinSet;
use zip::ZipArchive;

use crate::binance_perp::connector::rest_interval_for;
use crate::error::PackError;
use crate::okx_perp::connector::quantize_dp;
use crate::proxy::is_loopback_url;
use crate::traits::{BoxFuture, ClockFn, HistoryConnector};

/// Default Binance Public Data repository base URL.
pub const DEFAULT_ARCHIVE_BASE_URL: &str = "https://data.binance.vision";

/// Public archive dataset selector on `data.binance.vision`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinanceArchiveDataset {
    FuturesUm,
    Spot,
}

impl BinanceArchiveDataset {
    pub fn path_prefix(self) -> &'static str {
        match self {
            Self::FuturesUm => "data/futures/um",
            Self::Spot => "data/spot",
        }
    }

    pub fn archive_symbol(self, symbol: &str) -> &str {
        match self {
            Self::FuturesUm => symbol,
            Self::Spot => symbol.strip_suffix("-SPOT").unwrap_or(symbol),
        }
    }
}

fn precision_for_binance_symbol(symbol: &str) -> (u32, u32) {
    match symbol {
        "BTCUSDT" => (1, 3),
        "ETHUSDT" => (2, 3),
        "SOLUSDT" => (2, 2),
        "BTCUSDT-SPOT" => (2, 5),
        "ETHUSDT-SPOT" => (2, 4),
        "SOLUSDT-SPOT" => (2, 3),
        "1000PEPEUSDT" => (7, 0),
        _ => (4, 4),
    }
}

/// Downloadable ZIP+SHA256 history connector for Binance USD-M Futures & Spot (`data.binance.vision`).
#[derive(Clone)]
pub struct BinanceVisionArchiveConnector {
    base_url: String,
    client: Client,
    now_ms: ClockFn,
    dataset: BinanceArchiveDataset,
}

impl BinanceVisionArchiveConnector {
    pub fn new(
        base_url: impl Into<String>,
        timeout: Duration,
        now_ms: ClockFn,
    ) -> Result<Self, PackError> {
        Self::with_dataset(base_url, timeout, now_ms, BinanceArchiveDataset::FuturesUm)
    }

    pub fn new_spot(
        base_url: impl Into<String>,
        timeout: Duration,
        now_ms: ClockFn,
    ) -> Result<Self, PackError> {
        Self::with_dataset(base_url, timeout, now_ms, BinanceArchiveDataset::Spot)
    }

    fn with_dataset(
        base_url: impl Into<String>,
        timeout: Duration,
        now_ms: ClockFn,
        dataset: BinanceArchiveDataset,
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
            now_ms,
            dataset,
        })
    }

    async fn fetch_optional_bytes(&self, rel_path: &str) -> Result<Option<Vec<u8>>, PackError> {
        let url = format!("{}/{}", self.base_url, rel_path.trim_start_matches('/'));
        let backoffs = [
            Duration::from_millis(500),
            Duration::from_millis(1500),
            Duration::from_millis(3000),
        ];

        for attempt in 0..=backoffs.len() {
            match self.client.get(&url).send().await {
                Ok(resp) => {
                    let status = resp.status();
                    if status == reqwest::StatusCode::NOT_FOUND {
                        return Ok(None);
                    }
                    if status == reqwest::StatusCode::TOO_MANY_REQUESTS || status.is_server_error() {
                        if let Some(delay) = backoffs.get(attempt) {
                            tokio::time::sleep(*delay).await;
                            continue;
                        }
                        return Err(PackError::FeedUnavailable(format!(
                            "archive HTTP {status} for `{url}` after retries"
                        )));
                    }
                    let resp = resp.error_for_status().map_err(|err| {
                        PackError::FeedUnavailable(format!("archive HTTP error `{url}`: {err}"))
                    })?;
                    match resp.bytes().await {
                        Ok(bytes) => return Ok(Some(bytes.to_vec())),
                        Err(err) => {
                            if let Some(delay) = backoffs.get(attempt) {
                                tokio::time::sleep(*delay).await;
                                continue;
                            }
                            return Err(PackError::FeedUnavailable(format!(
                                "archive body `{url}` failed: {err}"
                            )));
                        }
                    }
                }
                Err(err) => {
                    if let Some(delay) = backoffs.get(attempt) {
                        tokio::time::sleep(*delay).await;
                        continue;
                    }
                    return Err(PackError::FeedUnavailable(format!(
                        "archive GET `{url}` failed: {err}"
                    )));
                }
            }
        }
        unreachable!("retry loop always returns")
    }

    async fn fetch_verified_archive_bars(
        &self,
        zip_rel: &str,
        symbol: &str,
        timeframe: &str,
        interval: i64,
        now: i64,
    ) -> Result<Option<Vec<RawBar>>, PackError> {
        let Some(zip_bytes) = self.fetch_optional_bytes(zip_rel).await? else {
            return Ok(None);
        };
        let checksum_rel = format!("{zip_rel}.CHECKSUM");
        let checksum_bytes = self.fetch_optional_bytes(&checksum_rel).await?.ok_or_else(|| {
            PackError::FeedUnavailable(format!("missing archive checksum file `{checksum_rel}`"))
        })?;
        verify_sha256_checksum(&zip_bytes, &checksum_bytes, zip_rel)?;
        let bars = decode_klines_zip_bytes(&zip_bytes, symbol, timeframe, interval, now)?;
        Ok(Some(bars))
    }

    /// Fetch closed bars covering `[start_ts, end_ts]` from monthly (and daily fallback) archives
    /// using bounded concurrency (`8` in-flight downloads).
    pub async fn fetch_bars_in_range(
        &self,
        symbol: &str,
        timeframe: &str,
        start_ts: i64,
        end_ts: i64,
    ) -> Result<Vec<RawBar>, PackError> {
        if end_ts < start_ts {
            return Ok(Vec::new());
        }
        let interval = timeframe_ms(timeframe)?;
        let bn_iv = rest_interval_for(timeframe)?;
        let now = (self.now_ms)();
        let prefix = self.dataset.path_prefix();
        let arch_sym = self.dataset.archive_symbol(symbol);

        let start_date = date_from_ms(start_ts);
        let end_date = date_from_ms(end_ts);
        let months = months_overlapping(start_date, end_date);

        let sem = Arc::new(Semaphore::new(8));
        let mut monthly_set = JoinSet::new();
        for (year, month) in months {
            let connector = self.clone();
            let sem = Arc::clone(&sem);
            let sym = symbol.to_owned();
            let arch = arch_sym.to_owned();
            let tf = timeframe.to_owned();
            let iv_str = bn_iv.to_owned();
            monthly_set.spawn(async move {
                let _permit = sem.acquire_owned().await.map_err(|_| {
                    PackError::FeedUnavailable("archive semaphore closed".to_owned())
                })?;
                let zip_rel = format!(
                    "{prefix}/monthly/klines/{arch}/{iv_str}/{arch}-{iv_str}-{year:04}-{month:02}.zip"
                );
                let bars = connector
                    .fetch_verified_archive_bars(&zip_rel, &sym, &tf, interval, now)
                    .await?;
                Ok::<_, PackError>((year, month, bars))
            });
        }

        let mut collected: BTreeMap<i64, RawBar> = BTreeMap::new();
        let trailing_cutoff_date = prev_month(prev_month(end_date));
        let trailing_cutoff = (trailing_cutoff_date.year(), trailing_cutoff_date.month());
        let mut missing_months = Vec::new();
        while let Some(res) = monthly_set.join_next().await {
            let (year, month, bars_opt) = res.map_err(|e| {
                PackError::FeedUnavailable(format!("archive monthly task failed: {e}"))
            })??;
            match bars_opt {
                Some(bars) => {
                    for bar in bars {
                        if start_ts <= bar.ts_event && bar.ts_event <= end_ts {
                            collected.insert(bar.ts_event, bar);
                        }
                    }
                }
                None if (year, month) >= trailing_cutoff => {
                    missing_months.push((year, month));
                }
                None => {}
            }
        }

        if !missing_months.is_empty() {
            let mut daily_set = JoinSet::new();
            for (year, month) in missing_months {
                for day in days_in_month_overlapping(year, month, start_date, end_date) {
                    let connector = self.clone();
                    let sem = Arc::clone(&sem);
                    let sym = symbol.to_owned();
                    let arch = arch_sym.to_owned();
                    let tf = timeframe.to_owned();
                    let iv_str = bn_iv.to_owned();
                    daily_set.spawn(async move {
                        let _permit = sem.acquire_owned().await.map_err(|_| {
                            PackError::FeedUnavailable("archive semaphore closed".to_owned())
                        })?;
                        let zip_rel = format!(
                            "{prefix}/daily/klines/{arch}/{iv_str}/{arch}-{iv_str}-{:04}-{:02}-{:02}.zip",
                            day.year(),
                            day.month(),
                            day.day()
                        );
                        connector
                            .fetch_verified_archive_bars(&zip_rel, &sym, &tf, interval, now)
                            .await
                    });
                }
            }

            while let Some(res) = daily_set.join_next().await {
                let bars_opt = res.map_err(|e| {
                    PackError::FeedUnavailable(format!("archive daily task failed: {e}"))
                })??;
                if let Some(bars) = bars_opt {
                    for bar in bars {
                        if start_ts <= bar.ts_event && bar.ts_event <= end_ts {
                            collected.insert(bar.ts_event, bar);
                        }
                    }
                }
            }
        }

        Ok(collected.into_values().collect())
    }
}

/// Verify `<hex-sha256>  <filename>` checksum bytes against `zip_bytes`.
pub fn verify_sha256_checksum(
    zip_bytes: &[u8],
    checksum_bytes: &[u8],
    context: &str,
) -> Result<(), PackError> {
    let checksum_text = std::str::from_utf8(checksum_bytes).map_err(|_| {
        PackError::FeedUnavailable(format!("checksum for `{context}` is not valid UTF-8"))
    })?;
    let expected = checksum_text
        .split_whitespace()
        .next()
        .unwrap_or("")
        .to_ascii_lowercase();
    if expected.len() != 64 || !expected.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(PackError::FeedUnavailable(format!(
            "malformed SHA-256 checksum for `{context}`"
        )));
    }
    let actual = hex::encode(Sha256::digest(zip_bytes));
    if actual != expected {
        return Err(PackError::FeedUnavailable(format!(
            "SHA-256 checksum mismatch for `{context}`: expected {expected}, got {actual}"
        )));
    }
    Ok(())
}

/// Extract the first CSV entry from a Binance public archive `.zip` buffer and parse 12-column rows.
pub fn decode_klines_zip_bytes(
    zip_bytes: &[u8],
    symbol: &str,
    timeframe: &str,
    interval: i64,
    now_ms: i64,
) -> Result<Vec<RawBar>, PackError> {
    let cursor = Cursor::new(zip_bytes);
    let mut archive = ZipArchive::new(cursor).map_err(|err| {
        PackError::FeedUnavailable(format!("corrupt Binance archive ZIP: {err}"))
    })?;
    if archive.is_empty() {
        return Ok(Vec::new());
    }
    let mut file = archive.by_index(0).map_err(|err| {
        PackError::FeedUnavailable(format!("cannot read entry in Binance archive ZIP: {err}"))
    })?;
    let mut csv_text = String::new();
    file.read_to_string(&mut csv_text).map_err(|err| {
        PackError::FeedUnavailable(format!("invalid UTF-8 CSV inside Binance archive ZIP: {err}"))
    })?;

    let (price_u, qty_u) = precision_for_binance_symbol(symbol);
    let mut bars = Vec::new();
    for line in csv_text.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() {
            continue;
        }
        let cols: Vec<&str> = trimmed.split(',').collect();
        if cols.len() < 8 {
            return Err(PackError::FeedUnavailable(format!(
                "Binance CSV row has {} columns (expected >= 8): `{trimmed}`",
                cols.len()
            )));
        }
        // Binance archives since 2022 include a header row `open_time,open,high,...`.
        // Spot archives starting in 2025-01 switched timestamps from 13-digit ms to 16-digit us.
        let Ok(raw_ts) = cols[0].trim().parse::<i64>() else {
            continue;
        };
        let ts_event = if raw_ts >= 1_000_000_000_000_000 {
            raw_ts / 1_000
        } else {
            raw_ts
        };
        let open = quantize_dp(
            parse_decimal_str(cols[1].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad open `{}`: {e}", cols[1])))?,
            price_u,
        );
        let high = quantize_dp(
            parse_decimal_str(cols[2].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad high `{}`: {e}", cols[2])))?,
            price_u,
        );
        let low = quantize_dp(
            parse_decimal_str(cols[3].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad low `{}`: {e}", cols[3])))?,
            price_u,
        );
        let close = quantize_dp(
            parse_decimal_str(cols[4].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad close `{}`: {e}", cols[4])))?,
            price_u,
        );
        let volume = quantize_dp(
            parse_decimal_str(cols[5].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad volume `{}`: {e}", cols[5])))?,
            qty_u,
        );
        let turnover = quantize_dp(
            parse_decimal_str(cols[7].trim())
                .map_err(|e| PackError::FeedUnavailable(format!("bad turnover `{}`: {e}", cols[7])))?,
            price_u + qty_u,
        );
        let _ = Value::Null;
        bars.push(RawBar {
            symbol: symbol.to_owned(),
            timeframe: timeframe.to_owned(),
            ts_event,
            open,
            high,
            low,
            close,
            volume,
            turnover,
            ts_available: ts_event + interval,
            ts_ingested: now_ms,
            ts_committed: now_ms,
            precision: PrecisionTier::SecondVerified,
            revision: 1,
            confirmed: true,
            history_only: false,
            synthetic: false,
        });
    }
    Ok(bars)
}

fn date_from_ms(ts_ms: i64) -> NaiveDate {
    DateTime::<Utc>::from_timestamp_millis(ts_ms)
        .unwrap_or_else(Utc::now)
        .date_naive()
}

fn prev_month(date: NaiveDate) -> NaiveDate {
    let first = NaiveDate::from_ymd_opt(date.year(), date.month(), 1).unwrap_or(date);
    first - ChronoDuration::days(1)
}

fn months_overlapping(start_date: NaiveDate, end_date: NaiveDate) -> Vec<(i32, u32)> {
    let mut months = Vec::new();
    let mut year = start_date.year();
    let mut month = start_date.month();
    let end_ym = (end_date.year(), end_date.month());
    while (year, month) <= end_ym {
        months.push((year, month));
        if month == 12 {
            year += 1;
            month = 1;
        } else {
            month += 1;
        }
    }
    months
}

fn days_in_month_overlapping(
    year: i32,
    month: u32,
    start_date: NaiveDate,
    end_date: NaiveDate,
) -> Vec<NaiveDate> {
    let mut days = Vec::new();
    let mut d = NaiveDate::from_ymd_opt(year, month, 1).expect("valid month start");
    while d.year() == year && d.month() == month {
        if d >= start_date && d <= end_date {
            days.push(d);
        }
        d += ChronoDuration::days(1);
    }
    days
}

impl HistoryConnector for BinanceVisionArchiveConnector {
    fn feed_mode(&self) -> &str {
        "binance_archive"
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
            let bn_iv = rest_interval_for(timeframe)?;
            let now = (self.now_ms)();
            let effective_end = end_ts.unwrap_or(now);
            let mut collected: BTreeMap<i64, RawBar> = BTreeMap::new();

            let end_date = date_from_ms(effective_end);
            let current_month_key = (end_date.year(), end_date.month());

            // Try daily files first within the active/recent month (bounded to 6 concurrent downloads).
            let mut days_desc = Vec::new();
            let mut day_cursor = end_date;
            for _ in 0..31 {
                if (day_cursor.year(), day_cursor.month()) != current_month_key {
                    break;
                }
                days_desc.push(day_cursor);
                day_cursor -= ChronoDuration::days(1);
            }
            let prefix = self.dataset.path_prefix();
            let arch_sym = self.dataset.archive_symbol(symbol);
            let sem = Arc::new(Semaphore::new(6));
            for chunk in days_desc.chunks(6) {
                if collected.len() >= limit {
                    break;
                }
                let mut set = JoinSet::new();
                for &day in chunk {
                    let connector = self.clone();
                    let sem = Arc::clone(&sem);
                    let sym = symbol.to_owned();
                    let arch = arch_sym.to_owned();
                    let tf = timeframe.to_owned();
                    let iv_str = bn_iv.to_owned();
                    set.spawn(async move {
                        let _permit = sem.acquire_owned().await.map_err(|_| {
                            PackError::FeedUnavailable("archive semaphore closed".to_owned())
                        })?;
                        let zip_rel = format!(
                            "{prefix}/daily/klines/{arch}/{iv_str}/{arch}-{iv_str}-{:04}-{:02}-{:02}.zip",
                            day.year(),
                            day.month(),
                            day.day()
                        );
                        connector
                            .fetch_verified_archive_bars(&zip_rel, &sym, &tf, interval, now)
                            .await
                    });
                }
                while let Some(res) = set.join_next().await {
                    let bars_opt = res.map_err(|e| {
                        PackError::FeedUnavailable(format!("archive daily task failed: {e}"))
                    })??;
                    if let Some(bars) = bars_opt {
                        for bar in bars {
                            if bar.ts_event <= effective_end {
                                collected.insert(bar.ts_event, bar);
                            }
                        }
                    }
                }
            }

            // Walk backward month by month via monthly archives until `limit` is satisfied (max 18 months).
            let mut month_cursor = if collected.is_empty() {
                end_date
            } else {
                prev_month(end_date)
            };
            for _ in 0..18 {
                if collected.len() >= limit {
                    break;
                }
                let zip_rel = format!(
                    "{prefix}/monthly/klines/{arch_sym}/{bn_iv}/{arch_sym}-{bn_iv}-{:04}-{:02}.zip",
                    month_cursor.year(),
                    month_cursor.month()
                );
                if let Some(bars) = self
                    .fetch_verified_archive_bars(&zip_rel, symbol, timeframe, interval, now)
                    .await?
                {
                    for bar in bars {
                        if bar.ts_event <= effective_end {
                            collected.insert(bar.ts_event, bar);
                        }
                    }
                }
                month_cursor = prev_month(month_cursor);
            }

            let mut out: Vec<RawBar> = collected.into_values().collect();
            if out.len() > limit {
                out.drain(..out.len() - limit);
            }
            Ok(out)
        })
    }
}
