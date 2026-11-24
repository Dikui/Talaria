//! Tests for `MarketPack`, `CryptoPerpPack`, `FixtureConnector`, and `BybitLinearConnector`.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::time::Duration;

use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::DerivativeKind;
use talaria_packs::crypto_perp::connector::BybitLinearConnector;
use talaria_packs::{
    BackfillWindow, FixtureConnector, HistoryConnector, HistoryConnectorConfig, PackError,
    available_pack_names, backfill_bars, backfill_derivatives, get_market_pack,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;

const NOW_MS: i64 = 1_793_600_000_000;
const MINUTE: i64 = 60_000;

static TEMP_COUNTER: AtomicUsize = AtomicUsize::new(1);

struct TempDirGuard(PathBuf);

impl TempDirGuard {
    fn new(prefix: &str) -> Self {
        let id = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "talaria-packs-{prefix}-{}-{id}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("create temp dir");
        Self(dir)
    }

    fn path(&self) -> &Path {
        &self.0
    }
}

impl Drop for TempDirGuard {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn bybit_fixture_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("tests/fixtures/bybit")
}

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn kline_row(ts: i64, price: &str) -> Value {
    json!([
        ts.to_string(),
        price,
        price,
        price,
        price,
        "1.5",
        "150.75"
    ])
}

fn bybit_envelope(rows: Value, ret_code: i64) -> Value {
    json!({
        "retCode": ret_code,
        "retMsg": if ret_code == 0 { "OK" } else { "boom" },
        "result": {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": rows,
        },
        "time": NOW_MS,
    })
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

async fn spawn_http_mock<F>(handler: F) -> (String, Arc<tokio::sync::Mutex<Vec<RecordedHttpRequest>>>)
where
    F: Fn(RecordedHttpRequest, usize) -> (u16, String) + Send + Sync + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind loopback");
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

fn instant_connector(base_url: &str, retries: usize) -> BybitLinearConnector {
    BybitLinearConnector::new(
        base_url,
        Duration::from_secs(5),
        retries,
        Arc::new(|| NOW_MS),
    )
    .unwrap()
    .with_sleeper(Arc::new(|_| Box::pin(async {})))
}

#[test]
fn market_pack_registry_and_crypto_perp_metadata() {
    assert_eq!(
        available_pack_names(),
        vec!["crypto_perp", "binance_perp", "us_equity"]
    );
    let pack = get_market_pack("crypto_perp").expect("crypto_perp pack");
    assert_eq!(pack.name(), "crypto_perp");
    assert_eq!(pack.raw_archive_id(), "bybit_linear");
    assert_eq!(
        pack.default_fixture_dir(),
        Some(Path::new("tests/fixtures/bybit"))
    );
    assert!(!pack.market_skill().is_empty());

    let btc = pack.instrument("BTC-USDT-PERP").expect("BTC spec");
    assert_eq!(btc.symbol, "BTCUSDT");
    assert_eq!(btc.instrument_spec_sha256(), "61aeb14515aa");

    let traits = pack.traits("BTC-USDT-PERP").expect("BTC traits");
    assert!(traits.feed.realtime && traits.feed.history);
    assert_eq!(traits.margin.margin_asset, "USDT");
    assert_eq!(traits.carry.funding_interval_hours, Some(8));
    assert_eq!(traits.session.session_kind, "24x7");
    assert!(traits.order_constraint.post_only);
    assert!(traits.derivatives.as_ref().is_some_and(|d| d.kinds.len() == 3));

    assert!(matches!(
        pack.traits("UNKNOWN-PERP"),
        Err(PackError::InstrumentNotFound(_))
    ));
    assert!(matches!(
        get_market_pack("unknown-pack"),
        Err(PackError::InvalidRequest(_))
    ));
}

#[tokio::test]
async fn fixture_connector_pagination_and_derivatives_match_bybit_fixtures() {
    let pack = get_market_pack("crypto_perp").unwrap();
    let connector = pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(bybit_fixture_dir()),
            ..HistoryConnectorConfig::default()
        })
        .unwrap();
    assert_eq!(connector.feed_mode(), "fixture");

    let latest = connector
        .fetch_bars("BTCUSDT", "1m", None, 300)
        .await
        .unwrap();
    assert_eq!(latest.len(), 300);
    assert!(latest.windows(2).all(|w| w[0].ts_event < w[1].ts_event));
    assert_eq!(latest.last().unwrap().ts_event, 1_793_545_440_000);
    assert_eq!(
        latest.last().unwrap().ts_available,
        latest.last().unwrap().ts_event + MINUTE
    );

    let older = connector
        .fetch_bars("BTCUSDT", "1m", Some(latest[0].ts_event - 1), 300)
        .await
        .unwrap();
    assert_eq!(older.len(), 300);
    assert!(older.last().unwrap().ts_event < latest[0].ts_event);

    // 3 pages cover all 900 fixture bars without duplicates.
    let all = backfill_bars(
        connector.as_ref(),
        "BTCUSDT",
        "1m",
        BackfillWindow {
            target_count: 1000,
            max_pages: 5,
            page_limit: 300,
            ..BackfillWindow::default()
        },
    )
    .await
    .unwrap();
    let unique_ts: BTreeSet<i64> = all.iter().map(|b| b.ts_event).collect();
    assert_eq!(all.len(), 900);
    assert_eq!(unique_ts.len(), 900);
    assert_eq!(all[0].close, dec("68044.9"));
    assert_eq!(all.last().unwrap().close, dec("68685.3"));

    // Derivatives fixtures (mark price, funding rate, open interest)
    let marks = connector
        .fetch_mark_price_bars("BTCUSDT", "1m", None, 50)
        .await
        .unwrap();
    assert_eq!(marks.len(), 50);
    assert!(marks.iter().all(|p| p.kind == DerivativeKind::MarkPrice));

    let funding = connector
        .fetch_funding_settlements("BTCUSDT", None, 50)
        .await
        .unwrap();
    assert_eq!(funding.len(), 30);
    assert!(funding.iter().all(|p| p.ts_published == Some(p.ts_event)));

    let oi = connector
        .fetch_open_interest("BTCUSDT", "5m", None, 50)
        .await
        .unwrap();
    assert_eq!(oi.len(), 50);
    assert!(oi.iter().all(|p| p.kind == DerivativeKind::OpenInterest));

    let paged_oi = backfill_derivatives(
        connector.as_ref(),
        "BTCUSDT",
        DerivativeKind::OpenInterest,
        Some("5m"),
        BackfillWindow {
            target_count: 240,
            max_pages: 5,
            page_limit: 100,
            ..BackfillWindow::default()
        },
    )
    .await
    .unwrap();
    assert_eq!(paged_oi.len(), 240);
}

