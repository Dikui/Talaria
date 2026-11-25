//! Coinbase Exchange spot market pack (`coinbase_spot`, US spot pricing reference).

pub mod connector;
pub mod stream;

use std::path::Path;
use std::str::FromStr;
use std::sync::{Arc, OnceLock};

use rust_decimal::Decimal;
use talaria_contracts::{
    CarryTrait, FeedCapabilityTrait, InstrumentSpec, MarginTrait, OrderConstraintTrait,
    SessionTrait, TradingSession, TraitBundle,
};

use crate::coinbase_spot::connector::{
    CoinbaseFixtureConnector, CoinbaseSpotConnector, DEFAULT_BASE_URL,
};
use crate::coinbase_spot::stream::CoinbaseSpotStream;
use crate::error::PackError;
use crate::traits::{
    FeedTransport, HistoryConnector, HistoryConnectorConfig, MarketPack, RealtimeStreamConfig,
};

pub const MARKET_PACK_SKILL: &str = "\
# Coinbase Exchange Spot Market Pack Conventions\n\
- US-regulated spot pricing reference (`BTC-USD`, `ETH-USD`, `SOL-USD`) for spot-vs-perp Coinbase Premium analytics.\n\
- No leverage or derivatives (`max_leverage = 1`, `derivatives = None`).\n\
- Parses Coinbase REST `[time, low, high, open, close, volume]` and WS `ticker` + `matches` strictly into Decimal without binary floats.\n";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        InstrumentSpec {
            instrument_id: "BTC-USD-CB-SPOT".to_owned(),
            venue: "coinbase".to_owned(),
            symbol: "BTC-USD".to_owned(),
            asset_class: "crypto_spot".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.000001"),
            min_qty: dec("0.000001"),
            price_precision: 2,
            qty_precision: 6,
        },
        InstrumentSpec {
            instrument_id: "ETH-USD-CB-SPOT".to_owned(),
            venue: "coinbase".to_owned(),
            symbol: "ETH-USD".to_owned(),
            asset_class: "crypto_spot".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.000001"),
            min_qty: dec("0.000001"),
            price_precision: 2,
            qty_precision: 6,
        },
        InstrumentSpec {
            instrument_id: "SOL-USD-CB-SPOT".to_owned(),
            venue: "coinbase".to_owned(),
            symbol: "SOL-USD".to_owned(),
            asset_class: "crypto_spot".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.0001"),
            min_qty: dec("0.0001"),
            price_precision: 2,
            qty_precision: 4,
        },
    ]
}

pub(crate) fn precision_for_coinbase_product(symbol: &str) -> (i32, i32) {
    match symbol {
        "SOL-USD" => (2, 4),
        _ => (2, 6),
    }
}

fn default_traits() -> TraitBundle {
    TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USD".to_owned(),
            max_leverage: Decimal::ONE,
            contract_multiplier: Decimal::ONE,
        },
        carry: CarryTrait {
            funding_interval_hours: None,
        },
        session: SessionTrait {
            session_kind: "24x7".to_owned(),
            timezone: Some("UTC".to_owned()),
            day_start: "00:00".to_owned(),
            sessions: vec![TradingSession {
                name: "continuous".to_owned(),
                start: "00:00".to_owned(),
                end: "23:59".to_owned(),
                timezone: Some("UTC".to_owned()),
            }],
        },
        order_constraint: OrderConstraintTrait {
            post_only: true,
            max_market_qty: None,
        },
        derivatives: None,
    }
}

/// US spot market pack powered by Coinbase Exchange public APIs.
#[derive(Debug, Clone)]
pub struct CoinbaseSpotPack {
    instruments: Vec<InstrumentSpec>,
    traits: TraitBundle,
}

impl CoinbaseSpotPack {
    pub fn new() -> Self {
        Self {
            instruments: default_instruments(),
            traits: default_traits(),
        }
    }

    pub fn with_instruments(instruments: Vec<InstrumentSpec>) -> Self {
        Self {
            instruments,
            traits: default_traits(),
        }
    }
}

impl Default for CoinbaseSpotPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for CoinbaseSpotPack {
    fn name(&self) -> &str {
        "coinbase_spot"
    }

    fn raw_archive_id(&self) -> &str {
        "coinbase_spot"
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/coinbase"))
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
            return Ok(Arc::new(CoinbaseFixtureConnector::new(fixture_dir)));
        }
        let base_url = config.base_url.unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        Ok(Arc::new(CoinbaseSpotConnector::new(
            base_url,
            config.timeout,
            config.retries,
            config.now_ms,
        )?))
    }

    fn create_realtime_stream(
        &self,
        config: RealtimeStreamConfig,
    ) -> Result<Arc<dyn FeedTransport>, PackError> {
        Ok(Arc::new(CoinbaseSpotStream::new(&self.instruments, config)))
    }
}

/// Shared singleton handle for `coinbase_spot`.
pub fn coinbase_spot_pack() -> Arc<CoinbaseSpotPack> {
    static PACK: OnceLock<Arc<CoinbaseSpotPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(CoinbaseSpotPack::new())))
}
