//! Binance USD-M Futures WebSocket transport (`kline`, `bookTicker`, `markPrice`, `aggTrade`).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use talaria_contracts::{
    CompactBar, DerivativeTickEvent, InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier,
    ReceivedKlineEvent, ReceivedOrderBookEvent, TradeSide, TransportEvent, classify_flat_bar,
    parse_decimal_str,
};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::warn;

use crate::binance_perp::connector::{
    parse_binance_decimal_str, parse_binance_i64, rest_interval_for,
};
use crate::error::PackError;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
    default_clock,
};
use crate::uuid_util::{
    ConnectionStamp, deterministic_packet_uuid, format_uuid_bytes, parse_trade_event,
};

/// Default Binance USD-M Futures combined public WebSocket endpoint.
pub const DEFAULT_WS_URL: &str = "wss://fstream.binance.com/stream";

type EventResult = Result<Option<TransportFrame>, PackError>;

enum WsCommand {
    Request {
        id: u64,
        payload: String,
        ack_tx: oneshot::Sender<Result<(), PackError>>,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    },
    Close,
}

struct StreamConnection {
    cmd_tx: mpsc::UnboundedSender<WsCommand>,
    worker: JoinHandle<()>,
}

struct LifecycleState {
    closed: bool,
    connection: Option<StreamConnection>,
    subscriptions: BTreeSet<(String, String)>,
    next_req_id: u64,
}

impl Drop for LifecycleState {
    fn drop(&mut self) {
        if let Some(conn) = self.connection.take() {
            conn.worker.abort();
        }
    }
}

#[derive(Default)]
struct BookTracker {
    stamp: Option<ConnectionStamp>,
    last_update_id: u64,
}

/// Stateful Binance USD-M Futures WS frame parser (shared by live WS and offline JSONL replay).
pub struct BinanceWsParser {
    instrument_by_symbol: Arc<std::sync::RwLock<BTreeMap<String, String>>>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    book_updates: BTreeMap<String, BookTracker>,
}

