//! Hyperliquid WebSocket real-time feed transport (`wss://api.hyperliquid.xyz/ws`).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use talaria_contracts::{
    CompactBar, DerivativeTickEvent, InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier,
    ReceivedKlineEvent, TradeSide, TransportEvent, classify_flat_bar,
};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::warn;

use crate::error::PackError;
use crate::hyperliquid_perp::connector::{hl_interval_for, parse_hl_candle, parse_hl_decimal_str};
use crate::hyperliquid_perp::precision_for_hl_coin;
use crate::okx_perp::connector::quantize_dp;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
};
use crate::uuid_util::{ConnectionStamp, deterministic_packet_uuid, parse_trade_event};

/// Default Hyperliquid public WebSocket endpoint.
pub const DEFAULT_WS_URL: &str = "wss://api.hyperliquid.xyz/ws";
const RATE_SCALE: u32 = 8;

#[derive(Default)]
struct BookTracker {
    stamp: Option<ConnectionStamp>,
    last_update_id: u64,
}

/// Stateful Hyperliquid WS JSON parser shared between live transport and offline tests.
pub struct HyperliquidWsParser {
    instrument_by_coin: BTreeMap<String, String>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    last_candle_ts: BTreeMap<(String, String), i64>,
    book_trackers: BTreeMap<String, BookTracker>,
}

