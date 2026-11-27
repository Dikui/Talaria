//! Integration tests for Chain 2 Track A & Track B Rust API endpoints:
//! - Track A: `GET /api/key-levels/at` & `GET /api/key-levels?as_of=` stateless PIT computation
//!   with zero disk/snapshot/registry writes and `confirm_bars` PIT behavior
//! - Track A: `GET /api/indicators?as_of=&start=&end=` stateless backfill with zero disk writes
//! - Track A: `GET /api/ticks` lightweight JSON replay ticks with >2000 bucket aggregation & empty fallback
//! - Track B: `GET /api/runs*` skipping incomplete dirs missing `summary.json` and working without `ledger.sqlite`

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::path::Path;
use std::sync::Arc;

use arrow::array::{Int64Builder, StringBuilder};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::ArrowWriter;
use rust_decimal::Decimal;
use serde_json::{Value, json};
use talaria_contracts::bars::RawBar;
use talaria_contracts::key_levels::{KeyLevelKind, KeyLevelSet};
use talaria_contracts::market::Direction;
use talaria_contracts::runs::{
    RunAccountCurrencySummary, RunBarrierChange, RunBarrierChangeKind, RunEquityPoint,
    RunEquityResponse, RunExportArtifacts, RunExportSummary, RunFillKind, RunFillReason,
    RunFillSide, RunFillTier, RunListResponse, RunRoundFill, RunRoundPhase, RunRoundRecord,
    RunRoundsResponse, RunStatus,
};
use talaria_contracts::ticks::{TickKind, TicksJsonResponse, Trade, TradeAvailability, TradeSide};
use talaria_contracts::types::PrecisionTier;
use talaria_contracts::IndicatorBundle;
use talaria_packs::{DEFAULT_PACK_NAME, get_market_pack};
use talaria_server::{ServerConfig, start_server};
use talaria_store::partition_month;
use tempfile::TempDir;
use uuid::Uuid;

const BTC: &str = "BTC-USDT-PERP";
const BASE_TS: i64 = 1_767_225_600_000; // 2026-01-01T00:00:00Z
const MINUTE: i64 = 60_000;

fn btc_spec() -> talaria_contracts::market::InstrumentSpec {
    get_market_pack(DEFAULT_PACK_NAME)
        .unwrap()
        .instrument(BTC)
        .unwrap()
        .clone()
}

fn offline_config(data_dir: &Path, now_ms: i64) -> ServerConfig {
    fs::write(
        data_dir.join("service.json"),
        json!({
            "version": 1,
            "features": {
                "market_data": false,
                "cold_store": true,
                "alerts": false,
                "scanner": false
            }
        })
        .to_string(),
    )
    .unwrap();
    let mut cfg = ServerConfig::new(data_dir);
    cfg.now_ms = Some(Arc::new(move || now_ms));
    cfg
}

fn snapshot_tree(root: &Path) -> BTreeMap<String, (u64, Vec<u8>)> {
    let mut out = BTreeMap::new();
    fn walk(base: &Path, dir: &Path, out: &mut BTreeMap<String, (u64, Vec<u8>)>) {
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let p = entry.path();
            if p.is_dir() {
                walk(base, &p, out);
            } else if p.is_file() {
                let rel = p
                    .strip_prefix(base)
                    .unwrap()
                    .to_string_lossy()
                    .replace('\\', "/");
                let bytes = fs::read(&p).unwrap_or_default();
                out.insert(rel, (bytes.len() as u64, bytes));
            }
        }
    }
    walk(root, root, &mut out);
    out
}

fn raw_bar(ts: i64, open: i64, high: i64, low: i64, close: i64) -> RawBar {
    RawBar {
        symbol: "BTCUSDT".to_owned(),
        timeframe: "1m".to_owned(),
        ts_event: ts,
        open: Decimal::from(open),
        high: Decimal::from(high),
        low: Decimal::from(low),
        close: Decimal::from(close),
        volume: Decimal::from(100),
        turnover: Decimal::from(close * 100),
        ts_available: ts + MINUTE,
        ts_ingested: ts + MINUTE,
        ts_committed: ts + MINUTE,
        precision: PrecisionTier::SecondVerified,
        revision: 1,
        confirmed: true,
        history_only: false,
        synthetic: false,
    }
}

