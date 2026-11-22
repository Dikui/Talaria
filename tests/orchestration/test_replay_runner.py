"""backtest-and-research/01 的批量账户进程和账本报告。"""

import json
import subprocess
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.account.host import run_backtest
from talaria.account.ledger import read_journal
from talaria.account.ownership import hold_sub_accounts
from talaria.account.scheme import SchemeError
from talaria.account.strategy_code import StrategyCodeRegistry
from talaria.cli.main import main
from talaria.foundation.connector import RawBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.packs.crypto_perp import CRYPTO_PERP_PACK
from talaria.perception.cold_store import ColdBatch, ColdStore

pytestmark = [pytest.mark.medium, pytest.mark.spec]
START = 1_793_491_200_000
MINUTE = 60_000
STRATEGY = Path(__file__).resolve().parents[1] / "account/fixtures/replay_strategy.py"


@pytest.fixture
def market(tmp_path):
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    starts = (START, START + 60 * MINUTE)
    data_dir = tmp_path / "data"
    with closing(ColdStore(data_dir)) as store:
        for start, second_price, exit_opening in zip(starts, (99, 98), (142, 85), strict=True):
            prices = (100, second_price, 103, 102, exit_opening)
            store.publish(ColdBatch(instrument, "1m", [
                RawBar(
                    symbol=instrument.symbol, timeframe="1m", ts_event=start + i * MINUTE,
                    open=Decimal(price), high=Decimal(price + 1), low=Decimal(price - 1),
                    close=Decimal(price), volume=Decimal(1000), turnover=Decimal(1000 * price),
                    ts_available=start + (i + 1) * MINUTE,
                    ts_ingested=start + (i + 1) * MINUTE, ts_committed=start + (i + 1) * MINUTE,
                )
                for i, price in enumerate(prices)
            ]))
            store.publish_derivatives(instrument, DerivativeKind.MARK_PRICE, [DerivativePoint(
                instrument_id=instrument.instrument_id, kind="mark_price", value="103",
                ts_event=start, ts_available=start, ts_ingested=start, ts_committed=start,
            )])
            settled_at = start + 3 * MINUTE + 10_000
            available = start + 5 * MINUTE + 10_000
            store.publish_derivatives(instrument, DerivativeKind.FUNDING_RATE, [DerivativePoint(
                instrument_id=instrument.instrument_id, kind="funding_rate", value="0.001", settled=True,
                ts_event=settled_at, ts_available=available, ts_ingested=available, ts_committed=available,
            )])
    return data_dir, tuple((start, start + 6 * MINUTE) for start in starts)


def write_scheme(path, name, trigger, start=START, end=START + 6 * MINUTE):
    path.write_text(f"""\
scheme: {name}
interface_version: 2
sub_accounts: [shared]
slots:
  data:
    plugin: parquet_replay
    params: {{instrument: BTC-USDT-PERP, timeframe: 1m, start: {start}, end: {end}}}
  strategy: {{python: strategy.py, params: {{trigger_price: "{trigger}"}}}}
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""", encoding="utf-8")
    return path


def write_strategy(directory, data_dir, source="human"):
    directory.mkdir()
    path = directory / "strategy.py"
    path.write_text(STRATEGY.read_text(encoding="utf-8").replace(
        "entry_high=bar.close + 2", "entry_high=bar.close + 5",
    ).replace(
        "invalidation_stop=bar.close - 2, take_profit=[bar.close + 2]",
        "invalidation_stop=bar.close - 10, take_profit=[bar.close + 35]",
    ), encoding="utf-8")
    if source is not None:
        StrategyCodeRegistry(data_dir).register(path, source)
    return path


