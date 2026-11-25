//! Binance USD-M Futures market pack (`binance_perp`, second `crypto_perp` venue).

pub mod archive;
pub mod connector;
pub mod stream;

use std::collections::BTreeMap;
use std::path::Path;
use std::str::FromStr;
use std::sync::{Arc, OnceLock};

use rust_decimal::Decimal;
use talaria_contracts::{
    CarryTrait, DerivativeCapabilityTrait, DerivativeKind, Direction, FeedCapabilityTrait,
    InstrumentSpec, MarginEstimate, MarginTrait, OrderConstraintTrait, SessionTrait,
    TradingSession, TraitBundle,
};

use crate::binance_perp::connector::{
    BinanceFixtureConnector, BinanceFuturesConnector, DEFAULT_BASE_URL,
};
use crate::binance_perp::stream::BinanceFuturesStream;
use crate::error::PackError;
use crate::traits::{
    FeedTransport, HistoryConnector, HistoryConnectorConfig, MarginValuation, MarketPack,
    RealtimeStreamConfig,
};

pub const MARKET_PACK_SKILL: &str = "\
# Binance USD-M Perpetual Market Pack Conventions\n\
- USDT-margined 24x7 linear perpetual contracts on Binance USD-M Futures (`fapi`).\n\
- Supports mark_price, funding_rate (8h cadence), and open_interest derivatives.\n\
- Numeric fields use strict DecimalStr strings parsed to rust_decimal::Decimal at boundary.\n";

const MAINTENANCE_MARGIN_RATE_STR: &str = "0.005";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        InstrumentSpec {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            venue: "binance".to_owned(),
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
            venue: "binance".to_owned(),
            symbol: "ETHUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.001"),
            min_qty: dec("0.001"),
            price_precision: 2,
            qty_precision: 3,
        },
        InstrumentSpec {
            instrument_id: "SOL-USDT-PERP".to_owned(),
            venue: "binance".to_owned(),
            symbol: "SOLUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.01"),
            min_qty: dec("0.01"),
            price_precision: 2,
            qty_precision: 2,
        },
        InstrumentSpec {
            instrument_id: "1000PEPE-USDT-PERP".to_owned(),
            venue: "binance".to_owned(),
            symbol: "1000PEPEUSDT".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.0000001"),
            qty_step: dec("1"),
            min_qty: dec("1"),
            price_precision: 7,
            qty_precision: 0,
        },
    ]
}

fn per_instrument_risk_limits(instrument_id: &str) -> (&'static str, Option<&'static str>) {
    match instrument_id {
        "BTC-USDT-PERP" => ("125", Some("120")),
        "ETH-USDT-PERP" => ("100", Some("2000")),
        "SOL-USDT-PERP" => ("75", Some("10000")),
        "1000PEPE-USDT-PERP" => ("50", Some("5000000")),
        _ => ("100", None),
    }
}

