"""trading/06、ADR-0015 和 ADR-0019 的策略子进程边界。"""

import json
import os
from pathlib import Path
import sys

import pytest

from fixtures.replay_plugins import fill, risk, sizing
from talaria.account.contracts import MarketData, MarketEvent
from talaria.account.host import open_account_process
from talaria.account.scheme import INTERFACE_VERSION, PluginRegistry, SchemeError, Slot, SlotPlugin
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

from talaria.account.scheme import INTERFACE_VERSION, Slot, SlotPlugin
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

plugin = SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), Strategy, Strategy.meta.parameters_schema)
"""


def bar(ts=0, price="10", **changes):
    return RangeBar.model_validate({
        "ts_event": ts, "ts_available": ts + 60_000, "open": price, "high": price, "low": price,
        "close": price, "volume": "1", "turnover": "1", "precision": "SECOND_VERIFIED",
        "confirmed": True, "revision": 0,
    } | changes)



@pytest.fixture
def paper(tmp_path):
    strategy, scheme, data_dir = tmp_path / "strategy.py", tmp_path / "scheme.yaml", tmp_path / "data"
    registry = PluginRegistry()
    for plugin in (sizing, risk, fill):
        registry.register("example", plugin)

    first = bar(0, "10")

    def read_bars(*_, **kwargs):
        return [first, bar(0, "99", revision=1, ts_available=60_001), bar(60_000, "20")]

    source_read = read_bars
    evaluation_time = 60_000
    after_event = None

    class Data:
        def events(self):
            source = MarketData(0, source_read, tail_completed=False)
            yield MarketEvent(evaluation_time, "BTC-USDT-PERP", "1m", first, source=source)
            if after_event:
                after_event()

    registry.register("example", SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), lambda *_: Data()))

    def start(
        probe, *, params=None, imports="", mode="paper", registered=True,
        source=SOURCE, reader=None, now=60_000, after=None, market_bar=None,
    ):
        nonlocal source_read, evaluation_time, after_event, first
        if market_bar is not None:
            first = market_bar
        after_event = after
        source_read = reader or read_bars
        evaluation_time = now
        strategy.write_text(imports + source.replace("PROBE", probe))
        scheme.write_text(f"""\
scheme: paper
interface_version: {INTERFACE_VERSION}
sub_accounts: [example]
slots:
  data: example
  strategy: {{python: strategy.py, params: {json.dumps(params or {})}}}
  sizing: example
  risk: example
  fill: example
""")
        records = StrategyCodeRegistry(data_dir)
        if registered:
            record = records.register(strategy, "agent")
            if mode == "paper":
                records.approve(strategy, record.sha256, "交易员")
        return open_account_process(scheme, data_dir, mode=mode, registry=registry)

    return start, data_dir


class Ledger:
    def __init__(self):
        self.events = []

    def balances(self, _):
        return {}

    def record(self, events):
        self.events.extend(events)


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


def test_worker_imports_the_approved_copy_of_local_module(paper, without_seatbelt, tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("value = 1\n")
    start, _ = paper
    with start("from helper import value\n        assert value == 1") as process:
        helper.write_text("value = 2\n")
        process.run(Ledger())


@pytest.mark.parametrize("registered", [True, False])
def test_backtest_uses_a_worker_without_human_confirmation(paper, without_seatbelt, registered):
    start, data_dir = paper
    imports = f"import os\nassert os.getpid() != {os.getpid()}\n"
    with start('assert self.context.mode == "backtest"', mode="backtest", registered=registered, imports=imports) as process:
        process.run(Ledger())
    assert (data_dir / "strategy-code.sqlite").exists() is registered


def test_worker_read_grants_exclude_ambient_import_roots(tmp_path, monkeypatch):
    from talaria.account.strategy_isolation import _worker_read_dirs
    import talaria

    quotes = tmp_path / "quotes"
    quotes.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(quotes))
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    grants = [Path(item) for item in _worker_read_dirs(snapshot)]
    package = Path(talaria.__file__).resolve().parent
    assert snapshot in grants and package in grants
    assert not any(quotes.is_relative_to(path) or package.parent.is_relative_to(path) for path in grants)


@pytest.mark.parametrize("mode", ["backtest", "paper"])
def test_seatbelt_denies_other_quotes_and_protects_registration(paper, seatbelt_available, tmp_path, tmp_path_factory, monkeypatch, mode):
    start, data_dir = paper
    quotes = tmp_path_factory.mktemp("other-quotes")
    other = quotes / "history.parquet"
    other.write_bytes(b"fixture")
    neighbor = tmp_path / "local.parquet"
    neighbor.touch()
    monkeypatch.setenv("PYTHONPATH", str(quotes))
    probe = f"""\
