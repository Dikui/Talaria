//! Coinbase Exchange public WebSocket transport (`ticker` + `matches` candle synthesis).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use chrono::DateTime;
use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::{
    CompactBar, InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier, ReceivedKlineEvent,
    TradeSide, classify_flat_bar, timeframe_ms,
};
use tokio::sync::{Mutex, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::warn;

use crate::coinbase_spot::connector::parse_cb_decimal;
use crate::coinbase_spot::precision_for_coinbase_product;
use crate::error::PackError;
use crate::okx_perp::connector::quantize_dp;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
};
use crate::uuid_util::{ConnectionStamp, deterministic_packet_uuid, parse_trade_event};

/// Default Coinbase Exchange public WebSocket URL.
pub const DEFAULT_WS_URL: &str = "wss://ws-feed.exchange.coinbase.com";

#[derive(Default)]
struct BookTracker {
    stamp: Option<ConnectionStamp>,
    last_update_id: u64,
}

#[derive(Clone)]
struct CandleAccum {
    ts_event: i64,
    open: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
    volume: Decimal,
    turnover: Decimal,
    revision: u64,
}

/// Stateful Coinbase Exchange WS parser (`ticker` quotes + `matches` OHLCV trade aggregation).
pub struct CoinbaseWsParser {
    instrument_by_product: BTreeMap<String, InstrumentSpec>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    candles: BTreeMap<(String, String), CandleAccum>,
    book_trackers: BTreeMap<String, BookTracker>,
}

