//! Character-for-character golden vector verification for Rust `talaria-operators::sizing`
//! against Python-generated `contracts/sizing_vectors/*.json` (ADR-0010 / spec D4).

use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;
use talaria_operators::{SizingPreviewInput, compute_sizing_preview, format_opt_normalized};

fn sizing_vector_dir() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../..")
        .join("contracts/sizing_vectors")
}

#[derive(Deserialize)]
struct SizingVectorFile {
    version: u32,
    suite: String,
    cases: Vec<SizingVectorCase>,
}

#[derive(Deserialize)]
struct SizingVectorCase {
    name: String,
    input: SizingPreviewInput,
    expected: ExpectedSizingPreview,
}

#[derive(Debug, PartialEq, Eq, Deserialize)]
struct ExpectedSizingPreview {
    quantity: Option<String>,
    basis: Option<String>,
    risk_amount_r: Option<String>,
    risk_pct_of_equity: Option<String>,
    est_liq_price: Option<String>,
    est_liq_distance: Option<String>,
    est_liq_distance_pct: Option<String>,
    rejected_reason: Option<String>,
}

#[test]
fn all_sizing_golden_vectors_match_python_character_for_character() {
    let dir = sizing_vector_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("failed to read {}: {e}", dir.display()))
        .filter_map(|entry| {
            let p = entry.ok()?.path();
            if p.extension().and_then(|e| e.to_str()) == Some("json") {
                Some(p)
            } else {
                None
            }
        })
        .collect();
    files.sort();
    assert!(
        files.len() >= 3,
        "expected at least 3 sizing vector suites in {}, found {}",
        dir.display(),
        files.len()
    );

    let mut total_cases = 0usize;
    for path in files {
        let raw = fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("failed to read {}: {e}", path.display()));
        let suite: SizingVectorFile = serde_json::from_str(&raw)
            .unwrap_or_else(|e| panic!("failed to parse {}: {e}", path.display()));
        assert_eq!(suite.version, 1, "unexpected vector version in {}", suite.suite);

        for case in suite.cases {
            let resp = compute_sizing_preview(&case.input);
            let actual = ExpectedSizingPreview {
                quantity: format_opt_normalized(resp.quantity),
                basis: resp.basis,
                risk_amount_r: format_opt_normalized(resp.risk_amount_r),
                risk_pct_of_equity: format_opt_normalized(resp.risk_pct_of_equity),
                est_liq_price: format_opt_normalized(resp.est_liq_price),
                est_liq_distance: format_opt_normalized(resp.est_liq_distance),
                est_liq_distance_pct: format_opt_normalized(resp.est_liq_distance_pct),
                rejected_reason: resp.rejected_reason,
            };
            assert_eq!(
                actual, case.expected,
                "sizing vector mismatch in suite '{}' case '{}'",
                suite.suite, case.name
            );
            total_cases += 1;
        }
    }
    assert!(total_cases >= 15, "expected comprehensive sizing coverage, got {total_cases}");
}
