//! `decimal128(18, s)` fixed-point conversion, column scale calculation, and batch quality checks.

use std::cmp::Ordering;
use std::collections::BTreeMap;

use rust_decimal::Decimal;
use talaria_contracts::{
    DerivativeKind, DerivativePoint, InstrumentSpec, Quote, RawBar, TickKind, Trade,
    TradeAvailability,
};

use crate::error::{Result, StoreError};
use crate::types::{COLD_DECIMAL_PRECISION, RATE_COLUMN_SCALE, partition_day, partition_month, safe_path_part};

const MAX_18_DIGITS: i128 = 999_999_999_999_999_999;

pub const BAR_PRICE_COLUMNS: &[&str] = &["open", "high", "low", "close"];
pub const BAR_QUANTITY_COLUMNS: &[&str] = &["volume"];
pub const BAR_TURNOVER_COLUMNS: &[&str] = &["turnover"];
pub const BAR_DECIMAL_COLUMNS: &[&str] = &["open", "high", "low", "close", "volume", "turnover"];

/// Convert a `Decimal` into a scaled `i128` strictly fitting `decimal128(18, scale)`.
///
/// Rejects values with non-zero digits beyond `scale` or more than 18 significant decimal digits.
pub fn decimal_to_i128(value: Decimal, scale: i8, column: &str) -> Result<i128> {
    if scale < 0 || scale > COLD_DECIMAL_PRECISION as i8 {
        return Err(StoreError::Quality(format!(
            "invalid scale {scale} for column {column}"
        )));
    }
    let target_scale = scale as u32;
    let normalized = value.normalize();
    if normalized.scale() > target_scale {
        return Err(StoreError::Quality(format!(
            "column {column} value {value} exceeds scale={target_scale}"
        )));
    }
    let shift = target_scale - normalized.scale();
    let multiplier = 10_i128
        .checked_pow(shift)
        .ok_or_else(|| StoreError::Quality(format!("column {column} scale shift overflow")))?;
    let scaled = normalized.mantissa().checked_mul(multiplier).ok_or_else(|| {
        StoreError::Quality(format!(
            "column {column} value {value} exceeds decimal128({COLD_DECIMAL_PRECISION}, {target_scale})"
        ))
    })?;
    if scaled.abs() > MAX_18_DIGITS {
        return Err(StoreError::Quality(format!(
            "column {column} value {value} exceeds {COLD_DECIMAL_PRECISION} digits in decimal128({COLD_DECIMAL_PRECISION}, {target_scale})"
        )));
    }
    Ok(scaled)
}

/// Reconstruct a `Decimal` from a raw Parquet `Decimal128(18, scale)` integer value.
pub fn i128_to_decimal(raw: i128, scale: i8, column: &str) -> Result<Decimal> {
    if scale < 0 || scale > COLD_DECIMAL_PRECISION as i8 {
        return Err(StoreError::Integrity(format!(
            "invalid scale {scale} for column {column}"
        )));
    }
    if raw.abs() > MAX_18_DIGITS {
        return Err(StoreError::Integrity(format!(
            "column {column} value {raw} exceeds {COLD_DECIMAL_PRECISION} digits"
        )));
    }
    Decimal::try_from_i128_with_scale(raw, scale as u32).map_err(|err| {
        StoreError::Integrity(format!(
            "failed to reconstruct Decimal for {column} ({raw}, scale={scale}): {err}"
        ))
    })
}

/// Per-column `decimal128(18, s)` scales for OHLCV + turnover bar files (ADR-0017).
pub fn cold_column_scales(instrument: &InstrumentSpec) -> Result<BTreeMap<String, i8>> {
    let price = instrument.price_precision;
    let qty = instrument.qty_precision;
    if price < 0 || qty < 0 {
        return Err(StoreError::Quality(
            "instrument price_precision and qty_precision cannot be negative".to_owned(),
        ));
    }
    if price + qty > i32::from(COLD_DECIMAL_PRECISION) {
        return Err(StoreError::Quality(format!(
            "price_precision + qty_precision exceeds decimal128({COLD_DECIMAL_PRECISION}, s): {price} + {qty}"
        )));
    }
    let price_scale = price as i8;
    let qty_scale = qty as i8;
    let turnover_scale = (price + qty) as i8;
    let mut scales = BTreeMap::new();
    for &col in BAR_PRICE_COLUMNS {
        scales.insert(col.to_owned(), price_scale);
    }
    for &col in BAR_QUANTITY_COLUMNS {
        scales.insert(col.to_owned(), qty_scale);
    }
    for &col in BAR_TURNOVER_COLUMNS {
        scales.insert(col.to_owned(), turnover_scale);
    }
    Ok(scales)
}

