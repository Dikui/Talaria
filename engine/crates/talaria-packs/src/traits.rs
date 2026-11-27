//! Asset-agnostic `MarketPack`, `HistoryConnector`, `FeedTransport`, and supporting traits.

use std::collections::BTreeMap;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::pin::Pin;
use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use talaria_contracts::{
    DerivativeKind, DerivativeTickEvent, Direction, InstrumentSpec, MarginEstimate, OrderBookEvent,
    RawBar, RawDerivativePoint, ReceivedKlineEvent, ReceivedOrderBookEvent, Trade, TradeEvent,
    TraitBundle, TransportEvent,
};

use crate::catalog::{CatalogInstrument, catalog_from_specs, filter_and_rank_catalog};
use crate::error::PackError;
use crate::uuid_util::trade_to_contract;

/// Boxed `Send` future alias for object-safe async traits without external macro crates.
pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// Shared UTC millisecond wall/virtual clock function.
pub type ClockFn = Arc<dyn Fn() -> i64 + Send + Sync>;

/// Default system UTC millisecond clock.
pub fn system_now_ms() -> i64 {
    chrono::Utc::now().timestamp_millis()
}

/// Construct a [`ClockFn`] backed by the system UTC clock.
pub fn default_clock() -> ClockFn {
    Arc::new(system_now_ms)
}

/// Configuration for constructing a [`HistoryConnector`].
#[derive(Clone)]
pub struct HistoryConnectorConfig {
    /// Optional REST base URL override (`--history-base-url`).
    pub base_url: Option<String>,
    /// Optional local fixture directory (`--fixture-dir`) for offline deterministic mode.
    pub fixture_dir: Option<PathBuf>,
    /// Injectable UTC millisecond clock.
    pub now_ms: ClockFn,
    /// HTTP request timeout.
    pub timeout: Duration,
    /// Number of transient request retries with exponential backoff.
    pub retries: usize,
}

impl Default for HistoryConnectorConfig {
    fn default() -> Self {
        Self {
            base_url: None,
            fixture_dir: None,
            now_ms: default_clock(),
            timeout: Duration::from_secs(10),
            retries: 2,
        }
    }
}

/// Configuration for constructing a real-time [`FeedTransport`].
#[derive(Clone)]
pub struct RealtimeStreamConfig {
    /// Optional WebSocket URL override.
    pub ws_url: Option<String>,
    /// Injectable UTC millisecond clock used at frame receipt time.
    pub now_ms: ClockFn,
    /// Toggle for subscribing to and persisting `publicTrade.{symbol}` ticks.
    pub persist_public_trades: bool,
    /// Timeout for WS connect handshake and subscription ack.
    pub ack_timeout: Duration,
    /// Application-level heartbeat ping interval (`None` disables periodic pings).
    pub ping_interval: Option<Duration>,
    /// Grace window beyond `ping_interval` before closing an unresponsive connection.
    pub ping_timeout: Duration,
    /// Initial exponential backoff delay used by reconnect loops.
    pub backoff_initial: Duration,
    /// Maximum exponential backoff delay used by reconnect loops.
    pub backoff_max: Duration,
}

impl Default for RealtimeStreamConfig {
    fn default() -> Self {
        Self {
            ws_url: None,
            now_ms: default_clock(),
            persist_public_trades: true,
            ack_timeout: Duration::from_secs(5),
            ping_interval: Some(Duration::from_secs(20)),
            ping_timeout: Duration::from_secs(10),
            backoff_initial: Duration::from_millis(200),
            backoff_max: Duration::from_secs(5),
        }
    }
}

/// Transport-layer trade tick carrying packet/sequence provenance alongside `TradeEvent`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReceivedTradeEvent {
    pub event: TradeEvent,
    pub ts_published: Option<i64>,
    pub sequence: Option<i64>,
    pub packet_id: Option<String>,
    pub packet_index: Option<i64>,
}

impl ReceivedTradeEvent {
    /// Materialize a contract `Trade` record using `ts_committed`.
    pub fn to_trade(&self, ts_committed: i64) -> Result<Trade, PackError> {
        trade_to_contract(self, ts_committed)
    }
}

/// Frame returned by [`FeedTransport::recv`], preserving parser receipt and connection metadata
/// while remaining trivially convertible to [`TransportEvent`].
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum TransportFrame {
    Event(TransportEvent),
    ReceivedKline(ReceivedKlineEvent),
    ReceivedOrderBook(ReceivedOrderBookEvent),
    ReceivedTrade(ReceivedTradeEvent),
}