def test_batch_journals_match_independent_account_runs_with_paper_lock_held(market, tmp_path, capsys):
    data_dir, windows = market
    strategy = write_strategy(tmp_path / "schemes", data_dir)
    schemes = [
        write_scheme(strategy.with_name(f"{name}.yaml"), name, price)
        for name, price in (("alpha", 100), ("beta", 99))
    ]
    args = ["replay", *map(str, schemes), "--data-dir", str(data_dir), "--workers", "2"]
    for start, end in windows:
        args.extend(("--range", str(start), str(end)))

    with hold_sub_accounts(data_dir, ("shared",)):
        assert main(args) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["strategy_scope"] == "deterministic_only"
    assert report["pnl_basis"] == "booked"
    results = report["runs"]
    assert [(row["scheme"], row["start"], row["end"]) for row in results] == [
        (name, start, end) for name in ("alpha", "beta") for start, end in windows
    ]
    assert len({row["run_id"] for row in results}) == 4
    assert set((data_dir / "runs").iterdir()) == {Path(row["directory"]) for row in results}
    assert not (data_dir / "accounts/shared/ledger.sqlite").exists()

    for row, price in zip(results, (100, 100, 99, 99), strict=True):
        scheme = write_scheme(strategy.with_name("single.yaml"), row["scheme"], price, row["start"], row["end"])
        independent = run_backtest(scheme, data_dir)
        batch_journal = read_journal(Path(row["directory"]) / "ledger.sqlite")
        assert batch_journal == read_journal(independent.directory / "ledger.sqlite")

    assert [row["trade_count"] for row in results] == [1, 1, 1, 0]
    first, loss, _, idle = [row["by_currency"]["USDT"] for row in results]
    # 3.287 手，103.1 入场、142 出场。结算在出场后入账，从盈利峰值扣 0.338561。
    assert first == {
        "net_pnl": "127.22905438", "fees": "0.29668462",
        "funding": "-0.338561", "liquidation_fees": "0", "max_drawdown": "0.338561",
    }
    assert loss == {
        "net_pnl": "-60.5327346", "fees": "0.3707736",
        "funding": "-0.338561", "liquidation_fees": "0", "max_drawdown": "60.5327346",
    }
    assert all(Decimal(value) == 0 for value in idle.values())


@pytest.mark.parametrize("source", ["agent", "unknown", "modified", "named"])
def test_batch_rejects_agent_scheme_before_starting_any_account(tmp_path, monkeypatch, capsys, source):
    data_dir = tmp_path / "data"
    first = write_strategy(tmp_path / "first", data_dir)
    second = write_strategy(
        tmp_path / "second", data_dir, {"agent": "agent", "modified": "human"}.get(source),
    )
    if source == "agent":
        registry = StrategyCodeRegistry(data_dir)
        record = registry.register(second, "agent")
        registry.approve(second, record.sha256, "reviewer")
    elif source == "modified":
        second.write_text(second.read_text(encoding="utf-8") + "\n# new version\n", encoding="utf-8")
    valid = write_scheme(first.with_name("valid.yaml"), "valid", 100)
    invalid = write_scheme(second.with_name("invalid.yaml"), "invalid", 100)
    if source == "named":
        invalid.write_text(invalid.read_text(encoding="utf-8").replace(
            'strategy: {python: strategy.py, params: {trigger_price: "100"}}', "strategy: agent",
        ), encoding="utf-8")

    def unexpected_process(*args, **kwargs):
        pytest.fail("含 agent 的批次应在启动任何账户进程前拒绝")

    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", unexpected_process)
    assert main(["replay", str(valid), str(invalid), "--data-dir", str(data_dir)]) == 2
    captured = capsys.readouterr()
    assert "invalid.yaml" in captured.err and "agent" in captured.err and "确定性" in captured.err
    assert not captured.out
    assert not (data_dir / "runs").exists()


def test_account_process_rechecks_strategy_source_after_batch_preflight(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 100)
    launch = subprocess.run

    # 模拟预检查结束后、账户进程读取文件前修改策略。
    def replace_strategy_then_launch(*args, **kwargs):
        strategy.write_text("raise AssertionError('modified strategy should not run')\n", encoding="utf-8")
        return launch(*args, **kwargs)

    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", replace_strategy_then_launch)
    assert main(["replay", str(scheme), "--data-dir", str(data_dir)]) == 2
    row, = json.loads(capsys.readouterr().out)["runs"]
    assert row["scheme_path"] == str(scheme) and "策略代码哈希" in row["error"]
    assert row["status"] == "failed"
    assert not (Path(row["directory"]) / "ledger.sqlite").exists()


