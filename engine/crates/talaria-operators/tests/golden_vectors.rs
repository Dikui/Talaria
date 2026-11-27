use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::str::FromStr;

use rust_decimal::Decimal;
use serde::Deserialize;
use serde_json::Value;
use talaria_contracts::{
    BarStatus, CompactBar, DerivativeKind, DerivativePoint, IndicatorName, KeyLevel, KeyLevelKind,
    KeyLevelStatus, PrecisionTier, StructureFeature, StructureGeometry, VwapAnchorMode,
    format_decimal_str,
};
use talaria_operators::{
    DerivativeSample, KeyLevelParams, OperatorCursor, OperatorPoint, OperatorSpec, StructureParams,
    compute_key_levels, compute_key_levels_with_notes, compute_series, compute_structure_set,
    update_series,
};

const VECTOR_EPOCH_MS: i64 = 1_700_000_000_000;
const MINUTE_MS: i64 = 60_000;

fn vector_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("contracts/operator_vectors")
}

#[derive(Deserialize)]
struct OperatorVector {
    operator: IndicatorName,
    params: OperatorVectorParams,
    warmup_bars: usize,
    #[serde(default)]
    post_gap_warmup: Option<usize>,
    bars: Vec<RawVectorBar>,
    #[serde(default)]
    derivatives: Option<Vec<RawVectorDerivative>>,
    expected: Vec<BTreeMap<String, Option<String>>>,
}

#[derive(Deserialize)]
struct OperatorVectorParams {
    period: Option<u32>,
    std_dev: Option<String>,
    anchor: Option<VwapAnchorMode>,
}

#[derive(Deserialize)]
struct RawVectorBar {
    offset: i64,
    open: Option<String>,
    high: Option<String>,
    low: Option<String>,
    close: Option<String>,
    volume: Option<String>,
    turnover: Option<String>,
    status: BarStatus,
}

#[derive(Deserialize)]
struct RawVectorDerivative {
    funding_rate: Option<String>,
    funding_offset: Option<i64>,
    open_interest: Option<String>,
}

fn parse_opt_dec(raw: &Option<String>) -> Option<Decimal> {
    raw.as_ref()
        .map(|s| Decimal::from_str(s).unwrap_or_else(|e| panic!("bad decimal {s}: {e}")))
}

fn build_operator_bars(raw: &[RawVectorBar]) -> Vec<CompactBar> {
    raw.iter()
        .map(|item| {
            let ts = VECTOR_EPOCH_MS + item.offset * MINUTE_MS;
            CompactBar {
                timestamp: ts,
                open: parse_opt_dec(&item.open),
                high: parse_opt_dec(&item.high),
                low: parse_opt_dec(&item.low),
                close: parse_opt_dec(&item.close),
                volume: parse_opt_dec(&item.volume),
                turnover: parse_opt_dec(&item.turnover),
                status: item.status,
                ts_available: VECTOR_EPOCH_MS + (item.offset + 1) * MINUTE_MS,
                precision: PrecisionTier::SecondVerified,
            }
        })
        .collect()
}

fn build_operator_samples(raw: &Option<Vec<RawVectorDerivative>>) -> Option<Vec<DerivativeSample>> {
    raw.as_ref().map(|items| {
        items
            .iter()
            .map(|item| DerivativeSample {
                funding_rate: parse_opt_dec(&item.funding_rate),
                funding_ts: item
                    .funding_offset
                    .map(|off| VECTOR_EPOCH_MS + off * MINUTE_MS),
                open_interest: parse_opt_dec(&item.open_interest),
                oi_ts: None,
                mark_price: None,
                mark_ts: None,
            })
            .collect()
    })
}

fn format_points(points: &[OperatorPoint]) -> Vec<BTreeMap<String, Option<String>>> {
    points
        .iter()
        .map(|pt| {
            pt.iter()
                .map(|(k, v)| (k.clone(), v.map(format_decimal_str)))
                .collect()
        })
        .collect()
}

