"""Ticket 04 端到端与契约测试：人工干预命令通道（Rust talaria-data + Python 账户进程）。

覆盖：
1. 全部 8 种 PaperCommandKind 端到端用例（Rust 服务 + AccountProcess + BarSim1m + SevenStepRisk）；
2. MODIFY_TAKE_PROFIT 收紧直达 / 放宽过风控（ADR-0015 / ADR-0025）与 NOOP；
3. WIDEN_STOP 超出 max_single_r_pct 被风控拒绝并返回 MAX_SINGLE_R_EXCEEDED；
4. 对已平仓回合及屏障已变回合返回 COMMAND_STALE；
5. 账户进程在领取后、回执前崩溃，重启后按 command_id 幂等不重复执行、不丢命令；
6. CLOSE_ALL 提交到投影显示平仓的本机实测耗时 <= 2 秒；
7. DraftIntake 接受草案时填充 DraftReceipt.quantity。
"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import subprocess
import threading
import time

import httpx
import pytest

from talaria.account import host
from talaria.account.contracts import (
    EntryCandidate,
    ExecutionEvent,
    MarketEvent,
    PlanReceipt,
    TradingState as AccountTradingState,
)
from talaria.account.execution import ExecutionGate, _tp_widens
from talaria.account.ledger import read_journal
from talaria.account.paper import CommandIntake, DraftIntake, PaperInbox
from talaria.account.paper_projection import projection_path
from talaria.account.plugins.live_ws import PaperMark, PaperPulse
from talaria.account.risk_config import AccountRiskConfig
from talaria.account.scheme import INTERFACE_VERSION, Slot, SlotPlugin
from talaria.components import ComponentMeta
from talaria.foundation.bars import BarStatus, CompactBar, RangeBar
from talaria.foundation.derivatives import DerivativeKind, DerivativePoint
from talaria.foundation.paper import (
    ConfirmedDraft,
    DraftContent,
    PaperAccountView,
    PaperCommand,
    PaperCommandKind,
    PaperCommandPayload,
    PaperCommandRecord,
    TradingState,
)
from talaria.service_endpoint import ServiceConnection, read_service_connection

pytestmark = pytest.mark.medium
INSTRUMENT = "BTC-USDT-PERP"
REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
FIXTURE_DIR = REPO_ROOT / "tests/fixtures/bybit"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _talaria_data_binary() -> Path:
    binary = ENGINE_DIR / "target/debug/talaria-data"
    if binary.is_file() and os.access(binary, os.X_OK):
        return binary
    if shutil.which("cargo"):
        subprocess.run(["cargo", "build", "--bin", "talaria-data"], cwd=ENGINE_DIR, check=True)
        return binary
    pytest.skip("talaria-data binary unavailable")


def _start_rust_service(data_dir: Path) -> tuple[subprocess.Popen[str], ServiceConnection]:
    replay_file = data_dir / "replay.jsonl"
    replay_file.write_text(json.dumps({"offset_ms": 0, "type": "heartbeat"}) + "\n", encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [
            str(_talaria_data_binary()),
            "replay",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--replay-events",
            str(replay_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            raise RuntimeError(f"talaria-data exited early ({proc.returncode}):\n{out}\n{err}")
        conn = read_service_connection(data_dir)
        if conn is not None:
            try:
                resp = httpx.get(
                    f"{conn.base_url}/api/health",
                    headers={"Authorization": f"Bearer {conn.token}"},
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    return proc, conn
            except httpx.HTTPError:
                pass
        time.sleep(0.05)
    proc.terminate()
    raise TimeoutError("Timed out waiting for talaria-data")


def _stop_proc(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _mark_point(ts: int, price: str) -> DerivativePoint:
    return DerivativePoint(
        instrument_id=INSTRUMENT,
        kind=DerivativeKind.MARK_PRICE,
        value=Decimal(price),
        ts_event=ts,
        ts_available=ts,
        precision="SECOND_VERIFIED",
        ts_ingested=ts,
        ts_committed=ts,
        revision=0,
    )


def _bar(ts: int, open_: str, high: str, low: str, close: str, volume: str = "100") -> RangeBar:
    return RangeBar(
        ts_event=ts,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        turnover=Decimal(close) * Decimal(volume),
        ts_available=ts + 60_000,
        precision="SECOND_VERIFIED",
        confirmed=True,
        revision=0,
    )


def _make_scheme(tmp_path: Path, *, max_single_r_pct: str = "0.01") -> Path:
    scheme = tmp_path / "paper-cmd.yaml"
    scheme.write_text(
        f"""\