fn write_rounds_parquet(path: &Path, rounds: &[RunRoundRecord]) {
    let schema = Arc::new(Schema::new(vec![
        Field::new("round_id", DataType::Utf8, false),
        Field::new("sub_account_id", DataType::Utf8, false),
        Field::new("currency", DataType::Utf8, false),
        Field::new("instrument_id", DataType::Utf8, false),
        Field::new("direction", DataType::Utf8, false),
        Field::new("phase", DataType::Utf8, false),
        Field::new("opened_ts", DataType::Int64, true),
        Field::new("closed_ts", DataType::Int64, true),
        Field::new("entry_price", DataType::Utf8, true),
        Field::new("exit_price", DataType::Utf8, true),
        Field::new("max_quantity", DataType::Utf8, false),
        Field::new("remaining_quantity", DataType::Utf8, false),
        Field::new("stop", DataType::Utf8, false),
        Field::new("take_profit_json", DataType::Utf8, false),
        Field::new("exit_reason", DataType::Utf8, true),
        Field::new("raw_exit_reason", DataType::Utf8, true),
        Field::new("net_pnl", DataType::Utf8, false),
        Field::new("fees", DataType::Utf8, false),
        Field::new("funding", DataType::Utf8, false),
        Field::new("liquidation_fees", DataType::Utf8, false),
        Field::new("mae", DataType::Utf8, true),
        Field::new("mfe", DataType::Utf8, true),
        Field::new("fills_json", DataType::Utf8, false),
        Field::new("barrier_changes_json", DataType::Utf8, false),
    ]));

    let mut round_id = StringBuilder::new();
    let mut sub_account_id = StringBuilder::new();
    let mut currency = StringBuilder::new();
    let mut instrument_id = StringBuilder::new();
    let mut direction = StringBuilder::new();
    let mut phase = StringBuilder::new();
    let mut opened_ts = Int64Builder::new();
    let mut closed_ts = Int64Builder::new();
    let mut entry_price = StringBuilder::new();
    let mut exit_price = StringBuilder::new();
    let mut max_quantity = StringBuilder::new();
    let mut remaining_quantity = StringBuilder::new();
    let mut stop = StringBuilder::new();
    let mut take_profit_json = StringBuilder::new();
    let mut exit_reason = StringBuilder::new();
    let mut raw_exit_reason = StringBuilder::new();
    let mut net_pnl = StringBuilder::new();
    let mut fees = StringBuilder::new();
    let mut funding = StringBuilder::new();
    let mut liquidation_fees = StringBuilder::new();
    let mut mae = StringBuilder::new();
    let mut mfe = StringBuilder::new();
    let mut fills_json = StringBuilder::new();
    let mut barrier_changes_json = StringBuilder::new();

    for r in rounds {
        round_id.append_value(&r.round_id);
        sub_account_id.append_value(&r.sub_account_id);
        currency.append_value(&r.currency);
        instrument_id.append_value(&r.instrument_id);
        direction.append_value(match r.direction {
            Direction::Long => "LONG",
            Direction::Short => "SHORT",
        });
        let phase_str = serde_json::to_value(r.phase).unwrap();
        phase.append_value(phase_str.as_str().unwrap());
        opened_ts.append_option(r.opened_ts);
        closed_ts.append_option(r.closed_ts);
        entry_price.append_option(r.entry_price.map(|d| d.to_string()));
        exit_price.append_option(r.exit_price.map(|d| d.to_string()));
        max_quantity.append_value(r.max_quantity.to_string());
        remaining_quantity.append_value(r.remaining_quantity.to_string());
        stop.append_value(r.stop.to_string());
        let tp_strs: Vec<String> = r.take_profit.iter().map(|d| d.to_string()).collect();
        take_profit_json.append_value(serde_json::to_string(&tp_strs).unwrap());
        exit_reason.append_option(
            r.exit_reason
                .map(|er| serde_json::to_value(er).unwrap().as_str().unwrap().to_owned()),
        );
        raw_exit_reason.append_option(r.raw_exit_reason.as_deref());
        net_pnl.append_value(r.net_pnl.to_string());
        fees.append_value(r.fees.to_string());
        funding.append_value(r.funding.to_string());
        liquidation_fees.append_value(r.liquidation_fees.to_string());
        mae.append_option(r.mae.map(|d| d.to_string()));
        mfe.append_option(r.mfe.map(|d| d.to_string()));
        fills_json.append_value(serde_json::to_string(&r.fills).unwrap());
        barrier_changes_json.append_value(serde_json::to_string(&r.barrier_changes).unwrap());
    }

    let batch = RecordBatch::try_new(
        Arc::clone(&schema),
        vec![
            Arc::new(round_id.finish()),
            Arc::new(sub_account_id.finish()),
            Arc::new(currency.finish()),
            Arc::new(instrument_id.finish()),
            Arc::new(direction.finish()),
            Arc::new(phase.finish()),
            Arc::new(opened_ts.finish()),
            Arc::new(closed_ts.finish()),
            Arc::new(entry_price.finish()),
            Arc::new(exit_price.finish()),
            Arc::new(max_quantity.finish()),
            Arc::new(remaining_quantity.finish()),
            Arc::new(stop.finish()),
            Arc::new(take_profit_json.finish()),
            Arc::new(exit_reason.finish()),
            Arc::new(raw_exit_reason.finish()),
            Arc::new(net_pnl.finish()),
            Arc::new(fees.finish()),
            Arc::new(funding.finish()),
            Arc::new(liquidation_fees.finish()),
            Arc::new(mae.finish()),
            Arc::new(mfe.finish()),
            Arc::new(fills_json.finish()),
            Arc::new(barrier_changes_json.finish()),
        ],
    )
    .unwrap();

    let file = File::create(path).unwrap();
    let mut writer = ArrowWriter::try_new(file, schema, None).unwrap();
    writer.write(&batch).unwrap();
    writer.close().unwrap();
}

