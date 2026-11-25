//! Integration tests for OKX (`okx_perp`), Hyperliquid (`hyperliquid_perp`),
//! Coinbase Spot (`coinbase_spot`), Binance Vision bulk archive (`data.binance.vision`),
//! and the composite multi-venue pack (`crypto_suite`).

use std::io::{Cursor, Write};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::Arc;
use std::time::Duration;

use rust_decimal::Decimal;
use sha2::{Digest, Sha256};
use talaria_contracts::TransportEvent;
use talaria_packs::binance_perp::archive::{
    BinanceVisionArchiveConnector, decode_klines_zip_bytes, verify_sha256_checksum,
};
use talaria_packs::coinbase_spot::stream::CoinbaseWsParser;
use talaria_packs::hyperliquid_perp::stream::HyperliquidWsParser;
use talaria_packs::okx_perp::stream::OkxWsParser;
use talaria_packs::{
    ClockFn, HistoryConnector, HistoryConnectorConfig, TransportFrame, available_pack_names,
    get_market_pack,
};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use zip::ZipWriter;
use zip::write::SimpleFileOptions;

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .canonicalize()
        .expect("repo root")
}

#[test]
fn registry_exposes_advanced_crypto_suite_and_component_packs() {
    let names = available_pack_names();
    for expected in [
        "crypto_perp",
        "binance_perp",
        "us_equity",
        "okx_perp",
        "hyperliquid_perp",
        "coinbase_spot",
        "crypto_suite",
    ] {
        assert!(names.contains(&expected), "missing pack `{expected}`");
        let pack = get_market_pack(expected).expect("pack resolves");
        assert_eq!(pack.name(), expected);
    }

    let suite = get_market_pack("crypto_suite").expect("crypto_suite");
    assert_eq!(suite.instruments().len(), 15);
    assert_eq!(suite.instrument("BTC-USDT-PERP").unwrap().venue, "okx");
    assert_eq!(suite.instrument("BTC-USDT-BN-PERP").unwrap().venue, "binance");
    assert_eq!(suite.instrument("BTC-USDT-BN-PERP").unwrap().symbol, "BTCUSDT");
    assert_eq!(
        suite.instrument("BTC-USDT-BN-SPOT").unwrap().venue,
        "binance_spot"
    );
    assert_eq!(
        suite.instrument("BTC-USDT-BN-SPOT").unwrap().asset_class,
        "crypto_spot"
    );
    assert_eq!(
        suite.instrument("BTC-USDT-BN-SPOT").unwrap().symbol,
        "BTCUSDT-SPOT"
    );
    assert_eq!(
        suite.instrument("BTC-USD-HL-PERP").unwrap().venue,
        "hyperliquid"
    );
    assert_eq!(suite.instrument("BTC-USD-CB-SPOT").unwrap().venue, "coinbase");

    let bn_traits = suite.traits("BTC-USDT-BN-PERP").unwrap();
    assert!(!bn_traits.feed.realtime);
    assert!(!bn_traits.feed.polling);
    assert!(bn_traits.feed.history);
    assert!(bn_traits.derivatives.as_ref().is_some_and(|d| !d.realtime && !d.history));

    let bn_spot_traits = suite.traits("BTC-USDT-BN-SPOT").unwrap();
    assert!(!bn_spot_traits.feed.realtime);
    assert!(!bn_spot_traits.feed.polling);
    assert!(bn_spot_traits.feed.history);
    assert!(bn_spot_traits.derivatives.is_none());
    assert_eq!(bn_spot_traits.margin.max_leverage, Decimal::ONE);

    assert_eq!(
        suite.traits("BTC-USD-HL-PERP").unwrap().carry.funding_interval_hours,
        Some(1)
    );
    assert!(suite.traits("BTC-USD-CB-SPOT").unwrap().derivatives.is_none());
    assert_eq!(
        suite.traits("BTC-USD-CB-SPOT").unwrap().margin.max_leverage,
        Decimal::ONE
    );
}