fn assert_operator_vector(filename: &str) {
    let path = vector_dir().join(filename);
    let raw = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("failed to read {}: {e}", path.display()));
    let vector: OperatorVector = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("failed to parse {}: {e}", path.display()));

    let std_dev = parse_opt_dec(&vector.params.std_dev);
    let spec = OperatorSpec::new(
        vector.operator,
        vector.params.period,
        std_dev,
        vector.params.anchor,
    )
    .expect("valid spec");
    let bars = build_operator_bars(&vector.bars);
    let samples = build_operator_samples(&vector.derivatives);

    let batch = compute_series(&bars, &spec, samples.as_deref()).expect("compute_series");
    assert_eq!(batch.points.len(), vector.expected.len(), "{filename}");
    assert_eq!(batch.points.len(), bars.len(), "{filename}");

    let actual_formatted = format_points(&batch.points);
    for (idx, (actual, expected)) in actual_formatted
        .iter()
        .zip(vector.expected.iter())
        .enumerate()
    {
        assert_eq!(
            actual, expected,
            "{filename} char-for-char mismatch at bar {idx}"
        );
    }

    // Check warmup_bars
    let leading = batch
        .points
        .iter()
        .position(|pt| pt.values().any(Option::is_some))
        .unwrap_or(batch.points.len());
    assert_eq!(leading, vector.warmup_bars, "{filename} warmup_bars");

    // Check FEED_GAP null & post_gap_warmup
    let gap_idx = vector
        .bars
        .iter()
        .position(|b| b.status == BarStatus::FeedGap)
        .expect("has FEED_GAP");
    assert!(
        batch.points[gap_idx].values().all(Option::is_none),
        "{filename} gap bar must emit null"
    );
    let post_gap = vector.post_gap_warmup.unwrap_or(vector.warmup_bars);
    if post_gap > 0 {
        let rewarm = &batch.points[(gap_idx + 1)..(gap_idx + 1 + post_gap)];
        assert_eq!(rewarm.len(), post_gap);
        assert!(
            rewarm
                .iter()
                .all(|pt| pt.values().all(Option::is_none)),
            "{filename} post_gap_warmup"
        );
    }
    if gap_idx + 1 + post_gap < batch.points.len() {
        assert!(
            batch.points[gap_idx + 1 + post_gap]
                .values()
                .any(Option::is_some),
            "{filename} must emit after rewarmup"
        );
    }

    // Check incremental split consistency across every possible split point
    for split in 0..=bars.len() {
        let s_head = samples.as_ref().map(|s| &s[..split]);
        let s_tail = samples.as_ref().map(|s| &s[split..]);
        let st = compute_series(&bars[..split], &spec, s_head).expect("split head");
        let st = update_series(&st, &bars[split..], &spec, s_tail).expect("split tail");
        assert_eq!(
            format_points(&st.points),
            actual_formatted,
            "{filename} incremental mismatch at split={split}"
        );
    }

    // Check bar-by-bar OperatorCursor consistency
    let mut cursor = OperatorCursor::new(spec, false).expect("cursor");
    let mut cursor_points = Vec::new();
    for i in 0..bars.len() {
        let s_slice = samples.as_ref().map(|s| &s[i..i + 1]);
        let pts = cursor.append(&bars[i..i + 1], s_slice).expect("cursor append");
        cursor_points.extend(pts);
    }
    assert_eq!(
        format_points(&cursor_points),
        actual_formatted,
        "{filename} cursor mismatch"
    );
}

#[test]
fn all_9_operator_golden_vectors_match_char_for_char() {
    for file in [
        "ema.json",
        "sma.json",
        "rsi.json",
        "atr.json",
        "boll.json",
        "vwap.json",
        "funding_quantile.json",
        "oi_divergence.json",
        "net_odds.json",
    ] {
        assert_operator_vector(file);
    }
}

