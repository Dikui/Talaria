//! Composite multi-venue market pack (`crypto_suite`):
//! Combines `okx_perp` (primary daily swaps) + `hyperliquid_perp` (on-chain sentiment)
//! + `coinbase_spot` (US spot reference) behind the single `MarketPack` trait.

use std::collections::{BTreeMap, BTreeSet};
use std::path::Path;
use std::sync::{Arc, Mutex as StdMutex, OnceLock};

use rust_decimal::Decimal;
use talaria_contracts::{
    DerivativeKind, Direction, InstrumentSpec, MarginEstimate, RawBar, RawDerivativePoint,
    TraitBundle,
};
use tokio::sync::{Mutex, mpsc};
use tokio::task::JoinHandle;

use crate::coinbase_spot::{CoinbaseSpotPack, coinbase_spot_pack};
use crate::error::PackError;
use crate::hyperliquid_perp::{HyperliquidPerpPack, hyperliquid_perp_pack};
use crate::okx_perp::{OkxPerpPack, okx_perp_pack};
use crate::traits::{
    BoxFuture, FeedTransport, HistoryConnector, HistoryConnectorConfig, MarginValuation,
    MarketPack, RealtimeStreamConfig, TransportFrame,
};

pub const MARKET_PACK_SKILL: &str = "\
# Talaria Multi-Venue Crypto Suite (`crypto_suite`)\n\
- Combines OKX V5 linear swaps (primary daily feed), Hyperliquid perpetuals (1h funding on-chain reference), and Coinbase spot (US Coinbase Premium anchor).\n\
- Feeds multiplex transparently through `MarketPack` with zero exchange conditionals in `talaria-feed` or `talaria-server`.\n";

#[derive(Clone, Copy)]
enum VenueSlot {
    Okx = 0,
    Hyperliquid = 1,
    Coinbase = 2,
}

impl VenueSlot {
    fn from_venue(venue: &str) -> Result<Self, PackError> {
        match venue {
            "okx" => Ok(Self::Okx),
            "hyperliquid" => Ok(Self::Hyperliquid),
            "coinbase" => Ok(Self::Coinbase),
            other => Err(PackError::InstrumentNotFound(format!(
                "unsupported venue `{other}` in crypto_suite"
            ))),
        }
    }
}

/// Composite `HistoryConnector` routing `symbol` lookups to the owning venue connector.
pub struct CompositeHistoryConnector {
    slot_by_symbol: BTreeMap<String, VenueSlot>,
    okx: Arc<dyn HistoryConnector>,
    hyperliquid: Arc<dyn HistoryConnector>,
    coinbase: Arc<dyn HistoryConnector>,
}

impl CompositeHistoryConnector {
    fn connector_for(&self, symbol: &str) -> Result<&Arc<dyn HistoryConnector>, PackError> {
        match self.slot_by_symbol.get(symbol).copied() {
            Some(VenueSlot::Okx) => Ok(&self.okx),
            Some(VenueSlot::Hyperliquid) => Ok(&self.hyperliquid),
            Some(VenueSlot::Coinbase) => Ok(&self.coinbase),
            None => Err(PackError::InstrumentNotFound(format!(
                "unknown symbol `{symbol}` in crypto_suite"
            ))),
        }
    }
}

impl HistoryConnector for CompositeHistoryConnector {
    fn feed_mode(&self) -> &str {
        "crypto_suite"
    }

    fn fetch_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawBar>, PackError>> {
        Box::pin(async move {
            self.connector_for(symbol)?
                .fetch_bars(symbol, timeframe, end_ts, limit)
                .await
        })
    }

    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            self.connector_for(symbol)?
                .fetch_mark_price_bars(symbol, timeframe, end_ts, limit)
                .await
        })
    }

    fn fetch_funding_settlements<'a>(
        &'a self,
        symbol: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            self.connector_for(symbol)?
                .fetch_funding_settlements(symbol, end_ts, limit)
                .await
        })
    }

    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        Box::pin(async move {
            self.connector_for(symbol)?
                .fetch_open_interest(symbol, timeframe, end_ts, limit)
                .await
        })
    }
}