@pytest.mark.regression
def test_batch_and_artifact_share_pinned_bars_and_derivatives(market, tmp_path, monkeypatch, capsys):
    data_dir, windows = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    strategy.write_text(strategy.read_text().replace(
        "        bar = ctx.bars()[-1]", f"""        assert not ctx.derivatives("open_interest")
        bar = ctx.bars()[-1]
        assert bar.ts_event < {START + 5 * MINUTE}""",
    ))
    StrategyCodeRegistry(data_dir).register(strategy, "human")
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 100)
    launch = subprocess.run
    first_launch = True
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    ts = START + 5 * MINUTE

    # 常驻服务在两个实际账户子进程之间追加冷库发布。
    def publish_between_jobs(*args, **kwargs):
        nonlocal first_launch
        result = launch(*args, **kwargs)
        if first_launch:
            first_launch = False
            with closing(ColdStore(data_dir)) as store:
                store.publish(ColdBatch(instrument, "1m", [RawBar(
                    symbol=instrument.symbol, timeframe="1m", ts_event=ts,
                    open=Decimal(110), high=Decimal(110), low=Decimal(110), close=Decimal(110),
                    volume=Decimal(1), turnover=Decimal(110), ts_available=ts + MINUTE,
                    ts_ingested=ts + MINUTE, ts_committed=ts + MINUTE,
                )]))
                store.publish_derivatives(instrument, DerivativeKind.OPEN_INTEREST, [DerivativePoint(
                    instrument_id=instrument.instrument_id, kind="open_interest", value="1",
                    ts_event=START, ts_available=START, ts_ingested=START, ts_committed=START,
                )])
        return result

    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", publish_between_jobs)
    assert main(["replay", str(scheme), str(scheme), "--data-dir", str(data_dir)]) == 0
    rows = json.loads(capsys.readouterr().out)["runs"]
    first, second = [Path(row["directory"]) for row in rows]
    artifact = first / "run-config.json"
    recorded = json.loads(artifact.read_text())["cold_snapshot"]
    assert recorded == json.loads((second / "run-config.json").read_text())["cold_snapshot"]
    assert recorded["bars"]["version"] == recorded["derivatives"]["version"] > 0
    assert len(recorded["bars"]["generations"]) == 1
    assert len(recorded["derivatives"]["generations"]) == 2

    retired = Path(recorded["bars"]["generations"][0]["parquet_path"])
    with closing(ColdStore(data_dir)) as store:
        store.compact([instrument], now_ms=START)
        assert retired not in {generation.parquet_path for generation in store.generations(instrument, "1m")}
    repeated = run_backtest(artifact, None, replay=True)
    assert read_journal(first / "ledger.sqlite") == read_journal(second / "ledger.sqlite")
    assert read_journal(first / "ledger.sqlite") == read_journal(repeated.directory / "ledger.sqlite")

    # 模拟压实之后，原清单引用的文件已被回收。
    missing = data_dir / retired
    missing.unlink()
    with pytest.raises(SchemeError, match="回放清单.*文件缺失") as error:
        run_backtest(artifact, None, replay=True)
    assert missing.name in str(error.value)


@pytest.mark.regression
def test_reregistered_strategy_cannot_replace_the_preflight_code(market, tmp_path, monkeypatch, capsys):
    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    registry = StrategyCodeRegistry(data_dir)
    original = registry.register(strategy, "human").sha256
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 100)
    launch = subprocess.run

    def reregister_then_launch(*args, **kwargs):
        strategy.write_text(strategy.read_text() + "\n# new registered version\n")
        assert registry.register(strategy, "human").sha256 != original
        return launch(*args, **kwargs)

    first = run_backtest(scheme, data_dir)
    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", reregister_then_launch)
    assert main(["replay", str(scheme), "--data-dir", str(data_dir)]) == 2
    row, = json.loads(capsys.readouterr().out)["runs"]
    assert row["status"] == "failed" and "策略代码哈希" in row["error"]

    artifact = first.directory / "run-config.json"
    assert json.loads(artifact.read_text())["file_plugins"]["strategy"]["sha256"] == original
    with pytest.raises(SchemeError, match="策略代码哈希"):
        run_backtest(artifact, None, replay=True)


@pytest.mark.regression
def test_plugins_get_their_own_state_directory_before_assembly(market, tmp_path, capsys):
    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    strategy.write_text(strategy.read_text().replace(
        "    return ReplayStrategy(**params)", """    with (_context.data_dir / "plugin-state.txt").open("x") as state:
        assert _context.state_dir == _context.data_dir
        state.write(_context.run_id)
    return ReplayStrategy(**params)""",
    ))
    StrategyCodeRegistry(data_dir).register(strategy, "human")
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 100)

    assert main(["replay", str(scheme), str(scheme), "--workers", "2", "--data-dir", str(data_dir)]) == 0
    for row in json.loads(capsys.readouterr().out)["runs"]:
        directory = Path(row["directory"])
        assert (directory / "state" / "plugin-state.txt").read_text() == row["run_id"]
    assert not (data_dir / "plugin-state.txt").exists()


