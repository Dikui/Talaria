from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from contextlib import closing
from io import BytesIO
from pathlib import Path
from urllib.response import addinfourl

import compression.zstd as zstd
import pytest
import uvicorn

from talaria.cli.main import main
from talaria.workbench_api.app import create_app
from talaria.workbench_api.service_config import FeatureToggles
from talaria.workbench_api.service_descriptor import (
    service_descriptor_path,
    write_service_descriptor,
)
from talaria.workbench_api.service_lock import ServiceLock, probe_lock
from talaria.workbench_api.settings import AppSettings
from talaria.workbench_api.state_store import StateStore

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
TOKEN = "test-token"
NOW_MS = 1_795_022_974_103

pytestmark = pytest.mark.medium

_HOLDER = """
import sys
from pathlib import Path

from talaria.workbench_api.service_lock import ServiceLock

lock = ServiceLock(Path(sys.argv[1]), role="usage-scan")
lock.acquire()
print("locked", flush=True)
sys.stdin.readline()
lock.release()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_session(root: Path, session_id: str = "sess-a") -> None:
    directory = root / "sessions" / "project" / session_id
    directory.mkdir(parents=True)
    payload = (
        json.dumps({"type": "session", "version": 4, "id": session_id})
        + "\n"
        + json.dumps(
            {
                "type": "assistant/message",
                "data": {
                    "turn": 1,
                    "step": 1,
                    "usage": {"inputTokens": 100, "outputTokens": 10},
                },
            }
        )
        + "\n"
    ).encode()
    (directory / "session.v4.jsonl.zstd").write_bytes(zstd.compress(payload))


def _summary(port: int) -> dict:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/collaboration/usage", timeout=5
    ) as response:
        return json.load(response)


@pytest.fixture
def running_service(tmp_path):
    data_dir = tmp_path / "data"
    port = _free_port()
    settings = AppSettings(
        data_dir=data_dir,
        dist_dir=tmp_path / "dist",
        fixture_dir=FIXTURES,
        token=TOKEN,
        now_ms=lambda: NOW_MS,
    )
    app = create_app(settings)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="test-uvicorn", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        if not thread.is_alive():
            raise RuntimeError("uvicorn 测试服务提前退出")
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("uvicorn 测试服务启动超时")

    lock = ServiceLock(data_dir, role="service")
    lock.acquire()
    descriptor = service_descriptor_path(data_dir)
    write_service_descriptor(
        descriptor,
        base_url=f"http://127.0.0.1:{port}",
        token=TOKEN,
        pid=os.getpid(),
        features=FeatureToggles(),
    )
    try:
        yield data_dir, port
    finally:
        lock.release()
        descriptor.unlink(missing_ok=True)
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


def test_usage_scan_writes_through_running_service(running_service, tmp_path, capsys):
    data_dir, port = running_service
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)

    code = main(
        ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
    )

    assert code == 0, capsys.readouterr().err
    first = json.loads(capsys.readouterr().out)
    assert first["scanned_files"] == 1
    assert first["recorded_turns"] == 1
    assert first["duplicate_turns"] == 0
    assert _summary(port)["summary"]["turns"] == 1

    code = main(
        ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
    )
    assert code == 0
    second = json.loads(capsys.readouterr().out)
    assert second["recorded_turns"] == 0
    assert second["duplicate_turns"] == 1
    assert _summary(port)["summary"]["turns"] == 1


@pytest.mark.regression
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_usage_scan_bypasses_environment_proxy_for_service(
    tmp_path, capsys, monkeypatch, host
):
    # platform/04: replace the HTTP handler after urllib has selected the destination.
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("http_proxy", "http://proxy.example:8080")
    monkeypatch.setattr(urllib.request, "_opener", None)
    data_dir = tmp_path / "data"
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)

    def respond(_handler, request):
        assert request.host == f"{host}:8765"
        response = addinfourl(BytesIO(b'{"recorded": true}'), {}, request.full_url, 200)
        response.msg = "OK"
        return response

    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", respond)
    with ServiceLock(data_dir, role="service"):
        write_service_descriptor(
            service_descriptor_path(data_dir),
            base_url=f"http://{host}:8765",
            token=TOKEN,
            pid=os.getpid(),
            features=FeatureToggles(),
        )
        code = main(
            ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
        )

    assert code == 0, capsys.readouterr().err
    assert json.loads(capsys.readouterr().out)["recorded_turns"] == 1


def test_usage_scan_without_service_takes_lock_and_writes_directly(tmp_path, capsys):
    data_dir = tmp_path / "data"
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)

    code = main(
        ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
    )

    assert code == 0, capsys.readouterr().err
    result = json.loads(capsys.readouterr().out)
    assert result["recorded_turns"] == 1
    assert probe_lock(data_dir) is None
    with closing(StateStore(data_dir)) as store:
        assert store.usage.summarize()[0].turns == 1


def test_usage_scan_refuses_when_service_lock_has_no_descriptor(tmp_path, capsys):
    data_dir = tmp_path / "data"
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)
    lock = ServiceLock(data_dir, role="service")
    lock.acquire()
    try:
        code = main(
            ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
        )
    finally:
        lock.release()

    stderr = capsys.readouterr().err
    assert code == 2
    assert "服务正在运行" in stderr
    assert "写入接口" in stderr
    assert not (data_dir / "state" / "collaboration_state.sqlite").exists()


def test_usage_scan_refuses_when_lock_is_held_by_another_process(tmp_path, capsys):
    data_dir = tmp_path / "data"
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(data_dir)],
        cwd=REPO_ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdin is not None
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"

        code = main(
            ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
        )

        stderr = capsys.readouterr().err
        assert code == 2
        assert "另一个进程" in stderr
        assert f"pid={holder.pid}" in stderr
        assert not (data_dir / "state" / "collaboration_state.sqlite").exists()

        holder.stdin.write("release\n")
        holder.stdin.flush()
        assert holder.wait(timeout=10) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)


def test_usage_scan_fails_closed_when_service_endpoint_is_unreachable(tmp_path, capsys):
    data_dir = tmp_path / "data"
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)
    lock = ServiceLock(data_dir, role="service")
    lock.acquire()
    try:
        write_service_descriptor(
            service_descriptor_path(data_dir),
            base_url=f"http://127.0.0.1:{_free_port()}",
            token=TOKEN,
            pid=os.getpid(),
            features=FeatureToggles(),
        )
        code = main(
            ["usage-scan", "--dsh-home", str(dsh_home), "--data-dir", str(data_dir)]
        )
    finally:
        lock.release()

    stderr = capsys.readouterr().err
    assert code == 2
    assert "经服务写入失败" in stderr
    assert "未直写" in stderr
    assert not (data_dir / "state" / "collaboration_state.sqlite").exists()
