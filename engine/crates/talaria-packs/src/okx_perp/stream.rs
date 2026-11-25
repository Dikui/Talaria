//! OKX V5 dual-endpoint public WebSocket transport (port 443 `/ws/v5/business` + `/ws/v5/public`).

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::Arc;
use std::time::{Duration, Instant};

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
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
use crate::okx_perp::connector::{
    okx_bar_for, parse_okx_decimal_str, parse_okx_i64, parse_okx_kline_row, quantize_dp,
};
use crate::okx_perp::precision_for_okx_symbol;
use crate::traits::{
    BoxFuture, ClockFn, FeedTransport, RealtimeStreamConfig, ReceivedTradeEvent, TransportFrame,
};
use crate::uuid_util::{ConnectionStamp, deterministic_packet_uuid, parse_trade_event};

/// OKX V5 Business WS endpoint (candles live here on port 443).
pub const DEFAULT_BUSINESS_WS_URL: &str = "wss://ws.okx.com:443/ws/v5/business";
/// OKX V5 Public WS endpoint (`bbo-tbt`, `mark-price`, `funding-rate`, `open-interest`, `trades`).
pub const DEFAULT_PUBLIC_WS_URL: &str = "wss://ws.okx.com:443/ws/v5/public";
const RATE_SCALE: u32 = 8;

/// Map OKX WS channel like `"candle1m"` / `"candle1H"` / `"candle1Dutc"` -> Talaria timeframe.
pub fn timeframe_from_okx_channel(channel: &str) -> Option<&'static str> {
    match channel {
        "candle1m" => Some("1m"),
        "candle5m" => Some("5m"),
        "candle15m" => Some("15m"),
        "candle1H" => Some("1h"),
        "candle4H" => Some("4h"),
        "candle1Dutc" | "candle1D" => Some("1d"),
        _ => None,
    }
}

#[derive(Default)]
struct BookTracker {
    stamp: Option<ConnectionStamp>,
    last_update_id: u64,
}

/// Stateful OKX V5 WS parser shared between live connections and unit/offline replay tests.
pub struct OkxWsParser {
    instrument_by_symbol: BTreeMap<String, String>,
    now_ms: ClockFn,
    persist_public_trades: bool,
    subscriptions: BTreeSet<(String, String)>,
    book_trackers: BTreeMap<String, BookTracker>,
}

