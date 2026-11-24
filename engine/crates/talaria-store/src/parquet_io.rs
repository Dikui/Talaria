//! Parquet `decimal128(18, s)` writers and readers for Bars, Derivatives, and Ticks/Quotes.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::fs::File;
use std::path::Path;
use std::sync::Arc;

use arrow::array::{
    Array, ArrayAccessor, ArrayRef, BooleanArray, BooleanBuilder, Decimal128Array,
    Decimal128Builder, DictionaryArray, FixedSizeBinaryArray, FixedSizeBinaryBuilder, Int64Array,
    Int64Builder, StringArray, StringBuilder, StringDictionaryBuilder,
};
use arrow::datatypes::{DataType, Field, Int8Type, Metadata, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::ArrowWriter;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::basic::Compression;
use parquet::file::metadata::KeyValue;
use parquet::file::properties::WriterProperties;
use talaria_contracts::{
    DerivativeKind, DerivativePoint, InstrumentSpec, PrecisionTier, Quote, RawBar, TickKind, Trade,
    TradeAvailability, TradeSide,
};

use crate::decimal_codec::{
    BAR_DECIMAL_COLUMNS, cold_column_scales, decimal_to_i128, derivative_scale, i128_to_decimal,
    tick_column_scales,
};
use crate::error::{Result, StoreError};
use crate::types::{
    COLD_COLUMN_SCALES_METADATA, COLD_DECIMAL_PRECISION, COLD_FORMAT_DECIMAL128,
    COLD_FORMAT_METADATA, COLD_INSTRUMENT_VERSION_METADATA,
};

fn build_cold_metadata(
    scales: &BTreeMap<String, i8>,
    instrument: &InstrumentSpec,
) -> Result<HashMap<String, String>> {
    let scales_json = serde_json::to_string(scales)?;
    let mut metadata = HashMap::new();
    metadata.insert(
        COLD_FORMAT_METADATA.to_owned(),
        COLD_FORMAT_DECIMAL128.to_owned(),
    );
    metadata.insert(COLD_COLUMN_SCALES_METADATA.to_owned(), scales_json);
    metadata.insert(
        COLD_INSTRUMENT_VERSION_METADATA.to_owned(),
        instrument.instrument_spec_sha256(),
    );
    Ok(metadata)
}

fn writer_properties() -> WriterProperties {
    WriterProperties::builder()
        .set_compression(Compression::ZSTD(Default::default()))
        .build()
}

fn write_record_batch_parquet(path: &Path, batch: &RecordBatch) -> Result<()> {
    let file = File::create(path)?;
    let mut writer = ArrowWriter::try_new(file, batch.schema(), Some(writer_properties()))?;
    writer.write(batch)?;
    let schema = batch.schema();
    let mut entries: Vec<(&String, &String)> = schema.metadata().iter().collect();
    entries.sort_by_key(|(a, _)| *a);
    for (k, v) in entries {
        writer.append_key_value_metadata(KeyValue::new(k.clone(), v.clone()));
    }
    writer.close()?;
    Ok(())
}

fn parse_precision_tier(raw: &str) -> Result<PrecisionTier> {
    match raw {
        "SECOND_VERIFIED" => Ok(PrecisionTier::SecondVerified),
        "SESSION_BOUND" => Ok(PrecisionTier::SessionBound),
        "DATE_CONSERVATIVE" => Ok(PrecisionTier::DateConservative),
        other => Err(StoreError::Integrity(format!(
            "unknown precision tier {other:?}"
        ))),
    }
}

fn parse_column_scales_metadata(
    metadata: &Metadata,
    expected_columns: &[&str],
) -> Result<BTreeMap<String, i8>> {
    let format_marker = metadata
        .get(COLD_FORMAT_METADATA)
        .ok_or_else(|| StoreError::Integrity("parquet missing talaria.cold.format".to_owned()))?;
    if format_marker != COLD_FORMAT_DECIMAL128 {
        return Err(StoreError::Integrity(format!(
            "unknown cold numeric format: {format_marker:?}"
        )));
    }
    let raw_scales = metadata.get(COLD_COLUMN_SCALES_METADATA).ok_or_else(|| {
        StoreError::Integrity("parquet missing talaria.cold.column_scales".to_owned())
    })?;
    let parsed: serde_json::Value = serde_json::from_str(raw_scales).map_err(|err| {
        StoreError::Integrity(format!("invalid talaria.cold.column_scales JSON: {err}"))
    })?;
    let obj = parsed.as_object().ok_or_else(|| {
        StoreError::Integrity("talaria.cold.column_scales must be a JSON object".to_owned())
    })?;

    let mut scales = BTreeMap::new();
    for &col in expected_columns {
        let val = obj
            .get(col)
            .and_then(|v| v.as_i64())
            .ok_or_else(|| StoreError::Integrity(format!("missing or non-int scale for {col}")))?;
        if !(0..=i64::from(COLD_DECIMAL_PRECISION)).contains(&val) {
            return Err(StoreError::Integrity(format!(
                "invalid decimal128 scale {val} for column {col}"
            )));
        }
        scales.insert(col.to_owned(), val as i8);
    }
    Ok(scales)
}

fn verify_decimal_schema(schema: &Schema, scales: &BTreeMap<String, i8>) -> Result<()> {
    for (name, &scale) in scales {
        let field = schema
            .field_with_name(name)
            .map_err(|_| StoreError::Integrity(format!("decimal128 file missing column {name}")))?;
        let expected = DataType::Decimal128(COLD_DECIMAL_PRECISION, scale);
        if field.data_type() != &expected {
            return Err(StoreError::Integrity(format!(
                "decimal128 column {name} type {:?} != expected {expected:?}",
                field.data_type()
            )));
        }
    }
    let rev_field = schema
        .field_with_name("revision")
        .map_err(|_| StoreError::Integrity("decimal128 file missing revision column".to_owned()))?;
    if rev_field.data_type() != &DataType::Int64 {
        return Err(StoreError::Integrity(format!(
            "revision column must be Int64, got {:?}",
            rev_field.data_type()
        )));
    }
    Ok(())
}

fn col_str<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a StringArray> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not Utf8")))
}

