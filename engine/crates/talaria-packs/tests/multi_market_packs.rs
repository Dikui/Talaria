//! Comprehensive tests for multi-market packs (`crypto_perp`, `binance_perp`, `us_equity`),
//! offline fixtures, Alpaca IEX vs SIP strict isolation, US sessions/DST, anti-lookahead calendar,
//! capability trait matrix, and zero venue/asset-class conditionals in `talaria-feed`/`talaria-server`.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::future::Future;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::sync::atomic::{AtomicI64, Ordering};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::{
    BarStatus, DerivativeKind, Direction, PrecisionTier, TradeSide, TransportEvent,
};
use talaria_packs::binance_perp::connector::{BinanceFixtureConnector, BinanceFuturesConnector};
use talaria_packs::binance_perp::stream::BinanceWsParser;
use talaria_packs::us_equity::connector::{AlpacaFixtureConnector, AlpacaHistoryConnector};
use talaria_packs::us_equity::stream::AlpacaWsParser;
use talaria_packs::{
    AlpacaDataFeed, BackfillWindow, BinancePerpPack, EquityCalendar, EquityCalendarEvent,
    EquityEventKind, EquityEventStatus, EtDate, HistoryConnector, HistoryConnectorConfig,
    MarketPack, PackError, RealtimeStreamConfig, TransportFrame, UsEquityPack, UsMarketSession,
    available_pack_names, backfill_bars, backfill_derivatives, classify_us_equity_session,
    et_to_utc_ms, get_market_pack, is_us_market_holiday, is_within_feed_hours,
    us_equity_sip_pack, utc_ms_to_et,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio_tungstenite::{WebSocketStream, accept_async, tungstenite::Message};

const MINUTE: i64 = 60_000;
const BINANCE_BASE_MS: i64 = 1_793_491_200_000; // 2026-11-01T00:00:00Z
const BINANCE_NOW_MS: i64 = BINANCE_BASE_MS + 1_000_000_000;

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

fn binance_fixture_dir() -> PathBuf {
    repo_root().join("tests/fixtures/binance")
}

fn alpaca_fixture_dir() -> PathBuf {
    repo_root().join("tests/fixtures/alpaca")
}

#[derive(Debug, Clone)]
struct RecordedHttpRequest {
    path: String,
    params: BTreeMap<String, String>,
}

fn parse_query_params(raw_query: &str) -> BTreeMap<String, String> {
    let mut map = BTreeMap::new();
    for pair in raw_query.split('&') {
        if pair.is_empty() {
            continue;
        }
        let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
        map.insert(k.to_owned(), v.to_owned());
    }
    map
}

async fn spawn_http_mock<F>(
    handler: F,
) -> (String, Arc<tokio::sync::Mutex<Vec<RecordedHttpRequest>>>)
where
    F: Fn(RecordedHttpRequest, usize) -> (u16, String) + Send + Sync + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback http");
    let addr = listener.local_addr().unwrap();
    let requests = Arc::new(tokio::sync::Mutex::new(Vec::new()));
    let requests_bg = Arc::clone(&requests);
    let handler = Arc::new(handler);

    tokio::spawn(async move {
        let mut count = 0usize;
        while let Ok((mut stream, _)) = listener.accept().await {
            count += 1;
            let idx = count;
            let reqs = Arc::clone(&requests_bg);
            let h = Arc::clone(&handler);
            tokio::spawn(async move {
                let mut buf = Vec::with_capacity(4096);
                let mut chunk = [0u8; 1024];
                loop {
                    let n = match stream.read(&mut chunk).await {
                        Ok(0) | Err(_) => break,
                        Ok(n) => n,
                    };
                    buf.extend_from_slice(&chunk[..n]);
                    if buf.windows(4).any(|w| w == b"\r\n\r\n") {
                        break;
                    }
                }
                let text = String::from_utf8_lossy(&buf);
                let first_line = text.lines().next().unwrap_or("");
                let target = first_line.split_whitespace().nth(1).unwrap_or("/");
                let (path, query) = target.split_once('?').unwrap_or((target, ""));
                let recorded = RecordedHttpRequest {
                    path: path.to_owned(),
                    params: parse_query_params(query),
                };
                reqs.lock().await.push(recorded.clone());

                let (status, body) = h(recorded, idx);
                let reason = match status {
                    200 => "OK",
                    503 => "Service Unavailable",
                    _ => "Internal Server Error",
                };
                let response = format!(
                    "HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                let _ = stream.write_all(response.as_bytes()).await;
                let _ = stream.shutdown().await;
            });
        }
    });

    (format!("http://{addr}"), requests)
}

type ServerWs = WebSocketStream<TcpStream>;

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

    tokio::time::timeout(Duration::from_secs(5), scenario(format!("ws://{addr}")))
        .await
        .expect("ws scenario timed out");
    accept_task.abort();
}

fn binance_kline_row(open_ms: i64, close_px: &str, volume: &str) -> Value {
    json!([
        open_ms,
        close_px,
        close_px,
        close_px,
        close_px,
        volume,
        open_ms + MINUTE - 1,
        "100.50",
        10,
        volume,
        "50.25",
        "0"
    ])
}

