"""M2 双平面协同工作台验收：录制桥接消息 + 真实 ``talaria up`` 子进程。

场景（任务单 20）：

1. ``talaria up`` 同时拉起看盘守护进程与 DSH 宿主替身；
2. 冻结 Evidence Snapshot，框选区间发起会诊，宿主按录制消息经桥接端点
   读取指标/关键位/可视窗口，写入一条合法标注、一条越界标注与一条预警草案，
   最后返回带证据的 ``Setup Insight``；
3. 越界标注被门控拒绝且结构化错误码可断言，标注只落一条、预警保持 DRAFT；
4. 采纳标注生成独立 HUMAN_DRAWING，确认草案转 ARMED；
5. 数值扫描命中后，宿主按录制消息生成简报，再“带入协同”；
6. 停掉宿主后 ``/api/health``、``/desk``、切图与刷新仍可用。

测试不依赖网络与真实模型；所有宿主消息来自
``tests/fixtures/m2/recorded_bridge.jsonl``。
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

pytestmark = pytest.mark.medium

INSTRUMENT = "BTC-USDT-PERP"
TIMEFRAME = "1m"
SESSION_ID = "m2-recorded-session"
DSH_SESSION_ID = "m2-recorded-session"
CONSULT_TURN_ID = "turn-consult-1"
BRIEFING_TURN_ID = "turn-briefing-1"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(port: int, process: subprocess.Popen[str], timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    url = f"http://127.0.0.1:{port}/api/health"
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"守护进程提前退出（{process.returncode}）\nstdout={stdout}\nstderr={stderr}"
            )
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:  # noqa: S310
                if response.status == 200:
                    return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"健康检查超时：{url}（{last_error}）")


def _wait_until(
    probe: Callable[[], Any],
    predicate: Callable[[Any], bool],
    *,
    description: str,
    timeout: float = 40.0,
    interval: float = 0.1,
) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except Exception as exc:  # 轮询期间允许瞬时连接失败
            last = exc
        else:
            if predicate(last):
                return last
        time.sleep(interval)
    raise AssertionError(f"等待超时：{description}；最后值={last!r}")


def _json_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(path)
    assert response.status_code == 200, f"GET {path} -> {response.status_code} {response.text}"
    return response.json()


def _write(client: httpx.Client, token: str, method: str, path: str, body: Any = None) -> Any:
    response = client.request(
        method, path, json=body, headers={"Authorization": f"Bearer {token}"}
    )
    return response


def test_m2_copilot_dual_plane_scenario(
    tmp_path: Path, repo_root: Path, fixture_dir: Path
) -> None:
    m2_dir = repo_root / "tests" / "fixtures" / "m2"
    recording = m2_dir / "recorded_bridge.jsonl"
    host_script = m2_dir / "fake_dsh_host.py"
    assert recording.is_file(), f"缺少录制消息：{recording}"
    assert host_script.is_file(), f"缺少宿主替身：{host_script}"

    bridge_log = tmp_path / "bridge-calls.jsonl"
    wrapper = tmp_path / "dsh"
    wrapper.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{host_script}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    data_dir = tmp_path / "data"
    port = _free_port()
    dsh_port = _free_port()
    env = os.environ.copy()
    env.update(
        {
            "TALARIA_DSH_BIN": str(wrapper),
            "TALARIA_M2_RECORDING": str(recording),
            "TALARIA_M2_LOG": str(bridge_log),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "talaria",
            "up",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--dsh-port",
            str(dsh_port),
            "--data-dir",
            str(data_dir),
            "--fixture-dir",
            str(fixture_dir),
        ],
        cwd=repo_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    host_pid: int | None = None
    try:
        health = _wait_healthy(port, process)
        assert health["ok"] is True
        assert health["feed_mode"] == "fixture"

        descriptor_path = data_dir / "runtime" / "dsh-bridge.json"
        # 健康检查可能早于桥接凭据落盘，等文件出现再读。
        descriptor = _wait_until(
            lambda: json.loads(descriptor_path.read_text(encoding="utf-8")),
            lambda payload: bool(payload.get("token")),
            description="桥接凭据写入",
            timeout=10.0,
        )
        assert descriptor["base_url"] == base
        token = descriptor["token"]
        auth = {"Authorization": f"Bearer {token}"}

        with httpx.Client(base_url=base, trust_env=False, timeout=30.0) as client:
            collaboration = _wait_until(
                lambda: _get(client, "/api/collaboration"),
                lambda payload: payload["host"] == "online",
                description="DSH 宿主上线",
            )
            assert collaboration["mode"] == "dual"
            host_pid = collaboration["pid"]
            assert host_pid

            # 冻结双周期快照：后续所有结论都必须绑定这个 snapshot_id。
            frozen = _write(
                client,
                token,
                "POST",
                "/api/snapshots",
                {"instrument_id": INSTRUMENT, "timeframes": ["1m", "5m"]},
            )
            assert frozen.status_code == 200, frozen.text
            snapshot = frozen.json()
            snapshot_id = snapshot["snapshot_id"]
            assert snapshot["bar_counts"]["1m"] > 0
            assert snapshot["bar_counts"]["5m"] > 0

            page = _get(
                client,
                f"/api/snapshots/{snapshot_id}/bars?timeframe={TIMEFRAME}&limit=300",
            )
            assert page["snapshot_id"] == snapshot_id
            bars = [bar for bar in page["bars"] if bar.get("close") is not None]
            assert bars
            last = max(bars, key=lambda bar: bar["timestamp"])
            last_close = Decimal(str(last["close"]))
            last_ts = int(last["timestamp"])

            def _decimal_text(value: Decimal) -> str:
                return format(value, "f")

            selection = {
                "from_ts": last_ts - 3_600_000,
                "to_ts": last_ts + 60_000,
                "price_min": _decimal_text(last_close - 200),
                "price_max": _decimal_text(last_close + 200),
            }
            visible = {
                "from_ts": min(int(bar["timestamp"]) for bar in bars),
                "to_ts": last_ts + 60_000,
                "price_min": _decimal_text(last_close - 2000),
                "price_max": _decimal_text(last_close + 2000),
            }

            consulted = _write(
                client,
                token,
                "POST",
                "/api/collaboration/consult",
                {
                    "snapshot_id": snapshot_id,
                    "instrument_id": INSTRUMENT,
                    "timeframe": TIMEFRAME,
                    "question": "请评估当前回踩承接机会，并给出失效条件。",
                    "visible": visible,
                    "selection": selection,
                },
            )
            assert consulted.status_code == 202, consulted.text
            consultation_id = consulted.json()["consultation_id"]

            record = _wait_until(
                lambda: _get(client, f"/api/collaboration/consultations/{consultation_id}"),
                lambda payload: payload["status"] in {"READY", "ABSTAIN", "FAILED"},
                description="会诊结束",
            )
            assert record["status"] == "READY", record
            assert record["snapshot_id"] == snapshot_id
            assert record["dsh_session_id"] == DSH_SESSION_ID
            assert record["dsh_turn_id"] == CONSULT_TURN_ID

            insight = record["insight"]
            assert insight is not None, record
            assert insight["direction"] == "LONG"
            entry_low = Decimal(str(insight["entry_low"]))
            entry_high = Decimal(str(insight["entry_high"]))
            # 结论的入场带必须与框选价格区间相交。
            assert entry_low <= Decimal(str(selection["price_max"]))
            assert entry_high >= Decimal(str(selection["price_min"]))
            evidence_ids = {
                ref["evidence_id"] for ref in insight["evidence_refs"]
            }
            assert f"snapshot:{snapshot_id}" in evidence_ids
            assert f"chartview:{consultation_id}:{TIMEFRAME}" in evidence_ids

            # 录制消息经桥接端点实际执行：合法标注落库、越界标注被门控、
            # 预警草案强制 DRAFT。
            entries = _wait_until(
                lambda: _json_log(bridge_log),
                lambda items: sum(
                    1
                    for item in items
                    if item.get("tool") == "report_usage"
                    and item.get("status") == 200
                )
                >= 2,
                description="录制桥接消息执行完成",
            )
            overlays = [
                item for item in entries if item.get("tool") == "write_agent_overlay"
            ]
            assert [item["status"] for item in overlays] == [201, 422], overlays
            rejected = overlays[1]
            assert rejected["code"] == "PRICE_OUT_OF_BOUNDS"
            assert rejected["body"]["message"]

            alerts = [item for item in entries if item.get("tool") == "create_alert"]
            assert len(alerts) == 1 and alerts[0]["status"] == 201, alerts
            assert alerts[0]["body"]["status"] == "DRAFT"

            usage_calls = [
                item
                for item in entries
                if item.get("tool") == "report_usage" and item.get("status") == 200
            ]
            assert len(usage_calls) >= 2
            assert usage_calls[0]["body"]["recorded"] is True
            assert usage_calls[0]["body"]["duplicate"] is False
            assert usage_calls[1]["body"]["recorded"] is False
            assert usage_calls[1]["body"]["duplicate"] is True

            annotations = _get(
                client,
                f"/api/annotations?instrument_id={INSTRUMENT}&snapshot_id={snapshot_id}",
            )["annotations"]
            active = [item for item in annotations if item["status"] == "ACTIVE"]
            assert len(active) == 1, annotations
            annotation = active[0]
            assert annotation["label"] == "回踩承接"
            assert Decimal(str(annotation["price_low"])) == last_close
            assert annotation["ts_anchor"] == last_ts
            assert all(
                item["snapshot_id"] == snapshot_id for item in annotations
            )
            assert not any(item["label"] == "越界价格" for item in annotations)

            adopted = _write(
                client,
                token,
                "POST",
                f"/api/annotations/{annotation['annotation_id']}/adopt",
            )
            assert adopted.status_code == 200, adopted.text
            adoption = adopted.json()
            assert adoption["annotation"]["status"] == "ADOPTED"
            drawing_id = adoption["adoption"]["drawing_id"]
            assert drawing_id != annotation["annotation_id"]
            assert adoption["annotation"]["adopted_drawing_id"] == drawing_id
            assert adoption["adoption"]["snapshot_id"] == snapshot_id

            drawings = _get(
                client,
                f"/api/drawings?instrument_id={INSTRUMENT}&timeframe={TIMEFRAME}",
            )["drawings"]
            adopted_drawing = next(
                item for item in drawings if item["drawing_id"] == drawing_id
            )
            assert adopted_drawing["kind"] == "HORIZONTAL_LINE"
            assert adopted_drawing["visible_timeframes"] == [TIMEFRAME]
            assert adopted_drawing["points"][0]["price"] == format(last_close, "f")

            # 智能体草案保持 DRAFT，人工确认后才转 ARMED。
            drafts = [
                item
                for item in _get(client, "/api/alerts")["alerts"]
                if item["status"] == "DRAFT"
            ]
            draft = next(
                item
                for item in drafts
                if item["note"] == "智能体草案：突破 70000 观察"
            )
            assert draft["instrument_id"] == INSTRUMENT
            confirmed = _write(
                client, token, "POST", f"/api/alerts/{draft['alert_id']}/confirm"
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["status"] == "ARMED"
            assert all(
                item["status"] != "DRAFT"
                for item in _get(client, "/api/alerts")["alerts"]
            )

            # 扫描命中 → 简报。扫描器由后台 ticker 驱动，这里等 READY 简报；
            # 简报的 snapshot_id 必须与触发它的 Trigger 一致。
            briefing = _wait_until(
                lambda: _get(client, "/api/scanner/briefings?limit=20"),
                lambda payload: any(
                    item["status"] == "READY" for item in payload["briefings"]
                ),
                description="扫描简报生成",
                timeout=60.0,
            )["briefings"]
            ready = next(item for item in briefing if item["status"] == "READY")
            assert ready["snapshot_id"]
            assert ready["evidence_refs"]

            triggers = _get(client, "/api/scanner/triggers?limit=50")["triggers"]
            trigger = next(
                item for item in triggers if item["trigger_id"] == ready["trigger_id"]
            )
            assert ready["snapshot_id"] == trigger["snapshot_id"]
            assert trigger["route"] in {"CO_PILOT_CONSULT", "LOCAL"}

            prefilled = _write(
                client,
                token,
                "POST",
                f"/api/scanner/briefings/{ready['id']}/consult",
                {},
            )
            assert prefilled.status_code == 200, prefilled.text
            brought = prefilled.json()
            assert brought["briefing_id"] == ready["id"]
            assert brought["snapshot_id"] == ready["snapshot_id"]
            assert brought["instrument_id"] == ready["instrument_id"]
            assert brought["timeframe"] == ready["timeframe"]
            assert brought["question"].strip()

            read = _write(
                client, token, "POST", f"/api/scanner/briefings/{ready['id']}/read"
            )
            assert read.status_code == 200, read.text
            assert read.json()["read_ts"] is not None
            unread = _get(client, "/api/scanner/briefings?unread_only=true")["briefings"]
            assert all(item["id"] != ready["id"] for item in unread)

            # 同一个 (dsh_session_id, dsh_turn_id) 幂等：会诊一条 + 简报一条。
            usage = _wait_until(
                lambda: _get(client, "/api/collaboration/usage"),
                lambda payload: payload["summary"]["turns"] >= 2,
                description="Token 用量落库",
            )["summary"]
            assert usage["turns"] == 2, usage
            assert usage["sessions"] == 1
            assert usage["input_tokens"] == 2000
            assert usage["cached_input_tokens"] == 400
            assert usage["output_tokens"] == 710

            # 停掉宿主：状态转为离线，快通道全部照常。
            assert host_pid is not None
            os.kill(host_pid, signal.SIGTERM)
            offline = _wait_until(
                lambda: _get(client, "/api/collaboration"),
                lambda payload: payload["host"] == "offline",
                description="宿主离线状态",
                timeout=15.0,
            )
            assert "离线" in offline["message"]

            assert client.get("/api/health").status_code == 200
            desk = client.get("/desk")
            assert desk.status_code == 200
            assert client.cookies.get("talaria_token")
            assert _get(client, f"/api/snapshots/{snapshot_id}")["snapshot_id"] == snapshot_id
            bars_5m = _get(
                client,
                f"/api/snapshots/{snapshot_id}/bars?timeframe=5m&limit=50",
            )
            assert bars_5m["snapshot_id"] == snapshot_id
            assert bars_5m["bars"], "宿主离线后切图仍应返回 5m 柱"
            assert _get(
                client,
                f"/api/chart-view?instrument_id={INSTRUMENT}&timeframe={TIMEFRAME}"
                f"&from_ts={visible['from_ts']}&to_ts={visible['to_ts']}"
                f"&price_min={visible['price_min']}&price_max={visible['price_max']}",
            )
            assert _get(client, f"/api/annotations?instrument_id={INSTRUMENT}")[
                "annotations"
            ]
            assert _get(client, "/api/alerts")["alerts"]
            # 刷新页面仍可拿到会话 Cookie 与前端入口。
            refreshed = client.get("/desk")
            assert refreshed.status_code == 200
            assert token in refreshed.text or "talaria" in refreshed.text.lower()

            missing_auth = client.post("/api/annotations", json={})
            assert missing_auth.status_code == 401
            allowed = client.post(
                "/api/watchlist/groups",
                json={"name": "宿主离线后写入"},
                headers=auth,
            )
            assert allowed.status_code == 200, allowed.text
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if host_pid is not None:
            try:
                os.kill(host_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
