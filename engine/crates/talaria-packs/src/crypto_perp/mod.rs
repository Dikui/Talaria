//! Crypto perpetual market pack (`crypto_perp`, Bybit V5 Linear).

pub mod connector;
pub mod stream;

use std::path::Path;
use std::str::FromStr;
use std::sync::{Arc, OnceLock, RwLock};

use rust_decimal::Decimal;
use talaria_contracts::{
    CarryTrait, DerivativeCapabilityTrait, DerivativeKind, FeedCapabilityTrait, InstrumentSpec,
    MarginTrait, OrderConstraintTrait, SessionTrait, TradingSession, TraitBundle,
};

use crate::catalog::{
    CatalogInstrument, catalog_from_specs, fetch_bybit_linear_catalog, filter_and_rank_catalog,
    try_load_fixture_catalog,
};
use crate::crypto_perp::connector::{BybitLinearConnector, DEFAULT_BASE_URL};
use crate::crypto_perp::stream::BybitPublicStream;
use crate::error::PackError;
use crate::testing::FixtureConnector;
use crate::traits::{
    BoxFuture, FeedTransport, HistoryConnector, HistoryConnectorConfig, MarketPack,
    RealtimeStreamConfig,
};

pub const MARKET_PACK_SKILL: &str = "\
# Crypto Perp Market Pack Conventions\n\
- USDT-margined 24x7 linear perpetual contracts on Bybit V5.\n\
- Supports mark_price, funding_rate (8h cadence), and open_interest derivatives.\n\
- Numeric fields use fixed-point DecimalStr without binary floats.\n";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        InstrumentSpec {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            venue: "bybit".to_owned(),
            symbol: "BTCUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.1"),
            qty_step: dec("0.001"),
            min_qty: dec("0.001"),
            price_precision: 1,
            qty_precision: 3,
        },
        InstrumentSpec {
            instrument_id: "ETH-USDT-PERP".to_owned(),
            venue: "bybit".to_owned(),
            symbol: "ETHUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.01"),
            min_qty: dec("0.01"),
            price_precision: 2,
            qty_precision: 2,
        },
        InstrumentSpec {
            instrument_id: "SOL-USDT-PERP".to_owned(),
            venue: "bybit".to_owned(),
            symbol: "SOLUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.1"),
            min_qty: dec("0.1"),
            price_precision: 2,
            qty_precision: 1,
        },
        InstrumentSpec {
            instrument_id: "1000BONK-USDT-PERP".to_owned(),
            venue: "bybit".to_owned(),
            symbol: "1000BONKUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.000001"),
            qty_step: dec("1"),
            min_qty: dec("1"),
            price_precision: 6,
            qty_precision: 0,
        },
    ]
}

fn default_traits() -> TraitBundle {
    TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USDT".to_owned(),
            max_leverage: dec("100"),
            contract_multiplier: Decimal::ONE,
        },
        carry: CarryTrait {
            funding_interval_hours: Some(8),
        },
        session: SessionTrait {
            session_kind: "24x7".to_owned(),
            timezone: Some("UTC".to_owned()),
            day_start: "00:00".to_owned(),
            sessions: vec![
                TradingSession {
                    name: "asia".to_owned(),
                    start: "00:00".to_owned(),
                    end: "08:00".to_owned(),
                    timezone: None,
                },
                TradingSession {
                    name: "london".to_owned(),
                    start: "07:00".to_owned(),
                    end: "16:00".to_owned(),
                    timezone: None,
                },
                TradingSession {
                    name: "new_york".to_owned(),
                    start: "12:00".to_owned(),
                    end: "21:00".to_owned(),
                    timezone: None,
                },
            ],
        },
        order_constraint: OrderConstraintTrait {
            post_only: true,
            max_market_qty: None,
        },
        derivatives: Some(DerivativeCapabilityTrait {
            kinds: vec![
                DerivativeKind::MarkPrice,
                DerivativeKind::FundingRate,
                DerivativeKind::OpenInterest,
            ],
            history: true,
            realtime: true,
        }),
    }
}

