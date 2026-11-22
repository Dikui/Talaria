"""CLI 参数边界：单机/双进程、回环地址、端口与退出码。"""

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
from pathlib import Path

import pytest

from talaria.cli.main import main, validate_loopback_host
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import (
    read_service_descriptor,
    service_descriptor_path,
    write_service_descriptor,
)

pytestmark = pytest.mark.small


def test_up_dual_rejects_same_port(capsys) -> None:
    assert main(["up", "--port", "8765", "--dsh-port", "8765"]) == 2
    assert "不能与 Talaria 守护进程端口相同" in capsys.readouterr().err


def test_up_rejects_non_loopback_host(capsys) -> None:
    assert main(["up", "--solo", "--host", "0.0.0.0"]) == 2
    assert "只允许绑定回环地址" in capsys.readouterr().err


def test_up_rejects_invalid_port(capsys) -> None:
    assert main(["up", "--solo", "--port", "0"]) == 2
    assert "端口必须在 1..65535" in capsys.readouterr().err


# 任务单 04：两个命令都列出已注册的市场包。
@pytest.mark.spec
@pytest.mark.parametrize(
    "arguments",
    [
        ["up"],
        [
            "evaluate-levels", "--instrument", "TEST", "--timeframe", "1m",
            "--from", "0", "--to", "60000",
        ],
    ],
)
def test_unknown_market_pack_lists_choices(arguments, capsys) -> None:
    with pytest.raises(SystemExit) as error:
        main([*arguments, "--pack", "unknown-market"])
    assert error.value.code == 2
    stderr = capsys.readouterr().err
    assert "unknown-market" in stderr
    assert "crypto_perp" in stderr


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1"])
def test_loopback_hosts_are_accepted(host: str) -> None:
    assert validate_loopback_host(host) == host


@pytest.mark.regression
@pytest.mark.parametrize(
    ("host", "url_host", "canonical_host", "canonical_url_host"),
    [
        ("127.0.0.2", "127.0.0.2", "127.0.0.2", "127.0.0.2"),
        ("0:0:0:0:0:0:0:1", "[0:0:0:0:0:0:0:1]", "::1", "[::1]"),
    ],
)
def test_up_loopback_host_can_be_published_in_service_descriptor(
    tmp_path, host, url_host, canonical_host, canonical_url_host
):
    # platform/01: CLI 绑定地址和磁盘接口描述采用相同的回环范围与规范化。
    assert validate_loopback_host(host) == canonical_host
    path = service_descriptor_path(tmp_path)
    write_service_descriptor(
        path,
        base_url=f"http://{url_host}:8765/",
        token="test-token",
        pid=os.getpid(),
        features=FeatureToggles(),
    )
    descriptor = read_service_descriptor(tmp_path)
    assert descriptor is not None
    assert descriptor.base_url == f"http://{canonical_url_host}:8765"
    assert json.loads(path.read_text())["base_url"] == descriptor.base_url


def test_contract_check_has_zero_drift(capsys) -> None:
    assert main(["contract-check"]) == 0
    assert "零漂移" in capsys.readouterr().out