#[test]
fn registry_and_capability_trait_matrix_across_all_three_packs() {
    assert_eq!(
        available_pack_names(),
        vec!["crypto_perp", "binance_perp", "us_equity"]
    );

    let bybit = get_market_pack("crypto_perp").expect("crypto_perp");
    let binance = get_market_pack("binance_perp").expect("binance_perp");
    let equity_iex = get_market_pack("us_equity").expect("us_equity");
    let equity_sip = us_equity_sip_pack();

    assert_eq!(bybit.name(), "crypto_perp");
    assert_eq!(bybit.raw_archive_id(), "bybit_linear");
    assert_eq!(binance.name(), "binance_perp");
    assert_eq!(binance.raw_archive_id(), "binance_usdm");
    assert_eq!(
        binance.default_fixture_dir(),
        Some(Path::new("tests/fixtures/binance"))
    );
    assert_eq!(equity_iex.name(), "us_equity");
    assert_eq!(equity_iex.raw_archive_id(), "alpaca_iex");
    assert_eq!(equity_sip.raw_archive_id(), "alpaca_sip");
    assert_eq!(
        equity_iex.default_fixture_dir(),
        Some(Path::new("tests/fixtures/alpaca"))
    );

    // Capability matrix via asset-agnostic dyn MarketPack + TraitBundle
    let packs: Vec<(&dyn MarketPack, &str)> = vec![
        (bybit.as_ref(), "BTC-USDT-PERP"),
        (binance.as_ref(), "BTC-USDT-PERP"),
        (equity_iex.as_ref(), "AAPL"),
        (equity_sip.as_ref(), "AAPL"),
    ];

    for (pack, inst_id) in &packs {
        let spec = pack.instrument(inst_id).expect("instrument exists");
        assert_eq!(spec.instrument_spec_sha256().len(), 12);
        assert!(!pack.market_skill().is_empty());
        let traits = pack.traits(inst_id).expect("traits");
        assert!(traits.feed.realtime && traits.feed.polling && traits.feed.history);
        assert!(matches!(
            pack.traits("DOES-NOT-EXIST"),
            Err(PackError::InstrumentNotFound(_))
        ));
    }

    // Bybit vs Binance per-venue trait nuances
    let bybit_btc = bybit.traits("BTC-USDT-PERP").unwrap();
    let binance_btc = binance.traits("BTC-USDT-PERP").unwrap();
    let binance_sol = binance.traits("SOL-USDT-PERP").unwrap();
    assert_eq!(bybit_btc.margin.margin_asset, "USDT");
    assert_eq!(bybit_btc.margin.max_leverage, dec("100"));
    assert_eq!(binance_btc.margin.max_leverage, dec("125"));
    assert_eq!(binance_btc.order_constraint.max_market_qty, Some(dec("120")));
    assert_eq!(binance_sol.margin.max_leverage, dec("75"));
    assert_eq!(binance_btc.carry.funding_interval_hours, Some(8));
    assert_eq!(binance_btc.session.session_kind, "24x7");
    assert!(binance_btc.order_constraint.post_only);
    assert!(
        binance_btc
            .derivatives
            .as_ref()
            .is_some_and(|d| d.kinds
                == vec![
                    DerivativeKind::MarkPrice,
                    DerivativeKind::FundingRate,
                    DerivativeKind::OpenInterest
                ])
    );

    // Binance MarginValuation capability
    let mv = binance.margin_valuation().expect("binance margin valuation");
    assert_eq!(mv.interface_version(), 2);
    let liq = mv
        .isolated_liquidation_price(
            "BTC-USDT-PERP",
            Direction::Long,
            dec("2"),
            dec("100"),
            dec("40"),
            0,
        )
        .unwrap();
    // 40 + 2 * (mark - 100) = 2 * mark * 0.005 -> mark = 80 / 0.995
    assert_eq!(liq, dec("80") / dec("0.995"));
    let mut marks = BTreeMap::new();
    marks.insert("BTC-USDT-PERP".to_owned(), dec("110"));
    let est = mv
        .cross_maintenance_margin(
            &[("BTC-USDT-PERP".to_owned(), Direction::Long, dec("2"), dec("100"))],
            &marks,
            dec("10"),
            dec("100"),
        )
        .unwrap();
    assert_eq!(est.equity, dec("120"));
    assert_eq!(est.initial_margin, dec("22"));
    assert_eq!(est.maintenance_margin, dec("1.1"));

    // US Equity IEX vs SIP traits: Reg-T USD margin, no carry, no post_only, no derivatives
    let iex_aapl = equity_iex.traits("AAPL").unwrap();
    let sip_aapl = equity_sip.traits("AAPL").unwrap();
    assert_eq!(iex_aapl.margin.margin_asset, "USD");
    assert_eq!(iex_aapl.margin.max_leverage, dec("2"));
    assert_eq!(iex_aapl.carry.funding_interval_hours, None);
    assert!(!iex_aapl.order_constraint.post_only);
    assert!(iex_aapl.derivatives.is_none());
    assert_eq!(iex_aapl.session.session_kind, "us_equity");
    assert_eq!(iex_aapl.session.timezone.as_deref(), Some("America/New_York"));
    assert_eq!(iex_aapl.session.day_start, "09:30");
    assert_eq!(
        iex_aapl
            .session
            .sessions
            .iter()
            .map(|s| (s.name.as_str(), s.start.as_str(), s.end.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("pre_market", "08:00", "09:30"),
            ("regular", "09:30", "16:00"),
            ("after_hours", "16:00", "17:00"),
        ]
    );
    assert_eq!(
        sip_aapl
            .session
            .sessions
            .iter()
            .map(|s| (s.name.as_str(), s.start.as_str(), s.end.as_str()))
            .collect::<Vec<_>>(),
        vec![
            ("pre_market", "04:00", "09:30"),
            ("regular", "09:30", "16:00"),
            ("after_hours", "16:00", "20:00"),
        ]
    );
}

#[tokio::test]
async fn binance_perp_offline_fixtures_pagination_and_derivatives() {
    let pack = BinancePerpPack::new();
    let connector = pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(binance_fixture_dir()),
            ..HistoryConnectorConfig::default()
        })
        .unwrap();
    assert_eq!(connector.feed_mode(), "fixture");

    let all = backfill_bars(
        connector.as_ref(),
        "BTCUSDT",
        "1m",
        BackfillWindow {
            target_count: 500,
            max_pages: 5,
            page_limit: 50,
            ..BackfillWindow::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(all.len(), 120);
    let unique_ts: BTreeSet<i64> = all.iter().map(|b| b.ts_event).collect();
    assert_eq!(unique_ts.len(), 120);
    assert!(all.windows(2).all(|w| w[0].ts_event < w[1].ts_event));
    assert_eq!(all[0].ts_event, BINANCE_BASE_MS);
    assert_eq!(all[0].ts_available, BINANCE_BASE_MS + MINUTE);

    // Index 60 is zero-volume flat NO_TRADES bar
    let flat_compact = all[60]
        .to_compact(talaria_contracts::classify_flat_bar(
            all[60].open,
            all[60].high,
            all[60].low,
            all[60].close,
            all[60].volume,
        ))
        .unwrap();
    assert_eq!(flat_compact.status, BarStatus::NoTrades);
    assert_eq!(flat_compact.volume, Some(Decimal::ZERO));

    // Derivatives fixtures (mark price, funding rate, open interest)
    let marks = connector
        .fetch_mark_price_bars("BTCUSDT", "1m", None, 30)
        .await
        .unwrap();
    assert_eq!(marks.len(), 30);
    assert!(marks.iter().all(|p| p.kind == DerivativeKind::MarkPrice));

    let funding = connector
        .fetch_funding_settlements("BTCUSDT", None, 20)
        .await
        .unwrap();
    assert_eq!(funding.len(), 20);
    assert!(funding.iter().all(|p| p.ts_published == Some(p.ts_event)));

    let oi = backfill_derivatives(
        connector.as_ref(),
        "BTCUSDT",
        DerivativeKind::OpenInterest,
        Some("5m"),
        BackfillWindow {
            target_count: 40,
            max_pages: 4,
            page_limit: 15,
            ..BackfillWindow::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(oi.len(), 40);
    assert!(oi.iter().all(|p| p.kind == DerivativeKind::OpenInterest));

    // 1m OI maps cleanly to 5m fixture fallback
    let fc = BinanceFixtureConnector::new(binance_fixture_dir());
    let oi_1m = fc.fetch_open_interest_sync("BTCUSDT", "1m", None, 5).unwrap();
    assert_eq!(oi_1m.len(), 5);
}

#[tokio::test]
async fn binance_perp_rest_connector_pagination_strict_decimals_and_retries() {
    let (base_url, requests) = spawn_http_mock(|req, attempt| match req.path.as_str() {
        "/fapi/v1/klines" => {
            if attempt == 1 {
                return (503, String::new());
            }
            let end = req
                .params
                .get("endTime")
                .and_then(|s| s.parse::<i64>().ok())
                .unwrap_or(BINANCE_NOW_MS);
            let rows = if end >= BINANCE_NOW_MS - 2 * MINUTE {
                json!([
                    binance_kline_row(BINANCE_NOW_MS - MINUTE + 1, "68100.0", "1.0"), // unclosed
                    binance_kline_row(BINANCE_NOW_MS - 3 * MINUTE, "68050.5", "2.0"),
                    binance_kline_row(BINANCE_NOW_MS - 2 * MINUTE, "68075.25", "1.5"),
                ])
            } else {
                json!([binance_kline_row(BINANCE_NOW_MS - 4 * MINUTE, "68025.0", "1.2")])
            };
            (200, rows.to_string())
        }
        "/fapi/v1/markPriceKlines" => (
            200,
            json!([[
                BINANCE_NOW_MS - 2 * MINUTE,
                "68070.0",
                "68080.0",
                "68065.0",
                "68076.1",
                "0",
                BINANCE_NOW_MS - MINUTE - 1,
                "0",
                0,
                "0",
                "0",
                "0"
            ]])
            .to_string(),
        ),
        "/fapi/v1/fundingRate" => (
            200,
            json!([
                {"symbol": "BTCUSDT", "fundingRate": "0.00015000", "fundingTime": BINANCE_NOW_MS - 8 * 3_600_000},
                {"symbol": "BTCUSDT", "fundingRate": "0.00020000", "fundingTime": BINANCE_NOW_MS + 3_600_000}, // future excluded
            ])
            .to_string(),
        ),
        "/futures/data/openInterestHist" => (
            200,
            json!([
                {"symbol": "BTCUSDT", "sumOpenInterest": "91234.567", "timestamp": BINANCE_NOW_MS - 10 * MINUTE},
            ])
            .to_string(),
        ),
        _ => (404, json!({"code": -1121, "msg": "Invalid symbol."}).to_string()),
    })
    .await;

    let conn = BinanceFuturesConnector::new(
        &base_url,
        Duration::from_secs(5),
        1,
        Arc::new(|| BINANCE_NOW_MS),
    )
    .unwrap()
    .with_sleeper(Arc::new(|_| Box::pin(async {})));

    assert_eq!(conn.feed_mode(), "binance");

    let paged = backfill_bars(
        &conn,
        "BTCUSDT",
        "1m",
        BackfillWindow {
            start_ts: None,
            end_ts: Some(BINANCE_NOW_MS),
            target_count: 3,
            max_pages: 5,
            page_limit: 2,
        },
    )
    .await
    .unwrap();
    assert_eq!(
        paged.iter().map(|b| (b.ts_event, b.close)).collect::<Vec<_>>(),
        vec![
            (BINANCE_NOW_MS - 4 * MINUTE, dec("68025.0")),
            (BINANCE_NOW_MS - 3 * MINUTE, dec("68050.5")),
            (BINANCE_NOW_MS - 2 * MINUTE, dec("68075.25")),
        ]
    );

    let marks = conn
        .fetch_mark_price_bars("BTCUSDT", "1m", Some(BINANCE_NOW_MS), 5)
        .await
        .unwrap();
    assert_eq!(marks.len(), 1);
    assert_eq!(marks[0].value, dec("68076.1"));

    let funding = conn
        .fetch_funding_settlements("BTCUSDT", Some(BINANCE_NOW_MS), 5)
        .await
        .unwrap();
    assert_eq!(funding.len(), 1);
    assert_eq!(funding[0].value, dec("0.00015000"));
    assert_eq!(funding[0].ts_published, Some(funding[0].ts_event));

    let oi = conn
        .fetch_open_interest("BTCUSDT", "5m", Some(BINANCE_NOW_MS), 5)
        .await
        .unwrap();
    assert_eq!(oi.len(), 1);
    assert_eq!(oi[0].value, dec("91234.567"));

    // Verify query params used Binance fapi conventions
    let reqs = requests.lock().await;
    assert!(reqs.iter().any(|r| r.path == "/futures/data/openInterestHist"
        && r.params.get("period").map(String::as_str) == Some("5m")));

    // Reject JSON floats and structured Binance error payloads at boundary
    let (bad_url, _) = spawn_http_mock(|req, _| match req.path.as_str() {
        "/fapi/v1/klines" => (
            200,
            json!([[BINANCE_NOW_MS - 2 * MINUTE, 68000.5, "68010", "67990", "68005", "1", BINANCE_NOW_MS - MINUTE - 1, "68005"]]).to_string(),
        ),
        _ => (200, json!({"code": -1121, "msg": "Invalid symbol."}).to_string()),
    })
    .await;

    let bad_conn = BinanceFuturesConnector::new(
        &bad_url,
        Duration::from_secs(5),
        0,
        Arc::new(|| BINANCE_NOW_MS),
    )
    .unwrap();
    assert!(matches!(
        bad_conn.fetch_bars("BTCUSDT", "1m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));
    let err = bad_conn
        .fetch_funding_settlements("BADSYM", None, 10)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("-1121"));
}

#[tokio::test]
async fn binance_perp_ws_frames_fixture_and_live_stream() {
    let pack = get_market_pack("binance_perp").unwrap();
    // 1. Offline replay of tests/fixtures/binance/ws_frames.jsonl through BinanceWsParser
    let mut parser = BinanceWsParser::new(
        pack.instruments(),
        Arc::new(|| BINANCE_BASE_MS + 120 * MINUTE + 30_000),
        true,
    );
    parser.subscribe("BTC-USDT-PERP", "1m");

    let ws_lines = fs::read_to_string(binance_fixture_dir().join("ws_frames.jsonl")).unwrap();
    let mut parsed_frames = Vec::new();
    for (idx, line) in ws_lines.lines().enumerate() {
        let frames = parser
            .parse_json_str(line, BINANCE_BASE_MS + 120 * MINUTE + (idx as i64) * 1000)
            .unwrap();
        parsed_frames.extend(frames);
    }
    assert_eq!(parsed_frames.len(), 7);
    assert!(matches!(parsed_frames[0], TransportFrame::ReceivedKline(ref k) if !k.event.confirmed));
    assert!(matches!(parsed_frames[1], TransportFrame::ReceivedOrderBook(ref b) if b.event.bid_price == dec("68265.4")));
    assert!(matches!(parsed_frames[2], TransportFrame::Event(TransportEvent::DerivativeTick(ref d)) if d.mark_price == Some(dec("68266.10000000"))));
    assert!(matches!(parsed_frames[3], TransportFrame::ReceivedTrade(ref t) if t.event.side == TradeSide::Buy));
    assert!(matches!(parsed_frames[4], TransportFrame::ReceivedTrade(ref t) if t.event.side == TradeSide::Sell));
    assert!(matches!(parsed_frames[5], TransportFrame::ReceivedKline(ref k) if k.event.confirmed));
    assert!(matches!(parsed_frames[6], TransportFrame::ReceivedKline(ref k) if k.event.bar.status == BarStatus::NoTrades));

    // Reject float close in WS JSON
    let bad_ws = json!({
        "stream": "btcusdt@kline_1m",
        "data": {"e": "kline", "E": BINANCE_BASE_MS, "s": "BTCUSDT", "k": {"t": BINANCE_BASE_MS, "T": BINANCE_BASE_MS + MINUTE - 1, "s": "BTCUSDT", "i": "1m", "o": 68000.0, "c": "68000", "h": "68000", "l": "68000", "v": "1", "q": "68000", "x": true}}
    })
    .to_string();
    assert!(parser.parse_json_str(&bad_ws, BINANCE_BASE_MS).unwrap().is_empty());

    // 2. Live mock WS server driving BinanceFuturesStream (FeedTransport)
    let seen_req = Arc::new(tokio::sync::Mutex::new(Vec::<Value>::new()));
    let seen_bg = Arc::clone(&seen_req);
    let now = Arc::new(AtomicI64::new(BINANCE_BASE_MS + 120 * MINUTE + 50_000));

    run_ws_harness(
        move |mut ws| {
            let seen_bg = Arc::clone(&seen_bg);
            async move {
                let raw = ws.next().await.unwrap().unwrap();
                let req: Value = serde_json::from_str(raw.to_text().unwrap()).unwrap();
                let id = req["id"].as_u64().unwrap();
                seen_bg.lock().await.push(req);
                ws.send(Message::Text(json!({"result": null, "id": id}).to_string().into()))
                    .await
                    .unwrap();

                let ws_lines = fs::read_to_string(binance_fixture_dir().join("ws_frames.jsonl")).unwrap();
                for line in ws_lines.lines().take(4) {
                    ws.send(Message::Text(line.to_owned().into())).await.unwrap();
                }
                let _ = ws.next().await;
            }
        },
        |url| async move {
            let stream = pack
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    now_ms: Arc::new(move || now.load(Ordering::SeqCst)),
                    persist_public_trades: true,
                    ack_timeout: Duration::from_secs(2),
                    ping_interval: None,
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            assert_eq!(stream.feed_mode(), "binance");
            stream.connect().await.unwrap();
            stream.subscribe("BTC-USDT-PERP", "1m").await.unwrap();

            let f1 = stream.recv().await.unwrap().unwrap();
            let f2 = stream.recv().await.unwrap().unwrap();
            let f3 = stream.recv().await.unwrap().unwrap();
            let f4 = stream.recv().await.unwrap().unwrap();
            stream.close().await.unwrap();

            assert!(matches!(f1, TransportFrame::ReceivedKline(_)));
            assert!(matches!(f2, TransportFrame::ReceivedOrderBook(_)));
            assert!(matches!(f3, TransportFrame::Event(TransportEvent::DerivativeTick(_))));
            assert!(matches!(f4, TransportFrame::ReceivedTrade(_)));

            let reqs = seen_req.lock().await;
            assert_eq!(reqs[0]["method"], "SUBSCRIBE");
            assert_eq!(
                reqs[0]["params"],
                json!([
                    "btcusdt@kline_1m",
                    "btcusdt@bookTicker",
                    "btcusdt@markPrice@1s",
                    "btcusdt@aggTrade"
                ])
            );
        },
    )
    .await;
}

#[tokio::test]
async fn us_equity_strict_iex_vs_sip_separation_forbids_silent_fallback() {
    let iex_pack = UsEquityPack::new(AlpacaDataFeed::Iex);
    let sip_pack = UsEquityPack::new(AlpacaDataFeed::Sip);

    let iex_conn = iex_pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(alpaca_fixture_dir()),
            ..HistoryConnectorConfig::default()
        })
        .unwrap();
    let sip_conn = sip_pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(alpaca_fixture_dir()),
            ..HistoryConnectorConfig::default()
        })
        .unwrap();

    // IEX bars strictly lie inside 08:00-17:00 ET
    let iex_bars = iex_conn.fetch_bars("AAPL", "1m", None, 100).await.unwrap();
    assert_eq!(iex_bars.len(), 60);
    assert!(
        iex_bars
            .iter()
            .all(|b| is_within_feed_hours(AlpacaDataFeed::Iex, b.ts_event))
    );
    // Flat zero-vol bar present at index 25
    assert_eq!(iex_bars[25].volume, Decimal::ZERO);

    // SIP bars span 04:00-20:00 ET (includes early pre-market 04:30 ET and late after-hours 18:00 ET)
    let sip_bars = sip_conn.fetch_bars("AAPL", "1m", None, 100).await.unwrap();
    assert_eq!(sip_bars.len(), 80);
    assert!(
        sip_bars
            .iter()
            .any(|b| !is_within_feed_hours(AlpacaDataFeed::Iex, b.ts_event))
    );
    assert!(
        sip_bars
            .iter()
            .all(|b| is_within_feed_hours(AlpacaDataFeed::Sip, b.ts_event))
    );

    // MSFT exists ONLY in sip/; IEX must fail instead of silently falling back to sip/!
    let sip_msft = sip_conn.fetch_bars("MSFT", "1m", None, 50).await.unwrap();
    assert_eq!(sip_msft.len(), 30);
    let iex_msft_err = iex_conn.fetch_bars("MSFT", "1m", None, 50).await.unwrap_err();
    assert!(matches!(iex_msft_err, PackError::FeedUnavailable(_)));
    assert!(iex_msft_err.to_string().contains("forbidden"));

    // SPY exists ONLY in iex/; SIP must fail instead of silently falling back to iex/!
    assert_eq!(iex_conn.fetch_bars("SPY", "1m", None, 50).await.unwrap().len(), 30);
    assert!(matches!(
        sip_conn.fetch_bars("SPY", "1m", None, 50).await,
        Err(PackError::FeedUnavailable(_))
    ));

    // Derivatives are unsupported on US equity connector
    assert!(matches!(
        iex_conn.fetch_funding_settlements("AAPL", None, 10).await,
        Err(PackError::InvalidRequest(_))
    ));

    // WS URL mismatch between IEX and SIP must be rejected immediately
    assert!(matches!(
        iex_pack.create_realtime_stream(RealtimeStreamConfig {
            ws_url: Some("wss://stream.data.alpaca.markets/v2/sip".to_owned()),
            ..RealtimeStreamConfig::default()
        }),
        Err(PackError::InvalidRequest(_))
    ));
    assert!(matches!(
        sip_pack.create_realtime_stream(RealtimeStreamConfig {
            ws_url: Some("wss://stream.data.alpaca.markets/v2/iex".to_owned()),
            ..RealtimeStreamConfig::default()
        }),
        Err(PackError::InvalidRequest(_))
    ));

    // HTTP mock: explicit `feed=` param and mismatch rejection
    let open_ms = et_to_utc_ms(2026, 11, 2, 9, 30, 0);
    let now_ms = et_to_utc_ms(2026, 11, 2, 10, 0, 0);
    let (base_url, requests) = spawn_http_mock(move |req, _| {
        let feed = req.params.get("feed").map(String::as_str).unwrap_or("");
        if req.params.get("symbols").map(String::as_str) == Some("SPOOF") {
            // Upstream erroneously tags response as `sip` when `iex` was requested -> must reject
            return (
                200,
                json!({
                    "feed": "sip",
                    "bars": {"SPOOF": [{"t": open_ms, "o": "100", "h": "101", "l": "99", "c": "100.5", "v": 100}]}
                })
                .to_string(),
            );
        }
        let page_tok = req.params.get("page_token").map(String::as_str);
        let body = if page_tok.is_none() {
            json!({
                "feed": feed,
                "bars": {
                    "AAPL": [
                        {"t": "2026-11-02T14:32:00Z", "o": 225.2, "h": 225.4, "l": 225.1, "c": 225.3, "v": 1000, "vw": 225.25}
                    ]
                },
                "next_page_token": "p2"
            })
        } else {
            json!({
                "feed": feed,
                "bars": {
                    "AAPL": [
                        {"t": "2026-11-02T14:31:00Z", "o": 225.0, "h": 225.3, "l": 224.9, "c": 225.2, "v": 800, "vw": 225.10}
                    ]
                },
                "next_page_token": null
            })
        };
        (200, body.to_string())
    })
    .await;

    let iex_http = AlpacaHistoryConnector::new(
        &base_url,
        AlpacaDataFeed::Iex,
        Duration::from_secs(5),
        0,
        Arc::new(move || now_ms),
    )
    .unwrap();
    assert_eq!(iex_http.feed_mode(), "alpaca_iex");
    let bars = iex_http
        .fetch_bars("AAPL", "1m", Some(now_ms), 2)
        .await
        .unwrap();
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].ts_event, et_to_utc_ms(2026, 11, 2, 9, 31, 0));
    assert_eq!(bars[0].close, dec("225.2"));
    assert_eq!(bars[0].turnover, dec("180080.0")); // 225.10 * 800 exact Decimal

    // Verify `feed=iex` was explicitly sent
    {
        let reqs = requests.lock().await;
        assert!(reqs.iter().all(|r| r.params.get("feed").map(String::as_str) == Some("iex")));
    }

    // Mismatched `"feed": "sip"` payload on `Iex` connector MUST fail (no silent fallback)
    let spoof_err = iex_http
        .fetch_bars("SPOOF", "1m", Some(now_ms), 5)
        .await
        .unwrap_err();
    assert!(matches!(spoof_err, PackError::FeedUnavailable(_)));
    assert!(spoof_err.to_string().contains("mismatch"));

    // Verify AlpacaFixtureConnector forbids cross-feed access even if constructed directly
    let iex_fc = AlpacaFixtureConnector::new(alpaca_fixture_dir(), AlpacaDataFeed::Iex);
    assert!(iex_fc.fetch_bars_sync("MSFT", "1m", None, 10).is_err());
}