impl CoinbaseWsParser {
    pub fn new(instruments: &[InstrumentSpec], now_ms: ClockFn, persist_public_trades: bool) -> Self {
        let mut instrument_by_product = BTreeMap::new();
        for spec in instruments {
            instrument_by_product.insert(spec.symbol.clone(), spec.clone());
        }
        Self {
            instrument_by_product,
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
            candles: BTreeMap::new(),
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

    fn parse_rfc3339_ms(raw: Option<&Value>, fallback: i64) -> i64 {
        raw.and_then(Value::as_str)
            .and_then(|s| DateTime::parse_from_rfc3339(s).ok())
            .map(|dt| dt.timestamp_millis())
            .unwrap_or(fallback)
    }

    /// Decode one raw Coinbase WS JSON frame into zero or more `TransportFrame`s.
    pub fn decode_text(&mut self, text: &str) -> Result<Vec<TransportFrame>, PackError> {
        let payload: Value = serde_json::from_str(text).map_err(|err| {
            PackError::FeedTransport(format!("invalid Coinbase WS JSON: {err}"))
        })?;
        let Some(obj) = payload.as_object() else {
            return Ok(Vec::new());
        };
        let msg_type = obj.get("type").and_then(Value::as_str).unwrap_or("");
        if matches!(msg_type, "subscriptions" | "heartbeat" | "") {
            return Ok(Vec::new());
        }
        if msg_type == "error" {
            let msg = obj
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or("unknown Coinbase WS error");
            return Err(PackError::FeedTransport(format!("Coinbase WS error: {msg}")));
        }

        let product_id = obj.get("product_id").and_then(Value::as_str).unwrap_or("");
        let Some(spec) = self.instrument_by_product.get(product_id).cloned() else {
            return Ok(Vec::new());
        };
        let instrument_id = spec.instrument_id.clone();
        let (price_dp, qty_dp) = precision_for_coinbase_product(product_id);
        let price_u = price_dp.max(0) as u32;
        let qty_u = qty_dp.max(0) as u32;
        let now = (self.now_ms)();

        match msg_type {
            "ticker" => {
                let (Some(bp_v), Some(ap_v)) = (obj.get("best_bid"), obj.get("best_ask")) else {
                    return Ok(Vec::new());
                };
                let bid_price = quantize_dp(parse_cb_decimal(bp_v, "best_bid")?, price_u);
                let ask_price = quantize_dp(parse_cb_decimal(ap_v, "best_ask")?, price_u);
                let bid_size = obj
                    .get("best_bid_size")
                    .map(|v| parse_cb_decimal(v, "best_bid_size"))
                    .transpose()?
                    .map(|v| quantize_dp(v, qty_u))
                    .unwrap_or(Decimal::ONE);
                let ask_size = obj
                    .get("best_ask_size")
                    .map(|v| parse_cb_decimal(v, "best_ask_size"))
                    .transpose()?
                    .map(|v| quantize_dp(v, qty_u))
                    .unwrap_or(Decimal::ONE);
                let ts_event = Self::parse_rfc3339_ms(obj.get("time"), now);

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
            "match" | "last_match" => {
                let (Some(px_v), Some(sz_v)) = (obj.get("price"), obj.get("size")) else {
                    return Ok(Vec::new());
                };
                let price = quantize_dp(parse_cb_decimal(px_v, "price")?, price_u);
                let size = quantize_dp(parse_cb_decimal(sz_v, "size")?, qty_u);
                let ts_event = Self::parse_rfc3339_ms(obj.get("time"), now);
                let mut out = Vec::new();

                let tfs: Vec<String> = self
                    .subscriptions
                    .iter()
                    .filter(|(id, _)| id == &instrument_id)
                    .map(|(_, tf)| tf.clone())
                    .collect();

                for tf in tfs {
                    let interval = timeframe_ms(&tf)?;
                    let bucket = ts_event - ts_event.rem_euclid(interval);
                    let key = (instrument_id.clone(), tf.clone());
                    let turnover_delta = quantize_dp(price * size, price_u + qty_u);

                    if let Some(prev) = self.candles.get(&key)
                        && bucket > prev.ts_event
                    {
                        let closed = prev.clone();
                        let status = classify_flat_bar(
                            closed.open,
                            closed.high,
                            closed.low,
                            closed.close,
                            closed.volume,
                        );
                        out.push(TransportFrame::ReceivedKline(ReceivedKlineEvent {
                            event: KlineEvent {
                                instrument_id: instrument_id.clone(),
                                timeframe: tf.clone(),
                                bar: CompactBar {
                                    timestamp: closed.ts_event,
                                    open: Some(closed.open),
                                    high: Some(closed.high),
                                    low: Some(closed.low),
                                    close: Some(closed.close),
                                    volume: Some(closed.volume),
                                    turnover: Some(closed.turnover),
                                    status,
                                    ts_available: closed.ts_event + interval,
                                    precision: PrecisionTier::SecondVerified,
                                },
                                confirmed: true,
                                revision: closed.revision + 1,
                            },
                            ts_ingested: now,
                            history_only: false,
                            synthetic: false,
                        }));
                    }

                    let cur = match self.candles.get_mut(&key) {
                        Some(existing) if existing.ts_event == bucket => {
                            if price > existing.high {
                                existing.high = price;
                            }
                            if price < existing.low {
                                existing.low = price;
                            }
                            existing.close = price;
                            existing.volume = quantize_dp(existing.volume + size, qty_u);
                            existing.turnover =
                                quantize_dp(existing.turnover + turnover_delta, price_u + qty_u);
                            existing.revision += 1;
                            existing.clone()
                        }
                        _ => {
                            let fresh = CandleAccum {
                                ts_event: bucket,
                                open: price,
                                high: price,
                                low: price,
                                close: price,
                                volume: size,
                                turnover: turnover_delta,
                                revision: 1,
                            };
                            self.candles.insert(key, fresh.clone());
                            fresh
                        }
                    };
                    let status =
                        classify_flat_bar(cur.open, cur.high, cur.low, cur.close, cur.volume);
                    out.push(TransportFrame::ReceivedKline(ReceivedKlineEvent {
                        event: KlineEvent {
                            instrument_id: instrument_id.clone(),
                            timeframe: tf,
                            bar: CompactBar {
                                timestamp: cur.ts_event,
                                open: Some(cur.open),
                                high: Some(cur.high),
                                low: Some(cur.low),
                                close: Some(cur.close),
                                volume: Some(cur.volume),
                                turnover: Some(cur.turnover),
                                status,
                                ts_available: ts_event,
                                precision: PrecisionTier::SecondVerified,
                            },
                            confirmed: false,
                            revision: cur.revision,
                        },
                        ts_ingested: now,
                        history_only: false,
                        synthetic: false,
                    }));
                }

                if self.persist_public_trades {
                    let tid = obj.get("trade_id").and_then(Value::as_u64).unwrap_or(0);
                    let side = match obj.get("side").and_then(Value::as_str).unwrap_or("") {
                        "buy" => TradeSide::Buy,
                        _ => TradeSide::Sell,
                    };
                    let synthetic_uuid =
                        deterministic_packet_uuid(&[format!("cb:{product_id}:{tid}:{ts_event}")]);
                    let event = parse_trade_event(
                        &instrument_id,
                        &synthetic_uuid,
                        side,
                        &price.to_string(),
                        &size.to_string(),
                        ts_event,
                        now,
                    )?;
                    let pkt = deterministic_packet_uuid(std::slice::from_ref(&synthetic_uuid));
                    out.push(TransportFrame::ReceivedTrade(ReceivedTradeEvent {
                        event,
                        ts_published: Some(ts_event),
                        sequence: Some(tid as i64),
                        packet_id: Some(pkt),
                        packet_index: Some(0),
                    }));
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

/// Real-time Coinbase Exchange WebSocket transport (`FeedTransport`).
pub struct CoinbaseSpotStream {
    instruments: BTreeMap<String, InstrumentSpec>,
    config: RealtimeStreamConfig,
    ws_url: String,
    parser: Arc<Mutex<CoinbaseWsParser>>,
    lifecycle: Mutex<LifecycleState>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    events_rx: Mutex<mpsc::UnboundedReceiver<EventResult>>,
    pending_frames: Mutex<VecDeque<TransportFrame>>,
}

impl CoinbaseSpotStream {
    pub fn new(instruments: &[InstrumentSpec], config: RealtimeStreamConfig) -> Self {
        let mut by_id = BTreeMap::new();
        for spec in instruments {
            by_id.insert(spec.instrument_id.clone(), spec.clone());
        }
        let ws_url = config
            .ws_url
            .clone()
            .unwrap_or_else(|| DEFAULT_WS_URL.to_owned());
        let parser = Arc::new(Mutex::new(CoinbaseWsParser::new(
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

    fn subscribe_payload(&self, product_id: &str, op: &str) -> String {
        json!({
            "type": op,
            "product_ids": [product_id],
            "channels": ["ticker", "matches"]
        })
        .to_string()
    }

    async fn spawn_socket(
        url: String,
        ping_interval: Option<Duration>,
        ping_timeout: Duration,
        ack_timeout: Duration,
        parser: Arc<Mutex<CoinbaseWsParser>>,
        events_tx: mpsc::UnboundedSender<EventResult>,
    ) -> Result<StreamConn, PackError> {
        let (ws_stream, _) = tokio::time::timeout(ack_timeout, connect_async(&url))
            .await
            .map_err(|_| PackError::FeedTransport(format!("Coinbase WS connect timeout: {url}")))?
            .map_err(|err| PackError::FeedTransport(format!("Coinbase WS connect error: {err}")))?;

        let (mut write, mut read) = ws_stream.split();
        let (cmd_tx, mut cmd_rx) = mpsc::unbounded_channel::<WsCmd>();

        let worker = tokio::spawn(async move {
            let mut last_rx = Instant::now();
            let mut awaiting_pong_since: Option<Instant> = None;
            let tick_dur = ping_interval
                .map(|iv| (iv / 2).min(ping_timeout).max(Duration::from_millis(250)))
                .unwrap_or(Duration::from_secs(20));
            let mut ping_ticker = tokio::time::interval(tick_dur);
            ping_ticker.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
            ping_ticker.tick().await;

            loop {
                tokio::select! {
                    biased;
                    cmd = cmd_rx.recv() => {
                        match cmd {
                            Some(WsCmd::Send { payload, ack_tx }) => {
                                let res = match tokio::time::timeout(
                                    ack_timeout,
                                    write.send(Message::Text(payload.into())),
                                )
                                .await
                                {
                                    Ok(Ok(())) => Ok(()),
                                    Ok(Err(err)) => Err(PackError::FeedTransport(format!(
                                        "Coinbase WS send error: {err}"
                                    ))),
                                    Err(_) => Err(PackError::FeedTransport(
                                        "Coinbase WS send timeout".to_owned(),
                                    )),
                                };
                                let failed = res.is_err();
                                let _ = ack_tx.send(res);
                                if failed {
                                    let _ = events_tx.send(Err(PackError::FeedTransport(
                                        "Coinbase WS write failed".to_owned(),
                                    )));
                                    break;
                                }
                            }
                            Some(WsCmd::Close) | None => {
                                let _ = tokio::time::timeout(
                                    Duration::from_secs(1),
                                    write.send(Message::Close(None)),
                                )
                                .await;
                                break;
                            }
                        }
                    }
                    _ = ping_ticker.tick(), if ping_interval.is_some() => {
                        if let Some(sent_at) = awaiting_pong_since {
                            if sent_at.elapsed() >= ping_timeout {
                                warn!("Coinbase WS ping timeout after {:?}", sent_at.elapsed());
                                let _ = events_tx.send(Err(PackError::FeedTransport(
                                    "Coinbase WS ping timeout".to_owned(),
                                )));
                                break;
                            }
                        } else if let Some(iv) = ping_interval
                            && last_rx.elapsed() >= iv / 2
                        {
                            match tokio::time::timeout(
                                ping_timeout,
                                write.send(Message::Ping(Vec::new().into())),
                            )
                            .await
                            {
                                Ok(Ok(())) => {
                                    awaiting_pong_since = Some(Instant::now());
                                }
                                Ok(Err(err)) => {
                                    let _ = events_tx.send(Err(PackError::FeedTransport(format!(
                                        "Coinbase WS ping failed: {err}"
                                    ))));
                                    break;
                                }
                                Err(_) => {
                                    let _ = events_tx.send(Err(PackError::FeedTransport(
                                        "Coinbase WS ping send timeout".to_owned(),
                                    )));
                                    break;
                                }
                            }
                        }
                    }
                    incoming = read.next() => {
                        match incoming {
                            Some(Ok(Message::Text(txt))) => {
                                last_rx = Instant::now();
                                awaiting_pong_since = None;
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
                                awaiting_pong_since = None;
                                let _ = tokio::time::timeout(
                                    ping_timeout,
                                    write.send(Message::Pong(p)),
                                )
                                .await;
                            }
                            Some(Ok(Message::Pong(_))) => {
                                last_rx = Instant::now();
                                awaiting_pong_since = None;
                            }
                            Some(Ok(Message::Close(_))) | None => {
                                let _ = events_tx.send(Err(PackError::FeedTransport("Coinbase WS closed".to_owned())));
                                break;
                            }
                            Some(Ok(_)) => {
                                last_rx = Instant::now();
                                awaiting_pong_since = None;
                            }
                            Some(Err(err)) => {
                                warn!("Coinbase WS read error: {err}");
                                let _ = events_tx.send(Err(PackError::FeedTransport(format!("Coinbase WS read error: {err}"))));
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

impl FeedTransport for CoinbaseSpotStream {
    fn feed_mode(&self) -> &str {
        "coinbase"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            if state.closed {
                return Err(PackError::FeedTransport("Coinbase stream closed".to_owned()));
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
                self.config.ping_timeout,
                self.config.ack_timeout,
                Arc::clone(&self.parser),
                self.events_tx.clone(),
            )
            .await?;

            let mut products = BTreeSet::new();
            for (inst_id, _) in &state.subscriptions {
                if let Some(spec) = self.instruments.get(inst_id) {
                    products.insert(spec.symbol.clone());
                }
            }
            for product in products {
                let payload = self.subscribe_payload(&product, "subscribe");
                let (tx, rx) = oneshot::channel();
                conn.cmd_tx
                    .send(WsCmd::Send { payload, ack_tx: tx })
                    .map_err(|_| PackError::FeedTransport("Coinbase WS disconnected".to_owned()))?;
                tokio::time::timeout(self.config.ack_timeout, rx)
                    .await
                    .map_err(|_| PackError::FeedTransport("Coinbase resubscribe timeout".to_owned()))?
                    .map_err(|_| PackError::FeedTransport("Coinbase resubscribe dropped".to_owned()))??;
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
            let payload = self.subscribe_payload(&spec.symbol, "subscribe");
            let mut state = self.lifecycle.lock().await;
            let first_for_product = !state.subscriptions.iter().any(|(id, _)| id == instrument_id);
            state
                .subscriptions
                .insert((instrument_id.to_owned(), timeframe.to_owned()));
            self.parser.lock().await.add_subscription(instrument_id, timeframe);
            if first_for_product && let Some(conn) = state.connection.as_ref() {
                let (tx, rx) = oneshot::channel();
                conn.cmd_tx
                    .send(WsCmd::Send { payload, ack_tx: tx })
                    .map_err(|_| PackError::FeedTransport("Coinbase WS disconnected".to_owned()))?;
                tokio::time::timeout(self.config.ack_timeout, rx)
                    .await
                    .map_err(|_| PackError::FeedTransport("Coinbase subscribe timeout".to_owned()))?
                    .map_err(|_| PackError::FeedTransport("Coinbase subscribe dropped".to_owned()))??;
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
            let mut state = self.lifecycle.lock().await;
            state
                .subscriptions
                .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
            self.parser
                .lock()
                .await
                .remove_subscription(instrument_id, timeframe);
            let still_subscribed = state.subscriptions.iter().any(|(id, _)| id == instrument_id);
            if !still_subscribed && let Some(conn) = state.connection.as_ref() {
                let payload = self.subscribe_payload(&spec.symbol, "unsubscribe");
                let (tx, _) = oneshot::channel();
                let _ = conn.cmd_tx.send(WsCmd::Send { payload, ack_tx: tx });
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
                None => Err(PackError::FeedTransport("Coinbase stream closed".to_owned())),
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