impl TransportFrame {
    /// Strip transport-only metadata and return the canonical L0 [`TransportEvent`].
    pub fn into_transport_event(self) -> TransportEvent {
        match self {
            Self::Event(event) => event,
            Self::ReceivedKline(kline) => TransportEvent::Kline(kline.event),
            Self::ReceivedOrderBook(book) => TransportEvent::OrderBook(book.event),
            Self::ReceivedTrade(trade) => TransportEvent::Trade(trade.event),
        }
    }

    /// Borrow the underlying `instrument_id`.
    pub fn instrument_id(&self) -> &str {
        match self {
            Self::Event(TransportEvent::Kline(e)) => &e.instrument_id,
            Self::Event(TransportEvent::OrderBook(e)) => &e.instrument_id,
            Self::Event(TransportEvent::DerivativeTick(e)) => &e.instrument_id,
            Self::Event(TransportEvent::Trade(e)) => &e.instrument_id,
            Self::ReceivedKline(e) => &e.event.instrument_id,
            Self::ReceivedOrderBook(e) => &e.event.instrument_id,
            Self::ReceivedTrade(e) => &e.event.instrument_id,
        }
    }
}

impl From<TransportEvent> for TransportFrame {
    fn from(value: TransportEvent) -> Self {
        Self::Event(value)
    }
}

impl From<ReceivedKlineEvent> for TransportFrame {
    fn from(value: ReceivedKlineEvent) -> Self {
        Self::ReceivedKline(value)
    }
}

impl From<ReceivedOrderBookEvent> for TransportFrame {
    fn from(value: ReceivedOrderBookEvent) -> Self {
        Self::ReceivedOrderBook(value)
    }
}

impl From<ReceivedTradeEvent> for TransportFrame {
    fn from(value: ReceivedTradeEvent) -> Self {
        Self::ReceivedTrade(value)
    }
}

impl From<OrderBookEvent> for TransportFrame {
    fn from(value: OrderBookEvent) -> Self {
        Self::Event(TransportEvent::OrderBook(value))
    }
}

impl From<talaria_contracts::KlineEvent> for TransportFrame {
    fn from(value: talaria_contracts::KlineEvent) -> Self {
        Self::Event(TransportEvent::Kline(value))
    }
}

impl From<DerivativeTickEvent> for TransportFrame {
    fn from(value: DerivativeTickEvent) -> Self {
        Self::Event(TransportEvent::DerivativeTick(value))
    }
}

impl From<TradeEvent> for TransportFrame {
    fn from(value: TradeEvent) -> Self {
        Self::Event(TransportEvent::Trade(value))
    }
}

/// Paginated historical market data connector (`REST` or offline fixture).
pub trait HistoryConnector: Send + Sync {
    /// Mode identifier (`"bybit"`, `"fixture"`, etc.).
    fn feed_mode(&self) -> &str;

    /// Fetch up to `limit` closed bars with `ts_event <= end_ts`, sorted ascending by `ts_event`.
    fn fetch_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawBar>, PackError>>;

    /// Fetch up to `limit` closed mark-price bars sorted ascending by `ts_event`.
    fn fetch_mark_price_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        let _ = (symbol, timeframe, end_ts, limit);
        Box::pin(async {
            Err(PackError::InvalidRequest(
                "mark price history is not supported by this connector".to_owned(),
            ))
        })
    }

    /// Fetch up to `limit` funding rate settlements sorted ascending by `ts_event`.
    fn fetch_funding_settlements<'a>(
        &'a self,
        symbol: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        let _ = (symbol, end_ts, limit);
        Box::pin(async {
            Err(PackError::InvalidRequest(
                "funding history is not supported by this connector".to_owned(),
            ))
        })
    }

    /// Fetch up to `limit` closed open-interest observations sorted ascending by `ts_event`.
    fn fetch_open_interest<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawDerivativePoint>, PackError>> {
        let _ = (symbol, timeframe, end_ts, limit);
        Box::pin(async {
            Err(PackError::InvalidRequest(
                "open interest history is not supported by this connector".to_owned(),
            ))
        })
    }

    /// Optional runtime registration of a dynamically added instrument.
    fn register_instrument(&self, _spec: &InstrumentSpec) {}
}

/// Real-time or replay feed transport consumed by `FeedSupervisor`.
///
/// Methods take `&self` using interior concurrency so `subscribe`/`unsubscribe` never race or
/// require cancelling an in-flight `recv`.
pub trait FeedTransport: Send + Sync {
    /// Mode identifier (`"bybit"`, `"replay"`, etc.).
    fn feed_mode(&self) -> &str;

    /// Establish a transport connection (or resume a replay script after disconnect).
    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>>;

