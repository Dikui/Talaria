//! Internal UUID construction/parsing bridge via `talaria-contracts` Serde types.
//!
//! `talaria-packs/Cargo.toml` does not declare `uuid` directly, while `talaria-contracts`
//! exposes `Quote::connection_id`, `TradeEvent::trade_id`, and `Trade` using `uuid::Uuid`.
//! Using `serde_json` through those contract structs preserves strict RFC 4122 UUID validation
//! without modifying workspace manifests.

use std::sync::atomic::{AtomicU64, Ordering};

use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use talaria_contracts::{
    OrderBookEvent, PrecisionTier, Quote, ReceivedOrderBookEvent, Trade, TradeAvailability,
    TradeEvent, TradeSide, format_decimal_str,
};

use crate::error::PackError;
use crate::traits::ReceivedTradeEvent;

static CONNECTION_COUNTER: AtomicU64 = AtomicU64::new(1);

/// Opaque wrapper storing a validated `Quote` carrying a generated connection UUID.
#[derive(Debug, Clone)]
pub(crate) struct ConnectionStamp(Quote);

impl ConnectionStamp {
    /// Generate a fresh unique connection stamp (`UUID v4` format).
    pub(crate) fn fresh(salt: &str, now_ms: i64) -> Result<Self, PackError> {
        let seq = CONNECTION_COUNTER.fetch_add(1, Ordering::Relaxed);
        let mut hasher = Sha256::new();
        hasher.update(salt.as_bytes());
        hasher.update(now_ms.to_le_bytes());
        hasher.update(seq.to_le_bytes());
        let digest = hasher.finalize();
        let mut bytes = [0u8; 16];
        bytes.copy_from_slice(&digest[..16]);
        let uuid_str = format_uuid_bytes(bytes, 4);
        let quote: Quote = serde_json::from_value(json!({
            "instrument_id": "STAMP",
            "ts_event": 0,
            "ts_available": 0,
            "ts_ingested": 0,
            "ts_committed": 0,
            "bid_price": "1",
            "bid_size": "1",
            "ask_price": "1",
            "ask_size": "1",
            "connection_id": uuid_str,
        }))
        .map_err(|err| PackError::FeedTransport(format!("failed to generate connection UUID: {err}")))?;
        Ok(Self(quote))
    }

    pub(crate) fn build_received_order_book(
        &self,
        event: OrderBookEvent,
        ts_published: Option<i64>,
        update_id: u64,
    ) -> ReceivedOrderBookEvent {
        ReceivedOrderBookEvent {
            event,
            ts_published,
            connection_id: self.0.connection_id.expect("stamp always carries connection_id"),
            update_id,
        }
    }
}

/// Format 16 bytes into RFC 4122 canonical lowercase UUID string with given version nibble (`4` or `5`).
pub(crate) fn format_uuid_bytes(mut bytes: [u8; 16], version: u8) -> String {
    bytes[6] = (bytes[6] & 0x0f) | ((version & 0x0f) << 4);
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let encoded = hex::encode(bytes);
    format!(
        "{}-{}-{}-{}-{}",
        &encoded[0..8],
        &encoded[8..12],
        &encoded[12..16],
        &encoded[16..20],
        &encoded[20..32]
    )
}

/// Deterministic packet UUID string computed from trade UUID strings.
pub(crate) fn deterministic_packet_uuid(trade_ids_json: &[String]) -> String {
    let mut hasher = Sha256::new();
    for id in trade_ids_json {
        for byte in id.as_bytes() {
            if *byte != b'-' {
                hasher.update([byte.to_ascii_lowercase()]);
            }
        }
    }
    let digest = hasher.finalize();
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    format_uuid_bytes(bytes, 5)
}

/// Parse a `TradeEvent` using `talaria-contracts`'s strict `DecimalStr` and `Uuid` deserializers.
pub(crate) fn parse_trade_event(
    instrument_id: &str,
    trade_id_str: &str,
    side: TradeSide,
    price_str: &str,
    size_str: &str,
    ts_event: i64,
    ts_ingested: i64,
) -> Result<TradeEvent, PackError> {
    let payload = json!({
        "instrument_id": instrument_id,
        "trade_id": trade_id_str,
        "side": side,
        "price": price_str,
        "size": size_str,
        "ts_event": ts_event,
        "ts_ingested": ts_ingested,
    });
    serde_json::from_value(payload)
        .map_err(|err| PackError::FeedTransport(format!("invalid Bybit trade payload: {err}")))
}

/// Convert [`ReceivedTradeEvent`] into [`Trade`].
pub(crate) fn trade_to_contract(
    received: &ReceivedTradeEvent,
    ts_committed: i64,
) -> Result<Trade, PackError> {
    let ev = &received.event;
    let ts_available = [ev.ts_event, received.ts_published.unwrap_or(0), ev.ts_ingested]
        .into_iter()
        .max()
        .unwrap_or(ev.ts_ingested);
    let mut map = serde_json::Map::new();
    map.insert("instrument_id".to_owned(), Value::String(ev.instrument_id.clone()));
    map.insert("ts_event".to_owned(), Value::from(ev.ts_event));
    map.insert("ts_available".to_owned(), Value::from(ts_available));
    map.insert("ts_ingested".to_owned(), Value::from(ev.ts_ingested));
    map.insert(
        "ts_committed".to_owned(),
        Value::from(ts_committed.max(ev.ts_ingested)),
    );
    map.insert("revision".to_owned(), Value::from(0u64));
    map.insert(
        "precision".to_owned(),
        serde_json::to_value(PrecisionTier::SecondVerified)
            .map_err(|e| PackError::Archive(e.to_string()))?,
    );
    map.insert("order_uncertain".to_owned(), Value::Bool(false));
    map.insert(
        "trade_id".to_owned(),
        serde_json::to_value(ev.trade_id).map_err(|e| PackError::Archive(e.to_string()))?,
    );
    map.insert(
        "side".to_owned(),
        serde_json::to_value(ev.side).map_err(|e| PackError::Archive(e.to_string()))?,
    );
    map.insert("price".to_owned(), Value::String(format_decimal_str(ev.price)));
    map.insert("size".to_owned(), Value::String(format_decimal_str(ev.size)));
    map.insert(
        "availability".to_owned(),
        serde_json::to_value(TradeAvailability::Measured)
            .map_err(|e| PackError::Archive(e.to_string()))?,
    );
    if let Some(seq) = received.sequence {
        map.insert("sequence".to_owned(), Value::from(seq));
    }
    if let Some(pid) = &received.packet_id {
        map.insert("packet_id".to_owned(), Value::String(pid.clone()));
    }
    if let Some(pidx) = received.packet_index {
        map.insert("packet_index".to_owned(), Value::from(pidx));
    }
    serde_json::from_value(Value::Object(map))
        .map_err(|err| PackError::Archive(format!("failed to materialize Trade: {err}")))
}
