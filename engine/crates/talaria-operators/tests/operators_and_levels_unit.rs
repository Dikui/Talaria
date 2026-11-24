use std::str::FromStr;

use rust_decimal::Decimal;
use talaria_contracts::{
    BarStatus, CompactBar, DerivativeKind, DerivativePoint, IndicatorName, KeyLevelCategory,
    KeyLevelKind, KeyLevelStatus, PrecisionTier, SessionProfile, TradingSession, VwapAnchor,
    VwapAnchorMode, format_decimal_str,
};
use talaria_operators::gda::GdaDecimal;
use talaria_operators::sha256::sha256_hex_prefix16;
use talaria_operators::{
    KeyLevelParams, OperatorSpec, compute_indicator_series, compute_key_level_set,
    compute_key_levels, compute_series, derivative_samples, legacy_alert_points,
};

const MINUTE_MS: i64 = 60_000;
const HOUR_MS: i64 = 60 * MINUTE_MS;
const DAY_MS: i64 = 86_400_000;
const BASE_TS: i64 = 1_700_000_000_000;

fn d(s: &str) -> Decimal {
    Decimal::from_str(s).unwrap()
}

fn make_bar(index: i64, open: &str, high: &str, low: &str, close: &str, volume: &str) -> CompactBar {
    let ts = BASE_TS + index * MINUTE_MS;
    let c = d(close);
    let v = d(volume);
    CompactBar {
        timestamp: ts,
        open: Some(d(open)),
        high: Some(d(high)),
        low: Some(d(low)),
        close: Some(c),
        volume: Some(v),
        turnover: Some(v * c),
        status: if v.is_zero() && d(open) == d(high) && d(high) == d(low) && d(low) == c {
            BarStatus::NoTrades
        } else {
            BarStatus::Normal
        },
        ts_available: ts + MINUTE_MS,
        precision: PrecisionTier::SecondVerified,
    }
}

fn gap_bar(index: i64) -> CompactBar {
    let ts = BASE_TS + index * MINUTE_MS;
    CompactBar {
        timestamp: ts,
        open: None,
        high: None,
        low: None,
        close: None,
        volume: None,
        turnover: None,
        status: BarStatus::FeedGap,
        ts_available: ts + MINUTE_MS,
        precision: PrecisionTier::SecondVerified,
    }
}

#[test]
fn gda_decimal_matches_python_prec28_round_half_even() {
    let two = GdaDecimal::from_i64(2);
    let seven = GdaDecimal::from_i64(7);
    let five = GdaDecimal::from_i64(5);
    let three = GdaDecimal::from_i64(3);
    let four = GdaDecimal::from_i64(4);
    let four_00 = GdaDecimal::from_decimal(d("4.00"));
    let four_000 = GdaDecimal::from_decimal(d("4.000"));
    let two_00 = GdaDecimal::from_decimal(d("2.00"));

    assert_eq!(
        format_decimal_str(two.div(&seven).unwrap().to_decimal().unwrap()),
        "0.2857142857142857142857142857"
    );
    assert_eq!(
        format_decimal_str(five.div(&three).unwrap().to_decimal().unwrap()),
        "1.666666666666666666666666667"
    );
    assert_eq!(
        format_decimal_str(four.div(&two).unwrap().to_decimal().unwrap()),
        "2"
    );
    assert_eq!(
        format_decimal_str(four_00.div(&two).unwrap().to_decimal().unwrap()),
        "2.00"
    );
    assert_eq!(
        format_decimal_str(four.div(&two_00).unwrap().to_decimal().unwrap()),
        "2"
    );
    assert_eq!(
        format_decimal_str(four.sqrt().unwrap().to_decimal().unwrap()),
        "2"
    );
    assert_eq!(
        format_decimal_str(four_00.sqrt().unwrap().to_decimal().unwrap()),
        "2.0"
    );
    assert_eq!(
        format_decimal_str(four_000.sqrt().unwrap().to_decimal().unwrap()),
        "2.00"
    );
    assert_eq!(
        format_decimal_str(two.sqrt().unwrap().to_decimal().unwrap()),
        "1.414213562373095048801688724"
    );
    assert_eq!(
        format_decimal_str(
            GdaDecimal::from_decimal(d("1.00"))
                .sub(&GdaDecimal::from_decimal(d("1.00")))
                .to_decimal()
                .unwrap()
        ),
        "0.00"
    );
}

