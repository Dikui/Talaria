//! Deterministic tests for `FeedSupervisor` state machine, gaps/no-trades, revision dedup,
//! broadcast channel, `BarPublishSink`, and async reconnect + REST backfill.

use std::collections::VecDeque;
use std::str::FromStr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use rust_decimal::Decimal;
use talaria_contracts::{
    BarStatus, CompactBar, DerivativeTickEvent, FeedEvent, FeedHealthEvent, FeedHealthStatus,
    InstrumentSpec, KlineEvent, OrderBookEvent, PrecisionTier, RawBar, ReceivedKlineEvent,
};
use talaria_feed::{
    BarPublishSink, FeedClock, FeedError, FeedSupervisor, FeedSupervisorConfig, ManualClock,
    StepClock,
};
use talaria_packs::{
    BoxFuture, FeedTransport, HistoryConnector, PackError, TransportFrame, get_market_pack,
};

const BASE: i64 = 1_763_596_800_000;
const MINUTE: i64 = 60_000;
const INSTRUMENT: &str = "BTC-USDT-PERP";
const SYMBOL: &str = "BTCUSDT";

fn dec(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn ts(index: i64) -> i64 {
    BASE + index * MINUTE
}

fn compact_bar(
    timestamp: i64,
    close: &str,
    volume: &str,
    status: BarStatus,
    confirmed: bool,
    open_override: Option<&str>,
) -> CompactBar {
    let price = dec(close);
    let opening = open_override.map_or(price, dec);
    CompactBar {
        timestamp,
        open: Some(opening),
        high: Some(opening.max(price)),
        low: Some(opening.min(price)),
        close: Some(price),
        volume: Some(dec(volume)),
        turnover: Some(dec("100")),
        status,
        ts_available: timestamp + if confirmed { MINUTE } else { 1_000 },
        precision: PrecisionTier::SecondVerified,
    }
}

fn kline(timestamp: i64, close: &str, confirmed: bool) -> KlineEvent {
    KlineEvent {
        instrument_id: INSTRUMENT.to_owned(),
        timeframe: "1m".to_owned(),
        bar: compact_bar(timestamp, close, "1.5", BarStatus::Normal, confirmed, None),
        confirmed,
        revision: 0,
    }
}

fn book(instrument_id: &str, now: i64) -> OrderBookEvent {
    OrderBookEvent {
        instrument_id: instrument_id.to_owned(),
        bid_price: dec("100.0"),
        bid_size: dec("2.5"),
        ask_price: dec("100.1"),
        ask_size: dec("3.5"),
        ts_event: now,
        ts_ingested: now,
    }
}

fn raw_bar(timestamp: i64, close: &str, volume: &str) -> RawBar {
    let price = dec(close);
    RawBar {
        symbol: SYMBOL.to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: timestamp,
        open: price,
        high: price,
        low: price,
        close: price,
        volume: dec(volume),
        turnover: dec("200"),
        ts_available: timestamp + MINUTE,
        ts_ingested: timestamp + MINUTE,
        ts_committed: timestamp + MINUTE,
        precision: PrecisionTier::SecondVerified,
        revision: 0,
        confirmed: true,
        history_only: false,
        synthetic: false,
    }
}

type CloseRecord = (String, String, CompactBar, i64, u64);
type FetchCall = (String, String, Option<i64>, usize);

#[derive(Default)]
struct RecordingSink {
    subscribes: Mutex<Vec<(String, String, i64)>>,
    closes: Mutex<Vec<CloseRecord>>,
    recovered: Mutex<Vec<(String, RawBar)>>,
    events: Mutex<Vec<FeedEvent>>,
    received_klines: Mutex<Vec<ReceivedKlineEvent>>,
}

impl RecordingSink {
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

    fn derivatives(&self) -> Vec<DerivativeTickEvent> {
        self.events
            .lock()
            .unwrap()
            .iter()
            .filter_map(|e| match e {
                FeedEvent::DerivativeTick(d) => Some(d.clone()),
                _ => None,
            })
            .collect()
    }

    fn received_klines(&self) -> Vec<ReceivedKlineEvent> {
        self.received_klines.lock().unwrap().clone()
    }
}

impl BarPublishSink for RecordingSink {
    fn on_subscribe(&self, instrument: &InstrumentSpec, timeframe: &str, subscribed_ts: i64) {
        self.subscribes.lock().unwrap().push((
            instrument.instrument_id.clone(),
            timeframe.to_owned(),
            subscribed_ts,
        ));
    }

    fn on_bar_close(
        &self,
        instrument: &InstrumentSpec,
        timeframe: &str,
        bar: &CompactBar,
        received_ts: i64,
        revision: u64,
    ) {
        self.closes.lock().unwrap().push((
            instrument.instrument_id.clone(),
            timeframe.to_owned(),
            bar.clone(),
            received_ts,
            revision,
        ));
    }

    fn on_recovered_bar(&self, instrument: &InstrumentSpec, bar: &RawBar) {
        self.recovered
            .lock()
            .unwrap()
            .push((instrument.instrument_id.clone(), bar.clone()));
    }

    fn on_kline_event(&self, received: &ReceivedKlineEvent) {
        self.received_klines.lock().unwrap().push(received.clone());
        self.on_event(&FeedEvent::Kline(received.event.clone()));
    }

    fn on_event(&self, event: &FeedEvent) {
        self.events.lock().unwrap().push(event.clone());
    }
}

struct StubTransport;

impl FeedTransport for StubTransport {
    fn feed_mode(&self) -> &str {
        "stub"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }

    fn subscribe<'a>(
        &'a self,
        _instrument_id: &'a str,
        _timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }

    fn unsubscribe<'a>(
        &'a self,
        _instrument_id: &'a str,
        _timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async { Err(PackError::FeedTransport("stub transport".to_owned())) })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }
}

