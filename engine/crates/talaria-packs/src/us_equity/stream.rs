//! Alpaca Market Data v2 WebSocket transport (`Iex` vs `Sip`) with strict feed isolation.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use talaria_contracts::{
    CompactBar, InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier, ReceivedKlineEvent,
    ReceivedOrderBookEvent, TradeSide, classify_flat_bar, format_decimal_str, timeframe_ms,
};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::warn;

use crate::error::PackError;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
    default_clock,
};
use crate::us_equity::AlpacaDataFeed;
use crate::us_equity::connector::{
    alpaca_timeframe_for, parse_alpaca_decimal, parse_alpaca_timestamp_ms,
};
use crate::us_equity::session::is_within_feed_hours;
use crate::uuid_util::{
    ConnectionStamp, deterministic_packet_uuid, format_uuid_bytes, parse_trade_event,
};

/// Default Alpaca IEX real-time WebSocket endpoint (`08:00-17:00 ET` free intraday).
pub const IEX_WS_URL: &str = "wss://stream.data.alpaca.markets/v2/iex";
/// Default Alpaca SIP real-time WebSocket endpoint (consolidated market stream).
pub const SIP_WS_URL: &str = "wss://stream.data.alpaca.markets/v2/sip";

type EventResult = Result<Option<TransportFrame>, PackError>;

/// Ensure an explicit WS URL does not point to the opposite Alpaca feed (`iex` vs `sip`).
pub fn validate_ws_url_for_feed(ws_url: &str, feed: AlpacaDataFeed) -> Result<(), PackError> {
    let lower = ws_url.to_ascii_lowercase();
    let path = lower.split(['?', '#']).next().unwrap_or(&lower);
    let opposite = match feed {
        AlpacaDataFeed::Iex => "sip",
        AlpacaDataFeed::Sip => "iex",
    };
    if path.ends_with(&format!("/{opposite}"))
        || lower.contains(&format!("feed={opposite}"))
    {
        return Err(PackError::InvalidRequest(format!(
            "refusing silent Alpaca feed mismatch: configured `{}` cannot connect to `{ws_url}`",
            feed.as_str()
        )));
    }
    Ok(())
}