fn col_i64<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Int64Array> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<Int64Array>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not Int64")))
}

fn col_bool<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a BooleanArray> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<BooleanArray>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not Boolean")))
}

fn col_decimal<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a Decimal128Array> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    batch
        .column(idx)
        .as_any()
        .downcast_ref::<Decimal128Array>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not Decimal128")))
}

fn col_precision_dict<'a>(
    batch: &'a RecordBatch,
    name: &str,
) -> Result<arrow::array::TypedDictionaryArray<'a, Int8Type, StringArray>> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    let dict = batch
        .column(idx)
        .as_any()
        .downcast_ref::<DictionaryArray<Int8Type>>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not Dictionary<Int8, Utf8>")))?;
    dict.downcast_dict::<StringArray>().ok_or_else(|| {
        StoreError::Integrity(format!("column {name} dictionary values are not Utf8"))
    })
}

fn col_fixed_binary<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a FixedSizeBinaryArray> {
    let idx = batch
        .schema()
        .index_of(name)
        .map_err(|_| StoreError::Integrity(format!("missing column {name}")))?;
    let arr = batch
        .column(idx)
        .as_any()
        .downcast_ref::<FixedSizeBinaryArray>()
        .ok_or_else(|| StoreError::Integrity(format!("column {name} is not FixedSizeBinary")))?;
    if arr.value_length() != 16 {
        return Err(StoreError::Integrity(format!(
            "column {name} FixedSizeBinary length {} != 16",
            arr.value_length()
        )));
    }
    Ok(arr)
}

fn fixed_bytes_16(slice: &[u8], col: &str) -> Result<[u8; 16]> {
    slice
        .try_into()
        .map_err(|_| StoreError::Integrity(format!("column {col} value length != 16 bytes")))
}

fn uuid_from_bytes_16<U: std::str::FromStr>(bytes: [u8; 16], col: &str) -> Result<U> {
    hex::encode(bytes)
        .parse::<U>()
        .map_err(|_| StoreError::Integrity(format!("invalid 16-byte UUID in column {col}")))
}

// =========================================================================
// Bars
// =========================================================================

/// Build the Arrow schema for cold bar Parquet files matching Python `_cold_schema`.
pub fn bars_schema(
    scales: &BTreeMap<String, i8>,
    metadata: HashMap<String, String>,
) -> Schema {
    Schema::new_with_metadata(
        vec![
            Field::new("symbol", DataType::Utf8, false),
            Field::new("timeframe", DataType::Utf8, false),
            Field::new("ts_event", DataType::Int64, false),
            Field::new(
                "open",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["open"]),
                false,
            ),
            Field::new(
                "high",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["high"]),
                false,
            ),
            Field::new(
                "low",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["low"]),
                false,
            ),
            Field::new(
                "close",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["close"]),
                false,
            ),
            Field::new(
                "volume",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["volume"]),
                false,
            ),
            Field::new(
                "turnover",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["turnover"]),
                false,
            ),
            Field::new("ts_available", DataType::Int64, false),
            Field::new("ts_ingested", DataType::Int64, false),
            Field::new("ts_committed", DataType::Int64, false),
            Field::new(
                "precision",
                DataType::Dictionary(Box::new(DataType::Int8), Box::new(DataType::Utf8)),
                false,
            ),
            Field::new("revision", DataType::Int64, false),
        ],
        metadata,
    )
}

