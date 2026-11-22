"""trading/06、ADR-0015 的模拟盘策略批准门禁。"""

import json
from hashlib import sha256

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
    digest = sha256(path.read_bytes()).hexdigest()
    register = ["account", "strategy-register", str(path), "--source", "agent", *options]
    approve = ["account", "strategy-approve", str(path), "--sha256", digest, "--approved-by", "交易员", *options]

    assert main(register) == 0
    assert json.loads(capsys.readouterr().out)["sha256"] == digest
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