#[tokio::test]
async fn us_equity_ws_auth_iex_window_filter_and_sip_rejection() {
    let iex_pack = UsEquityPack::new(AlpacaDataFeed::Iex);
    let sip_pack = UsEquityPack::new(AlpacaDataFeed::Sip);

    // 1. Offline WS JSONL frames: IEX vs SIP window filtering
    let mut iex_parser = AlpacaWsParser::new(
        iex_pack.instruments(),
        AlpacaDataFeed::Iex,
        Arc::new(|| et_to_utc_ms(2026, 11, 2, 9, 35, 0)),
        true,
    );
    iex_parser.subscribe("AAPL", "1m");

    let iex_lines = fs::read_to_string(alpaca_fixture_dir().join("iex/ws_frames.jsonl")).unwrap();
    let mut iex_frames = Vec::new();
    for line in iex_lines.lines() {
        iex_frames.extend(
            iex_parser
                .parse_json_str(line, et_to_utc_ms(2026, 11, 2, 9, 32, 0))
                .unwrap(),
        );
    }
    assert_eq!(iex_frames.len(), 4);
    assert!(matches!(iex_frames[0], TransportFrame::ReceivedOrderBook(_)));
    // Trade at 225.52 == ask 225.52 -> Buy via Lee-Ready rule
    assert!(matches!(iex_frames[1], TransportFrame::ReceivedTrade(ref t) if t.event.side == TradeSide::Buy));
    assert!(matches!(iex_frames[2], TransportFrame::ReceivedKline(ref k) if k.event.bar.status == BarStatus::Normal));
    assert!(matches!(iex_frames[3], TransportFrame::ReceivedKline(ref k) if k.event.bar.status == BarStatus::NoTrades));

    // SIP WS frames (06:15 ET and 18:15 ET) MUST be rejected/ignored by IEX parser, accepted by SIP parser
    let sip_lines = fs::read_to_string(alpaca_fixture_dir().join("sip/ws_frames.jsonl")).unwrap();
    let mut rejected_by_iex = 0usize;
    let mut sip_parser = AlpacaWsParser::new(
        sip_pack.instruments(),
        AlpacaDataFeed::Sip,
        Arc::new(|| et_to_utc_ms(2026, 11, 2, 18, 20, 0)),
        true,
    );
    sip_parser.subscribe("AAPL", "1m");
    let mut accepted_by_sip = 0usize;
    for line in sip_lines.lines() {
        rejected_by_iex += iex_parser
            .parse_json_str(line, et_to_utc_ms(2026, 11, 2, 18, 20, 0))
            .unwrap()
            .len();
        accepted_by_sip += sip_parser
            .parse_json_str(line, et_to_utc_ms(2026, 11, 2, 18, 20, 0))
            .unwrap()
            .len();
    }
    assert_eq!(rejected_by_iex, 0);
    assert_eq!(accepted_by_sip, 3);

    // 2. Live mock WS server: auth handshake, subscription ack, and SIP 403 error propagation
    let now = Arc::new(AtomicI64::new(et_to_utc_ms(2026, 11, 2, 9, 32, 0)));
    let iex_pack_first = iex_pack.clone();
    run_ws_harness(
        |mut ws| async move {
            ws.send(Message::Text(r#"[{"T":"success","msg":"connected"}]"#.into()))
                .await
                .unwrap();
            let auth = ws.next().await.unwrap().unwrap();
            let auth_v: Value = serde_json::from_str(auth.to_text().unwrap()).unwrap();
            assert_eq!(auth_v["action"], "auth");
            ws.send(Message::Text(r#"[{"T":"success","msg":"authenticated"}]"#.into()))
                .await
                .unwrap();

            let sub = ws.next().await.unwrap().unwrap();
            let sub_v: Value = serde_json::from_str(sub.to_text().unwrap()).unwrap();
            assert_eq!(sub_v["action"], "subscribe");
            ws.send(Message::Text(
                r#"[{"T":"subscription","bars":["AAPL"],"quotes":["AAPL"],"trades":["AAPL"]}]"#
                    .into(),
            ))
            .await
            .unwrap();

            let iex_lines =
                fs::read_to_string(alpaca_fixture_dir().join("iex/ws_frames.jsonl")).unwrap();
            for line in iex_lines.lines().take(3) {
                ws.send(Message::Text(line.to_owned().into())).await.unwrap();
            }
            let _ = ws.next().await;
        },
        |url| async move {
            let stream = iex_pack_first
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    now_ms: Arc::new(move || now.load(Ordering::SeqCst)),
                    persist_public_trades: true,
                    ack_timeout: Duration::from_secs(2),
                    ping_interval: None,
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            assert_eq!(stream.feed_mode(), "alpaca_iex");
            stream.connect().await.unwrap();
            stream.subscribe("AAPL", "1m").await.unwrap();

            assert!(matches!(
                stream.recv().await.unwrap().unwrap(),
                TransportFrame::ReceivedOrderBook(_)
            ));
            assert!(matches!(
                stream.recv().await.unwrap().unwrap(),
                TransportFrame::ReceivedTrade(_)
            ));
            assert!(matches!(
                stream.recv().await.unwrap().unwrap(),
                TransportFrame::ReceivedKline(_)
            ));
            stream.close().await.unwrap();
        },
    )
    .await;

    // Multi-timeframe subscribe/unsubscribe on AlpacaStream updates local filter without tearing down shared wire streams
    run_ws_harness(
        |mut ws| async move {
            ws.send(Message::Text(r#"[{"T":"success","msg":"connected"}]"#.into()))
                .await
                .unwrap();
            let _ = ws.next().await;
            ws.send(Message::Text(r#"[{"T":"success","msg":"authenticated"}]"#.into()))
                .await
                .unwrap();
            let mut sub_count = 0usize;
            while let Some(Ok(Message::Text(raw))) = ws.next().await {
                let v: Value = serde_json::from_str(raw.as_str()).unwrap();
                let action = v["action"].as_str().unwrap_or("");
                if action == "subscribe" || action == "unsubscribe" {
                    ws.send(Message::Text(
                        r#"[{"T":"subscription","bars":["AAPL"],"quotes":["AAPL"]}]"#.into(),
                    ))
                    .await
                    .unwrap();
                    if action == "subscribe" {
                        sub_count += 1;
                        if sub_count == 2 {
                            // Give client a moment to unsubscribe 1m, then send both 1m (filtered) and 5m (accepted)
                            tokio::time::sleep(Duration::from_millis(30)).await;
                            ws.send(Message::Text(
                                json!([
                                    {"T":"b","S":"AAPL","tf":"1m","o":225.5,"h":225.7,"l":225.4,"c":225.6,"v":900,"t":"2026-11-02T14:35:00Z","vw":225.55},
                                    {"T":"b","S":"AAPL","tf":"5m","o":225.5,"h":225.7,"l":225.4,"c":225.6,"v":900,"t":"2026-11-02T14:35:00Z","vw":225.55}
                                ])
                                    .to_string()
                                    .into(),
                            ))
                            .await
                            .unwrap();
                        }
                    }
                }
            }
        },
        |url| async move {
            let stream = iex_pack
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    now_ms: Arc::new(|| et_to_utc_ms(2026, 11, 2, 9, 40, 0)),
                    persist_public_trades: false,
                    ack_timeout: Duration::from_secs(2),
                    ping_interval: None,
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            stream.connect().await.unwrap();
            stream.subscribe("AAPL", "1m").await.unwrap();
            stream.subscribe("AAPL", "5m").await.unwrap();
            stream.unsubscribe("AAPL", "1m").await.unwrap();
            let frame = stream.recv().await.unwrap().unwrap();
            match frame {
                TransportFrame::ReceivedKline(k) => {
                    assert_eq!(k.event.timeframe, "5m");
                }
                other => panic!("expected 5m ReceivedKline after 1m unsubscribe, got {other:?}"),
            }
            stream.unsubscribe("AAPL", "5m").await.unwrap();
            stream.close().await.unwrap();
        },
    )
    .await;

    // SIP entitlement 403 rejection -> FeedTransport (no silent fallback to IEX)
    run_ws_harness(
        |mut ws| async move {
            ws.send(Message::Text(r#"[{"T":"success","msg":"connected"}]"#.into()))
                .await
                .unwrap();
            let _ = ws.next().await;
            ws.send(Message::Text(r#"[{"T":"success","msg":"authenticated"}]"#.into()))
                .await
                .unwrap();
            let _ = ws.next().await;
            ws.send(Message::Text(
                r#"[{"T":"error","code":403,"msg":"subscription for sip not permitted"}]"#.into(),
            ))
            .await
            .unwrap();
        },
        |url| async move {
            let stream = sip_pack
                .create_realtime_stream(RealtimeStreamConfig {
                    ws_url: Some(url),
                    ack_timeout: Duration::from_secs(2),
                    ping_interval: None,
                    ..RealtimeStreamConfig::default()
                })
                .unwrap();
            stream.connect().await.unwrap();
            let err = stream.subscribe("AAPL", "1m").await.unwrap_err();
            assert!(matches!(err, PackError::FeedTransport(_)));
            assert!(err.to_string().contains("403"));
            stream.close().await.unwrap();
        },
    )
    .await;
}

#[test]
fn us_equity_sessions_dst_transitions_and_market_holidays() {
    // 2026-11-02 is Monday (EST = UTC-5)
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 3, 59, 0)),
        UsMarketSession::Closed
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 4, 0, 0)),
        UsMarketSession::PreMarket
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 9, 29, 59)),
        UsMarketSession::PreMarket
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 9, 30, 0)),
        UsMarketSession::Regular
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 15, 59, 59)),
        UsMarketSession::Regular
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 16, 0, 0)),
        UsMarketSession::AfterHours
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 19, 59, 59)),
        UsMarketSession::AfterHours
    );
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 2, 20, 0, 0)),
        UsMarketSession::Closed
    );

    // IEX vs SIP intraday bounds on open trading day
    let t_0759 = et_to_utc_ms(2026, 11, 2, 7, 59, 0);
    let t_0800 = et_to_utc_ms(2026, 11, 2, 8, 0, 0);
    let t_1659 = et_to_utc_ms(2026, 11, 2, 16, 59, 59);
    let t_1700 = et_to_utc_ms(2026, 11, 2, 17, 0, 0);
    assert!(!is_within_feed_hours(AlpacaDataFeed::Iex, t_0759));
    assert!(is_within_feed_hours(AlpacaDataFeed::Iex, t_0800));
    assert!(is_within_feed_hours(AlpacaDataFeed::Iex, t_1659));
    assert!(!is_within_feed_hours(AlpacaDataFeed::Iex, t_1700));
    assert!(is_within_feed_hours(AlpacaDataFeed::Sip, t_0759));
    assert!(is_within_feed_hours(AlpacaDataFeed::Sip, t_1700));

    // DST transitions in 2026:
    // Spring forward: 2026-03-08 -> 2026-03-06 09:30 EST == 14:30 UTC; 2026-03-09 09:30 EDT == 13:30 UTC
    let mar6_open_utc = chrono::DateTime::parse_from_rfc3339("2026-03-06T14:30:00Z")
        .unwrap()
        .timestamp_millis();
    let mar9_open_utc = chrono::DateTime::parse_from_rfc3339("2026-03-09T13:30:00Z")
        .unwrap()
        .timestamp_millis();
    assert_eq!(utc_ms_to_et(mar6_open_utc).1, 9);
    assert_eq!(utc_ms_to_et(mar9_open_utc).1, 9);
    assert_eq!(classify_us_equity_session(mar6_open_utc), UsMarketSession::Regular);
    assert_eq!(classify_us_equity_session(mar9_open_utc), UsMarketSession::Regular);

    // Fall back: 2026-11-01 -> 2026-10-30 09:30 EDT == 13:30 UTC; 2026-11-02 09:30 EST == 14:30 UTC
    let oct30_open_utc = chrono::DateTime::parse_from_rfc3339("2026-10-30T13:30:00Z")
        .unwrap()
        .timestamp_millis();
    let nov2_open_utc = chrono::DateTime::parse_from_rfc3339("2026-11-02T14:30:00Z")
        .unwrap()
        .timestamp_millis();
    assert_eq!(classify_us_equity_session(oct30_open_utc), UsMarketSession::Regular);
    assert_eq!(classify_us_equity_session(nov2_open_utc), UsMarketSession::Regular);

    // Weekends & NYSE holidays closed
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 1, 10, 0, 0)), // Sunday
        UsMarketSession::Closed
    );
    assert!(is_us_market_holiday(EtDate { year: 2026, month: 4, day: 3 })); // Good Friday 2026
    assert!(is_us_market_holiday(EtDate { year: 2026, month: 7, day: 3 })); // Jul 4 Saturday -> Fri Jul 3
    assert!(is_us_market_holiday(EtDate { year: 2026, month: 11, day: 26 })); // Thanksgiving 2026
    assert_eq!(
        classify_us_equity_session(et_to_utc_ms(2026, 11, 26, 11, 0, 0)),
        UsMarketSession::Closed
    );
}

