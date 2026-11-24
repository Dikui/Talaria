//! Market pack abstractions, exchange connectors, offline fixture connector, and WS transports.

pub mod crypto_perp;
pub mod error;
pub mod proxy;
pub mod testing;
pub mod traits;
mod uuid_util;

use std::sync::Arc;

pub use crypto_perp::{CryptoPerpPack, crypto_perp_pack};
pub use error::PackError;
pub use testing::FixtureConnector;
pub use traits::{
    BackfillWindow, BoxFuture, ClockFn, FeedTransport, HistoryConnector, HistoryConnectorConfig,
    MarginValuation, MarketPack, RealtimeStreamConfig, ReceivedTradeEvent, TradeArchiveReader,
    TransportFrame, backfill_bars, backfill_derivatives, default_clock, system_now_ms,
};

/// Default market pack name (`"crypto_perp"`).
pub const DEFAULT_PACK_NAME: &str = "crypto_perp";

/// Return all registered market pack names.
pub fn available_pack_names() -> Vec<&'static str> {
    vec![DEFAULT_PACK_NAME]
}

/// Look up a registered [`MarketPack`] by name (`"crypto_perp"`).
pub fn get_market_pack(name: &str) -> Result<Arc<dyn MarketPack>, PackError> {
    match name {
        "crypto_perp" => Ok(crypto_perp_pack()),
        other => Err(PackError::InvalidRequest(format!(
            "unknown market pack `{other}`; available: {}",
            available_pack_names().join(", ")
        ))),
    }
}