#[derive(Deserialize)]
struct KeyLevelVectorFile {
    operator: String,
    method_version: String,
    instrument_id: String,
    timeframe: String,
    cases: Vec<KeyLevelCase>,
}

#[derive(Deserialize)]
struct KeyLevelCase {
    name: String,
    cutoff_ts: i64,
    params: KeyLevelParams,
    bars: Vec<CompactBar>,
    expected: Vec<ExpectedStructureLevel>,
}

#[derive(Debug, PartialEq, Deserialize)]
struct ExpectedStructureLevel {
    level_id: String,
    kind: KeyLevelKind,
    category: talaria_contracts::KeyLevelCategory,
    price: Option<String>,
    price_low: Option<String>,
    price_high: Option<String>,
    strength: f64,
    status: KeyLevelStatus,
    ts_available: i64,
    formed_ts: i64,
    touch_count: u64,
}

fn project_structure(level: &KeyLevel) -> ExpectedStructureLevel {
    ExpectedStructureLevel {
        level_id: level.level_id.clone(),
        kind: level.kind,
        category: level.category,
        price: level.price.map(format_decimal_str),
        price_low: level.price_low.map(format_decimal_str),
        price_high: level.price_high.map(format_decimal_str),
        strength: level.strength,
        status: level.status,
        ts_available: level.ts_available,
        formed_ts: level.formed_ts,
        touch_count: level.touch_count,
    }
}

#[test]
fn key_levels_golden_vector_matches_bit_for_bit() {
    let path = vector_dir().join("key_levels.json");
    let raw = fs::read_to_string(&path).expect("read key_levels.json");
    let payload: KeyLevelVectorFile = serde_json::from_str(&raw).expect("parse key_levels.json");

    assert_eq!(payload.operator, "key_levels");
    assert_eq!(payload.method_version, "key-levels@2");
    assert!(!payload.cases.is_empty());

    let mut by_name: BTreeMap<String, Vec<ExpectedStructureLevel>> = BTreeMap::new();
    for case in &payload.cases {
        let levels = compute_key_levels(
            &payload.instrument_id,
            &payload.timeframe,
            &case.bars,
            case.cutoff_ts,
            None,
            Some(&case.params),
            &[],
        )
        .unwrap_or_else(|e| panic!("case {} failed: {e}", case.name));

        for level in &levels {
            assert!(level.ts_available <= case.cutoff_ts, "{}", case.name);
        }

        let structure: Vec<ExpectedStructureLevel> = levels
            .iter()
            .filter(|l| matches!(l.kind, KeyLevelKind::SwingHigh | KeyLevelKind::SwingLow))
            .map(project_structure)
            .collect();

        assert_eq!(structure, case.expected, "case {}", case.name);
        by_name.insert(case.name.clone(), structure);
    }

    // Status chain identity
    let active = &by_name["support_active_broken_flipped"][0];
    let broken = &by_name["support_broken"][0];
    let flipped = &by_name["support_flipped"][0];
    assert_eq!(active.level_id, broken.level_id);
    assert_eq!(broken.level_id, flipped.level_id);
    assert_eq!(active.status, KeyLevelStatus::Active);
    assert_eq!(broken.status, KeyLevelStatus::Broken);
    assert_eq!(flipped.status, KeyLevelStatus::Flipped);

    // Order-invariance on zone_cluster
    let zone_case = payload
        .cases
        .iter()
        .find(|c| c.name == "zone_cluster")
        .unwrap();
    let mut rev_bars = zone_case.bars.clone();
    rev_bars.reverse();
    let rev_levels = compute_key_levels(
        &payload.instrument_id,
        &payload.timeframe,
        &rev_bars,
        zone_case.cutoff_ts,
        None,
        Some(&zone_case.params),
        &[],
    )
    .unwrap();
    let rev_zone = rev_levels
        .iter()
        .find(|l| l.kind == KeyLevelKind::SwingHigh)
        .unwrap();
    assert_eq!(rev_zone.level_id, zone_case.expected[0].level_id);
}

