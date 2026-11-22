"""trading/04：账户侧以假订阅、冻结快照和定仓边界验证。"""

from contextlib import closing

import pytest

from talaria.foundation.bars import BarStatus, CompactBar
from talaria.foundation.paper import ConfirmedDraft, DraftContent

pytestmark = pytest.mark.small
INSTRUMENT = "BTC-USDT-PERP"


def _draft(**changes):
    content = DraftContent(
        sub_account_id="sa-one", instrument_id=INSTRUMENT, snapshot_id="basis", direction="LONG",
        entry_low="100", entry_high="105", invalidation_stop="98", take_profit=["120"], **changes,
    )
    return ConfirmedDraft(
        draft_id="draft", version=1, content_hash=content.digest(), content=content,
        basis_cutoff_ts=60_000, created_ts=60_000, confirmed_ts=90_000, confirmed_by="trader",
    )


def _bar(low="99", high="103"):
    return CompactBar(
        timestamp=60_000, open="102", high=high, low=low, close="103", volume="2",
        turnover="201", status=BarStatus.NORMAL, ts_available=120_000, precision="SECOND_VERIFIED",
    )


@pytest.mark.parametrize(("low", "high", "expiry", "reason"), [
    ("98", "104", None, "DRAFT_STALE"),
    ("99", "106", None, "DRAFT_STALE"),
    ("99", "104", 119_999, "DRAFT_STALE"),
    ("99", "104", None, None),
])
def test_duplicate_delivery_freezes_and_sizes_only_once(low, high, expiry, reason):
    from talaria.account.contracts import PlanReceipt
    from talaria.account.paper import DraftIntake
    from talaria.foundation.paper import DraftReceipt

    frozen = []
    candidates = []
    recorded = []

    def freeze(draft, cutoff):
        frozen.append((draft.draft_id, cutoff))
        return "fresh", (_bar(low, high),)

    def submit(candidate):
        candidates.append(candidate)
        return PlanReceipt(accepted=True)

    draft = _draft(valid_until=expiry)
    intake = DraftIntake(freeze, submit, recorded.append)
    result = intake.receive(draft, 120_000)
    assert result == DraftReceipt(
        draft_id="draft", version=1, accepted=reason is None, reason=reason, snapshot_id="fresh",
    )
    assert intake.receive(draft, 130_000) == result
    recovered = DraftIntake(freeze, submit, recorded.append, recorded)
    assert recovered.receive(draft, 140_000) == result
    assert frozen == [("draft", 120_000)]
    assert len(candidates) == (1 if reason is None else 0)
    if candidates:
        assert candidates[0].ts_decision == 120_000
        assert candidates[0].deliberation_ms == 30_000
        assert candidates[0].snapshot_id == "fresh"


@pytest.mark.parametrize("reduce", [True, False])
def test_rejected_subscription_applies_configured_policy(tmp_path, reduce):
    from talaria import sdk
    from talaria.account.plugins.live_ws import LiveWS, PaperPulse
    from talaria.account.scheme import PluginContext

    async def denied(*args, **kwargs):
        raise sdk.SubscriptionRejectedError("QUOTA_EXCEEDED", "quota")
        yield

    plugin = LiveWS(
        PluginContext(tmp_path, "paper", 0, lambda: 0),
        {"instrument": INSTRUMENT, "reduce_on_disconnect": reduce, "derivatives": False},
        subscribe=denied, now_ms=lambda: 120_000,
    )
    with closing(plugin.events()) as events:
        event = next(events)
        assert isinstance(event, PaperPulse)
        assert event.ready is False
        assert event.reduce_reason == ("QUOTA_EXCEEDED" if reduce else None)


