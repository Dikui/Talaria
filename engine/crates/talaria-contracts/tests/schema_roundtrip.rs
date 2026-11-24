use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use rust_decimal::Decimal;
use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use talaria_contracts::*;
use uuid::Uuid;

fn schemas_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("contracts/schemas")
}

fn assert_schema_roundtrip<T>(schema_name: &str, value: &T, covered: &mut BTreeSet<String>)
where
    T: Serialize + DeserializeOwned + PartialEq + core::fmt::Debug,
{
    let filename = format!("{schema_name}.schema.json");
    let path = schemas_dir().join(&filename);
    let schema_text = fs::read_to_string(&path)
        .unwrap_or_else(|err| panic!("failed to read schema {}: {err}", path.display()));
    let schema: Value = serde_json::from_str(&schema_text)
        .unwrap_or_else(|err| panic!("invalid JSON in schema {}: {err}", path.display()));

    let validator = jsonschema::validator_for(&schema)
        .unwrap_or_else(|err| panic!("failed to compile schema {filename}: {err}"));

    let serialized = serde_json::to_value(value)
        .unwrap_or_else(|err| panic!("failed to serialize {schema_name}: {err}"));
    if let Err(err) = validator.validate(&serialized) {
        panic!("serialized {schema_name} failed schema {filename}: {err}\npayload: {serialized}");
    }

    let roundtrip: T = serde_json::from_value(serialized.clone())
        .unwrap_or_else(|err| panic!("failed to deserialize {schema_name}: {err}\npayload: {serialized}"));
    assert_eq!(&roundtrip, value, "round-trip mismatch for {schema_name}");

    let reserialized = serde_json::to_value(&roundtrip).unwrap();
    if let Err(err) = validator.validate(&reserialized) {
        panic!("reserialized {schema_name} failed schema {filename}: {err}");
    }

    covered.insert(filename);
}

fn sample_compact_bar() -> CompactBar {
    CompactBar {
        timestamp: 1_700_000_000_000,
        open: Some(Decimal::from_str("101.10").unwrap()),
        high: Some(Decimal::from_str("102.20").unwrap()),
        low: Some(Decimal::from_str("100.00").unwrap()),
        close: Some(Decimal::from_str("101.50").unwrap()),
        volume: Some(Decimal::from_str("12.345").unwrap()),
        turnover: Some(Decimal::from_str("1250.75").unwrap()),
        status: BarStatus::Normal,
        ts_available: 1_700_000_060_000,
        precision: PrecisionTier::SecondVerified,
    }
}

fn sample_instrument_spec() -> InstrumentSpec {
    InstrumentSpec {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "BTCUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::from_str("0.1").unwrap(),
        qty_step: Decimal::from_str("0.001").unwrap(),
        min_qty: Decimal::from_str("0.001").unwrap(),
        price_precision: 1,
        qty_precision: 3,
    }
}

fn sample_rule() -> Rule {
    Rule {
        version: RuleVersion::V1,
        timing: RuleTiming::BarClose,
        expression: RuleExpression::All {
            args: vec![RuleExpression::Compare {
                test: RuleComparisonTest::Gt,
                left: Box::new(RuleOperand::Indicator {
                    name: RuleIndicatorName::Ema,
                    version: RuleVersion::V1,
                    window: RuleIndicatorWindow::Alert,
                    snapshot_id: None,
                    channel: Some("value".to_owned()),
                    params: RuleIndicatorParams {
                        period: Some(9),
                        anchor: None,
                        std_dev: None,
                    },
                }),
                right: Box::new(RuleOperand::Constant {
                    value: Decimal::from_str("100.5").unwrap(),
                }),
            }],
        },
    }
}

fn sample_drawing_point() -> DrawingPoint {
    DrawingPoint {
        ts: 1_700_000_000_000,
        price: Decimal::from_str("65000.5").unwrap(),
    }
}

fn sample_drawing_record() -> DrawingRecord {
    DrawingRecord {
        drawing_id: "drw_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        kind: DrawingKind::HorizontalLine,
        visible_timeframes: vec!["1m".to_owned(), "15m".to_owned()],
        points: vec![sample_drawing_point()],
        text: "Support".to_owned(),
        style: DrawingStyle::default(),
        version: 1,
        created_ts: 1_700_000_000_000,
        updated_ts: 1_700_000_000_000,
        deleted_at: None,
    }
}

fn sample_evidence_ref() -> EvidenceRef {
    EvidenceRef {
        evidence_id: "kl_abc".to_owned(),
        kind: EvidenceKind::KeyLevel,
        label: "Prev Day High".to_owned(),
        detail: "tested twice".to_owned(),
        price_low: Some(Decimal::from_str("64000.0").unwrap()),
        price_high: Some(Decimal::from_str("64100.0").unwrap()),
    }
}

fn sample_setup_insight() -> SetupInsight {
    SetupInsight {
        direction: SetupDirection::Long,
        summary: "Breakout above range".to_owned(),
        entry_low: Decimal::from_str("64500.0").unwrap(),
        entry_high: Decimal::from_str("64600.0").unwrap(),
        invalidation_stop: Decimal::from_str("64000.0").unwrap(),
        take_profit: vec![Decimal::from_str("66000.0").unwrap()],
        max_position: Some(Decimal::from_str("0.5").unwrap()),
        valid_until: Some(1_700_003_600_000),
        evidence_refs: vec![sample_evidence_ref()],
    }
}

fn sample_abstain() -> Abstain {
    Abstain {
        reason_code: AbstainReason::EvidenceConflict,
        message: "Conflicting HTF signals".to_owned(),
        evidence_refs: vec![sample_evidence_ref()],
    }
}

fn sample_draft_content() -> DraftContent {
    DraftContent {
        sub_account_id: "sa-breakout".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        consultation_id: Some("cons_1".to_owned()),
        direction: Direction::Long,
        entry_low: Decimal::from_str("64500.0").unwrap(),
        entry_high: Decimal::from_str("64600.0").unwrap(),
        invalidation_stop: Decimal::from_str("64000.0").unwrap(),
        take_profit: vec![Decimal::from_str("66000.0").unwrap()],
        valid_until: Some(1_700_003_600_000),
        round_id: None,
        inherited_barriers: None,
    }
}