/// Convert `RawBar` rows to an Arrow `RecordBatch` with `decimal128(18, s)` columns and metadata.
pub fn bars_to_record_batch(bars: &[RawBar], instrument: &InstrumentSpec) -> Result<RecordBatch> {
    let scales = cold_column_scales(instrument)?;
    let metadata = build_cold_metadata(&scales, instrument)?;
    let schema = Arc::new(bars_schema(&scales, metadata));

    let n = bars.len();
    let mut symbol_b = StringBuilder::with_capacity(n, n * 12);
    let mut tf_b = StringBuilder::with_capacity(n, n * 4);
    let mut ts_event_b = Int64Builder::with_capacity(n);
    let mut open_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["open"]));
    let mut high_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["high"]));
    let mut low_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["low"]));
    let mut close_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["close"]));
    let mut vol_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["volume"]));
    let mut turn_b = Decimal128Builder::with_capacity(n).with_data_type(DataType::Decimal128(
        COLD_DECIMAL_PRECISION,
        scales["turnover"],
    ));
    let mut ts_avail_b = Int64Builder::with_capacity(n);
    let mut ts_ing_b = Int64Builder::with_capacity(n);
    let mut ts_comm_b = Int64Builder::with_capacity(n);
    let mut prec_b = StringDictionaryBuilder::<Int8Type>::new();
    let mut rev_b = Int64Builder::with_capacity(n);

    for bar in bars {
        symbol_b.append_value(&bar.symbol);
        tf_b.append_value(&bar.timeframe);
        ts_event_b.append_value(bar.ts_event);
        open_b.append_value(decimal_to_i128(bar.open, scales["open"], "open")?);
        high_b.append_value(decimal_to_i128(bar.high, scales["high"], "high")?);
        low_b.append_value(decimal_to_i128(bar.low, scales["low"], "low")?);
        close_b.append_value(decimal_to_i128(bar.close, scales["close"], "close")?);
        vol_b.append_value(decimal_to_i128(bar.volume, scales["volume"], "volume")?);
        turn_b.append_value(decimal_to_i128(
            bar.turnover,
            scales["turnover"],
            "turnover",
        )?);
        ts_avail_b.append_value(bar.ts_available);
        ts_ing_b.append_value(bar.ts_ingested);
        ts_comm_b.append_value(bar.ts_committed);
        prec_b.append_value(bar.precision.as_str());
        let rev = i64::try_from(bar.revision)
            .map_err(|_| StoreError::Quality(format!("revision {} overflows i64", bar.revision)))?;
        rev_b.append_value(rev);
    }

    let columns: Vec<ArrayRef> = vec![
        Arc::new(symbol_b.finish()),
        Arc::new(tf_b.finish()),
        Arc::new(ts_event_b.finish()),
        Arc::new(open_b.finish()),
        Arc::new(high_b.finish()),
        Arc::new(low_b.finish()),
        Arc::new(close_b.finish()),
        Arc::new(vol_b.finish()),
        Arc::new(turn_b.finish()),
        Arc::new(ts_avail_b.finish()),
        Arc::new(ts_ing_b.finish()),
        Arc::new(ts_comm_b.finish()),
        Arc::new(prec_b.finish()),
        Arc::new(rev_b.finish()),
    ];
    Ok(RecordBatch::try_new(schema, columns)?)
}

/// Write a `RawBar` slice to Parquet with `decimal128(18, s)` + ZSTD + `talaria.cold.*` metadata.
pub fn write_bars_parquet(
    path: &Path,
    bars: &[RawBar],
    instrument: &InstrumentSpec,
) -> Result<()> {
    let batch = bars_to_record_batch(bars, instrument)?;
    write_record_batch_parquet(path, &batch)
}

/// Read `RawBar` rows from a cold Parquet file, verifying `talaria.cold.*` metadata and schema.
pub fn read_bars_parquet(
    path: &Path,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
) -> Result<Vec<RawBar>> {
    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let scales = parse_column_scales_metadata(schema.metadata(), BAR_DECIMAL_COLUMNS)?;
    verify_decimal_schema(&schema, &scales)?;

    let reader = builder.build()?;
    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res?;
        let symbol = col_str(&batch, "symbol")?;
        let timeframe = col_str(&batch, "timeframe")?;
        let ts_event = col_i64(&batch, "ts_event")?;
        let open = col_decimal(&batch, "open")?;
        let high = col_decimal(&batch, "high")?;
        let low = col_decimal(&batch, "low")?;
        let close = col_decimal(&batch, "close")?;
        let volume = col_decimal(&batch, "volume")?;
        let turnover = col_decimal(&batch, "turnover")?;
        let ts_available = col_i64(&batch, "ts_available")?;
        let ts_ingested = col_i64(&batch, "ts_ingested")?;
        let ts_committed = col_i64(&batch, "ts_committed")?;
        let precision = col_precision_dict(&batch, "precision")?;
        let revision = col_i64(&batch, "revision")?;

        for row in 0..batch.num_rows() {
            let ts = ts_event.value(row);
            if let Some(lo) = start_ts
                && ts < lo
            {
                continue;
            }
            if let Some(hi) = end_ts
                && ts > hi
            {
                continue;
            }
            let rev_i64 = revision.value(row);
            let rev = u64::try_from(rev_i64).map_err(|_| {
                StoreError::Integrity(format!("negative bar revision in parquet: {rev_i64}"))
            })?;
            out.push(RawBar {
                symbol: symbol.value(row).to_owned(),
                timeframe: timeframe.value(row).to_owned(),
                ts_event: ts,
                open: i128_to_decimal(open.value(row), scales["open"], "open")?,
                high: i128_to_decimal(high.value(row), scales["high"], "high")?,
                low: i128_to_decimal(low.value(row), scales["low"], "low")?,
                close: i128_to_decimal(close.value(row), scales["close"], "close")?,
                volume: i128_to_decimal(volume.value(row), scales["volume"], "volume")?,
                turnover: i128_to_decimal(turnover.value(row), scales["turnover"], "turnover")?,
                ts_available: ts_available.value(row),
                ts_ingested: ts_ingested.value(row),
                ts_committed: ts_committed.value(row),
                precision: parse_precision_tier(precision.value(row))?,
                revision: rev,
                confirmed: true,
                history_only: false,
                synthetic: false,
            });
        }
    }
    Ok(out)
}

