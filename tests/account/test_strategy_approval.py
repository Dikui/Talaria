"""trading/06、ADR-0015 的模拟盘策略批准门禁。"""

import json

import pytest

from talaria.cli.main import main

pytestmark = [pytest.mark.small, pytest.mark.spec]


@pytest.fixture
def strategy_scheme(tmp_path):
    strategy = tmp_path / "strategy.py"
    strategy.write_text("raise AssertionError('启动门禁前不应执行策略')\n")
    scheme = tmp_path / "scheme.yaml"
    scheme.write_text("""\
scheme: paper
interface_version: 1
sub_accounts: [alpha]
slots: {data: live_ws, strategy: {python: strategy.py}, sizing: example, risk: example, fill: example}
""")
    return strategy, scheme, tmp_path / "data"


def test_paper_requires_registered_python_source(strategy_scheme, capsys):
    _, scheme, data_dir = strategy_scheme
    assert main(["account", "run", str(scheme), "--mode", "paper", "--data-dir", str(data_dir)]) == 2
    assert "策略代码未登记" in capsys.readouterr().err


def test_paper_rejects_an_unapproved_agent_strategy(strategy_scheme, capsys):
    path, scheme, data_dir = strategy_scheme
    assert main(["account", "strategy-register", str(path), "--source", "agent", "--data-dir", str(data_dir)]) == 0
    assert main(["account", "run", str(scheme), "--mode", "paper", "--data-dir", str(data_dir)]) == 2
    assert "agent 策略未批准" in capsys.readouterr().err


def test_editing_approved_code_invalidates_approval(strategy_scheme, capsys):
    path, scheme, data_dir = strategy_scheme
    options = ["--data-dir", str(data_dir)]
    register = ["account", "strategy-register", str(path), "--source", "agent", *options]
    assert main(register) == 0
    digest = json.loads(capsys.readouterr().out)["sha256"]
    approve = ["account", "strategy-approve", str(path), "--sha256", digest, "--approved-by", "交易员", *options]
    assert main(approve) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["source"] == "agent"
    assert record["approval"]["approved_by"] == "交易员"
    assert record["approval"]["approved_at"].endswith("Z")
    assert record["approval"]["sha256"] == digest

    path.write_bytes(path.read_bytes() + b"\n# new version\n")
    assert main(["account", "run", str(scheme), "--mode", "paper", *options]) == 2
    assert "策略代码哈希不符" in capsys.readouterr().err
    assert main(approve) == 2
    assert "批准哈希" in capsys.readouterr().err
    assert main(register) == 0
    assert json.loads(capsys.readouterr().out)["approval"] is None


@pytest.mark.regression
@pytest.mark.parametrize("kind", ["oversize", "fifo"])
def test_registration_only_reads_bounded_regular_files(strategy_scheme, kind):
    import os
    import subprocess
    import sys

    path, _, data_dir = strategy_scheme
    if kind == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        path.write_bytes(b" " * (1024 * 1024 + 1))
    result = subprocess.run(
        [sys.executable, "-m", "talaria", "account", "strategy-register", str(path),
         "--source", "agent", "--data-dir", str(data_dir)],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2
    assert ("普通文件" if kind == "fifo" else "上限") in result.stderr
    assert not (data_dir / "strategy-code.sqlite").exists()


def test_invalid_strategy_is_rejected_before_subaccount_lock(strategy_scheme, capsys):
    path, scheme, data_dir = strategy_scheme
    path.write_bytes(b" " * (1024 * 1024 + 1))
    assert main(["account", "run", str(scheme), "--mode", "paper", "--data-dir", str(data_dir)]) == 2
    assert "上限" in capsys.readouterr().err
    assert not (data_dir / "accounts").exists()


def test_paper_approval_covers_imported_package_contents(strategy_scheme, capsys):
    from talaria.account.strategy_code import StrategyCodeRegistry

    entry, scheme, data_dir = strategy_scheme
    entry.write_text("from helpers.signal import value\n")
    package = entry.parent / "helpers"
    package.mkdir()
    (package / "__init__.py").touch()
    helper = package / "signal.py"
    helper.write_text("value = 1\n")
    registry = StrategyCodeRegistry(data_dir)
    registered = registry.register(entry, "agent")
    registry.approve(entry, registered.sha256, "交易员")
    helper.write_text("value = 2\n")
    assert main(["account", "run", str(scheme), "--mode", "paper", "--data-dir", str(data_dir)]) == 2
    assert "策略代码哈希不符" in capsys.readouterr().err
    changed = registry.register(entry, "agent")
    assert changed.sha256 != registered.sha256 and changed.approval is None


@pytest.mark.parametrize("registered", [True, False])
def test_isolated_strategy_entry_cannot_be_loaded_as_another_slot(strategy_scheme, capsys, registered):
    from talaria.account.strategy_code import StrategyCodeRegistry

    path, scheme, data_dir = strategy_scheme
    if registered:
        StrategyCodeRegistry(data_dir).register(path, "agent")
    scheme.write_text(scheme.read_text().replace("data: live_ws", "data: strategy.py:data"))
    assert main(["account", "run", str(scheme), "--mode", "backtest", "--data-dir", str(data_dir)]) == 2
    assert "隔离策略" in capsys.readouterr().err


def test_backtest_file_in_host_slot_requires_confirmed_source(strategy_scheme, capsys):
    path, scheme, data_dir = strategy_scheme
    scheme.write_text(scheme.read_text().replace("data: live_ws", "data: strategy.py:data").replace(
        "{python: strategy.py}", "example",
    ))
    assert main(["account", "run", str(scheme), "--mode", "backtest", "--data-dir", str(data_dir)]) == 2
    assert "human" in capsys.readouterr().err
    assert not (data_dir / "strategy-code.sqlite").exists()


def test_registration_does_not_follow_entry_outside_strategy_directory(strategy_scheme, tmp_path_factory):
    path, _, data_dir = strategy_scheme
    other = tmp_path_factory.mktemp("outside") / "other.py"
    other.write_text("# another file\n")
    path.unlink()
    path.symlink_to(other)
    assert main(["account", "strategy-register", str(path), "--data-dir", str(data_dir), "--source", "human"]) == 2
    assert not (data_dir / "strategy-code.sqlite").exists()
