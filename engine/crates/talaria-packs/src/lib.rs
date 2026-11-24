//! Market pack abstractions, exchange connectors, offline fixture connector, and WS transports.

pub mod binance_perp;
pub mod crypto_perp;
pub mod error;
pub mod proxy;
pub mod testing;
pub mod traits;
pub mod us_equity;
mod uuid_util;

use std::sync::Arc;

pub use binance_perp::{BinancePerpPack, binance_perp_pack};
pub use crypto_perp::{CryptoPerpPack, crypto_perp_pack};
pub use error::PackError;
pub use testing::FixtureConnector;
pub use traits::{
    BackfillWindow, BoxFuture, ClockFn, FeedTransport, HistoryConnector, HistoryConnectorConfig,
    MarginValuation, MarketPack, RealtimeStreamConfig, ReceivedTradeEvent, TradeArchiveReader,
    TransportFrame, backfill_bars, backfill_derivatives, default_clock, system_now_ms,
};
pub use us_equity::{
    AlpacaDataFeed, EquityCalendar, EquityCalendarEvent, EquityCalendarObservation,
    EquityEventKind, EquityEventStatus, EtDate, UsEquityPack, UsMarketSession,
    classify_us_equity_session, et_to_utc_ms, is_us_market_holiday, is_within_feed_hours,
    us_equity_pack, us_equity_sip_pack, utc_ms_to_et,
};

/// Default market pack name (`"crypto_perp"`).
pub const DEFAULT_PACK_NAME: &str = "crypto_perp";

/// Return all registered market pack names.
pub fn available_pack_names() -> Vec<&'static str> {
    vec![DEFAULT_PACK_NAME, "binance_perp", "us_equity"]
}

/// Look up a registered [`MarketPack`] by name (`"crypto_perp"`, `"binance_perp"`, `"us_equity"`).
pub fn get_market_pack(name: &str) -> Result<Arc<dyn MarketPack>, PackError> {
    match name {
        "crypto_perp" => Ok(crypto_perp_pack()),
        "binance_perp" => Ok(binance_perp_pack()),
        "us_equity" => Ok(us_equity_pack()),
        other => Err(PackError::InvalidRequest(format!(
            "unknown market pack `{other}`; available: {}",
            available_pack_names().join(", ")
        ))),
    }
}
