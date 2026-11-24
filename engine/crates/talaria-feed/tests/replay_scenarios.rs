//! ReplayTransport unit tests, production data-dir safety guard tests, all 4
//! `tests/fixtures/realtime/*.jsonl` supervisor scenarios, and exact `CompactBar`/`RawBar`
//! equivalence checks against `tests/fixtures/bybit` + `demo_outage.jsonl`.

use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rust_decimal::Decimal;
use serde_json::json;
use talaria_contracts::{
    BarStatus, CompactBar, FeedEvent, FeedHealthEvent, FeedHealthStatus, InstrumentSpec,
    KlineEvent, RawBar, TransportEvent,
};
use talaria_feed::{
    BarPublishSink, FeedClock, FeedError, FeedSupervisor, FeedSupervisorConfig, ReplayConfig,
    ReplayTransport, StepClock, compact_series, validate_replay_data_dir_with_env,
};
use talaria_packs::{
    FeedTransport, FixtureConnector, HistoryConnectorConfig, PackError, get_market_pack,
};
use tempfile::TempDir;

const BASE: i64 = 1_763_596_800_000;
const MINUTE: i64 = 60_000;
const INSTRUMENT: &str = "BTC-USDT-PERP";
const REPLAY_NOW_MS: i64 = 1_795_022_974_103;

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn repo_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join("../../..")
}

fn realtime_fixture(name: &str) -> PathBuf {
    repo_root().join("tests/fixtures/realtime").join(name)
}

fn bybit_fixture_dir() -> PathBuf {
    repo_root().join("tests/fixtures/bybit")
}

fn write_replay(dir: &Path, lines: &[serde_json::Value]) -> PathBuf {
    let path = dir.join("replay.jsonl");
    let body = lines
        .iter()
        .map(serde_json::Value::to_string)
        .collect::<Vec<_>>()
        .join("\n");
    fs::write(&path, format!("{body}\n")).unwrap();
    path
}

fn kline_line(offset_ms: i64, timestamp: i64, close: &str, confirmed: bool) -> serde_json::Value {
    json!({
        "offset_ms": offset_ms,
        "type": "kline",
        "event": {
            "instrument_id": INSTRUMENT,
            "timeframe": "1m",
            "bar": {
                "timestamp": timestamp,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": "1",
                "turnover": "1",
                "status": "NORMAL",
                "ts_available": timestamp + MINUTE,
                "precision": "SECOND_VERIFIED",
            },
            "confirmed": confirmed,
            "revision": 0,
        }
    })
}

fn book_line(offset_ms: i64, instrument_id: &str) -> serde_json::Value {
    json!({
        "offset_ms": offset_ms,
        "type": "book",
        "event": {
            "instrument_id": instrument_id,
            "bid_price": "100.0",
            "bid_size": "2",
            "ask_price": "100.1",
            "ask_size": "3",
            "ts_event": BASE + offset_ms,
            "ts_ingested": BASE + offset_ms,
        }
    })
}

#[derive(Default)]
struct ReplaySink {
    closes: Mutex<Vec<(String, CompactBar, i64, u64)>>,
    events: Mutex<Vec<FeedEvent>>,
}

impl ReplaySink {
    fn klines(&self) -> Vec<KlineEvent> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                FeedEvent::Kline(k) => Some(k.clone()),
                _ => None,
            })
            .collect()
    }

    fn healths(&self) -> Vec<FeedHealthEvent> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                FeedEvent::Health(h) => Some(h.clone()),
                _ => None,
            })
            .collect()
    }

    fn book_count(&self) -> usize {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter(|e| matches!(e, FeedEvent::OrderBook(_)))
            .count()
    }
}

impl BarPublishSink for ReplaySink {
    fn on_bar_close(
        &self,
        instrument: &InstrumentSpec,
        _timeframe: &str,
        bar: &CompactBar,
        received_ts: i64,
        revision: u64,
    ) {
        self.closes.lock().unwrap().push((
            instrument.instrument_id.clone(),
            bar.clone(),
            received_ts,
            revision,
        ));
    }

    fn on_event(&self, event: &FeedEvent) {
        self.events.lock().unwrap().push(event.clone());
    }
}

