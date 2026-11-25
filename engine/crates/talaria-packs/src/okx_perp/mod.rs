//! OKX V5 linear perpetual market pack (`okx_perp`).

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

use crate::error::PackError;
use crate::okx_perp::connector::{DEFAULT_BASE_URL, OkxFixtureConnector, OkxPerpConnector};
use crate::okx_perp::stream::OkxPerpStream;
use crate::traits::{
    FeedTransport, HistoryConnector, HistoryConnectorConfig, MarginValuation, MarketPack,
    RealtimeStreamConfig,
};

pub const MARKET_PACK_SKILL: &str = "\
# OKX V5 Linear Perpetual Market Pack Conventions\n\
- Primary daily feed for Talaria (`crypto_suite` and standalone `okx_perp`).\n\
- USDT-margined 24x7 SWAP contracts (`BTC-USDT-SWAP`, `ETH-USDT-SWAP`, `SOL-USDT-SWAP`).\n\
- Uses coin units (`volCcy` / `oiCcy`) and strict fixed-point Decimal parsing without binary floats.\n";

const MAINTENANCE_MARGIN_RATE_STR: &str = "0.005";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).expect("valid static decimal literal")
}

fn default_instruments() -> Vec<InstrumentSpec> {
    vec![
        InstrumentSpec {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            venue: "okx".to_owned(),
            symbol: "BTC-USDT-SWAP".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.1"),
            qty_step: dec("0.0001"),
            min_qty: dec("0.0001"),
            price_precision: 1,
            qty_precision: 4,
        },
        InstrumentSpec {
            instrument_id: "ETH-USDT-PERP".to_owned(),
            venue: "okx".to_owned(),
            symbol: "ETH-USDT-SWAP".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.001"),
            min_qty: dec("0.001"),
            price_precision: 2,
            qty_precision: 3,
        },
        InstrumentSpec {
            instrument_id: "SOL-USDT-PERP".to_owned(),
            venue: "okx".to_owned(),
            symbol: "SOL-USDT-SWAP".to_owned(),
            asset_class: "crypto_perp".to_owned(),
            tick_size: dec("0.01"),
            qty_step: dec("0.01"),
            min_qty: dec("0.01"),
            price_precision: 2,
            qty_precision: 2,
        },
    ]
}

pub(crate) fn precision_for_okx_symbol(symbol: &str) -> (i32, i32) {
    match symbol {
        "BTC-USDT-SWAP" => (1, 4),
        "ETH-USDT-SWAP" => (2, 3),
        "SOL-USDT-SWAP" => (2, 2),
        _ => (2, 4),
    }
}

fn max_leverage_for_instrument(instrument_id: &str) -> &'static str {
    match instrument_id {
        "BTC-USDT-PERP" => "100",
        "ETH-USDT-PERP" => "75",
        "SOL-USDT-PERP" => "50",
        _ => "50",
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
            margin_asset: "USDT".to_owned(),
            max_leverage: dec(max_leverage_for_instrument(instrument_id)),
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

#[derive(Debug, Clone)]
struct OkxMarginValuation {
    instruments: Vec<InstrumentSpec>,
}

impl MarginValuation for OkxMarginValuation {
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
        let bundle = traits_for_instrument(instrument_id);
        let units = quantity * bundle.margin.contract_multiplier;
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
            return Err(PackError::InvalidRequest("leverage must be >= 1".to_owned()));
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

/// 24x7 USDT perpetual market pack powered by OKX V5 public market APIs.
#[derive(Debug, Clone)]
pub struct OkxPerpPack {
    instruments: Vec<InstrumentSpec>,
}

impl OkxPerpPack {
    pub fn new() -> Self {
        Self {
            instruments: default_instruments(),
        }
    }

    pub fn with_instruments(instruments: Vec<InstrumentSpec>) -> Self {
        Self { instruments }
    }
}

impl Default for OkxPerpPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for OkxPerpPack {
    fn name(&self) -> &str {
        "okx_perp"
    }

    fn raw_archive_id(&self) -> &str {
        "okx_swap"
    }

    fn market_skill(&self) -> &str {
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures/okx"))
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
            return Ok(Arc::new(OkxFixtureConnector::new(fixture_dir)));
        }
        let base_url = config.base_url.unwrap_or_else(|| DEFAULT_BASE_URL.to_owned());
        Ok(Arc::new(OkxPerpConnector::new(
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
        Ok(Arc::new(OkxPerpStream::new(&self.instruments, config)))
    }

    fn margin_valuation(&self) -> Option<Arc<dyn MarginValuation>> {
        Some(Arc::new(OkxMarginValuation {
            instruments: self.instruments.clone(),
        }))
    }
}

/// Shared singleton handle for `okx_perp`.
pub fn okx_perp_pack() -> Arc<OkxPerpPack> {
    static PACK: OnceLock<Arc<OkxPerpPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(OkxPerpPack::new())))
}
