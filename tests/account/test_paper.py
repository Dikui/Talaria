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


def test_owner_token_is_scoped_to_locked_directory_and_accounts(tmp_path):
    from talaria.account.ownership import AccountOwnershipError, hold_sub_accounts
    from talaria.account.paper_ledger import open_paper_ledger

    with hold_sub_accounts(tmp_path, ["alpha"]) as owner:
        for root, account in [(tmp_path, "beta"), (tmp_path / "other", "alpha")]:
            with pytest.raises(AccountOwnershipError), open_paper_ledger(root, [account], owner):
                pass


def test_owner_can_finish_interrupted_empty_ledger_initialization(tmp_path):
    from talaria.account.ownership import hold_sub_accounts
    from talaria.account.paper_ledger import open_paper_ledger

    with hold_sub_accounts(tmp_path, ["alpha"]) as owner:
        (tmp_path / "accounts/alpha/ledger.sqlite").touch()
        with open_paper_ledger(tmp_path, ["alpha"], owner) as ledger:
            assert ledger.balances("alpha") == {}


def live_bar(slot, *, revision=0, close="103"):
    from talaria.foundation.bars import RangeBar
    from talaria.foundation.strategy_subscription import StrategyBarEvent
    bar = RangeBar(ts_event=slot, ts_available=slot + 60_000, open="102", high="105", low="99",
                   close=close, volume="2", turnover="201", confirmed=True, revision=revision,
                   precision="SECOND_VERIFIED")
    return StrategyBarEvent(instrument_id=INSTRUMENT, timeframe="1m", epoch="service",
                            seq=slot // 60_000 + 1, content_id=f"{slot}-{revision}", bar=bar)


def test_minutes_wait_for_late_first_confirmation_and_ignore_processed_revisions(tmp_path):
    import asyncio
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext

    async def feed(*args, on_connection, **kwargs):
        on_connection(True)
        for event in (live_bar(0), live_bar(120_000), live_bar(60_000), live_bar(0, revision=1), live_bar(180_000)):
            yield event
        await asyncio.Event().wait()

    plugin = LiveWS(PluginContext(tmp_path, "paper", 0, lambda: 0),
                    {"instrument": INSTRUMENT, "derivatives": False}, subscribe=feed, now_ms=lambda: 240_000)
    with closing(plugin.events()) as events:
        minutes = (market.bar.ts_event for market in events if market.bar is not None)
        assert [next(minutes) for _ in range(3)] == [0, 60_000, 120_000]


def test_delayed_signal_reads_first_confirmed_version_at_its_available_time(tmp_path, monkeypatch):
    import asyncio
    from talaria import sdk
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext

    first = live_bar(60_000)
    async def feed(*args, on_connection, **kwargs):
        on_connection(True)
        yield first
        await asyncio.Event().wait()

    monkeypatch.setattr(sdk, "bars", lambda *args, **kwargs: [live_bar(60_000, revision=1, close="104").bar])
    plugin = LiveWS(PluginContext(tmp_path, "paper", 0, lambda: 0),
                    {"instrument": INSTRUMENT, "derivatives": False}, subscribe=feed, now_ms=lambda: 129_000)
    with closing(plugin.events()) as events:
        market = next(events)
        assert market.as_of == 129_000
        assert market.source.signal_as_of == 120_000
        assert market.source.read_bars(INSTRUMENT, "1m", start=0, end=60_000, as_of=120_000)[-1] == first.bar


def test_quota_retry_resumes_delivery(tmp_path):
    import asyncio
    from talaria import sdk
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext

    attempts = []
    async def feed(*args, on_connection, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise sdk.SubscriptionRejectedError("SUBSCRIPTION_QUOTA", "quota")
        on_connection(True)
        yield live_bar(120_000)
        await asyncio.Event().wait()

    plugin = LiveWS(PluginContext(tmp_path, "paper", 0, lambda: 0),
                    {"instrument": INSTRUMENT, "derivatives": False}, subscribe=feed, now_ms=lambda: 120_000)
    with closing(plugin.events()) as events:
        assert next(events).reduce_reason == "SUBSCRIPTION_QUOTA"
        assert next(events).bar == live_bar(120_000).bar


def test_visible_funding_prediction_arrives_before_next_execution_minute(tmp_path):
    import asyncio
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext
    from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
    from talaria.foundation.strategy_subscription import StrategyDerivativeEvent

    async def feed(*args, on_connection, **kwargs):
        on_connection(True)
        yield live_bar(0)
        await asyncio.Event().wait()

    point = DerivativePoint(instrument_id=INSTRUMENT, kind=DerivativeKind.FUNDING_RATE, value=".01",
                            ts_event=61_000, ts_available=61_000, ts_ingested=61_000, ts_committed=61_000, settled=False)
    async def derivatives(instrument, kind, *, on_connection, **kwargs):
        on_connection(True)
        if kind == point.kind:
            yield StrategyDerivativeEvent(epoch="service", seq=1, point=point, content_id="rate")
        await asyncio.Event().wait()
    plugin = LiveWS(PluginContext(tmp_path, "paper", 0, lambda: 0), {"instrument": INSTRUMENT},
                    subscribe=feed, subscribe_derivatives=derivatives, now_ms=lambda: 61_000)
    with closing(plugin.events()) as events:
        assert next(events).bar is not None
        assert next(events).derivative_points == (point,)


def test_missing_partial_minute_is_retryable_and_current_price_can_expire_draft():
    from talaria.account.contracts import PlanReceipt
    from talaria.account.paper import DraftIntake
    rows = [_bar()]
    recorded = []
    intake = DraftIntake(lambda draft, cutoff: ("fresh", rows), lambda _: PlanReceipt(accepted=True), recorded.append)
    assert intake.receive(_draft(), 135_000) is None
    rows.append(_bar(high="106").model_copy(update={"timestamp": 120_000, "ts_available": 135_000}))
    assert intake.receive(_draft(), 135_000).reason == "DRAFT_STALE"


def test_partial_first_minute_requires_only_post_creation_extrema(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from decimal import Decimal
    from talaria import sdk
    from talaria.account.paper import PaperInbox
    from talaria.foundation.paper import DraftEvidence
    import httpx

    bar = _bar(low="97", high="103")
    evidence = DraftEvidence(instrument_id=INSTRUMENT, cutoff_ts=120_000, snapshot_id="fresh", bars=[bar])
    monkeypatch.setattr(PaperInbox, "_request", lambda *args, **kwargs: httpx.Response(200, json=evidence.model_dump(mode="json")))
    trades = [SimpleNamespace(ts_event=65_000, price=Decimal(97), size=Decimal(1)),
              SimpleNamespace(ts_event=92_000, price=Decimal(103), size=Decimal(1))]
    monkeypatch.setattr(sdk, "trades", lambda *a, **kw: trades)
    draft = _draft().model_copy(update={"basis_cutoff_ts": 80_000, "created_ts": 80_000})
    with httpx.Client() as client:
        _, rows = PaperInbox(tmp_path, ["sa-one"], client).freeze(draft, 120_000)
    assert min(row.low for row in rows) == 103


def test_interrupted_draft_returns_real_result_after_redelivery():
    from talaria.account.contracts import PlanReceipt
    from talaria.account.paper import DraftIntake
    recorded = []
    def interrupt(candidate):
        raise InterruptedError()
    freeze = lambda *_: ("fresh", [_bar()])
    with pytest.raises(InterruptedError):
        DraftIntake(freeze, interrupt, recorded.append).receive(_draft(), 120_000)
    assert all(event.draft_receipt is None for event in recorded)
    recovered = DraftIntake(freeze, lambda candidate: PlanReceipt(accepted=True), recorded.append, recorded)
    assert recovered.receive(_draft(), 120_000).accepted


def test_add_on_confirmation_binds_round_and_all_inherited_barriers():
    from talaria.account.contracts import PlanReceipt
    from talaria.account.paper import DraftIntake
    barriers = dict(stop="98", take_profit=["120"], max_holding_ms=3600000, trailing_stop=None)
    draft = _draft(round_id="round-one", inherited_barriers=barriers)
    assert draft.content.digest() != _draft().content.digest()
    routed = []
    def submit(candidate):
        routed.append(candidate)
        return PlanReceipt(accepted=True)
    DraftIntake(lambda *_: ("fresh", [_bar()]), submit, lambda _: None).receive(draft, 120_000)
    assert routed[0].round_id == "round-one"
    assert routed[0].inherited_barriers.max_holding_ms == 3600000


def test_checkpoint_is_atomic_with_ledger_and_risk_state(tmp_path):
    from talaria.account.contracts import ExecutionEvent
    from talaria.account.ownership import hold_sub_accounts
    from talaria.account.paper_ledger import open_paper_ledger
    with hold_sub_accounts(tmp_path, ["sa-one"]) as owner:
        with open_paper_ledger(tmp_path, ["sa-one"], owner) as ledger:
            with pytest.raises(InterruptedError), ledger.transaction("sa-one"):
                ledger.record([ExecutionEvent(kind="PROBE", sub_account_id="sa-one", ts_event=1)])
                ledger.save_checkpoint("sa-one", {"minute": 60000})
                raise InterruptedError()
        with open_paper_ledger(tmp_path, ["sa-one"], owner) as ledger:
            assert not ledger.previous
            assert ledger.checkpoint("sa-one") is None


def test_feed_checkpoint_keeps_gap_buffer_and_consumed_channel_cursor(tmp_path):
    import asyncio
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext

    calls = []
    async def feed(*args, on_connection, **kwargs):
        calls.append(kwargs)
        on_connection(True)
        if len(calls) == 1:
            yield live_bar(0)
            yield live_bar(120_000)
        else:
            yield live_bar(60_000).model_copy(update={"seq": 4})
        await asyncio.Event().wait()

    args = PluginContext(tmp_path, "paper", 0, lambda: 0), {"instrument": INSTRUMENT, "derivatives": False}
    first = LiveWS(*args, subscribe=feed, now_ms=lambda: 180_000)
    with closing(first.events()) as events:
        assert next(events).bar.ts_event == 0
        assert next(events).reduce_reason == "CONFIRMED_MINUTE_GAP"
        state = first.checkpoint()
    recovered = LiveWS(*args, subscribe=feed, now_ms=lambda: 180_000)
    recovered.restore_checkpoint(state)
    with closing(recovered.events()) as events:
        assert [next(events).bar.ts_event for _ in range(2)] == [60_000, 120_000]
    assert calls[1]["after"].seq == 3
    assert calls[1]["resume_from"] == 60_000


@pytest.mark.parametrize("interruption", ["before_commit", "lost_ack"])
def test_crash_between_acceptance_and_ack_recovers_one_plan_and_pending_entry(tmp_path, interruption):
    import sqlite3
    from types import SimpleNamespace
    from decimal import Decimal
    from talaria.account.contracts import MarketEvent
    from talaria.account.host import default_registry, open_account_process
    from talaria.account.ledger import read_journal
    from talaria.account.paper_ledger import open_paper_ledger
    from talaria.account.plugins.bar_sim_1m import BarSim1m
    from talaria.account.plugins.live_ws import PaperPulse
    from talaria.account.scheme import SlotPlugin, Slot
    from talaria.components import ComponentMeta
    from talaria.foundation.derivatives import DerivativePoint

    pulse = PaperPulse(120_000, INSTRUMENT, "1m", None, ready=True)
    price = DerivativePoint(kind="mark_price", instrument_id=INSTRUMENT, ts_event=120_000,
                            ts_available=120_000, ts_ingested=120_000, ts_committed=120_000, value="102")
    stream = [MarketEvent(120_000, INSTRUMENT, "1m", None, derivative_points=(price,)), pulse]
    registry = default_registry()
    def events():
        for item in stream:
            if isinstance(item, InterruptedError):
                raise item
            yield item

    registry.register("feed", SlotPlugin(Slot.DATA, (2,), lambda *_: SimpleNamespace(events=events)))
    registry.register("sim", SlotPlugin(Slot.FILL, (2,), lambda ctx, params: BarSim1m(ctx, params, now_ms=lambda: 0)))
    registry.register("idle", SlotPlugin(Slot.STRATEGY, (2,), lambda *_: SimpleNamespace(
        meta=ComponentMeta("idle", "1", (), {}, "strategy_decision"), on_bar=lambda ctx: pytest.fail("无信号柱"))))
    scheme = tmp_path / "scheme.yaml"
    scheme.write_text("scheme: test\ninterface_version: 2\nsub_accounts: [sa-one]\n"
                      "slots: {data: feed, strategy: idle, sizing: r_unit, risk: seven_step, fill: sim}")
    receipts = []
    journal = tmp_path / "accounts/sa-one/ledger.sqlite"

    inject_failure = interruption == "before_commit"
    class Inbox:
        def claim(self, account, instrument, now):
            nonlocal inject_failure
            if inject_failure:
                with closing(sqlite3.connect(journal)) as db:
                    db.execute("""CREATE TRIGGER interrupt BEFORE INSERT ON execution_events
                                  WHEN json_extract(NEW.payload, '$.kind') = 'DRAFT_RESOLVED'
                                  BEGIN SELECT RAISE(ABORT, 'before commit'); END""")
                inject_failure = False
            return _draft()
        def freeze(self, draft, cutoff):
            assert read_journal(journal).events[-1].kind == "DRAFT_PENDING"
            return "fresh", [_bar()]
        def acknowledge(self, account, receipt):
            assert read_journal(journal).events[-1].kind == "DRAFT_RESOLVED"
            receipts.append(receipt)
            if interruption == "lost_ack" and len(receipts) == 1:
                raise InterruptedError("after commit")

    def run():
        with open_account_process(scheme, tmp_path, mode="paper", registry=registry) as process:
            with open_paper_ledger(tmp_path, ["sa-one"], process._ownership) as ledger:
                process.run(ledger, inbox=Inbox())

    with pytest.raises(InterruptedError if interruption == "lost_ack" else sqlite3.IntegrityError):
        run()
    if interruption == "before_commit":
        assert not receipts
        assert read_journal(journal).events[-1].kind == "DRAFT_PENDING"
        with closing(sqlite3.connect(journal)) as db:
            db.execute("DROP TRIGGER interrupt")
    stream[:] = [pulse, MarketEvent(240_000, INSTRUMENT, "1m", live_bar(180_000).bar), InterruptedError("partial")]
    with pytest.raises(InterruptedError, match="partial"):
        run()
    stream[:] = [MarketEvent(300_000, INSTRUMENT, "1m", live_bar(240_000).bar)]
    run()
    events = read_journal(journal).events
    assert receipts[0].accepted and receipts[0].plan_id == "draft-draft-v1"
    assert receipts == [receipts[0]] * (2 if interruption == "lost_ack" else 1)
    assert [e.kind for e in events].count("DRAFT_RESOLVED") == 1
    assert [e.plan_id for e in events if e.kind == "PLAN_SUBMITTED"] == ["draft-draft-v1"]
    fills = [e for e in events if e.kind == "ENTRY_FILLED"]
    assert [e.quantity for e in fills] == [Decimal(".2"), Decimal(".2")]
    assert fills[-1].position.quantity == Decimal(".4")


def test_refreeze_reads_earlier_pages_of_same_snapshot(tmp_path):
    import httpx
    from talaria.account.paper import PaperInbox, DraftIntake
    from talaria.foundation.paper import DraftEvidence
    from talaria.service_endpoint import ServiceConnection
    from unittest.mock import patch

    cutoff = 1002 * 60_000
    last_page = [_bar().model_copy(update={"timestamp": i * 60_000, "ts_available": (i + 1) * 60_000})
                 for i in range(2, 1002)]
    def reply(req):
        if req.method == "POST":
            evidence = DraftEvidence(instrument_id=INSTRUMENT, cutoff_ts=cutoff, snapshot_id="frozen", bars=last_page)
            return httpx.Response(200, json=evidence.model_dump(mode="json"))
        assert req.url.path == "/api/snapshots/frozen/bars"
        assert req.url.params["before_ts"] == "120000"
        return httpx.Response(200, json={"bars": [_bar(high="110").model_dump(mode="json")]})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        inbox = PaperInbox(tmp_path, ["sa-one"], client)
        with patch("talaria.account.paper.read_service_connection", return_value=ServiceConnection("http://local", "test")):
            intake = DraftIntake(inbox.freeze, lambda _: pytest.fail("已过期"), lambda event: None)
            assert intake.receive(_draft(), cutoff).reason == "DRAFT_STALE"


def test_legacy_interruption_after_plan_submission_recovers_existing_result():
    from talaria.account.contracts import ExecutionEvent
    from talaria.account.paper import DraftIntake
    from talaria.foundation.paper import DraftReceipt
    provisional = DraftReceipt(draft_id="draft", version=1, accepted=False, reason="DRAFT_INTERRUPTED", snapshot_id="fresh")
    previous = [
        ExecutionEvent(kind="DRAFT_RECEIVED", sub_account_id="sa-one", ts_event=120000, draft_receipt=provisional),
        ExecutionEvent(kind="PLAN_SUBMITTED", sub_account_id="sa-one", ts_event=120000, plan_id="old-plan"),
    ]
    intake = DraftIntake(lambda *_: pytest.fail("应取回原结果"), lambda _: pytest.fail("重复投递"), lambda _: None, previous)
    result = intake.receive(_draft(), 180000)
    assert result.accepted and result.plan_id == "old-plan"


def test_current_minute_at_exact_cutoff_can_prove_staleness():
    from talaria.account.paper import DraftIntake
    rows = [_bar(), _bar(high="106").model_copy(update={"timestamp": 120000})]
    intake = DraftIntake(lambda *_: ("fresh", rows), lambda _: pytest.fail("已越过价带"), lambda _: None)
    assert intake.receive(_draft(), 120000).reason == "DRAFT_STALE"


def test_ready_handshake_without_first_minute_keeps_draft_inbox_closed(tmp_path):
    import asyncio
    from talaria.account.plugins.live_ws import LiveWS
    from talaria.account.scheme import PluginContext
    from talaria.foundation.strategy_subscription import SubscriptionCursor

    async def feed(*args, on_connection, on_cursor, **kwargs):
        on_cursor(SubscriptionCursor(epoch="service", seq=7), 120000)
        on_connection(True)
        await asyncio.Event().wait()
        yield

    plugin = LiveWS(PluginContext(tmp_path, "paper", 0, lambda: 0), {"instrument": INSTRUMENT, "derivatives": False},
                    subscribe=feed, now_ms=lambda: 120000)
    with closing(plugin.events()) as events:
        assert not next(events).ready
        assert plugin.checkpoint()["cursors"]["1m"]["seq"] == 7