/// Column scale for `DerivativePoint.value` based on `DerivativeKind` (ADR-0017).
pub fn derivative_scale(instrument: &InstrumentSpec, kind: DerivativeKind) -> Result<i8> {
    let scale = match kind {
        DerivativeKind::MarkPrice => instrument.price_precision,
        DerivativeKind::OpenInterest => instrument.qty_precision,
        DerivativeKind::FundingRate => i32::from(RATE_COLUMN_SCALE),
    };
    if scale < 0 || scale > i32::from(COLD_DECIMAL_PRECISION) {
        return Err(StoreError::Quality(format!(
            "invalid derivative scale {scale} for {kind:?}"
        )));
    }
    Ok(scale as i8)
}

/// Per-column `decimal128(18, s)` scales for tick (`trades` / `quotes`) files (ADR-0017).
pub fn tick_column_scales(
    instrument: &InstrumentSpec,
    kind: TickKind,
) -> Result<BTreeMap<String, i8>> {
    let price = instrument.price_precision;
    let qty = instrument.qty_precision;
    if price < 0
        || price > i32::from(COLD_DECIMAL_PRECISION)
        || qty < 0
        || qty > i32::from(COLD_DECIMAL_PRECISION)
    {
        return Err(StoreError::Quality(format!(
            "invalid tick precision: price={price}, qty={qty}"
        )));
    }
    let price_scale = price as i8;
    let qty_scale = qty as i8;
    let mut scales = BTreeMap::new();
    match kind {
        TickKind::Trades => {
            scales.insert("price".to_owned(), price_scale);
            scales.insert("size".to_owned(), qty_scale);
        }
        TickKind::Quotes => {
            scales.insert("bid_price".to_owned(), price_scale);
            scales.insert("bid_size".to_owned(), qty_scale);
            scales.insert("ask_price".to_owned(), price_scale);
            scales.insert("ask_size".to_owned(), qty_scale);
        }
    }
    Ok(scales)
}

fn check_step_alignment(value: Decimal, step: Decimal, label: &str, where_ctx: &str) -> Result<()> {
    if step <= Decimal::ZERO {
        return Err(StoreError::Quality(format!(
            "instrument step for {label} must be positive: {step}"
        )));
    }
    if value % step != Decimal::ZERO {
        return Err(StoreError::Quality(format!(
            "{where_ctx} {label}={value} is not aligned to step={step}"
        )));
    }
    Ok(())
}