#[tokio::test]
async fn replay_events_follow_offsets_and_speed() {
    let tmp = TempDir::new().unwrap();
    let path = write_replay(
        tmp.path(),
        &[
            kline_line(0, BASE, "100", false),
            book_line(1_000, INSTRUMENT),
            kline_line(2_500, BASE, "101", true),
        ],
    );
    let clock = Arc::new(StepClock::new(0));
    let feed = ReplayTransport::from_file(
        &path,
        ReplayConfig {
            speed: 2.0,
            clock: clock.clone(),
            ..ReplayConfig::default()
        },
    )
    .unwrap();

    feed.connect().await.unwrap();
    feed.subscribe(INSTRUMENT, "1m").await.unwrap();

    let first = feed.recv().await.unwrap().unwrap().into_transport_event();
    let second = feed.recv().await.unwrap().unwrap().into_transport_event();
    let third = feed.recv().await.unwrap().unwrap().into_transport_event();

    match first {
        TransportEvent::Kline(k) => assert_eq!(k.bar.close, Some(dec("100"))),
        other => panic!("expected kline, got {other:?}"),
    }
    assert!(matches!(second, TransportEvent::OrderBook(_)));
    match third {
        TransportEvent::Kline(k) => assert!(k.confirmed),
        other => panic!("expected kline, got {other:?}"),
    }
    assert_eq!(
        clock.sleeps(),
        vec![Duration::from_millis(500), Duration::from_millis(750)]
    );
}

#[tokio::test]
async fn replay_disconnect_raises_and_connect_waits_for_reconnect_offset() {
    let tmp = TempDir::new().unwrap();
    let path = write_replay(
        tmp.path(),
        &[
            kline_line(0, BASE, "100", false),
            json!({"offset_ms": 1_000, "type": "disconnect", "reason": "simulated outage"}),
            json!({"offset_ms": 4_000, "type": "reconnect"}),
            kline_line(5_000, BASE, "101", true),
        ],
    );
    let clock = Arc::new(StepClock::new(0));
    let feed = ReplayTransport::from_file(
        &path,
        ReplayConfig {
            speed: 1.0,
            clock: clock.clone(),
            ..ReplayConfig::default()
        },
    )
    .unwrap();

    feed.connect().await.unwrap();
    feed.subscribe(INSTRUMENT, "1m").await.unwrap();
    let _ = feed.recv().await.unwrap();
    let err = feed.recv().await.unwrap_err();
    assert!(matches!(err, PackError::FeedTransport(_)));
    assert!(err.to_string().contains("simulated outage"));
    assert_eq!(clock.elapsed_ms(), 1_000);

    feed.connect().await.unwrap();
    assert_eq!(clock.elapsed_ms(), 4_000);
    let resumed = feed.recv().await.unwrap().unwrap().into_transport_event();
    match resumed {
        TransportEvent::Kline(k) => assert_eq!(k.bar.close, Some(dec("101"))),
        other => panic!("expected kline, got {other:?}"),
    }
    assert_eq!(clock.elapsed_ms(), 5_000);
}

#[tokio::test]
async fn replay_unsubscribed_channels_skipped_and_idle_heartbeat_after_eof() {
    let tmp = TempDir::new().unwrap();
    let path = write_replay(
        tmp.path(),
        &[
            book_line(0, "ETH-USDT-PERP"),
            kline_line(0, BASE, "100", false),
            book_line(0, INSTRUMENT),
        ],
    );
    let clock = Arc::new(StepClock::new(0));
    let feed = ReplayTransport::from_file(
        &path,
        ReplayConfig {
            speed: 2.0,
            clock: clock.clone(),
            ..ReplayConfig::default()
        },
    )
    .unwrap();

    feed.connect().await.unwrap();
    feed.subscribe(INSTRUMENT, "1m").await.unwrap();

    assert!(matches!(
        feed.recv().await.unwrap().unwrap().into_transport_event(),
        TransportEvent::Kline(_)
    ));
    assert!(matches!(
        feed.recv().await.unwrap().unwrap().into_transport_event(),
        TransportEvent::OrderBook(_)
    ));
    assert!(feed.recv().await.unwrap().is_none());
    assert!(feed.is_script_exhausted());
    assert_eq!(clock.sleeps(), vec![Duration::from_millis(2_500)]);

    feed.close().await.unwrap();
    assert!(matches!(feed.recv().await, Err(PackError::FeedTransport(_))));
}