def test_paper_ledger_keeps_balances_and_event_ids_across_flat_restarts(tmp_path):
    from talaria.account.contracts import ExecutionEvent, LedgerPosting
    from talaria.account.ledger import read_journal
    from talaria.account.ownership import hold_sub_accounts
    from talaria.account.paper_ledger import open_paper_ledger

    accounts = ("sa-one", "sa-two")
    with hold_sub_accounts(tmp_path, accounts) as owner:
        with open_paper_ledger(tmp_path, accounts, owner) as ledger:
            ledger.record([ExecutionEvent(
                kind="ACCOUNT_FUNDED", sub_account_id="sa-one", ts_event=0,
                postings=(
                    LedgerPosting(account="CASH_AVAILABLE", amount="100", currency="USDT"),
                    LedgerPosting(account="OWNER_CAPITAL", amount="-100", currency="USDT"),
                ),
            )])
        with open_paper_ledger(tmp_path, accounts, owner) as ledger:
            assert ledger.balances("sa-one")[("USDT", "CASH_AVAILABLE")] == 100
            assert ledger.balances("sa-two") == {}
            ledger.record([ExecutionEvent(kind="PROBE", sub_account_id="sa-one", ts_event=1)])
    assert [
        row.event_id for row in read_journal(tmp_path / "accounts" / "sa-one" / "ledger.sqlite").events
    ] == ["event-1", "event-2"]
    assert read_journal(tmp_path / "accounts" / "sa-two" / "ledger.sqlite").events == ()


def test_paper_cli_holds_every_owner_lock_and_records_reducing(tmp_path, monkeypatch):
    from talaria.account import host
    from talaria.account.ledger import read_journal
    from talaria.account.ownership import AccountOwnershipError, hold_sub_accounts
    from talaria.account.plugins.live_ws import PaperPulse
    from talaria.account.scheme import INTERFACE_VERSION, Slot, SlotPlugin
    from talaria.cli.main import main
    from talaria.components import ComponentMeta

    class Data:
        def events(self):
            for account in ("sa-one", "sa-two"):
                with pytest.raises(AccountOwnershipError), hold_sub_accounts(tmp_path, [account]):
                    pytest.fail("账户进程运行期间取得了第二把写锁")
            yield PaperPulse(120_000, INSTRUMENT, "1m", None, reduce_reason="SUBSCRIPTION_LOST")

    class Strategy:
        meta = ComponentMeta("quiet", "1", (), {}, "strategy_decision")

        def on_bar(self, ctx):
            pytest.fail("供流状态不能触发策略")

    registry = host.default_registry()
    registry.register("data_fixture", SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), lambda *_: Data()))
    registry.register("quiet", SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), lambda *_: Strategy()))
    monkeypatch.setattr(host, "default_registry", lambda: registry)
    scheme = tmp_path / "paper.yaml"
    scheme.write_text("""\
scheme: paper
interface_version: 2
sub_accounts: [sa-one, sa-two]
slots: {data: data_fixture, strategy: quiet, sizing: r_unit, risk: seven_step, fill: bar_sim_1m}
""")
    assert main(["account", "run", str(scheme), "--mode", "paper", "--data-dir", str(tmp_path)]) == 0
    with hold_sub_accounts(tmp_path, ["sa-one", "sa-two"]):
        for account in ("sa-one", "sa-two"):
            journal = read_journal(tmp_path / "accounts" / account / "ledger.sqlite")
            assert [(event.kind, event.reason) for event in journal.events] == [
                ("ACCOUNT_FUNDED", None), ("TRADING_STATE_CHANGED", "SUBSCRIPTION_LOST:REDUCING"),
            ]


def test_live_bar_stream_reports_a_lost_subscription(tmp_path):
    import asyncio

    from talaria.account.plugins.live_ws import LiveWS, PaperPulse
    from talaria.account.scheme import PluginContext
    from talaria.foundation.bars import RangeBar
    from talaria.foundation.strategy_subscription import StrategyBarEvent

    stop = False
    bar = RangeBar(
        ts_event=60_000, open="102", high="104", low="99", close="103", volume="2", turnover="201",
        ts_available=120_000, precision="SECOND_VERIFIED",
        confirmed=True, revision=0,
    )

    async def feed(*args, on_connection, **kwargs):
        on_connection(True)
        yield StrategyBarEvent(instrument_id=INSTRUMENT, timeframe="1m", epoch="service", seq=1,
                               content_id="close", bar=bar)
        while not stop:
            await asyncio.sleep(0.001)
        on_connection(False)

    plugin = LiveWS(
        PluginContext(tmp_path, "paper", 0, lambda: 0),
        {"instrument": INSTRUMENT, "derivatives": False}, subscribe=feed, now_ms=lambda: 120_000,
    )
    with closing(plugin.events()) as events:
        market = next(events)
        assert market.bar == bar and market.source is not None
        stop = True
        pulse = next(events)
        assert isinstance(pulse, PaperPulse)
        assert pulse.ready is False and pulse.reduce_reason == "SUBSCRIPTION_LOST"
