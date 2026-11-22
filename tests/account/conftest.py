import subprocess
import sys

import pytest


@pytest.fixture
def register_host_fixtures():
    """由测试交易员登记交易方案选中的当前 Python 插件版本。"""
    from talaria.account.scheme import Slot, load_scheme, python_reference
    from talaria.account.strategy_code import StrategyCodeRegistry

    def register(scheme, data_dir, *, include_strategy=True):
        parsed = load_scheme(scheme)
        records = StrategyCodeRegistry(data_dir)
        files = set()
        slots = (Slot.STRATEGY, Slot.DATA, Slot.SIZING, Slot.RISK, Slot.FILL)
        for slot in slots:
            if slot == Slot.STRATEGY and not include_strategy:
                continue
            reference = parsed.slots.selection(slot).reference
            if ":" in reference:
                path, _ = python_reference(reference, scheme.parent)
                files.add(path)
        for path in sorted(files):
            records.register(path, "human")

    return register


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
    if sys.platform != "darwin":
        pytest.skip("生产策略隔离使用 macOS")
    probe = subprocess.run(
        ["/usr/bin/sandbox-exec", "-p", "(version 1) (allow default)", "/usr/bin/true"],
        capture_output=True, text=True, timeout=5,
    )
    if probe.returncode and "sandbox_apply: Operation not permitted" in probe.stderr:
        pytest.skip("当前环境禁止嵌套 Seatbelt，需在沙箱外 macOS 运行")
    assert probe.returncode == 0, probe.stderr