#[test]
fn malformed_replay_and_invalid_speed_rejected() {
    let tmp = TempDir::new().unwrap();

    let empty = tmp.path().join("empty.jsonl");
    fs::write(&empty, "\n").unwrap();
    assert!(matches!(
        ReplayTransport::from_file(&empty, ReplayConfig::default()),
        Err(FeedError::InvalidRequest(_))
    ));

    for bad_lines in [
        vec![json!({"offset_ms": 0, "type": "tick"})],
        vec![json!({"type": "heartbeat"})],
        vec![
            json!({"offset_ms": 100, "type": "heartbeat"}),
            json!({"offset_ms": 50, "type": "heartbeat"}),
        ],
    ] {
        let p = write_replay(tmp.path(), &bad_lines);
        assert!(matches!(
            ReplayTransport::from_file(&p, ReplayConfig::default()),
            Err(FeedError::InvalidRequest(_))
        ));
    }

    let valid = write_replay(tmp.path(), &[json!({"offset_ms": 0, "type": "heartbeat"})]);
    for bad_speed in [0.0, -1.0, f64::NAN, f64::INFINITY] {
        assert!(matches!(
            ReplayTransport::from_file(
                &valid,
                ReplayConfig {
                    speed: bad_speed,
                    ..ReplayConfig::default()
                }
            ),
            Err(FeedError::InvalidRequest(_))
        ));
    }
}

#[test]
fn replay_safety_guard_forbids_default_and_overlapping_production_dirs() {
    let tmp = TempDir::new().unwrap();
    let daily = tmp.path().join("daily");
    fs::create_dir_all(&daily).unwrap();
    let sentinel = daily.join("keep");
    fs::write(&sentinel, b"daily data").unwrap();

    #[cfg(unix)]
    let alias = {
        let alias = tmp.path().join("alias");
        std::os::unix::fs::symlink(&daily, &alias).unwrap();
        alias
    };

    // Omitting --data-dir is forbidden
    assert!(matches!(
        validate_replay_data_dir_with_env(None, Some(&daily), &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));

    // Default `.talaria` is forbidden
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(Path::new(".talaria")), None, &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));

    // $TALARIA_DATA_DIR, nested subdir, parent dir, and symlink alias are all forbidden
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(&daily), Some(&daily), &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(&daily.join("nested")), Some(&daily), &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(tmp.path()), Some(&daily), &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));
    #[cfg(unix)]
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(&alias.join("nested")), Some(&daily), &[]),
        Err(FeedError::ProtectedDataDir(_))
    ));

    // Case-folded overlap (before directories exist)
    assert!(matches!(
        validate_replay_data_dir_with_env(
            Some(&tmp.path().join("talaria/data/nested")),
            Some(&tmp.path().join("Talaria/Data")),
            &[]
        ),
        Err(FeedError::ProtectedDataDir(_))
    ));

    // Protected extra service dirs (e.g. from desktop/launchd config)
    let svc_dir = tmp.path().join("service-root");
    assert!(matches!(
        validate_replay_data_dir_with_env(Some(&svc_dir), None, std::slice::from_ref(&svc_dir)),
        Err(FeedError::ProtectedDataDir(_))
    ));

    // Explicit isolated directory succeeds
    let isolated = tmp.path().join("isolated");
    let approved = validate_replay_data_dir_with_env(Some(&isolated), Some(&daily), &[svc_dir]).unwrap();
    assert!(approved.ends_with("isolated"));
    assert_eq!(fs::read(&sentinel).unwrap(), b"daily data");

    // `ReplayTransport::from_file_for_data_dir` enforces the guard at construction
    let replay_file = realtime_fixture("in_progress.jsonl");
    assert!(matches!(
        ReplayTransport::from_file_for_data_dir(
            &replay_file,
            Some(Path::new(".talaria")),
            &[],
            ReplayConfig::default()
        ),
        Err(FeedError::ProtectedDataDir(_))
    ));
}