/// Validate a bar batch for completeness, single-month partition containment, monotonicity,
/// OHLC constraints, step alignment, and `decimal128(18, s)` range.
pub fn validate_bars(
    bars: &[RawBar],
    instrument: &InstrumentSpec,
    timeframe: &str,
    allow_equal_revisions: bool,
) -> Result<()> {
    safe_path_part(&instrument.venue, "venue")?;
    safe_path_part(&instrument.symbol, "symbol")?;
    safe_path_part(timeframe, "timeframe")?;
    if bars.is_empty() {
        return Err(StoreError::Quality("batch is empty, rejecting publish".to_owned()));
    }
    let scales = cold_column_scales(instrument)?;
    let expected_month = partition_month(bars[0].ts_event)?;
    let mut previous: Option<(i64, u64)> = None;

    for (index, bar) in bars.iter().enumerate() {
        let where_ctx = format!("bar[{index}]");
        if bar.symbol != instrument.symbol {
            return Err(StoreError::Quality(format!(
                "{where_ctx} symbol {:?} does not match instrument {:?}",
                bar.symbol, instrument.symbol
            )));
        }
        if bar.timeframe != timeframe {
            return Err(StoreError::Quality(format!(
                "{where_ctx} timeframe {:?} does not match batch {:?}",
                bar.timeframe, timeframe
            )));
        }
        if bar.ts_event < 0 {
            return Err(StoreError::Quality(format!(
                "{where_ctx} negative ts_event {}",
                bar.ts_event
            )));
        }
        if bar.revision > i64::MAX as u64 {
            return Err(StoreError::Quality(format!(
                "{where_ctx} revision {} overflows i64",
                bar.revision
            )));
        }
        let row_month = partition_month(bar.ts_event)?;
        if row_month != expected_month {
            return Err(StoreError::Quality(format!(
                "{where_ctx} crosses month boundary ({expected_month} -> {row_month})"
            )));
        }
        let current = (bar.ts_event, bar.revision);
        if let Some(prev) = previous
            && (current < prev || (current == prev && !allow_equal_revisions))
        {
            return Err(StoreError::Quality(format!(
                "{where_ctx} non-monotonic (ts_event, revision): {current:?} after {prev:?}"
            )));
        }
        for (col, val) in [
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
            ("volume", bar.volume),
            ("turnover", bar.turnover),
        ] {
            decimal_to_i128(val, scales[col], col)?;
        }
        if bar.high < bar.low {
            return Err(StoreError::Quality(format!("{where_ctx} high < low")));
        }
        if bar.high < bar.open.max(bar.close) {
            return Err(StoreError::Quality(format!(
                "{where_ctx} high < max(open, close)"
            )));
        }
        if bar.low > bar.open.min(bar.close) {
            return Err(StoreError::Quality(format!(
                "{where_ctx} low > min(open, close)"
            )));
        }
        if bar.volume < Decimal::ZERO || bar.turnover < Decimal::ZERO {
            return Err(StoreError::Quality(format!(
                "{where_ctx} negative volume or turnover"
            )));
        }
        for (label, price) in [
            ("open", bar.open),
            ("high", bar.high),
            ("low", bar.low),
            ("close", bar.close),
        ] {
            check_step_alignment(price, instrument.tick_size, label, &where_ctx)?;
        }
        check_step_alignment(bar.volume, instrument.qty_step, "volume", &where_ctx)?;
        if bar.ts_available < bar.ts_event {
            return Err(StoreError::Quality(format!(
                "{where_ctx} ts_available precedes ts_event"
            )));
        }
        if bar.ts_committed < bar.ts_ingested {
            return Err(StoreError::Quality(format!(
                "{where_ctx} ts_committed precedes ts_ingested"
            )));
        }
        previous = Some(current);
    }
    Ok(())
}

/// Canonical derivative ordering matching Python `_derivative_order`.
pub fn derivative_order_cmp(a: &DerivativePoint, b: &DerivativePoint) -> Ordering {
    (
        a.ts_event,
        &a.instrument_id,
        a.kind,
        a.timeframe.as_deref().unwrap_or(""),
        a.is_funding_settlement(),
        a.revision,
    )
        .cmp(&(
            b.ts_event,
            &b.instrument_id,
            b.kind,
            b.timeframe.as_deref().unwrap_or(""),
            b.is_funding_settlement(),
            b.revision,
        ))
}