// =========================================================================
// Derivatives
// =========================================================================

fn parse_derivative_kind(raw: &str) -> Result<DerivativeKind> {
    match raw {
        "mark_price" => Ok(DerivativeKind::MarkPrice),
        "funding_rate" => Ok(DerivativeKind::FundingRate),
        "open_interest" => Ok(DerivativeKind::OpenInterest),
        other => Err(StoreError::Integrity(format!(
            "unknown derivative kind {other:?}"
        ))),
    }
}

/// Build the Arrow schema for cold derivative Parquet files matching Python `_write_derivative_cold_parquet`.
pub fn derivatives_schema(scale: i8, metadata: HashMap<String, String>) -> Schema {
    Schema::new_with_metadata(
        vec![
            Field::new("instrument_id", DataType::Utf8, false),
            Field::new("kind", DataType::Utf8, false),
            Field::new("timeframe", DataType::Utf8, true),
            Field::new(
                "value",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scale),
                false,
            ),
            Field::new("ts_event", DataType::Int64, false),
            Field::new("ts_published", DataType::Int64, true),
            Field::new("settled", DataType::Boolean, true),
            Field::new("ts_available", DataType::Int64, false),
            Field::new("ts_ingested", DataType::Int64, false),
            Field::new("ts_committed", DataType::Int64, false),
            Field::new(
                "precision",
                DataType::Dictionary(Box::new(DataType::Int8), Box::new(DataType::Utf8)),
                false,
            ),
            Field::new("revision", DataType::Int64, false),
        ],
        metadata,
    )
}

/// Convert `DerivativePoint` rows to an Arrow `RecordBatch`.
pub fn derivatives_to_record_batch(
    rows: &[DerivativePoint],
    instrument: &InstrumentSpec,
    kind: DerivativeKind,
) -> Result<RecordBatch> {
    let scale = derivative_scale(instrument, kind)?;
    let mut scales = BTreeMap::new();
    scales.insert("value".to_owned(), scale);
    let metadata = build_cold_metadata(&scales, instrument)?;
    let schema = Arc::new(derivatives_schema(scale, metadata));

    let n = rows.len();
    let mut id_b = StringBuilder::with_capacity(n, n * 16);
    let mut kind_b = StringBuilder::with_capacity(n, n * 14);
    let mut tf_b = StringBuilder::with_capacity(n, n * 4);
    let mut val_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scale));
    let mut ts_event_b = Int64Builder::with_capacity(n);
    let mut ts_pub_b = Int64Builder::with_capacity(n);
    let mut settled_b = BooleanBuilder::with_capacity(n);
    let mut ts_avail_b = Int64Builder::with_capacity(n);
    let mut ts_ing_b = Int64Builder::with_capacity(n);
    let mut ts_comm_b = Int64Builder::with_capacity(n);
    let mut prec_b = StringDictionaryBuilder::<Int8Type>::new();
    let mut rev_b = Int64Builder::with_capacity(n);

    for point in rows {
        id_b.append_value(&point.instrument_id);
        kind_b.append_value(point.kind.as_str());
        tf_b.append_option(point.timeframe.as_deref());
        val_b.append_value(decimal_to_i128(point.value, scale, "value")?);
        ts_event_b.append_value(point.ts_event);
        ts_pub_b.append_option(point.ts_published);
        settled_b.append_option(point.settled);
        ts_avail_b.append_value(point.ts_available);
        ts_ing_b.append_value(point.ts_ingested);
        ts_comm_b.append_value(point.ts_committed);
        prec_b.append_value(point.precision.as_str());
        let rev = i64::try_from(point.revision).map_err(|_| {
            StoreError::Quality(format!("revision {} overflows i64", point.revision))
        })?;
        rev_b.append_value(rev);
    }

    let columns: Vec<ArrayRef> = vec![
        Arc::new(id_b.finish()),
        Arc::new(kind_b.finish()),
        Arc::new(tf_b.finish()),
        Arc::new(val_b.finish()),
        Arc::new(ts_event_b.finish()),
        Arc::new(ts_pub_b.finish()),
        Arc::new(settled_b.finish()),
        Arc::new(ts_avail_b.finish()),
        Arc::new(ts_ing_b.finish()),
        Arc::new(ts_comm_b.finish()),
        Arc::new(prec_b.finish()),
        Arc::new(rev_b.finish()),
    ];
    Ok(RecordBatch::try_new(schema, columns)?)
}

/// Write `DerivativePoint` rows to Parquet with `decimal128(18, s)` + ZSTD + metadata.
pub fn write_derivatives_parquet(
    path: &Path,
    rows: &[DerivativePoint],
    instrument: &InstrumentSpec,
    kind: DerivativeKind,
) -> Result<()> {
    let batch = derivatives_to_record_batch(rows, instrument, kind)?;
    write_record_batch_parquet(path, &batch)
}

