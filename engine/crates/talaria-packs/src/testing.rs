//! Offline deterministic fixture connector reading `tests/fixtures/bybit/` layouts.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::Value;
use talaria_contracts::{RawBar, RawDerivativePoint, timeframe_ms};

use crate::crypto_perp::connector::{
    FUNDING_LIMIT, MAX_LIMIT, OI_LIMIT, parse_bybit_envelope_list, parse_funding_row,
    parse_kline_row, parse_mark_price_row, parse_open_interest_row,
};
use crate::error::PackError;
use crate::traits::{BoxFuture, HistoryConnector};

/// Offline deterministic [`HistoryConnector`] reading Bybit V5 envelope JSON fixtures.
#[derive(Debug, Clone)]
pub struct FixtureConnector {
    fixture_dir: PathBuf,
}

impl FixtureConnector {
    /// Construct a connector reading from `fixture_dir`.
    pub fn new(fixture_dir: impl Into<PathBuf>) -> Self {
        Self {
            fixture_dir: fixture_dir.into(),
        }
    }

    pub fn fixture_dir(&self) -> &Path {
        &self.fixture_dir
    }

    fn read_list(&self, path: &Path) -> Result<Vec<Value>, PackError> {
        if !path.is_file() {
            return Err(PackError::FeedUnavailable(format!(
                "missing offline fixture file `{}`",
                path.display()
            )));
        }
        let text = fs::read_to_string(path).map_err(|err| {
            PackError::FeedUnavailable(format!("cannot read fixture `{}`: {err}", path.display()))
        })?;
        let payload: Value = serde_json::from_str(&text).map_err(|err| {
            PackError::FeedUnavailable(format!("invalid JSON in fixture `{}`: {err}", path.display()))
        })?;
        Ok(parse_bybit_envelope_list(&payload)?.clone())
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
        let capped = limit.min(max_limit);
        let rows = self.read_list(path)?;
        let mut selected: Vec<(i64, T)> = Vec::with_capacity(rows.len());
        for row in &rows {
            let (ts_event, item) = parse_row(row)?;
            if end_ts.is_some_and(|end| ts_event > end) {
                continue;
            }
            selected.push((ts_event, item));
        }
        selected.sort_by_key(|(ts, _)| *ts);
        if selected.len() > capped {
            let drop_count = selected.len() - capped;
            selected.drain(..drop_count);
        }
        Ok(selected.into_iter().map(|(_, item)| item).collect())
    }

    /// Synchronous helper matching Python `FixtureConnector.fetch_bars`.
    pub fn fetch_bars_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawBar>, PackError> {
        let interval = timeframe_ms(timeframe)?;
        let path = self.fixture_dir.join(symbol).join(format!("{timeframe}.json"));
        self.select_latest_ascending(&path, end_ts, limit, MAX_LIMIT, |row| {
            let bar = parse_kline_row(row, symbol, timeframe, interval, None, None)?;
            Ok((bar.ts_event, bar))
        })
    }

    /// Synchronous helper matching Python `FixtureConnector.fetch_mark_price_bars`.
    pub fn fetch_mark_price_bars_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawDerivativePoint>, PackError> {
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

    /// Synchronous helper matching Python `FixtureConnector.fetch_funding_settlements`.
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

    /// Synchronous helper matching Python `FixtureConnector.fetch_open_interest`.
    pub fn fetch_open_interest_sync(
        &self,
        symbol: &str,
        timeframe: &str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> Result<Vec<RawDerivativePoint>, PackError> {
        let interval = timeframe_ms(timeframe)?;
        let path = self
            .fixture_dir
            .join(symbol)
            .join("derivatives")
            .join(format!("open_interest_{timeframe}.json"));
        self.select_latest_ascending(&path, end_ts, limit, OI_LIMIT, |row| {
            let pt = parse_open_interest_row(row, symbol, timeframe, interval, None, None)?;
            Ok((pt.ts_event, pt))
        })
    }
}

impl HistoryConnector for FixtureConnector {
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
        Box::pin(async move {
            self.fetch_mark_price_bars_sync(symbol, timeframe, end_ts, limit)
        })
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