#[derive(Deserialize)]
struct KeyLevelVolumeVectorFile {
    operator: String,
    method_version: String,
    instrument_id: String,
    timeframe: String,
    cases: Vec<KeyLevelVolumeCase>,
}

#[derive(Deserialize)]
struct KeyLevelVolumeCase {
    name: String,
    cutoff_ts: i64,
    params: KeyLevelParams,
    bars: Vec<CompactBar>,
    derivative_points: Vec<RawVolumeDerivativePoint>,
    include_kinds: Vec<KeyLevelKind>,
    expected: ExpectedVolumeOutput,
}

#[derive(Deserialize)]
struct RawVolumeDerivativePoint {
    kind: DerivativeKind,
    timeframe: Option<String>,
    value: String,
    ts_event: i64,
    ts_published: Option<i64>,
    ts_available: i64,
    #[serde(default)]
    precision: Option<PrecisionTier>,
    ts_ingested: i64,
    ts_committed: i64,
}

#[derive(Deserialize)]
struct ExpectedVolumeOutput {
    levels: Vec<ExpectedVolumeLevel>,
    #[serde(default)]
    notes: Vec<String>,
}

#[derive(Debug, PartialEq, Deserialize)]
struct ExpectedVolumeLevel {
    kind: KeyLevelKind,
    category: talaria_contracts::KeyLevelCategory,
    evidence_class: talaria_contracts::EvidenceClass,
    price: Option<String>,
    price_low: Option<String>,
    price_high: Option<String>,
    strength: f64,
    status: KeyLevelStatus,
    ts_available: i64,
    formed_ts: i64,
    parameters: BTreeMap<String, String>,
}

fn build_derivative_points(
    raw: &[RawVolumeDerivativePoint],
    instrument_id: &str,
) -> Vec<DerivativePoint> {
    raw.iter()
        .map(|p| DerivativePoint {
            instrument_id: instrument_id.to_owned(),
            kind: p.kind,
            timeframe: p.timeframe.clone(),
            value: Decimal::from_str(&p.value).unwrap(),
            ts_event: p.ts_event,
            ts_published: p.ts_published,
            ts_available: p.ts_available,
            precision: p.precision.unwrap_or(PrecisionTier::SecondVerified),
            ts_ingested: p.ts_ingested,
            ts_committed: p.ts_committed,
            revision: 0,
            settled: None,
        })
        .collect()
}

fn project_volume_level(
    level: &KeyLevel,
    expected_keys: &BTreeMap<String, String>,
) -> ExpectedVolumeLevel {
    let mut params = BTreeMap::new();
    for key in expected_keys.keys() {
        let val = level
            .parameters
            .get(key)
            .unwrap_or_else(|| panic!("missing parameter key {key}"));
        params.insert(key.clone(), val.clone());
    }
    ExpectedVolumeLevel {
        kind: level.kind,
        category: level.category,
        evidence_class: level.evidence_class,
        price: level.price.map(format_decimal_str),
        price_low: level.price_low.map(format_decimal_str),
        price_high: level.price_high.map(format_decimal_str),
        strength: level.strength,
        status: level.status,
        ts_available: level.ts_available,
        formed_ts: level.formed_ts,
        parameters: params,
    }
}