struct ChildSlot {
    transport: Arc<dyn FeedTransport>,
    connected: bool,
    pump: Option<JoinHandle<()>>,
}

struct CompositeState {
    closed: bool,
    started: bool,
    slots: [ChildSlot; 3],
    subscriptions: BTreeMap<(String, String), VenueSlot>,
}

impl Drop for CompositeState {
    fn drop(&mut self) {
        for slot in &mut self.slots {
            if let Some(h) = slot.pump.take() {
                h.abort();
            }
        }
    }
}

/// Composite `FeedTransport` multiplexing subscribed channels across OKX, Hyperliquid & Coinbase WS.
pub struct CompositeFeedTransport {
    slot_by_instrument: BTreeMap<String, VenueSlot>,
    state: Mutex<CompositeState>,
    events_tx: mpsc::UnboundedSender<Result<Option<TransportFrame>, PackError>>,
    events_rx: Mutex<mpsc::UnboundedReceiver<Result<Option<TransportFrame>, PackError>>>,
}

impl CompositeFeedTransport {
    fn spawn_pump(
        transport: Arc<dyn FeedTransport>,
        events_tx: mpsc::UnboundedSender<Result<Option<TransportFrame>, PackError>>,
    ) -> JoinHandle<()> {
        tokio::spawn(async move {
            loop {
                match transport.recv().await {
                    Ok(Some(frame)) => {
                        if events_tx.send(Ok(Some(frame))).is_err() {
                            break;
                        }
                    }
                    Ok(None) => {}
                    Err(err) => {
                        let _ = events_tx.send(Err(err));
                        break;
                    }
                }
            }
        })
    }

    async fn ensure_slot_connected(&self, slot_idx: usize) -> Result<(), PackError> {
        let transport = {
            let guard = self.state.lock().await;
            if guard.slots[slot_idx].connected {
                return Ok(());
            }
            Arc::clone(&guard.slots[slot_idx].transport)
        };
        transport.connect().await?;
        let pump = Self::spawn_pump(Arc::clone(&transport), self.events_tx.clone());
        let mut guard = self.state.lock().await;
        if let Some(old) = guard.slots[slot_idx].pump.take() {
            old.abort();
        }
        guard.slots[slot_idx].connected = true;
        guard.slots[slot_idx].pump = Some(pump);
        Ok(())
    }
}

impl FeedTransport for CompositeFeedTransport {
    fn feed_mode(&self) -> &str {
        "crypto_suite"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            {
                let mut guard = self.state.lock().await;
                if guard.closed {
                    return Err(PackError::FeedTransport("composite transport closed".to_owned()));
                }
                guard.started = true;
                for slot in &mut guard.slots {
                    if let Some(h) = slot.pump.take() {
                        h.abort();
                    }
                    slot.connected = false;
                }
            }
            {
                let mut rx = self.events_rx.lock().await;
                while rx.try_recv().is_ok() {}
            }

            let active_slots: BTreeSet<usize> = {
                let guard = self.state.lock().await;
                if guard.subscriptions.is_empty() {
                    // Connect primary OKX transport proactively even before subscriptions arrive.
                    [VenueSlot::Okx as usize].into_iter().collect()
                } else {
                    guard.subscriptions.values().map(|s| *s as usize).collect()
                }
            };

            for idx in active_slots {
                self.ensure_slot_connected(idx).await?;
            }
            Ok(())
        })
    }

    fn subscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            let slot = self
                .slot_by_instrument
                .get(instrument_id)
                .copied()
                .ok_or_else(|| PackError::InstrumentNotFound(instrument_id.to_owned()))?;
            let idx = slot as usize;
            let (started, transport) = {
                let mut guard = self.state.lock().await;
                guard
                    .subscriptions
                    .insert((instrument_id.to_owned(), timeframe.to_owned()), slot);
                (guard.started, Arc::clone(&guard.slots[idx].transport))
            };
            if started {
                self.ensure_slot_connected(idx).await?;
            }
            transport.subscribe(instrument_id, timeframe).await
        })
    }

    fn unsubscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            let Some(slot) = self.slot_by_instrument.get(instrument_id).copied() else {
                return Ok(());
            };
            let transport = {
                let mut guard = self.state.lock().await;
                guard
                    .subscriptions
                    .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
                Arc::clone(&guard.slots[slot as usize].transport)
            };
            transport.unsubscribe(instrument_id, timeframe).await
        })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            let mut rx = self.events_rx.lock().await;
            match rx.recv().await {
                Some(res) => res,
                None => Err(PackError::FeedTransport(
                    "composite transport closed".to_owned(),
                )),
            }
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut guard = self.state.lock().await;
            guard.closed = true;
            for slot in &mut guard.slots {
                if let Some(h) = slot.pump.take() {
                    h.abort();
                }
                let t = Arc::clone(&slot.transport);
                let _ = t.close().await;
                slot.connected = false;
            }
            Ok(())
        })
    }
}

