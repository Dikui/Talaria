//! Cross-language JSON golden vector verification for `talaria_server::rules`.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use talaria_contracts::bars::CompactBar;
use talaria_contracts::derivatives::{DerivativeKind, DerivativePoint};
use talaria_contracts::drawings::{DrawingKind, DrawingRecord};
use talaria_contracts::feed::{DerivativeTickEvent, KlineEvent, OrderBookEvent};
use talaria_contracts::format_decimal_str;
use talaria_contracts::key_levels::KeyLevel;
use talaria_contracts::rules::{AlertLevelEvent, Rule};
use talaria_contracts::timeframe_ms;
use talaria_server::rules::{EvalContext, FeedEventRef, LevelSide, RuleObservation, RuleState, observe_rule};

const EXPECTED_FILES: &[&str] = &[
    "cross_gaps_revisions.json",
    "event_vs_bar_close.json",
    "key_level_retest.json",
    "overlay_bounds.json",
];

fn vector_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("contracts/rule_vectors")
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuleVectorSuite {
    schema_version: u32,
    category: String,
    instrument_id: String,
    timeframe: String,
    cases: Vec<RuleVectorCase>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuleVectorCase {
    name: String,
    description: String,
    start_ts: i64,
    rule: Rule,
    drawings: Vec<DrawingRecord>,
    levels: Vec<KeyLevel>,
    derivatives: Vec<DerivativePoint>,
    steps: Vec<RuleVectorStep>,
}

#[derive(Deserialize)]
#[serde(tag = "kind", content = "payload", rename_all = "snake_case")]
enum VectorEvent {
    Kline(KlineEvent),
    OrderBook(OrderBookEvent),
    DerivativeTick(DerivativeTickEvent),
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct RuleVectorStep {
    now_ms: i64,
    bar_ts: Option<i64>,
    consume_retests: bool,
    upsert_bars: Vec<CompactBar>,
    event: Option<VectorEvent>,
    levels: Option<Vec<KeyLevel>>,
    expected: Option<ExpectedObservation>,
    expected_state: Option<ExpectedRuleState>,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpectedObservation {
    matched: bool,
    observed: Option<String>,
    threshold: Option<String>,
    drawing_kind: Option<DrawingKind>,
    level_id: Option<String>,
    level_event: Option<AlertLevelEvent>,
    level_direction: Option<String>,
    crossed_at: Option<i64>,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
struct ExpectedRuleState {
    breakouts: BTreeMap<String, (String, i64)>,
    consumed_sides: BTreeMap<String, String>,
}

fn side_str(side: LevelSide) -> &'static str {
    match side {
        LevelSide::Up => "UP",
        LevelSide::Down => "DOWN",
    }
}

fn format_observation(obs: &RuleObservation) -> ExpectedObservation {
    ExpectedObservation {
        matched: obs.matched,
        observed: obs.observed.map(format_decimal_str),
        threshold: obs.threshold.map(format_decimal_str),
        drawing_kind: obs.drawing_kind,
        level_id: obs.level.as_ref().map(|lv| lv.level_id.clone()),
        level_event: obs.level_event,
        level_direction: obs.level_direction.map(|s| side_str(s).to_owned()),
        crossed_at: obs.crossed_at,
    }
}

fn format_rule_state(state: &RuleState) -> ExpectedRuleState {
    let breakouts = state
        .breakouts
        .iter()
        .map(|(k, (side, ts))| (k.clone(), (side_str(*side).to_owned(), *ts)))
        .collect();
    let consumed_sides = state
        .consumed_sides
        .iter()
        .map(|(k, side)| (k.clone(), side_str(*side).to_owned()))
        .collect();
    ExpectedRuleState {
        breakouts,
        consumed_sides,
    }
}

fn assert_rule_vector_suite(filename: &str) {
    let path = vector_dir().join(filename);
    let raw = fs::read_to_string(&path)
        .unwrap_or_else(|e| panic!("failed to read {}: {e}", path.display()));
    let suite: RuleVectorSuite = serde_json::from_str(&raw)
        .unwrap_or_else(|e| panic!("failed to parse {}: {e}", path.display()));
    assert_eq!(suite.schema_version, 1, "{filename}");
    assert!(!suite.category.is_empty(), "{filename}");
    assert!(!suite.cases.is_empty(), "{filename}");

    let tf_ms = timeframe_ms(&suite.timeframe).expect("valid timeframe");

    for case in &suite.cases {
        let _ = &case.description;
        let drawings: HashMap<String, DrawingRecord> = case
            .drawings
            .iter()
            .map(|d| (d.drawing_id.clone(), d.clone()))
            .collect();
        let default_levels: HashMap<String, KeyLevel> = case
            .levels
            .iter()
            .map(|lv| (lv.level_id.clone(), lv.clone()))
            .collect();

        let mut funding = Vec::new();
        let mut open_interest = Vec::new();
        let mut mark_price = Vec::new();
        for pt in &case.derivatives {
            match pt.kind {
                DerivativeKind::FundingRate => funding.push(pt.clone()),
                DerivativeKind::OpenInterest => open_interest.push(pt.clone()),
                DerivativeKind::MarkPrice => mark_price.push(pt.clone()),
            }
        }

        let mut bars_by_ts: BTreeMap<i64, CompactBar> = BTreeMap::new();
        let mut state = RuleState::default();

        for (step_idx, step) in case.steps.iter().enumerate() {
            for bar in &step.upsert_bars {
                bars_by_ts.insert(bar.timestamp, bar.clone());
            }

            let mut end = step.now_ms - tf_ms;
            let mut eff_bar_ts = step.bar_ts;
            let event_ref = step.event.as_ref().map(|ev| match ev {
                VectorEvent::Kline(k) => {
                    let slot = if k.confirmed {
                        k.bar.timestamp
                    } else {
                        k.bar.timestamp - tf_ms
                    };
                    eff_bar_ts = Some(eff_bar_ts.map_or(slot, |prev| prev.min(slot)));
                    FeedEventRef::Kline(k)
                }
                VectorEvent::OrderBook(b) => FeedEventRef::Book(b),
                VectorEvent::DerivativeTick(d) => FeedEventRef::Derivative(d),
            });
            if let Some(bt) = eff_bar_ts {
                end = end.min(bt);
            }

            // Match `Ctx::compact_bars()` / `ConfirmedWindow::visible(cutoff, bar_ts)`:
            // `observe_rule` trusts the caller to pre-filter `ctx.bars` to `<= effective_bar_ts`
            // (including capping before the open slot on unconfirmed `KlineEvent` under `RuleTiming::Event`).
            let visible_bars: Vec<CompactBar> = bars_by_ts
                .values()
                .filter(|b| {
                    b.timestamp >= case.start_ts
                        && b.timestamp <= end
                        && b.ts_available <= step.now_ms
                })
                .cloned()
                .collect();

            let step_levels: HashMap<String, KeyLevel> = match &step.levels {
                Some(lvs) => lvs
                    .iter()
                    .map(|lv| (lv.level_id.clone(), lv.clone()))
                    .collect(),
                None => default_levels.clone(),
            };

            let empty_structures = HashMap::new();
            let drawing_lookup = |id: &str| drawings.get(id).cloned();
            let mut ctx = EvalContext {
                instrument_id: &suite.instrument_id,
                timeframe: &suite.timeframe,
                now_ms: step.now_ms,
                bars: &visible_bars,
                funding: &funding,
                open_interest: &open_interest,
                mark_price: &mark_price,
                levels: &step_levels,
                structures: &empty_structures,
                drawing_lookup: &drawing_lookup,
                event: event_ref,
                bar_ts_override: step.bar_ts,
                indicator_cache: BTreeMap::new(),
            };

            let actual = observe_rule(&case.rule, &mut ctx, &mut state, step.consume_retests)
                .unwrap_or_else(|e| {
                    panic!("{filename}:{}[{step_idx}] observe_rule error: {e}", case.name)
                })
                .map(|obs| format_observation(&obs));

            assert_eq!(
                actual, step.expected,
                "{filename}:{}[{step_idx}] char-for-char observation mismatch",
                case.name
            );

            if let Some(expected_state) = &step.expected_state {
                assert_eq!(
                    &format_rule_state(&state),
                    expected_state,
                    "{filename}:{}[{step_idx}] RuleState mismatch",
                    case.name
                );
            }
        }
    }
}

#[test]
fn verifies_every_json_file_in_rule_vectors_dir_is_covered() {
    let mut files: Vec<String> = fs::read_dir(vector_dir())
        .expect("read contracts/rule_vectors")
        .map(|entry| entry.unwrap().file_name().to_string_lossy().into_owned())
        .filter(|name| name.ends_with(".json"))
        .collect();
    files.sort();
    assert_eq!(files, EXPECTED_FILES);
}

#[test]
fn cross_gaps_revisions_vectors_match_char_for_char() {
    assert_rule_vector_suite("cross_gaps_revisions.json");
}

#[test]
fn overlay_bounds_vectors_match_char_for_char() {
    assert_rule_vector_suite("overlay_bounds.json");
}

#[test]
fn key_level_retest_vectors_match_char_for_char() {
    assert_rule_vector_suite("key_level_retest.json");
}

#[test]
fn event_vs_bar_close_vectors_match_char_for_char() {
    assert_rule_vector_suite("event_vs_bar_close.json");
}