#[test]
fn key_levels_volume_golden_vector_matches_bit_for_bit() {
    let path = vector_dir().join("key_levels_volume.json");
    let raw = fs::read_to_string(&path).expect("read key_levels_volume.json");
    let payload: KeyLevelVolumeVectorFile =
        serde_json::from_str(&raw).expect("parse key_levels_volume.json");

    assert_eq!(payload.operator, "key_level_volume");
    assert_eq!(payload.method_version, "key-levels@2");
    assert!(!payload.cases.is_empty());

    for case in &payload.cases {
        let points = build_derivative_points(&case.derivative_points, &payload.instrument_id);
        let computation = compute_key_levels_with_notes(
            &payload.instrument_id,
            &payload.timeframe,
            &case.bars,
            case.cutoff_ts,
            None,
            Some(&case.params),
            &points,
        )
        .unwrap_or_else(|e| panic!("case {} failed: {e}", case.name));

        for level in &computation.levels {
            assert!(level.ts_available <= case.cutoff_ts, "{}", case.name);
            assert!(level.formed_ts <= level.ts_available, "{}", case.name);
        }

        let included: HashSet<KeyLevelKind> = case.include_kinds.iter().copied().collect();
        let projected: Vec<ExpectedVolumeLevel> = if case.expected.levels.is_empty() {
            Vec::new()
        } else {
            let expected_keys = &case.expected.levels[0].parameters;
            computation
                .levels
                .iter()
                .filter(|l| included.contains(&l.kind))
                .map(|l| project_volume_level(l, expected_keys))
                .collect()
        };

        assert_eq!(projected, case.expected.levels, "case {}", case.name);
        assert_eq!(computation.notes, case.expected.notes, "notes {}", case.name);
    }

    // Order-invariance on liquidation_estimate_from_oi
    let liq_case = payload
        .cases
        .iter()
        .find(|c| c.name == "liquidation_estimate_from_oi")
        .unwrap();
    let points = build_derivative_points(&liq_case.derivative_points, &payload.instrument_id);
    let first = compute_key_levels_with_notes(
        &payload.instrument_id,
        &payload.timeframe,
        &liq_case.bars,
        liq_case.cutoff_ts,
        None,
        Some(&liq_case.params),
        &points,
    )
    .unwrap();
    let mut rev_bars = liq_case.bars.clone();
    rev_bars.reverse();
    let mut rev_points = points;
    rev_points.reverse();
    let second = compute_key_levels_with_notes(
        &payload.instrument_id,
        &payload.timeframe,
        &rev_bars,
        liq_case.cutoff_ts,
        None,
        Some(&liq_case.params),
        &rev_points,
    )
    .unwrap();
    let first_ids: Vec<&str> = first.levels.iter().map(|l| l.level_id.as_str()).collect();
    let second_ids: Vec<&str> = second.levels.iter().map(|l| l.level_id.as_str()).collect();
    assert_eq!(first_ids, second_ids);
}

#[derive(Deserialize)]
struct StructureVectorFile {
    operator: String,
    method_version: String,
    instrument_id: String,
    timeframe: String,
    cases: Vec<StructureVectorCase>,
}

#[derive(Deserialize)]
struct StructureVectorCase {
    name: String,
    cutoff_ts: i64,
    detector_ids: Vec<String>,
    #[serde(default)]
    params: StructureParams,
    bars: Vec<CompactBar>,
    expected: Vec<StructureFeature>,
}

fn assert_approx_dec(actual: Decimal, expected: Decimal, tol: Decimal, label: &str) {
    let diff = (actual - expected).abs();
    assert!(diff <= tol, "{label}: actual={actual} expected={expected} diff={diff}");
}