def test_up_rejects_missing_replay_file(capsys, tmp_path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert main(["up", "--solo", "--replay-events", str(missing)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


def test_up_rejects_replay_directory(capsys, tmp_path) -> None:
    assert main(["up", "--solo", "--replay-events", str(tmp_path)]) == 2
    assert "回放文件不存在" in capsys.readouterr().err


@pytest.mark.parametrize("speed", ["0", "-1", "-2.5"])
def test_up_rejects_non_positive_replay_speed(capsys, tmp_path, speed: str) -> None:
    replay = tmp_path / "demo.jsonl"
    replay.write_text('{"offset_ms": 0, "type": "heartbeat"}\n', encoding="utf-8")
    assert main(["up", "--solo", "--replay-events", str(replay), "--replay-speed", speed]) == 2
    assert "回放速度必须为正数" in capsys.readouterr().err


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _get(url: str, *, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError):
        return 0, b""


def _wait_for(predicate, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("等待超时")


def test_up_dual_keeps_fast_path_when_dsh_fails(tmp_path: Path) -> None:
    """DSH 起不来时，Python 守护进程、健康探针、快照与 /desk 仍可用。"""
    fake_dsh = tmp_path / "dsh"
    fake_dsh.write_text("#!/bin/sh\necho 'fatal: no dsh' >&2\nexit 7\n", encoding="utf-8")
    fake_dsh.chmod(0o755)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>desk</title>", encoding="utf-8")
    data_dir = tmp_path / "data"
    port = _free_port()
    dsh_port = _free_port()
    env = os.environ.copy()
    env["TALARIA_DSH_BIN"] = str(fake_dsh)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))",
            "up",
            "--port",
            str(port),
            "--dsh-port",
            str(dsh_port),
            "--data-dir",
            str(data_dir),
            "--dist-dir",
            str(dist),
            "--fixture-dir",
            "tests/fixtures/bybit",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(lambda: _get(f"http://127.0.0.1:{port}/api/health")[0] == 200)

        def offline_payload() -> dict | None:
            payload = json.loads(
                _get(f"http://127.0.0.1:{port}/api/collaboration")[1] or b"{}"
            )
            return payload if payload.get("host") == "offline" else None

        collaboration = _wait_for(offline_payload)
        assert collaboration["mode"] == "dual"
        assert "启动失败" in collaboration["detail"]

        assert _get(f"http://127.0.0.1:{port}/desk")[0] == 200

        descriptor = data_dir / "runtime" / "dsh-bridge.json"
        token = _wait_for(
            lambda: json.loads(descriptor.read_text(encoding="utf-8"))["token"]
            if descriptor.is_file()
            else None
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/snapshots",
            data=json.dumps(
                {
                    "instrument_id": "BTC-USDT-PERP",
                    "timeframes": ["1m"],
                    "mode": "HISTORICAL_PIT",
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5.0) as response:
            snapshot = json.loads(response.read())
        assert response.status == 200
        assert snapshot["snapshot_id"]
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 清理兜底
            process.kill()
            process.wait(timeout=5)
    output = process.stdout.read() if process.stdout is not None else ""
    assert "Talaria 看盘" in output
    assert "DSH 协同宿主：离线" in output


def test_up_dual_keeps_fast_path_when_dsh_binary_missing(tmp_path: Path) -> None:
    """DSH 可执行文件缺失时，协同状态必须是 offline 而不是一直 starting。"""
    data_dir = tmp_path / "data"
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>desk</title>", encoding="utf-8")
    port = _free_port()
    dsh_port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from talaria.cli.main import main; sys.exit(main(sys.argv[1:]))",
            "up",
            "--port",
            str(port),
            "--dsh-port",
            str(dsh_port),
            "--dsh-bin",
            str(tmp_path / "does-not-exist-dsh"),
            "--data-dir",
            str(data_dir),
            "--dist-dir",
            str(dist),
            "--fixture-dir",
            "tests/fixtures/bybit",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for(lambda: _get(f"http://127.0.0.1:{port}/api/health")[0] == 200)

        def offline_payload() -> dict | None:
            payload = json.loads(
                _get(f"http://127.0.0.1:{port}/api/collaboration")[1] or b"{}"
            )
            return payload if payload.get("host") == "offline" else None

        collaboration = _wait_for(offline_payload)
        assert "不存在" in collaboration["detail"] or "未找到" in collaboration["detail"]
        assert _get(f"http://127.0.0.1:{port}/desk")[0] == 200
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - 清理兜底
            process.kill()
            process.wait(timeout=5)
    output = process.stdout.read() if process.stdout is not None else ""
    assert "DSH 协同宿主：离线" in output