    /// Subscribe to market data for `(instrument_id, timeframe)`.
    fn subscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>>;

    /// Unsubscribe from `(instrument_id, timeframe)`.
    fn unsubscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>>;

    /// Receive next transport frame (`Ok(None)` = keepalive/heartbeat, `Err(FeedTransport)` = lost).
    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>>;

    /// Close the transport; idempotent.
    fn close(&self) -> BoxFuture<'_, Result<(), PackError>>;

    /// Optional runtime registration of a dynamically added instrument for live WS mapping.
    fn register_instrument(&self, _spec: &InstrumentSpec) {}
}

/// Read-only margin valuation capability exposed by packs supporting derivatives/leverage.
pub trait MarginValuation: Send + Sync {
    fn interface_version(&self) -> u32;

    fn isolated_liquidation_price(
        &self,
        instrument_id: &str,
        direction: Direction,
        quantity: Decimal,
        entry_price: Decimal,
        isolated_margin: Decimal,
        as_of: i64,
    ) -> Result<Decimal, PackError>;

    fn cross_maintenance_margin(
        &self,
        positions: &[(String, Direction, Decimal, Decimal)],
        mark_prices: &BTreeMap<String, Decimal>,
        leverage: Decimal,
        collateral: Decimal,
    ) -> Result<MarginEstimate, PackError>;
}

/// Optional historical trade archive reader capability (e.g., daily `.csv.gz` settlement dumps).
pub trait TradeArchiveReader: Send + Sync {
    fn fetch_daily_trades<'a>(
        &'a self,
        instrument: &'a InstrumentSpec,
        day_utc: chrono::NaiveDate,
        now_ms: i64,
    ) -> BoxFuture<'a, Result<Vec<Trade>, PackError>>;
}

/// Complete venue/asset-class plugin trait isolating market specifics from core/feed/server layers.
pub trait MarketPack: Send + Sync {
    /// Pack identifier (`"crypto_perp"`, `"us_equity"`, ...).
    fn name(&self) -> &str;

    /// Raw archive directory namespace (`"bybit_linear"`, ...).
    fn raw_archive_id(&self) -> &str;

    /// Optional co-pilot skill prompt describing pack conventions.
    fn market_skill(&self) -> &str;

    /// Relative path to built-in offline fixture root, if shipped in repo.
    fn default_fixture_dir(&self) -> Option<&Path>;

    /// All static instruments provided by this pack.
    fn instruments(&self) -> &[InstrumentSpec];

    /// Snapshot of all instruments (static + dynamically registered) currently active in this pack.
    fn instruments_snapshot(&self) -> Vec<InstrumentSpec> {
        self.instruments().to_vec()
    }

    /// Dynamically register a new instrument into this pack at runtime.
    fn register_instrument(&self, _spec: InstrumentSpec) -> Result<(), PackError> {
        Err(PackError::InvalidRequest(format!(
            "market pack `{}` does not support dynamic instrument registration",
            self.name()
        )))
    }

    /// Search online exchange catalog (or offline fixture/static fallback) for instruments matching `query`.
    fn search_catalog<'a>(
        &'a self,
        query: &'a str,
        _venue: Option<&'a str>,
        limit: usize,
        _config: &'a HistoryConnectorConfig,
    ) -> BoxFuture<'a, Result<Vec<CatalogInstrument>, PackError>> {
        let items = filter_and_rank_catalog(catalog_from_specs(&self.instruments_snapshot()), query, limit);
        Box::pin(async move { Ok(items) })
    }

    /// Look up one instrument by `instrument_id`.
    fn instrument(&self, instrument_id: &str) -> Option<&InstrumentSpec> {
        self.instruments()
            .iter()
            .find(|spec| spec.instrument_id == instrument_id)
    }

    /// Look up the six capability traits for `instrument_id`.
    fn traits(&self, instrument_id: &str) -> Result<TraitBundle, PackError>;

    /// Construct a historical data connector (REST or offline fixture).
    fn create_history_connector(
        &self,
        config: HistoryConnectorConfig,
    ) -> Result<Arc<dyn HistoryConnector>, PackError>;

    /// Construct a real-time WebSocket stream transport.
    fn create_realtime_stream(
        &self,
        config: RealtimeStreamConfig,
    ) -> Result<Arc<dyn FeedTransport>, PackError>;

    /// Optional read-only margin valuation helper.
    fn margin_valuation(&self) -> Option<Arc<dyn MarginValuation>> {
        None
    }

    /// Optional historical trade archive reader.
    fn trade_archive_reader(&self) -> Option<Arc<dyn TradeArchiveReader>> {
        None
    }
}

