"""Ticket 02 端到端测试：Python 账户进程 `PaperInbox` + `DraftIntake` 对接 Rust `talaria-data`。

覆盖：
1. 确认 → 领取 → 重冻结证据 → 定仓风控提交 → 回执；
2. 重复领取幂等（不重复执行）；
3. Rust 服务重启后，未回执的已领取草案重新出站；
4. 服务缺少草案路由（404）或鉴权失败（401）时，账户进程立即输出可见告警（log + stderr + warning hook）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import time
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from talaria.account.contracts import PlanReceipt
from talaria.account.paper import DraftIntake, PaperInbox
from talaria.foundation.paper import DraftContent
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
        subprocess.run(
            ["cargo", "build", "--bin", "talaria-data"],
            cwd=ENGINE_DIR,
            check=True,
        )
        return ENGINE_DIR / "target/debug/talaria-data"
    pytest.skip("talaria-data binary unavailable")


def _wait_for_descriptor(data_dir: Path, proc: subprocess.Popen[str], timeout: float = 15.0) -> ServiceConnection:
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
    raise TimeoutError("Timed out waiting for talaria-data service-descriptor.json")


def _write_live_replay_fixture(path: Path, minute_ts: int) -> None:
    events = [{"offset_ms": 0, "type": "heartbeat"}]
    for base_offset in (50, 600, 1200):
        events.extend(
            [
                {
                    "offset_ms": base_offset,
                    "type": "kline",
                    "event": {
                        "instrument_id": INSTRUMENT,
                        "timeframe": "1m",
                        "bar": {
                            "timestamp": minute_ts - 60_000,
                            "open": "64520",
                            "high": "64560",
                            "low": "64510",
                            "close": "64550",
                            "volume": "12",
                            "turnover": "774600",
                            "status": "NORMAL",
                            "ts_available": minute_ts,
                            "precision": "SECOND_VERIFIED",
                        },
                        "confirmed": True,
                        "revision": 1,
                    },
                },
                {
                    "offset_ms": base_offset + 20,
                    "type": "kline",
                    "event": {
                        "instrument_id": INSTRUMENT,
                        "timeframe": "1m",
                        "bar": {
                            "timestamp": minute_ts,
                            "open": "64550",
                            "high": "64570",
                            "low": "64530",
                            "close": "64555",
                            "volume": "5",
                            "turnover": "322775",
                            "status": "NORMAL",
                            "ts_available": minute_ts,
                            "precision": "SECOND_VERIFIED",
                        },
                        "confirmed": False,
                        "revision": 1,
                    },
                },
                {
                    "offset_ms": base_offset + 40,
                    "type": "kline",
                    "event": {
                        "instrument_id": INSTRUMENT,
                        "timeframe": "1m",
                        "bar": {
                            "timestamp": minute_ts + 60_000,
                            "open": "64555",
                            "high": "64575",
                            "low": "64535",
                            "close": "64560",
                            "volume": "3",
                            "turnover": "193680",
                            "status": "NORMAL",
                            "ts_available": minute_ts,
                            "precision": "SECOND_VERIFIED",
                        },
                        "confirmed": False,
                        "revision": 1,
                    },
                },
            ]
        )
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")


def _start_rust_service(
    data_dir: Path, port: int, replay_file: Path = REPLAY_FILE
) -> tuple[subprocess.Popen[str], ServiceConnection]:
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
            str(port),
            "--pack",
            "crypto_perp",
            "--fixture-dir",
            str(FIXTURE_DIR),
            "--replay-file",
            str(replay_file),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        conn = _wait_for_descriptor(data_dir, proc)
    except Exception:
        proc.terminate()
        proc.wait(timeout=5)
        raise
    return proc, conn


def _stop_rust_service(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _ensure_watchlist_subscribed(api: httpx.Client) -> None:
    wl = api.get("/api/watchlist").json()
    if any(
        item["instrument_id"] == INSTRUMENT
        for group in wl.get("groups", [])
        for item in group.get("items", [])
    ):
        return
    created = api.post("/api/watchlist/groups", json={"name": "e2e"})
    created.raise_for_status()
    group_id = created.json()["groups"][0]["group_id"]
    api.post(
        "/api/watchlist/items",
        json={"group_id": group_id, "instrument_id": INSTRUMENT},
    ).raise_for_status()


def _wait_for_current_bar(api: httpx.Client, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        now = int(time.time() * 1000)
        resp = api.post(
            "/api/paper/draft-evidence",
            json={"instrument_id": INSTRUMENT, "timeframes": ["1m"], "cutoff_ts": now},
        )
        if resp.status_code == 200 and resp.json().get("current_bar") is not None:
            return
        time.sleep(0.1)
    raise TimeoutError("Timed out waiting for replay current_bar in hot_buffer")


def test_python_account_paper_inbox_e2e_against_rust_talaria_data(tmp_path: Path) -> None:
    data_dir = tmp_path / "rust-paper-e2e"
    data_dir.mkdir(parents=True)
    replay_file = tmp_path / "live_e2e_replay.jsonl"
    minute_now = (int(time.time() * 1000) // 60_000) * 60_000
    _write_live_replay_fixture(replay_file, minute_now)

    port = _free_port()
    proc, conn = _start_rust_service(data_dir, port, replay_file=replay_file)
    try:
        headers = {"Authorization": f"Bearer {conn.token}"}
        with httpx.Client(base_url=conn.base_url, headers=headers, timeout=10.0) as api:
            _ensure_watchlist_subscribed(api)
            _wait_for_current_bar(api)
            # 1. Freeze snapshot from Rust service
            snap = api.post("/api/snapshots", json={"instrument_id": INSTRUMENT, "timeframes": ["1m"]}).json()

            # 2. Save & confirm draft via Rust API (verifies Python/Rust DraftContent.digest() byte equality!)
            draft_content = DraftContent(
                sub_account_id="sa-e2e",
                instrument_id=INSTRUMENT,
                snapshot_id=snap["snapshot_id"],
                direction="LONG",
                entry_low=Decimal("64500"),
                entry_high=Decimal("64600"),
                invalidation_stop=Decimal("64000"),
                take_profit=(Decimal("66000"),),
            )
            saved = api.post("/api/paper/drafts", json=draft_content.model_dump(mode="json"))
            assert saved.status_code == 200, saved.text
            record = saved.json()
            assert record["content_hash"] == draft_content.digest()

            confirmed = api.post(
                f"/api/paper/drafts/{record['draft_id']}/confirm",
                json={
                    "version": record["version"],
                    "content_hash": record["content_hash"],
                    "confirmed_by": "e2e-trader",
                },
            )
            assert confirmed.status_code == 200, confirmed.text

            # 3. Account Process claims confirmed draft from Rust service
            with httpx.Client(timeout=10.0) as inbox_client:
                inbox = PaperInbox(data_dir, ["sa-e2e"], inbox_client)
                claim_now = int(time.time() * 1000) + 5_000
                claimed = inbox.claim("sa-e2e", INSTRUMENT, claim_now)
                assert claimed is not None
                assert claimed.draft_id == record["draft_id"]
                assert claimed.content_hash == draft_content.digest()

                # 4. Restart Rust service BEFORE ack -> unacknowledged claimed draft must be redelivered!
        _stop_rust_service(proc)
        proc, conn = _start_rust_service(data_dir, _free_port(), replay_file=replay_file)
        headers = {"Authorization": f"Bearer {conn.token}"}
        with (
            httpx.Client(base_url=conn.base_url, headers=headers, timeout=10.0) as api,
            httpx.Client(timeout=10.0) as inbox_client,
        ):
            _wait_for_current_bar(api)
            inbox = PaperInbox(data_dir, ["sa-e2e"], inbox_client)
            redelivered = inbox.claim("sa-e2e", INSTRUMENT, claim_now)
            assert redelivered is not None
            assert redelivered.draft_id == record["draft_id"]

            # 5. DraftIntake processes the draft once, refreezes evidence against Rust (with current_bar),
            #    and is idempotent on duplicate delivery.
            submitted: list[str] = []
            events = []

            def submit(candidate):
                submitted.append(candidate.draft_plan_id)
                return PlanReceipt(plan_id=candidate.draft_plan_id, accepted=True)

            intake = DraftIntake(inbox.freeze, submit, events.append)
            receive_cutoff = max(redelivered.confirmed_ts, int(time.time() * 1000))
            first_receipt = intake.receive(redelivered, receive_cutoff)
            assert first_receipt is not None
            assert first_receipt.accepted is True
            assert len(submitted) == 1

            # Duplicate claim / receive must NOT execute twice
            second_receipt = intake.receive(redelivered, receive_cutoff)
            assert second_receipt == first_receipt
            assert len(submitted) == 1

            # 6. Acknowledge receipt back to Rust service -> queue drains and draft record reflects receipt
            inbox.acknowledge("sa-e2e", first_receipt)
            assert inbox.claim("sa-e2e", INSTRUMENT, claim_now) is None
            drafts = api.get("/api/paper/drafts").json()
            assert drafts[0]["receipt"]["accepted"] is True
            assert drafts[0]["receipt"]["plan_id"] == first_receipt.plan_id
    finally:
        _stop_rust_service(proc)


def test_paper_inbox_emits_visible_warning_on_404_and_401(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "talaria.account.paper.read_service_connection",
        lambda _dir: ServiceConnection("http://127.0.0.1:9999", "tok"),
    )

    warnings_seen: list[str | None] = []
    status_code = {"value": 404}

    def handler(request: httpx.Request) -> httpx.Response:
        if status_code["value"] == 200:
            return httpx.Response(200, text="null", headers={"content-type": "application/json"})
        return httpx.Response(status_code["value"], json={"code": "NOT_FOUND", "message": "missing"})

    with caplog.at_level(logging.WARNING, logger="talaria.account.paper"):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            inbox = PaperInbox(tmp_path, ["sa-one"], client, on_service_warning=warnings_seen.append)
            assert inbox.claim("sa-one", INSTRUMENT, 1_700_000_000_000) is None
            # Second identical 404 should not spam duplicate warnings
            assert inbox.claim("sa-one", INSTRUMENT, 1_700_000_001_000) is None
            assert inbox.service_warning is not None
            assert "SERVICE_INCOMPATIBLE" in inbox.service_warning
            assert "404" in inbox.service_warning
            assert len(warnings_seen) == 1

            # 401 triggers auth warning
            status_code["value"] = 401
            assert inbox.claim("sa-one", INSTRUMENT, 1_700_000_002_000) is None
            assert inbox.service_warning is not None
            assert "SERVICE_AUTH_FAILED" in inbox.service_warning
            assert "401" in inbox.service_warning
            assert len(warnings_seen) == 2

            # Recovery clears warning
            status_code["value"] = 200
            assert inbox.claim("sa-one", INSTRUMENT, 1_700_000_003_000) is None
            assert inbox.service_warning is None
            assert warnings_seen[-1] is None

    stderr = capsys.readouterr().err
    assert "SERVICE_INCOMPATIBLE" in stderr
    assert "SERVICE_AUTH_FAILED" in stderr
    assert any("SERVICE_INCOMPATIBLE" in rec.message for rec in caplog.records)