/// 24x7 USDT perpetual market pack powered by Bybit V5 Linear.
#[derive(Debug, Clone)]
pub struct CryptoPerpPack {
    instruments: Vec<InstrumentSpec>,
    dynamic_instruments: Arc<RwLock<Vec<InstrumentSpec>>>,
    traits: TraitBundle,
}

impl CryptoPerpPack {
    /// Construct a `CryptoPerpPack` with the default instrument catalog.
    pub fn new() -> Self {
        Self {
            instruments: default_instruments(),
            dynamic_instruments: Arc::new(RwLock::new(Vec::new())),
            traits: default_traits(),
        }
    }

    /// Construct a `CryptoPerpPack` with a custom list of instruments.
    pub fn with_instruments(instruments: Vec<InstrumentSpec>) -> Self {
        Self {
            instruments,
            dynamic_instruments: Arc::new(RwLock::new(Vec::new())),
            traits: default_traits(),
        }
    }
}

impl Default for CryptoPerpPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for CryptoPerpPack {
    fn name(&self) -> &str {
        "crypto_perp"
    }

    fn raw_archive_id(&self) -> &str {
        "bybit_linear"
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/bybit"))
    }

    fn instruments(&self) -> &[InstrumentSpec] {
        &self.instruments
    }

    fn instruments_snapshot(&self) -> Vec<InstrumentSpec> {
        let mut out = self.instruments.clone();
        if let Ok(extra) = self.dynamic_instruments.read() {
            for spec in extra.iter() {
                if !out.iter().any(|s| s.instrument_id == spec.instrument_id) {
                    out.push(spec.clone());
                }
            }
        }
        out
    }

    fn register_instrument(&self, spec: InstrumentSpec) -> Result<(), PackError> {
        if spec.venue != "bybit" {
            return Err(PackError::InvalidRequest(format!(
                "crypto_perp pack only accepts venue `bybit`, got `{}`",
                spec.venue
            )));
        }
        if self.instruments.iter().any(|s| s.instrument_id == spec.instrument_id) {
            return Ok(());
        }
        let mut extra = self.dynamic_instruments.write().unwrap();
        if !extra.iter().any(|s| s.instrument_id == spec.instrument_id) {
            extra.push(spec);
        }
        Ok(())
    }

    fn search_catalog<'a>(
        &'a self,
        query: &'a str,
        venue: Option<&'a str>,
        limit: usize,
        config: &'a HistoryConnectorConfig,
    ) -> BoxFuture<'a, Result<Vec<CatalogInstrument>, PackError>> {
        let venue_ok = venue.is_none_or(|v| v.eq_ignore_ascii_case("bybit"));
        let snapshot = self.instruments_snapshot();
        let fixture = try_load_fixture_catalog(config);
        let has_fixture_dir = config.fixture_dir.is_some();
        let base_url = config
            .base_url
            .clone()
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        let timeout = config.timeout;
        Box::pin(async move {
            if !venue_ok {
                return Ok(Vec::new());
            }
            let items = if let Some(f) = fixture {
                f
            } else if has_fixture_dir {
                catalog_from_specs(&snapshot)
            } else {
                fetch_bybit_linear_catalog(&base_url, timeout).await?
            };
            Ok(filter_and_rank_catalog(items, query, limit))
        })
    }

    fn traits(&self, instrument_id: &str) -> Result<TraitBundle, PackError> {
        if !self
            .instruments_snapshot()
            .iter()
            .any(|s| s.instrument_id == instrument_id)
        {
            return Err(PackError::InstrumentNotFound(instrument_id.to_owned()));
        }
        Ok(self.traits.clone())
    }

    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError> {
        if let Some(fixture_dir) = config.fixture_dir {
            return Ok(Arc::new(FixtureConnector::new(fixture_dir)));
        }
        let base_url = config
            .base_url
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        let connector = BybitLinearConnector::new(
            base_url,
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
        Ok(Arc::new(BybitPublicStream::new(
            &self.instruments_snapshot(),
            config,
        )))
    }
}

/// Shared singleton handle for the default `crypto_perp` market pack.
pub fn crypto_perp_pack() -> Arc<CryptoPerpPack> {
    static PACK: OnceLock<Arc<CryptoPerpPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(CryptoPerpPack::new())))
}