#[tokio::test]
async fn composite_pack_routes_offline_fixtures_across_all_three_venues() {
    let suite = get_market_pack("crypto_suite").expect("crypto_suite");
    let connector = suite
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(repo_root().join("tests/fixtures")),
            ..HistoryConnectorConfig::default()
        })
        .expect("composite fixture connector");

    // 1. OKX closed-only klines (`confirm=1` only -> skips 3rd row where confirm=0)
    let okx_bars = connector
        .fetch_bars("BTC-USDT-SWAP", "1m", None, 10)
        .await
        .expect("okx bars");
    assert_eq!(okx_bars.len(), 2);
    assert_eq!(okx_bars[0].close, dec("58150.0"));
    assert_eq!(okx_bars[1].volume, dec("15.5"));

    let okx_funding = connector
        .fetch_funding_settlements("BTC-USDT-SWAP", None, 10)
        .await
        .expect("okx funding");
    assert_eq!(okx_funding.len(), 2);
    assert_eq!(okx_funding[1].value, dec("0.00006298")); // quantized to RATE_SCALE=8

    let okx_oi = connector
        .fetch_open_interest("BTC-USDT-SWAP", "5m", None, 10)
        .await
        .expect("okx oi");
    assert_eq!(okx_oi.len(), 2);
    assert_eq!(okx_oi[1].value, dec("31979.1906")); // quantized to BTC qty_precision=4

    // 2. Hyperliquid candles + hourly funding + mark/OI context
    let hl_bars = connector
        .fetch_bars("BTC", "1m", None, 10)
        .await
        .expect("hl bars");
    assert_eq!(hl_bars.len(), 2);
    assert_eq!(hl_bars[1].close, dec("58202.0"));
    assert_eq!(hl_bars[1].volume, dec("14.25"));

    let hl_funding = connector
        .fetch_funding_settlements("BTC", None, 10)
        .await
        .expect("hl funding");
    assert_eq!(hl_funding.len(), 2);
    assert_eq!(hl_funding[0].value, dec("-0.00000361"));

    let hl_mark = connector
        .fetch_mark_price_bars("BTC", "1m", None, 1)
        .await
        .expect("hl mark");
    assert_eq!(hl_mark[0].value, dec("58201.5"));

    // 3. Coinbase spot [time, low, high, open, close, volume] + 4h aggregation
    let cb_1m = connector
        .fetch_bars("BTC-USD", "1m", None, 10)
        .await
        .expect("cb 1m");
    assert_eq!(cb_1m.len(), 2);
    assert_eq!(cb_1m[0].low, dec("58090"));
    assert_eq!(cb_1m[0].high, dec("58180"));
    assert_eq!(cb_1m[0].open, dec("58102"));
    assert_eq!(cb_1m[0].close, dec("58151"));

    let cb_4h = connector
        .fetch_bars("BTC-USD", "4h", None, 10)
        .await
        .expect("cb 4h aggregation");
    assert_eq!(cb_4h.len(), 1);
    assert_eq!(cb_4h[0].open, dec("58020"));
    assert_eq!(cb_4h[0].high, dec("58400"));
    assert_eq!(cb_4h[0].low, dec("58000"));
    assert_eq!(cb_4h[0].close, dec("58350"));
    assert_eq!(cb_4h[0].volume, dec("14"));
}

