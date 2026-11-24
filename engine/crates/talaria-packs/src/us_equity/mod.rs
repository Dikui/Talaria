//! US Equity market pack (`us_equity`, Alpaca Market Data v2 with strict IEX vs SIP separation).

pub mod calendar;
pub mod connector;
pub mod session;
pub mod stream;

use std::path::Path;
use std::str::FromStr;
use std::sync::{Arc, OnceLock};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use talaria_contracts::{
    CarryTrait, FeedCapabilityTrait, InstrumentSpec, MarginTrait, OrderConstraintTrait,
    TraitBundle,
};

use crate::error::PackError;
use crate::traits::{
    FeedTransport, HistoryConnector, HistoryConnectorConfig, MarketPack, RealtimeStreamConfig,
};
use crate::us_equity::connector::{AlpacaFixtureConnector, AlpacaHistoryConnector, DEFAULT_BASE_URL};
use crate::us_equity::session::session_trait_for_feed;
use crate::us_equity::stream::{AlpacaStream, IEX_WS_URL, SIP_WS_URL};

pub use calendar::{
    EquityCalendar, EquityCalendarEvent, EquityCalendarObservation, EquityEventKind,
    EquityEventStatus,
};
pub use session::{
    EtDate, UsMarketSession, classify_us_equity_session, et_to_utc_ms, et_utc_offset_secs,
    is_us_market_holiday, is_us_trading_day, is_within_feed_hours, utc_ms_to_et,
};

pub const MARKET_PACK_SKILL: &str = "\
# US Equity Market Pack Conventions (Alpaca IEX / SIP)\n\
- Cash & Reg-T US equities traded on NYSE/NASDAQ in America/New_York.\n\
- Strictly separates AlpacaDataFeed::Iex (08:00-17:00 ET intraday) from AlpacaDataFeed::Sip (04:00-20:00 ET consolidated); silent fallback between feeds is forbidden.\n\
- Corporate actions and earnings events enforce anti-lookahead ts_available <= as_of.\n";

/// Explicit Alpaca Market Data feed tier; silent fallback between `Iex` and `Sip` is forbidden.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum AlpacaDataFeed {
    /// Free IEX exchange feed (`08:00-17:00 America/New_York` intraday extended + regular hours).
    Iex,
    /// Consolidated SIP market data feed (`04:00-20:00 America/New_York` full history & stream).
    Sip,
}

impl AlpacaDataFeed {
    /// Alpaca REST `feed` query parameter and fixture subdirectory name (`"iex"` or `"sip"`).
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Iex => "iex",
            Self::Sip => "sip",
        }
    }

    /// Explicit `feed_mode` tag returned by Alpaca connectors and streams.
    pub const fn feed_mode(self) -> &'static str {
        match self {
            Self::Iex => "alpaca_iex",
            Self::Sip => "alpaca_sip",
        }
    }

    /// Default Alpaca Market Data v2 WebSocket URL for this feed.
    pub const fn default_ws_url(self) -> &'static str {
        match self {
            Self::Iex => IEX_WS_URL,
            Self::Sip => SIP_WS_URL,
        }
    }
}

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn equity_spec(symbol: &str) -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: symbol.to_owned(),
        venue: "alpaca".to_owned(),
        symbol: symbol.to_owned(),
        asset_class: "us_equity".to_owned(),
        tick_size: dec("0.01"),
        qty_step: dec("1"),
        min_qty: dec("1"),
        price_precision: 2,
        qty_precision: 0,
    }
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        equity_spec("AAPL"),
        equity_spec("MSFT"),
        equity_spec("NVDA"),
        equity_spec("SPY"),
    ]
}

fn traits_for_feed(feed: AlpacaDataFeed) -> TraitBundle {
    TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USD".to_owned(),
            max_leverage: dec("2"),
            contract_multiplier: Decimal::ONE,
        },
        carry: CarryTrait {
            funding_interval_hours: None,
        },
        session: session_trait_for_feed(feed),
        order_constraint: OrderConstraintTrait {
            post_only: false,
            max_market_qty: None,
        },
        derivatives: None,
    }
}

/// US Equity market pack powered by Alpaca Market Data v2 with strict [`AlpacaDataFeed`] isolation.
#[derive(Debug, Clone)]
pub struct UsEquityPack {
    feed: AlpacaDataFeed,
    instruments: Vec<InstrumentSpec>,
    traits: TraitBundle,
}

impl UsEquityPack {
    /// Construct a `UsEquityPack` explicitly bound to `feed` (`Iex` or `Sip`).
    pub fn new(feed: AlpacaDataFeed) -> Self {
        Self {
            feed,
            instruments: default_instruments(),
            traits: traits_for_feed(feed),
        }
    }

    /// Construct a `UsEquityPack` bound to `feed` with a custom instrument list.
    pub fn with_instruments(feed: AlpacaDataFeed, instruments: Vec<InstrumentSpec>) -> Self {
        Self {
            feed,
            instruments,
            traits: traits_for_feed(feed),
        }
    }

    /// Return the explicit [`AlpacaDataFeed`] configured on this pack.
    pub fn feed(&self) -> AlpacaDataFeed {
        self.feed
    }
}

impl Default for UsEquityPack {
    /// Defaults to free [`AlpacaDataFeed::Iex`]; use [`UsEquityPack::new`] for [`AlpacaDataFeed::Sip`].
    fn default() -> Self {
        Self::new(AlpacaDataFeed::Iex)
    }
}

impl MarketPack for UsEquityPack {
    fn name(&self) -> &str {
        "us_equity"
    }

    fn raw_archive_id(&self) -> &str {
        match self.feed {
            AlpacaDataFeed::Iex => "alpaca_iex",
            AlpacaDataFeed::Sip => "alpaca_sip",
        }
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/alpaca"))
    }

    fn instruments(&self) -> &[InstrumentSpec] {
        &self.instruments
    }

    fn traits(&self, instrument_id: &str) -> Result<TraitBundle, PackError> {
        if self.instrument(instrument_id).is_none() {
            return Err(PackError::InstrumentNotFound(instrument_id.to_owned()));
        }
        Ok(self.traits.clone())
    }

    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError> {
        if let Some(fixture_dir) = config.fixture_dir {
            return Ok(Arc::new(AlpacaFixtureConnector::new(
                fixture_dir,
                self.feed,
            )));
        }
        let base_url = config
            .base_url
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        let connector = AlpacaHistoryConnector::new(
            base_url,
            self.feed,
            config.timeout,
            config.retries,
            config.now_ms,
        )?;
        Ok(Arc::new(connector))
    }

    fn create_realtime_stream(
        &self,
        config: RealtimeStreamConfig,
    ) -> Result<Arc<dyn FeedTransport>, PackError> {
        Ok(Arc::new(AlpacaStream::new(
            &self.instruments,
            self.feed,
            config,
        )?))
    }
}

/// Shared singleton handle for the default `"us_equity"` pack (`AlpacaDataFeed::Iex`).
pub fn us_equity_pack() -> Arc<UsEquityPack> {
    static PACK: OnceLock<Arc<UsEquityPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(UsEquityPack::new(AlpacaDataFeed::Iex))))
}

/// Shared singleton handle for the `"us_equity"` pack configured explicitly for [`AlpacaDataFeed::Sip`].
pub fn us_equity_sip_pack() -> Arc<UsEquityPack> {
    static PACK: OnceLock<Arc<UsEquityPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(UsEquityPack::new(AlpacaDataFeed::Sip))))
}
