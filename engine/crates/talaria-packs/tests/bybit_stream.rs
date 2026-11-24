//! Tests for `BybitPublicStream` using a local `tokio-tungstenite` WS mock server.

use std::future::Future;
use std::pin::Pin;
use std::str::FromStr;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::{BarStatus, TradeSide, TransportEvent};
use talaria_packs::crypto_perp::stream::timeframe_from_bybit_interval;
use talaria_packs::{
    FeedTransport, PackError, RealtimeStreamConfig, TransportFrame, get_market_pack,
};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::{WebSocketStream, accept_async, tungstenite::Message};

const BASE: i64 = 1_763_596_800_000;
const MINUTE: i64 = 60_000;
const INSTRUMENT: &str = "BTC-USDT-PERP";

type ServerWs = WebSocketStream<TcpStream>;
type WsConnHandler = Box<dyn FnOnce(ServerWs) -> Pin<Box<dyn Future<Output = ()> + Send>> + Send>;

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

async fn run_ws_harness<H, Fut, S, SFut>(handler: H, scenario: S)
where
    H: Fn(ServerWs) -> Fut + Send + Sync + 'static,
    Fut: Future<Output = ()> + Send + 'static,
    S: FnOnce(String) -> SFut,
    SFut: Future<Output = ()>,
{
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback ws");
    let addr = listener.local_addr().unwrap();
    let handler = Arc::new(handler);

    let accept_task = tokio::spawn(async move {
        while let Ok((stream, _)) = listener.accept().await {
            let h = Arc::clone(&handler);
            tokio::spawn(async move {
                if let Ok(ws) = accept_async(stream).await {
                    h(ws).await;
                }
            });
        }
    });

    let url = format!("ws://{addr}");
    tokio::time::timeout(Duration::from_secs(5), scenario(url))
        .await
        .expect("ws scenario timed out");
    accept_task.abort();
}

async fn run_multi_ws_harness<F>(handlers: Vec<WsConnHandler>, scenario: impl FnOnce(String) -> F)
where
    F: Future<Output = ()>,
{
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let accept_task = tokio::spawn(async move {
        for handler in handlers {
            if let Ok((stream, _)) = listener.accept().await
                && let Ok(ws) = accept_async(stream).await
            {
                tokio::spawn(handler(ws));
            }
        }
    });
    tokio::time::timeout(Duration::from_secs(5), scenario(format!("ws://{addr}")))
        .await
        .expect("multi-conn ws scenario timed out");
    accept_task.abort();
}

fn kline_payload(symbol: &str, interval: &str, close: &str, volume: &str, confirm: bool, start: i64) -> Value {
    json!({
        "topic": format!("kline.{interval}.{symbol}"),
        "type": "snapshot",
        "ts": BASE,
        "data": [{
            "start": start,
            "end": start + MINUTE - 1,
            "interval": interval,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": volume,
            "turnover": "100",
            "confirm": confirm,
            "timestamp": start + 1_000,
        }]
    })
}

fn book_payload(symbol: &str, bid: &str, ask: &str, update_id: u64, msg_type: &str) -> Value {
    json!({
        "topic": format!("orderbook.1.{symbol}"),
        "type": msg_type,
        "ts": BASE + 500,
        "cts": BASE + 10,
        "data": {
            "s": symbol,
            "b": [[bid, "2.5"]],
            "a": [[ask, "3.5"]],
            "ts": BASE + 500,
            "u": update_id,
        }
    })
}

fn tickers_payload(symbol: &str) -> Value {
    json!({
        "topic": format!("tickers.{symbol}"),
        "type": "snapshot",
        "ts": BASE + 500,
        "data": {
            "symbol": symbol,
            "markPrice": "101.5",
            "fundingRate": "0.0001",
            "nextFundingTime": (BASE + 8 * 3_600_000).to_string(),
            "openInterest": "12345.6",
            "lastPrice": "101.4",
        }
    })
}