@pytest.mark.regression
def test_failed_account_run_marks_partial_ledger(market, tmp_path, capsys):
    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    strategy.write_text(strategy.read_text().replace(
        "        bar = ctx.bars()[-1]", """        raise ValueError("示例策略中断")
        bar = ctx.bars()[-1]""",
    ))
    StrategyCodeRegistry(data_dir).register(strategy, "human")
    scheme = write_scheme(strategy.with_name("bad.yaml"), "bad", 100)

    assert main(["account", "run", str(scheme), "--mode", "backtest", "--data-dir", str(data_dir)]) == 2
    assert "示例策略中断" in capsys.readouterr().err
    run, = (data_dir / "runs").iterdir()
    assert read_journal(run / "ledger.sqlite").entries
    completion = json.loads((run / "run-status.json").read_text())
    assert completion["run_id"] == run.name
    assert completion["status"] == "failed"
    assert "示例策略中断" in completion["error"]


@pytest.mark.regression
@pytest.mark.parametrize("crash", [False, True])
def test_batch_reports_failure_and_preserves_success(market, tmp_path, capsys, crash):
    data_dir, _ = market
    good = write_strategy(tmp_path / "good", data_dir)
    bad = write_strategy(tmp_path / "bad", data_dir)
    failure = '__import__("os")._exit(7)' if crash else 'raise ValueError("示例策略中断")'
    bad.write_text(bad.read_text().replace("        bar = ctx.bars()[-1]", "        " + failure))
    StrategyCodeRegistry(data_dir).register(bad, "human")
    schemes = [write_scheme(path.with_suffix(".yaml"), name, 100) for path, name in ((bad, "bad"), (good, "good"))]

    assert main(["replay", *map(str, schemes), "--workers", "2", "--data-dir", str(data_dir)]) == 2
    failed, succeeded = json.loads(capsys.readouterr().out)["runs"]
    assert failed["status"] == "failed" and failed["by_currency"] is None and failed["trade_count"] is None
    assert ("退出码 7" if crash else "示例策略中断") in failed["error"]
    assert succeeded["status"] == "succeeded" and succeeded["error"] is None
    assert succeeded["trade_count"] == 1 and succeeded["by_currency"]["USDT"]["net_pnl"] == "127.22905438"
    for row in (failed, succeeded):
        completion = json.loads((Path(row["directory"]) / "run-status.json").read_text())
        assert {key: row[key] for key in ("run_id", "status", "error", "signal_bars")} == completion


@pytest.mark.regression
def test_child_stdout_is_not_used_to_find_the_run(market, tmp_path, monkeypatch, capsys):
    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 100)
    launch = subprocess.run
    allocated = []

    def append_stdout_after_child_exits(command, **kwargs):
        dirs = sorted((data_dir / "runs").glob("*/run-config.json"))
        allocated.extend(path.parent.name for path in dirs)
        result = launch(command, **kwargs)
        result.stdout = (result.stdout or "") + "最后一行是日志\n"
        return result

    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", append_stdout_after_child_exits)
    assert main(["replay", str(scheme), "--data-dir", str(data_dir)]) == 0
    row, = json.loads(capsys.readouterr().out)["runs"]
    assert row["status"] == "succeeded"
    assert allocated == [row["run_id"]]


@pytest.mark.regression
def test_booked_costs_separate_execution_fees_funding_and_liquidation(tmp_path):
    from talaria.account.contracts import ExecutionEvent, LedgerPosting
    from talaria.account.ledger import open_ledger
    from talaria.orchestration.replay_runner import summarize_ledger

    path = tmp_path / "ledger.sqlite"
    with open_ledger(path, "run", ("shared",)) as ledger:
        ledger.record([ExecutionEvent(
            kind="EXIT_FILLED", ts_event=START, sub_account_id="shared",
            postings=tuple(LedgerPosting(account=account, currency="USDT", amount=amount)
                           for account, amount in (
                ("REALIZED_PNL", "-10"), ("EXPENSE_EXECUTION_FEE", "1"),
                ("FUNDING", "-2"), ("EXPENSE_LIQUIDATION_FEE", "3"), ("CASH_AVAILABLE", "8"),
            )),
        )])
    _, totals = summarize_ledger(path)
    assert totals["USDT"].model_dump(mode="json") == {
        "net_pnl": "8", "max_drawdown": "0", "fees": "1", "funding": "2", "liquidation_fees": "3",
    }