/// Read `DerivativePoint` rows from cold Parquet, optionally filtering by range and `historical_kind`.
pub fn read_derivatives_parquet(
    path: &Path,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
    historical_kind: Option<DerivativeKind>,
) -> Result<Vec<DerivativePoint>> {
    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let scales = parse_column_scales_metadata(schema.metadata(), &["value"])?;
    verify_decimal_schema(&schema, &scales)?;

    let has_settled = schema.field_with_name("settled").is_ok();
    let reader = builder.build()?;
    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res?;
        let instrument_id = col_str(&batch, "instrument_id")?;
        let kind_col = col_str(&batch, "kind")?;
        let timeframe = col_str(&batch, "timeframe")?;
        let value = col_decimal(&batch, "value")?;
        let ts_event = col_i64(&batch, "ts_event")?;
        let ts_published = col_i64(&batch, "ts_published")?;
        let settled = if has_settled {
            Some(col_bool(&batch, "settled")?)
        } else {
            None
        };
        let ts_available = col_i64(&batch, "ts_available")?;
        let ts_ingested = col_i64(&batch, "ts_ingested")?;
        let ts_committed = col_i64(&batch, "ts_committed")?;
        let precision = col_precision_dict(&batch, "precision")?;
        let revision = col_i64(&batch, "revision")?;

        for row in 0..batch.num_rows() {
            let ts = ts_event.value(row);
            if let Some(lo) = start_ts
                && ts < lo
            {
                continue;
            }
            if let Some(hi) = end_ts
                && ts > hi
            {
                continue;
            }
            let tf_opt = (!timeframe.is_null(row)).then(|| timeframe.value(row).to_owned());
            let ts_pub_opt = (!ts_published.is_null(row)).then(|| ts_published.value(row));
            let settled_opt = settled.and_then(|arr| (!arr.is_null(row)).then(|| arr.value(row)));

            if let Some(hk) = historical_kind {
                match hk {
                    DerivativeKind::FundingRate => {
                        let legacy = ts_pub_opt == Some(ts);
                        let is_hist = match settled_opt {
                            Some(true) => true,
                            Some(false) => false,
                            None => legacy,
                        };
                        if !is_hist {
                            continue;
                        }
                    }
                    DerivativeKind::MarkPrice | DerivativeKind::OpenInterest => {
                        if tf_opt.is_none() {
                            continue;
                        }
                    }
                }
            }

            let rev_i64 = revision.value(row);
            let rev = u64::try_from(rev_i64).map_err(|_| {
                StoreError::Integrity(format!(
                    "negative derivative revision in parquet: {rev_i64}"
                ))
            })?;
            out.push(DerivativePoint {
                instrument_id: instrument_id.value(row).to_owned(),
                kind: parse_derivative_kind(kind_col.value(row))?,
                timeframe: tf_opt,
                value: i128_to_decimal(value.value(row), scales["value"], "value")?,
                ts_event: ts,
                ts_published: ts_pub_opt,
                ts_available: ts_available.value(row),
                precision: parse_precision_tier(precision.value(row))?,
                ts_ingested: ts_ingested.value(row),
                ts_committed: ts_committed.value(row),
                revision: rev,
                settled: settled_opt,
            });
        }
    }
    Ok(out)
}

// =========================================================================
// Ticks: Trades & Quotes
// =========================================================================

/// Build the Arrow schema for cold `Trade` Parquet files matching Python `_write_tick_parquet`.
pub fn trades_schema(scales: &BTreeMap<String, i8>, metadata: HashMap<String, String>) -> Schema {
    Schema::new_with_metadata(
        vec![
            Field::new("ts_event", DataType::Int64, false),
            Field::new("ts_available", DataType::Int64, false),
            Field::new("ts_ingested", DataType::Int64, false),
            Field::new("ts_committed", DataType::Int64, false),
            Field::new("revision", DataType::Int64, false),
            Field::new(
                "precision",
                DataType::Dictionary(Box::new(DataType::Int8), Box::new(DataType::Utf8)),
                false,
            ),
            Field::new(
                "price",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["price"]),
                false,
            ),
            Field::new(
                "size",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["size"]),
                false,
            ),
            Field::new("availability", DataType::Utf8, true),
            Field::new("archive_day", DataType::Int64, true),
            Field::new("archive_index", DataType::Int64, true),
            Field::new("sequence", DataType::Int64, true),
            Field::new("packet_id", DataType::FixedSizeBinary(16), true),
            Field::new("packet_index", DataType::Int64, true),
            Field::new("trade_id", DataType::FixedSizeBinary(16), false),
            Field::new("side", DataType::Boolean, false),
        ],
        metadata,
    )
}

/// Build the Arrow schema for cold `Quote` Parquet files matching Python `_write_tick_parquet`.
pub fn quotes_schema(scales: &BTreeMap<String, i8>, metadata: HashMap<String, String>) -> Schema {
    Schema::new_with_metadata(
        vec![
            Field::new("ts_event", DataType::Int64, false),
            Field::new("ts_available", DataType::Int64, false),
            Field::new("ts_ingested", DataType::Int64, false),
            Field::new("ts_committed", DataType::Int64, false),
            Field::new("revision", DataType::Int64, false),
            Field::new(
                "precision",
                DataType::Dictionary(Box::new(DataType::Int8), Box::new(DataType::Utf8)),
                false,
            ),
            Field::new(
                "bid_price",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["bid_price"]),
                false,
            ),
            Field::new(
                "bid_size",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["bid_size"]),
                false,
            ),
            Field::new(
                "ask_price",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["ask_price"]),
                false,
            ),
            Field::new(
                "ask_size",
                DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["ask_size"]),
                false,
            ),
            Field::new("connection_id", DataType::FixedSizeBinary(16), true),
            Field::new("update_id", DataType::Int64, true),
        ],
        metadata,
    )
}

