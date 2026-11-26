"""Ticket 03 / ADR-0025: 模拟盘账户只读投影、心跳与重启一致性测试。"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from talaria.account import host
from talaria.account.contracts import (
    AccountState,
    ApprovedExecutionPlan,
    DecisionOrigin,
    EntryCandidate,
    ExecutionEvent,
    ExitBarrierSpec,
    LedgerPosting,
    MarketEvent,
    RoundPhase,
    RoundStatus,
    SizingDecision,
    TradingState as AccountTradingState,
)
from talaria.account.ledger import open_ledger, read_journal
from talaria.account.ownership import hold_sub_accounts
from talaria.account.paper_ledger import open_paper_ledger
from talaria.account.plugins.live_ws import PaperPulse
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import INTERFACE_VERSION, Slot, SlotPlugin
from talaria.components import ComponentMeta
from talaria.foundation.bars import RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.paper import (
    PaperAccountView,
    PaperFeedStatus,
    PaperProcessInfo,
    PaperProcessState,
    TradingState,
)
from talaria.packs import DEFAULT_PACK, get_market_pack

pytestmark = pytest.mark.small
INSTRUMENT = "BTC-USDT-PERP"


def _strip_heartbeat(raw: bytes) -> bytes:
    payload = json.loads(raw.decode("utf-8"))
    if payload.get("process") is not None:
        payload["process"].pop("heartbeat_ts", None)
    return json.dumps(payload, separators=(",", ":"), sort_keys=False).encode("utf-8")


def test_projection_derivation_is_byte_identical_excluding_heartbeat_and_caps_fills(tmp_path: Path):
    """Spec: ADR-0025 & Ticket 03 — 相同账本重复推导投影除 heartbeat_ts 外逐字节一致，且成交条数有上限。"""
    from talaria.account.paper_projection import (
        MAX_PROJECTION_FILLS,
        atomic_write_projection,
        build_paper_account_view,
        projection_path,
        serialize_projection,
    )

    ledger_file = tmp_path / "accounts" / "sa-alpha" / "ledger.sqlite"
    barriers = ExitBarrierSpec(stop=Decimal("95"), take_profit=(Decimal("115"),), max_holding_ms=3_600_000)

    with open_ledger(ledger_file, None, ("sa-alpha",), resume=True) as writer:
        events: list[ExecutionEvent] = [
            ExecutionEvent(
                kind="ACCOUNT_FUNDED",
                sub_account_id="sa-alpha",
                ts_event=0,
                postings=(
                    LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount=Decimal("10000")),
                    LedgerPosting(account="OWNER_CAPITAL", currency="USDT", amount=Decimal("-10000")),
                ),
            )
        ]
        # Generate > MAX_PROJECTION_FILLS fills to verify deterministic capping.
        for i in range(MAX_PROJECTION_FILLS + 10):
            ts = (i + 1) * 60_000
            rid = f"round-{i}"
            events.append(
                ExecutionEvent(
                    kind="ENTRY_FILLED",
                    sub_account_id="sa-alpha",
                    ts_event=ts,
                    plan_id=f"plan-{i}",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    position=RoundStatus(
                        round_id=rid,
                        sub_account_id="sa-alpha",
                        instrument_id=INSTRUMENT,
                        direction="LONG",
                        quantity=Decimal("1"),
                        stop=Decimal("95"),
                        phase=RoundPhase.ACTIVE_PROTECTED,
                    ),
                    barriers=barriers,
                    take_profit=(Decimal("115"),),
                    reason="TAKER",
                    postings=(
                        LedgerPosting(account="POSITION_COST", currency="USDT", amount=Decimal("100")),
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount=Decimal("-100")),
                        LedgerPosting(account="MARGIN_USED", currency="USDT", amount=Decimal("20")),
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount=Decimal("-20.06")),
                        LedgerPosting(account="EXPENSE_EXECUTION_FEE", currency="USDT", amount=Decimal("0.06")),
                    ),
                )
            )
            events.append(
                ExecutionEvent(
                    kind="EXIT_FILLED",
                    sub_account_id="sa-alpha",
                    ts_event=ts + 30_000,
                    plan_id=f"plan-{i}",
                    quantity=Decimal("1"),
                    price=Decimal("102"),
                    position=RoundStatus(
                        round_id=rid,
                        sub_account_id="sa-alpha",
                        instrument_id=INSTRUMENT,
                        direction="LONG",
                        quantity=Decimal("0"),
                        stop=Decimal("95"),
                        phase=RoundPhase.CLOSED,
                    ),
                    reason="TAKE_PROFIT",
                    postings=(
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount=Decimal("102")),
                        LedgerPosting(account="POSITION_COST", currency="USDT", amount=Decimal("-100")),
                        LedgerPosting(account="REALIZED_PNL", currency="USDT", amount=Decimal("-2")),
                        LedgerPosting(account="MARGIN_USED", currency="USDT", amount=Decimal("-20")),
                        LedgerPosting(account="EXPENSE_EXECUTION_FEE", currency="USDT", amount=Decimal("0.06")),
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount=Decimal("19.94")),
                    ),
                )
            )
        writer.record(events)
        balances = writer.balances("sa-alpha")

    journal = read_journal(ledger_file)
    risk_cfg = AccountRiskConfig()
    pack = get_market_pack(DEFAULT_PACK)

    pending_plan = ApprovedExecutionPlan(
        plan_id="plan-pending",
        round_id="round-pending",
        candidate=EntryCandidate(
            sub_account_id="sa-alpha",
            instrument_id=INSTRUMENT,
            direction="LONG",
            entry_low=Decimal("99"),
            entry_high=Decimal("101"),
            invalidation_stop=Decimal("95"),
            take_profit=(Decimal("115"),),
            ts_decision=99_000_000,
            origin=DecisionOrigin.HUMAN,
        ),
        sizing=SizingDecision(quantity=Decimal("0.5"), basis="r_unit"),
        barriers=barriers,
    )
    mark = DerivativePoint(
        instrument_id=INSTRUMENT,
        kind=DerivativeKind.MARK_PRICE,
        value=Decimal("103"),
        ts_event=99_000_000,
        ts_available=99_000_000,
        precision="SECOND_VERIFIED",
        ts_ingested=99_000_000,
        ts_committed=99_000_000,
        revision=0,
    )
    account_state = AccountState(
        sub_account_id="sa-alpha",
        balances=balances,
        rounds={},
        trading_state=AccountTradingState.ACTIVE,
        as_of=99_000_000,
        marks={INSTRUMENT: mark},
    )

    view1 = build_paper_account_view(
        "sa-alpha",
        journal,
        account_state,
        risk_cfg,
        pack=pack,
        pending_plans=(pending_plan,),
        feed_status=PaperFeedStatus(healthy=True, updated_ts=99_000_000),
        process=PaperProcessInfo(
            pid=1234,
            scheme="paper",
            started_ts=1_000,
            heartbeat_ts=2_000,
            status=PaperProcessState.RUNNING,
        ),
    )
    view2 = build_paper_account_view(
        "sa-alpha",
        journal,
        account_state,
        risk_cfg,
        pack=pack,
        pending_plans=(pending_plan,),
        feed_status=PaperFeedStatus(healthy=True, updated_ts=99_000_000),
        process=PaperProcessInfo(
            pid=1234,
            scheme="paper",
            started_ts=1_000,
            heartbeat_ts=9_999,
            status=PaperProcessState.RUNNING,
        ),
    )

    raw1 = serialize_projection(view1)
    raw2 = serialize_projection(view2)
    assert raw1 != raw2  # heartbeat_ts differs
    assert _strip_heartbeat(raw1) == _strip_heartbeat(raw2)

    assert len(view1.fills) == MAX_PROJECTION_FILLS
    assert view1.fills[0].event_id == f"event-{len(journal.events)}"
    assert view1.fills[0].realized_pnl == Decimal("2")
    assert view1.fills[0].fee == Decimal("0.06")
    assert view1.ledger_seq == len(journal.events)
    assert view1.account_state is not None
    assert view1.account_state.trading_state == TradingState.ACTIVE
    pending_round = next(r for r in view1.rounds if r.round_id == "round-pending")
    assert pending_round.phase == "PENDING_ENTRY"
    assert pending_round.entry_low == Decimal("99")
    assert pending_round.entry_high == Decimal("101")

    target = projection_path(tmp_path, "sa-alpha")
    atomic_write_projection(target, view1)
    assert target.is_file()
    assert not target.with_name(f".{target.name}.tmp").exists()
    assert PaperAccountView.model_validate_json(target.read_text(encoding="utf-8")) == view1


def test_account_process_writes_heartbeat_and_stopped_projection_and_recovers_consistently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Spec: Ticket 03 — 账户进程运行期间写投影与心跳，退出标 STOPPED，重启后与账本重放一致。"""
    from talaria.account.paper_projection import projection_path

    observed_during_run: list[PaperAccountView] = []

    class Data:
        def __init__(self, pulses: list[PaperPulse]) -> None:
            self._pulses = pulses

        def events(self):
            view_file = projection_path(tmp_path, "sa-one")
            for pulse in self._pulses:
                yield pulse
                assert view_file.is_file()
                observed_during_run.append(
                    PaperAccountView.model_validate_json(view_file.read_text(encoding="utf-8"))
                )

    class Strategy:
        meta = ComponentMeta("quiet", "1", (), {}, "strategy_decision")

        def on_bar(self, ctx):
            pytest.fail("PaperPulse should not invoke strategy")

    scheme = tmp_path / "paper.yaml"
    scheme.write_text(
        """\
scheme: paper-proj-test
interface_version: 2
sub_accounts: [sa-one]
slots: {data: data_fixture, strategy: quiet, sizing: r_unit, risk: seven_step, fill: bar_sim_1m}
""",
        encoding="utf-8",
    )

    base_default_registry = host.default_registry

    def _run_once(pulses: list[PaperPulse]) -> PaperAccountView:
        registry = base_default_registry()
        registry.register(
            "data_fixture",
            SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), lambda *_: Data(pulses)),
        )
        registry.register(
            "quiet",
            SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), lambda *_: Strategy()),
        )
        monkeypatch.setattr(host, "default_registry", lambda: registry)
        host.run_paper(scheme, tmp_path, registry=registry)
        return PaperAccountView.model_validate_json(
            projection_path(tmp_path, "sa-one").read_text(encoding="utf-8")
        )

    stopped_first = _run_once([
        PaperPulse(120_000, INSTRUMENT, "1m", None, ready=True),
        PaperPulse(121_000, INSTRUMENT, "1m", None, ready=False, reduce_reason="SUBSCRIPTION_LOST"),
    ])
    assert len(observed_during_run) == 2
    first_running, second_running = observed_during_run
    assert first_running.process is not None
    assert first_running.process.status == PaperProcessState.RUNNING
    assert first_running.process.pid == os.getpid()
    assert first_running.process.scheme == "paper-proj-test"
    assert first_running.feed_status is not None and first_running.feed_status.healthy is True
    assert first_running.account_state is not None
    assert first_running.account_state.equity == Decimal("10000")
    assert first_running.account_state.trading_state == TradingState.ACTIVE

    assert second_running.process is not None
    assert second_running.process.heartbeat_ts >= first_running.process.heartbeat_ts
    assert second_running.feed_status is not None
    assert second_running.feed_status.healthy is False
    assert second_running.feed_status.reduce_reason == "SUBSCRIPTION_LOST"
    assert second_running.account_state is not None
    assert second_running.account_state.trading_state == TradingState.REDUCING

    assert stopped_first.process is not None
    assert stopped_first.process.status == PaperProcessState.STOPPED
    assert stopped_first.process.stopped_ts is not None

    # Restart against the same ledger and verify recovered state matches ledger replay.
    observed_during_run.clear()
    stopped_restarted = _run_once([PaperPulse(122_000, INSTRUMENT, "1m", None, ready=True)])
    assert stopped_restarted.sub_account_id == "sa-one"
    assert stopped_restarted.ledger_seq == stopped_first.ledger_seq
    assert stopped_restarted.account_state is not None
    assert stopped_first.account_state is not None
    assert stopped_restarted.account_state.equity == stopped_first.account_state.equity
    assert stopped_restarted.account_state.trading_state == stopped_first.account_state.trading_state
    assert stopped_restarted.positions == stopped_first.positions
    assert stopped_restarted.rounds == stopped_first.rounds
    assert stopped_restarted.fills == stopped_first.fills
    assert stopped_restarted.process is not None
    assert stopped_restarted.process.status == PaperProcessState.STOPPED