@pytest.mark.regression
def test_batch_uses_one_snapshot_of_workbench_indicator_defaults(market, tmp_path, monkeypatch, capsys):
    import sqlite3

    from talaria import sdk
    from talaria.perception.indicator_defaults import DEFAULTS_KEY
    from talaria.perception.sqlite import COLLABORATION_STATE_DB

    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    schemes = [write_scheme(strategy.with_name(f"{name}.yaml"), name, 100) for name in ("first", "second")]
    db_path = data_dir / COLLABORATION_STATE_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(db_path)) as db, db:
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO meta VALUES (?, ?)", (DEFAULTS_KEY, '{"SMA":{"period":1}}'))

    read_defaults = sdk.indicator_defaults

    def change_workbench_after_read(*args, **kwargs):
        result = read_defaults(*args, **kwargs)
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute("UPDATE meta SET value = ? WHERE key = ?", ('{"SMA":{"period":5}}', DEFAULTS_KEY))
        return result

    monkeypatch.setattr(sdk, "indicator_defaults", change_workbench_after_read)
    assert main(["replay", *map(str, schemes), "--data-dir", str(data_dir)]) == 0
    rows = json.loads(capsys.readouterr().out)["runs"]
    configs = [json.loads((Path(row["directory"]) / "run-config.json").read_text()) for row in rows]
    assert [config["indicator_defaults"]["SMA"]["period"] for config in configs] == [1, 1]


@pytest.mark.regression
def test_report_distinguishes_empty_window_from_no_trades(market, tmp_path, capsys):
    data_dir, _ = market
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    scheme = write_scheme(strategy.with_name("valid.yaml"), "valid", 999)
    args = ["replay", str(scheme), "--data-dir", str(data_dir),
            "--range", str(START), str(START + 6 * MINUTE),
            "--range", str(START + 10 * MINUTE), str(START + 15 * MINUTE)]
    assert main(args) == 0
    no_trades, empty = json.loads(capsys.readouterr().out)["runs"]
    assert (no_trades["signal_bars"], no_trades["empty_sample"], no_trades["trade_count"]) == (5, False, 0)
    assert no_trades["by_currency"]["USDT"]["net_pnl"] == "0"
    assert (empty["status"], empty["signal_bars"], empty["empty_sample"]) == ("succeeded", 0, True)
    assert empty["by_currency"] is None and empty["trade_count"] is None


def test_l1_replay_compares_same_scheme_on_pinned_ticks(market, tmp_path, monkeypatch, capsys):
    from uuid import UUID

    from talaria.foundation.ticks import TickKind, Trade

    data_dir, _ = market
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    strategy = write_strategy(tmp_path / "scheme", data_dir)
    scheme = write_scheme(strategy.with_name("l1.yaml"), "l1", 100)
    scheme.write_text(scheme.read_text().replace("fill: bar_sim_1m", "fill: l1_trade_sim"))
    opening = START + 2 * MINUTE

    def publish(ts, price, sequence):
        with closing(ColdStore(data_dir)) as store:
            store.publish_ticks(instrument, TickKind.TRADES, [Trade(
                instrument_id=instrument.instrument_id, ts_event=ts, ts_available=ts,
                ts_ingested=ts, ts_committed=ts, trade_id=UUID(int=sequence), sequence=sequence,
                side="Buy", price=Decimal(price), size=Decimal(1000),
            )])

    publish(opening + 5, 103, 1)
    publish(opening + 10, 140, 2)
    launch = subprocess.run

    def publish_after_preflight(*args, **kwargs):
        # 在固定清单之后追加一笔更早的止损。两个子进程都应使用冻结清单。
        publish(opening + 7, 89, 3)
        return launch(*args, **kwargs)

    monkeypatch.setattr("talaria.orchestration.replay_runner.subprocess.run", publish_after_preflight)
    assert main(["replay", str(scheme), "--data-dir", str(data_dir)]) == 0

    report = json.loads(capsys.readouterr().out)
    by_fill = {run["fill_plugin"]: run for run in report["runs"]}
    l1, bar = by_fill["l1_trade_sim"], by_fill["bar_sim_1m"]
    comparison, = report["fill_comparisons"]
    assert comparison["l1_run_id"] == l1["run_id"] and comparison["bar_run_id"] == bar["run_id"]
    assert comparison["trade_count_delta"] == 0
    assert Decimal(comparison["by_currency_delta"]["USDT"]["net_pnl"]) == (
        Decimal(l1["by_currency"]["USDT"]["net_pnl"]) - Decimal(bar["by_currency"]["USDT"]["net_pnl"])
    ) < 0

    path = Path(l1["directory"])
    journal = read_journal(path / "ledger.sqlite")
    entry, exit_fill = [e for e in journal.events if e.kind in {"ENTRY_FILLED", "EXIT_FILLED"}]
    assert (entry.ts_event, exit_fill.ts_event, exit_fill.reason) == (opening + 5, opening + 10, "TAKE_PROFIT")
    assert (entry.fill_tier, exit_fill.fill_tier) == ("trade", "trade")
    snapshot = json.loads((path / "run-config.json").read_text())["cold_snapshot"]
    assert snapshot["ticks"]["version"] == snapshot["bars"]["version"]
    assert len(snapshot["ticks"]["generations"]) == 2
    rerun = run_backtest(path / "run-config.json", None, replay=True)
    assert read_journal(rerun.directory / "ledger.sqlite") == journal


