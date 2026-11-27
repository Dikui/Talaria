//! Hyperliquid on-chain perpetual market pack (`hyperliquid_perp`).

pub mod connector;
pub mod stream;

use std::collections::BTreeMap;
use std::path::Path;
use std::str::FromStr;
use std::sync::{Arc, OnceLock, RwLock};

use rust_decimal::Decimal;
use talaria_contracts::{
    CarryTrait, DerivativeCapabilityTrait, DerivativeKind, Direction, FeedCapabilityTrait,
    InstrumentSpec, MarginEstimate, MarginTrait, OrderConstraintTrait, SessionTrait,
    TradingSession, TraitBundle,
};

use crate::catalog::{
    CatalogInstrument, catalog_from_specs, fetch_hyperliquid_catalog, filter_and_rank_catalog,
    try_load_fixture_catalog,
};
use crate::error::PackError;
use crate::hyperliquid_perp::connector::{
    DEFAULT_BASE_URL, HyperliquidConnector, HyperliquidFixtureConnector,
};
use crate::hyperliquid_perp::stream::HyperliquidStream;
use crate::traits::{
    BoxFuture, FeedTransport, HistoryConnector, HistoryConnectorConfig, MarginValuation,
    MarketPack, RealtimeStreamConfig,
};

pub const MARKET_PACK_SKILL: &str = "\
# Hyperliquid Perpetual Market Pack Conventions\n\
- On-chain DEX perpetual futures (`hyperliquid`), hourly funding cadence (`funding_interval_hours = 1`).\n\
- REST `candleSnapshot` retains at most the 5,000 most recent candles per (coin, interval); deep history uses archive dumps.\n\
- Uses strict `rust_decimal::Decimal` boundary parsing without binary floats.\n";

const MAINTENANCE_MARGIN_RATE_STR: &str = "0.0125";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        InstrumentSpec {
            instrument_id: "BTC-USD-HL-PERP".to_owned(),
            venue: "hyperliquid".to_owned(),
            symbol: "BTC".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.1"),
            qty_step: dec("0.00001"),
            min_qty: dec("0.00001"),
            price_precision: 1,
            qty_precision: 5,
        },
        InstrumentSpec {
            instrument_id: "ETH-USD-HL-PERP".to_owned(),
            venue: "hyperliquid".to_owned(),
            symbol: "ETH".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.0001"),
            min_qty: dec("0.0001"),
            price_precision: 2,
            qty_precision: 4,
        },
        InstrumentSpec {
            instrument_id: "SOL-USD-HL-PERP".to_owned(),
            venue: "hyperliquid".to_owned(),
            symbol: "SOL".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.01"),
            min_qty: dec("0.01"),
            price_precision: 2,
            qty_precision: 2,
        },
    ]
}

pub(crate) fn precision_for_hl_coin(coin: &str) -> (i32, i32) {
    match coin {
        "BTC" => (1, 5),
        "ETH" => (2, 4),
        "SOL" => (2, 2),
        _ => (2, 4),
    }
}

fn max_leverage_for_instrument(instrument_id: &str) -> &'static str {
    match instrument_id {
        "BTC-USD-HL-PERP" => "40",
        "ETH-USD-HL-PERP" => "25",
        "SOL-USD-HL-PERP" => "20",
        _ => "20",
    }
}