#[test]
fn venue_ws_parsers_decode_kline_bbo_derivatives_and_roll_candles() {
    let now: ClockFn = Arc::new(|| 1_795_615_565_000_i64);

    // OKX WS parser
    let okx = get_market_pack("okx_perp").unwrap();
    let mut okx_parser = OkxWsParser::new(okx.instruments(), Arc::clone(&now), true);
    let notice = okx_parser
        .decode_text(r#"{"event":"notice","msg":"migrate","code":"64010"}"#)
        .unwrap();
    assert!(notice.is_empty());

    let okx_kline = okx_parser
        .decode_text(r#"{"arg":{"channel":"candle1m","instId":"BTC-USDT-SWAP"},"data":[["1795615500000","58200.0","58250.0","58195.0","58240.0","1000","10.0000","582200.00000","1"]]}"#)
        .unwrap();
    assert!(matches!(okx_kline.as_slice(), [TransportFrame::ReceivedKline(k)] if k.event.confirmed && k.event.instrument_id == "BTC-USDT-PERP"));

    let okx_bbo = okx_parser
        .decode_text(r#"{"arg":{"channel":"bbo-tbt","instId":"BTC-USDT-SWAP"},"data":[{"bids":[["58239.9","2.5","0","1"]],"asks":[["58240.0","1.5","0","1"]],"ts":"1795615501000"}]}"#)
        .unwrap();
    assert!(matches!(okx_bbo.as_slice(), [TransportFrame::ReceivedOrderBook(b)] if b.event.bid_price == dec("58239.9")));

    // Hyperliquid WS parser
    let hl = get_market_pack("hyperliquid_perp").unwrap();
    let mut hl_parser = HyperliquidWsParser::new(hl.instruments(), Arc::clone(&now), false);
    let hl_ctx = hl_parser
        .decode_text(r#"{"channel":"activeAssetCtx","data":{"coin":"BTC","ctx":{"markPx":"58241.0","funding":"0.0000125","openInterest":"2500.5"}}}"#)
        .unwrap();
    assert_eq!(hl_ctx.len(), 1);
    assert!(matches!(
        &hl_ctx[0],
        TransportFrame::Event(TransportEvent::DerivativeTick(ev))
            if ev.funding_rate == Some(dec("0.0000125"))
                && ev.mark_price == Some(dec("58241.0"))
                && ev.open_interest == Some(dec("2500.5"))
                && ev.instrument_id == "BTC-USD-HL-PERP"
    ));

    // Coinbase WS parser rolls a closed 1m candle when match enters next minute bucket
    let cb = get_market_pack("coinbase_spot").unwrap();
    let mut cb_parser = CoinbaseWsParser::new(cb.instruments(), now, true);
    cb_parser.add_subscription("BTC-USD-CB-SPOT", "1m");
    let m1 = cb_parser
        .decode_text(r#"{"type":"match","product_id":"BTC-USD","trade_id":101,"side":"buy","price":"58210.50","size":"0.25","time":"2026-11-25T14:04:10Z"}"#)
        .unwrap();
    assert_eq!(m1.len(), 2); // open bar + trade

    let m2 = cb_parser
        .decode_text(r#"{"type":"match","product_id":"BTC-USD","trade_id":102,"side":"sell","price":"58225.00","size":"0.50","time":"2026-11-25T14:05:05Z"}"#)
        .unwrap();
    assert!(m2.iter().any(|f| matches!(
        f,
        TransportFrame::ReceivedKline(k) if k.event.confirmed && k.event.bar.open == Some(dec("58210.5"))
    )));
}

#[tokio::test]
async fn binance_vision_archive_verifies_sha256_and_rejects_tampered_zip() {
    // Build a valid in-memory Binance CSV inside a ZIP archive.
    let csv = concat!(
        "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n",
        "1793491200000,58000.0,58100.0,57950.0,58080.0,12.500,1793491259999,725600.0,100,6.0,348000.0,0\n",
        "1793491260000,58080.0,58120.0,58050.0,58110.0,8.250,1793491319999,479325.0,80,4.0,232400.0,0\n"
    );
    let mut zip_buf = Cursor::new(Vec::new());
    {
        let mut w = ZipWriter::new(&mut zip_buf);
        w.start_file("BTCUSDT-1m-2026-11-01.csv", SimpleFileOptions::default())
            .unwrap();
        w.write_all(csv.as_bytes()).unwrap();
        w.finish().unwrap();
    }
    let valid_zip = zip_buf.into_inner();
    let digest = hex::encode(Sha256::digest(&valid_zip));
    let checksum_body = format!("{digest}  BTCUSDT-1m-2026-11-01.zip\n").into_bytes();

    // Direct helper verification + mismatch rejection
    verify_sha256_checksum(&valid_zip, &checksum_body, "test").expect("valid checksum");
    let err = verify_sha256_checksum(&valid_zip, b"0000000000000000000000000000000000000000000000000000000000000000  bad.zip", "test")
        .unwrap_err();
    assert!(err.to_string().contains("checksum mismatch"));

    let decoded = decode_klines_zip_bytes(&valid_zip, "BTCUSDT", "1m", 60_000, 1_793_500_000_000)
        .expect("decode zip");
    assert_eq!(decoded.len(), 2);
    assert_eq!(decoded[0].close, dec("58080"));
    assert_eq!(decoded[1].volume, dec("8.25"));

    // Serve ZIP + .CHECKSUM over loopback HTTP to verify full connector flow
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    let zip_clone = valid_zip.clone();
    let csum_clone = checksum_body.clone();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let z = zip_clone.clone();
            let c = csum_clone.clone();
            tokio::spawn(async move {
                let mut req_buf = [0u8; 2048];
                let n = stream.read(&mut req_buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&req_buf[..n]);
                let path = req.split_whitespace().nth(1).unwrap_or("/");
                let (status, body): (&str, &[u8]) = if path.ends_with("2026-11-01.zip") {
                    ("200 OK", &z)
                } else if path.ends_with("2026-11-01.zip.CHECKSUM") {
                    ("200 OK", &c)
                } else {
                    ("404 Not Found", b"not found")
                };
                let head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes()).await;
                let _ = stream.write_all(body).await;
            });
        }
    });

    let base_url = format!("http://{addr}");
    // end_ts inside 2026-11-01T00:05:00Z (`1_793_491_500_000`)
    let now_fn = Arc::new(|| 1_793_500_000_000_i64);
    let connector =
        BinanceVisionArchiveConnector::new(base_url, Duration::from_secs(5), now_fn).unwrap();
    let bars = connector
        .fetch_bars("BTCUSDT", "1m", Some(1_793_491_500_000), 10)
        .await
        .expect("archive fetch_bars");
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].open, dec("58000"));
    assert_eq!(bars[1].close, dec("58110"));
}