fn write_equity_parquet(path: &Path, points: &[RunEquityPoint]) {
    let schema = Arc::new(Schema::new(vec![
        Field::new("sub_account_id", DataType::Utf8, false),
        Field::new("currency", DataType::Utf8, false),
        Field::new("seq", DataType::Int64, false),
        Field::new("ts_event", DataType::Int64, false),
        Field::new("event_id", DataType::Utf8, false),
        Field::new("event_kind", DataType::Utf8, false),
        Field::new("round_id", DataType::Utf8, true),
        Field::new("pnl_basis", DataType::Utf8, false),
        Field::new("realized_pnl_delta", DataType::Utf8, false),
        Field::new("fee_delta", DataType::Utf8, false),
        Field::new("funding_delta", DataType::Utf8, false),
        Field::new("liquidation_fee_delta", DataType::Utf8, false),
        Field::new("net_pnl", DataType::Utf8, false),
        Field::new("equity", DataType::Utf8, false),
        Field::new("peak_pnl", DataType::Utf8, false),
        Field::new("drawdown", DataType::Utf8, false),
        Field::new("max_drawdown", DataType::Utf8, false),
    ]));

    let mut sub_account_id = StringBuilder::new();
    let mut currency = StringBuilder::new();
    let mut seq = Int64Builder::new();
    let mut ts_event = Int64Builder::new();
    let mut event_id = StringBuilder::new();
    let mut event_kind = StringBuilder::new();
    let mut round_id = StringBuilder::new();
    let mut pnl_basis = StringBuilder::new();
    let mut realized_pnl_delta = StringBuilder::new();
    let mut fee_delta = StringBuilder::new();
    let mut funding_delta = StringBuilder::new();
    let mut liquidation_fee_delta = StringBuilder::new();
    let mut net_pnl = StringBuilder::new();
    let mut equity = StringBuilder::new();
    let mut peak_pnl = StringBuilder::new();
    let mut drawdown = StringBuilder::new();
    let mut max_drawdown = StringBuilder::new();

    for p in points {
        sub_account_id.append_value(&p.sub_account_id);
        currency.append_value(&p.currency);
        seq.append_value(p.seq);
        ts_event.append_value(p.ts_event);
        event_id.append_value(&p.event_id);
        event_kind.append_value(&p.event_kind);
        round_id.append_option(p.round_id.as_deref());
        pnl_basis.append_value(&p.pnl_basis);
        realized_pnl_delta.append_value(p.realized_pnl_delta.to_string());
        fee_delta.append_value(p.fee_delta.to_string());
        funding_delta.append_value(p.funding_delta.to_string());
        liquidation_fee_delta.append_value(p.liquidation_fee_delta.to_string());
        net_pnl.append_value(p.net_pnl.to_string());
        equity.append_value(p.equity.to_string());
        peak_pnl.append_value(p.peak_pnl.to_string());
        drawdown.append_value(p.drawdown.to_string());
        max_drawdown.append_value(p.max_drawdown.to_string());
    }

    let batch = RecordBatch::try_new(
        Arc::clone(&schema),
        vec![
            Arc::new(sub_account_id.finish()),
            Arc::new(currency.finish()),
            Arc::new(seq.finish()),
            Arc::new(ts_event.finish()),
            Arc::new(event_id.finish()),
            Arc::new(event_kind.finish()),
            Arc::new(round_id.finish()),
            Arc::new(pnl_basis.finish()),
            Arc::new(realized_pnl_delta.finish()),
            Arc::new(fee_delta.finish()),
            Arc::new(funding_delta.finish()),
            Arc::new(liquidation_fee_delta.finish()),
            Arc::new(net_pnl.finish()),
            Arc::new(equity.finish()),
            Arc::new(peak_pnl.finish()),
            Arc::new(drawdown.finish()),
            Arc::new(max_drawdown.finish()),
        ],
    )
    .unwrap();

    let file = File::create(path).unwrap();
    let mut writer = ArrowWriter::try_new(file, schema, None).unwrap();
    writer.write(&batch).unwrap();
    writer.close().unwrap();
}