fn traits_for_instrument(instrument_id: &str) -> TraitBundle {
    let (max_lev, max_mkt_qty) = per_instrument_risk_limits(instrument_id);
    TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USDT".to_owned(),
            max_leverage: dec(max_lev),
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
            max_market_qty: max_mkt_qty.map(dec),
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
struct BinanceMarginValuation {
    instruments: Vec<InstrumentSpec>,
}

impl MarginValuation for BinanceMarginValuation {
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
        let trait_bundle = traits_for_instrument(instrument_id);
        let units = quantity * trait_bundle.margin.contract_multiplier;
        if units <= Decimal::ZERO || entry_price <= Decimal::ZERO || isolated_margin < Decimal::ZERO {
            return Err(PackError::InvalidRequest(
                "isolated liquidation requires positive quantity, positive entry price, and non-negative margin"
                    .to_owned(),
            ));
        }
        let sign = match direction {
            Direction::Long => Decimal::ONE,
            Direction::Short => -Decimal::ONE,
        };
        let mmr = dec(MAINTENANCE_MARGIN_RATE_STR);
        let raw = (sign * entry_price - isolated_margin / units) / (sign - mmr);
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
            return Err(PackError::InvalidRequest(
                "leverage must be >= 1".to_owned(),
            ));
        }
        let mmr = dec(MAINTENANCE_MARGIN_RATE_STR);
        let mut equity = collateral;
        let mut notional = Decimal::ZERO;

        for (instrument_id, direction, quantity, entry_price) in positions {
            if !self
                .instruments
                .iter()
                .any(|s| s.instrument_id == *instrument_id)
            {
                return Err(PackError::InstrumentNotFound(instrument_id.clone()));
            }
            let bundle = traits_for_instrument(instrument_id);
            if leverage > bundle.margin.max_leverage {
                return Err(PackError::InvalidRequest(format!(
                    "leverage {leverage} exceeds max leverage {} for {instrument_id}",
                    bundle.margin.max_leverage
                )));
            }
            let mark = mark_prices.get(instrument_id).copied().ok_or_else(|| {
                PackError::InvalidRequest(format!("missing mark price for {instrument_id}"))
            })?;
            if mark <= Decimal::ZERO || *quantity <= Decimal::ZERO || *entry_price <= Decimal::ZERO {
                return Err(PackError::InvalidRequest(format!(
                    "invalid mark/quantity/entry for {instrument_id}"
                )));
            }
            let units = *quantity * bundle.margin.contract_multiplier;
            let sign = match direction {
                Direction::Long => Decimal::ONE,
                Direction::Short => -Decimal::ONE,
            };
            equity += (mark - *entry_price) * units * sign;
            notional += mark * units;
        }

        Ok(MarginEstimate {
            equity,
            initial_margin: notional / leverage,
            maintenance_margin: notional * mmr,
        })
    }
}

/// 24x7 USDT perpetual market pack powered by Binance USD-M Futures.
#[derive(Debug, Clone)]
pub struct BinancePerpPack {
    instruments: Vec<InstrumentSpec>,
}

impl BinancePerpPack {
    /// Construct a `BinancePerpPack` with the default instrument catalog.
    pub fn new() -> Self {
        Self {
            instruments: default_instruments(),
        }
    }

    /// Construct a `BinancePerpPack` with a custom list of instruments.
    pub fn with_instruments(instruments: Vec<InstrumentSpec>) -> Self {
        Self { instruments }
    }
}

impl Default for BinancePerpPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for BinancePerpPack {
    fn name(&self) -> &str {
        "binance_perp"
    }

    fn raw_archive_id(&self) -> &str {
        "binance_usdm"
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/binance"))
    }

    fn instruments(&self) -> &[InstrumentSpec] {
        &self.instruments
    }

    fn traits(&self, instrument_id: &str) -> Result<TraitBundle, PackError> {
        if self.instrument(instrument_id).is_none() {
            return Err(PackError::InstrumentNotFound(instrument_id.to_owned()));
        }
        Ok(traits_for_instrument(instrument_id))
    }

    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError> {
        if let Some(fixture_dir) = config.fixture_dir {
            return Ok(Arc::new(BinanceFixtureConnector::new(fixture_dir)));
        }
        let base_url = config
            .base_url
            .unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        let connector = BinanceFuturesConnector::new(
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
        Ok(Arc::new(BinanceFuturesStream::new(
            &self.instruments,
            config,
        )))
    }

    fn margin_valuation(&self) -> Option<Arc<dyn MarginValuation>> {
        Some(Arc::new(BinanceMarginValuation {
            instruments: self.instruments.clone(),
        }))
    }
}

/// Shared singleton handle for the `"binance_perp"` market pack.
pub fn binance_perp_pack() -> Arc<BinancePerpPack> {
    static PACK: OnceLock<Arc<BinancePerpPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(BinancePerpPack::new())))
}