struct CompositeMarginValuation {
    slot_by_instrument: BTreeMap<String, VenueSlot>,
    okx: Option<Arc<dyn MarginValuation>>,
    hyperliquid: Option<Arc<dyn MarginValuation>>,
}

impl MarginValuation for CompositeMarginValuation {
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
        as_of: i64,
    ) -> Result<Decimal, PackError> {
        let mv = match self.slot_by_instrument.get(instrument_id).copied() {
            Some(VenueSlot::Okx) => self.okx.as_ref(),
            Some(VenueSlot::Hyperliquid) => self.hyperliquid.as_ref(),
            _ => None,
        }
        .ok_or_else(|| {
            PackError::InvalidRequest(format!("margin valuation unavailable for `{instrument_id}`"))
        })?;
        mv.isolated_liquidation_price(
            instrument_id,
            direction,
            quantity,
            entry_price,
            isolated_margin,
            as_of,
        )
    }

    fn cross_maintenance_margin(
        &self,
        positions: &[(String, Direction, Decimal, Decimal)],
        mark_prices: &BTreeMap<String, Decimal>,
        leverage: Decimal,
        collateral: Decimal,
    ) -> Result<MarginEstimate, PackError> {
        let Some(first) = positions.first() else {
            return Ok(MarginEstimate {
                equity: collateral,
                initial_margin: Decimal::ZERO,
                maintenance_margin: Decimal::ZERO,
            });
        };
        let mv = match self.slot_by_instrument.get(&first.0).copied() {
            Some(VenueSlot::Okx) => self.okx.as_ref(),
            Some(VenueSlot::Hyperliquid) => self.hyperliquid.as_ref(),
            _ => None,
        }
        .ok_or_else(|| {
            PackError::InvalidRequest(format!("margin valuation unavailable for `{}`", first.0))
        })?;
        mv.cross_maintenance_margin(positions, mark_prices, leverage, collateral)
    }
}

/// Multi-venue `crypto_suite` combining OKX + Hyperliquid + Coinbase.
#[derive(Clone)]
pub struct CompositeMarketPack {
    okx: Arc<OkxPerpPack>,
    hyperliquid: Arc<HyperliquidPerpPack>,
    coinbase: Arc<CoinbaseSpotPack>,
    instruments: Vec<InstrumentSpec>,
    slot_by_instrument: BTreeMap<String, VenueSlot>,
    slot_by_symbol: BTreeMap<String, VenueSlot>,
    fixture_override: Arc<StdMutex<Option<String>>>,
}

impl CompositeMarketPack {
    pub fn new() -> Self {
        let okx = okx_perp_pack();
        let hyperliquid = hyperliquid_perp_pack();
        let coinbase = coinbase_spot_pack();

        let mut instruments = Vec::new();
        let mut slot_by_instrument = BTreeMap::new();
        let mut slot_by_symbol = BTreeMap::new();

        for spec in okx
            .instruments()
            .iter()
            .chain(hyperliquid.instruments().iter())
            .chain(coinbase.instruments().iter())
        {
            let slot = VenueSlot::from_venue(&spec.venue).expect("known composite venue");
            slot_by_instrument.insert(spec.instrument_id.clone(), slot);
            slot_by_symbol.insert(spec.symbol.clone(), slot);
            instruments.push(spec.clone());
        }

        let _ = (DerivativeKind::MarkPrice,);
        Self {
            okx,
            hyperliquid,
            coinbase,
            instruments,
            slot_by_instrument,
            slot_by_symbol,
            fixture_override: Arc::new(StdMutex::new(None)),
        }
    }