#[test]
fn sha256_matches_standard_vector_and_python_level_id() {
    assert_eq!(
        sha256_hex_prefix16(b"BTC-USDT-PERP|SWING_HIGH|1m|1700000280000|120|"),
        "e5f53fb926c32a10"
    );
    assert_eq!(
        sha256_hex_prefix16(b"BTC-USDT-PERP|SWING_LOW|1m|1700000160000|100|"),
        "5101adf6bace872e"
    );
}

#[test]
fn ema_period_5000_handles_scale_gt_28_alpha_cleanly() {
    let spec = OperatorSpec::new(IndicatorName::Ema, Some(5000), None, None).unwrap();
    let bars = vec![
        make_bar(0, "100", "101", "99", "100", "1"),
        make_bar(1, "100", "105", "100", "105", "1"),
    ];
    let state = compute_series(&bars, &spec, None).unwrap();
    // alpha = 2 / 5001 = 0.0003999200159968006398720255949
    // ema[1] = alpha * 105 + (1 - alpha) * 100 = 100.0019996000799840031993601
    let val = state.points[1]["value"].unwrap();
    assert_eq!(format_decimal_str(val), "100.0019996000799840031993601");
}

#[test]
fn rsi_balanced_and_vwap_session_day_boundary() {
    let rsi_spec = OperatorSpec::new(IndicatorName::Rsi, Some(2), None, None).unwrap();
    let rsi_bars = vec![
        make_bar(0, "1", "1", "1", "1", "1"),
        make_bar(1, "2", "2", "2", "2", "1"),
        make_bar(2, "1", "1", "1", "1", "1"),
    ];
    let rsi_pts = compute_series(&rsi_bars, &rsi_spec, None).unwrap().points;
    assert_eq!(rsi_pts[0]["value"], None);
    assert_eq!(rsi_pts[1]["value"], None);
    assert_eq!(rsi_pts[2]["value"], Some(d("50")));

    // VWAP session crossing day boundary and rewarming after gap
    let day0 = (BASE_TS / DAY_MS) * DAY_MS;
    let vwap_spec =
        OperatorSpec::new(IndicatorName::Vwap, None, None, Some(VwapAnchorMode::Session)).unwrap();
    let mut b0 = make_bar(0, "100", "100", "100", "100", "1");
    b0.timestamp = day0 + MINUTE_MS;
    b0.ts_available = b0.timestamp + MINUTE_MS;
    let mut g1 = gap_bar(1);
    g1.timestamp = day0 + 2 * MINUTE_MS;
    g1.ts_available = g1.timestamp + MINUTE_MS;
    let mut b2 = make_bar(2, "110", "110", "110", "110", "1");
    b2.timestamp = day0 + 3 * MINUTE_MS;
    b2.ts_available = b2.timestamp + MINUTE_MS;
    let mut b3 = make_bar(3, "200", "200", "200", "200", "1");
    b3.timestamp = day0 + DAY_MS + MINUTE_MS;
    b3.ts_available = b3.timestamp + MINUTE_MS;

    let series = compute_indicator_series(&[b0, g1, b2, b3], &vwap_spec, None, None).unwrap();
    let vals: Vec<_> = series.points.iter().map(|p| p.values["value"]).collect();
    assert_eq!(vals, vec![None, None, None, Some(d("200"))]);
    assert_eq!(series.summary.warmup_bars, 3);
    assert_eq!(series.summary.valid_points, 1);
}

