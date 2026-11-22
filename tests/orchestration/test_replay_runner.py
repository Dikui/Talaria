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
        "funding": "-0.338561", "max_drawdown": "0.338561",
    }
    assert loss == {
        "net_pnl": "-60.5327346", "fees": "0.3707736",
        "funding": "-0.338561", "max_drawdown": "60.5327346",
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
    captured = capsys.readouterr()
    assert str(scheme) in captured.err and "agent 来源" in captured.err
    assert not captured.out
    assert not (data_dir / "runs").exists()