struct StubConnector {
    bars: Vec<RawBar>,
    calls: Mutex<Vec<FetchCall>>,
}

impl StubConnector {
    fn new(bars: Vec<RawBar>) -> Self {
        Self {
            bars,
            calls: Mutex::new(Vec::new()),
        }
    }
}

impl HistoryConnector for StubConnector {
    fn feed_mode(&self) -> &str {
        "stub"
    }

    fn fetch_bars<'a>(
        &'a self,
        symbol: &'a str,
        timeframe: &'a str,
        end_ts: Option<i64>,
        limit: usize,
    ) -> BoxFuture<'a, Result<Vec<RawBar>, PackError>> {
        Box::pin(async move {
            self.calls
                .lock()
                .unwrap()
                .push((symbol.to_owned(), timeframe.to_owned(), end_ts, limit));
            Ok(self.bars.clone())
        })
    }
}

fn make_supervisor(
    initial_now: i64,
    subscribe_default: bool,
) -> (Arc<FeedSupervisor>, Arc<StepClock>, Arc<RecordingSink>) {
    let clock = Arc::new(StepClock::new(initial_now));
    let sink = Arc::new(RecordingSink::default());
    let pack = get_market_pack("crypto_perp").unwrap();
    let sup = Arc::new(
        FeedSupervisor::new(
            Arc::new(StubTransport),
            None,
            pack.instruments().to_vec(),
            clock.clone(),
            sink.clone(),
            FeedSupervisorConfig::default(),
        )
        .unwrap(),
    );
    if subscribe_default {
        sup.subscribe(INSTRUMENT, "1m").unwrap();
    }
    (sup, clock, sink)
}

#[test]
fn in_progress_updates_and_confirmation_increment_revision() {
    let (sup, clock, sink) = make_supervisor(BASE + 1_000, true);
    let mut rx = sup.subscribe_events();

    sup.ingest(kline(ts(0), "100", false), Some(clock.now_ms()));
    clock.advance_ms(500);
    sup.ingest(kline(ts(0), "101", false), Some(clock.now_ms()));
    clock.advance_ms(500);
    sup.ingest(kline(ts(0), "101", true), Some(clock.now_ms()));

    let bars = sink.klines();
    assert_eq!(
        bars.iter().map(|b| b.revision).collect::<Vec<_>>(),
        vec![0, 1, 2]
    );
    assert_eq!(
        bars.iter().map(|b| b.confirmed).collect::<Vec<_>>(),
        vec![false, false, true]
    );
    assert!(bars.iter().all(|b| b.bar.timestamp == ts(0)));

    // Only confirmed bar close reaches BarPublishSink::on_bar_close
    let closes = sink.closes.lock().unwrap();
    assert_eq!(closes.len(), 1);
    assert_eq!(closes[0].4, 2);

    // Broadcast channel received the same 3 KlineEvents
    let mut bcast_revs = Vec::new();
    while let Ok(ev) = rx.try_recv() {
        if let FeedEvent::Kline(k) = ev {
            bcast_revs.push(k.revision);
        }
    }
    assert_eq!(bcast_revs, vec![0, 1, 2]);
}

