//! Lightweight JSON tick endpoint (`GET /api/ticks`) for Vela intrabar replay.

use std::sync::Arc;

use axum::Router;
use axum::extract::State;
use axum::routing::get;
use rust_decimal::Decimal;
use serde::Deserialize;
use talaria_contracts::ticks::{ReplayTick, TickKind, TicksJsonResponse};

use crate::error::{ApiError, ApiJson, ApiQuery};
use crate::routes::find_instrument;
use crate::server::AppState;

const DEFAULT_TICKS_LIMIT: usize = 2000;
const MAX_TICKS_LIMIT: usize = 20_000;

pub fn router() -> Router<Arc<AppState>> {
    Router::new().route("/api/ticks", get(get_ticks_json))
}

fn default_tick_kind() -> String {
    "trades".to_owned()
}

fn default_limit() -> usize {
    DEFAULT_TICKS_LIMIT
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TicksJsonQuery {
    #[serde(default)]
    pub instrument: Option<String>,
    #[serde(default)]
    pub instrument_id: Option<String>,
    #[serde(default = "default_tick_kind")]
    pub kind: String,
    #[serde(default)]
    pub start: i64,
    #[serde(default)]
    pub end: Option<i64>,
    #[serde(default)]
    pub as_of: Option<i64>,
    #[serde(default = "default_limit")]
    pub limit: usize,
}

struct TickSample {
    ts_event: i64,
    price: Decimal,
    volume: Option<Decimal>,
}

fn aggregate_samples(samples: Vec<TickSample>, limit: usize) -> (Vec<ReplayTick>, bool) {
    if samples.is_empty() {
        return (Vec::new(), false);
    }
    if samples.len() <= limit {
        let ticks = samples
            .into_iter()
            .map(|s| ReplayTick {
                price: s.price,
                open: None,
                high: None,
                low: None,
                volume: s.volume,
            })
            .collect();
        return (ticks, false);
    }

    let first_ts = samples.first().map(|s| s.ts_event).unwrap_or(0);
    let last_ts = samples.last().map(|s| s.ts_event).unwrap_or(first_ts);

    let mut out = Vec::with_capacity(limit);
    if last_ts <= first_ts {
        let chunk_size = samples.len().div_ceil(limit);
        for chunk in samples.chunks(chunk_size) {
            out.push(reduce_bucket(chunk));
        }
        return (out, true);
    }

    let span = (last_ts - first_ts) as u128 + 1;
    let bucket_count = limit as u128;
    let mut bucket_start = 0usize;
    let mut current_bucket = (((samples[0].ts_event - first_ts) as u128 * bucket_count) / span)
        .min(bucket_count - 1) as usize;

    for idx in 1..samples.len() {
        let b_idx = (((samples[idx].ts_event - first_ts) as u128 * bucket_count) / span)
            .min(bucket_count - 1) as usize;
        if b_idx != current_bucket {
            out.push(reduce_bucket(&samples[bucket_start..idx]));
            bucket_start = idx;
            current_bucket = b_idx;
        }
    }
    if bucket_start < samples.len() {
        out.push(reduce_bucket(&samples[bucket_start..]));
    }

    (out, true)
}

fn reduce_bucket(slice: &[TickSample]) -> ReplayTick {
    let open = slice[0].price;
    let close = slice[slice.len() - 1].price;
    let mut high = open;
    let mut low = open;
    let mut vol_sum = Decimal::ZERO;
    let mut has_volume = false;

    for item in slice {
        if item.price > high {
            high = item.price;
        }
        if item.price < low {
            low = item.price;
        }
        if let Some(v) = item.volume {
            vol_sum += v;
            has_volume = true;
        }
    }

    ReplayTick {
        price: close,
        open: Some(open),
        high: Some(high),
        low: Some(low),
        volume: has_volume.then_some(vol_sum),
    }
}

pub async fn get_ticks_json(
    State(state): State<Arc<AppState>>,
    ApiQuery(query): ApiQuery<TicksJsonQuery>,
) -> Result<ApiJson<TicksJsonResponse>, ApiError> {
    if query.start < 0 {
        return Err(ApiError::validation("start must be non-negative"));
    }
    if let Some(end) = query.end
        && end < query.start
    {
        return Err(ApiError::validation("end must not precede start"));
    }
    if let Some(as_of) = query.as_of
        && as_of < 0
    {
        return Err(ApiError::validation("as_of must be non-negative"));
    }
    let inst_id = query
        .instrument
        .or(query.instrument_id)
        .ok_or_else(|| ApiError::validation("instrument is required"))?;
    let spec = find_instrument(&state, &inst_id)?;

    let kind = match query.kind.trim().to_ascii_lowercase().as_str() {
        "trades" | "trade" => TickKind::Trades,
        "quotes" | "quote" => TickKind::Quotes,
        other => {
            return Err(ApiError::validation(format!(
                "unsupported tick kind: {other}"
            )));
        }
    };

    let now = state.now_ms();
    let effective_as_of = query.as_of.map(|a| a.min(now)).unwrap_or(now);
    let effective_end = query.end.unwrap_or(effective_as_of).max(query.start);
    let limit = query.limit.clamp(1, MAX_TICKS_LIMIT);
    let start = query.start;
    let reader = Arc::clone(&state.reader);

    let (ticks, aggregated) = tokio::task::spawn_blocking(move || -> Result<_, ApiError> {
        let samples: Vec<TickSample> = match kind {
            TickKind::Trades => reader
                .read_trades(&spec, Some(start), Some(effective_end), Some(effective_as_of))?
                .into_iter()
                .map(|t| TickSample {
                    ts_event: t.ts_event,
                    price: t.price,
                    volume: Some(t.size),
                })
                .collect(),
            TickKind::Quotes => reader
                .read_quotes(&spec, Some(start), Some(effective_end), Some(effective_as_of))?
                .into_iter()
                .map(|q| TickSample {
                    ts_event: q.ts_event,
                    price: (q.bid_price + q.ask_price) / Decimal::TWO,
                    volume: None,
                })
                .collect(),
        };
        Ok(aggregate_samples(samples, limit))
    })
    .await
    .map_err(|e| ApiError::internal(format!("ticks json join error: {e}")))??;

    Ok(ApiJson(TicksJsonResponse {
        instrument_id: inst_id,
        kind,
        start,
        end: effective_end,
        as_of: Some(effective_as_of),
        aggregated,
        ticks,
    }))
}
