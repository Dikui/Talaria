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
for definition in (fill, risk, sizing, plugin, SlotPlugin(Slot.DATA, (2,), lambda *_: TestData())):
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
interface_version: 2
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


@pytest.mark.parametrize("release_during_run", [False, True])
def test_paper_process_cannot_write_after_its_owner_lock_is_released(tmp_path, release_during_run):
    from talaria.account.contracts import ExecutionEvent, MarketEvent
    from talaria.account.host import open_account_process
    from talaria.account.ownership import AccountOwnershipError
    from talaria.account.scheme import PluginRegistry, Slot, SlotPlugin
    from talaria.foundation.bars import RangeBar

    class Data:
        def events(self):
            market = MarketEvent(
                60_000, "BTC-USDT-PERP", "1m",
                RangeBar(
                    ts_event=0, ts_available=60_000, open="1", high="1", low="1",
                    close="1", volume="1", turnover="1", precision="SECOND_VERIFIED",
                    confirmed=True, revision=0,
                ),
            )
            yield market

    class Fill:
        def poll_events(self, now):
            if release_during_run:
                owner.__exit__(None, None, None)
            return [ExecutionEvent(kind="FUNDING_SETTLED", sub_account_id="example", ts_event=now)]

        def on_market(self, *args):
            pass

        submit_plan = tighten_barrier = apply_approved_amendment = intervene_position = set_trading_state = on_market

    class OtherSlots:
        from talaria.components import ComponentMeta

        meta = ComponentMeta(
            name="test_strategy", version="1", inputs=(), parameters_schema={},
            output_type="strategy_decision",
        )

        def on_bar(self, *args):
            pytest.fail("当前行情无策略数据源")

        size = evaluate_candidate = approve_barrier_widening = on_bar

    class Ledger:
        def __init__(self):
            self.events = []

        def record(self, events):
            self.events.extend(events)

        def balances(self, sub_account_id):
            return {}

    registry = PluginRegistry()
    for slot, factory in (
        (Slot.DATA, Data), (Slot.FILL, Fill), (Slot.STRATEGY, OtherSlots),
        (Slot.SIZING, OtherSlots), (Slot.RISK, OtherSlots),
    ):
        registry.register("example", SlotPlugin(slot, (2,), lambda *_, factory=factory: factory()))
    scheme = tmp_path / "paper.yaml"
    scheme.write_text("""\
scheme: paper
interface_version: 2
sub_accounts: [example]
slots: {data: example, strategy: example, sizing: example, risk: example, fill: example}
""")
    ledger = Ledger()
    owner = open_account_process(scheme, tmp_path / "data", mode="paper", registry=registry)
    with owner as process:
        if release_during_run:
            with pytest.raises(AccountOwnershipError, match="owner.lock"):
                process.run(ledger)
    if not release_during_run:
        with pytest.raises(AccountOwnershipError, match="owner.lock"):
            process.run(ledger)
    assert ledger.events == []