/// Pagination window parameters for [`backfill_bars`] and [`backfill_derivatives`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct BackfillWindow {
    pub start_ts: Option<i64>,
    pub end_ts: Option<i64>,
    pub target_count: usize,
    pub max_pages: usize,
    pub page_limit: usize,
}

impl Default for BackfillWindow {
    fn default() -> Self {
        Self {
            start_ts: None,
            end_ts: None,
            target_count: 3000,
            max_pages: 5,
            page_limit: 1000,
        }
    }
}

/// Paginate backwards using `HistoryConnector::fetch_bars` across `window`.
pub async fn backfill_bars(
    connector: &dyn HistoryConnector,
    symbol: &str,
    timeframe: &str,
    window: BackfillWindow,
) -> Result<Vec<RawBar>, PackError> {
    if window.target_count == 0 || window.max_pages == 0 || window.page_limit == 0 {
        return Ok(Vec::new());
    }
    let lower = window.start_ts.unwrap_or(0);
    let mut cursor = window.end_ts;
    let mut collected: BTreeMap<i64, RawBar> = BTreeMap::new();

    for _ in 0..window.max_pages {
        if collected.len() >= window.target_count {
            break;
        }
        if let Some(c) = cursor
            && c < lower
        {
            break;
        }
        let wanted = window.page_limit.min(window.target_count - collected.len());
        let page = connector
            .fetch_bars(symbol, timeframe, cursor, wanted)
            .await?;
        let mut min_ts: Option<i64> = None;
        let mut inserted = 0usize;
        for bar in page {
            if let Some(c) = cursor
                && bar.ts_event > c
            {
                continue;
            }
            if bar.ts_event < lower {
                continue;
            }
            min_ts = Some(min_ts.map_or(bar.ts_event, |m| m.min(bar.ts_event)));
            if collected.insert(bar.ts_event, bar).is_none() {
                inserted += 1;
            }
        }
        let Some(oldest) = min_ts else {
            break;
        };
        if inserted == 0 || oldest <= lower {
            break;
        }
        cursor = Some(oldest - 1);
    }

    Ok(collected.into_values().collect())
}

/// Paginate backwards for derivative series (`MarkPrice`, `FundingRate`, `OpenInterest`).
pub async fn backfill_derivatives(
    connector: &dyn HistoryConnector,
    symbol: &str,
    kind: DerivativeKind,
    timeframe: Option<&str>,
    window: BackfillWindow,
) -> Result<Vec<RawDerivativePoint>, PackError> {
    if window.target_count == 0 || window.max_pages == 0 || window.page_limit == 0 {
        return Ok(Vec::new());
    }
    let lower = window.start_ts.unwrap_or(0);
    let mut cursor = window.end_ts;
    let mut collected: BTreeMap<i64, RawDerivativePoint> = BTreeMap::new();

    for _ in 0..window.max_pages {
        if collected.len() >= window.target_count {
            break;
        }
        if let Some(c) = cursor
            && c < lower
        {
            break;
        }
        let wanted = window.page_limit.min(window.target_count - collected.len());
        let page = match kind {
            DerivativeKind::MarkPrice => {
                let tf = timeframe.ok_or_else(|| {
                    PackError::InvalidRequest("mark_price backfill requires a timeframe".to_owned())
                })?;
                connector
                    .fetch_mark_price_bars(symbol, tf, cursor, wanted)
                    .await?
            }
            DerivativeKind::FundingRate => {
                connector
                    .fetch_funding_settlements(symbol, cursor, wanted)
                    .await?
            }
            DerivativeKind::OpenInterest => {
                let tf = timeframe.ok_or_else(|| {
                    PackError::InvalidRequest(
                        "open_interest backfill requires a timeframe".to_owned(),
                    )
                })?;
                connector
                    .fetch_open_interest(symbol, tf, cursor, wanted)
                    .await?
            }
        };
        let mut min_ts: Option<i64> = None;
        let mut inserted = 0usize;
        for pt in page {
            if let Some(c) = cursor
                && pt.ts_event > c
            {
                continue;
            }
            if pt.ts_event < lower {
                continue;
            }
            min_ts = Some(min_ts.map_or(pt.ts_event, |m| m.min(pt.ts_event)));
            if collected.insert(pt.ts_event, pt).is_none() {
                inserted += 1;
            }
        }
        let Some(oldest) = min_ts else {
            break;
        };
        if inserted == 0 || oldest <= lower {
            break;
        }
        cursor = Some(oldest - 1);
    }

    Ok(collected.into_values().collect())
}