for filename in ({str(other)!r}, {str(neighbor)!r}):
            try:
                open(filename, 'rb').close()
            except PermissionError:
                pass
            else:
                raise AssertionError('行情文件可读')
        for filename in ({str(data_dir / "strategy-code.sqlite")!r}, {str(data_dir / "new")!r}):
            try:
                fd = os.open(filename, os.O_WRONLY | os.O_CREAT)
            except PermissionError:
                pass
            else:
                os.close(fd)
                raise AssertionError('数据目录可写')"""
    with start(probe, mode=mode) as process:
        process.run(Ledger())


@pytest.mark.parametrize("value", ["valid", "invalid"])
def test_strategy_metadata_pattern_is_checked_only_in_worker(paper, without_seatbelt, monkeypatch, value):
    import re

    pattern = "^valid$"
    search = re.search

    def host_search(expression, *args, **kwargs):
        assert expression != pattern, "宿主执行了策略提供的 pattern"
        return search(expression, *args, **kwargs)

    monkeypatch.setattr(re, "search", host_search)
    source = SOURCE.replace('{"type": "string"}', '{"type": "string", "pattern": "^valid$"}').replace(
        "Strategy, Strategy.meta.parameters_schema)", "Strategy)",
    )
    start, _ = paper
    manager = start("pass", source=source, params={"probe": value})
    if value == "invalid":
        with pytest.raises(SchemeError, match=r"params\.probe.*pattern"), manager:
            pytest.fail("策略参数校验失败时不能启动")
    else:
        with manager as process:
            process.run(Ledger())


def test_large_ctx_query_is_rejected_before_reading_market_data(paper, without_seatbelt):
    calls = []

    def reader(instrument, timeframe, **kwargs):
        calls.append(timeframe)
        assert timeframe == "1m", "过大范围进入了底层读取器"
        return [bar()]

    source = SOURCE[:SOURCE.index("        assert self.context.now")] + SOURCE[SOURCE.index("        PROBE"):]
    start, _ = paper
    with pytest.raises(ValueError, match="取数.*上限"), start(
        'ctx.bars("5m")', source=source, reader=reader, now=60_000 * 1_000_000,
    ) as process:
        process.run(Ledger())
    assert "5m" not in calls


def test_host_does_not_wait_for_blocked_context_read(paper, without_seatbelt, monkeypatch):
    from threading import Event
    from time import monotonic
    from talaria.account import strategy_isolation

    release = Event()
    entered = Event()

    def read(_, timeframe, **kwargs):
        if timeframe == "5m":
            entered.set()
            release.wait(2)
            return []
        return [bar()]

    start, _ = paper
    monkeypatch.setattr(strategy_isolation, "CALLBACK_SECONDS", 0.3)
    source = SOURCE.replace("ctx.now == 60_000", "ctx.now == 300_000").replace('assert ctx.bars("5m") == ()', 'ctx.bars("5m")')
    with start("pass", reader=read, source=source, now=300_000) as process:
        begin = monotonic()
        try:
            with pytest.raises(ValueError, match="超时"):
                process.run(Ledger())
            assert entered.is_set() and monotonic() - begin < 1.5
        finally:
            release.set()


def test_ctx_reply_enforces_actual_encoded_byte_budget(paper, without_seatbelt, monkeypatch):
    from talaria.account import strategy_reads

    monkeypatch.setattr(strategy_reads, "MAX_CONTEXT_BYTES", 1024)
    wide = bar(turnover="1" * 1500)
    start, _ = paper
    with pytest.raises(ValueError, match="取数.*字节上限"), start(
        "pass", reader=lambda *args, **kwargs: [wide], market_bar=wide,
    ) as process:
        process.run(Ledger())


@pytest.mark.parametrize("fault", ["missing", "mismatch", "duplicate"])
def test_host_terminates_on_invalid_callback_reply(paper, monkeypatch, fault):
    from talaria.account.execution import AccountProcessError
    from talaria.account.strategy_isolation import worker_command

    # 替身只发送中性决定，模拟缺失、过期或重复的回调回复。
    def command():
        args = worker_command()
        peer = f"""