impl OkxWsParser {
    pub fn new(instruments: &[InstrumentSpec], now_ms: ClockFn, persist_public_trades: bool) -> Self {
        let mut instrument_by_symbol = BTreeMap::new();
        for spec in instruments {
            instrument_by_symbol.insert(spec.symbol.clone(), spec.instrument_id.clone());
        }
        Self {
            instrument_by_symbol,
            now_ms,
            persist_public_trades,
            subscriptions: BTreeSet::new(),
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

    /// Parse one raw text WebSocket message into zero or more canonical `TransportFrame`s.
    pub fn decode_text(&mut self, text: &str) -> Result<Vec<TransportFrame>, PackError> {
        if text == "pong" || text.trim().is_empty() {
            return Ok(Vec::new());
        }
        let payload: Value = serde_json::from_str(text).map_err(|err| {
            PackError::FeedTransport(format!("invalid OKX WS JSON message: {err}"))
        })?;
        let Some(obj) = payload.as_object() else {
            return Ok(Vec::new());
        };
        if let Some(ev) = obj.get("event").and_then(Value::as_str) {
            if ev == "error" {
                let msg = obj
                    .get("msg")
                    .and_then(Value::as_str)
                    .unwrap_or("unknown OKX WS error");
                return Err(PackError::FeedTransport(format!("OKX WS error: {msg}")));
            }
            return Ok(Vec::new());
        }

        let Some(arg) = obj.get("arg").and_then(Value::as_object) else {
            return Ok(Vec::new());
        };
        let channel = arg.get("channel").and_then(Value::as_str).unwrap_or("");
        let symbol = arg.get("instId").and_then(Value::as_str).unwrap_or("");
        let Some(instrument_id) = self.instrument_by_symbol.get(symbol).cloned() else {
            return Ok(Vec::new());
        };
        let Some(data) = obj.get("data").and_then(Value::as_array) else {
            return Ok(Vec::new());
        };
        let now = (self.now_ms)();

        if let Some(tf) = timeframe_from_okx_channel(channel) {
            let interval = talaria_contracts::timeframe_ms(tf)?;
            let mut out = Vec::with_capacity(data.len());
            for row in data {
                if let Some((raw_bar, is_closed)) =
                    parse_okx_kline_row(row, symbol, tf, interval, false, Some(now), Some(now))?
                {
                    let status = classify_flat_bar(raw_bar.open, raw_bar.high, raw_bar.low, raw_bar.close, raw_bar.volume);
                    let compact = CompactBar {
                        timestamp: raw_bar.ts_event,
                        open: Some(raw_bar.open),
                        high: Some(raw_bar.high),
                        low: Some(raw_bar.low),
                        close: Some(raw_bar.close),
                        volume: Some(raw_bar.volume),
                        turnover: Some(raw_bar.turnover),
                        status,
                        ts_available: if is_closed { raw_bar.ts_available } else { now },
                        precision: PrecisionTier::SecondVerified,
                    };
                    let kline = KlineEvent {
                        instrument_id: instrument_id.clone(),
                        timeframe: tf.to_owned(),
                        bar: compact,
                        confirmed: is_closed,
                        revision: 1,
                    };
                    out.push(TransportFrame::ReceivedKline(ReceivedKlineEvent {
                        event: kline,
                        ts_ingested: now,
                        history_only: false,
                        synthetic: false,
                    }));
                }
            }
            return Ok(out);
        }

        if channel == "bbo-tbt" || channel == "tickers" {
            let (price_dp, qty_dp) = precision_for_okx_symbol(symbol);
            let mut out = Vec::new();
            for item in data {
                let Some(row) = item.as_object() else { continue };
                let (bid_p, bid_s, ask_p, ask_s, ts_val) = if channel == "bbo-tbt" {
                    let bids = row.get("bids").and_then(Value::as_array).and_then(|a| a.first()).and_then(Value::as_array);
                    let asks = row.get("asks").and_then(Value::as_array).and_then(|a| a.first()).and_then(Value::as_array);
                    let (Some(b), Some(a)) = (bids, asks) else { continue };
                    if b.len() < 2 || a.len() < 2 { continue; }
                    (&b[0], &b[1], &a[0], &a[1], row.get("ts"))
                } else {
                    let (Some(bp), Some(bs), Some(ap), Some(as_)) = (
                        row.get("bidPx"),
                        row.get("bidSz"),
                        row.get("askPx"),
                        row.get("askSz"),
                    ) else {
                        continue;
                    };
                    (bp, bs, ap, as_, row.get("ts"))
                };
                let ts_event = ts_val.map(|v| parse_okx_i64(v, "ts")).transpose()?.unwrap_or(now);
                let bid_price = quantize_dp(parse_okx_decimal_str(bid_p, "bidPx")?, price_dp.max(0) as u32);
                let bid_size = quantize_dp(parse_okx_decimal_str(bid_s, "bidSz")?, qty_dp.max(0) as u32);
                let ask_price = quantize_dp(parse_okx_decimal_str(ask_p, "askPx")?, price_dp.max(0) as u32);
                let ask_size = quantize_dp(parse_okx_decimal_str(ask_s, "askSz")?, qty_dp.max(0) as u32);

                let tracker = self.book_trackers.entry(instrument_id.clone()).or_default();
                if tracker.stamp.is_none() {
                    tracker.stamp = Some(ConnectionStamp::fresh(&instrument_id, now)?);
                }
                tracker.last_update_id += 1;
                let update_id = tracker.last_update_id;
                let stamp = tracker.stamp.as_ref().expect("initialized");

                let event = OrderBookEvent {
                    instrument_id: instrument_id.clone(),
                    bid_price,
                    bid_size,
                    ask_price,
                    ask_size,
                    ts_event,
                    ts_ingested: now,
                };
                out.push(TransportFrame::ReceivedOrderBook(
                    stamp.build_received_order_book(event, Some(ts_event), update_id),
                ));
            }
            return Ok(out);
        }

        if channel == "mark-price" {
            let (price_dp, _) = precision_for_okx_symbol(symbol);
            let mut out = Vec::new();
            for item in data {
                let Some(row) = item.as_object() else { continue };
                let (Some(px_v), Some(ts_v)) = (row.get("markPx"), row.get("ts")) else {
                    continue;
                };
                let ts_event = parse_okx_i64(ts_v, "ts")?;
                let val = quantize_dp(parse_okx_decimal_str(px_v, "markPx")?, price_dp.max(0) as u32);
                out.push(TransportFrame::Event(TransportEvent::DerivativeTick(
                    DerivativeTickEvent {
                        instrument_id: instrument_id.clone(),
                        mark_price: Some(val),
                        funding_rate: None,
                        next_funding_ts: None,
                        open_interest: None,
                        ts_event,
                        ts_ingested: now,
                    },
                )));
            }
            return Ok(out);
        }

        if channel == "funding-rate" {
            let mut out = Vec::new();
            for item in data {
                let Some(row) = item.as_object() else { continue };
                let (Some(rate_v), Some(ts_v)) = (row.get("fundingRate"), row.get("fundingTime")) else {
                    continue;
                };
                let ts_event = parse_okx_i64(ts_v, "fundingTime")?;
                let val = quantize_dp(parse_okx_decimal_str(rate_v, "fundingRate")?, RATE_SCALE);
                out.push(TransportFrame::Event(TransportEvent::DerivativeTick(
                    DerivativeTickEvent {
                        instrument_id: instrument_id.clone(),
                        mark_price: None,
                        funding_rate: Some(val),
                        next_funding_ts: None,
                        open_interest: None,
                        ts_event,
                        ts_ingested: now,
                    },
                )));
            }
            return Ok(out);
        }

        if channel == "open-interest" {
            let (_, qty_dp) = precision_for_okx_symbol(symbol);
            let mut out = Vec::new();
            for item in data {
                let Some(row) = item.as_object() else { continue };
                let (Some(oi_v), Some(ts_v)) = (row.get("oiCcy"), row.get("ts")) else {
                    continue;
                };
                let ts_event = parse_okx_i64(ts_v, "ts")?;
                let val = quantize_dp(parse_okx_decimal_str(oi_v, "oiCcy")?, qty_dp.max(0) as u32);
                out.push(TransportFrame::Event(TransportEvent::DerivativeTick(
                    DerivativeTickEvent {
                        instrument_id: instrument_id.clone(),
                        mark_price: None,
                        funding_rate: None,
                        next_funding_ts: None,
                        open_interest: Some(val),
                        ts_event,
                        ts_ingested: now,
                    },
                )));
            }
            return Ok(out);
        }

        if channel == "trades" && self.persist_public_trades {
            let mut out = Vec::new();
            let mut packet_trade_ids = Vec::new();
            let mut parsed_rows = Vec::new();
            for item in data {
                let Some(row) = item.as_object() else { continue };
                let (Some(tid_v), Some(px_v), Some(sz_v), Some(side_v), Some(ts_v)) = (
                    row.get("tradeId"),
                    row.get("px"),
                    row.get("sz"),
                    row.get("side"),
                    row.get("ts"),
                ) else {
                    continue;
                };
                let tid = tid_v.as_str().unwrap_or("");
                let px = px_v.as_str().unwrap_or("0");
                let sz = sz_v.as_str().unwrap_or("0");
                let side = match side_v.as_str().unwrap_or("") {
                    "buy" => TradeSide::Buy,
                    _ => TradeSide::Sell,
                };
                let ts_event = parse_okx_i64(ts_v, "ts")?;
                let synthetic_uuid = deterministic_packet_uuid(&[format!("okx:{symbol}:{tid}")]);
                let trade_ev =
                    parse_trade_event(&instrument_id, &synthetic_uuid, side, px, sz, ts_event, now)?;
                packet_trade_ids.push(synthetic_uuid);
                parsed_rows.push((trade_ev, ts_event, tid.parse::<i64>().ok()));
            }
            if !parsed_rows.is_empty() {
                let pkt_id = deterministic_packet_uuid(&packet_trade_ids);
                for (idx, (event, ts_pub, seq)) in parsed_rows.into_iter().enumerate() {
                    out.push(TransportFrame::ReceivedTrade(ReceivedTradeEvent {
                        event,
                        ts_published: Some(ts_pub),
                        sequence: seq,
                        packet_id: Some(pkt_id.clone()),
                        packet_index: Some(idx as i64),
                    }));
                }
            }
            return Ok(out);
        }

        let _ = Decimal::ZERO;
        Ok(Vec::new())
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

struct OkxConn {
    cmd_tx: mpsc::UnboundedSender<WsCmd>,
    worker: JoinHandle<()>,
}

struct LifecycleState {
    closed: bool,
    connections: Vec<OkxConn>,
    subscriptions: BTreeSet<(String, String)>,
}

impl Drop for LifecycleState {
    fn drop(&mut self) {
        for conn in self.connections.drain(..) {
            conn.worker.abort();
        }
    }
}

/// Real-time OKX V5 WebSocket transport (`FeedTransport`).
pub struct OkxPerpStream {
    instruments: BTreeMap<String, InstrumentSpec>,
    config: RealtimeStreamConfig,
    public_ws_url: String,
    business_ws_url: String,
    parser: Arc<Mutex<OkxWsParser>>,
    lifecycle: Mutex<LifecycleState>,
    events_tx: mpsc::UnboundedSender<EventResult>,
    events_rx: Mutex<mpsc::UnboundedReceiver<EventResult>>,
    pending_frames: Mutex<VecDeque<TransportFrame>>,
}

impl OkxPerpStream {
    pub fn new(instruments: &[InstrumentSpec], config: RealtimeStreamConfig) -> Self {
        let mut by_id = BTreeMap::new();
        for spec in instruments {
            by_id.insert(spec.instrument_id.clone(), spec.clone());
        }
        let business_ws_url = config
            .ws_url
            .clone()
            .unwrap_or_else(|| DEFAULT_BUSINESS_WS_URL.to_owned());
        let public_ws_url = config
            .ws_url
            .clone()
            .unwrap_or_else(|| DEFAULT_PUBLIC_WS_URL.to_owned());

        let now_fn = Arc::clone(&config.now_ms);
        let parser = Arc::new(Mutex::new(OkxWsParser::new(
            instruments,
            now_fn,
            config.persist_public_trades,
        )));
        let (events_tx, events_rx) = mpsc::unbounded_channel();

        Self {
            instruments: by_id,
            config,
            public_ws_url,
            business_ws_url,
            parser,
            lifecycle: Mutex::new(LifecycleState {
                closed: false,
                connections: Vec::new(),
                subscriptions: BTreeSet::new(),
            }),
            events_tx,
            events_rx: Mutex::new(events_rx),
            pending_frames: Mutex::new(VecDeque::new()),
        }
    }

    fn build_subscribe_args(&self, symbol: &str, timeframe: &str, op: &str) -> Result<(String, Option<String>), PackError> {
        let bar_chan = format!("candle{}", okx_bar_for(timeframe)?);
        let biz_msg = json!({
            "op": op,
            "args": [{"channel": bar_chan, "instId": symbol}],
        })
        .to_string();

        if self.business_ws_url == self.public_ws_url {
            let mut args = vec![
                json!({"channel": bar_chan, "instId": symbol}),
                json!({"channel": "bbo-tbt", "instId": symbol}),
                json!({"channel": "mark-price", "instId": symbol}),
                json!({"channel": "funding-rate", "instId": symbol}),
                json!({"channel": "open-interest", "instId": symbol}),
            ];
            if self.config.persist_public_trades {
                args.push(json!({"channel": "trades", "instId": symbol}));
            }
            return Ok((json!({"op": op, "args": args}).to_string(), None));
        }

        let mut pub_args = vec![
            json!({"channel": "bbo-tbt", "instId": symbol}),
            json!({"channel": "mark-price", "instId": symbol}),
            json!({"channel": "funding-rate", "instId": symbol}),
            json!({"channel": "open-interest", "instId": symbol}),
        ];
        if self.config.persist_public_trades {
            pub_args.push(json!({"channel": "trades", "instId": symbol}));
        }
        Ok((biz_msg, Some(json!({"op": op, "args": pub_args}).to_string())))
    }

    async fn spawn_socket(
        url: String,
        ping_interval: Option<Duration>,
        ack_timeout: Duration,
        parser: Arc<Mutex<OkxWsParser>>,
        events_tx: mpsc::UnboundedSender<EventResult>,
    ) -> Result<OkxConn, PackError> {
        let (ws_stream, _) = tokio::time::timeout(ack_timeout, connect_async(&url))
            .await
            .map_err(|_| PackError::FeedTransport(format!("OKX WS connect timeout: {url}")))?
            .map_err(|err| PackError::FeedTransport(format!("OKX WS connect error: {err}")))?;

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
                                    .map_err(|err| PackError::FeedTransport(format!("OKX WS send error: {err}")));
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
                            && last_rx.elapsed() >= iv / 2
                            && let Err(err) = write.send(Message::Text("ping".into())).await
                        {
                            let _ = events_tx.send(Err(PackError::FeedTransport(format!("OKX WS ping failed: {err}"))));
                            break;
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
                            Some(Ok(Message::Pong(_))) => {
                                last_rx = Instant::now();
                            }
                            Some(Ok(Message::Close(_))) | None => {
                                let _ = events_tx.send(Err(PackError::FeedTransport("OKX WS closed".to_owned())));
                                break;
                            }
                            Some(Ok(_)) => { last_rx = Instant::now(); }
                            Some(Err(err)) => {
                                warn!("OKX WS read error: {err}");
                                let _ = events_tx.send(Err(PackError::FeedTransport(format!("OKX WS read error: {err}"))));
                                break;
                            }
                        }
                    }
                }
            }
        });