#[test]
fn us_equity_anti_lookahead_corporate_and_earnings_calendar() {
    let cal = EquityCalendar::from_fixture_file(&alpaca_fixture_dir().join("corporate_actions.json"))
        .expect("load calendar fixture");

    // At 2026-11-02 10:00 ET:
    // - AAPL dividend was available 10 days ago (visible/released)
    // - AAPL Q4 earnings are scheduled, but ts_available is 16:31 ET (must NOT leak eps_actual!)
    // - NVDA split isn't announced until 16:05 ET (must NOT be visible or scheduled!)
    let t_morning = et_to_utc_ms(2026, 11, 2, 10, 0, 0);
    let visible_morning = cal.visible_events(Some("AAPL"), t_morning);
    assert_eq!(visible_morning.len(), 1);
    assert_eq!(visible_morning[0].event_id, "aapl-div-2026-11");
    assert_eq!(visible_morning[0].cash_amount, Some(dec("0.25")));

    let obs_morning = cal.observations_at(Some("AAPL"), t_morning);
    assert_eq!(obs_morning.len(), 2);
    assert_eq!(obs_morning[0].status, EquityEventStatus::Released);
    assert_eq!(obs_morning[1].event_id, "aapl-q4-earnings-2026");
    assert_eq!(obs_morning[1].status, EquityEventStatus::Unreleased);
    assert_eq!(obs_morning[1].eps_estimate, Some(dec("1.62")));
    assert_eq!(obs_morning[1].eps_actual, None); // anti-lookahead redaction!
    assert_eq!(obs_morning[1].effective_available_ts, None);
    assert!(obs_morning[1].to_catalyst_event().is_none());

    assert!(cal.visible_events(Some("NVDA"), t_morning).is_empty());

    // At exact 16:31 ET: AAPL earnings release becomes visible with eps_actual == 1.71
    let t_post_earnings = et_to_utc_ms(2026, 11, 2, 16, 31, 0);
    let obs_after = cal.observations_at(Some("AAPL"), t_post_earnings);
    let earnings = obs_after
        .iter()
        .find(|o| o.event_id == "aapl-q4-earnings-2026")
        .unwrap();
    assert_eq!(earnings.status, EquityEventStatus::Released);
    assert_eq!(earnings.eps_actual, Some(dec("1.71")));
    let catalyst = earnings.to_catalyst_event().expect("released catalyst");
    assert_eq!(catalyst.ts, t_post_earnings);
    assert_eq!(catalyst.kind, "EARNINGS");

    // Causal violation (`ts_available < schedule_available_ts`) must be rejected
    let invalid = EquityCalendar::from_events(vec![EquityCalendarEvent {
        event_id: "bad-order".to_owned(),
        instrument_id: "AAPL".to_owned(),
        kind: EquityEventKind::Earnings,
        title: "Invalid".to_owned(),
        ts_event: 200,
        schedule_available_ts: 300,
        ts_available: 250,
        precision: PrecisionTier::SecondVerified,
        cash_amount: None,
        split_ratio: None,
        eps_estimate: None,
        eps_actual: None,
    }]);
    assert!(matches!(invalid, Err(PackError::InvalidRequest(_))));
}