from talaria.account.strategy_ipc import StrategyPipe
pipe = StrategyPipe(0, 1, receive_limit=16777216, send_limit=65536)
pipe.receive()
pipe.send({{"isolated": True}})
pipe.receive()
pipe.send({{"ready": True}})
request = pipe.receive()
reply = {{"decision": "Abstain", "data": {{"reason_code": "EVIDENCE_CONFLICT", "message": "fixture"}}}}
if {fault!r} != "missing":
    reply["id"] = "another-callback" if {fault!r} == "mismatch" else request.get("id")
pipe.send(reply)
if {fault!r} == "duplicate":
    pipe.send(reply)
pipe.receive()
"""
        args[-1] = args[-1].replace("worker.run()", peer)
        return args

    monkeypatch.setattr("talaria.account.strategy_isolation.worker_command", command)
    start, _ = paper
    with pytest.raises(AccountProcessError, match="协议"), start("pass") as process:
        process.run(Ledger())


@pytest.fixture
def children(monkeypatch):
    import subprocess

    spawned = []
    popen = subprocess.Popen

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", capture)
    return spawned


def test_worker_exit_interrupts_idle_market_wait_and_releases_owner(paper, without_seatbelt, children):
    from threading import Event
    from time import monotonic
    from talaria.account.ownership import hold_sub_accounts

    release = Event()

    def wait_for_market():
        children[0].kill()
        release.wait(2)

    start, data_dir = paper
    try:
        with pytest.raises(ValueError, match="提前退出"), start("pass", after=wait_for_market) as process:
            begin = monotonic()
            process.run(Ledger())
        assert monotonic() - begin < 1.5
        with hold_sub_accounts(data_dir, ("example",)):
            pass
    finally:
        release.set()


def test_host_enforces_resident_memory_limit(paper, without_seatbelt, children, monkeypatch):
    from talaria.account import strategy_isolation
    from talaria.account.strategy_limits import task_usage

    start, _ = paper
    with start('self.buffer = bytearray(b"x") * (32 * 1024 * 1024)\n        time.sleep(0.3)') as process:
        limit = task_usage(children[0].pid).resident + 8 * 1024 * 1024
        monkeypatch.setattr(strategy_isolation, "MAX_RSS_BYTES", limit)
        with pytest.raises(ValueError, match="驻留内存上限"):
            process.run(Ledger())


def test_worker_bootstrap_cpu_is_excluded_from_strategy_budget(paper, without_seatbelt, monkeypatch):
    from talaria.account import strategy_isolation

    original = strategy_isolation.worker_command

    def slow_bootstrap():
        args = original()
        prefix = "from time import process_time\nend = process_time() + 0.45\nwhile process_time() < end: pass\n"
        args[-1] = prefix + args[-1]
        return args

    monkeypatch.setattr(strategy_isolation, "CPU_SECONDS", 0.25)
    monkeypatch.setattr(strategy_isolation, "worker_command", slow_bootstrap)
    start, _ = paper
    with start("pass") as process:
        process.run(Ledger())


def test_venv_only_grants_its_installed_packages(tmp_path):
    import sysconfig
    from talaria.account.strategy_isolation import _worker_read_dirs

    if sys.prefix == sys.base_prefix:
        pytest.skip("需要 venv")
    packages = Path(sysconfig.get_path("purelib")).resolve()
    assert not any(packages.parent.is_relative_to(root) for root in _worker_read_dirs(tmp_path))


def test_worker_exit_cancels_an_inflight_data_reader(paper, without_seatbelt, children):
    from threading import Event
    from talaria.account import strategy_reads

    stopped = Event()

    def read(_, timeframe, **kwargs):
        if timeframe == "5m":
            cancel = strategy_reads.read_cancellation()
            children[0].kill()
            if cancel.wait(2):
                stopped.set()
            return []
        return [bar()]

    start, _ = paper
    source = SOURCE.replace("ctx.now == 60_000", "ctx.now == 300_000")
    with start("pass", reader=read, source=source, now=300_000) as process:
        with pytest.raises(ValueError, match="提前退出"):
            process.run(Ledger())
        assert stopped.wait(0.5)


def test_market_check_for_isolated_strategy_reads_only_signal_bar(paper, without_seatbelt):
    start, _ = paper
    seen = []
    ts = 60_000 * 20_000
    signal = bar(ts)

    def read(*_, start, end, **kwargs):
        seen.append((start, end))
        return [signal]

    source = SOURCE[:SOURCE.index("        assert self.context.now")] + SOURCE[SOURCE.index("        PROBE"):]
    with start("pass", source=source, reader=read, now=signal.ts_available, market_bar=signal) as process:
        process.run(Ledger())
    assert seen == [(ts, ts)]
