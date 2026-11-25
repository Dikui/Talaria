"""Track B：run_export 原子提交、账本投影一致性、MAE/MFE 与 CLI 补导契约测试。"""

from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil

import pyarrow.parquet as pq
import pytest

from talaria.account.contracts import ExecutionEvent, LedgerPosting, RoundPhase, RoundStatus
from talaria.account.host import RunCompletion, RunConfig, default_registry, run_backtest
from talaria.account.ledger import open_ledger
from talaria.account.run_export import export_run
from talaria.cli.main import main
from talaria.foundation.connector import RawBar
from talaria.foundation.runs import (
    RunBarrierChange,
    RunBarrierChangeKind,
    RunEquityPoint,
    RunEquityResponse,
    RunExportSummary,
    RunFillKind,
    RunFillReason,
    RunListResponse,
    RunRoundFill,
    RunRoundRecord,
    RunRoundsResponse,
)
from talaria.orchestration.replay_runner import summarize_ledger
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]
START = 1_793_491_200_000
MINUTE = 60_000
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def replay_env(tmp_path: Path, register_host_fixtures):
    scheme_dir = tmp_path / "scheme"
    shutil.copytree(FIXTURES, scheme_dir, ignore=shutil.ignore_patterns("__pycache__"))
    data_dir = tmp_path / "data"
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")

    def bar(ts: int, o: str, h: str, l: str, c: str) -> RawBar:
        available = ts + MINUTE
        return RawBar(
            symbol=instrument.symbol,
            timeframe="1m",
            ts_event=ts,
            open=Decimal(o),
            high=Decimal(h),
            low=Decimal(l),
            close=Decimal(c),
            volume=Decimal(10),
            turnover=Decimal(c) * Decimal(10),
            ts_available=available,
            ts_ingested=available,
            ts_committed=available,
            revision=0,
        )

    with closing(ColdStore(data_dir)) as store:
        store.publish(
            ColdBatch(
                instrument,
                "1m",
                [
                    bar(START, "11", "11.5", "10.5", "11"),
                    bar(START + MINUTE, "12", "12.5", "11.5", "12"),
                    bar(START + 2 * MINUTE, "12.7", "14.0", "11.0", "13.0"),
                    bar(START + 3 * MINUTE, "13.0", "15.0", "11.2", "14.0"),
                    bar(START + 4 * MINUTE, "14.0", "15.5", "11.5", "14.5"),
                ],
            )
        )

    scheme = scheme_dir / "scheme.yaml"
    scheme.write_text(
        scheme.read_text(encoding="utf-8").replace(
            f"end: {START + 3 * MINUTE}", f"end: {START + 5 * MINUTE}"
        ),
        encoding="utf-8",
    )
    register_host_fixtures(scheme, data_dir)
    return scheme, data_dir