def test_projection_computes_open_position_unrealized_pnl_and_drawdowns(tmp_path: Path):
    """Spec: ADR-0025 — 活跃持仓推导 entry_price / mark_price / unrealized_pnl 及回撤/敞口。"""
    from talaria.account.contracts import RoundExposure
    from talaria.account.paper_projection import build_paper_account_view
    from talaria.foundation.margin import MarginPosition

    ledger_file = tmp_path / "accounts" / "sa-pos" / "ledger.sqlite"
    barriers = ExitBarrierSpec(stop=Decimal("95"), take_profit=(Decimal("115"),), max_holding_ms=3_600_000)
    pos = RoundStatus(
        round_id="r-open",
        sub_account_id="sa-pos",
        instrument_id=INSTRUMENT,
        direction="LONG",
        quantity=Decimal("2"),
        stop=Decimal("95"),
        phase=RoundPhase.ACTIVE_PROTECTED,
    )
    with open_ledger(ledger_file, None, ("sa-pos",), resume=True) as writer:
        writer.record(
            [
                ExecutionEvent(
                    kind="ACCOUNT_FUNDED",
                    sub_account_id="sa-pos",
                    ts_event=0,
                    postings=(
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount=Decimal("10000")),
                        LedgerPosting(account="OWNER_CAPITAL", currency="USDT", amount=Decimal("-10000")),
                    ),
                ),
                ExecutionEvent(
                    kind="ENTRY_FILLED",
                    sub_account_id="sa-pos",
                    ts_event=60_000,
                    plan_id="p-1",
                    quantity=Decimal("2"),
                    price=Decimal("100"),
                    position=pos,
                    barriers=barriers,
                    take_profit=(Decimal("115"),),
                    postings=(
                        LedgerPosting(account="POSITION_COST", currency="USDT", amount=Decimal("200")),
                        LedgerPosting(account="TRADE_PAYABLE", currency="USDT", amount=Decimal("-200")),
                        LedgerPosting(account="MARGIN_USED", currency="USDT", amount=Decimal("40")),
                        LedgerPosting(account="CASH_AVAILABLE", currency="USDT", amount=Decimal("-40.12")),
                        LedgerPosting(account="EXPENSE_EXECUTION_FEE", currency="USDT", amount=Decimal("0.12")),
                    ),
                ),
            ]
        )
        balances = writer.balances("sa-pos")

    journal = read_journal(ledger_file)
    mark = DerivativePoint(
        instrument_id=INSTRUMENT,
        kind=DerivativeKind.MARK_PRICE,
        value=Decimal("105"),
        ts_event=120_000,
        ts_available=120_000,
        precision="SECOND_VERIFIED",
        ts_ingested=120_000,
        ts_committed=120_000,
        revision=0,
    )
    exposure = RoundExposure(
        round_id="r-open",
        position=MarginPosition(INSTRUMENT, "LONG", Decimal("2"), Decimal("100")),
        stop=Decimal("95"),
    )
    state = AccountState(
        sub_account_id="sa-pos",
        balances=balances,
        rounds={"r-open": pos},
        trading_state=AccountTradingState.ACTIVE,
        as_of=120_000,
        positions=(exposure,),
        barriers={"r-open": barriers},
        marks={INSTRUMENT: mark},
    )
    view = build_paper_account_view(
        "sa-pos",
        journal,
        state,
        AccountRiskConfig(),
        drawdown_bounds=(Decimal("10200"), Decimal("10500")),
        feed_status=PaperFeedStatus(healthy=False, service_warning="404 draft route"),
    )
    assert len(view.positions) == 1
    p = view.positions[0]
    assert p.entry_price == Decimal("100")
    assert p.mark_price == Decimal("105")
    assert p.unrealized_pnl == Decimal("10")
    assert p.opened_ts == 60_000
    assert p.closed_ts is None
    assert view.account_state is not None
    assert view.account_state.margin_used == Decimal("40")
    assert view.account_state.equity == Decimal("10009.88")
    assert view.account_state.intraday_drawdown_pct > 0
    assert view.account_state.peak_drawdown_pct > view.account_state.intraday_drawdown_pct
    assert view.account_state.risk_reserved > 0
    assert view.account_state.open_r > 0
    assert view.feed_status is not None
    assert view.feed_status.service_warning == "404 draft route"