/// Validate a derivative batch for single-month containment, monotonicity, temporal ordering, and scale.
pub fn validate_derivatives(
    rows: &[DerivativePoint],
    instrument: &InstrumentSpec,
    kind: DerivativeKind,
    allow_equal_revisions: bool,
) -> Result<()> {
    safe_path_part(&instrument.venue, "venue")?;
    safe_path_part(&instrument.symbol, "symbol")?;
    safe_path_part(kind.as_str(), "kind")?;
    if rows.is_empty() {
        return Err(StoreError::Quality(
            "derivative batch is empty, rejecting publish".to_owned(),
        ));
    }
    let scale = derivative_scale(instrument, kind)?;
    let expected_month = partition_month(rows[0].ts_event)?;

    for (index, point) in rows.iter().enumerate() {
        let where_ctx = format!("derivative[{index}]");
        if point.instrument_id != instrument.instrument_id || point.kind != kind {
            return Err(StoreError::Quality(format!(
                "{where_ctx} instrument_id or kind mismatch"
            )));
        }
        if point.ts_event < 0 || point.ts_available < point.ts_event || point.ts_committed < point.ts_ingested
        {
            return Err(StoreError::Quality(format!(
                "{where_ctx} invalid temporal ordering"
            )));
        }
        if point.revision > i64::MAX as u64 {
            return Err(StoreError::Quality(format!(
                "{where_ctx} revision {} overflows i64",
                point.revision
            )));
        }
        let row_month = partition_month(point.ts_event)?;
        if row_month != expected_month {
            return Err(StoreError::Quality(format!(
                "{where_ctx} crosses month boundary ({expected_month} -> {row_month})"
            )));
        }
        if index > 0 {
            let ord = derivative_order_cmp(point, &rows[index - 1]);
            if ord == Ordering::Less || (ord == Ordering::Equal && !allow_equal_revisions) {
                return Err(StoreError::Quality(format!(
                    "{where_ctx} non-monotonic derivative timestamp/revision"
                )));
            }
        }
        decimal_to_i128(point.value, scale, "value")?;
    }
    Ok(())
}

fn availability_str(avail: TradeAvailability) -> &'static str {
    match avail {
        TradeAvailability::Measured => "measured",
        TradeAvailability::Inferred => "inferred",
    }
}

/// Canonical `Trade` ordering matching Python `tick_order`.
pub fn trade_order_cmp(a: &Trade, b: &Trade) -> Ordering {
    let key = |t: &Trade| {
        (
            t.ts_event,
            availability_str(t.availability),
            t.archive_day.unwrap_or(-1),
            t.archive_index.unwrap_or(-1),
            t.sequence.unwrap_or(-1),
            *t.packet_id.unwrap_or(t.trade_id).as_bytes(),
            t.packet_index.unwrap_or(-1),
            *t.trade_id.as_bytes(),
            t.revision,
            t.ts_ingested,
        )
    };
    key(a).cmp(&key(b))
}

/// Canonical `Quote` ordering matching Python `tick_order`.
pub fn quote_order_cmp(a: &Quote, b: &Quote) -> Ordering {
    let a_conn = a
        .connection_id
        .as_ref()
        .map(|id| id.as_bytes().as_slice())
        .unwrap_or(&[][..]);
    let b_conn = b
        .connection_id
        .as_ref()
        .map(|id| id.as_bytes().as_slice())
        .unwrap_or(&[][..]);
    (
        a.ts_event,
        a_conn,
        a.update_id.unwrap_or(-1),
        a.revision,
        a.ts_ingested,
    )
        .cmp(&(
            b.ts_event,
            b_conn,
            b.update_id.unwrap_or(-1),
            b.revision,
            b.ts_ingested,
        ))
}

/// Validate a single `Trade` against `InstrumentSpec` and `tick_column_scales`.
pub fn validate_trade(
    row: &Trade,
    instrument: &InstrumentSpec,
    scales: &BTreeMap<String, i8>,
) -> Result<()> {
    if row.instrument_id != instrument.instrument_id {
        return Err(StoreError::Quality(format!(
            "trade instrument_id {:?} does not match {:?}",
            row.instrument_id, instrument.instrument_id
        )));
    }
    if row.ts_event < 0 || row.ts_available < row.ts_event || row.ts_committed < row.ts_ingested {
        return Err(StoreError::Quality("invalid trade timestamps".to_owned()));
    }
    if row.revision > i64::MAX as u64 {
        return Err(StoreError::Quality("trade revision overflows i64".to_owned()));
    }
    decimal_to_i128(row.price, scales["price"], "price")?;
    decimal_to_i128(row.size, scales["size"], "size")?;
    if row.price <= Decimal::ZERO {
        return Err(StoreError::Quality(format!("invalid trade price {}", row.price)));
    }
    if row.size <= Decimal::ZERO {
        return Err(StoreError::Quality(format!("invalid trade size {}", row.size)));
    }
    check_step_alignment(row.price, instrument.tick_size, "price", "trade")?;
    check_step_alignment(row.size, instrument.qty_step, "size", "trade")?;
    Ok(())
}