#[test]
fn identical_republish_is_idempotent_and_last_write_wins() {
    let (sup, clock, sink) = make_supervisor(BASE + MINUTE + 1_000, true);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));
    clock.advance_ms(100);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));
    assert_eq!(sink.klines().len(), 1);
    // Retry resilience: on_bar_close is invoked on re-confirm so failed store writes can recover.
    assert_eq!(sink.closes.lock().unwrap().len(), 2);

    // Later differing confirmed write increments revision and wins.
    clock.advance_ms(10);
    sup.ingest(kline(ts(0), "100.5", true), Some(clock.now_ms()));
    let bars = sink.klines();
    assert_eq!(bars.iter().map(|b| b.revision).collect::<Vec<_>>(), vec![0, 1]);
    assert_eq!(bars.last().unwrap().bar.close, Some(dec("100.5")));

    // In-progress regression after confirmation is ignored.
    sup.ingest(kline(ts(0), "99.0", false), Some(clock.now_ms()));
    assert_eq!(sink.klines().len(), 2);
}

#[test]
fn close_availability_and_revisions_follow_receipt_time_grace_window() {
    for (delay, first_offset) in [(5_000i64, 0i64), (5_001i64, 5_001i64)] {
        let (sup, _clock, sink) = make_supervisor(BASE, true);
        let end = ts(1);
        sup.ingest(kline(ts(0), "99", false), Some(end - 1));
        sup.ingest(kline(ts(0), "100", true), Some(end + delay));
        sup.ingest(kline(ts(0), "101", true), Some(end + 7_000));
        sup.ingest(kline(ts(0), "102", true), Some(end + 6_000));

        let avail: Vec<i64> = sink
            .klines()
            .into_iter()
            .filter(|k| k.confirmed)
            .map(|k| k.bar.ts_available)
            .collect();
        assert_eq!(avail, vec![end + first_offset, end + 7_000, end + 7_000]);
    }
}

#[test]
fn parser_receipt_time_preserved_for_received_kline_even_when_delayed() {
    let (sup, _clock, sink) = make_supervisor(BASE, true);
    let end = ts(1);
    sup.ingest(
        ReceivedKlineEvent {
            event: kline(ts(0), "100", true),
            ts_ingested: end + 5_000,
            history_only: false,
            synthetic: false,
        },
        Some(end + 20_000),
    );
    sup.ingest(
        ReceivedKlineEvent {
            event: kline(ts(0), "101", true),
            ts_ingested: end + 8_000,
            history_only: false,
            synthetic: false,
        },
        Some(end + 20_000),
    );
    let avail: Vec<i64> = sink
        .klines()
        .into_iter()
        .map(|k| k.bar.ts_available)
        .collect();
    assert_eq!(avail, vec![end, end + 8_000]);
}

#[test]
fn missing_minutes_without_transport_loss_become_no_trades() {
    let (sup, clock, sink) = make_supervisor(ts(0) + 1_000, true);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));
    clock.set_wall_ms(ts(2) + 1_000);
    sup.ingest(kline(ts(2), "102", true), Some(clock.now_ms()));

    let bars = sink.klines();
    assert_eq!(
        bars.iter().map(|b| b.bar.timestamp).collect::<Vec<_>>(),
        vec![ts(0), ts(1), ts(2)]
    );
    let synth = &bars[1].bar;
    assert_eq!(synth.status, BarStatus::NoTrades);
    assert_eq!(synth.open, Some(dec("100")));
    assert_eq!(synth.high, Some(dec("100")));
    assert_eq!(synth.low, Some(dec("100")));
    assert_eq!(synth.close, Some(dec("100")));
    assert_eq!(synth.volume, Some(Decimal::ZERO));
    assert_eq!(synth.turnover, Some(Decimal::ZERO));
    assert!(bars[1].confirmed);
    assert_eq!(bars[1].revision, 0);

    // Synthesized NO_TRADES bar must NOT be archived via on_bar_close
    assert_eq!(sink.closes.lock().unwrap().len(), 2);
}

#[test]
fn transport_loss_marks_stale_and_reconnect_synthesizes_feed_gap() {
    let (sup, clock, sink) = make_supervisor(ts(0) + 1_000, true);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));

    sup.transport_lost(Some(clock.set_wall_ms(ts(0) + 10_000)));
    clock.set_wall_ms(ts(2) + 10_000);
    sup.transport_restored(Some(clock.now_ms()), &[]);

    let statuses: Vec<FeedHealthStatus> = sink.healths().into_iter().map(|h| h.status).collect();
    assert_eq!(
        statuses,
        vec![
            FeedHealthStatus::Live,
            FeedHealthStatus::Stale,
            FeedHealthStatus::Recovering,
            FeedHealthStatus::Live,
        ]
    );
    assert_eq!(sink.healths()[1].stale_since, Some(ts(0) + 1_000));
    let gaps: Vec<KlineEvent> = sink
        .klines()
        .into_iter()
        .filter(|k| k.bar.status == BarStatus::FeedGap)
        .collect();
    assert_eq!(
        gaps.iter().map(|g| g.bar.timestamp).collect::<Vec<_>>(),
        vec![ts(1)]
    );
    assert_eq!(gaps[0].bar.open, None);
    assert_eq!(gaps[0].bar.close, None);
    assert_eq!(sup.health().status, FeedHealthStatus::Live);
}