        Ok(OkxConn { cmd_tx, worker })
    }
}

impl FeedTransport for OkxPerpStream {
    fn feed_mode(&self) -> &str {
        "okx"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            if state.closed {
                return Err(PackError::FeedTransport("OKX stream already closed".to_owned()));
            }
            for old in state.connections.drain(..) {
                old.worker.abort();
            }
            {
                let mut rx = self.events_rx.lock().await;
                while rx.try_recv().is_ok() {}
            }
            self.pending_frames.lock().await.clear();
            self.parser.lock().await.reset_connection_state();

            let conn_biz = Self::spawn_socket(
                self.business_ws_url.clone(),
                self.config.ping_interval,
                self.config.ack_timeout,
                Arc::clone(&self.parser),
                self.events_tx.clone(),
            )
            .await?;
            state.connections.push(conn_biz);

            if self.public_ws_url != self.business_ws_url {
                let conn_pub = Self::spawn_socket(
                    self.public_ws_url.clone(),
                    self.config.ping_interval,
                    self.config.ack_timeout,
                    Arc::clone(&self.parser),
                    self.events_tx.clone(),
                )
                .await?;
                state.connections.push(conn_pub);
            }

            let existing: Vec<_> = state.subscriptions.iter().cloned().collect();
            for (inst_id, tf) in existing {
                if let Some(spec) = self.instruments.get(&inst_id) {
                    let (biz_msg, pub_msg) = self.build_subscribe_args(&spec.symbol, &tf, "subscribe")?;
                    if let Some(c0) = state.connections.first() {
                        let (tx, rx) = oneshot::channel();
                        let _ = c0.cmd_tx.send(WsCmd::Send { payload: biz_msg, ack_tx: tx });
                        let _ = rx.await;
                    }
                    if let (Some(msg), Some(c1)) = (pub_msg, state.connections.get(1)) {
                        let (tx, rx) = oneshot::channel();
                        let _ = c1.cmd_tx.send(WsCmd::Send { payload: msg, ack_tx: tx });
                        let _ = rx.await;
                    }
                }
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
            let spec = self
                .instruments
                .get(instrument_id)
                .ok_or_else(|| PackError::InstrumentNotFound(instrument_id.to_owned()))?;
            let (biz_msg, pub_msg) = self.build_subscribe_args(&spec.symbol, timeframe, "subscribe")?;
            let mut state = self.lifecycle.lock().await;
            state
                .subscriptions
                .insert((instrument_id.to_owned(), timeframe.to_owned()));
            self.parser.lock().await.add_subscription(instrument_id, timeframe);

            if let Some(c0) = state.connections.first() {
                let (tx, rx) = oneshot::channel();
                c0.cmd_tx
                    .send(WsCmd::Send { payload: biz_msg, ack_tx: tx })
                    .map_err(|_| PackError::FeedTransport("OKX business WS disconnected".to_owned()))?;
                rx.await
                    .map_err(|_| PackError::FeedTransport("OKX subscribe dropped".to_owned()))??;
            }
            if let (Some(msg), Some(c1)) = (pub_msg, state.connections.get(1)) {
                let (tx, rx) = oneshot::channel();
                c1.cmd_tx
                    .send(WsCmd::Send { payload: msg, ack_tx: tx })
                    .map_err(|_| PackError::FeedTransport("OKX public WS disconnected".to_owned()))?;
                rx.await
                    .map_err(|_| PackError::FeedTransport("OKX public subscribe dropped".to_owned()))??;
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
            let (biz_msg, pub_msg) =
                self.build_subscribe_args(&spec.symbol, timeframe, "unsubscribe")?;
            let mut state = self.lifecycle.lock().await;
            state
                .subscriptions
                .remove(&(instrument_id.to_owned(), timeframe.to_owned()));
            self.parser
                .lock()
                .await
                .remove_subscription(instrument_id, timeframe);

            if let Some(c0) = state.connections.first() {
                let (tx, _) = oneshot::channel();
                let _ = c0.cmd_tx.send(WsCmd::Send { payload: biz_msg, ack_tx: tx });
            }
            if let (Some(msg), Some(c1)) = (pub_msg, state.connections.get(1)) {
                let (tx, _) = oneshot::channel();
                let _ = c1.cmd_tx.send(WsCmd::Send { payload: msg, ack_tx: tx });
            }
            Ok(())
        })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            {
                let mut q = self.pending_frames.lock().await;
                if let Some(frame) = q.pop_front() {
                    return Ok(Some(frame));
                }
            }
            let mut rx = self.events_rx.lock().await;
            match rx.recv().await {
                Some(res) => res,
                None => Err(PackError::FeedTransport("OKX stream channel closed".to_owned())),
            }
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut state = self.lifecycle.lock().await;
            state.closed = true;
            for conn in state.connections.drain(..) {
                let _ = conn.cmd_tx.send(WsCmd::Close);
                conn.worker.abort();
            }
            Ok(())
        })
    }
}