scheme: paper-cmd-e2e
interface_version: 2
sub_accounts: [sa-cmd]
account_risk:
  sa-cmd:
    initial_capital: "10000"
    currency: USDT
    margin_mode: CROSS
    leverage: "5"
    r_unit_pct: "0.005"
    max_single_r_pct: "{max_single_r_pct}"
    friction_buffer_pct: "0.002"
slots:
  data: data_feed
  strategy: open_once
  sizing: r_unit
  risk: seven_step
  fill: bar_sim_1m
""",
        encoding="utf-8",
    )
    return scheme


def _submit_and_wait_receipt(
    client: httpx.Client,
    conn: ServiceConnection,
    cmd: PaperCommand,
    timeout: float = 5.0,
) -> PaperCommandRecord:
    headers = {"Authorization": f"Bearer {conn.token}"}
    resp = client.post(
        f"{conn.base_url}/api/paper/commands",
        headers=headers,
        json=cmd.model_dump(mode="json"),
    )
    assert resp.status_code == 200, f"submit failed: {resp.status_code} {resp.text}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        listed = client.get(
            f"{conn.base_url}/api/paper/commands",
            headers=headers,
            params={"sub_account_id": cmd.sub_account_id},
        ).json()
        for raw in listed:
            rec = PaperCommandRecord.model_validate(raw)
            if rec.command.command_id == cmd.command_id and rec.receipt is not None:
                return rec
        time.sleep(0.05)
    raise TimeoutError(f"Timed out waiting for receipt of {cmd.command_id}")


def _fetch_view(client: httpx.Client, conn: ServiceConnection, account: str) -> PaperAccountView:
    resp = client.get(
        f"{conn.base_url}/api/paper/accounts/{account}",
        headers={"Authorization": f"Bearer {conn.token}"},
    )
    assert resp.status_code == 200, resp.text
    return PaperAccountView.model_validate(resp.json())


def test_tp_widens_classification_and_draft_receipt_quantity():
    """Spec: ADR-0015 / ADR-0025 — 止盈推远判放宽、拉近判收紧；DraftIntake 接受草案时填充 quantity。"""
    assert _tp_widens("LONG", (Decimal("110"),), (Decimal("115"),)) is True
    assert _tp_widens("LONG", (Decimal("110"),), (Decimal("108"),)) is False
    assert _tp_widens("SHORT", (Decimal("90"),), (Decimal("85"),)) is True
    assert _tp_widens("SHORT", (Decimal("90"),), (Decimal("92"),)) is False

    content = DraftContent(
        sub_account_id="sa-one",
        instrument_id=INSTRUMENT,
        snapshot_id="basis",
        direction="LONG",
        entry_low="100",
        entry_high="105",
        invalidation_stop="98",
        take_profit=["120"],
    )
    draft = ConfirmedDraft(
        draft_id="d-qty",
        version=1,
        content_hash=content.digest(),
        content=content,
        basis_cutoff_ts=60_000,
        created_ts=60_000,
        confirmed_ts=90_000,
        confirmed_by="trader",
    )
    bar = CompactBar(
        timestamp=60_000,
        open="102",
        high="104",
        low="99",
        close="103",
        volume="2",
        turnover="201",
        status=BarStatus.NORMAL,
        ts_available=120_000,
        precision="SECOND_VERIFIED",
    )
    recorded: list[ExecutionEvent] = []
    intake = DraftIntake(
        lambda d, c: ("fresh", (bar,)),
        lambda cand: PlanReceipt(accepted=True, plan_id=cand.draft_plan_id),
        recorded.append,
        quantity_lookup=lambda pid: Decimal("0.25") if pid == "draft-d-qty-v1" else None,
    )
    receipt = intake.receive(draft, 120_000)
    assert receipt is not None
    assert receipt.accepted is True
    assert receipt.quantity == Decimal("0.25")


def test_all_8_intervention_command_kinds_and_latency_e2e(tmp_path: Path):
    """Spec: Ticket 04 — 全部 8 种 kind 端到端（Rust talaria-data + Python 账户进程），含风控拒单、STALE、NOOP 及全平 <=2s。"""
    proc, conn = _start_rust_service(tmp_path)
    try:
        scheme = _make_scheme(tmp_path)
        event_queue: queue.Queue[MarketEvent | None] = queue.Queue()

        class LiveQueueData:
            def events(self):
                while True:
                    item = event_queue.get()
                    if item is None:
                        return
                    yield item

        class OpenEntryStrategy:
            meta = ComponentMeta("open_once", "1", (), {}, "strategy_decision")

            def __init__(self) -> None:
                self._count = 0

            def on_bar(self, ctx):
                from talaria.foundation.collaboration.contracts import Abstain, SetupInsight

                self._count += 1
                if self._count == 1:
                    # Filled position on next bar (100..102)
                    return SetupInsight(
                        direction="LONG",
                        entry_low=Decimal("100"),
                        entry_high=Decimal("102"),
                        invalidation_stop=Decimal("98"),
                        take_profit=[Decimal("112")],
                        summary="e2e-open",
                    )
                if self._count == 2:
                    # Resting pending entry (way below market 101 so it stays PENDING_ENTRY for CANCEL_ENTRY)
                    return SetupInsight(
                        direction="LONG",
                        entry_low=Decimal("80"),
                        entry_high=Decimal("81"),
                        invalidation_stop=Decimal("78"),
                        take_profit=[Decimal("95")],
                        summary="e2e-pending",
                    )
                return Abstain(reason="EVIDENCE_CONFLICT")

        from talaria.account.contracts import MarketData
        from talaria.sdk import BarsResult

        bars_history: list[RangeBar] = []

        def make_source(bar: RangeBar) -> MarketData:
            bars_history.append(bar)
            snap = tuple(bars_history)
            return MarketData(
                history_start=0,
                read_bars=lambda *a, **kw: BarsResult(snap, True),
                read_derivatives=lambda *a, **kw: (),
                signal_as_of=bar.ts_available,
            )

        base_reg = host.default_registry()
        base_reg.register("data_feed", SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), lambda *_: LiveQueueData()))
        base_reg.register(
            "open_once", SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), lambda *_: OpenEntryStrategy()),
        )

        # Start account process in a background thread with real PaperInbox hitting Rust talaria-data.
        run_error: list[BaseException] = []

        def _runner():
            try:
                host.run_paper(scheme, tmp_path, registry=base_reg)
            except BaseException as exc:  # pragma: no cover
                run_error.append(exc)

        thread = threading.Thread(target=_runner, daemon=True)
        thread.start()

        # Feed mark + 2 bars with timestamps >= wall clock so BarSim1m paper ready_at allows immediate fill.
        base_ts = (int(time.time() * 1000) // 60_000 + 2) * 60_000
        b1 = _bar(base_ts, "100", "101", "99.5", "100.5")
        event_queue.put(
            MarketEvent(
                base_ts + 60_000,
                INSTRUMENT,
                "1m",
                b1,
                source=make_source(b1),
                derivative_points=(_mark_point(base_ts + 60_000, "100.5"),),
            )
        )
        # Bar 2 (ts_event > ready_at) fills the LONG entry at open (100.5 inside 100..102).
        b2 = _bar(base_ts + 120_000, "100.5", "101.5", "100", "101")
        event_queue.put(
            MarketEvent(
                base_ts + 180_000,
                INSTRUMENT,
                "1m",
                b2,
                derivative_points=(_mark_point(base_ts + 180_000, "101"),),
            )
        )

        # Background pulse pump (~100ms) keeps heartbeat fresh and polls commands fast.
        stop_pulses = threading.Event()
        pulse_clock = [base_ts + 181_000]

        def _pump():
            while not stop_pulses.is_set():
                pulse_clock[0] += 100
                event_queue.put(PaperPulse(pulse_clock[0], INSTRUMENT, "1m", None, ready=True))
                time.sleep(0.08)

        pump_thread = threading.Thread(target=_pump, daemon=True)
        pump_thread.start()

        try:
            with httpx.Client(trust_env=False, timeout=3.0) as client:
                # Wait until position 1 is open in projection.
                deadline = time.monotonic() + 8.0
                view: PaperAccountView | None = None
                while time.monotonic() < deadline:
                    if projection_path(tmp_path, "sa-cmd").is_file():
                        v = _fetch_view(client, conn, "sa-cmd")
                        if len(v.positions) == 1:
                            view = v
                            break
                    time.sleep(0.05)
                assert view is not None, f"Position did not open; run_error={run_error}"
                round1 = view.positions[0]
                assert round1.quantity > 0
                assert round1.stop == Decimal("98")
                initial_qty = round1.quantity

                # 1. TIGHTEN_STOP: 98 -> 99
                r_tighten = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-tighten",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.TIGHTEN_STOP,
                        payload=PaperCommandPayload(stop=Decimal("99")),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_tighten.receipt is not None and r_tighten.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.positions[0].stop == Decimal("99")

                # Stale barrier check: using old based_on_ledger_seq (before tighten) -> COMMAND_STALE
                r_stale_barrier = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-stale-barrier",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.TIGHTEN_STOP,
                        payload=PaperCommandPayload(stop=Decimal("99.5")),
                        based_on_ledger_seq=1,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_stale_barrier.receipt is not None
                assert r_stale_barrier.receipt.accepted is False
                assert r_stale_barrier.receipt.reason == "COMMAND_STALE"

                # 2. WIDEN_STOP exceeding max_single_r_pct (0.01 = 100 USDT on 10k equity) -> REJECTED!
                # Qty is ~22 units, widening stop from 99 down to 85 creates >300 USDT risk >> 100 USDT max_single_r!
                r_widen_rej = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-widen-reject",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.WIDEN_STOP,
                        payload=PaperCommandPayload(stop=Decimal("85")),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_widen_rej.receipt is not None
                assert r_widen_rej.receipt.accepted is False
                assert r_widen_rej.receipt.reason == "MAX_SINGLE_R_EXCEEDED"

                # 2b. WIDEN_STOP within max_single_r_pct (99 -> 98.5) -> APPROVED via risk slot!
                r_widen_ok = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-widen-ok",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.WIDEN_STOP,
                        payload=PaperCommandPayload(stop=Decimal("98.5")),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_widen_ok.receipt is not None and r_widen_ok.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.positions[0].stop == Decimal("98.5")

                # 3. MOVE_TO_BREAKEVEN -> snaps stop to entry price (~100.6); repeat -> NOOP!
                r_be = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-be",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.MOVE_TO_BREAKEVEN,
                        payload=PaperCommandPayload(),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_be.receipt is not None and r_be.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.positions[0].stop >= (view.positions[0].entry_price or Decimal("100"))

                r_be_noop = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-be-noop",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.MOVE_TO_BREAKEVEN,
                        payload=PaperCommandPayload(),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_be_noop.receipt is not None
                assert r_be_noop.receipt.accepted is True
                assert r_be_noop.receipt.reason == "NOOP"

                # 4. MODIFY_TAKE_PROFIT: tighten (112 -> 108) and widen (108 -> 115)
                r_tp_tight = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-tp-tight",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.MODIFY_TAKE_PROFIT,
                        payload=PaperCommandPayload(take_profit=(Decimal("108"),)),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_tp_tight.receipt is not None and r_tp_tight.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.positions[0].take_profit == (Decimal("108"),)

                r_tp_widen = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-tp-widen",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.MODIFY_TAKE_PROFIT,
                        payload=PaperCommandPayload(take_profit=(Decimal("115"),)),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_tp_widen.receipt is not None and r_tp_widen.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.positions[0].take_profit == (Decimal("115"),)

                # 5. SET_TRADING_STATE: ACTIVE -> REDUCING (direct), verify widening TP blocked in REDUCING,
                #    then REDUCING -> HALTED (direct), then HALTED -> ACTIVE (via risk check)
                r_red = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-state-reducing",
                        sub_account_id="sa-cmd",
                        kind=PaperCommandKind.SET_TRADING_STATE,
                        payload=PaperCommandPayload(trading_state=TradingState.REDUCING, reason="manual_pause"),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_red.receipt is not None and r_red.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.account_state is not None
                assert view.account_state.trading_state == TradingState.REDUCING

                # Widening TP while REDUCING must be blocked by risk gate!
                r_tp_blocked = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-tp-widen-blocked",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.MODIFY_TAKE_PROFIT,
                        payload=PaperCommandPayload(take_profit=(Decimal("120"),)),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_tp_blocked.receipt is not None
                assert r_tp_blocked.receipt.accepted is False
                assert r_tp_blocked.receipt.reason == "TRADING_STATE_BLOCKED"

                r_halt = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-state-halted",
                        sub_account_id="sa-cmd",
                        kind=PaperCommandKind.SET_TRADING_STATE,
                        payload=PaperCommandPayload(trading_state=TradingState.HALTED, reason="emergency"),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_halt.receipt is not None and r_halt.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.account_state is not None
                assert view.account_state.trading_state == TradingState.HALTED

                r_resume = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-state-active",
                        sub_account_id="sa-cmd",
                        kind=PaperCommandKind.SET_TRADING_STATE,
                        payload=PaperCommandPayload(trading_state=TradingState.ACTIVE, reason="resume"),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_resume.receipt is not None and r_resume.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert view.account_state is not None
                assert view.account_state.trading_state == TradingState.ACTIVE

                # 6. REDUCE_RATIO (50%)
                r_reduce = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-reduce-50",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.REDUCE_RATIO,
                        payload=PaperCommandPayload(ratio=Decimal("0.5")),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_reduce.receipt is not None and r_reduce.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                assert len(view.positions) == 1
                assert Decimal(0) < view.positions[0].quantity < initial_qty

                # 7. CLOSE_ALL + verify latency from submit to closed projection <= 2.0s!
                t0 = time.monotonic()
                r_close = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-close-all",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.CLOSE_ALL,
                        payload=PaperCommandPayload(),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_close.receipt is not None and r_close.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                close_elapsed = time.monotonic() - t0
                assert view.positions == []
                closed_round = next(r for r in view.rounds if r.round_id == round1.round_id)
                assert closed_round.phase == "CLOSED"
                assert close_elapsed <= 2.0, f"CLOSE_ALL took {close_elapsed:.3f}s (> 2.0s)"
                print(f"[T04] CLOSE_ALL submit->closed projection latency: {close_elapsed * 1000:.1f} ms")

                # Command on already-closed round -> COMMAND_STALE!
                r_closed_stale = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-after-close",
                        sub_account_id="sa-cmd",
                        round_id=round1.round_id,
                        kind=PaperCommandKind.CLOSE_ALL,
                        payload=PaperCommandPayload(),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_closed_stale.receipt is not None
                assert r_closed_stale.receipt.accepted is False
                assert r_closed_stale.receipt.reason == "COMMAND_STALE"

                # 8. CANCEL_ENTRY: trigger strategy bar #2 to create a resting PENDING_ENTRY round, then cancel it.
                b3_ts = (pulse_clock[0] // 60_000 + 2) * 60_000
                b3 = _bar(b3_ts, "101", "102", "100.5", "101.5")
                pulse_clock[0] = b3_ts + 61_000
                event_queue.put(
                    MarketEvent(
                        b3_ts + 60_000,
                        INSTRUMENT,
                        "1m",
                        b3,
                        source=make_source(b3),
                        derivative_points=(_mark_point(b3_ts + 60_000, "101.5"),),
                    )
                )

                pending_round_id: str | None = None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    view = _fetch_view(client, conn, "sa-cmd")
                    pending = [r for r in view.rounds if r.phase == "PENDING_ENTRY"]
                    if pending:
                        pending_round_id = pending[0].round_id
                        break
                    time.sleep(0.05)
                assert pending_round_id is not None

                r_cancel = _submit_and_wait_receipt(
                    client,
                    conn,
                    PaperCommand(
                        command_id="cmd-cancel-entry",
                        sub_account_id="sa-cmd",
                        round_id=pending_round_id,
                        kind=PaperCommandKind.CANCEL_ENTRY,
                        payload=PaperCommandPayload(),
                        based_on_ledger_seq=view.ledger_seq,
                        issued_by="trader",
                        issued_ts=pulse_clock[0],
                    ),
                )
                assert r_cancel.receipt is not None and r_cancel.receipt.accepted is True
                view = _fetch_view(client, conn, "sa-cmd")
                cancelled_round = next(r for r in view.rounds if r.round_id == pending_round_id)
                assert cancelled_round.phase == "CANCELLED"
        finally:
            stop_pulses.set()
            pump_thread.join(timeout=2)
            event_queue.put(None)
            thread.join(timeout=5)
        assert not run_error
    finally:
        _stop_proc(proc)


def test_crash_before_ack_restart_is_idempotent_without_double_execution(tmp_path: Path, monkeypatch):
    """Spec: Ticket 04 — 领取后、回执前进程被杀，重启后不重复执行 REDUCE_RATIO 也不丢命令。"""
    proc, conn = _start_rust_service(tmp_path)
    try:
        scheme = _make_scheme(tmp_path)
        from talaria.account.contracts import MarketData
        from talaria.sdk import BarsResult

        class OpenOnceStrategy:
            meta = ComponentMeta("open_once", "1", (), {}, "strategy_decision")

            def __init__(self, should_open: bool) -> None:
                self._opened = not should_open

            def on_bar(self, ctx):
                from talaria.foundation.collaboration.contracts import Abstain, SetupInsight

                if not self._opened:
                    self._opened = True
                    return SetupInsight(
                        direction="LONG",
                        entry_low=Decimal("100"),
                        entry_high=Decimal("102"),
                        invalidation_stop=Decimal("98"),
                        take_profit=[Decimal("112")],
                        summary="crash-test",
                    )
                return Abstain(reason="EVIDENCE_CONFLICT")

        class ScriptedData:
            def __init__(self, events: list[MarketEvent]) -> None:
                self._events = events

            def events(self):
                yield from self._events

        base_ts = (int(time.time() * 1000) // 60_000 + 2) * 60_000
        b1 = _bar(base_ts, "100", "101", "99.5", "100.5")
        b2 = _bar(base_ts + 120_000, "100.5", "101.5", "100", "101")
        src1 = MarketData(
            history_start=0,
            read_bars=lambda *a, **kw: BarsResult((b1,), True),
            read_derivatives=lambda *a, **kw: (),
            signal_as_of=b1.ts_available,
        )

        # Phase 1: open position and write projection (keep it running long enough to submit command,
        # then simulate crash right before acknowledge_command!).
        original_ack = PaperInbox.acknowledge_command
        crash_triggered = False

        class SimulatedCrash(RuntimeError):
            pass

        def crashing_ack(self, account, receipt):
            nonlocal crash_triggered
            crash_triggered = True
            raise SimulatedCrash("killed before ack")

        with httpx.Client(trust_env=False, timeout=3.0) as client:
            # First run: open position cleanly and let it emit projection, then queue REDUCE_RATIO
            # and run a pulse where acknowledge_command crashes!
            q1: queue.Queue[MarketEvent | None] = queue.Queue()

            class QData:
                def events(self):
                    while True:
                        ev = q1.get()
                        if ev is None:
                            return
                        yield ev

            reg1 = host.default_registry()
            reg1.register("data_feed", SlotPlugin(Slot.DATA, (INTERFACE_VERSION,), lambda *_: QData()))
            reg1.register(
                "open_once", SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), lambda *_: OpenOnceStrategy(True)),
            )

            monkeypatch.setattr(PaperInbox, "acknowledge_command", crashing_ack)
            crashed_exc: list[BaseException] = []

            def _run_crash():
                try:
                    host.run_paper(scheme, tmp_path, registry=reg1)
                except BaseException as exc:
                    crashed_exc.append(exc)

            t1 = threading.Thread(target=_run_crash, daemon=True)
            t1.start()

            q1.put(
                MarketEvent(
                    base_ts + 60_000,
                    INSTRUMENT,
                    "1m",
                    b1,
                    source=src1,
                    derivative_points=(_mark_point(base_ts + 60_000, "100.5"),),
                )
            )
            q1.put(
                MarketEvent(
                    base_ts + 180_000,
                    INSTRUMENT,
                    "1m",
                    b2,
                    derivative_points=(_mark_point(base_ts + 180_000, "101"),),
                )
            )
            q1.put(PaperPulse(base_ts + 181_000, INSTRUMENT, "1m", None, ready=True))

            deadline = time.monotonic() + 8.0
            view: PaperAccountView | None = None
            while time.monotonic() < deadline:
                if projection_path(tmp_path, "sa-cmd").is_file():
                    v = _fetch_view(client, conn, "sa-cmd")
                    if len(v.positions) == 1:
                        view = v
                        break
                time.sleep(0.05)
            assert view is not None
            qty_before = view.positions[0].quantity
            round_id = view.positions[0].round_id

            # Submit REDUCE_RATIO 0.5 while process is online.
            cmd = PaperCommand(
                command_id="cmd-crash-idempotent",
                sub_account_id="sa-cmd",
                round_id=round_id,
                kind=PaperCommandKind.REDUCE_RATIO,
                payload=PaperCommandPayload(ratio=Decimal("0.5")),
                based_on_ledger_seq=view.ledger_seq,
                issued_by="trader",
                issued_ts=base_ts + 181_500,
            )
            resp = client.post(
                f"{conn.base_url}/api/paper/commands",
                headers={"Authorization": f"Bearer {conn.token}"},
                json=cmd.model_dump(mode="json"),
            )
            assert resp.status_code == 200, resp.text

            # Deliver pulse so process claims & executes cmd, commits ledger, then crashes before ack!
            q1.put(PaperPulse(base_ts + 182_000, INSTRUMENT, "1m", None, ready=True))
            t1.join(timeout=5)
            assert crash_triggered
            assert crashed_exc and isinstance(crashed_exc[0], SimulatedCrash)

            # Verify ledger already reduced once, while Rust service STILL has no receipt!
            view_after_crash = _fetch_view(client, conn, "sa-cmd")
            qty_after_first = view_after_crash.positions[0].quantity
            assert Decimal(0) < qty_after_first < qty_before

            listed_before_restart = client.get(
                f"{conn.base_url}/api/paper/commands",
                headers={"Authorization": f"Bearer {conn.token}"},
                params={"sub_account_id": "sa-cmd"},
            ).json()
            rec_before = PaperCommandRecord.model_validate(listed_before_restart[0])
            assert rec_before.claimed is True
            assert rec_before.receipt is None

            # Phase 2: restore normal acknowledge_command and restart AccountProcess!
            monkeypatch.setattr(PaperInbox, "acknowledge_command", original_ack)
            reg2 = host.default_registry()
            reg2.register(
                "data_feed",
                SlotPlugin(
                    Slot.DATA,
                    (INTERFACE_VERSION,),
                    lambda *_: ScriptedData([PaperPulse(base_ts + 185_000, INSTRUMENT, "1m", None, ready=True)]),
                ),
            )
            reg2.register(
                "open_once", SlotPlugin(Slot.STRATEGY, (INTERFACE_VERSION,), lambda *_: OpenOnceStrategy(False)),
            )
            host.run_paper(scheme, tmp_path, registry=reg2)

            # Verify Rust service received the receipt AND quantity was NOT reduced a second time!
            listed_after_restart = client.get(
                f"{conn.base_url}/api/paper/commands",
                headers={"Authorization": f"Bearer {conn.token}"},
                params={"sub_account_id": "sa-cmd"},
            ).json()
            rec_after = PaperCommandRecord.model_validate(listed_after_restart[0])
            assert rec_after.receipt is not None
            assert rec_after.receipt.accepted is True

            view_after_restart = _fetch_view(client, conn, "sa-cmd")
            assert view_after_restart.positions[0].quantity == qty_after_first
            journal = read_journal(tmp_path / "accounts" / "sa-cmd" / "ledger.sqlite")
            exit_fills = [e for e in journal.events if e.kind == "EXIT_FILLED"]
            assert len(exit_fills) == 1
    finally:
        _stop_proc(proc)