/// Validate a single `Quote` against `InstrumentSpec` and `tick_column_scales`.
pub fn validate_quote(
    row: &Quote,
    instrument: &InstrumentSpec,
    scales: &BTreeMap<String, i8>,
) -> Result<()> {
    if row.instrument_id != instrument.instrument_id {
        return Err(StoreError::Quality(format!(
            "quote instrument_id {:?} does not match {:?}",
            row.instrument_id, instrument.instrument_id
        )));
    }
    if row.ts_event < 0 || row.ts_available < row.ts_event || row.ts_committed < row.ts_ingested {
        return Err(StoreError::Quality("invalid quote timestamps".to_owned()));
    }
    if row.revision > i64::MAX as u64 {
        return Err(StoreError::Quality("quote revision overflows i64".to_owned()));
    }
    for (col, val, is_price) in [
        ("bid_price", row.bid_price, true),
        ("bid_size", row.bid_size, false),
        ("ask_price", row.ask_price, true),
        ("ask_size", row.ask_size, false),
    ] {
        decimal_to_i128(val, scales[col], col)?;
        if val < Decimal::ZERO || (val == Decimal::ZERO && is_price) {
            return Err(StoreError::Quality(format!("invalid quote {col}: {val}")));
        }
    }
    if row.bid_price > row.ask_price {
        return Err(StoreError::Quality(format!(
            "quote bid_price {} exceeds ask_price {}",
            row.bid_price, row.ask_price
        )));
    }
    Ok(())
}

/// Validate a sorted same-UTC-day batch of `Trade` records.
pub fn validate_trades(rows: &[Trade], instrument: &InstrumentSpec) -> Result<()> {
    safe_path_part(&instrument.venue, "venue")?;
    safe_path_part(&instrument.symbol, "symbol")?;
    if rows.is_empty() {
        return Err(StoreError::Quality("trade batch is empty".to_owned()));
    }
    let scales = tick_column_scales(instrument, TickKind::Trades)?;
    let expected_day = partition_day(rows[0].ts_event)?;
    for (idx, row) in rows.iter().enumerate() {
        validate_trade(row, instrument, &scales)?;
        let day = partition_day(row.ts_event)?;
        if day != expected_day {
            return Err(StoreError::Quality(format!(
                "trade[{idx}] crosses UTC day boundary ({expected_day} -> {day})"
            )));
        }
        if idx > 0 && trade_order_cmp(row, &rows[idx - 1]) == Ordering::Less {
            return Err(StoreError::Quality(format!(
                "trade[{idx}] is not ordered by tick_order"
            )));
        }
    }
    Ok(())
}

/// Validate a sorted same-UTC-day batch of `Quote` records.
pub fn validate_quotes(rows: &[Quote], instrument: &InstrumentSpec) -> Result<()> {
    safe_path_part(&instrument.venue, "venue")?;
    safe_path_part(&instrument.symbol, "symbol")?;
    if rows.is_empty() {
        return Err(StoreError::Quality("quote batch is empty".to_owned()));
    }
    let scales = tick_column_scales(instrument, TickKind::Quotes)?;
    let expected_day = partition_day(rows[0].ts_event)?;
    for (idx, row) in rows.iter().enumerate() {
        validate_quote(row, instrument, &scales)?;
        let day = partition_day(row.ts_event)?;
        if day != expected_day {
            return Err(StoreError::Quality(format!(
                "quote[{idx}] crosses UTC day boundary ({expected_day} -> {day})"
            )));
        }
        if idx > 0 && quote_order_cmp(row, &rows[idx - 1]) == Ordering::Less {
            return Err(StoreError::Quality(format!(
                "quote[{idx}] is not ordered by tick_order"
            )));
        }
    }
    Ok(())
}