impl BinanceWsParser {
    /// Construct a parser mapped over `instruments`.
    pub fn new(
        instruments: &[InstrumentSpec],
        now_ms: ClockFn,
        persist_public_trades: bool,
    ) -> Self {
        let mut instrument_by_symbol = BTreeMap::new();
        for spec in instruments {
            instrument_by_symbol.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        Self {
            instrument_by_symbol: Arc::new(std::sync::RwLock::new(instrument_by_symbol)),
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
            book_updates: BTreeMap::new(),
        }
    }

    /// Register an active `(instrument_id, timeframe)` subscription filter on the parser.
    pub fn subscribe(&mut self, instrument_id: &str, timeframe: &str) {
        self.subscriptions
            .insert((instrument_id.to_owned(), timeframe.to_owned()));
    }

    /// Remove an active `(instrument_id, timeframe)` subscription filter.
    pub fn unsubscribe(&mut self, instrument_id: &str, timeframe: &str) {
        self.subscriptions
            .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
        if !self.subscriptions.iter().any(|(i, _)| i == instrument_id) {
            self.book_updates.remove(instrument_id);
        }
    }

    fn apply_subscription_op(
        &mut self,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    ) {
        if let Some((inst, tf)) = subscribe {
            self.subscribe(&inst, &tf);
        }
        if let Some((inst, tf)) = unsubscribe {
            self.unsubscribe(&inst, &tf);
        }
    }

    /// Parse one raw Binance WS JSON payload (either combined `{"stream","data"}` or bare event).
    pub fn parse_json_str(
        &mut self,
        raw: &str,
        received_ts: i64,
    ) -> Result<Vec<TransportFrame>, PackError> {
        let value: Value = serde_json::from_str(raw).map_err(|err| {
            PackError::FeedTransport(format!("malformed Binance WebSocket JSON: {err}"))
        })?;
        let obj = value.as_object().ok_or_else(|| {
            PackError::FeedTransport("Binance WebSocket frame must be a JSON object".to_owned())
        })?;
        Ok(self.parse_envelope(obj, received_ts))
    }

    fn parse_envelope(&mut self, envelope: &Map<String, Value>, received_ts: i64) -> Vec<TransportFrame> {
        let data = match envelope.get("data").and_then(Value::as_object) {
            Some(inner) if envelope.contains_key("stream") => inner,
            _ => envelope,
        };
        let Some(event_type) = data.get("e").and_then(Value::as_str) else {
            return Vec::new();
        };

        match event_type {
            "kline" => self
                .parse_kline(data, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedKline)
                .collect(),
            "bookTicker" => self
                .parse_book_ticker(data, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedOrderBook)
                .collect(),
            "markPriceUpdate" => self
                .parse_mark_price(data, received_ts)
                .into_iter()
                .map(|ev| TransportFrame::Event(TransportEvent::DerivativeTick(ev)))
                .collect(),
            "aggTrade" => {
                if !self.persist_public_trades {
                    return Vec::new();
                }
                self.parse_agg_trade(data, received_ts).into_iter().collect()
            }
            _ => Vec::new(),
        }
    }

    fn parse_kline(&self, data: &Map<String, Value>, received_ts: i64) -> Option<ReceivedKlineEvent> {
        let k = data.get("k").and_then(Value::as_object)?;
        let symbol = k
            .get("s")
            .or_else(|| data.get("s"))
            .and_then(Value::as_str)?;
        let interval = k.get("i").and_then(Value::as_str)?;
        let timeframe = rest_interval_for(interval).ok()?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        if !self
            .subscriptions
            .contains(&(instrument_id.clone(), timeframe.to_owned()))
        {
            return None;
        }

        let parsed = (|| -> Result<ReceivedKlineEvent, PackError> {
            let start = parse_binance_i64(
                k.get("t")
                    .ok_or_else(|| PackError::FeedTransport("missing kline `t`".to_owned()))?,
                "t",
            )?;
            let close_ts = parse_binance_i64(
                k.get("T")
                    .ok_or_else(|| PackError::FeedTransport("missing kline `T`".to_owned()))?,
                "T",
            )? + 1;
            let open = parse_kline_decimal(k, "o")?;
            let high = parse_kline_decimal(k, "h")?;
            let low = parse_kline_decimal(k, "l")?;
            let close = parse_kline_decimal(k, "c")?;
            let volume = parse_kline_decimal(k, "v")?;
            let turnover = parse_kline_decimal(k, "q")?;
            let status = classify_flat_bar(open, high, low, close, volume);
            let confirmed = k.get("x").and_then(Value::as_bool).unwrap_or(false);

            let bar = CompactBar {
                timestamp: start,
                open: Some(open),
                high: Some(high),
                low: Some(low),
                close: Some(close),
                volume: Some(volume),
                turnover: Some(turnover),
                status,
                ts_available: close_ts,
                precision: PrecisionTier::SecondVerified,
            };
            bar.validate()?;
            Ok(ReceivedKlineEvent {
                event: KlineEvent {
                    instrument_id: instrument_id.clone(),
                    timeframe: timeframe.to_owned(),
                    bar,
                    confirmed,
                    revision: 0,
                },
                ts_ingested: received_ts,
                history_only: false,
                synthetic: false,
            })
        })();

        match parsed {
            Ok(ev) => Some(ev),
            Err(err) => {
                warn!("ignoring invalid Binance kline for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_book_ticker(
        &mut self,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<ReceivedOrderBookEvent> {
        let symbol = data.get("s").and_then(Value::as_str)?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }

        let parsed = (|| -> Result<Option<ReceivedOrderBookEvent>, PackError> {
            let bid_price = parse_kline_decimal(data, "b")?;
            let bid_size = parse_kline_decimal(data, "B")?;
            let ask_price = parse_kline_decimal(data, "a")?;
            let ask_size = parse_kline_decimal(data, "A")?;

            let ts_event = data
                .get("T")
                .or_else(|| data.get("E"))
                .filter(|v| !v.is_null())
                .map(|v| parse_binance_i64(v, "T"))
                .transpose()?
                .unwrap_or(received_ts);

            let update_id_val = data
                .get("u")
                .ok_or_else(|| PackError::FeedTransport("missing bookTicker `u`".to_owned()))?;
            let update_id = u64::try_from(parse_binance_i64(update_id_val, "u")?)
                .map_err(|_| PackError::FeedTransport("negative bookTicker `u`".to_owned()))?;

            let tracker = self.book_updates.entry(instrument_id.clone()).or_default();
            if tracker.stamp.is_some() && update_id <= tracker.last_update_id {
                return Ok(None);
            }

            let event = OrderBookEvent {
                instrument_id: instrument_id.clone(),
                bid_price,
                bid_size,
                ask_price,
                ask_size,
                ts_event,
                ts_ingested: received_ts,
            };
            event.validate()?;

            let stamp = match &tracker.stamp {
                Some(existing) => existing.clone(),
                None => ConnectionStamp::fresh(&instrument_id, received_ts)?,
            };
            let ts_published = data
                .get("E")
                .filter(|v| !v.is_null())
                .map(|v| parse_binance_i64(v, "E"))
                .transpose()?;
            let wrapped = stamp.build_received_order_book(event, ts_published, update_id);
            tracker.stamp = Some(stamp);
            tracker.last_update_id = update_id;
            Ok(Some(wrapped))
        })();

        match parsed {
            Ok(res) => res,
            Err(err) => {
                warn!("ignoring invalid Binance bookTicker for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_mark_price(
        &self,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<DerivativeTickEvent> {
        let symbol = data.get("s").and_then(Value::as_str)?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }
        let received = (self.now_ms)().max(received_ts);

        let parsed = (|| -> Result<DerivativeTickEvent, PackError> {
            let ts_event = data
                .get("E")
                .filter(|v| !v.is_null())
                .map(|v| parse_binance_i64(v, "E"))
                .transpose()?
                .unwrap_or(received);
            let mark_price = parse_opt_decimal(data.get("p"), "p")?;
            let funding_rate = parse_opt_decimal(data.get("r"), "r")?;
            let next_funding_ts = data
                .get("T")
                .filter(|v| !v.is_null())
                .map(|v| parse_binance_i64(v, "T"))
                .transpose()?
                .filter(|&t| t > 0);
            let ev = DerivativeTickEvent {
                instrument_id: instrument_id.clone(),
                mark_price,
                funding_rate,
                next_funding_ts,
                open_interest: None,
                ts_event,
                ts_ingested: received,
            };
            ev.validate()?;
            Ok(ev)
        })();

        match parsed {
            Ok(ev) => Some(ev),
            Err(err) => {
                warn!("ignoring invalid Binance markPriceUpdate for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_agg_trade(
        &self,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<TransportFrame> {
        let symbol = data.get("s").and_then(Value::as_str)?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }

        let parsed = (|| -> Result<TransportFrame, PackError> {
            let agg_id_val = data
                .get("a")
                .ok_or_else(|| PackError::FeedTransport("missing aggTrade `a`".to_owned()))?;
            let agg_id = parse_binance_i64(agg_id_val, "a")?;
            let price_str = data
                .get("p")
                .and_then(Value::as_str)
                .ok_or_else(|| PackError::FeedTransport("aggTrade `p` must be string".to_owned()))?;
            let size_str = data
                .get("q")
                .and_then(Value::as_str)
                .ok_or_else(|| PackError::FeedTransport("aggTrade `q` must be string".to_owned()))?;
            let ts_event = parse_binance_i64(
                data.get("T")
                    .ok_or_else(|| PackError::FeedTransport("missing aggTrade `T`".to_owned()))?,
                "T",
            )?;
            let is_buyer_maker = data
                .get("m")
                .and_then(Value::as_bool)
                .ok_or_else(|| PackError::FeedTransport("missing aggTrade `m`".to_owned()))?;
            // If buyer is maker, the market aggressor was a seller.
            let side = if is_buyer_maker {
                TradeSide::Sell
            } else {
                TradeSide::Buy
            };
            let trade_uuid = deterministic_trade_uuid("binance", symbol, agg_id, ts_event);
            let event = parse_trade_event(
                &instrument_id,
                &trade_uuid,
                side,
                price_str,
                size_str,
                ts_event,
                received_ts,
            )?;
            if event.price <= Decimal::ZERO || event.size <= Decimal::ZERO {
                return Err(PackError::FeedTransport(
                    "aggTrade price and size must be positive".to_owned(),
                ));
            }
            let ts_published = data
                .get("E")
                .filter(|v| !v.is_null())
                .map(|v| parse_binance_i64(v, "E"))
                .transpose()?;
            let packet_id = deterministic_packet_uuid(&[trade_uuid]);
            Ok(TransportFrame::ReceivedTrade(ReceivedTradeEvent {
                event,
                ts_published,
                sequence: Some(agg_id),
                packet_id: Some(packet_id),
                packet_index: Some(0),
            }))
        })();

        match parsed {
            Ok(frame) => Some(frame),
            Err(err) => {
                warn!("ignoring invalid Binance aggTrade for {symbol}: {err}");
                None
            }
        }
    }
}

fn deterministic_trade_uuid(namespace: &str, symbol: &str, trade_id: i64, ts_event: i64) -> String {
    let mut hasher = Sha256::new();
    hasher.update(namespace.as_bytes());
    hasher.update(b":");
    hasher.update(symbol.as_bytes());
    hasher.update(b":");
    hasher.update(trade_id.to_le_bytes());
    hasher.update(ts_event.to_le_bytes());
    let digest = hasher.finalize();
    let mut bytes = [0u8; 16];
    bytes.copy_from_slice(&digest[..16]);
    format_uuid_bytes(bytes, 5)
}

fn parse_kline_decimal(data: &Map<String, Value>, key: &str) -> Result<Decimal, PackError> {
    let val = data
        .get(key)
        .ok_or_else(|| PackError::FeedTransport(format!("missing Binance WS field `{key}`")))?;
    parse_binance_decimal_str(val, key)
}

fn parse_opt_decimal(val: Option<&Value>, key: &str) -> Result<Option<Decimal>, PackError> {
    match val {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::String(s)) => parse_decimal_str(s)
            .map(Some)
            .map_err(|err| PackError::FeedTransport(format!("invalid Binance `{key}`: {err}"))),
        Some(_) => Err(PackError::FeedTransport(format!(
            "Binance WS field `{key}` must be a DecimalStr string"
        ))),
    }
}

/// Public Binance USD-M Futures WebSocket [`FeedTransport`] implementation.
pub struct BinanceFuturesStream {
    ws_url: String,
    instruments: std::sync::RwLock<Vec<InstrumentSpec>>,
    symbol_by_instrument: std::sync::RwLock<BTreeMap<String, String>>,
    instrument_by_symbol: Arc<std::sync::RwLock<BTreeMap<String, String>>>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    ack_timeout: Duration,
    ping_interval: Option<Duration>,
    ping_timeout: Duration,
    lifecycle: Mutex<LifecycleState>,
    events_rx: Arc<Mutex<mpsc::UnboundedReceiver<EventResult>>>,
}

impl BinanceFuturesStream {
    /// Construct a Binance USD-M Futures WebSocket stream.
    pub fn new(instruments: &[InstrumentSpec], config: RealtimeStreamConfig) -> Self {
        let mut symbol_by_instrument = BTreeMap::new();
        let mut instrument_by_symbol = BTreeMap::new();
        for spec in instruments {
            symbol_by_instrument.insert(spec.instrument_id.clone(), spec.symbol.clone());
            instrument_by_symbol.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        let (_tx, rx) = mpsc::unbounded_channel();
        Self {
            ws_url: config
                .ws_url
                .unwrap_or_else(|| DEFAULT_WS_URL.to_owned()),
            instruments: std::sync::RwLock::new(instruments.to_vec()),
            symbol_by_instrument: std::sync::RwLock::new(symbol_by_instrument),
            instrument_by_symbol: Arc::new(std::sync::RwLock::new(instrument_by_symbol)),
            now_ms: config.now_ms,
            persist_public_trades: config.persist_public_trades,
            ack_timeout: config.ack_timeout,
            ping_interval: config.ping_interval,
            ping_timeout: config.ping_timeout,
            lifecycle: Mutex::new(LifecycleState {
                closed: false,
                connection: None,
                subscriptions: BTreeSet::new(),
                next_req_id: 1,
            }),
            events_rx: Arc::new(Mutex::new(rx)),
        }
    }

    /// Construct with default configuration targeting [`DEFAULT_WS_URL`].
    pub fn default_live(instruments: &[InstrumentSpec]) -> Self {
        Self::new(
            instruments,
            RealtimeStreamConfig {
                now_ms: default_clock(),
                ..RealtimeStreamConfig::default()
            },
        )
    }

    async fn teardown_connection(state: &mut LifecycleState) {
        if let Some(conn) = state.connection.take() {
            let _ = conn.cmd_tx.send(WsCommand::Close);
            let _ = conn.worker.await;
        }
    }

    async fn send_request_with_ack(
        &self,
        cmd_tx: &mpsc::UnboundedSender<WsCommand>,
        id: u64,
        payload: String,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    ) -> Result<(), PackError> {
        let (ack_tx, ack_rx) = oneshot::channel();
        cmd_tx
            .send(WsCommand::Request {
                id,
                payload,
                ack_tx,
                subscribe,
                unsubscribe,
            })
            .map_err(|_| {
                PackError::FeedTransport("Binance WebSocket worker stopped".to_owned())
            })?;

        match tokio::time::timeout(self.ack_timeout, ack_rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(PackError::FeedTransport(
                "Binance subscription ack interrupted".to_owned(),
            )),
            Err(_) => Err(PackError::FeedTransport(
                "timed out waiting for Binance subscription ack".to_owned(),
            )),
        }
    }

    fn build_streams(&self, symbol: &str, interval: &str, include_instrument_streams: bool) -> Vec<String> {
        let lower = symbol.to_ascii_lowercase();
        let mut streams = vec![format!("{lower}@kline_{interval}")];
        if include_instrument_streams {
            streams.push(format!("{lower}@bookTicker"));
            streams.push(format!("{lower}@markPrice@1s"));
            if self.persist_public_trades {
                streams.push(format!("{lower}@aggTrade"));
            }
        }
        streams
    }
}

impl FeedTransport for BinanceFuturesStream {
    fn feed_mode(&self) -> &str {
        "binance"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            {
                let mut state = self.lifecycle.lock().await;
                if state.closed {
                    return Err(PackError::FeedTransport(
                        "Binance Futures stream is closed".to_owned(),
                    ));
                }
                if let Some(conn) = &state.connection {
                    if !conn.worker.is_finished() {
                        return Ok(());
                    }
                    Self::teardown_connection(&mut state).await;
                }
            }

            let ws_stream = tokio::time::timeout(self.ack_timeout, connect_async(&self.ws_url))
                .await
                .map_err(|_| {
                    PackError::FeedTransport(format!(
                        "timed out connecting to Binance WebSocket `{}`",
                        self.ws_url
                    ))
                })?
                .map_err(|err| {
                    PackError::FeedTransport(format!(
                        "failed to connect to Binance WebSocket `{}`: {err}",
                        self.ws_url
                    ))
                })?
                .0;

            let (events_tx, new_events_rx) = mpsc::unbounded_channel();
            {
                let mut rx_guard = self.events_rx.lock().await;
                *rx_guard = new_events_rx;
            }

            let (cmd_tx, cmd_rx) = mpsc::unbounded_channel();
            let mut parser = BinanceWsParser::new(
                &self.instruments.read().unwrap(),
                Arc::clone(&self.now_ms),
                self.persist_public_trades,
            );
            parser.instrument_by_symbol = Arc::clone(&self.instrument_by_symbol);
            let worker = tokio::spawn(run_ws_worker(
                ws_stream,
                cmd_rx,
                events_tx,
                parser,
                self.ping_interval,
                self.ping_timeout,
            ));

            let mut state = self.lifecycle.lock().await;
            if state.closed {
                let _ = cmd_tx.send(WsCommand::Close);
                let _ = worker.await;
                return Err(PackError::FeedTransport(
                    "Binance Futures stream is closed".to_owned(),
                ));
            }
            state.subscriptions.clear();
            state.connection = Some(StreamConnection { cmd_tx, worker });
            Ok(())
        })
    }

    fn subscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            let symbol = self
                .symbol_by_instrument
                .read()
                .ok()
                .and_then(|m| m.get(instrument_id).cloned())
                .ok_or_else(|| {
                    PackError::FeedTransport(format!("unknown instrument: {instrument_id}"))
                })?;
            let interval = rest_interval_for(timeframe)
                .map_err(|_| PackError::FeedTransport(format!("unsupported timeframe: {timeframe}")))?;

            let (cmd_tx, req_id, payload) = {
                let mut state = self.lifecycle.lock().await;
                let pair = (instrument_id.to_owned(), timeframe.to_owned());
                if state.subscriptions.contains(&pair) {
                    return Ok(());
                }
                let cmd_tx = state
                    .connection
                    .as_ref()
                    .map(|c| c.cmd_tx.clone())
                    .ok_or_else(|| {
                        PackError::FeedTransport("Binance Futures stream is not connected".to_owned())
                    })?;
                let first_for_instrument = !state.subscriptions.iter().any(|(i, _)| i == instrument_id);
                let streams = self.build_streams(&symbol, interval, first_for_instrument);
                let req_id = state.next_req_id;
                state.next_req_id += 1;
                state.subscriptions.insert(pair);
                let payload = json!({
                    "method": "SUBSCRIBE",
                    "params": streams,
                    "id": req_id,
                })
                .to_string();
                (cmd_tx, req_id, payload)
            };

            let res = self
                .send_request_with_ack(
                    &cmd_tx,
                    req_id,
                    payload,
                    Some((instrument_id.to_owned(), timeframe.to_owned())),
                    None,
                )
                .await;
            if let Err(err) = res {
                let mut state = self.lifecycle.lock().await;
                state
                    .subscriptions
                    .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
                Self::teardown_connection(&mut state).await;
                return Err(err);
            }
            Ok(())
        })
    }

    fn unsubscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            let Some(symbol) = self
                .symbol_by_instrument
                .read()
                .ok()
                .and_then(|m| m.get(instrument_id).cloned())
            else {
                return Ok(());
            };
            let Ok(interval) = rest_interval_for(timeframe) else {
                return Ok(());
            };
            let (cmd_tx, req_id, payload) = {
                let mut state = self.lifecycle.lock().await;
                let pair = (instrument_id.to_owned(), timeframe.to_owned());
                if !state.subscriptions.remove(&pair) {
                    return Ok(());
                }
                let Some(cmd_tx) = state.connection.as_ref().map(|c| c.cmd_tx.clone()) else {
                    return Ok(());
                };
                let last_for_instrument = !state.subscriptions.iter().any(|(i, _)| i == instrument_id);
                let streams = self.build_streams(&symbol, interval, last_for_instrument);
                let req_id = state.next_req_id;
                state.next_req_id += 1;
                let payload = json!({
                    "method": "UNSUBSCRIBE",
                    "params": streams,
                    "id": req_id,
                })
                .to_string();
                (cmd_tx, req_id, payload)
            };

            let _ = self
                .send_request_with_ack(
                    &cmd_tx,
                    req_id,
                    payload,
                    None,
                    Some((instrument_id.to_owned(), timeframe.to_owned())),
                )
                .await;
            Ok(())
        })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            let mut rx = self.events_rx.lock().await;
            match rx.recv().await {
                Some(res) => res,
                None => Err(PackError::FeedTransport(
                    "Binance Futures stream connection closed".to_owned(),
                )),
            }
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            state.closed = true;
            state.subscriptions.clear();
            Self::teardown_connection(&mut state).await;
            Ok(())
        })
    }