#[test]
fn rest_backfill_fills_gap_and_is_idempotent() {
    let (sup, clock, sink) = make_supervisor(ts(0) + 1_000, true);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));

    sup.transport_lost(Some(clock.set_wall_ms(ts(0) + 10_000)));
    let backfill = vec![raw_bar(ts(1), "105", "2")];
    sup.transport_restored(Some(clock.set_wall_ms(ts(2) + 10_000)), &backfill);

    let bars = sink.klines();
    assert_eq!(
        bars.iter().map(|b| b.bar.timestamp).collect::<Vec<_>>(),
        vec![ts(0), ts(1)]
    );
    assert_eq!(bars.last().unwrap().bar.close, Some(dec("105")));
    assert!(bars.last().unwrap().confirmed);
    assert_eq!(sink.recovered.lock().unwrap().len(), 1);

    sup.transport_lost(Some(clock.set_wall_ms(ts(3) + 10_000)));
    sup.transport_restored(Some(clock.set_wall_ms(ts(3) + 20_000)), &backfill);

    let ts1_bars: Vec<KlineEvent> = sink
        .klines()
        .into_iter()
        .filter(|k| k.bar.timestamp == ts(1))
        .collect();
    assert_eq!(ts1_bars.len(), 1);
    let last = sink.klines().pop().unwrap();
    assert_eq!(last.bar.timestamp, ts(2));
    assert_eq!(last.bar.status, BarStatus::FeedGap);

    sup.ingest(kline(ts(1), "106", true), Some(clock.advance_ms(10)));
    let updated = sink.klines().pop().unwrap();
    assert_eq!(updated.revision, 1);
    assert_eq!(updated.bar.close, Some(dec("106")));
}

#[test]
fn tick_declares_stale_after_twelve_seconds_and_aggregates_worst_status() {
    let (sup, clock, sink) = make_supervisor(BASE + 1_000, true);
    sup.ingest(book(INSTRUMENT, clock.now_ms()), Some(clock.now_ms()));

    sup.tick(Some(clock.set_wall_ms(BASE + 12_999)));
    assert_eq!(sup.health().status, FeedHealthStatus::Live);
    assert_eq!(sink.healths().len(), 1);

    sup.tick(Some(clock.set_wall_ms(BASE + 13_000)));
    let stale = sink.healths().pop().unwrap();
    assert_eq!(stale.status, FeedHealthStatus::Stale);
    assert_eq!(stale.stale_since, Some(BASE + 1_000));
    let before = sink.healths().len();

    sup.tick(Some(clock.set_wall_ms(BASE + 20_000)));
    assert_eq!(sink.healths().len(), before);

    sup.ingest(book(INSTRUMENT, clock.now_ms()), Some(clock.now_ms()));
    let recovered = sink.healths().pop().unwrap();
    assert_eq!(recovered.status, FeedHealthStatus::Live);
    assert_eq!(recovered.stale_since, None);

    // Multi-channel worst_status aggregation and unsubscribe
    let (sup2, clock2, _) = make_supervisor(BASE, false);
    sup2.subscribe("BTC-USDT-PERP", "1m").unwrap();
    sup2.subscribe("ETH-USDT-PERP", "1m").unwrap();
    sup2.ingest(
        book("BTC-USDT-PERP", clock2.set_wall_ms(BASE + 5_000)),
        Some(clock2.now_ms()),
    );
    sup2.tick(Some(clock2.set_wall_ms(BASE + 12_000)));
    let report = sup2.health();
    assert_eq!(report.status, FeedHealthStatus::Stale);
    assert_eq!(report.feed_mode, "stub");
    assert_eq!(
        report
            .channels
            .iter()
            .map(|c| (c.instrument_id.as_str(), c.status))
            .collect::<Vec<_>>(),
        vec![
            ("BTC-USDT-PERP", FeedHealthStatus::Live),
            ("ETH-USDT-PERP", FeedHealthStatus::Stale),
        ]
    );

    sup2.unsubscribe("ETH-USDT-PERP", "1m");
    let report2 = sup2.health();
    assert_eq!(report2.status, FeedHealthStatus::Live);
    assert_eq!(report2.channels.len(), 1);
}

