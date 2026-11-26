"""端到端验收（Ticket 05）：同一草案的 Rust 定仓预演数量与 Python 账户进程实际定仓数量逐字符一致。"""

from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import httpx
import pytest

from talaria.account.contracts import AccountState, DecisionOrigin, EntryCandidate, TradingState
from talaria.account.paper_projection import atomic_write_projection, projection_path
from talaria.account.plugins.r_unit import RUnitSizer
from talaria.account.risk_config import AccountRiskConfig
from talaria.foundation.paper import (
    MarginMode,
    PaperAccountState,
    PaperAccountView,
    PaperProcessInfo,
    PaperProcessState,
    TradingState as FoundationTradingState,
)
from talaria.packs import DEFAULT_PACK, get_market_pack
from talaria.service_endpoint import ServiceConnection, read_service_connection

pytestmark = pytest.mark.medium
INSTRUMENT = "BTC-USDT-PERP"
REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "engine"
FIXTURE_DIR = REPO_ROOT / "tests/fixtures/bybit"
REPLAY_FILE = REPO_ROOT / "tests/fixtures/realtime/in_progress.jsonl"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _talaria_data_binary() -> Path:
    candidates = [
        ENGINE_DIR / "target/debug/talaria-data",
        ENGINE_DIR / "target/release/talaria-data",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if shutil.which("cargo"):
        subprocess.run(["cargo", "build", "--bin", "talaria-data"], cwd=ENGINE_DIR, check=True)
        return ENGINE_DIR / "target/debug/talaria-data"
    pytest.skip("talaria-data binary unavailable")


def _wait_for_service(data_dir: Path, proc: subprocess.Popen[str], timeout: float = 15.0) -> ServiceConnection:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=2)
            raise RuntimeError(f"talaria-data exited early ({proc.returncode}):\n{stdout}\n{stderr}")
        conn = read_service_connection(data_dir)
        if conn is not None:
            try:
                resp = httpx.get(
                    f"{conn.base_url}/api/health",
                    headers={"Authorization": f"Bearer {conn.token}"},
                    timeout=1.0,
                )
                if resp.status_code == 200:
                    return conn
            except httpx.HTTPError:
                pass
        time.sleep(0.1)
    raise TimeoutError("Timed out waiting for talaria-data")


def test_rust_sizing_preview_matches_python_account_process_r_unit_sizer(tmp_path: Path) -> None:
    data_dir = tmp_path / "sizing-e2e"
    data_dir.mkdir(parents=True)

    equity = Decimal("25000")
    risk_config = AccountRiskConfig(
        initial_capital=equity,
        currency="USDT",
        margin_mode="ISOLATED",
        leverage=Decimal("5"),
        r_unit_pct=Decimal("0.005"),
        max_single_r_pct=Decimal("0.01"),
        friction_buffer_pct=Decimal("0.002"),
    )
    now_ms = int(time.time() * 1000)
    view = PaperAccountView(
        sub_account_id="sa-e2e-sizing",
        positions=[],
        rounds=[],
        fills=[],
        ledger_seq=9,
        account_state=PaperAccountState(
            currency="USDT",
            equity=equity,
            cash_available=equity,
            margin_used=Decimal("0"),
            trading_state=FoundationTradingState.ACTIVE,
            intraday_drawdown_pct=Decimal("0"),
            peak_drawdown_pct=Decimal("0"),
            open_r=Decimal("0"),
            risk_reserved=Decimal("0"),
            margin_mode=MarginMode.ISOLATED,
            leverage=risk_config.leverage,
            r_unit_pct=risk_config.r_unit_pct,
            max_single_r_pct=risk_config.max_single_r_pct,
            friction_buffer_pct=risk_config.friction_buffer_pct,
            liquidation_buffer_pct=risk_config.liquidation_buffer_pct,
            liquidation_buffer_ticks=risk_config.liquidation_buffer_ticks,
        ),
        process=PaperProcessInfo(
            pid=os.getpid(),
            scheme="btc-breakout-paper",
            started_ts=now_ms - 10_000,
            heartbeat_ts=now_ms + 60_000,
            status=PaperProcessState.RUNNING,
        ),
    )
    atomic_write_projection(projection_path(data_dir, "sa-e2e-sizing"), view)

    binary = _talaria_data_binary()
    proc = subprocess.Popen(
        [
            str(binary),
            "replay",
            "--data-dir",
            str(data_dir),
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port()),
            "--pack",
            "crypto_perp",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--replay-file",
            str(REPLAY_FILE),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        conn = _wait_for_service(data_dir, proc)
        headers = {"Authorization": f"Bearer {conn.token}"}
        pack = get_market_pack(DEFAULT_PACK)
        valuation = pack.margin_valuation()
        sizer = RUnitSizer(pack, {"sa-e2e-sizing": risk_config})
        account_state = AccountState(
            sub_account_id="sa-e2e-sizing",
            balances={("USDT", "CASH_AVAILABLE"): equity},
            rounds={},
            trading_state=TradingState.ACTIVE,
            as_of=now_ms,
        )

        with httpx.Client(base_url=conn.base_url, headers=headers, timeout=10.0) as api:
            for direction, entry_low, entry_high, stop, tp in (
                ("LONG", Decimal("64500"), Decimal("64600"), Decimal("64000"), (Decimal("66000"),)),
                ("SHORT", Decimal("64500"), Decimal("64600"), Decimal("65200"), (Decimal("63000"),)),
            ):
                resp = api.post(
                    "/api/paper/sizing-preview",
                    json={
                        "sub_account_id": "sa-e2e-sizing",
                        "instrument_id": INSTRUMENT,
                        "direction": direction,
                        "entry_low": str(entry_low),
                        "entry_high": str(entry_high),
                        "invalidation_stop": str(stop),
                        "take_profit": [str(p) for p in tp],
                    },
                )
                assert resp.status_code == 200, resp.text
                preview = resp.json()
                assert preview["rejected_reason"] is None
                assert preview["from_offline_snapshot"] is False
                assert preview["based_on_ledger_seq"] == 9

                candidate = EntryCandidate(
                    sub_account_id="sa-e2e-sizing",
                    instrument_id=INSTRUMENT,
                    direction=direction,
                    entry_low=entry_low,
                    entry_high=entry_high,
                    invalidation_stop=stop,
                    take_profit=tp,
                    ts_decision=now_ms,
                    origin=DecisionOrigin.HUMAN,
                )
                actual = sizer.preview_sizing(candidate, account_state, valuation)
                assert Decimal(preview["quantity"]) == actual.sizing.quantity
                assert preview["basis"] == actual.sizing.basis
                assert actual.liquidation_price is not None
                assert abs(Decimal(preview["est_liq_price"]) - actual.liquidation_price) <= Decimal("0.00000001")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
