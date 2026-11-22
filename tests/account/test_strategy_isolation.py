"""trading/06、ADR-0015 和 ADR-0019 的策略子进程边界。"""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.host import open_account_process
from talaria.account.scheme import PluginRegistry, SchemeError, Slot, SlotPlugin
from talaria.account.strategy_code import StrategyCodeRegistry
from talaria.foundation.bars import RangeBar

pytestmark = [
    pytest.mark.medium, pytest.mark.spec,
    pytest.mark.skipif(sys.platform != "darwin", reason="生产策略隔离使用 macOS"),
]

SOURCE = """\
from decimal import Decimal
import os
import socket
import time

from talaria.account.scheme import Slot, SlotPlugin
from talaria.components import ComponentMeta
from talaria.foundation.collaboration.contracts import SetupInsight

class Strategy:
    meta = ComponentMeta(
        name="example", version="1", inputs=("bars",), output_type="strategy_decision",
        parameters_schema={"properties": {"probe": {"type": "string"}}},
    )
    def __init__(self, context, params):
        assert not hasattr(context, "data_dir")
        self.context = context
        self.probe = params.get("probe")
    def on_bar(self, ctx):
        assert self.context.now() == ctx.now == 60_000
        assert [bar.close for bar in ctx.bars()] == [Decimal(10)]
        assert ctx.bars("5m") == ()
        assert ctx.indicator("SMA", period=Decimal(1))[-1].values["value"] == Decimal(10)
        assert ctx.derivatives("funding_rate") == ()
        assert not ctx.tail_completed
        PROBE
        return SetupInsight(
            direction="LONG", summary="候选", entry_low="10", entry_high="12",
            invalidation_stop="8", take_profit=["12"], max_position="1",
        )

plugin = SlotPlugin(Slot.STRATEGY, (1,), Strategy, Strategy.meta.parameters_schema)
"""
FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture
def paper(tmp_path):
    strategy, scheme, data_dir = tmp_path / "strategy.py", tmp_path / "scheme.yaml", tmp_path / "data"
    registry = PluginRegistry()
    for slot in (Slot.SIZING, Slot.RISK, Slot.FILL):
        registry.register("example", registry.resolve(slot, f"replay_plugins.py:{slot}", 1, FIXTURES))

    def bar(ts, price, **changes):
        return RangeBar.model_validate({
            "ts_event": ts, "ts_available": ts + 60_000, "open": price, "high": price, "low": price,
            "close": price, "volume": "1", "turnover": "1", "precision": "SECOND_VERIFIED",
            "confirmed": True, "revision": 0,
        } | changes)

    first = bar(0, "10")

    def read_bars(*_, **kwargs):
        return [first, bar(0, "99", revision=1, ts_available=60_001), bar(60_000, "20")]

    class Data:
        def events(self):
            source = MarketData(0, read_bars, tail_completed=False)
            yield MarketEvent(60_000, "BTC-USDT-PERP", "1m", first, source=source)

    registry.register("example", SlotPlugin(Slot.DATA, (1,), lambda *_: Data()))

    def start(probe, *, params=None, imports=""):
        strategy.write_text(imports + SOURCE.replace("PROBE", probe))
        scheme.write_text(f"""\
scheme: paper
interface_version: 1
sub_accounts: [example]
slots:
  data: example
  strategy: {{python: strategy.py, params: {json.dumps(params or {})}}}
  sizing: example
  risk: example
  fill: example
""")
        records = StrategyCodeRegistry(data_dir)
        records.register(strategy, "agent")
        records.approve(strategy, sha256(strategy.read_bytes()).hexdigest(), "交易员")
        return open_account_process(scheme, data_dir, mode="paper", registry=registry)

    return start, data_dir


class Ledger:
    def __init__(self):
        self.events = []

    def balances(self, _):
        return {}

    def record(self, events):
        self.events.extend(events)


