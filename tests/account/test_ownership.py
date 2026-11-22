"""trading/01 的多个账户进程争用模拟盘子账户。"""

import select
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.medium, pytest.mark.spec]

START_PROCESS = """
import sys
from pathlib import Path
from talaria.account.host import open_account_process
from talaria.account.ownership import AccountOwnershipError
from talaria.account.scheme import PluginRegistry, Slot, SlotPlugin

sys.path.insert(0, sys.argv[3])
from replay_plugins import fill, risk, sizing
from replay_strategy import plugin

class TestData:
    def events(self):
        return iter(())

registry = PluginRegistry()
for definition in (fill, risk, sizing, plugin, SlotPlugin(Slot.DATA, (1,), lambda *_: TestData())):
    registry.register("example", definition)
try:
    with open_account_process(Path(sys.argv[1]), Path(sys.argv[2]), mode="paper", registry=registry):
        print("started", flush=True)
        sys.stdin.readline()
except AccountOwnershipError as error:
    print(error, file=sys.stderr)
    raise SystemExit(3)
"""


def test_paper_process_cannot_start_with_an_owned_sub_account(tmp_path):
    data_dir = tmp_path / "data"

    def process_args(name, sub_accounts):
        path = tmp_path / f"{name}.yaml"
        path.write_text(f"""\
scheme: {name}
interface_version: 1
sub_accounts: {sub_accounts}
slots: {{data: example, strategy: {{plugin: example, params: {{trigger_price: '11'}}}}, sizing: example, risk: example, fill: example}}
""")
        fixtures = Path(__file__).parent / "fixtures"
        return [sys.executable, "-c", START_PROCESS, str(path), str(data_dir), str(fixtures)]

    with subprocess.Popen(
        process_args("holder", "[beta, gamma]"),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    ) as holder:
        try:
            assert select.select([holder.stdout], [], [], 10)[0], "账户进程启动超时"
            assert holder.stdout.readline().strip() == "started", holder.stderr.read()
            assert (data_dir / "accounts" / "beta" / "owner.lock").is_file()
            contender = subprocess.run(
                process_args("contender", "[alpha, beta]"), input="", capture_output=True, text=True, timeout=10,
            )
            assert contender.returncode == 3, contender.stderr
            assert "beta" in contender.stderr and "owner.lock" in contender.stderr
            # 发生冲突前取得的 alpha 也必须释放。
            independent = subprocess.run(
                process_args("independent", "[alpha]"), input="", capture_output=True, text=True, timeout=10,
            )
            assert independent.returncode == 0, independent.stderr
        finally:
            holder.stdin.close()
            holder.wait(timeout=10)

    restarted = subprocess.run(
        process_args("restarted", "[beta]"), input="", capture_output=True, text=True, timeout=10,
    )
    assert restarted.returncode == 0, restarted.stderr