#[test]
fn unsubscribed_and_derivative_tick_behavior() {
    let (sup, clock, sink) = make_supervisor(BASE + 1_000, true);
    assert!(matches!(
        sup.subscribe("DOGE-USDT-PERP", "1m"),
        Err(FeedError::InstrumentNotFound(_))
    ));
    assert!(matches!(
        sup.subscribe(INSTRUMENT, "2m"),
        Err(FeedError::InvalidRequest(_))
    ));

    let dt = DerivativeTickEvent {
        instrument_id: INSTRUMENT.to_owned(),
        mark_price: Some(dec("100.5")),
        funding_rate: Some(dec("0.0001")),
        next_funding_ts: Some(clock.now_ms() + 8 * 3_600_000),
        open_interest: Some(dec("12345.6")),
        ts_event: clock.now_ms(),
        ts_ingested: clock.now_ms(),
    };
    sup.ingest(dt, Some(clock.now_ms()));
    assert_eq!(sink.derivatives().len(), 1);
    assert!(sup.recent_bars(INSTRUMENT, "1m", None, false).is_empty());

    // Unsubscribed instrument derivative tick & kline are dropped
    sup.ingest(
        DerivativeTickEvent {
            instrument_id: "ETH-USDT-PERP".to_owned(),
            mark_price: Some(dec("100.5")),
            funding_rate: None,
            next_funding_ts: None,
            open_interest: None,
            ts_event: clock.now_ms(),
            ts_ingested: clock.now_ms(),
        },
        Some(clock.now_ms()),
    );
    assert_eq!(sink.derivatives().len(), 1);

    sup.unsubscribe(INSTRUMENT, "1m");
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));
    assert!(sink.klines().is_empty());
}

struct SequenceMockTransport {
    steps: Mutex<VecDeque<Result<Option<TransportFrame>, PackError>>>,
    connect_failures: Mutex<usize>,
    subscribes: Mutex<Vec<Vec<(String, String)>>>,
    current_sub: Mutex<Vec<(String, String)>>,
    clock: Arc<ManualClock>,
}

impl SequenceMockTransport {
    fn new(
        connect_failures_after_first: usize,
        steps: Vec<Result<Option<TransportFrame>, PackError>>,
        clock: Arc<ManualClock>,
    ) -> Self {
        Self {
            steps: Mutex::new(steps.into()),
            connect_failures: Mutex::new(connect_failures_after_first),
            subscribes: Mutex::new(Vec::new()),
            current_sub: Mutex::new(Vec::new()),
            clock,
        }
    }
}

impl FeedTransport for SequenceMockTransport {
    fn feed_mode(&self) -> &str {
        "mock"
    }

    fn connect(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async move {
            let mut prev = self.current_sub.lock().unwrap();
            if !prev.is_empty() {
                self.subscribes.lock().unwrap().push(std::mem::take(&mut *prev));
            }
            if !self.subscribes.lock().unwrap().is_empty() {
                let mut fails = self.connect_failures.lock().unwrap();
                if *fails > 0 {
                    *fails -= 1;
                    return Err(PackError::FeedTransport("simulated outage connect failure".to_owned()));
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
            self.current_sub
                .lock()
                .unwrap()
                .push((instrument_id.to_owned(), timeframe.to_owned()));
            Ok(())
        })
    }

    fn unsubscribe<'a>(
        &'a self,
        _instrument_id: &'a str,
        _timeframe: &'a str,
    ) -> BoxFuture<'a, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }

    fn recv(&self) -> BoxFuture<'_, Result<Option<TransportFrame>, PackError>> {
        Box::pin(async move {
            if let Some(step) = self.steps.lock().unwrap().pop_front() {
                return step;
            }
            self.clock.sleep(Duration::from_secs(3600)).await;
            Ok(None)
        })
    }

    fn close(&self) -> BoxFuture<'_, Result<(), PackError>> {
        Box::pin(async { Ok(()) })
    }
}