fn traits_for_instrument(instrument_id: &str) -> TraitBundle {
    TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USDC".to_owned(),
            max_leverage: dec(max_leverage_for_instrument(instrument_id)),
            contract_multiplier: Decimal::ONE,
        },
        carry: CarryTrait {
            funding_interval_hours: Some(1),
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

#[derive(Debug, Clone)]
struct HyperliquidMarginValuation {
    instruments: Vec<InstrumentSpec>,
}

impl MarginValuation for HyperliquidMarginValuation {
    fn interface_version(&self) -> u32 {
        2
    }

    fn isolated_liquidation_price(
        &self,
        instrument_id: &str,
        direction: Direction,
        quantity: Decimal,
        entry_price: Decimal,
        isolated_margin: Decimal,
        _as_of: i64,
    ) -> Result<Decimal, PackError> {
        if !self.instruments.iter().any(|s| s.instrument_id == instrument_id) {
            return Err(PackError::InstrumentNotFound(instrument_id.to_owned()));
        }
        if quantity <= Decimal::ZERO || entry_price <= Decimal::ZERO || isolated_margin < Decimal::ZERO {
            return Err(PackError::InvalidRequest(
                "isolated liquidation requires positive quantity, entry price, and non-negative margin"
                    .to_owned(),
            ));
        }
        let sign = match direction {
            Direction::Long => Decimal::ONE,
            Direction::Short => -Decimal::ONE,
        };
        let mmr = dec(MAINTENANCE_MARGIN_RATE_STR);
        let raw = (sign * entry_price - isolated_margin / quantity) / (sign - mmr);
        Ok(raw.max(Decimal::ZERO))
    }

    fn cross_maintenance_margin(
        &self,
        positions: &[(String, Direction, Decimal, Decimal)],
        mark_prices: &BTreeMap<String, Decimal>,
        leverage: Decimal,
        collateral: Decimal,
    ) -> Result<MarginEstimate, PackError> {
        if leverage < Decimal::ONE {
            return Err(PackError::InvalidRequest("leverage must be >= 1".to_owned()));
        }
        let mmr = dec(MAINTENANCE_MARGIN_RATE_STR);
        let mut equity = collateral;
        let mut notional = Decimal::ZERO;
        for (instrument_id, direction, quantity, entry_price) in positions {
            let bundle = if self.instruments.iter().any(|s| s.instrument_id == *instrument_id) {
                traits_for_instrument(instrument_id)
            } else {
                return Err(PackError::InstrumentNotFound(instrument_id.clone()));
            };
            if leverage > bundle.margin.max_leverage {
                return Err(PackError::InvalidRequest(format!(
                    "leverage {leverage} exceeds max leverage {}",
                    bundle.margin.max_leverage
                )));
            }
            let mark = mark_prices.get(instrument_id).copied().ok_or_else(|| {
                PackError::InvalidRequest(format!("missing mark price for {instrument_id}"))
            })?;
            let sign = match direction {
                Direction::Long => Decimal::ONE,
                Direction::Short => -Decimal::ONE,
            };
            equity += (mark - *entry_price) * *quantity * sign;
            notional += mark * *quantity;
        }
        Ok(MarginEstimate {
            equity,
            initial_margin: notional / leverage,
            maintenance_margin: notional * mmr,
        })
    }
}

/// Hyperliquid on-chain perpetual market pack.
#[derive(Debug, Clone)]
pub struct HyperliquidPerpPack {
    instruments: Vec<InstrumentSpec>,
    dynamic_instruments: Arc<RwLock<Vec<InstrumentSpec>>>,
}

impl HyperliquidPerpPack {
    pub fn new() -> Self {
        Self {
            instruments: default_instruments(),
            dynamic_instruments: Arc::new(RwLock::new(Vec::new())),
        }
    }

    pub fn with_instruments(instruments: Vec<InstrumentSpec>) -> Self {
        Self {
            instruments,
            dynamic_instruments: Arc::new(RwLock::new(Vec::new())),
        }
    }
}

impl Default for HyperliquidPerpPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for HyperliquidPerpPack {
    fn name(&self) -> &str {
        "hyperliquid_perp"
    }

    fn raw_archive_id(&self) -> &str {
        "hyperliquid_perp"
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/hyperliquid"))
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
        if spec.venue != "hyperliquid" {
            return Err(PackError::InvalidRequest(format!(
                "hyperliquid_perp pack only accepts venue `hyperliquid`, got `{}`",
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
        let venue_ok = venue.is_none_or(|v| v.eq_ignore_ascii_case("hyperliquid"));
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
                fetch_hyperliquid_catalog(&base_url, timeout).await?
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
        Ok(traits_for_instrument(instrument_id))
    }

    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError> {
        if let Some(fixture_dir) = config.fixture_dir {
            return Ok(Arc::new(HyperliquidFixtureConnector::new(fixture_dir)));
        }
        let base_url = config.base_url.unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        Ok(Arc::new(HyperliquidConnector::new(
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
        Ok(Arc::new(HyperliquidStream::new(
            &self.instruments_snapshot(),
            config,
        )))
    }

    fn margin_valuation(&self) -> Option<Arc<dyn MarginValuation>> {
        Some(Arc::new(HyperliquidMarginValuation {
            instruments: self.instruments.clone(),
        }))
    }
}

/// Shared singleton handle for `hyperliquid_perp`.
pub fn hyperliquid_perp_pack() -> Arc<HyperliquidPerpPack> {
    static PACK: OnceLock<Arc<HyperliquidPerpPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(HyperliquidPerpPack::new())))
}