impl HyperliquidWsParser {
    pub fn new(instruments: &[InstrumentSpec], now_ms: ClockFn, persist_public_trades: bool) -> Self {
        let mut instrument_by_coin = BTreeMap::new();
        for spec in instruments {
            instrument_by_coin.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        Self {
            instrument_by_coin,
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
            last_candle_ts: BTreeMap::new(),
            book_trackers: BTreeMap::new(),
        }
    }

    pub fn add_subscription(&mut self, instrument_id: &str, timeframe: &str) {
        self.subscriptions
            .insert((instrument_id.to_owned(), timeframe.to_owned()));
    }

    pub fn remove_subscription(&mut self, instrument_id: &str, timeframe: &str) {
        self.subscriptions
            .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
    }

    pub fn reset_connection_state(&mut self) {
        for tracker in self.book_trackers.values_mut() {
            tracker.stamp = None;
            tracker.last_update_id = 0;
        }
    }

    /// Parse one raw text Hyperliquid WS message into canonical `TransportFrame`s.
    pub fn decode_text(&mut self, text: &str) -> Result<Vec<TransportFrame>, PackError> {
        let payload: Value = serde_json::from_str(text).map_err(|err| {
            PackError::FeedTransport(format!("invalid Hyperliquid WS JSON: {err}"))
        })?;
        let Some(obj) = payload.as_object() else {
            return Ok(Vec::new());
        };
        let channel = obj.get("channel").and_then(Value::as_str).unwrap_or("");
        if matches!(channel, "subscriptionResponse" | "pong" | "") {
            return Ok(Vec::new());
        }
        if channel == "error" {
            let msg = obj
                .get("data")
                .and_then(Value::as_str)
                .unwrap_or("unknown Hyperliquid WS error");
            return Err(PackError::FeedTransport(format!("Hyperliquid WS error: {msg}")));
        }

        let Some(data) = obj.get("data") else {
            return Ok(Vec::new());
        };
        let now = (self.now_ms)();

        match channel {
            "candle" => {
                let Some(row) = data.as_object() else {
                    return Ok(Vec::new());
                };
                let coin = row.get("s").and_then(Value::as_str).unwrap_or("");
                let tf = row.get("i").and_then(Value::as_str).unwrap_or("");
                let Some(instrument_id) = self.instrument_by_coin.get(coin).cloned() else {
                    return Ok(Vec::new());
                };
                let interval = talaria_contracts::timeframe_ms(tf)?;
                let raw = parse_hl_candle(data, coin, tf, interval, Some(now), Some(now))?;
                let prev_ts = self
                    .last_candle_ts
                    .insert((instrument_id.clone(), tf.to_owned()), raw.ts_event);
                let close_end = row.get("T").and_then(Value::as_i64).unwrap_or(raw.ts_available);
                let is_closed = now >= close_end || prev_ts.is_some_and(|p| raw.ts_event > p);
                let status = classify_flat_bar(raw.open, raw.high, raw.low, raw.close, raw.volume);
                Ok(vec![TransportFrame::ReceivedKline(ReceivedKlineEvent {
                    event: KlineEvent {
                        instrument_id,
                        timeframe: tf.to_owned(),
                        bar: CompactBar {
                            timestamp: raw.ts_event,
                            open: Some(raw.open),
                            high: Some(raw.high),
                            low: Some(raw.low),
                            close: Some(raw.close),
                            volume: Some(raw.volume),
                            turnover: Some(raw.turnover),
                            status,
                            ts_available: if is_closed { raw.ts_available } else { now },
                            precision: PrecisionTier::SecondVerified,
                        },
                        confirmed: is_closed,
                        revision: 1,
                    },
                    ts_ingested: now,
                    history_only: false,
                    synthetic: false,
                })])
            }
            "bbo" => {
                let Some(row) = data.as_object() else {
                    return Ok(Vec::new());
                };
                let coin = row.get("coin").and_then(Value::as_str).unwrap_or("");
                let Some(instrument_id) = self.instrument_by_coin.get(coin).cloned() else {
                    return Ok(Vec::new());
                };
                let Some(levels) = row.get("bbo").and_then(Value::as_array) else {
                    return Ok(Vec::new());
                };
                if levels.len() < 2 || levels[0].is_null() || levels[1].is_null() {
                    return Ok(Vec::new());
                }
                let (price_dp, qty_dp) = precision_for_hl_coin(coin);
                let price_u = price_dp.max(0) as u32;
                let qty_u = qty_dp.max(0) as u32;
                let bid_price = quantize_dp(parse_hl_decimal_str(&levels[0]["px"], "bbo.bid.px")?, price_u);
                let bid_size = quantize_dp(parse_hl_decimal_str(&levels[0]["sz"], "bbo.bid.sz")?, qty_u);
                let ask_price = quantize_dp(parse_hl_decimal_str(&levels[1]["px"], "bbo.ask.px")?, price_u);
                let ask_size = quantize_dp(parse_hl_decimal_str(&levels[1]["sz"], "bbo.ask.sz")?, qty_u);
                let ts_event = row.get("time").and_then(Value::as_i64).unwrap_or(now);

                let tracker = self.book_trackers.entry(instrument_id.clone()).or_default();
                if tracker.stamp.is_none() {
                    tracker.stamp = Some(ConnectionStamp::fresh(&instrument_id, now)?);
                }
                tracker.last_update_id += 1;
                let update_id = tracker.last_update_id;
                let stamp = tracker.stamp.as_ref().expect("initialized");

                let event = OrderBookEvent {
                    instrument_id,
                    bid_price,
                    bid_size,
                    ask_price,
                    ask_size,
                    ts_event,
                    ts_ingested: now,
                };
                Ok(vec![TransportFrame::ReceivedOrderBook(
                    stamp.build_received_order_book(event, Some(ts_event), update_id),
                )])
            }
            "activeAssetCtx" => {
                let Some(row) = data.as_object() else {
                    return Ok(Vec::new());
                };
                let coin = row.get("coin").and_then(Value::as_str).unwrap_or("");
                let Some(instrument_id) = self.instrument_by_coin.get(coin).cloned() else {
                    return Ok(Vec::new());
                };
                let Some(ctx) = row.get("ctx").and_then(Value::as_object) else {
                    return Ok(Vec::new());
                };
                let (price_dp, qty_dp) = precision_for_hl_coin(coin);
                let mark_price = ctx
                    .get("markPx")
                    .map(|v| parse_hl_decimal_str(v, "markPx"))
                    .transpose()?
                    .map(|v| quantize_dp(v, price_dp.max(0) as u32));
                let funding_rate = ctx
                    .get("funding")
                    .map(|v| parse_hl_decimal_str(v, "funding"))
                    .transpose()?
                    .map(|v| quantize_dp(v, RATE_SCALE));
                let open_interest = ctx
                    .get("openInterest")
                    .map(|v| parse_hl_decimal_str(v, "openInterest"))
                    .transpose()?
                    .map(|v| quantize_dp(v, qty_dp.max(0) as u32));
                if mark_price.is_none() && funding_rate.is_none() && open_interest.is_none() {
                    return Ok(Vec::new());
                }
                Ok(vec![TransportFrame::Event(TransportEvent::DerivativeTick(
                    DerivativeTickEvent {
                        instrument_id,
                        mark_price,
                        funding_rate,
                        next_funding_ts: None,
                        open_interest,
                        ts_event: now,
                        ts_ingested: now,
                    },
                ))])
            }
            "trades" if self.persist_public_trades => {
                let Some(rows) = data.as_array() else {
                    return Ok(Vec::new());
                };
                let mut out = Vec::new();
                let mut packet_ids = Vec::new();
                let mut parsed = Vec::new();
                for row in rows {
                    let Some(item) = row.as_object() else { continue };
                    let coin = item.get("coin").and_then(Value::as_str).unwrap_or("");
                    let Some(instrument_id) = self.instrument_by_coin.get(coin).cloned() else {
                        continue;
                    };
                    let px = item.get("px").and_then(Value::as_str).unwrap_or("0");
                    let sz = item.get("sz").and_then(Value::as_str).unwrap_or("0");
                    let side = match item.get("side").and_then(Value::as_str).unwrap_or("") {
                        "B" => TradeSide::Buy,
                        _ => TradeSide::Sell,
                    };
                    let ts_event = item.get("time").and_then(Value::as_i64).unwrap_or(now);
                    let tid = item.get("tid").and_then(Value::as_u64).unwrap_or(0);
                    let synthetic_uuid =
                        deterministic_packet_uuid(&[format!("hl:{coin}:{ts_event}:{tid}")]);
                    let trade_ev =
                        parse_trade_event(&instrument_id, &synthetic_uuid, side, px, sz, ts_event, now)?;
                    packet_ids.push(synthetic_uuid);
                    parsed.push((trade_ev, ts_event, Some(tid as i64)));
                }
                if !parsed.is_empty() {
                    let pkt_id = deterministic_packet_uuid(&packet_ids);
                    for (idx, (event, ts_pub, seq)) in parsed.into_iter().enumerate() {
                        out.push(TransportFrame::ReceivedTrade(ReceivedTradeEvent {
                            event,
                            ts_published: Some(ts_pub),
                            sequence: seq,
                            packet_id: Some(pkt_id.clone()),
                            packet_index: Some(idx as i64),
                        }));
                    }
                }
                Ok(out)
            }
            _ => Ok(Vec::new()),
        }
    }
}

type EventResult = Result<Option<TransportFrame>, PackError>;

enum WsCmd {
    Send {
        payload: String,
        ack_tx: oneshot::Sender<Result<(), PackError>>,
    },
    Close,
}

struct StreamConn {
    cmd_tx: mpsc::UnboundedSender<WsCmd>,
    worker: JoinHandle<()>,
}

struct LifecycleState {
    closed: bool,
    connection: Option<StreamConn>,
    subscriptions: BTreeSet<(String, String)>,
}

impl Drop for LifecycleState {
    fn drop(&mut self) {
        if let Some(conn) = self.connection.take() {
            conn.worker.abort();
        }
    }
}

/// Real-time Hyperliquid WebSocket transport (`FeedTransport`).
pub struct HyperliquidStream {
    instruments: BTreeMap<String, InstrumentSpec>,
    config: RealtimeStreamConfig,
    ws_url: String,
    parser: Arc<Mutex<HyperliquidWsParser>>,
    lifecycle: Mutex<LifecycleState>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    events_rx: Mutex<mpsc::UnboundedReceiver<EventResult>>,
    pending_frames: Mutex<VecDeque<TransportFrame>>,
}

impl HyperliquidStream {
    pub fn new(instruments: &[InstrumentSpec], config: RealtimeStreamConfig) -> Self {
        let mut by_id = BTreeMap::new();
        for spec in instruments {
            by_id.insert(spec.instrument_id.clone(), spec.clone());
        }
        let ws_url = config
            .ws_url
            .clone()
            .unwrap_or_else(|| DEFAULT_WS_URL.to_owned());
        let parser = Arc::new(Mutex::new(HyperliquidWsParser::new(
            instruments,
            Arc::clone(&config.now_ms),
            config.persist_public_trades,
        )));
        let (events_tx, events_rx) = mpsc::unbounded_channel();

        Self {
            instruments: by_id,
            config,
            ws_url,
            parser,
            lifecycle: Mutex::new(LifecycleState {
                closed: false,
                connection: None,
                subscriptions: BTreeSet::new(),
            }),
            events_tx,
            events_rx: Mutex::new(events_rx),
            pending_frames: Mutex::new(VecDeque::new()),
        }
    }