async fn run_realtime_fixture_scenario(
    fixture_name: &str,
    expected_klines: usize,
) -> (Arc<FeedSupervisor>, Arc<ReplaySink>, Arc<StepClock>) {
    let clock = Arc::new(StepClock::new(REPLAY_NOW_MS));
    let transport = Arc::new(
        ReplayTransport::from_file(
            realtime_fixture(fixture_name),
            ReplayConfig {
                speed: 1.0,
                clock: clock.clone(),
                idle_heartbeat_ms: 5_000,
            },
        )
        .unwrap(),
    );
    let sink = Arc::new(ReplaySink::default());
    let pack = get_market_pack("crypto_perp").unwrap();
    let sup = Arc::new(
        FeedSupervisor::new(
            transport.clone(),
            None,
            pack.instruments().to_vec(),
            clock.clone(),
            sink.clone(),
            FeedSupervisorConfig {
                tick_interval: None,
                backoff_initial: Duration::from_millis(50),
                backoff_max: Duration::from_millis(500),
                ..FeedSupervisorConfig::default()
            },
        )
        .unwrap(),
    );

    let mut rx = sup.subscribe_events();
    sup.subscribe(INSTRUMENT, "1m").unwrap();
    sup.start().await.unwrap();

    tokio::time::timeout(Duration::from_secs(5), async {
        let mut seen_klines = 0usize;
        while seen_klines < expected_klines {
            match rx.recv().await {
                Ok(FeedEvent::Kline(_)) => seen_klines += 1,
                Ok(_) => {}
                Err(err) => panic!("broadcast error during {fixture_name}: {err}"),
            }
        }
        // Wait briefly until replay reaches EOF if trailing heartbeat exists
        for _ in 0..10 {
            if transport.is_script_exhausted() {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap_or_else(|_| panic!("timed out waiting for {expected_klines} klines in {fixture_name}"));

    sup.stop().await;
    (sup, sink, clock)
}

#[tokio::test]
async fn realtime_fixture_in_progress_revisions_and_health() {
    let (sup, sink, _) = run_realtime_fixture_scenario("in_progress.jsonl", 3).await;
    assert_eq!(sup.feed_mode(), "replay");
    assert_eq!(sup.health().status, FeedHealthStatus::Live);
    assert_eq!(
        sink.healths().into_iter().map(|h| h.status).collect::<Vec<_>>(),
        vec![FeedHealthStatus::Live]
    );

    let klines = sink.klines();
    assert_eq!(
        klines.iter().map(|k| k.revision).collect::<Vec<_>>(),
        vec![0, 1, 2]
    );
    assert_eq!(
        klines.iter().map(|k| k.confirmed).collect::<Vec<_>>(),
        vec![false, false, true]
    );
    assert!(klines.iter().all(|k| k.bar.timestamp == BASE));

    let recent = sup.recent_bars(INSTRUMENT, "1m", None, false);
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].close, Some(dec("101.5")));
    assert_eq!(recent[0].status, BarStatus::Normal);
    assert_eq!(sink.closes.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn realtime_fixture_no_trades_synthesis() {
    let (sup, sink, _) = run_realtime_fixture_scenario("no_trades.jsonl", 4).await;
    assert_eq!(
        sink.healths().into_iter().map(|h| h.status).collect::<Vec<_>>(),
        vec![FeedHealthStatus::Live]
    );

    let recent = sup.recent_bars(INSTRUMENT, "1m", None, false);
    assert_eq!(
        recent
            .iter()
            .map(|b| (b.timestamp, b.status, b.close, b.volume))
            .collect::<Vec<_>>(),
        vec![
            (BASE, BarStatus::Normal, Some(dec("100.0")), Some(dec("3.0"))),
            (BASE + MINUTE, BarStatus::NoTrades, Some(dec("100.0")), Some(Decimal::ZERO)),
            (BASE + 2 * MINUTE, BarStatus::NoTrades, Some(dec("100.0")), Some(Decimal::ZERO)),
            (BASE + 3 * MINUTE, BarStatus::Normal, Some(dec("103.0")), Some(dec("1.5"))),
        ]
    );
    // Only the 2 exchange-confirmed bars archive to BarPublishSink
    assert_eq!(sink.closes.lock().unwrap().len(), 2);
}

#[tokio::test]
async fn realtime_fixture_outage_recovery_transitions_and_feed_gap() {
    let (sup, sink, _) = run_realtime_fixture_scenario("outage_recovery.jsonl", 3).await;
    let healths = sink.healths();
    assert_eq!(
        healths.iter().map(|h| h.status).collect::<Vec<_>>(),
        vec![
            FeedHealthStatus::Live,
            FeedHealthStatus::Stale,
            FeedHealthStatus::Recovering,
            FeedHealthStatus::Live,
        ]
    );
    assert!(healths[1].stale_since.is_some());
    assert_eq!(healths[3].stale_since, None);
    assert_eq!(sup.health().channels[0].reconnect_count, 1);

    let recent = sup.recent_bars(INSTRUMENT, "1m", None, false);
    assert_eq!(
        recent
            .iter()
            .map(|b| (b.timestamp, b.status, b.close))
            .collect::<Vec<_>>(),
        vec![
            (BASE, BarStatus::Normal, Some(dec("100.0"))),
            (BASE + MINUTE, BarStatus::FeedGap, None),
            (BASE + 2 * MINUTE, BarStatus::Normal, Some(dec("102.0"))),
        ]
    );
}

#[tokio::test]
async fn exact_equivalence_bybit_fixtures_and_demo_outage_jsonl() {
    // 1. Verify Bybit fixture RawBar & CompactBar sequence equivalence against Python reference
    let pack = get_market_pack("crypto_perp").unwrap();
    let connector = pack
        .create_history_connector(HistoryConnectorConfig {
            fixture_dir: Some(bybit_fixture_dir()),
            ..HistoryConnectorConfig::default()
        })
        .unwrap();
    let fc = FixtureConnector::new(bybit_fixture_dir());

    let raw_btc_1m = fc.fetch_bars_sync("BTCUSDT", "1m", None, 1000).unwrap();
    assert_eq!(raw_btc_1m.len(), 900);
    assert_eq!(raw_btc_1m[0].ts_event, 1_793_491_200_000);
    assert_eq!(raw_btc_1m[0].close, dec("68044.9"));
    assert_eq!(raw_btc_1m.last().unwrap().ts_event, 1_793_545_440_000);
    assert_eq!(raw_btc_1m.last().unwrap().close, dec("68685.3"));

    // Cross-check async HistoryConnector trait matches sync FixtureConnector identically
    assert_eq!(
        connector.fetch_bars("BTCUSDT", "1m", None, 1000).await.unwrap(),
        raw_btc_1m
    );

    let compact_btc_1m = compact_series(&raw_btc_1m, "1m", REPLAY_NOW_MS).unwrap();
    assert_eq!(compact_btc_1m.len(), 905);
    let gaps: Vec<i64> = compact_btc_1m
        .iter()
        .filter(|b| b.status == BarStatus::FeedGap)
        .map(|b| b.timestamp)
        .collect();
    assert_eq!(
        gaps,
        vec![
            1_793_538_000_000,
            1_793_538_060_000,
            1_793_538_120_000,
            1_793_538_180_000,
            1_793_538_240_000,
        ]
    );
    let no_trades: Vec<i64> = compact_btc_1m
        .iter()
        .filter(|b| b.status == BarStatus::NoTrades)
        .map(|b| b.timestamp)
        .collect();
    assert_eq!(no_trades, vec![1_793_540_400_000]);
    let flat = compact_btc_1m
        .iter()
        .find(|b| b.timestamp == 1_793_540_400_000)
        .unwrap();
    assert_eq!(flat.close, Some(dec("68584.8")));
    assert_eq!(flat.volume, Some(Decimal::ZERO));

    // Verify 5m, 1h, ETHUSDT, 1000BONKUSDT boundary parity
    let btc_5m = fc.fetch_bars_sync("BTCUSDT", "5m", None, 1000).unwrap();
    assert_eq!((btc_5m.len(), btc_5m[0].close, btc_5m.last().unwrap().close), (400, dec("67997.5"), dec("67320.6")));
    let btc_1h = fc.fetch_bars_sync("BTCUSDT", "1h", None, 1000).unwrap();
    assert_eq!((btc_1h.len(), btc_1h[0].close, btc_1h.last().unwrap().close), (200, dec("67996.0"), dec("68759.1")));
    let eth_1m = fc.fetch_bars_sync("ETHUSDT", "1m", None, 1000).unwrap();
    assert_eq!((eth_1m.len(), eth_1m[0].close, eth_1m.last().unwrap().close), (400, dec("3496.55"), dec("3513.36")));
    let bonk_1m = fc.fetch_bars_sync("1000BONKUSDT", "1m", None, 1000).unwrap();
    assert_eq!((bonk_1m.len(), bonk_1m[0].close, bonk_1m.last().unwrap().close), (300, dec("0.023501"), dec("0.023560")));

    // 2. Drive `demo_outage.jsonl` through FeedSupervisor + ReplayTransport + StepClock
    let (sup, sink, clock) = run_realtime_fixture_scenario("demo_outage.jsonl", 27).await;

    assert_eq!(
        sink.healths().into_iter().map(|h| h.status).collect::<Vec<_>>(),
        vec![
            FeedHealthStatus::Live,
            FeedHealthStatus::Stale,
            FeedHealthStatus::Recovering,
            FeedHealthStatus::Live,
        ]
    );
    assert_eq!(sup.health().channels[0].reconnect_count, 1);
    assert_eq!(sink.klines().len(), 27);
    assert_eq!(sink.book_count(), 6);

    let replay_recent = sup.recent_bars(INSTRUMENT, "1m", None, false);
    assert_eq!(replay_recent.len(), 15);
    assert_eq!(
        replay_recent
            .iter()
            .map(|b| (b.timestamp, b.status, b.close))
            .collect::<Vec<_>>(),
        vec![
            (1_793_545_500_000, BarStatus::Normal, Some(dec("68055.0"))),
            (1_793_545_560_000, BarStatus::Normal, Some(dec("68061.0"))),
            (1_793_545_620_000, BarStatus::Normal, Some(dec("68050.0"))),
            (1_793_545_680_000, BarStatus::Normal, Some(dec("68044.0"))),
            (1_793_545_740_000, BarStatus::FeedGap, None),
            (1_793_545_800_000, BarStatus::FeedGap, None),
            (1_793_545_860_000, BarStatus::FeedGap, None),
            (1_793_545_920_000, BarStatus::FeedGap, None),
            (1_793_545_980_000, BarStatus::Normal, Some(dec("68020.0"))),
            (1_793_546_040_000, BarStatus::Normal, Some(dec("68030.0"))),
            (1_793_546_100_000, BarStatus::Normal, Some(dec("68035.0"))),
            (1_793_546_160_000, BarStatus::Normal, Some(dec("68028.0"))),
            (1_793_546_220_000, BarStatus::Normal, Some(dec("68033.0"))),
            (1_793_546_280_000, BarStatus::Normal, Some(dec("68040.0"))),
            (1_793_546_340_000, BarStatus::Normal, Some(dec("68044.0"))),
        ]
    );

    // 11 confirmed RawBar closes persisted to BarPublishSink
    let closes = sink.closes.lock().unwrap();
    assert_eq!(closes.len(), 11);
    assert_eq!(
        closes
            .iter()
            .map(|(_, bar, _, rev)| (bar.timestamp, bar.close.unwrap(), *rev))
            .collect::<Vec<_>>(),
        vec![
            (1_793_545_500_000, dec("68055.0"), 2),
            (1_793_545_560_000, dec("68061.0"), 1),
            (1_793_545_620_000, dec("68050.0"), 1),
            (1_793_545_680_000, dec("68044.0"), 1),
            (1_793_545_980_000, dec("68020.0"), 1),
            (1_793_546_040_000, dec("68030.0"), 1),
            (1_793_546_100_000, dec("68035.0"), 1),
            (1_793_546_160_000, dec("68028.0"), 1),
            (1_793_546_220_000, dec("68033.0"), 1),
            (1_793_546_280_000, dec("68040.0"), 1),
            (1_793_546_340_000, dec("68044.0"), 1),
        ]
    );

    // 3. Combined Bybit BTCUSDT 1m (905) + demo_outage.jsonl (15) = 920 contiguous 1m slots
    let mut combined = compact_btc_1m;
    combined.extend(replay_recent);
    assert_eq!(combined.len(), 920);
    assert!(combined.windows(2).all(|w| w[1].timestamp - w[0].timestamp == MINUTE));

    // Also build corresponding RawBar sequence from confirmed closes and verify RawBar -> CompactBar
    let confirmed_raws: Vec<RawBar> = closes
        .iter()
        .map(|(_, b, received, rev)| RawBar {
            symbol: "BTCUSDT".to_owned(),
            timeframe: "1m".to_owned(),
            ts_event: b.timestamp,
            open: b.open.unwrap(),
            high: b.high.unwrap(),
            low: b.low.unwrap(),
            close: b.close.unwrap(),
            volume: b.volume.unwrap(),
            turnover: b.turnover.unwrap(),
            ts_available: b.ts_available,
            ts_ingested: *received,
            ts_committed: *received,
            precision: b.precision,
            revision: *rev,
            confirmed: true,
            history_only: false,
            synthetic: false,
        })
        .collect();
    let compacted_from_closes = compact_series(&confirmed_raws, "1m", clock.now_ms()).unwrap();
    assert_eq!(compacted_from_closes.len(), 15);
    assert_eq!(
        compacted_from_closes
            .iter()
            .map(|b| (b.timestamp, b.status, b.close))
            .collect::<Vec<_>>(),
        sup.recent_bars(INSTRUMENT, "1m", None, false)
            .iter()
            .map(|b| (b.timestamp, b.status, b.close))
            .collect::<Vec<_>>()
    );
}