/// Convert `Trade` rows to an Arrow `RecordBatch`.
pub fn trades_to_record_batch(
    rows: &[Trade],
    instrument: &InstrumentSpec,
) -> Result<RecordBatch> {
    let scales = tick_column_scales(instrument, TickKind::Trades)?;
    let metadata = build_cold_metadata(&scales, instrument)?;
    let schema = Arc::new(trades_schema(&scales, metadata));

    let n = rows.len();
    let mut ts_event_b = Int64Builder::with_capacity(n);
    let mut ts_avail_b = Int64Builder::with_capacity(n);
    let mut ts_ing_b = Int64Builder::with_capacity(n);
    let mut ts_comm_b = Int64Builder::with_capacity(n);
    let mut rev_b = Int64Builder::with_capacity(n);
    let mut prec_b = StringDictionaryBuilder::<Int8Type>::new();
    let mut price_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["price"]));
    let mut size_b = Decimal128Builder::with_capacity(n)
        .with_data_type(DataType::Decimal128(COLD_DECIMAL_PRECISION, scales["size"]));
    let mut avail_b = StringBuilder::with_capacity(n, n * 8);
    let mut archive_day_b = Int64Builder::with_capacity(n);
    let mut archive_idx_b = Int64Builder::with_capacity(n);
    let mut seq_b = Int64Builder::with_capacity(n);
    let mut packet_id_b = FixedSizeBinaryBuilder::with_capacity(n, 16);
    let mut packet_idx_b = Int64Builder::with_capacity(n);
    let mut trade_id_b = FixedSizeBinaryBuilder::with_capacity(n, 16);
    let mut side_b = BooleanBuilder::with_capacity(n);

    for row in rows {
        ts_event_b.append_value(row.ts_event);
        ts_avail_b.append_value(row.ts_available);
        ts_ing_b.append_value(row.ts_ingested);
        ts_comm_b.append_value(row.ts_committed);
        let rev = i64::try_from(row.revision)
            .map_err(|_| StoreError::Quality(format!("revision {} overflows i64", row.revision)))?;
        rev_b.append_value(rev);
        prec_b.append_value(row.precision.as_str());
        price_b.append_value(decimal_to_i128(row.price, scales["price"], "price")?);
        size_b.append_value(decimal_to_i128(row.size, scales["size"], "size")?);
        let avail_str = match row.availability {
            TradeAvailability::Measured => "measured",
            TradeAvailability::Inferred => "inferred",
        };
        avail_b.append_value(avail_str);
        archive_day_b.append_option(row.archive_day);
        archive_idx_b.append_option(row.archive_index);
        seq_b.append_option(row.sequence);
        match row.packet_id {
            Some(pid) => packet_id_b.append_value(pid.as_bytes())?,
            None => packet_id_b.append_null(),
        }
        packet_idx_b.append_option(row.packet_index);
        trade_id_b.append_value(row.trade_id.as_bytes())?;
        side_b.append_value(row.side == TradeSide::Buy);
    }

    let columns: Vec<ArrayRef> = vec![
        Arc::new(ts_event_b.finish()),
        Arc::new(ts_avail_b.finish()),
        Arc::new(ts_ing_b.finish()),
        Arc::new(ts_comm_b.finish()),
        Arc::new(rev_b.finish()),
        Arc::new(prec_b.finish()),
        Arc::new(price_b.finish()),
        Arc::new(size_b.finish()),
        Arc::new(avail_b.finish()),
        Arc::new(archive_day_b.finish()),
        Arc::new(archive_idx_b.finish()),
        Arc::new(seq_b.finish()),
        Arc::new(packet_id_b.finish()),
        Arc::new(packet_idx_b.finish()),
        Arc::new(trade_id_b.finish()),
        Arc::new(side_b.finish()),
    ];
    Ok(RecordBatch::try_new(schema, columns)?)
}