fn build_archive_zip_and_checksum(csv_name: &str, zip_name: &str, csv: &str) -> (Vec<u8>, Vec<u8>) {
    let mut zip_buf = Cursor::new(Vec::new());
    {
        let mut w = ZipWriter::new(&mut zip_buf);
        w.start_file(csv_name, SimpleFileOptions::default()).unwrap();
        w.write_all(csv.as_bytes()).unwrap();
        w.finish().unwrap();
    }
    let zip_bytes = zip_buf.into_inner();
    let digest = hex::encode(Sha256::digest(&zip_bytes));
    let checksum_body = format!("{digest}  {zip_name}\n").into_bytes();
    (zip_bytes, checksum_body)
}

#[tokio::test]
async fn binance_vision_archive_fetch_bars_in_range_spans_monthly_and_daily_fallback() {
    // 2026-10-31T23:58:00Z = 1793491080000
    // 2026-10-31T23:59:00Z = 1793491140000
    // 2026-11-01T00:00:00Z = 1793491200000
    // 2026-11-02T00:00:00Z = 1793577600000
    // 2026-11-02T00:01:00Z = 1793577660000
    let (oct_zip, oct_csum) = build_archive_zip_and_checksum(
        "BTCUSDT-1m-2026-10.csv",
        "BTCUSDT-1m-2026-10.zip",
        concat!(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n",
            "1793491080000,57900.0,57910.0,57890.0,57905.0,5.0,1793491139999,289500.0,40,2.5,144750.0,0\n",
            "1793491140000,57905.0,57950.0,57900.0,57940.0,7.5,1793491199999,434500.0,50,3.5,202750.0,0\n",
        ),
    );
    let (nov01_zip, nov01_csum) = build_archive_zip_and_checksum(
        "BTCUSDT-1m-2026-11-01.csv",
        "BTCUSDT-1m-2026-11-01.zip",
        concat!(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n",
            "1793491200000,57940.0,58010.0,57930.0,58000.0,10.0,1793491259999,579800.0,70,5.0,289900.0,0\n",
        ),
    );
    let (nov02_zip, nov02_csum) = build_archive_zip_and_checksum(
        "BTCUSDT-1m-2026-11-02.csv",
        "BTCUSDT-1m-2026-11-02.zip",
        concat!(
            "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n",
            "1793577600000,58000.0,58050.0,57990.0,58040.0,11.0,1793577659999,638200.0,80,6.0,348100.0,0\n",
            "1793577660000,58040.0,58090.0,58030.0,58080.0,12.0,1793577719999,696700.0,90,6.5,377400.0,0\n",
        ),
    );

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let oct_z = oct_zip.clone();
            let oct_c = oct_csum.clone();
            let n1_z = nov01_zip.clone();
            let n1_c = nov01_csum.clone();
            let n2_z = nov02_zip.clone();
            let n2_c = nov02_csum.clone();
            tokio::spawn(async move {
                let mut req_buf = [0u8; 2048];
                let n = stream.read(&mut req_buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&req_buf[..n]);
                let path = req.split_whitespace().nth(1).unwrap_or("/");
                let (status, body): (&str, &[u8]) = if path.ends_with("2026-10.zip") {
                    ("200 OK", &oct_z)
                } else if path.ends_with("2026-10.zip.CHECKSUM") {
                    ("200 OK", &oct_c)
                } else if path.ends_with("2026-11-01.zip") {
                    ("200 OK", &n1_z)
                } else if path.ends_with("2026-11-01.zip.CHECKSUM") {
                    ("200 OK", &n1_c)
                } else if path.ends_with("2026-11-02.zip") {
                    ("200 OK", &n2_z)
                } else if path.ends_with("2026-11-02.zip.CHECKSUM") {
                    ("200 OK", &n2_c)
                } else {
                    ("404 Not Found", b"not found")
                };
                let head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes()).await;
                let _ = stream.write_all(body).await;
            });
        }
    });

    let base_url = format!("http://{addr}");
    let now_fn = Arc::new(|| 1_793_600_000_000_i64);
    let connector =
        BinanceVisionArchiveConnector::new(base_url, Duration::from_secs(5), now_fn).unwrap();
    // Range excludes first Oct bar (1793491080000) and last Nov-02 bar (1793577660000)
    let bars = connector
        .fetch_bars_in_range("BTCUSDT", "1m", 1_793_491_140_000, 1_793_577_600_000)
        .await
        .expect("fetch_bars_in_range");
    assert_eq!(bars.len(), 3);
    assert_eq!(bars[0].ts_event, 1_793_491_140_000);
    assert_eq!(bars[1].ts_event, 1_793_491_200_000);
    assert_eq!(bars[2].ts_event, 1_793_577_600_000);
    assert_eq!(bars[0].close, dec("57940"));
    assert_eq!(bars[1].close, dec("58000"));
    assert_eq!(bars[2].close, dec("58040"));
}