#[tokio::test]
async fn async_supervisor_reconnect_backoff_resubscribe_and_rest_backfill() {
    let clock = Arc::new(ManualClock::new(ts(3)));
    let transport = Arc::new(SequenceMockTransport::new(
        1, // Fail first reconnect attempt, succeed on second -> tests exponential backoff (200ms, 400ms)
        vec![
            Ok(Some(TransportFrame::from(kline(ts(0), "100", true)))),
            Err(PackError::FeedTransport("socket drop".to_owned())),
            Ok(Some(TransportFrame::from(kline(ts(2), "102", true)))),
        ],
        Arc::clone(&clock),
    ));
    let connector = Arc::new(StubConnector::new(vec![
        raw_bar(ts(0), "99", "1"), // Already source_confirmed -> must be ignored
        raw_bar(ts(1), "105", "3"), // Gap filled via REST backfill!
    ]));
    let sink = Arc::new(RecordingSink::default());
    let pack = get_market_pack("crypto_perp").unwrap();
    let sup = Arc::new(
        FeedSupervisor::new(
            transport.clone(),
            Some(connector.clone()),
            pack.instruments().to_vec(),
            clock.clone(),
            sink.clone(),
            FeedSupervisorConfig {
                tick_interval: None,
                backoff_initial: Duration::from_millis(200),
                backoff_max: Duration::from_millis(5_000),
                ..FeedSupervisorConfig::default()
            },
        )
        .unwrap(),
    );
    let mut rx = sup.subscribe_events();
    sup.subscribe(INSTRUMENT, "1m").unwrap();
    sup.start().await.unwrap();

    // Wait for STALE from disconnect, then advance virtual clock through 200ms + 400ms backoff
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            match rx.recv().await {
                Ok(FeedEvent::Health(h)) if h.status == FeedHealthStatus::Stale => break,
                Ok(_) => {}
                Err(e) => panic!("broadcast error: {e}"),
            }
        }
    })
    .await
    .unwrap();

    // Advance virtual clock for first backoff (200ms) -> connect fails -> second backoff (400ms) -> reconnect!
    tokio::task::yield_now().await;
    clock.advance_ms(200);
    tokio::task::yield_now().await;
    clock.advance_ms(400);

    tokio::time::timeout(Duration::from_secs(2), async {
        let mut saw_ts2 = false;
        while !saw_ts2 {
            if let Ok(FeedEvent::Kline(k)) = rx.recv().await
                && k.bar.timestamp == ts(2)
            {
                saw_ts2 = true;
            }
        }
    })
    .await
    .unwrap();

    sup.stop().await;

    // Verify exponential backoff delays recorded cleanly on ManualClock
    let sleeps = clock.sleeps();
    assert!(sleeps.contains(&Duration::from_millis(200)));
    assert!(sleeps.contains(&Duration::from_millis(400)));

    // Verify health sequence: LIVE -> STALE -> RECOVERING -> LIVE
    let statuses: Vec<FeedHealthStatus> = sink.healths().into_iter().map(|h| h.status).collect();
    assert_eq!(
        statuses,
        vec![
            FeedHealthStatus::Live,
            FeedHealthStatus::Stale,
            FeedHealthStatus::Recovering,
            FeedHealthStatus::Live,
        ]
    );

    // Verify REST backfill filled ts(1) without overwriting source-confirmed ts(0)
    let recent = sup.recent_bars(INSTRUMENT, "1m", None, true);
    assert_eq!(
        recent
            .iter()
            .map(|b| (b.timestamp, b.close, b.status))
            .collect::<Vec<_>>(),
        vec![
            (ts(0), Some(dec("100")), BarStatus::Normal),
            (ts(1), Some(dec("105")), BarStatus::Normal),
            (ts(2), Some(dec("102")), BarStatus::Normal),
        ]
    );
    assert_eq!(sink.recovered.lock().unwrap().len(), 1);
    assert_eq!(connector.calls.lock().unwrap().len(), 1);
    assert!(transport.current_sub.lock().unwrap().contains(&(INSTRUMENT.to_owned(), "1m".to_owned())));
}

#[tokio::test]
async fn manual_clock_tick_loop_marks_stale_without_real_sleep() {
    let clock = Arc::new(ManualClock::new(BASE));
    let sink = Arc::new(RecordingSink::default());
    let pack = get_market_pack("crypto_perp").unwrap();
    let sup = Arc::new(
        FeedSupervisor::new(
            Arc::new(SequenceMockTransport::new(0, Vec::new(), Arc::clone(&clock))),
            None,
            pack.instruments().to_vec(),
            clock.clone(),
            sink.clone(),
            FeedSupervisorConfig {
                stale_after_ms: 12_000,
                tick_interval: Some(Duration::from_millis(4_000)),
                ..FeedSupervisorConfig::default()
            },
        )
        .unwrap(),
    );
    let mut rx = sup.subscribe_events();
    sup.subscribe(INSTRUMENT, "1m").unwrap();
    sup.start().await.unwrap();

    for _ in 0..3 {
        tokio::task::yield_now().await;
        clock.advance_ms(4_000);
    }

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Ok(FeedEvent::Health(h)) = rx.recv().await
                && h.status == FeedHealthStatus::Stale
            {
                assert_eq!(h.stale_since, Some(BASE));
                break;
            }
        }
    })
    .await
    .unwrap();
    sup.stop().await;
}