def test_backtest_auto_exports_atomic_order_and_matches_summarize_ledger(replay_env, monkeypatch):
    scheme, data_dir = replay_env
    replaced_targets: list[str] = []
    real_replace = os.replace

    def tracked_replace(src, dst):
        dst_path = Path(dst)
        if dst_path.name in {"rounds.parquet", "equity.parquet", "summary.json"}:
            assert Path(src).name.startswith(".") and Path(src).name.endswith(".tmp")
            replaced_targets.append(dst_path.name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", tracked_replace)
    run = run_backtest(scheme, data_dir, registry=default_registry())

    # 严格原子提交顺序：rounds.parquet & equity.parquet 先于 summary.json。
    assert replaced_targets == ["rounds.parquet", "equity.parquet", "summary.json"]
    assert not list(run.directory.glob(".*.tmp"))

    summary_path = run.directory / "summary.json"
    rounds_path = run.directory / "rounds.parquet"
    equity_path = run.directory / "equity.parquet"
    assert summary_path.is_file()
    assert rounds_path.is_file()
    assert equity_path.is_file()

    summary = RunExportSummary.model_validate_json(summary_path.read_text(encoding="utf-8"))
    assert summary.run_id == run.run_id
    assert summary.status == "succeeded"
    assert summary.pnl_basis == "booked"
    assert summary.artifacts.rounds_path == "rounds.parquet"
    assert summary.artifacts.equity_path == "equity.parquet"
    assert summary.artifacts.overlays_path is None

    trades, by_currency = summarize_ledger(run.directory / "ledger.sqlite")
    assert len(summary.accounts) == 1
    acct = summary.accounts[0]
    expected_curr = by_currency[acct.currency]
    assert acct.trade_count == trades
    assert acct.net_pnl == expected_curr.net_pnl
    assert acct.max_drawdown == expected_curr.max_drawdown
    assert acct.fees == expected_curr.fees
    assert acct.funding == expected_curr.funding
    assert acct.liquidation_fees == expected_curr.liquidation_fees

    # 验证 Parquet 读回可构造响应模型，且在删除 ledger.sqlite 后依然完整可读。
    (run.directory / "ledger.sqlite").unlink()
    rounds_table = pq.read_table(rounds_path)
    assert rounds_table.num_rows == 1
    row = rounds_table.to_pylist()[0]
    record = RunRoundRecord(
        round_id=row["round_id"],
        sub_account_id=row["sub_account_id"],
        currency=row["currency"],
        instrument_id=row["instrument_id"],
        direction=row["direction"],
        phase=row["phase"],
        opened_ts=row["opened_ts"],
        closed_ts=row["closed_ts"],
        entry_price=row["entry_price"],
        exit_price=row["exit_price"],
        max_quantity=row["max_quantity"],
        remaining_quantity=row["remaining_quantity"],
        stop=row["stop"],
        take_profit=json.loads(row["take_profit_json"]),
        exit_reason=row["exit_reason"],
        raw_exit_reason=row["raw_exit_reason"],
        net_pnl=row["net_pnl"],
        fees=row["fees"],
        funding=row["funding"],
        liquidation_fees=row["liquidation_fees"],
        mae=row["mae"],
        mfe=row["mfe"],
        fills=[RunRoundFill.model_validate(item) for item in json.loads(row["fills_json"])],
        barrier_changes=[
            RunBarrierChange.model_validate(item) for item in json.loads(row["barrier_changes_json"])
        ],
    )
    assert record.fills[0].kind == RunFillKind.ENTRY_FILLED
    assert record.fills[0].reason == RunFillReason.ENTRY
    assert record.fills[0].side == "buy"
    assert record.mae is not None and record.mfe is not None
    RunRoundsResponse(run_id=run.run_id, rounds=[record])

    equity_table = pq.read_table(equity_path)
    equity_points = [RunEquityPoint.model_validate(item) for item in equity_table.to_pylist()]
    RunEquityResponse(run_id=run.run_id, points=equity_points)
    RunListResponse(runs=[summary])


def test_repeat_export_is_byte_identical_and_cli_backfills(replay_env, capsys):
    scheme, data_dir = replay_env
    run = run_backtest(scheme, data_dir, registry=default_registry())

    files = ("rounds.parquet", "equity.parquet", "summary.json")
    first_bytes = {name: (run.directory / name).read_bytes() for name in files}

    for name in files:
        (run.directory / name).unlink()

    assert main(["runs", "export", run.run_id, "--data-dir", str(data_dir)]) == 0
    stdout = capsys.readouterr().out
    parsed = RunExportSummary.model_validate_json(stdout)
    assert parsed.run_id == run.run_id

    second_bytes = {name: (run.directory / name).read_bytes() for name in files}
    assert first_bytes == second_bytes

    # 再次直接调用 export_run 也应逐字节一致。
    export_run(run.directory, data_dir=data_dir)
    third_bytes = {name: (run.directory / name).read_bytes() for name in files}
    assert first_bytes == third_bytes


def test_cli_runs_export_rejects_invalid_run_id(tmp_path: Path, capsys):
    assert main(["runs", "export", "../escape", "--data-dir", str(tmp_path)]) == 2
    assert "无效的运行 ID" in capsys.readouterr().err

    assert main(["runs", "export", "missing_run_123", "--data-dir", str(tmp_path)]) == 2
    assert "找不到运行目录" in capsys.readouterr().err


def test_export_covers_multi_account_reasons_barriers_and_null_mae_without_bars(tmp_path: Path):
    run_dir = tmp_path / "runs" / "synthetic_run_01"
    run_dir.mkdir(parents=True)
    completion = RunCompletion(run_id="synthetic_run_01", status="succeeded", signal_bars=4)
    (run_dir / "run-status.json").write_text(completion.model_dump_json(indent=2) + "\n", encoding="utf-8")

    with open_ledger(run_dir / "ledger.sqlite", "synthetic_run_01", ("acct-a", "acct-b")) as ledger:
        ledger.record(
            [
                ExecutionEvent(
                    kind="ACCOUNT_FUNDED",
                    sub_account_id="acct-a",
                    ts_event=0,
                    postings=(
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount="1000"),
                        LedgerPosting(account="OWNER_CAPITAL", currency="USDT", amount="-1000"),
                    ),
                ),
                ExecutionEvent(
                    kind="ACCOUNT_FUNDED",
                    sub_account_id="acct-b",
                    ts_event=0,
                    postings=(
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDC", amount="2000"),
                        LedgerPosting(account="OWNER_CAPITAL", currency="USDC", amount="-2000"),
                    ),
                ),
                ExecutionEvent(
                    kind="ENTRY_FILLED",
                    sub_account_id="acct-a",
                    ts_event=START,
                    plan_id="p1",
                    quantity="2",
                    price="100",
                    reason="MAKER",
                    fill_tier="bar_1m",
                    take_profit=("120",),
                    position=RoundStatus(
                        round_id="r-long",
                        sub_account_id="acct-a",
                        instrument_id="BTC-USDT-PERP",
                        direction="LONG",
                        quantity="2",
                        stop="90",
                        phase=RoundPhase.ACTIVE_PROTECTED,
                    ),
                    postings=(
                        LedgerPosting(account="POSITION_COST", currency="USDT", amount="200"),
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount="-200"),
                        LedgerPosting(account="EXPENSE_EXECUTION_FEE", currency="USDT", amount="0.2"),
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount="-0.2"),
                    ),
                ),
                ExecutionEvent(
                    kind="BARRIER_TIGHTENED",
                    sub_account_id="acct-a",
                    ts_event=START + MINUTE,
                    reason="TRAILING_STOP",
                    take_profit=("120",),
                    position=RoundStatus(
                        round_id="r-long",
                        sub_account_id="acct-a",
                        instrument_id="BTC-USDT-PERP",
                        direction="LONG",
                        quantity="2",
                        stop="105",
                        phase=RoundPhase.ACTIVE_PROTECTED,
                    ),
                ),
                ExecutionEvent(
                    kind="EXIT_FILLED",
                    sub_account_id="acct-a",
                    ts_event=START + 2 * MINUTE,
                    quantity="2",
                    price="110",
                    reason="TAKE_PROFIT",
                    fill_tier="bar_1m",
                    take_profit=("120",),
                    position=RoundStatus(
                        round_id="r-long",
                        sub_account_id="acct-a",
                        instrument_id="BTC-USDT-PERP",
                        direction="LONG",
                        quantity="0",
                        stop="105",
                        phase=RoundPhase.CLOSED,
                    ),
                    postings=(
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount="220"),
                        LedgerPosting(account="POSITION_COST", currency="USDT", amount="-200"),
                        LedgerPosting(account="REALIZED_PNL", currency="USDT", amount="-20"),
                        LedgerPosting(account="EXPENSE_EXECUTION_FEE", currency="USDT", amount="0.2"),
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount="-0.2"),
                    ),
                ),
                ExecutionEvent(
                    kind="ENTRY_FILLED",
                    sub_account_id="acct-b",
                    ts_event=START + 3 * MINUTE,
                    plan_id="p2",
                    quantity="1",
                    price="50",
                    reason="TAKER",
                    fill_tier="l1",
                    take_profit=("40",),
                    position=RoundStatus(
                        round_id="r-short",
                        sub_account_id="acct-b",
                        instrument_id="BTC-USDT-PERP",
                        direction="SHORT",
                        quantity="1",
                        stop="55",
                        phase=RoundPhase.ACTIVE_PROTECTED,
                    ),
                    postings=(
                        LedgerPosting(account="POSITION_COST", currency="USDC", amount="-50"),
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDC", amount="50"),
                    ),
                ),
                ExecutionEvent(
                    kind="EXIT_FILLED",
                    sub_account_id="acct-b",
                    ts_event=START + 4 * MINUTE,
                    quantity="1",
                    price="52",
                    reason="MAX_HOLDING_TIME",
                    fill_tier="bar_1m",
                    take_profit=("40",),
                    position=RoundStatus(
                        round_id="r-short",
                        sub_account_id="acct-b",
                        instrument_id="BTC-USDT-PERP",
                        direction="SHORT",
                        quantity="0",
                        stop="55",
                        phase=RoundPhase.CLOSED,
                    ),
                    postings=(
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDC", amount="-52"),
                        LedgerPosting(account="POSITION_COST", currency="USDC", amount="50"),
                        LedgerPosting(account="REALIZED_PNL", currency="USDC", amount="2"),
                    ),
                ),
            ]
        )

    summary = export_run(run_dir)
    assert [(a.sub_account_id, a.currency, str(a.net_pnl), str(a.final_equity)) for a in summary.accounts] == [
        ("acct-a", "USDT", "19.6", "1019.6"),
        ("acct-b", "USDC", "-2", "1998"),
    ]

    rows = pq.read_table(run_dir / "rounds.parquet").to_pylist()
    assert len(rows) == 2
    by_id = {row["round_id"]: row for row in rows}

    r_long = by_id["r-long"]
    assert r_long["exit_reason"] == "TP"
    assert r_long["mae"] is None and r_long["mfe"] is None
    long_changes = [RunBarrierChange.model_validate(item) for item in json.loads(r_long["barrier_changes_json"])]
    assert len(long_changes) == 1
    assert long_changes[0].kind == RunBarrierChangeKind.BARRIER_TIGHTENED
    assert long_changes[0].stop == Decimal("105")
    long_fills = [RunRoundFill.model_validate(item) for item in json.loads(r_long["fills_json"])]
    assert [(f.side, f.reason) for f in long_fills] == [("buy", RunFillReason.ENTRY), ("sell", RunFillReason.TP)]

    r_short = by_id["r-short"]
    assert r_short["exit_reason"] == "TIME"
    short_fills = [RunRoundFill.model_validate(item) for item in json.loads(r_short["fills_json"])]
    assert [(f.side, f.reason) for f in short_fills] == [("sell", RunFillReason.ENTRY), ("buy", RunFillReason.TIME)]