#[test]
fn legacy_alert_points_and_derivative_samples_alignment() {
    let ema_spec = OperatorSpec::new(IndicatorName::Ema, Some(3), None, None).unwrap();
    let bars: Vec<CompactBar> = (0..4)
        .map(|i| make_bar(i, &format!("{}", i + 1), &format!("{}", i + 1), &format!("{}", i + 1), &format!("{}", i + 1), "1"))
        .collect();
    let legacy = legacy_alert_points(&bars, &ema_spec).unwrap();
    assert_eq!(legacy[0]["value"], None);
    assert_eq!(legacy[1]["value"], None);
    assert_eq!(legacy[2]["value"], Some(d("2")));
    assert_eq!(legacy[3]["value"], Some(d("3")));

    // Late older derivative points do not replace newer events
    let points = |kind: DerivativeKind| {
        [(-1i64, 0i64, "2"), (-2, 1, "1"), (-1, MINUTE_MS + 1, "3")]
            .into_iter()
            .map(|(ev, av, val)| DerivativePoint {
                instrument_id: "test".to_owned(),
                kind,
                timeframe: None,
                value: d(val),
                ts_event: BASE_TS + ev,
                ts_published: None,
                ts_available: BASE_TS + av,
                precision: PrecisionTier::SecondVerified,
                ts_ingested: BASE_TS + av,
                ts_committed: BASE_TS + av,
                revision: 0,
                settled: None,
            })
            .collect::<Vec<_>>()
    };
    let samples = derivative_samples(
        &bars[..3],
        &points(DerivativeKind::FundingRate),
        &points(DerivativeKind::OpenInterest),
        &[],
        None,
    )
    .unwrap();
    assert_eq!(samples[0].funding_rate, Some(d("2")));
    assert_eq!(samples[1].funding_rate, Some(d("2")));
    assert_eq!(samples[2].funding_rate, Some(d("3")));
}