fn assert_geometry_matches(actual: &StructureGeometry, expected: &StructureGeometry, label: &str) {
    let tol = Decimal::new(1, 5);
    match (actual, expected) {
        (
            StructureGeometry::HorizontalLine { price: a },
            StructureGeometry::HorizontalLine { price: e },
        ) => assert_eq!(a, e, "{label}"),
        (
            StructureGeometry::HorizontalZone {
                price_low: al,
                price_high: ah,
                start_ts: as_,
                end_ts: ae,
            },
            StructureGeometry::HorizontalZone {
                price_low: el,
                price_high: eh,
                start_ts: es,
                end_ts: ee,
            },
        ) => {
            assert_approx_dec(*al, *el, tol, &format!("{label}.price_low"));
            assert_approx_dec(*ah, *eh, tol, &format!("{label}.price_high"));
            assert_eq!(as_, es, "{label}.start_ts");
            assert_eq!(ae, ee, "{label}.end_ts");
        }
        (
            StructureGeometry::SlopeLine {
                anchor_start: a1,
                anchor_end: a2,
                scale: asc,
                extend_right: aext,
            },
            StructureGeometry::SlopeLine {
                anchor_start: e1,
                anchor_end: e2,
                scale: esc,
                extend_right: eext,
            },
        ) => {
            assert_eq!(a1.ts, e1.ts, "{label}.start.ts");
            assert_eq!(a2.ts, e2.ts, "{label}.end.ts");
            assert_approx_dec(a1.price, e1.price, tol, &format!("{label}.start.price"));
            assert_approx_dec(a2.price, e2.price, tol, &format!("{label}.end.price"));
            assert_eq!(asc, esc, "{label}.scale");
            assert_eq!(aext, eext, "{label}.extend_right");
        }
        _ => assert_eq!(actual, expected, "{label}"),
    }
}

#[test]
fn structures_golden_vector_matches_python_authority() {
    let path = vector_dir().join("structures.json");
    let raw = fs::read_to_string(&path).expect("read structures.json");
    let payload: StructureVectorFile = serde_json::from_str(&raw).expect("parse structures.json");
    assert_eq!(payload.operator, "structures");
    assert_eq!(payload.method_version, "structures@1");
    assert!(!payload.cases.is_empty());

    for case in &payload.cases {
        let actual_set = compute_structure_set(
            "vec_snap",
            &payload.instrument_id,
            &payload.timeframe,
            &case.bars,
            case.cutoff_ts,
            None,
            None,
            Some(&case.params),
            &[],
            Some(&case.detector_ids),
            None,
        )
        .unwrap_or_else(|e| panic!("case {} failed: {e}", case.name));

        assert_eq!(
            actual_set.structures.len(),
            case.expected.len(),
            "case {} count mismatch",
            case.name
        );
        for (idx, (a, e)) in actual_set
            .structures
            .iter()
            .zip(case.expected.iter())
            .enumerate()
        {
            let lbl = format!("{}.{}", case.name, idx);
            assert_eq!(a.detector_id, e.detector_id, "{lbl}.detector_id");
            assert_eq!(a.family, e.family, "{lbl}.family");
            assert_eq!(a.category, e.category, "{lbl}.category");
            assert_eq!(a.kind, e.kind, "{lbl}.kind");
            assert_eq!(a.status, e.status, "{lbl}.status");
            assert_eq!(a.ts_available, e.ts_available, "{lbl}.ts_available");
            assert_eq!(a.formed_ts, e.formed_ts, "{lbl}.formed_ts");
            assert_eq!(a.touch_count, e.touch_count, "{lbl}.touch_count");
            assert!((a.strength - e.strength).abs() <= 0.002, "{lbl}.strength");
            assert_geometry_matches(&a.geometry, &e.geometry, &format!("{lbl}.geometry"));
            if a.kind != "CONFLUENCE_ZONE" {
                assert_eq!(a.structure_id, e.structure_id, "{lbl}.structure_id");
            }
        }
    }
}

#[test]
fn verifies_every_json_file_in_operator_vectors_dir_is_covered() {
    let dir = vector_dir();
    let mut files: Vec<String> = fs::read_dir(&dir)
        .unwrap()
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".json"))
        .collect();
    files.sort();
    assert_eq!(
        files,
        vec![
            "atr.json",
            "boll.json",
            "ema.json",
            "funding_quantile.json",
            "key_levels.json",
            "key_levels_volume.json",
            "net_odds.json",
            "oi_divergence.json",
            "rsi.json",
            "sma.json",
            "structures.json",
            "vwap.json",
        ]
    );
    // Also sanity check Value parsing on every file
    for file in files {
        let content = fs::read_to_string(dir.join(&file)).unwrap();
        let _: Value = serde_json::from_str(&content).unwrap();
    }
}