#[tokio::test]
async fn fixture_connector_error_cases_and_mark_price_fallback() {
    let dir = TempDirGuard::new("fixture-fallback");
    let fc = FixtureConnector::new(dir.path());

    assert!(fc.fetch_bars("BTCUSDT", "1m", None, 0).await.unwrap().is_empty());
    assert!(matches!(
        fc.fetch_bars("BTCUSDT", "1m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));

    // Malformed payload
    let sym_dir = dir.path().join("BTCUSDT");
    fs::create_dir_all(&sym_dir).unwrap();
    fs::write(sym_dir.join("1m.json"), "{}").unwrap();
    assert!(matches!(
        fc.fetch_bars("BTCUSDT", "1m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));

    // Fallback: mark_price_1m.json missing -> uses 1m.json close
    let payload = bybit_envelope(json!([kline_row(NOW_MS - 2 * MINUTE, "123.4")]), 0);
    fs::write(sym_dir.join("1m.json"), payload.to_string()).unwrap();
    let marks = fc
        .fetch_mark_price_bars("BTCUSDT", "1m", None, 10)
        .await
        .unwrap();
    assert_eq!(marks.len(), 1);
    assert_eq!(marks[0].value, dec("123.4"));
}

#[tokio::test]
async fn rest_connector_params_unclosed_filter_and_strict_decimals() {
    let (base_url, requests) = spawn_http_mock(|_req, _| {
        let rows = json!([
            kline_row(NOW_MS - MINUTE + 1, "102.0"), // unclosed: ts + 1m > NOW_MS
            kline_row(NOW_MS - 3 * MINUTE, "101.25"),
            kline_row(NOW_MS - 4 * MINUTE, "100.75"),
        ]);
        (200, bybit_envelope(rows, 0).to_string())
    })
    .await;

    let pack = get_market_pack("crypto_perp").unwrap();
    let connector = pack
        .create_history_connector(HistoryConnectorConfig {
            base_url: Some(base_url),
            now_ms: Arc::new(|| NOW_MS),
            retries: 1,
            ..HistoryConnectorConfig::default()
        })
        .unwrap();
    assert_eq!(connector.feed_mode(), "bybit");

    let bars = connector
        .fetch_bars("BTCUSDT", "1m", Some(NOW_MS), 2)
        .await
        .unwrap();
    let reqs = requests.lock().await;
    assert_eq!(reqs.len(), 1);
    assert_eq!(reqs[0].path, "/v5/market/kline");
    assert_eq!(reqs[0].params.get("category").map(String::as_str), Some("linear"));
    assert_eq!(reqs[0].params.get("symbol").map(String::as_str), Some("BTCUSDT"));
    assert_eq!(reqs[0].params.get("interval").map(String::as_str), Some("1"));
    assert_eq!(reqs[0].params.get("limit").map(String::as_str), Some("2"));
    assert_eq!(
        reqs[0].params.get("end").map(String::as_str),
        Some(NOW_MS.to_string().as_str())
    );

    assert_eq!(
        bars.iter().map(|b| b.ts_event).collect::<Vec<_>>(),
        vec![NOW_MS - 4 * MINUTE, NOW_MS - 3 * MINUTE]
    );
    assert_eq!(bars[0].close, dec("100.75"));
    assert_eq!(bars[0].ts_available, bars[0].ts_event + MINUTE);
}

#[tokio::test]
async fn rest_connector_retries_transient_503_and_rejects_persistent_500_or_float() {
    let (base_url, requests) = spawn_http_mock(|_req, attempt| {
        if attempt == 1 {
            (503, String::new())
        } else {
            (
                200,
                bybit_envelope(json!([kline_row(NOW_MS - 2 * MINUTE, "100.5")]), 0).to_string(),
            )
        }
    })
    .await;

    let conn = instant_connector(&base_url, 1);
    let bars = conn.fetch_bars("BTCUSDT", "1m", None, 10).await.unwrap();
    assert_eq!(bars.len(), 1);
    assert_eq!(requests.lock().await.len(), 2);

    // Persistent 500 -> FeedUnavailable
    let (err_url, err_reqs) = spawn_http_mock(|_, _| (500, String::new())).await;
    let err_conn = instant_connector(&err_url, 1);
    assert!(matches!(
        err_conn.fetch_bars("BTCUSDT", "1m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));
    assert_eq!(err_reqs.lock().await.len(), 2);

    // JSON float instead of DecimalStr string -> rejected at boundary
    let (float_url, _) = spawn_http_mock(|_, _| {
        let rows = json!([[
            (NOW_MS - 2 * MINUTE).to_string(),
            100.5,
            "100.5",
            "100.5",
            "100.5",
            "1",
            "100.5"
        ]]);
        (200, bybit_envelope(rows, 0).to_string())
    })
    .await;
    let float_conn = instant_connector(&float_url, 0);
    assert!(matches!(
        float_conn.fetch_bars("BTCUSDT", "1m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));

    // Non-zero retCode -> FeedUnavailable
    let (ret_url, _) =
        spawn_http_mock(|_, _| (200, bybit_envelope(json!([]), 10006).to_string())).await;
    let ret_conn = instant_connector(&ret_url, 0);
    let err = ret_conn
        .fetch_bars("BTCUSDT", "1m", None, 10)
        .await
        .unwrap_err();
    assert!(err.to_string().contains("10006"));
}

#[tokio::test]
async fn rest_connector_derivatives_endpoints_and_paginated_backfill() {
    let (base_url, requests) = spawn_http_mock(|req, _| {
        let body = match req.path.as_str() {
            "/v5/market/kline" => {
                let end = req
                    .params
                    .get("end")
                    .and_then(|s| s.parse::<i64>().ok())
                    .unwrap_or(NOW_MS);
                let rows: Vec<Value> = if end >= NOW_MS - 2 * MINUTE {
                    vec![
                        kline_row(NOW_MS - 2 * MINUTE, "102"),
                        kline_row(NOW_MS - 3 * MINUTE, "101"),
                    ]
                } else {
                    vec![kline_row(NOW_MS - 4 * MINUTE, "100")]
                };
                bybit_envelope(Value::Array(rows), 0).to_string()
            }
            "/v5/market/mark-price-kline" => bybit_envelope(
                json!([
                    [(NOW_MS - MINUTE + 1).to_string(), "101", "101", "101", "101"],
                    [(NOW_MS - 2 * MINUTE).to_string(), "100", "100", "100", "100.5"],
                ]),
                0,
            )
            .to_string(),
            "/v5/market/funding/history" => bybit_envelope(
                json!([
                    {"symbol": "BTCUSDT", "fundingRate": "0.0002", "fundingRateTimestamp": (NOW_MS - 8 * 3_600_000).to_string()},
                    {"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": (NOW_MS - 16 * 3_600_000).to_string()},
                ]),
                0,
            )
            .to_string(),
            "/v5/market/open-interest" => bybit_envelope(
                json!([
                    {"openInterest": "999", "timestamp": (NOW_MS - MINUTE + 1).to_string()},
                    {"openInterest": "123.5", "timestamp": (NOW_MS - 10 * MINUTE).to_string()},
                ]),
                0,
            )
            .to_string(),
            _ => bybit_envelope(json!([]), 0).to_string(),
        };
        (200, body)
    })
    .await;

    let conn = instant_connector(&base_url, 0);

    // Multi-page kline backfill
    let paged = backfill_bars(
        &conn,
        "BTCUSDT",
        "1m",
        BackfillWindow {
            start_ts: None,
            end_ts: Some(NOW_MS),
            target_count: 3,
            max_pages: 5,
            page_limit: 2,
        },
    )
    .await
    .unwrap();
    assert_eq!(
        paged.iter().map(|b| b.ts_event).collect::<Vec<_>>(),
        vec![NOW_MS - 4 * MINUTE, NOW_MS - 3 * MINUTE, NOW_MS - 2 * MINUTE]
    );

    // Mark price
    let marks = conn
        .fetch_mark_price_bars("BTCUSDT", "1m", Some(NOW_MS), 2)
        .await
        .unwrap();
    assert_eq!(marks.len(), 1);
    assert_eq!(marks[0].value, dec("100.5"));
    assert_eq!(marks[0].kind, DerivativeKind::MarkPrice);

    // Funding rate
    let funding = conn
        .fetch_funding_settlements("BTCUSDT", Some(NOW_MS), 10)
        .await
        .unwrap();
    assert_eq!(
        funding.iter().map(|p| p.value).collect::<Vec<_>>(),
        vec![dec("0.0001"), dec("0.0002")]
    );
    assert_eq!(funding[0].ts_published, Some(funding[0].ts_event));

    // Open interest
    let oi = conn
        .fetch_open_interest("BTCUSDT", "5m", Some(NOW_MS), 10)
        .await
        .unwrap();
    assert_eq!(oi.len(), 1);
    assert_eq!(oi[0].value, dec("123.5"));

    // Invalid OI timeframe rejected
    assert!(matches!(
        conn.fetch_open_interest("BTCUSDT", "3m", None, 10).await,
        Err(PackError::FeedUnavailable(_))
    ));

    let recorded = requests.lock().await;
    assert!(recorded
        .iter()
        .any(|r| r.path == "/v5/market/open-interest"
            && r.params.get("intervalTime").map(String::as_str) == Some("5min")));
}

#[tokio::test]
#[ignore = "requires live internet access to api.bybit.com"]
async fn live_bybit_public_rest_smoke() {
    let pack = get_market_pack("crypto_perp").unwrap();
    let connector = pack
        .create_history_connector(HistoryConnectorConfig::default())
        .unwrap();
    let bars = connector
        .fetch_bars("BTCUSDT", "1m", None, 2)
        .await
        .expect("live Bybit kline REST should succeed");
    assert!(!bars.is_empty());
    assert!(bars.last().unwrap().ts_event > 0);
}