#[test]
fn subscribing_during_active_outage_starts_stale_and_recovers_on_restore() {
    let (sup, clock, sink) = make_supervisor(ts(0) + 1_000, true);
    sup.ingest(kline(ts(0), "100", true), Some(clock.now_ms()));

    let lost_at = clock.set_wall_ms(ts(0) + 10_000);
    sup.transport_lost(Some(lost_at));

    // Subscribing a new timeframe during an active outage must inherit STALE + outage start timestamp.
    clock.set_wall_ms(ts(0) + 15_000);
    sup.subscribe(INSTRUMENT, "5m").unwrap();

    let health_5m = sup.channel_health(INSTRUMENT, "5m").unwrap();
    assert_eq!(health_5m.status, FeedHealthStatus::Stale);
    assert_eq!(health_5m.stale_since, Some(lost_at));
    assert_eq!(health_5m.reconnect_count, 0);
    assert_eq!(sup.health().status, FeedHealthStatus::Stale);

    // Staleness tick must leave the already-STALE channel untouched.
    sup.tick(Some(clock.set_wall_ms(ts(0) + 30_000)));
    let health_5m_after_tick = sup.channel_health(INSTRUMENT, "5m").unwrap();
    assert_eq!(health_5m_after_tick.status, FeedHealthStatus::Stale);
    assert_eq!(health_5m_after_tick.stale_since, Some(lost_at));

    // Layout switch unsubscribing + resubscribing an existing channel during the outage also stays STALE.
    sup.unsubscribe(INSTRUMENT, "1m");
    clock.set_wall_ms(ts(0) + 35_000);
    sup.subscribe(INSTRUMENT, "1m").unwrap();
    let resub_1m = sup.channel_health(INSTRUMENT, "1m").unwrap();
    assert_eq!(resub_1m.status, FeedHealthStatus::Stale);
    assert_eq!(resub_1m.stale_since, Some(lost_at));
    assert_eq!(resub_1m.reconnect_count, 0);

    // Restoring transport transitions through RECOVERING -> LIVE with reconnect_count == 1.
    let mut backfill_5m = raw_bar(ts(0), "101", "5");
    backfill_5m.timeframe = "5m".to_owned();
    backfill_5m.ts_available = ts(5);
    backfill_5m.ts_ingested = ts(5);
    backfill_5m.ts_committed = ts(5);
    sup.transport_restored(Some(clock.set_wall_ms(ts(2) + 10_000)), &[backfill_5m]);

    let healths_5m: Vec<(FeedHealthStatus, Option<i64>, u64)> = sink
        .healths()
        .into_iter()
        .filter(|h| h.timeframe == "5m")
        .map(|h| (h.status, h.stale_since, h.reconnect_count))
        .collect();
    assert_eq!(
        healths_5m,
        vec![
            (FeedHealthStatus::Stale, Some(lost_at), 0),
            (FeedHealthStatus::Recovering, Some(lost_at), 1),
            (FeedHealthStatus::Live, None, 1),
        ]
    );

    let live_5m = sup.channel_health(INSTRUMENT, "5m").unwrap();
    assert_eq!(live_5m.status, FeedHealthStatus::Live);
    assert_eq!(live_5m.stale_since, None);
    assert_eq!(live_5m.reconnect_count, 1);

    let live_1m = sup.channel_health(INSTRUMENT, "1m").unwrap();
    assert_eq!(live_1m.status, FeedHealthStatus::Live);
    assert_eq!(live_1m.stale_since, None);
    assert_eq!(live_1m.reconnect_count, 1);

    // Because 5m joined resume_pending on subscribe, the first post-outage kline gap becomes FEED_GAP.
    clock.set_wall_ms(ts(10) + 1_000);
    let mut kline_5m = kline(ts(10), "103", true);
    kline_5m.timeframe = "5m".to_owned();
    sup.ingest(kline_5m, Some(clock.now_ms()));

    let bars_5m = sup.recent_bars(INSTRUMENT, "5m", None, false);
    assert_eq!(
        bars_5m
            .iter()
            .map(|b| (b.timestamp, b.status))
            .collect::<Vec<_>>(),
        vec![
            (ts(0), BarStatus::Normal),
            (ts(5), BarStatus::FeedGap),
            (ts(10), BarStatus::Normal),
        ]
    );
}