@pytest.fixture
def without_seatbelt(monkeypatch):
    """以测试替身替换 Seatbelt 调用，仍启动真实 Python 进程并执行资源限制。"""
    from talaria.account.strategy_isolation import worker_command

    def command():
        args = worker_command()
        args[-1] = args[-1].replace("worker.run()", "worker.apply_seatbelt = lambda *_: None; worker.run()")
        return args

    monkeypatch.setattr("talaria.account.strategy_isolation.worker_command", command)


@pytest.fixture(scope="module")
def seatbelt_available():
    """在独立进程中探测当前环境能否启用 Seatbelt。"""
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True, text=True, timeout=5,
    )
    if probe.returncode and "sandbox_apply: Operation not permitted" in probe.stderr:
        pytest.skip("当前环境禁止嵌套 Seatbelt，需在沙箱外 macOS 运行")
    assert probe.returncode == 0, probe.stderr


def test_agent_strategy_uses_host_context_and_cannot_choose_quantity(paper, without_seatbelt):
    start, _ = paper
    ledger = Ledger()
    with start(f"assert os.getpid() != {os.getpid()}") as process:
        process.run(ledger)
    assert [event.reason for event in ledger.events] == ["STRATEGY_QUANTITY_NOT_ALLOWED"]


@pytest.mark.parametrize(("probe", "message"), [
    ("time.sleep(30)", "回调超时"),
    ("bytearray(512 * 1024 * 1024)", "内存上限"),
])
def test_agent_strategy_resource_failure_stops_callback(paper, without_seatbelt, probe, message):
    from talaria.account.execution import AccountProcessError

    start, _ = paper
    ledger = Ledger()
    with pytest.raises(AccountProcessError, match=message), start(probe) as process:
        process.run(ledger)
    assert ledger.events == []


def test_host_enforces_cpu_budget(paper, without_seatbelt, monkeypatch):
    from talaria.account.execution import AccountProcessError
    from talaria.account import strategy_isolation

    monkeypatch.setattr(strategy_isolation, "CPU_SECONDS", 0.4)
    start, _ = paper
    with pytest.raises(AccountProcessError, match="CPU 上限"), start("while True: pass") as process:
        process.run(Ledger())


def test_paper_strategy_parameters_are_validated_at_startup(paper, without_seatbelt):
    start, _ = paper
    with pytest.raises(SchemeError, match=r"params\.probe"), start("pass", params={"probe": 10}):
        pytest.fail("策略参数不符合 Schema")


@pytest.mark.regression
def test_production_worker_imports_dependencies_under_seatbelt(paper, seatbelt_available):
    # trading/06：组件包在启用 Seatbelt 前已加载，重载确保测试实际读取组件包文件。
    start, _ = paper
    ledger = Ledger()
    with start("pass", imports="""\
from importlib import reload
import pydantic.dataclasses
import talaria.components
reload(talaria.components)
""") as process:
        process.run(ledger)
    assert [event.reason for event in ledger.events] == ["STRATEGY_QUANTITY_NOT_ALLOWED"]


def test_production_worker_cannot_read_cold_parquet_or_open_network(paper, seatbelt_available):
    from pyarrow import table
    from pyarrow.parquet import write_table

    start, data_dir = paper
    parquet = data_dir / "cold" / "BTC-USDT-PERP" / "1m" / "bars.parquet"
    parquet.parent.mkdir(parents=True)
    write_table(table({"close": [10]}), parquet)
    ledger = Ledger()
    probe = f"""\
try:
            with open({str(parquet)!r}, "rb") as source:
                source.read(4)
        except PermissionError:
            pass
        else:
            raise AssertionError("子进程读到了冷库")
        try:
            with socket.socket() as connection:
                connection.connect(("127.0.0.1", 9))
        except PermissionError:
            pass
        else:
            raise AssertionError("子进程能访问网络")
"""
    with start(probe) as process:
        process.run(ledger)
    assert [event.reason for event in ledger.events] == ["STRATEGY_QUANTITY_NOT_ALLOWED"]