fn make_stream(url: &str, persist_public_trades: bool, now_ms: Arc<AtomicI64>) -> Arc<dyn FeedTransport> {
    let pack = get_market_pack("crypto_perp").unwrap();
    pack.create_realtime_stream(RealtimeStreamConfig {
        ws_url: Some(url.to_owned()),
        now_ms: Arc::new(move || now_ms.load(Ordering::SeqCst)),
        persist_public_trades,
        ack_timeout: Duration::from_secs(2),
        ping_interval: None,
        ping_timeout: Duration::from_secs(1),
        ..RealtimeStreamConfig::default()
    })
    .unwrap()
}

#[tokio::test]
async fn subscribe_ack_then_kline_book_tickers_and_trade() {
    let seen_req = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let seen_bg = Arc::clone(&seen_req);
    let now = Arc::new(AtomicI64::new(BASE + 1_000));

    run_ws_harness(
        move |mut ws| {
            let seen_bg = Arc::clone(&seen_bg);
            async move {
                let raw = ws.next().await.unwrap().unwrap();
                seen_bg
                    .lock()
                    .await
                    .push(serde_json::from_str(raw.to_text().unwrap()).unwrap());
                ws.send(Message::Text(
                    r#"{"success":true,"ret_msg":"subscribe","op":"subscribe"}"#.into(),
                ))
                .await
                .unwrap();
                ws.send(Message::Text(
                    kline_payload("BTCUSDT", "1", "100", "1", false, BASE)
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                ws.send(Message::Text(
                    book_payload("BTCUSDT", "100.0", "100.1", 1, "snapshot")
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                ws.send(Message::Text(
                    tickers_payload("BTCUSDT").to_string().into(),
                ))
                .await
                .unwrap();
                let _ = ws.next().await;
            }
        },
        |url| async move {
            let stream = make_stream(&url, true, now);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();

            let f1 = stream.recv().await.unwrap().unwrap();
            let f2 = stream.recv().await.unwrap().unwrap();
            let f3 = stream.recv().await.unwrap().unwrap();
            stream.close().await.unwrap();

            let reqs = seen_req.lock().await;
            assert_eq!(reqs[0]["op"], "subscribe");
            assert_eq!(
                reqs[0]["args"],
                json!([
                    "kline.1.BTCUSDT",
                    "orderbook.1.BTCUSDT",
                    "tickers.BTCUSDT",
                    "publicTrade.BTCUSDT"
                ])
            );

            match f1 {
                TransportFrame::ReceivedKline(rk) => {
                    assert_eq!(rk.event.instrument_id, INSTRUMENT);
                    assert_eq!(rk.event.timeframe, "1m");
                    assert_eq!(rk.event.bar.timestamp, BASE);
                    assert_eq!(rk.event.bar.close, Some(dec("100")));
                    assert_eq!(rk.event.bar.status, BarStatus::Normal);
                    assert!(!rk.event.confirmed);
                    assert_eq!(rk.ts_ingested, BASE + 1_000);
                }
                other => panic!("expected ReceivedKline, got {other:?}"),
            }

            match f2 {
                TransportFrame::ReceivedOrderBook(rb) => {
                    assert_eq!(rb.event.bid_price, dec("100.0"));
                    assert_eq!(rb.event.ask_price, dec("100.1"));
                    assert_eq!(rb.event.bid_size, dec("2.5"));
                    assert_eq!(rb.update_id, 1);
                }
                other => panic!("expected ReceivedOrderBook, got {other:?}"),
            }

            match f3 {
                TransportFrame::Event(TransportEvent::DerivativeTick(dt)) => {
                    assert_eq!(dt.instrument_id, INSTRUMENT);
                    assert_eq!(dt.mark_price, Some(dec("101.5")));
                    assert_eq!(dt.funding_rate, Some(dec("0.0001")));
                    assert_eq!(dt.next_funding_ts, Some(BASE + 8 * 3_600_000));
                    assert_eq!(dt.open_interest, Some(dec("12345.6")));
                }
                other => panic!("expected DerivativeTick, got {other:?}"),
            }
        },
    )
    .await;
}

#[tokio::test]
async fn persist_public_trades_toggle_controls_subscription_and_emission() {
    let seen_args = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let seen_bg = Arc::clone(&seen_args);
    let now = Arc::new(AtomicI64::new(BASE + 1_000));

    run_ws_harness(
        move |mut ws| {
            let seen_bg = Arc::clone(&seen_bg);
            async move {
                let raw = ws.next().await.unwrap().unwrap();
                let v: Value = serde_json::from_str(raw.to_text().unwrap()).unwrap();
                seen_bg.lock().await.push(v["args"].clone());
                ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                    .await
                    .unwrap();
                ws.send(Message::Text(
                    json!({
                        "topic": "publicTrade.BTCUSDT",
                        "ts": BASE + 2000,
                        "data": [{
                            "s": "BTCUSDT", "T": BASE + 10, "S": "Buy", "p": "100.1", "v": "0.123",
                            "i": "11111111-0000-0000-0000-000000000004", "seq": 82
                        }]
                    })
                    .to_string()
                    .into(),
                ))
                .await
                .unwrap();
                ws.send(Message::Text(
                    kline_payload("BTCUSDT", "1", "100", "1", true, BASE)
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                let _ = ws.next().await;
            }
        },
        |url| async move {
            let stream = make_stream(&url, false, now);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            // Trade must be skipped because persist_public_trades = false; first received is kline.
            let frame = stream.recv().await.unwrap().unwrap();
            stream.close().await.unwrap();
            assert!(matches!(frame, TransportFrame::ReceivedKline(_)));
            let args = seen_args.lock().await;
            assert_eq!(
                args[0],
                json!(["kline.1.BTCUSDT", "orderbook.1.BTCUSDT", "tickers.BTCUSDT"])
            );
        },
    )
    .await;
}

#[tokio::test]
async fn public_trade_batch_preserves_packet_index_and_skips_broken_row() {
    let now = Arc::new(AtomicI64::new(BASE + 1_000));
    run_ws_harness(
        |mut ws| async move {
            let _ = ws.next().await;
            ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                .await
                .unwrap();
            let trade = json!({
                "s": "BTCUSDT", "T": BASE + 10, "S": "Buy", "p": "100.1", "v": "0.123",
                "i": "11111111-0000-0000-0000-000000000004", "seq": 82
            });
            let mut broken = trade.clone();
            broken["p"] = json!("broken");
            let mut second = trade.clone();
            second["S"] = json!("Sell");
            second["i"] = json!("11111111-0000-0000-0000-000000000002");

            ws.send(Message::Text(
                json!({
                    "topic": "publicTrade.BTCUSDT",
                    "ts": BASE + 2000,
                    "data": [trade, broken, second],
                })
                .to_string()
                .into(),
            ))
            .await
            .unwrap();
            let _ = ws.next().await;
        },
        |url| async move {
            let stream = make_stream(&url, true, now);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();

            let t1 = match stream.recv().await.unwrap().unwrap() {
                TransportFrame::ReceivedTrade(t) => t,
                other => panic!("expected ReceivedTrade, got {other:?}"),
            };
            let t2 = match stream.recv().await.unwrap().unwrap() {
                TransportFrame::ReceivedTrade(t) => t,
                other => panic!("expected ReceivedTrade, got {other:?}"),
            };
            stream.close().await.unwrap();

            assert_eq!(t1.event.side, TradeSide::Buy);
            assert_eq!(t1.packet_index, Some(0));
            assert_eq!(t1.sequence, Some(82));
            assert_eq!(t2.event.side, TradeSide::Sell);
            assert_eq!(t2.packet_index, Some(2));
            assert_eq!(t1.packet_id, t2.packet_id);
            assert!(t1.packet_id.is_some());

            let contract_trade = t1.to_trade(BASE + 3_000).unwrap();
            assert_eq!(contract_trade.price, dec("100.1"));
            assert_eq!(contract_trade.ts_available, BASE + 2_000);
        },
    )
    .await;
}

#[tokio::test]
async fn tickers_merge_deltas_per_instrument_and_skip_empty_readings() {
    let now = Arc::new(AtomicI64::new(BASE));
    let delta = |sym: &str, offset: i64, fields: Value| {
        json!({"topic": format!("tickers.{sym}"), "type": "delta", "ts": BASE + offset, "data": fields})
    };

    let messages = vec![
        tickers_payload("BTCUSDT"),
        delta("ETHUSDT", 600, json!({"symbol": "ETHUSDT", "lastPrice": "102"})),
        delta("BTCUSDT", 700, json!({"lastPrice": "102"})),
        delta("BTCUSDT", 800, json!({"fundingRate": "0"})),
        delta("BTCUSDT", 900, json!({"openInterest": "12346.7"})),
        delta("BTCUSDT", 750, json!({"fundingRate": "0.0002"})),
        delta("BTCUSDT", 950, json!({"lastPrice": "104"})),
        json!({"topic": "tickers.BTCUSDT", "type": "snapshot", "ts": BASE + 1000, "data": {"symbol": "BTCUSDT", "markPrice": "103"}}),
        delta("BTCUSDT", 1100, json!({"lastPrice": "104"})),
    ];

    run_ws_harness(
        move |mut ws| {
            let messages = messages.clone();
            async move {
                for _ in 0..2 {
                    let _ = ws.next().await;
                    ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                        .await
                        .unwrap();
                }
                for msg in messages {
                    ws.send(Message::Text(msg.to_string().into()))
                        .await
                        .unwrap();
                }
                let _ = ws.close(None).await;
            }
        },
        |url| async move {
            let stream = make_stream(&url, true, now);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            stream.subscribe("ETH-USDT-PERP", "1m").await.unwrap();

            let mut events = Vec::new();
            loop {
                match stream.recv().await {
                    Ok(Some(TransportFrame::Event(TransportEvent::DerivativeTick(ev)))) => {
                        events.push(ev);
                    }
                    Ok(_) => {}
                    Err(PackError::FeedTransport(_)) => break,
                    Err(other) => panic!("unexpected error: {other:?}"),
                }
            }
            stream.close().await.unwrap();

            let offsets: Vec<i64> = events.iter().map(|e| e.ts_event - BASE).collect();
            assert_eq!(offsets, vec![500, 700, 800, 900, 750, 950, 1000, 1100]);
            assert_eq!(events[1].mark_price, Some(dec("101.5")));
            assert_eq!(events[1].funding_rate, Some(dec("0.0001")));
            assert_eq!(events[3].funding_rate, Some(Decimal::ZERO));
            assert_eq!(events[3].open_interest, Some(dec("12346.7")));
            assert_eq!(events[3].next_funding_ts, Some(BASE + 8 * 3_600_000));
            assert_eq!(events[4].mark_price, Some(dec("101.5")));
            assert_eq!(events[4].open_interest, None);
            assert_eq!(events[4].funding_rate, Some(dec("0.0002")));
            assert_eq!(events[5].funding_rate, Some(Decimal::ZERO));
            assert_eq!(events[5].open_interest, Some(dec("12346.7")));
            let latest = events.last().unwrap();
            assert_eq!(latest.mark_price, Some(dec("103")));
            assert_eq!(
                (latest.funding_rate, latest.open_interest, latest.next_funding_ts),
                (None, None, None)
            );
        },
    )
    .await;
}

#[tokio::test]
async fn l1_ignores_stale_updates_and_rebuilds_from_reconnect_snapshot() {
    let now = Arc::new(AtomicI64::new(BASE + 1_000));

    let make_conn = |packets: Vec<Value>| -> WsConnHandler {
        Box::new(move |mut ws: ServerWs| {
            Box::pin(async move {
                let _ = ws.next().await;
                ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                    .await
                    .unwrap();
                for p in packets {
                    ws.send(Message::Text(p.to_string().into())).await.unwrap();
                }
                let _ = ws.close(None).await;
            })
        })
    };

    let h1 = make_conn(vec![
        book_payload("BTCUSDT", "99.0", "100.1", 8, "delta"),
        book_payload("BTCUSDT", "100.0", "100.1", 10, "snapshot"),
        book_payload("BTCUSDT", "100.1", "100.2", 11, "delta"),
        book_payload("BTCUSDT", "99.1", "100.1", 10, "delta"),
        book_payload("BTCUSDT", "99.2", "100.1", 11, "delta"),
    ]);
    let h2 = make_conn(vec![
        book_payload("BTCUSDT", "99.3", "100.1", 12, "delta"),
        book_payload("BTCUSDT", "99.4", "100.1", 1, "snapshot"),
    ]);

    run_multi_ws_harness(vec![h1, h2], |url| async move {
        let stream = make_stream(&url, true, now);
        let mut groups = Vec::new();
        for _ in 0..2 {
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            let mut updates = Vec::new();
            loop {
                match stream.recv().await {
                    Ok(Some(TransportFrame::ReceivedOrderBook(rb))) => updates.push(rb),
                    Ok(_) => {}
                    Err(PackError::FeedTransport(_)) => break,
                    Err(e) => panic!("unexpected err: {e:?}"),
                }
            }
            groups.push(updates);
        }
        stream.close().await.unwrap();

        let bids: Vec<Vec<Decimal>> = groups
            .iter()
            .map(|g| g.iter().map(|b| b.event.bid_price).collect())
            .collect();
        assert_eq!(
            bids,
            vec![vec![dec("100.0"), dec("100.1")], vec![dec("99.4")]]
        );
        assert_eq!(groups[0][0].connection_id, groups[0][1].connection_id);
        assert_ne!(groups[0][0].connection_id, groups[1][0].connection_id);
    })
    .await;
}

#[tokio::test]
async fn flat_zero_volume_no_trades_rejection_close_and_shared_subscriptions() {
    let now = Arc::new(AtomicI64::new(BASE));
    let now1 = Arc::clone(&now);
    run_ws_harness(
        |mut ws| async move {
            let _ = ws.next().await;
            ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                .await
                .unwrap();
            ws.send(Message::Text(
                kline_payload("BTCUSDT", "1", "99", "0", false, BASE)
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
            ws.send(Message::Text(
                kline_payload("BTCUSDT", "1", "100", "1", true, BASE + MINUTE)
                    .to_string()
                    .into(),
            ))
            .await
            .unwrap();
            let _ = ws.next().await;
        },
        |url| async move {
            let stream = make_stream(&url, true, now1);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            let flat = stream.recv().await.unwrap().unwrap();
            let normal = stream.recv().await.unwrap().unwrap();
            stream.close().await.unwrap();

            match flat {
                TransportFrame::ReceivedKline(k) => assert_eq!(k.event.bar.status, BarStatus::NoTrades),
                other => panic!("expected kline, got {other:?}"),
            }
            match normal {
                TransportFrame::ReceivedKline(k) => assert_eq!(k.event.bar.status, BarStatus::Normal),
                other => panic!("expected kline, got {other:?}"),
            }
        },
    )
    .await;

    // Rejected subscription -> FeedTransport
    let now2 = Arc::clone(&now);
    run_ws_harness(
        |mut ws| async move {
            let _ = ws.next().await;
            ws.send(Message::Text(
                r#"{"success":false,"ret_msg":"error:handler not found","op":"subscribe"}"#.into(),
            ))
            .await
            .unwrap();
        },
        |url| async move {
            let stream = make_stream(&url, true, now2);
            stream.connect().await.unwrap();
            let err = stream.subscribe(INSTRUMENT, "1m").await.unwrap_err();
            assert!(matches!(err, PackError::FeedTransport(_)));
            assert!(err.to_string().contains("handler not found"));
            stream.close().await.unwrap();
        },
    )
    .await;

    // Unexpected server close surfaces FeedTransport
    let now_close = Arc::clone(&now);
    run_ws_harness(
        |mut ws| async move {
            let _ = ws.next().await;
            ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                .await
                .unwrap();
            let _ = ws.close(None).await;
        },
        |url| async move {
            let stream = make_stream(&url, true, now_close);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            let err = stream.recv().await.unwrap_err();
            assert!(matches!(err, PackError::FeedTransport(_)));
            stream.close().await.unwrap();
        },
    )
    .await;

    // Shared topics across timeframes on same instrument
    let seen = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let seen_bg = Arc::clone(&seen);
    let now3 = Arc::clone(&now);
    run_ws_harness(
        move |mut ws| {
            let seen_bg = Arc::clone(&seen_bg);
            async move {
                while let Some(Ok(Message::Text(raw))) = ws.next().await {
                    let v: Value = serde_json::from_str(raw.as_str()).unwrap();
                    seen_bg.lock().await.push(v.clone());
                    let op = v["op"].as_str().unwrap_or("subscribe");
                    ws.send(Message::Text(
                        json!({"success": true, "op": op}).to_string().into(),
                    ))
                    .await
                    .unwrap();
                }
            }
        },
        |url| async move {
            let stream = make_stream(&url, true, now3);
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            stream.subscribe(INSTRUMENT, "5m").await.unwrap();
            stream.unsubscribe(INSTRUMENT, "1m").await.unwrap();
            stream.unsubscribe(INSTRUMENT, "5m").await.unwrap();
            stream.close().await.unwrap();

            let reqs = seen.lock().await;
            assert_eq!(reqs[1]["args"], json!(["kline.5.BTCUSDT"]));
            assert_eq!(reqs[2]["args"], json!(["kline.1.BTCUSDT"]));
            assert_eq!(
                reqs[3]["args"],
                json!([
                    "kline.5.BTCUSDT",
                    "orderbook.1.BTCUSDT",
                    "tickers.BTCUSDT",
                    "publicTrade.BTCUSDT"
                ])
            );
        },
    )
    .await;

    // Interval mapping round-trip
    assert_eq!(timeframe_from_bybit_interval("D"), Some("1d"));
    assert_eq!(timeframe_from_bybit_interval("1"), Some("1m"));
}

#[tokio::test]
async fn application_ping_and_pong_keep_connection_alive_and_timeout_disconnects() {
    let pings = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let pings_bg = Arc::clone(&pings);
    let now = Arc::new(AtomicI64::new(BASE));

    run_ws_harness(
        move |mut ws| {
            let pings_bg = Arc::clone(&pings_bg);
            async move {
                let _ = ws.next().await;
                ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                    .await
                    .unwrap();
                if let Some(Ok(Message::Text(raw))) = ws.next().await {
                    let msg: Value = serde_json::from_str(raw.as_str()).unwrap();
                    pings_bg.lock().await.push(msg.clone());
                    if msg["op"] == "ping" {
                        ws.send(Message::Text(r#"{"op":"pong"}"#.into()))
                            .await
                            .unwrap();
                    }
                }
                let _ = ws.next().await;
            }
        },
        |url| async move {
            let pack = get_market_pack("crypto_perp").unwrap();
            let stream = pack
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    now_ms: Arc::new(move || now.load(Ordering::SeqCst)),
                    ping_interval: Some(Duration::from_millis(40)),
                    ping_timeout: Duration::from_millis(500),
                    ack_timeout: Duration::from_secs(2),
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            tokio::time::sleep(Duration::from_millis(120)).await;
            stream.close().await.unwrap();
            let seen = pings.lock().await;
            assert!(!seen.is_empty());
            assert_eq!(seen[0]["op"], "ping");
        },
    )
    .await;

    // Silent server triggers ping_timeout disconnect
    let now_timeout = Arc::new(AtomicI64::new(BASE));
    run_ws_harness(
        |mut ws| async move {
            let _ = ws.next().await;
            ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                .await
                .unwrap();
            // Intentionally ignore ping and stay silent until client closes.
            let _ = ws.next().await;
            let _ = ws.next().await;
        },
        |url| async move {
            let pack = get_market_pack("crypto_perp").unwrap();
            let stream = pack
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    now_ms: Arc::new(move || now_timeout.load(Ordering::SeqCst)),
                    ping_interval: Some(Duration::from_millis(30)),
                    ping_timeout: Duration::from_millis(40),
                    ack_timeout: Duration::from_secs(2),
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            let err = stream.recv().await.unwrap_err();
            assert!(matches!(err, PackError::FeedTransport(_)));
            stream.close().await.unwrap();
        },
    )
    .await;
}

#[tokio::test]
async fn parser_receipt_timestamp_used_even_if_recv_is_delayed_and_float_rejected() {
    let now = Arc::new(AtomicI64::new(BASE + MINUTE + 5_000));
    let now_ws = Arc::clone(&now);

    run_ws_harness(
        move |mut ws| {
            let now_ws = Arc::clone(&now_ws);
            async move {
                let _ = ws.next().await;
                ws.send(Message::Text(r#"{"success":true,"op":"subscribe"}"#.into()))
                    .await
                    .unwrap();
                // Float close must be ignored by strict DecimalStr parser
                ws.send(Message::Text(
                    json!({
                        "topic": "kline.1.BTCUSDT",
                        "type": "snapshot",
                        "data": [{
                            "start": BASE, "end": BASE + MINUTE - 1, "interval": "1",
                            "open": 100.0, "high": "100", "low": "100", "close": "100",
                            "volume": "1", "turnover": "100", "confirm": true
                        }]
                    })
                    .to_string()
                    .into(),
                ))
                .await
                .unwrap();
                // Valid frame parsed at BASE + MINUTE + 5_000
                ws.send(Message::Text(
                    kline_payload("BTCUSDT", "1", "100", "1", true, BASE)
                        .to_string()
                        .into(),
                ))
                .await
                .unwrap();
                tokio::time::sleep(Duration::from_millis(30)).await;
                now_ws.store(BASE + MINUTE + 20_000, Ordering::SeqCst);
                let _ = ws.next().await;
            }
        },
        |url| async move {
            let stream = make_stream(&url, true, Arc::clone(&now));
            stream.connect().await.unwrap();
            stream.subscribe(INSTRUMENT, "1m").await.unwrap();
            tokio::time::sleep(Duration::from_millis(60)).await;
            let frame = stream.recv().await.unwrap().unwrap();
            stream.close().await.unwrap();
            match frame {
                TransportFrame::ReceivedKline(rk) => {
                    assert_eq!(rk.ts_ingested, BASE + MINUTE + 5_000);
                    assert_eq!(rk.event.bar.close, Some(dec("100")));
                }
                other => panic!("expected ReceivedKline, got {other:?}"),
            }
        },
    )
    .await;
}

#[tokio::test]
#[ignore = "requires live internet access to stream.bybit.com"]
async fn live_bybit_public_ws_smoke() {
    let pack = get_market_pack("crypto_perp").unwrap();
    let stream = pack
        .create_realtime_stream(RealtimeStreamConfig::default())
        .unwrap();
    stream.connect().await.expect("connect live Bybit WS");
    stream.subscribe(INSTRUMENT, "1m").await.expect("subscribe");
    let frame = tokio::time::timeout(Duration::from_secs(15), stream.recv())
        .await
        .expect("receive frame within 15s")
        .expect("valid WS frame");
    assert!(frame.is_some());
    stream.close().await.unwrap();
}