#[test]
fn zero_io_invariant_holds_in_talaria_contracts() {
    let manifest_dir = Path::new(env!("CARGO_MANIFEST_DIR"));
    let cargo_toml = fs::read_to_string(manifest_dir.join("Cargo.toml")).unwrap();
    let deps_section = cargo_toml
        .split("[dev-dependencies]")
        .next()
        .expect("Cargo.toml dependencies section");
    for forbidden_dep in ["tokio", "reqwest", "rusqlite"] {
        assert!(
            !deps_section.contains(forbidden_dep),
            "talaria-contracts runtime Cargo.toml must not depend on {forbidden_dep}"
        );
    }

    let mut stack = vec![manifest_dir.join("src")];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(&dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().and_then(|e| e.to_str()) == Some("rs") {
                let text = fs::read_to_string(&path).unwrap();
                for forbidden in ["std::fs", "std::net", "tokio", "reqwest", "rusqlite"] {
                    assert!(
                        !text.contains(forbidden),
                        "{} must not contain `{forbidden}`",
                        path.display()
                    );
                }
            }
        }
    }
}

#[test]
fn instrument_spec_sha256_matches_python_btc_usdt_perp() {
    let btc = sample_instrument_spec();
    assert_eq!(btc.instrument_spec_sha256(), "61aeb14515aa");

    let eth = InstrumentSpec {
        instrument_id: "ETH-USDT-PERP".to_owned(),
        venue: "bybit".to_owned(),
        symbol: "ETHUSDT".to_owned(),
        asset_class: "crypto_perp".to_owned(),
        tick_size: Decimal::from_str("0.01").unwrap(),
        qty_step: Decimal::from_str("0.01").unwrap(),
        min_qty: Decimal::from_str("0.01").unwrap(),
        price_precision: 2,
        qty_precision: 2,
    };
    assert_eq!(eth.instrument_spec_sha256().len(), 12);

    // Reject unknown fields.
    let bad = json!({
        "instrument_id": "BTC-USDT-PERP",
        "venue": "bybit",
        "symbol": "BTCUSDT",
        "asset_class": "crypto_perp",
        "tick_size": "0.1",
        "qty_step": "0.001",
        "min_qty": "0.001",
        "price_precision": 1,
        "qty_precision": 3,
        "extra_field": true
    });
    assert!(serde_json::from_value::<InstrumentSpec>(bad).is_err());
}

#[test]
fn decimal_str_parsing_and_rejection_rules() {
    assert_eq!(parse_decimal_str("0").unwrap(), Decimal::ZERO);
    assert_eq!(
        parse_decimal_str("0.000001").unwrap(),
        Decimal::from_str("0.000001").unwrap()
    );
    assert_eq!(
        format_decimal_str(Decimal::from_str("0.000001").unwrap()),
        "0.000001"
    );
    assert_eq!(
        format_normalized_decimal_str(Decimal::from_str("101.10").unwrap()),
        "101.1"
    );
    assert_eq!(
        format_normalized_decimal_str(Decimal::from_str("0.00").unwrap()),
        "0"
    );

    for invalid in ["", "-", "+1.5", "01", "01.5", ".5", "1.", "1e-6", "1E2", "nan"] {
        assert!(
            parse_decimal_str(invalid).is_err(),
            "expected `{invalid}` to be rejected"
        );
    }

    // JSON numbers must be rejected for DecimalStr fields.
    let numeric_bar = json!({
        "timestamp": 1_700_000_000_000_i64,
        "open": 101.1,
        "high": "102.2",
        "low": "100.0",
        "close": "101.5",
        "volume": "1.0",
        "turnover": "101.5",
        "status": "NORMAL",
        "ts_available": 1_700_000_060_000_i64,
        "precision": "SECOND_VERIFIED"
    });
    assert!(serde_json::from_value::<CompactBar>(numeric_bar).is_err());
}