enum WsCommand {
    Request {
        payload: String,
        ack_tx: oneshot::Sender<Result<(), PackError>>,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    },
    UpdateSubscription {
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
    last_quote: Option<(Decimal, Decimal)>,
}

/// Stateful Alpaca WS v2 parser supporting both live streams and offline `ws_frames.jsonl` replay.
pub struct AlpacaWsParser {
    feed: AlpacaDataFeed,
    instrument_by_symbol: Arc<std::sync::RwLock<BTreeMap<String, String>>>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    book_updates: BTreeMap<String, BookTracker>,
    last_trade_price: BTreeMap<String, Decimal>,
}

impl AlpacaWsParser {
    /// Construct an Alpaca WS parser bound to `feed` and `instruments`.
    pub fn new(
        instruments: &[InstrumentSpec],
        feed: AlpacaDataFeed,
        now_ms: ClockFn,
        persist_public_trades: bool,
    ) -> Self {
        let mut instrument_by_symbol = BTreeMap::new();
        for spec in instruments {
            instrument_by_symbol.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        Self {
            feed,
            instrument_by_symbol: Arc::new(std::sync::RwLock::new(instrument_by_symbol)),
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
            book_updates: BTreeMap::new(),
            last_trade_price: BTreeMap::new(),
        }
    }

    pub fn feed(&self) -> AlpacaDataFeed {
        self.feed
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
            self.last_trade_price.remove(instrument_id);
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

    /// Parse one raw Alpaca WS v2 JSON payload (array of typed objects or single object).
    pub fn parse_json_str(
        &mut self,
        raw: &str,
        received_ts: i64,
    ) -> Result<Vec<TransportFrame>, PackError> {
        let value: Value = serde_json::from_str(raw).map_err(|err| {
            PackError::FeedTransport(format!("malformed Alpaca WebSocket JSON: {err}"))
        })?;
        Ok(self.parse_value(&value, received_ts))
    }

    fn parse_value(&mut self, value: &Value, received_ts: i64) -> Vec<TransportFrame> {
        match value {
            Value::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for item in items {
                    if let Some(obj) = item.as_object() {
                        out.extend(self.parse_object(obj, received_ts));
                    }
                }
                out
            }
            Value::Object(obj) => self.parse_object(obj, received_ts),
            _ => Vec::new(),
        }
    }

    fn parse_object(
        &mut self,
        obj: &Map<String, Value>,
        received_ts: i64,
    ) -> Vec<TransportFrame> {
        if let Some(frame_feed) = obj.get("feed").and_then(Value::as_str)
            && !frame_feed.eq_ignore_ascii_case(self.feed.as_str())
        {
            warn!(
                "ignoring Alpaca WS frame with mismatched feed `{frame_feed}` (expected `{}`)",
                self.feed.as_str()
            );
            return Vec::new();
        }
        let Some(kind) = obj.get("T").and_then(Value::as_str) else {
            return Vec::new();
        };
        match kind {
            "b" => self
                .parse_bar(obj, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedKline)
                .collect(),
            "q" => self
                .parse_quote(obj, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedOrderBook)
                .collect(),
            "t" => {
                if !self.persist_public_trades {
                    return Vec::new();
                }
                self.parse_trade(obj, received_ts).into_iter().collect()
            }
            _ => Vec::new(),
        }
    }

    fn parse_bar(&self, data: &Map<String, Value>, received_ts: i64) -> Option<ReceivedKlineEvent> {
        let symbol = data.get("S").and_then(Value::as_str)?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        let timeframe = data
            .get("tf")
            .and_then(Value::as_str)
            .unwrap_or("1m");
        if !self
            .subscriptions
            .contains(&(instrument_id.clone(), timeframe.to_owned()))
        {
            return None;
        }
        let interval = timeframe_ms(timeframe).ok()?;

        let parsed = (|| -> Result<Option<ReceivedKlineEvent>, PackError> {
            let timestamp = parse_alpaca_timestamp_ms(
                data.get("t")
                    .ok_or_else(|| PackError::FeedTransport("missing Alpaca bar `t`".to_owned()))?,
                "t",
            )?;
            if !is_within_feed_hours(self.feed, timestamp) {
                return Ok(None);
            }
            let open = parse_field(data, "o")?;
            let high = parse_field(data, "h")?;
            let low = parse_field(data, "l")?;
            let close = parse_field(data, "c")?;
            let volume = parse_field(data, "v")?;
            let turnover = if let Some(vw_val) = data.get("vw").filter(|v| !v.is_null()) {
                (parse_alpaca_decimal(vw_val, "vw")? * volume).round_dp(6)
            } else {
                (close * volume).round_dp(6)
            };
            let status = classify_flat_bar(open, high, low, close, volume);
            let bar = CompactBar {
                timestamp,
                open: Some(open),
                high: Some(high),
                low: Some(low),
                close: Some(close),
                volume: Some(volume),
                turnover: Some(turnover),
                status,
                ts_available: timestamp + interval,
                precision: PrecisionTier::SecondVerified,
            };
            bar.validate()?;
            Ok(Some(ReceivedKlineEvent {
                event: KlineEvent {
                    instrument_id: instrument_id.clone(),
                    timeframe: timeframe.to_owned(),
                    bar,
                    confirmed: true,
                    revision: 0,
                },
                ts_ingested: received_ts,
                history_only: false,
                synthetic: false,
            }))
        })();

        match parsed {
            Ok(ev) => ev,
            Err(err) => {
                warn!("ignoring invalid Alpaca bar for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_quote(
        &mut self,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<ReceivedOrderBookEvent> {
        let symbol = data.get("S").and_then(Value::as_str)?;
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
            let ts_event = parse_alpaca_timestamp_ms(
                data.get("t")
                    .ok_or_else(|| PackError::FeedTransport("missing Alpaca quote `t`".to_owned()))?,
                "t",
            )?;
            if !is_within_feed_hours(self.feed, ts_event) {
                return Ok(None);
            }
            let bid_price = parse_field(data, "bp")?;
            let bid_size = parse_field(data, "bs")?;
            let ask_price = parse_field(data, "ap")?;
            let ask_size = parse_field(data, "as")?;

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

            let tracker = self.book_updates.entry(instrument_id.clone()).or_default();
            tracker.last_update_id = tracker.last_update_id.saturating_add(1);
            tracker.last_quote = Some((bid_price, ask_price));
            let update_id = tracker.last_update_id;
            let stamp = match &tracker.stamp {
                Some(existing) => existing.clone(),
                None => ConnectionStamp::fresh(&instrument_id, received_ts)?,
            };
            let wrapped = stamp.build_received_order_book(event, Some(ts_event), update_id);
            tracker.stamp = Some(stamp);
            Ok(Some(wrapped))
        })();

        match parsed {
            Ok(res) => res,
            Err(err) => {
                warn!("ignoring invalid Alpaca quote for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_trade(
        &mut self,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<TransportFrame> {
        let symbol = data.get("S").and_then(Value::as_str)?;
        let instrument_id = self
            .instrument_by_symbol
            .read()
            .ok()?
            .get(symbol)?
            .clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }

        let parsed = (|| -> Result<Option<TransportFrame>, PackError> {
            let ts_event = parse_alpaca_timestamp_ms(
                data.get("t")
                    .ok_or_else(|| PackError::FeedTransport("missing Alpaca trade `t`".to_owned()))?,
                "t",
            )?;
            if !is_within_feed_hours(self.feed, ts_event) {
                return Ok(None);
            }
            let trade_id = data
                .get("i")
                .and_then(Value::as_i64)
                .ok_or_else(|| PackError::FeedTransport("missing Alpaca trade `i`".to_owned()))?;
            let price = parse_field(data, "p")?;
            let size = parse_field(data, "s")?;
            if price <= Decimal::ZERO || size <= Decimal::ZERO {
                return Err(PackError::FeedTransport(
                    "Alpaca trade price and size must be positive".to_owned(),
                ));
            }
            let side = self.infer_trade_side(&instrument_id, price);
            self.last_trade_price.insert(instrument_id.clone(), price);

            let trade_uuid = deterministic_trade_uuid("alpaca", symbol, trade_id, ts_event);
            let event = parse_trade_event(
                &instrument_id,
                &trade_uuid,
                side,
                &format_decimal_str(price),
                &format_decimal_str(size),
                ts_event,
                received_ts,
            )?;
            let packet_id = deterministic_packet_uuid(&[trade_uuid]);
            Ok(Some(TransportFrame::ReceivedTrade(ReceivedTradeEvent {
                event,
                ts_published: Some(ts_event),
                sequence: Some(trade_id),
                packet_id: Some(packet_id),
                packet_index: Some(0),
            })))
        })();

        match parsed {
            Ok(res) => res,
            Err(err) => {
                warn!("ignoring invalid Alpaca trade for {symbol}: {err}");
                None
            }
        }
    }

    /// Infer aggressor side via quote-rule (Lee-Ready) with tick-rule fallback.
    fn infer_trade_side(&self, instrument_id: &str, price: Decimal) -> TradeSide {
        if let Some((bid, ask)) = self.book_updates.get(instrument_id).and_then(|t| t.last_quote) {
            if price >= ask {
                return TradeSide::Buy;
            }
            if price <= bid {
                return TradeSide::Sell;
            }
        }
        if let Some(&prev) = self.last_trade_price.get(instrument_id)
            && price < prev
        {
            return TradeSide::Sell;
        }
        TradeSide::Buy
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

fn parse_field(data: &Map<String, Value>, key: &str) -> Result<Decimal, PackError> {
    let val = data
        .get(key)
        .ok_or_else(|| PackError::FeedTransport(format!("missing Alpaca WS field `{key}`")))?;
    parse_alpaca_decimal(val, key)
}

/// Real-time Alpaca Market Data v2 WebSocket [`FeedTransport`] bound explicitly to [`AlpacaDataFeed`].
pub struct AlpacaStream {
    feed: AlpacaDataFeed,
    ws_url: String,
    api_key: String,
    api_secret: String,
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

impl AlpacaStream {
    /// Construct an Alpaca WS stream explicitly scoped to `feed` (`Iex` or `Sip`).
    pub fn new(
        instruments: &[InstrumentSpec],
        feed: AlpacaDataFeed,
        config: RealtimeStreamConfig,
    ) -> Result<Self, PackError> {
        let ws_url = config
            .ws_url
            .unwrap_or_else(|| feed.default_ws_url().to_owned());
        validate_ws_url_for_feed(&ws_url, feed)?;

        let mut symbol_by_instrument = BTreeMap::new();
        let mut instrument_by_symbol = BTreeMap::new();
        for spec in instruments {
            symbol_by_instrument.insert(spec.instrument_id.clone(), spec.symbol.clone());
            instrument_by_symbol.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        let (_tx, rx) = mpsc::unbounded_channel();
        Ok(Self {
            feed,
            ws_url,
            api_key: std::env::var("APCA_API_KEY_ID").unwrap_or_else(|_| "talaria-local".to_owned()),
            api_secret: std::env::var("APCA_API_SECRET_KEY").unwrap_or_else(|_| "talaria-local".to_owned()),
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
            }),
            events_rx: Arc::new(Mutex::new(rx)),
        })
    }

    /// Construct with default configuration for `feed`.
    pub fn default_live(instruments: &[InstrumentSpec], feed: AlpacaDataFeed) -> Result<Self, PackError> {
        Self::new(
            instruments,
            feed,
            RealtimeStreamConfig {
                now_ms: default_clock(),
                ..RealtimeStreamConfig::default()
            },
        )
    }

    pub fn feed(&self) -> AlpacaDataFeed {
        self.feed
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
        payload: String,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    ) -> Result<(), PackError> {
        let (ack_tx, ack_rx) = oneshot::channel();
        cmd_tx
            .send(WsCommand::Request {
                payload,
                ack_tx,
                subscribe,
                unsubscribe,
            })
            .map_err(|_| {
                PackError::FeedTransport("Alpaca WebSocket worker stopped".to_owned())
            })?;

        match tokio::time::timeout(self.ack_timeout, ack_rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(PackError::FeedTransport(
                "Alpaca subscription ack interrupted".to_owned(),
            )),
            Err(_) => Err(PackError::FeedTransport(
                "timed out waiting for Alpaca subscription ack".to_owned(),
            )),
        }
    }
}

impl FeedTransport for AlpacaStream {
    fn feed_mode(&self) -> &str {
        self.feed.feed_mode()
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            {
                let mut state = self.lifecycle.lock().await;
                if state.closed {
                    return Err(PackError::FeedTransport(
                        "Alpaca stream is closed".to_owned(),
                    ));
                }
                if let Some(conn) = &state.connection {
                    if !conn.worker.is_finished() {
                        return Ok(());
                    }
                    Self::teardown_connection(&mut state).await;
                }
            }

            let mut ws_stream = tokio::time::timeout(self.ack_timeout, connect_async(&self.ws_url))
                .await
                .map_err(|_| {
                    PackError::FeedTransport(format!(
                        "timed out connecting to Alpaca WebSocket `{}`",
                        self.ws_url
                    ))
                })?
                .map_err(|err| {
                    PackError::FeedTransport(format!(
                        "failed to connect to Alpaca WebSocket `{}`: {err}",
                        self.ws_url
                    ))
                })?
                .0;

            // Alpaca v2 WS handshake: read initial `connected` greeting (optional in tests) then authenticate.
            let auth_payload = json!({
                "action": "auth",
                "key": self.api_key,
                "secret": self.api_secret,
            })
            .to_string();

            tokio::time::timeout(self.ack_timeout, async {
                if let Some(Ok(Message::Text(text))) = ws_stream.next().await
                    && let Ok(items) = serde_json::from_str::<Vec<Value>>(text.as_str())
                    && let Some(err_msg) = find_alpaca_error(&items)
                {
                    return Err(PackError::FeedTransport(err_msg));
                }
                ws_stream
                    .send(Message::Text(auth_payload.into()))
                    .await
                    .map_err(|e| PackError::FeedTransport(format!("Alpaca auth send failed: {e}")))?;

                while let Some(msg) = ws_stream.next().await {
                    match msg {
                        Ok(Message::Text(text)) => {
                            let Ok(items) = serde_json::from_str::<Vec<Value>>(text.as_str()) else {
                                continue;
                            };
                            if let Some(err_msg) = find_alpaca_error(&items) {
                                return Err(PackError::FeedTransport(err_msg));
                            }
                            if items.iter().any(|v| {
                                v.get("T").and_then(Value::as_str) == Some("success")
                                    && v.get("msg").and_then(Value::as_str) == Some("authenticated")
                            }) {
                                return Ok(());
                            }
                        }
                        Ok(Message::Close(_)) | Err(_) => {
                            return Err(PackError::FeedTransport(
                                "Alpaca WebSocket closed during authentication".to_owned(),
                            ));
                        }
                        _ => {}
                    }
                }
                Err(PackError::FeedTransport(
                    "Alpaca WebSocket ended during authentication".to_owned(),
                ))
            })
            .await
            .map_err(|_| {
                PackError::FeedTransport("timed out waiting for Alpaca WS authentication".to_owned())
            })??;

            let (events_tx, new_events_rx) = mpsc::unbounded_channel();
            {
                let mut rx_guard = self.events_rx.lock().await;
                *rx_guard = new_events_rx;
            }

            let (cmd_tx, cmd_rx) = mpsc::unbounded_channel();
            let mut parser = AlpacaWsParser::new(
                &self.instruments.read().unwrap(),
                self.feed,
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
                    "Alpaca stream is closed".to_owned(),
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
            let _ = alpaca_timeframe_for(timeframe)
                .map_err(|_| PackError::FeedTransport(format!("unsupported timeframe: {timeframe}")))?;

            let (cmd_tx, payload) = {
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
                        PackError::FeedTransport("Alpaca stream is not connected".to_owned())
                    })?;
                state.subscriptions.insert(pair);
                let mut req = json!({
                    "action": "subscribe",
                    "bars": [symbol],
                    "quotes": [symbol],
                });
                if self.persist_public_trades {
                    req["trades"] = json!([symbol]);
                }
                (cmd_tx, req.to_string())
            };

            let res = self
                .send_request_with_ack(
                    &cmd_tx,
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
            let (cmd_tx, payload) = {
                let mut state = self.lifecycle.lock().await;
                let pair = (instrument_id.to_owned(), timeframe.to_owned());
                if !state.subscriptions.remove(&pair) {
                    return Ok(());
                }
                let Some(cmd_tx) = state.connection.as_ref().map(|c| c.cmd_tx.clone()) else {
                    return Ok(());
                };
                if state.subscriptions.iter().any(|(i, _)| i == instrument_id) {
                    let _ = cmd_tx.send(WsCommand::UpdateSubscription {
                        subscribe: None,
                        unsubscribe: Some(pair),
                    });
                    return Ok(());
                }
                let mut req = json!({
                    "action": "unsubscribe",
                    "bars": [symbol],
                    "quotes": [symbol],
                });
                if self.persist_public_trades {
                    req["trades"] = json!([symbol]);
                }
                (cmd_tx, req.to_string())
            };

            let _ = self
                .send_request_with_ack(
                    &cmd_tx,
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
                    "Alpaca stream connection closed".to_owned(),
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

fn find_alpaca_error(items: &[Value]) -> Option<String> {
    for item in items {
        if item.get("T").and_then(Value::as_str) == Some("error") {
            let code = item.get("code").and_then(Value::as_i64);
            let msg = item
                .get("msg")
                .and_then(Value::as_str)
                .unwrap_or("Alpaca WebSocket error");
            return Some(format!("Alpaca WS error ({code:?}): {msg}"));
        }
    }
    None
}

async fn run_ws_worker(
    ws: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    mut cmd_rx: mpsc::UnboundedReceiver<WsCommand>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    mut parser: AlpacaWsParser,
    ping_interval: Option<Duration>,
    ping_timeout: Duration,
) {
    let (mut ws_write, mut ws_read) = ws.split();
    let mut acks: VecDeque<oneshot::Sender<Result<(), PackError>>> = VecDeque::new();
    let mut last_message_at = Instant::now();
    let mut ping_ticker = ping_interval.map(|dur| {
        let mut interval = tokio::time::interval_at(tokio::time::Instant::now() + dur, dur);
        interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        interval
    });
    let mut clean_close = false;
    let mut disconnect_reason = "Alpaca WebSocket disconnected".to_owned();

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
                    Some(WsCommand::Request { payload, ack_tx, subscribe, unsubscribe }) => {
                        parser.apply_subscription_op(subscribe, unsubscribe);
                        acks.push_back(ack_tx);
                        if let Err(err) = ws_write.send(Message::Text(payload.into())).await {
                            disconnect_reason = format!("Alpaca WebSocket send failed: {err}");
                            break;
                        }
                    }
                    Some(WsCommand::UpdateSubscription { subscribe, unsubscribe }) => {
                        parser.apply_subscription_op(subscribe, unsubscribe);
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
                    warn!("Alpaca WebSocket heartbeat timed out; closing connection");
                    disconnect_reason = "Alpaca WebSocket heartbeat timeout".to_owned();
                    let _ = ws_write.close().await;
                    break;
                }
                if let Err(err) = ws_write.send(Message::Ping(Vec::new().into())).await {
                    disconnect_reason = format!("Alpaca ping send failed: {err}");
                    break;
                }
            }
            msg = ws_read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        let received_ts = (parser.now_ms)();
                        last_message_at = Instant::now();
                        let Ok(val) = serde_json::from_str::<Value>(text.as_str()) else {
                            warn!("ignoring malformed Alpaca WebSocket text frame");
                            continue;
                        };
                        let items: Vec<&Value> = match &val {
                            Value::Array(arr) => arr.iter().collect(),
                            Value::Object(_) => vec![&val],
                            _ => continue,
                        };
                        for item in items {
                            let Some(obj) = item.as_object() else { continue; };
                            match obj.get("T").and_then(Value::as_str) {
                                Some("subscription") => {
                                    if let Some(ack_tx) = acks.pop_front() {
                                        let _ = ack_tx.send(Ok(()));
                                    }
                                }
                                Some("error") => {
                                    let code = obj.get("code").and_then(Value::as_i64);
                                    let msg = obj
                                        .get("msg")
                                        .and_then(Value::as_str)
                                        .unwrap_or("Alpaca subscription error");
                                    if let Some(ack_tx) = acks.pop_front() {
                                        let _ = ack_tx.send(Err(PackError::FeedTransport(format!(
                                            "Alpaca WS error ({code:?}): {msg}"
                                        ))));
                                    }
                                }
                                Some("success") => {}
                                _ => {
                                    for frame in parser.parse_object(obj, received_ts) {
                                        let _ = events_tx.send(Ok(Some(frame)));
                                    }
                                }
                            }
                        }
                    }
                    Some(Ok(Message::Ping(payload))) => {
                        last_message_at = Instant::now();
                        if let Err(err) = ws_write.send(Message::Pong(payload)).await {
                            disconnect_reason = format!("Alpaca pong send failed: {err}");
                            break;
                        }
                    }
                    Some(Ok(Message::Pong(_)))
                    | Some(Ok(Message::Binary(_)))
                    | Some(Ok(Message::Frame(_))) => {
                        last_message_at = Instant::now();
                    }
                    Some(Ok(Message::Close(_))) => {
                        disconnect_reason = "Alpaca WebSocket closed by remote".to_owned();
                        break;
                    }
                    Some(Err(err)) => {
                        disconnect_reason = format!("Alpaca WebSocket read error: {err}");
                        break;
                    }
                    None => {
                        disconnect_reason = "Alpaca WebSocket stream ended".to_owned();
                        break;
                    }
                }
            }
        }
    }

    while let Some(ack_tx) = acks.pop_front() {
        let _ = ack_tx.send(Err(PackError::FeedTransport(
            "Alpaca subscription ack interrupted by disconnect".to_owned(),
        )));
    }
    if !clean_close {
        let _ = events_tx.send(Err(PackError::FeedTransport(disconnect_reason)));
    }
}