#[tokio::test]
async fn composite_suite_archive_transport_noop_and_margin_delegation() {
    let suite = get_market_pack("crypto_suite").expect("crypto_suite");
    let transport = suite
        .create_realtime_stream(talaria_packs::RealtimeStreamConfig::default())
        .expect("composite transport");
    transport
        .subscribe("BTC-USDT-BN-PERP", "1m")
        .await
        .expect("archive subscribe is a clean no-op");
    transport
        .unsubscribe("BTC-USDT-BN-PERP", "1m")
        .await
        .expect("archive unsubscribe is a clean no-op");
    transport.close().await.expect("close transport");

    let mv = suite.margin_valuation().expect("margin valuation");
    let liq = mv
        .isolated_liquidation_price(
            "BTC-USDT-BN-PERP",
            talaria_contracts::Direction::Long,
            dec("1"),
            dec("50000"),
            dec("5000"),
            0,
        )
        .expect("binance archive margin valuation");
    assert!(liq > Decimal::ZERO);

    transport
        .subscribe("BTC-USDT-BN-SPOT", "1m")
        .await
        .expect("spot archive subscribe is a clean no-op");
    transport
        .unsubscribe("BTC-USDT-BN-SPOT", "1m")
        .await
        .expect("spot archive unsubscribe is a clean no-op");
}