#[test]
fn core_domain_invariants_and_traits() {
    // TemporalStamp ordering
    assert!(
        TemporalStamp::new(100, None, 99, PrecisionTier::SecondVerified, 100, 100).is_err()
    );
    assert!(
        TemporalStamp::new(100, None, 100, PrecisionTier::SecondVerified, 101, 100).is_err()
    );
    let stamp =
        TemporalStamp::new(100, None, 100, PrecisionTier::SessionBound, 101, 102).unwrap();
    assert_eq!(stamp.ts_published, None);

    // CompactBar FEED_GAP rules
    let gap = CompactBar {
        timestamp: 1_700_000_000_000,
        open: None,
        high: None,
        low: None,
        close: None,
        volume: None,
        turnover: None,
        status: BarStatus::FeedGap,
        ts_available: 1_700_000_060_000,
        precision: PrecisionTier::SecondVerified,
    };
    assert!(gap.validate().is_ok());
    let mut bad_gap = gap.clone();
    bad_gap.open = Some(Decimal::ONE);
    assert!(bad_gap.validate().is_err());

    // RawBar conversion & flat bar classification
    let raw_bar = RawBar {
        symbol: "BTCUSDT".to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: 1_700_000_000_000,
        open: Decimal::from_str("100.0").unwrap(),
        high: Decimal::from_str("100.0").unwrap(),
        low: Decimal::from_str("100.0").unwrap(),
        close: Decimal::from_str("100.0").unwrap(),
        volume: Decimal::ZERO,
        turnover: Decimal::ZERO,
        ts_available: 1_700_000_060_000,
        ts_ingested: 1_700_000_060_010,
        ts_committed: 1_700_000_060_020,
        precision: PrecisionTier::SecondVerified,
        revision: 0,
        confirmed: true,
        history_only: false,
        synthetic: false,
    };
    let status = classify_flat_bar(
        raw_bar.open,
        raw_bar.high,
        raw_bar.low,
        raw_bar.close,
        raw_bar.volume,
    );
    assert_eq!(status, BarStatus::NoTrades);
    assert!(raw_bar.to_compact(status).is_ok());
    assert!(raw_bar.to_compact(BarStatus::FeedGap).is_err());

    // TraitBundle with all 6 traits
    let bundle = TraitBundle {
        feed: FeedCapabilityTrait {
            realtime: true,
            polling: true,
            history: true,
        },
        margin: MarginTrait {
            margin_asset: "USDT".to_owned(),
            max_leverage: Decimal::from(100),
            contract_multiplier: Decimal::ONE,
        },
        carry: CarryTrait {
            funding_interval_hours: Some(8),
        },
        session: SessionTrait {
            session_kind: "24x7".to_owned(),
            timezone: Some("UTC".to_owned()),
            day_start: "00:00".to_owned(),
            sessions: vec![TradingSession {
                name: "asia".to_owned(),
                start: "00:00".to_owned(),
                end: "08:00".to_owned(),
                timezone: None,
            }],
        },
        order_constraint: OrderConstraintTrait {
            post_only: true,
            max_market_qty: None,
        },
        derivatives: Some(DerivativeCapabilityTrait {
            kinds: vec![
                DerivativeKind::MarkPrice,
                DerivativeKind::FundingRate,
                DerivativeKind::OpenInterest,
            ],
            history: true,
            realtime: true,
        }),
    };
    let bundle_json = serde_json::to_string(&bundle).unwrap();
    let decoded_bundle: TraitBundle = serde_json::from_str(&bundle_json).unwrap();
    assert_eq!(decoded_bundle, bundle);

    // OrderBookEvent crossed book rejection
    let crossed = json!({
        "instrument_id": "BTC-USDT-PERP",
        "bid_price": "101.60",
        "bid_size": "1.0",
        "ask_price": "101.50",
        "ask_size": "1.0",
        "ts_event": 1_700_000_000_000_i64,
        "ts_ingested": 1_700_000_000_010_i64
    });
    assert!(serde_json::from_value::<OrderBookEvent>(crossed).is_err());

    // FeedHealthEvent stale_since invariant
    let bad_stale = json!({
        "instrument_id": "BTC-USDT-PERP",
        "timeframe": "1m",
        "status": "STALE",
        "ts": 1_700_000_072_000_i64,
        "last_event_ts": null,
        "stale_since": null,
        "reconnect_count": 1
    });
    assert!(serde_json::from_value::<FeedHealthEvent>(bad_stale).is_err());

    // Trade & Quote round-trip
    let trade = Trade {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        ts_event: 1_700_000_000_123,
        ts_available: 1_700_000_000_123,
        ts_ingested: 1_700_000_000_130,
        ts_committed: 1_700_000_000_140,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        trade_id: Uuid::new_v4(),
        side: TradeSide::Buy,
        price: Decimal::from_str("65000.1").unwrap(),
        size: Decimal::from_str("0.015").unwrap(),
        availability: TradeAvailability::Measured,
        archive_day: None,
        archive_index: None,
        sequence: Some(42),
        packet_id: None,
        packet_index: None,
    };
    let trade_rt: Trade =
        serde_json::from_str(&serde_json::to_string(&trade).unwrap()).unwrap();
    assert_eq!(trade_rt, trade);

    let quote = Quote {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        ts_event: 1_700_000_000_200,
        ts_available: 1_700_000_000_200,
        ts_ingested: 1_700_000_000_210,
        ts_committed: 1_700_000_000_220,
        revision: 0,
        precision: PrecisionTier::SecondVerified,
        order_uncertain: false,
        bid_price: Decimal::from_str("65000.0").unwrap(),
        bid_size: Decimal::from_str("1.5").unwrap(),
        ask_price: Decimal::from_str("65000.1").unwrap(),
        ask_size: Decimal::from_str("2.0").unwrap(),
        connection_id: Some(Uuid::new_v4()),
        update_id: Some(7),
    };
    let quote_rt: Quote =
        serde_json::from_str(&serde_json::to_string(&quote).unwrap()).unwrap();
    assert_eq!(quote_rt, quote);
}