#[tokio::test]
async fn stale_watchdog_forces_transport_reconnect_and_recovery() {
    let clock = Arc::new(ManualClock::new(BASE));
    let transport = Arc::new(SequenceMockTransport::new(0, Vec::new(), Arc::clone(&clock)));
    let sink = Arc::new(RecordingSink::default());
    let pack = get_market_pack("crypto_perp").unwrap();
    let sup = Arc::new(
        FeedSupervisor::new(
            transport.clone(),
            None,
            pack.instruments().to_vec(),
            clock.clone(),
            sink.clone(),
            FeedSupervisorConfig {
                stale_after_ms: 12_000,
                tick_interval: Some(Duration::from_millis(4_000)),
                backoff_initial: Duration::from_millis(200),
                backoff_max: Duration::from_millis(5_000),
                ..FeedSupervisorConfig::default()
            },
        )
        .unwrap(),
    );
    let mut rx = sup.subscribe_events();
    sup.subscribe(INSTRUMENT, "1m").unwrap();
    sup.start().await.unwrap();

    // Advance 12s -> channel becomes STALE without triggering reconnect yet.
    for _ in 0..3 {
        tokio::task::yield_now().await;
        clock.advance_ms(4_000);
    }
    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Ok(FeedEvent::Health(h)) = rx.recv().await
                && h.status == FeedHealthStatus::Stale
            {
                assert_eq!(h.reconnect_count, 0);
                break;
            }
        }
    })
    .await
    .unwrap();

    // Advance another 12s (24s total silence = 2 * stale_after_ms) -> watchdog forces reconnect.
    for _ in 0..3 {
        tokio::task::yield_now().await;
        clock.advance_ms(4_000);
    }
    // Advance past initial backoff (200ms) so reconnect completes.
    tokio::task::yield_now().await;
    clock.advance_ms(200);

    tokio::time::timeout(Duration::from_secs(2), async {
        loop {
            if let Ok(FeedEvent::Health(h)) = rx.recv().await
                && h.status == FeedHealthStatus::Live
                && h.reconnect_count >= 1
            {
                break;
            }
        }
    })
    .await
    .unwrap();

    sup.stop().await;
    let final_health = sup.channel_health(INSTRUMENT, "1m").unwrap();
    assert_eq!(final_health.status, FeedHealthStatus::Live);
    assert!(final_health.reconnect_count >= 1);
}

#[test]
fn unsubscribe_clears_series_and_resubscribe_does_not_synthesize_no_trades() {
    let (sup, clock, sink) = make_supervisor(BASE + 1_000, false);
    sup.subscribe(INSTRUMENT, "15m").unwrap();

    // Unconfirmed bar arrives on 15m before switching away.
    let mut open_15m = kline(ts(0), "59392", false);
    open_15m.timeframe = "15m".to_owned();
    open_15m.bar.ts_available = ts(0) + 1_000;
    sup.ingest(open_15m, Some(clock.now_ms()));

    // Switch away (unsubscribe) and return 5 intervals later.
    sup.unsubscribe(INSTRUMENT, "15m");
    clock.set_wall_ms(ts(75) + 1_000);
    sup.subscribe(INSTRUMENT, "15m").unwrap();

    let mut resumed_15m = kline(ts(75), "59298", false);
    resumed_15m.timeframe = "15m".to_owned();
    resumed_15m.bar.ts_available = ts(75) + 1_000;
    sup.ingest(resumed_15m, Some(clock.now_ms()));

    assert!(
        sink.klines()
            .iter()
            .all(|k| k.bar.status != BarStatus::NoTrades),
        "resubscribing after unsubscribe must not fabricate NO_TRADES bars across the unsubscribed span"
    );
    let recent = sup.recent_bars(INSTRUMENT, "15m", None, false);
    assert_eq!(recent.len(), 1);
    assert_eq!(recent[0].timestamp, ts(75));
}

#[test]
fn on_kline_event_preserves_synthetic_flag_for_live_gap_no_trades() {
    let (sup, clock, sink) = make_supervisor(BASE + 1_000, false);
    sup.subscribe(INSTRUMENT, "1m").unwrap();

    sup.ingest(kline(ts(0), "100", true), Some(clock.set_wall_ms(ts(1) + 500)));
    sup.ingest(kline(ts(2), "101", false), Some(clock.set_wall_ms(ts(2) + 500)));

    let received = sink.received_klines();
    let real_bar = received.iter().find(|r| r.event.bar.timestamp == ts(0)).unwrap();
    let synth_bar = received.iter().find(|r| r.event.bar.timestamp == ts(1)).unwrap();
    assert!(!real_bar.synthetic);
    assert!(synth_bar.synthetic);
    assert_eq!(synth_bar.event.bar.status, BarStatus::NoTrades);
}