#[tokio::test]
async fn binance_spot_archive_normalizes_microseconds_and_fetches_spot_path() {
    // 2024-12-31T23:59:00Z = 1735689540000 ms (13-digit ms format)
    // 2025-01-01T00:00:00Z = 1735689600000 ms = 1735689600000000 us (16-digit us format)
    let (dec_zip, dec_csum) = build_archive_zip_and_checksum(
        "BTCUSDT-1m-2024-12.csv",
        "BTCUSDT-1m-2024-12.zip",
        "1735689540000,93500.00,93600.00,93400.00,93576.00,10.5,1735689599999,982548.00,120,5.0,467880.00,0\n",
    );
    let (jan_zip, jan_csum) = build_archive_zip_and_checksum(
        "BTCUSDT-1m-2025-01.csv",
        "BTCUSDT-1m-2025-01.zip",
        "1735689600000000,93576.00,93700.00,93500.00,93650.00,12.25,1735689659999999,1147212.50,150,6.5,608725.00,0\n",
    );

    let decoded_ms = decode_klines_zip_bytes(&dec_zip, "BTCUSDT-SPOT", "1m", 60_000, 1_736_000_000_000)
        .expect("decode ms csv");
    assert_eq!(decoded_ms.len(), 1);
    assert_eq!(decoded_ms[0].ts_event, 1_735_689_540_000);
    assert_eq!(decoded_ms[0].ts_available, 1_735_689_600_000);

    let decoded_us = decode_klines_zip_bytes(&jan_zip, "BTCUSDT-SPOT", "1m", 60_000, 1_736_000_000_000)
        .expect("decode us csv");
    assert_eq!(decoded_us.len(), 1);
    assert_eq!(decoded_us[0].ts_event, 1_735_689_600_000);
    assert_eq!(decoded_us[0].ts_available, 1_735_689_660_000);
    assert_eq!(decoded_us[0].symbol, "BTCUSDT-SPOT");

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        while let Ok((mut stream, _)) = listener.accept().await {
            let dz = dec_zip.clone();
            let dc = dec_csum.clone();
            let jz = jan_zip.clone();
            let jc = jan_csum.clone();
            tokio::spawn(async move {
                let mut req_buf = [0u8; 2048];
                let n = stream.read(&mut req_buf).await.unwrap_or(0);
                let req = String::from_utf8_lossy(&req_buf[..n]);
                let path = req.split_whitespace().nth(1).unwrap_or("/");
                let (status, body): (&str, &[u8]) = if path == "/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-12.zip" {
                    ("200 OK", &dz)
                } else if path == "/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-12.zip.CHECKSUM" {
                    ("200 OK", &dc)
                } else if path == "/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip" {
                    ("200 OK", &jz)
                } else if path == "/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2025-01.zip.CHECKSUM" {
                    ("200 OK", &jc)
                } else {
                    ("404 Not Found", b"not found")
                };
                let head = format!(
                    "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(head.as_bytes()).await;
                let _ = stream.write_all(body).await;
            });
        }
    });

    let connector = BinanceVisionArchiveConnector::new_spot(
        format!("http://{addr}"),
        Duration::from_secs(5),
        Arc::new(|| 1_736_000_000_000),
    )
    .unwrap();
    let bars = connector
        .fetch_bars_in_range("BTCUSDT-SPOT", "1m", 1_735_689_540_000, 1_735_689_600_000)
        .await
        .expect("spot archive fetch_bars_in_range");
    assert_eq!(bars.len(), 2);
    assert_eq!(bars[0].ts_event, 1_735_689_540_000);
    assert_eq!(bars[1].ts_event, 1_735_689_600_000);
    assert_eq!(bars[0].symbol, "BTCUSDT-SPOT");
}