/// Convert `Quote` rows to an Arrow `RecordBatch`.
pub fn quotes_to_record_batch(
    rows: &[Quote],
    instrument: &InstrumentSpec,
) -> Result<RecordBatch> {
    let scales = tick_column_scales(instrument, TickKind::Quotes)?;
    let metadata = build_cold_metadata(&scales, instrument)?;
    let schema = Arc::new(quotes_schema(&scales, metadata));

    let n = rows.len();
    let mut ts_event_b = Int64Builder::with_capacity(n);
    let mut ts_avail_b = Int64Builder::with_capacity(n);
    let mut ts_ing_b = Int64Builder::with_capacity(n);
    let mut ts_comm_b = Int64Builder::with_capacity(n);
    let mut rev_b = Int64Builder::with_capacity(n);
    let mut prec_b = StringDictionaryBuilder::<Int8Type>::new();
    let mut bid_p_b = Decimal128Builder::with_capacity(n).with_data_type(DataType::Decimal128(
        COLD_DECIMAL_PRECISION,
        scales["bid_price"],
    ));
    let mut bid_s_b = Decimal128Builder::with_capacity(n).with_data_type(DataType::Decimal128(
        COLD_DECIMAL_PRECISION,
        scales["bid_size"],
    ));
    let mut ask_p_b = Decimal128Builder::with_capacity(n).with_data_type(DataType::Decimal128(
        COLD_DECIMAL_PRECISION,
        scales["ask_price"],
    ));
    let mut ask_s_b = Decimal128Builder::with_capacity(n).with_data_type(DataType::Decimal128(
        COLD_DECIMAL_PRECISION,
        scales["ask_size"],
    ));
    let mut conn_id_b = FixedSizeBinaryBuilder::with_capacity(n, 16);
    let mut update_id_b = Int64Builder::with_capacity(n);

    for row in rows {
        ts_event_b.append_value(row.ts_event);
        ts_avail_b.append_value(row.ts_available);
        ts_ing_b.append_value(row.ts_ingested);
        ts_comm_b.append_value(row.ts_committed);
        let rev = i64::try_from(row.revision)
            .map_err(|_| StoreError::Quality(format!("revision {} overflows i64", row.revision)))?;
        rev_b.append_value(rev);
        prec_b.append_value(row.precision.as_str());
        bid_p_b.append_value(decimal_to_i128(
            row.bid_price,
            scales["bid_price"],
            "bid_price",
        )?);
        bid_s_b.append_value(decimal_to_i128(
            row.bid_size,
            scales["bid_size"],
            "bid_size",
        )?);
        ask_p_b.append_value(decimal_to_i128(
            row.ask_price,
            scales["ask_price"],
            "ask_price",
        )?);
        ask_s_b.append_value(decimal_to_i128(
            row.ask_size,
            scales["ask_size"],
            "ask_size",
        )?);
        match row.connection_id {
            Some(cid) => conn_id_b.append_value(cid.as_bytes())?,
            None => conn_id_b.append_null(),
        }
        update_id_b.append_option(row.update_id);
    }

    let columns: Vec<ArrayRef> = vec![
        Arc::new(ts_event_b.finish()),
        Arc::new(ts_avail_b.finish()),
        Arc::new(ts_ing_b.finish()),
        Arc::new(ts_comm_b.finish()),
        Arc::new(rev_b.finish()),
        Arc::new(prec_b.finish()),
        Arc::new(bid_p_b.finish()),
        Arc::new(bid_s_b.finish()),
        Arc::new(ask_p_b.finish()),
        Arc::new(ask_s_b.finish()),
        Arc::new(conn_id_b.finish()),
        Arc::new(update_id_b.finish()),
    ];
    Ok(RecordBatch::try_new(schema, columns)?)
}

/// Write `Trade` rows to Parquet.
pub fn write_trades_parquet(
    path: &Path,
    rows: &[Trade],
    instrument: &InstrumentSpec,
) -> Result<()> {
    let batch = trades_to_record_batch(rows, instrument)?;
    write_record_batch_parquet(path, &batch)
}

/// Write `Quote` rows to Parquet.
pub fn write_quotes_parquet(
    path: &Path,
    rows: &[Quote],
    instrument: &InstrumentSpec,
) -> Result<()> {
    let batch = quotes_to_record_batch(rows, instrument)?;
    write_record_batch_parquet(path, &batch)
}