    fn register_instrument(&self, spec: &InstrumentSpec) {
        if let Ok(mut guard) = self.instruments.write()
            && !guard.iter().any(|s| s.instrument_id == spec.instrument_id)
        {
            guard.push(spec.clone());
        }
        if let Ok(mut guard) = self.symbol_by_instrument.write() {
            guard.insert(spec.instrument_id.clone(), spec.symbol.clone());
        }
        if let Ok(mut guard) = self.instrument_by_symbol.write() {
            guard.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
    }
}

async fn run_ws_worker(
    ws: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    mut cmd_rx: mpsc::UnboundedReceiver<WsCommand>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    mut parser: BinanceWsParser,
    ping_interval: Option<Duration>,
    ping_timeout: Duration,
) {
    let (mut ws_write, mut ws_read) = ws.split();
    let mut pending_acks: VecDeque<(u64, oneshot::Sender<Result<(), PackError>>)> = VecDeque::new();
    let mut last_message_at = Instant::now();
    let mut ping_ticker = ping_interval.map(|dur| {
        let mut interval = tokio::time::interval_at(tokio::time::Instant::now() + dur, dur);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        interval
    });
    let mut clean_close = false;
    let mut disconnect_reason = "Binance WebSocket disconnected".to_owned();

    loop {
        let ping_fut = async {
            match &mut ping_ticker {
                Some(t) => t.tick().await,
                None => std::future::pending::<tokio::time::Instant>().await,
            }
        };

        tokio::select! {
            biased;
            cmd = cmd_rx.recv() => {
                match cmd {
                    Some(WsCommand::Request { id, payload, ack_tx, subscribe, unsubscribe }) => {
                        parser.apply_subscription_op(subscribe, unsubscribe);
                        pending_acks.push_back((id, ack_tx));
                        if let Err(err) = ws_write.send(Message::Text(payload.into())).await {
                            disconnect_reason = format!("Binance WebSocket send failed: {err}");
                            break;
                        }
                    }
                    Some(WsCommand::Close) | None => {
                        clean_close = true;
                        let _ = ws_write.close().await;
                        break;
                    }
                }
            }
            _ = ping_fut => {
                if let Some(interval) = ping_interval
                    && last_message_at.elapsed() > interval + ping_timeout
                {
                    warn!("Binance WebSocket heartbeat timed out; closing connection");
                    disconnect_reason = "Binance WebSocket heartbeat timeout".to_owned();
                    let _ = ws_write.close().await;
                    break;
                }
                if let Err(err) = ws_write.send(Message::Ping(Vec::new().into())).await {
                    disconnect_reason = format!("Binance ping send failed: {err}");
                    break;
                }
            }
            msg = ws_read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        let received_ts = (parser.now_ms)();
                        last_message_at = Instant::now();
                        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(text.as_str()) else {
                            warn!("ignoring malformed Binance WebSocket text frame");
                            continue;
                        };
                        if let Some(id) = obj.get("id").and_then(Value::as_u64) {
                            if let Some(pos) = pending_acks.iter().position(|(pid, _)| *pid == id)
                                && let Some((_, ack_tx)) = pending_acks.remove(pos)
                            {
                                if let Some(err_obj) = obj.get("error").and_then(Value::as_object) {
                                    let msg = err_obj
                                        .get("msg")
                                        .and_then(Value::as_str)
                                        .unwrap_or("Binance subscription rejected");
                                    let _ = ack_tx.send(Err(PackError::FeedTransport(msg.to_owned())));
                                } else {
                                    let _ = ack_tx.send(Ok(()));
                                }
                            }
                            continue;
                        }
                        if obj.contains_key("result") && !obj.contains_key("e") && !obj.contains_key("stream") {
                            if let Some((_, ack_tx)) = pending_acks.pop_front() {
                                let _ = ack_tx.send(Ok(()));
                            }
                            continue;
                        }
                        for frame in parser.parse_envelope(&obj, received_ts) {
                            let _ = events_tx.send(Ok(Some(frame)));
                        }
                    }
                    Some(Ok(Message::Ping(payload))) => {
                        last_message_at = Instant::now();
                        if let Err(err) = ws_write.send(Message::Pong(payload)).await {
                            disconnect_reason = format!("Binance pong send failed: {err}");
                            break;
                        }
                    }
                    Some(Ok(Message::Pong(_)))
                    | Some(Ok(Message::Binary(_)))
                    | Some(Ok(Message::Frame(_))) => {
                        last_message_at = Instant::now();
                    }
                    Some(Ok(Message::Close(_))) => {
                        disconnect_reason = "Binance WebSocket closed by remote".to_owned();
                        break;
                    }
                    Some(Err(err)) => {
                        disconnect_reason = format!("Binance WebSocket read error: {err}");
                        break;
                    }
                    None => {
                        disconnect_reason = "Binance WebSocket stream ended".to_owned();
                        break;
                    }
                }
            }
        }
    }

    while let Some((_, ack_tx)) = pending_acks.pop_front() {
        let _ = ack_tx.send(Err(PackError::FeedTransport(
            "Binance subscription ack interrupted by disconnect".to_owned(),
        )));
    }
    if !clean_close {
        let _ = events_tx.send(Err(PackError::FeedTransport(disconnect_reason)));
    }
}