#[test]
fn all_115_json_schemas_roundtrip_without_drift() {
    let mut covered = BTreeSet::new();

    let stamp =
        TemporalStamp::new(1_700_000_000_000, Some(1_700_000_000_100), 1_700_000_000_100, PrecisionTier::SecondVerified, 1_700_000_000_150, 1_700_000_000_200).unwrap();
    assert_schema_roundtrip("temporal_stamp", &stamp, &mut covered);

    let spec = sample_instrument_spec();
    assert_schema_roundtrip("instrument_spec", &spec, &mut covered);

    let compact_bar = sample_compact_bar();
    assert_schema_roundtrip("compact_bar", &compact_bar, &mut covered);

    let range_bar = RangeBar {
        ts_event: 1_700_000_000_000,
        open: Decimal::from_str("101.1").unwrap(),
        high: Decimal::from_str("102.2").unwrap(),
        low: Decimal::from_str("100").unwrap(),
        close: Decimal::from_str("101.5").unwrap(),
        volume: Decimal::from_str("12.345").unwrap(),
        turnover: Decimal::from_str("1250.75").unwrap(),
        ts_available: 1_700_000_060_000,
        precision: PrecisionTier::SecondVerified,
        confirmed: true,
        revision: 0,
    };
    assert_schema_roundtrip("range_bar", &range_bar, &mut covered);

    let bars_query = BarsQuery {
        instrument: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        start: 1_700_000_000_000,
        end: Some(1_700_000_600_000),
        as_of: Some(1_700_000_600_000),
        include_open: false,
        tail_only: false,
        resync_from: None,
        available_since: None,
    };
    assert_schema_roundtrip("bars_query", &bars_query, &mut covered);

    let bars_resp = BarsResponse {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        generation: 1,
        bars: vec![range_bar.clone()],
    };
    assert_schema_roundtrip("bars_response", &bars_resp, &mut covered);

    let kline_event = KlineEvent {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        bar: compact_bar.clone(),
        confirmed: false,
        revision: 2,
    };
    assert_schema_roundtrip("kline_event", &kline_event, &mut covered);

    let order_book = OrderBookEvent {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        bid_price: Decimal::from_str("101.40").unwrap(),
        bid_size: Decimal::from_str("0.750").unwrap(),
        ask_price: Decimal::from_str("101.50").unwrap(),
        ask_size: Decimal::from_str("1.250").unwrap(),
        ts_event: 1_700_000_060_500,
        ts_ingested: 1_700_000_060_520,
    };
    assert_schema_roundtrip("order_book_event", &order_book, &mut covered);

    let deriv_tick = DerivativeTickEvent {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        mark_price: Some(Decimal::from_str("65000.5").unwrap()),
        funding_rate: Some(Decimal::from_str("0.0001").unwrap()),
        next_funding_ts: Some(1_700_016_000_000),
        open_interest: Some(Decimal::from_str("12345.678").unwrap()),
        ts_event: 1_700_000_060_000,
        ts_ingested: 1_700_000_060_010,
    };
    assert_schema_roundtrip("derivative_tick_event", &deriv_tick, &mut covered);

    let deriv_point = DerivativePoint {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        value: Decimal::from_str("0.0001").unwrap(),
        ts_event: 1_700_000_000_000,
        ts_published: Some(1_700_000_000_000),
        ts_available: 1_700_000_000_010,
        precision: PrecisionTier::SecondVerified,
        ts_ingested: 1_700_000_000_020,
        ts_committed: 1_700_000_000_030,
        revision: 0,
        settled: Some(true),
    };
    assert_schema_roundtrip("derivative_point", &deriv_point, &mut covered);

    let deriv_series = DerivativeSeries {
        kind: DerivativeKind::FundingRate,
        timeframe: None,
        points: vec![deriv_point.clone()],
    };
    assert_schema_roundtrip("derivative_series", &deriv_series, &mut covered);

    let deriv_summary = DerivativeSummary {
        mark_price: Some(Decimal::from_str("65000.5").unwrap()),
        mark_price_ts: Some(1_700_000_060_000),
        funding_rate: Some(Decimal::from_str("0.0001").unwrap()),
        funding_settled_ts: Some(1_700_000_000_000),
        next_funding_ts: Some(1_700_016_000_000),
        open_interest: Some(Decimal::from_str("12345.678").unwrap()),
        open_interest_ts: Some(1_700_000_060_000),
        oi_history_available: true,
    };
    assert_schema_roundtrip("derivative_summary", &deriv_summary, &mut covered);

    let deriv_bundle = DerivativeBundle {
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        cutoff_ts: 1_700_000_060_000,
        series: vec![deriv_series],
        summary: deriv_summary,
    };
    assert_schema_roundtrip("derivative_bundle", &deriv_bundle, &mut covered);

    let health_event = FeedHealthEvent {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        status: FeedHealthStatus::Live,
        ts: 1_700_000_080_000,
        last_event_ts: Some(1_700_000_079_900),
        stale_since: None,
        reconnect_count: 0,
    };
    assert_schema_roundtrip("feed_health_event", &health_event, &mut covered);

    let health_report = FeedHealthReport {
        feed_mode: "replay".to_owned(),
        status: FeedHealthStatus::Live,
        channels: vec![health_event],
    };
    assert_schema_roundtrip("feed_health_report", &health_report, &mut covered);

    let snap_req = SnapshotRequest {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframes: vec!["1m".to_owned()],
        cutoff_ts: Some(1_700_000_060_000),
        mode: SnapshotMode::HistoricalPit,
    };
    assert_schema_roundtrip("snapshot_request", &snap_req, &mut covered);

    let snap_meta = SnapshotMeta {
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframes: vec!["1m".to_owned()],
        cutoff_ts: 1_700_000_060_000,
        created_ts: 1_700_000_060_100,
        mode: SnapshotMode::HistoricalPit,
        bar_counts: BTreeMap::from([("1m".to_owned(), 100)]),
    };
    assert_schema_roundtrip("snapshot_meta", &snap_meta, &mut covered);

    let chart_page = ChartPage {
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        bars: vec![compact_bar.clone()],
        has_more_forward: false,
        has_more_backward: true,
    };
    assert_schema_roundtrip("chart_page", &chart_page, &mut covered);

    let err_resp = ErrorResponse {
        code: error_codes::NOT_FOUND.to_owned(),
        message: "not found".to_owned(),
    };
    assert_schema_roundtrip("error_response", &err_resp, &mut covered);

    let ind_params = IndicatorParams::defaults_for(IndicatorName::Ema);
    assert_schema_roundtrip("indicator_params", &ind_params, &mut covered);

    let ind_summary = IndicatorSummary {
        latest: BTreeMap::from([("value".to_owned(), Some(Decimal::from_str("101.2").unwrap()))]),
        warmup_bars: 0,
        valid_points: 1,
    };
    assert_schema_roundtrip("indicator_summary", &ind_summary, &mut covered);

    let ind_point = IndicatorPoint {
        timestamp: 1_700_000_000_000,
        values: BTreeMap::from([("value".to_owned(), Some(Decimal::from_str("101.2").unwrap()))]),
    };
    assert_schema_roundtrip("indicator_point", &ind_point, &mut covered);

    let ind_series = IndicatorSeries {
        name: IndicatorName::Ema,
        pane: IndicatorPane::Main,
        params: ind_params.clone(),
        points: vec![ind_point.clone()],
        summary: ind_summary,
    };
    assert_schema_roundtrip("indicator_series", &ind_series, &mut covered);

    let ind_req = IndicatorRequest {
        snapshot_id: "snap_1".to_owned(),
        timeframe: "1m".to_owned(),
        names: vec!["EMA".to_owned()],
        params: Some(BTreeMap::from([("EMA".to_owned(), ind_params.clone())])),
        limit: Some(100),
    };
    assert_schema_roundtrip("indicator_request", &ind_req, &mut covered);

    let op_req = OperatorRequest {
        snapshot_id: "snap_1".to_owned(),
        timeframe: "1m".to_owned(),
        operator: IndicatorName::Ema,
        params: Some(ind_params.clone()),
        limit: Some(100),
    };
    assert_schema_roundtrip("operator_request", &op_req, &mut covered);

    let ind_bundle = IndicatorBundle {
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        series: vec![ind_series],
    };
    assert_schema_roundtrip("indicator_bundle", &ind_bundle, &mut covered);

    let ind_defaults = IndicatorDefaultsResponse {
        defaults: BTreeMap::from([(IndicatorName::Ema, ind_params)]),
    };
    assert_schema_roundtrip("indicator_defaults_response", &ind_defaults, &mut covered);

    let pt = sample_drawing_point();
    assert_schema_roundtrip("drawing_point", &pt, &mut covered);

    let style = DrawingStyle::default();
    assert_schema_roundtrip("drawing_style", &style, &mut covered);

    let drw_create = DrawingCreate {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        kind: DrawingKind::HorizontalLine,
        visible_timeframes: vec!["1m".to_owned()],
        points: vec![pt.clone()],
        text: "S1".to_owned(),
        style: style.clone(),
    };
    assert_schema_roundtrip("drawing_create", &drw_create, &mut covered);

    let drw_patch = DrawingPatch {
        visible_timeframes: None,
        points: Some(vec![pt]),
        text: Some("S1 updated".to_owned()),
        style: None,
    };
    assert_schema_roundtrip("drawing_patch", &drw_patch, &mut covered);

    let drw_rec = sample_drawing_record();
    assert_schema_roundtrip("drawing_record", &drw_rec, &mut covered);

    let drw_list = DrawingListResponse {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        drawings: vec![drw_rec.clone()],
    };
    assert_schema_roundtrip("drawing_list_response", &drw_list, &mut covered);

    let drw_versions = DrawingVersionList {
        drawing_id: "drw_1".to_owned(),
        versions: vec![drw_rec.clone()],
    };
    assert_schema_roundtrip("drawing_version_list", &drw_versions, &mut covered);

    let drw_clear = DrawingClearResponse {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        cleared: 2,
    };
    assert_schema_roundtrip("drawing_clear_response", &drw_clear, &mut covered);

    let view_win = ChartViewWindow {
        from_ts: 1_700_000_000_000,
        to_ts: 1_700_000_600_000,
        price_min: Decimal::from_str("64000").unwrap(),
        price_max: Decimal::from_str("66000").unwrap(),
    };
    assert_schema_roundtrip("chart_view_window", &view_win, &mut covered);

    let chart_view = ChartView {
        instrument: spec,
        timeframe: "1m".to_owned(),
        window: view_win,
        drawings: vec![drw_rec],
    };
    assert_schema_roundtrip("chart_view", &chart_view, &mut covered);

    let kl = KeyLevel {
        level_id: "kl_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        kind: KeyLevelKind::SwingHigh,
        category: KeyLevelCategory::Structure,
        evidence_class: EvidenceClass::Observed,
        timeframe: "1m".to_owned(),
        price: Some(Decimal::from_str("65500.0").unwrap()),
        price_low: None,
        price_high: None,
        strength: 0.85,
        status: KeyLevelStatus::Active,
        ts_available: 1_700_000_120_000,
        formed_ts: 1_700_000_000_000,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters: BTreeMap::from([("swing_span".to_owned(), "2".to_owned())]),
        touch_count: 2,
        last_touch_ts: Some(1_700_000_060_000),
        volume_weight: Decimal::from_str("120.5").unwrap(),
    };
    assert_schema_roundtrip("key_level", &kl, &mut covered);

    let kl_set = KeyLevelSet {
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        cutoff_ts: 1_700_000_120_000,
        as_of_ts: 1_700_000_120_000,
        method_version: KEY_LEVEL_METHOD_VERSION.to_owned(),
        parameters: BTreeMap::new(),
        notes: vec![],
        levels: vec![kl],
    };
    assert_schema_roundtrip("key_level_set", &kl_set, &mut covered);

    assert_schema_roundtrip(
        "key_level_ignore_request",
        &KeyLevelIgnoreRequest {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            level_id: "kl_1".to_owned(),
            ignored: true,
        },
        &mut covered,
    );

    let kl_ign_rec = KeyLevelIgnoreRecord {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        level_id: "kl_1".to_owned(),
        ignored: true,
        updated_ts: 1_700_000_200_000,
    };
    assert_schema_roundtrip("key_level_ignore_record", &kl_ign_rec, &mut covered);
    assert_schema_roundtrip(
        "key_level_ignore_list",
        &KeyLevelIgnoreList {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            entries: vec![kl_ign_rec],
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "key_level_adoption_request",
        &KeyLevelAdoptionRequest {
            snapshot_id: "snap_1".to_owned(),
            timeframe: "1m".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "key_level_adoption_record",
        &KeyLevelAdoptionRecord {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            level_id: "kl_1".to_owned(),
            snapshot_id: "snap_1".to_owned(),
            drawing_id: "drw_1".to_owned(),
            adopted_ts: 1_700_000_200_000,
            timeframe: "1m".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "key_level_feedback_summary",
        &KeyLevelFeedbackSummary {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            adopted_level_ids: vec!["kl_1".to_owned()],
            ignored_level_ids: vec![],
        },
        &mut covered,
    );

    assert_schema_roundtrip(
        "watchlist_group_create",
        &WatchlistGroupCreate {
            name: "Core Perps".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "watchlist_group_update",
        &WatchlistGroupUpdate {
            name: "Renamed".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "watchlist_item_create",
        &WatchlistItemCreate {
            group_id: "grp_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "watchlist_order",
        &WatchlistOrder {
            ids: vec!["grp_1".to_owned()],
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "watchlist_item_order",
        &WatchlistItemOrder {
            group_id: "grp_1".to_owned(),
            ids: vec!["BTC-USDT-PERP".to_owned()],
        },
        &mut covered,
    );

    let wl_item = WatchlistItem {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        position: 0,
        latest_price: Some(Decimal::from_str("65000.1").unwrap()),
        change_pct: Some(Decimal::from_str("1.25").unwrap()),
        quote_ts: Some(1_700_000_060_000),
        quote_source: QuoteSource::Supervisor,
        realtime: true,
        feed_status: Some(FeedHealthStatus::Live),
        stale: false,
        degraded: false,
        degradation_reason: None,
    };
    assert_schema_roundtrip("watchlist_item", &wl_item, &mut covered);

    let wl_group = WatchlistGroup {
        group_id: "grp_1".to_owned(),
        name: "Core".to_owned(),
        position: 0,
        kind: WatchlistGroupKind::Manual,
        is_system: false,
        items: vec![wl_item],
    };
    assert_schema_roundtrip("watchlist_group", &wl_group, &mut covered);
    assert_schema_roundtrip(
        "watchlist",
        &Watchlist {
            groups: vec![wl_group],
            realtime_quota: 8,
            realtime_used: 1,
            generated_ts: 1_700_000_060_000,
        },
        &mut covered,
    );

    let rule = sample_rule();
    assert_schema_roundtrip("rule", &rule, &mut covered);
    assert_schema_roundtrip(
        "rule_preview_request",
        &RulePreviewRequest {
            timeframe: "1m".to_owned(),
            rule: rule.clone(),
            limit: 300,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "rule_preview_response",
        &RulePreviewResponse {
            rule: rule.clone(),
            unavailable_current_levels: vec![],
            history_incomplete: false,
            condition_true: vec![RulePreviewSignal {
                timestamp: 1_700_000_000_000,
                observed_at: 1_700_000_060_000,
                price: Some(Decimal::from_str("101.5").unwrap()),
            }],
            would_trigger: vec![],
            indicators: vec![RulePreviewIndicator {
                operand: RuleOperand::Indicator {
                    name: RuleIndicatorName::Ema,
                    version: RuleVersion::V1,
                    window: RuleIndicatorWindow::Alert,
                    snapshot_id: None,
                    channel: Some("value".to_owned()),
                    params: RuleIndicatorParams {
                        period: Some(9),
                        anchor: None,
                        std_dev: None,
                    },
                },
                pane: RulePane::Main,
                points: vec![ind_point],
            }],
        },
        &mut covered,
    );

    assert_schema_roundtrip(
        "alert_create",
        &AlertCreate {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            timeframe: "1m".to_owned(),
            condition: rule.clone(),
            note: "EMA cross".to_owned(),
            draft: false,
            repeat: true,
            cooldown_ms: 60_000,
            expires_at: None,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "alert_patch",
        &AlertPatch {
            condition: None,
            note: Some("Updated".to_owned()),
            repeat: None,
            cooldown_ms: None,
            expires_at: None,
            enabled: Some(true),
        },
        &mut covered,
    );
    let alert_rec = AlertRecord {
        alert_id: "alt_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        condition: rule.clone(),
        status: AlertStatus::Armed,
        repeat: true,
        cooldown_ms: 60_000,
        expires_at: None,
        note: "EMA cross".to_owned(),
        created_ts: 1_700_000_000_000,
        updated_ts: 1_700_000_000_000,
        last_triggered_ts: None,
        next_arm_ts: None,
        trigger_count: 0,
    };
    assert_schema_roundtrip("alert_record", &alert_rec, &mut covered);
    assert_schema_roundtrip(
        "alert_list_response",
        &AlertListResponse {
            alerts: vec![alert_rec],
        },
        &mut covered,
    );
    let trig_rec = AlertTriggerRecord {
        trigger_id: "trg_1".to_owned(),
        alert_id: "alt_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        condition_kind: AlertTriggerKind::Rule,
        triggered_ts: 1_700_000_060_000,
        snapshot_id: "snap_1".to_owned(),
        observed: Some(Decimal::from_str("101.5").unwrap()),
        threshold: Some(Decimal::from_str("100.5").unwrap()),
        message: "triggered".to_owned(),
        note: "".to_owned(),
    };
    assert_schema_roundtrip("alert_trigger_record", &trig_rec, &mut covered);
    assert_schema_roundtrip(
        "alert_trigger_list_response",
        &AlertTriggerListResponse {
            triggers: vec![trig_rec],
        },
        &mut covered,
    );

    let scan_ev = ScannerEvidence {
        metric: "atr_ratio".to_owned(),
        value: Decimal::from_str("2.4").unwrap(),
        threshold: Some(Decimal::from_str("2.0").unwrap()),
        note: Some("volatility spike".to_owned()),
    };
    assert_schema_roundtrip("scanner_evidence", &scan_ev, &mut covered);
    let scan_trig = ScannerTrigger {
        trigger_id: "st_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        kind: ScannerConditionKind::VolatilityExpansion,
        severity: ScannerSeverity::Warning,
        title: "ATR spike".to_owned(),
        summary: "ATR 2.4x".to_owned(),
        evidence: vec![scan_ev],
        tags: vec!["vol".to_owned()],
        created_ts: 1_700_000_060_000,
        snapshot_id: "snap_1".to_owned(),
        route: ScannerTriggerRoute::Local,
    };
    assert_schema_roundtrip("scanner_trigger", &scan_trig, &mut covered);
    assert_schema_roundtrip(
        "scanner_trigger_list_response",
        &ScannerTriggerListResponse {
            triggers: vec![scan_trig.clone()],
            total: 1,
            generated_ts: 1_700_000_060_000,
        },
        &mut covered,
    );
    let focus_item = ScannerFocusItem {
        instrument_id: "BTC-USDT-PERP".to_owned(),
        last_trigger_ts: 1_700_000_060_000,
        trigger_count: 1,
        severity: ScannerSeverity::Warning,
        kinds: vec![ScannerConditionKind::VolatilityExpansion],
        last_title: Some("ATR spike".to_owned()),
        last_timeframe: Some("1m".to_owned()),
        last_snapshot_id: Some("snap_1".to_owned()),
    };
    assert_schema_roundtrip("scanner_focus_item", &focus_item, &mut covered);
    assert_schema_roundtrip(
        "scanner_focus_response",
        &ScannerFocusResponse {
            group_id: "scanner_focus".to_owned(),
            name: "Scanner Focus".to_owned(),
            items: vec![focus_item],
            instrument_ids: vec!["BTC-USDT-PERP".to_owned()],
            generated_ts: 1_700_000_060_000,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "scanner_scan_request",
        &ScannerScanRequest {
            instrument_ids: Some(vec!["BTC-USDT-PERP".to_owned()]),
            rule: Some(rule),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "scanner_scan_response",
        &ScannerScanResponse {
            triggers: vec![scan_trig],
            scanned_instruments: vec!["BTC-USDT-PERP".to_owned()],
            generated_ts: 1_700_000_060_000,
        },
        &mut covered,
    );

    let b_ref = BriefingLevelRef {
        label: "PDH".to_owned(),
        price: Some(Decimal::from_str("65500").unwrap()),
        note: "resistance".to_owned(),
    };
    assert_schema_roundtrip("briefing_level_ref", &b_ref, &mut covered);
    let b_content = BriefingContent {
        trigger_reason: "Vol expansion".to_owned(),
        key_levels: vec![b_ref],
        risks: vec!["Whipsaw".to_owned()],
    };
    assert_schema_roundtrip("briefing_content", &b_content, &mut covered);
    let ev_ref = sample_evidence_ref();
    assert_schema_roundtrip("evidence_ref", &ev_ref, &mut covered);

    let briefing = ScannerBriefing {
        id: "brf_1".to_owned(),
        trigger_id: "st_1".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        status: BriefingStatus::Ready,
        summary: Some(b_content.clone()),
        evidence_refs: vec![ev_ref.clone()],
        abstain_reason: None,
        created_ts: 1_700_000_060_000,
        read_ts: None,
    };
    assert_schema_roundtrip("scanner_briefing", &briefing, &mut covered);
    assert_schema_roundtrip(
        "briefing_list_response",
        &BriefingListResponse {
            briefings: vec![briefing],
            unread: 1,
            generated_ts: 1_700_000_060_000,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "briefing_consult_request",
        &BriefingConsultRequest {
            question: Some("Explain trigger".to_owned()),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "briefing_consult_response",
        &BriefingConsultResponse {
            briefing_id: "brf_1".to_owned(),
            snapshot_id: "snap_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
            timeframe: "1m".to_owned(),
            question: "Explain trigger".to_owned(),
        },
        &mut covered,
    );

    let usage_rep = UsageReport {
        dsh_session_id: "sess_1".to_owned(),
        dsh_turn_id: "turn_1".to_owned(),
        input_tokens: Some(1200),
        cached_input_tokens: Some(800),
        output_tokens: Some(300),
        source: "host".to_owned(),
    };
    assert_schema_roundtrip("usage_report", &usage_rep, &mut covered);
    assert_schema_roundtrip(
        "briefing_result_request",
        &BriefingResultRequest {
            text: None,
            content: Some(b_content),
            abstain: None,
            error: None,
            dsh_session_id: Some("sess_1".to_owned()),
            dsh_turn_id: Some("turn_1".to_owned()),
            usage: Some(usage_rep),
        },
        &mut covered,
    );

    let cw = CollabWindow {
        from_ts: 1_700_000_000_000,
        to_ts: 1_700_000_600_000,
        price_min: Decimal::from_str("64000").unwrap(),
        price_max: Decimal::from_str("66000").unwrap(),
    };
    assert_schema_roundtrip("collab_window", &cw, &mut covered);

    let drw_ref = DrawingRef {
        drawing_id: "drw_1".to_owned(),
        kind: DrawingKind::HorizontalLine,
        points: vec![sample_drawing_point()],
        text: "S1".to_owned(),
    };
    assert_schema_roundtrip("drawing_ref", &drw_ref, &mut covered);

    let cat_ev = CatalystEvent {
        event_id: "cat_1".to_owned(),
        title: "FOMC".to_owned(),
        ts: 1_700_001_000_000,
        kind: "CATALYST".to_owned(),
        source: "catalyst_calendar".to_owned(),
    };
    assert_schema_roundtrip("catalyst_event", &cat_ev, &mut covered);

    assert_schema_roundtrip(
        "consult_request",
        &ConsultRequest {
            snapshot_id: "snap_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
            timeframe: "1m".to_owned(),
            question: "Review support".to_owned(),
            visible: cw.clone(),
            selection: None,
            drawing_ids: vec!["drw_1".to_owned()],
        },
        &mut covered,
    );

    let pkg = ConsultationPackage {
        consultation_id: "cons_1".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        visible: cw,
        selection: None,
        drawings: vec![drw_ref],
        catalysts: vec![cat_ev],
        question: "Review support".to_owned(),
        snapshot_cutoff_ts: 1_700_000_600_000,
        evidence_catalog: vec![ev_ref],
        created_ts: 1_700_000_600_100,
    };
    assert_schema_roundtrip("consultation_package", &pkg, &mut covered);

    let insight = sample_setup_insight();
    assert_schema_roundtrip("setup_insight", &insight, &mut covered);
    let abstain = sample_abstain();
    assert_schema_roundtrip("abstain", &abstain, &mut covered);

    let cons_rec = ConsultationRecord {
        consultation_id: "cons_1".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        status: ConsultationStatus::Ready,
        package: pkg,
        insight: Some(insight.clone()),
        abstain: None,
        error: None,
        dsh_session_id: Some("sess_1".to_owned()),
        dsh_turn_id: Some("turn_1".to_owned()),
        created_ts: 1_700_000_600_100,
        updated_ts: 1_700_000_601_000,
        completed_ts: Some(1_700_000_601_000),
    };
    assert_schema_roundtrip("consultation_record", &cons_rec, &mut covered);
    assert_schema_roundtrip(
        "consult_response",
        &ConsultResponse {
            consultation_id: "cons_1".to_owned(),
            snapshot_id: "snap_1".to_owned(),
            status: ConsultationStatus::Pending,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "consultation_list_response",
        &ConsultationListResponse {
            consultations: vec![cons_rec],
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "host_result_request",
        &HostResultRequest {
            text: None,
            insight: Some(insight),
            abstain: None,
            error: None,
            dsh_session_id: Some("sess_1".to_owned()),
            dsh_turn_id: Some("turn_1".to_owned()),
        },
        &mut covered,
    );

    let usage_row = UsageRow {
        dsh_session_id: "sess_1".to_owned(),
        dsh_turn_id: "turn_1".to_owned(),
        input_tokens: Some(1200),
        cached_input_tokens: Some(800),
        output_tokens: Some(300),
        source: "host".to_owned(),
        reported_ts: 1_700_000_601_000,
    };
    assert_schema_roundtrip("usage_row", &usage_row, &mut covered);
    assert_schema_roundtrip(
        "usage_report_response",
        &UsageReportResponse {
            recorded: true,
            dsh_session_id: "sess_1".to_owned(),
            dsh_turn_id: "turn_1".to_owned(),
            duplicate: false,
        },
        &mut covered,
    );
    let usage_summary = UsageSummary {
        dsh_session_id: Some("sess_1".to_owned()),
        input_tokens: 1200,
        cached_input_tokens: 800,
        output_tokens: 300,
        turns: 1,
        sessions: 1,
        missing_reports: 0,
        updated_ts: 1_700_000_601_000,
    };
    assert_schema_roundtrip("usage_summary", &usage_summary, &mut covered);
    assert_schema_roundtrip(
        "usage_summary_response",
        &UsageSummaryResponse {
            summary: usage_summary.clone(),
            sessions: vec![usage_summary],
            recent: vec![usage_row],
        },
        &mut covered,
    );

    let ann_ev = AnnotationEvidence {
        level_id: Some("kl_1".to_owned()),
        kline_ts: None,
        note: "confirmed".to_owned(),
    };
    assert_schema_roundtrip("annotation_evidence", &ann_ev, &mut covered);
    assert_schema_roundtrip(
        "agent_annotation_create",
        &AgentAnnotationCreate {
            snapshot_id: "snap_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
            timeframe: "1m".to_owned(),
            session_id: "sess_1".to_owned(),
            round: 1,
            kind: AnnotationKind::Zone,
            label: "Demand".to_owned(),
            rationale: "Reclaimed".to_owned(),
            price_low: Decimal::from_str("64000").unwrap(),
            price_high: Decimal::from_str("64200").unwrap(),
            ts_anchor: 1_700_000_000_000,
            evidence_refs: vec![ann_ev.clone()],
        },
        &mut covered,
    );
    let ann_rec = AgentAnnotationRecord {
        annotation_id: "ann_1".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        timeframe: "1m".to_owned(),
        session_id: "sess_1".to_owned(),
        round: 1,
        kind: AnnotationKind::Zone,
        status: AnnotationStatus::Active,
        label: "Demand".to_owned(),
        rationale: "Reclaimed".to_owned(),
        price_low: Decimal::from_str("64000").unwrap(),
        price_high: Decimal::from_str("64200").unwrap(),
        ts_anchor: 1_700_000_000_000,
        evidence_refs: vec![ann_ev],
        created_ts: 1_700_000_060_000,
        updated_ts: 1_700_000_060_000,
        adopted_drawing_id: None,
        archived_reason: None,
    };
    assert_schema_roundtrip("agent_annotation_record", &ann_rec, &mut covered);
    assert_schema_roundtrip(
        "annotation_list_response",
        &AnnotationListResponse {
            annotations: vec![ann_rec.clone()],
        },
        &mut covered,
    );
    let ann_sess = AnnotationSessionSummary {
        session_id: "sess_1".to_owned(),
        instrument_ids: vec!["BTC-USDT-PERP".to_owned()],
        annotation_count: 1,
        latest_ts: 1_700_000_060_000,
    };
    assert_schema_roundtrip("annotation_session_summary", &ann_sess, &mut covered);
    assert_schema_roundtrip(
        "annotation_sessions_response",
        &AnnotationSessionsResponse {
            sessions: vec![ann_sess],
        },
        &mut covered,
    );
    let ann_adopt = AnnotationAdoptionRecord {
        annotation_id: "ann_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        snapshot_id: "snap_1".to_owned(),
        timeframe: "1m".to_owned(),
        session_id: "sess_1".to_owned(),
        round: 1,
        drawing_id: "drw_1".to_owned(),
        adopted_ts: 1_700_000_070_000,
    };
    assert_schema_roundtrip("annotation_adoption_record", &ann_adopt, &mut covered);
    assert_schema_roundtrip(
        "annotation_adopt_response",
        &AnnotationAdoptResponse {
            annotation: ann_rec,
            adoption: ann_adopt,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "annotation_clear_request",
        &AnnotationClearRequest {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            session_id: Some("sess_1".to_owned()),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "annotation_clear_response",
        &AnnotationClearResponse {
            instrument_id: "BTC-USDT-PERP".to_owned(),
            session_id: None,
            cleared: 1,
        },
        &mut covered,
    );

    let draft_content = sample_draft_content();
    assert_schema_roundtrip("draft_content", &draft_content, &mut covered);
    let hash = "a".repeat(64);
    assert_schema_roundtrip(
        "confirmed_draft",
        &ConfirmedDraft {
            version: 1,
            content_hash: hash.clone(),
            confirmed_by: "trader".to_owned(),
            draft_id: "drf_1".to_owned(),
            content: draft_content.clone(),
            basis_cutoff_ts: 1_700_000_060_000,
            created_ts: 1_700_000_060_100,
            confirmed_ts: 1_700_000_060_500,
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "draft_edit",
        &DraftEdit {
            version: 1,
            content: draft_content.clone(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "draft_evidence",
        &DraftEvidence {
            snapshot_id: "snap_1".to_owned(),
            instrument_id: "BTC-USDT-PERP".to_owned(),
            cutoff_ts: 1_700_000_060_000,
            bars: vec![compact_bar.clone()],
            current_bar: Some(compact_bar),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "draft_confirmation",
        &DraftConfirmation {
            version: 1,
            content_hash: hash.clone(),
            confirmed_by: "trader".to_owned(),
        },
        &mut covered,
    );
    assert_schema_roundtrip(
        "draft_delivery_request",
        &DraftDeliveryRequest {
            instrument_id: "BTC-USDT-PERP".to_owned(),
        },
        &mut covered,
    );
    let receipt = DraftReceipt {
        draft_id: "drf_1".to_owned(),
        version: 1,
        accepted: true,
        plan_id: Some("pln_1".to_owned()),
        reason: None,
        snapshot_id: Some("snap_1".to_owned()),
    };
    assert_schema_roundtrip("draft_receipt", &receipt, &mut covered);
    assert_schema_roundtrip(
        "draft_record",
        &DraftRecord {
            draft_id: "drf_1".to_owned(),
            version: 1,
            content_hash: hash,
            content: draft_content,
            status: DraftStatus::Confirmed,
            claimed: false,
            receipt: Some(receipt),
        },
        &mut covered,
    );

    let round = PaperRound {
        round_id: "rnd_1".to_owned(),
        sub_account_id: "sa-breakout".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        direction: Direction::Long,
        quantity: Decimal::from_str("0.1").unwrap(),
        stop: Decimal::from_str("64000").unwrap(),
        take_profit: vec![Decimal::from_str("66000").unwrap()],
        phase: Some("ACTIVE_PROTECTED".to_owned()),
        barriers: Some(ExitBarrierSpec {
            stop: Decimal::from_str("64000").unwrap(),
            take_profit: vec![Decimal::from_str("66000").unwrap()],
            max_holding_ms: 3_600_000,
            trailing_stop: Some(TrailingStopSpec {
                activation_pct: Decimal::from_str("0.01").unwrap(),
                distance_pct: Decimal::from_str("0.005").unwrap(),
            }),
        }),
    };
    assert_schema_roundtrip("paper_round", &round, &mut covered);
    let fill = PaperFill {
        event_id: "evt_1".to_owned(),
        round_id: "rnd_1".to_owned(),
        instrument_id: "BTC-USDT-PERP".to_owned(),
        kind: PaperFillKind::EntryFilled,
        ts_event: 1_700_000_060_000,
        quantity: Decimal::from_str("0.1").unwrap(),
        price: Decimal::from_str("64550").unwrap(),
    };
    assert_schema_roundtrip("paper_fill", &fill, &mut covered);
    assert_schema_roundtrip(
        "paper_account_view",
        &PaperAccountView {
            sub_account_id: "sa-breakout".to_owned(),
            positions: vec![round.clone()],
            rounds: vec![round],
            fills: vec![fill],
        },
        &mut covered,
    );

    // Ensure every single *.schema.json in contracts/schemas/ was validated!
    let mut disk_schemas = BTreeSet::new();
    for entry in fs::read_dir(schemas_dir()).unwrap() {
        let path = entry.unwrap().path();
        if path.is_file() && path.extension().and_then(|e| e.to_str()) == Some("json") {
            disk_schemas.insert(path.file_name().unwrap().to_string_lossy().into_owned());
        }
    }
    let missing: Vec<_> = disk_schemas.difference(&covered).cloned().collect();
    assert!(
        missing.is_empty(),
        "uncovered schema files in contracts/schemas/: {missing:?}"
    );
}