    fn subpack_for_slot(&self, slot: VenueSlot) -> &dyn MarketPack {
        match slot {
            VenueSlot::Okx => self.okx.as_ref(),
            VenueSlot::Hyperliquid => self.hyperliquid.as_ref(),
            VenueSlot::Coinbase => self.coinbase.as_ref(),
        }
    }
}

impl Default for CompositeMarketPack {
    fn default() -> Self {
        Self::new()
    }
}

impl MarketPack for CompositeMarketPack {
    fn name(&self) -> &str {
        "crypto_suite"
    }

    fn raw_archive_id(&self) -> &str {
        "crypto_suite"
    }

    fn market_skill(&self) -> &str {
        let _ = &self.fixture_override;
        MARKET_PACK_SKILL
    }

    fn default_fixture_dir(&self) -> Option<&Path> {
        Some(Path::new("tests/fixtures"))
    }

    fn instruments(&self) -> &[InstrumentSpec] {
        &self.instruments
    }

    fn traits(&self, instrument_id: &str) -> Result<TraitBundle, PackError> {
        let slot = self
            .slot_by_instrument
            .get(instrument_id)
            .copied()
            .ok_or_else(|| PackError::InstrumentNotFound(instrument_id.to_owned()))?;
        self.subpack_for_slot(slot).traits(instrument_id)
    }

    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError> {
        let okx_cfg = HistoryConnectorConfig {
            fixture_dir: config.fixture_dir.as_ref().map(|d| d.join("okx")),
            ..config.clone()
        };
        let hl_cfg = HistoryConnectorConfig {
            fixture_dir: config.fixture_dir.as_ref().map(|d| d.join("hyperliquid")),
            ..config.clone()
        };
        let cb_cfg = HistoryConnectorConfig {
            fixture_dir: config.fixture_dir.as_ref().map(|d| d.join("coinbase")),
            ..config
        };

        Ok(Arc::new(CompositeHistoryConnector {
            slot_by_symbol: self.slot_by_symbol.clone(),
            okx: self.okx.create_history_connector(okx_cfg)?,
            hyperliquid: self.hyperliquid.create_history_connector(hl_cfg)?,
            coinbase: self.coinbase.create_history_connector(cb_cfg)?,
        }))
    }

    fn create_realtime_stream(
        &self,
        config: RealtimeStreamConfig,
    ) -> Result<Arc<dyn FeedTransport>, PackError> {
        let okx_t = self.okx.create_realtime_stream(config.clone())?;
        let hl_t = self.hyperliquid.create_realtime_stream(config.clone())?;
        let cb_t = self.coinbase.create_realtime_stream(config)?;
        let (events_tx, events_rx) = mpsc::unbounded_channel();

        Ok(Arc::new(CompositeFeedTransport {
            slot_by_instrument: self.slot_by_instrument.clone(),
            state: Mutex::new(CompositeState {
                closed: false,
                started: false,
                slots: [
                    ChildSlot {
                        transport: okx_t,
                        connected: false,
                        pump: None,
                    },
                    ChildSlot {
                        transport: hl_t,
                        connected: false,
                        pump: None,
                    },
                    ChildSlot {
                        transport: cb_t,
                        connected: false,
                        pump: None,
                    },
                ],
                subscriptions: BTreeMap::new(),
            }),
            events_tx,
            events_rx: Mutex::new(events_rx),
        }))
    }

    fn margin_valuation(&self) -> Option<Arc<dyn MarginValuation>> {
        Some(Arc::new(CompositeMarginValuation {
            slot_by_instrument: self.slot_by_instrument.clone(),
            okx: self.okx.margin_valuation(),
            hyperliquid: self.hyperliquid.margin_valuation(),
        }))
    }
}

/// Shared singleton handle for `crypto_suite`.
pub fn crypto_suite_pack() -> Arc<CompositeMarketPack> {
    static PACK: OnceLock<Arc<CompositeMarketPack>> = OnceLock::new();
    Arc::clone(PACK.get_or_init(|| Arc::new(CompositeMarketPack::new())))
}
