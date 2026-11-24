//! Bybit V5 Linear public WebSocket transport (`tokio-tungstenite`).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Map, Value, json};
use talaria_contracts::{
    CompactBar, DerivativeTickEvent, InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier,
    ReceivedKlineEvent, TradeSide, classify_flat_bar, parse_decimal_str,
};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::warn;

use crate::crypto_perp::connector::{parse_json_decimal_str, parse_json_i64, rest_interval_for};
use crate::error::PackError;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
    default_clock,
};
use crate::uuid_util::{ConnectionStamp, deterministic_packet_uuid, parse_trade_event};

/// Default Bybit V5 Linear public WebSocket endpoint.
pub const DEFAULT_WS_URL: &str = "wss://stream.bybit.com/v5/public/linear";

/// Map Bybit WS kline interval token back to Talaria timeframe label.
pub fn timeframe_from_bybit_interval(interval: &str) -> Option<&'static str> {
    match interval {
        "1" => Some("1m"),
        "5" => Some("5m"),
        "15" => Some("15m"),
        "60" => Some("1h"),
        "240" => Some("4h"),
        "D" => Some("1d"),
        _ => None,
    }
}

type EventResult = Result<Option<TransportFrame>, PackError>;

enum WsCommand {
    Request {
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

struct ParserState {
    instrument_by_symbol: BTreeMap<String, String>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    tickers_by_instrument: BTreeMap<String, BTreeMap<&'static str, (i64, Option<Value>)>>,
    book_updates: BTreeMap<String, BookTracker>,
}

impl ParserState {
    fn new(
        instrument_by_symbol: BTreeMap<String, String>,
        now_ms: ClockFn,
        persist_public_trades: bool,
    ) -> Self {
        Self {
            instrument_by_symbol,
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
            tickers_by_instrument: BTreeMap::new(),
            book_updates: BTreeMap::new(),
        }
    }

    fn apply_subscription_op(
        &mut self,
        subscribe: Option<(String, String)>,
        unsubscribe: Option<(String, String)>,
    ) {
        if let Some(pair) = subscribe {
            self.subscriptions.insert(pair);
        }
        if let Some((inst, tf)) = unsubscribe {
            self.subscriptions.remove(&(inst.clone(), tf));
            if !self.subscriptions.iter().any(|(i, _)| i == &inst) {
                self.tickers_by_instrument.remove(&inst);
                self.book_updates.remove(&inst);
            }
        }
    }

    fn parse_envelope(&mut self, envelope: &Map<String, Value>, received_ts: i64) -> Vec<TransportFrame> {
        let Some(topic) = envelope.get("topic").and_then(Value::as_str) else {
            return Vec::new();
        };
        let Some(data) = envelope.get("data") else {
            return Vec::new();
        };

        if let Some(symbol) = topic.strip_prefix("publicTrade.") {
            if !self.persist_public_trades {
                return Vec::new();
            }
            return self.parse_trade_batch(symbol, data, envelope, received_ts);
        }

        match data {
            Value::Array(items) => {
                let mut out = Vec::with_capacity(items.len());
                for item in items {
                    if let Some(obj) = item.as_object() {
                        out.extend(self.parse_item(topic, obj, envelope, received_ts));
                    }
                }
                out
            }
            Value::Object(obj) => self.parse_item(topic, obj, envelope, received_ts),
            _ => Vec::new(),
        }
    }

    fn parse_item(
        &mut self,
        topic: &str,
        data: &Map<String, Value>,
        envelope: &Map<String, Value>,
        received_ts: i64,
    ) -> Vec<TransportFrame> {
        if topic.starts_with("kline.") {
            self.parse_kline(topic, data, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedKline)
                .collect()
        } else if topic.starts_with("orderbook.1.") {
            self.parse_book(data, envelope, received_ts)
                .into_iter()
                .map(TransportFrame::ReceivedOrderBook)
                .collect()
        } else if let Some(symbol) = topic.strip_prefix("tickers.") {
            self.parse_tickers(symbol, data, envelope)
                .into_iter()
                .map(|t| TransportFrame::Event(talaria_contracts::TransportEvent::DerivativeTick(t)))
                .collect()
        } else {
            Vec::new()
        }
    }

    fn parse_kline(
        &self,
        topic: &str,
        data: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<ReceivedKlineEvent> {
        let interval = data.get("interval").and_then(Value::as_str)?;
        let (_, symbol) = topic.rsplit_once('.')?;
        let timeframe = timeframe_from_bybit_interval(interval)?;
        let instrument_id = self.instrument_by_symbol.get(symbol)?;
        if !self
            .subscriptions
            .contains(&(instrument_id.clone(), timeframe.to_owned()))
        {
            return None;
        }

        let parsed = (|| -> Result<ReceivedKlineEvent, PackError> {
            let start = parse_json_i64(
                data.get("start")
                    .ok_or_else(|| PackError::FeedTransport("missing kline start".to_owned()))?,
                "start",
            )?;
            let end = parse_json_i64(
                data.get("end")
                    .ok_or_else(|| PackError::FeedTransport("missing kline end".to_owned()))?,
                "end",
            )? + 1;
            let open = parse_field_decimal(data, "open")?;
            let high = parse_field_decimal(data, "high")?;
            let low = parse_field_decimal(data, "low")?;
            let close = parse_field_decimal(data, "close")?;
            let volume = parse_field_decimal(data, "volume")?;
            let turnover = parse_field_decimal(data, "turnover")?;
            let status = classify_flat_bar(open, high, low, close, volume);
            let confirmed = data.get("confirm").and_then(Value::as_bool).unwrap_or(false);

            let bar = CompactBar {
                timestamp: start,
                open: Some(open),
                high: Some(high),
                low: Some(low),
                close: Some(close),
                volume: Some(volume),
                turnover: Some(turnover),
                status,
                ts_available: end,
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
                warn!("ignoring incomplete/invalid Bybit kline for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_book(
        &mut self,
        data: &Map<String, Value>,
        envelope: &Map<String, Value>,
        received_ts: i64,
    ) -> Option<talaria_contracts::ReceivedOrderBookEvent> {
        let symbol = data.get("s").and_then(Value::as_str)?;
        let instrument_id = self.instrument_by_symbol.get(symbol)?.clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }
        let bids = data.get("b").and_then(Value::as_array)?;
        let asks = data.get("a").and_then(Value::as_array)?;
        if bids.is_empty() || asks.is_empty() {
            return None;
        }

        let bid_row = bids[0].as_array()?;
        let ask_row = asks[0].as_array()?;
        if bid_row.len() < 2 || ask_row.len() < 2 {
            return None;
        }

        let parsed = (|| -> Result<Option<talaria_contracts::ReceivedOrderBookEvent>, PackError> {
            let bid_price = parse_json_decimal_str(&bid_row[0], "bid_price")?;
            let bid_size = parse_json_decimal_str(&bid_row[1], "bid_size")?;
            let ask_price = parse_json_decimal_str(&ask_row[0], "ask_price")?;
            let ask_size = parse_json_decimal_str(&ask_row[1], "ask_size")?;

            let ts_event = first_nonzero_i64(&[
                envelope.get("cts"),
                data.get("ts"),
                envelope.get("ts"),
            ])?
            .unwrap_or(received_ts);

            let update_id_raw = data
                .get("u")
                .ok_or_else(|| PackError::FeedTransport("missing orderbook `u`".to_owned()))?;
            let update_id = u64::try_from(parse_json_i64(update_id_raw, "u")?)
                .map_err(|_| PackError::FeedTransport("negative orderbook `u`".to_owned()))?;

            let is_snapshot = envelope.get("type").and_then(Value::as_str) == Some("snapshot");
            let tracker = self.book_updates.entry(instrument_id.clone()).or_default();
            if tracker.stamp.is_none() && !is_snapshot {
                return Ok(None);
            }
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
            let ts_published = envelope
                .get("ts")
                .filter(|v| !v.is_null())
                .map(|v| parse_json_i64(v, "ts"))
                .transpose()?;

            let wrapped = stamp.build_received_order_book(event, ts_published, update_id);
            tracker.stamp = Some(stamp);
            tracker.last_update_id = update_id;
            Ok(Some(wrapped))
        })();

        match parsed {
            Ok(res) => res,
            Err(err) => {
                warn!("ignoring invalid Bybit orderbook for {symbol}: {err}");
                None
            }
        }
    }

    fn parse_tickers(
        &mut self,
        symbol: &str,
        data: &Map<String, Value>,
        envelope: &Map<String, Value>,
    ) -> Option<DerivativeTickEvent> {
        const FIELDS: [&str; 4] = ["markPrice", "fundingRate", "openInterest", "nextFundingTime"];
        let instrument_id = self.instrument_by_symbol.get(symbol)?.clone();
        if !self.subscriptions.iter().any(|(i, _)| i == &instrument_id) {
            return None;
        }

        let message_type = envelope.get("type").and_then(Value::as_str);
        let received = (self.now_ms)();

        let parsed = (|| -> Result<ParsedTickersResult, PackError> {
            let ts_event = first_nonzero_i64(&[data.get("ts"), envelope.get("ts")])?
                .unwrap_or(received);

            let mut ticker: BTreeMap<&'static str, Option<Value>> = if message_type == Some("delta") {
                self.tickers_by_instrument
                    .get(&instrument_id)
                    .map(|prev| {
                        prev.iter()
                            .filter(|(_, (updated, _))| *updated <= ts_event)
                            .map(|(&k, (_, v))| (k, v.clone()))
                            .collect()
                    })
                    .unwrap_or_default()
            } else {
                BTreeMap::new()
            };

            let mut updates: BTreeMap<&'static str, Option<Value>> = BTreeMap::new();
            for field in FIELDS {
                if let Some(val) = data.get(field) {
                    updates.insert(field, Some(val.clone()));
                } else if message_type == Some("snapshot") {
                    updates.insert(field, None);
                }
            }
            for (k, v) in &updates {
                ticker.insert(k, v.clone());
            }

            let mark_price = parse_opt_decimal(ticker.get("markPrice").and_then(Option::as_ref), "markPrice")?;
            let funding_rate = parse_opt_decimal(ticker.get("fundingRate").and_then(Option::as_ref), "fundingRate")?;
            let open_interest = parse_opt_decimal(ticker.get("openInterest").and_then(Option::as_ref), "openInterest")?;
            let next_funding_ts =
                parse_opt_nonzero_i64(ticker.get("nextFundingTime").and_then(Option::as_ref), "nextFundingTime")?;

            let has_reading = mark_price.is_some()
                || funding_rate.is_some()
                || open_interest.is_some()
                || next_funding_ts.is_some();

            let event = if has_reading {
                let ev = DerivativeTickEvent {
                    instrument_id: instrument_id.clone(),
                    mark_price,
                    funding_rate,
                    next_funding_ts,
                    open_interest,
                    ts_event,
                    ts_ingested: received,
                };
                ev.validate()?;
                Some(ev)
            } else {
                None
            };
            Ok((event, ts_event, updates))
        })();

        let (event, ts_event, updates) = match parsed {
            Ok(res) => res,
            Err(err) => {
                warn!("ignoring invalid Bybit tickers for {symbol}: {err}");
                return None;
            }
        };

        if matches!(message_type, Some("snapshot" | "delta")) {
            let cache = self.tickers_by_instrument.entry(instrument_id).or_default();
            for (field, value) in updates {
                if cache
                    .get(field)
                    .is_none_or(|(updated, _)| *updated <= ts_event)
                {
                    cache.insert(field, (ts_event, value));
                }
            }
        }
        event
    }

    fn parse_trade_batch(
        &self,
        symbol: &str,
        data: &Value,
        envelope: &Map<String, Value>,
        received_ts: i64,
    ) -> Vec<TransportFrame> {
        let Some(instrument_id) = self.instrument_by_symbol.get(symbol) else {
            return Vec::new();
        };
        if !self.subscriptions.iter().any(|(i, _)| i == instrument_id) {
            return Vec::new();
        }
        let items: Vec<&Map<String, Value>> = match data {
            Value::Array(arr) => arr.iter().filter_map(Value::as_object).collect(),
            Value::Object(obj) => vec![obj],
            _ => return Vec::new(),
        };

        let ts_published = envelope
            .get("ts")
            .filter(|v| !v.is_null())
            .and_then(|v| parse_json_i64(v, "ts").ok());

        let mut parsed_items: Vec<(ReceivedTradeEvent, String)> = Vec::new();
        for (index, item) in items.iter().enumerate() {
            if item.get("s").and_then(Value::as_str) != Some(symbol) {
                continue;
            }
            let one = (|| -> Result<(ReceivedTradeEvent, String), PackError> {
                let trade_id_str = item
                    .get("i")
                    .and_then(Value::as_str)
                    .ok_or_else(|| PackError::FeedTransport("missing trade `i`".to_owned()))?
                    .to_owned();
                let side = match item.get("S").and_then(Value::as_str) {
                    Some("Buy") => TradeSide::Buy,
                    Some("Sell") => TradeSide::Sell,
                    _ => return Err(PackError::FeedTransport("invalid trade side".to_owned())),
                };
                let price_str = item
                    .get("p")
                    .and_then(Value::as_str)
                    .ok_or_else(|| PackError::FeedTransport("trade `p` must be string".to_owned()))?;
                let size_str = item
                    .get("v")
                    .and_then(Value::as_str)
                    .ok_or_else(|| PackError::FeedTransport("trade `v` must be string".to_owned()))?;
                let ts_event = parse_json_i64(
                    item.get("T")
                        .ok_or_else(|| PackError::FeedTransport("missing trade `T`".to_owned()))?,
                    "T",
                )?;
                let event = parse_trade_event(
                    instrument_id,
                    &trade_id_str,
                    side,
                    price_str,
                    size_str,
                    ts_event,
                    received_ts,
                )?;
                if event.price <= Decimal::ZERO || event.size <= Decimal::ZERO {
                    return Err(PackError::FeedTransport(
                        "trade price and size must be positive".to_owned(),
                    ));
                }
                let sequence = item
                    .get("seq")
                    .filter(|v| !v.is_null())
                    .map(|v| parse_json_i64(v, "seq"))
                    .transpose()?;
                Ok((
                    ReceivedTradeEvent {
                        event,
                        ts_published,
                        sequence,
                        packet_id: None,
                        packet_index: Some(index as i64),
                    },
                    trade_id_str,
                ))
            })();
            match one {
                Ok(ok) => parsed_items.push(ok),
                Err(err) => warn!("ignoring invalid Bybit publicTrade for {symbol}: {err}"),
            }
        }

        if parsed_items.is_empty() {
            return Vec::new();
        }

        let ids: Vec<String> = parsed_items.iter().map(|(_, id)| id.clone()).collect();
        let packet_id = deterministic_packet_uuid(&ids);
        parsed_items
            .into_iter()
            .map(|(mut ev, _)| {
                ev.packet_id = Some(packet_id.clone());
                TransportFrame::ReceivedTrade(ev)
            })
            .collect()
    }
}

type ParsedTickersResult = (
    Option<DerivativeTickEvent>,
    i64,
    BTreeMap<&'static str, Option<Value>>,
);

fn parse_field_decimal(data: &Map<String, Value>, key: &str) -> Result<Decimal, PackError> {
    let val = data
        .get(key)
        .ok_or_else(|| PackError::FeedTransport(format!("missing kline field `{key}`")))?;
    parse_json_decimal_str(val, key)
}

fn parse_opt_decimal(val: Option<&Value>, key: &str) -> Result<Option<Decimal>, PackError> {
    match val {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(Value::String(s)) => parse_decimal_str(s)
            .map(Some)
            .map_err(|err| PackError::FeedTransport(format!("invalid `{key}`: {err}"))),
        Some(_) => Err(PackError::FeedTransport(format!(
            "ticker field `{key}` must be a DecimalStr string"
        ))),
    }
}

fn parse_opt_nonzero_i64(val: Option<&Value>, key: &str) -> Result<Option<i64>, PackError> {
    match val {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(s)) if s.is_empty() => Ok(None),
        Some(other) => {
            let n = parse_json_i64(other, key)?;
            Ok(if n == 0 { None } else { Some(n) })
        }
    }
}

fn first_nonzero_i64(candidates: &[Option<&Value>]) -> Result<Option<i64>, PackError> {
    for candidate in candidates.iter().flatten() {
        if candidate.is_null() {
            continue;
        }
        if let Some(s) = candidate.as_str()
            && s.is_empty()
        {
            continue;
        }
        let n = parse_json_i64(candidate, "ts")?;
        if n != 0 {
            return Ok(Some(n));
        }
    }
    Ok(None)
}

/// Public Bybit V5 Linear WebSocket [`FeedTransport`] implementation.
pub struct BybitPublicStream {
    ws_url: String,
    symbol_by_instrument: BTreeMap<String, String>,
    instrument_by_symbol: BTreeMap<String, String>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    ack_timeout: Duration,
    ping_interval: Option<Duration>,
    ping_timeout: Duration,
    lifecycle: Mutex<LifecycleState>,
    events_rx: Arc<Mutex<mpsc::UnboundedReceiver<EventResult>>>,
}

impl BybitPublicStream {
    /// Construct a Bybit public WS stream from instrument specs and [`RealtimeStreamConfig`].
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
            symbol_by_instrument,
            instrument_by_symbol,
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
            .map_err(|_| PackError::FeedTransport("Bybit WebSocket worker stopped".to_owned()))?;

        match tokio::time::timeout(self.ack_timeout, ack_rx).await {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err(PackError::FeedTransport(
                "Bybit subscription ack interrupted".to_owned(),
            )),
            Err(_) => Err(PackError::FeedTransport(
                "timed out waiting for Bybit subscription ack".to_owned(),
            )),
        }
    }

    fn build_topics(&self, symbol: &str, interval: &str, include_instrument_topics: bool) -> Vec<String> {
        let mut topics = vec![format!("kline.{interval}.{symbol}")];
        if include_instrument_topics {
            topics.push(format!("orderbook.1.{symbol}"));
            topics.push(format!("tickers.{symbol}"));
            if self.persist_public_trades {
                topics.push(format!("publicTrade.{symbol}"));
            }
        }
        topics
    }
}

impl FeedTransport for BybitPublicStream {
    fn feed_mode(&self) -> &str {
        "bybit"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            {
                let mut state = self.lifecycle.lock().await;
                if state.closed {
                    return Err(PackError::FeedTransport(
                        "Bybit public stream is closed".to_owned(),
                    ));
                }
                if let Some(conn) = &state.connection {
                    if !conn.worker.is_finished() {
                        return Ok(());
                    }
                    // Previous worker already exited due to disconnect; clean up before reconnecting.
                    Self::teardown_connection(&mut state).await;
                }
            }

            let ws_stream = tokio::time::timeout(self.ack_timeout, connect_async(&self.ws_url))
                .await
                .map_err(|_| {
                    PackError::FeedTransport(format!(
                        "timed out connecting to Bybit WebSocket `{}`",
                        self.ws_url
                    ))
                })?
                .map_err(|err| {
                    PackError::FeedTransport(format!(
                        "failed to connect to Bybit WebSocket `{}`: {err}",
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
            let parser = ParserState::new(
                self.instrument_by_symbol.clone(),
                Arc::clone(&self.now_ms),
                self.persist_public_trades,
            );
            let ping_interval = self.ping_interval;
            let ping_timeout = self.ping_timeout;
            let worker = tokio::spawn(run_ws_worker(
                ws_stream,
                cmd_rx,
                events_tx,
                parser,
                ping_interval,
                ping_timeout,
            ));

            let mut state = self.lifecycle.lock().await;
            if state.closed {
                let _ = cmd_tx.send(WsCommand::Close);
                let _ = worker.await;
                return Err(PackError::FeedTransport(
                    "Bybit public stream is closed".to_owned(),
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
                .get(instrument_id)
                .ok_or_else(|| PackError::FeedTransport(format!("unknown instrument: {instrument_id}")))?
                .clone();
            let interval = rest_interval_for(timeframe)
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
                        PackError::FeedTransport("Bybit public stream is not connected".to_owned())
                    })?;
                let first_for_instrument = !state.subscriptions.iter().any(|(i, _)| i == instrument_id);
                let topics = self.build_topics(&symbol, interval, first_for_instrument);
                state.subscriptions.insert(pair);
                let payload = json!({ "op": "subscribe", "args": topics }).to_string();
                (cmd_tx, payload)
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
            let Some(symbol) = self.symbol_by_instrument.get(instrument_id).cloned() else {
                return Ok(());
            };
            let Ok(interval) = rest_interval_for(timeframe) else {
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
                let last_for_instrument = !state.subscriptions.iter().any(|(i, _)| i == instrument_id);
                let topics = self.build_topics(&symbol, interval, last_for_instrument);
                let payload = json!({ "op": "unsubscribe", "args": topics }).to_string();
                (cmd_tx, payload)
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
                    "Bybit public stream connection closed".to_owned(),
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
}

async fn run_ws_worker(
    ws: tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >,
    mut cmd_rx: mpsc::UnboundedReceiver<WsCommand>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    mut parser: ParserState,
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
    let mut disconnect_reason = "Bybit WebSocket disconnected".to_owned();

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
                            disconnect_reason = format!("Bybit WebSocket send failed: {err}");
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
                    warn!("Bybit WebSocket heartbeat timed out; closing connection");
                    disconnect_reason = "Bybit WebSocket heartbeat timeout".to_owned();
                    let _ = ws_write.close().await;
                    break;
                }
                if let Err(err) = ws_write.send(Message::Text(r#"{"op":"ping"}"#.into())).await {
                    disconnect_reason = format!("Bybit ping send failed: {err}");
                    break;
                }
            }
            msg = ws_read.next() => {
                match msg {
                    Some(Ok(Message::Text(text))) => {
                        let received_ts = (parser.now_ms)();
                        last_message_at = Instant::now();
                        let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(text.as_str()) else {
                            warn!("ignoring non-object or malformed Bybit WebSocket text frame");
                            continue;
                        };
                        if let Some(op) = obj.get("op").and_then(Value::as_str) {
                            if op == "subscribe" || op == "unsubscribe" {
                                if let Some(ack_tx) = acks.pop_front() {
                                    if obj.get("success").and_then(Value::as_bool) == Some(false) {
                                        let msg = obj
                                            .get("ret_msg")
                                            .and_then(Value::as_str)
                                            .unwrap_or("subscription rejected by Bybit");
                                        let _ = ack_tx.send(Err(PackError::FeedTransport(msg.to_owned())));
                                    } else {
                                        let _ = ack_tx.send(Ok(()));
                                    }
                                }
                                continue;
                            }
                            if op == "ping" {
                                if let Err(err) = ws_write.send(Message::Text(r#"{"op":"pong"}"#.into())).await {
                                    disconnect_reason = format!("Bybit pong send failed: {err}");
                                    break;
                                }
                                continue;
                            }
                            if op == "pong" {
                                continue;
                            }
                        }
                        for frame in parser.parse_envelope(&obj, received_ts) {
                            let _ = events_tx.send(Ok(Some(frame)));
                        }
                    }
                    Some(Ok(Message::Ping(_))) | Some(Ok(Message::Pong(_))) => {
                        last_message_at = Instant::now();
                    }
                    Some(Ok(Message::Binary(_))) | Some(Ok(Message::Frame(_))) => {
                        last_message_at = Instant::now();
                    }
                    Some(Ok(Message::Close(_))) => {
                        disconnect_reason = "Bybit WebSocket closed by remote".to_owned();
                        break;
                    }
                    Some(Err(err)) => {
                        disconnect_reason = format!("Bybit WebSocket read error: {err}");
                        break;
                    }
                    None => {
                        disconnect_reason = "Bybit WebSocket stream ended".to_owned();
                        break;
                    }
                }
            }
        }
    }

    while let Some(ack_tx) = acks.pop_front() {
        let _ = ack_tx.send(Err(PackError::FeedTransport(
            "subscription ack interrupted by disconnect".to_owned(),
        )));
    }
    if !clean_close {
        let _ = events_tx.send(Err(PackError::FeedTransport(disconnect_reason)));
    }
}