    fn build_subscription_messages(
        &self,
        coin: &str,
        timeframe: &str,
        method: &str,
    ) -> Result<Vec<String>, PackError> {
        let interval = hl_interval_for(timeframe)?;
        let mut msgs = vec![
            json!({"method": method, "subscription": {"type": "candle", "coin": coin, "interval": interval}}).to_string(),
            json!({"method": method, "subscription": {"type": "bbo", "coin": coin}}).to_string(),
            json!({"method": method, "subscription": {"type": "activeAssetCtx", "coin": coin}}).to_string(),
        ];
        if self.config.persist_public_trades {
            msgs.push(json!({"method": method, "subscription": {"type": "trades", "coin": coin}}).to_string());
        }
        Ok(msgs)
    }

    async fn spawn_socket(
        url: String,
        ping_interval: Option<Duration>,
        ack_timeout: Duration,
        parser: Arc<Mutex<HyperliquidWsParser>>,
        events_tx: mpsc::UnboundedSender<EventResult>,
    ) -> Result<StreamConn, PackError> {
        let (ws_stream, _) = tokio::time::timeout(ack_timeout, connect_async(&url))
            .await
            .map_err(|_| PackError::FeedTransport(format!("Hyperliquid WS connect timeout: {url}")))?
            .map_err(|err| PackError::FeedTransport(format!("Hyperliquid WS connect error: {err}")))?;

        let (mut write, mut read) = ws_stream.split();
        let (cmd_tx, mut cmd_rx) = mpsc::unbounded_channel::<WsCmd>();

        let worker = tokio::spawn(async move {
            let mut last_rx = Instant::now();
            let mut ping_ticker = tokio::time::interval(ping_interval.unwrap_or(Duration::from_secs(20)));
            ping_ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            ping_ticker.tick().await;

            loop {
                tokio::select! {
                    biased;
                    cmd = cmd_rx.recv() => {
                        match cmd {
                            Some(WsCmd::Send { payload, ack_tx }) => {
                                let res = write.send(Message::Text(payload.into())).await
                                    .map_err(|err| PackError::FeedTransport(format!("Hyperliquid WS send error: {err}")));
                                let _ = ack_tx.send(res);
                            }
                            Some(WsCmd::Close) | None => {
                                let _ = write.send(Message::Close(None)).await;
                                break;
                            }
                        }
                    }
                    _ = ping_ticker.tick(), if ping_interval.is_some() => {
                        if let Some(iv) = ping_interval
                            && last_rx.elapsed() >= iv
                        {
                            let ping = json!({"method": "ping"}).to_string();
                            if let Err(err) = write.send(Message::Text(ping.into())).await {
                                let _ = events_tx.send(Err(PackError::FeedTransport(format!("Hyperliquid WS ping error: {err}"))));
                                break;
                            }
                        }
                    }
                    incoming = read.next() => {
                        match incoming {
                            Some(Ok(Message::Text(txt))) => {
                                last_rx = Instant::now();
                                let decoded = {
                                    let mut guard = parser.lock().await;
                                    guard.decode_text(&txt)
                                };
                                match decoded {
                                    Ok(frames) => {
                                        for f in frames {
                                            let _ = events_tx.send(Ok(Some(f)));
                                        }
                                    }
                                    Err(err) => {
                                        let _ = events_tx.send(Err(err));
                                        break;
                                    }
                                }
                            }
                            Some(Ok(Message::Ping(p))) => {
                                last_rx = Instant::now();
                                let _ = write.send(Message::Pong(p)).await;
                            }
                            Some(Ok(Message::Pong(_))) => { last_rx = Instant::now(); }
                            Some(Ok(Message::Close(_))) | None => {
                                let _ = events_tx.send(Err(PackError::FeedTransport("Hyperliquid WS closed".to_owned())));
                                break;
                            }
                            Some(Ok(_)) => { last_rx = Instant::now(); }
                            Some(Err(err)) => {
                                warn!("Hyperliquid WS read error: {err}");
                                let _ = events_tx.send(Err(PackError::FeedTransport(format!("Hyperliquid WS read error: {err}"))));
                                break;
                            }
                        }
                    }
                }
            }
        });

        Ok(StreamConn { cmd_tx, worker })
    }
}

impl FeedTransport for HyperliquidStream {
    fn feed_mode(&self) -> &str {
        "hyperliquid"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            if state.closed {
                return Err(PackError::FeedTransport("Hyperliquid stream closed".to_owned()));
            }
            if let Some(old) = state.connection.take() {
                old.worker.abort();
            }
            {
                let mut rx = self.events_rx.lock().await;
                while rx.try_recv().is_ok() {}
            }
            self.pending_frames.lock().await.clear();
            self.parser.lock().await.reset_connection_state();

            let conn = Self::spawn_socket(
                self.ws_url.clone(),
                self.config.ping_interval,
                self.config.ack_timeout,
                Arc::clone(&self.parser),
                self.events_tx.clone(),
            )
            .await?;

            let existing: Vec<_> = state.subscriptions.iter().cloned().collect();
            for (inst_id, tf) in existing {
                if let Some(spec) = self.instruments.get(&inst_id) {
                    for msg in self.build_subscription_messages(&spec.symbol, &tf, "subscribe")? {
                        let (tx, rx) = oneshot::channel();
                        let _ = conn.cmd_tx.send(WsCmd::Send { payload: msg, ack_tx: tx });
                        let _ = rx.await;
                    }
                }
            }
            state.connection = Some(conn);
            Ok(())
        })
    }

    fn subscribe<'a>(
        &'a self,
        instrument_id: &'a str,
        timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async move {
            let spec = self
                .instruments
                .get(instrument_id)
                .ok_or_else(|| PackError::InstrumentNotFound(instrument_id.to_owned()))?;
            let msgs = self.build_subscription_messages(&spec.symbol, timeframe, "subscribe")?;
            let mut state = self.lifecycle.lock().await;
            state
                .subscriptions
                .insert((instrument_id.to_owned(), timeframe.to_owned()));
            self.parser.lock().await.add_subscription(instrument_id, timeframe);
            if let Some(conn) = state.connection.as_ref() {
                for msg in msgs {
                    let (tx, rx) = oneshot::channel();
                    conn.cmd_tx
                        .send(WsCmd::Send { payload: msg, ack_tx: tx })
                        .map_err(|_| PackError::FeedTransport("Hyperliquid WS disconnected".to_owned()))?;
                    rx.await
                        .map_err(|_| PackError::FeedTransport("Hyperliquid subscribe dropped".to_owned()))??;
                }
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
            let Some(spec) = self.instruments.get(instrument_id) else {
                return Ok(());
            };
            let msgs = self.build_subscription_messages(&spec.symbol, timeframe, "unsubscribe")?;
            let mut state = self.lifecycle.lock().await;
            state
                .subscriptions
                .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
            self.parser
                .lock()
                .await
                .remove_subscription(instrument_id, timeframe);
            if let Some(conn) = state.connection.as_ref() {
                for msg in msgs {
                    let (tx, _) = oneshot::channel();
                    let _ = conn.cmd_tx.send(WsCmd::Send { payload: msg, ack_tx: tx });
                }
            }
            Ok(())
        })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            {
                let mut q = self.pending_frames.lock().await;
                if let Some(f) = q.pop_front() {
                    return Ok(Some(f));
                }
            }
            let mut rx = self.events_rx.lock().await;
            match rx.recv().await {
                Some(res) => res,
                None => Err(PackError::FeedTransport("Hyperliquid stream closed".to_owned())),
            }
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            state.closed = true;
            if let Some(conn) = state.connection.take() {
                let _ = conn.cmd_tx.send(WsCmd::Close);
                conn.worker.abort();
            }
            Ok(())
        })
    }
}