#[tokio::test]
async fn key_levels_at_and_indicators_as_of_zero_disk_writes_and_confirm_bars_pit() {
    let tmp = TempDir::new().unwrap();
    let cfg = offline_config(tmp.path(), BASE_TS + 100 * MINUTE);
    let srv = start_server(cfg).await.unwrap();
    let cold_store = srv.state.cold_store.as_ref().unwrap();
    let spec = btc_spec();

    // Seed 30 bars with a pronounced swing high at index 10 (price 55_000 vs ~50_000 around it).
    // Default KeyLevelParams has swing_span = 3, confirm_bars = 3, so bar 10 is confirmed at bar 13.
    let mut bars = Vec::new();
    for i in 0..30 {
        let ts = BASE_TS + (i as i64) * MINUTE;
        let (open, high, low, close) = if i == 10 {
            (51_000, 55_000, 50_800, 51_200)
        } else {
            let base = 50_000 + ((i as i64 % 3) * 50);
            (base, base + 100, base - 100, base + 20)
        };
        bars.push(raw_bar(ts, open, high, low, close));
    }
    cold_store.publish_bars(&spec, "1m", &bars).unwrap();

    // Capture full disk tree of state/ and cold/ and snapshots/ BEFORE Track A calls
    let before_state = snapshot_tree(&tmp.path().join("state"));
    let before_cold = snapshot_tree(&tmp.path().join("cold"));
    let before_snaps = snapshot_tree(&tmp.path().join("snapshots"));

    let client = reqwest::Client::new();

    // Default KeyLevelParams has swing_span = 2, confirm_bars = 2, so bar 10 is confirmed at bar 12
    // (bar 12 ts_available = BASE_TS + 13*MINUTE).
    // 1. Before confirm_bars (bar 11 confirmed at BASE_TS + 12*MINUTE): swing high at 55_000 MUST NOT appear
    let before_confirm_as_of = BASE_TS + 12 * MINUTE;
    let kl_before: KeyLevelSet = client
        .get(format!(
            "{}/api/key-levels/at?instrument={BTC}&timeframe=1m&as_of={before_confirm_as_of}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(kl_before.as_of_ts, before_confirm_as_of);
    assert!(
        !kl_before
            .levels
            .iter()
            .any(|lv| lv.kind == KeyLevelKind::SwingHigh && lv.price == Some(Decimal::from(55_000))),
        "swing high must NOT appear before confirm_bars elapse; got {:?}",
        kl_before.levels
    );

    // 2. At confirm bar availability (bar 12 ts_available = BASE_TS + 13*MINUTE): swing high MUST appear
    let after_confirm_as_of = BASE_TS + 13 * MINUTE;
    let kl_after: KeyLevelSet = client
        .get(format!(
            "{}/api/key-levels/at?instrument={BTC}&timeframe=1m&as_of={after_confirm_as_of}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(
        kl_after
            .levels
            .iter()
            .any(|lv| lv.kind == KeyLevelKind::SwingHigh && lv.price == Some(Decimal::from(55_000))),
        "swing high MUST appear once confirm_bars elapse; got {:?}",
        kl_after.levels
    );

    // Also verify optional as_of on GET /api/key-levels matches
    let kl_alias: KeyLevelSet = client
        .get(format!(
            "{}/api/key-levels?instrument={BTC}&timeframe=1m&as_of={after_confirm_as_of}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(kl_alias.levels, kl_after.levels);

    // 3. Stateless indicators backfill with as_of and start/end
    let mid_as_of = BASE_TS + 15 * MINUTE;
    let ind_mid: IndicatorBundle = client
        .get(format!(
            "{}/api/indicators?instrument={BTC}&timeframe=1m&names=EMA&as_of={mid_as_of}&limit=5000",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ind_mid.series.len(), 1);
    assert_eq!(ind_mid.series[0].points.len(), 15);
    assert!(
        ind_mid.series[0]
            .points
            .iter()
            .all(|p| p.timestamp + MINUTE <= mid_as_of)
    );

    let range_start = BASE_TS + 10 * MINUTE;
    let range_end = BASE_TS + 14 * MINUTE;
    let ind_range: IndicatorBundle = client
        .get(format!(
            "{}/api/indicators?instrument={BTC}&timeframe=1m&names=EMA&as_of={mid_as_of}&start={range_start}&end={range_end}&limit=5000",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ind_range.series[0].points.len(), 5);
    assert_eq!(ind_range.series[0].points[0].timestamp, range_start);
    assert_eq!(ind_range.series[0].points[4].timestamp, range_end);
    // Warmup already ran from bar 0..9, so all 5 points in [10..14] have valid EMA values
    assert_eq!(ind_range.series[0].summary.valid_points, 5);

    // 4. Verify ZERO snapshot/registry/disk writes occurred
    assert!(
        srv.state.key_levels.latest(BTC, "1m").is_none(),
        "KeyLevelRegistry must remain untouched"
    );
    assert_eq!(before_state, snapshot_tree(&tmp.path().join("state")));
    assert_eq!(before_cold, snapshot_tree(&tmp.path().join("cold")));
    assert_eq!(before_snaps, snapshot_tree(&tmp.path().join("snapshots")));

    srv.shutdown().await;
}

#[tokio::test]
async fn ticks_json_aggregates_over_limit_and_returns_empty_fallback() {
    let tmp = TempDir::new().unwrap();
    let cfg = offline_config(tmp.path(), BASE_TS + 100 * MINUTE);
    let srv = start_server(cfg).await.unwrap();
    let cold_store = srv.state.cold_store.as_ref().unwrap();
    let spec = btc_spec();

    // Seed 2,500 trades within a single 1m bar [BASE_TS, BASE_TS + MINUTE)
    let mut trades = Vec::with_capacity(2500);
    for i in 0..2500usize {
        let ts = BASE_TS + ((i as i64) * 20); // 0ms .. 49_980ms
        let price = if i == 0 {
            Decimal::from(50_000)
        } else if i == 1234 {
            Decimal::from(50_999) // global high
        } else if i == 2111 {
            Decimal::from(49_111) // global low
        } else if i == 2499 {
            Decimal::from(50_250) // final close
        } else {
            Decimal::from(50_100 + (i as i64 % 50))
        };
        trades.push(Trade {
            instrument_id: BTC.to_owned(),
            ts_event: ts,
            ts_available: ts,
            ts_ingested: ts,
            ts_committed: ts,
            revision: 1,
            precision: PrecisionTier::SecondVerified,
            order_uncertain: false,
            trade_id: Uuid::from_u128(10_000 + i as u128),
            side: if i % 2 == 0 {
                TradeSide::Buy
            } else {
                TradeSide::Sell
            },
            price,
            size: Decimal::new(2, 1), // 0.2
            availability: TradeAvailability::Measured,
            archive_day: None,
            archive_index: None,
            sequence: Some(i as i64),
            packet_id: None,
            packet_index: None,
        });
    }
    let _ = partition_month(BASE_TS).unwrap();
    cold_store.publish_trades(&spec, &trades).unwrap();

    let client = reqwest::Client::new();

    // 1. Default limit=2000 triggers bucket aggregation preserving open/high/low/price/volume extremes
    let agg: TicksJsonResponse = client
        .get(format!(
            "{}/api/ticks?instrument={BTC}&kind=trades&start={BASE_TS}&end={}&as_of={}",
            srv.base_url(),
            BASE_TS + MINUTE - 1,
            BASE_TS + MINUTE
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(agg.instrument_id, BTC);
    assert_eq!(agg.kind, TickKind::Trades);
    assert!(agg.aggregated);
    assert!(!agg.ticks.is_empty() && agg.ticks.len() <= 2000);
    assert_eq!(agg.ticks.first().unwrap().open, Some(Decimal::from(50_000)));
    assert_eq!(agg.ticks.last().unwrap().price, Decimal::from(50_250));
    let max_high = agg
        .ticks
        .iter()
        .filter_map(|t| t.high.or(Some(t.price)))
        .max()
        .unwrap();
    let min_low = agg
        .ticks
        .iter()
        .filter_map(|t| t.low.or(Some(t.price)))
        .min()
        .unwrap();
    let total_vol: Decimal = agg.ticks.iter().filter_map(|t| t.volume).sum();
    assert_eq!(max_high, Decimal::from(50_999));
    assert_eq!(min_low, Decimal::from(49_111));
    assert_eq!(total_vol, Decimal::from(500)); // 2500 * 0.2

    // 2. Limit >= 2500 returns raw unaggregated ticks
    let raw: TicksJsonResponse = client
        .get(format!(
            "{}/api/ticks?instrument={BTC}&kind=trades&start={BASE_TS}&end={}&limit=3000",
            srv.base_url(),
            BASE_TS + MINUTE - 1
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!raw.aggregated);
    assert_eq!(raw.ticks.len(), 2500);

    // 3. Empty window returns empty ticks array
    let empty: TicksJsonResponse = client
        .get(format!(
            "{}/api/ticks?instrument={BTC}&kind=trades&start={}&end={}",
            srv.base_url(),
            BASE_TS + 10 * MINUTE,
            BASE_TS + 11 * MINUTE
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!empty.aggregated);
    assert!(empty.ticks.is_empty());

    srv.shutdown().await;
}

#[tokio::test]
async fn runs_endpoints_skip_incomplete_dirs_and_succeed_without_ledger_sqlite() {
    let tmp = TempDir::new().unwrap();
    let cfg = offline_config(tmp.path(), BASE_TS + 100 * MINUTE);
    let srv = start_server(cfg).await.unwrap();

    let runs_dir = tmp.path().join("runs");
    let incomplete_dir = runs_dir.join("run_incomplete");
    let ready_dir = runs_dir.join("run_ready");
    fs::create_dir_all(&incomplete_dir).unwrap();
    fs::create_dir_all(&ready_dir).unwrap();

    // Incomplete dir has parquet & config, NO summary.json
    write_rounds_parquet(&incomplete_dir.join("rounds.parquet"), &[]);
    fs::write(incomplete_dir.join("run-config.json"), "{}").unwrap();

    // Ready dir has summary.json + rounds.parquet + equity.parquet, and NO ledger.sqlite anywhere!
    let round = RunRoundRecord {
        round_id: "rnd_001".to_owned(),
        sub_account_id: "sa-breakout".to_owned(),
        currency: "USDT".to_owned(),
        instrument_id: BTC.to_owned(),
        direction: Direction::Long,
        phase: RunRoundPhase::Closed,
        opened_ts: Some(BASE_TS + MINUTE),
        closed_ts: Some(BASE_TS + 5 * MINUTE),
        entry_price: Some(Decimal::from(50_000)),
        exit_price: Some(Decimal::from(50_500)),
        max_quantity: Decimal::new(1, 1),
        remaining_quantity: Decimal::ZERO,
        stop: Decimal::from(49_500),
        take_profit: vec![Decimal::from(50_500)],
        exit_reason: Some(RunFillReason::Tp),
        raw_exit_reason: Some("take_profit".to_owned()),
        net_pnl: Decimal::from(48),
        fees: Decimal::from(2),
        funding: Decimal::ZERO,
        liquidation_fees: Decimal::ZERO,
        mae: Some(Decimal::from(-15)),
        mfe: Some(Decimal::from(55)),
        fills: vec![
            RunRoundFill {
                event_id: "evt_1".to_owned(),
                ts_event: BASE_TS + MINUTE,
                kind: RunFillKind::EntryFilled,
                side: RunFillSide::Buy,
                price: Decimal::from(50_000),
                quantity: Decimal::new(1, 1),
                reason: RunFillReason::Entry,
                raw_reason: Some("signal".to_owned()),
                fill_tier: Some(RunFillTier::L1),
                realized_pnl: None,
                fee: Some(Decimal::from(1)),
            },
            RunRoundFill {
                event_id: "evt_2".to_owned(),
                ts_event: BASE_TS + 5 * MINUTE,
                kind: RunFillKind::ExitFilled,
                side: RunFillSide::Sell,
                price: Decimal::from(50_500),
                quantity: Decimal::new(1, 1),
                reason: RunFillReason::Tp,
                raw_reason: Some("take_profit".to_owned()),
                fill_tier: Some(RunFillTier::L1),
                realized_pnl: Some(Decimal::from(50)),
                fee: Some(Decimal::from(1)),
            },
        ],
        barrier_changes: vec![RunBarrierChange {
            event_id: "evt_b1".to_owned(),
            ts_event: BASE_TS + 3 * MINUTE,
            kind: RunBarrierChangeKind::BarrierTightened,
            stop: Decimal::from(49_800),
            take_profit: vec![Decimal::from(50_500)],
            reason: Some("breakeven".to_owned()),
            amendment_id: None,
        }],
    };
    write_rounds_parquet(&ready_dir.join("rounds.parquet"), &[round.clone()]);

    let eq_point = RunEquityPoint {
        sub_account_id: "sa-breakout".to_owned(),
        currency: "USDT".to_owned(),
        seq: 1,
        ts_event: BASE_TS + 5 * MINUTE,
        event_id: "evt_2".to_owned(),
        event_kind: "EXIT_FILLED".to_owned(),
        round_id: Some("rnd_001".to_owned()),
        pnl_basis: "booked".to_owned(),
        realized_pnl_delta: Decimal::from(50),
        fee_delta: Decimal::from(2),
        funding_delta: Decimal::ZERO,
        liquidation_fee_delta: Decimal::ZERO,
        net_pnl: Decimal::from(48),
        equity: Decimal::from(10_048),
        peak_pnl: Decimal::from(48),
        drawdown: Decimal::ZERO,
        max_drawdown: Decimal::ZERO,
    };
    write_equity_parquet(&ready_dir.join("equity.parquet"), &[eq_point.clone()]);

    let summary = RunExportSummary {
        schema_version: 1,
        run_id: "run_ready".to_owned(),
        status: RunStatus::Succeeded,
        error: None,
        scheme: "breakout_v1".to_owned(),
        fill_plugin: "l1_trade_sim".to_owned(),
        instrument_id: Some(BTC.to_owned()),
        instruments: vec![BTC.to_owned()],
        timeframe: Some("1m".to_owned()),
        start: Some(BASE_TS),
        end: Some(BASE_TS + 10 * MINUTE),
        signal_bars: Some(10),
        pnl_basis: "booked".to_owned(),
        artifacts: RunExportArtifacts::default(),
        accounts: vec![RunAccountCurrencySummary {
            sub_account_id: "sa-breakout".to_owned(),
            currency: "USDT".to_owned(),
            round_count: 1,
            trade_count: 1,
            net_pnl: Decimal::from(48),
            max_drawdown: Decimal::ZERO,
            fees: Decimal::from(2),
            funding: Decimal::ZERO,
            liquidation_fees: Decimal::ZERO,
            initial_capital: Some(Decimal::from(10_000)),
            final_equity: Some(Decimal::from(10_048)),
        }],
    };
    fs::write(
        ready_dir.join("summary.json"),
        serde_json::to_string_pretty(&summary).unwrap(),
    )
    .unwrap();

    assert!(!ready_dir.join("ledger.sqlite").exists());
    assert!(!ready_dir.join("state").join("ledger.sqlite").exists());

    let client = reqwest::Client::new();

    // 1. GET /api/runs lists ONLY run_ready and skips run_incomplete
    let list: RunListResponse = client
        .get(format!("{}/api/runs", srv.base_url()))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(list.runs.len(), 1);
    assert_eq!(list.runs[0], summary);

    // 2. GET /api/runs/run_incomplete returns 404 NOT_FOUND
    let inc_resp = client
        .get(format!("{}/api/runs/run_incomplete", srv.base_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(inc_resp.status(), 404);
    let err_body: Value = inc_resp.json().await.unwrap();
    assert_eq!(err_body["code"], "NOT_FOUND");

    // 3. GET /api/runs/run_ready, /rounds, /equity succeed without ledger.sqlite
    let got_summary: RunExportSummary = client
        .get(format!("{}/api/runs/run_ready", srv.base_url()))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(got_summary, summary);

    let got_rounds: RunRoundsResponse = client
        .get(format!("{}/api/runs/run_ready/rounds", srv.base_url()))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(got_rounds.run_id, "run_ready");
    assert_eq!(got_rounds.rounds, vec![round]);

    let got_equity: RunEquityResponse = client
        .get(format!("{}/api/runs/run_ready/equity", srv.base_url()))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(got_equity.run_id, "run_ready");
    assert_eq!(got_equity.pnl_basis, "booked");
    assert_eq!(got_equity.points, vec![eq_point]);

    // 4. Reject invalid/traversal run_id
    let bad = client
        .get(format!("{}/api/runs/bad.run", srv.base_url()))
        .send()
        .await
        .unwrap();
    assert_eq!(bad.status(), 422);

    srv.shutdown().await;
}

#[tokio::test]
async fn structures_api_mtfa_adopt_and_feedback_roundtrip() {
    use talaria_contracts::drawings::{DrawingKind, DrawingRecord};
    use talaria_contracts::structures::{
        PriceScaleMode, StructureFamily, StructureFeedbackSummary, StructureGeometry,
        StructureIgnoreList, StructureSet,
    };

    let tmp = TempDir::new().unwrap();
    let now_ms = BASE_TS + 120 * MINUTE;
    let cfg = offline_config(tmp.path(), now_ms);
    let srv = start_server(cfg).await.unwrap();
    let cold_store = srv.state.cold_store.as_ref().unwrap();
    let spec = btc_spec();

    let mut bars_1m = Vec::new();
    for i in 0..80 {
        let ts = BASE_TS + (i as i64) * MINUTE;
        let base = 50_000 + (i as i64) * 30;
        let (o, h, l, c) = if i % 8 == 2 {
            (base - 40, base + 20, base - 250, base)
        } else if i % 8 == 6 {
            (base + 20, base + 280, base - 20, base + 40)
        } else {
            (base, base + 40, base - 40, base + 10)
        };
        bars_1m.push(raw_bar(ts, o, h, l, c));
    }
    cold_store.publish_bars(&spec, "1m", &bars_1m).unwrap();

    let mut bars_15m = Vec::new();
    for i in 0..10 {
        let ts = BASE_TS + (i as i64) * 15 * MINUTE;
        let base = 49_500 + (i as i64) * 200;
        let mut b = raw_bar(ts, base, base + 180, base - 150, base + 80);
        b.timeframe = "15m".to_owned();
        b.ts_available = ts + 15 * MINUTE;
        bars_15m.push(b);
    }
    cold_store.publish_bars(&spec, "15m", &bars_15m).unwrap();

    let client = reqwest::Client::new();

    // 1. Create snapshot on 1m + 15m at cutoff = BASE_TS + 60 * MINUTE
    let cutoff = BASE_TS + 60 * MINUTE;
    let snap: Value = client
        .post(format!("{}/api/snapshots", srv.base_url()))
        .bearer_auth(srv.token())
        .json(&json!({
            "instrument_id": BTC,
            "timeframes": ["1m", "15m"],
            "cutoff_ts": cutoff,
            "mode": "HISTORICAL_PIT"
        }))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    let snap_id = snap["snapshot_id"].as_str().unwrap().to_owned();

    // 2. Query /api/snapshots/{id}/structures with scale=LOG and htf=15m
    let st_set: StructureSet = client
        .get(format!(
            "{}/api/snapshots/{snap_id}/structures?timeframe=1m&scale=LOG&htf=15m&limit=100",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!st_set.structures.is_empty());
    assert!(st_set.structures.iter().all(|s| s.ts_available <= cutoff));
    let trend = st_set
        .structures
        .iter()
        .find(|s| s.family == StructureFamily::Trendline)
        .expect("expected at least one trendline");
    if let StructureGeometry::SlopeLine { scale, .. } = &trend.geometry {
        assert_eq!(*scale, PriceScaleMode::Log);
    } else {
        panic!("expected SlopeLine geometry");
    }
    assert!(
        st_set
            .structures
            .iter()
            .any(|s| s.source_timeframe.as_deref() == Some("15m")),
        "expected MTFA 15m projection"
    );

    // 3. Adopt trendline -> creates DrawingKind::Ray + records feedback
    let drw: DrawingRecord = client
        .post(format!(
            "{}/api/structures/{}/adopt",
            srv.base_url(),
            trend.structure_id
        ))
        .bearer_auth(srv.token())
        .json(&json!({
            "snapshot_id": snap_id,
            "timeframe": "1m"
        }))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(drw.kind, DrawingKind::Ray);

    // 4. Ignore another structure and inspect /api/structures/feedback
    let first_id = st_set.structures[0].structure_id.clone();
    let ignored_list: StructureIgnoreList = client
        .put(format!("{}/api/structures/ignored", srv.base_url()))
        .bearer_auth(srv.token())
        .json(&json!({
            "instrument_id": BTC,
            "structure_id": first_id,
            "ignored": true
        }))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert_eq!(ignored_list.entries.len(), 1);

    let feedback: StructureFeedbackSummary = client
        .get(format!(
            "{}/api/structures/feedback?instrument_id={BTC}",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(feedback.adopted_structure_ids.contains(&trend.structure_id));
    assert!(feedback.ignored_structure_ids.contains(&first_id));

    // 5. Stateless PIT query /api/structures/at
    let at_set: StructureSet = client
        .get(format!(
            "{}/api/structures/at?instrument_id={BTC}&timeframe=1m&as_of={cutoff}&htf=15m",
            srv.base_url()
        ))
        .send()
        .await
        .unwrap()
        .error_for_status()
        .unwrap()
        .json()
        .await
        .unwrap();
    assert!(!at_set.structures.is_empty());
    assert!(at_set.structures.iter().all(|s| s.ts_available <= cutoff));

    srv.shutdown().await;
}