#[test]
fn session_levels_week_month_gap_round_and_ny_vwap() {
    // 2025-01-01 00:00:00 UTC = 1_735_689_600_000 ms
    let start = 1_735_689_600_000i64;
    let mut bars = Vec::new();
    for i in 0..30 {
        let ts = start + i * HOUR_MS;
        let high = if i == 5 { "110" } else { "105" };
        let low = if i == 14 { "95" } else { "99" };
        let close = if i < 24 { "104" } else { "106" };
        bars.push(CompactBar {
            timestamp: ts,
            open: Some(d("100")),
            high: Some(d(high)),
            low: Some(d(low)),
            close: Some(d(close)),
            volume: Some(d("10")),
            turnover: Some(d("1000")),
            status: BarStatus::Normal,
            ts_available: ts + HOUR_MS,
            precision: PrecisionTier::SessionBound,
        });
    }
    let cutoff = bars.last().unwrap().ts_available;
    let profile = SessionProfile {
        timezone: "UTC".to_owned(),
        day_start: "00:00".to_owned(),
        sessions: vec![TradingSession {
            name: "asia".to_owned(),
            start: "00:00".to_owned(),
            end: "08:00".to_owned(),
            timezone: None,
        }],
    };
    let set = compute_key_level_set(
        "snap-1",
        "BTC-USDT-PERP",
        "1h",
        &bars,
        cutoff,
        Some(&profile),
        Some(&KeyLevelParams::default()),
        &[],
    )
    .unwrap();

    let prev_high = set
        .levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::PrevDayHigh)
        .unwrap();
    let prev_low = set
        .levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::PrevDayLow)
        .unwrap();
    let prev_close = set
        .levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::PrevDayClose)
        .unwrap();
    assert_eq!(prev_high.price, Some(d("110")));
    assert_eq!(prev_low.price, Some(d("95")));
    assert_eq!(prev_close.price, Some(d("104")));
    assert_eq!(prev_high.ts_available, start + 24 * HOUR_MS);

    let sess_high = set
        .levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::SessionHigh)
        .unwrap();
    let sess_low = set
        .levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::SessionLow)
        .unwrap();
    assert_eq!(sess_high.price, Some(d("110")));
    assert_eq!(sess_low.price, Some(d("99")));
    assert_eq!(sess_high.ts_available, start + 8 * HOUR_MS);

    // Assert exact parity on all 23 emitted levels (IDs, prices, strengths, statuses, touches, weights)
    let expected_top5 = [
        (KeyLevelKind::PrevDayClose, "kl_6a41efe25a4688b0", "104", 0.88, KeyLevelStatus::Broken, 30u64, "1.000000"),
        (KeyLevelKind::RoundNumber, "kl_f99dc3551c7a1a84", "100", 0.879, KeyLevelStatus::Active, 30, "1.000000"),
        (KeyLevelKind::SessionLow, "kl_2ba5d25bc7ed0c48", "99", 0.86, KeyLevelStatus::Broken, 30, "1.000000"),
        (KeyLevelKind::MonthOpen, "kl_ae760b19db4c27a7", "100", 0.86, KeyLevelStatus::Broken, 30, "1.000000"),
        (KeyLevelKind::RoundNumber, "kl_ca5d62621678c2bc", "110", 0.586, KeyLevelStatus::Active, 1, "0.033333"),
    ];
    assert_eq!(set.levels.len(), 23);
    for (idx, (kind, id, price, strength, status, touches, vw)) in expected_top5.iter().enumerate() {
        let lvl = &set.levels[idx];
        assert_eq!(lvl.kind, *kind);
        assert_eq!(lvl.level_id, *id);
        assert_eq!(lvl.price.map(format_decimal_str).as_deref(), Some(*price));
        assert_eq!(lvl.strength, *strength);
        assert_eq!(lvl.status, *status);
        assert_eq!(lvl.touch_count, *touches);
        assert_eq!(format_decimal_str(lvl.volume_weight), *vw);
    }

    // Unfilled vs filled gap
    let gap_bars = vec![
        make_bar(0, "100", "100", "99", "99.5", "10"),
        make_bar(1, "101", "102", "101", "101.5", "10"),
        make_bar(2, "101.5", "102", "100.5", "101", "10"),
        make_bar(3, "101", "102", "101", "101.5", "10"),
        make_bar(4, "101.5", "102", "101", "101.5", "10"),
        make_bar(5, "103", "104", "103", "103.5", "10"),
        make_bar(6, "103.5", "104", "103", "103.5", "10"),
        make_bar(7, "104", "104.5", "102", "103", "10"),
    ];
    let gap_params = KeyLevelParams {
        gap_atr_min: Decimal::ZERO,
        ..KeyLevelParams::default()
    };
    let gap_levels = compute_key_levels(
        "BTC-USDT-PERP",
        "1m",
        &gap_bars,
        gap_bars[7].ts_available,
        None,
        Some(&gap_params),
        &[],
    )
    .unwrap();
    let gaps: Vec<_> = gap_levels
        .iter()
        .filter(|l| l.kind == KeyLevelKind::Gap)
        .collect();
    assert_eq!(gaps.len(), 1);
    assert_eq!(gaps[0].price_low, Some(d("100")));
    assert_eq!(gaps[0].price_high, Some(d("101")));
    assert_eq!(gaps[0].ts_available, gap_bars[1].ts_available);
    assert_eq!(gaps[0].status, KeyLevelStatus::Broken);
    assert_eq!(gaps[0].category, KeyLevelCategory::Gap);

    // Round numbers at 60000
    let round_bars: Vec<_> = (0..20)
        .map(|i| make_bar(i, "60000", "60100", "59900", "60000", "10"))
        .collect();
    let round_params = KeyLevelParams {
        round_levels_per_side: 2,
        ..KeyLevelParams::default()
    };
    let rounds = compute_key_levels(
        "BTC-USDT-PERP",
        "1m",
        &round_bars,
        round_bars.last().unwrap().ts_available,
        None,
        Some(&round_params),
        &[],
    )
    .unwrap();
    let round_prices: Vec<_> = rounds
        .iter()
        .filter(|l| l.kind == KeyLevelKind::RoundNumber)
        .filter_map(|l| l.price)
        .collect();
    for expected in ["58000", "59000", "60000", "61000", "62000"] {
        assert!(round_prices.contains(&d(expected)), "missing {expected}");
    }

    // Session VWAP waits for UTC boundary even with America/New_York profile & rewarms after gap
    let day = 1_699_920_000_000i64; // 2023-11-14 00:00:00 UTC
    let mk = |idx: i64, p: &str| {
        let ts = day + idx * MINUTE_MS;
        let pv = d(p);
        CompactBar {
            timestamp: ts,
            open: Some(pv),
            high: Some(pv),
            low: Some(pv),
            close: Some(pv),
            volume: Some(Decimal::ONE),
            turnover: Some(pv),
            status: BarStatus::Normal,
            ts_available: ts + MINUTE_MS,
            precision: PrecisionTier::SecondVerified,
        }
    };
    let ny_gap = CompactBar {
        timestamp: day + 2 * MINUTE_MS,
        open: None,
        high: None,
        low: None,
        close: None,
        volume: None,
        turnover: None,
        status: BarStatus::FeedGap,
        ts_available: day + 3 * MINUTE_MS,
        precision: PrecisionTier::SecondVerified,
    };
    let ny_bars = vec![
        mk(-2, "100"),
        mk(-1, "110"),
        mk(0, "200"),
        mk(1, "210"),
        ny_gap,
        mk(3, "250"),
        mk(1440, "300"),
    ];
    let ny_profile = SessionProfile {
        timezone: "America/New_York".to_owned(),
        day_start: "00:00".to_owned(),
        sessions: Vec::new(),
    };
    let ny_params = KeyLevelParams {
        vwap_anchor: VwapAnchor::Session,
        ..KeyLevelParams::default()
    };
    let expected = [None, None, Some(d("200")), Some(d("205")), None, None, Some(d("300"))];
    for (idx, want) in expected.iter().enumerate() {
        let levels = compute_key_levels(
            "BTC-USDT-PERP",
            "1m",
            &ny_bars,
            ny_bars[idx].ts_available,
            Some(&ny_profile),
            Some(&ny_params),
            &[],
        )
        .unwrap();
        let vwap: Vec<_> = levels
            .iter()
            .filter(|l| l.kind == KeyLevelKind::AnchoredVwap)
            .collect();
        match want {
            None => assert!(vwap.is_empty(), "idx={idx}"),
            Some(price) => {
                let center = vwap
                    .iter()
                    .find(|l| l.parameters.get("band_sigma").map(String::as_str) == Some("0"))
                    .unwrap();
                assert_eq!(center.price.unwrap().normalize(), price.normalize());
                assert_eq!(center.formed_ts, if idx < 6 { day } else { day + 1440 * MINUTE_MS });
            }
        }
    }
}