/// Read `Trade` rows from cold Parquet, verifying metadata and schema.
pub fn read_trades_parquet(
    path: &Path,
    instrument_id: &str,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
    trade_ids: Option<&HashSet<[u8; 16]>>,
) -> Result<Vec<Trade>> {
    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let scales = parse_column_scales_metadata(schema.metadata(), &["price", "size"])?;
    verify_decimal_schema(&schema, &scales)?;

    let has_availability = schema.field_with_name("availability").is_ok();
    let reader = builder.build()?;
    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res?;
        let ts_event = col_i64(&batch, "ts_event")?;
        let ts_available = col_i64(&batch, "ts_available")?;
        let ts_ingested = col_i64(&batch, "ts_ingested")?;
        let ts_committed = col_i64(&batch, "ts_committed")?;
        let revision = col_i64(&batch, "revision")?;
        let precision = col_precision_dict(&batch, "precision")?;
        let price = col_decimal(&batch, "price")?;
        let size = col_decimal(&batch, "size")?;
        let availability = if has_availability {
            Some(col_str(&batch, "availability")?)
        } else {
            None
        };
        let archive_day = col_i64(&batch, "archive_day")?;
        let archive_index = col_i64(&batch, "archive_index")?;
        let sequence = col_i64(&batch, "sequence")?;
        let packet_id = col_fixed_binary(&batch, "packet_id")?;
        let packet_index = col_i64(&batch, "packet_index")?;
        let trade_id = col_fixed_binary(&batch, "trade_id")?;
        let side = col_bool(&batch, "side")?;

        for row in 0..batch.num_rows() {
            let ts = ts_event.value(row);
            if let Some(lo) = start_ts
                && ts < lo
            {
                continue;
            }
            if let Some(hi) = end_ts
                && ts > hi
            {
                continue;
            }
            let tid_bytes = fixed_bytes_16(trade_id.value(row), "trade_id")?;
            if let Some(ids) = trade_ids
                && !ids.contains(&tid_bytes)
            {
                continue;
            }
            let ts_avail = ts_available.value(row);
            let ts_ing = ts_ingested.value(row);
            let avail = match availability.and_then(|arr| (!arr.is_null(row)).then(|| arr.value(row)))
            {
                Some("measured") => TradeAvailability::Measured,
                Some("inferred") => TradeAvailability::Inferred,
                Some(other) => {
                    return Err(StoreError::Integrity(format!(
                        "unknown trade availability {other:?}"
                    )));
                }
                None if ts_avail < ts_ing => TradeAvailability::Inferred,
                None => TradeAvailability::Measured,
            };
            let rev_i64 = revision.value(row);
            let rev = u64::try_from(rev_i64).map_err(|_| {
                StoreError::Integrity(format!("negative trade revision in parquet: {rev_i64}"))
            })?;
            let pid = if packet_id.is_null(row) {
                None
            } else {
                Some(uuid_from_bytes_16(
                    fixed_bytes_16(packet_id.value(row), "packet_id")?,
                    "packet_id",
                )?)
            };
            out.push(Trade {
                instrument_id: instrument_id.to_owned(),
                ts_event: ts,
                ts_available: ts_avail,
                ts_ingested: ts_ing,
                ts_committed: ts_committed.value(row),
                revision: rev,
                precision: parse_precision_tier(precision.value(row))?,
                order_uncertain: false,
                trade_id: uuid_from_bytes_16(tid_bytes, "trade_id")?,
                side: if side.value(row) {
                    TradeSide::Buy
                } else {
                    TradeSide::Sell
                },
                price: i128_to_decimal(price.value(row), scales["price"], "price")?,
                size: i128_to_decimal(size.value(row), scales["size"], "size")?,
                availability: avail,
                archive_day: (!archive_day.is_null(row)).then(|| archive_day.value(row)),
                archive_index: (!archive_index.is_null(row)).then(|| archive_index.value(row)),
                sequence: (!sequence.is_null(row)).then(|| sequence.value(row)),
                packet_id: pid,
                packet_index: (!packet_index.is_null(row)).then(|| packet_index.value(row)),
            });
        }
    }
    Ok(out)
}

/// Read `Quote` rows from cold Parquet, verifying metadata and schema.
pub fn read_quotes_parquet(
    path: &Path,
    instrument_id: &str,
    start_ts: Option<i64>,
    end_ts: Option<i64>,
) -> Result<Vec<Quote>> {
    let file = File::open(path)?;
    let builder = ParquetRecordBatchReaderBuilder::try_new(file)?;
    let schema = builder.schema().clone();
    let scales = parse_column_scales_metadata(
        schema.metadata(),
        &["bid_price", "bid_size", "ask_price", "ask_size"],
    )?;
    verify_decimal_schema(&schema, &scales)?;

    let reader = builder.build()?;
    let mut out = Vec::new();
    for batch_res in reader {
        let batch = batch_res?;
        let ts_event = col_i64(&batch, "ts_event")?;
        let ts_available = col_i64(&batch, "ts_available")?;
        let ts_ingested = col_i64(&batch, "ts_ingested")?;
        let ts_committed = col_i64(&batch, "ts_committed")?;
        let revision = col_i64(&batch, "revision")?;
        let precision = col_precision_dict(&batch, "precision")?;
        let bid_price = col_decimal(&batch, "bid_price")?;
        let bid_size = col_decimal(&batch, "bid_size")?;
        let ask_price = col_decimal(&batch, "ask_price")?;
        let ask_size = col_decimal(&batch, "ask_size")?;
        let connection_id = col_fixed_binary(&batch, "connection_id")?;
        let update_id = col_i64(&batch, "update_id")?;

        for row in 0..batch.num_rows() {
            let ts = ts_event.value(row);
            if let Some(lo) = start_ts
                && ts < lo
            {
                continue;
            }
            if let Some(hi) = end_ts
                && ts > hi
            {
                continue;
            }
            let rev_i64 = revision.value(row);
            let rev = u64::try_from(rev_i64).map_err(|_| {
                StoreError::Integrity(format!("negative quote revision in parquet: {rev_i64}"))
            })?;
            let cid = if connection_id.is_null(row) {
                None
            } else {
                Some(uuid_from_bytes_16(
                    fixed_bytes_16(connection_id.value(row), "connection_id")?,
                    "connection_id",
                )?)
            };
            out.push(Quote {
                instrument_id: instrument_id.to_owned(),
                ts_event: ts,
                ts_available: ts_available.value(row),
                ts_ingested: ts_ingested.value(row),
                ts_committed: ts_committed.value(row),
                revision: rev,
                precision: parse_precision_tier(precision.value(row))?,
                order_uncertain: false,
                bid_price: i128_to_decimal(
                    bid_price.value(row),
                    scales["bid_price"],
                    "bid_price",
                )?,
                bid_size: i128_to_decimal(bid_size.value(row), scales["bid_size"], "bid_size")?,
                ask_price: i128_to_decimal(
                    ask_price.value(row),
                    scales["ask_price"],
                    "ask_price",
                )?,
                ask_size: i128_to_decimal(ask_size.value(row), scales["ask_size"], "ask_size")?,
                connection_id: cid,
                update_id: (!update_id.is_null(row)).then(|| update_id.value(row)),
            });
        }
    }
    Ok(out)
}
