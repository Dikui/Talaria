//! Market pack abstractions, exchange connectors, offline fixture connector, and WS transports.

pub mod binance_perp;
pub mod catalog;
pub mod coinbase_spot;
pub mod composite;
pub mod crypto_perp;
pub mod error;
pub mod hyperliquid_perp;
pub mod okx_perp;
pub mod proxy;
pub mod testing;
pub mod traits;
pub mod us_equity;
mod uuid_util;

use std::sync::Arc;

pub use binance_perp::{BinancePerpPack, binance_perp_pack};
pub use catalog::{CatalogInstrument, DEFAULT_SEARCH_LIMIT, clear_catalog_cache};
pub use coinbase_spot::{CoinbaseSpotPack, coinbase_spot_pack};
pub use composite::{CompositeMarketPack, crypto_suite_pack};
pub use crypto_perp::{CryptoPerpPack, crypto_perp_pack};
pub use error::PackError;
pub use hyperliquid_perp::{HyperliquidPerpPack, hyperliquid_perp_pack};
pub use okx_perp::{OkxPerpPack, okx_perp_pack};
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
    vec![
        DEFAULT_PACK_NAME,
        "binance_perp",
        "us_equity",
        "okx_perp",
        "hyperliquid_perp",
        "coinbase_spot",
        "crypto_suite",
    ]
}

/// Look up a registered [`MarketPack`] by name (returns a fresh instance so runtime
/// dynamic instrument registrations remain isolated per server instance).
pub fn get_market_pack(name: &str) -> Result<Arc<dyn MarketPack>, PackError> {
    match name {
        "crypto_perp" => Ok(Arc::new(CryptoPerpPack::new())),
        "binance_perp" => Ok(Arc::new(BinancePerpPack::new())),
        "us_equity" => Ok(Arc::new(UsEquityPack::new(AlpacaDataFeed::Iex))),
        "okx_perp" => Ok(Arc::new(OkxPerpPack::new())),
        "hyperliquid_perp" => Ok(Arc::new(HyperliquidPerpPack::new())),
        "coinbase_spot" => Ok(Arc::new(CoinbaseSpotPack::new())),
        "crypto_suite" => Ok(Arc::new(CompositeMarketPack::new())),
        other => Err(PackError::InvalidRequest(format!(
            "unknown market pack `{other}`; available: {}",
            available_pack_names().join(", ")
        ))),
    }
}