#[test]
fn week_open_month_open_opening_range_and_validation_errors() {
    // 2025-03-01 00:00:00 UTC = 1_740_787_200_000 ms (Saturday)
    let march1 = 1_740_787_200_000i64;
    let mut bars = Vec::new();
    for i in 0..(24 * 3 + 6) {
        let ts = march1 + i * HOUR_MS;
        bars.push(CompactBar {
            timestamp: ts,
            open: Some(d("200")),
            high: Some(d("201")),
            low: Some(d("199")),
            close: Some(d("200")),
            volume: Some(d("5")),
            turnover: Some(d("1000")),
            status: BarStatus::Normal,
            ts_available: ts + HOUR_MS,
            precision: PrecisionTier::SessionBound,
        });
    }
    let utc = SessionProfile {
        timezone: "UTC".to_owned(),
        day_start: "00:00".to_owned(),
        sessions: Vec::new(),
    };
    let cutoff = bars[35].ts_available; // 2025-03-02 noon: month started, week has not
    let levels = compute_key_levels(
        "BTC-USDT-PERP",
        "1h",
        &bars,
        cutoff,
        Some(&utc),
        None,
        &[],
    )
    .unwrap();
    let month_open = levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::MonthOpen)
        .unwrap();
    assert_eq!(month_open.price, Some(d("200")));
    assert_eq!(month_open.ts_available, bars[0].ts_available);
    assert!(!levels.iter().any(|l| l.kind == KeyLevelKind::WeekOpen));

    // 2025-03-03 00:00:00 UTC (Monday) -> week_open emitted
    let march3 = 1_740_960_000_000i64;
    let week_bars: Vec<CompactBar> = bars[..8]
        .iter()
        .map(|b| CompactBar {
            timestamp: b.timestamp + (march3 - march1),
            ts_available: b.ts_available + (march3 - march1),
            ..b.clone()
        })
        .collect();
    let week_levels = compute_key_levels(
        "BTC-USDT-PERP",
        "1h",
        &week_bars,
        week_bars.last().unwrap().ts_available,
        Some(&utc),
        None,
        &[],
    )
    .unwrap();
    let week_open = week_levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::WeekOpen)
        .unwrap();
    assert_eq!(week_open.price, Some(d("200")));
    assert_eq!(week_open.ts_available, week_bars[0].ts_available);

    // Opening range on 15m bars (30 min opening range)
    let or_bars = vec![
        CompactBar {
            timestamp: march1,
            open: Some(d("100")),
            high: Some(d("105")),
            low: Some(d("98")),
            close: Some(d("102")),
            volume: Some(d("10")),
            turnover: Some(d("1020")),
            status: BarStatus::Normal,
            ts_available: march1 + 15 * MINUTE_MS,
            precision: PrecisionTier::SecondVerified,
        },
        CompactBar {
            timestamp: march1 + 15 * MINUTE_MS,
            open: Some(d("102")),
            high: Some(d("108")),
            low: Some(d("101")),
            close: Some(d("106")),
            volume: Some(d("10")),
            turnover: Some(d("1060")),
            status: BarStatus::Normal,
            ts_available: march1 + 30 * MINUTE_MS,
            precision: PrecisionTier::SecondVerified,
        },
    ];
    let or_levels = compute_key_levels(
        "BTC-USDT-PERP",
        "15m",
        &or_bars,
        march1 + 30 * MINUTE_MS,
        Some(&utc),
        Some(&KeyLevelParams::default()),
        &[],
    )
    .unwrap();
    let or = or_levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::OpeningRange)
        .unwrap();
    assert_eq!(or.price, None);
    assert_eq!(or.price_low, Some(d("98")));
    assert_eq!(or.price_high, Some(d("108")));
    assert_eq!(or.ts_available, march1 + 30 * MINUTE_MS);

    // Validation error checks
    assert!(OperatorSpec::new(IndicatorName::Sma, Some(0), None, None).is_err());
    assert!(OperatorSpec::new(IndicatorName::Sma, Some(5001), None, None).is_err());
    assert!(OperatorSpec::new(IndicatorName::Boll, Some(1), None, None).is_err());
    assert!(OperatorSpec::new(IndicatorName::Boll, Some(20), Some(Decimal::ZERO), None).is_err());
    assert!(OperatorSpec::new(IndicatorName::Ema, Some(9), Some(d("2")), None).is_err());
    assert!(
        OperatorSpec::new(IndicatorName::Ema, Some(9), None, Some(VwapAnchorMode::Session))
            .is_err()
    );
    let fq_spec = OperatorSpec::new(IndicatorName::FundingQuantile, None, None, None).unwrap();
    assert!(compute_series(&or_bars, &fq_spec, None).is_err());
    assert!(compute_series(&or_bars, &fq_spec, Some(&[])).is_err());
    let bad_kl = KeyLevelParams {
        vwap_anchor: VwapAnchor::Timestamp,
        vwap_anchor_ts: None,
        ..KeyLevelParams::default()
    };
    assert!(bad_kl.validate().is_err());
}