async fn run_asset_agnostic_backfill(
    pack: &dyn MarketPack,
    instrument_id: &str,
    fixture_dir: PathBuf,
    expected_min_bars: usize,
) {
    let spec = pack.instrument(instrument_id).expect("instrument");
    let _traits = pack.traits(instrument_id).expect("traits");
    let connector = pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(fixture_dir),
            ..HistoryConnectorConfig::default()
        })
        .expect("connector");
    let bars = backfill_bars(
        connector.as_ref(),
        &spec.symbol,
        "1m",
        BackfillWindow {
            target_count: 50,
            max_pages: 3,
            page_limit: 25,
            ..BackfillWindow::default()
        },
    )
    .await
    .expect("backfill");
    assert!(bars.len() >= expected_min_bars);
    assert!(bars.windows(2).all(|w| w[0].ts_event < w[1].ts_event));
    // Verify create_realtime_stream constructs generically for any pack
    let stream = pack
        .create_realtime_stream(RealtimeStreamConfig::default())
        .expect("stream");
    assert!(!stream.feed_mode().is_empty());
}

#[tokio::test]
async fn asset_agnostic_pipeline_and_zero_venue_conditionals_in_feed_and_server() {
    let bybit = get_market_pack("crypto_perp").unwrap();
    let binance = get_market_pack("binance_perp").unwrap();
    let equity = get_market_pack("us_equity").unwrap();

    run_asset_agnostic_backfill(
        bybit.as_ref(),
        "BTC-USDT-PERP",
        repo_root().join("tests/fixtures/bybit"),
        50,
    )
    .await;
    run_asset_agnostic_backfill(
        binance.as_ref(),
        "BTC-USDT-PERP",
        binance_fixture_dir(),
        50,
    )
    .await;
    run_asset_agnostic_backfill(
        equity.as_ref(),
        "AAPL",
        alpaca_fixture_dir(),
        50,
    )
    .await;

    // Verify `talaria-feed/src` and `talaria-server/src` contain ZERO venue/asset-class conditionals
    let crates_root = Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap();
    let forbidden_tokens = [
        "crypto_perp",
        "binance_perp",
        "us_equity",
        "bybit",
        "binance",
        "alpaca",
    ];
    for crate_name in ["talaria-feed", "talaria-server"] {
        let src_dir = crates_root.join(crate_name).join("src");
        let mut stack = vec![src_dir];
        while let Some(dir) = stack.pop() {
            for entry in fs::read_dir(&dir).unwrap() {
                let path = entry.unwrap().path();
                if path.is_dir() {
                    stack.push(path);
                } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                    let content = fs::read_to_string(&path).unwrap().to_ascii_lowercase();
                    for token in forbidden_tokens {
                        assert!(
                            !content.contains(token),
                            "{} must have zero venue/asset-class conditionals, found `{token}` in {}",
                            crate_name,
                            path.display()
                        );
                    }
                }
            }
        }
    }
}