def test_l1_replay_executes_ticks_in_gap_after_last_confirmed_bar(market, tmp_path):
    from uuid import UUID

    from talaria.foundation.ticks import TickKind, Trade

    data_dir, _ = market
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    strategy = write_strategy(tmp_path / "gap", data_dir)
    opening = START + 5 * MINUTE
    scheme = write_scheme(strategy.with_name("gap.yaml"), "gap", 142, end=opening + 20)
    scheme.write_text(scheme.read_text().replace("fill: bar_sim_1m", "fill: l1_trade_sim"))
    with closing(ColdStore(data_dir)) as store:
        store.publish_ticks(instrument, TickKind.TRADES, [
            Trade(instrument_id=instrument.instrument_id, ts_event=opening + sequence * 10,
                  ts_available=opening + sequence * 10, ts_ingested=opening + sequence * 10,
                  ts_committed=opening + sequence * 10, trade_id=UUID(int=sequence), sequence=sequence,
                  side="Buy", price=Decimal(price), size=Decimal(1000))
            for sequence, price in ((1, 145), (2, 180), (3, 120))
        ])
    run = run_backtest(scheme, data_dir)
    fills = [e for e in read_journal(run.directory / "ledger.sqlite").events if e.fill_tier is not None]
    assert [(e.kind, e.ts_event, e.reason) for e in fills] == [
        ("ENTRY_FILLED", opening + 10, "TAKER"), ("EXIT_FILLED", opening + 20, "TAKE_PROFIT"),
    ]


def test_l1_replay_uses_l1_in_quote_only_gap(market, tmp_path):
    from uuid import UUID

    from talaria.foundation.ticks import Quote, TickKind

    data_dir, _ = market
    instrument = CRYPTO_PERP_PACK.instrument("BTC-USDT-PERP")
    strategy = write_strategy(tmp_path / "quotes", data_dir)
    opening = START + 5 * MINUTE
    scheme = write_scheme(strategy.with_name("quotes.yaml"), "quotes", 142, end=opening + 500)
    scheme.write_text(scheme.read_text().replace("fill: bar_sim_1m", "fill: l1_trade_sim"))
    with closing(ColdStore(data_dir)) as store:
        store.publish_ticks(instrument, TickKind.QUOTES, [
            Quote(instrument_id=instrument.instrument_id, ts_event=opening + sequence * 10,
                  ts_available=opening + sequence * 10, ts_ingested=opening + sequence * 10,
                  ts_committed=opening + sequence * 10, connection_id=UUID(int=1), update_id=sequence,
                  bid_price=Decimal(bid), ask_price=Decimal(ask), bid_size=Decimal(1000), ask_size=Decimal(1000))
            for sequence, bid, ask in ((1, 144, 145), (2, 180, 181))
        ])
    run = run_backtest(scheme, data_dir)
    fills = [e for e in read_journal(run.directory / "ledger.sqlite").events if e.fill_tier is not None]
    assert [(e.kind, e.ts_event, e.fill_tier, e.price) for e in fills] == [
        ("ENTRY_FILLED", opening + 10, "l1", Decimal("145.1")),
        ("EXIT_FILLED", opening + 20, "l1", Decimal("180")),
    ]
