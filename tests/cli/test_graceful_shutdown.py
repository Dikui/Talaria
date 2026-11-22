"""守护进程真实子进程验收：健康检查后 SIGTERM，5 秒内退出码 0。"""

from __future__ import annotations

import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from talaria.cli import main as cli
from talaria.workbench_api.service_lock import probe_lock

pytestmark = pytest.mark.medium


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_healthy(port: int, process: subprocess.Popen, timeout: float = 15.0) -> dict:
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
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                response = client.get(url)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"健康检查超时：{url}（{last_error}）")


def test_sigterm_shuts_down_gracefully(tmp_path, fixture_dir, repo_root: Path) -> None:
    port = _free_port()
    data_dir = tmp_path / "data"
    command = [
        sys.executable,
        "-m",
        "talaria",
        "up",
        "--solo",
        "--port",
        str(port),
        "--data-dir",
        str(data_dir),
        "--fixture-dir",
        str(fixture_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        health = _wait_healthy(port, process)
        assert health["ok"] is True
        assert health["feed_mode"] == "fixture"

        process.send_signal(signal.SIGTERM)
        try:
            returncode = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("SIGTERM 后 5 秒内未退出")
        stdout, stderr = process.communicate(timeout=1)
        assert returncode == 0, f"退出码 {returncode}\nstdout={stdout}\nstderr={stderr}"
        assert (data_dir / "state" / "runtime_snapshots.sqlite").is_file()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.regression
def test_up_keeps_lock_until_timed_out_worker_finishes(tmp_path, monkeypatch) -> None:
    # platform/01: 模拟启动超时后仍需等待的存储关闭，缩短 CLI 的等待时钟。
    shutdown_waiting = threading.Event()
    finish_storage = threading.Event()
    cli_returned = threading.Event()
    observed_holders = []

    class SlowServer:
        started = False
        should_exit = False

        def __init__(self, config):
            pass

        def run(self):
            finish_storage.wait(timeout=5)

    class Worker(threading.Thread):
        def join(self, timeout=None):
            shutdown_waiting.set()
            # 原有有限等待会提前返回，实际测试无需等十秒。
            super().join(timeout=0 if timeout is not None else None)

    def probe_during_shutdown():
        try:
            if shutdown_waiting.wait(timeout=5):
                cli_returned.wait(timeout=0.1)
                observed_holders.append(probe_lock(tmp_path))
        finally:
            finish_storage.set()

    clock = iter((0, 21))
    monkeypatch.setattr(cli.uvicorn, "Server", SlowServer)
    monkeypatch.setattr(cli, "threading", SimpleNamespace(Thread=Worker))
    monkeypatch.setattr(cli, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    observer = threading.Thread(target=probe_during_shutdown)
    observer.start()
    try:
        assert cli.main(["up", "--solo", "--data-dir", str(tmp_path)]) == 2
    finally:
        cli_returned.set()
        observer.join(timeout=5)

    assert len(observed_holders) == 1
    assert observed_holders[0] is not None
    assert probe_lock(tmp_path) is None
